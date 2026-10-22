"""Read side of Email Campaigns: summary cards, the filterable / paginated message history, message detail, progress.

Every number comes from stored rows. Four DIFFERENT facts are kept apart and never merged:
    accepted   the mail server took the message          delivered  a provider CONFIRMED delivery (needs provider events)
    opened     an image was requested (a signal only)    clicked    a tracked link was requested
"""
import json

from . import eligibility, operators as ops, sender
from .tracking import mask_email

HISTORY_STATUSES = ("queued", "sending", "accepted", "failed", "unknown_outcome", "cancelled")
PER_PAGE_DEFAULT, PER_PAGE_MAX = 25, 100
STATUS_FILTERS = {
    "queued": "Queued", "sending": "Sending", "accepted": "Accepted by mail server", "delivered": "Delivered (confirmed)",
    "bounced": "Bounced", "complained": "Spam complaint", "failed": "Failed", "unknown_outcome": "Needs review (may have been sent)",
    "cancelled": "Cancelled", "opened": "Opened (signal)", "clicked": "Clicked (signal)"}


def _loads(value, default):
    try:
        return json.loads(value) if value else default
    except ValueError:
        return default


def display_status(r):
    s, d = r["status"], r["delivery_status"]
    if s == "accepted":
        return {"delivered": "Delivered (confirmed)", "bounced": "Bounced", "complained": "Spam complaint"}.get(d, "Accepted by mail server")
    return {"queued": "Queued", "sending": "Sending", "failed": "Failed", "unknown_outcome": "Needs review (may have been sent)",
            "cancelled": "Cancelled", "pending": "Not sent yet", "not_sent": "Not sent"}.get(s, s)


def ownership_text(r):
    missing = [ops.DEVICE_LABELS[d] for d in _loads(r["missing_devices_json"], []) if d in ops.DEVICE_LABELS]
    review = [ops.DEVICE_LABELS[d] for d in _loads(r["review_devices_json"], []) if d in ops.DEVICE_LABELS]
    text = ("Confirmed without: " + ", ".join(missing)) if missing else "-"
    if review:
        text += " | Not confirmed: " + ", ".join(review)
    return text


# --------------------------------------------------------------------------------------------------- summary
def summary(conn):
    cfg = eligibility.clean_config(ops.DEVICES, "any", False)
    aud = eligibility.build_audience(conn, cfg)
    one = lambda sql, *a: conn.execute(sql, a).fetchone()[0]
    hist = ",".join(f"'{s}'" for s in HISTORY_STATUSES)
    return {
        "eligible_operators": aud["counts"][eligibility.ELIGIBLE], "review_operators": aud["counts"][eligibility.REVIEW],
        "operators_total": aud["total"],
        "emails_accepted": one("SELECT COUNT(*) FROM email_campaign_recipients WHERE status='accepted'"),
        "delivered_confirmed": one("SELECT COUNT(*) FROM email_campaign_recipients WHERE delivery_status='delivered'"),
        "bounced": one("SELECT COUNT(*) FROM email_campaign_recipients WHERE delivery_status IN ('bounced','complained')"),
        "failed": one("SELECT COUNT(*) FROM email_campaign_recipients WHERE status='failed'"),
        "needs_review": one("SELECT COUNT(*) FROM email_campaign_recipients WHERE status='unknown_outcome'"),
        "pending_queued": one("SELECT COUNT(*) FROM email_campaign_recipients WHERE status IN ('queued','sending')"),
        "opened": one("SELECT COUNT(*) FROM email_campaign_recipients WHERE open_count>0"),
        "clicked": one("SELECT COUNT(*) FROM email_campaign_recipients WHERE click_count>0"),
        "campaigns": one("SELECT COUNT(*) FROM email_campaigns"),
        "campaigns_launched": one("SELECT COUNT(*) FROM email_campaigns WHERE status<>'draft'"),
        "unique_operators_contacted": one("SELECT COUNT(DISTINCT csp_code) FROM email_campaign_recipients WHERE status='accepted'"),
        "suppressed_addresses": one("SELECT COUNT(*) FROM email_suppressions"),
        "operators_imported": one("SELECT COUNT(*) FROM campaign_operators"),
        "delivery_confirmation_available": bool(sender.events_key()) or one("SELECT COUNT(*) FROM email_events WHERE source='provider'") > 0,
        "history_rows": one(f"SELECT COUNT(*) FROM email_campaign_recipients WHERE status IN ({hist})"),
    }


# -------------------------------------------------------------------------------------------- the history
def _where(f):
    cond = [f"r.status IN ({','.join('?' * len(HISTORY_STATUSES))})"]
    args = list(HISTORY_STATUSES)
    q = str(f.get("q") or "").strip().lower()
    if q:
        cond.append("(lower(r.name_at_send) LIKE ? OR lower(r.csp_code) LIKE ? OR lower(r.email_at_send) LIKE ?)")
        args += [f"%{q}%"] * 3
    if f.get("occasion"):
        cond.append("c.occasion=?")
        args.append(f["occasion"])
    if str(f.get("campaign_id") or "").isdigit():
        cond.append("r.campaign_id=?")
        args.append(int(f["campaign_id"]))
    dev = str(f.get("device") or "").upper()
    if dev in ops.DEVICES:
        cond.append("r.missing_devices_json LIKE ?")
        args.append(f'%"{dev}"%')
    st = str(f.get("status") or "")
    if st == "delivered":
        cond.append("r.delivery_status='delivered'")
    elif st in ("bounced", "complained"):
        cond.append("r.delivery_status=?")
        args.append(st)
    elif st == "accepted":
        cond.append("r.status='accepted' AND r.delivery_status='not_confirmed'")
    elif st == "opened":
        cond.append("r.open_count>0")
    elif st == "clicked":
        cond.append("r.click_count>0")
    elif st in ("queued", "sending", "failed", "unknown_outcome", "cancelled"):
        cond.append("r.status=?")
        args.append(st)
    when = "substr(COALESCE(r.accepted_at, r.updated_at),1,10)"
    if f.get("date_from"):
        cond.append(f"{when}>=?")
        args.append(str(f["date_from"])[:10])
    if f.get("date_to"):
        cond.append(f"{when}<=?")
        args.append(str(f["date_to"])[:10])
    if f.get("location"):
        cond.append("lower(r.location_at_send) LIKE ?")
        args.append(f"%{str(f['location']).strip().lower()}%")
    if f.get("rm"):
        cond.append("lower(r.rm_at_send) LIKE ?")
        args.append(f"%{str(f['rm']).strip().lower()}%")
    return " AND ".join(cond), args


def list_messages(conn, filters=None, page=1, per_page=PER_PAGE_DEFAULT):
    """Server-side pagination: {rows, total, page, pages, per_page}. Emails are masked here; the detail view shows the full address."""
    filters = filters or {}
    try:
        page, per_page = max(1, int(page)), max(1, min(int(per_page), PER_PAGE_MAX))
    except (TypeError, ValueError):
        page, per_page = 1, PER_PAGE_DEFAULT
    where, args = _where(filters)
    base = "FROM email_campaign_recipients r JOIN email_campaigns c ON c.id=r.campaign_id WHERE " + where
    total = conn.execute("SELECT COUNT(*) " + base, args).fetchone()[0]
    pages = max(1, -(-total // per_page))
    page = min(page, pages)
    rows = conn.execute(
        "SELECT r.*, c.name campaign_name, c.occasion " + base +
        " ORDER BY COALESCE(r.accepted_at, r.updated_at) DESC, r.id DESC LIMIT ? OFFSET ?", args + [per_page, (page - 1) * per_page]).fetchall()
    out = []
    for r in rows:
        out.append({"id": r["id"], "csp_code": r["csp_code"], "name": r["name_at_send"], "email_masked": mask_email(r["email_at_send"]),
                    "location": r["location_at_send"], "rm": r["rm_at_send"], "ownership": ownership_text(r),
                    "occasion": r["occasion"], "campaign_id": r["campaign_id"], "campaign_name": r["campaign_name"],
                    "subject": r["subject_sent"], "sent_at": r["accepted_at"], "status": display_status(r),
                    "status_key": r["status"], "delivery_status": r["delivery_status"],
                    "opened": (f"Opened {r['open_count']}x (signal)" if r["open_count"] else "No open recorded"),
                    "clicked": (f"Clicked {r['click_count']}x" if r["click_count"] else "No click recorded"),
                    "failure_reason": r["last_error"] if r["status"] in ("failed", "unknown_outcome", "cancelled") else "",
                    "updated_at": r["updated_at"], "attempts": r["attempts"]})
    return {"rows": out, "total": total, "page": page, "pages": pages, "per_page": per_page}


def message_detail(conn, recipient_id):
    r = conn.execute("SELECT r.*, c.name campaign_name, c.occasion, c.subject campaign_subject, c.mode campaign_mode, c.launched_by, "
                     "c.launched_at FROM email_campaign_recipients r JOIN email_campaigns c ON c.id=r.campaign_id WHERE r.id=?",
                     (recipient_id,)).fetchone()
    if not r:
        return None
    attempts = [dict(a) for a in conn.execute("SELECT * FROM email_attempts WHERE recipient_id=? ORDER BY id", (recipient_id,)).fetchall()]
    events = [dict(e) for e in conn.execute("SELECT * FROM email_events WHERE recipient_id=? ORDER BY occurred_at, id", (recipient_id,)).fetchall()]
    return {"id": r["id"], "csp_code": r["csp_code"], "name": r["name_at_send"], "email": r["email_at_send"], "rm": r["rm_at_send"],
            "location": r["location_at_send"], "campaign_id": r["campaign_id"], "campaign_name": r["campaign_name"],
            "occasion": r["occasion"], "mode": r["campaign_mode"], "launched_by": r["launched_by"], "launched_at": r["launched_at"],
            "status": display_status(r), "status_key": r["status"], "delivery_status": r["delivery_status"],
            "reason": r["reason"], "checks": _loads(r["details_json"], []), "ownership": ownership_text(r),
            "subject": r["subject_sent"] or r["campaign_subject"], "body_html": r["body_html"], "body_text": r["body_text"],
            "smtp_message_id": r["smtp_message_id"], "accepted_at": r["accepted_at"], "delivered_at": r["delivered_at"],
            "first_open_at": r["first_open_at"], "open_count": r["open_count"], "first_click_at": r["first_click_at"],
            "click_count": r["click_count"], "last_error": r["last_error"], "failure_kind": r["failure_kind"],
            "attempts": attempts, "events": events, "updated_at": r["updated_at"]}


def progress(conn, campaign_id):
    counts = {r["status"]: r["n"] for r in conn.execute(
        "SELECT status, COUNT(*) n FROM email_campaign_recipients WHERE campaign_id=? GROUP BY status", (campaign_id,)).fetchall()}
    sendable = sum(counts.get(k, 0) for k in ("queued", "sending", "accepted", "failed", "unknown_outcome", "cancelled"))
    done = sum(counts.get(k, 0) for k in ("accepted", "failed", "unknown_outcome", "cancelled"))
    one = lambda sql: conn.execute(sql, (campaign_id,)).fetchone()[0]
    return {"counts": counts, "sendable": sendable, "done": done, "percent": round(100 * done / sendable) if sendable else 0,
            "delivered_confirmed": one("SELECT COUNT(*) FROM email_campaign_recipients WHERE campaign_id=? AND delivery_status='delivered'"),
            "bounced": one("SELECT COUNT(*) FROM email_campaign_recipients WHERE campaign_id=? AND delivery_status IN ('bounced','complained')"),
            "opened": one("SELECT COUNT(*) FROM email_campaign_recipients WHERE campaign_id=? AND open_count>0"),
            "clicked": one("SELECT COUNT(*) FROM email_campaign_recipients WHERE campaign_id=? AND click_count>0")}


def list_campaigns(conn):
    out = []
    for c in conn.execute("SELECT * FROM email_campaigns ORDER BY id DESC").fetchall():
        p = progress(conn, c["id"])
        out.append(dict(c, devices=_loads(c["devices_json"], []), recipients=p["sendable"], accepted=p["counts"].get("accepted", 0),
                        failed=p["counts"].get("failed", 0), queued=p["counts"].get("queued", 0) + p["counts"].get("sending", 0),
                        percent=p["percent"]))
    return out


def filter_options(conn):
    return {"occasions": [r[0] for r in conn.execute("SELECT DISTINCT occasion FROM email_campaigns ORDER BY occasion").fetchall()],
            "campaigns": [{"id": r["id"], "name": r["name"]} for r in conn.execute("SELECT id, name FROM email_campaigns WHERE status<>'draft' ORDER BY id DESC").fetchall()],
            "locations": [r[0] for r in conn.execute("SELECT DISTINCT state FROM campaign_operators WHERE state<>'' ORDER BY state").fetchall()],
            "statuses": STATUS_FILTERS}
