# Production Readiness Report

This is the final gate for the hardening work done across Steps 1–7. Every
check below was actually run, not just asserted — see the "Test results"
section for what was executed and its real output.

## Verdict

**Qualified yes** — the security, reliability, and data-model gaps
identified in [AUDIT.md](AUDIT.md) are closed, verified against real
production data (not just synthetic test fixtures), and covered by a
permanent test suite. A small number of known limitations remain (listed
below) — none of them block a safe deployment, but they should be read and
consciously accepted, not discovered later.

## Implemented features (by step)

1. **Security** — API keys hashed (SHA-256, never plaintext at rest), CSRF
   protection on every admin form, enforced/warned session secret key,
   security headers (`CSP`, `X-Frame-Options`, `X-Content-Type-Options`),
   rate limiting (login: 8/min/IP, reports: 6/min/CSP), 64KB request cap,
   hardened session cookies.
2. **Data model** — explicit device/CSP states (`device_state.py`), an
   `events` log (meaningful transitions only), an `incidents` model
   (open/resolve with duration), an `admin_audit_log`.
3. **Dashboard** — search, status filter, "problems only" filter, sortable
   columns, pagination (50/page, tested at 120 synthetic rows), CSV export,
   relative timestamps with exact-time tooltips, auto-refresh.
4. **Agent reliability** — split connect/read timeout, one bounded retry on
   network failure (never on a 401), a versioned heartbeat payload
   (`schema_version`, `agent_version`, `os`, `hostname`), a PID-lock
   singleton guard, best-effort graceful shutdown.
5. **Tests** — 83 tests (unittest + Flask's test client), zero new
   dependencies.
6. **Docs** — README, ARCHITECTURE, API, DEPLOYMENT, OPERATIONS,
   TROUBLESHOOTING, SECURITY, AGENT_INSTALLATION.
7. **This review** — health/readiness endpoints, rotating logging (closing
   two gaps that would otherwise have made Step 6's docs describe features
   that didn't exist), a full secrets sweep (found and fixed a real leaked
   production key), and migration testing against genuine production data.

## Files changed

**New files:** `server/security.py`, `server/device_state.py`,
`server/events.py`, `server/templates/audit_log.html`,
`server/templates/not_found.html`, `server/tests/*`, `agent/singleton.py`,
`agent/version.py`, `agent/tests/*`, plus this doc and the other 7 markdown
files at the repo root.

**Modified:** `server/schema.sql`, `server/db.py`, `server/api.py`,
`server/routes.py`, `server/app.py`, `server/run.sh`,
`server/templates/{login,api_keys,fleet,csp_detail,base}.html`,
`agent/agent.py`, `agent/reporter.py`, `.gitignore`.

**Untouched, deliberately:** `agent/device_health.py`,
`agent/local_ui.py`, `agent/config_store.py` — the actual device-detection
logic, which was already solid per the original audit and was preserved
exactly per the standing instruction not to disturb working detection code.

## Database migrations

All additive — `ALTER TABLE ADD COLUMN` for new fields, `CREATE TABLE IF NOT
EXISTS` for new tables, a create-copy-drop-rename for the one constraint
change (`api_keys.api_key` plaintext → `api_key_hash`/`api_key_suffix`).
**Verified against the actual original production database backup**
(pre-dating all seven steps), not just synthetic test data — 6 CSPs, 8 API
keys, all names and hashes correct afterward, and idempotent (a second
`setup()` call is a safe no-op). See "Test results" below for the exact
commands run.

**Rollback:** there is no down-migration — these are all additive/backward-
compatible changes, so the correct rollback if ever needed is restoring the
pre-change database backup, not reversing individual `ALTER TABLE`
statements. Keep a backup before any deploy (see
[OPERATIONS.md](OPERATIONS.md)).

## New API endpoints

`GET /health`, `GET /readiness`, `GET /fleet.csv`, `GET /audit-log`. Full
reference in [API.md](API.md).

## Test results (actually run, not claimed)

```
server: 57 tests, OK
agent:  26 tests, OK
```

Plus, run specifically for this review (not part of the permanent suite,
since they need the real production backup file which isn't checked in):

- Migration against the real, original, pre-hardening production database:
  **PASS** (6 CSPs, 8 API keys, correct hashes, correct names)
- Migration run twice in a row (idempotency): **PASS**
- All 4 declared indexes present after migration: **PASS**
- `py_compile` on all 26 tracked `.py` files: **PASS**, zero errors
- Repo-wide secrets sweep: **found and fixed** a real, previously-
  undetected production secret key hardcoded in `server/run.sh` (separate
  from the Dockerfile issue caught earlier) — now sources from a gitignored
  `.env` instead
- `local-run/` (a working copy containing a real database backup) was not
  gitignored — **fixed**

## Known limitations (accepted, not blocking)

- **No RBAC** — one shared admin login. Fine for a small internal team; not
  suitable for a larger multi-admin rollout without real work first.
- **No agent auto-update** — deliberately: an insecure updater is worse than
  none. Distribution is manual (zip + `SETUP.bat`).
- **No aggregate "outdated agent versions" dashboard view** — the raw data
  (`csps.agent_version`) is captured and shown per-CSP, but there's no
  fleet-wide summary or alerting on stale versions. This needs a policy
  decision (what counts as "outdated") that wasn't mine to make unilaterally.
- **SQLite, not Postgres** — appropriate at current scale (single digit to
  low hundreds of CSPs); the data-access layer (`db.py`) is a thin enough
  wrapper that a future migration is straightforward, but hasn't been done.
- **No event/incident retention policy** — the `events` table grows forever
  (bounded per-CSP display via `LIMIT 25`/`LIMIT 10`, but nothing prunes old
  rows). Not a problem at current volume; revisit if the fleet grows into
  the thousands over a period of years.
- **No admin change-password UI** — reset is a direct DB command (documented
  in [OPERATIONS.md](OPERATIONS.md)), not a page.

## Deployment requirements

- Python 3.10+ (server), Python 3.10+ with `requests` only (agent)
- `CSP_MONITOR_SECRET_KEY` set to a real value in production (see
  [SECURITY.md](SECURITY.md) checklist)
- A backup of `monitor.db` taken **before** any infrastructure change to the
  host it runs on — this was learned the hard way, twice, on this exact
  project

## Recommended production environment

Current deployment model (Docker + Tailscale Funnel, `waitress-serve` as the
WSGI server) is appropriate for the current scale and is what's documented
in [DEPLOYMENT.md](DEPLOYMENT.md). No change recommended unless the fleet
size grows enough to need Postgres + a proper reverse proxy with its own TLS
termination.

## Production deployment checklist

See [SECURITY.md](SECURITY.md)'s checklist for the security-specific items.
In addition:

- [ ] Take a fresh `monitor.db` backup immediately before deploying
- [ ] Run the migration against a **copy** of production data first if
      there's any doubt (see the migration test commands above — they're
      copy-paste ready)
- [ ] Confirm `/readiness` returns `200` after deploy, not just `/health`
- [ ] Confirm the Fleet page loads and shows the expected CSP count
- [ ] Confirm `/audit-log` shows the deploying admin's own next login
