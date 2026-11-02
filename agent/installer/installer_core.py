"""The installer's engine - everything the wizard does, with no GUI, so it can be tested.

What it guarantees:
  * An EXISTING installation keeps its identity: agent_config.json (CSP id, API key, device choices,
    confirmed hardware identities), diagnostic history and update state are never overwritten.
  * A NEW PC enrolls securely (enroll.py): it makes its own key; nothing is typed or shown; an administrator approves.
  * Ambiguous generic USB entries ("USB Input Device") are never picked automatically as a business device.
  * Failures (permissions, low disk space, no internet) become plain-language messages, never tracebacks.
"""
import json
import logging
import os
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SOURCE_DIR = os.path.dirname(HERE)                  # the folder that holds agent.py etc. (the extracted installer package)
if SOURCE_DIR not in sys.path:
    sys.path.insert(0, SOURCE_DIR)

import apply_update as au                           # noqa: E402
import config_store                                 # noqa: E402
import device_health as dh                          # noqa: E402
import enroll                                       # noqa: E402
import release_manifest as rm                       # noqa: E402

log = logging.getLogger("csp_installer")

CATEGORIES = [
    ("printer", "Printer", "Passbook printer"),
    ("microatm", "Micro-ATM", "Connected Micro-ATM terminal"),
    ("biometric", "Biometric Scanner", "Fingerprint scanner"),
    ("gps", "GPS Device", "GPS dongle, if used"),
]
TITLE = {c: t for c, t, _ in CATEGORIES}
BRANDS = {"printer": dh._KNOWN_PRINTER_BRANDS, "microatm": dh._KNOWN_MICROATM_BRANDS,
          "biometric": dh._KNOWN_BIOMETRIC_BRANDS, "gps": dh._KNOWN_GPS_BRANDS}
CLASSES = {"printer": (), "microatm": (), "biometric": ("Biometric", "HIDClass"), "gps": dh._GPS_CANDIDATE_CLASSES}
GENERIC_HINTS = ("usb input device", "usb composite device", "hid-compliant", "usb device", "usb serial device",
                 "usb-serial", "unknown", "usb mass storage", "i2c hid", "usb serial converter")
SYSTEM_PRINTER_ENTRIES = ("root print queue",)         # Windows plumbing, never a real printer
REQUIRED_FREE_MB = 300
LOCAL_UI = "http://127.0.0.1:5057"
PYTHON_PACKAGES = ("flask", "requests")

TROUBLESHOOTING = {
    "printer": ("1. Make sure the printer is switched ON and its cable is plugged in at both ends.\n"
                "2. Put paper in it. If it uses a USB cable, try another USB port on the computer.\n"
                "3. Click 'Rescan devices'.\n"
                "4. Still missing? Use 'Find my device' below, or skip this device now - you can add it later."),
    "microatm": ("1. Plug the Micro-ATM into a USB port on the computer (not a hub) and switch it on.\n"
                 "2. Wait 10 seconds, then click 'Rescan devices'.\n"
                 "3. Still missing? Use 'Find my device' below, or skip it for now."),
    "biometric": ("1. Plug the fingerprint scanner into a USB port on the computer (not a hub). Its light should come on.\n"
                  "2. Wait 10 seconds, then click 'Rescan devices'.\n"
                  "3. Still missing? Use 'Find my device' below, or skip it for now."),
    "gps": ("1. Plug in the GPS dongle if your CSP uses one. If you do not use a GPS device, simply skip this.\n"
            "2. Wait 10 seconds, then click 'Rescan devices'.\n"
            "3. Still missing? Use 'Find my device' below, or skip it."),
}


class InstallError(Exception):
    """Carries a message written for the CSP operator."""


# --------------------------------------------------------------------------------------------- locations
def load_installer_config(source_dir=SOURCE_DIR):
    try:
        with open(os.path.join(source_dir, "installer_config.json"), "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        raise InstallError("This installer is incomplete: it does not contain the server address. Please ask your administrator for a new installer.")
    url = str(cfg.get("server_url", "")).strip()
    if not url.lower().startswith(("https://", "http://")):
        raise InstallError("This installer has an invalid server address. Please ask your administrator for a new installer.")
    cfg["server_url"] = url
    return cfg


def default_install_dir():
    base = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Local")
    return os.path.join(base, "CSPDeviceMonitor", "agent")


def startup_launcher_path():
    appdata = os.environ.get("APPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Roaming")
    return os.path.join(appdata, "Microsoft", "Windows", "Start Menu", "Programs", "Startup", "CSPDeviceMonitorAgent.bat")


def find_existing_install():
    """The folder of an agent already installed on this PC (found through its Startup launcher), or None."""
    import re
    try:
        with open(startup_launcher_path(), "r", encoding="utf-8", errors="ignore") as f:
            m = re.search(r'"([^"]*agent\.py)"', f.read(), re.I)
        if m and os.path.isfile(m.group(1)):
            return os.path.dirname(m.group(1))
    except OSError:
        pass
    return None


def read_existing_config(agent_dir):
    try:
        with open(os.path.join(agent_dir, "agent_config.json"), "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


# ------------------------------------------------------------------------------------------- registration
def verify_registration(server_url, code, name, session=None):
    code, name = (code or "").strip(), " ".join((name or "").split())
    if not code or not name:
        return {"ok": False, "verdict": "invalid", "message": "Please enter both your CSP name and your CSP code."}
    return enroll.verify(server_url, code, name, session)


# ---------------------------------------------------------------------------------------- device discovery
def _is_generic(name):
    n = (name or "").lower()
    return any(h in n for h in GENERIC_HINTS)


def _candidate(d, category):
    brands = BRANDS[category]
    recognised = dh.is_recognised(d, brands)
    return {"category": category, "name": d["name"], "label": dh.device_label(d), "recognised": recognised,
            "generic": (not recognised) and _is_generic(d["name"]), "instance": d.get("instance", ""),
            "vid": d.get("vid", ""), "pid": d.get("pid", ""), "manufacturer": d.get("manufacturer", ""),
            "windows_ok": bool(d.get("ok", True)), "status": d.get("status", ""), "klass": d.get("class", "")}


def scan_devices(check=None, cfg=None):
    """{category: [candidate, ...]} - the existing, tested discovery, ranked most-likely first. Virtual printers and
    software placeholders are left out. Never raises."""
    cfg = cfg or {}
    try:
        health = (check or (lambda: dh.check("", "", "", "", "")))()
    except Exception:
        health = {}
    lists = {"printer": health.get("printer_list", []), "microatm": health.get("usb_device_list", []),
             "biometric": health.get("biometric_device_list", []), "gps": health.get("gps_device_list", [])}
    out = {}
    for cat, devices in lists.items():
        keep = []
        for d in devices:
            if cat == "printer" and (dh.is_likely_virtual_printer(d["name"]) or d["name"].lower() in SYSTEM_PRINTER_ENTRIES):
                continue
            if cat != "printer" and dh.is_likely_virtual_usb_device(d["name"]):
                continue
            keep.append(d)
        ranked = dh.rank_devices(keep, BRANDS[cat], CLASSES[cat]) if cat != "printer" else sorted(
            keep, key=lambda d: (0 if dh.is_recognised(d, BRANDS[cat]) else 1, d["name"].lower()))
        seen, cands = set(), []
        for d in ranked:
            c = _candidate(d, cat)
            key = (c["name"], c["instance"])
            if key not in seen:
                seen.add(key)
                cands.append(c)
        out[cat] = cands
    out["_scan_problem"] = health.get("scan_problem", "")
    return out


def suggest_defaults(scan, existing_cfg=None):
    """What to pre-select. A previously saved choice that is still present wins; otherwise ONLY a device that is
    recognised by brand AND is the only such device in its category. A generic entry is never pre-selected."""
    existing_cfg = existing_cfg or {}
    out = {}
    for cat, _t, _d in CATEGORIES:
        cands = scan.get(cat, [])
        saved = existing_cfg.get(cat + "_name", "")
        if saved and any(c["name"] == saved for c in cands):
            out[cat] = saved
            continue
        known = [c for c in cands if c["recognised"]]
        out[cat] = known[0]["name"] if len(known) == 1 else None
    return out


def warning_for(candidate, category):
    """A plain warning to show beside a choice that cannot be confirmed (the operator may still continue)."""
    if candidate is None:
        return ""
    if candidate["recognised"]:
        return ""
    what = TITLE[category] if category == "microatm" else TITLE[category].lower()
    if candidate["generic"]:
        return ("Windows lists this as a general USB device, so it cannot be confirmed to be your %s. "
                "You can continue; the monitor will show it as 'not verified'. Use 'Find my device' to confirm it." % what)
    return "This device could not be recognised as a %s automatically. You can continue; it will be shown as 'not verified'." % what


# Find my device: plug-in test (reuses the same idea as the agent's setup page)
def snapshot_devices():
    devs = dh.list_all_devices()
    return {"instances": {d.get("instance") or d["name"] for d in devs}, "printers": {p["name"] for p in dh.list_printers()}, "ok": bool(devs)}


def detect_new(snapshot):
    devs = dh.list_all_devices()
    appeared = []
    for d in devs:
        if (d.get("instance") or d["name"]) not in snapshot["instances"] and not dh.is_likely_virtual_usb_device(d["name"]):
            appeared.append({"kind": "device", "name": d["name"], "instance": d.get("instance", ""), "label": dh.device_label(d)})
    for p in dh.list_printers():
        if p["name"] not in snapshot["printers"] and not dh.is_likely_virtual_printer(p["name"]):
            appeared.append({"kind": "printer", "name": p["name"], "instance": "", "label": p["name"] + " (installed printer)"})
    return appeared


def selection_to_config(selections, confirmed=None):
    """Device choices -> agent_config.json fields. `selections` {cat: candidate-dict or None (skipped)};
    `confirmed` {cat: appeared-entry} for devices identified with the plug-in test (stored exactly as the agent's own
    Find-my-device does, so hardware-identity tracking keeps working)."""
    confirmed = confirmed or {}
    updates = {}
    for cat, _t, _d in CATEGORIES:
        c = selections.get(cat)
        if c is None:
            updates[cat + "_name"] = ""
            continue
        updates[cat + "_name"] = c["name"]
        hit = confirmed.get(cat)
        if hit and hit["name"] == c["name"]:
            updates[cat + "_confirmed"] = c["name"]
            updates[cat + "_instance"] = hit.get("instance", "")
            updates[cat + "_fingerprint"] = dh.fingerprint(c["name"], hit.get("instance", ""))
    return updates


# --------------------------------------------------------------------------------------------- installing
def check_disk_space(path, need_mb=REQUIRED_FREE_MB):
    probe = path
    while probe and not os.path.exists(probe):
        parent = os.path.dirname(probe)
        if parent == probe:
            break
        probe = parent
    free = shutil.disk_usage(probe or ".").free
    if free < need_mb * 1024 * 1024:
        raise InstallError("There is not enough free space on this computer (about %d MB is needed, %d MB is free). "
                           "Please free up some space and try again." % (need_mb, free // (1024 * 1024)))


def check_writable(path):
    try:
        os.makedirs(path, exist_ok=True)
        probe = os.path.join(path, ".write-test")
        with open(probe, "w") as f:
            f.write("ok")
        os.remove(probe)
    except PermissionError:
        raise InstallError("Windows did not allow the installer to save files in %s. Please close the installer and open it again; "
                           "if Windows asks for permission, choose Yes. If it keeps happening, contact your administrator." % path)
    except OSError as e:
        raise InstallError("The installer could not prepare its folder (%s). Please contact your administrator." % e.strerror)


def _importable(name):
    import importlib.util
    return importlib.util.find_spec(name) is not None


def provision_dependencies(source_dir, progress=lambda m: None, run=subprocess.run, importable=_importable):
    """Make sure the Python packages the monitor needs are present: the bundled offline copies first, then the internet."""
    missing = [p for p in PYTHON_PACKAGES if not importable(p)]
    if not missing:
        return "already installed"
    progress("Installing the components the monitor needs...")
    wheels = os.path.join(source_dir, "wheels")
    req = os.path.join(source_dir, "requirements.txt")
    attempts = []
    if os.path.isdir(wheels):
        attempts.append([sys.executable, "-m", "pip", "install", "--quiet", "--disable-pip-version-check", "--no-index",
                         "--find-links", wheels, "-r", req])
    for _ in range(3):
        attempts.append([sys.executable, "-m", "pip", "install", "--quiet", "--disable-pip-version-check",
                         "--default-timeout=120", "--retries", "10", "-r", req])
    for cmd in attempts:
        r = run(cmd, capture_output=True, text=True, timeout=900)
        if r.returncode == 0 and all(importable(p) for p in missing):
            return "installed"
    raise InstallError("The monitor's required components could not be installed. Please check the internet connection and try again, "
                       "or ask your administrator for the full installer.")


def copy_agent_files(source_dir, install_dir, progress=lambda m: None):
    """Copy the program files. Never overwrites this PC's own data. No-op when installing in place."""
    if os.path.realpath(source_dir) == os.path.realpath(install_dir):
        return 0
    n = 0
    skip_dirs = {"tests", "__pycache__", "update_staging", "update_backup"}
    for dirpath, dirs, names in os.walk(source_dir):
        dirs[:] = [d for d in dirs if d not in skip_dirs]
        rel_dir = os.path.relpath(dirpath, source_dir)
        for name in names:
            if name in rm.PROTECTED_NAMES or name.endswith((".pyc", ".log", ".marker")) or name == "agent-diagnostic.txt":
                continue
            src = os.path.join(dirpath, name)
            dst = os.path.join(install_dir, "" if rel_dir == "." else rel_dir, name)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copyfile(src, dst)
            n += 1
    return n


def write_startup_launcher(install_dir, launcher_path=None, pythonw=None):
    launcher_path = launcher_path or startup_launcher_path()
    exe = pythonw or sys.executable
    if exe.lower().endswith("python.exe") and os.path.isfile(exe[:-10] + "pythonw.exe"):
        exe = exe[:-10] + "pythonw.exe"                # no console window
    os.makedirs(os.path.dirname(launcher_path), exist_ok=True)
    with open(launcher_path, "w", encoding="ascii", errors="replace") as f:
        f.write('@echo off\r\ncd /d "%s"\r\nstart "" "%s" "%s"\r\n' % (install_dir, exe, os.path.join(install_dir, "agent.py")))
    return launcher_path


def stop_running_agents(run=subprocess.run):
    """Stop any running copy of the agent (a fixed command; nothing from outside is ever put in it)."""
    script = ("Get-CimInstance Win32_Process -Filter \"Name='python.exe' OR Name='pythonw.exe'\" -ErrorAction SilentlyContinue | "
              "Where-Object { $_.CommandLine -like '*agent.py*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }")
    try:
        run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, timeout=60,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception:
        pass
    time.sleep(1)


def wait_for_local_ui(timeout=45, http=None, sleep=time.sleep):
    import requests
    http = http or requests
    end = time.time() + timeout
    while time.time() < end:
        try:
            if http.get(LOCAL_UI + "/", timeout=3).status_code == 200:
                return True
        except Exception:
            pass
        sleep(1)
    return False


def initial_report(http=None):
    """Ask the running agent for its first health check + heartbeat. -> (ok, message)."""
    import requests
    http = http or requests
    try:
        r = http.post(LOCAL_UI + "/report_now", timeout=180)
        body = r.json()
        return bool(body.get("ok")), str(body.get("error") or "")
    except Exception as e:
        return False, "the monitor did not answer (%s)" % type(e).__name__


def fetch_summary(http=None):
    import requests
    http = http or requests
    try:
        return http.get(LOCAL_UI + "/status", timeout=180).json()
    except Exception:
        return {}


def describe_devices(status, selections):
    """[(category title, device name or 'Skipped', verification text)] for the final screen."""
    rows = []
    diag = (status or {}).get("diagnosis", {})
    for cat, title, _d in CATEGORIES:
        c = selections.get(cat)
        if c is None:
            rows.append((title, "Skipped", "not used at this CSP"))
            continue
        dev = (status or {}).get(cat, {}) or {}
        if dev.get("verified") is True:
            text = "Verified" + (" (%s)" % dev["verified_by"] if dev.get("verified_by") else "")
        elif not dev:
            text = "Not checked yet"
        elif dev.get("configured") and not dev.get("present"):
            text = "Not connected right now"
        else:
            text = "Not verified - it could not be confirmed to be your %s" % (title if cat == "microatm" else title.lower())
        health = (diag.get(cat) or {}).get("health_state")
        if health and health not in ("HEALTHY", "UNVERIFIED", "NOT_SET_UP"):
            text += "; the monitor found: %s" % (diag[cat].get("summary") or health)
        rows.append((title, c["name"], text))
    return rows


class InstallResult(dict):
    pass


def run_install(params, progress, cancel=lambda: False, *, provision=provision_dependencies, start=None, stop=None,
                wait_ui=wait_for_local_ui, report=initial_report, summary=fetch_summary, enroll_session=None):
    """The whole installation. `progress(percent, message)`. Raises InstallError (plain message). Returns InstallResult.

    params: csp_id, name, server_url, install_dir, source_dir, selections, confirmed, launcher_path(optional),
            agent_version"""
    install_dir = params["install_dir"]
    source_dir = params.get("source_dir", SOURCE_DIR)
    server_url, csp_id, name = params["server_url"], params["csp_id"], params["name"]
    selections, confirmed = params.get("selections", {}), params.get("confirmed", {})
    start = start or au.start_agent
    stop = stop or stop_running_agents
    save = lambda **kw: config_store.save(**kw)           # noqa: E731

    def gate():
        if cancel():
            raise InstallError("Setup was cancelled. Nothing is half-installed; you can run it again at any time.")

    progress(3, "Checking this computer...")
    check_disk_space(install_dir)
    check_writable(install_dir)
    gate()

    existing = read_existing_config(install_dir)
    has_identity = bool(existing.get("api_key") and existing.get("csp_id") and existing.get("enroll_status", "") in ("", "approved", "pending"))
    if has_identity and existing["csp_id"] != csp_id:
        raise InstallError("This PC is already set up as a different CSP (%s). Please contact your administrator before changing it." % existing["csp_id"])

    progress(10, "Preparing the monitor's components...")
    provision(source_dir, lambda m: progress(15, m))
    gate()

    progress(30, "Copying the monitor program...")
    copy_agent_files(source_dir, install_dir)
    # the config path used by config_store must point at the install folder
    os.environ["CSP_AGENT_CONFIG_PATH"] = os.path.join(install_dir, "agent_config.json")
    gate()

    progress(45, "Saving your device choices...")
    updates = selection_to_config(selections, confirmed)
    if not has_identity:
        updates["server_url"] = server_url
    else:
        updates.setdefault("server_url", existing.get("server_url") or server_url)
    save(**updates)

    enrolled = "existing"
    if not has_identity:
        progress(55, "Registering this PC securely...")
        res = enroll.start(config_store.save, server_url, csp_id, name, params.get("agent_version", ""), enroll_session)
        enrolled = res.get("status", "")
        if enrolled in ("rejected", "invalid", "unknown_code", "name_mismatch"):
            raise InstallError(res.get("message") or "The registration was not accepted.")
        # "pending", "approved" and temporary network trouble all continue: the agent keeps retrying by itself
    gate()

    progress(65, "Setting the monitor to start with Windows...")
    launcher = write_startup_launcher(install_dir, params.get("launcher_path"))

    progress(72, "Starting the monitor...")
    stop()
    start(install_dir)
    up = wait_ui(45)
    if not up:
        raise InstallError("The monitor was installed but did not start. Please restart the computer, or ask your administrator for help.")
    progress(85, "Running the first health check...")
    ok, why = report()
    status = summary()
    progress(100, "Done.")
    cfg = config_store.load()
    return InstallResult(
        csp_id=csp_id, name=name, install_dir=install_dir, launcher=launcher, enrollment=enrolled or cfg.get("enroll_status", ""),
        connected=bool(ok), connection_note=why, devices=describe_devices(status, selections),
        agent_version=(status or {}).get("agent_version") or params.get("agent_version", ""),
        scan_summary=(status or {}).get("summary", ""), local_url=LOCAL_UI)
