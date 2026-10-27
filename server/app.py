"""Central dashboard server. Run with: python app.py"""
import logging
import os
import secrets as _secrets
from logging.handlers import RotatingFileHandler

from flask import Flask, Request, abort, request, url_for
from werkzeug.middleware.proxy_fix import ProxyFix

from api import api_bp
from api_update import update_bp
from db import setup
from routes import ui_bp
import routes_updates  # noqa: F401  (adds the Agent Updates / Enrollments pages to ui_bp)
import routes_campaigns  # noqa: F401  (adds the Email Campaigns pages and JSON API to ui_bp)
import routes_learning  # noqa: F401  (diagnosis outcomes page and the "was this right?" answers)
import routes_ai  # noqa: F401  (AI drafting assistant endpoints and the AI quality pages)
from campaign_public import public_bp
from security import get_csrf_token, verify_csrf
from datetime import datetime, timezone, timedelta

log = logging.getLogger("csp_monitor.app")


def _configure_logging():
    """Rotating file (bounded - never grows unboundedly) + console (so
    `docker logs` still shows everything). Auth failures, admin actions, and
    unexpected errors all land here; never logs API keys/passwords - only
    csp_id and coarse outcomes."""
    log_path = os.environ.get("CSP_MONITOR_LOG_PATH",
                              os.path.join(os.path.dirname(os.path.abspath(__file__)), "server.log"))
    level = getattr(logging, os.environ.get("CSP_MONITOR_LOG_LEVEL", "INFO").upper(), logging.INFO)
    handlers = [
        RotatingFileHandler(log_path, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"),
        logging.StreamHandler(),
    ]
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s: %(message)s", handlers=handlers)


class _Request(Request):
    """Same 256 KB cap as always, except on the one admin route that uploads a signed release package."""
    @property
    def max_content_length(self):
        if self.path == "/updates/upload" and self.method == "POST":
            return 64 * 1024 * 1024
        if self.path == "/email-campaigns/operators/import" and self.method == "POST":
            return 16 * 1024 * 1024                 # the operator file (csps.json is ~0.5 MB) and optionally state.json
        return self.application_max()

    def application_max(self):
        from flask import current_app
        return current_app.config.get("MAX_CONTENT_LENGTH")


def create_app() -> Flask:
    app = Flask(__name__)
    app.request_class = _Request

    secret = os.environ.get("CSP_MONITOR_SECRET_KEY")
    if not secret:
        # Never silently fall back to a fixed, publicly-known string (that
        # string would sit in source control forever). A random per-process
        # secret at least means sessions can't be forged from outside - the
        # only cost is that a restart invalidates existing sessions, which is
        # far preferable to a predictable one. Set the env var in production
        # so this warning never fires and sessions survive restarts.
        secret = _secrets.token_hex(32)
        log.warning("CSP_MONITOR_SECRET_KEY is not set - using a random secret for "
                   "this process only. Sessions will not survive a restart. "
                   "Set this environment variable for production.")
    app.secret_key = secret

    # Trust one hop of X-Forwarded-* (this app always sits behind a reverse
    # proxy / Tailscale Funnel that terminates TLS) so Flask sees the real
    # originating scheme - needed for SESSION_COOKIE_SECURE below to behave
    # correctly instead of thinking every request is plain HTTP.
    # x_prefix: when nginx serves the app under a sub-path (e.g. /csp-device-heartbeat) it strips the prefix and
    # sends X-Forwarded-Prefix, so every generated link/redirect keeps the prefix. No header = unchanged behaviour.
    app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1, x_prefix=1)

    app.config.update(
        # Small, deliberate cap - every real payload here (a report, a login,
        # an API-key form post) is a few hundred bytes at most.
        MAX_CONTENT_LENGTH=256 * 1024,
        # Unique name: on a shared domain (several apps behind one nginx) the default cookie name "session"
        # would be overwritten by other apps' logins.
        SESSION_COOKIE_NAME="csp_monitor_session",
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        # Only force HTTPS-only cookies once the deployment is actually
        # fronted by TLS (true in production via Tailscale Funnel/nginx) -
        # forcing this on for a plain-HTTP local/dev run would silently break
        # login there, since the browser refuses to send a Secure cookie
        # back over HTTP.
        SESSION_COOKIE_SECURE=os.environ.get("CSP_MONITOR_FORCE_HTTPS", "0") == "1",
    )

    app.register_blueprint(ui_bp)
    app.register_blueprint(api_bp)
    app.register_blueprint(update_bp)
    app.register_blueprint(public_bp)

    app.jinja_env.globals["csrf_token"] = get_csrf_token

    def brand_logo():
        """URL of the supplied Eko logo file if it has been placed in static/img/ (svg preferred), else None -
        the layout then shows its built-in drawn mark. Checked per render so dropping the file in needs no restart."""
        for name in ("eko-logo.svg", "eko-logo.png", "eko-logo.webp"):
            if os.path.isfile(os.path.join(app.static_folder, "img", name)):
                return url_for("static", filename="img/" + name)
        return None
    app.jinja_env.globals["brand_logo"] = brand_logo
    app.jinja_env.globals["has_endpoint"] = lambda name: name in app.view_functions

    @app.before_request
    def _csrf_protect():
        # Only the session-cookie-authenticated admin UI is a CSRF target -
        # /api/report authenticates with a header API key, not a cookie, so
        # it's not vulnerable to this class of attack and is left alone.
        if request.blueprint == "ui" and request.method == "POST":
            if not verify_csrf(request.form.get("csrf_token", "")):
                abort(400, description="Invalid or missing CSRF token - please reload the page and try again.")

    @app.after_request
    def _security_headers(resp):
        resp.headers["X-Content-Type-Options"] = "nosniff"
        # setdefault: a route may tighten or (for the sandboxed email preview only) relax these itself
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        # Safe to set script-src 'self' outright: no template anywhere in
        # this app has an inline <script> tag. style-src needs 'unsafe-inline'
        # since the templates use inline style="" attributes throughout.
        resp.headers.setdefault("Content-Security-Policy", (
            "default-src 'self'; style-src 'self' 'unsafe-inline'; "
            "script-src 'self'; frame-ancestors 'none'"
        ))
        return resp

    IST = timezone(timedelta(hours=5, minutes=30))

    @app.template_filter("ist")
    def _ist_filter(value):
        if not value:
            return "-"
        try:
            dt = datetime.fromisoformat(str(value))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            dt = dt.astimezone(IST)
            return dt.strftime("%d %b %Y, %I:%M:%S %p") + " IST"
        except (ValueError, TypeError):
            return str(value)

    @app.template_filter("relative_time")
    def _relative_time_filter(value):
        """"3 min ago" style text - the exact timestamp is still available
        via the `ist` filter for a hover tooltip, this is just the headline."""
        if not value:
            return "-"
        try:
            dt = datetime.fromisoformat(str(value))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            return str(value)
        seconds = (datetime.now(timezone.utc) - dt).total_seconds()
        if seconds < 0:
            return "just now"
        if seconds < 60:
            return "just now"
        if seconds < 3600:
            return f"{int(seconds // 60)} min ago"
        if seconds < 86400:
            return f"{int(seconds // 3600)} hr ago"
        return f"{int(seconds // 86400)} day{'s' if seconds >= 172800 else ''} ago"

    return app


_configure_logging()
setup()
app = create_app()

# Opt-in background sender for launched / scheduled email campaigns (CSP_MONITOR_CAMPAIGN_WORKER=1). Off by default so tests and
# one-off scripts that import this module never start a thread. It only ever sends campaigns a person has explicitly launched.
if os.environ.get("CSP_MONITOR_CAMPAIGN_WORKER") == "1":
    from campaigns import worker as _campaign_worker
    from db import get_connection as _get_connection
    _campaign_worker.start(_get_connection, int(os.environ.get("CAMPAIGN_TICK_SECONDS") or 60))

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5100))
    app.run(host="0.0.0.0", port=port)
