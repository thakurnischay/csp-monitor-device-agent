"""Diagnosis learning, stage 2: collect what happened to each diagnosed problem across the fleet, collect the administrator's verdicts on
diagnoses, and summarise both per problem type with an honest "how much evidence is this" label.

This module only RECORDS and DESCRIBES. It never changes a diagnosis, a severity, a rule or any agent. The "signals" it shows are plain
observations for a person to look at; turning them into reviewed rule changes is a later stage and is always human-approved.

Everything arriving from agents is clipped, type-checked and whitelisted, and a malformed list can never break a heartbeat.
"""
import json
import logging
import statistics
from datetime import datetime, timedelta, timezone

log = logging.getLogger("csp_monitor.learning")
DEVICES = ("printer", "microatm", "biometric", "gps")
RESOLUTIONS = ("after_fix", "self_resolved")
VERDICTS = {"correct": "Correct", "wrong": "Wrong", "unclear": "Not sure"}
MIN_EPISODES = 5                 # fewer closed episodes than this = "not enough evidence"
MIN_VERDICTS = 3
MAX_ROWS_PER_REPORT = 40
KEEP_DAYS = 365


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _s(v, n):
    return " ".join(str(v if v is not None else "").split())[:n]


def _int(v, lo=0, hi=10 ** 9):
    try:
        return max(lo, min(int(v), hi))
    except (TypeError, ValueError):
        return lo


def _iso(v):
    try:
        datetime.fromisoformat(str(v))
        return _s(v, 40)
    except ValueError:
        return None


def clean_row(r):
    if not isinstance(r, dict) or r.get("device") not in DEVICES or not r.get("code"):
        return None
    first, last = _iso(r.get("first_seen")), _iso(r.get("resolved_at"))
    if not first or not last or r.get("resolution") not in RESOLUTIONS:
        return None
    actions = [{"action": _s(a.get("action"), 30), "target": _s(a.get("target"), 80), "result": _s(a.get("result"), 40)}
               for a in (r.get("actions") or [])[:10] if isinstance(a, dict)]
    return {"device": r["device"], "code": _s(r["code"], 80), "first_seen": first, "resolved_at": last,
            "duration_s": _int(r.get("duration_s")), "checks": _int(r.get("checks"), 1), "confidence": _s(r.get("confidence"), 12) or None,
            "severity": _s(r.get("severity"), 12) or None, "offered_fix": _s(r.get("offered_fix"), 30) or None,
            "resolution": r["resolution"], "recurred": 1 if r.get("recurred") else 0, "actions": actions}


def store(conn, csp_id, body, now=None):
    """Upsert the closed-problem rows an agent reported. Idempotent (keyed by csp/device/code/first_seen). Returns how many were kept."""
    rows = body.get("outcomes") if isinstance(body, dict) else None
    if not isinstance(rows, list):
        return 0
    now, kept = now or _now(), 0
    version = _s(body.get("agent_version"), 20) or None
    for raw in rows[:MAX_ROWS_PER_REPORT]:
        r = clean_row(raw)
        if not r:
            continue
        conn.execute(
            """INSERT INTO diagnosis_outcomes (csp_id, device, code, first_seen, resolved_at, duration_s, checks, confidence, severity,
                   offered_fix, resolution, recurred, actions_json, agent_version, received_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(csp_id, device, code, first_seen) DO UPDATE SET recurred=MAX(diagnosis_outcomes.recurred, excluded.recurred),
                   resolved_at=excluded.resolved_at, duration_s=excluded.duration_s, resolution=excluded.resolution,
                   actions_json=excluded.actions_json""",
            (csp_id, r["device"], r["code"], r["first_seen"], r["resolved_at"], r["duration_s"], r["checks"], r["confidence"], r["severity"],
             r["offered_fix"], r["resolution"], r["recurred"], json.dumps(r["actions"]), version, now))
        kept += 1
    return kept


def record_feedback(conn, admin, issue_id, verdict, note=""):
    """The administrator's verdict on one diagnosed issue. Raises ValueError for anything unknown."""
    if verdict not in VERDICTS:
        raise ValueError("Choose Correct, Wrong or Not sure.")
    issue = conn.execute("SELECT id, csp_id, device, code FROM device_issues WHERE id=?", (issue_id,)).fetchone()
    if not issue:
        raise ValueError("That issue no longer exists.")
    conn.execute("INSERT INTO diagnosis_feedback (created_at, admin, csp_id, device, code, issue_id, verdict, note) VALUES (?,?,?,?,?,?,?,?)",
                 (_now(), _s(admin, 60), issue["csp_id"], issue["device"], issue["code"], issue["id"], verdict, _s(note, 300) or None))
    return dict(issue)


def my_verdicts(conn, admin, issue_ids):
    """{issue_id: latest verdict by this admin} so the page can show what was already said."""
    if not issue_ids:
        return {}
    marks = ",".join("?" * len(issue_ids))
    out = {}
    for r in conn.execute(f"SELECT issue_id, verdict FROM diagnosis_feedback WHERE admin=? AND issue_id IN ({marks}) ORDER BY id", (admin, *issue_ids)):
        out[r["issue_id"]] = r["verdict"]
    return out


def _signals(s):
    """Plain observations, only once there is enough evidence. They describe; they do not change anything."""
    out = []
    n = s["episodes"]
    if n >= MIN_EPISODES:
        if s["self_resolved"] / n >= 0.7:
            out.append(("info", "Mostly clears by itself (%d of %d) - may be a passing blip rather than a fault" % (s["self_resolved"], n)))
        if s["recurred"] / n >= 0.3:
            out.append(("warn", "Often comes back within 3 days (%d of %d)" % (s["recurred"], n)))
    if s["fix_then_resolved"] >= 3:
        stuck = s["fix_stuck"] / s["fix_then_resolved"]
        out.append(("good", "The offered fix works and holds (%d of %d stayed fixed)" % (s["fix_stuck"], s["fix_then_resolved"])) if stuck >= 0.8
                   else ("warn", "The offered fix often does not hold (only %d of %d stayed fixed)" % (s["fix_stuck"], s["fix_then_resolved"])) if stuck < 0.5
                   else ("info", "The offered fix holds about half the time (%d of %d)" % (s["fix_stuck"], s["fix_then_resolved"])))
    v = s["verdicts"]
    if v["total"] >= MIN_VERDICTS and v["wrong"] > v["correct"]:
        out.append(("warn", "Administrators say this diagnosis is often wrong (%d wrong, %d correct)" % (v["wrong"], v["correct"])))
    elif v["total"] >= MIN_VERDICTS and v["correct"] >= 0.8 * v["total"]:
        out.append(("good", "Administrators confirm this diagnosis (%d of %d correct)" % (v["correct"], v["total"])))
    return out


def summary(conn, days=90):
    """One row per (device, code): episodes, time to resolve, fix results, recurrence, verdicts, open now, evidence level."""
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    by = {}
    for r in conn.execute("SELECT * FROM diagnosis_outcomes WHERE resolved_at>=?", (since,)):
        by.setdefault((r["device"], r["code"]), []).append(dict(r))
    for r in conn.execute("SELECT device, code, COUNT(*) n, COUNT(DISTINCT csp_id) c FROM device_issues WHERE status='OPEN' GROUP BY device, code"):
        by.setdefault((r["device"], r["code"]), [])
    open_now = {(r["device"], r["code"]): (r["n"], r["c"]) for r in conn.execute(
        "SELECT device, code, COUNT(*) n, COUNT(DISTINCT csp_id) c FROM device_issues WHERE status='OPEN' GROUP BY device, code")}
    votes = {}
    for r in conn.execute("SELECT device, code, verdict, COUNT(*) n FROM diagnosis_feedback GROUP BY device, code, verdict"):
        votes.setdefault((r["device"], r["code"]), {})[r["verdict"]] = r["n"]
        by.setdefault((r["device"], r["code"]), [])
    out = []
    for (dev, code), eps in by.items():
        fixes = [e for e in eps if json.loads(e["actions_json"] or "[]")]
        fixed = [e for e in fixes if e["resolution"] == "after_fix"]
        durations = [e["duration_s"] for e in eps]
        v = votes.get((dev, code), {})
        s = {"device": dev, "code": code, "episodes": len(eps), "csps": len({e["csp_id"] for e in eps}),
             "median_minutes": round(statistics.median(durations) / 60, 1) if durations else None,
             "self_resolved": sum(1 for e in eps if e["resolution"] == "self_resolved"),
             "fix_attempts": len(fixes), "fix_then_resolved": len(fixed), "fix_stuck": sum(1 for e in fixed if not e["recurred"]),
             "recurred": sum(1 for e in eps if e["recurred"]),
             "open_now": open_now.get((dev, code), (0, 0))[0], "open_csps": open_now.get((dev, code), (0, 0))[1],
             "verdicts": {"correct": v.get("correct", 0), "wrong": v.get("wrong", 0), "unclear": v.get("unclear", 0), "total": sum(v.values())}}
        s["evidence"] = "enough" if s["episodes"] >= MIN_EPISODES else "thin"
        s["signals"] = _signals(s)
        out.append(s)
    out.sort(key=lambda s: (-s["episodes"] - s["open_now"], s["device"], s["code"]))
    return out


def prune(conn, now=None):
    cutoff = ((now or datetime.now(timezone.utc)) - timedelta(days=KEEP_DAYS)).isoformat(timespec="seconds")
    conn.execute("DELETE FROM diagnosis_outcomes WHERE resolved_at<?", (cutoff,))
