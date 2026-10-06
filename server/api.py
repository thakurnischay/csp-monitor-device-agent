"""The one API endpoint every agent talks to: POST /api/report.

Auth is a per-CSP API key (api_keys table), same shape as the report the
agent builds in agent/reporter.py.build_payload(): {csp_id, printer, microatm}.
"""
import logging
from datetime import datetime, timezone

from flask import Blueprint, jsonify, request

from db import get_connection
from device_state import derive_device_state
from events import apply_device_transition, record_event
from security import report_limiter, verify_api_key

log = logging.getLogger("csp_monitor.api")

api_bp = Blueprint("api", __name__)


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _valid_key(conn, csp_id, key) -> bool:
    if not csp_id or not key:
        return False
    row = conn.execute(
        "SELECT api_key_hash, active FROM api_keys WHERE csp_id=?", (csp_id,)).fetchone()
    if not row or row["active"] != 1:
        return False
    return verify_api_key(key, row["api_key_hash"])


def _bool(d, k) -> int:
    return 1 if (isinstance(d, dict) and d.get(k)) else 0


def _text(d, k, maxlen=160) -> str:
    if not isinstance(d, dict):
        return ""
    return str(d.get(k) or "")[:maxlen]


def _int_or_none(d, k):
    try:
        return int(d.get(k))
    except (TypeError, ValueError, AttributeError):
        return None


@api_bp.route("/api/v1", methods=["GET"])
def root():
    return jsonify({"ok": True, "service": "csp-device-monitor", "endpoints": ["/api/report (POST)"]})


@api_bp.route("/health", methods=["GET"])
def health():
    """Liveness only - the process is up and answering HTTP at all. Never
    touches the database, so this stays fast and meaningful even if the DB
    is having trouble (that's what /readiness is for)."""
    return jsonify({"ok": True, "status": "running"})


@api_bp.route("/readiness", methods=["GET"])
def readiness():
    """Liveness AND the database is actually reachable. Never exposes any
    secret/path/config - just a boolean per dependency."""
    try:
        with get_connection() as conn:
            conn.execute("SELECT 1").fetchone()
        return jsonify({"ok": True, "status": "ready", "database": "ok"})
    except Exception:
        log.error("readiness check failed - database unreachable", exc_info=True)
        return jsonify({"ok": False, "status": "not ready", "database": "unreachable"}), 503


@api_bp.route("/api/report", methods=["POST"])
def report():
    # .strip() protects against an agent that saved its key with stray
    # whitespace (e.g. a copy-paste mishap on the one-time key reveal) -
    # every such report would otherwise fail as "invalid API key" forever
    # with no visible reason why, even on agent versions already deployed
    # before this was fixed on the agent side too.
    key = request.headers.get("X-API-Key", "").strip()
    body = request.get_json(silent=True) or {}
    csp_id = str(body.get("csp_id") or "").strip()

    if csp_id and not report_limiter.allow(csp_id):
        log.warning("rate limit hit for csp_id=%s", csp_id)
        return jsonify({"ok": False, "error": "too many reports, slow down"}), 429

    with get_connection() as conn:
        if not _valid_key(conn, csp_id, key):
            log.warning("rejected report: invalid csp_id/API key (csp_id=%r)", csp_id)
            return jsonify({"ok": False, "error": "invalid csp_id or API key"}), 401
        conn.execute("UPDATE api_keys SET last_used_at=? WHERE csp_id=?", (_now(), csp_id))

        printer = body.get("printer") or {}
        microatm = body.get("microatm") or {}
        functional = body.get("printer_functional_test") or {}
        now = _now()

        # The admin-set label (from Issue Key) is authoritative display name.
        label_row = conn.execute("SELECT name FROM api_keys WHERE csp_id=?", (csp_id,)).fetchone()
        name = (label_row["name"] if label_row else "") or ""

        old = conn.execute("SELECT * FROM csps WHERE csp_id=?", (csp_id,)).fetchone()
        test_ran = _bool(functional, "ran")

        # Metadata added in agent schema_version 2 - all optional so an
        # older agent that doesn't send them still reports fine (Phase 28:
        # backward-compatible, no field is required to exist).
        agent_version = _text(body, "agent_version", 40) or None
        schema_version = _int_or_none(body, "schema_version")
        os_info = _text(body, "os", 200) or None
        hostname = _text(body, "hostname", 100) or None

        new_printer_present = _bool(printer, "present")
        new_microatm_present = _bool(microatm, "present")
        # `core` matches the shared prefix of BOTH statements exactly (name
        # through printer_test_detail); `metadata` is kept separate because
        # the UPDATE statement's CASE-WHEN clause for printer_test_checked_at
        # sits BETWEEN the core fields and the metadata fields in the SQL -
        # concatenating everything into one flat tuple in the wrong order
        # previously misaligned every value after printer_test_detail.
        core = (
            name,
            _bool(printer, "configured"), new_printer_present, _bool(printer, "ok"), _text(printer, "status"),
            _bool(microatm, "configured"), new_microatm_present, _bool(microatm, "ok"), _text(microatm, "status"),
            test_ran, _bool(functional, "ok"), _text(functional, "detail", 300),
        )
        metadata = (agent_version, schema_version, os_info, hostname)
        if old:
            # agent_version/schema_version/os_info/hostname use COALESCE, not
            # a plain overwrite: an older agent (or one that briefly rolls
            # back) that doesn't send this metadata must not blow away
            # previously-known-good values with NULL - only a report that
            # actually carries a new value should replace the old one.
            conn.execute(
                """UPDATE csps SET name=?,
                       printer_configured=?, printer_present=?, printer_ok=?, printer_status=?,
                       microatm_configured=?, microatm_present=?, microatm_ok=?, microatm_status=?,
                       printer_test_ran=?, printer_test_ok=?, printer_test_detail=?,
                       printer_test_checked_at=CASE WHEN ?=1 THEN ? ELSE printer_test_checked_at END,
                       agent_version=COALESCE(?, agent_version), schema_version=COALESCE(?, schema_version),
                       os_info=COALESCE(?, os_info), hostname=COALESCE(?, hostname),
                       last_seen=? WHERE csp_id=?""",
                core + (test_ran, now) + metadata + (now, csp_id))
        else:
            conn.execute(
                """INSERT INTO csps (csp_id, name,
                       printer_configured, printer_present, printer_ok, printer_status,
                       microatm_configured, microatm_present, microatm_ok, microatm_status,
                       printer_test_ran, printer_test_ok, printer_test_detail, printer_test_checked_at,
                       agent_version, schema_version, os_info, hostname,
                       first_seen, last_seen)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (csp_id,) + core + (now if test_ran else None,) + metadata + (now, now))

        # Event/incident history - only meaningful transitions, never one row
        # per identical heartbeat (see events.py). A brand-new CSP (no `old`
        # row) is treated as transitioning from NOT_CONFIGURED, so an
        # immediately-broken first-ever report still opens an incident right
        # away instead of silently waiting for a second report to notice.
        old_printer_state = derive_device_state(
            old["printer_configured"] if old else 0, old["printer_present"] if old else 0,
            old["printer_ok"] if old else 0, old["printer_status"] if old else "")
        new_printer_state = derive_device_state(
            _bool(printer, "configured"), new_printer_present, _bool(printer, "ok"), _text(printer, "status"))
        apply_device_transition(conn, csp_id, "printer", old_printer_state, new_printer_state, _text(printer, "status"))

        old_microatm_state = derive_device_state(
            old["microatm_configured"] if old else 0, old["microatm_present"] if old else 0,
            old["microatm_ok"] if old else 0, old["microatm_status"] if old else "")
        new_microatm_state = derive_device_state(
            _bool(microatm, "configured"), new_microatm_present, _bool(microatm, "ok"), _text(microatm, "status"))
        apply_device_transition(conn, csp_id, "microatm", old_microatm_state, new_microatm_state, _text(microatm, "status"))

        # Biometric scanner / GPS dongle (agent schema_version 3+). Only
        # touched when the report actually carries them: an older agent that
        # doesn't send these keys must not have the column defaults read as
        # "device disconnected", nor generate bogus transition events. `dev`
        # comes from this fixed tuple, never from request data.
        for dev in ("biometric", "gps"):
            d = body.get(dev)
            if not isinstance(d, dict):
                continue
            conn.execute(
                f"UPDATE csps SET {dev}_configured=?, {dev}_present=?, {dev}_ok=?, "
                f"{dev}_status=? WHERE csp_id=?",
                (_bool(d, "configured"), _bool(d, "present"), _bool(d, "ok"),
                 _text(d, "status"), csp_id))
            old_state = derive_device_state(
                old[f"{dev}_configured"] if old else 0, old[f"{dev}_present"] if old else 0,
                old[f"{dev}_ok"] if old else 0, old[f"{dev}_status"] if old else "")
            new_state = derive_device_state(
                _bool(d, "configured"), _bool(d, "present"), _bool(d, "ok"), _text(d, "status"))
            apply_device_transition(conn, csp_id, dev, old_state, new_state, _text(d, "status"))

        # A test print only "happened" (worth an event) when this report
        # carries a freshly-run result, not the same cached one reported
        # again on every heartbeat for the rest of the day.
        old_checked_at = old["printer_test_checked_at"] if old else None
        new_checked_at = now if test_ran else old_checked_at
        if test_ran and new_checked_at != old_checked_at:
            event_type = "test_print_success" if _bool(functional, "ok") else "test_print_failure"
            record_event(conn, csp_id, event_type, _text(functional, "detail", 300))

        conn.commit()
    log.debug("accepted report from csp_id=%s", csp_id)
    return jsonify({"ok": True, "received_at": now})
