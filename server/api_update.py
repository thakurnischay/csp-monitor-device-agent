"""Agent-facing endpoints for enrollment and updates. Outbound-only: agents call these; the server never calls a CSP PC.

    POST /api/enroll/verify   {csp_id, name}                         -> is this a real CSP code + matching name?  (no credentials issued)
    POST /api/enroll          X-API-Key: <key the agent made>        -> create a PENDING enrollment (or approved if admin pre-approved)
    POST /api/enroll/status   X-API-Key: <that key>, {csp_id}        -> pending / approved / rejected / superseded
    GET  /api/update/manifest ?csp_id=&deployment_id=   X-API-Key    -> signed manifest (exact bytes, base64) + signature
    GET  /api/update/package  ?csp_id=&deployment_id=   X-API-Key    -> the zip (resumable: HTTP Range)
    POST /api/update/status   X-API-Key, {csp_id, deployment_id, attempt, status, detail} -> {ok, proceed}

Enrollment endpoints take no existing credential by design (the agent has none yet), so they are strictly
rate-limited, validated, and issue nothing by themselves.
"""
import logging

from flask import Blueprint, jsonify, request, send_file

import registry
import updates
from api import _valid_key
from db import get_connection
from security import enroll_csp_limiter, enroll_ip_limiter, update_limiter

log = logging.getLogger("csp_monitor.api_update")
update_bp = Blueprint("update_api", __name__)


def _ip():
    return request.remote_addr or "unknown"


def _enroll_limited(csp_id):
    if not enroll_ip_limiter.allow(_ip()) or (csp_id and not enroll_csp_limiter.allow(csp_id)):
        return jsonify({"ok": False, "status": "busy", "message": "Too many attempts. Please wait a few minutes and try again."}), 429
    return None


def _json():
    body = request.get_json(silent=True)
    return body if isinstance(body, dict) else {}


@update_bp.route("/api/enroll/verify", methods=["POST"])
def enroll_verify():
    body = _json()
    csp_id = str(body.get("csp_id") or "").strip()
    limited = _enroll_limited(csp_id)
    if limited:
        return limited
    with get_connection() as conn:
        verdict = registry.verify(conn, csp_id, body.get("name"))
    ok = verdict in ("ok", "no_name_on_record")
    return jsonify({"ok": ok, "verdict": verdict, "message": registry.MESSAGES[verdict]}), (200 if ok else 400)


@update_bp.route("/api/enroll", methods=["POST"])
def enroll():
    body = _json()
    csp_id = str(body.get("csp_id") or "").strip()
    limited = _enroll_limited(csp_id)
    if limited:
        return limited
    key = request.headers.get("X-API-Key", "").strip()
    with get_connection() as conn:
        code, resp = registry.request_enrollment(
            conn, key, csp_id, body.get("name"), body.get("hostname", ""), body.get("machine_id", ""),
            body.get("agent_version", ""), _ip())
        conn.commit()
    log.info("enrollment request csp_id=%r -> %s", csp_id, resp.get("status"))
    return jsonify(resp), code


@update_bp.route("/api/enroll/status", methods=["POST"])
def enroll_status():
    body = _json()
    csp_id = str(body.get("csp_id") or "").strip()
    limited = _enroll_limited(csp_id)
    if limited:
        return limited
    with get_connection() as conn:
        code, resp = registry.status_for(conn, request.headers.get("X-API-Key", "").strip(), csp_id)
    return jsonify(resp), code


def _authenticate(conn, csp_id):
    key = request.headers.get("X-API-Key", "").strip()
    if not csp_id or not update_limiter.allow(csp_id):
        return False
    return _valid_key(conn, csp_id, key)


@update_bp.route("/api/update/manifest", methods=["GET"])
def update_manifest():
    csp_id = (request.args.get("csp_id") or "").strip()
    with get_connection() as conn:
        if not _authenticate(conn, csp_id):
            return jsonify({"ok": False, "error": "invalid csp_id or API key"}), 401
        d, reason = updates.authorized_deployment(conn, csp_id, request.args.get("deployment_id"))
        if d is None:
            return jsonify({"ok": False, "error": "unknown deployment"}), 404
        if reason:
            return jsonify({"ok": False, "proceed": False, "error": reason}), 409
        return jsonify({"ok": True, "attempt": d["attempt"], "manifest_b64": d["manifest_b64"],
                        "signature_b64": d["signature_b64"]})


@update_bp.route("/api/update/package", methods=["GET"])
def update_package():
    csp_id = (request.args.get("csp_id") or "").strip()
    with get_connection() as conn:
        if not _authenticate(conn, csp_id):
            return jsonify({"ok": False, "error": "invalid csp_id or API key"}), 401
        d, reason = updates.authorized_deployment(conn, csp_id, request.args.get("deployment_id"))
        if d is None:
            return jsonify({"ok": False, "error": "unknown deployment"}), 404
        if reason:
            return jsonify({"ok": False, "proceed": False, "error": reason}), 409
        path, sha = d["package_path"], d["package_sha256"]
    # conditional=True gives HTTP Range support, so an interrupted download resumes where it stopped
    resp = send_file(path, mimetype="application/zip", conditional=True, max_age=0, download_name="agent-release.zip")
    resp.headers["X-Package-SHA256"] = sha
    return resp


@update_bp.route("/api/update/status", methods=["POST"])
def update_status():
    body = _json()
    csp_id = str(body.get("csp_id") or "").strip()
    with get_connection() as conn:
        if not _authenticate(conn, csp_id):
            return jsonify({"ok": False, "error": "invalid csp_id or API key"}), 401
        result = updates.record_status(conn, csp_id, body.get("deployment_id"), body.get("attempt"),
                                       str(body.get("status") or ""), body.get("detail", ""))
        conn.commit()
    return jsonify(result), (200 if result.get("ok") else 404)
