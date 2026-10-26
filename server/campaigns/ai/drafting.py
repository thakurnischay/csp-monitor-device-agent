"""The AI drafting assistant: turns a campaign's settings (and an optional brief) into a proposed subject and message.

Privacy by construction: the model receives only non-personal context chunks and the brief AFTER it was scrubbed; a final check on
the assembled prompt refuses to send anything that still looks personal. The model's reply is validated (placeholders, length,
no personal data) and screened for review flags. Every attempt - success, refusal, error - is stored as one telemetry record.

Uses the official Anthropic Python SDK. Default model claude-opus-5-5 (override with CAMPAIGN_AI_MODEL), with the server-side
refusal fallback enabled. The key comes only from ANTHROPIC_API_KEY in the server environment.
"""
import json
import logging
import os
import re
import time

from .. import operators as ops, rendering
from . import context, knowledge, redaction, telemetry

log = logging.getLogger("csp_monitor.ai_drafting")
DEFAULT_MODEL = "claude-opus-5-5"
FALLBACK_BETA = "server-side-fallback-2026-07-01"
MAX_TOKENS = 8000                    # thinking is always on for this model; leave room so the JSON answer is never cut off
EFFORT = "low"                       # a short email does not need deep reasoning
BRIEF_MAX = 1000
PRICES_PER_MTOK = {"claude-opus-5-5": (4.0, 20.0), "claude-sonnet-5-5": (2.0, 10.0), "claude-haiku-5-5": (0.10, 0.50)}   # estimate only


class AiUnavailable(Exception):
    """The assistant is not configured (no key / switched off)."""


class DraftError(Exception):
    """The attempt failed; the message is safe to show. The failed attempt is still recorded."""
    def __init__(self, message, generation_id=None):
        super().__init__(message)
        self.generation_id = generation_id


def model_name():
    return (os.environ.get("CAMPAIGN_AI_MODEL") or DEFAULT_MODEL).strip()


def enabled():
    return bool((os.environ.get("ANTHROPIC_API_KEY") or "").strip()) and (os.environ.get("CAMPAIGN_AI_ENABLED") or "1").strip() != "0"


def settings_summary():
    """Safe for a web page: never contains the key."""
    return {"enabled": enabled(), "key_set": bool((os.environ.get("ANTHROPIC_API_KEY") or "").strip()), "model": model_name(),
            "prompt_version": knowledge.PROMPT_VERSION, "context_budget_tokens": context.budget(), "effort": EFFORT}


def get_client():
    """Overridden in tests. Imported lazily so the dashboard runs without the SDK until the assistant is used."""
    import anthropic
    return anthropic.Anthropic(timeout=60.0, max_retries=2)


# ------------------------------------------------------------------------------------------- review flags
# Heuristic screen for wording a person should double-check. It is NOT the model's confidence (the API gives none) and it never
# blocks a draft: it highlights.
_FLAG_RULES = [
    ("high", "amount", re.compile(r"(?:₹|\brs\.?\s?|\binr\s?)\s?\d|\b\d[\d,]*\s?(?:rupees|lakh|crore)\b", re.I), "states an amount of money"),
    ("high", "security", re.compile(r"\b(?:otp|pin|password|cvv|account number)\b", re.I), "mentions a credential or account detail"),
    ("high", "promise", re.compile(r"\b(?:guarantee[d]?|assured|risk[- ]free|certain(?:ly)? (?:to )?(?:earn|profit))\b", re.I), "promises a result"),
    ("medium", "earnings", re.compile(r"\b(?:earn|earning|income|profit|profits|returns?|savings?|commission)\b", re.I), "talks about earnings"),
    ("medium", "offer", re.compile(r"\b(?:free|discount|offer|cashback|bonus|coupon)\b", re.I), "suggests an offer or discount"),
    ("medium", "pressure", re.compile(r"\b(?:hurry|last chance|limited time|only today|don't miss|act now|urgent(?:ly)?|before it'?s too late)\b", re.I), "creates urgency"),
    ("medium", "bank", re.compile(r"\b(?:sbi|hdfc|icici|axis|pnb|bank of baroda|state bank)\b", re.I), "names a bank"),
    ("low", "superlative", re.compile(r"\b(?:best|number one|#1|100%|unlimited|never|always)\b", re.I), "uses an absolute claim"),
]


def _sentence_around(text, start, end):
    left = max(text.rfind(c, 0, start) for c in ".!?\n") + 1
    cuts = [i for i in (text.find(c, end) for c in ".!?\n") if i != -1]
    return text[left:(min(cuts) + 1) if cuts else len(text)].strip()[:240]


def review_flags(subject, body):
    """Match on the whole text (so 'Rs. 8,500' is not cut in two), then show the sentence around each match."""
    text = f"{subject}\n{body}"
    flags, seen = [], set()
    for severity, kind, rx, why in _FLAG_RULES:
        for m in rx.finditer(text):
            key = (kind, m.group(0).lower())
            if key not in seen:
                seen.add(key)
                flags.append({"severity": severity, "kind": kind, "phrase": m.group(0).strip()[:60],
                              "sentence": _sentence_around(text, m.start(), m.end()), "why": why})
    return flags


def _friendly(err):
    name = type(err).__name__
    return {"AuthenticationError": "The AI service rejected the server's key.", "PermissionDeniedError": "The AI key is not allowed to use this model.",
            "RateLimitError": "The AI service is busy. Please try again in a minute.", "APITimeoutError": "The AI service took too long to answer.",
            "APIConnectionError": "The server could not reach the AI service.", "NotFoundError": "The AI model name is not available.",
            "BadRequestError": "The AI service refused the request."}.get(name, "The AI service returned an error (%s)." % name)


def _est_cost(model, in_tokens, out_tokens):
    price = PRICES_PER_MTOK.get(model)
    if not price or in_tokens is None or out_tokens is None:
        return None
    return round((in_tokens * price[0] + out_tokens * price[1]) / 1_000_000, 6)


def _used_fallback(resp):
    if any(getattr(b, "type", "") == "fallback" for b in getattr(resp, "content", []) or []):
        return True
    return any(getattr(i, "type", "") == "fallback_message" for i in (getattr(getattr(resp, "usage", None), "iterations", None) or []))


# ---------------------------------------------------------------------------------------------------- planning
def prepare(conn, fields, brief="", excluded_ids=()):
    """Everything the model WOULD see, without calling it: scrubbed brief, chosen chunks, token budget. Free; used for the context panel."""
    known = redaction.load_known(conn)
    brief_clean, report = redaction.redact(str(brief or "")[:BRIEF_MAX], known)
    plan = context.plan(conn, fields["occasion"], fields["devices"], brief_clean, excluded_ids, known)
    return {"brief_clean": brief_clean, "redaction": report, "plan": plan, "known": known}


def _public_chunks(plan, cited=()):
    cited = set(cited)
    return [{"id": c["id"], "kind": c["kind"], "title": c["title"], "source": c["source"], "locked": c["locked"], "included": c["included"],
             "reason": c["reason"], "score": c["score"], "tokens": c["tokens"], "used": c["id"] in cited, "text": c["text"]} for c in plan["chunks"]]


# -------------------------------------------------------------------------------------------------- generation
def generate(conn, fields, brief, excluded_ids, actor, client=None, replaces=None):
    """One model call. `fields`: {occasion, devices}. Always records a telemetry row; raises AiUnavailable / DraftError."""
    if client is None:
        if not enabled():
            raise AiUnavailable("AI drafting is not set up on this server (an administrator must set ANTHROPIC_API_KEY).")
        client = get_client()
    prep = prepare(conn, fields, brief, excluded_ids)
    plan, known = prep["plan"], prep["known"]
    user_prompt = context.render_user_prompt(plan, fields["occasion"], fields["devices"], prep["brief_clean"])
    model = model_name()
    base = {"created_by": actor, "model": model, "prompt_version": knowledge.PROMPT_VERSION, "effort": EFFORT, "brief_redacted": prep["brief_clean"],
            "redaction": prep["redaction"], "system_prompt": knowledge.SYSTEM_PROMPT, "user_prompt": user_prompt,
            "context": _public_chunks(plan), "context_tokens_est": plan["tokens_used"], "token_budget": plan["budget"]}

    def fail(status, message, **extra):
        gid = telemetry.record_generation(conn, dict(base, status=status, error=message, **extra))
        conn.commit()
        raise DraftError(message, gid)

    leaked = redaction.leaks(knowledge.SYSTEM_PROMPT + "\n" + user_prompt, known)
    if leaked:                                                  # defence in depth: nothing personal may leave
        fail("error", "Blocked: the prompt still contained personal data (%s), so nothing was sent to the AI service." % ", ".join(leaked))

    started = time.time()
    try:
        resp = client.beta.messages.create(
            model=model, max_tokens=MAX_TOKENS, system=knowledge.SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_prompt}],
            output_config={"effort": EFFORT, "format": {"type": "json_schema", "schema": knowledge.OUTPUT_SCHEMA}},
            betas=[FALLBACK_BETA], fallbacks="default")
    except Exception as err:                                    # noqa: BLE001 - classified for a friendly message
        log.warning("AI drafting call failed: %s", type(err).__name__)
        fail("error", _friendly(err), latency_ms=int((time.time() - started) * 1000))
    latency = int((time.time() - started) * 1000)
    usage = getattr(resp, "usage", None)
    in_t, out_t = getattr(usage, "input_tokens", None), getattr(usage, "output_tokens", None)
    meta = {"latency_ms": latency, "input_tokens": in_t, "output_tokens": out_t, "cache_read_tokens": getattr(usage, "cache_read_input_tokens", None),
            "request_id": getattr(resp, "_request_id", None), "stop_reason": getattr(resp, "stop_reason", None),
            "fallback_used": _used_fallback(resp), "est_cost_usd": _est_cost(getattr(resp, "model", model), in_t, out_t)}
    if meta["stop_reason"] == "refusal":
        fail("refused", "The AI service declined to write this draft. Please adjust the brief or write it yourself.", **meta)
    text = next((getattr(b, "text", "") for b in getattr(resp, "content", []) or [] if getattr(b, "type", "") == "text"), "")
    try:
        data = json.loads(text)
        subject, body = rendering.validate_template(data.get("subject"), data.get("body"))
        cited = [i for i in (data.get("used_context_ids") or []) if isinstance(i, str)]
    except (ValueError, AttributeError, TypeError) as err:
        fail("invalid_output", "The AI returned a draft that could not be used (%s). Please try again." % str(err)[:120], output_body=text[:4000], **meta)
    reply_leaks = redaction.leaks(subject + "\n" + body, known)
    if reply_leaks:
        fail("invalid_output", "The AI draft contained personal data (%s) and was discarded." % ", ".join(reply_leaks), **meta)
    included = {c["id"] for c in plan["chunks"] if c["included"]}
    cited = [i for i in dict.fromkeys(cited) if i in included]
    flags = review_flags(subject, body)
    gid = telemetry.record_generation(conn, dict(base, status="ok", output_subject=subject, output_body=body, cited_context=cited,
                                                 review_flags=flags, context=_public_chunks(plan, cited), **meta))
    conn.commit()
    if replaces:
        telemetry.log_feedback(replaces, "regenerated", actor, detail=f"replaced by generation #{gid}")
    return {"generation_id": gid, "subject": subject, "body": body, "flags": flags, "cited": cited, "chunks": _public_chunks(plan, cited),
            "tokens_used_est": plan["tokens_used"], "budget": plan["budget"], "input_tokens": in_t, "output_tokens": out_t, "model": model,
            "served_by": getattr(resp, "model", model), "fallback_used": meta["fallback_used"], "prompt_version": knowledge.PROMPT_VERSION,
            "redaction": prep["redaction"], "latency_ms": latency}
