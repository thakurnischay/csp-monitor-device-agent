"""The CSP Growth Platform (device-adoption sales: 8-touch email drip, self-serve order page, order lifecycle), merged
into the Device Monitor as one blueprint served at /growth.

Ported from the Growth Platform's backend/run.py with its route bodies unchanged. What differs from that app:
  * Auth is the Device Monitor's own admin login (no separate DASHBOARD_PASSWORD). The order page, its API and the two
    links every campaign email carries (/pixel/, /go/) stay public, exactly as before.
  * POSTs from the dashboard need the console's CSRF token (X-CSRF-Token header), like every other admin action here.
  * Live sending is GROWTH_EMAIL_MODE (not EMAIL_MODE, which belongs to the festival Email Campaigns).
  * Not merged: WhatsApp (no routes, no scheduler) and the courier-tracking browser automation.
  * Background loops run only with CSP_MONITOR_GROWTH_WORKER=1 (see start_background), like the campaign worker.
Data stays in the Growth Platform's own JSON files, in GROWTH_DATA_DIR (see core/store.py).
"""
import base64
import os
import threading
import time
from datetime import datetime
from html import escape
from pathlib import Path
from typing import Optional

from flask import Blueprint, jsonify, redirect, request, send_from_directory, session, url_for

from security import get_csrf_token, verify_csrf

from .core import config, emailer, engine, forms, metrics, orders, orderpage, outreach, store

FRONTEND_DIR = str(Path(__file__).resolve().parent / "frontend")
PREFIX = "/growth"

growth_bp = Blueprint("growth", __name__, url_prefix=PREFIX)


# --------------------------------------------------------------------- auth
# What an unauthenticated CSP's browser must still be able to reach: the order page itself, its API routes, and the
# two links a campaign email carries (open-pixel, /go/<token>). Everything else needs the console login.
_PUBLIC_PATH_PREFIXES = ("/api/order-page/", "/pixel/", "/go/", "/assets/")
_PUBLIC_PATHS = ("/order.html", "/order.js", "/style.css", "/payment-qr")

# The dashboard and order page load Google Fonts and use inline onclick handlers, which the console's default
# Content-Security-Policy (script-src 'self') would block. Same protections otherwise: no framing, no foreign scripts.
_CSP = ("default-src 'self'; script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src 'self' https://fonts.gstatic.com; "
        "img-src 'self' data: https:; frame-ancestors 'none'")


def _is_public(sub_path: str) -> bool:
    return sub_path.startswith(_PUBLIC_PATH_PREFIXES) or sub_path in _PUBLIC_PATHS


@growth_bp.before_request
def _require_console_login():
    sub = request.path[len(PREFIX):] or "/"
    if _is_public(sub):
        return None
    if not session.get("admin_in"):
        if sub.startswith("/api/"):
            return jsonify({"error": "Login required"}), 401
        return redirect(url_for("ui.login"))
    if request.method == "POST" and not verify_csrf(request.headers.get("X-CSRF-Token", "")):
        return jsonify({"error": "Invalid or missing CSRF token - please reload the page and try again."}), 400
    return None


@growth_bp.after_request
def _growth_csp(resp):
    resp.headers["Content-Security-Policy"] = _CSP
    return resp


# ------------------------------------------------------------------ helpers
def _sweep(state: dict) -> None:
    """Runs orders.auto_advance and persists if it moved anything - the
    other half of "zero human intervention" alongside the background thread
    at the bottom of this file. Called on every read of order data, so a
    page load never shows a stale status even if the background thread's
    last tick was a while ago.

    Callers MUST hold store's lock across their own load_state() through
    this call (see store.transaction()) - _sweep() only does the
    conditional save, not the load, so it cannot enforce that itself. Every
    call site below wraps its load + _sweep() in store.transaction() for
    exactly this reason: otherwise the auto_advance mutation and the
    request handler's own change could interleave and one would be lost.
    """
    if orders.auto_advance(state):
        store.save_state(state)


def current_view():
    """Re-evaluates from the imported base plus the human reconciliations on file.

    Non-blocking: a page read must never queue behind a scheduled outreach
    batch holding _LOCK for minutes (see store.transaction's wait=False
    docstring). If the lock is busy, this just reads the current state.json
    unlocked instead of waiting - safe because save_state() writes it
    atomically - and skips this request's _sweep() call; the next request
    (or the background loop) runs it instead."""
    payload = store.load_csps()
    with store.transaction(wait=False) as state:
        if state is None:
            state = store.load_state()
        else:
            _sweep(state)
    result = engine.evaluate(payload["csps"], state["reconciliations"])
    return payload, state, result


def product_or_400(product_id: str):
    product = config.PRODUCTS.get(product_id)
    if product is None:
        return None, (jsonify({"error": f"Unknown product: {product_id}"}), 400)
    return product, None


# ------------------------------------------------------------------- static
@growth_bp.get("/")
def index():
    """The dashboard shell, with the console's CSRF token and a link back to the console written in."""
    with open(Path(FRONTEND_DIR) / "index.html", encoding="utf-8") as f:
        page = f.read()
    page = page.replace("<head>", f'<head>\n<meta name="csrf-token" content="{escape(get_csrf_token())}" />', 1)
    page = page.replace("__CONSOLE_URL__", escape(url_for("ui.fleet")))
    return page, 200, {"Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store"}


# ------------------------------------------------------------- open tracking
# The smallest valid PNG - a single transparent pixel, served as static bytes
# rather than generated per-request. What actually matters here is that this
# URL got *requested at all* - see emailer.open_pixel_tag and
# store.mark_email_opened. Registered ahead of the catch-all static route
# below so "pixel" as a literal first path segment can never be shadowed by it.
_TRACKING_PIXEL = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
    "+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


@growth_bp.get("/pixel/<token>.png")
def open_pixel(token):
    """
    Fires the instant an email client renders the tracking image in a sent
    campaign email. Best-effort by design: a real inbox will hit this with
    all kinds of replayed, stale, or malformed requests (proxies, prefetchers,
    someone just poking the URL), and none of that should ever surface as a
    broken image or a 500 - the caller is a CSP's email client, not a
    dashboard user who can be shown an error.

    Cache-Control: no-store matters more here than on a normal static asset -
    an intermediary caching this image would mean only the FIRST recipient
    it serves ever actually registers as opened.
    """
    try:
        with store.transaction() as state:
            entry = store.mark_email_opened(state, token)
            if entry is not None:
                if entry["openHits"] == 1:
                    store.audit(state, "EMAIL_OPENED", {"token": token})
                store.save_state(state)   # every hit, not just the first - openHits must not be lost
    except Exception:                              # noqa: BLE001 - never break the image response
        pass
    return _TRACKING_PIXEL, 200, {
        "Content-Type": "image/png",
        "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
        "Pragma": "no-cache",
    }


@growth_bp.get("/go/<token>")
def form_click(token):
    """Every campaign email's form link points here first now (see
    emailer.click_tracked_form_url / app/outreach.py), not straight at the
    Google Form - logs the click (store.mark_form_clicked), the "form
    opened" signal that used to be invisible, then redirects on immediately.
    Best-effort like /pixel/<token>.png: a bad or stale token still sends
    the CSP somewhere useful (the bare, un-prefilled form) rather than a
    broken link or a visible error - the caller here is a CSP clicking a
    link in their inbox, not a dashboard user who can be shown a 404.
    """
    try:
        with store.transaction() as state:
            record = store.mark_form_clicked(state, token)
            if record is not None:
                if record["clickHits"] == 1:
                    store.audit(state, "FORM_LINK_CLICKED", {"token": token})
                store.save_state(state)   # every hit, not just the first - clickHits must not be lost

        if record is not None:
            # Phase 6 of design/order-page-build-plan.html - gated on
            # config.ORDER_PAGE_LIVE so campaign links can be switched
            # between the Google Form and the order page without re-sending
            # anything.
            #
            # ABSOLUTE url, deliberately - not a root-relative "/order.html".
            # This app is served under a path prefix in the real deployment
            # (TRACKING_BASE_URL is .../csp-growth-platform, and the proxy
            # strips that before Flask sees the request). A root-relative
            # redirect would send the browser to /order.html at the host
            # root, dropping the prefix, and 404 for every CSP who clicked
            # a campaign email. orderpage.order_page_url() builds it from
            # the same ORDER_PAGE_BASE_URL the WhatsApp templates use, so
            # both channels can never drift apart.
            if config.ORDER_PAGE_LIVE:
                return redirect(orderpage.order_page_url(token))
            payload = store.load_csps()
            csp = next((c for c in payload["csps"] if c["cspCode"] == record["cspCode"]), None)
            if csp is not None:
                return redirect(forms.build_prefill_url(csp, record["productId"], token))
    except Exception as err:                          # noqa: BLE001 - never break the redirect
        print(f"[form-click] failed for token={token}: {err}", flush=True)
    return redirect(config.FORM_BASE_URL)


@growth_bp.get("/<path:filename>")
def static_files(filename):
    return send_from_directory(FRONTEND_DIR, filename)



# ----------------------------------------------------------------- overview
@growth_bp.get("/api/overview")
def overview():
    try:
        payload, state, result = current_view()
    except RuntimeError as err:
        return jsonify({"error": str(err)}), 500

    # `targeted` is capped for payload size, so the true size travels separately
    # as `targetedCount` - otherwise the dashboard would report the page size as
    # the size of the programme.
    queues = {
        pid: {**q, "targetedCount": len(q["targeted"]),
              "targeted": q["targeted"][:100], "deferred": len(q["deferred"])}
        for pid, q in result["queues"].items()
    }

    return jsonify({
        "source": {"file": payload["source"], "importedAt": payload["importedAt"],
                   "cspCount": payload["cspCount"]},
        "coverage": result["coverage"],
        "dataQuality": result["dataQuality"],
        "summary": result["summary"],
        "queues": queues,
        "outreachCount": len(state["outreach"]),
        "reconciledCount": len(state["reconciliations"]),
        "orders": orders.summary(state),
        "products": {pid: {"id": p.id, "name": p.name, "price": p.price}
                     for pid, p in config.PRODUCTS.items()},
        "formSetup": {"missingFields": forms.missing_fields(),
                     "responseSheetConfigured": bool(config.RESPONSE_SHEET_URL)},
    })


# ------------------------------------------------------ data reconciliation
# The workflow that unblocks quarantined records: an RM confirms by phone
# whether the device physically exists, and that answer overrides the sheet.
@growth_bp.get("/api/disputed")
def disputed():
    _, _, result = current_view()
    rows = []
    for c in result["assessed"]:
        matm_disputed = c["ownership"]["MATM"]["verdict"] == engine.GAP_DISPUTED
        printer_disputed = c["ownership"]["PRINTER"]["verdict"] == engine.GAP_DISPUTED
        if not (matm_disputed or printer_disputed):
            continue
        which = "MATM" if matm_disputed else "PRINTER"
        rows.append({
            "cspCode": c["cspCode"], "name": c["name"], "rm": c["rm"],
            "state": c["state"], "mobile": c["mobile"], "email": c["email"],
            "commission": c["commission"], "matmTxns": c["matmTxns"],
            "transactions": c["transactions"],
            "product": which,
            "evidence": c["ownership"][which]["evidence"],
        })
    rows.sort(key=lambda r: r["matmTxns"], reverse=True)
    return jsonify({"count": len(rows), "disputed": rows})


@growth_bp.get("/api/reconcile/plan")
def reconcile_plan():
    """What sending ownership-confirmation emails to the disputed list would
    do, without sending - same 'show the real picture before anyone clicks'
    idea as /api/outreach/<product>/plan."""
    rows = disputed().get_json()["disputed"]
    plan = outreach.plan_confirmation_run(store.load_state(), rows,
                                          request.args.get("limit", type=int))
    return jsonify({**plan, "mode": emailer.MODE})


@growth_bp.post("/api/reconcile/send")
def reconcile_send():
    """Sends the ownership-confirmation batch. Resolves nothing by itself -
    a CSP's reply is still acted on by hand via POST /api/reconcile, same as
    a phone call would be."""
    body = request.get_json(silent=True) or {}
    if emailer.MODE == "live" and body.get("confirmLive") is not True:
        return jsonify({"error": "GROWTH_EMAIL_MODE is live. Pass confirmLive: true to "
                                 "send to real CSPs."}), 400

    rows = disputed().get_json()["disputed"]
    try:
        with store.transaction() as state:
            outcome = outreach.run_confirmation_outreach(state, rows, body.get("limit"))
            if outcome["mode"] != "dry":
                store.audit(state, "CONFIRMATION_RUN", {
                    "sent": len(outcome["sent"]), "failed": len(outcome["failed"]),
                    "held": len(outcome["held"]),
                })
                store.save_state(state)
    except Exception as err:                      # noqa: BLE001
        return jsonify({"error": str(err)}), 500

    held_by_kind: dict = {}
    for h in outcome["held"]:
        held_by_kind[h["kind"]] = held_by_kind.get(h["kind"], 0) + 1

    samples = outcome["sent"] or outcome["composed"]
    return jsonify({
        "mode": outcome["mode"], "sent": len(outcome["sent"]),
        "composed": len(outcome["composed"]), "failed": len(outcome["failed"]),
        "held": len(outcome["held"]), "heldByKind": held_by_kind,
        "samples": samples[:5],
    })


@growth_bp.post("/api/reconcile")
def reconcile():
    body = request.get_json(silent=True) or {}
    csp_code, product_id = body.get("cspCode"), body.get("productId")
    owns_device, by = body.get("ownsDevice"), body.get("by")

    if not csp_code or not product_id or not isinstance(owns_device, bool) or not by:
        return jsonify({"error": "cspCode, productId, ownsDevice (boolean) and by "
                                 "are required"}), 400
    if product_id not in config.PRODUCTS:
        return jsonify({"error": f"Unknown product: {product_id}"}), 400

    with store.transaction() as state:
        state["reconciliations"][store.recon_key(csp_code, product_id)] = {
            "ownsDevice": owns_device, "by": by,
            "note": body.get("note") or "", "at": store.now_iso(),
        }
        store.audit(state, "RECONCILED", {"cspCode": csp_code, "productId": product_id,
                                          "ownsDevice": owns_device, "by": by})
        store.save_state(state)
    return jsonify({"ok": True, "cspCode": csp_code, "productId": product_id,
                    "ownsDevice": owns_device})


# -------------------------------------------------------------------- email
@growth_bp.get("/api/email/status")
def email_status():
    """Current send mode and SMTP reachability, so the UI can show the real state."""
    conn = (emailer.verify_connection() if emailer.SMTP_HOST
            else {"ok": False, "error": "SMTP not configured"})
    return jsonify({
        **emailer.config_summary(), "connection": conn,
        "outreachSchedule": {
            "enabled": config.OUTREACH_SCHEDULE_ENABLED,
            "sendAtHour": config.OUTREACH_SEND_AT_HOUR,
            "sheetRefreshAtHour": config.CALLING_SHEET_REFRESH_AT_HOUR,
            "batchIntervalHours": config.OUTREACH_BATCH_INTERVAL_HOURS,
            "lastBatchHour": config.OUTREACH_LAST_BATCH_HOUR,
            "batchSizePerProduct": emailer.MAX_PER_RUN,
            # Deprecated: the loop is batch-driven (sendAtHour + interval +
            # lastBatchHour), not a single once-a-day interval, but kept so
            # anything still reading this key doesn't break. Do not use it to
            # predict when the next batch fires.
            "intervalHours": config.OUTREACH_RUN_INTERVAL_HOURS,
        },
    })


@growth_bp.get("/api/email/preview/<product_id>")
@growth_bp.get("/api/email/preview/<product_id>/<csp_code>")
def email_preview(product_id, csp_code=None):
    """Renders one CSP's email without sending. The safe way to review copy."""
    product, err = product_or_400(product_id)
    if err:
        return err

    _, state, result = current_view()
    queue = result["queues"][product_id]
    pool = queue["targeted"] + queue["deferred"]
    target = (next((t for t in pool if t["cspCode"] == csp_code), None) if csp_code
              else (queue["targeted"][0] if queue["targeted"] else None))
    if target is None:
        return jsonify({"error": "No such CSP in this queue"}), 404

    # Previewing mints a real token (so the link genuinely works end to end)
    # but never persists it - state here is never saved back, same as a dry run.
    issued = forms.issue_and_build(state, target, product_id)
    # ?touch=1..8 previews a specific email in the 8-touch sequence; omitted,
    # it shows whichever one this CSP would actually receive next (see
    # outreach.next_touch) so "Preview email" never shows stale touch-1 copy
    # for someone already past it.
    touch = request.args.get("touch", type=int)
    if touch is None:
        touch = outreach.next_touch(state, target["cspCode"], product_id) or 1
    message = emailer.compose(target, product, form_url=issued["url"], touch=touch)
    if request.args.get("format") == "html":
        return message["html"], 200, {"Content-Type": "text/html; charset=utf-8"}
    return jsonify({"target": {"cspCode": target["cspCode"], "name": target["name"],
                               "email": target["email"]},
                    "formUrl": issued["url"], "missingFormFields": forms.missing_fields(),
                    "touch": touch,
                    **message})


@growth_bp.get("/api/email/preview/confirm")
@growth_bp.get("/api/email/preview/confirm/<csp_code>")
def email_preview_confirm(csp_code=None):
    """Renders the ownership-confirmation email for one disputed CSP, without
    sending. csp_code is optional - omit it to preview the first disputed row."""
    rows = disputed().get_json()["disputed"]
    row = (next((r for r in rows if r["cspCode"] == csp_code), None) if csp_code
          else (rows[0] if rows else None))
    if row is None:
        return jsonify({"error": "No such CSP in the disputed list"}), 404

    message = emailer.compose_ownership_confirmation(row, config.PRODUCTS[row["product"]])
    if request.args.get("format") == "html":
        return message["html"], 200, {"Content-Type": "text/html; charset=utf-8"}
    return jsonify({"target": {"cspCode": row["cspCode"], "name": row["name"],
                               "email": row["email"]}, **message})




# ----------------------------------------------------------------- outreach
@growth_bp.get("/api/metrics")
def metrics_view():
    """Everything the Overview tab charts, plus today's snapshot write.

    The snapshot happens here rather than on a timer because this endpoint is
    hit every time anyone opens the dashboard, which is a far more reliable
    trigger than a background thread on a machine that gets restarted - and
    store.record_snapshot is idempotent per day, so repeated loads update
    today's row instead of piling up duplicates. The write is best-effort: a
    failure to record history must never stop the dashboard from rendering
    the numbers it already has.
    """
    _, state, result = current_view()
    payload = metrics.build(state, result)
    try:
        # Reload fresh under the lock rather than reusing `state` above -
        # current_view()'s own transaction already ended, so re-entering one
        # here (instead of mutating the stale `state` object and saving it)
        # means this can't discard a write another request made in between.
        # snapshot_metrics reads off the slightly-older state/result, which
        # is fine (it's deriving today's headline numbers, not writing
        # anything from them) - only the append-and-save below needs the
        # fresh copy.
        with store.transaction() as fresh_state:
            created = store.record_snapshot(fresh_state, metrics.snapshot_metrics(state, result))
            store.save_state(fresh_state)
            if created:
                payload["history"] = metrics.snapshot_series(fresh_state)
    except Exception as err:                      # noqa: BLE001 - reporting beats crashing
        payload["snapshotError"] = str(err)
    return jsonify(payload)


@growth_bp.get("/api/outreach/<product_id>/plan")
def outreach_plan(product_id):
    """What WOULD happen on a send, without sending."""
    _, err = product_or_400(product_id)
    if err:
        return err
    _, state, result = current_view()
    plan = outreach.plan_run(state, result["queues"][product_id],
                             request.args.get("limit", type=int))
    # "batch" carries the actual target list (not just a count) so the
    # dashboard can mark, per CSP row, whether this run would reach them.
    return jsonify({**plan, "mode": emailer.MODE})


@growth_bp.post("/api/outreach/<product_id>")
def outreach_run(product_id):
    _, err = product_or_400(product_id)
    if err:
        return err

    body = request.get_json(silent=True) or {}
    _, state, result = current_view()
    queue = result["queues"][product_id]

    # Sending to real CSPs is not something to trip into. In live mode the
    # caller must say so explicitly, on top of having set EMAIL_MODE=live.
    if emailer.MODE == "live" and body.get("confirmLive") is not True:
        return jsonify({"error": "GROWTH_EMAIL_MODE is live. Pass confirmLive: true to "
                                 "send to real CSPs."}), 400

    try:
        # Fresh state under the lock, same reasoning as metrics_view above -
        # current_view()'s transaction already closed, so mutating that
        # stale `state` and saving it later could discard a concurrent
        # write. `queue` is just a list of target CSPs computed a moment
        # ago; run_outreach doesn't need it to come from this exact state.
        with store.transaction() as state:
            outcome = outreach.run_outreach(state, queue, body.get("limit"))
            # A dry run changes nothing, so it should not leave an audit entry either.
            if outcome["mode"] != "dry":
                store.audit(state, "OUTREACH_RUN", {
                    "productId": product_id, "mode": outcome["mode"],
                    "sent": len(outcome["sent"]), "failed": len(outcome["failed"]),
                    "held": len(outcome["held"]),
                })
                store.save_state(state)
    except Exception as err:                      # noqa: BLE001
        return jsonify({"error": str(err)}), 500

    held_by_kind: dict = {}
    for h in outcome["held"]:
        held_by_kind[h["kind"]] = held_by_kind.get(h["kind"], 0) + 1

    samples = outcome["sent"] or outcome["composed"]
    return jsonify({
        "productId": product_id, "mode": outcome["mode"],
        "queueSize": len(queue["targeted"]),
        "sent": len(outcome["sent"]), "composed": len(outcome["composed"]),
        "failed": len(outcome["failed"]), "held": len(outcome["held"]),
        "heldByKind": held_by_kind, "samples": samples[:3],
    })


@growth_bp.get("/api/outreach")
def outreach_history():
    state = store.load_state()
    recent = list(reversed(state["outreach"][-60:]))
    # Full HTML bodies are left out here - the activity feed lists dozens of
    # entries at once and each body can run a few KB. hasMessage tells the
    # UI whether "View message" has anything to open (older entries, from
    # before this was tracked, won't) without shipping the body itself. See
    # /api/outreach/<id>/message below for the one-entry endpoint that does.
    light = [{**{k: v for k, v in e.items() if k != "html"},
              "hasMessage": "html" in e or "text" in e}
             for e in recent]
    return jsonify({"count": len(state["outreach"]), "recent": light})


@growth_bp.get("/api/outreach/<int:entry_id>/message")
def outreach_message(entry_id):
    """The actual message one specific past send carried - not a fresh
    preview (which would reflect today's data if anything's changed
    since), the literal content this entry composed and sent at the time.
    Email entries carry "html"; WhatsApp entries carry "text" (plain, no
    HTML - WhatsApp doesn't have a body format to render as a page). See
    store.find_outreach_entry."""
    state = store.load_state()
    entry = store.find_outreach_entry(state, entry_id)
    if entry is None or ("html" not in entry and "text" not in entry):
        return jsonify({"error": "No stored message for this entry - either it predates "
                                 "this feature, or it has no body (e.g. a held/failed "
                                 "send)."}), 404

    if "html" in entry:
        if request.args.get("format") == "html":
            return entry["html"], 200, {"Content-Type": "text/html; charset=utf-8"}
        return jsonify({"id": entry["id"], "channel": "EMAIL",
                        "subject": entry.get("subject"), "html": entry["html"]})

    # WhatsApp - plain text only. format=html still returns something
    # renderable (so the same "View message" link works for both channels
    # without the frontend needing to branch on channel first), just
    # escaped and wrapped instead of raw markup.
    if request.args.get("format") == "html":
        safe = escape(entry["text"]).replace("\n", "<br>")
        page = (f'<div style="font-family:system-ui,sans-serif;white-space:pre-wrap;'
               f'max-width:480px;margin:24px auto;padding:16px;border:1px solid #ddd;'
               f'border-radius:8px;line-height:1.5">{safe}</div>')
        return page, 200, {"Content-Type": "text/html; charset=utf-8"}
    return jsonify({"id": entry["id"], "channel": "WHATSAPP", "text": entry["text"]})


# ------------------------------------------------------------------ messages
@growth_bp.get("/api/messages")
def messages_view():
    """Per-CSP send/delivery/open/click status across every channel - the
    Messages tab. Summary counts are computed over the full history
    regardless of search/channel/page, since those are the headline numbers;
    "messages" below them is what q/channel/page actually narrow. WhatsApp
    will show up here the moment something starts writing channel="WHATSAPP"
    entries - nothing about this endpoint is email-specific."""
    state = store.load_state()
    all_entries = state["outreach"]

    def channel_counts(entries):
        counts = {"total": len(entries), "email": 0, "whatsapp": 0}
        for e in entries:
            key = (e.get("channel") or "EMAIL").lower()
            if key in counts:
                counts[key] += 1
        return counts

    summary = {
        "sent": channel_counts(all_entries),
        # "Delivered" for email only ever means "our SMTP server accepted it" -
        # see design/message-tracking-mockup.html's note. Not the same claim
        # as WhatsApp's Delivered, which will come from a real BSP webhook.
        "delivered": channel_counts([e for e in all_entries if e.get("delivered")]),
        "opened": channel_counts([e for e in all_entries if e.get("openedAt")]),
        "failed": channel_counts([e for e in all_entries if e.get("delivered") is False]),
    }

    q = (request.args.get("q") or "").strip().lower()
    channel = (request.args.get("channel") or "").strip().upper()
    page = max(request.args.get("page", default=1, type=int) or 1, 1)
    per_page = min(max(request.args.get("perPage", default=20, type=int) or 20, 1), 100)

    # Most recent first - same convention /api/outreach's activity feed uses.
    filtered = list(reversed(all_entries))
    if q:
        filtered = [e for e in filtered
                   if q in e["cspCode"].lower() or q in (e.get("name") or "").lower()]
    if channel:
        filtered = [e for e in filtered if (e.get("channel") or "EMAIL") == channel]

    total = len(filtered)
    total_pages = max((total + per_page - 1) // per_page, 1)
    page = min(page, total_pages)
    page_entries = filtered[(page - 1) * per_page: page * per_page]

    def row(e):
        # clickedAt lives on the token record (store.mark_form_clicked), not
        # on the outreach entry itself - see store.py's token shape.
        token_record = state["tokens"].get(e.get("token")) if e.get("token") else None
        product = config.PRODUCTS.get(e.get("productId"))
        return {
            "id": e["id"], "cspCode": e["cspCode"], "name": e["name"],
            "productId": e.get("productId"),
            "productName": product.name if product else e.get("productId"),
            "channel": e.get("channel", "EMAIL"), "kind": e.get("kind", "CAMPAIGN"),
            "touch": e.get("touch"), "cycle": e.get("cycle"),
            # occasion/touchIndex are the WhatsApp SEASONAL_OFFER equivalent
            # of touch/cycle above - see outreach.run_offer_outreach's entry
            # shape. None on every other kind of entry (email, ownership
            # confirm), same as touch/cycle are None on those.
            "occasion": e.get("occasion"), "touchIndex": e.get("touchIndex"),
            "at": e.get("at"),
            "delivered": e.get("delivered"), "failReason": e.get("failReason"),
            "openedAt": e.get("openedAt"),
            "clickedAt": (token_record or {}).get("clickedAt"),
            "hasMessage": "html" in e or "text" in e,
        }

    return jsonify({
        "summary": summary,
        # Same function Overview's banner already calls - one source of truth
        # for "can opens/clicks even be measured right now", not a second
        # guess made from the row data alone.
        "tracking": metrics.tracking_health(),
        "total": total, "page": page, "perPage": per_page, "totalPages": total_pages,
        "messages": [row(e) for e in page_entries],
    })


# --------------------------------------------------------------------- misc
@growth_bp.get("/api/csp/<code>")
def csp_detail(code):
    _, _, result = current_view()
    csp = next((c for c in result["assessed"] if c["cspCode"] == code), None)
    if csp is None:
        return jsonify({"error": "Not found"}), 404
    return jsonify(csp)


@growth_bp.get("/api/csps")
def csp_directory():
    """
    The full base - active, dormant, and inactive alike - unlike the target
    queues (ACTIVE only, see engine.build_queue) or /api/disputed (GAP_DISPUTED
    only). For looking up any CSP regardless of whether the campaigns would
    ever reach them.

    Query params: q (matches cspCode or name, case-insensitive substring),
    activity (ACTIVE/DORMANT/INACTIVE, exact). Capped at 500 rows in one
    response - always search/filter rather than paginate at this base size.
    """
    _, _, result = current_view()
    rows = result["assessed"]

    q = (request.args.get("q") or "").strip().lower()
    if q:
        rows = [c for c in rows if q in c["cspCode"].lower() or q in (c["name"] or "").lower()]

    activity = (request.args.get("activity") or "").strip().upper()
    if activity:
        rows = [c for c in rows if c["activity"] == activity]

    total = len(rows)
    trimmed = rows[:500]
    return jsonify({
        "total": total,
        "shown": len(trimmed),
        "csps": [{
            "cspCode": c["cspCode"], "name": c["name"], "rm": c["rm"],
            "state": c["state"], "district": c["district"],
            "activity": c["activity"], "activityReason": c["activityReason"],
            "band": c["band"], "commission": c.get("commission"),
            "transactions": c["transactions"],
            "matmOwnership": c["ownership"]["MATM"]["verdict"],
            "printerOwnership": c["ownership"]["PRINTER"]["verdict"],
            "contactable": c["contactable"], "email": c.get("email"),
            "mobile": c.get("mobile"),
        } for c in trimmed],
    })


@growth_bp.get("/api/audit")
def audit_log():
    state = store.load_state()
    return jsonify({"recent": list(reversed(state["audit"][-80:]))})


# -------------------------------------------------------------- order page
# The one part of this app an unauthenticated CSP's browser talks to
# directly - see app/orderpage.py's module docstring for exactly what it
# does and does not expose. Not linked from campaign outreach yet (that's
# build-plan Phase 6, deliberately last) - reachable today only by calling
# these two endpoints directly.
def _client_ip() -> str:
    # X-Forwarded-For is client-suppliable and not verified here - fine for
    # a best-effort rate limit (see orderpage.rate_limited), not something to
    # trust for anything security-critical. Real IP-verification (trusting
    # only a known reverse proxy's header) is a deployment-time concern, not
    # something this function can decide.
    return (request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
            or request.remote_addr or "unknown")


# ---------------------------------------------------------------- payment QR
# Finance's static UPI QR lives in GROWTH_DATA_DIR as payment-qr.png (or .jpg/.jpeg/.webp), not in the code, so
# replacing the file swaps the QR without a rebuild. The order page offers "QR code se" ONLY with
# ORDER_PAGE_QR_ENABLED=true AND an image: the QR is not in use yet, so by default CSPs see bank transfer only.
# ORDER_PAGE_QR_IMAGE still overrides the image address.
_QR_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}


def _qr_file() -> Optional[Path]:
    for ext in _QR_TYPES:
        path = Path(store.DATA_DIR) / f"payment-qr{ext}"
        if path.is_file():
            return path
    return None


def _qr_settings() -> dict:
    flag = (os.environ.get("ORDER_PAGE_QR_ENABLED") or "").strip().lower() in ("true", "1", "yes", "on")
    path = _qr_file()
    image = config.ORDER_PAGE_QR_IMAGE or (f"payment-qr?v={int(path.stat().st_mtime)}" if path else None)
    enabled = flag and bool(image)
    return {"enabled": enabled, "image": image if enabled else None}


@growth_bp.get("/payment-qr")
def payment_qr():
    """Public (it is on the order page every CSP sees). 404 until finance's QR file is in GROWTH_DATA_DIR."""
    path = _qr_file()
    if path is None:
        return jsonify({"error": "No payment QR uploaded"}), 404
    return send_from_directory(str(path.parent), path.name, mimetype=_QR_TYPES[path.suffix],
                               max_age=300)


@growth_bp.get("/api/order-page/config")
def order_page_config():
    """Static, non-secret display config the order page needs and has no
    other way to get (it's a plain static file, not server-rendered) - bank
    details are meant to be shown to every CSP, same as they already are on
    the Google Form's Payment Details section, so this isn't a new exposure.
    No rate limit: nothing here is per-CSP or guessable-enumeration risk."""
    return jsonify({
        "bank": {
            "beneficiary": config.BANK_BENEFICIARY_NAME,
            "accountNumber": config.BANK_ACCOUNT_NUMBER,
            "ifsc": config.BANK_IFSC,
            "branch": config.BANK_BRANCH,
            "configured": config.ORDER_PAGE_BANK_DETAILS_SET,
        },
        "qr": _qr_settings(),
        "products": {pid: {"id": p.id, "name": p.name, "price": p.price}
                     for pid, p in config.PRODUCTS.items()},
        # Fixed list, not free text - see orderpage.delivery_states() for
        # why the pincode zone check depends on it.
        "deliveryStates": orderpage.delivery_states(),
    })


@growth_bp.get("/api/order-page/resolve-token")
def order_page_resolve_token():
    if orderpage.rate_limited(_client_ip()):
        return jsonify({"error": "Too many attempts - try again in a minute."}), 429

    token = (request.args.get("t") or "").strip()
    if not token:
        return jsonify({"found": False}), 400

    payload = store.load_csps()
    csps_by_code = {c["cspCode"]: c for c in payload["csps"]}
    state = store.load_state()
    result = orderpage.resolve_token(state, csps_by_code, token)
    return jsonify(result)


@growth_bp.get("/api/order-page/lookup")
def order_page_lookup():
    if orderpage.rate_limited(_client_ip()):
        return jsonify({"error": "Too many lookups - try again in a minute."}), 429

    query = (request.args.get("q") or "").strip()
    if not query:
        return jsonify({"error": "q is required"}), 400

    payload = store.load_csps()
    state = store.load_state()
    result = orderpage.lookup(payload["csps"], state["reconciliations"],
                              query, request.args.get("last4"))
    return jsonify(result)


@growth_bp.post("/api/order-page/submit")
def order_page_submit():
    if orderpage.rate_limited(_client_ip()):
        return jsonify({"error": "Too many attempts - try again in a minute."}), 429

    body = request.get_json(silent=True) or {}
    payload = store.load_csps()
    csps_by_code = {c["cspCode"]: c for c in payload["csps"]}

    order = None
    try:
        with store.transaction() as state:
            result = orderpage.submit(
                state, csps_by_code,
                csp_code=body.get("cspCode"), product_id=body.get("productId"),
                payment_method=body.get("paymentMethod"), utr=body.get("utr"),
                address_line=body.get("addressLine"), landmark=body.get("landmark"),
                city=body.get("city"), pincode=body.get("pincode"),
                delivery_state=body.get("deliveryState"),
            )
            store.save_state(state)
            order = store.get_order(state, result["orderId"])
    except (ValueError, KeyError) as err:
        return jsonify({"error": str(err)}), 400

    # Deliberately OUTSIDE the lock and after save_state above - this is a
    # network call (see orderpage.mirror_to_sheet), and the order is already
    # safely on disk regardless of whether the mirror succeeds.
    if order is not None:
        orderpage.mirror_to_sheet(order)

    return jsonify(result)


# ------------------------------------------------------------------ orders
# Order capture: every form submission becomes an order, which a human then
# walks by hand through finance -> payment verified -> dispatch -> delivery,
# or rejects at any point before dispatch. Device ownership flips only at
# delivery - see app/orders.py's module docstring for why.
@growth_bp.post("/api/form/responses/import")
def import_form_responses():
    """
    Body is EITHER `{"responses": [...]}` with rows already in this
    platform's internal field names (the shape a Google Apps Script
    "on form submit" trigger would POST), OR `{"sheetUrl": "..."}` /
    nothing at all, in which case it falls back to polling
    config.RESPONSE_SHEET_URL (or the sheetUrl given) as CSV.
    """
    body = request.get_json(silent=True) or {}
    payload = store.load_csps()
    csps_by_code = {c["cspCode"]: c for c in payload["csps"]}

    if body.get("responses"):
        rows = body["responses"]
        for i, row in enumerate(rows):
            row.setdefault("responseId",
                            f"manual|{row.get('cspCode')}|{row.get('token')}|"
                            f"{row.get('utr')}|{i}")
    else:
        sheet_url = body.get("sheetUrl") or config.RESPONSE_SHEET_URL
        if not sheet_url:
            return jsonify({"error": "No 'responses' in the request body and "
                                     "config.RESPONSE_SHEET_URL is not set"}), 400
        try:
            # Deliberately fetched BEFORE the lock below - this is a network
            # call (CSV download), and holding the state lock across it would
            # stall every other request touching state.json for as long as
            # the fetch takes, not just for the quick in-memory part.
            rows = orders.fetch_response_rows(sheet_url)
        except (ValueError, RuntimeError) as err:
            return jsonify({"error": str(err)}), 502

    with store.transaction() as state:
        outcome = orders.import_responses(rows, csps_by_code, state)
        if outcome["created"] or outcome["verified"]:
            store.audit(state, "FORM_RESPONSES_IMPORTED", {
                "received": len(rows), "created": len(outcome["created"]),
                "financeVerified": len(outcome["verified"]),
            })
            store.save_state(state)

    return jsonify({"received": len(rows), "created": len(outcome["created"]),
                    "financeVerified": outcome["verified"],
                    "results": outcome["created"]})


@growth_bp.get("/api/orders")
def list_orders():
    # See current_view()'s comment - a page read must not queue behind a
    # scheduled outreach batch holding the lock.
    with store.transaction(wait=False) as state:
        if state is None:
            state = store.load_state()
        else:
            _sweep(state)
    status = request.args.get("status")
    rows = state["orders"]
    if status:
        rows = [o for o in rows if o["status"] == status]
    enriched = [{**o, "eta": orders.eta_info(o)} for o in rows]
    return jsonify({"count": len(enriched), "orders": list(reversed(enriched))})


@growth_bp.get("/api/orders/<order_id>")
def order_detail(order_id):
    # See current_view()'s comment - a page read must not queue behind a
    # scheduled outreach batch holding the lock.
    with store.transaction(wait=False) as state:
        if state is None:
            state = store.load_state()
        else:
            _sweep(state)
    order = store.get_order(state, order_id)
    if order is None:
        return jsonify({"error": "Not found"}), 404
    return jsonify({**order, "eta": orders.eta_info(order)})


# Every one of these is a MANUAL OVERRIDE, not a required step - the normal
# path needs none of them (see app/orders.py's module docstring): payment
# auto-verifies on submission, dispatch/deliver auto-advance on a timer.
# reject is the one action with no automatic equivalent.
_ORDER_ACTIONS = {
    "verify-payment": orders.verify_payment,
    "dispatch": orders.dispatch,
    "deliver": orders.deliver,
    "reject": orders.reject,
}


@growth_bp.post("/api/orders/<order_id>/<action>")
def order_action(order_id, action):
    """Manual override endpoint - see _ORDER_ACTIONS. What's actually
    allowed next depends on the order's current status - see
    orders._TRANSITIONS; an out-of-order request 400s with the real options."""
    fn = _ORDER_ACTIONS.get(action)
    if fn is None:
        return jsonify({"error": f"Unknown action: {action}. "
                                 f"Use one of {sorted(_ORDER_ACTIONS)}."}), 400
    body = request.get_json(silent=True) or {}
    by = body.get("by")
    if not by:
        return jsonify({"error": "'by' (who performed this action) is required"}), 400

    # courier/trackingNumber only mean anything to "dispatch" (see
    # orders.dispatch) - the other three actions don't take these kwargs, so
    # they're only included when the action actually accepts them.
    extra = {}
    if action == "dispatch":
        if body.get("courier"):
            extra["courier"] = body["courier"]
        if body.get("trackingNumber"):
            extra["tracking_number"] = body["trackingNumber"]

    try:
        with store.transaction() as state:
            order = fn(state, order_id, by=by, note=body.get("note", ""), **extra)
            store.save_state(state)
    except (KeyError, ValueError) as err:
        return jsonify({"error": str(err)}), 400
    return jsonify(order)



# ------------------------------------------------------------ auto-advance
# Runs on a fixed interval so orders progress (and their emails fire) even if
# nobody ever opens the dashboard AND nobody ever clicks "Import responses" -
# "no human intervention" means neither the dashboard nor the response sheet
# can be something a person has to remember to go check. Two jobs each tick:
#   1. Poll the response sheet - picks up brand-new submissions AND any
#      "Credited" a finance person typed into the Finance Remark column
#      since the last poll (see orders.import_responses/_try_finance_verify).
#   2. orders.auto_advance - the DISPATCHED/DELIVERED timers.
# _sweep() in the request handlers above covers the case where someone IS
# looking, for freshness between ticks; this covers the case where nobody is.
_AUTO_ADVANCE_INTERVAL_SECONDS = 15 * 60   # 15 minutes


def _fetch_response_sheet_rows() -> Optional[list]:
    """The network half of what used to be _poll_response_sheet, split out
    so it runs OUTSIDE the state lock (see _auto_advance_loop below) - a
    slow or stalled sheet fetch must not block every other request from
    touching state.json for however long it takes. Returns None if no sheet
    is configured yet (see GOOGLE_FORM_SETUP.md) - a setup step still
    pending, not an error each tick should complain about."""
    if not config.RESPONSE_SHEET_URL:
        return None
    return orders.fetch_response_rows(config.RESPONSE_SHEET_URL)


def _apply_response_rows(state: dict, rows: Optional[list]) -> bool:
    """The in-memory half - mutates `state` from already-fetched rows. Must
    be called with the lock already held (see _auto_advance_loop)."""
    if not rows:
        return False
    payload = store.load_csps()
    csps_by_code = {c["cspCode"]: c for c in payload["csps"]}
    outcome = orders.import_responses(rows, csps_by_code, state)
    return bool(outcome["created"] or outcome["verified"])


def _calling_sheet_stale() -> bool:
    """True when the live sheet is due a re-fetch, on either of two rules:

      1. The daily deadline - it is past CALLING_SHEET_REFRESH_AT_HOUR local
         time today and the data on disk predates that hour. This is the rule
         that matters: it guarantees fresh data is in place before the morning
         outreach send, and unlike an interval it cannot drift later day by
         day until the send overtakes it.
      2. The age fallback - data older than CALLING_SHEET_REFRESH_HOURS is
         re-fetched whatever the clock says, so a server that was down all
         morning doesn't serve day-old data until tomorrow's deadline.

    Both are checked against csps.json's own "importedAt" rather than a
    separate counter, so restarting the server never triggers an early
    re-fetch just because the process is new.

    On timezones: importedAt is now written as aware UTC (see
    import_calling_sheet, store.now_iso). A NAIVE timestamp is a legacy stamp
    from the old importer, which wrote local wall-clock - so .astimezone()
    reading it as local is what makes those correct too. The previous version
    read naive stamps as UTC, which in IST made every import look 5.5 hours
    younger than it was and turned the daily refresh into a 29.5-hourly one.
    """
    imported_at = store.load_csps().get("importedAt")
    if not imported_at:
        return False   # no file yet - store.load_csps() would already have
                       # raised if data/csps.json were genuinely missing

    # astimezone() converts an aware stamp to local, and assumes local for a
    # naive one - correct for both cases above.
    then = datetime.fromisoformat(imported_at).astimezone()
    now = datetime.now().astimezone()

    deadline = now.replace(hour=config.CALLING_SHEET_REFRESH_AT_HOUR,
                           minute=0, second=0, microsecond=0)
    if now >= deadline and then < deadline:
        return True

    age_hours = (now - then).total_seconds() / 3600
    return age_hours >= config.CALLING_SHEET_REFRESH_HOURS


def _maybe_refresh_calling_sheet() -> None:
    """Re-fetches the live Calling Sheet with no one running the importer by
    hand - the CSP base (csps.json) was the one thing in this pipeline that
    stayed manual. Every dashboard/API read already calls store.load_csps()
    fresh (see current_view()), so a successful refresh here is picked up on
    the very next request with no other wiring needed."""
    if not _calling_sheet_stale():
        return
    from . import import_calling_sheet     # lazy: pandas is only needed when the sheet is due a refresh
    payload = import_calling_sheet.import_live()
    print(f"[calling-sheet] refreshed: {payload['cspCount']} CSPs "
          f"({len(payload['duplicateCodes'])} duplicates merged)", flush=True)


def _auto_advance_loop():
    while True:
        try:
            _maybe_refresh_calling_sheet()
        except Exception as err:                      # noqa: BLE001
            # Isolated from the order-timer tick below - a failed sheet fetch
            # (network blip, sharing setting changed) must not also block
            # orders from advancing this tick.
            print(f"[calling-sheet] refresh failed: {err}", flush=True)

        try:
            rows = _fetch_response_sheet_rows()
        except Exception as err:                      # noqa: BLE001
            print(f"[auto-advance] response-sheet fetch failed: {err}", flush=True)
            rows = None

        try:
            with store.transaction() as state:
                changed = _apply_response_rows(state, rows)
                if orders.auto_advance(state):
                    changed = True
                if changed:
                    store.save_state(state)
        except Exception as err:                      # noqa: BLE001
            # A background loop must never die from one bad tick - there's
            # no one watching its stdout to notice and restart it, so the
            # next tick just tries again.
            print(f"[auto-advance] tick failed: {err}")
        time.sleep(_AUTO_ADVANCE_INTERVAL_SECONDS)



# ---------------------------------------------------------------- outreach
# See config.OUTREACH_SCHEDULE_ENABLED for why this is off by default and
# gated independently of EMAIL_MODE.
#
# This loop TICKS every 15 minutes but SENDS in BATCHES across the day - the
# first at config.OUTREACH_SEND_AT_HOUR, then one more every
# config.OUTREACH_BATCH_INTERVAL_HOURS, each capped at emailer.MAX_PER_RUN per
# product, until config.OUTREACH_LAST_BATCH_HOUR. The frequent tick is only
# how a due batch gets noticed; it is not how often mail goes out. Same
# deadline shape as _calling_sheet_stale above, and deliberately not a fixed
# sleep: an interval anchored to process start drifts to whenever the server
# was last restarted, which is how a batch ends up preceding the morning
# refresh or landing in the middle of the night.
_OUTREACH_TICK_SECONDS = 15 * 60


def _outreach_due(state: dict) -> bool:
    """True when the next scheduled batch is owed and hasn't run yet.

    Tracked as a TIMESTAMP (state["lastScheduledOutreachAt"]), not a date -
    a date only answers "did today's send happen", which is the wrong
    question once a day can hold several batches. A batch is due once
    OUTREACH_BATCH_INTERVAL_HOURS have passed since the last one (or none has
    ever run), AND the clock is inside today's sending window
    [OUTREACH_SEND_AT_HOUR, OUTREACH_LAST_BATCH_HOUR) - so a big queue that
    hasn't finished by the cutoff hour picks back up at tomorrow's first
    batch rather than continuing into the night.

    Read from state, not a counter in this process, so restarting the server
    mid-afternoon does not re-trigger a batch that already went out an hour
    ago.
    """
    now = datetime.now().astimezone()
    if not (config.OUTREACH_SEND_AT_HOUR <= now.hour < config.OUTREACH_LAST_BATCH_HOUR):
        return False
    last_at = state.get("lastScheduledOutreachAt")
    if not last_at:
        return True
    elapsed_hours = (now - datetime.fromisoformat(last_at).astimezone()).total_seconds() / 3600
    return elapsed_hours >= config.OUTREACH_BATCH_INTERVAL_HOURS


def _outreach_loop():
    """Automated equivalent of clicking "Send" on the dashboard for every
    product's queue, in batches across the day - the missing piece that
    actually drives the 100%/50% adoption targets, since _auto_advance_loop
    and _tracking_check_loop above only manage orders that already exist.
    Every batch re-evaluates the queues fresh (same engine.evaluate()
    current_view() uses) so a CSP who became sendable since the last batch
    (new import, reconciliation resolved) is picked up automatically.
    outreach.run_outreach already enforces EMAIL_MAX_PER_RUN (the per-batch,
    per-product cap) and the 8-touch send-day schedule
    (config.CAMPAIGN_SEND_DAY_OFFSETS - see outreach.next_touch), and respects
    EMAIL_MODE (dry composes and reports, same as a manual send) - this loop
    adds no separate safety logic of its own, it only removes the need for
    someone to trigger each batch by hand.

    Timing is config.OUTREACH_SEND_AT_HOUR for the day's first batch, which
    config guarantees is later than the calling-sheet refresh - so every
    batch today is built from today's sheet, not yesterday's.
    """
    while True:
        try:
            with store.transaction() as state:
                if _outreach_due(state):
                    payload = store.load_csps()
                    _sweep(state)
                    result = engine.evaluate(payload["csps"], state["reconciliations"])
                    for product_id, queue in result["queues"].items():
                        outcome = outreach.run_outreach(state, queue)
                        if outcome["mode"] != "dry" and (outcome["sent"] or outcome["failed"]):
                            store.audit(state, "OUTREACH_RUN", {
                                "productId": product_id, "mode": outcome["mode"],
                                "sent": len(outcome["sent"]), "failed": len(outcome["failed"]),
                                "held": len(outcome["held"]), "trigger": "scheduled-batch",
                            })
                    # Stamped whatever the batch produced - including one that sent
                    # nothing because nobody was due. The marker means "a batch
                    # attempt happened just now", not "mail went out"; without
                    # that, an empty batch would retry every tick instead of
                    # waiting out the full interval like a real batch would.
                    state["lastScheduledOutreachAt"] = datetime.now().astimezone().isoformat()
                    store.save_state(state)
        except Exception as err:                          # noqa: BLE001 - see the other two loops
            print(f"[outreach-schedule] tick failed: {err}", flush=True)
        time.sleep(_OUTREACH_TICK_SECONDS)


_started = False


def start_background() -> None:
    """Opt-in (CSP_MONITOR_GROWTH_WORKER=1, see app.py) and idempotent. Starts the order timers + daily Calling Sheet
    refresh, and the scheduled drip batches when config.OUTREACH_SCHEDULE_ENABLED. The drip still only composes until
    GROWTH_EMAIL_MODE=live. The courier-tracking and WhatsApp loops of the original app are not part of this merge."""
    global _started
    if _started:
        return
    _started = True
    threading.Thread(target=_auto_advance_loop, daemon=True, name="growth-auto-advance").start()
    if config.OUTREACH_SCHEDULE_ENABLED:
        threading.Thread(target=_outreach_loop, daemon=True, name="growth-outreach").start()
    print(f"[growth] background loops started (drip schedule {'on' if config.OUTREACH_SCHEDULE_ENABLED else 'off'}, "
          f"GROWTH_EMAIL_MODE={emailer.MODE})", flush=True)
