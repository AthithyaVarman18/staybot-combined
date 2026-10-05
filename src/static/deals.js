// Deals: browse MLS homes, work out if they make money as a rental, shortlist them.
// Reuses the .obx styles injected by onboarding.js.
(function () {
  "use strict";
  const E = (tag, cls, text) => { const n = document.createElement(tag); if (cls) n.className = cls; if (text != null) n.textContent = text; return n; };
  const S = { tab: "search", listings: [], stats: null, deals: [], offerSummaries: {}, assumptions: null, current: null, filters: { city: "", min_beds: "", max_price: "", status: "active", sort: "price_asc" },
              // "For customer": investors (with cash on file) the list can be filtered for.
              investors: [], forInvestor: "", matchNote: null };
  const staff = () => { try { return localStorage.getItem("staybot.onboarding.actingStaff") || ""; } catch (e) { return ""; } };
  const DEAL_STATUS = { shortlist: "Shortlisted", offer_made: "Offer made", bought: "Bought", rejected: "Rejected" };

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
  const root = () => document.getElementById("deals-root");
  function say(node, text, tone) { node.textContent = text || ""; node.className = "obx-status " + (tone || ""); }

  async function load() {
    const box = root(); if (!box) return;
    box.replaceChildren(E("p", "obx-muted", "Loading homes for sale…"));
    try {
      const [stats, assumptions, deals] = await Promise.all([api("GET", "/mls/stats"), api("GET", "/deals/assumptions"), api("GET", "/deals")]);
      S.stats = stats; S.assumptions = assumptions.values; S.deals = deals;
      try { S.investors = ((await api("GET", "/investors")).investors || []).filter((i) => i.cash_available); }
      catch (err) { S.investors = []; }
      S.offerSummaries = {};
      const summaries = await Promise.all(deals.map(async (deal) => {
        try { return [deal.id, await api("GET", `/purchase-offers/by-deal/${deal.id}`)]; }
        catch (err) { return [deal.id, null]; }
      }));
      for (const [dealId, summary] of summaries) S.offerSummaries[dealId] = summary;
      await search();
    } catch (err) { box.replaceChildren(E("div", "obx-banner bad", err.message)); }
  }

  async function search() {
    if (S.forInvestor) {
      const data = await api("GET", `/investors/${encodeURIComponent(S.forInvestor)}/matches?limit=20`);
      S.listings = (data.matches || []).map((m) => ({ ...m.listing, _deal: { cash_needed: m.cash_needed, cash_flow_monthly: m.cash_flow_monthly, cap_rate_percent: m.cap_rate_percent } }));
      S.matchNote = data.alternative_note || null;
      render();
      return;
    }
    S.matchNote = null;
    const f = S.filters;
    const params = new URLSearchParams({ status: f.status, sort: f.sort, limit: "60" });
    if (f.city) params.set("city", f.city);
    if (f.min_beds) params.set("min_beds", f.min_beds);
    if (f.max_price) params.set("max_price", f.max_price);
    const data = await api("GET", "/mls?" + params);
    S.listings = data.listings;
    render();
  }

  function render(message, tone) {
    const box = root(); box.replaceChildren();
    const head = E("div", "obx-head");
    const left = E("div");
    left.append(E("h2", null, "Deals"), E("p", "obx-muted", "Homes for sale from the MLS export, plus homes other investors have listed for sale on Staybot, with the rental numbers worked out. Projections only — confirm taxes, insurance and rent before any offer."));
    const right = E("div", "obx-row");
    if (S.stats) right.append(chip(`${S.stats.total} homes`, "info"), chip(`middle price ${dollars(S.stats.price_middle)}`));
    head.append(left, right);
    box.append(head);
    const tabs = E("div", "obx-actions");
    for (const [key, label] of [["search", "Find homes"], ["shortlist", `Shortlist (${S.deals.length})`], ["settings", "Assumptions"]]) {
      const b = E("button", "obx-btn small" + (S.tab === key ? " dark" : ""), label); b.type = "button";
      b.onclick = () => { S.tab = key; render(); };
      tabs.append(b);
    }
    box.append(tabs);
    const status = E("div", "obx-status"); if (message) say(status, message, tone); box.append(status);
    ({ search: searchTab, shortlist: shortlistTab, settings: settingsTab })[S.tab](box, status);
  }

  function photoCell(l, size = 64) {
    const td = E("td");
    if (l.photo_url) {
      const img = E("img"); img.src = l.photo_url; img.alt = l.street_address || "Property photo";
      img.width = size; img.height = Math.round(size * 0.72); img.loading = "lazy"; img.referrerPolicy = "no-referrer";
      img.style.cssText = "object-fit:cover;border-radius:8px;background:var(--bg-soft)";
      img.onerror = () => { img.replaceWith(E("span", "obx-small obx-muted", "no photo")); };
      td.append(img);
    } else td.append(E("span", "obx-small obx-muted", "no photo"));
    return td;
  }

  function investorLabel(i) {
    const areas = Array.isArray(i.areas) ? i.areas.join(", ") : (i.areas || "");
    return [i.full_name || "Investor", dollars(i.cash_available) + " cash", areas, i.min_bedrooms ? i.min_bedrooms + "+ bed" : "", i.tier].filter(Boolean).join(" · ");
  }

  function searchTab(box, status) {
    // "For customer": pick an investor and the list becomes the homes that fit them.
    const who = E("select");
    who.add(new Option("Everyone (normal search)", ""));
    for (const i of S.investors) who.add(new Option(investorLabel(i), i.id));
    who.value = S.forInvestor;
    const whoRow = E("div", "obx-form");
    const wl = E("label"); wl.append(E("span", null, "For customer"), who); whoRow.append(wl);
    who.onchange = async () => {
      S.forInvestor = who.value;
      try { await search(); } catch (err) { say(status, err.message, "bad"); }
    };
    box.append(whoRow);
    const picked = S.investors.find((i) => i.id === S.forInvestor);
    if (picked) {
      const banner = E("div", "obx-banner");
      banner.append(E("b", null, `Homes that fit ${picked.full_name || "this investor"}: `), document.createTextNode(investorLabel(picked)));
      if (S.matchNote) banner.append(document.createElement("br"), E("span", "obx-small obx-muted", S.matchNote));
      box.append(banner);
    }
    const form = E("div", "obx-form");
    if (picked) form.style.display = "none";   // the customer's own cash / areas / beds decide the list
    const city = E("select"); city.add(new Option("All cities", ""));
    for (const c of (S.stats?.cities || [])) city.add(new Option(`${c.city} (${c.count})`, c.city));
    city.value = S.filters.city;
    const beds = E("select"); [["", "Any beds"], ["2", "2+ beds"], ["3", "3+ beds"], ["4", "4+ beds"], ["5", "5+ beds"]].forEach(([v, t]) => beds.add(new Option(t, v)));
    beds.value = S.filters.min_beds;
    const price = E("select"); [["", "Any price"], ["250000", "under $250k"], ["350000", "under $350k"], ["450000", "under $450k"], ["600000", "under $600k"]].forEach(([v, t]) => price.add(new Option(t, v)));
    price.value = S.filters.max_price;
    const sort = E("select"); [["price_asc", "Cheapest first"], ["price_desc", "Most expensive first"], ["newest", "Newest listing"]].forEach(([v, t]) => sort.add(new Option(t, v)));
    sort.value = S.filters.sort;
    for (const [label, control] of [["City", city], ["Bedrooms", beds], ["Max price", price], ["Sort", sort]]) {
      const l = E("label"); l.append(E("span", null, label), control); form.append(l);
    }
    const apply = async () => {
      S.filters = { ...S.filters, city: city.value, min_beds: beds.value, max_price: price.value, sort: sort.value };
      try { await search(); } catch (err) { say(status, err.message, "bad"); }
    };
    [city, beds, price, sort].forEach((c) => { c.onchange = apply; });
    box.append(form);
    box.append(E("p", "obx-muted obx-small", `${S.listings.length} homes shown`));
    const wrap = E("div", "obx-scroll"); const t = E("table", "obx-table");
    const hr = E("tr");
    (picked ? ["", "Address", "City", "Price", "Beds/baths", "Cash needed", "Cash flow /mo", "Return", ""]
            : ["", "Address", "City", "Price", "Beds/baths", "Size", "Built", "Taxes/yr", "HOA", ""]).forEach((h) => hr.append(E("th", null, h)));
    const th = E("thead"); th.append(hr); t.append(th);
    const tb = E("tbody");
    for (const l of S.listings) {
      const tr = E("tr");
      const hoa = l.hoa_fee ? `${dollars(l.hoa_fee)} ${l.hoa_frequency || ""}`.trim() : "—";
      const addr = E("td", null, l.street_address || "—");
      if (l.owner_listed) { addr.append(document.createElement("br"), chip("Listed by investor", "info")); }
      tr.append(photoCell(l),
        addr, E("td", null, l.city || "—"), E("td", null, dollars(l.list_price)),
        E("td", null, `${l.bedrooms ?? "—"} / ${l.bathrooms_full ?? "—"}`));
      if (picked && l._deal) {
        const flow = Number(l._deal.cash_flow_monthly);
        tr.append(E("td", null, dollars(l._deal.cash_needed)),
          E("td", null, isNaN(flow) ? "—" : (flow >= 0 ? "+" : "-") + dollars(Math.abs(flow))),
          E("td", null, pct(l._deal.cap_rate_percent)));
      } else {
        tr.append(E("td", null, l.living_area ? Number(l.living_area).toLocaleString() + " sq ft" : "—"),
          E("td", null, l.year_built || "—"), E("td", null, dollars(l.tax_annual)), E("td", null, hoa));
      }
      const act = E("td"); const b = E("button", "obx-btn small dark", "Analyse"); b.type = "button";
      b.onclick = () => openAnalyser(l);
      act.append(b);
      if (picked) {
        // Same as Investors -> "Find homes that fit" -> send: one-page deal PDF on WhatsApp.
        const send = E("button", "obx-btn small", picked.whatsapp ? `Send to ${(picked.full_name || "investor").split(" ")[0]}` : "No WhatsApp");
        send.type = "button";
        send.disabled = !picked.whatsapp;
        send.onclick = async () => {
          send.disabled = true; send.textContent = "Sending…";
          try { await api("POST", `/investors/${encodeURIComponent(picked.id)}/send-deal`, { list_number: l.list_number }); send.textContent = "✅ Sent"; }
          catch (err) { send.textContent = "Send"; send.disabled = false; say(status, err.message, "bad"); }
        };
        act.append(document.createTextNode(" "), send);
      }
      tr.append(act); tb.append(tr);
    }
    t.append(tb); wrap.append(t); box.append(wrap);
  }

  async function openAnalyser(listing) {
    const modal = E("div", "obx-modal obx"); modal.setAttribute("role", "dialog"); modal.setAttribute("aria-label", "Analyse deal");
    const d = E("div", "obx-dialog"); modal.append(d);
    modal.addEventListener("click", (e) => { if (e.target === modal) modal.remove(); });
    if (listing.photo_url) {
      const hero = E("img"); hero.src = listing.photo_url; hero.alt = listing.street_address || "Property photo";
      hero.referrerPolicy = "no-referrer"; hero.loading = "lazy";
      hero.style.cssText = "width:100%;max-height:230px;object-fit:cover;border-radius:14px;margin-bottom:10px";
      hero.onerror = () => hero.remove();
      d.append(hero);
    }
    d.append(E("div", "obx-kicker", "Deal analysis"), E("h2", null, listing.street_address || "Property"),
      E("p", "obx-muted", `${[listing.city, listing.postal_code].filter(Boolean).join(" ")} · ${dollars(listing.list_price)} · ${listing.bedrooms ?? "?"} bed · ${listing.living_area ? Number(listing.living_area).toLocaleString() + " sq ft" : "size unknown"}${listing.days_on_market != null ? " · " + listing.days_on_market + " days on market" : ""}`));
    const form = E("div", "obx-form");
    const rent = E("input"); rent.type = "number"; rent.min = "1"; rent.step = "25"; rent.setAttribute("aria-label", "Expected monthly rent");
    const rehab = E("input"); rehab.type = "number"; rehab.min = "0"; rehab.step = "500"; rehab.value = "0";
    const rentL = E("label"); rentL.append(E("span", null, "Expected monthly rent"), rent, E("small", "obx-muted", "Loading a guide from our own listings…"));
    const rehabL = E("label"); rehabL.append(E("span", null, "Repairs before renting"), rehab);
    form.append(rentL, rehabL);
    d.append(form);
    const out = E("div", "obx-status");
    const results = E("div");
    const foot = E("div", "obx-foot");
    const close = E("button", "obx-btn", "Close"); close.type = "button"; close.onclick = () => modal.remove();
    const run = E("button", "obx-btn primary", "Work out the numbers"); run.type = "button";
    foot.append(close, run);
    d.append(out, results, foot);
    document.body.append(modal);
    api("GET", `/deals/rent-estimate?list_number=${encodeURIComponent(listing.list_number)}`).then((g) => {
      const hint = rentL.querySelector("small");
      if (g.estimate) { rent.value = Math.round(g.estimate); hint.textContent = `Guide: ${dollars(g.estimate)} from ${g.based_on} of our own rentals in ${g.city}. Confirm with local comparables.`; }
      else hint.textContent = g.note;
    }).catch(() => { rentL.querySelector("small").textContent = "Enter the rent from local comparables."; });
    rent.focus();

    let analysis = null;
    run.onclick = async () => {
      if (!rent.value || Number(rent.value) <= 0) { say(out, "Enter the expected monthly rent.", "bad"); return; }
      run.disabled = true; say(out, "Calculating…");
      try {
        analysis = await api("POST", "/deals/analyze", { list_number: listing.list_number, monthly_rent: Number(rent.value), rehab: Number(rehab.value || 0) });
        say(out, "");
        showResults(results, analysis, listing, modal);
      } catch (err) { say(out, err.message, "bad"); }
      finally { run.disabled = false; }
    };
  }

  function showResults(box, a, listing, modal) {
    box.replaceChildren();
    const r = a.results;
    const good = r.cash_flow_monthly >= 0;
    const top = E("div", "obx-box"); top.style.marginTop = "14px";
    top.append(E("div", "obx-story", `${good ? "+" : "-"}${dollars(Math.abs(r.cash_flow_monthly))} a month`),
      E("div", "obx-row"), E("p", "obx-small obx-muted", `Break-even rent ${dollars(r.breakeven_rent)}`));
    top.querySelector(".obx-row").append(chip(`Cap rate ${pct(r.cap_rate_percent)}`, "info"), chip(`Cash-on-cash ${pct(r.cash_on_cash_percent)}`, good ? "ok" : "bad"),
      chip(`DSCR ${r.dscr ?? "—"}`, r.dscr >= 1.25 ? "ok" : "warn"), chip(`Cash to buy ${dollars(a.cash_needed.total)}`));
    box.append(top);
    const two = E("div", "obx-two"); two.style.marginTop = "12px";
    const money = E("div", "obx-box"); money.append(E("h4", null, "Every month"));
    const dl = E("dl", "obx-dl");
    const row = (k, v) => dl.append(E("dt", null, k), E("dd", null, v));
    row("Rent", dollars(a.income.monthly_rent));
    for (const [k, label] of [["taxes", "Taxes"], ["insurance", "Insurance"], ["hoa", "HOA"], ["management", "Management"], ["maintenance", "Maintenance"], ["vacancy", "Empty periods"], ["other", "Other"]])
      if (a.costs_monthly[k]) row(label, "-" + dollars(a.costs_monthly[k]));
    row("Loan payment", "-" + dollars(a.loan.monthly_payment));
    row("Cash flow", (good ? "+" : "-") + dollars(Math.abs(r.cash_flow_monthly)));
    money.append(dl);
    const buy = E("div", "obx-box"); buy.append(E("h4", null, "To buy it"));
    const dl2 = E("dl", "obx-dl");
    dl2.append(E("dt", null, "Price"), E("dd", null, dollars(a.inputs.price)), E("dt", null, `Down (${a.inputs.down_payment_percent}%)`), E("dd", null, dollars(a.cash_needed.down_payment)),
      E("dt", null, "Closing costs"), E("dd", null, dollars(a.cash_needed.closing_costs)), E("dt", null, "Repairs"), E("dd", null, dollars(a.cash_needed.repairs)),
      E("dt", null, "Total cash"), E("dd", null, dollars(a.cash_needed.total)), E("dt", null, "Loan"), E("dd", null, `${dollars(a.loan.amount)} at ${a.inputs.interest_rate_percent}%`));
    buy.append(dl2);
    two.append(money, buy); box.append(two);
    if (a.sensitivity) {
      const sens = E("div", "obx-box"); sens.style.marginTop = "12px";
      sens.append(E("h4", null, "If things change"));
      const dl3 = E("dl", "obx-dl");
      dl3.append(E("dt", null, "Rent 10% lower"), E("dd", null, dollars(a.sensitivity.rent_down_10_percent) + "/mo"),
        E("dt", null, "Rent 10% higher"), E("dd", null, dollars(a.sensitivity.rent_up_10_percent) + "/mo"),
        E("dt", null, "Rate 1% higher"), E("dd", null, dollars(a.sensitivity.rate_up_1_percent) + "/mo"),
        E("dt", null, "Rate 1% lower"), E("dd", null, dollars(a.sensitivity.rate_down_1_percent) + "/mo"));
      sens.append(dl3);
      if (a.area) sens.append(E("p", "obx-small obx-muted", "Price per sq ft: " + a.area.summary));
      box.append(sens);
    }
    if (a.flags.length) {
      const f = E("div", "obx-missing"); f.append(E("b", null, "Watch out"));
      const ul = E("ul"); a.flags.forEach((x) => ul.append(E("li", null, x.text))); f.append(ul); box.append(f);
    }
    box.append(E("p", "obx-small obx-muted", "Where the numbers came from: " + Object.entries(a.sources).map(([k, v]) => `${k.replace(/_/g, " ")}: ${v}`).join("; ")));
    const actions = E("div", "obx-actions"); actions.style.marginTop = "10px";
    const save = E("button", "obx-btn dark", "Save to shortlist"); save.type = "button";
    const out = E("div", "obx-status");
    save.onclick = async () => {
      save.disabled = true;
      try {
        const deal = await api("POST", "/deals", { list_number: listing.list_number, label: listing.street_address || listing.list_number, analysis: a, notes: "" });
        S.deals = await api("GET", "/deals");
        modal.remove(); S.tab = "shortlist"; render("Saved to the shortlist.", "ok");
        window.open(`/deals/${deal.id}/summary.pdf`, "_blank", "noopener");
      } catch (err) { say(out, err.message, "bad"); save.disabled = false; }
    };
    actions.append(save, E("span", "obx-small obx-muted", "Saving also opens the one-page PDF you can send the investor."));
    box.append(actions, out);
  }

  function shortlistTab(box, status) {
    if (!S.deals.length) { box.append(E("p", "obx-muted", "Nothing shortlisted yet. Find a home and analyse it.")); return; }
    const grid = E("div", "obx-cards");
    for (const deal of S.deals) {
      const r = (deal.results || {}).results || {};
      const c = E("div", "obx-card");
      const flow = Number(r.cash_flow_monthly || 0);
      c.append(E("div", "obx-row"), E("h3", null, deal.label));
      c.querySelector(".obx-row").append(chip(DEAL_STATUS[deal.status], deal.status === "bought" ? "ok" : deal.status === "rejected" ? "" : "info"),
        chip(`${flow >= 0 ? "+" : "-"}${dollars(Math.abs(flow))}/mo`, flow >= 0 ? "ok" : "bad"),
        chip(`Cap ${pct(r.cap_rate_percent)}`), chip(`CoC ${pct(r.cash_on_cash_percent)}`));
      const dl = E("dl", "obx-dl");
      dl.append(E("dt", null, "Price"), E("dd", null, dollars((deal.inputs || {}).price)), E("dt", null, "Rent used"), E("dd", null, dollars((deal.inputs || {}).monthly_rent)),
        E("dt", null, "Cash to buy"), E("dd", null, dollars(((deal.results || {}).cash_needed || {}).total)));
      c.append(dl);
      const offerSummary = S.offerSummaries[deal.id];
      // null = the staff offer API refused/failed (e.g. an investor account:
      // /purchase-offers is staff-only) - show nothing rather than a wrong "no offer".
      if (offerSummary) {
      const offerBox = E("div", "obx-box"); offerBox.style.marginTop = "12px";
      offerBox.append(E("h4", null, "Offer workflow"));
      if (offerSummary && offerSummary.offer) {
        const o = offerSummary.offer;
        const h = offerSummary.handoff;
        offerBox.append(E("p", "obx-small", `${o.reference} · ${o.status.replaceAll("_", " ")} · version ${o.current_version}`));
        if (h) offerBox.append(E("p", "obx-small obx-muted", `${h.platform} · ${h.status}${h.platform_link ? " · handoff link saved" : ""}`));
        if (offerSummary.deadlines && offerSummary.deadlines.length) {
          const next = offerSummary.deadlines[0];
          offerBox.append(E("p", "obx-small obx-muted", `${next.label}: ${next.due_at}`));
        }
      } else {
        offerBox.append(E("p", "obx-small obx-muted", "No purchase offer has been recorded for this deal yet."));
      }
      const offerLink = E("a", "obx-btn small", "Open purchase offer"); offerLink.href = `/team/purchase-offers?deal=${encodeURIComponent(deal.id)}`; offerLink.target = "_blank"; offerLink.rel = "noopener";
      offerBox.append(offerLink);
      c.append(offerBox);
      }
      const actions = E("div", "obx-actions");
      const pdf = E("a", "obx-btn small", "Deal PDF"); pdf.href = `/deals/${deal.id}/summary.pdf`; pdf.target = "_blank"; pdf.rel = "noopener";
      const sel = E("select"); sel.setAttribute("aria-label", "Deal status");
      for (const [k, v] of Object.entries(DEAL_STATUS)) sel.add(new Option(v, k));
      sel.value = deal.status;
      sel.onchange = async () => {
        try { await api("PATCH", `/deals/${deal.id}`, { status: sel.value }); S.deals = await api("GET", "/deals"); render("Updated.", "ok"); }
        catch (err) { say(status, err.message, "bad"); }
      };
      actions.append(pdf, sel);
      c.append(actions);
      grid.append(c);
    }
    box.append(grid);
  }

  function settingsTab(box, status) {
    box.append(E("p", "obx-muted", "These defaults are used for every calculation. Ask the client for their real numbers and put them here."));
    const a = S.assumptions || {};
    const form = E("div", "obx-form");
    const fields = [["down_payment_percent", "Down payment %"], ["interest_rate_percent", "Interest rate %"], ["loan_years", "Loan years"],
      ["closing_costs_percent", "Closing costs %"], ["management_percent", "Management % of rent"], ["maintenance_percent", "Maintenance % of rent"],
      ["vacancy_percent", "Empty periods % of rent"], ["insurance_annual", "Insurance per year ($)"], ["other_monthly", "Other monthly costs ($)"]];
    const inputs = {};
    for (const [key, label] of fields) {
      const l = E("label"); const i = E("input"); i.type = "number"; i.step = "0.1"; i.value = a[key] ?? "";
      l.append(E("span", null, label), i); form.append(l); inputs[key] = i;
    }
    box.append(form);
    const save = E("button", "obx-btn primary", "Save assumptions"); save.type = "button"; save.style.marginTop = "12px";
    save.onclick = async () => {
      save.disabled = true;
      try {
        const body = {}; for (const [key] of fields) body[key] = Number(inputs[key].value || 0);
        const saved = await api("PUT", "/deals/assumptions", body); S.assumptions = saved.values;
        render("Assumptions saved.", "ok");
      } catch (err) { say(status, err.message, "bad"); save.disabled = false; }
    };
    box.append(save);

  }

  // forInvestor(id): open Deals filtered to the homes that fit this investor
  // (used by the Inquiries pop-up's "Open in Deals").
  async function forInvestor(id) { S.forInvestor = id || ""; S.tab = "search"; await load(); }
  // setInvestor / setFilters: choose what Deals shows next time it opens.
  function setInvestor(id) { S.forInvestor = id || ""; S.tab = "search"; }
  function setFilters(f) {
    S.forInvestor = ""; S.tab = "search";
    S.filters = { ...S.filters, city: f.city || "", min_beds: f.min_beds ? String(f.min_beds) : "", max_price: f.max_price ? String(Math.round(f.max_price)) : "" };
  }
  // analyse(listing): the same "Analyse deal" box, opened on top of any page
  // (the Inquiries tab uses it so staff never leave the customer).
  async function analyse(listing) {
    if (!S.assumptions) { try { S.assumptions = (await api("GET", "/deals/assumptions")).values; } catch (e) { /* the box works without */ } }
    return openAnalyser(listing);
  }
  window.StaybotDeals = { load, forInvestor, setInvestor, setFilters, analyse };
  if (location.hash === "#deals") load();
})();
