"""
Entry point for the CSP Device Monitor agent.

Run this once (or install it via install_task.ps1 so Windows starts it
automatically). It does two things at once:
  1. reporter.start_background() — a daemon thread that checks the printer +
     micro-ATM every `interval_seconds` and POSTs the result to the server.
  2. local_ui.run() — a tiny localhost-only web page (default
     http://127.0.0.1:5057) where the CSP does one-time setup: server URL,
     CSP ID, API key, and which local printer/USB device is theirs.

Both keep running for as long as this process is alive, which is the whole
point of installing it as an always-on Windows Scheduled Task (see
install_task.ps1) rather than a one-shot script.
"""
import logging
import os
import signal
import sys
from logging.handlers import RotatingFileHandler

import reporter
import local_ui
import singleton

LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "agent.log")


def _handle_shutdown_signal(signum, frame):
    """Best-effort graceful shutdown. On Windows the real stop mechanism is
    install_task.ps1's Stop-Process -Force during a reinstall, which bypasses
    Python signal handling entirely - this exists for the cases where it
    DOES apply (Ctrl+C in a console, or running on a different OS), not as
    the primary shutdown path for this deployment."""
    logging.getLogger("csp_agent").info("received shutdown signal %s - stopping", signum)
    reporter.stop_background()
    singleton.release()
    sys.exit(0)


def main():
    # Rotating, not a plain FileHandler - this runs unattended on a CSP PC
    # for months, an unbounded log file would eventually fill the disk.
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[RotatingFileHandler(LOG_PATH, maxBytes=2 * 1024 * 1024, backupCount=2, encoding="utf-8"),
                 logging.StreamHandler()],
    )
    singleton.acquire_or_exit()
    try:
        signal.signal(signal.SIGINT, _handle_shutdown_signal)
        signal.signal(signal.SIGTERM, _handle_shutdown_signal)
    except (ValueError, AttributeError, OSError):
        pass  # not every platform/thread context supports this - non-fatal

    reporter.start_background()
    try:
        local_ui.run()
    finally:
        singleton.release()


if __name__ == "__main__":
    main()
