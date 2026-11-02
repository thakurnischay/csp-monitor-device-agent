"""
Real WhatsApp delivery, over CERF's Business Solution Provider API.

Two modes, nothing in between - same contract as emailer.py:
  dry  - composes and reports, never opens a connection. THE DEFAULT.
  live - actually sends, to the real number.

ONE template PER PRODUCT (design settled 2026-08-27, third iteration - tried
a per-product pair, then one shared template with product name as a
variable, back to a per-product pair). Product name is STATIC text in each
body, not a variable - simpler and fewer variables (8, not 12) than the
shared-template version, and avoids repeating any {{n}} at all (the shared
version needed the same product-name variable at 4 separate slots since
CERF's own editor does not allow reusing one number). Occasion name,
reference price and deadline are still template variables, filled per day
from offer_calendar.active_offer() - adding a new festival is a data-only
change (backend/data/offer_calendar.json), no new CERF submission for it.
Adding a new PRODUCT, unlike the shared-template version, DOES need a new
template submission now (one per product is the whole point of this
design), but that trade was made deliberately for the simpler body.

*** These two templates have NOT yet been submitted to/approved by CERF. ***
An earlier, differently-structured pair (csp_rakhi_matm / csp_rakhi_printer,
1 variable each, Rakhi-specific fixed text) WAS approved 2026-08-27, but was
superseded the same day before ever being used to send - that approval does
not carry over. Nor does the single-shared-template version's (also drafted
and abandoned the same day, 12 variables, never submitted).

CONFIRMED 2026-08-27 - the real send mechanism: goinfinito (the BSP CERF
sits on top of), via https://api.goinfinito.com/unified/v2/send. Both
templates are approved, both numeric template IDs and the sender/WABA
number are known, and CERF_API_KEY (the Bearer token) is set in .env. See
send() below for the exact payload shape - it is NOT the standard Meta
Cloud API shape this module originally guessed at; goinfinito's own
"templateinfo" field is a tilde-separated string of
"<numeric template ID>~<var1>~<var2>~..." - the template's NAME is not
sent at all, only its ID (TEMPLATE_ID below), and putting the name there
by mistake shifts every variable by one and produces an
"Incorrect Template Details" error from goinfinito.

goinfinito also expects a per-message unique id and a delivery-receipt (DLR)
webhook URL (CERF_DLR_URL) that IT will POST to once a message is
delivered/read. That webhook's payload shape is not yet known (goinfinito's
docs don't specify it) - see run.py's /api/whatsapp/dlr route, which
currently just logs whatever arrives to the audit trail so a real payload
can be inspected before writing a parser for it.
"""

import json
import os
import urllib.error
import urllib.request
import uuid
from datetime import date
from typing import Optional

from . import offer_calendar


# ------------------------------------------------------------------- config
def _env(key: str, default: str = "") -> str:
    return (os.environ.get(key) or default).strip()


# WhatsApp is NOT part of the Device Monitor merge: no route or scheduler calls this module (outreach.py only imports
# it), and the mode is pinned to dry so nothing here can ever send, whatever the environment says.
MODE = "dry"
MAX_PER_RUN = int(_env("WHATSAPP_MAX_PER_RUN", "25") or 25)

# goinfinito connection details - confirmed 2026-08-27. CERF_API_KEY has no
# default (a real secret, must come from .env) - a live send fails loudly
# ("CERF_API_KEY not configured") rather than silently hitting nothing.
# CERF_FROM / CERF_WABA_ID default to the same confirmed sender number
# (7428655377) - goinfinito's docs call it "your registered WhatsApp sender
# number", used as the "from" address on every send.
# CERF_WABA_ID default carries country code (91) - goinfinito's own example
# payload shows "from": "917428655377". The number as first given
# (7428655377, no country code) was used bare as CERF_FROM's default until
# 2026-08-27, when a live test send failed with goinfinito's real
# errorcode 28680 "Invalid Sender ID" - a country-code-less sender number
# is apparently what that error means. Fixed by prefixing "91" here.
CERF_API_URL = _env("CERF_API_URL", "https://api.goinfinito.com/unified/v2/send")
CERF_API_KEY = os.environ.get("CERF_API_KEY") or ""
CERF_WABA_ID = _env("CERF_WABA_ID", "917428655377")
CERF_FROM = _env("CERF_FROM", CERF_WABA_ID)

# Base URL this app is reachable at - same variable emailer.py's open-
# tracking pixel uses, so both features point at the same deployment
# without duplicating config. Defaults to localhost purely so local
# testing doesn't crash; goinfinito can never reach a localhost DLR URL, so
# this MUST point at the real deployment before DLR receipts can work.
TRACKING_BASE_URL = _env("GROWTH_PUBLIC_BASE_URL", "http://localhost:5100/growth").rstrip("/")
CERF_DLR_URL = _env("CERF_DLR_URL", f"{TRACKING_BASE_URL}/api/whatsapp/dlr")

TEMPLATE_LANGUAGE = _env("CERF_TEMPLATE_LANGUAGE", "en")
# One submission per product - each covers every future occasion for that
# product, but a new product needs its own new template. TEMPLATE_NAME
# (CERF's actual approved names, e.g. "r_atm"/"p_printer" - not the
# csp_offer_matm/csp_offer_printer this module originally submitted under)
# is kept for record-keeping/display only - the goinfinito send API uses
# TEMPLATE_ID (the numeric IDs, e.g. 1792301/1792302), NOT the name. See
# send()'s templateinfo construction.
TEMPLATE_NAME = {
    "MATM": _env("CERF_TEMPLATE_MATM", "r_atm"),
    "PRINTER": _env("CERF_TEMPLATE_PRINTER", "p_printer"),
}
TEMPLATE_ID = {
    "MATM": _env("CERF_TEMPLATE_ID_MATM", "1792301"),
    "PRINTER": _env("CERF_TEMPLATE_ID_PRINTER", "1792302"),
}

# Evergreen, product-specific (not occasion-specific) commission averages -
# see design/rakshabandhan-offer-templates.md's "Where the numbers come
# from" for the computation (backend/data/csps.json, 535 CSPs, 2026-08-27).
# Re-verify from that file if this module is reused long after that date,
# since commission distributions drift.
EARNINGS = {
    "MATM": (26700, 10900),      # (owner avg/month, non-owner avg/month)
    "PRINTER": (40700, 14200),
}


class NoActiveOfferError(Exception):
    """Raised by compose() when offer_calendar has no entry covering today
    for this product - there is genuinely nothing to advertise, not a
    missing-data bug to paper over with a default price."""


def config_summary() -> dict:
    return {
        "MODE": MODE, "MAX_PER_RUN": MAX_PER_RUN,
        "configured": bool(CERF_API_URL and CERF_API_KEY and CERF_FROM),
        "templates": dict(TEMPLATE_NAME), "templateIds": dict(TEMPLATE_ID),
        "language": TEMPLATE_LANGUAGE, "from": CERF_FROM,
        "dlrUrl": CERF_DLR_URL,
    }


# --------------------------------------------------- deliverability guarding
def is_valid_mobile(mobile: Optional[str]) -> bool:
    """Bare sanity check - digits only (after a leading +), 10-15 of them,
    same spirit as emailer.is_valid_email: catches obviously-missing or
    malformed numbers, not a carrier-level validity check."""
    if not mobile:
        return False
    digits = str(mobile).strip().lstrip("+")
    return digits.isdigit() and 10 <= len(digits) <= 15


# ---------------------------------------------------------------- rendering
def _rupees(n) -> str:
    """Indian digit grouping: 41000 -> ₹41,000; 150000 -> ₹1,50,000."""
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


def _format_date(iso_date: str) -> str:
    d = date.fromisoformat(iso_date)
    return f"{d.day} {d.strftime('%B')}, {d.year}"


# The two CERF-submittable bodies - product name is STATIC text (appears 4x,
# same as the earlier shared-template draft, but hardcoded per product
# instead of a repeated variable). Copywriting structure (loss-aversion
# lead, not gain-framing - behaviourally people weight an avoidable LOSS
# more heavily than an equivalent gain, and "paisa kisi aur ke paas ja raha
# hai" is the sharper hook than "paisa kama sakte hain"):
#   1. Hook: money is already slipping away, right now, for a fixable reason.
#   2. Proof: real owner-vs-non-owner ₹ gap, stated as an explicit monthly
#      LOSS figure (not left for the reader to subtract themselves).
#   3. Offer: occasion + reference price + real price + deadline - arrives
#      only after the reader already feels the cost of not having one.
#   4. Payoff: what changes the moment they own it - total, no split.
#   5. CTA: single link, urgency repeated once more at the very end.
#
# 8 variables, numbered sequentially from {{1}} in the order each first
# appears (Meta requires this - no forward references), none repeated, no
# two adjacent with nothing between them - including the "ki keemat"
# separator between price variables, confirmed necessary 2026-08-27 when
# CERF's editor flagged a bare-space gap between two variables as "variable
# parameters next to each other" (see design/
# whatsapp-templates-cerf-submission.md's pre-submission checklist).
# Reference price is now STATIC per-product text, not a variable ({{6}}
# dropped, per 2026-08-27 instruction) - safe because it's the same anchor
# for every occasion anyway (₹10,000 / ₹42,000, set 2026-08-27: "same anchor
# har occasion ke liye"). Trade-off: changing this number later means
# editing the template body and re-submitting to CERF, not just editing
# offer_calendar.json - that file's own "referencePrice" field is no longer
# read by this module (kept in the data for record-keeping / other
# channels), so it can drift from what's hardcoded below if edited without
# also updating REFERENCE_PRICE here and the CERF-approved body text.
REFERENCE_PRICE = {
    "MATM": 10000,
    "PRINTER": 42000,
}

_BODY = {
    "MATM": (
        "Namaste {0} ji,\n\n"
        "Har mahine, apna Micro ATM na hone ki wajah se aapka kamaya hua "
        "paisa kisi aur ke paas ja raha hai.\n\n"
        "Jinke paas apna Micro ATM hai, unki average monthly kamai *{1}* "
        "hai. Jinke paas nahi hai, unki sirf *{2}* — matlab har mahine "
        "*{3}* ka nuksan, sirf isi ek wajah se.\n\n"
        "Is *{4}*, yeh nuksan rokiye — Micro ATM ki keemat ₹10,000 ki jagah "
        "sirf *{5}*, sirf {6} tak.\n\n"
        "Ek baar apna ho jaaye, toh har transaction ka poora fayda hamesha "
        "aapka — kisi aur Micro ATM ya kisi aur CSP ke paas nahi.\n\n"
        "Offer khatam hone se pehle abhi order kijiye:\n"
        "https://orders.kiosk.eko.in:8443/csp-growth-platform/order.html"
    ),
    "PRINTER": (
        "Namaste {0} ji,\n\n"
        "Har mahine, apna Passbook Printer na hone ki wajah se aapka "
        "kamaya hua paisa kisi aur ke paas ja raha hai.\n\n"
        "Jinke paas apna Passbook Printer hai, unki average monthly kamai "
        "*{1}* hai. Jinke paas nahi hai, unki sirf *{2}* — matlab har "
        "mahine *{3}* ka nuksan, sirf isi ek wajah se.\n\n"
        "Is *{4}*, yeh nuksan rokiye — Passbook Printer ki keemat ₹42,000 "
        "ki jagah sirf *{5}*, sirf {6} tak.\n\n"
        "Ek baar apna ho jaaye, toh har transaction ka poora fayda hamesha "
        "aapka — kisi aur Passbook Printer ya kisi aur CSP ke paas nahi.\n\n"
        "Offer khatam hone se pehle abhi order kijiye:\n"
        "https://orders.kiosk.eko.in:8443/csp-growth-platform/order.html"
    ),
}

TEMPLATE_VARIABLES = [
    "CSP name",                                    # {{1}}
    "Owner avg monthly income",                    # {{2}}
    "Non-owner avg monthly income",                # {{3}}
    "Monthly loss (owner avg minus non-owner avg)",  # {{4}}
    "Occasion name",                               # {{5}}
    "Offer price (= config.PRODUCTS[id].price, unchanged)",  # {{6}}
    "Offer deadline",                              # {{7}}
]


def _check_reference_prices_match_body() -> None:
    """Fails loudly at import time if REFERENCE_PRICE ever drifts from the
    number hardcoded into _BODY's text, instead of silently sending a
    Rakhi-era anchor price forever - same guard config.py runs for
    PRODUCTS.price vs the Google Form's option text."""
    for pid, price in REFERENCE_PRICE.items():
        expected = _rupees(price)
        if expected not in _BODY[pid]:
            raise RuntimeError(
                f"whatsapp._BODY[{pid!r}] does not contain {expected!r} - "
                f"REFERENCE_PRICE[{pid!r}] has changed without updating the "
                f"hardcoded text in _BODY (which needs a fresh CERF "
                f"submission to actually take effect, since it's no longer "
                f"a variable)")


_check_reference_prices_match_body()


# ---------------------------------------------------------------- composing
def _compose_from(target: dict, product, *, occasion: str, deadline_text: str) -> dict:
    """Shared by compose() (calendar-driven) and compose_event() (trigger-
    driven, see outreach.py's click-nudge track) - both fill the exact same
    approved template, just from different sources for {{5}}/{{7}}. See
    design discussion 2026-08-31: reusing this template for non-occasion
    sends needs no new CERF submission as long as the STATIC text is
    untouched - only what goes into these two variables differs."""
    owner_avg, non_owner_avg = EARNINGS[product.id]
    # Computed from the SAME rounded figures shown in the message, not from
    # the unrounded source data - so a CSP who does the subtraction
    # themselves gets the exact number just stated, never an off-by-~100
    # mismatch from independently-rounded figures.
    monthly_loss = owner_avg - non_owner_avg
    params = [
        target["name"],
        _rupees(owner_avg),
        _rupees(non_owner_avg),
        _rupees(monthly_loss),
        occasion,
        _rupees(product.price),
        deadline_text,
    ]
    text = _BODY[product.id].format(*params)
    return {
        "to": target["mobile"],
        "templateId": TEMPLATE_ID[product.id],
        "template": TEMPLATE_NAME[product.id],   # record-keeping only - not sent to goinfinito
        "language": TEMPLATE_LANGUAGE,
        "params": params,
        "text": text,
    }


def compose(target: dict, product, offer: Optional[dict] = None) -> dict:
    """Builds today's CALENDAR-driven offer message for `product`. Pure -
    sends nothing.

    `offer` is normally left None so this looks up
    offer_calendar.active_offer() itself; a caller may pass one explicitly
    (previews, tests) to render a specific day's/occasion's message without
    waiting for the calendar date to arrive.

    Raises NoActiveOfferError if neither the calendar nor `offer` has
    anything active for this product today - callers must treat that as
    "hold, nothing to send", never fall back to inventing a price."""
    offer = offer or offer_calendar.active_offer(product.id)
    if offer is None:
        raise NoActiveOfferError(f"No active offer for {product.id} today")
    return _compose_from(target, product, occasion=offer["occasion"],
                         deadline_text=_format_date(offer["endDate"]))


# Fixed per 2026-08-31 design decision - "Is waqt" is the generic (non-
# festival) noun that still reads naturally in the template's "Is {{5}},
# yeh nuksan rokiye" sentence. Not a variable of its own choosing per call -
# every event-triggered send uses the same phrase, so the wording stays
# predictable and auditable rather than improvised per trigger type.
EVENT_OCCASION_TEXT = "waqt"


def compose_event(target: dict, product, deadline_text: str) -> dict:
    """Builds an EVENT-triggered message (e.g. outreach.py's click-nudge
    track) from the same approved template as compose(), with no calendar
    occasion involved. `deadline_text` is a real, honourable-every-time
    commitment window (e.g. "3 din") - see this module's docstring on why
    it must never be a fabricated price deadline: the static template text
    still ends on "Offer khatam hone se pehle abhi order kijiye", so
    whatever goes here should be something genuinely true for every send,
    not just the occasion ones."""
    return _compose_from(target, product, occasion=EVENT_OCCASION_TEXT,
                         deadline_text=deadline_text)


# ------------------------------------------------------------------ sending
def _new_message_id() -> str:
    """A unique per-message id for goinfinito's messages[].id field and for
    correlating a later DLR webhook back to this specific send. 32 hex
    chars - goinfinito's own example ("abc123unique24charsid") wasn't a
    strict length spec, just an illustrative placeholder, so this errs on
    the side of more entropy rather than trying to match it exactly."""
    return uuid.uuid4().hex


def send(*, to: str, templateId: str, language: str, params: list, template: str = "",
          text: str = "") -> dict:
    """Sends one template message via goinfinito. dry mode never opens a
    connection - it returns exactly what a live send would report, minus
    the network call, same contract as emailer.send().

    `template` (the name) and `text` (the rendered body) are accepted but
    NOT part of the goinfinito payload - goinfinito identifies the template
    purely by templateId, and sends no free-text body at all for a
    template message. They're kept as parameters so callers (outreach.py)
    can pass compose()'s whole dict through with **message without having
    to strip fields it doesn't need for the actual API call."""
    if MODE == "dry":
        return {"sent": False, "mode": "dry"}

    if not CERF_API_URL or not CERF_API_KEY or not CERF_FROM:
        raise RuntimeError("CERF_API_URL / CERF_API_KEY / CERF_FROM not configured - "
                           "set all three in .env before WHATSAPP_MODE=live can send anything")

    message_id = _new_message_id()
    # goinfinito's own field, NOT Meta's Cloud API shape - confirmed
    # 2026-08-27 from goinfinito's own integration docs. templateinfo is
    # "<numeric template ID>~<var1>~<var2>~..." - the template NAME must
    # NOT go here (goinfinito rejects it with "Incorrect Template Details",
    # since a name where an ID is expected shifts every variable by one).
    templateinfo = "~".join([str(templateId)] + [str(p) for p in params])
    payload = {
        "apiver": "1.0",
        "whatsapp": {
            "ver": "2.0",
            "dlr": {"url": CERF_DLR_URL},
            "messages": [{
                "coding": "1",
                "id": message_id,
                "msgtype": "1",
                "text": "",
                "templateinfo": templateinfo,
                "type": "",
                "filename": "",
                "contenttype": "",
                "mediadata": "",
                "b_urlinfo": "",
                "addresses": [{
                    "seq": "",
                    "to": to,
                    "from": CERF_FROM,
                    "tag": "",
                }],
            }],
        },
    }

    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        CERF_API_URL, data=body, method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {CERF_API_KEY}",
        })
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            resp_body = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as err:
        raise RuntimeError(f"goinfinito send failed: {err}") from err

    # status:"Success" alone is NOT sufficient - goinfinito can report
    # top-level Success while a specific guid still carries an error (e.g.
    # errorcode 28694), which is the actual signal a message was rejected.
    # Confirmed 2026-08-27 from goinfinito's own docs.
    if resp_body.get("status") != "Success":
        raise RuntimeError(f"goinfinito send failed: {resp_body}")
    guids = (resp_body.get("messageack") or {}).get("guids") or []
    errors = [e for g in guids for e in (g.get("errors") or [])]
    if errors:
        raise RuntimeError(f"goinfinito rejected the message: {errors}")

    return {"sent": True, "mode": "live", "messageId": message_id}
