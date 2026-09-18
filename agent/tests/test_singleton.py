"""Unit tests for singleton.py - only one agent instance may run at a time,
and a stale (crashed) lock must not permanently block a fresh start."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import singleton


class SingletonTests(unittest.TestCase):
    def setUp(self):
        if os.path.exists(singleton._LOCK_PATH):
            os.remove(singleton._LOCK_PATH)

    def tearDown(self):
        if os.path.exists(singleton._LOCK_PATH):
            os.remove(singleton._LOCK_PATH)

    def test_first_acquire_succeeds_and_writes_our_pid(self):
        singleton.acquire_or_exit()
        self.assertTrue(os.path.exists(singleton._LOCK_PATH))
        with open(singleton._LOCK_PATH) as f:
            self.assertEqual(f.read().strip(), str(os.getpid()))

    def test_second_acquire_while_the_first_is_live_exits(self):
        singleton.acquire_or_exit()
        with self.assertRaises(SystemExit) as ctx:
            singleton.acquire_or_exit()
        self.assertEqual(ctx.exception.code, 0)

    def test_stale_lock_from_a_dead_pid_is_overwritten(self):
        with open(singleton._LOCK_PATH, "w") as f:
            f.write("999999")  # a PID essentially guaranteed not to be running
        singleton.acquire_or_exit()  # must NOT raise
        with open(singleton._LOCK_PATH) as f:
            self.assertEqual(f.read().strip(), str(os.getpid()))

    def test_release_removes_our_own_lock(self):
        singleton.acquire_or_exit()
        singleton.release()
        self.assertFalse(os.path.exists(singleton._LOCK_PATH))

    def test_release_never_removes_someone_elses_lock(self):
        with open(singleton._LOCK_PATH, "w") as f:
            f.write("123456")  # not our PID
        singleton.release()
        self.assertTrue(os.path.exists(singleton._LOCK_PATH))


if __name__ == "__main__":
    unittest.main()
