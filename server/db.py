"""Central server DB (SQLite, local to wherever this server runs)."""
import os
import sqlite3
from datetime import datetime, timezone

_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("CSP_MONITOR_DB_PATH", os.path.join(_DIR, "monitor.db"))
SCHEMA = os.path.join(_DIR, "schema.sql")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _migrate_csps_columns(conn):
    """Add any csps columns introduced after this DB was first created, via
    ALTER TABLE ADD COLUMN - skipped if the column already exists. Safe to
    run on every startup, including against an already-migrated DB."""
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(csps)").fetchall()}
    new_columns = {
        "printer_test_ran": "INTEGER NOT NULL DEFAULT 0",
        "printer_test_ok": "INTEGER NOT NULL DEFAULT 0",
        "printer_test_detail": "TEXT",
        "printer_test_checked_at": "TEXT",
    }
    for col_name, col_type in new_columns.items():
        if col_name not in existing:
            conn.execute(f"ALTER TABLE csps ADD COLUMN {col_name} {col_type}")


def setup():
    with get_connection() as conn:
        with open(SCHEMA, "r", encoding="utf-8") as f:
            conn.executescript(f.read())
        cur = conn.execute("SELECT COUNT(*) c FROM admin_users").fetchone()
        if cur["c"] == 0:
            from auth import hash_password
            conn.execute(
                "INSERT INTO admin_users (login_id, password, created_at) VALUES (?,?,?)",
                ("admin", hash_password("admin123"), _now()))
        _migrate_csps_columns(conn)
        conn.commit()
