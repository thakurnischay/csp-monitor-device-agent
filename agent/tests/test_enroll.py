"""Agent-side enrollment: the agent makes its own key, nothing is typed or shown, and an established agent never enrolls."""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config_store
import enroll
import reporter


class Resp:
    def __init__(self, status=200, body=None):
        self.status_code, self._b = status, body or {}

    def json(self):
        return self._b

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


class FakeEnrollServer:
    def __init__(self):
        self.calls = []
        self.verify_reply = Resp(200, {"ok": True, "verdict": "ok", "message": "Verified."})
        self.enroll_reply = Resp(200, {"ok": True, "status": "pending", "request_id": 1})
        self.status_reply = Resp(200, {"ok": True, "status": "pending"})
        self.down = False

    def post(self, url, headers=None, json=None, timeout=None):
        import requests
        if self.down:
            raise requests.ConnectionError("down")
        self.calls.append((url.rsplit("/api", 1)[-1], dict(headers or {}), json))
        if url.endswith("/enroll/verify"):
            return self.verify_reply
        if url.endswith("/enroll/status"):
            return self.status_reply
        return self.enroll_reply


class Base(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp()
        os.environ["CSP_AGENT_CONFIG_PATH"] = os.path.join(d, "agent_config.json")
        self.srv = FakeEnrollServer()


class Verify(Base):
    def test_a_good_code_and_name(self):
        self.assertEqual(enroll.verify("https://s", "CSP-1", "Name Here", self.srv)["verdict"], "ok")

    def test_server_rejections_come_back_as_the_servers_plain_message(self):
        self.srv.verify_reply = Resp(400, {"ok": False, "verdict": "name_mismatch", "message": "The CSP name does not match."})
        r = enroll.verify("https://s", "CSP-1", "Wrong", self.srv)
        self.assertEqual((r["ok"], r["verdict"], r["message"]), (False, "name_mismatch", "The CSP name does not match."))

    def test_no_internet_gives_a_friendly_message_not_a_traceback(self):
        self.srv.down = True
        r = enroll.verify("https://s", "CSP-1", "Name", self.srv)
        self.assertEqual((r["ok"], r["verdict"]), (False, "network"))
        self.assertIn("internet connection", r["message"])

    def test_rate_limiting_is_explained(self):
        self.srv.verify_reply = Resp(429, {"ok": False, "status": "busy", "message": "Too many attempts. Please wait a few minutes and try again."})
        self.assertEqual(enroll.verify("https://s", "CSP-1", "Name", self.srv)["verdict"], "busy")

    def test_garbage_from_the_server_is_handled(self):
        class Bad:
            status_code = 200

            def json(self):
                raise ValueError("not json")
        self.srv.verify_reply = Bad()
        self.assertFalse(enroll.verify("https://s", "CSP-1", "Name", self.srv)["ok"])


class Start(Base):
    def test_the_agent_makes_its_own_key_saves_it_first_and_sends_it_only_in_the_header(self):
        res = enroll.start(config_store.save, "https://s", "CSP-1", "Name Here", "2.1.0", self.srv)
        cfg = config_store.load()
        self.assertEqual(res["status"], "pending")
        self.assertEqual((cfg["csp_id"], cfg["csp_name"], cfg["enroll_status"]), ("CSP-1", "Name Here", "pending"))
        self.assertGreaterEqual(len(cfg["api_key"]), 40)
        path, headers, body = self.srv.calls[0]
        self.assertEqual(headers["X-API-Key"], cfg["api_key"])
        self.assertNotIn(cfg["api_key"], str(body))                              # never in the body or anywhere else
        self.assertEqual(len(enroll.machine_id()), 32)

    def test_two_agents_never_get_the_same_key(self):
        keys = {enroll.new_key() for _ in range(50)}
        self.assertEqual(len(keys), 50)

    def test_a_lost_reply_does_not_strand_the_pc_the_key_is_kept_and_retried(self):
        self.srv.down = True
        enroll.start(config_store.save, "https://s", "CSP-1", "Name", "2.1.0", self.srv)
        cfg = config_store.load()
        self.assertEqual(cfg["enroll_status"], "error")
        self.assertTrue(cfg["api_key"])
        self.srv.down = False
        self.srv.enroll_reply = Resp(200, {"ok": True, "status": "pending"})
        self.assertFalse(enroll.maintain(None, self.srv))                        # retried; now pending
        self.assertEqual(config_store.load()["enroll_status"], "pending")
        self.assertEqual(config_store.load()["api_key"], cfg["api_key"])         # the SAME key

    def test_pre_approval_makes_it_approved_immediately(self):
        self.srv.enroll_reply = Resp(200, {"ok": True, "status": "approved", "request_id": 1})
        enroll.start(config_store.save, "https://s", "CSP-1", "Name", "", self.srv)
        self.assertEqual(config_store.load()["enroll_status"], "approved")

    def test_a_refused_registration_is_recorded_with_the_reason(self):
        self.srv.enroll_reply = Resp(400, {"ok": False, "status": "name_mismatch", "message": "The CSP name does not match this CSP code."})
        enroll.start(config_store.save, "https://s", "CSP-1", "Name", "", self.srv)
        cfg = config_store.load()
        self.assertEqual(cfg["enroll_status"], "rejected")
        self.assertIn("does not match", cfg["enroll_message"])


class Maintain(Base):
    def pending(self):
        config_store.save(csp_id="CSP-1", csp_name="N", api_key="k" * 43, server_url="https://s", enroll_status="pending")

    def test_an_established_agent_goes_straight_to_heartbeats_without_any_network_call(self):
        config_store.save(csp_id="OLD-1", api_key="k" * 43, server_url="https://s")                # enroll_status "" (legacy)
        self.assertTrue(enroll.maintain(None, self.srv))
        self.assertEqual(self.srv.calls, [])

    def test_pending_waits_and_polls_with_its_own_key(self):
        self.pending()
        self.assertFalse(enroll.maintain(None, self.srv))
        self.assertEqual(self.srv.calls[0][0], "/enroll/status")
        self.assertEqual(self.srv.calls[0][1]["X-API-Key"], "k" * 43)

    def test_approval_flips_it_to_approved_and_lets_heartbeats_start(self):
        self.pending()
        self.srv.status_reply = Resp(200, {"ok": True, "status": "approved"})
        self.assertTrue(enroll.maintain(None, self.srv))
        self.assertEqual(config_store.load()["enroll_status"], "approved")

    def test_rejection_and_replacement_stop_everything(self):
        for server_says, final in (("rejected", "rejected"), ("superseded", "superseded"), ("unknown", "rejected")):
            self.pending()
            self.srv.status_reply = Resp(200, {"ok": True, "status": server_says})
            self.assertFalse(enroll.maintain(None, self.srv))
            self.assertEqual(config_store.load()["enroll_status"], final)
            self.assertFalse(enroll.maintain(None, self.srv))                    # and it stays stopped

    def test_network_trouble_while_waiting_just_waits(self):
        self.pending()
        self.srv.down = True
        self.assertFalse(enroll.maintain(None, self.srv))
        self.assertEqual(config_store.load()["enroll_status"], "pending")


class HeartbeatLoop(Base):
    def test_a_pending_pc_sends_no_heartbeat_and_an_approved_one_does(self):
        config_store.save(csp_id="CSP-1", csp_name="N", api_key="k" * 43, server_url="https://s", enroll_status="pending")
        with mock.patch.object(reporter.requests, "post") as post, \
                mock.patch.object(enroll, "poll_status", return_value={"status": "pending"}):
            out = reporter.report_once()
        self.assertFalse(out["ok"])
        post.assert_not_called()
        self.assertIn("approve", out["error"])
        # once approved, the normal heartbeat path runs
        with mock.patch.object(enroll, "poll_status", return_value={"status": "approved"}), \
                mock.patch.object(reporter, "build_payload", return_value={"x": 1}), \
                mock.patch.object(reporter.requests, "post", return_value=Resp(200, {"ok": True})) as post:
            out = reporter.report_once()
        self.assertTrue(out["ok"])
        post.assert_called_once()

    def test_a_rejected_pc_never_sends(self):
        config_store.save(csp_id="CSP-1", api_key="k" * 43, server_url="https://s", enroll_status="rejected",
                          enroll_message="The administrator did not approve this PC.")
        with mock.patch.object(reporter.requests, "post") as post:
            out = reporter.report_once()
        post.assert_not_called()
        self.assertIn("did not approve", out["error"])

    def test_an_established_legacy_agent_is_completely_unaffected(self):
        config_store.save(csp_id="OLD-1", api_key="k" * 43, server_url="https://s")
        with mock.patch.object(reporter, "build_payload", return_value={"x": 1}), \
                mock.patch.object(reporter.requests, "post", return_value=Resp(200, {"ok": True})) as post, \
                mock.patch.object(enroll, "poll_status") as poll:
            self.assertTrue(reporter.report_once()["ok"])
        post.assert_called_once()
        poll.assert_not_called()


if __name__ == "__main__":
    unittest.main()
