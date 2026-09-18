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
