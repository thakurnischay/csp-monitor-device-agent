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

    def test_biometric_scanner_is_never_auto_picked_as_the_microatm(self):
        # Real field bug: a Mantra fingerprint scanner (Aadhaar auth, not a
        # micro-ATM) got silently saved as "the micro-ATM" because its brand
        # used to be in the known-microATM keyword list. A pure biometric
        # device must never be recognized as a micro-ATM candidate, even when
        # it's the only USB device present and the manual pick is blank/wrong.
        usb = [{"name": "Mantra MFS100 Biometric Device", "ok": True, "status": "OK"}]
        self.assertEqual(dh.auto_detect_microatm(usb), "")
        self.assertEqual(dh.resolve_microatm("", usb), "")
        self.assertEqual(dh.resolve_microatm("Some Manual Entry", usb), "Some Manual Entry")

    def test_real_microatm_still_recognized_alongside_a_biometric_scanner(self):
        # Both devices present at once (the common real setup) - the genuine
        # micro-ATM must still be the one that gets recognized.
        usb = [
            {"name": "Mantra MFS100 Biometric Device", "ok": True, "status": "OK"},
            {"name": "Ezetap Micro ATM", "ok": True, "status": "OK"},
        ]
        self.assertEqual(dh.auto_detect_microatm(usb), "Ezetap Micro ATM")

    def test_virtual_enumerator_placeholder_is_never_auto_picked_as_the_microatm(self):
        # Real field bug: "IngenicoEnum" - a software-installed virtual bus/
        # enumerator placeholder that Ingenico's own driver keeps present in
        # Windows even with NOTHING physically plugged in - got auto-picked
        # as "the micro-ATM" and showed OK despite no real device being
        # connected. The "Enum" naming pattern is a placeholder, never a real
        # product, and must never be treated as a genuine candidate.
        usb = [{"name": "IngenicoEnum", "ok": True, "status": "OK"}]
        self.assertEqual(dh.auto_detect_microatm(usb), "")
        self.assertEqual(dh.resolve_microatm("", usb), "")

    def test_manual_pick_of_a_virtual_enumerator_is_corrected_away(self):
        # Same protection as the virtual-printer case: if a placeholder ever
        # ends up manually saved, a real device found alongside it must win.
        usb = [
            {"name": "IngenicoEnum", "ok": True, "status": "OK"},
            {"name": "Ingenico Move 5000", "ok": True, "status": "OK"},
        ]
        self.assertEqual(dh.resolve_microatm("IngenicoEnum", usb), "Ingenico Move 5000")


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


class NamePrefixFallbackTests(unittest.TestCase):
    """Real field case: a CSP typed the printer's physical model label
    ("EPSON PLQ-50 CSK") because it wasn't in the dropdown as typed, but
    Windows had it registered under a different driver name
    ("EPSON PLQ-50 ESC/P2") - a working printer was reported as "not found"
    purely from a naming mismatch, not an actual problem."""

    def test_saved_name_with_different_trailing_word_still_matches(self):
        printers = [{"name": "EPSON PLQ-50 ESC/P2", "ok": True, "status": "idle (ready)"}]
        result = dh.printer_status("EPSON PLQ-50 CSK", printers)
        self.assertTrue(result["present"])
        self.assertTrue(result["ok"])

    def test_com_port_renumbering_still_matches_the_same_printer(self):
        # The exact scenario the prefix match also covers: a COM-port
        # printer's port number shifting after a replug/driver reset.
        printers = [{"name": "TVS RP3200 (COM6)", "ok": True, "status": "idle (ready)"}]
        result = dh.printer_status("TVS RP3200 (COM4)", printers)
        self.assertTrue(result["present"])

    def test_unrelated_device_on_the_same_port_is_never_mistaken_for_the_printer(self):
        # Safety check: the fallback matches on the device's own name text,
        # never on the port number alone - a completely different device
        # that happens to land on the same COM port must still report
        # "not found", not be silently treated as the passbook printer.
        printers = [{"name": "USB-SERIAL CH340 (COM4)", "ok": True, "status": "OK"}]
        result = dh.printer_status("TVS RP3200 (COM4)", printers)
        self.assertFalse(result["present"])

    def test_ambiguous_prefix_match_is_never_guessed(self):
        # Two present devices share the fallback prefix - must still refuse,
        # same "never guess" rule as the exact-match ambiguous case.
        printers = [
            {"name": "EPSON PLQ-50 ESC/P2", "ok": True, "status": "idle (ready)"},
            {"name": "EPSON PLQ-50 USB", "ok": True, "status": "idle (ready)"},
        ]
        result = dh.printer_status("EPSON PLQ-50 CSK", printers)
        self.assertFalse(result["present"])

    def test_single_generic_word_prefix_is_too_permissive_to_match(self):
        # A one-word remainder (e.g. just the brand name) is excluded from
        # the fallback entirely - too loose to safely match on.
        printers = [{"name": "EPSON L3210", "ok": True, "status": "idle (ready)"}]
        result = dh.printer_status("EPSON XYZ", printers)
        self.assertFalse(result["present"])

    def test_microatm_gets_the_same_fallback(self):
        usb = [{"name": "Ingenico Move iCT220", "ok": True, "status": "OK"}]
        result = dh.microatm_status("Ingenico Move 5000", usb)
        self.assertTrue(result["present"])


class BiometricAndGpsTests(unittest.TestCase):
    """Fingerprint scanner and USB GPS dongle detection, added alongside the
    printer/micro-ATM checks. Same rules: recognize by vendor/name only when
    unambiguous, never pick a software placeholder, never guess."""

    def test_known_fingerprint_scanner_is_recognized(self):
        usb = [{"name": "Mantra MFS100 Biometric Device", "ok": True, "status": "OK"}]
        self.assertEqual(dh.auto_detect_biometric(usb), "Mantra MFS100 Biometric Device")

    def test_laptop_builtin_reader_is_not_mistaken_for_the_scanner(self):
        usb = [{"name": "Synaptics UWP WBDI", "ok": True, "status": "OK"}]
        self.assertEqual(dh.auto_detect_biometric(usb), "")

    def test_a_micro_atm_is_never_picked_as_the_scanner(self):
        usb = [{"name": "Ingenico Move 5000", "ok": True, "status": "OK"}]
        self.assertEqual(dh.auto_detect_biometric(usb), "")

    def test_vendor_driver_placeholder_is_never_picked_as_the_scanner(self):
        usb = [{"name": "MantraEnum", "ok": True, "status": "OK"}]
        self.assertEqual(dh.auto_detect_biometric(usb), "")

    def test_gps_dongle_with_gps_in_its_name_is_recognized(self):
        devs = [{"name": "u-blox 7 - GPS/GNSS Receiver", "ok": True, "status": "OK"}]
        self.assertEqual(dh.auto_detect_gps(devs), "u-blox 7 - GPS/GNSS Receiver")

    def test_generic_usb_serial_dongle_is_not_guessed_as_gps(self):
        # Needs a human's one-time pick - a generic serial chip could equally
        # be a COM-port printer.
        devs = [{"name": "USB-SERIAL CH340 (COM3)", "ok": True, "status": "OK"}]
        self.assertEqual(dh.auto_detect_gps(devs), "")

    def test_a_working_manual_pick_is_never_overridden(self):
        devs = [{"name": "USB-SERIAL CH340 (COM3)", "ok": True, "status": "OK"},
                {"name": "u-blox 7 - GPS/GNSS Receiver", "ok": True, "status": "OK"}]
        self.assertEqual(dh.resolve_gps("USB-SERIAL CH340 (COM3)", devs), "USB-SERIAL CH340 (COM3)")

    def test_a_missing_manual_pick_is_replaced_by_a_confident_recognition(self):
        devs = [{"name": "Mantra MFS100 Biometric Device", "ok": True, "status": "OK"}]
        self.assertEqual(dh.resolve_biometric("Old Scanner Name", devs), "Mantra MFS100 Biometric Device")

    def test_status_not_configured_present_and_missing(self):
        devs = [{"name": "Mantra MFS100 Biometric Device", "ok": True, "status": "OK"}]
        self.assertFalse(dh.biometric_status("", devs)["configured"])
        self.assertTrue(dh.biometric_status("Mantra MFS100 Biometric Device", devs)["ok"])
        gone = dh.gps_status("u-blox 7 - GPS/GNSS Receiver", [])
        self.assertTrue(gone["configured"])
        self.assertFalse(gone["present"])

    def test_check_reports_both_devices_from_the_existing_scans(self):
        with mock.patch.object(dh, "list_printers", return_value=[]),              mock.patch.object(dh, "list_usb_devices", return_value=[
                 {"name": "Mantra MFS100 Biometric Device", "ok": True, "status": "OK"}]),              mock.patch.object(dh, "list_all_devices", return_value=[
                 {"name": "u-blox GNSS Location Sensor", "class": "Sensor", "ok": True, "status": "OK"}]),              mock.patch.object(dh, "process_running", return_value={"configured": False, "running": False}):
            result = dh.check("", "", "")
        self.assertTrue(result["biometric"]["present"])
        self.assertEqual(result["biometric"]["resolved_name"], "Mantra MFS100 Biometric Device")
        self.assertTrue(result["gps"]["present"])
        self.assertEqual(result["gps"]["resolved_name"], "u-blox GNSS Location Sensor")
        self.assertIn("u-blox GNSS Location Sensor", [d["name"] for d in result["gps_device_list"]])

    def test_a_com_port_device_is_offered_in_the_gps_dropdown(self):
        with mock.patch.object(dh, "list_printers", return_value=[]),              mock.patch.object(dh, "list_usb_devices", return_value=[]),              mock.patch.object(dh, "list_all_devices", return_value=[
                 {"name": "USB-SERIAL CH340 (COM3)", "class": "Ports", "ok": True, "status": "OK"}]),              mock.patch.object(dh, "process_running", return_value={"configured": False, "running": False}):
            result = dh.check("", "", "")
        self.assertIn("USB-SERIAL CH340 (COM3)", [d["name"] for d in result["gps_device_list"]])
        self.assertEqual(result["gps"]["resolved_name"], "")  # not auto-picked

    def test_timeout_fallback_still_returns_every_key_callers_read(self):
        with mock.patch.object(dh, "_CHECK_HARD_TIMEOUT", 1),              mock.patch.object(dh, "list_printers", side_effect=lambda: time.sleep(30)):
            result = dh.check("", "", "")
        for key in ("printer", "microatm", "biometric", "gps", "software",
                    "printer_list", "usb_device_list", "biometric_device_list", "gps_device_list"):
            self.assertIn(key, result)
        self.assertIn("timed out", result["biometric"]["status"])


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

    def test_unknown_status_is_not_treated_as_a_problem(self):
        # Real field case: a printer that worked fine (confirmed by the CSP
        # and by the separate connectivity check) still showed "Test print:
        # problem - UNKNOWN". Win32_PrintJob's generic Status property
        # defaults to "Unknown" for plenty of successfully-printed jobs, not
        # just failed ones - it must not be treated as a specific error like
        # "Paper Out" or "Door Open".
        with mock.patch("subprocess.run"), \
             mock.patch.object(dh, "_run_ps", return_value=[{"Name": "TVS RP3200,5", "Status": "Unknown"}]), \
             mock.patch("time.sleep"):
            result = dh.printer_functional_test("TVS RP3200")
        self.assertTrue(result["ran"])
        self.assertTrue(result["ok"])


if __name__ == "__main__":
    unittest.main()
