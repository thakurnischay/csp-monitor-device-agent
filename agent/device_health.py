"""
Passbook-printer / micro-ATM health check — a lightweight, PRESENCE-based
"is it plugged in and not erroring" probe for the two peripherals a CSP relies
on, both connected to the PC by cable.

Detection approach (no vendor SDK — printer/micro-ATM models vary per CSP):
  - Passbook printer: a cabled printer is installed as a normal Windows
    printer, so its live status is read straight from the OS print spooler
    (Win32_Printer via PowerShell/WMI) — no pywin32 dependency needed.
  - Micro-ATM: a cabled micro-ATM enumerates as a USB device (card reader /
    biometric scanner) even though its own banking software is separate and
    proprietary. We check that device is present and Windows reports its
    driver status as OK (Get-PnpDevice).
  - CSP software (optional, once its process name is known): the device can
    be physically fine while the software that actually drives it has
    crashed, so we also check whether that program's process is still
    running (tasklist) and fold that into the printer/micro-ATM verdict —
    see _apply_software_signal(). Off by default until a process name is
    configured; nothing breaks if it's never set.
  - Recognition layer (resolve_printer/resolve_microatm): a human's manual
    pick is not always right (e.g. an accidentally-selected virtual PDF
    printer on a CSP with no real printer). Known-brand name matching
    silently corrects two specific cases: a manual pick that's a known-fake
    virtual printer, or a confidently-recognized real device that differs
    from whatever's manually saved. This is plain keyword matching, not a
    cloud/AI call - free, instant, works offline, deterministic. It only
    acts when there is exactly ONE matching real candidate; two or more
    matches (genuinely ambiguous) or zero matches (unrecognized/generic
    hardware) both fall back to respecting the manual pick untouched.

This is mostly a CONNECTIVITY check, not a functional test — printer_status()/
microatm_status() will not catch "out of paper" on a printer that still
reports Idle, since they never touch the device (no test print, no test
transaction), so they are cheap and safe to run on every check.

The one exception is printer_functional_test(): it actually sends a real
Windows test page to the printer and reads that job's own error state, which
catches things the connectivity check can't — out of paper, paper jam, door
open. That costs one physical sheet, so it must be rate-limited by the caller
(reporter.py runs it at most once a day) rather than on every heartbeat.

Every function here is best-effort and NEVER raises — a broken PowerShell/WMI
call must degrade to "unknown", never crash the agent.
"""
import concurrent.futures
import json
import re
import subprocess
import time

import deep_checks

_PS_TIMEOUT = 30           # one Windows scan; a weak CSP PC can need far more than 8 s
_CHECK_HARD_TIMEOUT = 75   # overall ceiling for one check(): 3 scans (run in parallel) + the GPS data read

# Win32_Printer.PrinterStatus values (Microsoft WMI reference):
# 1=Other 2=Unknown 3=Idle 4=Printing 5=Warmup 6=Stopped Printing 7=Offline
_PRINTER_OK_STATUSES = {3, 4, 5}
_PRINTER_STATUS_TEXT = {
    1: "other", 2: "unknown", 3: "idle (ready)", 4: "printing",
    5: "warming up", 6: "stopped printing", 7: "offline",
}

# PnP device classes that cover the great majority of cabled card readers,
# biometric scanners, and micro-ATM USB dongles.
_USB_CLASSES = ("USB", "HIDClass", "Ports", "SmartCardReader", "Biometric")

# Many passbook printers do NOT register as a real Windows printer at all -
# they attach through the vendor's own USB/serial driver and only ever show
# up as a COM port (Windows PnP class "Ports"). Any present device in this
# class is a plausible passbook-printer candidate regardless of whether its
# brand is recognized, so it's always offered in the manual dropdown - see
# list_all_devices()/_check_impl().
_COM_PORT_CLASSES = ("Ports",)
_PRINT_CLASSES = ("Printer", "PrintQueue", "USBPrint")

# Printers matching any of these are virtual/software printers (PDF writers,
# fax, OneNote, or ones injected by remote-access tools like UltraViewer) -
# they always report "ready" since there's no real hardware behind them, so
# a CSP with NO physical printer can end up falsely looking "ok" if one gets
# picked by mistake.
_VIRTUAL_PRINTER_HINTS = (
    "microsoft print to pdf", "microsoft xps", "fax", "onenote", "adobe pdf",
    "rustdesk", "anydesk", "teamviewer", "ultraviewer", "pdfcreator",
    "cutepdf", "dopdf", "bullzip",
)

# Known real-hardware brand/model keywords for passbook printers and
# micro-ATMs commonly used by Indian CSPs. Used only to silently recognize
# and correct an obviously wrong or missing manual pick — never to identify
# a device that isn't from a recognized brand, which still needs a human's
# one-time pick (see resolve_printer/resolve_microatm below).
_KNOWN_PRINTER_BRANDS = (
    "tvs", "wep", "epson tm", "epson l1c", "epson lx", "epson plq", "gprs",
    "passbook", "troy", "godex", "genicom", "olivetti bp", "posiflex",
    "citizen ct-s", "custom vkp", "star tsp", "hasman", "svp",
)
# Deliberately excludes pure fingerprint/biometric-scanner brands (Mantra,
# Morpho, SecuGen, Startek, Precision Biometric, Bioenable) - those are
# Aadhaar-auth devices, not micro-ATMs. A real field CSP had its Mantra
# MFS100 biometric scanner silently auto-picked as "the micro-ATM" because
# it used to be in this list, which meant the actual micro-ATM (or its
# absence) was never checked at all.
_KNOWN_MICROATM_BRANDS = (
    "ingenico", "ezetap", "mswipe", "morefun", "bijlipay", "spice digital",
    "pax tech", "verifone", "innoviti", "eko minipos",
    "electracard", "cygnet", "m2i", "wcbs", "genesys", "smart chip",
)

# Fingerprint/biometric scanners (Aadhaar auth) - vendor and model names only.
# Deliberately NO generic words like "biometric"/"fingerprint": a laptop's
# built-in Windows Hello reader sits in the same PnP class and is not the
# scanner the CSP uses for transactions.
_KNOWN_BIOMETRIC_BRANDS = (
    "mantra", "morpho", "idemia", "secugen", "startek", "precision biometric",
    "bioenable", "tatvik", "evolute", "aratek", "next biometrics", "cogent",
    "mfs100", "mfs110", "mis100", "fm220", "mso1300", "mso 1300",
    "suprema", "digitalpersona", "futronic",
)

# USB GPS dongles (the "GPS Dongle Status" column in the ops Calling Sheet).
# Many dongles use a generic USB-serial chip and show up as just "USB-SERIAL
# CH340 (COM3)" with no GPS in the name - those can't be recognized by name
# and need a human's one-time pick from the dropdown, same as COM-port printers.
_KNOWN_GPS_BRANDS = (
    "gps", "gnss", "u-blox", "ublox", "g-mouse", "globalsat", "navilock",
    "garmin", "sirf",
)
_GPS_CANDIDATE_CLASSES = ("Ports", "Sensor", "Location")


def run_with_ceiling(func, timeout: float, *args, **kwargs):
    """Run `func` with a hard wall-clock ceiling, regardless of what's
    blocking underneath. subprocess.run's own `timeout` parameter is not
    enough on its own: it only bounds the WAIT after the child process has
    already spawned, so if something on this PC (Smart App Control, an
    antivirus product) intercepts and stalls the process launch itself
    before that wait even starts, subprocess.run can still hang far longer
    than its own timeout - a real failure mode already seen once on an
    actual CSP PC in this project. Returns None if `func` doesn't finish in
    time; the stuck worker thread is abandoned (Python has no safe way to
    kill a thread), which is an acceptable rare cost for guaranteeing the
    caller (an HTTP request) always gets a response. Never raises."""
    ex = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    future = ex.submit(func, *args, **kwargs)
    try:
        result = future.result(timeout=timeout)
        ex.shutdown(wait=False)
        return result
    except Exception:
        ex.shutdown(wait=False)
        return None


def is_likely_virtual_printer(name: str) -> bool:
    n = (name or "").lower()
    return any(hint in n for hint in _VIRTUAL_PRINTER_HINTS)


# Real field case: "IngenicoEnum" got auto-picked as "the micro-ATM" even
# though nothing was physically plugged in. Ingenico's own driver/SDK
# installs a persistent virtual bus/enumerator placeholder device that stays
# present in Windows regardless of whether real hardware is attached
# downstream - the "Enum" suffix is the standard Windows convention for this
# kind of software-only placeholder, never a real marketed product name, so
# it must never be picked as if it were the actual device.
_VIRTUAL_USB_HINTS = ("enum",)


def is_likely_virtual_usb_device(name: str) -> bool:
    n = (name or "").lower()
    return any(hint in n for hint in _VIRTUAL_USB_HINTS)


def _best_brand_match(items: list, brand_keywords: tuple):
    """The single item whose name matches a known brand keyword, or None if
    still ambiguous after the tiebreaker below — an ambiguous or unrecognized
    result must never be silently guessed at, only an unambiguous one.

    Real field case: a CSP had TWO Epson PLQ printer entries in Windows (the
    real, currently-connected one, plus a stale leftover definition from a
    previous printer) - both matched the same brand keyword, so this used to
    give up entirely and leave the printer unconfigured despite the real one
    being right there. If exactly one candidate is actually present/OK and
    the rest are not, that live one is the answer - a real "is it plugged in
    and working" signal, not a guess between two equally-plausible names."""
    matches = [item for item in items
              if any(kw in search_text(item) for kw in brand_keywords)]
    if len(matches) == 1:
        return matches[0]["name"]
    if len(matches) > 1:
        live = [item for item in matches if item.get("ok")]
        if len(live) == 1:
            return live[0]["name"]
    return None


def auto_detect_printer(printers: list = None) -> str:
    """Best-guess real passbook printer by known brand name, or "" if none or
    ambiguous. Pass an already-fetched `printers` list (from list_printers())
    to avoid spawning another PowerShell scan — every caller in this module
    that already has one MUST pass it; a fresh scan is the fallback only for
    a caller with nothing cached yet. Never raises."""
    try:
        printers = list_printers() if printers is None else printers
        real = [p for p in printers if not is_likely_virtual_printer(p["name"])]
        return _best_brand_match(real, _KNOWN_PRINTER_BRANDS) or ""
    except Exception:
        return ""


def auto_detect_microatm(usb_devices: list = None) -> str:
    """Best-guess real micro-ATM by known brand name, or "" if none or
    ambiguous. Pass an already-fetched `usb_devices` list (from
    list_usb_devices()) to avoid spawning another PowerShell scan. Never
    raises."""
    try:
        usb_devices = list_usb_devices() if usb_devices is None else usb_devices
        real = [d for d in usb_devices if not is_likely_virtual_usb_device(d["name"])]
        return _best_brand_match(real, _KNOWN_MICROATM_BRANDS) or ""
    except Exception:
        return ""


def resolve_printer(manual_name: str, printers: list = None) -> str:
    """The printer name to actually use for the health check: corrects an
    accidentally-selected virtual printer, and prefers a confidently
    recognized real device over the manual pick when they differ. Falls back
    to the manual pick untouched whenever recognition can't confidently do
    better. Pass an already-fetched `printers` list to reuse one scan instead
    of triggering another. Never raises."""
    try:
        manual_name = (manual_name or "").strip()
        auto = auto_detect_printer(printers)
        if manual_name and is_likely_virtual_printer(manual_name):
            return auto or manual_name
        if auto and auto != manual_name:
            return auto
        return manual_name
    except Exception:
        return manual_name


def resolve_microatm(manual_name: str, usb_devices: list = None) -> str:
    """Same idea as resolve_printer, for the micro-ATM. Never raises."""
    try:
        manual_name = (manual_name or "").strip()
        auto = auto_detect_microatm(usb_devices)
        if manual_name and is_likely_virtual_usb_device(manual_name):
            return auto or manual_name
        if auto and auto != manual_name:
            return auto
        return manual_name
    except Exception:
        return manual_name


def _run_ps(script: str, timeout: int = _PS_TIMEOUT):
    """Run a PowerShell script, return parsed JSON (list/dict) or None on any
    failure (PowerShell missing, timeout, empty/bad output). Never raises."""
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        out = (r.stdout or "").strip()
        if not out:
            return None
        return json.loads(out)
    except Exception:
        return None


def _as_list(data) -> list:
    """ConvertTo-Json returns a bare object (not a list) when there is exactly
    one result — normalise to a list either way."""
    if data is None:
        return []
    return data if isinstance(data, list) else [data]


# USB vendor IDs we are CONFIDENT about (a wrong entry would mislabel a real
# device, so this is deliberately short). Used only to put a human-readable
# maker next to a generic Windows name like "USB Serial Device (COM5)".
_USB_VENDORS = {
    # generic USB-serial chips: they name the CHIP, not what is plugged into it
    "0403": "FTDI", "067B": "Prolific", "1A86": "QinHeng (CH340)", "10C4": "Silicon Labs",
    # makers of the actual devices. The USB vendor id is set by the device's own
    # maker, so it identifies the brand even when Windows shows a generic name
    # like "USB Input Device". 2C0F and 225D were seen on real CSP scanners
    # (Mantra MFS110 = 2C0F:1204, MorphoSmart MSO1300-E3 = 225D:000A).
    "2C0F": "Mantra Softech", "225D": "Morpho/Idemia", "079B": "Sagem/Morpho",
    "1162": "SecuGen", "16D1": "Suprema", "05BA": "DigitalPersona", "1491": "Futronic",
    "0B00": "Ingenico", "11CA": "Verifone",
    "1546": "u-blox", "091E": "Garmin",
}
_VID_PID = re.compile(r"VID_([0-9A-F]{4})&PID_([0-9A-F]{4})", re.I)
_MI = re.compile(r"&MI_([0-9A-F]{2})", re.I)
# Manufacturer values Windows fills in for its own generic drivers - they say
# nothing about who really made the device.
_GENERIC_MAKERS = ("microsoft", "generic", "(standard", "compatible usb",
                   "standard system devices", "windows")


def _enrich_device(name: str, raw: dict) -> dict:
    """One PnP row -> the dict the rest of the module uses, plus the extra
    identifying detail (maker, USB vendor/product id) the setup page shows so
    a human can tell which generic-looking entry is which."""
    status = str(raw.get("Status") or "").strip()
    maker = str(raw.get("Manufacturer") or "").strip()
    if not maker or any(g in maker.lower() for g in _GENERIC_MAKERS):
        maker = ""
    m = _VID_PID.search(str(raw.get("InstanceId") or ""))
    vid, pid = (m.group(1).upper(), m.group(2).upper()) if m else ("", "")
    return {"name": name, "class": str(raw.get("Class") or "").strip(),
            "ok": status.upper() == "OK", "status": status or "unknown",
            "manufacturer": maker, "vid": vid, "pid": pid,
            "vendor": _USB_VENDORS.get(vid, ""),
            "instance": str(raw.get("InstanceId") or ""),
            "mi": (_MI.search(str(raw.get("InstanceId") or "")).group(1).upper()
                   if _MI.search(str(raw.get("InstanceId") or "")) else "")}


def search_text(d: dict) -> str:
    """Everything that could name this device's brand, lowercased: Windows
    often gives a generic name but the right maker."""
    return " ".join(str(d.get(k) or "") for k in ("name", "manufacturer", "vendor")).lower()


def device_label(d: dict) -> str:
    """Dropdown caption: the name plus whatever identifies it better -
    maker and USB id - e.g. "USB Serial Device (COM5) | Prolific | USB 067B:2303"."""
    parts = [d.get("name", "")]
    maker = d.get("manufacturer") or d.get("vendor") or ""
    if maker and maker.lower() not in parts[0].lower():
        parts.append(maker)
    if d.get("vid"):
        parts.append(f"USB {d['vid']}:{d.get('pid', '')}")
    return " | ".join(p for p in parts if p)


def rank_devices(devices: list, brands: tuple, preferred_classes: tuple = ()) -> list:
    """Most-likely-first ordering for a dropdown: recognised brand, then the
    device classes this kind of hardware normally uses, then everything else
    (alphabetical). Nothing is hidden - only reordered."""
    def key(d):
        if any(kw in search_text(d) for kw in brands):
            rank = 0
        elif d.get("class") in preferred_classes:
            rank = 1 + preferred_classes.index(d["class"])   # earlier class = likelier
        else:
            rank = 1 + len(preferred_classes)
        return (rank, d.get("name", "").lower())
    return sorted(devices, key=key)


def is_recognised(d: dict, brands: tuple) -> bool:
    return bool(d) and any(kw in search_text(d) for kw in brands)


def list_printers() -> list:
    """Every printer Windows currently knows about (installed, not necessarily
    powered on). [{"name", "ok", "status"}] — best-effort, never raises."""
    data = _run_ps(
        "Get-CimInstance -ClassName Win32_Printer | "
        "Select-Object Name, PrinterStatus, WorkOffline | ConvertTo-Json -Compress")
    out = []
    for p in _as_list(data):
        if not isinstance(p, dict):
            continue
        name = str(p.get("Name") or "").strip()
        if not name:
            continue
        status = p.get("PrinterStatus")
        offline = bool(p.get("WorkOffline"))
        ok = (not offline) and status in _PRINTER_OK_STATUSES
        text = "offline (set to work offline)" if offline else _PRINTER_STATUS_TEXT.get(
            status, f"status {status}" if status is not None else "unknown")
        out.append({"name": name, "ok": ok, "status": text})
    return out


def list_usb_devices() -> list:
    """Every currently-PRESENT plug-and-play device in a USB-ish class.
    [{"name", "ok", "status"}] — best-effort, never raises."""
    class_filter = ",".join(f"'{c}'" for c in _USB_CLASSES)
    data = _run_ps(
        "Get-PnpDevice -PresentOnly | "
        f"Where-Object {{ $_.Class -in @({class_filter}) }} | "
        "Select-Object FriendlyName, Class, Status, Manufacturer, InstanceId | ConvertTo-Json -Compress")
    out = []
    for d in _as_list(data):
        if not isinstance(d, dict):
            continue
        name = str(d.get("FriendlyName") or "").strip()
        if not name:
            continue
        out.append(_enrich_device(name, d))
    return out


def list_all_devices() -> list:
    """Every currently-PRESENT plug-and-play device, ANY class, unfiltered -
    the broadest possible view of what's connected. Used as a fallback source
    for both printer and micro-ATM recognition: list_printers() only sees
    devices registered as a real Windows printer, and list_usb_devices() only
    checks a specific set of classes - a device that attaches via a vendor's
    own USB/serial driver without fitting either of those (very common for
    passbook printers, which often show up as a plain COM port) would
    otherwise be invisible to this agent no matter how good the brand list
    is. [{"name", "class", "ok", "status"}] — best-effort, never raises."""
    data = _run_ps(
        "Get-PnpDevice -PresentOnly | "
        "Select-Object FriendlyName, Class, Status, Manufacturer, InstanceId | ConvertTo-Json -Compress")
    out = []
    for d in _as_list(data):
        if not isinstance(d, dict):
            continue
        name = str(d.get("FriendlyName") or "").strip()
        if not name:
            continue
        out.append(_enrich_device(name, d))
    return out


def _name_prefix(name: str) -> str:
    """Everything but the LAST whitespace-separated word, lowercased - the
    "same device, different trailing detail" signature. Two real field cases
    this exists for:
      - A COM-port printer's name carries the port number, which can shift
        after a replug/driver reset: "TVS RP3200 (COM4)" -> "TVS RP3200 (COM6)".
      - A CSP typed the printer's physical model label, which differs from
        the interface suffix Windows' own driver name uses: "EPSON PLQ-50 CSK"
        (typed) vs "EPSON PLQ-50 ESC/P2" (actual Windows driver name) - both
        share the "EPSON PLQ-50" prefix.
    Returns "" unless at least TWO words remain after dropping the last one -
    a single generic word (e.g. just "EPSON") is too permissive to match on,
    so that case is deliberately excluded from the fallback below."""
    parts = (name or "").strip().split()
    return " ".join(parts[:-1]).lower() if len(parts) > 2 else ""


def _find_by_name_or_prefix(configured_name: str, devices: list):
    """Exact name match first; if that finds nothing, fall back to the
    "same prefix, different last word" match ONLY when it is unambiguous
    (exactly one present device shares that prefix) - otherwise still
    "not found", never a guess between two genuinely different devices."""
    for d in devices:
        if d["name"] == configured_name:
            return d
    prefix = _name_prefix(configured_name)
    if not prefix:
        return None
    matches = [d for d in devices if _name_prefix(d["name"]) == prefix]
    return matches[0] if len(matches) == 1 else None


def printer_status(configured_name: str, printers: list = None) -> dict:
    """Health of the ONE printer the CSP configured as their passbook printer.
    Pass an already-fetched `printers` list to reuse one scan instead of
    triggering another. {"configured", "present", "ok", "status"} — never
    raises."""
    configured_name = (configured_name or "").strip()
    if not configured_name:
        return {"configured": False, "present": False, "ok": False, "status": "not configured"}
    printers = list_printers() if printers is None else printers
    match = _find_by_name_or_prefix(configured_name, printers)
    if match:
        return {"configured": True, "present": True, "ok": match["ok"], "status": match["status"],
                "recognised": is_recognised(match, _KNOWN_PRINTER_BRANDS)}
    return {"configured": True, "present": False, "ok": False,
            "status": "not found — check the printer is connected and installed"}


def microatm_status(configured_name: str, usb_devices: list = None) -> dict:
    """Health of the ONE USB device the CSP configured as their micro-ATM.
    Pass an already-fetched `usb_devices` list to reuse one scan instead of
    triggering another. {"configured", "present", "ok", "status"} — never
    raises."""
    configured_name = (configured_name or "").strip()
    if not configured_name:
        return {"configured": False, "present": False, "ok": False, "status": "not configured"}
    usb_devices = list_usb_devices() if usb_devices is None else usb_devices
    match = _find_by_name_or_prefix(configured_name, usb_devices)
    if match:
        return {"configured": True, "present": True, "ok": match["ok"], "status": match["status"],
                "recognised": is_recognised(match, _KNOWN_MICROATM_BRANDS)}
    return {"configured": True, "present": False, "ok": False,
            "status": "not found — check the device is plugged in"}


def _auto_detect_by_brand(devices: list, brands: tuple) -> str:
    real = [d for d in devices if not is_likely_virtual_usb_device(d["name"])]
    return _best_brand_match(real, brands) or ""


def _resolve_keeping_a_working_pick(manual_name: str, devices: list, auto: str) -> str:
    """Biometric/GPS selection rule: a manual pick that is a real, currently
    present device is never overridden - auto-detection only fills in when
    the pick is blank, a software placeholder, or no longer present. (The
    printer/micro-ATM resolvers override any differing manual pick, which has
    caused wrong silent re-selections in the field; the newer devices do not
    inherit that.)"""
    manual_name = (manual_name or "").strip()
    if (manual_name and not is_likely_virtual_usb_device(manual_name)
            and _find_by_name_or_prefix(manual_name, devices)):
        return manual_name
    return auto or manual_name


def auto_detect_biometric(devices: list = None) -> str:
    """Best-guess fingerprint scanner by known vendor/model, or "" if none or
    ambiguous. Never raises."""
    try:
        devices = list_usb_devices() if devices is None else devices
        return _auto_detect_by_brand(devices, _KNOWN_BIOMETRIC_BRANDS)
    except Exception:
        return ""


def auto_detect_gps(devices: list = None) -> str:
    """Best-guess GPS dongle by name, or "" if none/ambiguous/generic serial
    chip. Never raises."""
    try:
        devices = list_usb_devices() if devices is None else devices
        return _auto_detect_by_brand(devices, _KNOWN_GPS_BRANDS)
    except Exception:
        return ""


def resolve_biometric(manual_name: str, devices: list = None) -> str:
    try:
        devices = list_usb_devices() if devices is None else devices
        return _resolve_keeping_a_working_pick(
            manual_name, devices, auto_detect_biometric(devices))
    except Exception:
        return (manual_name or "").strip()


def resolve_gps(manual_name: str, devices: list = None) -> str:
    try:
        devices = list_usb_devices() if devices is None else devices
        return _resolve_keeping_a_working_pick(
            manual_name, devices, auto_detect_gps(devices))
    except Exception:
        return (manual_name or "").strip()


def _device_status(configured_name: str, devices: list, not_found: str, brands: tuple = ()) -> dict:
    configured_name = (configured_name or "").strip()
    if not configured_name:
        return {"configured": False, "present": False, "ok": False, "status": "not configured"}
    match = _find_by_name_or_prefix(configured_name, devices)
    if match:
        # `recognised`: the picked device really looks like this kind of
        # hardware (brand/maker match). A pick that does not is only a
        # driver-level "plugged in" - deep_checks.py says so on the status.
        return {"configured": True, "present": True, "ok": match["ok"], "status": match["status"],
                "recognised": is_recognised(match, brands)}
    return {"configured": True, "present": False, "ok": False, "status": not_found}


def biometric_status(configured_name: str, devices: list = None) -> dict:
    """Connectivity health of the CSP's fingerprint scanner (present + Windows
    reports the driver OK). {"configured", "present", "ok", "status"} - never
    raises."""
    devices = list_usb_devices() if devices is None else devices
    return _device_status(configured_name, devices,
                          "not found — check the fingerprint scanner is plugged in",
                          _KNOWN_BIOMETRIC_BRANDS)


def gps_status(configured_name: str, devices: list = None) -> dict:
    """Connectivity health of the CSP's GPS dongle. Never raises."""
    devices = list_usb_devices() if devices is None else devices
    return _device_status(configured_name, devices,
                          "not found — check the GPS dongle is plugged in",
                          _KNOWN_GPS_BRANDS)


def printer_functional_test(printer_name: str, wait_seconds: int = 6) -> dict:
    """Sends a real Windows test page to the named printer, then reads that
    print job's own status for problems invisible to printer_status() alone —
    out of paper, paper jam, door/cover open. This is the one function in this
    module that actually touches the device: it costs one physical sheet, so
    the CALLER must rate-limit it (reporter.py: once a day), not this
    function itself. {"ran", "ok", "detail"} — never raises."""
    printer_name = (printer_name or "").strip()
    if not printer_name:
        return {"ran": False, "ok": False, "detail": "no printer configured"}
    try:
        subprocess.run(
            ["rundll32", "printui.dll,PrintUIEntry", "/k", "/n", printer_name],
            capture_output=True, text=True, timeout=_PS_TIMEOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception as e:
        return {"ran": False, "ok": False, "detail": f"could not send test page: {e}"}
    time.sleep(wait_seconds)
    data = _run_ps(
        "Get-CimInstance -ClassName Win32_PrintJob | "
        "Select-Object Name, Status | ConvertTo-Json -Compress")
    problems = set()
    for j in _as_list(data):
        if not isinstance(j, dict):
            continue
        name = str(j.get("Name") or "")
        if not name.startswith(printer_name + ","):
            continue
        status = str(j.get("Status") or "").strip()
        # "Unknown" is the CIM Status property's generic default when the
        # print subsystem has nothing specific to report - many drivers
        # (especially older dot-matrix/ESC-P2 passbook printer drivers) leave
        # it as "Unknown" for jobs that printed successfully, not just for
        # failed ones. A real field case: a CSP's printer worked and the
        # connectivity check agreed, but the daily test print alone showed
        # "problem: UNKNOWN" - a false positive from treating this generic
        # default as if it were a specific error like "Paper Out"/"Door Open".
        if status and status.lower() not in ("printing", "spooling", "printed", "normal", "unknown"):
            problems.add(status)
    if problems:
        return {"ran": True, "ok": False, "detail": "test page problem: " + ", ".join(sorted(problems))}
    return {"ran": True, "ok": True, "detail": "test page sent — printer accepted it with no reported error"}


def process_running(process_name: str) -> dict:
    """Best-effort check for whether a process with this exact image name
    (e.g. "MicroATM.exe", as seen in Task Manager's Details tab) is currently
    running. {"configured", "running"} — never raises. Uses tasklist (plain
    ASCII, built into Windows) rather than PowerShell to sidestep the
    encoding pitfalls that broke a different script in this project."""
    process_name = (process_name or "").strip()
    if not process_name:
        return {"configured": False, "running": False}
    try:
        r = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {process_name}", "/NH"],
            capture_output=True, text=True, timeout=_PS_TIMEOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        running = process_name.lower() in (r.stdout or "").lower()
        return {"configured": True, "running": running}
    except Exception:
        return {"configured": True, "running": False}


def _apply_software_signal(hw: dict, software: dict) -> dict:
    """Fold the CSP software's running-state into a hardware verdict: a
    device can be physically present and fine but useless if the software
    driving it has crashed. Only adds a reason when the hardware check is
    otherwise fine and a process name IS configured but NOT running — never
    touches an already-"not configured"/"not present" hardware result, and
    never applies when no process name has been set."""
    if not hw.get("configured") or not hw.get("present") or not hw.get("ok"):
        return hw
    if software.get("configured") and not software.get("running"):
        out = dict(hw)
        out["ok"] = False
        out["status"] = hw["status"] + " (but the CSP software isn't running)"
        return out
    return hw


_DEVICE_LABELS = {"printer": "passbook printer", "microatm": "micro-ATM",
                  "biometric": "fingerprint scanner", "gps": "GPS dongle"}


def apply_verification(health: dict, confirmed: dict = None, functional: dict = None) -> dict:
    """Decide, for each of the four devices, whether we actually KNOW it is
    that kind of device - and say so, so the dashboard never shows a green OK
    for something that was merely picked from a list.

    "OK" from Windows only means "this entry is plugged in and its driver is
    fine". A generic entry such as "USB Input Device" is also a keyboard or a
    mouse, so on its own that proves nothing about a scanner or printer.
    A device counts as VERIFIED only through real evidence:
      - the scanner's RD service answered, or the GPS dongle is streaming data;
      - its name/maker is a known brand of that kind of device;
      - the person identified it with the plug-in test ("Find my device");
      - the printer accepted today's test print, or (micro-ATM) the CSP
        software that drives it is running.
    Everything else stays UNVERIFIED (verified=False). Only devices that are
    configured, present and healthy are judged - problems and "not set up"
    states are left exactly as they are."""
    confirmed = confirmed or {}
    functional = functional or {}
    out = dict(health)
    windows_printers = set(health.get("windows_printer_names", []))
    software = health.get("software") or {}
    for dev in ("printer", "microatm", "biometric", "gps"):
        d = dict(health[dev])
        out[dev] = d
        if not (d.get("configured") and d.get("present") and d.get("ok")):
            continue
        name = d.get("resolved_name", "")
        conf = confirmed.get(dev) or {}
        by = ""
        if d.get("verified"):
            by = d.get("verified_by") or "the device itself"
        elif d.get("identity_confirmed") or (conf.get("name") and conf["name"] == name):
            by = "plug-in test"
        elif d.get("recognised"):
            by = "known brand"
        elif (dev == "printer" and name in windows_printers
              and not is_likely_virtual_printer(name)
              and functional.get("ran") and functional.get("ok")):
            by = "test print"
        elif dev == "microatm" and software.get("configured") and software.get("running"):
            by = "CSP software running"
        d["verified"] = bool(by)
        d["verified_by"] = by
        if not by:
            d["status"] = ("UNVERIFIED - cannot confirm this is the " + _DEVICE_LABELS[dev]
                           + ". Windows shows the entry as plugged in (driver status: "
                           + d["status"] + "), but a generic entry could be any device. "
                           "Pick the right entry, or use 'Find my device' (plug it in and see what appears).")
    return out


def check(printer_name: str, microatm_name: str, software_process: str = "",
          biometric_name: str = "", gps_name: str = "", confirmed: dict = None) -> dict:
    """Combined snapshot for the reporter loop and the local status page.
    Wrapped in run_with_ceiling() so this ALWAYS returns within
    _CHECK_HARD_TIMEOUT seconds no matter what's blocking underneath (Smart
    App Control/antivirus stalling a spawned process, a genuinely hung WMI
    query, ...) - the caller (an HTTP request) must never hang forever.
    Never raises."""
    result = run_with_ceiling(_check_impl, _CHECK_HARD_TIMEOUT,
                              printer_name, microatm_name, software_process,
                              biometric_name, gps_name, confirmed)
    if result is not None:
        return result
    timed_out_msg = ("The device check did not finish in time - the PC is very slow or a "
                     "security tool (Smart App Control / antivirus) is blocking it. Wait a "
                     "minute and refresh; you can still type each device name yourself.")
    timed_out = {"configured": False, "present": False, "ok": False,
                "status": "scan timed out - a security tool on this PC "
                          "(Smart App Control/antivirus) may be blocking the check"}
    return {"printer": dict(timed_out), "microatm": dict(timed_out),
            "biometric": dict(timed_out), "gps": dict(timed_out),
            "software": {"configured": False, "running": False},
            "scan_problem": timed_out_msg,
            "printer_list": [], "usb_device_list": [],
            "biometric_device_list": [], "gps_device_list": []}


_last_good = {"printers": [], "usb": [], "all": []}


def _prefer_recognised_same_name(status: dict, devices: list, resolved_name: str, brands: tuple) -> dict:
    """Several entries can share one generic name ("USB Input Device": a
    keyboard, a mouse, a scanner...). Looking a device up by name alone would
    judge the FIRST of them. If exactly one of the same-named entries is
    recognised by its maker / USB vendor id, that one is the device."""
    try:
        if not (status.get("configured") and resolved_name):
            return status
        same = [d for d in devices if d.get("name") == resolved_name]
        if len(same) < 2:
            return status
        known = [d for d in same if is_recognised(d, brands)]
        if len(known) != 1:
            return status
        d = known[0]
        out = dict(status)
        out.update({"present": True, "ok": d["ok"], "status": d["status"], "recognised": True})
        return out
    except Exception:
        return status


def fingerprint(name: str, instance: str) -> dict:
    """What identifies THIS hardware regardless of which USB port it is in:
    USB vendor id + product id (+ interface number for multi-function
    devices). A device's Windows "instance id" often embeds the physical
    port (for devices without a serial number), and a COM port's number can
    change too - so neither is a stable identity on its own."""
    m = _VID_PID.search(instance or "")
    mi = _MI.search(instance or "")
    return {"vid": m.group(1).upper() if m else "", "pid": m.group(2).upper() if m else "",
            "mi": mi.group(1).upper() if mi else "",
            "base": re.sub(r"\s*\(COM\d+\)", "", name or "").strip().lower()}


def _same_hardware(d: dict, fp: dict) -> bool:
    if fp.get("vid"):
        return (d.get("vid") == fp["vid"] and d.get("pid") == fp["pid"]
                and (not fp.get("mi") or d.get("mi") == fp["mi"]))
    return bool(fp.get("base")) and fingerprint(d.get("name", ""), "")["base"] == fp["base"]


def _resolve_confirmed_device(status: dict, devices: list, resolved_name: str, conf: dict):
    """Track the device the person identified with the plug-in test.

    Returns (status, moved_to). Tried in order:
      1. the exact same Windows instance (same device, same port);
      2. the same HARDWARE (USB vendor+product id [+interface]) showing up
         under a new instance id or COM number - i.e. it was moved to another
         USB port (or replugged and Windows renumbered it). If exactly one such
         device is present it is the one: found again automatically, and
         `moved_to` tells the caller to remember the new instance/name;
      3. nothing matches - it really is not connected (or was replaced by a
         different model, which needs Find my device again).
    Generic names ("USB Input Device") are shared by many devices, which is why
    the name alone is never used for a confirmed device."""
    try:
        if not conf or not conf.get("name") or conf.get("name") != resolved_name:
            return status, None
        inst = conf.get("instance") or ""
        fp = conf.get("fingerprint") or {}
        if inst:
            for d in devices:
                if d.get("instance") == inst:
                    out = dict(status)
                    out.update({"configured": True, "present": True, "ok": d["ok"], "status": d["status"],
                                "identity_confirmed": True})
                    return out, None
        if fp:
            hits = [d for d in devices if _same_hardware(d, fp)]
            several = len(hits) > 1
            if several:            # identical interfaces: prefer one whose name still matches
                same_name = [d for d in hits if d.get("name") == conf["name"]]
                hits = same_name or hits
            if hits:
                d = hits[0]
                out = dict(status)
                note = " (found again: it was moved to a different USB port)"
                if several:
                    note = " (several identical devices are connected - tracking one of them)"
                out.update({"configured": True, "present": True, "ok": d["ok"],
                            "status": d["status"] + note, "identity_confirmed": True})
                return out, {"name": d["name"], "instance": d.get("instance", ""),
                             "fingerprint": fingerprint(d["name"], d.get("instance", ""))}
        out = dict(status)
        out.update({"configured": True, "present": False, "ok": False,
                    "status": "not found - the device you identified is not connected right now. Plug it "
                              "back in; if you replaced it with a different model, use Find my device again."})
        return out, None
    except Exception:
        return status, None


def _scan_everything():
    """The three Windows scans, run IN PARALLEL (each is mostly PowerShell
    start-up time, so on a slow PC this is ~3x faster than one after another).

    Returns (printers, usb_devices, all_devices, problem). A scan that returns
    nothing is NOT the same as "no devices": Windows always has dozens of
    devices, so an empty device list means the scan was blocked, timed out or
    failed. In that case the last good list is reused (if any) and `problem`
    says so, so the setup page can tell the person instead of silently showing
    an empty dropdown."""
    out = {}
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=3)
    futures = {"printers": pool.submit(list_printers),
               "usb": pool.submit(list_usb_devices),
               "all": pool.submit(list_all_devices)}
    for key, fut in futures.items():
        try:
            out[key] = fut.result()
        except Exception:
            out[key] = []
    pool.shutdown(wait=False)   # never block on a stuck scan thread
    problem = ""
    # The full device list is the canary: Windows always has dozens of
    # devices, so an empty one means the scan itself failed (timed out,
    # blocked, PowerShell restricted). Printers/USB lists can legitimately be
    # short or empty, so they are only restored when the WHOLE scan failed.
    if not out["all"]:
        if _last_good["all"]:
            for key in ("printers", "usb", "all"):
                out[key] = list(_last_good[key])
            problem = ("The latest Windows device scan did not answer in time, so the "
                       "last successful list is being shown. The PC may be slow or busy.")
        else:
            problem = ("Windows did not return the list of connected devices (the scan "
                       "timed out, failed, or is blocked by antivirus / Smart App Control). "
                       "Wait a minute and refresh. If this stays, run DIAGNOSE.bat and send "
                       "the file to your admin. You can still type each device name yourself.")
    else:
        for key in ("printers", "usb", "all"):
            _last_good[key] = list(out[key])
    return out["printers"], out["usb"], out["all"], problem


def _check_impl(printer_name: str, microatm_name: str, software_process: str = "",
                biometric_name: str = "", gps_name: str = "", confirmed: dict = None) -> dict:
    """The actual check() body, run inside run_with_ceiling()'s worker thread.
    The manual printer_name/microatm_name picks are passed through
    resolve_printer/resolve_microatm first, so a wrong or stale manual
    selection gets silently corrected before the health check even runs.

    Scans the printer list and the USB device list exactly ONCE each here and
    reuses them for both resolution and the status lookup - resolve_printer()
    and printer_status() used to each trigger their own separate PowerShell
    scan (3 scans total per call once auto-detection was added), which on a
    slow/real CSP PC (weak hardware, antivirus inspecting every new process)
    was slow enough to make the local status page look stuck on "checking...".
    The result also carries the raw lists (printer_list/usb_device_list) so
    local_ui.py's dropdown menus can reuse them too instead of scanning again.

    Also merges in list_all_devices() candidates that list_printers()/
    list_usb_devices() alone would miss - a passbook printer that only shows
    up as a raw COM port (no real Windows printer registered at all), or
    either device sitting in a PnP class outside the ones list_usb_devices()
    checks but still matching a known brand keyword. This is what lets
    recognition and the manual dropdown both work for hardware that attaches
    through a vendor's own USB/serial driver, not just a proper Windows
    printer queue."""
    printers, usb_devices, all_devices, scan_problem = _scan_everything()

    printer_names_seen = {p["name"] for p in printers}
    windows_printer_names = sorted(printer_names_seen)   # real installed printers only
    extra_printer_candidates = []
    for d in all_devices:
        if d["name"] in printer_names_seen:
            continue
        # A passbook printer often is NOT an installed Windows printer: it
        # can be only a COM port, a bare "USB Printing Support" entry, or
        # recognisable just by its maker/brand. Offer all of those.
        if (d["class"] in _COM_PORT_CLASSES or d["class"] in _PRINT_CLASSES
                or "printing support" in d["name"].lower()
                or any(kw in search_text(d) for kw in _KNOWN_PRINTER_BRANDS)):
            extra_printer_candidates.append(d)
            printer_names_seen.add(d["name"])
    printers = printers + extra_printer_candidates

    usb_names_seen = {u["name"] for u in usb_devices}
    extra_usb_candidates = []
    for d in all_devices:
        if d["name"] in usb_names_seen:
            continue
        if any(kw in d["name"].lower() for kw in _KNOWN_MICROATM_BRANDS):
            extra_usb_candidates.append(d)
            usb_names_seen.add(d["name"])
    usb_devices = usb_devices + extra_usb_candidates

    # Biometric/GPS candidates come from the SAME three scans - no extra
    # PowerShell calls (ScanEfficiencyTests pins the count at exactly 3).
    bio_devices = list(usb_devices)
    bio_seen = {d["name"] for d in bio_devices}
    for d in all_devices:
        if d["name"] not in bio_seen and any(kw in search_text(d) for kw in _KNOWN_BIOMETRIC_BRANDS):
            bio_devices.append(d)
            bio_seen.add(d["name"])
    gps_devices = list(usb_devices)
    gps_seen = {d["name"] for d in gps_devices}
    for d in all_devices:
        if d["name"] in gps_seen:
            continue
        if d["class"] in _GPS_CANDIDATE_CLASSES or any(kw in search_text(d) for kw in _KNOWN_GPS_BRANDS):
            gps_devices.append(d)
            gps_seen.add(d["name"])

    bio_devices = rank_devices(bio_devices, _KNOWN_BIOMETRIC_BRANDS, ("Biometric", "HIDClass"))
    gps_devices = rank_devices(gps_devices, _KNOWN_GPS_BRANDS, _GPS_CANDIDATE_CLASSES)
    def _confirmed_name(dev, resolved):
        return ((confirmed or {}).get(dev) or {}).get("name") or resolved

    resolved_printer_name = _confirmed_name("printer", resolve_printer(printer_name, printers))
    resolved_microatm_name = _confirmed_name("microatm", resolve_microatm(microatm_name, usb_devices))
    resolved_biometric_name = _confirmed_name("biometric", resolve_biometric(biometric_name, bio_devices))
    resolved_gps_name = _confirmed_name("gps", resolve_gps(gps_name, gps_devices))
    try:
        printer = printer_status(resolved_printer_name, printers)
    except Exception:
        printer = {"configured": False, "present": False, "ok": False, "status": "check failed"}
    try:
        microatm = microatm_status(resolved_microatm_name, usb_devices)
    except Exception:
        microatm = {"configured": False, "present": False, "ok": False, "status": "check failed"}
    try:
        software = process_running(software_process)
    except Exception:
        software = {"configured": False, "running": False}
    try:
        biometric = biometric_status(resolved_biometric_name, bio_devices)
    except Exception:
        biometric = {"configured": False, "present": False, "ok": False, "status": "check failed"}
    try:
        gps = gps_status(resolved_gps_name, gps_devices)
    except Exception:
        gps = {"configured": False, "present": False, "ok": False, "status": "check failed"}
    microatm = _prefer_recognised_same_name(microatm, usb_devices, resolved_microatm_name, _KNOWN_MICROATM_BRANDS)
    biometric = _prefer_recognised_same_name(biometric, bio_devices, resolved_biometric_name, _KNOWN_BIOMETRIC_BRANDS)
    gps = _prefer_recognised_same_name(gps, gps_devices, resolved_gps_name, _KNOWN_GPS_BRANDS)
    microatm, moved_microatm = _resolve_confirmed_device(
        microatm, usb_devices, resolved_microatm_name, (confirmed or {}).get("microatm"))
    biometric, moved_biometric = _resolve_confirmed_device(
        biometric, bio_devices, resolved_biometric_name, (confirmed or {}).get("biometric"))
    gps, moved_gps = _resolve_confirmed_device(
        gps, gps_devices, resolved_gps_name, (confirmed or {}).get("gps"))
    if moved_microatm:
        resolved_microatm_name = moved_microatm["name"]
    if moved_biometric:
        resolved_biometric_name = moved_biometric["name"]
    if moved_gps:
        resolved_gps_name = moved_gps["name"]
    printer = _apply_software_signal(printer, software)
    microatm = _apply_software_signal(microatm, software)
    biometric["resolved_name"] = resolved_biometric_name
    gps["resolved_name"] = resolved_gps_name
    if moved_microatm:
        microatm["moved_to"] = moved_microatm
    if moved_biometric:
        biometric["moved_to"] = moved_biometric
    if moved_gps:
        gps["moved_to"] = moved_gps
    # Deeper "does it actually work" checks (RD service / NMEA data) - only
    # ever tighten an otherwise-OK verdict, never raise, see deep_checks.py.
    try:
        biometric = deep_checks.apply_biometric_rd_check(biometric)
    except Exception:
        pass
    try:
        gps = deep_checks.apply_gps_nmea_check(gps)
    except Exception:
        pass
    printer["resolved_name"] = resolved_printer_name
    microatm["resolved_name"] = resolved_microatm_name
    biometric["resolved_name"] = resolved_biometric_name
    gps["resolved_name"] = resolved_gps_name
    return {"printer": printer, "microatm": microatm,
            "biometric": biometric, "gps": gps, "software": software,
            "scan_problem": scan_problem,
            "windows_printer_names": windows_printer_names,
            "printer_list": printers, "usb_device_list": usb_devices,
            "biometric_device_list": bio_devices, "gps_device_list": gps_devices}
