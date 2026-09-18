"""Explicit device/CSP status states (replaces ambiguous booleans for
anything that decides what to SHOW or whether to open/resolve an incident).

This only names states more precisely from data the agent already sends
(configured/present/ok/status) - it does not change what the agent reports
or how device_health.py detects anything. The agent's own timeout/error text
("scan timed out - a security tool...") is a stable, code-controlled string,
so matching on it here is a safe way to distinguish "we couldn't even scan"
from "we scanned and nothing's there" without touching the agent's payload
shape.
"""
from datetime import datetime, timezone, timedelta

# Device-level states
OK = "OK"
PROBLEM = "PROBLEM"
NOT_CONFIGURED = "NOT_CONFIGURED"
NOT_DETECTED = "NOT_DETECTED"
SCAN_ERROR = "SCAN_ERROR"
UNKNOWN = "UNKNOWN"

# States that represent an actionable problem worth an incident.
PROBLEM_STATES = {PROBLEM, NOT_DETECTED, SCAN_ERROR}

# CSP-level (agent liveness) states
ONLINE = "ONLINE"
STALE = "STALE"
OFFLINE = "OFFLINE"
NEVER_REPORTED = "NEVER_REPORTED"

ONLINE_WINDOW_MIN = 15
STALE_WINDOW_MIN = 60


def derive_device_state(configured, present, ok, status_text: str) -> str:
    text = (status_text or "").lower()
    if "scan timed out" in text or "could not send test page" in text:
        return SCAN_ERROR
    if not configured:
        return NOT_CONFIGURED
    if not present:
        return NOT_DETECTED
    if ok:
        return OK
    return PROBLEM


def derive_csp_state(last_seen_iso: str, now: datetime = None) -> str:
    if not last_seen_iso:
        return NEVER_REPORTED
    try:
        dt = datetime.fromisoformat(last_seen_iso)
    except (ValueError, TypeError):
        return NEVER_REPORTED
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc)
    age_min = (now - dt).total_seconds() / 60.0
    if age_min <= ONLINE_WINDOW_MIN:
        return ONLINE
    if age_min <= STALE_WINDOW_MIN:
        return STALE
    return OFFLINE
