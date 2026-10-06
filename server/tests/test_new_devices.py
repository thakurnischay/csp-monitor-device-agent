"""Biometric scanner + GPS dongle support: storing them from the heartbeat,
backward compatibility with older agents, history/incidents, the additive
migration, and how the dashboard shows them."""
import os
import html as htmllib
import re
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.helpers import issue_test_key, make_test_app
from tests.test_routes import RouteTestCase

OK = {"configured": True, "present": True, "ok": True, "status": "OK"}
MISSING = {"configured": True, "present": False, "ok": False,
           "status": "not found — check the fingerprint scanner is plugged in"}


class ReportStoresNewDevicesTests(unittest.TestCase):
    def setUp(self):
        self.app, self.db_path = make_test_app()
        self.client = self.app.test_client()
        self.key = issue_test_key("CSP001", "Test CSP One")

    def tearDown(self):
        try:
            os.remove(self.db_path)
        except OSError:
            pass

    def _report(self, **extra):
        body = {"csp_id": "CSP001", "schema_version": 3,
                "printer": OK, "microatm": OK}
        body.update(extra)
        return self.client.post("/api/report", headers={"X-API-Key": self.key}, json=body)

    def _row(self):
        import db
        with db.get_connection() as conn:
            return dict(conn.execute("SELECT * FROM csps WHERE csp_id='CSP001'").fetchone())

    def _events(self):
        import db
        with db.get_connection() as conn:
            return [r["event_type"] for r in conn.execute(
                "SELECT event_type FROM events WHERE csp_id='CSP001' ORDER BY id").fetchall()]

    def _incidents(self):
        import db
        with db.get_connection() as conn:
            return [(r["device"], r["status"]) for r in conn.execute(
                "SELECT device, status FROM incidents WHERE csp_id='CSP001' ORDER BY id").fetchall()]

    def test_both_devices_are_stored(self):
        self.assertEqual(self._report(biometric=OK, gps=dict(OK, status="Present")).status_code, 200)
        row = self._row()
        self.assertEqual((row["biometric_configured"], row["biometric_present"], row["biometric_ok"]), (1, 1, 1))
        self.assertEqual((row["gps_configured"], row["gps_ok"], row["gps_status"]), (1, 1, "Present"))

    def test_a_disconnected_scanner_opens_an_incident_and_reconnecting_resolves_it(self):
        self._report(biometric=OK, gps=OK)
        self._report(biometric=MISSING, gps=OK)
        self.assertIn(("biometric", "OPEN"), self._incidents())
        self.assertIn("biometric_disconnected", self._events())
        self._report(biometric=OK, gps=OK)
        self.assertIn(("biometric", "RESOLVED"), self._incidents())
        self.assertIn("biometric_connected", self._events())

    def test_an_older_agent_without_these_keys_changes_nothing_and_creates_no_events(self):
        resp = self.client.post("/api/report", headers={"X-API-Key": self.key},
                                json={"csp_id": "CSP001", "schema_version": 2,
                                      "printer": OK, "microatm": OK})
        self.assertEqual(resp.status_code, 200)
        row = self._row()
        self.assertEqual((row["biometric_configured"], row["gps_configured"]), (0, 0))
        self.assertFalse([e for e in self._events() if e.startswith(("biometric", "gps"))])
        self.assertFalse([i for i in self._incidents() if i[0] in ("biometric", "gps")])

    def test_an_older_report_never_wipes_previously_stored_values(self):
        self._report(biometric=OK, gps=OK)
        self.client.post("/api/report", headers={"X-API-Key": self.key},
                         json={"csp_id": "CSP001", "printer": OK, "microatm": OK})
        row = self._row()
        self.assertEqual((row["biometric_ok"], row["gps_ok"]), (1, 1))

    def test_a_malformed_value_does_not_crash_the_endpoint(self):
        self.assertEqual(self._report(biometric="oops", gps=["x"]).status_code, 200)
        self.assertEqual(self._row()["biometric_configured"], 0)


class AdditiveMigrationTests(unittest.TestCase):
    def test_old_csps_table_gains_the_new_columns_and_keeps_its_rows(self):
        import db
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            conn = sqlite3.connect(path)
            conn.row_factory = sqlite3.Row
            conn.execute("CREATE TABLE csps (csp_id TEXT PRIMARY KEY, name TEXT, last_seen TEXT)")
            conn.execute("INSERT INTO csps VALUES ('OLD1', 'Old CSP', '2026-01-01T00:00:00+00:00')")
            db._migrate_csps_columns(conn)
            db._migrate_csps_columns(conn)   # idempotent
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(csps)").fetchall()}
            for dev in ("biometric", "gps"):
                for suffix in ("configured", "present", "ok", "status"):
                    self.assertIn(f"{dev}_{suffix}", cols)
            row = conn.execute("SELECT * FROM csps WHERE csp_id='OLD1'").fetchone()
            self.assertEqual(row["name"], "Old CSP")
            self.assertEqual(row["biometric_configured"], 0)
            conn.close()
        finally:
            os.remove(path)


class DashboardShowsNewDevicesTests(RouteTestCase):
    def setUp(self):
        super().setUp()
        self._login()
        import db
        now = datetime.now(timezone.utc)
        fresh = now.isoformat(timespec="seconds")
        old = (now - timedelta(days=2)).isoformat(timespec="seconds")
        cols = ("csp_id, name, schema_version, printer_configured, printer_present, printer_ok, "
                "microatm_configured, microatm_present, microatm_ok, "
                "biometric_configured, biometric_present, biometric_ok, "
                "gps_configured, gps_present, gps_ok, first_seen, last_seen")
        with db.get_connection() as conn:
            # v3 agent, online: scanner fine, GPS dongle configured but gone
            conn.execute(f"INSERT INTO csps ({cols}) VALUES ('V3','Vee Three',3,1,1,1,1,1,1,1,1,1,1,0,0,?,?)", (fresh, fresh))
            # old agent, online: never reports the new devices
            conn.execute(f"INSERT INTO csps ({cols}) VALUES ('OLDAG','Old Agent',NULL,1,1,1,1,1,1,0,0,0,0,0,0,?,?)", (fresh, fresh))
            # v3 agent, offline for 2 days, last report said a scanner problem
            conn.execute(f"INSERT INTO csps ({cols}) VALUES ('OFFV3','Offline Vee',3,1,1,1,1,1,1,1,0,0,1,1,1,?,?)", (old, old))
            conn.commit()

    def _row_text(self, html, name):
        row = html.split(name)[1].split("</tr>")[0]
        return re.sub(r"\s+", " ", htmllib.unescape(re.sub(r"<[^>]+>", " ", row))).strip()

    def test_columns_and_kpi_cards_exist(self):
        html = self.client.get("/").get_data(as_text=True)
        self.assertIn("Biometric status", html)
        self.assertIn("GPS status", html)
        self.assertIn("Biometric problems", html)
        self.assertIn("GPS problems", html)

    def test_online_v3_csp_shows_live_ok_and_problem(self):
        text = self._row_text(self.client.get("/").get_data(as_text=True), "Vee Three")
        self.assertTrue(text.endswith("OK Problem"), text)   # biometric OK, GPS problem

    def test_older_agent_shows_a_dash_not_not_set_up(self):
        html = self.client.get("/").get_data(as_text=True)
        text = self._row_text(html, "Old Agent")
        self.assertTrue(text.endswith("– –"), text)   # two dashes
        self.assertNotIn("Not set up", text)

    def test_offline_v3_csp_shows_unknown_for_the_new_devices(self):
        text = self._row_text(self.client.get("/").get_data(as_text=True), "Offline Vee")
        self.assertTrue(text.endswith("Unknown Unknown"), text)

    def test_kpi_counts_only_live_problems_from_agents_that_report_them(self):
        html = self.client.get("/").get_data(as_text=True)
        def kpi(label):
            return re.search(r'kpi-value">(\d+)</div>\s*<div class="kpi-label">' + label, html).group(1)
        self.assertEqual(kpi("Biometric problems"), "0")  # the only biometric "problem" is on the OFFLINE csp
        self.assertEqual(kpi("GPS problems"), "1")        # Vee Three's missing dongle

    def test_problems_only_catches_a_gps_only_problem(self):
        html = self.client.get("/?problems_only=1").get_data(as_text=True)
        self.assertIn("Vee Three", html)
        self.assertNotIn("Old Agent", html)
        self.assertNotIn("Offline Vee", html)

    def test_csv_has_the_new_columns_with_honest_states(self):
        lines = self.client.get("/fleet.csv").get_data(as_text=True).splitlines()
        self.assertIn("biometric_state,gps_state", lines[0])
        by_id = {l.split(",")[0]: l for l in lines[1:]}
        self.assertIn("OK,NOT_DETECTED", by_id["V3"])
        self.assertIn("NOT_REPORTED,NOT_REPORTED", by_id["OLDAG"])
        self.assertIn("UNKNOWN,UNKNOWN", by_id["OFFV3"])

    def test_detail_page_explains_each_case(self):
        v3 = self.client.get("/csp/V3").get_data(as_text=True)
        self.assertIn("Biometric device (fingerprint scanner)", v3)
        self.assertIn("GPS dongle", v3)
        old = self.client.get("/csp/OLDAG").get_data(as_text=True)
        self.assertIn("older version that doesn't report this device yet", old)
        off = self.client.get("/csp/OFFV3").get_data(as_text=True)
        self.assertIn("unknown - CSP offline", off)


if __name__ == "__main__":
    unittest.main()
