"""Micro-ATM vendor software (Ingenico LLT): report crashes and hangs from the Windows event log. Report-only:
"not open" is information, never a fault, and nothing is ever restarted by the agent."""
import json
import os
import sys
import unittest
from datetime import timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import diagnostics as dg
from tests.test_diagnostics import Base, NOW, ev, health, row, status

ATM = r"USB\VID_0B00&PID_0071\5&AAAA&0&1"


def atm_health(present=True):
    r = row("Ingenico Tetra", ATM, maker="Ingenico")
    return health(microatm=status("microatm", r, present=present, ok=present))


def program(installed=True, running=True, crashes=()):
    return {"name": "Ingenico LLT", "process": "jLLT", "installed": installed, "running": running,
            "started": [NOW - timedelta(hours=3)] if running else [],
            "crashes": [{"id": i, "t": NOW - timedelta(hours=h)} for i, h in crashes]}


def evidence(*programs):
    e = ev()
    e["software"] = list(programs)
    return e


class SoftwareCheck(Base):
    def dx(self, programs=None, **kw):
        e = evidence(*programs) if programs is not None else ev()
        return self.run_dx(atm_health(**kw), e)["microatm"]

    def test_pcs_without_the_software_hear_nothing_about_it(self):
        for e in (ev(), evidence(), evidence(program(installed=False, running=False))):
            d = self.run_dx(atm_health(), e)["microatm"]
            self.assertEqual(d["findings"], [])
            self.assertFalse([n for n in d["notes"] if "LLT" in n])
            self.assertFalse([c for c in d["checked"] if "LLT" in c])

    def test_installed_and_running_with_no_crashes_is_healthy_and_says_what_was_checked(self):
        d = self.dx([program()])
        self.assertEqual((d["health_state"], d["findings"]), ("HEALTHY", []))
        self.assertTrue([c for c in d["checked"] if "Ingenico LLT" in c])

    def test_not_open_is_information_never_a_fault(self):
        d = self.dx([program(running=False)])
        self.assertEqual((d["health_state"], d["findings"]), ("HEALTHY", []))
        self.assertTrue([n for n in d["notes"] if "not open right now" in n])

    def test_one_recent_crash_is_reported_with_windows_evidence(self):
        d = self.dx([program(crashes=[(1000, 2)])])
        f = d["findings"][0]
        self.assertEqual((f["code"], f["state"], f["confidence"]), ("SOFTWARE_CRASHED", "SOFTWARE_ISSUE", "confirmed"))
        self.assertEqual(d["health_state"], "SOFTWARE_ISSUE")
        self.assertTrue([e for e in f["evidence"] if "1 crash" in e])
        self.assertTrue([a for a in f["actions"] if "Do not repeat a payment" in a])

    def test_a_crash_while_the_software_is_closed_matters_more_than_while_it_is_open(self):
        open_ = self.dx([program(running=True, crashes=[(1000, 2)])])["findings"][0]["severity"]
        closed = self.dx([program(running=False, crashes=[(1000, 2)])])["findings"][0]["severity"]
        self.assertEqual(open_, "low")
        self.assertIn(closed, ("medium", "high", "critical"))

    def test_three_crashes_in_a_week_is_flagged_as_unstable(self):
        d = self.dx([program(crashes=[(1000, 5), (1002, 50), (1000, 120)])])
        f = d["findings"][0]
        self.assertEqual((f["code"], f["state"]), ("SOFTWARE_UNSTABLE", "SOFTWARE_ISSUE"))
        self.assertIn(f["severity"], ("medium", "high", "critical"))
        self.assertTrue([e for e in f["evidence"] if "2 crash(es) and 1 hang(s)" in e])
        self.assertTrue([a for a in f["actions"] if "official SBI / Ingenico package" in a])

    def test_old_crashes_alone_are_only_a_note(self):
        d = self.dx([program(crashes=[(1000, 72), (1002, 100)])])
        self.assertEqual((d["health_state"], d["findings"]), ("HEALTHY", []))
        self.assertTrue([n for n in d["notes"] if "none in the last 24 hours" in n])

    def test_a_crashing_program_is_reported_even_when_the_device_is_unplugged(self):
        d = self.dx([program(crashes=[(1000, 2)])], present=False)
        self.assertEqual({f["code"] for f in d["findings"]}, {"NOT_CONNECTED", "SOFTWARE_CRASHED"})

    def test_software_findings_never_offer_an_automatic_fix(self):
        for crashes in ([(1000, 2)], [(1000, 5), (1002, 50), (1000, 120)]):
            for f in self.dx([program(crashes=crashes)])["findings"]:
                self.assertIsNone(f["auto_fix"])

    def test_the_result_is_json_serialisable_and_other_devices_are_untouched(self):
        out = self.run_dx(atm_health(), evidence(program(crashes=[(1000, 2)])))
        json.dumps(out)
        self.assertEqual(out["biometric"]["findings"], [])


class Collector(unittest.TestCase):
    def test_parsing_what_windows_returns(self):
        reply = {"running": True, "started": ["2026-10-19T07:00:00+00:00"], "installed": True,
                 "events": [{"I": 1000, "T": "2026-10-19T09:00:00+00:00"}, {"I": 1002, "T": "2026-10-18T09:00:00+00:00"}]}
        with mock.patch.object(dg, "_ps", return_value=reply):
            out = dg.collect_software()
        p = out[0]
        self.assertEqual((p["name"], p["installed"], p["running"], len(p["crashes"])), ("Ingenico LLT", True, True, 2))
        self.assertEqual([c["id"] for c in p["crashes"]], [1000, 1002])
        self.assertTrue(p["crashes"][0]["t"])

    def test_a_single_event_is_not_lost_when_windows_returns_it_as_an_object(self):
        reply = {"running": False, "started": [], "installed": True, "events": {"I": 1000, "T": "2026-10-19T09:00:00+00:00"}}
        with mock.patch.object(dg, "_ps", return_value=reply):
            self.assertEqual(len(dg.collect_software()[0]["crashes"]), 1)

    def test_a_failed_query_is_unknown_not_healthy(self):
        with mock.patch.object(dg, "_ps", return_value=None):
            self.assertIsNone(dg.collect_software())

    def test_every_watched_program_is_queried_and_returns_its_version(self):
        reply = {"running": False, "started": [], "installed": True, "version": "3.12", "source": "installed programs", "events": []}
        with mock.patch.object(dg, "_ps", return_value=reply):
            out = dg.collect_software()
        self.assertEqual([p["key"] for p in out], ["llt", "usb_drivers", "gps_driver", "morpho_rd"])
        self.assertEqual([p["version"] for p in out], ["3.12"] * 4)

    def test_the_driver_query_has_no_process_or_crash_part(self):
        script = dg._software_script(dg.MICROATM_SOFTWARE[1])
        self.assertNotIn("Get-WinEvent", script)
        self.assertIn("Win32_PnPSignedDriver", script)

    def test_a_watched_name_with_unsafe_characters_is_never_put_into_a_command(self):
        bad = dict(dg.MICROATM_SOFTWARE[0], process="x'; Remove-Item C:\ ; '")
        self.assertIsNone(dg._software_script(bad))
        bad = dict(dg.MICROATM_SOFTWARE[0], uninstall="a'b")
        self.assertIsNone(dg._software_script(bad))

    def test_the_query_only_reads(self):
        seen = []
        with mock.patch.object(dg, "_ps", side_effect=lambda s, t=45: seen.append(s) or {"running": False, "installed": False, "events": []}):
            dg.collect_software()
        script = seen[0]
        for forbidden in ("Stop-Process", "Start-Process", "Restart-", "Remove-", "Set-", "taskkill"):
            self.assertNotIn(forbidden, script)
        self.assertIn("jLLT", script)


def usb_drivers(installed=True, version="3.12"):
    return {"key": "usb_drivers", "name": "Ingenico USB Drivers", "process": "", "installed": installed,
            "version": version if installed else "", "source": "installed programs", "running": False, "started": [], "crashes": []}


def llt(version="5.2.4", installed=True, **kw):
    p = program(installed=installed, **kw)
    p.update(key="llt", version=version if installed else "", source="installed programs")
    return p


class Inventory(Base):
    """The agent reports FACTS about the software; the server decides whether they are acceptable."""
    def dx(self, programs, make="Ingenico", name="Ingenico Move 2500", instance=ATM):
        r = row(name, instance, maker=make)
        h = health(microatm=status("microatm", r))
        return self.run_dx(h, evidence(*programs))["microatm"]

    def test_the_inventory_carries_versions_open_state_and_crash_counts(self):
        d = self.dx([llt("5.2.1", crashes=[(1000, 2), (1002, 50)]), usb_drivers(version="3.10")])
        by = {p["key"]: p for p in d["software"]}
        self.assertEqual((by["llt"]["installed"], by["llt"]["version"], by["llt"]["running"]), (True, "5.2.1", True))
        self.assertEqual((by["llt"]["crashes_24h"], by["llt"]["crashes_7d"]), (1, 2))
        self.assertTrue(by["llt"]["last_crash_at"])
        self.assertEqual((by["usb_drivers"]["version"], by["usb_drivers"]["running"]), ("3.10", None))   # a driver is never "open"
        json.dumps(d)

    def test_it_says_when_the_software_is_not_installed(self):
        by = {p["key"]: p for p in self.dx([llt(installed=False), usb_drivers(installed=False)])["software"]}
        self.assertFalse(by["llt"]["installed"])
        self.assertFalse(by["usb_drivers"]["installed"])
        self.assertEqual(by["llt"]["version"], "")

    def test_the_software_is_expected_only_for_an_ingenico_micro_atm(self):
        self.assertTrue(all(p["expected"] for p in self.dx([llt(), usb_drivers()])["software"]))
        other = self.dx([llt(installed=False)], make="PAX Technology", name="PAX D180", instance=r"USB\VID_1234&PID_0001")
        self.assertFalse(any(p["expected"] for p in other["software"]))

    def test_an_ingenico_usb_vendor_id_alone_marks_it_expected(self):
        d = self.dx([llt()], make="", name="USB Device", instance=r"USB\VID_0B00&PID_0071&1")
        self.assertTrue(all(p["expected"] for p in d["software"]))

    def test_no_inventory_when_windows_could_not_be_asked(self):
        e = ev()
        e["software"] = None
        d = self.run_dx(health(microatm=status("microatm", row("Ingenico Tetra", ATM, maker="Ingenico"))), e)["microatm"]
        self.assertNotIn("software", d)

    def test_only_the_micro_atm_carries_an_inventory(self):
        out = self.run_dx(health(microatm=status("microatm", row("Ingenico Tetra", ATM, maker="Ingenico"))), evidence(llt(), usb_drivers()))
        for dev in ("printer", "biometric", "gps"):
            self.assertNotIn("software", out[dev])


CH340 = r"USB\VID_1A86&PID_7523\5&BBBB&0&2"
MORPHO = r"USB\VID_079B&PID_0047\5&CCCC&0&3"


def gps_driver(version="3.8.2023.2", installed=True):
    return {"key": "gps_driver", "name": "GPS dongle driver (WCH CH341)", "process": "", "installed": installed,
            "version": version if installed else "", "source": "driver file" if installed else "", "running": False, "started": [], "crashes": []}


def morpho_rd(version="5.0.0.10", installed=True):
    return {"key": "morpho_rd", "name": "Morpho / IDEMIA RD Service", "process": "", "installed": installed,
            "version": version if installed else "", "source": "service program" if installed else "", "running": installed, "started": [], "crashes": []}


class GpsAndBiometricInventory(Base):
    """GPS dongle driver and the fingerprint scanner's RD service are watched the same way: facts out, server judges."""
    def gps(self, programs, name="USB-SERIAL CH340 (COM3)", maker="wch.cn", instance=CH340):
        r = row(name, instance, maker=maker)
        return self.run_dx(health(gps=status("gps", r)), evidence(*programs))["gps"]

    def bio(self, programs, name="MorphoSmart MSO 1300", maker="Morpho", instance=MORPHO, **extra):
        r = row(name, instance, maker=maker)
        return self.run_dx(health(biometric=status("biometric", r, **extra)), evidence(*programs))["biometric"]

    def test_the_gps_driver_version_is_reported_for_a_ch340_dongle(self):
        d = self.gps([gps_driver("3.8.2023.2"), llt(), usb_drivers()])
        self.assertEqual([p["key"] for p in d["software"]], ["gps_driver"])                  # only this device's software
        p = d["software"][0]
        self.assertEqual((p["installed"], p["version"], p["expected"], p["running"]), (True, "3.8.2023.2", True, None))

    def test_a_gps_dongle_with_a_different_chip_is_not_expected_to_have_the_ch340_driver(self):
        d = self.gps([gps_driver(installed=False)], name="u-blox 7 GPS", maker="u-blox AG", instance=r"USB\VID_1546&PID_01A7\1")
        self.assertFalse(d["software"][0]["expected"])

    def test_a_missing_gps_driver_is_reported_as_not_installed(self):
        p = self.gps([gps_driver(installed=False)])["software"][0]
        self.assertEqual((p["installed"], p["version"], p["expected"]), (False, "", True))

    def test_the_morpho_rd_service_version_and_running_state(self):
        p = self.bio([morpho_rd("5.0.0.10")])["software"][0]
        self.assertEqual((p["key"], p["installed"], p["version"], p["running"], p["expected"]), ("morpho_rd", True, "5.0.0.10", True, True))

    def test_a_rd_service_that_answers_counts_as_installed_even_if_the_lookup_missed_it(self):
        answered = [{"status": "READY", "info": "Morpho_RD_Service", "port": 11100}]
        p = self.bio([morpho_rd(installed=False)], rd=answered)["software"][0]
        self.assertEqual((p["installed"], p["running"], p["source"]), (True, True, "RD service answered"))

    def test_a_morpho_scanner_with_no_rd_service_at_all_is_reported_as_not_installed(self):
        p = self.bio([morpho_rd(installed=False)], rd=[])["software"][0]
        self.assertEqual((p["installed"], p["expected"]), (False, True))

    def test_other_scanner_makes_are_not_expected_to_have_the_morpho_service(self):
        p = self.bio([morpho_rd(installed=False)], name="Mantra MFS110", maker="Mantra Softech", instance=r"HID\VID_2C0F&PID_1204\1")["software"][0]
        self.assertFalse(p["expected"])

    def test_the_morpho_answer_of_another_make_does_not_count(self):
        answered = [{"status": "READY", "info": "Mantra_RD", "port": 11100}]
        p = self.bio([morpho_rd(installed=False)], rd=answered)["software"][0]
        self.assertFalse(p["installed"])

    def test_the_whole_result_is_json_serialisable(self):
        json.dumps(self.bio([morpho_rd()]))
        json.dumps(self.gps([gps_driver()]))


if __name__ == "__main__":
    unittest.main()
