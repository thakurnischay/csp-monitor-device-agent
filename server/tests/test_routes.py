"""UI route tests - login (with CSRF/rate-limit), Fleet filtering, API-key
lifecycle, and the audit log they all write to."""
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.helpers import make_test_app

ADMIN_PASSWORD = "admin123"  # db.setup()'s default bootstrap password


def _extract_csrf(html: str) -> str:
    m = re.search(r'name="csrf_token" value="([^"]*)"', html)
    return m.group(1) if m else ""


class RouteTestCase(unittest.TestCase):
    def setUp(self):
        self.app, self.db_path = make_test_app()
        self.client = self.app.test_client()

    def tearDown(self):
        try:
            os.remove(self.db_path)
        except OSError:
            pass

    def _login(self, login_id="admin", password=ADMIN_PASSWORD):
        page = self.client.get("/login")
        token = _extract_csrf(page.get_data(as_text=True))
        return self.client.post("/login", data={
            "csrf_token": token, "login_id": login_id, "password": password})


class LoginTests(RouteTestCase):
    def test_correct_credentials_redirect_to_fleet(self):
        resp = self._login()
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.headers["Location"], "/")

    def test_wrong_password_shows_flash_not_a_redirect(self):
        resp = self._login(password="wrong")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Invalid credentials", resp.get_data(as_text=True))

    def test_missing_csrf_token_is_rejected(self):
        resp = self.client.post("/login", data={"login_id": "admin", "password": ADMIN_PASSWORD})
        self.assertEqual(resp.status_code, 400)

    def test_login_writes_an_audit_entry(self):
        self._login()
        import db
        with db.get_connection() as conn:
            rows = conn.execute("SELECT * FROM admin_audit_log WHERE action='login'").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["actor"], "admin")

    def test_fleet_requires_login(self):
        resp = self.client.get("/")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login", resp.headers["Location"])

    def test_login_rate_limit(self):
        # security.login_limiter allows 8/min per IP (see security.py).
        for _ in range(8):
            self._login(password="wrong")
        resp = self._login(password="wrong")
        self.assertEqual(resp.status_code, 429)


class FleetFilterTests(RouteTestCase):
    def setUp(self):
        super().setUp()
        self._login()
        import db
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with db.get_connection() as conn:
            conn.execute("""INSERT INTO csps (csp_id, name, printer_configured, printer_present, printer_ok,
                             microatm_configured, microatm_present, microatm_ok, first_seen, last_seen)
                             VALUES ('A1','Alice CSP',1,1,1,0,0,0,?,?)""", (now, now))
            conn.execute("""INSERT INTO csps (csp_id, name, printer_configured, printer_present, printer_ok,
                             microatm_configured, microatm_present, microatm_ok, first_seen, last_seen)
                             VALUES ('B1','Bob CSP',1,1,0,1,1,1,?,?)""", (now, now))
            conn.commit()

    def test_fleet_lists_all_by_default(self):
        resp = self.client.get("/")
        html = resp.get_data(as_text=True)
        self.assertIn("Alice CSP", html)
        self.assertIn("Bob CSP", html)

    def test_search_filters_by_name(self):
        resp = self.client.get("/?q=alice")
        html = resp.get_data(as_text=True)
        self.assertIn("Alice CSP", html)
        self.assertNotIn("Bob CSP", html)

    def test_problems_only_filters_out_healthy_csps(self):
        resp = self.client.get("/?problems_only=1")
        html = resp.get_data(as_text=True)
        self.assertIn("Bob CSP", html)   # printer_ok=0 -> a problem
        self.assertNotIn("Alice CSP", html)

    def test_csv_export_matches_filter(self):
        resp = self.client.get("/fleet.csv?q=bob")
        self.assertEqual(resp.content_type, "text/csv; charset=utf-8")
        text = resp.get_data(as_text=True)
        self.assertIn("B1", text)
        self.assertNotIn("A1", text)


class OfflineCspDeviceStatusTests(RouteTestCase):
    """An offline CSP's printer/micro-ATM values are just the last thing it
    reported before going dark - not a live reading. They must not be shown
    (or counted) as a confident OK/Problem."""

    def setUp(self):
        super().setUp()
        self._login()
        import db
        from datetime import datetime, timezone, timedelta
        now = datetime.now(timezone.utc)
        two_days_ago = (now - timedelta(days=2)).isoformat(timespec="seconds")
        fresh = now.isoformat(timespec="seconds")
        with db.get_connection() as conn:
            # Offline for 2 days, last report said micro-ATM OK, printer problem
            conn.execute("""INSERT INTO csps (csp_id, name, printer_configured, printer_present, printer_ok,
                             microatm_configured, microatm_present, microatm_ok, first_seen, last_seen)
                             VALUES ('OFF1','Offline Olga',1,1,0,1,1,1,?,?)""", (two_days_ago, two_days_ago))
            # Online right now, with a genuine live printer problem
            conn.execute("""INSERT INTO csps (csp_id, name, printer_configured, printer_present, printer_ok,
                             microatm_configured, microatm_present, microatm_ok, first_seen, last_seen)
                             VALUES ('ON1','Online Omar',1,1,0,1,1,1,?,?)""", (fresh, fresh))
            conn.commit()

    def test_offline_csps_devices_show_unknown_not_a_stale_ok(self):
        html = self.client.get("/").get_data(as_text=True)
        row = html.split("Offline Olga")[1].split("</tr>")[0]
        self.assertIn("Disconnected", row)
        # The row's own Status badge is red (Offline), so ANY green badge in
        # it would be a stale "OK" leaking through for the micro-ATM.
        self.assertNotIn("badge-green", row)

    def test_offline_csps_stale_problems_do_not_inflate_the_kpi_counts(self):
        html = self.client.get("/").get_data(as_text=True)
        # Only the ONLINE CSP's printer problem counts (1), not the offline one's.
        m = re.search(r'kpi-value">(\d+)</div>\s*<div class="kpi-label">Printer problems', html)
        self.assertEqual(m.group(1), "1")

    def test_problems_only_excludes_offline_csps(self):
        html = self.client.get("/?problems_only=1").get_data(as_text=True)
        self.assertIn("Online Omar", html)
        self.assertNotIn("Offline Olga", html)

    def test_csv_reports_unknown_for_offline_csp_devices(self):
        text = self.client.get("/fleet.csv").get_data(as_text=True)
        olga = [l for l in text.splitlines() if l.startswith("OFF1")][0]
        self.assertIn("UNKNOWN", olga)

    def test_detail_page_labels_offline_device_status_as_unknown(self):
        html = self.client.get("/csp/OFF1").get_data(as_text=True)
        self.assertIn("disconnected - CSP offline", html)


class ApiKeyManagementTests(RouteTestCase):
    def setUp(self):
        super().setUp()
        self._login()

    def _csrf(self):
        page = self.client.get("/api-keys")
        return _extract_csrf(page.get_data(as_text=True))

    def test_issue_key_stores_hash_not_plaintext(self):
        resp = self.client.post("/api-keys", data={
            "csrf_token": self._csrf(), "action": "issue", "csp_id": "NEWCSP", "name": "New CSP"})
        self.assertEqual(resp.status_code, 200)
        self.assertIn("NEWCSP", resp.get_data(as_text=True))
        import db
        with db.get_connection() as conn:
            row = conn.execute("SELECT api_key_hash FROM api_keys WHERE csp_id='NEWCSP'").fetchone()
        self.assertIsNotNone(row)
        self.assertNotIn("NEWCSP", row["api_key_hash"])  # sanity: not a plaintext-derived value

    def test_issue_without_csrf_is_rejected(self):
        resp = self.client.post("/api-keys", data={"action": "issue", "csp_id": "NEWCSP2"})
        self.assertEqual(resp.status_code, 400)

    def test_revoke_then_key_cannot_report(self):
        self.client.post("/api-keys", data={"csrf_token": self._csrf(), "action": "issue", "csp_id": "REVME"})
        import db
        with db.get_connection() as conn:
            row = conn.execute("SELECT active FROM api_keys WHERE csp_id='REVME'").fetchone()
        self.assertEqual(row["active"], 1)

        self.client.post("/api-keys", data={"csrf_token": self._csrf(), "action": "toggle", "csp_id": "REVME"})
        with db.get_connection() as conn:
            row = conn.execute("SELECT active FROM api_keys WHERE csp_id='REVME'").fetchone()
        self.assertEqual(row["active"], 0)

    def test_api_key_actions_write_audit_entries(self):
        self.client.post("/api-keys", data={"csrf_token": self._csrf(), "action": "issue", "csp_id": "AUDITME"})
        self.client.post("/api-keys", data={"csrf_token": self._csrf(), "action": "toggle", "csp_id": "AUDITME"})
        self.client.post("/api-keys", data={"csrf_token": self._csrf(), "action": "delete", "csp_id": "AUDITME"})
        import db
        with db.get_connection() as conn:
            actions = [r["action"] for r in conn.execute(
                "SELECT action FROM admin_audit_log WHERE target_csp='AUDITME' ORDER BY id").fetchall()]
        self.assertEqual(actions, ["api_key_issue", "api_key_revoke", "api_key_delete"])


if __name__ == "__main__":
    unittest.main()
