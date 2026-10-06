"""The agent's localhost setup page - the biometric/GPS controls added next to
the printer/micro-ATM ones. Device scanning is mocked; no PowerShell runs."""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _health():
    ok = {"configured": True, "present": True, "ok": True, "status": "OK"}
    return {
        "printer": dict(ok, resolved_name="P"),
        "microatm": dict(ok, resolved_name="M"),
        "biometric": dict(ok, resolved_name="Mantra MFS100 Biometric Device"),
        "gps": dict(ok, resolved_name="u-blox 7 - GPS/GNSS Receiver"),
        "software": {"configured": False, "running": False},
        "printer_list": [{"name": "P", "ok": True, "status": "idle (ready)"}],
        "usb_device_list": [{"name": "M", "ok": True, "status": "OK"}],
        "biometric_device_list": [
            {"name": "Mantra MFS100 Biometric Device", "ok": True, "status": "OK"},
            {"name": "MantraEnum", "ok": True, "status": "OK"}],
        "gps_device_list": [{"name": "u-blox 7 - GPS/GNSS Receiver", "ok": True, "status": "OK"}],
    }


class LocalUiTests(unittest.TestCase):
    def setUp(self):
        fd, self.cfg_path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        os.remove(self.cfg_path)
        os.environ["CSP_AGENT_CONFIG_PATH"] = self.cfg_path
        import local_ui
        self.local_ui = local_ui
        self.client = local_ui.app.test_client()

    def tearDown(self):
        try:
            os.remove(self.cfg_path)
        except OSError:
            pass

    def test_page_has_the_biometric_and_gps_controls(self):
        html = self.client.get("/").get_data(as_text=True)
        for needle in ("biometricSelect", "gpsSelect", "biometricBadge", "gpsBadge",
                       "biometric_name:", "gps_name:"):
            self.assertIn(needle, html)

    def test_status_reports_both_new_devices_and_hides_driver_placeholders(self):
        with mock.patch.object(self.local_ui.device_health, "check", return_value=_health()):
            data = self.client.get("/status").get_json()
        self.assertTrue(data["biometric"]["ok"])
        self.assertTrue(data["gps"]["ok"])
        self.assertEqual(data["available_biometric_devices"], ["Mantra MFS100 Biometric Device"])
        self.assertEqual(data["available_gps_devices"], ["u-blox 7 - GPS/GNSS Receiver"])
        # Recognized devices self-configure, same as printer/micro-ATM.
        self.assertEqual(data["config"]["biometric_name"], "Mantra MFS100 Biometric Device")
        self.assertEqual(data["config"]["gps_name"], "u-blox 7 - GPS/GNSS Receiver")

    def test_configure_saves_and_can_clear_the_new_selections(self):
        self.client.post("/configure", json={"biometric_name": " Mantra X ", "gps_name": "GPS Y"})
        import config_store
        cfg = config_store.load()
        self.assertEqual(cfg["biometric_name"], "Mantra X")   # whitespace trimmed
        self.assertEqual(cfg["gps_name"], "GPS Y")
        self.client.post("/configure", json={"gps_name": ""})  # explicit blank clears
        self.assertEqual(config_store.load()["gps_name"], "")
        self.assertEqual(config_store.load()["biometric_name"], "Mantra X")


if __name__ == "__main__":
    unittest.main()
