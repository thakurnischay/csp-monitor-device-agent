"""Unit tests for device_state.py - the explicit state derivation that
replaces ambiguous booleans."""
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from device_state import (NOT_CONFIGURED, NOT_DETECTED, OFFLINE, OK, ONLINE,
                          PROBLEM, SCAN_ERROR, STALE, NEVER_REPORTED,
                          derive_csp_state, derive_device_state)


class DeviceStateTests(unittest.TestCase):
    def test_not_configured_when_configured_false(self):
        self.assertEqual(derive_device_state(0, 0, 0, ""), NOT_CONFIGURED)
        self.assertEqual(derive_device_state(0, 1, 1, "idle (ready)"), NOT_CONFIGURED)

    def test_not_detected_when_configured_but_not_present(self):
        self.assertEqual(derive_device_state(1, 0, 0, "not found"), NOT_DETECTED)

    def test_ok_when_configured_present_and_ok(self):
        self.assertEqual(derive_device_state(1, 1, 1, "idle (ready)"), OK)

    def test_problem_when_configured_present_but_not_ok(self):
        self.assertEqual(derive_device_state(1, 1, 0, "offline"), PROBLEM)

    def test_scan_error_overrides_everything_else(self):
        # A scan-error status string wins regardless of the boolean flags,
        # since those are meaningless when the scan itself never completed.
        self.assertEqual(
            derive_device_state(0, 0, 0, "scan timed out - a security tool on this PC may be blocking the check"),
            SCAN_ERROR)
        self.assertEqual(
            derive_device_state(1, 1, 1, "could not send test page: timed out"),
            SCAN_ERROR)

    def test_case_insensitive_scan_error_match(self):
        self.assertEqual(derive_device_state(1, 1, 0, "SCAN TIMED OUT - blocked"), SCAN_ERROR)


class CspStateTests(unittest.TestCase):
    def _iso(self, minutes_ago):
        return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat(timespec="seconds")

    def test_never_reported_when_blank(self):
        self.assertEqual(derive_csp_state(""), NEVER_REPORTED)
        self.assertEqual(derive_csp_state(None), NEVER_REPORTED)

    def test_never_reported_on_unparseable_timestamp(self):
        self.assertEqual(derive_csp_state("not-a-date"), NEVER_REPORTED)

    def test_online_within_window(self):
        self.assertEqual(derive_csp_state(self._iso(5)), ONLINE)
        self.assertEqual(derive_csp_state(self._iso(14.9)), ONLINE)

    def test_stale_between_windows(self):
        self.assertEqual(derive_csp_state(self._iso(30)), STALE)
        self.assertEqual(derive_csp_state(self._iso(59)), STALE)

    def test_offline_beyond_stale_window(self):
        self.assertEqual(derive_csp_state(self._iso(120)), OFFLINE)
        self.assertEqual(derive_csp_state(self._iso(60 * 24 * 30)), OFFLINE)

    def test_naive_timestamp_treated_as_utc(self):
        # last_seen is stored without a timezone suffix in some rows (older
        # data) - must not crash and must not be misread as local time.
        naive = (datetime.now(timezone.utc) - timedelta(minutes=5)).replace(tzinfo=None).isoformat(timespec="seconds")
        self.assertEqual(derive_csp_state(naive), ONLINE)


if __name__ == "__main__":
    unittest.main()
