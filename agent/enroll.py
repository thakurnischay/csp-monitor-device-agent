"""Agent-side enrollment: how a brand-new CSP PC gets a credential WITHOUT anyone typing or seeing an API key.

    1. verify()            "is this a real CSP code, and does the name match?"  (no credential involved)
    2. the agent makes its OWN random key and keeps it in agent_config.json (never shown to anyone)
    3. request()           sends that key + code + name; the server stores only the key's HASH, as PENDING
    4. an administrator approves it in the dashboard (or pre-approved the CSP) - only then does the key work
    5. poll()              asks "approved yet?" using the same key; the heartbeat loop starts when it is

A CSP code and name are not secrets, so they never authenticate anything on their own. A CSP that already has
an agent (config has a key + no pending enrollment) never enrolls: its identity is preserved untouched.
"""
import hashlib
import logging
import os
import secrets
import socket

import requests

import config_store

log = logging.getLogger("csp_agent.enroll")
TIMEOUT = (5, 20)
FRIENDLY_NETWORK = "Could not reach the server. Please check the internet connection and try again."


def new_key():
    return secrets.token_urlsafe(32)


def machine_id():
    """A stable, non-reversible id for this PC (helps the administrator recognise a reinstall). Never sent raw."""
    raw = ""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography") as k:
            raw = winreg.QueryValueEx(k, "MachineGuid")[0]
    except Exception:
        raw = socket.gethostname()
    return hashlib.sha256(("csp-monitor|" + str(raw)).encode()).hexdigest()[:32]


def _url(server_url, path):
    return server_url.rstrip("/") + path


def verify(server_url, csp_id, name, session=None):
    """-> {"ok": bool, "verdict": str, "message": str}. Network trouble is reported in plain words, never as a traceback."""
    http = session or requests
    try:
        r = http.post(_url(server_url, "/api/enroll/verify"), json={"csp_id": csp_id, "name": name}, timeout=TIMEOUT)
        body = r.json()
        if r.status_code == 429:
            return {"ok": False, "verdict": "busy", "message": body.get("message", "Too many attempts. Please wait a few minutes.")}
        return {"ok": bool(body.get("ok")), "verdict": body.get("verdict", "invalid"),
                "message": body.get("message", "The server did not accept that.")}
    except (requests.RequestException, ValueError):
        return {"ok": False, "verdict": "network", "message": FRIENDLY_NETWORK}


def request_enrollment(server_url, key, csp_id, name, agent_version="", session=None):
    """-> {"status": pending|approved|rejected|superseded|invalid|unknown_code|name_mismatch|network|busy, "message": str}"""
    http = session or requests
    try:
        r = http.post(_url(server_url, "/api/enroll"), headers={"X-API-Key": key}, timeout=TIMEOUT,
                      json={"csp_id": csp_id, "name": name, "hostname": socket.gethostname(),
                            "machine_id": machine_id(), "agent_version": agent_version})
        body = r.json()
        return {"status": body.get("status", "invalid"), "message": body.get("message", "")}
    except (requests.RequestException, ValueError):
        return {"status": "network", "message": FRIENDLY_NETWORK}


def poll_status(server_url, key, csp_id, session=None):
    http = session or requests
    try:
        r = http.post(_url(server_url, "/api/enroll/status"), headers={"X-API-Key": key}, json={"csp_id": csp_id}, timeout=TIMEOUT)
        return {"status": r.json().get("status", "unknown")}
    except (requests.RequestException, ValueError):
        return {"status": "network"}


def start(cfg_save, server_url, csp_id, name, agent_version="", session=None):
    """Begin enrollment for a NEW PC: make the key, remember it, ask the server. `cfg_save` writes agent_config.json.
    The key is saved BEFORE the request, so a lost reply can never strand the PC (the same key is simply reused)."""
    key = new_key()
    cfg_save(csp_id=csp_id, csp_name=name, api_key=key, server_url=server_url, enroll_status="pending",
             enroll_message="Waiting for the administrator to approve this PC.")
    res = request_enrollment(server_url, key, csp_id, name, agent_version, session)
    return finish(cfg_save, res)


def finish(cfg_save, res):
    status = res.get("status")
    if status in ("approved", "pending"):
        cfg_save(enroll_status=status, enroll_message="" if status == "approved" else "Waiting for the administrator to approve this PC.")
    elif status in ("network", "busy"):
        cfg_save(enroll_status="error", enroll_message=res.get("message", ""))       # retried automatically later
    else:
        cfg_save(enroll_status="rejected", enroll_message=res.get("message", "The registration was not accepted."))
    return res


def maintain(cfg=None, session=None):
    """Called by the heartbeat loop. Returns True if the loop should send a heartbeat now, False to wait.
    Keeps a pending enrollment moving and retries one that hit a network error. Never raises."""
    cfg = cfg or config_store.load()
    state = cfg.get("enroll_status", "")
    if state in ("", "approved"):
        return True                                   # an established agent, or a finished enrollment
    if state in ("rejected", "superseded"):
        return False                                  # stop: needs an administrator / a new installation
    try:
        if state == "error":                          # the first request never reached the server: send it again
            res = request_enrollment(cfg["server_url"], cfg["api_key"], cfg["csp_id"], cfg.get("csp_name") or cfg["csp_id"], session=session)
            finish(config_store.save, res)
            return res.get("status") == "approved"
        res = poll_status(cfg["server_url"], cfg["api_key"], cfg["csp_id"], session)
        if res["status"] == "approved":
            config_store.save(enroll_status="approved", enroll_message="")
            return True
        if res["status"] in ("rejected", "superseded", "unknown"):
            config_store.save(enroll_status="rejected" if res["status"] != "superseded" else "superseded",
                              enroll_message={"rejected": "The administrator did not approve this PC.",
                                              "superseded": "A newer registration replaced this one.",
                                              "unknown": "The server has no record of this registration."}[res["status"]])
        return False
    except Exception:
        log.exception("enrollment maintenance failed")
        return False
