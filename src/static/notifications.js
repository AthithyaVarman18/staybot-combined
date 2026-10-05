/*
 * "Someone asked about your house" - pop-ups for owners (investor accounts).
 *
 * When a tenant or a new property investor asks about a home this owner
 * listed (in Chat, on WhatsApp, with "Enquire about this", or on Messages),
 * src/services/owner_notifications.py saves a notification with the asker's
 * details. This script:
 *   - checks GET /me/notifications on load, every 20 seconds and when the tab
 *     regains focus,
 *   - pops each new one up (top-right) with the asker's name, account type,
 *     email, phone, what we know about them, and their question - it stays
 *     until the owner closes it ("Dismiss" marks it read),
 *   - adds a bell with an unread count next to the account box in the sidebar,
 *     listing recent notifications.
 */
(function () {
  if (window.__staybotOwnerNotifications) return;
  window.__staybotOwnerNotifications = true;

  var POLL_MS = 20000;
  var MAX_POPUPS = 3;
  var OWNER_ROLES = ["new_investor", "existing_investor"];
  var POPPED_KEY = "staybot.ownerNotifications.popped";
  var SOURCE_TEXT = {
    chat: "asked in Chat", whatsapp: "asked on WhatsApp",
    enquiry: "sent an enquiry", message: "messaged you"
  };

  var accountId = null;
  var items = [];
  var popups = {};        // id -> element
  var bell = null, badge = null, panel = null;
  var checking = false;

  // ---------- helpers ----------
  function esc(v) {
    return String(v == null ? "" : v).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function ago(iso) {
    var t = Date.parse(iso || "");
    if (!t) return "";
    var s = Math.max(0, Math.round((Date.now() - t) / 1000));
    if (s < 60) return "just now";
    if (s < 3600) return Math.round(s / 60) + " min ago";
    if (s < 86400) return Math.round(s / 3600) + " h ago";
    return new Date(t).toLocaleDateString();
  }
  function poppedAll() { try { return JSON.parse(localStorage.getItem(POPPED_KEY) || "{}") || {}; } catch (e) { return {}; } }
  function popKey(n) { return n.id + ":" + (n.ask_count || 1); }
  function wasPopped(n) { var a = poppedAll(); return !!(a[accountId] && a[accountId][popKey(n)]); }
  function markPopped(n) {
    var a = poppedAll(); a[accountId] = a[accountId] || {};
    a[accountId][popKey(n)] = Date.now();
    var keys = Object.keys(a[accountId]);                 // keep it small
    if (keys.length > 300) keys.slice(0, keys.length - 300).forEach(function (k) { delete a[accountId][k]; });
    try { localStorage.setItem(POPPED_KEY, JSON.stringify(a)); } catch (e) {}
  }
  function api(method, url) {
    return fetch(url, { method: method, credentials: "same-origin", headers: { Accept: "application/json" } })
      .then(function (r) { return r.ok ? r.json() : Promise.reject(new Error("HTTP " + r.status)); });
  }

  // ---------- styles ----------
  function injectStyles() {
    if (document.getElementById("onf-styles")) return;
    var css =
      ".onf-stack{position:fixed;top:16px;right:16px;z-index:99990;display:flex;flex-direction:column;gap:12px;" +
      "width:min(380px,calc(100vw - 32px));pointer-events:none}" +
      ".onf-card{pointer-events:auto;background:#fff;color:#222;border-radius:16px;overflow:hidden;" +
      "box-shadow:0 18px 48px rgba(18,40,63,.28),0 2px 6px rgba(0,0,0,.08);border:1px solid #e6eaef;" +
      "transform:translateX(24px);opacity:0;transition:transform .28s cubic-bezier(.2,.9,.3,1.1),opacity .2s ease;font-family:inherit}" +
      ".onf-card.on{transform:none;opacity:1}" +
      ".onf-head{display:flex;align-items:center;gap:10px;padding:12px 14px;background:linear-gradient(135deg,#1B3A5C,#12283F);color:#fff}" +
      ".onf-head .ic{font-size:20px;line-height:1}" +
      ".onf-head .tt{flex:1;min-width:0;font-size:13.5px;font-weight:600;line-height:1.3}" +
      ".onf-head .tt small{display:block;font-weight:400;opacity:.75;font-size:12px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}" +
      ".onf-x{appearance:none;border:0;background:rgba(255,255,255,.12);color:#fff;width:28px;height:28px;border-radius:50%;cursor:pointer;font-size:16px;line-height:1}" +
      ".onf-x:hover{background:rgba(255,255,255,.22)}" +
      ".onf-body{padding:12px 14px 14px;font-size:13.5px;line-height:1.45}" +
      ".onf-who{display:flex;align-items:center;gap:10px;margin-bottom:8px}" +
      ".onf-av{width:38px;height:38px;border-radius:50%;background:#EAF0F6;color:#1B3A5C;display:grid;place-items:center;font-weight:700;flex:none}" +
      ".onf-name{font-weight:700;font-size:14.5px}" +
      ".onf-role{display:inline-block;font-size:11px;font-weight:600;padding:1px 8px;border-radius:999px;margin-left:4px;vertical-align:1px}" +
      ".onf-role.tenant{background:#E6F4EA;color:#1E6B35}.onf-role.new_investor{background:#FDF1DE;color:#8A5A12}" +
      ".onf-contact{font-size:12.5px;color:#555}.onf-contact a{color:#1B3A5C;font-weight:600;text-decoration:none}" +
      ".onf-q{margin:8px 0;padding:8px 10px;background:#F6F8FA;border-left:3px solid #B8873C;border-radius:6px;color:#333;white-space:pre-wrap;word-break:break-word}" +
      ".onf-facts{display:grid;grid-template-columns:auto 1fr;gap:2px 10px;font-size:12.5px;margin-top:6px}" +
      ".onf-facts dt{color:#777}.onf-facts dd{margin:0;color:#222;word-break:break-word}" +
      ".onf-meta{font-size:11.5px;color:#888;margin-top:8px}" +
      ".onf-actions{display:flex;gap:8px;margin-top:12px;flex-wrap:wrap}" +
      ".onf-btn{appearance:none;font:inherit;font-size:13px;font-weight:600;padding:7px 14px;border-radius:999px;cursor:pointer;" +
      "border:1px solid #d5d9de;background:#fff;color:#222;text-decoration:none;display:inline-block}" +
      ".onf-btn.primary{background:#1B3A5C;border-color:#1B3A5C;color:#fff}" +
      ".onf-btn:focus-visible,.onf-x:focus-visible,.onf-bell:focus-visible{outline:2px solid #B8873C;outline-offset:2px}" +
      ".onf-bell{position:relative;appearance:none;border:0;cursor:pointer;width:36px;height:36px;border-radius:10px;" +
      "background:rgba(255,255,255,.1);color:#fff;display:grid;place-items:center;flex:none}" +
      ".onf-bell:hover{background:rgba(255,255,255,.2)}" +
      ".onf-bell.floating{position:fixed;right:18px;bottom:18px;z-index:99980;width:48px;height:48px;border-radius:50%;" +
      "background:#1B3A5C;box-shadow:0 8px 24px rgba(0,0,0,.25)}" +
      ".onf-badge{position:absolute;top:-4px;right:-4px;min-width:18px;height:18px;padding:0 5px;border-radius:9px;" +
      "background:#E5484D;color:#fff;font-size:11px;font-weight:700;line-height:18px;text-align:center}" +
      ".onf-bell.ring svg{animation:onf-ring .8s ease}" +
      "@keyframes onf-ring{0%,100%{transform:rotate(0)}20%{transform:rotate(14deg)}40%{transform:rotate(-12deg)}60%{transform:rotate(8deg)}80%{transform:rotate(-4deg)}}" +
      ".onf-panel{position:fixed;z-index:99985;width:min(380px,calc(100vw - 24px));max-height:min(520px,calc(100vh - 24px));" +
      "background:#fff;color:#222;border-radius:14px;box-shadow:0 18px 48px rgba(0,0,0,.25);border:1px solid #e6eaef;" +
      "display:flex;flex-direction:column;overflow:hidden;font-family:inherit}" +
      ".onf-panel header{display:flex;align-items:center;justify-content:space-between;padding:12px 14px;border-bottom:1px solid #eef1f4;font-weight:700}" +
      ".onf-panel header button{appearance:none;border:0;background:none;color:#1B3A5C;font:inherit;font-size:12.5px;font-weight:600;cursor:pointer}" +
      ".onf-list{overflow:auto}" +
      ".onf-row{display:block;width:100%;text-align:left;appearance:none;border:0;border-bottom:1px solid #f0f2f5;background:#fff;" +
      "padding:10px 14px;font:inherit;font-size:13px;cursor:pointer;color:#222}" +
      ".onf-row:hover{background:#F6F8FA}.onf-row.unread{background:#F3F7FB}" +
      ".onf-row b{font-weight:700}.onf-row .sub{display:block;color:#666;font-size:12px;margin-top:2px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}" +
      ".onf-empty{padding:24px 14px;text-align:center;color:#888;font-size:13px}" +
      "@media (prefers-reduced-motion:reduce){.onf-card{transition:none}.onf-bell.ring svg{animation:none}}";
    var s = document.createElement("style");
    s.id = "onf-styles"; s.textContent = css;
    document.head.appendChild(s);
  }

  // ---------- a notification's details ----------
  function cardHtml(n) {
    var a = n.asker || {};
    var initials = String(a.name || "?").trim().split(/\s+/).slice(0, 2).map(function (w) { return w[0]; }).join("").toUpperCase();
    var contact = [];
    if (a.email) contact.push('<a href="mailto:' + esc(a.email) + '">' + esc(a.email) + "</a>");
    if (a.phone) contact.push('<a href="tel:' + esc(String(a.phone).replace(/[^\d+]/g, "")) + '">' + esc(a.phone) + "</a>");
    var facts = (a.facts || []).map(function (f) { return "<dt>" + esc(f.label) + "</dt><dd>" + esc(f.value) + "</dd>"; }).join("");
    var more = (n.ask_count || 1) > 1 ? " \u00b7 " + n.ask_count + " questions" : "";
    return '<div class="onf-who"><div class="onf-av" aria-hidden="true">' + esc(initials) + "</div><div>" +
      '<div class="onf-name">' + esc(a.name || "Someone") +
      (a.role_label ? '<span class="onf-role ' + esc(n.asker_role || "") + '">' + esc(a.role_label) + "</span>" : "") + "</div>" +
      '<div class="onf-contact">' + (contact.join(" \u00b7 ") || "No contact details yet") + "</div></div></div>" +
      (n.question ? '<div class="onf-q">' + esc(n.question) + "</div>" : "") +
      (facts ? '<dl class="onf-facts">' + facts + "</dl>" : "") +
      '<div class="onf-meta">' + esc(SOURCE_TEXT[n.source] || "asked") + " \u00b7 " + esc(ago(n.updated_at || n.created_at)) + esc(more) + "</div>";
  }

  function actionsHtml(n) {
    var a = n.asker || {};
    var html = "";
    if (n.source === "message") html += '<a class="onf-btn primary" href="#messages" data-go>Reply in Messages</a>';
    else if (a.email) html += '<a class="onf-btn primary" href="mailto:' + esc(a.email) + "?subject=" +
      encodeURIComponent("About " + (n.property_title || "my home")) + '">Email ' + esc(String(a.name || "them").split(" ")[0]) + "</a>";
    if (a.phone) html += '<a class="onf-btn" href="tel:' + esc(String(a.phone).replace(/[^\d+]/g, "")) + '">Call</a>';
    html += '<button class="onf-btn" type="button" data-dismiss>Dismiss</button>';
    return html;
  }

  // ---------- pop-ups ----------
  function stack() {
    var s = document.getElementById("onf-stack");
    if (!s) {
      s = document.createElement("div");
      s.id = "onf-stack"; s.className = "onf-stack";
      s.setAttribute("aria-live", "polite");
      document.body.appendChild(s);
    }
    return s;
  }

  function popup(n) {
    if (popups[n.id]) popups[n.id].remove();
    markPopped(n);
    var card = document.createElement("section");
    card.className = "onf-card";
    card.setAttribute("role", "alert");
    card.innerHTML =
      '<div class="onf-head"><span class="ic" aria-hidden="true">\ud83d\udd14</span><div class="tt">Someone asked about your house' +
      "<small>" + esc(n.property_title || "your home") + "</small></div>" +
      '<button class="onf-x" type="button" aria-label="Close">\u00d7</button></div>' +
      '<div class="onf-body">' + cardHtml(n) + '<div class="onf-actions">' + actionsHtml(n) + "</div></div>";
    function close(read) {
      card.classList.remove("on");
      setTimeout(function () { card.remove(); }, 250);
      delete popups[n.id];
      if (read) markRead(n);
    }
    card.querySelector(".onf-x").addEventListener("click", function () { close(true); });
    card.querySelector("[data-dismiss]").addEventListener("click", function () { close(true); });
    var go = card.querySelector("[data-go]");
    if (go) go.addEventListener("click", function () { close(true); });
    var s = stack();
    s.insertBefore(card, s.firstChild);
    popups[n.id] = card;
    while (s.children.length > MAX_POPUPS) {            // oldest pop-ups fold into the bell
      var last = s.lastChild;
      Object.keys(popups).forEach(function (k) { if (popups[k] === last) delete popups[k]; });
      last.remove();
    }
    requestAnimationFrame(function () { card.classList.add("on"); });
    if (bell) { bell.classList.remove("ring"); void bell.offsetWidth; bell.classList.add("ring"); }
  }

  function markRead(n) {
    if (n.read_at) return;
    n.read_at = new Date().toISOString();
    renderBell();
    api("POST", "/me/notifications/" + encodeURIComponent(n.id) + "/read").catch(function () {});
  }

  // ---------- bell + list ----------
  function makeBell() {
    if (bell) return;
    bell = document.createElement("button");
    bell.type = "button";
    bell.className = "onf-bell";
    bell.setAttribute("aria-label", "Notifications");
    bell.innerHTML = '<svg viewBox="0 0 24 24" width="20" height="20" aria-hidden="true"><path d="M6 8a6 6 0 1 1 12 0c0 7 3 9 3 9H3s3-2 3-9M10.3 21a1.94 1.94 0 0 0 3.4 0" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>';
    badge = document.createElement("span");
    badge.className = "onf-badge"; badge.hidden = true;
    bell.appendChild(badge);
    var box = document.getElementById("admin-box");
    var logout = document.getElementById("admin-logout");
    if (box && logout) box.insertBefore(bell, logout);
    else { bell.classList.add("floating"); document.body.appendChild(bell); }
    bell.addEventListener("click", function (e) { e.stopPropagation(); panel ? closePanel() : openPanel(); });
    document.addEventListener("click", function (e) { if (panel && !panel.contains(e.target)) closePanel(); });
    document.addEventListener("keydown", function (e) { if (e.key === "Escape" && panel) closePanel(); });
  }

  function renderBell() {
    if (!badge) return;
    var unread = items.filter(function (n) { return !n.read_at; }).length;
    badge.hidden = !unread;
    badge.textContent = unread > 9 ? "9+" : String(unread);
    bell.setAttribute("aria-label", unread ? "Notifications, " + unread + " unread" : "Notifications");
    if (panel) fillPanel();
  }

  function openPanel() {
    panel = document.createElement("div");
    panel.className = "onf-panel";
    panel.setAttribute("role", "dialog");
    panel.setAttribute("aria-label", "Notifications");
    document.body.appendChild(panel);
    fillPanel();
    var r = bell.getBoundingClientRect();
    var w = panel.offsetWidth, h = panel.offsetHeight;
    var left = r.right + 10 + w <= innerWidth ? r.right + 10 : Math.max(12, r.right - w);
    var top = Math.min(Math.max(12, r.bottom - h), innerHeight - h - 12);
    if (bell.classList.contains("floating")) { left = innerWidth - w - 18; top = r.top - h - 10; }
    panel.style.left = left + "px"; panel.style.top = Math.max(12, top) + "px";
  }
  function closePanel() { if (panel) { panel.remove(); panel = null; } }

  function fillPanel() {
    var unread = items.some(function (n) { return !n.read_at; });
    panel.innerHTML = "<header><span>Questions about your homes</span>" +
      (unread ? '<button type="button" data-all>Mark all read</button>' : "") + "</header>" +
      '<div class="onf-list">' + (items.length ? items.map(function (n, i) {
        var a = n.asker || {};
        return '<button type="button" class="onf-row' + (n.read_at ? "" : " unread") + '" data-i="' + i + '"><b>' +
          esc(a.name || "Someone") + "</b> (" + esc(a.role_label || "") + ") \u00b7 " + esc(n.property_title || "") +
          '<span class="sub">' + esc(n.question || SOURCE_TEXT[n.source] || "") + " \u00b7 " + esc(ago(n.updated_at || n.created_at)) + "</span></button>";
      }).join("") : '<div class="onf-empty">No questions yet. When a tenant or new investor asks about one of your homes, it shows up here.</div>') + "</div>";
    var all = panel.querySelector("[data-all]");
    if (all) all.addEventListener("click", function (e) {
      e.stopPropagation();
      items.forEach(function (n) { n.read_at = n.read_at || new Date().toISOString(); });
      renderBell();
      api("POST", "/me/notifications/read-all").catch(function () {});
    });
    panel.querySelectorAll("[data-i]").forEach(function (b) {
      b.addEventListener("click", function (e) {
        e.stopPropagation();
        var n = items[+b.dataset.i];
        closePanel();
        popup(n);
      });
    });
  }

  // ---------- polling ----------
  function check() {
    if (checking || document.hidden) return;
    checking = true;
    api("GET", "/me/notifications")
      .then(function (data) {
        items = data.notifications || [];
        renderBell();
        items.filter(function (n) { return !n.read_at && !wasPopped(n); })
          .slice(0, MAX_POPUPS).reverse().forEach(popup);
      })
      .catch(function () {})
      .then(function () { checking = false; });
  }

  function start() {
    api("GET", "/auth/me").then(function (me) {
      if (!me || OWNER_ROLES.indexOf(me.role) < 0) return;   // owners (investor accounts) only
      accountId = me.id || me.session_id || "me";
      injectStyles();
      makeBell();
      check();
      setInterval(check, POLL_MS);
      document.addEventListener("visibilitychange", function () { if (!document.hidden) check(); });
    }).catch(function () {});
  }

  window.StaybotOwnerNotifications = {
    check: check,
    preview: function () {
      injectStyles(); if (!accountId) accountId = "preview"; makeBell();
      var n = { id: "preview-" + Date.now(), property_title: "3 bed house, Garner", asker_role: "tenant", source: "chat",
        question: "Is the first one pet friendly? I have a small dog.", ask_count: 1, updated_at: new Date().toISOString(),
        asker: { name: "Priya Sharma", role_label: "Tenant", email: "priya@example.com", phone: "+1 919 555 0111",
          facts: [{ label: "Employment", value: "employed" }, { label: "Monthly income", value: "6200" },
                  { label: "Move-in", value: "2026-11-01" }, { label: "Pets", value: "Yes" }] } };
      items.unshift(n); renderBell(); popup(n);
    }
  };

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start);
  else start();
})();
