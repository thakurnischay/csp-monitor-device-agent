"""
Real email delivery, over smtplib.

Two modes, nothing in between:
  dry  - composes and reports, never opens a connection. THE DEFAULT.
  live - actually sends, to the real address.

Rendering is inline-styles-only, single column, no external assets, since
108 of the 120 reachable CSPs are on Gmail.
"""

import os
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr, parseaddr
from html import escape
from typing import Optional

from . import config


# ------------------------------------------------------------------- config
def _env(key: str, default: str = "") -> str:
    return (os.environ.get(key) or default).strip()


# GROWTH_ prefixed, not EMAIL_MODE / EMAIL_MAX_PER_RUN: merged into the Device Monitor, those two names belong to the
# festival Email Campaigns, and switching festival emails live must never also start this 30-day drip.
MODE = _env("GROWTH_EMAIL_MODE", "dry").lower()          # dry | live
MAX_PER_RUN = int(_env("GROWTH_EMAIL_MAX_PER_RUN", "25") or 25)
FROM = _env("EMAIL_FROM", "eko <no-reply@example.com>")
REPLY_TO = _env("EMAIL_REPLY_TO")

SMTP_HOST = _env("SMTP_HOST")
SMTP_PORT = int(_env("SMTP_PORT", "587") or 587)
SMTP_USER = _env("SMTP_USER")
SMTP_PASS = os.environ.get("SMTP_PASS") or ""
# 465 is implicit TLS; 587 upgrades via STARTTLS. Getting this backwards is the
# usual cause of a connection that hangs rather than errors.
SMTP_SECURE = _env("SMTP_SECURE").lower() == "true" or SMTP_PORT == 465

# Base URL the open-tracking pixel is served from (see open_pixel_tag() and
# run.py's /pixel/<token>.png). MUST be a real, internet-reachable address
# for this to do anything - localhost only resolves on the machine running
# this server, so a CSP's email client can never reach it there. Defaults to
# localhost purely so local testing/preview doesn't crash; change this the
# moment the app is deployed somewhere public.
TRACKING_BASE_URL = config.PUBLIC_BASE_URL     # see config.PUBLIC_BASE_URL: <dashboard>/growth


def config_summary() -> dict:
    return {
        "MODE": MODE, "MAX_PER_RUN": MAX_PER_RUN,
        "FROM": FROM, "REPLY_TO": REPLY_TO,
        "host": SMTP_HOST or None,
    }


# --------------------------------------------------- deliverability guarding
def is_valid_email(address: Optional[str]) -> bool:
    """Bare sanity check - has an @ and a real-looking domain. Not a typo
    detector: a malformed address is skipped, a real-but-wrong one is not
    caught here (nothing short of actually sending can tell the difference)."""
    if not address:
        return False
    trimmed = str(address).strip().lower()
    local, _, domain = trimmed.partition("@")
    if not local or not domain or "." not in domain or " " in trimmed:
        return False
    return len(domain.rsplit(".", 1)[-1]) >= 2


# ---------------------------------------------------------------- rendering
# Eko's brand palette, the same values the dashboard uses (frontend/style.css)
# and taken from eko.in's own logo fill and ramp. Named constants rather than
# hex scattered through the templates, because these strings appear in five
# separate renderers below and used to drift apart.
#
# Note on the CTA specifically: white on amber is what eko.in sets its own
# buttons in, so the campaign button matches it. Everywhere text has to be
# READ rather than tapped - headlines, body, the price line - the dark warm
# inks below are used instead, because amber behind small text is unreadable.
AMBER = "#ffb218"          # fills: buttons, banner, rules
AMBER_DEEP = "#a77000"     # amber as text on a light ground
CREAM = "#fffcf6"          # panel tint, matches the site's dropdown panels
CREAM_LINE = "#f5ead5"     # hairline around cream panels
INK = "#3f3f3f"            # headings - grey, never pure black, as on eko.in
INK_BODY = "#4a4a4a"
INK_MUTED = "#8a8a8a"
PAGE_BG = "#f7f4ee"        # the warm ground the message card floats on


def _rupees(n) -> str:
    """Indian digit grouping: 41000 -> 41,000; 150000 -> 1,50,000."""
    s = str(int(n))
    if len(s) <= 3:
        return "₹" + s
    last3, rest = s[-3:], s[:-3]
    parts = []
    while len(rest) > 2:
        parts.insert(0, rest[-2:])
        rest = rest[:-2]
    if rest:
        parts.insert(0, rest)
    return "₹" + ",".join(parts + [last3])


def _render_html(name, headline, body_lines, product, price_line, offer, rm_name,
                  form_url) -> str:
    paras = "".join(
        f'<p style="margin:0 0 14px;font-size:15px;line-height:1.6;color:{INK_BODY}">{line}</p>'
        for line in body_lines
    )
    headline_block = (
        f'<div style="margin:0 0 16px;padding:13px 16px;background:{CREAM};'
        f'border-left:4px solid {AMBER};border-radius:6px;font-size:14px;'
        f'line-height:1.55;color:#5c4108">{headline}</div>' if headline else ""
    )
    rm_bit = f" ya apne RM <b>{escape(rm_name)}</b> se baat kijiye" if rm_name else ""

    # form_url is None only if forms.missing_fields() still has entries the
    # live form doesn't carry yet (see GOOGLE_FORM_SETUP.md) - in that case the
    # button is skipped rather than pointing at a form that can't validate the
    # submission, and the copy below falls back to "reply".
    cta_block = (
        f'<div style="text-align:center;margin:22px 0">'
        f'<a href="{escape(form_url)}" '
        f'style="display:inline-block;background:{AMBER};color:#ffffff;'
        f'font-size:15px;font-weight:bold;text-decoration:none;padding:14px 34px;'
        f'border-radius:10px">Abhi Order Karein — Form Bhariye</a></div>'
        if form_url else ""
    )
    action_line = (
        "Order confirm karne ke liye upar diya button dabaiye — aapki details "
        "pehle se bhari hui hain, bas payment details aur confirm karni hain."
        if form_url else
        f"Interested hain? Is email ka <b>reply</b> kijiye{rm_bit} &mdash; "
        "hum aage ka process bata denge."
    )

    return f"""<div style="margin:0;padding:24px 12px;background:{PAGE_BG};font-family:Arial,Helvetica,sans-serif">
  <div style="max-width:560px;margin:0 auto;background:#ffffff;border-radius:14px;overflow:hidden;border:1px solid {CREAM_LINE}">
    <div style="background:{AMBER};padding:17px 24px">
      <div style="color:#ffffff;font-size:19px;font-weight:bold;letter-spacing:.02em">eko</div>
    </div>
    <div style="padding:24px 26px">
      <div style="font-size:12px;color:{AMBER_DEEP};text-transform:uppercase;letter-spacing:.07em;font-weight:bold">{escape(product)}</div>
      <p style="margin:9px 0 14px;font-size:15px;line-height:1.6;color:{INK_BODY}">
        Namaste <b style="color:{INK}">{escape(name)}</b> ji,
      </p>
      {headline_block}
      {paras}
      <div style="margin:20px 0;padding:16px 18px;background:{CREAM};border:1px solid {CREAM_LINE};border-radius:10px">
        <div style="font-size:11.5px;color:{INK_MUTED};text-transform:uppercase;letter-spacing:.06em">Keemat</div>
        <div style="font-size:22px;font-weight:bold;color:{INK};margin-top:3px">{escape(price_line)}</div>
        <div style="font-size:13px;color:{AMBER_DEEP};margin-top:6px;font-weight:bold">{escape(offer)}</div>
      </div>
      {cta_block}
      <p style="margin:0 0 6px;font-size:15px;line-height:1.6;color:{INK_BODY}">
        {action_line}
      </p>
      <p style="margin:24px 0 0;padding-top:16px;border-top:1px solid #f0ece4;font-size:12.5px;color:{INK_MUTED}">
        Team Eko
      </p>
    </div>
  </div>
  <div style="max-width:560px;margin:12px auto 0;text-align:center;font-size:11px;color:{INK_MUTED}">
    Aapko yeh email isliye mila kyunki aap Eko CSP network ka hissa hain.
  </div>
</div>"""


def _render_text(name, body_lines, price_line, offer, rm_name, form_url) -> str:
    import re
    plain = [re.sub(r"<[^>]+>", "", line) for line in body_lines]
    rm_bit = f" ya apne RM {rm_name} se baat kijiye" if rm_name else ""
    action = (f"Order confirm karne ke liye is link ko kholiye: {form_url}"
              if form_url else f"Interested hain? Is email ka reply kijiye{rm_bit}.")
    return "\n".join([
        f"Namaste {name} ji,", "", *plain, "",
        price_line, offer, "",
        action, "",
        "Team Eko",
    ])


# ------------------------------------------------------------- open tracking
def open_pixel_tag(token: Optional[str]) -> str:
    """
    A 1x1 invisible image whose request IS the open signal - see
    run.py's /pixel/<token>.png and store.mark_email_opened(). Empty string
    (nothing rendered) when there's no token, e.g. a preview with no real
    send behind it.

    Known-noisy by nature, not a bug in this implementation: Apple Mail
    Privacy Protection pre-fetches every image for every email regardless of
    whether anyone reads it (inflates opens), while clients that block
    remote images by default under-count real ones. Treat "opened" as "a
    signal", not a fact.
    """
    if not token:
        return ""
    url = f"{TRACKING_BASE_URL}/pixel/{token}.png"
    return f'<img src="{escape(url)}" width="1" height="1" alt="" border="0" style="display:block;width:1px;height:1px" />'


def click_tracked_form_url(token: Optional[str]) -> Optional[str]:
    """Wraps a campaign's order link behind our own server first, so a
    click can be recorded (run.py's /go/<token>, store.mark_form_clicked)
    before the CSP is redirected on - the same "email sent" vs "form
    opened" vs "form submitted" gap open_pixel_tag closes for opens, closed
    here for clicks. Same TRACKING_BASE_URL as the pixel; None in, None out
    - a preview with no persisted token has nothing to route through.

    WHERE /go/<token> SENDS THEM is decided at click time, not here, by
    config.ORDER_PAGE_LIVE: the order page (pre-filled from the token via
    orderpage.resolve_token, so the CSP types nothing) when it's on, the
    Google Form when it's off. That's deliberately a redirect rather than
    baking the destination into the email body: emails already sent stay
    correct when the destination changes, and nobody has to re-send a
    campaign to switch CSPs over.

    Note this is the one thing that differs from the WhatsApp templates
    (design/whatsapp-templates.md), which carry the STATIC order page link
    with no token - WhatsApp has no per-CSP token to route through, so the
    CSP identifies themselves on the page instead. Both land on the same
    order page; email just gets there pre-filled and click-tracked."""
    if not token:
        return None
    return f"{TRACKING_BASE_URL}/go/{token}"


# Eight touches per product (see outreach.next_touch / config.CAMPAIGN_SEND_
# DAY_OFFSETS - Day 1, 3, 6, 9, 12, 16, 20, 24 since the CSP's first touch),
# each arguing a genuinely different reason to buy - not the same pitch
# re-sent, and deliberately NOT framed as a follow-up/reminder (no "just
# checking in", no eyebrow badge announcing which touch this is). Every
# email should read like it could be the first one a CSP ever saw. The
# angles, in order, borrow from standard sales-influence technique, but
# stay grounded in real per-CSP data - no invented deadlines, no specific
# adoption numbers this platform doesn't actually have:
#   1 - opportunity cost: value already being left on the table (the
#       original pitch)
#   2 - ROI math: the concrete payback-time argument, spelled out
#   3 - peer norm: other CSPs on the network have already made this move
#   4 - friction removal: ordering is a two-minute, already-prefilled step
#   5 - ownership framing: ownership means the full upside is theirs, no
#       sharing it with whoever's device the activity currently runs on
#   6 - cost of waiting: the leak is ongoing and compounding, not a one-time
#       miss
#   7 - genuine urgency: earlier purchase means more of *this* earning cycle
#       is captured - true because commission accrues from install date, not
#       a manufactured deadline
#   8 - direct close: every argument already made, restated once, with a
#       plain ask
# Each touch has a "latent" variant (CSP has proven demand - passbook
# prints / mATM txns with no device of their own) and a "flat" variant (no
# latent-demand signal, pitched on transaction volume instead) - same split
# compose() has always made.
_TOUCH_COPY = {
    1: {
        "subject_latent": "Aapke {n:g} {unit} — apna {product} lagane ka sahi waqt",
        # Rewritten 2026-08-18: the old subject_flat/body_flat led with a raw
        # transaction-count citation ("aapke 1128 transactions") - flagged as
        # reading like a flat fact, not a reason to act. Still touch 1's own
        # job (opportunity cost - see the module-level _TOUCH_COPY comment
        # for all 8 angles), just argued through ownership/control loss and a
        # direct question instead of a statistic. body_latent (CSPs with a
        # real latent-demand signal, e.g. mATM transactions on someone else's
        # device) already leads with that specific number, which is the
        # relevant one - left unchanged.
        "subject_flat": "{name} ji, itni mehnat ka poora fayda aapko kyun nahi milta?",
        "body_latent": [
            "Apna {product} hone se yeh sab seedha aapke counter par hoga "
            "— na customer kahin aur jaayega, na kamai kahin aur.",
            "Jitni zyada activity, utni zyada kamai seedha aapki jeb mein — bina kisi "
            "hisse ke.",
        ],
        "body_flat": [
            "Aap itni mehnat karte hain apne business mein — lekin apna {product} na "
            "hone ki wajah se, is mehnat ka poora fayda seedha aapke paas nahi aata.",
            "{product} jodte hi yeh badlega — har transaction ka poora fayda seedha "
            "aapki jeb mein aayega, kisi aur device ya kisi aur CSP ke paas nahi.",
        ],
    },
    2: {
        "subject_latent": "{name} ji, {product} ka poora paisa kitne dino mein wapas aayega?",
        "subject_flat": "{name} ji, {product} ka poora paisa kitne dino mein wapas aayega?",
        "body_latent": [
            "Seedha hisaab: {product} ki keemat sirf {price} hai, aur {offer} — matlab "
            "paisa dubega nahi, seedha wapas aapke paas aayega.",
            "Uske baad har {unit} ka fayda 100% aapka hoga — na kisi doosre device ko, "
            "na kisi aur CSP ko.",
        ],
        "body_flat": [
            "Seedha hisaab: {product} ki keemat sirf {price} hai, aur {offer}.",
            "Aapke <b>{txns:,.0f} transactions</b> already batate hain ki investment "
            "jaldi recover ho jayega.",
        ],
    },
    3: {
        "subject_latent": "{name} ji, aapke jaise CSPs already yeh fayda utha rahe hain",
        "subject_flat": "{name} ji, aapke jaise CSPs already yeh fayda utha rahe hain",
        "body_latent": [
            "Eko network mein aapke jaise kai CSPs apna {product} laga kar yeh {unit} apne "
            "counter par le aaye hain, aur seedha har mahine isi se extra kama rahe hain.",
            "Wahi {n:g} {unit} jo abhi kisi aur device se ho rahe hain, unka poora fayda "
            "unke paas hai — aapka bhi ho sakta hai.",
        ],
        "body_flat": [
            "Eko network mein aapke jaisa business karne wale kai CSPs ne {product} laga "
            "liya hai, aur ab har transaction ka poora fayda khud le rahe hain.",
            "Aapke <b>{txns:,.0f} transactions</b> already dikhate hain ki yeh aapke liye "
            "bhi utna hi fayda mand hoga.",
        ],
    },
    4: {
        "subject_latent": "{name} ji, order karna sirf 2 minute ka kaam hai",
        "subject_flat": "{name} ji, order karna sirf 2 minute ka kaam hai",
        "body_latent": [
            "Poori process simple hai — form mein aapki details pehle se bhari hui hain, "
            "bas payment details daalni hain aur confirm karna hai.",
            "Delivery seedha aapke diye gaye address par hoti hai — kahin jaane ki "
            "zarurat nahi.",
        ],
        "body_flat": [
            "Poori process simple hai — form mein aapki details pehle se bhari hui hain, "
            "bas payment details daalni hain aur confirm karna hai.",
            "Delivery seedha aapke diye gaye address par hoti hai — kahin jaane ki "
            "zarurat nahi.",
        ],
    },
    5: {
        "subject_latent": "{name} ji, apna device — apna poora fayda",
        "subject_flat": "{name} ji, apna device — apna poora fayda",
        "body_latent": [
            "Jab tak {product} aapka khud ka nahi hai, {n:g} {unit} ka fayda kisi aur "
            "ke paas ja raha hai.",
            "Apna device hone ka matlab hai — koi share nahi, koi dependency nahi, poora "
            "control aapke haath mein.",
        ],
        "body_flat": [
            "Jab tak {product} aapka khud ka nahi hai, aapke transactions ka poora fayda "
            "kisi aur device tak simat sakta hai.",
            "Apna device hone ka matlab hai — koi share nahi, koi dependency nahi, poora "
            "control aapke haath mein.",
        ],
    },
    6: {
        "subject_latent": "{n:g} {unit} — yeh number har hafte badhta hi ja raha hai",
        "subject_flat": "{name} ji, jitna wait karenge, utna fayda haath se nikal raha hai",
        "body_latent": [
            "Pichhle do mahine mein {n:g} {unit} ho chuke hain — aur yeh number rukta "
            "nahi, badhta rehta hai.",
            "Jitni der {product} nahi lagate, utna zyada fayda kisi aur device ke paas "
            "jaata rahega.",
        ],
        "body_flat": [
            "Aapka <b>{txns:,.0f} transactions</b> ka business already bata raha hai ki "
            "demand hai — bas apna {product} lagane ki der hai.",
            "Jitni der wait karenge, utne transactions ka fayda bina wajah kisi aur ke "
            "paas jaata rahega.",
        ],
    },
    7: {
        "subject_latent": "{name} ji, jitni jaldi lagayenge, utna is mahine ka fayda aapka",
        "subject_flat": "{name} ji, jitni jaldi lagayenge, utna is mahine ka fayda aapka",
        "body_latent": [
            "{product} jitni jaldi lagega, utni jaldi iska fayda is mahine ki kamai mein "
            "judna shuru ho jayega — der karne ka seedha matlab hai kam fayda.",
            "{offer} — abhi order kijiye taaki yeh mahine se hi asar dikhna shuru ho.",
        ],
        "body_flat": [
            "{product} jitni jaldi lagega, utni jaldi iska fayda is mahine ki kamai mein "
            "judna shuru ho jayega — der karne ka seedha matlab hai kam fayda.",
            "{offer} — abhi order kijiye taaki yeh mahine se hi asar dikhna shuru ho.",
        ],
    },
    8: {
        "subject_latent": "{name} ji, {n:g} {unit} — ab bas order karna baaki hai",
        "subject_flat": "{name} ji, apna {product} — ab bas order karna baaki hai",
        "body_latent": [
            "Aapke {n:g} {unit}, {price} ki keemat, aur {offer} — sab kuch already "
            "aapke favour mein hai.",
            "Bas ek chhota sa step baaki hai: neeche diya button dabaiye aur order "
            "confirm kijiye.",
        ],
        "body_flat": [
            "Aapka <b>{txns:,.0f} transactions</b> ka business, {product} ki {price} "
            "keemat, aur {offer} — sab kuch already aapke favour mein hai.",
            "Bas ek chhota sa step baaki hai: neeche diya button dabaiye aur order "
            "confirm kijiye.",
        ],
    },
}


def compose(target: dict, product, form_url: str = None, token: Optional[str] = None,
            touch: int = 1) -> dict:
    """Builds subject + html + text for one target. Pure - sends nothing.

    `form_url` is the personalised, pre-filled order-form link for this CSP
    and product (see app.forms.issue_and_build) - pass None only when no
    token/link exists yet (e.g. previewing before forms.py is fully wired),
    in which case the email falls back to asking for a reply. `token` (same
    one form_url was built from) drives the open-tracking pixel - pass None
    to compose an email with no tracking, e.g. a preview. `touch` picks
    which email in the 8-touch sequence this is (see _TOUCH_COPY,
    outreach.next_touch); anything outside 1..8 falls back to touch 1."""
    copy = _TOUCH_COPY.get(touch, _TOUCH_COPY[1])
    price_line = f"{product.name} — {_rupees(product.price)}"
    latent = (target.get("latentDemand") or 0) > 0
    unit = product.latent_unit or "activity"
    n = target.get("latentDemand") or 0
    txns = target.get("transactions") or 0
    fmt = dict(
        n=n, unit=escape(unit), product=escape(product.name), txns=txns,
        price=escape(_rupees(product.price)), offer=escape(target["bandOffer"]),
        name=target["name"],
    )

    variant = "latent" if latent else "flat"
    subject = copy[f"subject_{variant}"].format(**fmt)
    body_lines = [line.format(**fmt) for line in copy[f"body_{variant}"]]

    # The data records that this activity happened through this CSP, never
    # confirms where the device physically sits - so the copy states the count
    # (real, per-CSP) plus the same "value is leaking to someone else's
    # device" inference the platform's own targeting logic already assumes
    # (see config.PRINTS_PROVE_OWNERSHIP / MATM_TXNS_PROVE_OWNERSHIP), not a
    # location or amount it cannot back. Same headline fact on every touch -
    # it doesn't change, only the argument built on top of it does.
    headline = (
        f"Pichhle do mahine mein aapke through <b>{n:g} {fmt['unit']}</b> "
        f"hue hain — lekin apna {fmt['product']} na hone se, iska poora fayda aapke "
        "paas nahi aa raha." if latent else None
    )

    html = _render_html(target["name"], headline, body_lines, product.name,
                        price_line, target["bandOffer"], target.get("rm"), form_url)
    html += open_pixel_tag(token)   # tail of the document - never affects visible layout

    return {
        "subject": subject,
        "html": html,
        "text": _render_text(target["name"], body_lines, price_line,
                             target["bandOffer"], target.get("rm"), form_url),
    }


# ---------------------------------------------------- ownership confirmation
# Not a sales pitch - these CSPs are the ones the sheet and the transaction
# data disagree about (see outreach.plan_confirmation_run). The ask is just
# "which one is right," the same question an RM would ask by phone. No CTA
# button/link - the reply itself is what gets acted on, in the Reconciliation
# tab, same as a phone call would be.
def _render_confirmation_html(name, product_name, txns, rm_name) -> str:
    rm_bit = f" ya apne RM <b>{escape(rm_name)}</b> se baat kijiye" if rm_name else ""
    return f"""<div style="margin:0;padding:24px 12px;background:{PAGE_BG};font-family:Arial,Helvetica,sans-serif">
  <div style="max-width:560px;margin:0 auto;background:#ffffff;border-radius:14px;overflow:hidden;border:1px solid {CREAM_LINE}">
    <div style="background:{AMBER};padding:17px 24px">
      <div style="color:#ffffff;font-size:19px;font-weight:bold;letter-spacing:.02em">eko</div>
      <div style="color:#ffffff;font-size:12px;margin-top:2px;opacity:.9">Jaldi confirm kijiye</div>
    </div>
    <div style="padding:24px">
      <p style="margin:0 0 14px;font-size:15px;line-height:1.6;color:{INK_BODY}">
        Namaste <b>{escape(name)}</b> ji,
      </p>
      <div style="margin:0 0 16px;padding:13px 16px;background:{CREAM};border-left:4px solid {AMBER};
                  border-radius:6px;font-size:14px;line-height:1.55;color:#5c4108">
        Hamare records mein aapke paas <b>{escape(product_name)}</b> nahi hai - lekin pichhle do
        mahine mein aapke <b>{txns:,.0f} transactions</b> is device ke through record hue hain.
      </div>
      <p style="margin:0 0 14px;font-size:15px;line-height:1.6;color:{INK_BODY}">
        Kripya confirm kijiye - is email ka reply <b>HAAN</b> (agar aapke paas
        {escape(product_name)} hai) ya <b>NAHI</b> (agar nahi hai) likh kar kijiye{rm_bit}.
      </p>
      <p style="margin:0 0 6px;font-size:15px;line-height:1.6;color:{INK_BODY}">
        Yeh sirf ek confirmation hai - koi order ya payment nahi mangi ja rahi.
      </p>
      <p style="margin:22px 0 0;padding-top:16px;border-top:1px solid #f0ece4;font-size:12.5px;color:{INK_MUTED}">
        Team Eko
      </p>
    </div>
  </div>
</div>"""


def _render_confirmation_text(name, product_name, txns, rm_name) -> str:
    rm_bit = f" ya apne RM {rm_name} se baat kijiye" if rm_name else ""
    return "\n".join([
        f"Namaste {name} ji,", "",
        f"Hamare records mein aapke paas {product_name} nahi hai - lekin pichhle do mahine "
        f"mein aapke {txns:,.0f} transactions is device ke through record hue hain.", "",
        f"Kripya confirm kijiye - is email ka reply HAAN (agar aapke paas {product_name} hai) "
        f"ya NAHI (agar nahi hai) likh kar kijiye{rm_bit}.", "",
        "Yeh sirf ek confirmation hai - koi order ya payment nahi mangi ja rahi.", "",
        "Team Eko",
    ])


def compose_ownership_confirmation(row: dict, product) -> dict:
    """row is one entry from /api/disputed - needs name/email/rm/matmTxns
    (or the equivalent txn count for the disputed product)."""
    txns = row.get("matmTxns") or row.get("transactions") or 0
    return {
        "subject": f"Confirm kijiye: kya aapke paas {product.name} hai?",
        "html": _render_confirmation_html(row["name"], product.name, txns, row.get("rm")),
        "text": _render_confirmation_text(row["name"], product.name, txns, row.get("rm")),
    }


# ------------------------------------------------ order status update emails
# A different, smaller template from compose() above - no CTA button, no
# affordability pitch, just "here's what just happened to your order." One
# shared renderer for all three stages (see orders.py:_EMAIL_ON_STATUS) since
# they're identically structured, just different banner colour/headline/body.
_ORDER_EMAIL_COPY = {
    "SUBMITTED": {
        "banner_color": "#475569",
        "banner_title": "Order Mil Gaya",
        "subject": "Order Mil Gaya - Aapka {product} Payment Verify Ho Raha Hai",
        "body": "Aapka <b>{product}</b> order aur payment details successfully receive ho gaye hain. "
                "Hamari finance team aapka payment verify karegi, uske baad hi aapka order place hoga. "
                "Verification complete hone par aapko email milega.",
    },
    "PAYMENT_VERIFIED": {
        "banner_color": "#0d7a3e",
        "banner_title": "Payment Mil Gaya",
        "subject": "Payment Mil Gaya - Aapka {product} Order Confirm Ho Gaya",
        "body": "Aapka payment finance team dwara verify ho gaya hai. Aapka <b>{product}</b> "
                "order ab process ho raha hai — jaise hi dispatch hoga, hum aapko update karenge.",
    },
    "DISPATCHED": {
        "banner_color": "#1d4ed8",
        "banner_title": "Order Dispatch Ho Gaya",
        "subject": "Dispatch Ho Gaya - Aapka {product} Order Raaste Mein Hai",
        "body": "Aapka <b>{product}</b> order dispatch ho gaya hai aur aapke diye gaye address "
                "par bheja ja raha hai. Jaldi hi aapko delivery mil jayegi.",
    },
    "DELIVERED": {
        "banner_color": "#0d7a3e",
        "banner_title": "Order Deliver Ho Gaya",
        "subject": "Deliver Ho Gaya - Aapka {product} Order Pahunch Gaya",
        "body": "Aapka <b>{product}</b> device deliver ho gaya hai! Ab aap ise turant use "
                "karna shuru kar sakte hain.",
    },
}


def _render_order_email_html(copy, name, order_id, product_name, amount_paid, utr) -> str:
    body = copy["body"].format(product=escape(product_name))
    return f"""<div style="margin:0;padding:24px 12px;background:{PAGE_BG};font-family:Arial,Helvetica,sans-serif">
  <div style="max-width:560px;margin:0 auto;background:#ffffff;border-radius:14px;overflow:hidden;border:1px solid {CREAM_LINE}">
    <div style="background:{copy['banner_color']};padding:18px 24px">
      <div style="color:#ffffff;font-size:17px;font-weight:bold">{escape(copy['banner_title'])}</div>
      <div style="color:#ffffff;font-size:12px;margin-top:2px;opacity:.85">Eko CSP Network</div>
    </div>
    <div style="padding:24px">
      <p style="margin:0 0 14px;font-size:15px;line-height:1.6;color:{INK_BODY}">
        Namaste <b>{escape(name)}</b> ji,
      </p>
      <p style="margin:0 0 14px;font-size:15px;line-height:1.6;color:{INK_BODY}">
        {body}
      </p>
      <div style="margin:18px 0;padding:14px 16px;background:{CREAM};border:1px solid {CREAM_LINE};border-radius:10px;
                  font-size:13px;color:{INK_BODY};line-height:1.9">
        <div><b>Order ID:</b> {escape(order_id)}</div>
        <div><b>Amount Paid:</b> {escape(_rupees(amount_paid))}</div>
        <div><b>UTR:</b> {escape(utr)}</div>
      </div>
      <p style="margin:22px 0 0;padding-top:16px;border-top:1px solid #f0ece4;font-size:12.5px;color:{INK_MUTED}">
        Team Eko
      </p>
    </div>
  </div>
</div>"""


def _render_order_email_text(copy, name, order_id, product_name, amount_paid, utr) -> str:
    import re
    body = re.sub(r"<[^>]+>", "", copy["body"].format(product=product_name))
    return "\n".join([
        f"Namaste {name} ji,", "", body, "",
        f"Order ID: {order_id}",
        f"Amount Paid: {_rupees(amount_paid)}",
        f"UTR: {utr}", "",
        "Team Eko",
    ])


def _compose_order_email(status: str, order: dict, product_name: str) -> dict:
    copy = _ORDER_EMAIL_COPY[status]
    return {
        "subject": copy["subject"].format(product=product_name),
        "html": _render_order_email_html(copy, order["name"], order["id"], product_name,
                                         order["amountPaid"], order["utr"]),
        "text": _render_order_email_text(copy, order["name"], order["id"], product_name,
                                         order["amountPaid"], order["utr"]),
    }


def compose_submission_received(order: dict, product_name: str) -> dict:
    """Sent the instant a CSP submits the form, before any verification -
    the one email in this pipeline that fires with zero conditions, since
    payment verification now genuinely depends on finance looking at the
    bank statement (see orders.py's module docstring) rather than an
    automatic amount/UTR match."""
    return _compose_order_email("SUBMITTED", order, product_name)


def compose_payment_confirmation(order: dict, product_name: str) -> dict:
    """Sent the moment an order reaches PAYMENT_VERIFIED - now only after
    finance marks it credited in the response sheet. `order` needs
    name/id/amountPaid/utr; product_name is the human-readable product name
    (order only stores the productId)."""
    return _compose_order_email("PAYMENT_VERIFIED", order, product_name)


def compose_dispatch_notification(order: dict, product_name: str) -> dict:
    """Sent the moment an order reaches DISPATCHED."""
    return _compose_order_email("DISPATCHED", order, product_name)


def compose_delivery_notification(order: dict, product_name: str) -> dict:
    """Sent the moment an order reaches DELIVERED."""
    return _compose_order_email("DELIVERED", order, product_name)


# --------------------------------------------- real-tracking progress emails
# A THIRD template family, distinct from both compose() (the sales pitch) and
# _compose_order_email() (the four formal order-status stages). These three
# phases are informational only - see app.tracking.run_tracking_checks -
# they never change order["status"], so there's no _EMAIL_ON_STATUS-style
# dispatch table for them; app.tracking calls compose_tracking_update
# directly. DELIVERED is deliberately NOT here - a courier confirming
# delivery goes through the real DELIVERED transition and
# compose_delivery_notification instead, so there is exactly one template
# for "delivered," not two that could drift apart.
_TRACKING_PHASE_COPY = {
    "PICKED_UP": {
        "banner_color": "#475569",
        "banner_title": "Courier Ne Pick Up Kar Liya",
        "subject": "Update: Aapka {product} Courier Ke Paas Pahunch Gaya",
        "body": "Courier ne aapka <b>{product}</b> pick up kar liya hai. Yeh ab transit ke "
                "liye taiyar ho raha hai.",
    },
    "IN_TRANSIT": {
        "banner_color": "#1d4ed8",
        "banner_title": "Order Transit Mein Hai",
        "subject": "Update: Aapka {product} Transit Mein Hai",
        "body": "Aapka <b>{product}</b> abhi transit mein hai aur aapke diye gaye address "
                "ki taraf badh raha hai.",
    },
    "OUT_FOR_DELIVERY": {
        "banner_color": "#c2410c",
        "banner_title": "Aaj Delivery Ho Sakti Hai",
        "subject": "Update: Aapka {product} Out For Delivery Hai",
        "body": "Aapka <b>{product}</b> out for delivery hai - jald hi aapko delivery mil "
                "sakti hai.",
    },
}


def _render_tracking_html(copy, name, product_name, order_id, courier, tracking_number) -> str:
    body = copy["body"].format(product=escape(product_name))
    return f"""<div style="margin:0;padding:24px 12px;background:{PAGE_BG};font-family:Arial,Helvetica,sans-serif">
  <div style="max-width:560px;margin:0 auto;background:#ffffff;border-radius:14px;overflow:hidden;border:1px solid {CREAM_LINE}">
    <div style="background:{copy['banner_color']};padding:18px 24px">
      <div style="color:#ffffff;font-size:17px;font-weight:bold">{escape(copy['banner_title'])}</div>
      <div style="color:#ffffff;font-size:12px;margin-top:2px;opacity:.85">Eko CSP Network</div>
    </div>
    <div style="padding:24px">
      <p style="margin:0 0 14px;font-size:15px;line-height:1.6;color:{INK_BODY}">
        Namaste <b>{escape(name)}</b> ji,
      </p>
      <p style="margin:0 0 14px;font-size:15px;line-height:1.6;color:{INK_BODY}">
        {body}
      </p>
      <div style="margin:18px 0;padding:14px 16px;background:{CREAM};border:1px solid {CREAM_LINE};border-radius:10px;
                  font-size:13px;color:{INK_BODY};line-height:1.9">
        <div><b>Order ID:</b> {escape(order_id)}</div>
        <div><b>Courier:</b> {escape(courier)}</div>
        <div><b>Tracking Number:</b> {escape(tracking_number)}</div>
      </div>
      <p style="margin:22px 0 0;padding-top:16px;border-top:1px solid #f0ece4;font-size:12.5px;color:{INK_MUTED}">
        Team Eko
      </p>
    </div>
  </div>
</div>"""


def _render_tracking_text(copy, name, product_name, order_id, courier, tracking_number) -> str:
    import re
    body = re.sub(r"<[^>]+>", "", copy["body"].format(product=product_name))
    return "\n".join([
        f"Namaste {name} ji,", "", body, "",
        f"Order ID: {order_id}",
        f"Courier: {courier}",
        f"Tracking Number: {tracking_number}", "",
        "Team Eko",
    ])


def compose_tracking_update(order: dict, product_name: str, phase: str) -> dict:
    """phase must be one of _TRACKING_PHASE_COPY's keys (PICKED_UP /
    IN_TRANSIT / OUT_FOR_DELIVERY) - DELIVERED goes through
    compose_delivery_notification instead, see the module comment above."""
    copy = _TRACKING_PHASE_COPY[phase]
    return {
        "subject": copy["subject"].format(product=product_name),
        "html": _render_tracking_html(copy, order["name"], product_name, order["id"],
                                      order.get("courier") or "", order.get("trackingNumber") or ""),
        "text": _render_tracking_text(copy, order["name"], product_name, order["id"],
                                      order.get("courier") or "", order.get("trackingNumber") or ""),
    }


# ------------------------------------------------------------------ sending
def _connect() -> smtplib.SMTP:
    if not SMTP_HOST:
        raise RuntimeError(
            "SMTP_HOST is not set. Copy .env.example to .env and fill it in before sending."
        )
    if SMTP_SECURE:
        server = smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=20,
                                  context=ssl.create_default_context())
    else:
        server = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20)
        server.starttls(context=ssl.create_default_context())
    if SMTP_USER:
        server.login(SMTP_USER, SMTP_PASS)
    return server


def verify_connection() -> dict:
    """Confirms the SMTP settings actually work, without sending anything."""
    if not SMTP_HOST:
        return {"ok": False, "error": "SMTP_HOST is not set"}
    try:
        with _connect() as server:
            server.noop()
        return {"ok": True, "host": SMTP_HOST, "port": SMTP_PORT,
                "secure": SMTP_SECURE, "user": SMTP_USER or None}
    except Exception as err:                      # noqa: BLE001 - report, never crash the app
        return {"ok": False, "error": f"{type(err).__name__}: {err}"}


def send(to: str, subject: str, html: str, text: str, server=None) -> dict:
    """
    Sends one message, honouring the mode.
      dry  - compose only, nothing leaves the machine
      live - send to the CSP

    `server` lets a caller reuse one connection across a batch instead of
    reconnecting per message, which SMTP providers rate-limit on.
    """
    if MODE == "dry":
        return {"sent": False, "mode": "dry", "to": to, "note": "dry run - nothing sent"}

    message = EmailMessage()
    name, addr = parseaddr(FROM)
    message["From"] = formataddr((name, addr)) if name else addr
    message["To"] = to
    if REPLY_TO:
        message["Reply-To"] = REPLY_TO
    message["Subject"] = subject
    message.set_content(text)
    message.add_alternative(html, subtype="html")

    own_connection = server is None
    if own_connection:
        server = _connect()
    try:
        server.send_message(message)
    finally:
        if own_connection:
            server.quit()

    return {"sent": True, "mode": MODE, "to": to}
