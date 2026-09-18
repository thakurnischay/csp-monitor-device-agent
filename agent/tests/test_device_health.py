"""Unit tests for device_health.py - the recognition engine, timeout
protection, and COM-port/USB merging. These codify scenarios that were
previously only verified via throwaway scripts during development; they now
persist as real regression tests.

IMPORTANT: preserves and never weakens the actual detection logic - these
tests only exercise the existing code, they don't change its behavior.
"""
import os
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import device_health as dh


class AutoCorrectionTests(unittest.TestCase):
    def test_wrong_virtual_printer_pick_is_corrected_to_the_real_device(self):
        printers = [
            {"name": "Microsoft Print to PDF", "ok": True, "status": "idle (ready)"},
            {"name": "TVS RP3200 Passbook Printer", "ok": True, "status": "idle (ready)"},
        ]
        self.assertEqual(dh.resolve_printer("Microsoft Print to PDF", printers), "TVS RP3200 Passbook Printer")

    def test_already_correct_pick_is_left_alone(self):
        printers = [{"name": "TVS RP3200 Passbook Printer", "ok": True, "status": "idle (ready)"}]
        self.assertEqual(dh.resolve_printer("TVS RP3200 Passbook Printer", printers), "TVS RP3200 Passbook Printer")

    def test_blank_manual_pick_auto_fills_a_recognized_brand(self):
        printers = [{"name": "Epson PLQ-50CSK", "ok": True, "status": "idle (ready)"}]
        self.assertEqual(dh.resolve_printer("", printers), "Epson PLQ-50CSK")

    def test_ambiguous_match_falls_back_to_the_manual_pick(self):
        # Two known-brand printers present at once - must never guess.
        printers = [
            {"name": "TVS RP3200 Passbook Printer", "ok": True, "status": "idle (ready)"},
            {"name": "WEP BP204 Passbook Printer", "ok": True, "status": "idle (ready)"},
        ]
        self.assertEqual(dh.resolve_printer("WEP BP204 Passbook Printer", printers), "WEP BP204 Passbook Printer")

    def test_unrecognized_brand_is_left_untouched(self):
        printers = [{"name": "Some Random Printer XYZ", "ok": True, "status": "idle (ready)"}]
        self.assertEqual(dh.resolve_printer("Some Random Printer XYZ", printers), "Some Random Printer XYZ")

    def test_microatm_auto_correction(self):
        usb = [{"name": "Ingenico iCT220 Micro ATM", "ok": True, "status": "OK"}]
        self.assertEqual(dh.resolve_microatm("Wrong Manual Entry", usb), "Ingenico iCT220 Micro ATM")


class ComPortMergeTests(unittest.TestCase):
    """A printer that never registers as a real Windows printer, only as a
    raw COM-port device, must still be detectable - this was the root cause
    of a real field issue (Bhupendra Prasad's printer)."""

    def test_com_port_device_appears_as_a_printer_candidate_even_when_unrecognized(self):
        with mock.patch.object(dh, "list_printers", return_value=[]), \
             mock.patch.object(dh, "list_usb_devices", return_value=[]), \
             mock.patch.object(dh, "list_all_devices", return_value=[
                 {"name": "USB-SERIAL CH340 (COM4)", "class": "Ports", "ok": True, "status": "OK"}]), \
             mock.patch.object(dh, "process_running", return_value={"configured": False, "running": False}):
            result = dh.check("", "", "")
        printer_names = [p["name"] for p in result["printer_list"]]
        self.assertIn("USB-SERIAL CH340 (COM4)", printer_names)
        self.assertEqual(result["printer"]["resolved_name"], "")  # unrecognized -> not auto-picked

    def test_recognized_com_port_printer_is_auto_detected(self):
        with mock.patch.object(dh, "list_printers", return_value=[]), \
             mock.patch.object(dh, "list_usb_devices", return_value=[]), \
             mock.patch.object(dh, "list_all_devices", return_value=[
                 {"name": "TVS RP3200 (COM4)", "class": "Ports", "ok": True, "status": "OK"}]), \
             mock.patch.object(dh, "process_running", return_value={"configured": False, "running": False}):
            result = dh.check("", "", "")
        self.assertEqual(result["printer"]["resolved_name"], "TVS RP3200 (COM4)")
        self.assertTrue(result["printer"]["present"])


class TimeoutProtectionTests(unittest.TestCase):
    def test_run_with_ceiling_bounds_a_hanging_function(self):
        def hangs_forever(seconds):
            time.sleep(seconds)
            return "should never arrive in time"

        t0 = time.time()
        result = dh.run_with_ceiling(hangs_forever, 1, 30)
        elapsed = time.time() - t0
        self.assertIsNone(result)
        self.assertLess(elapsed, 3, "run_with_ceiling did not bound the hang")

    def test_check_reports_a_clear_status_when_the_scan_itself_times_out(self):
        def hanging_list_printers():
            time.sleep(30)
            return []

        with mock.patch.object(dh, "_CHECK_HARD_TIMEOUT", 1), \
             mock.patch.object(dh, "list_printers", side_effect=hanging_list_printers):
            t0 = time.time()
            result = dh.check("SomePrinter", "SomeATM", "")
            elapsed = time.time() - t0
        self.assertLess(elapsed, 3)
        self.assertIn("timed out", result["printer"]["status"])
        self.assertIn("timed out", result["microatm"]["status"])


class ScanEfficiencyTests(unittest.TestCase):
    """A real regression: auto-detection used to trigger 3 separate scans
    per check() call, which made the local setup page look frozen on a real
    (slow/AV-laden) CSP PC. Must stay at exactly 1 scan per device type."""

    def test_check_scans_each_device_type_exactly_once(self):
        with mock.patch.object(dh, "_run_ps") as run_ps_mock:
            def fake_run_ps(script, timeout=8):
                if "Win32_Printer" in script:
                    return []
                if "Class, Status" in script:
                    return []
                if "PnpDevice" in script:
                    return []
                return None
            run_ps_mock.side_effect = fake_run_ps
            dh.check("", "", "")
        self.assertEqual(run_ps_mock.call_count, 3)  # printers + usb + all_devices, each exactly once


class FunctionalTestTests(unittest.TestCase):
    def test_blank_printer_name_never_attempts_a_test_print(self):
        result = dh.printer_functional_test("")
        self.assertFalse(result["ran"])

    def test_reported_problem_status_is_surfaced(self):
        with mock.patch("subprocess.run"), \
             mock.patch.object(dh, "_run_ps", return_value=[{"Name": "TVS RP3200,5", "Status": "Paper Out"}]), \
             mock.patch("time.sleep"):
            result = dh.printer_functional_test("TVS RP3200")
        self.assertTrue(result["ran"])
        self.assertFalse(result["ok"])
        self.assertIn("Paper Out", result["detail"])

    def test_subprocess_failure_never_raises(self):
        with mock.patch("subprocess.run", side_effect=Exception("boom")):
            result = dh.printer_functional_test("TVS RP3200")
        self.assertFalse(result["ran"])


if __name__ == "__main__":
    unittest.main()
