"""Admin UI — login, Fleet (all CSPs), CSP detail, and API key management."""
import secrets
from datetime import datetime, timezone, timedelta
from functools import wraps

from flask import Blueprint, flash, redirect, render_template, request, session, url_for

from db import get_connection

ui_bp = Blueprint("ui", __name__)
ONLINE_WINDOW_MIN = 15


def _now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _is_online(last_seen: str) -> bool:
    if not last_seen:
        return False
    try:
        dt = datetime.fromisoformat(last_seen)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt) <= timedelta(minutes=ONLINE_WINDOW_MIN)
    except ValueError:
        return False


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
        from auth import verify_password
        lid = request.form.get("login_id", "").strip()
        pw = request.form.get("password", "").strip()
        with get_connection() as conn:
            u = conn.execute("SELECT * FROM admin_users WHERE login_id=?", (lid,)).fetchone()
        if u and verify_password(pw, u["password"]):
            session.clear()
            session["admin_in"] = True
            session["admin_login"] = lid
            return redirect(url_for("ui.fleet"))
        flash("Invalid credentials")
    return render_template("login.html")


@ui_bp.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("ui.login"))


@ui_bp.route("/")
@login_required
def fleet():
    with get_connection() as conn:
        rows = conn.execute("SELECT * FROM csps ORDER BY name, csp_id").fetchall()
        labels = {r["csp_id"]: r["name"] for r in conn.execute(
            "SELECT csp_id, name FROM api_keys").fetchall()}
    csps = []
    for r in rows:
        d = dict(r)
        d["online"] = _is_online(r["last_seen"])
        d["name"] = (labels.get(d["csp_id"]) or "").strip() or d.get("name")
        csps.append(d)
    online = sum(1 for c in csps if c["online"])
    printer_problems = sum(1 for c in csps if c["printer_configured"] and not c["printer_ok"])
    microatm_problems = sum(1 for c in csps if c["microatm_configured"] and not c["microatm_ok"])
    return render_template("fleet.html", csps=csps, total=len(csps), online=online,
                          offline=len(csps) - online, window=ONLINE_WINDOW_MIN,
                          printer_problems=printer_problems, microatm_problems=microatm_problems)


@ui_bp.route("/csp/<csp_id>")
@login_required
def csp_detail(csp_id):
    with get_connection() as conn:
        c = conn.execute("SELECT * FROM csps WHERE csp_id=?", (csp_id,)).fetchone()
        label = conn.execute("SELECT name FROM api_keys WHERE csp_id=?", (csp_id,)).fetchone()
    if not c:
        return "CSP not found", 404
    d = dict(c)
    d["online"] = _is_online(c["last_seen"])
    if label and (label["name"] or "").strip():
        d["name"] = label["name"].strip()
    return render_template("csp_detail.html", c=d)


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
                    conn.execute("UPDATE api_keys SET active=? WHERE csp_id=?",
                                (0 if row["active"] else 1, csp_id))
                    conn.commit()
            return redirect(url_for("ui.api_keys"))

        if action == "delete":
            with get_connection() as conn:
                conn.execute("DELETE FROM api_keys WHERE csp_id=?", (csp_id,))
                conn.execute("DELETE FROM csps WHERE csp_id=?", (csp_id,))
                conn.commit()
            flash(f"Removed {csp_id}.")
            return redirect(url_for("ui.api_keys"))

        # issue (new CSP) or rotate (existing CSP gets a fresh key). A blank
        # name on rotate keeps whatever was already saved instead of
        # clobbering it — only the key itself is guaranteed to change.
        name = request.form.get("name", "").strip()
        key = secrets.token_urlsafe(32)
        with get_connection() as conn:
            conn.execute(
                """INSERT INTO api_keys (csp_id, api_key, name, active, created_at)
                   VALUES (?,?,?,1,?)
                   ON CONFLICT(csp_id) DO UPDATE SET
                       api_key=excluded.api_key,
                       name=COALESCE(NULLIF(excluded.name, ''), api_keys.name),
                       active=1, created_at=excluded.created_at""",
                (csp_id, key, name, _now_iso()))
            conn.commit()
        new_key = {"csp_id": csp_id, "api_key": key}
        flash(f"Key issued for {csp_id} — copy it now, it will not be shown again.")

    with get_connection() as conn:
        keys = conn.execute("SELECT * FROM api_keys ORDER BY csp_id").fetchall()
    return render_template("api_keys.html", keys=keys, new_key=new_key)
