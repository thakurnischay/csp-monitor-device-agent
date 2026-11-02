let overview = null;
let activeQueue = "PRINTER";

const PAGE = {
  overview:  { title: "Overview",            sub: "Where the programme is against target" },
  targets:   { title: "Target queues",      sub: "Ranked, capped, and safe to contact" },
  disputed:  { title: "Data reconciliation", sub: "Records the platform will not act on until a human confirms" },
  orders:    { title: "Orders",              sub: "Submission to delivery - ownership updates only on delivery" },
  segments:  { title: "Segments & RMs",      sub: "Who can pay, and who is carrying the work" },
  messages:  { title: "Messages",            sub: "Per-CSP delivery, open, and click status - every channel" },
  activity:  { title: "Activity",            sub: "Messages sent and decisions recorded" },
  directory: { title: "CSP Directory",       sub: "Every CSP in the base, active or not" },
};

let orderFilter = "";

const PRODUCT_LABEL = { PRINTER: "Passbook Printer", MATM: "Micro ATM" };

// Matches app/config.py's Product.latent_unit + the "no device" copy in
// emailer.compose() - the Signal column was hardcoded to printer wording
// even on the mATM tab until this existed.
const LATENT_SIGNAL = {
  PRINTER: { some: "prints, no printer", none: "no print history" },
  MATM:    { some: "mATM txns, no device", none: "no mATM activity" },
};

const rs = (n) =>
  n === null || n === undefined ? "—" : "₹" + Number(n).toLocaleString("en-IN", { maximumFractionDigits: 0 });

const lakh = (n) => (n >= 100000 ? "₹" + (n / 100000).toFixed(2) + " L" : rs(n));

// Served inside the Device Monitor: every POST carries the console session's CSRF token, which the server writes
// into this page's <meta name="csrf-token"> (see growth_platform/routes.py index()).
const CSRF_TOKEN = (document.querySelector('meta[name="csrf-token"]') || {}).content || "";

async function api(path, opts) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json", "X-CSRF-Token": CSRF_TOKEN },
    ...opts,
  });
  if (res.status === 401) {
    location.reload();               // console session expired: the reload lands on the login page
    throw new Error("Session expired - please log in again.");
  }
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.error || res.statusText);
  return body;
}

function toast(msg) {
  const el = document.getElementById("toast");
  el.textContent = msg;
  el.classList.remove("hidden");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => el.classList.add("hidden"), 3600);
}

// ------------------------------------------------------------------ loading
let emailStatus = null;
let sendPlan = null;

async function load() {
  overview = await api("api/overview");
  renderSource();
  renderBlocker();
  renderCoverage();
  renderQueue();
  renderSegments();
  renderOrderStats();
  renderFormSetupAlert();
  renderBadges();
  await Promise.all([loadConfirmStatus(), loadLogs(), loadEmailStatus(), loadOrders()]);
  // After loadEmailStatus, not alongside it - this reads emailStatus, which
  // loadEmailStatus is the one call that sets.
  await loadSendCard();
}

/** Sets the mode pill and the detailed table's activeQueue-scoped sendPlan
 * (used only by the collapsed "Full ranked list" table's Send status
 * column now - the actual send action lives in loadSendCard below). */
async function loadEmailStatus() {
  [emailStatus, sendPlan] = await Promise.all([
    api("api/email/status"),
    api(`api/outreach/${activeQueue}/plan`),
  ]);

  const mode = (emailStatus.MODE || "dry").toLowerCase();
  const pill = document.getElementById("modePill");
  pill.className = "modepill " + mode;
  pill.textContent = mode === "dry" ? "Dry run" : "Live";

  // sendPlan just landed (it loads after the queue table's first paint) -
  // re-render so each row's Send status column reflects it.
  renderQueue();
}

/** The one place manual sending happens: both products side by side, each
 * showing exactly who (name + email) the next click would reach, with its
 * own Send button right next to that list - no tab-switching, no reading a
 * detail table to work out who's in the batch. */
async function loadSendCard() {
  const [matmPlan, printerPlan] = await Promise.all([
    api("api/outreach/MATM/plan"),
    api("api/outreach/PRINTER/plan"),
  ]);

  const mode = (emailStatus?.MODE || "dry").toLowerCase();
  const schedule = emailStatus?.outreachSchedule;
  const scheduleNote = schedule
    ? (schedule.enabled
        ? ` Automatic sending is <b>ON</b> — this also goes out on its own in batches of up to ${schedule.batchSizePerProduct ?? 50} per product, starting ${String(schedule.sendAtHour ?? 10).padStart(2, "0")}:00 (after the ${String(schedule.sheetRefreshAtHour ?? 9).padStart(2, "0")}:00 sheet refresh), every ${schedule.batchIntervalHours ?? 2}h until ${String(schedule.lastBatchHour ?? 20).padStart(2, "0")}:00.`
        : ` Automatic sending is <b>OFF</b> — nothing goes out unless you click Send.`)
    : "";
  document.getElementById("sendCardNote").innerHTML =
    (mode === "live"
      ? `<b style="color:#c0264d">LIVE — Send reaches real CSPs.</b>`
      : `Dry run — Send composes and reports, nothing leaves this machine.`)
    + scheduleNote;

  const column = (productId, label, plan) => {
    const held = plan.heldByKind || {};
    const heldParts = [
      held.NO_EMAIL ? `${held.NO_EMAIL} no email on file` : null,
      held.TOO_SOON ? `${held.TOO_SOON} waiting on their next email` : null,
      held.BETWEEN_CYCLES ? `${held.BETWEEN_CYCLES} between cycles` : null,
      held.SEQUENCE_EXHAUSTED ? `${held.SEQUENCE_EXHAUSTED} finished all cycles` : null,
    ].filter(Boolean).join(" · ");

    return `
    <div>
      <div class="card-head" style="padding:0 0 10px 0">
        <div><h2 style="font-size:14px;margin:0">${label}</h2>
        <p class="sub" style="margin:2px 0 0">Next <b>${plan.batch.length}</b> of ${plan.sendable} eligible
          ${heldParts ? ` · held: ${heldParts}` : ""}</p></div>
      </div>
      ${plan.batch.length
        ? `<ul class="feed" style="max-height:280px;overflow-y:auto">${plan.batch.map((t, i) => `<li><b>${i + 1}. ${t.name}</b>
            <span class="tag" style="margin-left:6px">${t.cycle > 1 ? `Cycle ${t.cycle} · ` : ""}Email ${t.touch}</span>
            <a href="#" style="margin-left:6px;font-size:11.5px;color:var(--brand-deep);text-decoration:none;border-bottom:1px dotted var(--brand-deep)"
               onclick="event.preventDefault(); previewProduct('${productId}', '${t.cspCode}', ${t.touch})"
               title="See the exact email this CSP will get">Preview</a>
            <div class="t">${t.email || "no email"} · ${t.cspCode}</div></li>`).join("")}</ul>`
        : `<div class="empty"><div class="small">Nobody eligible right now.</div></div>`}
      <div style="display:flex;gap:8px;padding:14px 0 2px">
        <button class="btn ghost" onclick="previewProduct('${productId}')">Preview email</button>
        <button class="btn primary" ${plan.batch.length ? "" : "disabled"}
          onclick="sendOutreach('${productId}', this)">Send to ${plan.batch.length} CSPs</button>
      </div>
    </div>`;
  };

  document.getElementById("sendGrid").innerHTML =
    column("MATM", "Micro ATM", matmPlan) + column("PRINTER", "Passbook Printer", printerPlan);
}

/** cspCode/touch are optional - the bottom "Preview email" button omits both
 * and gets whichever CSP is next up (see run.py's email_preview). Each
 * batch row's own "Preview" link passes both explicitly, so it shows THAT
 * CSP's actual email at THAT touch, not just "someone in this batch". */
function previewProduct(productId, cspCode, touch) {
  const path = cspCode
    ? `api/email/preview/${productId}/${encodeURIComponent(cspCode)}`
    : `api/email/preview/${productId}`;
  const params = new URLSearchParams({ format: "html" });
  if (touch) params.set("touch", touch);
  window.open(`${path}?${params}`, "_blank");
}

/** Sends one product's next batch. Re-fetches its own plan rather than
 * trusting the one loadSendCard already rendered, so a stale click (plan
 * changed since page load) can't send to the wrong count silently. */
async function sendOutreach(productId, btn) {
  const mode = (emailStatus?.MODE || "dry").toLowerCase();
  const plan = await api(`api/outreach/${productId}/plan`);

  if (mode === "live") {
    const ok = confirm(
      `Send REAL ${PRODUCT_LABEL[productId]} email to ${plan.willSend} CSPs?\n\n` +
      `From: ${emailStatus.FROM}\n` +
      `This cannot be undone.`
    );
    if (!ok) return;
  }

  btn.disabled = true;
  try {
    const r = await api(`api/outreach/${productId}`, {
      method: "POST",
      body: JSON.stringify({ confirmLive: mode === "live" }),
    });
    toast(
      r.mode === "dry"
        ? `Dry run: ${r.composed} composed, nothing sent · ${r.held} held`
        : `Live: sent ${r.sent} · failed ${r.failed} · held ${r.held}`
    );
    await load();
  } catch (err) { toast(err.message); }
  btn.disabled = false;
}

function renderSource() {
  const s = overview.source;
  // Was printing the last path segment of the source, which for a Google
  // Sheets URL is the query string ("edit?gid=1807123739") - it read like
  // debug output leaking into the UI. Name the source, link to it, and say
  // when it was last pulled.
  const isUrl = /^https?:/i.test(s.file || "");
  const label = isUrl
    ? `<a href="${s.file}" target="_blank" rel="noopener">Calling Sheet</a>`
    : (s.file || "").split(/[\\/]/).pop();
  const when = new Date(s.importedAt);
  const mins = Math.round((Date.now() - when.getTime()) / 60000);
  const ago = mins < 1 ? "just now"
    : mins < 60 ? `${mins} min ago`
    : mins < 1440 ? `${Math.round(mins / 60)} hr ago`
    : `${Math.round(mins / 1440)} d ago`;

  document.getElementById("sourceLine").innerHTML =
    `<b>${s.cspCount.toLocaleString("en-IN")}</b> CSPs from ${label}<br/>
     <span title="${when.toLocaleString()}">Synced ${ago}</span>`;
}

function renderBadges() {
  const t = overview.queues.PRINTER.targetedCount + overview.queues.MATM.targetedCount;
  document.getElementById("badgeTargets").textContent = t || "";
  document.getElementById("badgeDisputed").textContent = overview.dataQuality.matmDisputedGap || "";
  document.getElementById("badgeOrders").textContent = overview.orders.byStatus.SUBMITTED || "";
  document.getElementById("badgeMessages").textContent = overview.outreachCount || "";
}

function renderFormSetupAlert() {
  const el = document.getElementById("formSetupAlert");
  const missing = overview.formSetup.missingFields;
  if (!missing.length && overview.formSetup.responseSheetConfigured) {
    el.classList.add("hidden");
    return;
  }
  el.classList.remove("hidden");
  el.className = "alert";
  el.innerHTML = `<span class="dot"></span><div>
    <b>Form setup incomplete.</b>
    ${missing.length ? `The live form is still missing: <b>${missing.join(", ")}</b> - see GOOGLE_FORM_SETUP.md.` : ""}
    ${overview.formSetup.responseSheetConfigured ? "" : " No response sheet is configured yet (config.RESPONSE_SHEET_URL) - use “Import responses” to feed submissions in manually until then."}
  </div>`;
}

// The platform's most important statement: it will not invent targets.
function renderBlocker() {
  const dq = overview.dataQuality;
  const el = document.getElementById("blocker");
  el.classList.remove("hidden");

  if (dq.matmDisputedGap === 0) {
    el.className = "alert ok";
    el.innerHTML =
      `<span class="dot"></span><div><b>Data is clean.</b> Every ownership record agrees with its
       transaction evidence — all ${overview.queues.MATM.targetedCount + overview.queues.PRINTER.targetedCount}
       targets are safe to contact.</div>`;
    return;
  }

  el.className = "alert";
  el.innerHTML =
    `<span class="dot"></span><div><b>${dq.matmDisputedGap} Micro ATM targets are on hold.</b>
     They are marked “No mATM” in the sheet, yet their own transaction records show mATM activity — so the
     platform will not put them in a campaign. Of the ${dq.matmFlaggedGap} CSPs flagged as needing an mATM,
     only <b>${dq.matmVerifiedGap}</b> are backed by evidence. Resolve them under <b>Reconciliation</b> to
     release the Micro ATM programme. The Passbook Printer programme is unaffected.</div>`;
}

/** Progress ring with a tick marking the target percentage. */
function ring(pct, targetPct) {
  const R = 40, C = 2 * Math.PI * R;
  const dash = C * Math.min(pct, 100) / 100;
  const angle = (Math.min(targetPct, 100) / 100) * 360 - 90;
  const rad = (angle * Math.PI) / 180;
  const x1 = 50 + Math.cos(rad) * (R - 8), y1 = 50 + Math.sin(rad) * (R - 8);
  const x2 = 50 + Math.cos(rad) * (R + 8), y2 = 50 + Math.sin(rad) * (R + 8);
  return `<svg class="ring" viewBox="0 0 100 100">
    <circle class="track" cx="50" cy="50" r="${R}" stroke-width="9"/>
    <circle class="val" cx="50" cy="50" r="${R}" stroke-width="9"
      stroke-dasharray="${dash} ${C - dash}" transform="rotate(-90 50 50)"/>
    <line class="tick" x1="${x1}" y1="${y1}" x2="${x2}" y2="${y2}" stroke="currentColor"/>
    <text x="50" y="50">${pct}%</text>
    <text x="50" y="63" class="unit">TARGET ${targetPct}%</text>
  </svg>`;
}

const PRODUCT_ACCENT = { MATM: "accent-matm", PRINTER: "accent-printer" };

function renderCoverage() {
  const cards = Object.values(overview.coverage).map((c) => `
    <div class="stat ${PRODUCT_ACCENT[c.productId] || ""}">
      ${ring(c.coveragePct, c.targetPct)}
      <div class="stat-body">
        <div class="stat-label">${c.productName}</div>
        <div class="stat-main">${c.owned} <span style="font-size:14px;color:var(--ink-3);font-weight:600">/ ${c.activeBase} active</span></div>
        <div class="stat-note">
          <b>${c.remaining}</b> more to reach target
          ${c.idleDevices ? `<span class="chip">${c.idleDevices} idle</span>` : ""}
        </div>
      </div>
    </div>`);

  const s = overview.summary;
  cards.push(`
    <div class="stat accent-revenue">
      <div class="stat-body">
        <div class="stat-label">Programme value in play</div>
        <div class="stat-main">${lakh(s.revenueAtStake)}</div>
        <div class="stat-note">
          <b>${overview.queues.PRINTER.targetedCount}</b> printers ·
          <b>${overview.queues.MATM.targetedCount}</b> micro ATMs queued<br/>
          Base: ${s.byStatus.ACTIVE || 0} active · ${s.byStatus.DORMANT || 0} dormant · ${s.byStatus.INACTIVE || 0} inactive
        </div>
      </div>
    </div>`);

  document.getElementById("coverage").innerHTML = cards.join("");
}

function renderQueue() {
  const q = overview.queues[activeQueue];
  const note = document.getElementById("queueNote");
  const tbody = document.querySelector("#queueTable tbody");

  if (!q.targeted.length) {
    note.textContent = "";
    tbody.innerHTML = `<tr><td colspan="10"><div class="empty">
      <div class="big">Nothing queued — by design</div>
      <div class="small">${
        activeQueue === "MATM"
          ? `${overview.dataQuality.matmDisputedGap} candidates are held for reconciliation, and the rest are
             dormant or blocked by affordability rules. No message will be sent until the ownership data is settled.`
          : "No CSPs currently meet the eligibility rules for this product."
      }</div></div></td></tr>`;
    return;
  }

  note.innerHTML =
    `Showing <b>${q.targeted.length}</b> of ${q.candidatePool} eligible candidates — capped at the
     <b>${q.needed}</b> needed to hit target. ${q.deferred} ranked below the cap are held for the next cycle.`;

  // Send status per row, sourced from the same plan the send button and mode
  // banner use - so "who gets an email" is answerable by reading this table,
  // not by trusting the aggregate held-count in the banner above it.
  const planMatches = sendPlan && sendPlan.productId === activeQueue;
  const batchByCode = new Map();
  const heldByCode = new Map();
  if (planMatches) {
    sendPlan.batch.forEach((t) => batchByCode.set(t.cspCode, t));
    sendPlan.held.forEach((h) => heldByCode.set(h.cspCode, h));
  }

  tbody.innerHTML = q.targeted.map((t, i) => {
    const sig = LATENT_SIGNAL[activeQueue] || LATENT_SIGNAL.PRINTER;
    const signal = t.latentDemand > 0
      ? `<span class="tag hot">${t.latentDemand} ${sig.some}</span>`
      : `<span class="tag quiet">${sig.none}</span>`;
    const months = t.monthsOfCommission
      ? `<div class="dim">${t.monthsOfCommission} mo of commission</div>` : "";

    let sendStatus = `<span class="dim">—</span>`;
    if (planMatches) {
      if (batchByCode.has(t.cspCode)) {
        sendStatus = `<span class="tag send-next">Next batch</span>`;
      } else if (heldByCode.has(t.cspCode)) {
        const h = heldByCode.get(t.cspCode);
        sendStatus = `<span class="tag send-held" title="${h.reason}">Held</span>
          <div class="dim" style="max-width:180px">${h.reason}</div>`;
      } else {
        sendStatus = `<span class="tag send-queued">Eligible — next run</span>`;
      }
    }

    return `<tr>
      <td><div class="rank${i < 3 ? " top" : ""}">${i + 1}</div></td>
      <td><div class="who">${t.name}</div><div class="dim">${t.cspCode}</div></td>
      <td>${t.rm}</td>
      <td>${t.state || "—"}<div class="dim">${t.district || ""}</div></td>
      <td class="r">${rs(t.commission)}</td>
      <td class="r">${t.transactions.toLocaleString("en-IN")}</td>
      <td>${signal}</td>
      <td><span class="tag ${t.band}">${t.band}</span>${months}</td>
      <td>${sendStatus}</td>
      <td><button class="mini" onclick="previewFor('${activeQueue}','${t.cspCode}')">Preview</button></td>
    </tr>`;
  }).join("");
}

/** Opens the exact email one specific CSP would get - same endpoint the
 * header's generic "Preview email" button uses, just scoped to a CSP code
 * instead of showing whichever candidate happens to be first in the queue. */
function previewFor(productId, cspCode) {
  window.open(`api/email/preview/${productId}/${cspCode}?format=html`, "_blank");
}

/** The literal email one past send carried, not a fresh regeneration - see
 * run.py's /api/outreach/<id>/message. Distinct from previewFor() above,
 * which recomposes against today's data and only works for a CSP still
 * sitting in the queue; this works for anyone ever contacted. */
function viewMessage(entryId) {
  window.open(`api/outreach/${entryId}/message?format=html`, "_blank");
}

let confirmPlan = null;

/** Mirrors loadEmailStatus() for the disputed list's own send (ownership
 * confirmation, not a campaign) - separate mode pill + banner + button,
 * since it's a different question to a different audience. */
async function loadConfirmStatus() {
  [emailStatus, confirmPlan] = await Promise.all([
    api("api/email/status"),
    api("api/reconcile/plan"),
  ]);

  const mode = (emailStatus.MODE || "dry").toLowerCase();
  const pill = document.getElementById("confirmModePill");
  pill.className = "modepill " + mode;
  pill.textContent = mode === "dry" ? "Dry run" : "Live";

  const el = document.getElementById("confirmAlert");
  el.classList.remove("hidden");
  const held = confirmPlan.heldByKind || {};
  const heldParts = [
    held.NO_EMAIL ? `${held.NO_EMAIL} no valid email` : null,
    held.ALREADY_ASKED ? `${held.ALREADY_ASKED} already asked` : null,
  ].filter(Boolean).join(" · ");

  if (mode === "dry") {
    el.className = "alert";
    el.innerHTML = `<span class="dot"></span><div>
      <b>Dry run — no email will leave this machine.</b> A send would reach
      <b>${confirmPlan.willSend}</b> of ${confirmPlan.queueSize} disputed CSPs
      (${confirmPlan.sendable} eligible, capped at ${confirmPlan.limit} per run)${heldParts ? `; held: ${heldParts}` : ""}.
      This asks them to confirm ownership by replying — it is not a sales email.
    </div>`;
  } else {
    el.className = "alert";
    el.style.background = "#fff1f2"; el.style.borderColor = "#fecdd3"; el.style.color = "#9f1239";
    el.innerHTML = `<span class="dot" style="background:#e11d48"></span><div>
      <b>LIVE — real CSPs will receive this email.</b> A run sends to
      <b>${confirmPlan.willSend}</b> of ${confirmPlan.queueSize} disputed CSPs, from
      ${emailStatus.FROM}${heldParts ? `; held: ${heldParts}` : ""}.
    </div>`;
  }

  await loadDisputed();
}

async function loadDisputed() {
  const d = await api("api/disputed");
  const tbody = document.querySelector("#disputedTable tbody");
  if (!d.count) {
    tbody.innerHTML = `<tr><td colspan="7"><div class="empty">
      <div class="big">Nothing held</div>
      <div class="small">Every ownership record agrees with its transaction evidence.</div>
    </div></td></tr>`;
    return;
  }

  const batchByCode = new Map();
  const heldByCode = new Map();
  if (confirmPlan) {
    confirmPlan.batch.forEach((t) => batchByCode.set(t.cspCode, t));
    confirmPlan.held.forEach((h) => heldByCode.set(h.cspCode, h));
  }

  // The full reasoning is stated once in the panel description above - repeating
  // the same paragraph on 189 rows just buries the number that differs.
  tbody.innerHTML = d.disputed.slice(0, 200).map((c) => {
    let sendStatus = `<span class="dim">—</span>`;
    if (confirmPlan) {
      if (batchByCode.has(c.cspCode)) {
        sendStatus = `<span class="tag send-next">Next batch</span>`;
      } else if (heldByCode.has(c.cspCode)) {
        const h = heldByCode.get(c.cspCode);
        sendStatus = `<span class="tag send-held" title="${h.reason}">Held</span>
          <div class="dim" style="max-width:160px">${h.reason}</div>`;
      } else {
        sendStatus = `<span class="tag send-queued">Eligible — next run</span>`;
      }
    }
    return `<tr>
    <td><div class="who">${c.name}</div><div class="dim">${c.cspCode} · ${c.state || ""}</div></td>
    <td>${c.rm}</td>
    <td class="r">${rs(c.commission)}</td>
    <td class="r"><b>${c.matmTxns.toLocaleString("en-IN")}</b></td>
    <td><span class="tag hot">status says “No”</span></td>
    <td>${sendStatus}</td>
    <td style="white-space:nowrap">
      <button class="mini yes" onclick="reconcile('${c.cspCode}','${c.product}',true)">Has it</button>
      <button class="mini no" onclick="reconcile('${c.cspCode}','${c.product}',false)">Needs it</button>
    </td>
  </tr>`;
  }).join("");
}

function renderSegments() {
  const MEANING = {
    STRONG: "Pays upfront, recovered in 1–2 months",
    MID: "Pays upfront, recovered in 2–3 months",
    LOW: "Show the ROI first — blocked from the ₹38,350 printer",
    REVIVE: "Revive the business first — never pitched hardware",
    UNKNOWN: "No commission data — confirm earnings before pitching",
  };
  document.querySelector("#bandTable tbody").innerHTML = Object.entries(overview.summary.byBand)
    .sort((a, b) => b[1] - a[1])
    .map(([band, n]) => `<tr>
      <td><span class="tag ${band}">${band}</span></td>
      <td class="r"><b>${n}</b></td>
      <td class="dim" style="font-size:12.5px">${MEANING[band] || ""}</td></tr>`).join("");

  const rms = overview.summary.rmWorkload;
  document.querySelector("#rmTable tbody").innerHTML = rms.length
    ? rms.map((r) => `<tr><td class="who">${r.rm}</td>
        <td class="r">${r.MATM}</td><td class="r">${r.PRINTER}</td>
        <td class="r"><b>${r.total}</b></td></tr>`).join("")
    : `<tr><td colspan="4"><div class="empty"><div class="small">No queued targets yet.</div></div></td></tr>`;
}

// ------------------------------------------------------------------ orders
// Nothing here is a required click. SUBMITTED waits on finance typing a
// remark in the response sheet's Finance Remark column (not the dashboard);
// PAYMENT_VERIFIED -> DISPATCHED -> DELIVERED auto-advance on a timer (see
// app/orders.py). These are manual OVERRIDES for the rare exception, shown
// small and secondary; the normal-path cell shows an ETA/status instead of
// a button. Reject is the one action with no automatic equivalent.
const OVERRIDE_ACTIONS = {
  SUBMITTED:        [["verify-payment", "Force verify"]],
  PAYMENT_VERIFIED: [["dispatch", "Force dispatch"]],
  DISPATCHED:       [["deliver", "Force deliver"]],
  DELIVERED:        [],
  REJECTED:         [],
};
const CAN_REJECT = new Set(["SUBMITTED", "PAYMENT_VERIFIED"]);
const STAGE_LABEL = { DISPATCHED: "dispatches", DELIVERED: "delivers" };

// The happy path in order, for the drawer's trail - stages already reached
// come off order.history, the rest of this list renders hollow underneath.
// REJECTED isn't in here on purpose: it's an exit, not a later stage, so a
// rejected order shows no "still to come" at all.
const ORDER_FLOW = ["SUBMITTED", "PAYMENT_VERIFIED", "DISPATCHED", "DELIVERED"];

// The last /api/orders payload, by id - what the drawer renders from, so
// opening one costs no extra request. Refreshed by loadOrders() on every
// poll and after every override, and the open drawer re-renders with it.
let ordersById = {};
let openOrderId = null;

/** Which path this order came in through. paymentMethod is the reliable
 *  discriminator: orderpage.submit() requires it to be "bank" or "qr", and
 *  a Google-Form submission has no such field at all, so it lands null (see
 *  store.create_order's comment on the field). addressLine is a fallback for
 *  the same reason - only the order page collects it onto the order. */
function orderSource(o) {
  return o.paymentMethod || o.addressLine ? "page" : "form";
}

function renderOrderStats() {
  const o = overview.orders;
  const stat = (label, main, note, accent) => `
    <div class="stat ${accent || ""}">
      <div class="stat-body">
        <div class="stat-label">${label}</div>
        <div class="stat-main">${main}</div>
        <div class="stat-note">${note}</div>
      </div>
    </div>`;
  document.getElementById("orderStats").innerHTML = [
    stat("Forms submitted", o.formsSubmitted, `${o.byStatus.SUBMITTED || 0} awaiting finance`, "accent-neutral"),
    stat("Payments verified", o.paymentsVerified, `${o.rejected || 0} rejected`, "accent-matm"),
    stat("Dispatched", o.ordersDispatched, "auto, on a timer", "accent-revenue"),
    stat("Devices delivered", o.devicesDelivered, "confirmed with the CSP - ownership updated", "accent-good"),
    stat("Pending orders", o.pendingOrders, "not yet delivered", "accent-warn"),
  ].join("");
}

/** What to show in the Next column: an ETA for the timer-driven stages, or
 * "waiting on finance" for SUBMITTED - which is the normal state now, not a
 * problem. A genuine issue (finance marked credited but the amount/UTR
 * didn't check out) shows separately as a remark under the status tag. */
function describeNext(o) {
  if (o.eta) {
    const stage = STAGE_LABEL[o.eta.next] || o.eta.next.toLowerCase();
    const when = o.eta.etaDays <= 0 ? "any moment" : `in ~${o.eta.etaDays}d`;
    return `<span class="dim">Auto-${stage} ${when}</span>`;
  }
  if (o.status === "SUBMITTED") {
    return `<span class="dim">Waiting on finance remark</span>`;
  }
  if (o.status === "PAYMENT_VERIFIED" && o.addressFlag) {
    return `<span class="next-flag">Held — check address before dispatch</span>`;
  }
  return `<span class="dim">—</span>`;
}

async function loadOrders() {
  const q = orderFilter ? `?status=${encodeURIComponent(orderFilter)}` : "";
  const d = await api("api/orders" + q);
  // Cache before the early return below, so a filter that hides the open
  // order still leaves the drawer with something to re-render from.
  for (const o of d.orders) ordersById[o.id] = o;
  const tbody = document.querySelector("#ordersTable tbody");
  if (!d.count) {
    tbody.innerHTML = `<tr><td colspan="8"><div class="empty">
      <div class="big">No orders${orderFilter ? " in this status" : " yet"}</div>
      <div class="small">Orders appear here the moment a CSP submits the order form -
        see “Import responses” above, or wait on the live form once it's wired up.</div>
    </div></td></tr>`;
    return;
  }
  tbody.innerHTML = d.orders.map((o) => {
    const overrides = (OVERRIDE_ACTIONS[o.status] || []).map(([action, label]) =>
      `<button class="mini" onclick="orderAction('${o.id}','${action}')">${label}</button>`).join(" ");
    const rejectButton = CAN_REJECT.has(o.status)
      ? `<button class="mini no" onclick="orderAction('${o.id}','reject')">Reject</button>` : "";
    // Which path the order came in through. The only thing added to the row
    // itself - everything else the CSP typed is in the drawer, because the
    // address alone would need four more columns on a table that already
    // scrolls sideways.
    const source = orderSource(o) === "page"
      ? `<span class="ordersrc page">Order page</span>`
      : `<span class="ordersrc form">Form</span>`;
    return `<tr data-order-id="${esc(o.id)}"${o.id === openOrderId ? ' class="selected"' : ""}>
      <td class="who">${o.id}</td>
      <td>${esc(o.name)}${source}<div class="dim">${esc(o.cspCode)}</div></td>
      <td>${o.productId}</td>
      <td class="r">${rs(o.price)}</td>
      <td class="dim">${esc(o.utr || "—")}</td>
      <td><span class="tag ${o.status}">${o.status.replace(/_/g, " ")}</span>
        ${o.remarks ? `<div class="dim" title="${esc(o.remarks)}">${esc(o.remarks.slice(0, 60))}${o.remarks.length > 60 ? "…" : ""}</div>` : ""}
      </td>
      <td>${describeNext(o)}</td>
      <td style="white-space:nowrap">${overrides} ${rejectButton}</td>
    </tr>`;
  }).join("");
}

async function orderAction(orderId, action) {
  const by = prompt("Who is doing this? (your name)");
  if (!by) return;
  let note = "";
  if (action === "reject") note = prompt("Why reject? (optional)") || "";
  try {
    await api(`api/orders/${orderId}/${action}`, { method: "POST", body: JSON.stringify({ by, note }) });
    toast(`${orderId} updated`);
    await load();
    // load() refreshed ordersById - repaint the drawer so an override taken
    // from inside it doesn't leave a stale status sitting on screen.
    if (openOrderId) openOrderDrawer(openOrderId);
  } catch (err) { toast(err.message); }
}

// ------------------------------------------------------- order detail drawer
// Everything a CSP fills in on the self-serve order page (app/orderpage.py)
// is stored on the order and, before this, displayed nowhere: the table's 8
// columns carry none of the address, contact or payment-method fields. With
// config.ORDER_PAGE_SHEET_MIRROR_URL unset there's no response-sheet row to
// read them off either, so an order-page order's shipping address existed
// only inside state.json. No new API: /api/orders already returns every
// field below.

const fmtWhen = (iso) => {
  const d = new Date(iso);
  return isNaN(d) ? esc(iso || "—")
                  : d.toLocaleString("en-IN", { dateStyle: "medium", timeStyle: "short" });
};

/** The shipping address, or an honest account of where it actually lives.
 *  A Google-Form order has addressLine/landmark/city as null - that form
 *  does collect them, but config.RESPONSE_HEADER_MAP never mapped them, so
 *  they only ever reached the response sheet. Rendering em-dashes there
 *  would read as "this CSP gave no address", which is wrong and would send
 *  someone looking for a problem that doesn't exist. */
function drawerDelivery(o) {
  if (!o.addressLine) {
    return `
      <div class="not-captured">
        <b>Full address isn't on this order.</b> It came in through the Google Form, which
        collects Address / Landmark / City into the response sheet only - the platform reads
        just State and Pincode from a form submission. Ops reads the rest off the sheet.
      </div>
      <dl class="kv">
        <dt>State</dt><dd>${esc(o.deliveryState || "—")}</dd>
        <dt>Pincode</dt><dd class="num">${esc(o.pincode || "—")}</dd>
      </dl>`;
  }
  // Held, not merely flagged: orders.auto_advance() refuses to dispatch this
  // one until a human clears it, so say that rather than just marking it.
  const flag = o.addressFlag ? `
    <div class="flagbox"><b>Address held.</b> Pincode ${esc(o.pincode || "")} doesn't match the
    postal zone for ${esc(o.deliveryState || "")}. This order won't auto-dispatch until someone
    clears it with Force dispatch.</div>` : "";
  return `${flag}
    <div class="addr-block" id="dwAddress">
      ${esc(o.addressLine)}${o.landmark ? `<br />${esc(o.landmark)}` : ""}<br />
      ${esc(o.city || "")}, ${esc(o.deliveryState || "")} —
      <span class="pin">${esc(o.pincode || "")}</span>
    </div>
    <button class="mini" onclick="copyOrderAddress('${esc(o.id)}')">Copy address</button>`;
}

function drawerPayment(o) {
  const matches = Math.round(o.amountPaid) === Math.round(o.price);
  const method = o.paymentMethod
    ? (o.paymentMethod === "qr" ? "UPI / static QR" : "Bank transfer")
    : `<span class="sub2">Not asked (Google Form)</span>`;
  return `<dl class="kv">
    <dt>Method</dt><dd>${method}</dd>
    <dt>UTR / ref</dt><dd class="num">${esc(o.utr || "—")}</dd>
    <dt>Amount paid</dt><dd class="num">${rs(o.amountPaid)}
      ${matches ? `<span class="amt-ok">· matches price</span>`
                : `<span class="amt-bad">· price is ${rs(o.price)}</span>`}</dd>
    <dt>Product</dt><dd>${esc(PRODUCT_LABEL[o.productId] || o.productId)} · ${rs(o.price)}</dd>
  </dl>`;
}

/** Reached stages from order.history, then whatever's left of ORDER_FLOW
 *  hollow underneath. The first history entry (written by store.create_order)
 *  carries no `by`, so that's rendered as absent rather than "undefined". */
function drawerTrail(o) {
  const reached = new Set(o.history.map((h) => h.status));
  const done = o.history.map((h) => `<li>
    <b>${h.status.replace(/_/g, " ")}</b>
    <div class="t">${fmtWhen(h.at)}${h.by ? ` · ${esc(h.by)}` : ""}</div>
    ${h.note ? `<div class="t">${esc(h.note)}</div>` : ""}
  </li>`).join("");
  const pending = o.status === "REJECTED" ? "" : ORDER_FLOW
    .filter((s) => !reached.has(s))
    .map((s) => `<li class="pending"><span class="t">${s.replace(/_/g, " ")}</span></li>`)
    .join("");
  return `<ul class="trail">${done}${pending}</ul>`;
}

function openOrderDrawer(orderId) {
  const o = ordersById[orderId];
  if (!o) return;
  openOrderId = orderId;
  document.querySelectorAll("#ordersTable tbody tr").forEach((tr) =>
    tr.classList.toggle("selected", tr.dataset.orderId === orderId));

  const source = orderSource(o) === "page"
    ? `<span class="ordersrc page">Order page</span>`
    : `<span class="ordersrc form">Form</span>`;
  const footActions = (OVERRIDE_ACTIONS[o.status] || []).map(([action, label]) =>
    `<button class="mini" onclick="orderAction('${esc(o.id)}','${action}')">${label}</button>`).join(" ")
    + (CAN_REJECT.has(o.status)
        ? ` <button class="mini no" onclick="orderAction('${esc(o.id)}','reject')">Reject</button>` : "");

  document.getElementById("orderDrawer").innerHTML = `
    <div class="dw-head">
      <div class="row1">
        <h3>${esc(o.id)}</h3>
        <button class="dw-close" onclick="closeOrderDrawer()" aria-label="Close">×</button>
      </div>
      <div class="name">${esc(o.name)}${source}</div>
      <div class="dim">${esc(o.cspCode)} · submitted ${fmtWhen(o.createdAt)}</div>
      <div style="margin-top:10px">
        <span class="tag ${o.status}">${o.status.replace(/_/g, " ")}</span>
      </div>
      ${o.remarks ? `<div class="dim" style="margin-top:8px">${esc(o.remarks)}</div>` : ""}
    </div>
    <div class="dw-body">
      <div class="dw-sec"><h4>Delivery</h4>${drawerDelivery(o)}</div>
      <div class="dw-sec"><h4>Contact</h4>
        <dl class="kv">
          <dt>Mobile</dt><dd class="num">${esc(o.mobile || "—")}</dd>
          <dt>Email</dt><dd>${esc(o.email || "—")}</dd>
        </dl>
        <div class="dim">From the CSP master record - every order status email goes here.</div>
      </div>
      <div class="dw-sec"><h4>Payment</h4>${drawerPayment(o)}</div>
      <div class="dw-sec"><h4>Trail</h4>
        ${drawerTrail(o)}
        <dl class="kv" style="margin-top:14px">
          ${o.courier || o.trackingNumber ? `
            <dt>Courier</dt><dd>${esc(o.courier || "not resolved yet")}</dd>
            <dt>Tracking</dt><dd class="num">${esc(o.trackingNumber || "—")}</dd>` : ""}
          <dt>Token</dt><dd class="num">${esc(o.token || "—")}</dd>
        </dl>
      </div>
    </div>
    <div class="dw-foot">${footActions || `<span class="dim">No actions - this order is finished.</span>`}</div>`;

  const drawer = document.getElementById("orderDrawer");
  drawer.classList.add("open");
  drawer.setAttribute("aria-hidden", "false");
  document.getElementById("orderScrim").classList.add("open");
}

function closeOrderDrawer() {
  openOrderId = null;
  const drawer = document.getElementById("orderDrawer");
  drawer.classList.remove("open");
  drawer.setAttribute("aria-hidden", "true");
  document.getElementById("orderScrim").classList.remove("open");
  document.querySelectorAll("#ordersTable tbody tr").forEach((tr) => tr.classList.remove("selected"));
}

/** One copy action, not five: ops pastes this straight into a courier
 *  system. Reads the order rather than the rendered block so the copied text
 *  has real newlines instead of the block's <br />s. */
async function copyOrderAddress(orderId) {
  const o = ordersById[orderId];
  if (!o) return;
  const text = [o.addressLine, o.landmark,
                `${o.city || ""}, ${o.deliveryState || ""} - ${o.pincode || ""}`]
    .filter(Boolean).join("\n");
  try {
    await navigator.clipboard.writeText(text);
    toast("Address copied");
  } catch {
    // Clipboard API needs a secure context - it's absent over plain http,
    // which is exactly how this dashboard is served on the rack today.
    toast("Couldn't copy - select the address and copy it by hand");
  }
}

// Delegated so it survives every loadOrders() repaint. Clicks on the row's
// own override buttons are left alone - those already do their own thing and
// shouldn't also open the drawer.
document.querySelector("#ordersTable tbody").addEventListener("click", (e) => {
  if (e.target.closest("button")) return;
  const tr = e.target.closest("tr[data-order-id]");
  if (tr) openOrderDrawer(tr.dataset.orderId);
});
document.getElementById("orderScrim").addEventListener("click", closeOrderDrawer);
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && openOrderId) closeOrderDrawer();
});

document.getElementById("importResponsesBtn").addEventListener("click", async () => {
  const sheetUrl = prompt(
    "Paste the form's response spreadsheet URL to import from " +
    "(leave blank to use config.RESPONSE_SHEET_URL if it's set):"
  );
  if (sheetUrl === null) return;
  try {
    const body = sheetUrl.trim() ? { sheetUrl: sheetUrl.trim() } : {};
    const r = await api("api/form/responses/import", { method: "POST", body: JSON.stringify(body) });
    toast(`Imported ${r.received}: ${r.created} order(s) created - ${r.skippedAlreadyProcessed} already processed`);
    await load();
  } catch (err) { toast(err.message); }
});

document.querySelectorAll("#orderFilter .segbtn").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll("#orderFilter .segbtn").forEach((b) => b.classList.remove("active"));
    btn.classList.add("active");
    orderFilter = btn.dataset.status;
    loadOrders();
  });
});

async function loadLogs() {
  const [o, a] = await Promise.all([api("api/outreach"), api("api/audit")]);
  document.getElementById("outreachLog").innerHTML = o.recent.length
    ? o.recent.map((e) => `<li><b>${e.name}</b> · ${e.channel}
        ${e.kind === "CAMPAIGN" && e.touch ? ` · ${e.cycle > 1 ? `cycle ${e.cycle}, ` : ""}email ${e.touch} of 8` : ""}
        ${e.delivered ? "" : "<span style='color:var(--bad)'>· failed</span>"}
        ${e.openedAt ? `<span style="color:var(--good)">· opened ${new Date(e.openedAt).toLocaleString()}</span>` : ""}
        ${e.hasMessage ? ` · <a href="#" onclick="viewMessage(${e.id});return false">View message</a>` : ""}
        <div class="t">${e.cspCode}${e.subject ? " · " + e.subject : ""} · ${new Date(e.at).toLocaleString()}</div></li>`).join("")
    : `<li class="t">No messages sent yet.</li>`;
  document.getElementById("auditLog").innerHTML = a.recent.length
    ? a.recent.map((e) => `<li><b>${e.event.replace(/_/g, " ").toLowerCase()}</b>
        <div class="t">${new Date(e.at).toLocaleString()} · ${JSON.stringify(e.details)}</div></li>`).join("")
    : `<li class="t">Nothing logged yet.</li>`;
}

// ------------------------------------------------------------------ messages
// Mirrors config.CAMPAIGN_SEND_DAY_OFFSETS - display only, same convention as
// PRODUCT_LABEL/LATENT_SIGNAL above (small hardcoded mirrors of backend
// config purely for showing a label, never used to make a decision).
const CAMPAIGN_DAY_OFFSETS = [1, 3, 6, 9, 12, 16, 20, 24];

// Mirrors offer_calendar.touch_dates()'s 3 slots - display only, same
// small-hardcoded-mirror convention as CAMPAIGN_DAY_OFFSETS above.
const WA_TOUCH_STAGE_LABEL = ["Announce", "Peak", "Last chance"];

let messagesPage = 1;
let messagesQuery = "";
let messagesChannel = "";

async function loadMessages(page) {
  if (page) messagesPage = page;
  const params = new URLSearchParams({ page: messagesPage, perPage: 20 });
  if (messagesQuery) params.set("q", messagesQuery);
  if (messagesChannel) params.set("channel", messagesChannel);

  const d = await api(`api/messages?${params}`);
  renderMessageStats(d.summary);
  renderMessageTrackingAlert(d.tracking);
  renderMessagesTable(d.messages, d.tracking);
  renderMessagesPager(d);
}

function renderMessageStats(s) {
  const breakdown = (c) =>
    `<span style="font-size:13px;color:var(--ink-3);font-weight:600">· ${c.email} Email, ${c.whatsapp} WhatsApp</span>`;
  const card = (label, counts, accent, note) => `
    <div class="stat ${accent}">
      <div class="stat-body">
        <div class="stat-label">${label}</div>
        <div class="stat-main">${counts.total} ${breakdown(counts)}</div>
        <div class="stat-note">${note}</div>
      </div>
    </div>`;

  document.getElementById("messageStats").innerHTML =
    card("Sent", s.sent, "", "Every send attempt, across all channels.") +
    card("Delivered", s.delivered, "",
      `Email here means "accepted by our mail server," <b>not</b> confirmed inbox delivery. WhatsApp's Delivered currently means the same kind of thing - goinfinito accepted the send - not a confirmed delivery receipt yet (that needs parsing a real DLR webhook payload, which hasn't been seen/built yet - see /api/whatsapp/dlr).`) +
    card("Opened / Read", s.opened, "accent-good",
      "WhatsApp's Read status will come from that same DLR webhook, once there's a real payload to build a parser from - nothing populates it yet.") +
    card("Failed", s.failed, "accent-bad",
      "A send that failed immediately (bad address, SMTP error) - not the same as a later bounce, which isn't tracked yet.");
}

function renderMessageTrackingAlert(tracking) {
  const el = document.getElementById("messageTrackingAlert");
  if (tracking && !tracking.reachable) {
    el.classList.remove("hidden");
    el.innerHTML = `<span class="dot"></span><div><b>Every EMAIL row below can only ever show "Sent."</b> ${esc(tracking.note)}</div>`;
  } else {
    el.classList.add("hidden");
  }
}

function renderMessagesTable(messages, tracking) {
  const tbody = document.querySelector("#messagesTable tbody");
  if (!messages.length) {
    tbody.innerHTML = `<tr><td colspan="7"><div class="empty"><div class="big">No messages match.</div>
      <div class="small">Try a different search or channel.</div></div></td></tr>`;
    return;
  }

  const blind = tracking && !tracking.reachable;
  let lastDate = null;
  const html = [];

  for (const m of messages) {
    const at = new Date(m.at);
    const dateLabel = at.toLocaleDateString("en-IN", { day: "numeric", month: "short", year: "numeric" });
    if (dateLabel !== lastDate) {
      html.push(`<tr class="daygroup"><td colspan="7">${dateLabel}</td></tr>`);
      lastDate = dateLabel;
    }

    const isWa = m.channel === "WHATSAPP";
    // WhatsApp seasonal-offer sends have no "touch"/"cycle" (that's the
    // 8-email drip's concept) - they have an occasion + which of the 3
    // announce/peak/last-chance touches this was (see offer_calendar.py).
    const dayLabel = isWa
      ? (m.occasion
          ? `${esc(m.occasion)}${m.touchIndex != null ? " · " + (WA_TOUCH_STAGE_LABEL[m.touchIndex] ?? `Touch ${m.touchIndex + 1}`) : ""}`
          : "—")
      : (m.touch ? `Day ${CAMPAIGN_DAY_OFFSETS[m.touch - 1] ?? m.touch}` : "—");
    const sentAt = at.toLocaleString("en-IN", { day: "numeric", month: "short", hour: "numeric", minute: "2-digit" });

    const notMeasurable = `<span class="blindswatch"><span class="hatch"></span>not measurable</span>`;
    const openedCell = m.openedAt
      ? new Date(m.openedAt).toLocaleString("en-IN", { day: "numeric", month: "short", hour: "numeric", minute: "2-digit" })
      : (blind && !isWa ? notMeasurable : "—");
    const clickedCell = m.clickedAt
      ? new Date(m.clickedAt).toLocaleString("en-IN", { day: "numeric", month: "short", hour: "numeric", minute: "2-digit" })
      : (blind && !isWa ? notMeasurable : "—");

    let statusTag;
    if (m.delivered === false) {
      statusTag = `<span class="tag failed">Failed${m.failReason ? " — " + esc(m.failReason) : ""}</span>`;
    } else if (m.clickedAt) {
      statusTag = `<span class="tag read">Clicked through</span>`;
    } else if (m.openedAt) {
      statusTag = `<span class="tag read">Opened</span>`;
    } else {
      statusTag = `<span class="tag sent">Sent</span>`;
    }
    const viewLink = m.hasMessage
      ? ` <a class="viewmsg" href="#" onclick="viewMessage(${m.id});return false">View message</a>` : "";

    html.push(`<tr>
      <td class="${isWa ? "chan-wa" : "chan-email"}"><span class="who">${esc(m.name)}</span>
        <div class="dim">${m.cspCode} · ${esc(m.productName)}</div></td>
      <td><span class="chanpill${isWa ? " wa" : ""}"><span class="dot"></span>${isWa ? "WhatsApp" : "Email"}</span></td>
      <td>${dayLabel}</td>
      <td>${sentAt}</td>
      <td>${openedCell}</td>
      <td>${clickedCell}</td>
      <td>${statusTag}${viewLink}</td>
    </tr>`);
  }

  tbody.innerHTML = html.join("");
}

function renderMessagesPager(d) {
  const rangeEl = document.getElementById("messagesPagerRange");
  const btnsEl = document.getElementById("messagesPagerBtns");
  if (!d.total) { rangeEl.textContent = ""; btnsEl.innerHTML = ""; return; }

  const from = (d.page - 1) * d.perPage + 1;
  const to = Math.min(d.page * d.perPage, d.total);
  rangeEl.textContent = `Showing ${from}–${to} of ${d.total}`;

  let btns = `<button class="mini" ${d.page === 1 ? "disabled" : ""}
    onclick="loadMessages(${d.page - 1}).catch((err) => toast(err.message))">‹ Prev</button>`;
  for (let p = 1; p <= d.totalPages; p++) {
    btns += `<button class="mini pagebtn ${p === d.page ? "active" : ""}"
      onclick="loadMessages(${p}).catch((err) => toast(err.message))">${p}</button>`;
  }
  btns += `<button class="mini" ${d.page === d.totalPages ? "disabled" : ""}
    onclick="loadMessages(${d.page + 1}).catch((err) => toast(err.message))">Next ›</button>`;
  btnsEl.innerHTML = btns;
}

// ------------------------------------------------------------------ actions
async function reconcile(cspCode, productId, ownsDevice) {
  const by = prompt("Who confirmed this? (your name)");
  if (!by) return;
  await api("api/reconcile", {
    method: "POST",
    body: JSON.stringify({ cspCode, productId, ownsDevice, by, note: "Confirmed by phone" }),
  });
  toast(ownsDevice ? `${cspCode} marked as already owning the device` : `${cspCode} released into the queue`);
  await load();
}

document.getElementById("confirmPreviewBtn").addEventListener("click", () => {
  window.open("api/email/preview/confirm?format=html", "_blank");
});

document.getElementById("confirmSendBtn").addEventListener("click", async (e) => {
  const btn = e.currentTarget;
  const mode = (emailStatus?.MODE || "dry").toLowerCase();

  if (mode === "live") {
    const ok = confirm(
      `Send REAL ownership-confirmation email to ${confirmPlan.willSend} CSPs?\n\n` +
      `From: ${emailStatus.FROM}\n` +
      `This is not a sales email - it asks them to confirm by reply.\n` +
      `This cannot be undone.`
    );
    if (!ok) return;
  }

  btn.disabled = true;
  try {
    const r = await api("api/reconcile/send", {
      method: "POST",
      body: JSON.stringify({ confirmLive: mode === "live" }),
    });
    toast(
      r.mode === "dry"
        ? `Dry run: ${r.composed} composed, nothing sent · ${r.held} held`
        : `Live: sent ${r.sent} · failed ${r.failed} · held ${r.held}`
    );
    await loadConfirmStatus();
  } catch (err) { toast(err.message); }
  btn.disabled = false;
});

/* ═══════════════════════════════════ charts ═══════════════════════════════
 * Hand-written SVG, no charting library. The dashboard has no build step and
 * loads no external scripts, and these four shapes are simple enough that a
 * library would cost more than it saved.
 * Shared conventions: every chart states its own scale, none of them start a
 * value axis anywhere but zero, and a series with no data renders an explicit
 * "nothing here yet" rather than empty axes that look like a rendering bug. */

const esc = (s) => String(s).replace(/[&<>"]/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

function emptyChart(msg) {
  return `<div class="chart-empty">${esc(msg)}</div>`;
}

/** Coverage: owned vs the target count, per product. A progress bar rather
 *  than a percentage alone, because "257 of 469" is the number people act on
 *  and the remaining gap is the whole point of the programme. */
function renderCoverageBars(coverage) {
  const el = document.getElementById("coverageBars");
  if (!coverage || !coverage.length) { el.innerHTML = emptyChart("No coverage data."); return; }

  el.innerHTML = coverage.map((c) => {
    const pctOfTarget = c.targetCount ? Math.min(100, (c.owned / c.targetCount) * 100) : 0;
    const cls = c.productId === "MATM" ? "matm" : "printer";
    return `
    <div class="cov-row">
      <div class="cov-head">
        <span class="cov-name">${esc(c.productName)}</span>
        <span class="cov-num"><b>${c.owned.toLocaleString("en-IN")}</b> of ${c.targetCount.toLocaleString("en-IN")}</span>
      </div>
      <div class="cov-track">
        <div class="cov-fill ${cls}" style="width:${pctOfTarget.toFixed(1)}%"></div>
      </div>
      <div class="cov-foot">
        <span>${c.coveragePct}% of ${c.activeBase.toLocaleString("en-IN")} active · target ${c.targetPct}%</span>
        <span class="${c.remaining ? "cov-gap" : "cov-done"}">
          ${c.remaining ? c.remaining.toLocaleString("en-IN") + " to go" : "target met"}</span>
      </div>
    </div>`;
  }).join("");
}

/** Funnel: distinct CSPs per stage, widest at top. Stages the platform
 *  currently CANNOT measure (measurable: false - see metrics.funnel on the
 *  backend) are dropped from the dashboard entirely rather than shown
 *  hatched - a plain 0 there would read as "nobody opened it" when the
 *  truth is "we have no way to know", and even a hatched "not measurable"
 *  row was still clutter once that point had been made once. The backend
 *  still returns them (with measurable: false) so nothing here needs to
 *  change again once TRACKING_BASE_URL is fixed - stages simply reappear
 *  as their measurable flag flips to true. */
function renderFunnel(stages) {
  const el = document.getElementById("funnelChart");
  if (!stages || !stages.length) { el.innerHTML = emptyChart("No funnel data."); return; }

  stages = stages.filter((s) => s.measurable);
  if (!stages.length) { el.innerHTML = emptyChart("No measurable funnel data yet."); return; }

  const top = Math.max(...stages.map((s) => s.count), 1);
  el.innerHTML = `<div class="funnel">` + stages.map((s, i) => {
    // An unmeasurable stage draws its hatch across the full width - it stands
    // for "unknown", not "zero", so sizing it to a count would be exactly the
    // wrong signal. Set here rather than in CSS because this inline width
    // would otherwise win over any stylesheet rule.
    const w = !s.measurable ? 100 : Math.max((s.count / top) * 100, s.count > 0 ? 2 : 0);
    const prev = i > 0 ? stages[i - 1] : null;
    // Step conversion only where both ends are real measurements.
    let conv = "";
    if (prev && prev.count > 0 && s.measurable && prev.measurable) {
      const r = (s.count / prev.count) * 100;
      // 0 stays a flat "0%" - "0.0%" implies a precision that isn't meaningful
      // at a count of zero. Below 10% keeps one decimal, where the difference
      // between 0.4% and 4% actually matters.
      conv = (r === 0 ? "0" : r < 10 ? r.toFixed(1) : r.toFixed(0)) + "% of previous";
    }
    return `
    <div class="fn-row${s.measurable ? "" : " fn-blind"}">
      <div class="fn-label">${esc(s.stage)}${s.measurable ? "" :
        ` <span class="fn-tagblind" title="Open and click tracking cannot reach this server - see the notice above">not measurable</span>`}</div>
      <div class="fn-barwrap">
        <div class="fn-track"><div class="fn-bar" style="width:${w}%"></div></div>
        <span class="fn-count">${s.measurable ? s.count.toLocaleString("en-IN") : "—"}</span>
      </div>
      <div class="fn-conv">${conv}</div>
    </div>`;
  }).join("") + `</div>`;
}

/** Sends per day - vertical bars, zero-filled by the backend so quiet days
 *  occupy real width instead of being collapsed out of the series. */
function renderSendsChart(points) {
  const el = document.getElementById("sendsChart");
  const note = document.getElementById("sendsNote");
  if (!points || !points.length) {
    el.innerHTML = emptyChart("No campaign emails sent yet.");
    note.textContent = "";
    return;
  }
  const max = Math.max(...points.map((p) => p.count), 1);
  const total = points.reduce((a, p) => a + p.count, 0);
  note.textContent = `${total.toLocaleString("en-IN")} sent across the last ${points.length} days`;

  const W = 100 / points.length;
  el.innerHTML = `
    <div class="bars" style="--maxlabel:'${max}'">
      ${points.map((p) => {
        const h = (p.count / max) * 100;
        const d = new Date(p.date + "T00:00:00");
        const label = d.toLocaleDateString("en-IN", { day: "numeric", month: "short" });
        return `<div class="bar-col" style="width:${W}%" title="${label}: ${p.count} sent">
            <div class="bar-val">${p.count || ""}</div>
            <div class="bar" style="height:${h}%"></div>
            <div class="bar-x">${label}</div>
          </div>`;
      }).join("")}
    </div>`;
}

/** Sequence spread: how many sends have gone out at each of the 8 steps.
 *  Reads as a decay curve once the schedule is running; today it is almost
 *  entirely step 1, which is exactly what a campaign on day 1 looks like. */
function renderTouchChart(touches) {
  const el = document.getElementById("touchChart");
  if (!touches || !touches.length) { el.innerHTML = emptyChart("No sends yet."); return; }
  const max = Math.max(...touches.map((t) => t.count), 1);

  el.innerHTML = `<div class="touchlist">` + touches.map((t) => `
    <div class="touch-row">
      <div class="touch-day">Day ${t.day}</div>
      <div class="touch-track"><div class="touch-fill" style="width:${(t.count / max) * 100}%"></div></div>
      <div class="touch-n">${t.count.toLocaleString("en-IN")}</div>
    </div>`).join("") + `</div>`;
}

/** Coverage trend. This is the one chart that CANNOT be backfilled: coverage
 *  is recomputed from the current sheet every load, so history only exists
 *  from the first day snapshots ran. With fewer than two points there is no
 *  direction to show, and saying so is more useful than a single dot. */
function renderTrend(history) {
  const el = document.getElementById("trendChart");
  const note = document.getElementById("trendNote");

  if (!history || !history.enoughForTrend) {
    const started = history && history.startedOn;
    note.textContent = "History starts the first day this dashboard was opened";
    el.innerHTML = emptyChart(
      started
        ? `Tracking since ${started}. One day recorded so far — the trend line appears tomorrow, once there are two points to compare.`
        : "No history recorded yet. A point is written each day the dashboard is opened."
    );
    return;
  }

  const pts = history.points;
  note.textContent = `${history.days} days recorded, since ${history.startedOn}`;

  const W = 720, H = 200, PAD = { l: 38, r: 12, t: 12, b: 24 };
  const iw = W - PAD.l - PAD.r, ih = H - PAD.t - PAD.b;
  const series = [
    { id: "MATM", label: "Micro ATM", cls: "matm" },
    { id: "PRINTER", label: "Passbook Printer", cls: "printer" },
  ];
  // Axis always to 100% - coverage IS a percentage of a known base, and
  // rescaling to the data would make a 3-point rise look like a surge.
  const y = (v) => PAD.t + ih - (v / 100) * ih;
  const x = (i) => PAD.l + (pts.length === 1 ? iw / 2 : (i / (pts.length - 1)) * iw);

  const paths = series.map((s) => {
    const d = pts.map((p, i) => {
      const v = ((p.coverage || {})[s.id] || {}).coveragePct || 0;
      return `${i ? "L" : "M"}${x(i).toFixed(1)},${y(v).toFixed(1)}`;
    }).join(" ");
    return `<path class="trend-line ${s.cls}" d="${d}" />`;
  }).join("");

  const grid = [0, 25, 50, 75, 100].map((v) =>
    `<line class="trend-grid" x1="${PAD.l}" x2="${W - PAD.r}" y1="${y(v)}" y2="${y(v)}"/>
     <text class="trend-ylab" x="${PAD.l - 7}" y="${y(v) + 3.5}">${v}%</text>`).join("");

  el.innerHTML = `
    <svg class="trendsvg" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" role="img"
         aria-label="Coverage percentage over time by product">
      ${grid}${paths}
    </svg>
    <div class="legend">
      ${series.map((s) => `<span><i class="dot ${s.cls}"></i>${s.label}</span>`).join("")}
    </div>`;
}

async function loadOverview() {
  const m = await api("api/metrics");

  const warn = document.getElementById("trackingWarn");
  if (m.tracking && !m.tracking.reachable) {
    warn.classList.remove("hidden");
    warn.classList.add("serious");
    warn.innerHTML = `<span class="dot"></span><div><b>Open and click tracking is not recording anything.</b>
      ${esc(m.tracking.note)}</div>`;
  } else {
    warn.classList.add("hidden");
  }

  renderCoverageBars(m.coverage);
  renderFunnel(m.funnel);
  renderSendsChart(m.sendsPerDay);
  renderTouchChart(m.touchSpread);
  renderTrend(m.history);
}

function showTab(tab) {
  if (!PAGE[tab]) tab = "overview";
  // The order drawer is a fixed overlay outside the panes, so it would
  // otherwise hang over whatever tab you switched to.
  if (openOrderId) closeOrderDrawer();
  document.querySelectorAll(".navitem").forEach((b) =>
    b.classList.toggle("active", b.dataset.tab === tab));
  document.querySelectorAll(".pane").forEach((p) =>
    p.classList.toggle("hidden", p.id !== "pane-" + tab));
  document.getElementById("pageTitle").textContent = PAGE[tab].title;
  document.getElementById("pageSub").textContent = PAGE[tab].sub;
  // Coverage is the programme scoreboard - it belongs on the targeting view,
  // not on top of a reconciliation worklist.
  document.getElementById("coverage").classList.toggle("hidden", tab !== "targets");
  if (tab === "directory") loadDirectory().catch((err) => toast(err.message));
  // Re-fetched on every visit rather than cached: the funnel and the send
  // chart both move the moment anyone clicks Send, and a stale scoreboard is
  // worse than a half-second wait.
  if (tab === "overview") loadOverview().catch((err) => toast(err.message));
  // Same idea as directory/overview above - a stale "0 opened" would be
  // actively misleading the moment tracking gets fixed, so this refetches
  // every visit rather than caching.
  if (tab === "messages") loadMessages(1).catch((err) => toast(err.message));
}

// ---------------------------------------------------------------- directory
let directoryStatus = "";

async function loadDirectory() {
  const q = document.getElementById("directorySearch").value.trim();
  const params = new URLSearchParams();
  if (q) params.set("q", q);
  if (directoryStatus) params.set("activity", directoryStatus);

  const d = await api(`api/csps?${params.toString()}`);
  const note = document.getElementById("directoryNote");
  const tbody = document.querySelector("#directoryTable tbody");

  note.textContent = d.total > d.shown
    ? `Showing ${d.shown} of ${d.total} matching CSPs - narrow the search to see the rest.`
    : `${d.total} CSP${d.total === 1 ? "" : "s"}${q || directoryStatus ? " matching" : " in the base"}.`;

  if (!d.csps.length) {
    tbody.innerHTML = `<tr><td colspan="8"><div class="empty">
      <div class="big">No matches</div>
      <div class="small">Try a different name, CSP code, or status filter.</div>
    </div></td></tr>`;
    return;
  }

  const OWNERSHIP_TAG = {
    OWNED: '<span class="tag STRONG">Owned</span>',
    GAP_VERIFIED: '<span class="tag quiet">Gap</span>',
    GAP_DISPUTED: '<span class="tag hot">Disputed</span>',
    UNKNOWN: '<span class="tag quiet">Unknown</span>',
  };
  const STATUS_TAG = {
    ACTIVE: '<span class="tag STRONG">Active</span>',
    DORMANT: '<span class="tag MID">Dormant</span>',
    INACTIVE: '<span class="tag quiet">Inactive</span>',
  };

  tbody.innerHTML = d.csps.map((c) => `<tr>
    <td><div class="who">${c.name}</div><div class="dim">${c.cspCode}</div></td>
    <td>${c.rm}</td>
    <td>${c.state || "—"}<div class="dim">${c.district || ""}</div></td>
    <td>${STATUS_TAG[c.activity] || c.activity}<div class="dim" style="max-width:160px">${c.activityReason || ""}</div></td>
    <td class="r">${rs(c.commission)}</td>
    <td class="r">${c.transactions.toLocaleString("en-IN")}</td>
    <td>${OWNERSHIP_TAG[c.matmOwnership] || c.matmOwnership}</td>
    <td>${OWNERSHIP_TAG[c.printerOwnership] || c.printerOwnership}</td>
  </tr>`).join("");
}

let _directorySearchDebounce;
document.getElementById("directorySearch").addEventListener("input", () => {
  clearTimeout(_directorySearchDebounce);
  _directorySearchDebounce = setTimeout(() => loadDirectory().catch((err) => toast(err.message)), 250);
});

document.querySelectorAll("#directoryFilter .segbtn").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll("#directoryFilter .segbtn").forEach((b) => b.classList.remove("active"));
    btn.classList.add("active");
    directoryStatus = btn.dataset.status;
    loadDirectory().catch((err) => toast(err.message));
  });
});

let _messagesSearchDebounce;
document.getElementById("messageSearch").addEventListener("input", () => {
  clearTimeout(_messagesSearchDebounce);
  _messagesSearchDebounce = setTimeout(() => {
    messagesQuery = document.getElementById("messageSearch").value.trim();
    loadMessages(1).catch((err) => toast(err.message));
  }, 250);
});

document.querySelectorAll("#messageChannelFilter .segbtn").forEach((btn) => {
  btn.addEventListener("click", () => {
    if (btn.disabled) return;
    document.querySelectorAll("#messageChannelFilter .segbtn").forEach((b) => b.classList.remove("active"));
    btn.classList.add("active");
    messagesChannel = btn.dataset.channel;
    loadMessages(1).catch((err) => toast(err.message));
  });
});

document.querySelectorAll(".navitem").forEach((btn) => {
  // Routed through the hash so a view can be linked to and survives a reload.
  btn.addEventListener("click", () => { location.hash = btn.dataset.tab; });
});
window.addEventListener("hashchange", () => showTab(location.hash.slice(1)));

document.querySelectorAll("#queueSwitch .segbtn").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll("#queueSwitch .segbtn").forEach((b) => b.classList.remove("active"));
    btn.classList.add("active");
    activeQueue = btn.dataset.q;
    renderQueue();
    // The send plan, mode banner, and button label are all scoped to
    // whichever queue is active - switching tabs must refresh them, or the
    // MATM tab would keep showing (and sending) the PRINTER plan.
    loadEmailStatus().catch((err) => toast(err.message));
  });
});

showTab(location.hash.slice(1) || "overview");

load().catch((err) => {
  document.getElementById("sourceLine").textContent = "Error: " + err.message;
  toast("Failed to load: " + err.message);
});
