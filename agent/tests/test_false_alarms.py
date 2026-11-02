"""Field false alarms (agent 2.2.1): a product name typed as the "process name", a micro-ATM that blips off the USB bus
for a moment, Ingenico software installed somewhere the agent did not look, and "Report now" reusing old answers.
No PowerShell runs: scans are mocked."""
import os
import re
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import device_health
import diagnostics as dg


class ProcessNameTests(unittest.TestCase):
    def test_only_a_single_exe_name_is_a_process_name(self):
        for ok in ("MicroATM.exe", "jLLT.exe", "Micro ATM.EXE", "sbi_matm-2.exe"):
            self.assertTrue(device_health.valid_process_name(ok), ok)
        for bad in ("Ingenico LLT, Ingenico USB Drivers", "Ingenico LLT", "jLLT", "a.exe; del x", "", "x.exe.bak"):
            self.assertFalse(device_health.valid_process_name(bad), bad)

    def test_a_product_name_is_ignored_and_never_runs_tasklist(self):
        with mock.patch("device_health.subprocess.run") as run:
            r = device_health.process_running("Ingenico LLT, Ingenico USB Drivers")
        run.assert_not_called()
        self.assertEqual((r["configured"], r["running"], r.get("invalid")), (False, False, True))

    def test_an_ignored_name_does_not_turn_a_working_device_into_a_problem(self):
        hw = {"configured": True, "present": True, "ok": True, "status": "OK"}
        sw = device_health.process_running("Ingenico LLT, Ingenico USB Drivers")
        self.assertEqual(device_health._apply_software_signal(hw, sw), hw)

    def test_no_app_not_running_finding_for_an_ignored_name(self):
        ctx = {"cfg": {"software_process": "Ingenico LLT, Ingenico USB Drivers"},
               "software": device_health.process_running("Ingenico LLT, Ingenico USB Drivers")}
        proc, sw = ctx["cfg"]["software_process"], ctx["software"]
        self.assertFalse(proc and sw.get("configured") and not sw.get("running"))   # the APP_NOT_RUNNING condition


class LocalPageProcessNameTests(unittest.TestCase):
    def setUp(self):
        fd, self.cfg_path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        os.remove(self.cfg_path)
        os.environ["CSP_AGENT_CONFIG_PATH"] = self.cfg_path
        import local_ui
        import config_store
        self.config_store = config_store
        self.client = local_ui.app.test_client()

    def tearDown(self):
        os.environ.pop("CSP_AGENT_CONFIG_PATH", None)
        if os.path.exists(self.cfg_path):
            os.remove(self.cfg_path)

    def test_a_product_name_is_refused_with_a_plain_reason(self):
        r = self.client.post("/configure", json={"software_process": "Ingenico LLT, Ingenico USB Drivers"}).get_json()
        self.assertFalse(r["ok"])
        self.assertIn(".exe", r["error"])
        self.assertEqual(self.config_store.load().get("software_process", ""), "")

    def test_a_real_exe_name_and_a_blank_are_saved(self):
        self.assertTrue(self.client.post("/configure", json={"software_process": "MicroATM.exe"}).get_json()["ok"])
        self.assertEqual(self.config_store.load()["software_process"], "MicroATM.exe")
        self.assertTrue(self.client.post("/configure", json={"software_process": ""}).get_json()["ok"])
        self.assertEqual(self.config_store.load()["software_process"], "")

    def test_report_now_reads_everything_fresh(self):
        import reporter
        dg._cache["software"] = (10 ** 12, ["stale"])
        with mock.patch.object(reporter, "report_once", return_value={"ok": True}):
            self.client.post("/report_now")
        self.assertNotIn("software", dg._cache)


def _h(present=True, ok=None):
    d = {"configured": True, "present": present, "ok": present if ok is None else ok, "status": "x"}
    return {"printer": dict(d), "microatm": dict(d), "biometric": {"configured": False}, "gps": {"configured": False}}


class MissingDeviceRecheckTests(unittest.TestCase):
    def setUp(self):
        import reporter
        self.r = reporter
        reporter._last_presence.clear()
        reporter._last_problem = False

    def test_a_device_that_blips_off_is_checked_again_and_the_newer_scan_wins(self):
        self.r._last_presence.update(printer=True, microatm=True)
        waits = []
        with mock.patch.object(self.r, "_check", side_effect=[_h(present=False), _h(present=True)]) as chk:
            health = self.r.check_confirmed({}, wait=waits.append)
        self.assertEqual(chk.call_count, 2)
        self.assertEqual(waits, [self.r.RECHECK_MISSING_SECONDS])
        self.assertTrue(health["microatm"]["present"])
        self.assertFalse(self.r._last_problem)

    def test_still_missing_after_the_recheck_is_reported_as_missing(self):
        self.r._last_presence.update(printer=True, microatm=True)
        with mock.patch.object(self.r, "_check", side_effect=[_h(present=False), _h(present=False)]):
            health = self.r.check_confirmed({}, wait=lambda s: None)
        self.assertFalse(health["microatm"]["present"])
        self.assertTrue(self.r._last_problem)

    def test_a_device_already_missing_last_time_is_not_waited_for_again(self):
        self.r._last_presence.update(printer=False, microatm=False)
        with mock.patch.object(self.r, "_check", return_value=_h(present=False)) as chk:
            self.r.check_confirmed({}, wait=lambda s: self.fail("should not wait"))
        self.assertEqual(chk.call_count, 1)

    def test_everything_present_means_one_scan_only(self):
        with mock.patch.object(self.r, "_check", return_value=_h(present=True)) as chk:
            self.r.check_confirmed({}, wait=lambda s: self.fail("should not wait"))
        self.assertEqual(chk.call_count, 1)

    def test_while_a_problem_is_open_the_next_report_comes_sooner(self):
        self.assertLess(self.r.PROBLEM_INTERVAL_SECONDS, 300)


class SoftwareDetectionTests(unittest.TestCase):
    def _ps_regex(self, key):
        return re.compile(dg.SOFTWARE_BY_KEY[key]["uninstall"])

    def test_llt_is_recognised_under_the_names_it_ships_with(self):
        rx = self._ps_regex("llt")
        for name in ("Ingenico LLT", "LLT 5.2.4", "LLT", "jLLT", "Local Loader Tool"):
            self.assertTrue(rx.search(name), name)
        for name in ("ALLTEL Suite", "Ingenico USB Drivers", "Bullet"):
            self.assertFalse(rx.search(name), name)

    def test_usb_drivers_are_recognised_under_the_names_they_ship_with(self):
        rx = self._ps_regex("usb_drivers")
        for name in ("Ingenico USB Drivers 3.12", "Ingenico USB Driver", "Ingenico Telium USB", "Telium USB driver"):
            self.assertTrue(rx.search(name), name)
        self.assertFalse(rx.search("Ingenico LLT"))

    def test_llt_is_also_searched_for_under_the_ingenico_folder_and_by_both_program_names(self):
        script = dg._software_script(dg.SOFTWARE_BY_KEY["llt"])
        self.assertIn("-Filter '*LLT*.exe'", script)
        self.assertIn("Join-Path $base 'Ingenico'", script)
        self.assertIn("Get-Process -Name 'jLLT','LLT'", script)

    def test_usb_drivers_are_found_from_the_installed_driver_package_even_with_the_device_unplugged(self):
        script = dg._software_script(dg.SOFTWARE_BY_KEY["usb_drivers"])
        self.assertIn("INF\\oem*.inf", script)
        self.assertIn("Select-String -Pattern 'Ingenico' -List", script)

    def test_unsafe_values_still_refuse_to_build_a_script(self):
        for field, value in (("search", ("Ingenico", "*'; Remove-Item x")), ("inf_provider", "a'b"),
                             ("alt_processes", ("x';y",))):
            bad = dict(dg.SOFTWARE_BY_KEY["llt"], **{field: value})
            self.assertIsNone(dg._software_script(bad), field)


if __name__ == "__main__":
    unittest.main()
