#!/bin/bash
# Never hardcode CSP_MONITOR_SECRET_KEY here - a real production key was
# found committed in this exact file during the Step 7 production review and
# had to be removed. Put real secrets in a local, gitignored .env file (or
# your process manager's own env config) instead, and source it here if
# present. If nothing sets it, app.py falls back to a random per-process key
# with a logged warning rather than a predictable one.
cd "$(dirname "$0")"
if [ -f .env ]; then
    set -a
    source .env
    set +a
fi
exec venv/bin/waitress-serve --host=0.0.0.0 --port=5100 app:app
