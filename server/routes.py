"""Admin UI — login, Fleet (all CSPs), CSP detail, and API key management."""
import csv
import io
import logging
from datetime import datetime, timezone, timedelta
from functools import wraps
from urllib.parse import urlencode

from flask import Blueprint, Response, flash, redirect, render_template, request, session, url_for

from db import get_connection
from device_state import (ONLINE_WINDOW_MIN, NEVER_REPORTED, NOT_REPORTED, OFFLINE, ONLINE, STALE,
                          UNKNOWN, derive_csp_state, derive_device_state)
from events import record_audit
from security import generate_api_key, hash_api_key, key_suffix, login_limiter

log = logging.getLogger("csp_monitor.routes")

ui_bp = Blueprint("ui", __name__)
FLEET_PAGE_SIZE = 50


def _now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _is_online(last_seen: str) -> bool:
    return derive_csp_state(last_seen) == ONLINE


def login_required(fn):
    @wraps(fn)
    def wrap(*a, **k):
        if not session.get("admin_in"):
            return redirect(url_for("ui.login"))
        return fn(*a, **k)
    return wrap


@ui_bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        # Per-IP sliding window - slows down password guessing without
        # needing an external service; fine for this single-process deploy.
        if not login_limiter.allow(request.remote_addr or "unknown"):
            log.warning("login rate limit hit from %s", request.remote_addr)
            flash("Too many login attempts - please wait a minute and try again.")
            return render_template("login.html"), 429
        from auth import verify_password
        lid = request.form.get("login_id", "").strip()
        pw = request.form.get("password", "").strip()
        with get_connection() as conn:
            u = conn.execute("SELECT * FROM admin_users WHERE login_id=?", (lid,)).fetchone()
        if u and verify_password(pw, u["password"]):
            session.clear()
            session["admin_in"] = True
            session["admin_login"] = lid
            with get_connection() as conn:
                record_audit(conn, lid, "login")
                conn.commit()
            log.info("admin login: %s", lid)
            return redirect(url_for("ui.fleet"))
        log.warning("failed login attempt for login_id=%r from %s", lid, request.remote_addr)
        flash("Invalid credentials")
    return render_template("login.html")


@ui_bp.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("ui.login"))


def _load_fleet(conn):
    """Every CSP with its derived states attached - shared by the Fleet page
    and the CSV export so they can never drift apart."""
    rows = conn.execute("SELECT * FROM csps ORDER BY name, csp_id").fetchall()
    labels = {r["csp_id"]: r["name"] for r in conn.execute(
        "SELECT csp_id, name FROM api_keys").fetchall()}
    csps = []
    for r in rows:
        d = dict(r)
        d["state"] = derive_csp_state(r["last_seen"])
        d["online"] = d["state"] == ONLINE
        d["printer_state"] = derive_device_state(
            r["printer_configured"], r["printer_present"], r["printer_ok"], r["printer_status"])
        d["microatm_state"] = derive_device_state(
            r["microatm_configured"], r["microatm_present"], r["microatm_ok"], r["microatm_status"])
        # An offline CSP's device badges are just the LAST value it reported
        # before going dark - not a live reading. Showing them with the same
        # confident OK/Problem styling as a fresh report is misleading, so
        # they're flagged "unknown" and every consumer (badges, KPI counts,
        # problems filter, CSV) treats them that way.
        d["devices_unknown"] = d["state"] == OFFLINE
        # Agents older than schema_version 3 never report biometric/GPS at
        # all - that is "not reported", NOT "no device", so it must not read
        # as "Not set up" (which would claim the CSP has no scanner/dongle).
        d["new_devices_reported"] = (r["schema_version"] or 0) >= 3
        for dev in ("biometric", "gps"):
            d[dev + "_state"] = derive_device_state(
                r[dev + "_configured"], r[dev + "_present"], r[dev + "_ok"],
                r[dev + "_status"]) if d["new_devices_reported"] else NOT_REPORTED
        if d["devices_unknown"]:
            d["printer_state"] = UNKNOWN
            d["microatm_state"] = UNKNOWN
            for dev in ("biometric", "gps"):
                if d["new_devices_reported"]:
                    d[dev + "_state"] = UNKNOWN
        d["name"] = (labels.get(d["csp_id"]) or "").strip() or d.get("name")
        csps.append(d)
    return csps


_DEVICES = ("printer", "microatm", "biometric", "gps")


def _device_problem(c, dev):
    """A LIVE problem: configured, reporting not-ok, from a CSP that is not
    offline and (for the newer devices) from an agent that reports them."""
    if c["devices_unknown"]:
        return False
    if dev in ("biometric", "gps") and not c["new_devices_reported"]:
        return False
    return bool(c[dev + "_configured"]) and not c[dev + "_ok"]


_SORT_KEYS = {
    "name": lambda c: (c["name"] or "").lower(),
    "status": lambda c: c["state"],
    "last_seen": lambda c: c["last_seen"] or "",
}


def _filter_and_sort(csps, q, status_filter, problems_only, sort, direction):
    if q:
        csps = [c for c in csps if q in (c["name"] or "").lower() or q in (c["csp_id"] or "").lower()]
    if status_filter:
        csps = [c for c in csps if c["state"] == status_filter]
    if problems_only:
        csps = [c for c in csps if any(_device_problem(c, dev) for dev in _DEVICES)]
    key_fn = _SORT_KEYS.get(sort, _SORT_KEYS["name"])
    csps = sorted(csps, key=key_fn, reverse=(direction == "desc"))
    return csps


@ui_bp.route("/")
@login_required
def fleet():
    q = (request.args.get("q") or "").strip().lower()
    status_filter = request.args.get("status") or ""
    problems_only = request.args.get("problems_only") == "1"
    sort = request.args.get("sort") or "name"
    direction = request.args.get("dir") or "asc"
    try:
        page = max(1, int(request.args.get("page", 1)))
    except ValueError:
        page = 1
    refresh = request.args.get("refresh") or "0"
    if refresh not in ("0", "30", "60"):
        refresh = "0"

    with get_connection() as conn:
        csps = _load_fleet(conn)

    total_before_filter = len(csps)
    online = sum(1 for c in csps if c["state"] == ONLINE)
    stale = sum(1 for c in csps if c["state"] == STALE)
    offline = sum(1 for c in csps if c["state"] == OFFLINE)
    never_reported = sum(1 for c in csps if c["state"] == NEVER_REPORTED)
    printer_problems = sum(1 for c in csps if _device_problem(c, "printer"))
    microatm_problems = sum(1 for c in csps if _device_problem(c, "microatm"))
    biometric_problems = sum(1 for c in csps if _device_problem(c, "biometric"))
    gps_problems = sum(1 for c in csps if _device_problem(c, "gps"))

    csps = _filter_and_sort(csps, q, status_filter, problems_only, sort, direction)

    matched = len(csps)
    total_pages = max(1, (matched + FLEET_PAGE_SIZE - 1) // FLEET_PAGE_SIZE)
    page = min(page, total_pages)
    page_csps = csps[(page - 1) * FLEET_PAGE_SIZE: page * FLEET_PAGE_SIZE]

    def qs(**overrides):
        args = {"q": q or None, "status": status_filter or None,
                "problems_only": "1" if problems_only else None,
                "sort": sort, "dir": direction, "page": page, "refresh": refresh if refresh != "0" else None}
        args.update(overrides)
        args = {k: v for k, v in args.items() if v not in (None, "")}
        return ("?" + urlencode(args)) if args else ""

    def sort_url(col):
        new_dir = "desc" if sort == col and direction == "asc" else "asc"
        return qs(sort=col, dir=new_dir, page=1)

    sort_urls = {col: sort_url(col) for col in _SORT_KEYS}
    csv_url = url_for("ui.fleet_csv") + qs(page=None, refresh=None)

    return render_template("fleet.html", csps=page_csps, total=total_before_filter, matched=matched,
                          online=online, stale=stale, offline=offline, never_reported=never_reported,
                          window=ONLINE_WINDOW_MIN, printer_problems=printer_problems,
                          microatm_problems=microatm_problems,
                          biometric_problems=biometric_problems, gps_problems=gps_problems, q=q, status_filter=status_filter,
                          problems_only=problems_only, sort=sort, direction=direction,
                          page=page, total_pages=total_pages, refresh=refresh,
                          qs=qs, sort_urls=sort_urls, csv_url=csv_url,
                          updated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))


@ui_bp.route("/fleet.csv")
@login_required
def fleet_csv():
    """CSV export of the CURRENT FILTER (not the current page) - an operator
    exporting "problems only" should get every matching row, not just
    whatever fit on screen."""
    q = (request.args.get("q") or "").strip().lower()
    status_filter = request.args.get("status") or ""
    problems_only = request.args.get("problems_only") == "1"
    sort = request.args.get("sort") or "name"
    direction = request.args.get("dir") or "asc"

    with get_connection() as conn:
        csps = _load_fleet(conn)
    csps = _filter_and_sort(csps, q, status_filter, problems_only, sort, direction)

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["csp_id", "name", "status", "printer_state", "microatm_state",
                     "biometric_state", "gps_state", "last_seen_utc"])
    for c in csps:
        writer.writerow([c["csp_id"], c["name"] or "", c["state"],
                         c["printer_state"], c["microatm_state"],
                         c["biometric_state"], c["gps_state"], c["last_seen"] or ""])
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=csp_fleet.csv"})


@ui_bp.route("/csp/<csp_id>")
@login_required
def csp_detail(csp_id):
    with get_connection() as conn:
        c = conn.execute("SELECT * FROM csps WHERE csp_id=?", (csp_id,)).fetchone()
        label = conn.execute("SELECT name FROM api_keys WHERE csp_id=?", (csp_id,)).fetchone()
        events = conn.execute(
            "SELECT * FROM events WHERE csp_id=? ORDER BY occurred_at DESC LIMIT 25", (csp_id,)).fetchall()
        open_incidents = conn.execute(
            "SELECT * FROM incidents WHERE csp_id=? AND status='OPEN' ORDER BY started_at DESC", (csp_id,)).fetchall()
        past_incidents = conn.execute(
            "SELECT * FROM incidents WHERE csp_id=? AND status='RESOLVED' ORDER BY resolved_at DESC LIMIT 10",
            (csp_id,)).fetchall()
    if not c:
        return render_template("not_found.html", csp_id=csp_id), 404
    d = dict(c)
    d["state"] = derive_csp_state(c["last_seen"])
    d["online"] = d["state"] == ONLINE
    d["printer_state"] = derive_device_state(
        c["printer_configured"], c["printer_present"], c["printer_ok"], c["printer_status"])
    d["microatm_state"] = derive_device_state(
        c["microatm_configured"], c["microatm_present"], c["microatm_ok"], c["microatm_status"])
    d["devices_unknown"] = d["state"] == OFFLINE
    d["new_devices_reported"] = (c["schema_version"] or 0) >= 3
    if label and (label["name"] or "").strip():
        d["name"] = label["name"].strip()
    return render_template("csp_detail.html", c=d, events=events,
                          open_incidents=open_incidents, past_incidents=past_incidents)


@ui_bp.route("/api-keys", methods=["GET", "POST"])
@login_required
def api_keys():
    new_key = None
    if request.method == "POST":
        action = request.form.get("action")
        csp_id = request.form.get("csp_id", "").strip()
        if not csp_id:
            flash("CSP code is required.")
            return redirect(url_for("ui.api_keys"))

        if action == "toggle":
            with get_connection() as conn:
                row = conn.execute("SELECT active FROM api_keys WHERE csp_id=?", (csp_id,)).fetchone()
                if row:
                    new_active = 0 if row["active"] else 1
                    conn.execute("UPDATE api_keys SET active=? WHERE csp_id=?", (new_active, csp_id))
                    record_audit(conn, session.get("admin_login"),
                                "api_key_reactivate" if new_active else "api_key_revoke", csp_id)
                    conn.commit()
            return redirect(url_for("ui.api_keys"))

        if action == "delete":
            with get_connection() as conn:
                conn.execute("DELETE FROM api_keys WHERE csp_id=?", (csp_id,))
                conn.execute("DELETE FROM csps WHERE csp_id=?", (csp_id,))
                record_audit(conn, session.get("admin_login"), "api_key_delete", csp_id)
                conn.commit()
            flash(f"Removed {csp_id}.")
            return redirect(url_for("ui.api_keys"))

        # issue (new CSP) or rotate (existing CSP gets a fresh key). A blank
        # name on rotate keeps whatever was already saved instead of
        # clobbering it — only the key itself is guaranteed to change.
        # The plaintext key is shown ONCE, right now, in the response - only
        # its hash and a display suffix are ever persisted (see security.py).
        name = request.form.get("name", "").strip()
        key = generate_api_key()
        with get_connection() as conn:
            already_existed = conn.execute(
                "SELECT 1 FROM api_keys WHERE csp_id=?", (csp_id,)).fetchone()
            conn.execute(
                """INSERT INTO api_keys (csp_id, api_key_hash, api_key_suffix, name, active, created_at)
                   VALUES (?,?,?,?,1,?)
                   ON CONFLICT(csp_id) DO UPDATE SET
                       api_key_hash=excluded.api_key_hash,
                       api_key_suffix=excluded.api_key_suffix,
                       name=COALESCE(NULLIF(excluded.name, ''), api_keys.name),
                       active=1, created_at=excluded.created_at""",
                (csp_id, hash_api_key(key), key_suffix(key), name, _now_iso()))
            record_audit(conn, session.get("admin_login"),
                        "api_key_rotate" if already_existed else "api_key_issue", csp_id)
            conn.commit()
        new_key = {"csp_id": csp_id, "api_key": key}
        flash(f"Key issued for {csp_id} — copy it now, it will not be shown again.")

    with get_connection() as conn:
        keys = conn.execute("SELECT * FROM api_keys ORDER BY csp_id").fetchall()
    return render_template("api_keys.html", keys=keys, new_key=new_key)


@ui_bp.route("/audit-log")
@login_required
def audit_log():
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM admin_audit_log ORDER BY occurred_at DESC LIMIT 200").fetchall()
    return render_template("audit_log.html", rows=rows)
