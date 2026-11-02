"""Centralized agent updates: signed releases, rollouts, per-CSP deployments.

The server distributes; it cannot forge. Every release arrives signed by the administrator's offline key
and is verified again here before it is stored. The server never connects to a CSP PC - agents discover
their assignment in the heartbeat response and fetch the package themselves (outbound only).

State machine for one deployment (one row per CSP + release, so assigning twice is a no-op):

    pending -> assigned -> downloading -> verifying -> installing -> restarting -> SUCCESSFUL
                    \\___________________________________\\______> failed
                                         installing/restarting -> rollback_in_progress -> rolled_back
    pending/assigned -> cancelled            failed/rolled_back/cancelled -> (admin retry) -> pending (attempt+1)

SUCCESSFUL is reached in exactly one way: the NEW agent version sends a heartbeat carrying a healthy
`update_result` for this deployment AND that same heartbeat's agent_version equals the release version.
"Assigned", "downloaded" or "installed" never count as success.
"""
import base64
import json
import os
from datetime import datetime, timedelta, timezone

import release_manifest as rm
from db import db_path
from events import record_audit, record_event

AGENT_RANK = {"assigned": 1, "downloading": 2, "verifying": 3, "installing": 4, "restarting": 5}
FINAL = {"successful", "failed", "rolled_back", "cancelled"}
IN_PROGRESS = {"downloading", "verifying", "installing", "restarting", "rollback_in_progress"}
STALE_MINUTES = {"downloading": 45, "verifying": 45, "installing": 45, "restarting": 30, "rollback_in_progress": 30}
LABELS = {"pending": "Pending", "assigned": "Assigned", "downloading": "Downloading", "verifying": "Verifying",
          "installing": "Installing", "restarting": "Restarting", "successful": "Successful", "failed": "Failed",
          "rollback_in_progress": "Rollback in progress", "rolled_back": "Rolled back", "cancelled": "Cancelled"}
MAX_UPLOAD_BYTES = 64 * 1024 * 1024


class UpdateError(ValueError):
    """An admin-visible, safe-to-display reason an action was refused."""


def _now():
    return datetime.now(timezone.utc)


def _iso(dt=None):
    return (dt or _now()).isoformat(timespec="seconds")


def _parse(value):
    try:
        dt = datetime.fromisoformat(str(value))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


# ----------------------------------------------------------------------------------------------- keys / storage
def load_public_keys():
    path = os.environ.get("CSP_MONITOR_RELEASE_PUBKEYS") or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "release_public_keys.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"keys": []}


def release_dir():
    d = os.environ.get("CSP_MONITOR_RELEASE_DIR") or os.path.join(os.path.dirname(os.path.abspath(db_path())), "releases")
    os.makedirs(d, exist_ok=True)
    return d


# ------------------------------------------------------------------------------------------------- releases
def add_release(conn, package_bytes, manifest_bytes, signature_b64, actor):
    """Verify (signature + package contents), then store. Raises UpdateError with a plain-language reason."""
    keys = load_public_keys()
    if not keys.get("keys"):
        raise UpdateError("No release-signing public key is installed on the server, so no release can be accepted. "
                          "Put your public key in release_public_keys.json (see UPDATES.md).")
    if len(package_bytes) > MAX_UPLOAD_BYTES:
        raise UpdateError("The package is too large.")
    try:
        manifest = rm.verify_signed_release(manifest_bytes, signature_b64, keys)
        rm.check_package(package_bytes, manifest)
    except rm.ManifestError as e:
        raise UpdateError("Rejected: %s." % str(e).rstrip("."))
    newest = conn.execute("SELECT MAX(sequence) m FROM agent_releases").fetchone()["m"] or 0
    if manifest["sequence"] <= newest:
        raise UpdateError("Rejected: sequence %d is not higher than the newest release (%d). Releases must keep increasing; "
                          "for a rollback use `release_tool.py rollback`, which issues a new, higher sequence."
                          % (manifest["sequence"], newest))
    if conn.execute("SELECT 1 FROM agent_releases WHERE version=?", (manifest["version"],)).fetchone():
        raise UpdateError("Rejected: version %s already exists." % manifest["version"])
    path = os.path.join(release_dir(), "release-%d-%s.zip" % (manifest["sequence"], manifest["package_sha256"][:12]))
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(package_bytes)
    os.replace(tmp, path)
    cur = conn.execute(
        """INSERT INTO agent_releases (version, sequence, kind, notes, key_id, manifest_b64, signature_b64, package_sha256,
                                       package_size, package_path, min_agent_sequence, status, uploaded_by, uploaded_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?, 'UPLOADED', ?, ?)""",
        (manifest["version"], manifest["sequence"], manifest.get("kind", "release"), manifest.get("notes", ""),
         manifest["key_id"], base64.b64encode(manifest_bytes).decode("ascii"), signature_b64.strip(),
         manifest["package_sha256"], manifest["package_size"], path, int(manifest.get("min_agent_sequence", 0)),
         actor, _iso()))
    record_audit(conn, actor, "release_uploaded", "", "%s (sequence %d, %s)" % (manifest["version"], manifest["sequence"],
                                                                                 manifest.get("kind", "release")))
    return cur.lastrowid


def get_release(conn, release_id):
    return conn.execute("SELECT * FROM agent_releases WHERE id=?", (release_id,)).fetchone()


def approve_release(conn, release_id, actor):
    r = get_release(conn, release_id)
    if not r:
        raise UpdateError("Unknown release.")
    if r["status"] != "UPLOADED":
        raise UpdateError("Only an uploaded release can be approved (this one is %s)." % r["status"].lower())
    conn.execute("UPDATE agent_releases SET status='APPROVED', approved_by=?, approved_at=? WHERE id=?",
                 (actor, _iso(), release_id))
    record_audit(conn, actor, "release_approved", "", r["version"])


def revoke_release(conn, release_id, actor):
    """Stops the release being offered. Waiting deployments are cancelled; agents already installing are told to stop."""
    r = get_release(conn, release_id)
    if not r:
        raise UpdateError("Unknown release.")
    if r["status"] == "REVOKED":
        return 0
    conn.execute("UPDATE agent_releases SET status='REVOKED', revoked_by=?, revoked_at=? WHERE id=?",
                 (actor, _iso(), release_id))
    n = conn.execute(
        "UPDATE update_deployments SET status='cancelled', detail='release revoked', updated_at=?, finished_at=? "
        "WHERE release_id=? AND status IN ('pending','assigned')", (_iso(), _iso(), release_id)).rowcount
    record_audit(conn, actor, "release_revoked", "", "%s (%d waiting deployments cancelled)" % (r["version"], n))
    return n


# --------------------------------------------------------------------------------------------- compatibility
def compatibility(csp, release):
    """(ok, reason) for one CSP row against one release row."""
    if not csp["update_capable"]:
        return False, "legacy agent - needs the one-time bootstrap"
    seq = csp["agent_sequence"] or 0
    if seq >= release["sequence"]:
        return False, "already on this release or newer"
    if seq < (release["min_agent_sequence"] or 0):
        return False, "too old to update directly (needs sequence %d first)" % release["min_agent_sequence"]
    return True, ""


def compatible_csp_ids(conn, release_id):
    r = get_release(conn, release_id)
    if not r:
        return []
    out = []
    for c in conn.execute("SELECT csp_id, update_capable, agent_sequence FROM csps ORDER BY csp_id").fetchall():
        ok, _ = compatibility(c, r)
        busy = conn.execute("SELECT 1 FROM update_deployments WHERE csp_id=? AND release_id=?", (c["csp_id"], release_id)).fetchone()
        if ok and not busy:
            out.append(c["csp_id"])
    return out


# ---------------------------------------------------------------------------------------------- rollouts
def create_rollout(conn, release_id, csp_ids, kind, actor, note=""):
    """Create deployments for the chosen CSPs. Idempotent: a CSP that already has this release is skipped.
    -> (rollout_id, created_count, [(csp_id, reason), ...])"""
    r = get_release(conn, release_id)
    if not r:
        raise UpdateError("Unknown release.")
    if r["status"] != "APPROVED":
        raise UpdateError("Approve the release before deploying it.")
    csp_ids = sorted({str(c).strip() for c in csp_ids if str(c).strip()})
    if not csp_ids:
        raise UpdateError("Choose at least one CSP.")
    if kind not in ("pilot", "full", "expansion"):
        kind = "pilot"
    cur = conn.execute("INSERT INTO rollouts (release_id, kind, status, note, created_by, created_at) VALUES (?,?, 'ACTIVE', ?,?,?)",
                       (release_id, kind, str(note or "")[:200], actor, _iso()))
    rid = cur.lastrowid
    created, skipped = _add_deployments(conn, rid, r, csp_ids)
    record_audit(conn, actor, "rollout_created", "", "%s %s -> %d CSPs (%d skipped)" % (kind, r["version"], created, len(skipped)))
    return rid, created, skipped


def _add_deployments(conn, rollout_id, release, csp_ids):
    created, skipped = 0, []
    for csp_id in csp_ids:
        c = conn.execute("SELECT * FROM csps WHERE csp_id=?", (csp_id,)).fetchone()
        if not c:
            skipped.append((csp_id, "unknown CSP (never reported)"))
            continue
        ok, reason = compatibility(c, release)
        if not ok:
            skipped.append((csp_id, reason))
            continue
        if conn.execute("SELECT 1 FROM update_deployments WHERE csp_id=? AND release_id=?", (csp_id, release["id"])).fetchone():
            skipped.append((csp_id, "already has this release assigned"))
            continue
        now = _iso()
        conn.execute("""INSERT INTO update_deployments (rollout_id, release_id, csp_id, status, from_version, created_at, updated_at)
                        VALUES (?,?,?, 'pending', ?, ?, ?)""", (rollout_id, release["id"], csp_id, c["agent_version"], now, now))
        created += 1
    return created, skipped


def expand_rollout(conn, rollout_id, csp_ids, actor, force=False):
    """Add more CSPs to a pilot, but only once the pilot has actually succeeded (and not failed) - unless forced."""
    ro = conn.execute("SELECT * FROM rollouts WHERE id=?", (rollout_id,)).fetchone()
    if not ro or ro["status"] == "CANCELLED":
        raise UpdateError("That rollout is not available.")
    stats = rollout_stats(conn, rollout_id)
    if not force and (stats.get("successful", 0) < 1 or stats.get("failed", 0) or stats.get("rolled_back", 0)):
        raise UpdateError("Expand only after the pilot has at least one verified success and no failures "
                          "(or tick 'expand anyway').")
    r = get_release(conn, ro["release_id"])
    if r["status"] != "APPROVED":
        raise UpdateError("This release is no longer approved.")
    created, skipped = _add_deployments(conn, rollout_id, r, sorted({str(c).strip() for c in csp_ids if str(c).strip()}))
    if ro["kind"] == "pilot":
        conn.execute("UPDATE rollouts SET kind='expansion' WHERE id=?", (rollout_id,))
    record_audit(conn, actor, "rollout_expanded", "", "rollout %d: +%d CSPs%s" % (rollout_id, created, " (forced)" if force else ""))
    return created, skipped


def set_rollout_status(conn, rollout_id, status, actor):
    ro = conn.execute("SELECT * FROM rollouts WHERE id=?", (rollout_id,)).fetchone()
    if not ro:
        raise UpdateError("Unknown rollout.")
    if ro["status"] == "CANCELLED":
        raise UpdateError("A cancelled rollout cannot be changed.")
    if status not in ("ACTIVE", "PAUSED", "CANCELLED"):
        raise UpdateError("Unknown status.")
    conn.execute("UPDATE rollouts SET status=? WHERE id=?", (status, rollout_id))
    n = 0
    if status == "CANCELLED":
        n = conn.execute(
            "UPDATE update_deployments SET status='cancelled', detail='rollout cancelled', updated_at=?, finished_at=? "
            "WHERE rollout_id=? AND status IN ('pending','assigned')", (_iso(), _iso(), rollout_id)).rowcount
    record_audit(conn, actor, {"ACTIVE": "rollout_resumed", "PAUSED": "rollout_paused", "CANCELLED": "rollout_cancelled"}[status],
                 "", "rollout %d (%d waiting deployments cancelled)" % (rollout_id, n))
    return n


def rollout_stats(conn, rollout_id):
    return {r["status"]: r["n"] for r in conn.execute(
        "SELECT status, COUNT(*) n FROM update_deployments WHERE rollout_id=? GROUP BY status", (rollout_id,)).fetchall()}


def retry_deployment(conn, deployment_id, actor):
    d = conn.execute("SELECT * FROM update_deployments WHERE id=?", (deployment_id,)).fetchone()
    if not d:
        raise UpdateError("Unknown deployment.")
    if d["status"] not in ("failed", "rolled_back", "cancelled"):
        raise UpdateError("Only a failed, rolled-back or cancelled deployment can be retried.")
    r = get_release(conn, d["release_id"])
    ro = conn.execute("SELECT status FROM rollouts WHERE id=?", (d["rollout_id"],)).fetchone()
    if r["status"] != "APPROVED":
        raise UpdateError("This release is no longer approved.")
    if ro["status"] == "CANCELLED":
        raise UpdateError("Its rollout was cancelled - create a new rollout instead.")
    conn.execute("UPDATE update_deployments SET status='pending', attempt=attempt+1, detail='retry requested', finished_at=NULL, "
                 "updated_at=? WHERE id=?", (_iso(), deployment_id))
    record_audit(conn, actor, "deployment_retried", d["csp_id"], "release %s, attempt %d" % (r["version"], d["attempt"] + 1))


def cancel_deployment(conn, deployment_id, actor):
    d = conn.execute("SELECT * FROM update_deployments WHERE id=?", (deployment_id,)).fetchone()
    if not d:
        raise UpdateError("Unknown deployment.")
    if d["status"] in FINAL:
        raise UpdateError("That deployment has already finished.")
    # one still in progress is cancelled too: the agent is told "do not proceed" at its next status call
    conn.execute("UPDATE update_deployments SET status='cancelled', detail='cancelled by administrator', updated_at=?, finished_at=? WHERE id=?",
                 (_iso(), _iso(), deployment_id))
    record_audit(conn, actor, "deployment_cancelled", d["csp_id"], "deployment %d" % deployment_id)


# --------------------------------------------------------------------------------- the agent's side of things
def sweep_stale(conn, now=None):
    """Deployments that stopped reporting: re-offer an interrupted download/install, fail a restart that never came back."""
    now = now or _now()
    for d in conn.execute("SELECT * FROM update_deployments WHERE status IN (%s)" % ",".join("'%s'" % s for s in IN_PROGRESS)).fetchall():
        updated = _parse(d["updated_at"])
        if not updated or now - updated < timedelta(minutes=STALE_MINUTES[d["status"]]):
            continue
        if d["status"] in ("restarting", "rollback_in_progress"):
            conn.execute("UPDATE update_deployments SET status='failed', last_error=?, detail=?, updated_at=?, finished_at=? WHERE id=?",
                         ("no health report arrived after the restart", "the agent did not report back after restarting - check the PC",
                          _iso(now), _iso(now), d["id"]))
        else:
            conn.execute("UPDATE update_deployments SET status='assigned', detail='interrupted - offered again', updated_at=? WHERE id=?",
                         (_iso(now), d["id"]))


def assignment_for_heartbeat(conn, csp_id, now=None):
    """What the heartbeat response tells this agent about updates, or None."""
    now = now or _now()
    c = conn.execute("SELECT update_capable, agent_sequence FROM csps WHERE csp_id=?", (csp_id,)).fetchone()
    if not c or not c["update_capable"]:
        return None
    d = conn.execute(
        """SELECT d.*, r.version, r.sequence, r.status rstatus, ro.status rostatus FROM update_deployments d
           JOIN agent_releases r ON r.id=d.release_id JOIN rollouts ro ON ro.id=d.rollout_id
           WHERE d.csp_id=? AND d.status IN ('pending','assigned') ORDER BY d.id LIMIT 1""", (csp_id,)).fetchone()
    if not d or d["rstatus"] != "APPROVED" or d["rostatus"] != "ACTIVE":
        return None
    if (c["agent_sequence"] or 0) >= d["sequence"]:
        return None                                    # already there (e.g. installed by hand): nothing to do
    if d["status"] == "pending":
        conn.execute("UPDATE update_deployments SET status='assigned', assigned_at=?, updated_at=?, detail='offered to the agent' WHERE id=?",
                     (_iso(now), _iso(now), d["id"]))
    return {"deployment_id": d["id"], "attempt": d["attempt"], "version": d["version"], "sequence": d["sequence"]}


def authorized_deployment(conn, csp_id, deployment_id):
    """The deployment row, only if it belongs to this CSP and may still proceed. -> (row_or_None, reason)."""
    try:
        deployment_id = int(deployment_id)
    except (TypeError, ValueError):
        return None, "unknown deployment"
    d = conn.execute(
        """SELECT d.*, r.version, r.sequence, r.status rstatus, r.manifest_b64, r.signature_b64, r.package_path,
                  r.package_sha256, r.package_size, ro.status rostatus
           FROM update_deployments d JOIN agent_releases r ON r.id=d.release_id JOIN rollouts ro ON ro.id=d.rollout_id
           WHERE d.id=? AND d.csp_id=?""", (deployment_id, csp_id)).fetchone()
    if not d:
        return None, "unknown deployment"
    if d["status"] in ("cancelled", "successful", "failed", "rolled_back"):
        return d, "this deployment is %s" % d["status"].replace("_", " ")
    if d["rstatus"] != "APPROVED":
        return d, "the release is no longer approved"
    if d["rostatus"] != "ACTIVE":
        return d, "the rollout is %s" % d["rostatus"].lower()
    return d, ""


def record_status(conn, csp_id, deployment_id, attempt, status, detail=""):
    """The agent reports progress. -> {"ok": bool, "proceed": bool}. Idempotent and forward-only."""
    d, reason = authorized_deployment(conn, csp_id, deployment_id)
    if d is None:
        return {"ok": False, "proceed": False, "error": "unknown deployment"}
    detail = str(detail or "")[:300]
    if int(attempt or 0) != d["attempt"]:
        return {"ok": True, "proceed": False, "note": "stale attempt"}           # an older attempt must not change anything
    cur = d["status"]
    now = _iso()
    if status in AGENT_RANK:
        if cur in FINAL:
            return {"ok": True, "proceed": False}
        if reason:                                                               # paused / revoked / cancelled: stop here
            return {"ok": True, "proceed": False, "note": reason}
        forward = cur == "pending" or (cur in AGENT_RANK and AGENT_RANK[status] >= AGENT_RANK[cur])
        if forward:                                                              # never move backwards (idempotent, out-of-order safe)
            conn.execute("UPDATE update_deployments SET status=?, detail=?, updated_at=? WHERE id=?", (status, detail, now, d["id"]))
        return {"ok": True, "proceed": True}
    if status == "failed":
        if cur in FINAL:
            return {"ok": True, "proceed": False}
        conn.execute("UPDATE update_deployments SET status='failed', detail=?, last_error=?, updated_at=?, finished_at=? WHERE id=?",
                     (detail or "failed", detail, now, now, d["id"]))
        record_event(conn, csp_id, "agent_update_failed", "%s: %s" % (d["version"], detail[:200]))
        return {"ok": True, "proceed": False}
    if status == "rollback_in_progress":
        if cur in FINAL:
            return {"ok": True, "proceed": False}
        conn.execute("UPDATE update_deployments SET status='rollback_in_progress', detail=?, last_error=?, updated_at=? WHERE id=?",
                     (detail or "rolling back", detail, now, d["id"]))
        return {"ok": True, "proceed": False}
    if status == "rolled_back":
        if cur in ("successful", "cancelled"):
            return {"ok": True, "proceed": False}
        conn.execute("UPDATE update_deployments SET status='rolled_back', detail=?, last_error=COALESCE(NULLIF(?, ''), last_error), "
                     "updated_at=?, finished_at=? WHERE id=?", (detail or "rolled back to the previous version", detail, now, now, d["id"]))
        record_event(conn, csp_id, "agent_update_rolled_back", "%s: %s" % (d["version"], detail[:200]))
        return {"ok": True, "proceed": False}
    return {"ok": False, "proceed": False, "error": "unknown status"}


def record_result(conn, csp_id, result, reported_version):
    """The heartbeat carries the outcome of the last update. SUCCESS needs the NEW version in this same heartbeat."""
    if not isinstance(result, dict):
        return
    try:
        deployment_id = int(result.get("deployment_id"))
    except (TypeError, ValueError):
        return
    d = conn.execute(
        """SELECT d.*, r.version rversion FROM update_deployments d JOIN agent_releases r ON r.id=d.release_id
           WHERE d.id=? AND d.csp_id=?""", (deployment_id, csp_id)).fetchone()
    if not d:
        return
    outcome, detail, now = str(result.get("outcome") or ""), str(result.get("detail") or "")[:300], _iso()
    if outcome == "healthy":
        if d["status"] in ("successful",):
            return                                                               # duplicate report: harmless
        if reported_version == d["rversion"] and d["status"] not in ("cancelled",):
            conn.execute("UPDATE update_deployments SET status='successful', detail='installed and verified healthy', last_error=NULL, "
                         "updated_at=?, finished_at=? WHERE id=?", (now, now, d["id"]))
            record_event(conn, csp_id, "agent_updated", "now running %s (verified healthy)" % reported_version)
        else:
            conn.execute("UPDATE update_deployments SET detail=?, updated_at=? WHERE id=?",
                         ("health report received but the agent reports version %s, expected %s" % (reported_version, d["rversion"]), now, d["id"]))
    elif outcome in ("failed", "rolled_back") and d["status"] not in ("successful", "cancelled"):
        record_status(conn, csp_id, deployment_id, d["attempt"], outcome, detail or outcome)


# ------------------------------------------------------------------------------------------------ dashboard
def display_status(deployment_status, csp_state):
    """Offline overrides the label of anything not finished: nothing is happening on a PC that is off."""
    if deployment_status not in FINAL and csp_state == "OFFLINE":
        return "Offline"
    return LABELS.get(deployment_status, deployment_status)
