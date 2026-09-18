"""Unit tests for events.py - meaningful transitions only, incident
open/resolve, never a duplicate open incident."""
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from device_state import NOT_CONFIGURED, OK, PROBLEM
from events import apply_device_transition, record_audit, record_event


class EventsTestCase(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript("""
            CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT, csp_id TEXT, event_type TEXT, detail TEXT, occurred_at TEXT);
            CREATE TABLE incidents (id INTEGER PRIMARY KEY AUTOINCREMENT, csp_id TEXT, device TEXT, problem TEXT, status TEXT DEFAULT 'OPEN', started_at TEXT, resolved_at TEXT);
            CREATE TABLE admin_audit_log (id INTEGER PRIMARY KEY AUTOINCREMENT, actor TEXT, action TEXT, target_csp TEXT, detail TEXT, occurred_at TEXT);
        """)

    def _events(self):
        return self.conn.execute("SELECT * FROM events ORDER BY id").fetchall()

    def _incidents(self):
        return self.conn.execute("SELECT * FROM incidents ORDER BY id").fetchall()

    def test_no_op_when_state_unchanged(self):
        apply_device_transition(self.conn, "C1", "printer", OK, OK, "idle")
        self.assertEqual(len(self._events()), 0)
        self.assertEqual(len(self._incidents()), 0)

    def test_transition_into_problem_opens_incident_and_event(self):
        apply_device_transition(self.conn, "C1", "printer", OK, PROBLEM, "offline")
        events = self._events()
        incidents = self._incidents()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event_type"], "printer_disconnected")
        self.assertEqual(len(incidents), 1)
        self.assertEqual(incidents[0]["status"], "OPEN")
        self.assertEqual(incidents[0]["device"], "printer")

    def test_recovery_resolves_the_incident_and_events(self):
        apply_device_transition(self.conn, "C1", "printer", OK, PROBLEM, "offline")
        apply_device_transition(self.conn, "C1", "printer", PROBLEM, OK, "idle (ready)")
        events = self._events()
        incidents = self._incidents()
        self.assertEqual(len(events), 2)
        self.assertEqual(events[1]["event_type"], "printer_connected")
        self.assertEqual(incidents[0]["status"], "RESOLVED")
        self.assertIsNotNone(incidents[0]["resolved_at"])

    def test_does_not_open_a_second_incident_while_one_is_open(self):
        apply_device_transition(self.conn, "C1", "printer", OK, PROBLEM, "offline")
        # A second, different problem state while still broken (e.g. flips
        # to NOT_DETECTED) must not create ANOTHER open incident.
        apply_device_transition(self.conn, "C1", "printer", PROBLEM, "NOT_DETECTED", "unplugged")
        open_incidents = [i for i in self._incidents() if i["status"] == "OPEN"]
        self.assertEqual(len(open_incidents), 1)

    def test_non_problem_to_non_problem_transition_logs_generic_event_no_incident(self):
        apply_device_transition(self.conn, "C1", "printer", NOT_CONFIGURED, OK, "idle (ready)")
        events = self._events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event_type"], "printer_state_changed")
        self.assertEqual(len(self._incidents()), 0)

    def test_devices_are_independent(self):
        apply_device_transition(self.conn, "C1", "printer", OK, PROBLEM, "offline")
        apply_device_transition(self.conn, "C1", "microatm", OK, OK, "OK")
        self.assertEqual(len(self._events()), 1)  # only the printer transition
        self.assertEqual(len(self._incidents()), 1)

    def test_record_event_and_audit_write_expected_rows(self):
        record_event(self.conn, "C1", "test_print_success", "all good")
        record_audit(self.conn, "admin", "login", "", "")
        self.assertEqual(len(self._events()), 1)
        audit_rows = self.conn.execute("SELECT * FROM admin_audit_log").fetchall()
        self.assertEqual(len(audit_rows), 1)
        self.assertEqual(audit_rows[0]["actor"], "admin")
        self.assertEqual(audit_rows[0]["action"], "login")


if __name__ == "__main__":
    unittest.main()
