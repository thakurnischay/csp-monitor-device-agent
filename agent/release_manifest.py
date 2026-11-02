"""The signed release manifest: format, validation and the safety rules.

A release is: a zip package + a manifest (JSON, exact bytes signed with Ed25519).
The manifest says what the package contains, so the agent can check every file
before it touches the live installation.

Shared by the release tool (administrator's machine), the server (checks uploads)
and the agent (checks downloads). The same file lives in agent/ and server/
(a test fails if the copies differ).

Safety rules enforced here (never relaxed by the server's content):
  * only file TYPES on an allow-list can be installed (no .exe/.dll/.pyd/.whl:
    a release cannot smuggle in binaries or new dependencies);
  * every path must be relative, inside the agent folder, no "..", no drive
    letters, no reserved names;
  * hard caps on file count and sizes (zip-bomb protection);
  * `sequence` is a strictly increasing integer - the agent refuses to go down.
"""
import base64
import hashlib
import io
import json
import re
import zipfile

PRODUCT = "csp-device-monitor"
PLATFORM = "windows"
ALLOWED_EXTENSIONS = (".py", ".bat", ".ps1", ".txt", ".json", ".md", ".csv", ".ico", ".png")
MAX_FILES = 300
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_TOTAL_BYTES = 60 * 1024 * 1024
MAX_MANIFEST_BYTES = 256 * 1024
# files the updater must never overwrite: they hold this PC's identity and history
PROTECTED_NAMES = {"agent_config.json", "diagnostic_history.json", "agent_state.json", "agent.lock",
                   "impact_map.json", "update_state.json"}
_RESERVED = {"con", "prn", "aux", "nul"} | {f"com{i}" for i in range(1, 10)} | {f"lpt{i}" for i in range(1, 10)}
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_VERSION = re.compile(r"^\d{1,4}(\.\d{1,4}){1,3}([-+][0-9A-Za-z.\-]{1,32})?$")


class ManifestError(ValueError):
    """The manifest (or a path in it) is not acceptable. The message is safe to show to an admin."""


def version_tuple(v):
    """'2.1.0' / '2.1.0-rollback' -> (2, 1, 0). Pre-release suffixes do not affect ordering
    (ordering safety comes from `sequence`, not from the version string)."""
    core = re.split(r"[-+]", str(v), maxsplit=1)[0]
    try:
        return tuple(int(x) for x in core.split("."))
    except ValueError:
        return (0,)


def safe_relpath(path):
    """True only for a plain relative path that stays inside the install folder."""
    if not isinstance(path, str) or not path or len(path) > 200:
        return False
    if "\\" in path or path.startswith("/") or ":" in path or "\x00" in path:
        return False
    parts = path.split("/")
    for part in parts:
        if part in ("", ".", "..") or part.strip() != part or part.endswith("."):
            return False
        if part.split(".")[0].lower() in _RESERVED:
            return False
    return path.lower().endswith(ALLOWED_EXTENSIONS)


def parse_manifest(raw):
    """Bytes -> validated manifest dict. Raises ManifestError (never anything else)."""
    if not isinstance(raw, (bytes, bytearray)) or not raw:
        raise ManifestError("the manifest is empty")
    if len(raw) > MAX_MANIFEST_BYTES:
        raise ManifestError("the manifest is too large")
    try:
        m = json.loads(bytes(raw).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise ManifestError("the manifest is not valid JSON")
    if not isinstance(m, dict):
        raise ManifestError("the manifest must be a JSON object")
    if m.get("product") != PRODUCT:
        raise ManifestError("this release is not for CSP Device Monitor")
    if m.get("platform") != PLATFORM:
        raise ManifestError("this release is not for Windows")
    if not isinstance(m.get("version"), str) or not _VERSION.match(m["version"]):
        raise ManifestError("the version must look like 2.1.0")
    seq = m.get("sequence")
    if not isinstance(seq, int) or isinstance(seq, bool) or not (1 <= seq <= 10 ** 9):
        raise ManifestError("the sequence must be a whole number of at least 1")
    if m.get("kind", "release") not in ("release", "rollback"):
        raise ManifestError("the release kind must be release or rollback")
    if not isinstance(m.get("key_id"), str) or not re.match(r"^[A-Za-z0-9_.-]{1,40}$", m["key_id"]):
        raise ManifestError("the signing key id is missing or invalid")
    if not _HEX64.match(str(m.get("package_sha256", ""))):
        raise ManifestError("the package hash must be 64 hex characters")
    size = m.get("package_size")
    if not isinstance(size, int) or isinstance(size, bool) or not (1 <= size <= MAX_TOTAL_BYTES):
        raise ManifestError("the package size is missing or too large")
    if m.get("requires_new_dependencies"):
        raise ManifestError("this release needs new software packages - it must be installed manually, not by the updater")
    if "min_agent_sequence" in m and (not isinstance(m["min_agent_sequence"], int) or m["min_agent_sequence"] < 0):
        raise ManifestError("min_agent_sequence must be a whole number")
    files = m.get("files")
    if not isinstance(files, list) or not files or len(files) > MAX_FILES:
        raise ManifestError("the file list is missing or too long")
    seen, total = set(), 0
    for f in files:
        if not isinstance(f, dict):
            raise ManifestError("a file entry is malformed")
        path = f.get("path")
        if not safe_relpath(path):
            raise ManifestError("unsafe or disallowed file path in the release: %r" % (str(path)[:80],))
        if path.lower() in seen:
            raise ManifestError("the same file is listed twice: %s" % path)
        seen.add(path.lower())
        if path.split("/")[-1].lower() in PROTECTED_NAMES:
            raise ManifestError("a release may not replace this PC's own data file: %s" % path)
        if not _HEX64.match(str(f.get("sha256", ""))):
            raise ManifestError("a file hash is invalid: %s" % path)
        fsz = f.get("size")
        if not isinstance(fsz, int) or isinstance(fsz, bool) or not (0 <= fsz <= MAX_FILE_BYTES):
            raise ManifestError("a file size is invalid or too large: %s" % path)
        total += fsz
    if total > MAX_TOTAL_BYTES:
        raise ManifestError("the release is too large")
    if "agent.py" not in {f["path"] for f in files}:
        raise ManifestError("a release must contain agent.py")
    notes = m.get("notes", "")
    if not isinstance(notes, str) or len(notes) > 4000:
        raise ManifestError("the release notes are too long")
    return m


def verify_signed_release(manifest_bytes, signature_b64, public_keys):
    """Signature check, then validation. Returns the manifest dict or raises ManifestError.

    The signature is over the EXACT manifest bytes - nothing is parsed or trusted before it verifies
    (parse_manifest below only looks at fields after the signature is good for the size/structure
    it needs, never to decide trust)."""
    import ed25519
    m = parse_manifest(manifest_bytes)                       # structure first: cheap, and rejects junk early
    keys = {}
    for k in (public_keys or {}).get("keys", []):
        try:
            keys[str(k["id"])] = bytes.fromhex(k["public"])
        except (KeyError, ValueError, TypeError):
            continue
    if m["key_id"] not in keys:
        raise ManifestError("the release was signed with an unknown key")
    try:
        sig = base64.b64decode(str(signature_b64).strip(), validate=True)
    except Exception:
        raise ManifestError("the signature is not valid base64")
    if not ed25519.verify(keys[m["key_id"]], bytes(manifest_bytes), sig):
        raise ManifestError("the signature does not match - the release was modified or signed by someone else")
    return m


def check_package(package_bytes, manifest):
    """The package must be EXACTLY what the signed manifest lists: same bytes, same files, nothing extra.
    Raises ManifestError. Safe against zip-slip and zip bombs (declared sizes are checked before reading)."""
    if hashlib.sha256(package_bytes).hexdigest() != manifest["package_sha256"] or len(package_bytes) != manifest["package_size"]:
        raise ManifestError("the package does not match the signed hash")
    try:
        z = zipfile.ZipFile(io.BytesIO(package_bytes))
    except zipfile.BadZipFile:
        raise ManifestError("the package is not a valid zip file")
    listed = {f["path"]: f for f in manifest["files"]}
    infos = [i for i in z.infolist() if not i.is_dir()]
    names = [i.filename for i in infos]
    if len(names) != len(set(names)):
        raise ManifestError("the package contains the same file twice")
    for name in names:
        if name not in listed:
            raise ManifestError("the package contains a file that is not in the signed list: %r" % name[:80])
    if set(names) != set(listed):
        raise ManifestError("the package is missing files the signed list requires")
    for i in infos:
        f = listed[i.filename]
        if i.file_size != f["size"] or i.file_size > MAX_FILE_BYTES:
            raise ManifestError("a file in the package has the wrong size: %s" % i.filename)
        with z.open(i) as fh:
            data = fh.read(f["size"] + 1)
        if len(data) != f["size"] or hashlib.sha256(data).hexdigest() != f["sha256"]:
            raise ManifestError("a file in the package does not match its signed hash: %s" % i.filename)
    return True

