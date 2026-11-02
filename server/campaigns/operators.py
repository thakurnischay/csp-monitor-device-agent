"""Operators and device ownership for email campaigns.

Operator data is an IMPORTED SNAPSHOT of the CSP Growth Platform's Calling Sheet import (csps.json, optionally with its
human reconciliations). This module never writes anything back to that project, never deletes an operator because a newer
file omits it (it is flagged instead), and never touches campaign history.

Ownership verdicts reuse the email project's own engine, vendored unchanged (campaigns/growth/engine.py), so the four
verdicts mean exactly what they mean there:
    OWNED         they have it
    GAP_VERIFIED  the data says they do not, and nothing contradicts it   <- the ONLY verdict that counts as "absent"
    GAP_DISPUTED  the data says no but other evidence says yes            -> flagged for review
    UNKNOWN       the data has no opinion                                 -> flagged for review

The email project models only the micro-ATM and the printer. This module adds, without changing those rules:
    GPS        from the sheet's `claimsGps` field (a claim only: there is no activity data to cross-check it)
    BIOMETRIC  no field exists in the sheet: UNKNOWN unless the monitoring agent or a human says otherwise
and uses the monitoring agent as a second witness: a device the agent has CONFIGURED contradicts a "no" in the sheet
(-> GAP_DISPUTED, review) and turns an UNKNOWN into OWNED. A human reconciliation always wins over everything.
"""
import json
import re
from datetime import datetime, timezone

from .growth import engine

DEVICES = ("MATM", "PRINTER", "GPS", "BIOMETRIC")
DEVICE_LABELS = {"MATM": "Micro-ATM", "PRINTER": "Passbook printer", "GPS": "GPS dongle", "BIOMETRIC": "Fingerprint scanner"}
SENTENCE_LABELS = {"MATM": "Micro-ATM", "PRINTER": "passbook printer", "GPS": "GPS dongle", "BIOMETRIC": "fingerprint scanner"}   # mid-sentence
AGENT_PREFIX = {"MATM": "microatm", "PRINTER": "printer", "GPS": "gps", "BIOMETRIC": "biometric"}
VERDICTS = (engine.OWNED, engine.GAP_VERIFIED, engine.GAP_DISPUTED, engine.UNKNOWN)

# What the ownership rules read from a sheet row. A row without these keys is malformed and is skipped, not guessed at.
_REQUIRED_NUMERIC = ("matmTxnPrev", "matmTxnThis", "pbPrintsPrev", "pbPrintsThis", "txnPrev", "txnThis")
_ENGINE_KEYS = ("claimsMatm", "claimsPrinter", "claimsGps", "terminalStatus", "inactivityDays") + _REQUIRED_NUMERIC
_EMAIL_MAX = 254


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalize_code(value):
    """The shared operator identity: the CSP code, upper-case, no spaces."""
    return re.sub(r"\s+", "", str(value or "")).upper()


def _text(value, n=200):
    return " ".join(str(value if value is not None else "").split())[:n]


def _number(value):
    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        return value
    try:
        return float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return 0


def _claim(value):
    return value if isinstance(value, bool) else None


def parse_payload(payload):
    """Accept the email project's csps.json (a dict with a 'csps' list) or a bare list. -> (rows, source_stamp, problems)."""
    if isinstance(payload, dict):
        rows, stamp = payload.get("csps"), _text(payload.get("importedAt"), 40)
    else:
        rows, stamp = payload, ""
    if not isinstance(rows, list):
        raise ValueError("That file does not look like the CSP Growth Platform's csps.json (no list of operators found).")
    return rows, stamp


def parse_reconciliations(payload):
    """The email project's state.json['reconciliations']: {"CODE:PRODUCT": {ownsDevice, by, at, note}}. Also accepts the
    whole state.json. -> {(code, device): {...}}"""
    if isinstance(payload, dict) and isinstance(payload.get("reconciliations"), dict):
        payload = payload["reconciliations"]
    out = {}
    if isinstance(payload, dict):
        for key, rec in payload.items():
            code, _, device = str(key).partition(":")
            device = device.upper()
            if device in DEVICES and isinstance(rec, dict) and "ownsDevice" in rec:
                out[(normalize_code(code), device)] = rec
    return out


# ------------------------------------------------------------------------------------------------------ import
def import_operators(conn, payload, actor, source_name="", reconciliations=None):
    """Upsert the operators by CSP code. Returns a report. Never deletes; operators absent from this file are kept and
    marked present_in_latest=0 (they are never emailed, and their campaign history is untouched)."""
    rows, stamp = parse_payload(payload)
    now = _now()
    seen_codes, duplicates = {}, set()
    for r in rows:
        code = normalize_code(r.get("cspCode")) if isinstance(r, dict) else ""
        if code:
            if code in seen_codes:
                duplicates.add(code)
            seen_codes[code] = True
    accepted, skipped = [], []
    for r in rows:
        if not isinstance(r, dict):
            skipped.append(("?", "not a record"))
            continue
        code = normalize_code(r.get("cspCode"))
        if not code:
            skipped.append(("?", "no CSP code"))
        elif code in duplicates:
            skipped.append((code, "the same CSP code appears more than once in the file (identity is ambiguous)"))
        elif any(k not in r for k in _REQUIRED_NUMERIC):
            skipped.append((code, "missing transaction/print counts needed by the ownership rules"))
        else:
            accepted.append((code, r))
    cur = conn.execute("INSERT INTO operator_imports (imported_at, imported_by, source_name, source_stamp, rows_total, "
                       "rows_accepted, rows_skipped) VALUES (?,?,?,?,?,?,?)",
                       (now, actor, _text(source_name, 120), stamp, len(rows), len(accepted), len(skipped)))
    import_id = cur.lastrowid
    conn.execute("UPDATE campaign_operators SET present_in_latest=0")
    for code, r in accepted:
        engine_input = {k: (r.get(k) if k in ("terminalStatus",) else
                            _claim(r.get(k)) if k.startswith("claims") else
                            (r.get(k) if k == "inactivityDays" and r.get(k) is not None else _number(r.get(k))))
                        for k in _ENGINE_KEYS}
        if not isinstance(engine_input.get("inactivityDays"), (int, float)):
            engine_input["inactivityDays"] = None
        activity = engine.classify_activity(dict(engine_input, cspCode=code))
        email = _text(r.get("email"), _EMAIL_MAX)
        existing = conn.execute("SELECT first_imported_at FROM campaign_operators WHERE csp_code=?", (code,)).fetchone()
        conn.execute(
            """INSERT INTO campaign_operators (csp_code, name, email, mobile, rm, state, district, activity, activity_reason,
                   contactable, engine_input_json, present_in_latest, first_imported_at, updated_at, last_import_id)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,1,?,?,?)
               ON CONFLICT(csp_code) DO UPDATE SET name=excluded.name, email=excluded.email, mobile=excluded.mobile,
                   rm=excluded.rm, state=excluded.state, district=excluded.district, activity=excluded.activity,
                   activity_reason=excluded.activity_reason, contactable=excluded.contactable,
                   engine_input_json=excluded.engine_input_json, present_in_latest=1, updated_at=excluded.updated_at,
                   last_import_id=excluded.last_import_id""",
            (code, _text(r.get("name")), email, _text(r.get("mobile"), 30), _text(r.get("rm")), _text(r.get("state")),
             _text(r.get("district")), activity["status"], activity["reason"][:200], 1 if r.get("contactable") else 0,
             json.dumps(engine_input), existing["first_imported_at"] if existing else now, now, import_id))
    if reconciliations:
        for (code, device), rec in parse_reconciliations(reconciliations).items():
            conn.execute(
                "INSERT INTO operator_reconciliations (csp_code, device, owns_device, decided_by, decided_at, note) VALUES (?,?,?,?,?,?) "
                "ON CONFLICT(csp_code, device) DO UPDATE SET owns_device=excluded.owns_device, decided_by=excluded.decided_by, "
                "decided_at=excluded.decided_at, note=excluded.note",
                (code, device, 1 if rec.get("ownsDevice") else 0, _text(rec.get("by"), 60), _text(rec.get("at"), 40), _text(rec.get("note"), 200)))
    refreshed = refresh_ownership(conn, now)
    summary = {"accepted": len(accepted), "skipped": skipped[:50], "skipped_total": len(skipped),
               "no_longer_present": conn.execute("SELECT COUNT(*) c FROM campaign_operators WHERE present_in_latest=0").fetchone()["c"],
               "ownership": refreshed}
    conn.execute("UPDATE operator_imports SET summary_json=? WHERE id=?", (json.dumps(summary), import_id))
    return dict(summary, import_id=import_id, total=len(rows))


# ------------------------------------------------------------------------------------------------ ownership
def _recon_dict(conn):
    return {(r["csp_code"], r["device"]): {"ownsDevice": bool(r["owns_device"]), "by": r["decided_by"] or "someone",
                                          "at": r["decided_at"] or "", "note": r["note"] or ""}
            for r in conn.execute("SELECT * FROM operator_reconciliations").fetchall()}


def _claim_only(claim, label, recon):
    """GPS: the sheet carries a claim and nothing to cross-check it with."""
    override = engine._apply_reconciliation(recon)
    if override:
        return override
    if claim is True:
        return {"verdict": engine.OWNED, "evidence": "Marked Yes in the Calling Sheet"}
    if claim is False:
        return {"verdict": engine.GAP_VERIFIED, "evidence": 'Marked "No" in the Calling Sheet (a claim only: no activity data to cross-check it)'}
    return {"verdict": engine.UNKNOWN, "evidence": f"The Calling Sheet has no {label} status for this operator"}


def _agent_overlay(device, verdict, agent_row, has_human_decision):
    """The monitoring agent as a second witness. A human decision is never overridden."""
    if has_human_decision or not agent_row:
        return verdict, verdict.get("source", "calling_sheet")
    prefix = AGENT_PREFIX[device]
    try:
        configured = bool(agent_row[prefix + "_configured"])
    except (KeyError, IndexError):
        configured = False
    if not configured:
        return verdict, "calling_sheet"
    label = DEVICE_LABELS[device].lower()
    v = verdict["verdict"]
    if v == engine.OWNED:
        return dict(verdict, evidence=verdict["evidence"] + f"; also configured on the monitoring agent"), "calling_sheet"
    if v == engine.UNKNOWN:
        return {"verdict": engine.OWNED,
                "evidence": f"No status in the sheet, but the monitoring agent has a {label} configured on this operator's PC"}, "monitoring_agent"
    return {"verdict": engine.GAP_DISPUTED,
            "evidence": f"The sheet says no {label}, but the monitoring agent has one configured on this operator's PC - conflicting, needs review"}, "monitoring_agent"


def assess_all(engine_input, code, recons, agent_row):
    """{device: (verdict, evidence, source)} for one operator."""
    csp = dict(engine_input, cspCode=code)
    base = {
        "MATM": engine.assess_matm(csp, recons.get((code, "MATM"))),
        "PRINTER": engine.assess_printer(csp, recons.get((code, "PRINTER"))),
        "GPS": _claim_only(csp.get("claimsGps"), "GPS", recons.get((code, "GPS"))),
        "BIOMETRIC": _claim_only(None, "fingerprint scanner", recons.get((code, "BIOMETRIC"))),
    }
    out = {}
    for device in DEVICES:
        human = (code, device) in recons
        v, source = _agent_overlay(device, base[device], agent_row, human)
        if human:
            source = "reconciliation"
        out[device] = (v["verdict"], v["evidence"][:300], source)
    return out


def refresh_ownership(conn, now=None):
    """Recompute every operator's device verdicts (after an import, a reconciliation, or fresh agent data).
    Returns {device: {verdict: count}} over operators present in the latest import."""
    now = now or _now()
    recons = _recon_dict(conn)
    agents = {normalize_code(r["csp_id"]): r for r in conn.execute("SELECT * FROM csps").fetchall()}
    for op in conn.execute("SELECT csp_code, engine_input_json FROM campaign_operators").fetchall():
        code = op["csp_code"]
        try:
            engine_input = json.loads(op["engine_input_json"] or "{}")
        except ValueError:
            continue
        for device, (verdict, evidence, source) in assess_all(engine_input, code, recons, agents.get(code)).items():
            conn.execute(
                "INSERT INTO operator_devices (csp_code, device, verdict, evidence, source, updated_at) VALUES (?,?,?,?,?,?) "
                "ON CONFLICT(csp_code, device) DO UPDATE SET verdict=excluded.verdict, evidence=excluded.evidence, "
                "source=excluded.source, updated_at=excluded.updated_at", (code, device, verdict, evidence, source, now))
    return ownership_summary(conn)


def ownership_summary(conn):
    out = {d: {v: 0 for v in VERDICTS} for d in DEVICES}
    for r in conn.execute("SELECT d.device, d.verdict, COUNT(*) n FROM operator_devices d JOIN campaign_operators o ON o.csp_code=d.csp_code "
                          "WHERE o.present_in_latest=1 GROUP BY d.device, d.verdict").fetchall():
        if r["device"] in out and r["verdict"] in out[r["device"]]:
            out[r["device"]][r["verdict"]] = r["n"]
    return out


def device_map(conn, codes=None):
    """{csp_code: {device: {verdict, evidence, source}}}"""
    out = {}
    for r in conn.execute("SELECT * FROM operator_devices").fetchall():
        if codes is None or r["csp_code"] in codes:
            out.setdefault(r["csp_code"], {})[r["device"]] = {"verdict": r["verdict"], "evidence": r["evidence"], "source": r["source"]}
    return out


def set_reconciliation(conn, code, device, owns_device, actor, note=""):
    """A human decision (e.g. after phoning the operator). Beats every inferred signal; recomputes ownership."""
    code, device = normalize_code(code), str(device).upper()
    if device not in DEVICES:
        raise ValueError("Unknown device.")
    conn.execute(
        "INSERT INTO operator_reconciliations (csp_code, device, owns_device, decided_by, decided_at, note) VALUES (?,?,?,?,?,?) "
        "ON CONFLICT(csp_code, device) DO UPDATE SET owns_device=excluded.owns_device, decided_by=excluded.decided_by, "
        "decided_at=excluded.decided_at, note=excluded.note",
        (code, device, 1 if owns_device else 0, _text(actor, 60), _now(), _text(note, 200)))
    refresh_ownership(conn)
