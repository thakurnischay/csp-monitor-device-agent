"""
Order lifecycle: from a form submission through to a device confirmed
delivered.

    SUBMITTED --(instant)--> "Order Received" email (zero conditions)
              --(finance types a credited remark in the response sheet)-->
                PAYMENT_VERIFIED --(AUTO_DISPATCH_AFTER_DAYS later)-->
                DISPATCHED --(AUTO_DELIVER_AFTER_DAYS later)--> DELIVERED
    (REJECTED: manual override only, for whatever finance flags as wrong)

Two different kinds of "automatic" live in this file, and it matters which:

  - PAYMENT_VERIFIED genuinely depends on a human: finance checks the bank
    statement and types something like "Credited" into a "Finance Remark"
    column on the response sheet (not a form field - a column finance adds
    and edits directly). import_responses() below picks that up on its next
    poll (run.py polls it on a background schedule, no one has to open the
    dashboard) and fires PAYMENT_VERIFIED + its email. The amount/UTR match
    and the duplicate-UTR check still run even after finance says credited -
    safety nets against a finance typo, not the primary gate.
  - DISPATCHED and DELIVERED are pure timers (config.AUTO_DISPATCH_AFTER_DAYS
    / AUTO_DELIVER_AFTER_DAYS), advanced by auto_advance() below - not a real
    "the courier scanned it" event, because there is no courier integration
    to produce one. One exception: a PAYMENT_VERIFIED order whose delivery
    pincode doesn't match its declared state's postal zone
    (_pincode_zone_mismatch, config.POSTAL_ZONE_BY_STATE) never auto-advances
    to DISPATCHED - it's held for a human to check the address, same idea as
    the duplicate-UTR/amount safety nets below.

Two rules that matter most in here:

  1. Has mATM / Has Passbook Printer is updated ONLY at DELIVERED - never on
     submission, never on payment. A paid-for order that never arrives must
     not read as coverage gained. Ownership is flipped by writing into
     state["reconciliations"] - the same override mechanism engine.py uses
     for RM phone confirmations.
  2. Every stage - SUBMITTED, PAYMENT_VERIFIED, DISPATCHED, DELIVERED - emails
     the CSP automatically the moment the order enters it. Nobody sends these
     by hand.
"""

import csv
import io
import re
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Optional

from . import config, emailer, store

_SHEET_ID_RE = re.compile(r"/spreadsheets/d/([a-zA-Z0-9_-]+)")
_GID_RE = re.compile(r"[?&#]gid=(\d+)")


# Allowed transitions. Anything not listed here is a mistake, not a business
# decision the caller gets to make silently. REJECTED is reachable up to (not
# including) DISPATCHED - once a device has shipped, rejecting the order
# doesn't undo that, so the model doesn't allow it.
_TRANSITIONS = {
    "SUBMITTED":        ("PAYMENT_VERIFIED", "REJECTED"),
    "PAYMENT_VERIFIED": ("DISPATCHED", "REJECTED"),
    "DISPATCHED":       ("DELIVERED",),
    "DELIVERED":        (),
    "REJECTED":         (),
}

# Which email template fires on which status - see _notify(). SUBMITTED isn't
# here because it's not reached via transition() (it's the initial status
# store.create_order sets directly) - process_submission sends its email
# itself, right after creating the order.
_EMAIL_ON_STATUS = {
    "PAYMENT_VERIFIED": emailer.compose_payment_confirmation,
    "DISPATCHED": emailer.compose_dispatch_notification,
    "DELIVERED": emailer.compose_delivery_notification,
}


def _product_id_for_label(label: str) -> Optional[str]:
    label = (label or "").strip()
    for pid, campaign_label in config.FORM_CAMPAIGN_PRODUCT_LABEL.items():
        if campaign_label == label:
            return pid
    return None


def _pincode_zone_mismatch(delivery_state: Optional[str], pincode: Optional[str]) -> bool:
    """True only when both a recognised state and a well-formed 6-digit
    pincode are present AND the pincode's leading digit doesn't match that
    state's postal zone (config.POSTAL_ZONE_BY_STATE) - see that dict's
    comment for the check's known blind spots. Anything not confidently
    checkable (missing state, "Other", a non-6-digit pincode) returns False:
    this is a hold-for-a-look signal, not a validator, so it must never flag
    on missing data alone."""
    pincode = (pincode or "").strip()
    expected_zone = config.POSTAL_ZONE_BY_STATE.get((delivery_state or "").strip())
    if not expected_zone or len(pincode) != 6 or not pincode.isdigit():
        return False
    return pincode[0] != expected_zone


def process_submission(submission: dict, csps_by_code: dict, state: dict, *,
                       response_id: Optional[str] = None) -> dict:
    """
    Turns one form-response row directly into a SUBMITTED order and sends
    the "Order Received" email immediately - the only unconditional step in
    this whole pipeline. Nothing about the submission is checked or judged
    here; PAYMENT_VERIFIED is a separate, later event (see
    _try_finance_verify) triggered by finance, not by this function. If the
    submitted "Campaign Product" text doesn't match a known product, the raw
    text is kept as the order's productId (with price 0) rather than guessed
    at, so a human notices and fixes it instead of it silently becoming the
    wrong product.

    `response_id` links this order back to its response-sheet row so a later
    poll can find it again once finance has typed a remark - see
    import_responses().
    """
    csp_code = (submission.get("cspCode") or "").strip() or "UNKNOWN"
    csp = csps_by_code.get(csp_code)

    product_id = (_product_id_for_label(submission.get("campaignProduct"))
                  or (submission.get("campaignProduct") or "UNKNOWN").strip())
    product = config.PRODUCTS.get(product_id)
    price = product.price if product else 0

    try:
        amount_paid = float(submission.get("amountPaid") or 0)
    except (TypeError, ValueError):
        amount_paid = 0.0

    delivery_state = (submission.get("state") or "").strip() or None
    pincode = (submission.get("pincode") or "").strip() or None
    address_flag = _pincode_zone_mismatch(delivery_state, pincode)
    remarks = (
        f"Pincode {pincode} doesn't look right for {delivery_state} - "
        "verify the delivery address before this ships."
        if address_flag else ""
    )

    order = store.create_order(
        state,
        csp_code=csp_code,
        name=(csp.get("name") if csp else None) or submission.get("cspName") or csp_code,
        product_id=product_id, price=price,
        token=submission.get("token", ""), amount_paid=amount_paid,
        utr=(submission.get("utr") or "").strip(),
        mobile=(csp.get("mobile") if csp else None) or submission.get("mobile"),
        email=(csp.get("email") if csp else None) or submission.get("email"),
        status="SUBMITTED",
        remarks=remarks,
        response_id=response_id,
        delivery_state=delivery_state,
        pincode=pincode,
        address_flag=address_flag,
        payment_method=submission.get("paymentMethod"),
        address_line=submission.get("addressLine"),
        landmark=submission.get("landmark"),
        city=submission.get("city"),
    )
    _notify(state, order, "SUBMITTED", emailer.compose_submission_received)
    return {"orderId": order["id"], "cspCode": csp_code, "productId": product_id}


def _find_order_with_utr(state: dict, utr: str, *, exclude_order_id: str = None) -> Optional[dict]:
    """A REJECTED order's UTR doesn't stay claimed forever - if a human
    rejected it, that UTR is free to be claimed again (e.g. the CSP
    resubmits correctly after a mixup)."""
    utr_norm = (utr or "").strip().lower()
    if not utr_norm:
        return None
    return next((o for o in state["orders"]
                 if o["id"] != exclude_order_id and o["status"] != "REJECTED"
                 and (o.get("utr") or "").strip().lower() == utr_norm), None)


def _looks_credited(value: Optional[str]) -> bool:
    return (value or "").strip().lower() in config.FINANCE_CREDITED_VALUES


def _try_finance_verify(state: dict, order: dict, finance_remark: Optional[str]) -> None:
    """
    The gate on PAYMENT_VERIFIED: finance must have typed a credited-looking
    remark against this row in the response sheet (see the module docstring
    and GOOGLE_FORM_SETUP.md's "Finance Remark column" section) - the
    amount/UTR match alone is no longer sufficient by itself. Once finance
    says credited, the amount-match and duplicate-UTR checks still run as
    safety nets against a finance typo or a genuinely duplicated UTR, not as
    the primary gate. Called on every poll of the response sheet
    (import_responses), so it can pick up a remark that wasn't there on an
    earlier poll - nobody has to click anything for this to fire, they just
    have to have typed the remark at some point.
    """
    if not _looks_credited(finance_remark):
        return
    if round(order["amountPaid"]) != round(order["price"]):
        order["remarks"] = (f"Finance marked credited, but amount paid "
                             f"(Rs {order['amountPaid']:,.0f}) doesn't match the campaign "
                             f"price (Rs {order['price']:,.0f}) - needs a manual look.")
        return
    clash = _find_order_with_utr(state, order["utr"], exclude_order_id=order["id"])
    if clash is not None:
        order["remarks"] = (f"Finance marked credited, but UTR {order['utr']} is already "
                             f"used by order {clash['id']} ({clash['status']}) - needs a manual look.")
        return
    transition(state, order["id"], "PAYMENT_VERIFIED", by="finance-sheet:auto-detected",
              note=f"Finance Remark column said: {finance_remark!r}")


def _try_attach_tracking_from_sheet(state: dict, order: dict, tracking_id: Optional[str]) -> None:
    """Mirrors _try_finance_verify's pattern, one stage later: ops typing an
    AWB into the response sheet's "Tracking ID" column (added by
    google-apps-script/cleanup_and_add_tracking_column.gs) is itself the
    dispatch signal - the sheet-driven equivalent of calling dispatch() with
    courier + tracking_number by hand. There's no "Courier" column for ops
    to fill in alongside it (see the project's own choice to guess from the
    AWB format instead), so courier is left for
    app.tracking.run_tracking_checks to resolve against the real courier
    sites - see tracking.classify_courier_candidates and
    store.attach_pending_tracking. Called on every poll of the response
    sheet (import_responses), same as _try_finance_verify.
    """
    tracking_id = (tracking_id or "").strip()
    if not tracking_id or order.get("trackingNumber"):
        return

    from . import tracking  # local import: tracking.py imports this module at load time

    candidates = tracking.classify_courier_candidates(tracking_id)

    order = transition(state, order["id"], "DISPATCHED", by="ops-sheet:auto-detected",
                       note=f"Tracking ID column said: {tracking_id!r}")

    if len(candidates) == 1:
        store.attach_tracking(state, order["id"], courier=candidates[0], tracking_number=tracking_id)
    elif candidates:
        store.attach_pending_tracking(state, order["id"], tracking_number=tracking_id)
    else:
        order["remarks"] = (f"Tracking ID {tracking_id!r} typed in the sheet, but its format "
                             "doesn't match any known courier's AWB pattern - needs a manual look "
                             "(attach courier + tracking number by hand instead, e.g. via the "
                             "dispatch API).")


def transition(state: dict, order_id: str, new_status: str, *,
               by: Optional[str] = None, note: str = "") -> dict:
    order = store.get_order(state, order_id)
    if order is None:
        raise KeyError(f"No such order: {order_id}")

    allowed = _TRANSITIONS.get(order["status"], ())
    if new_status not in allowed:
        raise ValueError(f"Cannot move order {order_id} from {order['status']} "
                          f"to {new_status} (allowed: {allowed or 'none - terminal state'})")

    store.set_order_status(state, order_id, new_status, by=by, note=note)
    store.audit(state, "ORDER_STATUS_CHANGED", {
        "orderId": order_id, "from": order["status"], "to": new_status, "by": by,
    })

    if new_status in config.OWNERSHIP_CONFIRMING_STATUSES:
        _confirm_ownership(state, order, new_status, by=by)

    if new_status in _EMAIL_ON_STATUS:
        _notify(state, store.get_order(state, order_id), new_status, _EMAIL_ON_STATUS[new_status])

    return store.get_order(state, order_id)


def _notify(state: dict, order: dict, status: str, compose) -> None:
    """
    Emails the CSP the moment their order enters `status` - called both from
    transition() (for PAYMENT_VERIFIED/DISPATCHED/DELIVERED) and directly
    from process_submission() (for SUBMITTED, which isn't reached via
    transition() at all). A missing email or an SMTP failure must never
    break whatever triggered this - the status change is already committed
    by the time this runs, so a failure here is logged, not raised.
    """
    if not order.get("email"):
        store.audit(state, "NOTIFICATION_SKIPPED", {
            "orderId": order["id"], "status": status, "reason": "no email on file for this CSP",
        })
        return

    product = config.PRODUCTS.get(order["productId"])
    product_name = product.name if product else order["productId"]
    message = compose(order, product_name)

    try:
        outcome = emailer.send(to=order["email"], **message)
    except Exception as err:                          # noqa: BLE001 - best-effort side effect
        store.audit(state, "NOTIFICATION_FAILED", {
            "orderId": order["id"], "status": status, "to": order["email"], "reason": str(err),
        })
        return

    if outcome["sent"]:
        state["outreach"].append({
            "id": len(state["outreach"]) + 1,
            "cspCode": order["cspCode"], "name": order["name"],
            "productId": order["productId"], "channel": "EMAIL",
            "to": order["email"], "subject": message["subject"], "html": message["html"],
            "delivered": True, "mode": outcome["mode"], "at": store.now_iso(),
            "kind": f"ORDER_{status}", "orderId": order["id"],
        })
    else:
        # dry run - composed, nothing sent, not logged to outreach history
        # (same convention as outreach.run_outreach for campaign email).
        store.audit(state, "NOTIFICATION_COMPOSED", {
            "orderId": order["id"], "status": status, "to": order["email"], "mode": outcome["mode"],
        })


def _confirm_ownership(state: dict, order: dict, status: str, *, by: Optional[str]) -> None:
    key = store.recon_key(order["cspCode"], order["productId"])
    state["reconciliations"][key] = {
        "ownsDevice": True,
        "by": by or f"system:order:{order['id']}",
        "at": store.now_iso(),
        "note": f"{status} via order {order['id']}",
    }
    store.audit(state, "OWNERSHIP_CONFIRMED", {
        "cspCode": order["cspCode"], "productId": order["productId"],
        "orderId": order["id"], "status": status,
    })


def verify_payment(state: dict, order_id: str, *, by: str, note: str = "") -> dict:
    """Manual override only - the normal path reaches PAYMENT_VERIFIED via
    _try_auto_verify() with no one calling this."""
    return transition(state, order_id, "PAYMENT_VERIFIED", by=by, note=note)


def dispatch(state: dict, order_id: str, *, by: str, note: str = "",
             courier: Optional[str] = None, tracking_number: Optional[str] = None) -> dict:
    """Manual override only - the normal path reaches DISPATCHED via
    auto_advance() below with no one calling this. Passing courier +
    tracking_number here (both required together) is what unlocks real
    courier-driven updates for this order (see app.tracking) instead of
    leaving DELIVERED to the AUTO_DELIVER_AFTER_DAYS timer - this is the one
    moment an ops person naturally has the AWB in hand, right as they hand
    the package to the courier."""
    order = transition(state, order_id, "DISPATCHED", by=by, note=note)
    if courier and tracking_number:
        order = store.attach_tracking(state, order_id, courier=courier, tracking_number=tracking_number)
    return order


def deliver(state: dict, order_id: str, *, by: str, note: str = "") -> dict:
    """Manual override only - the normal path reaches DELIVERED via
    auto_advance() below with no one calling this."""
    return transition(state, order_id, "DELIVERED", by=by, note=note)


def reject(state: dict, order_id: str, *, by: str, note: str = "") -> dict:
    """The one action that stays genuinely manual - nothing here has grounds
    to auto-reject a submission, only a person noticing something is wrong."""
    return transition(state, order_id, "REJECTED", by=by, note=note)


def _days_since_entering_status(order: dict) -> float:
    if not order["history"]:
        return 0.0
    entered_at = datetime.fromisoformat(order["history"][-1]["at"])
    if entered_at.tzinfo is None:
        entered_at = entered_at.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - entered_at).total_seconds() / 86400


def auto_advance(state: dict) -> list:
    """
    The engine behind "no one clicks anything": moves every order forward
    once enough time has passed in its current stage, per
    config.AUTO_DISPATCH_AFTER_DAYS / AUTO_DELIVER_AFTER_DAYS. Called by
    run.py both on a background schedule and on every dashboard/API read of
    the order list, so progress happens on a timer regardless of whether
    anyone is looking, and is never stale when someone does look.

    Returns the list of order IDs it moved, for logging/testing - empty most
    of the time, since most orders aren't due yet. A PAYMENT_VERIFIED order
    with addressFlag set (see _pincode_zone_mismatch) never auto-dispatches -
    it sits there until a human clears it with the dashboard's "Force
    dispatch" override, since shipping it as-is is the one thing worse than
    a delay.
    """
    advanced = []
    for order in state["orders"]:
        if order["status"] == "PAYMENT_VERIFIED" and not order.get("addressFlag") and \
                _days_since_entering_status(order) >= config.AUTO_DISPATCH_AFTER_DAYS:
            transition(state, order["id"], "DISPATCHED", by="system:auto-advance")
            advanced.append(order["id"])
        elif order["status"] == "DISPATCHED" and \
                _days_since_entering_status(order) >= config.AUTO_DELIVER_AFTER_DAYS:
            transition(state, order["id"], "DELIVERED", by="system:auto-advance")
            advanced.append(order["id"])
    return advanced


def fetch_response_rows(sheet_url: str) -> list:
    """
    Downloads the form's response spreadsheet as CSV - same "Anyone with the
    link can view" live-fetch pattern as import_calling_sheet.py's
    fetch_live_xlsx, just CSV instead of xlsx since responses don't need the
    heavier pandas/openpyxl path. Returns one normalised dict per row, headers
    translated via config.RESPONSE_HEADER_MAP.

    Polling this is a fallback. The better integration is a Google Apps
    Script "on form submit" trigger that POSTs the response straight to
    /api/form/responses/import as JSON - no polling delay, no risk of
    missing a row between polls.
    """
    match = _SHEET_ID_RE.search(sheet_url)
    if not match:
        raise ValueError(f"Could not find a spreadsheet ID in: {sheet_url}")
    sheet_id = match.group(1)
    gid_match = _GID_RE.search(sheet_url)
    export_url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv"
    if gid_match:
        export_url += f"&gid={gid_match.group(1)}"

    request = urllib.request.Request(export_url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read().decode("utf-8-sig")
    except urllib.error.HTTPError as err:
        raise RuntimeError(
            f"Could not download the response sheet (HTTP {err.code}). Most "
            "likely cause: sharing isn't \"Anyone with the link can view\"."
        ) from err
    except urllib.error.URLError as err:
        raise RuntimeError(f"Could not reach Google Sheets: {err.reason}") from err

    rows = []
    for i, raw_row in enumerate(csv.DictReader(io.StringIO(raw))):
        row = {}
        for header, value in raw_row.items():
            key = config.RESPONSE_HEADER_MAP.get((header or "").strip())
            if key:
                row[key] = value
        row["responseId"] = (f"{row.get('cspCode','')}|{row.get('token','')}|"
                              f"{row.get('utr','')}|{row.get('timestamp', i)}")
        rows.append(row)
    return rows


def _adopt_order_page_row(state: dict, row: dict, response_id: str):
    """Links a response-sheet row back to an order that was created by the
    ORDER PAGE rather than by this poll, and returns it.

    Why this is needed: an order-page order (app/orderpage.py) is written
    straight into state.json with responseId=None - there was no sheet row
    when it was created. orderpage.mirror_to_sheet() then appends one, and
    the next poll reads it back. Without this, that row looks brand new:
    the poll would create a SECOND, duplicate order for a payment the CSP
    made once, AND the original would never be finance-verified (nothing
    would ever match its responseId), leaving it stuck at SUBMITTED
    forever.

    Matched on UTR, which this module already treats as an order's unique
    payment reference - see _find_order_with_utr and the duplicate-UTR
    safety net in _try_finance_verify. Restricted to orders that have no
    responseId yet, so a row can never steal an order that already belongs
    to a different row. The CSP code has to agree too: a UTR typo that
    happens to collide with another CSP's order should fall through and be
    caught by the existing duplicate-UTR check, not silently adopt the
    wrong order.

    Adoption is recorded by stamping the responseId onto the order, so
    subsequent polls find it by the normal path.
    """
    utr = (row.get("utr") or "").strip()
    if not utr:
        return None
    candidate = _find_order_with_utr(state, utr)
    if candidate is None or candidate.get("responseId"):
        return None
    if (candidate.get("cspCode") or "").strip() != (row.get("cspCode") or "").strip():
        return None
    candidate["responseId"] = response_id
    store.audit(state, "ORDER_PAGE_ROW_ADOPTED", {
        "orderId": candidate["id"], "responseId": response_id, "utr": utr,
    })
    return candidate


def import_responses(rows: list, csps_by_code: dict, state: dict) -> dict:
    """
    Runs a batch of normalised response rows two ways at once, since a row
    seen before might now carry a finance remark it didn't have last time:

      - A row whose responseId has no matching order yet is genuinely new:
        process_submission() creates it (SUBMITTED, "Order Received" email).
      - Every row - new or already seen - then gets checked for whatever
        column matters at its current status: a finance remark for a
        SUBMITTED order (_try_finance_verify), or a Tracking ID for a
        PAYMENT_VERIFIED one (_try_attach_tracking_from_sheet) - so a value
        typed in AFTER an earlier poll is picked up on this one, without
        anyone re-submitting anything or clicking a button. This is what
        makes both gates actually automatic rather than a one-shot check.

    Only SUBMITTED and PAYMENT_VERIFIED orders are worth re-checking -
    anything past that has already been dispatched (or rejected) and
    doesn't need another look at these columns.
    """
    created, verified = [], []
    for row in rows:
        response_id = row.get("responseId") or f"{row.get('cspCode')}|{row.get('token')}"
        order = store.find_order_by_response_id(state, response_id)

        if order is None:
            order = _adopt_order_page_row(state, row, response_id)

        if order is None:
            result = process_submission(row, csps_by_code, state, response_id=response_id)
            store.record_submission(state, response_id, row, "SUBMITTED")
            created.append(result)
            order = store.get_order(state, result["orderId"])

        if order["status"] == "SUBMITTED":
            before = order["status"]
            _try_finance_verify(state, order, row.get("financeRemark"))
            if order["status"] != before:
                verified.append(order["id"])
        elif order["status"] == "PAYMENT_VERIFIED":
            _try_attach_tracking_from_sheet(state, order, row.get("trackingId"))

    return {"created": created, "verified": verified}


def eta_info(order: dict) -> Optional[dict]:
    """What's next for this order and roughly when, for display only - a
    fresh estimate computed on read, not a value auto_advance() trusts (it
    re-derives the same days-elapsed math itself each sweep). An order
    sitting at SUBMITTED has no ETA: there's no timer for finance to type a
    remark, so there's nothing to count down - the dashboard just shows
    "waiting on finance" for that status, no ETA needed. Same for a
    PAYMENT_VERIFIED order held by addressFlag - auto_advance() won't move
    it, so there's no countdown to show until a human clears it."""
    if order["status"] == "PAYMENT_VERIFIED":
        if order.get("addressFlag"):
            return None
        remaining = config.AUTO_DISPATCH_AFTER_DAYS - _days_since_entering_status(order)
        return {"next": "DISPATCHED", "etaDays": max(round(remaining, 1), 0)}
    if order["status"] == "DISPATCHED":
        remaining = config.AUTO_DELIVER_AFTER_DAYS - _days_since_entering_status(order)
        return {"next": "DELIVERED", "etaDays": max(round(remaining, 1), 0)}
    return None


def summary(state: dict) -> dict:
    """Counts behind the dashboard's order-pipeline metrics."""
    orders = state["orders"]
    by_status = {}
    for o in orders:
        by_status[o["status"]] = by_status.get(o["status"], 0) + 1
    return {
        "total": len(orders),
        "byStatus": by_status,
        "formsSubmitted": len(state["formSubmissions"]),
        "paymentsVerified": sum(1 for o in orders if o["status"] not in ("SUBMITTED", "REJECTED")),
        "ordersDispatched": by_status.get("DISPATCHED", 0) + by_status.get("DELIVERED", 0),
        "devicesDelivered": by_status.get("DELIVERED", 0),
        "rejected": by_status.get("REJECTED", 0),
        "pendingOrders": by_status.get("SUBMITTED", 0) + by_status.get("PAYMENT_VERIFIED", 0)
                         + by_status.get("DISPATCHED", 0),
    }
