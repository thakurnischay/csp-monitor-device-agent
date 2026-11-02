"""ADMIN TOOL - builds the ONE generic installer zip you send to any new CSP.

It contains the server address and the PUBLIC release key - nothing secret and nothing CSP-specific. The CSP
extracts it, double-clicks SETUP.bat, types their CSP name + code, picks their devices; the PC then makes its own
key and an administrator approves it on the dashboard (Enrollments page).

    python tools/make_installer_zip.py --server-url https://monitor.example.com [--out tools/dist]

Refuses to build if the agent folder holds a config / history / state file (they must never ship).
"""
import argparse
import json
import os
import re
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
AGENT_DIR = os.path.join(os.path.dirname(HERE), "agent")
sys.path.insert(0, AGENT_DIR)

FILES = ["SETUP.bat", "agent.py", "apply_update.py", "config_store.py", "deep_checks.py", "device_health.py", "diagnose.py",
         "diagnostics.py", "ed25519.py", "enroll.py", "impact_map.py", "outcomes.py", "local_ui.py", "release_manifest.py",
         "release_public_keys.json", "reporter.py", "requirements.txt", "singleton.py", "update_client.py", "version.py",
         "DIAGNOSE.bat", "install_task.ps1", "uninstall_task.ps1",
         "installer/__init__.py", "installer/installer_core.py", "installer/wizard.py"]
FORBIDDEN = {"agent_config.json", "diagnostic_history.json", "agent_state.json", "update_state.json", "agent.lock"}


def agent_version():
    ns = {}
    with open(os.path.join(AGENT_DIR, "version.py"), encoding="utf-8") as f:
        exec(f.read(), ns)
    return ns.get("AGENT_VERSION", "")


def build(server_url, out_dir):
    if not re.match(r"^https?://\S+$", server_url):
        raise SystemExit("The server URL must start with http:// or https://")
    top = "CSP-Device-Monitor-Setup/"
    os.makedirs(out_dir, exist_ok=True)
    out = os.path.join(out_dir, "CSP-Device-Monitor-Setup.zip")
    init = os.path.join(AGENT_DIR, "installer", "__init__.py")
    if not os.path.isfile(init):
        open(init, "w").write("")
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for rel in FILES:
            if os.path.basename(rel) in FORBIDDEN:
                raise SystemExit("refusing to package %s" % rel)
            path = os.path.join(AGENT_DIR, *rel.split("/"))
            if not os.path.isfile(path):
                raise SystemExit("agent file missing: %s" % rel)
            z.write(path, top + rel)
        wheels = os.path.join(AGENT_DIR, "wheels")
        if os.path.isdir(wheels):
            for w in sorted(os.listdir(wheels)):
                if w.endswith(".whl"):
                    z.write(os.path.join(wheels, w), top + "wheels/" + w)
        z.writestr(top + "installer_config.json", json.dumps({"server_url": server_url, "agent_version": agent_version()}, indent=2))
        z.writestr(top + "README.txt", "1. Right-click this zip and choose Extract All.\r\n2. Open the new folder and double-click SETUP.bat.\r\n"
                                       "3. Follow the five screens. You will need your CSP name and CSP code.\r\n")
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description="Build the generic CSP installer zip.")
    ap.add_argument("--server-url", required=True)
    ap.add_argument("--out", default=os.path.join(HERE, "dist"))
    a = ap.parse_args(argv)
    path = build(a.server_url, a.out)
    print("built %s\nserver address inside: %s\nNo credentials are inside this zip." % (path, a.server_url))
    return 0


if __name__ == "__main__":
    sys.exit(main())
