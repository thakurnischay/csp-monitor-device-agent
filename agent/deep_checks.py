"""Deeper "does it actually work" checks for the biometric scanner and the
GPS dongle, layered ON TOP of the plugged-in/driver-OK check in
device_health.py.

  - Biometric: Indian fingerprint scanners are driven by a vendor "RD
    Service" (Mantra, Morpho, ...) that listens on 127.0.0.1, ports
    11100-11120. Per the UIDAI RD-service spec an HTTP request with the
    method RDSERVICE returns a small XML document whose `status` is READY or
    NOTREADY. This is a status query only - it never captures a fingerprint.
  - GPS: a USB GPS dongle streams NMEA text ($GPGGA/$GPRMC ...) over a COM
    port. Reading a couple of seconds of it proves data is flowing and tells
    us whether there is a satellite fix.

Design rules (the devices were not available to test on, so these checks are
deliberately conservative):
  - They only ever run when the basic check already says present + OK, and
    they can only make the verdict WORSE for a clearly-diagnosed fault; any
    uncertainty (port busy, serial read failed, no RD service installed)
    leaves the existing verdict untouched.
  - "No satellite fix" is NOT a problem (indoors it's normal) - it is noted
    in the status text only.
  - A silent GPS dongle must be silent on TWO consecutive checks before it is
    flagged, to avoid a false alarm from one slow read.
  - Never raises, no extra dependency (stdlib + PowerShell's SerialPort).
"""
import concurrent.futures
import http.client
import re
import subprocess
import threading
import time

RD_PORTS = tuple(range(11100, 11121))
_RD_TIMEOUT = 0.6
_NMEA_BAUDS = (9600, 4800)      # most USB dongles; BU-353-style ones use 4800
_NMEA_SECONDS_PER_BAUD = 2.5
_NMEA_PS_TIMEOUT = 14
_CACHE_SECONDS = 60             # the local status page polls check() often
_SILENT_CHECKS_BEFORE_PROBLEM = 2

_lock = threading.Lock()
_rd_seen = False                # an RD service has answered at least once this run
_gps_silent = {}                # COM port -> consecutive silent reads
_cache = {}                     # key -> (timestamp, value)


def _cached(key, fn):
    """(value, fresh): fresh is False when served from the cache."""
    now = time.time()
    with _lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < _CACHE_SECONDS:
            return hit[1], False
    value = fn()
    with _lock:
        _cache[key] = (now, value)
    return value, True


def reset_state():
    """For tests."""
    global _rd_seen
    with _lock:
        _rd_seen = False
        _gps_silent.clear()
        _cache.clear()


# ----------------------------------------------------------------- biometric

_RD_TAG = re.compile(r"<\s*RDService\b([^>]*)>", re.I)
_ATTR = re.compile(r'(\w+)\s*=\s*"([^"]*)"')


def parse_rd_response(text: str):
    """{"status": "READY"|"NOTREADY"|..., "info": str} or None if `text` is
    not an RD-service reply."""
    m = _RD_TAG.search(text or "")
    if not m:
        return None
    attrs = {k.lower(): v for k, v in _ATTR.findall(m.group(1))}
    status = (attrs.get("status") or "").strip().upper()
    if not status:
        return None
    return {"status": status, "info": (attrs.get("info") or "").strip()}


def _probe_one_rd_port(port: int):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=_RD_TIMEOUT)
    try:
        conn.request("RDSERVICE", "/")
        resp = conn.getresponse()
        body = resp.read(4096).decode("utf-8", "replace")
        parsed = parse_rd_response(body)
        if parsed:
            parsed["port"] = port
        return parsed
    except Exception:
        return None
    finally:
        try:
            conn.close()
        except Exception:
            pass


def probe_rd_services() -> list:
    """Every RD service currently answering on this PC. Windows does NOT
    refuse a closed local port instantly (each one costs the full timeout),
    so all 21 ports are probed in parallel - ~one timeout in total instead
    of 21. Never raises."""
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(RD_PORTS)) as pool:
            results = list(pool.map(_probe_one_rd_port, RD_PORTS))
    except Exception:
        return []
    return [r for r in results if r]


def apply_biometric_rd_check(bio: dict) -> dict:
    """Fold the RD-service state into the biometric verdict (see module doc)."""
    global _rd_seen
    if not (bio.get("configured") and bio.get("present") and bio.get("ok")):
        return bio
    try:
        services, _ = _cached("rd", probe_rd_services)
    except Exception:
        return bio
    if services:
        with _lock:
            _rd_seen = True
        not_ready = [s for s in services if s["status"] != "READY"]
        if len(not_ready) == len(services):
            s = not_ready[0]
            out = dict(bio)
            out["ok"] = False
            out["status"] = (bio["status"] + " (but its RD service reports "
                             + s["status"] + " - the scanner is not ready to use)")
            return out
        return bio
    with _lock:
        seen_before = _rd_seen
    if seen_before:
        out = dict(bio)
        out["ok"] = False
        out["status"] = (bio["status"] + " (but its RD service is no longer "
                         "responding - restart the scanner's RD service)")
        return out
    return bio     # no RD service ever seen on this PC: nothing to compare to


# ----------------------------------------------------------------------- GPS

_COM = re.compile(r"\((COM\d+)\)", re.I)
_GGA = re.compile(r"\$G[A-Z]GGA,([^*\r\n]*)")
_RMC = re.compile(r"\$G[A-Z]RMC,([^*\r\n]*)")


def com_port_from_name(name: str):
    m = _COM.search(name or "")
    return m.group(1).upper() if m else None


def parse_nmea(text: str) -> dict:
    """{"data": bool, "fix": bool, "satellites": int|None}. `data` is True
    when any NMEA sentence was seen."""
    text = text or ""
    data = bool(re.search(r"\$G[A-Z]{4},", text))
    fix, sats = False, None
    for m in _GGA.finditer(text):
        f = m.group(1).split(",")
        try:
            if len(f) > 6 and int(f[5] or 0) > 0:
                fix = True
            if len(f) > 6 and f[6]:
                sats = int(f[6])
        except ValueError:
            pass
    for m in _RMC.finditer(text):
        f = m.group(1).split(",")
        if len(f) > 1 and f[1] == "A":
            fix = True
    return {"data": data, "fix": fix, "satellites": sats}


def _nmea_script(port: str) -> str:
    bauds = ",".join(str(b) for b in _NMEA_BAUDS)
    return (
        "$ErrorActionPreference='SilentlyContinue';"
        f"foreach($b in @({bauds})){{"
        f"$p=New-Object System.IO.Ports.SerialPort '{port}',$b,'None',8,'one';"
        "try{$p.Open()}catch{"
        "if($_.Exception.Message -match 'denied|in use|being used'){Write-Output 'RESULT:BUSY'}"
        "else{Write-Output 'RESULT:OPENFAIL'};exit};"
        f"$end=(Get-Date).AddSeconds({_NMEA_SECONDS_PER_BAUD});$buf='';"
        "while((Get-Date) -lt $end){$buf+=$p.ReadExisting();Start-Sleep -Milliseconds 150};"
        "$p.Close();"
        "if($buf -match '\\$G[A-Z]{4},'){Write-Output 'RESULT:DATA';Write-Output $buf;exit}};"
        "Write-Output 'RESULT:SILENT'")


def read_nmea(port: str):
    """("DATA", text) | ("SILENT", "") | ("BUSY", "") | ("UNKNOWN", ""). Any
    failure of the probe itself is UNKNOWN, never a verdict."""
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", _nmea_script(port)],
            capture_output=True, text=True, timeout=_NMEA_PS_TIMEOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception:
        return ("UNKNOWN", "")
    out = (r.stdout or "").strip()
    first, _, rest = out.partition("\n")
    first = first.strip()
    if first == "RESULT:DATA":
        return ("DATA", rest)
    if first == "RESULT:SILENT":
        return ("SILENT", "")
    if first == "RESULT:BUSY":
        return ("BUSY", "")
    return ("UNKNOWN", "")


def apply_gps_nmea_check(gps: dict) -> dict:
    """Fold what the dongle is actually transmitting into the GPS verdict."""
    if not (gps.get("configured") and gps.get("present") and gps.get("ok")):
        return gps
    port = com_port_from_name(gps.get("resolved_name") or "")
    if not port:
        return gps          # not a COM-port dongle: nothing we can read
    try:
        (kind, text), fresh = _cached("nmea:" + port, lambda: read_nmea(port))
    except Exception:
        return gps
    out = dict(gps)
    if kind == "DATA":
        if fresh:
            with _lock:
                _gps_silent[port] = 0
        info = parse_nmea(text)
        if info["fix"]:
            sats = f", {info['satellites']} satellites" if info["satellites"] is not None else ""
            out["status"] = gps["status"] + f" (receiving data, satellite fix{sats})"
        else:
            out["status"] = gps["status"] + " (receiving data, no satellite fix yet - normal indoors)"
        return out
    if kind == "BUSY":
        out["status"] = gps["status"] + " (port in use by another program - data check skipped)"
        return out
    if kind == "SILENT":
        with _lock:
            if fresh:     # only a NEW read counts toward "silent twice in a row"
                _gps_silent[port] = _gps_silent.get(port, 0) + 1
            count = _gps_silent.get(port, 0)
        if count >= _SILENT_CHECKS_BEFORE_PROBLEM:
            out["ok"] = False
            out["status"] = (gps["status"] + " (but it is sending no data - unplug and "
                             "re-plug the GPS dongle)")
        return out
    return gps              # UNKNOWN: probe itself failed, say nothing
