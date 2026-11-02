"""
Personalised, pre-filled links into the one Google Form used for every device
campaign, and the campaign tokens that make a submission traceable back to a
specific (CSP, product) offer actually made.

Fields not yet added to the live form (see GOOGLE_FORM_SETUP.md) are simply
left out of the generated link rather than raising - a partially-wired form
still produces a usable link with the fields that DO exist, and
missing_fields() tells the caller what is not wired up yet. Nothing here
should block testing while that checklist is being worked through.
"""

from typing import Optional
from urllib.parse import urlencode

from . import config, store


# ----------------------------------------------------------------- helpers
def missing_fields() -> list:
    """Entries in config.FORM_ENTRY with no real entry.NNNNNNN id yet."""
    return [k for k, v in config.FORM_ENTRY.items() if v is None]


def _local_mobile(mobile: Optional[str]) -> Optional[str]:
    """csps.json stores mobiles as 91XXXXXXXXXX (see import_calling_sheet.py:
    normalise_mobile); the form's Mobile field validates ^[0-9]{10}$, so the
    country code has to come back off before it goes in the link."""
    if not mobile:
        return None
    digits = "".join(ch for ch in str(mobile) if ch.isdigit())
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    return digits or None


# The form's State field is a dropdown with a fixed option list; Google Forms
# silently leaves it unselected if the prefilled value doesn't match an option
# byte-for-byte. The Calling Sheet has the same state under several castings
# ("BIHAR", "Bihar", "Nct Of Delhi") - normalise here rather than in the
# importer, since this casing requirement is specific to this form's dropdown,
# not a fact about the underlying data.
_STATE_OPTIONS = ("Bihar", "Jharkhand", "Uttar Pradesh", "Haryana", "Punjab",
                   "Delhi", "Himachal Pradesh", "Rajasthan", "Maharashtra",
                   "Madhya Pradesh", "Uttarakhand", "J & K", "Chandigarh",
                   "West Bengal")
_STATE_ALIASES = {
    "nct of delhi": "Delhi",
    "jammu and kashmir": "J & K",
    "jammu & kashmir": "J & K",
}


def _normalise_state(raw: Optional[str]) -> Optional[str]:
    s = (raw or "").strip()
    if not s:
        return None
    lower = s.lower()
    if lower in _STATE_ALIASES:
        return _STATE_ALIASES[lower]
    for option in _STATE_OPTIONS:
        if option.lower() == lower:
            return option
    return None   # unrecognised - left blank so the CSP picks "Other" themselves


# ------------------------------------------------------------------- links
def build_prefill_url(csp: dict, product_id: str, token: str) -> str:
    """Pure - builds a URL from a CSP record, a product, and an already-issued
    token. Does not touch state or issue anything; see issue_and_build for the
    one call site that should actually be used when handing a link to a CSP."""
    campaign_label = config.FORM_CAMPAIGN_PRODUCT_LABEL.get(product_id)
    if campaign_label is None:
        raise ValueError(f"Unknown product: {product_id}")
    product = config.PRODUCTS.get(product_id)

    values = {
        "cspCode": csp.get("cspCode"),
        "cspName": csp.get("name"),
        "mobile": _local_mobile(csp.get("mobile")),
        "state": _normalise_state(csp.get("state")),
        "district": csp.get("district"),
        "email": csp.get("email"),
        "campaignProduct": campaign_label,
        "token": token,
        # The exact amount for THIS campaign/token, so a CSP never has to type
        # (or mistype) a number that orders.py's amount-match check would
        # otherwise reject - see _try_finance_verify's amount comparison.
        "amountPaid": product.price if product else None,
    }

    params = {"usp": "pp_url"}
    for key, value in values.items():
        entry_id = config.FORM_ENTRY.get(key)
        if entry_id is None or not value:
            continue    # field not on the form yet, or nothing to prefill it with
        params[entry_id] = value

    return f"{config.FORM_BASE_URL}?{urlencode(params)}"


def issue_and_build(state: dict, csp: dict, product_id: str) -> dict:
    """Issues a fresh token for this (csp, product) and returns it together
    with the link built from it. This is the call site outreach.py and any
    manual link-generation path should use - a link should never be handed to
    a CSP without a matching token on file to validate their submission
    against later."""
    product = config.PRODUCTS[product_id]
    token = store.issue_token(state, csp["cspCode"], product_id, product.price)
    return {"token": token, "url": build_prefill_url(csp, product_id, token)}
