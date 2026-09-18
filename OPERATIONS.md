# Operations

## Health checks

- `GET /health` — is the process alive at all (no DB touch)
- `GET /readiness` — is the process alive AND can it reach the database

Both are unauthenticated by design (a load balancer/monitoring probe
shouldn't need a key) and never expose secrets or internal details.

## Database backup

The database is a single SQLite file — back it up by copying it while the
container is running (SQLite handles this safely for a simple file copy in
this deployment's usage pattern — one writer, occasional reads):

```
# Docker Compose / named volume:
docker cp csp-monitor:/data/monitor.db ./monitor-backup-$(date +%F).db

# plain docker run / bind mount:
cp /path/to/monitor.db ./monitor-backup-$(date +%F).db
```

**Do this before any planned server/infrastructure work — not after.** This
project has already needed a full restore from exactly this kind of backup
once, after an infrastructure change unexpectedly wiped the original
container and its directory entirely.

### Restore

```
docker cp ./monitor-backup-2026-09-17.db csp-monitor:/data/monitor.db
docker restart csp-monitor
```

Then verify:

```
docker exec csp-monitor python3 -c "
import sqlite3
c = sqlite3.connect('/data/monitor.db')
print('csps:', c.execute('SELECT COUNT(*) FROM csps').fetchone())
print('api_keys:', c.execute('SELECT COUNT(*) FROM api_keys').fetchone())
"
```

## Logs

| | Location | Rotation |
|---|---|---|
| Server | `<server dir>/server.log` (or `CSP_MONITOR_LOG_PATH`) | 5MB × 3 backups |
| Agent | `<agent dir>/agent.log` | 2MB × 2 backups |

Both also write to stdout/stderr, so `docker logs csp-monitor` shows
everything for the server without needing to exec into the container.

**What gets logged (server):** authentication failures (`WARNING`), rate-
limit hits (`WARNING`), successful admin logins (`INFO`), unexpected
readiness-check failures (`ERROR`). Successful `/api/report` calls only log
at `DEBUG` (set `CSP_MONITOR_LOG_LEVEL=DEBUG` to see them) — at `INFO` the
log stays quiet during normal operation and only speaks up for something
worth noticing. **Never logs API keys or passwords** — only `csp_id` and
coarse outcomes. The full history of *what happened to which CSP* lives in
the `events`/`incidents` tables, not the log file — see below.

## Answering common operational questions

All of these are answerable directly from the database, without needing log
archaeology:

- **When did this CSP last report?** `csps.last_seen`, or the Fleet/detail
  page (shown as both relative and exact IST time).
- **When did its printer last work / how many times has it failed / how
  long was it down?** `incidents` table, filtered by `csp_id` and
  `device='printer'` — each row has `started_at`/`resolved_at`.
- **Which agent versions are deployed?** `csps.agent_version` per row (no
  aggregate "outdated agents" view exists yet — see
  [PRODUCTION_READINESS.md](PRODUCTION_READINESS.md) for what's still open).
- **What did an admin do and when?** `/audit-log` page, or the
  `admin_audit_log` table directly.

## Rotating the admin password

There's no in-app "change password" flow yet. Reset it directly:

```
docker exec csp-monitor python3 -c "
import sqlite3
from werkzeug.security import generate_password_hash
conn = sqlite3.connect('/data/monitor.db')
conn.execute(\"UPDATE admin_users SET password=? WHERE login_id='admin'\", (generate_password_hash('YOUR-NEW-PASSWORD'),))
conn.commit()
"
```

## Issuing/rotating/revoking a CSP's API key

Via the dashboard's **API Keys** page — issuing or rotating shows the
plaintext key exactly once (it's never stored or shown again, only its hash
and last-4 suffix). Revoke immediately blocks that CSP's agent from
reporting (`401` on its next heartbeat) without deleting its history.
