"""
Outreach: turning a ranked queue into email that actually gets sent.

Email only for now - WhatsApp is deferred until Meta template approval, so there
is deliberately no half-built WhatsApp path sitting here pretending to work.

Each CSP gets an 8-email sequence per product on fixed days since that cycle's
first email (config.CAMPAIGN_SEND_DAY_OFFSETS - Day 1, 3, 6, 9, 12, 16, 20,
24), each arguing a different reason to buy (see emailer._TOUCH_COPY) rather
than re-sending the same pitch or announcing itself as a reminder.

The sequence then REPEATS monthly: a CSP who has had all 8 without ordering
begins the whole sequence again config.CAMPAIGN_CYCLE_DAYS after that cycle
started - see sequence_position() below, which is the single place cycle and
touch are worked out.

The only automatic way out of the loop is buying: ownership flips at
DELIVERED and engine.build_queue stops targeting them. config.CAMPAIGN_MAX_CYCLES
(None by default) is the one lever that adds a stop back, for handing a
persistently non-responding CSP to an RM instead.
"""

from datetime import datetime, timedelta, timezone
from typing import Optional

from . import config, emailer, forms, offer_calendar, store, whatsapp


def _campaign_sends(state: dict, csp_code: str, product_id: str) -> list:
    # Entries with no "kind" are legacy campaign sends from before this field
    # existed - treated as CAMPAIGN rather than migrated, since that's what
    # they actually were.
    sends = [e for e in state["outreach"]
             if e["cspCode"] == csp_code and e["productId"] == product_id
             and e.get("delivered") and e.get("kind", "CAMPAIGN") == "CAMPAIGN"]
    sends.sort(key=lambda e: e["at"])
    return sends


def sequence_position(state: dict, csp_code: str, product_id: str) -> dict:
    """Where this CSP stands in the repeating campaign, and when the next
    email is owed.

    Returns cycle (1-based), touch (1..8), dueAt, and whether it is due yet.
    Always returns a position - "nothing is due" is expressed as due=False
    with a real dueAt, so the dashboard can say WHEN rather than just "not
    yet". When the cycle cap is reached, exhausted=True and dueAt is None.

    Two anchoring rules, both deliberate:

    * Touches within a cycle are measured from that CYCLE'S first email, not
      from the previous touch. Touch 5 is always "day 12 of this cycle", so a
      day the run was late never pushes the rest of the cycle back with it.
    * A new cycle is measured from the previous cycle's FIRST email plus
      CAMPAIGN_CYCLE_DAYS - not from its last. A cycle therefore always
      occupies exactly 30 days, and late touches cannot make cycles drift
      steadily later over the year.
    """
    sends = _campaign_sends(state, csp_code, product_id)
    offsets = config.CAMPAIGN_SEND_DAY_OFFSETS
    n = len(offsets)
    now = datetime.now(timezone.utc)

    done_cycles, pos = divmod(len(sends), n)

    if not sends:
        return {"cycle": 1, "touch": 1, "dueAt": None, "due": True,
                "exhausted": False, "sent": 0}

    if pos == 0:
        # A cycle finished. The next one opens CAMPAIGN_CYCLE_DAYS after that
        # cycle started, which is what leaves days 25-30 quiet.
        if config.CAMPAIGN_MAX_CYCLES is not None and done_cycles >= config.CAMPAIGN_MAX_CYCLES:
            return {"cycle": done_cycles, "touch": None, "dueAt": None, "due": False,
                    "exhausted": True, "sent": len(sends)}
        prev_anchor = datetime.fromisoformat(sends[(done_cycles - 1) * n]["at"])
        due_at = prev_anchor + timedelta(days=config.CAMPAIGN_CYCLE_DAYS)
        return {"cycle": done_cycles + 1, "touch": 1, "dueAt": due_at,
                "due": now >= due_at, "exhausted": False, "sent": len(sends)}

    anchor = datetime.fromisoformat(sends[done_cycles * n]["at"])
    due_at = anchor + timedelta(days=offsets[pos])
    return {"cycle": done_cycles + 1, "touch": pos + 1, "dueAt": due_at,
            "due": now >= due_at, "exhausted": False, "sent": len(sends)}


def next_touch(state: dict, csp_code: str, product_id: str) -> Optional[int]:
    """Which touch is due right now, or None if nothing is. Thin wrapper over
    sequence_position for callers that only need the copy variant to use."""
    p = sequence_position(state, csp_code, product_id)
    return p["touch"] if p["due"] else None


def already_asked_to_confirm(state: dict, csp_code: str, product_id: str) -> bool:
    """Separate from next_touch - asking a disputed CSP to confirm ownership
    is not a sales touch, and must not count against (or be double-counted
    by) the campaign drip sequence's touch cap."""
    return any(e["cspCode"] == csp_code and e["productId"] == product_id
               and e.get("delivered") and e.get("kind") == "OWNERSHIP_CONFIRM"
               for e in state["outreach"])


def _brief(t: dict) -> dict:
    return {"cspCode": t["cspCode"], "name": t["name"], "rm": t["rm"],
            "email": t.get("email")}


# -------------------------------------------------------------------- audit
def plan_run(state: dict, queue: dict, limit: Optional[int] = None) -> dict:
    """
    Works out who in a queue is actually sendable right now, and why everyone
    else is not. Runs without sending, so the dashboard can show the real
    picture before anyone commits to a send.
    """
    cap = min(limit or emailer.MAX_PER_RUN, emailer.MAX_PER_RUN)

    sendable, held = [], []
    for target in queue["targeted"]:
        if not emailer.is_valid_email(target.get("email")):
            held.append({**_brief(target), "reason": "no valid email on file",
                         "kind": "NO_EMAIL"})
            continue
        pos = sequence_position(state, target["cspCode"], queue["productId"])
        if pos["exhausted"]:
            held.append({**_brief(target),
                         "reason": f"finished all {pos['cycle']} cycles - hand to an RM",
                         "kind": "SEQUENCE_EXHAUSTED"})
            continue
        if not pos["due"]:
            when = pos["dueAt"].strftime("%d %b") if pos["dueAt"] else "later"
            # A CSP between cycles is a different situation from one waiting on
            # the next email of the cycle they're in - worth telling apart,
            # since the first means "we've said everything once already".
            if pos["touch"] == 1:
                held.append({**_brief(target),
                             "reason": f"cycle {pos['cycle']} starts {when}",
                             "kind": "BETWEEN_CYCLES"})
            else:
                held.append({**_brief(target),
                             "reason": f"email {pos['touch']} due {when}",
                             "kind": "TOO_SOON"})
            continue
        sendable.append({**target, "touch": pos["touch"], "cycle": pos["cycle"]})

    held_by_kind: dict = {}
    for h in held:
        held_by_kind[h["kind"]] = held_by_kind.get(h["kind"], 0) + 1

    return {
        "productId": queue["productId"],
        "queueSize": len(queue["targeted"]),
        "sendable": len(sendable),
        "willSend": min(len(sendable), cap),
        "limit": cap,
        "held": held,
        "heldByKind": held_by_kind,
        "batch": sendable[:cap],
    }


# ------------------------------------------------------------------ sending
def _persist_failed(state: dict, target: dict, product_id: str, reason: str, *,
                     kind: str = "CAMPAIGN", subject: Optional[str] = None,
                     channel: str = "EMAIL") -> dict:
    """Records a failed send the same way a successful one is recorded, so it
    shows up in the Messages tab instead of only existing for the duration of
    one API response. delivered=False (not omitted) is what tells
    _campaign_sends() below to keep excluding it from the cooldown/sequence
    math - a failed attempt must not count as a touch actually delivered.
    touch/cycle are only meaningful for CAMPAIGN sends; an OWNERSHIP_CONFIRM
    or SEASONAL_OFFER row simply won't have them (target.get returns None),
    same as a successful entry of either kind never carries them either."""
    to = target.get("mobile") if channel == "WHATSAPP" else target.get("email")
    mode = whatsapp.MODE if channel == "WHATSAPP" else emailer.MODE
    entry = {
        "id": len(state["outreach"]) + 1,
        "cspCode": target["cspCode"], "name": target["name"],
        "productId": product_id, "channel": channel, "kind": kind,
        "to": to, "subject": subject,
        "delivered": False, "failReason": reason, "mode": mode, "at": store.now_iso(),
        "touch": target.get("touch"), "cycle": target.get("cycle"),
    }
    state["outreach"].append(entry)
    return entry


def run_outreach(state: dict, queue: dict, limit: Optional[int] = None) -> dict:
    """
    Sends the planned batch. Every attempt is recorded, successful or not - a
    failed send leaves the CSP outside the cooldown so the next run retries them
    rather than dropping them silently, and is saved to state["outreach"]
    (delivered=False) so it stays visible after this call returns, not just in
    this one response.
    """
    product = config.PRODUCTS[queue["productId"]]
    plan = plan_run(state, queue, limit)
    result = {"sent": [], "composed": [], "failed": [],
              "held": plan["held"], "mode": emailer.MODE}

    # One SMTP connection for the whole batch rather than one per message -
    # providers rate-limit on connection churn.
    server = None
    try:
        if emailer.MODE != "dry" and plan["batch"]:
            try:
                server = emailer._connect()
            except Exception as err:              # noqa: BLE001
                for target in plan["batch"]:
                    _persist_failed(state, target, queue["productId"], str(err))
                    result["failed"].append({**_brief(target), "reason": str(err)})
                return result

        for target in plan["batch"]:
            # A fresh token per send - not used to gate submissions any more
            # (see orders.py), just prefilled because the live form still has
            # "Campaign Token" as a required field. Issued even in dry mode so
            # the preview shows the real link a CSP would get - it's simply
            # never persisted, since dry runs don't call save_state.
            issued = forms.issue_and_build(state, target, queue["productId"])
            # Routed through our own /go/<token> first (emailer.click_tracked_
            # form_url, run.py's /go/<token>) so a click gets recorded - the
            # "Clicked through to form" funnel stage - before the CSP is sent
            # on to the real form/order page. Reverted 2026-08-24 from the
            # direct-link STOPGAP that was here while TRACKING_BASE_URL still
            # defaulted to localhost (unreachable from a CSP's phone, so the
            # tracked redirect was a dead link and every email had to point
            # straight at issued["url"] instead). TRACKING_BASE_URL is now a
            # real, internet-reachable deployment, so the tracked redirect
            # works again - see click_tracked_form_url's docstring for what
            # decides where /go/<token> sends them.
            message = emailer.compose(target, product,
                                      form_url=emailer.click_tracked_form_url(issued["token"]),
                                      token=issued["token"], touch=target["touch"])
            try:
                outcome = emailer.send(to=target["email"], server=server, **message)
            except Exception as err:              # noqa: BLE001
                _persist_failed(state, target, queue["productId"], str(err), subject=message["subject"])
                result["failed"].append({**_brief(target), "reason": str(err)})
                continue

            # A dry run is a rehearsal, not a contact. It is neither logged to
            # the outreach history nor counted as a failure - otherwise the
            # activity feed fills with messages nobody ever received, and every
            # rehearsal looks like an outage.
            if not outcome["sent"]:
                result["composed"].append({**_brief(target),
                                           "subject": message["subject"]})
                continue

            entry = {
                "id": len(state["outreach"]) + 1,
                "cspCode": target["cspCode"], "name": target["name"],
                "productId": queue["productId"], "channel": "EMAIL", "kind": "CAMPAIGN",
                "to": target["email"], "subject": message["subject"], "html": message["html"],
                "delivered": True, "mode": outcome["mode"], "at": store.now_iso(),
                "token": issued["token"], "formUrl": issued["url"],
                "touch": target["touch"], "cycle": target["cycle"],
            }
            state["outreach"].append(entry)
            result["sent"].append(entry)
    finally:
        if server is not None:
            try:
                server.quit()
            except Exception:                     # noqa: BLE001 - already sent
                pass

    return result


# --------------------------------------------------- ownership confirmation
# A different track from the campaign above: these CSPs are NOT being sold
# anything. The sheet says they don't own the device, their own transactions
# disagree, and the platform won't guess which is right (see README's "one
# thing to understand" section) - so instead of a sales pitch, this asks the
# CSP directly to confirm, the same question an RM would ask by phone. There
# is no link to click; the reply itself is what a human then acts on in the
# Reconciliation tab, same as a phone call would be.
def plan_confirmation_run(state: dict, disputed: list, limit: Optional[int] = None) -> dict:
    cap = min(limit or emailer.MAX_PER_RUN, emailer.MAX_PER_RUN)

    sendable, held = [], []
    for row in disputed:
        if not emailer.is_valid_email(row.get("email")):
            held.append({**_brief(row), "reason": "no valid email on file",
                         "kind": "NO_EMAIL"})
            continue
        if already_asked_to_confirm(state, row["cspCode"], row["product"]):
            held.append({**_brief(row), "reason": "already asked to confirm",
                         "kind": "ALREADY_ASKED"})
            continue
        sendable.append(row)

    held_by_kind: dict = {}
    for h in held:
        held_by_kind[h["kind"]] = held_by_kind.get(h["kind"], 0) + 1

    return {
        "queueSize": len(disputed),
        "sendable": len(sendable),
        "willSend": min(len(sendable), cap),
        "limit": cap,
        "held": held,
        "heldByKind": held_by_kind,
        "batch": sendable[:cap],
    }


def run_confirmation_outreach(state: dict, disputed: list, limit: Optional[int] = None) -> dict:
    plan = plan_confirmation_run(state, disputed, limit)
    result = {"sent": [], "composed": [], "failed": [],
              "held": plan["held"], "mode": emailer.MODE}

    server = None
    try:
        if emailer.MODE != "dry" and plan["batch"]:
            try:
                server = emailer._connect()
            except Exception as err:              # noqa: BLE001
                for row in plan["batch"]:
                    _persist_failed(state, row, row["product"], str(err), kind="OWNERSHIP_CONFIRM")
                    result["failed"].append({**_brief(row), "reason": str(err)})
                return result

        for row in plan["batch"]:
            product = config.PRODUCTS[row["product"]]
            message = emailer.compose_ownership_confirmation(row, product)
            try:
                outcome = emailer.send(to=row["email"], server=server, **message)
            except Exception as err:              # noqa: BLE001
                _persist_failed(state, row, row["product"], str(err),
                               kind="OWNERSHIP_CONFIRM", subject=message["subject"])
                result["failed"].append({**_brief(row), "reason": str(err)})
                continue

            if not outcome["sent"]:
                result["composed"].append({**_brief(row), "subject": message["subject"]})
                continue

            entry = {
                "id": len(state["outreach"]) + 1,
                "cspCode": row["cspCode"], "name": row["name"],
                "productId": row["product"], "channel": "EMAIL", "kind": "OWNERSHIP_CONFIRM",
                "to": row["email"], "subject": message["subject"], "html": message["html"],
                "delivered": True, "mode": outcome["mode"], "at": store.now_iso(),
            }
            state["outreach"].append(entry)
            result["sent"].append(entry)
    finally:
        if server is not None:
            try:
                server.quit()
            except Exception:                     # noqa: BLE001 - already sent
                pass

    return result


# ---------------------------------------------------- seasonal offer (WhatsApp)
# A third, separate track from the two above: a ONE-OFF broadcast per active
# occasion (not the repeating 8-touch cycle _campaign_sends() tracks), over
# WhatsApp, of whatever offer_calendar.active_offer() says is running today
# (see offer_calendar.py and whatsapp.py's single generic template - design
# decision made 2026-08-27, replacing the earlier fixed csp_rakhi_matm /
# csp_rakhi_printer pair). Reuses the same per-product targeting as the email
# campaign (queue["targeted"]) since that's already the vetted "who genuinely
# doesn't own this device" list - no separate eligibility logic needed here.
#
# The offer is looked up ONCE per run, not per CSP: "what's today's offer" is
# a single decision, and every CSP in one run should get the same occasion/
# price/deadline rather than risk a different answer for someone processed a
# moment later, right at a date boundary.
#
# Sending is capped at 3 touches per occasion, not daily across the whole
# window (see offer_calendar.py's module docstring for why - block-rate /
# Meta quality-rating risk from over-messaging). A run on a day that isn't
# one of offer_calendar.touch_dates()'s 3 dates holds EVERYONE, same shape as
# "no active offer" below - there is genuinely nothing to send that day.
def already_sent_offer_touch(state: dict, csp_code: str, product_id: str,
                              offer_id: str, touch_index: int) -> bool:
    """Scoped to the specific (occasion, touch) pair - a CSP can get up to 3
    messages for ONE occasion (announce/peak/last-chance), but never twice
    for the same touch, and a CSP who got the Rakhi offer should still be
    reachable for Diwali's, since that is a different thing being
    advertised."""
    return any(e["cspCode"] == csp_code and e["productId"] == product_id
               and e.get("delivered") and e.get("channel") == "WHATSAPP"
               and e.get("kind") == "SEASONAL_OFFER" and e.get("offerId") == offer_id
               and e.get("touchIndex") == touch_index
               for e in state["outreach"])


def plan_offer_run(state: dict, queue: dict, limit: Optional[int] = None) -> dict:
    cap = min(limit or whatsapp.MAX_PER_RUN, whatsapp.MAX_PER_RUN)
    offer = offer_calendar.active_offer(queue["productId"])

    if offer is None:
        held = [{**_brief(t), "reason": "no active offer today for this product",
                "kind": "NO_ACTIVE_OFFER"} for t in queue["targeted"]]
        return {
            "productId": queue["productId"], "offer": None, "touchIndex": None,
            "queueSize": len(queue["targeted"]), "sendable": 0, "willSend": 0,
            "limit": cap, "held": held,
            "heldByKind": {"NO_ACTIVE_OFFER": len(held)} if held else {},
            "batch": [],
        }

    touch_index = offer_calendar.today_touch_index(offer)
    if touch_index is None:
        held = [{**_brief(t), "reason": f"today is not a send day for the "
                f"{offer['occasion']} offer (window is open, but today isn't "
                f"the announce/peak/last-chance date)",
                "kind": "NOT_A_TOUCH_DAY"} for t in queue["targeted"]]
        return {
            "productId": queue["productId"], "offer": offer, "touchIndex": None,
            "queueSize": len(queue["targeted"]), "sendable": 0, "willSend": 0,
            "limit": cap, "held": held,
            "heldByKind": {"NOT_A_TOUCH_DAY": len(held)} if held else {},
            "batch": [],
        }

    sendable, held = [], []
    for target in queue["targeted"]:
        if not whatsapp.is_valid_mobile(target.get("mobile")):
            held.append({**_brief(target), "reason": "no valid mobile on file",
                         "kind": "NO_MOBILE"})
            continue
        if already_sent_offer_touch(state, target["cspCode"], queue["productId"],
                                    offer["id"], touch_index):
            held.append({**_brief(target),
                         "reason": f"already sent this touch of the {offer['occasion']} offer",
                         "kind": "ALREADY_SENT"})
            continue
        sendable.append(target)

    held_by_kind: dict = {}
    for h in held:
        held_by_kind[h["kind"]] = held_by_kind.get(h["kind"], 0) + 1

    return {
        "productId": queue["productId"], "offer": offer, "touchIndex": touch_index,
        "queueSize": len(queue["targeted"]),
        "sendable": len(sendable),
        "willSend": min(len(sendable), cap),
        "limit": cap,
        "held": held,
        "heldByKind": held_by_kind,
        "batch": sendable[:cap],
    }


def run_offer_outreach(state: dict, queue: dict, limit: Optional[int] = None) -> dict:
    """Sends the planned seasonal-offer batch over WhatsApp. Same
    failed-attempt bookkeeping as run_outreach: every attempt is recorded so
    a retry doesn't silently re-target someone who already got it, and
    doesn't silently skip someone whose send actually failed."""
    product = config.PRODUCTS[queue["productId"]]
    plan = plan_offer_run(state, queue, limit)
    result = {"sent": [], "composed": [], "failed": [],
              "held": plan["held"], "mode": whatsapp.MODE, "offer": plan["offer"],
              "touchIndex": plan["touchIndex"]}

    for target in plan["batch"]:
        message = whatsapp.compose(target, product, offer=plan["offer"])
        try:
            outcome = whatsapp.send(**message)
        except Exception as err:                  # noqa: BLE001
            _persist_failed(state, target, queue["productId"], str(err),
                           kind="SEASONAL_OFFER", channel="WHATSAPP")
            result["failed"].append({**_brief(target), "reason": str(err)})
            continue

        if not outcome["sent"]:
            result["composed"].append({**_brief(target), "text": message["text"]})
            continue

        entry = {
            "id": len(state["outreach"]) + 1,
            "cspCode": target["cspCode"], "name": target["name"],
            "productId": queue["productId"], "channel": "WHATSAPP", "kind": "SEASONAL_OFFER",
            "offerId": plan["offer"]["id"], "occasion": plan["offer"]["occasion"],
            "touchIndex": plan["touchIndex"],
            "to": target["mobile"], "text": message["text"],
            # messageId is None in dry mode (send() never generates one) -
            # only a real send has something for a DLR webhook to match
            # back against.
            "messageId": outcome.get("messageId"),
            "delivered": True, "mode": outcome["mode"], "at": store.now_iso(),
        }
        state["outreach"].append(entry)
        result["sent"].append(entry)

    return result


# ------------------------------------------------- click-nudge (WhatsApp)
# A fourth, event-triggered track (design settled 2026-08-31, after two
# other event ideas - "latent demand spike" for MATM and for PRINTER - both
# turned out to have ZERO viable targets against real data:
#   - PRINTER: 381 of 383 non-owners have literally 0 passbook prints: no
#     signal exists to trigger on.
#   - MATM: config.MATM_TXNS_PROVE_OWNERSHIP = True means ANY mATM activity
#     while claiming no device routes straight to GAP_DISPUTED (quarantined
#     for Reconciliation, not campaign-eligible) - there is no state where a
#     clean, campaign-eligible non-owner shows rising mATM activity to
#     trigger on.
# Click-nudge is the one event trigger that IS real: a CSP who clicked their
# order-page link (real, recorded intent) but never completed an order.
#
# Reuses the exact same CERF-approved template as the occasion track
# (whatsapp.compose_event, not compose) - no new CERF submission, just a
# different source for the {{5}}/{{7}} variables. See that function's
# docstring for why the deadline text must be a real, always-honourable
# commitment (e.g. "3 din" priority handling), never a fabricated price
# deadline - the static template text still closes on "Offer khatam hone se
# pehle", which this track is NOT entitled to claim about the price itself.
CLICK_NUDGE_DELAY_DAYS = 2
CLICK_NUDGE_DEADLINE_TEXT = "3 din"


def already_sent_click_nudge(state: dict, token: str) -> bool:
    """Scoped to the specific click (token), not "ever nudged" - a CSP who
    clicks again later (a genuinely renewed signal) is still reachable."""
    return any(e.get("channel") == "WHATSAPP" and e.get("kind") == "CLICK_NUDGE"
               and e.get("clickToken") == token and e.get("delivered")
               for e in state["outreach"])


def _has_any_order(state: dict, csp_code: str, product_id: str) -> bool:
    """Checked instead of trusting token["used"] alone - a CSP can be
    re-issued a fresh token (a later campaign touch) and order through
    THAT one, which would leave an earlier stale token's "used" flag at
    False forever. Any order for this (csp, product) pair, through any
    token, means they already acted - don't nudge them for not doing the
    thing they did."""
    return any(o["cspCode"] == csp_code and o["productId"] == product_id
               for o in state["orders"])


def plan_click_nudge_run(state: dict, queue: dict, limit: Optional[int] = None) -> dict:
    cap = min(limit or whatsapp.MAX_PER_RUN, whatsapp.MAX_PER_RUN)
    product_id = queue["productId"]
    targeted_by_code = {t["cspCode"]: t for t in queue["targeted"]}
    now = datetime.now(timezone.utc)

    sendable, held = [], []
    seen_csps = set()
    # state["tokens"] holds every campaign token ever issued (email AND
    # order-page), not just this product's - filtered below.
    for token, record in state["tokens"].items():
        if record.get("productId") != product_id:
            continue
        clicked_at = record.get("clickedAt")
        if not clicked_at:
            continue
        csp_code = record.get("cspCode")
        if not csp_code or csp_code in seen_csps:
            continue   # one nudge candidate per CSP per run, even with multiple stale tokens
        target = targeted_by_code.get(csp_code)
        if target is None:
            held.append({"cspCode": csp_code, "reason": "no longer a clean campaign target",
                         "kind": "NOT_TARGETED"})
            continue
        if not whatsapp.is_valid_mobile(target.get("mobile")):
            held.append({**_brief(target), "reason": "no valid mobile on file", "kind": "NO_MOBILE"})
            continue
        if _has_any_order(state, csp_code, product_id):
            held.append({**_brief(target), "reason": "already ordered", "kind": "ALREADY_ORDERED"})
            continue
        elapsed_days = (now - datetime.fromisoformat(clicked_at)).total_seconds() / 86400
        if elapsed_days < CLICK_NUDGE_DELAY_DAYS:
            held.append({**_brief(target), "reason": f"clicked only {elapsed_days:.1f}d ago - "
                        f"waiting for {CLICK_NUDGE_DELAY_DAYS}d", "kind": "TOO_SOON"})
            continue
        if already_sent_click_nudge(state, token):
            held.append({**_brief(target), "reason": "already nudged for this click", "kind": "ALREADY_SENT"})
            continue
        seen_csps.add(csp_code)
        sendable.append({**target, "clickToken": token})

    held_by_kind: dict = {}
    for h in held:
        held_by_kind[h["kind"]] = held_by_kind.get(h["kind"], 0) + 1

    return {
        "productId": product_id,
        "sendable": len(sendable),
        "willSend": min(len(sendable), cap),
        "limit": cap,
        "held": held,
        "heldByKind": held_by_kind,
        "batch": sendable[:cap],
    }


def run_click_nudge_outreach(state: dict, queue: dict, limit: Optional[int] = None) -> dict:
    """Sends the planned click-nudge batch over WhatsApp. Same
    failed-attempt bookkeeping as the other WhatsApp tracks."""
    product = config.PRODUCTS[queue["productId"]]
    plan = plan_click_nudge_run(state, queue, limit)
    result = {"sent": [], "composed": [], "failed": [],
              "held": plan["held"], "mode": whatsapp.MODE}

    for target in plan["batch"]:
        message = whatsapp.compose_event(target, product, deadline_text=CLICK_NUDGE_DEADLINE_TEXT)
        try:
            outcome = whatsapp.send(**message)
        except Exception as err:                  # noqa: BLE001
            _persist_failed(state, target, queue["productId"], str(err),
                           kind="CLICK_NUDGE", channel="WHATSAPP")
            result["failed"].append({**_brief(target), "reason": str(err)})
            continue

        if not outcome["sent"]:
            result["composed"].append({**_brief(target), "text": message["text"]})
            continue

        entry = {
            "id": len(state["outreach"]) + 1,
            "cspCode": target["cspCode"], "name": target["name"],
            "productId": queue["productId"], "channel": "WHATSAPP", "kind": "CLICK_NUDGE",
            "clickToken": target["clickToken"],
            "to": target["mobile"], "text": message["text"],
            "messageId": outcome.get("messageId"),
            "delivered": True, "mode": outcome["mode"], "at": store.now_iso(),
        }
        state["outreach"].append(entry)
        result["sent"].append(entry)

    return result
