"""Admin pages: Agent Updates and Enrollments. Same session login + CSRF + audit log as the rest of the dashboard.

Every state-changing action is a POST (CSRF is enforced for all ui_bp POSTs in app.py), goes through
`updates.py` / `registry.py` (which raise UpdateError with a plain-language reason), and is written to the audit log.
"""
import logging

from flask import flash, redirect, render_template, request, session, url_for

import registry
import software
import updates
from db import get_connection
from device_state import derive_csp_state
from routes import login_required, ui_bp

log = logging.getLogger("csp_monitor.routes_updates")

_UPDATE_AUDIT = ("release_%", "rollout_%", "deployment_%", "enrollment_%", "registry_%")


def _admin():
    return session.get("admin_login") or "admin"


@ui_bp.app_context_processor
def _nav_counts():
    """Number shown beside 'Enrollments' in the menu."""
    if not session.get("admin_in"):
        return {}
    try:
        with get_connection() as conn:
            return {"pending_enrollments": registry.pending_count(conn)}
    except Exception:
        return {"pending_enrollments": 0}


# ------------------------------------------------------------------------------------------------ Agent Updates
def _updates_view(conn):
    updates.sweep_stale(conn)
    conn.commit()
    stats = {}
    for r in conn.execute("SELECT release_id, status, COUNT(*) n FROM update_deployments GROUP BY release_id, status").fetchall():
        stats.setdefault(r["release_id"], {})[r["status"]] = r["n"]
    releases = []
    for r in conn.execute("SELECT * FROM agent_releases ORDER BY sequence DESC").fetchall():
        releases.append(dict(r, stats=stats.get(r["id"], {}), total=sum(stats.get(r["id"], {}).values())))
    ro_stats = {}
    for r in conn.execute("SELECT rollout_id, status, COUNT(*) n FROM update_deployments GROUP BY rollout_id, status").fetchall():
        ro_stats.setdefault(r["rollout_id"], {})[r["status"]] = r["n"]
    rollouts = []
    for r in conn.execute("SELECT ro.*, rel.version, rel.kind rkind FROM rollouts ro JOIN agent_releases rel ON rel.id=ro.release_id "
                          "ORDER BY ro.id DESC LIMIT 40").fetchall():
        st = ro_stats.get(r["id"], {})
        rollouts.append(dict(r, stats=st, total=sum(st.values()),
                             can_expand=r["status"] != "CANCELLED" and st.get("successful", 0) >= 1
                             and not st.get("failed", 0) and not st.get("rolled_back", 0)))
    latest = {}
    for d in conn.execute(
            """SELECT d.*, r.version target_version FROM update_deployments d JOIN agent_releases r ON r.id=d.release_id
               WHERE d.id IN (SELECT MAX(id) FROM update_deployments GROUP BY csp_id)""").fetchall():
        latest[d["csp_id"]] = d
    members = {}
    for m in conn.execute("SELECT group_id, csp_id FROM csp_group_members ORDER BY csp_id").fetchall():
        members.setdefault(m["group_id"], []).append(m["csp_id"])
    groups = [dict(g, members=members.get(g["id"], [])) for g in conn.execute("SELECT * FROM csp_groups ORDER BY name").fetchall()]
    rows = []
    for c in conn.execute("SELECT * FROM csps ORDER BY name, csp_id").fetchall():
        state = derive_csp_state(c["last_seen"])
        d = latest.get(c["csp_id"])
        if c["update_capable"]:
            capability = "Ready" + (" (updater %s)" % c["updater_version"] if c["updater_version"] else "")
        else:
            capability = "Legacy agent - needs the one-time bootstrap"
        rows.append({
            "csp_id": c["csp_id"], "name": c["name"] or c["csp_id"], "version": c["agent_version"] or "unknown",
            "sequence": c["agent_sequence"], "capable": bool(c["update_capable"]), "capability": capability,
            "last_seen": c["last_seen"], "state": state, "deployment": d,
            "target": d["target_version"] if d else "", "status": (updates.display_status(d["status"], state) if d else ""),
            "raw_status": d["status"] if d else "", "detail": d["detail"] if d else "",
            "attempted": (d["assigned_at"] or d["updated_at"]) if d else "", "error": d["last_error"] if d else "",
        })
    audit = conn.execute("SELECT * FROM admin_audit_log WHERE " + " OR ".join("action LIKE ?" for _ in _UPDATE_AUDIT)
                         + " ORDER BY id DESC LIMIT 40", _UPDATE_AUDIT).fetchall()
    approved = [r for r in releases if r["status"] == "APPROVED"]
    return dict(releases=releases, rollouts=rollouts, rows=rows, groups=groups, audit=audit, approved=approved,
                labels=updates.LABELS, keys_installed=bool(updates.load_public_keys().get("keys")))


@ui_bp.route("/updates")
@login_required
def agent_updates():
    with get_connection() as conn:
        view = _updates_view(conn)
    return render_template("updates.html", **view)


@ui_bp.route("/updates/upload", methods=["POST"])
@login_required
def updates_upload():
    files = {k: request.files.get(k) for k in ("package", "manifest", "signature")}
    if not all(files.values()) or not all(f.filename for f in files.values()):
        flash("Choose all three files: the package (.zip), the manifest (.manifest.json) and the signature (.manifest.sig).")
        return redirect(url_for("ui.agent_updates"))
    try:
        with get_connection() as conn:
            rid = updates.add_release(conn, files["package"].read(), files["manifest"].read(),
                                      files["signature"].read().decode("ascii", "ignore"), _admin())
            conn.commit()
        flash("Release uploaded and verified. It is NOT deployed yet: approve it, then create a rollout.")
    except updates.UpdateError as e:
        flash(str(e))
    return redirect(url_for("ui.agent_updates"))


def _ids(name):
    return [v for v in request.form.getlist(name) if v.strip()]


@ui_bp.route("/updates/action", methods=["POST"])
@login_required
def updates_action():
    f = request.form
    action, admin = f.get("action", ""), _admin()
    try:
        with get_connection() as conn:
            if action == "approve_release":
                updates.approve_release(conn, int(f["release_id"]), admin)
                flash("Release approved. You can now deploy it.")
            elif action == "revoke_release":
                n = updates.revoke_release(conn, int(f["release_id"]), admin)
                flash("Release revoked: it will not be offered again (%d waiting deployments cancelled)." % n)
            elif action == "create_rollout":
                rid = int(f["release_id"])
                mode = f.get("mode", "selected")
                if mode == "all":
                    csp_ids = updates.compatible_csp_ids(conn, rid)
                elif mode == "group":
                    csp_ids = [m["csp_id"] for m in conn.execute("SELECT csp_id FROM csp_group_members WHERE group_id=?",
                                                                  (int(f.get("group_id") or 0),)).fetchall()]
                else:
                    csp_ids = _ids("csp_ids")
                _, created, skipped = updates.create_rollout(conn, rid, csp_ids, f.get("kind", "pilot"), admin, f.get("note", ""))
                flash("Rollout created: %d CSP(s) will be offered the update at their next heartbeat%s."
                      % (created, ("; %d skipped (%s)" % (len(skipped), "; ".join("%s: %s" % s for s in skipped[:4]))) if skipped else ""))
            elif action == "expand_rollout":
                rid = int(f["rollout_id"])
                ro = conn.execute("SELECT release_id FROM rollouts WHERE id=?", (rid,)).fetchone()
                targets = updates.compatible_csp_ids(conn, ro["release_id"]) if ro else []
                created, skipped = updates.expand_rollout(conn, rid, targets, admin, force=bool(f.get("force")))
                flash("Rollout expanded to %d more CSP(s)." % created)
            elif action in ("pause_rollout", "resume_rollout", "cancel_rollout"):
                status = {"pause_rollout": "PAUSED", "resume_rollout": "ACTIVE", "cancel_rollout": "CANCELLED"}[action]
                n = updates.set_rollout_status(conn, int(f["rollout_id"]), status, admin)
                flash("Rollout %s%s." % (status.lower(), " (%d waiting deployments cancelled)" % n if n else ""))
            elif action == "retry_deployment":
                updates.retry_deployment(conn, int(f["deployment_id"]), admin)
                flash("Retry queued: the CSP will be offered the update again at its next heartbeat.")
            elif action == "cancel_deployment":
                updates.cancel_deployment(conn, int(f["deployment_id"]), admin)
                flash("Deployment cancelled.")
            elif action == "create_group":
                name = " ".join(f.get("name", "").split())[:60]
                if not name:
                    raise updates.UpdateError("Give the group a name.")
                if conn.execute("SELECT 1 FROM csp_groups WHERE name=?", (name,)).fetchone():
                    raise updates.UpdateError("A group with that name already exists.")
                cur = conn.execute("INSERT INTO csp_groups (name, created_at) VALUES (?, ?)", (name, updates._iso()))
                for code in {c.strip() for c in f.get("members", "").replace(",", " ").split() if c.strip()}:
                    if conn.execute("SELECT 1 FROM csps WHERE csp_id=?", (code,)).fetchone():
                        conn.execute("INSERT OR IGNORE INTO csp_group_members (group_id, csp_id) VALUES (?,?)", (cur.lastrowid, code))
                updates.record_audit(conn, admin, "release_group_created", "", name)
                flash("Group '%s' created." % name)
            elif action == "delete_group":
                gid = int(f["group_id"])
                conn.execute("DELETE FROM csp_group_members WHERE group_id=?", (gid,))
                conn.execute("DELETE FROM csp_groups WHERE id=?", (gid,))
                updates.record_audit(conn, admin, "release_group_deleted", "", str(gid))
                flash("Group deleted.")
            else:
                raise updates.UpdateError("Unknown action.")
            conn.commit()
    except updates.UpdateError as e:
        flash(str(e))
    except (KeyError, ValueError):
        flash("That request was incomplete.")
    return redirect(url_for("ui.agent_updates"))


# --------------------------------------------------------------------------------------------------- Enrollments
@ui_bp.route("/enrollments", methods=["GET", "POST"])
@login_required
def enrollments():
    admin = _admin()
    if request.method == "POST":
        f, action = request.form, request.form.get("action", "")
        with get_connection() as conn:
            try:
                if action == "approve":
                    ok, msg = registry.approve(conn, int(f["enrollment_id"]), admin, confirm_replace=bool(f.get("confirm_replace")))
                    flash(msg)
                elif action == "reject":
                    ok, msg = registry.reject(conn, int(f["enrollment_id"]), admin, f.get("note", ""))
                    flash(msg)
                elif action == "approve_matching":
                    flash("Approved %d request(s) whose name matches the record exactly." % registry.approve_matching(conn, admin))
                elif action == "import_registry":
                    added, updated, problems = registry.import_registry(conn, f.get("registry_text", ""), admin, overwrite=bool(f.get("overwrite")))
                    flash("Registry: %d added, %d updated.%s" % (added, updated, (" Notes: " + " | ".join(problems[:4])) if problems else ""))
                elif action in ("pre_approve_on", "pre_approve_off"):
                    registry.set_pre_approved(conn, f.get("csp_id", ""), action.endswith("on"), admin)
                else:
                    flash("Unknown action.")
            except (KeyError, ValueError):
                flash("That request was incomplete.")
            conn.commit()
        return redirect(url_for("ui.enrollments"))
    with get_connection() as conn:
        pending = conn.execute("SELECT * FROM enrollments WHERE status='PENDING' ORDER BY id").fetchall()
        recent = conn.execute("SELECT * FROM enrollments WHERE status<>'PENDING' ORDER BY id DESC LIMIT 25").fetchall()
        reg = conn.execute("SELECT * FROM csp_registry ORDER BY csp_id LIMIT 500").fetchall()
        n_reg = conn.execute("SELECT COUNT(*) c FROM csp_registry").fetchone()["c"]
    return render_template("enrollments.html", pending=pending, recent=recent, registry_rows=reg, registry_total=n_reg,
                           norm=registry.normalize_name)


# ------------------------------------------------------------------------------------------------ Software
@ui_bp.route("/software", methods=["GET", "POST"])
@login_required
def software_page():
    admin = _admin()
    if request.method == "POST":
        f = request.form
        with get_connection() as conn:
            try:
                software.set_requirement(conn, f.get("key", ""), f.get("min_version", ""), f.get("note", ""),
                                         bool(f.get("active")), admin)
                flash("Saved. Every CSP is judged against this the next time it reports (this page already uses it).")
            except software.SoftwareError as e:
                flash(str(e))
            conn.commit()
        return redirect(url_for("ui.software_page"))
    with get_connection() as conn:
        reqs = software.requirements(conn)
        rows, totals = software.fleet(conn)
    return render_template("software.html", known=software.KNOWN, reqs=reqs, rows=rows, totals=totals,
                           labels=software.VERDICT_LABELS)
