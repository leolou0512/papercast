// papercast-group: approve a CLI device (GET /cli?code=ABCD-EFGH). The terminal ran
// `papercast login`, which printed this page's link; whoever is signed in on this browser
// approves or denies it, and the terminal then collects its token by itself.
"use strict";
(function () {
  const $ = (id) => document.getElementById(id);

  async function api(method, path, body) {
    const opt = { method, headers: {}, credentials: "same-origin" };
    if (method !== "GET") opt.headers["X-PCG"] = "1";
    if (body !== undefined) { opt.headers["Content-Type"] = "application/json"; opt.body = JSON.stringify(body); }
    let r;
    try { r = await fetch(path, opt); } catch (e) {
      const err = new Error("The hub did not answer. Check the connection and reload the page.");
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

  function normCode(s) {
    const c = String(s || "").replace(/[^A-Za-z0-9]/g, "").toUpperCase();
    return c.length === 8 ? c.slice(0, 4) + "-" + c.slice(4) : "";
  }

  function askForCode() {
    $("title").textContent = "papercast login";
    say("Type the code from your terminal.", "");
    $("enter").hidden = false;
    $("code-in").focus();
  }

  $("enter").addEventListener("submit", (e) => {
    e.preventDefault();
    const c = normCode($("code-in").value);
    if (!c) { say("A code is eight letters and digits, like ABCD-EFGH.", "warn"); return; }
    location.search = "?code=" + encodeURIComponent(c);
  });

  async function decide(code, approve) {
    $("yes").disabled = true; $("no").disabled = true;
    try {
      await api("POST", "/api/cli/login/approve", { code, approve });
      $("ask").hidden = true;
      if (approve) {
        $("title").textContent = "Approved";
        say("Go back to your terminal: papercast is signed in there. You can close this page.", "ok");
      } else {
        $("title").textContent = "Denied";
        say("Nothing was given to that device. You can close this page.", "");
      }
    } catch (err) {
      $("yes").disabled = false; $("no").disabled = false;
      if (err.code === "expired") { $("ask").hidden = true; say("This code has expired. Run papercast login again.", "warn"); }
      else if (err.code === "already_decided") { $("ask").hidden = true; say(err.message + ".", "warn"); }
      else say(err.message, "danger");
    }
  }

  async function start() {
    const code = normCode(new URLSearchParams(location.search).get("code"));
    // With passwords, a browser that is not signed in (or still has the first password) goes
    // through the sign-in page and comes back here, code and all.
    let st = null;
    try { st = await api("GET", "/api/auth/state"); } catch (err) { /* an older hub: /api/me decides */ }
    if (st && st.mode === "password" && (!st.signed_in || st.must_change)) {
      const back = encodeURIComponent(location.pathname + location.search);
      location.replace((st.signed_in ? "/set-password?next=" : "/signin?next=") + back);
      return;
    }
    let me;
    try {
      me = await api("GET", "/api/me");
    } catch (err) {
      if (err.status === 401) {
        $("title").textContent = "Sign in first";
        say("This browser is not signed in to papercast. Open your invite or sign-in link here, then open this page again.", "warn");
      } else if (err.status === 403) {
        $("title").textContent = "Not allowed";
        say(err.message, "danger");
      } else say(err.message, "danger");
      return;
    }
    const who = $("who");
    who.textContent = `Signed in as ${me.name}` + (me.email && !/@local\.invalid$/.test(me.email) ? ` (${me.email})` : "");
    who.hidden = false;
    if (!code) { askForCode(); return; }

    let info;
    try {
      info = await api("GET", "/api/cli/login/info?code=" + encodeURIComponent(code));
    } catch (err) {
      if (err.status === 404) { say("No login has that code. Check it, or run papercast login again.", "warn"); $("enter").hidden = false; }
      else say(err.message, "danger");
      return;
    }
    if (info.state === "pending") {
      $("title").textContent = `Approve papercast on ${info.device}?`;
      $("code").textContent = info.code;
      say("", "");
      $("ask").hidden = false;
      $("yes").addEventListener("click", () => decide(info.code, true));
      $("no").addEventListener("click", () => decide(info.code, false));
    } else if (info.state === "expired") {
      say("This code has expired. Run papercast login again.", "warn");
    } else if (info.state === "denied") {
      $("title").textContent = "Denied";
      say("This login was denied.", "");
    } else {
      $("title").textContent = "Approved";
      say("This login was already approved.", "ok");
    }
  }

  start();
})();
