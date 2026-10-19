"""Root-cause diagnosis: every scenario from the acceptance list, driven by
realistic evidence (no hardware needed - the evidence structures are exactly
what the real Windows queries return)."""
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import device_health as dh
import diagnostics as dg
import impact_map

NOW = datetime(2026, 10, 19, 10, 0, 0, tzinfo=timezone.utc)
SCN = r"HID\VID_2C0F&PID_1204&MI_00\6&AAAA&0&0000"


def row(name="Mantra MFS110", instance=SCN, cm=0, problem="", status="OK", maker="Mantra Softech India Pvt Ltd"):
    return dh._enrich_device(name, {"Status": status, "Class": "HIDClass", "InstanceId": instance,
                                    "Manufacturer": maker, "ConfigManagerErrorCode": cm,
                                    "ProblemDescription": problem})


def status(dev, r=None, ok=True, present=True, configured=True, verified=True, recognised=True, **extra):
    r = r or row()
    d = {"configured": configured, "present": present, "ok": ok, "status": "OK", "resolved_name": r["name"],
         "_row": r, "recognised": recognised}
    if verified is not None:
        d["verified"] = verified
    d.update(extra)
    return d


def not_set_up():
    return {"configured": False, "present": False, "ok": False, "status": "not configured"}


def health(**devs):
    h = {d: not_set_up() for d in dg.DEVICES}
    h.update(devs)
    h["software"] = {"configured": False, "running": False}
    return h


def svc(name="MantraRDService", display="Mantra RD Service", state="Running", start="Auto",
        path=r"C:\Program Files\Mantra\rd.exe", exit_code=0):
    return {"name": name, "display": display, "state": state, "start": start, "exit": exit_code, "path": path}


def ev(services=(), drivers=(), events=(), printers=(), jobs=()):
    return {"services": list(services), "drivers": list(drivers), "events": list(events),
            "printers": list(printers), "jobs": list(jobs)}


def crash(name="MantraRDService", minutes_ago=30):
    return {"provider": "Service Control Manager", "id": 7031,
            "t": NOW - timedelta(minutes=minutes_ago),
            "msg": f"The Mantra RD Service ({name}) service terminated unexpectedly."}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.hist = dg.History(os.path.join(self.tmp, "h.json"))

    def run_dx(self, h, evidence=None, cfg=None, functional=None, now=NOW, impact=None):
        return dg.analyze(h, cfg or {}, functional, history=self.hist,
                          evidence=evidence if evidence is not None else ev(), impact=impact, now=now)

    @staticmethod
    def codes(diag, dev):
        return [f["code"] for f in diag[dev]["findings"]]


class HealthyAndShape(Base):
    def test_a_healthy_device_has_no_findings_and_says_what_was_checked(self):
        d = self.run_dx(health(biometric=status("biometric", rd=[{"status": "READY", "info": "Mantra", "port": 11100}])),
                        ev(services=[svc()]))["biometric"]
        self.assertEqual((d["health_state"], d["severity"], d["findings"]), ("HEALTHY", "none", []))
        self.assertIn("Windows device status and error code", d["checked"])
        self.assertTrue(d["not_checked"])                       # limits are stated, not hidden
        self.assertIsNotNone(d["last_healthy_at"])

    def test_not_set_up_devices_have_no_diagnosis(self):
        d = self.run_dx(health())["gps"]
        self.assertEqual((d["health_state"], d["findings"]), ("NOT_SET_UP", []))

    def test_the_result_is_json_serialisable(self):
        d = self.run_dx(health(biometric=status("biometric", row(cm=28, problem="drivers not installed (Code 28).")),
                               printer=status("printer", row("EPSON", r"USB\X\1"))), ev(drivers=[]))
        json.dumps(d)

    def test_revenue_is_never_invented(self):
        for dev in dg.DEVICES:
            self.assertIn("Not calculable", self.run_dx(health())[dev]["impact"]["revenue"])
            self.assertNotRegex(self.run_dx(health())[dev]["impact"]["revenue"], r"\d")


class DriverAndHardware(Base):
    def test_driver_failure_is_found_with_windows_own_evidence_and_vendor_steps(self):
        r = row(cm=28, problem="The drivers for this device are not installed. (Code 28).", status="Error")
        d = self.run_dx(health(biometric=status("biometric", r, ok=False)),
                        ev(drivers=[{"device_id": SCN, "name": "Mantra MFS110", "version": "1.2.3.4",
                                     "date": NOW - timedelta(days=900), "maker": "Mantra", "signed": True}]))["biometric"]
        f = d["findings"][0]
        self.assertEqual((f["code"], f["state"], f["confidence"]), ("CM_28", "DRIVER_ISSUE", "confirmed"))
        self.assertEqual(d["health_state"], "DRIVER_ISSUE")
        self.assertTrue(any("error code 28" in e for e in f["evidence"]))
        self.assertTrue(any("version 1.2.3.4" in e for e in f["evidence"]))
        self.assertTrue(any("vendor-approved driver" in a for a in f["actions"]))
        self.assertTrue(f["operator_action"])

    def test_every_documented_driver_code_maps_to_a_driver_issue(self):
        for code in (1, 18, 19, 28, 31, 37, 39, 40, 48, 52):
            d = self.run_dx(health(biometric=status("biometric", row(cm=code), ok=False)))["biometric"]
            self.assertEqual(d["health_state"], "DRIVER_ISSUE", code)

    def test_a_disabled_device_is_a_setting_not_a_fault(self):
        d = self.run_dx(health(biometric=status("biometric", row(cm=22, problem="This device is disabled. (Code 22)."), ok=False)))["biometric"]
        self.assertEqual(d["health_state"], "SOFTWARE_ISSUE")
        self.assertTrue(any("Enable device" in a for a in d["findings"][0]["actions"]))

    def test_code_43_is_suspected_hardware_and_replacement_is_conditional(self):
        d = self.run_dx(health(biometric=status("biometric", row(cm=43, problem="Windows has stopped this device"), ok=False)))["biometric"]
        f = d["findings"][0]
        self.assertEqual((d["health_state"], f["confidence"]), ("SUSPECTED_HARDWARE", "likely"))
        text = " ".join(f["actions"])
        self.assertIn("different USB port", text)
        self.assertIn("another PC", text)
        self.assertIn("replacement is justified only after", text)

    def test_an_unknown_error_code_asks_for_diagnosis_instead_of_guessing(self):
        d = self.run_dx(health(biometric=status("biometric", row(cm=99, problem="Something odd (Code 99)."), ok=False)))["biometric"]
        self.assertEqual(d["health_state"], "DIAGNOSIS_REQUIRED")
        self.assertEqual(d["findings"][0]["confidence"], "possible")
        self.assertNotEqual(d["findings"][0]["state"], "SUSPECTED_HARDWARE")

    def test_an_unsigned_driver_alone_is_only_a_note(self):
        d = self.run_dx(health(biometric=status("biometric")),
                        ev(drivers=[{"device_id": SCN, "name": "x", "version": "1", "date": None, "maker": "", "signed": False}]))["biometric"]
        self.assertEqual(d["health_state"], "HEALTHY")
        self.assertTrue(any("not digitally signed" in n for n in d["notes"]))


class ScannerServices(Base):
    def test_rd_service_stopped_is_a_software_issue_not_a_reconnect_request(self):
        d = self.run_dx(health(biometric=status("biometric", rd=[])), ev(services=[svc(state="Stopped")]))["biometric"]
        f = d["findings"][0]
        self.assertEqual((f["code"], f["state"], f["confidence"]), ("SERVICE_STOPPED:MantraRDService", "SERVICE_UNAVAILABLE", "confirmed"))
        self.assertIn("Do NOT reconnect", f["actions"][0])
        self.assertEqual(f["auto_fix"]["type"], "start_service")
        self.assertEqual(d["severity"], "high")                  # confirmed, workflow needs it, no workaround
        self.assertTrue(any("Windows lists the fingerprint scanner as connected" in e for e in f["evidence"]))

    def test_a_manually_started_service_that_is_stopped_is_not_a_fault(self):
        d = self.run_dx(health(biometric=status("biometric", rd=[{"status": "READY", "info": "x", "port": 11100}])),
                        ev(services=[svc(state="Stopped", start="Manual")]))["biometric"]
        self.assertEqual(d["findings"], [])

    def test_windows_own_biometric_service_is_never_mistaken_for_the_scanner_software(self):
        # both names contain the keyword "fingerprint", so only the exclusion rule keeps them out
        by_path = svc("WbioSrvc", "Fingerprint Service", "Stopped", "Auto", r"C:\Windows\system32\svchost.exe -k WbioSvcGroup")
        by_name = svc("WinFp", "Windows Fingerprint Reader", "Stopped", "Auto", r"C:\Program Files\Other\x.exe")
        rd_ready = [{"status": "READY", "info": "x", "port": 11100}]
        for windows_service in (by_path, by_name):
            d = self.run_dx(health(biometric=status("biometric", rd=rd_ready)), ev(services=[windows_service]))["biometric"]
            self.assertEqual(d["findings"], [], windows_service["display"])
        vendor = svc("FpVendor", "Acme Fingerprint Service", "Stopped", "Auto", r"C:\Program Files\Acme\fp.exe")
        d = self.run_dx(health(biometric=status("biometric", rd=rd_ready)), ev(services=[vendor]))["biometric"]
        self.assertEqual(d["findings"][0]["code"], "SERVICE_STOPPED:FpVendor")     # a real vendor service still counts

    def test_a_running_service_that_does_not_answer_is_found(self):
        d = self.run_dx(health(biometric=status("biometric", rd=[])), ev(services=[svc(state="Running")]))["biometric"]
        f = d["findings"][0]
        self.assertEqual((f["code"], f["state"], f["confidence"]), ("RD_NOT_RESPONDING", "SERVICE_UNAVAILABLE", "likely"))
        self.assertEqual(d["severity"], "medium")                # likely, not confirmed

    def test_rd_notready_is_a_note_not_a_fault(self):
        d = self.run_dx(health(biometric=status("biometric", rd=[{"status": "NOTREADY", "info": "Morpho", "port": 11100}])),
                        ev(services=[svc()]))["biometric"]
        self.assertEqual((d["health_state"], d["findings"]), ("HEALTHY", []))
        self.assertTrue(any("NOT READY" in n for n in d["notes"]))

    def test_no_rd_service_anywhere_is_a_low_diagnosis_required_lead(self):
        d = self.run_dx(health(biometric=status("biometric", rd=[])), ev(services=[]))["biometric"]
        f = d["findings"][0]
        self.assertEqual((f["code"], f["confidence"], d["severity"]), ("RD_NOT_FOUND", "possible", "low"))
        self.assertEqual(d["health_state"], "DIAGNOSIS_REQUIRED")
        self.assertIn("no action is needed", " ".join(f["actions"]))

    def test_repeated_service_crashes_are_a_confirmed_software_issue(self):
        d = self.run_dx(health(biometric=status("biometric", rd=[{"status": "READY", "info": "x", "port": 1}])),
                        ev(services=[svc()], events=[crash(minutes_ago=30), crash(minutes_ago=300)]))["biometric"]
        f = [x for x in d["findings"] if x["code"].startswith("SERVICE_CRASHING")][0]
        self.assertEqual((f["state"], f["confidence"]), ("SOFTWARE_ISSUE", "confirmed"))
        self.assertTrue(any("2 Windows service error event" in e for e in f["evidence"]))

    def test_a_single_crash_is_not_enough(self):
        d = self.run_dx(health(biometric=status("biometric", rd=[{"status": "READY", "info": "x", "port": 1}])),
                        ev(services=[svc()], events=[crash()]))["biometric"]
        self.assertEqual(d["findings"], [])

    def test_after_a_failed_start_attempt_it_escalates_instead_of_offering_another_restart(self):
        self.hist.record_action("biometric", "start_service", "MantraRDService", "Access is denied", NOW - timedelta(hours=1))
        f = self.run_dx(health(biometric=status("biometric", rd=[])), ev(services=[svc(state="Stopped")]))["biometric"]["findings"][0]
        self.assertIsNone(f["auto_fix"])
        text = " ".join(f["actions"])
        self.assertIn("already tried", text)
        self.assertIn("Event Viewer", text)
        self.assertIn("Do not keep restarting", text)


class Connectivity(Base):
    def test_not_connected_after_working_recently_is_medium(self):
        self.hist.observe("biometric", True, NOW - timedelta(hours=5))
        d = self.run_dx(health(biometric=status("biometric", present=False, ok=False)))["biometric"]
        self.assertEqual((d["health_state"], d["severity"]), ("NOT_CONNECTED", "medium"))
        self.assertTrue(any("last detected" in e for e in d["findings"][0]["evidence"]))

    def test_never_seen_is_low(self):
        d = self.run_dx(health(biometric=status("biometric", present=False, ok=False)))["biometric"]
        self.assertEqual((d["health_state"], d["severity"]), ("NOT_CONNECTED", "low"))

    def test_repeated_disconnects_are_suspected_hardware_with_conditional_replacement(self):
        t = NOW - timedelta(hours=10)
        for i in range(4):                                    # 4 drop-outs in 10 hours
            self.hist.observe("biometric", True, t + timedelta(hours=i * 2))
            self.hist.observe("biometric", False, t + timedelta(hours=i * 2, minutes=30))
        self.hist.observe("biometric", True, NOW - timedelta(minutes=5))
        d = self.run_dx(health(biometric=status("biometric")))["biometric"]
        f = [x for x in d["findings"] if x["code"] == "REPEATED_DISCONNECTS"][0]
        self.assertEqual((f["state"], f["confidence"]), ("SUSPECTED_HARDWARE", "likely"))
        self.assertIn("replacement is justified only once", " ".join(f["actions"]))

    def test_two_disconnects_are_not_enough(self):
        t = NOW - timedelta(hours=10)
        for i in range(2):
            self.hist.observe("biometric", True, t + timedelta(hours=i * 2))
            self.hist.observe("biometric", False, t + timedelta(hours=i * 2, minutes=30))
        self.hist.observe("biometric", True, NOW)
        d = self.run_dx(health(biometric=status("biometric", rd=[{"status": "READY", "info": "x", "port": 1}])))["biometric"]
        self.assertEqual(d["findings"], [])

    def test_old_disconnects_outside_24_hours_do_not_count(self):
        t = NOW - timedelta(days=3)
        for i in range(5):
            self.hist.observe("biometric", True, t + timedelta(minutes=i * 20))
            self.hist.observe("biometric", False, t + timedelta(minutes=i * 20 + 10))
        self.hist.observe("biometric", True, NOW - timedelta(days=2))
        self.assertEqual(self.hist.flaps("biometric", NOW), 0)


class MicroAtmAndGps(Base):
    def test_micro_atm_application_not_running_is_a_confirmed_software_issue(self):
        h = health(microatm=status("microatm", row("Ingenico iCT250", r"USB\VID_0B00&PID_0001\1")))
        h["software"] = {"configured": True, "running": False}
        d = self.run_dx(h, cfg={"software_process": "microatm.exe"})["microatm"]
        f = d["findings"][0]
        self.assertEqual((f["code"], f["state"], f["confidence"]), ("APP_NOT_RUNNING", "SOFTWARE_ISSUE", "confirmed"))
        self.assertEqual(d["severity"], "high")

    def test_micro_atm_states_what_it_cannot_check(self):
        d = self.run_dx(health(microatm=status("microatm", row("Ingenico", r"USB\VID_0B00&PID_1\1"))))["microatm"]
        self.assertTrue(any("vendor's SDK" in n for n in d["not_checked"]))
        self.assertTrue(any("live financial transaction is never used" in n for n in d["not_checked"]))

    def test_gps_impact_is_not_established_and_severity_is_capped(self):
        d = self.run_dx(health(gps=status("gps", row("USB-SERIAL CH340 (COM7)", r"USB\VID_1A86&PID_7523\5&1&0&2", cm=10), ok=False)))["gps"]
        self.assertEqual(d["findings"][0]["confidence"], "likely")
        self.assertNotIn(d["severity"], ("critical", "high"))
        self.assertIn("not established", d["impact"]["assessment"].lower())

    def test_a_silent_gps_port_is_a_note_never_a_fault(self):
        d = self.run_dx(health(gps=status("gps", row("USB-SERIAL CH340 (COM7)", r"USB\VID_1A86&PID_7523\5&1&0&2"),
                                           gps_check="SILENT")))["gps"]
        self.assertEqual((d["health_state"], d["findings"]), ("HEALTHY", []))
        self.assertTrue(any("not treated as a fault" in n for n in d["notes"]))


PRN = {"Name": "TVS MSP 250", "PrinterStatus": 3, "DetectedErrorState": 2, "WorkOffline": False}


def printer_status():
    r = {"name": "TVS MSP 250", "instance": "", "cm_code": 0, "problem": "", "status": "idle (ready)"}
    return status("printer", r, recognised=True)


class PrinterRules(Base):
    def run_printer(self, printers=(PRN,), services=None, jobs=(), functional=None, now=NOW):
        services = services if services is not None else [svc("Spooler", "Print Spooler", "Running", "Auto", r"C:\Windows\System32\spoolsv.exe")]
        return self.run_dx(health(printer=printer_status()), ev(services=services, printers=printers, jobs=jobs),
                           functional=functional, now=now)["printer"]

    def test_a_healthy_printer(self):
        self.assertEqual(self.run_printer()["health_state"], "HEALTHY")

    def test_spooler_stopped_is_a_service_issue_with_an_approved_fix(self):
        d = self.run_printer(services=[svc("Spooler", "Print Spooler", "Stopped", "Auto", r"C:\Windows\System32\spoolsv.exe")])
        f = d["findings"][0]
        self.assertEqual((f["code"], f["state"]), ("SPOOLER_STOPPED", "SERVICE_UNAVAILABLE"))
        self.assertEqual(f["auto_fix"], {"type": "start_service", "service": "Spooler", "label": "Print Spooler"})

    def test_out_of_paper_is_a_confirmed_consumable_condition_not_a_driver_fault(self):
        d = self.run_printer(printers=[dict(PRN, DetectedErrorState=4)])
        f = d["findings"][0]
        self.assertEqual((f["code"], f["confidence"], f["category"]), ("PRINTER_4", "confirmed", "consumable"))
        self.assertIn("Load paper", f["actions"][0])
        self.assertEqual(d["severity"], "medium")                # a workaround exists (receipts)

    def test_jam_door_and_offline_states_have_specific_steps(self):
        for es, word in ((8, "jam"), (7, "door"), (9, "OFFLINE")):
            d = self.run_printer(printers=[dict(PRN, DetectedErrorState=es)])
            self.assertIn(word.lower(), d["findings"][0]["title"].lower(), es)

    def test_low_paper_is_low_priority(self):
        d = self.run_printer(printers=[dict(PRN, DetectedErrorState=3)])
        self.assertEqual(d["severity"], "low")

    def test_use_printer_offline_is_found(self):
        d = self.run_printer(printers=[dict(PRN, WorkOffline=True)])
        self.assertEqual(d["findings"][0]["code"], "PRINTER_WORK_OFFLINE")

    def test_a_stuck_print_queue_is_found_only_for_old_jobs(self):
        old = {"Name": "TVS MSP 250, 17", "JobStatus": "Error", "TimeSubmitted": "2026-10-19T08:00:00+00:00"}
        new = {"Name": "TVS MSP 250, 18", "JobStatus": "", "TimeSubmitted": "2026-10-19T09:50:00+00:00"}
        self.assertEqual(self.run_printer(jobs=[old])["findings"][0]["code"], "PRINT_QUEUE_STUCK")
        self.assertEqual(self.run_printer(jobs=[new])["findings"], [])

    def test_one_failed_test_print_is_not_blamed_on_the_hardware(self):
        d = self.run_printer(functional={"ran": True, "ok": False, "detail": "test page problem: paper out"})
        f = d["findings"][0]
        self.assertEqual((f["code"], f["confidence"]), ("TESTPRINT_FAILED", "possible"))
        self.assertNotEqual(d["health_state"], "SUSPECTED_HARDWARE")

    def test_repeated_test_print_failures_with_everything_else_normal_suggest_hardware(self):
        for days_ago in (3, 2):
            self.hist.record_test_print(False, NOW - timedelta(days=days_ago))
        d = self.run_printer(functional={"ran": True, "ok": False, "detail": "rejected"})      # 3rd failure today
        f = d["findings"][0]
        self.assertEqual((f["code"], d["health_state"]), ("TESTPRINT_FAILING", "SUSPECTED_HARDWARE"))
        self.assertIn("self-test", " ".join(f["actions"]))
        self.assertIn("replacement is justified", " ".join(f["actions"]))

    def test_a_success_resets_the_failure_run(self):
        self.hist.record_test_print(False, NOW - timedelta(days=3))
        self.hist.record_test_print(True, NOW - timedelta(days=2))
        self.hist.record_test_print(False, NOW - timedelta(days=1))
        self.assertEqual(self.hist.test_print_failures(NOW), (1, 1))


class Prioritisation(Base):
    def test_two_confirmed_blocking_failures_make_both_critical(self):
        h = health(biometric=status("biometric", row(cm=28), ok=False),
                   microatm=status("microatm", row("Ingenico", r"USB\VID_0B00&PID_1\1", cm=39), ok=False))
        d = self.run_dx(h)
        self.assertEqual((d["biometric"]["severity"], d["microatm"]["severity"]), ("critical", "critical"))
        self.assertIn("several services are down", d["biometric"]["summary"])

    def test_one_confirmed_failure_alone_is_high_not_critical(self):
        self.assertEqual(self.run_dx(health(biometric=status("biometric", row(cm=28), ok=False)))["biometric"]["severity"], "high")

    def test_operations_can_confirm_the_impact_which_makes_it_critical_alone(self):
        impact = impact_map.load()
        for w in impact["biometric"]["workflows"]:
            w["confirmed"] = True
        d = self.run_dx(health(biometric=status("biometric", row(cm=28), ok=False)), impact=impact)["biometric"]
        self.assertEqual(d["severity"], "critical")
        self.assertIn("confirmed by operations", d["impact"]["assessment"])

    def test_severity_depends_on_confidence_not_just_the_error(self):
        likely = self.run_dx(health(biometric=status("biometric", row(cm=10), ok=False)))["biometric"]
        self.assertEqual(likely["severity"], "medium")

    def test_impact_names_workflows_and_flags_them_as_assumed(self):
        imp = self.run_dx(health(biometric=status("biometric", row(cm=28), ok=False)))["biometric"]["impact"]
        self.assertTrue(any("Aadhaar" in w["name"] for w in imp["workflows"]))
        self.assertTrue(all(not w["confirmed"] for w in imp["workflows"]))
        self.assertIn("not yet confirmed", imp["assessment"])

    def test_findings_are_sorted_most_severe_first(self):
        h = health(biometric=status("biometric", row(cm=28), ok=False, rd=[]))
        d = self.run_dx(h, ev(services=[svc(state="Stopped")]))["biometric"]
        sev = [f["severity"] for f in d["findings"]]
        self.assertEqual(sev, sorted(sev, key=lambda s: dg.SEVERITY_ORDER[s]))


class RecoveryAndRobustness(Base):
    def test_recovery_is_verified_after_the_cause_is_fixed(self):
        broken = health(biometric=status("biometric", row(cm=28), ok=False))
        d1 = self.run_dx(broken, now=NOW)["biometric"]
        self.assertEqual(d1["verification"]["status"], "pending")
        fixed = health(biometric=status("biometric", rd=[{"status": "READY", "info": "x", "port": 1}]))
        d2 = self.run_dx(fixed, ev(services=[svc()]), now=NOW + timedelta(minutes=5))["biometric"]
        self.assertEqual(d2["health_state"], "HEALTHY")
        self.assertEqual(d2["verification"]["status"], "recovery_verified")
        self.assertEqual(d2["verification"]["codes"], ["CM_28"])

    def test_no_verification_message_when_nothing_was_ever_wrong(self):
        self.assertIsNone(self.run_dx(health(biometric=status("biometric")))["biometric"]["verification"])

    def test_unverified_identity_with_no_findings_is_not_called_healthy(self):
        d = self.run_dx(health(biometric=status("biometric", verified=False, recognised=False)))["biometric"]
        self.assertEqual(d["health_state"], "UNVERIFIED")

    def test_missing_evidence_is_reported_not_hidden_or_guessed(self):
        d = self.run_dx(health(biometric=status("biometric")),
                        {"services": None, "drivers": None, "events": None, "printers": None, "jobs": None})["biometric"]
        self.assertEqual(d["health_state"], "HEALTHY")
        text = " ".join(d["not_checked"])
        self.assertIn("service query did not answer", text)
        self.assertNotIn("Vendor / system services", " ".join(d["checked"]))

    def test_a_rule_bug_degrades_to_diagnosis_required_without_breaking(self):
        with mock.patch.object(dg, "_device_rules", side_effect=RuntimeError("bug")):
            d = self.run_dx(health(biometric=status("biometric")))["biometric"]
        self.assertEqual(d["health_state"], "DIAGNOSIS_REQUIRED")

    def test_evidence_collection_failure_does_not_break_analysis(self):
        with mock.patch.object(dg, "collect", side_effect=RuntimeError("no powershell")):
            d = dg.analyze(health(biometric=status("biometric")), {}, None, history=self.hist, now=NOW)
        self.assertIn("biometric", d)


class RemediationTests(Base):
    def diag_with_stopped_service(self):
        return self.run_dx(health(biometric=status("biometric", rd=[])), ev(services=[svc(state="Stopped")]))

    def test_starts_only_a_service_the_diagnosis_proposes_and_records_it(self):
        diag = self.diag_with_stopped_service()
        runner = mock.Mock(return_value=(True, ""))
        r = dg.start_service("MantraRDService", diag, self.hist, NOW, runner)
        self.assertTrue(r["ok"])
        runner.assert_called_once_with("MantraRDService")
        self.assertEqual(self.hist.actions("biometric")[-1]["result"], "ok")

    def test_refuses_anything_the_diagnosis_did_not_propose(self):
        diag = self.diag_with_stopped_service()
        runner = mock.Mock()
        for name in ("SomethingElse", "Spooler", "x; Remove-Item C:\\", ""):
            r = dg.start_service(name, diag, self.hist, NOW, runner)
            self.assertFalse(r["ok"], name)
        runner.assert_not_called()

    def test_a_failed_start_is_not_retried_inside_the_cooldown(self):
        diag = self.diag_with_stopped_service()
        runner = mock.Mock(return_value=(False, "Access is denied"))
        r1 = dg.start_service("MantraRDService", diag, self.hist, NOW, runner)
        r2 = dg.start_service("MantraRDService", diag, self.hist, NOW + timedelta(minutes=10), runner)
        self.assertFalse(r1["ok"])
        self.assertTrue(r1["needs_admin"])
        self.assertIn("Administrator", r1["message"])
        self.assertFalse(r2["ok"])
        self.assertIn("not retried", r2["message"])
        self.assertEqual(runner.call_count, 1)                   # no restart loop

    def test_a_fix_clears_cached_answers_so_recovery_is_verified_against_the_new_state(self):
        import deep_checks
        diag = self.diag_with_stopped_service()
        dg._cache["services"] = (1e18, ["stale"])
        deep_checks._cache["rd"] = (1e18, [])
        dg.start_service("MantraRDService", diag, self.hist, NOW, mock.Mock(return_value=(True, "")))
        self.assertNotIn("services", dg._cache)
        self.assertNotIn("rd", deep_checks._cache)

    def test_after_the_cooldown_the_diagnosis_offers_it_only_with_the_new_evidence(self):
        self.hist.record_action("biometric", "start_service", "MantraRDService", "Access is denied", NOW - timedelta(hours=7))
        f = self.run_dx(health(biometric=status("biometric", rd=[])), ev(services=[svc(state="Stopped")]))["biometric"]["findings"][0]
        self.assertIsNotNone(f["auto_fix"])                      # cooldown over


class HistoryTests(unittest.TestCase):
    def test_persists_and_tolerates_a_corrupt_file(self):
        path = os.path.join(tempfile.mkdtemp(), "h.json")
        h = dg.History(path)
        h.observe("gps", True, NOW)
        self.assertEqual(dg.History(path).last_present("gps"), NOW)
        open(path, "w").write("{not json")
        self.assertIsNone(dg.History(path).last_present("gps"))          # no crash
        dg.History(path).observe("gps", True, NOW)                       # and it recovers

    def test_only_changes_are_logged(self):
        h = dg.History(os.path.join(tempfile.mkdtemp(), "h.json"))
        for i in range(10):
            h.observe("gps", True, NOW + timedelta(minutes=i))
        self.assertEqual(h._dev(h._load(), "gps")["transitions"], [])


if __name__ == "__main__":
    unittest.main()
