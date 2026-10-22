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
    biometric_configured INTEGER NOT NULL DEFAULT 0,
    biometric_present    INTEGER NOT NULL DEFAULT 0,
    biometric_ok         INTEGER NOT NULL DEFAULT 0,
    biometric_status     TEXT,
    gps_configured       INTEGER NOT NULL DEFAULT 0,
    gps_present          INTEGER NOT NULL DEFAULT 0,
    gps_ok               INTEGER NOT NULL DEFAULT 0,
    gps_status           TEXT,
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

-- Root-cause diagnosis (agent 2.0+): the CURRENT diagnosis of each device, and
-- the history of every issue the agent has raised for it.
CREATE TABLE IF NOT EXISTS device_diagnosis (
    csp_id          TEXT NOT NULL,
    device          TEXT NOT NULL,
    health_state    TEXT NOT NULL,
    severity        TEXT NOT NULL,
    summary         TEXT,
    detail_json     TEXT,
    last_healthy_at TEXT,
    updated_at      TEXT NOT NULL,
    PRIMARY KEY (csp_id, device)
);
CREATE TABLE IF NOT EXISTS device_issues (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    csp_id          TEXT NOT NULL,
    device          TEXT NOT NULL,
    code            TEXT NOT NULL,
    state           TEXT,
    severity        TEXT,
    title           TEXT,
    symptom         TEXT,
    root_cause      TEXT,
    confidence      TEXT,
    category        TEXT,
    evidence_json   TEXT,
    actions_json    TEXT,
    operator_action INTEGER NOT NULL DEFAULT 1,
    auto_fix_json   TEXT,
    status          TEXT NOT NULL DEFAULT 'OPEN',
    first_seen      TEXT NOT NULL,
    last_seen       TEXT NOT NULL,
    resolved_at     TEXT,
    occurrences     INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_issues_csp ON device_issues(csp_id, device, status);

-- ===================================================================================================
-- Secure enrollment + centralized signed updates (additive: nothing above is changed or dropped).
-- ===================================================================================================

-- The administrator's list of real CSPs. A CSP that already has an API key is also treated as registered.
CREATE TABLE IF NOT EXISTS csp_registry (
    csp_id        TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    pre_approved  INTEGER NOT NULL DEFAULT 0,      -- 1 = a matching first enrollment is approved automatically
    created_at    TEXT NOT NULL
);

-- A new agent asks for credentials. The agent generated the key itself; only its HASH is stored here.
-- Nothing is usable until an administrator approves (or the CSP was pre-approved by one).
CREATE TABLE IF NOT EXISTS enrollments (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    csp_id            TEXT NOT NULL,
    requested_name    TEXT NOT NULL,
    registry_name     TEXT,
    hostname          TEXT,
    machine_id        TEXT,
    agent_version     TEXT,
    remote_addr       TEXT,
    key_hash          TEXT NOT NULL,
    key_suffix        TEXT NOT NULL DEFAULT '',
    replaces_existing INTEGER NOT NULL DEFAULT 0,  -- 1 = this CSP already has an agent/key
    status            TEXT NOT NULL DEFAULT 'PENDING',   -- PENDING / APPROVED / REJECTED / SUPERSEDED
    created_at        TEXT NOT NULL,
    decided_at        TEXT,
    decided_by        TEXT,
    note              TEXT
);
CREATE INDEX IF NOT EXISTS idx_enroll_status ON enrollments(status, csp_id);
CREATE UNIQUE INDEX IF NOT EXISTS ux_enroll_key ON enrollments(key_hash);

-- Signed agent releases. The package file lives on disk (see updates.release_dir()); only its metadata is here.
CREATE TABLE IF NOT EXISTS agent_releases (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    version            TEXT NOT NULL UNIQUE,
    sequence           INTEGER NOT NULL UNIQUE,          -- strictly increasing: agents never go down
    kind               TEXT NOT NULL DEFAULT 'release',  -- release / rollback
    notes              TEXT,
    key_id             TEXT,
    manifest_b64       TEXT NOT NULL,                    -- the exact signed bytes
    signature_b64      TEXT NOT NULL,
    package_sha256     TEXT NOT NULL,
    package_size       INTEGER NOT NULL,
    package_path       TEXT NOT NULL,
    min_agent_sequence INTEGER NOT NULL DEFAULT 0,
    status             TEXT NOT NULL DEFAULT 'UPLOADED', -- UPLOADED / APPROVED / REVOKED
    uploaded_by        TEXT,
    uploaded_at        TEXT NOT NULL,
    approved_by        TEXT,
    approved_at        TEXT,
    revoked_by         TEXT,
    revoked_at         TEXT
);

CREATE TABLE IF NOT EXISTS rollouts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    release_id  INTEGER NOT NULL,
    kind        TEXT NOT NULL DEFAULT 'pilot',       -- pilot / full / expansion
    status      TEXT NOT NULL DEFAULT 'ACTIVE',      -- ACTIVE / PAUSED / CANCELLED
    note        TEXT,
    created_by  TEXT,
    created_at  TEXT NOT NULL
);

-- One row per (CSP, release): re-assigning the same release to the same CSP is a no-op (idempotent).
CREATE TABLE IF NOT EXISTS update_deployments (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    rollout_id    INTEGER NOT NULL,
    release_id    INTEGER NOT NULL,
    csp_id        TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'pending',
    detail        TEXT,
    attempt       INTEGER NOT NULL DEFAULT 1,
    from_version  TEXT,
    created_at    TEXT NOT NULL,
    assigned_at   TEXT,
    updated_at    TEXT NOT NULL,
    finished_at   TEXT,
    last_error    TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_deploy_csp_release ON update_deployments(csp_id, release_id);
CREATE INDEX IF NOT EXISTS idx_deploy_csp_status ON update_deployments(csp_id, status);

CREATE TABLE IF NOT EXISTS csp_groups (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE,
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS csp_group_members (
    group_id  INTEGER NOT NULL,
    csp_id    TEXT NOT NULL,
    PRIMARY KEY (group_id, csp_id)
);


-- Required versions of vendor software that drives the micro-ATM (set by an administrator; see software.py).
-- Rows are never deleted, only deactivated, so the initial values are seeded exactly once.
CREATE TABLE IF NOT EXISTS software_requirements (
    key          TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    min_version  TEXT NOT NULL,
    note         TEXT,
    active       INTEGER NOT NULL DEFAULT 1,
    updated_by   TEXT,
    updated_at   TEXT
);

-- ============================================================================================================
-- Email Campaigns (festival / occasion emails to operators who lack a device). All additive.
-- The stable operator identity is the CSP code (upper-case, no spaces) - the same value the monitoring agents use
-- as csp_id. Operator data is an IMPORTED SNAPSHOT owned by the CSP Growth Platform; it is never written back, and
-- re-importing never touches campaign history (campaign_* / email_* tables hold their own copy of what was sent).
-- ============================================================================================================
CREATE TABLE IF NOT EXISTS operator_imports (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    imported_at     TEXT NOT NULL,
    imported_by     TEXT,
    source_name     TEXT,
    source_stamp    TEXT,                 -- when the source file says it was produced
    rows_total      INTEGER NOT NULL DEFAULT 0,
    rows_accepted   INTEGER NOT NULL DEFAULT 0,
    rows_skipped    INTEGER NOT NULL DEFAULT 0,
    summary_json    TEXT
);
CREATE TABLE IF NOT EXISTS campaign_operators (
    csp_code            TEXT PRIMARY KEY,
    name                TEXT,
    email               TEXT,
    mobile              TEXT,
    rm                  TEXT,
    state               TEXT,
    district            TEXT,
    activity            TEXT,             -- ACTIVE / DORMANT / INACTIVE (the email project's own rule)
    activity_reason     TEXT,
    contactable         INTEGER,
    engine_input_json   TEXT,             -- the fields the ownership rules read (claims, transaction counts)
    present_in_latest   INTEGER NOT NULL DEFAULT 1,   -- 0 = not in the newest import (kept, but never emailed)
    first_imported_at   TEXT,
    updated_at          TEXT,
    last_import_id      INTEGER
);
CREATE TABLE IF NOT EXISTS operator_devices (
    csp_code    TEXT NOT NULL,
    device      TEXT NOT NULL,            -- MATM / PRINTER / GPS / BIOMETRIC
    verdict     TEXT NOT NULL,            -- OWNED / GAP_VERIFIED / GAP_DISPUTED / UNKNOWN
    evidence    TEXT,
    source      TEXT,                     -- calling_sheet / monitoring_agent / reconciliation
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (csp_code, device)
);
-- Human decisions (an RM phoned the CSP): they beat every inferred signal, exactly as in the email project.
CREATE TABLE IF NOT EXISTS operator_reconciliations (
    csp_code    TEXT NOT NULL,
    device      TEXT NOT NULL,
    owns_device INTEGER NOT NULL,
    decided_by  TEXT,
    decided_at  TEXT,
    note        TEXT,
    PRIMARY KEY (csp_code, device)
);

CREATE TABLE IF NOT EXISTS email_campaigns (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    name                 TEXT NOT NULL,
    description          TEXT,
    occasion             TEXT NOT NULL,
    devices_json         TEXT NOT NULL,                 -- devices the audience must lack, e.g. ["GPS","BIOMETRIC"]
    audience_match       TEXT NOT NULL DEFAULT 'any',   -- any: lacks at least one selected device / all: lacks every one
    include_dormant      INTEGER NOT NULL DEFAULT 0,
    subject              TEXT NOT NULL,
    body                 TEXT NOT NULL,
    link_url             TEXT,
    link_label           TEXT,
    status               TEXT NOT NULL DEFAULT 'draft', -- draft / scheduled / queued / sending / paused / completed / cancelled
    mode                 TEXT,                          -- dry / live (decided when launched or scheduled)
    scheduled_at         TEXT,
    created_by           TEXT,
    created_at           TEXT NOT NULL,
    updated_at           TEXT NOT NULL,
    launched_by          TEXT,
    launched_at          TEXT,
    completed_at         TEXT,
    cancelled_at         TEXT,
    config_snapshot_json TEXT,                          -- the exact configuration at launch (audit)
    outcome_json         TEXT
);
CREATE TABLE IF NOT EXISTS email_campaign_recipients (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id         INTEGER NOT NULL REFERENCES email_campaigns(id),
    csp_code            TEXT NOT NULL,
    bucket              TEXT NOT NULL,                  -- eligible / excluded / review
    reason              TEXT NOT NULL,                  -- why this operator is in / out, in plain words
    details_json        TEXT,                           -- every check that was evaluated
    missing_devices_json TEXT,                          -- devices CONFIRMED absent (named in the email)
    review_devices_json  TEXT,                          -- devices whose ownership is unknown or in conflict
    email_at_send       TEXT,
    name_at_send        TEXT,
    rm_at_send          TEXT,
    location_at_send    TEXT,
    status              TEXT NOT NULL DEFAULT 'pending', -- pending / queued / sending / accepted / failed / unknown_outcome / cancelled
    delivery_status     TEXT NOT NULL DEFAULT 'not_confirmed', -- not_confirmed / delivered / bounced / complained (set ONLY by provider events)
    attempts            INTEGER NOT NULL DEFAULT 0,
    next_attempt_at     TEXT,
    last_error          TEXT,
    failure_kind        TEXT,                           -- transient_exhausted / permanent
    smtp_message_id     TEXT,                           -- the Message-ID we put on the message (not a provider receipt)
    subject_sent        TEXT,
    body_html           TEXT,
    body_text           TEXT,
    token               TEXT NOT NULL UNIQUE,           -- unguessable; used by the unsubscribe / open / click links
    accepted_at         TEXT,
    delivered_at        TEXT,
    first_open_at       TEXT,
    open_count          INTEGER NOT NULL DEFAULT 0,
    first_click_at      TEXT,
    click_count         INTEGER NOT NULL DEFAULT 0,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    UNIQUE (campaign_id, csp_code)                      -- one operator, one message per campaign: the duplicate guard
);
CREATE INDEX IF NOT EXISTS idx_ecr_campaign_status ON email_campaign_recipients(campaign_id, status);
CREATE INDEX IF NOT EXISTS idx_ecr_csp ON email_campaign_recipients(csp_code);
CREATE INDEX IF NOT EXISTS idx_ecr_msgid ON email_campaign_recipients(smtp_message_id);
CREATE TABLE IF NOT EXISTS email_attempts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    recipient_id    INTEGER NOT NULL REFERENCES email_campaign_recipients(id),
    attempt_no      INTEGER NOT NULL,
    mode            TEXT NOT NULL,                      -- dry / live
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    outcome         TEXT,                               -- dry_run / accepted / transient_failure / permanent_failure
    smtp_message_id TEXT,
    error           TEXT
);
CREATE INDEX IF NOT EXISTS idx_attempts_recipient ON email_attempts(recipient_id);
CREATE TABLE IF NOT EXISTS email_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    recipient_id    INTEGER NOT NULL REFERENCES email_campaign_recipients(id),
    kind            TEXT NOT NULL,                      -- accepted / delivered / bounced / complained / open / click / unsubscribed
    occurred_at     TEXT NOT NULL,
    source          TEXT NOT NULL,                      -- smtp / provider / pixel / link
    external_id     TEXT,                               -- provider's own event id (for de-duplication)
    detail          TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_recipient ON email_events(recipient_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_events_external ON email_events(recipient_id, kind, external_id) WHERE external_id IS NOT NULL;
CREATE TABLE IF NOT EXISTS email_suppressions (
    email               TEXT PRIMARY KEY,               -- lower-case
    csp_code            TEXT,
    reason              TEXT NOT NULL,                  -- unsubscribed / bounced / complaint / manual
    source_campaign_id  INTEGER,
    created_at          TEXT NOT NULL,
    created_by          TEXT
);
