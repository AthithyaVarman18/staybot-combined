// Investors: who wants to buy, how ready they are, and which homes fit their money.
// Reuses the .obx styles injected by onboarding.js.
(function () {
  "use strict";
  const E = (tag, cls, text) => { const n = document.createElement(tag); if (cls) n.className = cls; if (text != null) n.textContent = text; return n; };
  const S = { investors: [], counts: {}, filter: "all", open: null, matches: null };
  const TIER = { hot: ["Hot", "bad"], warm: ["Warm", "warn"], nurture: ["Nurture", "info"], unqualified: ["Unqualified", ""] };
  const STATUS = { new: "New", qualified: "Qualified", advisory: "Advisory agreed", buying: "Buying", bought: "Bought", not_now: "Not now" };
  const GOAL = { income: "Monthly income", growth: "Price growth", both: "Both" };
  const TIMELINE = { now: "Now", "3_months": "In 3 months", "6_months": "In 6 months", "12_months": "In a year", unsure: "Not sure" };
  const EXPERIENCE = { first_time: "First-time investor", owns_some: "Owns 1-2", owns_many: "Owns 3+" };
  const MANAGEMENT = { self: "Manages it themselves", company: "Wants a management company", unsure: "Not sure" };
  const CONDITION = { turnkey: "Turnkey only", light_work: "Light work ok", heavy_work: "Full renovation ok", unsure: "Not sure" };
  const OWNERSHIP = { personal: "Personal name", company: "Company / LLC", unsure: "Not sure" };
  const HOME_AGE = { new: "New build", older: "Older home", any: "Either" };
  const STRATEGY = { long_term_rental: "Long-term rental", short_term_rental: "Short-term rental (Airbnb)", fix_and_flip: "Fix and flip", buy_and_hold: "Buy and hold", unsure: "Not sure" };
  const RISK = { low: "Low", moderate: "Moderate", high: "High", unsure: "Not sure" };
  const staff = () => { try { return localStorage.getItem("staybot.onboarding.actingStaff") || ""; } catch (e) { return ""; } };

  async function api(method, url, body) {
    const headers = { "X-Staybot-Staff": staff() };
    if (body !== undefined) headers["Content-Type"] = "application/json";
    const res = await fetch(url, { method, headers, body: body !== undefined ? JSON.stringify(body) : undefined });
    let data = null; try { data = await res.json(); } catch (e) { /* not JSON */ }
    if (!res.ok) {
      const d = data && data.detail;
      throw new Error(typeof d === "string" ? d : Array.isArray(d) ? d.map((x) => x.msg).join("; ") : `Request failed (${res.status}).`);
    }
    return data;
  }
  const chip = (t, tone) => E("span", "obx-chip " + (tone || ""), t);
  const dollars = (v) => v == null ? "—" : "$" + Math.round(Number(v)).toLocaleString("en-US");
  const pct = (v) => v == null ? "—" : Number(v).toFixed(2) + "%";
  const root = () => document.getElementById("investors-root");
  function say(n, t, tone) { n.textContent = t || ""; n.className = "obx-status " + (tone || ""); }

  async function load() {
    const box = root(); if (!box) return;
    try {
      const data = await api("GET", "/investors");
      S.investors = data.investors; S.counts = data.counts;
      render();
    } catch (err) { box.replaceChildren(E("div", "obx-banner bad", err.message)); }
  }

  function render(message, tone) {
    const box = root(); box.replaceChildren();
    const head = E("div", "obx-head");
    const left = E("div");
    left.append(E("h2", null, "Investors"), E("p", "obx-muted", "People who want to buy property. The AI collects their budget and goal in the chat; staff can add or fix anything here."));
    const add = E("button", "obx-btn primary", "+ Add investor"); add.type = "button"; add.onclick = () => openForm();
    head.append(left, add);
    box.append(head);
    const bar = E("div", "obx-actions");
    for (const key of ["all", "hot", "warm", "nurture", "unqualified"]) {
      const n = key === "all" ? S.investors.length : (S.counts[key] || 0);
      const b = E("button", "obx-btn small" + (S.filter === key ? " dark" : ""), `${key === "all" ? "All" : TIER[key][0]} (${n})`);
      b.type = "button"; b.onclick = () => { S.filter = key; render(); };
      bar.append(b);
    }
    box.append(bar);
    const status = E("div", "obx-status"); if (message) say(status, message, tone); box.append(status);
    const rows = S.investors.filter((i) => S.filter === "all" || i.tier === S.filter);
    if (!rows.length) { box.append(E("p", "obx-muted", "No investors yet. Add one, or wait for the AI chat to find one.")); return; }
    const grid = E("div", "obx-cards");
    for (const investor of rows) grid.append(card(investor, status));
    box.append(grid);
  }

  function card(i, status) {
    const c = E("div", "obx-card");
    const top = E("div", "obx-row");
    top.append(chip(`${TIER[i.tier][0]} · ${i.score}`, TIER[i.tier][1]), chip(STATUS[i.status]));
    if (i.source === "chat") top.append(chip("From AI chat", "info"));
    c.append(top, E("h3", null, i.full_name));
    const dl = E("dl", "obx-dl");
    const pair = (k, v) => { if (v) dl.append(E("dt", null, k), E("dd", null, v)); };
    pair("Cash", dollars(i.cash_available));
    pair("Financing", i.financing === "cash" ? "Cash" : i.financing === "mortgage" ? `Mortgage (${i.pre_approved === "yes" ? "pre-approved" : "not pre-approved"})` : i.financing ? "Not sure" : null);
    pair("Goal", GOAL[i.goal]);
    pair("Where", (i.areas || []).join(", "));
    pair("Bedrooms", i.min_bedrooms ? `${i.min_bedrooms}+` : null);
    pair("When", TIMELINE[i.timeline]);
    pair("Experience", EXPERIENCE[i.experience]);
    pair("Management", MANAGEMENT[i.management_preference]);
    pair("Condition", CONDITION[i.condition_preference]);
    pair("New or older", HOME_AGE[i.home_age_preference]);
    pair("Strategy", STRATEGY[i.strategy]);
    pair("Risk", RISK[i.risk_tolerance]);
    pair("Wants cash flow", i.target_cash_flow ? dollars(i.target_cash_flow) + "/mo" : null);
    pair("Hold", i.hold_years ? `${i.hold_years} years` : null);
    pair("Buying as", OWNERSHIP[i.ownership]);
    pair("WhatsApp", i.whatsapp);
    c.append(dl);
    if (i.notes) c.append(E("div", "obx-next", i.notes.slice(0, 200)));
    const actions = E("div", "obx-actions");
    const edit = E("button", "obx-btn small", "Edit"); edit.type = "button"; edit.onclick = () => openForm(i);
    const match = E("button", "obx-btn small dark", "Find homes that fit"); match.type = "button";
    match.disabled = !i.cash_available;
    match.title = i.cash_available ? "" : "Add how much cash they have first";
    match.onclick = () => openMatches(i);
    const sel = E("select"); sel.setAttribute("aria-label", "Investor status");
    for (const [k, v] of Object.entries(STATUS)) sel.add(new Option(v, k));
    sel.value = i.status;
    sel.onchange = async () => { try { await api("PATCH", `/investors/${i.id}`, { full_name: i.full_name, status: sel.value }); await load(); } catch (err) { say(status, err.message, "bad"); } };
    actions.append(edit, match, sel);
    c.append(actions);
    const why = E("details"); why.append(E("summary", "obx-small obx-muted", `Why score ${i.score}`));
    const parts = E("dl", "obx-dl");
    for (const [k, v] of Object.entries(i.score_parts || {})) parts.append(E("dt", null, `${k} (${v.score})`), E("dd", null, v.reason));
    why.append(parts); c.append(why);
    return c;
  }

  function openForm(existing) {
    const modal = E("div", "obx-modal obx"); modal.setAttribute("role", "dialog"); modal.setAttribute("aria-label", "Investor");
    const d = E("div", "obx-dialog"); modal.append(d);
    modal.addEventListener("click", (e) => { if (e.target === modal) modal.remove(); });
    d.append(E("h2", null, existing ? `Edit ${existing.full_name}` : "Add an investor"));
    const form = E("div", "obx-form");
    const field = (label, control, hint, full) => { const l = E("label", full ? "full" : null); l.append(E("span", null, label), control); if (hint) l.append(E("small", "obx-muted", hint)); form.append(l); return control; };
    const text = (value, attrs) => { const i = E("input"); i.type = "text"; i.value = value ?? ""; Object.assign(i, attrs || {}); return i; };
    const pick = (options, value) => { const s = E("select"); for (const [v, t] of options) s.add(new Option(t, v)); s.value = value ?? ""; return s; };
    const e = existing || {};
    const name = field("Full name", text(e.full_name, { maxLength: 200 }));
    const phone = field("WhatsApp", text(e.whatsapp, { placeholder: "+1 704 555 0101" }));
    const email = field("Email", text(e.email, { maxLength: 200 }));
    const cash = field("Cash available ($)", Object.assign(E("input"), { type: "number", min: "0", step: "1000", value: e.cash_available ?? "" }), "Down payment plus closing costs");
    const financing = field("Financing", pick([["", "Not known"], ["cash", "Cash"], ["mortgage", "Mortgage"], ["unsure", "Not sure"]], e.financing));
    const approved = field("Pre-approved", pick([["", "Not known"], ["yes", "Yes"], ["no", "No"], ["unsure", "Not sure"]], e.pre_approved));
    const goal = field("Goal", pick([["", "Not known"], ["income", "Monthly income"], ["growth", "Price growth"], ["both", "Both"]], e.goal));
    const areas = field("Areas", text((e.areas || []).join(", "), { placeholder: "Garner, Fuquay Varina" }), "Separate with commas");
    const type = field("Property type", text(e.property_type, { placeholder: "single family" }));
    const beds = field("Minimum bedrooms", Object.assign(E("input"), { type: "number", min: "0", max: "20", value: e.min_bedrooms ?? "" }));
    const timeline = field("When do they want to buy", pick([["", "Not known"], ...Object.entries(TIMELINE)], e.timeline));
    const experience = field("Experience", pick([["", "Not known"], ...Object.entries(EXPERIENCE)], e.experience));
    const management = field("Management", pick([["", "Not known"], ...Object.entries(MANAGEMENT)], e.management_preference), "Self-managing removes the management fee from every calculation");
    const condition = field("Condition wanted", pick([["", "Not known"], ...Object.entries(CONDITION)], e.condition_preference));
    const homeAge = field("New or older home", pick([["", "Not known"], ...Object.entries(HOME_AGE)], e.home_age_preference));
    const strategy = field("Strategy", pick([["", "Not known"], ...Object.entries(STRATEGY)], e.strategy));
    const risk = field("Risk level", pick([["", "Not known"], ...Object.entries(RISK)], e.risk_tolerance));
    const ownership = field("Buying as", pick([["", "Not known"], ...Object.entries(OWNERSHIP)], e.ownership));
    const target = field("Wanted cash flow ($/month)", Object.assign(E("input"), { type: "number", min: "0", step: "50", value: e.target_cash_flow ?? "" }));
    const hold = field("Years they plan to keep it", Object.assign(E("input"), { type: "number", min: "0", max: "60", value: e.hold_years ?? "" }));
    const notes = field("Notes", Object.assign(E("textarea"), { value: e.notes || "", maxLength: 2000 }), null, true);
    d.append(form);
    const out = E("div", "obx-status");
    const foot = E("div", "obx-foot");
    const cancel = E("button", "obx-btn", "Cancel"); cancel.type = "button"; cancel.onclick = () => modal.remove();
    const save = E("button", "obx-btn primary", existing ? "Save changes" : "Add investor"); save.type = "button";
    save.onclick = async () => {
      if (!name.value.trim()) { say(out, "Enter their name.", "bad"); return; }
      save.disabled = true;
      const body = { full_name: name.value.trim() };
      const maybe = { whatsapp: phone.value.trim(), email: email.value.trim(), property_type: type.value.trim(), notes: notes.value.trim(),
        financing: financing.value, pre_approved: approved.value, goal: goal.value, timeline: timeline.value, experience: experience.value,
        management_preference: management.value, condition_preference: condition.value, ownership: ownership.value,
        home_age_preference: homeAge.value, strategy: strategy.value, risk_tolerance: risk.value };
      for (const [k, v] of Object.entries(maybe)) if (v) body[k] = v;
      if (cash.value) body.cash_available = Number(cash.value);
      if (beds.value) body.min_bedrooms = Number(beds.value);
      if (target.value) body.target_cash_flow = Number(target.value);
      if (hold.value) body.hold_years = Number(hold.value);
      const list = areas.value.split(",").map((a) => a.trim()).filter(Boolean);
      if (list.length) body.areas = list;
      try {
        await api(existing ? "PATCH" : "POST", existing ? `/investors/${existing.id}` : "/investors", body);
        modal.remove(); await load(); render(existing ? "Investor updated." : "Investor added.", "ok");
      } catch (err) { say(out, err.message, "bad"); save.disabled = false; }
    };
    foot.append(cancel, save); d.append(out, foot);
    document.body.append(modal); name.focus();
  }

  async function openMatches(investor) {
    const modal = E("div", "obx-modal obx"); modal.setAttribute("role", "dialog"); modal.setAttribute("aria-label", "Matching homes");
    const d = E("div", "obx-dialog"); d.style.width = "min(900px, 100%)"; modal.append(d);
    modal.addEventListener("click", (e) => { if (e.target === modal) modal.remove(); });
    d.append(E("div", "obx-kicker", "Homes that fit the money"), E("h2", null, investor.full_name), E("p", "obx-muted", "Loading…"));
    document.body.append(modal);
    const out = E("div", "obx-status");
    try {
      const data = await api("GET", `/investors/${investor.id}/matches?limit=6`);
      d.replaceChildren(E("div", "obx-kicker", "Homes that fit the money"), E("h2", null, investor.full_name));
      const chips = E("div", "obx-row");
      chips.append(chip(`Cash ${dollars(data.cash_available)}`), chip(`Can buy up to ${dollars(data.budget_ceiling)}`, "info"),
        chip(`Checked ${data.considered} homes`), chip(`Rent assumed ${data.assumed_rent_percent}% of price`, "warn"));
      d.append(chips, E("p", "obx-small obx-muted", data.note));
      if (!data.matches.length) { d.append(E("div", "obx-missing", "No homes match this budget. Try a bigger cash amount or different areas.")); }
      const wrap = E("div", "obx-scroll"); const t = E("table", "obx-table");
      const hr = E("tr"); ["", "Home", "Price", "Cash needed", "Rent used", "Cash flow", "Cap", "CoC", "If things change", ""].forEach((h) => hr.append(E("th", null, h)));
      const th = E("thead"); th.append(hr); t.append(th);
      const tb = E("tbody");
      for (const m of data.matches) {
        const l = m.listing; const tr = E("tr");
        const pic = E("td");
        if (l.photo_url) {
          const img = E("img"); img.src = l.photo_url; img.alt = l.street_address || "Property photo";
          img.width = 104; img.height = 76; img.loading = "lazy"; img.referrerPolicy = "no-referrer";
          img.style.cssText = "object-fit:cover;border-radius:10px";
          img.onerror = () => img.remove();
          pic.append(img);
        }
        const home = E("td"); home.append(E("b", null, l.street_address || l.list_number),
          E("div", "obx-small obx-muted", `${l.city} · ${l.bedrooms ?? "?"} bed · ${l.living_area ? Number(l.living_area).toLocaleString() + " sq ft" : "size unknown"} · built ${l.year_built || "?"}${l.days_on_market != null ? " · " + l.days_on_market + "d on market" : ""}`));
        const flow = E("td", null, (m.cash_flow_monthly >= 0 ? "+" : "-") + dollars(Math.abs(m.cash_flow_monthly)));
        flow.style.color = m.cash_flow_monthly >= 0 ? "#008A05" : "#C13515"; flow.style.fontWeight = "700";
        const detail = E("td", "obx-small obx-muted");
        if (m.sensitivity) detail.textContent = `If rent drops 10%: ${dollars(m.sensitivity.rent_down_10_percent)}/mo · if rates rise 1%: ${dollars(m.sensitivity.rate_up_1_percent)}/mo · break-even rent ${dollars(m.breakeven_rent)}`;
        tr.append(pic, home, E("td", null, dollars(l.list_price)), E("td", null, dollars(m.cash_needed)), E("td", null, dollars(m.rent_used)),
          flow, E("td", null, pct(m.cap_rate_percent)), E("td", null, pct(m.cash_on_cash_percent)), detail);
        const act = E("td"); const send = E("button", "obx-btn small dark", "Send on WhatsApp"); send.type = "button";
        send.disabled = !investor.whatsapp;
        send.title = investor.whatsapp ? "" : "Add their WhatsApp number first";
        send.onclick = async () => {
          const rent = prompt(`Monthly rent to use for ${l.street_address}?\n(Leave as is to use the assumed ${dollars(m.rent_used)}, but confirm real rents first.)`, Math.round(m.rent_used));
          if (rent === null) return;
          send.disabled = true; say(out, "Sending…");
          try {
            const r = await api("POST", `/investors/${investor.id}/send-deal`, { list_number: l.list_number, monthly_rent: Number(rent) || undefined });
            say(out, r.sent.test_mode ? "Test mode: the deal PDF was not really sent." : "Deal PDF sent on WhatsApp.", "ok");
          } catch (err) { say(out, err.message, "bad"); send.disabled = false; }
        };
        act.append(send); tr.append(act); tb.append(tr);
      }
      t.append(tb); wrap.append(t); d.append(wrap, out);
      const foot = E("div", "obx-foot"); const close = E("button", "obx-btn", "Close"); close.type = "button"; close.onclick = () => modal.remove();
      foot.append(close); d.append(foot);
    } catch (err) {
      d.replaceChildren(E("h2", null, investor.full_name), E("div", "obx-banner bad", err.message));
      const foot = E("div", "obx-foot"); const close = E("button", "obx-btn", "Close"); close.type = "button"; close.onclick = () => modal.remove();
      foot.append(close); d.append(foot);
    }
  }

  window.StaybotInvestors = { load };
  if (location.hash === "#investors") load();
})();
