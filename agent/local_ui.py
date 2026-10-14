"""
Tiny local status/config page for the agent — LOCALHOST ONLY, no auth needed
because it never leaves the CSP's own PC. This is the one-time setup screen
where the CSP:
  - points the agent at the central server (URL + CSP Code + API key), and
  - picks which installed printer / which USB device is theirs.

Everything else (the actual reporting) runs headless in reporter.py.
"""
import logging
from datetime import date

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
  .ok { background: #1a7f37; } .bad { background: #c62828; } .off { background: #888; } .warn { background: #b26a00; }
  .found { border: 1px solid #ddd; border-radius: 6px; padding: 8px 10px; margin: 6px 0; font-size: 0.9rem; }
  .found button { margin-top: 6px; margin-right: 6px; padding: 4px 10px; font-size: 0.82rem; }
  .row { display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid #eee; padding: 6px 0; }
  #msg { margin-top: 10px; font-size: 0.9rem; }
</style>
<h1>CSP Device Monitor Agent</h1>
<p>Reports passbook-printer and micro-ATM connectivity to the central dashboard every few minutes.</p>

<div class="card">
  <div id="scanBanner" style="display:none;background:#fdecea;color:#8a1c14;border:1px solid #f5c2bd;border-radius:6px;padding:10px 12px;margin-bottom:12px;font-size:0.9rem"></div>
  <div class="row"><span><strong>Passbook printer</strong></span><span id="printerBadge" class="badge off">checking...</span></div>
  <div id="printerDetail" style="color:#666;font-size:0.9rem;margin-bottom:8px">-</div>
  <div id="printerAutoNote" style="color:#a15c00;font-size:0.82rem;margin-bottom:8px"></div>
  <div id="printerFunctionalNote" style="color:#555;font-size:0.82rem;margin-bottom:8px"></div>
  <label>Which printer is it?</label>
  <div style="color:#666;font-size:0.82rem;margin-bottom:4px">The agent fills this in automatically when it recognizes a known printer brand - checking both installed Windows printers and USB/serial (COM port) devices, since many passbook printers connect that way instead. If it can't recognize the brand, every real printer and serial device it finds is offered below to pick from - and if the scan itself can't run at all (e.g. blocked by antivirus/Smart App Control), you can type the exact device name here yourself instead. Leave it blank if this PC has no physical passbook printer. Once a day it also sends a real test page to catch things like out-of-paper or a paper jam.</div>
  <select id="printerPick" onchange="pickDevice('printer')" style="width:100%;margin-bottom:6px"></select>
  <input id="printerSelect" list="printerOptions" placeholder="Pick from the list, or type the exact device name">
  <datalist id="printerOptions"></datalist>

  <div class="row" style="margin-top:14px"><span><strong>Micro-ATM</strong></span><span id="microatmBadge" class="badge off">checking...</span></div>
  <div id="microatmDetail" style="color:#666;font-size:0.9rem;margin-bottom:8px">-</div>
  <div id="microatmAutoNote" style="color:#a15c00;font-size:0.82rem;margin-bottom:8px"></div>
  <label>Which USB device is it?</label>
  <div style="color:#666;font-size:0.82rem;margin-bottom:4px">Also filled in automatically for known micro-ATM brands. If it can't recognize your device, pick from the list, or type the exact device name yourself if the scan can't run at all. Leave it blank if this PC has no micro-ATM.</div>
  <select id="microatmPick" onchange="pickDevice('microatm')" style="width:100%;margin-bottom:6px"></select>
  <input id="microatmSelect" list="microatmOptions" placeholder="Pick from the list, or type the exact device name">
  <datalist id="microatmOptions"></datalist>

  <div class="row" style="margin-top:14px"><span><strong>Biometric device (fingerprint scanner)</strong></span><span id="biometricBadge" class="badge off">checking...</span></div>
  <div id="biometricDetail" style="color:#666;font-size:0.9rem;margin-bottom:8px">-</div>
  <div id="biometricAutoNote" style="color:#a15c00;font-size:0.82rem;margin-bottom:8px"></div>
  <label>Which USB device is it?</label>
  <div style="color:#666;font-size:0.82rem;margin-bottom:4px">Filled in automatically for known fingerprint-scanner brands (Mantra, Morpho, SecuGen, Startek and others). If it can't recognize yours, pick from the list, or type the exact device name. Leave it blank if this PC has no fingerprint scanner.</div>
  <select id="biometricPick" onchange="pickDevice('biometric')" style="width:100%;margin-bottom:6px"></select>
  <input id="biometricSelect" list="biometricOptions" placeholder="Pick from the list, or type the exact device name">
  <datalist id="biometricOptions"></datalist>

  <div class="row" style="margin-top:14px"><span><strong>GPS dongle</strong></span><span id="gpsBadge" class="badge off">checking...</span></div>
  <div id="gpsDetail" style="color:#666;font-size:0.9rem;margin-bottom:8px">-</div>
  <div id="gpsAutoNote" style="color:#a15c00;font-size:0.82rem;margin-bottom:8px"></div>
  <label>Which device is it?</label>
  <div style="color:#666;font-size:0.82rem;margin-bottom:4px">Filled in automatically when the dongle's name says GPS/GNSS (u-blox, G-Mouse and similar). Many dongles only show up as a generic "USB-SERIAL ... (COM3)" device - if so, pick it from the list yourself. Leave it blank if this PC has no GPS dongle.</div>
  <select id="gpsPick" onchange="pickDevice('gps')" style="width:100%;margin-bottom:6px"></select>
  <input id="gpsSelect" list="gpsOptions" placeholder="Pick from the list, or type the exact device name">
  <datalist id="gpsOptions"></datalist>

  <button onclick="saveDevices()">Save device selection</button>
</div>

<div class="card">
  <div class="row"><span><strong>Not sure which entry is your device? Find it by plugging it in</strong></span></div>
  <div style="color:#666;font-size:0.85rem;margin:8px 0">
    Entries such as "USB Input Device" are generic - a keyboard has the same name - so picking one by guess proves nothing,
    and the dashboard will show <b>unverified</b> for it. This test shows exactly which entry belongs to the device:
    <ol style="margin:6px 0 6px 18px;padding:0">
      <li>Unplug the device (or switch it off) and click <b>Step 1</b>.</li>
      <li>Plug it back in, wait about 10 seconds, and click <b>Step 2</b>.</li>
      <li>Click the button that matches what you plugged in. That entry is then confirmed.</li>
    </ol>
  </div>
  <button onclick="findStep1()">Step 1 - remember what is connected now</button>
  <button onclick="findStep2()">Step 2 - what appeared?</button>
  <div id="findResult" style="margin-top:8px;font-size:0.9rem"></div>
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
  else if (status.ok && status.verified === false) { b.textContent = "unverified"; b.className = "badge warn"; }
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
function fillSelect(inputEl, datalistEl, options, current, labels) {
  // A plain <input list="..."> combo box, not a rigid <select>: shows
  // whatever the scan found as suggestions, but ALWAYS lets someone type the
  // exact device name directly - essential when the scan can't run at all
  // (blocked by Smart App Control/antivirus) and finds literally nothing.
  datalistEl.innerHTML = "";
  for (var i = 0; i < (options || []).length; i++) {
    var o = document.createElement("option");
    o.value = options[i];
    // The caption shown next to the name (maker, USB id, "likely match").
    if (labels && labels[options[i]]) { o.label = labels[options[i]]; }
    datalistEl.appendChild(o);
  }
  // A normal, clearly-visible drop-down next to the type-in box: a datalist
  // gives no visible arrow in some browsers, so people could not tell a
  // list existed at all. Choosing here fills the box below.
  var pick = document.getElementById(inputEl.id.replace("Select", "Pick"));
  if (pick) {
    pick.innerHTML = "";
    var first = document.createElement("option");
    first.value = "";
    first.textContent = (options && options.length)
      ? "Click here to choose from the " + options.length + " devices found on this PC..."
      : "No devices found by the scan - type the device name in the box below";
    pick.appendChild(first);
    for (var j = 0; j < (options || []).length; j++) {
      var po = document.createElement("option");
      po.value = options[j];
      po.textContent = (labels && labels[options[j]]) ? labels[options[j]] : options[j];
      if (options[j] === current) { po.selected = true; first.selected = false; }
      pick.appendChild(po);
    }
  }
  // Never overwrite text the person is actively typing.
  if (document.activeElement !== inputEl) {
    inputEl.value = current || "";
  }
}
function findStep1() {
  var out = document.getElementById("findResult");
  out.textContent = "Remembering what is connected now - please wait...";
  xhrJson("POST", "/snapshot", {}, function (d) {
    out.textContent = (d && d.ok) ? ("Done - " + d.count + " entries remembered. Now plug the device in, wait about 10 seconds, then click Step 2.")
                                  : "Could not read the device list - try again.";
  }, function () { out.textContent = "Could not read the device list - is the agent still running?"; }, 60000);
}
function findStep2() {
  var out = document.getElementById("findResult");
  out.textContent = "Looking for what changed - please wait...";
  xhrJson("POST", "/detect", {}, function (d) {
    out.innerHTML = "";
    if (!d || !d.ok) { out.textContent = (d && d.error) || "Could not read the device list - try again."; return; }
    if (!d.appeared.length) {
      out.textContent = "Nothing new appeared. Make sure the device is plugged in and switched on, wait a few more seconds, and click Step 2 again.";
      return;
    }
    var names = [["printer", "passbook printer"], ["microatm", "micro-ATM"], ["biometric", "fingerprint scanner"], ["gps", "GPS dongle"]];
    for (var i = 0; i < d.appeared.length; i++) {
      var item = d.appeared[i];
      var box = document.createElement("div");
      box.className = "found";
      box.appendChild(document.createTextNode("Appeared: " + item.label));
      box.appendChild(document.createElement("br"));
      for (var j = 0; j < names.length; j++) {
        (function (it, key, text) {
          var b = document.createElement("button");
          b.textContent = "This is my " + text;
          b.onclick = function () { confirmDevice(key, it); };
          box.appendChild(b);
        })(item, names[j][0], names[j][1]);
      }
      out.appendChild(box);
    }
  }, function () { out.textContent = "Could not read the device list - is the agent still running?"; }, 60000);
}
function confirmDevice(key, item) {
  xhrJson("POST", "/confirm", {device: key, name: item.name, instance: item.instance || ""}, function (d) {
    document.getElementById("findResult").textContent = (d && d.ok) ? "Saved - this entry is now confirmed as your device." : "Could not save.";
    loadStatus();
  }, function () { document.getElementById("findResult").textContent = "Could not save - is the agent still running?"; }, 10000);
}
function pickDevice(prefix) {
  var pick = document.getElementById(prefix + "Pick");
  if (pick && pick.value) { document.getElementById(prefix + "Select").value = pick.value; }
}
function loadStatus() {
  var slowTimer = setTimeout(function () {
    document.getElementById("printerDetail").textContent = "Still checking - this can take up to 20-30 seconds on some PCs.";
    document.getElementById("microatmDetail").textContent = "Still checking - this can take up to 20-30 seconds on some PCs.";
    document.getElementById("biometricDetail").textContent = "Still checking - this can take up to 20-30 seconds on some PCs.";
    document.getElementById("gpsDetail").textContent = "Still checking - this can take up to 20-30 seconds on some PCs.";
  }, 4000);
  xhrJson("GET", "/status", null, function (d) {
    clearTimeout(slowTimer);
    if (!d) { document.getElementById("msg").textContent = "Could not load status from the agent - is it still running?"; return; }
    var sb2 = document.getElementById("scanBanner");
    if (d.scan_problem) { sb2.textContent = d.scan_problem; sb2.style.display = "block"; }
    else { sb2.style.display = "none"; }
    badge("printerBadge", "printerDetail", d.printer, "printerAutoNote", d.config.printer_name);
    badge("microatmBadge", "microatmDetail", d.microatm, "microatmAutoNote", d.config.microatm_name);
    badge("biometricBadge", "biometricDetail", d.biometric, "biometricAutoNote", d.config.biometric_name);
    badge("gpsBadge", "gpsDetail", d.gps, "gpsAutoNote", d.config.gps_name);
    functionalNote("printerFunctionalNote", d.printer_functional_test);
    fillSelect(document.getElementById("printerSelect"), document.getElementById("printerOptions"), d.available_printers, d.config.printer_name, d.device_labels);
    fillSelect(document.getElementById("microatmSelect"), document.getElementById("microatmOptions"), d.available_usb_devices, d.config.microatm_name, d.device_labels);
    fillSelect(document.getElementById("biometricSelect"), document.getElementById("biometricOptions"), d.available_biometric_devices, d.config.biometric_name, d.device_labels);
    fillSelect(document.getElementById("gpsSelect"), document.getElementById("gpsOptions"), d.available_gps_devices, d.config.gps_name, d.device_labels);
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
    microatm_name: document.getElementById("microatmSelect").value,
    biometric_name: document.getElementById("biometricSelect").value,
    gps_name: document.getElementById("gpsSelect").value
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
  // Save whatever is currently typed in the connection fields FIRST - a
  // real, repeated confusion: typing a new API key and clicking "Report now"
  // without also clicking "Save connection" reports using the OLD saved key
  // and fails, making a perfectly good new key look broken. A blank api_key
  // field (e.g. right after a previous save cleared it) is a no-op on the
  // server side, so this never wipes an already-saved key.
  document.getElementById("msg").textContent = "Saving and reporting...";
  xhrJson("POST", "/configure", {
    server_url: document.getElementById("serverUrl").value,
    csp_id: document.getElementById("cspId").value,
    api_key: document.getElementById("apiKey").value
  }, function () {
    document.getElementById("apiKey").value = "";
    xhrJson("POST", "/report_now", null, function (d) {
      if (!d) { document.getElementById("msg").textContent = "Report failed - no response from the agent."; return; }
      document.getElementById("msg").textContent = d.ok ? "Reported successfully." : ("Failed: " + d.error);
    }, function () {
      document.getElementById("msg").textContent = "Report failed - request timed out or the agent is not responding.";
    }, 40000);
  }, function () {
    document.getElementById("msg").textContent = "Could not save - is the agent still running?";
  }, 10000);
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
                                 cfg.get("software_process", ""),
                                 cfg.get("biometric_name", ""), cfg.get("gps_name", ""),
                                 config_store.confirmed_map(cfg))
    # Self-configure: a confidently-recognized real device gets saved as the
    # actual selection, so the dropdown (and the dashboard) show it without
    # anyone touching Save - re-read cfg so THIS same page load reflects it.
    config_store.sync_resolved_device("printer_name", health["printer"].get("resolved_name", ""))
    config_store.sync_resolved_device("microatm_name", health["microatm"].get("resolved_name", ""))
    config_store.sync_resolved_device("biometric_name", health["biometric"].get("resolved_name", ""))
    config_store.sync_resolved_device("gps_name", health["gps"].get("resolved_name", ""))
    cfg = config_store.load()
    # same verification the heartbeat applies, so this page and the dashboard agree
    todays_test = (cfg.get("last_printer_test_result") or {}
                   if cfg.get("last_printer_test_date") == date.today().isoformat() else {})
    health = device_health.apply_verification(health, config_store.confirmed_map(cfg), todays_test)
    safe_cfg = {k: v for k, v in cfg.items() if k != "api_key"}
    current_printer = cfg.get("printer_name", "")
    current_microatm = cfg.get("microatm_name", "")
    # Reuse the SAME scan check() already did (health["printer_list"]/
    # ["usb_device_list"]) instead of scanning again - three separate scans
    # per page load used to make this page look stuck on a slow/real CSP PC.
    printers = [p["name"] for p in health["printer_list"]
               if p["name"] == current_printer or not device_health.is_likely_virtual_printer(p["name"])]
    # Same idea for micro-ATM: a software-installed virtual bus/enumerator
    # placeholder (e.g. "IngenicoEnum") should never be suggested as if it
    # were the real device - it stays present in Windows even with nothing
    # actually plugged in, and is a real field case that got auto-picked
    # incorrectly before this filter existed.
    usb_devices = [d["name"] for d in health["usb_device_list"]
                  if d["name"] == current_microatm or not device_health.is_likely_virtual_usb_device(d["name"])]
    current_biometric = cfg.get("biometric_name", "")
    current_gps = cfg.get("gps_name", "")
    biometric_devices = [d["name"] for d in health["biometric_device_list"]
                         if d["name"] == current_biometric or not device_health.is_likely_virtual_usb_device(d["name"])]
    gps_devices = [d["name"] for d in health["gps_device_list"]
                   if d["name"] == current_gps or not device_health.is_likely_virtual_usb_device(d["name"])]
    labels = {}
    for lst, brands in ((health["usb_device_list"], device_health._KNOWN_MICROATM_BRANDS),
                        (health["biometric_device_list"], device_health._KNOWN_BIOMETRIC_BRANDS),
                        (health["gps_device_list"], device_health._KNOWN_GPS_BRANDS)):
        for d in lst:
            text = device_health.device_label(d)
            if device_health.is_recognised(d, brands):
                text = "likely match | " + text
            # the same device can sit in two lists; keep the "likely" caption
            if d["name"] not in labels or text.startswith("likely"):
                labels[d["name"]] = text
    return jsonify({
        "device_labels": labels,
        "scan_problem": health.get("scan_problem", ""),
        "printer": health["printer"],
        "microatm": health["microatm"],
        "biometric": health["biometric"],
        "gps": health["gps"],
        "software": health["software"],
        "config": safe_cfg,
        "available_printers": printers,
        "available_usb_devices": usb_devices,
        "available_biometric_devices": biometric_devices,
        "available_gps_devices": gps_devices,
        "printer_functional_test": {
            "date": cfg.get("last_printer_test_date", ""),
            "result": cfg.get("last_printer_test_result", {}),
        },
    })


_snapshot = {"instances": None, "printers": None}


def _scan_for_find():
    devs = device_health.list_all_devices()
    printers = [p["name"] for p in device_health.list_printers()]
    return devs, printers


@app.route("/snapshot", methods=["POST"])
def snapshot():
    """Plug-in test, step 1: remember every connected entry right now."""
    devs, printers = _scan_for_find()
    if not devs:
        return jsonify({"ok": False})
    _snapshot["instances"] = {d.get("instance") or d["name"] for d in devs}
    _snapshot["printers"] = set(printers)
    return jsonify({"ok": True, "count": len(devs) + len(printers)})


@app.route("/detect", methods=["POST"])
def detect():
    """Plug-in test, step 2: which entries are new since step 1."""
    if _snapshot["instances"] is None:
        return jsonify({"ok": False, "error": "Please click Step 1 first (with the device unplugged)."})
    devs, printers = _scan_for_find()
    if not devs:
        return jsonify({"ok": False})
    appeared = []
    for d in devs:
        key = d.get("instance") or d["name"]
        if key not in _snapshot["instances"] and not device_health.is_likely_virtual_usb_device(d["name"]):
            appeared.append({"kind": "device", "name": d["name"], "instance": d.get("instance", ""),
                             "label": device_health.device_label(d)})
    for name in printers:
        if name not in _snapshot["printers"] and not device_health.is_likely_virtual_printer(name):
            appeared.append({"kind": "printer", "name": name, "instance": "", "label": name + " (installed printer)"})
    return jsonify({"ok": True, "appeared": appeared})


@app.route("/confirm", methods=["POST"])
def confirm():
    """Plug-in test, step 3: the person says which kind of device an entry that
    appeared is. Saves it as the selection AND records that its identity was
    confirmed, which is what lets the dashboard show it as verified."""
    data = request.get_json(silent=True) or {}
    dev = data.get("device")
    name = (data.get("name") or "").strip()
    if dev not in ("printer", "microatm", "biometric", "gps") or not name:
        return jsonify({"ok": False}), 400
    config_store.save(**{dev + "_name": name, dev + "_confirmed": name,
                         dev + "_instance": (data.get("instance") or "").strip()})
    return jsonify({"ok": True})


@app.route("/configure", methods=["POST"])
def configure():
    data = request.get_json(silent=True) or {}
    allowed = {"printer_name", "microatm_name", "biometric_name", "gps_name",
              "software_process", "server_url", "csp_id", "api_key", "interval_seconds"}
    # Strip whitespace on every string field - a stray leading/trailing space
    # from a copy-paste (easy to pick up when copying the API key, which is
    # shown once and never re-shown) makes the saved value byte-for-byte
    # different from what the server actually stored, so every report is
    # silently rejected as "invalid API key" with no visible sign why.
    data = {k: (v.strip() if isinstance(v, str) else v) for k, v in data.items()}
    updates = {k: v for k, v in data.items() if k in allowed and v not in (None, "")}
    # An explicitly blank device/software selection should still clear it.
    for k in ("printer_name", "microatm_name", "biometric_name", "gps_name", "software_process"):
        if k in data and data[k] == "":
            updates[k] = ""
    config_store.save(**updates)
    return jsonify({"ok": True})


@app.route("/report_now", methods=["POST"])
def report_now():
    return jsonify(reporter.report_once())


def run(host: str = "127.0.0.1", port: int = 5057):
    app.run(host=host, port=port)
