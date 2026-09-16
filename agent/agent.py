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

import reporter
import local_ui

LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "agent.log")


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.FileHandler(LOG_PATH, encoding="utf-8"), logging.StreamHandler()],
    )
    reporter.start_background()
    local_ui.run()


if __name__ == "__main__":
    main()
