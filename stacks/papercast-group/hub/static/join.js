// papercast-group: open a one-use link (GET /join/<token>). An invite asks for a name (and,
// optionally, an email) and makes a new person; a sign-in link signs in someone who exists.
// Either way the hub sets this browser's session cookie and the page goes to the library.
"use strict";
(function () {
  const $ = (id) => document.getElementById(id);
  const token = decodeURIComponent(location.pathname.replace(/^\/join\//, ""));
  const ROLE = { viewer: "listen and edit graphs", contributor: "listen, edit graphs and add papers from the command line",
                 admin: "run the group's papercast" };

  async function api(body) {
    let r;
    try {
      r = await fetch("/api/join", { method: "POST", credentials: "same-origin",
                                     headers: { "Content-Type": "application/json", "X-PCG": "1" },
                                     body: JSON.stringify(body) });
    } catch (e) {
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

  function dead(err) {
    $("join").hidden = true; $("signin").hidden = true;
    if (err.status === 410) { $("title").textContent = "This link has been used"; say(err.message + ".", "warn"); }
    else say(err.message, "danger");
  }

  async function go(body, btn) {
    btn.disabled = true;
    try {
      await api(Object.assign({ token }, body));
      say("Signed in. Opening papercast…", "ok");
      location.replace("/");
    } catch (err) {
      btn.disabled = false;
      if (err.status === 410 || err.status === 403) dead(err);
      else say(err.message, "danger");
    }
  }

  $("join").addEventListener("submit", (e) => {
    e.preventDefault();
    const name = $("name").value.trim();
    const email = $("email").value.trim();
    if (!name) { say("Type your name first.", "warn"); $("name").focus(); return; }
    go(email ? { name, email } : { name }, $("join-go"));
  });
  $("signin-go").addEventListener("click", () => go({}, $("signin-go")));

  async function start() {
    let info;
    try { info = await api({ token, peek: true }); } catch (err) { dead(err); return; }
    if (info.kind === "signin") {
      $("title").textContent = `Sign in as ${info.name}`;
      say("This link works once. It signs this browser in.", "");
      $("signin").hidden = false;
    } else {
      $("title").textContent = "Join papercast";
      say(`You are invited to ${ROLE[info.role] || "listen"}. This link works once.`, "");
      $("join").hidden = false;
      $("name").focus();
    }
  }

  start();
})();
