"""ADMIN TOOL - builds one ready-to-install agent zip PER CSP, with that CSP's
server address, CSP code and API key already filled in, so the CSP's PC needs
nothing typed: extract, double-click SETUP.bat, then only pick the devices.

Why a tool and not one shared zip with credentials inside: every CSP has its
own API key, and the server keeps only a one-way hash of it (the real key is
shown ONCE when it is issued on the dashboard's API Keys page), so no zip can
be built without the keys being supplied here. A key only ever lands in the
zip of the CSP it belongs to.

Usage (from the project folder):
    python tools/make_csp_zips.py                      # reads tools/csp_list.csv
    python tools/make_csp_zips.py --csv my_list.csv --out my_zips
    python tools/make_csp_zips.py --server-url https://host:8443

csp_list.csv has one CSP per line:   csp_id,api_key[,name]
(see csp_list.example.csv). Keep that file private - it holds real keys; it is
git-ignored. Output zips go to tools/dist/ (also git-ignored).

Never prints an API key. Does NOT touch the update packs: those must not carry
a config file, or they would overwrite a CSP's saved settings.
"""
import argparse
import csv
import json
import os
import re
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
AGENT_DIR = os.path.join(os.path.dirname(HERE), "agent")
DEFAULT_SERVER_URL = "https://openclaw-poweredge-r730.tailb276af.ts.net:8443/"

# what ships to a CSP (tests, caches and local state never do)
AGENT_FILES = [
    "agent.py", "config_store.py", "deep_checks.py", "device_health.py", "diagnostics.py", "impact_map.py", "local_ui.py",
    "reporter.py", "singleton.py", "version.py", "diagnose.py", "requirements.txt",
    "SETUP.bat", "DIAGNOSE.bat", "INSTALL_FOR_CSP.txt", "install_task.ps1", "uninstall_task.ps1",
]
_KEY_SHAPE = re.compile(r"^[A-Za-z0-9_-]{30,64}$")      # secrets.token_urlsafe(32) is 43 chars


def _clean(value: str) -> str:
    # strip stray spaces/newlines picked up when copying a key - one of them
    # makes every report fail as "invalid API key" with no visible reason
    return (value or "").strip().strip('"').strip("'").strip()


def read_rows(csv_path: str):
    """[(csp_id, api_key, name)] plus a list of problems found. Skips blanks,
    comment lines (#...) and a header row."""
    rows, problems, seen = [], [], set()
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        for n, raw in enumerate(csv.reader(f), start=1):
            if not raw or not any(c.strip() for c in raw) or raw[0].strip().startswith("#"):
                continue
            if raw[0].strip().lower() in ("csp_id", "csp id", "csp code", "code"):
                continue
            if len(raw) < 2:
                problems.append(f"line {n}: needs csp_id,api_key (found {len(raw)} column)")
                continue
            csp_id, key = _clean(raw[0]), _clean(raw[1])
            name = _clean(raw[2]) if len(raw) > 2 else ""
            if not csp_id or not key:
                problems.append(f"line {n}: csp_id or api_key is empty")
                continue
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", csp_id):
                problems.append(f"line {n}: csp_id '{csp_id}' has characters that are not allowed")
                continue
            if csp_id in seen:
                problems.append(f"line {n}: csp_id {csp_id} is listed twice - second one skipped")
                continue
            if not _KEY_SHAPE.match(key):
                problems.append(f"line {n}: the API key for {csp_id} does not look like a real key "
                                f"(expected ~43 letters/digits/-/_) - check it was copied completely")
                continue
            seen.add(csp_id)
            rows.append((csp_id, key, name))
    return rows, problems


def build_zip(csp_id: str, api_key: str, server_url: str, out_dir: str) -> str:
    """One install zip for one CSP. Returns its path."""
    cfg = {"csp_id": csp_id, "api_key": api_key, "server_url": server_url}
    out = os.path.join(out_dir, f"CSP-{csp_id}-agent-setup.zip")
    top = f"csp-device-monitor-agent-{csp_id}/"
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for name in AGENT_FILES:
            path = os.path.join(AGENT_DIR, name)
            if not os.path.isfile(path):
                raise FileNotFoundError(f"agent file missing: {name}")
            z.write(path, top + name)
        wheels = os.path.join(AGENT_DIR, "wheels")
        if os.path.isdir(wheels):
            for w in sorted(os.listdir(wheels)):
                if w.endswith(".whl"):
                    z.write(os.path.join(wheels, w), top + "wheels/" + w)
        # the one CSP-specific file: read by the agent at start-up
        z.writestr(top + "agent_config.json", json.dumps(cfg, indent=2))
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Build one ready-to-install agent zip per CSP.")
    ap.add_argument("--csv", default=os.path.join(HERE, "csp_list.csv"))
    ap.add_argument("--out", default=os.path.join(HERE, "dist"))
    ap.add_argument("--server-url", default=DEFAULT_SERVER_URL)
    args = ap.parse_args(argv)

    if not os.path.isfile(args.csv):
        print(f"Cannot find {args.csv}\nCopy tools/csp_list.example.csv to tools/csp_list.csv, "
              f"fill in one CSP per line (csp_id,api_key,name) and run this again.")
        return 2
    if not re.match(r"^https?://", args.server_url):
        print("The server URL must start with http:// or https://")
        return 2
    rows, problems = read_rows(args.csv)
    for p in problems:
        print("  WARNING:", p)
    if not rows:
        print("No usable CSP lines found - nothing built.")
        return 1
    os.makedirs(args.out, exist_ok=True)
    for csp_id, key, name in rows:
        path = build_zip(csp_id, key, args.server_url, args.out)
        print(f"  built {os.path.basename(path)}" + (f"   ({name})" if name else ""))
    print(f"\n{len(rows)} zip(s) in {args.out}\nServer address in every zip: {args.server_url}")
    print("Each zip holds that CSP's own API key - send it only to that CSP.")
    return 0 if not problems else 3


if __name__ == "__main__":
    sys.exit(main())
