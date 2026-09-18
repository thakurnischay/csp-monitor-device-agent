"""Central server DB (SQLite, local to wherever this server runs)."""
import os
import sqlite3
from datetime import datetime, timezone

_DIR = os.path.dirname(os.path.abspath(__file__))
SCHEMA = os.path.join(_DIR, "schema.sql")


def db_path() -> str:
    # Read lazily (not once at import time) so tests can point this at an
    # isolated temp file per test just by setting the env var before calling
    # get_connection()/setup() - no module reloading required.
    return os.environ.get("CSP_MONITOR_DB_PATH", os.path.join(_DIR, "monitor.db"))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(db_path())
    conn.row_factory = sqlite3.Row
    return conn


def _migrate_api_keys_table(conn):
    """One-time migration: the original schema stored api_keys.api_key in
    PLAINTEXT. This rebuilds the table with api_key_hash/api_key_suffix
    instead, hashing every existing key IN PLACE - no CSP needs a new key,
    the actual secret value is unchanged, only how it's stored. Safe to run
    on every startup: no-ops once the new column already exists. Uses the
    create-new-table/copy/drop/rename pattern since SQLite can't relax a
    NOT NULL constraint or rename semantics with a plain ALTER TABLE."""
    from security import hash_api_key, key_suffix

    existing = {row["name"] for row in conn.execute("PRAGMA table_info(api_keys)").fetchall()}
    if "api_key_hash" in existing:
        return  # already migrated

    conn.execute("""
        CREATE TABLE api_keys_new (
            csp_id          TEXT PRIMARY KEY,
            api_key_hash    TEXT NOT NULL,
            api_key_suffix  TEXT NOT NULL DEFAULT '',
            name            TEXT,
            active          INTEGER NOT NULL DEFAULT 1,
            created_at      TEXT NOT NULL,
            last_used_at    TEXT
        )
    """)
    old_rows = conn.execute("SELECT * FROM api_keys").fetchall()
    for r in old_rows:
        plaintext = r["api_key"]
        conn.execute(
            """INSERT INTO api_keys_new
                   (csp_id, api_key_hash, api_key_suffix, name, active, created_at)
               VALUES (?,?,?,?,?,?)""",
            (r["csp_id"], hash_api_key(plaintext), key_suffix(plaintext),
             r["name"], r["active"], r["created_at"]))
    conn.execute("DROP TABLE api_keys")
    conn.execute("ALTER TABLE api_keys_new RENAME TO api_keys")


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
        "printer_present": "INTEGER NOT NULL DEFAULT 0",
        "microatm_present": "INTEGER NOT NULL DEFAULT 0",
        "agent_version": "TEXT",
        "schema_version": "INTEGER",
        "os_info": "TEXT",
        "hostname": "TEXT",
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
        _migrate_api_keys_table(conn)
        conn.commit()
