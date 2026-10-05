// Owner leads: import a provider CSV, work the outreach list, hand interested owners to the AI chat.
// Reuses the .obx styles injected by onboarding.js.
(function () {
  "use strict";
  const E = (tag, cls, text) => { const n = document.createElement(tag); if (cls) n.className = cls; if (text != null) n.textContent = text; return n; };
  const S = { leads: [], counts: {}, filter: "new", query: "", mode: "test", email: { mode: "test" }, waSet: false, preview: null, file: null };
  const EMAIL = { not_sent: ["Not emailed", ""], sending: ["Emailing…", "info"], sent: ["Emailed", "info"], test_sent: ["Emailed (test mode)", "test"], failed: ["Email failed", "bad"] };
  const RESPONSE = { whatsapp: ["Said YES · WhatsApp", "ok"], email: ["Said YES · email", "ok"], not_interested: ["Said no", ""], unsubscribed: ["Unsubscribed", "bad"] };
  const STATUS = { new: "New", contacted: "Contacted", interested: "Interested", call_back: "Call back", not_interested: "Not interested", do_not_contact: "Do not contact", converted: "Listing created" };
  const TONE = { new: "info", contacted: "", interested: "ok", call_back: "warn", not_interested: "", do_not_contact: "bad", converted: "ok" };
  const CONSENT = { whatsapp: "WhatsApp OK", call: "Calls OK", email: "Email OK", none: "No consent" };
  const staff = () => { try { return localStorage.getItem("staybot.onboarding.actingStaff") || ""; } catch (e) { return ""; } };

  async function api(method, url, body, raw) {
    const headers = { "X-Staybot-Staff": staff() };
    const init = { method, headers };
    if (raw) { init.body = raw.data; headers["Content-Type"] = "text/csv"; headers["X-File-Name"] = raw.name; }
    else if (body !== undefined) { init.body = JSON.stringify(body); headers["Content-Type"] = "application/json"; }
    const res = await fetch(url, init);
    let data = null; try { data = await res.json(); } catch (e) { /* not JSON */ }
    if (!res.ok) {
      const d = data && data.detail;
      throw new Error(typeof d === "string" ? d : Array.isArray(d) ? d.map((x) => x.msg).join("; ") : res.status === 401 ? "Please sign in again." : `Request failed (${res.status}).`);
    }
    return data;
  }
  const chip = (text, tone) => E("span", "obx-chip " + (tone || ""), text);
  const money = (l) => l.price ? `${l.currency === "INR" ? "₹" : "$"}${Number(l.price).toLocaleString(l.currency === "INR" ? "en-IN" : "en-US")}${l.listing_type === "rent" ? "/mo" : ""}` : "";
  const root = () => document.getElementById("ol-root");
  function say(node, text, tone) { node.textContent = text || ""; node.className = "obx-status " + (tone || ""); }

  async function load() {
    const box = root(); if (!box) return;
    try {
      const data = await api("GET", "/owner-leads");
      S.leads = data.leads; S.counts = data.counts; S.mode = data.whatsapp_mode; S.email = data.email; S.waSet = data.business_whatsapp_set;
      render();
    } catch (err) { box.replaceChildren(E("div", "obx-banner bad", err.message)); }
  }

  function render(message, tone) {
    const box = root(); box.replaceChildren();
    const head = E("div", "obx-head");
    const left = E("div");
    left.append(E("h2", null, "Owner leads"), E("p", "obx-muted", "Import the provider's owner list, call or message owners, and hand interested owners to the AI chat."));
    const modes = E("div", "obx-row");
    modes.append(chip(S.email.mode === "live" ? "Email: Live" : "Email: Test mode (nothing is sent)", S.email.mode === "live" ? "ok" : "warn"),
      chip(S.mode === "live" ? "WhatsApp: Live" : "WhatsApp: Test mode", S.mode === "live" ? "ok" : "warn"));
    head.append(left, modes);
    box.append(head);
    box.append(emailPanel());
    box.append(importPanel());
    const status = E("div", "obx-status"); if (message) say(status, message, tone); box.append(status);

    const bar = E("div", "obx-head"); bar.style.marginTop = "18px";
    const filters = E("div", "obx-actions");
    for (const key of ["all", ...Object.keys(STATUS)]) {
      const n = key === "all" ? S.leads.length : (S.counts[key] || 0);
      const b = E("button", "obx-btn small" + (S.filter === key ? " dark" : ""), `${key === "all" ? "All" : STATUS[key]} (${n})`); b.type = "button";
      b.onclick = () => { S.filter = key; render(); };
      filters.append(b);
    }
    const search = E("input"); search.type = "text"; search.placeholder = "Search name, phone, area…"; search.value = S.query; search.setAttribute("aria-label", "Search owner leads"); search.style.maxWidth = "280px";
    search.oninput = () => { S.query = search.value; drawList(list); };
    bar.append(filters, search);
    box.append(bar);
    const list = E("div", "obx-cards"); box.append(list);
    drawList(list);
  }

  function drawList(list) {
    const q = S.query.trim().toLowerCase();
    const rows = S.leads.filter((l) => (S.filter === "all" || l.status === S.filter) &&
      (!q || [l.owner_name, l.phone, l.email, l.area, l.city, l.property_title].some((v) => (v || "").toLowerCase().includes(q))));
    list.replaceChildren();
    if (!rows.length) { list.append(E("p", "obx-muted", S.leads.length ? "No owners in this list." : "No owner leads yet. Import a CSV above.")); return; }
    for (const l of rows) list.append(card(l));
  }

  function card(l) {
    const c = E("div", "obx-card");
    const top = E("div", "obx-row");
    top.append(chip(STATUS[l.status], TONE[l.status]), chip(CONSENT[l.consent], l.consent === "whatsapp" ? "ok" : ""));
    if (l.do_not_call) top.append(chip("Do Not Call", "bad"));
    if (l.replied_at) top.append(chip("Replied on WhatsApp", "ok"));
    if (l.response) top.append(chip(...RESPONSE[l.response]));
    else if (l.email) top.append(chip(...EMAIL[l.email_status]));
    else top.append(chip("No email address", ""));
    c.append(top, E("h3", null, l.owner_name));
    const dl = E("dl", "obx-dl");
    const pair = (k, v) => { if (v) dl.append(E("dt", null, k), E("dd", null, v)); };
    pair("Home", [l.property_title, l.area, l.city].filter(Boolean).join(" · "));
    pair("Price", [l.listing_type === "rent" ? "For rent" : l.listing_type === "sale" ? "For sale" : "", money(l)].filter(Boolean).join(" · "));
    pair("Phone", l.phone || "—");
    pair("Email", l.email);
    pair("Source", `${l.source}${l.listed_on ? " · listed " + l.listed_on : ""}`);
    c.append(dl);
    if (l.notes) c.append(E("div", "obx-next", l.notes.split("\n").slice(-2).join("\n")));
    const out = E("div", "obx-status");
    const actions = E("div", "obx-actions");
    const call = E("a", "obx-btn small", "Call"); call.href = l.phone && !l.do_not_call ? `tel:${l.phone}` : "#";
    if (!l.phone || l.do_not_call) { call.setAttribute("aria-disabled", "true"); call.style.opacity = ".45"; call.onclick = (e) => { e.preventDefault(); say(out, l.do_not_call ? "On the Do Not Call list. Don't call." : "No phone number.", "bad"); }; }
    actions.append(call);
    if (l.email) {
      const em = E("button", "obx-btn small", l.email_status === "not_sent" ? "Send email" : "Resend email"); em.type = "button";
      em.disabled = !l.email || !!l.response || ["do_not_contact", "converted", "not_interested", "interested"].includes(l.status);
      em.onclick = async () => {
        if (l.email_status !== "not_sent" && l.email_status !== "failed" && !confirm("This owner was already emailed. Send again?")) return;
        em.disabled = true;
        try { await api("POST", `/owner-leads/${l.id}/email`, { resend: l.email_status !== "not_sent" && l.email_status !== "failed" }); await load(); }
        catch (err) { say(out, err.message, "bad"); em.disabled = false; }
      };
      actions.append(em);
    }
    const wa = E("button", "obx-btn small dark", "WhatsApp intro"); wa.type = "button";
    if (!l.can_whatsapp) { wa.disabled = true; wa.title = l.consent !== "whatsapp" ? "Needs WhatsApp consent first" : "Not allowed for this owner"; }
    wa.onclick = () => openWhatsApp(l);
    actions.append(wa);
    const result = E("select"); result.setAttribute("aria-label", "Call result");
    result.add(new Option("Record result…", ""));
    for (const [k, v] of Object.entries(STATUS)) if (k !== "converted" && k !== l.status) result.add(new Option(v, k));
    result.disabled = l.status === "converted";
    result.onchange = async () => {
      if (!result.value) return;
      const note = result.value === "do_not_contact" ? "" : (prompt("Add a short note (optional):") || "");
      try { await api("PATCH", `/owner-leads/${l.id}`, { status: result.value, note }); await load(); }
      catch (err) { say(out, err.message, "bad"); result.value = ""; }
    };
    actions.append(result);
    const consent = E("button", "obx-btn small", "Record consent"); consent.type = "button"; consent.disabled = l.do_not_call || l.status === "converted";
    consent.onclick = async () => {
      const how = prompt("The owner agreed to WhatsApp messages. How and when? (e.g. 'agreed on call, 20 Sep')");
      if (!how || !how.trim()) return;
      try { await api("PATCH", `/owner-leads/${l.id}`, { consent: "whatsapp", consent_how: how }); await load(); }
      catch (err) { say(out, err.message, "bad"); }
    };
    actions.append(consent);
    if (l.status === "interested") {
      const conv = E("button", "obx-btn small primary", "Create listing"); conv.type = "button";
      conv.onclick = async () => {
        conv.disabled = true;
        try { const r = await api("POST", `/owner-leads/${l.id}/convert`); await load(); render(`Listing ${r.property_id} created. Review it in New listings before it goes live.`, "ok"); }
        catch (err) { say(out, err.message, "bad"); conv.disabled = false; }
      };
      actions.append(conv);
    }
    if (l.property_id) actions.append(chip(`Listing ${l.property_id}`, "ok"));
    c.append(actions, out);
    const hist = E("details"); hist.append(E("summary", "obx-small obx-muted", "History"));
    const list = E("div", "obx-audit"); hist.append(list);
    hist.addEventListener("toggle", async () => {
      if (!hist.open) return;
      try { const rows = await api("GET", `/owner-leads/${l.id}/activity`);
        list.replaceChildren(...rows.map((r) => E("div", null, `${new Date(r.created_at).toLocaleString()} · ${r.actor} · ${r.action.replaceAll("_", " ")}`))); }
      catch (err) { list.replaceChildren(E("div", null, err.message)); }
    });
    c.append(hist);
    return c;
  }

  async function openWhatsApp(l) {
    const modal = E("div", "obx-modal obx"); modal.setAttribute("role", "dialog"); modal.setAttribute("aria-label", "WhatsApp intro");
    const d = E("div", "obx-dialog"); modal.append(d);
    modal.addEventListener("click", (e) => { if (e.target === modal) modal.remove(); });
    d.append(E("h2", null, `WhatsApp ${l.owner_name}`), E("p", "obx-muted", `${l.phone} · ${S.mode === "live" ? "This sends a real WhatsApp message." : "Test mode: nothing is sent."}`));
    const text = E("textarea"); text.rows = 6; text.maxLength = 1000; text.setAttribute("aria-label", "Message");
    const firstName = (l.owner_name.replace("DEMO", "").trim().split(/\s+/)[0]) || "there";
    text.value = `Hi ${firstName}, this is Staybot. We saw your ${l.property_title || "property"}${l.area ? " in " + l.area : ""}${l.price ? " listed at " + money(l) : ""}. We help owners ${l.listing_type === "sale" ? "sell" : "rent out"} their homes faster with verified ${l.listing_type === "sale" ? "buyers" : "tenants"}, and we handle viewings and paperwork. Would you like us to take care of it for you? Reply YES to know more, or STOP and we won't message you again.`;
    d.append(text, E("p", "obx-small obx-muted", "Keep 'Reply STOP' in the message. When the owner replies, the AI chat continues the conversation."));
    const out = E("div", "obx-status");
    const foot = E("div", "obx-foot");
    const cancel = E("button", "obx-btn", "Cancel"); cancel.type = "button"; cancel.onclick = () => modal.remove();
    const send = E("button", "obx-btn primary", S.mode === "live" ? "Send WhatsApp" : "Send (test mode)"); send.type = "button";
    send.onclick = async () => {
      send.disabled = true; say(out, "Sending…");
      try { await api("POST", `/owner-leads/${l.id}/whatsapp`, { message: text.value }); modal.remove(); await load();
        render(S.mode === "live" ? `WhatsApp sent to ${l.owner_name}.` : `Test mode: message recorded for ${l.owner_name}, nothing was sent.`, "ok"); }
      catch (err) { say(out, err.message, "bad"); send.disabled = false; }
    };
    foot.append(cancel, send); d.append(out, foot);
    document.body.append(modal); text.focus();
  }

  function importPanel() {
    const panel = E("section", "obx-panel obx");
    panel.append(E("h3", null, "Import owners from a CSV file"));
    const form = E("div", "obx-form");
    const fileL = E("label"); const file = E("input"); file.type = "file"; file.accept = ".csv,text/csv"; fileL.append(E("span", null, "CSV file from the provider"), file);
    const countryL = E("label"); const country = E("select"); [["US", "United States (+1, $)"], ["IN", "India (+91, ₹)"]].forEach(([v, t]) => country.add(new Option(t, v)));
    countryL.append(E("span", null, "Phone numbers are from"), country);
    const sourceL = E("label", "full"); const source = E("input"); source.type = "text"; source.value = "CSV provider"; source.maxLength = 120;
    sourceL.append(E("span", null, "Provider name (saved with each owner)"), source);
    const autoL = E("label", "obx-check full"); const auto = E("input"); auto.type = "checkbox"; auto.checked = false;
    autoL.append(auto, E("span", null, "Email every new owner right after import (each owner is emailed once, with an unsubscribe link)"));
    form.append(fileL, countryL, sourceL, autoL);
    panel.append(form);
    const out = E("div", "obx-status");
    const actions = E("div", "obx-actions"); actions.style.marginTop = "12px";
    const previewB = E("button", "obx-btn dark", "Preview"); previewB.type = "button";
    const importB = E("button", "obx-btn primary", "Import"); importB.type = "button"; importB.hidden = true;
    const result = E("div");
    const url = (commit) => `/owner-leads/import?commit=${commit}&country=${country.value}&source=${encodeURIComponent(source.value)}&email_owners=${commit && auto.checked}`;
    previewB.onclick = async () => {
      const f = file.files[0]; if (!f) { say(out, "Choose a CSV file first.", "bad"); return; }
      previewB.disabled = true; say(out, "Checking the file…");
      try { const r = await api("POST", url(false), undefined, { data: f, name: f.name }); showPreview(result, r); say(out, "");
        importB.hidden = !r.summary.new; importB.textContent = `Import ${r.summary.new} new owner${r.summary.new === 1 ? "" : "s"}`; }
      catch (err) { say(out, err.message, "bad"); }
      finally { previewB.disabled = false; }
    };
    importB.onclick = async () => {
      const f = file.files[0]; if (!f) return;
      importB.disabled = true; say(out, "Importing…");
      try { const r = await api("POST", url(true), undefined, { data: f, name: f.name }); S.filter = "all";
        const emailed = r.emails_queued ? ` Emailing ${r.emails_queued} owner${r.emails_queued === 1 ? "" : "s"}${r.email_mode === "test" ? " (test mode: nothing is sent)" : ""}.` : "";
        await load(); render(`Imported ${r.created} owner${r.created === 1 ? "" : "s"}. ${r.summary.duplicate} duplicate, ${r.summary.skipped} skipped.${emailed}`, "ok");
        if (r.emails_queued) setTimeout(load, 2500); }
      catch (err) { say(out, err.message, "bad"); importB.disabled = false; }
    };
    actions.append(previewB, importB);
    panel.append(actions, out, result);
    return panel;
  }

  function showPreview(box, r) {
    box.replaceChildren();
    const s = r.summary;
    const chips = E("div", "obx-row"); chips.style.margin = "12px 0";
    chips.append(chip(`${s.new} new`, "ok"), chip(`${s.with_email} with email`, "info"), chip(`${s.duplicate} duplicate`, s.duplicate ? "warn" : ""), chip(`${s.skipped} skipped`, s.skipped ? "bad" : ""),
      chip(`${s.do_not_call} Do Not Call`, s.do_not_call ? "bad" : ""), chip(`${s.whatsapp_consent} can get WhatsApp`, "info"));
    box.append(chips);
    if (r.ignored_columns.length) box.append(E("p", "obx-small obx-muted", `Columns not used: ${r.ignored_columns.join(", ")}`));
    const wrap = E("div", "obx-scroll"); const t = E("table", "obx-table");
    const hr = E("tr"); ["Line", "Owner", "Phone", "Home", "Price", "Consent", "Result"].forEach((h) => hr.append(E("th", null, h)));
    const th = E("thead"); th.append(hr); t.append(th);
    const tb = E("tbody");
    for (const row of r.rows) {
      const tr = E("tr");
      const res = E("td"); res.append(chip(row.result === "new" ? "Will import" : row.result === "duplicate" ? "Duplicate" : "Skipped", row.result === "new" ? "ok" : row.result === "duplicate" ? "warn" : "bad"));
      if (row.reason) res.append(E("div", "obx-small obx-muted", row.reason));
      for (const issue of row.issues) res.append(E("div", "obx-small obx-muted", issue));
      if (row.do_not_call) res.append(E("div", "obx-small", "Do Not Call"));
      tr.append(E("td", null, String(row.line)), E("td", null, row.owner_name), E("td", null, row.phone || "—"),
        E("td", null, [row.property_title, row.area].filter(Boolean).join(" · ")), E("td", null, row.price ? `${row.currency} ${Number(row.price).toLocaleString()}` : ""),
        E("td", null, CONSENT[row.consent]), res);
      tb.append(tr);
    }
    t.append(tb); wrap.append(t); box.append(wrap);
  }

  function emailPanel() {
    const panel = E("div", "obx-actions"); panel.style.margin = "4px 0 16px";
    const ready = S.leads.filter((l) => l.can_email).length;
    const all = E("button", "obx-btn primary", `Email all owners not yet emailed (${ready})`); all.type = "button"; all.disabled = !ready;
    const out = E("span", "obx-small obx-muted");
    all.onclick = async () => {
      if (!confirm(`Send the intro email to ${ready} owner${ready === 1 ? "" : "s"}?${S.email.mode === "test" ? " (Test mode: nothing is really sent.)" : ""}`)) return;
      all.disabled = true;
      try { const r = await api("POST", "/owner-leads/email-all"); out.textContent = `Emailing ${r.queued} owners…`; setTimeout(load, 2500); }
      catch (err) { out.textContent = err.message; all.disabled = false; }
    };
    panel.append(all, out);
    if (!S.waSet) panel.append(E("span", "obx-small obx-muted", "Set BUSINESS_WHATSAPP_NUMBER so owners who say yes get a 'Chat on WhatsApp' button."));
    if (S.email.mode === "test") {
      const d = E("details"); d.style.width = "100%";
      d.append(E("summary", "obx-small", "Test outbox: emails that would have been sent"));
      const list = E("div", "obx-audit"); d.append(list);
      d.addEventListener("toggle", async () => {
        if (!d.open) return;
        const r = await api("GET", "/owner-leads/email-outbox");
        list.replaceChildren(...(r.outbox.length ? r.outbox.map((m) => {
          const row = E("div"); row.append(document.createTextNode(`${m.to} · ${m.subject} `));
          if (m.link) { const a = E("a", null, "open owner's page"); a.href = m.link; a.target = "_blank"; a.rel = "noopener"; row.append(a); }
          return row; }) : [E("div", null, "Nothing yet.")]));
      });
      panel.append(d);
    }
    return panel;
  }

  window.StaybotOwnerLeads = { load };
  if (location.hash === "#ownerleads") load();
})();
