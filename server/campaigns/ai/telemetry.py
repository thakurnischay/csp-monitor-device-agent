"""Telemetry for the AI drafting assistant.

  * `record_generation`  writes ONE immutable row holding the whole chain: prompt + injected context chunks + model output
    (+ usage, latency, request id). Synchronous, because the page needs the generation's id.
  * `log_feedback`       records what the person then did (saved with edits, regenerated, previewed, launched ...), with the
    Levenshtein distance between the AI's text and theirs. It is QUEUED and written by a background thread, so telemetry can
    never slow down or break the page. The edit distance itself is computed in that thread.
  * If the queue is full or the writer is not running, the write happens inline instead of being dropped.

`CSP_MONITOR_TELEMETRY_SYNC=1` makes everything inline (used by the tests); `drain()` waits for the queue to empty.
"""
import json
import logging
import os
import queue
import threading
import time
from datetime import datetime, timezone

from db import get_connection

from . import textdiff

log = logging.getLogger("csp_monitor.ai_telemetry")
_queue = queue.Queue(maxsize=10000)
_thread = None
_lock = threading.Lock()

EVENTS = ("draft_saved", "regenerated", "previewed", "dry_run", "test_sent", "launched")


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _sync_mode():
    return os.environ.get("CSP_MONITOR_TELEMETRY_SYNC") == "1"


def _run(fn):
    try:
        conn = get_connection()
        try:
            fn(conn)
            conn.commit()
        finally:
            conn.close()
    except Exception:                                           # noqa: BLE001 - telemetry must never break the app
        log.exception("telemetry write failed")


def _worker(q):
    while True:
        fn = q.get()
        try:
            _run(fn)
        finally:
            q.task_done()


def submit(fn):
    """Run `fn(conn)` off the request path. Returns how it was handled: 'sync', 'queued' or 'inline-overflow'."""
    global _thread
    if _sync_mode():
        _run(fn)
        return "sync"
    with _lock:
        if _thread is None or not _thread.is_alive():
            _thread = threading.Thread(target=_worker, args=(_queue,), name="ai-telemetry", daemon=True)
            _thread.start()
    try:
        _queue.put_nowait(fn)
        return "queued"
    except queue.Full:
        _run(fn)
        return "inline-overflow"


def drain(timeout=10.0):
    """Wait until everything queued has been written. -> True if the queue emptied in time."""
    end = time.time() + timeout
    while _queue.unfinished_tasks and time.time() < end:
        time.sleep(0.01)
    return not _queue.unfinished_tasks


# -------------------------------------------------------------------------------------------- generations
def record_generation(conn, g):
    """Insert one generation (an atomic block). `g` keys match the ai_generations columns; JSON fields may be given as objects."""
    def j(v):
        return v if isinstance(v, str) or v is None else json.dumps(v)
    cur = conn.execute(
        """INSERT INTO ai_generations (created_at, created_by, campaign_id, kind, provider, model, prompt_version, effort, brief_redacted,
               redaction_json, system_prompt, user_prompt, context_json, context_tokens_est, token_budget, input_tokens, output_tokens,
               cache_read_tokens, latency_ms, request_id, stop_reason, fallback_used, status, error, output_subject, output_body,
               cited_context_json, review_flags_json, est_cost_usd)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (g.get("created_at") or _now(), g.get("created_by"), g.get("campaign_id"), g.get("kind", "campaign_draft"),
         g.get("provider", "anthropic"), g.get("model"), g.get("prompt_version"), g.get("effort"), g.get("brief_redacted"),
         j(g.get("redaction")), g.get("system_prompt"), g.get("user_prompt"), j(g.get("context")), g.get("context_tokens_est"),
         g.get("token_budget"), g.get("input_tokens"), g.get("output_tokens"), g.get("cache_read_tokens"), g.get("latency_ms"),
         g.get("request_id"), g.get("stop_reason"), 1 if g.get("fallback_used") else 0, g["status"], (g.get("error") or None) and str(g["error"])[:400],
         g.get("output_subject"), g.get("output_body"), j(g.get("cited_context")), j(g.get("review_flags")), g.get("est_cost_usd")))
    return cur.lastrowid


def get_generation(conn, generation_id):
    r = conn.execute("SELECT * FROM ai_generations WHERE id=?", (generation_id,)).fetchone()
    if not r:
        return None
    g = dict(r)
    for key, col in (("context", "context_json"), ("redaction", "redaction_json"), ("cited", "cited_context_json"), ("flags", "review_flags_json")):
        try:
            g[key] = json.loads(g[col]) if g.get(col) else ([] if key in ("cited", "flags", "context") else {})
        except ValueError:
            g[key] = [] if key in ("cited", "flags", "context") else {}
    return g


# ---------------------------------------------------------------------------------------------- feedback
def link_campaign(generation_id, campaign_id):
    """Attach a generation to the campaign it became (queued, off the request path)."""
    if not generation_id:
        return None
    return submit(lambda conn: conn.execute("UPDATE ai_generations SET campaign_id=? WHERE id=? AND campaign_id IS NULL", (campaign_id, generation_id)))


def log_feedback(generation_id, event, actor, campaign_id=None, final_subject=None, final_body=None, detail=""):
    """Record one human reaction. For events that carry the person's text, the Levenshtein distance from the AI's text is computed
    here (in the background writer) and stored with it. Safe to call with a missing generation id (then it does nothing)."""
    if not generation_id or event not in EVENTS:
        return None
    when = _now()

    def write(conn):
        gen = conn.execute("SELECT output_subject, output_body FROM ai_generations WHERE id=?", (generation_id,)).fetchone()
        if gen is None:
            return
        ds = db_ = ratio = outcome = None
        if final_subject is not None or final_body is not None:
            ds, db_, ratio = textdiff.edit_ratio(gen["output_subject"], gen["output_body"], final_subject or "", final_body or "")
            outcome = textdiff.outcome_for(ds + db_, ratio)
        conn.execute(
            """INSERT INTO ai_feedback (generation_id, campaign_id, occurred_at, actor, event, final_subject, final_body, subject_distance,
                   body_distance, edit_ratio, outcome, detail) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (generation_id, campaign_id, when, actor, event, final_subject, final_body, ds, db_, ratio, outcome, str(detail or "")[:300]))
    return submit(write)
