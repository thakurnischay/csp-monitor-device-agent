"""Sending launched campaigns safely.

Guarantees (each one is covered by a test):
  * One message per operator per campaign: UNIQUE(campaign_id, csp_code) in the database.
  * A message is CLAIMED (queued -> sending) with a single atomic UPDATE before any network call, and committed, so two
    workers (or a restart mid-batch) can never both send it.
  * A message the mail server ACCEPTED is never sent again. Retry only touches failures.
  * If the process dies between claiming and recording, the message is 'unknown_outcome' after restart: it is NOT sent
    again automatically (it may have gone out). A person decides.
  * Transient failures (timeouts, 4xx) are retried with a delay up to CAMPAIGN_MAX_ATTEMPTS, then recorded as failed;
    permanent failures (5xx, refused recipient) are recorded at once. Every attempt is its own row: history is never erased.
  * A batch sends at most EMAIL_MAX_PER_RUN messages, with CAMPAIGN_SEND_DELAY_SECONDS between them.
  * Live switches are re-checked on every batch; an opt-out is re-checked per message right before sending.
  * Problems that affect every message (bad login, sender refused) pause the campaign instead of burning attempts.
"""
import json
import logging
import threading
import time
from datetime import datetime, timedelta, timezone

from events import record_audit

from . import operators as ops, rendering, sender, tracking
from .service import get_campaign, now_iso

log = logging.getLogger("csp_monitor.campaigns")


def _loads(value, default):
    try:
        return json.loads(value) if value else default
    except ValueError:
        return default


def recover_after_restart(conn):
    """Run once at start-up. Messages stuck 'sending' may or may not have left: mark them for a human, never auto-resend."""
    n = conn.execute("UPDATE email_campaign_recipients SET status='unknown_outcome', updated_at=?, "
                     "last_error='the server stopped while this message was being sent; it may or may not have been delivered' "
                     "WHERE status='sending'", (now_iso(),)).rowcount
    conn.execute("UPDATE email_campaigns SET status='queued', updated_at=? WHERE status='sending'", (now_iso(),))
    return n


def _finish_if_done(conn, campaign_id):
    left = conn.execute("SELECT COUNT(*) c FROM email_campaign_recipients WHERE campaign_id=? AND status IN ('queued','sending')",
                        (campaign_id,)).fetchone()["c"]
    if left:
        return False
    counts = {r["status"]: r["n"] for r in conn.execute(
        "SELECT status, COUNT(*) n FROM email_campaign_recipients WHERE campaign_id=? GROUP BY status", (campaign_id,)).fetchall()}
    conn.execute("UPDATE email_campaigns SET status='completed', completed_at=?, outcome_json=?, updated_at=? WHERE id=? AND status IN ('queued','sending')",
                 (now_iso(), json.dumps({"final_counts": counts}), now_iso(), campaign_id))
    record_audit(conn, "worker", "campaign_completed", "", f"#{campaign_id}: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    return True


def run_batch(conn, campaign_id, transport=None, limit=None, now=None, sleep=time.sleep):
    """Send up to `limit` (default EMAIL_MAX_PER_RUN) queued messages. `conn` commits as it goes. Returns a report."""
    now_dt = now or datetime.now(timezone.utc)
    limit = limit or sender.max_per_run()
    report = {"sent": 0, "failed": 0, "retry_later": 0, "skipped_opted_out": 0, "paused": None}
    c = get_campaign(conn, campaign_id)
    if c["status"] not in ("queued", "sending"):
        return dict(report, skipped=f"campaign is {c['status']}")
    if c["mode"] == "live" and not sender.live_enabled():
        conn.execute("UPDATE email_campaigns SET status='paused', updated_at=? WHERE id=? AND status IN ('queued','sending')", (now_iso(now_dt), campaign_id))
        record_audit(conn, "worker", "campaign_paused", "", f"#{campaign_id}: live sending is switched off on the server")
        conn.commit()
        return dict(report, paused="live sending is switched off on the server")
    conn.execute("UPDATE email_campaigns SET status='sending', updated_at=? WHERE id=? AND status='queued'", (now_iso(now_dt), campaign_id))
    conn.commit()
    due = conn.execute("SELECT id FROM email_campaign_recipients WHERE campaign_id=? AND status='queued' AND (next_attempt_at IS NULL OR next_attempt_at<=?) "
                       "ORDER BY id LIMIT ?", (campaign_id, now_iso(now_dt), limit)).fetchall()
    base = sender.public_base_url()
    transport = transport or sender.get_transport()
    opened = False
    try:
        for item in due:
            row = conn.execute("SELECT status FROM email_campaigns WHERE id=?", (campaign_id,)).fetchone()
            if row["status"] not in ("queued", "sending"):                     # paused / cancelled while we were working
                break
            # 1. claim it: one atomic UPDATE, committed BEFORE any network call
            if conn.execute("UPDATE email_campaign_recipients SET status='sending', attempts=attempts+1, updated_at=? WHERE id=? AND status='queued'",
                            (now_iso(now_dt), item["id"])).rowcount != 1:
                continue
            conn.commit()
            r = conn.execute("SELECT * FROM email_campaign_recipients WHERE id=?", (item["id"],)).fetchone()
            attempt_no = r["attempts"]
            # 2. last-moment checks
            if tracking.is_suppressed(conn, r["email_at_send"]):
                conn.execute("UPDATE email_campaign_recipients SET status='cancelled', attempts=attempts-1, last_error='opted out', updated_at=? WHERE id=?",
                             (now_iso(now_dt), r["id"]))
                conn.commit()
                report["skipped_opted_out"] += 1
                continue
            labels = [ops.SENTENCE_LABELS[d] for d in _loads(r["missing_devices_json"], [])]
            message = rendering.render(c, rendering.context_for(c, r["name_at_send"], r["csp_code"], r["rm_at_send"], labels), r["token"], base)
            started = now_iso(now_dt)
            try:
                if not opened:
                    transport.open()
                    opened = True
                message_id = transport.send(r["email_at_send"], message["subject"], message["html"], message["text"], message["headers"])
            except Exception as err:                                           # noqa: BLE001 - classified below
                try:
                    kind = sender.classify(err)
                except sender.BatchAbort as stop:
                    conn.execute("UPDATE email_campaign_recipients SET status='queued', attempts=attempts-1, updated_at=? WHERE id=?", (now_iso(now_dt), r["id"]))
                    conn.execute("UPDATE email_campaigns SET status='paused', updated_at=? WHERE id=?", (now_iso(now_dt), campaign_id))
                    record_audit(conn, "worker", "campaign_paused", "", f"#{campaign_id}: {stop}")
                    conn.commit()
                    report["paused"] = str(stop)
                    break
                text = sender.error_text(err)
                conn.execute("INSERT INTO email_attempts (recipient_id, attempt_no, mode, started_at, finished_at, outcome, error) VALUES (?,?,?,?,?,?,?)",
                             (r["id"], attempt_no, "live", started, now_iso(now_dt),
                              "permanent_failure" if kind == "permanent" else "transient_failure", text))
                if kind == "permanent" or attempt_no >= sender.max_attempts():
                    conn.execute("UPDATE email_campaign_recipients SET status='failed', failure_kind=?, last_error=?, updated_at=? WHERE id=?",
                                 ("permanent" if kind == "permanent" else "transient_exhausted", text, now_iso(now_dt), r["id"]))
                    report["failed"] += 1
                else:
                    retry_at = (now_dt + timedelta(minutes=sender.retry_backoff_minutes() * attempt_no)).isoformat(timespec="seconds")
                    conn.execute("UPDATE email_campaign_recipients SET status='queued', next_attempt_at=?, last_error=?, updated_at=? WHERE id=?",
                                 (retry_at, text, now_iso(now_dt), r["id"]))
                    report["retry_later"] += 1
                conn.commit()
                if opened and kind == "transient":                              # the connection may be dead: reconnect for the next one
                    transport.close()
                    opened = False
                continue
            # 3. record success (the server ACCEPTED it - that is all this proves)
            done = now_iso(now_dt)
            conn.execute("UPDATE email_campaign_recipients SET status='accepted', accepted_at=?, smtp_message_id=?, subject_sent=?, body_html=?, body_text=?, "
                         "last_error=NULL, next_attempt_at=NULL, updated_at=? WHERE id=?",
                         (done, message_id, message["subject"], message["html"], message["text"], done, r["id"]))
            conn.execute("INSERT INTO email_attempts (recipient_id, attempt_no, mode, started_at, finished_at, outcome, smtp_message_id) VALUES (?,?,?,?,?, 'accepted', ?)",
                         (r["id"], attempt_no, "live", started, done, message_id))
            tracking.record_event(conn, r["id"], "accepted", "smtp", "accepted by the mail server", when=done)
            conn.commit()
            report["sent"] += 1
            if sender.send_delay():
                sleep(sender.send_delay())
    finally:
        try:
            transport.close()
        except Exception:                                                       # noqa: BLE001
            pass
    _finish_if_done(conn, campaign_id)
    conn.commit()
    return report


def tick(conn_factory, transport_factory=None, now=None, sleep=time.sleep):
    """One pass of the background loop: start scheduled campaigns that are due, then send one capped batch of each running one.
    Safe to call as often as you like and from several processes: every state change is an atomic UPDATE."""
    now_dt = now or datetime.now(timezone.utc)
    reports = {}
    conn = conn_factory()
    try:
        for row in conn.execute("SELECT id FROM email_campaigns WHERE status='scheduled' AND scheduled_at<=?", (now_iso(now_dt),)).fetchall():
            if conn.execute("UPDATE email_campaigns SET status='queued', updated_at=? WHERE id=? AND status='scheduled'",
                            (now_iso(now_dt), row["id"])).rowcount == 1:
                record_audit(conn, "scheduler", "campaign_started_on_schedule", "", f"#{row['id']}")
            conn.commit()
        for row in conn.execute("SELECT id FROM email_campaigns WHERE status IN ('queued','sending') ORDER BY id").fetchall():
            transport = transport_factory() if transport_factory else None
            reports[row["id"]] = run_batch(conn, row["id"], transport, now=now_dt, sleep=sleep)
    finally:
        conn.close()
    return reports


# ------------------------------------------------------------------------------------ the background thread
_started = False


def start(conn_factory, interval_seconds=60):
    """Opt-in background sender (CSP_MONITOR_CAMPAIGN_WORKER=1). Idempotent. Recovers from a restart first."""
    global _started
    if _started:
        return False
    _started = True
    conn = conn_factory()
    try:
        n = recover_after_restart(conn)
        conn.commit()
        if n:
            log.warning("%d campaign message(s) were mid-send when the server stopped; marked 'unknown outcome' (not resent)", n)
    finally:
        conn.close()

    def loop():
        while True:
            try:
                tick(conn_factory)
            except Exception:                                                   # noqa: BLE001 - never let the loop die
                log.exception("campaign worker pass failed")
            time.sleep(interval_seconds)

    threading.Thread(target=loop, name="campaign-worker", daemon=True).start()
    return True
