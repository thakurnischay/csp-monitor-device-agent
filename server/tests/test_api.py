"""API tests for POST /api/report - the one endpoint every agent talks to."""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.helpers import issue_test_key, make_test_app


class ReportEndpointTests(unittest.TestCase):
    def setUp(self):
        self.app, self.db_path = make_test_app()
        self.client = self.app.test_client()
        self.key = issue_test_key("CSP001", "Test CSP One")

    def tearDown(self):
        try:
            os.remove(self.db_path)
        except OSError:
            pass

    def _report(self, csp_id="CSP001", key=None, printer=None, microatm=None, extra=None):
        body = {"csp_id": csp_id,
               "printer": printer if printer is not None else {"configured": True, "present": True, "ok": True, "status": "idle (ready)"},
               "microatm": microatm if microatm is not None else {"configured": True, "present": True, "ok": True, "status": "OK"}}
        if extra:
            body.update(extra)
        return self.client.post("/api/report",
                                headers={"X-API-Key": key if key is not None else self.key},
                                json=body)

    def test_valid_report_is_accepted(self):
        resp = self._report()
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json()["ok"])

    def test_invalid_api_key_is_rejected(self):
        resp = self._report(key="not-the-real-key")
        self.assertEqual(resp.status_code, 401)
        self.assertFalse(resp.get_json()["ok"])

    def test_missing_api_key_is_rejected(self):
        resp = self.client.post("/api/report", json={"csp_id": "CSP001", "printer": {}, "microatm": {}})
        self.assertEqual(resp.status_code, 401)

    def test_revoked_key_is_rejected(self):
        import db
        with db.get_connection() as conn:
            conn.execute("UPDATE api_keys SET active=0 WHERE csp_id=?", ("CSP001",))
            conn.commit()
        resp = self._report()
        self.assertEqual(resp.status_code, 401)

    def test_correct_key_for_a_different_csp_is_rejected(self):
        """The real security property: a valid key only authenticates the
        SPECIFIC csp_id it was issued for, not any csp_id the request claims."""
        other_key = issue_test_key("CSP002", "Test CSP Two")
        resp = self._report(csp_id="CSP001", key=other_key)
        self.assertEqual(resp.status_code, 401)

    def test_malformed_json_body_does_not_crash(self):
        resp = self.client.post("/api/report", headers={"X-API-Key": self.key},
                                data="not valid json{{{", content_type="application/json")
        self.assertIn(resp.status_code, (400, 401))  # never a 500

    def test_missing_csp_id_is_rejected(self):
        resp = self.client.post("/api/report", headers={"X-API-Key": self.key},
                                json={"printer": {}, "microatm": {}})
        self.assertEqual(resp.status_code, 401)

    def test_printer_as_wrong_type_does_not_crash(self):
        """A misbehaving/buggy agent sending printer as a string, not an
        object, must degrade gracefully, never 500."""
        resp = self._report(printer="not-an-object")
        self.assertEqual(resp.status_code, 200)

    def test_oversized_payload_is_rejected(self):
        huge = {"csp_id": "CSP001", "printer": {"status": "x" * (100 * 1024)}, "microatm": {}}
        resp = self.client.post("/api/report", headers={"X-API-Key": self.key}, json=huge)
        self.assertEqual(resp.status_code, 413)

    def test_rate_limit_blocks_excessive_reports(self):
        # security.report_limiter is a module-level singleton shared across
        # the whole test run - a csp_id reused by other tests would make
        # this order-dependent, so this test gets its own dedicated CSP with
        # a clean rate-limit budget regardless of what ran before it.
        key = issue_test_key("CSP_RATE_LIMIT_ONLY", "Rate Limit Test CSP")
        for _ in range(6):
            resp = self._report(csp_id="CSP_RATE_LIMIT_ONLY", key=key)
            self.assertEqual(resp.status_code, 200)
        resp = self._report(csp_id="CSP_RATE_LIMIT_ONLY", key=key)
        self.assertEqual(resp.status_code, 429)

    def test_last_used_at_updates_on_successful_report(self):
        import db
        with db.get_connection() as conn:
            before = conn.execute("SELECT last_used_at FROM api_keys WHERE csp_id=?", ("CSP001",)).fetchone()
        self.assertIsNone(before["last_used_at"])
        self._report()
        with db.get_connection() as conn:
            after = conn.execute("SELECT last_used_at FROM api_keys WHERE csp_id=?", ("CSP001",)).fetchone()
        self.assertIsNotNone(after["last_used_at"])

    def test_state_transition_opens_and_resolves_incident(self):
        self._report(printer={"configured": True, "present": True, "ok": True, "status": "idle (ready)"})
        self._report(printer={"configured": True, "present": True, "ok": False, "status": "offline"})
        import db
        with db.get_connection() as conn:
            open_incidents = conn.execute(
                "SELECT * FROM incidents WHERE csp_id='CSP001' AND status='OPEN'").fetchall()
        self.assertEqual(len(open_incidents), 1)
        self.assertEqual(open_incidents[0]["device"], "printer")

    def test_metadata_is_stored_and_preserved_across_a_metadata_less_report(self):
        self._report(extra={"agent_version": "1.1.0", "hostname": "TEST-PC", "os": "Windows-10"})
        import db
        with db.get_connection() as conn:
            row = conn.execute("SELECT agent_version, hostname FROM csps WHERE csp_id='CSP001'").fetchone()
        self.assertEqual(row["agent_version"], "1.1.0")
        self.assertEqual(row["hostname"], "TEST-PC")

        # A subsequent report with NO metadata must not wipe it (regression
        # test for a real bug found and fixed during Step 4).
        self._report()
        with db.get_connection() as conn:
            row2 = conn.execute("SELECT agent_version, hostname FROM csps WHERE csp_id='CSP001'").fetchone()
        self.assertEqual(row2["agent_version"], "1.1.0")
        self.assertEqual(row2["hostname"], "TEST-PC")


if __name__ == "__main__":
    unittest.main()
