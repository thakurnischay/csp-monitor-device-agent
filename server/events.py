"""Writes to the events/incidents/admin_audit_log tables. Kept in one place
so api.py (device state transitions) and routes.py (admin actions) share the
exact same insert logic rather than duplicating SQL.

Nothing here creates a row for "nothing changed" - only for a real
transition, a completed test print, or an explicit admin action (Phase 9's
"do NOT create massive duplicate events for every identical heartbeat").
"""
from datetime import datetime, timezone

from device_state import PROBLEM_STATES


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def record_event(conn, csp_id: str, event_type: str, detail: str = "") -> None:
    conn.execute(
        "INSERT INTO events (csp_id, event_type, detail, occurred_at) VALUES (?,?,?,?)",
        (csp_id, event_type, detail or "", _now()))


def record_audit(conn, actor: str, action: str, target_csp: str = "", detail: str = "") -> None:
    conn.execute(
        "INSERT INTO admin_audit_log (actor, action, target_csp, detail, occurred_at) VALUES (?,?,?,?,?)",
        (actor or "unknown", action, target_csp or "", detail or "", _now()))


def _open_incident(conn, csp_id: str, device: str, problem: str) -> None:
    already_open = conn.execute(
        "SELECT id FROM incidents WHERE csp_id=? AND device=? AND status='OPEN'",
        (csp_id, device)).fetchone()
    if already_open:
        return
    conn.execute(
        "INSERT INTO incidents (csp_id, device, problem, status, started_at) VALUES (?,?,?, 'OPEN', ?)",
        (csp_id, device, problem, _now()))


def _resolve_incidents(conn, csp_id: str, device: str) -> None:
    conn.execute(
        "UPDATE incidents SET status='RESOLVED', resolved_at=? WHERE csp_id=? AND device=? AND status='OPEN'",
        (_now(), csp_id, device))


def apply_device_transition(conn, csp_id: str, device: str, old_state: str, new_state: str, status_text: str) -> None:
    """Given a device's state before/after this report, write an event if it
    actually changed, and open/resolve an incident as appropriate. `device`
    is 'printer' or 'microatm'. No-op if old_state == new_state."""
    if old_state == new_state:
        return
    was_problem = old_state in PROBLEM_STATES
    is_problem = new_state in PROBLEM_STATES
    if is_problem and not was_problem:
        record_event(conn, csp_id, f"{device}_disconnected", f"{new_state}: {status_text}")
        _open_incident(conn, csp_id, device, status_text or new_state)
    elif was_problem and not is_problem:
        record_event(conn, csp_id, f"{device}_connected", f"{new_state}: {status_text}")
        _resolve_incidents(conn, csp_id, device)
    else:
        # Moved between two non-problem states (e.g. NOT_CONFIGURED -> OK)
        # or two problem states (e.g. NOT_DETECTED -> PROBLEM) - still worth
        # a lightweight event, just not an incident open/close.
        record_event(conn, csp_id, f"{device}_state_changed", f"{old_state} -> {new_state}: {status_text}")
