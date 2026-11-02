# Device Sales: the CSP Growth Platform, merged into the dashboard

The CSP Growth Platform (the Micro ATM / Passbook Printer sales programme: targeting, the 8-touch email drip, the
self-serve order page and the order lifecycle) now runs **inside this dashboard**, at `/growth`:

| What | Address (production) | Who can open it |
|---|---|---|
| Device Sales dashboard (sidebar: **Device Sales**) | `https://indev.eko.in/csp-device-heartbeat/growth/` | Console admins (same login) |
| Order page | `https://indev.eko.in/csp-device-heartbeat/growth/order.html` | Public (CSPs) |
| Email links | `…/growth/go/<token>`, `…/growth/pixel/<token>.png` | Public |

One app, one login, one Docker image, one domain. The Growth Platform's own code is in `server/growth_platform/`
(ported from `csp-growth-platform` at commit `1352030` plus its uncommitted working-tree changes as of 2 Nov 2026).

## What changed compared with the separate app

* **Login:** the console's admin login replaces `DASHBOARD_PASSWORD`. Dashboard POSTs carry the console's CSRF token.
* **Separate live switch:** the drip goes live with `GROWTH_EMAIL_MODE=live`, *not* `EMAIL_MODE` (that one belongs to
  the festival Email Campaigns). Turning one live never turns the other live. SMTP settings are shared.
* **Links:** every new email links to `<CAMPAIGN_PUBLIC_BASE_URL>/growth` (override with `GROWTH_PUBLIC_BASE_URL`).
* **Not merged:**
  * WhatsApp: no routes or scheduler, and the module is pinned to dry mode.
  * Courier-tracking browser automation (Playwright/EasyOCR): orders are dispatched by hand with a tracking number,
    and the auto-deliver timer still works.
* **Background jobs** (order timers, daily Calling Sheet refresh, scheduled drip batches) run only with
  `CSP_MONITOR_GROWTH_WORKER=1`.
* **Storage is unchanged:** the same `csps.json` and `state.json` files (orders, outreach history, tokens), kept in
  `GROWTH_DATA_DIR`. Moving them into SQLite is a separate, later change.

## Settings (`.env` on the server; see `server/campaigns.env.example`)

```
GROWTH_EMAIL_MODE=dry              # live = the drip really sends (also needs SMTP_* set)
GROWTH_EMAIL_MAX_PER_RUN=25
GROWTH_DATA_DIR=/data/growth       # on the existing named volume, next to monitor.db
ORDER_PAGE_LIVE=true               # email buttons open the order page, not the old Google Form
CSP_MONITOR_GROWTH_WORKER=1        # order timers + daily sheet refresh + scheduled drip batches
# GROWTH_PUBLIC_BASE_URL=          # only if /growth is served from a different address
# ORDER_PAGE_SHEET_MIRROR_URL=     # Apps Script URL that mirrors orders into finance's sheet
```

With `CSP_MONITOR_GROWTH_WORKER=1`, `OUTREACH_SCHEDULE_ENABLED = True` (in `growth_platform/core/config.py`) means drip
batches run every day from 10:00. They only *compose* until `GROWTH_EMAIL_MODE=live`.

## Moving to production (in this order)

1. **Back up** the live Growth Platform's `backend/data/csps.json` and `backend/data/state.json` from the server it
   runs on today (`orders.kiosk.eko.in`). The copy on a laptop is out of date: orders and clicks keep arriving.
2. **Stop the old Growth Platform app**, so nothing writes to `state.json` while it is being moved.
3. **Copy both files** into the dashboard's data volume at `/data/growth/` (the `GROWTH_DATA_DIR` folder).
4. Add the settings above to the dashboard's `.env`, rebuild the image (adds `pandas` and `openpyxl` for the sheet
   import) and restart. Leave `GROWTH_EMAIL_MODE=dry` for now.
5. **Check:**
   * Open **Device Sales** and confirm the CSP count, orders and messages match the old dashboard.
   * Open the order page from a phone that is not on the office network.
6. **Keep the old email links working.** Emails and WhatsApp messages already sent (about 860) link to the *old*
   addresses: `http://122.176.147.78:8080/csp-growth-platform/...` and
   `https://orders.kiosk.eko.in:8443/csp-growth-platform/...`. On the server or proxy behind those addresses,
   replace the old app with a permanent redirect, so the old tokens keep working:

   ```nginx
   location /csp-growth-platform/ {
       rewrite ^/csp-growth-platform/(.*)$ https://indev.eko.in/csp-device-heartbeat/growth/$1 permanent;
   }
   ```

   Test it with one old link (`…/csp-growth-platform/go/<a real token>` should land on the new order page).
7. Only after all of the above: switch `GROWTH_EMAIL_MODE=live` (SPF/DKIM done first), starting with a small
   `GROWTH_EMAIL_MAX_PER_RUN`.

## Rollback

Deploy the previous image. The Growth data is untouched files in `/data/growth/`, so you can copy them back to the
old app and remove the redirect.

## Tests

`server/tests/test_growth_platform.py` covers:

* login, CSRF, and which routes are public;
* the separate live switch;
* that WhatsApp and courier tracking are absent;
* the link addresses.

It uses synthetic operators and never contacts a mail server.
