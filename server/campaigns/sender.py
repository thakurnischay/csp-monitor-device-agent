"""Sending: settings, the live-sending gate, and the SMTP transport.

The SMTP connection itself is the email project's own engine (campaigns/growth/emailer.py, vendored unchanged), so it reads
the same environment variables (EMAIL_FROM, EMAIL_REPLY_TO, SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS, SMTP_SECURE).
Credentials live only in the server's environment; nothing here ever puts them in a page, a log line or a database row.

Two switches must BOTH be on before any real email leaves (on top of the per-campaign typed confirmation):
    EMAIL_MODE=live                (same switch as the email project; anything else = dry)
    CAMPAIGNS_LIVE_ENABLED=1       (a second, campaign-specific switch)
"""
import os
import smtplib
import socket
import ssl
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid, parseaddr

from .growth import emailer as ge


class BatchAbort(Exception):
    """A problem that affects every message (bad login, sender refused): stop the batch, do not burn attempts."""


def _int(name, default, lo, hi):
    try:
        value = int(float(os.environ.get(name) or default))
    except (TypeError, ValueError):
        value = default
    return max(lo, min(value, hi))


def _float(name, default, lo, hi):
    try:
        value = float(os.environ.get(name) or default)
    except (TypeError, ValueError):
        value = default
    return max(lo, min(value, hi))


def mode():
    return "live" if (os.environ.get("EMAIL_MODE") or "dry").strip().lower() == "live" else "dry"


def live_enabled():
    return mode() == "live" and (os.environ.get("CAMPAIGNS_LIVE_ENABLED") or "").strip() == "1"


def max_per_run():
    return _int("EMAIL_MAX_PER_RUN", 25, 1, 500)


def send_delay():
    return _float("CAMPAIGN_SEND_DELAY_SECONDS", 1.0, 0.0, 30.0)


def max_attempts():
    return _int("CAMPAIGN_MAX_ATTEMPTS", 3, 1, 10)


def retry_backoff_minutes():
    return _int("CAMPAIGN_RETRY_MINUTES", 15, 1, 1440)


def public_base_url():
    return (os.environ.get("CAMPAIGN_PUBLIC_BASE_URL") or "").strip().rstrip("/")


def live_blockers():
    """Reasons a live send cannot start right now, in plain words (empty list = nothing is blocking)."""
    out = []
    if mode() != "live":
        out.append("EMAIL_MODE is not 'live' on the server.")
    if (os.environ.get("CAMPAIGNS_LIVE_ENABLED") or "").strip() != "1":
        out.append("CAMPAIGNS_LIVE_ENABLED is not '1' on the server.")
    base = public_base_url()
    if not base.startswith("https://"):
        out.append("CAMPAIGN_PUBLIC_BASE_URL is not set to the dashboard's public https address "
                   "(the unsubscribe link in every email needs it).")
    if not os.environ.get("SMTP_HOST"):
        out.append("SMTP_HOST is not set on the server.")
    return out


def settings_summary():
    """Safe to show on the dashboard: never includes a password or key."""
    return {"mode": mode(), "live_enabled": live_enabled(), "max_per_run": max_per_run(), "send_delay_seconds": send_delay(),
            "max_attempts": max_attempts(), "smtp_configured": bool(os.environ.get("SMTP_HOST")),
            "from": os.environ.get("EMAIL_FROM") or "", "public_base_url_set": bool(public_base_url()),
            "events_endpoint_enabled": bool(events_key()), "blockers": live_blockers()}


def events_key():
    return (os.environ.get("CAMPAIGN_EVENTS_KEY") or "").strip()


def classify(exc):
    """'permanent' (this address will not work), 'transient' (try again later) or raises BatchAbort (nothing will work)."""
    if isinstance(exc, (smtplib.SMTPAuthenticationError, smtplib.SMTPSenderRefused, smtplib.SMTPNotSupportedError,
                        smtplib.SMTPHeloError)):
        raise BatchAbort(f"{type(exc).__name__}: the mail server refused our login or sender address")
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        codes = [c for c, _ in exc.recipients.values()]
        return "transient" if codes and all(400 <= c < 500 for c in codes) else "permanent"
    if isinstance(exc, smtplib.SMTPResponseException):
        return "transient" if 400 <= exc.smtp_code < 500 else "permanent"
    if isinstance(exc, (smtplib.SMTPServerDisconnected, smtplib.SMTPConnectError, socket.timeout, TimeoutError,
                        ConnectionError, ssl.SSLError, OSError)):
        return "transient"
    return "transient"


def error_text(exc):
    """A short, safe description of a failure for the dashboard (no addresses, no credentials)."""
    text = f"{type(exc).__name__}"
    code = getattr(exc, "smtp_code", None)
    if code:
        text += f" {code}"
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        text = "SMTPRecipientsRefused " + ",".join(str(c) for c, _ in exc.recipients.values())
    return text


class SmtpTransport:
    """One connection reused for a whole batch (providers rate-limit connection churn), via the email project's _connect."""
    def __init__(self):
        self.server = None

    def open(self):
        self.server = ge._connect()
        return self

    def send(self, to, subject, html, text, headers=None):
        message = EmailMessage()
        name, addr = parseaddr(os.environ.get("EMAIL_FROM") or ge.FROM)
        message["From"] = formataddr((name, addr)) if name else addr
        message["To"] = to
        reply = os.environ.get("EMAIL_REPLY_TO") or ge.REPLY_TO
        if reply:
            message["Reply-To"] = reply
        message["Subject"] = subject
        message["Date"] = formatdate(localtime=False)
        message_id = make_msgid(domain=(addr.partition("@")[2] or "localhost"))
        message["Message-ID"] = message_id
        for k, v in (headers or {}).items():
            message[k] = v
        message.set_content(text)
        message.add_alternative(html, subtype="html")
        refused = self.server.send_message(message)
        if refused:
            raise smtplib.SMTPRecipientsRefused(refused)
        return message_id

    def close(self):
        if self.server is not None:
            try:
                self.server.quit()
            except Exception:                 # noqa: BLE001 - already sent
                pass
            self.server = None


def get_transport():
    """Overridden by tests with a fake; production uses SMTP."""
    return SmtpTransport()
