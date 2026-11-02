"""
Every business rule in the platform, in one file.

Nothing below is derived or clever - these are the levers the business owns.
Anything a manager might want to argue about should live here, not inside the
engine, so a policy change is an edit to this file and a re-run, not a code
change.
"""

import os
from dataclasses import dataclass, field
from typing import Optional


# ------------------------------------------------------------------- products
@dataclass(frozen=True)
class Product:
    id: str
    name: str
    price: int
    coverage_target: float   # share of ACTIVE CSPs that should end up owning it
    priority: int
    requires_owned: Optional[str] = None
    # What a "latent demand" count is measured in, for this product's email
    # copy (target["latentDemand"] in engine.py) - e.g. "92 passbook prints".
    # Empty string means this product doesn't have a latent-demand signal.
    latent_unit: str = ""


PRODUCTS = {
    "MATM": Product(
        id="MATM", name="Micro ATM", price=8500,
        coverage_target=1.0, priority=1,
        latent_unit="mATM transactions",
    ),
    "PRINTER": Product(
        id="PRINTER", name="Passbook Printer", price=38350,
        coverage_target=0.5, priority=2,
        # A printer is only offered to a CSP who already runs an mATM: it is the
        # second device in the stack, not a substitute for the first.
        requires_owned="MATM",
        latent_unit="passbook prints",
    ),
}


# --------------------------------------------------------------- eligibility
# A CSP counts as active when the sheet says the terminal is Active AND they
# have been transacting recently. Terminal status alone is not enough - a
# terminal can read Active while the CSP has stopped working.
TERMINAL_ACTIVE_VALUES = ("active",)
MAX_INACTIVITY_DAYS = 30
# Below this many transactions across the two reported months, a CSP is treated
# as dormant for SELLING purposes even if technically active - pitching hardware
# to someone doing 4 transactions a month is what makes a campaign feel like spam.
MIN_TRANSACTIONS_TO_PITCH = 20


# ------------------------------------------------- ownership evidence rules
#
# The Calling Sheet carries one static claim on mATM ownership (the Yes/No
# column) and one behavioural signal (mATM transaction counts) that disagree
# for 189-207 of the CSPs flagged as needing an mATM, depending on how the
# blank-status rows are counted - CSPs marked "No mATM" that are running
# dozens to thousands of mATM transactions a month.
#
# NOTE ON A THIRD-LOOKING SIGNAL THAT ISN'T ONE: the sheet also has a "mATM
# Inactivity Days" column that reads "No mATM" for every single one of these
# disputed rows, which looks like independent corroboration for the "No"
# claim. It is not. Checked against the live sheet: whenever mATM Status =
# "No", this column reads "No mATM" in 215/215 cases with zero variation;
# whenever Status = "Yes", it instead shows real day-counts ("0 Days
# inactive", "25 Days inactive", ...). That is a formula mirroring the
# Status column, not a second, independently-maintained opinion. An earlier
# version of this file added AUTO_TRUST_MATM_TXN_THRESHOLD to auto-resolve
# disputed records above a transaction-volume cutoff, reasoning partly from
# this column as if it were confirmation. It was reverted once that turned
# out to be circular - don't re-add volume-based auto-trust without a
# genuinely independent second signal to justify it.
#
# So there is exactly one static claim and one behavioural signal in
# contradiction, with no tiebreaker in the data. Rather than pick a side, a
# disputed record is QUARANTINED: excluded from outreach and surfaced for a
# human to reconcile by phone.

# Transactions in the reporting window that prove a device is present.
# 1 is deliberate: a single genuine transaction is proof of possession.
MATM_TXN_PROVES_OWNERSHIP = 1

# Whether a "No mATM" claim that's contradicted by transactions above the
# threshold gets quarantined (True, the original default) or trusted anyway
# (False) - i.e. whether MATM_TXN_PROVES_OWNERSHIP actually blocks targeting,
# or is recorded as evidence but overridden.
#
# Set False on 2026-08-11 at product's explicit direction: the transaction
# metric is being treated the same way PRINTS_PROVE_OWNERSHIP already treats
# prints - as attributable to activity done on a device that isn't the CSP's
# own (or AePS activity the metric conflates with mATM use), not as proof.
# This is a deliberate assumption switch, not a rerun of the volume-threshold
# auto-trust idea above that got reverted for being circular - that idea used
# the mirrored "mATM Inactivity Days" column as fake corroboration; this flag
# uses no such thing, it just stops trusting the transaction signal at all.
#
# Set back to True on 2026-08-18: at the point False was chosen, ~200 active
# CSPs carried this contradiction - too many to reconcile by phone, which was
# the whole reason the sheet's claim was trusted outright instead. A live
# refresh that day showed the calling-sheet team had since corrected 21 of
# those 22 records directly (claimsMatm flipped from No/blank to Yes, matching
# their transactions - see the 2026-08-18 import diff). Only 3 active CSPs
# still contradict the sheet (Md Akhtar Hussain, Bhushan Ramesh Nandre, Rakesh
# Kumar - each 1-7 transactions). At that size, quarantining for an RM phone
# call is the cheap, correct option again, not a bottleneck - so this flag
# reverts to its original, more conservative default.
#
# Flip back to False only if the contradiction count grows large again to the
# point phone reconciliation genuinely cannot keep up.
MATM_TXNS_PROVE_OWNERSHIP = True

# Whether passbook prints prove the CSP owns a printer.
#
# Set False, which means a "No printer" claim is trusted and the prints are read
# as demand rather than as a contradiction. This is an ASSUMPTION, not something
# the sheet establishes - it holds only if a print can be attributed to a CSP who
# had it done on someone else's device.
#
# Flip this to True if that turns out to be impossible, and those records will be
# quarantined for reconciliation exactly like the mATM ones.
PRINTS_PROVE_OWNERSHIP = False


# ----------------------------------------------------- affordability bands
#
# Keyed off the latest full month's commission. The device has to be payable out
# of what the CSP actually earns; a Passbook Printer at Rs 38,350 is ~1.4 months
# of total commission for a typical candidate, so "can they afford it" is a
# targeting question, not a footnote.
@dataclass(frozen=True)
class Band:
    id: str
    label: str
    minimum: float
    offer: str


BANDS = (
    # No EMI anywhere - a single upfront payment is the only thing this
    # platform actually processes (see orders.py: one Amount Paid field, no
    # instalment tracking). The offer text used to say "EMI", which promised
    # something the system can't do; recovery-time framing makes the same
    # affordability point honestly.
    Band("STRONG", "Strong earner", 15000, "Pehle 1-2 mahine mein hi recover ho jayega"),
    Band("MID", "Mid earner", 5000, "2-3 mahine mein recover ho jayega"),
    Band("LOW", "Low earner", 1000, "Pehle ROI dikhayenge, phir aap decide kijiye"),
    Band("REVIVE", "Near-zero earner", 0,
         "Revive the business first - do not pitch hardware"),
)
UNKNOWN_BAND = Band("UNKNOWN", "No commission data", -1,
                    "Confirm earnings before pitching")

# Bands that must never be pitched a given product, however high they score.
# Selling a Rs 38,350 printer to someone earning Rs 2,000/month is not a sale,
# it is a bad debt with extra steps.
BLOCKED_FOR = {
    "MATM": ("REVIVE",),
    "PRINTER": ("REVIVE", "LOW"),
}


# ------------------------------------------------------------ prioritisation
# Score is 0-100 and drives who gets contacted first, and - for the capped
# printer target - who makes the cut at all.
SCORE_WEIGHTS = {
    "affordability": 40,   # can they pay
    "volume": 30,          # business size = how much they need it
    "latent_demand": 20,   # already doing it the hard way = proven intent
    "engagement": 10,      # logging in = reachable and running
}
# Values at which each input is considered "full marks".
SCORE_CEILINGS = {
    "commission": 25000,   # rupees/month
    "transactions": 800,   # across the two reported months
    "latent_units": 20,    # passbook prints without a printer
    "login_days": 3,
}


# -------------------------------------------------------------------- form
# The one Google Form used for every device campaign - "Eko CSP Device
# Order", created from scratch by google-apps-script/create_form.gs under
# the company account. See GOOGLE_FORM_SETUP.md. Entry IDs below are real,
# extracted from the prefilled "probe" URL that script prints on its last
# run - see create_form.gs's own comments for how that trick works.
FORM_BASE_URL = (
    "https://docs.google.com/forms/d/e/"
    "1FAIpQLScZ5D-Sa8tXTRherq-Yfy30BjsnbHCWNjWjRpkmuD5tvfiuGg/viewform"
)

FORM_ENTRY = {
    "cspCode":  "entry.651321021",
    "cspName":  "entry.1471085135",
    "mobile":   "entry.2044713935",
    "state":    "entry.1101744840",
    "district": "entry.859112102",
    "email":    "entry.1756062800",

    # These three were re-created on 2026-08-11 by repairMissingFields() -
    # they'd been missing from the live form the whole time before that, so
    # every entry ID below is from THIS repair, not the form's original
    # creation. Same getId()-vs-generated-URL discrepancy as amountPaid hit
    # again here: repairMissingFields()'s own log printed a getId()-based ID
    # for each field that did NOT match what its own probe URL actually
    # used. Using the URL-confirmed ones (see the probe URL in the repair
    # run's log, or GOOGLE_FORM_SETUP.md).
    "campaignProduct":  "entry.1307709753",   # "Campaign Product" - Short Answer
    "token":            "entry.1267485206",   # "Campaign Token"
    "deliverToAddress": "entry.483568073",    # "Deliver to the address entered above?" - Multiple Choice (radio);
                                               # NOT independently confirmed to actually pre-select "Yes" when opened -
                                               # GOOGLE_FORM_SETUP.md documents radio buttons failing to pre-fill on
                                               # this form even with a correct entry ID. Visually verify before trusting this.
    "consent":          "entry.603844005",    # "Consent" checkbox - NOT re-verified against this same bug yet;
                                               # wasn't touched by this repair run since it was never deleted

    # "Amount Paid (Rs)". probeAmountPaidEntryId()'s two log lines disagreed -
    # item.getId() said entry.1138145975, but the actual generated prefill
    # URL put the test value in entry.807187976. The URL is authoritative
    # (it's Google's own output, proven by construction); getId() is not
    # reliably the same as the real prefill entry ID for every field type.
    # Using the URL-confirmed one.
    "amountPaid": "entry.807187976",

    # Not present: pan/address/landmark/city/pincode/utr. These are always
    # CSP-filled, never prefilled - forms.py never looks them up, so there's
    # nothing to wire in here. No Payment Proof upload either - decided
    # against adding one; UTR is the proof.
}

# Comma-free labels for the new "Campaign Product" field - deliberately plain
# text ("mATM", not "mATM - Rs 8,500") so the comma-in-checkbox prefill bug
# above cannot recur here. Price is communicated in the campaign email and
# validated server-side against the issued token, not re-stated on the form.
FORM_CAMPAIGN_PRODUCT_LABEL = {
    "MATM": "mATM",
    "PRINTER": "Passbook Printer",
}


# The exact checkbox option text for each product, as it appears on the form -
# this is what gets pre-ticked, so it must match the form verbatim (Google
# Forms silently fails to pre-select a value that doesn't exactly match an
# option). Captured as ground truth from the live form rather than derived
# from PRODUCTS.name/.price: the form's label is "mATM", not this platform's
# "Micro ATM", and it is an external string this code does not own - deriving
# it would silently break the moment the two vocabularies disagree, exactly
# like the mismatch this comment used to have before it was caught.
FORM_PRODUCT_OPTION = {
    "MATM": "mATM - Rs 8,500",
    "PRINTER": "Passbook Printer - Rs 38,350",
}


def _check_form_product_prices_match() -> None:
    """Fails loudly at import time if PRODUCTS.price ever drifts from the
    price baked into the form's option text, instead of silently pre-ticking
    a checkbox next to the wrong number."""
    for pid, option in FORM_PRODUCT_OPTION.items():
        product = PRODUCTS.get(pid)
        if product is None:
            continue
        expected = f"Rs {product.price:,}"
        if expected not in option:
            raise AssertionError(
                f"FORM_PRODUCT_OPTION[{pid!r}] = {option!r} does not contain "
                f"{expected!r} - PRODUCTS[{pid!r}].price has changed without "
                "updating the form (or vice versa). Fix the form's option text "
                "and this mapping together."
            )


_check_form_product_prices_match()

# Where completed form responses land, so the platform can read them without
# anyone manually exporting a CSV each time. Same "Anyone with the link can
# view" live-fetch pattern as import_calling_sheet.py - the sheet's sharing
# must be set to "Anyone with the link can view" for orders.fetch_response_rows
# to reach it (it was created by create_form.gs under the company account,
# which defaults new files to private - this needs a manual sharing change).
RESPONSE_SHEET_URL: Optional[str] = (
    "https://docs.google.com/spreadsheets/d/1oUVoZZfMZQ3xcyKDEOYT6WjlDjj16Eq4hXkk6YJUBmw/edit"
)

# Google Forms writes the question text itself as the response sheet's column
# headers. This maps that verbatim text to the platform's internal field
# names - keep it in sync with the exact wording used when GOOGLE_FORM_SETUP.md
# is actioned, the same "external string, not derived" caution as
# FORM_PRODUCT_OPTION above.
RESPONSE_HEADER_MAP = {
    "CSP Code": "cspCode",
    "State": "state",
    "Pincode": "pincode",
    "Campaign Product": "campaignProduct",
    "Campaign Token": "token",
    "Deliver to the address entered above?": "deliverToAddress",
    "Consent": "consent",
    "Amount Paid (Rs)": "amountPaid",
    "UTR / Reference No": "utr",
    "Email": "email",
    "Mobile (WhatsApp)": "mobile",
    "Timestamp": "timestamp",

    # NOT a form question - finance types into this column by hand, directly
    # in the response sheet, after checking the bank statement. Not part of
    # the Google Form at all, so no GOOGLE_FORM_SETUP.md entry - see that
    # doc's "Finance Remark column" section for the exact header text finance
    # needs to add.
    "Finance Remark": "financeRemark",

    # Also not a form question - ops types the AWB/tracking number into this
    # column by hand once a courier picks the order up (added by
    # google-apps-script/cleanup_and_add_tracking_column.gs). Same
    # poll-driven pattern as Finance Remark: see
    # orders._try_attach_tracking_from_sheet.
    "Tracking ID": "trackingId",
}

# What finance can type in that column to mean "yes, the money is really
# there" - lenient/case-insensitive on purpose, since asking finance to type
# an exact magic word is how this silently stops working.
FINANCE_CREDITED_VALUES = {"credited", "yes", "y", "confirmed", "verified", "ok", "done"}


# ---------------------------------------------------------- address sanity
# A coarse, cheap typo/fraud check: India Post's PIN zones are keyed off the
# declared state, and a delivery pincode whose first digit doesn't match its
# state's zone is a strong signal the address was mistyped - not proof, but
# enough to hold an order for a human look before a device ships to a likely
# wrong address. One digit only, on purpose - it exists to catch an obviously
# wrong address, not to certify a correct one. Covers exactly the 14 states
# on the form's State dropdown (forms.py's _STATE_OPTIONS); a CSP who picked
# "Other" can't be checked and is never flagged.
#
# Known blind spot: Uttar Pradesh and Uttarakhand share zone "2", and
# Maharashtra and Madhya Pradesh share zone "4" - a pincode swapped between
# either pair won't be caught by a one-digit check.
POSTAL_ZONE_BY_STATE = {
    "Delhi": "1", "Haryana": "1", "Punjab": "1", "Himachal Pradesh": "1",
    "J & K": "1", "Chandigarh": "1",
    "Uttar Pradesh": "2", "Uttarakhand": "2",
    "Rajasthan": "3",
    "Maharashtra": "4", "Madhya Pradesh": "4",
    "West Bengal": "7",
    "Bihar": "8", "Jharkhand": "8",
}


# ------------------------------------------------------------------- orders
# One order per submitted response, moved through its stages with exactly one
# human step - finance typing a credited remark - and zero clicks otherwise:
#
#   SUBMITTED -> (finance types a credited remark in the response sheet,
#                 amount + UTR still checked as a safety net)  PAYMENT_VERIFIED
#             -> (after AUTO_DISPATCH_AFTER_DAYS, unless the
#                 delivery address looks mistyped - see
#                 POSTAL_ZONE_BY_STATE above)                  DISPATCHED
#             -> (after AUTO_DELIVER_AFTER_DAYS more)          DELIVERED
#
# There is no bank API or courier API behind this (see orders.py's module
# docstring) - PAYMENT_VERIFIED depends on finance's remark, not a live bank
# feed, and DISPATCHED/DELIVERED are timers, not real shipment events.
# REJECTED stays available as a manual override for the rare submission
# finance flags as wrong - an exception path, not a required step.
#
# Ownership (Has mATM / Has Passbook Printer) is NOT updated until DELIVERED
# - a paid-for order that never arrives should not read as coverage gained.
ORDER_STATUSES = (
    "SUBMITTED",          # CSP submitted the form; waits on finance's remark
    "PAYMENT_VERIFIED",   # finance marked it credited - triggers the "Payment Received" email
    "DISPATCHED",         # AUTO_DISPATCH_AFTER_DAYS after payment - triggers the "Dispatched" email
    "DELIVERED",          # AUTO_DELIVER_AFTER_DAYS after dispatch - triggers the "Delivered" email, ownership flips here
    "REJECTED",           # manual override only, for a submission finance flags as wrong
)
# How long an order sits in a stage before the background sweep
# (orders.auto_advance, run by run.py's daemon thread) moves it on with no
# one clicking anything. Business-tunable, not a technical constant.
AUTO_DISPATCH_AFTER_DAYS = 2
AUTO_DELIVER_AFTER_DAYS = 5

# Reaching this is what flips Has mATM / Has Passbook Printer.
OWNERSHIP_CONFIRMING_STATUSES = ("DELIVERED",)

# ------------------------------------------------------------------ couriers
# Real courier tracking (app/tracking.py, track_dtdc.py / track_delhivery.py /
# track_xpressbees.py / track_bluedart.py at the repo root) - an order only
# gets checked once a courier + tracking number is attached at dispatch time
# (orders.dispatch). AUTO_DELIVER_AFTER_DAYS above remains the fallback for
# any order dispatched without one, or where checks never resolve.
COURIERS = ("DTDC", "DELHIVERY", "XPRESSBEES", "BLUEDART")

# Don't re-check a shipment more often than this - a courier's status page
# doesn't change minute to minute, and each check is a real, comparatively
# slow hit against a live courier site (see app.tracking's module docstring
# for the DTDC/Blue Dart captcha-retry cost specifically).
TRACKING_CHECK_INTERVAL_HOURS = 4

# When run.py's background thread re-fetches the live Calling Sheet
# (import_calling_sheet.import_live) with no one running the command by hand.
# Checked against csps.json's own "importedAt" timestamp, not a separate
# counter - so restarting the server never causes an early re-fetch.
#
# TWO rules, and the hour is the one that matters:
#
# CALLING_SHEET_REFRESH_AT_HOUR is a local-time deadline, not an interval. The
# refresh lands on the first background tick at or after this hour each day,
# which puts fresh data in place BEFORE the morning outreach send - the whole
# point being that a campaign email must never be built on yesterday's sheet.
# Ticks are 15 minutes apart (run.py's _AUTO_ADVANCE_INTERVAL_SECONDS), so 9
# here means the import completes by ~09:15 local, comfortably ahead of a 10:00
# send. Raise it only with that gap in mind.
#
# An interval alone cannot give that guarantee: "every 24 hours" drifts later
# by however long the previous run took, so a refresh that starts at 09:50
# today happens after 10:00 within a few days and the send quietly goes out on
# stale data. The hour anchors it; the interval below is only a safety net.
CALLING_SHEET_REFRESH_AT_HOUR = 9   # 24-hour clock, LOCAL time

# Fallback maximum age, for what the daily deadline alone cannot cover: if the
# server was down all morning and starts at 14:00, data older than this is
# re-fetched immediately rather than waiting for tomorrow's deadline.
CALLING_SHEET_REFRESH_HOURS = 24


# ------------------------------------------------------------------ outreach
# Off by default. Sending the actual "buy a Micro ATM / printer" pitch has
# always required someone to trigger it (dashboard click or
# POST /api/outreach/<product_id>) - see run.py's _outreach_loop for the
# scheduled equivalent. Flipping EMAIL_MODE to live is a decision about HOW
# a send behaves once triggered, not a decision to also start sending
# unattended on a schedule - this flag is the separate, explicit opt-in for
# that. Same EMAIL_MAX_PER_RUN cap and CAMPAIGN_SEND_DAY_OFFSETS schedule
# apply either way; this only removes the "someone has to remember to
# click" dependency.
OUTREACH_SCHEDULE_ENABLED = True

# What time of day the scheduled send runs, local, 24-hour clock. Like
# CALLING_SHEET_REFRESH_AT_HOUR this is a deadline, not an interval, and for
# the same reason: an interval anchored to process start means the send drifts
# to whenever the server was last restarted, which is exactly how a campaign
# ends up being built on yesterday's sheet.
#
# It MUST be later than CALLING_SHEET_REFRESH_AT_HOUR - the whole point of the
# pairing is that the day's sheet is already imported before a single email is
# composed from it. _check_outreach_runs_after_refresh() below refuses to let
# the app start if that ordering is ever broken.
#
# Runs in BATCHES across the day, not one send-everyone-at-once trigger -
# decided 2026-08-18 after concluding a single large send is a deliverability
# risk on a sending identity with no track record yet (108 of 120 reachable
# CSPs are on Gmail, which is unforgiving about sudden volume from a new
# sender). OUTREACH_SEND_AT_HOUR is the FIRST batch of the day; every
# OUTREACH_BATCH_INTERVAL_HOURS after that, another batch fires, each capped
# at EMAIL_MAX_PER_RUN per product (see emailer.MAX_PER_RUN) - so a 250-email
# day at 50/product/batch takes several batches to clear, not one.
#
# Like CALLING_SHEET_REFRESH_AT_HOUR this first-batch hour is a deadline, not
# an interval, for the same drift reason. It MUST be later than
# CALLING_SHEET_REFRESH_AT_HOUR - the whole point of the pairing is that the
# day's sheet is already imported before a single email is composed from it.
# _check_outreach_runs_after_refresh() below refuses to let the app start if
# that ordering is ever broken.
OUTREACH_SEND_AT_HOUR = 10

# Gap between batches, once the day's first one has fired. Chosen so a queue
# too big for one batch clears across working hours rather than either all at
# once or dragging past OUTREACH_LAST_BATCH_HOUR into the night.
OUTREACH_BATCH_INTERVAL_HOURS = 2

# Safety bound, not requested outright but added deliberately: without a
# cutoff, a large enough queue would keep firing batches every
# OUTREACH_BATCH_INTERVAL_HOURS past midnight until it ran out of people to
# email. No batch starts at or after this local hour - whatever's left over
# waits for tomorrow's first batch instead. Adjust freely; this exists so a
# big queue day fails safe (spills to tomorrow) rather than failing loud
# (3 AM sends).
OUTREACH_LAST_BATCH_HOUR = 20

# Legacy interval, kept only because the startup banner used to report a
# cadence. The loop is driven by OUTREACH_SEND_AT_HOUR / _BATCH_INTERVAL_HOURS
# / _LAST_BATCH_HOUR above, not by this.
OUTREACH_RUN_INTERVAL_HOURS = 24


def _check_outreach_runs_after_refresh() -> None:
    """Fails loudly at import time if the scheduled send would run before, or
    in the same hour as, the calling-sheet refresh - which would mean emailing
    off yesterday's data without anything visibly going wrong."""
    if OUTREACH_SEND_AT_HOUR <= CALLING_SHEET_REFRESH_AT_HOUR:
        raise AssertionError(
            f"OUTREACH_SEND_AT_HOUR ({OUTREACH_SEND_AT_HOUR}) must be later than "
            f"CALLING_SHEET_REFRESH_AT_HOUR ({CALLING_SHEET_REFRESH_AT_HOUR}) - "
            "otherwise the day's campaign is composed from the previous day's "
            "Calling Sheet. Leave at least an hour between them; the refresh "
            "lands on a 15-minute tick, not instantly."
        )


_check_outreach_runs_after_refresh()


# -------------------------------------------------------- whatsapp schedule
# Off by default (same reasoning as OUTREACH_SCHEDULE_ENABLED above), turned
# ON 2026-08-31 per explicit instruction: unattended sending, no dashboard
# click or manual script needed. See run.py's _whatsapp_schedule_loop.
#
# Structurally simpler than the email scheduler above - it doesn't need an
# hour-by-hour batch cadence, because offer_calendar.py already answers
# "should anything go out today" (only 3 touch-days per occasion window;
# every other day is a no-op by design - see that module's docstring on why
# not daily). What this loop adds is just the timing WITHIN a touch day: it
# only sends inside a working-hours window (WHATSAPP_SCHEDULE_START_HOUR to
# WHATSAPP_SCHEDULE_END_HOUR), same courtesy-hours reasoning as
# OUTREACH_LAST_BATCH_HOUR - a CSP's phone buzzing at 3 AM is a worse first
# impression than the message arriving a few hours later.
WHATSAPP_SCHEDULE_ENABLED = True

# Every tick in this window, the loop asks outreach.run_offer_outreach for
# each product - which is itself a no-op (sendable=0) on a non-touch-day, or
# once everyone due today has already been sent (dedup via
# already_sent_offer_touch). A big touch-day queue (256 CSPs, as Rakhi 2026
# was) clears over several ticks at WHATSAPP_MAX_PER_RUN per tick, same
# shape as the email scheduler's multi-batch day.
WHATSAPP_SCHEDULE_START_HOUR = 10
WHATSAPP_SCHEDULE_END_HOUR = 20
WHATSAPP_SCHEDULE_TICK_MINUTES = 15


# A CSP gets an 8-email sequence per product across a 30-day window, not one
# email forever and not a generic reminder re-send - each email argues a
# different, genuine reason to buy (see emailer.py's _TOUCH_COPY: ROI math,
# peer adoption, friction removal, ownership framing, cost of waiting,
# genuine this-cycle urgency, direct close). Business-chosen cadence: Day 1,
# 3, 6, 9, 12, 16, 20, 24 since the CSP's first touch - dense early on, then
# spreading out, with the last 6 days of the 30-day window (25-30) always
# left silent as a deliberate cool-off before the sequence just stops (no
# repeat cycle - see outreach.next_touch, which also anchors every touch to
# the FIRST send's timestamp rather than the previous one's, so a missed
# daily run never drifts the whole schedule).
CAMPAIGN_SEND_DAY_OFFSETS = (0, 2, 5, 8, 11, 15, 19, 23)   # Day 1, 3, 6, 9, 12, 16, 20, 24

# The sequence REPEATS. A CSP who has had all 8 emails without ordering starts
# the whole sequence again CAMPAIGN_CYCLE_DAYS after that cycle began - so the
# 6 days between the day-24 email and day 30 are the gap between cycles, not
# the end of contact.
#
# Measured from the cycle's OWN first email, not from its last, so a cycle
# always occupies exactly this many days regardless of whether the daily run
# was late on any individual touch (see outreach.sequence_position).
#
# Worth understanding before raising this number's frequency: a CSP only leaves
# this loop by ordering (ownership flips at DELIVERED and engine.build_queue
# stops targeting them) or by a human removing them. There is no automatic
# stop, by design and by request - CAMPAIGN_MAX_CYCLES below is the one lever
# that adds one back.
CAMPAIGN_CYCLE_DAYS = 30

# Hard stop on how many full 8-email cycles one CSP may receive for one
# product. None = no limit, which is the configured behaviour: the sequence
# repeats monthly until they buy. Set an integer (e.g. 3) if repeated
# non-response should hand the CSP to an RM instead of continuing to email
# them - the platform will then hold them as SEQUENCE_EXHAUSTED and stop.
CAMPAIGN_MAX_CYCLES = None


# -------------------------------------------------------------------- token
# Still issued and prefilled into every campaign link (app/forms.py) purely
# because the live Google Form has "Campaign Token" as a required field -
# removing it would mean editing the form again. It is NOT used to validate
# or gate a submission any more; every submission is trusted and becomes an
# order regardless of its token. See the "why this got simpler" note in
# orders.py's module docstring.
TOKEN_BYTES = 12   # -> 16-character urlsafe token


# ---------------------------------------------------------- order page
# The self-serve page (frontend/order.html) that replaces the prefilled
# Google Form link - see design/order-page-mockup.html and
# design/order-page-build-plan.html for the full design history.

# Shown on the payment screen for the "bank transfer" and "static QR"
# methods - both settle to the same account, QR is just a faster way to pay
# it (see design/payment-gateway-workflow.html: no payment gateway, so no
# dynamic per-order amount either).
#
# These are DISPLAY data, not credentials - a receiving account shown to
# every CSP who opens the order page, exactly as the Google Form already
# shows them in its Payment Details section (see GOOGLE_FORM_SETUP.md step
# 6). That's why they live here in config.py rather than .env: nothing here
# grants access to anything, and keeping them next to the other
# "what the CSP sees" settings makes a change one obvious edit. Note this
# file IS tracked by git, so these values will be in its history.
BANK_BENEFICIARY_NAME = "Eko Bharat Ventures Pvt Ltd"
BANK_ACCOUNT_NUMBER = "00032000039765"
BANK_IFSC = "HDFC0009141"
BANK_BRANCH = ("HDFC Bank Ltd, Millennium Plaza, Unit 1, Millennium Towers, "
               "Near Max Hospital, Sector 27, Gurgaon 122002, Haryana")
# Flipped to True once the four values above are real - the order page
# disables its submit button and shows a "details not set" message while
# this is False, so a half-configured deployment can never take an order
# nobody can actually pay for.
ORDER_PAGE_BANK_DETAILS_SET = True

# The static UPI QR code image (see design/payment-gateway-workflow.html's
# "static QR" decision - no payment gateway, so this is a plain image, not a
# generated per-order code). Point this at a file under frontend/ once it
# exists (e.g. "qr-payment.png") and flip ORDER_PAGE_QR_ENABLED - until then
# the order page simply doesn't offer QR as a payment method, only bank
# transfer. Deliberately a feature flag, not an empty-string check on the
# path, so "QR isn't ready yet" is a one-line, obviously-temporary toggle.
# The filename is resolved RELATIVE to the order page, not the host root,
# for the same path-prefix reason as order.js's API base - keep it a bare
# filename ("qr-payment.png"), not a leading-slash path.
ORDER_PAGE_QR_IMAGE = os.environ.get("ORDER_PAGE_QR_IMAGE") or None
ORDER_PAGE_QR_ENABLED = (os.environ.get("ORDER_PAGE_QR_ENABLED", "").strip().lower()
                          in ("true", "1", "yes", "on"))

# When True, a campaign email's /go/<token> link redirects into the order
# page (pre-filled via resolve-token) instead of the Google Form - so email
# and WhatsApp both land on the same order page, email just arriving
# pre-filled and click-tracked because it has a token to route through
# (see emailer.click_tracked_form_url).
#
# DEFAULTS TO OFF, deliberately: the safe state has to be the one you get
# by doing nothing. A deploy to a server whose .env never mentions this
# keeps every campaign link pointing at the Google Form, exactly as before,
# so shipping the order page and switching email over to it are two
# separate decisions rather than one accidental one.
#
# Emails ALREADY SENT switch over the moment this flips - the destination
# is decided at click time by the /go/<token> redirect, not baked into the
# message body - so no campaign ever needs re-sending, in either direction.
# That also makes it a real mid-campaign undo: set it back to false,
# restart, and every link reverts to the form.
#
# CHECK BOTH BEFORE TURNING IT ON:
#   1. config.ORDER_PAGE_SHEET_MIRROR_URL is set. Without the mirror an
#      order-page order never reaches the response sheet, so finance can't
#      verify it and it sits at SUBMITTED (see google-apps-script/
#      order_sink.gs). The read-back side is already safe -
#      orders._adopt_order_page_row stops the mirrored row creating a
#      duplicate - but nothing can verify an order that isn't there.
#   2. The order page is actually reachable at ORDER_PAGE_BASE_URL. If the
#      app moves, that setting (or TRACKING_BASE_URL) has to move with it,
#      or every campaign link in every already-sent email breaks at once.
#
# On auth, since it's the obvious next question: the order page's own three
# endpoints (config, lookup, resolve-token, submit) are deliberately public
# - see app/orderpage.py's module docstring for exactly what they expose.
# Everything else (the dashboard shell and every other /api/*) sits behind
# DASHBOARD_PASSWORD, which is read from .env in run.py rather than set
# here, same as emailer.py's SMTP_PASS - a secret, unlike the display data
# above.
#
# Read from .env so it can be switched on the server WITHOUT a code change
# or redeploy - the one setting most likely to need flipping in a hurry.
ORDER_PAGE_LIVE = (os.environ.get("ORDER_PAGE_LIVE", "false").strip().lower()
                   in ("true", "1", "yes", "on"))

# Where the order page is reachable from the outside - what goes into a
# WhatsApp message, or anywhere else a link has to be written down rather
# than redirected to.
#
# Derived from TRACKING_BASE_URL (the .env setting the open-pixel and
# /go/<token> links already use) rather than being a second address to keep
# in sync: the order page is served by the same app, off the same root, so
# there is exactly one "where is this deployed" answer. Override with
# ORDER_PAGE_BASE_URL in .env only if the order page is ever fronted by a
# different hostname to the dashboard (e.g. csp.eko.co.in for CSPs while
# the dashboard stays internal) - that's the one case the two genuinely
# differ.
#
# Merged into the Device Monitor, this app is served at <dashboard>/growth, so
# the one "where is this deployed" answer is the dashboard's own public address
# (CAMPAIGN_PUBLIC_BASE_URL, e.g. https://indev.eko.in/csp-device-heartbeat)
# plus "/growth". GROWTH_PUBLIC_BASE_URL overrides it outright.
_CAMPAIGN_BASE = (os.environ.get("CAMPAIGN_PUBLIC_BASE_URL") or "").strip().rstrip("/")
PUBLIC_BASE_URL = (
    (os.environ.get("GROWTH_PUBLIC_BASE_URL") or "").strip()
    or (_CAMPAIGN_BASE + "/growth" if _CAMPAIGN_BASE else "")
    or "http://localhost:5100/growth"
).rstrip("/")

ORDER_PAGE_BASE_URL = (
    os.environ.get("ORDER_PAGE_BASE_URL")
    or PUBLIC_BASE_URL
).rstrip("/")

# Apps Script Web App URL (see google-apps-script/order_sink.gs) that
# mirrors an order-page submission into the SAME response sheet the Google
# Form already writes to (config.RESPONSE_SHEET_URL) - so finance keeps one
# place to look regardless of which path a CSP ordered through. None skips
# the mirror silently (the order is still saved to state.json either way -
# see orderpage.submit's docstring on why state.json comes first). Deploying
# the script requires a Google login this environment doesn't have (same
# limitation as GOOGLE_FORM_SETUP.md) - run it once from script.google.com
# under the account that owns the response sheet, then paste the printed
# web app URL here.
ORDER_PAGE_SHEET_MIRROR_URL: Optional[str] = (
    os.environ.get("ORDER_PAGE_SHEET_MIRROR_URL") or None
)
