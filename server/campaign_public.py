"""The only unauthenticated endpoints of Email Campaigns, kept in their own blueprint (so the admin-UI CSRF rule does not apply):

    GET  /c/o/<token>.gif        open pixel (a SIGNAL, not a fact: image pre-fetching and blocking make it inaccurate)
    GET  /c/l/<token>            click redirect to the campaign's own https link (never to anything taken from the request)
    GET  /c/u/<token>            unsubscribe confirmation page (a GET never unsubscribes: link scanners prefetch links)
    POST /c/u/<token>            unsubscribe (also the RFC 8058 one-click POST sent by mail clients)
    POST /api/email-campaigns/events   delivery / bounce / complaint events from a provider or bounce processor, authenticated
                                       with the shared secret CAMPAIGN_EVENTS_KEY (404 while that is not configured)

Tokens are 24 random characters, one per message; an unknown token reveals nothing. Everything is rate limited per source address.
"""
import hmac
import logging

from flask import Blueprint, Response, abort, jsonify, redirect, render_template, request

from campaigns import sender, tracking
from db import get_connection
from security import campaign_events_limiter, campaign_public_limiter

log = logging.getLogger("csp_monitor.campaign_public")
public_bp = Blueprint("campaign_public", __name__)

_GIF = (b"GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff!\xf9\x04\x01\x00\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01\x00\x00"
        b"\x02\x02D\x01\x00;")
_NO_STORE = {"Cache-Control": "no-store, max-age=0", "X-Robots-Tag": "noindex"}


def _source():
    return request.remote_addr or "unknown"


def _limited():
    return not campaign_public_limiter.allow(_source())


@public_bp.route("/c/o/<token>.gif")
def open_pixel(token):
    if not _limited():
        try:
            with get_connection() as conn:
                tracking.track_open(conn, token)
                conn.commit()
        except Exception:                                         # noqa: BLE001 - an image request must never error visibly
            log.exception("open tracking failed")
    return Response(_GIF, mimetype="image/gif", headers=_NO_STORE)


@public_bp.route("/c/l/<token>")
def click(token):
    if _limited():
        abort(429)
    with get_connection() as conn:
        target = tracking.track_click(conn, token)
        conn.commit()
    if not target:
        abort(404)
    resp = redirect(target, code=302)
    resp.headers.update(_NO_STORE)
    return resp


@public_bp.route("/c/u/<token>", methods=["GET", "POST"])
def unsubscribe(token):
    if _limited():
        abort(429)
    with get_connection() as conn:
        if request.method == "POST":
            out = tracking.unsubscribe(conn, token)
            conn.commit()
            if out is None:
                abort(404)
            return render_template("campaign_unsubscribe.html", done=True, masked=out["email_masked"], token=token), 200, _NO_STORE
        row = tracking._by_token(conn, token)
    if row is None:
        abort(404)
    return render_template("campaign_unsubscribe.html", done=False, masked=tracking.mask_email(row["email_at_send"]), token=token), 200, _NO_STORE


@public_bp.route("/api/email-campaigns/events", methods=["POST"])
def provider_events():
    key = sender.events_key()
    if not key:
        abort(404)                                                # the endpoint does not exist until it is configured
    if not campaign_events_limiter.allow(_source()):
        return jsonify({"ok": False, "error": "too many requests"}), 429
    given = request.headers.get("X-Campaign-Events-Key", "")
    if not hmac.compare_digest(given.encode(), key.encode()):
        log.warning("campaign events rejected: bad key from %s", _source())
        return jsonify({"ok": False, "error": "invalid key"}), 401
    body = request.get_json(silent=True)
    events = body.get("events") if isinstance(body, dict) else body
    try:
        with get_connection() as conn:
            report = tracking.ingest_provider_events(conn, events)
            conn.commit()
    except ValueError as err:
        return jsonify({"ok": False, "error": str(err)}), 400
    return jsonify(dict(report, ok=True))
