"""
Bridges the standalone courier-tracking scripts at the repo root
(track_dtdc.py, track_delhivery.py, track_xpressbees.py, track_bluedart.py -
see each one's own docstring for what's confirmed vs. still unverified for
that courier) into the order pipeline: checks real courier status for
DISPATCHED orders that have a tracking number attached (via orders.dispatch,
or ops typing one straight into the response sheet - see
orders._try_attach_tracking_from_sheet), and turns a detected phase change
into either the real DELIVERED transition or an informational "still on its
way" email - see _notify_phase.

Orders dispatched via the sheet have no courier column to read - the
courier is guessed from the AWB's own format (classify_courier_candidates)
and confirmed against the real courier sites the first time this runs
(_resolve_pending_courier), not assumed.

Deliberately NOT wired into run.py's existing 15-minute auto-advance tick -
each check drives a real browser against a live courier site (DTDC/Blue Dart
specifically can take 10-30+ seconds per order across captcha retries),
which is much heavier than everything else on that loop. Give this its own,
longer-interval tick instead (config.TRACKING_CHECK_INTERVAL_HOURS also
double-duties as that interval, so a shipment is naturally re-checked no
more often than that).

Deployment note: DTDC and Blue Dart need headless=False (their sites block
plain headless Chrome - see track_dtdc.py's docstring for the WAF details).
That means whatever machine runs this needs a real display, or a virtual one
(Xvfb on Linux) - it will NOT work unmodified on a typical headless cloud
box. Delhivery and XpressBees have no such requirement and run headless.
"""

import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from . import config, emailer, orders, store

# Merged into the Device Monitor WITHOUT the courier automation (Playwright + Chromium + EasyOCR, ~2 GB): only
# classify_courier_candidates (AWB format guessing, used by orders.py) is needed there. The browser checks below run
# only if those packages and the track_*.py scripts are installed next to this package; otherwise they are a no-op.
try:
    from playwright.sync_api import sync_playwright

    BASE_DIR = Path(__file__).resolve().parent.parent
    if str(BASE_DIR) not in sys.path:
        sys.path.insert(0, str(BASE_DIR))

    import track_dtdc        # noqa: E402
    import track_delhivery   # noqa: E402
    import track_xpressbees  # noqa: E402
    import track_bluedart    # noqa: E402
except ImportError:
    sync_playwright = None


# Keyword checks, most-specific/most-final first. A results page is
# typically a whole timeline (every past event, not just the latest one), so
# e.g. both "out for delivery" and "delivered" text can appear on an
# already-delivered shipment's page - checking DELIVERED first means that
# case is never mis-classified as merely "out for delivery".
_PHASE_MARKERS = (
    ("DELIVERED", ("delivered",)),
    ("OUT_FOR_DELIVERY", ("out for delivery",)),
    ("IN_TRANSIT", ("in transit", "processing centre", "in-transit")),
    ("PICKED_UP", ("picked up", "pickup scheduled", "booked")),
)


def classify_phase(raw_result: str) -> Optional[str]:
    """Best-effort keyword search over whatever text a courier's result page
    returned. Returns None (no phase change recorded, no email sent) rather
    than guessing when nothing recognisable matches - e.g. a "not found"
    response, an error page, or genuinely novel wording none of the four
    courier scripts' testing ever saw. Silence is the safe failure mode
    here, not a wrong status landing in a CSP's inbox."""
    lowered = (raw_result or "").lower()
    for phase, markers in _PHASE_MARKERS:
        if any(marker in lowered for marker in markers):
            return phase
    return None


# Best-effort AWB-format classifier, used only when ops types a Tracking ID
# straight into the response sheet with no courier alongside it (see
# orders._try_attach_tracking_from_sheet - there is no "Courier" column,
# per the choice made when this was built: guess from the number itself
# rather than adding a second manual column). Real-world formats overlap
# too much for a single confident guess in every band - see the per-band
# comments below - so this returns candidates in most-likely-first order,
# and _resolve_pending_courier tries each in turn against the real courier
# site until one returns a result that isn't a not-found/invalid page. A
# wrong guess only ever costs one extra page load: classify_phase() above
# only acts on a recognised phase, so a mis-tried courier's "not found" text
# can never reach a CSP as a wrong status.
_DTDC_LETTER_FORMAT = re.compile(r"[A-Z]\d{8}")


def classify_courier_candidates(tracking_number: str) -> list:
    tn = (tracking_number or "").strip().upper()
    if not tn:
        return []

    # DTDC's distinctive shape: one letter + 8 digits (e.g. D57291883) -
    # no other courier here uses a single-letter prefix on a 9-char code.
    if _DTDC_LETTER_FORMAT.fullmatch(tn):
        return ["DTDC"]

    if tn.isdigit():
        length = len(tn)
        if 8 <= length <= 11:
            return ["BLUEDART"]
        if 12 <= length <= 13:
            # DTDC also issues pure-numeric ~12-digit codes; XpressBees'
            # numeric AWBs start around 13 digits - overlapping band.
            return ["DTDC", "XPRESSBEES"]
        if length == 14:
            # The one band with real, unavoidable ambiguity: this project's
            # own confirmed-live Delhivery AWB (17321480360496) is 14
            # digits, and XpressBees' commonest format is also a 14-digit
            # numeric code - format alone cannot tell these apart.
            return ["DELHIVERY", "XPRESSBEES"]
        if length == 15:
            return ["XPRESSBEES", "DELHIVERY"]
        if 16 <= length <= 20:
            return ["DELHIVERY"]
        return []

    # Alphanumeric, not pure-digit, and not DTDC's letter+8digits shape -
    # Delhivery's documented format is the most tolerant of letters mixed in.
    if 10 <= len(tn) <= 20:
        return ["DELHIVERY", "XPRESSBEES"]
    return []


def _track_dtdc(page, ctx, awb):
    return track_dtdc.track_one(page, ctx["reader"], awb, max_attempts=4)


def _track_bluedart(page, ctx, awb):
    return track_bluedart.track_one(page, ctx["reader"], awb, max_attempts=4)


def _track_delhivery(page, ctx, awb):
    return track_delhivery.track_one(page, awb)


def _track_xpressbees(page, ctx, awb):
    return track_xpressbees.track_one(page, awb)


_TRACK_FN = {
    "DTDC": _track_dtdc,
    "BLUEDART": _track_bluedart,
    "DELHIVERY": _track_delhivery,
    "XPRESSBEES": _track_xpressbees,
}

# Only DTDC and Blue Dart need a real (non-headless) browser and an EasyOCR
# reader - see the deployment note in this module's docstring.
_NEEDS_HEADED = {"DTDC", "BLUEDART"}
_NEEDS_OCR = {"DTDC", "BLUEDART"}


def _trackable_orders(state: dict) -> list:
    """DISPATCHED orders with a tracking number attached (see orders.dispatch
    and orders._try_attach_tracking_from_sheet) that haven't been checked in
    the last config.TRACKING_CHECK_INTERVAL_HOURS - re-checking a shipment
    every tick would just be hammering the courier's site for a status that
    hasn't had time to change. Courier itself may still be unset (a pending
    order - see attach_pending_tracking); run_tracking_checks resolves those
    before running the normal per-courier batch."""
    due = []
    for order in state["orders"]:
        if order["status"] != "DISPATCHED":
            continue
        if not order.get("trackingNumber"):
            continue
        last_check = order.get("lastTrackingCheckAt")
        if last_check:
            then = datetime.fromisoformat(last_check)
            if then.tzinfo is None:
                then = then.replace(tzinfo=timezone.utc)
            age_hours = (datetime.now(timezone.utc) - then).total_seconds() / 3600
            if age_hours < config.TRACKING_CHECK_INTERVAL_HOURS:
                continue
        due.append(order)
    return due


def _notify_phase(state: dict, order: dict, phase: str) -> None:
    """DELIVERED goes through the real state-machine transition (fires the
    existing DELIVERED email + ownership flip via orders.transition,
    imported here rather than at module load to avoid a circular import
    with orders.py) - one code path for "this order is delivered" whether
    the timer or real tracking noticed it first. Every earlier phase is
    informational only: it does not change order["status"], it just lets
    the CSP know their already-DISPATCHED order is moving."""
    if phase == "DELIVERED":
        orders.transition(state, order["id"], "DELIVERED", by="system:tracking-check",
                          note=f"{order['courier']} confirmed delivery (AWB {order['trackingNumber']})")
        return

    if not order.get("email"):
        store.audit(state, "NOTIFICATION_SKIPPED", {
            "orderId": order["id"], "status": f"TRACKING_{phase}",
            "reason": "no email on file for this CSP",
        })
        return

    product = config.PRODUCTS.get(order["productId"])
    product_name = product.name if product else order["productId"]
    message = emailer.compose_tracking_update(order, product_name, phase)

    try:
        outcome = emailer.send(to=order["email"], **message)
    except Exception as err:                          # noqa: BLE001 - best-effort side effect
        store.audit(state, "NOTIFICATION_FAILED", {
            "orderId": order["id"], "status": f"TRACKING_{phase}", "to": order["email"], "reason": str(err),
        })
        return

    if outcome["sent"]:
        state["outreach"].append({
            "id": len(state["outreach"]) + 1,
            "cspCode": order["cspCode"], "name": order["name"],
            "productId": order["productId"], "channel": "EMAIL",
            "to": order["email"], "subject": message["subject"], "html": message["html"],
            "delivered": True, "mode": outcome["mode"], "at": store.now_iso(),
            "kind": f"TRACKING_{phase}", "orderId": order["id"],
        })
    else:
        store.audit(state, "NOTIFICATION_COMPOSED", {
            "orderId": order["id"], "status": f"TRACKING_{phase}", "to": order["email"], "mode": outcome["mode"],
        })


def _record_and_notify(state: dict, order: dict, result: dict, notified: list) -> None:
    raw_result = result.get("raw_result", "")
    store.record_tracking_check(state, order["id"], raw_result=raw_result)
    phase = classify_phase(raw_result) if result.get("success") else None
    if phase and phase != order.get("trackingPhase"):
        order["trackingPhase"] = phase
        _notify_phase(state, order, phase)
        notified.append(order["id"])


def _resolve_pending_courier(state: dict, order: dict, ctx: dict, candidates: list,
                              *, delay_seconds: float) -> bool:
    """Tries each format-guessed candidate (see classify_courier_candidates)
    in its own short-lived browser, matched to that candidate's own
    headless/OCR needs, until one comes back success - then locks it in via
    store.attach_tracking so every later check goes straight to the
    confirmed courier. Returns True if a phase-worthy result was recorded
    (caller still needs record_tracking_check either way to reset the
    cooldown), same booking as the confirmed-courier path below."""
    for candidate in candidates:
        track_fn = _TRACK_FN[candidate]
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=candidate not in _NEEDS_HEADED)
                page = browser.new_page()
                try:
                    result = track_fn(page, ctx, order["trackingNumber"])
                finally:
                    browser.close()
        except Exception as err:                            # noqa: BLE001 - one bad candidate must not kill the batch
            store.audit(state, "TRACKING_CHECK_FAILED", {
                "orderId": order["id"], "courier": candidate, "reason": str(err),
            })
            time.sleep(delay_seconds)
            continue

        if result.get("success"):
            store.attach_tracking(state, order["id"], courier=candidate,
                                   tracking_number=order["trackingNumber"])
            store.audit(state, "TRACKING_COURIER_RESOLVED", {
                "orderId": order["id"], "courier": candidate, "triedFirst": candidates[0],
            })
            return result
        time.sleep(delay_seconds)

    return None


def run_tracking_checks(state: dict, *, delay_seconds: float = 5.0) -> list:
    """Checks real courier status for every order due a check (see
    _trackable_orders) and notifies the CSP on any recognised phase change.
    Returns the list of order IDs notified this run (DELIVERED transition or
    informational email) - empty most of the time, same convention as
    orders.auto_advance. The caller should save state after calling this
    regardless of the return value: record_tracking_check below updates
    lastTrackingCheckAt on every order checked, not only the ones that got
    a notification.

    Two passes: orders with no confirmed courier yet (see
    attach_pending_tracking) are resolved first, one candidate courier at a
    time in their own browser - see _resolve_pending_courier. Everything
    else launches at most one headless and one headed browser for the whole
    remaining batch (grouped by each courier's needs - see _NEEDS_HEADED),
    not one per order, sharing one EasyOCR reader (loading its model is the
    slow part) across every DTDC/Blue Dart lookup in this run.
    """
    if sync_playwright is None:
        return []                      # courier automation not installed (see the imports above)
    due = _trackable_orders(state)
    if not due:
        return []

    pending = [o for o in due if not o.get("courier")]
    confirmed = [o for o in due if o.get("courier")]

    pending_candidates = {o["id"]: classify_courier_candidates(o["trackingNumber"]) for o in pending}

    needs_ocr = (any(o["courier"] in _NEEDS_OCR for o in confirmed)
                 or any(c in _NEEDS_OCR for cands in pending_candidates.values() for c in cands))
    reader = None
    if needs_ocr:
        import easyocr
        reader = easyocr.Reader(["en"], gpu=False, verbose=False)
    ctx = {"reader": reader}

    notified = []

    for order in pending:
        candidates = pending_candidates[order["id"]]
        if not candidates:
            # Format didn't match anything recognisable - orders.py already
            # left a remark for a human; nothing more to try here, and no
            # point re-attempting every cooldown period since the format
            # isn't going to change.
            store.record_tracking_check(
                state, order["id"],
                raw_result="(unrecognised Tracking ID format - no courier candidate)")
            continue
        result = _resolve_pending_courier(state, order, ctx, candidates, delay_seconds=delay_seconds)
        if result is None:
            store.record_tracking_check(
                state, order["id"],
                raw_result=f"(tried {', '.join(candidates)} - none recognised this AWB)")
            continue
        _record_and_notify(state, order, result, notified)

    by_courier: dict = {}
    for order in confirmed:
        by_courier.setdefault(order["courier"], []).append(order)

    headless_couriers = [c for c in by_courier if c not in _NEEDS_HEADED]
    headed_couriers = [c for c in by_courier if c in _NEEDS_HEADED]

    if by_courier:
        with sync_playwright() as p:
            for headless_mode, couriers_this_pass in ((True, headless_couriers), (False, headed_couriers)):
                if not couriers_this_pass:
                    continue
                browser = p.chromium.launch(headless=headless_mode)
                page = browser.new_page()
                for courier in couriers_this_pass:
                    track_fn = _TRACK_FN[courier]
                    for order in by_courier[courier]:
                        try:
                            result = track_fn(page, ctx, order["trackingNumber"])
                        except Exception as err:            # noqa: BLE001 - one bad check must not kill the batch
                            store.audit(state, "TRACKING_CHECK_FAILED", {
                                "orderId": order["id"], "courier": courier, "reason": str(err),
                            })
                            time.sleep(delay_seconds)
                            continue

                        _record_and_notify(state, order, result, notified)
                        time.sleep(delay_seconds)
                browser.close()

    return notified
