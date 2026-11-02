/* Email Campaigns: confirmation dialogs for consequential actions (no inline scripts: the app's CSP forbids them). */
document.addEventListener("submit", function (e) {
  var f = e.target;
  var submitter = e.submitter;
  var msg = (submitter && submitter.getAttribute("data-confirm")) || f.getAttribute("data-confirm");
  if (msg && !window.confirm(msg)) { e.preventDefault(); }
});
document.addEventListener("change", function (e) {
  var t = e.target;
  if (t.matches && t.matches("[data-toggle-target]")) {
    var box = document.getElementById(t.getAttribute("data-toggle-target"));
    if (box) { box.style.display = t.checked ? "" : "none"; }
  }
});
