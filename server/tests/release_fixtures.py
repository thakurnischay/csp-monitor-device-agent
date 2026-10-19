"""Builds genuinely signed releases for server tests, with the real release tool and a throwaway key."""
import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.path.insert(0, os.path.join(ROOT, "agent"))
import release_tool as rt        # noqa: E402
import ed25519                   # noqa: E402


class Signer:
    """A throwaway signing key + the public-key file the server is pointed at."""

    def __init__(self, key_id="k1"):
        self.secret = os.urandom(32)
        self.key_id = key_id
        self.keys = {"keys": [{"id": key_id, "public": ed25519.secret_to_public(self.secret).hex()}]}
        fd, self.keys_path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        with open(self.keys_path, "w") as f:
            json.dump(self.keys, f)
        os.environ["CSP_MONITOR_RELEASE_PUBKEYS"] = self.keys_path
        self.release_dir = tempfile.mkdtemp()
        os.environ["CSP_MONITOR_RELEASE_DIR"] = self.release_dir

    def files(self, extra=None):
        f = {"agent.py": b"print('agent')\n", "version.py": b'AGENT_VERSION = "0.0.0"\nSCHEMA_VERSION = 4\n',
             "reporter.py": b"# reporter\n"}
        f.update(extra or {})
        return f

    def release(self, version="2.1.0", sequence=2, kind="release", files=None, min_agent_sequence=0, notes="test release"):
        out = tempfile.mkdtemp()
        zp, mp, sp = rt.build_release(files or self.files(), version, sequence, self.key_id, self.secret, notes, kind,
                                      min_agent_sequence, out)
        return open(zp, "rb").read(), open(mp, "rb").read(), open(sp).read()

    def cleanup(self):
        for k in ("CSP_MONITOR_RELEASE_PUBKEYS", "CSP_MONITOR_RELEASE_DIR"):
            os.environ.pop(k, None)
