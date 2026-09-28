/* papercast-group: the paper map, for the group and editable. Leo's map (stacks/papercast/web/
   static/map.js: a force graph drawn like Obsidian's graph view, WebGL where there is a GPU and
   2D otherwise, hover lighting a paper's links, labels that fade in as you zoom, Start here and
   the listening order) with its graphs from the hub (SPEC.md section 8). One tab per graph.
   Anyone links two papers, regrades or removes a link, adds a paper to a graph or takes one
   out, makes, renames or retags a graph; a locked graph only an admin changes. Undo says what it
   will undo before it does, for "my last edit" or "the last edit"; History lists the last 100
   changes. Every edit shows at once and is set right by the hub's answer; a failure rolls back
   with a short message. The hub works the positions out (layout.py runs these same equations
   to rest), so the map opens at rest: the browser never warms the layout up, it only eases a
   node to where the hub puts it. An arrow goes from a paper to a paper built on it. Grey: not
   listened; green: listened (by whoever is looking); accent: a place to start. Mounted by app.js:
   window.PaperMap.mount(host, opts) -> {changed, show, hide, refresh, select}. */
(function () {
  "use strict";
  var DEFAULTS = { arrows: true, fade: 0.5, node: 1, line: 1, center: 0.4, repel: 10, link: 0.7, dist: 70 };
  var SKEY = "pcg.map.settings", TKEY = "pcg.map.tab";
  var ALPHA_MIN = 0.001, ALPHA_DECAY = 1 - Math.pow(ALPHA_MIN, 1 / 300), VDECAY = 0.6;
  // Leo's grades (the lineage judge's words, shortened)
  var GRADES = [
    ["e", "Essential", "a direct successor: it extends or fine-tunes the earlier paper’s own method"],
    ["s", "Strong", "the earlier paper is a main ingredient of it"],
    ["w", "Weak", "it uses something small from the earlier paper"]];
  var GNAME = { e: "essential", s: "strong", w: "weak" };

  function h(tag, cls, text) { var e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; }
  function svg(html) { var s = h("span"); s.innerHTML = html; return s.firstChild; }
  function load(k) { try { return JSON.parse(localStorage.getItem(k) || "null"); } catch (e) { return null; } }
  function save(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch (e) { /* private window */ } }
  function phone() { return window.matchMedia("(max-width: 760px)").matches; }
  function surname(n) { n = String(n || "").replace(/\s+/g, " ").trim(); var w = n.split(" "); return w[w.length - 1] || n; }
  function nameOf(u) { return !u ? "" : typeof u === "string" ? u : String(u.name || u.email || ""); }
  function same(a, b) { return a != null && b != null && String(a) === String(b); }
  function obj(v) { if (v == null) return null; if (typeof v === "string") { try { v = JSON.parse(v); } catch (e) { return null; } } return v && typeof v === "object" ? v : null; }
  function quote(s) { return "‘" + s + "’"; }
  function names(a) { return a.length < 2 ? a.join("") : a.slice(0, -1).join(", ") + " and " + a[a.length - 1]; }
  function cap(s) { return s ? s.charAt(0).toUpperCase() + s.slice(1) : s; }
  function tagList(s) { var out = []; String(s || "").split(",").forEach(function (t) { t = t.replace(/\s+/g, " ").trim(); if (t && out.indexOf(t) < 0) out.push(t); }); return out; }
  function ease(p) { return p < 0.5 ? 2 * p * p : 1 - Math.pow(-2 * p + 2, 2) / 2; }
  function ago(iso) {
    var t = Date.parse(iso); if (isNaN(t)) return "";
    var s = Math.max(0, (Date.now() - t) / 1000);
    if (s < 45) return "just now";
    if (s < 3570) return Math.max(1, Math.round(s / 60)) + " min ago";
    if (s < 86400) return Math.round(s / 3600) + " h ago";
    if (s < 86400 * 14) return Math.round(s / 86400) + " d ago";
    return new Date(t).toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" });
  }
  function when(iso) { var t = Date.parse(iso); return isNaN(t) ? "" : new Date(t).toLocaleString(); }

  var ICONS = {
    list: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M9 6h11M9 12h11M9 18h11"/><path d="M4 6h.01M4 12h.01M4 18h.01" stroke-width="2.6"/></svg>',
    fit: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5"/></svg>',
    gear: '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="3"/><path d="M12 2.5v3M12 18.5v3M2.5 12h3M18.5 12h3M5.3 5.3l2.1 2.1M16.6 16.6l2.1 2.1M5.3 18.7l2.1-2.1M16.6 7.4l2.1-2.1"/></svg>',
    close: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m6 6 12 12M18 6 6 18"/></svg>',
    undo: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M9 14 4 9l5-5"/><path d="M4 9h10.5a5.5 5.5 0 0 1 0 11H11"/></svg>',
    clock: '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="8.5"/><path d="M12 7.5V12l3 2"/></svg>',
    lock: '<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="5" y="11" width="14" height="9" rx="2"/><path d="M8 11V8a4 4 0 0 1 8 0v3"/></svg>'
  };

  function mount(root, opts) {
    opts = opts || {};
    var API = opts.api || "", ME = opts.me || {}, ADMIN = ME.role === "admin", EDIT = opts.editable !== false;
    var papers = opts.papers || new Map();
    var S = Object.assign({}, DEFAULTS, load(SKEY) || {});
    var C = {}, W = 0, H = 0, DPR = 1, FONT = "sans-serif";
    // graphs: {id, meta: {name, tags, locked, n, created_by}, data: the hub's answer, _s: what is drawn}
    var graphs = [], byId = {}, cur = null, listBusy = false, listAgain = false, listErr = null, listLoaded = false;
    var hoverI = null, hoverL = null, selId = null, selLink = null, linkFrom = null, draft = null, dragN = null, qset = null, want = null;
    var hl = 0, hlSet = null, hlFocus = null, hlLink = null, hlKey = null, running = false, anim = null, moves = null, userMoved = false, visible = true;
    // settled counts the edits the hub has answered: an answer to a GET sent before one of them is
    // out of date (it may still show a paper just taken out), so it is dropped and asked again
    var pending = [], settled = 0, tmpSeq = 0, editLabel = null, delAsk = false, deleting = {};
    var LOG = { entries: [], hint: null, papers: null, users: null, loaded: false, stale: true, busy: null, again: false, err: null, undoing: false };

    /* ---------- the hub ---------- */
    function call(method, path, body) {
      var init = { method: method, credentials: "same-origin", headers: { Accept: "application/json" } };
      if (method !== "GET") init.headers["X-PCG"] = "1";          // the hub's CSRF check (SPEC.md section 2)
      if (body !== undefined) { init.headers["Content-Type"] = "application/json"; init.body = JSON.stringify(body); }
      return fetch(API + path, init).then(function (r) {
        return r.text().then(function (t) {
          var j = null; try { j = t ? JSON.parse(t) : null; } catch (e) { j = null; }
          if (!r.ok) { var err = new Error("HTTP " + r.status); err.status = r.status; err.body = j || {}; throw err; }
          return j;
        });
      }, function () { var err = new Error("offline"); err.status = 0; err.body = {}; throw err; });
    }
    function errText(e) {
      var b = (e && e.body) || {};
      if (!e || !e.status) return "the hub did not answer";
      if (b.message && b.message !== b.error) return String(b.message).replace(/\.$/, "");
      if (e.status === 403) return b.error === "locked" ? "the graph is locked" : "not allowed";
      if (e.status === 404) return "it is no longer there";
      return b.error ? String(b.error).replace(/_/g, " ") : "error " + e.status;
    }

    /* ---------- DOM ---------- */
    root.classList.add("pmap");
    root.textContent = "";
    // Two canvases: the graph underneath, drawn with WebGL (as Obsidian's graph view is), and the
    // labels on top in 2D, which also takes the pointer. Without WebGL the graph is drawn in 2D.
    var gv = h("canvas", "pm-cv pm-gl"), cv = h("canvas", "pm-cv"); cv.setAttribute("aria-label", "Paper map");
    var ctx = cv.getContext("2d");
    var top = h("div", "pm-top"), head = h("div", "pm-head"), tabsEl = h("div", "pm-tabs");
    tabsEl.setAttribute("role", "tablist");
    var qEl = h("input", "pm-q"); qEl.type = "search"; qEl.placeholder = "Search papers"; qEl.autocomplete = "off"; qEl.spellcheck = false;
    qEl.setAttribute("aria-label", "Search papers on the map");
    var icons = h("div", "pm-icons"), closeBox = h("div", "pm-closebox");
    function ib(icon, title, into) { var b = h("button", "pm-ib"); b.type = "button"; b.title = title; b.setAttribute("aria-label", title); b.appendChild(svg(ICONS[icon])); (into || icons).appendChild(b); return b; }
    var bStart = ib("list", "Start here"), bUndo = ib("undo", "Undo"), bHist = ib("clock", "History"), bFit = ib("fit", "Fit to screen"), bSet = ib("gear", "Graph settings");
    var bClose = opts.onClose ? ib("close", "Close the map", closeBox) : null;
    if (!EDIT) bUndo.hidden = true;
    if (opts.title) head.appendChild(h("span", "pm-title", opts.title));
    if (opts.graphs !== false) head.appendChild(tabsEl);
    [head, qEl, icons, closeBox].forEach(function (e) { top.appendChild(e); });
    var PANELS = {}, PBTN = { start: bStart, undo: bUndo, hist: bHist, set: bSet };
    function panel(key, label) { var e = h("div", "pm-panel"); e.hidden = true; e.setAttribute("data-panel", key); e.setAttribute("role", "region"); e.setAttribute("aria-label", label); PANELS[key] = e; return e; }
    var startEl = panel("start", "Start here"), undoEl = panel("undo", "Undo"), histEl = panel("hist", "History"), setEl = panel("set", "Graph settings"), newEl = panel("newg", "New graph");
    var setDyn = h("div", "pm-setdyn"), setFix = h("div");
    setEl.appendChild(setDyn); setEl.appendChild(setFix);
    Object.keys(PBTN).forEach(function (k) { PBTN[k].setAttribute("aria-pressed", "false"); });
    var card = h("div", "pm-card"); card.hidden = true;
    var tip = h("div", "pm-tip"); tip.hidden = true;
    var legend = h("div", "pm-legend");
    function dot(cls, text) { var s = h("span"); s.appendChild(h("i", cls)); s.appendChild(document.createTextNode(text)); legend.appendChild(s); }
    dot("pm-d-done", "listened");
    dot("pm-d-start", "start here");
    legend.appendChild(h("span", null, "arrow: paper → paper built on it"));
    var banner = h("div", "pm-banner"); banner.hidden = true;
    var empty = h("div", "pm-empty"); empty.hidden = true;
    var msg = h("div", "pm-msg"); msg.hidden = true; msg.setAttribute("role", "status"); msg.setAttribute("aria-live", "polite");
    [gv, cv, top, startEl, undoEl, histEl, setEl, newEl, card, tip, legend, banner, empty, msg].forEach(function (e) { root.appendChild(e); });
    function btn(text, cls, fn) { var b = h("button", cls || "pm-btn", text); b.type = "button"; if (fn) b.addEventListener("click", fn); return b; }

    function readColors() {
      var cs = getComputedStyle(root);
      ["bg", "text", "muted", "node", "line", "hi", "start", "done"].forEach(function (k) { C[k] = cs.getPropertyValue("--pm-" + k).trim(); });
      FONT = cs.fontFamily || "sans-serif";
      colorCache = {};
    }

    /* ---------- short messages ---------- */
    var msgT = null;
    function say(text, action) {
      msg.textContent = "";
      msg.appendChild(h("span", null, text));
      if (action) msg.appendChild(btn(action.label, "pm-msg-b", function () { msg.hidden = true; action.fn(); }));
      msg.hidden = false;
      clearTimeout(msgT); msgT = setTimeout(function () { msg.hidden = true; }, action ? 8000 : 5000);
    }
    // The message's Undo: my last edit, if that is still the edit the message named (op and
    // target); else the Undo panel opens and shows what "my last edit" is now.
    function undoMine(op, key) {
      return { label: "Undo", fn: function () {
        loadLog(true).then(function () {
          var e = candidate("mine"), t = e && targets(e);
          var same = e && e.op === op && t && (op === "link.remove" ? String(t.lid) === String(key) : op === "graph.delete" ? t.gid === key : t.pid === key);
          if (same) undo("mine"); else { if (undoEl.hidden) openPanel("undo"); say("Your last edit is another one now: see Undo."); }
        });
      } };
    }

    /* ---------- paper facts ---------- */
    function paper(id) { return papers.get(id) || null; }
    function nodeOf(g, id) { var s = g && g._s; return s && s.idx[id] != null ? s.nodes[s.idx[id]] : null; }
    function info(id) {                                   // the hub's node, from any graph drawn so far
      var n = nodeOf(cur, id); if (n) return n.o;
      for (var i = 0; i < graphs.length; i++) { n = nodeOf(graphs[i], id); if (n) return n.o; }
      return (LOG.papers && LOG.papers[id]) || {};
    }
    function labelFrom(o, id) { var p = paper(id); return (o && o.label) || (p && (p.label || p.title)) || (o && o.title) || id; }
    function label(id) { var n = nodeOf(cur, id); return n ? n.label : labelFrom(info(id), id); }
    function titleOf(id) { var p = paper(id); return (p && p.title) || info(id).title || label(id); }
    function yearOf(id) { var y = info(id).year; if (y == null) { var p = paper(id); y = p && p.year; } return y == null ? null : y; }
    function yy(id) { var y = yearOf(id); return y == null ? "" : "’" + String(y).slice(-2); }
    function authorsOf(id) {
      var p = paper(id), a = (p && p.authors && p.authors.length ? p.authors : info(id).authors) || [];
      if (!a.length) { var f = (p && p.first_author) || info(id).first_author; return f ? surname(f) : ""; }
      if (a.length === 1) return surname(a[0]);
      if (a.length === 2) return surname(a[0]) + " & " + surname(a[1]);
      return surname(a[0]) + " et al.";
    }
    function listened(id) { var p = paper(id); return !!(p && p.listened); }
    function byline(id) { var b = []; var y = yearOf(id); if (y != null) b.push(y); var a = authorsOf(id); if (a) b.push(a); return b.join(" · "); }
    function isMe(u) { return ME.id != null && u != null && (typeof u === "object" ? same(u.id, ME.id) : same(u, ME.id)); }
    function makers(id) {
      var p = paper(id), v = (p && (p.made_by || (p.episodes && p.episodes.map(function (e) { return e.made_by; })))) || info(id).made_by || [];
      if (!Array.isArray(v)) v = [v];
      var out = [];
      v.forEach(function (u) { var n = isMe(u) ? "you" : nameOf(u); if (n && out.indexOf(n) < 0) out.push(n); });
      return out;
    }

    /* ---------- graph state: the hub's answer, then the edits still on their way ---------- */
    function normMeta(m) {
      m = m || {};
      var tags = m.tags != null ? m.tags : m.rule_tags;
      tags = Array.isArray(tags) ? tags : obj(tags) || (typeof tags === "string" ? tagList(tags) : []);
      var n = m.n != null ? m.n : m.count != null ? m.count : m.size != null ? m.size : Array.isArray(m.papers) ? m.papers.length : null;
      var o = { id: m.id, name: m.name, tags: Array.isArray(tags) ? tags : [], locked: m.locked == null ? null : !!m.locked, n: n,
        created_by: m.created_by && typeof m.created_by === "object" ? m.created_by.id : m.created_by };
      Object.keys(o).forEach(function (k) { if (o[k] == null) delete o[k]; });
      if (m.tags == null && m.rule_tags == null) delete o.tags;
      return o;
    }
    function normGraph(r) {
      r = r || {};
      var links = (r.links || r.edges || []).map(function (e) { return Array.isArray(e) ? { id: e[0] + ">" + e[1], src: e[0], dst: e[1], grade: e[2] } : e; });
      return { nodes: (r.nodes || []).filter(function (n) { return n && n.id != null; }), links: links, roots: r.roots || [], start: r.start || [],
        path: r.path || [], descendants: r.descendants || {} };
    }
    function viewData(g) {
      var d = g.data || { nodes: [], links: [] }, nodes = d.nodes.slice(), links = d.links.slice();
      pending.forEach(function (op) { op.apply(g, nodes, links); });
      return { nodes: nodes, links: links };
    }
    function centreWorld(v) { return W && v ? { x: (W / 2 - v.x) / v.k, y: (H / 2 - v.y) / v.k } : { x: 0, y: 0 }; }
    function rebuild(g) {
      var v = viewData(g), old = g._s, D = g.data || {}, view = old ? old.view : { k: 1, x: 0, y: 0 };
      var idx = {}, nodes = [], mv = [], guess = [], carry = {};
      if (moves && old && g === cur) moves.list.forEach(function (m) { carry[m.n.id] = m; });
      v.nodes.forEach(function (o) {
        if (idx[o.id] != null) return;
        var i = nodes.length, p = old && old.idx[o.id] != null ? old.nodes[old.idx[o.id]] : null;
        var n = { id: o.id, i: i, o: o, label: labelFrom(o, o.id), x: 0, y: 0, vx: 0, vy: 0, fx: null, fy: null, deg: 0, par: [], ch: [], start: false, ox: null, oy: null, guess: 0 };
        var xy = o.x != null && o.y != null && isFinite(o.x) && isFinite(o.y);
        if (p) { n.x = p.x; n.y = p.y; n.vx = p.vx; n.vy = p.vy; n.fx = p.fx; n.fy = p.fy; n.ox = p.ox; n.oy = p.oy; n.guess = p.guess; }
        if (xy) {
          if (!p) { n.x = o.x; n.y = o.y; }
          else if (p.ox !== o.x || p.oy !== o.y || p.guess) mv.push({ n: n, x1: o.x, y1: o.y });   // the hub moved it: ease there
          n.ox = o.x; n.oy = o.y; n.guess = 0;
        } else if (!p) { n.guess = -1; guess.push(n); }
        else if (p.guess) guess.push(n);
        idx[o.id] = i; nodes.push(n);
      });
      var links = [], lid = {};
      v.links.forEach(function (e) {
        var s = idx[e.src], t = idx[e.dst];
        if (s == null || t == null || s === t || lid[String(e.id)]) return;
        var l = { id: e.id, s: s, t: t, grade: GNAME[e.grade] ? e.grade : "w", e: e };
        links.push(l); lid[String(e.id)] = l;
        nodes[s].deg++; nodes[t].deg++; nodes[s].ch.push(t); nodes[t].par.push(s);
      });
      links.forEach(function (l) { var a = nodes[l.s].deg, b = nodes[l.t].deg; l.str = 1 / Math.min(a, b); l.bias = a / (a + b); });
      (D.start || []).forEach(function (id) { if (idx[id] != null) nodes[idx[id]].start = true; });
      // A paper the hub has not placed yet (layout.py runs 30 s after a change): by its placed
      // neighbours, else in the middle of the screen; placed again when more neighbours show up.
      var mid = centreWorld(view);
      guess.forEach(function (n) {
        var sx = 0, sy = 0, k = 0;
        n.par.concat(n.ch).forEach(function (j) { var m = nodes[j]; if (!m.guess) { sx += m.x; sy += m.y; k++; } });
        if (n.guess > 0 && k <= n.guess) return;        // placed from as many before: stays
        var a = n.i * 2.39996, r = S.dist * 0.6, x = (k ? sx / k : mid.x) + Math.cos(a) * r, y = (k ? sy / k : mid.y) + Math.sin(a) * r;
        if (n.guess === -1) { n.x = x; n.y = y; } else mv.push({ n: n, x1: x, y1: y });
        n.guess = Math.max(k, 0.5);
      });
      // an ease still under way goes on to where it was going
      var moving = {}; mv.forEach(function (m) { moving[m.n.id] = true; });
      Object.keys(carry).forEach(function (id) { if (!moving[id] && idx[id] != null) mv.push({ n: nodes[idx[id]], x1: carry[id].x1, y1: carry[id].y1 }); });
      g._s = { nodes: nodes, links: links, idx: idx, lid: lid, alpha: old ? old.alpha : 0, target: old ? old.target : 0, view: view, fitted: old ? old.fitted : false };
      g.dirty = false;
      if (g !== cur) { mv.forEach(function (m) { m.n.x = m.x1; m.n.y = m.y1; }); return; }
      moves = mv.length ? { t0: performance.now(), dur: 650, list: mv.map(function (m) { return { n: m.n, x0: m.n.x, y0: m.n.y, x1: m.x1, y1: m.y1 }; }) } : null;
      if (dragN) { dragN = nodeOf(g, dragN.id); }
      hoverI = null; hoverL = null; hlKey = null;
      if (selId != null && idx[selId] == null) { selId = null; editLabel = null; }
      if (selLink != null && !lid[String(selLink)]) selLink = null;
      if (linkFrom != null && idx[linkFrom] == null) linkFrom = null;
      if (draft && (idx[draft.src] == null || idx[draft.dst] == null)) draft = null;
      renderBanner();
      kick();
    }
    function rebuildAll() { graphs.forEach(function (g) { if (!g.data) return; if (g === cur) rebuild(g); else g.dirty = true; }); if (cur) afterChange(); }
    function afterChange() {
      if (!cur || !cur._s) return;
      var s = cur._s;
      if (!s.fitted && s.nodes.length && W && !userMoved) { fit(false); s.fitted = true; }
      renderTabs(); renderStart(); refreshCard(); renderSettings(); renderEmpty(); runQuery(); kick();
    }
    function forData(fn) { graphs.forEach(function (g) { if (g.data) fn(g, g.data); }); }
    function has(nodes, id) { for (var i = 0; i < nodes.length; i++) if (nodes[i].id === id) return true; return false; }
    function radius(n) { return (3.5 + 1.25 * Math.sqrt(n.deg)) * S.node; }

    /* ---------- physics (only while a node is dragged: the hub's positions are at rest) ---------- */
    function tick(s) {
      s.alpha += (s.target - s.alpha) * ALPHA_DECAY;
      var a = s.alpha, N = s.nodes, L = s.links, n = N.length, i, j, dist = S.dist;
      for (i = 0; i < L.length; i++) {
        var l = L[i], so = N[l.s], ta = N[l.t];
        var x = ta.x + ta.vx - so.x - so.vx, y = ta.y + ta.vy - so.y - so.vy, d = Math.sqrt(x * x + y * y) || 1e-6;
        var f = (d - dist) / d * a * l.str * S.link; x *= f; y *= f;
        ta.vx -= x * l.bias; ta.vy -= y * l.bias; so.vx += x * (1 - l.bias); so.vy += y * (1 - l.bias);
      }
      var ch = -S.repel * 32, dmax2 = Math.pow(dist * 8, 2);
      for (i = 0; i < n; i++) N[i].r = radius(N[i]);
      for (i = 0; i < n; i++) {
        var A = N[i], ra = A.r;
        for (j = i + 1; j < n; j++) {
          var B = N[j], dx = B.x - A.x, dy = B.y - A.y, d2 = dx * dx + dy * dy;
          if (d2 === 0) { dx = (i - j) * 1e-3; dy = 1e-3; d2 = dx * dx + dy * dy; }
          if (d2 < dmax2) { var w = ch * a / Math.max(d2, 36); A.vx += dx * w; A.vy += dy * w; B.vx -= dx * w; B.vy -= dy * w; }
          var rr = ra + B.r + 3;
          if (d2 < rr * rr) { var dd = Math.sqrt(d2), push = (rr - dd) / dd * 0.35; A.vx -= dx * push; A.vy -= dy * push; B.vx += dx * push; B.vy += dy * push; }
        }
      }
      var cs = S.center * 0.08 * a;
      for (i = 0; i < n; i++) {
        var m = N[i];
        m.vx -= m.x * cs; m.vy -= m.y * cs;
        if (m.fx != null) { m.x = m.fx; m.y = m.fy; m.vx = 0; m.vy = 0; } else { m.vx *= VDECAY; m.vy *= VDECAY; m.x += m.vx; m.y += m.vy; }
      }
    }
    function reheat(a) { if (!cur || !cur._s) return; var s = cur._s; s.alpha = Math.max(s.alpha, a); kick(); }

    /* ---------- drawing ---------- */
    function resize() {
      var r = root.getBoundingClientRect();
      DPR = window.devicePixelRatio || 1; W = r.width; H = r.height;
      if (!W || !H) return;
      cv.width = gv.width = Math.round(W * DPR); cv.height = gv.height = Math.round(H * DPR);
      if (cur && cur._s && !cur._s.fitted && cur._s.nodes.length) { fit(false); cur._s.fitted = true; }
      kick();
    }
    function neighbours(s, i) {
      var set = {}; set[i] = true;
      s.nodes[i].par.forEach(function (j) { set[j] = true; }); s.nodes[i].ch.forEach(function (j) { set[j] = true; });
      return set;
    }
    function nodeColor(n) { return listened(n.id) ? C.done : n.start ? C.start : C.node; }
    /* ---------- the graph's picture: one scene, two ways to draw it ---------- */
    var colorCache = {}, cc = document.createElement("canvas").getContext("2d");
    function rgba(css) {
      var c = colorCache[css];
      if (c) return c;
      cc.fillStyle = "#000"; cc.fillStyle = css;
      var v = String(cc.fillStyle), m;
      if (v.charAt(0) === "#") m = [parseInt(v.slice(1, 3), 16), parseInt(v.slice(3, 5), 16), parseInt(v.slice(5, 7), 16), 1];
      else { m = (v.match(/[\d.]+/g) || [0, 0, 0]).map(Number); if (m.length < 4) m.push(1); }
      return (colorCache[css] = [m[0] / 255, m[1] / 255, m[2] / 255, m[3]]);
    }
    // WebGL: every line, arrow head and node of a frame in one buffer and one draw call. Lines
    // and discs are quads whose edge the fragment shader smooths, so they stay crisp at any zoom.
    var gl = null, glProg = null, glBuf = null, glLoc = null, VB = new Float32Array(11 * 8192), vn = 0;
    function initGL() {
      if (opts.webgl === false) return false;
      // a software WebGL is slower than 2D; the tests ask for it by name to check this path
      var soft = opts.webgl === "software";
      try { gl = gv.getContext("webgl", { failIfMajorPerformanceCaveat: !soft, antialias: false, alpha: false, depth: false, stencil: false, premultipliedAlpha: true }); } catch (e) { gl = null; }
      if (!gl) return false;
      function sh(type, src) { var s = gl.createShader(type); gl.shaderSource(s, src); gl.compileShader(s); return gl.getShaderParameter(s, gl.COMPILE_STATUS) ? s : null; }
      var vs = sh(gl.VERTEX_SHADER,
        "attribute vec2 p; attribute vec4 c; attribute vec2 u; attribute vec3 q; uniform vec2 s;" +
        "varying vec4 vc; varying vec2 vu; varying vec3 vq;" +
        "void main() { gl_Position = vec4(p.x / s.x * 2.0 - 1.0, 1.0 - p.y / s.y * 2.0, 0.0, 1.0); vc = c; vu = u; vq = q; }");
      var fs = sh(gl.FRAGMENT_SHADER,
        "precision mediump float; varying vec4 vc; varying vec2 vu; varying vec3 vq;" +
        "void main() { float cov = 1.0;" +
        " if (vq.x > 2.5) cov = clamp(vq.y + 0.5 - abs(vu.y), 0.0, 1.0);" +                  // line: vq.y half width
        " else if (vq.x > 1.5) cov = clamp(vq.z + 0.5 - abs(length(vu) - vq.y), 0.0, 1.0);" + // ring: radius, half width
        " else if (vq.x > 0.5) cov = clamp(vq.y + 0.5 - length(vu), 0.0, 1.0);" +            // disc: radius
        " gl_FragColor = vec4(vc.rgb * vc.a * cov, vc.a * cov); }");
      if (!vs || !fs) { gl = null; return false; }
      glProg = gl.createProgram(); gl.attachShader(glProg, vs); gl.attachShader(glProg, fs); gl.linkProgram(glProg);
      if (!gl.getProgramParameter(glProg, gl.LINK_STATUS)) { gl = null; return false; }
      glBuf = gl.createBuffer();
      glLoc = { p: gl.getAttribLocation(glProg, "p"), c: gl.getAttribLocation(glProg, "c"), u: gl.getAttribLocation(glProg, "u"),
        q: gl.getAttribLocation(glProg, "q"), s: gl.getUniformLocation(glProg, "s") };
      gl.disable(gl.DEPTH_TEST); gl.enable(gl.BLEND); gl.blendFunc(gl.ONE, gl.ONE_MINUS_SRC_ALPHA);
      return true;
    }
    function vert(x, y, c, a, ux, uy, kind, qa, qb) {
      if ((vn + 1) * 11 > VB.length) { var nb = new Float32Array(VB.length * 2); nb.set(VB); VB = nb; }
      var o = vn * 11;
      VB[o] = x; VB[o + 1] = y; VB[o + 2] = c[0]; VB[o + 3] = c[1]; VB[o + 4] = c[2]; VB[o + 5] = c[3] * a;
      VB[o + 6] = ux; VB[o + 7] = uy; VB[o + 8] = kind; VB[o + 9] = qa; VB[o + 10] = qb; vn++;
    }
    function quad(x, y, e, c, a, kind, qa, qb) {
      vert(x - e, y - e, c, a, -e, -e, kind, qa, qb); vert(x + e, y - e, c, a, e, -e, kind, qa, qb); vert(x + e, y + e, c, a, e, e, kind, qa, qb);
      vert(x - e, y - e, c, a, -e, -e, kind, qa, qb); vert(x + e, y + e, c, a, e, e, kind, qa, qb); vert(x - e, y + e, c, a, -e, e, kind, qa, qb);
    }
    var GLB = {
      begin: function () { vn = 0; },
      line: function (x0, y0, x1, y1, col, a, w) {
        var c = rgba(col); if (w < 1) { a *= w; w = 1; }
        var hw = w / 2, e = hw + 1, dx = x1 - x0, dy = y1 - y0, d = Math.sqrt(dx * dx + dy * dy) || 1, nx = -dy / d * e, ny = dx / d * e;
        vert(x0 + nx, y0 + ny, c, a, 0, e, 3, hw, 0); vert(x0 - nx, y0 - ny, c, a, 0, -e, 3, hw, 0); vert(x1 + nx, y1 + ny, c, a, 0, e, 3, hw, 0);
        vert(x0 - nx, y0 - ny, c, a, 0, -e, 3, hw, 0); vert(x1 - nx, y1 - ny, c, a, 0, -e, 3, hw, 0); vert(x1 + nx, y1 + ny, c, a, 0, e, 3, hw, 0);
      },
      tri: function (x0, y0, x1, y1, x2, y2, col, a) { var c = rgba(col); vert(x0, y0, c, a, 0, 0, 0, 0, 0); vert(x1, y1, c, a, 0, 0, 0, 0, 0); vert(x2, y2, c, a, 0, 0, 0, 0, 0); },
      disc: function (x, y, r, col, a) { quad(x, y, r + 1, rgba(col), a, 1, r, 0); },
      ring: function (x, y, R, hw, col, a) { quad(x, y, R + hw + 1, rgba(col), a, 2, R, hw); },
      end: function () {
        var bg = rgba(C.bg);
        gl.viewport(0, 0, gv.width, gv.height); gl.clearColor(bg[0], bg[1], bg[2], 1); gl.clear(gl.COLOR_BUFFER_BIT);
        if (!vn) return;
        gl.useProgram(glProg); gl.bindBuffer(gl.ARRAY_BUFFER, glBuf);
        gl.bufferData(gl.ARRAY_BUFFER, VB.subarray(0, vn * 11), gl.DYNAMIC_DRAW);
        var F = 44;
        gl.enableVertexAttribArray(glLoc.p); gl.vertexAttribPointer(glLoc.p, 2, gl.FLOAT, false, F, 0);
        gl.enableVertexAttribArray(glLoc.c); gl.vertexAttribPointer(glLoc.c, 4, gl.FLOAT, false, F, 8);
        gl.enableVertexAttribArray(glLoc.u); gl.vertexAttribPointer(glLoc.u, 2, gl.FLOAT, false, F, 24);
        gl.enableVertexAttribArray(glLoc.q); gl.vertexAttribPointer(glLoc.q, 3, gl.FLOAT, false, F, 32);
        gl.uniform2f(glLoc.s, gv.width, gv.height);
        gl.drawArrays(gl.TRIANGLES, 0, vn);
      }
    };
    // 2D, when there is no WebGL: one canvas call per line and node (the canvas's fast case for
    // a single line or circle), each style set only when it changes.
    var g2 = null, last = {};
    function st2(col, a, w) {
      if (last.c !== col) { g2.strokeStyle = g2.fillStyle = col; last.c = col; }
      if (last.a !== a) { g2.globalAlpha = a; last.a = a; }
      if (w != null && last.w !== w) { g2.lineWidth = w; last.w = w; }
    }
    var B2D = {
      begin: function () { last = {}; g2.setTransform(1, 0, 0, 1, 0, 0); g2.globalAlpha = 1; g2.fillStyle = C.bg; g2.fillRect(0, 0, gv.width, gv.height); },
      line: function (x0, y0, x1, y1, col, a, w) { st2(col, a, w); g2.beginPath(); g2.moveTo(x0, y0); g2.lineTo(x1, y1); g2.stroke(); },
      tri: function (x0, y0, x1, y1, x2, y2, col, a) { st2(col, a); g2.beginPath(); g2.moveTo(x0, y0); g2.lineTo(x1, y1); g2.lineTo(x2, y2); g2.closePath(); g2.fill(); },
      disc: function (x, y, r, col, a) { st2(col, a); g2.beginPath(); g2.arc(x, y, r, 0, 6.2832); g2.fill(); },
      ring: function (x, y, R, hw, col, a) { st2(col, a, hw * 2); g2.beginPath(); g2.arc(x, y, R, 0, 6.2832); g2.stroke(); },
      end: function () {}
    };
    var GA = { e: 0.95, s: 0.7, w: 0.42 }, KINDS = ["w", "s", "e", "lit"];
    function edge(B, a, b, col, al, w, arrow, awd) {
      var dx = b.sx - a.sx, dy = b.sy - a.sy, d = Math.sqrt(dx * dx + dy * dy) || 1, ux = dx / d, uy = dy / d;
      if (d <= a.sr + b.sr + DPR) return;
      var ex = b.sx - ux * (b.sr + DPR), ey = b.sy - uy * (b.sr + DPR);
      B.line(a.sx + ux * a.sr, a.sy + uy * a.sr, arrow ? ex - ux * awd * 1.2 : ex, arrow ? ey - uy * awd * 1.2 : ey, col, al, w);
      if (arrow) B.tri(ex, ey, ex - ux * awd * 2 - uy * awd, ey - uy * awd * 2 + ux * awd, ex - ux * awd * 2 + uy * awd, ey - uy * awd * 2 - ux * awd, col, al);
    }
    // The scene in device pixels: lines by kind (dim first, lit last), then nodes by colour.
    function scene(B) {
      var s = cur._s, v = s.view, k = v.k, N = s.nodes, L = s.links, i, j, n;
      var sc = k * DPR, ox = v.x * DPR, oy = v.y * DPR, dim = 1 - 0.88 * hl, set = hlSet;
      var lwd = S.line * 0.9 * Math.sqrt(k) * DPR, awd = 3.2 * Math.max(1, S.line) * Math.sqrt(k) * DPR;
      var arrow = S.arrows && awd > 1.6 * DPR;
      var X0 = -40 * DPR, X1 = (W + 40) * DPR, Y0 = -40 * DPR, Y1 = (H + 40) * DPR;
      for (i = 0; i < N.length; i++) { n = N[i]; n.r = radius(n); n.sx = n.x * sc + ox; n.sy = n.y * sc + oy; n.sr = n.r * sc; }
      B.begin();
      var byKind = { w: [], s: [], e: [], lit: [] };
      for (i = 0; i < L.length; i++) {
        var l = L[i], a = N[l.s], b = N[l.t];
        if ((a.sx < X0 && b.sx < X0) || (a.sx > X1 && b.sx > X1) || (a.sy < Y0 && b.sy < Y0) || (a.sy > Y1 && b.sy > Y1)) continue;
        var lit = set && (hlLink ? l === hlLink : hlFocus != null && (l.s === hlFocus || l.t === hlFocus));
        byKind[lit ? "lit" : GA[l.grade] ? l.grade : "w"].push(l);
      }
      for (var q = 0; q < KINDS.length; q++) {
        var kind = KINDS[q], list = byKind[kind], isLit = kind === "lit";
        var al = isLit ? 0.35 + 0.65 * hl : GA[kind] * (set ? dim : 1), col = isLit ? C.hi : C.line;
        for (j = 0; j < list.length; j++) {
          var w = isLit ? (list[j] === hlLink ? lwd * 2.4 : lwd * 1.4) : lwd;
          edge(B, N[list[j].s], N[list[j].t], col, al, w, arrow, awd);
        }
      }
      // the link being made: in the accent, with its arrow whatever the settings say
      if (draft && s.idx[draft.src] != null && s.idx[draft.dst] != null) edge(B, N[s.idx[draft.src]], N[s.idx[draft.dst]], C.start, 1, Math.max(lwd * 2, 1.5 * DPR), true, Math.max(awd, 3 * DPR));
      var groups = {}, order = [];
      for (i = 0; i < N.length; i++) {
        n = N[i];
        if (n.sx + n.sr < X0 || n.sx - n.sr > X1 || n.sy + n.sr < Y0 || n.sy - n.sr > Y1) continue;
        var on = !set || set[i] ? 1 : 0;
        var c2 = (i === hlFocus && hl > 0.01) || n === dragN ? C.hi : nodeColor(n), key = on + c2;
        if (!groups[key]) { groups[key] = { on: on, col: c2, ns: [] }; order.push(key); }
        groups[key].ns.push(n);
      }
      order.sort(function (x, y) { return groups[x].on - groups[y].on; });
      for (q = 0; q < order.length; q++) {
        var g = groups[order[q]];
        for (j = 0; j < g.ns.length; j++) B.disc(g.ns[j].sx, g.ns[j].sy, g.ns[j].sr, g.col, g.on ? 1 : dim);
      }
      if (selId != null && s.idx[selId] != null) { n = N[s.idx[selId]]; B.ring(n.sx, n.sy, n.sr + 3 * DPR, 0.8 * DPR, C.hi, 1); }
      if (linkFrom != null && s.idx[linkFrom] != null) { n = N[s.idx[linkFrom]]; B.ring(n.sx, n.sy, n.sr + 3 * DPR, 1.2 * DPR, C.start, 1); }
      B.end();
    }
    function draw() {
      if (!cur || !cur._s || !W) return;
      if (gl && gl.isContextLost()) return;
      scene(gl ? GLB : B2D);
      var s = cur._s, v = s.view, k = v.k, N = s.nodes, i, n, set = hlSet, dim = 1 - 0.88 * hl;
      ctx.setTransform(DPR, 0, 0, DPR, 0, 0); ctx.globalAlpha = 1; ctx.clearRect(0, 0, W, H);
      // labels in screen space, so the text stays crisp at any zoom
      var fs = Math.max(10, Math.min(15, 12 * Math.sqrt(k)));
      ctx.font = fs + "px " + FONT; ctx.textAlign = "center"; ctx.textBaseline = "top"; ctx.fillStyle = C.text;
      var thr = (0.25 + S.fade * 1.1) * Math.max(1, Math.sqrt(N.length / 60));
      for (i = 0; i < N.length; i++) {
        n = N[i];
        // a paper many others link to keeps its label further out, as the zoom falls
        var ke = k * (0.8 + 0.09 * Math.sqrt(n.deg)), la = Math.max(0, Math.min(1, (ke - thr * 0.6) / (thr * 0.4)));
        if (n.start) la = Math.max(la, 0.85);
        if (set) la = set[i] ? Math.max(la, hl) : la * dim;
        if (la < 0.03) continue;
        var px = n.x * k + v.x, py = (n.y + n.r) * k + v.y + 4;
        if (px < -200 || px > W + 200 || py < -40 || py > H + 40) continue;
        ctx.globalAlpha = Math.round(la * 10) / 10;
        ctx.fillText(n.label, px, py);
      }
      ctx.globalAlpha = 1;
    }

    /* ---------- animation: one frame per event, none at rest ---------- */
    function kick() { if (!running && visible) { running = true; requestAnimationFrame(frame); } }
    // What is lit: the link being made, else a paper (dragged, hovered, picked) with its links,
    // else one link (hovered, picked). Only the key is worked out each frame; the set when it changes.
    function pairSet(a, b) { var o = {}; o[a] = o[b] = true; return o; }
    function focusNow(s) {
      if (draft) {
        var da = s.idx[draft.src], db = s.idx[draft.dst];
        if (da != null && db != null) return { key: "d" + da + ":" + db, set: function () { return pairSet(da, db); }, node: null, link: null };
      }
      var i = dragN ? dragN.i : hoverI;
      var L = i == null ? (hoverL || (selLink != null ? s.lid[String(selLink)] : null)) : null;
      if (L) return { key: "l" + L.s + ":" + L.t, set: function () { return pairSet(L.s, L.t); }, node: null, link: L };
      if (i == null) { var id = linkFrom != null ? linkFrom : selId; if (id != null && s.idx[id] != null) i = s.idx[id]; }
      return i != null ? { key: "n" + i, set: function () { return neighbours(s, i); }, node: i, link: null } : null;
    }
    function frame(t) {
      if (!visible || !cur || !cur._s) { running = false; return; }
      var s = cur._s, busy = false;
      if (s.alpha > ALPHA_MIN || s.target > 0) { tick(s); busy = true; }
      if (moves) {
        var mp = Math.min(1, Math.max(0, (t - moves.t0) / moves.dur)), me = ease(mp);
        moves.list.forEach(function (m) { if (m.n === dragN) return; m.n.x = m.x0 + (m.x1 - m.x0) * me; m.n.y = m.y0 + (m.y1 - m.y0) * me; });
        if (mp >= 1) moves = null; else busy = true;
      }
      var f = focusNow(s), wantHl = f || qset ? 1 : 0;
      if (f) { if (f.key !== hlKey) { hlKey = f.key; hlSet = f.set(); hlFocus = f.node; hlLink = f.link; } }
      else if (qset) { hlKey = "q"; hlSet = qset; hlFocus = null; hlLink = null; }
      hl += (wantHl - hl) * 0.22;
      if (Math.abs(wantHl - hl) < 0.01) hl = wantHl; else busy = true;
      if (hl === 0 && !wantHl) { hlSet = null; hlFocus = null; hlLink = null; hlKey = null; }
      if (anim) {
        var p = Math.min(1, (t - anim.t0) / anim.dur), e = ease(p), v = s.view;
        v.k = anim.k0 + (anim.k1 - anim.k0) * e; v.x = anim.x0 + (anim.x1 - anim.x0) * e; v.y = anim.y0 + (anim.y1 - anim.y0) * e;
        if (p >= 1) anim = null; else busy = true;
      }
      draw();
      if (busy) requestAnimationFrame(frame); else running = false;
    }

    /* ---------- view ---------- */
    function clampK(k) { return Math.min(4, Math.max(0.08, k)); }
    function moveTo(k, x, y, smooth) {
      var v = cur._s.view;
      if (!smooth) { v.k = k; v.x = x; v.y = y; kick(); return; }
      anim = { t0: performance.now(), dur: 450, k0: v.k, x0: v.x, y0: v.y, k1: k, x1: x, y1: y }; kick();
    }
    function panelOpen() { for (var k in PANELS) if (!PANELS[k].hidden) return k; return null; }
    function fit(smooth) {
      if (!W || !cur || !cur._s || !cur._s.nodes.length) return;
      var N = cur._s.nodes, x0 = Infinity, x1 = -Infinity, y0 = Infinity, y1 = -Infinity;
      N.forEach(function (n) { var r = radius(n); x0 = Math.min(x0, n.x - r); x1 = Math.max(x1, n.x + r); y0 = Math.min(y0, n.y - r); y1 = Math.max(y1, n.y + r + 18); });
      var tp = phone() ? 124 : 88, bt = phone() ? 24 : 40, sl = 24, sr = !phone() && panelOpen() ? 312 : 24;
      // a graph of one or two papers is not blown up to the whole screen
      var k = clampK(Math.min(2, (W - sl - sr) / Math.max(1, x1 - x0), (H - tp - bt) / Math.max(1, y1 - y0)));
      moveTo(k, sl + (W - sl - sr) / 2 - (x0 + x1) / 2 * k, tp + (H - tp - bt) / 2 - (y0 + y1) / 2 * k, smooth);
    }
    function centerAt(x, y) {
      var k = Math.max(cur._s.view.k, 1.1);
      var cx = phone() ? W / 2 : W / 2 + 60, cy = phone() ? H * 0.3 : H / 2;
      userMoved = true;
      moveTo(k, cx - x * k, cy - y * k, true);
    }
    function centerOn(i) { var n = cur._s.nodes[i]; centerAt(n.x, n.y); }
    function zoomAt(f, px, py) {
      var v = cur._s.view, k = clampK(v.k * f); f = k / v.k;
      v.x = px - (px - v.x) * f; v.y = py - (py - v.y) * f; v.k = k; anim = null; userMoved = true; kick();
    }
    function local(e) { var r = cv.getBoundingClientRect(); return { x: e.clientX - r.left, y: e.clientY - r.top }; }
    function toWorld(p) { var v = cur._s.view; return { x: (p.x - v.x) / v.k, y: (p.y - v.y) / v.k }; }
    function hit(p) {
      var w = toWorld(p), N = cur._s.nodes, best = null, bd = Infinity, slop = (phone() ? 12 : 5) / cur._s.view.k;
      for (var i = 0; i < N.length; i++) {
        var n = N[i], d = Math.hypot(n.x - w.x, n.y - w.y) - radius(n);
        if (d < slop && d < bd) { bd = d; best = n; }
      }
      return best;
    }
    // A link near the pointer, measured on the screen (so the reach is the same at any zoom):
    // the distance to the line between the two discs' edges.
    function hitLink(p) {
      var s = cur._s, v = s.view, k = v.k, N = s.nodes, L = s.links, tol = phone() ? 14 : 6, best = null, bd = tol;
      for (var i = 0; i < L.length; i++) {
        var a = N[L[i].s], b = N[L[i].t];
        var ax = a.x * k + v.x, ay = a.y * k + v.y, bx = b.x * k + v.x, by = b.y * k + v.y;
        if (p.x < Math.min(ax, bx) - tol || p.x > Math.max(ax, bx) + tol || p.y < Math.min(ay, by) - tol || p.y > Math.max(ay, by) + tol) continue;
        var dx = bx - ax, dy = by - ay, d = Math.sqrt(dx * dx + dy * dy);
        if (!d) continue;
        var ra = radius(a) * k / d, rb = 1 - radius(b) * k / d;
        if (ra >= rb) continue;
        var t = Math.max(ra, Math.min(rb, ((p.x - ax) * dx + (p.y - ay) * dy) / (d * d)));
        var qx = ax + t * dx - p.x, qy = ay + t * dy - p.y, e = Math.sqrt(qx * qx + qy * qy);
        if (e < bd) { bd = e; best = L[i]; }
      }
      return best;
    }

    /* ---------- hover card ---------- */
    var tipKey = null, tw = 0, th = 0;
    function showTip(key, title, sub, p) {
      if (key == null) { tip.hidden = true; tipKey = null; return; }
      if (tipKey !== key || tip.hidden) {
        tip.textContent = "";
        tip.appendChild(h("div", "pm-tip-t", title));
        if (sub) tip.appendChild(h("div", "pm-tip-s", sub));
        tip.hidden = false; tipKey = key; tw = tip.offsetWidth; th = tip.offsetHeight;
      }
      var x = p.x + 14, y = p.y + 16;
      if (x + tw > W - 8) x = p.x - tw - 14;
      if (y + th > H - 8) y = p.y - th - 12;
      tip.style.transform = "translate(" + Math.max(8, x) + "px," + Math.max(8, y) + "px)";
    }
    function linkWords(l) { var N = cur._s.nodes; return N[l.t].label + " builds on " + N[l.s].label; }

    /* ---------- pointer: drag nodes, pan, pinch, wheel; a click picks ---------- */
    var ptrs = {}, mode = null, down = null, moved = false, pinch = null;
    function pcount() { return Object.keys(ptrs).length; }
    function release() { if (dragN) { dragN.fx = dragN.fy = null; if (cur && cur._s) cur._s.target = 0; dragN = null; } }
    cv.addEventListener("pointerdown", function (e) {
      if (!cur || !cur._s) return;
      cv.setPointerCapture(e.pointerId);
      var p = local(e); ptrs[e.pointerId] = p; tip.hidden = true;
      if (pcount() === 1) {
        down = { x: p.x, y: p.y, lx: p.x, ly: p.y, shift: e.shiftKey }; moved = false;
        var n = hit(p); mode = n ? "node" : "pan"; dragN = n;
      } else if (pcount() === 2) {
        release();
        var ids = Object.keys(ptrs), a = ptrs[ids[0]], b = ptrs[ids[1]], v = cur._s.view;
        pinch = { d: Math.hypot(a.x - b.x, a.y - b.y) || 1, mx: (a.x + b.x) / 2, my: (a.y + b.y) / 2, k: v.k, x: v.x, y: v.y };
        mode = "pinch"; moved = true;
      }
      kick();
    });
    cv.addEventListener("pointermove", function (e) {
      if (!cur || !cur._s) return;
      var p = local(e);
      if (!ptrs[e.pointerId]) {
        if (e.pointerType === "mouse") {
          var n = hit(p), i = n ? n.i : null, l = n ? null : hitLink(p);
          cv.classList.toggle("over", !!(n || l));
          if (i !== hoverI || l !== hoverL) { hoverI = i; hoverL = l; kick(); }
          if (n) showTip("n" + n.id, titleOf(n.id), byline(n.id), p);
          else if (l) showTip("l" + l.id, linkWords(l), cap(GNAME[l.grade]) + " link", p);
          else showTip(null);
        }
        return;
      }
      ptrs[e.pointerId] = p;
      if (mode === "pinch" && pcount() === 2) {
        var ids = Object.keys(ptrs), a = ptrs[ids[0]], b = ptrs[ids[1]], v = cur._s.view;
        var k = clampK(pinch.k * Math.hypot(a.x - b.x, a.y - b.y) / pinch.d), mx = (a.x + b.x) / 2, my = (a.y + b.y) / 2;
        var wx = (pinch.mx - pinch.x) / pinch.k, wy = (pinch.my - pinch.y) / pinch.k;
        v.k = k; v.x = mx - wx * k; v.y = my - wy * k; userMoved = true; anim = null; kick();
        return;
      }
      if (!down) return;
      if (!moved && Math.hypot(p.x - down.x, p.y - down.y) > 4) {
        moved = true; userMoved = true;
        if (mode === "node") { cur._s.target = 0.3; reheat(0.3); }
        cv.classList.add("grabbing");
      }
      if (!moved) return;
      if (mode === "node" && dragN) { var w = toWorld(p); dragN.fx = w.x; dragN.fy = w.y; kick(); }
      else if (mode === "pan") { var v2 = cur._s.view; v2.x += p.x - down.lx; v2.y += p.y - down.ly; anim = null; kick(); }
      down.lx = p.x; down.ly = p.y;
    });
    function up(e) {
      if (!ptrs[e.pointerId]) return;
      delete ptrs[e.pointerId];
      cv.classList.remove("grabbing");
      var shift = !!(down && down.shift), at = down ? { x: down.x, y: down.y } : local(e);
      if (mode === "node" && dragN) {
        var n = dragN;
        release();
        if (!moved) pick(n.id, shift);
        if (e.pointerType !== "mouse") hoverI = null;
      } else if (mode === "pan" && !moved && cur && cur._s) {
        var l = hitLink(at);
        if (l && linkFrom == null) selectLink(l.id, false);
        else if (linkFrom == null) select(null);
      }
      if (pcount() === 1) { var q = ptrs[Object.keys(ptrs)[0]]; down = { x: q.x, y: q.y, lx: q.x, ly: q.y }; mode = "pan"; moved = true; }
      else if (!pcount()) { mode = null; down = null; }
      kick();
    }
    cv.addEventListener("pointerup", up);
    cv.addEventListener("pointercancel", up);
    cv.addEventListener("pointerleave", function (e) { if (e.pointerType === "mouse" && !ptrs[e.pointerId]) { tip.hidden = true; if (hoverI != null || hoverL) { hoverI = null; hoverL = null; kick(); } } });
    cv.addEventListener("wheel", function (e) { e.preventDefault(); if (!cur || !cur._s) return; tip.hidden = true; zoomAt(Math.exp(-e.deltaY * (e.ctrlKey ? 0.01 : 0.0015)), local(e).x, local(e).y); }, { passive: false });

    /* ---------- picking: a paper, a link, or the second paper of a new link ---------- */
    function canEdit(g) { g = g || cur; return EDIT && !!g && !g.tmp && (!g.meta.locked || ADMIN); }
    function pick(id, shift) {
      if (linkFrom != null) { if (id !== linkFrom) startDraft(linkFrom, id); return; }
      var from = draft ? draft.anchor : selId;
      if (shift && canEdit() && from != null && from !== id) { startDraft(from, id); return; }
      select(id, false);
    }
    function closePanelsOnPhone() { if (phone()) openPanel(null); }
    function select(id, focus) {
      selId = id; selLink = null; draft = null; linkFrom = null; editLabel = null;
      renderBanner();
      if (id == null) card.hidden = true;
      else {
        renderCard(true);
        if (focus && cur._s.idx[id] != null) centerOn(cur._s.idx[id]);
        closePanelsOnPhone();
      }
      kick();
    }
    function selectLink(lid, focus) {
      var l = cur._s.lid[String(lid)];
      if (!l) return;
      selId = null; selLink = l.id; draft = null; linkFrom = null; editLabel = null;
      renderBanner(); renderCard(true);
      if (focus) { var N = cur._s.nodes; centerAt((N[l.s].x + N[l.t].x) / 2, (N[l.s].y + N[l.t].y) / 2); }
      closePanelsOnPhone();
      kick();
    }
    function linkBetween(a, b) {
      var s = cur._s, i = s.idx[a], j = s.idx[b];
      for (var k = 0; k < s.links.length; k++) { var l = s.links[k]; if ((l.s === i && l.t === j) || (l.s === j && l.t === i)) return l; }
      return null;
    }
    function startLinkMode(id) {
      if (!canEdit()) return;
      selId = null; selLink = null; draft = null; linkFrom = id; editLabel = null;
      card.hidden = true; renderBanner(); kick();
    }
    function startDraft(a, b) {
      var old = linkBetween(a, b);
      if (old) { selectLink(old.id, false); say("These two are linked already: change its grade here."); return; }
      // the earlier paper is the one built on; the first picked when the years do not say
      var ya = yearOf(a), yb = yearOf(b), swap = ya != null && yb != null && yb < ya;
      draft = { src: swap ? b : a, dst: swap ? a : b, anchor: a };
      selId = null; selLink = null; linkFrom = null; editLabel = null;
      renderBanner(); renderCard(true); closePanelsOnPhone();
      // on the phone the card covers the lower half: the arrow being made goes above it
      if (phone()) {
        var s = cur._s, na = s.nodes[s.idx[a]], nb = s.nodes[s.idx[b]], v = s.view;
        userMoved = true; moveTo(v.k, W / 2 - (na.x + nb.x) / 2 * v.k, H * 0.26 - (na.y + nb.y) / 2 * v.k, true);
      }
      kick();
    }
    function cancelDraft() { var a = draft ? draft.anchor : linkFrom; draft = null; linkFrom = null; if (a != null && cur && cur._s && cur._s.idx[a] != null) select(a); else select(null); }
    function renderBanner() {
      banner.textContent = "";
      if (linkFrom == null || !cur || !cur._s || cur._s.idx[linkFrom] == null) { banner.hidden = true; return; }
      banner.appendChild(h("span", null, (phone() ? "Tap" : "Click") + " the paper to link with " + label(linkFrom)));
      banner.appendChild(btn("Cancel", "pm-btn", cancelDraft));
      banner.hidden = false;
    }

    /* ---------- the edits: shown at once, set right by the hub's answer ---------- */
    function edit(op) { pending.push(op); rebuildAll(); }
    function settle(op) { var i = pending.indexOf(op); if (i >= 0) pending.splice(i, 1); settled++; }
    var refetchT = null;
    function refetchSoon() { clearTimeout(refetchT); refetchT = setTimeout(function () { if (cur && visible) loadGraph(cur); loadLog(); }, 300); }
    function addLink(src, dst, grade) {
      var tmp = "t" + (++tmpSeq), made = new Date().toISOString();
      var op = { apply: function (g, nodes, links) {
        if (has(nodes, src) && has(nodes, dst)) links.push({ id: tmp, src: src, dst: dst, grade: grade, origin: "human", by: { id: ME.id, name: ME.name }, created_at: made, pending: true });
      } };
      draft = null; selLink = tmp; selId = null;
      edit(op);
      call("POST", "/api/links", { src: src, dst: dst, grade: grade }).then(function (r) {
        var l = (r && (r.link || r)) || {};
        settle(op);
        if (l.id != null) {
          var e = { id: l.id, src: l.src || src, dst: l.dst || dst, grade: l.grade || grade, origin: l.origin || "human", by: l.by || { id: ME.id, name: ME.name }, created_at: l.created_at || made };
          forData(function (g, d) {
            if (!has(d.nodes, e.src) || !has(d.nodes, e.dst)) return;
            d.links = d.links.filter(function (x) { return !same(x.id, e.id) && !(x.src === e.src && x.dst === e.dst); });
            d.links.push(e);
          });
          if (selLink === tmp) selLink = e.id;
        } else if (selLink === tmp) selLink = null;
        rebuildAll(); refetchSoon();
        say("Linked: " + label(dst) + " builds on " + label(src) + ".");
      }, function (err) {
        settle(op); if (selLink === tmp) selLink = null;
        rebuildAll();
        say("Could not add the link: " + errText(err) + ".");
      });
    }
    function setGrade(id, grade) {
      var l = cur._s.lid[String(id)];
      if (!l || l.grade === grade || l.e.pending) return;
      var op = { apply: function (g, nodes, links) { for (var i = 0; i < links.length; i++) if (same(links[i].id, id)) links[i] = Object.assign({}, links[i], { grade: grade }); } };
      edit(op);
      call("PUT", "/api/links/" + encodeURIComponent(id), { grade: grade }).then(function (r) {
        var got = (r && ((r.link && r.link.grade) || r.grade)) || grade;
        settle(op);
        forData(function (g, d) { d.links = d.links.map(function (x) { return same(x.id, id) ? Object.assign({}, x, { grade: got }) : x; }); });
        rebuildAll(); refetchSoon();
      }, function (err) {
        settle(op); rebuildAll();
        say("Could not change the grade: " + errText(err) + ".");
      });
    }
    function removeLink(id) {
      var l = cur._s.lid[String(id)];
      if (!l || l.e.pending) return;
      var words = label(l.e.src) + " → " + label(l.e.dst);
      var op = { apply: function (g, nodes, links) { for (var i = links.length - 1; i >= 0; i--) if (same(links[i].id, id)) links.splice(i, 1); } };
      selLink = null; card.hidden = true;
      edit(op);
      call("DELETE", "/api/links/" + encodeURIComponent(id)).then(function () {
        settle(op);
        forData(function (g, d) { d.links = d.links.filter(function (x) { return !same(x.id, id); }); });
        rebuildAll(); refetchSoon();
        say("Removed " + words + ".", EDIT ? undoMine("link.remove", id) : null);
      }, function (err) {
        settle(op); rebuildAll();
        say("Could not remove the link: " + errText(err) + ".");
      });
    }
    function stub(pid) {
      var p = paper(pid) || {};
      return { id: pid, title: p.title, year: p.year, authors: p.authors, made_by: p.made_by, x: null, y: null };
    }
    function addPaper(g, pid) {
      var op = { apply: function (x, nodes) { if (x === g && !has(nodes, pid)) nodes.push(stub(pid)); } };
      edit(op);
      if (g === cur) select(pid, true);
      call("POST", "/api/graphs/" + encodeURIComponent(g.id) + "/papers", { paper_id: pid }).then(function (r) {
        settle(op);
        if (g.data && !has(g.data.nodes, pid)) g.data.nodes = g.data.nodes.concat([(r && r.node) || stub(pid)]);
        rebuildAll(); loadGraph(g); loadLog(); soon({ list: true });
        say("Added " + label(pid) + " to " + quote(g.meta.name) + ".");
      }, function (err) {
        settle(op); rebuildAll();
        say("Could not add the paper: " + errText(err) + ".");
      });
    }
    function removePaper(g, pid) {
      var words = label(pid);
      var op = { apply: function (x, nodes) { if (x !== g) return; for (var i = nodes.length - 1; i >= 0; i--) if (nodes[i].id === pid) nodes.splice(i, 1); } };
      if (selId === pid) { selId = null; card.hidden = true; }
      edit(op);
      call("DELETE", "/api/graphs/" + encodeURIComponent(g.id) + "/papers/" + encodeURIComponent(pid)).then(function () {
        settle(op);
        if (g.data) g.data.nodes = g.data.nodes.filter(function (n) { return n.id !== pid; });
        rebuildAll(); refetchSoon(); soon({ list: true });
        say("Took " + words + " out of " + quote(g.meta.name) + ".", EDIT ? undoMine("graph.remove_paper", pid) : null);
      }, function (err) {
        settle(op); rebuildAll();
        say("Could not take the paper out: " + errText(err) + ".");
      });
    }
    function setLabel(pid, text) {
      text = String(text || "").replace(/\s+/g, " ").trim();
      var was = label(pid);
      editLabel = null;
      renderCard(true);                                   // the field goes (it has the focus: a refresh would skip it)
      if (!text || text === was) return;
      var op = { apply: function (g, nodes) { for (var i = 0; i < nodes.length; i++) if (nodes[i].id === pid) nodes[i] = Object.assign({}, nodes[i], { label: text }); } };
      edit(op);
      call("PUT", "/api/papers/" + encodeURIComponent(pid) + "/label", { label: text }).then(function (r) {
        var got = (r && (r.label || (r.paper && r.paper.label))) || text;
        settle(op);
        forData(function (g, d) { d.nodes = d.nodes.map(function (n) { return n.id === pid ? Object.assign({}, n, { label: got }) : n; }); });
        rebuildAll(); refetchSoon();
        say("The label is now " + quote(got) + ".");
      }, function (err) {
        settle(op); rebuildAll();
        say("Could not change the label: " + (err.status === 404 || err.status === 405 ? "this hub does not take labels yet" : errText(err)) + ".");
      });
    }
    // A graph's name, tags or lock on their way to the hub stay over what a list says meanwhile.
    function putGraph(g, body, undoLocal, done) {
      var k = Object.keys(body)[0], tok = ++tmpSeq;
      g.mp = g.mp || {}; g.mpTok = g.mpTok || {}; g.mp[k] = body[k]; g.mpTok[k] = tok;
      var over = function () { settled++; if (g.mpTok[k] === tok) { delete g.mp[k]; delete g.mpTok[k]; } };
      renderTabs(); renderSettings(true); refreshCard();
      call("PUT", "/api/graphs/" + encodeURIComponent(g.id), body).then(function () {
        over();
        if (done) done();
        soon({ list: true, log: true });
      }, function (err) {
        over();
        undoLocal(); renderTabs(); renderSettings(true); refreshCard();
        say("Could not change the graph: " + errText(err) + ".");
      });
    }
    function renameGraph(g, name) {
      name = String(name || "").replace(/\s+/g, " ").trim();
      if (!name || name === g.meta.name) return;
      var was = g.meta.name; g.meta.name = name;
      putGraph(g, { name: name }, function () { g.meta.name = was; }, function () { say("Renamed to " + quote(name) + "."); });
    }
    function setTags(g, tags) {
      var was = g.meta.tags || [];
      if (tags.join("\n") === was.join("\n")) return;
      g.meta.tags = tags;
      putGraph(g, { tags: tags }, function () { g.meta.tags = was; }, function () { g.stale = true; if (g === cur) loadGraph(g); say("Tags saved."); });
    }
    function setLocked(g, on) {
      var was = !!g.meta.locked; g.meta.locked = on;
      putGraph(g, { locked: on }, function () { g.meta.locked = was; }, function () { say(on ? "Locked: only admins can change " + quote(g.meta.name) + " now." : "Unlocked."); });
    }
    function createGraph(name, tags) {
      name = String(name || "").replace(/\s+/g, " ").trim();
      if (!name) { say("Give the graph a name."); return; }
      var tmp = { id: "tmp" + (++tmpSeq), tmp: true, meta: { name: name, tags: tags, locked: false, n: 0 }, data: null, _s: null };
      graphs.push(tmp); byId[tmp.id] = tmp;
      openPanel(null); renderTabs();
      call("POST", "/api/graphs", tags.length ? { name: name, tags: tags } : { name: name }).then(function (r) {
        var m = normMeta((r && (r.graph || r)) || {});
        settled++; drop(tmp);
        if (!m.id) { loadList(); return; }
        var g = byId[m.id];
        if (!g) { g = { id: m.id, meta: { name: name, tags: tags, locked: false, n: 0 }, data: null, _s: null, stale: true }; graphs.push(g); byId[g.id] = g; }
        Object.assign(g.meta, m);
        renderTabs(); showGraph(g); loadList(); loadLog();
        say("Made " + quote(name) + (tags.length ? "." : ": add papers from Graph settings."));
      }, function (err) {
        settled++; drop(tmp); renderTabs();
        say("Could not make the graph: " + errText(err) + ".");
      });
    }
    function drop(g) { var i = graphs.indexOf(g); if (i >= 0) graphs.splice(i, 1); if (byId[g.id] === g) delete byId[g.id]; return i; }
    function deleteGraph(g) {
      var name = g.meta.name, i = drop(g);
      delAsk = false; deleting[g.id] = true;          // a list answered meanwhile does not bring it back
      if (cur === g) { cur = null; showGraph(graphs[Math.max(0, i - 1)] || null); }
      openPanel(null); renderTabs();
      call("DELETE", "/api/graphs/" + encodeURIComponent(g.id)).then(function () {
        settled++; delete deleting[g.id];
        loadLog(); soon({ list: true });
        say("Deleted " + quote(name) + ".", undoMine("graph.delete", g.id));
      }, function (err) {
        settled++; delete deleting[g.id];
        graphs.splice(Math.min(i, graphs.length), 0, g); byId[g.id] = g; renderTabs();
        say("Could not delete the graph: " + errText(err) + ".");
      });
    }

    /* ---------- the edit log: Undo and History ---------- */
    function entryById(id) { for (var i = 0; i < LOG.entries.length; i++) if (same(LOG.entries[i].id, id)) return LOG.entries[i]; return null; }
    function isMine(e) { return isMe(e.user_id != null ? e.user_id : e.user && e.user.id); }
    function candidate(scope) {
      // the hub may say which op it would revert; else the newest not yet reverted, in scope
      var hint = LOG.hint;
      if (hint && Object.prototype.hasOwnProperty.call(hint, scope)) {
        var hv = hint[scope];
        if (hv == null) return null;
        return entryById(typeof hv === "object" ? hv.id : hv) || (typeof hv === "object" ? hv : null);
      }
      for (var i = 0; i < LOG.entries.length; i++) {
        var e = LOG.entries[i];
        if (e.reverted_by != null) continue;
        if (scope === "mine" && !isMine(e)) continue;
        return e;
      }
      return null;
    }
    function userName(e) {
      var u = LOG.users && e.user_id != null ? LOG.users[e.user_id] : null;
      return nameOf(e.user) || e.user_name || e.by_name || nameOf(e.by) || nameOf(u) || "";
    }
    function who(e) {
      var mine = isMine(e), nm = userName(e);
      if (e.actor === "agent") return "The agent for " + (mine ? "you" : nm || "someone");
      return mine ? "You" : nm || "Someone";
    }
    function linkIn(id) {                              // a link by id, in any graph drawn so far
      var out = null;
      graphs.forEach(function (g) { var l = !out && g._s && g._s.lid[String(id)]; if (l) out = l.e; });
      return out;
    }
    function targets(e) {
      var b = obj(e.before) || {}, a = obj(e.after) || {}, t = String(e.target == null ? "" : e.target);
      var o = { src: a.src || b.src, dst: a.dst || b.dst, lid: null, gid: a.graph_id || b.graph_id || null, pid: a.paper_id || b.paper_id || null, a: a, b: b };
      if (/^link\./.test(e.op || "")) {
        o.lid = a.id != null ? a.id : b.id != null ? b.id : t;
        if (o.src == null) { var l = linkIn(o.lid); if (l) { o.src = l.src; o.dst = l.dst; } }
      } else {
        var mg = t.match(/g_[a-z0-9]+/), mp = t.match(/p_[a-z0-9]+/);
        o.gid = o.gid || (mg && mg[0]) || (/^graph\./.test(e.op || "") ? a.id || b.id : null) || null;
        o.pid = o.pid || (mp && mp[0]) || null;
      }
      return o;
    }
    function graphName(t) { var g = t.gid && byId[t.gid]; return (g && g.meta.name) || t.a.name || t.b.name || "a graph"; }
    function what(e, inner) {
      if (e.revert_of != null && !inner) { var r = entryById(e.revert_of); return "undid: " + (r ? sentence(r, true) : "an edit"); }
      var t = targets(e), a = t.a, b = t.b;
      var pair = t.src != null && t.dst != null ? label(t.src) + " → " + label(t.dst) : "a link";
      switch (e.op) {
        case "link.add": return "added " + pair + (GNAME[a.grade] ? " (" + GNAME[a.grade] + ")" : "");
        case "link.remove": return "removed " + pair;
        case "link.grade": return "made " + pair + " " + (GNAME[a.grade] || "regraded") + (GNAME[b.grade] ? " (was " + GNAME[b.grade] + ")" : "");
        case "graph.create": return "made the graph " + quote(graphName(t));
        case "graph.rename": return "renamed " + quote(b.name || "a graph") + " to " + quote(a.name || graphName(t));
        case "graph.delete": return "deleted the graph " + quote(graphName(t));
        case "graph.add_paper": return "added " + (t.pid ? label(t.pid) : "a paper") + " to " + quote(graphName(t));
        case "graph.remove_paper": return "took " + (t.pid ? label(t.pid) : "a paper") + " out of " + quote(graphName(t));
        case "graph.set_tags": return "set the tags of " + quote(graphName(t)) + " to " + ((a.tags || a.rule_tags || []).join(", ") || "none");
        case "graph.lock": return (a.locked ? "locked " : "unlocked ") + quote(graphName(t));
        default: return e.text || e.summary || String(e.op || "changed something");
      }
    }
    function sentence(e, inner) { return who(e) + " " + what(e, inner); }
    function thing(e) {
      var t = targets(e);
      if (t.src != null && t.dst != null) return label(t.src) + " → " + label(t.dst);
      if (t.gid) return quote(graphName(t));
      return "that";
    }
    function loadLog(fresh) {
      if (LOG.busy && !fresh) { LOG.again = true; return LOG.busy; }
      var seq = LOG.seq = (LOG.seq || 0) + 1;
      var p = call("GET", "/api/graph-log?limit=100").then(function (r) {
        if (seq !== LOG.seq) return;                   // a newer load was asked for
        var list = Array.isArray(r) ? r : (r && (r.entries || r.log || r.items)) || [];
        LOG.entries = list.slice().sort(function (x, y) { return y.id - x.id; });
        LOG.hint = r && !Array.isArray(r) ? (r.undo || r.next || null) : null;
        LOG.papers = (r && r.papers) || null; LOG.users = (r && r.users) || null;
        LOG.loaded = true; LOG.stale = false; LOG.err = null;
      }, function (err) { if (seq === LOG.seq) LOG.err = errText(err); }).then(function () {
        if (LOG.busy === p) LOG.busy = null;
        renderUndo(); renderHist();
        if (LOG.again && !LOG.busy) { LOG.again = false; loadLog(); }
      });
      LOG.busy = p;
      return p;
    }
    function undo(scope) {
      var e = candidate(scope);
      if (!e) { say("Nothing to undo."); return; }
      if (LOG.undoing) return;
      var text = sentence(e);
      LOG.undoing = true; renderUndo();
      call("POST", "/api/graph-log/revert", { scope: scope, expect: e.id }).then(function () {
        say("Undone: " + text + ".");
      }, function (err) {
        var b = err.body || {};
        if (err.status === 409 && b.error === "moved") say("Not undone: someone edited since. The Undo now shows the newest edit.");
        else if (err.status === 409) say("Not undone: " + thing(e) + " was changed after that edit" + (b.message && b.message !== b.error ? " (" + String(b.message).replace(/\.$/, "") + ")" : "") + ".");
        else say("Could not undo: " + errText(err) + ".");
      }).then(function () {
        LOG.undoing = false; settled++;
        graphs.forEach(function (g) { g.stale = true; });
        loadList(); if (cur && !cur.tmp) loadGraph(cur); loadLog(true);
      });
    }
    function renderUndo() {
      var c = candidate("any");
      bUndo.title = c ? "Undo: " + sentence(c) + " · " + ago(c.at) : "Undo";
      if (undoEl.hidden) return;
      undoEl.textContent = "";
      undoEl.appendChild(h("h2", null, "Undo"));
      if (!LOG.loaded) { undoEl.appendChild(h("p", "pm-note", LOG.err ? "Could not load the changes: " + LOG.err + "." : "Loading…")); return; }
      [["mine", "My last edit"], ["any", "The last edit, by anyone"]].forEach(function (sc) {
        var e = candidate(sc[0]), box = h("div", "pm-undo");
        box.setAttribute("data-scope", sc[0]);
        box.appendChild(h("div", "pm-undo-h", sc[1]));
        if (e) {
          box.appendChild(h("div", "pm-undo-t", "Undo: " + sentence(e) + " · " + ago(e.at)));
          var b = btn("Undo", "pm-btn", function () { undo(sc[0]); });
          b.disabled = LOG.undoing || !EDIT;
          box.appendChild(b);
        } else box.appendChild(h("div", "pm-undo-t pm-muted", "Nothing to undo."));
        undoEl.appendChild(box);
      });
      undoEl.appendChild(h("p", "pm-note", "An undo is an edit too: it shows in the History and can be undone."));
    }
    function renderHist() {
      if (histEl.hidden) return;
      var st = histEl.scrollTop;
      histEl.textContent = "";
      histEl.appendChild(h("h2", null, "History"));
      if (!LOG.loaded) { histEl.appendChild(h("p", "pm-note", LOG.err ? "Could not load the changes: " + LOG.err + "." : "Loading…")); return; }
      if (!LOG.entries.length) { histEl.appendChild(h("p", "pm-note", "No changes yet.")); return; }
      var ul = h("ul", "pm-list pm-hist");
      LOG.entries.slice(0, 100).forEach(function (e) {
        var b = btn(null, "pm-hitem", function () { goTo(e); });
        b.appendChild(h("span", "pm-h-what", sentence(e)));
        var w = h("span", "pm-h-when", ago(e.at) + (e.reverted_by != null ? " · undone" : "")); w.title = when(e.at);
        b.appendChild(w);
        if (e.reverted_by != null) b.classList.add("undone");
        b.setAttribute("data-id", e.id);
        var li = h("li"); li.appendChild(b); ul.appendChild(li);
      });
      histEl.appendChild(ul);
      histEl.appendChild(h("p", "pm-note", "The last 100 changes, newest first."));
      histEl.scrollTop = st;
    }
    // From a change to what it changed: the link (or its papers), the paper, the graph.
    function goTo(e) {
      var t = targets(e);
      if (t.src != null && t.dst != null) {
        var inG = function (g) { return g && g._s && g._s.idx[t.src] != null && g._s.idx[t.dst] != null; };
        var g = inG(cur) ? cur : graphs.filter(inG)[0] || (cur && cur._s && (cur._s.idx[t.src] != null || cur._s.idx[t.dst] != null) ? cur : null);
        if (!g) { say("Those papers are not on a graph together now."); return; }
        want = { lid: t.lid, pid: g._s.idx[t.src] != null ? t.src : t.dst };
      } else if (t.gid) {
        var gg = byId[t.gid];
        if (!gg) { say("That graph is gone."); return; }
        want = { pid: t.pid };
        if (t.pid && !(gg._s && gg._s.idx[t.pid] != null)) gg.stale = true;    // it may have come in since
        if (gg !== cur) { showGraph(gg); return; }
        if (gg.stale) loadGraph(gg);
      } else return;
      if (g && g !== cur) showGraph(g); else applyWant();
    }
    function applyWant(final) {
      if (!want || !cur || !cur._s) return;
      var w = want, okL = w.lid != null && !!cur._s.lid[String(w.lid)], okP = w.pid != null && cur._s.idx[w.pid] != null;
      if (!okL && !okP && !final && (cur.busy || cur.stale)) return;   // the fresh answer may have it
      want = null;
      if (okL) selectLink(w.lid, true); else if (okP) select(w.pid, true);
    }

    /* ---------- the card: a paper, a link, or a link being made ---------- */
    function closeX(fn) {
      var x = h("button", "pm-x"); x.type = "button"; x.setAttribute("aria-label", "Close"); x.appendChild(svg(ICONS.close));
      x.addEventListener("click", fn || function () { select(null); });
      card.appendChild(x);
    }
    function item(id, num, meta) {
      var b = h("button", "pm-item"); b.type = "button";
      if (num != null) b.appendChild(h("span", "pm-num", num));
      var lb = h("span", "pm-lbl", label(id)); if (listened(id)) lb.classList.add("done");
      b.appendChild(lb);
      b.appendChild(h("span", "pm-meta", meta != null ? meta : yy(id)));
      b.addEventListener("click", function () { select(id, true); });
      var li = h("li"); li.appendChild(b); return li;
    }
    function section(title, ids) {
      if (!ids.length) return;
      card.appendChild(h("h2", null, title));
      var ul = h("ul", "pm-list");
      ids.slice().sort(function (a, b) { return (yearOf(a) || 0) - (yearOf(b) || 0); }).forEach(function (id) { ul.appendChild(item(id)); });
      card.appendChild(ul);
    }
    function refreshCard() { if (!card.hidden || draft || selLink != null || selId != null) renderCard(false); }
    function renderCard(force) {
      var a = document.activeElement;
      if (!force && a && card.contains(a) && (a.tagName === "INPUT" || editLabel != null)) return;   // someone is typing in it
      card.textContent = "";
      if (!cur || !cur._s) { card.hidden = true; return; }
      if (draft) draftCard(); else if (selLink != null && cur._s.lid[String(selLink)]) linkCard(); else if (selId != null && cur._s.idx[selId] != null) paperCard();
      else { card.hidden = true; return; }
      card.hidden = false;
    }
    function lockNote() {
      if (cur.meta.locked) card.appendChild(h("p", "pm-note", ADMIN ? "This graph is locked; as an admin you can still change it." : "This graph is locked: only admins change it."));
    }
    function paperCard() {
      var id = selId, s = cur._s, n = s.nodes[s.idx[id]], can = canEdit();
      card.setAttribute("data-kind", "paper");
      closeX();
      card.appendChild(h("h3", null, titleOf(id)));
      var bits = [], b = byline(id); if (b) bits.push(b);
      var dn = (cur.data && cur.data.descendants && cur.data.descendants[id]) || 0; if (dn) bits.push("leads to " + dn);
      if (bits.length) card.appendChild(h("p", "pm-sub", bits.join(" · ")));
      var mk = makers(id); if (mk.length) card.appendChild(h("p", "pm-sub pm-made", "Made by " + names(mk)));
      var lr = h("div", "pm-label");
      if (editLabel === id && can) {
        var inp = h("input", "pm-in"); inp.value = n.label; inp.maxLength = 40; inp.setAttribute("aria-label", "Short label on the map");
        inp.addEventListener("keydown", function (e) { if (e.key === "Enter") { e.preventDefault(); setLabel(id, inp.value); } });
        lr.appendChild(inp);
        lr.appendChild(btn("Save", "pm-btn", function () { setLabel(id, inp.value); }));
        lr.appendChild(btn("Cancel", "pm-btn", function () { editLabel = null; renderCard(true); }));
        card.appendChild(lr);
        setTimeout(function () { inp.focus(); inp.select(); }, 0);
      } else {
        lr.appendChild(h("span", "pm-muted", "On the map: "));
        lr.appendChild(h("span", "pm-lab", n.label));
        if (can) lr.appendChild(btn("Edit label", "pm-btn pm-small", function () { editLabel = id; renderCard(true); }));
        card.appendChild(lr);
      }
      var act = h("div", "pm-act");
      if (opts.onOpen && paper(id)) act.appendChild(btn(listened(id) ? "Open (listened)" : "Open", "pm-open", function () { opts.onOpen(id); }));
      if (can) {
        act.appendChild(btn("Link to…", "pm-btn pm-linkto", function () { startLinkMode(id); }));
        act.appendChild(btn("Take out of this graph", "pm-btn pm-rm", function () { removePaper(cur, id); }));
      }
      if (act.childNodes.length) card.appendChild(act);
      if (can && !phone()) card.appendChild(h("p", "pm-note", "Shift-click another paper to link the two."));
      lockNote();
      section("Builds on", n.par.map(function (j) { return s.nodes[j].id; }));
      section("Built on by", n.ch.map(function (j) { return s.nodes[j].id; }));
      var others = graphs.filter(function (g) { return g !== cur && g._s && g._s.idx[id] != null; });
      if (others.length) {
        var al = h("div", "pm-also", "Also in");
        others.forEach(function (g) { al.appendChild(btn(g.meta.name, "pm-alsob", function () { want = { pid: id }; showGraph(g); })); });
        card.appendChild(al);
      }
    }
    function linkBy(e) {
      var u = e.by || e.user || (e.created_by && typeof e.created_by === "object" ? e.created_by : null);
      var mine = u ? isMe(u) : isMe(e.created_by), nm = mine ? "you" : nameOf(u) || e.by_name || e.created_by_name || "";
      var s = e.origin === "agent" ? "added by the agent" + (nm ? " for " + nm : "") : nm ? "added by " + nm : "";
      var t = e.created_at ? ago(e.created_at) : "";
      return s ? cap(s) + (t ? " · " + t : "") : "";
    }
    function gradeHints(ul) { GRADES.forEach(function (g) { ul.appendChild(h("li", null, g[1] + ": " + g[2] + ".")); }); return ul; }
    function linkCard() {
      var s = cur._s, l = s.lid[String(selLink)], e = l.e, A = s.nodes[l.s].id, B = s.nodes[l.t].id, can = canEdit() && !e.pending;
      card.setAttribute("data-kind", "link");
      closeX();
      card.appendChild(h("h3", null, label(A) + " → " + label(B)));
      card.appendChild(h("p", "pm-dir", label(B) + " builds on " + label(A)));
      var by = linkBy(e); if (by) card.appendChild(h("p", "pm-sub pm-by", by));
      card.appendChild(h("h2", null, "Grade"));
      var seg = h("div", "pm-seg"); seg.setAttribute("role", "group"); seg.setAttribute("aria-label", "Grade");
      GRADES.forEach(function (g) {
        var b = btn(g[1], "pm-segb", function () { setGrade(e.id, g[0]); });
        b.setAttribute("aria-pressed", String(l.grade === g[0])); b.title = g[2]; b.disabled = !can; b.setAttribute("data-grade", g[0]);
        seg.appendChild(b);
      });
      card.appendChild(seg);
      var gl0 = GRADES.filter(function (g) { return g[0] === l.grade; })[0];
      if (gl0) card.appendChild(h("p", "pm-note", gl0[1] + ": " + gl0[2] + "."));
      if (can) { var act = h("div", "pm-act"); act.appendChild(btn("Remove link", "pm-btn pm-danger", function () { removeLink(e.id); })); card.appendChild(act); }
      else if (e.pending) card.appendChild(h("p", "pm-note", "Saving…"));
      lockNote();
      card.appendChild(h("h2", null, "Papers"));
      var ul = h("ul", "pm-list"); ul.appendChild(item(A, null, "earlier")); ul.appendChild(item(B, null, "builds on it")); card.appendChild(ul);
    }
    function draftCard() {
      var A = draft.src, B = draft.dst, ya = yearOf(A), yb = yearOf(B);
      card.setAttribute("data-kind", "draft");
      closeX(cancelDraft);
      card.appendChild(h("h3", null, "New link"));
      var p = h("p", "pm-dir"); p.appendChild(h("b", null, label(B))); p.appendChild(document.createTextNode(" builds on ")); p.appendChild(h("b", null, label(A)));
      card.appendChild(p);
      card.appendChild(h("p", "pm-sub", "The arrow goes from " + label(A) + (ya != null ? " (" + ya + ")" : "") + " to " + label(B) + (yb != null ? " (" + yb + ")" : "") + ", the paper built on it."));
      card.appendChild(btn("Swap: " + label(A) + " builds on " + label(B), "pm-btn pm-swap", function () { draft = { src: B, dst: A, anchor: draft.anchor }; renderCard(true); kick(); }));
      card.appendChild(h("h2", null, "How much does " + label(B) + " build on it?"));
      var seg = h("div", "pm-seg pm-seg-add"); seg.setAttribute("role", "group"); seg.setAttribute("aria-label", "Add the link as");
      GRADES.forEach(function (g) { var b = btn(g[1], "pm-segb", function () { addLink(A, B, g[0]); }); b.title = g[2]; b.setAttribute("data-grade", g[0]); seg.appendChild(b); });
      card.appendChild(seg);
      card.appendChild(gradeHints(h("ul", "pm-hints")));
      var act = h("div", "pm-act"); act.appendChild(btn("Cancel", "pm-btn", cancelDraft)); card.appendChild(act);
    }

    /* ---------- panels ---------- */
    function openPanel(key) {
      var open = key != null && PANELS[key].hidden;
      Object.keys(PANELS).forEach(function (k) {
        var o = open && k === key; PANELS[k].hidden = !o;
        if (PBTN[k]) PBTN[k].setAttribute("aria-pressed", String(o));
      });
      delAsk = false;
      if (open) {
        // on the phone a panel and the card do not share the screen
        if (phone() && (selId != null || selLink != null || draft || linkFrom != null)) { selId = null; selLink = null; draft = null; linkFrom = null; card.hidden = true; renderBanner(); kick(); }
        if (key === "start") renderStart();
        if (key === "undo" || key === "hist") { renderUndo(); renderHist(); loadLog(); }
        if (key === "set") renderSettings(true);
        if (key === "newg") renderNew();
      }
      renderTabs();
    }
    var FIRST = 12, allPath = false;
    function renderStart() {
      if (startEl.hidden) return;
      startEl.textContent = "";
      var d = cur && cur.data;
      startEl.appendChild(h("h2", null, "Start here"));
      if (!d) { startEl.appendChild(h("p", "pm-note", "Loading…")); return; }
      var s = cur._s, D = d.descendants || {}, inG = function (id) { return s && s.idx[id] != null; };
      var ol = h("ul", "pm-list");
      (d.roots || []).filter(inG).sort(function (a, b) { return (D[b] || 0) - (D[a] || 0); }).forEach(function (id) {
        var n = D[id] || 0; if (n) ol.appendChild(item(id, null, yy(id) + " · " + n + " after"));
      });
      if (!ol.childNodes.length) startEl.appendChild(h("p", "pm-note", "No paper leads anywhere yet: link a few."));
      startEl.appendChild(ol);
      var path = (d.path || []).filter(inG);
      startEl.appendChild(h("h2", null, "Listening order"));
      var ol2 = h("ul", "pm-list");
      (allPath ? path : path.slice(0, FIRST)).forEach(function (id, k) { ol2.appendChild(item(id, k + 1)); });
      startEl.appendChild(ol2);
      if (path.length > FIRST) startEl.appendChild(btn(allPath ? "Show fewer" : "Show all " + path.length, "pm-more", function () { allPath = !allPath; renderStart(); }));
    }
    // A labelled field; with an action, its button on the same line (Enter does the same).
    function field(into, text, value, cls, action, fn) {
      var lab = h("label", "pm-f"); lab.appendChild(h("span", null, text));
      var inp = h("input", "pm-in" + (cls ? " " + cls : "")); inp.value = value || ""; inp.autocomplete = "off"; inp.spellcheck = false;
      lab.appendChild(inp);
      if (action) {
        var row = h("div", "pm-frow"); row.appendChild(lab);
        row.appendChild(btn(action, "pm-btn", function () { fn(inp.value); }));
        onEnter(inp, function () { fn(inp.value); });
        into.appendChild(row);
      } else into.appendChild(lab);
      return inp;
    }
    function onEnter(inp, fn) { inp.addEventListener("keydown", function (e) { if (e.key === "Enter") { e.preventDefault(); fn(); } }); }
    function renderSettings(force) {
      if (setEl.hidden) return;
      var a = document.activeElement;
      if (!force && a && setDyn.contains(a) && a.tagName === "INPUT") return;
      setDyn.textContent = "";
      if (!cur) return;
      var g = cur, m = g.meta;
      setDyn.appendChild(h("h2", null, "This graph"));
      if (!canEdit()) {
        setDyn.appendChild(h("p", "pm-gname", m.name));
        if (m.tags && m.tags.length) setDyn.appendChild(h("p", "pm-note", "Tags: " + m.tags.join(", ")));
        setDyn.appendChild(h("p", "pm-note", m.locked ? "Locked: only admins change this graph." : "You can look but not change it here."));
        return;
      }
      field(setDyn, "Name", m.name, "pm-name", "Rename", function (v) { renameGraph(g, v); });
      var tg = field(setDyn, "Tags", (m.tags || []).join(", "), "pm-tags", "Save tags", function (v) { setTags(g, tagList(v)); });
      tg.placeholder = "e.g. robotics, agents";
      setDyn.appendChild(h("p", "pm-note", "Papers with any of these tags join the graph by themselves; add others by hand below."));
      if (ADMIN) {
        var row = h("label", "pm-row pm-lockrow"), cb = h("input"); cb.type = "checkbox"; cb.checked = !!m.locked;
        row.appendChild(h("span", null, "Locked: only admins change it")); row.appendChild(cb);
        cb.addEventListener("change", function () { setLocked(g, cb.checked); });
        setDyn.appendChild(row);
      }
      setDyn.appendChild(h("h2", null, "Add a paper"));
      var aq = h("input", "pm-in pm-addq"); aq.type = "search"; aq.placeholder = "Search the library"; aq.autocomplete = "off"; aq.spellcheck = false;
      aq.setAttribute("aria-label", "Search the library for a paper to add");
      var res = h("ul", "pm-list pm-addres");
      aq.addEventListener("input", function () { addResults(aq.value, res); });
      onEnter(aq, function () { var b = res.querySelector("button"); if (b) b.click(); });
      setDyn.appendChild(aq); setDyn.appendChild(res);
      if (ADMIN || same(m.created_by, ME.id)) {
        var dz = h("div", "pm-act pm-delrow");
        if (!delAsk) dz.appendChild(btn("Delete this graph", "pm-btn pm-danger", function () { delAsk = true; renderSettings(true); }));
        else {
          dz.appendChild(h("span", "pm-note", "Delete " + quote(m.name) + " for everyone? The links stay."));
          dz.appendChild(btn("Delete", "pm-btn pm-danger", function () { deleteGraph(g); }));
          dz.appendChild(btn("Keep", "pm-btn", function () { delAsk = false; renderSettings(true); }));
        }
        setDyn.appendChild(dz);
      }
    }
    function addResults(q, res) {
      res.textContent = "";
      q = String(q || "").trim().toLowerCase();
      if (!q || !cur || !cur._s) return;
      var out = [];
      papers.forEach(function (p, id) {
        if (cur._s.idx[id] != null || out.length >= 8) return;
        var hay = [p.title, p.label, (p.authors || []).join(" "), p.year].join(" ").toLowerCase();
        if (hay.indexOf(q) >= 0) out.push(id);
      });
      if (!out.length) { res.appendChild(h("li", "pm-note", "No paper in the library matches.")); return; }
      out.forEach(function (id) {
        var p = paper(id), b = h("button", "pm-item"); b.type = "button";
        b.appendChild(h("span", "pm-lbl", p.title || id)); b.appendChild(h("span", "pm-meta", p.year != null ? String(p.year) : ""));
        b.setAttribute("aria-label", "Add " + (p.title || id));
        b.addEventListener("click", function () { res.textContent = ""; addPaper(cur, id); });
        var li = h("li"); li.appendChild(b); res.appendChild(li);
      });
    }
    function renderNew() {
      newEl.textContent = "";
      newEl.appendChild(h("h2", null, "New graph"));
      var nm = field(newEl, "Name", "", "pm-newname"); nm.maxLength = 80;
      var tg = field(newEl, "Tags (optional)", "", "pm-newtags"); tg.placeholder = "e.g. robotics, agents";
      newEl.appendChild(h("p", "pm-note", "Papers with any of these tags join by themselves; add others by hand from Graph settings."));
      var go = function () { createGraph(nm.value, tagList(tg.value)); };
      onEnter(nm, go); onEnter(tg, go);
      var act = h("div", "pm-act"); act.appendChild(btn("Make graph", "pm-open pm-make", go)); act.appendChild(btn("Cancel", "pm-btn", function () { openPanel(null); }));
      newEl.appendChild(act);
      setTimeout(function () { nm.focus(); }, 0);
    }
    bStart.addEventListener("click", function () { openPanel("start"); });
    bUndo.addEventListener("click", function () { openPanel("undo"); });
    bHist.addEventListener("click", function () { openPanel("hist"); });
    bSet.addEventListener("click", function () { openPanel("set"); });
    bFit.addEventListener("click", function () { userMoved = false; fit(true); });
    if (bClose) bClose.addEventListener("click", function () { opts.onClose(); });

    var CONTROLS = [["Display"], ["arrows", "Arrows"], ["fade", "Text fade", 0, 1, 0.01], ["node", "Node size", 0.4, 2.5, 0.05], ["line", "Line thickness", 0.2, 3, 0.05],
      ["Forces"], ["center", "Center force", 0, 1, 0.01], ["repel", "Repel force", 0, 20, 0.1], ["link", "Link force", 0, 1, 0.01], ["dist", "Link distance", 20, 250, 1]];
    CONTROLS.forEach(function (c) {
      if (c.length === 1) { setFix.appendChild(h("h2", null, c[0])); return; }
      var row = h("label", "pm-row"), inp = h("input");
      row.appendChild(h("span", null, c[1]));
      if (c[0] === "arrows") { inp.type = "checkbox"; inp.checked = !!S.arrows; }
      else { inp.type = "range"; inp.min = c[2]; inp.max = c[3]; inp.step = c[4]; inp.value = S[c[0]]; }
      inp.addEventListener("input", function () {
        S[c[0]] = inp.type === "checkbox" ? inp.checked : parseFloat(inp.value); save(SKEY, S);
        // a force moved by hand runs the forces from here (the hub's positions return on the next change there)
        if (["center", "repel", "link", "dist", "node"].indexOf(c[0]) >= 0) reheat(0.5); else kick();
      });
      row.appendChild(inp); setFix.appendChild(row);
    });
    setFix.appendChild(btn("Reset", "pm-more", function () {
      Object.assign(S, DEFAULTS); save(SKEY, S);
      setFix.querySelectorAll("input").forEach(function (inp, i) { var key = CONTROLS.filter(function (c) { return c.length > 1; })[i][0]; if (inp.type === "checkbox") inp.checked = S[key]; else inp.value = S[key]; });
      reheat(0.5);
    }));

    /* ---------- search ---------- */
    function runQuery() {
      var q = qEl.value.trim().toLowerCase();
      if (!q || !cur || !cur._s) { qset = null; kick(); return; }
      qset = {};
      cur._s.nodes.forEach(function (n, i) { if ((titleOf(n.id) + " " + n.label + " " + authorsOf(n.id)).toLowerCase().indexOf(q) >= 0) qset[i] = true; });
      kick();
    }
    qEl.addEventListener("input", runQuery);
    qEl.addEventListener("keydown", function (e) {
      if (e.key === "Enter" && qset && cur && cur._s) { var i = Object.keys(qset)[0]; if (i != null) { var id = cur._s.nodes[+i].id; if (linkFrom != null) pick(id, false); else select(id, true); } qEl.blur(); }
      if (e.key === "Escape") { e.stopPropagation(); qEl.value = ""; runQuery(); qEl.blur(); }
    });
    function onKey(e) {
      if (!visible || e.key !== "Escape" || e.target === qEl) return;
      if (e.target && e.target.tagName === "INPUT" && root.contains(e.target)) {
        if (editLabel != null && card.contains(e.target)) { editLabel = null; renderCard(true); } else e.target.blur();
        e.stopPropagation(); return;
      }
      if (draft || linkFrom != null) { cancelDraft(); e.stopPropagation(); }
      else if (selId != null || selLink != null) { select(null); e.stopPropagation(); }
      else if (opts.onClose) { opts.onClose(); e.stopPropagation(); }
    }
    document.addEventListener("keydown", onKey, true);

    /* ---------- tabs: one per graph, then + New graph ---------- */
    function renderTabs() {
      var sl = tabsEl.scrollLeft;
      tabsEl.textContent = "";
      graphs.forEach(function (g) {
        var b = h("button", "pm-tab"); b.type = "button"; b.setAttribute("role", "tab"); b.setAttribute("aria-selected", String(g === cur));
        b.setAttribute("data-id", g.id);
        b.appendChild(document.createTextNode(g.meta.name || "Untitled"));
        if (g.meta.locked) { var lk = svg(ICONS.lock); lk.setAttribute("class", "pm-lock"); b.appendChild(lk); b.setAttribute("aria-label", g.meta.name + ", locked"); }
        var n = g._s ? g._s.nodes.length : g.meta.n;
        if (n != null) b.appendChild(h("span", "pm-n", String(n)));
        if (g.tmp) { b.disabled = true; b.classList.add("pm-tmp"); }
        b.addEventListener("click", function () { showGraph(g); });
        tabsEl.appendChild(b);
      });
      if (EDIT) {
        var nb = h("button", "pm-tab pm-tab-new", "+ New graph"); nb.type = "button";
        nb.setAttribute("aria-pressed", String(!newEl.hidden));
        nb.addEventListener("click", function () { openPanel("newg"); });
        tabsEl.appendChild(nb);
      }
      tabsEl.scrollLeft = sl;
    }
    function scrollTab() {
      var tb = null;
      Array.prototype.forEach.call(tabsEl.children, function (b) { if (cur && b.getAttribute("data-id") === String(cur.id)) tb = b; });
      if (!tb) return;
      var off = tb.offsetLeft - tabsEl.offsetLeft;
      if (off < tabsEl.scrollLeft || off + tb.offsetWidth > tabsEl.scrollLeft + tabsEl.clientWidth) tabsEl.scrollLeft = off - 4;
    }
    function showGraph(g) {
      if (!g) {
        cur = null; selId = null; selLink = null; draft = null; linkFrom = null; editLabel = null; qset = null;
        renderTabs(); renderBanner(); renderEmpty(); card.hidden = true; tip.hidden = true; ctxClear(); return;
      }
      if (cur === g) { applyWant(); return; }
      cur = g;
      if (!g.tmp) save(TKEY, g.id);
      hoverI = null; hoverL = null; selId = null; selLink = null; draft = null; linkFrom = null; editLabel = null; delAsk = false;
      card.hidden = true; tip.hidden = true; hl = 0; hlSet = null; hlFocus = null; hlLink = null; hlKey = null; anim = null; moves = null; allPath = false;
      renderBanner();
      if (g.data && (!g._s || g.dirty)) rebuild(g);
      userMoved = !!(g._s && g._s.fitted);
      if (!newEl.hidden) openPanel(null);
      renderTabs(); scrollTab();
      if (g.data) afterChange();
      else { renderStart(); renderSettings(true); renderEmpty(); ctxClear(); }
      if (!g.data || g.stale) loadGraph(g);
      applyWant();
      kick();
    }
    function ctxClear() { ctx.setTransform(1, 0, 0, 1, 0, 0); ctx.clearRect(0, 0, cv.width, cv.height); if (gl) { GLB.begin(); GLB.end(); } else if (g2) B2D.begin(); }
    function renderEmpty() {
      empty.textContent = "";
      var text = null, add = false, retry = null;
      if (!listLoaded) { if (listErr) { text = "The map could not be loaded: " + listErr + "."; retry = loadList; } else text = "Loading…"; }
      else if (!cur) text = EDIT ? "No graphs yet: make one with + New graph." : "No graphs yet.";
      else if (!cur.data) { if (cur.loadErr) { text = "This graph could not be loaded: " + cur.loadErr + "."; retry = function () { loadGraph(cur); }; } else text = cur.tmp ? "Making the graph…" : "Loading…"; }
      else if (!cur._s || !cur._s.nodes.length) { text = "No papers in this graph yet."; add = canEdit(); }
      if (text == null) { empty.hidden = true; legend.hidden = false; return; }
      empty.appendChild(h("p", null, text));
      if (add) empty.appendChild(btn("Add a paper", "pm-btn", function () { if (setEl.hidden) openPanel("set"); var q = setEl.querySelector(".pm-addq"); if (q) q.focus(); }));
      if (retry) empty.appendChild(btn("Try again", "pm-btn", retry));
      empty.hidden = false; legend.hidden = true;
    }

    /* ---------- loading ---------- */
    function loadList() {
      if (listBusy) { listAgain = true; return; }
      listBusy = true;
      var s0 = settled;
      call("GET", "/api/graphs").then(function (r) {
        if (settled !== s0) { listAgain = true; return; }
        var arr = Array.isArray(r) ? r : (r && r.graphs) || [], next = [], seen = {};
        arr.forEach(function (m0) {
          var m = normMeta(m0);
          if (m.id == null || seen[m.id] || deleting[m.id]) return;
          seen[m.id] = true;
          var g = byId[m.id] || { id: m.id, meta: { name: "Untitled", tags: [], locked: false }, data: null, _s: null, stale: true };
          Object.assign(g.meta, m, g.mp || {});
          next.push(g);
        });
        graphs.forEach(function (g) { if (g.tmp) next.push(g); });
        var gone = cur && !cur.tmp && !seen[cur.id] ? cur : null;
        graphs = next; byId = {}; graphs.forEach(function (g) { byId[g.id] = g; });
        listLoaded = true; listErr = null;
        if (gone) { cur = null; say(quote(gone.meta.name) + " was deleted."); }
        renderTabs();
        if (!cur) showGraph(byId[want && want.gid] || byId[opts.graph] || byId[load(TKEY)] || graphs[0] || null);
        else { renderSettings(); refreshCard(); }
        renderEmpty();
      }, function (err) { listErr = errText(err); renderEmpty(); }).then(function () {
        listBusy = false;
        if (listAgain) { listAgain = false; loadList(); }
      });
    }
    function loadGraph(g) {
      if (!g || g.tmp) return Promise.resolve();
      if (g.busy) { g.again = true; return g.busy; }
      g.loadErr = null;
      var seq = (g.seq || 0) + 1; g.seq = seq;
      var s0 = settled;
      g.busy = call("GET", "/api/graphs/" + encodeURIComponent(g.id)).then(function (r) {
        if (settled !== s0) { g.again = true; return; }
        g.data = normGraph(r); g.stale = false;
        if (r && r.graph) Object.assign(g.meta, normMeta(r.graph), g.mp || {});
        rebuild(g);
        if (g === cur) { afterChange(); applyWant(true); }
        else renderTabs();
      }, function (err) {
        g.loadErr = errText(err);
        if (err.status === 404) { g.stale = true; soon({ list: true }); }
        if (g === cur) renderEmpty();
      }).then(function () {
        g.busy = null;
        if (g.again) { g.again = false; loadGraph(g); }
        else if (g === cur) prefetch();
      });
      return g.busy;
    }
    // The other graphs, one at a time once the one shown is in: tabs switch at once, and a
    // paper's card knows which other graphs it is in.
    var prefetching = false;
    function prefetch() {
      if (prefetching) return;
      var g = graphs.filter(function (x) { return !x.data && !x.busy && !x.tmp && !x.loadErr; })[0];
      if (!g) return;
      prefetching = true;
      loadGraph(g).then(function () { prefetching = false; setTimeout(prefetch, 50); });
    }

    /* ---------- live: the page passes the hub's events; else a refetch on show() ---------- */
    var soonT = null, soonWhat = {};
    function soon(w) {
      Object.keys(w).forEach(function (k) { if (w[k]) soonWhat[k] = true; });
      if (soonT) return;
      soonT = setTimeout(function () {
        soonT = null; var x = soonWhat; soonWhat = {};
        if (!visible) { graphs.forEach(function (g) { g.stale = true; }); LOG.stale = true; return; }
        if (x.list) loadList();
        if (x.graph && cur) loadGraph(cur);
        if (x.log) loadLog();
      }, 250);
    }
    function onGraphEvent(d) {
      d = d || {};
      // one graph (graph_id), several (graphs: a link is in every graph that has both papers), or not said
      var gid = d.graph_id || (d.graph && d.graph.id) || (typeof d.id === "string" && /^g_/.test(d.id) ? d.id : null);
      var ids = Array.isArray(d.graphs) ? d.graphs.map(function (x) { return x && typeof x === "object" ? x.id : x; }) : gid ? [gid] : null;
      if (ids) ids.forEach(function (id) { if (byId[id]) byId[id].stale = true; }); else graphs.forEach(function (g) { g.stale = true; });
      soon({ list: true, graph: !ids || (cur && ids.indexOf(cur.id) >= 0), log: true });
    }
    // a new row in the edit log means a graph changed: the one shown is asked again, the others when shown
    function onLogEvent() { LOG.stale = true; graphs.forEach(function (g) { g.stale = true; }); soon({ log: true, graph: true }); }
    function onPaperEvent(d) { if (d && d.label != null) { graphs.forEach(function (g) { g.stale = true; }); soon({ graph: true }); } }
    if (typeof opts.subscribe === "function") {
      opts.subscribe("graph", onGraphEvent);
      opts.subscribe("log", onLogEvent);
      opts.subscribe("paper", onPaperEvent);
    }

    if (!initGL()) g2 = gv.getContext("2d");
    gv.addEventListener("webglcontextlost", function (e) { e.preventDefault(); });
    gv.addEventListener("webglcontextrestored", function () { initGL(); kick(); });
    readColors();
    var mq = window.matchMedia("(prefers-color-scheme: dark)");
    if (mq.addEventListener) mq.addEventListener("change", function () { readColors(); kick(); });
    if (window.ResizeObserver) new ResizeObserver(function () { resize(); }).observe(root); else window.addEventListener("resize", resize);
    resize();
    renderTabs(); renderEmpty();
    if (!phone()) openPanel("start");
    loadList(); loadLog();

    var lastSig = "";
    function relabel() { graphs.forEach(function (g) { if (g._s) g._s.nodes.forEach(function (n) { n.label = labelFrom(n.o, n.id); }); }); }
    var api = {
      // the papers changed (a tick, a new title, a new maker): redraw colours and the open lists
      changed: function () {
        if (!cur || !cur._s) return;
        var sig = cur._s.nodes.map(function (n) { var p = paper(n.id); return (listened(n.id) ? 1 : 0) + titleOf(n.id) + (p && p.made_by ? JSON.stringify(p.made_by) : ""); }).join("|");
        if (sig === lastSig) return;
        lastSig = sig; relabel(); renderStart(); refreshCard(); kick();
      },
      show: function () { visible = true; resize(); readColors(); api.refresh(); kick(); },
      hide: function () { visible = false; tip.hidden = true; hoverI = null; hoverL = null; release(); },
      // everything again from the hub (what show() does)
      refresh: function () { loadList(); if (cur && !cur.tmp) loadGraph(cur); graphs.forEach(function (g) { if (g !== cur) g.stale = true; }); loadLog(); },
      // show a paper (and the graph it is in, when given)
      select: function (paperId, graphId) { want = { pid: paperId, gid: graphId }; if (graphId && byId[graphId] && byId[graphId] !== cur) showGraph(byId[graphId]); else applyWant(); },
      debug: { S: S, cur: function () { return cur; }, graphs: function () { return graphs; }, show: function (id) { showGraph(byId[id]); }, select: select, selectLink: selectLink,
        hover: function (i) { hoverI = i; kick(); }, draw: function () { draw(); }, tick: function () { tick(cur._s); }, webgl: function () { return !!gl; },
        state: function () { return { sel: selId, link: selLink, from: linkFrom, draft: draft && { src: draft.src, dst: draft.dst }, pending: pending.length, moving: !!moves, running: running }; },
        // a paper's place on the page, and a link's middle (CSS px)
        pos: function (id) { var n = nodeOf(cur, id), v = cur._s.view; return n ? [n.x * v.k + v.x, n.y * v.k + v.y] : null; },
        mid: function (lid) { var l = cur._s.lid[String(lid)], N = cur._s.nodes, v = cur._s.view; if (!l) return null; return [(N[l.s].x + N[l.t].x) / 2 * v.k + v.x, (N[l.s].y + N[l.t].y) / 2 * v.k + v.y]; },
        view: function (k, x, y) { var v = cur._s.view; if (k != null) { v.k = k; v.x = x; v.y = y; anim = null; userMoved = true; draw(); } return { k: v.k, x: v.x, y: v.y }; },
        // the graph's colour at a point of the page (CSS px), read right after drawing
        pixel: function (x, y) {
          draw();
          var X = Math.round(x * DPR), Y = Math.round(y * DPR);
          if (gl) { var px = new Uint8Array(4); gl.readPixels(X, gv.height - 1 - Y, 1, 1, gl.RGBA, gl.UNSIGNED_BYTE, px); return [px[0], px[1], px[2]]; }
          return Array.prototype.slice.call(g2.getImageData(X, Y, 1, 1).data, 0, 3);
        } }
    };
    window.PaperMap.current = api;       // for the browser tests
    return api;
  }
  window.PaperMap = { mount: mount };
})();
