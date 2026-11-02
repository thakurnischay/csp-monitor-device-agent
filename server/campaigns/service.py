"""Campaign management: create, preview, dry run, test send, launch / schedule, pause, cancel, retry.

Nothing here sends mail on its own. A campaign only reaches `queued` after an explicit launch (typed confirmation, live
switches on); the background worker (worker.py) then sends it in capped batches. A dry run records what WOULD be sent and
never opens a network connection.
"""
import hashlib
import json
import secrets
from datetime import datetime, timedelta, timezone

from events import record_audit

from . import eligibility, operators as ops, rendering, sender

OCCASIONS = ("Makar Sankranti", "Republic Day", "Holi", "Eid-ul-Fitr", "Baisakhi", "Akshaya Tritiya", "Eid-ul-Adha",
             "Independence Day", "Rakshabandhan", "Ganesh Chaturthi", "Navratri / Dussehra", "Dhanteras / Diwali",
             "Chhath Puja", "Christmas / New Year")
ACTIVE_STATES = ("queued", "sending")
LIVE_STATES = ("scheduled", "queued", "sending", "paused")


class CampaignError(ValueError):
    """A reason an action was refused, written for the person using the dashboard."""


def now_iso(now=None):
    return (now or datetime.now(timezone.utc)).isoformat(timespec="seconds")


def _text(value, n):
    return " ".join(str(value or "").split())[:n]


def _loads(value, default):
    try:
        return json.loads(value) if value else default
    except ValueError:
        return default


# --------------------------------------------------------------------------------------------- campaigns
def validate_fields(form):
    """Clean and validate the campaign form. Raises CampaignError / TemplateError / ConfigError with plain messages."""
    name = _text(form.get("name"), 100)
    occasion = _text(form.get("occasion"), 80)
    if not name:
        raise CampaignError("Enter a campaign name.")
    if not occasion:
        raise CampaignError("Choose or enter the festival / occasion.")
    cfg = eligibility.clean_config(form.get("devices") or [], form.get("audience_match") or "any",
                                   form.get("include_dormant") in (True, "1", "on", "true", 1))
    subject, body = rendering.validate_template(form.get("subject"), form.get("body"))
    link_url, link_label = rendering.validate_link(form.get("link_url"), form.get("link_label"))
    return {"name": name, "description": _text(form.get("description"), 400), "occasion": occasion,
            "devices_json": json.dumps(cfg["devices"]), "audience_match": cfg["audience_match"],
            "include_dormant": 1 if cfg["include_dormant"] else 0, "subject": subject, "body": body,
            "link_url": link_url, "link_label": link_label}


def get_campaign(conn, campaign_id):
    row = conn.execute("SELECT * FROM email_campaigns WHERE id=?", (campaign_id,)).fetchone()
    if not row:
        raise CampaignError("That campaign does not exist.")
    c = dict(row)
    c["devices"] = _loads(c["devices_json"], [])
    c["config"] = {"devices": c["devices"], "audience_match": c["audience_match"], "include_dormant": bool(c["include_dormant"])}
    return c


def create_campaign(conn, fields, actor, now=None):
    now = now_iso(now)
    cur = conn.execute(
        """INSERT INTO email_campaigns (name, description, occasion, devices_json, audience_match, include_dormant, subject, body,
               link_url, link_label, status, created_by, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?, 'draft', ?,?,?)""",
        (fields["name"], fields["description"], fields["occasion"], fields["devices_json"], fields["audience_match"],
         fields["include_dormant"], fields["subject"], fields["body"], fields["link_url"], fields["link_label"], actor, now, now))
    record_audit(conn, actor, "campaign_created", "", f"#{cur.lastrowid} {fields['name']} ({fields['occasion']})")
    return cur.lastrowid


def update_campaign(conn, campaign_id, fields, actor, now=None):
    c = get_campaign(conn, campaign_id)
    if c["status"] != "draft":
        raise CampaignError("Only a draft campaign can be edited. Launched campaigns are frozen so the record stays accurate.")
    conn.execute(
        """UPDATE email_campaigns SET name=?, description=?, occasion=?, devices_json=?, audience_match=?, include_dormant=?, subject=?,
               body=?, link_url=?, link_label=?, updated_at=? WHERE id=? AND status='draft'""",
        (fields["name"], fields["description"], fields["occasion"], fields["devices_json"], fields["audience_match"],
         fields["include_dormant"], fields["subject"], fields["body"], fields["link_url"], fields["link_label"], now_iso(now), campaign_id))
    record_audit(conn, actor, "campaign_edited", "", f"#{campaign_id} {fields['name']}")


# --------------------------------------------------------------------------------------------- audience
def audience(conn, campaign_id):
    """The current audience for a campaign's settings (not stored): {rows, counts, total}."""
    c = get_campaign(conn, campaign_id)
    return eligibility.build_audience(conn, eligibility.clean_config(c["devices"], c["audience_match"], c["include_dormant"]))


def materialize(conn, campaign_id, now=None):
    """Store one recipient row per operator with the decision and reason. Draft only: replaces earlier rows (no email has
    ever been created for a draft). Returns the audience."""
    c = get_campaign(conn, campaign_id)
    if c["status"] != "draft":
        raise CampaignError("The audience is frozen once a campaign is launched or scheduled.")
    aud = eligibility.build_audience(conn, eligibility.clean_config(c["devices"], c["audience_match"], c["include_dormant"]))
    now = now_iso(now)
    old = [r["id"] for r in conn.execute("SELECT id FROM email_campaign_recipients WHERE campaign_id=?", (campaign_id,)).fetchall()]
    for i in range(0, len(old), 500):
        chunk = old[i:i + 500]
        marks = ",".join("?" * len(chunk))
        conn.execute(f"DELETE FROM email_events WHERE recipient_id IN ({marks})", chunk)
        conn.execute(f"DELETE FROM email_attempts WHERE recipient_id IN ({marks})", chunk)
    conn.execute("DELETE FROM email_campaign_recipients WHERE campaign_id=?", (campaign_id,))
    for r in aud["rows"]:
        location = ", ".join(x for x in (r.get("district"), r.get("state")) if x)
        conn.execute(
            """INSERT INTO email_campaign_recipients (campaign_id, csp_code, bucket, reason, details_json, missing_devices_json,
                   review_devices_json, email_at_send, name_at_send, rm_at_send, location_at_send, status, token, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,'pending',?,?,?)""",
            (campaign_id, r["csp_code"], r["bucket"], r["reason"][:500], json.dumps(r["checks"]), json.dumps(r["missing"]),
             json.dumps(r["review"]), (r["email"] or "")[:254], r["name"], r["rm"], location, secrets.token_urlsafe(18), now, now))
    return aud


# --------------------------------------------------------------------------------- preview, dry run, test
def _sample_context(c, conn, csp_code=None):
    """(context, label) for a preview: a chosen operator, else the first eligible one, else a clearly fake sample."""
    cfg = eligibility.clean_config(c["devices"], c["audience_match"], c["include_dormant"])
    suppressed = {r["email"] for r in conn.execute("SELECT email FROM email_suppressions").fetchall()}
    dev = ops.device_map(conn)
    if csp_code:
        op = conn.execute("SELECT * FROM campaign_operators WHERE csp_code=?", (ops.normalize_code(csp_code),)).fetchone()
        if not op:
            raise CampaignError("That operator code is not in the operator data.")
        d = eligibility.evaluate(op, dev.get(op["csp_code"], {}), cfg, suppressed, {})
        labels = [ops.SENTENCE_LABELS[x] for x in d["missing"]]
        return rendering.context_for(c, op["name"], op["csp_code"], op["rm"], labels), f"{op['csp_code']} ({d['bucket']})"
    claimed = {}
    for op in conn.execute("SELECT * FROM campaign_operators WHERE present_in_latest=1 ORDER BY csp_code").fetchall():
        d = eligibility.evaluate(op, dev.get(op["csp_code"], {}), cfg, suppressed, claimed)
        if d["bucket"] == eligibility.ELIGIBLE:
            labels = [ops.SENTENCE_LABELS[x] for x in d["missing"]]
            return rendering.context_for(c, op["name"], op["csp_code"], op["rm"], labels), f"{op['csp_code']} (first eligible operator)"
    labels = [ops.SENTENCE_LABELS[x] for x in c["devices"]]
    return rendering.context_for(c, "Sample Operator", "SAMPLE-001", "Sample RM", labels), "a made-up sample (no eligible operator yet)"


def preview(conn, campaign_id, csp_code=None):
    c = get_campaign(conn, campaign_id)
    ctx, label = _sample_context(c, conn, csp_code)
    out = rendering.render(c, ctx, "PREVIEW", sender.public_base_url() or "https://your-dashboard.example", tracking=False)
    return dict(out, sample=label, mode=sender.mode())


def dry_run(conn, campaign_id, actor, now=None):
    """Compose every message that WOULD be sent. No connection is opened, no recipient changes state, nothing is delivered.
    Each composed message is recorded as a 'dry' attempt so the rehearsal is visible; dry attempts never count as sent."""
    c = get_campaign(conn, campaign_id)
    if c["status"] != "draft":
        raise CampaignError("A dry run is for draft campaigns (a launched campaign is already frozen).")
    aud = materialize(conn, campaign_id, now)
    problems, samples, n = [], [], 0
    base = sender.public_base_url() or "https://your-dashboard.example"
    for r in conn.execute("SELECT * FROM email_campaign_recipients WHERE campaign_id=? AND bucket='eligible' ORDER BY id", (campaign_id,)).fetchall():
        try:
            labels = [ops.SENTENCE_LABELS[d] for d in _loads(r["missing_devices_json"], [])]
            out = rendering.render(c, rendering.context_for(c, r["name_at_send"], r["csp_code"], r["rm_at_send"], labels), r["token"], base)
        except Exception as err:                                         # noqa: BLE001 - report, never crash
            problems.append({"csp_code": r["csp_code"], "problem": f"{type(err).__name__}: {err}"})
            continue
        conn.execute("INSERT INTO email_attempts (recipient_id, attempt_no, mode, started_at, finished_at, outcome) VALUES (?,?,?,?,?, 'dry_run')",
                     (r["id"], 1, "dry", now_iso(now), now_iso(now)))
        n += 1
        if len(samples) < 3:
            samples.append({"csp_code": r["csp_code"], "subject": out["subject"]})
    result = {"would_send": n, "counts": aud["counts"], "problems": problems, "samples": samples,
              "at": now_iso(now), "note": "Dry run: nothing was sent and no connection to a mail server was opened."}
    conn.execute("UPDATE email_campaigns SET outcome_json=?, updated_at=? WHERE id=?",
                 (json.dumps({"last_dry_run": result}), now_iso(now), campaign_id))
    record_audit(conn, actor, "campaign_dry_run", "", f"#{campaign_id}: would send {n}, review {aud['counts']['review']}, excluded {aud['counts']['excluded']}")
    return result


def send_test(conn, campaign_id, to_address, actor, transport=None, now=None):
    """One test message to an address the person types. Not a recipient: it never counts in the history or totals."""
    from .growth import emailer as ge
    c = get_campaign(conn, campaign_id)
    to_address = " ".join(str(to_address or "").split())
    if not ge.is_valid_email(to_address) or len(to_address) > 254 or "," in to_address or ";" in to_address:
        raise CampaignError("Enter one valid email address for the test.")
    ctx, label = _sample_context(c, conn)
    out = rendering.render(c, ctx, "TEST", sender.public_base_url() or "https://your-dashboard.example", tracking=False)
    out["subject"] = "[TEST] " + out["subject"]
    if sender.mode() != "live":
        record_audit(conn, actor, "campaign_test_not_sent", "", f"#{campaign_id}: dry mode, nothing sent")
        return {"sent": False, "mode": "dry", "note": "Dry mode: nothing was sent. The message below is what would be sent.", "preview": out}
    blockers = sender.live_blockers()
    if blockers:
        raise CampaignError("A test email cannot be sent yet: " + " ".join(blockers))
    transport = transport or sender.get_transport()
    try:
        transport.open()
        message_id = transport.send(to_address, out["subject"], out["html"], out["text"], {})
    except Exception as err:                                               # noqa: BLE001
        raise CampaignError("The test email could not be sent (" + sender.error_text(err) + ").")
    finally:
        try:
            transport.close()
        except Exception:                                                  # noqa: BLE001
            pass
    record_audit(conn, actor, "campaign_test_sent", "", f"#{campaign_id} to {_mask(to_address)}")
    return {"sent": True, "mode": "live", "message_id": message_id, "sample": label}


def _mask(address):
    local, _, domain = str(address).partition("@")
    return (local[:1] + "***@" + domain) if domain else "***"


# -------------------------------------------------------------------------------------------- launch & control
def launch(conn, campaign_id, actor, confirm_text, schedule_at=None, now=None):
    """Explicit, confirmed, live. Freezes the audience, records the exact configuration, and queues (or schedules) it."""
    now_dt = now or datetime.now(timezone.utc)
    c = get_campaign(conn, campaign_id)
    if c["status"] != "draft":
        raise CampaignError("This campaign has already been launched, scheduled or cancelled.")
    blockers = sender.live_blockers()
    if blockers:
        raise CampaignError("Live sending is not switched on yet. " + " ".join(blockers) + " Use a dry run until it is.")
    when = None
    if schedule_at:
        try:
            when = datetime.fromisoformat(str(schedule_at).replace("Z", "+00:00"))
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            when = when.astimezone(timezone.utc)
        except ValueError:
            raise CampaignError("The scheduled time is not valid.")
        if when < now_dt + timedelta(minutes=1) or when > now_dt + timedelta(days=366):
            raise CampaignError("Choose a time in the future (within a year).")
    aud = materialize(conn, campaign_id, now_dt)
    n = aud["counts"][eligibility.ELIGIBLE]
    if n == 0:
        raise CampaignError("There are no eligible recipients, so there is nothing to send.")
    if str(confirm_text or "").strip() != str(n):
        raise CampaignError(f"To confirm, type the number of recipients ({n}) exactly as shown.")
    status = "scheduled" if when else "queued"
    stamp = now_iso(now_dt)
    snapshot = {"config": c["config"], "counts": aud["counts"], "subject": c["subject"], "link_url": c["link_url"],
                "body_sha256": hashlib.sha256(c["body"].encode()).hexdigest(), "from": sender.settings_summary()["from"],
                "max_per_run": sender.max_per_run(), "scheduled_at": when.isoformat(timespec="seconds") if when else None,
                "confirmed_recipients": n}
    cur = conn.execute(
        """UPDATE email_campaigns SET status=?, mode='live', scheduled_at=?, launched_by=?, launched_at=?, config_snapshot_json=?, updated_at=?
           WHERE id=? AND status='draft'""",
        (status, when.isoformat(timespec="seconds") if when else None, actor, stamp, json.dumps(snapshot), stamp, campaign_id))
    if cur.rowcount != 1:                                                  # double submit: the first one already won
        raise CampaignError("This campaign has already been launched.")
    conn.execute("UPDATE email_campaign_recipients SET status='queued', updated_at=? WHERE campaign_id=? AND bucket='eligible'", (stamp, campaign_id))
    conn.execute("UPDATE email_campaign_recipients SET status='not_sent', updated_at=? WHERE campaign_id=? AND bucket<>'eligible'", (stamp, campaign_id))
    record_audit(conn, actor, "campaign_launched" if not when else "campaign_scheduled", "",
                 f"#{campaign_id} {c['name']}: {n} recipients, {c['audience_match']} of {','.join(c['devices'])}"
                 + (f", at {snapshot['scheduled_at']}" if when else ""))
    return {"status": status, "recipients": n, "scheduled_at": snapshot["scheduled_at"]}


def pause(conn, campaign_id, actor):
    c = get_campaign(conn, campaign_id)
    if c["status"] not in ("scheduled", "queued", "sending"):
        raise CampaignError("Only a scheduled or running campaign can be paused.")
    conn.execute("UPDATE email_campaigns SET status='paused', updated_at=? WHERE id=?", (now_iso(), campaign_id))
    record_audit(conn, actor, "campaign_paused", "", f"#{campaign_id}")


def resume(conn, campaign_id, actor):
    c = get_campaign(conn, campaign_id)
    if c["status"] != "paused":
        raise CampaignError("Only a paused campaign can be resumed.")
    blockers = sender.live_blockers() if c["mode"] == "live" else []
    if blockers:
        raise CampaignError("Live sending is not switched on: " + " ".join(blockers))
    started = conn.execute("SELECT COUNT(*) c FROM email_campaign_recipients WHERE campaign_id=? AND status<>'queued' AND status<>'not_sent'",
                           (campaign_id,)).fetchone()["c"]
    new = "scheduled" if (c["scheduled_at"] and not started and c["scheduled_at"] > now_iso()) else "queued"
    conn.execute("UPDATE email_campaigns SET status=?, updated_at=? WHERE id=?", (new, now_iso(), campaign_id))
    record_audit(conn, actor, "campaign_resumed", "", f"#{campaign_id}")


def cancel(conn, campaign_id, actor):
    c = get_campaign(conn, campaign_id)
    if c["status"] in ("completed", "cancelled"):
        raise CampaignError("This campaign is already finished.")
    n = conn.execute("UPDATE email_campaign_recipients SET status='cancelled', updated_at=? WHERE campaign_id=? AND status='queued'",
                     (now_iso(), campaign_id)).rowcount
    conn.execute("UPDATE email_campaigns SET status='cancelled', cancelled_at=?, updated_at=? WHERE id=?", (now_iso(), now_iso(), campaign_id))
    record_audit(conn, actor, "campaign_cancelled", "", f"#{campaign_id}: {n} unsent message(s) cancelled")
    return n


def retry_failed(conn, campaign_id, actor, include_unknown=False):
    """Put failed messages back in the queue. NEVER touches a message the mail server already accepted, and never retries a
    permanent failure. 'unknown outcome' (the server crashed mid-send, so the message MAY have gone) needs include_unknown."""
    c = get_campaign(conn, campaign_id)
    if c["status"] not in ("queued", "sending", "paused", "completed"):
        raise CampaignError("Retry is for campaigns that have been launched.")
    now = now_iso()
    n = conn.execute("UPDATE email_campaign_recipients SET status='queued', attempts=0, next_attempt_at=NULL, failure_kind=NULL, updated_at=? "
                     "WHERE campaign_id=? AND status='failed' AND failure_kind='transient_exhausted'", (now, campaign_id)).rowcount
    u = 0
    if include_unknown:
        u = conn.execute("UPDATE email_campaign_recipients SET status='queued', attempts=0, next_attempt_at=NULL, updated_at=? "
                         "WHERE campaign_id=? AND status='unknown_outcome'", (now, campaign_id)).rowcount
    if n + u and c["status"] == "completed":
        conn.execute("UPDATE email_campaigns SET status='queued', completed_at=NULL, updated_at=? WHERE id=?", (now, campaign_id))
    record_audit(conn, actor, "campaign_retry", "", f"#{campaign_id}: {n} failed + {u} unknown-outcome message(s) re-queued")
    return {"requeued": n, "requeued_unknown": u}
