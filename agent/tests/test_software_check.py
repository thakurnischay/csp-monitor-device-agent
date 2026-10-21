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

    def test_the_query_only_reads(self):
        seen = []
        with mock.patch.object(dg, "_ps", side_effect=lambda s, t=45: seen.append(s) or {"running": False, "installed": False, "events": []}):
            dg.collect_software()
        script = seen[0]
        for forbidden in ("Stop-Process", "Start-Process", "Restart-", "Remove-", "Set-", "taskkill"):
            self.assertNotIn(forbidden, script)
        self.assertIn("jLLT", script)


if __name__ == "__main__":
    unittest.main()
