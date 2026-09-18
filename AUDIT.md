# CSP Device Monitor — Codebase Audit

Scope: `agent/` (10 files) and `server/` (10 files + 5 templates), read in full.
No code changed as part of this audit.

---

## 1. Architecture as it actually exists today

```
CSP Windows PC
  agent.py (entry point)
    ├─ reporter.start_background()  → daemon thread, loop every interval_seconds (default 300s):
    │     device_health.check() → resolve/auto-detect devices → build_payload()
    │     → POST {server_url}/api/report  (X-API-Key header, JSON body)
    └─ local_ui.run()  → Flask dev server on 127.0.0.1:5057
          one-time setup page: server URL / CSP code / API key / device pick

Central server (Flask, single process)
  app.py     → creates Flask app, registers blueprints, defines `ist` Jinja filter, calls db.setup()
  api.py     → POST /api/report (agent-facing), GET /api/v1 (health-ish ping)
  routes.py  → /login, /logout, / (fleet), /csp/<id>, /api-keys  (admin-facing, session auth)
  db.py      → sqlite3 connection factory + schema bootstrap + ad-hoc column migration
  auth.py    → werkzeug password hash/verify
  schema.sql → 3 tables: admin_users, api_keys, csps
```

Config storage:
- **Agent**: single JSON file (`agent_config.json`) next to the script, atomic write (`tmp` + `os.replace`), guarded by an in-process `threading.Lock`.
- **Server**: env vars `CSP_MONITOR_SECRET_KEY`, `CSP_MONITOR_DB_PATH`; everything else is in `schema.sql`/hardcoded defaults.

No scheduler library anywhere — the agent's "scheduler" is a plain `while` loop with `Event.wait(interval)`; the server has no background jobs at all (fully request-driven).

---

## 2. What's genuinely already solid (do not disturb)

- **Device detection**: `device_health.py` checks Windows' real printer spooler (`Win32_Printer`) and PnP device list (`Get-PnpDevice`), merges in unfiltered/COM-port devices so a printer with no real Windows driver still surfaces, and does brand-keyword recognition with a strict "exactly one match or don't guess" rule. This is well-reasoned and already defends against the exact failure modes (virtual printers, unrecognized brands, COM-port-only printers) that were found on real CSP machines.
- **Hard timeouts on scans**: `run_with_ceiling()` wraps every `check()` call in a worker thread with a wall-clock ceiling (18s), specifically to survive Smart App Control / AV blocking a spawned process — this was validated against a real failure on a real CSP PC.
- **Daily test-print gating**: `_maybe_run_printer_functional_test()` is keyed off a stored date string, so it cannot run more than once per calendar day regardless of how many times `report_once()` fires (5-min loop or manual "Report now").
- **Setup page resilience**: combo-box (`<input list>` + `<datalist>`) instead of a rigid `<select>`, so manual entry always works even when the scan itself times out. Plain ES5 + `XMLHttpRequest` throughout (no `fetch`/arrow functions/`const`) specifically because a real CSP PC's browser silently killed a modern-JS page once.
- **Single-instance enforcement**: `install_task.ps1` kills any existing `agent.py` process before starting a new one, avoiding the split-config-file bug that was hit in the field.
- **DB migration pattern**: `_migrate_csps_columns()` uses `PRAGMA table_info` + conditional `ALTER TABLE ADD COLUMN` — safe to run on every startup, doesn't touch existing data. Good precedent to extend.

---

## 3. Bugs / correctness issues found

| # | Where | Issue |
|---|---|---|
| B1 | `reporter.py` `report_once()` | `requests.post(..., timeout=10)` uses one scalar for both connect and read; a slow-to-respond (not down) server can eat the full 10s on read alone, on top of the agent's own 18s device-scan ceiling — no separate connect-timeout. |
| B2 | `csp_detail()` in `routes.py` | Returns bare `"CSP not found", 404` — no template, breaks the UI's visual consistency. |
| B3 | `fleet.html` | Micro-ATM cell has no equivalent of the printer's `Test print:` sub-line — not wrong, just an inconsistency now that printer has functional-test detail and micro-ATM structurally cannot (documented, but worth a one-line UI note instead of silence). |

Nothing else rises to "wrong behavior" — the rest of the correctness issues below are *absences*, not defects.

---

## 4. Security risks

| # | Severity | Finding |
|---|---|---|
| S1 | **High** | `api_keys.api_key` is stored **in plaintext** (`schema.sql` line 17: `api_key TEXT NOT NULL`) and compared with `hmac.compare_digest` against the stored plaintext (`api.py`). A DB read (backup, dump, `SELECT *`) exposes every CSP's live key instantly. This is the single biggest gap relative to Phase 11. |
| S2 | **High** | No CSRF protection anywhere — `login.html`, and both forms in `api_keys.html` (issue/rotate, toggle, delete) are plain `<form method="post">` with no token. Session-cookie-only auth + no CSRF = a logged-in admin can be tricked into revoking/deleting a CSP's key via a forged cross-site request. |
| S3 | **Medium** | `app.secret_key` falls back to the literal string `"dev-secret-change-me"` if `CSP_MONITOR_SECRET_KEY` is unset (`app.py` line 14). If a future deploy forgets the env var, session cookies are forgeable by anyone who's read this file (which is now on GitHub). |
| S4 | **Medium** | No rate limiting on `/login` (password brute-force) or `/api/report` (an agent, or anyone with a stolen key, can hammer it). |
| S5 | **Medium** | No security headers set anywhere (`X-Frame-Options`, `X-Content-Type-Options`, `Content-Security-Policy`, `Strict-Transport-Security`). |
| S6 | **Low** | No `MAX_CONTENT_LENGTH` on the Flask app — `/api/report` will happily buffer an arbitrarily large request body into memory. |
| S7 | **Low** | Session cookie hardening not explicit — `SESSION_COOKIE_SECURE`/`SESSION_COOKIE_SAMESITE` are never set (Flask's own defaults apply, which are reasonable but not asserted). |
| S8 | **Low** | Default bootstrap admin credential (`admin` / `admin123`, `db.py` line 46) is hardcoded and now sits in a public-facing (private, but shared) repo. Already rotated on the live deployment, but the *pattern* ships to every fresh install. |
| S9 | **Informational** | CSP identity on `/api/report` comes from the request body (`csp_id`), and the API key is looked up *by* that csp_id, not the reverse. This isn't currently exploitable (a wrong csp_id + right key for a different CSP simply fails the lookup — fails safe), but it's architecturally backwards from what Phase 2 asks for ("derive identity from the authenticated key"), and blocks a future "list all keys, find which one this is" style lookup. |

---

## 5. Reliability / concurrency

- **No connection pooling / WAL mode** — `db.py` opens a fresh `sqlite3.connect()` per request with default settings (rollback-journal mode, default 5s busy-timeout). Fine at today's scale (6 CSPs); under concurrent writes from hundreds of CSPs reporting near-simultaneously, `database is locked` errors become likely. `PRAGMA journal_mode=WAL` is a near-free fix.
- **No bounded retry on a failed report** — a failed POST just waits for the next normal interval (≥30s, default 300s). This is *acceptable* (no busy-loop, no thundering herd) but doesn't match Phase 3's explicit "bounded retries/backoff" ask.
- **Agent config file race** — the in-process lock in `config_store.py` only protects against concurrent writes from *within one process*; `install_task.ps1`'s kill-existing-process step mitigates but doesn't atomically prevent a two-processes-briefly-alive race during a re-install.
- **No health/readiness endpoint** — `/api/v1` exists but only says "yes I'm a Flask app", doesn't check DB connectivity.

---

## 6. Data model gaps (relative to the requested Phases 5/9/10/25)

This is the largest structural gap. Today, `csps` is a single row per CSP that gets **overwritten** on every report — there is:
- **No history table** — the moment a printer goes from OK → PROBLEM → OK again, nothing records that it ever happened. "How long was it down?" is unanswerable from the DB as it stands.
- **No incidents model** — no OPEN/RESOLVED concept, no duration tracking.
- **No STALE state** — `_is_online()` is a hard boolean (within 15 min = online, else offline); there's no distinction between "just went quiet" and "never configured."
- **No audit log table** — API key issue/rotate/revoke/delete and admin logins are not recorded anywhere.
- **No agent_version / OS / hostname fields** — the heartbeat payload today is exactly `{csp_id, printer, microatm, printer_functional_test}`, no metadata, no schema version field.

---

## 7. UI/UX gaps (relative to Phase 7/8/16)

- Fleet page: no search, no filters, no sort, no pagination, no CSV export, no auto-refresh, no "problems only" view, no last-updated timestamp. (Fine for 6 rows; won't scale.)
- No relative timestamps anywhere (`"3 min ago"`) — only absolute IST strings.
- API Keys page: delete has a JS `confirm()`, but revoke/reactivate has **no confirmation** at all — one click flips a live CSP's ability to report.
- No last-used timestamp on API keys (Phase 16 asks for it; schema has no column for it).
- No loading/empty/error states beyond a single "No CSP has reported yet." row.

---

## 8. Low-spec-machine risk check (agent side specifically)

Checked against the constraint that CSP PCs may be weak (this exact codebase has already been field-tested against a 4GB-RAM class machine in an earlier related project):
- Agent has no GUI, no heavy deps (`requests`, stdlib only) — lightweight, fine.
- `subprocess.run(["powershell", ...])` per scan is the heaviest recurring cost; already mitigated by scanning once per cycle and reusing the result across resolve+status+dropdown (this was a real regression that got fixed earlier in this project).
- `printer_functional_test()` costs one real sheet of paper, gated to once/day — correctly bounded.
- No memory leaks spotted — no unbounded lists/caches; `agent_config.json` stays small (fixed key set).

**No changes recommended here** — this part is already tuned for the target hardware.

---

## 9. Summary — what's real vs. what's cosmetic

**Must fix for "production" in the sense Eko would mean it:**
S1 (plaintext API keys), S2 (CSRF), S3 (secret-key fallback), the missing history/incident/audit data model (Section 6), and basic Fleet-page usability (search/filter/pagination) once the fleet grows past a handful of CSPs.

**Should fix, lower urgency:**
S4–S7, B1, WAL mode, health check depth, retry/backoff, rate limiting.

**Cosmetic / already fine:**
Everything in Section 2 and Section 8 — agent-side device detection and low-spec behavior should not be touched.

---

## Proposed sequencing (matching the requested Implementation Strategy)

Given the size of the full spec (30 phases), doing all of it in one pass isn't realistic to review or trust. Proposed order, each a checkpoint:

1. **Security hardening** (S1–S8) — plaintext keys → hashed, CSRF tokens, secret-key enforcement, security headers, rate limiting. Backward-compatible migration (existing plaintext keys re-hashed in place, no re-issuing required).
2. **Data model** (Section 6) — add `events` and `incidents` tables + `agent_last_seen`/status-transition logic, additive migration (no data loss), wire into the existing report-ingest path.
3. **Dashboard** (Section 7) — search/filter/pagination/CSV/auto-refresh/relative timestamps, built on top of step 2's data.
4. **Agent metadata + reliability polish** (B1, agent_version field, split timeouts, bounded retry).
5. **Tests** for all of the above.
6. **Docs** (README/ARCHITECTURE/API/DEPLOYMENT/OPERATIONS/SECURITY) reflecting what was actually built.
7. **PRODUCTION_READINESS.md** as the final gate.

All work stays local — nothing gets committed/pushed to GitHub without your explicit go-ahead, per your instruction.

**Question before I start Step 1**: the plaintext→hashed API key migration means existing keys' *plaintext* value must be hashed in place (the value itself doesn't change, so no CSP needs a new key) — I'll do that automatically in the migration. Confirm you want me to proceed with Step 1 (security hardening) now?
