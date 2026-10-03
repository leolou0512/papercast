/* papercast-group: the paper map, for the group and editable. Leo's map (stacks/papercast/web/
   static/map.js: a force graph drawn like Obsidian's graph view, WebGL where there is a GPU and
   2D otherwise, hover lighting a paper's links, labels that fade in as you zoom, Start here and
   the listening order) with its graphs from the hub (SPEC.md section 8). One tab per graph.
   Anyone links two papers, regrades or removes a link, adds a paper to a graph or takes one
   out, makes, renames or retags a graph; a locked graph only an admin changes, and its maker or an
   admin deletes it (after a dialog; Undo brings it back). Undo and Redo say what they will do
   before they do, for "my last edit" or "the last edit" (Ctrl/Cmd+Z, Ctrl/Cmd+Shift+Z, Ctrl+Y);
   History lists the last 100 changes. A new link is aimed: an arrow from the first paper to the
   pointer, snapping to the paper under it. While an admin has links from uploads on "suggest
   only", an upload's links wait as suggestions: shown on request as dashed lines, each accepted
   (it becomes a link) or dismissed. Every edit shows at once and is set right by the hub's
   answer; a failure rolls back with a short message. Every edit names the revision of the graph
   it was made on: when someone else changed the graph since, the hub refuses it and the map
   brings itself up to date at once. The hub's live events (map.event(kind, data) from the page,
   or opts.subscribe) keep every open map current. The hub works the positions out (layout.py
   runs these same equations to rest), so the map opens at rest: the browser never warms the
   layout up, it only eases a node to where the hub puts it. An arrow goes from a paper to a paper built on it. Grey: not
   listened; green: heard (by whoever is looking: ticked, or a version finished); accent: a place to start. Mounted by app.js:
   window.PaperMap.mount(host, opts) -> {changed, show, hide, refresh, select, event, graph, refreshList}.
   With opts.column (the page's home: its graph list chooses the graph) there are no tabs and no
   search of its own: opts.head goes where the tabs were, the page is told of the list
   (opts.onList), the graph shown (opts.onShow) and each graph's answer (opts.onData), and
   "Not in any graph" (the list's `unfiled`) is one more graph, whose papers nobody adds or takes
   out; opts.live: the list and the graph shown are kept current while the map is hidden too.
   Who changes what is the hub's word (can_edit: the graph's papers, name, tags, deletion;
   can_link: links and labels, everyone's), else the old rule (a locked graph: admins). */
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
  function phone() { return window.matchMedia("(max-width: 720px)").matches; }
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
    gear: '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>',   // Feather's "settings" (MIT)
    close: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m6 6 12 12M18 6 6 18"/></svg>',
    undo: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M9 14 4 9l5-5"/><path d="M4 9h10.5a5.5 5.5 0 0 1 0 11H11"/></svg>',
    redo: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m15 14 5-5-5-5"/><path d="M20 9H9.5a5.5 5.5 0 0 0 0 11H13"/></svg>',
    clock: '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="8.5"/><path d="M12 7.5V12l3 2"/></svg>',
    lock: '<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="5" y="11" width="14" height="9" rx="2"/><path d="M8 11V8a4 4 0 0 1 8 0v3"/></svg>'
  };

  function mount(root, opts) {
    opts = opts || {};
    var API = opts.api || "", ME = opts.me || {}, ADMIN = ME.role === "admin", EDIT = opts.editable !== false, COL = !!opts.column;
    var papers = opts.papers || new Map();
    var S = Object.assign({}, DEFAULTS, load(SKEY) || {});
    // W, H and everything drawn are in the page's CSS px; under the page's zoom (theme.js's size, CSS
    // zoom) the screen's px are Z of them: DPR is device px per CSS px, as with the browser's zoom
    var C = {}, W = 0, H = 0, Z = 1, DPR = 1, FONT = "sans-serif";
    // graphs: {id, meta: {name, tags, locked, n, created_by}, data: the hub's answer, _s: what is drawn}
    var graphs = [], byId = {}, cur = null, listBusy = false, listAgain = false, listErr = null, listLoaded = false;
    var hoverI = null, hoverL = null, selId = null, selLink = null, linkFrom = null, draft = null, dragN = null, qset = null, want = null;
    var hl = 0, hlSet = null, hlFocus = null, hlLink = null, hlKey = null, running = false, anim = null, moves = null, userMoved = false, visible = true;
    // settled counts the edits the hub has answered: an answer to a GET sent before one of them is
    // out of date (it may still show a paper just taken out), so it is dropped and asked again
    var pending = [], settled = 0, tmpSeq = 0, editLabel = null, deleting = {};
    var LOG = { entries: [], hint: null, redoHint: null, papers: null, users: null, loaded: false, stale: true, busy: null, again: false, err: null, undoing: false };
    // aim: a link being aimed (from a paper to the pointer; `over` the paper it snaps to)
    var aim = null, shiftHeld = false;
    // links from uploads (the hub's setting) and the suggestions shown
    var SETS = { mode: null, open: 0, loaded: false }, showSugg = false, selSugg = null, hoverS = null;

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
    var icons = h("div", "pm-icons"), undoBox = h("div", "pm-undobox"), closeBox = h("div", "pm-closebox");
    function ib(icon, title, into) { var b = h("button", "pm-ib"); b.type = "button"; b.title = title; b.setAttribute("aria-label", title); b.appendChild(svg(ICONS[icon])); (into || icons).appendChild(b); return b; }
    var bUndo = ib("undo", "Undo", undoBox), bRedo = ib("redo", "Redo", undoBox);
    var bStart = ib("list", "Start here"), bHist = ib("clock", "History"), bFit = ib("fit", "Fit to screen"), bSet = ib("gear", "Graph settings");
    var bClose = opts.onClose ? ib("close", "Close the map", closeBox) : null;
    if (!EDIT) undoBox.hidden = true;
    if (opts.title) head.appendChild(h("span", "pm-title", opts.title));
    if (opts.graphs !== false && !COL) head.appendChild(tabsEl);
    if (opts.head) head.appendChild(opts.head);
    if (COL) root.classList.add("pm-col");
    // under the search: Show suggestions, and who else changed this graph in the last minute
    var subRow = h("div", "pm-sub"), bSugg = h("button", "pm-btn pm-small pm-suggb"); bSugg.type = "button"; bSugg.hidden = true;
    bSugg.setAttribute("aria-pressed", "false");
    var live = h("div", "pm-live"); live.hidden = true; live.setAttribute("aria-live", "polite");
    subRow.appendChild(bSugg); subRow.appendChild(live);
    [head, undoBox, icons, closeBox].concat(COL ? [] : [qEl]).concat([subRow]).forEach(function (e) { top.appendChild(e); });
    var PANELS = {}, PBTN = { start: bStart, undo: bUndo, redo: bRedo, hist: bHist, set: bSet };
    function panel(key, label) { var e = h("div", "pm-panel"); e.hidden = true; e.setAttribute("data-panel", key); e.setAttribute("role", "region"); e.setAttribute("aria-label", label); PANELS[key] = e; return e; }
    var startEl = panel("start", "Start here"), undoEl = panel("undo", "Undo"), redoEl = panel("redo", "Redo"), histEl = panel("hist", "History"), setEl = panel("set", "Graph settings"), newEl = panel("newg", "New graph");
    var setDyn = h("div", "pm-setdyn"), setSite = h("div", "pm-setsite"), setFix = h("div");
    setEl.appendChild(setDyn); setEl.appendChild(setSite); setEl.appendChild(setFix);
    Object.keys(PBTN).forEach(function (k) { PBTN[k].setAttribute("aria-pressed", "false"); });
    var card = h("div", "pm-card"); card.hidden = true;
    var tip = h("div", "pm-tip"); tip.hidden = true;
    var aimTip = h("div", "pm-aim"); aimTip.hidden = true;           // what the link being aimed would say
    var legend = h("div", "pm-legend");
    function dot(cls, text) { var s = h("span"); s.appendChild(h("i", cls)); s.appendChild(document.createTextNode(text)); legend.appendChild(s); }
    dot("pm-d-done", "heard");
    dot("pm-d-start", "start here");
    var banner = h("div", "pm-banner"); banner.hidden = true;
    var empty = h("div", "pm-empty"); empty.hidden = true;
    var msg = h("div", "pm-msg"); msg.hidden = true; msg.setAttribute("role", "status"); msg.setAttribute("aria-live", "polite");
    // the dialog that asks before a graph is deleted (a real one: modal, Escape and a click outside cancel)
    var dlg = h("dialog", "pm-dialog"), dlgIn = h("div", "pm-dlg-in"), dlgH = h("h2", "pm-dlg-h"), dlgP = h("p", "pm-dlg-p");
    var dlgAct = h("div", "pm-act pm-dlg-act");
    dlgH.id = "pm-dlg-h-" + Math.random().toString(36).slice(2, 8); dlgP.id = dlgH.id.replace("-h-", "-p-");
    dlg.setAttribute("aria-labelledby", dlgH.id); dlg.setAttribute("aria-describedby", dlgP.id);
    [dlgH, dlgP, dlgAct].forEach(function (e) { dlgIn.appendChild(e); }); dlg.appendChild(dlgIn);
    [gv, cv, top, startEl, undoEl, redoEl, histEl, setEl, newEl, card, tip, aimTip, legend, banner, empty, msg, dlg].forEach(function (e) { root.appendChild(e); });
    function btn(text, cls, fn) { var b = h("button", cls || "pm-btn", text); b.type = "button"; if (fn) b.addEventListener("click", fn); return b; }

    function readColors() {
      var cs = getComputedStyle(root);
      ["bg", "text", "muted", "node", "line", "hi", "start", "done", "warn"].forEach(function (k) { C[k] = cs.getPropertyValue("--pm-" + k).trim(); });
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
    // heard (green): ticked as listened, or a version finished (the hub's `heard`); the page's
    // copy of the paper is the newest word, else the graph's answer
    function listened(id) { var p = paper(id); if (p) return !!(p.listened || p.heard); var o = info(id); return !!(o.listened || o.heard); }
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
        created_by: m.created_by && typeof m.created_by === "object" ? m.created_by.id : m.created_by,
        can_delete: m.can_delete == null ? null : !!m.can_delete, can_edit: m.can_edit == null ? null : !!m.can_edit,
        can_link: m.can_link == null ? null : !!m.can_link, rev: m.rev, changed: m.changed, pseudo: m.pseudo ? true : null };
      Object.keys(o).forEach(function (k) { if (o[k] == null) delete o[k]; });
      if (m.tags == null && m.rule_tags == null) delete o.tags;
      return o;
    }
    function normGraph(r) {
      r = r || {};
      var links = (r.links || r.edges || []).map(function (e) { return Array.isArray(e) ? { id: e[0] + ">" + e[1], src: e[0], dst: e[1], grade: e[2] } : e; });
      return { nodes: (r.nodes || []).filter(function (n) { return n && n.id != null; }), links: links, roots: r.roots || [], start: r.start || [],
        path: r.path || [], descendants: r.descendants || {}, suggestions: Array.isArray(r.suggestions) ? r.suggestions : [] };
    }
    function viewData(g) {
      var d = g.data || { nodes: [], links: [] }, nodes = d.nodes.slice(), links = d.links.slice(), sugg = (d.suggestions || []).slice();
      pending.forEach(function (op) { op.apply(g, nodes, links, sugg); });
      return { nodes: nodes, links: links, sugg: sugg };
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
      // suggestions: drawn (on request) and picked like links, but no force pulls along them
      var sugg = [], sid = {};
      v.sugg.forEach(function (e) {
        var a = idx[e.src], b = idx[e.dst];
        if (a == null || b == null || a === b || sid[String(e.id)]) return;
        var x = { id: e.id, s: a, t: b, grade: GNAME[e.grade] ? e.grade : "w", e: e, sugg: true };
        sugg.push(x); sid[String(e.id)] = x;
      });
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
      g._s = { nodes: nodes, links: links, idx: idx, lid: lid, sugg: sugg, sid: sid, alpha: old ? old.alpha : 0, target: old ? old.target : 0, view: view, fitted: old ? old.fitted : false };
      g.dirty = false;
      if (g !== cur) { mv.forEach(function (m) { m.n.x = m.x1; m.n.y = m.y1; }); return; }
      moves = mv.length ? { t0: performance.now(), dur: 650, list: mv.map(function (m) { return { n: m.n, x0: m.n.x, y0: m.n.y, x1: m.x1, y1: m.y1 }; }) } : null;
      if (dragN) { dragN = nodeOf(g, dragN.id); }
      hoverI = null; hoverL = null; hlKey = null;
      if (selId != null && idx[selId] == null) { selId = null; editLabel = null; }
      if (selLink != null && !lid[String(selLink)]) selLink = null;
      if (selSugg != null && !sid[String(selSugg)]) selSugg = null;
      hoverS = null;
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
      renderTabs(); renderStart(); refreshCard(); renderSettings(); renderEmpty(); renderSuggBtn(); runQuery(); kick();
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
      Z = root.currentCSSZoom || 1; DPR = (window.devicePixelRatio || 1) * Z; W = r.width / Z; H = r.height / Z;
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
      // a dashed line: its dashes as short lines
      dash: function (x0, y0, x1, y1, col, a, w, on, off) {
        var dx = x1 - x0, dy = y1 - y0, d = Math.sqrt(dx * dx + dy * dy) || 1, ux = dx / d, uy = dy / d;
        for (var t = 0; t < d; t += on + off) { var t1 = Math.min(d, t + on); GLB.line(x0 + ux * t, y0 + uy * t, x0 + ux * t1, y0 + uy * t1, col, a, w); }
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
      dash: function (x0, y0, x1, y1, col, a, w, on, off) { st2(col, a, w); g2.setLineDash([on, off]); g2.beginPath(); g2.moveTo(x0, y0); g2.lineTo(x1, y1); g2.stroke(); g2.setLineDash([]); },
      tri: function (x0, y0, x1, y1, x2, y2, col, a) { st2(col, a); g2.beginPath(); g2.moveTo(x0, y0); g2.lineTo(x1, y1); g2.lineTo(x2, y2); g2.closePath(); g2.fill(); },
      disc: function (x, y, r, col, a) { st2(col, a); g2.beginPath(); g2.arc(x, y, r, 0, 6.2832); g2.fill(); },
      ring: function (x, y, R, hw, col, a) { st2(col, a, hw * 2); g2.beginPath(); g2.arc(x, y, R, 0, 6.2832); g2.stroke(); },
      end: function () {}
    };
    var GA = { e: 0.95, s: 0.7, w: 0.42 }, KINDS = ["w", "s", "e", "lit"], AT_T = [0.5, 0.38, 0.62, 0.28, 0.72];
    function edge(B, a, b, col, al, w, arrow, awd, dash) {
      var dx = b.sx - a.sx, dy = b.sy - a.sy, d = Math.sqrt(dx * dx + dy * dy) || 1, ux = dx / d, uy = dy / d;
      if (d <= a.sr + b.sr + DPR) return;
      var ex = b.sx - ux * (b.sr + DPR), ey = b.sy - uy * (b.sr + DPR);
      var x1 = arrow ? ex - ux * awd * 1.2 : ex, y1 = arrow ? ey - uy * awd * 1.2 : ey;
      if (dash) B.dash(a.sx + ux * a.sr, a.sy + uy * a.sr, x1, y1, col, al, w, 5 * DPR, 4 * DPR);
      else B.line(a.sx + ux * a.sr, a.sy + uy * a.sr, x1, y1, col, al, w);
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
      // the suggestions, when shown: dashed, in the muted colour (the one lit in the text colour)
      if (showSugg) {
        var SL = s.sugg, litS = hoverS || (selSugg != null ? s.sid[String(selSugg)] : null);
        for (i = 0; i < SL.length; i++) {
          var x = SL[i], on2 = x === litS;
          edge(B, N[x.s], N[x.t], on2 ? C.hi : C.muted, on2 ? 1 : GA[x.grade] * (set ? dim : 1), on2 ? lwd * 1.6 : lwd, arrow, awd, true);
        }
      }
      // the link being made: in the accent (a muted warning colour when it points back in time),
      // with its arrow whatever the settings say
      var bw = Math.max(lwd * 2, 1.5 * DPR), baw = Math.max(awd, 3 * DPR);
      if (draft && s.idx[draft.src] != null && s.idx[draft.dst] != null) edge(B, N[s.idx[draft.src]], N[s.idx[draft.dst]], backwards(draft.src, draft.dst) ? C.warn : C.start, 1, bw, true, baw);
      // the link being aimed: from its first paper to the pointer, or to the paper it snaps to
      var AT = aim && aim.p && s.idx[aim.from] != null ? tipNow() : null, acol = aim && aimBad() ? C.warn : C.start;
      if (AT) edge(B, N[s.idx[aim.from]], { sx: AT.x * DPR, sy: AT.y * DPR, sr: AT.r * DPR }, acol, 1, bw, true, baw);
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
      else if (AT) { n = N[s.idx[aim.from]]; B.ring(n.sx, n.sy, n.sr + 3 * DPR, 1.2 * DPR, C.start, 1); }
      if (AT) {
        // the paper snapped to comes in with a ring; the one left goes out with it
        var q = aimQ();
        if (aim.over != null && s.idx[aim.over] != null) { n = N[s.idx[aim.over]]; B.ring(n.sx, n.sy, n.sr + 3 * DPR, 1.2 * DPR, acol, q); }
        if (aim.prev != null && aim.prev !== aim.over && q < 1 && s.idx[aim.prev] != null) { n = N[s.idx[aim.prev]]; B.ring(n.sx, n.sy, n.sr + 3 * DPR, 1.2 * DPR, C.start, 1 - q); }
      }
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
        if (aim && aim.p) { if (n.id === aim.from) la = 1; else if (n.id === aim.over) la = Math.max(la, aimQ()); }
        if (la < 0.03) continue;
        var px = n.x * k + v.x, py = (n.y + n.r) * k + v.y + 4;
        if (px < -200 || px > W + 200 || py < -40 || py > H + 40) continue;
        ctx.globalAlpha = Math.round(la * 10) / 10;
        ctx.fillText(n.label, px, py);
      }
      // each suggestion's grade by its middle, where it is long enough on the screen
      if (showSugg && s.sugg.length) {
        ctx.font = "11px " + FONT; ctx.textBaseline = "middle";
        for (i = 0; i < s.sugg.length; i++) {
          var x = s.sugg[i], a = N[x.s], b = N[x.t];
          var ax = a.x * k + v.x, ay = a.y * k + v.y, bx = b.x * k + v.x, by = b.y * k + v.y;
          if (Math.hypot(bx - ax, by - ay) < 90) continue;
          // along the line, where no paper (or its label) is: the middle, else a little either side
          var word = GNAME[x.grade], tw2 = ctx.measureText(word).width, mx = 0, my = 0, best = -1;
          for (var c = 0; c < AT_T.length; c++) {
            var cx = ax + (bx - ax) * AT_T[c], cy = ay + (by - ay) * AT_T[c], near = Infinity;
            for (var m = 0; m < N.length; m++) {
              var nx = N[m].x * k + v.x, ny = N[m].y * k + v.y, rr = radius(N[m]) * k;
              near = Math.min(near, Math.hypot(nx - cx, ny - cy) - rr, Math.hypot(nx - cx, ny + rr + 10 - cy) - 8);
            }
            if (near > best) { best = near; mx = cx; my = cy; }
            if (near > 24) break;
          }
          if (mx < -60 || mx > W + 60 || my < -20 || my > H + 20) continue;
          ctx.globalAlpha = set && !(set[x.s] && set[x.t]) ? 0.35 : 0.95;
          ctx.fillStyle = C.bg; ctx.fillRect(mx - tw2 / 2 - 3, my - 7, tw2 + 6, 14);
          ctx.fillStyle = C.muted; ctx.fillText(word, mx, my);
        }
        ctx.fillStyle = C.text; ctx.textBaseline = "top";
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
      if ((aim && aim.p) || linkFrom != null) return null;         // every paper stays a clear target
      var i = dragN ? dragN.i : hoverI;
      var L = i == null ? (hoverL || hoverS || (selLink != null ? s.lid[String(selLink)] : null) || (showSugg && selSugg != null ? s.sid[String(selSugg)] : null)) : null;
      if (L) return { key: "l" + L.s + ":" + L.t, set: function () { return pairSet(L.s, L.t); }, node: null, link: L };
      if (i == null && selId != null && s.idx[selId] != null) i = s.idx[selId];
      return i != null ? { key: "n" + i, set: function () { return neighbours(s, i); }, node: i, link: null } : null;
    }
    function frame(t) {
      if (!visible || !cur || !cur._s) { running = false; return; }
      var s = cur._s, busy = false;
      if (s.alpha > ALPHA_MIN || s.target > 0) { tick(s); busy = true; }
      if (moves) {
        var mp = Math.min(1, Math.max(0, (t - moves.t0) / moves.dur)), me = ease(mp);
        moves.list.forEach(function (m) { if (m.n === dragN) return; m.n.x = mp >= 1 ? m.x1 : m.x0 + (m.x1 - m.x0) * me; m.n.y = mp >= 1 ? m.y1 : m.y0 + (m.y1 - m.y0) * me; });
        if (mp >= 1) moves = null; else busy = true;
      }
      if (aim && aim.p && aimQ() < 1) busy = true;
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
    // where the bar over the map ends (CSS px from the top)
    function chromeBottom() { var r = top.getBoundingClientRect(), o = root.getBoundingClientRect(); return r.height ? (r.bottom - o.top) / Z : 0; }
    function fit(smooth) {
      if (!W || !cur || !cur._s || !cur._s.nodes.length) return;
      var N = cur._s.nodes, x0 = Infinity, x1 = -Infinity, y0 = Infinity, y1 = -Infinity;
      N.forEach(function (n) { var r = radius(n); x0 = Math.min(x0, n.x - r); x1 = Math.max(x1, n.x + r); y0 = Math.min(y0, n.y - r); y1 = Math.max(y1, n.y + r + 18); });
      var tp = Math.max(phone() ? 124 : 88, chromeBottom() + 16), bt = phone() ? 24 : 40, sl = 24, sr = !phone() && panelOpen() ? 312 : 24;
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
    function local(e) { var r = cv.getBoundingClientRect(); return { x: (e.clientX - r.left) / Z, y: (e.clientY - r.top) / Z }; }
    function onScreen(x, y) { var r = cv.getBoundingClientRect(); return [r.left + x * Z, r.top + y * Z]; }       // local()'s inverse
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
    function hitLink(p) { var a = hitAlong(p, cur._s.links), b = showSugg ? hitAlong(p, cur._s.sugg) : null; return b && (!a || b.d < a.d) ? null : a && a.l; }
    function hitSugg(p) { var a = hitAlong(p, cur._s.links), b = showSugg ? hitAlong(p, cur._s.sugg) : null; return b && (!a || b.d < a.d) ? b.l : null; }
    function hitAlong(p, L) {
      var s = cur._s, v = s.view, k = v.k, N = s.nodes, tol = phone() ? 14 : 6, best = null, bd = tol;
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
      return best ? { l: best, d: bd } : null;
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

    /* ---------- aiming a new link: an arrow from its first paper to the pointer ---------- */
    // From "Link to…" (or a picked paper while Shift is held) the arrow follows the mouse, or the
    // finger while it drags; over a paper it snaps to that paper's edge, rings it and shows its
    // label, and a line by the pointer says what the link would mean. The arrow is the link:
    // first → second means the second builds on the first; pointing back in time (the hub
    // refuses those) it turns the warning colour. Each pointer event asks for one frame; the tip
    // eases on and off a paper in AIM_MS.
    var AIM_MS = 140, lastP = null, aimKey = null, aw = 0, ah = 0;
    function aimFrom() {
      if (!cur || !cur._s || draft || !canLink()) return null;
      if (linkFrom != null) return cur._s.idx[linkFrom] != null ? linkFrom : null;
      if (shiftHeld && selId != null && cur._s.idx[selId] != null) return selId;
      return null;
    }
    function aimAt(p, touch) {
      var from = aimFrom();
      if (from == null) { clearAim(); return; }
      lastP = p;
      if (!aim || aim.from !== from) aim = { from: from, p: null, over: null, prev: null, e0: null, t0: 0, touch: false };
      var n = p ? hit(p) : null, over = n && n.id !== from ? n.id : null;
      if (p && aim.p && over !== aim.over) { aim.e0 = tipNow(); aim.t0 = performance.now(); aim.prev = aim.over; }
      else if (!aim.p) { aim.e0 = null; aim.prev = null; }
      aim.over = over; aim.p = p; aim.touch = !!touch;
      if (hoverI != null || hoverL) { hoverI = null; hoverL = null; }
      tip.hidden = true;
      renderAim(); kick();
    }
    function aimHere() { if (aimFrom() != null) aimAt(lastP, aim && aim.touch); else clearAim(); }
    function clearAim() { if (aim) { aim = null; aimTip.hidden = true; aimKey = null; kick(); } }
    function aimQ() { return !aim || !aim.e0 ? 1 : ease(Math.min(1, (performance.now() - aim.t0) / AIM_MS)); }
    // where the tip is going: the snapped paper (its disc; the arrow stops at its edge), else the pointer
    function aimTarget() {
      var s = cur._s, v = s.view, n = aim.over != null && s.idx[aim.over] != null ? s.nodes[s.idx[aim.over]] : null;
      return n ? { x: n.x * v.k + v.x, y: n.y * v.k + v.y, r: radius(n) * v.k } : { x: aim.p.x, y: aim.p.y, r: 0 };
    }
    function tipNow() {
      var T = aimTarget(), q = aimQ(), E = aim.e0;
      return E && q < 1 ? { x: E.x + (T.x - E.x) * q, y: E.y + (T.y - E.y) * q, r: E.r + (T.r - E.r) * q } : T;
    }
    function backwards(src, dst) { var ya = yearOf(src), yb = yearOf(dst); return ya != null && yb != null && ya > yb; }
    function aimBad() { return !!aim && aim.over != null && backwards(aim.from, aim.over); }
    function renderAim() {
      if (!aim || !aim.p || aim.over == null) { aimTip.hidden = true; aimKey = null; return; }
      var o = aim.over, f = aim.from, old = linkBetween(f, o), bad = !old && aimBad();
      var text = old ? "Linked already: " + label(cur._s.nodes[old.t].id) + " builds on " + label(cur._s.nodes[old.s].id)
        : bad ? label(o) + " is older: the arrow would go the other way" : label(o) + " builds on " + label(f);
      var key = text + (bad ? "!" : "");
      if (key !== aimKey || aimTip.hidden) {
        aimTip.textContent = text; aimTip.classList.toggle("warn", bad);
        aimTip.hidden = false; aimKey = key; aw = aimTip.offsetWidth; ah = aimTip.offsetHeight;
      }
      // over the paper it snapped to (its label, under it, stays in sight), clear of a finger;
      // under the label when there is no room above
      var T = aimTarget(), x = T.x - aw / 2, y = T.y - T.r - ah - (aim.touch ? 44 : 10);
      if (y < chromeBottom() + 4) y = T.y + T.r + 26;
      aimTip.style.transform = "translate(" + Math.round(Math.max(8, Math.min(W - aw - 8, x))) + "px," + Math.round(Math.max(8, Math.min(H - ah - 8, y))) + "px)";
    }

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
        var n = hit(p);
        // linking: a finger anywhere, or the mouse on a paper, aims (a drag to the paper, or a tap on it)
        if (linkFrom != null && aimFrom() != null && (e.pointerType !== "mouse" || n)) { mode = "aim"; dragN = null; aimAt(p, e.pointerType !== "mouse"); }
        else { mode = n ? "node" : "pan"; dragN = n; }
      } else if (pcount() === 2) {
        release();
        if (aim && aim.touch) { aim.p = null; lastP = null; aimTip.hidden = true; }
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
          lastP = p;
          if (e.shiftKey !== shiftHeld && !typing(document.activeElement)) shiftHeld = e.shiftKey;
          if (aimFrom() != null) { cv.classList.toggle("over", !!hit(p)); aimAt(p, false); return; }
          if (aim) clearAim();
          var n = hit(p), i = n ? n.i : null, l = n ? null : hitLink(p), sg = n || l ? null : hitSugg(p);
          cv.classList.toggle("over", !!(n || l || sg));
          if (i !== hoverI || l !== hoverL || sg !== hoverS) { hoverI = i; hoverL = l; hoverS = sg; kick(); }
          if (n) showTip("n" + n.id, titleOf(n.id), byline(n.id), p);
          else if (l) showTip("l" + l.id, linkWords(l), cap(GNAME[l.grade]) + " link", p);
          else if (sg) showTip("s" + sg.id, linkWords(sg), "Suggested · " + GNAME[sg.grade], p);
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
      if (mode === "aim") {
        if (!moved && Math.hypot(p.x - down.x, p.y - down.y) > 4) moved = true;
        aimAt(p, e.pointerType !== "mouse");
        return;
      }
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
      if (mode === "aim") {
        var over = aim && aim.over;
        if (over != null) pick(over, false);                         // the grade choice
        else if (!moved && !hit(at)) cancelDraft();                 // a tap on nothing: no link
        else if (aim && aim.touch) { aim.p = null; lastP = null; aimTip.hidden = true; kick(); }   // let go on nothing: still linking
      } else if (mode === "node" && dragN) {
        var n = dragN;
        release();
        if (!moved) pick(n.id, shift);
        if (e.pointerType !== "mouse") hoverI = null;
      } else if (mode === "pan" && !moved && cur && cur._s) {
        var l = hitLink(at), sg = l ? null : hitSugg(at);
        if (linkFrom != null) cancelDraft();                        // a click on nothing while linking: no link
        else if (l) selectLink(l.id, false);
        else if (sg) selectSugg(sg.id);
        else select(null);
      }
      if (pcount() === 1) { var q = ptrs[Object.keys(ptrs)[0]]; down = { x: q.x, y: q.y, lx: q.x, ly: q.y }; mode = "pan"; moved = true; }
      else if (!pcount()) { mode = null; down = null; }
      kick();
    }
    cv.addEventListener("pointerup", up);
    cv.addEventListener("pointercancel", up);
    cv.addEventListener("pointerleave", function (e) {
      if (e.pointerType === "mouse" && !ptrs[e.pointerId]) {
        tip.hidden = true; lastP = null;
        if (aim) aimAt(null, false);
        if (hoverI != null || hoverL || hoverS) { hoverI = null; hoverL = null; hoverS = null; kick(); }
      }
    });
    cv.addEventListener("wheel", function (e) { e.preventDefault(); if (!cur || !cur._s) return; tip.hidden = true; zoomAt(Math.exp(-e.deltaY * (e.ctrlKey ? 0.01 : 0.0015)), local(e).x, local(e).y); }, { passive: false });

    /* ---------- picking: a paper, a link, or the second paper of a new link ---------- */
    // the graph's papers, name, tags, deletion (the hub says: its maker's and admins' when someone made it)
    function canEdit(g) { g = g || cur; return EDIT && !!g && !g.tmp && (g.meta.can_edit != null ? !!g.meta.can_edit : (!g.meta.locked || ADMIN)); }
    // links and labels: everyone's, but for a locked graph (the hub says; else the same rule here)
    function canLink(g) { g = g || cur; return EDIT && !!g && !g.tmp && (g.meta.can_link != null ? !!g.meta.can_link : (!g.meta.locked || ADMIN)); }
    function pick(id, shift) {
      if (linkFrom != null) { if (id !== linkFrom) startDraft(linkFrom, id); return; }
      var from = draft ? draft.anchor : selId;
      if (shift && canLink() && from != null && from !== id) { startDraft(from, id); return; }
      select(id, false);
    }
    function closePanelsOnPhone() { if (phone()) openPanel(null); }
    function select(id, focus) {
      selId = id; selLink = null; selSugg = null; draft = null; linkFrom = null; editLabel = null;
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
      selId = null; selLink = l.id; selSugg = null; draft = null; linkFrom = null; editLabel = null;
      renderBanner(); renderCard(true);
      if (focus) { var N = cur._s.nodes; centerAt((N[l.s].x + N[l.t].x) / 2, (N[l.s].y + N[l.t].y) / 2); }
      closePanelsOnPhone();
      kick();
    }
    function selectSugg(id) {
      if (!cur || !cur._s || !cur._s.sid[String(id)]) return;
      if (!showSugg) toggleSugg(true);
      selId = null; selLink = null; selSugg = cur._s.sid[String(id)].id; draft = null; linkFrom = null; editLabel = null;
      renderBanner(); renderCard(true); closePanelsOnPhone(); kick();
    }
    function linkBetween(a, b) {
      var s = cur._s, i = s.idx[a], j = s.idx[b];
      for (var k = 0; k < s.links.length; k++) { var l = s.links[k]; if ((l.s === i && l.t === j) || (l.s === j && l.t === i)) return l; }
      return null;
    }
    function startLinkMode(id) {
      if (!canLink()) return;
      selId = null; selLink = null; selSugg = null; draft = null; linkFrom = id; editLabel = null;
      card.hidden = true; renderBanner(); kick();
    }
    function startDraft(a, b) {
      var old = linkBetween(a, b);
      clearAim();
      if (old) { selectLink(old.id, false); say("These two are linked already: change its grade here."); return; }
      // the link as it was aimed: from the first paper to the second, which builds on it (pointing
      // back in time, the card says so and offers the swap)
      draft = { src: a, dst: b, anchor: a };
      selId = null; selLink = null; selSugg = null; linkFrom = null; editLabel = null;
      renderBanner(); renderCard(true); closePanelsOnPhone();
      // on the phone the card covers the lower half: the arrow being made goes between it and the bar
      if (phone()) {
        var s = cur._s, na = s.nodes[s.idx[a]], nb = s.nodes[s.idx[b]], v = s.view;
        userMoved = true; moveTo(v.k, W / 2 - (na.x + nb.x) / 2 * v.k, Math.max(H * 0.26, (chromeBottom() + H * 0.42) / 2) - (na.y + nb.y) / 2 * v.k, true);
      }
      kick();
    }
    function cancelDraft() { var a = draft ? draft.anchor : linkFrom; draft = null; linkFrom = null; clearAim(); if (a != null && cur && cur._s && cur._s.idx[a] != null) select(a); else select(null); }
    function renderBanner() {
      banner.textContent = "";
      aimHere();                   // the state changed: an arrow being aimed follows it
      if (linkFrom == null || !cur || !cur._s || cur._s.idx[linkFrom] == null) { banner.hidden = true; return; }
      banner.appendChild(h("span", null, (phone() ? "Tap" : "Click") + " the paper to link with " + label(linkFrom)));
      banner.appendChild(btn("Cancel", "pm-btn", cancelDraft));
      banner.hidden = false;
    }

    /* ---------- the edits: shown at once, set right by the hub's answer ---------- */
    function edit(op) { pending.push(op); rebuildAll(); }
    function settle(op) { var i = pending.indexOf(op); if (i >= 0) pending.splice(i, 1); settled++; }
    // An edit goes to the hub with the revision of the graph it was made on (base_rev), one edit
    // at a time, so a second quick edit carries the revision the first one made. `seen` is taken
    // when the person acts: if the graph was brought up to date from the hub before the edit
    // leaves, it goes with the revision the person saw (and the hub says whether that is stale).
    var EQ = Promise.resolve();
    function seen(g) { return g && !g.tmp && g.rev != null ? { g: g, rev: g.rev, ep: g.revEp || 0 } : null; }
    function send(sn, method, path, body) {
      var g = sn && sn.g, out = false;
      var p = EQ.then(function () {
        var b = body, q = "";
        if (g && g.rev != null) {
          var base = (g.revEp || 0) === sn.ep ? g.rev : sn.rev;
          if (method === "DELETE") q = (path.indexOf("?") < 0 ? "?" : "&") + "base_rev=" + base + "&graph_id=" + encodeURIComponent(g.id);
          else b = Object.assign({}, body || {}, { base_rev: base, graph_id: g.id });
        }
        if (g) { g.inflight = (g.inflight || 0) + 1; out = true; }
        return call(method, path + q, b);
      });
      // the next edit waits for this one's answer, but not for ever (a request the network lost)
      EQ = Promise.race([p.then(function () {}, function () {}), new Promise(function (res) { setTimeout(res, 10000); })]);
      var landed = function () { if (out && !--g.inflight) g.seenRv = null; };
      return p.then(function (r) { took(r); landed(); return r; }, function (err) { landed(); throw err; });
    }
    // An edit's answer: the graphs it changed are at these revisions now. One up from what is
    // drawn: it is what is drawn now. Drawn already (the hub's answer to a read overtook this
    // one): someone else's change is in it only when that read saw a newer revision than this
    // edit made (`seenRv`), and then the edits people made before it came go as they were made.
    function took(r) {
      var revs = r && r.revs;
      if (!revs || typeof revs !== "object") return;
      Object.keys(revs).forEach(function (id) {
        var g = byId[id], nr = revs[id];
        if (!g || !g.data || g.rev == null || nr == null) return;
        if (g.seenRv != null && g.seenRv > nr) { g.revEp = (g.revEp || 0) + 1; return; }
        if (nr === g.rev) return;
        if (nr === g.rev + 1) g.rev = nr; else { g.stale = true; if (g === cur) soon({ graph: true }); }
      });
    }
    // The hub refused an edit made on an older revision (someone changed the graph meanwhile):
    // the edit is already rolled back; the graph comes up to date at once, and the message says so.
    function refused(err, g) {
      var b = (err && err.body) || {};
      if (!err || err.status !== 409 || b.error !== "stale") return false;
      g = g || cur;
      if (!g) return true;
      if (b.deleted) { soon({ list: true, log: true }); say(quote(g.meta.name) + " was deleted meanwhile."); return true; }
      if (b.by && b.actor === "human") editedBy(g, b.by, b.at);
      g.revEp = (g.revEp || 0) + 1;              // the edits made before this answer came go as they were made
      reload(g).then(function (ok) {
        loadList(); loadLog(true);
        var who = b.actor !== "human" ? (b.actor === "agent" ? "The agent just changed" : "This graph just changed") :
          isMe(b.by) ? "An edit of yours just changed" : (nameOf(b.by) || "Someone") + " just changed";
        say(who + " this graph; " + (ok ? "it’s up to date now. Try again." : "it could not be loaded again: " + (g.loadErr || "the hub did not answer") + "."));
      });
      return true;
    }
    // the graph from the hub again, from a request sent from now on (not one already on its way)
    function reload(g) {
      return new Promise(function (res) {
        g.stale = true;
        (g.waiters = g.waiters || []).push({ after: LOADS.n, res: res });
        loadGraph(g);
      });
    }
    var LOADS = { n: 0 };
    var refetchT = null;
    function refetchSoon() { clearTimeout(refetchT); refetchT = setTimeout(function () { if (cur && visible) loadGraph(cur); loadLog(); }, 300); }
    function addLink(src, dst, grade) {
      var tmp = "t" + (++tmpSeq), made = new Date().toISOString();
      var op = { apply: function (g, nodes, links) {
        if (has(nodes, src) && has(nodes, dst)) links.push({ id: tmp, src: src, dst: dst, grade: grade, origin: "human", by: { id: ME.id, name: ME.name }, created_at: made, pending: true });
      } };
      draft = null; selLink = tmp; selId = null; clearAim();
      var sn = seen(cur);
      edit(op);
      send(sn, "POST", "/api/links", { src: src, dst: dst, grade: grade }).then(function (r) {
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
        say((r && r.already ? "Linked already: " : "Linked: ") + label(dst) + " builds on " + label(src) + ".");
      }, function (err) {
        settle(op); if (selLink === tmp) selLink = null;
        rebuildAll();
        if (!refused(err, sn && sn.g)) say("Could not add the link: " + errText(err) + ".");
      });
    }
    function setGrade(id, grade) {
      var l = cur._s.lid[String(id)];
      if (!l || l.grade === grade || l.e.pending) return;
      var op = { apply: function (g, nodes, links) { for (var i = 0; i < links.length; i++) if (same(links[i].id, id)) links[i] = Object.assign({}, links[i], { grade: grade }); } };
      var sn = seen(cur);
      edit(op);
      send(sn, "PUT", "/api/links/" + encodeURIComponent(id), { grade: grade }).then(function (r) {
        var got = (r && ((r.link && r.link.grade) || r.grade)) || grade;
        settle(op);
        forData(function (g, d) { d.links = d.links.map(function (x) { return same(x.id, id) ? Object.assign({}, x, { grade: got }) : x; }); });
        rebuildAll(); refetchSoon();
      }, function (err) {
        settle(op); rebuildAll();
        if (!refused(err, sn && sn.g)) say("Could not change the grade: " + errText(err) + ".");
      });
    }
    function removeLink(id) {
      var l = cur._s.lid[String(id)];
      if (!l || l.e.pending) return;
      var words = label(l.e.src) + " → " + label(l.e.dst);
      var op = { apply: function (g, nodes, links) { for (var i = links.length - 1; i >= 0; i--) if (same(links[i].id, id)) links.splice(i, 1); } };
      selLink = null; card.hidden = true;
      var sn = seen(cur);
      edit(op);
      send(sn, "DELETE", "/api/links/" + encodeURIComponent(id)).then(function (r) {
        settle(op);
        forData(function (g, d) { d.links = d.links.filter(function (x) { return !same(x.id, id); }); });
        rebuildAll(); refetchSoon();
        if (r && r.already) say("Removed already: " + words + ".");
        else say("Removed " + words + ".", EDIT ? undoMine("link.remove", id) : null);
      }, function (err) {
        settle(op); rebuildAll();
        if (!refused(err, sn && sn.g)) say("Could not remove the link: " + errText(err) + ".");
      });
    }
    function stub(pid) {
      var p = paper(pid) || {};
      return { id: pid, title: p.title, year: p.year, authors: p.authors, made_by: p.made_by, x: null, y: null };
    }
    function addPaper(g, pid) {
      var op = { apply: function (x, nodes) { if (x === g && !has(nodes, pid)) nodes.push(stub(pid)); } };
      var sn = seen(g);
      edit(op);
      if (g === cur) select(pid, true);
      send(sn, "POST", "/api/graphs/" + encodeURIComponent(g.id) + "/papers", { paper_id: pid }).then(function (r) {
        settle(op);
        if (g.data && !has(g.data.nodes, pid)) g.data.nodes = g.data.nodes.concat([(r && r.node) || stub(pid)]);
        rebuildAll(); loadGraph(g); loadLog(); soon({ list: true });
        say((r && r.already ? "Already in " : "Added " + label(pid) + " to ") + quote(g.meta.name) + ".");
      }, function (err) {
        settle(op); rebuildAll();
        if (!refused(err, g)) say("Could not add the paper: " + errText(err) + ".");
      });
    }
    function removePaper(g, pid) {
      var words = label(pid);
      var op = { apply: function (x, nodes) { if (x !== g) return; for (var i = nodes.length - 1; i >= 0; i--) if (nodes[i].id === pid) nodes.splice(i, 1); } };
      if (selId === pid) { selId = null; card.hidden = true; }
      var sn = seen(g);
      edit(op);
      send(sn, "DELETE", "/api/graphs/" + encodeURIComponent(g.id) + "/papers/" + encodeURIComponent(pid)).then(function (r) {
        settle(op);
        if (g.data) g.data.nodes = g.data.nodes.filter(function (n) { return n.id !== pid; });
        rebuildAll(); refetchSoon(); soon({ list: true });
        if (r && r.already) say(words + " was out of " + quote(g.meta.name) + " already.");
        else say("Took " + words + " out of " + quote(g.meta.name) + ".", EDIT ? undoMine("graph.remove_paper", pid) : null);
      }, function (err) {
        settle(op); rebuildAll();
        if (!refused(err, g)) say("Could not take the paper out: " + errText(err) + ".");
      });
    }
    function setLabel(pid, text) {
      text = String(text || "").replace(/\s+/g, " ").trim();
      var was = label(pid);
      editLabel = null;
      renderCard(true);                                   // the field goes (it has the focus: a refresh would skip it)
      if (!text || text === was) return;
      var op = { apply: function (g, nodes) { for (var i = 0; i < nodes.length; i++) if (nodes[i].id === pid) nodes[i] = Object.assign({}, nodes[i], { label: text }); } };
      var sn = seen(cur);
      edit(op);
      send(sn, "PUT", "/api/papers/" + encodeURIComponent(pid) + "/label", { label: text }).then(function (r) {
        var got = (r && (r.label || (r.paper && r.paper.label))) || text;
        settle(op);
        forData(function (g, d) { d.nodes = d.nodes.map(function (n) { return n.id === pid ? Object.assign({}, n, { label: got }) : n; }); });
        rebuildAll(); refetchSoon();
        say("The label is now " + quote(got) + ".");
      }, function (err) {
        settle(op); rebuildAll();
        if (!refused(err, sn && sn.g)) say("Could not change the label: " + (err.status === 404 || err.status === 405 ? "this hub does not take labels yet" : errText(err)) + ".");
      });
    }
    // A graph's name, tags or lock on their way to the hub stay over what a list says meanwhile.
    function putGraph(g, body, undoLocal, done) {
      var k = Object.keys(body)[0], tok = ++tmpSeq;
      g.mp = g.mp || {}; g.mpTok = g.mpTok || {}; g.mp[k] = body[k]; g.mpTok[k] = tok;
      var over = function () { settled++; if (g.mpTok[k] === tok) { delete g.mp[k]; delete g.mpTok[k]; } };
      renderTabs(); renderSettings(true); refreshCard();
      send(seen(g), "PUT", "/api/graphs/" + encodeURIComponent(g.id), body).then(function () {
        over();
        if (done) done();
        soon({ list: true, log: true });
      }, function (err) {
        over();
        undoLocal(); renderTabs(); renderSettings(true); refreshCard();
        if (!refused(err, g)) say("Could not change the graph: " + errText(err) + ".");
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
      var name = g.meta.name, sn = seen(g), i = drop(g);
      deleting[g.id] = true;          // a list answered meanwhile does not bring it back
      if (cur === g) { cur = null; showGraph(graphs[Math.max(0, i - 1)] || null); }
      openPanel(null); renderTabs();
      send(sn, "DELETE", "/api/graphs/" + encodeURIComponent(g.id)).then(function () {
        settled++; delete deleting[g.id];
        loadLog(); soon({ list: true });
        say("Deleted " + quote(name) + ".", undoMine("graph.delete", g.id));
      }, function (err) {
        settled++; delete deleting[g.id];
        if (err.status === 404) { loadLog(); soon({ list: true }); say(quote(name) + " was deleted already."); return; }
        graphs.splice(Math.min(i, graphs.length), 0, g); byId[g.id] = g; renderTabs();
        if (refused(err, g)) { showGraph(g); return; }
        say("Could not delete the graph: " + errText(err) + ".");
      });
    }

    /* ---------- suggestions: links an upload found, waiting for a person ---------- */
    function acceptSugg(id) {
      var x = cur && cur._s && cur._s.sid[String(id)];
      if (!x || x.e.pending) return;
      var e = x.e, tmp = "t" + (++tmpSeq), made = new Date().toISOString();
      var op = { apply: function (g, nodes, links, sugg) {
        if (sugg) for (var i = sugg.length - 1; i >= 0; i--) if (same(sugg[i].id, id)) sugg.splice(i, 1);
        if (has(nodes, e.src) && has(nodes, e.dst)) links.push({ id: tmp, src: e.src, dst: e.dst, grade: e.grade, origin: "human", by: { id: ME.id, name: ME.name }, created_at: made, pending: true });
      } };
      selSugg = null; selLink = tmp; selId = null;
      var sn = seen(cur);
      edit(op);
      send(sn, "POST", "/api/link-suggestions/" + encodeURIComponent(id) + "/accept", {}).then(function (r) {
        var l = (r && r.link) || null;
        settle(op);
        forData(function (g, d) {
          d.suggestions = (d.suggestions || []).filter(function (y) { return !same(y.id, id); });
          if (l && l.id != null && has(d.nodes, l.src) && has(d.nodes, l.dst) && !d.links.some(function (y) { return same(y.id, l.id); }))
            d.links = d.links.concat([{ id: l.id, src: l.src, dst: l.dst, grade: l.grade, origin: l.origin, by: { id: ME.id, name: ME.name }, created_at: made }]);
        });
        if (selLink === tmp) selLink = l ? l.id : null;
        rebuildAll(); refetchSoon(); soon({ settings: true });
        say((r && r.already ? "Linked already: " : "Accepted: ") + label(e.dst) + " builds on " + label(e.src) + ".");
      }, function (err) {
        settle(op); if (selLink === tmp) selLink = null;
        rebuildAll();
        if (!refused(err, sn && sn.g)) { say("Could not accept it: " + errText(err) + "."); refetchSoon(); }
      });
    }
    function dismissSugg(id) {
      var x = cur && cur._s && cur._s.sid[String(id)];
      if (!x || x.e.pending) return;
      var e = x.e;
      var op = { apply: function (g, nodes, links, sugg) { if (sugg) for (var i = sugg.length - 1; i >= 0; i--) if (same(sugg[i].id, id)) sugg.splice(i, 1); } };
      selSugg = null; card.hidden = true;
      edit(op);
      send(null, "POST", "/api/link-suggestions/" + encodeURIComponent(id) + "/dismiss", {}).then(function () {
        settle(op);
        forData(function (g, d) { d.suggestions = (d.suggestions || []).filter(function (y) { return !same(y.id, id); }); });
        rebuildAll(); refetchSoon(); soon({ settings: true });
        say("Dismissed " + label(e.src) + " → " + label(e.dst) + ": it will not be suggested again.");
      }, function (err) {
        settle(op); rebuildAll();
        say("Could not dismiss it: " + errText(err) + ".");
      });
    }
    function acceptAll() {
      if (!ADMIN || !SETS.open) return;
      var n0 = SETS.open;
      send(null, "POST", "/api/link-suggestions/accept-all", {}).then(function (r) {
        settled++;
        var n = (r && r.accepted) || 0, left = r && r.skipped ? r.skipped.length : 0;
        graphs.forEach(function (g) { g.stale = true; });
        soon({ list: true, graph: true, log: true, settings: true });
        say("Accepted " + (n === 1 ? "1 suggested link" : n + " suggested links") + (left ? "; " + left + " could not be (linked the other way since, or a loop)" : "") + ".");
      }, function (err) { say("Could not accept the " + n0 + " suggestions: " + errText(err) + "."); });
    }
    function toggleSugg(on) {
      showSugg = on == null ? !showSugg : !!on;
      if (!showSugg) { selSugg = null; hoverS = null; if (card.getAttribute("data-kind") === "suggestion") card.hidden = true; }
      renderSuggBtn(); kick();
    }
    function renderSuggBtn() {
      var n = cur && cur._s ? cur._s.sugg.length : 0;
      bSugg.hidden = !n;
      if (!n && showSugg) { showSugg = false; selSugg = null; }
      bSugg.textContent = (showSugg ? "Hide suggestions" : "Show suggestions") + " (" + n + ")";
      bSugg.setAttribute("aria-pressed", String(showSugg));
      root.classList.toggle("pm-subrow", !!n);
    }
    bSugg.addEventListener("click", function () { toggleSugg(); });
    // the hub's setting: links from uploads automatic, or suggestions only
    function loadSettings() {
      return call("GET", "/api/graph-settings").then(function (r) {
        SETS.mode = r && r.agent_links === "suggest" ? "suggest" : "auto"; SETS.open = (r && r.suggestions) || 0; SETS.loaded = true;
        renderSite();
      }, function () { SETS.loaded = false; renderSite(); });
    }
    function setMode(v) {
      if (!ADMIN || SETS.mode === v) return;
      var was = SETS.mode; SETS.mode = v; renderSite();
      send(null, "PUT", "/api/graph-settings", { agent_links: v }).then(function () {
        say(v === "suggest" ? "Links from uploads are suggestions now: someone accepts each." : "Links from uploads are added by themselves now.");
        soon({ settings: true });
      }, function (err) { SETS.mode = was; renderSite(); say("Could not change it: " + errText(err) + "."); });
    }
    var MODES = [["auto", "Automatic", "An upload’s links are drawn at once."], ["suggest", "Suggest only", "An upload’s links wait as suggestions until someone accepts them."]];
    function renderSite() {
      if (setEl.hidden) return;
      setSite.textContent = "";
      if (!SETS.loaded) return;
      setSite.appendChild(h("h2", null, "Links from uploads"));
      var m = MODES.filter(function (x) { return x[0] === SETS.mode; })[0] || MODES[0];
      if (ADMIN) {
        var seg = h("div", "pm-seg pm-modes"); seg.setAttribute("role", "group"); seg.setAttribute("aria-label", "Links from uploads");
        MODES.forEach(function (x) {
          var b = btn(x[1], "pm-segb", function () { setMode(x[0]); });
          b.setAttribute("aria-pressed", String(x[0] === SETS.mode)); b.setAttribute("data-mode", x[0]); b.title = x[2];
          seg.appendChild(b);
        });
        setSite.appendChild(seg);
        if (SETS.open) {
          var act = h("div", "pm-act");
          act.appendChild(btn("Accept all " + SETS.open + (SETS.open === 1 ? " suggestion" : " suggestions"), "pm-btn pm-acceptall", acceptAll));
          setSite.appendChild(act);
        }
      } else setSite.appendChild(h("p", "pm-note", "Links from uploads: " + m[1]));
    }

    /* ---------- the edit log: Undo and History ---------- */
    function entryById(id) { for (var i = 0; i < LOG.entries.length; i++) if (same(LOG.entries[i].id, id)) return LOG.entries[i]; return null; }
    function isMine(e) { return isMe(e.user_id != null ? e.user_id : e.user && e.user.id); }
    function candidate(scope, redo) {
      // the hub says which op it would undo (or redo); without its word: for an undo the newest
      // not yet reverted in scope, and nothing to redo
      var hint = redo ? LOG.redoHint : LOG.hint;
      if (hint && Object.prototype.hasOwnProperty.call(hint, scope)) {
        var hv = hint[scope];
        if (hv == null) return null;
        return entryById(typeof hv === "object" ? hv.id : hv) || (typeof hv === "object" ? hv : null);
      }
      if (redo) return null;
      for (var i = 0; i < LOG.entries.length; i++) {
        var e = LOG.entries[i];
        if (e.reverted_by != null) continue;
        if (scope === "mine" && !isMine(e)) continue;
        return e;
      }
      return null;
    }
    // change, undo (of a change) or redo (of an undo): the hub's word, else counted back along revert_of
    function kindOf(e) {
      if (e.kind === "change" || e.kind === "undo" || e.kind === "redo") return e.kind;
      var d = 0, x = e;
      while (x && x.revert_of != null && d < 200) { d++; x = entryById(x.revert_of); }
      return d === 0 ? "change" : d % 2 ? "undo" : "redo";
    }
    // the change an undo or a redo goes back to (null when it is older than the entries here)
    function original(e) {
      var x = e, k = 0;
      while (x && x.revert_of != null && k++ < 200) x = entryById(x.revert_of);
      return x && x !== e ? x : null;
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
      if (e.revert_of != null && !inner) { var r = original(e); return (kindOf(e) === "redo" ? "redid: " : "undid: ") + (r ? sentence(r, true) : "an edit"); }
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
        LOG.redoHint = r && !Array.isArray(r) && r.redo ? r.redo : null;
        LOG.papers = (r && r.papers) || null; LOG.users = (r && r.users) || null;
        LOG.loaded = true; LOG.stale = false; LOG.err = null;
      }, function (err) { if (seq === LOG.seq) LOG.err = errText(err); }).then(function () {
        if (LOG.busy === p) LOG.busy = null;
        renderUndo(); renderRedo(); renderHist();
        if (LOG.again && !LOG.busy) { LOG.again = false; loadLog(); }
      });
      LOG.busy = p;
      return p;
    }
    // Undo, or redo (the undo the hub named): what it does is said first, and after
    function undo(scope, redo) {
      var e = candidate(scope, redo);
      if (!e) { say(redo ? "Nothing to redo." : "Nothing to undo."); return; }
      if (LOG.undoing) return;
      var text = redo ? redoWords(e) : sentence(e);
      LOG.undoing = true; renderUndo(); renderRedo();
      var body = { scope: scope, expect: e.id };
      if (redo) body.redo = true;
      var g0 = cur;
      send(seen(cur), "POST", "/api/graph-log/revert", body).then(function () {
        say((redo ? "Redone: " : "Undone: ") + text + ".");
      }, function (err) {
        var b = err.body || {}, no = redo ? "Not redone: " : "Not undone: ";
        if (refused(err, g0)) return;
        if (err.status === 409 && b.error === "moved") say(no + "someone edited since. The " + (redo ? "Redo" : "Undo") + " now shows the newest edit.");
        else if (err.status === 409) say(no + thing(e) + " was changed after that edit" + (b.message && b.message !== b.error ? " (" + String(b.message).replace(/\.$/, "") + ")" : "") + ".");
        else say("Could not " + (redo ? "redo" : "undo") + ": " + errText(err) + ".");
      }).then(function () {
        LOG.undoing = false; LOG.stale = true; settled++;
        graphs.forEach(function (g) { g.stale = true; });
        loadList(); if (cur && !cur.tmp) loadGraph(cur); loadLog(true);
      });
    }
    function redo(scope) { undo(scope, true); }
    // "Bob removed PPO → DPO": the change a redo brings back
    function redoWords(e) {
      var o = original(e);
      if (o) return sentence(o, true);
      return "what " + (isMine(e) ? "you" : userName(e) || "someone") + " undid";
    }
    // Undo and Redo are greyed out when there is nothing to undo or redo (known once the log is in)
    function renderUndoButtons() {
      [[bUndo, false, "undo"], [bRedo, true, "redo"]].forEach(function (x) {
        var c = candidate("any", x[1]) || candidate("mine", x[1]);
        x[0].title = c ? (x[1] ? "Redo: " + redoWords(c) + " · undone " + ago(c.at) : "Undo: " + sentence(c) + " · " + ago(c.at)) : x[1] ? "Redo" : "Undo";
        x[0].disabled = PANELS[x[2]].hidden && !LOG.err && (!LOG.loaded || !c);
      });
    }
    function renderUndo() {
      renderUndoButtons();
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
    }
    function renderRedo() {
      renderUndoButtons();
      if (redoEl.hidden) return;
      redoEl.textContent = "";
      redoEl.appendChild(h("h2", null, "Redo"));
      if (!LOG.loaded) { redoEl.appendChild(h("p", "pm-note", LOG.err ? "Could not load the changes: " + LOG.err + "." : "Loading…")); return; }
      [["mine", "My last undo"], ["any", "The last undo, by anyone"]].forEach(function (sc) {
        var e = candidate(sc[0], true), box = h("div", "pm-undo");
        box.setAttribute("data-scope", sc[0]);
        box.appendChild(h("div", "pm-undo-h", sc[1]));
        if (e) {
          box.appendChild(h("div", "pm-undo-t", "Redo: " + redoWords(e) + " · undone " + ago(e.at)));
          var b = btn("Redo", "pm-btn", function () { redo(sc[0]); });
          b.disabled = LOG.undoing || !EDIT;
          box.appendChild(b);
        } else box.appendChild(h("div", "pm-undo-t pm-muted", "Nothing to redo."));
        redoEl.appendChild(box);
      });
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
    function refreshCard() { if (!card.hidden || draft || selLink != null || selSugg != null || selId != null) renderCard(false); }
    function renderCard(force) {
      var a = document.activeElement;
      if (!force && a && card.contains(a) && (a.tagName === "INPUT" || editLabel != null)) return;   // someone is typing in it
      card.textContent = "";
      if (!cur || !cur._s) { card.hidden = true; return; }
      if (draft) draftCard(); else if (selLink != null && cur._s.lid[String(selLink)]) linkCard();
      else if (selSugg != null && cur._s.sid[String(selSugg)]) suggCard(); else if (selId != null && cur._s.idx[selId] != null) paperCard();
      else { card.hidden = true; return; }
      card.hidden = false;
    }
    function lockNote() {
      if (cur.meta.locked) card.appendChild(h("p", "pm-note", "Locked"));
    }
    function paperCard() {
      var id = selId, s = cur._s, n = s.nodes[s.idx[id]], can = canLink(), mem = canEdit();
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
      if (can) act.appendChild(btn("Link to…", "pm-btn pm-linkto", function () { startLinkMode(id); }));
      if (mem) act.appendChild(btn("Take out of this graph", "pm-btn pm-rm", function () { removePaper(cur, id); }));
      if (act.childNodes.length) card.appendChild(act);
      lockNote();
      section("Builds on", n.par.map(function (j) { return s.nodes[j].id; }));
      section("Built on by", n.ch.map(function (j) { return s.nodes[j].id; }));
      // the graphs it is in, as the hub says (else those drawn so far)
      var others = Array.isArray(n.o.in) ? n.o.in.map(function (x) { return byId[x]; }).filter(function (g) { return g && g !== cur; })
        : graphs.filter(function (g) { return g !== cur && g._s && g._s.idx[id] != null; });
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
      var s = cur._s, l = s.lid[String(selLink)], e = l.e, A = s.nodes[l.s].id, B = s.nodes[l.t].id, can = canLink() && !e.pending;
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
      if (can) { var act = h("div", "pm-act"); act.appendChild(btn("Remove link", "pm-btn pm-danger", function () { removeLink(e.id); })); card.appendChild(act); }
      else if (e.pending) card.appendChild(h("p", "pm-note", "Saving…"));
      lockNote();
      card.appendChild(h("h2", null, "Papers"));
      var ul = h("ul", "pm-list"); ul.appendChild(item(A, null, "earlier")); ul.appendChild(item(B, null, "builds on it")); card.appendChild(ul);
    }
    function suggCard() {
      var x = cur._s.sid[String(selSugg)], e = x.e, A = e.src, B = e.dst, can = canLink() && !e.pending;
      card.setAttribute("data-kind", "suggestion");
      closeX();
      card.appendChild(h("h3", null, label(A) + " → " + label(B)));
      card.appendChild(h("p", "pm-dir", label(B) + " builds on " + label(A)));
      var up = e.by && (e.by.name || isMe(e.by)) ? (isMe(e.by) ? "your" : e.by.name + "’s") + " upload" : "an upload";
      card.appendChild(h("p", "pm-sub pm-by", "Suggested, " + GNAME[x.grade] + ": found in " + up + (e.created_at ? " · " + ago(e.created_at) : "")));
      if (can) {
        var act = h("div", "pm-act");
        act.appendChild(btn("Accept", "pm-open pm-accept", function () { acceptSugg(e.id); }));
        act.appendChild(btn("Dismiss", "pm-btn pm-dismiss", function () { dismissSugg(e.id); }));
        card.appendChild(act);
      }
      lockNote();
      card.appendChild(h("h2", null, "Papers"));
      var ul = h("ul", "pm-list"); ul.appendChild(item(A, null, "earlier")); ul.appendChild(item(B, null, "builds on it")); card.appendChild(ul);
    }
    function draftCard() {
      var A = draft.src, B = draft.dst, ya = yearOf(A), yb = yearOf(B), bad = backwards(A, B);
      card.setAttribute("data-kind", "draft");
      closeX(cancelDraft);
      card.appendChild(h("h3", null, "New link"));
      var p = h("p", "pm-dir"); p.appendChild(h("b", null, label(B))); p.appendChild(document.createTextNode(" builds on ")); p.appendChild(h("b", null, label(A)));
      card.appendChild(p);
      // the hub takes a link only from the earlier paper to the later one
      if (bad) card.appendChild(h("p", "pm-warn", label(B) + " is older: the arrow would go the other way."));
      card.appendChild(h("p", "pm-sub", "The arrow goes from " + label(A) + (ya != null ? " (" + ya + ")" : "") + " to " + label(B) + (yb != null ? " (" + yb + ")" : "") + ", the paper built on it."));
      card.appendChild(btn("Swap: " + label(A) + " builds on " + label(B), bad ? "pm-open pm-swap" : "pm-btn pm-swap", function () { draft = { src: B, dst: A, anchor: draft.anchor }; renderCard(true); kick(); }));
      card.appendChild(h("h2", null, "How much does " + label(B) + " build on it?"));
      var seg = h("div", "pm-seg pm-seg-add"); seg.setAttribute("role", "group"); seg.setAttribute("aria-label", "Add the link as");
      GRADES.forEach(function (g) { var b = btn(g[1], "pm-segb", function () { addLink(A, B, g[0]); }); b.title = g[2]; b.setAttribute("data-grade", g[0]); b.disabled = bad; seg.appendChild(b); });
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
      if (open) {
        // on the phone a panel and the card do not share the screen
        if (phone() && (selId != null || selLink != null || draft || linkFrom != null)) { selId = null; selLink = null; draft = null; linkFrom = null; clearAim(); card.hidden = true; renderBanner(); kick(); }
        if (key === "start") renderStart();
        if (key === "undo" || key === "redo" || key === "hist") { renderUndo(); renderRedo(); renderHist(); loadLog(); }
        if (key === "set") { renderSettings(true); renderSite(); loadSettings(); }
        if (key === "newg") renderNew();
      }
      renderTabs(); renderUndoButtons(); renderLive();
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
      if (!ol.childNodes.length) startEl.appendChild(h("p", "pm-note", "No links yet."));
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
        setDyn.appendChild(h("p", "pm-note", m.locked ? "Locked" : "View only"));
        return;
      }
      field(setDyn, "Name", m.name, "pm-name", "Rename", function (v) { renameGraph(g, v); });
      var tg = field(setDyn, "Tags", (m.tags || []).join(", "), "pm-tags", "Save tags", function (v) { setTags(g, tagList(v)); });
      tg.placeholder = "e.g. robotics, agents";
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
      if (canDelete(g)) {
        var dz = h("div", "pm-act pm-delrow");
        dz.appendChild(btn("Delete this graph", "pm-btn pm-danger pm-delg", function () { askDelete(g); }));
        setDyn.appendChild(dz);
      }
    }
    // its maker or an admin (the hub says; a hub that does not: the same rule here)
    function canDelete(g) { return canEdit(g) && (g.meta.can_delete != null ? g.meta.can_delete : ADMIN || same(g.meta.created_by, ME.id)); }

    /* ---------- the delete dialog ---------- */
    var dlgFor = null;
    function askDelete(g) {
      if (!canDelete(g) || dlg.open) return;
      dlgFor = g;
      dlgH.textContent = "Delete the graph " + "“" + g.meta.name + "”?";
      dlgP.textContent = "Its papers and links stay; only this graph goes. You can undo it.";
      dlgAct.textContent = "";
      var no = btn("Cancel", "pm-btn pm-dlg-no", function () { closeDialog(); });
      var yes = btn("Delete", "pm-btn pm-danger-fill pm-dlg-yes", function () { var x = dlgFor; closeDialog(true); if (x && byId[x.id] === x) deleteGraph(x); });
      dlgAct.appendChild(no); dlgAct.appendChild(yes);
      tip.hidden = true;
      try { dlg.showModal(); } catch (e) { dlg.setAttribute("open", ""); }
      no.focus();
    }
    function closeDialog(gone) {
      if (!dlg.open) return;
      dlg.close();
      var g = dlgFor; dlgFor = null;
      if (!gone && g) { var b = setEl.querySelector(".pm-delg"); if (b) b.focus(); }
    }
    dlg.addEventListener("cancel", function (e) { e.preventDefault(); closeDialog(); });      // Escape
    dlg.addEventListener("click", function (e) { if (e.target === dlg) closeDialog(); });      // outside the box: its backdrop
    dlg.addEventListener("close", function () { if (!dlg.open) dlgFor = null; });   // late: it may be open again for another ask
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
      var go = function () { createGraph(nm.value, tagList(tg.value)); };
      onEnter(nm, go); onEnter(tg, go);
      var act = h("div", "pm-act"); act.appendChild(btn("Make graph", "pm-open pm-make", go)); act.appendChild(btn("Cancel", "pm-btn", function () { openPanel(null); }));
      newEl.appendChild(act);
      setTimeout(function () { nm.focus(); }, 0);
    }
    bStart.addEventListener("click", function () { openPanel("start"); });
    bUndo.addEventListener("click", function () { openPanel("undo"); });
    bRedo.addEventListener("click", function () { openPanel("redo"); });
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
    function typing(t) { return !!t && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName)); }
    function onKey(e) {
      if (!visible) return;
      if (dlg.open) return;                           // the dialog's own: Escape closes it, nothing else here
      // as the page's home, its keys are the map's only from the map (not from the paper's window or the list)
      if (COL && e.target && e.target !== document.body && e.target !== document.documentElement && !root.contains(e.target)) return;
      if (e.key === "Shift" && !typing(e.target)) { shiftHeld = true; aimHere(); }
      // Ctrl/Cmd+Z: undo my last edit; Ctrl/Cmd+Shift+Z or Ctrl+Y: redo my last undo (not while typing)
      var k = String(e.key || "").toLowerCase();
      if ((e.ctrlKey || e.metaKey) && !e.altKey && (k === "z" || k === "y") && EDIT && !typing(e.target)) {
        e.preventDefault(); e.stopPropagation();
        var again = k === "y" || e.shiftKey;
        if (LOG.loaded && !LOG.stale) undo("mine", again); else loadLog(true).then(function () { undo("mine", again); });
        return;
      }
      if (e.key !== "Escape" || e.target === qEl) return;
      if (e.target && e.target.tagName === "INPUT" && root.contains(e.target)) {
        if (editLabel != null && card.contains(e.target)) { editLabel = null; renderCard(true); } else e.target.blur();
        e.stopPropagation(); return;
      }
      if (draft || linkFrom != null) { cancelDraft(); e.stopPropagation(); }
      else if (selId != null || selLink != null || selSugg != null) { select(null); e.stopPropagation(); }
      else if (opts.onClose) { opts.onClose(); e.stopPropagation(); }
    }
    document.addEventListener("keydown", onKey, true);
    document.addEventListener("keyup", function (e) { if (e.key === "Shift") { shiftHeld = false; aimHere(); } }, true);
    window.addEventListener("blur", function () { if (shiftHeld) { shiftHeld = false; aimHere(); } });

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
        var had = cur;
        cur = null; selId = null; selLink = null; selSugg = null; draft = null; linkFrom = null; editLabel = null; qset = null;
        renderTabs(); renderBanner(); renderEmpty(); renderLive(); renderSuggBtn(); card.hidden = true; tip.hidden = true; ctxClear();
        if (had && opts.onShow) opts.onShow(null);
        return;
      }
      if (cur === g) { applyWant(); return; }
      cur = g;
      if (!g.tmp && !COL) save(TKEY, g.id);
      if (opts.onShow && !g.tmp) opts.onShow(g.id);
      hoverI = null; hoverL = null; hoverS = null; selId = null; selLink = null; selSugg = null; draft = null; linkFrom = null; editLabel = null; clearAim();
      card.hidden = true; tip.hidden = true; hl = 0; hlSet = null; hlFocus = null; hlLink = null; hlKey = null; anim = null; moves = null; allPath = false;
      renderBanner(); renderLive();
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
        if (COL && r && r.unfiled && r.unfiled.id) {           // "Not in any graph": the last one
          var um = normMeta(r.unfiled), ug = byId[um.id] || { id: um.id, pseudo: true, meta: {}, data: null, _s: null, stale: true };
          Object.assign(ug.meta, um);
          seen[ug.id] = true; next.push(ug);
        }
        var gone = cur && !cur.tmp && !seen[cur.id] ? cur : null;
        graphs = next; byId = {}; graphs.forEach(function (g) { byId[g.id] = g; });
        listLoaded = true; listErr = null;
        if (dlgFor && !seen[dlgFor.id]) closeDialog(true);
        // a graph whose revision moved on without our hearing of it: asked again when shown
        graphs.forEach(function (g) { if (g.data && g.rev != null && g.meta.rev != null && g.meta.rev > g.rev) g.stale = true; });
        if (gone) { cur = null; say(quote(gone.meta.name) + " was deleted."); }
        renderTabs();
        // the page's graph list picks the graph (where it lands, what is open): only the one it asked for
        if (COL) { if (want && want.gid && byId[want.gid] && cur !== byId[want.gid]) showGraph(byId[want.gid]); else if (gone && opts.onShow) opts.onShow(null); }
        else if (!cur) showGraph(byId[want && want.gid] || byId[opts.graph] || byId[load(TKEY)] || graphs[0] || null);
        if (cur) { renderSettings(); refreshCard(); if (cur.stale && !cur.busy) loadGraph(cur); }
        renderEmpty();
        if (opts.onList) opts.onList(r);
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
      var s0 = settled, n0 = ++LOADS.n;
      var done = function (ok) { g.waiters = (g.waiters || []).filter(function (w) { if (n0 > w.after) { w.res(ok); return false; } return true; }); };
      g.busy = call("GET", "/api/graphs/" + encodeURIComponent(g.id)).then(function (r) {
        if (settled !== s0) { g.again = true; return; }
        g.data = normGraph(r); g.stale = false;
        if (r && r.graph) Object.assign(g.meta, normMeta(r.graph), g.mp || {});
        // the revision of what is drawn now (a new one from here, not from an edit of ours: note it)
        // (an edit of ours on its way: the new revision may be only that edit's; its answer says)
        var rv = r && (r.rev != null ? r.rev : r.graph && r.graph.rev);
        if (rv != null) {
          if (g.rev != null && rv !== g.rev) { if (g.inflight) g.seenRv = Math.max(g.seenRv || 0, rv); else g.revEp = (g.revEp || 0) + 1; }
          g.rev = rv;
        }
        if (r && r.graph && r.graph.changed) editedBy(g, r.graph.changed.by, r.graph.changed.at, r.graph.changed.actor);
        rebuild(g);
        if (g === cur) { afterChange(); applyWant(true); }
        else renderTabs();
        done(true);
        if (opts.onData) opts.onData(g.id, g.data, g.meta);
      }, function (err) {
        g.loadErr = errText(err);
        if (err.status === 404) { g.stale = true; soon({ list: true }); }
        if (g === cur) renderEmpty();
        done(false);
      }).then(function () {
        g.busy = null;
        if (g.again) { g.again = false; loadGraph(g); }
      });
      return g.busy;
    }
    // Each graph is read when it is shown, never all of them (a long list): a paper's card knows
    // the other graphs it is in from the hub's answer (each node's `in`).

    /* ---------- live: the page passes the hub's events; else a refetch on show() ---------- */
    var soonT = null, soonWhat = {};
    function soon(w) {
      Object.keys(w).forEach(function (k) { if (w[k]) soonWhat[k] = true; });
      if (soonT) return;
      soonT = setTimeout(function () {
        soonT = null; var x = soonWhat; soonWhat = {};
        if (!visible) {
          graphs.forEach(function (g) { if (!(opts.live && g === cur && x.graph)) g.stale = true; }); LOG.stale = true; SETS.loaded = SETS.loaded && !x.settings;
          if (opts.live) { if (x.list) loadList(); if (x.graph && cur) loadGraph(cur); }       // the page's list (and a phone's papers) stay current
          return;
        }
        if (x.list) loadList();
        if (x.graph && cur) loadGraph(cur);
        if (x.log) loadLog();
        if (x.settings && (SETS.loaded || !setEl.hidden)) loadSettings();
      }, 250);
    }
    function onGraphEvent(d) {
      d = d || {};
      if (d.change === "settings") { soon({ settings: true }); return; }
      if (d.change === "suggestions") soon({ settings: true });
      // one graph (graph_id), several (graphs: a link is in every graph that has both papers), or not said
      var gid = d.graph_id || (d.graph && d.graph.id) || (typeof d.id === "string" && /^g_/.test(d.id) ? d.id : null);
      var ids = Array.isArray(d.graphs) ? d.graphs.map(function (x) { return x && typeof x === "object" ? x.id : x; }) : gid ? [gid] : null;
      var mine = false;
      if (ids) ids.forEach(function (id) {
        var g = byId[id];
        if (!g) return;
        if (d.change === "edit" && d.by && d.actor === "human") editedBy(g, d.by, null);
        // our own edit, whose revision is drawn already: nothing to fetch for this graph
        if (d.change === "edit" && d.graph_rev != null && g.rev === d.graph_rev && !d.deleted) { if (g === cur) mine = true; return; }
        g.stale = true;
      }); else graphs.forEach(function (g) { g.stale = true; });
      soon({ list: true, graph: !mine && (!ids || (cur && ids.indexOf(cur.id) >= 0)), log: true });
    }
    // a new row in the edit log: the history and the Undo; the graphs it touched come with their own event
    function onLogEvent() { LOG.stale = true; soon({ log: true }); }
    function onPaperEvent(d) {
      d = d || {};
      // a new paper may join graphs by its tags; a label or title shows on the map
      if (d.label != null || d.new || d.title != null) { graphs.forEach(function (g) { g.stale = true; }); soon({ list: true, graph: true }); }
    }
    function onEpisodeEvent(d) {
      d = d || {};
      // a paper whose every episode is deleted (or back) leaves the map (or comes back)
      if (d.deleted != null || d.state === "rejected" || d.state === "ready") { graphs.forEach(function (g) { g.stale = true; }); soon({ list: true, graph: true }); }
    }
    function onResync() { graphs.forEach(function (g) { g.stale = true; }); LOG.stale = true; soon({ list: true, graph: true, log: true }); }
    var ON = { graph: onGraphEvent, log: onLogEvent, paper: onPaperEvent, episode: onEpisodeEvent, resync: onResync };
    if (typeof opts.subscribe === "function") {
      opts.subscribe("graph", onGraphEvent);
      opts.subscribe("log", onLogEvent);
      opts.subscribe("paper", onPaperEvent);
    }

    /* ---------- who else is editing this graph ---------- */
    // "Bob is editing this graph" for a minute after someone else changed it
    var LIVE_MS = 60000, liveT = null;
    function editedBy(g, by, at, actor) {
      if (!g || !by || (actor && actor !== "human")) return;
      var t = at ? Date.parse(at) : Date.now();
      if (isNaN(t)) t = Date.now();
      t = Math.min(t, Date.now());
      if (!g.editor || t >= g.editor.t) g.editor = { by: by, t: t };
      if (g === cur) renderLive();
    }
    function renderLive() {
      clearTimeout(liveT);
      var e = cur && cur.editor, left = e ? e.t + LIVE_MS - Date.now() : 0;
      if (!e || isMe(e.by) || !nameOf(e.by) || left <= 0) { live.hidden = true; return; }
      if (phone() && panelOpen()) { live.hidden = true; return; }        // under the panel there
      live.textContent = nameOf(e.by) + " is editing this graph";
      live.title = "Changed it " + ago(new Date(e.t).toISOString());
      live.hidden = false;
      liveT = setTimeout(renderLive, Math.min(left + 50, LIVE_MS));
    }

    if (!initGL()) g2 = gv.getContext("2d");
    gv.addEventListener("webglcontextlost", function (e) { e.preventDefault(); });
    gv.addEventListener("webglcontextrestored", function () { initGL(); kick(); });
    readColors();
    var mq = window.matchMedia("(prefers-color-scheme: dark)");
    if (mq.addEventListener) mq.addEventListener("change", function () { readColors(); kick(); });
    document.addEventListener("pcg-theme", function () { readColors(); kick(); });   // theme.js: chosen on the page
    document.addEventListener("pcg-size", function () { resize(); });                // theme.js: the page's zoom
    if (window.ResizeObserver) new ResizeObserver(function () { resize(); }).observe(root); else window.addEventListener("resize", resize);
    resize();
    renderTabs(); renderEmpty();
    if (!phone()) openPanel("start");
    loadList(); loadLog(); loadSettings();

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
      // the page's live events: graph, log, paper, episode, resync
      event: function (kind, data) { var fn = ON[kind]; if (fn) fn(data || {}); },
      hide: function () { visible = false; tip.hidden = true; hoverI = null; hoverL = null; shiftHeld = false; clearAim(); release(); closeDialog(true); },
      // everything again from the hub (what show() does)
      refresh: function () { loadList(); if (cur && !cur.tmp) loadGraph(cur); graphs.forEach(function (g) { if (g !== cur) g.stale = true; }); loadLog(); loadSettings(); },
      // show a paper (and the graph it is in, when given)
      select: function (paperId, graphId) { want = { pid: paperId, gid: graphId }; if (graphId && byId[graphId] && byId[graphId] !== cur) showGraph(byId[graphId]); else if (graphId && !byId[graphId]) loadList(); else applyWant(); },
      // the page's graph list: show this graph (when the list has it; else once it does)
      graph: function (gid) {
        if (!gid) { want = null; showGraph(null); return; }
        want = { gid: gid };
        if (byId[gid]) { if (byId[gid] !== cur) showGraph(byId[gid]); else if (!cur.data || cur.stale) loadGraph(cur); } else loadList();
      },
      current: function () { return cur ? cur.id : null; },
      data: function (gid) { var g = byId[gid]; return g && g.data ? g.data : null; },
      meta: function (gid) { var g = byId[gid]; return g ? g.meta : null; },
      refreshList: function () { loadList(); },
      selected: function () { return selId; },
      // the selection's card and the panels closed (the page opens a paper's window over the map's side)
      deselect: function () { select(null); },
      debug: { S: S, cur: function () { return cur; }, graphs: function () { return graphs; }, show: function (id) { showGraph(byId[id]); }, select: select, selectLink: selectLink,
        hover: function (i) { hoverI = i; kick(); }, draw: function () { draw(); }, tick: function () { tick(cur._s); }, webgl: function () { return !!gl; },
        state: function () {
          var t = aim && aim.p && cur && cur._s ? tipNow() : null;
          return { sel: selId, link: selLink, from: linkFrom, draft: draft && { src: draft.src, dst: draft.dst }, pending: pending.length, moving: !!moves, running: running,
            aim: t ? { from: aim.from, over: aim.over, tip: [t.x, t.y], r: t.r, warn: aimBad(), easing: aimQ() < 1, text: aimTip.hidden ? null : aimTip.textContent } : null,
            rev: cur ? cur.rev : null, dialog: dlg.open ? dlgH.textContent : null, live: live.hidden ? null : live.textContent,
            sugg: { shown: showSugg, n: cur && cur._s ? cur._s.sugg.length : 0, sel: selSugg, mode: SETS.mode, open: SETS.open } };
        },
        // a paper's place on the screen, and a link's middle (the screen's px, where a click goes: the
        // map's CSS px, which view() and the nodes are in, times zoom(), from the map's corner)
        pos: function (id) { var n = nodeOf(cur, id), v = cur._s.view; return n ? onScreen(n.x * v.k + v.x, n.y * v.k + v.y) : null; },
        mid: function (lid) { var l = cur._s.lid[String(lid)], N = cur._s.nodes, v = cur._s.view; if (!l) return null; return onScreen((N[l.s].x + N[l.t].x) / 2 * v.k + v.x, (N[l.s].y + N[l.t].y) / 2 * v.k + v.y); },
        zoom: function () { return Z; },
        view: function (k, x, y) { var v = cur._s.view; if (k != null) { v.k = k; v.x = x; v.y = y; anim = null; userMoved = true; draw(); } return { k: v.k, x: v.x, y: v.y }; },
        // the graph's colour at a point of the screen (as pos() gives it), read right after drawing
        pixel: function (x, y) {
          draw();
          var q = local({ clientX: x, clientY: y }), X = Math.round(q.x * DPR), Y = Math.round(q.y * DPR);
          if (gl) { var px = new Uint8Array(4); gl.readPixels(X, gv.height - 1 - Y, 1, 1, gl.RGBA, gl.UNSIGNED_BYTE, px); return [px[0], px[1], px[2]]; }
          return Array.prototype.slice.call(g2.getImageData(X, Y, 1, 1).data, 0, 3);
        } }
    };
    window.PaperMap.current = api;       // for the browser tests
    return api;
  }
  window.PaperMap = { mount: mount };
})();
