"""The KPIs from the blueprint, computed from the stored telemetry. Every figure says what it is and how it is measured; where there
is not enough data to mean anything it says so instead of showing a number.

  Implicit acceptance rate   launched AI drafts whose final text is IDENTICAL to what the model wrote / all launched AI drafts   (target > 75%)
  Correction delta           mean Levenshtein distance (characters) and mean edit ratio between the AI's text and the launched text  (target -> 0)
  Context window efficiency  mean input tokens per draft, and mean total tokens spent per launched campaign (all drafts for it)  (target: falling)
  Retrieval precision        of the optional context chunks given to the model, the share it reports having used        (target > 85%)  [MODEL-REPORTED]
  HITL interventions         edits saved + regenerations per launched campaign                                           (target: falling)
  Task completion rate       drafts that produced a valid result / all attempts (errors, refusals and invalid output are failures)   (target > 95%)
"""
import json
from datetime import datetime, timedelta, timezone

MIN_LAUNCHED = 5            # below this, an acceptance / delta figure is shown as "not enough data"
TARGETS = {"acceptance": 75.0, "completion": 95.0, "precision": 85.0, "delta_ratio": 0.10}


def _since(days):
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")


def _avg(xs):
    xs = [x for x in xs if x is not None]
    return round(sum(xs) / len(xs), 2) if xs else None


def _trend(values):
    """'down' / 'up' / 'flat' comparing the first half of a series with the second, or None if too short."""
    values = [v for v in values if v is not None]
    if len(values) < 4:
        return None
    h = len(values) // 2
    a, b = sum(values[:h]) / h, sum(values[h:]) / (len(values) - h)
    if abs(b - a) <= 0.02 * max(abs(a), 1e-9):
        return "flat"
    return "down" if b < a else "up"


def _week(iso):
    try:
        d = datetime.fromisoformat(str(iso))
    except ValueError:
        return "?"
    d = d - timedelta(days=d.weekday())
    return d.strftime("%Y-%m-%d")


def compute(conn, days=90):
    since = _since(days)
    gens = [dict(r) for r in conn.execute("SELECT * FROM ai_generations WHERE created_at>=? ORDER BY id", (since,)).fetchall()]
    gid = {g["id"] for g in gens}
    fb = [dict(r) for r in conn.execute("SELECT * FROM ai_feedback WHERE generation_id IN (SELECT id FROM ai_generations WHERE created_at>=?) ORDER BY id", (since,)).fetchall()]
    by_gen = {}
    for e in fb:
        by_gen.setdefault(e["generation_id"], []).append(e)

    ok = [g for g in gens if g["status"] == "ok"]
    launched = {}
    for g in ok:
        ev = [e for e in by_gen.get(g["id"], []) if e["event"] == "launched"]
        if ev:
            launched[g["id"]] = ev[-1]
    n_l = len(launched)
    unchanged = sum(1 for e in launched.values() if e["outcome"] == "unchanged")
    light = sum(1 for e in launched.values() if e["edit_ratio"] is not None and e["edit_ratio"] <= 0.05)
    dist = [(e["subject_distance"] or 0) + (e["body_distance"] or 0) for e in launched.values()]
    ratios = [e["edit_ratio"] for e in launched.values()]

    saved = [[e for e in by_gen.get(g["id"], []) if e["event"] == "draft_saved"] for g in ok]
    saved_final = [s[-1] for s in saved if s]
    regenerated = sum(1 for e in fb if e["event"] == "regenerated")
    edits = sum(1 for e in fb if e["event"] == "draft_saved" and e["outcome"] in ("edited", "rewritten"))

    campaigns = {}
    for g in gens:
        if g["campaign_id"]:
            c = campaigns.setdefault(g["campaign_id"], {"tokens": 0, "launched": False})
            c["tokens"] += (g["input_tokens"] or 0) + (g["output_tokens"] or 0)
    for gid_, e in launched.items():
        cid = next((g["campaign_id"] for g in gens if g["id"] == gid_), None)
        if cid in campaigns:
            campaigns[cid]["launched"] = True
    tokens_per_launched = _avg([c["tokens"] for c in campaigns.values() if c["launched"]])

    prec_all, prec_opt = [], []
    for g in ok:
        try:
            ctx = json.loads(g["context_json"] or "[]")
            cited = set(json.loads(g["cited_context_json"] or "[]"))
        except ValueError:
            continue
        inc = [c for c in ctx if c.get("included")]
        opt = [c for c in inc if not c.get("locked")]
        if inc:
            prec_all.append(100.0 * len({c["id"] for c in inc} & cited) / len(inc))
        if opt:
            prec_opt.append(100.0 * len({c["id"] for c in opt} & cited) / len(opt))

    weeks = {}
    for g in gens:
        w = weeks.setdefault(_week(g["created_at"]), {"week": _week(g["created_at"]), "generations": 0, "ok": 0, "launched": 0, "unchanged": 0,
                                                       "ratios": [], "input_tokens": []})
        w["generations"] += 1
        if g["status"] == "ok":
            w["ok"] += 1
            w["input_tokens"].append(g["input_tokens"])
        if g["id"] in launched:
            w["launched"] += 1
            w["unchanged"] += 1 if launched[g["id"]]["outcome"] == "unchanged" else 0
            w["ratios"].append(launched[g["id"]]["edit_ratio"])
    series = []
    for k in sorted(weeks):
        w = weeks[k]
        series.append({"week": w["week"], "generations": w["generations"], "launched": w["launched"],
                       "acceptance": round(100.0 * w["unchanged"] / w["launched"], 1) if w["launched"] else None,
                       "avg_edit_ratio": _avg(w["ratios"]), "avg_input_tokens": _avg(w["input_tokens"])})

    lat = sorted(g["latency_ms"] for g in ok if g["latency_ms"])
    enough = n_l >= MIN_LAUNCHED
    acceptance = round(100.0 * unchanged / n_l, 1) if n_l else None
    avg_ratio = _avg(ratios)
    return {
        "days": days, "min_launched": MIN_LAUNCHED, "enough_data": enough, "targets": TARGETS,
        "generations": len(gens), "ok": len(ok), "failed": len(gens) - len(ok),
        "failures_by_status": {s: sum(1 for g in gens if g["status"] == s) for s in ("error", "refused", "invalid_output") if any(g["status"] == s for g in gens)},
        "completion_rate": round(100.0 * len(ok) / len(gens), 1) if gens else None,
        "launched": n_l, "saved_with_events": len(saved_final),
        "acceptance_rate": acceptance, "light_edit_rate": round(100.0 * light / n_l, 1) if n_l else None,
        "outcomes_at_launch": {o: sum(1 for e in launched.values() if e["outcome"] == o) for o in ("unchanged", "edited", "rewritten")},
        "outcomes_when_saved": {o: sum(1 for e in saved_final if e["outcome"] == o) for o in ("unchanged", "edited", "rewritten")},
        "avg_distance_chars": _avg(dist), "avg_edit_ratio": avg_ratio,
        "regenerations": regenerated, "edits_saved": edits,
        "hitl_per_launched": round((regenerated + edits) / n_l, 2) if n_l else None,
        "avg_input_tokens": _avg([g["input_tokens"] for g in ok]), "avg_output_tokens": _avg([g["output_tokens"] for g in ok]),
        "tokens_per_launched_campaign": tokens_per_launched, "avg_context_tokens_est": _avg([g["context_tokens_est"] for g in ok]),
        "retrieval_precision_optional": _avg(prec_opt), "retrieval_precision_all": _avg(prec_all),
        "avg_latency_ms": _avg(lat), "p95_latency_ms": lat[int(0.95 * (len(lat) - 1))] if lat else None,
        "est_cost_usd": round(sum(g["est_cost_usd"] or 0 for g in gens), 4),
        "weekly": series,
        "trend_edit_ratio": _trend([s["avg_edit_ratio"] for s in series]),
        "trend_input_tokens": _trend([s["avg_input_tokens"] for s in series]),
        "status": {
            "acceptance": None if not enough else acceptance >= TARGETS["acceptance"],
            "delta": None if not enough else (avg_ratio is not None and avg_ratio <= TARGETS["delta_ratio"]),
            "completion": None if len(gens) < MIN_LAUNCHED else (len(ok) / len(gens) * 100 >= TARGETS["completion"]),
            "precision": None if len(prec_opt) < MIN_LAUNCHED else (_avg(prec_opt) >= TARGETS["precision"]),
        },
    }


def recent_generations(conn, limit=25):
    rows = conn.execute(
        """SELECT g.id, g.created_at, g.created_by, g.campaign_id, g.status, g.model, g.input_tokens, g.output_tokens, g.latency_ms,
                  (SELECT outcome FROM ai_feedback f WHERE f.generation_id=g.id AND f.event='launched' ORDER BY f.id DESC LIMIT 1) launched_outcome,
                  (SELECT edit_ratio FROM ai_feedback f WHERE f.generation_id=g.id AND f.event IN ('launched','draft_saved') AND f.edit_ratio IS NOT NULL ORDER BY f.id DESC LIMIT 1) last_ratio,
                  (SELECT COUNT(*) FROM ai_feedback f WHERE f.generation_id=g.id) events
           FROM ai_generations g ORDER BY g.id DESC LIMIT ?""", (limit,)).fetchall()
    return [dict(r) for r in rows]


def eval_runs(conn, limit=8):
    return [dict(r) for r in conn.execute("SELECT * FROM ai_eval_runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()]
