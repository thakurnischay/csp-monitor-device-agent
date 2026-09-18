# Agent Installation

## What gets sent to a CSP

A single zip containing everything in `agent/`: `agent.py`, `config_store.py`,
`device_health.py`, `local_ui.py`, `reporter.py`, `singleton.py`, `version.py`,
`requirements.txt`, `install_task.ps1`, `uninstall_task.ps1`, `SETUP.bat`,
`INSTALL_FOR_CSP.txt`.

## Install (first time)

1. **Extract the zip fully first** — right-click → "Extract All...". Do
   **not** run `SETUP.bat` directly from inside a zip-viewer window; it
   can't find its sibling files that way and fails with a confusing error.
   `SETUP.bat` now detects this specific case and shows a clear message
   instead.
2. Open the extracted folder and double-click `SETUP.bat`.
3. It will, in order:
   - Check for Python; download and silently install it if missing (a
     one-time, few-minutes step) — detects a fake Store-redirect
     `python.exe` stub rather than trusting `where python`.
   - Install `requirements.txt` (just `requests` — deliberately minimal).
   - Register the agent to start automatically at Windows logon (via the
     user's own Startup folder, not Task Scheduler — `Register-
     ScheduledTask` was found to fail with "Access is denied" on a real
     test machine even for a non-admin account).
   - Start the agent immediately and open `http://127.0.0.1:5057`.
4. On that page: enter the **Server URL**, **CSP Code**, and **API key**
   (issued from the dashboard's API Keys page). Devices usually fill in
   automatically — see [ARCHITECTURE.md](ARCHITECTURE.md#device-detection-device_healthpy).

## Reinstall / update (a CSP already has an older version)

Same steps as a first install — extract the new zip, run `SETUP.bat` again.
It's idempotent and safe to re-run:

- `install_task.ps1` kills any currently-running `agent.py` process before
  starting the new one (so an old and new copy can never both be alive,
  fighting over the same config file).
- The existing `agent_config.json` (server URL, CSP code, API key, device
  selection) is untouched by the zip — it lives in the agent folder and
  isn't overwritten, so a reinstall doesn't require re-entering any of it,
  **provided the new zip is extracted into the same folder** as before. If
  it's extracted into a brand-new folder, that setup information is lost
  and needs re-entering once.
- `singleton.py`'s PID lock also prevents two copies from running even if
  someone launches `agent.py` by hand instead of going through the
  installer.

## Uninstall

Run `uninstall_task.ps1` (in the same folder):

```
powershell -ExecutionPolicy Bypass -File uninstall_task.ps1
```

This removes the Startup-folder entry and stops the running process. The
extracted folder (and `agent_config.json`) can then be deleted manually if
desired.

## Known Windows-environment failure modes

| Symptom | Cause | Fix |
|---|---|---|
| Setup fails, cryptic `Rar$...` path | Ran from inside the zip, not extracted | Extract first |
| "Python not found" even after install | Fake Store-redirect `python.exe` on PATH | `SETUP.bat` already handles this — runs `python -c "import sys"`, doesn't trust `where` |
| Local setup page hangs on "checking..." | Smart App Control / antivirus blocking scans | See [TROUBLESHOOTING.md](TROUBLESHOOTING.md) |
| Device dropdown empty even for a real device | It's a COM-port-only device or an unrecognized brand | Type the name manually — the field is a combo box, not a rigid dropdown |

## Signed updates

There is currently **no auto-update mechanism** for the agent, and
`SETUP.bat`/`install_task.ps1` are not code-signed. This is a known,
documented gap (Phase 17 of the original hardening plan) — a future signed-
update mechanism should be designed before adding automatic updates, not
retrofitted onto an unsigned distribution flow.
