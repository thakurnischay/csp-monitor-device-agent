"""Support tool: writes agent-diagnostic.txt describing EXACTLY what this PC
shows the agent - every printer, every USB/COM/other device (name, class,
maker, USB id, driver status), any fingerprint-scanner RD service, how long
each Windows scan took, and what the agent decided for each of the four
devices and why.

Run by double-clicking DIAGNOSE.bat. Read-only: it changes nothing, sends
nothing anywhere, and never prints the API key. Send the resulting text file
to the person who maintains the agent so detection can be tuned to this
hardware.
"""
import json
import os
import subprocess
import sys
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

OUT = os.path.join(HERE, "agent-diagnostic.txt")
lines = []


def w(text=""):
    lines.append(text)
    print(text)


def section(title):
    w()
    w("=" * 72)
    w(title)
    w("=" * 72)


def timed_ps(label, script, timeout=40):
    """Run a PowerShell snippet and report timing + any failure text, so we can
    tell "Windows returned nothing" from "PowerShell was blocked/slow"."""
    t = time.time()
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        took = time.time() - t
        out = (r.stdout or "").strip()
        err = (r.stderr or "").strip()
        w(f"  {label}: {took:.1f}s, exit code {r.returncode}, {len(out)} characters of output"
          + (f", ERROR TEXT: {err[:300]}" if err else ""))
        return out
    except subprocess.TimeoutExpired:
        w(f"  {label}: TIMED OUT after {timeout}s  <-- PowerShell is being blocked or is very slow")
    except FileNotFoundError:
        w(f"  {label}: PowerShell NOT FOUND on this PC")
    except Exception as e:
        w(f"  {label}: FAILED: {e}")
    return ""


def row(d, brands=()):
    import device_health as dh
    tag = ""
    if brands and dh.is_recognised(d, brands):
        tag = "   <== matches a known brand"
    return (f"  - {d.get('name')}\n"
            f"      class={d.get('class', '?')}  status={d.get('status')}  "
            f"maker={d.get('manufacturer') or '-'}  usb={d.get('vid') or '-'}:{d.get('pid') or '-'}"
            f"{tag}")


def main():
    import device_health as dh
    import deep_checks as dc

    section("1. THIS PC")
    w(f"  Windows: {sys.platform}, Python {sys.version.split()[0]}")
    try:
        from version import AGENT_VERSION
        w(f"  Agent version: {AGENT_VERSION}")
    except Exception:
        w("  Agent version: unknown")
    timed_ps("PowerShell basic test", "Write-Output 'ok'", timeout=20)

    section("2. WHAT THE AGENT HAS SAVED (API key hidden)")
    try:
        import config_store
        cfg = config_store.load()
        for k in ("csp_id", "server_url", "printer_name", "microatm_name",
                  "biometric_name", "gps_name", "software_process"):
            w(f"  {k}: {cfg.get(k, '') or '(blank)'}")
    except Exception as e:
        w(f"  could not read saved settings: {e}")

    section("3. WINDOWS SCANS (timing and whether Windows answered)")
    timed_ps("printer list", "Get-CimInstance -ClassName Win32_Printer | "
             "Select-Object Name, PrinterStatus, WorkOffline | ConvertTo-Json -Compress")
    timed_ps("device list", "Get-PnpDevice -PresentOnly | "
             "Select-Object FriendlyName, Class, Status, Manufacturer, InstanceId | ConvertTo-Json -Compress")

    printers = dh.list_printers()
    usb = dh.list_usb_devices()
    alld = dh.list_all_devices()
    w(f"  -> agent parsed {len(printers)} printers, {len(usb)} USB-type devices, {len(alld)} devices in total")
    if not alld:
        w("  !! The agent could not read ANY devices. Either Windows blocked the scan "
          "(Smart App Control / antivirus) or PowerShell is restricted. Detection "
          "cannot work until that is fixed.")

    section("4. PRINTERS (the passbook printer should be one of these)")
    for p in printers:
        flag = "  [virtual - ignored]" if dh.is_likely_virtual_printer(p["name"]) else ""
        hit = "  <== matches a known passbook-printer brand" if any(
            k in p["name"].lower() for k in dh._KNOWN_PRINTER_BRANDS) else ""
        w(f"  - {p['name']}   status={p['status']}{flag}{hit}")
    w(f"  Agent's automatic choice: {dh.auto_detect_printer(printers) or '(none - needs a manual pick)'}")

    w()
    w("  Other devices that could be a passbook printer that Windows has NOT installed")
    w("  as a printer (USB printing entries, printer-class devices, devices with no driver):")
    seen_print = {p["name"] for p in printers}
    found_print = False
    for d in alld:
        n = d["name"].lower()
        if d["name"] in seen_print:
            continue
        if (d["class"] in dh._PRINT_CLASSES or "print" in n or d["class"] in ("Unknown", "")
                or d["status"].upper() in ("ERROR", "UNKNOWN", "DEGRADED")
                or any(k in dh.search_text(d) for k in dh._KNOWN_PRINTER_BRANDS)):
            w(row(d, dh._KNOWN_PRINTER_BRANDS))
            found_print = True
    if not found_print:
        w("  (none) - if the passbook printer is plugged in and switched ON, but nothing")
        w("  above looks like it, Windows cannot see it at all (cable, power or USB port).")

    com = [d for d in alld if d["class"] == "Ports"]
    section("5. COM / SERIAL PORTS (passbook printers and GPS dongles often appear here)")
    for d in com:
        w(row(d))
    if not com:
        w("  (none present)")

    section("6. MICRO-ATM candidates (USB-type devices)")
    for d in usb:
        w(row(d, dh._KNOWN_MICROATM_BRANDS))
    w(f"  Agent's automatic choice: {dh.auto_detect_microatm(usb) or '(none - needs a manual pick)'}")

    section("7. FINGERPRINT SCANNER")
    rd = dc.probe_rd_services()
    if rd:
        for s in rd:
            w(f"  RD SERVICE FOUND on port {s['port']}: status={s['status']}  name={s.get('info') or '-'}")
    else:
        w("  No RD service answered on ports 11100-11120 (not installed, not running, or uses HTTPS).")
    bio = [d for d in alld if d["class"] in ("Biometric",) or
           dh.is_recognised(d, dh._KNOWN_BIOMETRIC_BRANDS) or d["class"] == "HIDClass"]
    for d in dh.rank_devices(bio, dh._KNOWN_BIOMETRIC_BRANDS, ("Biometric", "HIDClass")):
        w(row(d, dh._KNOWN_BIOMETRIC_BRANDS))
    w(f"  Agent's automatic choice: {dh.auto_detect_biometric(usb + [d for d in bio if d not in usb]) or '(none)'}")

    section("8. GPS DONGLE candidates")
    gps = [d for d in alld if d["class"] in dh._GPS_CANDIDATE_CLASSES or
           dh.is_recognised(d, dh._KNOWN_GPS_BRANDS)]
    for d in dh.rank_devices(gps, dh._KNOWN_GPS_BRANDS, dh._GPS_CANDIDATE_CLASSES):
        w(row(d, dh._KNOWN_GPS_BRANDS))
    w(f"  Agent's automatic choice: {dh.auto_detect_gps(gps + usb) or '(none)'}")

    section("8b. GPS DONGLE - what the serial port actually sends")
    cfg_gps = ""
    try:
        import config_store
        cfg_gps = config_store.load().get("gps_name", "")
    except Exception:
        pass
    ports = []
    for d in alld:
        port = dc.com_port_from_name(d["name"])
        if port and (d["name"] == cfg_gps or "serial" in d["name"].lower() or "gps" in d["name"].lower()
                     or "ch340" in d["name"].lower() or "pl2303" in d["name"].lower()):
            ports.append((port, d["name"]))
    if not ports:
        w("  No USB-serial / GPS-looking COM port is present.")
    for port, name in ports:
        w(f"  {name}")
        w(dc.debug_serial(port))

    section("9. EVERY OTHER DEVICE WINDOWS SHOWS (so nothing is hidden from review)")
    shown = {d["name"] for d in usb} | {d["name"] for d in com} | {d["name"] for d in bio} | {d["name"] for d in gps}
    for d in alld:
        if d["name"] not in shown:
            w(f"  - {d['name']}   class={d['class']}  maker={d.get('manufacturer') or '-'}  "
              f"usb={d.get('vid') or '-'}:{d.get('pid') or '-'}  status={d['status']}")

    section("END - please send this file (agent-diagnostic.txt) back")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        w("DIAGNOSTIC ITSELF FAILED:")
        w(traceback.format_exc())
    try:
        with open(OUT, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        print(f"\nSaved to: {OUT}")
    except Exception as e:
        print(f"Could not save the file: {e}")
