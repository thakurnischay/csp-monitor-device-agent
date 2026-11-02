"""A micro-ATM software crash reported by the agent is stored as an issue, shown on the dashboard, and closes by itself."""
import sqlite3
import unittest

from tests.helpers import issue_test_key
from tests.test_routes import RouteTestCase, _extract_csrf

FINDING = {"code": "SOFTWARE_CRASHED", "state": "SOFTWARE_ISSUE", "severity": "medium",
           "title": "The micro-ATM software (Ingenico LLT) crashed recently",
           "symptom": "Ingenico LLT stopped unexpectedly in the last 24 hours.", "root_cause": "Windows recorded the program crashing.",
           "confidence": "confirmed", "category": "software", "evidence": ["Windows recorded 1 crash(es) and 0 hang(s)"],
           "actions": ["Open Ingenico LLT again from its desktop shortcut."], "operator_action": True, "auto_fix": None}


def report(findings):
    return {"csp_id": "SW-1", "agent_version": "2.1.0", "schema_version": 4,
            "microatm": {"configured": True, "present": True, "ok": True, "status": "OK"},
            "diagnosis": {"microatm": {"health_state": "SOFTWARE_ISSUE" if findings else "HEALTHY",
                                       "severity": "medium" if findings else "none",
                                       "summary": findings[0]["title"] if findings else "No problem found",
                                       "findings": findings, "notes": [], "checked": [], "not_checked": []}}}


class SoftwareDiag(RouteTestCase):
    def setUp(self):
        super().setUp()
        self.key = issue_test_key("SW-1", "Software CSP")

    def post(self, body):
        return self.client.post("/api/report", json=body, headers={"X-API-Key": self.key})

    def issues(self):
        con = sqlite3.connect(self.db_path)
        try:
            return con.execute("SELECT code, status, severity FROM device_issues WHERE csp_id='SW-1'").fetchall()
        finally:
            con.close()

    def test_a_crash_becomes_an_open_issue_and_closes_when_it_is_no_longer_reported(self):
        self.assertEqual(self.post(report([FINDING])).status_code, 200)
        self.assertEqual(self.issues(), [("SOFTWARE_CRASHED", "OPEN", "medium")])
        self._login()
        page = self.client.get("/csp/SW-1").get_data(as_text=True)
        self.assertIn("Ingenico LLT", page)
        self.assertEqual(self.post(report([])).status_code, 200)
        self.assertEqual(self.issues()[0][1], "RESOLVED")


if __name__ == "__main__":
    unittest.main()
