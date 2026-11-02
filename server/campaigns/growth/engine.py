"""
The decision engine: who owns what, who genuinely has a gap, who can afford to
close it, and in what order they should be contacted.

Pure functions over plain dicts - no I/O, no reading the clock beyond what is
passed in. That keeps it testable and keeps the rules auditable, which matters
because these decisions get spent as real calls to real people.
"""

import math
from typing import Optional

from . import config


# --------------------------------------------------------------- ownership
#
# Four verdicts, because "do they own one" genuinely has four answers here and
# collapsing them to a boolean is what produces embarrassing sales calls:
#
#   OWNED         - they have it; nothing to sell
#   GAP_VERIFIED  - they do not have it, and the behavioural data agrees
#   GAP_DISPUTED  - the sheet says no, but their activity says yes -> quarantine
#   UNKNOWN       - the sheet has no opinion -> needs a human
OWNED = "OWNED"
GAP_VERIFIED = "GAP_VERIFIED"
GAP_DISPUTED = "GAP_DISPUTED"
UNKNOWN = "UNKNOWN"


def _apply_reconciliation(recon: Optional[dict]) -> Optional[dict]:
    """
    A human decision beats every inferred signal.

    When an RM has actually phoned the CSP and confirmed whether the device is
    physically there, that answer is the truth and the evidence rules stop
    applying - this is the escape hatch that turns a quarantined record back
    into an actionable one (or closes it for good).
    """
    if not recon:
        return None
    who = recon.get("by", "someone")
    when = str(recon.get("at", ""))[:10]
    note = recon.get("note") or ""
    suffix = f" - {note}" if note else ""
    if recon.get("ownsDevice"):
        return {
            "verdict": OWNED,
            "evidence": f"Confirmed by {who} on {when}: device is present{suffix}",
            "idle": False, "reconciled": True,
        }
    return {
        "verdict": GAP_VERIFIED,
        "evidence": f"Confirmed by {who} on {when}: no device{suffix}",
        "idle": False, "reconciled": True,
    }


def assess_matm(csp: dict, recon: Optional[dict] = None) -> dict:
    override = _apply_reconciliation(recon)
    if override:
        return override

    txns = csp["matmTxnPrev"] + csp["matmTxnThis"]
    transacts = txns >= config.MATM_TXN_PROVES_OWNERSHIP

    if csp["claimsMatm"] is True:
        return {
            "verdict": OWNED,
            "evidence": (f"Marked Yes; {txns:g} mATM transactions confirm it"
                         if transacts else
                         "Marked Yes, but 0 mATM transactions - device may be idle"),
            "idle": not transacts,
        }

    if csp["claimsMatm"] is False:
        if transacts:
            if config.MATM_TXNS_PROVE_OWNERSHIP:
                return {
                    "verdict": GAP_DISPUTED,
                    "evidence": (
                        f'Marked "No mATM" but recorded {txns:g} mATM transactions. '
                        "A CSP cannot transact on a device they do not have - either "
                        "the status is stale or the transaction metric counts AePS "
                        "activity. Withheld from outreach until reconciled."
                    ),
                    "idle": False,
                }
            return {
                "verdict": GAP_VERIFIED,
                "evidence": (
                    f'Marked "No"; {txns:g} mATM transactions recorded but not '
                    "treated as proof of ownership (config.MATM_TXNS_PROVE_OWNERSHIP "
                    "= False) - attributed to activity on a device that isn't the "
                    "CSP's own, same assumption as PRINTS_PROVE_OWNERSHIP."
                ),
                "idle": False,
                # This IS a latent-demand signal, same shape as printer's -
                # they're already generating mATM transactions without owning
                # the device, which is real evidence for the campaign email,
                # not just a caveat buried in the audit trail.
                "latentDemand": txns,
            }
        return {
            "verdict": GAP_VERIFIED,
            # Safe to act on because there is no contradicting evidence: 0
            # transactions is consistent with "No mATM". Not using
            # matmInactSaysNoDevice as corroboration here - it's a sheet
            # column that mechanically mirrors the Status column whenever
            # Status is "No", not an independent opinion; see config.py.
            "evidence": 'Marked "No"; 0 mATM transactions',
            "idle": False,
        }

    return {"verdict": UNKNOWN, "evidence": "mATM status column is blank", "idle": False}


def assess_printer(csp: dict, recon: Optional[dict] = None) -> dict:
    prints = csp["pbPrintsPrev"] + csp["pbPrintsThis"]

    override = _apply_reconciliation(recon)
    if override:
        return {**override, "latentDemand": prints}

    if csp["claimsPrinter"] is True:
        return {
            "verdict": OWNED,
            "evidence": (f"Marked Yes; {prints:g} passbook prints confirm use"
                         if prints > 0 else
                         "Marked Yes, but 0 prints - printer may be idle"),
            "idle": prints == 0,
        }

    if csp["claimsPrinter"] is False:
        # Prints recorded against a CSP with no printer are treated as demand
        # rather than as a contradiction, on the assumption that a print can be
        # attributed to a CSP who had it done elsewhere.
        #
        # That assumption is NOT established by the data - the sheet records
        # that a print happened, never where. See PRINTS_PROVE_OWNERSHIP.
        if prints > 0 and config.PRINTS_PROVE_OWNERSHIP:
            return {
                "verdict": GAP_DISPUTED,
                "evidence": (f'Marked "No" but recorded {prints:g} passbook prints, '
                             "which cannot happen without a printer. Withheld "
                             "until reconciled."),
                "idle": False, "latentDemand": prints,
            }
        return {
            "verdict": GAP_VERIFIED,
            "evidence": (f'Marked "No", yet {prints:g} passbook prints recorded '
                         "against them" if prints > 0 else 'Marked "No"'),
            "idle": False, "latentDemand": prints,
        }

    return {"verdict": UNKNOWN, "evidence": "Printer status column is blank",
            "idle": False, "latentDemand": prints}


# ------------------------------------------------------------- eligibility
def classify_activity(csp: dict) -> dict:
    terminal = (csp.get("terminalStatus") or "").strip().lower()
    if terminal not in config.TERMINAL_ACTIVE_VALUES:
        return {"status": "INACTIVE",
                "reason": f"Terminal is {csp.get('terminalStatus') or 'unknown'}"}

    days = csp.get("inactivityDays")
    if days is not None and days > config.MAX_INACTIVITY_DAYS:
        return {"status": "INACTIVE", "reason": f"{days} days inactive"}

    txns = csp["txnPrev"] + csp["txnThis"]
    if txns < config.MIN_TRANSACTIONS_TO_PITCH:
        # Active on paper, not trading in practice. Kept separate from INACTIVE
        # because these CSPs are a revival problem, not a dead account.
        return {"status": "DORMANT",
                "reason": f"Only {txns:g} transactions across the reported months"}

    return {"status": "ACTIVE",
            "reason": f"{txns:g} transactions across the reported months"}


def affordability_band(csp: dict) -> config.Band:
    commission = csp.get("commission")
    if commission is None:
        return config.UNKNOWN_BAND
    for band in config.BANDS:
        if commission >= band.minimum:
            return band
    return config.BANDS[-1]


def months_of_commission(csp: dict, product: config.Product) -> Optional[float]:
    """Months of the CSP's ENTIRE commission a device costs - the honesty check."""
    commission = csp.get("commission")
    if not commission or commission <= 0:
        return None
    return round(product.price / commission, 1)


# ----------------------------------------------------------------- scoring
def score_for(csp: dict, ownership: dict) -> int:
    w, c = config.SCORE_WEIGHTS, config.SCORE_CEILINGS
    clamp = lambda v, ceiling: max(0.0, min(v / ceiling, 1.0))

    return round(
        clamp(csp.get("commission") or 0, c["commission"]) * w["affordability"]
        + clamp(csp["txnPrev"] + csp["txnThis"], c["transactions"]) * w["volume"]
        + clamp(ownership.get("latentDemand") or 0, c["latent_units"]) * w["latent_demand"]
        + clamp(csp.get("loginDays") or 0, c["login_days"]) * w["engagement"]
    )


# ------------------------------------------------------------------ the run
def evaluate(csps: list, reconciliations: Optional[dict] = None) -> dict:
    """
    Evaluates the whole base and returns everything the app needs: per-CSP
    assessments, the outreach queues, coverage against the ACTIVE base, and the
    data-quality picture.
    """
    reconciliations = reconciliations or {}

    assessed = []
    for csp in csps:
        activity = classify_activity(csp)
        band = affordability_band(csp)
        code = csp["cspCode"]
        assessed.append({
            **csp,
            "activity": activity["status"],
            "activityReason": activity["reason"],
            "band": band.id,
            "bandLabel": band.label,
            "bandOffer": band.offer,
            "ownership": {
                "MATM": assess_matm(csp, reconciliations.get(f"{code}:MATM")),
                "PRINTER": assess_printer(csp, reconciliations.get(f"{code}:PRINTER")),
            },
            "transactions": csp["txnPrev"] + csp["txnThis"],
            "matmTxns": csp["matmTxnPrev"] + csp["matmTxnThis"],
            "passbookPrints": csp["pbPrintsPrev"] + csp["pbPrintsThis"],
        })

    active = [c for c in assessed if c["activity"] == "ACTIVE"]

    # --------------------------------------------------- coverage (the KPI)
    #
    # Measured against the ACTIVE base, which is the number the existing
    # spreadsheet dashboard cannot produce because it has no CSP denominator.
    def coverage_for(product_id: str) -> dict:
        product = config.PRODUCTS[product_id]
        owned = [c for c in active if c["ownership"][product_id]["verdict"] == OWNED]
        target_count = math.ceil(len(active) * product.coverage_target)
        return {
            "productId": product_id,
            "productName": product.name,
            "activeBase": len(active),
            "owned": len(owned),
            "coveragePct": round(len(owned) / len(active) * 100, 1) if active else 0,
            # Rendered as 50 rather than 50.0 - it is shown to people as a
            # percentage, and a trailing .0 in the UI reads like a bug.
            "targetPct": int(product.coverage_target * 100)
            if (product.coverage_target * 100).is_integer()
            else product.coverage_target * 100,
            "targetCount": target_count,
            "remaining": max(target_count - len(owned), 0),
            "idleDevices": sum(1 for c in owned if c["ownership"][product_id]["idle"]),
        }

    coverage = {pid: coverage_for(pid) for pid in config.PRODUCTS}

    # ------------------------------------------------------ outreach queues
    def build_queue(product_id: str) -> dict:
        product = config.PRODUCTS[product_id]
        blocked = config.BLOCKED_FOR.get(product_id, ())

        candidates = []
        for c in active:
            own = c["ownership"][product_id]
            if own["verdict"] != GAP_VERIFIED:
                continue
            if not c["contactable"]:
                continue
            if c["band"] in blocked:
                continue
            # A printer only makes sense on top of a working mATM.
            if product.requires_owned and \
                    c["ownership"][product.requires_owned]["verdict"] != OWNED:
                continue
            candidates.append({
                "cspCode": c["cspCode"], "name": c["name"], "rm": c["rm"],
                "state": c["state"], "district": c["district"],
                "mobile": c["mobile"], "email": c["email"],
                "band": c["band"], "bandOffer": c["bandOffer"],
                "commission": c["commission"],
                "transactions": c["transactions"],
                "latentDemand": own.get("latentDemand") or 0,
                "monthsOfCommission": months_of_commission(c, product),
                "score": score_for(c, own),
                "evidence": own["evidence"],
            })

        candidates.sort(key=lambda t: t["score"], reverse=True)

        # The capped target (printer at 50%) means picking WHICH CSPs, not all
        # of them. Everyone past the cap is kept, ranked, as the next cycle's
        # list - deleting them would throw away the ranking work.
        room = coverage[product_id]["remaining"]
        return {
            "productId": product_id,
            "productName": product.name,
            "needed": room,
            "targeted": candidates[:room],
            "deferred": candidates[room:],
            "candidatePool": len(candidates),
        }

    queues = {pid: build_queue(pid) for pid in config.PRODUCTS}

    # --------------------------------------------------------- data quality
    disputed = [c for c in assessed
                if c["ownership"]["MATM"]["verdict"] == GAP_DISPUTED
                or c["ownership"]["PRINTER"]["verdict"] == GAP_DISPUTED]
    unknown = [c for c in assessed
               if c["ownership"]["MATM"]["verdict"] == UNKNOWN
               or c["ownership"]["PRINTER"]["verdict"] == UNKNOWN]

    data_quality = {
        "total": len(assessed),
        "disputed": len(disputed),
        "unknown": len(unknown),
        "uncontactable": sum(1 for c in assessed if not c["contactable"]),
        "idleMatm": coverage["MATM"]["idleDevices"],
        "idlePrinter": coverage["PRINTER"]["idleDevices"],
        # The headline: how much of the apparent mATM opportunity is unproven.
        "matmFlaggedGap": sum(1 for c in active if c["claimsMatm"] is False),
        "matmVerifiedGap": sum(1 for c in active
                               if c["ownership"]["MATM"]["verdict"] == GAP_VERIFIED),
        "matmDisputedGap": sum(1 for c in active
                               if c["ownership"]["MATM"]["verdict"] == GAP_DISPUTED),
        "disputedSample": [{
            "cspCode": c["cspCode"], "name": c["name"], "rm": c["rm"],
            "commission": c["commission"], "matmTxns": c["matmTxns"],
            "evidence": c["ownership"]["MATM"]["evidence"],
        } for c in disputed[:25]],
    }

    # -------------------------------------------------------------- rollups
    by_status: dict = {}
    for c in assessed:
        by_status[c["activity"]] = by_status.get(c["activity"], 0) + 1

    by_band: dict = {}
    for c in active:
        by_band[c["band"]] = by_band.get(c["band"], 0) + 1

    rm_workload: dict = {}
    for q in queues.values():
        for t in q["targeted"]:
            row = rm_workload.setdefault(
                t["rm"], {"rm": t["rm"], "MATM": 0, "PRINTER": 0, "total": 0})
            row[q["productId"]] += 1
            row["total"] += 1

    revenue = sum(len(queues[pid]["targeted"]) * config.PRODUCTS[pid].price
                  for pid in config.PRODUCTS)

    return {
        "assessed": assessed,
        "coverage": coverage,
        "queues": queues,
        "dataQuality": data_quality,
        "summary": {
            "totalCsps": len(assessed),
            "byStatus": by_status,
            "byBand": by_band,
            "rmWorkload": sorted(rm_workload.values(),
                                 key=lambda r: r["total"], reverse=True),
            "revenueAtStake": revenue,
        },
    }
