// Staybot guided resident onboarding (team console). Loaded by index.html.
// All data comes from /onboarding/cases; the server enforces every rule.
(function () {
  "use strict";

  const css = `
  .obx { --ok:#008A05; --warn:#B25E09; --bad:#C13515; --info:#1D5FBF; }
  .obx h2 { margin: 0; }
  .obx-head { display:flex; justify-content:space-between; align-items:flex-start; gap:16px; flex-wrap:wrap; margin-bottom:16px; }
  .obx-actions { display:flex; gap:8px; flex-wrap:wrap; align-items:center; }
  .obx-btn { display:inline-flex; align-items:center; justify-content:center; border:1px solid var(--line); background:#fff; border-radius:22px; padding:10px 18px; font-weight:600; min-height:42px; color:var(--ink); text-decoration:none; }
  .obx-btn:hover:not(:disabled) { border-color:var(--ink); }
  .obx-btn.primary { border:0; color:#fff; background:var(--brand-grad); }
  .obx-btn.dark { border:0; color:#fff; background:var(--ink); }
  .obx-btn.small { padding:6px 12px; min-height:32px; font-size:13px; }
  .obx-btn:disabled { opacity:.45; cursor:not-allowed; }
  .obx-link { border:0; background:none; padding:0; color:var(--ink); font-weight:600; text-decoration:underline; }
  .obx-banners { display:grid; grid-template-columns:repeat(auto-fit,minmax(220px,1fr)); gap:10px; margin:10px 0 20px; }
  .obx-banner { border:1px solid var(--line); border-radius:12px; padding:12px 14px; background:#fff; font-size:13px; }
  .obx-banner b { display:block; font-size:13px; margin-bottom:2px; }
  .obx-banner.warn { background:#FFF8EC; border-color:#F5D9A8; }
  .obx-banner.bad { background:#FFF1EE; border-color:#F3C4B8; }
  .obx-chip { display:inline-flex; align-items:center; gap:5px; padding:3px 10px; border-radius:12px; font-size:12px; font-weight:700; background:var(--bg-soft); white-space:nowrap; }
  .obx-chip.ok { background:#E8F5E9; color:var(--ok); }
  .obx-chip.warn { background:#FFF3E0; color:var(--warn); }
  .obx-chip.bad { background:#FDECEA; color:var(--bad); }
  .obx-chip.info { background:#E8F0FB; color:var(--info); }
  .obx-chip.test { background:#F1ECFB; color:#5B3BA8; }
  .obx-cards { display:grid; grid-template-columns:repeat(auto-fill,minmax(300px,1fr)); gap:16px; }
  .obx-card { border:1px solid var(--line); border-radius:16px; padding:18px; background:#fff; text-align:left; display:flex; flex-direction:column; gap:10px; }
  .obx-card:hover { box-shadow:var(--shadow-sm); }
  .obx-card h3 { margin:0; font-size:17px; }
  .obx-row { display:flex; gap:6px; flex-wrap:wrap; align-items:center; }
  .obx-muted { color:var(--ink-2); }
  .obx-small { font-size:12px; }
  .obx-progress { height:8px; border-radius:4px; background:var(--line-soft); overflow:hidden; }
  .obx-progress i { display:block; height:100%; background:var(--brand-grad); }
  .obx-next { border-radius:10px; background:var(--bg-soft); padding:10px 12px; font-size:13px; }
  .obx-dl { display:grid; grid-template-columns:auto 1fr; gap:4px 12px; margin:0; font-size:13px; }
  .obx-dl dt { color:var(--ink-2); } .obx-dl dd { margin:0; font-weight:600; overflow-wrap:anywhere; }
  .obx-layout { display:grid; grid-template-columns:250px minmax(0,1fr); gap:24px; align-items:start; }
  .obx-stepper { list-style:none; margin:0; padding:0; display:flex; flex-direction:column; gap:6px; position:sticky; top:96px; }
  .obx-stepper button { width:100%; display:flex; gap:10px; align-items:center; text-align:left; border:1px solid transparent; background:none; border-radius:12px; padding:10px; }
  .obx-stepper button:hover { background:var(--bg-soft); }
  .obx-stepper button.on { border-color:var(--ink); background:#fff; }
  .obx-num { width:28px; height:28px; border-radius:50%; display:grid; place-items:center; font-weight:800; font-size:13px; flex:none; border:1.5px solid var(--line); background:#fff; }
  .obx-num.done { background:var(--ink); color:#fff; border-color:var(--ink); }
  .obx-stepper small { display:block; color:var(--ink-2); font-size:12px; }
  .obx-panel { border:1px solid var(--line); border-radius:16px; padding:24px; background:#fff; }
  .obx-panel + .obx-panel { margin-top:16px; }
  .obx-kicker { color:var(--brand-dark); font-weight:700; font-size:13px; text-transform:uppercase; letter-spacing:.4px; }
  .obx-story { font-size:24px; font-weight:800; letter-spacing:-.3px; margin:4px 0 6px; }
  .obx-missing { border-radius:12px; background:#FFF8EC; border:1px solid #F5D9A8; padding:10px 14px; margin:12px 0; font-size:13px; }
  .obx-missing ul { margin:4px 0 0; padding-left:18px; }
  .obx-good { border-radius:12px; background:#EEF8EF; border:1px solid #BFE3C3; padding:10px 14px; margin:12px 0; font-size:13px; }
  .obx-form { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:16px; margin-top:16px; }
  .obx-form .full { grid-column:1/-1; }
  .obx-form label, .obx-field { display:flex; flex-direction:column; gap:6px; font-weight:600; font-size:14px; }
  .obx-form label small, .obx-field small { font-weight:400; color:var(--ink-2); }
  .obx input[type=text], .obx input[type=tel], .obx input[type=email], .obx input[type=date], .obx input[type=number], .obx select, .obx textarea {
    padding:11px 12px; border:1px solid var(--line); border-radius:10px; font:inherit; width:100%; background:#fff; min-height:44px; }
  .obx input:focus, .obx select:focus, .obx textarea:focus { outline:none; border-color:var(--ink); box-shadow:0 0 0 1px var(--ink); }
  .obx textarea { min-height:84px; resize:vertical; }
  .obx-check { display:flex !important; flex-direction:row !important; gap:10px !important; align-items:flex-start; font-weight:500 !important; }
  .obx-check input { width:20px; height:20px; margin-top:2px; flex:none; }
  .obx-foot { display:flex; justify-content:space-between; gap:10px; margin-top:22px; padding-top:18px; border-top:1px solid var(--line-soft); flex-wrap:wrap; }
  .obx-status { margin-top:10px; font-size:13px; min-height:18px; }
  .obx-status.bad { color:var(--bad); font-weight:600; } .obx-status.ok { color:var(--ok); font-weight:600; }
  .obx-table { width:100%; border-collapse:collapse; font-size:13px; }
  .obx-table th { text-align:left; color:var(--ink-2); font-weight:600; padding:8px; border-bottom:1px solid var(--line); }
  .obx-table td { padding:10px 8px; border-bottom:1px solid var(--line-soft); vertical-align:top; }
  .obx-scroll { overflow-x:auto; }
  .obx-two { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:14px; }
  .obx-box { border:1px solid var(--line); border-radius:12px; padding:14px; }
  .obx-box h4 { margin:0 0 8px; }
  .obx-box > * + * { margin-top:8px; }
  .obx-modal { position:fixed; inset:0; z-index:60; background:rgba(0,0,0,.4); display:grid; place-items:center; padding:16px; }
  .obx-dialog { background:#fff; border-radius:18px; width:min(760px,100%); max-height:calc(100vh - 32px); overflow:auto; padding:24px; }
  .obx-section-title { margin:18px 0 0; font-size:16px; }
  .obx-escalation { border:1px solid #F3C4B8; background:#FFF6F4; border-radius:12px; padding:12px; margin-top:10px; font-size:13px; }
  .obx-audit { font-size:12px; max-height:260px; overflow:auto; }
  .obx-audit div { padding:6px 0; border-bottom:1px solid var(--line-soft); }
  @media (max-width: 860px) {
    .obx-layout { grid-template-columns:1fr; }
    .obx-stepper { position:static; flex-direction:row; overflow-x:auto; padding-bottom:4px; }
    .obx-stepper li { flex:none; }
    .obx-stepper button { width:auto; }
    .obx-stepper small { display:none; }
    .obx-form, .obx-two { grid-template-columns:1fr; }
    .obx-panel { padding:18px; }
    .obx-story { font-size:20px; }
  }`;
  document.head.appendChild(Object.assign(document.createElement("style"), { textContent: css }));

  const E = (tag, cls, text) => { const n = document.createElement(tag); if (cls) n.className = cls; if (text != null) n.textContent = text; return n; };
  const PARTY = { tenant: "Tenant", owner: "Owner" };
  const STAFF_KEY = "staybot.onboarding.actingStaff";
  const S = { setup: null, options: null, cases: [], view: null, step: 1, filter: "active", staff: "" };
  try { S.staff = localStorage.getItem(STAFF_KEY) || ""; } catch (e) { /* convenience only */ }

  async function api(method, url, body, raw) {
    const headers = { "X-Staybot-Staff": S.staff };
    const init = { method, headers };
    if (raw) { init.body = raw.data; headers["Content-Type"] = raw.type; headers["X-File-Name"] = raw.name; }
    else if (body !== undefined) { init.body = JSON.stringify(body); headers["Content-Type"] = "application/json"; }
    const res = await fetch(url, init);
    let data = null;
    try { data = await res.json(); } catch (e) { /* not JSON */ }
    if (!res.ok) {
      const d = data && data.detail;
      const msg = typeof d === "string" ? d : Array.isArray(d) ? d.map((x) => `${(x.loc || []).slice(-1)[0]}: ${String(x.msg).replace(/^Value error, /, "")}`).join("; ")
        : res.status === 401 ? "Please sign in to the team console again." : `Request failed (${res.status}).`;
      const err = new Error(msg); err.status = res.status; throw err;
    }
    return data;
  }

  const when = (iso) => iso ? new Date(iso).toLocaleString([], { dateStyle: "medium", timeStyle: "short" }) : "";
  const chip = (text, tone) => E("span", "obx-chip " + (tone || ""), text);
  function decisionChip(party, d) {
    const map = { approved: ["Approved", "ok"], changes_requested: ["Changes requested", "bad"], pending: ["Pending", "warn"] };
    const [label, tone] = map[d.status] || map.pending;
    return chip(`${PARTY[party]}: ${label}`, tone);
  }
  function deliveryChip(m) {
    const label = { accepted: "Accepted by WhatsApp", queued: "Queued", sending: "Sending", sent: "Sent", delivered: "Delivered", read: "Read", failed: "Failed" }[m.status];
    if (m.test_mode) return chip(`${PARTY[m.party]}: Simulated · ${m.status === "accepted" ? "not really sent" : label} (test mode)`, m.status === "failed" ? "bad" : "test");
    const tone = { queued: "info", sending: "info", accepted: "info", sent: "info", delivered: "ok", read: "ok", failed: "bad" }[m.status];
    return chip(`${PARTY[m.party]}: ${label}`, tone);
  }
  const actorLabel = (a) => ({ staff: "Staff", nobody: "No one", whatsapp: "WhatsApp" })[a] || a.replace("owner & tenant", "Owner and tenant").replace(/^./, (x) => x.toUpperCase());
  function statusLine(node, text, tone) { node.textContent = text || ""; node.className = "obx-status " + (tone || ""); }
  function root() { return document.getElementById("obx-root"); }

  // ------------------------------------------------------------------
  // List
  // ------------------------------------------------------------------
  async function load() {
    const box = root(); if (!box) return;
    if (!S.view) box.replaceChildren(E("p", "obx-muted", "Loading onboarding cases…"));
    try {
      const [setup, options] = await Promise.all([api("GET", "/onboarding/cases/setup"), api("GET", "/onboarding/cases/options")]);
      S.setup = setup; S.options = options;
      if (S.view) { await openCase(S.view.id, S.step); return; }
      S.cases = await api("GET", "/onboarding/cases");
      renderList();
    } catch (err) {
      box.replaceChildren(E("div", "obx-banner bad", err.message));
    }
  }

  function header(title, sub) {
    const head = E("div", "obx-head");
    const left = E("div"); left.append(E("h2", null, title), E("p", "obx-muted", sub));
    const actions = E("div", "obx-actions");
    const label = E("label", "obx-field obx-small"); label.style.minWidth = "220px";
    label.append(E("span", null, "Acting as (for reviews and the audit trail)"));
    const select = E("select"); select.setAttribute("aria-label", "Acting staff member");
    select.add(new Option("Choose staff member…", ""));
    for (const s of (S.options?.staff || [])) select.add(new Option(s.name, s.name));
    select.value = S.staff;
    select.onchange = () => { S.staff = select.value; try { localStorage.setItem(STAFF_KEY, S.staff); } catch (e) { /* ignore */ } };
    label.append(select);
    actions.append(label);
    head.append(left, actions);
    return head;
  }

  function banners() {
    const wrap = E("div", "obx-banners");
    const s = S.setup; if (!s) return wrap;
    const add = (tone, title, text) => { const b = E("div", "obx-banner " + tone); b.append(E("b", null, title), E("span", null, text)); wrap.append(b); };
    add(s.whatsapp.mode === "live" ? "" : "warn", `WhatsApp: ${s.whatsapp.mode === "live" ? "Live" : "Test mode"}`,
      s.whatsapp.mode === "live" ? "Messages are really sent. Delivery comes from WhatsApp status events." : "Nothing is sent. Deliveries are simulated and clearly labelled.");
    add(s.templates.approved_available ? "" : "bad", `Lease template: ${s.templates.approved_available ? "Approved template available" : "Setup required"}`,
      s.templates.message || "Agreements use a staff-approved template.");
    add("warn", "TurboTenant: Not connected", "Use the manual handoff on the final step. Nothing is synced automatically.");
    add("warn", "E-signatures: Not connected", "Both parties approve the agreement; PDFs keep blank hand-signature lines.");
    return wrap;
  }

  function renderList() {
    const box = root(); box.replaceChildren();
    box.append(header("Resident onboarding", "Assign the home, tenant and owner, collect details step by step, agree the lease and deliver the final PDF on WhatsApp."));
    box.append(banners());
    const bar = E("div", "obx-head");
    const filters = E("div", "obx-actions");
    for (const [key, label] of [["active", "In progress"], ["finalized", "Finalized"], ["cancelled", "Cancelled"], ["all", "All"]]) {
      const b = E("button", "obx-btn small" + (S.filter === key ? " dark" : ""), label); b.type = "button";
      b.onclick = () => { S.filter = key; renderList(); };
      filters.append(b);
    }
    const right = E("div", "obx-actions");
    const refresh = E("button", "obx-btn", "Refresh"); refresh.type = "button"; refresh.onclick = () => load();
    const settings = E("button", "obx-btn", "Templates & checklist"); settings.type = "button"; settings.onclick = () => openSettings();
    const start = E("button", "obx-btn primary", "+ Start onboarding"); start.type = "button"; start.onclick = () => openStart();
    right.append(refresh, settings, start);
    bar.append(filters, right);
    box.append(bar);

    const rows = S.cases.filter((c) => S.filter === "all" || c.status === S.filter);
    if (!rows.length) {
      const empty = E("div", "lv-empty");
      empty.append(E("p", null, S.filter === "active" ? "No onboarding in progress." : "No cases here."),
        E("p", "obx-muted", "Start onboarding from the button above or from a lead."));
      box.append(empty);
    }
    const grid = E("div", "obx-cards");
    for (const c of rows) grid.append(caseCard(c));
    box.append(grid);
    metricsLine(box);
    legacyRecords(box);
  }

  function caseCard(c) {
    const card = E("button", "obx-card"); card.type = "button";
    card.onclick = () => openCase(c.id, firstOpenStep(c));
    const top = E("div", "obx-row");
    top.append(chip(c.reference), chip(c.status === "active" ? "In progress" : c.status === "finalized" ? "Finalized" : "Cancelled", c.status === "finalized" ? "ok" : c.status === "cancelled" ? "" : "info"));
    if (c.is_test) top.append(chip("DEMO / test", "test"));
    card.append(top, E("h3", null, `${c.property.title}${c.unit ? " · Unit " + c.unit : ""}`));
    const dl = E("dl", "obx-dl");
    const pair = (k, v) => dl.append(E("dt", null, k), E("dd", null, v));
    pair("Tenant", c.tenant ? c.tenant.full_name : "Not assigned");
    pair("Owner", c.owner ? c.owner.full_name : "Not assigned");
    pair("Staff", c.staff ? c.staff.name : "Not assigned");
    card.append(dl);
    const step = firstOpenStep(c);
    card.append(E("div", "obx-small obx-muted", `Step ${step} of 7 · ${c.completed_steps} of 7 steps complete · ${c.missing_count} item(s) missing`));
    const bar = E("div", "obx-progress"); const fill = E("i"); fill.style.width = `${Math.round(100 * c.completed_steps / 7)}%`; bar.append(fill); card.append(bar);
    const approvals = E("div", "obx-row");
    approvals.append(decisionChip("tenant", c.agreement.approvals.tenant), decisionChip("owner", c.agreement.approvals.owner));
    approvals.append(c.final_document ? chip("Final PDF ready", "ok") : chip("No final PDF"));
    card.append(approvals);
    const finals = c.whatsapp.filter((m) => m.kind === "final_pdf");
    if (finals.length) { const w = E("div", "obx-row"); finals.forEach((m) => w.append(deliveryChip(m))); card.append(w); }
    const next = E("div", "obx-next"); next.append(E("b", null, `Next: ${actorLabel(c.next_actor)}`), document.createTextNode(` — ${c.next_action}`));
    card.append(next);
    if (c.open_escalations.length) card.append(chip(`${c.open_escalations.length} request(s) for staff help`, "bad"));
    return card;
  }

  async function metricsLine(box) {
    try {
      const m = await api("GET", "/onboarding/cases/metrics");
      const p = E("p", "obx-muted obx-small"); p.style.marginTop = "18px";
      p.textContent = m.invited_parties
        ? `WhatsApp self-service completion (real cases only): ${m.completed_without_staff} of ${m.invited_parties} invited people (${m.completion_rate_percent}%). Target ${m.target_percent}%, measured from real usage.`
        : `WhatsApp self-service completion: no real (non-test) invitations yet. The ${m.target_percent}% target will be measured from real usage.`;
      box.append(p);
    } catch (e) { /* metrics are optional */ }
  }

  function legacyRecords(box) {
    const details = E("details"); details.style.marginTop = "24px";
    details.append(E("summary", "obx-muted", "Earlier onboarding records (before guided cases)"));
    const holder = E("div");
    details.append(holder);
    details.addEventListener("toggle", async () => {
      if (!details.open || holder.dataset.loaded) return;
      holder.dataset.loaded = "1";
      try {
        const rows = await api("GET", "/onboarding");
        if (!rows.length) { holder.append(E("p", "obx-muted", "No earlier records.")); return; }
        for (const r of rows) holder.append(E("div", "obx-next", `${r.data.resident_name || "Resident pending"} · ${r.property.name} (Property ID ${r.property.id}) · ${r.tenant ? "Tenant " + r.tenant.tenant_ref : r.status}`));
      } catch (err) { holder.append(E("p", "obx-muted", err.message)); }
    });
    box.append(details);
  }

  function firstOpenStep(c) {
    if (c.status === "finalized") return 7;
    const open = c.steps.find((s) => !s.complete && s.number !== 4);
    return Math.max(1, Math.min(open ? open.number : 7, c.current_step || 1));
  }

  // ------------------------------------------------------------------
  // Start a case
  // ------------------------------------------------------------------
  function openStart(prefill) {
    prefill = prefill || {};
    const modal = E("div", "obx-modal obx"); modal.setAttribute("role", "dialog"); modal.setAttribute("aria-modal", "true"); modal.setAttribute("aria-label", "Start onboarding");
    const dialog = E("div", "obx-dialog"); modal.append(dialog);
    const close = () => modal.remove();
    modal.addEventListener("click", (e) => { if (e.target === modal) close(); });
    dialog.append(E("div", "obx-kicker", "New onboarding"), E("div", "obx-story", "Let's choose the home."),
      E("p", "obx-muted", "Pick the home and the people involved. You can fill in the rest step by step. Nothing is sent to anyone yet."));
    const form = E("form", "obx-form");
    const field = (label, control, hint, full) => { const l = E("label", full ? "full" : null); l.append(E("span", null, label), control); if (hint) l.append(E("small", null, hint)); form.append(l); return control; };

    const homeSelect = E("select");
    homeSelect.add(new Option("Choose a stored rental home…", ""));
    for (const p of S.options.properties) homeSelect.add(new Option(`${p.title} · ${p.id}${p.status !== "active" ? " (" + p.status + ")" : ""}`, p.id));
    homeSelect.add(new Option("+ Add a new home", "__new"));
    field("Home", homeSelect, "Only rental homes that are not already let or sold are listed.", true);
    const newWrap = E("div", "full obx-form"); newWrap.style.marginTop = "0"; newWrap.hidden = true;
    const newId = Object.assign(E("input"), { type: "text", placeholder: "e.g. oak-street-12" });
    const newTitle = Object.assign(E("input"), { type: "text", placeholder: "e.g. 12 Oak Street townhome" });
    const newCity = Object.assign(E("input"), { type: "text", placeholder: "e.g. Charlotte" });
    for (const [label, control, hint] of [["Property identifier", newId, "Lowercase letters, numbers and dashes."], ["Home name", newTitle], ["City", newCity]]) {
      const l = E("label"); l.append(E("span", null, label), control); if (hint) l.append(E("small", null, hint)); newWrap.append(l);
    }
    form.append(newWrap);
    const unit = field("Unit", Object.assign(E("input"), { type: "text", placeholder: "e.g. 2B (leave empty if none)", maxLength: 40 }));
    const address = field("Full address", Object.assign(E("input"), { type: "text", placeholder: "Street, unit, city, state, ZIP", maxLength: 300 }));

    function personPicker(role, prefillPerson) {
      form.append(E("h4", "obx-section-title full", role === "tenant" ? "Let's meet the tenant." : "Let's confirm the owner."));
      const pick = E("select");
      pick.add(new Option(`Decide later`, ""));
      for (const p of S.options.people) pick.add(new Option(`${p.full_name} · ${p.whatsapp || "no WhatsApp"}`, p.id));
      pick.add(new Option(`+ Add a new person`, "__new"));
      field(PARTY[role], pick, "Existing records are reused so nobody is duplicated.", true);
      const name = field(`${PARTY[role]} full name`, Object.assign(E("input"), { type: "text", maxLength: 200 }));
      const phone = field(`${PARTY[role]} WhatsApp`, Object.assign(E("input"), { type: "tel", placeholder: "+1 704 555 0101" }));
      const toggle = () => { const isNew = pick.value === "__new"; name.parentElement.hidden = phone.parentElement.hidden = !isNew; };
      pick.onchange = toggle;
      if (prefillPerson) {
        const digits = (prefillPerson.whatsapp || "").replace(/\D/g, "");
        const match = digits && S.options.people.find((p) => (p.whatsapp || "").replace(/\D/g, "") === digits);
        if (match) pick.value = match.id;
        else if (digits || prefillPerson.full_name) { pick.value = "__new"; name.value = prefillPerson.full_name || ""; phone.value = prefillPerson.whatsapp || ""; }
      }
      toggle();
      return { pick, name, phone };
    }
    const tenant = personPicker("tenant", prefill.tenant);
    const owner = personPicker("owner", null);
    homeSelect.onchange = () => {
      newWrap.hidden = homeSelect.value !== "__new";
      const p = S.options.properties.find((x) => x.id === homeSelect.value);
      if (p && !address.value) address.value = p.location || "";
      if (p && p.owner_phone && !owner.pick.value) {
        const digits = p.owner_phone.replace(/\D/g, "");
        const match = S.options.people.find((x) => (x.whatsapp || "").replace(/\D/g, "").endsWith(digits.slice(-10)));
        if (match) owner.pick.value = match.id; else { owner.pick.value = "__new"; owner.name.value = p.owner_name || ""; owner.phone.value = p.owner_phone; }
        owner.pick.onchange();
      }
    };
    if (prefill.property_id && S.options.properties.some((p) => p.id === prefill.property_id)) { homeSelect.value = prefill.property_id; homeSelect.onchange(); }

    form.append(E("h4", "obx-section-title full", "Who is responsible?"));
    const staffSelect = E("select");
    staffSelect.add(new Option("Choose staff member…", ""));
    for (const s of S.options.staff) staffSelect.add(new Option(s.name, s.id));
    staffSelect.add(new Option("+ Add a staff member", "__new"));
    field("Responsible staff member", staffSelect);
    const staffName = field("New staff member name", Object.assign(E("input"), { type: "text", maxLength: 120 }));
    staffName.parentElement.hidden = true;
    staffSelect.onchange = () => { staffName.parentElement.hidden = staffSelect.value !== "__new"; };
    const testLabel = E("label", "obx-check full");
    const isTest = Object.assign(E("input"), { type: "checkbox", checked: !!prefill.is_test });
    testLabel.append(isTest, E("span", null, "This is a demo/test case (never messages real phones and is left out of completion metrics)"));
    form.append(testLabel);

    const status = E("div", "obx-status full"); status.setAttribute("role", "status");
    const foot = E("div", "obx-foot full");
    const cancel = E("button", "obx-btn", "Cancel"); cancel.type = "button"; cancel.onclick = close;
    const submit = E("button", "obx-btn primary", "Start onboarding"); submit.type = "submit";
    foot.append(cancel, submit);
    form.append(status, foot);
    dialog.append(form);
    document.body.append(modal);
    homeSelect.focus();

    form.onsubmit = async (e) => {
      e.preventDefault();
      submit.disabled = true; statusLine(status, "Starting…");
      try {
        const personId = async (picker, role) => {
          if (picker.pick.value !== "__new") return picker.pick.value || null;
          if (!picker.name.value.trim() || !picker.phone.value.trim()) throw new Error(`Enter the ${role}'s name and WhatsApp number, or choose “Decide later”.`);
          return (await api("POST", "/onboarding/cases/people", { full_name: picker.name.value, whatsapp: picker.phone.value, is_test: isTest.checked })).id;
        };
        const body = { unit: unit.value, property_address: address.value, is_test: isTest.checked, conversation_id: prefill.conversation_id || null };
        if (homeSelect.value === "__new") body.new_property = { id: newId.value.trim(), title: newTitle.value.trim(), city: newCity.value.trim() };
        else if (homeSelect.value) body.property_id = homeSelect.value;
        else throw new Error("Choose the home first.");
        body.tenant_id = await personId(tenant, "tenant");
        body.owner_id = await personId(owner, "owner");
        if (staffSelect.value === "__new") body.staff_id = staffName.value.trim() ? (await api("POST", "/onboarding/cases/staff", { name: staffName.value })).id : null;
        else body.staff_id = staffSelect.value || null;
        const created = await api("POST", "/onboarding/cases", body);
        close();
        S.options = await api("GET", "/onboarding/cases/options");
        openCase(created.id, firstOpenStep(created));
      } catch (err) {
        statusLine(status, err.message, "bad");
        const ref = err.status === 409 && (err.message.match(/OB-\d+/) || [])[0];
        if (ref) {
          const existing = (await api("GET", "/onboarding/cases?status=active")).find((c) => c.reference === ref);
          if (existing) { const open = E("button", "obx-link", ` Open ${ref}`); open.type = "button"; open.onclick = () => { close(); openCase(existing.id, firstOpenStep(existing)); }; status.append(open); }
        }
      } finally { submit.disabled = false; }
    };
  }

  function startFromLead(lead) {
    if (typeof showView === "function") showView("onboarding");
    const phone = (lead.session_id || "").startsWith("wa:") ? lead.session_id.slice(3) : "";
    const title = lead.listing_title || "";
    const name = title.startsWith("WhatsApp · ") ? title.slice(11) : "";
    const go = () => openStart({ property_id: lead.listing_id, conversation_id: lead.id,
      tenant: phone || name ? { whatsapp: phone, full_name: /^\+?\d/.test(name) ? "" : name } : null });
    const ready = S.options ? Promise.resolve() : Promise.all([api("GET", "/onboarding/cases/options"), api("GET", "/onboarding/cases/setup")]).then(([o, s]) => { S.options = o; S.setup = s; });
    ready.then(go).catch((err) => alert(err.message));
  }

  // ------------------------------------------------------------------
  // Case detail
  // ------------------------------------------------------------------
  async function openCase(id, step) {
    const box = root();
    try {
      if (!S.options) { const [o, s] = await Promise.all([api("GET", "/onboarding/cases/options"), api("GET", "/onboarding/cases/setup")]); S.options = o; S.setup = s; }
      S.view = await api("GET", `/onboarding/cases/${id}`);
      S.step = step || firstOpenStep(S.view);
      renderCase();
      window.scrollTo(0, 0);
    } catch (err) { box.replaceChildren(E("div", "obx-banner bad", err.message)); S.view = null; }
  }

  function renderCase(message, tone, extraNode) {
    const c = S.view, box = root();
    box.replaceChildren();
    const back = E("button", "obx-link", "← All onboarding cases"); back.type = "button";
    back.onclick = async () => { S.view = null; await load(); };
    box.append(back);
    box.append(header(`${c.reference} · ${c.property.title}${c.unit ? " · Unit " + c.unit : ""}`,
      `${c.tenant ? c.tenant.full_name : "Tenant not assigned"} (tenant) · ${c.owner ? c.owner.full_name : "Owner not assigned"} (owner) · ${c.staff ? "Staff: " + c.staff.name : "No staff assigned"}`));
    const chips = E("div", "obx-row");
    chips.append(chip(c.status === "active" ? "In progress" : c.status === "finalized" ? "Finalized" : "Cancelled", c.status === "finalized" ? "ok" : "info"));
    if (c.is_test) chips.append(chip("DEMO / test case", "test"));
    chips.append(decisionChip("tenant", c.agreement.approvals.tenant), decisionChip("owner", c.agreement.approvals.owner));
    chips.append(c.final_document ? chip("Final PDF ready", "ok") : chip("No final PDF yet"));
    box.append(chips);
    const next = E("div", "obx-next"); next.style.margin = "14px 0 18px";
    next.append(E("b", null, `Who acts next: ${actorLabel(c.next_actor)}`), document.createTextNode(` — ${c.next_action}`));
    box.append(next);

    const layout = E("div", "obx-layout");
    const stepper = E("ol", "obx-stepper"); stepper.setAttribute("aria-label", "Onboarding steps");
    for (const s of c.steps) {
      const li = E("li"); const b = E("button", s.number === S.step ? "on" : ""); b.type = "button";
      if (s.number === S.step) b.setAttribute("aria-current", "step");
      const num = E("span", "obx-num" + (s.complete ? " done" : ""), s.complete ? "✓" : String(s.number));
      const text = E("span"); text.append(document.createTextNode(`${s.number}. ${STEP_SHORT[s.key]}`));
      text.append(E("small", null, s.complete ? "Complete" : `${s.missing.length} still needed`));
      b.append(num, text); b.onclick = () => { S.step = s.number; renderCase(); };
      li.append(b); stepper.append(li);
    }
    layout.append(stepper);
    const main = E("div");
    const panel = E("section", "obx-panel obx");
    const s = c.steps[S.step - 1];
    panel.append(E("div", "obx-kicker", `Step ${s.number} of 7`), E("h3", "obx-story", s.title));
    if (s.missing.length) {
      const m = E("div", "obx-missing"); m.append(E("b", null, "Still needed")); const ul = E("ul"); s.missing.forEach((x) => ul.append(E("li", null, x))); m.append(ul); panel.append(m);
    } else panel.append(E("div", "obx-good", "Everything needed for this step is in place."));
    const status = E("div", "obx-status"); status.setAttribute("role", "status");
    const locked = c.status !== "active";
    const renderers = { 1: stepHome, 2: (p, st, l) => stepPerson("tenant", p, st, l), 3: (p, st, l) => stepPerson("owner", p, st, l), 4: stepDocuments, 5: stepTerms, 6: stepReview, 7: stepFinal };
    renderers[S.step](panel, status, locked);
    if (message) { statusLine(status, message, tone); if (extraNode) status.append(extraNode); }
    panel.insertBefore(status, panel.querySelector(".obx-foot"));
    main.append(panel, helpPanel(), auditPanel());
    layout.append(main);
    box.append(layout);
    if (message) status.scrollIntoView({ block: "nearest" });
  }
  const STEP_SHORT = { home: "Home", tenant: "Tenant", owner: "Owner", documents: "Documents", terms: "Rental terms", review: "Review & approvals", final: "Final lease PDF" };

  async function refreshCase(message, tone) {
    S.view = await api("GET", `/onboarding/cases/${S.view.id}`);
    renderCase(message, tone);
  }

  function footer(panel, status, locked, save) {
    const foot = E("div", "obx-foot");
    const back = E("button", "obx-btn", "← Back"); back.type = "button"; back.disabled = S.step === 1;
    back.onclick = () => { S.step -= 1; renderCase(); };
    const right = E("div", "obx-actions");
    if (save && !locked) {
      const saveOnly = E("button", "obx-btn", "Save"); saveOnly.type = "button";
      const cont = E("button", "obx-btn primary", "Save and continue →"); cont.type = "button";
      const run = async (advance, btn) => {
        btn.disabled = true; statusLine(status, "Saving…");
        try {
          const body = { version: S.view.version, advance, ...(await save()) };
          S.view = await api("PUT", `/onboarding/cases/${S.view.id}/steps/${S.step}`, body);
          if (advance) S.step = Math.min(7, S.step + 1);
          renderCase(advance ? "Saved. On to the next step." : "Progress saved.", "ok");
        } catch (err) {
          statusLine(status, err.message, "bad");
          if (err.status === 409) { const r = E("button", "obx-link", " Reload latest"); r.type = "button"; r.onclick = () => refreshCase(); status.append(r); }
        } finally { btn.disabled = false; }
      };
      saveOnly.onclick = () => run(false, saveOnly);
      cont.onclick = () => run(true, cont);
      right.append(saveOnly, cont);
    } else if (S.step < 7) {
      const nextB = E("button", "obx-btn dark", "Next step →"); nextB.type = "button"; nextB.onclick = () => { S.step += 1; renderCase(); }; right.append(nextB);
    }
    foot.append(back, right);
    panel.append(foot);
  }

  function input(type, value, attrs) { const i = E("input"); i.type = type; if (type !== "file" && type !== "checkbox") i.value = value ?? ""; Object.assign(i, attrs || {}); return i; }
  function labelled(form, label, control, hint, full) { const l = E("label", full ? "full" : null); l.append(E("span", null, label), control); if (hint) l.append(E("small", null, hint)); form.append(l); return control; }
  function select(options, value) { const s = E("select"); for (const [v, t] of options) s.add(new Option(t, v)); s.value = value ?? ""; return s; }
  function lockForm(form, locked) { if (locked) form.querySelectorAll("input,select,textarea,button").forEach((x) => { x.disabled = true; }); }

  function stepHome(panel, status, locked) {
    const c = S.view;
    panel.append(E("p", "obx-muted", "Confirm which home this lease is for. The property identifier stays fixed for the case."));
    const form = E("div", "obx-form");
    const dl = E("dl", "obx-dl full");
    for (const [k, v] of [["Home", c.property.title], ["Property ID", c.property.id + (c.property.ref ? " · ref " + c.property.ref : "")], ["Listing status", c.property.status]]) dl.append(E("dt", null, k), E("dd", null, v));
    form.append(dl);
    const unit = labelled(form, "Unit", input("text", c.unit, { maxLength: 40 }), "Leave empty if the home has no unit number.");
    const address = labelled(form, "Full address", input("text", c.property_address, { maxLength: 300 }), "As it should appear on the lease.");
    const staff = labelled(form, "Responsible staff member", select([["", "Choose…"], ...S.options.staff.map((s) => [s.id, s.name])], c.staff?.id), null, true);
    lockForm(form, locked);
    panel.append(form);
    footer(panel, status, locked, async () => ({ unit: unit.value, property_address: address.value, staff_id: staff.value || null }));
  }

  function stepPerson(role, panel, status, locked) {
    const c = S.view, person = c[role], consent = (c.consent || {})[role] || {};
    panel.append(E("p", "obx-muted", role === "tenant"
      ? "Who will live in the home and sign the lease? Choose an existing record or add their details."
      : "Who owns the home (or is authorised to rent it)? Check the details before the agreement is prepared."));
    const form = E("div", "obx-form");
    const pick = labelled(form, `${PARTY[role]} record`, select([["", person ? `Keep ${person.full_name}` : "Enter new details below"],
      ...S.options.people.filter((p) => !person || p.id !== person.id).map((p) => [p.id, `Use existing: ${p.full_name} · ${p.whatsapp || ""}`])]),
      "Switching to another person revokes the previous secure link and needs consent again.", true);
    const name = labelled(form, "Full legal name", input("text", person?.full_name, { maxLength: 200, autocomplete: "off" }));
    const phone = labelled(form, "WhatsApp number", input("tel", person?.whatsapp, { placeholder: "+1 704 555 0101" }), "US numbers can be typed without +1.");
    const email = labelled(form, "Email (optional)", input("email", person?.email, { maxLength: 200 }));
    const lang = labelled(form, "Preferred language", select([["en", "English"], ["es", "Español"]], person?.preferred_language || "en"));
    const channel = labelled(form, "Keep in touch by", select([["whatsapp", "WhatsApp only"], ["whatsapp_and_email", "WhatsApp and email"]], person?.communication?.channel || "whatsapp"));
    const best = labelled(form, "Best time to contact", select([["any", "Any time"], ["morning", "Morning"], ["afternoon", "Afternoon"], ["evening", "Evening"]], person?.communication?.best_time || "any"));
    pick.onchange = () => { [name, phone, email, lang, channel, best].forEach((x) => { x.disabled = !!pick.value || locked; }); };
    const consentBox = E("div", "full obx-box");
    consentBox.append(E("h4", null, "WhatsApp consent"), E("p", "obx-muted obx-small", "WhatsApp Business rules require the person's permission before we message them."));
    const optLabel = E("label", "obx-check"); const opt = input("checkbox"); opt.checked = !!consent.whatsapp_opt_in;
    optLabel.append(opt, E("span", null, `The ${role} agreed to receive onboarding messages on WhatsApp`));
    const source = select([["", "How did they agree?"], ...Object.entries(S.options.consent_sources)], consent.source || "");
    source.setAttribute("aria-label", "How they agreed");
    consentBox.append(optLabel, source);
    if (consent.recorded_by) consentBox.append(E("p", "obx-muted obx-small", `Recorded by ${consent.recorded_by.replace("staff:", "")} on ${when(consent.recorded_at)}`));
    form.append(consentBox);
    lockForm(form, locked);
    panel.append(form);

    if (person) {
      const invite = E("div", "obx-box"); invite.style.marginTop = "16px";
      const progress = c.party_progress[role] || {};
      const invites = c.whatsapp.filter((m) => m.kind === "invite" && m.party === role);
      invite.append(E("h4", null, `Invite the ${role} to self-serve on WhatsApp`),
        E("p", "obx-muted obx-small", "Sends a personal secure link (no account or password). They confirm their own details, upload documents and review the agreement. A new invitation replaces the previous link."));
      const row = E("div", "obx-row");
      if (invites.length) row.append(deliveryChip(invites[invites.length - 1]));
      if (progress.invited_at) row.append(chip(`Invited ${when(progress.invited_at)}`));
      if (progress.first_response_at) row.append(chip("Responded", "ok"));
      if (progress.self_service_done_at) row.append(chip("Self-service complete", "ok"));
      if (row.children.length) invite.append(row);
      const send = E("button", "obx-btn dark small", invites.length ? "Send a new invitation" : "Send WhatsApp invitation"); send.type = "button"; send.disabled = locked;
      const out = E("div", "obx-status");
      send.onclick = async () => {
        send.disabled = true; statusLine(out, "Sending…");
        try {
          const r = await api("POST", `/onboarding/cases/${c.id}/invite`, { party: role });
          S.view = r.case;
          let extra = null;
          if (r.link) { extra = E("a", null, ` Open the ${role}'s test link`); extra.href = r.link; extra.target = "_blank"; extra.rel = "noopener"; }
          renderCase(r.message.status === "failed" ? `Invitation failed: ${r.message.error}` :
            r.message.test_mode ? "Test mode: invitation simulated, nothing was sent." : "Invitation accepted by WhatsApp. Delivery updates when WhatsApp confirms.",
          r.message.status === "failed" ? "bad" : "ok", extra);
        } catch (err) { statusLine(out, err.message, "bad"); send.disabled = false; }
      };
      invite.append(send, out);
      panel.append(invite);
    }
    footer(panel, status, locked, async () => {
      const body = { consent: { whatsapp_opt_in: opt.checked, source: source.value || null } };
      if (pick.value) body.person_id = pick.value;
      else if (name.value.trim() || phone.value.trim()) body.person = { full_name: name.value, whatsapp: phone.value, email: email.value || null, preferred_language: lang.value, communication: { channel: channel.value, best_time: best.value } };
      return body;
    });
  }

  function stepDocuments(panel, status, locked) {
    const c = S.view;
    panel.append(E("p", "obx-muted", "Each document is checked by a staff member. Uploading a file never marks it accepted. Tenants and owners upload with their secure link, or staff can upload for them."));
    const wrap = E("div", "obx-scroll"); const table = E("table", "obx-table");
    const thead = E("thead"); const hr = E("tr"); ["For", "Document and why it's needed", "Status", "Actions"].forEach((h) => hr.append(E("th", null, h))); thead.append(hr); table.append(thead);
    const tbody = E("tbody");
    const labels = { not_uploaded: ["Not uploaded", ""], awaiting_review: ["Uploaded · awaiting review", "warn"], accepted: ["Accepted", "ok"], changes_required: ["Changes required", "bad"] };
    for (const d of c.documents) {
      const tr = E("tr");
      tr.append(E("td", null, PARTY[d.party]));
      const what = E("td"); what.append(E("b", null, d.label + (d.required ? "" : " (optional)")), E("div", "obx-muted obx-small", d.reason));
      if (d.latest) what.append(E("div", "obx-small", `${d.latest.file_name} · uploaded by ${d.latest.uploaded_by.split(":")[0]} ${when(d.latest.created_at)}`));
      if (d.latest?.review_note) what.append(E("div", "obx-small", `Note: ${d.latest.review_note}`));
      tr.append(what);
      const st = E("td"); const [label, tone] = labels[d.status]; st.append(chip(label, tone));
      if (d.latest?.reviewed_by) st.append(E("div", "obx-muted obx-small", `by ${d.latest.reviewed_by.replace("staff:", "")}`));
      tr.append(st);
      const actions = E("td"); const acts = E("div", "obx-actions");
      const file = input("file"); file.accept = "application/pdf,image/jpeg,image/png"; file.hidden = true;
      const up = E("button", "obx-btn small", d.latest ? "Upload new file" : "Upload for them"); up.type = "button"; up.onclick = () => file.click();
      file.onchange = async () => {
        const f = file.files[0]; if (!f) return;
        statusLine(status, `Uploading ${f.name}…`);
        try { const r = await api("POST", `/onboarding/cases/${c.id}/uploads/${d.party}/${d.doc_key}`, undefined, { data: f, type: f.type || "application/octet-stream", name: f.name });
          S.view = r.case; renderCase("Uploaded. It now needs a staff review.", "ok"); } catch (err) { statusLine(status, err.message, "bad"); }
      };
      acts.append(up, file);
      if (d.latest) { const view = E("a", "obx-btn small", "View file"); view.href = `/onboarding/cases/${c.id}/documents/${d.latest.id}/file`; view.target = "_blank"; view.rel = "noopener"; acts.append(view); }
      if (d.status === "awaiting_review") {
        const ok = E("button", "obx-btn small dark", "Accept"); ok.type = "button";
        const no = E("button", "obx-btn small", "Changes required"); no.type = "button";
        const review = async (decision) => {
          if (!S.staff) { statusLine(status, "Choose who you are in “Acting as” at the top first.", "bad"); return; }
          let note = "";
          if (decision === "changes_required") { note = prompt("What needs to change? (shown to the person)") || ""; if (!note.trim()) return; }
          try { S.view = await api("POST", `/onboarding/cases/${c.id}/documents/${d.latest.id}/review`, { decision, note }); renderCase(decision === "accepted" ? "Document accepted." : "Marked as changes required.", "ok"); }
          catch (err) { statusLine(status, err.message, "bad"); }
        };
        ok.onclick = () => review("accepted"); no.onclick = () => review("changes_required");
        acts.append(ok, no);
      }
      if (locked) acts.querySelectorAll("button").forEach((b) => { b.disabled = true; });
      actions.append(acts); tr.append(actions); tbody.append(tr);
    }
    if (!c.documents.length) { const tr = E("tr"); const td = E("td", "obx-muted", "No documents are required for this home and jurisdiction."); td.colSpan = 4; tr.append(td); tbody.append(tr); }
    table.append(tbody); wrap.append(table); panel.append(wrap);
    panel.append(E("p", "obx-muted obx-small", "You can move on to the rental terms while documents are collected. The lease cannot be finalized until every required document is accepted. Change the checklist in “Templates & checklist”."));
    footer(panel, status, locked, null);
  }

  function stepTerms(panel, status, locked) {
    const c = S.view, t = c.terms, p = c.preferences;
    panel.append(E("p", "obx-muted", "Enter exactly what the tenant and owner agreed. These values go into the agreement word for word. Nothing is filled in automatically."));
    const form = E("div", "obx-form");
    const start = labelled(form, "Lease start", input("date", t.lease_start));
    const end = labelled(form, "Lease end", input("date", t.lease_end));
    const movein = labelled(form, "Move-in date", input("date", t.move_in_date));
    const currency = labelled(form, "Currency", select([["USD", "USD (US dollar)"]], t.currency || "USD"));
    const rent = labelled(form, "Monthly rent", input("number", t.rent, { min: "0.01", step: "0.01", inputMode: "decimal" }));
    const deposit = labelled(form, "Security deposit", input("number", t.deposit, { min: "0", step: "0.01", inputMode: "decimal" }));
    const schedule = labelled(form, "Payment schedule", select([["", "Choose…"], ["monthly", "Monthly"], ["twice_monthly", "Twice a month"], ["weekly", "Weekly"]], t.payment_schedule || ""));
    const due = labelled(form, "Rent due day", input("number", t.rent_due_day, { min: 1, max: 28, step: 1 }), "Day of the month (1–28).");

    const chargesBox = E("div", "full obx-box"); chargesBox.append(E("h4", null, "Agreed charges"), E("p", "obx-muted obx-small", "Only add charges both parties agreed to (for example trash service or parking)."));
    const list = E("div");
    const addCharge = (ch) => {
      const row = E("div", "obx-row"); row.style.marginBottom = "8px";
      const label = input("text", ch?.label, { placeholder: "Charge name", maxLength: 80 }); label.style.flex = "2 1 160px"; label.setAttribute("aria-label", "Charge name");
      const amount = input("number", ch?.amount, { placeholder: "Amount", min: 0, step: "0.01" }); amount.style.flex = "1 1 100px"; amount.setAttribute("aria-label", "Charge amount");
      const freq = select([["monthly", "per month"], ["one_time", "one time"], ["yearly", "per year"]], ch?.frequency || "monthly"); freq.style.flex = "1 1 120px"; freq.setAttribute("aria-label", "Charge frequency");
      const rm = E("button", "obx-btn small", "Remove"); rm.type = "button"; rm.onclick = () => row.remove();
      row.append(label, amount, freq, rm); row._get = () => ({ label: label.value, amount: amount.value, frequency: freq.value });
      list.append(row);
    };
    (t.charges || []).forEach(addCharge);
    const add = E("button", "obx-btn small", "+ Add charge"); add.type = "button"; add.onclick = () => addCharge();
    chargesBox.append(list, add); form.append(chargesBox);

    const occ = labelled(form, "Occupants", Object.assign(E("textarea"), { value: (t.occupants || []).join("\n") }), "One name per line, including the tenant.");
    const extra = labelled(form, "Additional agreed terms", Object.assign(E("textarea"), { value: (t.additional_terms || []).join("\n") }), "One term per line. Only terms both parties agreed.");
    const pets = labelled(form, "Pets", input("text", t.pets, { maxLength: 200, placeholder: "e.g. One cat allowed" }));
    const util = labelled(form, "Utilities", input("text", t.utilities, { maxLength: 300, placeholder: "e.g. Tenant pays electricity and water" }));
    form.append(E("h4", "obx-section-title full", "Lease and account setup"));
    const leaseSetup = labelled(form, "Lease copy", select([["", "Choose…"], ...Object.entries(S.options.lease_setup)], p.lease_setup || ""));
    const account = labelled(form, "Rent account setup", select([["", "Choose…"], ...Object.entries(S.options.account_setup)], p.account_setup || ""), "Staybot does not create TurboTenant accounts automatically.");
    const template = labelled(form, "Lease template", select([["", "Choose…"], ...S.options.templates.map((x) => [x.id, `${x.name} v${x.template_version}${x.status === "demo" ? " (DEMO, testing only)" : " (approved)"}`])], c.template?.id || ""), null, true);
    if (c.template?.status === "demo") form.append(E("div", "full obx-banner bad", "This case uses the DEMO template. Agreements and PDFs are labelled testing only. Add a staff-approved template before real use."));
    const sign = labelled(form, "Signing", select([["approval_only", "Approval by both parties (no e-signature)"], ["external_esign", "Electronic signatures required (provider not connected)"]], c.signature_method), "Approval is not a signature. Choosing e-signatures blocks finalization until a provider is integrated.", true);
    lockForm(form, locked);
    panel.append(form);
    footer(panel, status, locked, async () => ({
      terms: { lease_start: start.value || null, lease_end: end.value || null, move_in_date: movein.value || null, currency: currency.value,
        rent: rent.value || null, deposit: deposit.value === "" ? null : deposit.value, payment_schedule: schedule.value || null,
        rent_due_day: due.value ? Number(due.value) : null, charges: [...list.children].map((r) => r._get()).filter((x) => x.label || x.amount),
        occupants: occ.value, additional_terms: extra.value, pets: pets.value, utilities: util.value },
      preferences: { lease_setup: leaseSetup.value || null, account_setup: account.value || null },
      template_id: template.value || null, signature_method: sign.value,
    }));
  }

  function stepReview(panel, status, locked) {
    const c = S.view, a = c.agreement, v = a.latest_version;
    panel.append(E("p", "obx-muted", "Prepare the agreement from the saved details. The tenant and the owner each open the actual draft with their secure link and approve it themselves; staff cannot approve for them. If any detail changes, a new version is created and both must approve again."));
    const bar = E("div", "obx-actions");
    const prep = E("button", "obx-btn dark", v && a.is_current ? `Agreement version ${v.version} is up to date` : v ? "Prepare new version with latest details" : "Prepare agreement for review");
    prep.type = "button"; prep.disabled = locked || (v && a.is_current);
    prep.onclick = async () => {
      prep.disabled = true; statusLine(status, "Preparing…");
      try { const r = await api("POST", `/onboarding/cases/${c.id}/agreement`); S.view = r.case; renderCase(r.created ? `Agreement version ${r.version} prepared. Ask both parties to review it.` : "No changes since the latest version.", "ok"); }
      catch (err) { statusLine(status, err.message, "bad"); prep.disabled = false; }
    };
    bar.append(prep);
    if (v) { const prev = E("a", "obx-btn", "Preview agreement"); prev.href = `/onboarding/cases/${c.id}/agreement/preview.pdf`; prev.target = "_blank"; prev.rel = "noopener"; bar.append(prev); }
    panel.append(bar);
    if (v) {
      panel.append(E("p", "obx-muted obx-small", `Version ${v.version} prepared ${when(v.created_at)}${v.is_demo ? " · DEMO template" : ""} · fingerprint ${v.content_hash.slice(0, 12)}…${a.versions.length > 1 ? ` · ${a.versions.length - 1} earlier version(s) kept` : ""}`));
      if (!a.is_current) panel.append(E("div", "obx-banner bad", "Details changed after this version was prepared. Prepare a new version; approvals of older versions no longer count."));
      const two = E("div", "obx-two"); two.style.marginTop = "14px";
      for (const party of ["tenant", "owner"]) {
        const d = a.approvals[party], b = E("div", "obx-box");
        b.append(E("h4", null, `${PARTY[party]} approval`), decisionChip(party, d));
        if (d.at) b.append(E("p", "obx-small obx-muted", `${d.status === "approved" ? "Approved" : "Responded"} ${when(d.at)} via ${d.channel === "secure_link" ? "secure link" : d.channel} (version ${v.version})`));
        if (d.note) b.append(E("p", "obx-small", `“${d.note}”`));
        if (d.status !== "approved" && !locked) b.append(E("p", "obx-small obx-muted", `If the ${party} has no working link, send an invitation on step ${party === "tenant" ? 2 : 3}.`));
        two.append(b);
      }
      panel.append(two);
      const review = E("div", "obx-box"); review.style.marginTop = "14px";
      review.append(E("h4", null, "Staff review"));
      if (a.staff_reviewed) review.append(chip(`Reviewed by ${c.staff_review.reviewed_by.replace("staff:", "")} · ${when(c.staff_review.reviewed_at)}`, "ok"));
      else {
        review.append(E("p", "obx-small obx-muted", "A staff member confirms the agreement matches what was agreed."));
        const rb = E("button", "obx-btn small dark", `I have reviewed version ${v.version}`); rb.type = "button"; rb.disabled = locked || !a.is_current;
        rb.onclick = async () => {
          if (!S.staff) { statusLine(status, "Choose who you are in “Acting as” at the top first.", "bad"); return; }
          try { S.view = await api("POST", `/onboarding/cases/${c.id}/staff-review`, { agreement_version: v.version }); renderCase("Staff review recorded.", "ok"); } catch (err) { statusLine(status, err.message, "bad"); }
        };
        review.append(rb);
      }
      review.append(E("p", "obx-small obx-muted", c.signature_method === "approval_only" ? "Signing: approval only. No electronic signatures are collected; the PDF has blank lines for hand signatures." : "Signing: electronic signatures required, but no provider is connected. Finalization is blocked."));
      panel.append(review);
    }
    footer(panel, status, locked, null);
  }

  function stepFinal(panel, status, locked) {
    const c = S.view;
    const blockers = c.steps.slice(0, 6).flatMap((s) => s.missing);
    if (!c.final_document) {
      panel.append(E("p", "obx-muted", "The final PDF is generated from the exact version both parties approved, stored privately, and never overwritten."));
      if (blockers.length) { const m = E("div", "obx-missing"); m.append(E("b", null, "Before the final PDF can be generated")); const ul = E("ul"); blockers.forEach((x) => ul.append(E("li", null, x))); m.append(ul); panel.append(m); }
      const gen = E("button", "obx-btn primary", "Generate final lease PDF"); gen.type = "button"; gen.disabled = locked || blockers.length > 0;
      gen.onclick = async () => {
        if (!S.staff) { statusLine(status, "Choose who you are in “Acting as” at the top first.", "bad"); return; }
        gen.disabled = true; statusLine(status, "Checking everything and generating the PDF…");
        try { const r = await api("POST", `/onboarding/cases/${c.id}/finalize`); S.view = r.case; renderCase("Final lease PDF generated and saved.", "ok"); }
        catch (err) { statusLine(status, err.message, "bad"); gen.disabled = false; }
      };
      panel.append(gen);
    } else {
      const f = c.final_document;
      panel.append(E("div", "obx-good", `Final lease PDF generated ${when(f.created_at)} by ${f.created_by.replace("staff:", "")}${f.is_demo ? " · DEMO template (testing only)" : ""}. Fingerprint ${f.sha256.slice(0, 16)}…`));
      const bar = E("div", "obx-actions");
      const dl = E("a", "obx-btn dark", "Download final lease PDF"); dl.href = `/onboarding/cases/${c.id}/final.pdf`;
      const send = E("button", "obx-btn primary", "Send final PDF through WhatsApp"); send.type = "button";
      send.onclick = async () => {
        send.disabled = true; statusLine(status, "Sending to the tenant and the owner…");
        try {
          const r = await api("POST", `/onboarding/cases/${c.id}/send-final`); S.view = r.case;
          const failed = Object.entries(r.results).filter(([, m]) => m.status === "failed");
          renderCase(failed.length ? `Failed for: ${failed.map(([p, m]) => `${p} (${m.error})`).join("; ")}` : "Send requests recorded. Statuses below update from WhatsApp.", failed.length ? "bad" : "ok");
        } catch (err) { statusLine(status, err.message, "bad"); send.disabled = false; }
      };
      bar.append(dl, send); panel.append(bar);
      const deliveries = c.whatsapp.filter((m) => m.kind === "final_pdf");
      const table = E("table", "obx-table"); table.style.marginTop = "16px";
      const hr = E("tr"); ["Recipient", "Status", "Details", ""].forEach((h) => hr.append(E("th", null, h))); const th = E("thead"); th.append(hr); table.append(th);
      const tb = E("tbody");
      for (const party of ["tenant", "owner"]) {
        const m = deliveries.find((x) => x.party === party);
        const tr = E("tr"); tr.append(E("td", null, `${PARTY[party]} · ${c[party]?.full_name || ""}`));
        const st = E("td"); st.append(m ? deliveryChip(m) : chip("Not sent yet")); tr.append(st);
        const det = E("td", "obx-small");
        if (m) det.textContent = [m.test_mode ? "Test mode: nothing reached a real phone." : "", m.error ? `Error: ${m.error}` : "", `Attempts: ${m.attempts}`, m.status_updated_at ? `Updated ${when(m.status_updated_at)}` : ""].filter(Boolean).join(" · ");
        tr.append(det);
        const act = E("td"); const acts = E("div", "obx-actions");
        if (m && m.status === "failed") {
          const r = E("button", "obx-btn small dark", "Retry"); r.type = "button";
          r.onclick = async () => { r.disabled = true; try { const x = await api("POST", `/onboarding/cases/${c.id}/messages/${m.id}/retry`); S.view = x.case; renderCase("Retry recorded.", "ok"); } catch (err) { statusLine(status, err.message, "bad"); r.disabled = false; } };
          acts.append(r);
        }
        if (m && m.test_mode && S.setup?.whatsapp.mode === "test") {
          for (const ev of ["delivered", "read", "failed"]) {
            const b = E("button", "obx-btn small", `Simulate ${ev}`); b.type = "button";
            b.onclick = async () => { try { const x = await api("POST", `/onboarding/cases/${c.id}/test/simulate-status`, { message_id: m.id, status: ev }); S.view = x.case; renderCase(`Simulated “${ev}” event applied (test mode only).`, "ok"); } catch (err) { statusLine(status, err.message, "bad"); } };
            acts.append(b);
          }
        }
        act.append(acts); tr.append(act); tb.append(tr);
      }
      table.append(tb); const wrap = E("div", "obx-scroll"); wrap.append(table); panel.append(wrap);
      panel.append(turbotenantBox());
    }
    footer(panel, status, true, null);
  }

  function turbotenantBox() {
    const c = S.view, box = E("div", "obx-box"); box.style.marginTop = "18px";
    box.append(E("h4", null, "TurboTenant handoff"), chip("Not connected", "warn"),
      E("p", "obx-small obx-muted", "Staybot cannot create records or onboarding links in TurboTenant. Enter this case in TurboTenant yourself, then record it here."));
    const packet = Object.assign(E("textarea"), { readOnly: true, rows: 7 }); packet.value = "Loading handoff details…"; packet.setAttribute("aria-label", "TurboTenant handoff details");
    api("GET", `/onboarding/cases/${c.id}/turbotenant`).then((r) => { packet.value = r.handoff_packet; }).catch((err) => { packet.value = err.message; });
    const copy = E("button", "obx-btn small", "Copy details"); copy.type = "button"; copy.onclick = () => navigator.clipboard?.writeText(packet.value);
    box.append(packet, copy);
    if (c.turbotenant?.status === "manual_handoff_recorded") box.append(E("p", "obx-small", `Manual handoff recorded by ${c.turbotenant.recorded_by.replace("staff:", "")} ${when(c.turbotenant.recorded_at)}${c.turbotenant.staff_entered_link ? " · link " + c.turbotenant.staff_entered_link : ""}. Not synced by Staybot.`));
    const link = input("text", "", { placeholder: "TurboTenant link you created (optional)" }); link.setAttribute("aria-label", "TurboTenant link");
    const note = input("text", "", { placeholder: "Note (optional)", maxLength: 500 }); note.setAttribute("aria-label", "Handoff note");
    const rec = E("button", "obx-btn small dark", "Record manual handoff"); rec.type = "button";
    const out = E("div", "obx-status");
    rec.onclick = async () => { try { S.view = await api("POST", `/onboarding/cases/${c.id}/turbotenant`, { link: link.value || null, note: note.value }); renderCase("Manual TurboTenant handoff recorded in the audit trail.", "ok"); } catch (err) { statusLine(out, err.message, "bad"); } };
    box.append(link, note, rec, out);
    return box;
  }

  function helpPanel() {
    const c = S.view, panel = E("section", "obx-panel obx");
    panel.append(E("h3", null, "Need help or found a problem?"),
      E("p", "obx-muted obx-small", "Tenants and owners can reply HELP on WhatsApp or tap “Ask staff for help” on their page. Legal questions, pricing disputes, conflicting details and document problems are routed here with a handoff summary."));
    for (const e of c.open_escalations) {
      const box = E("div", "obx-escalation");
      box.append(E("b", null, `${e.category.replace("_", " ")} · ${e.party || "staff"} · ${when(e.created_at)}`), E("div", null, e.handoff.summary));
      const dl = E("dl", "obx-dl"); dl.style.marginTop = "6px";
      for (const [k, v] of [["Role", e.handoff.role], ["Advisor", e.handoff.advisor_type], ["Urgency", e.handoff.urgency], ["Understood", e.handoff.understood], ["Context", e.handoff.context], ["Next action", e.handoff.next_action], ["Follow-up", e.handoff.follow_up], ["Reason", e.handoff.escalation_reason], ["Confidence", e.handoff.confidence]]) dl.append(E("dt", null, k), E("dd", null, v));
      box.append(dl);
      const done = E("button", "obx-btn small", "Mark resolved"); done.type = "button"; done.style.marginTop = "8px";
      done.onclick = async () => { try { S.view = await api("POST", `/onboarding/cases/${c.id}/escalations/${e.id}/resolve`); renderCase("Marked resolved.", "ok"); } catch (err) { alert(err.message); } };
      box.append(done); panel.append(box);
    }
    const form = E("div", "obx-form");
    const cat = labelled(form, "Type", select([["other", "General follow-up"], ["legal", "Legal concern"], ["pricing", "Pricing dispute"], ["conflict", "Conflicting information"], ["documents", "Document problem"]], "other"));
    const who = labelled(form, "Raised by", select([["staff", "Staff"], ["tenant", "Tenant (told staff)"], ["owner", "Owner (told staff)"]], "staff"));
    const msg = labelled(form, "What happened?", Object.assign(E("textarea"), { maxLength: 2000 }), null, true);
    const btn = E("button", "obx-btn small", "Flag for staff follow-up"); btn.type = "button";
    btn.onclick = async () => { if (!msg.value.trim()) return; try { S.view = await api("POST", `/onboarding/cases/${c.id}/escalations`, { party: who.value, category: cat.value, message: msg.value }); renderCase("Flagged for follow-up.", "ok"); } catch (err) { alert(err.message); } };
    form.append(btn);
    const details = E("details"); details.style.marginTop = "12px"; details.append(E("summary", "obx-small", "Flag an issue"), form);
    panel.append(details);
    if (c.status === "active") {
      const cancel = E("button", "obx-link obx-small", "Cancel this onboarding case"); cancel.type = "button"; cancel.style.marginTop = "14px";
      cancel.onclick = async () => { if (!confirm("Cancel this onboarding case? It stays in history and cannot be resumed.")) return; try { S.view = await api("POST", `/onboarding/cases/${c.id}/cancel`); renderCase("Case cancelled.", "ok"); } catch (err) { alert(err.message); } };
      panel.append(E("div"), cancel);
    }
    return panel;
  }

  function auditPanel() {
    const panel = E("section", "obx-panel");
    const details = E("details"); details.append(E("summary", null, "Activity and audit trail"));
    const list = E("div", "obx-audit"); details.append(list);
    details.addEventListener("toggle", async () => {
      if (!details.open) return;
      try {
        const rows = await api("GET", `/onboarding/cases/${S.view.id}/audit`);
        list.replaceChildren(...rows.map((r) => E("div", null, `${when(r.created_at)} · ${r.actor} · ${r.action.replaceAll("_", " ")}`)));
      } catch (err) { list.replaceChildren(E("div", null, err.message)); }
    });
    panel.append(details);
    return panel;
  }

  // ------------------------------------------------------------------
  // Settings: templates and document checklist
  // ------------------------------------------------------------------
  async function openSettings() {
    const modal = E("div", "obx-modal obx"); const dialog = E("div", "obx-dialog"); modal.append(dialog);
    modal.setAttribute("role", "dialog"); modal.setAttribute("aria-label", "Templates and document checklist");
    modal.addEventListener("click", (e) => { if (e.target === modal) modal.remove(); });
    dialog.append(E("h2", null, "Templates & document checklist"));
    const status = E("div", "obx-status");
    try {
      const [templates, reqs] = await Promise.all([api("GET", "/onboarding/cases/config/templates"), api("GET", "/onboarding/cases/config/requirements")]);
      dialog.append(E("h3", "obx-section-title", "Lease templates"));
      for (const t of templates) dialog.append(E("div", "obx-next", `${t.name} v${t.template_version} · ${t.jurisdiction} · ${t.status === "demo" ? "DEMO, testing only" : t.status === "approved" ? `approved by ${t.approved_by}` : t.status} · ${t.clauses.length} clauses`));
      const tf = E("div", "obx-form");
      const name = labelled(tf, "Template name", input("text", "", { maxLength: 120 }));
      const by = labelled(tf, "Approved by (name and role)", input("text", "", { maxLength: 120 }));
      const text = labelled(tf, "Clauses", Object.assign(E("textarea"), { rows: 8, placeholder: "## Rent\nThe tenant pays...\n\n## Deposit\n..." }), "Paste the approved wording. Start each clause with a line “## Clause title”.", true);
      const okL = E("label", "obx-check full"); const ok = input("checkbox"); okL.append(ok, E("span", null, "An authorized person (for example our attorney) approved this exact wording for North Carolina leases.")); tf.append(okL);
      const add = E("button", "obx-btn dark small", "Add approved template"); add.type = "button";
      add.onclick = async () => { try { await api("POST", "/onboarding/cases/config/templates", { name: name.value, approved_by: by.value, clauses_text: text.value, approval_confirmed: ok.checked, jurisdiction: "US-NC" }); modal.remove(); S.options = null; await load(); } catch (err) { statusLine(status, err.message, "bad"); } };
      tf.append(add); dialog.append(tf);

      dialog.append(E("h3", "obx-section-title", "Required documents"));
      const table = E("table", "obx-table"); const tb = E("tbody");
      for (const r of reqs) {
        const tr = E("tr");
        tr.append(E("td", null, `${r.jurisdiction}${r.property_id ? " · " + r.property_id : " · all homes"}`), E("td", null, `${PARTY[r.party]}: ${r.label}`), E("td", "obx-small obx-muted", r.reason));
        const td = E("td"); const tog = E("button", "obx-btn small", r.active && r.required ? "Required" : r.active ? "Optional" : "Off"); tog.type = "button";
        tog.title = "Click to switch between Required, Optional and Off";
        tog.onclick = async () => { const next = r.active && r.required ? { required: false, active: true } : r.active ? { required: true, active: false } : { required: true, active: true };
          try { await api("POST", "/onboarding/cases/config/requirements", { jurisdiction: r.jurisdiction, property_id: r.property_id, party: r.party, doc_key: r.doc_key, label: r.label, reason: r.reason, ...next }); modal.remove(); openSettings(); } catch (err) { statusLine(status, err.message, "bad"); } };
        td.append(tog); tr.append(td); tb.append(tr);
      }
      table.append(tb); const wrap = E("div", "obx-scroll"); wrap.append(table); dialog.append(wrap);
      const rf = E("div", "obx-form");
      const party = labelled(rf, "For", select([["tenant", "Tenant"], ["owner", "Owner"]], "tenant"));
      const home = labelled(rf, "Applies to", select([["", "All homes in North Carolina"], ...S.options.properties.map((p) => [p.id, p.title])], ""));
      const label = labelled(rf, "Document name", input("text", "", { maxLength: 80, placeholder: "e.g. Renters insurance" }));
      const key = labelled(rf, "Short key", input("text", "", { maxLength: 60, placeholder: "e.g. renters_insurance" }));
      const reason = labelled(rf, "Why it's needed (shown to the person)", input("text", "", { maxLength: 300 }), null, true);
      const addReq = E("button", "obx-btn dark small", "Add document requirement"); addReq.type = "button";
      addReq.onclick = async () => { try { await api("POST", "/onboarding/cases/config/requirements", { jurisdiction: "US-NC", property_id: home.value || null, party: party.value, doc_key: key.value, label: label.value, reason: reason.value }); modal.remove(); openSettings(); } catch (err) { statusLine(status, err.message, "bad"); } };
      rf.append(addReq); dialog.append(rf);
    } catch (err) { statusLine(status, err.message, "bad"); }
    const close = E("button", "obx-btn", "Close"); close.type = "button"; close.style.marginTop = "18px"; close.onclick = () => modal.remove();
    dialog.append(status, close);
    document.body.append(modal);
  }

  window.StaybotOnboarding = { load, startFromLead, openCase };
  // index.html may have shown the Onboarding tab before this script loaded.
  if (location.hash === "#onboarding") load();
})();
