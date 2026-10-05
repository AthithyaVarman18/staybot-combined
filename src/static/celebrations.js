/*
 * Congratulation pop-ups for Staybot (/ui).
 *
 *   Tenant   - "Congratulations! You've been assigned <home>"   (rental approved)
 *   Investor - "Congratulations on your new home!"               (purchase agreed / completed)
 *   Owner    - "Congratulations! Your house <home> has been sold"
 *
 * Reads GET /me/celebrations (src/services/celebrations.py). Each pop-up shows
 * once per account (remembered in localStorage), closes by itself after 5
 * seconds, and has a Cancel button to close it straight away. Checked on page
 * load, every 30 seconds, when the tab regains focus, and right after any
 * action the user takes on /me/... (approve, accept offer, ...), so the owner
 * who clicks "accept" sees their pop-up immediately.
 */
(function () {
  if (window.__staybotCelebrations) return;
  window.__staybotCelebrations = true;

  var SHOW_MS = 5000;
  var POLL_MS = 30000;
  var SEEN_KEY = "staybot.celebrations.seen";
  var ICONS = { tenant_assigned: "\uD83D\uDD11", home_bought: "\uD83C\uDFE1", home_sold: "\uD83C\uDF89" };

  var queue = [];
  var showing = false;
  var checking = false;
  var accountKey = "";

  // ---------- seen ids (per account) ----------
  function seenAll() {
    try { return JSON.parse(localStorage.getItem(SEEN_KEY) || "{}") || {}; } catch (e) { return {}; }
  }
  function isSeen(id) {
    var all = seenAll();
    return !!(all[accountKey] && all[accountKey][id]);
  }
  function markSeen(id) {
    var all = seenAll();
    all[accountKey] = all[accountKey] || {};
    all[accountKey][id] = Date.now();
    try { localStorage.setItem(SEEN_KEY, JSON.stringify(all)); } catch (e) { /* storage blocked */ }
  }

  // ---------- styles ----------
  function injectStyles() {
    if (document.getElementById("cel-styles")) return;
    var css =
      ".cel-overlay{position:fixed;inset:0;z-index:100000;display:flex;align-items:center;justify-content:center;" +
      "padding:20px;background:rgba(18,40,63,.55);backdrop-filter:blur(3px);opacity:0;transition:opacity .25s ease}" +
      ".cel-overlay.on{opacity:1}" +
      ".cel-canvas{position:fixed;inset:0;width:100%;height:100%;pointer-events:none}" +
      ".cel-card{position:relative;width:100%;max-width:420px;background:#fff;border-radius:20px;overflow:hidden;" +
      "box-shadow:0 24px 60px rgba(0,0,0,.28);text-align:center;transform:translateY(16px) scale(.96);" +
      "transition:transform .3s cubic-bezier(.2,.9,.3,1.2);font-family:inherit;color:#222}" +
      ".cel-overlay.on .cel-card{transform:none}" +
      ".cel-top{background:linear-gradient(135deg,#1B3A5C 0%,#12283F 100%);padding:28px 24px 22px;color:#fff}" +
      ".cel-icon{font-size:52px;line-height:1;display:inline-block;animation:cel-pop .6s ease}" +
      "@keyframes cel-pop{0%{transform:scale(.3)}70%{transform:scale(1.15)}100%{transform:scale(1)}}" +
      ".cel-title{margin:12px 0 0;font-size:22px;font-weight:700;letter-spacing:-.01em}" +
      ".cel-body{padding:20px 24px 22px}" +
      ".cel-msg{margin:0 0 18px;font-size:15.5px;line-height:1.5;color:#3b3b3b}" +
      ".cel-cancel{appearance:none;border:1px solid #d5d9de;background:#fff;color:#222;font:inherit;font-weight:600;" +
      "font-size:14.5px;padding:10px 28px;border-radius:999px;cursor:pointer}" +
      ".cel-cancel:hover{background:#f4f6f8}" +
      ".cel-cancel:focus-visible{outline:2px solid #B8873C;outline-offset:2px}" +
      ".cel-bar{height:4px;background:#EAF0F6}" +
      ".cel-bar i{display:block;height:100%;width:100%;background:#B8873C;transform-origin:left}" +
      ".cel-hint{margin-top:10px;font-size:12px;color:#888}" +
      "@media (prefers-reduced-motion:reduce){.cel-overlay,.cel-card{transition:none}.cel-icon{animation:none}}";
    var s = document.createElement("style");
    s.id = "cel-styles";
    s.textContent = css;
    document.head.appendChild(s);
  }

  // ---------- confetti ----------
  function confetti(canvas) {
    if (window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches) return function () {};
    var ctx = canvas.getContext("2d");
    var dpr = window.devicePixelRatio || 1;
    var w = canvas.width = innerWidth * dpr;
    var h = canvas.height = innerHeight * dpr;
    var colors = ["#B8873C", "#1B3A5C", "#E9C46A", "#2A9D8F", "#E76F51", "#ffffff"];
    var bits = [];
    for (var i = 0; i < 140; i++) {
      bits.push({
        x: Math.random() * w, y: -Math.random() * h * 0.6,
        vx: (Math.random() - 0.5) * 3 * dpr, vy: (2 + Math.random() * 3.5) * dpr,
        r: (4 + Math.random() * 5) * dpr, a: Math.random() * Math.PI, va: (Math.random() - 0.5) * 0.25,
        c: colors[i % colors.length]
      });
    }
    var raf, stopped = false;
    (function tick() {
      if (stopped) return;
      ctx.clearRect(0, 0, w, h);
      for (var j = 0; j < bits.length; j++) {
        var b = bits[j];
        b.x += b.vx; b.y += b.vy; b.a += b.va; b.vy += 0.03 * dpr;
        ctx.save(); ctx.translate(b.x, b.y); ctx.rotate(b.a);
        ctx.fillStyle = b.c; ctx.fillRect(-b.r / 2, -b.r / 4, b.r, b.r / 2);
        ctx.restore();
      }
      raf = requestAnimationFrame(tick);
    })();
    return function () { stopped = true; cancelAnimationFrame(raf); };
  }

  // ---------- the pop-up ----------
  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }

  function show(ev) {
    showing = true;
    markSeen(ev.id); // seen as soon as it's shown - a reload won't repeat it
    injectStyles();

    var lastFocus = document.activeElement;
    var overlay = el("div", "cel-overlay");
    overlay.setAttribute("role", "dialog");
    overlay.setAttribute("aria-modal", "true");
    overlay.setAttribute("aria-labelledby", "cel-title");
    overlay.setAttribute("aria-describedby", "cel-msg");

    var canvas = el("canvas", "cel-canvas");
    var card = el("div", "cel-card");
    var top = el("div", "cel-top");
    var icon = el("div", "cel-icon", ICONS[ev.kind] || "\uD83C\uDF89");
    icon.setAttribute("aria-hidden", "true");
    var title = el("h2", "cel-title", ev.title || "Congratulations!");
    title.id = "cel-title";
    top.appendChild(icon); top.appendChild(title);

    var bar = el("div", "cel-bar"); var fill = el("i"); bar.appendChild(fill);

    var body = el("div", "cel-body");
    var msg = el("p", "cel-msg", ev.message || "");
    msg.id = "cel-msg";
    var cancel = el("button", "cel-cancel", "Cancel");
    cancel.type = "button";
    var hint = el("div", "cel-hint", "Closes in 5 seconds");
    body.appendChild(msg); body.appendChild(cancel); body.appendChild(hint);

    card.appendChild(top); card.appendChild(bar); card.appendChild(body);
    overlay.appendChild(canvas); overlay.appendChild(card);
    document.body.appendChild(overlay);

    var stopConfetti = confetti(canvas);
    requestAnimationFrame(function () { overlay.classList.add("on"); });
    cancel.focus({ preventScroll: true });

    // 5-second countdown bar + text
    fill.style.transition = "transform " + SHOW_MS + "ms linear";
    requestAnimationFrame(function () { requestAnimationFrame(function () { fill.style.transform = "scaleX(0)"; }); });
    var started = Date.now();
    var countdown = setInterval(function () {
      var left = Math.max(0, Math.ceil((SHOW_MS - (Date.now() - started)) / 1000));
      hint.textContent = "Closes in " + left + " second" + (left === 1 ? "" : "s");
    }, 250);

    var closed = false;
    function close() {
      if (closed) return;
      closed = true;
      clearTimeout(timer); clearInterval(countdown);
      document.removeEventListener("keydown", onKey, true);
      stopConfetti();
      overlay.classList.remove("on");
      setTimeout(function () {
        overlay.remove();
        if (lastFocus && lastFocus.focus) { try { lastFocus.focus({ preventScroll: true }); } catch (e) {} }
        showing = false;
        next();
      }, 260);
    }
    function onKey(e) {
      if (e.key === "Escape") { e.preventDefault(); close(); }
      else if (e.key === "Tab") { e.preventDefault(); cancel.focus(); } // only one control - keep focus on it
    }
    var timer = setTimeout(close, SHOW_MS);
    cancel.addEventListener("click", close);
    overlay.addEventListener("click", function (e) { if (e.target === overlay) close(); });
    document.addEventListener("keydown", onKey, true);
  }

  function next() {
    if (showing) return;
    while (queue.length && isSeen(queue[0].id)) queue.shift();
    if (queue.length) show(queue.shift());
  }

  // ---------- fetching ----------
  function check() {
    if (checking || document.hidden) return;
    checking = true;
    fetch("/me/celebrations", { credentials: "same-origin", headers: { Accept: "application/json" } })
      .then(function (r) { return r.ok ? r.json() : { events: [] }; })
      .then(function (data) {
        accountKey = data.account_key || accountKey || "me";
        (data.events || []).forEach(function (ev) {
          if (!isSeen(ev.id) && !queue.some(function (q) { return q.id === ev.id; })) queue.push(ev);
        });
        next();
      })
      .catch(function () { /* offline / logged out - try again later */ })
      .then(function () { checking = false; });
  }

  // Check again shortly after the user does something on their own routes
  // (approve a tenant, accept an offer...), so the pop-up follows the click.
  var origFetch = window.fetch;
  window.fetch = function (input, init) {
    var p = origFetch.apply(this, arguments);
    try {
      var url = typeof input === "string" ? input : (input && input.url) || String(input);
      var method = ((init && init.method) || (input && input.method) || "GET").toUpperCase();
      var path = new URL(url, location.href).pathname;
      if (method !== "GET" && (path.indexOf("/me/") === 0 || path.indexOf("/applications/") === 0)) {
        p.then(function () { setTimeout(check, 700); }, function () {});
      }
    } catch (e) { /* never break the page's own fetch */ }
    return p;
  };

  // Expose for testing from the console: StaybotCelebrations.preview("home_sold")
  window.StaybotCelebrations = {
    check: check,
    preview: function (kind) {
      var demo = {
        tenant_assigned: ["Congratulations!", "You've been assigned 12 Oak Street. Welcome to your new home!"],
        home_bought: ["Congratulations on your new home!", "You've bought 12 Oak Street. It's now part of your portfolio."],
        home_sold: ["Congratulations!", "Your house 12 Oak Street has been sold."]
      }[kind || "home_sold"];
      queue.push({ id: "preview:" + Date.now(), kind: kind || "home_sold", title: demo[0], message: demo[1] });
      next();
    }
  };

  function start() {
    check();
    setInterval(check, POLL_MS);
    document.addEventListener("visibilitychange", function () { if (!document.hidden) check(); });
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start);
  else start();
})();
