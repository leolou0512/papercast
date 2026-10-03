// papercast-group: the graph list, the page's home column (DESIGN.md, Leo's decisions of
// 2026-10-03; hub/graphlist.py). Everyone's graphs: the ones this person subscribes to first, in
// solid colour, then the rest greyed, the chosen sort within each; a subscribed graph says how
// many papers joined since they last opened it. One search box finds papers and graphs. "+ New
// graph" makes one (its maker is subscribed); "Not in any graph" is last. On a phone the column
// is the whole screen, and a graph's papers are a list (the map's order, earliest first) with
// "Map" for the drawing. The sort is the account's, on the hub. app.js mounts it
// (window.PaperGraphs.mount(ctx)) and gives it the hub's list as the map reads it. Names and
// titles go in as text; the only markup is this file's own icons.
"use strict";
(() => {
  const $ = (id) => document.getElementById(id);
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
  const IC = {
    sub: (on) => (on
      ? `<svg width="20" height="20" viewBox="0 0 20 20" aria-hidden="true"><circle cx="10" cy="10" r="8.2" fill="currentColor"/><path d="m6.4 10.3 2.5 2.5 4.8-5.2" fill="none" stroke="var(--bg)" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"/></svg>`
      : `<svg width="20" height="20" viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" aria-hidden="true"><circle cx="10" cy="10" r="7.6"/><path d="M10 6.6v6.8M6.6 10h6.8"/></svg>`),
    lock: `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="5" y="11" width="14" height="9" rx="2"/><path d="M8 11V8a4 4 0 0 1 8 0v3"/></svg>`,
    sort: `<svg width="18" height="18" viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" aria-hidden="true"><path d="M3.5 5.5h13M3.5 10h9M3.5 14.5h5"/></svg>`,
    back: `<svg width="22" height="22" viewBox="0 0 22 22" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M14 4 7 11l7 7"/></svg>`,
    check: `<svg width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m2.5 7.5 3 3 6-7"/></svg>`,
    heard: `<svg width="16" height="16" viewBox="0 0 16 16" aria-hidden="true"><circle cx="8" cy="8" r="7" fill="var(--alive)"/><path d="m4.8 8.2 2.2 2.2 4.2-4.6" fill="none" stroke="var(--bg)" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/></svg>`,
    x: `<svg width="16" height="16" viewBox="0 0 12 12" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" aria-hidden="true"><path d="m3 3 6 6M9 3 3 9"/></svg>`,
  };
  // The sorts (graphlist.py SORTS), in Leo's order; "updated" is the default.
  const SORTS = [["az", "A–Z"], ["updated", "Recently updated"], ["papers", "Most papers"], ["created", "Date created"],
    ["mine", "Mine first"], ["unheard", "Most unheard by me"], ["newest", "Newest paper added"]];
  const coll = new Intl.Collator("en", { sensitivity: "base", numeric: true });
  const byName = (a, b) => coll.compare(a.name || "", b.name || "") || String(a.id).localeCompare(String(b.id));
  const later = (k) => (a, b) => String(b[k] || "").localeCompare(String(a[k] || "")) || byName(a, b);      // ISO times
  const more = (k) => (a, b) => (b[k] || 0) - (a[k] || 0) || byName(a, b);
  const PAPERS_FIRST = 30;

  function mount(ctx) {
    const G = { list: [], unfiled: null, sort: "updated", sortMine: false, last: null, loaded: false, cur: null, q: "", seq: 0, res: null, all: false,
      timer: null, form: false, busy: false, pend: new Map(), papersFor: null, data: new Map() };
    const mineOf = (g) => (g.created_by && ctx.me() && g.created_by.id === ctx.me().id ? 1 : 0);
    const ORDER = {
      az: byName, updated: later("updated_at"), papers: more("n"), created: later("created_at"),
      mine: (a, b) => mineOf(b) - mineOf(a) || later("updated_at")(a, b), unheard: more("unheard"), newest: later("newest_at"),
    };
    const item = (gid) => (gid === "none" ? G.unfiled : G.list.find((g) => g.id === gid)) || null;
    const box = $("gl");

    // ------------------------------------------------------------------ the hub's list
    // As the map reads it (GET /api/graphs): the graphs with this person's fields, Not in any
    // graph, and the account's choices. A toggle on its way stays over what a list says meanwhile.
    function setList(r) {
      if (!r || !Array.isArray(r.graphs)) return;
      G.list = r.graphs.filter((g) => g && typeof g.id === "string");
      for (const g of G.list) if (G.pend.has(g.id)) g.subscribed = G.pend.get(g.id);
      G.unfiled = r.unfiled && r.unfiled.id ? r.unfiled : null;
      if (r.ui) {
        // the account's sort until one is picked here: after that this page's own choice stands
        // (a list asked for before the choice reached the hub would bring the old one back)
        if (!G.sortMine) G.sort = SORTS.some((s) => s[0] === r.ui.sort) ? r.ui.sort : "updated";
        G.last = r.ui.last_graph || null;
      }
      G.loaded = true;
      render();
      renderHead();
      renderPapers();
    }
    // In the order the column shows them: subscribed, then the rest, each in the sort.
    function sections() {
      const how = ORDER[G.sort] || ORDER.updated;
      return { subs: G.list.filter((g) => g.subscribed).sort(how), rest: G.list.filter((g) => !g.subscribed).sort(how) };
    }
    function ordered() { const s = sections(); return s.subs.concat(s.rest); }
    // Where the page lands: the graph open last (the account's), else the one with the most papers.
    function landing() {
      if (G.last && item(G.last) && (G.last !== "none" || (G.unfiled && G.unfiled.n))) return G.last;
      const best = G.list.slice().sort(more("n"))[0];
      return best ? best.id : (G.unfiled && G.unfiled.n ? "none" : null);
    }
    // The graph a paper opens in: the one shown, if it has it; else a subscribed one, else any, in
    // the column's order; else Not in any graph.
    function graphFor(pid, gids) {
      const has = new Set(gids || []);
      if (G.cur && (G.cur === "none" ? !has.size : has.has(G.cur))) return G.cur;
      const g = ordered().find((x) => has.has(x.id));
      return g ? g.id : (has.size ? [...has][0] : "none");
    }
    function setCurrent(gid) {
      if (G.cur === gid) return;
      G.cur = gid;
      for (const r of box.querySelectorAll(".gl-row")) {
        const on = r.dataset.id === gid;
        r.classList.toggle("cur", on);
        const b = r.querySelector(".gl-open");
        if (b) { if (on) b.setAttribute("aria-current", "true"); else b.removeAttribute("aria-current"); }
      }
      renderHead();
    }
    // Opened: its new papers are seen.
    function opened(gid) { const g = item(gid); if (g && g.new) { g.new = 0; render(); } }

    // ------------------------------------------------------------------ subscribing
    async function toggle(gid, on) {
      const g = item(gid);
      if (!g || g.pseudo) return;
      const was = !!g.subscribed;
      if (was === on) return;
      g.subscribed = on;
      G.pend.set(gid, on);
      render(); renderHead(); renderPapers();
      let j = null;
      try { j = await ctx.api("PUT", `/api/graphs/${encodeURIComponent(gid)}/subscription`, { subscribed: on }); }
      catch (e) { if (G.pend.get(gid) === on) { G.pend.delete(gid); const x = item(gid); if (x) x.subscribed = was; } ctx.toast(e.message); render(); renderHead(); renderPapers(); return; }
      if (G.pend.get(gid) === on) G.pend.delete(gid);
      if (j && j.graph && j.graph.id === gid && !G.pend.has(gid)) {
        const i = G.list.findIndex((x) => x.id === gid);
        if (i >= 0) G.list[i] = j.graph;
      }
      render(); renderHead(); renderPapers();
    }
    function subBtn(g, cls) {
      const on = !!g.subscribed;
      return el("button", { type: "button", class: cls || "gl-sub", "aria-pressed": String(on), "aria-label": `Subscribe to ${g.name}`,
        title: on ? "Subscribed" : "Subscribe", html: IC.sub(on), onclick: (e) => { e.stopPropagation(); toggle(g.id, !on); } });
    }
    // The graph's own header (on the map, and above a phone's list of its papers): its name, and
    // the same toggle, with its word.
    function pill(g) {
      const on = !!g.subscribed;
      return el("button", { type: "button", class: `gh-sub${on ? " on" : ""}`, "aria-pressed": String(on), "aria-label": `Subscribe to ${g.name}`,
        onclick: () => toggle(g.id, !on) }, el("span", { class: "gh-i", html: IC.sub(on) }), el("span", { text: on ? "Subscribed" : "Subscribe" }));
    }
    const head = el("div", { class: "gh", id: "gh" });
    function renderHead() {
      const g = item(G.cur);
      if (!g) { head.replaceChildren(); return; }
      const kids = [el("h2", { class: "gh-name", id: "gh-name", text: g.name })];
      if (g.locked) kids.push(el("span", { class: "gh-lock", role: "img", "aria-label": "Locked", title: "Locked", html: IC.lock }));
      if (!g.pseudo) kids.push(pill(g));
      head.replaceChildren(...kids);
    }

    // ------------------------------------------------------------------ the column
    function row(g, parts) {
      const cur = g.id === G.cur, n = Number(g.n) || 0, fresh = g.subscribed && g.new > 0;
      const name = el("span", { class: "gl-name" });
      if (Array.isArray(parts) && parts.length) name.append(...marked(parts)); else name.textContent = g.name;
      const label = [g.name, `${n} ${n === 1 ? "paper" : "papers"}`, g.locked ? "locked" : "", fresh ? `${g.new} new` : ""].filter(Boolean).join(", ");
      const open = el("button", { type: "button", class: "gl-open", "aria-label": label, "aria-current": cur ? "true" : null,
        onclick: () => ctx.openGraph(g.id) },
      name,
      g.locked ? el("span", { class: "gl-lock", html: IC.lock }) : null,
      fresh ? el("span", { class: "gl-new t", text: `${g.new} new` }) : null,
      el("span", { class: "gl-n t", text: String(n) }));
      return el("li", { class: `gl-row${g.subscribed ? " on" : ""}${g.pseudo ? " pseudo" : ""}${cur ? " cur" : ""}`, "data-id": g.id },
        open, g.pseudo ? el("span", { class: "gl-sub-sp" }) : subBtn(g));
    }
    // A title or a name, with what matched marked: text nodes and <mark> (never HTML).
    function marked(parts) {
      const out = [];
      parts.forEach((x, i) => { const t = typeof x === "string" ? x : ""; if (t) out.push(i % 2 ? el("mark", { text: t }) : document.createTextNode(t)); });
      return out;
    }
    function newForm() {
      const name = el("input", { class: "field", id: "gl-name", type: "text", maxlength: "80", autocomplete: "off", placeholder: "Name", "aria-label": "Name" });
      const tags = el("input", { class: "field", id: "gl-tags", type: "text", autocomplete: "off", placeholder: "Tags", "aria-label": "Tags" });
      const go = el("button", { type: "submit", class: "btn-accent", id: "gl-make", text: "Make graph" });
      const msg = el("p", { class: "gl-msg", role: "status" });
      const f = el("form", { class: "gl-form", id: "gl-form", novalidate: true }, name, tags,
        el("div", { class: "gl-frow" }, go, el("button", { type: "button", class: "text-btn", id: "gl-cancel", text: "Cancel", onclick: () => { G.form = false; render(); $("gl-newb").focus(); } })), msg);
      f.addEventListener("submit", async (e) => {
        e.preventDefault();
        const nm = name.value.replace(/\s+/g, " ").trim();
        if (!nm || G.busy) { if (!nm) name.focus(); return; }
        const tg = [];
        for (const t of tags.value.split(",")) { const x = t.replace(/\s+/g, " ").trim(); if (x && !tg.includes(x)) tg.push(x); }
        G.busy = true; go.disabled = true; msg.textContent = "";
        try {
          const j = await ctx.api("POST", "/api/graphs", tg.length ? { name: nm, tags: tg } : { name: nm });
          const g = j && j.graph;
          G.form = false;
          if (g && g.id) {
            if (!item(g.id)) G.list.push(Object.assign({ subscribed: true, new: 0, unheard: 0 }, g));
            render();
            ctx.created(g.id);
          } else render();
        } catch (err) { msg.textContent = err.message; go.disabled = false; }
        G.busy = false;
      });
      setTimeout(() => name.focus(), 0);
      return f;
    }
    function render() {
      sortLabel();
      const keep = document.activeElement && box.contains(document.activeElement) ? focusKey(document.activeElement) : null;
      if (G.q.trim()) renderResults(); else renderSections();
      if (keep) refocus(keep);
    }
    function renderSections() {
      if (!G.loaded) { box.replaceChildren(); return; }
      const { subs, rest } = sections();
      const kids = [];
      if (subs.length) kids.push(el("h3", { class: "gl-h", text: "Subscribed" }), el("ul", { class: "gl-list gl-subs" }, subs.map((g) => row(g))));
      if (rest.length) kids.push(el("h3", { class: "gl-h", text: subs.length ? "Not subscribed" : "Graphs" }), el("ul", { class: "gl-list gl-rest" }, rest.map((g) => row(g))));
      kids.push(G.form ? newForm() : el("button", { type: "button", class: "gl-newb", id: "gl-newb", text: "+ New graph", onclick: () => { G.form = true; render(); } }));
      if (G.unfiled && (G.unfiled.n > 0 || G.cur === "none")) kids.push(el("ul", { class: "gl-list gl-none" }, row(G.unfiled)));
      box.replaceChildren(...kids);
    }
    function paperRow(p) {
      const m = p.match || {};
      const t = el("span", { class: "gl-ptitle" });
      if (Array.isArray(m.title) && m.title.length) t.append(...marked(m.title)); else t.textContent = typeof p.title === "string" ? p.title : "";
      const sub = [ctx.authors(p), p.year].filter(Boolean).join(" · ");
      // where else it matched, in a few words (the hub's snippet: "in the script: …"), when not in its title
      const hit = m.lead && m.where !== "title" ? el("span", { class: "gl-phit" }, el("span", { class: "lead", text: m.lead }), ...marked(m.parts || [])) : null;
      return el("li", { class: "gl-prow", "data-id": p.id },
        el("button", { type: "button", class: "gl-popen", onclick: () => ctx.openPaper(p.id) }, t, hit, sub ? el("span", { class: "gl-psub", text: sub }) : null));
    }
    function renderResults() {
      const r = G.res;
      if (!r) { box.replaceChildren(); return; }
      const kids = [];
      const gs = r.graphs.map((h) => [item(h.id), h.parts]).filter((x) => x[0]);
      if (gs.length) kids.push(el("h3", { class: "gl-h", text: "Graphs" }), el("ul", { class: "gl-list gl-hits" }, gs.map(([g, parts]) => row(g, parts))));
      if (r.papers.length) {
        const shown = G.all ? r.papers : r.papers.slice(0, PAPERS_FIRST);
        kids.push(el("h3", { class: "gl-h", text: "Papers" }), el("ul", { class: "gl-list gl-papers" }, shown.map(paperRow)));
        if (shown.length < r.papers.length) kids.push(el("button", { type: "button", class: "text-btn gl-all", id: "gl-all", text: `Show all ${r.papers.length}`, onclick: () => { G.all = true; render(); } }));
      }
      if (!kids.length) kids.push(el("p", { class: "gl-none-t", text: "No matches" }));
      box.replaceChildren(...kids);
    }
    // The keyboard stays where it was when the column is drawn again.
    function focusKey(a) {
      const li = a.closest("[data-id]");
      if (a.id) return { id: a.id };
      return li ? { row: li.dataset.id, cls: a.className.split(" ")[0] } : null;
    }
    function refocus(k) {
      let e = k.id ? $(k.id) : null;
      if (!e && k.row) { const li = box.querySelector(`[data-id="${CSS.escape(k.row)}"]`); e = li && li.querySelector(`.${k.cls}`); }
      if (e && e !== document.activeElement) e.focus({ preventScroll: true });
    }

    // ------------------------------------------------------------------ the search box
    // Papers and graphs as it is typed (the hub's search, GET /api/library?q=: the papers best
    // first, and the graphs whose name matches); "/" from anywhere goes to it, Escape empties it
    // (then leaves it), Enter opens the first result.
    const sb = $("search"), clr = $("search-clear");
    clr.innerHTML = IC.x;
    function changed() {
      G.q = sb.value;
      clr.hidden = !G.q;
      ctx.typed();
      clearTimeout(G.timer);
      if (!G.q.trim()) { G.seq++; G.res = null; G.all = false; render(); return; }
      G.timer = setTimeout(search, 150);
    }
    async function search() {
      clearTimeout(G.timer);
      const q = G.q, seq = ++G.seq;
      if (!q.trim()) return;
      let j;
      try { j = await ctx.api("GET", `/api/library?q=${encodeURIComponent(q)}`); } catch (e) { if (seq === G.seq) ctx.toast(e.message); return; }
      if (seq !== G.seq) return;
      const papers = (j.papers || []).filter((p) => p && typeof p.id === "string");
      ctx.known(papers);
      G.res = { graphs: Array.isArray(j.graphs) ? j.graphs : [], papers };
      G.all = false;
      render();
    }
    sb.addEventListener("input", changed);
    sb.addEventListener("keydown", (e) => {
      if (e.key === "Escape") {
        e.preventDefault(); e.stopPropagation();
        if (sb.value) { sb.value = ""; changed(); } else sb.blur();
      } else if (e.key === "Enter") {
        e.preventDefault();
        const first = box.querySelector(".gl-open, .gl-popen");
        if (G.res && first) first.click(); else search();
        if (ctx.phone()) sb.blur();
      }
    });
    clr.addEventListener("click", () => { sb.value = ""; changed(); sb.focus(); });
    document.addEventListener("keydown", (e) => {
      if (e.key !== "/" || e.ctrlKey || e.metaKey || e.altKey || e.defaultPrevented) return;
      const t = e.target;
      if (t && t.closest && t.closest("input, textarea, select, [contenteditable], .menu, #map, #win")) return;
      if (!ctx.free()) return;
      e.preventDefault();
      sb.focus(); sb.select();
    });

    // ------------------------------------------------------------------ the sort
    const sortB = $("sort-btn");
    $("sort-icon").innerHTML = IC.sort;
    function sortLabel() {
      const s = SORTS.find((x) => x[0] === G.sort) || SORTS[1];
      $("sort-label").textContent = s[1];
      sortB.setAttribute("aria-label", `Sort: ${s[1]}`);
    }
    async function setSort(id) {
      if (!SORTS.some((s) => s[0] === id)) return;
      G.sort = id; G.sortMine = true;
      sortLabel(); render();
      try { await ctx.api("PUT", "/api/ui-state", { sort: id }); } catch (e) { ctx.toast(e.message); }
    }
    sortB.addEventListener("click", (e) => {
      e.stopPropagation();
      ctx.openMenu(sortB, "gsort", SORTS.map(([id, label]) => el("button", {
        type: "button", role: "menuitemradio", class: "radio", "aria-checked": String(id === G.sort), "data-sort": id,
        onclick: () => { ctx.closeMenu(); setSort(id); },
      }, el("span", { class: "mark", html: id === G.sort ? IC.check : "" }), el("span", { text: label }))));
    });

    // ------------------------------------------------------------------ a phone: one graph's papers
    // The map's order (time, earliest first), from the graph's answer as the map reads it.
    const gp = { rows: $("gpl-rows"), name: $("gpl-name"), acts: $("gpl-acts") };
    $("gpl-back").innerHTML = IC.back;
    function showPapers(gid) { G.papersFor = gid || null; renderPapers(); }
    function graphData(gid, d) { if (d) G.data.set(gid, d); if (gid === G.papersFor) renderPapers(); }
    function renderPapers() {
      const gid = G.papersFor;
      if (!gid) return;
      const g = item(gid);
      gp.name.textContent = g ? g.name : "";
      gp.acts.replaceChildren(...(g && !g.pseudo ? [pill(g)] : []));
      const d = G.data.get(gid);
      if (!d) { gp.rows.replaceChildren(); return; }
      const sig = JSON.stringify([gid, d.nodes.map((n) => { const p = ctx.paperOf(n.id) || {}; return [n.id, p.title || n.title, !!(p.listened || p.heard || n.heard)]; })]);
      if (gp.rows.dataset.sig === sig) return;
      gp.rows.dataset.sig = sig;
      if (!d.nodes.length) { gp.rows.replaceChildren(el("li", { class: "gpl-empty", text: "No papers" })); return; }
      gp.rows.replaceChildren(...d.nodes.map((n) => {
        const p = ctx.paperOf(n.id) || {}, heard = !!(p.listened || p.heard || n.heard);
        const title = (typeof p.title === "string" && p.title) || n.title || n.label || "";
        const sub = [n.year || p.year, ctx.authors(p)].filter(Boolean).join(" · ");
        return el("li", { class: `gpl-row${heard ? " heard" : ""}`, "data-id": n.id },
          el("button", { type: "button", class: "gpl-btn", onclick: () => ctx.openPaper(n.id, gid) },
            el("span", { class: "gpl-t", text: title }), sub ? el("span", { class: "gpl-s t", text: sub }) : null),
          heard ? el("span", { class: "gpl-heard", role: "img", "aria-label": "Heard", title: "Heard", html: IC.heard }) : null);
      }));
    }
    function paperChanged() { if (G.papersFor) renderPapers(); }

    render();
    const api = {
      setList,
      setCurrent, opened, landing, graphFor, item, ordered, head, showPapers, graphData, paperChanged,
      sort: () => G.sort, query: () => G.q,
    };
    window.PaperGraphs.current = api;       // for the browser tests
    return api;
  }

  window.PaperGraphs = { mount };
})();
