"""The USB vendor id is set by a device's own maker, so it identifies the brand
even when Windows gives the device a generic name. Real ids seen on real CSP
scanners: Mantra MFS110 = 2C0F:1204, MorphoSmart MSO1300-E3 = 225D:000A."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import device_health as dh


def dev(name, instance, cls="HIDClass", maker=""):
    return dh._enrich_device(name, {"Status": "OK", "Class": cls, "InstanceId": instance, "Manufacturer": maker})


class VendorIdRecognitionTests(unittest.TestCase):
    def test_a_generic_name_with_a_mantra_vendor_id_is_a_known_scanner(self):
        d = dev("USB Input Device", r"HID\VID_2C0F&PID_1204&MI_00\6&A&0&0000")
        self.assertTrue(dh.is_recognised(d, dh._KNOWN_BIOMETRIC_BRANDS))
        self.assertEqual(dh.auto_detect_biometric([d]), "USB Input Device")

    def test_the_real_morpho_id_is_recognised_without_any_name_help(self):
        d = dev("USB Composite Device", r"USB\VID_225D&PID_000A\123", cls="USB")
        self.assertTrue(dh.is_recognised(d, dh._KNOWN_BIOMETRIC_BRANDS))

    def test_other_scanner_makers(self):
        for vid in ("079B", "1162", "16D1", "05BA", "1491"):
            d = dev("USB Device", rf"USB\VID_{vid}&PID_0001\1", cls="USB")
            self.assertTrue(dh.is_recognised(d, dh._KNOWN_BIOMETRIC_BRANDS), vid)

    def test_micro_atm_and_gps_makers(self):
        self.assertTrue(dh.is_recognised(dev("USB Device", r"USB\VID_0B00&PID_0001\1", "USB"), dh._KNOWN_MICROATM_BRANDS))
        self.assertTrue(dh.is_recognised(dev("USB Device", r"USB\VID_11CA&PID_0001\1", "USB"), dh._KNOWN_MICROATM_BRANDS))
        self.assertTrue(dh.is_recognised(dev("USB Device", r"USB\VID_1546&PID_01A7\1", "USB"), dh._KNOWN_GPS_BRANDS))

    def test_a_scanner_id_is_not_taken_for_a_gps_or_micro_atm(self):
        d = dev("USB Input Device", r"HID\VID_2C0F&PID_1204&MI_00\6&A&0&0000")
        self.assertFalse(dh.is_recognised(d, dh._KNOWN_GPS_BRANDS))
        self.assertFalse(dh.is_recognised(d, dh._KNOWN_MICROATM_BRANDS))

    def test_a_keyboard_or_unknown_vendor_is_not_recognised_as_anything(self):
        kb = dev("USB Input Device", r"HID\VID_046D&PID_C31C\7&1&0&0000")
        for brands in (dh._KNOWN_BIOMETRIC_BRANDS, dh._KNOWN_GPS_BRANDS, dh._KNOWN_MICROATM_BRANDS):
            self.assertFalse(dh.is_recognised(kb, brands))

    def test_a_generic_serial_chip_is_never_recognised_as_a_gps_by_its_id(self):
        """CH340 / PL2303 / FTDI are used by printers, GPS, modems... the chip
        id says nothing about what is attached - that needs Find my device."""
        for vid in ("1A86", "067B", "0403", "10C4"):
            d = dev("USB-SERIAL (COM3)", rf"USB\VID_{vid}&PID_7523\1", cls="Ports")
            self.assertFalse(dh.is_recognised(d, dh._KNOWN_GPS_BRANDS), vid)
            self.assertFalse(dh.is_recognised(d, dh._KNOWN_BIOMETRIC_BRANDS), vid)

    def test_a_recognised_vendor_id_verifies_the_device_without_find_my_device(self):
        health = {"printer": {"configured": False}, "microatm": {"configured": False}, "gps": {"configured": False},
                  "biometric": {"configured": True, "present": True, "ok": True, "status": "OK",
                                "resolved_name": "USB Input Device", "recognised": True},
                  "software": {}, "windows_printer_names": []}
        out = dh.apply_verification(health)["biometric"]
        self.assertTrue(out["verified"])
        self.assertEqual(out["verified_by"], "known brand")

    def test_the_dropdown_ranks_a_vendor_id_match_first(self):
        kb = dev("USB Input Device", r"HID\VID_046D&PID_C31C\7&1&0&0000")
        scn = dev("USB Input Device", r"HID\VID_2C0F&PID_1204&MI_00\6&A&0&0000")
        ranked = dh.rank_devices([kb, scn], dh._KNOWN_BIOMETRIC_BRANDS, ("Biometric", "HIDClass"))
        self.assertEqual(ranked[0]["vid"], "2C0F")


class SameGenericNameTests(unittest.TestCase):
    def setUp(self):
        for k in dh._last_good:
            dh._last_good[k] = []

    def _check(self, devices, bio):
        from unittest import mock
        import deep_checks
        deep_checks.reset_state()
        with mock.patch.object(dh, "list_printers", return_value=[]),                 mock.patch.object(dh, "list_usb_devices", return_value=devices),                 mock.patch.object(dh, "list_all_devices", return_value=devices),                 mock.patch.object(dh, "process_running", return_value={"configured": False, "running": False}),                 mock.patch.object(deep_checks, "probe_rd_services", return_value=[]):
            return dh.check("", "", "", bio, "")

    def test_the_scanner_is_judged_not_the_keyboard_that_shares_its_name(self):
        kb = dh._enrich_device("USB Input Device", {"Status": "OK", "Class": "HIDClass",
                                                     "InstanceId": r"HID\VID_046D&PID_C31C&1&0&0000"})
        scn = dh._enrich_device("USB Input Device", {"Status": "Error", "Class": "HIDClass",
                                                      "InstanceId": r"HID\VID_2C0F&PID_1204&MI_00&A&0&0000"})
        for order in ([kb, scn], [scn, kb]):                 # whichever comes first
            r = self._check(order, "USB Input Device")
            self.assertEqual(r["biometric"]["status"], "Error", order)    # the scanner's own status
            self.assertFalse(r["biometric"]["ok"])

    def test_two_unrecognised_same_named_entries_are_left_alone(self):
        a = dh._enrich_device("USB Input Device", {"Status": "OK", "InstanceId": r"HID\VID_046D&PID_1"})
        b = dh._enrich_device("USB Input Device", {"Status": "Error", "InstanceId": r"HID\VID_046D&PID_2"})
        r = self._check([a, b], "USB Input Device")
        self.assertEqual(r["biometric"]["status"], "OK")      # no vendor evidence: no guessing


if __name__ == "__main__":
    unittest.main()
