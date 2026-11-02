"""Secure enrollment: a CSP code and name are never enough to get a credential."""
import os
import re
import secrets
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import registry
from security import hash_api_key
from tests.helpers import issue_test_key
from tests.test_routes import RouteTestCase, _extract_csrf, ADMIN_PASSWORD


def new_key():
    return secrets.token_urlsafe(32)


class EnrollBase(RouteTestCase):
    def setUp(self):
        super().setUp()
        self.add_registry("CSP-JH-1042", "Sharma Digital Seva Kendra")

    def add_registry(self, code, name, pre=0):
        import db
        with db.get_connection() as conn:
            conn.execute("INSERT INTO csp_registry (csp_id, name, pre_approved, created_at) VALUES (?,?,?, '2026-01-01')", (code, name, pre))
            conn.commit()

    def q(self, sql, *a):
        import db
        with db.get_connection() as conn:
            return [dict(r) for r in conn.execute(sql, a).fetchall()]

    def verify(self, code="CSP-JH-1042", name="Sharma Digital Seva Kendra"):
        return self.client.post("/api/enroll/verify", json={"csp_id": code, "name": name})

    def enroll(self, key=None, code="CSP-JH-1042", name="Sharma Digital Seva Kendra", host="DESKTOP-1"):
        key = key or new_key()
        r = self.client.post("/api/enroll", headers={"X-API-Key": key}, json={"csp_id": code, "name": name, "hostname": host, "agent_version": "2.1.0"})
        return key, r

    def status(self, key, code="CSP-JH-1042"):
        return self.client.post("/api/enroll/status", headers={"X-API-Key": key}, json={"csp_id": code})

    def heartbeat(self, key, code="CSP-JH-1042"):
        body = {"csp_id": code, "schema_version": 4, "agent_version": "2.1.0",
                "printer": {"configured": False}, "microatm": {"configured": False}}
        return self.client.post("/api/report", headers={"X-API-Key": key}, json=body)

    def admin(self):
        self._login()

    def post_admin(self, path, **data):
        token = _extract_csrf(self.client.get("/enrollments").get_data(as_text=True))
        return self.client.post(path, data=dict(csrf_token=token, **data), follow_redirects=True)

    def approve_latest(self, **extra):
        eid = self.q("SELECT id FROM enrollments WHERE status='PENDING' ORDER BY id DESC")[0]["id"]
        return self.post_admin("/enrollments", action="approve", enrollment_id=eid, **extra)


class VerifyTests(EnrollBase):
    def test_a_valid_code_and_matching_name_verifies(self):
        r = self.verify()
        self.assertEqual((r.status_code, r.get_json()["verdict"]), (200, "ok"))

    def test_names_match_regardless_of_case_spacing_and_trailing_punctuation(self):
        for name in ("sharma digital seva kendra", "  SHARMA   Digital  Seva Kendra. ", "Sharma Digital Seva Kendra"):
            self.assertEqual(self.verify(name=name).get_json()["verdict"], "ok", name)

    def test_an_unregistered_code_is_allowed_through_for_the_admin_to_check(self):
        r = self.verify(code="1A850657", name="Shyam Kumar")
        self.assertEqual((r.status_code, r.get_json()["verdict"]), (200, "not_registered"))
        self.assertTrue(r.get_json()["ok"])
        self.assertIn("administrator", r.get_json()["message"])

    def test_a_wrong_name_for_a_real_code_is_a_mismatch(self):
        for name in ("Sharma Digital", "Verma Digital Seva Kendra", "Sharma Digital Seva Kendra Two"):
            r = self.verify(name=name)
            self.assertEqual((r.status_code, r.get_json()["verdict"]), (400, "name_mismatch"), name)

    def test_blank_or_malformed_input_is_invalid_not_an_error(self):
        for code, name in (("", "x y"), ("CSP-JH-1042", ""), ("CSP JH", "Name"), ("a" * 80, "Name"), ("CSP-JH-1042", "x")):
            self.assertEqual(self.verify(code, name).get_json()["verdict"], "invalid", (code, name))
        self.assertEqual(self.client.post("/api/enroll/verify", data="junk").status_code, 400)

    def test_the_registered_name_is_never_revealed_to_the_asker(self):
        text = self.verify(name="Wrong Name").get_data(as_text=True)
        self.assertNotIn("Sharma", text)

    def test_an_existing_csp_with_an_api_key_counts_as_registered(self):
        issue_test_key("OLD-CSP-1", "Old Existing CSP")
        self.assertEqual(self.verify("OLD-CSP-1", "old existing csp").get_json()["verdict"], "ok")

    def test_an_existing_csp_without_a_stored_name_is_allowed_but_flagged_for_the_admin(self):
        issue_test_key("NONAME-1", "")
        self.assertEqual(self.verify("NONAME-1", "Any Name Here").get_json()["verdict"], "no_name_on_record")

    def test_guessing_is_rate_limited(self):
        codes = [self.verify(code="GUESS-%03d" % i).status_code for i in range(30)]
        self.assertEqual(codes[0], 200)
        self.assertIn(429, codes)


class EnrollmentAuthorization(EnrollBase):
    def test_enrolling_creates_a_pending_request_and_the_key_does_not_work_yet(self):
        key, r = self.enroll()
        self.assertEqual((r.status_code, r.get_json()["status"]), (200, "pending"))
        self.assertEqual(self.status(key).get_json()["status"], "pending")
        self.assertEqual(self.heartbeat(key).status_code, 401)                  # code + name alone grant nothing
        self.assertEqual(self.q("SELECT * FROM api_keys WHERE csp_id='CSP-JH-1042'"), [])

    def test_a_wrong_name_for_a_registered_code_cannot_even_request(self):
        self.assertEqual(self.enroll(name="Somebody Else")[1].status_code, 400)
        self.assertEqual(self.q("SELECT * FROM enrollments"), [])

    def test_an_unregistered_code_creates_a_pending_request_and_the_key_does_not_work_yet(self):
        key, r = self.enroll(code="1A850657", name="Shyam Kumar")
        self.assertEqual((r.status_code, r.get_json()["status"]), (200, "pending"))
        self.assertEqual(self.status(key, "1A850657").get_json()["status"], "pending")
        self.assertEqual(self.heartbeat(key, "1A850657").status_code, 401)
        row = self.q("SELECT * FROM enrollments WHERE csp_id='1A850657'")[0]
        self.assertEqual((row["status"], row["registry_name"], row["replaces_existing"]), ("PENDING", None, 0))
        self.assertEqual(self.q("SELECT * FROM api_keys WHERE csp_id='1A850657'"), [])

    def test_approving_an_unregistered_csp_activates_its_key_and_registers_it(self):
        key, _ = self.enroll(code="1A850657", name="Shyam Kumar")
        self.admin()
        self.approve_latest()
        self.assertEqual(self.status(key, "1A850657").get_json()["status"], "approved")
        self.assertEqual(self.heartbeat(key, "1A850657").status_code, 200)
        self.assertEqual(self.q("SELECT name, pre_approved FROM csp_registry WHERE csp_id='1A850657'"),
                         [{"name": "Shyam Kumar", "pre_approved": 0}])
        # once registered, a later install must type the matching name
        self.assertEqual(self.verify("1A850657", "Someone Else").get_json()["verdict"], "name_mismatch")

    def test_an_unregistered_csp_is_never_auto_approved(self):
        self.admin()
        self.post_admin("/enrollments", action="approve_matching")
        key, r = self.enroll(code="1A850657", name="Shyam Kumar")
        self.post_admin("/enrollments", action="approve_matching")
        self.assertEqual(r.get_json()["status"], "pending")
        self.assertEqual(self.status(key, "1A850657").get_json()["status"], "pending")
        self.assertEqual(self.heartbeat(key, "1A850657").status_code, 401)

    def test_an_unregistered_request_is_marked_on_the_enrollments_page(self):
        self.enroll(code="1A850657", name="Shyam Kumar")
        self.enroll()                                              # a registered one, for contrast
        self.admin()
        html = self.client.get("/enrollments").get_data(as_text=True)
        self.assertEqual(html.count("Not in registry"), 1)
        self.assertIn("(not in registry)", html)
        self.assertIn("Name matches", html)

    def test_rejecting_an_unregistered_csp_leaves_its_key_useless_and_does_not_register_it(self):
        key, _ = self.enroll(code="1A850657", name="Shyam Kumar")
        self.admin()
        eid = self.q("SELECT id FROM enrollments WHERE status='PENDING'")[0]["id"]
        self.post_admin("/enrollments", action="reject", enrollment_id=eid)
        self.assertEqual(self.status(key, "1A850657").get_json()["status"], "rejected")
        self.assertEqual(self.heartbeat(key, "1A850657").status_code, 401)
        self.assertEqual(self.q("SELECT * FROM csp_registry WHERE csp_id='1A850657'"), [])

    def test_an_invalid_key_shape_is_refused(self):
        for key in ("", "short", "x" * 100, "has spaces in the key abcdefghijklmnopqrstuvwxyz"):
            r = self.client.post("/api/enroll", headers={"X-API-Key": key}, json={"csp_id": "CSP-JH-1042", "name": "Sharma Digital Seva Kendra"})
            self.assertEqual(r.status_code, 400, key)

    def test_admin_approval_activates_exactly_that_key(self):
        key, _ = self.enroll()
        self.admin()
        self.approve_latest()
        self.assertEqual(self.status(key).get_json()["status"], "approved")
        self.assertEqual(self.heartbeat(key).status_code, 200)
        row = self.q("SELECT * FROM api_keys WHERE csp_id='CSP-JH-1042'")[0]
        self.assertEqual(row["api_key_hash"], hash_api_key(key))
        self.assertEqual(row["name"], "Sharma Digital Seva Kendra")
        self.assertEqual(self.heartbeat(new_key()).status_code, 401)             # nobody else's key works

    def test_only_the_key_holder_can_ask_about_a_request(self):
        key, _ = self.enroll()
        self.assertEqual(self.status(new_key()).status_code, 404)
        self.assertEqual(self.status(key, code="OTHER-CODE").status_code, 404)

    def test_the_server_never_stores_the_key_only_its_hash(self):
        key, _ = self.enroll()
        self.admin()
        self.approve_latest()
        import db
        with db.get_connection() as conn:
            dump = " ".join(str(tuple(r)) for t in ("enrollments", "api_keys", "admin_audit_log")
                            for r in conn.execute("SELECT * FROM %s" % t).fetchall())
        self.assertNotIn(key, dump)
        self.assertIn(hash_api_key(key), dump)

    def test_rejecting_leaves_the_key_useless(self):
        key, _ = self.enroll()
        self.admin()
        eid = self.q("SELECT id FROM enrollments")[0]["id"]
        self.post_admin("/enrollments", action="reject", enrollment_id=eid, note="not ours")
        self.assertEqual(self.status(key).get_json()["status"], "rejected")
        self.assertEqual(self.heartbeat(key).status_code, 401)

    def test_repeating_the_same_request_is_idempotent(self):
        key, r1 = self.enroll()
        _, r2 = self.enroll(key=key)
        self.assertEqual(r1.get_json()["request_id"], r2.get_json()["request_id"])
        self.assertEqual(len(self.q("SELECT * FROM enrollments")), 1)

    def test_a_newer_request_supersedes_an_older_pending_one(self):
        k1, _ = self.enroll()
        k2, _ = self.enroll(host="DESKTOP-2")
        self.assertEqual(self.status(k1).get_json()["status"], "superseded")
        self.assertEqual(self.status(k2).get_json()["status"], "pending")
        self.assertEqual(len(self.q("SELECT * FROM enrollments WHERE status='PENDING'")), 1)

    def test_one_key_cannot_be_reused_for_another_csp(self):
        self.add_registry("CSP-XX-0001", "Other Kendra")
        key, _ = self.enroll()
        r = self.client.post("/api/enroll", headers={"X-API-Key": key}, json={"csp_id": "CSP-XX-0001", "name": "Other Kendra"})
        self.assertEqual(r.status_code, 409)

    def test_requests_are_rate_limited(self):
        codes = [self.enroll()[1].status_code for _ in range(25)]
        self.assertIn(429, codes)

    def test_pre_approval_by_the_admin_makes_a_matching_first_enrollment_automatic(self):
        self.add_registry("CSP-PRE-0001", "Pre Approved Kendra", pre=1)
        key, r = self.enroll(code="CSP-PRE-0001", name="pre approved kendra")
        self.assertEqual(r.get_json()["status"], "approved")
        self.assertEqual(self.heartbeat(key, "CSP-PRE-0001").status_code, 200)

    def test_pre_approval_never_applies_to_a_csp_that_already_has_an_agent(self):
        issue_test_key("CSP-PRE-0002", "Pre Existing")
        self.add_registry("CSP-PRE-0002", "Pre Existing", pre=1)
        _, r = self.enroll(code="CSP-PRE-0002", name="Pre Existing")
        self.assertEqual(r.get_json()["status"], "pending")


class ExistingCspsAreProtected(EnrollBase):
    def setUp(self):
        super().setUp()
        self.old_key = issue_test_key("CSP-JH-1042", "Sharma Digital Seva Kendra")
        self.heartbeat(self.old_key)                                   # real history exists

    def test_a_new_enrollment_for_an_existing_csp_is_flagged_and_changes_nothing_until_approved(self):
        key, r = self.enroll()
        self.assertEqual(r.get_json()["status"], "pending")
        self.assertEqual(self.q("SELECT replaces_existing FROM enrollments")[0]["replaces_existing"], 1)
        self.assertEqual(self.heartbeat(self.old_key).status_code, 200)      # the old agent keeps working
        self.assertEqual(self.heartbeat(key).status_code, 401)

    def test_replacing_needs_an_explicit_confirmation(self):
        key, _ = self.enroll()
        self.admin()
        page = self.approve_latest().get_data(as_text=True)
        self.assertIn("already has an agent", page)
        self.assertEqual(self.heartbeat(key).status_code, 401)               # not approved
        self.assertEqual(self.heartbeat(self.old_key).status_code, 200)

    def test_confirmed_replacement_swaps_the_key_but_keeps_all_history(self):
        import db
        with db.get_connection() as conn:
            conn.execute("INSERT INTO events (csp_id, event_type, detail, occurred_at) VALUES ('CSP-JH-1042','printer_connected','x','2026-01-01')")
            conn.commit()
        before = self.q("SELECT * FROM csps WHERE csp_id='CSP-JH-1042'")[0]
        key, _ = self.enroll()
        self.admin()
        self.approve_latest(confirm_replace="1")
        self.assertEqual(self.heartbeat(key).status_code, 200)
        self.assertEqual(self.heartbeat(self.old_key).status_code, 401)
        after = self.q("SELECT * FROM csps WHERE csp_id='CSP-JH-1042'")
        self.assertEqual(len(after), 1)                                       # no duplicate CSP record
        self.assertEqual(after[0]["first_seen"], before["first_seen"])        # same record, history kept
        self.assertEqual(len(self.q("SELECT * FROM events WHERE csp_id='CSP-JH-1042' AND event_type='printer_connected'")), 1)
        self.assertEqual(len(self.q("SELECT * FROM api_keys WHERE csp_id='CSP-JH-1042'")), 1)

    def test_an_existing_agent_never_needs_to_enroll_its_key_keeps_working(self):
        for _ in range(3):
            self.assertEqual(self.heartbeat(self.old_key).status_code, 200)


class AdminControls(EnrollBase):
    def test_the_enrollment_pages_require_login(self):
        self.assertEqual(self.client.get("/enrollments").status_code, 302)
        self.assertEqual(self.client.post("/enrollments", data={"action": "approve_matching"}).status_code in (302, 400), True)

    def test_approval_requires_a_csrf_token(self):
        self.enroll()
        self.admin()
        eid = self.q("SELECT id FROM enrollments")[0]["id"]
        r = self.client.post("/enrollments", data={"action": "approve", "enrollment_id": eid})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.q("SELECT status FROM enrollments")[0]["status"], "PENDING")

    def test_every_decision_is_in_the_audit_log(self):
        key, _ = self.enroll()
        self.admin()
        self.approve_latest()
        actions = [a["action"] for a in self.q("SELECT action FROM admin_audit_log")]
        self.assertIn("enrollment_requested", actions)
        self.assertIn("enrollment_approved", actions)

    def test_approve_matching_only_approves_exact_name_matches_for_new_agents(self):
        self.add_registry("CSP-B-0001", "Beta Kendra")
        self.enroll()
        self.client.post("/api/enroll", headers={"X-API-Key": new_key()}, json={"csp_id": "CSP-B-0001", "name": "Beta Kendra"})
        self.admin()
        page = self.post_admin("/enrollments", action="approve_matching").get_data(as_text=True)
        self.assertIn("Approved 2 request(s)", page)

    def test_registry_import_never_overwrites_existing_names_by_default(self):
        self.admin()
        page = self.post_admin("/enrollments", action="import_registry",
                               registry_text="CSP-JH-1042,Totally Different Name\nNEW-0001,New Place\nbad line").get_data(as_text=True)
        self.assertIn("1 added", page)
        self.assertIn("already registered under a different name", page)
        self.assertEqual(self.q("SELECT name FROM csp_registry WHERE csp_id='CSP-JH-1042'")[0]["name"], "Sharma Digital Seva Kendra")
        self.post_admin("/enrollments", action="import_registry", registry_text="CSP-JH-1042,Renamed Kendra", overwrite="1")
        self.assertEqual(self.q("SELECT name FROM csp_registry WHERE csp_id='CSP-JH-1042'")[0]["name"], "Renamed Kendra")

    def test_pending_requests_show_in_the_menu_count_and_the_page_escapes_markup(self):
        self.add_registry("CSP-XSS-1", "<b>Bold</b> Kendra")
        self.client.post("/api/enroll", headers={"X-API-Key": new_key()},
                         json={"csp_id": "CSP-XSS-1", "name": "<b>Bold</b> Kendra", "hostname": "<script>alert(1)</script>"})
        self.admin()
        page = self.client.get("/enrollments").get_data(as_text=True)
        self.assertNotIn("<script>alert", page)
        self.assertIn("&lt;script&gt;", page)
        self.assertRegex(self.client.get("/").get_data(as_text=True), r"Enrollments.*?sev-high")


if __name__ == "__main__":
    unittest.main()
