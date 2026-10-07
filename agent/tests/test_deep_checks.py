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
          "resolved_name": "Mantra MFS100"}
GPS_OK = {"configured": True, "present": True, "ok": True, "status": "OK",
          "resolved_name": "USB-SERIAL CH340 (COM5)"}

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

    def test_ready_service_leaves_the_verdict_alone(self):
        with self._with_services([{"status": "READY", "info": "", "port": 11100}]):
            self.assertEqual(dc.apply_biometric_rd_check(dict(BIO_OK)), BIO_OK)

    def test_notready_service_is_a_problem(self):
        with self._with_services([{"status": "NOTREADY", "info": "", "port": 11100}]):
            out = dc.apply_biometric_rd_check(dict(BIO_OK))
        self.assertFalse(out["ok"])
        self.assertIn("NOTREADY", out["status"])

    def test_one_ready_service_among_several_is_enough(self):
        with self._with_services([{"status": "NOTREADY", "info": "", "port": 11100},
                                  {"status": "READY", "info": "", "port": 11101}]):
            self.assertTrue(dc.apply_biometric_rd_check(dict(BIO_OK))["ok"])

    def test_no_rd_service_ever_seen_changes_nothing(self):
        with self._with_services([]):
            self.assertEqual(dc.apply_biometric_rd_check(dict(BIO_OK)), BIO_OK)

    def test_service_that_answered_before_and_now_vanished_is_a_problem(self):
        with self._with_services([{"status": "READY", "info": "", "port": 11100}]):
            dc.apply_biometric_rd_check(dict(BIO_OK))
        dc._cache.clear()   # let the next check probe again
        with self._with_services([]):
            out = dc.apply_biometric_rd_check(dict(BIO_OK))
        self.assertFalse(out["ok"])
        self.assertIn("no longer responding", out["status"])

    def test_only_runs_when_the_basic_check_is_already_ok(self):
        for bad in ({"configured": False}, {"present": False}, {"ok": False}):
            bio = dict(BIO_OK, **bad)
            with mock.patch.object(dc, "probe_rd_services") as probe:
                self.assertEqual(dc.apply_biometric_rd_check(dict(bio)), bio)
            probe.assert_not_called()

    def test_a_crashing_probe_never_raises_or_changes_the_verdict(self):
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
        self.assertEqual(dc.com_port_from_name("USB-SERIAL CH340 (COM5)"), "COM5")
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

    def test_one_silent_read_is_not_enough_to_flag(self):
        with self._read("SILENT"):
            self.assertTrue(dc.apply_gps_nmea_check(dict(GPS_OK))["ok"])

    def test_two_consecutive_silent_reads_flag_a_problem(self):
        with self._read("SILENT"):
            dc.apply_gps_nmea_check(dict(GPS_OK))
            dc._cache.clear()     # next heartbeat = a fresh read
            out = dc.apply_gps_nmea_check(dict(GPS_OK))
        self.assertFalse(out["ok"])
        self.assertIn("sending no data", out["status"])

    def test_cached_repeat_polls_do_not_count_as_extra_silent_reads(self):
        with self._read("SILENT") as read:
            for _ in range(5):        # status page polling within the cache window
                out = dc.apply_gps_nmea_check(dict(GPS_OK))
        self.assertEqual(read.call_count, 1)
        self.assertTrue(out["ok"])

    def test_data_resets_the_silent_counter(self):
        with self._read("SILENT"):
            dc.apply_gps_nmea_check(dict(GPS_OK))
        dc._cache.clear()
        with self._read("DATA", GGA_FIX):
            dc.apply_gps_nmea_check(dict(GPS_OK))
        dc._cache.clear()
        with self._read("SILENT"):
            self.assertTrue(dc.apply_gps_nmea_check(dict(GPS_OK))["ok"])

    def test_busy_port_is_ok_with_a_note(self):
        with self._read("BUSY"):
            out = dc.apply_gps_nmea_check(dict(GPS_OK))
        self.assertTrue(out["ok"])
        self.assertIn("in use by another program", out["status"])

    def test_a_failed_probe_says_nothing(self):
        with self._read("UNKNOWN"):
            self.assertEqual(dc.apply_gps_nmea_check(dict(GPS_OK)), GPS_OK)

    def test_non_com_dongle_is_left_alone(self):
        gps = dict(GPS_OK, resolved_name="u-blox GNSS Location Sensor")
        with mock.patch.object(dc, "read_nmea") as read:
            self.assertEqual(dc.apply_gps_nmea_check(dict(gps)), gps)
        read.assert_not_called()

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

    def _fake_scans(self):
        usb = [{"name": "Mantra MFS100", "ok": True, "status": "OK"},
               {"name": "USB-SERIAL CH340 (COM5)", "ok": True, "status": "OK"}]
        return (mock.patch.object(dh, "list_printers", return_value=[]),
                mock.patch.object(dh, "list_usb_devices", return_value=usb),
                mock.patch.object(dh, "list_all_devices", return_value=[]))

    def test_notready_rd_service_shows_up_in_check(self):
        p, u, a = self._fake_scans()
        with p, u, a, mock.patch.object(
                dc, "probe_rd_services", return_value=[{"status": "NOTREADY", "info": "", "port": 11100}]), \
                mock.patch.object(dc, "read_nmea", return_value=("DATA", GGA_FIX)):
            r = dh._check_impl("", "", "", "Mantra MFS100", "USB-SERIAL CH340 (COM5)")
        self.assertFalse(r["biometric"]["ok"])
        self.assertTrue(r["gps"]["ok"])
        self.assertIn("satellite fix", r["gps"]["status"])

    def test_exceptions_in_deep_checks_do_not_break_check(self):
        p, u, a = self._fake_scans()
        with p, u, a, mock.patch.object(dc, "apply_biometric_rd_check", side_effect=RuntimeError), \
                mock.patch.object(dc, "apply_gps_nmea_check", side_effect=RuntimeError):
            r = dh._check_impl("", "", "", "Mantra MFS100", "USB-SERIAL CH340 (COM5)")
        self.assertTrue(r["biometric"]["ok"])
        self.assertTrue(r["gps"]["ok"])


if __name__ == "__main__":
    unittest.main()
