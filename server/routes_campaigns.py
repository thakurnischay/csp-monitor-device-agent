"""Admin pages and JSON API for the Email Campaigns tab. Same session login, CSRF (every POST) and audit log as the rest of the
dashboard. Nothing on these pages sends an email by itself: opening a page, creating or editing a campaign, previewing and
running a dry run never leave the server. Only an explicit, typed-confirmation launch queues real mail (see campaigns/service.py).

JSON endpoints (all need the admin session; unauthenticated calls get 401 JSON, not a redirect):
    GET  /api/email-campaigns/summary                 summary cards
    GET  /api/email-campaigns/campaigns               campaign list with progress
    GET  /api/email-campaigns/messages                history; filters + ?page=&per_page=
    GET  /api/email-campaigns/messages/<id>           one message: recipient, content, attempts, delivery / open / click events
    GET  /api/email-campaigns/<id>/audience           eligibility: counts and (?bucket=&page=) the operators with the reason for each
    GET  /api/email-campaigns/<id>/preview            personalised preview (?csp_code= for a specific operator)
    GET  /api/email-campaigns/<id>/progress           live progress
    POST /api/email-campaigns/<id>/<action>           refresh | dry_run | test | launch | pause | resume | cancel | retry | retry_unknown
                                                      (form-encoded with csrf_token, like every admin POST)
"""
import json
import logging
from datetime import datetime, timedelta, timezone
from functools import wraps

from flask import Response, abort, flash, jsonify, redirect, render_template, request, session, url_for

from campaigns import eligibility, operators as ops, queries, rendering, sender, service, tracking
from campaigns.ai import drafting as ai_drafting, telemetry as ai_telemetry
from campaigns.service import CampaignError
from db import get_connection
from routes import login_required, ui_bp
from security import campaign_test_limiter

log = logging.getLogger("csp_monitor.routes_campaigns")
IST = timezone(timedelta(hours=5, minutes=30))
_FILTERS = ("q", "occasion", "campaign_id", "device", "status", "date_from", "date_to", "location", "rm")
_PREVIEW_HEADERS = {
    # The message body is shown in a sandboxed frame with NO image loading, so previewing never records an "open" and
    # nothing in the message can run.
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; img-src 'none'; frame-ancestors 'self'",
    "X-Frame-Options": "SAMEORIGIN", "Cache-Control": "no-store"}
_ERRORS = (CampaignError, rendering.TemplateError, eligibility.ConfigError)


def _admin():
    return session.get("admin_login") or "admin"


def api_login_required(fn):
    @wraps(fn)
    def wrap(*a, **k):
        if not session.get("admin_in"):
            return jsonify({"ok": False, "error": "login required"}), 401
        return fn(*a, **k)
    return wrap


def _filters():
    return {k: (request.args.get(k) or "").strip() for k in _FILTERS}


def _parse_ist(value):
    """The schedule box is in IST (what the dashboard shows everywhere); stored as UTC."""
    value = (value or "").strip()
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        raise CampaignError("The scheduled time is not valid.")
    return (dt if dt.tzinfo else dt.replace(tzinfo=IST)).astimezone(timezone.utc).isoformat(timespec="seconds")


def _form_defaults():
    return {"name": "", "description": "", "occasion": "", "devices": [], "audience_match": "any", "include_dormant": False,
            "subject": "Wishing you a happy {occasion}, {first_name}",
            "body": ("Dear {name},\n\nWishing you and your family a very happy {occasion}!\n\n"
                     "We noticed you do not yet have {missing_devices} at your CSP. If you would like to know more, "
                     "please contact {rm_name}.\n\nWarm regards,\nTeam Eko"),
            "link_url": "", "link_label": ""}


def _form_values(source):
    return {"name": source.get("name", ""), "description": source.get("description", ""), "occasion": source.get("occasion", ""),
            "devices": source.getlist("devices") if hasattr(source, "getlist") else (source.get("devices") or []),
            "audience_match": source.get("audience_match", "any"), "include_dormant": bool(source.get("include_dormant")),
            "subject": source.get("subject", ""), "body": source.get("body", ""), "link_url": source.get("link_url", ""),
            "link_label": source.get("link_label", "")}


# ------------------------------------------------------------------------------------------ AI telemetry hooks
def _ai_generation_for(conn, cid):
    r = conn.execute("SELECT id FROM ai_generations WHERE campaign_id=? AND status='ok' ORDER BY id DESC LIMIT 1", (cid,)).fetchone()
    return r["id"] if r else None


def _ai_after_save(cid, form, fields):
    """Link the AI draft the person started from to the campaign and record what they actually saved (edit distance included).
    Runs only AFTER the campaign change is committed, and can never break the save."""
    try:
        gid = form.get("ai_generation_id", "")
        with get_connection() as conn:
            if gid.isdigit() and conn.execute("SELECT 1 FROM ai_generations WHERE id=? AND status='ok'", (int(gid),)).fetchone():
                gid = int(gid)
            else:
                gid = _ai_generation_for(conn, cid)
        if gid:
            ai_telemetry.link_campaign(gid, cid)
            ai_telemetry.log_feedback(gid, "draft_saved", _admin(), cid, fields["subject"], fields["body"])
    except Exception:                                               # noqa: BLE001
        log.exception("AI telemetry hook failed")


def _ai_after_action(cid, action):
    events = {"launch": "launched", "dry_run": "dry_run", "test": "test_sent"}
    if action not in events:
        return
    try:
        with get_connection() as conn:
            gid = _ai_generation_for(conn, cid)
            c = service.get_campaign(conn, cid) if gid else None
        if gid and action == "launch":
            ai_telemetry.log_feedback(gid, "launched", _admin(), cid, c["subject"], c["body"])
        elif gid:
            ai_telemetry.log_feedback(gid, events[action], _admin(), cid)
    except Exception:                                               # noqa: BLE001
        log.exception("AI telemetry hook failed")


# ------------------------------------------------------------------------------------------------- the main tab
@ui_bp.route("/email-campaigns")
@login_required
def email_campaigns():
    filters = _filters()
    with get_connection() as conn:
        summary = queries.summary(conn)
        campaigns = queries.list_campaigns(conn)
        messages = queries.list_messages(conn, filters, request.args.get("page", 1), request.args.get("per_page", queries.PER_PAGE_DEFAULT))
        options = queries.filter_options(conn)
    kept = {k: v for k, v in filters.items() if v}
    return render_template("email_campaigns.html", summary=summary, campaigns=campaigns, messages=messages, filters=filters, options=options,
                           settings=sender.settings_summary(), device_labels=ops.DEVICE_LABELS, status_filters=queries.STATUS_FILTERS,
                           page_url=lambda n: url_for("ui.email_campaigns", **dict(kept, page=n, per_page=messages["per_page"])))


@ui_bp.route("/email-campaigns/new", methods=["GET", "POST"])
@login_required
def email_campaign_new():
    form = _form_defaults()
    if request.method == "POST":
        form = _form_values(request.form)
        try:
            fields = service.validate_fields(form)
            with get_connection() as conn:
                cid = service.create_campaign(conn, fields, _admin())
                conn.commit()
            _ai_after_save(cid, request.form, fields)
            flash("Draft saved. Nothing has been sent. Review the audience, preview the email and run a dry run before launching.")
            return redirect(url_for("ui.email_campaign", cid=cid))
        except _ERRORS as err:
            flash(str(err))
    return render_template("email_campaign_form.html", form=form, editing=None, occasions=service.OCCASIONS, device_labels=ops.DEVICE_LABELS,
                           placeholders=rendering.PLACEHOLDERS, ai=ai_drafting.settings_summary(), linked_generation=None)


@ui_bp.route("/email-campaigns/<int:cid>/edit", methods=["GET", "POST"])
@login_required
def email_campaign_edit(cid):
    with get_connection() as conn:
        try:
            c = service.get_campaign(conn, cid)
        except CampaignError:
            abort(404)
    if c["status"] != "draft":
        flash("Only a draft campaign can be edited.")
        return redirect(url_for("ui.email_campaign", cid=cid))
    form = {"name": c["name"], "description": c["description"] or "", "occasion": c["occasion"], "devices": c["devices"],
            "audience_match": c["audience_match"], "include_dormant": bool(c["include_dormant"]), "subject": c["subject"],
            "body": c["body"], "link_url": c["link_url"] or "", "link_label": c["link_label"] or ""}
    if request.method == "POST":
        form = _form_values(request.form)
        try:
            fields = service.validate_fields(form)
            with get_connection() as conn:
                service.update_campaign(conn, cid, fields, _admin())
                conn.commit()
            _ai_after_save(cid, request.form, fields)
            flash("Saved.")
            return redirect(url_for("ui.email_campaign", cid=cid))
        except _ERRORS as err:
            flash(str(err))
    with get_connection() as conn:
        linked = _ai_generation_for(conn, cid)
    return render_template("email_campaign_form.html", form=form, editing=c, occasions=service.OCCASIONS, device_labels=ops.DEVICE_LABELS,
                           placeholders=rendering.PLACEHOLDERS, ai=ai_drafting.settings_summary(), linked_generation=linked)


def _recipient_rows(conn, c, view, page, per_page=25):
    """The operators of a campaign in one bucket, with the reason for each. Drafts show the live audience (nothing stored);
    launched campaigns show the frozen records."""
    if c["status"] == "draft":
        aud = service.audience(conn, c["id"])
        rows = [r for r in aud["rows"] if r["bucket"] == view]
        counts = aud["counts"]
        shaped = [{"csp_code": r["csp_code"], "name": r["name"], "email_masked": tracking.mask_email(r["email"]), "rm": r["rm"],
                   "location": ", ".join(x for x in (r["district"], r["state"]) if x), "reason": r["reason"],
                   "missing": r["missing"], "review": r["review"], "status": "", "id": None} for r in rows]
    else:
        counts = {b: conn.execute("SELECT COUNT(*) FROM email_campaign_recipients WHERE campaign_id=? AND bucket=?", (c["id"], b)).fetchone()[0]
                  for b in (eligibility.ELIGIBLE, eligibility.REVIEW, eligibility.EXCLUDED)}
        raw = conn.execute("SELECT * FROM email_campaign_recipients WHERE campaign_id=? AND bucket=? ORDER BY csp_code", (c["id"], view)).fetchall()
        shaped = [{"csp_code": r["csp_code"], "name": r["name_at_send"], "email_masked": tracking.mask_email(r["email_at_send"]), "rm": r["rm_at_send"],
                   "location": r["location_at_send"], "reason": r["reason"], "missing": json.loads(r["missing_devices_json"] or "[]"),
                   "review": json.loads(r["review_devices_json"] or "[]"), "status": queries.display_status(r), "id": r["id"]} for r in raw]
    total = len(shaped)
    pages = max(1, -(-total // per_page))
    page = max(1, min(int(page or 1), pages))
    return shaped[(page - 1) * per_page: page * per_page], counts, {"page": page, "pages": pages, "total": total, "per_page": per_page}


@ui_bp.route("/email-campaigns/<int:cid>")
@login_required
def email_campaign(cid):
    view = request.args.get("view") if request.args.get("view") in (eligibility.ELIGIBLE, eligibility.REVIEW, eligibility.EXCLUDED) else eligibility.ELIGIBLE
    with get_connection() as conn:
        try:
            c = service.get_campaign(conn, cid)
        except CampaignError:
            abort(404)
        rows, counts, paging = _recipient_rows(conn, c, view, request.args.get("page", 1))
        progress = queries.progress(conn, cid) if c["status"] != "draft" else None
        outcome = json.loads(c["outcome_json"] or "{}")
        suppressed = conn.execute("SELECT COUNT(*) FROM email_suppressions").fetchone()[0]
        operators_loaded = conn.execute("SELECT COUNT(*) FROM campaign_operators").fetchone()[0]
    return render_template("email_campaign.html", c=c, rows=rows, counts=counts, paging=paging, view=view, progress=progress, outcome=outcome,
                           settings=sender.settings_summary(), device_labels=ops.DEVICE_LABELS, suppressed=suppressed,
                           operators_loaded=operators_loaded, can_edit=c["status"] == "draft")


# --------------------------------------------------------------------------------------------------- actions
def _perform(conn, cid, action, form):
    """One campaign action. Returns (message for the person, data for the API). Raises on any refusal."""
    admin = _admin()
    if action == "refresh":
        aud = service.materialize(conn, cid)
        return (f"Audience refreshed: {aud['counts']['eligible']} eligible, {aud['counts']['review']} to review, "
                f"{aud['counts']['excluded']} excluded."), {"counts": aud["counts"]}
    if action == "dry_run":
        out = service.dry_run(conn, cid, admin)
        return (f"Dry run complete: {out['would_send']} email(s) would be sent. Nothing was sent."
                + (f" {len(out['problems'])} problem(s) found." if out["problems"] else "")), out
    if action == "test":
        if not campaign_test_limiter.allow(admin):
            raise CampaignError("Too many test emails just now. Please wait a few minutes.")
        out = service.send_test(conn, cid, form.get("test_to"), admin)
        return (("Test email sent." if out["sent"] else out["note"]), {k: v for k, v in out.items() if k != "preview"})
    if action == "launch":
        out = service.launch(conn, cid, admin, form.get("confirm_text"), schedule_at=_parse_ist(form.get("schedule_at")))
        if out["status"] == "scheduled":
            return f"Scheduled for {out['scheduled_at']} UTC: {out['recipients']} recipients.", out
        return f"Launched: {out['recipients']} recipients queued. They are sent in capped batches.", out
    if action == "pause":
        service.pause(conn, cid, admin)
        return "Paused. Nothing more will be sent until you resume.", {}
    if action == "resume":
        service.resume(conn, cid, admin)
        return "Resumed.", {}
    if action == "cancel":
        n = service.cancel(conn, cid, admin)
        return f"Cancelled. {n} unsent message(s) will not be sent. Messages already accepted by the mail server cannot be recalled.", {"cancelled": n}
    if action in ("retry", "retry_unknown"):
        out = service.retry_failed(conn, cid, admin, include_unknown=(action == "retry_unknown"))
        return (f"{out['requeued'] + out['requeued_unknown']} message(s) put back in the queue. Messages already accepted are never resent."), out
    raise CampaignError("Unknown action.")


@ui_bp.route("/email-campaigns/<int:cid>/action", methods=["POST"])
@login_required
def email_campaign_action(cid):
    action, done = request.form.get("action", ""), False
    with get_connection() as conn:
        try:
            message, _ = _perform(conn, cid, action, request.form)
            conn.commit()
            flash(message)
            done = True
        except _ERRORS as err:
            conn.rollback()
            flash(str(err))
    if done:
        _ai_after_action(cid, action)
    return redirect(url_for("ui.email_campaign", cid=cid))


# ------------------------------------------------------------------------------------------- message pages
@ui_bp.route("/email-campaigns/messages/<int:rid>")
@login_required
def email_message(rid):
    with get_connection() as conn:
        d = queries.message_detail(conn, rid)
    if not d:
        abort(404)
    return render_template("email_message.html", m=d, tracking_note=True)


@ui_bp.route("/email-campaigns/messages/<int:rid>/body.html")
@login_required
def email_message_body(rid):
    with get_connection() as conn:
        d = queries.message_detail(conn, rid)
    if not d or not d["body_html"]:
        abort(404)
    return Response(d["body_html"], mimetype="text/html", headers=_PREVIEW_HEADERS)


@ui_bp.route("/email-campaigns/<int:cid>/preview.html")
@login_required
def email_campaign_preview_frame(cid):
    with get_connection() as conn:
        try:
            out = service.preview(conn, cid, request.args.get("csp_code") or None)
        except _ERRORS:
            abort(404)
    return Response(out["html"], mimetype="text/html", headers=_PREVIEW_HEADERS)


# ----------------------------------------------------------------------------------------- operators & opt-outs
@ui_bp.route("/email-campaigns/operators")
@login_required
def email_operators():
    with get_connection() as conn:
        summary = ops.ownership_summary(conn)
        imports = conn.execute("SELECT * FROM operator_imports ORDER BY id DESC LIMIT 8").fetchall()
        total = conn.execute("SELECT COUNT(*) FROM campaign_operators").fetchone()[0]
        present = conn.execute("SELECT COUNT(*) FROM campaign_operators WHERE present_in_latest=1").fetchone()[0]
        agents = {ops.normalize_code(r["csp_id"]) for r in conn.execute("SELECT csp_id FROM csps").fetchall()}
        codes = {r["csp_code"] for r in conn.execute("SELECT csp_code FROM campaign_operators").fetchall()}
        suppressions = conn.execute("SELECT rowid AS rid, * FROM email_suppressions ORDER BY created_at DESC LIMIT 25").fetchall()
        n_suppressed = conn.execute("SELECT COUNT(*) FROM email_suppressions").fetchone()[0]
        recon = conn.execute("SELECT COUNT(*) FROM operator_reconciliations").fetchone()[0]
    return render_template("email_operators.html", summary=summary, imports=imports, total=total, present=present, agents=len(agents),
                           matched=len(agents & codes), suppressions=suppressions, n_suppressed=n_suppressed, recon=recon,
                           device_labels=ops.DEVICE_LABELS, mask=tracking.mask_email, verdicts=ops.VERDICTS)


def _json_upload(name, limit=8 * 1024 * 1024):
    f = request.files.get(name)
    if not f or not f.filename:
        return None
    raw = f.read(limit + 1)
    if len(raw) > limit:
        raise CampaignError(f"{name} is too large.")
    try:
        return json.loads(raw.decode("utf-8-sig"))
    except (ValueError, UnicodeDecodeError):
        raise CampaignError("That file is not valid JSON.")


@ui_bp.route("/email-campaigns/operators/import", methods=["POST"])
@login_required
def email_operators_import():
    try:
        payload = _json_upload("csps_file")
        if payload is None:
            raise CampaignError("Choose the CSP Growth Platform's csps.json file.")
        recon = _json_upload("state_file")
        with get_connection() as conn:
            out = ops.import_operators(conn, payload, _admin(), request.files["csps_file"].filename, recon)
            from events import record_audit
            record_audit(conn, _admin(), "operators_imported", "", f"{out['accepted']} accepted, {out['skipped_total']} skipped of {out['total']}")
            conn.commit()
        flash(f"Imported {out['accepted']} operators ({out['skipped_total']} skipped). Existing campaign history was not touched.")
    except (CampaignError, ValueError) as err:
        flash(str(err))
    return redirect(url_for("ui.email_operators"))


@ui_bp.route("/email-campaigns/operators/reconcile", methods=["POST"])
@login_required
def email_operators_reconcile():
    f = request.form
    try:
        if f.get("owns") not in ("yes", "no"):
            raise CampaignError("Say whether the operator has the device.")
        with get_connection() as conn:
            code = ops.normalize_code(f.get("csp_code"))
            if not conn.execute("SELECT 1 FROM campaign_operators WHERE csp_code=?", (code,)).fetchone():
                raise CampaignError("That CSP code is not in the operator data.")
            ops.set_reconciliation(conn, code, f.get("device", ""), f.get("owns") == "yes", _admin(), f.get("note", ""))
            from events import record_audit
            record_audit(conn, _admin(), "ownership_reconciled", code, f"{f.get('device')}: owns={f.get('owns')}")
            conn.commit()
        flash("Recorded. A human decision overrides the sheet and the monitoring agent for this device.")
    except (CampaignError, ValueError) as err:
        flash(str(err))
    return redirect(url_for("ui.email_operators"))


@ui_bp.route("/email-campaigns/operators/suppress", methods=["POST"])
@login_required
def email_suppress():
    f = request.form
    address = " ".join((f.get("email") or "").split()).lower()
    with get_connection() as conn:
        if f.get("action") == "remove":
            if (f.get("suppression_id") or "").isdigit():                      # the list shows masked addresses, so it removes by id
                row = conn.execute("SELECT email FROM email_suppressions WHERE rowid=?", (int(f["suppression_id"]),)).fetchone()
                address = row["email"] if row else ""
            n = tracking.remove_suppression(conn, address, _admin())
            flash("Address removed from the opt-out list." if n else "That address was not on the list.")
        else:
            from campaigns.growth import emailer as ge
            if not ge.is_valid_email(address):
                flash("Enter a valid email address.")
            else:
                new = tracking.suppress(conn, address, None, "manual", actor=_admin())
                from events import record_audit
                record_audit(conn, _admin(), "email_suppression_added", "", tracking.mask_email(address))
                flash("Added to the opt-out list: it will never be emailed." if new else "That address was already on the list.")
        conn.commit()
    return redirect(url_for("ui.email_operators"))


# ------------------------------------------------------------------------------------------------ JSON API
@ui_bp.route("/api/email-campaigns/summary")
@api_login_required
def api_summary():
    with get_connection() as conn:
        return jsonify({"ok": True, "summary": queries.summary(conn), "settings": sender.settings_summary()})


@ui_bp.route("/api/email-campaigns/campaigns")
@api_login_required
def api_campaigns():
    with get_connection() as conn:
        return jsonify({"ok": True, "campaigns": [{k: v for k, v in c.items() if k not in ("body", "config_snapshot_json")} for c in queries.list_campaigns(conn)]})


@ui_bp.route("/api/email-campaigns/messages")
@api_login_required
def api_messages():
    with get_connection() as conn:
        out = queries.list_messages(conn, _filters(), request.args.get("page", 1), request.args.get("per_page", queries.PER_PAGE_DEFAULT))
    return jsonify(dict(out, ok=True))


@ui_bp.route("/api/email-campaigns/messages/<int:rid>")
@api_login_required
def api_message(rid):
    with get_connection() as conn:
        d = queries.message_detail(conn, rid)
    if not d:
        return jsonify({"ok": False, "error": "not found"}), 404
    return jsonify({"ok": True, "message": d})


@ui_bp.route("/api/email-campaigns/<int:cid>/audience")
@api_login_required
def api_audience(cid):
    bucket = request.args.get("bucket") if request.args.get("bucket") in (eligibility.ELIGIBLE, eligibility.REVIEW, eligibility.EXCLUDED) else eligibility.ELIGIBLE
    with get_connection() as conn:
        try:
            c = service.get_campaign(conn, cid)
        except CampaignError:
            return jsonify({"ok": False, "error": "not found"}), 404
        rows, counts, paging = _recipient_rows(conn, c, bucket, request.args.get("page", 1))
    return jsonify({"ok": True, "counts": counts, "bucket": bucket, "rows": rows, **paging})


@ui_bp.route("/api/email-campaigns/<int:cid>/preview")
@api_login_required
def api_preview(cid):
    try:
        with get_connection() as conn:
            out = service.preview(conn, cid, request.args.get("csp_code") or None)
    except _ERRORS as err:
        return jsonify({"ok": False, "error": str(err)}), 400
    return jsonify(dict(out, ok=True))


@ui_bp.route("/api/email-campaigns/<int:cid>/progress")
@api_login_required
def api_progress(cid):
    with get_connection() as conn:
        try:
            c = service.get_campaign(conn, cid)
        except CampaignError:
            return jsonify({"ok": False, "error": "not found"}), 404
        return jsonify({"ok": True, "status": c["status"], "mode": c["mode"], "progress": queries.progress(conn, cid)})


@ui_bp.route("/api/email-campaigns/<int:cid>/<action>", methods=["POST"])
@api_login_required
def api_action(cid, action):
    with get_connection() as conn:
        try:
            message, data = _perform(conn, cid, action, request.form)
            conn.commit()
        except _ERRORS as err:
            conn.rollback()
            return jsonify({"ok": False, "error": str(err)}), 400
    _ai_after_action(cid, action)
    return jsonify({"ok": True, "message": message, "data": data})
