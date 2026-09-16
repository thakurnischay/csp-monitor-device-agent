"""
Tiny local status/config page for the agent — LOCALHOST ONLY, no auth needed
because it never leaves the CSP's own PC. This is the one-time setup screen
where the CSP:
  - points the agent at the central server (URL + CSP Code + API key), and
  - picks which installed printer / which USB device is theirs.

Everything else (the actual reporting) runs headless in reporter.py.
"""
import logging

from flask import Flask, jsonify, render_template_string, request

import config_store
import device_health
import reporter

log = logging.getLogger("csp_agent.local_ui")

app = Flask(__name__)

PAGE = """
<!doctype html>
<title>CSP Device Monitor Agent</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  body { font-family: system-ui, sans-serif; max-width: 640px; margin: 32px auto; padding: 0 16px; color: #1a1a1a; }
  h1 { font-size: 1.3rem; }
  .card { border: 1px solid #ddd; border-radius: 8px; padding: 16px; margin-bottom: 16px; }
  label { display: block; font-weight: 600; margin: 10px 0 4px; font-size: 0.9rem; }
  input, select { width: 100%; padding: 6px 8px; font-size: 0.95rem; box-sizing: border-box; }
  button { margin-top: 14px; padding: 8px 16px; font-size: 0.95rem; cursor: pointer; }
  .badge { display: inline-block; padding: 2px 10px; border-radius: 10px; font-size: 0.8rem; color: #fff; }
  .ok { background: #1a7f37; } .bad { background: #c62828; } .off { background: #888; }
  .row { display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid #eee; padding: 6px 0; }
  #msg { margin-top: 10px; font-size: 0.9rem; }
</style>
<h1>CSP Device Monitor Agent</h1>
<p>Reports passbook-printer and micro-ATM connectivity to the central dashboard every few minutes.</p>

<div class="card">
  <div class="row"><span><strong>Passbook printer</strong></span><span id="printerBadge" class="badge off">checking...</span></div>
  <div id="printerDetail" style="color:#666;font-size:0.9rem;margin-bottom:8px">-</div>
  <div id="printerAutoNote" style="color:#a15c00;font-size:0.82rem;margin-bottom:8px"></div>
  <div id="printerFunctionalNote" style="color:#555;font-size:0.82rem;margin-bottom:8px"></div>
  <label>Which printer is it?</label>
  <div style="color:#666;font-size:0.82rem;margin-bottom:4px">The agent fills this in automatically when it recognizes a known printer brand - checking both installed Windows printers and USB/serial (COM port) devices, since many passbook printers connect that way instead. If it can't recognize the brand, every real printer and serial device it finds is still listed below to pick manually (or leave it as "not selected" if this PC has no physical passbook printer). Once a day it also sends a real test page to catch things like out-of-paper or a paper jam.</div>
  <select id="printerSelect"></select>

  <div class="row" style="margin-top:14px"><span><strong>Micro-ATM</strong></span><span id="microatmBadge" class="badge off">checking...</span></div>
  <div id="microatmDetail" style="color:#666;font-size:0.9rem;margin-bottom:8px">-</div>
  <div id="microatmAutoNote" style="color:#a15c00;font-size:0.82rem;margin-bottom:8px"></div>
  <label>Which USB device is it?</label>
  <div style="color:#666;font-size:0.82rem;margin-bottom:4px">Also filled in automatically for known micro-ATM brands - pick one here only if it can't recognize your device, or leave "not selected" if this PC has no micro-ATM.</div>
  <select id="microatmSelect"></select>

  <button onclick="saveDevices()">Save device selection</button>
</div>

<div class="card">
  <div class="row"><span><strong>CSP software</strong> (optional)</span><span id="softwareBadge" class="badge off">not set</span></div>
  <div id="softwareDetail" style="color:#666;font-size:0.9rem;margin-bottom:8px">
    If your printer/micro-ATM is run by a separate program, entering its exact
    process name here (check Task Manager's Details tab while it's open) lets
    this also catch "device is fine but the software crashed."
  </div>
  <label>Process name (e.g. MicroATM.exe)</label>
  <input id="softwareProcess" placeholder="leave blank if unknown">
  <button onclick="saveSoftware()">Save software process</button>
</div>

<div class="card">
  <strong>Server connection</strong>
  <label>Server URL</label>
  <input id="serverUrl" placeholder="http://your-server:5100">
  <label>CSP Code</label>
  <input id="cspId" placeholder="e.g. 1AB50895">
  <label>API key</label>
  <input id="apiKey" type="password" placeholder="issued by the admin">
  <button onclick="saveServer()">Save connection</button>
  <button onclick="reportNow()" style="margin-left:8px">Report now</button>
  <div id="msg"></div>
</div>

<script>
// Plain XMLHttpRequest + ES5 syntax throughout this file on purpose (no
// fetch, no arrow functions, no const/let) - a real CSP PC can be running an
// old or locked-down browser that silently fails to parse modern JS, which
// leaves the WHOLE script dead: badges stuck on "checking...", dropdowns
// with zero options, buttons that do nothing. XHR + ES5 works on essentially
// any Windows browser ever shipped, so this class of failure can't recur.
// Every call also has a hard client-side timeout so a genuinely hung agent
// (e.g. a security tool silently blocking the PowerShell scans) still shows
// a clear error instead of spinning forever.
function xhrJson(method, url, body, onSuccess, onError, timeoutMs) {
  var xhr = new XMLHttpRequest();
  xhr.open(method, url, true);
  if (body !== null) {
    xhr.setRequestHeader("Content-Type", "application/json");
  }
  xhr.timeout = timeoutMs || 20000;
  xhr.onload = function () {
    if (xhr.status >= 200 && xhr.status < 300) {
      var data = null;
      try { data = JSON.parse(xhr.responseText); } catch (e) { data = null; }
      onSuccess(data);
    } else {
      onError();
    }
  };
  xhr.onerror = function () { onError(); };
  xhr.ontimeout = function () { onError(); };
  try {
    xhr.send(body !== null ? JSON.stringify(body) : null);
  } catch (e) {
    onError();
  }
}
function badge(id, detailId, status, autoNoteId, manualName) {
  var b = document.getElementById(id), d = document.getElementById(detailId);
  if (!status.configured) { b.textContent = "not set up"; b.className = "badge off"; }
  else if (status.ok) { b.textContent = "OK"; b.className = "badge ok"; }
  else { b.textContent = "problem"; b.className = "badge bad"; }
  d.textContent = status.status || "-";
  if (autoNoteId) {
    var note = document.getElementById(autoNoteId);
    if (status.resolved_name && manualName && status.resolved_name !== manualName) {
      note.textContent = "Auto-corrected: using \\"" + status.resolved_name + "\\" instead of the selected \\"" + manualName + "\\".";
    } else {
      note.textContent = "";
    }
  }
}
function functionalNote(id, ft) {
  var el = document.getElementById(id);
  if (!ft || !ft.date) { el.textContent = "Test print: not run yet (runs automatically once a day)."; return; }
  var r = ft.result || {};
  if (!r.ran) { el.textContent = "Test print (" + ft.date + "): " + (r.detail || "skipped"); return; }
  el.textContent = "Test print (" + ft.date + "): " + (r.ok ? "OK - " : "PROBLEM - ") + (r.detail || "");
}
function fillSelect(sel, options, current) {
  sel.innerHTML = "";
  var blank = document.createElement("option");
  blank.value = ""; blank.textContent = "-- not selected --";
  sel.appendChild(blank);
  var found = false;
  for (var i = 0; i < (options || []).length; i++) {
    var name = options[i];
    var o = document.createElement("option");
    o.value = name; o.textContent = name;
    if (name === current) { o.selected = true; found = true; }
    sel.appendChild(o);
  }
  if (current && !found) {
    var o2 = document.createElement("option");
    o2.value = current; o2.textContent = current + " (not currently detected)"; o2.selected = true;
    sel.appendChild(o2);
  }
}
function loadStatus() {
  var slowTimer = setTimeout(function () {
    document.getElementById("printerDetail").textContent = "Still checking - this can take up to 20-30 seconds on some PCs.";
    document.getElementById("microatmDetail").textContent = "Still checking - this can take up to 20-30 seconds on some PCs.";
  }, 4000);
  xhrJson("GET", "/status", null, function (d) {
    clearTimeout(slowTimer);
    if (!d) { document.getElementById("msg").textContent = "Could not load status from the agent - is it still running?"; return; }
    badge("printerBadge", "printerDetail", d.printer, "printerAutoNote", d.config.printer_name);
    badge("microatmBadge", "microatmDetail", d.microatm, "microatmAutoNote", d.config.microatm_name);
    functionalNote("printerFunctionalNote", d.printer_functional_test);
    fillSelect(document.getElementById("printerSelect"), d.available_printers, d.config.printer_name);
    fillSelect(document.getElementById("microatmSelect"), d.available_usb_devices, d.config.microatm_name);
    document.getElementById("serverUrl").value = d.config.server_url || "";
    document.getElementById("cspId").value = d.config.csp_id || "";
    document.getElementById("softwareProcess").value = d.config.software_process || "";
    var sb = document.getElementById("softwareBadge");
    if (!d.software.configured) { sb.textContent = "not set"; sb.className = "badge off"; }
    else if (d.software.running) { sb.textContent = "running"; sb.className = "badge ok"; }
    else { sb.textContent = "not running"; sb.className = "badge bad"; }
  }, function () {
    clearTimeout(slowTimer);
    document.getElementById("msg").textContent = "Could not load status from the agent - is it still running?";
  }, 25000);
}
function saveDevices() {
  document.getElementById("msg").textContent = "Saving...";
  xhrJson("POST", "/configure", {
    printer_name: document.getElementById("printerSelect").value,
    microatm_name: document.getElementById("microatmSelect").value
  }, function () {
    document.getElementById("msg").textContent = "Device selection saved.";
    loadStatus();
  }, function () {
    document.getElementById("msg").textContent = "Could not save - is the agent still running?";
  }, 10000);
}
function saveSoftware() {
  document.getElementById("msg").textContent = "Saving...";
  xhrJson("POST", "/configure", {
    software_process: document.getElementById("softwareProcess").value
  }, function () {
    document.getElementById("msg").textContent = "Software process saved.";
    loadStatus();
  }, function () {
    document.getElementById("msg").textContent = "Could not save - is the agent still running?";
  }, 10000);
}
function saveServer() {
  document.getElementById("msg").textContent = "Saving...";
  xhrJson("POST", "/configure", {
    server_url: document.getElementById("serverUrl").value,
    csp_id: document.getElementById("cspId").value,
    api_key: document.getElementById("apiKey").value
  }, function () {
    document.getElementById("msg").textContent = "Saved.";
    document.getElementById("apiKey").value = "";
  }, function () {
    document.getElementById("msg").textContent = "Could not save - is the agent still running?";
  }, 10000);
}
function reportNow() {
  document.getElementById("msg").textContent = "Reporting...";
  xhrJson("POST", "/report_now", null, function (d) {
    if (!d) { document.getElementById("msg").textContent = "Report failed - no response from the agent."; return; }
    document.getElementById("msg").textContent = d.ok ? "Reported successfully." : ("Failed: " + d.error);
  }, function () {
    document.getElementById("msg").textContent = "Report failed - request timed out or the agent is not responding.";
  }, 40000);
}
window.onerror = function (msg) {
  var m = document.getElementById("msg");
  if (m) { m.textContent = "Page error: " + msg; }
};
loadStatus();
</script>
"""


@app.route("/")
def index():
    return render_template_string(PAGE)


@app.route("/status")
def status():
    cfg = config_store.load()
    health = device_health.check(cfg.get("printer_name", ""), cfg.get("microatm_name", ""),
                                 cfg.get("software_process", ""))
    # Self-configure: a confidently-recognized real device gets saved as the
    # actual selection, so the dropdown (and the dashboard) show it without
    # anyone touching Save - re-read cfg so THIS same page load reflects it.
    config_store.sync_resolved_device("printer_name", health["printer"].get("resolved_name", ""))
    config_store.sync_resolved_device("microatm_name", health["microatm"].get("resolved_name", ""))
    cfg = config_store.load()
    safe_cfg = {k: v for k, v in cfg.items() if k != "api_key"}
    current_printer = cfg.get("printer_name", "")
    # Reuse the SAME scan check() already did (health["printer_list"]/
    # ["usb_device_list"]) instead of scanning again - three separate scans
    # per page load used to make this page look stuck on a slow/real CSP PC.
    printers = [p["name"] for p in health["printer_list"]
               if p["name"] == current_printer or not device_health.is_likely_virtual_printer(p["name"])]
    return jsonify({
        "printer": health["printer"],
        "microatm": health["microatm"],
        "software": health["software"],
        "config": safe_cfg,
        "available_printers": printers,
        "available_usb_devices": [d["name"] for d in health["usb_device_list"]],
        "printer_functional_test": {
            "date": cfg.get("last_printer_test_date", ""),
            "result": cfg.get("last_printer_test_result", {}),
        },
    })


@app.route("/configure", methods=["POST"])
def configure():
    data = request.get_json(silent=True) or {}
    allowed = {"printer_name", "microatm_name", "software_process", "server_url",
              "csp_id", "api_key", "interval_seconds"}
    updates = {k: v for k, v in data.items() if k in allowed and v not in (None, "")}
    # An explicitly blank device/software selection should still clear it.
    for k in ("printer_name", "microatm_name", "software_process"):
        if k in data and data[k] == "":
            updates[k] = ""
    config_store.save(**updates)
    return jsonify({"ok": True})


@app.route("/report_now", methods=["POST"])
def report_now():
    return jsonify(reporter.report_once())


def run(host: str = "127.0.0.1", port: int = 5057):
    app.run(host=host, port=port)
