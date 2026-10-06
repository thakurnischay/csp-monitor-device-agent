"""
Local agent configuration — a small JSON file living next to the agent, holding
everything one CSP install needs: which server to report to, its credentials,
which local devices are "the" printer/micro-ATM, and how often to check.

Deliberately NOT a database — this is one agent, one CSP, one config. A plain
JSON file is easy to inspect/edit by hand if something needs fixing on-site.
"""
import json
import os
import threading

_LOCK = threading.Lock()

DEFAULTS = {
    "csp_id": "",
    "api_key": "",
    "server_url": "http://localhost:5100",
    "printer_name": "",
    "microatm_name": "",
    "biometric_name": "",
    "gps_name": "",
    "software_process": "",
    "interval_seconds": 300,
    # Internal state for the once-a-day printer test print (see reporter.py) —
    # not user-editable, not part of the /configure allowed-keys set.
    "last_printer_test_date": "",
    "last_printer_test_result": {},
}


def _config_path() -> str:
    """Overridable via env var so tests never touch a real install's config."""
    override = os.environ.get("CSP_AGENT_CONFIG_PATH")
    if override:
        return override
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "agent_config.json")


def load() -> dict:
    """Current config, defaults filled in for any missing/new key. Never raises
    — a missing or corrupt file just means "nothing configured yet"."""
    path = _config_path()
    data = dict(DEFAULTS)
    try:
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as f:
                data.update(json.load(f) or {})
    except Exception:
        pass
    return data


def save(**updates) -> dict:
    """Merge `updates` into the current config and persist. Only keys actually
    passed are changed — e.g. save(printer_name="X") leaves everything else
    untouched. Returns the resulting full config."""
    with _LOCK:
        cfg = load()
        for k, v in updates.items():
            if k in DEFAULTS and v is not None:
                cfg[k] = v
        path = _config_path()
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
        os.replace(tmp, path)
        return cfg


def is_registered(cfg: dict = None) -> bool:
    """Whether the agent has been pointed at a server + given credentials —
    the reporter loop refuses to send anything until this is true."""
    cfg = cfg or load()
    return bool(cfg.get("csp_id") and cfg.get("api_key") and cfg.get("server_url"))


def sync_resolved_device(field: str, resolved_name: str) -> None:
    """Self-configuring device selection: whenever device_health confidently
    recognizes a real device by brand name (a non-blank resolved_name that
    differs from what's saved), persist it as the new saved selection - a CSP
    with only a printer, only a micro-ATM, both, or a recognizable brand of
    either needs no manual dropdown step at all. A blank resolved_name (device
    unrecognized or genuinely absent) is a no-op, leaving the manual dropdown
    as the fallback for whatever the agent can't confidently identify on its
    own. Never raises."""
    if not resolved_name:
        return
    try:
        if load().get(field) == resolved_name:
            return
        save(**{field: resolved_name})
    except Exception:
        pass
