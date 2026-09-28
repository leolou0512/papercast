// papercast-group: the sign-in page (GET /signin, PCG_AUTH=password). Username or email and a
// password; "Forgot your password?" asks the hub to email a one-use link, and the hub answers the
// same whoever is on the list. ?next= (a path on this hub) is where a successful sign-in goes:
// the CLI's approve page sends people here and gets them back.
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

  // Only a path on this hub, never another site (an open redirect would make this page a lure).
  function nextPath() {
    const n = new URLSearchParams(location.search).get("next") || "/";
    const ok = /^\/(?![/\\])[^\s]*$/.test(n) && !/^\/(signin|set-password)\b/.test(n);
    const base = ok ? n : "/";
    return base === "/" ? "/" + location.hash : base;       // /#p=... came here as /signin#p=...
  }
  const NEXT = nextPath();
  const go = (must) => location.replace(must ? "/set-password?next=" + encodeURIComponent(NEXT) : NEXT);

  let st = null;
  function show(which) {
    $("signin").hidden = which !== "signin";
    $("forgot").hidden = which !== "forgot";
    $("asked").hidden = which !== "asked";
    $("title").textContent = which === "signin" ? "Sign in to papercast" : "Forgot your password";
    if (which === "signin") { say("", ""); ($("login").value ? $("password") : $("login")).focus(); }
    if (which === "forgot") {
      say("", "");
      $("forgot-sub").textContent = st && st.email
        ? "Type the email address the group has for you. If it is on the list, the hub emails you a link to set a new password."
        : "This hub cannot send email yet. Type your email address anyway: the admins will see that you asked, and one of them can reset your password.";
      $("send").textContent = st && st.email ? "Send the link" : "Ask for a reset";
      if (!$("email").value && /@/.test($("login").value)) $("email").value = $("login").value.trim();
      $("email").focus();
    }
  }

  $("show").addEventListener("click", () => {
    const p = $("password"), on = p.type === "password";
    p.type = on ? "text" : "password";
    $("show").textContent = on ? "Hide" : "Show";
    $("show").setAttribute("aria-pressed", String(on));
    p.focus();
  });
  $("forgot-open").addEventListener("click", () => show("forgot"));
  $("forgot-back").addEventListener("click", () => show("signin"));
  $("asked-back").addEventListener("click", () => show("signin"));

  $("signin").addEventListener("submit", async (e) => {
    e.preventDefault();
    const login = $("login").value.trim(), password = $("password").value;
    if (!login) { say("Type your username or email.", "warn"); $("login").focus(); return; }
    if (!password) { say("Type your password.", "warn"); $("password").focus(); return; }
    $("go").disabled = true;
    try {
      const r = await api("POST", "/api/auth/login", { login, password });
      say("Signed in.", "ok");
      go(r.must_change);
    } catch (err) {
      $("go").disabled = false;
      say(err.message, err.status === 429 ? "warn" : "danger");
      if (err.status === 401) { $("password").value = ""; $("password").focus(); }
    }
  });

  $("forgot").addEventListener("submit", async (e) => {
    e.preventDefault();
    const email = $("email").value.trim();
    if (!/^[^@\s]+@[^@\s]+$/.test(email)) { say("Type your email address.", "warn"); $("email").focus(); return; }
    $("send").disabled = true;
    try {
      const r = await api("POST", "/api/auth/forgot", { email });
      show("asked");
      $("title").textContent = st && st.email ? "Check your email" : "Ask an admin";
      say(r.message, "ok");
    } catch (err) {
      say(err.message, err.status === 429 ? "warn" : "danger");
    } finally {
      $("send").disabled = false;
    }
  });

  async function start() {
    try { st = await api("GET", "/api/auth/state"); } catch (err) { say(err.message, "danger"); return; }
    if (st.signed_in) { go(st.must_change); return; }
    if (st.mode !== "password") {
      $("title").textContent = "Not signed in";
      say(st.mode === "local" ? "This browser is not signed in to papercast. Open your invite or sign-in link in this browser."
        : st.mode === "cf-access" ? "Sign in through Cloudflare Access: reload this page." : "This browser is not signed in.", "warn");
      return;
    }
    const u = new URLSearchParams(location.search).get("u");       // the welcome email's Join button
    if (u && !$("login").value) $("login").value = u.trim().slice(0, 120);
    show("signin");
  }

  start();
})();
