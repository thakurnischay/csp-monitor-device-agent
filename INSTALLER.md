# Installer and enrollment

## For the administrator

1. Make sure the CSP exists in the registry (dashboard → **Enrollments** → import `code,name[,pre_approved]`, or the CSP already has an API key).
2. Build the one generic installer (no credentials inside, same zip for everyone):
   ```
   python tools/make_installer_zip.py --server-url https://your-server --out tools/dist
   ```
   It contains the server address and the **public** release key only. It refuses to include any config/history/state file.
3. Send `CSP-Device-Monitor-Setup.zip` to the CSP.
4. When the CSP finishes, the new PC appears under **Enrollments** as *pending*. Check the name/code and **Approve**
   (or tick *pre-approve* for that CSP beforehand). If the CSP already had an agent, a new PC is shown as a **replacement**
   and needs an explicit confirmation — it never silently overwrites the existing one.

## What the CSP sees (5 screens — previews in `docs/installer_screens/`)

1. **Welcome** — Continue.
2. **Register** — CSP Name + Unique CSP Code (both required) → *Verify and Continue*. Plain errors: "unknown code", "name does not match", "check the internet connection". No API key, URL or technical question is ever asked.
3. **Select devices** — automatic scan; Printer / Micro-ATM / Biometric Scanner / GPS dropdowns showing make/model, **Rescan**, **Device Not Found** (simple troubleshooting), **Skip**. A generic "USB Input Device" is **never** pre-selected or auto-identified; choosing one shows a warning and it is saved as *not verified*. The choice is saved in the same `agent_config.json` fields the agent already uses (hardware identity tracking preserved).
4. **Installing** — progress bar with human messages: space/permission check, components, copy, save choices, register, start-with-Windows, start, first health check. Failures show a plain message and **Try again**; cancelling leaves nothing half-installed.
5. **Complete** — CSP name/code, connection status, version, each device with *Verified / Not verified / Skipped*, Finish, optional "open local page". Secrets are never shown.

## How enrollment stays secure

* A CSP code and name are **not secrets**, so they never authenticate anything alone.
* The agent generates its **own** random key, saves it locally, and sends it only in a header; the server stores only its **SHA-256 hash**.
* The request is *pending* until an administrator approves (or the CSP was pre-approved). Until then the key cannot send heartbeats (401).
* Repeating the request is idempotent (no duplicate records); a different PC for an existing CSP is held for confirmation.
* Rate limits apply per IP and per CSP code.

## Existing installation

Running the installer on a PC that already has the agent (found through its Startup launcher) **keeps** `agent_config.json` (CSP id, API key, devices, confirmed hardware), history and state, refuses if the entered code belongs to a *different* CSP, and does not enroll again.

## Install location and startup

`%LOCALAPPDATA%\CSPDeviceMonitor\agent` (or the existing install folder). Starts with Windows through a per-user Startup launcher (no administrator rights, no Windows service). Python is installed automatically if missing; packages come from the bundled `wheels/` first, then the internet.

## Tests

`agent/tests/test_installer_core.py` (registration rules, generic-USB rule, skipped devices, identity preservation, wrong-CSP refusal, low disk, cancel, no-start, rejected registration) and `tools/test_installer_zip.py` (no secrets in zip, every imported module shipped in both zip types). The wizard itself was exercised with `python agent/installer/wizard.py --selftest` and `--screenshots` (sample data, no network, nothing installed).
