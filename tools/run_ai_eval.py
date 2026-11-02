"""ADMIN TOOL - runs the AI drafting assistant's golden test set and records the result for the "AI quality" page.

    python tools/run_ai_eval.py                       free: deterministic stand-in, checks the graders and the harness
    python tools/run_ai_eval.py --live --yes-spend    calls the real model once per case (56 calls, costs real money; needs ANTHROPIC_API_KEY)

Run it on the server (inside the container: docker compose exec csp-heartbeat-v2 python /app/tools/run_ai_eval.py ...) so the
result lands in the live database. Live drafts are generated against a throw-away database, so they never appear as real drafts.
"""
import argparse
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "server"))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--live", action="store_true", help="call the real model")
    ap.add_argument("--yes-spend", action="store_true", help="confirm you accept the cost of a live run")
    args = ap.parse_args(argv)

    import db
    from campaigns.ai import drafting, evals

    if args.live:
        if not args.yes_spend:
            ap.error("a live run makes one paid model call per case; add --yes-spend to confirm")
        if not drafting.enabled():
            ap.error("AI drafting is not configured (ANTHROPIC_API_KEY is not set)")
        real_path = db.db_path()
        scratch = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        scratch.close()
        os.environ["CSP_MONITOR_DB_PATH"] = scratch.name
        db.setup()
        scratch_conn = db.get_connection()

        def draft(case):
            out = drafting.generate(scratch_conn, {"occasion": case["occasion"], "devices": case["devices"], "audience_match": "any",
                                                   "include_dormant": False}, "", [], "eval")
            return out["subject"], out["body"]
        os.environ["CSP_MONITOR_DB_PATH"] = real_path
        conn = db.get_connection()
        try:
            summary = evals.run(conn, "live", draft)
        finally:
            scratch_conn.close()
            conn.close()
            try:
                os.remove(scratch.name)
            except OSError:
                pass
    else:
        conn = db.get_connection()
        summary = evals.run(conn, "offline")
        conn.close()
    print("%d of %d cases passed" % (summary["passed"], summary["total"]))
    for f in summary["failed"]:
        print("  FAILED", f["id"], f["failed"])
    return 0 if not summary["failed"] else 1


if __name__ == "__main__":
    sys.exit(main())
