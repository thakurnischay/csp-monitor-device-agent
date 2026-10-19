"""Which CSP business workflows depend on which device - and how SURE we are.

The agent can read what Windows, drivers, services and vendor endpoints say. It
cannot read how the CSP's banking application uses each device, and it has no
transaction or revenue data. So this map is a statement of what is *assumed*,
every entry says whether operations has confirmed it, and nothing here ever
turns into an invented revenue number.

Operations can correct it without touching code: put an `impact_map.json` next
to the agent (same shape as DEFAULT_IMPACT) and it overrides these defaults.
Set "confirmed": true on an entry once ops has verified it - only then may a
failure of that device be rated CRITICAL on its own (see diagnostics.py).
"""
import json
import os

DEFAULT_IMPACT = {
    "biometric": {
        "label": "Fingerprint scanner",
        "workflows": [
            {"name": "Aadhaar-based transactions (AePS): cash withdrawal, balance enquiry, mini-statement",
             "needs": "required", "confirmed": False},
            {"name": "Aadhaar eKYC / biometric customer authentication",
             "needs": "required", "confirmed": False},
        ],
        "workaround": "None known for biometric authentication; card-based services on the micro-ATM are not affected.",
    },
    "microatm": {
        "label": "Micro-ATM",
        "workflows": [
            {"name": "Card-based cash withdrawal and balance enquiry on the micro-ATM",
             "needs": "required", "confirmed": False},
        ],
        "workaround": "None known for card-based withdrawals and balance enquiries "
                      "(Aadhaar-based services do not use the micro-ATM and may still work).",
    },
    "printer": {
        "label": "Passbook printer",
        "workflows": [
            {"name": "Passbook printing / passbook update", "needs": "required", "confirmed": False},
        ],
        "workaround": "Transactions themselves are not stopped; customers can be given a receipt or "
                      "asked to return to update the passbook.",
    },
    "gps": {
        "label": "GPS dongle",
        "workflows": [
            {"name": "Location capture for the CSP's transactions", "needs": "unknown", "confirmed": False},
        ],
        "workaround": "Not established - the agent has not been told how the CSP application uses the dongle.",
    },
}

REVENUE_NOTE = ("Not calculable: no transaction or revenue data is connected to the agent. "
                "No figure is estimated.")


def load():
    """Defaults, overridden per device by impact_map.json next to the agent."""
    impact = json.loads(json.dumps(DEFAULT_IMPACT))
    path = os.environ.get("CSP_AGENT_IMPACT_MAP") or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "impact_map.json")
    try:
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as f:
                custom = json.load(f) or {}
            for dev, entry in custom.items():
                if dev in impact and isinstance(entry, dict):
                    impact[dev].update(entry)
    except Exception:
        pass            # a broken override file never breaks monitoring
    return impact


def describe(dev: str, impact: dict = None) -> dict:
    """What the dashboard shows as the business impact of a failure of `dev`."""
    entry = (impact or load()).get(dev) or {}
    workflows = entry.get("workflows") or []
    known = [w for w in workflows if w.get("needs") in ("required", "optional")]
    return {
        "workflows": [{"name": w.get("name", ""), "needs": w.get("needs", "unknown"),
                       "confirmed": bool(w.get("confirmed"))} for w in workflows],
        "assessment": ("Likely affected while this device is down (assumed, not yet confirmed by operations)."
                       if known and not all(w.get("confirmed") for w in known) else
                       "Affected while this device is down (confirmed by operations)." if known else
                       "Impact not established: how the CSP application uses this device is not known to the agent."),
        "workaround": entry.get("workaround", ""),
        "revenue": REVENUE_NOTE,
        "confirmed": bool(known) and all(w.get("confirmed") for w in known),
        "required": any(w.get("needs") == "required" for w in workflows),
    }
