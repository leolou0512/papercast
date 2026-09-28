// papercast-group: choose a password (GET /set-password, PCG_AUTH=password). Two ways here:
// a one-use link (#t=<secret>, from the reset email, an admin or the server's bootstrap), or a
// session signed in with the first password (the username), which can do nothing else until a
// new one is chosen. Either way the hub signs this browser in and every other session ends.
"use strict";
(function () {
  const $ = (id) => document.getElementById(id);

  async function api(method, path, body) {
    const opt = { method, headers: {}, credentials: "same-origin" };
    if (method !== "GET") opt.headers["X-PCG"] = "1";
    if (body !== undefined) { opt.headers["Content-Type"] = "application/json"; opt.body = JSON.stringify(body); }
    let r;
    try { r = await fetch(path, opt); } catch (e) {
      const err = new Error("The hub did not answer. Check the connection and try again.");
      err.status = 0; throw err;
    }
    let j = null;
    try { j = await r.json(); } catch (e) { /* not JSON */ }
    if (!r.ok) {
      const err = new Error((j && j.message) || `HTTP ${r.status}`);
      err.status = r.status; err.code = j && j.error; throw err;
    }
    return j;
  }

  function say(text, kind) {
    const m = $("msg");
    m.textContent = text;
    m.className = "msg" + (kind ? " " + kind : "");
    m.hidden = !text;
  }

  function nextPath() {
    const n = new URLSearchParams(location.search).get("next") || "/";
    return /^\/(?![/\\])[^\s]*$/.test(n) && !/^\/(signin|set-password)\b/.test(n) ? n : "/";
  }
  const NEXT = nextPath();

  // The link's secret: read once, then out of the address bar (and so out of the history).
  const m = /^#t=([A-Za-z0-9_-]{20,100})$/.exec(location.hash);
  const token = m ? m[1] : null;
  if (token) history.replaceState(null, "", location.pathname + location.search);

  function dead(text) {
    $("setpw").hidden = true;
    $("title").textContent = "This link cannot be used";
    say(text, "warn");
    $("dead").hidden = false;
  }

  function form(title, sub, username) {
    $("title").textContent = title;
    say(sub, "");
    $("username").value = username || "";
    $("setpw").hidden = false;
    $("password").focus();
  }

  $("show").addEventListener("click", () => {
    const p = $("password"), on = p.type === "password";
    p.type = on ? "text" : "password";
    $("show").textContent = on ? "Hide" : "Show";
    $("show").setAttribute("aria-pressed", String(on));
    p.focus();
  });

  $("out").addEventListener("click", async () => {
    try { await api("POST", "/api/auth/signout"); } catch (e) { /* the cookie goes with the page anyway */ }
    location.replace("/signin");
  });

  $("setpw").addEventListener("submit", async (e) => {
    e.preventDefault();
    const password = $("password").value;
    if (password.length < 10) { say("Use at least 10 characters.", "warn"); $("password").focus(); return; }
    $("save").disabled = true;
    try {
      if (token) await api("POST", "/api/auth/link", { token, password });
      else await api("POST", "/api/auth/password", { password });
      say("Saved. Opening papercast…", "ok");
      location.replace(NEXT);
    } catch (err) {
      $("save").disabled = false;
      if (err.status === 410) dead(err.message);
      else if (err.status === 401) location.replace("/signin?next=" + encodeURIComponent(NEXT));
      else { say(err.message, err.status === 400 ? "warn" : "danger"); $("password").focus(); }
    }
  });

  async function start() {
    if (token) {
      let info;
      try { info = await api("POST", "/api/auth/link", { token, peek: true }); } catch (err) {
        if (err.status === 410) dead(err.message); else say(err.message, "danger");
        return;
      }
      form("Set a new password", `For ${info.username} (${info.email}). This link works once.`, info.username);
      return;
    }
    let st;
    try { st = await api("GET", "/api/auth/state"); } catch (err) { say(err.message, "danger"); return; }
    if (!st.signed_in) { location.replace("/signin" + (NEXT !== "/" ? "?next=" + encodeURIComponent(NEXT) : "")); return; }
    if (!st.must_change) { location.replace(NEXT); return; }
    $("out").hidden = false;
    form("Choose your own password", "You signed in with the first password. Choose your own to go on.",
      st.user && st.user.username);
  }

  start();
})();
