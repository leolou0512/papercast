// papercast-group: the tour a new person gets after their first sign-in, "Tour again" for three
// days after it, and the "?" help with the commands for adding papers. hub/tour.py keeps each
// person's tour state, so a tour finished or skipped on the laptop never comes back on the phone.
// Each step darkens the page except one real control: a hole in the scrim that follows the
// control's box as it scrolls or moves (never a copy of it), with an arrow and a few words beside
// it. A step opens what it needs first (a paper, its ⋯ menu, the map); a step whose control is
// not there for this person is left out; at the end the page is put back as it was. Escape skips,
// the arrow keys go back and on, and the keyboard stays in the tour. app.js mounts it
// (window.PaperTour.mount(ctx)). Text goes in as text; the only markup is this file's own icons.
"use strict";
(() => {
  const $ = (id) => document.getElementById(id);
  const PAD = 6;            // the hole is this much bigger than the control, all round
  const GAP = 44;           // between the hole and the words: the arrow's room
  const EDGE = 12;          // nothing closer than this to the screen's edge
  const reduced = () => window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const store = {
    get(k) { try { return localStorage.getItem(k); } catch (e) { return null; } },
    put(k, v) { try { if (v === null || v === undefined) localStorage.removeItem(k); else localStorage.setItem(k, v); } catch (e) { /* private mode */ } },
  };
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
  const CLOSE = `<svg width="20" height="20" viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" aria-hidden="true"><path d="m5 5 10 10M15 5 5 15"/></svg>`;
  const REPO = "git+https://github.com/leolou0512/papercast#subdirectory=packages/papercast-cli";

  // A control that is on the page now: not hidden, laid out, visible.
  function shown(e) {
    if (!e || !e.isConnected || e.closest("[hidden]") || !e.getClientRects().length) return null;
    return getComputedStyle(e).visibility === "visible" ? e : null;
  }
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  async function waitFor(fn, ms) {
    const end = Date.now() + ms;
    for (;;) {
      let v = null;
      try { v = fn(); } catch (e) { v = null; }
      if (v) return v;
      if (Date.now() >= end) return null;
      await sleep(50);
    }
  }
  // Until the control stops moving (the phone's window sliding in), at most `ms`.
  async function still(e, ms) {
    const end = Date.now() + ms, box = () => { const r = e.getBoundingClientRect(); return `${r.left},${r.top},${r.width},${r.height}`; };
    let last = box(), same = 0;
    while (same < 2 && Date.now() < end) {
      await new Promise((r) => requestAnimationFrame(r));
      const now = box();
      same = now === last ? same + 1 : 0;
      last = now;
    }
  }
  const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
  const hit = (a, b) => a.l < b.r && b.l < a.r && a.t < b.b && b.t < a.b;

  function mount(ctx) {
    const me = () => ctx.me() || {};
    const maker = () => me().role === "contributor" || me().role === "admin";

    // ================================================================ "?": the help
    const helpBtn = $("help-btn");
    const H = { node: null, timers: new Map() };
    function server() {
      const u = (ctx.cfg() || {}).public_url;
      return typeof u === "string" && /^https?:\/\//.test(u) ? u.replace(/\/+$/, "") : location.origin;
    }
    function copyBtn(text, what, code) {
      const b = el("button", { type: "button", class: "text-btn help-copy", text: "Copy", "aria-label": `Copy the ${what} command` });
      b.addEventListener("click", async () => {
        let ok = false;
        try { await navigator.clipboard.writeText(text); ok = true; } catch (e) {
          // no clipboard here (an old browser, no permission): the command is selected instead
          try {
            const r = document.createRange();
            r.selectNodeContents(code);
            const sel = window.getSelection();
            sel.removeAllRanges(); sel.addRange(r);
            ok = document.execCommand("copy");
          } catch (x) { ok = false; }
        }
        b.textContent = ok ? "Copied" : "Selected";
        clearTimeout(H.timers.get(b));
        H.timers.set(b, setTimeout(() => { b.textContent = "Copy"; }, 1600));
      });
      return b;
    }
    function helpKids() {
      const head = el("div", { class: "help-head" }, el("h2", { class: "help-h", id: "help-h", text: "Add papers" }),
        el("button", { type: "button", class: "icon-btn help-x", id: "help-close", "aria-label": "Close", html: CLOSE, onclick: () => closeHelp(true) }));
      if (!maker()) return [head, el("p", { class: "help-note help-only", id: "help-viewer", text: "Uploading needs contributor access: ask an admin." })];
      const rows = [
        ["install", `pipx install '${REPO}'`, ""],
        ["log in", `papercast login --server ${server()}`, ""],
        ["add", "papercast add paper.pdf", "or an arXiv / DOI link"],
        ["status", "papercast status", ""],
      ].map(([what, cmd, note]) => {
        const code = el("code", { class: "help-code", text: cmd });
        return el("li", { class: "help-cmd", "data-cmd": what }, el("div", { class: "help-line" }, code, copyBtn(cmd, what, code)),
          note ? el("p", { class: "help-note", text: note }) : null);
      });
      return [head, el("ol", { class: "help-cmds", id: "help-cmds" }, rows),
        el("p", { class: "help-note help-claude", text: "Needs Claude Code, installed and logged in." })];
    }
    function placeHelp() {
      if (!H.node || H.node.hidden) return;
      const r = helpBtn.getBoundingClientRect(), w = H.node.offsetWidth;
      H.node.style.top = `${Math.round(Math.max(8, r.bottom + 6))}px`;
      H.node.style.left = `${Math.round(clamp(r.right - w, 8, window.innerWidth - w - 8))}px`;
    }
    function openHelp() {
      if (!H.node) {
        H.node = el("div", { class: "help", id: "help", role: "dialog", "aria-labelledby": "help-h", tabindex: "-1", hidden: true });
        document.body.append(H.node);
      }
      H.node.replaceChildren(...helpKids());      // drawn each time: the role may have changed meanwhile
      H.node.hidden = false;
      helpBtn.setAttribute("aria-expanded", "true");
      placeHelp();
      H.node.focus({ preventScroll: true });
    }
    function closeHelp(focusBack) {
      if (!H.node || H.node.hidden) return;
      H.node.hidden = true;
      helpBtn.setAttribute("aria-expanded", "false");
      if (focusBack) helpBtn.focus({ preventScroll: true });
    }
    if (helpBtn) {
      helpBtn.addEventListener("click", (e) => { e.stopPropagation(); if (H.node && !H.node.hidden) closeHelp(true); else openHelp(); });
      document.addEventListener("pointerdown", (e) => {
        if (H.node && !H.node.hidden && !H.node.contains(e.target) && !helpBtn.contains(e.target)) closeHelp(false);
      }, true);
      window.addEventListener("keydown", (e) => {
        if (e.key !== "Escape" || !H.node || H.node.hidden || T.on) return;
        e.stopPropagation(); e.preventDefault();
        closeHelp(true);
      }, true);
      window.addEventListener("resize", placeHelp);
      $("list-pane").addEventListener("scroll", placeHelp, { passive: true });
    }

    // ================================================================ the tour
    const T = { on: false, seq: 0, plan: [], i: -1, target: null, busy: false, raf: 0, key: "", glideT: null, waitT: null,
      before: null, replay: false, audio: null, paper: null, state: null, againT: null, autoDone: false, parked: false };
    const N = {};
    const SVG = "http://www.w3.org/2000/svg";

    // What the steps open, and how they put it away. The map's back entry in the history goes
    // before anything else touches the address, or its popstate would come after and undo it.
    async function mapClosed() {
      if (ctx.view() !== "map") return;
      ctx.closeMap();
      await waitFor(() => !(history.state && history.state.pmap), 600);
    }
    async function onList() {
      ctx.closeMenu();
      await mapClosed();
      if (ctx.phone()) ctx.list();          // on a phone the list is under the window
    }
    async function inPaper(pid) {
      ctx.closeMenu();
      await mapClosed();
      if (!pid) return;
      if (!(ctx.view() === "paper" && ctx.openId() === pid)) ctx.open(pid);
      await waitFor(() => ctx.view() === "paper" && ctx.openId() === pid, 1500);
    }
    function openMore() {
      if (document.querySelector(".menu")) return;
      const b = shown($("w-more")) || shown($("w-more-phone"));
      if (b) b.click();
    }
    const upNext = () => [...document.querySelectorAll(".menu [role=menuitem]")].find((b) => /Up next$/.test(b.textContent.trim())) || null;
    const linkTo = () => shown(document.querySelector("#map .pm-card .pm-linkto"));
    // The map, with a paper picked in a graph this person may change, so its card shows Link to…
    async function inMap() {
      ctx.closeMenu();
      if (ctx.view() !== "map") await ctx.openMap();
      const m = await waitFor(() => { const x = ctx.map(); return x && typeof x.select === "function" && document.querySelector("#map .pm-tab") ? x : null; }, 4000);
      if (!m || linkTo()) return;
      let gs;
      try { gs = await ctx.api("GET", "/api/graphs"); } catch (e) { return; }
      const list = (Array.isArray(gs) ? gs : (gs && gs.graphs) || []).filter((g) => g && typeof g.id === "string" && g.n > 0 && g.can_edit !== false);
      for (const g of list.slice(0, 6)) {
        let d;
        try { d = await ctx.api("GET", `/api/graphs/${encodeURIComponent(g.id)}`); } catch (e) { continue; }
        const ids = ((d && d.nodes) || []).map((n) => n && n.id).filter(Boolean);
        if (!ids.length) continue;
        if (!T.on || ctx.view() !== "map") return;
        m.select(ids.includes(T.paper) ? T.paper : ids[0], g.id);
        return;
      }
    }

    // At most ten, in the order a newcomer meets them. `need`: a paper with audio, or any paper.
    const STEPS = [
      { id: "search", text: () => "Search inside papers, or filter", prep: onList,
        at: () => shown(document.querySelector("#list-pane .search")) },
      { id: "play", need: "audio", text: () => "Play the episode", prep: () => inPaper(T.audio),
        at: () => shown($("p-play")) },
      { id: "listened", need: "audio", text: () => "Tick it when you’ve listened", prep: () => inPaper(T.audio),
        at: () => shown($("w-listened")) },
      { id: "upnext", need: "audio", text: () => (/^Add/.test((upNext() || {}).textContent || "") ? "Add it to Up next" : "Up next"),
        prep: async () => { await inPaper(T.audio); openMore(); }, at: upNext },
      { id: "transcript", need: "paper", text: () => "Tap a sentence to play from there", prep: () => inPaper(T.paper), wait: 2500,
        at: () => ($("tr-body").classList.contains("live") ? shown(document.querySelector("#tr-body .tr-p .tr-s")) : null) },
      { id: "comments", need: "paper", text: () => "Comment on the paper", prep: () => inPaper(T.paper), wait: 2000,
        at: () => shown($("c-text")) },
      { id: "map", text: () => "The map of how papers connect", prep: onList, at: () => shown($("map-btn")) },
      { id: "link", need: "paper", text: () => "Link two papers", prep: inMap, wait: 3000, at: linkTo },
      { id: "settings", text: () => (maker() ? "Your voice and preferences" : "Your settings"), prep: onList, at: () => shown($("set-btn")) },
      { id: "help", text: () => (maker() ? "How to add papers" : "Help"), prep: onList, at: () => shown(helpBtn) },
    ];

    function build() {
      N.block = el("div", { class: "tour-block" });
      N.hole = el("div", { class: "tour-hole" });
      N.svg = document.createElementNS(SVG, "svg");
      N.svg.setAttribute("class", "tour-arrow");
      N.svg.setAttribute("aria-hidden", "true");
      N.line = document.createElementNS(SVG, "path");
      N.head = document.createElementNS(SVG, "path");
      N.svg.append(N.line, N.head);
      N.text = el("p", { class: "tour-t", id: "tour-t", "aria-live": "polite" });
      N.count = el("span", { class: "tour-n t", id: "tour-n" });
      N.back = el("button", { type: "button", class: "text-btn tour-back", id: "tour-back", text: "Back", onclick: back });
      N.next = el("button", { type: "button", class: "btn-accent tour-next", id: "tour-next", text: "Next", onclick: next });
      N.card = el("div", { class: "tour-card", id: "tour-card", tabindex: "-1", hidden: true },
        N.text, el("div", { class: "tour-row" }, N.count, N.back, N.next));
      N.skip = el("button", { type: "button", class: "tour-skip at-rm", id: "tour-skip", text: "Skip tour", onclick: () => end("skipped") });
      // one modal dialog: the words, Back and Next, and Skip (the rest of the page is out of reach)
      N.root = el("div", { class: "tour", id: "tour", role: "dialog", "aria-modal": "true", "aria-labelledby": "tour-t" },
        N.block, N.hole, N.svg, N.card, N.skip);
      // nothing in the tour reaches the page under it (the app closes menus on a click elsewhere)
      for (const ev of ["click", "pointerdown", "pointerup", "mousedown", "mouseup", "touchstart", "touchend", "dblclick", "contextmenu"]) {
        N.root.addEventListener(ev, (e) => e.stopPropagation());
      }
      N.block.addEventListener("wheel", (e) => e.preventDefault(), { passive: false });
      N.block.addEventListener("touchmove", (e) => e.preventDefault(), { passive: false });
      // the spotlight opens from the middle of the screen
      Object.assign(N.hole.style, { left: `${window.innerWidth / 2}px`, top: `${window.innerHeight / 2}px`, width: "0px", height: "0px" });
      document.body.append(N.root);
    }
    const inTour = (e) => !!(e && N.root && N.root.contains(e));
    // The words themselves (no ring round a button nobody has reached for yet), or the button the
    // keyboard is on.
    function focusIn() {
      const a = document.activeElement;
      if ((a === N.back || a === N.next || (a === N.skip && !T.parked)) && !a.disabled) return;
      T.parked = N.card.hidden;              // on Skip only until the first step's words are up
      (N.card.hidden ? N.skip : N.card).focus({ preventScroll: true });
    }

    function onKey(e) {
      if (!T.on) return;
      e.stopPropagation();            // the page's own keys (/, Escape, the map's Ctrl+Z…) wait
      if (e.type !== "keydown") return;
      const k = e.key;
      if (k === "Escape") { e.preventDefault(); end("skipped"); }
      else if (k === "ArrowRight" || k === "ArrowDown") { e.preventDefault(); next(); }
      else if (k === "ArrowLeft" || k === "ArrowUp") { e.preventDefault(); back(); }
      else if (k === "Tab") {
        e.preventDefault();
        const f = [N.back, N.next, N.skip].filter((b) => !b.disabled && !(b !== N.skip && N.card.hidden));
        const at = f.indexOf(document.activeElement);
        T.parked = false;
        f[(at + (e.shiftKey ? -1 : 1) + f.length) % f.length].focus({ preventScroll: true });
      } else if ((k === "Enter" || k === " ") && !(document.activeElement instanceof HTMLButtonElement && inTour(document.activeElement))) {
        e.preventDefault(); next();
      }
    }
    function onFocus(e) { if (T.on && !inTour(e.target)) focusIn(); }

    async function run(replay) {
      if (T.on) return;
      closeHelp(false);
      const papers = ctx.papers() || [];
      const audio = papers.find((p) => ctx.hasAudio(p));
      T.audio = audio ? audio.id : null;
      T.paper = T.audio || (papers[0] ? papers[0].id : null);
      T.plan = STEPS.filter((s) => !s.need || (s.need === "audio" ? T.audio : T.paper));
      T.before = { hash: location.hash, last: store.get("pcg.last"), tab: store.get("pcg.map.tab"),
        listTop: $("list-pane").scrollTop, winTop: $("win").scrollTop, focus: document.activeElement };
      T.on = true; T.replay = replay; T.i = -1; T.target = null; T.key = "";
      build();
      document.body.classList.add("touring");
      window.addEventListener("keydown", onKey, true);
      window.addEventListener("keyup", onKey, true);
      document.addEventListener("focusin", onFocus, true);
      drawAgain();
      focusIn();
      // a replay is not written: only its end is, so it never makes the tour start by itself again
      if (!replay) ctx.api("PUT", "/api/tour", { state: "started" }).then((st) => { T.state = st; }, () => {});
      T.raf = requestAnimationFrame(frame);
      await go(0, 1);
    }

    // Step i, or the nearest one after it (before it, going back) whose control is there; a step
    // whose control is missing leaves the plan.
    async function go(i, dir) {
      const my = ++T.seq;
      T.busy = true;
      clearTimeout(T.waitT);
      T.waitT = setTimeout(() => { if (T.busy && my === T.seq && N.root) N.root.classList.add("tour-wait"); }, 250);
      for (;;) {
        if (i < 0) { i = 0; dir = 1; }
        if (i >= T.plan.length) { T.busy = false; end("finished"); return; }
        const s = T.plan[i];
        try { await s.prep(); } catch (e) { /* its control is looked for all the same */ }
        if (my !== T.seq || !T.on) return;
        const t = await waitFor(s.at, s.wait || 1200);
        if (t) await still(t, 600);
        if (my !== T.seq || !T.on) return;
        if (t) { show(i, t); return; }
        T.plan.splice(i, 1);
        if (dir < 0) i--;
      }
    }
    function next() {
      if (!T.on || T.busy) return;
      if (T.i >= T.plan.length - 1) end("finished"); else go(T.i + 1, 1);
    }
    function back() {
      if (!T.on || T.busy || T.i <= 0) return;
      go(T.i - 1, -1);
    }

    // Wholly in view (and in its scrolling pane, clear of the window's slim player bar)?
    function inView(t) {
      const r = t.getBoundingClientRect();
      if (r.top < EDGE || r.bottom > window.innerHeight - EDGE || r.left < 0 || r.right > window.innerWidth) return false;
      for (let a = t.parentElement; a && a !== document.body; a = a.parentElement) {
        if (a.scrollHeight <= a.clientHeight || !/(auto|scroll)/.test(getComputedStyle(a).overflowY)) continue;
        const ar = a.getBoundingClientRect(), top = a.id === "win" ? 64 : 8;
        if (r.top < ar.top + top || r.bottom > ar.bottom - 8) return false;
      }
      return true;
    }
    function show(i, t) {
      T.i = i; T.target = t; T.key = ""; T.busy = false;
      clearTimeout(T.waitT);
      N.root.classList.remove("tour-wait");
      const s = T.plan[i];
      N.text.textContent = s.text();
      N.count.textContent = `${i + 1} / ${T.plan.length}`;
      N.back.disabled = i === 0;
      N.next.textContent = i === T.plan.length - 1 ? "Done" : "Next";
      N.root.dataset.step = s.id;
      if (!inView(t)) t.scrollIntoView({ block: "center", inline: "nearest" });     // at once: the spotlight glides instead
      const first = N.card.hidden;
      N.card.hidden = false;
      if (!reduced()) {
        N.root.classList.add("glide");
        clearTimeout(T.glideT);
        T.glideT = setTimeout(stopGlide, first ? 420 : 320);
      }
      frame(true);
      focusIn();
    }

    // The glide over: a transition still chasing a control that moved under it (the phone's window
    // sliding in) ends where the control is now, and from here on the spotlight follows it at once.
    function stopGlide() {
      if (!N.root) return;
      N.root.classList.remove("glide");
      for (const e of [N.hole, N.card]) {
        for (const a of (e.getAnimations ? e.getAnimations() : [])) if (a.transitionProperty) a.finish();
      }
    }
    const moving = () => !!(N.root && (N.root.classList.contains("glide") ||
      [N.hole, N.card].some((e) => e.getAnimations && e.getAnimations().some((a) => a.playState === "running"))));

    // Every frame: the control's box, and when it moved (a scroll, a resize, the phone's window
    // sliding in) the hole, the words, the arrow and Skip go with it.
    function frame(once) {
      if (!T.on) return;
      if (once !== true) T.raf = requestAnimationFrame(frame);
      let t = T.target;
      if (!t) return;
      if (!t.isConnected) {                     // drawn again (the list, a menu): the same control, new node
        const again = T.plan[T.i] && T.plan[T.i].at();
        if (!again) return;
        t = T.target = again;
      }
      const r = t.getBoundingClientRect();
      const key = [r.left, r.top, r.width, r.height, window.innerWidth, window.innerHeight, N.card.offsetWidth, N.card.offsetHeight].join();
      if (key === T.key) return;
      T.key = key;
      place(r);
    }
    function place(r) {
      const vw = window.innerWidth, vh = window.innerHeight;
      const h = { l: r.left - PAD, t: r.top - PAD, r: r.right + PAD, b: r.bottom + PAD };
      Object.assign(N.hole.style, { left: `${h.l}px`, top: `${h.t}px`, width: `${h.r - h.l}px`, height: `${h.b - h.t}px` });
      // the words: under the control, else over it, else beside it, else low on the screen
      const cw = N.card.offsetWidth, ch = N.card.offsetHeight, cx = (h.l + h.r) / 2, cy = (h.t + h.b) / 2;
      const X = (x) => clamp(x, EDGE, vw - cw - EDGE), Y = (y) => clamp(y, EDGE, vh - ch - EDGE);
      let side, x, y;
      if (vh - h.b >= ch + GAP + EDGE) { side = "below"; x = X(cx - cw / 2); y = h.b + GAP; }
      else if (h.t >= ch + GAP + EDGE) { side = "above"; x = X(cx - cw / 2); y = h.t - GAP - ch; }
      else if (vw - h.r >= cw + GAP + EDGE) { side = "right"; x = h.r + GAP; y = Y(cy - ch / 2); }
      else if (h.l >= cw + GAP + EDGE) { side = "left"; x = h.l - GAP - cw; y = Y(cy - ch / 2); }
      else { side = "over"; x = X(cx - cw / 2); y = Y(vh - ch - 72); }
      N.card.style.left = `${Math.round(x)}px`;
      N.card.style.top = `${Math.round(y)}px`;
      const c = { l: x, t: y, r: x + cw, b: y + ch };
      arrow(side, h, c);
      skipAt(h, c);
    }
    // From the words' edge to the hole's, a head at the hole.
    function arrow(side, h, c) {
      let a = null, b = null;
      if (side === "below" || side === "above") {
        const x0 = clamp((h.l + h.r) / 2, c.l + 20, c.r - 20), x1 = clamp(x0, h.l + 10, h.r - 10);
        a = [x0, side === "below" ? c.t - 6 : c.b + 6];
        b = [x1, side === "below" ? h.b + 6 : h.t - 6];
      } else if (side === "right" || side === "left") {
        const y0 = clamp((h.t + h.b) / 2, c.t + 16, c.b - 16), y1 = clamp(y0, h.t + 8, h.b - 8);
        a = [side === "right" ? c.l - 6 : c.r + 6, y0];
        b = [side === "right" ? h.r + 6 : h.l - 6, y1];
      }
      if (!a) { N.line.setAttribute("d", ""); N.head.setAttribute("d", ""); return; }
      const dx = b[0] - a[0], dy = b[1] - a[1], len = Math.hypot(dx, dy) || 1, ux = dx / len, uy = dy / len;
      const k = 10, co = Math.cos(Math.PI / 6), si = Math.sin(Math.PI / 6);
      const p1 = [b[0] - k * (ux * co - uy * si), b[1] - k * (uy * co + ux * si)];
      const p2 = [b[0] - k * (ux * co + uy * si), b[1] - k * (uy * co - ux * si)];
      const f = (p) => `${p[0].toFixed(1)} ${p[1].toFixed(1)}`;
      N.line.setAttribute("d", `M${f(a)} L${f(b)}`);
      N.head.setAttribute("d", `M${f(p1)} L${f(b)} L${f(p2)}`);
    }
    // Skip tour stays on the side, where the hole and the words leave room.
    function skipAt(h, c) {
      const vw = window.innerWidth, vh = window.innerHeight, w = N.skip.offsetWidth, ht = N.skip.offsetHeight, m = 12;
      const box = { l: h.l - 8, t: h.t - 8, r: h.r + 8, b: h.b + 8 }, mid = Math.round((vh - ht) / 2);
      const spots = { rm: { l: vw - m - w, t: mid }, br: { l: vw - m - w, t: vh - m - ht }, tr: { l: vw - m - w, t: m },
        lm: { l: m, t: mid }, bl: { l: m, t: vh - m - ht }, tl: { l: m, t: m } };
      let pick = "rm";
      for (const [k, p] of Object.entries(spots)) {
        const s = { l: p.l, t: p.t, r: p.l + w, b: p.t + ht };
        if (!hit(s, box) && !hit(s, c)) { pick = k; break; }
      }
      N.skip.className = `tour-skip at-${pick}`;
    }

    async function end(how) {
      if (!T.on) return;
      T.on = false; T.seq++; T.busy = false;
      cancelAnimationFrame(T.raf);
      clearTimeout(T.glideT); clearTimeout(T.waitT);
      window.removeEventListener("keydown", onKey, true);
      window.removeEventListener("keyup", onKey, true);
      document.removeEventListener("focusin", onFocus, true);
      N.root.remove();
      document.body.classList.remove("touring");
      try { await restore(); } catch (e) { /* the state is written all the same */ }
      let st = null;
      try { st = await ctx.api("PUT", "/api/tour", { state: how }); } catch (e) { /* the hub still has it as started */ }
      if (st) T.state = st;
      drawAgain();
    }
    // The page as it was before the tour: what was open, where the lists were scrolled, the paper
    // a wide screen reopens next time, the map's graph.
    async function restore() {
      const b = T.before;
      ctx.closeMenu();
      await mapClosed();
      if (b.hash !== location.hash) {
        const m = /^#p=(p_[a-z0-9]{4,32})/.exec(b.hash);
        if (!b.hash) ctx.list(); else if (m) ctx.open(m[1]); else location.hash = b.hash.slice(1);
      }
      store.put("pcg.last", b.last);
      store.put("pcg.map.tab", b.tab);
      $("list-pane").scrollTop = b.listTop;
      $("win").scrollTop = b.winTop;
      const f = b.focus;
      if (f && f !== document.body && f.isConnected && shown(f)) f.focus({ preventScroll: true });
      else if (document.activeElement && document.activeElement !== document.body) document.activeElement.blur();
    }

    // ================================================================ Tour again
    const again = el("button", { type: "button", class: "tour-again", id: "tour-again", text: "Tour again", hidden: true, onclick: () => run(true) });
    document.body.append(again);
    function drawAgain() {
      const st = T.state, on = !!(st && st.again) && !T.on;
      again.hidden = !on;
      clearTimeout(T.againT);
      if (on && st.again_left_s > 0) {
        // gone once its three days are up, even on a page left open
        T.againT = setTimeout(() => { st.again = false; drawAgain(); }, Math.min(st.again_left_s * 1000 + 500, 2 ** 31 - 1));
      }
    }

    // It starts by itself on the library, the first time; opened elsewhere, when the person gets there.
    function maybeAuto() {
      if (!T.state || !T.state.auto || T.on || T.autoDone) return;
      if (ctx.view() !== "list" || !ctx.idle()) return;
      T.autoDone = true;
      run(false);
    }
    ctx.api("GET", "/api/tour").then((st) => {
      T.state = st;
      drawAgain();
      if (!st || !st.auto) return;
      setTimeout(maybeAuto, 400);
      const wait = setInterval(() => { if (T.autoDone || !T.state || !T.state.auto) clearInterval(wait); else maybeAuto(); }, 1000);
    }, () => { /* no tour state: no tour */ });

    const api = {
      start: () => run(true),
      skip: () => end("skipped"),
      openHelp, closeHelp,
      // for the browser tests
      state() {
        const rect = (e) => { if (!e) return null; const r = e.getBoundingClientRect(); return { l: r.left, t: r.top, w: r.width, h: r.height }; };
        const s = T.on && T.i >= 0 ? T.plan[T.i] : null;
        return { on: T.on, busy: T.busy, id: s ? s.id : null, i: T.i, n: T.plan.length, plan: T.plan.map((x) => x.id),
          text: T.on && N.text ? N.text.textContent : null, target: T.on && s ? rect(T.target) : null, hole: T.on ? rect(N.hole) : null,
          card: T.on && N.card && !N.card.hidden ? rect(N.card) : null, gliding: T.on && moving(),
          again: !again.hidden, hub: T.state };
      },
    };
    window.PaperTour.current = api;
    return api;
  }

  window.PaperTour = { mount };
})();
