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
CREATE TABLE IF NOT EXISTS api_keys (
    csp_id      TEXT PRIMARY KEY,
    api_key     TEXT NOT NULL,
    name        TEXT,
    active      INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL
);

-- One row per CSP install — the latest reported device status. last_seen
-- drives online/offline on the Fleet page.
CREATE TABLE IF NOT EXISTS csps (
    csp_id              TEXT PRIMARY KEY,
    name                TEXT,
    printer_configured  INTEGER NOT NULL DEFAULT 0,
    printer_ok          INTEGER NOT NULL DEFAULT 0,
    printer_status      TEXT,
    microatm_configured INTEGER NOT NULL DEFAULT 0,
    microatm_ok         INTEGER NOT NULL DEFAULT 0,
    microatm_status     TEXT,
    printer_test_ran         INTEGER NOT NULL DEFAULT 0,
    printer_test_ok          INTEGER NOT NULL DEFAULT 0,
    printer_test_detail      TEXT,
    printer_test_checked_at  TEXT,
    first_seen          TEXT,
    last_seen           TEXT
);
