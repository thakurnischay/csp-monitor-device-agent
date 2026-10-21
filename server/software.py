"""Micro-ATM vendor software: required versions (set by an administrator) and the verdict for each CSP.

The agent only reports FACTS for each program: installed or not, its version, whether it is open and how often it crashed.
The server decides what is acceptable, so when a new version is released the administrator changes ONE number here and every
CSP is re-judged on its next report - no agent has to be updated.

Verdicts (per program, per CSP):
    ok          installed and at least the required version
    outdated    installed but older than the required version
    missing     the PC has an Ingenico micro-ATM but the software is not installed
    unknown     installed, but its version could not be read (informational, never an alarm)
    not_needed  this PC does not use that software (e.g. a micro-ATM of another make), or no requirement is active
"""
import re
from datetime import datetime, timezone

from events import record_audit

# The programs the agent knows how to read. The administrator can set the required version for each of these.
KNOWN = {"llt": "Ingenico LLT", "usb_drivers": "Ingenico USB Drivers"}
# Initial values, taken from the SBI "Move 2500 LLT USB" package (LLT 5.2.4, Ingenico USB Drivers 3.12). Editable on the page.
SEED = {"llt": ("5.2.4", "Initial value from the supplied Move 2500 LLT USB package. Change it when a newer version is released."),
        "usb_drivers": ("3.12", "Initial value from the supplied Move 2500 LLT USB package. Change it when a newer version is released.")}
VERDICT_LABELS = {"ok": ("Up to date", "green"), "outdated": ("Out of date", "amber"), "missing": ("Not installed", "red"),
                  "unknown": ("Version unknown", "gray"), "not_needed": ("Not needed", "gray")}
_VERSION = re.compile(r"\d+(?:\.\d+){0,5}")


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_version(text):
    """'5.2.4' / 'v5.2.4' / 'LLT 5.2.4' / '3.12.0.0' -> (5, 2, 4). None if there is no number to compare."""
    m = _VERSION.search(str(text or ""))
    return tuple(int(x) for x in m.group(0).split(".")) if m else None


def compare(installed, required):
    """-1 installed older, 0 same, 1 newer, None if either cannot be read. Missing parts count as 0 (3.12 == 3.12.0.0)."""
    a, b = parse_version(installed), parse_version(required)
    if a is None or b is None:
        return None
    n = max(len(a), len(b))
    a, b = a + (0,) * (n - len(a)), b + (0,) * (n - len(b))
    return (a > b) - (a < b)


def valid_version(text):
    return bool(re.fullmatch(r"\d+(\.\d+){0,5}", str(text or "").strip()))


# -------------------------------------------------------------------------------------------- requirements
def seed_defaults(conn):
    """Insert the initial requirements once (only into an empty table; rows are never deleted)."""
    if conn.execute("SELECT COUNT(*) c FROM software_requirements").fetchone()["c"]:
        return
    for key, (ver, note) in SEED.items():
        conn.execute("INSERT INTO software_requirements (key, name, min_version, note, active, updated_by, updated_at) "
                     "VALUES (?,?,?,?,1,'system',?)", (key, KNOWN[key], ver, note, _now()))


def requirements(conn):
    """{key: row-dict}."""
    return {r["key"]: dict(r) for r in conn.execute("SELECT * FROM software_requirements").fetchall()}


class SoftwareError(ValueError):
    """A reason an admin action was refused, safe to show."""


def set_requirement(conn, key, min_version, note, active, actor):
    if key not in KNOWN:
        raise SoftwareError("Unknown software.")
    min_version = str(min_version or "").strip()
    if not valid_version(min_version):
        raise SoftwareError("Enter the required version as numbers separated by dots, for example 5.2.4.")
    note = " ".join(str(note or "").split())[:200]
    old = conn.execute("SELECT min_version, active FROM software_requirements WHERE key=?", (key,)).fetchone()
    if old:
        conn.execute("UPDATE software_requirements SET min_version=?, note=?, active=?, updated_by=?, updated_at=? WHERE key=?",
                     (min_version, note, 1 if active else 0, actor, _now(), key))
    else:
        conn.execute("INSERT INTO software_requirements (key, name, min_version, note, active, updated_by, updated_at) "
                     "VALUES (?,?,?,?,?,?,?)", (key, KNOWN[key], min_version, note, 1 if active else 0, actor, _now()))
    changed = "" if not old else " (was %s%s)" % (old["min_version"], "" if old["active"] else ", inactive")
    record_audit(conn, actor, "software_requirement_set", "",
                 "%s: required %s%s%s" % (KNOWN[key], min_version, "" if active else " [inactive]", changed))


# ------------------------------------------------------------------------------------------------- judgement
def clean_inventory(value):
    """Whitelist what an agent reported about the software. Anything unexpected is dropped."""
    out = []
    if not isinstance(value, list):
        return out
    for p in value[:6]:
        if not isinstance(p, dict) or p.get("key") not in KNOWN:
            continue
        def n(x, k):
            try:
                return max(0, min(int(x), k))
            except (TypeError, ValueError):
                return 0
        running = p.get("running")
        out.append({"key": p["key"], "name": KNOWN[p["key"]], "installed": bool(p.get("installed")),
                    "version": " ".join(str(p.get("version") or "").split())[:40],
                    "source": " ".join(str(p.get("source") or "").split())[:40],
                    "running": running if isinstance(running, bool) else None, "expected": bool(p.get("expected")),
                    "crashes_7d": n(p.get("crashes_7d"), 999), "crashes_24h": n(p.get("crashes_24h"), 999),
                    "last_crash_at": " ".join(str(p.get("last_crash_at") or "").split())[:40] or None})
    return out


def verdict(item, req):
    """-> (verdict, required_version). `req` is a requirements row or None."""
    if not req or not req.get("active") or not item.get("expected"):
        return "not_needed", (req or {}).get("min_version", "")
    required = req["min_version"]
    if not item.get("installed"):
        return "missing", required
    c = compare(item.get("version"), required)
    if c is None:
        return "unknown", required
    return ("outdated" if c < 0 else "ok"), required


def annotate(inventory, reqs):
    """Add the required version and the verdict to each item (for storing and for display)."""
    out = []
    for item in inventory:
        v, required = verdict(item, reqs.get(item["key"]))
        out.append(dict(item, required=required, verdict=v))
    return out


def findings_for(annotated):
    """Diagnosis findings (same shape the agent's own findings have) for programs that are missing or out of date."""
    findings = []
    for p in annotated:
        name, key = p["name"], p["key"].upper()
        if p["verdict"] == "missing":
            findings.append({
                "code": "SOFTWARE_MISSING_" + key, "state": "SOFTWARE_ISSUE", "severity": "high", "confidence": "confirmed",
                "title": "The micro-ATM software (%s) is not installed" % name,
                "symptom": "This PC has an Ingenico micro-ATM but %s is not installed, so micro-ATM transactions cannot work properly." % name,
                "root_cause": "Windows has no record of %s on this PC (it is not in the installed programs and its program file was not found)." % name,
                "category": "software",
                "evidence": ["%s was not found on this PC" % name, "Required: %s %s or newer" % (name, p["required"])],
                "actions": ["Install %s from the official SBI / Ingenico package supplied to the CSP (not from copies of unknown origin)." % name,
                            "Restart the PC after installing, then check this page again.",
                            "Ask the support team for the package if the CSP does not have it."],
                "operator_action": True, "auto_fix": None})
        elif p["verdict"] == "outdated":
            findings.append({
                "code": "SOFTWARE_OUTDATED_" + key, "state": "SOFTWARE_ISSUE", "severity": "medium", "confidence": "confirmed",
                "title": "The micro-ATM software (%s) is out of date (installed %s, required %s)" % (name, p["version"], p["required"]),
                "symptom": "%s is older than the version required for this CSP." % name,
                "root_cause": "A newer version has been released and this PC still has the old one.",
                "category": "software",
                "evidence": ["Installed version: %s" % p["version"], "Required version: %s or newer" % p["required"],
                             "Version read from: %s" % (p["source"] or "this PC")],
                "actions": ["Install the latest %s from the official SBI / Ingenico package (install over the old one)." % name,
                            "Close the program before installing and restart the PC afterwards.",
                            "Do not change anything in the middle of a customer's transaction."],
                "operator_action": True, "auto_fix": None})
    return findings


# --------------------------------------------------------------------------------------------- fleet view
def fleet(conn):
    """One row per CSP that reported micro-ATM software, worst first, plus totals. Judged NOW against the current
    requirements (so changing a required version updates this page immediately, before agents report again)."""
    import json
    reqs = requirements(conn)
    rows, totals = [], {"ok": 0, "outdated": 0, "missing": 0, "unknown": 0, "crashing": 0}
    names = {r["csp_id"]: r["name"] for r in conn.execute("SELECT csp_id, name FROM csps").fetchall()}
    for r in conn.execute("SELECT csp_id, detail_json, updated_at FROM device_diagnosis WHERE device='microatm'").fetchall():
        try:
            inv = clean_inventory((json.loads(r["detail_json"] or "{}") or {}).get("software"))
        except ValueError:
            continue
        if not inv:
            continue
        items = {p["key"]: p for p in annotate(inv, reqs)}
        verdicts = [p["verdict"] for p in items.values()]
        crashes = sum(p["crashes_24h"] for p in items.values())
        worst = ("missing" if "missing" in verdicts else "outdated" if "outdated" in verdicts else
                 "unknown" if "unknown" in verdicts else "ok" if "ok" in verdicts else "not_needed")
        if worst in totals:
            totals[worst] += 1
        if crashes:
            totals["crashing"] += 1
        rank = {"missing": 0, "outdated": 1, "unknown": 2, "ok": 3, "not_needed": 4}[worst]
        rows.append({"csp_id": r["csp_id"], "name": names.get(r["csp_id"], ""), "items": items, "worst": worst,
                     "rank": rank, "crashes_24h": crashes, "updated_at": r["updated_at"]})
    rows.sort(key=lambda x: (x["rank"], -x["crashes_24h"], x["csp_id"]))
    return rows, totals
