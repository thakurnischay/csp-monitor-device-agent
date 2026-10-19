"""The agent's update client: it must install ONLY what the administrator signed, never harm the working
installation, and survive every kind of network and server misbehaviour."""
import base64
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.update_fixtures import FakeServer, Signer, install_agent, snapshot  # noqa: E402

import apply_update as au          # noqa: E402
import update_client as uc         # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self.signer = Signer()
        self.dir = install_agent(self)
        self.before = snapshot(self.dir)
        self.launched = 0
        self.exited = 0
        self.free = 10 ** 10

    def client(self, server, sequence=1, keys=None, **kw):
        def launch():
            self.launched += 1

        def exit_process():
            self.exited += 1
        return uc.UpdateClient(self.dir, "https://server.example", "X1", "key-for-X1", keys if keys is not None else self.signer.keys,
                               "2.0.0", sequence, session=server, free_bytes=lambda path: self.free, launch=launch,
                               exit_process=exit_process, **kw)

    def offer(self, release, did=7, attempt=1):
        return {"deployment_id": did, "attempt": attempt, "version": release.info["version"], "sequence": release.info["sequence"]}

    def assertUntouched(self):
        self.assertEqual(snapshot(self.dir), self.before)

    def staged_dir(self, seq=2):
        return os.path.join(self.dir, au.STAGING, str(seq), "new")


class HappyPath(Base):
    def test_a_good_release_is_verified_staged_and_handed_over_without_touching_the_live_install(self):
        rel = self.signer.build()
        srv = FakeServer(rel)
        out = self.client(srv).run(self.offer(rel))
        self.assertEqual((out["ok"], out["handed_over"]), (True, True))
        self.assertEqual([s for s, _ in srv.statuses], ["downloading", "verifying", "installing"])
        self.assertEqual((self.launched, self.exited), (1, 1))
        self.assertUntouched()                                                 # nothing live changed yet
        st = au.read_state(self.dir)
        self.assertEqual((st["phase"], st["to_version"], st["to_sequence"], st["from_version"], st["deployment_id"]), ("staged", "2.1.0", 2, "2.0.0", 7))
        for f in rel.info["files"]:
            staged = os.path.join(self.staged_dir(), *f["path"].split("/"))
            self.assertTrue(os.path.exists(staged), f["path"])
        self.assertEqual(st["agent_pid"], os.getpid())

    def test_the_manifest_key_may_be_any_trusted_key_so_keys_can_be_rotated(self):
        other = Signer("k2")
        keys = {"keys": self.signer.keys["keys"] + other.keys["keys"]}
        rel = other.build()
        self.assertTrue(self.client(FakeServer(rel), keys=keys).run(self.offer(rel))["ok"])


class RefusesForgeries(Base):
    def run_bad(self, srv, rel, **kw):
        out = self.client(srv, **kw).run(self.offer(rel))
        self.assertFalse(out["ok"], out)
        self.assertEqual((self.launched, self.exited), (0, 0))
        self.assertUntouched()
        self.assertFalse(os.path.exists(os.path.join(self.dir, au.STAGING)))   # nothing left behind
        self.assertEqual(au.read_state(self.dir)["phase"], "failed")
        self.assertEqual(srv.statuses[-1][0], "failed")                        # and the dashboard is told
        return out

    def test_a_modified_manifest_is_rejected(self):
        rel = self.signer.build()
        m = json.loads(rel.manifest)
        m["sequence"] = 99
        srv = FakeServer(rel)
        srv.manifest_override = (base64.b64encode(json.dumps(m, indent=2, sort_keys=True).encode()).decode(), rel.signature)
        out = self.run_bad(srv, rel)
        self.assertIn("not trusted", out["error"])

    def test_a_manifest_signed_by_an_unknown_key_is_rejected(self):
        attacker = Signer("k1")                                                # same key id, different key pair
        rel = attacker.build()
        out = self.run_bad(FakeServer(rel), rel)
        self.assertIn("signature", out["error"])

    def test_an_agent_with_no_trusted_keys_installs_nothing(self):
        rel = self.signer.build()
        srv = FakeServer(rel)
        out = self.client(srv, keys={"keys": []}).run(self.offer(rel))
        self.assertFalse(out["ok"])
        self.assertUntouched()

    def test_a_modified_package_is_discarded(self):
        rel = self.signer.build()
        bad = bytearray(rel.package)
        bad[len(bad) // 2] ^= 0xFF
        srv = FakeServer(rel)
        srv.package_override = bytes(bad)
        out = self.run_bad(srv, rel)
        self.assertIn("hash mismatch", out["error"])

    def test_a_package_that_is_a_different_size_than_signed_is_refused(self):
        rel = self.signer.build()
        srv = FakeServer(rel)
        srv.package_override = rel.package + b"EXTRA"
        out = self.run_bad(srv, rel)
        self.assertIn("larger than the signed size", out["error"])

    def test_a_signed_release_whose_code_does_not_compile_is_refused_before_installing(self):
        rel = self.signer.build(files=self.signer.files(**{"reporter.py": b"def broken(:\n"}))
        out = self.run_bad(FakeServer(rel), rel)
        self.assertIn("does not compile", out["error"])

    def test_an_offer_that_disagrees_with_the_signed_manifest_is_refused(self):
        rel = self.signer.build()
        srv = FakeServer(rel)
        offer = self.offer(rel)
        offer["version"] = "9.9.9"
        self.assertFalse(self.client(srv).run(offer)["ok"])
        self.assertUntouched()


class DowngradeAndCompatibility(Base):
    def test_an_older_release_is_refused_even_if_correctly_signed(self):
        rel = self.signer.build("1.9.0", sequence=3)
        srv = FakeServer(rel)
        out = self.client(srv, sequence=5).run(self.offer(rel))
        self.assertFalse(out["ok"])
        self.assertIn("downgrades are not allowed", out["error"])
        self.assertUntouched()

    def test_the_same_release_is_not_reinstalled(self):
        rel = self.signer.build("2.0.0", sequence=2)
        out = self.client(FakeServer(rel), sequence=2).run(self.offer(rel))
        self.assertTrue(out["ok"])
        self.assertIn("skipped", out)
        self.assertEqual(self.launched, 0)

    def test_a_release_that_needs_a_newer_base_is_refused(self):
        rel = self.signer.build("3.0.0", sequence=9, min_agent_sequence=5)
        out = self.client(FakeServer(rel), sequence=2).run(self.offer(rel))
        self.assertFalse(out["ok"])
        self.assertIn("too old to update directly", out["error"])

    def test_not_enough_disk_space_fails_before_any_download(self):
        rel = self.signer.build()
        srv = FakeServer(rel)
        self.free = 1024
        out = self.client(srv).run(self.offer(rel))
        self.assertFalse(out["ok"])
        self.assertIn("free disk space", out["error"])
        self.assertEqual(srv.package_requests, [])
        self.assertUntouched()


class Downloads(Base):
    def test_an_interrupted_download_resumes_with_a_range_request(self):
        rel = self.signer.build(files=self.signer.files(**{"big.txt": os.urandom(150 * 1024)}))
        srv = FakeServer(rel)
        srv.break_first_download_after = 100 * 1024
        out = self.client(srv).run(self.offer(rel))
        self.assertTrue(out["ok"], out)
        self.assertEqual(len(srv.package_requests), 2)
        self.assertIsNone(srv.package_requests[0])
        self.assertTrue(srv.package_requests[1].startswith("bytes=") and not srv.package_requests[1].endswith("=0-"))
        self.assertEqual(self.launched, 1)

    def test_a_download_that_cannot_finish_this_time_is_kept_and_finished_at_the_next_heartbeat(self):
        rel = self.signer.build(files=self.signer.files(**{"big.txt": os.urandom(150 * 1024)}))
        srv = FakeServer(rel)
        srv.break_first_download_after = 100 * 1024
        first = self.client(srv, max_download_attempts=1).run(self.offer(rel))
        self.assertEqual((first["ok"], first.get("retry")), (False, True))
        self.assertNotEqual(au.read_state(self.dir)["phase"], "failed")        # not a failure: just try again
        self.assertUntouched()
        second = self.client(srv, max_download_attempts=1).run(self.offer(rel))
        self.assertTrue(second["ok"], second)
        self.assertEqual(self.launched, 1)

    def test_a_server_that_ignores_range_restarts_cleanly_and_still_verifies(self):
        rel = self.signer.build(files=self.signer.files(**{"big.txt": os.urandom(150 * 1024)}))
        srv = FakeServer(rel)
        srv.break_first_download_after = 100 * 1024
        srv.ignore_range = True
        self.assertTrue(self.client(srv).run(self.offer(rel))["ok"])

    def test_the_server_being_unreachable_is_a_retry_not_a_failure(self):
        rel = self.signer.build()
        srv = FakeServer(rel)
        srv.network_down = True
        out = self.client(srv).run(self.offer(rel))
        self.assertEqual((out["ok"], out.get("retry")), (False, True))
        self.assertUntouched()

    def test_persistent_network_failure_eventually_fails_with_a_clear_reason(self):
        rel = self.signer.build()
        srv = FakeServer(rel)
        srv.network_down = True
        client = self.client(srv)
        last = None
        for _ in range(uc.MAX_NETWORK_FAILURES):
            last = client.run(self.offer(rel))
        self.assertFalse(last["ok"])
        self.assertEqual(au.read_state(self.dir)["phase"], "failed")


class ServerControl(Base):
    def test_a_cancelled_update_stops_before_installing(self):
        rel = self.signer.build()
        srv = FakeServer(rel)
        srv.stop_after_status = "downloading"                                   # the admin cancels while we download
        out = self.client(srv).run(self.offer(rel))
        self.assertTrue(out["ok"])
        self.assertIn("stopped", out["skipped"])
        self.assertEqual((self.launched, self.exited), (0, 0))
        self.assertUntouched()
        self.assertFalse(os.path.exists(os.path.join(self.dir, au.STAGING)))

    def test_a_paused_or_revoked_release_is_not_installed(self):
        rel = self.signer.build()
        srv = FakeServer(rel)
        srv.manifest_status = 409
        out = self.client(srv).run(self.offer(rel))
        self.assertTrue(out["ok"])
        self.assertEqual(self.launched, 0)
        self.assertUntouched()


class DuplicateCommands(Base):
    def test_repeating_the_same_offer_after_hand_over_does_nothing(self):
        rel = self.signer.build()
        srv = FakeServer(rel)
        c = self.client(srv)
        c.run(self.offer(rel))
        again = c.run(self.offer(rel))
        self.assertTrue(again["ok"])
        self.assertIn("already handled", again["skipped"])
        self.assertEqual((self.launched, srv.manifest_calls), (1, 1))

    def test_an_offer_for_an_attempt_that_just_failed_is_not_hammered(self):
        rel = self.signer.build()
        srv = FakeServer(rel)
        srv.package_override = rel.package[:-1] + b"X"
        c = self.client(srv)
        c.run(self.offer(rel))
        calls = srv.manifest_calls
        again = c.run(self.offer(rel))
        self.assertFalse(again["ok"])
        self.assertIn("already failed", again["skipped"])
        self.assertEqual(srv.manifest_calls, calls)

    def test_a_retry_from_the_admin_is_a_new_attempt_and_is_tried_again(self):
        rel = self.signer.build()
        srv = FakeServer(rel)
        srv.package_override = rel.package[:-1] + b"X"
        c = self.client(srv)
        c.run(self.offer(rel, attempt=1))
        srv.package_override = None                                             # the fault is fixed
        out = c.run(self.offer(rel, attempt=2))
        self.assertTrue(out["ok"])
        self.assertEqual(self.launched, 1)

    def test_a_malformed_offer_is_handled_quietly(self):
        rel = self.signer.build()
        for bad in ({}, {"deployment_id": "x"}, {"deployment_id": None}):
            self.assertFalse(self.client(FakeServer(rel)).run(bad)["ok"])
        self.assertUntouched()

    def test_an_installation_without_an_updater_refuses_cleanly(self):
        os.remove(os.path.join(self.dir, "apply_update.py"))
        rel = self.signer.build()
        out = self.client(FakeServer(rel)).run(self.offer(rel))
        self.assertFalse(out["ok"])
        self.assertIn("no updater", out["error"])
        self.assertEqual(self.launched, 0)


class HeartbeatHooks(Base):
    def test_payload_extras_advertise_capability_sequence_and_a_pending_outcome(self):
        au.write_state(self.dir, phase="healthy", deployment_id=7, attempt=1, to_version="2.1.0")
        extras = uc.payload_extras(self.dir)
        self.assertTrue(extras["capabilities"]["remote_update"])
        self.assertIn("agent_sequence", extras)
        self.assertEqual(extras["update_result"]["outcome"], "healthy")

    def test_no_outcome_is_sent_when_nothing_happened(self):
        self.assertNotIn("update_result", uc.payload_extras(self.dir))

    def test_a_delivered_outcome_is_acknowledged_so_it_is_not_sent_forever(self):
        au.write_state(self.dir, phase="healthy", deployment_id=7, attempt=1, to_version="2.1.0")
        uc.handle_heartbeat_response({"server_url": "x", "csp_id": "X1", "api_key": "k"}, {"ok": True}, True, self.dir)
        self.assertEqual(au.read_state(self.dir)["phase"], "reported")
        self.assertIsNone(au.pending_result(self.dir))

    def test_a_heartbeat_without_an_offer_starts_nothing(self):
        self.assertFalse(uc.handle_heartbeat_response({"server_url": "x", "csp_id": "X1", "api_key": "k"}, {"ok": True}, False, self.dir))
        self.assertFalse(uc.handle_heartbeat_response({}, "not a dict", False, self.dir))


class PostStartVerification(Base):
    def stage(self, **kw):
        au.write_state(self.dir, **dict({"phase": "restarting", "to_version": "9.9.9", "to_sequence": 99}, **kw))

    def test_the_new_version_proves_itself_then_marks_healthy(self):
        import version
        self.stage(to_version=version.AGENT_VERSION, to_sequence=version.AGENT_SEQUENCE)
        ran = []
        self.assertTrue(uc.post_start_verification(self.dir, run_check=lambda: ran.append(1)))
        self.assertEqual((ran, au.read_state(self.dir)["phase"]), ([1], "healthy"))

    def test_a_version_mismatch_is_not_marked_healthy_so_the_updater_rolls_back(self):
        self.stage()                                                           # expects 9.9.9, this is something else
        self.assertFalse(uc.post_start_verification(self.dir, run_check=lambda: None))
        self.assertEqual(au.read_state(self.dir)["phase"], "restarting")

    def test_a_failing_health_check_is_not_marked_healthy(self):
        import version
        self.stage(to_version=version.AGENT_VERSION, to_sequence=version.AGENT_SEQUENCE)

        def boom():
            raise RuntimeError("scan crashed")
        self.assertFalse(uc.post_start_verification(self.dir, run_check=boom))
        st = au.read_state(self.dir)
        self.assertEqual(st["phase"], "restarting")
        self.assertIn("scan crashed", st["health_error"])

    def test_nothing_happens_when_no_update_is_in_progress(self):
        self.assertIsNone(uc.post_start_verification(self.dir, run_check=lambda: None))


if __name__ == "__main__":
    unittest.main()
