/**
 * The self-serve order page's logic - real fetch() calls into
 * backend/app/orderpage.py's endpoints (lookup, resolve-token, submit,
 * config), replacing the hardcoded-data click-through in
 * design/order-page-mockup.html this was built from. See that mockup and
 * design/order-page-build-plan.html for the fuller design history.
 */

/* Derived from where THIS page is being served, not hardcoded to the host
 * root. The real deployment serves the app under a path prefix
 * (.../csp-growth-platform/order.html), so a root-relative "/api/..." would
 * request the host root, miss the prefix entirely, and 404 every call.
 * Taking the directory of the current page keeps it correct both under a
 * prefix and at the root during local development. */
const API = window.location.pathname.replace(/\/[^/]*$/, "") + "/api/order-page";

let S = { step: "loading", csp: null, product: null, price: null, pay: null,
          err: null, cfg: null, suggestedProductId: null,
          // Kept across re-renders so a validation error never wipes what
          // the CSP already typed - render() rebuilds innerHTML wholesale.
          addr: { line: "", landmark: "", city: "", pincode: "", state: "" } };

const REASON_TEXT = {
  "already-owned": "Aapke paas ye device pehle se hai.",
  "needs-rm": "Aapki details verify honi baaki hain - RM se sampark karein.",
  "requires-matm": "Printer lene ke liye pehle Micro ATM chahiye.",
  "affordability-band": "Ye product abhi aapke liye available nahi hai.",
};

function money(n) { return "₹" + Number(n).toLocaleString("en-IN"); }

/** Everything rendered through innerHTML that came from a user or the API
 *  goes through this - a CSP name or a typed address must never be able to
 *  inject markup into the page. */
function esc(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

async function api(path, opts) {
  const res = await fetch(API + path, opts);
  let body = null;
  try { body = await res.json(); } catch (e) { /* non-JSON error page */ }
  if (!res.ok && res.status !== 400) {
    throw new Error((body && body.error) || `Request failed (${res.status})`);
  }
  return { status: res.status, body: body || {} };
}

function dots(active, total) {
  let h = '<div style="display:flex;gap:6px;margin-top:20px">';
  for (let i = 0; i < total; i++) {
    h += `<div style="height:7px;border-radius:4px;background:${i === active ? 'var(--brand)' : 'var(--line)'};width:${i === active ? '18px' : '7px'}"></div>`;
  }
  return h + "</div>";
}

function render() {
  const body = document.getElementById("screenBody");
  const sub = document.getElementById("screenSub");
  let h = "";

  if (S.step === "loading") {
    sub.textContent = "Device Order";
    h = '<div class="loading-wrap"><div class="spin"></div>Loading...</div>';
  }

  else if (S.step === "identify") {
    sub.textContent = "Device Order";
    h += '<span class="step-badge">Step 1 of 5</span>';
    h += '<p class="field-label">Apna CSP code ya naam darj karein</p>';
    h += '<input class="field-input" id="idInput" placeholder="jaise: 1A850184 ya Amit Kumar" autofocus />';
    h += '<p class="hint">CSP code sabse pakka tarika hai. Naam se bhi dhoondh sakte hain.</p>';
    if (S.err) h += `<div class="err">${esc(S.err)}</div>`;
    h += '<button class="btn primary" id="btnSearch">Search</button>';
    h += dots(0, 5);
  }

  else if (S.step === "last4") {
    sub.textContent = "Aap kaunse hain?";
    h += '<span class="step-badge">Step 2 of 5 - pehchaan</span>';
    h += `<div class="err">Is naam ke <b>${S.count} CSPs</b> mile. Pehchaan pakki karne ke liye apne registered mobile number ke <b>aakhri 4 digits</b> daalein.</div>`;
    h += '<input class="field-input otp" id="l4Input" maxlength="4" inputmode="numeric" placeholder="&#8226;&#8226;&#8226;&#8226;" autofocus />';
    if (S.err) h += `<div class="err">${esc(S.err)}</div>`;
    h += '<button class="btn primary" id="btnLast4">Aage badhein</button>';
    h += '<button class="btn ghost" id="btnBack1">&larr; Wapas</button>';
    h += dots(1, 5);
  }

  else if (S.step === "rm") {
    sub.textContent = "RM se sampark";
    h += '<span class="step-badge">Verification chahiye</span>';
    h += '<div class="err">Aapki details automatically verify nahi ho paayi. Aapke RM aapse sampark karenge, ya aap unhe call kar sakte hain.</div>';
    h += '<button class="btn ghost" id="btnRetry">&larr; Dobara try karein</button>';
    h += dots(1, 5);
  }

  else if (S.step === "notfound") {
    sub.textContent = "Device Order";
    h += '<span class="step-badge">Step 1 of 5</span>';
    h += '<div class="err">Koi match nahi mila. Apna CSP code check karein, ya apne RM se sampark karein.</div>';
    h += '<button class="btn primary" id="btnRetry">&larr; Dobara try karein</button>';
    h += dots(0, 5);
  }

  else if (S.step === "alreadyOrdered") {
    sub.textContent = "Order Status";
    const o = S.order;
    h += '<div class="success"><div class="tick">&#10003;</div>';
    h += `<h3>${esc(S.csp.name)} ji, aap already order kar chuke hain</h3>`;
    if (o) {
      h += `<p>Product: <b>${(S.cfg && S.cfg.products[o.productId] && S.cfg.products[o.productId].name) || o.productId}</b></p>`;
      h += `<p>Status: <b>${o.status}</b></p>`;
    }
    h += '<p>Agar koi sawaal hai to apne RM se sampark karein.</p></div>';
  }

  else if (S.step === "confirm") {
    const c = S.csp;
    sub.textContent = "Details confirm karein";
    h += '<span class="step-badge">Step 2 of 5</span>';
    h += '<div class="profile-card">';
    h += `<div class="profile-name">${esc(c.name)}</div><div class="profile-code">${esc(c.cspCode)}</div>`;
    h += `<div class="profile-row"><span class="k">Mobile</span><span class="v">&#8226;&#8226;&#8226;&#8226;&#8226;&#8226; ${esc(c.mobileLast4) || "(darj nahi)"}</span></div>`;
    h += `<div class="profile-row"><span class="k">District</span><span class="v">${esc(c.district) || "-"}</span></div>`;
    h += `<div class="profile-row"><span class="k">State</span><span class="v">${esc(c.state) || "-"}</span></div>`;
    h += "</div>";
    h += '<p class="hint">Calling sheet se auto-fetch hui. Mobile poora nahi dikhaya jaata.</p>';
    h += '<button class="btn primary" id="btnConfirmYes">Haan, yeh main hoon &rarr;</button>';
    h += '<button class="btn ghost" id="btnRetry">Yeh main nahi hoon</button>';
    h += dots(1, 5);
  }

  else if (S.step === "product") {
    const c = S.csp;
    sub.textContent = "Product chunein";
    h += '<span class="step-badge">Step 3 of 5</span>';
    for (const pid of Object.keys(c.eligibility)) {
      const e = c.eligibility[pid];
      const name = (S.cfg.products[pid] && S.cfg.products[pid].name) || pid;
      const price = (S.cfg.products[pid] && S.cfg.products[pid].price) || e.price;
      if (e.orderable) {
        const sel = S.product === pid ? " selected" : "";
        h += `<div class="product-opt${sel}" data-pid="${pid}"><span class="pname">${esc(name)}</span><span class="pprice">${money(price)}</span></div>`;
      } else {
        h += `<div class="product-opt blocked"><span><span class="pname">${esc(name)}</span><span class="pblock">${REASON_TEXT[e.reason] || "Abhi available nahi"}</span></span><span class="pprice" style="color:var(--ink-3)">${price ? money(price) : ""}</span></div>`;
      }
    }
    if (S.product) {
      h += `<div class="amount-box"><div class="k">Amount payable</div><div class="v">${money(S.price)}</div></div>`;
    }
    h += `<button class="btn primary" id="btnProductNext" ${S.product ? "" : "disabled"}>Aage badhein &rarr;</button>`;
    h += '<button class="btn ghost" id="btnBack2">&larr; Wapas</button>';
    h += dots(2, 5);
  }

  else if (S.step === "address") {
    sub.textContent = "Delivery address";
    h += '<span class="step-badge">Step 4 of 5</span>';
    h += '<p class="field-label">Address (house no, street, locality)</p>';
    h += `<input class="field-input" id="addrLine" value="${esc(S.addr.line)}" placeholder="jaise: H.No 12, Main Road, Rampur" />`;
    h += '<p class="field-label">Landmark <span class="opt-tag">(optional)</span></p>';
    h += `<input class="field-input" id="addrLandmark" value="${esc(S.addr.landmark)}" placeholder="jaise: SBI branch ke paas" />`;
    h += '<div class="field-row">';
    h += `<div><p class="field-label">City / Town</p><input class="field-input" id="addrCity" value="${esc(S.addr.city)}" placeholder="City" /></div>`;
    h += `<div><p class="field-label">Pincode</p><input class="field-input" id="addrPincode" value="${esc(S.addr.pincode)}" maxlength="6" inputmode="numeric" placeholder="6 digits" /></div>`;
    h += "</div>";
    h += '<p class="field-label">State</p>';
    h += '<select class="field-input" id="addrState"><option value="">-- chunein --</option>';
    for (const st of (S.cfg.deliveryStates || [])) {
      h += `<option value="${esc(st)}"${S.addr.state === st ? " selected" : ""}>${esc(st)}</option>`;
    }
    h += "</select>";
    h += '<p class="hint">Device isi address par bheja jayega.</p>';
    if (S.err) h += `<div class="err">${esc(S.err)}</div>`;
    h += '<button class="btn primary" id="btnAddrNext">Aage badhein &rarr;</button>';
    h += '<button class="btn ghost" id="btnBackAddr">&larr; Wapas</button>';
    h += dots(3, 5);
  }

  else if (S.step === "payment") {
    sub.textContent = "Payment ka tarika chunein";
    h += '<span class="step-badge">Step 5 of 5</span>';
    h += `<div class="amount-box" style="margin-top:0"><div class="k">Amount payable</div><div class="v">${money(S.price)}</div></div>`;
    const bankSel = S.pay === "bank" ? " selected" : "";
    h += `<div class="pay-opt${bankSel}" data-pay="bank"><div class="pi">&#127974;</div><div><div class="pn">Bank transfer</div><div class="pd">NEFT / IMPS se bhejein, phir UTR daalein</div></div></div>`;
    if (S.cfg && S.cfg.qr && S.cfg.qr.enabled) {
      const qrSel = S.pay === "qr" ? " selected" : "";
      h += `<div class="pay-opt${qrSel}" data-pay="qr"><div class="pi">&#9635;</div><div><div class="pn">QR code se</div><div class="pd">UPI app se scan karein, amount daalein</div></div></div>`;
    }
    h += `<button class="btn primary" id="btnPayNext" ${S.pay ? "" : "disabled"}>Aage badhein &rarr;</button>`;
    h += '<button class="btn ghost" id="btnBack3">&larr; Wapas</button>';
    h += dots(4, 5);
  }

  else if (S.step === "paydetail") {
    h += '<span class="step-badge">Step 5 of 5</span>';
    if (S.pay === "bank") {
      sub.textContent = "Bank transfer";
      const b = (S.cfg && S.cfg.bank) || {};
      if (!b.configured) {
        h += '<div class="err">Bank details abhi set nahi hain. Kripya baad mein try karein ya RM se sampark karein.</div>';
      } else {
        h += '<div class="bank-box">';
        h += `<div class="bank-row"><span class="bk">Beneficiary</span><span class="bv">${esc(b.beneficiary)}</span></div>`;
        h += `<div class="bank-row"><span class="bk">A/C No</span><span class="bv mono">${esc(b.accountNumber)}<button class="bank-copy" data-copy="${esc(b.accountNumber)}">Copy</button></span></div>`;
        h += `<div class="bank-row"><span class="bk">IFSC</span><span class="bv mono">${esc(b.ifsc)}<button class="bank-copy" data-copy="${esc(b.ifsc)}">Copy</button></span></div>`;
        if (b.branch) h += `<div class="bank-row"><span class="bk">Bank</span><span class="bv small">${esc(b.branch)}</span></div>`;
        h += `<div class="bank-row"><span class="bk">Amount</span><span class="bv"><b>${money(S.price)}</b></span></div>`;
        h += "</div>";
      }
      h += '<p class="field-label">Payment ke baad UTR number daalein</p>';
      h += '<input class="field-input" id="utrInput" placeholder="jaise: 3124589671234567" />';
      if (S.err) h += `<div class="err">${esc(S.err)}</div>`;
      h += `<button class="btn primary" id="btnSubmit" ${b.configured ? "" : "disabled"}>Order confirm karein</button>`;
    } else if (S.pay === "qr") {
      sub.textContent = "QR code se payment";
      const q = (S.cfg && S.cfg.qr) || {};
      if (q.image) {
        h += `<div style="text-align:center;margin-bottom:14px"><img src="${q.image}" alt="Payment QR" style="width:170px;height:170px;border:1px solid var(--line);border-radius:10px;padding:8px;background:#fff" /></div>`;
      }
      h += '<div class="amt-callout"><div class="ac-t">UPI app mein <b>ye amount</b> daalein</div>';
      h += `<div class="ac-v"><span>${money(S.price)}</span><button class="ac-copy" id="btnCopyAmt">Copy</button></div>`;
      h += '<div class="ac-w">Exactly yahi amount - kam ya zyada hua to order verify hone mein deri hogi</div></div>';
      h += '<p class="field-label">Payment ke baad UTR / reference daalein</p>';
      h += '<input class="field-input" id="utrInput" placeholder="UPI reference number" />';
      if (S.err) h += `<div class="err">${esc(S.err)}</div>`;
      h += '<button class="btn primary" id="btnSubmit">Order confirm karein</button>';
    }
    h += '<button class="btn ghost" id="btnBack4">&larr; Doosra tarika chunein</button>';
    h += dots(4, 5);
  }

  else if (S.step === "success") {
    sub.textContent = "Order Received";
    h += '<div class="success"><div class="tick">&#10003;</div><h3>Order confirm ho gaya</h3>';
    h += `<p>${esc(S.csp.name)} ji, aapka order darj ho gaya hai (${esc(S.orderId)}). Payment verify hote hi aapko update mil jayega.</p>`;
    // Repeat the address back - the last chance for a CSP to notice they
    // mistyped where a device is about to be shipped.
    h += '<div class="bank-box" style="text-align:left;margin-top:16px">';
    h += `<div class="bank-row"><span class="bk">Delivery</span><span class="bv small">${esc(S.addr.line)}${S.addr.landmark ? ", " + esc(S.addr.landmark) : ""}, ${esc(S.addr.city)}, ${esc(S.addr.state)} - ${esc(S.addr.pincode)}</span></div>`;
    h += "</div></div>";
  }

  body.innerHTML = h;
  wire();
}

function wire() {
  const on = (id, evt, fn) => { const el = document.getElementById(id); if (el) el.addEventListener(evt, fn); };

  on("idInput", "keydown", (e) => { if (e.key === "Enter") doSearch(); });
  on("btnSearch", "click", doSearch);

  on("l4Input", "keydown", (e) => { if (e.key === "Enter") doLast4(); });
  on("btnLast4", "click", doLast4);
  on("btnBack1", "click", () => goIdentify());

  on("btnRetry", "click", () => goIdentify());

  on("btnConfirmYes", "click", () => { S.step = "product"; render(); });

  on("btnProductNext", "click", () => {
    if (!S.product) return;
    // Pre-select the CSP's own state the first time through, but only if
    // it normalised to one of the known options - see public_profile's
    // stateNormalised. Never overwrite a choice they already made.
    if (!S.addr.state && S.csp && S.csp.stateNormalised) S.addr.state = S.csp.stateNormalised;
    S.step = "address"; S.err = null; render();
  });
  on("btnBack2", "click", () => { S.step = "confirm"; render(); });

  on("btnAddrNext", "click", doAddressNext);
  on("btnBackAddr", "click", () => { captureAddress(); S.step = "product"; S.err = null; render(); });

  on("btnPayNext", "click", () => { if (S.pay) { S.step = "paydetail"; S.err = null; render(); } });
  on("btnBack3", "click", () => { S.step = "address"; S.err = null; render(); });
  on("btnBack4", "click", () => { S.step = "payment"; S.err = null; render(); });

  const copyTo = (btn, text) => {
    const done = () => { btn.textContent = "Copied"; setTimeout(() => { btn.textContent = "Copy"; }, 1400); };
    if (navigator.clipboard) navigator.clipboard.writeText(String(text)).then(done, done);
    else done();
  };
  on("btnCopyAmt", "click", (e) => copyTo(e.target, S.price));
  // Account number and IFSC are the two things a CSP has to get exactly
  // right in their banking app - typing either by hand off a phone screen
  // is where a payment goes to the wrong place.
  document.querySelectorAll(".bank-copy").forEach((el) => {
    el.addEventListener("click", () => copyTo(el, el.getAttribute("data-copy")));
  });

  on("btnSubmit", "click", doSubmit);

  document.querySelectorAll(".product-opt[data-pid]").forEach((el) => {
    el.addEventListener("click", () => {
      const pid = el.getAttribute("data-pid");
      S.product = pid;
      S.price = (S.cfg.products[pid] && S.cfg.products[pid].price) || S.csp.eligibility[pid].price;
      S.pay = null;
      render();
    });
  });
  document.querySelectorAll(".pay-opt[data-pay]").forEach((el) => {
    el.addEventListener("click", () => { S.pay = el.getAttribute("data-pay"); render(); });
  });
}

/** Reads whatever is currently in the address inputs into S.addr, so a
 *  re-render (validation error, going back a step) never loses it. */
function captureAddress() {
  const v = (id) => { const el = document.getElementById(id); return el ? el.value.trim() : ""; };
  if (!document.getElementById("addrLine")) return;   // not on that screen
  S.addr = {
    line: v("addrLine"), landmark: v("addrLandmark"), city: v("addrCity"),
    pincode: v("addrPincode"), state: v("addrState"),
  };
}

function doAddressNext() {
  captureAddress();
  S.err = null;
  const a = S.addr;
  // Client-side for a fast, friendly message only - orderpage.submit()
  // re-validates all of this server-side, which is the real gate.
  if (!a.line) S.err = "Address zaroori hai.";
  else if (!a.city) S.err = "City / town zaroori hai.";
  else if (!/^\d{6}$/.test(a.pincode)) S.err = "Pincode exactly 6 digits ka hona chahiye.";
  else if (!a.state) S.err = "State chunein.";
  if (S.err) { render(); return; }
  S.step = "payment";
  render();
}

function goIdentify() {
  S = { step: "identify", csp: null, product: null, price: null, pay: null,
        err: null, cfg: S.cfg, suggestedProductId: null,
        addr: { line: "", landmark: "", city: "", pincode: "", state: "" } };
  render();
}

async function doSearch() {
  const input = document.getElementById("idInput");
  const q = (input && input.value || "").trim();
  S.err = null;
  if (!q) { S.err = "Kuch to daaliye - CSP code ya naam."; render(); return; }

  try {
    const { body } = await api(`/lookup?q=${encodeURIComponent(q)}`);
    handleLookupResult(body);
  } catch (e) {
    S.err = e.message;
    render();
  }
}

async function doLast4() {
  const input = document.getElementById("l4Input");
  const v = (input && input.value || "").trim();
  S.err = null;
  if (!/^\d{4}$/.test(v)) { S.err = "Chaar digits daalein."; render(); return; }

  try {
    const { body } = await api(`/lookup?q=${encodeURIComponent(S.lastQuery)}&last4=${encodeURIComponent(v)}`);
    handleLookupResult(body);
  } catch (e) {
    S.err = e.message;
    render();
  }
}

function handleLookupResult(body) {
  if (body.found === "multiple") {
    S.lastQuery = document.getElementById("idInput") ? document.getElementById("idInput").value.trim() : S.lastQuery;
    S.count = body.count;
    S.step = "last4";
    render();
    return;
  }
  if (body.found === false) {
    S.step = body.needsRm ? "rm" : "notfound";
    render();
    return;
  }
  S.csp = body.csp;
  S.product = null;
  S.step = "confirm";
  render();
}

async function doSubmit() {
  const input = document.getElementById("utrInput");
  const utr = (input && input.value || "").trim();
  S.err = null;
  if (!utr) { S.err = "UTR / reference number zaroori hai."; render(); return; }

  const btn = document.getElementById("btnSubmit");
  if (btn) { btn.disabled = true; btn.textContent = "Confirm ho raha hai..."; }

  try {
    const { status, body } = await api("/submit", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        cspCode: S.csp.cspCode, productId: S.product,
        paymentMethod: S.pay, utr,
        addressLine: S.addr.line, landmark: S.addr.landmark,
        city: S.addr.city, pincode: S.addr.pincode,
        deliveryState: S.addr.state,
      }),
    });
    if (status !== 200) {
      S.err = body.error || "Order confirm nahi ho paaya.";
      render();
      return;
    }
    S.orderId = body.orderId;
    S.step = "success";
    render();
  } catch (e) {
    S.err = e.message;
    render();
  }
}

async function init() {
  try {
    const { body: cfg } = await api("/config");
    S.cfg = cfg;
  } catch (e) {
    S.cfg = { bank: { configured: false }, qr: { enabled: false }, products: {} };
  }

  const params = new URLSearchParams(window.location.search);
  const token = params.get("t");

  if (token) {
    try {
      const { body } = await api(`/resolve-token?t=${encodeURIComponent(token)}`);
      if (body.found && body.alreadyUsed) {
        S.step = "alreadyOrdered";
        S.csp = body.csp;
        S.order = body.order;
        render();
        return;
      }
      if (body.found) {
        S.csp = body.csp;
        S.suggestedProductId = body.suggestedProductId;
        S.product = body.suggestedProductId && S.csp.eligibility[body.suggestedProductId] &&
                    S.csp.eligibility[body.suggestedProductId].orderable ? body.suggestedProductId : null;
        if (S.product) {
          S.price = (S.cfg.products[S.product] && S.cfg.products[S.product].price) ||
                     S.csp.eligibility[S.product].price;
        }
        S.step = "confirm";
        render();
        return;
      }
    } catch (e) {
      // fall through to the normal search screen
    }
  }

  goIdentify();
}

init();
