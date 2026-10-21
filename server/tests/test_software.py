"""Micro-ATM software: required versions set by the administrator, and the verdict for each CSP
(not installed / out of date / up to date / unknown), with issues that open and close like every other diagnosis."""
import sqlite3
import unittest

import db
import software
from tests.helpers import issue_test_key
from tests.test_routes import RouteTestCase, _extract_csrf


def inv(llt_version="5.2.4", llt_installed=True, drv_version="3.12", drv_installed=True, expected=True, running=True,
        crashes_24h=0, crashes_7d=0):
    return [{"key": "llt", "name": "Ingenico LLT", "installed": llt_installed, "version": llt_version if llt_installed else "",
             "source": "installed programs", "running": running, "expected": expected, "crashes_7d": crashes_7d,
             "crashes_24h": crashes_24h, "last_crash_at": None},
            {"key": "usb_drivers", "name": "Ingenico USB Drivers", "installed": drv_installed,
             "version": drv_version if drv_installed else "", "source": "installed programs", "running": None,
             "expected": expected, "crashes_7d": 0, "crashes_24h": 0, "last_crash_at": None}]


def report(software_items, csp="SW-1", findings=(), state="HEALTHY", severity="none", summary="No problem found"):
    return {"csp_id": csp, "agent_version": "2.1.0", "schema_version": 4,
            "microatm": {"configured": True, "present": True, "ok": True, "status": "OK"},
            "diagnosis": {"microatm": {"health_state": state, "severity": severity, "summary": summary,
                                       "findings": list(findings), "notes": [], "checked": [], "not_checked": [],
                                       "software": software_items}}}


class Versions(unittest.TestCase):
    def test_versions_compare_as_numbers_not_text(self):
        self.assertEqual(software.compare("5.10", "5.9"), 1)
        self.assertEqual(software.compare("5.2.4", "5.2.4"), 0)
        self.assertEqual(software.compare("5.2.3", "5.2.4"), -1)
        self.assertEqual(software.compare("3.12.0.0", "3.12"), 0)         # missing parts count as zero
        self.assertEqual(software.compare("v5.3", "5.2.9"), 1)
        self.assertEqual(software.compare("LLT 5.2.4", "5.2.4"), 0)

    def test_unreadable_versions_are_unknown_not_outdated(self):
        for bad in ("", None, "unknown", "n/a"):
            self.assertIsNone(software.compare(bad, "5.2.4"))

    def test_only_plain_dotted_numbers_are_accepted_as_a_requirement(self):
        for ok in ("5", "5.2", "5.2.4", "3.12.0.1"):
            self.assertTrue(software.valid_version(ok))
        for bad in ("", "abc", "5.2.x", "5..2", "<script>", "5.2.4; DROP", "-1"):
            self.assertFalse(software.valid_version(bad))


class Verdicts(unittest.TestCase):
    REQ = {"llt": {"active": 1, "min_version": "5.2.4"}, "usb_drivers": {"active": 1, "min_version": "3.12"}}

    def v(self, **kw):
        return {p["key"]: p["verdict"] for p in software.annotate(inv(**kw), self.REQ)}

    def test_each_verdict(self):
        self.assertEqual(self.v(), {"llt": "ok", "usb_drivers": "ok"})
        self.assertEqual(self.v(llt_version="5.3.0")["llt"], "ok")                       # newer is fine
        self.assertEqual(self.v(llt_version="5.2.1")["llt"], "outdated")
        self.assertEqual(self.v(llt_installed=False)["llt"], "missing")
        self.assertEqual(self.v(llt_version="")["llt"], "unknown")
        self.assertEqual(self.v(drv_version="3.11")["usb_drivers"], "outdated")

    def test_a_pc_without_an_ingenico_micro_atm_is_never_judged(self):
        self.assertEqual(self.v(expected=False, llt_installed=False), {"llt": "not_needed", "usb_drivers": "not_needed"})

    def test_an_inactive_requirement_is_not_judged(self):
        reqs = {"llt": dict(self.REQ["llt"], active=0), "usb_drivers": self.REQ["usb_drivers"]}
        out = {p["key"]: p["verdict"] for p in software.annotate(inv(llt_version="1.0"), reqs)}
        self.assertEqual(out["llt"], "not_needed")

    def test_the_agents_report_is_whitelisted(self):
        dirty = [{"key": "llt", "installed": 1, "version": "<script>alert(1)</script>" + "9" * 100, "crashes_24h": "x",
                  "crashes_7d": 10 ** 9, "expected": "yes", "running": "maybe"},
                 {"key": "evil", "installed": True}, "junk", None]
        out = software.clean_inventory(dirty)
        self.assertEqual([p["key"] for p in out], ["llt"])
        self.assertLessEqual(len(out[0]["version"]), 40)
        self.assertEqual((out[0]["crashes_24h"], out[0]["crashes_7d"], out[0]["running"]), (0, 999, None))


class Stored(RouteTestCase):
    def setUp(self):
        super().setUp()
        self.key = issue_test_key("SW-1", "Software CSP")

    def post(self, body):
        r = self.client.post("/api/report", json=body, headers={"X-API-Key": self.key})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))

    def q(self, sql, *args):
        con = sqlite3.connect(self.db_path)
        con.row_factory = sqlite3.Row
        try:
            return con.execute(sql, args).fetchall()
        finally:
            con.close()

    def issues(self):
        return {r["code"]: (r["status"], r["severity"]) for r in self.q("SELECT code, status, severity FROM device_issues WHERE csp_id='SW-1'")}

    def headline(self):
        r = self.q("SELECT health_state, severity, summary FROM device_diagnosis WHERE csp_id='SW-1' AND device='microatm'")[0]
        return r["health_state"], r["severity"], r["summary"]

    def test_the_initial_requirements_come_from_the_supplied_package(self):
        rows = {r["key"]: r["min_version"] for r in self.q("SELECT key, min_version FROM software_requirements")}
        self.assertEqual(rows, {"llt": "5.2.4", "usb_drivers": "3.12"})

    def test_seeding_runs_once_and_never_overwrites_an_edit(self):
        with db.get_connection() as conn:
            software.set_requirement(conn, "llt", "5.3.0", "", True, "admin")
            conn.commit()
        db.setup()
        self.assertEqual(self.q("SELECT min_version FROM software_requirements WHERE key='llt'")[0][0], "5.3.0")

    def test_not_installed_opens_a_high_issue_and_closes_when_installed(self):
        self.post(report(inv(llt_installed=False)))
        self.assertEqual(self.issues(), {"SOFTWARE_MISSING_LLT": ("OPEN", "high")})
        state, sev, summary = self.headline()
        self.assertEqual((state, sev), ("SOFTWARE_ISSUE", "high"))
        self.assertIn("not installed", summary)
        self.post(report(inv()))
        self.assertEqual(self.issues(), {"SOFTWARE_MISSING_LLT": ("RESOLVED", "high")})
        self.assertEqual(self.headline()[0], "HEALTHY")

    def test_an_old_version_is_flagged_and_fixed_by_updating(self):
        self.post(report(inv(llt_version="5.1.0")))
        self.assertEqual(self.issues(), {"SOFTWARE_OUTDATED_LLT": ("OPEN", "medium")})
        self.assertIn("installed 5.1.0, required 5.2.4", self.headline()[2])
        self.post(report(inv(llt_version="5.2.4")))
        self.assertEqual(self.issues()["SOFTWARE_OUTDATED_LLT"][0], "RESOLVED")

    def test_raising_the_required_version_flags_the_csp_on_its_next_report_without_any_agent_update(self):
        self.post(report(inv(llt_version="5.2.4")))
        self.assertEqual(self.issues(), {})
        with db.get_connection() as conn:
            software.set_requirement(conn, "llt", "5.3.0", "new release", True, "admin")
            conn.commit()
        self.post(report(inv(llt_version="5.2.4")))
        self.assertEqual(self.issues(), {"SOFTWARE_OUTDATED_LLT": ("OPEN", "medium")})

    def test_other_micro_atm_makes_are_left_alone(self):
        self.post(report(inv(expected=False, llt_installed=False, drv_installed=False)))
        self.assertEqual(self.issues(), {})
        self.assertEqual(self.headline()[0], "HEALTHY")

    def test_an_older_agent_that_reports_no_software_changes_nothing(self):
        body = report(inv())
        del body["diagnosis"]["microatm"]["software"]
        self.post(body)
        self.assertEqual(self.issues(), {})

    def test_a_more_urgent_agent_finding_keeps_the_headline(self):
        crit = {"code": "CM_28", "state": "DRIVER_ISSUE", "severity": "critical", "title": "Driver missing", "symptom": "s",
                "root_cause": "r", "confidence": "confirmed", "category": "driver", "evidence": [], "actions": []}
        self.post(report(inv(llt_version="5.1.0"), findings=[crit], state="DRIVER_ISSUE", severity="critical", summary="Driver missing"))
        self.assertEqual(self.headline(), ("DRIVER_ISSUE", "critical", "Driver missing"))
        self.assertIn("SOFTWARE_OUTDATED_LLT", self.issues())

    def test_the_csp_page_shows_the_software_table_judged_against_todays_requirement(self):
        self.post(report(inv(llt_version="5.1.0")))
        self._login()
        page = self.client.get("/csp/SW-1").get_data(as_text=True)
        self.assertIn("Micro-ATM software", page)
        self.assertIn("5.1.0", page)
        self.assertIn("Out of date", page)

    def test_a_hostile_version_string_is_shown_as_text_not_markup(self):
        self.post(report(inv(llt_version="<script>alert(1)</script>")))
        self._login()
        page = self.client.get("/csp/SW-1").get_data(as_text=True)
        self.assertNotIn("<script>alert(1)</script>", page)


class SoftwarePage(RouteTestCase):
    def setUp(self):
        super().setUp()
        self.key = issue_test_key("SW-1", "Software CSP")
        for csp, items in (("SW-1", inv(llt_version="5.1.0")), ("SW-2", inv()), ("SW-3", inv(llt_installed=False, crashes_24h=2))):
            k = self.key if csp == "SW-1" else issue_test_key(csp, csp)
            r = self.client.post("/api/report", json=report(items, csp=csp), headers={"X-API-Key": k})
            self.assertEqual(r.status_code, 200)

    def test_the_page_needs_a_login(self):
        r = self.client.get("/software")
        self.assertIn(r.status_code, (301, 302, 303, 401))

    def test_the_page_lists_every_csp_worst_first_with_totals(self):
        self._login()
        page = self.client.get("/software").get_data(as_text=True)
        self.assertLess(page.index("SW-3"), page.index("SW-1"))            # not installed before out of date
        self.assertLess(page.index("SW-1"), page.index("SW-2"))
        for text in ("Not installed 1", "Out of date 1", "Up to date 1", "Crashed in the last 24 h 1"):
            self.assertIn(text, page)

    def test_changing_the_required_version_updates_the_page_immediately(self):
        self._login()
        token = _extract_csrf(self.client.get("/software").get_data(as_text=True))
        r = self.client.post("/software", data={"csrf_token": token, "key": "llt", "min_version": "5.0.0", "note": "relaxed", "active": "1"},
                             follow_redirects=True)
        self.assertEqual(r.status_code, 200)
        page = self.client.get("/software").get_data(as_text=True)
        self.assertIn("Out of date 0", page)                                # 5.1.0 now satisfies 5.0.0
        con = sqlite3.connect(self.db_path)
        try:
            audit = [r[0] for r in con.execute("SELECT detail FROM admin_audit_log WHERE action='software_requirement_set'")]
        finally:
            con.close()
        self.assertTrue([a for a in audit if "required 5.0.0" in a and "was 5.2.4" in a], audit)

    def test_a_bad_version_or_unknown_software_is_refused_and_nothing_changes(self):
        self._login()
        token = _extract_csrf(self.client.get("/software").get_data(as_text=True))
        for data in ({"key": "llt", "min_version": "latest"}, {"key": "nope", "min_version": "1.0"}):
            self.client.post("/software", data=dict(csrf_token=token, active="1", **data), follow_redirects=True)
        con = sqlite3.connect(self.db_path)
        try:
            self.assertEqual(con.execute("SELECT min_version FROM software_requirements WHERE key='llt'").fetchone()[0], "5.2.4")
            self.assertIsNone(con.execute("SELECT 1 FROM software_requirements WHERE key='nope'").fetchone())
        finally:
            con.close()

    def test_saving_without_the_csrf_token_is_refused(self):
        self._login()
        r = self.client.post("/software", data={"key": "llt", "min_version": "9.9.9", "active": "1"})
        self.assertIn(r.status_code, (400, 403))
        con = sqlite3.connect(self.db_path)
        try:
            self.assertEqual(con.execute("SELECT min_version FROM software_requirements WHERE key='llt'").fetchone()[0], "5.2.4")
        finally:
            con.close()


if __name__ == "__main__":
    unittest.main()
