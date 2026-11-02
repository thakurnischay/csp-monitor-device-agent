"""Upgrading a database created BEFORE the revamp: every existing record survives, nothing is rewritten, and the new
tables/columns appear. The old schema is a frozen copy of the last pre-revamp schema.sql, so this is a real upgrade."""
import os
import sqlite3
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.dirname(HERE)
sys.path.insert(0, SERVER)

NEW_TABLES = {"csp_registry", "enrollments", "agent_releases", "rollouts", "update_deployments", "csp_groups", "csp_group_members"}


def old_schema():
    """The schema as it was before the revamp (frozen copy of the last pre-revamp commit's schema.sql)."""
    with open(os.path.join(HERE, "fixtures", "pre_revamp_schema.sql"), encoding="utf-8") as f:
        return f.read()


class Migration(unittest.TestCase):
    def test_existing_records_survive_the_upgrade(self):
        schema = old_schema()
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        def cleanup():
            for suffix in ("", "-wal", "-shm"):
                try:
                    os.remove(path + suffix)
                except OSError:
                    pass                                                 # Windows may still hold the file; temp dir is cleaned later
        self.addCleanup(cleanup)
        con = sqlite3.connect(path)
        con.executescript(schema)
        self.assertNotIn("enrollments", {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")})
        import hashlib
        key_hash = hashlib.sha256(b"legacy-key").hexdigest()
        con.execute("INSERT INTO api_keys (csp_id, api_key_hash, api_key_suffix, name, active, created_at) VALUES ('OLD-1', ?, 'key', 'Old CSP', 1, '2026-01-01T00:00:00')", (key_hash,))
        cols = [r[1] for r in con.execute("PRAGMA table_info(csps)")]
        con.execute("INSERT INTO csps (csp_id, name, last_reported_at) VALUES ('OLD-1', 'Old CSP', '2026-01-01T00:00:00')") if "last_reported_at" in cols else \
            con.execute("INSERT INTO csps (csp_id, name) VALUES ('OLD-1', 'Old CSP')")
        con.commit()
        before_csps = [tuple(r) for r in con.execute("SELECT * FROM csps")]
        before_keys = [tuple(r) for r in con.execute("SELECT * FROM api_keys")]
        con.close()

        os.environ["CSP_MONITOR_DB_PATH"] = path
        import db
        db.setup()
        db.setup()                                                       # idempotent: safe on every start
        con = sqlite3.connect(path)
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue(NEW_TABLES <= tables, NEW_TABLES - tables)
        new_cols = [r[1] for r in con.execute("PRAGMA table_info(csps)")]
        for c in ("update_capable", "updater_version", "agent_sequence"):
            self.assertIn(c, new_cols)
        after_keys = [tuple(r) for r in con.execute("SELECT * FROM api_keys")]
        self.assertEqual(after_keys, before_keys)                       # keys and hashes untouched (nothing rotated)
        row = con.execute("SELECT * FROM csps WHERE csp_id='OLD-1'").fetchone()
        self.assertEqual(tuple(row[:len(before_csps[0])]), before_csps[0])   # every existing column value unchanged
        legacy = con.execute("SELECT update_capable, updater_version, agent_sequence FROM csps WHERE csp_id='OLD-1'").fetchone()
        self.assertEqual(tuple(legacy), (None, None, None))             # legacy: update fields unknown -> "needs bootstrap"
        con.close()


if __name__ == "__main__":
    unittest.main()
