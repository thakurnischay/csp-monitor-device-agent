"""Agent side of centralized updates. Outbound only: the agent asks; nothing ever connects in to the PC.

Lifecycle (each step reports to the dashboard; any failure leaves the working installation untouched):

    heartbeat reply says "update available" ->
    1  fetch the signed manifest                       (HTTPS, this CSP's own API key)
    2  verify signature, product/platform, sequence    (refuses downgrades, unknown keys, forgeries)
    3  check compatibility and free disk space
    4  download the package, resuming if interrupted   (HTTP Range)
    5  verify size, SHA-256 and EVERY file against the signed list
    6  stage the files in a side folder and compile-check the Python (the live install is not touched)
    7  hand over to apply_update.py and exit           (it swaps files, restarts, waits for health, rolls back if needed)
    8  the NEW version verifies itself, then reports healthy in its heartbeat; the server decides "Successful"

It never runs anything the server sends: the only thing launched is this installation's own apply_update.py.
"""
import base64
import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
import zipfile

import requests

import apply_update as au
import release_manifest as rm

log = logging.getLogger("csp_agent.update")
AGENT_DIR = os.path.dirname(os.path.abspath(__file__))
HTTP_TIMEOUT = (5, 60)
CHUNK = 64 * 1024
MIN_FREE_EXTRA = 30 * 1024 * 1024
RETRY_AFTER_FAILURE_HOURS = 6
MAX_NETWORK_FAILURES = 10


class UpdateFailure(Exception):
    """A reason the update cannot proceed, in words the dashboard can show."""


class Stopped(Exception):
    """The server told us not to proceed (cancelled / paused / revoked)."""


class NetworkTrouble(Exception):
    """A temporary network problem: try again at the next heartbeat (the partial download is kept)."""


def load_public_keys(agent_dir=AGENT_DIR):
    try:
        with open(os.path.join(agent_dir, "release_public_keys.json"), "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"keys": []}


class UpdateClient:
    def __init__(self, agent_dir, server_url, csp_id, api_key, public_keys, version, sequence, session=None,
                 free_bytes=None, launch=None, exit_process=None, max_download_attempts=4):
        self.dir = agent_dir
        self.base = server_url.rstrip("/")
        self.csp_id = csp_id
        self.key = (api_key or "").strip()
        self.keys = public_keys
        self.version = version
        self.sequence = int(sequence or 0)
        self.http = session or requests.Session()
        self.free_bytes = free_bytes or (lambda path: shutil.disk_usage(path).free)
        self.launch = launch or self._launch_applier
        self.exit_process = exit_process or self._exit
        self.max_download_attempts = max_download_attempts
        self.staging = os.path.join(agent_dir, au.STAGING)

    # ---------------------------------------------------------------------------------------- plumbing
    def _headers(self, extra=None):
        h = {"X-API-Key": self.key}
        h.update(extra or {})
        return h

    def _report(self, deployment_id, attempt, status, detail=""):
        """Tell the dashboard where we are. Raises Stopped if the server says not to continue."""
        try:
            r = self.http.post(self.base + "/api/update/status", headers=self._headers(), timeout=HTTP_TIMEOUT,
                               json={"csp_id": self.csp_id, "deployment_id": deployment_id, "attempt": attempt,
                                     "status": status, "detail": detail})
            body = r.json() if r.status_code == 200 else {}
        except (requests.RequestException, ValueError):
            return True                       # cannot reach the dashboard right now: carry on, the heartbeat will tell it later
        if status in ("downloading", "verifying", "installing", "restarting") and body.get("proceed") is False:
            raise Stopped(body.get("note") or "the server stopped this update")
        return True

    def _launch_applier(self):
        flags = 0
        if os.name == "nt":
            flags = 0x00000008 | 0x00000200 | 0x08000000
        subprocess.Popen([sys.executable, os.path.join(self.dir, "apply_update.py"), self.dir], cwd=self.dir,
                         creationflags=flags, close_fds=True, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    @staticmethod
    def _exit():
        logging.shutdown()
        os._exit(0)

    # ------------------------------------------------------------------------------------------- the run
    def run(self, offer):
        """Process one offer from the heartbeat. Never raises. -> {"ok", "handed_over", "skipped", "error", "retry"}"""
        try:
            did, attempt = int(offer["deployment_id"]), int(offer.get("attempt", 1))
        except (KeyError, TypeError, ValueError):
            return {"ok": False, "error": "malformed update offer"}
        st = au.read_state(self.dir)
        same = st.get("deployment_id") == did and st.get("attempt") == attempt
        if same and st.get("phase") in ("staged", "applying", "restarting", "healthy", "reported", "rolling_back", "rolled_back", "reported_rollback"):
            return {"ok": True, "skipped": "already handled (%s)" % st["phase"]}      # duplicate command: nothing to do
        if same and st.get("phase") == "failed":
            age = time.time() - _epoch(st.get("updated"))
            if age < RETRY_AFTER_FAILURE_HOURS * 3600:
                return {"ok": False, "skipped": "this attempt already failed: %s" % st.get("error")}
        if int(offer.get("sequence", 0) or 0) == self.sequence:
            return {"ok": True, "skipped": "already installed (sequence %s)" % self.sequence}
        # an OLDER sequence is not skipped silently: it goes on to fail loudly ("downgrades are not allowed"),
        # so the administrator sees that something unexpected was offered
        try:
            return self._run(did, attempt, offer)
        except Stopped as e:
            self._cleanup()
            log.info("update stopped by the server: %s", e)
            return {"ok": True, "skipped": "stopped: %s" % e}
        except NetworkTrouble as e:
            n = int(st.get("net_failures", 0)) + 1 if same else 1
            au.write_state(self.dir, deployment_id=did, attempt=attempt, phase="waiting", net_failures=n, error=str(e)[:200])
            if n >= MAX_NETWORK_FAILURES:
                return self._fail(did, attempt, "the download keeps failing: %s" % e)
            return {"ok": False, "retry": True, "error": str(e)}
        except UpdateFailure as e:
            return self._fail(did, attempt, str(e))
        except Exception as e:                                       # a bug must never harm the running agent
            log.exception("update crashed")
            return self._fail(did, attempt, "unexpected error: %s" % str(e)[:150])

    def _fail(self, did, attempt, message):
        log.warning("update failed: %s", message)
        self._cleanup()
        au.write_state(self.dir, deployment_id=did, attempt=attempt, phase="failed", error=message, failure_reported=False)
        try:
            self._report(did, attempt, "failed", message)
            au.acknowledge_result(self.dir)
        except Exception:
            pass
        return {"ok": False, "error": message}

    def _cleanup(self):
        shutil.rmtree(self.staging, ignore_errors=True)

    def _run(self, did, attempt, offer):
        # 1-2: manifest
        manifest, raw = self._fetch_manifest(did)
        self._report(did, attempt, "downloading", "fetching %s" % manifest["version"])
        # 3: compatibility
        self._check_compatible(manifest, offer)
        # 4: download (resumable) + 5: verify
        package = self._download(did, attempt, manifest)
        self._report(did, attempt, "verifying", "checking the signed hashes")
        try:
            rm.check_package(package, manifest)
        except rm.ManifestError as e:
            raise UpdateFailure("the downloaded package is not what was signed: %s" % e)
        # 6: stage + compile check
        staged = self._stage(package, manifest)
        # 7: hand over
        if not os.path.isfile(os.path.join(self.dir, "apply_update.py")):
            raise UpdateFailure("this installation has no updater (apply_update.py is missing)")
        self._report(did, attempt, "installing", "handing over to the updater")
        au.write_state(self.dir, deployment_id=did, attempt=attempt, phase="staged", to_version=manifest["version"],
                       to_sequence=manifest["sequence"], from_version=self.version, from_sequence=self.sequence,
                       files=[f["path"] for f in manifest["files"]], agent_pid=os.getpid(), error=None, net_failures=0,
                       failure_reported=False, staged_dir=staged)
        log.info("update to %s staged; handing over to the updater", manifest["version"])
        self.launch()
        self.exit_process()                      # the applier waits for this process to end
        return {"ok": True, "handed_over": True}

    # ----------------------------------------------------------------------------------------- steps
    def _fetch_manifest(self, did):
        try:
            r = self.http.get(self.base + "/api/update/manifest", params={"csp_id": self.csp_id, "deployment_id": did},
                              headers=self._headers(), timeout=HTTP_TIMEOUT)
        except requests.RequestException as e:
            raise NetworkTrouble("cannot reach the server: %s" % e)
        if r.status_code == 409:
            raise Stopped(r.json().get("error", "not available"))
        if r.status_code in (401, 404):
            raise UpdateFailure("the server did not accept the request for the manifest (%d)" % r.status_code)
        if r.status_code != 200:
            raise NetworkTrouble("the server answered %d" % r.status_code)
        try:
            body = r.json()
            raw = base64.b64decode(body["manifest_b64"], validate=True)
            manifest = rm.verify_signed_release(raw, body["signature_b64"], self.keys)
        except rm.ManifestError as e:
            raise UpdateFailure("the update is not trusted: %s" % e)
        except Exception:
            raise UpdateFailure("the update manifest was malformed")
        return manifest, raw

    def _check_compatible(self, m, offer):
        if int(offer.get("sequence", m["sequence"])) != m["sequence"] or str(offer.get("version", m["version"])) != m["version"]:
            raise UpdateFailure("the offer and the signed manifest disagree")
        if m["sequence"] < self.sequence:
            raise UpdateFailure("refusing to install an older release (sequence %d < %d): downgrades are not allowed"
                                % (m["sequence"], self.sequence))
        if m["sequence"] == self.sequence:
            raise Stopped("this release is already installed")
        if self.sequence < int(m.get("min_agent_sequence", 0)):
            raise UpdateFailure("this agent is too old to update directly to %s (it needs release %d first)"
                                % (m["version"], m["min_agent_sequence"]))
        need = 3 * m["package_size"] + MIN_FREE_EXTRA
        free = self.free_bytes(self.dir)
        if free < need:
            raise UpdateFailure("not enough free disk space for the update (need about %d MB, %d MB free)"
                                % (need // 1048576, free // 1048576))

    def _download(self, did, attempt, m):
        os.makedirs(self.staging, exist_ok=True)
        part = os.path.join(self.staging, "dl-%d-%d.part" % (did, attempt))
        size = m["package_size"]
        for _ in range(self.max_download_attempts):
            have = os.path.getsize(part) if os.path.exists(part) else 0
            if have == size:
                break
            if have > size:
                os.remove(part)
                have = 0
            headers = self._headers({"Range": "bytes=%d-" % have} if have else None)
            try:
                r = self.http.get(self.base + "/api/update/package", params={"csp_id": self.csp_id, "deployment_id": did},
                                  headers=headers, stream=True, timeout=HTTP_TIMEOUT)
                if r.status_code == 409:
                    raise Stopped(r.json().get("error", "not available"))
                if r.status_code not in (200, 206):
                    raise NetworkTrouble("the server answered %d for the package" % r.status_code)
                mode = "ab" if (have and r.status_code == 206) else "wb"      # a server that ignored Range restarts from zero
                written = have if mode == "ab" else 0
                with open(part, mode) as f:
                    for chunk in r.iter_content(CHUNK):
                        if not chunk:
                            continue
                        written += len(chunk)
                        if written > size:
                            raise UpdateFailure("the download is larger than the signed size")
                        f.write(chunk)
            except (requests.RequestException, OSError) as e:
                log.info("download interrupted (%s); will resume", e)
                continue
        have = os.path.getsize(part) if os.path.exists(part) else 0
        if have != size:
            raise NetworkTrouble("the download was interrupted (%d of %d bytes); it will resume" % (have, size))
        data = open(part, "rb").read()
        if hashlib.sha256(data).hexdigest() != m["package_sha256"]:
            os.remove(part)
            raise UpdateFailure("the downloaded package is damaged or was modified (hash mismatch); it was discarded")
        return data

    def _stage(self, package, m):
        import io
        root = os.path.join(self.staging, str(m["sequence"]), "new")
        shutil.rmtree(os.path.dirname(root), ignore_errors=True)
        z = zipfile.ZipFile(io.BytesIO(package))
        for f in m["files"]:
            target = au._safe_target(root, f["path"])           # staged files obey the same path rules
            os.makedirs(os.path.dirname(target), exist_ok=True)
            data = z.read(f["path"])
            with open(target, "wb") as fh:
                fh.write(data)
            if f["path"].endswith(".py"):
                try:
                    compile(data, f["path"], "exec")             # syntax check ONLY - nothing is executed
                except SyntaxError as e:
                    raise UpdateFailure("the update contains code that does not compile (%s line %s); it was not installed"
                                        % (f["path"], e.lineno))
        return root



def _epoch(iso):
    from datetime import datetime, timezone
    try:
        dt = datetime.fromisoformat(str(iso))
        return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).timestamp()
    except (ValueError, TypeError):
        return 0.0


# ------------------------------------------------------------------------------- hooks used by reporter/agent
_lock = threading.Lock()
_running = False


def payload_extras(agent_dir=AGENT_DIR):
    """Extra heartbeat fields: what this agent can do, its release number, and any un-acknowledged update outcome."""
    import version
    extras = {"capabilities": dict(version.CAPABILITIES), "agent_sequence": version.AGENT_SEQUENCE}
    result = au.pending_result(agent_dir)
    if result:
        extras["update_result"] = result
    return extras


def handle_heartbeat_response(cfg, response_json, sent_result, agent_dir=AGENT_DIR):
    """Called after every successful heartbeat. Acknowledges a delivered outcome; starts an offered update in the background."""
    global _running
    if sent_result:
        au.acknowledge_result(agent_dir)
    offer = response_json.get("update") if isinstance(response_json, dict) else None
    if not isinstance(offer, dict):
        return False
    import version
    with _lock:
        if _running:
            return False
        _running = True

    def work():
        global _running
        try:
            client = UpdateClient(agent_dir, cfg["server_url"], cfg["csp_id"], cfg["api_key"], load_public_keys(agent_dir),
                                  version.AGENT_VERSION, version.AGENT_SEQUENCE)
            outcome = client.run(offer)
            log.info("update run: %s", outcome)
        finally:
            with _lock:
                _running = False

    threading.Thread(target=work, daemon=True, name="csp-agent-update").start()
    return True


def post_start_verification(agent_dir=AGENT_DIR, run_check=None):
    """Run once when the agent starts. If this IS the freshly installed version, prove it works locally, then mark it healthy
    (the applier is waiting for that marker, and rolls back if it never comes)."""
    import version
    st = au.read_state(agent_dir)
    if st.get("phase") != "restarting":
        return None
    if st.get("to_version") != version.AGENT_VERSION or st.get("to_sequence") != version.AGENT_SEQUENCE:
        log.error("started as %s but the update expected %s", version.AGENT_VERSION, st.get("to_version"))
        return False
    try:
        if run_check:
            run_check()
    except Exception as e:
        log.error("post-update health check failed: %s", e)
        au.write_state(agent_dir, health_error=str(e)[:200])
        return False
    au.write_state(agent_dir, phase="healthy", healthy_at=au._now())
    log.info("update to %s verified healthy", version.AGENT_VERSION)
    return True
