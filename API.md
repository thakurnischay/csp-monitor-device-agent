# API Reference

Base URL is whatever the server is reached at (e.g.
`https://openclaw-poweredge-r730.tailb276af.ts.net:8443` in the live
deployment, or `http://127.0.0.1:5100` locally).

## Authentication

Every CSP has its own API key (issued from the admin dashboard's **API
Keys** page). Send it on every request as a header:

```
X-API-Key: <the key>
```

The key is looked up by its SHA-256 hash — the server never stores or logs
the plaintext value after issuing it once. A revoked or unknown key, or a
key that doesn't match the `csp_id` in the body, is rejected with `401`.

## `POST /api/report`

The one endpoint the agent calls. Also usable for manual/scripted testing.

**Headers:** `X-API-Key: <key>`, `Content-Type: application/json`

**Body:**

| Field | Type | Required | Notes |
|---|---|---|---|
| `csp_id` | string | yes | Must match the CSP the API key was issued for |
| `printer` | object | no | `{configured, present, ok, status, resolved_name}` |
| `microatm` | object | no | same shape as `printer` |
| `biometric` | object | no | fingerprint scanner, same shape as `printer` (agent `schema_version` 3+). Omitted by older agents - the server then leaves its stored values untouched and the dashboard shows a dash, not "Not set up" |
| `gps` | object | no | USB GPS dongle, same shape as `printer` (agent `schema_version` 3+), same omission rule as `biometric` |
| `printer_functional_test` | object | no | `{ran, ok, detail}` |
| `schema_version` | int | no | `3` for the current agent (`2` = no biometric/GPS) |
| `agent_version` | string | no | e.g. `"1.1.0"` |
| `os` | string | no | e.g. `"Windows-10-10.0.26200-SP0"` |
| `hostname` | string | no | the CSP PC's machine name |

Any missing/malformed sub-object degrades gracefully (treated as "not
configured") rather than erroring — a buggy or old agent can't crash the
endpoint.

**Responses:**

| Status | Meaning |
|---|---|
| `200` | `{"ok": true, "received_at": "..."}` |
| `401` | `{"ok": false, "error": "invalid csp_id or API key"}` — wrong/revoked/mismatched key |
| `413` | Payload over 64KB (`MAX_CONTENT_LENGTH`) |
| `429` | `{"ok": false, "error": "too many reports, slow down"}` — more than 6 reports/min from this `csp_id` |

**Example:**

```
curl -X POST https://your-server/api/report \
  -H "X-API-Key: <key>" -H "Content-Type: application/json" \
  -d '{"csp_id":"1A850244","printer":{"configured":true,"present":true,"ok":true,"status":"idle (ready)"},"microatm":{"configured":true,"present":true,"ok":true,"status":"OK"}}'
```

## `GET /api/v1`

Unauthenticated. A trivial "is this the right service" ping.

```json
{"ok": true, "service": "csp-device-monitor", "endpoints": ["/api/report (POST)"]}
```

## `GET /health`

Unauthenticated liveness check — the process is up and answering HTTP.
Never touches the database.

```json
{"ok": true, "status": "running"}
```

## `GET /readiness`

Unauthenticated readiness check — liveness AND the database is reachable.
Returns `503` if the database can't be queried.

```json
{"ok": true, "status": "ready", "database": "ok"}
```

## Admin UI routes (session-cookie auth, not API-key)

These are browser routes, not a machine API, but documented for
completeness:

| Route | Method | Purpose |
|---|---|---|
| `/login` | GET, POST | Admin login (CSRF-protected, rate-limited) |
| `/` | GET | Fleet page — `?q=`, `?status=`, `?problems_only=1`, `?sort=`, `?dir=`, `?page=`, `?refresh=` |
| `/fleet.csv` | GET | CSV export, respects the same filter params as `/` |
| `/csp/<csp_id>` | GET | CSP detail — status, recent events, open/past incidents |
| `/api-keys` | GET, POST | Issue/rotate/revoke/delete a CSP's API key |
| `/audit-log` | GET | Last 200 admin actions |

All POST forms require a `csrf_token` field (rendered automatically by
every template via `{{ csrf_token() }}`).
