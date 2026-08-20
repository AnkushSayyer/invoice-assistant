"use strict";

/* ------------------------------------------------------------------ *
 * Helpers
 * ------------------------------------------------------------------ */
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
const el = (tag, cls, html) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (html != null) n.innerHTML = html;
  return n;
};
const esc = (s) =>
  String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])
  );
const num = (v) => {
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
};
const money = (v) => {
  const n = num(v);
  return n == null ? "—" : "₹" + n.toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
};

async function api(path, opts = {}) {
  const res = await fetch(path, opts);
  let body = null;
  const text = await res.text();
  try { body = text ? JSON.parse(text) : null; } catch { body = text; }
  if (!res.ok) {
    const detail = (body && body.detail) || body || `HTTP ${res.status}`;
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return body;
}

function toast(msg, kind = "") {
  const t = el("div", `toast ${kind}`, esc(msg));
  $("#toastWrap").appendChild(t);
  setTimeout(() => { t.style.opacity = "0"; setTimeout(() => t.remove(), 250); }, 4200);
}

/* ------------------------------------------------------------------ *
 * View routing
 * ------------------------------------------------------------------ */
const LOADERS = {
  clarify: loadClarify,
  queue: loadQueue,
  runs: loadRuns,
  training: loadTraining,
};

function switchView(name) {
  $$(".nav-item").forEach((b) => b.classList.toggle("active", b.dataset.view === name));
  $$(".view").forEach((v) => v.classList.toggle("active", v.id === `view-${name}`));
  if (LOADERS[name]) LOADERS[name]();
}

$$(".nav-item").forEach((b) => b.addEventListener("click", () => switchView(b.dataset.view)));
$$("[data-refresh]").forEach((b) => b.addEventListener("click", () => LOADERS[b.dataset.refresh]()));

/* ------------------------------------------------------------------ *
 * Process invoice
 * ------------------------------------------------------------------ */
const fileInput = $("#fileInput");
const dropzone = $("#dropzone");

fileInput.addEventListener("change", () => {
  $("#fileName").textContent = fileInput.files[0]?.name || "No file selected";
});
["dragover", "dragenter"].forEach((ev) =>
  dropzone.addEventListener(ev, (e) => { e.preventDefault(); dropzone.classList.add("drag"); })
);
["dragleave", "drop"].forEach((ev) =>
  dropzone.addEventListener(ev, (e) => { e.preventDefault(); dropzone.classList.remove("drag"); })
);
dropzone.addEventListener("drop", (e) => {
  const f = e.dataTransfer.files[0];
  if (f) { fileInput.files = e.dataTransfer.files; $("#fileName").textContent = f.name; }
});

$("#processForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const file = fileInput.files[0];
  if (!file) return toast("Choose a PDF first", "err");
  const amount = $("#claimedAmount").value;
  if (amount === "") return toast("Enter a claimed amount", "err");

  const fd = new FormData();
  fd.append("file", file);
  fd.append("claimed_amount", amount);
  const cat = $("#category").value.trim();
  if (cat) fd.append("category", cat);

  const btn = $("#processBtn");
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span>Running agent…';
  $("#processResult").innerHTML = "";
  try {
    const result = await api("/agent/process", { method: "POST", body: fd });
    showProcessResult(result);
    toast(`Decision: ${result.decision.toUpperCase()}`, result.decision === "approve" ? "ok" : result.decision === "reject" ? "err" : "");
    loadQueueCount();
    loadClarifyCount();
  } catch (err) {
    $("#processResult").appendChild(el("div", "card", `<strong style="color:var(--red)">Error</strong><p class="item-meta">${esc(err.message)}</p>`));
    toast(err.message, "err");
  } finally {
    btn.disabled = false;
    btn.textContent = "Run agent";
  }
});

/* ------------------------------------------------------------------ *
 * Renderers
 * ------------------------------------------------------------------ */
function showProcessResult(result) {
  const area = $("#processResult");
  area.innerHTML = "";
  area.appendChild(renderAgentResult(result, showProcessResult));
}

function renderAgentResult(r, onAnswered) {
  const d = r.decision;
  const conf = Math.round((r.confidence || 0) * 100);
  const card = el("div", `card decision-card ${d}`);

  const top = el("div", "decision-top");
  top.appendChild(el("span", `pill ${d}`, d));
  top.appendChild(el("span", "pill soft", `source: ${esc(r.source || "?")}`));
  if (r.vendor_key) top.appendChild(el("span", "pill soft", esc(r.vendor_key)));
  const confBox = el("div", "conf",
    `<span class="conf-label">confidence ${conf}%</span><div class="conf-bar"><div class="conf-fill" style="width:${conf}%"></div></div>`);
  top.appendChild(confBox);
  card.appendChild(top);

  const ex = r.extraction || {};
  const claimed = num(r.claimed_amount);
  const calc = num(r.calculated_total);
  const matchOk = claimed != null && calc != null && Math.abs(claimed - calc) < 0.05;
  const metrics = el("div", "metrics");
  metrics.appendChild(metric("Claimed", money(r.claimed_amount)));
  metrics.appendChild(metric("Calculated", money(r.calculated_total), matchOk ? "ok" : "warn"));
  metrics.appendChild(metric("Approved", money(r.approved_amount)));
  metrics.appendChild(metric("Vendor", esc(ex.vendor || "—")));
  card.appendChild(metrics);

  if (r.reasons?.length) {
    card.appendChild(el("div", "section-title", "Why"));
    const ul = el("ul", "reason-list");
    r.reasons.forEach((x) => ul.appendChild(el("li", "", esc(x))));
    card.appendChild(ul);
  }
  if (r.remediations?.length) {
    card.appendChild(el("div", "section-title", "Self-correction"));
    const ul = el("ul", "reason-list");
    r.remediations.forEach((x) => ul.appendChild(el("li", "rem", "✔ " + esc(x))));
    card.appendChild(ul);
  }

  card.appendChild(el("div", "section-title", "Extracted invoice"));
  card.appendChild(extractionTable(ex));

  if (r.steps?.length) {
    const det = el("details", "collapse");
    det.appendChild(el("summary", "", `Agent trace (${r.steps.length} steps)`));
    const steps = el("div", "steps");
    r.steps.forEach((s) => {
      const row = el("div", "step");
      row.appendChild(el("div", `step-dot ${s.ok ? "" : "bad"}`));
      const body = el("div");
      body.appendChild(el("div", "step-tool", esc(s.tool)));
      body.appendChild(el("div", "step-sum", esc(s.summary)));
      row.appendChild(body);
      steps.appendChild(row);
    });
    det.appendChild(steps);
    card.appendChild(det);
  }

  if (d === "clarify" && r.clarifications?.length) {
    card.appendChild(el("div", "section-title", "Answer to resolve"));
    r.clarifications.forEach((clar) => card.appendChild(clarifyForm(clar, onAnswered)));
  }

  if (r.document_id) {
    card.appendChild(el("div", "mono", "document: " + esc(r.document_id)));
  }
  return card;
}

function metric(k, v, cls = "") {
  return el("div", "metric", `<div class="k">${esc(k)}</div><div class="v ${cls}">${v}</div>`);
}

function extractionTable(ex) {
  const wrap = el("div");
  const items = ex.line_items || [];
  if (items.length) {
    const t = el("table", "tbl");
    t.innerHTML = `<thead><tr><th>Item</th><th class="num">Qty</th><th class="num">Amount</th></tr></thead>`;
    const tb = el("tbody");
    items.forEach((it) => {
      tb.appendChild(el("tr", "", `<td>${esc(it.description)}</td><td class="num">${esc(it.quantity ?? 1)}</td><td class="num">${money(it.amount)}</td>`));
    });
    t.appendChild(tb);
    wrap.appendChild(t);
  }
  (ex.discounts || []).forEach((d) => {
    wrap.appendChild(el("div", "item-meta", `Discount: ${esc(d.description)} <b>-${money(d.amount)}</b>`));
  });
  const totals = el("table", "tbl");
  const rows = [
    ["Subtotal", ex.subtotal], ["Tax", ex.tax], ["Fees", ex.fees],
    ["Tip", ex.tip], ["Total", ex.total],
  ].filter(([, v]) => v != null);
  totals.innerHTML = "<tbody>" + rows.map(([k, v]) =>
    `<tr><td>${k}${k === "Total" ? " <b>" : ""}</td><td class="num">${money(v)}</td></tr>`).join("") + "</tbody>";
  wrap.appendChild(totals);
  return wrap;
}

/* ------------------------------------------------------------------ *
 * Review queue
 * ------------------------------------------------------------------ */
async function loadQueueCount() {
  try {
    const items = await api("/agent/queue");
    const badge = $("#queueCount");
    if (items.length) { badge.textContent = items.length; badge.hidden = false; }
    else badge.hidden = true;
  } catch { /* ignore */ }
}

async function loadQueue() {
  const list = $("#queueList");
  list.innerHTML = '<div class="empty">Loading…</div>';
  try {
    const items = await api("/agent/queue");
    if (!items.length) { list.innerHTML = '<div class="empty">Nothing awaiting review. 🎉</div>'; return; }
    list.innerHTML = "";
    items.forEach((item) => list.appendChild(queueCard(item)));
  } catch (err) {
    list.innerHTML = `<div class="empty">${esc(err.message)}</div>`;
  }
}

function queueCard({ document: doc, latest_run: run }) {
  const card = el("div", "card item-card");
  const top = el("div", "item-top");
  const dec = (run?.decision || doc.status || "").toLowerCase();
  top.appendChild(el("span", "item-name", esc(doc.filename)));
  if (dec) top.appendChild(el("span", `pill ${["approve","reject","escalate"].includes(dec) ? dec : "soft"}`, dec));
  const actions = el("div", "item-actions");
  const resolveBtn = el("button", "btn btn-primary btn-sm", "Resolve");
  resolveBtn.addEventListener("click", () => openResolve(doc));
  actions.appendChild(resolveBtn);
  top.appendChild(actions);
  card.appendChild(top);

  const meta = el("div", "item-meta");
  meta.innerHTML =
    `Claimed <b>${money(doc.claimed_amount)}</b>` +
    `<span>Calculated <b>${money(doc.calculated_total)}</b></span>` +
    (doc.vendor_key ? `<span>${esc(doc.vendor_key)}</span>` : "");
  card.appendChild(meta);

  const reasons = run?.reasons || [];
  if (reasons.length) {
    const ul = el("ul", "reason-list");
    reasons.slice(0, 4).forEach((x) => ul.appendChild(el("li", "", esc(x))));
    card.appendChild(ul);
  } else if (doc.error_message) {
    card.appendChild(el("div", "item-meta", esc(doc.error_message)));
  }
  card.appendChild(el("div", "mono", esc(doc.id)));
  return card;
}

/* ------------------------------------------------------------------ *
 * Clarifications (ask & learn)
 * ------------------------------------------------------------------ */
async function loadClarifyCount() {
  try {
    const items = await api("/agent/clarifications");
    const n = items.reduce((a, i) => a + (i.clarifications?.length || 0), 0);
    const badge = $("#clarifyCount");
    if (n) { badge.textContent = n; badge.hidden = false; }
    else badge.hidden = true;
  } catch { /* ignore */ }
}

async function loadClarify() {
  const list = $("#clarifyList");
  list.innerHTML = '<div class="empty">Loading…</div>';
  try {
    const items = await api("/agent/clarifications");
    if (!items.length) { list.innerHTML = '<div class="empty">No open questions. 🎉</div>'; return; }
    list.innerHTML = "";
    items.forEach((item) => list.appendChild(clarifyCard(item)));
  } catch (err) {
    list.innerHTML = `<div class="empty">${esc(err.message)}</div>`;
  }
}

function clarifyCard(item) {
  const doc = item.document || {};
  const card = el("div", "card item-card");
  const top = el("div", "item-top");
  top.appendChild(el("span", "item-name", esc(doc.filename || "invoice.pdf")));
  top.appendChild(el("span", "pill clarify", "needs input"));
  if (doc.vendor_key) top.appendChild(el("span", "pill soft", esc(doc.vendor_key)));
  card.appendChild(top);

  const meta = el("div", "item-meta");
  meta.innerHTML =
    `Claimed <b>${money(doc.claimed_amount)}</b>` +
    `<span>Calculated <b>${money(doc.calculated_total)}</b></span>`;
  card.appendChild(meta);

  const onAnswered = () => {
    loadClarify();
    if ($("#view-runs").classList.contains("active")) loadRuns();
  };
  (item.clarifications || []).forEach((clar) => card.appendChild(clarifyForm(clar, onAnswered)));
  return card;
}

function clarifyForm(clar, onAnswered) {
  const form = el("div", "clarify-form");
  form.appendChild(el("div", "clarify-q", esc(clar.question)));

  const ev = clar.evidence || {};
  const chips = el("div", "clarify-evidence");
  if (ev.gap != null) chips.appendChild(el("span", "chip", `gap ${money(ev.gap)}`));
  if (ev.total != null) chips.appendChild(el("span", "chip", `stated total ${money(ev.total)}`));
  if (ev.calculated_total != null) chips.appendChild(el("span", "chip", `components ${money(ev.calculated_total)}`));
  if (chips.childElementCount) form.appendChild(chips);

  const name = `opt_${clar.id}`;
  const optList = el("div", "opt-list");
  (clar.options || []).forEach((o) => {
    const lab = el("label", "opt");
    const radio = el("input");
    radio.type = "radio"; radio.name = name; radio.value = o.id;
    if (o.id === clar.agent_hypothesis) { radio.checked = true; lab.classList.add("sel"); }
    radio.addEventListener("change", () => {
      $$(".opt", optList).forEach((x) => x.classList.remove("sel"));
      if (radio.checked) lab.classList.add("sel");
    });
    lab.appendChild(radio);
    lab.appendChild(el("span", "", esc(o.label)));
    optList.appendChild(lab);
  });
  form.appendChild(optList);

  const noteWrap = el("div", "field");
  noteWrap.innerHTML = `<label>Note <span class="muted">(optional — name the exact line to pin a permanent capture rule)</span></label>`;
  const note = el("textarea"); note.id = `note_${clar.id}`; note.style.minHeight = "56px";
  note.placeholder = "e.g. Airport Surcharge";
  noteWrap.appendChild(note); form.appendChild(noteWrap);

  const scopeRow = el("div", "scope-row");
  const scopeWrap = el("div", "field");
  scopeWrap.innerHTML = `<label>Learn as</label>`;
  const scopeSel = el("select"); scopeSel.id = `scope_${clar.id}`;
  [["vendor", "This vendor"], ["category", "Category"], ["global", "All vendors (global)"]]
    .forEach(([v, l]) => {
      const op = el("option", "", esc(l)); op.value = v;
      if (v === clar.proposed_scope) op.selected = true;
      scopeSel.appendChild(op);
    });
  scopeWrap.appendChild(scopeSel); scopeRow.appendChild(scopeWrap);

  const keyWrap = el("div", "field");
  keyWrap.innerHTML = `<label>Scope key</label>`;
  const keyInp = el("input"); keyInp.id = `scopekey_${clar.id}`;
  keyInp.value = clar.proposed_scope_key || "";
  keyWrap.appendChild(keyInp); scopeRow.appendChild(keyWrap);
  form.appendChild(scopeRow);

  const syncKey = () => {
    if (scopeSel.value === "global") { keyInp.value = "*"; keyInp.disabled = true; }
    else { keyInp.disabled = false; if (keyInp.value === "*") keyInp.value = clar.proposed_scope_key || ""; }
  };
  scopeSel.addEventListener("change", syncKey); syncKey();

  const learnWrap = el("label", "check");
  learnWrap.innerHTML = `<input type="checkbox" id="learn_${clar.id}" checked /> Persist as a reusable rule`;
  form.appendChild(learnWrap);

  const btn = el("button", "btn btn-primary", "Answer & re-run");
  btn.addEventListener("click", () => submitClarify(clar, btn, onAnswered));
  form.appendChild(btn);
  return form;
}

async function submitClarify(clar, btn, onAnswered) {
  const picked = document.querySelector(`input[name="opt_${clar.id}"]:checked`);
  if (!picked) return toast("Pick an answer first", "err");

  const payload = {
    clarification_id: clar.id,
    answer_option_id: picked.value,
    confirmed_scope: $(`#scope_${clar.id}`).value,
    learn: $(`#learn_${clar.id}`).checked,
  };
  const note = $(`#note_${clar.id}`).value.trim();
  if (note) payload.answer_note = note;
  const key = $(`#scopekey_${clar.id}`).value.trim();
  if (key) payload.confirmed_scope_key = key;

  btn.disabled = true;
  const orig = btn.textContent;
  btn.innerHTML = '<span class="spinner"></span>Re-running…';
  try {
    const result = await api("/agent/clarify", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    toast(
      `Learned & re-ran · ${result.decision.toUpperCase()}`,
      result.decision === "approve" ? "ok" : result.decision === "reject" ? "err" : ""
    );
    loadClarifyCount(); loadQueueCount();
    if (onAnswered) onAnswered(result);
  } catch (err) {
    toast(err.message, "err");
    btn.disabled = false; btn.textContent = orig;
  }
}

/* ------------------------------------------------------------------ *
 * Resolve drawer
 * ------------------------------------------------------------------ */
const drawer = $("#resolveDrawer");
const overlay = $("#drawerOverlay");
function closeDrawer() { drawer.hidden = true; overlay.hidden = true; }
$("#drawerClose").addEventListener("click", closeDrawer);
overlay.addEventListener("click", closeDrawer);

const TARGET_FIELDS = ["subtotal", "tax", "fees", "tip"];
let resolvePreviewValid = false;
let resolvePreviewTimer = null;

function openResolve(doc) {
  const f = doc.extracted_fields || {};
  const g = (k, v = f[k]) => v ?? "";
  const body = $("#drawerBody");
  body.innerHTML = "";

  body.appendChild(el("div", "item-meta", `<span>${esc(doc.filename)}</span>`));

  const fields = [
    ["vendor", "Vendor", "text"],
    ["invoice_number", "Invoice #", "text"],
    ["date", "Date", "text"],
  ];
  fields.forEach(([k, label, type]) => {
    const wrap = el("div", "field");
    wrap.innerHTML = `<label>${label}</label>`;
    const inp = el("input");
    inp.type = type; inp.id = `rf_${k}`; inp.value = g(k);
    wrap.appendChild(inp);
    body.appendChild(wrap);
  });

  const discountSum = (f.discounts || []).reduce((a, d) => a + (num(d.amount) || 0), 0);
  const grid = el("div", "drawer-grid");
  [["subtotal", "Subtotal", g("subtotal")], ["tax", "Tax", g("tax")],
   ["fees", "Fees", g("fees")], ["tip", "Tip", g("tip")],
   ["total", "Total (pre-discount)", g("total")],
   ["discount", "Discounts", discountSum || ""]]
    .forEach(([k, label, value]) => {
      const wrap = el("div", "field");
      wrap.innerHTML = `<label>${label}</label>`;
      const inp = el("input");
      inp.type = "number"; inp.step = "0.01"; inp.id = `rf_${k}`; inp.value = value;
      wrap.appendChild(inp);
      grid.appendChild(wrap);
    });
  body.appendChild(grid);

  const amt = el("div", "field");
  amt.innerHTML = `<label>Approved amount <span class="muted">(blank = claimed)</span></label>`;
  const amtInp = el("input"); amtInp.type = "number"; amtInp.step = "0.01"; amtInp.id = "rf_approved_amount";
  amtInp.placeholder = money(doc.claimed_amount);
  amt.appendChild(amtInp); body.appendChild(amt);

  // ----- Teach the agent (optional) --------------------------------------- //
  body.appendChild(el("div", "section-title", "Teach the agent (optional)"));

  const baseRules = doc.validation_rules || {};
  const activeComponents = baseRules.total_components || ["subtotal", "tax", "fees", "tip"];

  const formulaWrap = el("div", "field");
  formulaWrap.innerHTML = `<label>Total formula <span class="muted">(which parts sum to the claimable total)</span></label>`;
  const compRow = el("div", "check-row");
  TARGET_FIELDS.forEach((c) => {
    const lab = el("label", "check");
    const box = el("input"); box.type = "checkbox"; box.id = `rf_comp_${c}`;
    box.checked = activeComponents.includes(c);
    lab.appendChild(box); lab.appendChild(el("span", "", c));
    compRow.appendChild(lab);
  });
  formulaWrap.appendChild(compRow);
  const subLab = el("label", "check");
  subLab.innerHTML = `<input type="checkbox" id="rf_subtract" ${baseRules.subtract_discounts === false ? "" : "checked"} /> Subtract discounts`;
  formulaWrap.appendChild(subLab);
  body.appendChild(formulaWrap);

  const capWrap = el("div", "field");
  capWrap.innerHTML = `<label>Always capture a charge <span class="muted">(add a named line into a component on future invoices)</span></label>`;
  const capRow = el("div", "scope-row");
  const anchorWrap = el("div", "field");
  anchorWrap.innerHTML = `<label>Line label</label>`;
  const anchorInp = el("input"); anchorInp.id = "rf_capture_anchor"; anchorInp.placeholder = "e.g. Airport Surcharge";
  anchorWrap.appendChild(anchorInp); capRow.appendChild(anchorWrap);
  const fieldWrap = el("div", "field");
  fieldWrap.innerHTML = `<label>Into field</label>`;
  const fieldSel = el("select"); fieldSel.id = "rf_capture_field";
  TARGET_FIELDS.forEach((c) => { const op = el("option", "", c); op.value = c; if (c === "fees") op.selected = true; fieldSel.appendChild(op); });
  fieldWrap.appendChild(fieldSel); capRow.appendChild(fieldWrap);
  capWrap.appendChild(capRow); body.appendChild(capWrap);

  const dirWrap = el("div", "field");
  dirWrap.innerHTML = `<label>Extra instruction <span class="muted">(free text; steers future LLM extraction only, not validated here)</span></label>`;
  const dir = el("textarea"); dir.id = "rf_directive"; dir.style.minHeight = "56px";
  dir.placeholder = "e.g. This vendor lists a service charge in the footer; include it in fees.";
  dirWrap.appendChild(dir); body.appendChild(dirWrap);

  const scopeRow = el("div", "scope-row");
  const scopeWrap = el("div", "field");
  scopeWrap.innerHTML = `<label>Learn as</label>`;
  const scopeSel = el("select"); scopeSel.id = "rf_scope";
  [["vendor", "This vendor"], ["category", "Category"], ["global", "All vendors (global)"]]
    .forEach(([v, l]) => { const op = el("option", "", esc(l)); op.value = v; scopeSel.appendChild(op); });
  scopeWrap.appendChild(scopeSel); scopeRow.appendChild(scopeWrap);
  const keyWrap = el("div", "field");
  keyWrap.innerHTML = `<label>Scope key</label>`;
  const keyInp = el("input"); keyInp.id = "rf_scopekey"; keyInp.value = doc.vendor_key || "";
  keyWrap.appendChild(keyInp); scopeRow.appendChild(keyWrap);
  body.appendChild(scopeRow);
  const syncKey = () => {
    if (scopeSel.value === "global") { keyInp.value = "*"; keyInp.disabled = true; }
    else { keyInp.disabled = false; if (keyInp.value === "*") keyInp.value = doc.vendor_key || ""; }
  };
  scopeSel.addEventListener("change", syncKey); syncKey();

  const noteWrap = el("div", "field");
  noteWrap.innerHTML = `<label>Reviewer note <span class="muted">(audit trail only)</span></label>`;
  const note = el("textarea"); note.id = "rf_note"; note.style.minHeight = "56px";
  noteWrap.appendChild(note); body.appendChild(noteWrap);

  const preview = el("div", "preview-banner", "Checking…");
  preview.id = "rf_preview";
  body.appendChild(preview);

  const forceWrap = el("label", "check"); forceWrap.id = "rf_force_wrap";
  forceWrap.innerHTML = `<input type="checkbox" id="rf_force" /> Force approve past math mismatch <span class="muted">(fallback when it can't reconcile)</span>`;
  body.appendChild(forceWrap);

  const actions = el("div", "drawer-actions");
  const approveBtn = el("button", "btn btn-success", "Approve & learn");
  const rejectBtn = el("button", "btn btn-danger", "Reject");
  approveBtn.addEventListener("click", () => submitResolve(doc, "approve", approveBtn));
  rejectBtn.addEventListener("click", () => submitResolve(doc, "reject", rejectBtn));
  actions.appendChild(approveBtn); actions.appendChild(rejectBtn);
  body.appendChild(actions);

  // Live validation: re-check on any edit that affects the math.
  const schedulePreview = () => {
    clearTimeout(resolvePreviewTimer);
    resolvePreviewTimer = setTimeout(() => runResolvePreview(doc), 350);
  };
  ["rf_subtotal", "rf_tax", "rf_fees", "rf_tip", "rf_total", "rf_discount",
   "rf_subtract", "rf_capture_anchor", "rf_capture_field",
   ...TARGET_FIELDS.map((c) => `rf_comp_${c}`)]
    .forEach((id) => {
      const node = $(`#${id}`);
      if (node) { node.addEventListener("input", schedulePreview); node.addEventListener("change", schedulePreview); }
    });

  drawer.hidden = false; overlay.hidden = false;
  runResolvePreview(doc);
}

function collectResolveState(doc) {
  const base = doc.extracted_fields || {};
  const discountVal = num($("#rf_discount").value) || 0;
  const approved_fields = {
    ...base,
    vendor: $("#rf_vendor").value,
    invoice_number: $("#rf_invoice_number").value,
    date: $("#rf_date").value,
    subtotal: $("#rf_subtotal").value,
    tax: $("#rf_tax").value,
    fees: $("#rf_fees").value,
    tip: $("#rf_tip").value,
    total: $("#rf_total").value,
    discounts: discountVal > 0 ? [{ description: "Discount", amount: discountVal }] : [],
  };
  const baseRules = doc.validation_rules || {};
  const components = TARGET_FIELDS.filter((c) => $(`#rf_comp_${c}`).checked);
  const validation_rules = {
    validate_line_items: !!baseRules.validate_line_items,
    total_components: components.length ? components : ["subtotal"],
    subtract_discounts: $("#rf_subtract").checked,
    line_amount_includes_tax: !!baseRules.line_amount_includes_tax,
  };
  const state = { approved_fields, validation_rules };
  const anchor = $("#rf_capture_anchor").value.trim();
  if (anchor) { state.capture_anchor = anchor; state.capture_target_field = $("#rf_capture_field").value; }
  return state;
}

function setApproveGate(valid) {
  resolvePreviewValid = valid;
  const forceWrap = $("#rf_force_wrap");
  if (forceWrap) forceWrap.classList.toggle("needed", !valid);
}

function renderResolvePreview(r) {
  const banner = $("#rf_preview");
  if (!banner) return;
  if (r.is_valid) {
    banner.className = "preview-banner ok";
    banner.textContent = `Reconciles · claimable total ${money(r.calculated_total)}`;
  } else {
    banner.className = "preview-banner err";
    const errs = (r.errors || []).map((e) => `<li>${esc(e)}</li>`).join("");
    const capNote = r.capture_previewable === false
      ? `<div class="muted">Capture rule can't be previewed (no stored text for this document); it will still be learned.</div>`
      : "";
    banner.innerHTML = `<strong>Does not reconcile · calculated ${money(r.calculated_total)}</strong><ul>${errs}</ul>${capNote}`;
  }
  setApproveGate(r.is_valid);
}

async function runResolvePreview(doc) {
  const banner = $("#rf_preview");
  if (!banner) return;
  banner.className = "preview-banner loading";
  banner.textContent = "Checking…";
  try {
    const state = collectResolveState(doc);
    const r = await api("/agent/resolve/preview", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ document_id: doc.id, ...state }),
    });
    renderResolvePreview(r);
  } catch (err) {
    banner.className = "preview-banner err";
    banner.textContent = err.message;
    setApproveGate(false);
  }
}

async function submitResolve(doc, decision, btn) {
  const note = $("#rf_note").value.trim();
  const payload = { document_id: doc.id, decision, learn_vendor: true };
  if (note) payload.note = note;

  if (decision === "approve") {
    const state = collectResolveState(doc);
    payload.approved_fields = state.approved_fields;
    payload.validation_rules = state.validation_rules;
    if (state.capture_anchor) {
      payload.capture_anchor = state.capture_anchor;
      payload.capture_target_field = state.capture_target_field;
    }
    const directive = $("#rf_directive").value.trim();
    if (directive) payload.directive = directive;
    payload.learn_scope = $("#rf_scope").value;
    const key = $("#rf_scopekey").value.trim();
    if (key) payload.learn_scope_key = key;

    const amt = $("#rf_approved_amount").value;
    if (amt !== "") payload.approved_amount = amt;
    const forced = $("#rf_force").checked;
    if (forced) payload.force = true;

    if (!resolvePreviewValid && !forced && amt === "") {
      return toast("Doesn't reconcile yet — adjust to green, set an approved amount, or tick Force.", "err");
    }
  }

  btn.disabled = true;
  const orig = btn.textContent;
  btn.innerHTML = '<span class="spinner"></span>Saving…';
  try {
    await api("/agent/resolve", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    toast(`${decision === "approve" ? "Approved · agent learned the rule" : "Rejected"}`, decision === "approve" ? "ok" : "");
    closeDrawer();
    loadQueue(); loadQueueCount();
    if ($("#view-runs").classList.contains("active")) loadRuns();
  } catch (err) {
    toast(err.message, "err");
    btn.disabled = false; btn.textContent = orig;
  }
}

/* ------------------------------------------------------------------ *
 * Audit trail
 * ------------------------------------------------------------------ */
async function loadRuns() {
  const list = $("#runsList");
  list.innerHTML = '<div class="empty">Loading…</div>';
  try {
    const runs = await api("/agent/runs?limit=50");
    if (!runs.length) { list.innerHTML = '<div class="empty">No agent runs yet.</div>'; return; }
    list.innerHTML = "";
    runs.forEach((run) => {
      const card = el("div", "card item-card");
      const top = el("div", "item-top");
      const dec = (run.decision || "").toLowerCase();
      top.appendChild(el("span", "item-name", esc(run.filename || "invoice.pdf")));
      top.appendChild(el("span", `pill ${["approve","reject","escalate"].includes(dec) ? dec : "soft"}`, dec));
      top.appendChild(el("span", "pill soft", `source: ${esc(run.source || "?")}`));
      const conf = Math.round((run.confidence || 0) * 100);
      const c = el("div", "conf");
      c.innerHTML = `<span class="conf-label">${conf}%</span><div class="conf-bar"><div class="conf-fill" style="width:${conf}%"></div></div>`;
      top.appendChild(c);
      card.appendChild(top);
      if (run.reasons?.length) {
        const ul = el("ul", "reason-list");
        run.reasons.slice(0, 3).forEach((x) => ul.appendChild(el("li", "", esc(x))));
        card.appendChild(ul);
      }
      if (run.vendor_key) card.appendChild(el("div", "mono", esc(run.vendor_key)));
      list.appendChild(card);
    });
  } catch (err) {
    list.innerHTML = `<div class="empty">${esc(err.message)}</div>`;
  }
}

/* ------------------------------------------------------------------ *
 * Training data
 * ------------------------------------------------------------------ */
async function loadTraining() {
  const list = $("#trainingList");
  list.innerHTML = '<div class="empty">Loading…</div>';
  try {
    const rows = await api("/agent/training-data?limit=200");
    if (!rows.length) {
      list.innerHTML = '<div class="empty">No human-verified examples yet. Resolve a document to start building training data.</div>';
      return;
    }
    list.innerHTML = "";
    const info = el("div", "item-meta", `<span><b>${rows.length}</b> labelled example(s) ready to export for uptraining Document AI.</span>`);
    list.appendChild(info);
    rows.forEach((row) => {
      const f = row.fields || {};
      const card = el("div", "card item-card");
      const top = el("div", "item-top");
      top.appendChild(el("span", "item-name", esc(row.filename || "invoice.pdf")));
      if (row.vendor_key) top.appendChild(el("span", "pill soft", esc(row.vendor_key)));
      card.appendChild(top);
      card.appendChild(el("div", "item-meta",
        `<span>${esc(f.vendor || "—")}</span><span>Total <b>${money(f.total)}</b></span><span>${esc(f.invoice_number || "")}</span>`));
      card.appendChild(el("div", "mono", esc(row.document_id)));
      list.appendChild(card);
    });
  } catch (err) {
    list.innerHTML = `<div class="empty">${esc(err.message)}</div>`;
  }
}

/* ------------------------------------------------------------------ *
 * Init
 * ------------------------------------------------------------------ */
loadQueueCount();
loadClarifyCount();
