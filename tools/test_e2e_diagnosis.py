"""END TO END: the real agent code (only Windows' answers are mocked) sends a real
heartbeat over HTTP to the real server code, and the real dashboard pages are read
back. Covers a driver failure, a stopped service, an approved fix and verified recovery."""
import html as htmllib
import os
import re
import sys
import tempfile
import threading
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SERVER_DIR, AGENT_DIR = os.path.join(ROOT, "server"), os.path.join(ROOT, "agent")
# both projects have a package called "tests": load the SERVER's helpers first, then
# put the agent's modules on the path (the agent code never imports "tests")
sys.path.insert(0, SERVER_DIR)
from tests.helpers import make_test_app, issue_test_key        # noqa: E402
from tests.test_routes import ADMIN_PASSWORD                    # noqa: E402
sys.path.insert(0, AGENT_DIR)

SCN = r"HID\VID_2C0F&PID_1204&MI_00\6&AAAA&0&0000"


class E2E(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app, cls.db_path = make_test_app()
        cls.key = issue_test_key("E2E1", "End To End CSP")
        from werkzeug.serving import make_server
        cls.httpd = make_server("127.0.0.1", 0, cls.app, threaded=True)
        cls.url = f"http://127.0.0.1:{cls.httpd.server_port}/"
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.web = cls.app.test_client()
        page = cls.web.get("/login").get_data(as_text=True)
        token = re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)
        cls.web.post("/login", data={"csrf_token": token, "login_id": "admin", "password": ADMIN_PASSWORD})

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()

    def setUp(self):
        d = tempfile.mkdtemp()
        os.environ["CSP_AGENT_CONFIG_PATH"] = os.path.join(d, "agent_config.json")
        os.environ["CSP_AGENT_HISTORY_PATH"] = os.path.join(d, "history.json")
        os.environ["CSP_AGENT_IMPACT_MAP"] = os.path.join(d, "none.json")
        import config_store
        import deep_checks
        import device_health as dh
        import diagnostics as dg
        import reporter
        self.dh, self.dg, self.reporter = dh, dg, reporter
        config_store.save(csp_id="E2E1", api_key=self.key, server_url=self.url, biometric_name="Mantra MFS110")
        dg.reset_cache()
        deep_checks.reset_state()
        for k in dh._last_good:
            dh._last_good[k] = []
        self._reset_db()
        for p in (mock.patch.object(dh, "list_printers", return_value=[]),
                  mock.patch.object(dh, "process_running", return_value={"configured": False, "running": False}),
                  mock.patch.object(deep_checks, "read_nmea", return_value=("UNKNOWN", "")),
                  mock.patch.object(deep_checks, "probe_rd_services", side_effect=lambda: list(self.rd_answers))):
            p.start()
            self.addCleanup(p.stop)
        # the scanner's RD service answers READY unless a test says otherwise
        self.rd_answers = [{"status": "READY", "info": "Mantra Authentication Vendor Device Service", "port": 11100}]

    @staticmethod
    def _reset_db():
        import db
        from security import report_limiter
        report_limiter.reset()
        with db.get_connection() as conn:
            for table in ("device_issues", "device_diagnosis", "events", "incidents"):
                conn.execute(f"DELETE FROM {table}")
            conn.commit()

    def agent_reports(self, devices, services=()):
        """Run the REAL agent once against Windows' (mocked) answers."""
        from security import report_limiter
        report_limiter.reset()
        ev = {"services": list(services), "drivers": [], "events": [], "printers": [], "jobs": []}
        with mock.patch.object(self.dh, "list_usb_devices", return_value=devices), \
                mock.patch.object(self.dh, "list_all_devices", return_value=devices), \
                mock.patch.object(self.dg, "collect", return_value=ev):
            result = self.reporter.report_once()
        self.assertTrue(result["ok"], result)

    @staticmethod
    def dev(cm=0, problem="", status="OK"):
        from device_health import _enrich_device
        return _enrich_device("Mantra MFS110", {"Status": status, "Class": "HIDClass", "InstanceId": SCN,
                                                "Manufacturer": "Mantra Softech India Pvt Ltd",
                                                "ConfigManagerErrorCode": cm, "ProblemDescription": problem})

    def page(self, path="/csp/E2E1"):
        return self.web.get(path).get_data(as_text=True)

    @staticmethod
    def text(html):
        return re.sub(r"\s+", " ", htmllib.unescape(re.sub(r"<[^>]+>", " ", html)))

    def issues(self):
        import db
        with db.get_connection() as conn:
            return [dict(r) for r in conn.execute("SELECT code, status, severity, state FROM device_issues ORDER BY id")]

    def events(self):
        import db
        with db.get_connection() as conn:
            return [r["event_type"] for r in conn.execute("SELECT event_type FROM events")]

    def test_a_driver_failure_travels_from_windows_to_the_dashboard_and_recovery_is_verified(self):
        # 1. Windows reports "drivers not installed" for the scanner
        self.agent_reports([self.dev(28, "The drivers for this device are not installed. (Code 28).", "Error")])
        page = self.text(self.page())
        for needle in ("Driver issue", "Windows reports error code 28 for", "The drivers for this device are not installed",
                       "No driver is installed for this device", "Confirmed: Windows or the device itself reports this",
                       "vendor-approved driver", "Aadhaar-based transactions (AePS)", "assumed - not yet confirmed",
                       "Not calculable", "Operator action required"):
            self.assertIn(needle, page, needle)
        self.assertEqual(self.issues(), [{"code": "CM_28", "status": "OPEN", "severity": "high", "state": "DRIVER_ISSUE"}])
        self.assertIn("Driver issue", self.text(self.web.get("/").get_data(as_text=True)))
        # 2. the operator installs the driver; the next heartbeat is clean
        self.agent_reports([self.dev()])
        self.assertEqual(self.issues()[0]["status"], "RESOLVED")
        page = self.text(self.page())
        self.assertIn("Recovery verified", page)
        self.assertNotIn("Operator action required", page)
        self.assertIn("biometric_recovery_verified", self.events())

    def test_a_stopped_scanner_service_is_not_a_missing_device_and_the_approved_fix_is_verified(self):
        svc = {"name": "MantraRDService", "display": "Mantra RD Service", "state": "Stopped", "start": "Auto",
               "exit": 0, "path": r"C:\Program Files\Mantra\rd.exe"}
        self.rd_answers = []                                       # a stopped service answers nothing
        self.agent_reports([self.dev()], services=[svc])
        page = self.text(self.page())
        self.assertIn("Service unavailable", page)
        self.assertIn("'Mantra RD Service' is stopped", page)
        self.assertIn("Do NOT reconnect the fingerprint scanner", page)
        self.assertIn("Operator approval needed", page)
        self.assertNotIn("is not detected", page)                  # the device itself is fine
        # the operator approves on the agent page; the service starts (Windows call mocked)
        import config_store
        ev = {"services": [svc], "drivers": [], "events": [], "printers": [], "jobs": []}
        with mock.patch.object(self.dh, "list_usb_devices", return_value=[self.dev()]), \
                mock.patch.object(self.dh, "list_all_devices", return_value=[self.dev()]):
            health = self.dh.apply_verification(
                self.dh.check("", "", "", "Mantra MFS110", "", {}), {}, {})
        diagnosis = self.dg.analyze(health, config_store.load(), None, evidence=ev)
        with mock.patch.object(self.dg, "_run_start", return_value=(True, "")):
            self.assertTrue(self.dg.start_service("MantraRDService", diagnosis)["ok"])
        self.rd_answers = [{"status": "READY", "info": "Mantra", "port": 11100}]     # it answers again
        self.agent_reports([self.dev()], services=[dict(svc, state="Running")])
        self.assertEqual(self.issues()[0]["status"], "RESOLVED")
        self.assertIn("Recovery verified", self.text(self.page()))

    def test_a_healthy_scanner_shows_no_fault_and_states_its_limits(self):
        self.agent_reports([self.dev()])
        page = self.text(self.page())
        self.assertIn("Healthy", page)
        self.assertEqual(self.issues(), [])
        self.assertIn("A live fingerprint capture (never done", page)


if __name__ == "__main__":
    unittest.main()
