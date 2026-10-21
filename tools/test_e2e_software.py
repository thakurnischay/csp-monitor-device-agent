"""END-TO-END: the agent's real diagnosis code produces the micro-ATM software report; the real server judges it against the
administrator's required versions and shows it. (Windows queries are replaced by realistic answers; everything else is real.)"""
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "agent"))
sys.path.insert(0, os.path.join(ROOT, "server"))

import diagnostics as dg                                   # noqa: E402  (agent)
import device_health as dh                                 # noqa: E402
from tests.test_routes import RouteTestCase                # noqa: E402  (server)
from tests.helpers import issue_test_key                   # noqa: E402

NOW = datetime(2026, 10, 21, 10, 0, 0, tzinfo=timezone.utc)
ATM = r"USB\VID_0B00&PID_0071\5&AAAA&0&1"


def agent_report(software_evidence, csp="E2E-SW"):
    """What the agent would send, using its real analysis."""
    r = dh._enrich_device("Ingenico Move 2500", {"Status": "OK", "Class": "USB", "InstanceId": ATM, "Manufacturer": "Ingenico",
                                                 "ConfigManagerErrorCode": 0, "ProblemDescription": ""})
    atm = {"configured": True, "present": True, "ok": True, "status": "OK", "resolved_name": r["name"], "_row": r,
           "recognised": True, "verified": True}
    off = {"configured": False, "present": False, "ok": False, "status": "not configured"}
    health = {"printer": off, "microatm": atm, "biometric": off, "gps": off, "software": {"configured": False, "running": False}}
    evidence = {"services": [], "drivers": [], "events": [], "printers": [], "jobs": [], "software": software_evidence}
    hist = dg.History(os.path.join(tempfile.mkdtemp(), "h.json"))
    diag = dg.analyze(health, {}, None, history=hist, evidence=evidence, now=NOW)
    body = {"csp_id": csp, "agent_version": "2.1.0", "schema_version": 4,
            "microatm": {k: v for k, v in atm.items() if not k.startswith("_")}, "diagnosis": diag}
    return json.loads(json.dumps(body, default=str))           # exactly what travels over the wire


def llt(version="5.2.4", installed=True, running=True, crash_hours=()):
    return {"key": "llt", "name": "Ingenico LLT", "process": "jLLT", "installed": installed, "version": version if installed else "",
            "source": "installed programs", "running": running, "started": [],
            "crashes": [{"id": 1000, "t": NOW - timedelta(hours=h)} for h in crash_hours]}


def drivers(version="3.12", installed=True):
    return {"key": "usb_drivers", "name": "Ingenico USB Drivers", "process": "", "installed": installed,
            "version": version if installed else "", "source": "installed programs", "running": False, "started": [], "crashes": []}


class E2E(RouteTestCase):
    def setUp(self):
        super().setUp()
        self.key = issue_test_key("E2E-SW", "E2E Software CSP")

    def send(self, programs):
        r = self.client.post("/api/report", json=agent_report(programs), headers={"X-API-Key": self.key})
        self.assertEqual(r.status_code, 200)

    def page(self):
        self._login()
        return self.client.get("/csp/E2E-SW").get_data(as_text=True)

    def headline(self):
        """The micro-ATM's CURRENT one-line summary (the second panel; earlier, resolved issues live in the history section)."""
        import re
        return re.findall(r'<p class="diag-summary">(.*?)</p>', self.page(), re.S)[1]

    def test_up_to_date_and_healthy(self):
        self.send([llt(), drivers()])
        page = self.page()
        self.assertIn("Micro-ATM software", page)
        self.assertEqual(page.count("Up to date"), 2)
        self.assertEqual(self.headline(), "No problem found by the checks listed below")

    def test_not_installed_out_of_date_and_crashing_are_all_reported(self):
        self.send([llt(installed=False), drivers(version="3.10")])
        page = self.page()
        self.assertIn("The micro-ATM software (Ingenico LLT) is not installed", page)
        self.assertIn("The micro-ATM software (Ingenico USB Drivers) is out of date (installed 3.10, required 3.12)", page)
        self.send([llt(version="5.2.4", crash_hours=(1, 5, 30)), drivers()])
        # both earlier problems are fixed, so only the crash problem is current (the other two move to the history)
        self.assertEqual(self.headline(), "The micro-ATM software (Ingenico LLT) keeps crashing")
        self.send([llt(version="5.2.4"), drivers()])
        self.assertEqual(self.headline(), "No problem found by the checks listed below")

    def test_a_new_llt_release_is_handled_by_changing_one_number_on_the_software_page(self):
        self.send([llt(version="5.2.4"), drivers()])
        self.assertNotIn("is out of date", self.headline())
        import db, software
        with db.get_connection() as conn:
            software.set_requirement(conn, "llt", "5.3.0", "LLT 5.3.0 released", True, "admin")
            conn.commit()
        self.send([llt(version="5.2.4"), drivers()])                                   # the SAME agent report as before
        self.assertEqual(self.headline(), "The micro-ATM software (Ingenico LLT) is out of date (installed 5.2.4, required 5.3.0)")
        self.send([llt(version="5.3.0"), drivers()])                                   # the CSP installs the new LLT
        self.assertEqual(self.headline(), "No problem found by the checks listed below")


CH340 = r"USB\VID_1A86&PID_7523\5&BBBB&0&2"
MORPHO = r"USB\VID_079B&PID_0047\5&CCCC&0&3"


def device_report(csp, gps=None, biometric=None, evidence_software=()):
    """Agent report for a PC with a GPS dongle and/or a fingerprint scanner (real analysis)."""
    off = {"configured": False, "present": False, "ok": False, "status": "not configured"}
    health = {"printer": off, "microatm": off, "biometric": off, "gps": off, "software": {"configured": False, "running": False}}
    body = {"csp_id": csp, "agent_version": "2.2.0", "schema_version": 4}
    if gps:
        r = dh._enrich_device("USB-SERIAL CH340 (COM3)", {"Status": "OK", "Class": "Ports", "InstanceId": CH340, "Manufacturer": "wch.cn",
                                                          "ConfigManagerErrorCode": 0, "ProblemDescription": ""})
        health["gps"] = {"configured": True, "present": True, "ok": True, "status": "OK", "resolved_name": r["name"], "_row": r,
                         "recognised": True, "verified": True}
    if biometric is not None:
        r = dh._enrich_device("MorphoSmart MSO 1300", {"Status": "OK", "Class": "Biometric", "InstanceId": MORPHO, "Manufacturer": "Morpho",
                                                       "ConfigManagerErrorCode": 0, "ProblemDescription": ""})
        health["biometric"] = {"configured": True, "present": True, "ok": True, "status": "OK", "resolved_name": r["name"], "_row": r,
                               "recognised": True, "verified": True, "rd": biometric}
    evidence = {"services": [], "drivers": [], "events": [], "printers": [], "jobs": [], "software": list(evidence_software)}
    hist = dg.History(os.path.join(tempfile.mkdtemp(), "h.json"))
    body["diagnosis"] = dg.analyze(health, {}, None, history=hist, evidence=evidence, now=NOW)
    for dev in ("gps", "biometric"):
        if health[dev]["configured"]:
            body[dev] = {k: v for k, v in health[dev].items() if not k.startswith("_") and k != "rd"}
    return json.loads(json.dumps(body, default=str))


def gps_driver(version="3.8.2023.2", installed=True):
    return {"key": "gps_driver", "name": "GPS dongle driver (WCH CH341)", "process": "", "installed": installed,
            "version": version if installed else "", "source": "driver file" if installed else "", "running": False, "started": [], "crashes": []}


def morpho_rd(version="5.0.0.10", installed=True):
    return {"key": "morpho_rd", "name": "Morpho / IDEMIA RD Service", "process": "", "installed": installed,
            "version": version if installed else "", "source": "service program" if installed else "", "running": installed,
            "started": [], "crashes": []}


class E2EOtherDevices(RouteTestCase):
    def setUp(self):
        super().setUp()
        self.key = issue_test_key("E2E-DEV", "E2E Devices CSP")

    def send(self, **kw):
        r = self.client.post("/api/report", json=device_report("E2E-DEV", **kw), headers={"X-API-Key": self.key})
        self.assertEqual(r.status_code, 200)

    def headlines(self):
        import re
        self._login()
        page = self.client.get("/csp/E2E-DEV").get_data(as_text=True)
        return re.findall(r'<p class="diag-summary">(.*?)</p>', page, re.S)           # printer, micro-ATM, biometric, GPS

    def test_gps_driver_missing_old_and_current(self):
        self.send(gps=True, evidence_software=[gps_driver(installed=False)])
        self.assertIn("The GPS dongle driver (GPS dongle driver (WCH CH341)) is not installed", self.headlines()[3])
        self.send(gps=True, evidence_software=[gps_driver("3.6.2020.1")])
        self.assertIn("is out of date (installed 3.6.2020.1, required 3.8.2023.2)", self.headlines()[3])
        self.send(gps=True, evidence_software=[gps_driver("3.8.2023.2")])
        self.assertNotIn("driver", self.headlines()[3])

    def test_the_fingerprint_scanner_service_missing_old_and_current(self):
        self.send(biometric=[], evidence_software=[morpho_rd(installed=False)])
        self.assertIn("The fingerprint scanner software (Morpho / IDEMIA RD Service) is not installed", self.headlines()[2])
        self.send(biometric=[], evidence_software=[morpho_rd("5.0.0.8")])
        self.assertIn("is out of date (installed 5.0.0.8, required 5.0.0.10)", self.headlines()[2])
        self.send(biometric=[], evidence_software=[morpho_rd("5.0.0.10")])
        self.assertNotIn("out of date", self.headlines()[2])

    def test_a_scanner_whose_service_answers_is_never_reported_as_not_installed(self):
        answered = [{"status": "READY", "info": "Morpho_RD_Service", "port": 11100}]
        self.send(biometric=answered, evidence_software=[morpho_rd(installed=False)])    # the installed-programs lookup missed it
        self.assertNotIn("not installed", self.headlines()[2])

    def test_other_makes_are_not_judged(self):
        # a Mantra scanner / u-blox GPS would not carry the Morpho / CH340 software expectation (covered by the agent tests);
        # here: a PC with neither device reports nothing about them at all
        r = self.client.post("/api/report", json=device_report("E2E-DEV", evidence_software=[gps_driver(installed=False)]),
                             headers={"X-API-Key": self.key})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len([h for h in self.headlines() if "not installed" in h]), 0)


if __name__ == "__main__":
    unittest.main()
