# Email Campaigns — festival and occasion emails inside the device-monitor dashboard

A new **Email Campaigns** tab in the existing dashboard. It lets an administrator create festival / occasion email campaigns for
kiosk operators who **confirmedly lack** a device, review exactly who would receive them and why, rehearse with a dry run, launch
with an explicit confirmation, and then see — per operator — what really happened to every email.

Nothing else in the dashboard or the monitoring agents changed behaviour. Agents never send email and never depend on this feature.

---

## 1. Architecture (what was chosen and why)

| Question | Decision | Why |
|---|---|---|
| Separate service or inside the dashboard? | **Inside the dashboard's backend** (`server/campaigns/`) | One Flask + SQLite app, one login, one deployment. No second public dashboard. |
| Reuse the CSP Growth Platform's code? | **Yes, vendored unchanged** in `server/campaigns/growth/`: `engine.py` (ownership rules), `emailer.py` (SMTP connection, validation, palette) and the business-rule part of `config.py` | Ownership verdicts are *identical* to the sales project's. `SOURCE.txt` records the originals' SHA-256. Verified against the real data: 0 differences in 535 records. |
| Where do campaign records live? | **The dashboard's SQLite**, new tables only (additive) | The email project keeps one JSON file behind an in-process lock held for minutes during sends: unsuitable for per-recipient history. |
| Operator data | **Imported snapshot** of the email project's `csps.json` (+ optional `state.json` human decisions), keyed by CSP code | The email project stays the authoritative owner of operator data. Nothing is ever written back. Re-importing never touches campaign history. |
| Sending | **Background worker inside the dashboard** (opt-in), capped batches | Reliable (state is in the database, restart-safe), not a browser timer. |
| Agents | Untouched | They never learn about email. |

Campaign records, send attempts and provider events are three separate record types, so a retry never erases history.

## 2. Shared operator identity

The stable key is the **CSP code**, upper-cased with spaces removed (`normalize_code`). It is the same value the monitoring agents use as
`csp_id`. Names and emails are never used as identity. A code that appears twice in one import file is treated as ambiguous and skipped.

## 3. Who is eligible — and why (`campaigns/eligibility.py`)

An operator is emailed only if, in order: (1) they are in the latest operator data; (2) they are **active** (the email project's own
ACTIVE rule; "dormant" operators are an opt-in checkbox); (3) they have a **valid email**; (4) the address is **not opted out or bounced**;
(5) they **confirmedly lack** the selected device(s); (6) no other operator in the same campaign already uses that address.
Every operator gets a recorded **reason** and the list of checks behind it, in one of three buckets: **eligible**, **review**, **excluded**.

### Device ownership (`campaigns/operators.py`)

Four verdicts per operator per device, exactly as in the email project: `OWNED`, `GAP_VERIFIED`, `GAP_DISPUTED`, `UNKNOWN`.
**Only `GAP_VERIFIED` counts as "does not own".** Disputed or unknown goes to the **review** list and is never emailed.

| Device | Source of the verdict |
|---|---|
| Micro-ATM, Printer | The email project's engine (claims + transaction evidence), human decisions win |
| GPS dongle | The sheet's `claimsGps` column (a claim only: nothing to cross-check it with) |
| Fingerprint scanner | **No source exists in the sheet.** Unknown unless the monitoring agent has it configured or a person records a decision |
| All | The **monitoring agent** is a second witness: a device it has *configured* turns "unknown" into "owned" and contradicts a "no" (-> *disputed*, review). A **human decision** (Operators page, or imported from `state.json`) beats everything |

`audience_match`: **any** (lacks at least one selected device — default) or **all**. An operator who lacks one device is never assumed
to lack the others; the email names only devices confirmed missing for that operator.

This audience is separate from the sales campaign's: it uses its own tables and never reads or writes the sales project's outreach state.

## 4. Data model (all new, additive — `server/schema.sql`)

`operator_imports`, `campaign_operators`, `operator_devices`, `operator_reconciliations`, `email_campaigns`, `email_campaign_recipients`
(UNIQUE `campaign_id + csp_code` = the duplicate guard), `email_attempts`, `email_events`, `email_suppressions`.

A recipient row keeps the address, name, RM and location **as they were when sent**, the reason, the checks, the rendered email, the
Message-ID, and counters. Status values (only ones the implementation can substantiate):

* message `status`: `pending`, `queued`, `sending`, `accepted` (**the mail server took it**), `failed`, `unknown_outcome`, `cancelled`, `not_sent`
* `delivery_status`: `not_confirmed` (default), `delivered`, `bounced`, `complained` — **set only by provider events**, never by SMTP acceptance
* campaign: `draft`, `scheduled`, `queued`, `sending`, `paused`, `completed`, `cancelled`

## 5. The tab

* **Summary cards** from real rows: eligible operators, emails accepted by the mail server, delivered (confirmed), failed, pending/queued, campaigns,
  unique operators contacted, opened / clicked (signals). *Accepted, delivered, opened and clicked are four different things and are never merged.*
* **Campaigns table**, **history table** (CSP code, operator, masked email, location, RM, device ownership, occasion, campaign, subject, sent,
  status, opened, clicked, failure reason, updated) with search, filters (occasion, campaign, device not owned, status, dates, state, RM),
  reset, refresh and server-side pagination.
* **Message page**: recipient, campaign, full address, the rendered email (sandboxed, no images loaded — viewing never counts as an open),
  Message-ID, delivery / open / click events, failure information, every attempt, and the eligibility checks.
* **Campaign page**: audience in three tabs with a reason per operator, personalised preview, dry run, test email, launch / schedule,
  pause / resume / cancel / retry, progress, and the configuration recorded at launch.
* **Operators & opt-outs**: import the operator file, ownership summary, record a human decision, manage opt-outs.

Email addresses are masked (`a***@gmail.com`) in tables; the full address is shown only on the message page.

## 6. API (all need the admin session; unauthenticated calls get 401 JSON; every POST needs the CSRF token like the rest of the dashboard)

`GET /api/email-campaigns/summary` · `/campaigns` · `/messages` (filters, `page`, `per_page`) · `/messages/<id>` · `/<id>/audience?bucket=` · `/<id>/preview?csp_code=` · `/<id>/progress`
`POST /api/email-campaigns/<id>/<action>` with `action` = `refresh | dry_run | test | launch | pause | resume | cancel | retry | retry_unknown`
Public (no login): `GET /c/o/<token>.gif`, `GET /c/l/<token>`, `GET|POST /c/u/<token>`, and `POST /api/email-campaigns/events` (needs `X-Campaign-Events-Key`; **404 until `CAMPAIGN_EVENTS_KEY` is set**).

## 7. Safety model (each item has a test)

* **Dry run is the default.** A dry run never opens a connection and never changes a recipient; it only records "would send".
* **Live needs ALL of:** `EMAIL_MODE=live`, `CAMPAIGNS_LIVE_ENABLED=1`, `CAMPAIGN_PUBLIC_BASE_URL` (https), `SMTP_HOST`, **and** the person typing the exact recipient count at launch. The worker re-checks the switches on every batch and pauses itself if they go off.
* **One message per operator per campaign**: a database constraint plus an atomic claim (`queued -> sending`, committed *before* the network call).
* **Never resend an accepted message.** Retry touches only failures; permanent failures (refused address) are not retried; transient ones are retried with back-off up to `CAMPAIGN_MAX_ATTEMPTS`, then recorded as exhausted. Each attempt is its own row.
* **Restart-safe:** a message caught mid-send becomes `unknown_outcome` (needs a human; never auto-resent). A scheduled campaign starts exactly once even if two workers run.
* **Sending limits:** `EMAIL_MAX_PER_RUN` per batch, `CAMPAIGN_SEND_DELAY_SECONDS` between messages.
* **Opt-outs:** every email has an unsubscribe link and RFC 8058 one-click headers. A GET never unsubscribes (link scanners prefetch). An opted-out or hard-bounced address is never emailed again, checked again right before each send.
* **No unsafe HTML:** templates are plain text with a fixed placeholder list; everything is escaped; subjects cannot inject headers; links must be `https`.
* **Credentials** stay in the server's environment and are never shown, logged or stored. Audit entries use masked addresses.
* **Audit:** who created / edited / dry-ran / launched / paused / cancelled, the exact launch configuration, imports, opt-outs and human decisions.
* **Tracking endpoints** use unguessable per-message tokens, reveal nothing for unknown tokens, are rate-limited, and the click redirect can only go to the campaign's own stored link.

## 8. Configuration (placeholders only — never commit real values)

See `server/campaigns.env.example`. In the dashboard's `.env` / compose environment:

| Variable | Purpose |
|---|---|
| `EMAIL_MODE` | `dry` (default) or `live` |
| `CAMPAIGNS_LIVE_ENABLED` | `1` to allow live sending (second switch) |
| `CAMPAIGN_PUBLIC_BASE_URL` | The dashboard's public https address *including any path prefix*, e.g. `https://indev.eko.in/csp-device-heartbeat`. Used for unsubscribe / open / click links |
| `EMAIL_FROM`, `EMAIL_REPLY_TO`, `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASS`, `SMTP_SECURE` | Same names and meaning as the CSP Growth Platform |
| `EMAIL_MAX_PER_RUN` (25), `CAMPAIGN_SEND_DELAY_SECONDS` (1), `CAMPAIGN_MAX_ATTEMPTS` (3), `CAMPAIGN_RETRY_MINUTES` (15) | Limits |
| `CSP_MONITOR_CAMPAIGN_WORKER` | `1` to start the background sender (off by default); `CAMPAIGN_TICK_SECONDS` (60) |
| `CAMPAIGN_EVENTS_KEY` | A long random secret enabling the provider events endpoint |

### Turning live sending on safely
1. **Authenticate the sender domain** (SPF, DKIM, DMARC) for the address in `EMAIL_FROM` — the email project's README describes the `csp.eko.co.in` setup.
2. Set the SMTP variables and `CAMPAIGN_PUBLIC_BASE_URL`; restart. Open the tab: the banner lists anything still missing.
3. Import the operator file; review **Operators & opt-outs** (ownership table, matched monitored CSPs).
4. Create a campaign, review all three audience tabs, preview, **dry run**, and **send a test** to yourself.
5. Set `EMAIL_MODE=live` and `CAMPAIGNS_LIVE_ENABLED=1`, restart, launch with a **small first batch** (e.g. `EMAIL_MAX_PER_RUN=5`), check the result, then raise the cap.
6. For real delivery / bounce data, point your provider's webhook (or a bounce processor) at `POST …/api/email-campaigns/events` with `X-Campaign-Events-Key` and events `{message_id, event: delivered|bounced|complained, type: hard|soft, id, timestamp}`.

## 9. Install, run, test

```
cd server
pip install -r requirements.txt
python app.py                                  # dev server on :5100 (dry mode, worker off)
python -m unittest discover -s tests -t .      # the server suite
```
Production uses the existing Docker / compose setup unchanged (`CSP_MONITOR_CAMPAIGN_WORKER=1` in `.env` to enable background sending).
Behind nginx under a path prefix, nothing extra is needed beyond the `X-Forwarded-Prefix` header already documented for the dashboard; public links use `CAMPAIGN_PUBLIC_BASE_URL`.

## 10. Migration and rollback

* **Migration:** automatic at start-up, additive (`CREATE TABLE IF NOT EXISTS`). No existing table or row is changed. Back up `monitor.db` first.
* **Rollback:** deploy the previous image. The new tables are harmless to the old code. To remove them entirely (**destroys campaign history**), run `server/campaigns/rollback.sql` after taking a backup.

## 11. What the data and the tooling can and cannot tell you

* **Accepted ≠ delivered.** Plain SMTP only says the server took the message. "Delivered" and "bounced" appear **only** once an email provider or bounce processor posts events. Until then every sent message honestly reads *Accepted by mail server*.
* **Opens are a signal, not a fact:** a 1×1 image. Apple Mail Privacy Protection loads images for every message (inflating opens) and clients that block images hide real opens. **Clicks** can also be inflated by security scanners.
* The Message-ID shown is the one *we* attach; it is not a provider receipt.

## 12. Known limitations

* **Fingerprint-scanner and (mostly) GPS audiences are thin or empty**: no source records biometric ownership, and the sheet's GPS column is blank for 52 of 535 operators. Those operators land in *review*. Record decisions on the Operators page, or let monitoring agents configure the device.
* **Operator data is a manual import** (upload `csps.json`). There is no live link to the CSP Growth Platform; re-import after the sheet refreshes.
* **136 of the operators who lack a micro-ATM have no email address**; they cannot be reached by email.
* **One admin role.** Any dashboard administrator can launch a campaign (guarded by the typed confirmation and the server switches). Per-user roles would be a separate change.
* No unsubscribe *preference centre*, no A/B testing, no per-recipient send-time optimisation, no attachments.
* The background worker assumes one dashboard process (it is also safe with several: every state change is an atomic update).
* Real SMTP delivery, DNS authentication and a provider webhook are **external configuration** and were **not exercised** here: all automated tests use a fake transport, and no email was ever sent.

## 13. A security finding about the supplied email project

The zip contained a real `.env` with a live SMTP password, an API key and another password (and `state.json` / `csps.json` with operator contact data).
None of it was copied into this project. Because those secrets travelled in a zip, **consider rotating them**, and keep that `.env` out of any shared folder or repository (its `.gitignore` does ignore it, but a zip does not).
