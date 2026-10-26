"""The golden set and its programmatic graders."""
import json
import os
import sys
import unittest

import db
from campaigns.ai import evals
from tests.helpers import make_test_app

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "tools"))


class Evals(unittest.TestCase):
    def setUp(self):
        self.app, self.path = make_test_app()
        self.conn = db.get_connection()
        self.addCleanup(lambda: os.path.exists(self.path) and os.remove(self.path))   # runs last
        self.addCleanup(self.conn.close)

    def test_golden_set_covers_every_occasion_and_is_unique(self):
        from campaigns import service
        cases = evals.load_cases()
        self.assertEqual(len(cases), len(service.OCCASIONS) * 4)
        self.assertEqual(len({c["id"] for c in cases}), len(cases))
        self.assertEqual({c["occasion"] for c in cases}, set(service.OCCASIONS))

    def test_offline_run_passes_and_is_recorded(self):
        out = evals.run(self.conn, "offline")
        self.assertEqual(out["passed"], out["total"])
        row = self.conn.execute("SELECT * FROM ai_eval_runs").fetchone()
        self.assertEqual((row["mode"], row["cases_passed"], row["cases_total"]), ("offline", out["total"], out["total"]))
        self.assertEqual(len(json.loads(row["details_json"])), out["total"])

    def test_graders_catch_bad_drafts(self):
        case = {"occasion": "Holi", "devices": ["GPS"]}
        ok = evals.grade(case, "Happy {occasion}, {first_name}", "Dear {name}, enjoy {occasion}. You do not yet have {missing_devices}. Please ask {rm_name} about it today.")
        self.assertTrue(all(p for p, _ in ok.values()), ok)
        bad = evals.grade(case, "Guaranteed earnings", "Call 9876543210 or mail a@b.com. Earn Rs. 5000 guaranteed, share your OTP {bogus}")
        for check in ("valid_template", "no_personal_data", "no_high_risk_wording", "names_missing_devices", "greets_by_name"):
            self.assertFalse(bad[check][0], check)

    def test_a_failing_draft_source_fails_the_case_not_the_run(self):
        def boom(case):
            raise RuntimeError("model down")
        out = evals.run(self.conn, "offline", boom)
        self.assertEqual(out["passed"], 0)
        self.assertIn("produced_a_draft", out["failed"][0]["failed"])

    def test_live_run_needs_explicit_spend_confirmation(self):
        import run_ai_eval
        with self.assertRaises(SystemExit):
            run_ai_eval.main(["--live"])


if __name__ == "__main__":
    unittest.main()
