"""Admin pages and JSON endpoints for the AI drafting assistant and its quality metrics. Same session login and CSRF as the rest of
the dashboard. Nothing here sends an email; drafting only fills the campaign form, and a person still reviews, edits and launches.

    POST /api/email-campaigns/ai/context   what the model WOULD be given (free: no model call): chunks, scores, token budget
    POST /api/email-campaigns/ai/draft     one model call -> subject + message + the chunks it used + review flags
    GET  /api/email-campaigns/ai/metrics   the KPIs
    GET  /email-campaigns/ai/<id>          one generation in full: prompt, context, output, redaction, human corrections
    GET  /email-campaigns/ai-quality       the metrics page
"""
import json
import logging

from flask import abort, jsonify, render_template, request

from campaigns import eligibility, operators as ops, service
from campaigns.ai import drafting, knowledge, metrics, telemetry, textdiff
from db import get_connection
from routes import login_required, ui_bp
from routes_campaigns import _admin, api_login_required
from security import campaign_ai_limiter

log = logging.getLogger("csp_monitor.routes_ai")


def _fields_from_form(form):
    occasion = " ".join((form.get("occasion") or "").split())[:80]
    if not occasion:
        raise ValueError("Choose or type the festival / occasion first.")
    try:
        cfg = eligibility.clean_config(form.getlist("devices"), form.get("audience_match") or "any", False)
    except eligibility.ConfigError as err:
        raise ValueError(str(err))
    return {"occasion": occasion, "devices": cfg["devices"], "audience_match": cfg["audience_match"], "include_dormant": False}


@ui_bp.route("/api/email-campaigns/ai/context", methods=["POST"])
@api_login_required
def api_ai_context():
    try:
        fields = _fields_from_form(request.form)
    except ValueError as err:
        return jsonify({"ok": False, "error": str(err)}), 400
    with get_connection() as conn:
        prep = drafting.prepare(conn, fields, request.form.get("brief", ""), request.form.getlist("excluded"))
    return jsonify({"ok": True, "chunks": drafting._public_chunks(prep["plan"]), "tokens_used_est": prep["plan"]["tokens_used"],
                    "budget": prep["plan"]["budget"], "brief_clean": prep["brief_clean"], "redaction": prep["redaction"],
                    "ai": drafting.settings_summary()})


@ui_bp.route("/api/email-campaigns/ai/draft", methods=["POST"])
@api_login_required
def api_ai_draft():
    admin = _admin()
    try:
        fields = _fields_from_form(request.form)
    except ValueError as err:
        return jsonify({"ok": False, "error": str(err)}), 400
    if not drafting.enabled():
        return jsonify({"ok": False, "error": "AI drafting is not set up on this server yet. An administrator must configure it (see EMAIL_CAMPAIGNS_AI.md)."}), 503
    if not campaign_ai_limiter.allow(admin):
        return jsonify({"ok": False, "error": "Too many AI drafts just now. Please wait a few minutes."}), 429
    replaces = request.form.get("replaces_generation_id", "")
    with get_connection() as conn:
        try:
            out = drafting.generate(conn, fields, request.form.get("brief", ""), request.form.getlist("excluded"), admin,
                                    replaces=int(replaces) if replaces.isdigit() else None)
        except drafting.AiUnavailable as err:
            return jsonify({"ok": False, "error": str(err)}), 503
        except drafting.DraftError as err:
            return jsonify({"ok": False, "error": str(err), "generation_id": err.generation_id}), 502
    return jsonify({"ok": True, "generation": out})


@ui_bp.route("/api/email-campaigns/ai/metrics")
@api_login_required
def api_ai_metrics():
    try:
        days = max(1, min(int(request.args.get("days", 90)), 730))
    except ValueError:
        days = 90
    with get_connection() as conn:
        return jsonify({"ok": True, "metrics": metrics.compute(conn, days)})


@ui_bp.route("/email-campaigns/ai-quality")
@login_required
def email_ai_quality():
    try:
        days = max(1, min(int(request.args.get("days", 90)), 730))
    except ValueError:
        days = 90
    with get_connection() as conn:
        m = metrics.compute(conn, days)
        recent = metrics.recent_generations(conn)
        evals = metrics.eval_runs(conn)
    return render_template("email_ai_quality.html", m=m, recent=recent, evals=evals, ai=drafting.settings_summary(), days=days)


@ui_bp.route("/email-campaigns/ai/<int:gid>")
@login_required
def email_ai_generation(gid):
    with get_connection() as conn:
        g = telemetry.get_generation(conn, gid)
        if not g:
            abort(404)
        events = [dict(r) for r in conn.execute("SELECT * FROM ai_feedback WHERE generation_id=? ORDER BY id", (gid,)).fetchall()]
        campaign = None
        if g["campaign_id"]:
            row = conn.execute("SELECT id, name, subject, body, status FROM email_campaigns WHERE id=?", (g["campaign_id"],)).fetchone()
            campaign = dict(row) if row else None
    last_text = next((e for e in reversed(events) if e["final_body"] is not None), None)
    diff = None
    if last_text:
        diff = {"subject": textdiff.inline_diff_html(g["output_subject"], last_text["final_subject"]),
                "body": textdiff.inline_diff_html(g["output_body"], last_text["final_body"]), "event": last_text}
    return render_template("email_ai_generation.html", g=g, events=events, campaign=campaign, diff=diff)
