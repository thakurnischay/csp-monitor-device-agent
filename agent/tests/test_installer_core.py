"""Installer engine: registration, device choice rules, preserving an existing install, plain-language failures."""
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
AGENT = os.path.dirname(HERE)
sys.path.insert(0, AGENT)
sys.path.insert(0, os.path.join(AGENT, "installer"))

import config_store                      # noqa: E402
import installer_core as ic              # noqa: E402


def dev(name, instance="USB\\VID_1234&PID_0001\\A", klass="USB", ok=True):
    return {"name": name, "instance": instance, "class": klass, "ok": ok, "status": "OK", "manufacturer": "", "vid": "1234", "pid": "0001"}


def fake_check(printers=(), usb=(), bio=(), gps=()):
    return lambda: {"printer_list": list(printers), "usb_device_list": list(usb), "biometric_device_list": list(bio),
                    "gps_device_list": list(gps), "scan_problem": ""}


class Base(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.install = os.path.join(self.root, "agent")
        self.source = os.path.join(self.root, "src")
        os.makedirs(self.source)
        for n, c in (("agent.py", "# agent"), ("version.py", "AGENT_VERSION='2.0.0'"), ("requirements.txt", "flask\nrequests\n")):
            with open(os.path.join(self.source, n), "w") as f:
                f.write(c)
        os.environ["CSP_AGENT_CONFIG_PATH"] = os.path.join(self.install, "agent_config.json")

    def params(self, **kw):
        p = dict(csp_id="CSP-1", name="Sample CSP", server_url="https://s", install_dir=self.install, source_dir=self.source,
                 selections={}, confirmed={}, launcher_path=os.path.join(self.root, "startup.bat"), agent_version="2.0.0")
        p.update(kw)
        return p

    def run_install(self, params, **over):
        started = []
        kw = dict(provision=lambda *a, **k: "ok", start=lambda d: started.append(d), stop=lambda: None,
                  wait_ui=lambda t: True, report=lambda: (True, ""), summary=lambda: {"agent_version": "2.0.0"})
        kw.update(over)
        res = ic.run_install(params, lambda p, m: None, **kw)
        return res, started


class Registration(Base):
    def test_both_fields_are_mandatory(self):
        self.assertEqual(ic.verify_registration("https://s", "", "Name")["verdict"], "invalid")
        self.assertEqual(ic.verify_registration("https://s", "CSP-1", "  ")["verdict"], "invalid")

    def test_name_is_whitespace_normalised_before_asking(self):
        seen = {}

        class S:
            def post(self, url, json=None, **k):
                seen.update(json)

                class R:
                    status_code = 200

                    def json(self_):
                        return {"ok": True, "verdict": "ok", "message": ""}
                return R()
        ic.verify_registration("https://s", " CSP-1 ", "Sample   CSP", S())
        self.assertEqual(seen, {"csp_id": "CSP-1", "name": "Sample CSP"})


class Devices(Base):
    def test_generic_usb_is_never_preselected(self):
        scan = ic.scan_devices(fake_check(usb=[dev("USB Input Device")]))
        self.assertTrue(scan["microatm"][0]["generic"])
        self.assertIsNone(ic.suggest_defaults(scan)["microatm"])
        self.assertIn("not verified", ic.warning_for(scan["microatm"][0], "microatm"))

    def test_virtual_printers_are_hidden(self):
        scan = ic.scan_devices(fake_check(printers=[dev("Microsoft Print to PDF"), dev("Epson TM-T20")]))
        names = [c["name"] for c in scan["printer"]]
        self.assertNotIn("Microsoft Print to PDF", names)
        self.assertIn("Epson TM-T20", names)

    def test_windows_system_print_entries_are_hidden(self):
        scan = ic.scan_devices(fake_check(printers=[dev("Root Print Queue")]))
        self.assertEqual(scan["printer"], [])

    def test_a_saved_choice_that_is_still_present_wins(self):
        scan = ic.scan_devices(fake_check(usb=[dev("USB Input Device"), dev("Other Thing")]))
        self.assertEqual(ic.suggest_defaults(scan, {"microatm_name": "Other Thing"})["microatm"], "Other Thing")

    def test_scan_never_raises(self):
        def boom():
            raise RuntimeError("wmi down")
        scan = ic.scan_devices(boom)
        self.assertEqual(scan["printer"], [])

    def test_skipped_devices_are_saved_as_empty(self):
        u = ic.selection_to_config({"printer": None, "microatm": None, "biometric": None, "gps": None})
        self.assertEqual(u, {"printer_name": "", "microatm_name": "", "biometric_name": "", "gps_name": ""})

    def test_confirmed_device_keeps_hardware_identity(self):
        cand = {"name": "Scanner X", "instance": "USB\\VID_1234&PID_0001\\A"}
        u = ic.selection_to_config({"biometric": cand}, {"biometric": {"name": "Scanner X", "instance": "USB\\VID_1234&PID_0001\\A"}})
        self.assertEqual(u["biometric_confirmed"], "Scanner X")
        self.assertEqual(u["biometric_fingerprint"]["vid"], "1234")

    def test_unconfirmed_choice_has_no_confirmation(self):
        u = ic.selection_to_config({"gps": {"name": "GPS Y"}})
        self.assertNotIn("gps_confirmed", u)


class Install(Base):
    def test_new_pc_enrolls_and_reports_no_secret(self):
        calls = []
        import enroll
        orig = enroll.request_enrollment
        enroll.request_enrollment = lambda *a, **k: calls.append(a) or {"status": "pending", "message": ""}
        try:
            res, started = self.run_install(self.params(selections={"printer": {"name": "Epson TM-T20"}}))
        finally:
            enroll.request_enrollment = orig
        cfg = config_store.load()
        self.assertEqual(len(calls), 1)
        self.assertEqual(res["enrollment"], "pending")
        self.assertEqual(cfg["printer_name"], "Epson TM-T20")
        self.assertEqual(started, [self.install])
        self.assertNotIn(cfg["api_key"], json.dumps(dict(res), default=str))     # secrets never reach the summary
        with open(res["launcher"]) as f:
            self.assertIn("agent.py", f.read())

    def test_existing_install_keeps_identity_history_and_never_re_enrolls(self):
        os.makedirs(self.install)
        with open(os.path.join(self.install, "agent_config.json"), "w") as f:
            json.dump({"csp_id": "CSP-1", "api_key": "K" * 43, "server_url": "https://old", "printer_name": "Old Printer"}, f)
        with open(os.path.join(self.install, "diagnostic_history.json"), "w") as f:
            f.write('{"keep": true}')
        import enroll
        orig = enroll.request_enrollment
        enroll.request_enrollment = lambda *a, **k: self.fail("an established CSP must not enroll again")
        try:
            res, _ = self.run_install(self.params(selections={"printer": {"name": "New Printer"}}))
        finally:
            enroll.request_enrollment = orig
        cfg = config_store.load()
        self.assertEqual((cfg["api_key"], cfg["csp_id"], cfg["server_url"]), ("K" * 43, "CSP-1", "https://old"))
        self.assertEqual(cfg["printer_name"], "New Printer")
        with open(os.path.join(self.install, "diagnostic_history.json")) as f:
            self.assertEqual(f.read(), '{"keep": true}')
        self.assertEqual(res["enrollment"], "existing")

    def test_a_pc_registered_as_another_csp_is_refused(self):
        os.makedirs(self.install)
        with open(os.path.join(self.install, "agent_config.json"), "w") as f:
            json.dump({"csp_id": "OTHER-9", "api_key": "K" * 43}, f)
        with self.assertRaises(ic.InstallError) as cm:
            self.run_install(self.params())
        self.assertIn("different CSP", str(cm.exception))

    def test_copy_never_overwrites_this_pcs_own_data(self):
        os.makedirs(self.install)
        with open(os.path.join(self.source, "agent_config.json"), "w") as f:
            f.write("{}")
        with open(os.path.join(self.install, "agent_config.json"), "w") as f:
            f.write('{"mine": 1}')
        ic.copy_agent_files(self.source, self.install)
        with open(os.path.join(self.install, "agent_config.json")) as f:
            self.assertEqual(f.read(), '{"mine": 1}')
        self.assertTrue(os.path.isfile(os.path.join(self.install, "agent.py")))

    def test_low_disk_space_is_explained(self):
        with self.assertRaises(ic.InstallError) as cm:
            ic.check_disk_space(self.root, need_mb=10 ** 9)
        self.assertIn("not enough free space", str(cm.exception))

    def test_cancel_stops_cleanly(self):
        with self.assertRaises(ic.InstallError) as cm:
            ic.run_install(self.params(), lambda p, m: None, cancel=lambda: True, provision=lambda *a, **k: "ok")
        self.assertIn("cancelled", str(cm.exception))

    def test_agent_that_never_starts_gives_a_plain_message(self):
        import enroll
        orig = enroll.request_enrollment
        enroll.request_enrollment = lambda *a, **k: {"status": "pending", "message": ""}
        try:
            with self.assertRaises(ic.InstallError) as cm:
                self.run_install(self.params(), wait_ui=lambda t: False)
        finally:
            enroll.request_enrollment = orig
        self.assertIn("did not start", str(cm.exception))

    def test_rejected_registration_stops_with_servers_message(self):
        import enroll
        orig = enroll.request_enrollment
        enroll.request_enrollment = lambda *a, **k: {"status": "name_mismatch", "message": "The CSP name does not match this CSP code."}
        try:
            with self.assertRaises(ic.InstallError) as cm:
                self.run_install(self.params())
        finally:
            enroll.request_enrollment = orig
        self.assertIn("does not match", str(cm.exception))

    def test_installer_config_must_be_present_and_valid(self):
        with self.assertRaises(ic.InstallError):
            ic.load_installer_config(self.root)
        with open(os.path.join(self.root, "installer_config.json"), "w") as f:
            json.dump({"server_url": "ftp://x"}, f)
        with self.assertRaises(ic.InstallError):
            ic.load_installer_config(self.root)
        with open(os.path.join(self.root, "installer_config.json"), "w") as f:
            json.dump({"server_url": "https://x/"}, f)
        self.assertEqual(ic.load_installer_config(self.root)["server_url"], "https://x/")


if __name__ == "__main__":
    unittest.main()
