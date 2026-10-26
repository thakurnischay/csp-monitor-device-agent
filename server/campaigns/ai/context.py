"""The context orchestrator: decides exactly what the model gets to see, and records why.

Instead of pasting everything into the prompt, candidate chunks are scored for relevance to THIS campaign and brief, and only the
ones that fit a token budget go in. Mandatory (locked) chunks always go in; a person can switch the others off. Every chunk -
included or not - is written to the telemetry record with its score and the reason, so any draft can later be explained.

Token counts here are ESTIMATES (about four characters per token); the real count the API reports is stored next to them.
"""
import json
import math
import os
import re

from . import knowledge, redaction

DEFAULT_BUDGET_TOKENS = 1500
_STOP = set("a an the and or of to in on for with is are be this that it as at by from your you we our their they them his her its "
            "will can may not no do does did have has had been being into over under more most very just also than then so if but".split())
_WORD = re.compile(r"[a-z][a-z'-]{2,}")


def budget():
    try:
        return max(300, min(int(os.environ.get("CAMPAIGN_AI_CONTEXT_BUDGET") or DEFAULT_BUDGET_TOKENS), 20000))
    except ValueError:
        return DEFAULT_BUDGET_TOKENS


def estimate_tokens(text):
    return max(1, len(str(text or "")) // 4)


def _terms(text):
    return [w for w in _WORD.findall(str(text or "").lower()) if w not in _STOP]


def _chunk(cid, kind, title, text, source, locked=False, meta=None):
    return {"id": cid, "kind": kind, "title": title, "text": text, "source": source, "locked": locked,
            "tokens": estimate_tokens(text), "score": 0.0, "included": False, "reason": "", "meta": meta or {}}


def candidate_chunks(conn, occasion, devices, known=None):
    """Every chunk that COULD go in. Example emails come from earlier launched campaigns, scrubbed of personal data first."""
    chunks = [dict(_chunk(c["id"], c["kind"], c["title"], c["text"], c["source"], c["locked"])) for c in knowledge.FIXED_CHUNKS]
    chunks.append(_chunk("occasion.name", "occasion", "The occasion", f"The occasion for this email is: {occasion}.", "campaign settings", locked=True))
    for d in devices:
        if d in knowledge.DEVICE_FACTS:
            label, text = knowledge.DEVICE_FACTS[d]
            chunks.append(_chunk(f"device.{d}", "device", label, text, "knowledge/devices@" + knowledge.PROMPT_VERSION, locked=True))
    chunks += [_chunk(c["id"], c["kind"], c["title"], c["text"], c["source"]) for c in knowledge.STYLE_CHUNKS]
    known = known or redaction.Known()
    rows = conn.execute("SELECT id, name, occasion, devices_json, subject, body, launched_at FROM email_campaigns "
                        "WHERE status IN ('queued','sending','paused','completed') ORDER BY id DESC LIMIT 40").fetchall()
    for r in rows:
        subject, _ = redaction.redact(r["subject"], known)
        body, _ = redaction.redact(r["body"], known)
        try:
            devs = json.loads(r["devices_json"] or "[]")
        except ValueError:
            devs = []
        chunks.append(_chunk(f"example.campaign{r['id']}", "example", f"Earlier campaign: {r['occasion']}",
                             f"Subject: {subject}\n{body}", f"campaign #{r['id']} (launched {str(r['launched_at'] or '')[:10]})",
                             meta={"occasion": r["occasion"], "devices": devs}))
    return chunks


def select(chunks, query_text, occasion, devices, excluded_ids=(), token_budget=None):
    """Score, filter and budget the chunks. Returns {"chunks": all (each with included/reason/score), "tokens_used", "budget"}."""
    token_budget = token_budget or budget()
    excluded_ids = set(excluded_ids or [])
    corpus = [set(_terms(c["title"] + " " + c["text"])) for c in chunks]
    n = len(chunks) or 1
    df = {}
    for terms in corpus:
        for t in terms:
            df[t] = df.get(t, 0) + 1
    idf = {t: math.log((n + 1) / (v + 0.5)) for t, v in df.items()}
    q = set(_terms(query_text))
    for c, terms in zip(chunks, corpus):
        overlap = sum(idf[t] for t in terms & q)
        c["score"] = round(overlap / (math.sqrt(len(terms)) + 1), 4)
        meta = c.get("meta") or {}
        if meta.get("occasion") and str(meta["occasion"]).strip().lower() == str(occasion).strip().lower():
            c["score"] = round(c["score"] + 1.0, 4)
        if meta.get("devices") and set(meta["devices"]) == set(devices):
            c["score"] = round(c["score"] + 0.5, 4)

    used = 0
    for c in chunks:                                            # locked chunks: always in, first
        if c["locked"]:
            c["included"], c["reason"] = True, "required: carries a rule or a fact the draft depends on"
            used += c["tokens"]
    optional = sorted((c for c in chunks if not c["locked"]), key=lambda c: (-c["score"], c["id"]))
    for c in optional:
        if c["id"] in excluded_ids:
            c["reason"] = "switched off by the person drafting"
        elif c["kind"] == "example" and c["score"] <= 0:
            c["reason"] = "not relevant to this campaign"
        elif used + c["tokens"] > token_budget:
            c["reason"] = "left out to stay within the token budget"
        else:
            c["included"], c["reason"] = True, f"relevant (score {c['score']})"
            used += c["tokens"]
    order = {c["id"]: i for i, c in enumerate(chunks)}
    chunks.sort(key=lambda c: (not c["included"], order[c["id"]]))
    return {"chunks": chunks, "tokens_used": used, "budget": token_budget}


def plan(conn, occasion, devices, brief_clean, excluded_ids=(), known=None, token_budget=None):
    chunks = candidate_chunks(conn, occasion, devices, known)
    query = " ".join([occasion, brief_clean] + [knowledge.DEVICE_FACTS[d][0] for d in devices if d in knowledge.DEVICE_FACTS])
    return select(chunks, query, occasion, devices, excluded_ids, token_budget)


def render_user_prompt(plan_, occasion, devices, brief_clean):
    """The user message: included chunks as labelled, quoted data; then the campaign facts; then the task."""
    parts = ["<context>"]
    for c in plan_["chunks"]:
        if c["included"]:
            parts.append(f'<chunk id="{c["id"]}" kind="{c["kind"]}" source="{c["source"]}">\n{c["text"]}\n</chunk>')
    parts.append("</context>")
    labels = ", ".join(knowledge.DEVICE_FACTS[d][0] for d in devices if d in knowledge.DEVICE_FACTS)
    parts.append(f"<campaign>\noccasion: {occasion}\ndevices the audience does not have: {labels}\n</campaign>")
    if brief_clean.strip():
        parts.append(f"<brief>\n{brief_clean.strip()}\n</brief>")
    parts.append(knowledge.TASK)
    return "\n".join(parts)
