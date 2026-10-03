#!/usr/bin/env python3
"""The map on the real page (app.js mounting map.js beside the graph list: the page's home) against
the real hub (graph.py, the SSE stream), in two headless Chromes at once: Alice (admin) and Bob
(contributor) on the same graph.

What is checked: an edit on one page shows on the other within a second (a link, its grade, its
removal, a paper taken out, a rename, a label, an undo, a redo, a delete and its undo, a layout);
an edit made on a revision someone else moved past is refused and the page brings itself up to
date at once, with nothing half-applied; redo after undo for "my last undo" and "the last undo by
anyone", greyed out when there is nothing to redo; the delete dialog (Cancel, Escape, a click
outside, Delete, then Undo), and no Delete for someone who neither made the graph nor is an
admin (a graph someone made: its maker's and admins' to change); links from uploads switched to suggest only by an admin, the suggestions on both pages,
accepted on one and seen on the other, Accept all, and automatic again; Ctrl/Cmd+Z, Ctrl/Cmd+Shift+Z and Ctrl+Y, not while typing; the phone's 44 px taps for the
new buttons and the dialog; no console error or CSP violation on either page.

    nice python3 -m unittest discover -s stacks/papercast-group/hub/tests -p 'test_map_live.py' -v

Skips when no headless Chrome or no `websocket-client` is available."""
from __future__ import annotations

import json
import sys
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from web_rig import Rig, publish  # noqa: E402

from hub import db, graph  # noqa: E402

try:
    import websocket  # noqa: F401
    from web_cdp import Browser, find_chrome
    SKIP = None if find_chrome() else "no headless Chrome"
except ImportError:
    SKIP = "websocket-client not installed"

A, B, C = "alice@example.org", "bob@example.org", "carol@example.org"
M = "PaperMap.current.debug"
J = json.dumps
LIVE_S = 1.2          # "within about a second": from the hub's answer to the other page showing it

# test_map.py's TAP_TARGETS: every control in the map (only the dialog's while it is open)
TAP_TARGETS = r"""(() => {
  const map = document.getElementById('map');
  const root = map.querySelector('dialog[open]') || map;
  const q = 'a[href], button, input:not([type=hidden]), select, textarea, label, [role=button], [tabindex]:not([tabindex="-1"])';
  const name = (e) => `${e.tagName.toLowerCase()}.${e.className} "${(e.getAttribute('aria-label') || e.textContent || e.placeholder || '').trim().slice(0, 30)}"`;
  const bad = [];
  for (const e of root.querySelectorAll(q)) {
    const cs = getComputedStyle(e);
    if (e.closest('[hidden]') || cs.visibility !== 'visible' || cs.pointerEvents === 'none' || !e.getClientRects().length) continue;
    if ((e.type === 'checkbox' || e.type === 'radio') && e.closest('label')) continue;
    e.scrollIntoView({block: 'nearest', inline: 'nearest'});
    const r = e.getBoundingClientRect();
    if (r.width < 43.5 || r.height < 43.5) { bad.push(`${name(e)} is ${r.width.toFixed(1)} x ${r.height.toFixed(1)}`); continue; }
    const at = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
    if (!at || !(at === e || e.contains(at) || (e.tagName === 'LABEL' && at.closest('label') === e))) bad.push(`${name(e)} is covered by ${at ? name(at) : 'nothing'}`);
  }
  return JSON.stringify(bad);
})()"""


def errors(b):
    out = []
    for m in b.events:
        meth, p = m.get("method"), m.get("params", {})
        if meth == "Runtime.exceptionThrown":
            d = p.get("exceptionDetails", {})
            out.append(f"exception: {(d.get('exception') or {}).get('description') or d.get('text')}")
        elif meth == "Runtime.consoleAPICalled" and p.get("type") in ("error", "assert"):
            out.append("console.error: " + " ".join(str(a.get("value", a.get("description", ""))) for a in p.get("args", [])))
        elif meth == "Log.entryAdded" and p.get("entry", {}).get("level") == "error":
            e = p["entry"]
            out.append(f"log ({e.get('source')}): {e.get('text')} {e.get('url', '')}")
    return out


@unittest.skipIf(SKIP, SKIP or "")
class LiveMap(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.r = r = Rig()
        cls.alice = r.user(A, "Alice", "admin")
        cls.bob = r.user(B, "Bob", "contributor")
        cls.carol = r.user(C, "Carol", "viewer")
        cls.p = []
        for i, (title, year) in enumerate([("Alpha: a fake first paper", 2015), ("Beta: a fake second paper", 2017),
                                           ("Gamma: a fake third paper", 2018), ("Delta: a fake fourth paper", 2021),
                                           ("Epsilon: a fake fifth paper", 2023)]):
            pid = r.paper(title, cls.alice, year=year, tags=["reinforcement learning"])
            r.episode(pid, cls.alice)
            cls.p.append(pid)
        graph.ensure_schema()
        cls.rl = next(g["id"] for g in r.req("GET", "/api/graphs", user=A)[1]["graphs"] if g["name"] == "Reinforcement learning")
        cls.ba = cls.open_browser(A)
        cls.bb = cls.open_browser(B)

    @classmethod
    def open_browser(cls, who):
        b = Browser()
        b.call("Log.enable")
        b.viewport(1440, 900)
        b.call("Network.setExtraHTTPHeaders", headers={"X-Test-User": who})
        return b

    @classmethod
    def tearDownClass(cls):
        for b in (cls.ba, cls.bb):
            b.close()
        cls.r.close()

    def setUp(self):
        for b in (self.ba, self.bb):
            b.pump(0.05)
            b.events.clear()
        self.allow = []                  # console errors a test expects: 409 answers Chrome logs as failed loads

    def tearDown(self):
        errs = {}
        for b, who in ((self.ba, "Alice"), (self.bb, "Bob")):
            b.pump(0.3)
            errs[who] = [e for e in errors(b) if not any(a in e for a in self.allow)]
            b.events.clear()
            b.call("Page.navigate", url="about:blank")
        b.pump(0.2)
        db.conn().execute("DELETE FROM links")
        db.conn().execute("DELETE FROM graph_members")
        db.conn().execute("UPDATE graphs SET deleted_at = NULL, locked = 0, name = 'Reinforcement learning' WHERE id = ?", (self.rl,))
        db.conn().execute("UPDATE papers SET label = NULL")
        db.conn().execute("DELETE FROM link_suggestions")
        db.conn().execute("DELETE FROM graph_settings")
        self.assertEqual(errs, {"Alice": [], "Bob": []}, "errors in the console")

    # ------------------------------------------------------------------ helpers
    def map(self, b, who, hide_events=False, phone=False):
        """The page at the Reinforcement learning graph: its map, beside the list of graphs (a
        phone: the graph's drawing, #g=…&map)."""
        b.call("Page.navigate", url="about:blank")
        if phone:
            b.call("Emulation.setDeviceMetricsOverride", width=390, height=844, deviceScaleFactor=1, mobile=True)
        else:
            b.viewport(1440, 900)
        if hide_events:            # a page that hears nothing (its stream dropped): only the revision check helps it
            deaf = b.call("Page.addScriptToEvaluateOnNewDocument",
                          source="window.EventSource = function () { this.addEventListener = function () {}; this.close = function () {}; this.readyState = 1; };")["identifier"]
        b.goto(self.r.base + "/")
        b.js("localStorage.clear(); sessionStorage.clear()")
        b.goto("about:blank")
        b.goto(self.r.base + f"/#g={self.rl}" + ("&map" if phone else ""))
        b.wait_js("document.querySelectorAll('#gl .gl-row').length > 0 && !document.getElementById('map').hidden", 10, f"{who}'s page")
        b.wait_js(f"!!(window.PaperMap && PaperMap.current && {M}.cur() && {M}.cur().id === {J(self.rl)} && {M}.cur()._s"
                  f" && {M}.cur()._s.nodes.length === 5 && {M}.state().rev != null)", 10, f"{who}'s map")
        b.wait_js("!document.querySelector('#map .pm-ib[aria-label=Undo]').disabled || true", 5)
        if hide_events:
            b.call("Page.removeScriptToEvaluateOnNewDocument", identifier=deaf)

    def shows(self, b, expr, what, secs=LIVE_S):
        """Within secs (the other page, live)."""
        t0 = time.time()
        b.wait_js(expr, secs, what)
        return time.time() - t0

    def row(self, gid, name=None):
        """Graph gid's row in the list of graphs (with this name)."""
        r = f"document.querySelector('#gl .gl-row[data-id=\"{gid}\"]')"
        return f"!!{r}" if name is None else f"(r => !!r && r.querySelector('.gl-name').textContent === {J(name)})({r})"

    def button(self, scope, text):
        return f"[...document.querySelectorAll('#map {scope} button')].find(x => x.textContent.trim() === {J(text)})"

    def press(self, b, scope, text):
        self.assertTrue(b.js(f"!!{self.button(scope, text)}"), f"no button {text!r} in {scope}")
        b.js(f"{self.button(scope, text)}.click()")

    def click(self, b, x, y, shift=False):
        for t in ("mouseMoved", "mousePressed", "mouseReleased"):
            kw = {"button": "left", "clickCount": 1} if t != "mouseMoved" else {}
            b.call("Input.dispatchMouseEvent", type=t, x=x, y=y, modifiers=8 if shift else 0, **kw)

    def key(self, b, key, code, vk, mods=0):
        b.call("Input.dispatchKeyEvent", type="keyDown", key=key, code=code, windowsVirtualKeyCode=vk, modifiers=mods)

    def has_link(self, b, src, dst, grade=None):
        g = "" if grade is None else f" && l.grade === {J(grade)}"
        return f"{M}.cur()._s.links.some(l => l.e.src === {J(src)} && l.e.dst === {J(dst)} && !l.e.pending{g})"

    def link_id(self, src, dst):
        r = db.conn().execute("SELECT id FROM links WHERE src = ? AND dst = ? AND state = 'active'", (src, dst)).fetchone()
        return r[0] if r else None

    def api(self, method, path, body=None, who=B, code=None):
        st, js, _ = self.r.req(method, path, body, user=who)
        if (code is None and st >= 300) or (code is not None and st != code):
            raise AssertionError(f"{method} {path} -> {st} {js}")
        return js

    def undo_api(self, who, scope="mine", redo=False):
        log = self.api("GET", "/api/graph-log", who=who)
        e = log["redo" if redo else "undo"][scope]
        body = {"scope": scope, "expect": e["id"]}
        if redo:
            body["redo"] = True
        return self.api("POST", "/api/graph-log/revert", body, who=who)

    # ------------------------------------------------------------------ 1. two pages, one graph
    def test_1_an_edit_on_one_page_shows_on_the_other_within_a_second(self):
        a, b, p = self.ba, self.bb, self.p
        self.map(a, "Alice")
        self.map(b, "Bob")
        # Bob links Alpha to Delta on his map: Link to…, then a click on Delta, then Strong
        b.js(f"{M}.select({J(p[0])})")
        self.press(b, ".pm-card", "Link to…")
        x, y = b.js(f"{M}.pos({J(p[3])})")
        self.click(b, x, y)
        b.wait_js("document.querySelector('#map .pm-card').dataset.kind === 'draft'", 5, "Bob's new-link card")
        self.press(b, ".pm-seg-add", "Strong")
        b.wait_js(f"{M}.state().pending === 0 && {self.has_link(b, p[0], p[3])}", 5, "Bob's link saved")
        took = {"link": self.shows(a, self.has_link(a, p[0], p[3], "s"), "Bob's link on Alice's map")}
        lid = self.link_id(p[0], p[3])
        # his regrade, on his map
        b.js(f"{M}.selectLink({lid})")
        b.js("document.querySelector('#map .pm-card .pm-seg [data-grade=e]').click()")
        b.wait_js(f"{M}.state().pending === 0", 5, "regraded")
        took["grade"] = self.shows(a, self.has_link(a, p[0], p[3], "e"), "the new grade on Alice's map")
        # Alice sees who is editing
        a.wait_js("(e => !e.hidden && e.textContent === 'Bob is editing this graph')(document.querySelector('#map .pm-live'))", 2, "Bob is editing")
        # the rest through the hub, as Bob
        self.api("PUT", f"/api/papers/{p[1]}/label", {"label": "Beta-2"})
        took["label"] = self.shows(a, f"{M}.cur()._s.nodes[{M}.cur()._s.idx[{J(p[1])}]].label === 'Beta-2'", "the label")
        self.api("DELETE", f"/api/graphs/{self.rl}/papers/{p[2]}")
        took["paper out"] = self.shows(a, f"{M}.cur()._s.idx[{J(p[2])}] == null", "Gamma taken out")
        self.api("PUT", f"/api/graphs/{self.rl}", {"name": "RL"}, who=A)
        took["rename"] = self.shows(b, f"{self.row(self.rl, 'RL')} && document.getElementById('gh-name').textContent === 'RL'", "the new name on Bob's page")
        self.undo_api(A)                                             # Alice's rename undone
        took["undo"] = self.shows(b, self.row(self.rl, "Reinforcement learning"), "the undo")
        self.undo_api(A, redo=True)
        took["redo"] = self.shows(b, self.row(self.rl, "RL"), "the redo")
        self.api("DELETE", f"/api/links/{lid}")
        took["link removed"] = self.shows(a, f"!({self.has_link(a, p[0], p[3])})", "the removal")
        # the hub's layout moved a paper: it eases there on both
        db.conn().execute("INSERT OR REPLACE INTO layout(graph_id, paper_id, x, y, updated_at) VALUES (?, ?, ?, ?, ?)", (self.rl, p[4], 321.0, -123.0, db.now()))
        db.conn().execute("INSERT INTO layout_state(graph_id, rev, updated_at) VALUES (?, 9, ?) "
                          "ON CONFLICT(graph_id) DO UPDATE SET rev = rev + 1", (self.rl, db.now()))       # as layout.py stores one
        publish("graph", {"id": self.rl, "change": "layout", "rev": 9})
        took["layout"] = self.shows(a, f"(n => n.ox === 321 && n.oy === -123)({M}.cur()._s.nodes[{M}.cur()._s.idx[{J(p[4])}]])", "on its way to the new place")
        a.wait_js(f"(n => n.x === 321 && n.y === -123)({M}.cur()._s.nodes[{M}.cur()._s.idx[{J(p[4])}]])", 2, "eased there")
        # a graph deleted on one page goes from the other; its undo brings it back
        gid = self.api("POST", "/api/graphs", {"name": "Bob's picks", "tags": ["reinforcement learning"]}, code=201)["graph"]["id"]
        self.shows(a, self.row(gid), "Bob's new graph in Alice's list")
        self.api("DELETE", f"/api/graphs/{gid}")
        took["delete"] = self.shows(a, f"!{self.row(gid)}", "the deleted graph gone")
        self.undo_api(B)
        took["undelete"] = self.shows(a, self.row(gid), "the graph back")
        self.assertEqual(a.js(f"{M}.state().rev"), self.api("GET", f"/api/graphs/{self.rl}")["rev"], "Alice's map is not at the hub's revision")
        print("\n  shown on the other page after (s): " + ", ".join(f"{k} {v:.2f}" for k, v in took.items()), file=sys.stderr)

    # ------------------------------------------------------------------ 2. a stale edit
    def test_2_an_edit_on_an_old_revision_is_refused_and_the_page_catches_up(self):
        a, p = self.ba, self.p
        self.allow = ["409"]
        self.map(a, "Alice", hide_events=True)                      # she hears nothing from here on
        rev0 = a.js(f"{M}.state().rev")
        self.api("POST", "/api/links", {"src": p[0], "dst": p[1], "grade": "w"})     # Bob, meanwhile
        time.sleep(0.4)
        self.assertFalse(a.js(self.has_link(a, p[0], p[1])), "Alice heard of it after all")
        # Alice takes Delta out: refused, rolled back, her map brought up to date at once
        n_log = db.conn().execute("SELECT count(*) FROM graph_log").fetchone()[0]
        a.js(f"{M}.select({J(p[3])})")
        self.press(a, ".pm-card", "Take out of this graph")
        a.wait_js(f"{M}.state().pending === 0", 5, "answered")
        self.shows(a, self.has_link(a, p[0], p[1], "w"), "Bob's link, fetched at once", 2)
        a.wait_js(f"(m => !m.hidden && m.textContent.includes('up to date'))(document.querySelector('#map .pm-msg'))", 3, "said")
        self.assertEqual(a.js("document.querySelector('#map .pm-msg span').textContent"), "Bob just changed this graph; it’s up to date now. Try again.")
        self.assertIsNotNone(a.js(f"{M}.cur()._s.idx[{J(p[3])}]"), "the refused edit still shows")
        self.assertEqual(db.conn().execute("SELECT count(*) FROM graph_log").fetchone()[0], n_log, "something was written")
        self.assertIsNone(db.conn().execute("SELECT how FROM graph_members WHERE graph_id = ? AND paper_id = ?", (self.rl, p[3])).fetchone())
        self.assertEqual(a.js(f"{M}.state().rev"), rev0 + 1)
        # again: done
        a.js(f"{M}.select({J(p[3])})")
        self.press(a, ".pm-card", "Take out of this graph")
        a.wait_js(f"{M}.state().pending === 0 && (m => !m.hidden && m.textContent.startsWith('Took'))(document.querySelector('#map .pm-msg'))", 5, "taken out")
        self.assertEqual(db.conn().execute("SELECT how FROM graph_members WHERE graph_id = ? AND paper_id = ?", (self.rl, p[3])).fetchone()[0], "removed")
        # the same link Bob already made: done, not refused
        self.api("POST", "/api/links", {"src": p[1], "dst": p[4], "grade": "s"})
        a.js(f"{M}.select({J(p[1])})")
        self.press(a, ".pm-card", "Link to…")
        x, y = a.js(f"{M}.pos({J(p[4])})")
        self.click(a, x, y)
        a.wait_js("document.querySelector('#map .pm-card').dataset.kind === 'draft'", 5, "the new-link card")
        self.press(a, ".pm-seg-add", "Strong")
        a.wait_js(f"{M}.state().pending === 0 && (m => !m.hidden && m.textContent.startsWith('Linked already'))(document.querySelector('#map .pm-msg'))", 5, "done already")
        self.assertEqual(db.conn().execute("SELECT count(*) FROM links WHERE src = ? AND dst = ?", (p[1], p[4])).fetchone()[0], 1)

    # ------------------------------------------------------------------ 3. undo and redo, both scopes; the keys
    def redo_text(self, b, scope):
        return b.js(f"(t => t && t.textContent)(document.querySelector('#map [data-panel=redo] .pm-undo[data-scope={scope}] .pm-undo-t'))")

    def test_3_redo_after_undo_both_scopes_and_the_keys(self):
        a, b, p = self.ba, self.bb, self.p
        self.map(a, "Alice")
        self.map(b, "Bob")
        redo_off = "document.querySelector('#map .pm-ib[aria-label=Redo]').disabled"
        a.wait_js(redo_off, 3, "nothing to redo: greyed out")
        # Alice links Alpha and Beta, then Ctrl+Z
        self.api("POST", "/api/links", {"src": p[0], "dst": p[1], "grade": "s"}, who=A)
        a.wait_js(f"{self.has_link(a, p[0], p[1])} && document.querySelector('#map .pm-ib[aria-label=Undo]').title.startsWith('Undo: You added')", 3, "her link")
        self.key(a, "z", "KeyZ", 90, 2)
        a.wait_js(f"!({self.has_link(a, p[0], p[1])})", 3, "Ctrl+Z undid it")
        a.wait_js(f"!{redo_off}", 3, "Redo on")
        self.assertEqual(a.js("document.querySelector('#map .pm-ib[aria-label=Redo]').title"), "Redo: You added Alpha → Beta (strong) · undone just now")
        # Bob sees the undo, and can redo it as "the last undo, by anyone"
        self.shows(b, f"!({self.has_link(b, p[0], p[1])})", "the undo on Bob's map")
        b.js("document.querySelector('#map .pm-ib[aria-label=Redo]').click()")
        b.wait_js("!!document.querySelector('#map [data-panel=redo] .pm-undo[data-scope=any] button')", 3, "Bob's redo panel")
        self.assertEqual(self.redo_text(b, "mine"), "Nothing to redo.")
        self.assertEqual(self.redo_text(b, "any"), "Redo: Alice added Alpha → Beta (strong) · undone just now")
        self.press(b, "[data-panel=redo] .pm-undo[data-scope=any]", "Redo")
        b.wait_js(f"{self.has_link(b, p[0], p[1])} && (m => !m.hidden && m.textContent.startsWith('Redone'))(document.querySelector('#map .pm-msg'))", 3, "Bob redid it")
        self.assertEqual(b.js("document.querySelector('#map .pm-msg span').textContent"), "Redone: Alice added Alpha → Beta (strong).")
        self.shows(a, self.has_link(a, p[0], p[1]), "the redo on Alice's map")
        b.wait_js("document.querySelector('#map [data-panel=redo] .pm-undo[data-scope=any] .pm-undo-t').textContent === 'Nothing to redo.'", 3, "nothing more to redo for Bob")
        self.assertEqual(self.redo_text(b, "mine"), "Nothing to redo.")
        # the redo is Bob's edit: Alice has nothing of hers left to undo
        a.wait_js("document.querySelector('#map .pm-ib[aria-label=Undo]').title.startsWith('Undo: Bob redid')", 3, "the log")
        # a new link of hers: Ctrl+Z, Ctrl+Shift+Z, Ctrl+Z, Ctrl+Y
        self.api("POST", "/api/links", {"src": p[1], "dst": p[2], "grade": "w"}, who=A)
        a.wait_js(f"{self.has_link(a, p[1], p[2])} && document.querySelector('#map .pm-ib[aria-label=Undo]').title.startsWith('Undo: You added Beta')", 3, "her new link")
        self.key(a, "z", "KeyZ", 90, 2)
        a.wait_js(f"!({self.has_link(a, p[1], p[2])})", 3, "Ctrl+Z undid it")
        a.wait_js(f"!{redo_off}", 3, "Redo on")
        self.key(a, "Z", "KeyZ", 90, 2 | 8)
        a.wait_js(self.has_link(a, p[1], p[2]), 3, "Ctrl+Shift+Z redid it")
        a.wait_js(f"{redo_off} && document.querySelector('#map .pm-ib[aria-label=Undo]').title.startsWith('Undo: You redid')", 3, "the log")
        self.key(a, "z", "KeyZ", 90, 2)
        a.wait_js(f"!({self.has_link(a, p[1], p[2])}) && !{redo_off}", 3, "undone")
        self.key(a, "y", "KeyY", 89, 2)
        a.wait_js(self.has_link(a, p[1], p[2]), 3, "Ctrl+Y redid it")
        # in a text field the keys are the field's (the list's search box: the map has none of its own here)
        n = db.conn().execute("SELECT count(*) FROM graph_log").fetchone()[0]
        a.js("document.getElementById('search').focus()")
        self.key(a, "z", "KeyZ", 90, 2)
        self.key(a, "y", "KeyY", 89, 2)
        time.sleep(0.4)
        self.assertEqual(db.conn().execute("SELECT count(*) FROM graph_log").fetchone()[0], n, "Ctrl+Z in the search undid an edit")
        # a new edit after an undo: nothing to redo
        a.js("document.activeElement.blur()")
        self.key(a, "z", "KeyZ", 90, 2)
        a.wait_js(f"!{redo_off}", 3, "Redo on")
        self.api("PUT", f"/api/papers/{p[2]}/label", {"label": "G"}, who=A)
        a.wait_js(redo_off, 3, "greyed out after a new edit")

    # ------------------------------------------------------------------ 4. the delete dialog
    def test_4_delete_a_graph_after_the_dialog_and_undo_it(self):
        a, b = self.ba, self.bb
        mine = self.api("POST", "/api/graphs", {"name": "Bob's own"}, code=201)["graph"]["id"]
        # deleted once the pages are gone (one may still be telling the hub it opened it)
        self.addCleanup(lambda: db.conn().execute("UPDATE graphs SET deleted_at = ? WHERE id = ?", (db.now(), mine)))
        self.map(a, "Alice")
        self.map(b, "Bob")
        # Bob may delete his own graph, not the seeded one
        b.js("document.querySelector('#map .pm-ib[aria-label=\"Graph settings\"]').click()")
        self.assertFalse(b.js(f"!!{self.button('[data-panel=set]', 'Delete this graph')}"), "Bob may delete a seeded graph")
        b.js(f"{M}.show({J(mine)})")
        b.wait_js(f"{M}.cur().id === {J(mine)} && !!{self.button('[data-panel=set]', 'Delete this graph')}", 3, "Bob's Delete on his graph")
        # Alice, an admin, on the seeded graph: Escape, a click outside and Cancel keep it
        a.js("document.querySelector('#map .pm-ib[aria-label=\"Graph settings\"]').click()")
        opened = "document.querySelector('#map .pm-dialog').open"
        for how in ("escape", "outside", "cancel"):
            self.press(a, "[data-panel=set]", "Delete this graph")
            a.wait_js(opened, 3, "the dialog")
            self.assertEqual(a.js("document.querySelector('#map .pm-dialog h2').textContent"), "Delete the graph “Reinforcement learning”?")
            self.assertEqual(a.js("document.querySelector('#map .pm-dialog p').textContent"), "Its papers and links stay; only this graph goes. You can undo it.")
            self.assertEqual(a.js("document.activeElement.textContent"), "Cancel")
            if how == "escape":
                self.key(a, "Escape", "Escape", 27)
            elif how == "outside":
                self.click(a, 20, 450)
            else:
                self.press(a, ".pm-dialog", "Cancel")
            a.wait_js(f"!{opened}", 3, f"closed by {how}")
            self.assertTrue(a.js("!document.getElementById('map').hidden"), f"{how} closed the map")
            self.assertIsNone(db.conn().execute("SELECT deleted_at FROM graphs WHERE id = ?", (self.rl,)).fetchone()[0])
        self.press(a, "[data-panel=set]", "Delete this graph")
        a.wait_js(opened, 3, "the dialog")
        self.press(a, ".pm-dialog", "Delete")
        a.wait_js(f"!{opened} && !{self.row(self.rl)}", 3, "its row gone")
        a.wait_js("(m => !m.hidden && m.textContent.startsWith('Deleted'))(document.querySelector('#map .pm-msg'))", 3, "said")
        self.assertIsNotNone(db.conn().execute("SELECT deleted_at FROM graphs WHERE id = ?", (self.rl,)).fetchone()[0])
        self.shows(b, f"!{self.row(self.rl)}", "gone from Bob's list too")
        # the message's Undo brings it back, on both
        a.js("document.querySelector('#map .pm-msg button').click()")
        a.wait_js(self.row(self.rl), 3, "back")
        self.shows(b, self.row(self.rl), "back in Bob's list")
        self.assertIsNone(db.conn().execute("SELECT deleted_at FROM graphs WHERE id = ?", (self.rl,)).fetchone()[0])
        # Carol (a viewer who did not make it) sees no Delete
        b.call("Network.setExtraHTTPHeaders", headers={"X-Test-User": C})
        try:
            self.map(b, "Carol")
            b.js("document.querySelector('#map .pm-ib[aria-label=\"Graph settings\"]').click()")
            self.assertFalse(b.js(f"!!{self.button('[data-panel=set]', 'Delete this graph')}"), "Carol may delete it")
            b.js(f"{M}.show({J(mine)})")
            b.wait_js(f"{M}.cur().id === {J(mine)}", 3, "Bob's graph")
            self.assertFalse(b.js(f"!!{self.button('[data-panel=set]', 'Delete this graph')}"), "Carol may delete Bob's graph")
        finally:
            b.call("Network.setExtraHTTPHeaders", headers={"X-Test-User": B})

    # ------------------------------------------------------------------ 5. the phone
    def test_5_phone_taps_for_undo_redo_and_the_dialog(self):
        a = self.ba
        try:
            self.map(a, "Alice", phone=True)
            self.api("POST", "/api/links", {"src": self.p[0], "dst": self.p[4], "grade": "w"}, who=A)
            self.undo_api(A)
            a.wait_js("!document.querySelector('#map .pm-ib[aria-label=Redo]').disabled", 3, "Redo on")
            sizes = a.js("['Undo', 'Redo'].map(t => (r => [r.width, r.height])(document.querySelector(`#map .pm-ib[aria-label=${t}]`).getBoundingClientRect()))")
            z = a.js("document.documentElement.currentCSSZoom")         # 44 CSS px, times the page's zoom (theme.js's size)
            self.assertEqual([[round(v / z) for v in s] for s in sizes], [[44, 44], [44, 44]])
            self.assertEqual(json.loads(a.js(TAP_TARGETS)), [], "the map")
            a.js("document.querySelector('#map .pm-ib[aria-label=Redo]').click()")
            a.wait_js("!!document.querySelector('#map [data-panel=redo] .pm-undo button')", 3, "the redo panel")
            self.assertEqual(json.loads(a.js(TAP_TARGETS)), [], "the redo panel")
            a.js("document.querySelector('#map .pm-ib[aria-label=\"Graph settings\"]').click()")
            a.js(f"{self.button('[data-panel=set]', 'Delete this graph')}.click()")
            a.wait_js("document.querySelector('#map .pm-dialog').open", 3, "the dialog")
            self.assertEqual(json.loads(a.js(TAP_TARGETS)), [], "the dialog")
            self.assertLessEqual(a.js("document.documentElement.scrollWidth"), 390)
            self.press(a, ".pm-dialog", "Cancel")
        finally:
            a.viewport(1440, 900)

    # ------------------------------------------------------------------ 6. links from uploads: suggestions
    def upload(self, paper, others):
        """The agent's links of an upload by Bob (contrib calls this after the checks)."""
        return graph.apply_agent_links("e_up", paper, self.bob, [{"other": {"paper_id": o}, "direction": "builds_on", "grade": g} for o, g in others])

    def test_6_suggestions_reach_both_pages_and_are_accepted(self):
        a, b, p = self.ba, self.bb, self.p
        self.map(a, "Alice")
        self.map(b, "Bob")
        for x in (a, b):
            x.js("document.querySelector('#map .pm-ib[aria-label=\"Graph settings\"]').click()")
            x.wait_js("!!document.querySelector('#map .pm-setsite h2')", 3, "the setting")
        self.assertEqual(b.js("document.querySelectorAll('#map .pm-setsite button').length"), 0, "Bob may change the setting")
        self.assertEqual(b.js("document.querySelector('#map .pm-setsite .pm-note').textContent"), "Links from uploads: Automatic")
        # Alice (an admin) switches to suggestions: saved, and Bob's panel says so
        self.press(a, "[data-panel=set] .pm-modes", "Suggest only")
        a.wait_js("(m => !m.hidden && m.textContent.startsWith('Links from uploads are suggestions'))(document.querySelector('#map .pm-msg'))", 3, "saved")
        self.assertEqual(self.api("GET", "/api/graph-settings")["agent_links"], "suggest")
        self.shows(b, "document.querySelector('#map .pm-setsite .pm-note').textContent === 'Links from uploads: Suggest only'", "Bob's panel")
        # an upload finds two links: suggestions on both pages, not links
        out = self.upload(p[4], [(p[0], "s"), (p[2], "w")])
        self.assertEqual((out["added"], out["suggested"]), (0, 2))
        took = self.shows(b, "!document.querySelector('#map .pm-suggb').hidden && document.querySelector('#map .pm-suggb').textContent === 'Show suggestions (2)'", "Bob's suggestions")
        self.shows(a, "document.querySelector('#map .pm-suggb').textContent === 'Show suggestions (2)'", "Alice's suggestions")
        self.assertFalse(a.js(self.has_link(a, p[0], p[4])))
        # Bob shows them and accepts one on his map; Alice sees the link
        b.js("document.querySelector('#map .pm-ib[aria-label=\"Graph settings\"]').click()")    # the panel away
        b.js("document.querySelector('#map .pm-suggb').click()")
        (ax, ay), (bx, by) = b.js(f"{M}.pos({J(p[0])})"), b.js(f"{M}.pos({J(p[4])})")
        b.js(f"{M}.view(1, innerWidth / 2 - ({M}.cur()._s.nodes[{M}.cur()._s.idx[{J(p[0])}]].x + {M}.cur()._s.nodes[{M}.cur()._s.idx[{J(p[4])}]].x) / 2, innerHeight / 2 - ({M}.cur()._s.nodes[{M}.cur()._s.idx[{J(p[0])}]].y + {M}.cur()._s.nodes[{M}.cur()._s.idx[{J(p[4])}]].y) / 2)")
        (ax, ay), (bx, by) = b.js(f"{M}.pos({J(p[0])})"), b.js(f"{M}.pos({J(p[4])})")
        self.click(b, (ax + bx) / 2, (ay + by) / 2)
        b.wait_js("document.querySelector('#map .pm-card').dataset.kind === 'suggestion' && !document.querySelector('#map .pm-card').hidden", 3, "the suggestion's card")
        self.assertEqual(b.js("document.querySelector('#map .pm-card .pm-dir').textContent"), "Epsilon builds on Alpha")
        self.assertEqual(b.js("document.querySelector('#map .pm-card .pm-by').textContent").split(" · ")[0], "Suggested, strong: found in your upload")
        self.press(b, ".pm-card", "Accept")
        b.wait_js(f"{M}.state().pending === 0 && {self.has_link(b, p[0], p[4])}", 5, "accepted")
        took = self.shows(a, self.has_link(a, p[0], p[4], "s"), "the accepted link on Alice's map")
        self.shows(a, "document.querySelector('#map .pm-suggb').textContent === 'Show suggestions (1)'", "one left on Alice's map")
        e = self.api("GET", "/api/graph-log")["log"][0]
        self.assertEqual((e["op"], e["user"]["name"], e["actor"]), ("link.add", "Bob", "human"))
        # Accept all: Alice's, in her settings panel
        a.js("document.querySelector('#map .pm-ib[aria-label=\"Graph settings\"]').click()")
        a.wait_js("!!document.querySelector('#map .pm-acceptall')", 3, "Accept all")
        self.assertEqual(a.js("document.querySelector('#map .pm-acceptall').textContent"), "Accept all 1 suggestion")
        a.js("document.querySelector('#map .pm-acceptall').click()")
        self.shows(b, self.has_link(b, p[2], p[4], "w"), "the last one on Bob's map", 2)
        b.wait_js("document.querySelector('#map .pm-suggb').hidden", 3, "nothing left to show")
        # automatic again: the next upload's links are links at once
        self.press(a, "[data-panel=set] .pm-modes", "Automatic")
        a.wait_js("(m => !m.hidden && m.textContent.startsWith('Links from uploads are added'))(document.querySelector('#map .pm-msg'))", 3, "saved")
        self.assertEqual(self.upload(p[4], [(p[1], "e")])["added"], 1)
        self.shows(b, self.has_link(b, p[1], p[4], "e"), "an automatic link on Bob's map")
        self.assertTrue(b.js("document.querySelector('#map .pm-suggb').hidden"))


if __name__ == "__main__":
    unittest.main()
