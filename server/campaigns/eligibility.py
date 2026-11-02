"""Who receives a festival / occasion campaign, and WHY each operator is in or out.

An operator is eligible when, in order:
  1. they are in the latest operator data                       (identity is the CSP code)
  2. they are an active operator                                (the email project's ACTIVE rule; dormant is opt-in)
  3. they have a valid email address
  4. that address has not opted out or bounced                  (suppression list)
  5. they CONFIRMEDLY lack the selected device(s)               (verdict GAP_VERIFIED - see operators.py)
  6. no other operator in the same campaign already uses that address

Three buckets, each with a plain-words reason and the list of checks that produced it:
  eligible   will receive the email (if the campaign is launched)
  review     ownership of a selected device is unknown or in conflict: NEVER treated as "does not own"; a person decides
  excluded   not eligible (reason says why)

`audience_match`:  "any"  lacks AT LEAST ONE selected device (default)   |   "all"  lacks EVERY selected device.
An operator who lacks one device is never assumed to lack the others: the email names only devices confirmed absent.
This audience is built from its own tables and never reads or writes the device-adoption sales campaign's state.
"""
from . import operators as ops
from .growth import emailer
from .growth.engine import GAP_DISPUTED, GAP_VERIFIED, OWNED, UNKNOWN

ELIGIBLE, REVIEW, EXCLUDED = "eligible", "review", "excluded"
MATCH_MODES = ("any", "all")


class ConfigError(ValueError):
    """The campaign's audience settings are not usable."""


def clean_config(devices, audience_match="any", include_dormant=False):
    devices = [str(d).upper() for d in (devices or [])]
    devices = [d for i, d in enumerate(devices) if d in ops.DEVICES and d not in devices[:i]]
    if not devices:
        raise ConfigError("Choose at least one device category for the audience.")
    if audience_match not in MATCH_MODES:
        raise ConfigError("Audience match must be 'any' or 'all'.")
    return {"devices": devices, "audience_match": audience_match, "include_dormant": bool(include_dormant)}


def _labels(devices):
    return ", ".join(ops.SENTENCE_LABELS[d] for d in devices)


def evaluate(op, devices, cfg, suppressed, claimed_emails):
    """One operator -> {bucket, reason, missing, review, owned, checks}.
    `op` is a campaign_operators row; `devices` {device: {verdict, evidence}}; `suppressed` a set of lower-case addresses;
    `claimed_emails` {address: csp_code} for eligible operators already placed in this audience (mutated on success)."""
    checks = []

    def result(bucket, reason, missing=(), review=(), owned=()):
        return {"bucket": bucket, "reason": reason, "missing": list(missing), "review": list(review),
                "owned": list(owned), "checks": checks}

    def check(name, passed, note):
        checks.append({"check": name, "passed": bool(passed), "note": note})
        return passed

    if not check("In the latest operator data", op["present_in_latest"],
                 "yes" if op["present_in_latest"] else "not in the newest import"):
        return result(EXCLUDED, "No longer in the latest operator data (kept for history, never emailed).")

    allowed = ("ACTIVE", "DORMANT") if cfg["include_dormant"] else ("ACTIVE",)
    if not check("Active operator", op["activity"] in allowed, f"{op['activity']}: {op['activity_reason'] or ''}".strip()):
        return result(EXCLUDED, f"Not an active operator ({(op['activity'] or 'unknown').lower()}: {op['activity_reason'] or 'no detail'}).")

    address = (op["email"] or "").strip()
    if not check("Valid email address", emailer.is_valid_email(address), "present and well-formed" if address else "none on record"):
        return result(EXCLUDED, "No valid email address on record." if not address else "The email address on record is not valid.")
    if not check("Not opted out or bounced", address.lower() not in suppressed, "not on the suppression list"
                 if address.lower() not in suppressed else "on the suppression list"):
        return result(EXCLUDED, "This address has opted out or bounced earlier: it is never emailed again.")

    missing, review, owned = [], [], []
    for d in cfg["devices"]:
        v = (devices.get(d) or {}).get("verdict", UNKNOWN)
        (missing if v == GAP_VERIFIED else owned if v == OWNED else review).append(d)
    checks.append({"check": "Device ownership", "passed": None, "note": "; ".join(
        f"{ops.DEVICE_LABELS[d]}: {(devices.get(d) or {}).get('verdict', UNKNOWN)}" for d in cfg["devices"])})

    if cfg["audience_match"] == "all":
        qualifies = len(missing) == len(cfg["devices"])
        if not qualifies and owned:
            return result(EXCLUDED, f"Owns {_labels(owned)} (this audience needs operators who lack every selected device).",
                          missing, review, owned)
    else:
        qualifies = bool(missing)
        if not qualifies and not review:
            return result(EXCLUDED, f"Already owns {_labels(owned)}.", missing, review, owned)
    if not qualifies:
        notes = "; ".join(f"{ops.DEVICE_LABELS[d]} - {(devices.get(d) or {}).get('evidence') or 'no record'}" for d in review)
        return result(REVIEW, f"Ownership not confirmed for {_labels(review)}: {notes}", missing, review, owned)

    if not check("Address not already used in this campaign", address.lower() not in claimed_emails,
                 "unique" if address.lower() not in claimed_emails else f"already used for {claimed_emails.get(address.lower())}"):
        return result(EXCLUDED, f"The same address is already used for operator {claimed_emails[address.lower()]} in this campaign.",
                      missing, review, owned)
    claimed_emails[address.lower()] = op["csp_code"]
    reason = f"Confirmed without {_labels(missing)}."
    if review:
        reason += f" (Not mentioned in the email: {_labels(review)} - ownership unknown or in conflict.)"
    return result(ELIGIBLE, reason, missing, review, owned)


def build_audience(conn, cfg):
    """Evaluate every operator. -> {"rows": [...decisions with the operator's fields...], "counts": {...}}"""
    suppressed = {r["email"] for r in conn.execute("SELECT email FROM email_suppressions").fetchall()}
    dev = ops.device_map(conn)
    claimed, rows = {}, []
    counts = {ELIGIBLE: 0, REVIEW: 0, EXCLUDED: 0}
    for op in conn.execute("SELECT * FROM campaign_operators ORDER BY csp_code").fetchall():
        d = evaluate(op, dev.get(op["csp_code"], {}), cfg, suppressed, claimed)
        counts[d["bucket"]] += 1
        rows.append(dict(d, csp_code=op["csp_code"], name=op["name"], email=op["email"], rm=op["rm"], state=op["state"],
                         district=op["district"], devices=dev.get(op["csp_code"], {})))
    return {"rows": rows, "counts": counts, "total": len(rows)}
