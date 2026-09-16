"""The one API endpoint every agent talks to: POST /api/report.

Auth is a per-CSP API key (api_keys table), same shape as the report the
agent builds in agent/reporter.py.build_payload(): {csp_id, printer, microatm}.
"""
import hmac
from datetime import datetime, timezone

from flask import Blueprint, jsonify, request

from db import get_connection

api_bp = Blueprint("api", __name__)


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _valid_key(conn, csp_id, key) -> bool:
    if not csp_id or not key:
        return False
    row = conn.execute(
        "SELECT api_key, active FROM api_keys WHERE csp_id=?", (csp_id,)).fetchone()
    return bool(row) and row["active"] == 1 and hmac.compare_digest(str(row["api_key"]), str(key))


def _bool(d, k) -> int:
    return 1 if (isinstance(d, dict) and d.get(k)) else 0


def _text(d, k, maxlen=160) -> str:
    return str((d or {}).get(k) or "")[:maxlen]


@api_bp.route("/api/v1", methods=["GET"])
def root():
    return jsonify({"ok": True, "service": "csp-device-monitor", "endpoints": ["/api/report (POST)"]})


@api_bp.route("/api/report", methods=["POST"])
def report():
    key = request.headers.get("X-API-Key", "")
    body = request.get_json(silent=True) or {}
    csp_id = str(body.get("csp_id") or "").strip()

    with get_connection() as conn:
        if not _valid_key(conn, csp_id, key):
            return jsonify({"ok": False, "error": "invalid csp_id or API key"}), 401

        printer = body.get("printer") or {}
        microatm = body.get("microatm") or {}
        functional = body.get("printer_functional_test") or {}
        now = _now()

        # The admin-set label (from Issue Key) is authoritative display name.
        label_row = conn.execute("SELECT name FROM api_keys WHERE csp_id=?", (csp_id,)).fetchone()
        name = (label_row["name"] if label_row else "") or ""

        exists = conn.execute("SELECT csp_id FROM csps WHERE csp_id=?", (csp_id,)).fetchone()
        test_ran = _bool(functional, "ran")
        params = (
            name,
            _bool(printer, "configured"), _bool(printer, "ok"), _text(printer, "status"),
            _bool(microatm, "configured"), _bool(microatm, "ok"), _text(microatm, "status"),
            test_ran, _bool(functional, "ok"), _text(functional, "detail", 300),
        )
        if exists:
            conn.execute(
                """UPDATE csps SET name=?,
                       printer_configured=?, printer_ok=?, printer_status=?,
                       microatm_configured=?, microatm_ok=?, microatm_status=?,
                       printer_test_ran=?, printer_test_ok=?, printer_test_detail=?,
                       printer_test_checked_at=CASE WHEN ?=1 THEN ? ELSE printer_test_checked_at END,
                       last_seen=? WHERE csp_id=?""",
                params + (test_ran, now, now, csp_id))
        else:
            conn.execute(
                """INSERT INTO csps (csp_id, name,
                       printer_configured, printer_ok, printer_status,
                       microatm_configured, microatm_ok, microatm_status,
                       printer_test_ran, printer_test_ok, printer_test_detail, printer_test_checked_at,
                       first_seen, last_seen)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (csp_id,) + params + (now if test_ran else None, now, now))
        conn.commit()
    return jsonify({"ok": True, "received_at": now})
