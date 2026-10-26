"""Programmatic evals for the drafting assistant: a fixed golden set of cases (golden.json) and graders that check a draft against
rules that can be tested by code - no model judges another model. Offline mode uses a deterministic stand-in so the harness and
graders are testable for free; live mode (tools/run_ai_eval.py --live) calls the real model and costs money.

Results are stored in ai_eval_runs so the AI quality page can show regressions after any prompt or model change.
"""
import json
import os
from datetime import datetime, timezone

from .. import rendering
from . import drafting, knowledge, redaction

GOLDEN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "golden.json")
BODY_MIN, BODY_MAX_CHARS = 60, 1800


def load_cases():
    with open(GOLDEN, encoding="utf-8") as fh:
        return json.load(fh)["cases"]


def grade(case, subject, body):
    """-> {check_name: (passed, why)} for one draft."""
    checks = {}
    try:
        rendering.validate_template(subject, body)
        checks["valid_template"] = (True, "")
    except ValueError as err:
        checks["valid_template"] = (False, str(err))
    leaked = redaction.leaks(subject + "\n" + body, redaction.Known())
    checks["no_personal_data"] = (not leaked, ", ".join(leaked))
    high = [f["phrase"] for f in drafting.review_flags(subject, body) if f["severity"] == "high"]
    checks["no_high_risk_wording"] = (not high, ", ".join(high))
    checks["length_ok"] = (BODY_MIN <= len(body) <= BODY_MAX_CHARS, f"{len(body)} characters")
    text = (subject + " " + body).lower()
    checks["mentions_occasion"] = ("{occasion}" in text or case["occasion"].lower() in text, "")
    checks["names_missing_devices"] = ("{missing_devices}" in text, "")
    checks["greets_by_name"] = ("{name}" in text or "{first_name}" in text, "")
    return checks


def offline_draft(case):
    """Deterministic stand-in, so the harness runs without a model."""
    return ("Warm wishes for {occasion}, {first_name}",
            "Dear {name},\n\nWishing you and your family a joyful {occasion}. We noticed you do not yet have {missing_devices} at your "
            "centre. If you would like to know more, please speak to {rm_name}.\n\nWith regards,\nTeam Eko")


def run(conn, mode="offline", draft_fn=None, actor="eval"):
    """Run every golden case, store one ai_eval_runs row, return the summary. `draft_fn(case)->(subject, body)` overrides the draft source."""
    cases = load_cases()
    draft_fn = draft_fn or offline_draft
    details, passed = [], 0
    for case in cases:
        try:
            subject, body = draft_fn(case)
            checks = grade(case, subject, body)
        except Exception as err:                                    # noqa: BLE001 - a failed draft is a failed case
            checks = {"produced_a_draft": (False, str(err)[:200])}
        ok = all(p for p, _ in checks.values())
        passed += ok
        details.append({"id": case["id"], "passed": ok, "failed": {k: why for k, (p, why) in checks.items() if not p}})
    conn.execute("INSERT INTO ai_eval_runs (started_at, prompt_version, model, mode, cases_total, cases_passed, details_json) VALUES (?,?,?,?,?,?,?)",
                 (datetime.now(timezone.utc).isoformat(timespec="seconds"), knowledge.PROMPT_VERSION,
                  drafting.model_name() if mode == "live" else "offline-stand-in", mode, len(cases), passed, json.dumps(details)))
    conn.commit()
    return {"total": len(cases), "passed": passed, "failed": [d for d in details if not d["passed"]]}
