"""Centralized signed updates, server side: releases, rollouts, targeting, the agent-facing endpoints, the
state machine, and the rule that SUCCESS needs the new version's own healthy heartbeat."""
import base64
import hashlib
import io
import json
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import updates
from tests.helpers import issue_test_key
from tests.release_fixtures import Signer
from tests.test_routes import RouteTestCase, _extract_csrf


class UpdBase(RouteTestCase):
    def setUp(self):
        super().setUp()
        self.signer = Signer()
        self.addCleanup(self.signer.cleanup)
        self.keys = {}

    # ----- helpers
    def q(self, sql, *a):
        import db
        with db.get_connection() as conn:
            return [dict(r) for r in conn.execute(sql, a).fetchall()]

    def csp(self, code, capable=True, seq=1, version="2.0.0", name=None):
        """Create a CSP with a real key and one real heartbeat."""
        self.keys[code] = issue_test_key(code, name or ("CSP " + code))
        self.beat(code, capable=capable, seq=seq, version=version)
        return self.keys[code]

    def beat(self, code, capable=True, seq=1, version="2.0.0", update_result=None):
        body = {"csp_id": code, "schema_version": 4, "agent_version": version,
                "printer": {"configured": False}, "microatm": {"configured": False}}
        if capable:
            body["capabilities"] = {"remote_update": True, "updater": "1.0"}
            body["agent_sequence"] = seq
        if update_result:
            body["update_result"] = update_result
        r = self.client.post("/api/report", headers={"X-API-Key": self.keys[code]}, json=body)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        return r.get_json()

    def release(self, version="2.1.0", seq=2, approve=True, **kw):
        import db
        pkg, man, sig = self.signer.release(version, seq, **kw)
        with db.get_connection() as conn:
            rid = updates.add_release(conn, pkg, man, sig, "admin")
            if approve:
                updates.approve_release(conn, rid, "admin")
            conn.commit()
        return rid

    def rollout(self, rid, codes, kind="pilot"):
        import db
        with db.get_connection() as conn:
            out = updates.create_rollout(conn, rid, codes, kind, "admin")
            conn.commit()
        return out

    def dep(self, code):
        rows = self.q("SELECT * FROM update_deployments WHERE csp_id=? ORDER BY id DESC", code)
        return rows[0] if rows else None

    def status(self, code, dep, status, detail="", attempt=None):
        return self.client.post("/api/update/status", headers={"X-API-Key": self.keys[code]},
                                json={"csp_id": code, "deployment_id": dep["id"], "attempt": attempt or dep["attempt"],
                                      "status": status, "detail": detail})

    def admin_post(self, path, page="/updates", **data):
        token = _extract_csrf(self.client.get(page).get_data(as_text=True))
        return self.client.post(path, data=dict(csrf_token=token, **data), follow_redirects=True)


class ReleaseValidation(UpdBase):
    def add(self, pkg, man, sig):
        import db
        with db.get_connection() as conn:
            rid = updates.add_release(conn, pkg, man, sig, "admin")
            conn.commit()
        return rid

    def test_a_valid_signed_release_is_stored_but_not_approved(self):
        rid = self.release(approve=False)
        row = self.q("SELECT * FROM agent_releases WHERE id=?", rid)[0]
        self.assertEqual((row["version"], row["status"], row["sequence"]), ("2.1.0", "UPLOADED", 2))
        self.assertTrue(os.path.exists(row["package_path"]))

    def test_refused_when_the_server_has_no_public_key(self):
        pkg, man, sig = self.signer.release()
        os.environ["CSP_MONITOR_RELEASE_PUBKEYS"] = os.path.join(self.signer.release_dir, "missing.json")
        with self.assertRaises(updates.UpdateError) as cm:
            self.add(pkg, man, sig)
        self.assertIn("public key", str(cm.exception))

    def test_a_modified_package_is_rejected(self):
        pkg, man, sig = self.signer.release()
        bad = bytearray(pkg)
        bad[len(bad) // 2] ^= 0xFF
        with self.assertRaises(updates.UpdateError):
            self.add(bytes(bad), man, sig)
        self.assertEqual(self.q("SELECT * FROM agent_releases"), [])

    def test_a_modified_manifest_or_signature_is_rejected(self):
        pkg, man, sig = self.signer.release()
        m = json.loads(man)
        m["sequence"] = 99
        with self.assertRaises(updates.UpdateError):
            self.add(pkg, json.dumps(m, indent=2, sort_keys=True).encode(), sig)
        with self.assertRaises(updates.UpdateError):
            self.add(pkg, man, base64.b64encode(os.urandom(64)).decode())
        with self.assertRaises(updates.UpdateError):
            self.add(pkg, man, "garbage!")

    def test_a_release_signed_by_somebody_else_is_rejected(self):
        pkg, man, sig = Signer("k1").release()                  # a different key pair, same key id
        os.environ["CSP_MONITOR_RELEASE_PUBKEYS"] = self.signer.keys_path
        with self.assertRaises(updates.UpdateError) as cm:
            self.add(pkg, man, sig)
        self.assertIn("signature", str(cm.exception))

    def test_a_signed_release_with_a_dangerous_path_is_still_rejected(self):
        import release_tool as rt
        files = self.signer.files({"../evil.py": b"x"})
        with self.assertRaises(SystemExit):                     # the tool itself refuses to build it
            rt.build_release(files, "9.9.9", 9, "k1", self.signer.secret, out_dir=self.signer.release_dir)

    def test_binaries_and_data_files_cannot_be_shipped(self):
        import release_tool as rt
        for name in ("tool.exe", "x.dll", "agent_config.json"):
            with self.assertRaises((SystemExit, Exception)):
                rt.build_release(self.signer.files({name: b"x"}), "9.9.9", 9, "k1", self.signer.secret, out_dir=self.signer.release_dir)

    def test_the_sequence_must_keep_increasing(self):
        self.release("2.1.0", 5)
        pkg, man, sig = self.signer.release("2.0.5", 3)          # a genuine OLD release, correctly signed
        with self.assertRaises(updates.UpdateError) as cm:
            self.add(pkg, man, sig)
        self.assertIn("not higher", str(cm.exception))

    def test_a_version_cannot_be_uploaded_twice(self):
        self.release("2.1.0", 2)
        pkg, man, sig = self.signer.release("2.1.0", 3)
        with self.assertRaises(updates.UpdateError):
            self.add(pkg, man, sig)

    def test_a_rollback_release_is_accepted_when_its_sequence_is_higher(self):
        self.release("2.1.0", 2)
        rid = self.release("2.1.1", 3, kind="rollback")
        self.assertEqual(self.q("SELECT kind FROM agent_releases WHERE id=?", rid)[0]["kind"], "rollback")

    def test_deployment_needs_an_approved_release(self):
        rid = self.release(approve=False)
        self.csp("A1")
        with self.assertRaises(updates.UpdateError):
            self.rollout(rid, ["A1"])

    def test_upload_through_the_dashboard_needs_login_and_accepts_a_large_package(self):
        pkg, man, sig = self.signer.release(files=self.signer.files({"big.txt": os.urandom(600 * 1024)}))   # > the 256 KB general cap
        data = lambda: {"package": (io.BytesIO(pkg), "p.zip"), "manifest": (io.BytesIO(man), "m.json"), "signature": (io.BytesIO(sig.encode()), "s.sig")}
        self.assertIn(self.client.post("/updates/upload", data=data(), content_type="multipart/form-data").status_code, (302, 400))
        self._login()
        token = _extract_csrf(self.client.get("/updates").get_data(as_text=True))
        d = data(); d["csrf_token"] = token
        page = self.client.post("/updates/upload", data=d, content_type="multipart/form-data", follow_redirects=True).get_data(as_text=True)
        self.assertIn("Release uploaded and verified", page)
        self.assertEqual(len(self.q("SELECT * FROM agent_releases")), 1)

    def test_the_general_request_size_cap_still_applies_everywhere_else(self):
        self._login()
        token = _extract_csrf(self.client.get("/updates").get_data(as_text=True))
        r = self.client.post("/updates/action", data={"csrf_token": token, "action": "x", "pad": "A" * (400 * 1024)})
        self.assertEqual(r.status_code, 413)

    def test_upload_with_a_missing_file_is_refused_politely(self):
        self._login()
        token = _extract_csrf(self.client.get("/updates").get_data(as_text=True))
        page = self.client.post("/updates/upload", data={"csrf_token": token}, content_type="multipart/form-data", follow_redirects=True).get_data(as_text=True)
        self.assertIn("Choose all three files", page)


class Targeting(UpdBase):
    def test_selected_csps_only_and_the_skip_reasons_are_given(self):
        rid = self.release("2.1.0", 5)
        self.csp("OK1")
        self.csp("OK2")
        self.csp("LEGACY", capable=False)
        self.csp("CURRENT", seq=5)                                      # already on it
        _, created, skipped = self.rollout(rid, ["OK1", "LEGACY", "CURRENT", "GHOST"])
        reasons = dict(skipped)
        self.assertEqual(created, 1)
        self.assertIn("one-time bootstrap", reasons["LEGACY"])
        self.assertIn("already", reasons["CURRENT"])
        self.assertIn("unknown", reasons["GHOST"])
        self.assertIsNone(self.dep("OK2"))

    def test_all_compatible_means_capable_and_not_already_updated_and_not_too_old(self):
        rid = self.release("2.1.0", 5, min_agent_sequence=2)
        self.csp("NEW1", seq=3)
        self.csp("NEW2", seq=2)
        self.csp("TOOOLD", seq=1)
        self.csp("LEGACY", capable=False)
        self.csp("DONE", seq=5)
        import db
        with db.get_connection() as conn:
            self.assertEqual(updates.compatible_csp_ids(conn, rid), ["NEW1", "NEW2"])

    def test_assigning_twice_is_idempotent(self):
        rid = self.release()
        self.csp("A1")
        self.rollout(rid, ["A1"])
        _, created, skipped = self.rollout(rid, ["A1"])
        self.assertEqual(created, 0)
        self.assertIn("already has this release", skipped[0][1])
        self.assertEqual(len(self.q("SELECT * FROM update_deployments")), 1)

    def test_a_group_targets_exactly_its_members_through_the_dashboard(self):
        rid = self.release()
        for c in ("G1", "G2", "OUT"):
            self.csp(c)
        self._login()
        self.admin_post("/updates/action", action="create_group", name="Pilot Group", members="G1 G2 NOT-A-CSP")
        gid = self.q("SELECT id FROM csp_groups")[0]["id"]
        page = self.admin_post("/updates/action", action="create_rollout", release_id=rid, mode="group", group_id=gid, kind="pilot").get_data(as_text=True)
        self.assertIn("2 CSP(s) will be offered", page)
        self.assertIsNone(self.dep("OUT"))

    def test_fleet_wide_rollout_through_the_dashboard(self):
        rid = self.release()
        for c in ("F1", "F2", "F3"):
            self.csp(c)
        self.csp("OLDAGENT", capable=False)
        self._login()
        page = self.admin_post("/updates/action", action="create_rollout", release_id=rid, mode="all", kind="full").get_data(as_text=True)
        self.assertIn("3 CSP(s) will be offered", page)
        self.assertEqual(len(self.q("SELECT * FROM update_deployments")), 3)

    def test_selecting_csps_with_checkboxes(self):
        rid = self.release()
        self.csp("S1")
        self.csp("S2")
        self._login()
        self.admin_post("/updates/action", action="create_rollout", release_id=rid, mode="selected", kind="pilot", csp_ids="S1")
        self.assertIsNotNone(self.dep("S1"))
        self.assertIsNone(self.dep("S2"))


class Heartbeat(UpdBase):
    def test_a_legacy_agent_is_never_offered_an_update(self):
        rid = self.release()
        self.csp("OLD", capable=False)
        self.assertNotIn("update", self.beat("OLD", capable=False))

    def test_a_capable_agent_is_offered_its_assignment_and_it_becomes_assigned(self):
        rid = self.release("2.1.0", 2)
        self.csp("A1")
        self.rollout(rid, ["A1"])
        offer = self.beat("A1")["update"]
        d = self.dep("A1")
        self.assertEqual((offer["deployment_id"], offer["version"], offer["sequence"], offer["attempt"]), (d["id"], "2.1.0", 2, 1))
        self.assertEqual(d["status"], "assigned")

    def test_repeated_heartbeats_re_offer_the_same_deployment_without_duplicates(self):
        rid = self.release()
        self.csp("A1")
        self.rollout(rid, ["A1"])
        first, second = self.beat("A1")["update"], self.beat("A1")["update"]
        self.assertEqual(first, second)
        self.assertEqual(len(self.q("SELECT * FROM update_deployments")), 1)

    def test_nothing_is_offered_without_an_assignment_and_the_response_stays_backward_compatible(self):
        self.csp("A1")
        resp = self.beat("A1")
        self.assertEqual(set(resp), {"ok", "received_at"})

    def test_an_agent_that_already_has_the_sequence_is_not_offered_it(self):
        rid = self.release("2.1.0", 2)
        self.csp("A1")
        self.rollout(rid, ["A1"])
        self.assertNotIn("update", self.beat("A1", seq=2))              # installed by hand in the meantime

    def test_paused_and_cancelled_rollouts_offer_nothing_and_resume_offers_again(self):
        rid = self.release()
        self.csp("A1")
        ro, _, _ = self.rollout(rid, ["A1"])
        import db
        with db.get_connection() as conn:
            updates.set_rollout_status(conn, ro, "PAUSED", "admin")
            conn.commit()
        self.assertNotIn("update", self.beat("A1"))
        with db.get_connection() as conn:
            updates.set_rollout_status(conn, ro, "ACTIVE", "admin")
            conn.commit()
        self.assertIn("update", self.beat("A1"))
        with db.get_connection() as conn:
            updates.set_rollout_status(conn, ro, "CANCELLED", "admin")
            conn.commit()
        self.assertNotIn("update", self.beat("A1"))
        self.assertEqual(self.dep("A1")["status"], "cancelled")

    def test_a_revoked_release_is_never_offered_and_waiting_deployments_are_cancelled(self):
        rid = self.release()
        self.csp("A1")
        self.csp("A2")
        self.rollout(rid, ["A1", "A2"])
        import db
        with db.get_connection() as conn:
            self.assertEqual(updates.revoke_release(conn, rid, "admin"), 2)
            conn.commit()
        self.assertNotIn("update", self.beat("A1"))
        self.assertEqual(self.dep("A2")["status"], "cancelled")

    def test_capability_is_recorded_and_not_wiped_by_a_plain_heartbeat(self):
        self.csp("A1", seq=4)
        row = self.q("SELECT update_capable, updater_version, agent_sequence FROM csps WHERE csp_id='A1'")[0]
        self.assertEqual((row["update_capable"], row["updater_version"], row["agent_sequence"]), (1, "1.0", 4))
        self.beat("A1", capable=False)
        self.assertEqual(self.q("SELECT update_capable FROM csps WHERE csp_id='A1'")[0]["update_capable"], 1)


class AgentEndpoints(UpdBase):
    def setUp(self):
        super().setUp()
        self.rid = self.release("2.1.0", 2)
        self.csp("A1")
        self.csp("A2")
        self.rollout(self.rid, ["A1"])
        self.beat("A1")
        self.d = self.dep("A1")

    def get(self, path, code="A1", key=None, **params):
        params = dict({"csp_id": code, "deployment_id": self.d["id"]}, **params)
        return self.client.get(path, query_string=params, headers={"X-API-Key": key if key is not None else self.keys.get(code, "none")})

    def test_the_manifest_is_the_exact_signed_bytes(self):
        r = self.get("/api/update/manifest")
        j = r.get_json()
        row = self.q("SELECT * FROM agent_releases WHERE id=?", self.rid)[0]
        self.assertEqual((r.status_code, j["manifest_b64"], j["signature_b64"]), (200, row["manifest_b64"], row["signature_b64"]))
        import ed25519
        pub = bytes.fromhex(self.signer.keys["keys"][0]["public"])
        self.assertTrue(ed25519.verify(pub, base64.b64decode(j["manifest_b64"]), base64.b64decode(j["signature_b64"])))

    def test_the_package_downloads_and_matches_its_signed_hash(self):
        r = self.get("/api/update/package")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(hashlib.sha256(r.data).hexdigest(), r.headers["X-Package-SHA256"])

    def test_an_interrupted_download_can_resume_with_a_range_request(self):
        full = self.get("/api/update/package").data
        part = self.client.get("/api/update/package", query_string={"csp_id": "A1", "deployment_id": self.d["id"]},
                               headers={"X-API-Key": self.keys["A1"], "Range": "bytes=100-"})
        self.assertEqual(part.status_code, 206)
        self.assertEqual(part.data, full[100:])

    def test_authentication_is_required_and_a_csp_cannot_read_anothers_deployment(self):
        for path in ("/api/update/manifest", "/api/update/package"):
            self.assertEqual(self.get(path, key="wrong-key").status_code, 401)
            self.assertEqual(self.get(path, key="").status_code, 401)
            self.assertEqual(self.get(path, code="A2").status_code, 404)            # A2's valid key, A1's deployment
        self.assertEqual(self.client.get("/api/update/manifest", query_string={"deployment_id": self.d["id"]}).status_code, 401)

    def test_bad_deployment_ids_are_404_not_errors(self):
        for bad in ("abc", "999999", "-1", "1 OR 1=1", ""):
            self.assertEqual(self.get("/api/update/manifest", deployment_id=bad).status_code, 404, bad)

    def test_no_download_once_paused_cancelled_or_revoked(self):
        import db
        ro = self.d["rollout_id"]
        with db.get_connection() as conn:
            updates.set_rollout_status(conn, ro, "PAUSED", "admin")
            conn.commit()
        self.assertEqual(self.get("/api/update/package").status_code, 409)
        with db.get_connection() as conn:
            updates.set_rollout_status(conn, ro, "ACTIVE", "admin")
            updates.revoke_release(conn, self.rid, "admin")
            conn.commit()
        self.assertEqual(self.get("/api/update/manifest").status_code, 409)


class StateMachine(UpdBase):
    def setUp(self):
        super().setUp()
        rid = self.release("2.1.0", 2)
        self.csp("A1")
        self.rollout(rid, ["A1"])
        self.beat("A1")
        self.d = self.dep("A1")

    def test_progress_moves_forward_and_proceeds(self):
        for s in ("downloading", "verifying", "installing", "restarting"):
            r = self.status("A1", self.d, s).get_json()
            self.assertEqual((r["ok"], r["proceed"]), (True, True))
            self.assertEqual(self.dep("A1")["status"], s)

    def test_status_never_moves_backwards_and_repeats_are_harmless(self):
        self.status("A1", self.d, "installing")
        self.status("A1", self.d, "downloading")                            # late/out-of-order
        self.status("A1", self.d, "installing")                             # duplicate
        self.assertEqual(self.dep("A1")["status"], "installing")

    def test_a_stale_attempt_cannot_change_anything(self):
        self.status("A1", self.d, "failed", "boom")
        import db
        with db.get_connection() as conn:
            updates.retry_deployment(conn, self.d["id"], "admin")
            conn.commit()
        r = self.status("A1", self.d, "installing", attempt=1).get_json()    # the old attempt number
        self.assertEqual(r["proceed"], False)
        self.assertEqual(self.dep("A1")["status"], "pending")

    def test_failed_records_the_error_and_stops(self):
        r = self.status("A1", self.d, "failed", "signature check failed").get_json()
        d = self.dep("A1")
        self.assertEqual((r["proceed"], d["status"], d["last_error"]), (False, "failed", "signature check failed"))
        self.assertIn("agent_update_failed", [e["event_type"] for e in self.q("SELECT event_type FROM events")])

    def test_rollback_reporting(self):
        self.status("A1", self.d, "installing")
        self.status("A1", self.d, "rollback_in_progress", "new version did not start")
        self.assertEqual(self.dep("A1")["status"], "rollback_in_progress")
        self.status("A1", self.d, "rolled_back", "restored 2.0.0")
        d = self.dep("A1")
        self.assertEqual((d["status"], d["last_error"]), ("rolled_back", "restored 2.0.0"))

    def test_the_agent_is_told_to_stop_when_the_admin_cancels_mid_flight(self):
        self.status("A1", self.d, "downloading")
        import db
        with db.get_connection() as conn:
            updates.cancel_deployment(conn, self.d["id"], "admin")
            conn.commit()
        r = self.status("A1", self.d, "installing").get_json()
        self.assertEqual(r["proceed"], False)
        self.assertEqual(self.dep("A1")["status"], "cancelled")

    def test_unknown_statuses_and_other_csps_are_refused(self):
        self.assertEqual(self.status("A1", self.d, "hacked").status_code, 404)
        self.csp("A2")
        r = self.client.post("/api/update/status", headers={"X-API-Key": self.keys["A2"]},
                             json={"csp_id": "A2", "deployment_id": self.d["id"], "attempt": 1, "status": "failed"})
        self.assertEqual(r.status_code, 404)
        self.assertEqual(self.dep("A1")["status"], "assigned")

    def test_retry_only_applies_to_finished_unsuccessful_deployments(self):
        import db
        with db.get_connection() as conn:
            with self.assertRaises(updates.UpdateError):
                updates.retry_deployment(conn, self.d["id"], "admin")          # still assigned
        self.status("A1", self.d, "failed", "x")
        with db.get_connection() as conn:
            updates.retry_deployment(conn, self.d["id"], "admin")
            conn.commit()
        d = self.dep("A1")
        self.assertEqual((d["status"], d["attempt"]), ("pending", 2))
        self.assertIn("update", self.beat("A1"))                                # offered again


class SuccessIsServerVerified(UpdBase):
    def setUp(self):
        super().setUp()
        rid = self.release("2.1.0", 2)
        self.csp("A1")
        self.rollout(rid, ["A1"])
        self.beat("A1")
        self.d = self.dep("A1")
        for s in ("downloading", "verifying", "installing", "restarting"):
            self.status("A1", self.d, s)

    def result(self, outcome="healthy", version="2.1.0", seq=2, detail=""):
        return self.beat("A1", seq=seq, version=version,
                         update_result={"deployment_id": self.d["id"], "attempt": 1, "outcome": outcome, "detail": detail})

    def test_assigned_downloaded_installed_restarting_are_never_success(self):
        self.assertEqual(self.dep("A1")["status"], "restarting")
        self.assertNotEqual(self.dep("A1")["status"], "successful")

    def test_success_needs_the_new_version_to_report_healthy(self):
        self.result("healthy", version="2.1.0")
        d = self.dep("A1")
        self.assertEqual(d["status"], "successful")
        self.assertIsNotNone(d["finished_at"])
        self.assertIn("agent_updated", [e["event_type"] for e in self.q("SELECT event_type FROM events")])

    def test_a_healthy_claim_from_the_OLD_version_is_not_success(self):
        self.result("healthy", version="2.0.0", seq=1)
        d = self.dep("A1")
        self.assertEqual(d["status"], "restarting")
        self.assertIn("expected 2.1.0", d["detail"])

    def test_a_duplicate_healthy_report_changes_nothing(self):
        self.result("healthy")
        before = self.dep("A1")
        self.result("healthy")
        self.assertEqual(self.dep("A1")["finished_at"], before["finished_at"])
        self.assertEqual(len([e for e in self.q("SELECT * FROM events") if e["event_type"] == "agent_updated"]), 1)

    def test_a_failed_or_rolled_back_result_is_recorded(self):
        self.result("rolled_back", version="2.0.0", seq=1, detail="new version never became healthy")
        d = self.dep("A1")
        self.assertEqual((d["status"], d["last_error"]), ("rolled_back", "new version never became healthy"))

    def test_a_healthy_report_for_another_csps_deployment_is_ignored(self):
        self.csp("A2")
        self.beat("A2", update_result={"deployment_id": self.d["id"], "attempt": 1, "outcome": "healthy"}, version="2.1.0", seq=2)
        self.assertEqual(self.dep("A1")["status"], "restarting")

    def test_malformed_results_never_break_the_heartbeat(self):
        from security import report_limiter
        for bad in ("oops", {"deployment_id": "x"}, {"deployment_id": 99999, "outcome": "healthy"}, [1], {"outcome": 5}):
            report_limiter.reset()                                      # several heartbeats in a row would hit the (correct) rate limit
            self.beat("A1", update_result=bad)


class StaleHandling(UpdBase):
    def make(self, status):
        rid = self.release("2.1.0", 2)
        self.csp("A1")
        self.rollout(rid, ["A1"])
        self.beat("A1")
        d = self.dep("A1")
        import db
        old = (datetime.now(timezone.utc) - timedelta(minutes=120)).isoformat(timespec="seconds")
        with db.get_connection() as conn:
            conn.execute("UPDATE update_deployments SET status=?, updated_at=? WHERE id=?", (status, old, d["id"]))
            conn.commit()
        return d

    def test_an_interrupted_download_is_offered_again(self):
        self.make("downloading")
        offer = self.beat("A1").get("update")
        self.assertIsNotNone(offer)
        self.assertEqual(self.dep("A1")["status"], "assigned")

    def test_a_restart_that_never_reported_back_is_failed_not_assumed_successful(self):
        self.make("restarting")
        self.beat("A1")
        d = self.dep("A1")
        self.assertEqual(d["status"], "failed")
        self.assertIn("did not report back", d["detail"])

    def test_recent_progress_is_left_alone(self):
        rid = self.release("2.1.0", 2)
        self.csp("A1")
        self.rollout(rid, ["A1"])
        self.beat("A1")
        d = self.dep("A1")
        self.status("A1", d, "downloading")
        self.beat("A1")
        self.assertEqual(self.dep("A1")["status"], "downloading")


class RolloutControl(UpdBase):
    def setUp(self):
        super().setUp()
        self.rid = self.release("2.1.0", 2)
        for c in ("P1", "P2", "P3", "P4"):
            self.csp(c)

    def test_expand_is_blocked_until_the_pilot_has_a_verified_success(self):
        ro, _, _ = self.rollout(self.rid, ["P1"])
        import db
        with db.get_connection() as conn:
            with self.assertRaises(updates.UpdateError):
                updates.expand_rollout(conn, ro, updates.compatible_csp_ids(conn, self.rid), "admin")
        self.beat("P1")
        d = self.dep("P1")
        for s in ("downloading", "verifying", "installing", "restarting"):
            self.status("P1", d, s)
        self.beat("P1", seq=2, version="2.1.0", update_result={"deployment_id": d["id"], "attempt": 1, "outcome": "healthy"})
        with db.get_connection() as conn:
            created, _ = updates.expand_rollout(conn, ro, updates.compatible_csp_ids(conn, self.rid), "admin")
            conn.commit()
        self.assertEqual(created, 3)

    def test_expand_is_blocked_after_a_pilot_failure_unless_forced(self):
        ro, _, _ = self.rollout(self.rid, ["P1", "P2"])
        self.beat("P1")
        d = self.dep("P1")
        self.status("P1", d, "failed", "x")
        import db
        with db.get_connection() as conn:
            with self.assertRaises(updates.UpdateError):
                updates.expand_rollout(conn, ro, ["P3"], "admin")
            created, _ = updates.expand_rollout(conn, ro, ["P3"], "admin", force=True)
            self.assertEqual(created, 1)

    def test_cancel_pending_leaves_work_in_progress_alone(self):
        ro, _, _ = self.rollout(self.rid, ["P1", "P2"])
        self.beat("P1")
        d = self.dep("P1")
        self.status("P1", d, "downloading")
        import db
        with db.get_connection() as conn:
            n = updates.set_rollout_status(conn, ro, "CANCELLED", "admin")
            conn.commit()
        self.assertEqual(n, 1)                                              # only P2 (pending); P1 was already downloading
        self.assertEqual(self.dep("P2")["status"], "cancelled")
        self.assertEqual(self.dep("P1")["status"], "downloading")

    def test_a_cancelled_rollout_cannot_be_changed_or_retried(self):
        ro, _, _ = self.rollout(self.rid, ["P1"])
        import db
        with db.get_connection() as conn:
            updates.set_rollout_status(conn, ro, "CANCELLED", "admin")
            with self.assertRaises(updates.UpdateError):
                updates.set_rollout_status(conn, ro, "ACTIVE", "admin")
            with self.assertRaises(updates.UpdateError):
                updates.retry_deployment(conn, self.dep("P1")["id"], "admin")


class AdminPage(UpdBase):
    def test_the_page_requires_login_and_every_action_requires_csrf(self):
        self.assertEqual(self.client.get("/updates").status_code, 302)
        self._login()
        self.assertEqual(self.client.post("/updates/action", data={"action": "approve_release", "release_id": 1}).status_code, 400)

    def test_the_page_shows_every_required_column_and_the_status_labels(self):
        rid = self.release("2.1.0", 2)
        self.csp("A1", name="Alpha Kendra")
        self.csp("LEG", capable=False)
        self.rollout(rid, ["A1"])
        self.beat("A1")
        self._login()
        page = self.client.get("/updates").get_data(as_text=True)
        for needle in ("Alpha Kendra", "A1", "2.0.0", "2.1.0", "Ready (updater 1.0)", "Legacy agent - needs the one-time bootstrap",
                       "Assigned", "Current version", "Target", "Update capability", "Last heartbeat", "Installation status", "Last attempt"):
            self.assertIn(needle, page, needle)

    def test_an_offline_csp_shows_offline_instead_of_a_progress_status(self):
        rid = self.release("2.1.0", 2)
        self.csp("A1")
        self.rollout(rid, ["A1"])
        import db
        with db.get_connection() as conn:
            conn.execute("UPDATE csps SET last_seen='2026-01-01T00:00:00+00:00' WHERE csp_id='A1'")
            conn.commit()
        self._login()
        page = self.client.get("/updates").get_data(as_text=True)
        self.assertRegex(page, r"badge-gray[^>]*><span class=\"dot\"></span>Offline")

    def test_approving_revoking_pausing_retrying_cancelling_all_work_and_are_audited(self):
        rid = self.release("2.1.0", 2, approve=False)
        self.csp("A1")
        self._login()
        self.admin_post("/updates/action", action="approve_release", release_id=rid)
        self.admin_post("/updates/action", action="create_rollout", release_id=rid, mode="selected", kind="pilot", csp_ids="A1")
        ro = self.q("SELECT id FROM rollouts")[0]["id"]
        self.admin_post("/updates/action", action="pause_rollout", rollout_id=ro)
        self.admin_post("/updates/action", action="resume_rollout", rollout_id=ro)
        dep = self.dep("A1")
        self.admin_post("/updates/action", action="cancel_deployment", deployment_id=dep["id"])
        self.admin_post("/updates/action", action="retry_deployment", deployment_id=dep["id"])
        self.admin_post("/updates/action", action="revoke_release", release_id=rid)
        actions = [a["action"] for a in self.q("SELECT action FROM admin_audit_log")]
        for expected in ("release_approved", "rollout_created", "rollout_paused", "rollout_resumed", "deployment_cancelled",
                         "deployment_retried", "release_revoked"):
            self.assertIn(expected, actions)
        page = self.client.get("/updates").get_data(as_text=True)
        self.assertIn("release_approved", page)                                # the history panel shows them

    def test_a_rollback_release_flows_through_the_same_pipeline(self):
        self.release("2.1.0", 2)
        rb = self.release("2.1.1", 3, kind="rollback")
        self.csp("A1", seq=2, version="2.1.0")
        self.rollout(rb, ["A1"])
        offer = self.beat("A1", seq=2, version="2.1.0")["update"]
        self.assertEqual((offer["version"], offer["sequence"]), ("2.1.1", 3))

    def test_page_text_from_a_release_is_escaped(self):
        self.release("2.1.0", 2, notes="<script>alert(1)</script>")
        self._login()
        page = self.client.get("/updates").get_data(as_text=True)
        self.assertNotIn("<script>alert(1)", page)
        self.assertIn("&lt;script&gt;", page)


class MigrationPreservesData(unittest.TestCase):
    def test_an_old_database_gains_the_new_tables_and_columns_and_keeps_every_row(self):
        import sqlite3
        import tempfile
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.remove(path)
        con = sqlite3.connect(path)
        con.executescript("""
            CREATE TABLE api_keys (csp_id TEXT PRIMARY KEY, api_key_hash TEXT NOT NULL, api_key_suffix TEXT NOT NULL DEFAULT '',
                                   name TEXT, active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, last_used_at TEXT);
            CREATE TABLE csps (csp_id TEXT PRIMARY KEY, name TEXT, printer_configured INTEGER NOT NULL DEFAULT 0,
                               printer_ok INTEGER NOT NULL DEFAULT 0, printer_status TEXT, agent_version TEXT, first_seen TEXT, last_seen TEXT);
            INSERT INTO api_keys VALUES ('LEGACY-1','abc123hash','1234','Old Kendra',1,'2026-01-01',NULL);
            INSERT INTO csps (csp_id, name, printer_configured, printer_ok, agent_version, first_seen, last_seen)
                VALUES ('LEGACY-1','Old Kendra',1,1,'1.2.0','2026-01-01','2026-02-01T00:00:00+00:00');
        """)
        con.commit()
        con.close()
        os.environ["CSP_MONITOR_DB_PATH"] = path
        import db
        db.setup()
        db.setup()                                                    # idempotent
        with db.get_connection() as conn:
            self.assertEqual(dict(conn.execute("SELECT api_key_hash, name, active FROM api_keys").fetchone()),
                             {"api_key_hash": "abc123hash", "name": "Old Kendra", "active": 1})
            row = dict(conn.execute("SELECT * FROM csps").fetchone())
            self.assertEqual((row["name"], row["agent_version"], row["first_seen"]), ("Old Kendra", "1.2.0", "2026-01-01"))
            for col in ("update_capable", "updater_version", "agent_sequence"):
                self.assertIn(col, row)
                self.assertIsNone(row[col])                          # legacy: unknown, so shown as "needs bootstrap"
            tables = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            for t in ("agent_releases", "rollouts", "update_deployments", "csp_groups", "csp_group_members", "csp_registry", "enrollments"):
                self.assertIn(t, tables)
        try:
            os.remove(path)
        except OSError:
            pass                                                         # Windows may still hold the file; harmless


if __name__ == "__main__":
    unittest.main()
