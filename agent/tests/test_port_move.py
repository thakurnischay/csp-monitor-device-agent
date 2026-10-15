"""The CSP moves a confirmed device to another USB port, or Windows renumbers
its COM port: it must be found again automatically, never reported missing -
and a DIFFERENT device must never be mistaken for it."""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import deep_checks
import device_health as dh

SCN = r"HID\VID_2C0F&PID_1204&MI_00\6&AAAA1111&0&0000"        # port-based instance id (no serial)


def dev(name, instance, status="OK", cls="HIDClass"):
    return dh._enrich_device(name, {"Status": status, "Class": cls, "InstanceId": instance})


class PortMoveTests(unittest.TestCase):
    def setUp(self):
        deep_checks.reset_state()
        for k in dh._last_good:
            dh._last_good[k] = []
        self.addCleanup(deep_checks.reset_state)
        self.addCleanup(lambda: [dh._last_good.__setitem__(k, []) for k in dh._last_good])

    @staticmethod
    def conf(name="USB Input Device", instance=SCN):
        return {"biometric": {"name": name, "instance": instance,
                              "fingerprint": dh.fingerprint(name, instance)}}

    @staticmethod
    def check(devices, confirmed, bio="USB Input Device"):
        with mock.patch.object(dh, "list_printers", return_value=[]), \
                mock.patch.object(dh, "list_usb_devices", return_value=devices), \
                mock.patch.object(dh, "list_all_devices", return_value=devices), \
                mock.patch.object(dh, "process_running", return_value={"configured": False, "running": False}), \
                mock.patch.object(deep_checks, "probe_rd_services", return_value=[]), \
                mock.patch.object(deep_checks, "read_nmea", return_value=("UNKNOWN", "")):
            return dh.check("", "", "", bio, "", confirmed)

    def test_the_same_port_is_matched_directly(self):
        r = self.check([dev("USB Input Device", SCN)], self.conf())
        self.assertTrue(r["biometric"]["ok"])
        self.assertNotIn("moved_to", r["biometric"])

    def test_moved_to_another_usb_port_is_found_again_and_stays_verified(self):
        moved = dev("USB Input Device", r"HID\VID_2C0F&PID_1204&MI_00\6&BBBB2222&0&0000")
        r = self.check([moved], self.conf())
        self.assertTrue(r["biometric"]["present"] and r["biometric"]["ok"])
        self.assertIn("moved to a different USB port", r["biometric"]["status"])
        self.assertEqual(r["biometric"]["moved_to"]["instance"], moved["instance"])
        out = dh.apply_verification(r, self.conf())["biometric"]
        self.assertTrue(out["verified"])                                # still confirmed, no re-test
        self.assertEqual(out["verified_by"], "plug-in test")

    def test_a_com_port_that_was_renumbered_is_found_again(self):
        old = r"USB\VID_1A86&PID_7523\5&1111&0&2"
        moved = dev("USB-SERIAL CH340 (COM3)", r"USB\VID_1A86&PID_7523\5&9999&0&4", cls="Ports")
        r = self.check([moved], self.conf("USB-SERIAL CH340 (COM7)", old), bio="USB-SERIAL CH340 (COM7)")
        self.assertTrue(r["biometric"]["present"] and r["biometric"]["ok"])
        self.assertEqual(r["biometric"]["resolved_name"], "USB-SERIAL CH340 (COM3)")

    def test_the_move_is_remembered_so_the_next_check_matches_directly(self):
        import config_store
        os.environ["CSP_AGENT_CONFIG_PATH"] = os.path.join(tempfile.mkdtemp(), "c.json")
        moved = dev("USB Input Device", r"HID\VID_2C0F&PID_1204&MI_00\6&BBBB2222&0&0000")
        r = self.check([moved], self.conf())
        config_store.sync_moved_device("biometric", r["biometric"])
        cfg = config_store.load()
        self.assertEqual(cfg["biometric_instance"], moved["instance"])
        r2 = self.check([moved], config_store.confirmed_map(cfg))
        self.assertTrue(r2["biometric"]["ok"])
        self.assertNotIn("moved_to", r2["biometric"])                   # now an exact match

    def test_an_unmoved_device_causes_no_config_writes(self):
        import config_store
        os.environ["CSP_AGENT_CONFIG_PATH"] = os.path.join(tempfile.mkdtemp(), "c.json")
        r = self.check([dev("USB Input Device", SCN)], self.conf())
        config_store.sync_moved_device("biometric", r["biometric"])
        self.assertFalse(os.path.exists(os.environ["CSP_AGENT_CONFIG_PATH"]))

    def test_a_keyboard_with_the_same_generic_name_is_never_mistaken_for_it(self):
        keyboard = dev("USB Input Device", r"HID\VID_046D&PID_C31C\7&1234&0&0000")
        r = self.check([keyboard], self.conf())                         # scanner gone; only a keyboard remains
        self.assertFalse(r["biometric"]["present"])
        self.assertFalse(r["biometric"]["ok"])
        self.assertIn("Find my device again", r["biometric"]["status"])

    def test_the_scanner_is_picked_out_from_a_keyboard_after_a_move(self):
        keyboard = dev("USB Input Device", r"HID\VID_046D&PID_C31C\7&1234&0&0000")
        moved = dev("USB Input Device", r"HID\VID_2C0F&PID_1204&MI_00\6&BBBB2222&0&0000")
        r = self.check([keyboard, moved], self.conf())
        self.assertEqual(r["biometric"]["moved_to"]["instance"], moved["instance"])

    def test_a_replacement_of_a_different_model_is_not_silently_accepted(self):
        other_model = dev("USB Input Device", r"HID\VID_2C0F&PID_9999&MI_00\6&CCCC3333&0&0000")
        self.assertFalse(self.check([other_model], self.conf())["biometric"]["present"])

    def test_a_different_interface_of_the_same_usb_device_is_not_confused(self):
        mi01 = dev("USB Input Device", r"HID\VID_2C0F&PID_1204&MI_01\6&DDDD4444&0&0001")
        self.assertFalse(self.check([mi01], self.conf())["biometric"]["present"])

    def test_unplugged_entirely_is_reported_not_found(self):
        hub = dev("USB Root Hub", r"USB\ROOT_HUB30\4&1&0&0", cls="USB")
        self.assertFalse(self.check([hub], self.conf())["biometric"]["present"])

    def test_two_identical_devices_prefer_the_one_with_the_same_name(self):
        a = dev("USB Input Device", r"HID\VID_2C0F&PID_1204&MI_00\6&A&0&0")
        b = dev("Other Name", r"HID\VID_2C0F&PID_1204&MI_00\6&B&0&0")
        r = self.check([b, a], self.conf())
        self.assertEqual(r["biometric"]["moved_to"]["name"], "USB Input Device")
        self.assertIn("several identical devices", r["biometric"]["status"])

    def test_the_fingerprint_is_built_from_hardware_ids_not_the_port(self):
        fp = dh.fingerprint("USB-SERIAL CH340 (COM7)", r"USB\VID_1A86&PID_7523&MI_00\5&1&0&2")
        self.assertEqual(fp, {"vid": "1A86", "pid": "7523", "mi": "00", "base": "usb-serial ch340"})
        self.assertEqual(dh.fingerprint("X", r"HID\NO_IDS\1")["vid"], "")

    def test_a_device_with_a_serial_number_matches_on_instance_even_if_ids_are_odd(self):
        inst = r"USB\VID_2C0F&PID_1204\SERIAL12345"                     # stable across ports
        r = self.check([dev("Mantra MFS110", inst)], self.conf("Mantra MFS110", inst), bio="Mantra MFS110")
        self.assertTrue(r["biometric"]["present"])
        self.assertNotIn("moved_to", r["biometric"])


if __name__ == "__main__":
    unittest.main()
