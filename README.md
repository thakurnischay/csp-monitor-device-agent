# CSP Device Monitor

Tells Eko, from one central dashboard, whether each CSP's passbook printer
and micro-ATM are actually working — without anyone having to call the CSP
or visit in person to find out.

## The two parts

| | Runs where | Purpose |
|---|---|---|
| **[`agent/`](agent/)** | Each CSP's Windows PC | Checks the printer/micro-ATM every few minutes, reports to the server |
| **[`server/`](server/)** | Central rack server | Dashboard, API, database, admin login |

Nothing in this project handles customer transaction data — it only watches
whether the two pieces of hardware are alive and working.

## Documentation

- **[ARCHITECTURE.md](ARCHITECTURE.md)** — how the two halves fit together, the data model, the recognition engine
- **[API.md](API.md)** — the one endpoint every agent talks to, request/response shapes, auth
- **[DEPLOYMENT.md](DEPLOYMENT.md)** — running the server (Docker), environment variables, exposing it publicly
- **[AGENT_INSTALLATION.md](AGENT_INSTALLATION.md)** — installing/updating the agent on a CSP's PC
- **[OPERATIONS.md](OPERATIONS.md)** — health checks, backups, logs, day-to-day admin tasks
- **[TROUBLESHOOTING.md](TROUBLESHOOTING.md)** — real problems hit in the field and how they were actually diagnosed/fixed
- **[SECURITY.md](SECURITY.md)** — what's protected, how, and the security checklist
- **[AUDIT.md](AUDIT.md)** — the original codebase audit this hardening work was based on

## Quick start (local development)

```
cd server
pip install -r requirements.txt
python app.py
```

Opens on `http://127.0.0.1:5100` (or set `PORT`). First run creates
`monitor.db` with a default admin login `admin` / `admin123` — **change this
immediately** on any real deployment (see [SECURITY.md](SECURITY.md)).

## Running the tests

```
cd server && python -m unittest discover -s tests -p "test_*.py"
cd agent  && python -m unittest discover -s tests -p "test_*.py"
```

No test framework dependency beyond Python's own `unittest` and Flask's
built-in test client.
