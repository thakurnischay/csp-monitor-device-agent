"""RD-service and GPS-NMEA deeper checks (deep_checks.py). No real hardware or
sockets: probes/reads are mocked."""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import deep_checks as dc
import device_health as dh

BIO_OK = {"configured": True, "present": True, "ok": True, "status": "OK",
          "resolved_name": "Mantra MFS100", "recognised": True}
GPS_OK = {"configured": True, "present": True, "ok": True, "status": "OK",
          "resolved_name": "u-blox 7 (COM5)", "recognised": True}
# a device picked by hand that nothing identifies as the right kind of hardware
BIO_GENERIC = dict(BIO_OK, resolved_name="USB Input Device", recognised=False)
GPS_GENERIC = dict(GPS_OK, resolved_name="USB Serial Device (COM5)", recognised=False)
NOTHING_PICKED = {"configured": False, "present": False, "ok": False, "status": "not configured"}

GGA_FIX = "$GPGGA,123519,4807.038,N,01131.000,E,1,08,0.9,545.4,M,46.9,M,,*47\r\n"
GGA_NOFIX = "$GPGGA,123519,,,,,0,00,,,M,,M,,*66\r\n$GPRMC,123519,V,,,,,,,230394,,*3B\r\n"


class RdParsingTests(unittest.TestCase):
    def test_ready(self):
        r = dc.parse_rd_response('<RDService status="READY" info="Mantra Authentication Vendor Device Service">'
                                 '<Interface id="CAPTURE" path="/rd/capture"/></RDService>')
        self.assertEqual(r, {"status": "READY", "info": "Mantra Authentication Vendor Device Service"})

    def test_notready_lowercase_attribute_names(self):
        self.assertEqual(dc.parse_rd_response('<rdservice STATUS="NOTREADY" info="x"></rdservice>')["status"], "NOTREADY")

    def test_non_rd_replies_are_ignored(self):
        for text in ("", None, "<html>hello</html>", "<RDService info='x'/>"):
            self.assertIsNone(dc.parse_rd_response(text))


class BiometricRdCheckTests(unittest.TestCase):
    def setUp(self):
        dc.reset_state()

    def _with_services(self, services):
        return mock.patch.object(dc, "probe_rd_services", return_value=services)

    def test_ready_service_confirms_the_pick_and_names_the_real_brand(self):
        with self._with_services([{"status": "READY", "info": "Mantra Authentication Vendor Device Service", "port": 11100}]):
            out = dc.apply_biometric_rd_check(dict(BIO_GENERIC))
        self.assertTrue(out["ok"])
        self.assertTrue(out["verified"])
        self.assertIn("confirmed by the RD service: Mantra Authentication", out["status"])
        self.assertEqual(out["verified_by"], "RD service")

    def test_nothing_picked_but_a_ready_rd_service_reports_the_scanner_by_itself(self):
        with self._with_services([{"status": "READY", "info": "Mantra Authentication Vendor Device Service", "port": 11100}]):
            out = dc.apply_biometric_rd_check(dict(NOTHING_PICKED))
        self.assertTrue(out["configured"] and out["present"] and out["ok"])
        self.assertIn("Mantra Authentication Vendor Device Service is ready", out["status"])
        self.assertEqual(out["resolved_name"], "")      # never saved as a "device pick"

    def test_notready_is_never_a_problem_field_case_morpho_mso1300(self):
        """A real CSP's perfectly working MorphoSmart MSO1300-E3 was shown as a
        Problem because its RD service said NOTREADY (Morpho does that until
        first use). The agent cannot prove a fault from that."""
        morpho = dict(BIO_OK, resolved_name="MorphoSmart MSO1300-E3")
        with self._with_services([{"status": "NOTREADY", "info": "Morpho RD Service", "port": 11100}]):
            out = dc.apply_biometric_rd_check(dict(morpho))
        self.assertTrue(out["ok"])
        self.assertIn("not treated as a fault", out["status"])
        self.assertNotIn("verified", out)          # the known brand verifies it later, not NOTREADY

    def test_nothing_picked_and_a_notready_rd_service_is_unverified_not_a_problem(self):
        with self._with_services([{"status": "NOTREADY", "info": "Morpho RD Service", "port": 11100}]):
            out = dc.apply_biometric_rd_check(dict(NOTHING_PICKED))
        self.assertTrue(out["ok"])
        self.assertFalse(out["verified"])
        self.assertIn("could not confirm the scanner is connected", out["status"])

    def test_a_service_that_answered_before_and_now_does_not_is_only_a_note(self):
        with self._with_services([{"status": "READY", "info": "", "port": 11100}]):
            dc.apply_biometric_rd_check(dict(BIO_OK))
        dc._cache.clear()
        with self._with_services([]):
            out = dc.apply_biometric_rd_check(dict(BIO_OK))
        self.assertTrue(out["ok"])
        self.assertIn("not answering right now", out["status"])

    def test_the_deep_checks_can_never_turn_a_device_into_a_problem(self):
        """Whatever the RD service or serial port says, ok must not become False."""
        import itertools
        service_sets = ([], [{"status": "READY", "info": "x", "port": 1}],
                        [{"status": "NOTREADY", "info": "x", "port": 1}],
                        [{"status": "BUSY", "info": "", "port": 1}])
        for services, picked in itertools.product(service_sets, (BIO_OK, BIO_GENERIC)):
            for _ in range(3):
                dc._cache.clear()
                with self._with_services(services):
                    out = dc.apply_biometric_rd_check(dict(picked))
                self.assertTrue(out["ok"], (services, picked))
        for services in service_sets:              # nothing picked: never red either
            dc._cache.clear()
            with self._with_services(services):
                out = dc.apply_biometric_rd_check(dict(NOTHING_PICKED))
            if out.get("configured"):
                self.assertTrue(out["ok"], services)
        for kind in ("DATA", "SILENT", "BUSY", "UNKNOWN"):
            for _ in range(5):
                dc._cache.clear()
                with mock.patch.object(dc, "read_nmea", return_value=(kind, GGA_FIX if kind == "DATA" else "")):
                    self.assertTrue(dc.apply_gps_nmea_check(dict(GPS_GENERIC))["ok"], kind)

    def test_nothing_picked_and_no_rd_service_stays_not_set_up(self):
        with self._with_services([]):
            out = dc.apply_biometric_rd_check(dict(NOTHING_PICKED))
        self.assertEqual({k: v for k, v in out.items() if k != "rd"}, NOTHING_PICKED)
        self.assertEqual(out["rd"], [])            # the probe ran and nothing answered

    def test_a_wrong_pick_is_rescued_by_a_ready_rd_service(self):
        """The CSP picked the wrong USB entry, but the scanner does work."""
        wrong = dict(BIO_GENERIC, resolved_name="USB Composite Device")
        with self._with_services([{"status": "READY", "info": "Mantra", "port": 11100}]):
            self.assertTrue(dc.apply_biometric_rd_check(wrong)["ok"])

    def test_an_unrecognised_pick_with_no_rd_service_is_left_for_the_verification_step(self):
        with self._with_services([]):
            out = dc.apply_biometric_rd_check(dict(BIO_GENERIC))
        self.assertTrue(out["ok"])
        self.assertNotIn("verified", out)      # apply_verification() decides, not this check

    def test_a_recognised_brand_with_no_rd_service_is_not_flagged(self):
        with self._with_services([]):
            out = dc.apply_biometric_rd_check(dict(BIO_OK))
        self.assertEqual({k: v for k, v in out.items() if k != "rd"}, BIO_OK)

    def test_one_ready_service_among_several_is_enough(self):
        with self._with_services([{"status": "NOTREADY", "info": "", "port": 11100},
                                  {"status": "READY", "info": "", "port": 11101}]):
            self.assertTrue(dc.apply_biometric_rd_check(dict(BIO_OK))["ok"])

    def test_a_windows_driver_problem_is_left_exactly_as_reported(self):
        bio = dict(BIO_OK, ok=False, status="Error")
        with mock.patch.object(dc, "probe_rd_services") as probe:
            self.assertEqual(dc.apply_biometric_rd_check(dict(bio)), bio)
        probe.assert_not_called()

    def test_a_crashing_probe_never_raises_or_changes_a_recognised_verdict(self):
        with mock.patch.object(dc, "probe_rd_services", side_effect=RuntimeError("boom")):
            self.assertEqual(dc.apply_biometric_rd_check(dict(BIO_OK)), BIO_OK)


class NmeaParsingTests(unittest.TestCase):
    def test_fix_and_satellite_count(self):
        self.assertEqual(dc.parse_nmea(GGA_FIX), {"data": True, "fix": True, "satellites": 8})

    def test_data_without_fix(self):
        self.assertEqual(dc.parse_nmea(GGA_NOFIX), {"data": True, "fix": False, "satellites": 0})

    def test_other_constellation_talker_ids(self):
        self.assertTrue(dc.parse_nmea("$GNGGA,1,2,N,3,E,1,05,1,1,M,1,M,,*00")["fix"])

    def test_garbage_is_no_data(self):
        self.assertEqual(dc.parse_nmea("\x00\x00hello"), {"data": False, "fix": False, "satellites": None})

    def test_com_port_extraction(self):
        self.assertEqual(dc.com_port_from_name("u-blox 7 (COM5)"), "COM5")
        self.assertIsNone(dc.com_port_from_name("u-blox GNSS Location Sensor"))


class GpsNmeaCheckTests(unittest.TestCase):
    def setUp(self):
        dc.reset_state()

    def _read(self, kind, text=""):
        return mock.patch.object(dc, "read_nmea", return_value=(kind, text))

    def test_data_with_fix_is_ok_and_says_so(self):
        with self._read("DATA", GGA_FIX):
            out = dc.apply_gps_nmea_check(dict(GPS_OK))
        self.assertTrue(out["ok"])
        self.assertIn("satellite fix, 8 satellites", out["status"])

    def test_no_fix_is_not_a_problem(self):
        with self._read("DATA", GGA_NOFIX):
            out = dc.apply_gps_nmea_check(dict(GPS_OK))
        self.assertTrue(out["ok"])
        self.assertIn("no satellite fix yet", out["status"])

    def test_a_silent_port_is_never_reported_as_a_problem(self):
        """Real field case: a dongle its owner says works was flagged Problem
        because this check heard nothing. Silence cannot prove a fault."""
        for _ in range(5):                       # however many times it is silent
            dc._cache.clear()
            with self._read("SILENT"):
                out = dc.apply_gps_nmea_check(dict(GPS_GENERIC))
            self.assertTrue(out["ok"])
            self.assertNotIn("verified", out)    # simply not confirmed by data
            self.assertIn("could not confirm", out["status"])
            self.assertNotIn("re-plug", out["status"])

    def test_the_status_always_explains_why_the_data_check_did_not_confirm(self):
        for kind, words in (("BUSY", "in use by another program"), ("SILENT", "no data heard"),
                            ("UNKNOWN", "could not open")):
            dc._cache.clear()
            with self._read(kind):
                out = dc.apply_gps_nmea_check(dict(GPS_GENERIC))
            self.assertIn(words, out["status"], kind)

    def test_busy_port_is_ok_with_a_note(self):
        with self._read("BUSY"):
            out = dc.apply_gps_nmea_check(dict(GPS_OK))
        self.assertTrue(out["ok"])
        self.assertIn("in use by another program", out["status"])

    def test_a_failed_probe_says_what_happened_but_changes_no_verdict(self):
        with self._read("UNKNOWN"):
            out = dc.apply_gps_nmea_check(dict(GPS_OK))
        self.assertTrue(out["ok"])
        self.assertNotIn("verified", out)
        self.assertIn("could not open COM5", out["status"])

    def test_the_serial_read_turns_dtr_and_rts_on_and_tries_the_common_speeds(self):
        script = dc._nmea_script("COM7")
        self.assertIn("DtrEnable=$true", script)
        self.assertIn("RtsEnable=$true", script)
        for baud in ("9600", "4800", "38400", "115200"):
            self.assertIn(baud, script)

    def test_debug_serial_reports_a_port_that_cannot_be_opened(self):
        out = dc.debug_serial("COM99")
        self.assertIn("COM99", out)
        self.assertIn("OPEN ERROR", out)

    def test_non_com_recognised_dongle_is_left_alone(self):
        gps = dict(GPS_OK, resolved_name="u-blox GNSS Location Sensor")
        with mock.patch.object(dc, "read_nmea") as read:
            self.assertEqual(dc.apply_gps_nmea_check(dict(gps)), gps)
        read.assert_not_called()

    def test_a_generic_pick_that_streams_gps_data_is_verified(self):
        with self._read("DATA", GGA_FIX):
            out = dc.apply_gps_nmea_check(dict(GPS_GENERIC))
        self.assertTrue(out["verified"])
        self.assertNotIn("not verified", out["status"])

    def test_a_generic_pick_that_cannot_be_read_is_left_for_the_verification_step(self):
        for kind in ("BUSY", "UNKNOWN"):
            dc._cache.clear()
            with self._read(kind):
                out = dc.apply_gps_nmea_check(dict(GPS_GENERIC))
            self.assertTrue(out["ok"])
            self.assertNotIn("verified", out, kind)

    def test_only_runs_when_the_basic_check_is_already_ok(self):
        with mock.patch.object(dc, "read_nmea") as read:
            dc.apply_gps_nmea_check(dict(GPS_OK, present=False))
            dc.apply_gps_nmea_check(dict(GPS_OK, ok=False))
        read.assert_not_called()


class WiredIntoCheckTests(unittest.TestCase):
    """check() must actually apply these, and never let a failure in them
    break the whole snapshot."""

    def setUp(self):
        dc.reset_state()
        for k in dh._last_good:        # never inherit another test's device list
            dh._last_good[k] = []
        self.addCleanup(lambda: [dh._last_good.__setitem__(k, []) for k in dh._last_good])

    def _fake_scans(self):
        usb = [{"name": "Mantra MFS100", "ok": True, "status": "OK"},
               {"name": "u-blox 7 (COM5)", "ok": True, "status": "OK"}]
        return (mock.patch.object(dh, "list_printers", return_value=[]),
                mock.patch.object(dh, "list_usb_devices", return_value=usb),
                mock.patch.object(dh, "list_all_devices", return_value=[]))

    def test_a_working_morpho_with_a_notready_rd_service_is_ok_and_verified_end_to_end(self):
        """The exact field case through check() + apply_verification()."""
        usb = [{"name": "MorphoSmart MSO1300-E3", "class": "USB", "ok": True, "status": "OK",
                "manufacturer": "", "vid": "225D", "pid": "000A", "vendor": "", "instance": "USB\X"}]
        with mock.patch.object(dh, "list_printers", return_value=[]),                 mock.patch.object(dh, "list_usb_devices", return_value=usb),                 mock.patch.object(dh, "list_all_devices", return_value=usb),                 mock.patch.object(dc, "probe_rd_services",
                                  return_value=[{"status": "NOTREADY", "info": "Morpho", "port": 11100}]):
            r = dh.check("", "", "", "MorphoSmart MSO1300-E3", "")
        out = dh.apply_verification(r)["biometric"]
        self.assertTrue(out["ok"])
        self.assertTrue(out["verified"])
        self.assertEqual(out["verified_by"], "known brand")

    def test_exceptions_in_deep_checks_do_not_break_check(self):
        p, u, a = self._fake_scans()
        with p, u, a, mock.patch.object(dc, "apply_biometric_rd_check", side_effect=RuntimeError), \
                mock.patch.object(dc, "apply_gps_nmea_check", side_effect=RuntimeError):
            r = dh._check_impl("", "", "", "Mantra MFS100", "u-blox 7 (COM5)")
        self.assertTrue(r["biometric"]["ok"])
        self.assertTrue(r["gps"]["ok"])


class DeviceNamingTests(unittest.TestCase):
    """The dropdown must identify devices by more than Windows' generic name."""

    def test_enrich_reads_maker_and_usb_ids(self):
        d = dh._enrich_device("USB Serial Device (COM5)", {
            "Status": "OK", "Class": "Ports", "Manufacturer": "Prolific",
            "InstanceId": "USB\\VID_067B&PID_2303\\5&1A2B3C"})
        self.assertEqual((d["manufacturer"], d["vid"], d["pid"], d["vendor"]),
                         ("Prolific", "067B", "2303", "Prolific"))

    def test_windows_generic_makers_are_dropped(self):
        d = dh._enrich_device("USB Input Device", {"Status": "OK", "Manufacturer": "(Standard system devices)"})
        self.assertEqual(d["manufacturer"], "")
        d = dh._enrich_device("X", {"Status": "OK", "Manufacturer": "Microsoft"})
        self.assertEqual(d["manufacturer"], "")

    def test_label_adds_maker_and_usb_id_to_a_generic_name(self):
        d = dh._enrich_device("USB Serial Device (COM5)", {
            "Status": "OK", "Manufacturer": "Prolific", "InstanceId": "USB\\VID_067B&PID_2303\\1"})
        self.assertEqual(dh.device_label(d), "USB Serial Device (COM5) | Prolific | USB 067B:2303")

    def test_label_does_not_repeat_a_maker_already_in_the_name(self):
        d = dh._enrich_device("Mantra MFS100", {"Status": "OK", "Manufacturer": "Mantra"})
        self.assertEqual(dh.device_label(d), "Mantra MFS100")

    def test_a_generic_name_is_recognised_through_its_maker(self):
        d = dh._enrich_device("USB Input Device", {"Status": "OK", "Manufacturer": "Mantra Softech"})
        self.assertEqual(dh.auto_detect_biometric([d]), "USB Input Device")
        self.assertTrue(dh.is_recognised(d, dh._KNOWN_BIOMETRIC_BRANDS))

    def test_a_generic_name_is_recognised_through_a_known_usb_vendor(self):
        d = dh._enrich_device("USB Device", {"Status": "OK", "InstanceId": "USB\\VID_1546&PID_01A7\\1"})
        self.assertEqual(d["vendor"], "u-blox")
        self.assertEqual(dh.auto_detect_gps([d]), "USB Device")   # recognised via its USB vendor id

    def test_dropdown_puts_likely_devices_first_and_hides_nothing(self):
        devs = [dh._enrich_device(n, r) for n, r in (
            ("USB Root Hub", {"Status": "OK", "Class": "USB"}),
            ("HID Keyboard Device", {"Status": "OK", "Class": "HIDClass"}),
            ("Fingerprint Reader", {"Status": "OK", "Class": "Biometric"}),
            ("USB Input Device", {"Status": "OK", "Class": "HIDClass", "Manufacturer": "Mantra"}))]
        ranked = [d["name"] for d in dh.rank_devices(devs, dh._KNOWN_BIOMETRIC_BRANDS, ("Biometric", "HIDClass"))]
        self.assertEqual(ranked[0], "USB Input Device")            # brand (via maker)
        self.assertEqual(ranked[1], "Fingerprint Reader")          # right class, alphabetical
        self.assertEqual(sorted(ranked), sorted(d["name"] for d in devs))   # nothing dropped


if __name__ == "__main__":
    unittest.main()
