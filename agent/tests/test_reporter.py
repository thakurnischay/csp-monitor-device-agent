"""Unit tests for reporter.py - heartbeat payload shape, bounded retry, and
the once-a-day test-print gate."""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests


def _fresh_config_env():
    tmpdir = tempfile.mkdtemp()
    os.environ["CSP_AGENT_CONFIG_PATH"] = os.path.join(tmpdir, "agent_config.json")


class BuildPayloadTests(unittest.TestCase):
    def setUp(self):
        _fresh_config_env()
        import config_store
        import device_health
        import reporter
        self.config_store = config_store
        self.device_health = device_health
        self.reporter = reporter
        self.config_store.save(csp_id="T1", api_key="x")

    def test_payload_includes_version_and_host_metadata(self):
        with mock.patch.object(self.device_health, "list_printers", return_value=[]), \
             mock.patch.object(self.device_health, "list_usb_devices", return_value=[]), \
             mock.patch.object(self.device_health, "list_all_devices", return_value=[]), \
             mock.patch.object(self.device_health, "process_running", return_value={"configured": False, "running": False}):
            payload = self.reporter.build_payload(self.config_store.load())
        self.assertEqual(payload["schema_version"], 3)
        self.assertTrue(payload["agent_version"])
        self.assertTrue(payload["hostname"])
        self.assertTrue(payload["os"])
        self.assertIn("printer", payload)
        self.assertIn("microatm", payload)
        self.assertIn("biometric", payload)
        self.assertIn("gps", payload)
        self.assertIn("printer_functional_test", payload)


class BoundedRetryTests(unittest.TestCase):
    def setUp(self):
        _fresh_config_env()
        import config_store
        import device_health
        import reporter
        self.config_store = config_store
        self.device_health = device_health
        self.reporter = reporter
        self.config_store.save(csp_id="T1", api_key="x", server_url="http://fake-server:9999")
        self._hw_patches = [
            mock.patch.object(device_health, "list_printers", return_value=[]),
            mock.patch.object(device_health, "list_usb_devices", return_value=[]),
            mock.patch.object(device_health, "list_all_devices", return_value=[]),
            mock.patch.object(device_health, "process_running", return_value={"configured": False, "running": False}),
        ]
        for p in self._hw_patches:
            p.start()
            self.addCleanup(p.stop)
        for k in device_health._last_good:      # no devices leaked in from other tests
            device_health._last_good[k] = []

    def test_retries_exactly_once_on_persistent_connection_failure(self):
        call_count = {"n": 0}

        def fake_post(*a, **k):
            call_count["n"] += 1
            raise requests.exceptions.ConnectionError("simulated")

        with mock.patch("reporter.requests.post", side_effect=fake_post), \
             mock.patch("reporter.time.sleep") as sleep_mock:
            result = self.reporter.report_once()
        self.assertEqual(call_count["n"], 2)
        self.assertFalse(result["ok"])
        sleep_mock.assert_called_once_with(self.reporter._RETRY_DELAY_SECONDS)

    def test_a_401_is_never_retried(self):
        call_count = {"n": 0}

        class FakeResp:
            status_code = 401

        def fake_post(*a, **k):
            call_count["n"] += 1
            return FakeResp()

        with mock.patch("reporter.requests.post", side_effect=fake_post):
            result = self.reporter.report_once()
        self.assertEqual(call_count["n"], 1)
        self.assertIn("401", result["error"])

    def test_success_on_the_retry_still_reports_ok(self):
        call_count = {"n": 0}

        class FakeResp:
            status_code = 200
            def raise_for_status(self):
                pass

        def fake_post(*a, **k):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise requests.exceptions.ConnectionError("first attempt fails")
            return FakeResp()

        with mock.patch("reporter.requests.post", side_effect=fake_post), \
             mock.patch("reporter.time.sleep"):
            result = self.reporter.report_once()
        self.assertEqual(call_count["n"], 2)
        self.assertTrue(result["ok"])

    def test_split_timeout_tuple_is_used_not_a_single_scalar(self):
        captured = {}

        class FakeResp:
            status_code = 200
            def raise_for_status(self):
                pass

        def fake_post(*a, **k):
            captured["timeout"] = k.get("timeout")
            return FakeResp()

        with mock.patch("reporter.requests.post", side_effect=fake_post):
            self.reporter.report_once()
        self.assertEqual(captured["timeout"], (5, 15))


class FunctionalTestGatingTests(unittest.TestCase):
    def setUp(self):
        _fresh_config_env()
        import config_store
        import reporter
        self.config_store = config_store
        self.reporter = reporter

    def test_runs_once_then_caches_for_the_rest_of_the_day(self):
        call_count = {"n": 0}

        def fake_test(name):
            call_count["n"] += 1
            return {"ran": True, "ok": True, "detail": "run #%d" % call_count["n"]}

        with mock.patch.object(self.reporter.device_health, "printer_functional_test", side_effect=fake_test):
            r1 = self.reporter._maybe_run_printer_functional_test(self.config_store.load(), "TVS RP3200", True)
            r2 = self.reporter._maybe_run_printer_functional_test(self.config_store.load(), "TVS RP3200", True)
        self.assertEqual(call_count["n"], 1)
        self.assertEqual(r1, r2)

    def test_never_attempted_when_printer_not_present(self):
        with mock.patch.object(self.reporter.device_health, "printer_functional_test") as test_mock:
            result = self.reporter._maybe_run_printer_functional_test(self.config_store.load(), "TVS RP3200", False)
        test_mock.assert_not_called()
        self.assertFalse(result["ran"])

    def test_never_sends_a_test_page_to_a_com_port_or_usb_entry(self):
        """Windows pops an error box (0x00000709) on the CSP's screen if a test
        page is sent to a name that is not an installed Windows printer."""
        with mock.patch.object(self.reporter.device_health, "printer_functional_test") as test_mock:
            result = self.reporter._maybe_run_printer_functional_test(
                self.config_store.load(), "USB-SERIAL CH340 (COM3)", True, is_windows_printer=False)
        test_mock.assert_not_called()
        self.assertFalse(result["ran"])
        self.assertIn("not an installed Windows printer", result["detail"])

    def test_never_sends_a_test_page_to_a_software_printer(self):
        """A PC with no real printer: PDF / AnyDesk / OneNote etc. always look
        present and ready, and printing to them pops dialogs on the screen."""
        for name in ("Microsoft Print to PDF", "AnyDesk Printer", "RustDesk Printer",
                     "OneNote (Desktop)", "Fax"):
            with mock.patch.object(self.reporter.device_health, "printer_functional_test") as test_mock:
                result = self.reporter._maybe_run_printer_functional_test(
                    self.config_store.load(), name, True, True, True)
            test_mock.assert_not_called()
            self.assertFalse(result["ran"], name)
            self.assertIn("software printer", result["detail"])

    def test_never_sends_a_test_page_to_a_printer_in_an_error_state(self):
        with mock.patch.object(self.reporter.device_health, "printer_functional_test") as test_mock:
            result = self.reporter._maybe_run_printer_functional_test(
                self.config_store.load(), "TVS RP3200", True, True, printer_ok=False)
        test_mock.assert_not_called()
        self.assertIn("not ready", result["detail"])

    def test_the_skip_is_not_cached_so_a_later_good_state_still_gets_its_daily_test(self):
        with mock.patch.object(self.reporter.device_health, "printer_functional_test",
                               return_value={"ran": True, "ok": True, "detail": "x"}) as test_mock:
            self.reporter._maybe_run_printer_functional_test(
                self.config_store.load(), "TVS RP3200", True, True, printer_ok=False)
            self.reporter._maybe_run_printer_functional_test(
                self.config_store.load(), "TVS RP3200", True, True, printer_ok=True)
        self.assertEqual(test_mock.call_count, 1)


if __name__ == "__main__":
    unittest.main()
