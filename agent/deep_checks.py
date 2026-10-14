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
  - These checks only ever ADD evidence or notes. They NEVER turn a device
    into a Problem: a status the agent cannot independently prove (RD NOT
    READY, a silent serial port) has been wrong on real, working hardware.
    Problems come only from Windows' own facts (missing / driver error).
  - "No satellite fix" is NOT a problem (indoors it's normal) - it is noted
    in the status text only.
  - A silent GPS dongle is NOT a problem (we can only hear common NMEA
    speeds): it is reported as not confirmed, never as a fault.
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
_NMEA_BAUDS = (9600, 4800, 38400, 115200)   # common NMEA speeds, most likely first
_NMEA_SECONDS_PER_BAUD = 2.0
_NMEA_PS_TIMEOUT = 25
_CACHE_SECONDS = 60             # the local status page polls check() often

_lock = threading.Lock()
_rd_seen = False                # an RD service has answered at least once this run
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


def _rd_name(service: dict) -> str:
    return service.get("info") or "fingerprint scanner (RD service)"


def apply_biometric_rd_check(bio: dict) -> dict:
    """Use the scanner's RD service as EVIDENCE of what the device is - never
    as a reason to call it broken.

    READY is strong positive evidence (the scanner is up and usable), so it
    verifies the device. NOT READY is NOT treated as a fault: some vendors'
    services (Morpho's, for one) report it until the scanner has been used
    once, and a real CSP's perfectly working MorphoSmart MSO1300-E3 was shown
    as a Problem because of it. A status the agent cannot independently prove
    must never turn a device red; it is noted in the text and nothing more.
    Only Windows' own facts (device missing / driver error) make a Problem.
    """
    global _rd_seen
    if bio.get("configured") and bio.get("present") and not bio.get("ok"):
        return bio          # Windows already reports a driver problem
    try:
        services, _ = _cached("rd", probe_rd_services)
    except Exception:
        return bio
    if services:
        with _lock:
            _rd_seen = True
        ready = [s for s in services if s["status"] == "READY"]
        if not (bio.get("configured") and bio.get("present")):
            # nothing valid picked: report from the RD service alone
            s = (ready or services)[0]
            out = {"configured": True, "present": True, "ok": True,
                   "verified": bool(ready), "source": "rd_service",
                   "resolved_name": bio.get("resolved_name", "")}
            if ready:
                out["verified_by"] = "RD service"
                out["status"] = f"OK - {_rd_name(s)} is ready"
            else:
                out["status"] = (f"{_rd_name(s)} is running but reports NOT READY (some scanners say this "
                                 "until first use) - could not confirm the scanner is connected")
            return out
        out = dict(bio)
        if ready:
            out["verified"] = True
            out["verified_by"] = "RD service"
            out["status"] = bio["status"] + f" (confirmed by the RD service: {_rd_name(ready[0])})"
        else:
            out["status"] = (bio["status"] + f" (its RD service, {_rd_name(services[0])}, reports NOT READY - "
                             "some scanners report this until first use; not treated as a fault)")
        return out
    if bio.get("configured") and bio.get("present"):
        with _lock:
            seen_before = _rd_seen
        if seen_before:
            out = dict(bio)
            out["status"] = (bio["status"] + " (its RD service is not answering right now - fingerprint "
                             "capture may need the scanner's RD service started)")
            return out
    return bio


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
        # DTR/RTS ON, as every serial terminal does: .NET leaves them off, and
        # many cheap USB (CH340/PL2303) GPS dongles stay silent until they are on.
        "$p.DtrEnable=$true;$p.RtsEnable=$true;"
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
        info = parse_nmea(text)
        out["verified"] = True       # it really is streaming GPS data
        out["verified_by"] = "GPS data"
        if info["fix"]:
            sats = f", {info['satellites']} satellites" if info["satellites"] is not None else ""
            out["status"] = gps["status"] + f" (receiving data, satellite fix{sats})"
        else:
            out["status"] = gps["status"] + " (receiving data, no satellite fix yet - normal indoors)"
        return out
    if kind == "BUSY":
        out["status"] = gps["status"] + " (GPS data check skipped: " + port + " is in use by another program)"
        return out
    if kind == "SILENT":
        # Silence is NOT proof of a fault: this check only knows common NMEA
        # speeds, and a dongle that works for its owner can still look silent to
        # it (different protocol/speed, needs the vendor's software to start it).
        # An unprovable negative must never be reported as a Problem - a real
        # field CSP's working dongle was wrongly flagged that way. It simply is
        # not confirmed by data, so it stays unverified unless recognised or
        # identified by the plug-in test.
        out["status"] = (gps["status"] + " (GPS data check: no data heard on " + port
                         + " - this check could not confirm it is sending location data)")
        return out
    # the check itself could not run - say so instead of staying silent
    out["status"] = gps["status"] + " (GPS data check could not open " + port + ")"
    return out


def debug_serial(port: str) -> str:
    """For the DIAGNOSE tool only: what a serial port actually sends at each
    common speed (byte count + the first printable characters), so a dongle
    that does not verify can be understood from real data."""
    lines = []
    for baud in _NMEA_BAUDS:
        script = (
            "$ErrorActionPreference='Stop';"
            f"$p=New-Object System.IO.Ports.SerialPort '{port}',{baud},'None',8,'one';"
            "$p.DtrEnable=$true;$p.RtsEnable=$true;"
            "try{$p.Open()}catch{Write-Output ('OPEN ERROR: '+$_.Exception.Message);exit};"
            "$end=(Get-Date).AddSeconds(3);$buf='';"
            "while((Get-Date) -lt $end){$buf+=$p.ReadExisting();Start-Sleep -Milliseconds 150};"
            "$p.Close();"
            "Write-Output ('BYTES: '+$buf.Length);"
            "Write-Output ($buf.Substring(0,[Math]::Min(160,$buf.Length)) -replace '[^\\x20-\\x7E]','.')")
        try:
            r = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                capture_output=True, text=True, timeout=20,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            text = " | ".join(l.strip() for l in (r.stdout or "").splitlines() if l.strip()) or "(no output)"
        except Exception as e:
            text = f"could not run: {e}"
        lines.append(f"    {port} at {baud} baud: {text}")
        if text.startswith("OPEN ERROR"):
            break           # the port itself cannot be opened - other speeds will fail the same way
    return "\n".join(lines)
