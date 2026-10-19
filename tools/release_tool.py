"""ADMINISTRATOR TOOL - creates and signs agent releases. Run on YOUR machine only.

    python tools/release_tool.py keygen   --out D:\\csp-signing-keys
    python tools/release_tool.py build    --private-key D:\\csp-signing-keys\\release_signing_private.key \\
                                          --version 2.1.0 --notes "Fixes ..." --out D:\\releases
    python tools/release_tool.py rollback --private-key ... --base-package old.zip --base-manifest old.manifest.json \\
                                          --version 2.1.1 --notes "Rollback to 2.0.0 code" --out D:\\releases
    python tools/release_tool.py verify   --package P.zip --manifest M.json --signature M.sig

SECURITY MODEL
  * The PRIVATE key never leaves your machine: not in the repository, not on the
    server, not on any CSP PC. `keygen` refuses to write it inside a git repository.
  * The server can distribute releases but cannot forge one: agents verify the
    Ed25519 signature against the PUBLIC key shipped inside the agent.
  * `sequence` rises with every release; agents refuse to go backwards. A rollback is
    therefore a NEW release (higher sequence) whose content is old, known-good code.

A release = <name>.zip + <name>.manifest.json + <name>.manifest.sig (upload all three
on the dashboard's Agent Updates page).
"""
import argparse
import base64
import hashlib
import io
import json
import os
import re
import sys
import zipfile
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "agent"))
import ed25519                       # noqa: E402
import release_manifest as rm        # noqa: E402

AGENT_DIR = os.path.join(ROOT, "agent")
KEY_FILE = "release_signing_private.key"
PUBKEY_FILE = "release_public_keys.json"
STATE_FILE = "release_state.json"

# What goes into a release: the agent's own code. Never tests, wheels, local state or secrets.
INCLUDE_TOP = ["agent.py", "config_store.py", "deep_checks.py", "device_health.py", "diagnostics.py",
               "impact_map.py", "local_ui.py", "reporter.py", "singleton.py", "version.py", "diagnose.py",
               "ed25519.py", "release_manifest.py", "update_client.py", "apply_update.py", "enroll.py",
               "release_public_keys.json", "requirements.txt", "DIAGNOSE.bat"]
INCLUDE_DIRS = ["installer"]


def _inside_git_repo(path):
    cur = os.path.abspath(path)
    while True:
        if os.path.exists(os.path.join(cur, ".git")):
            return True
        parent = os.path.dirname(cur)
        if parent == cur:
            return False
        cur = parent


def cmd_keygen(a):
    out = os.path.abspath(a.out)
    if _inside_git_repo(out) and not a.allow_in_repo:
        print("REFUSED: %s is inside a git repository. The private signing key must live outside any repository." % out)
        return 2
    os.makedirs(out, exist_ok=True)
    kp, pp = os.path.join(out, KEY_FILE), os.path.join(out, PUBKEY_FILE)
    if os.path.exists(kp):
        print("REFUSED: %s already exists. Not overwriting a signing key." % kp)
        return 2
    sk = os.urandom(32)
    pk = ed25519.secret_to_public(sk)
    key_id = a.key_id or ("k" + datetime.now(timezone.utc).strftime("%Y%m%d"))
    fd = os.open(kp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(sk.hex() + "\n")
    with open(pp, "w", encoding="utf-8") as f:
        json.dump({"keys": [{"id": key_id, "public": pk.hex()}]}, f, indent=2)
    print("Private key  : %s   <- KEEP OFFLINE. Back it up. Never commit, upload or e-mail it." % kp)
    print("Public keys  : %s   <- copy into agent/release_public_keys.json AND server/release_public_keys.json" % pp)
    print("Key id       : %s" % key_id)
    return 0


def _load_private(path):
    with open(path, "r", encoding="utf-8") as f:
        return bytes.fromhex(f.read().strip())


def _key_id_for(secret, keys_file):
    pub = ed25519.secret_to_public(secret).hex()
    with open(keys_file, "r", encoding="utf-8") as f:
        for k in json.load(f).get("keys", []):
            if k.get("public") == pub:
                return k["id"]
    raise SystemExit("This private key's public half is not listed in %s" % keys_file)


def _next_sequence(state_path, floor=0):
    last = 0
    if os.path.exists(state_path):
        with open(state_path, "r", encoding="utf-8") as f:
            last = int(json.load(f).get("last_sequence", 0))
    seq = max(last, floor) + 1
    return seq


def _write_sequence(state_path, seq):
    with open(state_path, "w", encoding="utf-8") as f:
        json.dump({"last_sequence": seq}, f)


def collect_agent_files(agent_dir=AGENT_DIR):
    files = {}
    for name in INCLUDE_TOP:
        p = os.path.join(agent_dir, name)
        if os.path.isfile(p):
            files[name] = open(p, "rb").read()
    for d in INCLUDE_DIRS:
        base = os.path.join(agent_dir, d)
        for dirpath, _dirs, names in os.walk(base):
            for n in names:
                full = os.path.join(dirpath, n)
                rel = os.path.relpath(full, agent_dir).replace(os.sep, "/")
                if rm.safe_relpath(rel) and "__pycache__" not in rel:
                    files[rel] = open(full, "rb").read()
    return files


def _stamp_version(files, version, sequence):
    """version.py inside the package is rewritten so the installed code reports exactly
    the version/sequence the signed manifest says."""
    src = files["version.py"].decode("utf-8")
    src = re.sub(r'AGENT_VERSION\s*=\s*"[^"]*"', 'AGENT_VERSION = "%s"' % version, src)
    if re.search(r"AGENT_SEQUENCE\s*=\s*\d+", src):
        src = re.sub(r"AGENT_SEQUENCE\s*=\s*\d+", "AGENT_SEQUENCE = %d" % sequence, src)
    else:
        src += "\n# Monotonic release number: an agent never installs a lower one (set by tools/release_tool.py).\nAGENT_SEQUENCE = %d\n" % sequence
    files["version.py"] = src.encode("utf-8")


def build_release(files, version, sequence, key_id, secret, notes="", kind="release", min_agent_sequence=0, out_dir=".", name=None):
    """Pure function used by the CLI and the tests: files(dict path->bytes) -> (zip_path, manifest_path, sig_path)."""
    files = dict(files)
    _stamp_version(files, version, sequence)
    for path in files:
        if not rm.safe_relpath(path):
            raise SystemExit("refusing to package unsafe path: %s" % path)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for path in sorted(files):
            info = zipfile.ZipInfo(path, date_time=(2026, 1, 1, 0, 0, 0))     # deterministic bytes
            info.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(info, files[path])
    package = buf.getvalue()
    manifest = {
        "product": rm.PRODUCT, "platform": rm.PLATFORM, "version": version, "sequence": sequence, "kind": kind,
        "key_id": key_id, "created": datetime.now(timezone.utc).isoformat(timespec="seconds"), "notes": notes,
        "min_agent_sequence": min_agent_sequence, "requires_new_dependencies": False,
        "package_sha256": hashlib.sha256(package).hexdigest(), "package_size": len(package),
        "files": [{"path": p, "sha256": hashlib.sha256(files[p]).hexdigest(), "size": len(files[p])} for p in sorted(files)],
    }
    raw = json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8")
    rm.parse_manifest(raw)                                               # never emit something we would reject
    signature = ed25519.sign(secret, raw)
    os.makedirs(out_dir, exist_ok=True)
    name = name or "csp-agent-%s" % version
    zp, mp, sp = (os.path.join(out_dir, name + ext) for ext in (".zip", ".manifest.json", ".manifest.sig"))
    open(zp, "wb").write(package)
    open(mp, "wb").write(raw)
    open(sp, "w").write(base64.b64encode(signature).decode("ascii") + "\n")
    return zp, mp, sp


def cmd_build(a):
    secret = _load_private(a.private_key)
    state = a.state or os.path.join(os.path.dirname(os.path.abspath(a.private_key)), STATE_FILE)
    keys_file = a.public_keys or os.path.join(os.path.dirname(os.path.abspath(a.private_key)), PUBKEY_FILE)
    key_id = _key_id_for(secret, keys_file)
    seq = _next_sequence(state, a.min_sequence)
    zp, mp, sp = build_release(collect_agent_files(a.agent_dir), a.version, seq, key_id, secret, a.notes, "release",
                               a.min_agent_sequence, a.out)
    _write_sequence(state, seq)
    print("Release %s  sequence %d  signed with key %s" % (a.version, seq, key_id))
    for p in (zp, mp, sp):
        print("  ", p)
    print("Upload all three files on the dashboard: Agent Updates -> Upload release.")
    return 0


def cmd_rollback(a):
    secret = _load_private(a.private_key)
    state = a.state or os.path.join(os.path.dirname(os.path.abspath(a.private_key)), STATE_FILE)
    keys_file = a.public_keys or os.path.join(os.path.dirname(os.path.abspath(a.private_key)), PUBKEY_FILE)
    key_id = _key_id_for(secret, keys_file)
    old_manifest = rm.parse_manifest(open(a.base_manifest, "rb").read())
    old_zip = zipfile.ZipFile(a.base_package)
    files = {p: old_zip.read(p) for p in (f["path"] for f in old_manifest["files"])}
    seq = _next_sequence(state, a.min_sequence)
    zp, mp, sp = build_release(files, a.version, seq, key_id, secret,
                               a.notes or ("Rollback to the code of %s" % old_manifest["version"]),
                               "rollback", 0, a.out, "csp-agent-%s-rollback" % a.version)
    _write_sequence(state, seq)
    print("Rollback release %s (code of %s) sequence %d" % (a.version, old_manifest["version"], seq))
    for p in (zp, mp, sp):
        print("  ", p)
    return 0


def verify_release(package_bytes, manifest_bytes, signature_b64, public_keys):
    """Signature + package check, using the SAME code the server and the agent run."""
    m = rm.verify_signed_release(manifest_bytes, signature_b64, public_keys)
    rm.check_package(package_bytes, m)
    return m


def cmd_verify(a):
    keys = json.load(open(a.public_keys or os.path.join(AGENT_DIR, PUBKEY_FILE), "r", encoding="utf-8"))
    try:
        m = verify_release(open(a.package, "rb").read(), open(a.manifest, "rb").read(), open(a.signature).read(), keys)
    except rm.ManifestError as e:
        print("INVALID:", e)
        return 1
    print("VALID: version %s, sequence %d, %d files" % (m["version"], m["sequence"], len(m["files"])))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    k = sub.add_parser("keygen")
    k.add_argument("--out", required=True)
    k.add_argument("--key-id")
    k.add_argument("--allow-in-repo", action="store_true")
    for name in ("build", "rollback"):
        b = sub.add_parser(name)
        b.add_argument("--private-key", required=True)
        b.add_argument("--public-keys")
        b.add_argument("--state")
        b.add_argument("--version", required=True)
        b.add_argument("--notes", default="")
        b.add_argument("--out", required=True)
        b.add_argument("--min-sequence", type=int, default=0, help="never issue a sequence at or below this")
        if name == "build":
            b.add_argument("--agent-dir", default=AGENT_DIR)
            b.add_argument("--min-agent-sequence", type=int, default=0)
        else:
            b.add_argument("--base-package", required=True)
            b.add_argument("--base-manifest", required=True)
    v = sub.add_parser("verify")
    v.add_argument("--package", required=True)
    v.add_argument("--manifest", required=True)
    v.add_argument("--signature", required=True)
    v.add_argument("--public-keys")
    a = ap.parse_args(argv)
    return {"keygen": cmd_keygen, "build": cmd_build, "rollback": cmd_rollback, "verify": cmd_verify}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
