"""Switches an ALREADY-INSTALLED agent to a new server address, safely.

What it does on the CSP's PC:
  1. finds the installed agent (next to this file, or via the Startup launcher),
  2. checks the NEW address really is the monitor server and is reachable,
  3. backs up agent_config.json, changes ONLY server_url (CSP code, API key and
     the device selections are left exactly as they are - nothing is typed),
  4. sends one real report to the new address using the CSP's existing key,
  5. if the new server does not accept it, puts the OLD address back.

So a wrong address, a server that is down, or a key the new server does not
know can never leave a CSP cut off. No credentials are inside this tool.

The new address comes from new_address.txt next to this file (or the first
command-line argument).
"""
import json
import os
import re
import shutil
import sys
import time
import urllib.request
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
LOG_LINES = []


def say(text=""):
    LOG_LINES.append(text)
    print(text)


def read_new_address(argv=None) -> str:
    argv = sys.argv[1:] if argv is None else argv
    if argv:
        return argv[0].strip()
    path = os.path.join(HERE, "new_address.txt")
    if os.path.isfile(path):
        for line in open(path, encoding="utf-8-sig"):
            line = line.strip()
            if line and not line.startswith("#"):
                return line
    return ""


def normalise(url: str) -> str:
    url = (url or "").strip().strip('"').strip("'")
    return url.rstrip("/") + "/" if url else ""


def find_agent_dir():
    """The folder holding agent.py + agent_config.json, or None."""
    candidates = [HERE]
    startup = os.path.join(os.environ.get("APPDATA", ""), "Microsoft", "Windows",
                           "Start Menu", "Programs", "Startup", "CSPDeviceMonitorAgent.bat")
    try:
        if os.path.isfile(startup):
            m = re.search(r'"([^"]*agent\.py)"', open(startup, encoding="utf-8", errors="ignore").read(), re.I)
            if m:
                candidates.append(os.path.dirname(m.group(1)))
    except Exception:
        pass
    for base in candidates:
        if base and os.path.isfile(os.path.join(base, "agent.py")):
            return base
    return None


def reachable(new_url: str, attempts: int = 2, timeout: int = 20):
    """(ok, message): is the NEW address really the monitor server?"""
    last = ""
    for n in range(attempts):
        try:
            req = urllib.request.Request(new_url + "readiness", headers={"User-Agent": "csp-address-switch"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                body = json.loads(r.read().decode("utf-8", "replace") or "{}")
            if body.get("ok") and body.get("database") == "ok":
                return True, "reachable and ready"
            return False, f"answered, but it is not a ready monitor server ({body})"
        except Exception as e:
            last = str(e)
            time.sleep(2)
    return False, f"cannot reach it: {last}"


def send_real_report(agent_dir: str):
    """One real report using the agent's own code and the (new) saved address."""
    sys.path.insert(0, agent_dir)
    os.environ.pop("CSP_AGENT_CONFIG_PATH", None)
    import importlib
    import config_store
    import reporter
    importlib.reload(config_store)
    importlib.reload(reporter)
    result = reporter.report_once()
    return bool(result.get("ok")), result.get("error", "")


def switch(new_url: str, old_url_guard: str = "", agent_dir: str = None, check=reachable, report=send_real_report) -> int:
    """Returns 0 = switched and verified, 1 = failed (nothing changed / reverted), 2 = nothing to do."""
    new_url = normalise(new_url)
    if not re.match(r"^https?://[^/\s]+", new_url):
        say("The new address is missing or invalid (it must start with http:// or https://).")
        return 1
    agent_dir = agent_dir or find_agent_dir()
    if not agent_dir:
        say("Could not find the installed agent on this PC.")
        say("Put these files inside the agent folder (the one containing agent.py) and run again.")
        return 1
    cfg_path = os.path.join(agent_dir, "agent_config.json")
    if not os.path.isfile(cfg_path):
        say("This PC's agent has no saved settings yet (agent_config.json) - it was never set up.")
        say("Nothing changed. Use the full installer for this CSP instead.")
        return 1
    original_text = open(cfg_path, encoding="utf-8").read()
    cfg = json.loads(original_text)
    old_url = normalise(cfg.get("server_url", ""))
    say(f"Agent folder : {agent_dir}")
    say(f"CSP code     : {cfg.get('csp_id') or '(none)'}")
    say(f"Current      : {old_url or '(none)'}")
    say(f"New          : {new_url}")
    say("")
    if not cfg.get("csp_id") or not cfg.get("api_key"):
        say("This agent has no CSP code / API key saved, so it cannot report anywhere. Nothing changed.")
        return 1
    if old_url_guard and old_url != normalise(old_url_guard):
        say(f"This agent is not using {normalise(old_url_guard)} so it was left alone.")
        return 2
    if old_url == new_url:
        say("Already using the new address. Nothing to do.")
        return 2

    say("Step 1/3: checking the new address ...")
    ok, msg = check(new_url)
    say(f"           {msg}")
    if not ok:
        say("\nFAILED - the new address does not work from this PC. NOTHING WAS CHANGED.")
        return 1

    backup = cfg_path + ".bak-" + datetime.now().strftime("%Y%m%d-%H%M%S")
    shutil.copyfile(cfg_path, backup)
    say(f"Step 2/3: switching (backup saved as {os.path.basename(backup)}) ...")
    cfg["server_url"] = new_url
    with open(cfg_path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)

    say("Step 3/3: sending one real report to the new address ...")
    sent, err = report(agent_dir)
    if sent:
        say("\nSUCCESS - the new server accepted this CSP's report. The agent will keep")
        say("reporting there automatically; nothing else needs to be done.")
        return 0
    with open(cfg_path, "w", encoding="utf-8") as f:
        f.write(original_text)
    say(f"           {err or 'the new server did not accept the report'}")
    say("\nFAILED - the OLD address has been put back, so this CSP keeps reporting as before.")
    if "401" in (err or ""):
        say("(The new server does not know this CSP's API key - the key list has to be moved to")
        say(" the new server, or a new key issued for this CSP.)")
    return 1


def main() -> int:
    say("=" * 60)
    say("  CSP Device Monitor - change server address")
    say("=" * 60)
    code = switch(read_new_address(), read_guard())
    try:
        agent_dir = find_agent_dir() or HERE
        with open(os.path.join(agent_dir, "address-change-result.txt"), "w", encoding="utf-8") as f:
            f.write(datetime.now().isoformat(timespec="seconds") + "\n" + "\n".join(LOG_LINES) + "\n")
    except Exception:
        pass
    return code


def read_guard() -> str:
    """Optional old_address.txt: only switch agents currently on that address."""
    path = os.path.join(HERE, "old_address.txt")
    if os.path.isfile(path):
        for line in open(path, encoding="utf-8-sig"):
            line = line.strip()
            if line and not line.startswith("#"):
                return line
    return ""


if __name__ == "__main__":
    sys.exit(main())
