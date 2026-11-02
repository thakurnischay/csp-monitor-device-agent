"""The outcome ledger: records what happened to each diagnosed problem; never changes a diagnosis."""
import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import diagnostics as dg
import outcomes

T0 = datetime(2026, 10, 19, 10, 0, 0, tzinfo=timezone.utc)


def diag(*codes, state="DEGRADED", dev="printer", fix=None):
    return {dev: {"health_state": state, "findings": [
        {"code": c, "category": "service", "confidence": "confirmed", "severity": "high", "blocks": True,
         "auto_fix": {"type": "start_service", "service": "Spooler"} if fix else None} for c in codes]}}


class LedgerCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.led = outcomes.Ledger(os.path.join(self.dir, "o.json"))

    def data(self):
        with open(self.led.path) as f:
            return json.load(f)


class Episodes(LedgerCase):
    def test_a_problem_opens_once_and_counts_checks(self):
        for i in range(3):
            self.led.observe(diag("SPOOLER_STOPPED"), T0 + timedelta(minutes=5 * i))
        d = self.data()
        self.assertEqual(len(d["open"]), 1)
        self.assertEqual(d["open"]["printer:SPOOLER_STOPPED"]["checks"], 3)
        self.assertEqual(d["closed"], [])

    def test_it_closes_when_the_finding_goes_away_and_records_how_long(self):
        self.led.observe(diag("A"), T0)
        self.led.observe(diag(), T0 + timedelta(minutes=30))
        d = self.data()
        self.assertEqual(d["open"], {})
        self.assertEqual(d["closed"][0]["duration_s"], 1800)
        self.assertEqual(d["closed"][0]["resolution"], "self_resolved")

    def test_a_successful_fix_makes_it_resolved_after_fix(self):
        self.led.observe(diag("A", fix=True), T0)
        self.led.record_action("printer", "A", "start_service", "Spooler", "ok", T0 + timedelta(minutes=2))
        self.led.observe(diag(), T0 + timedelta(minutes=10))
        self.assertEqual(self.data()["closed"][0]["resolution"], "after_fix")

    def test_a_failed_fix_is_not_credited(self):
        self.led.observe(diag("A", fix=True), T0)
        self.led.record_action("printer", "A", "start_service", "Spooler", "failed", T0 + timedelta(minutes=2))
        self.led.observe(diag(), T0 + timedelta(minutes=10))
        self.assertEqual(self.data()["closed"][0]["resolution"], "self_resolved")

    def test_an_old_fix_is_not_credited_for_a_much_later_recovery(self):
        self.led.observe(diag("A"), T0)
        self.led.record_action("printer", "A", "start_service", "Spooler", "ok", T0)
        self.led.observe(diag("A"), T0 + timedelta(hours=30))
        self.led.observe(diag(), T0 + timedelta(hours=31))
        self.assertEqual(self.data()["closed"][0]["resolution"], "self_resolved")

    def test_coming_back_soon_marks_the_earlier_episode_as_recurred(self):
        self.led.observe(diag("A"), T0)
        self.led.observe(diag(), T0 + timedelta(hours=1))
        self.led.observe(diag("A"), T0 + timedelta(hours=5))
        d = self.data()
        self.assertTrue(d["closed"][0]["recurred"])
        self.assertEqual(d["open"]["printer:A"]["recurrence_of"], d["closed"][0]["id"])

    def test_coming_back_after_a_long_time_is_a_new_problem(self):
        self.led.observe(diag("A"), T0)
        self.led.observe(diag(), T0 + timedelta(hours=1))
        self.led.observe(diag("A"), T0 + timedelta(days=10))
        self.assertFalse(self.data()["closed"][0]["recurred"])

    def test_devices_are_independent(self):
        self.led.observe({**diag("A", dev="printer"), **diag("A", dev="gps")}, T0)
        self.led.observe({**diag("A", dev="printer"), **diag(dev="gps")}, T0 + timedelta(minutes=5))
        self.assertEqual(list(self.data()["open"]), ["printer:A"])
        self.assertEqual([e["device"] for e in self.data()["closed"]], ["gps"])

    def test_if_the_agent_could_not_diagnose_a_device_nothing_is_closed(self):
        self.led.observe(diag("A"), T0)
        self.led.observe({"printer": {"health_state": "DIAGNOSIS_REQUIRED", "findings": []}}, T0 + timedelta(minutes=5))
        self.assertIn("printer:A", self.data()["open"])
        self.assertEqual(self.data()["closed"], [])


class Summary(LedgerCase):
    def test_summary_numbers(self):
        # episode 1 (day 0): fix ok, resolves, never returns (stuck). episode 2 (day 10): fix ok, resolves, returns 2h later (recurred).
        # episode 3 (day 10 +2h, still open). Episode 4 (day 20): self-resolved.
        self.led.observe(diag("A", fix=True), T0)
        self.led.record_action("printer", "A", "start_service", "Spooler", "ok", T0 + timedelta(minutes=1))
        self.led.observe(diag(), T0 + timedelta(minutes=20))
        d10 = T0 + timedelta(days=10)
        self.led.observe(diag("A", fix=True), d10)
        self.led.record_action("printer", "A", "start_service", "Spooler", "ok", d10 + timedelta(minutes=1))
        self.led.observe(diag(), d10 + timedelta(minutes=20))
        self.led.observe(diag("A"), d10 + timedelta(hours=2))
        self.led.observe(diag(), d10 + timedelta(hours=2, minutes=20))
        s = {(r["device"], r["code"]): r for r in self.led.summary(d10 + timedelta(days=1), days=60)}[("printer", "A")]
        self.assertEqual(s["closed_episodes"], 3)
        self.assertEqual(s["fix_attempts"], 2)
        self.assertEqual(s["fix_then_resolved"], 2)
        self.assertEqual(s["fix_stuck"], 1)
        self.assertEqual(s["recurred"], 1)
        self.assertEqual(s["self_resolved"], 1)
        self.assertEqual(s["median_minutes_to_resolve"], 20.0)


class Reporting(LedgerCase):
    def test_recent_closed_rows_are_compact_and_age_out(self):
        self.led.observe(diag("A", fix=True), T0)
        self.led.record_action("printer", "A", "start_service", "Spooler", "ok", T0 + timedelta(minutes=1))
        self.led.observe(diag(), T0 + timedelta(minutes=20))
        rows = self.led.recent_closed(T0 + timedelta(days=1))
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["device"], rows[0]["code"], rows[0]["resolution"]), ("printer", "A", "after_fix"))
        self.assertEqual(rows[0]["actions"], [{"action": "start_service", "target": "Spooler", "result": "ok"}])
        self.assertNotIn("evidence", rows[0])
        self.assertEqual(self.led.recent_closed(T0 + timedelta(days=30)), [])

    def test_report_rows_never_raises(self):
        with mock.patch.object(outcomes, "for_history", side_effect=RuntimeError("boom")):
            self.assertEqual(outcomes.report_rows(), [])


class Robustness(LedgerCase):
    def test_corrupt_file_is_ignored_not_fatal(self):
        with open(self.led.path, "w") as f:
            f.write("{not json")
        self.led.observe(diag("A"), T0)
        self.assertEqual(len(self.data()["open"]), 1)

    def test_unwritable_location_never_raises(self):
        outcomes.Ledger(os.path.join(self.dir, "no", "such", "dir", "o.json")).observe(diag("A"), T0)

    def test_old_history_is_pruned(self):
        old = (T0 - timedelta(days=200)).isoformat()
        d = {"seq": 0, "open": {}, "closed": [{"id": i, "device": "x", "code": "c", "resolved_at": old, "duration_s": 1, "actions": [],
                                               "resolution": "self_resolved", "recurred": False} for i in range(5)]}
        with open(self.led.path, "w") as f:
            json.dump(d, f)
        self.led.observe(diag(), T0)
        self.assertEqual(self.data()["closed"], [])

    def test_the_file_holds_no_evidence_text(self):
        d = diag("A")
        d["printer"]["findings"][0]["evidence"] = ["SECRET-EVIDENCE for someone"]
        self.led.observe(d, T0)
        with open(self.led.path) as f:
            self.assertNotIn("SECRET-EVIDENCE", f.read())


class Wiring(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.health = {d: {"configured": False, "present": False, "ok": False, "status": "not configured"} for d in dg.DEVICES}
        self.ev = {"services": [], "drivers": [], "events": [], "printers": [], "jobs": [], "software": None}

    def test_analyze_records_next_to_the_history_file_and_does_not_change_the_result(self):
        a = dg.analyze(self.health, {}, history=dg.History(os.path.join(self.tmp, "h1.json")), evidence=self.ev, now=T0)
        with mock.patch.object(outcomes, "for_history", side_effect=RuntimeError("boom")):       # a broken ledger changes nothing
            b = dg.analyze(self.health, {}, history=dg.History(os.path.join(self.tmp, "h2.json")), evidence=self.ev, now=T0)
        self.assertEqual(a, b)
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "diagnostic_outcomes.json")))

    def test_start_service_attaches_the_result_to_the_open_episode(self):
        hist = dg.History(os.path.join(self.tmp, "h.json"))
        led = outcomes.for_history(hist)
        diagnosis = {"printer": {"health_state": "DEGRADED", "findings": [
            {"code": "SPOOLER_STOPPED", "category": "service", "confidence": "confirmed", "severity": "high", "blocks": True,
             "auto_fix": {"type": "start_service", "service": "Spooler"}}]}}
        led.observe(diagnosis, T0)
        out = dg.start_service("Spooler", diagnosis, history=hist, now=T0 + timedelta(minutes=1), runner=lambda s: (True, ""))
        self.assertTrue(out["ok"])
        with open(led.path) as f:
            ep = json.load(f)["open"]["printer:SPOOLER_STOPPED"]
        self.assertEqual(ep["actions"][0]["result"], "ok")


if __name__ == "__main__":
    unittest.main()
