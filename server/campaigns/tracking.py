"""Opt-outs, open / click tracking and delivery events.

What each signal does and does NOT prove:
  accepted     the mail server took the message. NOT proof it reached the inbox.
  delivered    ONLY set from a provider event (ingest_provider_events); plain SMTP can never produce it.
  bounced      the same: only from a provider event (or a bounce processor posting to the events endpoint).
  open         a 1x1 image was requested. A SIGNAL, not a fact: Apple Mail Privacy Protection pre-fetches images for every
               message (inflating opens) and clients that block remote images hide real ones (under-counting).
  click        the tracked link was requested (security scanners also follow links, so this can be inflated too).
"""
import sqlite3
from datetime import datetime, timezone

from events import record_audit

_EVENT_CAP = 50           # stop storing individual open/click rows past this many per message (counters keep counting)
_KIND_ALIASES = {"delivery": "delivered", "delivered": "delivered", "bounce": "bounced", "bounced": "bounced",
                 "complaint": "complained", "complained": "complained", "spamreport": "complained", "open": "open",
                 "opened": "open", "click": "click", "clicked": "click"}


def _now(now=None):
    return (now or datetime.now(timezone.utc)).isoformat(timespec="seconds")


def mask_email(address):
    local, _, domain = str(address or "").partition("@")
    return (local[:1] + "***@" + domain) if domain else ""


# --------------------------------------------------------------------------------------------- suppressions
def suppress(conn, email, csp_code, reason, campaign_id=None, actor=None, now=None):
    email = str(email or "").strip().lower()
    if not email:
        return False
    cur = conn.execute("INSERT OR IGNORE INTO email_suppressions (email, csp_code, reason, source_campaign_id, created_at, created_by) "
                       "VALUES (?,?,?,?,?,?)", (email, csp_code, reason, campaign_id, _now(now), actor))
    return cur.rowcount == 1


def is_suppressed(conn, email):
    return conn.execute("SELECT 1 FROM email_suppressions WHERE email=?", (str(email or "").strip().lower(),)).fetchone() is not None


def remove_suppression(conn, email, actor):
    """Manual removal (e.g. an operator asks to hear from us again). Recorded in the audit log."""
    n = conn.execute("DELETE FROM email_suppressions WHERE email=?", (str(email or "").strip().lower(),)).rowcount
    if n:
        record_audit(conn, actor, "email_suppression_removed", "", mask_email(email))
    return n


# --------------------------------------------------------------------------------------------------- events
def record_event(conn, recipient_id, kind, source, detail="", external_id=None, when=None):
    """Append one event. Returns False if this exact provider event was already recorded (idempotent)."""
    try:
        conn.execute("INSERT INTO email_events (recipient_id, kind, occurred_at, source, external_id, detail) VALUES (?,?,?,?,?,?)",
                     (recipient_id, kind, when or _now(), source, external_id, str(detail or "")[:300]))
        return True
    except sqlite3.IntegrityError:
        return False


def _by_token(conn, token):
    token = str(token or "")
    if not token or len(token) > 64:
        return None
    return conn.execute("SELECT r.*, c.link_url, c.status campaign_status FROM email_campaign_recipients r "
                        "JOIN email_campaigns c ON c.id=r.campaign_id WHERE r.token=?", (token,)).fetchone()


def _touch(conn, row, kind, source, when):
    """Count an open/click (only for a message the server actually accepted: a preview or unsent row is not an open)."""
    if row is None or row["status"] != "accepted":
        return False
    col = "open" if kind == "open" else "click"
    conn.execute(f"UPDATE email_campaign_recipients SET {col}_count={col}_count+1, first_{col}_at=COALESCE(first_{col}_at, ?), updated_at=? WHERE id=?",
                 (when, when, row["id"]))
    n = conn.execute("SELECT COUNT(*) c FROM email_events WHERE recipient_id=? AND kind=?", (row["id"], kind)).fetchone()["c"]
    if n < _EVENT_CAP:
        record_event(conn, row["id"], kind, source, when=when)
    return True


def track_open(conn, token, now=None):
    return _touch(conn, _by_token(conn, token), "open", "pixel", _now(now))


def track_click(conn, token, now=None):
    """-> the campaign's own link (never anything taken from the request), or None."""
    row = _by_token(conn, token)
    if row is None or not row["link_url"] or not str(row["link_url"]).startswith("https://"):
        return None
    _touch(conn, row, "click", "link", _now(now))
    return row["link_url"]


def unsubscribe(conn, token, now=None):
    """-> {"email_masked", "already"} or None for an unknown token. Opts the ADDRESS out of every future campaign and cancels
    any of its messages that have not been sent yet."""
    row = _by_token(conn, token)
    if row is None:
        return None
    when = _now(now)
    address = (row["email_at_send"] or "").strip().lower()
    new = suppress(conn, address, row["csp_code"], "unsubscribed", row["campaign_id"], now=now)
    record_event(conn, row["id"], "unsubscribed", "link", when=when, external_id=None if new else f"again:{when}")
    cancelled = conn.execute("UPDATE email_campaign_recipients SET status='cancelled', last_error='opted out', updated_at=? "
                             "WHERE lower(email_at_send)=? AND status='queued'", (when, address)).rowcount
    if new:
        record_audit(conn, "recipient", "email_unsubscribed", row["csp_code"],
                     f"{mask_email(address)} via campaign #{row['campaign_id']}; {cancelled} queued message(s) cancelled")
    return {"email_masked": mask_email(address), "already": not new}


# -------------------------------------------------------------------------------------- provider events
def ingest_provider_events(conn, events, now=None):
    """Apply delivery / bounce / complaint / open / click events from an email provider or a bounce processor.
    Each event: {"message_id": "<...>", "event": "delivered|bounced|complained|open|click", "timestamp": ISO, "id": "provider id",
    "type": "hard|soft" (bounces), "reason": "..."}. Matching is by the Message-ID we put on the message. Idempotent."""
    report = {"processed": 0, "matched": 0, "unmatched": 0, "duplicate": 0, "ignored": 0}
    if not isinstance(events, list):
        raise ValueError("'events' must be a list.")
    for ev in events[:500]:
        report["processed"] += 1
        if not isinstance(ev, dict):
            report["ignored"] += 1
            continue
        kind = _KIND_ALIASES.get(str(ev.get("event") or "").strip().lower())
        mid = str(ev.get("message_id") or "").strip().strip("<>")
        if not kind or not mid:
            report["ignored"] += 1
            continue
        row = conn.execute("SELECT * FROM email_campaign_recipients WHERE replace(replace(smtp_message_id,'<',''),'>','')=?", (mid,)).fetchone()
        if row is None:
            report["unmatched"] += 1
            continue
        when = str(ev.get("timestamp") or "")[:40] or _now(now)
        ext = str(ev.get("id") or f"{kind}:{when}")[:120]
        detail = str(ev.get("reason") or ev.get("type") or "")[:200]
        if kind in ("open", "click"):
            if not record_event(conn, row["id"], kind, "provider", detail, ext, when):
                report["duplicate"] += 1
                continue
            _touch_counters(conn, row, kind, when)
        elif not record_event(conn, row["id"], kind, "provider", detail, ext, when):
            report["duplicate"] += 1
            continue
        elif kind == "delivered":
            conn.execute("UPDATE email_campaign_recipients SET delivery_status=CASE WHEN delivery_status IN ('bounced','complained') "
                         "THEN delivery_status ELSE 'delivered' END, delivered_at=COALESCE(delivered_at, ?), updated_at=? WHERE id=?",
                         (when, when, row["id"]))
        elif kind == "bounced":
            conn.execute("UPDATE email_campaign_recipients SET delivery_status='bounced', updated_at=? WHERE id=?", (when, row["id"]))
            if str(ev.get("type") or "hard").lower() in ("hard", "permanent"):
                suppress(conn, row["email_at_send"], row["csp_code"], "bounced", row["campaign_id"], now=now)
        elif kind == "complained":
            conn.execute("UPDATE email_campaign_recipients SET delivery_status='complained', updated_at=? WHERE id=?", (when, row["id"]))
            suppress(conn, row["email_at_send"], row["csp_code"], "complaint", row["campaign_id"], now=now)
        report["matched"] += 1
    return report


def _touch_counters(conn, row, kind, when):
    col = "open" if kind == "open" else "click"
    conn.execute(f"UPDATE email_campaign_recipients SET {col}_count={col}_count+1, first_{col}_at=COALESCE(first_{col}_at, ?), updated_at=? WHERE id=?",
                 (when, when, row["id"]))
