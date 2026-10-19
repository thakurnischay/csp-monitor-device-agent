"""Root-cause diagnosis on the server: issue lifecycle (open / refresh / resolve /
recur), validation of hostile input, and what the dashboard shows."""
import html as htmllib
import json
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.helpers import issue_test_key
from tests.test_routes import RouteTestCase


def finding(code="CM_28", state="DRIVER_ISSUE", severity="high", title="Windows reports error code 28",
            confidence="confirmed", **extra):
    f = {"code": code, "device": "biometric", "state": state, "severity": severity, "title": title,
         "symptom": "Windows lists the scanner with a problem",
         "root_cause": "No driver is installed for this device.", "confidence": confidence,
         "category": "driver", "evidence": ["Windows Device Manager reports error code 28", "Driver: none"],
         "actions": ["Open Device Manager", "Install the vendor-approved driver", "Restart the PC"],
         "operator_action": True, "auto_fix": None, "blocks": True}
    f.update(extra)
    return f


def diag(state="HEALTHY", severity="none", findings=(), summary="No problem found by the checks below", **extra):
    d = {"health_state": state, "severity": severity, "summary": summary, "findings": list(findings),
         "notes": [], "checked": ["Whether Windows lists the device", "Windows device status and error code"],
         "not_checked": ["A live fingerprint capture (never done)"],
         "impact": {"workflows": [{"name": "Aadhaar-based transactions (AePS)", "needs": "required", "confirmed": False}],
                    "assessment": "Likely affected while this device is down (assumed, not yet confirmed by operations).",
                    "workaround": "None known for biometric authentication.",
                    "revenue": "Not calculable: no transaction or revenue data is connected to the agent.",
                    "confirmed": False, "required": True},
         "last_healthy_at": "2026-10-18T09:00:00+00:00", "verification": None}
    d.update(extra)
    return d


def legacy(configured=True, present=True, ok=True, status="OK"):
    return {"configured": configured, "present": present, "ok": ok, "status": status, "verified": True}


class DiagBase(RouteTestCase):
    def setUp(self):
        super().setUp()
        self.key = issue_test_key("CSP001", "Test CSP One")

    def report(self, biometric_diag=None, **diagnosis_extra):
        body = {"csp_id": "CSP001", "schema_version": 4, "agent_version": "2.0.0",
                "printer": legacy(), "microatm": legacy(), "biometric": legacy(ok=False, status="Error"),
                "gps": legacy()}
        dg = {"biometric": biometric_diag if biometric_diag is not None else diag()}
        dg.update(diagnosis_extra)
        body["diagnosis"] = dg
        r = self.client.post("/api/report", headers={"X-API-Key": self.key}, json=body)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        return r

    def q(self, sql, *args):
        import db
        with db.get_connection() as conn:
            return [dict(r) for r in conn.execute(sql, args).fetchall()]


class IssueLifecycleTests(DiagBase):
    def test_a_finding_opens_an_issue_with_parsed_evidence_and_an_event(self):
        self.report(diag("DRIVER_ISSUE", "high", [finding()], summary="Windows reports error code 28"))
        issue = self.q("SELECT * FROM device_issues")[0]
        self.assertEqual((issue["device"], issue["status"], issue["severity"], issue["occurrences"]),
                         ("biometric", "OPEN", "high", 1))
        self.assertEqual(json.loads(issue["evidence_json"])[0], "Windows Device Manager reports error code 28")
        self.assertIn("biometric_issue_opened", [e["event_type"] for e in self.q("SELECT event_type FROM events")])
        row = self.q("SELECT * FROM device_diagnosis WHERE device='biometric'")[0]
        self.assertEqual((row["health_state"], row["severity"]), ("DRIVER_ISSUE", "high"))

    def test_the_same_finding_again_updates_it_without_a_duplicate_or_a_new_event(self):
        for _ in range(3):
            self.report(diag("DRIVER_ISSUE", "high", [finding()]))
        self.assertEqual(len(self.q("SELECT id FROM device_issues")), 1)
        self.assertEqual(len([e for e in self.q("SELECT event_type FROM events") if e["event_type"] == "biometric_issue_opened"]), 1)

    def test_the_issue_closes_with_a_recovery_event_once_the_cause_is_gone(self):
        self.report(diag("DRIVER_ISSUE", "high", [finding()]))
        self.report(diag("HEALTHY"))
        issue = self.q("SELECT * FROM device_issues")[0]
        self.assertEqual(issue["status"], "RESOLVED")
        self.assertIsNotNone(issue["resolved_at"])
        self.assertIn("biometric_recovery_verified", [e["event_type"] for e in self.q("SELECT event_type FROM events")])

    def test_a_recurrence_is_a_new_issue_with_a_higher_occurrence_count(self):
        self.report(diag("DRIVER_ISSUE", "high", [finding()]))
        self.report(diag("HEALTHY"))
        self.report(diag("DRIVER_ISSUE", "high", [finding()]))
        rows = self.q("SELECT status, occurrences FROM device_issues ORDER BY id")
        self.assertEqual([(r["status"], r["occurrences"]) for r in rows], [("RESOLVED", 1), ("OPEN", 2)])

    def test_two_different_findings_are_tracked_independently(self):
        self.report(diag("DRIVER_ISSUE", "high", [finding("CM_28"), finding("REPEATED_DISCONNECTS", "SUSPECTED_HARDWARE", "medium", "Keeps disconnecting")]))
        self.report(diag("SUSPECTED_HARDWARE", "medium", [finding("REPEATED_DISCONNECTS", "SUSPECTED_HARDWARE", "medium", "Keeps disconnecting")]))
        status = {r["code"]: r["status"] for r in self.q("SELECT code, status FROM device_issues")}
        self.assertEqual(status, {"CM_28": "RESOLVED", "REPEATED_DISCONNECTS": "OPEN"})

    def test_diagnosed_devices_do_not_also_create_the_older_shallow_incident(self):
        self.report(diag("DRIVER_ISSUE", "high", [finding()]))          # legacy fields say not-ok too
        self.assertEqual(self.q("SELECT id FROM incidents WHERE device='biometric'"), [])

    def test_devices_without_a_diagnosis_keep_the_older_incident_logic(self):
        body = {"csp_id": "CSP001", "schema_version": 3, "printer": legacy(ok=False, status="offline"),
                "microatm": legacy(), "biometric": legacy(), "gps": legacy()}
        self.client.post("/api/report", headers={"X-API-Key": self.key}, json=body)
        self.assertEqual([i["device"] for i in self.q("SELECT device FROM incidents")], ["printer"])


class ValidationTests(DiagBase):
    def test_unknown_values_are_replaced_with_safe_ones(self):
        bad = finding(state="LOL", severity="apocalyptic", confidence="certain")
        self.report(diag("NOT_A_STATE", "weird", [bad]))
        row = self.q("SELECT * FROM device_diagnosis")[0]
        self.assertEqual((row["health_state"], row["severity"]), ("DIAGNOSIS_REQUIRED", "none"))
        issue = self.q("SELECT * FROM device_issues")[0]
        self.assertEqual((issue["state"], issue["severity"], issue["confidence"]), ("DIAGNOSIS_REQUIRED", "low", "possible"))

    def test_oversized_text_and_lists_are_clipped(self):
        self.report(diag("DRIVER_ISSUE", "high", [finding(title="T" * 5000, evidence=["e" * 5000] * 20, actions=["a" * 5000] * 20)]))
        issue = self.q("SELECT * FROM device_issues")[0]
        self.assertLessEqual(len(issue["title"]), 160)
        self.assertLessEqual(len(json.loads(issue["evidence_json"])), 10)
        self.assertTrue(all(len(e) <= 240 for e in json.loads(issue["evidence_json"])))
        self.assertLessEqual(len(json.loads(issue["actions_json"])), 9)

    def test_malformed_diagnoses_never_break_the_heartbeat(self):
        for bad in ("oops", ["x"], 5, {"biometric": "x"}, {"biometric": {"findings": "x"}},
                    {"biometric": {"findings": [1, None, {}, {"code": ""}, {"code": "OK1"}]}}, {"nope": {}}):
            body = {"csp_id": "CSP001", "schema_version": 4, "printer": legacy(), "microatm": legacy(),
                    "biometric": legacy(), "gps": legacy(), "diagnosis": bad}
            from security import report_limiter
            report_limiter.reset()                   # seven reports in a row would hit the (correct) rate limit
            r = self.client.post("/api/report", headers={"X-API-Key": self.key}, json=body)
            self.assertEqual(r.status_code, 200, bad)

    def test_markup_in_a_report_is_stored_as_text_and_escaped_on_the_page(self):
        evil = "<script>alert('x')</script>"
        self.report(diag("DRIVER_ISSUE", "high", [finding(title=evil, evidence=[evil], actions=[evil], symptom=evil)], summary=evil))
        self._login()
        page = self.client.get("/csp/CSP001").get_data(as_text=True)
        self.assertNotIn("<script>alert", page)
        self.assertIn("&lt;script&gt;", page)
        fleet = self.client.get("/").get_data(as_text=True)
        self.assertNotIn("<script>alert", fleet)

    def test_a_diagnosis_for_an_unknown_device_is_ignored(self):
        self.report(diag(), toaster=diag("DRIVER_ISSUE", "high", [finding()]))
        self.assertEqual({r["device"] for r in self.q("SELECT device FROM device_diagnosis")}, {"biometric"})

    def test_a_large_realistic_payload_is_accepted(self):
        big = diag("DRIVER_ISSUE", "high", [finding(f"C{i}", evidence=["x" * 200] * 10, actions=["y" * 280] * 9) for i in range(6)])
        self.report(big, printer=big, microatm=big, gps=big)


class DashboardTests(DiagBase):
    def setUp(self):
        super().setUp()
        self._login()

    def row_text(self, html, name):
        row = html.split(name)[1].split("</tr>")[0]
        return re.sub(r"\s+", " ", htmllib.unescape(re.sub(r"<[^>]+>", " ", row))).strip()

    def test_the_fleet_cell_shows_the_diagnosis_severity_and_headline_cause(self):
        self.report(diag("DRIVER_ISSUE", "high", [finding()], summary="Windows reports error code 28"))
        text = self.row_text(self.client.get("/").get_data(as_text=True), "Test CSP One")
        self.assertIn("Driver issue", text)
        self.assertIn("high", text)
        self.assertIn("Windows reports error code 28", text)

    def test_every_state_has_a_readable_label(self):
        import diagnosis
        for state in diagnosis.HEALTH_STATES:
            self.assertIn(state, diagnosis.STATE_LABELS)
        labels = {v[0] for v in diagnosis.STATE_LABELS.values()}
        for needed in ("Healthy", "Degraded", "Software issue", "Driver issue", "Suspected hardware failure",
                       "Service unavailable", "Diagnosis required"):
            self.assertIn(needed, labels)

    def test_a_healthy_device_after_recovery_says_recovery_verified(self):
        self.report(diag("DRIVER_ISSUE", "high", [finding()]))
        self.report(diag("HEALTHY", verification={"status": "recovery_verified", "at": "2026-10-19T10:00:00+00:00",
                                                  "note": "The earlier problem is gone.", "codes": ["CM_28"]}))
        html = self.client.get("/").get_data(as_text=True)
        self.assertIn("Recovery verified", html)
        self.assertIn("Recovery verified", self.client.get("/csp/CSP001").get_data(as_text=True))

    def test_kpis_count_urgent_issues_and_problems_use_severity(self):
        self.report(diag("DRIVER_ISSUE", "high", [finding()]), gps=diag("NOT_CONNECTED", "low", [finding("NOT_CONNECTED", "NOT_CONNECTED", "low")]))
        html = self.client.get("/").get_data(as_text=True)
        kpi = lambda label: re.search(r'kpi-value">(\d+)</div>\s*<div class="kpi-label">' + label, html).group(1)
        self.assertEqual(kpi("Critical / high issues"), "1")
        self.assertEqual(kpi("Biometric problems"), "1")
        self.assertEqual(kpi("GPS problems"), "0")                  # a low lead is not a problem

    def test_problems_only_follows_severity(self):
        self.report(diag("HEALTHY"))
        self.assertNotIn("Test CSP One", self.client.get("/?problems_only=1").get_data(as_text=True))
        self.report(diag("SOFTWARE_ISSUE", "medium", [finding("X", "SOFTWARE_ISSUE", "medium")]))
        self.assertIn("Test CSP One", self.client.get("/?problems_only=1").get_data(as_text=True))

    def test_an_offline_csp_is_disconnected_not_diagnosed(self):
        import db
        self.report(diag("DRIVER_ISSUE", "high", [finding()]))
        with db.get_connection() as conn:
            conn.execute("UPDATE csps SET last_seen='2026-01-01T00:00:00+00:00' WHERE csp_id='CSP001'")
            conn.commit()
        text = self.row_text(self.client.get("/").get_data(as_text=True), "Test CSP One")
        self.assertIn("Disconnected", text)
        self.assertNotIn("Driver issue", text)

    def test_the_csv_carries_health_and_severity(self):
        self.report(diag("DRIVER_ISSUE", "high", [finding()]))
        lines = self.client.get("/fleet.csv").get_data(as_text=True).splitlines()
        self.assertIn("biometric_health", lines[0])
        self.assertIn("DRIVER_ISSUE/high", lines[1])

    def test_the_detail_page_shows_everything_an_operator_needs(self):
        self.report(diag("DRIVER_ISSUE", "high", [finding()], summary="Windows reports error code 28",
                         notes=["The RD service answered READY."]))
        page = self.client.get("/csp/CSP001").get_data(as_text=True)
        for needle in ("Windows reports error code 28",                       # title / summary
                       "Windows lists the scanner with a problem",           # symptom
                       "No driver is installed for this device.",           # root cause
                       "Confirmed: Windows or the device itself reports this",  # confidence explained
                       "Driver: none",                                        # evidence
                       "Aadhaar-based transactions (AePS)",                   # affected workflow
                       "assumed - not yet confirmed",                         # honesty about the mapping
                       "None known for biometric authentication",             # workaround
                       "Not calculable",                                      # no invented revenue
                       "Install the vendor-approved driver",                  # steps
                       "Operator action required",                            # who acts
                       "occurrence 1",                                        # history
                       "What was checked and what could not be",              # limits
                       "A live fingerprint capture (never done)",
                       "The RD service answered READY."):                     # notes
            self.assertIn(needle, page, needle)

    def test_a_service_fix_is_offered_only_with_operator_approval_wording(self):
        f = finding("SERVICE_STOPPED:X", "SERVICE_UNAVAILABLE", "high", "'X' is stopped",
                    auto_fix={"type": "start_service", "service": "X", "label": "X"})
        self.report(diag("SERVICE_UNAVAILABLE", "high", [f]))
        page = self.client.get("/csp/CSP001").get_data(as_text=True)
        self.assertIn("Operator approval needed", page)
        self.assertIn("nothing is changed automatically", page)

    def test_history_lists_earlier_issues_and_recurrence(self):
        self.report(diag("DRIVER_ISSUE", "high", [finding()]))
        self.report(diag("HEALTHY"))
        self.report(diag("DRIVER_ISSUE", "high", [finding()]))
        page = self.client.get("/csp/CSP001").get_data(as_text=True)
        self.assertIn("Diagnostic history (1 earlier issue)", page)
        self.assertIn("occurrence 2 of this issue", page)
        self.assertIn("it has happened before", page)

    def test_older_agents_still_get_the_older_panels(self):
        body = {"csp_id": "CSP001", "schema_version": 3, "printer": legacy(), "microatm": legacy(),
                "biometric": legacy(), "gps": legacy()}
        self.client.post("/api/report", headers={"X-API-Key": self.key}, json=body)
        page = self.client.get("/csp/CSP001").get_data(as_text=True)
        self.assertIn("Passbook printer", page)
        self.assertNotIn("Likely root cause", page)

    def test_legend_explains_the_states_and_severity(self):
        html = self.client.get("/").get_data(as_text=True)
        for needle in ("root-cause diagnosis", "Suspected hardware failure", "Diagnosis required", "business severity"):
            self.assertIn(needle, html)

    def test_deleting_a_csp_removes_its_diagnosis_and_issue_history(self):
        self.report(diag("DRIVER_ISSUE", "high", [finding()]))
        token = re.search(r'name="csrf_token" value="([^"]+)"', self.client.get("/api-keys").get_data(as_text=True)).group(1)
        self.client.post("/api-keys", data={"csrf_token": token, "action": "delete", "csp_id": "CSP001"})
        self.assertEqual(self.q("SELECT * FROM device_diagnosis"), [])
        self.assertEqual(self.q("SELECT * FROM device_issues"), [])


if __name__ == "__main__":
    unittest.main()
