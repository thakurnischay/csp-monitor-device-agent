"""Shared test setup: a fresh, isolated SQLite file per test (never the real
monitor.db) plus a real Flask app instance wired to it."""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def make_test_app():
    """Returns (app, db_path). Call this AFTER setting
    os.environ['CSP_MONITOR_DB_PATH'] if you need a specific path, or just
    use the temp file this creates. Each call creates a brand-new database
    with a fresh schema - never touches a real install's data."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.remove(path)  # db.setup() creates it fresh
    os.environ["CSP_MONITOR_DB_PATH"] = path
    os.environ.setdefault("CSP_MONITOR_SECRET_KEY", "test-secret-key-not-for-production")
    # app.py configures logging once at first import (logging.basicConfig is
    # a no-op on later calls) - without this, running tests would leave a
    # real server.log file behind inside the actual source tree.
    os.environ.setdefault("CSP_MONITOR_LOG_PATH", os.path.join(tempfile.gettempdir(), "csp_monitor_test.log"))

    # Import AFTER setting env vars, and force a fresh app/blueprint each
    # time by clearing any cached modules from a previous test in this
    # process (Flask blueprints can only be registered once per app, but a
    # brand-new Flask() instance per test avoids that entirely).
    import db
    db.setup()
    import app as app_module

    # login_limiter/report_limiter are process-wide singletons (see
    # security.py) - without resetting them here, tests would leak rate-limit
    # state into each other via the test client's fixed IP or a reused
    # csp_id, in whatever order unittest happens to run them.
    from security import login_limiter, report_limiter
    login_limiter.reset()
    report_limiter.reset()

    return app_module.create_app(), path


def issue_test_key(csp_id="TEST_CSP_1", name="Test CSP"):
    """Issues a real API key the same way the admin UI does, returns the
    PLAINTEXT key (only obtainable at issue time, matching production)."""
    import db
    from security import generate_api_key, hash_api_key, key_suffix
    from datetime import datetime, timezone

    key = generate_api_key()
    with db.get_connection() as conn:
        conn.execute(
            "INSERT INTO api_keys (csp_id, api_key_hash, api_key_suffix, name, active, created_at) VALUES (?,?,?,?,1,?)",
            (csp_id, hash_api_key(key), key_suffix(key), name,
             datetime.now(timezone.utc).isoformat(timespec="seconds")))
        conn.commit()
    return key
