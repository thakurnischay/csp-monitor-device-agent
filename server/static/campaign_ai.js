/* AI drafting assistant on the campaign form. No inline scripts (CSP); all text is inserted with textContent. */
(function () {
  var panel = document.getElementById("ai-panel");
  if (!panel) { return; }
  var form = panel.closest("form");
  var $ = function (id) { return document.getElementById(id); };
  var excluded = {};
  var lastGeneration = "";
  var timer = null;

  function el(tag, text, cls) {
    var n = document.createElement(tag);
    if (text !== undefined) { n.textContent = text; }
    if (cls) { n.className = cls; }
    return n;
  }
  function params(extra) {
    var p = new URLSearchParams();
    p.set("csrf_token", form.querySelector("[name=csrf_token]").value);
    p.set("occasion", form.occasion.value);
    p.set("audience_match", form.audience_match.value);
    Array.prototype.forEach.call(form.querySelectorAll("[name=devices]:checked"), function (c) { p.append("devices", c.value); });
    p.set("brief", $("ai_brief").value);
    Object.keys(excluded).forEach(function (id) { if (excluded[id]) { p.append("excluded", id); } });
    Object.keys(extra || {}).forEach(function (k) { p.set(k, extra[k]); });
    return p;
  }
  function post(url, extra) {
    return fetch(url, { method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/x-www-form-urlencoded" }, body: params(extra).toString() })
      .then(function (r) { return r.json().catch(function () { return { ok: false, error: "Unexpected reply (HTTP " + r.status + ")." }; }); });
  }
  function status(msg, bad) { var s = $("ai-status"); s.textContent = msg || ""; s.style.color = bad ? "#b42318" : ""; }

  function renderChunks(chunks, used) {
    var box = $("ai-chunks");
    box.textContent = "";
    var usedSet = {};
    (used || []).forEach(function (i) { usedSet[i] = true; });
    chunks.forEach(function (c) {
      var row = el("div", undefined, "ai-chunk");
      var cb = document.createElement("input");
      cb.type = "checkbox";
      cb.checked = !!c.included;
      cb.disabled = !!c.locked;
      cb.title = c.locked ? "Always included (rules the assistant must follow)" : "Include or leave out";
      cb.addEventListener("change", function () { excluded[c.id] = !cb.checked; schedule(); });
      var label = el("label", " " + c.title, "ai-chunk-title");
      label.insertBefore(cb, label.firstChild);
      row.appendChild(label);
      var meta = [c.locked ? "locked" : "optional", c.source, c.tokens + " tokens"];
      if (!c.locked) { meta.push("relevance " + c.score); }
      if (!c.included && c.reason) { meta.push("left out: " + c.reason); }
      row.appendChild(el("span", " " + meta.join(" · "), "note"));
      if (usedSet[c.id] || c.used) { row.appendChild(el("span", "used", "badge badge-green")); }
      var det = el("details");
      det.appendChild(el("summary", "text"));
      det.appendChild(el("pre", c.text, "ai-pre"));
      row.appendChild(det);
      box.appendChild(row);
    });
  }
  function renderBudget(used, budget) {
    var b = $("ai-budget");
    b.textContent = "";
    var pct = Math.min(100, Math.round(100 * used / Math.max(budget, 1)));
    b.appendChild(el("span", "Context: about " + used + " of " + budget + " tokens "));
    var bar = el("span", undefined, "ai-bar");
    var fill = el("span", undefined, "ai-bar-fill");
    fill.style.width = pct + "%";
    bar.appendChild(fill);
    b.appendChild(bar);
  }
  function preview() {
    status("Checking context...");
    return post(panel.getAttribute("data-context-url")).then(function (d) {
      if (!d.ok) { status(d.error, true); return; }
      status("");
      renderChunks(d.chunks);
      renderBudget(d.tokens_used_est, d.budget);
    }).catch(function () { status("Could not reach the server.", true); });
  }
  function schedule() { clearTimeout(timer); timer = setTimeout(function () { if (form.occasion.value.trim()) { preview(); } }, 500); }

  function renderFlags(flags) {
    var box = $("ai-flags");
    box.textContent = "";
    if (!flags.length) { box.appendChild(el("p", "No wording needing a second look was detected (this is a simple keyword check, not a guarantee).", "note")); return; }
    box.appendChild(el("p", "Please double-check:", "note"));
    flags.forEach(function (f) {
      var p = el("p", undefined, "note");
      p.appendChild(el("span", f.severity, "badge " + (f.severity === "high" ? "badge-red" : f.severity === "medium" ? "badge-amber" : "badge-gray")));
      p.appendChild(document.createTextNode(" " + f.why + ": “" + f.phrase + "” in “" + f.sentence + "”"));
      box.appendChild(p);
    });
  }
  function draft() {
    if (!form.occasion.value.trim()) { status("Choose the festival / occasion first.", true); return; }
    var btn = $("ai-draft");
    btn.disabled = true;
    status("Drafting... this can take up to a minute.");
    post(panel.getAttribute("data-draft-url"), lastGeneration ? { replaces_generation_id: lastGeneration } : {}).then(function (d) {
      btn.disabled = false;
      if (!d.ok) { status(d.error, true); return; }
      var g = d.generation;
      lastGeneration = String(g.generation_id);
      $("ai_generation_id").value = lastGeneration;
      form.subject.value = g.subject;
      form.body.value = g.body;
      status("Draft ready. Review and edit it below.");
      $("ai-result").hidden = false;
      var used = g.chunks.filter(function (c) { return c.used; }).map(function (c) { return c.title; });
      $("ai-why").textContent = used.length ? "built from: " + used.join(", ") + "." : "built from the locked rules only.";
      renderChunks(g.chunks, g.cited);
      renderBudget(g.tokens_used_est, g.budget);
      renderFlags(g.flags);
    }).catch(function () { btn.disabled = false; status("Could not reach the server.", true); });
  }

  $("ai-preview").addEventListener("click", preview);
  $("ai-draft").addEventListener("click", draft);
  form.occasion.addEventListener("change", schedule);
  form.addEventListener("change", function (e) { if (e.target.name === "devices") { schedule(); } });
})();
