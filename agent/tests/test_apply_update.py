"""The updater: swap files, wait for health, roll back on any failure. Includes REAL subprocess runs on this machine
(a real detached agent process that becomes healthy; a real new version that crashes and is rolled back)."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.update_fixtures import FakeServer, Signer, install_agent, snapshot, ROOT  # noqa: E402

import apply_update as au          # noqa: E402
import update_client as uc         # noqa: E402


class Clock:
    def __init__(self):
        self.t = 0.0
        self.on_sleep = None
        self.sleeps = 0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s
        self.sleeps += 1
        if self.on_sleep:
            self.on_sleep(self)


def dead_pid():
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


class Base(unittest.TestCase):
    def setUp(self):
        self.signer = Signer()
        self.dir = install_agent(self)
        self.before = snapshot(self.dir)
        self.rel = self.signer.build()
        self.stage_release(self.rel)
        self.starts, self.kills = [], []
        self.clock = Clock()

    def stage_release(self, rel, files=None, **state):
        """Do exactly what update_client does up to the hand-over, with the real client code."""
        srv = FakeServer(rel)
        client = uc.UpdateClient(self.dir, "https://s", "X1", "k", self.signer.keys, "2.0.0", 1, session=srv,
                                 free_bytes=lambda p: 10 ** 10, launch=lambda: None, exit_process=lambda: None)
        out = client.run({"deployment_id": 7, "attempt": 1, "version": rel.info["version"], "sequence": rel.info["sequence"]})
        assert out["ok"] and out.get("handed_over"), out
        extra = dict(state, agent_pid=dead_pid())
        if files is not None:
            extra["files"] = files
        au.write_state(self.dir, **extra)

    def start(self, pid=4242):
        def fn(agent_dir):
            self.starts.append(agent_dir)
            return pid
        return fn

    def run_apply(self, running=None, **kw):
        running = running or (lambda pid: pid == 4242 and self.alive)
        return au.apply(self.dir, start=self.start(), kill=lambda pid: self.kills.append(pid), running=running,
                        sleep=self.clock.sleep, clock=self.clock, health_timeout=60, exit_timeout=10, poll=2, **kw)

    alive = True


class SuccessfulUpdate(Base):
    def test_files_are_swapped_data_is_kept_and_a_healthy_new_agent_completes_it(self):
        self.clock.on_sleep = lambda c: au.write_state(self.dir, phase="healthy")        # the new agent reports healthy
        self.assertEqual(self.run_apply(), "healthy")
        snap = snapshot(self.dir)
        for f in self.rel.info["files"]:
            self.assertIn(f["path"], snap)
        self.assertEqual(snap["agent.py"], b"# NEW agent\n")
        self.assertEqual(snap["installer/wizard.py"], b"# brand new file\n")
        # this PC's own data and unrelated files are exactly as they were
        for keep in ("agent_config.json", "diagnostic_history.json", "notes_from_the_csp.txt"):
            self.assertEqual(snap[keep], self.before[keep], keep)
        self.assertEqual(len(self.starts), 1)
        self.assertFalse(os.path.exists(os.path.join(self.dir, au.STAGING)))              # staging cleaned
        self.assertTrue(os.path.exists(os.path.join(self.dir, au.BACKUP)))                # backup kept for safety
        self.assertEqual(au.pending_result(self.dir)["outcome"], "healthy")

    def test_it_waits_for_the_old_agent_to_exit_before_touching_anything(self):
        polls = []

        def running(pid):
            if pid == au.read_state(self.dir)["agent_pid"]:
                polls.append(1)
                return len(polls) < 3
            return pid == 4242
        self.clock.on_sleep = lambda c: au.write_state(self.dir, phase="healthy") if len(polls) >= 3 else None
        self.assertEqual(self.run_apply(running=running), "healthy")
        self.assertGreaterEqual(len(polls), 3)

    def test_the_new_file_order_replaces_the_crash_recovery_files_last(self):
        order = au._ordered(["agent.py", "reporter.py", "apply_update.py", "version.py", "a.py"])
        self.assertEqual(order[-2:], ["apply_update.py", "agent.py"])


class RollbackRestoresEverything(Base):
    def assertRolledBack(self, expect_reason=None):
        snap = snapshot(self.dir)
        self.assertEqual(snap, self.before)                                               # byte-identical, new files gone
        st = au.read_state(self.dir)
        self.assertEqual(st["phase"], "rolled_back")
        self.assertTrue(st["restored"])
        if expect_reason:
            self.assertIn(expect_reason, st["error"])
        self.assertEqual(au.pending_result(self.dir)["outcome"], "rolled_back")

    def test_a_new_version_that_never_becomes_healthy_is_rolled_back(self):
        self.assertEqual(self.run_apply(), "rolled_back")
        self.assertRolledBack("did not report healthy")
        self.assertEqual(len(self.starts), 2)                                             # new, then the OLD one again
        self.assertEqual(self.kills, [4242])                                              # the stuck new process was stopped

    def test_a_new_version_that_dies_right_away_is_rolled_back_without_waiting(self):
        self.alive = False
        self.assertEqual(self.run_apply(), "rolled_back")
        self.assertRolledBack("stopped right after starting")
        self.assertLess(self.clock.t, 10)

    def test_a_failure_while_copying_files_restores_the_half_replaced_install(self):
        real, calls = au._atomic_copy, []

        def flaky(src, dst):
            calls.append(dst)
            if len(calls) == 3:
                raise OSError("disk full")
            return real(src, dst)
        au._atomic_copy = flaky
        try:
            self.assertEqual(self.run_apply(), "rolled_back")
        finally:
            au._atomic_copy = real
        self.assertRolledBack("could not install the new files")
        self.assertEqual(len(self.starts), 1)                                             # only the old version was restarted

    def test_a_new_version_that_cannot_be_started_is_rolled_back(self):
        def cannot(agent_dir):
            if not self.starts:
                self.starts.append(agent_dir)
                raise OSError("python not found")
            self.starts.append(agent_dir)
            return 1
        out = au.apply(self.dir, start=cannot, kill=lambda p: None, running=lambda p: False, sleep=self.clock.sleep,
                       clock=self.clock, health_timeout=30, exit_timeout=5, poll=1)
        self.assertEqual(out, "rolled_back")
        self.assertRolledBack("could not be started")

    def test_if_the_old_agent_will_not_stop_nothing_is_changed(self):
        out = au.apply(self.dir, start=self.start(), kill=lambda p: None, running=lambda pid: True, sleep=self.clock.sleep,
                       clock=self.clock, health_timeout=30, exit_timeout=6, poll=2)
        self.assertEqual(out, "aborted")
        self.assertEqual(snapshot(self.dir), self.before)
        self.assertEqual(au.read_state(self.dir)["phase"], "failed")
        self.assertEqual(self.starts, [])


class PathSafety(Base):
    def attempt(self, files):
        au.write_state(self.dir, files=files, phase="staged", agent_pid=dead_pid())     # a fresh staged update each time
        return self.run_apply()

    def test_a_listed_path_that_escapes_the_install_folder_is_refused_and_nothing_outside_is_written(self):
        outside = os.path.join(os.path.dirname(self.dir), "escaped.py")
        out = self.attempt(["../escaped.py", "agent.py"])
        self.assertEqual(out, "aborted")                                                 # refused during the backup plan
        self.assertFalse(os.path.exists(outside))
        self.assertEqual(snapshot(self.dir), self.before)

    def test_this_pcs_own_data_files_are_never_overwritten_even_if_listed(self):
        self.assertEqual(self.attempt(["agent_config.json", "agent.py"]), "aborted")
        self.assertEqual(snapshot(self.dir), self.before)

    def test_absolute_and_drive_paths_are_refused(self):
        for bad in ("/etc/x.py", "C:/Windows/x.py", "a\\b.py", "x.exe"):
            self.assertEqual(self.attempt([bad]), "aborted", bad)
        self.assertEqual(snapshot(self.dir), self.before)

    def test_a_junction_or_symlink_inside_the_folder_cannot_be_used_to_write_outside_it(self):
        outside = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(outside, ignore_errors=True))
        link = os.path.join(self.dir, "installer")
        try:
            if os.name == "nt":
                r = subprocess.run(["cmd", "/c", "mklink", "/J", link, outside], capture_output=True)
                if r.returncode != 0:
                    self.skipTest("could not create a directory junction here")
            else:
                os.symlink(outside, link)
        except OSError:
            self.skipTest("could not create a symlink here")
        with self.assertRaises(ValueError):
            au._safe_target(self.dir, "installer/wizard.py")
        out = self.attempt(["installer/wizard.py", "agent.py"])
        self.assertEqual(out, "aborted")
        self.assertEqual(os.listdir(outside), [])                                   # nothing was written through the link
        if os.name == "nt":
            os.rmdir(link)                                                          # remove the junction, not its target's files

    def test_safe_target_stays_inside_the_folder(self):
        for ok in ("agent.py", "installer/wizard.py"):
            self.assertTrue(au._safe_target(self.dir, ok).startswith(os.path.realpath(self.dir)))
        for bad in ("../x.py", "a/../../x.py", "agent_config.json", ""):
            with self.assertRaises(ValueError):
                au._safe_target(self.dir, bad)


class CrashRecovery(Base):
    def interrupted(self):
        """Simulate a power cut half-way through copying: some files new, state says 'applying'."""
        bdir = au._backup_dir(self.dir, au.read_state(self.dir))
        os.makedirs(os.path.join(bdir, "files"), exist_ok=True)
        for rel in ("agent.py", "reporter.py"):
            shutil.copyfile(os.path.join(self.dir, rel), os.path.join(bdir, "files", rel))
        json.dump({"replaced": ["agent.py", "reporter.py"], "new_files": ["installer/wizard.py"]}, open(os.path.join(bdir, "backup.json"), "w"))
        open(os.path.join(self.dir, "agent.py"), "wb").write(b"# NEW agent\n")             # half-applied mixture
        open(os.path.join(self.dir, "reporter.py"), "wb").write(b"# NEW reporter\n")
        os.makedirs(os.path.join(self.dir, "installer"), exist_ok=True)
        open(os.path.join(self.dir, "installer", "wizard.py"), "wb").write(b"# partial\n")
        au.write_state(self.dir, phase="applying", backup_dir=bdir)

    def test_an_interrupted_update_is_undone_the_next_time_the_agent_starts(self):
        self.interrupted()
        self.assertTrue(au.recover_if_needed(self.dir))
        self.assertEqual(snapshot(self.dir), self.before)
        st = au.read_state(self.dir)
        self.assertEqual(st["phase"], "rolled_back")
        self.assertIn("interrupted", st["error"])

    def test_recovery_is_a_no_op_in_every_other_state(self):
        for phase in ("staged", "restarting", "healthy", "reported", "failed"):
            au.write_state(self.dir, phase=phase)
            self.assertFalse(au.recover_if_needed(self.dir), phase)
        os.remove(au.state_path(self.dir))
        self.assertFalse(au.recover_if_needed(self.dir))

    def test_recovery_with_a_missing_backup_does_not_crash(self):
        au.write_state(self.dir, phase="applying", backup_dir=os.path.join(self.dir, "nope"))
        self.assertTrue(au.recover_if_needed(self.dir))
        self.assertFalse(au.read_state(self.dir)["restored"])

    def test_a_corrupt_state_file_is_treated_as_nothing_happening(self):
        open(au.state_path(self.dir), "w").write("{not json")
        self.assertEqual(au.read_state(self.dir), {})
        self.assertEqual(au.apply(self.dir, start=self.start()), "nothing-to-do")
        self.assertFalse(au.recover_if_needed(self.dir))


class ReportedOutcomes(Base):
    def test_each_outcome_is_reported_once_and_then_acknowledged(self):
        for phase, outcome, acked in (("healthy", "healthy", "reported"), ("rolled_back", "rolled_back", "reported_rollback")):
            au.write_state(self.dir, phase=phase, deployment_id=7, attempt=1, error="because", to_version="2.1.0")
            self.assertEqual(au.pending_result(self.dir)["outcome"], outcome)
            au.acknowledge_result(self.dir)
            self.assertEqual(au.read_state(self.dir)["phase"], acked)
            self.assertIsNone(au.pending_result(self.dir))

    def test_a_failure_is_reported_once(self):
        au.write_state(self.dir, phase="failed", deployment_id=7, attempt=1, error="signature mismatch")
        self.assertEqual(au.pending_result(self.dir)["outcome"], "failed")
        au.acknowledge_result(self.dir)
        self.assertIsNone(au.pending_result(self.dir))


# ------------------------------------------------------------------------- REAL processes on this machine
NEW_AGENT_HEALTHY = b"""import os, sys, time
d = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, d)
import apply_update as au
open(os.path.join(d, "new-started.marker"), "w").write("new version ran")
au.write_state(d, phase="healthy")
time.sleep(1)
"""
NEW_AGENT_CRASHES = b"import sys\nsys.exit(3)\n"
OLD_AGENT = b"""import os
d = os.path.dirname(os.path.abspath(__file__))
open(os.path.join(d, "old-restarted.marker"), "w").write("old version ran again")
"""


class RealProcesses(unittest.TestCase):
    """Exercises real process spawning (detached, no console) and the real file swap. This is a real test on the
    machine it runs on - it does NOT prove anything about other Windows versions, antivirus or service permissions."""

    def build(self, new_agent):
        signer = Signer()
        installed = {"agent.py": OLD_AGENT, "version.py": b'AGENT_VERSION = "2.0.0"\nAGENT_SEQUENCE = 1\n',
                     "agent_config.json": b'{"csp_id": "X1", "api_key": "SECRET"}',
                     "release_manifest.py": open(os.path.join(ROOT, "agent", "release_manifest.py"), "rb").read(),
                     "apply_update.py": open(os.path.join(ROOT, "agent", "apply_update.py"), "rb").read()}
        d = install_agent(self, installed)
        before = snapshot(d)
        files = {"agent.py": new_agent, "version.py": b'AGENT_VERSION = "0.0.0"\n',
                 "release_manifest.py": installed["release_manifest.py"], "apply_update.py": installed["apply_update.py"]}
        rel = signer.build(files=files)
        client = uc.UpdateClient(d, "https://s", "X1", "k", signer.keys, "2.0.0", 1, session=FakeServer(rel),
                                 free_bytes=lambda p: 10 ** 10, launch=lambda: None, exit_process=lambda: None)
        out = client.run({"deployment_id": 7, "attempt": 1, "version": "2.1.0", "sequence": 2})
        self.assertTrue(out.get("handed_over"), out)
        au.write_state(d, agent_pid=dead_pid())
        return d, before

    def run_updater(self, d, timeout=120):
        return subprocess.run([sys.executable, os.path.join(d, "apply_update.py"), d], capture_output=True, text=True, timeout=timeout)

    def wait_for(self, path, seconds=20):
        end = time.time() + seconds
        while time.time() < end:
            if os.path.exists(path):
                return True
            time.sleep(0.3)
        return False

    def test_a_real_new_agent_process_starts_reports_healthy_and_the_update_completes(self):
        d, before = self.build(NEW_AGENT_HEALTHY)
        r = self.run_updater(d)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(self.wait_for(os.path.join(d, "new-started.marker")), "the new agent process never ran")
        st = au.read_state(d)
        self.assertIn(st["phase"], ("healthy", "reported"))
        self.assertEqual(open(os.path.join(d, "agent.py"), "rb").read(), NEW_AGENT_HEALTHY)
        self.assertEqual(open(os.path.join(d, "agent_config.json"), "rb").read(), before["agent_config.json"])

    def test_a_real_new_agent_that_crashes_is_rolled_back_and_the_old_one_runs_again(self):
        d, before = self.build(NEW_AGENT_CRASHES)
        r = self.run_updater(d)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(au.read_state(d)["phase"], "rolled_back")
        self.assertEqual(open(os.path.join(d, "agent.py"), "rb").read(), OLD_AGENT)             # old code is back
        self.assertTrue(self.wait_for(os.path.join(d, "old-restarted.marker")), "the previous version was not restarted")
        after = {k: v for k, v in snapshot(d).items() if not k.endswith(".marker")}             # the marker proves it ran
        self.assertEqual(after, before)


if __name__ == "__main__":
    unittest.main()
