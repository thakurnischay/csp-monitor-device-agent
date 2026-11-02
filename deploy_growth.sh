#!/usr/bin/env bash
# Deploy "Device Sales" (the CSP Growth Platform merged at /growth) to the live csp-heartbeat-v2 container - SAFELY.
#   1. stages a copy of the live source, adds growth_platform/ and small additive patches (app.py, base.html, hindi.py,
#      requirements.txt, .dockerignore) - every other change already on the server is kept
#   2. renders every admin page AND the new /growth pages from that copy in a throwaway container (temp database)
#   3. only if all of that works: backs up, copies the Growth data into the container's data volume (never overwriting
#      data already there), adds the settings to .env, switches the live source, rebuilds, checks
# If anything fails before step 3 the live dashboard is never touched. Drip emails stay DRY (GROWTH_EMAIL_MODE=dry).
# Usage (on the server, as kapil):  bash ~/growth/deploy_growth.sh        undo:  bash ~/growth/deploy_growth.sh --undo
set -euo pipefail
NEW=~/growth
COMPOSE_DIR=~/csp-heartbeat-v2
IMAGE=csp-heartbeat-v2-app
CONTAINER=csp-heartbeat-v2
PORT=8215
PUBLIC_URL=https://indev.eko.in/csp-device-heartbeat/growth

APP_DIR=$(dirname "$(find "$COMPOSE_DIR" -maxdepth 4 -name registry.py -not -path '*/tests/*' | head -1)")
[ -f "$APP_DIR/app.py" ] || { echo "STOP: could not find the dashboard source under $COMPOSE_DIR"; exit 1; }
ENV_FILE="$COMPOSE_DIR/.env"

if [ "${1:-}" = "--undo" ]; then
  B=$(ls -d ~/backup-growth-* 2>/dev/null | tail -1)
  [ -n "$B" ] || { echo "STOP: no ~/backup-growth-* folder found"; exit 1; }
  echo "== restoring $B into $APP_DIR"
  cp "$B"/app.py "$B"/requirements.txt "$APP_DIR"/ && cp "$B"/base.html "$APP_DIR"/templates/
  [ -f "$B"/hindi.py ] && cp "$B"/hindi.py "$APP_DIR"/
  if [ -f "$B"/dockerignore ]; then cp "$B"/dockerignore "$APP_DIR"/.dockerignore; else rm -f "$APP_DIR"/.dockerignore; fi
  [ -f "$B"/dotenv ] && sudo cp "$B"/dotenv "$ENV_FILE"
  rm -rf "$APP_DIR"/growth_platform
  cd "$COMPOSE_DIR" && sudo docker compose up -d --build
  echo "UNDONE: the previous dashboard is back. The Growth data in the container's data folder was left in place."
  exit 0
fi

[ -f "$NEW/growth_platform.tgz" ] || { echo "STOP: $NEW/growth_platform.tgz is missing - copy the files first (step 1)"; exit 1; }
sudo test -f "$ENV_FILE" || { echo "STOP: $ENV_FILE not found - tell Claude how the dashboard's settings are passed in"; exit 1; }
echo "== live source: $APP_DIR"

# Where the dashboard keeps its database inside the container = the persistent volume. Growth data goes next to it.
DB_PATH=$(sudo docker exec "$CONTAINER" printenv CSP_MONITOR_DB_PATH 2>/dev/null || true)
[ -n "$DB_PATH" ] || { echo "STOP: CSP_MONITOR_DB_PATH is not set in $CONTAINER - the data volume cannot be located"; exit 1; }
GROWTH_DIR="$(dirname "$DB_PATH")/growth"
echo "== Growth data folder (inside the container): $GROWTH_DIR"

STAGE=$(mktemp -d ~/growth-stage-XXXX)
cp -a "$APP_DIR"/. "$STAGE"/
rm -rf "$STAGE"/growth_platform
tar -xzf "$NEW"/growth_platform.tgz -C "$STAGE"
[ -f "$STAGE/growth_platform/routes.py" ] || { echo "STOP: growth_platform.tgz did not unpack to growth_platform/"; exit 1; }
# Preflight data: the copies uploaded with the first deploy, or (on a re-deploy) the live files from the container.
mkdir -p "$STAGE/_preflight_data"
for f in state.json csps.json; do
  if [ -f "$NEW/$f" ]; then cp "$NEW/$f" "$STAGE/_preflight_data/"
  elif sudo docker exec "$CONTAINER" test -f "$GROWTH_DIR/$f"; then sudo docker cp "$CONTAINER:$GROWTH_DIR/$f" "$STAGE/_preflight_data/$f"
  else echo "STOP: no $f in $NEW and none in the container yet - copy it first (step 1)"; sudo rm -rf "$STAGE"; exit 1; fi
done

echo "== adding Device Sales into the staged copy of the live files (their other changes are kept)"
python3 - "$STAGE" <<'PYEOF' || { echo "The live dashboard was NOT changed."; sudo rm -rf "$STAGE"; exit 1; }
import os, sys
stage = sys.argv[1]
def rd(p): return open(os.path.join(stage, p), encoding="utf-8").read()
def wr(p, s): open(os.path.join(stage, p), "w", encoding="utf-8").write(s)

a = rd("app.py")
if "from growth_platform import growth_bp" not in a:
    for old, new in (
        ("from campaign_public import public_bp\n",
         "from campaign_public import public_bp\nfrom growth_platform import growth_bp  # the CSP Growth Platform (device sales drip, order page, orders) at /growth\n"),
        ("    app.register_blueprint(public_bp)\n", "    app.register_blueprint(public_bp)\n    app.register_blueprint(growth_bp)\n"),
    ):
        if old not in a:
            print("STOP: app.py did not contain:", old.strip()); sys.exit(1)
        a = a.replace(old, new, 1)
    worker = ('\n# Opt-in background loops of the merged Growth Platform (order timers, daily Calling Sheet refresh, scheduled drip\n'
              '# batches - which only compose until GROWTH_EMAIL_MODE=live). Off by default.\n'
              'if os.environ.get("CSP_MONITOR_GROWTH_WORKER") == "1":\n'
              '    from growth_platform import start_background as _start_growth_background\n'
              '    _start_growth_background()\n')
    main = '\nif __name__ == "__main__":'
    a = a.replace(main, worker + main, 1) if main in a else a.rstrip("\n") + "\n" + worker
    wr("app.py", a)
print("  app.py: blueprint + background loops added")

b = rd("templates/base.html")
if "'growth.index'" not in b:
    nav = "  ('ui.email_campaigns', 'Email Campaigns', 'mail', ep.startswith('ui.email_')),\n"
    icon = "{%- elif name == 'enroll' %}"
    if nav in b and icon in b:
        b = b.replace(nav, nav + "  ('growth.index', 'Device Sales', 'cart', false),\n", 1)
        b = b.replace(icon, "{%- elif name == 'cart' %}<circle cx=\"9\" cy=\"20\" r=\"1.5\"/><circle cx=\"18\" cy=\"20\" r=\"1.5\"/>"
                            "<path d=\"M2 3h3l2.7 12.4a2 2 0 0 0 2 1.6h7.6a2 2 0 0 0 2-1.5L21 8H6\"/>\n" + icon, 1)
        wr("templates/base.html", b)
        print("  base.html: 'Device Sales' added to the sidebar")
    else:
        print("  WARNING: sidebar layout not recognised - no sidebar link added (the page still works at /growth/)")

if os.path.exists(os.path.join(stage, "hindi.py")):
    h = rd("hindi.py")
    anchor = '"Email Campaigns": "ईमेल कैंपेन",'
    if '"Device Sales"' not in h and anchor in h:
        wr("hindi.py", h.replace(anchor, anchor + ' "Device Sales": "डिवाइस बिक्री",', 1))
        print("  hindi.py: Hindi label added")

r = rd("requirements.txt")
for pkg in ("pandas==3.0.2", "openpyxl==3.1.5"):
    if pkg.split("==")[0] not in r:
        r = r.rstrip("\n") + "\n" + pkg + "\n"
wr("requirements.txt", r)
d = rd(".dockerignore") if os.path.exists(os.path.join(stage, ".dockerignore")) else ""
if "growth_platform/data" not in d:
    wr(".dockerignore", d.rstrip("\n") + ("\n" if d else "") + "growth_platform/data/\n_preflight_data/\n")
print("PATCHES OK")
PYEOF

echo "== rendering every page from the staged copy in a throwaway container"
cat > "$STAGE/_preflight.py" <<'PYEOF'
import sys
import app as A
bad = []
c = A.app.test_client()
for p, want in (("/login", 200), ("/growth/", 302), ("/growth/api/overview", 401), ("/growth/order.html", 200),
                ("/growth/api/order-page/config", 200), ("/growth/pixel/x.png", 200)):
    r = c.get(p); print("  [public] %-32s %s" % (p, r.status_code))
    if r.status_code != want: bad.append("%s gave %s, expected %s" % (p, r.status_code, want))
with c.session_transaction() as s:
    s["admin_in"] = True; s["admin_login"] = "admin"
paths = ["/"]
for rule in A.app.url_map.iter_rules():
    if rule.endpoint.startswith("ui.") and "GET" in rule.methods and not rule.arguments and rule.endpoint != "ui.logout":
        paths.append(rule.rule)
for p in sorted(set(paths)) + ["/growth/", "/growth/api/overview", "/growth/api/orders", "/growth/api/messages",
                               "/growth/api/metrics", "/growth/api/email/status"]:
    r = c.get(p); print("  [admin]  %-32s %s" % (p, r.status_code))
    if r.status_code >= 400: bad.append("%s gave %s" % (p, r.status_code))
ov = c.get("/growth/api/overview").get_json() or {}
print("  Growth data: %s CSPs, %s orders" % (ov.get("source", {}).get("cspCount"), (ov.get("orders") or {}).get("total")))
if not ov.get("source", {}).get("cspCount"): bad.append("Growth data did not load")
if "Device Sales" not in c.get("/").get_data(as_text=True): print("  WARNING: no 'Device Sales' link in the sidebar")
print("PREFLIGHT FAILED: " + "; ".join(bad) if bad else "PREFLIGHT OK")
sys.exit(1 if bad else 0)
PYEOF
if ! sudo docker run --rm -v "$STAGE":/app -w /app \
     -e CSP_MONITOR_DB_PATH=/tmp/preflight.db -e CSP_MONITOR_LOG_PATH=/tmp/preflight.log \
     -e CSP_MONITOR_SECRET_KEY=preflight-only -e CSP_MONITOR_RELEASE_DIR=/tmp/releases -e PYTHONDONTWRITEBYTECODE=1 \
     -e GROWTH_DATA_DIR=/app/_preflight_data -e GROWTH_EMAIL_MODE=dry \
     "$IMAGE" python /app/_preflight.py; then
  echo "STOP: a page failed (error above). The live dashboard was NOT changed. Staged copy kept in $STAGE"
  exit 1
fi
sudo rm -rf "$STAGE/_preflight.py" "$STAGE/_preflight_data"

echo "== backing up the live files"
B=~/backup-growth-$(date +%Y%m%d-%H%M%S)
mkdir -p "$B"
cp "$APP_DIR"/app.py "$APP_DIR"/requirements.txt "$APP_DIR"/templates/base.html "$B"/
[ -f "$APP_DIR"/hindi.py ] && cp "$APP_DIR"/hindi.py "$B"/
[ -f "$APP_DIR"/.dockerignore ] && cp "$APP_DIR"/.dockerignore "$B"/dockerignore
sudo cp "$ENV_FILE" "$B"/dotenv && sudo chown "$(id -u)" "$B"/dotenv
echo "   backup in $B"

echo "== copying the Growth data into the container's data volume"
sudo docker exec "$CONTAINER" mkdir -p "$GROWTH_DIR"
if sudo docker exec "$CONTAINER" test -f "$GROWTH_DIR/state.json"; then
  echo "   $GROWTH_DIR/state.json already exists - NOT overwritten (it may hold newer orders)"
else
  sudo docker cp "$NEW"/state.json "$CONTAINER:$GROWTH_DIR/state.json"
  sudo docker cp "$NEW"/csps.json "$CONTAINER:$GROWTH_DIR/csps.json"
  echo "   copied state.json and csps.json"
fi

echo "== adding the Device Sales settings to $ENV_FILE (existing values are kept)"
# sudo: the .env file is often root-owned (it holds the dashboard's secrets)
add_env() { sudo grep -q "^$1=" "$ENV_FILE" || { echo "$1=$2" | sudo tee -a "$ENV_FILE" >/dev/null; echo "   + $1"; }; }
[ -n "$(sudo tail -c1 "$ENV_FILE")" ] && echo | sudo tee -a "$ENV_FILE" >/dev/null
add_env GROWTH_EMAIL_MODE dry
add_env GROWTH_EMAIL_MAX_PER_RUN 5
add_env GROWTH_DATA_DIR "$GROWTH_DIR"
add_env GROWTH_PUBLIC_BASE_URL "$PUBLIC_URL"
add_env ORDER_PAGE_LIVE true
add_env CSP_MONITOR_GROWTH_WORKER 1
if [ -f "$NEW/extra.env" ]; then
  while IFS='=' read -r k v; do [ -n "$k" ] && [ -n "$v" ] && add_env "$k" "$v"; done < <(grep -E '^[A-Z_]+=.' "$NEW/extra.env")
fi

echo "== switching the live source to the tested files"
cp "$STAGE"/app.py "$STAGE"/requirements.txt "$STAGE"/.dockerignore "$APP_DIR"/
cp "$STAGE"/templates/base.html "$APP_DIR"/templates/
[ -f "$STAGE"/hindi.py ] && cp "$STAGE"/hindi.py "$APP_DIR"/
rm -rf "$APP_DIR"/growth_platform && cp -a "$STAGE"/growth_platform "$APP_DIR"/
rm -rf "$STAGE" 2>/dev/null || sudo rm -rf "$STAGE"     # the test container may leave root-owned files behind

echo "== rebuilding and restarting $CONTAINER (installs pandas/openpyxl - takes a few minutes)"
cd "$COMPOSE_DIR" && sudo docker compose up -d --build
for i in $(seq 1 30); do
  code=$(curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:$PORT/login || true)
  [ "$code" = "200" ] && break; sleep 2
done
order=$(curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:$PORT/growth/order.html || true)
cfg=$(curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:$PORT/growth/api/order-page/config || true)
data=$(sudo docker exec "$CONTAINER" printenv GROWTH_DATA_DIR 2>/dev/null || true)
echo "   /login -> $code   /growth/order.html -> $order   /growth/api/order-page/config -> $cfg   GROWTH_DATA_DIR=$data"
if [ "$code" = "200" ] && [ "$order" = "200" ] && [ "$cfg" = "200" ] && [ "$data" = "$GROWTH_DIR" ]; then
  echo "DONE: Device Sales is live at $PUBLIC_URL/ (drip emails stay DRY until GROWTH_EMAIL_MODE=live)."
  echo "      Open the dashboard, press Ctrl+F5 once, and click Device Sales in the sidebar."
elif [ "$data" != "$GROWTH_DIR" ]; then
  echo "WARNING: the container did not pick up the new settings from $ENV_FILE (GROWTH_DATA_DIR='$data')."
  echo "         Paste this output to Claude. Undo with:  bash ~/growth/deploy_growth.sh --undo"
else
  echo "WARNING: the dashboard did not answer as expected. Undo with:  bash ~/growth/deploy_growth.sh --undo  (or paste the output to Claude)"
fi
