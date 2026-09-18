# Troubleshooting

Real problems hit in the field on this project, and how they were actually
diagnosed and fixed — not hypothetical scenarios.

## Agent-side (on a CSP's PC)

### "Package install failed" / Python not found, even though Python looks installed

Windows ships a fake `python.exe` on `PATH` that just redirects to the
Microsoft Store — `where python` finds it and wrongly concludes Python is
present. `SETUP.bat` actually **runs** `python -c "import sys"` to test for
real, not just checks if the command exists.

### Setup fails with a path containing `Rar$DIa...` or similar

`SETUP.bat` was double-clicked directly from inside a zip-viewer (WinRAR/7-
Zip) without extracting first — Windows lets you open files from inside an
archive, but relative paths to sibling files then resolve to a temp
extraction folder that disappears. `SETUP.bat` now checks for this
explicitly and shows a clear "extract the zip first" message instead of a
cryptic path error.

### `install_task.ps1` fails with "missing terminator" or similar parse error

Non-ASCII characters (em-dashes, curly quotes) in a `.ps1` file can break
under Windows PowerShell 5.1's default (non-UTF8) file reading. Any `.ps1`
file in this project is kept plain-ASCII for this reason — `.py` files
don't have this problem (Python 3 source is UTF-8 by default regardless of
platform).

### The local setup page (`127.0.0.1:5057`) is stuck on "checking..." forever, dropdown shows no options at all

Two distinct real causes, both now mitigated:

1. **The scan itself is being blocked** — Windows Smart App Control or
   antivirus silently blocking the spawned `powershell.exe`/`rundll32.exe`
   processes. The page now shows this explicitly: *"scan timed out — a
   security tool on this PC may be blocking the check"* within ~18 seconds,
   instead of hanging forever. The only real fix is disabling Smart App
   Control (Windows Security → App & browser control) — **note: on some
   Windows versions this cannot be re-enabled without a full reset**, warn
   the CSP before doing this.
2. **A JavaScript error killed the whole page silently** — a real bug
   (fixed) where a Python string-escaping mistake produced literally invalid
   JavaScript, and any browser (old or new) that hit it just... did nothing,
   with no visible error. If this page is ever unresponsive with no error
   message at all, check the browser's own dev console (F12 → Console) for
   a `SyntaxError` first, before assuming it's a device-detection problem.

Either way: even when the scan can't run at all, the printer/micro-ATM
fields are combo boxes, not rigid dropdowns — you can always type the exact
device name manually.

### A device is picked correctly but the dashboard still shows the wrong status

The recognition engine **silently overrides a manual pick** whenever it
confidently recognizes a different, real, connected device by brand — this
is intentional (explicitly requested and approved), not a bug. If you type
in a *deliberately wrong* value to test what happens, and the real device is
a recognized brand, it'll still report the real device's actual status,
not your wrong entry. To see a genuine "not found" state, either physically
disconnect the device, or use a brand not in the known-brands list.

### A printer never shows up in the dropdown at all, even after the fix that adds COM-port scanning

Some passbook printers (this project confirmed it with a real
SBI/Ingenico-adjacent install) genuinely never register as a Windows
printer OR a recognizable device name — they may show up under a completely
unexpected PnP class. Get the raw diagnostic from that PC:

```
Get-CimInstance -ClassName Win32_Printer | Select-Object Name, PrinterStatus, WorkOffline | Format-List
Get-PnpDevice -PresentOnly | Select-Object FriendlyName, Class, Status | Format-Table -AutoSize
```

If the device genuinely doesn't appear in either list, Windows itself
doesn't see it — check the physical USB connection and driver installation
before assuming it's an agent bug.

### Two copies of the agent seem to be running / device selection doesn't stick

Each `agent.py` process has its own `agent_config.json` — running two means
a device picked in one copy's setup page never reaches the copy that's
actually reporting. `install_task.ps1` kills any existing `agent.py`
process before starting a new one; `singleton.py` (a PID lock file) also
now prevents this at the code level even if launched by hand, not just via
the installer.

## Server-side

### The dashboard is unreachable, but the container is running and healthy locally

Check whether **another process on the shared server** has taken the same
public port at the OS level — `sudo ss -tlnp | grep :<port>`. This
happened for real: an unrelated `nginx` instance serving a different
project grabbed the same port Tailscale Funnel needed, and Tailscale's own
listener silently failed to bind (visible in `journalctl -u tailscaled` as
`bind: address already in use`). The app itself was fine the whole time —
`curl http://127.0.0.1:<port>/health` locally on the server confirms this
quickly, before assuming the app is broken.

### The dashboard shows completely empty / "No CSP has reported yet" after a redeploy

Almost always means: fresh code, empty database — very common when a
deployment is recreated from `git clone` alone, since `monitor.db` is
deliberately excluded from version control (it holds live API key hashes
and would defeat the point of hashing them). Restore from the most recent
backup — see [OPERATIONS.md](OPERATIONS.md).

### `docker cp <container>:... ` says "No such container"

The container itself, not just its data, was removed (e.g. as part of
unrelated infrastructure work). Check `docker ps -a` first — if it's truly
gone, rebuild from source + restore the database backup rather than trying
to recover the old container.

## General debugging tips that came up repeatedly

- **Confirm which machine a command is actually running on.** The single
  most common mistake throughout this project's operational history was
  pasting a command meant for the remote server into a local PowerShell
  window (or vice versa) — `docker`/`tailscale`/`grep` "not recognized" is
  the telltale sign you're in the wrong shell.
- **A multi-line SSH paste can break on an interactive prompt** (e.g. a
  `sudo` password prompt appearing mid-block) — the remaining lines get fed
  to that prompt as garbage input instead of executing. Run commands that
  need `sudo` one at a time when in doubt.
