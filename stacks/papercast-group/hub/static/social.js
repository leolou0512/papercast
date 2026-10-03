// papercast-group: comments on each paper and the board of what happened (hub/social.py).
// app.js mounts it (window.PaperSocial.mount(ctx)) and tells it what the page does: the paper
// that is open, the version that plays, the live events. Whatever a person wrote goes in as text
// nodes, never as HTML: the times in it ("12:40", "1:02:03") and its web addresses become links
// built as DOM nodes, and only http(s) addresses ever do.
"use strict";
(() => {
  const BODY_MAX = 2000, NOTICE_MAX = 280;
  const BOARD_FIRST = 5, BOARD_STEP = 20;
  const MIN = 60000, HOUR = 3600000, DAY = 86400000;
  const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  const $ = (id) => document.getElementById(id);
  const tab = {
    get(k) { try { return sessionStorage.getItem(k); } catch (e) { return null; } },
    set(k, v) { try { sessionStorage.setItem(k, v); } catch (e) { /* private mode */ } },
    del(k) { try { sessionStorage.removeItem(k); } catch (e) { /* private mode */ } },
  };
  const store = {
    get(k) { try { return localStorage.getItem(k); } catch (e) { return null; } },
    set(k, v) { try { localStorage.setItem(k, v); } catch (e) { /* private mode */ } },
  };

  // Text only: no attribute here ever takes HTML.
  function el(tag, attrs, ...kids) {
    const e = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v === null || v === undefined || v === false) continue;
      if (k === "class") e.className = v;
      else if (k === "text") e.textContent = v;
      else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
      else e.setAttribute(k, v === true ? "" : v);
    }
    for (const c of kids.flat()) if (c !== null && c !== undefined && c !== false) e.append(c);
    return e;
  }

  // ------------------------------------------------------------------ times and links in text
  // A time: 12:40 or 1:02:03, standing on its own ("12:40pm", "10:20:30:40", "3.12:40" are not).
  const TIME_RX = /(^|[^\w:.])(?:(\d{1,2}):([0-5]\d):([0-5]\d)|(\d{1,2}):([0-5]\d))(?![\w:]|\.\d|\s?[ap]\.?m\b)/gi;
  const URL_RX = /\bhttps?:\/\/[^\s<>"'`]+/gi;
  function trimUrl(u) {
    for (;;) {
      const last = u.slice(-1);
      if (".,;:!?*".includes(last)) u = u.slice(0, -1);
      else if (last === ")" && (u.match(/\(/g) || []).length < (u.match(/\)/g) || []).length) u = u.slice(0, -1);
      else if (last === "]" && (u.match(/\[/g) || []).length < (u.match(/\]/g) || []).length) u = u.slice(0, -1);
      else return u;
    }
  }
  function safeUrl(u) {
    try { const x = new URL(u); return x.protocol === "https:" || x.protocol === "http:" ? x.href : null; } catch (e) { return null; }
  }
  function withTimes(frag, s, timeLink) {
    if (!timeLink || !s) { if (s) frag.append(s); return; }
    let last = 0, m;
    TIME_RX.lastIndex = 0;
    while ((m = TIME_RX.exec(s))) {
      const at = m.index + m[1].length, label = m[0].slice(m[1].length);
      const t = m[2] !== undefined ? Number(m[2]) * 3600 + Number(m[3]) * 60 + Number(m[4]) : Number(m[5]) * 60 + Number(m[6]);
      const a = timeLink(t, label);
      if (!a) continue;
      frag.append(s.slice(last, at), a);
      last = at + label.length;
    }
    frag.append(s.slice(last));
  }
  // A person's text as nodes: web addresses as links that open elsewhere, and, when timeLink is
  // given, times as the links it makes (or plain text where it makes none).
  function richText(text, timeLink) {
    const frag = document.createDocumentFragment();
    let last = 0;
    for (const m of String(text || "").matchAll(URL_RX)) {
      const u = trimUrl(m[0]), href = safeUrl(u);
      if (!href) continue;
      withTimes(frag, text.slice(last, m.index), timeLink);
      frag.append(el("a", { class: "c-url", href, target: "_blank", rel: "noopener noreferrer", text: u }));
      last = m.index + u.length;
    }
    withTimes(frag, String(text || "").slice(last), timeLink);
    return frag;
  }

  const pad = (n) => String(n).padStart(2, "0");
  function stamp(iso) {
    const d = new Date(iso || "");
    return isNaN(d) ? "" : `${d.getDate()} ${MONTHS[d.getMonth()]} ${d.getFullYear()}, ${pad(d.getHours())}:${pad(d.getMinutes())}`;
  }

  function mount(ctx) {
    const me = () => ctx.me() || {};
    const admin = () => !!ctx.isAdmin();
    // a person's picture, or their initials (avatar.js, through app.js); an empty text without it
    const face = (u, size) => (u && ctx.avatar ? ctx.avatar(u, size) : document.createTextNode(""));
    let offset = 0;                 // the hub's clock minus this device's (ms), from the board's "now"
    const now = () => Date.now() + offset;
    const ms = (iso) => { const t = Date.parse(iso || ""); return isNaN(t) ? 0 : t; };
    function ago(iso) {
      const t = ms(iso);
      if (!t) return "";
      const d = Math.max(0, now() - t);
      if (d < MIN) return "just now";
      if (d < HOUR) return `${Math.floor(d / MIN)} min ago`;
      if (d < DAY) return `${Math.floor(d / HOUR)} h ago`;
      if (d < 2 * DAY) return "yesterday";
      if (d < 7 * DAY) return `${Math.floor(d / DAY)} d ago`;
      const x = new Date(t), y = new Date(now());
      return `${x.getDate()} ${MONTHS[x.getMonth()]}${x.getFullYear() !== y.getFullYear() ? ` ${x.getFullYear()}` : ""}`;
    }
    const whenNode = (iso, cls) => el("span", { class: `${cls || "when"} t`, "data-ago": iso, title: stamp(iso), text: ago(iso) });
    // every relative time this file drew, once in a while
    function retime() {
      for (const n of document.querySelectorAll("[data-ago]")) {
        const v = ago(n.dataset.ago);
        if (n.textContent !== v) n.textContent = v;
      }
    }

    // ================================================================ comments
    // One discussion per paper; each comment names the version it was written about, so its
    // times play that version. Newest last, the box to write in under them: on a wide screen a
    // column right of the transcript, its list scrolling on its own (it opens at the newest).
    const C = {
      pid: null, p: null, chosen: null, eps: new Map(), list: new Map(), loaded: false, seq: 0, pending: null, fresh: false,
      nodes: new Map(), replyTo: null, editing: null, armed: null, armTimer: null, focus: null, counts: new Map(),
    };
    const sec = el("section", { class: "comments", id: "comments", hidden: true, "aria-labelledby": "c-h" });
    const cHead = el("h3", { class: "sec-h", id: "c-h", text: "Comments" });
    const atBtn = el("button", { type: "button", class: "text-btn c-at", id: "c-at", hidden: true });
    const cList = el("ul", { class: "c-list", id: "c-list" });
    const cText = el("textarea", { class: "field note c-text", id: "c-text", maxlength: String(BODY_MAX), rows: "3",
      placeholder: "Write a comment", "aria-label": "Write a comment" });
    const cPost = el("button", { type: "button", class: "btn-accent", id: "c-post", text: "Comment", disabled: true });
    const cMsg = el("span", { class: "c-msg", id: "c-msg", role: "status" });
    sec.append(el("div", { class: "col" }, el("div", { class: "c-head" }, cHead, atBtn), cList,
      el("div", { class: "c-compose" }, cText, el("div", { class: "c-row" }, cPost, cMsg))));
    $("paper").append(sec);

    const draftKey = (pid, what) => `pcg.${me().id !== undefined ? me().id : 0}.c.${pid}.${what}`;
    // A box to write in: its text survives a reload of the page (this tab only), and Ctrl or
    // Cmd + Enter sends it.
    function wireBox(ta, key, send, btn) {
      const saved = key ? tab.get(key) : null;
      if (saved && !ta.value) ta.value = saved;
      const sync = () => {
        if (btn) btn.disabled = !ta.value.trim();
        if (key) { if (ta.value) tab.set(key, ta.value); else tab.del(key); }
      };
      ta.addEventListener("input", () => { ctx.typed(); sync(); });
      ta.addEventListener("keydown", (e) => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); send(); } });
      sync();
      return sync;
    }
    let syncMain = () => {};

    function canDelete(c) { return !c.deleted && !!c.user && (c.user.id === me().id || admin()); }
    function mineC(c) { return !c.deleted && !!c.user && c.user.id === me().id; }
    // The version whose audio a comment's times are in: the one it names, else the one that plays.
    function epOf(c) { return c.episode_id ? C.eps.get(c.episode_id) || null : C.chosen; }
    function versionNote(c) {
      const eps = (C.p && C.p.episodes) || [];
      if (!c.episode_id || eps.length < 2 || (C.chosen && C.chosen.id === c.episode_id)) return "";
      const e = C.eps.get(c.episode_id);
      if (!e) return "on a deleted version";
      return e.mine ? "on your version" : `on ${(e.made_by && e.made_by.name) || "someone"}’s version`;
    }
    function timeLinker(c) {
      const e = epOf(c);
      if (!e || !e.has_audio) return null;
      const dur = Number(e.duration_s) || 0;
      return (t, label) => (dur && t > dur + 1 ? null : el("a", {
        class: "c-time t", href: `#p=${encodeURIComponent(C.pid)}&t=${t}`, "data-t": String(t), "data-ep": e.id,
        title: `Play from ${label}`, text: label,
        onclick: (ev) => { if (ev.button || ev.metaKey || ev.ctrlKey || ev.shiftKey) return; ev.preventDefault(); ctx.seek(e.id, t); },
      }));
    }

    function setCount(pid, n) {
      n = Math.max(0, Number(n) || 0);
      if ((C.counts.get(pid) || 0) === n) return;
      if (n) C.counts.set(pid, n); else C.counts.delete(pid);
      ctx.rowChanged(pid);
      if (pid === C.pid) drawHead();
    }
    function drawHead() {
      const n = C.counts.get(C.pid) || 0;
      cHead.textContent = n ? `Comments (${n})` : "Comments";
    }
    async function loadCounts() {
      let j;
      try { j = await ctx.api("GET", "/api/comments/counts"); } catch (e) { return; }
      const got = (j && j.counts) || {};
      for (const pid of [...C.counts.keys()]) if (!(pid in got)) setCount(pid, 0);
      for (const [pid, n] of Object.entries(got)) setCount(pid, n);
    }

    // The window shows paper p, with version c in its player (app.js's renderWin).
    function paper(p, c) {
      if (!p) return;
      C.p = p; C.chosen = c || null;
      C.eps = new Map((p.episodes || []).map((e) => [e.id, e]));
      if (p.id !== C.pid) {
        C.pid = p.id; C.list = new Map(); C.loaded = false; C.pending = []; C.fresh = true;
        for (const n of C.nodes.values()) n.root.remove();
        C.nodes.clear(); cList.replaceChildren();
        C.replyTo = null; C.editing = null; C.armed = null;
        cMsg.textContent = "";
        cText.value = tab.get(draftKey(p.id, "new")) || "";
        syncMain = wireMainBox();
        drawHead();
        load();
      }
      sec.hidden = false;
      render();
      drawAt();
    }
    let mainWired = false;
    function wireMainBox() {
      const key = () => draftKey(C.pid, "new");
      const sync = () => {
        cPost.disabled = !cText.value.trim();
        if (C.pid) { if (cText.value) tab.set(key(), cText.value); else tab.del(key()); }
      };
      if (!mainWired) {
        mainWired = true;
        cText.addEventListener("input", () => { ctx.typed(); sync(); if (cMsg.textContent) cMsg.textContent = ""; counter(); });
        cText.addEventListener("keydown", (e) => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); postNew(); } });
        cPost.addEventListener("click", postNew);
      }
      sync();
      return sync;
    }
    function counter() {
      const n = cText.value.length;
      if (n > BODY_MAX - 200) { cMsg.className = "c-msg t"; cMsg.textContent = `${n.toLocaleString("en")} / ${BODY_MAX.toLocaleString("en")}`; }
      else if (/\d \/ /.test(cMsg.textContent)) cMsg.textContent = "";
    }
    async function load() {
      const pid = C.pid, seq = ++C.seq;
      let j;
      try { j = await ctx.api("GET", `/api/papers/${encodeURIComponent(pid)}/comments`); }
      catch (e) { if (seq === C.seq) { cMsg.className = "c-msg err"; cMsg.textContent = e.message; } return; }
      if (seq !== C.seq || pid !== C.pid) return;
      C.list = new Map((j.comments || []).map((c) => [c.id, c]));
      C.loaded = true;
      for (const d of C.pending || []) take(d);        // what came live while the list was on its way
      C.pending = null;
      setCount(pid, [...C.list.values()].filter((c) => !c.deleted).length);
      render();
      if (C.fresh) { C.fresh = false; toEnd(); }
      showFocus();
    }
    // The newest in view (the list scrolls on its own only in the column).
    function toEnd() { cList.scrollTop = cList.scrollHeight; }
    function take(c) {
      if (!c || c.paper_id !== C.pid) return;
      const old = C.list.get(c.id);
      // an answer and an event can cross: an older state never replaces a newer one
      if (old && old.deleted && !c.deleted) return;
      C.list.set(c.id, c);
      if (c.deleted && C.editing === c.id) C.editing = null;
    }

    // Keyed, so a live comment touches only its own node: a box someone is typing in stays put.
    function place(parent, want) {
      let next = parent.firstElementChild;
      for (const n of want) {
        if (n === next) next = n.nextElementSibling; else parent.insertBefore(n, next);
      }
      while (next) { const x = next.nextElementSibling; next.remove(); next = x; }
    }
    function render() {
      if (!C.loaded) return;
      const all = [...C.list.values()].sort((a, b) => a.id - b.id);
      const tops = [], under = new Map();
      for (const c of all) {
        if (c.parent_id == null) tops.push(c);
        else { if (!under.has(c.parent_id)) under.set(c.parent_id, []); under.get(c.parent_id).push(c); }
      }
      const shown = new Set();
      const threads = [];
      for (const t of tops) {
        const replies = (under.get(t.id) || []).filter((r) => !r.deleted);
        if (t.deleted && !replies.length) continue;       // deleted, and nothing hangs under it
        shown.add(t.id);
        for (const r of replies) shown.add(r.id);
        threads.push(thread(t, replies));
      }
      for (const [id, n] of C.nodes) if (!shown.has(id)) { n.root.remove(); C.nodes.delete(id); }
      if (C.replyTo !== null && !shown.has(C.replyTo)) C.replyTo = null;
      place(cList, threads);
      if (!threads.length) cList.replaceChildren(el("li", { class: "c-none", text: "No comments yet." }));
      drawHead();
    }
    function thread(t, replies) {
      let n = C.nodes.get(t.id);
      if (!n) {
        const item = el("div", { class: "c-item", "data-id": String(t.id) });
        const rs = el("ul", { class: "c-replies" });
        n = { root: el("li", { class: "c-thread", "data-id": String(t.id) }, item, rs), item, rs, sig: null, box: null };
        C.nodes.set(t.id, n);
      }
      fill(n, t);
      place(n.rs, replies.map((r) => {
        let x = C.nodes.get(r.id);
        if (!x) { const item = el("li", { class: "c-item c-reply", "data-id": String(r.id) }); x = { root: item, item, sig: null }; C.nodes.set(r.id, x); }
        fill(x, r);
        return x.root;
      }));
      if (C.replyTo === t.id) { if (!n.box) { n.box = replyBox(t); n.root.append(n.box); } }
      else if (n.box) { n.box.remove(); n.box = null; }
      n.root.classList.toggle("has-replies", replies.length > 0 || !!n.box);
      return n.root;
    }
    function fill(n, c) {
      const e = epOf(c);
      const sig = C.editing === c.id ? `edit:${c.id}` : JSON.stringify([c.body, c.deleted, c.user, c.edited_at, c.created_at,
        versionNote(c), e ? [e.id, e.has_audio, e.duration_s] : null, mineC(c), canDelete(c), C.armed === c.id, C.replyTo === c.id]);
      if (n.sig === sig) return;
      n.sig = sig;
      if (c.deleted) { n.item.replaceChildren(el("p", { class: "c-gone", text: "comment deleted" })); return; }
      if (C.editing === c.id) { n.item.replaceChildren(editBox(c)); return; }
      const note = versionNote(c);
      const meta = el("div", { class: "c-meta" }, face(c.user, "m"), el("span", { class: "c-who", text: c.user.name }), " · ",
        whenNode(c.created_at, "c-when"), c.edited_at ? el("span", { text: " · edited", title: stamp(c.edited_at) }) : null,
        note ? el("span", { class: "c-ver", text: ` · ${note}` }) : null);
      const body = el("div", { class: "c-body md" }, richText(c.body, timeLinker(c)));
      const acts = el("div", { class: "c-acts" },
        el("button", { type: "button", class: "text-btn", "data-act": "reply", text: "Reply",
          "aria-label": `Reply to ${c.user.name}`, onclick: () => openReply(c.parent_id == null ? c.id : c.parent_id) }),
        mineC(c) ? el("button", { type: "button", class: "text-btn", "data-act": "edit", text: "Edit", onclick: () => { C.editing = c.id; C.armed = null; render(); focusEnd(n.item.querySelector("textarea")); } }) : null,
        canDelete(c) ? el("button", { type: "button", class: "text-btn danger", "data-act": "delete",
          text: C.armed === c.id ? "Delete now" : "Delete", onclick: () => del(c) }) : null);
      n.item.replaceChildren(meta, body, acts);
    }
    function focusEnd(ta) {
      if (!ta) return;
      ta.focus({ preventScroll: false });
      const n = ta.value.length;
      try { ta.setSelectionRange(n, n); } catch (e) { /* not a text box */ }
    }
    function openReply(tid) {
      C.replyTo = tid;
      render();
      const n = C.nodes.get(tid);
      if (n && n.box) focusEnd(n.box.querySelector("textarea"));
    }
    function replyBox(t) {
      const key = draftKey(C.pid, `r${t.id}`);
      const ta = el("textarea", { class: "field note c-text", maxlength: String(BODY_MAX), rows: "2", placeholder: "Write a reply",
        "aria-label": `Reply to ${t.user ? t.user.name : "this thread"}` });
      const go = el("button", { type: "button", class: "btn-accent", "data-act": "send-reply", text: "Reply", disabled: true });
      const msg = el("span", { class: "c-msg err", role: "status" });
      const send = async () => {
        if (!ta.value.trim() || go.disabled && go.dataset.busy) return;
        go.disabled = true; go.dataset.busy = "1"; msg.textContent = "";
        try {
          const c = await ctx.api("POST", `/api/papers/${encodeURIComponent(C.pid)}/comments`,
            { body: ta.value, episode_id: C.chosen ? C.chosen.id : null, parent_id: t.id });
          tab.del(key); ta.value = "";
          take(c); C.replyTo = null; render();
        } catch (e) { msg.textContent = e.message; go.disabled = false; }
        delete go.dataset.busy;
      };
      go.addEventListener("click", send);
      wireBox(ta, key, send, go);
      const cancel = el("button", { type: "button", class: "text-btn", text: "Cancel", onclick: () => { tab.del(key); C.replyTo = null; render(); } });
      return el("div", { class: "c-box c-replybox" }, ta, el("div", { class: "c-row" }, go, cancel, msg));
    }
    function editBox(c) {
      const ta = el("textarea", { class: "field note c-text", maxlength: String(BODY_MAX), rows: "3", "aria-label": "Edit your comment" });
      ta.value = c.body;
      const save = el("button", { type: "button", class: "btn-accent", "data-act": "save", text: "Save" });
      const msg = el("span", { class: "c-msg err", role: "status" });
      const send = async () => {
        if (!ta.value.trim()) return;
        save.disabled = true; msg.textContent = "";
        try {
          const r = await ctx.api("PUT", `/api/comments/${c.id}`, { body: ta.value });
          C.editing = null; take(r); render();
        } catch (e) { msg.textContent = e.message; save.disabled = false; }
      };
      save.addEventListener("click", send);
      wireBox(ta, null, send, save);
      const cancel = el("button", { type: "button", class: "text-btn", text: "Cancel", onclick: () => { C.editing = null; render(); } });
      return el("div", { class: "c-box" }, ta, el("div", { class: "c-row" }, save, cancel, msg));
    }
    // Delete asks once more (the button says "Delete now" for a few seconds).
    async function del(c) {
      if (C.armed !== c.id) {
        C.armed = c.id; render();
        clearTimeout(C.armTimer);
        C.armTimer = setTimeout(() => { if (C.armed === c.id) { C.armed = null; render(); } }, 4000);
        return;
      }
      C.armed = null;
      try {
        const r = await ctx.api("DELETE", `/api/comments/${c.id}`);
        take(Object.assign({}, c, { deleted: true, body: "", user: null }));
        setCount(r.paper_id, r.count);
        render();
      } catch (e) { render(); ctx.toast(e.message); }
    }
    async function postNew() {
      const body = cText.value;
      if (!body.trim() || cPost.dataset.busy) return;
      const pid = C.pid;
      cPost.disabled = true; cPost.dataset.busy = "1"; cMsg.textContent = "";
      try {
        const c = await ctx.api("POST", `/api/papers/${encodeURIComponent(pid)}/comments`,
          { body, episode_id: C.chosen ? C.chosen.id : null });
        if (C.pid === pid) {
          if (cText.value === body) cText.value = "";
          take(c); render(); toEnd();
        }
        tab.del(draftKey(pid, "new"));
      } catch (e) { cMsg.className = "c-msg err"; cMsg.textContent = e.message; }
      delete cPost.dataset.busy;
      syncMain();
    }

    // "Comment at 12:40": the box starts with where the player is in this paper's version.
    function drawAt() {
      const pos = C.pid && C.chosen && C.chosen.has_audio ? ctx.position() : null;
      const s = pos && pos.eid === C.chosen.id ? Math.floor(pos.s || 0) : 0;
      atBtn.hidden = !(s >= 1);
      if (atBtn.hidden) return;
      const label = `Comment at ${ctx.hms(s)}`;
      if (atBtn.textContent !== label) { atBtn.textContent = label; atBtn.dataset.s = String(s); }
    }
    atBtn.addEventListener("click", () => {
      const s = Number(atBtn.dataset.s) || 0;
      const t = ctx.hms(s);
      // a time the box already starts with is replaced, not stacked
      const rest = cText.value.replace(/^\s*(?:\d{1,2}:)?\d{1,2}:[0-5]\d\s*/, "");
      cText.value = `${t} ${rest}`;
      cText.dispatchEvent(new Event("input"));
      focusEnd(cText);
    });
    const audio = $("audio");
    if (audio) for (const ev of ["timeupdate", "seeked", "loadedmetadata", "emptied"]) audio.addEventListener(ev, drawAt);

    // A comment the board pointed at: into view, marked for a moment.
    function showFocus() {
      if (!C.focus || C.focus.pid !== C.pid || !C.loaded) return;
      const n = C.nodes.get(C.focus.cid);
      C.focus = null;
      if (!n) return;
      if (ctx.showComments) ctx.showComments();
      n.item.scrollIntoView({ block: "center" });
      n.item.classList.add("c-flash");
      setTimeout(() => n.item.classList.remove("c-flash"), 1600);
    }

    function onComment(d) {
      if (!d) return;
      if (d.paper_id && typeof d.count === "number") setCount(d.paper_id, d.count);
      if (d.paper_id === C.pid && d.comment) {
        if (!C.loaded) { if (C.pending) C.pending.push(d.comment); }
        else { take(d.comment); render(); }
      }
    }

    // ================================================================ the board
    // A panel the bell in the column's header opens (DESIGN.md decision 10): the pinned notice,
    // then what happened, newest first. A dot on the bell says something came since this person
    // last had it open; in the panel, those are marked.
    const B = {
      items: [], notice: null, seenNow: null, seenSnap: undefined, more: false, limit: BOARD_FIRST,
      loaded: false, seq: 0, rt: null, seenTimer: null, closed: true, nsig: "", nodes: new Map(),
    };
    const bell = $("bell-btn"), bDot = $("bell-dot") || el("span", { hidden: true }), panel = $("bell-panel");
    const bToggle = el("h2", { class: "bd-toggle", id: "bd-toggle" }, el("span", { class: "bd-h", text: "Board" }));
    const bPin = el("button", { type: "button", class: "text-btn bd-pin", id: "bd-pin", text: "Pin a notice", hidden: true });
    const nText = el("div", { class: "bd-ntext md", id: "bd-ntext" });
    const nMeta = el("div", { class: "bd-nmeta" });
    const bNotice = el("div", { class: "bd-notice", id: "bd-notice", hidden: true }, nText, nMeta);
    const fIn = el("input", { class: "field", id: "bd-nin", type: "text", maxlength: String(NOTICE_MAX), autocomplete: "off",
      placeholder: "Reading group Thursday 3 pm", "aria-label": "The notice" });
    const fGo = el("button", { type: "submit", class: "btn-accent", id: "bd-ngo", text: "Pin", disabled: true });
    const fMsg = el("p", { class: "bd-msg", role: "status" });
    const bForm = el("form", { class: "bd-form", id: "bd-form", hidden: true, novalidate: true },
      el("div", { class: "bd-frow" }, fIn, fGo,
        el("button", { type: "button", class: "text-btn", text: "Cancel", onclick: () => { bForm.hidden = true; fMsg.textContent = ""; } })), fMsg);
    const bList = el("ul", { class: "bd-list", id: "bd-list" });
    const bMore = el("button", { type: "button", class: "text-btn bd-more", id: "bd-more", text: "Show more", hidden: true });
    const bLess = el("button", { type: "button", class: "text-btn bd-more", id: "bd-less", text: "Show fewer", hidden: true });
    const bFeed = el("div", { class: "bd-feed", id: "bd-feed" }, bList, el("div", { class: "bd-foot" }, bMore, bLess));
    const board = el("section", { class: "board", id: "board", "aria-label": "Board" },
      el("div", { class: "bd-head" }, bToggle, bPin), bForm, bNotice, bFeed);
    panel.append(board);

    const after = (a, b) => ms(a) > ms(b);
    const theirs = (it) => !(it.user && it.user.id === me().id);
    const unreadNow = () => B.items.some((it) => theirs(it) && (!B.seenNow || after(it.at, B.seenNow)));
    function drawDot() {
      const on = B.loaded && unreadNow();
      bDot.hidden = !on;
      if (bell) bell.setAttribute("aria-label", on ? "Board, something new" : "Board");
    }
    function drawToggle() {
      if (bell) bell.setAttribute("aria-expanded", String(!B.closed));
      panel.hidden = B.closed;
    }
    // Under the bell, inside the screen.
    function placePanel() {
      if (panel.hidden || !bell) return;
      const z = document.documentElement.currentCSSZoom || 1, r = bell.getBoundingClientRect(), w = panel.offsetWidth;
      const vw = window.innerWidth / z;
      panel.style.top = `${Math.round(r.bottom / z + 6)}px`;
      panel.style.left = `${Math.round(Math.max(8, Math.min(vw - w - 8, r.left / z - 8)))}px`;
    }
    function openBoard(on) {
      if (B.closed === !on) return;
      B.closed = !on;
      if (B.closed) { B.limit = BOARD_FIRST; B.seenSnap = undefined; }
      else { B.seenSnap = B.seenNow; }      // opened: what is new since the last time is marked
      drawToggle(); drawBoard(); placePanel(); maybeSeen();
      if (!B.closed) { panel.focus({ preventScroll: true }); loadBoard(); }
    }
    if (bell) bell.addEventListener("click", (e) => { e.stopPropagation(); openBoard(B.closed); });
    document.addEventListener("pointerdown", (e) => {
      if (!B.closed && !panel.contains(e.target) && !(bell && bell.contains(e.target)) && !e.target.closest(".menu")) openBoard(false);
    }, true);
    window.addEventListener("keydown", (e) => {
      if (e.key !== "Escape" || B.closed || document.body.classList.contains("touring")) return;
      if (!bForm.hidden && bForm.contains(document.activeElement)) return;       // the notice's own Escape: nothing
      e.stopPropagation(); e.preventDefault();
      openBoard(false);
      if (bell) bell.focus({ preventScroll: true });
    }, true);
    window.addEventListener("resize", placePanel);
    bPin.addEventListener("click", () => { bForm.hidden = !bForm.hidden; if (!bForm.hidden) fIn.focus(); });
    fIn.addEventListener("input", () => { fGo.disabled = !fIn.value.trim(); fMsg.textContent = ""; ctx.typed(); });
    bForm.addEventListener("submit", async (e) => {
      e.preventDefault();
      if (!fIn.value.trim()) return;
      fGo.disabled = true;
      try {
        B.notice = await ctx.api("POST", "/api/board/notice", { body: fIn.value });
        fIn.value = ""; bForm.hidden = true;
        drawNotice();
      } catch (err) { fMsg.textContent = err.message; fGo.disabled = false; }
    });
    bMore.addEventListener("click", () => { B.limit += BOARD_STEP; loadBoard(); });
    bLess.addEventListener("click", () => { B.limit = BOARD_FIRST; loadBoard(); panel.scrollTop = 0; });

    function drawNotice() {
      const n = B.notice;
      bNotice.hidden = !n;
      const sig = JSON.stringify([n, admin()]);
      if (!n || sig === B.nsig) { B.nsig = n ? sig : ""; return; }
      B.nsig = sig;
      nText.replaceChildren(richText(n.body, null));
      nMeta.replaceChildren(face(n.user, "s"), el("span", { class: "bd-by", text: `Pinned by ${n.user.name} · ` }), whenNode(n.created_at),
        admin() ? el("button", { type: "button", class: "text-btn", id: "bd-unpin", text: "Unpin", onclick: unpin }) : null);
    }
    async function unpin() {
      const n = B.notice;
      if (!n) return;
      try { await ctx.api("DELETE", `/api/board/notice/${n.id}`); B.notice = null; drawNotice(); }
      catch (e) { ctx.toast(e.message); }
    }
    function itemNode(it, unread) {
      const who = face(it.user, "s");
      const text = el("span", { class: "bd-text" }, it.lead, it.title ? el("span", { class: "bd-t", text: it.title }) : null, it.tail || null);
      const when = el("span", { class: "bd-when" }, " · ", whenNode(it.at));
      let link;
      if (it.paper_id) {
        link = el("a", { class: "bd-link", href: `#p=${encodeURIComponent(it.paper_id)}`, onclick: (e) => {
          if (e.button || e.metaKey || e.ctrlKey || e.shiftKey) return;
          e.preventDefault();
          if (it.comment_id) C.focus = { pid: it.paper_id, cid: it.comment_id };
          openBoard(false);
          ctx.openPaper(it.paper_id);
          if (C.pid === it.paper_id) showFocus();
        } }, who, text, when);
      } else if (it.graph_id) {
        link = el("button", { type: "button", class: "bd-link", onclick: () => { openBoard(false); ctx.openGraph(it.graph_id); } }, who, text, when);
      } else {
        link = el("div", { class: "bd-link bd-plain" }, who, text, when);
      }
      return el("li", { class: `bd-item${unread ? " unread" : ""}`, "data-key": it.key, "data-kind": it.kind }, link);
    }
    function drawBoard() {
      drawToggle();
      bPin.hidden = !admin();
      drawNotice();
      drawDot();
      if (!B.loaded) return;
      const snap = B.seenSnap, keep = new Map();
      // keyed: an item that did not change keeps its node (and the keyboard focus on it)
      const rows = B.items.map((it) => {
        const unread = !B.closed && snap !== undefined && theirs(it) && (!snap || after(it.at, snap));
        const sig = JSON.stringify([it, unread]);
        let x = B.nodes.get(it.key);
        if (!x || x.sig !== sig) x = { li: itemNode(it, unread), sig };
        keep.set(it.key, x);
        return x.li;
      });
      B.nodes = keep;
      if (rows.length) place(bList, rows);
      else bList.replaceChildren(el("li", { class: "bd-none", text: "Nothing yet." }));
      bMore.hidden = !B.more;
      bLess.hidden = B.limit <= BOARD_FIRST;
    }
    function refresh(delay) { clearTimeout(B.rt); B.rt = setTimeout(loadBoard, delay === undefined ? 700 : delay); }
    async function loadBoard() {
      clearTimeout(B.rt);
      const seq = ++B.seq;
      let j;
      try { j = await ctx.api("GET", `/api/board?limit=${B.limit}`); } catch (e) { return; }
      if (seq !== B.seq) return;
      const t = Date.parse(j.now || "");
      if (!isNaN(t)) offset = t + 500 - Date.now();       // the hub's seconds, cut down: half a second on
      B.items = j.items || []; B.more = !!j.more; B.notice = j.notice || null;
      B.seenNow = j.seen_at || null;
      if (!B.loaded) { B.loaded = true; if (!B.closed) B.seenSnap = B.seenNow; }
      drawBoard();
      retime();
      maybeSeen();
    }
    // Seen: the board's panel is open, and the page is in front.
    function onScreen() {
      return !B.closed && document.visibilityState === "visible" && !!board.getClientRects().length;
    }
    const needSeen = () => B.loaded && !B.closed && B.items.length > 0 && (!B.seenNow || after(B.items[0].at, B.seenNow));
    function maybeSeen() {
      clearTimeout(B.seenTimer);
      if (!needSeen()) return;
      B.seenTimer = setTimeout(async () => {
        if (!needSeen() || !onScreen()) return;
        const at = B.items[0].at;
        try { const j = await ctx.api("PUT", "/api/board/seen", { at }); B.seenNow = j.seen_at || at; } catch (e) { return; }
        drawDot();
      }, 1200);
    }
    document.addEventListener("visibilitychange", () => { if (document.visibilityState === "visible") { retime(); maybeSeen(); } });
    window.addEventListener("hashchange", () => setTimeout(maybeSeen, 300));

    function onBoard(d) {
      if (!d) return;
      if (d.why === "seen") {
        if (d.seen_at && (!B.seenNow || after(d.seen_at, B.seenNow))) B.seenNow = d.seen_at;
        drawDot();
      } else if (d.why === "notice") {
        B.notice = d.notice || null;
        drawNotice();
      } else refresh();
    }
    // Anything the page hears that can change the board (app.js forwards every event as
    // papercast:event): an upload or a version (it starts in "checking"), one rejected, deleted
    // or back; a graph edit (the log). A version being voiced changes nothing here.
    window.addEventListener("papercast:event", (ev) => {
      const k = ev.detail && ev.detail.kind, d = (ev.detail && ev.detail.data) || {};
      if (k === "log") refresh();
      else if (k === "episode" && (d.state === "checking" || d.state === "rejected" || "deleted" in d)) refresh();
      else if (k === "paper" && d.new) refresh();
    });

    function event(kind, d) {
      if (kind === "comment") { onComment(d); refresh(); }
      else if (kind === "board") onBoard(d);
      else if (kind === "resync") {
        loadCounts(); refresh(0);
        if (C.pid) { C.loaded = false; C.pending = []; load(); }
      }
    }

    drawBoard();
    loadBoard();
    loadCounts();
    setInterval(retime, 30000);
    // joins and names are not events: a look every few minutes, when the page is in front
    setInterval(() => { if (document.visibilityState === "visible") refresh(0); }, 300000);

    return {
      paper, event,
      count: (pid) => C.counts.get(pid) || 0,
    };
  }

  window.PaperSocial = { mount };
})();
