"""Diagnosis Outcomes page and the administrator's "was this diagnosis right?" answers. Records and describes only: nothing here
changes a diagnosis, a rule or an agent."""
from flask import flash, redirect, render_template, request, session, url_for

import learning
from db import get_connection
from routes import login_required, ui_bp


@ui_bp.route("/diagnosis-outcomes")
@login_required
def diagnosis_learning():
    try:
        days = max(7, min(int(request.args.get("days", 90)), 365))
    except ValueError:
        days = 90
    with get_connection() as conn:
        rows = learning.summary(conn, days)
        total = conn.execute("SELECT COUNT(*) n FROM diagnosis_outcomes").fetchone()["n"]
        agents = conn.execute("SELECT COUNT(DISTINCT csp_id) n FROM diagnosis_outcomes").fetchone()["n"]
        feedback = conn.execute("SELECT COUNT(*) n FROM diagnosis_feedback").fetchone()["n"]
    return render_template("diagnosis_outcomes.html", rows=rows, days=days, total=total, agents=agents, feedback=feedback,
                           min_episodes=learning.MIN_EPISODES)


@ui_bp.route("/diagnosis-feedback", methods=["POST"])
@login_required
def diagnosis_feedback():
    admin = session.get("admin_login") or "admin"
    issue_id = request.form.get("issue_id", "")
    csp_id = None
    with get_connection() as conn:
        try:
            if not issue_id.isdigit():
                raise ValueError("That issue no longer exists.")
            issue = learning.record_feedback(conn, admin, int(issue_id), request.form.get("verdict", ""), request.form.get("note", ""))
            conn.commit()
            csp_id = issue["csp_id"]
            flash("Thank you - your answer was recorded.")
        except ValueError as err:
            conn.rollback()
            flash(str(err))
    return redirect(url_for("ui.csp_detail", csp_id=csp_id) if csp_id else url_for("ui.fleet"))
