"""The diagnosis wired into the heartbeat payload and the agent's setup page."""
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import deep_checks
import device_health as dh
import diagnostics as dg

SCN = r"HID\VID_2C0F&PID_1204&MI_00\6&AAAA&0&0000"


def devrow(name="Mantra MFS110", instance=SCN, cm=0, problem="", status="OK"):
    return dh._enrich_device(name, {"Status": status, "Class": "HIDClass", "InstanceId": instance,
                                    "Manufacturer": "Mantra Softech India Pvt Ltd",
                                    "ConfigManagerErrorCode": cm, "ProblemDescription": problem})


class Env(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp()
        os.environ["CSP_AGENT_CONFIG_PATH"] = os.path.join(d, "agent_config.json")
        os.environ["CSP_AGENT_HISTORY_PATH"] = os.path.join(d, "history.json")
        os.environ["CSP_AGENT_IMPACT_MAP"] = os.path.join(d, "none.json")
        import config_store
        config_store.save(csp_id="T1", api_key="k" * 43, server_url="http://x", biometric_name="Mantra MFS110")
        dg.reset_cache()
        deep_checks.reset_state()
        for k in dh._last_good:
            dh._last_good[k] = []
        self.patches = [
            mock.patch.object(dh, "list_printers", return_value=[]),
            mock.patch.object(dh, "process_running", return_value={"configured": False, "running": False}),
            mock.patch.object(deep_checks, "read_nmea", return_value=("UNKNOWN", "")),
            mock.patch.object(deep_checks, "probe_rd_services", return_value=[]),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def scan(self, devices, evidence=None):
        ev = evidence or {"services": [], "drivers": [], "events": [], "printers": [], "jobs": []}
        for p in (mock.patch.object(dh, "list_usb_devices", return_value=devices),
                  mock.patch.object(dh, "list_all_devices", return_value=devices),
                  mock.patch.object(dg, "collect", return_value=ev)):
            p.start()
            self.addCleanup(p.stop)


class PayloadTests(Env):
    def test_the_heartbeat_carries_a_json_serialisable_diagnosis_without_internals(self):
        import reporter
        self.scan([devrow(cm=28, problem="The drivers for this device are not installed. (Code 28).", status="Error")])
        payload = reporter.build_payload(reporter.config_store.load())
        text = json.dumps(payload)                                  # must serialise
        self.assertNotIn('"_row"', text)                            # internal match details stay local
        self.assertEqual(payload["schema_version"], 4)
        bio = payload["diagnosis"]["biometric"]
        self.assertEqual((bio["health_state"], bio["findings"][0]["code"]), ("DRIVER_ISSUE", "CM_28"))
        self.assertEqual(set(payload["diagnosis"]), set(dg.DEVICES))

    def test_the_basic_status_fields_are_still_sent_for_older_servers(self):
        import reporter
        self.scan([devrow()])
        p = reporter.build_payload(reporter.config_store.load())
        for dev in dg.DEVICES:
            for k in ("configured", "present", "ok", "status"):
                self.assertIn(k, p[dev])

    def test_a_diagnosis_failure_never_blocks_the_heartbeat(self):
        import reporter
        self.scan([devrow()])
        with mock.patch.object(dg, "analyze", side_effect=RuntimeError("bug")):
            p = reporter.build_payload(reporter.config_store.load())
        self.assertEqual(p["diagnosis"], {})
        self.assertIn("biometric", p)

    def test_an_oversized_payload_is_trimmed_not_dropped(self):
        import reporter
        big = {"diagnosis": {d: {"findings": [{"evidence": ["e" * 3000] * 40, "actions": ["a" * 3000] * 40}] * 6,
                                 "checked": ["c" * 3000] * 40, "not_checked": [], "notes": []} for d in dg.DEVICES},
               "biometric": {"ok": True}}
        out = reporter._fit(big)
        self.assertLessEqual(len(json.dumps(out)), reporter._MAX_PAYLOAD_BYTES)
        self.assertIn("biometric", out)


class LocalUiTests(Env):
    def setUp(self):
        super().setUp()
        import local_ui
        self.ui = local_ui
        self.client = local_ui.app.test_client()

    def test_status_includes_the_diagnosis_and_the_page_renders_it(self):
        self.scan([devrow(cm=43, problem="Windows has stopped this device because it has reported problems. (Code 43).", status="Error")])
        data = self.client.get("/status").get_json()
        self.assertEqual(data["diagnosis"]["biometric"]["health_state"], "SUSPECTED_HARDWARE")
        html = self.client.get("/").get_data(as_text=True)
        for needle in ("renderDiag", "startService", "biometricDiag", "printerDiag", "gpsDiag", "microatmDiag"):
            self.assertIn(needle, html)

    def test_the_pages_javascript_is_valid(self):
        html = self.client.get("/").get_data(as_text=True)
        js = "\n".join(re.findall(r"<script[^>]*>(.*?)</script>", html, re.S))
        path = os.path.join(tempfile.mkdtemp(), "p.js")
        open(path, "w", encoding="utf-8").write(js)
        try:
            r = subprocess.run(["node", "--check", path], capture_output=True, text=True)
        except FileNotFoundError:
            self.skipTest("node is not installed")
        self.assertEqual(r.returncode, 0, r.stderr)

    def _stopped_service_diagnosis(self):
        svc = {"name": "MantraRDService", "display": "Mantra RD Service", "state": "Stopped", "start": "Auto",
               "exit": 0, "path": r"C:\Program Files\Mantra\rd.exe"}
        self.scan([devrow()], {"services": [svc], "drivers": [], "events": [], "printers": [], "jobs": []})

    def test_fix_starts_only_the_service_the_diagnosis_proposes(self):
        self._stopped_service_diagnosis()
        with mock.patch.object(dg, "_run_start", return_value=(True, "")) as run:
            r = self.client.post("/fix", json={"service": "MantraRDService"}).get_json()
            self.assertTrue(r["ok"])
            run.assert_called_once_with("MantraRDService")
            run.reset_mock()
            r2 = self.client.post("/fix", json={"service": "Spooler"}).get_json()
            self.assertFalse(r2["ok"])
            run.assert_not_called()

    def test_a_failed_fix_is_not_repeated_and_explains_admin_rights(self):
        self._stopped_service_diagnosis()
        with mock.patch.object(dg, "_run_start", return_value=(False, "Access is denied")) as run:
            r1 = self.client.post("/fix", json={"service": "MantraRDService"}).get_json()
            r2 = self.client.post("/fix", json={"service": "MantraRDService"}).get_json()
        self.assertTrue(r1["needs_admin"])
        self.assertIn("not retried", r2["message"])
        self.assertEqual(run.call_count, 1)

    def test_fix_rejects_garbage(self):
        self.scan([devrow()])
        for body in ({}, {"service": ""}, {"service": "a; rm -rf /"}, None):
            r = self.client.post("/fix", json=body)
            self.assertFalse(r.get_json()["ok"])


if __name__ == "__main__":
    unittest.main()
