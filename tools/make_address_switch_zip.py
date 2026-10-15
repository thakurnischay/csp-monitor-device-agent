"""ADMIN TOOL - builds the small zip that moves already-installed CSP agents to
a NEW server address. It contains NO credentials: each CSP keeps the code and
API key already saved on its own PC, and only the server address changes.

    python tools/make_address_switch_zip.py --server-url https://new-host:8443
    python tools/make_address_switch_zip.py --server-url https://new-host:8443 \
        --old-url https://openclaw-poweredge-r730.tailb276af.ts.net:8443/

--old-url (optional): the zip then only switches agents that are currently on
that address and leaves any other agent alone.

IMPORTANT for a real migration: the NEW server must know every CSP's API key,
i.e. it must run with the SAME database (the csp_heartbeat_data volume) as the
old one. Otherwise every CSP's report is rejected (401) - the switch tool then
puts the old address back, but the CSPs would need new keys (use
make_csp_zips.py for that).
"""
import argparse
import os
import re
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "address_switch")


def build(new_url: str, old_url: str, out_dir: str) -> str:
    if not re.match(r"^https?://[^/\s]+", new_url or ""):
        raise ValueError("the server URL must start with http:// or https://")
    new_url = new_url.strip().rstrip("/") + "/"
    host = re.sub(r"[^A-Za-z0-9.-]", "_", re.sub(r"^https?://", "", new_url).rstrip("/"))
    os.makedirs(out_dir, exist_ok=True)
    out = os.path.join(out_dir, f"switch-server-address-to-{host}.zip")
    top = "switch-server-address/"
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(os.path.join(SRC, "change_address.py"), top + "change_address.py")
        z.write(os.path.join(SRC, "CHANGE_SERVER_ADDRESS.bat"), top + "CHANGE_SERVER_ADDRESS.bat")
        z.writestr(top + "new_address.txt", new_url + "\n")
        if old_url:
            z.writestr(top + "old_address.txt", old_url.strip().rstrip("/") + "/\n")
        z.writestr(top + "READ_ME.txt",
                   "CSP Device Monitor - change server address\n"
                   "===========================================\n"
                   "1. Extract this zip (right-click > Extract All), then open the folder.\n"
                   "2. Double-click CHANGE_SERVER_ADDRESS.bat.\n"
                   "3. Wait for SUCCESS or FAILED. FAILED changes nothing - the old\n"
                   "   address is kept. Send a photo of the window to your admin.\n"
                   "Nothing needs to be typed. The CSP code, API key and devices are kept.\n"
                   f"New address: {new_url}\n")
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--server-url", required=True)
    ap.add_argument("--old-url", default="")
    ap.add_argument("--out", default=os.path.join(HERE, "dist"))
    args = ap.parse_args(argv)
    try:
        path = build(args.server_url, args.old_url, args.out)
    except ValueError as e:
        print("Error:", e)
        return 2
    print("Built", path)
    print("Contains no credentials. Safe to send to every CSP.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
