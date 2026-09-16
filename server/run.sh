#!/bin/bash
cd "$(dirname "$0")"
export CSP_MONITOR_SECRET_KEY="kiY4CwzhXbL2zgIQgi2aLUBhc61af1xHXqvz7B6capxgRjJqBRQZMlDvarlMMHmo"
exec venv/bin/waitress-serve --host=0.0.0.0 --port=5100 app:app
