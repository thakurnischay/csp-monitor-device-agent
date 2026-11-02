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
| `verified` (inside each of `printer`, `microatm`, `biometric`, `gps`) | bool | no | agent 1.3.1+. `true` = the agent could confirm the device really is that kind of device (RD service answered / GPS data streaming / known brand / identified by the plug-in test / test print accepted). `false` = plugged in with a working driver but identity not confirmed (e.g. a generic "USB Input Device" was picked): the dashboard shows **Unverified** (amber), never OK. Omitted by older agents, which are treated as verified. Optional sibling `verified_by` says how |
| `diagnosis` | object | no | agent 2.0+ (`schema_version` 4). `{printer, microatm, biometric, gps}`, each: `health_state` (HEALTHY, DEGRADED, SOFTWARE_ISSUE, DRIVER_ISSUE, SUSPECTED_HARDWARE, SERVICE_UNAVAILABLE, DIAGNOSIS_REQUIRED, NOT_CONNECTED, UNVERIFIED, NOT_SET_UP), `severity` (critical/high/medium/low/none), `summary`, `findings[]` (`code`, `state`, `severity`, `title`, `symptom`, `root_cause`, `confidence` confirmed/likely/possible, `evidence[]`, `actions[]`, `operator_action`, `auto_fix`), `notes[]`, `checked[]`, `not_checked[]`, `impact`, `last_healthy_at`, `verification`, `actions_taken[]`. Everything is clipped and whitelisted server-side. An issue present in one report and absent from the next is resolved (recovery verified). Devices covered by a diagnosis do not also get the older shallow incidents. Omitted by older agents, which keep the earlier status display |
| `diagnosis.<device>.software` | list | no | agent 2.2+. Facts about the vendor software of that device, one item per program (`key`: `llt`, `usb_drivers` under `microatm`; `gps_driver` under `gps`; `morpho_rd` under `biometric`; a program sent under the wrong device is dropped): `installed`, `version`, `source`, `running` (null for drivers), `expected` (an Ingenico micro-ATM is attached), `crashes_24h`, `crashes_7d`, `last_crash_at`. The server whitelists it, compares it with the administrator's required versions (Software page) and raises `SOFTWARE_MISSING_*` / `SOFTWARE_OUTDATED_*` issues; the agent itself never judges versions |
| `schema_version` | int | no | `4` for agent 2.0+ (`3` adds biometric/GPS,  (`2` = no biometric/GPS) |
| `agent_version` | string | no | e.g. `"1.1.0"` |
| `os` | string | no | e.g. `"Windows-10-10.0.26200-SP0"` |
| `hostname` | string | no | the CSP PC's machine name |
| `capabilities` | object | no | agent 2.0+ with remote updates: `{"remote_update": true, "updater": "1.0"}`. Absent on older agents, which the server treats as *not update-capable* (never targeted) |
| `agent_sequence` | int | no | the agent's release sequence number (rises with every release; used for compatibility and anti-downgrade) |
| `update_result` | object | no | outcome of an update the agent performed and has not yet had acknowledged: `{deployment_id, attempt, outcome: healthy|rolled_back|failed, detail}`. `healthy` + a matching `agent_version` is the only thing that makes a deployment **Successful** |

Any missing/malformed sub-object degrades gracefully (treated as "not
configured") rather than erroring — a buggy or old agent can't crash the
endpoint.

**Responses:**

| Status | Meaning |
|---|---|
| `200` | `{"ok": true, "received_at": "...", "update": {"deployment_id", "attempt", "version", "sequence"}}` — `update` is present **only** when this CSP has a pending, approved deployment |
| `401` | `{"ok": false, "error": "invalid csp_id or API key"}` — wrong/revoked/mismatched key |
| `413` | Payload over 64KB (`MAX_CONTENT_LENGTH`) |
| `429` | `{"ok": false, "error": "too many reports, slow down"}` — more than 6 reports/min from this `csp_id` |

**Example:**

```
curl -X POST https://your-server/api/report \
  -H "X-API-Key: <key>" -H "Content-Type: application/json" \
  -d '{"csp_id":"1A850244","printer":{"configured":true,"present":true,"ok":true,"status":"idle (ready)"},"microatm":{"configured":true,"present":true,"ok":true,"status":"OK"}}'
```

## Enrollment endpoints (no admin session; rate-limited per IP and per CSP code)

| Endpoint | Auth | Body | Reply |
|---|---|---|---|
| `POST /api/enroll/verify` | none | `{csp_id, name}` | `{ok, verdict, message}`; verdict `ok` / `unknown_code` / `name_mismatch` / `no_name_on_record` / `invalid`. `400` unless ok. Verification alone grants nothing |
| `POST /api/enroll` | `X-API-Key: <key the agent generated>` | `{csp_id, name, hostname, machine_id, agent_version}` | `{ok, status: pending|approved, request_id}`; `400` invalid/unknown/mismatch, `409` key already used by another CSP, `429` busy. Idempotent per key. The server stores only the key's SHA-256 |
| `POST /api/enroll/status` | same key | `{csp_id}` | `{ok, status: pending|approved|rejected|superseded|unknown}` |

A key from a *pending* enrollment cannot call `/api/report` (401) until approved.

## Update endpoints (agent, `X-API-Key` of the CSP; manifest/package answer `409` with `proceed:false` when the deployment was cancelled/paused/revoked)

| Endpoint | Purpose |
|---|---|
| `GET /api/update/manifest?csp_id=&deployment_id=` | `{ok, attempt, manifest_b64, signature_b64}` — the exact signed manifest bytes (base64) and its Ed25519 signature |
| `GET /api/update/package?csp_id=&deployment_id=` | the release zip; supports HTTP `Range` (resume); header `X-Package-SHA256` |
| `POST /api/update/status` | `{csp_id, deployment_id, attempt, status: downloading|verifying|installing|restarting|failed, detail}` → `{ok, proceed}`; `proceed:false` tells the agent to stop. `successful` can **not** be reported this way |

A CSP can only touch its **own** deployments. All three are rate-limited (90/min per CSP).

## Admin routes added (session + CSRF, audited)

`/software` (page; save a required version), `/updates` (page), `/updates/upload`, `/updates/action` (approve/revoke release; create/expand/pause/resume/cancel rollout; retry/cancel deployment; create/delete group), `/enrollments` (approve / reject / approve matching / import registry / pre-approve).

## Email Campaigns endpoints

Admin session + CSRF on every POST, like the rest of the dashboard; unauthenticated JSON calls get `401`. Full list, the public tracking / unsubscribe links and the
provider events endpoint (`POST /api/email-campaigns/events`, header `X-Campaign-Events-Key`, `404` until `CAMPAIGN_EVENTS_KEY` is set) are in `EMAIL_CAMPAIGNS.md` section 6.

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
