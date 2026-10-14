"""A device is only shown as a healthy OK when we actually KNOW what it is.

Real field problem: the CSP picked the generic "USB Input Device" entry (a
keyboard has the same name) for a scanner/printer/GPS, the agent showed OK
because the entry was plugged in with a working driver, and the dashboard
showed a green OK for a device that did not work."""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import device_health as dh


def dev(configured=True, present=True, ok=True, name="", recognised=False, **extra):
    d = {"configured": configured, "present": present, "ok": ok, "status": "OK",
         "resolved_name": name, "recognised": recognised}
    d.update(extra)
    return d


def health(**devices):
    base = {k: dev(configured=False, present=False, ok=False) for k in ("printer", "microatm", "biometric", "gps")}
    base.update(devices)
    base["windows_printer_names"] = []
    base["software"] = {"configured": False, "running": False}
    return base


class VerificationRulesTests(unittest.TestCase):
    def test_a_generic_usb_input_device_pick_is_unverified_not_ok(self):
        h = health(biometric=dev(name="USB Input Device"))
        out = dh.apply_verification(h)
        self.assertFalse(out["biometric"]["verified"])
        self.assertIn("UNVERIFIED", out["biometric"]["status"])
        self.assertIn("fingerprint scanner", out["biometric"]["status"])

    def test_same_for_every_device_type(self):
        for dev_key, word in (("printer", "passbook printer"), ("microatm", "micro-ATM"),
                              ("biometric", "fingerprint scanner"), ("gps", "GPS dongle")):
            out = dh.apply_verification(health(**{dev_key: dev(name="USB Input Device")}))
            self.assertFalse(out[dev_key]["verified"], dev_key)
            self.assertIn(word, out[dev_key]["status"], dev_key)

    def test_a_known_brand_is_verified(self):
        out = dh.apply_verification(health(biometric=dev(name="Mantra MFS100", recognised=True)))
        self.assertTrue(out["biometric"]["verified"])
        self.assertEqual(out["biometric"]["verified_by"], "known brand")
        self.assertNotIn("UNVERIFIED", out["biometric"]["status"])

    def test_the_plug_in_test_confirms_a_generic_entry(self):
        confirmed = {"gps": {"name": "USB Serial Device (COM5)", "instance": "USB\\X"}}
        out = dh.apply_verification(health(gps=dev(name="USB Serial Device (COM5)")), confirmed)
        self.assertTrue(out["gps"]["verified"])
        self.assertEqual(out["gps"]["verified_by"], "plug-in test")

    def test_a_confirmation_does_not_carry_over_to_a_different_pick(self):
        confirmed = {"gps": {"name": "USB Serial Device (COM5)", "instance": ""}}
        out = dh.apply_verification(health(gps=dev(name="USB Input Device")), confirmed)
        self.assertFalse(out["gps"]["verified"])

    def test_evidence_from_the_device_itself_verifies_it(self):
        out = dh.apply_verification(health(
            biometric=dev(name="USB Input Device", verified=True, verified_by="RD service"),
            gps=dev(name="USB Serial Device (COM5)", verified=True, verified_by="GPS data")))
        self.assertTrue(out["biometric"]["verified"] and out["gps"]["verified"])

    def test_a_windows_printer_is_verified_only_by_a_successful_test_print(self):
        h = health(printer=dev(name="EPSON L3100 Series"))
        h["windows_printer_names"] = ["EPSON L3100 Series"]
        self.assertFalse(dh.apply_verification(h)["printer"]["verified"])
        self.assertFalse(dh.apply_verification(h, None, {"ran": True, "ok": False})["printer"]["verified"])
        self.assertFalse(dh.apply_verification(h, None, {"ran": False, "ok": False})["printer"]["verified"])
        ok = dh.apply_verification(h, None, {"ran": True, "ok": True})["printer"]
        self.assertTrue(ok["verified"])
        self.assertEqual(ok["verified_by"], "test print")

    def test_a_software_printer_can_never_be_verified_by_a_test_print(self):
        h = health(printer=dev(name="Microsoft Print to PDF"))
        h["windows_printer_names"] = ["Microsoft Print to PDF"]
        self.assertFalse(dh.apply_verification(h, None, {"ran": True, "ok": True})["printer"]["verified"])

    def test_a_micro_atm_is_verified_when_its_software_is_running(self):
        h = health(microatm=dev(name="USB Composite Device"))
        h["software"] = {"configured": True, "running": True}
        self.assertTrue(dh.apply_verification(h)["microatm"]["verified"])
        h["software"] = {"configured": True, "running": False}
        self.assertFalse(dh.apply_verification(h)["microatm"]["verified"])

    def test_problems_and_not_set_up_devices_are_left_untouched(self):
        h = health(printer=dev(ok=False, name="X"), gps=dev(present=False, name="Y"))
        out = dh.apply_verification(h)
        for key in ("printer", "gps", "biometric", "microatm"):
            self.assertNotIn("verified", out[key], key)
        self.assertEqual(out["printer"]["status"], "OK")

    def test_the_input_is_not_mutated(self):
        h = health(biometric=dev(name="USB Input Device"))
        dh.apply_verification(h)
        self.assertNotIn("verified", h["biometric"])


class RealWorldFlowTests(unittest.TestCase):
    """The exact scenario: pick 'USB Input Device' from the dropdown."""

    def setUp(self):
        import deep_checks
        deep_checks.reset_state()      # its RD/GPS result cache must not leak between tests
        self.addCleanup(deep_checks.reset_state)
        for k in dh._last_good:
            dh._last_good[k] = []
        self.addCleanup(lambda: [dh._last_good.__setitem__(k, []) for k in dh._last_good])

    def _check(self, usb, alld=None, **kw):
        alld = usb if alld is None else alld
        with mock.patch.object(dh, "list_printers", return_value=[]), \
                mock.patch.object(dh, "list_usb_devices", return_value=usb), \
                mock.patch.object(dh, "list_all_devices", return_value=alld), \
                mock.patch.object(dh, "process_running", return_value={"configured": False, "running": False}), \
                mock.patch("deep_checks.probe_rd_services", return_value=[]):
            return dh.check("", "", "", **kw)

    def test_picking_usb_input_device_yields_unverified(self):
        generic = dh._enrich_device("USB Input Device", {"Status": "OK", "Class": "HIDClass",
                                                          "InstanceId": "HID\\VID_1234&PID_5678\\A"})
        r = self._check([generic], biometric_name="USB Input Device")
        out = dh.apply_verification(r)
        self.assertTrue(out["biometric"]["ok"])           # Windows: plugged in, driver fine
        self.assertFalse(out["biometric"]["verified"])    # ...but NOT known to be a scanner

    def test_two_entries_with_the_same_generic_name_track_the_confirmed_one(self):
        keyboard = dh._enrich_device("USB Input Device", {"Status": "OK", "Class": "HIDClass", "InstanceId": "HID\\KBD\\1"})
        scanner = dh._enrich_device("USB Input Device", {"Status": "Error", "Class": "HIDClass", "InstanceId": "HID\\SCN\\2"})
        confirmed = {"biometric": {"name": "USB Input Device", "instance": "HID\\SCN\\2"}}
        r = self._check([keyboard, scanner], biometric_name="USB Input Device", confirmed=confirmed)
        self.assertFalse(r["biometric"]["ok"])            # judged by the scanner's entry, not the keyboard's
        self.assertEqual(r["biometric"]["status"], "Error")

    def test_a_confirmed_device_that_is_unplugged_is_reported_not_found(self):
        keyboard = dh._enrich_device("USB Input Device", {"Status": "OK", "Class": "HIDClass", "InstanceId": "HID\\KBD\\1"})
        confirmed = {"biometric": {"name": "USB Input Device", "instance": "HID\\SCN\\2"}}
        r = self._check([keyboard], biometric_name="USB Input Device", confirmed=confirmed)
        self.assertFalse(r["biometric"]["ok"])
        self.assertFalse(r["biometric"]["present"])

    def test_a_confirmed_pick_is_not_silently_auto_corrected_to_another_device(self):
        other = dh._enrich_device("Ingenico iCT250", {"Status": "OK", "Class": "USB"})
        mine = dh._enrich_device("USB Composite Device", {"Status": "OK", "Class": "USB", "InstanceId": "USB\\X\\9"})
        confirmed = {"microatm": {"name": "USB Composite Device", "instance": "USB\\X\\9"}}
        with mock.patch.object(dh, "list_printers", return_value=[]), \
                mock.patch.object(dh, "list_usb_devices", return_value=[other, mine]), \
                mock.patch.object(dh, "list_all_devices", return_value=[other, mine]), \
                mock.patch.object(dh, "process_running", return_value={"configured": False, "running": False}):
            r = dh.check("", "USB Composite Device", "", confirmed=confirmed)
        self.assertEqual(r["microatm"]["resolved_name"], "USB Composite Device")


class FindMyDeviceTests(unittest.TestCase):
    def setUp(self):
        fd, self.cfg_path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        os.remove(self.cfg_path)
        os.environ["CSP_AGENT_CONFIG_PATH"] = self.cfg_path
        import local_ui
        self.local_ui = local_ui
        local_ui._snapshot.update({"instances": None, "printers": None})
        self.client = local_ui.app.test_client()
        self.addCleanup(lambda: os.path.exists(self.cfg_path) and os.remove(self.cfg_path))

    def _scan(self, devs, printers=()):
        return mock.patch.object(self.local_ui, "_scan_for_find", return_value=(devs, list(printers)))

    def test_step2_before_step1_asks_for_step1(self):
        with self._scan([dh._enrich_device("A", {"Status": "OK"})]):
            data = self.client.post("/detect").get_json()
        self.assertFalse(data["ok"])
        self.assertIn("Step 1", data["error"])

    def test_it_reports_only_what_appeared_after_plugging_in(self):
        before = [dh._enrich_device("USB Root Hub", {"Status": "OK", "InstanceId": "USB\\ROOT\\1"})]
        after = before + [dh._enrich_device("USB Input Device", {"Status": "OK", "InstanceId": "HID\\NEW\\7",
                                                                  "Manufacturer": "Mantra"})]
        with self._scan(before):
            self.assertTrue(self.client.post("/snapshot").get_json()["ok"])
        with self._scan(after):
            data = self.client.post("/detect").get_json()
        self.assertEqual([a["name"] for a in data["appeared"]], ["USB Input Device"])
        self.assertEqual(data["appeared"][0]["instance"], "HID\\NEW\\7")
        self.assertIn("Mantra", data["appeared"][0]["label"])

    def test_a_same_named_second_entry_is_still_detected_by_instance_id(self):
        kbd = dh._enrich_device("USB Input Device", {"Status": "OK", "InstanceId": "HID\\KBD\\1"})
        scn = dh._enrich_device("USB Input Device", {"Status": "OK", "InstanceId": "HID\\SCN\\2"})
        with self._scan([kbd]):
            self.client.post("/snapshot")
        with self._scan([kbd, scn]):
            data = self.client.post("/detect").get_json()
        self.assertEqual([a["instance"] for a in data["appeared"]], ["HID\\SCN\\2"])

    def test_a_newly_installed_real_printer_is_reported_but_a_software_one_is_not(self):
        d = [dh._enrich_device("USB Root Hub", {"Status": "OK", "InstanceId": "U\\1"})]
        with self._scan(d, ["Fax"]):
            self.client.post("/snapshot")
        with self._scan(d, ["Fax", "TVS MSP 250", "Microsoft Print to PDF"]):
            data = self.client.post("/detect").get_json()
        self.assertEqual([a["name"] for a in data["appeared"]], ["TVS MSP 250"])

    def test_nothing_new_says_so(self):
        d = [dh._enrich_device("USB Root Hub", {"Status": "OK", "InstanceId": "U\\1"})]
        with self._scan(d):
            self.client.post("/snapshot")
            data = self.client.post("/detect").get_json()
        self.assertEqual(data["appeared"], [])

    def test_confirm_saves_the_selection_and_the_confirmation(self):
        r = self.client.post("/confirm", json={"device": "biometric", "name": "USB Input Device",
                                               "instance": "HID\\SCN\\2"})
        self.assertTrue(r.get_json()["ok"])
        import config_store
        cfg = config_store.load()
        self.assertEqual((cfg["biometric_name"], cfg["biometric_confirmed"], cfg["biometric_instance"]),
                         ("USB Input Device", "USB Input Device", "HID\\SCN\\2"))
        self.assertEqual(config_store.confirmed_map(cfg)["biometric"],
                         {"name": "USB Input Device", "instance": "HID\\SCN\\2"})

    def test_confirm_rejects_unknown_devices_and_blank_names(self):
        self.assertEqual(self.client.post("/confirm", json={"device": "toaster", "name": "x"}).status_code, 400)
        self.assertEqual(self.client.post("/confirm", json={"device": "gps", "name": ""}).status_code, 400)

    def test_choosing_an_entry_the_test_just_found_and_saving_counts_as_confirming_it(self):
        """The real field flow: Step 1, Step 2, then pick the entry in the dropdown
        and Save instead of pressing the 'This is my ...' button."""
        before = [dh._enrich_device("USB Root Hub", {"Status": "OK", "InstanceId": "U\1"})]
        ch = dh._enrich_device("USB-SERIAL CH340 (COM7)", {"Status": "OK", "Class": "Ports",
                                                           "Manufacturer": "wch.cn", "InstanceId": "USB\VID_1A86&PID_7523\5"})
        with self._scan(before):
            self.client.post("/snapshot")
        with self._scan(before + [ch]):
            self.client.post("/detect")
        self.client.post("/configure", json={"gps_name": "USB-SERIAL CH340 (COM7)"})
        import config_store
        cfg = config_store.load()
        self.assertEqual(cfg["gps_confirmed"], "USB-SERIAL CH340 (COM7)")
        self.assertEqual(cfg["gps_instance"], "USB\VID_1A86&PID_7523\5")

    def test_choosing_an_entry_the_test_did_not_find_does_not_confirm_it(self):
        with self._scan([dh._enrich_device("USB Root Hub", {"Status": "OK", "InstanceId": "U\1"})]):
            self.client.post("/snapshot")
            self.client.post("/detect")
        self.client.post("/configure", json={"gps_name": "USB Input Device"})
        import config_store
        self.assertEqual(config_store.load()["gps_confirmed"], "")

    def test_an_ambiguous_same_named_entry_is_not_auto_confirmed(self):
        a = dh._enrich_device("USB Input Device", {"Status": "OK", "InstanceId": "H\1"})
        b = dh._enrich_device("USB Input Device", {"Status": "OK", "InstanceId": "H\2"})
        with self._scan([]), mock.patch.object(self.local_ui, "_scan_for_find", return_value=([dh._enrich_device("X", {"Status": "OK", "InstanceId": "X\1"})], [])):
            self.client.post("/snapshot")
        with mock.patch.object(self.local_ui, "_scan_for_find",
                               return_value=([dh._enrich_device("X", {"Status": "OK", "InstanceId": "X\1"}), a, b], [])):
            self.client.post("/detect")
        self.client.post("/configure", json={"biometric_name": "USB Input Device"})
        import config_store
        self.assertEqual(config_store.load()["biometric_confirmed"], "")

    def test_the_page_has_the_find_my_device_steps(self):
        html = self.client.get("/").get_data(as_text=True)
        for needle in ("findStep1", "findStep2", "confirmDevice", "badge warn", "unverified"):
            self.assertIn(needle, html)


if __name__ == "__main__":
    unittest.main()
