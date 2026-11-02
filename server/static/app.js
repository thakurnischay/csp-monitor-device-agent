// External file, not inline - the app's CSP is script-src 'self', which
// blocks both inline <script> blocks and inline onclick/onchange attributes.

function copyToClipboard(text, btn) {
  function done() {
    var original = btn.innerHTML;
    btn.classList.add("copied");
    btn.innerHTML = "&#10003;";
    setTimeout(function () { btn.classList.remove("copied"); btn.innerHTML = original; }, 1500);
  }
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).then(done, done);
  } else {
    var ta = document.createElement("textarea");
    ta.value = text; document.body.appendChild(ta); ta.select();
    try { document.execCommand("copy"); } catch (e) {}
    document.body.removeChild(ta);
    done();
  }
}

document.addEventListener("click", function (e) {
  var btn = e.target.closest(".copy-btn");
  if (btn && btn.dataset.copy !== undefined) {
    copyToClipboard(btn.dataset.copy, btn);
  }
});

document.addEventListener("change", function (e) {
  if (e.target.matches(".auto-submit")) {
    e.target.form.submit();
  }
});

// ---- app shell: collapsible sidebar (desktop, remembered) and slide-in drawer (tablet/phone) ----
(function () {
  var root = document.documentElement, app = document.getElementById("app");
  if (!app) return;
  var KEY = "cspmon.sidebarCollapsed";
  try { if (localStorage.getItem(KEY) === "1") root.classList.add("sidebar-collapsed"); } catch (e) {}
  var collapseBtn = document.getElementById("collapseBtn"), menuBtn = document.getElementById("menuBtn"),
      scrim = document.getElementById("scrim");
  function setDrawer(open) {
    root.classList.toggle("nav-open", open);
    if (menuBtn) menuBtn.setAttribute("aria-expanded", open ? "true" : "false");
  }
  if (collapseBtn) collapseBtn.addEventListener("click", function () {
    var on = !root.classList.contains("sidebar-collapsed");
    root.classList.toggle("sidebar-collapsed", on);
    collapseBtn.setAttribute("aria-label", on ? "Expand sidebar" : "Collapse sidebar");
    try { localStorage.setItem(KEY, on ? "1" : "0"); } catch (e) {}
  });
  if (menuBtn) menuBtn.addEventListener("click", function () { setDrawer(!root.classList.contains("nav-open")); });
  if (scrim) scrim.addEventListener("click", function () { setDrawer(false); });
  document.addEventListener("keydown", function (e) { if (e.key === "Escape") setDrawer(false); });
})();

// ---- "Refresh now" buttons: reload the page with its current filters, showing that it is working ----
document.addEventListener("click", function (e) {
  var btn = e.target.closest("[data-refresh]");
  if (!btn) return;
  btn.setAttribute("aria-busy", "true");
  btn.classList.add("is-loading");
  window.location.reload();
});

// ---- confirmation for buttons/forms marked data-confirm ----
document.addEventListener("submit", function (e) {
  var f = e.target, sub = e.submitter, msg = (sub && sub.dataset.confirm) || f.dataset.confirm;
  if (msg && !window.confirm(msg)) e.preventDefault();
});

// ---- whole table row opens its detail page (the name link stays the keyboard/screen-reader path) ----
document.addEventListener("click", function (e) {
  var tr = e.target.closest("tr[data-href]");
  if (!tr || e.target.closest("a, button, input, select, label, form")) return;
  if (window.getSelection && String(window.getSelection())) return;      // selecting text, not navigating
  window.location.href = tr.dataset.href;
});

// ---- every data table scrolls inside its own box on narrow screens instead of widening the page ----
(function () {
  var tables = document.querySelectorAll("main table");
  for (var i = 0; i < tables.length; i++) {
    var t = tables[i];
    if (t.closest(".table-wrap, .table-scroll")) continue;
    var w = document.createElement("div");
    w.className = "table-wrap";
    t.parentNode.insertBefore(w, t);
    w.appendChild(t);
  }
})();
