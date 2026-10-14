"""The dashboard must never show a green OK for a device the agent could not
confirm: a plugged-in generic "USB Input Device" pick is UNVERIFIED (amber),
not OK. Older agents that don't report `verified` keep showing as before."""
import os
import re
import sys
import unittest
import html as htmllib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from device_state import (OK, PROBLEM, UNVERIFIED, NOT_CONFIGURED, NOT_DETECTED,
                          PROBLEM_STATES, derive_device_state)
from tests.helpers import issue_test_key, make_test_app
from tests.test_routes import RouteTestCase


def dev(verified=None, ok=True, present=True, configured=True, status="OK"):
    d = {"configured": configured, "present": present, "ok": ok, "status": status}
    if verified is not None:
        d["verified"] = verified
    return d


class DeriveStateTests(unittest.TestCase):
    def test_unverified_is_its_own_state_not_ok_and_not_a_problem(self):
        self.assertEqual(derive_device_state(1, 1, 1, "OK", verified=0), UNVERIFIED)
        self.assertNotEqual(UNVERIFIED, OK)
        self.assertNotIn(UNVERIFIED, PROBLEM_STATES)

    def test_verified_or_not_reported_stays_ok(self):
        self.assertEqual(derive_device_state(1, 1, 1, "OK", verified=1), OK)
        self.assertEqual(derive_device_state(1, 1, 1, "OK", verified=None), OK)   # older agent
        self.assertEqual(derive_device_state(1, 1, 1, "OK"), OK)

    def test_unverified_only_applies_to_a_healthy_present_device(self):
        self.assertEqual(derive_device_state(1, 1, 0, "Error", verified=0), PROBLEM)
        self.assertEqual(derive_device_state(1, 0, 0, "not found", verified=0), NOT_DETECTED)
        self.assertEqual(derive_device_state(0, 0, 0, "not configured", verified=0), NOT_CONFIGURED)


class ApiStoresVerifiedTests(unittest.TestCase):
    def setUp(self):
        self.app, self.db_path = make_test_app()
        self.client = self.app.test_client()
        self.key = issue_test_key("CSP001", "Test CSP One")
        self.addCleanup(lambda: os.path.exists(self.db_path) and os.remove(self.db_path))

    def _report(self, **dev_overrides):
        body = {"csp_id": "CSP001", "schema_version": 3, "agent_version": "1.3.2",
                "printer": dev(), "microatm": dev(), "biometric": dev(), "gps": dev()}
        body.update(dev_overrides)
        r = self.client.post("/api/report", headers={"X-API-Key": self.key}, json=body)
        self.assertEqual(r.status_code, 200)

    def _row(self):
        import db
        with db.get_connection() as conn:
            return dict(conn.execute("SELECT * FROM csps WHERE csp_id='CSP001'").fetchone())

    def _incidents(self):
        import db
        with db.get_connection() as conn:
            return conn.execute("SELECT device FROM incidents WHERE csp_id='CSP001'").fetchall()

    def test_verified_values_are_stored_per_device(self):
        self._report(printer=dev(True), microatm=dev(False), biometric=dev(False), gps=dev(True))
        row = self._row()
        self.assertEqual((row["printer_verified"], row["microatm_verified"],
                          row["biometric_verified"], row["gps_verified"]), (1, 0, 0, 1))

    def test_an_older_agent_report_without_verified_stores_null_and_never_wipes(self):
        self._report()   # no `verified` anywhere
        self.assertIsNone(self._row()["biometric_verified"])
        self._report(biometric=dev(False))
        self._report()   # an older-style report must not wipe the stored value
        self.assertEqual(self._row()["biometric_verified"], 0)

    def test_becoming_unverified_is_not_an_incident(self):
        self._report(biometric=dev(True))
        self._report(biometric=dev(False))
        self.assertEqual(self._incidents(), [])

    def test_a_real_problem_after_unverified_still_opens_an_incident(self):
        self._report(biometric=dev(False))
        self._report(biometric=dev(False, ok=False, status="Error"))
        self.assertEqual([r["device"] for r in self._incidents()], ["biometric"])

    def test_a_non_boolean_verified_value_cannot_crash_the_endpoint(self):
        self._report(biometric={"configured": True, "present": True, "ok": True, "status": "OK",
                                "verified": "maybe"})


class DashboardShowsUnverifiedTests(RouteTestCase):
    def setUp(self):
        super().setUp()
        self._login()
        import db
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        cols = ("csp_id, name, schema_version, printer_configured, printer_present, printer_ok, printer_verified, "
                "microatm_configured, microatm_present, microatm_ok, microatm_verified, "
                "biometric_configured, biometric_present, biometric_ok, biometric_verified, "
                "gps_configured, gps_present, gps_ok, gps_verified, first_seen, last_seen")
        with db.get_connection() as conn:
            # new agent: scanner picked as a generic entry -> unverified; everything else verified
            conn.execute(f"INSERT INTO csps ({cols}) VALUES ('NEWAG','New Agent',3,1,1,1,1, 1,1,1,1, 1,1,1,0, 1,1,1,1, ?,?)", (now, now))
            # old agent: never sends `verified` (NULL)
            conn.execute(f"INSERT INTO csps ({cols}) VALUES ('OLDAG','Old Agent',3,1,1,1,NULL, 1,1,1,NULL, 1,1,1,NULL, 1,1,1,NULL, ?,?)", (now, now))
            conn.commit()

    def _row_text(self, html, name):
        row = html.split(name)[1].split("</tr>")[0]
        return re.sub(r"\s+", " ", htmllib.unescape(re.sub(r"<[^>]+>", " ", row))).strip()

    def test_a_generic_pick_is_amber_unverified_not_green_ok(self):
        text = self._row_text(self.client.get("/").get_data(as_text=True), "New Agent")
        self.assertTrue(text.endswith("OK OK Unverified OK"), text)   # printer, micro-ATM OK; biometric unverified; GPS OK

    def test_an_older_agent_keeps_showing_ok(self):
        text = self._row_text(self.client.get("/").get_data(as_text=True), "Old Agent")
        self.assertTrue(text.endswith("OK OK OK OK"), text)

    def test_unverified_is_counted_but_is_not_a_problem(self):
        html = self.client.get("/").get_data(as_text=True)
        def kpi(label):
            return re.search(r'kpi-value">(\d+)</div>\s*<div class="kpi-label">' + label, html).group(1)
        self.assertEqual(kpi("Unverified devices"), "1")
        self.assertEqual(kpi("Biometric problems"), "0")
        self.assertNotIn("New Agent", self.client.get("/?problems_only=1").get_data(as_text=True))

    def test_legend_explains_unverified(self):
        html = self.client.get("/").get_data(as_text=True)
        self.assertIn("Unverified</strong> (amber)", html)
        self.assertIn("Find my device", html)

    def test_csv_carries_the_unverified_state(self):
        lines = self.client.get("/fleet.csv").get_data(as_text=True).splitlines()
        by_id = {l.split(",")[0]: l for l in lines[1:]}
        self.assertIn("UNVERIFIED", by_id["NEWAG"])
        self.assertNotIn("UNVERIFIED", by_id["OLDAG"])

    def test_detail_page_shows_unverified(self):
        html = self.client.get("/csp/NEWAG").get_data(as_text=True)
        self.assertIn("unverified", html)


if __name__ == "__main__":
    unittest.main()
