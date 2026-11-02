"""The updater: swaps in a STAGED, already-verified release - and undoes it if the new version does not come up.

Design limits (deliberate - this is the only code that replaces the agent's own files):
  * It does NOT download, does NOT talk to the server, and runs NO script or shell command supplied by the server.
    update_client.py has already verified the signature and every file hash and staged the files; this module only
    copies those staged files over the installation, from the list in the signed manifest.
  * It needs NO elevation: everything happens inside the agent's own folder (per-user install), so there is no
    privileged updater to attack. It cannot touch anything outside that folder (paths are re-validated here).
  * It never replaces this PC's own data (agent_config.json, history, state, lock).
  * Every replaced file is backed up first. If the new version is not healthy in time (or anything fails while
    copying), the backup is restored and the previous version is started again.
  * Standard library only, so it still works if a release breaks everything else.

State machine in update_state.json (written atomically, format 1):
    staged -> applying -> restarting -> healthy -> reported            (success; "reported" = server has acknowledged)
                  \\______________\\_> rolling_back -> rolled_back      (failure, previous version restored)
    failed                                                            (nothing was changed)
Run as a separate process: `python apply_update.py <agent_dir>` (update_client starts it, then the agent exits).
"""
import json
import logging
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone

import release_manifest as rm

STATE_NAME = "update_state.json"
STAGING = "update_staging"
BACKUP = "update_backup"
LAST_FILES = ("apply_update.py", "agent.py")        # replaced last: they hold the crash-recovery hooks
log = logging.getLogger("csp_agent.apply_update")


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ------------------------------------------------------------------------------------------------ state file
def state_path(agent_dir):
    return os.path.join(agent_dir, STATE_NAME)


def read_state(agent_dir):
    try:
        with open(state_path(agent_dir), "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_state(agent_dir, **fields):
    """Merge `fields` into the state and write it atomically (a crash never leaves half a file)."""
    st = read_state(agent_dir)
    st.update(fields)
    st["format"] = 1
    st["updated"] = _now()
    path = state_path(agent_dir)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f)
    os.replace(tmp, path)
    return st


def clear_state(agent_dir):
    try:
        os.remove(state_path(agent_dir))
    except OSError:
        pass


# ---------------------------------------------------------------------------------------- process helpers
def pid_running(pid):
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            out = subprocess.run(["tasklist", "/FI", "PID eq %d" % pid, "/NH"], capture_output=True, text=True, timeout=15,
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
            return str(pid) in out
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def kill_tree(pid):
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(int(pid)), "/T", "/F"], capture_output=True, timeout=30,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        else:
            os.kill(int(pid), 9)
    except Exception:
        pass


def start_agent(agent_dir):
    """Start agent.py detached (no console, survives this process). Returns the new PID."""
    flags = 0
    if os.name == "nt":
        flags = 0x00000008 | 0x00000200 | 0x08000000       # DETACHED_PROCESS | NEW_PROCESS_GROUP | NO_WINDOW
    p = subprocess.Popen([sys.executable, os.path.join(agent_dir, "agent.py")], cwd=agent_dir, creationflags=flags,
                         close_fds=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return p.pid


# ------------------------------------------------------------------------------------------------- file ops
def _safe_target(agent_dir, rel):
    """The absolute target for a manifest path, or raises. Re-validated here even though the manifest was
    already checked: this is the last line of defence before a write."""
    if not rm.safe_relpath(rel) or rel.split("/")[-1] in rm.PROTECTED_NAMES:
        raise ValueError("refusing to touch %r" % rel)
    root = os.path.realpath(agent_dir)
    target = os.path.realpath(os.path.join(root, *rel.split("/")))
    if os.path.commonpath([root, target]) != root:
        raise ValueError("path escapes the install folder: %r" % rel)
    return target


def _atomic_copy(src, dst):
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    tmp = dst + ".new"
    shutil.copyfile(src, tmp)
    os.replace(tmp, dst)


def _ordered(files):
    return sorted(files, key=lambda p: (p in LAST_FILES, LAST_FILES.index(p) if p in LAST_FILES else 0, p))


def _backup_dir(agent_dir, st):
    return os.path.join(agent_dir, BACKUP, "%s-%s" % (st.get("from_version", "old"), st.get("from_sequence", 0)))


def _restore(agent_dir, st):
    """Put every replaced file back and delete files that did not exist before. Idempotent."""
    bdir = st.get("backup_dir") or _backup_dir(agent_dir, st)
    meta_path = os.path.join(bdir, "backup.json")
    try:
        with open(meta_path, "r", encoding="utf-8") as f:
            meta = json.load(f)
    except (OSError, ValueError):
        return False
    for rel in meta.get("replaced", []):
        _atomic_copy(os.path.join(bdir, "files", *rel.split("/")), _safe_target(agent_dir, rel))
    for rel in meta.get("new_files", []):
        try:
            os.remove(_safe_target(agent_dir, rel))
        except OSError:
            pass
    return True


def _rollback(agent_dir, st, reason, start, kill, new_pid=None):
    log.error("rolling back: %s", reason)
    write_state(agent_dir, phase="rolling_back", error=reason)
    if new_pid:
        kill(new_pid)
    ok = False
    try:
        ok = _restore(agent_dir, st)
    except Exception as e:                                           # pragma: no cover - last resort
        log.error("restore failed: %s", e)
    try:
        start(agent_dir)                                             # bring the previous version back
    except Exception as e:
        log.error("could not restart the previous version: %s", e)
    write_state(agent_dir, phase="rolled_back", error=reason, restored=ok)
    return "rolled_back"


# ------------------------------------------------------------------------------------------------ the update
def apply(agent_dir, start=start_agent, kill=kill_tree, running=pid_running, sleep=time.sleep, clock=time.time,
          health_timeout=180, exit_timeout=60, poll=2):
    """Swap in the staged release. Returns "healthy" | "rolled_back" | "aborted" | "nothing-to-do"."""
    st = read_state(agent_dir)
    if st.get("phase") != "staged":
        return "nothing-to-do"
    staged = os.path.join(agent_dir, STAGING, str(st.get("to_sequence")), "new")
    files = list(st.get("files") or [])
    # 1. the old agent must be gone before its files change
    deadline = clock() + exit_timeout
    while st.get("agent_pid") and running(st["agent_pid"]):
        if clock() > deadline:
            write_state(agent_dir, phase="failed", error="the running agent did not stop for the update; nothing was changed")
            return "aborted"
        sleep(poll)
    # 2. back up everything that will be replaced
    bdir = _backup_dir(agent_dir, st)
    shutil.rmtree(bdir, ignore_errors=True)
    os.makedirs(os.path.join(bdir, "files"), exist_ok=True)
    replaced, new_files = [], []
    try:
        for rel in files:
            target = _safe_target(agent_dir, rel)
            if os.path.isfile(target):
                dst = os.path.join(bdir, "files", *rel.split("/"))
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copyfile(target, dst)
                replaced.append(rel)
            else:
                new_files.append(rel)
        with open(os.path.join(bdir, "backup.json"), "w", encoding="utf-8") as f:
            json.dump({"replaced": replaced, "new_files": new_files}, f)
    except Exception as e:
        write_state(agent_dir, phase="failed", error="could not back up the current version: %s" % e)
        return "aborted"
    st = write_state(agent_dir, phase="applying", backup_dir=bdir)
    # 3. copy the staged files over the installation, one atomic replace at a time
    try:
        for rel in _ordered(files):
            _atomic_copy(os.path.join(staged, *rel.split("/")), _safe_target(agent_dir, rel))
    except Exception as e:
        return _rollback(agent_dir, st, "could not install the new files: %s" % e, start, kill)
    # 4. start the new version and wait until IT says it is healthy
    write_state(agent_dir, phase="restarting", restarted_at=_now())
    try:
        pid = start(agent_dir)
    except Exception as e:
        return _rollback(agent_dir, st, "the new version could not be started: %s" % e, start, kill)
    deadline = clock() + health_timeout
    while clock() < deadline:
        cur = read_state(agent_dir)
        if cur.get("phase") in ("healthy", "reported"):
            shutil.rmtree(os.path.join(agent_dir, STAGING), ignore_errors=True)
            log.info("update to %s is healthy", st.get("to_version"))
            return "healthy"
        if not running(pid):
            return _rollback(agent_dir, st, "the new version stopped right after starting", start, kill, None)
        sleep(poll)
    return _rollback(agent_dir, st, "the new version did not report healthy within %d seconds" % health_timeout, start, kill, pid)


def recover_if_needed(agent_dir):
    """Called first thing at agent start-up: if an update was interrupted half-way (power cut, crash), restore the
    backup so the agent never runs on a mixture of old and new files. True if it had to recover."""
    try:
        st = read_state(agent_dir)
        if st.get("phase") not in ("applying", "rolling_back"):
            return False
        ok = _restore(agent_dir, st)
        write_state(agent_dir, phase="rolled_back", restored=ok,
                    error="an update was interrupted and the previous version was restored" if ok
                    else "an update was interrupted and the previous version could not be fully restored")
        return True
    except Exception:
        return False


# ------------------------------------------------- the outcome the heartbeat must report (read by reporter)
def pending_result(agent_dir):
    """The update outcome the server has not acknowledged yet, in the heartbeat's shape, or None."""
    st = read_state(agent_dir)
    phase = st.get("phase")
    if phase == "healthy":
        return {"deployment_id": st.get("deployment_id"), "attempt": st.get("attempt"), "outcome": "healthy",
                "detail": "installed %s and passed its health check" % st.get("to_version")}
    if phase == "rolled_back":
        return {"deployment_id": st.get("deployment_id"), "attempt": st.get("attempt"), "outcome": "rolled_back",
                "detail": str(st.get("error") or "rolled back")[:250]}
    if phase == "failed" and st.get("deployment_id") and not st.get("failure_reported"):
        return {"deployment_id": st.get("deployment_id"), "attempt": st.get("attempt"), "outcome": "failed",
                "detail": str(st.get("error") or "failed")[:250]}
    return None


def acknowledge_result(agent_dir):
    st = read_state(agent_dir)
    if st.get("phase") == "healthy":
        write_state(agent_dir, phase="reported")
    elif st.get("phase") == "rolled_back":
        write_state(agent_dir, phase="reported_rollback")
    elif st.get("phase") == "failed":
        write_state(agent_dir, failure_reported=True)


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    agent_dir = os.path.abspath(argv[0]) if argv else os.path.dirname(os.path.abspath(__file__))
    logging.basicConfig(filename=os.path.join(agent_dir, "update.log"), level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    try:
        result = apply(agent_dir)
        log.info("updater finished: %s", result)
    except Exception:
        log.exception("updater crashed")
        recover_if_needed(agent_dir)
        try:
            start_agent(agent_dir)
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
