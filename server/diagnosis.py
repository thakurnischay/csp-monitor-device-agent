"""Storing and reading the agent's root-cause diagnosis (agent 2.0+).

Everything arriving from the network is clipped, type-checked and whitelisted
before it is stored: a report can never put arbitrary markup or unbounded text
into the database, and a malformed diagnosis can never break the heartbeat.
"""
import json
from datetime import datetime, timezone

from events import record_event

DEVICES = ("printer", "microatm", "biometric", "gps")
HEALTH_STATES = {"HEALTHY", "DEGRADED", "SOFTWARE_ISSUE", "DRIVER_ISSUE", "SUSPECTED_HARDWARE",
                 "SERVICE_UNAVAILABLE", "DIAGNOSIS_REQUIRED", "NOT_CONNECTED", "UNVERIFIED", "NOT_SET_UP"}
SEVERITIES = ("critical", "high", "medium", "low", "none")
CONFIDENCES = {"confirmed", "likely", "possible"}
# what the dashboard calls each state (label, badge colour)
STATE_LABELS = {
    "HEALTHY": ("Healthy", "green"), "DEGRADED": ("Degraded", "amber"),
    "SOFTWARE_ISSUE": ("Software issue", "red"), "DRIVER_ISSUE": ("Driver issue", "red"),
    "SUSPECTED_HARDWARE": ("Suspected hardware failure", "red"),
    "SERVICE_UNAVAILABLE": ("Service unavailable", "red"),
    "DIAGNOSIS_REQUIRED": ("Diagnosis required", "amber"), "NOT_CONNECTED": ("Not connected", "amber"),
    "UNVERIFIED": ("Unverified", "amber"), "NOT_SET_UP": ("Not set up", "gray"),
}
BLOCKING = ("critical", "high", "medium")        # severities that count as an open problem


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _s(value, n):
    return " ".join(str(value if value is not None else "").split())[:n]


def _list(value, n_items, n_chars):
    if not isinstance(value, list):
        return []
    return [_s(v, n_chars) for v in value[:n_items] if v not in (None, "")]


def _impact(value):
    if not isinstance(value, dict):
        return {}
    flows = []
    for w in (value.get("workflows") or [])[:6]:
        if isinstance(w, dict):
            flows.append({"name": _s(w.get("name"), 200), "needs": _s(w.get("needs"), 12),
                          "confirmed": bool(w.get("confirmed"))})
    return {"workflows": flows, "assessment": _s(value.get("assessment"), 300),
            "workaround": _s(value.get("workaround"), 300), "revenue": _s(value.get("revenue"), 300),
            "confirmed": bool(value.get("confirmed")), "required": bool(value.get("required"))}


def _verification(value):
    if not isinstance(value, dict):
        return None
    return {"status": _s(value.get("status"), 30), "at": _s(value.get("at"), 40),
            "note": _s(value.get("note"), 200), "codes": _list(value.get("codes"), 6, 60)}


def clean_finding(f):
    if not isinstance(f, dict) or not f.get("code"):
        return None
    sev = f.get("severity") if f.get("severity") in SEVERITIES else "low"
    fix = f.get("auto_fix") if isinstance(f.get("auto_fix"), dict) else None
    return {"code": _s(f.get("code"), 80),
            "state": f.get("state") if f.get("state") in HEALTH_STATES else "DIAGNOSIS_REQUIRED",
            "severity": sev, "title": _s(f.get("title"), 160), "symptom": _s(f.get("symptom"), 300),
            "root_cause": _s(f.get("root_cause"), 400),
            "confidence": f.get("confidence") if f.get("confidence") in CONFIDENCES else "possible",
            "category": _s(f.get("category"), 30), "evidence": _list(f.get("evidence"), 10, 240),
            "actions": _list(f.get("actions"), 9, 300), "operator_action": bool(f.get("operator_action", True)),
            "auto_fix": ({"type": _s(fix.get("type"), 30), "service": _s(fix.get("service"), 80),
                          "label": _s(fix.get("label"), 80)} if fix else None)}


def store(conn, csp_id, body, now=None):
    """Upsert each device's current diagnosis; open / refresh / RESOLVE issues.
    An issue that is no longer reported was verified as recovered by the check
    that just ran, which is recorded as a recovery event. Returns the set of
    devices handled (so the caller can skip the older, shallower incident logic)."""
    now = now or _now()
    diag = body.get("diagnosis") if isinstance(body, dict) else None
    handled = set()
    if not isinstance(diag, dict):
        return handled
    for dev in DEVICES:
        d = diag.get(dev)
        if not isinstance(d, dict):
            continue
        state = d.get("health_state") if d.get("health_state") in HEALTH_STATES else "DIAGNOSIS_REQUIRED"
        severity = d.get("severity") if d.get("severity") in SEVERITIES else "none"
        findings = [x for x in (clean_finding(f) for f in (d.get("findings") or [])[:6]) if x]
        detail = {"checked": _list(d.get("checked"), 12, 160), "not_checked": _list(d.get("not_checked"), 8, 240),
                  "notes": _list(d.get("notes"), 6, 240), "impact": _impact(d.get("impact")),
                  "verification": _verification(d.get("verification")),
                  "actions_taken": [{"t": _s(a.get("t"), 40), "action": _s(a.get("action"), 30),
                                     "target": _s(a.get("target"), 80), "result": _s(a.get("result"), 160)}
                                    for a in (d.get("actions_taken") or [])[-5:] if isinstance(a, dict)]}
        conn.execute(
            """INSERT INTO device_diagnosis (csp_id, device, health_state, severity, summary, detail_json,
                                             last_healthy_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT(csp_id, device) DO UPDATE SET health_state=excluded.health_state,
                   severity=excluded.severity, summary=excluded.summary, detail_json=excluded.detail_json,
                   last_healthy_at=COALESCE(excluded.last_healthy_at, device_diagnosis.last_healthy_at),
                   updated_at=excluded.updated_at""",
            (csp_id, dev, state, severity, _s(d.get("summary"), 200), json.dumps(detail),
             _s(d.get("last_healthy_at"), 40) or None, now))
        codes = []
        for f in findings:
            codes.append(f["code"])
            row = conn.execute("SELECT id FROM device_issues WHERE csp_id=? AND device=? AND code=? AND status='OPEN'",
                               (csp_id, dev, f["code"])).fetchone()
            values = (f["state"], f["severity"], f["title"], f["symptom"], f["root_cause"], f["confidence"],
                      f["category"], json.dumps(f["evidence"]), json.dumps(f["actions"]),
                      1 if f["operator_action"] else 0, json.dumps(f["auto_fix"]) if f["auto_fix"] else None)
            if row:
                conn.execute("""UPDATE device_issues SET state=?, severity=?, title=?, symptom=?, root_cause=?,
                                confidence=?, category=?, evidence_json=?, actions_json=?, operator_action=?,
                                auto_fix_json=?, last_seen=? WHERE id=?""", values + (now, row["id"]))
            else:
                n = conn.execute("SELECT COUNT(*) c FROM device_issues WHERE csp_id=? AND device=? AND code=?",
                                 (csp_id, dev, f["code"])).fetchone()["c"]
                conn.execute("""INSERT INTO device_issues (csp_id, device, code, state, severity, title, symptom,
                                root_cause, confidence, category, evidence_json, actions_json, operator_action,
                                auto_fix_json, status, first_seen, last_seen, occurrences)
                                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,'OPEN',?,?,?)""",
                             (csp_id, dev, f["code"]) + values + (now, now, n + 1))
                record_event(conn, csp_id, f"{dev}_issue_opened", f"{f['severity']}: {f['title']}")
        still = conn.execute("SELECT id, code, title FROM device_issues WHERE csp_id=? AND device=? AND status='OPEN'",
                             (csp_id, dev)).fetchall()
        for r in still:
            if r["code"] not in codes:
                conn.execute("UPDATE device_issues SET status='RESOLVED', resolved_at=? WHERE id=?", (now, r["id"]))
                record_event(conn, csp_id, f"{dev}_recovery_verified", f"Recovered: {r['title']}")
        handled.add(dev)
    return handled


def load(conn, csp_id):
    """{device: {...diagnosis...}} for one CSP, with detail_json parsed."""
    out = {}
    for r in conn.execute("SELECT * FROM device_diagnosis WHERE csp_id=?", (csp_id,)).fetchall():
        d = dict(r)
        try:
            d["detail"] = json.loads(d.pop("detail_json") or "{}")
        except ValueError:
            d["detail"] = {}
        label, colour = STATE_LABELS.get(d["health_state"], (d["health_state"], "gray"))
        d["label"], d["colour"] = label, colour
        out[d["device"]] = d
    return out


def load_all(conn):
    """{csp_id: {device: row}} (detail not parsed - the fleet table needs only the headline)."""
    out = {}
    for r in conn.execute("SELECT csp_id, device, health_state, severity, summary, last_healthy_at, detail_json "
                          "FROM device_diagnosis").fetchall():
        d = dict(r)
        label, colour = STATE_LABELS.get(d["health_state"], (d["health_state"], "gray"))
        d["label"], d["colour"] = label, colour
        try:
            ver = (json.loads(d.pop("detail_json") or "{}").get("verification") or {})
        except ValueError:
            ver = {}
        d["recovery_verified"] = ver.get("status") == "recovery_verified"
        out.setdefault(d["csp_id"], {})[d["device"]] = d
    return out


def issues_for(conn, csp_id, limit=60):
    """Open issues first, then the most recent resolved ones, with JSON parsed."""
    rows = conn.execute(
        """SELECT * FROM device_issues WHERE csp_id=? ORDER BY (status='OPEN') DESC,
           CASE severity WHEN 'critical' THEN 0 WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END,
           COALESCE(resolved_at, last_seen) DESC LIMIT ?""", (csp_id, limit)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        for k in ("evidence", "actions"):
            try:
                d[k] = json.loads(d.pop(k + "_json") or "[]")
            except ValueError:
                d[k] = []
        try:
            d["auto_fix"] = json.loads(d.pop("auto_fix_json")) if d.get("auto_fix_json") else None
        except ValueError:
            d["auto_fix"] = None
        out.append(d)
    return out
