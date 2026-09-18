# Security

## What's protected, and how

| Concern | Protection | Where |
|---|---|---|
| API keys at rest | SHA-256 hash only; plaintext shown once at issue time, never stored or logged again | `security.py: hash_api_key/verify_api_key` |
| CSP identity spoofing | A key only authenticates the exact `csp_id` it was issued for — a client-claimed `csp_id` with someone else's key is rejected | `api.py: _valid_key` |
| Admin password | Werkzeug's `generate_password_hash`/`check_password_hash` (salted) | `auth.py` |
| CSRF | Per-session token, required on every admin-UI POST form; the agent-facing `/api/report` is exempt (header-key auth, not cookies, isn't a CSRF target) | `security.py: get_csrf_token/verify_csrf`, enforced in `app.py`'s `before_request` |
| Session cookies | `HttpOnly`, `SameSite=Lax`, `Secure` when `CSP_MONITOR_FORCE_HTTPS=1` | `app.py` |
| Session-signing key | Random per-process if `CSP_MONITOR_SECRET_KEY` unset (never a fixed, source-controlled string) | `app.py` |
| Brute-force login | 8 attempts/minute per IP | `security.py: login_limiter` |
| API abuse | 6 reports/minute per `csp_id` | `security.py: report_limiter` |
| Oversized requests | 64KB cap (`MAX_CONTENT_LENGTH`) — Flask/Werkzeug reject before any view code runs | `app.py` |
| XSS / clickjacking | `Content-Security-Policy` (`script-src 'self'`, no inline scripts anywhere in the templates), `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff` | `app.py` |
| Malformed/wrong-type payloads | Every field access checks `isinstance(..., dict)` before use — never trusts the agent to send well-formed JSON | `api.py: _bool/_text` |
| Admin actions | Every login, key issue/rotate/revoke/delete is recorded (actor, action, target, timestamp) — never the password or full key | `events.py: record_audit`, `/audit-log` |

## Known, deliberate limitations

- **No RBAC** — one shared admin login, no per-user accounts or roles. Fine
  for a small internal ops team; would need real work before opening this to
  a larger group.
- **No signed/verified agent auto-update** — the agent has no self-update
  mechanism at all (deliberately: an insecure auto-updater is worse than
  none). New agent versions are distributed as a zip + `SETUP.bat`, run
  manually.
- **CORS is simply not configured** — there's no cross-origin API use case
  today, so no `Access-Control-Allow-Origin` header is ever set, which means
  browsers deny cross-origin reads by default. If a legitimate cross-origin
  consumer is ever needed, add an explicit, narrow allow-list then — don't
  default to `*`.
- **`admin`/`admin123` is still the bootstrapped default** on any brand-new
  database — change it immediately after first login (see
  [OPERATIONS.md](OPERATIONS.md) for the reset procedure if the UI doesn't
  have a change-password flow yet).

## Security checklist (before calling a deployment production-ready)

- [ ] `CSP_MONITOR_SECRET_KEY` set to a real random value (not left to the
      random-per-process fallback, so sessions survive a restart)
- [ ] `CSP_MONITOR_FORCE_HTTPS=1` set once genuinely behind TLS
- [ ] Default admin password changed from `admin123`
- [ ] `monitor.db` is NOT committed to git (already `.gitignore`d) and is
      backed up somewhere the admin can actually reach in an emergency
- [ ] The real Dockerfile with any baked-in secrets is kept OUT of any
      shared/public repo — the version control copy should reference
      `CSP_MONITOR_SECRET_KEY` via environment, never a hardcoded value
- [ ] HTTPS is actually terminated somewhere in the request path (Tailscale
      Funnel or a reverse proxy) — this app itself doesn't terminate TLS
- [ ] Server logs (`server.log`) are periodically reviewed for repeated
      `WARNING`-level auth failures (possible brute-force attempts)

## Reporting a concern

This is an internal Eko tool; raise anything found directly with whoever
currently owns the deployment rather than filing it publicly.
