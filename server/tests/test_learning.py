"""Diagnosis learning (stage 2): outcome records from agents, administrator verdicts, the summary page. Records only."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db
import learning
from tests.test_diagnosis import DiagBase, diag, finding
from tests.test_routes import _extract_csrf


def row(code="SPOOLER_STOPPED", device="printer", first="2026-10-20T10:00:00+00:00", last="2026-10-20T10:30:00+00:00", **kw):
    r = {"device": device, "code": code, "first_seen": first, "resolved_at": last, "duration_s": 1800, "checks": 6, "confidence": "confirmed",
         "severity": "high", "offered_fix": "start_service", "resolution": "self_resolved", "recurred": False, "actions": []}
    r.update(kw)
    return r


class Ingest(DiagBase):
    def send(self, rows, **extra):
        body = {"csp_id": "CSP001", "schema_version": 4, "agent_version": "2.3.0", "printer": {"configured": True, "present": True, "ok": True, "status": "OK"},
                "microatm": {"configured": True, "present": True, "ok": True, "status": "OK"}, "outcomes": rows}
        body.update(extra)
        r = self.client.post("/api/report", headers={"X-API-Key": self.key}, json=body)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))

    def test_rows_are_stored_and_resending_never_double_counts(self):
        for _ in range(3):
            self.send([row(), row(code="B", first="2026-10-21T10:00:00+00:00")])
        self.assertEqual(len(self.q("SELECT * FROM diagnosis_outcomes")), 2)

    def test_a_later_report_can_set_recurred_but_never_clears_it(self):
        self.send([row()])
        self.send([row(recurred=True)])
        self.send([row(recurred=False)])
        self.assertEqual(self.q("SELECT recurred FROM diagnosis_outcomes")[0]["recurred"], 1)

    def test_hostile_or_malformed_rows_are_dropped_and_never_break_the_heartbeat(self):
        self.send([row(device="evil"), row(resolution="magic"), row(first_seen="not a date"), "junk", None, {"device": "printer"},
                   row(code="<script>x</script>" * 20, first="2026-10-22T10:00:00+00:00", duration_s="NaN", actions="x")])
        stored = self.q("SELECT code, duration_s FROM diagnosis_outcomes")
        self.assertEqual(len(stored), 1)
        self.assertLessEqual(len(stored[0]["code"]), 80)
        self.assertEqual(stored[0]["duration_s"], 0)
        self.send("not a list")
        self.send(None)

    def test_old_agents_without_the_field_still_work(self):
        r = self.client.post("/api/report", headers={"X-API-Key": self.key},
                             json={"csp_id": "CSP001", "printer": {"configured": True, "present": True, "ok": True, "status": "OK"},
                                   "microatm": {"configured": True, "present": True, "ok": True, "status": "OK"}})
        self.assertEqual(r.status_code, 200)

    def test_a_row_cap_applies(self):
        self.send([row(code=f"C{i}") for i in range(learning.MAX_ROWS_PER_REPORT + 20)])
        self.assertEqual(len(self.q("SELECT * FROM diagnosis_outcomes")), learning.MAX_ROWS_PER_REPORT)


class Summary(unittest.TestCase):
    def setUp(self):
        from tests.helpers import make_test_app
        self.app, self.path = make_test_app()
        self.conn = db.get_connection()
        self.addCleanup(lambda: os.path.exists(self.path) and os.remove(self.path))
        self.addCleanup(self.conn.close)

    def add(self, n, code="X", csp="C1", **kw):
        body = {"agent_version": "2.3.0", "outcomes": [row(code=code, first=f"2026-10-{10 + i:02d}T10:00:00+00:00",
                                                           last=f"2026-10-{10 + i:02d}T10:30:00+00:00", **kw) for i in range(n)]}
        learning.store(self.conn, csp, body)
        self.conn.commit()

    def get(self, code="X"):
        return {s["code"]: s for s in learning.summary(self.conn, days=3650)}[code]

    def test_thin_evidence_is_labelled_and_gives_no_signals(self):
        self.add(2, resolution="self_resolved")
        s = self.get()
        self.assertEqual((s["episodes"], s["evidence"], s["signals"]), (2, "thin", []))

    def test_mostly_clears_by_itself_signal_needs_enough_cases(self):
        self.add(6, resolution="self_resolved")
        s = self.get()
        self.assertEqual(s["evidence"], "enough")
        self.assertTrue(any("clears by itself" in t for _, t in s["signals"]))

    def test_fix_that_holds_and_fix_that_does_not(self):
        act = [{"action": "start_service", "target": "S", "result": "ok"}]
        self.add(4, code="GOOD", resolution="after_fix", actions=act)
        self.add(4, code="BAD", resolution="after_fix", actions=act, recurred=True)
        self.assertTrue(any(k == "good" and "holds" in t for k, t in self.get("GOOD")["signals"]))
        self.assertTrue(any(k == "warn" and "does not hold" in t for k, t in self.get("BAD")["signals"]))
        self.assertEqual(self.get("GOOD")["fix_stuck"], 4)
        self.assertEqual(self.get("BAD")["fix_stuck"], 0)

    def test_median_time_and_pcs_counted(self):
        self.add(3, csp="C1")
        self.add(3, csp="C2")
        s = self.get()
        self.assertEqual(s["median_minutes"], 30.0)
        self.assertEqual(s["csps"], 2)

    def test_verdicts_feed_a_signal_only_with_enough_answers(self):
        self.add(1)
        for v in ("wrong", "wrong", "correct"):
            self.conn.execute("INSERT INTO diagnosis_feedback (created_at, admin, csp_id, device, code, verdict) VALUES ('2026-10-20','a','C1','printer','X',?)", (v,))
        self.conn.commit()
        self.assertTrue(any("often wrong" in t for _, t in self.get()["signals"]))


class Pages(DiagBase):
    def _issue_id(self):
        self.report(diag("DRIVER_ISSUE", "high", [finding()], summary="Windows reports error code 28"))
        return self.q("SELECT id FROM device_issues")[0]["id"]

    def token(self):
        return _extract_csrf(self.client.get("/api-keys").get_data(as_text=True))

    def test_pages_need_login_and_post_needs_csrf(self):
        self.assertEqual(self.client.get("/diagnosis-outcomes").status_code, 302)
        self.assertIn(self.client.post("/diagnosis-feedback", data={"issue_id": "1", "verdict": "correct"}).status_code, (302, 400))
        self._login()
        self.assertEqual(self.client.post("/diagnosis-feedback", data={"issue_id": "1", "verdict": "correct"}).status_code, 400)

    def test_verdict_is_recorded_and_shown_on_the_pc_page(self):
        iid = self._issue_id()
        self._login()
        r = self.client.post("/diagnosis-feedback", data={"csrf_token": self.token(), "issue_id": str(iid), "verdict": "wrong"}, follow_redirects=True)
        self.assertIn("Your answer was recorded".lower(), r.get_data(as_text=True).lower().replace("thank you - ", ""))
        self.assertEqual(self.q("SELECT verdict, csp_id, code FROM diagnosis_feedback"), [{"verdict": "wrong", "csp_id": "CSP001", "code": "CM_28"}])
        page = self.client.get("/csp/CSP001").get_data(as_text=True)
        self.assertIn("You said", page)
        self.assertIn("Wrong", page)

    def test_bad_input_is_refused_cleanly(self):
        iid = self._issue_id()
        self._login()
        for data in ({"issue_id": str(iid), "verdict": "maybe"}, {"issue_id": "99999", "verdict": "correct"}, {"issue_id": "abc", "verdict": "correct"}):
            r = self.client.post("/diagnosis-feedback", data=dict(data, csrf_token=self.token()), follow_redirects=True)
            self.assertEqual(r.status_code, 200)
        self.assertEqual(self.q("SELECT COUNT(*) n FROM diagnosis_feedback")[0]["n"], 0)

    def test_outcomes_page_renders_empty_and_with_data(self):
        self._login()
        self.assertIn("Nothing recorded yet", self.client.get("/diagnosis-outcomes").get_data(as_text=True))
        self._issue_id()
        with db.get_connection() as conn:
            learning.store(conn, "CSP001", {"agent_version": "2.3.0", "outcomes": [row(code="CM_28", device="biometric")]})
            conn.commit()
        page = self.client.get("/diagnosis-outcomes").get_data(as_text=True)
        self.assertIn("CM_28", page)
        self.assertIn("Not enough yet", page)


if __name__ == "__main__":
    unittest.main()
