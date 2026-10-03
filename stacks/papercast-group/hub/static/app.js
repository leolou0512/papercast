// papercast-group: Leo's one page ("Now Playing", stacks/papercast/web/static/app.js) for a
// group. Its home is the graph list and the map (graphs.js, map.js; DESIGN.md, 2026-10-03): a
// paper opens in its graph, its window over the map; the player as Leo's; no chat, no adding
// (that is `papercast add`, on each person's own machine). A paper can have several versions,
// each made by one person with their preferences; the window lists them all. Listened and
// positions are each person's own. Nothing is fetched from anywhere else, and every string
// that came from a paper or a person is inserted as text, never as HTML. The page's CSP (no
// inline script, no inline style) is the second fence behind that.
"use strict";
(() => {
  const $ = (id) => document.getElementById(id);
  const A = () => $("audio");
  const S = {
    cfg: null, me: {}, papers: new Map(), open: null, view: "home",
    graph: null, phoneMap: false, landed: false,      // the graph shown (or "none"); a phone's drawing open
    es: null, lastId: "", esRetry: null, esFails: 0, offTimer: null,
    audioEp: null, audioPaper: null, audioReady: null, afterReady: null, dirty: false,
    barEp: null, barOpen: false, pane: "tr", speed: 1,
    menu: null, lastFocus: null, scrubbing: null,
    build: "", reloadFor: "", typedAt: 0, details: false,
    epPaper: new Map(),       // episode id -> paper id
    setTab: "prefs", set: {},
  };
  const SPEEDS = [1, 1.25, 1.5, 1.75, 2, 0.75];
  const phone = () => window.matchMedia("(max-width: 720px)").matches;
  // The page's zoom (theme.js's size): getBoundingClientRect, clientX and innerWidth are in the
  // screen's px, style, scrollTop and offsetWidth in the page's CSS px, the screen's / zoom.
  const zoom = () => document.documentElement.currentCSSZoom || 1;
  const box = (e) => { const r = e.getBoundingClientRect(), z = zoom();
    return { left: r.left / z, top: r.top / z, right: r.right / z, bottom: r.bottom / z, width: r.width / z, height: r.height / z }; };
  const isAdmin = () => S.me && S.me.role === "admin";
  const store = {
    get(k) { try { return localStorage.getItem(k); } catch (e) { return null; } },
    set(k, v) { try { localStorage.setItem(k, v); } catch (e) { /* private mode */ } },
    del(k) { try { localStorage.removeItem(k); } catch (e) { /* private mode */ } },
  };
  // This tab only, and it survives location.reload(): the search and filters, the build reloaded for.
  const tab = {
    get(k) { try { return sessionStorage.getItem(k); } catch (e) { return null; } },
    set(k, v) { try { sessionStorage.setItem(k, v); } catch (e) { /* private mode */ } },
    del(k) { try { sessionStorage.removeItem(k); } catch (e) { /* private mode */ } },
  };
  // What is one person's on a shared device (positions, which version plays) is kept under
  // their id.
  const mine = (k) => `pcg.${S.me && S.me.id !== undefined ? S.me.id : 0}.${k}`;

  // `html` is only ever given this file's own icon strings.
  function el(tag, attrs, ...kids) {
    const e = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v === null || v === undefined || v === false) continue;
      if (k === "class") e.className = v;
      else if (k === "text") e.textContent = v;
      else if (k === "html") e.innerHTML = v;
      else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
      else e.setAttribute(k, v === true ? "" : v);
    }
    for (const c of kids.flat()) if (c !== null && c !== undefined && c !== false) e.append(c);
    return e;
  }

  // Every request carries X-PCG (the hub's CSRF fence, SPEC section 2).
  async function api(method, path, body, more) {
    const opt = { method, headers: { "X-PCG": "1" }, credentials: "same-origin" };
    if (more && more.keepalive) opt.keepalive = true;
    if (body instanceof Blob) { opt.headers["Content-Type"] = body.type || "application/octet-stream"; opt.body = body; }     // a picture
    else if (body !== undefined) { opt.headers["Content-Type"] = "application/json"; opt.body = JSON.stringify(body); }
    let r;
    try { r = await fetch(path, opt); } catch (e) {
      const err = new Error("The hub did not answer (or the sign-in expired: reload the page).");
      err.code = "network"; throw err;
    }
    let j = null;
    try { j = await r.json(); } catch (e) { /* not JSON */ }
    if (!r.ok) {
      const err = new Error((j && j.message) || `HTTP ${r.status}`);
      err.code = j && j.error; err.status = r.status;
      if (passwordMode() || !S.cfg) signedOut(err);
      throw err;
    }
    return j;
  }
  // With passwords, a session that ended (a new password, "sign out everywhere", removal) sends
  // the page to the sign-in page, which brings it back here.
  const passwordMode = () => !!(S.cfg && S.cfg.auth === "password");
  function signedOut(err) {
    const back = encodeURIComponent(location.pathname + location.search + location.hash);
    if (err.status === 401 && err.code === "login_required") location.replace(`/signin?next=${back}`);
    else if (err.status === 403 && err.code === "must_change_password") location.replace(`/set-password?next=${back}`);
  }

  // ------------------------------------------------------------------ icons (constants)
  const I = {
    play: (s = 20) => `<svg width="${s}" height="${s}" viewBox="0 0 20 20" aria-hidden="true"><path d="M6 3.8v12.4a.8.8 0 0 0 1.2.7l10-6.2a.8.8 0 0 0 0-1.4l-10-6.2A.8.8 0 0 0 6 3.8Z" fill="currentColor"/></svg>`,
    pause: (s = 20) => `<svg width="${s}" height="${s}" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true"><rect x="4.5" y="3.5" width="4" height="13" rx="1"/><rect x="11.5" y="3.5" width="4" height="13" rx="1"/></svg>`,
    skip: (n, fwd) => `<svg width="32" height="32" viewBox="0 0 32 32" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
      <g ${fwd ? 'transform="translate(32 0) scale(-1 1)"' : ""}><path d="M16 5a11 11 0 1 1-10.4 7.4"/><path d="M19 2 15.6 5 19 8"/></g>
      <text x="16" y="20" text-anchor="middle" font-size="9.5" font-weight="700" fill="currentColor" stroke="none" font-family="-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Arial, sans-serif">${n}</text></svg>`,
    more: `<svg width="20" height="20" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true"><circle cx="4.5" cy="10" r="1.6"/><circle cx="10" cy="10" r="1.6"/><circle cx="15.5" cy="10" r="1.6"/></svg>`,
    back: `<svg width="22" height="22" viewBox="0 0 22 22" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M14 4 7 11l7 7"/></svg>`,
    doc: `<svg width="18" height="18" viewBox="0 0 18 18" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round" aria-hidden="true"><path d="M4 1.8h6.5L14 5.3v10.9H4z"/><path d="M6.5 8.5h5M6.5 11h5M6.5 13.5h3" stroke-linecap="round"/></svg>`,
    check: `<svg width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m2.5 7.5 3 3 6-7"/></svg>`,
    close: `<svg width="20" height="20" viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" aria-hidden="true"><path d="m5 5 10 10M15 5 5 15"/></svg>`,
  };

  // ------------------------------------------------------------------ formatting
  const pad = (n) => String(n).padStart(2, "0");
  function hms(s) {
    s = Math.max(0, Math.floor(s || 0));
    const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), x = s % 60;
    return h ? `${h}:${pad(m)}:${pad(x)}` : `${m}:${pad(x)}`;
  }
  const DAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
  const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  function when(iso) {
    if (!iso) return "";
    const d = new Date(iso);
    if (isNaN(d)) return "";
    const now = new Date(), hm = `${pad(d.getHours())}:${pad(d.getMinutes())}`;
    if (d.toDateString() === now.toDateString()) return hm;
    if (now - d < 6 * 86400000 && now - d > 0) return `${DAYS[d.getDay()]} ${hm}`;
    return `${d.getDate()} ${MONTHS[d.getMonth()]}${d.getFullYear() !== now.getFullYear() ? ` ${d.getFullYear()}` : ""}`;
  }
  function day(iso) {
    const d = new Date(iso || "");
    return isNaN(d) ? "" : `${d.getDate()} ${MONTHS[d.getMonth()]} ${d.getFullYear()}`;
  }
  // The paper's title, or "" when it is not known: then a short grey bar, never an id.
  const displayTitle = (p) => (typeof p.title === "string" ? p.title : "");
  function titleInto(node, p) {
    const t = displayTitle(p);
    if (t) node.textContent = t;
    else node.replaceChildren(el("span", { class: "tbar", "aria-label": "Title not known" }));
    return t;
  }
  function shortAuthors(p) {
    const a = p.authors || [];
    const last = (n) => String(n).trim().split(/\s+/).slice(-1)[0];
    if (!a.length) return "";
    if (a.length === 1) return last(a[0]);
    if (a.length === 2) return `${last(a[0])} and ${last(a[1])}`;
    return `${last(a[0])} et al.`;
  }
  const short = (t, n = 26) => (t.length > n ? `${t.slice(0, n - 2).trimEnd()}…` : t);
  const makerName = (e) => (e && e.made_by && e.made_by.name) || "someone";
  // "by Alice · derivations": who made a version, and the preferences it was made with.
  const whoLine = (e) => [`by ${makerName(e)}`, e.prefs_summary].filter(Boolean).join(" · ");
  function speakPct(e) {
    let x = Number(e.progress);
    if (!isFinite(x)) return 0;
    if (x <= 1) x *= 100;       // the voice worker's progress is a fraction
    return Math.max(0, Math.min(100, Math.round(x)));
  }

  // ------------------------------------------------------------------ versions
  function indexEps(p) { for (const e of p.episodes || []) S.epPaper.set(e.id, p.id); }
  // A paper the list does not show (a search, or queued from elsewhere) is still known from the
  // Up next answer or the map.
  const paperOf = (pid) => S.papers.get(pid) || Q.papers.get(pid) || null;
  function epById(eid) {
    const p = paperOf(S.epPaper.get(eid));
    return (p && (p.episodes || []).find((e) => e.id === eid)) || null;
  }
  const anyAudio = (p) => (p.episodes || []).some((e) => e.has_audio);
  // The version that plays: the one picked in the window on this device; else the one in the
  // player; else one part-way through (where this person left it); else their own; else the
  // first made. Versions with audio come before those still being made.
  function chosen(p) {
    const eps = (p && p.episodes) || [];
    if (!eps.length) return null;
    const pk = store.get(mine(`pick.${p.id}`));
    let c = pk && eps.find((e) => e.id === pk);
    if (c) return c;
    if (S.audioPaper === p.id && (c = eps.find((e) => e.id === S.audioEp))) return c;
    const ready = eps.filter((e) => e.has_audio);
    let best = null, bestAt = -1;
    for (const e of ready) {
      const n = newestPos(e, localPos(e.id)), d = e.duration_s || 0;
      if (n.s > 0 && (!d || n.s < d - 2) && n.at > bestAt) { best = e; bestAt = n.at; }
    }
    if (best) return best;
    const pool = ready.length ? ready : eps;
    return pool.find((e) => e.mine) || pool[0];
  }

  // ------------------------------------------------------------------ positions
  // {s, at}: the position and when it was taken (ms). The hub keeps each person's; this device
  // keeps its own copy too, for when the hub cannot be reached. The newer one wins, so the phone
  // resumes where the desktop stopped.
  const posKey = (eid) => mine(`pos.${eid}`);
  function localPos(eid) {
    const raw = store.get(posKey(eid));
    if (raw === null) return null;
    try {
      const v = JSON.parse(raw);
      if (v && typeof v.s === "number" && v.s >= 0) return { s: v.s, at: Number(v.at) || 0 };
    } catch (e) { /* junk */ }
    return null;
  }
  function newestPos(e, l) {
    const n = { s: e.position_s || 0, at: e.position_at || 0 };
    return l && l.at > n.at ? l : n;
  }
  function durOf(e) {
    const a = A();
    if (S.audioEp === e.id && isFinite(a.duration) && a.duration > 0) return a.duration;
    return e.duration_s || 0;
  }
  function posOf(e) {
    const a = A();
    if (S.audioEp === e.id && S.audioReady === e.id) return a.ended ? durOf(e) : a.currentTime;
    return newestPos(e, localPos(e.id)).s;
  }
  const isPlaying = (eid) => !!eid && S.audioEp === eid && !A().paused && !A().ended;

  // ------------------------------------------------------------------ toast
  let toastTimer = null;
  function toast(msg, action, ms) {
    const t = $("toast"), b = $("toast-act");
    $("toast-msg").textContent = msg;
    t.classList.toggle("one", !!action);
    b.hidden = !action;
    b.onclick = null;
    if (action) {
      b.textContent = action.label;
      b.onclick = () => { hideToast(); action.fn(); };
    }
    t.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(hideToast, ms || (action ? 10000 : 6000));
  }
  function hideToast() { clearTimeout(toastTimer); $("toast").hidden = true; }

  // ------------------------------------------------------------------ listened
  // Each person's own tick: shown at once, stored on the hub. Changes to one paper go one after
  // another, and until the last is answered what was set is what the page shows: an event or
  // an answer that left before the newest change cannot bring an older tick back.
  S.edits = new Map();      // id -> {listened, n: requests in flight, chain, failed, last}
  function pinEdits(v) {
    const e = v && S.edits.get(v.id);
    if (e && "listened" in e) v.listened = e.listened;
    return v;
  }
  function setListened(id, on) {
    const p = S.papers.get(id);
    if (!p) return;
    const e = S.edits.get(id) || { n: 0, chain: Promise.resolve(), failed: null, last: null };
    S.edits.set(id, e);
    e.listened = !!on; e.n++;
    p.listened = !!on;
    refresh(id);
    if (S.open === id) renderWin();
    const body = { listened: !!on };
    e.chain = e.chain.then(() => api("PUT", `/api/papers/${id}/listened`, body))
      .then((v) => { e.last = v; }, (err) => { e.failed = err; })
      .then(async () => {
        if (--e.n) return;
        S.edits.delete(id);
        let v = e.last;
        if (e.failed) {
          toast(e.failed.message);
          try { v = await api("GET", `/api/papers/${id}`); } catch (x) { v = null; }
        }
        if (v && v.id && S.papers.has(id)) upsert(v);
      });
  }

  // ------------------------------------------------------------------ deleting a version
  // Its maker or an admin: gone at once; the hub keeps it 30 days, and Undo brings it back.
  async function deleteVersion(eid) {
    const pid = S.epPaper.get(eid), p = S.papers.get(pid);
    const e = p && (p.episodes || []).find((x) => x.id === eid);
    if (!e) return;
    closeMenu();
    const rest = p.episodes.filter((x) => x.id !== eid);
    if (S.audioEp === eid) unloadAudio();
    if (store.get(mine(`pick.${pid}`)) === eid) store.del(mine(`pick.${pid}`));
    if (rest.length) S.papers.set(pid, Object.assign({}, p, { episodes: rest }));
    else {
      S.papers.delete(pid);
      if (S.open === pid) closePaper();
    }
    refresh(pid);
    if (S.open === pid) renderWin();
    try {
      await api("DELETE", `/api/episodes/${eid}`);
      const what = rest.length ? `Deleted ${e.mine ? "your" : `${makerName(e)}’s`} version`
        : displayTitle(p) ? `Deleted “${short(displayTitle(p))}”` : "Deleted";
      toast(what, { label: "Undo", fn: () => undelete(eid) }, 10000);
    } catch (err) {
      if (err.status !== 404) { S.papers.set(pid, p); refresh(pid); if (S.open === pid) renderWin(); }
      toast(err.message);
    }
  }
  async function undelete(eid) {
    try {
      upsert(await api("POST", `/api/episodes/${eid}/undelete`, {}));
    } catch (e) {
      toast(e.code === "undo_expired" ? "Too late: it is deleted." : e.message);
    }
  }

  function removed(id) {
    if (!S.papers.has(id) && S.open !== id) return;
    S.papers.delete(id);
    if (S.audioPaper === id) unloadAudio();
    if (S.open === id) closePaper();
    refresh(id);
  }

  // ------------------------------------------------------------------ the library
  // S.papers holds every paper in the library (GET /api/library), read whole at the start and
  // on a resync, one paper again on its event: the player, Versions, Up next and the map draw
  // from it (the map is given this very Map). There is no list of papers to draw any more: the
  // home is the graph list and the map (graphs.js, map.js).
  function upsert(v) {
    pinEdits(v);
    S.papers.set(v.id, v);
    indexEps(v);
    // a version that was playing and is gone (deleted elsewhere) stops
    if (S.audioPaper === v.id && !(v.episodes || []).some((e) => e.id === S.audioEp)) unloadAudio();
    refresh(v.id);
    if (S.open === v.id) renderWin();
  }
  // What shows a paper besides its window: the player bar, the map's colours, a phone's list of
  // a graph's papers.
  function refresh(pid) {
    renderPlayer();
    mapSync();
    if (S.graphs) S.graphs.paperChanged(pid);
    // the open paper's versions say how far each one is
    const p = pid && pid === S.open && S.view === "paper" ? S.papers.get(pid) : null;
    if (p) renderVersions(p, chosen(p));
  }
  // Papers the search found: known here from now on (their match is the search's own).
  function known(list) {
    for (const p of list || []) {
      if (!p || typeof p.id !== "string") continue;
      const v = Object.assign({}, p);
      delete v.match;
      S.papers.set(v.id, pinEdits(v));
      indexEps(v);
    }
  }
  async function loadLibrary() {
    let j;
    try { j = await api("GET", "/api/library"); } catch (e) { toast(e.message); return false; }
    const fresh = new Map((j.papers || []).map((p) => [p.id, pinEdits(p)]));
    for (const id of [...S.papers.keys()]) if (!fresh.has(id)) S.papers.delete(id);
    for (const [id, p] of fresh) { S.papers.set(id, p); indexEps(p); }
    S.loaded = true;
    refresh();
    if (S.open) { if (!S.papers.has(S.open)) closePaper(); else renderWin(); }
    return true;
  }

  // One short line for a version: how long, how much is left, or how far it has got.
  function stateLine(e) {
    if (!e) return { text: "" };
    if (e.has_audio) {
      const d = durOf(e), t = posOf(e);
      if (!d) return { text: "Ready" };
      if (t >= d - 2) return { text: "Played" };
      if (t > 0) return { text: `${Math.max(1, Math.round((d - t) / 60))} min left` };
      return { text: `${Math.max(1, Math.round(d / 60))} min` };
    }
    const st = e.state;
    if (st === "speaking") return { text: `Speaking ${speakPct(e)}%` };
    if (st === "waiting-for-gpu") return { text: "Waiting for GPU", cls: "warn" };
    if (st === "failed") return { text: "Voice failed", cls: "danger" };
    if (st === "ready") return { text: "Audio missing", cls: "danger" };
    if (st === "checking") return { text: "Checking…" };
    return { text: st || "" };
  }

  // A paper or episode event names what changed; those papers alone are read again, in one
  // request (a big burst, or one the page cannot place, reads the whole library). One that is
  // not in the answer has left the library.
  S.touch = new Set(); S.touchTimer = null;
  function touched(pid) {
    if (!pid) { loadLibrary(); return; }
    S.touch.add(pid);
    if (!S.touchTimer) S.touchTimer = setTimeout(flushTouched, 150);
  }
  async function flushTouched() {
    S.touchTimer = null;
    const ids = [...S.touch];
    S.touch.clear();
    if (ids.length > 40) { loadLibrary(); return; }
    let j;
    try { j = await api("GET", `/api/library?ids=${ids.map(encodeURIComponent).join(",")}`); } catch (e) { return; }
    const got = new Set();
    for (const v of j.papers || []) { got.add(v.id); upsert(v); }
    for (const id of ids) if (!got.has(id)) removed(id);
  }

  // ------------------------------------------------------------------ offline, a newer build
  function setOffline(on) {
    clearTimeout(S.offTimer);
    const o = $("offline");
    if (!on) { o.hidden = true; return; }
    S.offTimer = setTimeout(() => { o.textContent = "The hub is not answering · reconnecting…"; o.hidden = false; }, 4000);
  }
  // The hub's build (a hash of these static files) comes in /api/config and the event stream's
  // hello, also the first after the stream reconnects. This page never keeps running code older
  // than the hub's: when the build changes it reloads itself. Not while audio plays (at the next
  // pause or end), not mid-keystroke, and not over unsaved settings.
  function checkBuild(b) {
    if (!S.build || !b || b === S.build || S.reloadFor === b) return;
    S.reloadFor = b;
    maybeReload();
  }
  function maybeReload() {
    if (!S.reloadFor) return;
    const a = A();
    if (S.audioEp && a.getAttribute("src") && !a.paused && !a.ended) return;   // the pause or end calls again
    if (Date.now() - S.typedAt < 3000 || settingsDirty()) { setTimeout(maybeReload, 2000); return; }
    // Once per target build: if the hub answered the reload with the old build again, stay.
    if (tab.get("pcg.reloaded") === S.reloadFor) return;
    tab.set("pcg.reloaded", S.reloadFor);
    savePosition(true, true);
    location.reload();
  }

  // ------------------------------------------------------------------ where the page is: its address
  //   #g=<graph>             the graph on the map (a phone: its papers, as a list)
  //   #g=<graph>&p=<paper>   a paper in it: its window over the map
  //   #g=<graph>&map         a phone: the graph's drawing
  //   #p=<paper>             the paper in a graph that has it (the one shown, else one subscribed
  //                          to, else any), else in Not in any graph
  //   #settings[=tab], #listening
  //   nothing                where the page lands: the graph open last (the account's, on the hub),
  //                          else the one with the most papers; afterwards, a phone's home is the
  //                          list of graphs
  // A step the page takes itself (a paper opened, the drawing, another graph) is pushed, so the
  // browser's Back and the page's own back buttons undo it.
  const GRX = /^(g_[a-z0-9]{4,32}|none)$/, PRX = /^p_[a-z0-9]{4,32}$/;
  const hashFor = (gid, pid, map) => [gid ? `g=${gid}` : "", pid ? `p=${pid}` : "", map ? "map" : ""].filter(Boolean).join("&");
  function nav(hash, push) {
    if ((hash ? `#${hash}` : "") !== location.hash) {
      const url = hash ? `#${hash}` : location.pathname + location.search;
      if (push) history.pushState({ pcg: 1, from: location.hash }, "", url); else history.replaceState(history.state, "", url);
    }
    openFromHash();
  }
  // Back to `hash`: the browser's own step back when the page came from there, else there without a step.
  function back(hash) {
    const to = hash ? `#${hash}` : "";
    if (history.state && history.state.pcg && history.state.from === to) history.back(); else nav(hash, false);
  }
  // Is the paper in this graph (as the library says)?
  function inGraph(gid, pid) {
    const p = paperOf(pid);
    if (!p || !Array.isArray(p.graphs)) return false;
    return gid === "none" ? !p.graphs.length : p.graphs.includes(gid);
  }
  function openPaper(id, gid) {
    closeMenu();
    const g = gid || (S.graph && inGraph(S.graph, id) ? S.graph : null);
    nav(g ? hashFor(g, id, S.phoneMap) : `p=${id}`, true);
  }
  function openGraph(gid) { closeMenu(); if (gid !== S.graph || S.view !== "home" || S.phoneMap) nav(hashFor(gid), true); }
  function closePaper() { back(hashFor(S.graph, null, S.phoneMap)); }
  // Back from Settings or Listening, and where the tour starts from: the graph (a phone: the list of graphs).
  function goHome() {
    if (phone()) { S.landed = true; nav("", false); }          // a return home, not a landing: the list of graphs
    else nav(hashFor(S.graph || (S.graphs && S.graphs.landing())), false);
  }
  function openFromHash() {
    S.handled = location.hash;
    const s = /^#settings(?:=([a-z]+))?$/.exec(location.hash);
    if (s) return openSettings(s[1]);
    if (location.hash === "#listening") return openListening();
    const q = new URLSearchParams(location.hash.replace(/^#/, ""));
    const gid = GRX.test(q.get("g") || "") ? q.get("g") : null, pid = PRX.test(q.get("p") || "") ? q.get("p") : null;
    if (pid && !gid) { placePaper(pid); return; }
    if (!gid) { home(); return; }
    showGraph(gid, q.has("map"));
    if (pid) openWin(pid); else closeWin();
  }
  // #p=: the graph it opens in, once the library and the graph list are in.
  async function placePaper(pid) {
    const want = location.hash;
    await S.ready;
    let p = paperOf(pid);
    if (!p) { try { p = await api("GET", `/api/papers/${pid}`); upsert(p); } catch (e) { toast(e.message); p = null; } }
    if (location.hash !== want) return;           // gone elsewhere meanwhile
    if (!p) { nav(hashFor(S.graph || S.graphs.landing()), false); return; }
    nav(hashFor(S.graphs.graphFor(pid, p.graphs), pid), false);
  }
  function home() {
    S.open = null;
    closeExplainer(); closeMenu();
    if (phone() && S.landed) { showGraph(null); showView("home"); document.title = "Papers"; return; }
    S.ready.then(() => {
      if (location.hash) return;
      const gid = S.graphs.landing();
      S.landed = true;
      if (!gid) { showGraph(null); showView("home"); return; }
      // a phone lands in the graph, with Back to the list of graphs
      if (phone()) { history.pushState({ pcg: 1, from: "" }, "", `#${hashFor(gid)}`); openFromHash(); } else nav(hashFor(gid), false);
    });
  }
  function showGraph(gid, map) {
    const was = S.graph;
    S.graph = gid;
    S.landed = S.landed || !!gid;
    S.phoneMap = !!(gid && map && phone());
    if (S.graphs) { S.graphs.setCurrent(gid); S.graphs.showPapers(phone() ? gid : null); }
    document.body.classList.toggle("gpl-open", !!gid && phone());
    if (gid && S.map && S.map.current() !== gid) S.map.graph(gid);
    if (gid && gid !== was) markOpened(gid);
    mapShown();
  }
  // This person opened the graph: its new papers are seen, and the page lands here next time.
  function markOpened(gid) {
    clearTimeout(S.openedT);
    S.openedT = setTimeout(() => {
      if (S.graph !== gid) return;
      api("POST", `/api/graphs/${encodeURIComponent(gid)}/opened`, {}).then(() => { if (S.graphs) S.graphs.opened(gid); }, () => {});
    }, 300);
  }
  function showView(v) {
    S.view = v;
    $("paper").hidden = v !== "paper";
    $("settings").hidden = v !== "settings";
    $("listening").hidden = v !== "listening";
    for (const k of ["paper", "settings", "listening"]) document.body.classList.toggle(`v-${k}`, v === k);
    document.body.classList.toggle("open", v !== "home");
    if (v === "settings") $("set-btn").setAttribute("aria-current", "page"); else $("set-btn").removeAttribute("aria-current");
    if (v === "listening") $("listen-btn").setAttribute("aria-current", "page"); else $("listen-btn").removeAttribute("aria-current");
    if (v !== "listening" && S.listening) S.listening.hide();
    mapShown();
    mapInset();
  }
  // Listening (listening.js, hub/listening.py): read from the hub each time it opens.
  function openListening() {
    S.open = null;
    closeExplainer(); closeMenu();
    showView("listening");
    document.title = "Listening · Papers";
    refresh();
    if (S.listening) S.listening.render($("ls-body"));
    $("win").scrollTop = 0;
  }
  async function openWin(id) {
    if (S.open !== id) { S.open = id; S.details = false; closeExplainer(); closeMenu(); setPane("tr"); }
    showView("paper");
    if (!S.papers.has(id)) {
      try { upsert(await api("GET", `/api/papers/${id}`)); } catch (e) { toast(e.message); return closePaper(); }
      if (S.open !== id) return;
    }
    refresh(id);
    // its card on the map, beside the window (a wide screen; a phone's drawing, when it is open)
    if (S.map && S.graph && (!phone() || S.phoneMap)) S.map.select(id, S.graph);
    // A paper that is playing keeps playing while another one is looked at.
    const c = chosen(S.papers.get(id));
    if (c && !isPlaying(S.audioEp)) loadAudio(c.id);
    renderWin();
    $("win").scrollTop = 0; $("p-mid").scrollTop = 0;
  }
  function closeWin() {
    if (S.view === "home" && !S.open) return;
    S.open = null;
    closeExplainer(); closeMenu();
    showView("home");
    document.title = "Papers";
    refresh();
  }

  // ------------------------------------------------------------------ the map
  // map.js (window.PaperMap) draws the graph the list picks, beside it (a phone: over it, from
  // the graph's "Map"). The papers it colours (heard: the Listened tick, or a version finished)
  // are S.papers. Its list of graphs is the column's (onList); a graph it moves to by itself
  // (another one a paper is "Also in", the one before a deleted one) becomes the page's.
  S.map = null; S.ready = null;
  function mapShown() {
    const host = $("map");
    const on = phone() ? S.phoneMap && (S.view === "home" || S.view === "paper") : (S.view === "home" || S.view === "paper");
    if (on === !host.hidden) return;
    host.hidden = !on;
    if (S.map) { if (on) S.map.show(); else S.map.hide(); }
  }
  // the paper's window covers the map's right side (a wide screen): papers are centred left of it
  function mapInset() {
    const w = !phone() && S.view === "paper" ? box($("win")).width : 0;
    document.body.style.setProperty("--win-w", `${w}px`);       // graphs.css: the map's bar and panels stop there
    if (S.map && S.map.inset) S.map.inset(w);
  }
  function mapShowed(gid) {
    if (gid === S.graph) return;
    if (!gid) { if (S.graphs) nav(hashFor(S.graphs.landing()), false); return; }      // its graph was deleted
    nav(hashFor(gid, null, S.phoneMap), true);
  }
  function mapClose() {
    if (S.phoneMap) back(hashFor(S.graph));
    else if (S.view === "paper") closePaper();
  }
  function mapData(gid, data) {
    if (S.graphs) S.graphs.graphData(gid, data);
    // new papers in the graph on the screen: seen
    const it = S.graphs && S.graphs.item(gid);
    if (gid === S.graph && it && it.new && document.visibilityState === "visible") markOpened(gid);
  }
  function mapSync() { if (S.map && S.map.changed) S.map.changed(); }
  // Every live event also goes to the map (graph and log events are its own): as
  // map.event(kind, data) when it has one, and as a "papercast:event" on window.
  function toMap(kind, d) {
    if (S.map && typeof S.map.event === "function") { try { S.map.event(kind, d); } catch (e) { /* the map's own trouble */ } }
    window.dispatchEvent(new CustomEvent("papercast:event", { detail: { kind, data: d } }));
  }
  // What the tour asks for: the drawing (a phone opens it for the graph; a wide screen has it as
  // the home), and back.
  function openMap() {
    if (phone()) { const g = S.graph || (S.graphs && S.graphs.landing()); if (g && !S.phoneMap) nav(hashFor(g, null, true), true); }
    else if (S.view !== "home") nav(hashFor(S.graph || (S.graphs && S.graphs.landing())), false);
    return Promise.resolve();
  }
  function closeMap() { if (S.phoneMap) back(hashFor(S.graph)); }

  // ------------------------------------------------------------------ the window
  function renderWin() {
    const p = S.papers.get(S.open);
    if (!p || S.view !== "paper") return;
    const c = chosen(p);
    const title = titleInto($("w-title"), p);
    document.title = title ? `${title} · Papers` : "Papers";
    // The first author only; the ⋯ menu opens the paper's link.
    $("w-by").replaceChildren(shortAuthors(p));
    $("w-maker").textContent = c ? whoLine(c) : "";
    const det = $("w-details");
    det.hidden = !S.details;
    if (S.details) {
      const who = (p.authors || []).join(", ");
      det.textContent = [p.year ? `${who}${who ? " " : ""}(${p.year})` : who, p.added_at ? `added ${day(p.added_at)}` : "",
        c && c.duration_s ? `${Math.round(c.duration_s / 60)} min` : "", p.arxiv_id ? `arXiv ${p.arxiv_id}` : "",
        c && c.model ? c.model : ""].filter(Boolean).join(" · ");
    }
    const tags = p.tags || [];
    $("w-tags").hidden = !tags.length;
    $("w-tags").textContent = tags.join(" · ");
    const lb = $("w-listened");
    lb.hidden = !anyAudio(p);
    lb.setAttribute("aria-checked", String(!!p.listened));
    lb.querySelector(".box").innerHTML = p.listened ? I.check : "";
    $("x-open").disabled = !(c && c.has_explainer);
    renderState(c);
    renderVersions(p, c);
    renderVoice(c);
    renderPlayer();
    renderQueue();
    renderTranscript(c);
    if (S.social) S.social.paper(p, c);
    renderTabs();
  }

  // Where the window is too narrow for the comments column (a phone), the transcript and the
  // comments are two tabs under the versions.
  function setPane(k) {
    S.pane = k === "c" ? "c" : "tr";
    $("paper").classList.toggle("pane-c", S.pane === "c");
    $("pt-tr").setAttribute("aria-selected", String(S.pane === "tr"));
    $("pt-c").setAttribute("aria-selected", String(S.pane === "c"));
    if (S.pane === "tr" && isPlaying(T.eid)) { T.follow = true; keepInView(true); }
    trNowBtn();
  }
  function renderTabs() {
    const n = S.social && S.open ? S.social.count(S.open) : 0, t = n ? `Comments (${n})` : "Comments";
    if ($("pt-c").textContent !== t) $("pt-c").textContent = t;
  }

  function renderState(c) {
    const box = $("w-state");
    if (!c || c.has_audio) { box.hidden = true; box.replaceChildren(); return; }
    const big = (text, cls) => el("div", { class: `big t${cls ? ` ${cls}` : ""}`, text });
    const detail = (text) => (text ? el("div", { class: "detail", text }) : null);
    const kids = [];
    const st = c.state;
    if (st === "speaking") {
      const pct = speakPct(c);
      const bar = el("div", { class: "bar" }, el("i"));
      bar.firstChild.style.width = `${pct}%`;
      kids.push(big(`Speaking ${pct}%`), bar, detail("Recording the voice."));
    } else if (st === "waiting-for-gpu") {
      kids.push(big("Waiting for GPU", "warn"));
    } else if (st === "failed") {
      kids.push(big("Couldn't record the voice", "danger"), detail(c.state_detail || "The voice worker gave up on it."));
    } else if (st === "ready") {
      kids.push(big("The audio is missing", "danger"));
    } else {
      kids.push(big("Checking…"), detail("The hub is checking the upload."));
    }
    box.hidden = false;
    box.replaceChildren(...kids.filter(Boolean));
  }

  // The versions of one paper, oldest first: a tap picks the one that plays (remembered here).
  function renderVersions(p, c) {
    const eps = p.episodes || [];
    const sec = $("versions"), box = $("vlist");
    if (eps.length < 2) { sec.hidden = true; box.replaceChildren(); box.dataset.sig = ""; return; }
    sec.hidden = false;
    const lines = eps.map((e) => [e.id, makerName(e), e.mine, e.prefs_summary, stateLine(e), c && c.id === e.id]);
    const sig = JSON.stringify([p.id, lines]);
    if (box.dataset.sig === sig) return;
    box.dataset.sig = sig;
    box.replaceChildren(...lines.map(([id, name, own, sum, st, on]) => el("button", {
      type: "button", class: "ver", role: "radio", "aria-checked": String(!!on), "data-ep": id,
      onclick: () => pickVersion(p.id, id),
    }, el("span", { class: "mark", "aria-hidden": "true" }),
    el("span", { class: "v-main" }, el("span", { class: "v-who", text: `by ${name}${own ? " (you)" : ""}` }),
      sum ? el("span", { class: "v-sum", text: sum }) : null),
    el("span", { class: `v-st t${st.cls ? ` ${st.cls}` : ""}`, text: st.text }))));
  }
  function pickVersion(pid, eid) {
    const p = S.papers.get(pid);
    const e = p && (p.episodes || []).find((x) => x.id === eid);
    if (!e) return;
    store.set(mine(`pick.${pid}`), eid);
    // Into the player at once, unless another paper is playing: then it waits for Play.
    if (e.has_audio && S.audioEp !== eid) {
      const was = S.audioPaper === pid && isPlaying(S.audioEp);
      if (was || !isPlaying(S.audioEp)) {
        loadAudio(eid);
        if (was) A().play().catch(() => {});
      }
    }
    renderWin();
    refresh(pid);
  }

  // ------------------------------------------------------------------ audio player
  let lastSaved = 0, lastRowTick = 0;
  function loadAudio(eid, then) {
    const e = epById(eid), pid = S.epPaper.get(eid), a = A();
    if (!e || !e.has_audio) return false;
    const src = audioUrl(e);
    if (S.audioEp === eid && a.getAttribute("src") === src) {
      if (then && S.audioReady === eid) then(a); else if (then) S.afterReady = then;
      return true;
    }
    if (S.audioEp) { savePosition(true, false, true); const old = S.audioPaper; a.pause(); S.audioEp = null; S.audioPaper = null; if (old) refresh(old); }
    S.audioEp = eid; S.audioPaper = pid; S.audioReady = null; S.afterReady = null; S.dirty = false;
    a.src = src;
    a.playbackRate = S.speed;
    // The hub's position, fresh: the list may have been loaded before another device played.
    const fresh = api("GET", `/api/papers/${pid}`).then((v) => {
      if (v && v.id && S.papers.has(v.id)) { S.papers.set(v.id, pinEdits(v)); indexEps(v); } else if (v && v.id && Q.papers.has(v.id)) Q.papers.set(v.id, v);
      return v;
    }).catch(() => null);
    a.addEventListener("loadedmetadata", async () => {
      const v = await fresh;
      if (S.audioEp !== eid || a.getAttribute("src") !== src) return;
      const q = (v && (v.episodes || []).find((x) => x.id === eid)) || e;
      const { s } = newestPos(q, localPos(eid));
      if (s > 0 && s < (a.duration || Infinity) - 2) a.currentTime = s;
      a.playbackRate = S.speed;
      S.audioReady = eid;          // only now may a position be saved: before, it would be 0
      if (!a.paused) S.dirty = true;
      if (then) then(a);
      if (S.afterReady) { const f = S.afterReady; S.afterReady = null; f(a); }
      renderPlayer(); refresh(pid);
    }, { once: true });
    if ("mediaSession" in navigator) {
      const p = paperOf(pid) || {};
      try { navigator.mediaSession.metadata = new MediaMetadata({ title: displayTitle(p) || "Papers", artist: shortAuthors(p), album: whoLine(e) }); } catch (x) { /* old browser */ }
    }
    renderPlayer();
    return true;
  }
  function unloadAudio() {
    const a = A();
    savePosition(true, false, true);
    a.pause(); a.removeAttribute("src"); a.load();
    const old = S.audioPaper;
    S.audioEp = null; S.audioPaper = null; S.audioReady = null;
    if (old) refresh(old);
    renderPlayer();
  }
  // To the hub while playing every 10 s, and at once on a pause, a seek or the end; a burst of
  // seeks is one request (half a second after the last). Leaving the page, it goes as a
  // keepalive request, so it arrives after the tab has gone.
  // Each also says what hub/listening.py counts the time heard by: the speed, whether it is
  // playing as it is taken (not at a pause, the end, leaving, or switching to another), this
  // page load (a random id: time counts only between two of the same tab's) and the time zone.
  let putWant = null, putTimer = null;
  const TAB = (() => { try { return Array.from(crypto.getRandomValues(new Uint32Array(2)), (x) => x.toString(36)).join("-"); } catch (e) { return `t${Date.now().toString(36)}${Math.random().toString(36).slice(2, 8)}`; } })();
  function putPosition(eid, s, at, leaving, more) {
    if (putWant && putWant.eid !== eid) flushPosition(false);
    putWant = Object.assign({ eid, s, at }, more || {});
    if (leaving) { flushPosition(true); return; }
    if (!putTimer) putTimer = setTimeout(() => flushPosition(false), 500);
  }
  function flushPosition(leaving) {
    clearTimeout(putTimer); putTimer = null;
    const w = putWant;
    putWant = null;
    if (w) {
      api("PUT", `/api/episodes/${w.eid}/position`, { s: w.s, at: w.at, rate: w.rate, playing: !!w.playing, tab: TAB,
        tz: new Date(w.at).getTimezoneOffset(), d: w.d }, { keepalive: !!leaving }).catch(() => {});
    }
  }
  function savePosition(force, leaving, stopping) {
    const a = A(), eid = S.audioEp;
    // Saved only once it was played or moved here: opening a paper must not change its place.
    if (!eid || !S.dirty || S.audioReady !== eid || !a.getAttribute("src") || !isFinite(a.currentTime) || a.readyState < 1) {
      if (leaving) flushPosition(true);
      return;
    }
    // A finished episode is stored at its end: the list says "Played", and it starts over.
    const s = a.ended ? (a.duration || a.currentTime) : a.currentTime, now = Date.now();
    store.set(posKey(eid), JSON.stringify({ s, at: now }));
    const e = epById(eid);
    if (e) { e.position_s = s; e.position_at = now; }
    if (force || now - lastSaved > 10000) {
      lastSaved = now;
      putPosition(eid, s, now, leaving, { rate: a.playbackRate || 1, playing: !leaving && !stopping && !a.paused && !a.ended,
        d: isFinite(a.duration) && a.duration > 0 ? a.duration : undefined });
    }
  }
  function withAudio(eid, fn) { loadAudio(eid, fn); }
  function openEp() { const p = S.papers.get(S.open); return p ? chosen(p) : null; }
  function togglePlay(eid) {
    const a = A();
    const e = eid ? epById(eid) : barEpisode();
    if (!e || !e.has_audio) return;
    if (S.audioEp === e.id && a.getAttribute("src")) {
      if (a.paused || a.ended) a.play().catch((x) => toast(`Cannot play: ${x.message}`)); else a.pause();
      return;
    }
    loadAudio(e.id);
    a.play().catch((x) => { if (x.name !== "AbortError") toast(`Cannot play: ${x.message}`); });
  }
  function seekTo(e, t) {
    withAudio(e.id, (a) => {
      S.dirty = true;
      const d = a.duration || durOf(e);
      a.currentTime = Math.max(0, Math.min(d || 0, t));
      savePosition(true); renderPlayer(); refresh(S.epPaper.get(e.id));
    });
  }
  function skip(delta) {
    const e = barEpisode();
    if (!e || !e.has_audio) return;
    withAudio(e.id, (a) => { S.dirty = true; a.currentTime = Math.max(0, Math.min(a.duration || 0, a.currentTime + delta)); savePosition(true); renderPlayer(); });
  }
  // The bar at the bottom plays one episode wherever the page is: the one loaded in <audio>,
  // else (after a reload) the one listened to most recently that is part-way through. Opening a
  // paper loads its version unless another one is playing; that paper then has its own Play.
  function barEpisode() {
    const a = A();
    if (S.audioEp && a.getAttribute("src")) return epById(S.audioEp);
    let best = null, bestAt = -1;
    for (const p of S.papers.values()) {
      for (const e of p.episodes || []) {
        if (!e.has_audio) continue;
        const n = newestPos(e, localPos(e.id)), d = e.duration_s || 0;
        if (!(n.s > 0 && d && n.s < d - 2)) continue;
        if (n.at > bestAt) { best = e; bestAt = n.at; }
      }
    }
    return best;
  }
  function renderPlayer() {
    const e = barEpisode(), bar = $("bar");
    S.barEp = e ? e.id : null;
    const was = !bar.hidden;
    bar.hidden = !e;
    document.body.classList.toggle("has-bar", !!e);
    if (!e) { if (S.barOpen) openBar(false); } else {
      const d = durOf(e), t = S.scrubbing !== null ? S.scrubbing * d : posOf(e), f = d ? Math.min(1, t / d) : 0;
      const playing = isPlaying(e.id);
      $("fill").style.width = `${f * 100}%`;
      $("thumb").style.left = `${f * 100}%`;
      $("b-prog").style.width = `${f * 100}%`;
      $("p-el").textContent = hms(t);
      $("p-rem").textContent = `−${hms(Math.max(0, d - t))}`;
      const sc = $("scrub");
      sc.setAttribute("aria-valuemax", String(Math.round(d)));
      sc.setAttribute("aria-valuenow", String(Math.round(t)));
      sc.setAttribute("aria-valuetext", `${hms(t)} of ${hms(d)}`);
      const p = paperOf(S.epPaper.get(e.id)) || {};
      if ($("bar-title").dataset.ep !== e.id || $("bar-title").textContent !== displayTitle(p)) { titleInto($("bar-title"), p); $("bar-title").dataset.ep = e.id; }
      const sub = phone() ? `${Math.max(1, Math.round((d - t) / 60))} min left · ${whoLine(e)}` : whoLine(e);
      if ($("bar-sub").textContent !== sub) $("bar-sub").textContent = sub;
      const pb = $("p-play");
      if (pb.dataset.on !== String(playing)) {
        pb.dataset.on = String(playing);
        pb.innerHTML = playing ? I.pause(20) : I.play(20); pb.setAttribute("aria-label", playing ? "Pause" : "Play");
      }
    }
    if (was !== !bar.hidden) barHeight();
    // the open paper's own Play, while the bar has another one
    const c = S.view === "paper" ? openEp() : null;
    $("w-play").hidden = !(c && c.has_audio && (!e || c.id !== e.id));
  }
  // The window and the list end where the bar starts (the bar's own height, in this page's px).
  function barHeight() {
    const bar = $("bar");
    document.body.style.setProperty("--bar-h", `${bar.hidden ? 0 : bar.offsetHeight}px`);
  }
  // On a phone the bar is one line (Play, the title, a thin line for how far); a tap on it opens
  // the rest (the position, skips, speed, Up next), and a tap on the title then opens the paper.
  function openBar(on) {
    S.barOpen = !!on;
    $("bar").classList.toggle("open", S.barOpen);
    const x = $("bar-exp");
    x.setAttribute("aria-expanded", String(S.barOpen));
    x.innerHTML = S.barOpen ? PI.down2 : PI.up2;
    barHeight();
  }
  function setSpeed(v) {
    S.speed = v;
    A().playbackRate = v;
    $("p-speed").textContent = `${v}×`;
    store.set("pcg.speed", String(v));
  }
  function wirePlayer() {
    const a = A();
    $("p-play").innerHTML = I.play(20); $("w-play").innerHTML = I.play(20);
    $("p-back").innerHTML = I.skip(15, false); $("p-fwd").innerHTML = I.skip(30, true);
    $("x-icon").innerHTML = I.doc;
    $("bar-exp").innerHTML = PI.up2;
    $("p-play").addEventListener("click", () => { if (S.barEp) togglePlay(S.barEp); });
    $("w-play").addEventListener("click", () => { const c = openEp(); if (c) togglePlay(c.id); });
    $("p-back").addEventListener("click", () => skip(-15));
    $("p-fwd").addEventListener("click", () => skip(30));
    $("p-speed").addEventListener("click", () => setSpeed(SPEEDS[(SPEEDS.indexOf(S.speed) + 1) % SPEEDS.length]));
    $("bar-open").addEventListener("click", () => {
      if (phone() && !S.barOpen) { openBar(true); return; }
      openBar(false);
      if (S.barEp) openPaper(S.epPaper.get(S.barEp));
    });
    $("bar-exp").addEventListener("click", () => openBar(!S.barOpen));
    if (window.ResizeObserver) new ResizeObserver(barHeight).observe($("bar"));
    window.matchMedia("(max-width: 720px)").addEventListener("change", () => { if (!phone()) openBar(false); renderPlayer(); });
    const sc = $("scrub");
    const frac = (x) => { const r = sc.getBoundingClientRect(); return Math.max(0, Math.min(1, (x - r.left) / (r.width || 1))); };
    sc.addEventListener("pointerdown", (e) => { sc.setPointerCapture(e.pointerId); S.scrubbing = frac(e.clientX); renderPlayer(); });
    sc.addEventListener("pointermove", (e) => { if (S.scrubbing !== null && sc.hasPointerCapture(e.pointerId)) { S.scrubbing = frac(e.clientX); renderPlayer(); } });
    const commit = () => {
      if (S.scrubbing === null) return;
      const e = barEpisode(), f = S.scrubbing;
      S.scrubbing = null;
      if (e) seekTo(e, f * durOf(e));
    };
    sc.addEventListener("pointerup", commit);
    sc.addEventListener("pointercancel", () => { S.scrubbing = null; renderPlayer(); });
    sc.addEventListener("keydown", (ev) => {
      const e = barEpisode();
      if (!e) return;
      const k = { ArrowLeft: -15, ArrowDown: -15, ArrowRight: 30, ArrowUp: 30 }[ev.key];
      if (k) { ev.preventDefault(); skip(k); } else if (ev.key === "Home") { ev.preventDefault(); seekTo(e, 0); } else if (ev.key === "End") { ev.preventDefault(); seekTo(e, durOf(e) - 1); }
    });
    for (const ev of ["play", "pause", "durationchange", "ended", "seeked", "emptied"]) a.addEventListener(ev, () => { renderPlayer(); if (S.audioPaper) refresh(S.audioPaper); });
    a.addEventListener("timeupdate", () => {
      savePosition(false);
      renderPlayer();
      const now = Date.now();
      if (now - lastRowTick > 5000 && S.audioPaper) { lastRowTick = now; refresh(S.audioPaper); }
    });
    // Saved as it starts playing too: the time heard counts from there (hub/listening.py).
    a.addEventListener("playing", () => { if (S.audioReady === S.audioEp) { S.dirty = true; savePosition(true); } });
    // A reload waiting for the pause goes 1.5 s after it: the position is saved, and a quick
    // pause and play again is not cut off.
    a.addEventListener("pause", () => { savePosition(true); if (S.reloadFor) setTimeout(maybeReload, 1500); });
    a.addEventListener("ended", () => {
      savePosition(true);
      // heard, if the hub counted it finished (listening.py): the paper read again once the last position is in
      const pid = S.audioPaper;
      if (pid && S.papers.has(pid) && !S.papers.get(pid).heard) setTimeout(() => touched(pid), 1500);
      if (S.reloadFor) setTimeout(maybeReload, 1500);
    });
    a.addEventListener("error", () => { if (a.getAttribute("src")) toast("The audio could not be loaded."); });
    window.addEventListener("pagehide", () => savePosition(true, true));
    if ("mediaSession" in navigator) {
      const ms = navigator.mediaSession;
      try {
        ms.setActionHandler("play", () => a.play());
        ms.setActionHandler("pause", () => a.pause());
        ms.setActionHandler("seekbackward", () => { a.currentTime = Math.max(0, a.currentTime - 15); });
        ms.setActionHandler("seekforward", () => { a.currentTime = Math.min(a.duration || 0, a.currentTime + 30); });
      } catch (e) { /* unsupported action */ }
    }
    const saved = parseFloat(store.get("pcg.speed") || "1");
    setSpeed(SPEEDS.includes(saved) ? saved : 1);
  }

  // ------------------------------------------------------------------ up next
  // Each person's queue of episodes, kept on the hub so it follows them from the laptop to the
  // phone. A change shows at once and goes to the hub one after another; once the last is
  // answered, the hub's queue is what stays (as with the Listened tick). When an episode ends,
  // the first in the queue that has audio plays and leaves the queue; an episode that starts
  // playing any other way leaves it too. A window showing the one that ended follows to the next.
  const Q = { ids: [], rev: -1, papers: new Map(), n: 0, chain: Promise.resolve(), last: null, failed: null,
    again: false, lastFocus: null, dragging: false, timer: null };
  const PI = {
    queue: `<svg width="22" height="22" viewBox="0 0 22 22" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" aria-hidden="true"><path d="M3.5 6h11M3.5 11h11M3.5 16h6.5"/><path d="M14.5 13.6v5.2l4.2-2.6z" fill="currentColor" stroke-width="1.2" stroke-linejoin="round"/></svg>`,
    up: `<svg width="20" height="20" viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M10 16V4.5M5 9.5l5-5 5 5"/></svg>`,
    down: `<svg width="20" height="20" viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M10 4v11.5M5 10.5l5 5 5-5"/></svg>`,
    grip: `<svg width="16" height="16" viewBox="0 0 16 16" fill="currentColor" aria-hidden="true"><circle cx="5.5" cy="3.5" r="1.3"/><circle cx="10.5" cy="3.5" r="1.3"/><circle cx="5.5" cy="8" r="1.3"/><circle cx="10.5" cy="8" r="1.3"/><circle cx="5.5" cy="12.5" r="1.3"/><circle cx="10.5" cy="12.5" r="1.3"/></svg>`,
    prev: `<svg width="20" height="20" viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m5 12.5 5-5 5 5"/></svg>`,
    next: `<svg width="20" height="20" viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m5 7.5 5 5 5-5"/></svg>`,
    up2: `<svg width="22" height="22" viewBox="0 0 22 22" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m5 14 6-6 6 6"/></svg>`,
    down2: `<svg width="22" height="22" viewBox="0 0 22 22" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m5 8 6 6 6-6"/></svg>`,
  };
  const queued = (eid) => Q.ids.includes(eid);
  // An answer older than one already seen (a read that left before a change and came back
  // after it) is dropped: the hub's rev only goes up.
  function applyQueue(j) {
    if (!j || !Array.isArray(j.ids) || (typeof j.rev === "number" && j.rev < Q.rev)) return;
    for (const p of j.papers || []) { Q.papers.set(p.id, p); indexEps(p); }
    Q.ids = j.ids.slice();
    if (typeof j.rev === "number") Q.rev = j.rev;
    renderQueue();
  }
  async function loadQueue() {
    if (Q.n) { Q.again = true; return; }       // after the changes on their way
    let j;
    try { j = await api("GET", "/api/queue"); } catch (e) { return; }
    if (Q.n) { Q.again = true; return; }
    applyQueue(j);
  }
  function queueOp(body, ids) {
    Q.ids = ids;
    renderQueue();
    Q.n++;
    Q.chain = Q.chain.then(() => api("PUT", "/api/queue", body))
      .then((j) => { Q.last = j; }, (e) => { Q.failed = e; })
      .then(() => {
        if (--Q.n) return;
        const f = Q.failed, j = Q.last;
        Q.failed = null; Q.last = null;
        if (f) { if (f.status !== 404) toast(f.message); Q.again = false; loadQueue(); return; }
        applyQueue(j);
        if (Q.again) { Q.again = false; loadQueue(); }
      });
    return Q.chain;
  }
  function qAdd(ids) {
    const add = ids.filter((x) => !queued(x));
    return add.length ? queueOp({ op: "add", ids: add }, Q.ids.concat(add)) : Promise.resolve();
  }
  function qNext(ids) {
    return ids.length ? queueOp({ op: "next", ids }, ids.concat(Q.ids.filter((x) => !ids.includes(x)))) : Promise.resolve();
  }
  function qRemove(ids) {
    const gone = ids.filter(queued);
    return gone.length ? queueOp({ op: "remove", ids: gone }, Q.ids.filter((x) => !gone.includes(x))) : Promise.resolve();
  }
  function qMove(id, to) {
    const rest = Q.ids.filter((x) => x !== id);
    to = Math.max(0, Math.min(rest.length, to));
    rest.splice(to, 0, id);
    if (rest.join() === Q.ids.join()) { drawQueue(); return; }
    queueOp({ op: "move", id, to }, rest);
  }
  function qClear() { if (Q.ids.length) queueOp({ op: "clear" }, []); }

  // "Play next" and "Add to Up next" for a version with audio, but not the one playing now.
  const canQueue = (e) => !!(e && e.has_audio && !isPlaying(e.id));
  function queueItems(e) {
    if (!canQueue(e)) return [];
    return [mItem("Play next", () => { qNext([e.id]); toast("Plays next", null, 2500); }),
      queued(e.id) ? mItem("Remove from Up next", () => qRemove([e.id]))
        : mItem("Add to Up next", () => { qAdd([e.id]); toast("Added to Up next", null, 2500); })];
  }
  function playEp(eid) {
    if (!loadAudio(eid)) return false;
    A().play().catch((x) => { if (x.name !== "AbortError") toast(`Cannot play: ${x.message}`); });
    return true;
  }
  function advance() {
    const next = Q.ids.find((x) => { const e = epById(x); return x !== S.audioEp && e && e.has_audio; });
    if (!next) return;
    const follow = S.view === "paper" && !!S.open && S.open === S.audioPaper;
    qRemove([next]);
    if (!playEp(next)) return;
    const pid = S.epPaper.get(next);
    if (follow && pid && pid !== S.open) openPaper(pid);
  }

  // Ids from elsewhere (the map): episode ids, or paper ids for the version that would play.
  async function episodeIds(ids) {
    const want = [].concat(ids || []).filter((x) => typeof x === "string" && /^[ep]_[a-z0-9]{4,32}$/.test(x));
    const missing = [...new Set(want.filter((x) => x[0] === "p" && !paperOf(x)))];
    for (let i = 0; i < missing.length; i += 50) {
      try {
        const j = await api("GET", `/api/library?ids=${missing.slice(i, i + 50).join(",")}`);
        for (const p of j.papers || []) { Q.papers.set(p.id, p); indexEps(p); }
      } catch (e) { /* those papers are left out */ }
    }
    const out = [];
    for (const x of want) {
      const c = x[0] === "p" ? chosen(paperOf(x)) : epById(x) || { id: x, has_audio: true };
      if (c && c.has_audio && !out.includes(c.id)) out.push(c.id);
    }
    return out;
  }
  async function playAll(ids) {
    const eps = (await episodeIds(ids)).filter((x) => epById(x));
    if (!eps.length) return 0;
    const rest = eps.slice(1);
    if (rest.length) qNext(rest);
    playEp(eps[0]);
    return eps.length;
  }
  // For the map ("Play this graph in order") and anything else on the page. Each answers how
  // many episodes it took.
  window.papercastQueue = Object.freeze({
    add: async (ids) => { const e = (await episodeIds(ids)).filter((x) => !isPlaying(x)); await qAdd(e); return e.length; },
    playNext: async (id) => { const e = (await episodeIds(id)).filter((x) => !isPlaying(x)); await qNext(e); return e.length; },
    playAll,
  });

  // The panel: what plays, then the queue; a row plays at a tap, and moves by its arrows or by
  // dragging its handle (with a mouse).
  function openQueue() {
    if (!$("q-overlay").hidden) return;
    closeMenu();
    Q.lastFocus = document.activeElement;
    $("q-overlay").hidden = false;
    drawQueue();
    $("q-close").focus();
    loadQueue();
  }
  function closeQueue() {
    if ($("q-overlay").hidden) return;
    $("q-overlay").hidden = true;
    if (Q.lastFocus && Q.lastFocus.isConnected && Q.lastFocus.focus) Q.lastFocus.focus();
  }
  function renderQueue() {
    const first = Q.ids[0], fp = first ? paperOf(S.epPaper.get(first)) || {} : null, more = Q.ids.length - 1;
    const pn = $("p-next");
    pn.hidden = !first;
    if (first) {
      titleInto($("p-next-t"), fp);
      $("p-next-n").textContent = more > 0 ? `${more} more` : "";
      pn.setAttribute("aria-label", `Up next: ${displayTitle(fp) || "a paper"}${more > 0 ? `, and ${more} more` : ""}`);
    }
    if (!$("q-overlay").hidden) drawQueue();
  }
  function drawQueue(focus) {
    if (Q.dragging) return;
    const now = S.audioEp && A().getAttribute("src") ? epById(S.audioEp) : null;
    const np = now ? paperOf(S.epPaper.get(now.id)) || {} : null;
    $("q-now").hidden = !now;
    if (now) $("q-now").textContent = `${isPlaying(now.id) ? "Playing" : "Paused"}: ${displayTitle(np) || "a paper"}`;
    $("q-empty").hidden = Q.ids.length > 0;
    $("q-clear").disabled = !Q.ids.length;
    const list = $("q-list");
    // Drawn again only when a row changed; the focus stays on its row (the button it was on,
    // else the row itself).
    const sig = JSON.stringify(Q.ids.map((eid) => { const e = epById(eid), p = paperOf(S.epPaper.get(eid)) || {}; return [eid, displayTitle(p), e && whoLine(e), e && stateLine(e).text]; }));
    if (!focus && list.dataset.sig === sig) return;
    const had = document.activeElement && document.activeElement.closest && document.activeElement.closest("#q-list .q-row");
    if (!focus && had) focus = { eid: had.dataset.ep, act: document.activeElement.dataset.act };
    list.dataset.sig = sig;
    list.replaceChildren(...Q.ids.map((eid, i) => queueRow(eid, i)));
    if (focus) {
      const li = list.querySelector(`.q-row[data-ep="${focus.eid}"]`);
      const b = li && (li.querySelector(`[data-act="${focus.act}"]:not(:disabled)`) || li.querySelector(".q-main"));
      if (b) b.focus();
    }
  }
  function queueRow(eid, i) {
    const e = epById(eid), p = paperOf(S.epPaper.get(eid)) || {};
    const name = displayTitle(p) || "this paper";
    const t = el("span", { class: "q-t" });
    titleInto(t, p);
    const st = e ? stateLine(e) : { text: "" };
    const move = (to, act) => () => { qMove(eid, to); drawQueue({ eid, act }); };
    const btn = (act, label, html, fn, off) => el("button", { type: "button", class: `icon-btn q-${act}`, "data-act": act, "aria-label": label, title: label, html, disabled: off, onclick: fn });
    return el("li", { class: "q-row", "data-ep": eid },
      el("span", { class: "q-grip", "aria-hidden": "true", title: "Drag to move", html: PI.grip, onpointerdown: (ev) => dragRow(ev, eid) }),
      el("button", { type: "button", class: "q-main", "data-act": "main", "aria-label": `Play ${name} now`,
        onclick: () => { if (e && e.has_audio) { qRemove([eid]); playEp(eid); } } },
      t, el("span", { class: "q-s t", text: [e ? whoLine(e) : "", st.text].filter(Boolean).join(" · ") })),
      btn("up", `Move ${name} up`, PI.up, move(i - 1, "up"), i === 0),
      btn("down", `Move ${name} down`, PI.down, move(i + 1, "down"), i === Q.ids.length - 1),
      btn("del", `Remove ${name} from Up next`, I.close, () => qRemove([eid])));
  }
  function dragRow(ev, eid) {
    if (ev.button) return;
    const grip = ev.currentTarget, li = grip.closest(".q-row"), list = $("q-list");
    ev.preventDefault();
    try { grip.setPointerCapture(ev.pointerId); } catch (x) { /* already gone */ }
    Q.dragging = true;
    li.classList.add("drag");
    const from = [...list.children].indexOf(li);
    let y0 = ev.clientY / zoom();
    // Past half of a neighbour, the row takes its place; it follows the pointer in between.
    const move = (e) => {
      if (e.pointerId !== ev.pointerId) return;
      const y = e.clientY / zoom(), prev = li.previousElementSibling, next = li.nextElementSibling, dy = y - y0;
      if (next && dy > next.offsetHeight / 2) { list.insertBefore(next, li); y0 += next.offsetHeight; }
      else if (prev && dy < -prev.offsetHeight / 2) { list.insertBefore(li, prev); y0 -= prev.offsetHeight; }
      li.style.transform = `translateY(${y - y0}px)`;
    };
    const done = (e) => {
      if (e.pointerId !== ev.pointerId) return;
      for (const [k, f] of [["pointermove", move], ["pointerup", done], ["pointercancel", done]]) grip.removeEventListener(k, f);
      li.classList.remove("drag");
      li.style.transform = "";
      Q.dragging = false;
      const to = [...list.children].indexOf(li);
      if (e.type === "pointerup" && to !== from) qMove(eid, to); else drawQueue();
    };
    grip.addEventListener("pointermove", move);
    grip.addEventListener("pointerup", done);
    grip.addEventListener("pointercancel", done);
  }
  // The hub tells this person's other tabs and devices of a change; a version deleted, or a
  // paper changed, that the queue holds reads the queue again (the hub leaves the deleted out).
  function queueEvents(es) {
    const soon = () => { clearTimeout(Q.timer); Q.timer = setTimeout(loadQueue, 200); };
    es.addEventListener("queue", (e) => {
      if (e.lastEventId) S.lastId = e.lastEventId;
      let d = {};
      try { d = JSON.parse(e.data) || {}; } catch (x) { /* junk */ }
      if (!(typeof d.rev === "number" && d.rev <= Q.rev)) soon();
    });
    es.addEventListener("resync", soon);
    for (const kind of ["episode", "paper"]) {
      es.addEventListener(kind, (e) => {
        let d = {};
        try { d = JSON.parse(e.data) || {}; } catch (x) { return; }
        const eid = d.episode_id || (kind === "episode" ? d.id : null), pid = d.paper_id || (kind === "paper" ? d.id : null);
        if ((eid && queued(eid)) || (pid && Q.ids.some((x) => S.epPaper.get(x) === pid))) soon();
      });
    }
  }
  function wireQueue() {
    const a = A();
    $("q-close").innerHTML = I.close;
    $("p-next").insertAdjacentHTML("afterbegin", PI.queue);
    $("p-next").addEventListener("click", openQueue);
    $("q-close").addEventListener("click", closeQueue);
    $("q-clear").addEventListener("click", qClear);
    $("q-overlay").addEventListener("click", (e) => { if (e.target === $("q-overlay")) closeQueue(); });
    document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !$("q-overlay").hidden && !S.menu) closeQueue(); });
    a.addEventListener("ended", advance);
    a.addEventListener("playing", () => { if (S.audioEp && queued(S.audioEp)) qRemove([S.audioEp]); });
    for (const ev of ["play", "pause", "emptied"]) a.addEventListener(ev, () => { if (!$("q-overlay").hidden) drawQueue(); });
  }

  // ------------------------------------------------------------------ transcript
  // The window's version's script as plain headings and paragraphs (text, never markup), each
  // sentence tied to its time: the voice worker's timings, else the hub's estimate (stretched to
  // the audio's real length). The sentence being spoken is marked. While this paper plays, the
  // window rolls with it: the sentence stays about a third of the way down. Once the person
  // scrolls away it is left alone until "Back to now", Play, a tap on a sentence, or their own
  // scroll back to it. A tap on a sentence plays from there.
  const T = { key: null, eid: null, data: null, parts: [], texts: [], bySeg: new Map(), rep: [], cur: -1,
    follow: true, autoUntil: 0, req: 0, q: "", hits: [], hit: -1, marked: [], findTimer: null };
  function renderTranscript(c) {
    $("tr-body").classList.toggle("live", !!(c && c.has_audio));
    const key = c ? `${c.id}:${!!c.has_audio}:${(c.voice && c.voice.rev) || 1}` : null;   // voiced (again) since: new timings, maybe a new script
    if (key === T.key) { trTick(); return; }
    T.key = key; T.eid = c ? c.id : null;
    T.data = null; T.parts = []; T.texts = []; T.bySeg = new Map(); T.rep = []; T.cur = -1; T.follow = true;
    T.hits = []; T.hit = -1; T.marked = [];
    $("tr-body").replaceChildren();
    $("tr").hidden = true;
    $("paper").classList.remove("has-tr");
    $("tr-now").hidden = true;
    $("tr-count").textContent = "";
    if (!c) return;
    const n = ++T.req, eid = c.id;
    api("GET", `/api/episodes/${eid}/transcript`).then((j) => { if (n === T.req) drawTranscript(j); }, () => { /* none: the section stays hidden */ });
  }
  function drawTranscript(j) {
    const blocks = (j && j.blocks) || [];
    if (!blocks.length) return;
    const segs = Array.isArray(j.segments) ? j.segments : [];
    T.data = { segments: segs, estimated: !!j.estimated, duration_s: j.duration_s };
    const kids = [];
    for (const b of blocks) {
      const node = el(b.kind === "heading" ? "h4" : "p", { class: b.kind === "heading" ? "tr-hd" : "tr-p" });
      (b.parts || []).forEach((pt, i) => {
        if (i) node.append(" ");
        const timed = Number.isInteger(pt.seg) && pt.seg >= 0 && pt.seg < segs.length;
        const s = el("span", { class: timed ? "tr-s" : "tr-x", text: String(pt.text) });
        if (timed) {
          s.dataset.seg = String(pt.seg);
          if (!T.bySeg.has(pt.seg)) T.bySeg.set(pt.seg, []);
          T.bySeg.get(pt.seg).push(s);
        }
        T.parts.push(s); T.texts.push(String(pt.text));
        node.append(s);
      });
      kids.push(node);
    }
    $("tr-body").replaceChildren(...kids);
    // A segment with no words of its own shows as the one before it.
    let last = -1;
    T.rep = segs.map((_, i) => (T.bySeg.has(i) ? (last = i) : last));
    $("tr-note").hidden = !T.data.estimated;
    $("tr").hidden = false;
    $("paper").classList.add("has-tr");
    if ($("tr-q").value.trim()) trFind(false, true);
    trTick();
    trNowBtn();
  }
  // Estimated timings are shares of the length the hub knew; the audio's own length wins.
  function trScale(e) {
    const d = T.data;
    if (!d || !d.estimated || !(d.duration_s > 0)) return 1;
    const real = durOf(e);
    return real > 0 ? real / d.duration_s : 1;
  }
  function segAt(t) {
    const s = T.data.segments;
    let lo = 0, hi = s.length - 1, at = -1;
    while (lo <= hi) {
      const m = (lo + hi) >> 1;
      if (s[m].start <= t + 0.01) { at = m; lo = m + 1; } else hi = m - 1;
    }
    return at;
  }
  function trTick() {
    if (!T.data) return;
    const e = openEp();
    if (!e || e.id !== T.eid || !e.has_audio) { setCur(-1); return; }
    setCur(segAt(posOf(e) / trScale(e)));
  }
  const curEls = () => (T.cur >= 0 && T.bySeg.get(T.rep[T.cur])) || [];
  const following = () => T.follow && isPlaying(T.eid);
  const trShown = () => S.view === "paper" && !$("tr").hidden && $("tr").getClientRects().length > 0;
  function setCur(i) {
    if (i === T.cur) return;
    for (const s of curEls()) s.classList.remove("now");
    T.cur = i;
    for (const s of curEls()) s.classList.add("now");
    if (following()) keepInView(false);
    trNowBtn();
  }
  // What scrolls the transcript: the middle column where the comments have a column of their
  // own (app.css), else the window.
  function trScroller() {
    const m = $("p-mid");
    return getComputedStyle(m).overflowY === "auto" ? m : $("win");
  }
  // The part of it a sentence is seen in: under the phone's top bar, which stays.
  function trView() {
    const r = box(trScroller()), pt = document.querySelector("#paper > .phone-top");
    const top = pt && pt.getClientRects().length && getComputedStyle(pt).position === "sticky" ? Math.max(r.top, box(pt).bottom) : r.top;
    return { top, bottom: r.bottom, h: Math.max(1, r.bottom - top) };
  }
  function curVisible() {
    const els = curEls();
    if (!els.length || !trShown()) return false;
    const v = trView(), a = box(els[0]), b = box(els[els.length - 1]);
    return b.bottom > v.top + 8 && a.top < v.bottom - 8;
  }
  const motion = () => (window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth");
  function scrollWinTo(node, at) {
    const w = trScroller(), v = trView(), r = box(node);
    T.autoUntil = Date.now() + 1000;        // this scroll is the page's own, not the person's
    w.scrollTo({ top: Math.max(0, w.scrollTop + r.top - (v.top + v.h * at)), behavior: motion() });
  }
  function keepInView(force) {
    const els = curEls();
    if (!els.length || !trShown()) return;
    const v = trView(), r = box(els[0]);
    if (!force && r.top >= v.top + v.h * 0.22 && r.top <= v.top + v.h * 0.42 && r.bottom <= v.bottom - 8) return;
    scrollWinTo(els[0], 0.32);
  }
  function trNowBtn() {
    $("tr-now").hidden = !(T.data && T.cur >= 0 && !following() && !curVisible());
  }
  function playFrom(e, t) {
    const a = A();
    loadAudio(e.id, (au) => {
      S.dirty = true;
      au.currentTime = Math.max(0, Math.min((au.duration || durOf(e) || t + 1) - 0.1, t));
      savePosition(true); renderPlayer(); refresh(S.epPaper.get(e.id)); trTick();
    });
    if (a.paused || a.ended) a.play().catch((x) => { if (x.name !== "AbortError") toast(`Cannot play: ${x.message}`); });
  }

  // Find: every match marked, one of them the current; Enter and the arrows go through them.
  function trFind(go, keep) {
    const q = $("tr-q").value.trim();
    const was = T.hit;
    T.q = q;
    for (const i of T.marked) T.parts[i].textContent = T.texts[i];
    T.marked = []; T.hits = []; T.hit = -1;
    if (q.length >= 2) {
      const rx = new RegExp(q.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"), "giu");
      T.parts.forEach((s, i) => {
        const t = T.texts[i];
        let m, from = 0, kids = null;
        rx.lastIndex = 0;
        while ((m = rx.exec(t))) {
          kids = kids || [];
          if (m.index > from) kids.push(t.slice(from, m.index));
          const mk = el("mark", { class: "tr-m", text: m[0] });
          kids.push(mk); T.hits.push(mk);
          from = m.index + m[0].length;
        }
        if (kids) { if (from < t.length) kids.push(t.slice(from)); s.replaceChildren(...kids); T.marked.push(i); }
      });
    }
    const n = T.hits.length;
    $("tr-count").textContent = q.length >= 2 && !n ? "No matches" : "";
    $("tr-prev").disabled = $("tr-next").disabled = !n;
    if (!n) return;
    if (keep) { goHit(Math.max(0, Math.min(n - 1, was)), true); return; }
    // the first match at or under the top of what is on screen
    const top = trView().top, i = T.hits.findIndex((mk) => box(mk).bottom > top);
    goHit(i < 0 ? 0 : i, !go);
  }
  function goHit(i, quiet) {
    const n = T.hits.length;
    if (!n) return;
    i = ((i % n) + n) % n;
    if (T.hits[T.hit]) T.hits[T.hit].classList.remove("on");
    T.hit = i;
    T.hits[i].classList.add("on");
    $("tr-count").textContent = `${i + 1} of ${n}`;
    if (quiet) return;
    const v = trView(), r = box(T.hits[i]);
    if (r.top < v.top + 8 || r.bottom > v.bottom - 64) { T.follow = false; scrollWinTo(T.hits[i], 0.35); }
    trNowBtn();
  }
  function wireTranscript() {
    const w = $("win"), a = A(), q = $("tr-q");
    $("tr-prev").innerHTML = PI.prev; $("tr-next").innerHTML = PI.next;
    // What the person does to scroll ends the page's own scroll at once (the comments column
    // scrolls on its own, and does not count).
    const theirs = (e) => { if (e && e.target && e.target.closest && e.target.closest("#comments, #bar")) return; T.autoUntil = 0; };
    for (const ev of ["wheel", "touchstart", "pointerdown"]) w.addEventListener(ev, theirs, { passive: true });
    document.addEventListener("keydown", (e) => { if (/^(Arrow|Page|Home|End| )/.test(e.key)) theirs(e); });
    // Scrolled by the person: the page follows the sentence again once they can see it.
    const scrolled = () => {
      if (!T.data) return;
      if (Date.now() >= T.autoUntil) T.follow = curVisible();
      trNowBtn();
    };
    for (const x of [w, $("p-mid")]) x.addEventListener("scroll", scrolled, { passive: true });
    for (const ev of ["timeupdate", "seeked", "loadedmetadata", "durationchange", "emptied"]) a.addEventListener(ev, trTick);
    // Play follows the sentence again.
    a.addEventListener("play", () => { if (S.audioEp !== T.eid) return; T.follow = true; trTick(); keepInView(false); trNowBtn(); });
    a.addEventListener("pause", trNowBtn);
    $("tr-body").addEventListener("click", (ev) => {
      const s = ev.target.closest(".tr-s");
      if (!s || !T.data) return;
      const sel = window.getSelection && window.getSelection();
      if (sel && !sel.isCollapsed && String(sel).trim()) return;      // selecting words, not a tap
      const e = openEp(), seg = T.data.segments[Number(s.dataset.seg)];
      if (!e || e.id !== T.eid || !e.has_audio || !seg) return;
      T.follow = true;
      playFrom(e, seg.start * trScale(e));
    });
    $("tr-now").addEventListener("click", () => { T.follow = true; $("tr-now").hidden = true; keepInView(true); });
    q.addEventListener("input", () => { S.typedAt = Date.now(); clearTimeout(T.findTimer); T.findTimer = setTimeout(() => trFind(true), 150); });
    q.addEventListener("keydown", (e) => {
      if (e.key === "Enter") {
        e.preventDefault();
        clearTimeout(T.findTimer);
        if (T.q !== q.value.trim()) trFind(true); else goHit(T.hit + (e.shiftKey ? -1 : 1));
      } else if (e.key === "Escape" && q.value) { e.preventDefault(); e.stopPropagation(); q.value = ""; trFind(false); }
    });
    $("tr-prev").addEventListener("click", () => goHit(T.hit - 1));
    $("tr-next").addEventListener("click", () => goHit(T.hit + 1));
  }

  // ------------------------------------------------------------------ explainer overlay
  function openExplainer() {
    const e = openEp();
    if (!e || !e.has_explainer) return;
    S.lastFocus = document.activeElement;
    $("x-frame").src = `/x/${e.id}/explainer.html`;
    $("overlay").hidden = false;
    $("x-close").focus();
  }
  function closeExplainer() {
    if ($("overlay").hidden) return;
    $("overlay").hidden = true;
    $("x-frame").src = "about:blank";
    if (S.lastFocus && S.lastFocus.focus) S.lastFocus.focus();
  }

  // ------------------------------------------------------------------ comments and the board
  // social.js (window.PaperSocial) draws them; this is what it may use of the page.
  function socialCtx() {
    return {
      api, me: () => S.me, isAdmin, phone, toast, hms, avatar: avatarNode,
      typed: () => { S.typedAt = Date.now(); },
      openPaper: (pid) => openPaper(pid),
      rowChanged: (pid) => { refresh(pid); if (pid === S.open) renderTabs(); },
      showComments: () => setPane("c"),
      // the open paper's version in the player, and where it is
      position: () => { const e = openEp(); return e && e.has_audio ? { eid: e.id, s: posOf(e) } : null; },
      seek: seekPlay,
      openGraph: (gid) => openGraph(gid),
    };
  }

  // ------------------------------------------------------------------ the graph list
  // graphs.js (window.PaperGraphs): the home column, and a phone's list of a graph's papers.
  function graphsCtx() {
    return {
      api, me: () => S.me, phone, toast, openMenu, closeMenu, paperOf, known,
      authors: (p) => shortAuthors(p || {}),
      typed: () => { S.typedAt = Date.now(); },
      openGraph: (gid) => openGraph(gid),
      openPaper: (pid, gid) => openPaper(pid, gid),
      // a graph just made: the list again, and the graph
      created: (gid) => { if (S.map) S.map.refreshList(); openGraph(gid); },
      // "/" may take the keyboard to the search
      free: () => $("overlay").hidden && $("q-overlay").hidden && !(phone() && document.body.classList.contains("open")),
    };
  }

  // ------------------------------------------------------------------ the tour and "?"
  // tour.js (window.PaperTour): the tour a new person gets, Tour again, and the help panel.
  function tourCtx() {
    return {
      api, me: () => S.me, cfg: () => S.cfg, phone,
      view: () => (S.phoneMap ? "map" : S.view),
      openId: () => S.open,
      // the graph's papers first (the map's order), then the rest of the library, newest first
      papers: () => {
        const d = S.map && S.graph ? S.map.data(S.graph) : null, ids = d ? d.nodes.map((n) => n.id) : [];
        const first = ids.map((id) => S.papers.get(id)).filter(Boolean);
        const rest = [...S.papers.values()].filter((p) => !ids.includes(p.id)).sort((a, b) => (b.added_at || "").localeCompare(a.added_at || ""));
        return first.concat(rest);
      },
      hasAudio: anyAudio,
      // a paper opened without a step in the history
      open: (pid) => { history.replaceState(history.state, "", `#${S.graph && inGraph(S.graph, pid) ? hashFor(S.graph, pid) : `p=${pid}`}`); openFromHash(); },
      list: () => { closeMenu(); closeQueue(); if (S.view !== "home" || S.open || phone()) goHome(); },
      openMap, closeMap: () => closeMap(), closeMenu, map: () => S.map,
      go: (h) => nav(String(h || "").replace(/^#/, ""), false),        // back to an address, without a step
      pane: (k) => setPane(k),
      // nothing over the page, and the page has landed (the graph open last): the tour starts from where it stays
      idle: () => S.landed && $("overlay").hidden && $("q-overlay").hidden && !S.menu && document.visibilityState === "visible",
    };
  }

  // A time in a comment: that version plays from there. It becomes the version this paper plays
  // here (as a tap on it in Versions would); play() is called in the tap itself, so a phone lets it.
  function seekPlay(eid, t) {
    const e = epById(eid), pid = S.epPaper.get(eid), p = S.papers.get(pid);
    if (!e || !e.has_audio || !p) return;
    if (chosen(p) !== e) { store.set(mine(`pick.${pid}`), eid); if (S.open === pid) renderWin(); refresh(pid); }
    const a = A();
    loadAudio(eid, (x) => {
      S.dirty = true;
      x.currentTime = Math.max(0, Math.min(x.duration || durOf(e) || 0, t));
      savePosition(true); renderPlayer(); refresh(pid);
    });
    a.play().catch((x) => { if (x.name !== "AbortError") toast(`Cannot play: ${x.message}`); });
  }

  // ------------------------------------------------------------------ menus
  function closeMenu() {
    if (!S.menu) return;
    S.menu.node.remove();
    if (S.menu.anchor) S.menu.anchor.setAttribute("aria-expanded", "false");
    S.menu = null;
  }
  function openMenu(anchor, key, items) {
    const same = S.menu && S.menu.for === key;
    closeMenu();
    if (same || !items.length) return;
    anchor.setAttribute("aria-expanded", "true");
    const node = el("div", { class: "menu", role: "menu" }, items);
    document.body.append(node);
    const r = box(anchor), mw = node.offsetWidth, mh = node.offsetHeight, vw = window.innerWidth / zoom(), vh = window.innerHeight / zoom();
    const top = r.bottom + 4 + mh > vh - 8 ? Math.max(8, r.top - 4 - mh) : r.bottom + 4;
    node.style.top = `${top}px`;
    node.style.left = `${Math.max(8, Math.min(vw - mw - 8, r.right - mw))}px`;
    S.menu = { node, anchor, for: key };
    const first = node.querySelector("button, a");
    if (first) first.focus({ preventScroll: true });
  }
  const mItem = (label, fn, cls) => el("button", { type: "button", role: "menuitem", class: cls || null, text: label, onclick: () => { closeMenu(); fn(); } });
  const delLabel = (e) => (e.mine ? "Delete my version" : `Delete ${makerName(e)}’s version`);
  function rowMenu(p) {
    const c = chosen(p);
    return queueItems(c).concat(c && c.can_delete ? [mItem(delLabel(c), () => deleteVersion(c.id), "danger")] : []);
  }
  function paperLink(p) {
    if (p.url && /^https?:\/\//i.test(p.url)) return p.url;
    if (p.arxiv_id && /^[\w./-]+$/.test(p.arxiv_id)) return `https://arxiv.org/abs/${p.arxiv_id}`;
    if (p.doi && /^10\.\S+$/.test(p.doi)) return `https://doi.org/${p.doi}`;
    return "";
  }
  function winMenu() {
    const p = S.papers.get(S.open);
    if (!p) return [];
    const c = chosen(p);
    const items = queueItems(c).concat([mItem(S.details ? "Hide details" : "Details", () => { S.details = !S.details; renderWin(); })]);
    const link = paperLink(p);
    if (link) items.push(el("a", { role: "menuitem", href: link, target: "_blank", rel: "noopener noreferrer", text: "Open paper link", onclick: closeMenu }));
    if (c && c.can_delete) items.push(mItem(delLabel(c), () => deleteVersion(c.id), "danger"));
    return items;
  }

  // ------------------------------------------------------------------ settings
  // Preferences (how this person's own versions are made), Voice (who reads them: those who
  // make versions), Devices (where papercast is logged in as them), and for admins Users, the
  // Base prompt and Slack. Each tab reads the hub when opened.
  const TABS = [["prefs", "Preferences"], ["voice", "Voice", false, false, true], ["devices", "Devices"], ["account", "Account", false, true], ["users", "Users", true], ["base", "Base prompt", true], ["slack", "Slack", true]];
  const PREF_TEXT = {
    maths: ["Maths", { words: "In words", "key-steps": "Key steps", full: "Full derivations" }, ""],
    emphasis: ["What gets more time", { balanced: "Balanced", theory: "Theory", method: "Method", practice: "Practice" }, ""],
    background: ["What the listener already knows", { newcomer: "New to the field", field: "Works in the field", specialist: "Specialist" }, ""],
  };
  const tabsFor = () => TABS.filter((t) => (!t[2] || isAdmin()) && (!t[3] || passwordMode()) && (!t[4] || makesVersions()));
  function openSettings(which) {
    S.open = null;
    closeExplainer(); closeMenu();
    const ok = tabsFor().map((t) => t[0]);
    S.setTab = ok.includes(which) ? which : ok.includes(S.setTab) ? S.setTab : "prefs";
    showView("settings");
    document.title = "Settings · Papers";
    refresh();
    renderSettings();
    $("win").scrollTop = 0;
  }
  function settingsDirty() {
    if (S.view !== "settings") return false;        // left, the draft is gone anyway
    const pr = S.set.prefs;
    return !!((pr && pr.draft && JSON.stringify(pr.draft) !== JSON.stringify({ settings: pr.saved.settings, note: pr.saved.note }))
      || (S.set.base && S.set.base.editing));
  }
  function renderSettings() {
    const me = S.me || {};
    $("set-who").textContent = [me.name, me.email, me.role].filter(Boolean).join(" · ");
    // Behind Cloudflare Access, signing out is Access's own page (it ends the Access session).
    if (S.cfg && S.cfg.auth === "cf-access" && !$("set-signout")) {
      $("set-who").after(el("p", { class: "muted" }, el("a", { id: "set-signout", href: "/cdn-cgi/access/logout", text: "Sign out" })));
    }
    $("set-tabs").replaceChildren(...tabsFor().map(([id, label]) => el("button", {
      type: "button", class: "tab", role: "tab", "aria-selected": String(id === S.setTab), id: `tab-${id}`,
      onclick: () => { location.hash = `settings=${id}`; },
    }, label)));
    const body = $("set-body");
    body.replaceChildren(el("p", { class: "muted intro", id: "set-loading", text: "Loading…" }));
    ({ prefs: prefsTab, voice: voiceTab, devices: devicesTab, account: accountTab, users: passwordMode() ? peopleTab : usersTab, base: baseTab, slack: slackTab })[S.setTab](body);
  }
  const stillOn = (t) => S.view === "settings" && S.setTab === t;
  const failed = (body, e) => body.replaceChildren(el("p", { class: "err", text: e.message }));
  // Answers whose shape is another module's: a list, or an object holding one.
  const listOf = (j, ...keys) => (Array.isArray(j) ? j : (keys.map((k) => j && j[k]).find(Array.isArray) || []));

  async function prefsTab(body) {
    let j;
    try { j = await api("GET", "/api/prefs"); } catch (e) { if (stillOn("prefs")) failed(body, e); return; }
    if (!stillOn("prefs")) return;
    const st = S.set.prefs = { saved: j, draft: { settings: Object.assign({}, j.settings), note: j.note || "" } };
    const sumLine = el("p", { class: "muted", id: "pref-summary" });
    const msg = el("span", { class: "ok", id: "pref-msg", role: "status" });
    const save = el("button", { type: "button", class: "btn-accent", id: "pref-save", text: "Save", disabled: true });
    const dirty = () => { save.disabled = !settingsDirty(); if (!save.disabled) { msg.textContent = ""; msg.className = "ok"; } };
    const summary = () => {
      const s = st.saved.summary;
      sumLine.textContent = `Your versions show as “by ${S.me.name || "you"}${s ? ` · ${s}` : ""}”.`;
    };
    const groups = Object.keys(j.choices || {}).map((k) => {
      const [label, names, help] = PREF_TEXT[k] || [k, {}, ""];
      const seg = el("div", { class: "seg", role: "radiogroup", "aria-label": label });
      const draw = () => seg.replaceChildren(...j.choices[k].map((v) => el("button", {
        type: "button", role: "radio", "aria-checked": String(st.draft.settings[k] === v), "data-v": v,
        onclick: () => { st.draft.settings[k] = v; draw(); dirty(); },
      }, names[v] || v)));
      draw();
      return el("div", { class: "pref", "data-k": k }, el("p", { class: "pref-h", text: label }), seg,
        help ? el("p", { class: "muted", text: help }) : null);
    });
    const max = j.note_max || 500;
    const note = el("textarea", { class: "field note", id: "pref-note", maxlength: String(max), rows: "4",
      placeholder: "Anything else, in a sentence or two", "aria-label": "A note for the agent" });
    note.value = st.draft.note;
    const count = el("div", { class: "counter t", text: `${note.value.length} / ${max}` });
    note.addEventListener("input", () => { st.draft.note = note.value; count.textContent = `${note.value.length} / ${max}`; S.typedAt = Date.now(); dirty(); });
    save.addEventListener("click", async () => {
      save.disabled = true;
      try {
        const r = await api("PUT", "/api/prefs", { settings: st.draft.settings, note: st.draft.note });
        st.saved = r;
        st.draft = { settings: Object.assign({}, r.settings), note: r.note || "" };
        if (note.value !== st.draft.note) note.value = st.draft.note;
        msg.className = "ok"; msg.textContent = "Saved";
        summary();
      } catch (e) {
        msg.className = "err"; msg.textContent = e.message;
        save.disabled = false;
      }
    });
    summary();
    body.replaceChildren(
      ...groups,
      el("div", { class: "pref" }, el("p", { class: "pref-h", text: "Note" }), note, count),
      sumLine,
      el("div", { class: "save-row" }, save, msg),
      ...(passwordMode() ? [] : listeningSwitch()));        // with passwords it is in Account
    slackPref(body);
  }

  // Whether the group sees your listening (hub/listening.py): off until you turn it on, saved at
  // once; the one line on what is kept (it must say what hub/listening.py does: RETAIN_DAYS); and
  // deleting all of yours, two presses as with Sign out everywhere.
  const LS_NOTE = "Records minutes and episodes per day. Only you see them unless this is on. Kept 12 months.";
  function listeningSwitch() {
    const sw = el("button", { type: "button", class: "sw-row", id: "ls-shown", role: "switch", "aria-checked": "false", disabled: true },
      el("span", { class: "sw", "aria-hidden": "true" }), el("span", { text: "Show mine to the group" }));
    const msg = el("span", { class: "ok", id: "ls-shown-msg", role: "status" });
    const DEL = "Delete my listening history";
    const del = el("button", { type: "button", class: "text-btn danger", id: "ls-delete", text: DEL });
    const delMsg = el("span", { class: "ok", id: "ls-delete-msg", role: "status" });
    let on = false;
    const draw = () => sw.setAttribute("aria-checked", String(on));
    api("GET", `/api/listening/me?tz=${new Date().getTimezoneOffset()}`).then((j) => { on = j.shown === true; draw(); sw.disabled = false; }, (e) => { msg.className = "err"; msg.textContent = e.message; });
    sw.addEventListener("click", async () => {
      const want = !on;
      on = want; draw(); sw.disabled = true; msg.className = "ok"; msg.textContent = ""; delMsg.textContent = "";
      try { on = (await api("PUT", "/api/me/listening-visibility", { shown: want })).shown; draw(); msg.textContent = "Saved"; }
      catch (e) { on = !want; draw(); msg.className = "err"; msg.textContent = e.message; }
      sw.disabled = false;
    });
    let armed = false;
    del.addEventListener("click", async () => {
      delMsg.className = "ok"; delMsg.textContent = "";
      if (!armed) { armed = true; del.textContent = "Delete now"; return; }
      del.disabled = true;
      try { await api("DELETE", "/api/me/listening"); on = false; draw(); msg.textContent = ""; delMsg.textContent = "Deleted"; }
      catch (e) { delMsg.className = "err"; delMsg.textContent = e.message; }
      armed = false; del.textContent = DEL; del.disabled = false;
    });
    return [el("p", { class: "pref-h sec", text: "Listening" }), el("div", { class: "save-row sw-line" }, sw, msg),
      el("p", { class: "muted ls-note", id: "ls-note", text: LS_NOTE }),
      el("div", { class: "save-row ls-del" }, del, delMsg)];
  }

  // papercast add's Slack question (hub/slack.py): this person's answer on Enter, saved at once.
  // Shown when the hub has Slack set up, to those who can add papers.
  async function slackPref(body) {
    let j;
    try { j = await api("GET", "/api/slack"); } catch (e) { return; }
    if (!stillOn("prefs") || !j.enabled || !j.can_upload) return;
    const old = $("pref-slack");
    if (old) old.remove();
    const msg = el("span", { class: "ok", id: "slack-msg", role: "status" });
    const seg = el("div", { class: "seg", role: "radiogroup", "aria-label": `Post to ${j.channel}` });
    const draw = () => seg.replaceChildren(...[[true, "Yes"], [false, "No"]].map(([v, label]) => el("button", {
      type: "button", role: "radio", "aria-checked": String(j.default === v), "data-v": String(v),
      onclick: async () => {
        if (j.default === v) return;
        const was = j.default;
        j.default = v; draw(); msg.className = "ok"; msg.textContent = "";
        try { const r = await api("PUT", "/api/slack", { default: v }); j.default = r.default; draw(); msg.textContent = "Saved"; }
        catch (e) { j.default = was; draw(); msg.className = "err"; msg.textContent = e.message; }
      },
    }, label)));
    draw();
    body.append(el("div", { class: "pref", "data-k": "slack", id: "pref-slack" },
      el("p", { class: "pref-h sec", text: `Post my new episodes to ${j.channel}` }), seg,
      msg));
  }

  async function devicesTab(body) {
    let j;
    try { j = await api("GET", "/api/tokens"); } catch (e) { if (stillOn("devices")) failed(body, e); return; }
    if (!stillOn("devices")) return;
    const live = listOf(j, "tokens", "devices").filter((t) => !t.revoked_at);
    const ul = el("ul", { class: "items", id: "devices" });
    const draw = () => {
      ul.replaceChildren(...live.map((t) => {
        let armed = false;
        const b = el("button", { type: "button", class: "text-btn danger", text: "Revoke", "aria-label": `Revoke ${t.name || "this device"}` });
        b.addEventListener("click", async () => {
          if (!armed) { armed = true; b.textContent = "Revoke now"; return; }
          b.disabled = true;
          try {
            await api("DELETE", `/api/tokens/${encodeURIComponent(t.id)}`);
            live.splice(live.indexOf(t), 1);
            draw();
            toast(`Revoked ${t.name || "the device"}`);
          } catch (e) { b.disabled = false; toast(e.message); }
        });
        return el("li", { class: "item", "data-id": String(t.id) },
          el("div", { class: "it-main" }, el("div", { class: "it-t", text: t.name || "A device" }),
            el("div", { class: "it-s", text: [t.created_at ? `added ${day(t.created_at)}` : "", t.last_used_at ? `last used ${when(t.last_used_at)}` : "never used"].filter(Boolean).join(" · ") })),
          b);
      }));
      if (!live.length) ul.replaceChildren(el("li", { class: "muted intro", text: "None yet." }));
    };
    draw();
    body.replaceChildren(ul);
  }

  async function usersTab(body) {
    let j;
    try { j = await api("GET", "/api/admin/users"); } catch (e) { if (stillOn("users")) failed(body, e); return; }
    if (!stillOn("users")) return;
    const users = listOf(j, "users");
    const ROLES = ["viewer", "contributor", "admin"];
    const put = async (u, change, undo) => {
      try {
        const r = await api("PUT", `/api/admin/users/${encodeURIComponent(u.id)}`, change);
        Object.assign(u, change, r && r.id !== undefined ? r : {});
        toast(`Saved ${u.name || u.email}`, null, 3000);
      } catch (e) { undo(); toast(e.message); }
    };
    const ul = el("ul", { class: "items", id: "users" }, users.map((u) => {
      const role = el("select", { class: "pick", "aria-label": `Role of ${u.name || u.email}` },
        ROLES.map((r) => el("option", { value: r, text: r[0].toUpperCase() + r.slice(1), selected: u.role === r })));
      role.addEventListener("change", () => { const was = u.role; put(u, { role: role.value }, () => { role.value = was; }); });
      const dis = el("button", { type: "button", class: "lbox", role: "checkbox", "aria-checked": String(!!u.disabled),
        "aria-label": `${u.name || u.email} disabled` }, el("span", { class: "box", "aria-hidden": "true", html: u.disabled ? I.check : "" }), el("span", { text: "Disabled" }));
      dis.addEventListener("click", () => {
        const to = !u.disabled;
        const show = (v) => { dis.setAttribute("aria-checked", String(v)); dis.firstChild.innerHTML = v ? I.check : ""; };
        show(to);
        put(u, { disabled: to }, () => show(!to));
      });
      const pic = u.id !== S.me.id && hasPicture(u) ? el("button", { type: "button", class: "text-btn", text: "Remove picture",
        "aria-label": `Remove the picture of ${u.name || u.email}`, onclick: (e) => { e.currentTarget.remove(); removePicture(u); } }) : null;
      return el("li", { class: "item", "data-id": String(u.id) }, avatarNode(u, "m"),
        el("div", { class: "it-main" }, el("div", { class: "it-t", text: `${u.name || u.email}${u.id === S.me.id ? " (you)" : ""}` }),
          el("div", { class: "it-s", text: [u.email, u.created_at ? `since ${day(u.created_at)}` : ""].filter(Boolean).join(" · ") })),
        el("div", { class: "it-ctl" }, role, dis, pic));
    }));
    const kids = [ul];
    // Invite links are the local sign-in's (with Cloudflare Access, people sign in by email).
    if (S.cfg && S.cfg.auth === "local") {
      const irole = el("select", { class: "pick", "aria-label": "Role for the invite" },
        ROLES.map((r) => el("option", { value: r, text: r[0].toUpperCase() + r.slice(1), selected: r === "viewer" })));
      const out = el("input", { class: "field", type: "text", readonly: true, id: "invite-link", "aria-label": "Invite link", hidden: true });
      const copy = el("button", { type: "button", class: "text-btn", text: "Copy", hidden: true,
        onclick: () => { try { navigator.clipboard.writeText(out.value).then(() => toast("Copied", null, 2000), () => out.select()); } catch (e) { out.select(); } } });
      const make = el("button", { type: "button", class: "btn-accent", text: "Make an invite link", onclick: async () => {
        try {
          const r = await api("POST", "/api/admin/invites", { role: irole.value });
          const link = (r && (r.url || r.link)) || (r && r.token ? `${location.origin}/join/${r.token}` : "");
          out.value = link; out.hidden = copy.hidden = !link;
        } catch (e) { toast(e.message); }
      } });
      kids.push(el("p", { class: "pref-h", text: "Invite someone" }),
        el("div", { class: "invite" }, irole, make), el("div", { class: "invite" }, out, copy),
        el("p", { class: "muted", text: "One use, for 7 days." }));
    }
    body.replaceChildren(...kids);
  }

  // An admin takes someone's picture down (hub/avatars.py); the event redraws it everywhere.
  async function removePicture(u) {
    try {
      await api("DELETE", `/api/admin/users/${encodeURIComponent(u.id)}/avatar`);
      u.avatar = null;
      if (window.PcgAvatar) window.PcgAvatar.set(u.id, null);
      toast("Picture removed", null, 3000);
    } catch (e) { toast(e.message); }
  }

  // Password sign-in (PCG_AUTH=password): this person's name, password and sessions.
  function pwField(id, label, auto) {
    const f = el("input", { class: "field", type: "password", id, autocomplete: auto, maxlength: "256" });
    f.setAttribute("aria-labelledby", `${id}-l`);
    return [el("span", { class: "fld-l", id: `${id}-l`, text: label }), f];
  }
  async function accountTab(body) {
    const me = S.me || {};
    const name = el("input", { class: "field", id: "acct-name", maxlength: "60", autocomplete: "name", "aria-label": "Your name" });
    name.value = me.name || "";
    const nameMsg = el("span", { class: "ok", id: "acct-name-msg", role: "status" });
    const nameSave = el("button", { type: "button", class: "btn-accent", id: "acct-name-save", text: "Save" });
    nameSave.addEventListener("click", async () => {
      nameSave.disabled = true;
      try {
        const r = await api("PUT", "/api/me", { name: name.value });
        S.me.name = r.name; name.value = r.name;
        $("set-who").textContent = [S.me.name, S.me.email, S.me.role].filter(Boolean).join(" · ");
        nameMsg.className = "ok"; nameMsg.textContent = "Saved";
      } catch (e) { nameMsg.className = "err"; nameMsg.textContent = e.message; }
      nameSave.disabled = false;
    });
    const [curL, cur] = pwField("acct-cur", "Current password", "current-password");
    const [newL, nw] = pwField("acct-new", "New password", "new-password");
    const user = el("input", { type: "text", autocomplete: "username", value: me.username || "", hidden: true, readonly: true, "aria-hidden": "true", tabindex: "-1" });
    const pwMsg = el("p", { class: "err", id: "acct-pw-msg", role: "status" });
    const pwSave = el("button", { type: "submit", class: "btn-accent", id: "acct-pw-save", text: "Change password" });
    const form = el("form", { id: "acct-pw", novalidate: true }, user, curL, cur, newL, nw,
      el("div", { class: "save-row" }, pwSave), pwMsg);
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      pwMsg.className = "err"; pwMsg.textContent = "";
      pwSave.disabled = true;
      try {
        await api("POST", "/api/auth/password", { current: cur.value, password: nw.value });
        cur.value = ""; nw.value = "";
        pwMsg.className = "ok"; pwMsg.textContent = "Changed. Every other browser is signed out.";
      } catch (err) { pwMsg.textContent = err.message; }
      pwSave.disabled = false;
    });
    const out = el("button", { type: "button", class: "btn-accent", id: "acct-out", text: "Sign out" });
    const all = el("button", { type: "button", class: "text-btn danger", id: "acct-out-all", text: "Sign out everywhere" });
    out.addEventListener("click", async () => {
      out.disabled = true;
      try { await api("POST", "/api/auth/signout"); } catch (e) { /* the cookie is cleared either way */ }
      location.replace("/signin");
    });
    let armed = false;
    all.addEventListener("click", async () => {
      if (!armed) { armed = true; all.textContent = "Sign out everywhere now"; return; }
      all.disabled = true;
      try { await api("POST", "/api/auth/signout-all"); location.replace("/signin"); } catch (e) { all.disabled = false; toast(e.message); }
    });
    // the theme and the size in this browser (theme.js; the themes' colours in themes.css); System
    // follows the computer's light or dark
    const PT = window.pcgTheme;
    const look = el("div", { class: "seg", id: "acct-theme", role: "radiogroup", "aria-label": "Theme" });
    const drawLook = () => {
      const now = PT ? PT.saved() : "";
      const all = [{ v: "", label: "System" }].concat(PT ? PT.themes : [{ v: "light", label: "Light" }, { v: "dark", label: "Dark" }]);
      look.replaceChildren(...all.map(({ v, label }) => el("button", {
        type: "button", role: "radio", "aria-checked": String(now === v), "data-v": v || "system",
        onclick: () => { if (PT) PT.set(v); drawLook(); },
      }, el("span", { class: "sw", "data-sw": v || "system", "aria-hidden": "true" }), label)));
    };
    drawLook();
    const sizeSeg = el("div", { class: "seg", id: "acct-size", role: "radiogroup", "aria-label": "Size" });
    const drawSize = () => {
      const now = PT ? PT.size() : 100;
      sizeSeg.replaceChildren(...(PT ? PT.sizes : [100]).map((v) => el("button", {
        type: "button", role: "radio", "aria-checked": String(now === v), "data-v": String(v),
        onclick: () => { if (PT) PT.setSize(v); drawSize(); },
      }, `${v}%`)));
    };
    drawSize();
    body.replaceChildren(
      el("p", { class: "pref-h", text: "Name" }), el("div", { class: "invite first" }, name, nameSave), nameMsg,
      ...pictureSection(),
      el("p", { class: "pref-h sec", text: "Appearance" }), look,
      el("p", { class: "pref-h sec", text: "Size" }), sizeSeg,
      ...listeningSwitch(),
      el("p", { class: "pref-h sec", text: "Change password" }), form,
      el("p", { class: "pref-h sec", text: "Sign out" }),
      el("div", { class: "save-row" }, out, all));
  }

  // ------------------------------------------------------------------ profile pictures
  // avatar.js draws them (the picture, or initials on a colour) and hub/avatars.py keeps them.
  // The browser makes the picture that is sent: the middle square of the file, 256 x 256, a JPEG
  // (quality 0.85), so the hub only ever gets a small JPEG and never opens an image itself.
  const AV_SIDE = 256, AV_QUALITY = 0.85, AV_FILE_MAX = 10 * 1024 * 1024;
  const avatarNode = (u, size) => (window.PcgAvatar && u ? window.PcgAvatar.node(u, size) : document.createTextNode(""));
  const hasPicture = (u) => !!(window.PcgAvatar ? window.PcgAvatar.version(u) : u && u.avatar);
  function avatarChanged(d) {
    if (!d || d.user_id === undefined || d.user_id === null) return;
    if (window.PcgAvatar) window.PcgAvatar.set(d.user_id, d.avatar || null);
    if (S.me && S.me.id === d.user_id) { S.me.avatar = d.avatar || null; if (S.set.pic) S.set.pic(); }
  }
  function imageOf(f) {
    const viaImg = () => new Promise((resolve, reject) => {          // a data: URL: the page's CSP allows no blob: images
      const r = new FileReader();
      r.onerror = () => reject(new Error("unreadable"));
      r.onload = () => {
        const img = new Image();
        img.onload = () => resolve(img);
        img.onerror = () => reject(new Error("undecodable"));
        img.src = r.result;
      };
      r.readAsDataURL(f);
    });
    return window.createImageBitmap ? createImageBitmap(f, { imageOrientation: "from-image" }).catch(viaImg) : viaImg();
  }
  // Refused: a word or two, no sentences (Leo, 2026-09-29).
  const AV_SAYS = { not_image: "Not an image", not_jpeg: "Not an image", bad_type: "Not an image", too_big: "Too big",
    too_large: "Too big", bad_size: "Wrong size", not_square: "Wrong size", slow_down: "Wait a minute" };
  const avRefusal = (code) => Object.assign(new Error(AV_SAYS[code] || "Failed"), { code });
  async function makePicture(f) {
    if (!f || !/^image\//i.test(f.type || "")) throw avRefusal("not_image");
    if (f.size > AV_FILE_MAX) throw avRefusal("too_big");
    let img;
    try { img = await imageOf(f); } catch (e) { throw avRefusal("not_image"); }
    const w = img.naturalWidth || img.width, h = img.naturalHeight || img.height, side = Math.min(w, h);
    if (!side) throw avRefusal("not_image");
    const cv = el("canvas", { width: String(AV_SIDE), height: String(AV_SIDE) });
    const g = cv.getContext("2d");
    g.fillStyle = "#FFFFFF";                 // a transparent picture on white, not on black
    g.fillRect(0, 0, AV_SIDE, AV_SIDE);
    g.imageSmoothingEnabled = true; g.imageSmoothingQuality = "high";
    g.drawImage(img, (w - side) / 2, (h - side) / 2, side, side, 0, 0, AV_SIDE, AV_SIDE);
    if (img.close) img.close();
    const blob = await new Promise((resolve) => cv.toBlob(resolve, "image/jpeg", AV_QUALITY));
    if (!blob || blob.type !== "image/jpeg") throw avRefusal("failed");
    return blob;
  }
  // Settings > Account: "Profile picture", the picture, Upload, Remove; nothing else to read.
  function pictureSection() {
    const pic = el("div", { class: "av-now", id: "acct-av" });
    const file = el("input", { type: "file", accept: "image/*", id: "acct-av-file", hidden: true, "aria-label": "Profile picture" });
    const up = el("button", { type: "button", class: "btn-accent", id: "acct-av-up", text: "Upload" });
    const rm = el("button", { type: "button", class: "text-btn danger", id: "acct-av-rm", text: "Remove" });
    const msg = el("span", { class: "err", id: "acct-av-msg", role: "status" });
    const draw = () => {
      if (!pic.isConnected && pic.firstChild) return;
      pic.replaceChildren(avatarNode(S.me, "l"));
      rm.hidden = !hasPicture(S.me);
    };
    const run = async (fn) => {
      up.disabled = rm.disabled = true; msg.textContent = "";
      try {
        const v = await fn();
        S.me.avatar = v;
        if (window.PcgAvatar) window.PcgAvatar.set(S.me.id, v);
      } catch (e) { msg.textContent = AV_SAYS[e.code] || "Failed"; }
      up.disabled = rm.disabled = false; draw();
    };
    up.addEventListener("click", () => { file.value = ""; file.click(); });
    file.addEventListener("change", () => {
      const f = file.files && file.files[0];
      if (f) run(async () => (await api("PUT", "/api/me/avatar", await makePicture(f))).avatar).then(() => { file.value = ""; });
    });
    rm.addEventListener("click", () => run(async () => { await api("DELETE", "/api/me/avatar"); return null; }));
    S.set.pic = draw;
    draw();
    return [el("p", { class: "pref-h sec", text: "Profile picture" }),
      el("div", { class: "av-edit" }, pic, el("div", { class: "av-btns" }, up, rm, msg)), file];
  }

  // Users with password sign-in: the group's list of Imperial addresses. Adding a short code
  // makes the account at once (username = the short code, which is also the first password,
  // replaced at the first sign-in); removing one disables the account, ending its sessions and
  // devices, and keeps what they made.
  const EVENTS = {
    signin: "signed in", signin_failed: "sign-in failed", signin_limited: "sign-ins paused after too many failures",
    signout_all: "signed out everywhere", password_changed: "changed the password", password_change_failed: "typed a wrong current password",
    reset_asked: "asked for a new password", reset_unknown: "asked for a new password: not on the list", reset_limited: "asked for too many links",
    link_sent: "password link emailed", email_failed: "email failed", link_made: "password link made", link_used: "set a password with a link",
    reset_default: "reset to the first password", allowed: "added to the list", removed: "removed from the list",
    role: "role changed", disabled: "disabled changed", welcome_sent: "welcome email sent", welcome_failed: "welcome email failed",
  };
  const CODE_RX = /^[a-z0-9]+(?:[.-][a-z0-9]+)*$/;
  const DOMAIN_RX = /^(?=.{3,190}$)[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)+$/;
  // the short code and the domain after the @ (ic.ac.uk unless the admin changes it)
  function codeOf(v, d) {
    const s = v.trim().toLowerCase(), dom = d.trim().toLowerCase().replace(/^@/, "");
    if (!s) return { err: "" };
    if (s.includes("@")) return { err: "Type only the part before the @ here; the part after it goes in the second box." };
    if (!CODE_RX.test(s) || s.length > 64) return { err: "A short code is letters and digits, maybe with dots or hyphens, like yl6719." };
    if (!DOMAIN_RX.test(dom)) return { err: dom ? `“${dom.slice(0, 40)}” is not an email domain, like ic.ac.uk.` : "Type the part after the @, like ic.ac.uk." };
    return { code: s, email: `${s}@${dom}` };
  }
  async function peopleTab(body) {
    let j, lg;
    try { [j, lg] = await Promise.all([api("GET", "/api/admin/users"), api("GET", "/api/admin/auth-log?limit=100")]); }
    catch (e) { if (stillOn("users")) failed(body, e); return; }
    if (!stillOn("users")) return;
    const users = listOf(j, "users");
    const again = () => { if (stillOn("users")) peopleTab(body); };
    const ROLES = ["viewer", "contributor", "admin"];

    // add: the short code @ the domain (ic.ac.uk, which the admin can change); a whole address
    // pasted into the first box is split into the two
    const code = el("input", { class: "field", id: "add-code", autocomplete: "off", autocapitalize: "off", spellcheck: "false",
      maxlength: "80", placeholder: "short code, like yl6719", "aria-label": "Short code", enterkeyhint: "next" });
    const dom = el("input", { class: "field dom", id: "add-domain", autocomplete: "off", autocapitalize: "off", spellcheck: "false",
      maxlength: "190", "aria-label": "Email domain", enterkeyhint: "done" });
    dom.value = "ic.ac.uk";
    const add = el("button", { type: "submit", class: "btn-accent", id: "add-go", text: "Add", disabled: true });
    const addMsg = el("p", { class: "muted", id: "add-msg", role: "status" });
    const check = () => {
      const at = code.value.indexOf("@");
      if (at >= 0) {                        // an "@" typed or pasted: the rest goes on in the domain box
        dom.value = code.value.slice(at + 1).trim(); code.value = code.value.slice(0, at);
        dom.focus(); dom.setSelectionRange(dom.value.length, dom.value.length);
      }
      const c = codeOf(code.value, dom.value);
      add.disabled = !c.code;
      addMsg.className = c.err ? "err" : "muted"; addMsg.textContent = c.err || "";
    };
    code.addEventListener("input", check);
    dom.addEventListener("input", check);
    const addForm = el("form", { class: "invite first", id: "add-form", novalidate: true },
      el("div", { class: "addr" }, code, el("span", { class: "suffix", "aria-hidden": "true", text: "@" }), dom), add);
    addForm.addEventListener("submit", async (e) => {
      e.preventDefault();
      const c = codeOf(code.value, dom.value);
      if (!c.code) return;
      add.disabled = true;
      try {
        const r = await api("POST", "/api/admin/allowed", { email: c.email });
        S.set.added = r.state === "already" ? `${r.email} is already on the list.`
          : r.welcome === "queued" ? `Added ${r.email}. Welcome email sent.`
          : `Added ${r.email}. Tell them: username ${r.username}, first password ${r.username}${r.user && !r.user.must_change ? " (or the one they had)" : ""}.`;
        again();
      } catch (err) { addMsg.className = "err"; addMsg.textContent = err.message; add.disabled = false; }
    });
    if (S.set.added) { addMsg.className = "ok"; addMsg.textContent = S.set.added; S.set.added = null; }

    const put = async (u, change, undo) => {
      try {
        const r = await api("PUT", `/api/admin/users/${encodeURIComponent(u.id)}`, change);
        Object.assign(u, change, r && r.id !== undefined ? r : {});
        toast(`Saved ${u.username || u.name}`, null, 3000);
      } catch (e) { undo(); toast(e.message); }
    };
    // what opens under a row: a password link to copy, or a confirm step
    function panel(li, kids) {
      const old = li.querySelector(".it-more");
      if (old) old.remove();
      if (kids) li.append(el("div", { class: "it-more" }, kids));
    }
    async function linkFor(u, li) {
      try {
        const r = await api("POST", `/api/admin/users/${u.id}/reset-link`);
        const out = el("input", { class: "field", type: "text", readonly: true, id: `link-${u.id}`, "aria-label": `Password link for ${u.username}` });
        out.value = r.url;
        const copy = el("button", { type: "button", class: "text-btn", text: "Copy", onclick: () => {
          try { navigator.clipboard.writeText(out.value).then(() => toast("Copied", null, 2000), () => out.select()); } catch (e) { out.select(); }
        } });
        panel(li, [out, copy, el("button", { type: "button", class: "text-btn", text: "Close", onclick: () => panel(li) }),
          el("p", { class: "muted", text: `Works once, for 24 hours: send it to ${u.username} yourself.` })]);
      } catch (e) { toast(e.message); }
    }
    async function welcomeAgain(u) {
      try { await api("POST", `/api/admin/users/${u.id}/welcome`); toast(`Welcome email on its way to ${u.email}`, null, 4000); }
      catch (e) { toast(e.message); }
    }
    function resetDefault(u, li) {
      const go = el("button", { type: "button", class: "btn-accent", text: "Reset" });
      go.addEventListener("click", async () => {
        go.disabled = true;
        try { await api("POST", `/api/admin/users/${u.id}/reset-default`); toast(`${u.username} has the first password again`, null, 4000); again(); }
        catch (e) { go.disabled = false; toast(e.message); }
      });
      panel(li, [el("p", { class: "muted", text: `Sets the password back to ${u.username} and ends every session and device of theirs.` }),
        go, el("button", { type: "button", class: "text-btn", text: "Cancel", onclick: () => panel(li) })]);
    }
    function remove(u, li) {
      const word = el("input", { class: "field", id: `del-${u.id}`, autocomplete: "off", autocapitalize: "off", spellcheck: "false",
        placeholder: "delete", "aria-label": "Type delete to confirm" });
      const go = el("button", { type: "button", class: "btn-accent danger", id: `del-go-${u.id}`, text: "Remove", disabled: true });
      word.addEventListener("input", () => { go.disabled = word.value.trim().toLowerCase() !== "delete"; });
      go.addEventListener("click", async () => {
        go.disabled = true;
        try {
          await api("DELETE", "/api/admin/allowed", { email: u.email, confirm: word.value });
          toast(`Removed ${u.email}`, null, 4000);
          again();
        } catch (e) { go.disabled = false; toast(e.message); }
      });
      panel(li, [el("p", { class: "muted", text: `Type delete to remove ${u.email}. They can no longer sign in, and their sessions and devices end; the episodes and graph edits they made stay, still theirs.` }),
        word, go, el("button", { type: "button", class: "text-btn", text: "Cancel", onclick: () => panel(li) })]);
      word.focus();
    }

    const listed = users.filter((u) => u.on_list);
    const ul = el("ul", { class: "items", id: "users" }, listed.map((u) => {
      const li = el("li", { class: "item", "data-id": String(u.id) });
      const role = el("select", { class: "pick", "aria-label": `Role of ${u.username || u.name}` },
        ROLES.map((r) => el("option", { value: r, text: r[0].toUpperCase() + r.slice(1), selected: u.role === r })));
      role.addEventListener("change", () => { const was = u.role; put(u, { role: role.value }, () => { role.value = was; }); });
      const mine = u.id === S.me.id;
      const more = el("button", { type: "button", class: "icon-btn", "aria-label": `More for ${u.username || u.name}`, "aria-haspopup": "menu", html: I.more });
      more.addEventListener("click", (e) => {
        e.stopPropagation();
        openMenu(more, `user-${u.id}`, [mItem("Make a password link", () => linkFor(u, li))]
          .concat(!mine && u.default_password && !u.disabled && j && j.email ? [mItem("Send the welcome email again", () => welcomeAgain(u))] : [])
          .concat(!mine && hasPicture(u) ? [mItem("Remove their picture", () => removePicture(u))] : [])
          .concat(mine ? [] : [mItem("Reset to the first password", () => resetDefault(u, li)), mItem("Remove…", () => remove(u, li), "danger")]));
      });
      const sub = [u.name && u.name !== u.username ? u.username : u.email,
        u.default_password ? el("span", { class: "warn", text: "first password" }) : null,
        !u.default_password ? null
          : u.welcome_failed_at && !(u.welcome_at && u.welcome_at > u.welcome_failed_at) ? el("span", { class: "warn", text: "welcome email failed" })
          : u.welcome_at ? `welcome emailed ${when(u.welcome_at)}` : null,
        u.last_login_at ? `last signed in ${when(u.last_login_at)}` : "never signed in",
        u.reset_asked_at ? el("span", { class: "warn", text: `asked for a new password ${when(u.reset_asked_at)}` }) : null,
        u.disabled ? el("span", { class: "warn", text: "disabled" }) : null,
        u.note && u.note !== "bootstrap" ? u.note : null].filter(Boolean);
      const s = el("div", { class: "it-s" });
      sub.forEach((x, i) => { if (i) s.append(" · "); s.append(x); });
      li.append(avatarNode(u, "m"), el("div", { class: "it-main" }, el("div", { class: "it-t", text: `${u.name || u.username}${mine ? " (you)" : ""}` }), s),
        el("div", { class: "it-ctl" }, role, more));
      return li;
    }));
    const off = users.filter((u) => !u.on_list);
    const events = (lg && lg.events) || [];
    const evs = el("ul", { class: "events", id: "auth-log" }, events.slice(0, 50).map((e) => {
      const who = e.user ? (e.user.username || e.user.name) : (e.email || "");
      const bits = [who, EVENTS[e.kind] || e.kind, e.actor ? `by ${e.actor.name}` : "", e.detail || "", e.ip || ""].filter(Boolean);
      return el("li", { class: "ev", "data-kind": e.kind }, el("span", { class: "ev-t t", text: when(e.at) }), el("span", { class: "ev-x", text: bits.join(" · ") }));
    }));
    if (!events.length) evs.append(el("li", { class: "muted", text: "Nothing yet." }));
    body.replaceChildren(...[
      el("p", { class: "pref-h", text: "Add someone" }), addForm, addMsg,
      el("p", { class: "pref-h sec", text: `On the list (${listed.length})` }), ul,
      j && j.email === false ? el("p", { class: "muted", id: "no-email", text: "This hub cannot send email yet, so someone who forgets their password shows up here as “asked for a new password”: reset them to the first password, or make them a password link." }) : null,
      off.length ? el("p", { class: "muted", id: "off-list", text: `Not on the list, so they cannot sign in: ${off.map((u) => u.username || u.email).join(", ")}. Add one again to bring the account back.` }) : null,
      el("p", { class: "pref-h sec", text: "Sign-ins and changes" }), evs].filter(Boolean));
  }

  async function baseTab(body) {
    let j;
    try { j = await api("GET", "/api/admin/base"); } catch (e) { if (stillOn("base")) failed(body, e); return; }
    if (!stillOn("base")) return;
    const versions = j.versions || [];
    const st = S.set.base = { versions, show: versions.length ? versions[0].version : null, editing: false };
    const draw = () => {
      if (!stillOn("base")) return;
      const newest = versions[0];
      const kids = [];
      if (st.editing) {
        const g = el("textarea", { class: "field editor", id: "base-text", "aria-label": "Guideline", spellcheck: "false" });
        g.value = newest ? newest.guideline : "";
        const w = el("textarea", { class: "field editor small", id: "base-wording", "aria-label": "Wording list (JSON)", spellcheck: "false" });
        w.value = newest ? JSON.stringify(newest.wording, null, 2) : "";
        for (const t of [g, w]) t.addEventListener("input", () => { S.typedAt = Date.now(); });
        const err = el("p", { class: "err", id: "base-err", role: "status" });
        const n = (newest ? newest.version : 0) + 1;
        const saveB = el("button", { type: "button", class: "btn-accent", id: "base-save", text: `Save as v${n}` });
        saveB.addEventListener("click", async () => {
          saveB.disabled = true; err.textContent = "";
          try {
            const r = await api("POST", "/api/admin/base", { guideline: g.value, wording: w.value.trim() ? w.value : null });
            versions.unshift(r);
            st.show = r.version; st.editing = false;
            toast(`Base prompt v${r.version} saved`, null, 3000);
            draw();
          } catch (e) { err.textContent = e.message; saveB.disabled = false; }
        });
        kids.push(el("p", { class: "pref-h", text: `New version (v${n})` }), g,
          el("p", { class: "pref-h", text: "Wording that is never wanted (JSON)" }), w,
          el("div", { class: "save-row" }, saveB, el("button", { type: "button", class: "text-btn", text: "Cancel", onclick: () => { st.editing = false; draw(); } })), err);
      } else if (!versions.length) {
        kids.push(el("p", { class: "muted", text: "No base prompt yet." }));
      } else {
        const v = versions.find((x) => x.version === st.show) || newest;
        kids.push(el("div", { class: "seg vers", role: "radiogroup", "aria-label": "Versions" }, versions.map((x) => el("button", {
          type: "button", role: "radio", "aria-checked": String(x.version === v.version), text: `v${x.version}`,
          onclick: () => { st.show = x.version; draw(); },
        }))),
        el("p", { class: "muted", id: "base-meta", text: [`v${v.version}`, day(v.created_at), v.created_by && v.created_by.name ? `by ${v.created_by.name}` : "", v === newest ? "in use" : ""].filter(Boolean).join(" · ") }),
        el("pre", { class: "guide", id: "base-view", text: v.guideline }));
      }
      if (!st.editing) kids.push(el("div", { class: "save-row" }, el("button", { type: "button", class: "btn-accent", id: "base-new", text: "New version", onclick: () => { st.editing = true; draw(); } })));
      body.replaceChildren(...kids);
    };
    draw();
  }

  // ------------------------------------------------------------------ voices
  // The narrator. Each person's own, for the versions they make (Settings, Voice); and a
  // version's own, which its maker or an admin can change ("Voice: … Change" in the window):
  // the hub records it again in the fair queue, this version plays on meanwhile, and when the
  // new audio lands this page moves to it at the same sentence. The audio's revision is in its
  // URL (?v=), so no browser plays a cached old one. Samples come from tools/make_voice_samples.py.
  S.voices = null; S.vpick = null; S.vtab = null;
  const makesVersions = () => !!S.me && (S.me.role === "contributor" || S.me.role === "admin");
  function audioUrl(e) {
    const r = e && e.voice && e.voice.rev;
    return `/audio/${e.id}.mp3${r > 1 ? `?v=${r}` : ""}`;
  }
  function voicesList() {
    if (!S.voices) S.voices = api("GET", "/api/voices").catch((e) => { S.voices = null; throw e; });
    return S.voices;
  }
  function nth(n) {
    const t = n % 100, u = n % 10;
    return `${n}${t >= 11 && t <= 13 ? "th" : u === 1 ? "st" : u === 2 ? "nd" : u === 3 ? "rd" : "th"}`;
  }
  function pendingText(pd) {
    if (pd.script && pd.cancel === false) {      // a replaced script, recorded again in the voice it has
      if (pd.state === "retrying") return "new script: it failed, trying again";
      if (pd.state !== "working") return `new script waiting to be recorded${pd.position ? `, ${nth(pd.position)} in line` : ""}`;
      if (pd.phase === "speaking") return `recording the new script, ${speakPct(pd)}%`;
      if (pd.phase === "encoding") return "new script: making the MP3";
      return "new script: waiting for the GPU";
    }
    const who = pd.name || "the new voice";
    if (pd.state === "retrying") return `${who}: it failed, trying again`;
    if (pd.state !== "working") return `changing to ${who}${pd.position ? `, ${nth(pd.position)} in line` : ""}`;
    if (pd.phase === "speaking") return `recording ${who}, ${speakPct(pd)}%`;
    if (pd.phase === "encoding") return `${who}: making the MP3`;
    return `${who}: waiting for the GPU`;
  }

  // One sample plays at a time, never over the episode.
  const sample = { a: null, id: null };
  function sampleAudio() {
    if (!sample.a) {
      sample.a = new Audio();
      for (const ev of ["play", "pause", "ended", "error"]) sample.a.addEventListener(ev, drawSampleButtons);
    }
    return sample.a;
  }
  function playSample(x) {
    const a = sampleAudio();
    if (sample.id === x.id && !a.paused) { a.pause(); return; }
    if (!A().paused) A().pause();
    sample.id = x.id;
    a.src = x.sample;
    a.play().catch((e) => { if (e.name !== "AbortError") toast(`Cannot play the sample: ${e.message}`); });
    drawSampleButtons();
  }
  function stopSample() { if (sample.a && !sample.a.paused) sample.a.pause(); }
  function drawSampleButtons() {
    const on = sample.a && !sample.a.paused && !sample.a.ended ? sample.id : null;
    for (const b of document.querySelectorAll(".vplay[data-sample]")) {
      const playing = b.dataset.sample === on;
      if (b.getAttribute("aria-pressed") === String(playing)) continue;
      b.setAttribute("aria-pressed", String(playing));
      b.innerHTML = playing ? I.pause(16) : I.play(16);
    }
  }
  // The voices as radio rows, each with its sample (the Versions list's look).
  function voiceRows(list, sel, pick) {
    return list.map((x) => el("div", { class: "vrow", "data-voice": x.id },
      el("button", { type: "button", class: "ver", role: "radio", "aria-checked": String(x.id === sel), onclick: () => pick(x.id) },
        el("span", { class: "mark", "aria-hidden": "true" }),
        el("span", { class: "v-main" }, el("span", { class: "v-who", text: x.name }))),
      x.sample
        ? el("button", { type: "button", class: "icon-btn vplay", "data-sample": x.id, "aria-pressed": "false", "aria-label": `Play the sample of ${x.name}`,
          html: I.play(16), onclick: () => playSample(x) })
        : el("button", { type: "button", class: "text-btn vnone", disabled: true, "aria-label": `${x.name}: sample not made yet`, text: "sample not made yet" })));
  }

  // The window: "Voice: Warm male · Change", and the chooser under the player.
  function renderVoice(c) {
    let line = $("w-voice"), box = $("vchoose");
    if (!line) { line = el("p", { class: "maker vline", id: "w-voice", hidden: true }); $("w-maker").after(line); }
    if (!box) { box = el("section", { class: "versions vchoose", id: "vchoose", hidden: true }); $("versions").before(box); }
    const v = c && c.has_audio ? c.voice : null;
    if (!v || (!v.name && !v.can_change && !v.pending)) {
      line.hidden = true; box.hidden = true; box.dataset.sig = "";
      return;
    }
    const pd = v.pending, kids = [el("span", { text: `Voice: ${v.name || "original"}` })];
    if (pd) {
      kids.push(el("span", { class: "vpend", text: ` · ${pendingText(pd)}` }));
      if (v.can_change && pd.state !== "working" && pd.cancel !== false) kids.push(" · ", el("button", { type: "button", class: "text-btn vbtn", id: "voice-cancel", text: "Cancel", onclick: () => undoChange(c.id) }));
    } else if (v.can_change) {
      kids.push(" · ", el("button", { type: "button", class: "text-btn vbtn", id: "voice-change", text: "Change", "aria-expanded": String(!!S.vpick && S.vpick.eid === c.id),
        onclick: () => { S.vpick = S.vpick && S.vpick.eid === c.id ? null : { eid: c.id, id: v.id }; stopSample(); renderWin(); } }));
    }
    if (v.error && !pd) kids.push(el("span", { class: "verr", text: ` · Couldn’t change it to ${v.error}` }));
    const sig = JSON.stringify([c.id, v, !!S.vpick && S.vpick.eid === c.id]);
    if (line.dataset.sig !== sig) { line.dataset.sig = sig; line.replaceChildren(...kids); }
    line.hidden = false;
    if (!S.vpick || S.vpick.eid !== c.id || pd || !v.can_change) { box.hidden = true; box.dataset.sig = ""; return; }
    voicesList().then((j) => {
      if (!S.vpick || S.vpick.eid !== c.id) return;
      const bsig = JSON.stringify([c.id, S.vpick.id, v.id, v.custom || null, !!v.custom_old]);
      box.hidden = false;
      if (box.dataset.sig === bsig) return;
      box.dataset.sig = bsig;
      // the presets, then its maker's custom voice (in an older version of it: recorded again)
      const rows = j.voices.concat(v.custom ? [v.custom] : []);
      const to = rows.find((x) => x.id === S.vpick.id);
      const same = S.vpick.id === v.id && !(v.id === "custom" && v.custom_old);
      const go = el("button", { type: "button", class: "btn-accent", id: "voice-go", disabled: !to || same,
        text: to && !same ? `Record it in ${to.name}` : "Pick a voice", onclick: () => askChange(c.id, S.vpick.id) });
      box.replaceChildren(el("div", { class: "col" }, el("h3", { class: "sec-h", text: "Voice" }),
        el("div", { class: "vlist", role: "radiogroup", "aria-label": "Voice for this version" },
          voiceRows(rows, S.vpick.id, (id) => { S.vpick.id = id; renderWin(); })),
        el("div", { class: "save-row" }, go, el("button", { type: "button", class: "text-btn", id: "voice-close", text: "Cancel", onclick: () => { S.vpick = null; stopSample(); renderWin(); } }))));
      drawSampleButtons();
    }).catch((e) => toast(e.message));
  }
  function setVoice(eid, v) {
    const e = epById(eid);
    if (e) e.voice = Object.assign({}, e.voice || {}, v);
    const pid = S.epPaper.get(eid);
    if (S.open === pid) renderWin();
  }
  async function askChange(eid, id) {
    const b = $("voice-go");
    if (b) b.disabled = true;
    try {
      const v = await api("PUT", `/api/episodes/${eid}/voice`, { voice: id });
      S.vpick = null; stopSample();
      setVoice(eid, v);
      if (v.pending) toast(`Recording it in ${v.pending.name}. This version plays until it is ready.`);
    } catch (e) { if (b) b.disabled = false; toast(e.message); }
  }
  async function undoChange(eid) {
    try { setVoice(eid, await api("DELETE", `/api/episodes/${eid}/voice`)); } catch (e) { toast(e.message); }
  }
  // The new audio of a version this page has loaded: carry on in it at the same sentence (the
  // hub maps the second), playing if it was.
  async function voiceSwapped(d) {
    const eid = d.episode_id || d.id, a = A();
    if (S.audioEp !== eid || !a.getAttribute("src")) return;
    const was = !a.paused && !a.ended, t = a.currentTime || 0;
    let v;
    try { v = await api("GET", `/api/episodes/${eid}/voice?at=${t.toFixed(2)}&rev=${d.voice_swap.from_rev}`); } catch (x) { return; }
    const e = epById(eid);
    if (!e || S.audioEp !== eid) return;
    e.voice = Object.assign({}, e.voice || {}, v);
    if (v.duration_s) e.duration_s = v.duration_s;
    S.dirty = false;                  // the old audio's second is not saved over the new one's
    a.pause();
    const now = Date.now();
    store.set(posKey(eid), JSON.stringify({ s: v.at, at: now }));
    e.position_s = v.at; e.position_at = now;
    loadAudio(eid, was ? (x) => { x.play().catch(() => {}); } : null);
    toast(d.voice_swap.script ? "Now the new script." : `Now in the new voice${v.name ? `: ${v.name}` : ""}.`);
  }
  window.addEventListener("papercast:event", (ev) => {
    const d = ev.detail || {};
    if (d.kind === "episode" && d.data && d.data.voice_swap) voiceSwapped(d.data);
  });

  // Settings, Voice: my voice for new versions (the presets, and my custom voice once I have
  // one), every voice's sample, and Custom: a description in Breeze's words, previewed through
  // the voice queue (customvoice.py; the page follows it live), then used.
  const PREVIEW_STATE = { queued: "Waiting", working: "Making…", done: "Ready", failed: "Didn’t work: try other words" };
  async function voiceTab(body) {
    stopSample();
    S.voices = null;                    // the custom voice and its preview change: read them afresh
    let j;
    try { j = await voicesList(); } catch (e) { if (stillOn("voice")) failed(body, e); return; }
    if (!stillOn("voice")) return;
    const msg = el("span", { class: "ok", id: "voice-msg", role: "status" });
    const list = el("div", { class: "vlist", id: "voice-list", role: "radiogroup", "aria-label": "My voice" });
    const draw = () => { list.replaceChildren(...voiceRows(j.voices.concat(j.custom ? [j.custom] : []), j.mine, pick)); drawSampleButtons(); };
    const pick = async (id) => {
      if (id === j.mine) return;
      const was = j.mine;
      j.mine = id; draw();
      msg.className = "ok"; msg.textContent = "";
      try {
        const r = await api("PUT", "/api/voices/mine", { voice: id });
        j.mine = r.mine; msg.textContent = "Saved";
      } catch (e) { j.mine = was; draw(); msg.className = "err"; msg.textContent = e.message; }
    };
    const custom = customVoice(j, draw, msg);
    S.vtab = (d) => {                   // a live `voice` event (mine only), or a resync
      if (!stillOn("voice")) return;
      if (d === null) { api("GET", "/api/voices").then((x) => { Object.assign(j, x); draw(); custom.draw(); }).catch(() => {}); return; }
      if ("preview" in d) j.preview = d.preview;
      if (d.custom) j.custom = d.custom;
      if (d.mine) j.mine = d.mine;
      draw(); custom.draw();
    };
    draw();
    body.replaceChildren(
      el("div", { class: "pref" }, el("p", { class: "pref-h", text: "My voice" }), list),
      custom.el,
      el("div", { class: "save-row" }, msg));
  }
  function customVoice(j, drawList, msg) {
    const max = j.custom_max || 500;
    const text = el("textarea", { class: "field note", id: "vc-text", maxlength: String(max), rows: "3",
      placeholder: j.custom_example || "", "aria-label": "Custom voice" });
    text.value = (j.preview && j.preview.description) || (j.custom && j.custom.description) || "";
    const count = el("div", { class: "counter t", id: "vc-count" });
    const prev = el("button", { type: "button", class: "btn-accent", id: "vc-preview", text: "Preview" });
    const again = el("button", { type: "button", class: "text-btn", id: "vc-again", text: "Another take" });
    const use = el("button", { type: "button", class: "text-btn", id: "vc-use", text: "Use this voice" });
    const state = el("span", { class: "vc-state", id: "vc-state", role: "status" });
    const play = el("button", { type: "button", class: "icon-btn vplay", id: "vc-play", "data-sample": "preview", "aria-pressed": "false",
      "aria-label": "Play the preview", html: I.play(16) });
    let asking = false;
    const draw = () => {
      const pv = j.preview, said = text.value.replace(/\s+/g, " ").trim();
      const same = !!pv && pv.description === said, st = pv ? pv.state : null;
      const inUse = same && st === "done" && j.mine === "custom" && !!j.custom && j.custom.key === pv.key;
      count.textContent = `${text.value.length} / ${max}`;
      prev.disabled = asking || !said || st === "working" || (same && (st === "queued" || st === "done"));
      again.hidden = !(same && (st === "done" || st === "failed"));
      again.disabled = asking;
      use.hidden = !(same && st === "done");
      use.disabled = asking || inUse;
      use.textContent = inUse ? "In use" : "Use this voice";
      const show = !!pv && (same || st === "queued" || st === "working");
      state.textContent = show ? PREVIEW_STATE[st] || "" : "";
      state.className = `vc-state${show && st === "failed" ? " err" : ""}`;
      play.hidden = !(same && st === "done" && pv.sample);
      drawSampleButtons();
    };
    const ask = async (another) => {
      asking = true; draw();
      msg.className = "ok"; msg.textContent = "";
      try { j.preview = (await api("POST", "/api/voices/custom/preview", { description: text.value, another })).preview; }
      catch (e) { msg.className = "err"; msg.textContent = e.message; }
      asking = false; draw();
    };
    prev.addEventListener("click", () => ask(false));
    again.addEventListener("click", () => ask(true));
    use.addEventListener("click", async () => {
      asking = true; draw();
      msg.className = "ok"; msg.textContent = "";
      try {
        const r = await api("PUT", "/api/voices/custom", { preview: j.preview.id });
        j.mine = r.mine; j.custom = r.custom; msg.textContent = "Saved";
        drawList();
      } catch (e) { msg.className = "err"; msg.textContent = e.message; }
      asking = false; draw();
    });
    play.addEventListener("click", () => { if (j.preview && j.preview.sample) playSample({ id: "preview", sample: j.preview.sample }); });
    text.addEventListener("input", draw);
    draw();
    return { draw, el: el("div", { class: "pref vcustom", id: "voice-custom" }, el("p", { class: "pref-h", text: "Custom" }), text, count,
      el("div", { class: "save-row vc-row" }, prev, again, use, state, play)) };
  }
  window.addEventListener("papercast:event", (ev) => {
    const d = ev.detail || {};
    if (!S.vtab) return;
    if (d.kind === "voice") S.vtab(d.data || {});
    else if (d.kind === "resync") S.vtab(null);
  });

  // Slack (admins): whether it is set up, where it posts, a test message, the last 20 posts.
  const SLACK_STATE = { posted: "posted", pending: "waiting", sending: "sending", retrying: "trying again", failed: "failed", skipped: "not posted" };
  async function slackTab(body) {
    let j;
    try { j = await api("GET", "/api/admin/slack"); } catch (e) { if (stillOn("slack")) failed(body, e); return; }
    if (!stillOn("slack")) return;
    const status = el("p", { id: "slack-status", class: j.configured ? "ok" : "err",
      text: j.configured ? `Set up: episodes whose makers say yes are posted to ${j.channel} when they are ready.` : `Not set up: ${j.problem}` });
    const res = el("span", { class: "ok", id: "slack-test-msg", role: "status" });
    const test = el("button", { type: "button", class: "btn-accent", id: "slack-test", text: "Send test message", disabled: !j.configured });
    test.addEventListener("click", async () => {
      test.disabled = true; res.className = "ok"; res.textContent = "Sending…";
      try {
        const r = await api("POST", "/api/admin/slack/test", {});
        res.className = r.ok ? "ok" : "err"; res.textContent = r.ok ? `Sent to ${j.channel}` : r.detail;
      } catch (e) { res.className = "err"; res.textContent = e.message; }
      test.disabled = false;
    });
    const posts = j.posts || [];
    const ul = el("ul", { class: "items", id: "slack-posts" }, posts.map((p) => el("li", { class: "item", "data-id": p.episode_id, "data-state": p.state },
      el("div", { class: "it-main" },
        el("div", { class: "it-t", text: p.title || p.episode_id }),
        el("div", { class: "it-s", text: [p.made_by && p.made_by.name ? `by ${p.made_by.name}` : "", SLACK_STATE[p.state] || p.state,
          when(p.sent_at || p.updated_at || p.created_at), p.attempts > 1 ? `${p.attempts} tries` : ""].filter(Boolean).join(" · ") }),
        p.detail ? el("div", { class: "it-s" }, el("span", { class: p.state === "skipped" ? "" : "warn", text: p.detail })) : null))));
    if (!posts.length) ul.replaceChildren(el("li", { class: "muted intro", text: "Nothing announced yet." }));
    body.replaceChildren(
      status,
      el("div", { class: "save-row" }, test, res),
      el("p", { class: "pref-h sec", text: "Last 20" }), ul);
  }


  // ------------------------------------------------------------------ live events
  function connect() {
    if (S.es) S.es.close();
    clearTimeout(S.esRetry);
    const es = new EventSource(S.lastId ? `/api/events?last=${encodeURIComponent(S.lastId)}` : "/api/events");
    S.es = es;
    // EventSource retries a dropped connection by itself, but an answer that is not the stream
    // (a proxy's 502 while the hub restarts) closes it for good: reopen it.
    es.addEventListener("open", () => { S.esFails = 0; setOffline(false); });
    es.addEventListener("error", () => {
      if (S.es !== es) return;
      setOffline(true);
      if (es.readyState !== EventSource.CLOSED) return;
      if (passwordMode()) api("GET", "/api/config").catch(() => {});     // a session that ended: to the sign-in page
      S.esRetry = setTimeout(() => { if (S.es === es) connect(); }, Math.min(30000, 2000 * 2 ** Math.min(4, S.esFails++)));
    });
    const on = (t, fn) => es.addEventListener(t, (e) => {
      if (e.lastEventId) S.lastId = e.lastEventId;
      let d = null; try { d = JSON.parse(e.data); } catch (x) { return; }
      d = d || {};
      fn(d);
      if (t !== "hello" && t !== "resync" && t !== "graph" && t !== "log") toMap(t, d);
    });
    on("hello", (d) => checkBuild(d.build));
    on("resync", () => { loadLibrary(); toMap("resync", {}); });      // the map missed events too: it reads its graphs again
    on("mygraphs", () => { if (S.map && S.map.refreshList) S.map.refreshList(); });     // my subscriptions, from another tab
    on("paper", (d) => touched(d.paper_id || d.id || (d.paper && d.paper.id)));
    on("episode", (d) => touched(d.paper_id || (d.paper && d.paper.id) || S.epPaper.get(d.episode_id || d.id)));
    on("graph", (d) => toMap("graph", d));
    on("log", (d) => toMap("log", d));
    on("voice", () => {});               // my custom voice's preview (Settings, Voice): as papercast:event
    queueEvents(es);
    // profile pictures: avatar.js redraws each one on the page; a resync reads them all again
    on("avatar", avatarChanged);
    on("resync", () => { api("GET", "/api/config").then((j) => { if (window.PcgAvatar && j) window.PcgAvatar.seed(j.avatars); }, () => {}); });
    // comments and the board (social.js); paper, episode and log events reach it as papercast:event
    on("comment", (d) => { if (S.social) S.social.event("comment", d); });
    on("board", (d) => { if (S.social) S.social.event("board", d); });
    on("resync", () => { if (S.social) S.social.event("resync", {}); });
  }

  // ------------------------------------------------------------------ start
  async function start() {
    $("back").innerHTML = I.back; $("set-back").innerHTML = I.back; $("ls-back").innerHTML = I.back;
    $("w-more").innerHTML = I.more; $("w-more-phone").innerHTML = I.more; $("w-close").innerHTML = I.close;
    $("x-close").innerHTML = I.close;
    try { S.cfg = await api("GET", "/api/config"); } catch (e) { toast(e.message); return; }
    S.me = S.cfg.me || {};
    if (window.PcgAvatar) window.PcgAvatar.seed(S.cfg.avatars);       // everyone's profile picture
    S.build = S.cfg.build || "";        // the build this page's code came with
    S.social = window.PaperSocial ? window.PaperSocial.mount(socialCtx()) : null;     // comments and the board
    S.listening = window.PaperListening ? window.PaperListening.mount({ api, avatar: avatarNode }) : null;
    S.graphs = window.PaperGraphs.mount(graphsCtx());                                 // the home column
    $("w-listened").addEventListener("click", () => { const p = S.papers.get(S.open); if (p) setListened(p.id, !p.listened); });
    wirePlayer();
    wireQueue();
    wireTranscript();
    $("back").addEventListener("click", closePaper);
    $("w-close").addEventListener("click", closePaper);
    $("pt-tr").addEventListener("click", () => setPane("tr"));
    $("pt-c").addEventListener("click", () => setPane("c"));
    $("set-back").addEventListener("click", goHome);
    $("ls-back").addEventListener("click", goHome);
    $("gpl-back").addEventListener("click", () => back(""));
    $("gpl-map").addEventListener("click", () => { if (S.graph) nav(hashFor(S.graph, null, true), true); });
    $("listen-btn").addEventListener("click", () => { if (S.view === "listening") goHome(); else location.hash = "listening"; });
    $("set-btn").addEventListener("click", () => { if (S.view === "settings") goHome(); else location.hash = "settings"; });
    $("w-more").addEventListener("click", (e) => { e.stopPropagation(); openMenu(e.currentTarget, "win", winMenu()); });
    $("w-more-phone").addEventListener("click", (e) => { e.stopPropagation(); openMenu(e.currentTarget, "win", winMenu()); });
    $("x-open").addEventListener("click", openExplainer);
    $("x-close").addEventListener("click", closeExplainer);
    $("overlay").addEventListener("click", (e) => { if (e.target === $("overlay")) closeExplainer(); });
    document.addEventListener("click", (e) => { if (S.menu && !S.menu.node.contains(e.target) && !S.menu.anchor.contains(e.target)) closeMenu(); });
    document.addEventListener("keydown", (e) => {
      if (e.key !== "Escape" || e.defaultPrevented) return;
      if (S.menu) { const a = S.menu.anchor; closeMenu(); a.focus(); } else if (!$("overlay").hidden) closeExplainer();
      else if (S.view === "paper" && !phone() && !(e.target && e.target.closest && e.target.closest("input, textarea, select"))) closePaper();
    });
    // the address: a step back or forward (each one reaches the page once)
    const address = () => { if (location.hash !== S.handled) openFromHash(); };
    window.addEventListener("hashchange", address);
    window.addEventListener("popstate", address);
    // a phone's layout and a wide screen's differ (the drawing, a graph's papers): the address again
    // (not over Settings or Listening, which keep a draft)
    window.matchMedia("(max-width: 720px)").addEventListener("change", () => { if (S.view !== "settings" && S.view !== "listening") openFromHash(); });
    // The map, the page's home: mounted at once, shown beside the column (a phone: from a graph's "Map").
    let listIn = null;
    const listReady = new Promise((res) => { listIn = res; });
    mapShown();
    S.map = window.PaperMap.mount($("map"), {
      api: "", column: true, live: true, editable: true, me: S.me, papers: S.papers, head: S.graphs.head,
      onOpen: (id) => openPaper(id, S.map.current()), onClose: mapClose, onShow: mapShowed, onData: mapData,
      onList: (r) => {
        S.graphs.setList(r);
        listIn();
        // a graph the address names that is not there (deleted, or never was): where the page lands
        if (S.graph && !S.graphs.item(S.graph) && S.view !== "settings" && S.view !== "listening") nav(hashFor(S.graphs.landing()), false);
      },
    }) || {};
    if ($("map").hidden && S.map.hide) S.map.hide();
    connect();          // first: nothing that changes while the library loads is missed
    loadQueue();
    S.ready = Promise.all([loadLibrary(), listReady]);
    openFromHash();
    S.tour = window.PaperTour ? window.PaperTour.mount(tourCtx()) : null;
  }
  document.addEventListener("DOMContentLoaded", start);
})();
