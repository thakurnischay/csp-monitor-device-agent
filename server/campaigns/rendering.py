"""Turning a campaign template + one operator into a safe email.

Templates are PLAIN TEXT with a fixed set of {placeholders}. Raw HTML is never accepted: everything inserted is escaped,
so a template (or an operator's name) cannot inject markup or script. Subjects are stripped of line breaks (no header
injection). Links are limited to https. Every message carries an unsubscribe link and the RFC 8058 one-click headers.
"""
import re
from html import escape
from urllib.parse import urlparse

from .growth import emailer as ge       # the email project's brand palette, so these look like the same sender

PLACEHOLDERS = ("name", "first_name", "csp_code", "rm_name", "occasion", "missing_devices")
_PLACEHOLDER = re.compile(r"\{(\w+)\}")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
SUBJECT_MAX, BODY_MAX, LINK_MAX = 150, 8000, 500


class TemplateError(ValueError):
    """The campaign's subject, message or link is not acceptable."""


def _unknown(text):
    return sorted({m for m in _PLACEHOLDER.findall(text or "") if m not in PLACEHOLDERS})


def validate_template(subject, body):
    subject = " ".join(str(subject or "").split())
    body = str(body or "").replace("\r\n", "\n").strip()
    if not subject:
        raise TemplateError("Enter an email subject.")
    if len(subject) > SUBJECT_MAX:
        raise TemplateError(f"The subject is too long (maximum {SUBJECT_MAX} characters).")
    if not body:
        raise TemplateError("Enter the message text.")
    if len(body) > BODY_MAX:
        raise TemplateError(f"The message is too long (maximum {BODY_MAX} characters).")
    if _CONTROL.search(subject) or _CONTROL.search(body):
        raise TemplateError("The text contains characters that cannot be sent.")
    bad = _unknown(subject) + _unknown(body)
    if bad:
        raise TemplateError("Unknown placeholder(s): " + ", ".join("{" + b + "}" for b in sorted(set(bad)))
                            + ". Allowed: " + ", ".join("{" + p + "}" for p in PLACEHOLDERS) + ".")
    return subject, body


def validate_link(url, label=""):
    url = str(url or "").strip()
    if not url:
        return "", ""
    p = urlparse(url)
    if p.scheme != "https" or not p.netloc or p.username or p.password or len(url) > LINK_MAX or _CONTROL.search(url) or " " in url:
        raise TemplateError("The link must be a plain https:// address.")
    label = " ".join(str(label or "").split())[:60] or "Learn more"
    return url, label


def human_list(items):
    items = list(items)
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1] if items else ""


def context_for(campaign, op_name, csp_code, rm, missing_labels):
    name = " ".join(str(op_name or "").split()) or "Partner"
    return {"name": name, "first_name": name.split()[0].title() if name != "Partner" else "Partner",
            "csp_code": csp_code, "rm_name": " ".join(str(rm or "").split()) or "your Regional Manager",
            "occasion": campaign["occasion"], "missing_devices": human_list(missing_labels) or "our devices"}


def fill(text, ctx, html=False):
    def sub(m):
        v = str(ctx.get(m.group(1), ""))
        return escape(v) if html else v
    return _PLACEHOLDER.sub(sub, text)


def render(campaign, ctx, token, base_url, tracking=True):
    """-> {subject, html, text, headers}. `base_url` is the dashboard's public address (no trailing slash)."""
    subject = _CONTROL.sub("", fill(campaign["subject"], ctx)).replace("\r", " ").replace("\n", " ").strip()[:SUBJECT_MAX + 80]
    base = (base_url or "").rstrip("/")
    unsub = f"{base}/c/u/{token}"
    click = f"{base}/c/l/{token}" if campaign.get("link_url") else ""
    body = fill(campaign["body"], ctx)
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", body) if p.strip()]

    text = "\n\n".join(paragraphs)
    if click:
        text += f"\n\n{campaign.get('link_label') or 'Learn more'}: {click}"
    text += f"\n\n--\nYou are receiving this because you are an Eko CSP partner. To stop these emails: {unsub}\n"

    # `body` already has the values substituted; escape each WHOLE paragraph so nothing in it can ever be markup
    html_paras = "".join(
        f'<p style="margin:0 0 16px;font-size:16px;line-height:1.6;color:{ge.INK_BODY}">'
        + escape(p).replace("\n", "<br>") + "</p>" for p in paragraphs)
    button = ""
    if click:
        button = (f'<p style="margin:24px 0"><a href="{escape(click)}" style="background:{ge.AMBER};color:#ffffff;text-decoration:none;'
                  f'font-weight:700;padding:12px 22px;border-radius:8px;display:inline-block">'
                  f'{escape(campaign.get("link_label") or "Learn more")}</a></p>')
    pixel = ""
    if tracking:
        pixel = (f'<img src="{escape(base)}/c/o/{escape(token)}.gif" width="1" height="1" alt="" border="0" '
                 f'style="display:block;width:1px;height:1px" />')
    html = (f'<!doctype html><html><body style="margin:0;padding:0;background:{ge.PAGE_BG}">'
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:{ge.PAGE_BG};padding:24px 12px"><tr><td align="center">'
            f'<table role="presentation" width="600" cellpadding="0" cellspacing="0" style="max-width:600px;width:100%;background:#ffffff;'
            f'border-top:4px solid {ge.AMBER};border-radius:10px;font-family:Arial,Helvetica,sans-serif"><tr><td style="padding:28px 30px">'
            f'<p style="margin:0 0 18px;font-size:13px;font-weight:700;letter-spacing:.06em;text-transform:uppercase;color:{ge.AMBER_DEEP}">{escape(ctx["occasion"])}</p>'
            f'{html_paras}{button}'
            f'<hr style="border:none;border-top:1px solid {ge.CREAM_LINE};margin:26px 0 14px">'
            f'<p style="margin:0;font-size:12px;line-height:1.5;color:{ge.INK_MUTED}">You are receiving this because you are an Eko CSP partner. '
            f'<a href="{escape(unsub)}" style="color:{ge.INK_MUTED}">Unsubscribe</a>.</p>'
            f'</td></tr></table>{pixel}</td></tr></table></body></html>')
    headers = {"List-Unsubscribe": f"<{unsub}>", "List-Unsubscribe-Post": "List-Unsubscribe=One-Click"}
    return {"subject": subject, "html": html, "text": text, "headers": headers}
