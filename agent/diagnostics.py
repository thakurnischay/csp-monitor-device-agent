"""Root-cause diagnosis for the four CSP devices.

The older checks answered "is it plugged in and does Windows list it?". This
module answers the questions an operator actually needs answered when a
revenue service stops:

    WHAT is wrong?  WHY (root cause, how sure)?  WHICH workflow does it hit?
    HOW severe?  WHAT should be done, step by step?  Did the fix WORK?

Rules that keep it honest (each one exists because a real field CSP was shown a
false alarm by an earlier version):
  * Everything is built from evidence read from Windows or a vendor endpoint:
    Device Manager error codes + descriptions, driver version/signature, service
    state and start mode, recent service/driver error events, printer error
    state, the vendor RD service's own answer, the agent's own observation
    history. Nothing is simulated.
  * Every finding carries a CONFIDENCE: "confirmed" (Windows/the device itself
    says so), "likely" (the evidence points there), "possible" (a lead only).
    Only confirmed/likely findings can make a device look faulty. A status the
    agent cannot prove (RD "NOTREADY", a silent GPS port) is a NOTE, never a fault.
  * Severity comes from confidence x whether the workflow needs the device x
    whether a workaround exists - never from the device type alone - and a
    device is only CRITICAL on its own when operations has confirmed the impact.
  * Hardware failure is only suspected on repeated evidence (Windows reporting
    the device itself stopped, or repeated disconnects). Replacement is advised
    only after cable/port/software causes were ruled out - never for one miss.
  * Revenue is never estimated: no transaction data is connected to the agent.
  * Potentially disruptive fixes (starting a service) are never automatic: they
    run only when the operator clicks, once, and the result is verified.

Never raises: a broken scan degrades to "could not be checked", not a crash.
"""
import json
import os
import re
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone

import deep_checks
import device_health as dh
import impact_map

DEVICES = ("printer", "microatm", "biometric", "gps")
SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3, "none": 4}
STATE_PRIORITY = ["SERVICE_UNAVAILABLE", "DRIVER_ISSUE", "SOFTWARE_ISSUE", "SUSPECTED_HARDWARE",
                  "DEGRADED", "DIAGNOSIS_REQUIRED", "NOT_CONNECTED"]
FLAP_THRESHOLD = 3            # disconnect/reconnect cycles within FLAP_HOURS
FLAP_HOURS = 24
CRASH_THRESHOLD = 2           # service-crash events within 24 h
FIX_COOLDOWN_HOURS = 6        # never retry a failed fix sooner (no restart loops)
TESTPRINT_FAILS_FOR_HARDWARE = 3

# ---------------------------------------------------------------------------
# Windows Device Manager error codes (CM_PROB_*): code -> (state, confidence,
# blocks_device, plain-language cause). Source: Microsoft's documented codes.
# ---------------------------------------------------------------------------
CM_CODES = {
    1: ("DRIVER_ISSUE", "confirmed", True, "Windows reports the device is not configured correctly (driver set-up is incomplete)."),
    3: ("DRIVER_ISSUE", "likely", True, "The driver may be corrupted, or the PC is low on memory."),
    10: ("DRIVER_ISSUE", "likely", True, "Windows cannot start the device - usually a faulty or incompatible driver; the device itself is a less likely cause."),
    12: ("DRIVER_ISSUE", "likely", True, "Windows cannot find enough free resources for the device (a conflict with another device)."),
    14: ("DEGRADED", "confirmed", False, "The device will not work properly until the PC is restarted."),
    18: ("DRIVER_ISSUE", "confirmed", True, "The device's drivers need to be reinstalled."),
    19: ("DRIVER_ISSUE", "confirmed", True, "Windows has a damaged configuration record (registry) for this device."),
    22: ("SOFTWARE_ISSUE", "confirmed", True, "The device has been DISABLED in Windows (a configuration setting, not a fault)."),
    24: ("DRIVER_ISSUE", "likely", True, "Windows says the device is not working properly or does not have all its drivers installed."),
    28: ("DRIVER_ISSUE", "confirmed", True, "No driver is installed for this device."),
    31: ("DRIVER_ISSUE", "confirmed", True, "Windows cannot load the driver this device needs."),
    32: ("SOFTWARE_ISSUE", "confirmed", True, "The device's driver service is disabled in Windows."),
    37: ("DRIVER_ISSUE", "confirmed", True, "The device's driver failed while starting."),
    39: ("DRIVER_ISSUE", "confirmed", True, "The device's driver is corrupted or missing."),
    40: ("DRIVER_ISSUE", "confirmed", True, "The driver's registry information is missing or damaged."),
    41: ("SUSPECTED_HARDWARE", "likely", True, "Windows loaded the driver but cannot find the device itself - the device or its connection may be faulty."),
    43: ("SUSPECTED_HARDWARE", "likely", True, "Windows stopped the device because the DEVICE REPORTED A PROBLEM to it (a hardware, firmware or USB power fault is the usual cause)."),
    44: ("SOFTWARE_ISSUE", "confirmed", True, "An application or service shut this device down."),
    45: ("NOT_CONNECTED", "confirmed", True, "Windows knows the device but it is not currently connected."),
    47: ("NOT_CONNECTED", "confirmed", True, "The device was prepared for safe removal."),
    48: ("DRIVER_ISSUE", "confirmed", True, "The driver was blocked because of a known compatibility problem."),
    52: ("DRIVER_ISSUE", "confirmed", True, "Windows cannot verify the digital signature of the driver (it will not load it)."),
}

# Printer: Win32_Printer.DetectedErrorState
PRINTER_ERRORS = {
    3: ("DEGRADED", False, "paper is running low", ["Refill paper soon."]),
    4: ("DEGRADED", True, "the printer reports it is OUT OF PAPER", ["Load paper (or the passbook feeder) and press the printer's feed/online button."]),
    7: ("DEGRADED", True, "the printer reports its door or cover is OPEN", ["Close the printer's door/cover completely."]),
    8: ("DEGRADED", True, "the printer reports a PAPER JAM", ["Switch the printer off, remove the jammed paper carefully, switch on again.",
                                                              "If jams keep returning with clean, correct paper, the paper path may need servicing."]),
    9: ("DEGRADED", True, "the printer reports it is OFFLINE", ["Check the printer is switched on and its cable is firmly connected.",
                                                              "Press the printer's online button; clear any error light."]),
    10: ("DEGRADED", True, "the printer reports it needs SERVICE", ["Switch it off and on. If the error light returns, contact the printer vendor for service."]),
    5: ("DEGRADED", False, "ribbon/toner is running low", ["Replace the ribbon/toner soon."]),
    6: ("DEGRADED", True, "the printer reports NO RIBBON/TONER", ["Replace the ribbon/toner."]),
    11: ("DEGRADED", False, "the output tray is full", ["Empty the output tray."]),
}

SERVICE_ERROR_EVENTS = {7000, 7001, 7009, 7011, 7023, 7024, 7031, 7034}
CRASH_EVENTS = {7031, 7034}

# Windows services that match a vendor keyword but are NOT the vendor's
# software (Windows' own biometric service for Windows Hello, etc.).
_SYSTEM_PATH = re.compile(r"\\windows\\system32\\|\\windows\\syswow64\\|\\windows defender\\|svchost\.exe", re.I)
SERVICE_KEYWORDS = {
    "biometric": ("rdservice", "rd service", "rd_service", "mantra", "morpho", "idemia", "secugen",
                  "startek", "evolute", "tatvik", "aratek", "bioenable", "precision biometric", "fingerprint"),
    "microatm": ("ingenico", "ezetap", "mswipe", "verifone", "morefun", "telium", "minipos", "micro atm", "microatm", "pax "),
    "gps": ("gps", "gnss", "u-blox", "ublox"),
    "printer": (),
}
LABEL = {"printer": "passbook printer", "microatm": "micro-ATM",
         "biometric": "fingerprint scanner", "gps": "GPS dongle"}

NOT_CHECKED = {
    "biometric": ["A live fingerprint capture (never done: the agent never collects biometric data)",
                  "The banking application's own connection to the scanner (needs the application's logs or an API)"],
    "microatm": ["A transaction-level self-test of the micro-ATM (needs the vendor's SDK or diagnostic tool; "
                 "a live financial transaction is never used as a test)",
                 "Device error codes and logs held by the vendor's software (vendor-specific, not exposed to the agent)"],
    "printer": ["Print quality or alignment on the passbook (only a once-a-day test page is sent)"],
    "gps": ["Location accuracy / satellite fix quality as used by the CSP application",
            "How the CSP application uses the GPS dongle (not known to the agent)"],
}


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------
def _now():
    return datetime.now(timezone.utc)


def _iso(dt):
    return dt.isoformat(timespec="seconds")


def _parse_time(value):
    """ISO string, or PowerShell's /Date(ms)/ form -> aware datetime, else None."""
    if not value:
        return None
    try:
        s = str(value)
        m = re.search(r"/Date\((-?\d+)", s)
        if m:
            return datetime.fromtimestamp(int(m.group(1)) / 1000.0, tz=timezone.utc)
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _clip(text, n):
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"


def _ago(dt, now):
    if not dt:
        return "never"
    secs = max(0, int((now - dt).total_seconds()))
    if secs < 3600:
        return f"{max(1, secs // 60)} min ago"
    if secs < 172800:
        return f"{secs // 3600} h ago"
    return f"{secs // 86400} days ago"


# ---------------------------------------------------------------------------
# the agent's own observation history (flapping, last healthy, actions, ...)
# ---------------------------------------------------------------------------
class History:
    def __init__(self, path=None):
        self.path = path or os.environ.get("CSP_AGENT_HISTORY_PATH") or os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "diagnostic_history.json")
        self._lock = threading.Lock()

    def _load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
        except Exception:
            pass
        return {"devices": {}}

    def _save(self, data):
        try:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f)
            os.replace(tmp, self.path)
        except Exception:
            pass

    @staticmethod
    def _dev(data, dev):
        d = data.setdefault("devices", {}).setdefault(dev, {})
        for k, v in (("transitions", []), ("test_prints", []), ("actions", []), ("open_codes", []), ("recovered", [])):
            d.setdefault(k, v)
        return d

    @staticmethod
    def _prune(items, now, days=30, key="t"):
        cutoff = now - timedelta(days=days)
        return [i for i in items if (_parse_time(i.get(key)) or now) >= cutoff][-200:]

    def observe(self, dev, present, now=None):
        """Record whether the device is currently present; log only CHANGES."""
        now = now or _now()
        with self._lock:
            data = self._load()
            d = self._dev(data, dev)
            was = d.get("was_present")
            if present:
                d["last_present"] = _iso(now)
            if was is not None and bool(was) != bool(present):
                d["transitions"].append({"t": _iso(now), "present": bool(present)})
            d["was_present"] = bool(present)
            d["transitions"] = self._prune(d["transitions"], now)
            self._save(data)

    def flaps(self, dev, now=None, hours=FLAP_HOURS):
        """How many times the device DISAPPEARED (after having been present) recently."""
        now = now or _now()
        with self._lock:
            d = self._dev(self._load(), dev)
        cutoff = now - timedelta(hours=hours)
        return sum(1 for t in d["transitions"]
                   if not t.get("present") and (_parse_time(t.get("t")) or now) >= cutoff)

    def last_present(self, dev):
        with self._lock:
            return _parse_time(self._dev(self._load(), dev).get("last_present"))

    def last_healthy(self, dev):
        with self._lock:
            return _parse_time(self._dev(self._load(), dev).get("last_healthy"))

    def mark_healthy(self, dev, now=None):
        with self._lock:
            data = self._load()
            self._dev(data, dev)["last_healthy"] = _iso(now or _now())
            self._save(data)

    def record_test_print(self, ok, now=None):
        now = now or _now()
        with self._lock:
            data = self._load()
            d = self._dev(data, "printer")
            today = now.date().isoformat()
            if not any(t.get("t", "").startswith(today) for t in d["test_prints"]):   # one per day
                d["test_prints"].append({"t": _iso(now), "ok": bool(ok)})
            d["test_prints"] = self._prune(d["test_prints"], now)
            self._save(data)

    def test_print_failures(self, now=None, days=7):
        """(failed test prints, distinct days) in the last `days`, ONLY counting
        the unbroken run of failures up to now (a success resets it)."""
        now = now or _now()
        with self._lock:
            tests = list(self._dev(self._load(), "printer")["test_prints"])
        run = []
        for t in reversed(tests):
            if (_parse_time(t.get("t")) or now) < now - timedelta(days=days):
                break
            if t.get("ok"):
                break
            run.append(t)
        return len(run), len({(t.get("t") or "")[:10] for t in run})

    def record_action(self, dev, action, target, result, now=None):
        now = now or _now()
        with self._lock:
            data = self._load()
            d = self._dev(data, dev)
            d["actions"].append({"t": _iso(now), "action": action, "target": target, "result": result})
            d["actions"] = self._prune(d["actions"], now)
            self._save(data)

    def recent_failed_action(self, dev, action, target, now=None, hours=FIX_COOLDOWN_HOURS):
        now = now or _now()
        with self._lock:
            acts = list(self._dev(self._load(), dev)["actions"])
        cutoff = now - timedelta(hours=hours)
        for a in reversed(acts):
            if (a.get("action") == action and a.get("target") == target and a.get("result") != "ok"
                    and (_parse_time(a.get("t")) or now) >= cutoff):
                return a
        return None

    def reconcile(self, dev, open_codes, now=None):
        """Compare this check's open issue codes with the last check's. Issues that
        DISAPPEARED are recorded as recovered (verification). Returns the list of
        codes recovered within the last 24 h."""
        now = now or _now()
        with self._lock:
            data = self._load()
            d = self._dev(data, dev)
            gone = [c for c in d["open_codes"] if c not in open_codes]
            for c in gone:
                d["recovered"].append({"t": _iso(now), "code": c})
            d["open_codes"] = sorted(open_codes)
            d["recovered"] = self._prune(d["recovered"], now, days=30)
            self._save(data)
            cutoff = now - timedelta(hours=24)
            return [r for r in d["recovered"] if (_parse_time(r.get("t")) or now) >= cutoff]

    def actions(self, dev):
        with self._lock:
            return list(self._dev(self._load(), dev)["actions"])


# ---------------------------------------------------------------------------
# evidence collection (real Windows queries, cached because most of it changes slowly)
# ---------------------------------------------------------------------------
_cache = {}
_cache_lock = threading.Lock()


def reset_cache():
    with _cache_lock:
        _cache.clear()


def _cached(key, ttl, fn):
    now = time.time()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
    try:
        value = fn()
    except Exception:
        value = None
    if value is not None:                      # never cache a failed collection
        with _cache_lock:
            _cache[key] = (now, value)
    return value


def _ps(script, timeout=45):
    return dh._run_ps(script, timeout)


def collect_services():
    data = _ps("Get-CimInstance Win32_Service | Select-Object Name,DisplayName,State,StartMode,ExitCode,PathName "
               "| ConvertTo-Json -Compress")
    if data is None:
        return None
    return [{"name": str(s.get("Name") or ""), "display": str(s.get("DisplayName") or ""),
             "state": str(s.get("State") or ""), "start": str(s.get("StartMode") or ""),
             "exit": s.get("ExitCode"), "path": str(s.get("PathName") or "")}
            for s in dh._as_list(data) if isinstance(s, dict)]


def collect_drivers():
    data = _ps("Get-CimInstance Win32_PnPSignedDriver | Where-Object { $_.DeviceName } | "
               "Select-Object DeviceName,DriverVersion,DriverDate,Manufacturer,IsSigned,DeviceID | ConvertTo-Json -Compress", 60)
    if data is None:
        return None
    return [{"device_id": str(d.get("DeviceID") or ""), "name": str(d.get("DeviceName") or ""),
             "version": str(d.get("DriverVersion") or ""), "date": _parse_time(d.get("DriverDate")),
             "maker": str(d.get("Manufacturer") or ""), "signed": d.get("IsSigned")}
            for d in dh._as_list(data) if isinstance(d, dict)]


def collect_events():
    script = (
        "$s=(Get-Date).AddHours(-24);"
        "$a=@(Get-WinEvent -FilterHashtable @{LogName='System';Level=1,2,3;StartTime=$s} -MaxEvents 600 -ErrorAction SilentlyContinue | "
        "Where-Object { $_.ProviderName -in 'Service Control Manager','Microsoft-Windows-Kernel-PnP',"
        "'Microsoft-Windows-USB-USBHUB3','Microsoft-Windows-UserPnp' } | "
        "ForEach-Object { $m=($_.Message -replace '[\r\n]+',' '); "
        "[pscustomobject]@{P=$_.ProviderName;I=$_.Id;T=$_.TimeCreated.ToString('o');M=$m.Substring(0,[Math]::Min(300,$m.Length))} });"
        "if($a.Count -eq 0){'[]'}else{$a | ConvertTo-Json -Compress}")
    data = _ps(script, 60)
    if data is None:            # '[]' means "no matching events"; None means the log could not be read
        return None
    return [{"provider": str(e.get("P") or ""), "id": e.get("I"), "t": _parse_time(e.get("T")),
             "msg": str(e.get("M") or "")} for e in dh._as_list(data) if isinstance(e, dict)]


def collect_printers():
    data = _ps("Get-CimInstance Win32_Printer | Select-Object Name,PrinterStatus,DetectedErrorState,"
               "ExtendedDetectedErrorState,WorkOffline,PortName,DriverName | ConvertTo-Json -Compress")
    if data is None:
        return None
    return [p for p in dh._as_list(data) if isinstance(p, dict)]


def collect_jobs():
    data = _ps("$j=@(Get-CimInstance Win32_PrintJob | Select-Object Name,JobStatus,Status,TimeSubmitted); "
               "if($j.Count -eq 0){'[]'}else{$j | ConvertTo-Json -Compress}")
    if data is None:
        return None
    return [j for j in dh._as_list(data) if isinstance(j, dict)]       # [] = no jobs (valid)


def collect():
    """All evidence, gathered in parallel. A source that failed is None, which
    is different from "checked and found nothing" ([]) - the rules rely on it."""
    jobs = {"services": lambda: _cached("services", 60, collect_services),
            "drivers": lambda: _cached("drivers", 6 * 3600, collect_drivers),
            "events": lambda: _cached("events", 600, collect_events),
            "printers": lambda: _cached("printers", 30, collect_printers),
            "jobs": lambda: _cached("jobs", 30, collect_jobs)}
    out = {}
    threads = []

    def run(name, fn):
        try:
            out[name] = fn()
        except Exception:
            out[name] = None

    for name, fn in jobs.items():
        t = threading.Thread(target=run, args=(name, fn), daemon=True)
        t.start()
        threads.append(t)
    deadline = time.time() + 90
    for t in threads:
        t.join(max(0.1, deadline - time.time()))
    for name in jobs:
        out.setdefault(name, None)
    return out


# ---------------------------------------------------------------------------
# finding construction + severity
# ---------------------------------------------------------------------------
def finding(code, dev, state, title, symptom, cause, confidence, evidence, actions,
            blocks=False, operator=True, auto_fix=None, category=""):
    return {"code": code, "device": dev, "state": state, "title": _clip(title, 140),
            "symptom": _clip(symptom, 260), "root_cause": _clip(cause, 360), "confidence": confidence,
            "evidence": [_clip(e, 220) for e in evidence if e][:12],
            "actions": [_clip(a, 280) for a in actions if a][:9],
            "blocks": bool(blocks), "operator_action": bool(operator), "auto_fix": auto_fix,
            "category": category or state.lower()}


def _has_workaround(entry_text):
    t = (entry_text or "").strip().lower()
    return bool(t) and not t.startswith(("none", "not established"))


def severity_for(f, impact):
    """confidence x does-the-workflow-need-it x workaround, never device type alone."""
    required = impact.get("required")
    known = bool(impact.get("workflows")) and any(w["needs"] in ("required", "optional") for w in impact["workflows"])
    if f["state"] == "NOT_CONNECTED":
        return "medium" if f.get("was_recently_present") else "low"
    if f["confidence"] == "possible":
        return "low"
    if not f["blocks"]:
        return "low" if f["confidence"] == "confirmed" else "low"
    if not known:                                   # impact not established (e.g. GPS)
        return "medium" if f["confidence"] == "confirmed" else "low"
    sev = "high" if f["confidence"] == "confirmed" else "medium"
    if sev == "high" and (not required or _has_workaround(impact.get("workaround"))):
        sev = "medium"                              # a workaround exists: not "no practical workaround"
    elif sev == "high" and impact.get("confirmed") and f["confidence"] == "confirmed":
        sev = "critical"                            # operations has CONFIRMED this workflow depends on it
    return sev


# ---------------------------------------------------------------------------
# rules
# ---------------------------------------------------------------------------
def _services_for(dev, services):
    if not services:
        return []
    kws = SERVICE_KEYWORDS.get(dev, ())
    out = []
    for s in services:
        hay = (s["name"] + " " + s["display"]).lower()
        if not any(k in hay for k in kws):
            continue
        if _SYSTEM_PATH.search(s.get("path") or "") or s["display"].lower().startswith("windows "):
            continue                                  # Windows' own service, not the vendor's
        out.append(s)
    return out


def _events_for_service(svc, events):
    if not events:
        return []
    names = {svc["name"].lower(), svc["display"].lower()}
    return [e for e in events if e["provider"] == "Service Control Manager" and e["id"] in SERVICE_ERROR_EVENTS
            and any(n and n in e["msg"].lower() for n in names)]


def _driver_for(row, drivers):
    if not row or not drivers:
        return None
    inst = (row.get("instance") or "").lower()
    for d in drivers:
        if inst and d["device_id"].lower() == inst:
            return d
    return None


def _service_findings(dev, svc_list, events, history, now, extra_evidence, brand):
    out = []
    for s in svc_list:
        label = s["display"] or s["name"]
        evid = list(extra_evidence) + [f"Windows service '{label}': state {s['state']}, start mode {s['start']}"]
        if s.get("exit") not in (None, 0):
            evid.append(f"The service's last exit code was {s['exit']}")
        errs = _events_for_service(s, events)
        crashes = [e for e in errs if e["id"] in CRASH_EVENTS]
        if errs:
            evid.append(f"{len(errs)} Windows service error event(s) for it in the last 24 h "
                        f"(latest: {_ago(max((e['t'] for e in errs if e['t']), default=None), now)})")
        if s["state"].lower() != "running" and s["start"].lower() == "auto":
            tried = history.recent_failed_action(dev, "start_service", s["name"], now)
            actions = [f"Do NOT reconnect the {LABEL[dev]}: Windows detects it; the software that drives it is stopped.",
                       f"Start the service '{label}': press Win+R, type services.msc, find '{label}', right-click -> Start "
                       "(Administrator rights may be needed)."]
            if tried:
                actions = [f"The agent already tried to start '{label}' ({_ago(_parse_time(tried['t']), now)}) and it did not start: "
                           f"{tried.get('result')}.",
                           "Open Windows Event Viewer -> Windows Logs -> System and read the error logged for this service.",
                           f"If it keeps failing, repair or reinstall the vendor-approved {brand or LABEL[dev]} software, "
                           "then restart the PC.",
                           "Do not keep restarting it repeatedly - the cause needs to be found first."]
            else:
                actions.append("Or click 'Start it now' on the agent page (http://127.0.0.1:5057) - it asks for your approval first, "
                               "tries once, and re-checks.")
            actions.append("This issue closes by itself once the service is running and the next check passes.")
            out.append(finding(
                f"SERVICE_STOPPED:{s['name']}", dev, "SERVICE_UNAVAILABLE",
                f"'{label}' is stopped",
                f"The {LABEL[dev]} is detected by Windows but its software service '{label}' is not running.",
                "The service is set to start automatically but is stopped (it crashed, was stopped, or failed to start).",
                "confirmed", evid, actions, blocks=True, operator=True,
                auto_fix=None if tried else {"type": "start_service", "service": s["name"], "label": label},
                category="software"))
        if len(crashes) >= CRASH_THRESHOLD:
            out.append(finding(
                f"SERVICE_CRASHING:{s['name']}", dev, "SOFTWARE_ISSUE",
                f"'{label}' keeps crashing",
                f"The service '{label}' terminated unexpectedly {len(crashes)} times in 24 hours.",
                "The vendor software is unstable (a bad install, a conflict, or a driver/device problem underneath it).",
                "confirmed", evid, [
                    f"Repair or update the vendor-approved {brand or LABEL[dev]} software (do not use copies from unknown sources).",
                    "Check Event Viewer -> Windows Logs -> System for the exact error.",
                    "If it still crashes after a clean reinstall, contact the vendor with the event details; "
                    "check the device on another PC before blaming the hardware."],
                blocks=True, operator=True, category="software"))
    return out


def _cm_finding(dev, row, driver, history, now):
    code = row.get("cm_code") or 0
    if not code:
        return None
    state, conf, blocks, cause = CM_CODES.get(code, (
        "DIAGNOSIS_REQUIRED", "possible", True, "Windows reports a device problem the agent has no specific rule for."))
    name = row.get("name", "the device")
    brand = row.get("manufacturer") or row.get("vendor") or ""
    evid = [f"Windows Device Manager reports error code {code} for '{name}'"
            + (f": {row['problem']}" if row.get("problem") else "")]
    if driver:
        evid.append(f"Driver: {driver['name']} version {driver['version']}"
                    + (f" dated {driver['date'].date().isoformat()}" if driver.get("date") else "")
                    + (" (digitally signed)" if driver.get("signed") else " (NOT signed)" if driver.get("signed") is False else ""))
    if state == "NOT_CONNECTED":
        return finding(f"CM_{code}", dev, state, f"Windows lists '{name}' but it is not connected",
                       "The device is registered but not currently connected.", cause, conf, evid,
                       ["Check the cable and the USB port; reconnect the device.",
                        "Try a different USB port directly on the PC (not a hub)."], blocks=blocks, category="connection")
    base = [f"Open Device Manager (right-click Start -> Device Manager) and find '{name}'."]
    if state == "DRIVER_ISSUE":
        actions = base + [
            f"Install the vendor-approved driver for the {brand or LABEL[dev]} - use the package supplied by the vendor or Eko support; "
            "do not download drivers from unknown sites.",
            "Right-click the device -> Update driver -> Browse my computer, and choose that package (or run the vendor's installer).",
            "Restart the PC if the installer asks.",
            "The agent re-checks every 5 minutes; this issue closes itself once Windows reports no error."]
    elif state == "SUSPECTED_HARDWARE":
        actions = [
            "Unplug the device, wait 10 seconds, and plug it directly into a different USB port on the PC (not a hub).",
            "If the device has a detachable cable, try another cable.",
            "Restart the PC and check again.",
            "If Windows still reports this error after the steps above, test the device on another PC. "
            "If it fails there too, contact the vendor for inspection, repair or replacement - "
            "replacement is justified only after these steps fail."]
    elif code == 22:
        actions = base + ["Right-click the device -> Enable device.",
                          "If 'Enable device' is not offered, restart the PC."]
    elif code == 14:
        actions = ["Restart the PC (Windows needs a restart to finish setting this device up)."]
    else:
        actions = base + ["Right-click the device -> Properties and read the status message; try Update driver.",
                          "Restart the PC. If the error returns, contact support with the error code above."]
    return finding(f"CM_{code}", dev, state, f"Windows reports error code {code} for '{name}'",
                   f"Windows has the {LABEL[dev]} listed with a problem: {row.get('problem') or 'error code ' + str(code)}",
                   cause, conf, evid, actions, blocks=blocks,
                   category={"DRIVER_ISSUE": "driver", "SUSPECTED_HARDWARE": "hardware"}.get(state, "software"))


def _flap_finding(dev, flaps, hours):
    return finding(
        "REPEATED_DISCONNECTS", dev, "SUSPECTED_HARDWARE",
        f"The {LABEL[dev]} keeps disconnecting",
        f"The device disappeared and reappeared {flaps} times in the last {hours} hours.",
        "An unstable connection - a loose or damaged cable, a faulty USB port or hub, insufficient USB power, "
        "or a failing device. The software side is less likely (the device itself keeps dropping off).",
        "likely",
        [f"The agent saw the device disconnect {flaps} times in {hours} hours (its own observations every 5 minutes)"],
        ["Plug the device directly into a USB port on the PC itself (not a hub or front-panel extension).",
         "Try a different USB port, then a different cable if it has one.",
         "Check the cable is not bent or loose; avoid moving the device while it is in use.",
         "If it keeps disconnecting after the cable and port were changed, contact the vendor for repair; "
         "replacement is justified only once those steps have been tried."],
        blocks=True, category="hardware")


def _device_rules(dev, status, ctx):
    """Findings + notes for one CONNECTED device."""
    row = status.get("_row") or {}
    ev, hist, now = ctx["evidence"], ctx["history"], ctx["now"]
    findings, notes = [], []
    brand = row.get("manufacturer") or row.get("vendor") or ""
    driver = _driver_for(row, ev.get("drivers"))
    cm = _cm_finding(dev, row, driver, hist, now) if dev != "printer" or row.get("instance") else None
    if cm:
        findings.append(cm)
    if driver and driver.get("signed") is False and not cm:
        notes.append(f"The driver for '{row.get('name')}' is not digitally signed.")

    # --- Windows services belonging to the vendor ---------------------------------------------------
    svcs = _services_for(dev, ev.get("services"))
    base_evidence = []
    if status.get("ok") and not cm:
        base_evidence.append(f"Windows lists the {LABEL[dev]} as connected (status {row.get('status', 'OK')}, no error code)")
    if dev in ("biometric", "microatm"):
        findings += _service_findings(dev, svcs, ev.get("events"), hist, now, base_evidence, brand)

    # --- device-specific ---------------------------------------------------------------------------
    if dev == "biometric":
        rd_probed = "rd" in status
        rd = status.get("rd") or []
        running_svc = [s for s in svcs if s["state"].lower() == "running"]
        ready = [r for r in rd if r.get("status") == "READY"]
        notready = [r for r in rd if r.get("status") != "READY"]
        stopped_auto = [s for s in svcs if s["state"].lower() != "running" and s["start"].lower() == "auto"]
        if ready:
            notes.append("The scanner's RD service answered READY.")
        elif notready:
            notes.append("The scanner's RD service answered NOT READY. Some scanners report this until their first use; "
                         "it is not treated as a fault.")
        elif rd_probed and not stopped_auto:
            if running_svc:
                findings.append(finding(
                    "RD_NOT_RESPONDING", dev, "SERVICE_UNAVAILABLE", "The scanner's RD service is running but not answering",
                    "A scanner software service is running, but nothing answers on the RD service ports (11100-11120).",
                    "The RD service is hung or blocked (by a firewall/antivirus, or a port conflict), or it needs the scanner to be re-detected.",
                    "likely", base_evidence + [f"Windows service '{running_svc[0]['display'] or running_svc[0]['name']}' is running",
                                               "No RD service answered on ports 11100-11120"],
                    ["Restart the RD service: services.msc -> find it -> right-click -> Restart (Administrator rights may be needed).",
                     "Make sure antivirus/firewall is not blocking local connections to ports 11100-11120.",
                     "If it keeps happening, repair or reinstall the vendor-approved RD service.",
                     "The agent re-checks every 5 minutes and closes this by itself when the service answers."],
                    blocks=True, category="software"))
            elif not svcs and status.get("recognised"):
                findings.append(finding(
                    "RD_NOT_FOUND", dev, "DIAGNOSIS_REQUIRED", "No RD service was found for this scanner",
                    f"The {brand or 'scanner'} is connected, but no RD service answers and no matching Windows service exists.",
                    "The vendor's RD service may not be installed or is not running (some run as an application in the system tray, not as a Windows service).",
                    "possible", base_evidence + ["No RD service answered on ports 11100-11120",
                                                 "No vendor service found in Windows services"],
                    ["If this CSP does Aadhaar-based (AePS/eKYC) work on this PC: open the vendor's RD service application (check the system tray) and make sure it is running.",
                     "If it is not installed, install the vendor-approved RD service for this scanner model, then restart the PC.",
                     "If this PC does not use this scanner for Aadhaar services, no action is needed."],
                    blocks=False, category="software"))

    if dev == "microatm":
        proc = ctx["cfg"].get("software_process", "")
        sw = ctx["software"]
        if proc and sw.get("configured") and not sw.get("running"):
            findings.append(finding(
                "APP_NOT_RUNNING", dev, "SOFTWARE_ISSUE", f"The micro-ATM application '{proc}' is not running",
                "The device is connected, but the CSP software that operates it is not running.",
                "The application was closed or crashed.", "confirmed",
                base_evidence + [f"Process '{proc}' is not running (checked just now)"],
                [f"Start the micro-ATM application ('{proc}') from its icon.",
                 "If it closes again by itself, restart the PC; if it still crashes, contact the application's support with the time it happened."],
                blocks=True, category="software"))

    if dev == "gps":
        kind = status.get("gps_check")
        if kind == "DATA":
            notes.append("The dongle is sending valid GPS data."
                         + (" It has a satellite fix." if status.get("gps_fix") else " It has no satellite fix yet (normal indoors)."))
        elif kind == "SILENT":
            notes.append("No GPS data was heard on the port. This check only understands common NMEA speeds, "
                         "so it is not treated as a fault.")
        elif kind == "BUSY":
            notes.append("The GPS port is in use by another program, so the data check was skipped.")

    if dev == "printer":
        findings += _printer_findings(status, ctx, row, notes)

    # --- repeated disconnects -----------------------------------------------------------------------
    flaps = hist.flaps(dev, now)
    if flaps >= FLAP_THRESHOLD:
        findings.append(_flap_finding(dev, flaps, FLAP_HOURS))
    return findings, notes


def _printer_findings(status, ctx, row, notes):
    ev, hist, now = ctx["evidence"], ctx["history"], ctx["now"]
    name = status.get("resolved_name") or row.get("name") or ""
    out = []
    svc = next((s for s in (ev.get("services") or []) if s["name"].lower() == "spooler"), None)
    if svc and svc["state"].lower() != "running":
        tried = hist.recent_failed_action("printer", "start_service", svc["name"], now)
        out.append(finding(
            "SPOOLER_STOPPED", "printer", "SERVICE_UNAVAILABLE", "The Windows Print Spooler service is stopped",
            "Nothing can print on this PC while the Print Spooler is stopped.",
            "The Print Spooler service crashed or was stopped.", "confirmed",
            [f"Windows service 'Print Spooler': state {svc['state']}, start mode {svc['start']}"],
            ["Press Win+R, type services.msc, find 'Print Spooler', right-click -> Start (Administrator rights may be needed).",
             "Or click 'Start it now' on the agent page - it asks for your approval first and tries once." if not tried
             else "The agent already tried to start it and it failed - check Event Viewer -> System for the Print Spooler error.",
             "If the spooler stops again by itself, a corrupted print job or printer driver is the usual cause: clear the print queue and update the driver."],
            blocks=True, auto_fix=None if tried else {"type": "start_service", "service": svc["name"], "label": "Print Spooler"},
            category="software"))
    prn = next((p for p in (ev.get("printers") or []) if str(p.get("Name")) == name), None)
    if prn:
        es = prn.get("DetectedErrorState") or 0
        if es in PRINTER_ERRORS:
            state, blocks, what, steps = PRINTER_ERRORS[es]
            out.append(finding(
                f"PRINTER_{es}", "printer", state, f"The printer {what}",
                f"Windows reads the printer's own error state: {what}.",
                "A physical/consumable condition reported by the printer itself - not a driver or software fault.",
                "confirmed", [f"Printer '{name}' reports DetectedErrorState {es} ({what})"], steps + [
                    "The agent re-checks every 5 minutes and closes this by itself once the printer reports no error."],
                blocks=blocks, category="consumable"))
        if prn.get("WorkOffline"):
            out.append(finding(
                "PRINTER_WORK_OFFLINE", "printer", "SOFTWARE_ISSUE", "The printer is set to 'Use Printer Offline'",
                "Windows will hold all print jobs while this setting is on, even though the printer is connected.",
                "The 'Use Printer Offline' option is ticked in Windows (set by a person, or by Windows after losing contact once).",
                "confirmed", [f"Printer '{name}' has WorkOffline = true"],
                ["Open Settings -> Bluetooth & devices -> Printers & scanners -> the printer -> Open print queue.",
                 "In the queue window choose Printer menu and untick 'Use Printer Offline'.",
                 "If it switches itself offline again, check the cable and replace it if damaged."],
                blocks=True, category="software"))
    jobs = ev.get("jobs") or []
    mine = [j for j in jobs if str(j.get("Name", "")).split(",")[0].strip() == name]
    stuck = [j for j in mine if (_parse_time(j.get("TimeSubmitted")) or now) < now - timedelta(minutes=30)]
    if stuck:
        out.append(finding(
            "PRINT_QUEUE_STUCK", "printer", "SOFTWARE_ISSUE", f"{len(stuck)} print job(s) are stuck in the queue",
            "Documents have been waiting in the printer's queue for more than 30 minutes.",
            "A job is stuck (a bad document, an offline printer, or a spooler problem) and blocks the ones behind it.",
            "likely", [f"{len(stuck)} job(s) for '{name}' were submitted more than 30 minutes ago and are still queued"],
            ["Open the printer's print queue (Settings -> Printers & scanners -> the printer -> Open print queue).",
             "Cancel the stuck job(s) (Printer menu -> Cancel All Documents).",
             "If it jams again, restart the Print Spooler service."],
            blocks=True, category="software"))
    t = ctx.get("functional") or {}
    fails, days = hist.test_print_failures(now)
    if t.get("ran") and t.get("ok") is False:
        findings_so_far = [f for f in out]
        if fails >= TESTPRINT_FAILS_FOR_HARDWARE and days >= 2 and not findings_so_far:
            out.append(finding(
                "TESTPRINT_FAILING", "printer", "SUSPECTED_HARDWARE", "The daily test print keeps failing",
                f"The test page was rejected on {fails} checks over {days} days while the printer, driver and spooler all report normally.",
                "With the driver, spooler and printer status all normal, the repeated failure points to a printer hardware or mechanism fault.",
                "likely", [f"Test page failed {fails} times on {days} different days: {t.get('detail', '')}",
                           "Windows reports no driver error, the Print Spooler is running, and the printer reports no error state"],
                ["Check paper type and path, and that the printer's own self-test page prints (hold the feed button while switching on - see the manual).",
                 "Try another USB/serial cable and port.",
                 "If the self-test page also fails, contact the vendor for inspection or repair; replacement is justified once the self-test fails."],
                blocks=True, category="hardware"))
        elif not out:
            out.append(finding(
                "TESTPRINT_FAILED", "printer", "DIAGNOSIS_REQUIRED", "The daily test print did not succeed",
                f"The test page was not accepted: {t.get('detail', 'no detail')}",
                "Not yet known - Windows reports no other problem, so more evidence is needed. One failure is not enough to blame the hardware.",
                "possible", [f"Test page result: {t.get('detail', '')}"],
                ["Check paper is loaded and the printer is switched on, then wait for the next check.",
                 "If it fails again on later days, the agent will gather the evidence and tell you whether it looks like a printer fault."],
                blocks=False, category="unknown"))
    return out


def _not_connected(dev, ctx):
    hist, now = ctx["history"], ctx["now"]
    last = hist.last_present(dev)
    recently = bool(last and last >= now - timedelta(days=7))
    ev = [f"The {LABEL[dev]} is not detected right now"]
    ev.append(f"It was last detected {_ago(last, now)}" if last else "The agent has never detected it on this PC")
    f = finding(
        "NOT_CONNECTED", dev, "NOT_CONNECTED", f"The {LABEL[dev]} is not detected",
        f"Windows does not list the {LABEL[dev]} as connected.",
        "It is unplugged, switched off, or not getting power/connection (cable, port or - if it keeps happening - the device)."
        if recently else "It has not been connected to this PC while the agent has been watching.",
        "confirmed", ev,
        ["Check the device is switched on and its cable is firmly connected at both ends.",
         "Try a different USB port directly on the PC (not a hub).",
         "If it disappears again and again, the agent will flag a suspected cable/port/hardware fault with evidence."]
        if recently else
        ["If this CSP has a " + LABEL[dev] + ", connect it. If it does not use one, no action is needed (clear the selection on the agent page)."],
        blocks=True, category="connection")
    f["was_recently_present"] = recently
    flaps = hist.flaps(dev, now)
    return [f] + ([_flap_finding(dev, flaps, FLAP_HOURS)] if flaps >= FLAP_THRESHOLD else [])


# ---------------------------------------------------------------------------
# per-device diagnosis + whole-CSP aggregation
# ---------------------------------------------------------------------------
def diagnose_device(dev, status, ctx):
    now, hist, ev = ctx["now"], ctx["history"], ctx["evidence"]
    impact = impact_map.describe(dev, ctx["impact"])
    checked = ["Whether Windows lists the device (present / not present)"]
    if not status.get("configured"):
        return {"health_state": "NOT_SET_UP", "severity": "none", "summary": "Not set up on this PC",
                "findings": [], "notes": [], "checked": [], "not_checked": [], "impact": impact,
                "last_healthy_at": _iso(hist.last_healthy(dev)) if hist.last_healthy(dev) else None,
                "verification": None}
    present = bool(status.get("present"))
    hist.observe(dev, present, now)
    findings, notes = ([], []) if present else (_not_connected(dev, ctx), [])
    if present:
        findings, notes = _device_rules(dev, status, ctx)
        checked.append("Windows device status and error code")
        if ev.get("drivers") is not None:
            checked.append("Driver version and digital signature")
        if dev in ("biometric", "microatm", "printer") and ev.get("services") is not None:
            checked.append("Vendor / system services (running state and start mode)")
        if ev.get("events") is not None and dev in ("biometric", "microatm"):
            checked.append("Windows service-crash events in the last 24 hours")
        if dev == "biometric":
            checked.append("The scanner's RD service (local status request - never a fingerprint capture)")
        if dev == "printer" and ev.get("printers") is not None:
            checked.append("The printer's own error state (paper, jam, door, offline) and its print queue")
        if dev == "gps":
            checked.append("Whether the dongle is sending GPS data (read-only)")
        checked.append("Disconnect history (the agent's own observations)")
    not_checked = list(NOT_CHECKED.get(dev, []))
    for source, text in (("drivers", "Driver details (the Windows driver query did not answer)"),
                         ("services", "Service state (the Windows service query did not answer)"),
                         ("events", "Windows event log (could not be read)")):
        if present and ev.get(source) is None and dev in ("biometric", "microatm", "printer"):
            not_checked.append(text)

    for f in findings:
        f["severity"] = severity_for(f, impact)
        f.pop("was_recently_present", None)
    findings.sort(key=lambda f: (SEVERITY_ORDER[f["severity"]],
                                 STATE_PRIORITY.index(f["state"]) if f["state"] in STATE_PRIORITY else 99))
    top = findings[0] if findings else None
    if top:
        health_state, severity, summary = top["state"], top["severity"], top["title"]
    elif status.get("verified") is False:
        health_state, severity = "UNVERIFIED", "low"
        summary = "No problem found, but the device identity is not confirmed (use Find my device)"
    else:
        health_state, severity = "HEALTHY", "none"
        summary = "No problem found by the checks listed below"
        hist.mark_healthy(dev, now)
    recovered = hist.reconcile(dev, [f["code"] for f in findings], now)
    verification = None
    if findings:
        verification = {"status": "pending", "note": "Re-checked automatically every 5 minutes; "
                                                     "this closes by itself when the cause is fixed."}
    elif recovered:
        verification = {"status": "recovery_verified", "at": recovered[-1]["t"],
                        "codes": sorted({r["code"] for r in recovered}),
                        "note": "The earlier problem is gone and the latest checks pass."}
    last_ok = hist.last_healthy(dev)
    return {"health_state": health_state, "severity": severity, "summary": summary,
            "findings": findings[:8], "notes": [_clip(n, 220) for n in notes][:6],
            "checked": checked, "not_checked": not_checked, "impact": impact,
            "last_healthy_at": _iso(last_ok) if last_ok else None, "verification": verification,
            "actions_taken": hist.actions(dev)[-5:]}


def analyze(health, cfg, functional=None, history=None, evidence=None, impact=None, now=None):
    """The whole diagnosis: {device: {...}}. Never raises."""
    now = now or _now()
    history = history or History()
    try:
        evidence = evidence if evidence is not None else collect()
    except Exception:
        evidence = {"services": None, "drivers": None, "events": None, "printers": None, "jobs": None}
    impact = impact or impact_map.load()
    functional = functional or {}
    if functional.get("ran") and cfg is not None:
        try:
            history.record_test_print(bool(functional.get("ok")), now)
        except Exception:
            pass
    ctx = {"evidence": evidence, "history": history, "now": now, "cfg": cfg or {},
           "software": health.get("software") or {}, "functional": functional, "impact": impact}
    result = {}
    for dev in DEVICES:
        try:
            result[dev] = diagnose_device(dev, health.get(dev) or {}, ctx)
        except Exception as e:                                # a rule bug must never break reporting
            result[dev] = {"health_state": "DIAGNOSIS_REQUIRED", "severity": "low",
                           "summary": "The diagnosis could not be completed", "findings": [], "notes": [str(e)[:160]],
                           "checked": [], "not_checked": [], "impact": impact_map.describe(dev, impact),
                           "last_healthy_at": None, "verification": None}
    # several confirmed, workflow-blocking failures at one CSP -> the whole operation is at risk
    blocked = [d for d, r in result.items() if r["severity"] == "high"
               and any(f["blocks"] and f["confidence"] == "confirmed" for f in r["findings"])]
    if len(blocked) >= 2:
        for d in blocked:
            result[d]["severity"] = "critical"
            for f in result[d]["findings"]:
                if f["severity"] == "high":
                    f["severity"] = "critical"
            result[d]["summary"] += " (several services are down at this CSP)"
    return result


# ---------------------------------------------------------------------------
# operator-approved remediation (never automatic, never looped)
# ---------------------------------------------------------------------------
_SERVICE_NAME = re.compile(r"^[A-Za-z0-9_.\- ]{1,80}$")


def start_service(service, diagnosis, history=None, now=None, runner=None):
    """Start ONE Windows service, only if the current diagnosis proposes exactly
    that fix, and only once per cooldown after a failure. The operator's click is
    the approval. Returns {"ok", "message", "needs_admin"}."""
    history = history or History()
    now = now or _now()
    runner = runner or _run_start
    if not _SERVICE_NAME.match(service or ""):
        return {"ok": False, "message": "That is not a valid service name."}
    target, dev = None, None
    for d, r in (diagnosis or {}).items():
        for f in r.get("findings", []):
            fix = f.get("auto_fix") or {}
            if fix.get("type") == "start_service" and fix.get("service") == service:
                target, dev = f, d
    if not target:
        for d in DEVICES:                      # a failed attempt withdraws the offer - say so, don't just refuse
            tried = history.recent_failed_action(d, "start_service", service, now)
            if tried:
                return {"ok": False, "message": f"A start was already tried {_ago(_parse_time(tried['t']), now)} and failed "
                                                f"({tried.get('result')}). It is not retried automatically - the cause needs to be "
                                                "found (see the steps shown)."}
        return {"ok": False, "message": "The current diagnosis does not propose starting that service, so nothing was changed."}
    tried = history.recent_failed_action(dev, "start_service", service, now)
    if tried:
        return {"ok": False, "message": f"A start was already tried {_ago(_parse_time(tried['t']), now)} and failed ({tried.get('result')}). "
                                        "It is not retried automatically - the cause needs to be found (see the steps shown)."}
    ok, detail = runner(service)
    history.record_action(dev, "start_service", service, "ok" if ok else _clip(detail, 160), now)
    reset_cache()                    # the next check must see the NEW state, not a cached "stopped"
    deep_checks.clear_cache()
    if ok:
        return {"ok": True, "message": f"'{service}' is now running. The agent will re-check and confirm recovery."}
    needs_admin = bool(re.search(r"access.*denied|requires elevation|permission|privilege", detail or "", re.I))
    return {"ok": False, "needs_admin": needs_admin,
            "message": ("Windows refused: Administrator rights are needed. Start it yourself: Win+R -> services.msc -> right-click the service -> Start."
                        if needs_admin else f"The service did not start: {_clip(detail, 200)}")}


def _run_start(service):
    script = (f"try {{ Start-Service -Name '{service}' -ErrorAction Stop; Start-Sleep -Seconds 2; "
              f"$s=(Get-Service -Name '{service}').Status; Write-Output \"STATUS:$s\" }} "
              "catch { Write-Output ('ERROR:' + $_.Exception.Message) }")
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                           capture_output=True, text=True, timeout=45,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        out = (r.stdout or "").strip()
        if out.startswith("STATUS:Running"):
            return True, ""
        return False, out.replace("ERROR:", "", 1) or (r.stderr or "no response")
    except Exception as e:
        return False, str(e)
