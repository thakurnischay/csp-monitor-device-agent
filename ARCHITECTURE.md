# Architecture

## Overall flow

```
CSP Windows PC
    |
    v
Lightweight Agent (agent/agent.py)
    |
    | HTTPS, X-API-Key header
    v
Flask API (server/api.py: POST /api/report)
    |
    v
SQLite (server/monitor.db)
    |
    v
Fleet Dashboard (server/routes.py + templates/)
```

The API key identifies the CSP — the server looks up the key's hash and
only trusts the `csp_id` that key was actually issued for. A client-supplied
`csp_id` in the request body is never trusted on its own (see
[SECURITY.md](SECURITY.md)).

## Agent (`agent/`)

One process (`agent.py`) runs two things at once:

- **`reporter.py`** — a background thread. Every `interval_seconds` (default
  300s): calls `device_health.check()`, builds a heartbeat payload, POSTs it
  to the server. A failed check or unreachable server never stops the loop;
  a failed POST gets one bounded retry (2s delay) before waiting for the
  next normal interval.
- **`local_ui.py`** — a localhost-only Flask page (`127.0.0.1:5057`) for
  one-time setup: server URL, CSP code, API key, and (if needed) manually
  picking the printer/micro-ATM.

### Device detection (`device_health.py`)

No vendor SDK — this reads what Windows itself already knows:

- **Printers**: the Windows print spooler (`Win32_Printer` via
  PowerShell/WMI) for status, PLUS every raw PnP device (`Get-PnpDevice`,
  unfiltered) for anything that's a printer but never registered as one —
  many passbook printers attach as a plain COM port, not a real Windows
  printer. Both sources are merged into one candidate list.
- **Micro-ATMs**: PnP devices in USB-ish classes (`USB`, `HIDClass`,
  `Ports`, `SmartCardReader`, `Biometric`), plus anything elsewhere that
  matches a known micro-ATM brand keyword.
- **Recognition**: known brand-name keyword matching (`_KNOWN_PRINTER_BRANDS`,
  `_KNOWN_MICROATM_BRANDS`) against whatever's actually present. Recognizes
  and self-configures automatically ONLY when there's exactly one match — an
  ambiguous (2+ matches) or unrecognized (0 matches) result always falls
  back to whatever's manually saved, never guesses.
- **Manual fallback**: the setup page's device fields are combo boxes
  (`<input list>` + `<datalist>`), not rigid dropdowns — even if the scan
  itself fails entirely (blocked by antivirus/Smart App Control), you can
  type the exact device name yourself.
- **Functional test**: once per calendar day, sends a real Windows test
  page (`rundll32 printui.dll,PrintUIEntry /k`) to the printer and reads the
  print job's own error state (`Win32_PrintJob`) — catches paper-out/jam/
  door-open that a simple "is it plugged in" check can't. Gated by a stored
  date, so it can never run more than once/day regardless of heartbeat
  frequency or manual "Report now" clicks.
- **Hard timeouts everywhere**: `run_with_ceiling()` wraps every scan in a
  worker thread with a wall-clock ceiling (18s) — a real failure mode
  (Smart App Control silently blocking a spawned process) can otherwise hang
  a scan indefinitely; this guarantees the caller always gets a response.
- **Single instance**: `singleton.py` — a PID lock file + `tasklist`, so two
  copies never run at once (each would have its own config file and
  silently fight the other) even if launched by hand, not just via the
  installer.

### Config storage

A single JSON file (`agent_config.json`) next to the script — server URL,
CSP code, API key, which devices are configured, check interval. Written
atomically (`tmp` file + `os.replace`).

## Server (`server/`)

```
app.py     -> creates the Flask app: secret key, security headers, CSRF,
              logging, health checks, Jinja filters (ist/relative_time)
api.py     -> POST /api/report (agent-facing), GET /api/v1, /health, /readiness
routes.py  -> /login, / (fleet), /csp/<id>, /api-keys, /audit-log (session auth)
db.py      -> SQLite connection + schema bootstrap + migrations
device_state.py -> explicit state derivation (see below)
events.py  -> writes to events/incidents/admin_audit_log
security.py -> API key hashing, CSRF tokens, rate limiting
auth.py    -> admin password hashing (werkzeug)
```

### Data model

| Table | Purpose |
|---|---|
| `admin_users` | Dashboard login (hashed password) |
| `api_keys` | One row per CSP; `api_key_hash` + `api_key_suffix`, never plaintext |
| `csps` | Latest known state per CSP — overwritten on every report |
| `events` | Meaningful state changes only (never one row per heartbeat) |
| `incidents` | OPEN/RESOLVED problems, with start/resolve timestamps |
| `admin_audit_log` | Login + API-key lifecycle actions |

### Explicit state model (`device_state.py`)

Device state (`OK` / `PROBLEM` / `NOT_CONFIGURED` / `NOT_DETECTED` /
`SCAN_ERROR`) is derived from `configured`/`present`/`ok`/`status` already
in the payload — not a new field the agent has to send. `SCAN_ERROR` is
detected by matching the agent's own stable, code-controlled status text
("scan timed out...") rather than trusting a boolean, since a scan that
never completed can't set booleans meaningfully either way.

CSP state (`ONLINE` / `STALE` / `OFFLINE` / `NEVER_REPORTED`) is derived
purely from `last_seen` age — no extra field, no background job needed.

### Events and incidents (`events.py`)

Every `/api/report` compares the OLD stored state against the NEW derived
state (for both printer and micro-ATM independently). A change writes an
`events` row; a transition INTO a problem state opens an `incidents` row (if
one isn't already open for that CSP+device); a transition OUT of one
resolves it. A CSP's very first-ever report is treated as transitioning from
`NOT_CONFIGURED`, so an immediately-broken first report still opens an
incident right away.

Identical repeated heartbeats create nothing — this was a deliberate
design constraint (Phase 9 of the original hardening plan): "meaningful
transitions only," not an unbounded row per heartbeat.

### Heartbeat payload (schema_version 2)

```json
{
  "schema_version": 2,
  "agent_version": "1.1.0",
  "os": "Windows-10-10.0.26200-SP0",
  "hostname": "CSP-PC-01",
  "csp_id": "1A850244",
  "reported_at": "2026-09-18T07:15:00+00:00",
  "printer": {"configured": true, "present": true, "ok": true, "status": "idle (ready)", "resolved_name": "..."},
  "microatm": {"configured": true, "present": true, "ok": true, "status": "OK", "resolved_name": "..."},
  "printer_functional_test": {"ran": true, "ok": true, "detail": "..."}
}
```

`schema_version`/`agent_version`/`os`/`hostname` are all optional — an
older agent that doesn't send them still reports fine; the server preserves
previously-known-good metadata via `COALESCE` rather than overwriting it
with `NULL` when a report omits it.

See [API.md](API.md) for the full field reference.
