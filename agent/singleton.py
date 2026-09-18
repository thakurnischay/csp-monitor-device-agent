"""Ensures only one copy of this agent runs at a time, even if launched
directly (python agent.py) rather than through install_task.ps1 - which
already kills any prior copy before starting a new one, but only covers the
install/reinstall path, not someone double-launching the script by hand.
Two live copies would each hold their own config file and silently fight
each other - a real bug already hit once in this project.

Uses a PID lock file + `tasklist` (the same approach device_health.py's
process_running() already uses elsewhere in this project) rather than a new
dependency (no psutil, no pywin32)."""
import logging
import os
import subprocess

log = logging.getLogger("csp_agent.singleton")

_LOCK_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "agent.lock")


def _pid_is_running(pid: int) -> bool:
    try:
        r = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            capture_output=True, text=True, timeout=8,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return str(pid) in (r.stdout or "")
    except Exception:
        return False


def acquire_or_exit() -> None:
    """If another live copy already holds the lock, log and exit immediately.
    A stale lock file (left behind by a crash, not a clean shutdown) is
    detected by checking whether its recorded PID is actually still alive,
    and is safely overwritten if not. Never raises - a lock-file problem
    must never block the agent from starting."""
    try:
        if os.path.isfile(_LOCK_PATH):
            with open(_LOCK_PATH, "r", encoding="utf-8") as f:
                old_pid_text = f.read().strip()
            if old_pid_text.isdigit() and _pid_is_running(int(old_pid_text)):
                log.warning("another agent instance (PID %s) is already running - exiting", old_pid_text)
                raise SystemExit(0)
        with open(_LOCK_PATH, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
    except SystemExit:
        raise
    except Exception:
        log.debug("could not manage the singleton lock file - continuing anyway", exc_info=True)


def release() -> None:
    """Best-effort cleanup on a clean shutdown - never raises."""
    try:
        if not os.path.isfile(_LOCK_PATH):
            return
        with open(_LOCK_PATH, "r", encoding="utf-8") as f:
            is_ours = f.read().strip() == str(os.getpid())
        # os.remove() must happen AFTER the file handle above is closed
        # (the `with` block has exited) - Windows refuses to delete a file
        # that still has an open handle, and that failure was previously
        # swallowed silently by the bare except below.
        if is_ours:
            os.remove(_LOCK_PATH)
    except Exception:
        pass
