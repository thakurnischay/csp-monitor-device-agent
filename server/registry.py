"""CSP registry and secure agent enrollment.

The problem: a CSP operator should only have to type their CSP name and CSP code - but a code and a name
are not secrets (they appear on screens and in screenshots), so they can never be enough to be handed a
credential. The design:

  1. The administrator's registry (plus every CSP that already has a key) defines which codes are real
     and what each CSP is officially called.
  2. The agent generates its OWN random API key on the CSP's PC and sends it with the enrollment request.
     The server stores only the hash - a key is never sent TO the PC, never shown, never typed.
  3. The request waits as PENDING. It becomes usable only when an administrator approves it in the
     dashboard (audited) - or when the administrator pre-approved that CSP in the registry and the name
     matches exactly. Until then the key does not authenticate anything.
  4. Nothing existing is overwritten: an enrollment for a CSP that ALREADY has an agent is flagged and
     needs an explicit "replace" confirmation; its monitoring history (`csps`, events, issues) is never touched.
"""
import os
import re
import unicodedata
from datetime import datetime, timezone

from events import record_audit
from security import hash_api_key, key_suffix

CODE_RE = re.compile(r"^[A-Za-z0-9_-]{3,40}$")
KEY_RE = re.compile(r"^[A-Za-z0-9_-]{30,64}$")
MAX_PENDING = 500


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalize_name(name):
    """Casefolded, whitespace-collapsed, no accents/punctuation noise - so 'Sharma  Digital Seva Kendra.' matches
    'sharma digital seva kendra'. Still strict: a different word or spelling does not match."""
    s = unicodedata.normalize("NFKC", str(name or "")).casefold()
    s = re.sub(r"[^\w\s&-]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def lookup(conn, csp_id):
    """What the server knows about a CSP code: registered name, whether it is known at all, whether it already has an agent."""
    reg = conn.execute("SELECT name, pre_approved FROM csp_registry WHERE csp_id=?", (csp_id,)).fetchone()
    key = conn.execute("SELECT name, active FROM api_keys WHERE csp_id=?", (csp_id,)).fetchone()
    name = None
    if reg and (reg["name"] or "").strip():
        name = reg["name"].strip()
    elif key and (key["name"] or "").strip():
        name = key["name"].strip()
    return {"known": bool(reg or key), "name": name, "has_agent": bool(key and key["active"]),
            "has_key_row": bool(key), "pre_approved": bool(reg and reg["pre_approved"])}


def verify(conn, csp_id, name):
    """-> 'ok' | 'invalid' | 'unknown_code' | 'name_mismatch' | 'no_name_on_record'."""
    csp_id, name = str(csp_id or "").strip(), " ".join(str(name or "").split())
    if not CODE_RE.match(csp_id) or not (2 <= len(name) <= 80):
        return "invalid"
    info = lookup(conn, csp_id)
    if not info["known"]:
        return "unknown_code"
    if info["name"] is None:
        return "no_name_on_record"
    return "ok" if normalize_name(name) == normalize_name(info["name"]) else "name_mismatch"


MESSAGES = {
    "ok": "Verified.",
    "invalid": "Please enter your CSP name and your CSP code.",
    "unknown_code": "This CSP code was not found. Please check it, or contact your administrator.",
    "name_mismatch": "The CSP name does not match this CSP code. Please check the spelling, or contact your administrator.",
    "no_name_on_record": "Verified. Your administrator will confirm your name when approving.",
}


def _approve_row(conn, row, actor, label=None):
    """Make the enrollment's key the CSP's API key. Touches api_keys (+ registry if missing) only - never `csps` history."""
    info = lookup(conn, row["csp_id"])
    label = (label or info["name"] or row["requested_name"]).strip()
    conn.execute(
        """INSERT INTO api_keys (csp_id, api_key_hash, api_key_suffix, name, active, created_at)
           VALUES (?,?,?,?,1,?)
           ON CONFLICT(csp_id) DO UPDATE SET api_key_hash=excluded.api_key_hash,
               api_key_suffix=excluded.api_key_suffix, active=1, created_at=excluded.created_at,
               name=COALESCE(NULLIF(api_keys.name, ''), excluded.name)""",
        (row["csp_id"], row["key_hash"], row["key_suffix"], label, _now()))
    conn.execute("INSERT OR IGNORE INTO csp_registry (csp_id, name, pre_approved, created_at) VALUES (?,?,0,?)",
                 (row["csp_id"], label, _now()))
    conn.execute("UPDATE enrollments SET status='APPROVED', decided_at=?, decided_by=? WHERE id=?",
                 (_now(), actor, row["id"]))
    conn.execute("UPDATE enrollments SET status='SUPERSEDED', decided_at=? WHERE csp_id=? AND status='PENDING' AND id<>?",
                 (_now(), row["csp_id"], row["id"]))
    record_audit(conn, actor, "enrollment_approved", row["csp_id"],
                 "replaced an existing agent's key" if row["replaces_existing"] else "new agent")


def request_enrollment(conn, key, csp_id, name, hostname="", machine_id="", agent_version="", remote_addr=""):
    """-> (http_status, body). Idempotent: repeating the same request returns its current status."""
    csp_id, name = str(csp_id or "").strip(), " ".join(str(name or "").split())
    if not KEY_RE.match(str(key or "")):
        return 400, {"ok": False, "status": "invalid", "message": "The agent did not present a valid key."}
    verdict = verify(conn, csp_id, name)
    if verdict in ("invalid", "unknown_code", "name_mismatch"):
        return (404 if verdict == "unknown_code" else 400), {"ok": False, "status": verdict, "message": MESSAGES[verdict]}
    kh = hash_api_key(key)
    existing = conn.execute("SELECT * FROM enrollments WHERE key_hash=?", (kh,)).fetchone()
    if existing:
        if existing["csp_id"] != csp_id:
            return 409, {"ok": False, "status": "conflict", "message": "This key is already used for another CSP."}
        return 200, {"ok": True, "status": existing["status"].lower(), "request_id": existing["id"]}
    if conn.execute("SELECT COUNT(*) c FROM enrollments WHERE status='PENDING'").fetchone()["c"] >= MAX_PENDING:
        return 429, {"ok": False, "status": "busy", "message": "Too many requests are waiting. Please try again later."}
    info = lookup(conn, csp_id)
    conn.execute("UPDATE enrollments SET status='SUPERSEDED', decided_at=? WHERE csp_id=? AND status='PENDING'",
                 (_now(), csp_id))
    cur = conn.execute(
        """INSERT INTO enrollments (csp_id, requested_name, registry_name, hostname, machine_id, agent_version,
                                    remote_addr, key_hash, key_suffix, replaces_existing, status, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?, 'PENDING', ?)""",
        (csp_id, name, info["name"], str(hostname or "")[:100], str(machine_id or "")[:80], str(agent_version or "")[:40],
         str(remote_addr or "")[:60], kh, key_suffix(key), 1 if info["has_agent"] else 0, _now()))
    row = conn.execute("SELECT * FROM enrollments WHERE id=?", (cur.lastrowid,)).fetchone()
    record_audit(conn, "agent", "enrollment_requested", csp_id, "%s from %s" % (name[:60], (hostname or "unknown")[:40]))
    # administrator pre-approval: only for a brand-new agent whose name matches exactly
    if info["pre_approved"] and verdict == "ok" and not info["has_agent"]:
        _approve_row(conn, row, "pre-approval")
        return 200, {"ok": True, "status": "approved", "request_id": row["id"]}
    return 200, {"ok": True, "status": "pending", "request_id": row["id"]}


def status_for(conn, key, csp_id):
    """-> (http_status, body). Only the holder of the key that made the request can ask."""
    row = conn.execute("SELECT * FROM enrollments WHERE key_hash=? AND csp_id=?",
                       (hash_api_key(str(key or "")), str(csp_id or "").strip())).fetchone()
    if not row:
        return 404, {"ok": False, "status": "unknown"}
    status = row["status"].lower()
    if status == "approved":
        live = conn.execute("SELECT api_key_hash, active FROM api_keys WHERE csp_id=?", (row["csp_id"],)).fetchone()
        if not live or live["api_key_hash"] != row["key_hash"] or not live["active"]:
            status = "superseded"          # a later approval replaced this key
    return 200, {"ok": True, "status": status}


def approve(conn, enrollment_id, actor, confirm_replace=False, label=None):
    """-> (ok, message)."""
    row = conn.execute("SELECT * FROM enrollments WHERE id=?", (enrollment_id,)).fetchone()
    if not row or row["status"] != "PENDING":
        return False, "That request is no longer pending."
    if row["replaces_existing"] and not confirm_replace:
        return False, ("This CSP already has an agent. Approving will replace its key and the old PC will stop reporting. "
                       "Tick the confirmation box if that is what you want.")
    _approve_row(conn, row, actor, label)
    return True, "Approved %s." % row["csp_id"]


def reject(conn, enrollment_id, actor, note=""):
    row = conn.execute("SELECT * FROM enrollments WHERE id=?", (enrollment_id,)).fetchone()
    if not row or row["status"] != "PENDING":
        return False, "That request is no longer pending."
    conn.execute("UPDATE enrollments SET status='REJECTED', decided_at=?, decided_by=?, note=? WHERE id=?",
                 (_now(), actor, str(note or "")[:200], enrollment_id))
    record_audit(conn, actor, "enrollment_rejected", row["csp_id"], str(note or "")[:200])
    return True, "Rejected %s." % row["csp_id"]


def approve_matching(conn, actor):
    """Approve every pending request that is for a NEW agent and whose name matches the record exactly."""
    n = 0
    for row in conn.execute("SELECT * FROM enrollments WHERE status='PENDING' AND replaces_existing=0").fetchall():
        info = lookup(conn, row["csp_id"])
        if info["name"] and normalize_name(info["name"]) == normalize_name(row["requested_name"]):
            _approve_row(conn, row, actor)
            n += 1
    return n


def import_registry(conn, text, actor, overwrite=False):
    """Lines of `code,name[,pre_approved]`. Never overwrites an existing CSP's name unless asked. -> (added, updated, problems)."""
    added = updated = 0
    problems = []
    for n, line in enumerate(str(text or "").splitlines(), start=1):
        line = line.strip()
        if not line or line.startswith("#") or line.lower().startswith(("csp_id", "csp code", "code,")):
            continue
        parts = [p.strip().strip('"') for p in line.replace("\t", ",").split(",")]
        if len(parts) < 2 or not CODE_RE.match(parts[0]) or not (2 <= len(parts[1]) <= 80):
            problems.append("line %d: expected code,name" % n)
            continue
        code, name = parts[0], " ".join(parts[1].split())
        pre = 1 if len(parts) > 2 and parts[2].lower() in ("1", "yes", "true", "y") else 0
        row = conn.execute("SELECT name FROM csp_registry WHERE csp_id=?", (code,)).fetchone()
        if not row:
            conn.execute("INSERT INTO csp_registry (csp_id, name, pre_approved, created_at) VALUES (?,?,?,?)",
                         (code, name, pre, _now()))
            added += 1
        elif normalize_name(row["name"]) != normalize_name(name):
            if overwrite:
                conn.execute("UPDATE csp_registry SET name=?, pre_approved=? WHERE csp_id=?", (name, pre, code))
                updated += 1
            else:
                problems.append("line %d: %s is already registered under a different name - not changed" % (n, code))
        elif pre:
            conn.execute("UPDATE csp_registry SET pre_approved=1 WHERE csp_id=?", (code,))
    record_audit(conn, actor, "registry_import", "", "%d added, %d updated, %d problems" % (added, updated, len(problems)))
    return added, updated, problems


def set_pre_approved(conn, csp_id, flag, actor):
    row = conn.execute("SELECT 1 FROM csp_registry WHERE csp_id=?", (csp_id,)).fetchone()
    if not row:
        return False
    conn.execute("UPDATE csp_registry SET pre_approved=? WHERE csp_id=?", (1 if flag else 0, csp_id))
    record_audit(conn, actor, "registry_pre_approve_on" if flag else "registry_pre_approve_off", csp_id)
    return True


def pending_count(conn):
    return conn.execute("SELECT COUNT(*) c FROM enrollments WHERE status='PENDING'").fetchone()["c"]
