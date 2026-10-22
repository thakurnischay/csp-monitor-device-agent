"""Business-rule subset of the email project's config.py (products, eligibility, ownership evidence rules, bands, scoring),
copied VERBATIM so ownership verdicts are identical. Everything else in the original (bank details, form fields, order/courier/
schedule settings) is deliberately NOT carried over. See SOURCE.txt for the original file's checksum."""

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


