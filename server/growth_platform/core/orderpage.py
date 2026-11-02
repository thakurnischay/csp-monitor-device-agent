"""
Self-serve order page: identity lookup (CSP code or name, disambiguated by
mobile last-4 when more than one CSP shares a name) and order submission.

Deliberately its own module, not folded into run.py or orders.py - this is
the one part of the platform an unauthenticated CSP's browser talks to
directly (see README's "Not built yet: Auth"), so keeping its surface small
and separate makes it easy to see everything a stranger on the internet can
reach: a CSP's name/code/state/district and mobile LAST FOUR DIGITS ONLY
(see public_profile), never the full record.

Design decisions this module encodes (see design/order-page-mockup.html and
design/order-page-build-plan.html for the fuller reasoning):
  - CSP code is checked before name and always wins if it matches - it's the
    one field confirmed unique across the whole base (535/535).
  - A name match that isn't unique is resolved by mobile last-4, never
    exposed until it's the ONLY thing separating two real people - see
    find_by_code_or_name / lookup.
  - Eligibility (can this CSP order THIS product right now) reuses engine.py's
    OWNED/GAP_DISPUTED verdicts and config.BLOCKED_FOR/requires_owned rules
    directly, so self-serve can never approve an order the campaign engine
    itself wouldn't also consider valid. GAP_DISPUTED is blocked outright
    (decided 2026-08-20) - ownership itself is unresolved, so self-serve is
    not the place to hand over a device.
  - The amount is ALWAYS config.PRODUCTS[...].price, never anything the CSP
    types - see submit(). A static QR (see design/payment-gateway-workflow.html)
    carries no amount, so the CSP types one into their own UPI app, but that
    number never reaches the order record; only the bank-side amount/UTR
    checks in orders.py can still catch a real mismatch.

  - The delivery address is collected here and stored ON THE ORDER, unlike
    the Google Form path. The form does collect Address/Landmark/City, but
    config.RESPONSE_HEADER_MAP never mapped them, so the platform has only
    ever read State and Pincode from a submission and ops reads the full
    address off the response sheet itself. An order-page order has no sheet
    row to fall back on whenever the mirror is off
    (config.ORDER_PAGE_SHEET_MIRROR_URL), so it has to carry its own
    address or there is nothing to ship against. Collecting a real pincode
    also re-arms orders._pincode_zone_mismatch's wrong-address hold, which
    would otherwise silently never fire for these orders.
"""

import json
import re
import threading
import time
import urllib.error
import urllib.request
from typing import Optional

from . import config, engine, forms, orders, store

# --------------------------------------------------------------- rate limit
# Lookup has no login (see README's "Not built yet: Auth"), so without this a
# script could enumerate the CSP directory by trying names/codes. Best-effort
# and per-process, in-memory - resets on restart, and only actually correct
# with the single-process deployment Phase 1's lock already assumes. Not
# meant to be a hardened defence, just enough friction that scraping the
# whole base takes more than one request.
_RATE_LOCK = threading.Lock()
_RATE_WINDOW_SECONDS = 60
_RATE_MAX_PER_WINDOW = 20
_rate_hits: dict = {}   # ip -> [unix timestamps within the current window]


def rate_limited(ip: str) -> bool:
    now = time.time()
    with _RATE_LOCK:
        hits = [t for t in _rate_hits.get(ip, []) if now - t < _RATE_WINDOW_SECONDS]
        hits.append(now)
        _rate_hits[ip] = hits
        # Unbounded growth guard: an attacker cycling fake IPs (X-Forwarded-For
        # is client-supplied, not verified) could otherwise grow this dict
        # forever. Cheap, approximate cleanup - exact eviction isn't worth it
        # for a best-effort limiter.
        if len(_rate_hits) > 5000:
            _rate_hits.clear()
        return len(hits) > _RATE_MAX_PER_WINDOW


# ------------------------------------------------------------------ lookup
def _norm(s: Optional[str]) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def _mobile_last4(mobile: Optional[str]) -> str:
    digits = re.sub(r"\D", "", str(mobile or ""))
    return digits[-4:] if len(digits) >= 4 else ""


def find_by_code_or_name(csps: list, query: str) -> list:
    """CSP code match short-circuits to it alone if found (it's unique -
    535/535, see the memory doc). Name matching is exact-normalised, not
    substring: a substring match on "Kumar" would fish out hundreds of CSPs
    in one query, which would make the rate limit above nearly meaningless.
    """
    q = _norm(query)
    if not q:
        return []
    by_code = [c for c in csps if _norm(c.get("cspCode")) == q]
    if by_code:
        return by_code
    return [c for c in csps if _norm(c.get("name")) == q]


def eligibility(csp: dict, reconciliations: dict) -> dict:
    """Per-product ownership eligibility, reusing the exact verdict
    functions engine.py's campaign queues are built from - see this
    module's docstring for why. Returns one entry per config.PRODUCTS key:
    {"orderable": bool, "reason": str|None, "price": int|None}."""
    code = csp["cspCode"]
    ownership = {
        "MATM": engine.assess_matm(csp, reconciliations.get(f"{code}:MATM")),
        "PRINTER": engine.assess_printer(csp, reconciliations.get(f"{code}:PRINTER")),
    }
    band = engine.affordability_band(csp)

    result = {}
    for product_id, product in config.PRODUCTS.items():
        verdict = ownership[product_id]["verdict"]
        if verdict == engine.OWNED:
            result[product_id] = {"orderable": False, "reason": "already-owned", "price": None}
        elif verdict == engine.GAP_DISPUTED:
            result[product_id] = {"orderable": False, "reason": "needs-rm", "price": None}
        elif product.requires_owned and \
                ownership[product.requires_owned]["verdict"] != engine.OWNED:
            result[product_id] = {"orderable": False, "reason": "requires-matm", "price": None}
        elif band.id in config.BLOCKED_FOR.get(product_id, ()):
            result[product_id] = {"orderable": False, "reason": "affordability-band", "price": None}
        else:
            result[product_id] = {"orderable": True, "reason": None, "price": product.price}
    return result


def public_profile(csp: dict, reconciliations: dict) -> dict:
    """The ONLY shape a CSP's data ever leaves this module in. No full
    mobile, no email, no commission, no RM name - see this module's
    docstring."""
    return {
        "cspCode": csp["cspCode"],
        "name": csp["name"],
        "state": csp.get("state"),
        "district": csp.get("district"),
        # The same value normalised to one of forms._STATE_OPTIONS, so the
        # delivery dropdown can pre-select it. None when the sheet's spelling
        # isn't recognised ("Nct Of Delhi" -> "Delhi" is handled, anything
        # unmapped isn't) - the CSP then picks it themselves rather than the
        # page guessing at where to ship a device.
        "stateNormalised": forms._normalise_state(csp.get("state")),
        "mobileLast4": _mobile_last4(csp.get("mobile")),
        "eligibility": eligibility(csp, reconciliations),
    }


def lookup(csps: list, reconciliations: dict, query: str, last4: Optional[str]) -> dict:
    """
    Returns one of:
      {"found": False}
      {"found": False, "needsRm": True}     - ambiguous and unresolvable by
                                               last4 (incl. blank-mobile CSPs
                                               - see the memory doc's "1 of
                                               33 groups" finding)
      {"found": "multiple", "count": N}     - name matched >1, give the
                                               caller a last4 prompt without
                                               ever listing who they are
      {"found": True, "csp": {...}}         - see public_profile
    """
    matches = find_by_code_or_name(csps, query)
    if not matches:
        return {"found": False}

    if len(matches) > 1:
        last4 = (last4 or "").strip()
        if not last4:
            return {"found": "multiple", "count": len(matches)}
        matches = [c for c in matches if _mobile_last4(c.get("mobile")) == last4]
        if len(matches) != 1:
            return {"found": False, "needsRm": True}

    return {"found": True, "csp": public_profile(matches[0], reconciliations)}


# -------------------------------------------------------------------- link
def order_page_url(token: Optional[str] = None) -> str:
    """The link a CSP is actually given.

    Without a token: the plain, identical-for-everyone page - what a
    WhatsApp template's link variable carries. That was the whole point of
    the order page's search-by-code/name flow: WhatsApp needs one static
    link, so the page has to be able to work out who the CSP is from what
    they type, rather than from the URL.

    With a token: the same page, pre-resolved via resolve_token() so the CSP
    types nothing. Campaign EMAIL does not use this - it links to
    /go/<token> instead (see emailer.click_tracked_form_url), which logs the
    click first and then redirects here when config.ORDER_PAGE_LIVE is on.
    This form exists for anywhere a tokenised link has to be written down
    directly rather than redirected to - a WhatsApp send that issues its own
    tokens, once that gets built, being the obvious one.
    """
    base = f"{config.ORDER_PAGE_BASE_URL}/order.html"
    return f"{base}?t={token}" if token else base


# ------------------------------------------------------------- token entry
def resolve_token(state: dict, csps_by_code: dict, token: str) -> dict:
    """
    The email-channel entry point (see design/order-page-build-plan.html's
    Phase 3 note) - resolves a campaign token straight to a CSP with zero
    typing, distinct from lookup() above in trust model: a token was only
    ever emailed to that one CSP's own inbox, so it doesn't need last-4
    disambiguation or the same guessing-attack posture as name/code search.
    Still passed through orderpage.rate_limited by the caller (run.py) - not
    because resolving is a guessing attack, but because nothing should be
    hammerable at unlimited speed from an unauthenticated route.

    Returns one of:
      {"found": False}                                    - unknown token,
                                                              or the CSP it
                                                              names is gone
                                                              from the base;
                                                              caller falls
                                                              back to search
      {"found": True, "alreadyUsed": True,
       "csp": {"name", "cspCode"}, "order": {...} | None}  - this token
                                                              already became
                                                              an order; show
                                                              status, don't
                                                              let them order
                                                              again
      {"found": True, "alreadyUsed": False,
       "csp": {...public_profile...},
       "suggestedProductId": str}                          - pre-select this
                                                              product, but
                                                              the CSP can
                                                              still pick the
                                                              other one (see
                                                              eligibility) -
                                                              never forced
    """
    record = store.lookup_token(state, (token or "").strip())
    if record is None:
        return {"found": False}

    csp = csps_by_code.get(record["cspCode"])
    if csp is None:
        # CSP existed when the token was issued, isn't in the current base
        # any more (a re-import dropped or renumbered them) - nothing safe
        # to resolve to.
        return {"found": False}

    if record.get("used"):
        order = store.get_order(state, record["orderId"]) if record.get("orderId") else None
        return {
            "found": True, "alreadyUsed": True,
            "csp": {"name": csp["name"], "cspCode": csp["cspCode"]},
            "order": ({"status": order["status"], "productId": order["productId"],
                       "createdAt": order["createdAt"]} if order else None),
        }

    return {
        "found": True, "alreadyUsed": False,
        "csp": public_profile(csp, state["reconciliations"]),
        "suggestedProductId": record.get("productId"),
    }


# ------------------------------------------------------------------ submit
def delivery_states() -> list:
    """The state options the delivery dropdown offers. Deliberately
    forms._STATE_OPTIONS - the same 14 the Google Form's own State dropdown
    uses, and exactly the keys config.POSTAL_ZONE_BY_STATE knows about, so
    a state picked here can always be zone-checked against the pincode
    (orders._pincode_zone_mismatch). A free-text state would silently
    disable that check."""
    return list(forms._STATE_OPTIONS)


def submit(state: dict, csps_by_code: dict, *, csp_code: str, product_id: str,
           payment_method: str, utr: str,
           address_line: str = "", landmark: str = "", city: str = "",
           pincode: str = "", delivery_state: str = "") -> dict:
    """Validates and creates the order - the one place a self-serve
    order-page submission becomes a real order. Re-derives eligibility from
    scratch rather than trusting whatever lookup() told the browser earlier
    (a re-import or a reconciliation could have changed the answer since).
    Raises ValueError on any rejection - the caller (run.py) maps that to a
    400, same convention order_action's manual overrides already use.

    Must be called with state already loaded under store.transaction() -
    this function mutates it (issues a token, appends the order) but does
    not load or save it itself, same division of responsibility as
    orders.process_submission below it.
    """
    csp = csps_by_code.get((csp_code or "").strip())
    if csp is None:
        raise ValueError("Unknown CSP code")

    product = config.PRODUCTS.get(product_id)
    if product is None:
        raise ValueError(f"Unknown product: {product_id}")

    elig = eligibility(csp, state["reconciliations"])[product_id]
    if not elig["orderable"]:
        raise ValueError(f"Not orderable: {elig['reason']}")

    if payment_method not in ("bank", "qr"):
        raise ValueError(f"Unknown payment method: {payment_method}")

    utr = (utr or "").strip()
    if not utr:
        raise ValueError("UTR / reference number is required")

    # Delivery address. Validated here, not just in the browser - the
    # endpoint is public, so client-side checks are a convenience for the
    # CSP, never the guarantee. Landmark stays optional, same as the Google
    # Form has it.
    address_line = (address_line or "").strip()
    city = (city or "").strip()
    landmark = (landmark or "").strip()
    if not address_line:
        raise ValueError("Delivery address is required")
    if not city:
        raise ValueError("City / town is required")

    pincode = re.sub(r"\D", "", str(pincode or ""))
    if len(pincode) != 6:
        raise ValueError("Pincode must be exactly 6 digits")

    delivery_state = (delivery_state or "").strip()
    if delivery_state not in forms._STATE_OPTIONS:
        raise ValueError("Select a delivery state from the list")

    # Same token mechanism the email path uses (forms.issue_and_build) - an
    # order-page order should be traceable the same way, not a special case.
    issued = forms.issue_and_build(state, csp, product_id)

    submission = {
        "cspCode": csp["cspCode"],
        "cspName": csp["name"],
        "campaignProduct": config.FORM_CAMPAIGN_PRODUCT_LABEL.get(product_id, product_id),
        "token": issued["token"],
        # ALWAYS the campaign price - see this module's docstring. Never
        # anything from the request body.
        "amountPaid": product.price,
        "utr": utr,
        # The DELIVERY state the CSP confirmed, not their on-file state -
        # orders._pincode_zone_mismatch compares the pincode's zone against
        # this, so it has to describe where the device is actually going.
        "state": delivery_state,
        "pincode": pincode,
        "addressLine": address_line,
        "landmark": landmark,
        "city": city,
        "mobile": csp.get("mobile"),
        "email": csp.get("email"),
        "paymentMethod": payment_method,
    }
    return orders.process_submission(submission, csps_by_code, state)


# ------------------------------------------------------------ sheet mirror
def mirror_to_sheet(order: dict) -> bool:
    """
    Best-effort POST of one order-page order to the Apps Script web app
    (google-apps-script/order_sink.gs) that appends it into the SAME
    response sheet the Google Form path already writes to
    (config.RESPONSE_SHEET_URL) - so finance keeps exactly one place to
    look regardless of which path a CSP ordered through.

    Deliberately called AFTER the order is already saved to state.json (see
    run.py's order_page_submit) and never raises - state.json is the source
    of truth (see this module's docstring on why), this is a convenience
    copy. A failed mirror means finance's sheet is momentarily missing a
    row, not that the order itself is lost; retrying that gap is future
    work, not something silently invented here.

    No-ops (returns False) when config.ORDER_PAGE_SHEET_MIRROR_URL isn't
    set - the feature is simply off until someone deploys order_sink.gs and
    supplies the URL it prints (same one-time manual step
    GOOGLE_FORM_SETUP.md already documents for the form itself).
    """
    url = config.ORDER_PAGE_SHEET_MIRROR_URL
    if not url:
        return False

    payload = {
        "cspCode": order["cspCode"],
        "cspName": order.get("name") or "",
        "state": order.get("deliveryState") or "",
        "pincode": order.get("pincode") or "",
        "addressLine": order.get("addressLine") or "",
        "landmark": order.get("landmark") or "",
        "city": order.get("city") or "",
        "campaignProduct": config.FORM_CAMPAIGN_PRODUCT_LABEL.get(
            order["productId"], order["productId"]),
        "token": order.get("token") or "",
        "amountPaid": order.get("amountPaid"),
        "utr": order.get("utr") or "",
        "email": order.get("email") or "",
        "mobile": order.get("mobile") or "",
        "orderId": order["id"],
        "paymentMethod": order.get("paymentMethod") or "",
    }
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return 200 <= resp.status < 300
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as err:
        print(f"[order-page] sheet mirror failed for {order['id']}: {err}", flush=True)
        return False
