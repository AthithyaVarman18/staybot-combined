// Investors: pipeline for the Existing Property Investor / New Property Investor journeys.
// Reuses the .obx styles injected by onboarding.js and the staff-name convention from owner_leads.js.
(function () {
  "use strict";
  const E = (tag, cls, text) => { const n = document.createElement(tag); if (cls) n.className = cls; if (text != null) n.textContent = text; return n; };
  const S = { investors: [], counts: {}, journeys: {}, journeyFilter: "all", query: "" };
  const STATUS_TONE = { active: "ok", paused: "warn", converted: "ok", lost: "bad" };
  const staff = () => { try { return localStorage.getItem("staybot.onboarding.actingStaff") || ""; } catch (e) { return ""; } };

  async function api(method, url, body) {
    const headers = { "X-Staybot-Staff": staff() };
    const init = { method, headers };
    if (body !== undefined) { init.body = JSON.stringify(body); headers["Content-Type"] = "application/json"; }
    const res = await fetch(url, init);
    let data = null; try { data = await res.json(); } catch (e) { /* not JSON */ }
    if (!res.ok) {
      const d = data && data.detail;
      throw new Error(typeof d === "string" ? d : Array.isArray(d) ? d.map((x) => x.msg).join("; ") : res.status === 401 ? "Please sign in again." : `Request failed (${res.status}).`);
    }
    return data;
  }
  const chip = (text, tone) => E("span", "obx-chip " + (tone || ""), text);
  const money = (inv) => inv.budget ? `${inv.budget_currency === "USD" ? "$" : "₹"}${Number(inv.budget).toLocaleString(inv.budget_currency === "USD" ? "en-US" : "en-IN")}` : "";
  const root = () => document.getElementById("journey-root");
  function say(node, text, tone) { node.textContent = text || ""; node.className = "obx-status " + (tone || ""); }

  function journeyLabel(inv) {
    if (!inv.journey) return "Not yet classified";
    return (S.journeys[inv.investor_type] || {}).label || inv.journey;
  }
  function stageTitle(inv) {
    const j = S.journeys[inv.investor_type];
    if (!j) return inv.stage;
    const s = j.stages.find((x) => x.key === inv.stage);
    return s ? s.title : inv.stage;
  }

  async function load() {
    const box = root(); if (!box) return;
    try {
      const data = await api("GET", "/journeys");
      S.investors = data.investors; S.counts = data.counts; S.journeys = data.journeys;
      render();
    } catch (err) { box.replaceChildren(E("div", "obx-banner bad", err.message)); }
  }

  function render(message, tone) {
    const box = root(); box.replaceChildren();
    box.append(E("h2", null, "Investors"), E("p", "obx-muted",
      "Existing Property Investor and New Property Investor journeys, assigned automatically once each investor is qualified."));

    const bar = E("div", "obx-head");
    const filters = E("div", "obx-actions");
    const journeyOpts = [["all", "All"], ["", "Not yet classified"], ["existing", "Existing Property Investor"], ["new", "New Property Investor"]];
    for (const [key, label] of journeyOpts) {
      const n = key === "all" ? S.investors.length : S.investors.filter((i) => (i.investor_type || "") === key).length;
      const b = E("button", "obx-btn small" + (S.journeyFilter === key ? " dark" : ""), `${label} (${n})`); b.type = "button";
      b.onclick = () => { S.journeyFilter = key; render(); };
      filters.append(b);
    }
    const search = E("input"); search.type = "text"; search.placeholder = "Search name, phone, location, goals…";
    search.value = S.query; search.setAttribute("aria-label", "Search investors"); search.style.maxWidth = "280px";
    search.oninput = () => { S.query = search.value; drawList(list); };
    bar.append(filters, search);
    box.append(bar);

    const status = E("div", "obx-status"); if (message) say(status, message, tone); box.append(status);
    const list = E("div", "obx-cards"); box.append(list);
    drawList(list);
  }

  function drawList(list) {
    const q = S.query.trim().toLowerCase();
    const rows = S.investors.filter((i) =>
      (S.journeyFilter === "all" || (i.investor_type || "") === S.journeyFilter) &&
      (!q || [i.name, i.phone, i.email, i.location, i.investment_goals].some((v) => (v || "").toLowerCase().includes(q))));
    list.replaceChildren();
    if (!rows.length) { list.append(E("p", "obx-muted", S.investors.length ? "No investors match this filter." : "No investors yet - they're created automatically from the AI chat once someone says they want to invest.")); return; }
    for (const i of rows) list.append(card(i));
  }

  function card(inv) {
    const c = E("div", "obx-card");
    const top = E("div", "obx-row");
    top.append(chip(journeyLabel(inv), inv.journey ? "info" : "warn"));
    if (inv.journey) top.append(chip(stageTitle(inv), "ok"));
    top.append(chip(inv.status, STATUS_TONE[inv.status]));
    c.append(top, E("h3", null, inv.name || inv.phone || "Unnamed investor"));
    const dl = E("dl", "obx-dl");
    const pair = (k, v) => { if (v) dl.append(E("dt", null, k), E("dd", null, v)); };
    pair("Phone", inv.phone);
    pair("Goals", inv.investment_goals);
    pair("Budget", money(inv));
    pair("Location", inv.location);
    pair("Strategy", inv.investment_strategy);
    pair("Risk", inv.risk_tolerance);
    pair("Timeline", inv.timeline);
    pair("Owns already", inv.existing_property_count != null ? String(inv.existing_property_count) : null);
    c.append(dl);
    const actions = E("div", "obx-actions");
    const view = E("button", "obx-btn small dark", "View / advance"); view.type = "button";
    view.onclick = () => openDetail(inv.id);
    actions.append(view);
    c.append(actions);
    return c;
  }

  // -----------------------------------------------------------------
  // Detail modal: profile edit, stage pipeline, portfolio, activity
  // -----------------------------------------------------------------

  async function openDetail(id) {
    const modal = E("div", "obx-modal obx"); modal.setAttribute("role", "dialog"); modal.setAttribute("aria-label", "Investor detail");
    const d = E("div", "obx-dialog"); modal.append(d);
    modal.addEventListener("click", (e) => { if (e.target === modal) modal.remove(); });
    d.append(E("p", "obx-muted", "Loading…"));
    document.body.append(modal);

    let inv;
    try { inv = await api("GET", `/journeys/${id}`); }
    catch (err) { d.replaceChildren(E("div", "obx-banner bad", err.message)); return; }

    async function refresh() { inv = await api("GET", `/journeys/${id}`); draw(); }

    function draw() {
      d.replaceChildren();
      d.append(E("h2", null, inv.name || inv.phone || "Unnamed investor"),
        E("p", "obx-muted", `${inv.journey_label || "Not yet classified"}${inv.phone ? " · " + inv.phone : ""}`));

      const out = E("div", "obx-status");

      d.append(stagePanel(inv, out, refresh));
      d.append(profilePanel(inv, out, refresh));
      d.append(portfolioPanel(inv, out, refresh));
      d.append(activityPanel(inv, out, refresh));
      d.append(out);

      const foot = E("div", "obx-foot");
      const close = E("button", "obx-btn", "Close"); close.type = "button"; close.onclick = () => modal.remove();
      foot.append(close);
      d.append(foot);
    }

    draw();
  }

  function stagePanel(inv, out, refresh) {
    const panel = E("section", "obx-panel obx");
    panel.append(E("h3", null, "Journey & stage"));
    const j = S.journeys[inv.investor_type];
    if (!j) {
      panel.append(E("p", "obx-muted", "Not yet assigned - the journey is chosen automatically once \"number of properties already owned\" is known (0 = New Property Investor, more = Existing Property Investor)."));
      return panel;
    }
    const track = E("div", "obx-row"); track.style.flexWrap = "wrap";
    for (const s of j.stages) track.append(chip(s.title, s.key === inv.stage ? "ok" : ""));
    panel.append(track);
    const current = j.stages.find((s) => s.key === inv.stage);
    if (current) panel.append(E("p", "obx-small obx-muted", current.description));

    const actions = E("div", "obx-actions"); actions.style.marginTop = "10px";
    const idx = j.stages.findIndex((s) => s.key === inv.stage);
    if (idx < j.stages.length - 1) {
      const next = E("button", "obx-btn small primary", `Advance to "${j.stages[idx + 1].title}"`); next.type = "button";
      next.onclick = async () => {
        const note = prompt("Note for this move (optional):") || "";
        next.disabled = true;
        try { await api("POST", `/journeys/${inv.id}/advance`, { note }); await refresh(); }
        catch (err) { say(out, err.message, "bad"); next.disabled = false; }
      };
      actions.append(next);
    }
    const jump = E("select"); jump.setAttribute("aria-label", "Jump to stage");
    jump.add(new Option("Jump to stage…", ""));
    for (const s of j.stages) jump.add(new Option(s.title, s.key));
    jump.onchange = async () => {
      if (!jump.value || jump.value === inv.stage) return;
      const note = prompt("Note for this move (optional):") || "";
      try { await api("POST", `/journeys/${inv.id}/advance`, { target_stage: jump.value, note }); await refresh(); }
      catch (err) { say(out, err.message, "bad"); jump.value = ""; }
    };
    actions.append(jump);
    panel.append(actions);
    return panel;
  }

  function profilePanel(inv, out, refresh) {
    const panel = E("section", "obx-panel obx");
    panel.append(E("h3", null, "Profile"));
    const form = E("div", "obx-form");

    const field = (label, key, value, opts) => {
      const l = E("label"); l.append(E("span", null, label));
      let input;
      if (opts) { input = E("select"); input.add(new Option("—", "")); for (const o of opts) input.add(new Option(o, o)); input.value = value || ""; }
      else { input = E("input"); input.type = key === "budget" || key === "existing_property_count" ? "number" : "text"; input.value = value ?? ""; }
      input.dataset.key = key;
      l.append(input); form.append(l);
      return input;
    };

    field("Name", "name", inv.name);
    field("Phone", "phone", inv.phone);
    field("Email", "email", inv.email);
    field("Investor type (override)", "investor_type", inv.investor_type, ["new", "existing"]);
    field("Status", "status", inv.status, ["active", "paused", "converted", "lost"]);
    field("Investment goals", "investment_goals", inv.investment_goals);
    field("Budget", "budget", inv.budget);
    field("Location", "location", inv.location);
    field("Strategy", "investment_strategy", inv.investment_strategy);
    field("Risk tolerance", "risk_tolerance", inv.risk_tolerance, ["low", "moderate", "high"]);
    field("Timeline", "timeline", inv.timeline);
    field("Properties already owned", "existing_property_count", inv.existing_property_count);
    field("Financing plan", "financing_requirements", inv.financing_requirements);
    field("Assigned staff", "assigned_staff", inv.assigned_staff);
    const notesL = E("label", "full"); notesL.append(E("span", null, "Notes"));
    const notes = E("textarea"); notes.rows = 3; notes.dataset.key = "notes"; notes.textContent = inv.notes || "";
    notesL.append(notes); form.append(notesL);

    panel.append(form);
    const actions = E("div", "obx-actions"); actions.style.marginTop = "12px";
    const save = E("button", "obx-btn primary", "Save changes"); save.type = "button";
    save.onclick = async () => {
      const changes = {};
      form.querySelectorAll("[data-key]").forEach((el) => {
        const key = el.dataset.key;
        let val = el.value;
        if (val === "") return;
        if (key === "budget") val = Number(val);
        if (key === "existing_property_count") val = parseInt(val, 10);
        changes[key] = val;
      });
      save.disabled = true;
      try { await api("PATCH", `/journeys/${inv.id}`, changes); say(out, "Saved.", "ok"); await refresh(); }
      catch (err) { say(out, err.message, "bad"); save.disabled = false; }
    };
    actions.append(save);
    panel.append(actions);
    return panel;
  }

  function portfolioPanel(inv, out, refresh) {
    const panel = E("section", "obx-panel obx");
    panel.append(E("h3", null, "Portfolio properties"),
      E("p", "obx-muted", "Properties already owned, or being pursued as the next acquisition."));
    const list = E("div", "obx-audit");
    if (!inv.portfolio.length) list.append(E("div", null, "None recorded yet."));
    for (const p of inv.portfolio) {
      list.append(E("div", null, `${p.relationship} · ${p.address || "no address"}${p.monthly_rent ? " · rent " + p.monthly_rent : ""}${p.estimated_value ? " · value " + p.estimated_value : ""}`));
    }
    panel.append(list);

    const form = E("div", "obx-form"); form.style.marginTop = "12px";
    const addr = E("input"); addr.placeholder = "Address";
    const rel = E("select"); [["owned", "Owned"], ["target", "Target"], ["under_contract", "Under contract"], ["acquired", "Acquired"]].forEach(([v, t]) => rel.add(new Option(t, v)));
    const value = E("input"); value.type = "number"; value.placeholder = "Estimated value";
    const rent = E("input"); rent.type = "number"; rent.placeholder = "Monthly rent";
    const wrap = (input, label) => { const l = E("label"); l.append(E("span", null, label), input); return l; };
    form.append(wrap(addr, "Address"), wrap(rel, "Relationship"), wrap(value, "Estimated value"), wrap(rent, "Monthly rent"));
    panel.append(form);

    const add = E("button", "obx-btn small dark", "Add property"); add.type = "button"; add.style.marginTop = "10px";
    add.onclick = async () => {
      if (!addr.value.trim()) { say(out, "Enter an address first.", "bad"); return; }
      add.disabled = true;
      try {
        await api("POST", `/journeys/${inv.id}/portfolio`, {
          address: addr.value.trim(), relationship: rel.value,
          estimated_value: value.value ? Number(value.value) : undefined,
          monthly_rent: rent.value ? Number(rent.value) : undefined,
        });
        await refresh();
      } catch (err) { say(out, err.message, "bad"); add.disabled = false; }
    };
    panel.append(add);
    return panel;
  }

  function activityPanel(inv, out, refresh) {
    const panel = E("section", "obx-panel obx");
    panel.append(E("h3", null, "Activity"));
    const list = E("div", "obx-audit");
    const rows = [...inv.stage_history.map((h) => ({ ...h, action: `stage: ${h.from_stage || "start"} → ${h.to_stage}` })), ...inv.activity]
      .sort((a, b) => new Date(b.created_at) - new Date(a.created_at));
    if (!rows.length) list.append(E("div", null, "No activity yet."));
    for (const r of rows.slice(0, 30)) {
      const note = r.note || (r.details && r.details.note) || "";
      list.append(E("div", null, `${new Date(r.created_at).toLocaleString()} · ${r.actor} · ${r.action.replaceAll("_", " ")}${note ? " — " + note : ""}`));
    }
    panel.append(list);

    const row = E("div", "obx-row"); row.style.marginTop = "10px";
    const text = E("input"); text.type = "text"; text.placeholder = "Log a call, email or note…"; text.style.flex = "1";
    const add = E("button", "obx-btn small", "Log"); add.type = "button";
    add.onclick = async () => {
      if (!text.value.trim()) return;
      add.disabled = true;
      try { await api("POST", `/journeys/${inv.id}/activity`, { action: "note", details: text.value.trim() }); text.value = ""; await refresh(); }
      catch (err) { say(out, err.message, "bad"); }
      finally { add.disabled = false; }
    };
    row.append(text, add);
    panel.append(row);
    return panel;
  }

  window.StaybotJourneys = { load, openDetail };
  if (location.hash === "#investors") load();
})();
