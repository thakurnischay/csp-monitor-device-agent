"""
The numbers behind "where are we actually at", and the honest limits on them.

Everything here is DERIVED from data the platform already keeps - outreach
entries, tokens, orders, and the daily rows store.record_snapshot writes. None
of it is a second source of truth, and nothing is estimated or modelled: if a
figure cannot be known, it is reported as unknown rather than filled in.

Two limits worth stating up front, because a chart that silently hides them is
worse than no chart:

1. COVERAGE HAS NO PAST. Ownership is recomputed from whatever the current
   Calling Sheet import says, so before store.record_snapshot existed there is
   simply no record of what coverage was on any earlier date. The trend starts
   the first day this ran and cannot be backfilled - see snapshot_series().

2. OPENS AND CLICKS DEPEND ON A REACHABLE TRACKING URL. The pixel and the
   /go/<token> redirect are served by THIS app, so if TRACKING_BASE_URL points
   at localhost (the default) no CSP's mail client can ever reach them and both
   figures are permanently zero - not because nobody opened the mail, but
   because nothing could report it. tracking_health() reports that state so the
   funnel can say so out loud instead of showing a truthful-looking 0%.
"""

import collections
from typing import Optional

from . import config, emailer


# ------------------------------------------------------------------ tracking
def tracking_health() -> dict:
    """Whether open/click tracking can physically work right now."""
    base = (emailer.TRACKING_BASE_URL or "").lower()
    unreachable = ("localhost" in base or "127.0.0.1" in base or not base)
    return {
        "baseUrl": emailer.TRACKING_BASE_URL,
        "reachable": not unreachable,
        # Said plainly, because this is the difference between "nobody opened
        # our email" and "we never had any way of finding out".
        "note": (
            "The tracking address points at this machine, which no CSP's email "
            "client can reach - so opens and clicks cannot be recorded at all. "
            "Set CAMPAIGN_PUBLIC_BASE_URL (or GROWTH_PUBLIC_BASE_URL) on the server "
            "to its public https address to start measuring them. Emails already "
            "sent stay unmeasurable."
        ) if unreachable else None,
    }


# -------------------------------------------------------------------- funnel
def funnel(state: dict, result: dict) -> list:
    """
    The campaign end to end, per stage, as counts of DISTINCT CSPs.

    Distinct CSPs, not messages: with an 8-touch sequence one CSP produces up
    to 8 outreach rows, and a funnel that counted rows would show "emailed"
    climbing past the number of people who exist.
    """
    campaign = [e for e in state["outreach"]
                if e.get("delivered") and e.get("kind", "CAMPAIGN") == "CAMPAIGN"]

    emailed = {e["cspCode"] for e in campaign}
    tokens = state.get("tokens", {})
    opened = {t["cspCode"] for t in tokens.values() if t.get("openedAt")}
    clicked = {t["cspCode"] for t in tokens.values() if t.get("clickedAt")}
    ordered = {o["cspCode"] for o in state["orders"]}
    delivered = {o["cspCode"] for o in state["orders"] if o.get("status") == "DELIVERED"}

    # The top of the funnel is everyone the engine considers reachable and
    # worth pitching - targeted plus those ranked below the cap, since the
    # deferred ones are still in the programme, just not this cycle.
    reachable = 0
    for queue in result["queues"].values():
        reachable += len(queue["targeted"]) + len(queue.get("deferred", []))

    health = tracking_health()
    blind = not health["reachable"]

    return [
        {"stage": "In target queues", "count": reachable, "measurable": True},
        {"stage": "Emailed at least once", "count": len(emailed), "measurable": True},
        {"stage": "Opened an email", "count": len(opened), "measurable": not blind},
        {"stage": "Clicked through to form", "count": len(clicked), "measurable": not blind},
        {"stage": "Submitted an order", "count": len(ordered), "measurable": True},
        {"stage": "Device delivered", "count": len(delivered), "measurable": True},
    ]


# ------------------------------------------------------------------ activity
def sends_per_day(state: dict, days: int = 30) -> list:
    """Campaign emails actually sent, per calendar day, oldest first.

    Zero-filled across the whole window rather than skipping empty days -
    a line chart that omits quiet days silently compresses time and makes a
    week of silence look like a steady drip.
    """
    campaign = [e for e in state["outreach"]
                if e.get("delivered") and e.get("kind", "CAMPAIGN") == "CAMPAIGN"]
    if not campaign:
        return []

    by_day = collections.Counter(e["at"][:10] for e in campaign)
    from datetime import date, timedelta
    last = max(by_day)
    end = date.fromisoformat(last)
    start = end - timedelta(days=days - 1)
    earliest = date.fromisoformat(min(by_day))
    if earliest > start:
        start = earliest

    out, cursor = [], start
    while cursor <= end:
        key = cursor.isoformat()
        out.append({"date": key, "count": by_day.get(key, 0)})
        cursor += timedelta(days=1)
    return out


def touch_spread(state: dict) -> list:
    """How many sends have gone out at each step of the 8-touch sequence.

    Entries written before the sequence existed carry no "touch" field; they
    are counted as touch 1, which is what they actually were, rather than
    being dropped and making the totals disagree with the activity feed.
    """
    campaign = [e for e in state["outreach"]
                if e.get("delivered") and e.get("kind", "CAMPAIGN") == "CAMPAIGN"]
    counts = collections.Counter(e.get("touch", 1) for e in campaign)
    return [{"touch": i + 1, "day": off + 1, "count": counts.get(i + 1, 0)}
            for i, off in enumerate(config.CAMPAIGN_SEND_DAY_OFFSETS)]


# ------------------------------------------------------------------ coverage
def coverage_now(result: dict) -> list:
    """Current coverage per product, with the gap to target spelled out."""
    out = []
    for pid, cov in result["coverage"].items():
        out.append({
            "productId": pid,
            "productName": cov["productName"],
            "owned": cov["owned"],
            "activeBase": cov["activeBase"],
            "targetCount": cov["targetCount"],
            "targetPct": cov["targetPct"],
            "coveragePct": cov["coveragePct"],
            "remaining": cov["remaining"],
        })
    return out


def snapshot_series(state: dict) -> dict:
    """The only genuine history in the platform - see this module's docstring.

    Returns the rows plus an explicit note when there is too little history to
    draw a trend from, so the UI can say "history starts here" instead of
    rendering a one-point line that implies a flat trend.
    """
    rows = state.get("snapshots", [])
    return {
        "points": rows,
        "days": len(rows),
        # Two points is the minimum that can show a direction at all.
        "enoughForTrend": len(rows) >= 2,
        "startedOn": rows[0]["date"] if rows else None,
    }


def snapshot_metrics(state: dict, result: dict) -> dict:
    """The subset of today's numbers worth keeping forever, kept deliberately
    small - this row is written every day and never deleted."""
    campaign = [e for e in state["outreach"]
                if e.get("delivered") and e.get("kind", "CAMPAIGN") == "CAMPAIGN"]
    cov = {pid: {"owned": c["owned"], "coveragePct": c["coveragePct"],
                 "activeBase": c["activeBase"], "targetCount": c["targetCount"]}
           for pid, c in result["coverage"].items()}
    return {
        "coverage": cov,
        "cspsEmailed": len({e["cspCode"] for e in campaign}),
        "emailsSent": len(campaign),
        "orders": len(state["orders"]),
        "delivered": sum(1 for o in state["orders"] if o.get("status") == "DELIVERED"),
    }


# --------------------------------------------------------------------- build
def build(state: dict, result: dict) -> dict:
    """Everything the Overview tab needs, in one payload."""
    return {
        "funnel": funnel(state, result),
        "sendsPerDay": sends_per_day(state),
        "touchSpread": touch_spread(state),
        "coverage": coverage_now(result),
        "history": snapshot_series(state),
        "tracking": tracking_health(),
    }
