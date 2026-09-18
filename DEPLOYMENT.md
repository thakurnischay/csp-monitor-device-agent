# Deployment

## Environment variables

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `CSP_MONITOR_SECRET_KEY` | **Yes, in production** | random per-process | Flask session signing key. If unset, the server logs a warning and uses a random key — sessions still work but don't survive a restart. Generate one with `python -c "import secrets; print(secrets.token_hex(32))"`. |
| `CSP_MONITOR_DB_PATH` | No | `<server dir>/monitor.db` | Where the SQLite file lives. Set this explicitly when using a Docker volume (see below). |
| `CSP_MONITOR_FORCE_HTTPS` | No | `0` | Set to `1` once the deployment is genuinely behind TLS (Tailscale Funnel/nginx) — forces `Secure` session cookies. Leave `0` for local HTTP-only dev, or login sessions silently won't persist. |
| `CSP_MONITOR_LOG_PATH` | No | `<server dir>/server.log` | Rotating log file location (5MB × 3 backups). |
| `CSP_MONITOR_LOG_LEVEL` | No | `INFO` | `DEBUG`/`INFO`/`WARNING`/`ERROR`. |
| `PORT` | No | `5100` | Only used by `python app.py`'s own dev-server `__main__` block — not relevant when running under `waitress-serve` (see Dockerfile). |

**Never hardcode `CSP_MONITOR_SECRET_KEY` into a `Dockerfile` `ENV` line** —
this was done once in this project's history and the key ended up
permanently baked into the built image layer and, briefly, in a public-
facing config file. Pass it at `docker run`/`compose.yml` time instead.

## Docker

**`Dockerfile`** (already in `server/`): builds a small Python image running
`waitress-serve` (production WSGI, not Flask's dev server) on port 5100.

### Option A — plain `docker run` with a bind-mounted file

```
cd server
docker build -t csp-monitor .
docker run -d --name csp-monitor --restart unless-stopped \
  -p 127.0.0.1:5100:5100 \
  -v $(pwd)/monitor.db:/app/monitor.db \
  -e CSP_MONITOR_SECRET_KEY="$(python3 -c 'import secrets;print(secrets.token_hex(32))')" \
  csp-monitor
```

### Option B — Docker Compose with a named volume (the current live pattern)

```yaml
# compose.yml
services:
  app:
    build:
      context: ./server
    container_name: csp-monitor
    ports:
      - "127.0.0.1:5100:5100"
    env_file:
      - .env      # CSP_MONITOR_SECRET_KEY=..., CSP_MONITOR_DB_PATH=/data/monitor.db
    volumes:
      - csp_monitor_data:/data
    restart: unless-stopped
    healthcheck:
      test: ["CMD", "python3", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:5100/health').getcode()==200 else 1)"]
      interval: 30s
      timeout: 5s
      retries: 3

volumes:
  csp_monitor_data:
    name: csp_monitor_data
```

With this pattern, `CSP_MONITOR_DB_PATH=/data/monitor.db` in `.env` matters —
without it, the app defaults to writing `monitor.db` next to the source
files inside the image (not the named volume), and the database silently
doesn't persist across container recreation.

**⚠️ Real incident, twice on this project:** a fresh redeploy that only
brought code (e.g. `git clone` + `docker compose up`) started with a
genuinely empty database — the schema creates itself fine, but there's no
data in it until CSPs report in again, or a backup is restored. **A restore
was needed both times** because the original server was fully wiped as part
of unrelated infrastructure changes. See [OPERATIONS.md](OPERATIONS.md) for
the backup/restore procedure — do this *before* any planned infrastructure
work, not after discovering it's needed.

## Exposing it publicly (Tailscale Funnel)

The live deployment uses [Tailscale Funnel](https://tailscale.com/kb/1223/funnel)
rather than traditional port-forwarding:

```
tailscale funnel --bg --https=8443 http://127.0.0.1:5100
tailscale funnel status   # always verify after any funnel command
```

**Syntax gotchas that caused real incidents:**

- `tailscale funnel --bg <port> on` retargets the **root/default** funnel
  entry regardless of the port number given — this once silently redirected
  another team's live service on this shared box. Always use the
  `--https=<port>` form shown above, and always run `tailscale funnel
  status` before *and* after to compare.
- If another process on the same host (e.g. an `nginx` instance serving a
  different project) is already bound to `0.0.0.0:<port>`, it wins the OS-
  level bind and Tailscale's own funnel listener silently fails for that
  port (visible via `sudo ss -tlnp | grep :<port>` and in `journalctl -u
  tailscaled`, as `bind: address already in use`). The fix is either freeing
  that port or picking a different one for this app — not fighting over it.

## Running locally for development

```
cd server
pip install -r requirements.txt
python app.py
```

No Docker needed for local dev — `python app.py` runs Flask's own dev
server directly, with `CSP_MONITOR_FORCE_HTTPS` left unset (`0`) so login
works over plain `http://127.0.0.1:5100`.
