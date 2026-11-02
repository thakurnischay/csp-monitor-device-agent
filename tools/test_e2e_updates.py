"""END-TO-END: the real server (real Flask app + SQLite, over real HTTP on 127.0.0.1) against the real agent code
(enroll.py, update_client.py, apply_update.py, release_manifest.py). Nothing here is mocked except two things that
cannot run in a test: the process that restarts the agent, and Windows hardware scans.

Covers: registration check -> enrollment -> admin approval -> heartbeat -> signed release -> pilot rollout -> agent
downloads/verifies/stages -> applier swaps files -> new version proves itself -> server shows Successful; a bad
update rolling back; a forged release being refused; and an old agent (no capabilities) staying completely unaffected.

    python tools/test_e2e_updates.py
"""
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for p in ("tools", "agent", "server", os.path.join("server", "tests")):
    sys.path.insert(0, os.path.join(ROOT, p))

import requests                                  # noqa: E402
from werkzeug.serving import make_server         # noqa: E402

from helpers import make_test_app, issue_test_key           # noqa: E402  (server/tests)
from release_fixtures import Signer as ServerSigner         # noqa: E402

import apply_update as au                         # noqa: E402
import config_store                               # noqa: E402
import enroll                                     # noqa: E402
import update_client as uc                        # noqa: E402
import version as agent_version_module            # noqa: E402


class LiveServer:
    def __init__(self, app):
        self.srv = make_server("127.0.0.1", 0, app, threaded=True)
        self.url = "http://127.0.0.1:%d" % self.srv.server_port
        self.thread = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.thread.start()

    def stop(self):
        self.srv.shutdown()


def real_agent_files(version, sequence, extra=None):
    """The agent's genuine source files, stamped to a given release."""
    import release_tool as rt
    files = rt.collect_agent_files()
    files.update(extra or {})
    return files


def install_old_agent(test, sequence=1, version="2.0.0"):
    """A genuine copy of the current agent in a temp folder, standing in for a CSP PC (with its own config + history)."""
    d = tempfile.mkdtemp()
    test.addCleanup(lambda: shutil.rmtree(d, ignore_errors=True))
    agent_src = os.path.join(ROOT, "agent")
    for name in os.listdir(agent_src):
        full = os.path.join(agent_src, name)
        if os.path.isfile(full) and name.endswith((".py", ".json")) and name not in ("agent_config.json", "diagnostic_history.json"):
            shutil.copyfile(full, os.path.join(d, name))
    with open(os.path.join(d, "version.py"), "r", encoding="utf-8") as f:
        src = f.read()
    import re
    src = re.sub(r'AGENT_VERSION\s*=\s*"[^"]*"', 'AGENT_VERSION = "%s"' % version, src)
    src = re.sub(r"AGENT_SEQUENCE\s*=\s*\d+", "AGENT_SEQUENCE = %d" % sequence, src)
    with open(os.path.join(d, "version.py"), "w", encoding="utf-8") as f:
        f.write(src)
    with open(os.path.join(d, "diagnostic_history.json"), "w") as f:
        f.write('{"history": "must survive every update"}')
    return d


class E2E(unittest.TestCase):
    def setUp(self):
        self.signer = ServerSigner()
        self.app, self.db_path = make_test_app()
        self.live = LiveServer(self.app)
        self.addCleanup(self.live.stop)
        self.addCleanup(self.signer.cleanup)
        self.cfg_dir = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(self.cfg_dir, ignore_errors=True))
        os.environ["CSP_AGENT_CONFIG_PATH"] = os.path.join(self.cfg_dir, "agent_config.json")
        import db
        self.db = db
        self._saved_version = (agent_version_module.AGENT_VERSION, agent_version_module.AGENT_SEQUENCE)
        self.addCleanup(lambda: (setattr(agent_version_module, "AGENT_VERSION", self._saved_version[0]),
                                 setattr(agent_version_module, "AGENT_SEQUENCE", self._saved_version[1])))

    # ----------------------------------------------------------------------------------------- helpers
    def admin(self, fn, *a, **k):
        with self.db.get_connection() as conn:
            out = fn(conn, *a, **k)
            conn.commit()
            return out

    def sql(self, q, *args):
        with self.db.get_connection() as conn:
            return conn.execute(q, args).fetchall()

    def heartbeat(self, cfg, agent_dir, version, sequence, capable=True):
        body = {"csp_id": cfg["csp_id"], "agent_version": version, "schema_version": 4}
        if capable:
            agent_version_module.AGENT_VERSION, agent_version_module.AGENT_SEQUENCE = version, sequence
            body.update(uc.payload_extras(agent_dir))
        r = requests.post(self.live.url + "/api/report", headers={"X-API-Key": cfg["api_key"]}, json=body, timeout=15)
        reply = r.json() if r.content else {}
        if capable and agent_dir and r.status_code == 200:
            # what reporter.py does after a good heartbeat: acknowledge a delivered result (offers are handled by the test itself)
            uc.handle_heartbeat_response(cfg, {k: v for k, v in reply.items() if k != "update"}, body.get("update_result"), agent_dir)
        return r, reply

    def enroll_and_approve(self, code="E2E-1", name="E2E Sample CSP"):
        import registry
        self.admin(registry.import_registry, "%s,%s" % (code, name), "tester")
        self.assertEqual(enroll.verify(self.live.url, code, name)["verdict"], "ok")
        res = enroll.start(config_store.save, self.live.url, code, name, "2.0.0")
        self.assertEqual(res["status"], "pending")
        pending = self.sql("SELECT id FROM enrollments WHERE csp_id=? AND status='PENDING'", code)
        self.assertEqual(len(pending), 1)
        self.admin(registry.approve, pending[0]["id"], "tester")
        self.assertTrue(enroll.maintain())
        cfg = config_store.load()
        self.assertEqual(cfg["enroll_status"], "approved")
        return cfg

    def publish(self, files, version, sequence, **kw):
        import release_tool as rt
        import updates
        out = tempfile.mkdtemp()
        zp, mp, sp = rt.build_release(files, version, sequence, self.signer.key_id, self.signer.secret, kw.pop("notes", "e2e"),
                                      kw.pop("kind", "release"), kw.pop("min_agent_sequence", 0), out)
        rid = self.admin(updates.add_release, open(zp, "rb").read(), open(mp, "rb").read(), open(sp).read(), "tester")
        self.admin(updates.approve_release, rid, "tester")
        return rid

    def make_client(self, cfg, agent_dir, version, sequence, healthy, keys=None):
        """An UpdateClient whose 'launch' runs the REAL applier in-process, and whose 'restart' simulates the new
        process starting (healthy or crashing). Everything else - HTTP, verification, staging, swap, rollback - is real."""
        def start(adir):
            if healthy:
                import re
                with open(os.path.join(adir, "version.py"), encoding="utf-8") as f:
                    src = f.read()
                agent_version_module.AGENT_VERSION = re.search(r'AGENT_VERSION\s*=\s*"([^"]*)"', src).group(1)
                agent_version_module.AGENT_SEQUENCE = int(re.search(r"AGENT_SEQUENCE\s*=\s*(\d+)", src).group(1))
                uc.post_start_verification(adir, run_check=lambda: None)
            return 4242

        def launch():
            self.apply_result = au.apply(agent_dir, start=start, kill=lambda pid: None,
                                         running=lambda pid: bool(healthy) if pid == 4242 else False,
                                         sleep=lambda s: None, health_timeout=5, exit_timeout=1, poll=0)

        self.exited = False

        def exit_process():                      # the real one is os._exit(0) and never returns
            self.exited = True
        return uc.UpdateClient(agent_dir, self.live.url, cfg["csp_id"], cfg["api_key"], keys or uc.load_public_keys(agent_dir),
                               version, sequence, launch=launch, exit_process=exit_process)

    def offer_for(self, cfg, agent_dir, version, sequence):
        r, body = self.heartbeat(cfg, agent_dir, version, sequence)
        self.assertEqual(r.status_code, 200, body)
        return body.get("update")

    def deployment(self, csp_id):
        return self.sql("SELECT * FROM update_deployments WHERE csp_id=? ORDER BY id DESC", csp_id)[0]

    def use_signer_keys_in_agent(self, agent_dir):
        with open(os.path.join(agent_dir, "release_public_keys.json"), "w") as f:
            json.dump(self.signer.keys, f)

    # ------------------------------------------------------------------------------------------- tests
    def test_registration_rules_over_real_http(self):
        import registry
        self.admin(registry.import_registry, "E2E-1,E2E Sample CSP", "tester")
        self.assertEqual(enroll.verify(self.live.url, "E2E-1", "Wrong Name")["verdict"], "name_mismatch")
        unregistered = enroll.verify(self.live.url, "NOPE-9", "E2E Sample CSP")      # allowed through; the admin approves
        self.assertEqual((unregistered["ok"], unregistered["verdict"]), (True, "not_registered"))
        self.assertEqual(enroll.verify(self.live.url, "E2E-1", "  e2e   SAMPLE csp ")["verdict"], "ok")
        # the code and name alone are never enough: an enrolled-but-unapproved key cannot send heartbeats
        res = enroll.start(config_store.save, self.live.url, "E2E-1", "E2E Sample CSP", "2.0.0")
        self.assertEqual(res["status"], "pending")
        cfg = config_store.load()
        r = requests.post(self.live.url + "/api/report", headers={"X-API-Key": cfg["api_key"]},
                          json={"csp_id": "E2E-1", "agent_version": "2.0.0"}, timeout=15)
        self.assertEqual(r.status_code, 401)
        # the server stored only a hash of the key
        stored = json.dumps([tuple(x) for x in self.sql("SELECT * FROM enrollments")], default=str)
        self.assertNotIn(cfg["api_key"], stored)
        # enrolling the same PC again does not create a second record
        enroll.request_enrollment(self.live.url, cfg["api_key"], "E2E-1", "E2E Sample CSP", "2.0.0")
        self.assertEqual(len(self.sql("SELECT id FROM enrollments WHERE csp_id='E2E-1' AND status='PENDING'")), 1)

    def test_a_different_pc_cannot_silently_replace_an_enrolled_csp(self):
        cfg = self.enroll_and_approve()
        r, _ = self.heartbeat(cfg, None, "2.0.0", 1, capable=False)
        self.assertEqual(r.status_code, 200)
        other_key = enroll.new_key()
        res = enroll.request_enrollment(self.live.url, other_key, "E2E-1", "E2E Sample CSP", "2.0.0")
        self.assertEqual(res["status"], "pending")                      # held for an administrator - not applied
        r = requests.post(self.live.url + "/api/report", headers={"X-API-Key": other_key},
                          json={"csp_id": "E2E-1", "agent_version": "2.0.0"}, timeout=15)
        self.assertEqual(r.status_code, 401)
        r, _ = self.heartbeat(cfg, None, "2.0.0", 1, capable=False)     # the original keeps working
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(self.sql("SELECT csp_id FROM csps WHERE csp_id='E2E-1'")), 1)

    def test_full_successful_update_end_to_end(self):
        cfg = self.enroll_and_approve()
        agent_dir = install_old_agent(self, sequence=1, version="2.0.0")
        self.use_signer_keys_in_agent(agent_dir)
        r, body = self.heartbeat(cfg, agent_dir, "2.0.0", 1)
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("update", body)                                 # nothing assigned yet
        files = real_agent_files("2.1.0", 2, {"installer/NEW_FILE.txt": b"added by the update\n"})
        rid = self.publish(files, "2.1.0", 2)
        import updates
        _, created, _ = self.admin(updates.create_rollout, rid, ["E2E-1"], "pilot", "tester")
        self.assertEqual(created, 1)
        self.assertEqual(self.deployment("E2E-1")["status"], "pending")
        offer = self.offer_for(cfg, agent_dir, "2.0.0", 1)
        self.assertEqual((offer["version"], offer["sequence"]), ("2.1.0", 2))
        self.assertEqual(self.deployment("E2E-1")["status"], "assigned")  # assigned is NOT success
        client = self.make_client(cfg, agent_dir, "2.0.0", 1, healthy=True)
        out = client.run(offer)
        self.assertTrue(out.get("handed_over"), out)
        self.assertTrue(self.exited)
        self.assertEqual(self.apply_result, "healthy")
        self.assertNotEqual(self.deployment("E2E-1")["status"], "successful")   # installed locally, not yet verified by the server
        # files swapped, this PC's own data untouched
        with open(os.path.join(agent_dir, "version.py")) as f:
            self.assertIn('"2.1.0"', f.read())
        self.assertTrue(os.path.isfile(os.path.join(agent_dir, "installer", "NEW_FILE.txt")))
        with open(os.path.join(agent_dir, "diagnostic_history.json")) as f:
            self.assertIn("must survive", f.read())
        # the NEW version's healthy heartbeat is what makes it Successful
        r, body = self.heartbeat(cfg, agent_dir, "2.1.0", 2)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.deployment("E2E-1")["status"], "successful")
        self.assertEqual(au.read_state(agent_dir)["phase"], "reported")
        self.assertEqual(self.sql("SELECT agent_version FROM csps WHERE csp_id='E2E-1'")[0]["agent_version"], "2.1.0")
        r, body = self.heartbeat(cfg, agent_dir, "2.1.0", 2)
        self.assertNotIn("update", body)                                 # nothing offered again
        audits = [x["action"] for x in self.sql("SELECT action FROM admin_audit_log")]
        self.assertIn("release_uploaded", audits)
        self.assertIn("rollout_created", audits)

    def test_a_broken_update_rolls_back_and_the_dashboard_says_so(self):
        cfg = self.enroll_and_approve()
        agent_dir = install_old_agent(self, sequence=1, version="2.0.0")
        self.use_signer_keys_in_agent(agent_dir)
        self.heartbeat(cfg, agent_dir, "2.0.0", 1)
        before = open(os.path.join(agent_dir, "agent.py"), "rb").read()
        rid = self.publish(real_agent_files("2.1.0", 2), "2.1.0", 2)
        import updates
        self.admin(updates.create_rollout, rid, ["E2E-1"], "pilot", "tester")
        offer = self.offer_for(cfg, agent_dir, "2.0.0", 1)
        client = self.make_client(cfg, agent_dir, "2.0.0", 1, healthy=False)       # the new version crashes at start-up
        out = client.run(offer)
        self.assertTrue(out.get("handed_over"), out)
        self.assertEqual(self.apply_result, "rolled_back")
        self.assertEqual(open(os.path.join(agent_dir, "agent.py"), "rb").read(), before)   # previous version restored
        with open(os.path.join(agent_dir, "version.py")) as f:
            self.assertIn('"2.0.0"', f.read())
        r, _ = self.heartbeat(cfg, agent_dir, "2.0.0", 1)                  # the old version reports what happened
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.deployment("E2E-1")["status"], "rolled_back")
        self.assertEqual(self.sql("SELECT agent_version FROM csps WHERE csp_id='E2E-1'")[0]["agent_version"], "2.0.0")

    def test_a_forged_release_is_refused_by_the_server_and_by_the_agent(self):
        import release_tool as rt
        import updates
        cfg = self.enroll_and_approve()
        agent_dir = install_old_agent(self, sequence=1, version="2.0.0")
        self.use_signer_keys_in_agent(agent_dir)
        self.heartbeat(cfg, agent_dir, "2.0.0", 1)
        # 1. signed by an attacker's key: the server will not accept it
        attacker = os.urandom(32)
        out = tempfile.mkdtemp()
        zp, mp, sp = rt.build_release(real_agent_files("9.9.9", 9), "9.9.9", 9, "k1", attacker, "evil", "release", 0, out)
        with self.assertRaises(updates.UpdateError):
            self.admin(updates.add_release, open(zp, "rb").read(), open(mp, "rb").read(), open(sp).read(), "tester")
        # 2. a genuine release whose package is swapped after upload: the agent refuses it and reports failure
        rid = self.publish(real_agent_files("2.1.0", 2), "2.1.0", 2)
        row = self.sql("SELECT package_path FROM agent_releases WHERE id=?", rid)[0]
        with open(row["package_path"], "r+b") as f:
            f.seek(100)
            f.write(b"\x00\x01\x02TAMPER")
        self.admin(updates.create_rollout, rid, ["E2E-1"], "pilot", "tester")
        offer = self.offer_for(cfg, agent_dir, "2.0.0", 1)
        client = self.make_client(cfg, agent_dir, "2.0.0", 1, healthy=True)
        before = open(os.path.join(agent_dir, "agent.py"), "rb").read()
        outcome = client.run(offer)                                         # returns (no restart): nothing was installed
        self.assertFalse(outcome.get("handed_over"), outcome)
        self.assertFalse(self.exited)
        self.assertEqual(open(os.path.join(agent_dir, "agent.py"), "rb").read(), before)
        self.assertEqual(self.deployment("E2E-1")["status"], "failed")

    def test_an_agent_that_trusts_a_different_key_will_not_install(self):
        cfg = self.enroll_and_approve()
        agent_dir = install_old_agent(self, sequence=1, version="2.0.0")      # its release_public_keys.json is the empty placeholder
        self.heartbeat(cfg, agent_dir, "2.0.0", 1)
        rid = self.publish(real_agent_files("2.1.0", 2), "2.1.0", 2)
        import updates
        self.admin(updates.create_rollout, rid, ["E2E-1"], "pilot", "tester")
        offer = self.offer_for(cfg, agent_dir, "2.0.0", 1)
        before = open(os.path.join(agent_dir, "agent.py"), "rb").read()
        out = self.make_client(cfg, agent_dir, "2.0.0", 1, healthy=True).run(offer)
        self.assertFalse(out.get("handed_over"), out)
        self.assertEqual(open(os.path.join(agent_dir, "agent.py"), "rb").read(), before)
        self.assertEqual(self.deployment("E2E-1")["status"], "failed")

    def test_duplicate_rollout_commands_do_not_duplicate_work(self):
        import updates
        cfg = self.enroll_and_approve()
        agent_dir = install_old_agent(self, sequence=1, version="2.0.0")
        self.heartbeat(cfg, agent_dir, "2.0.0", 1)
        rid = self.publish(real_agent_files("2.1.0", 2), "2.1.0", 2)
        self.admin(updates.create_rollout, rid, ["E2E-1"], "pilot", "tester")
        _, created, skipped = self.admin(updates.create_rollout, rid, ["E2E-1"], "pilot", "tester")
        self.assertEqual(created, 0)
        self.assertEqual(len(self.sql("SELECT id FROM update_deployments WHERE csp_id='E2E-1'")), 1)

    def test_legacy_agent_with_only_an_api_key_is_unaffected_and_marked_needing_bootstrap(self):
        import updates
        key = issue_test_key("OLD-77", "Old Legacy CSP")
        cfg = {"csp_id": "OLD-77", "api_key": key}
        r, body = self.heartbeat(cfg, None, "1.4.0", 0, capable=False)       # exactly what a pre-update agent sends
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("update", body)
        row = self.sql("SELECT update_capable, agent_version FROM csps WHERE csp_id='OLD-77'")[0]
        self.assertFalse(row["update_capable"])
        rid = self.publish(real_agent_files("2.1.0", 2), "2.1.0", 2)
        with self.db.get_connection() as conn:
            self.assertNotIn("OLD-77", updates.compatible_csp_ids(conn, rid))   # never targeted - it cannot self-update
        r, body = self.heartbeat(cfg, None, "1.4.0", 0, capable=False)
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("update", body)


if __name__ == "__main__":
    unittest.main(verbosity=2)
