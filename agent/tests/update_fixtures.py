"""Shared test scaffolding for the update tests: a signer, an installed agent folder, and a fake server."""
import base64
import json
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "agent"))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import ed25519                  # noqa: E402
import release_tool as rt       # noqa: E402

INSTALLED = {
    "agent.py": b"# old agent\n", "reporter.py": b"# old reporter\n", "version.py": b'AGENT_VERSION = "2.0.0"\nAGENT_SEQUENCE = 1\n',
    "apply_update.py": b"# old updater\n", "release_public_keys.json": b"{}",
    "agent_config.json": b'{"csp_id": "X1", "api_key": "SECRET-KEEP-ME"}', "diagnostic_history.json": b'{"keep": "history"}',
    "notes_from_the_csp.txt": b"not part of any release - must survive",
}


class Signer:
    def __init__(self, key_id="k1"):
        self.secret = os.urandom(32)
        self.key_id = key_id
        self.keys = {"keys": [{"id": key_id, "public": ed25519.secret_to_public(self.secret).hex()}]}

    def files(self, **overrides):
        f = {"agent.py": b"# NEW agent\n", "reporter.py": b"# NEW reporter\n", "version.py": b'AGENT_VERSION = "0.0.0"\n',
             "apply_update.py": b"# NEW updater\n", "release_public_keys.json": b"{}", "installer/wizard.py": b"# brand new file\n"}
        for k, v in overrides.items():
            if v is None:
                f.pop(k, None)
            else:
                f[k] = v
        return f

    def build(self, version="2.1.0", sequence=2, files=None, **kw):
        out = tempfile.mkdtemp()
        zp, mp, sp = rt.build_release(files or self.files(), version, sequence, self.key_id, self.secret, kw.pop("notes", ""),
                                      kw.pop("kind", "release"), kw.pop("min_agent_sequence", 0), out)
        return Release(open(zp, "rb").read(), open(mp, "rb").read(), open(sp).read().strip())


class Release:
    def __init__(self, package, manifest, signature):
        self.package, self.manifest, self.signature = package, manifest, signature
        self.info = json.loads(manifest)

    @property
    def manifest_b64(self):
        return base64.b64encode(self.manifest).decode()


def install_agent(test, files=None):
    d = tempfile.mkdtemp()
    test.addCleanup(lambda: shutil.rmtree(d, ignore_errors=True))
    for name, data in (files or INSTALLED).items():
        p = os.path.join(d, *name.split("/"))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        open(p, "wb").write(data)
    return d


def snapshot(d):
    out = {}
    for dirpath, _dirs, names in os.walk(d):
        for n in names:
            full = os.path.join(dirpath, n)
            rel = os.path.relpath(full, d).replace(os.sep, "/")
            if rel.startswith(("update_staging", "update_backup")) or rel in ("update_state.json", "update.log") or rel.endswith(".new"):
                continue
            out[rel] = open(full, "rb").read()
    return out


class FakeResponse:
    def __init__(self, status=200, body=None, content=b"", headers=None, break_after=None):
        self.status_code, self._body, self.content = status, body, content
        self.headers = headers or {}
        self._break_after = break_after

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body

    def iter_content(self, chunk):
        import requests
        sent = 0
        for i in range(0, len(self.content), chunk):
            piece = self.content[i:i + chunk]
            if self._break_after is not None and sent + len(piece) > self._break_after:
                cut = self._break_after - sent
                if cut > 0:
                    yield piece[:cut]
                raise requests.ConnectionError("connection dropped mid-download")
            sent += len(piece)
            yield piece


class FakeServer:
    """Speaks the server's update protocol. Records everything the agent sends."""

    def __init__(self, release=None):
        self.release = release
        self.statuses = []                      # [(status, detail)]
        self.package_requests = []              # the Range header (or None) of each package download
        self.manifest_calls = 0
        self.proceed = True                     # False -> status calls answer proceed=False
        self.stop_after_status = None           # answer proceed=False once this status is reported
        self.manifest_status = 200
        self.manifest_override = None           # (manifest_b64, signature)
        self.package_override = None
        self.break_first_download_after = None
        self.ignore_range = False
        self.network_down = False
        self._downloads = 0
        self._stopped = False

    def post(self, url, headers=None, json=None, timeout=None):
        import requests
        if self.network_down:
            raise requests.ConnectionError("down")
        self.statuses.append((json["status"], json.get("detail", "")))
        if self.stop_after_status == json["status"]:
            self._stopped = True
        proceed = self.proceed and not self._stopped
        return FakeResponse(200, {"ok": True, "proceed": proceed, "note": "cancelled by the administrator" if not proceed else ""})

    def get(self, url, params=None, headers=None, stream=False, timeout=None):
        import requests
        if self.network_down:
            raise requests.ConnectionError("down")
        if url.endswith("/api/update/manifest"):
            self.manifest_calls += 1
            if self.manifest_status != 200:
                return FakeResponse(self.manifest_status, {"error": "not available"})
            mb, sig = self.manifest_override or (self.release.manifest_b64, self.release.signature)
            return FakeResponse(200, {"ok": True, "attempt": 1, "manifest_b64": mb, "signature_b64": sig})
        data = self.package_override if self.package_override is not None else self.release.package
        rng = (headers or {}).get("Range")
        self.package_requests.append(rng)
        self._downloads += 1
        brk = self.break_first_download_after if self._downloads == 1 else None
        if rng and not self.ignore_range:
            start = int(rng.split("=")[1].split("-")[0])
            return FakeResponse(206, None, data[start:], break_after=brk)
        return FakeResponse(200, None, data, break_after=brk)
