"""
Reporter — the actual "heartbeat" loop. Every `interval_seconds`, checks the
two configured devices and POSTs the result to the central server. Runs
forever in a background thread; a single failed check or a server that is
temporarily unreachable must never stop the loop.
"""
import logging
import platform
import socket
import threading
import time
from datetime import date, datetime, timezone

import requests

import config_store
import device_health
from version import AGENT_VERSION, SCHEMA_VERSION

log = logging.getLogger("csp_agent.reporter")

_stop_event = threading.Event()

# (connect_timeout, read_timeout) - connect fails fast if the server is
# simply unreachable, read gets more room since a report can involve a
# real database write server-side. A single scalar timeout would apply the
# same short window to both, which is the wrong tradeoff for either case.
_HTTP_TIMEOUT = (5, 15)
_MAX_ATTEMPTS = 2          # 1 real attempt + 1 bounded retry, never more
_RETRY_DELAY_SECONDS = 2   # short - a persistent outage still waits for the
                          # normal interval, this only smooths over a blip


def _maybe_run_printer_functional_test(cfg: dict, printer_name: str, printer_present: bool) -> dict:
    """The real test-print check (device_health.printer_functional_test) costs
    one physical sheet, so it runs at most once per calendar day no matter how
    often report_once() is called — the 5-minute heartbeat, or a manual
    "Report now" click both share this same cap. Any other call that day gets
    back the cached result instead of printing again."""
    if not printer_name or not printer_present:
        return {"ran": False, "ok": False, "detail": "no printer configured/connected"}
    today = date.today().isoformat()
    if cfg.get("last_printer_test_date") == today:
        return cfg.get("last_printer_test_result") or {"ran": False, "ok": None, "detail": "already checked today"}
    # Bounded the same way as device_health.check(): a real test print spawns
    # its own processes (rundll32, PowerShell) that can hang if something on
    # this PC blocks them, and /report_now must still respond either way.
    result = device_health.run_with_ceiling(
        device_health.printer_functional_test, 20, printer_name)
    if result is None:
        result = {"ran": False, "ok": False,
                  "detail": "test print timed out - a security tool on this PC may be blocking it"}
    config_store.save(last_printer_test_date=today, last_printer_test_result=result)
    return result


def _safe_os_info() -> str:
    try:
        return platform.platform()
    except Exception:
        return "unknown"


def _safe_hostname() -> str:
    # The PC's machine name, not a person's identity - useful for support
    # (telling apart multiple PCs at one CSP), not sensitive personal data.
    try:
        return socket.gethostname()
    except Exception:
        return ""


def build_payload(cfg: dict) -> dict:
    health = device_health.check(cfg.get("printer_name", ""), cfg.get("microatm_name", ""),
                                 cfg.get("software_process", ""),
                                 cfg.get("biometric_name", ""), cfg.get("gps_name", ""))
    # Self-configure: a confidently-recognized real device gets saved as the
    # actual selection, not just used transiently for this one check - so a
    # CSP with a recognizable printer and/or micro-ATM needs zero manual setup.
    config_store.sync_resolved_device("printer_name", health["printer"].get("resolved_name", ""))
    config_store.sync_resolved_device("microatm_name", health["microatm"].get("resolved_name", ""))
    config_store.sync_resolved_device("biometric_name", health["biometric"].get("resolved_name", ""))
    config_store.sync_resolved_device("gps_name", health["gps"].get("resolved_name", ""))
    resolved_printer_name = health["printer"].get("resolved_name") or cfg.get("printer_name", "")
    functional = _maybe_run_printer_functional_test(
        cfg, resolved_printer_name, health["printer"].get("present", False))
    return {
        "schema_version": SCHEMA_VERSION,
        "agent_version": AGENT_VERSION,
        "os": _safe_os_info(),
        "hostname": _safe_hostname(),
        "csp_id": cfg["csp_id"],
        "reported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "printer": health["printer"],
        "microatm": health["microatm"],
        "biometric": health["biometric"],
        "gps": health["gps"],
        "printer_functional_test": functional,
    }


def report_once() -> dict:
    """One check-and-send cycle. Never raises — returns {"ok": bool, ...} so
    the caller (loop or a manual "Report now" button) can show what happened.

    A failed POST gets one bounded retry after a short delay, to smooth over
    a transient network blip without turning into a busy loop - a
    persistently unreachable server still just waits for the next normal
    interval, same as before. Never retries on a 401 (a bad key won't fix
    itself by trying again)."""
    cfg = config_store.load()
    if not config_store.is_registered(cfg):
        return {"ok": False, "error": "agent not registered yet — set server URL, "
                                       "CSP ID and API key in the local status page"}
    payload = build_payload(cfg)
    url = cfg["server_url"].rstrip("/") + "/api/report"
    # .strip() defends against a stray whitespace character already saved in
    # an older config (before local_ui.py started stripping on save) - a
    # config on disk from before that fix would otherwise keep failing
    # forever with no way to tell why.
    headers = {"X-API-Key": (cfg["api_key"] or "").strip(), "Content-Type": "application/json"}

    last_error = "unknown error"
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            r = requests.post(url, headers=headers, json=payload, timeout=_HTTP_TIMEOUT)
            if r.status_code == 401:
                return {"ok": False, "error": "server rejected the API key (401) — "
                                               "check CSP ID / API key in the status page"}
            r.raise_for_status()
            return {"ok": True, "payload": payload}
        except requests.RequestException as e:
            last_error = str(e)
            if attempt < _MAX_ATTEMPTS:
                time.sleep(_RETRY_DELAY_SECONDS)
    return {"ok": False, "error": f"could not reach server: {last_error}"}


def _loop():
    log.info("reporter loop started")
    while not _stop_event.is_set():
        cfg = config_store.load()
        result = report_once()
        if result["ok"]:
            log.info("reported ok: printer=%s microatm=%s test_print=%s",
                     result["payload"]["printer"]["status"],
                     result["payload"]["microatm"]["status"],
                     result["payload"].get("printer_functional_test", {}).get("detail", "n/a"))
        else:
            log.warning("report failed: %s", result["error"])
        interval = max(30, int(cfg.get("interval_seconds", 300)))
        _stop_event.wait(interval)
    log.info("reporter loop stopped")


def start_background() -> threading.Thread:
    """Start the loop in a daemon thread. Safe to call once at agent startup."""
    _stop_event.clear()
    t = threading.Thread(target=_loop, daemon=True, name="csp-agent-reporter")
    t.start()
    return t


def stop_background():
    _stop_event.set()
