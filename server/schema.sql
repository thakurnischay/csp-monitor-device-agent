-- CSP Device Monitor — central server DB. Holds ONLY device-connectivity
-- status per CSP install (never customer data — there is none in this
-- project at all, it only watches hardware).

CREATE TABLE IF NOT EXISTS admin_users (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    login_id    TEXT UNIQUE NOT NULL,
    password    TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

-- Per-CSP API keys; an agent authenticates with one of these. csp_id IS the
-- CSP's code (the bank/business-assigned code doubles as the identifier —
-- there's no separate "code" field). name is an admin-entered display label.
--
-- The key itself is NEVER stored in plaintext - only a SHA-256 hash
-- (api_key_hash) plus a short suffix captured at issue time (api_key_suffix)
-- so the admin UI can still show "...ab12" without ever re-reading the real
-- value. See server/security.py.
CREATE TABLE IF NOT EXISTS api_keys (
    csp_id          TEXT PRIMARY KEY,
    api_key_hash    TEXT NOT NULL,
    api_key_suffix  TEXT NOT NULL DEFAULT '',
    name            TEXT,
    active          INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT NOT NULL,
    last_used_at    TEXT
);

-- One row per CSP install — the latest reported device status. last_seen
-- drives online/stale/offline on the Fleet page (see device_state.py).
CREATE TABLE IF NOT EXISTS csps (
    csp_id              TEXT PRIMARY KEY,
    name                TEXT,
    printer_configured  INTEGER NOT NULL DEFAULT 0,
    printer_present     INTEGER NOT NULL DEFAULT 0,
    printer_ok          INTEGER NOT NULL DEFAULT 0,
    printer_status      TEXT,
    microatm_configured INTEGER NOT NULL DEFAULT 0,
    microatm_present    INTEGER NOT NULL DEFAULT 0,
    microatm_ok         INTEGER NOT NULL DEFAULT 0,
    microatm_status     TEXT,
    printer_test_ran         INTEGER NOT NULL DEFAULT 0,
    printer_test_ok          INTEGER NOT NULL DEFAULT 0,
    printer_test_detail      TEXT,
    printer_test_checked_at  TEXT,
    agent_version       TEXT,
    schema_version      INTEGER,
    os_info             TEXT,
    hostname            TEXT,
    first_seen          TEXT,
    last_seen           TEXT
);

-- Meaningful state-change history, NOT a row per heartbeat (a CSP reporting
-- "still fine" every 5 minutes forever would otherwise generate unbounded
-- rows for zero information gain). One row is written only when a device's
-- derived state actually changes, a test print completes, or an admin
-- action happens - see device_state.py / api.py.
CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    csp_id      TEXT NOT NULL,
    event_type  TEXT NOT NULL,
    detail      TEXT,
    occurred_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_csp_time ON events(csp_id, occurred_at DESC);

-- Actionable problems, opened when a device's state transitions into a
-- problem state and resolved when it recovers. Never more than one OPEN
-- incident per (csp_id, device) at a time.
CREATE TABLE IF NOT EXISTS incidents (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    csp_id       TEXT NOT NULL,
    device       TEXT NOT NULL,
    problem      TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'OPEN',
    started_at   TEXT NOT NULL,
    resolved_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_incidents_csp ON incidents(csp_id);
CREATE INDEX IF NOT EXISTS idx_incidents_status ON incidents(status);

-- Administrative actions (login, API key lifecycle) - who did what, when.
-- Never records passwords or full API keys, only safe metadata.
CREATE TABLE IF NOT EXISTS admin_audit_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    actor       TEXT NOT NULL,
    action      TEXT NOT NULL,
    target_csp  TEXT,
    detail      TEXT,
    occurred_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_time ON admin_audit_log(occurred_at DESC);
