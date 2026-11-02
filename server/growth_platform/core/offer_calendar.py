"""
The project-maintained calendar of festival/occasion offers - what decides
"what offer do we give today" for the WhatsApp generic-offer template (see
whatsapp.py).

backend/data/offer_calendar.json is the single file to edit for a new
occasion: add an entry with its date window and a reference (anchor) price
per product. It deliberately does NOT carry the actual selling price - that
always comes from config.PRODUCTS[id].price, so there is no way for this
file to drift into quoting a CSP a price different from what the order page
actually charges (the exact bug flagged twice during the Rakhi 2026 draft,
when a typed reference price of 8,499/38,500 turned out to disagree with the
real 8,500/38,350 on the live order page).

"dateConfirmed": false on an entry means its date window is an estimate,
not verified against an official panchang/calendar - most of the 2026-08-27
batch (Holi, Eid-ul-Fitr, Akshaya Tritiya, Eid-ul-Adha, Ganesh Chaturthi,
Navratri-Dussehra, Dhanteras, Diwali, Chhath Puja) are lunar-calendar
festivals whose exact dates were not independently verified when added -
confirm the real date before relying on that entry to actually fire on the
right day, then flip this to true. Fixed/solar-calendar entries (Makar
Sankranti, Republic Day, Baisakhi, Independence Day, Rakshabandhan,
Christmas-New Year) are marked true. This field is informational only -
active_offer() does not filter on it, so an unconfirmed entry still fires
on its stated dates.

"Today" is evaluated in IST, not server-local or UTC, since every CSP and
every offer deadline is India-specific - a UTC day boundary would flip the
active offer 5.5 hours early/late relative to what a CSP actually
experiences.

Sending is NOT daily across the whole window. Decided 2026-08-27: a CSP gets
at most 3 WhatsApp messages per occasion - the window's opening day
("announce"), its midpoint ("peak"), and its closing day ("last-chance") -
not one every day the window is open. An earlier daily-send idea was
dropped because WhatsApp/Meta tracks block and complaint rate per number
and can throttle or restrict a WABA's messaging ability if it gets too
high; ~9 days of daily marketing texts per occasion, across ~14
occasions/year, was judged too likely to trigger that. See touch_dates()
and today_touch_index() below - outreach.py only sends on a day that is
one of the 3 returned dates, and only once per (CSP, occasion, touch).
"""

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

IST = timezone(timedelta(hours=5, minutes=30))

# Ships with the code (festival dates, not operator data), so it is not in GROWTH_DATA_DIR.
_CALENDAR_PATH = Path(__file__).resolve().parent.parent / "offer_calendar.json"


def today_ist() -> date:
    return datetime.now(IST).date()


def load_calendar() -> list:
    if not _CALENDAR_PATH.exists():
        return []
    with open(_CALENDAR_PATH, encoding="utf-8") as f:
        return json.load(f)


def active_offer(product_id: str, on_date: Optional[date] = None) -> Optional[dict]:
    """The occasion offer active for `product_id` on `on_date` (default:
    today, IST), or None if nothing in the calendar covers this date - which
    means genuinely no offer to send, not a fallback to some default price
    framing. Calendar entries are not expected to overlap for the same
    product; if they do, the first match in file order wins."""
    on_date = on_date or today_ist()
    for entry in load_calendar():
        ref_prices = entry.get("referencePrice", {})
        if product_id not in ref_prices:
            continue
        start = date.fromisoformat(entry["startDate"])
        end = date.fromisoformat(entry["endDate"])
        if start <= on_date <= end:
            return {
                "id": entry["id"],
                "occasion": entry["occasion"],
                "referencePrice": ref_prices[product_id],
                "startDate": entry["startDate"],
                "endDate": entry["endDate"],
                "dateConfirmed": entry.get("dateConfirmed", False),
            }
    return None


def touch_dates(offer: dict) -> list:
    """The (up to) 3 send dates within `offer`'s window, as ISO strings:
    window start, midpoint, window end. Works for any window length,
    including short ones (e.g. a 5-day window still yields 3 distinct
    dates) - see this module's docstring for why 3, not daily."""
    start = date.fromisoformat(offer["startDate"])
    end = date.fromisoformat(offer["endDate"])
    mid = start + (end - start) // 2
    return [start.isoformat(), mid.isoformat(), end.isoformat()]


def today_touch_index(offer: dict, on_date: Optional[date] = None) -> Optional[int]:
    """Which of touch_dates()'s 3 slots `on_date` (default: today, IST) is,
    or None if today is inside the offer window but is not itself a send
    day. Callers must treat None as "hold - not a send day", never as
    "send anyway"."""
    on_date = on_date or today_ist()
    for i, iso in enumerate(touch_dates(offer)):
        if date.fromisoformat(iso) == on_date:
            return i
    return None
