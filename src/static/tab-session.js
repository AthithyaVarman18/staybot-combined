/*
 * Per-tab login for Staybot (loaded first in <head> of every account page).
 *
 * Problem this fixes: the login cookie is shared by every tab and window of
 * the browser. Logging in as a tenant in one window and then as a New/Existing
 * Property Investor in another used to silently turn the tenant window into
 * the investor - its Portfolio, chat and tabs all followed the newest cookie.
 *
 * How: the server hands each login its own token (X-Staybot-Session-Issued
 * response header). This script keeps it in sessionStorage - which is private
 * to this tab - and adds it as X-Staybot-Session to every same-origin fetch,
 * so API calls always act as the account THIS tab logged in as. When the tab
 * gets focus it calls /auth/activate so the shared cookie (used by full page
 * loads and plain links) points back at this tab's account too.
 */
(function () {
  if (window.__staybotTabSession) return;
  window.__staybotTabSession = true;

  var KEY = "staybot.tabSession";
  var LOGIN_KEY = "staybot.tabSession.loginPage";
  var HEADER = "X-Staybot-Session";
  var ISSUED = "X-Staybot-Session-Issued";
  var CLEARED = "X-Staybot-Session-Cleared";

  function read(k) { try { return sessionStorage.getItem(k) || ""; } catch (e) { return ""; } }
  function write(k, v) { try { v ? sessionStorage.setItem(k, v) : sessionStorage.removeItem(k); } catch (e) { /* storage blocked */ } }

  var nativeFetch = window.fetch.bind(window);

  function urlOf(input) {
    if (typeof input === "string") return input;
    if (input && typeof input.url === "string") return input.url; // Request
    return String(input);                                            // URL object
  }
  function sameOrigin(url) {
    try { return new URL(url, location.href).origin === location.origin; } catch (e) { return false; }
  }

  function remember(res, url) {
    try {
      var issued = res.headers.get(ISSUED);
      if (issued) {
        write(KEY, issued);
        // /auth/me pins a tab that had no token to the account it opened as;
        // keep the login page an actual login already chose.
        if (url.indexOf("/auth/me") === -1 || !read(LOGIN_KEY)) {
          write(LOGIN_KEY, url.indexOf("/auth/admin/login") !== -1 ? "/admin/login" : "/login");
        }
      }
      if (res.headers.get(CLEARED)) { write(KEY, ""); }
    } catch (e) { /* ignore */ }
    return res;
  }

  window.fetch = function (input, init) {
    var url = urlOf(input);
    if (!sameOrigin(url)) return nativeFetch(input, init);

    var token = read(KEY);
    if (token) {
      init = Object.assign({}, init || {});
      var headers = new Headers(init.headers || (input instanceof Request ? input.headers : undefined));
      if (!headers.has(HEADER)) headers.set(HEADER, token);
      init.headers = headers;
    }
    return nativeFetch(input, init).then(function (res) { return remember(res, url); });
  };

  // One logout for every page (/ui sidebar, screening, investor education,
  // /dashboard, /portal). Logs out THIS tab's account only (accounts.py
  // logout() leaves another window's login alone), forgets the tab's token,
  // and goes to the right login page. `to` overrides where it lands;
  // `to === false` logs out without navigating (the caller redirects).
  window.staybotLogout = function (to) {
    var dest = to === false ? null : (to || read(LOGIN_KEY) || "/login");
    return window.fetch("/auth/logout", { method: "POST" })
      .catch(function () { /* still leave - the session is dropped below */ })
      .then(function () {
        write(KEY, "");
        write(LOGIN_KEY, "");
        if (dest) location.href = dest;
      });
  };

  // Point the shared cookie at this tab's account whenever the tab is in use,
  // so page loads / links opened from it act as this tab's account as well.
  // Only the top window does this (the /ui iframes share its sessionStorage).
  if (window.self !== window.top) return;

  var pending = false;
  function activate() {
    var token = read(KEY);
    if (!token || pending) return;
    pending = true;
    var headers = {};
    headers[HEADER] = token;
    nativeFetch("/auth/activate", { method: "POST", headers: headers, credentials: "same-origin", keepalive: true })
      .then(function (res) {
        if (res.status === 401) {
          // This tab's own session has ended. Don't fall back to whatever
          // account another window is using - send this tab to log in again.
          var loginPage = read(LOGIN_KEY) || "/login";
          write(KEY, "");
          if (location.pathname !== loginPage) location.href = loginPage;
        }
      })
      .catch(function () { /* offline etc. - try again on next focus */ })
      .then(function () { pending = false; });
  }

  document.addEventListener("visibilitychange", function () {
    if (document.visibilityState === "visible") activate();
  });
  window.addEventListener("focus", activate);
  window.addEventListener("pageshow", activate);
  activate();
})();
