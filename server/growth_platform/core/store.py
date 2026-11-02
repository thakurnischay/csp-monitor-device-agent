"""
Persistence. JSON files, deliberately.

The CSP base is ~535 records refreshed from a spreadsheet; the mutable state is
a few hundred outreach entries plus the reconciliation decisions. A database
would be more machinery than this earns. When the base outgrows a file, the only
thing that has to change is this module.
"""

import json
import os
import secrets
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator, Optional

from . import config

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Merged into the Device Monitor: the live csps.json / state.json sit in GROWTH_DATA_DIR (a mounted volume in
# production) so a new image never ships, or overwrites, operator data. Default: growth_platform/data (git-ignored).
DATA_DIR = os.environ.get("GROWTH_DATA_DIR") or os.path.join(BASE_DIR, "data")
CSP_FILE = os.path.join(DATA_DIR, "csps.json")
STATE_FILE = os.path.join(DATA_DIR, "state.json")

# Every access to state.json - read or write - goes through this one lock.
# Flask runs with threaded=True (see run.py), so two requests can genuinely
# be inside a handler at the same time; without this, two concurrent
# load_state() -> mutate -> save_state() sequences can interleave so the
# second save silently overwrites the first's change (a lost update - e.g.
# two CSPs submitting an order in the same second could mean one order
# never ends up on disk, despite the CSP having paid). RLock, not Lock: the
# common pattern is transaction() calling load_state()/save_state()
# internally while already holding the lock on the same thread, and a
# plain Lock would deadlock on that.
_LOCK = threading.RLock()


def _empty_state() -> dict:
    return {
        # Human decisions that override what the spreadsheet claims. This is how
        # a quarantined "disputed" record gets unblocked: an RM confirms on the
        # phone whether the device is physically there, and that answer wins.
        # The order pipeline reuses this SAME mechanism to flip ownership once a
        # device is delivered - see orders.mark_delivered.
        "reconciliations": {},   # "cspCode:PRODUCT" -> {ownsDevice, by, at, note}
        "outreach": [],          # one entry per message actually sent
        "audit": [],
        "tokens": {},            # token -> {cspCode, productId, price, issuedAt, used}
        "orders": [],            # one entry per verified form submission
        "formSubmissions": [],   # raw log of every response row processed, for idempotency
        # One row per day of "where we were on that date" - see record_snapshot.
        # Coverage is computed fresh from the CURRENT calling-sheet import every
        # time, so without this there is no way to answer "are we further along
        # than last week": the old numbers simply aren't anywhere. This is the
        # only place history is kept, and it only starts from the first day it
        # runs - it cannot be backfilled.
        "snapshots": [],
    }


def _read_json(path: str):
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        try:
            return json.load(f)
        except json.JSONDecodeError as err:
            raise RuntimeError(f"{os.path.basename(path)} is not valid JSON: {err}") from err


def load_csps() -> dict:
    payload = _read_json(CSP_FILE)
    if payload is None:
        raise RuntimeError(
            "data/csps.json not found. Run:  python import_calling_sheet.py"
        )
    return payload


def load_state() -> dict:
    with _LOCK:
        state = _read_json(STATE_FILE)
        if state is None:
            return _empty_state()
        # Tolerate a state file written by an older version.
        merged = _empty_state()
        merged.update(state)
        return merged


def save_state(state: dict) -> None:
    """Writes state.json atomically: to a temp file first, then one
    os.replace() onto the real path. os.replace is atomic at the OS level,
    so any reader - even one that skips the lock entirely - always sees
    either the complete old file or the complete new one, never a
    half-written one from a save that's still in progress (which would
    otherwise raise a JSON decode error, or worse, read as a smaller-than-
    real but valid-looking state). The temp filename includes the PID and
    thread id so two saves racing on the same lock (impossible here since
    _LOCK serialises them, but cheap insurance if this is ever called
    without it) never clobber each other's temp file.
    """
    with _LOCK:
        os.makedirs(DATA_DIR, exist_ok=True)
        tmp_path = f"{STATE_FILE}.tmp-{os.getpid()}-{threading.get_ident()}"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=1)
        os.replace(tmp_path, STATE_FILE)


@contextmanager
def transaction(*, wait: bool = True) -> Iterator[Optional[dict]]:
    """The single choke point for every read-modify-write against
    state.json. Acquires _LOCK, loads state, and hands it to the caller for
    the WHOLE critical section - not just around save_state() itself, which
    is what closes the lost-update race described above: a second
    transaction() cannot even start its load_state() until this one has
    finished (saved or not) and released the lock.

    The caller decides whether to call save_state(state) inside the block,
    exactly as before this existed - nothing is forced, so a read-only or
    dry-run path that doesn't mutate anything doesn't pay for rewriting the
    (large) state file for no reason. Nothing is saved if the block raises;
    the lock is still always released (a `with` on a threading lock releases
    on exception same as on normal exit), matching the existing convention
    where a caught exception turns into an error response with no side
    effect.

    wait=False is for callers that must never block on a slow lock-holder -
    a scheduled outreach batch can hold _LOCK for minutes sending real email
    (see run.py's _outreach_loop), and a page read has no business queuing
    behind that. Yields None immediately instead of waiting; the caller falls
    back to load_state() outside the lock, which save_state()'s atomic
    os.replace() makes safe to do unlocked - a lock-free reader always sees
    a complete old-or-new file, never a half-written one. The only thing that
    fallback gives up is _sweep()'s conditional write for this one request;
    the very next request (or the background loop's own tick) catches it up.
    """
    acquired = _LOCK.acquire(blocking=wait)
    if not acquired:
        yield None
        return
    try:
        state = load_state()
        yield state
    finally:
        _LOCK.release()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def find_outreach_entry(state: dict, entry_id: int) -> Optional[dict]:
    """Looks up one specific sent message by its id (see the "id" field
    outreach.py/orders.py/tracking.py stamp onto every entry they append to
    state["outreach"]) - what the dashboard's "View message" action reads
    off, since the entry itself carries the actual composed html, not just
    who/when/subject."""
    return next((e for e in state["outreach"] if e.get("id") == entry_id), None)


def record_snapshot(state: dict, metrics: dict) -> bool:
    """Writes today's "where we are" numbers, one row per calendar day.

    Idempotent per day on purpose: called on every dashboard load, but the
    day's existing row is overwritten rather than appended, so the series
    stays one-point-per-day however many times anyone opens the page. The
    last write of a day wins, which means a day's figure is "as at the last
    time someone looked", not a fixed time of day - accepted deliberately,
    since the alternative (a fixed cron hour) would silently record nothing
    on any day the server happened to be down at that hour.

    Returns True if this created a new day's row rather than updating one.
    """
    today = now_iso()[:10]
    row = {"date": today, "at": now_iso(), **metrics}
    for i, existing in enumerate(state["snapshots"]):
        if existing.get("date") == today:
            state["snapshots"][i] = row
            return False
    state["snapshots"].append(row)
    # ~2 years of daily points; well past any trend anyone reads, and keeps
    # the state file from growing without bound.
    if len(state["snapshots"]) > 730:
        state["snapshots"] = state["snapshots"][-730:]
    return True


def audit(state: dict, event: str, details: Any) -> None:
    state["audit"].append({
        "id": len(state["audit"]) + 1,
        "event": event,
        "details": details,
        "at": now_iso(),
    })
    # Keep the tail bounded - this is an operational log, not an archive.
    if len(state["audit"]) > 2000:
        state["audit"] = state["audit"][-2000:]


def recon_key(csp_code: str, product_id: str) -> str:
    return f"{csp_code}:{product_id}"


# --------------------------------------------------------------- tokens
def issue_token(state: dict, csp_code: str, product_id: str, price: int) -> str:
    """
    Mints a fresh campaign token binding one CSP to one product at one price,
    and records it so a later form submission can be checked against what was
    actually offered rather than trusted at face value.

    Re-issuing for the same (csp, product) is allowed and expected - a CSP may
    be recontacted with a reminder. Old tokens for the same pair are left
    valid rather than revoked: revoking would break a CSP who already has an
    older link open in their browser.
    """
    token = secrets.token_urlsafe(config.TOKEN_BYTES)
    state["tokens"][token] = {
        "cspCode": csp_code,
        "productId": product_id,
        "price": price,
        "issuedAt": now_iso(),
        "used": False,
    }
    return token


def lookup_token(state: dict, token: str) -> Optional[dict]:
    return state["tokens"].get((token or "").strip())


def mark_token_used(state: dict, token: str, order_id: str) -> None:
    record = state["tokens"].get(token)
    if record is not None:
        record["used"] = True
        record["orderId"] = order_id


# ---------------------------------------------------------------- orders
def next_order_id(state: dict) -> str:
    return f"ORD-{len(state['orders']) + 1:06d}"


def create_order(state: dict, *, csp_code: str, name: str, product_id: str,
                  price: int, token: str, amount_paid: float, utr: str,
                  mobile: Optional[str] = None,
                  email: Optional[str] = None, status: str = "SUBMITTED",
                  remarks: str = "", response_id: Optional[str] = None,
                  delivery_state: Optional[str] = None,
                  pincode: Optional[str] = None,
                  address_flag: bool = False,
                  payment_method: Optional[str] = None,
                  address_line: Optional[str] = None,
                  landmark: Optional[str] = None,
                  city: Optional[str] = None) -> dict:
    if status not in config.ORDER_STATUSES:
        raise ValueError(f"Unknown order status: {status}")
    order = {
        "id": next_order_id(state),
        "cspCode": csp_code,
        "name": name,
        "productId": product_id,
        "price": price,
        "token": token,
        "mobile": mobile,
        "email": email,
        "amountPaid": amount_paid,
        "utr": utr,
        # "bank" / "qr" from the order page, or None for a Google-Form
        # submission (which doesn't ask) - display/audit only, not used by
        # any verification logic, since both methods land on the same bank
        # statement finance already checks.
        "paymentMethod": payment_method,
        "status": status,
        "remarks": remarks,
        "deliveryState": delivery_state,
        "pincode": pincode,
        # The actual shipping address. Google-Form orders leave these three
        # None: that form DOES collect them, but only into the response
        # sheet - config.RESPONSE_HEADER_MAP never mapped Address/Landmark/
        # City, so the platform has only ever read State and Pincode from a
        # form submission and ops reads the full address off the sheet
        # itself. Order-page orders have no sheet row to fall back on when
        # the mirror is off (config.ORDER_PAGE_SHEET_MIRROR_URL), so they
        # carry the address on the order - otherwise there would be nothing
        # to ship against.
        "addressLine": address_line,
        "landmark": landmark,
        "city": city,
        # True when the delivery pincode doesn't match the declared state's
        # postal zone - see config.POSTAL_ZONE_BY_STATE and
        # orders._pincode_zone_mismatch. Holds the order at PAYMENT_VERIFIED
        # instead of auto-dispatching it to a likely-wrong address.
        "addressFlag": address_flag,
        # Real courier tracking, attached at dispatch time (see
        # orders.dispatch) - courier/trackingNumber stay None for an order
        # dispatched without them, which is fine: auto_advance's
        # AUTO_DELIVER_AFTER_DAYS timer is still the fallback path to
        # DELIVERED for those. trackingPhase is the last phase a check
        # actually recognised (see app.tracking.classify_phase) - None until
        # the first successful check, never overwritten by an
        # unrecognised/failed one. trackingHistory logs every check
        # attempted, recognised or not - see record_tracking_check.
        "courier": None,
        "trackingNumber": None,
        "trackingPhase": None,
        "trackingHistory": [],
        "lastTrackingCheckAt": None,
        "createdAt": now_iso(),
        "history": [{"status": status, "at": now_iso()}],
        # Links this order back to the exact response-sheet row it came from,
        # so a later poll can find THIS order again to re-check for a finance
        # remark that wasn't there yet the first time - see
        # orders.import_responses and find_order_by_response_id below.
        "responseId": response_id,
    }
    state["orders"].append(order)
    if token:
        mark_token_used(state, token, order["id"])
    return order


def get_order(state: dict, order_id: str) -> Optional[dict]:
    return next((o for o in state["orders"] if o["id"] == order_id), None)


def find_order_by_response_id(state: dict, response_id: str) -> Optional[dict]:
    if not response_id:
        return None
    return next((o for o in state["orders"] if o.get("responseId") == response_id), None)


def set_order_status(state: dict, order_id: str, status: str, *,
                      by: Optional[str] = None, note: str = "") -> dict:
    if status not in config.ORDER_STATUSES:
        raise ValueError(f"Unknown order status: {status}")
    order = get_order(state, order_id)
    if order is None:
        raise KeyError(f"No such order: {order_id}")
    order["status"] = status
    order["history"].append({"status": status, "at": now_iso(), "by": by, "note": note})
    return order




def attach_tracking(state: dict, order_id: str, *, courier: str, tracking_number: str) -> dict:
    """Called once, at dispatch time (see orders.dispatch), or once more
    later to resolve a pending order that attach_pending_tracking() left
    with no confirmed courier (see tracking._resolve_pending_courier). Not
    meant to be re-called to change an ALREADY-confirmed courier/
    trackingNumber mid-flight, since trackingPhase/trackingHistory below
    assume they describe one continuous shipment."""
    order = get_order(state, order_id)
    if order is None:
        raise KeyError(f"No such order: {order_id}")
    order["courier"] = courier
    order["trackingNumber"] = tracking_number
    return order


def attach_pending_tracking(state: dict, order_id: str, *, tracking_number: str) -> dict:
    """Ops typed a Tracking ID into the response sheet, but its AWB format
    alone doesn't pin down a single courier (see
    tracking.classify_courier_candidates) - courier stays None until
    tracking.run_tracking_checks resolves it by trying each candidate
    against the real courier sites, then calls attach_tracking() to lock in
    the answer. See orders._try_attach_tracking_from_sheet."""
    order = get_order(state, order_id)
    if order is None:
        raise KeyError(f"No such order: {order_id}")
    order["trackingNumber"] = tracking_number
    order["courier"] = None
    return order


def record_tracking_check(state: dict, order_id: str, *, raw_result: str) -> dict:
    """Logs one tracking check, successful or not - every call, not just the
    ones that found something new, so lastTrackingCheckAt is accurate for
    app.tracking's re-check cooldown regardless of outcome. Deliberately
    does NOT touch trackingPhase - only app.tracking.run_tracking_checks
    decides whether a check's result is a genuinely new, recognised phase
    worth acting on."""
    order = get_order(state, order_id)
    if order is None:
        raise KeyError(f"No such order: {order_id}")
    order["lastTrackingCheckAt"] = now_iso()
    order["trackingHistory"].append({"at": now_iso(), "rawResult": (raw_result or "")[:500]})
    return order


def mark_email_opened(state: dict, token: str) -> Optional[dict]:
    """Records an open-pixel hit against whichever outreach entry carries
    this token (see emailer.open_pixel_tag / run.py's /pixel/<token>.png).

    Returns the entry if the token matched (openHits is incremented on
    EVERY hit - the caller must save state whenever this is not None, not
    only on the first hit, or every hit after the first silently vanishes).
    openedAt itself is set once - a client re-fetching the same image on a
    later view isn't a second "open" worth re-timestamping, but it still
    counts toward openHits. Check entry["openHits"] == 1 to tell whether
    THIS call was the genuinely first open (worth an audit entry) versus a
    repeat. An unrecognised token (stale link, dry-run preview that was
    never persisted, someone just poking the URL) returns None - not an
    error, just nothing to record."""
    entry = next((e for e in state["outreach"] if e.get("token") == token), None)
    if entry is None:
        return None
    entry["openHits"] = entry.get("openHits", 0) + 1
    if not entry.get("openedAt"):
        entry["openedAt"] = now_iso()
    return entry


def mark_form_clicked(state: dict, token: str) -> Optional[dict]:
    """Records a click on a campaign's form link - the step between "email
    sent" and "form submitted" that used to be invisible (see run.py's
    /go/<token> and forms.issue_and_build). Same shape as mark_email_opened
    above: clickHits increments on EVERY hit, the caller must save state
    whenever this isn't None; clickedAt is set once, on the first hit only.
    An unrecognised token (stale link, a token from a dry-run preview that
    was never persisted, someone just poking the URL) returns None."""
    record = state["tokens"].get(token)
    if record is None:
        return None
    record["clickHits"] = record.get("clickHits", 0) + 1
    if not record.get("clickedAt"):
        record["clickedAt"] = now_iso()
    return record


def record_submission(state: dict, response_id: str, raw: dict, outcome: str) -> None:
    """Logs every new order created from a response row, keyed by the form's
    own response identifier (or a synthesised one) - an audit trail, not the
    dedup mechanism (that's find_order_by_response_id, since a row needs to
    be re-checked on later polls for a finance remark, not skipped)."""
    state["formSubmissions"].append({
        "responseId": response_id, "outcome": outcome, "raw": raw, "at": now_iso(),
    })
