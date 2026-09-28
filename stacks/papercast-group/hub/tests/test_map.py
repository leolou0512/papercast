"""The group's map (hub/static/map.js, map.css; owner A6) in headless Chrome, against a stand-in
for the graph API (map_fake.py, which follows SPEC.md section 8 as written; the real graph.py is
built alongside). What is checked: the tabs; the hover card; a link added by shift-click and by
the phone's "Link to…"; a link picked near its line at any zoom, regraded and removed; every edit
on screen before the hub answers, and rolled back with a short message when it fails; Undo for
"my last edit" and for "the last edit", and the hub's `moved` and `conflict` answers; History; a
new graph; a paper added and taken out; a graph and a label renamed; a locked graph read-only
for a viewer and open to an admin; the hub's live events, and a refetch on show() without them;
the colours read off the canvas in 2D and in WebGL, light and dark; the phone's two-row bar and
its 44 px taps; the positions as the hub sent them (no warm-up); no console error anywhere (the
test page carries the hub's CSP, so an inline style or script would show as one).

Needs a headless Chrome and websocket-client, as Leo's browser tests do (runs on stibnite):
    nice python3 -m unittest discover -s stacks/papercast-group/hub/tests -p 'test_map.py'
"""
from __future__ import annotations

import json
import sys
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from map_cdp import Browser, find_chrome  # noqa: E402
from map_fake import (ALICE, BOB, DDPM, DPO, FLOW, GEN, INSTRUCT, LEO, LOCKED, LOST, PPO, RL, RLHF,  # noqa: E402
                      TRPO, FakeHub)

try:
    import websocket  # noqa: F401
    HAVE_WS = True
except ImportError:
    HAVE_WS = False

M = "PaperMap.current.debug"
J = json.dumps

# Every control in the map as it is now: at least 44 x 44 px, and its middle reaches it (nothing
# covers it). A checkbox inside a label: the label takes the tap. As Leo's TAP_TARGETS.
TAP_TARGETS = r"""(() => {
  const root = document.getElementById('map');
  const q = 'a[href], button, input:not([type=hidden]), select, textarea, label, [role=button], [tabindex]:not([tabindex="-1"])';
  const name = (e) => `${e.tagName.toLowerCase()}.${e.className} "${(e.getAttribute('aria-label') || e.textContent || e.placeholder || '').trim().slice(0, 30)}"`;
  const bad = [];
  for (const e of root.querySelectorAll(q)) {
    const cs = getComputedStyle(e);
    if (e.closest('[hidden]') || cs.visibility !== 'visible' || cs.pointerEvents === 'none' || !e.getClientRects().length) continue;
    const lab = (e.type === 'checkbox' || e.type === 'radio') ? e.closest('label') : null;
    if (lab) continue;                                    // its label is checked on its own
    e.scrollIntoView({block: 'nearest', inline: 'nearest'});
    const r = e.getBoundingClientRect();
    if (r.width < 43.5 || r.height < 43.5) { bad.push(`${name(e)} is ${r.width.toFixed(1)} x ${r.height.toFixed(1)}`); continue; }
    const x = r.left + r.width / 2, y = r.top + r.height / 2;
    const at = document.elementFromPoint(x, y);
    if (!at || !(at === e || e.contains(at) || (e.tagName === 'LABEL' && at.closest('label') === e))) bad.push(`${name(e)} is covered by ${at ? name(at) : 'nothing'}`);
  }
  return JSON.stringify(bad);
})()"""


@unittest.skipUnless(HAVE_WS and find_chrome(), "needs a headless Chrome and websocket-client")
class MapTest(unittest.TestCase):
    b: Browser = None

    @classmethod
    def setUpClass(cls):
        cls.b = Browser()
        cls.b.call("Log.enable")

    @classmethod
    def tearDownClass(cls):
        if cls.b:
            cls.b.close()

    def setUp(self):
        self.hub = FakeHub().start()
        self.http_errors_ok = False
        b = self.b
        b.call("Emulation.setTouchEmulationEnabled", enabled=False)
        b.call("Emulation.setEmulatedMedia", features=[{"name": "prefers-color-scheme", "value": "light"}])
        b.viewport(1440, 900)
        b.events.clear()

    def tearDown(self):
        try:
            self.b.pump(0.3)
            self.assertEqual(self.console_errors(), [], "console errors")
        finally:
            self.b.call("Page.navigate", url="about:blank")
            self.b.pump(0.2)
            self.hub.stop()

    # ------------------------------------------------------------------ helpers
    def console_errors(self):
        out = []
        for ev in self.b.events:
            m, p = ev.get("method"), ev.get("params", {})
            if m == "Runtime.exceptionThrown":
                d = p.get("exceptionDetails", {})
                out.append("exception: " + str((d.get("exception") or {}).get("description") or d.get("text")))
            elif m == "Runtime.consoleAPICalled" and p.get("type") in ("error", "assert"):
                out.append("console: " + " ".join(str(a.get("value", a.get("description", ""))) for a in p.get("args", [])))
            elif m == "Log.entryAdded" and p.get("entry", {}).get("level") == "error":
                e = p["entry"]
                # an answer the test asked the fake hub for (409, 500...) is logged by Chrome as a failed load
                if e.get("source") == "network" and self.http_errors_ok:
                    continue
                out.append(f"log ({e.get('source')}): {e.get('text')} {e.get('url', '')}")
        return out

    def open(self, query="", me=LEO, tabs=4):
        self.hub.me = me
        self.b.goto(self.hub.url + (("?" + query) if query else ""))
        self.b.wait_js(f"!!(window.PaperMap && PaperMap.current && {M}.cur() && {M}.cur()._s && {M}.cur()._s.nodes.length)"
                       f" && document.querySelectorAll('#map .pm-tab').length === {tabs}", 10, "the map loaded")

    def js(self, expr):
        return self.b.js(expr)

    def wait(self, expr, what, timeout=6):
        return self.b.wait_js(expr, timeout, what)

    def pos(self, pid):
        return self.js(f"{M}.pos({J(pid)})")

    def near(self, lid, off):
        """A point `off` px beside a link's middle, across the line (CSS px)."""
        return self.js(f"""(() => {{ const c = {M}.cur(), s = c._s, l = s.lid[{J(str(lid))}], N = s.nodes, v = s.view;
            const ax = N[l.s].x * v.k + v.x, ay = N[l.s].y * v.k + v.y, bx = N[l.t].x * v.k + v.x, by = N[l.t].y * v.k + v.y;
            const d = Math.hypot(bx - ax, by - ay); return [(ax + bx) / 2 - (by - ay) / d * {off}, (ay + by) / 2 + (bx - ax) / d * {off}]; }})()""")

    def centre_on_link(self, lid, k):
        self.js(f"""(() => {{ const s = {M}.cur()._s, l = s.lid[{J(str(lid))}], N = s.nodes;
            const mx = (N[l.s].x + N[l.t].x) / 2, my = (N[l.s].y + N[l.t].y) / 2;
            {M}.view({k}, innerWidth / 2 - mx * {k}, innerHeight / 2 - my * {k}); }})()""")

    def click(self, x, y, shift=False):
        mod = 8 if shift else 0
        for t in ("mouseMoved", "mousePressed", "mouseReleased"):
            kw = {"button": "left", "clickCount": 1} if t != "mouseMoved" else {}
            self.b.call("Input.dispatchMouseEvent", type=t, x=x, y=y, modifiers=mod, **kw)

    def tap(self, x, y):
        self.b.call("Input.dispatchTouchEvent", type="touchStart", touchPoints=[{"x": x, "y": y}])
        self.b.call("Input.dispatchTouchEvent", type="touchEnd", touchPoints=[])

    def button(self, scope, text):
        return f"[...document.querySelectorAll('#map {scope} button')].find(b => b.textContent.trim() === {J(text)})"

    def press(self, scope, text):
        self.assertTrue(self.js(f"!!{self.button(scope, text)}"), f"no button {text!r} in {scope}")
        self.js(f"{self.button(scope, text)}.click()")

    def tap_button(self, scope, text):
        r = self.js(f"(() => {{ const b = {self.button(scope, text)}; if (!b) return null; b.scrollIntoView({{block: 'nearest'}}); const r = b.getBoundingClientRect(); return [r.left + r.width / 2, r.top + r.height / 2]; }})()")
        self.assertIsNotNone(r, f"no button {text!r} in {scope}")
        self.tap(*r)

    def text(self, sel):
        return self.js(f"(e => e ? e.textContent : null)(document.querySelector('#map {sel}'))")

    def texts(self, sel):
        return self.js(f"[...document.querySelectorAll('#map {sel}')].map(e => e.textContent)")

    def card(self):
        return self.js("(c => c.hidden ? null : c.dataset.kind)(document.querySelector('#map .pm-card'))")

    def state(self):
        return self.js(f"{M}.state()")

    def has_link(self, lid):
        return self.js(f"!!{M}.cur()._s.lid[{J(str(lid))}]")

    def link_between(self, src, dst):
        return self.js(f"(() => {{ const l = {M}.cur()._s.links.find(l => l.e.src === {J(src)} && l.e.dst === {J(dst)}); return l ? {{id: l.id, grade: l.grade, pending: !!l.e.pending}} : null; }})()")

    def msg(self):
        return self.js("(m => m.hidden ? null : m.firstChild.textContent)(document.querySelector('#map .pm-msg'))")

    def edits(self, method, pattern):
        return [(r[1], r[2]) for r in self.hub.edits(method, pattern)]

    def assert_csrf(self):
        for method, path, _, xpcg in self.hub.edits():
            self.assertEqual(xpcg, "1", f"{method} {path} went without X-PCG: 1")

    def phone(self):
        self.b.viewport(390, 844, mobile=True)
        self.b.call("Emulation.setTouchEmulationEnabled", enabled=True, maxTouchPoints=1)

    def until(self, fn, what, timeout=6):
        end = time.time() + timeout
        while time.time() < end:
            if fn():
                return
            time.sleep(0.05)
        self.fail(f"timed out: {what}")

    def assertTargets(self, where):
        if where != "the message":            # a message comes and goes; it is checked on its own
            self.js("document.querySelector('#map .pm-msg').hidden = true")
        bad = json.loads(self.js(TAP_TARGETS))
        self.assertEqual(bad, [], f"phone, {where}: {len(bad)} tap target(s) too small or covered:\n" + "\n".join(bad))

    def open_panel(self, title):
        self.js(f"document.querySelector('#map .pm-ib[aria-label=\"{title}\"]').click()")

    # ------------------------------------------------------------------ 1. tabs, card, hover, at rest
    def test_01_tabs_card_hover_and_at_rest(self):
        self.open()
        tabs = self.js("[...document.querySelectorAll('#map .pm-tab')].map(t => [t.textContent, t.getAttribute('aria-selected'), !!t.querySelector('.pm-lock')])")
        self.assertEqual(tabs, [["Reinforcement learning5", "true", False], ["Diffusion and generative models3", "false", False],
                                ["Locked picks3", "false", True], ["+ New graph", None, False]])
        # the positions are the hub's, as sent, and nothing runs at rest (no warm-up in the browser)
        time.sleep(0.8)
        self.assertEqual(self.js(f"{M}.cur()._s.nodes.map(n => [n.id, n.x, n.y])"),
                         [[TRPO, -160, -70], [PPO, -50, -10], [RLHF, -70, 120], [INSTRUCT, 70, 70], [DPO, 180, 10]])
        self.assertFalse(self.state()["running"], "frames keep running at rest")
        # the Start here panel (open on the desktop): roots, then the listening order
        self.assertEqual(self.texts("[data-panel=start] .pm-lbl"), ["TRPO", "RLHF", "TRPO", "PPO", "RLHF", "InstructGPT", "DPO"])
        # hover: title, then year and authors; and on a line, which builds on which
        x, y = self.pos(PPO)
        self.b.call("Input.dispatchMouseEvent", type="mouseMoved", x=x, y=y)
        self.wait("!document.querySelector('#map .pm-tip').hidden", "hover card")
        self.assertEqual(self.text(".pm-tip-t"), "Proximal Policy Optimization Algorithms")
        self.assertEqual(self.text(".pm-tip-s"), "2017 · Schulman et al.")
        x, y = self.near(4, 3)
        self.b.call("Input.dispatchMouseEvent", type="mouseMoved", x=x, y=y)
        self.wait("document.querySelector('#map .pm-tip-t').textContent === 'DPO builds on InstructGPT'", "hover on a link")
        self.assertEqual(self.text(".pm-tip-s"), "Strong link")
        # a click selects a paper: its card, who made it, its label, Open
        x, y = self.pos(DPO)
        self.click(x, y)
        self.wait("document.querySelector('#map .pm-card h3') && !document.querySelector('#map .pm-card').hidden", "paper card")
        self.assertEqual(self.card(), "paper")
        self.assertEqual(self.text(".pm-card h3"), "Direct Preference Optimization")
        self.assertEqual(self.text(".pm-card .pm-made"), "Made by you and Alice")
        self.assertEqual(self.text(".pm-card .pm-lab"), "DPO")
        self.assertEqual(self.texts(".pm-card .pm-also button"), ["Locked picks"])
        self.press(".pm-card", "Open (listened)")
        self.assertEqual(self.js("H.opened"), [DPO])
        # a drag moves the paper here only (as in Leo's map); nothing goes to the hub
        x, y = self.pos(RLHF)
        self.b.call("Input.dispatchMouseEvent", type="mousePressed", x=x, y=y, button="left", clickCount=1)
        for i in range(1, 8):
            self.b.call("Input.dispatchMouseEvent", type="mouseMoved", x=x + 10 * i, y=y - 6 * i, button="left", buttons=1)
            if i == 3:                           # the hub's news mid-drag: the graph is fetched again
                self.js("H.emit('graph', {})")
                self.wait(f"{M}.cur()._s.nodes.length === 5 && !!{M}.cur().busy === false && {M}.cur().stale === false", "refetched mid-drag")
        self.b.call("Input.dispatchMouseEvent", type="mouseMoved", x=x + 70, y=y - 42, button="left", buttons=1)
        self.assertGreater(self.js(f"{M}.cur()._s.target"), 0, "the forces stopped under the drag")
        self.b.call("Input.dispatchMouseEvent", type="mouseReleased", x=x + 70, y=y - 42, button="left", clickCount=1)
        nx, ny = self.pos(RLHF)
        self.assertLess(abs(nx - (x + 70)) + abs(ny - (y - 42)), 12, "the paper did not follow the pointer")
        self.assertEqual(self.hub.edits(), [])
        # another tab: its own nodes, where the hub put them
        self.js(f"document.querySelector('#map .pm-tab[data-id=\"{GEN}\"]').click()")
        self.wait(f"{M}.cur().id === {J(GEN)} && {M}.cur()._s.nodes.length === 3", "second tab")
        self.assertEqual(self.js(f"{M}.cur()._s.nodes.map(n => [n.x, n.y])"), [[-120, 0], [0, 40], [120, 0]])
        self.assertTrue(self.card() is None)
        # Escape: the selection first, then the map closes
        x, y = self.pos(DDPM)
        self.click(x, y)
        self.wait("!document.querySelector('#map .pm-card').hidden", "card")
        esc = lambda: self.b.call("Input.dispatchKeyEvent", type="keyDown", key="Escape", code="Escape", windowsVirtualKeyCode=27)
        esc()
        self.wait("document.querySelector('#map .pm-card').hidden", "Escape clears the selection")
        self.assertEqual(self.js("H.closed"), 0)
        esc()
        self.wait("H.closed === 1", "Escape closes the map")

    # ------------------------------------------------------------------ 2. a link by shift-click
    def test_02_add_a_link_by_shift_click(self):
        self.open()
        x, y = self.pos(DPO)
        self.click(x, y)
        self.wait("!document.querySelector('#map .pm-card').hidden", "DPO selected")
        x, y = self.pos(TRPO)
        self.click(x, y, shift=True)
        self.wait("document.querySelector('#map .pm-card').dataset.kind === 'draft'", "the new-link card")
        # the earlier paper is the one built on, whichever was picked first; in plain words
        self.assertEqual(self.text(".pm-card .pm-dir"), "DPO builds on TRPO")
        self.assertEqual(self.state()["draft"], {"src": TRPO, "dst": DPO})
        # the link is on the map before the hub answers
        self.hub.delay[("POST", r"^/api/links$")] = 1.5
        self.js("document.querySelector('#map .pm-seg-add [data-grade=w]').click()")
        got = self.link_between(TRPO, DPO)
        self.assertIsNotNone(got, "the link is not drawn at once")
        self.assertTrue(got["pending"])
        self.assertEqual(self.card(), "link")
        self.assertIn("Saving", self.text(".pm-card"))
        self.wait(f"{M}.state().pending === 0", "the hub answered", 5)
        got = self.link_between(TRPO, DPO)
        self.assertEqual(got, {"id": 9, "grade": "w", "pending": False}, "the hub's link replaces the one drawn")
        self.assertEqual(self.edits("POST", r"^/api/links$"), [("/api/links", {"src": TRPO, "dst": DPO, "grade": "w"})])
        self.assertEqual(self.js(f"{M}.state().link"), 9)
        self.assertEqual(self.text(".pm-card h3"), "TRPO → DPO")
        self.assertTrue(self.text(".pm-card .pm-by").startswith("Added by you"))
        self.assertTrue(self.hub.active(9))
        # two papers linked already: shift-click shows that link instead of a second one
        x, y = self.pos(PPO)
        self.click(x, y)
        self.wait("document.querySelector('#map .pm-card').dataset.kind === 'paper'", "PPO selected")
        x, y = self.pos(TRPO)
        self.click(x, y, shift=True)
        self.wait(f"{M}.state().link === 1", "the existing link")
        self.assertEqual(self.card(), "link")
        self.assertIn("linked already", self.msg())
        # Link to…, then the search and Enter pick the second paper
        self.js(f"{M}.select({J(RLHF)})")
        self.press(".pm-card", "Link to…")
        self.js("(q => { q.value = 'Direct Pref'; q.dispatchEvent(new Event('input')); q.focus(); })(document.querySelector('#map .pm-q'))")
        self.b.call("Input.dispatchKeyEvent", type="keyDown", key="Enter", code="Enter", windowsVirtualKeyCode=13)
        self.wait("document.querySelector('#map .pm-card').dataset.kind === 'draft' && !document.querySelector('#map .pm-card').hidden", "picked by search")
        self.assertEqual(self.state()["draft"], {"src": RLHF, "dst": DPO})
        self.assert_csrf()

    # ------------------------------------------------------------------ 3. a link on the phone
    def test_03_add_a_link_on_the_phone(self):
        self.phone()
        self.open()
        x, y = self.pos(RLHF)
        self.tap(x, y)
        self.wait("document.querySelector('#map .pm-card').dataset.kind === 'paper' && !document.querySelector('#map .pm-card').hidden", "RLHF card")
        r = self.js("(r => [r.left, r.bottom, r.width])(document.querySelector('#map .pm-card').getBoundingClientRect())")
        self.assertEqual(r, [0, 844, 390], "the card is a sheet at the bottom")
        self.assertTargets("a paper's card")
        self.tap_button(".pm-card", "Link to…")
        self.wait("!document.querySelector('#map .pm-banner').hidden", "the pick-a-second-paper banner")
        self.assertTrue(self.js("document.querySelector('#map .pm-card').hidden"))
        self.assertEqual(self.text(".pm-banner span"), "Tap the paper to link with RLHF")
        self.assertTargets("the banner")
        x, y = self.pos(DPO)
        self.tap(x, y)
        self.wait("document.querySelector('#map .pm-card').dataset.kind === 'draft' && !document.querySelector('#map .pm-card').hidden", "the new-link card")
        self.assertTrue(self.js("document.querySelector('#map .pm-banner').hidden"))
        self.assertEqual(self.text(".pm-card .pm-dir"), "DPO builds on RLHF")
        self.assertTargets("the new-link card")
        self.tap_button(".pm-card", "Swap: RLHF builds on DPO")
        self.wait("document.querySelector('#map .pm-card .pm-dir').textContent === 'RLHF builds on DPO'", "swapped")
        self.tap_button(".pm-card", "Swap: DPO builds on RLHF")
        self.wait("document.querySelector('#map .pm-card .pm-dir').textContent === 'DPO builds on RLHF'", "swapped back")
        self.tap_button(".pm-card .pm-seg-add", "Essential")
        self.wait(f"{M}.state().pending === 0 && !!{M}.cur()._s.lid['8']", "the link from the hub")
        self.assertEqual(self.edits("POST", r"^/api/links$"), [("/api/links", {"src": RLHF, "dst": DPO, "grade": "e"})])
        self.assertTrue(self.hub.active(8))
        self.assertEqual(self.link_between(RLHF, DPO)["grade"], "e")
        self.assertTargets("the link's card")
        # Cancel in link mode goes back to the paper
        self.wait(f"!{M}.state().running", "the view at rest after the pan to the new link")
        x, y = self.pos(PPO)
        self.tap(x, y)
        self.wait("document.querySelector('#map .pm-card').dataset.kind === 'paper' && !document.querySelector('#map .pm-card').hidden", "PPO card")
        self.tap_button(".pm-card", "Link to…")
        self.wait("!document.querySelector('#map .pm-banner').hidden", "banner")
        self.tap_button(".pm-banner", "Cancel")
        self.wait(f"document.querySelector('#map .pm-banner').hidden && {M}.state().sel === {J(PPO)}", "cancelled")
        self.assert_csrf()

    # ------------------------------------------------------------------ 4. pick a link, regrade, remove
    def test_04_pick_a_link_at_any_zoom_regrade_and_remove(self):
        self.open()
        x, y = self.near(2, 4)
        self.click(x, y)
        self.wait("document.querySelector('#map .pm-card').dataset.kind === 'link' && !document.querySelector('#map .pm-card').hidden", "link card")
        self.assertEqual(self.text(".pm-card h3"), "PPO → InstructGPT")
        self.assertEqual(self.text(".pm-card .pm-dir"), "InstructGPT builds on PPO")
        # an agent's link looks like any other; its card says whose upload it came with
        self.assertTrue(self.text(".pm-card .pm-by").startswith("Added by the agent for Alice"), self.text(".pm-card .pm-by"))
        self.assertEqual(self.js("[...document.querySelectorAll('#map .pm-card .pm-seg [aria-pressed=true]')].map(b => b.dataset.grade)"), ["s"])
        # regrade: pressed at once, saved after
        self.hub.delay[("PUT", r"^/api/links/2$")] = 1.0
        self.js("document.querySelector('#map .pm-card .pm-seg [data-grade=e]').click()")
        self.assertEqual(self.link_between(PPO, INSTRUCT)["grade"], "e")
        self.assertEqual(self.js("[...document.querySelectorAll('#map .pm-card .pm-seg [aria-pressed=true]')].map(b => b.dataset.grade)"), ["e"])
        self.wait(f"{M}.state().pending === 0", "regraded", 5)
        self.assertEqual(self.edits("PUT", r"^/api/links/"), [("/api/links/2", {"grade": "e"})])
        self.assertEqual(self.hub.s["links"][2]["grade"], "e")
        # remove: gone at once, with an Undo in the message
        self.hub.delay[("DELETE", r"^/api/links/2$")] = 1.0
        self.press(".pm-card", "Remove link")
        self.assertFalse(self.has_link(2), "not gone at once")
        self.assertIsNone(self.card())
        self.wait(f"{M}.state().pending === 0", "removed", 5)
        self.assertEqual(self.edits("DELETE", r"^/api/links/"), [("/api/links/2", None)])
        self.assertFalse(self.hub.active(2))
        self.wait("!document.querySelector('#map .pm-msg').hidden", "message")
        self.assertEqual(self.msg(), "Removed PPO → InstructGPT.")
        self.assertEqual(self.texts(".pm-msg button"), ["Undo"])
        time.sleep(0.5)
        self.assertFalse(self.has_link(2), "the refetch brought it back")
        # the message's Undo takes my last edit back (asking the hub which one it is first)
        self.js("document.querySelector('#map .pm-msg button').click()")
        self.wait(f"!!{M}.cur()._s.lid['2']", "the removal undone")
        self.assertEqual(self.edits("POST", "revert"), [("/api/graph-log/revert", {"scope": "mine", "expect": 5})])
        self.assertTrue(self.hub.active(2))
        # ...but not another edit that became my last one meanwhile (an agent link from my upload)
        self.js(f"{M}.selectLink(2)")
        self.press(".pm-card", "Remove link")
        self.wait("(m => !m.hidden && m.textContent.startsWith('Removed'))(document.querySelector('#map .pm-msg'))", "removed again")
        with self.hub.lock:
            self.hub.s["links"][11] = {"id": 11, "src": TRPO, "dst": RLHF, "grade": "w", "origin": "agent", "state": "active", "created_by": LEO, "created_at": "2026-09-28T00:00:00Z"}
            self.hub.add_log(LEO, "link.add", 11, None, {"id": 11, "src": TRPO, "dst": RLHF, "grade": "w", "state": "active"}, actor="agent")
        self.js("document.querySelector('#map .pm-msg button').click()")
        self.wait("!document.querySelector('#map [data-panel=undo]').hidden", "the Undo panel instead")
        self.assertEqual(self.msg(), "Your last edit is another one now: see Undo.")
        self.wait("(t => t && t.textContent.startsWith('Undo: The agent for you added TRPO'))(document.querySelector('#map [data-panel=undo] .pm-undo[data-scope=mine] .pm-undo-t'))", "what it would undo")
        self.assertEqual(len(self.edits("POST", "revert")), 1, "the message's Undo undid another edit")
        self.assertFalse(self.hub.active(2))
        # the reach is measured on the screen: the same few pixels far out and close in
        for k, lid, off in ((0.35, 4, 4), (3.5, 1, 5), (1.0, 3, -5)):
            self.centre_on_link(lid, k)
            x, y = self.near(lid, off)
            self.click(x, y)
            self.wait(f"{M}.state().link === {lid}", f"link {lid} picked at zoom {k}")
        x, y = self.near(3, 40)
        self.click(x, y)
        self.wait(f"{M}.state().link === null && document.querySelector('#map .pm-card').hidden", "a click on nothing clears")
        self.assert_csrf()

    # ------------------------------------------------------------------ 5. failures roll back
    def test_05_a_failed_edit_rolls_back_with_a_message(self):
        self.http_errors_ok = True
        self.open()
        self.hub.delay[("POST", r"^/api/links$")] = 0.8
        self.hub.fail[("POST", r"^/api/links$")] = (500, {"error": "internal", "message": "something went wrong on the hub"})
        self.js(f"{M}.select({J(DPO)})")
        x, y = self.pos(TRPO)
        self.click(x, y, shift=True)
        self.wait("document.querySelector('#map .pm-card').dataset.kind === 'draft'", "draft")
        self.js("document.querySelector('#map .pm-seg-add [data-grade=s]').click()")
        self.assertIsNotNone(self.link_between(TRPO, DPO), "drawn at once")
        self.wait(f"{M}.state().pending === 0", "the hub answered", 5)
        self.assertIsNone(self.link_between(TRPO, DPO), "not rolled back")
        self.assertEqual(self.msg(), "Could not add the link: something went wrong on the hub.")
        self.assertIsNone(self.card())
        # a removal the hub refuses comes back
        self.hub.delay[("DELETE", r"^/api/links/5$")] = 0.8
        self.hub.fail[("DELETE", r"^/api/links/5$")] = (403, {"error": "forbidden", "message": "not allowed here"})
        self.js(f"{M}.selectLink(5)")
        self.press(".pm-card", "Remove link")
        self.assertFalse(self.has_link(5))
        self.wait(f"{M}.state().pending === 0", "the hub answered", 5)
        self.assertTrue(self.has_link(5), "not rolled back")
        self.assertEqual(self.msg(), "Could not remove the link: not allowed here.")
        # a regrade the hub cannot reach
        self.hub.fail[("PUT", r"^/api/links/5$")] = (502, {"error": "bad_gateway"})
        self.js(f"{M}.selectLink(5)")
        self.js("document.querySelector('#map .pm-card .pm-seg [data-grade=s]').click()")
        self.wait(f"{M}.state().pending === 0 && {M}.cur()._s.lid['5'].grade === 'w'", "grade rolled back")
        self.assertEqual(self.msg(), "Could not change the grade: bad gateway.")
        self.assertEqual(self.js("[...document.querySelectorAll('#map .pm-card .pm-seg [aria-pressed=true]')].map(b => b.dataset.grade)"), ["w"])

    # ------------------------------------------------------------------ 6. undo, both scopes
    def test_06_undo_my_last_edit_and_the_last_edit(self):
        self.open()
        self.wait("document.querySelector('#map .pm-ib[aria-label=Undo]').title.startsWith('Undo: Bob')", "the Undo button says what")
        self.assertEqual(self.js("document.querySelector('#map .pm-ib[aria-label=Undo]').title"), "Undo: Bob removed RLHF → DPO · 3 min ago")
        self.open_panel("Undo")
        self.wait("!!document.querySelector('#map [data-panel=undo] .pm-undo[data-scope=mine] .pm-undo-t')", "undo panel")
        undo_text = lambda scope: self.text(f"[data-panel=undo] .pm-undo[data-scope={scope}] .pm-undo-t")
        self.assertEqual(undo_text("mine"), "Undo: You added TRPO → PPO (essential) · 2 h ago")
        self.assertEqual(undo_text("any"), "Undo: Bob removed RLHF → DPO · 3 min ago")
        # my last edit
        self.press("[data-panel=undo] .pm-undo[data-scope=mine]", "Undo")
        self.wait(f"!{M}.cur()._s.lid['1']", "TRPO → PPO undone on the map")
        self.assertEqual(self.edits("POST", "revert"), [("/api/graph-log/revert", {"scope": "mine", "expect": 1})])
        self.assertFalse(self.hub.active(1))
        self.assertEqual(self.msg(), "Undone: You added TRPO → PPO (essential).")
        # an undo is an edit too: now it is the last edit, mine and anyone's
        self.wait("document.querySelector('#map [data-panel=undo] .pm-undo[data-scope=any] .pm-undo-t').textContent.includes('undid')", "the undo is in the log")
        self.assertEqual(undo_text("any"), "Undo: You undid: You added TRPO → PPO (essential) · just now")
        self.assertEqual(undo_text("mine"), undo_text("any"))
        # the last edit, anyone's
        self.press("[data-panel=undo] .pm-undo[data-scope=any]", "Undo")
        self.wait(f"!!{M}.cur()._s.lid['1']", "TRPO → PPO back")
        self.assertEqual(self.edits("POST", "revert")[1], ("/api/graph-log/revert", {"scope": "any", "expect": 4}))
        self.assertTrue(self.hub.active(1))
        self.assert_csrf()

    # ------------------------------------------------------------------ 7. moved and conflict
    def test_07_undo_answered_moved_or_conflict(self):
        self.http_errors_ok = True
        self.open()
        self.open_panel("Undo")
        any_text = "document.querySelector('#map [data-panel=undo] .pm-undo[data-scope=any] .pm-undo-t')"
        self.wait(f"!!{any_text}", "undo panel")
        # conflict: RLHF -> DPO is back already (someone added it again), so Bob's removal cannot be undone
        with self.hub.lock:
            self.hub.s["links"][8]["state"] = "active"
        self.press("[data-panel=undo] .pm-undo[data-scope=any]", "Undo")
        self.wait("(m => !m.hidden && m.textContent.startsWith('Not undone'))(document.querySelector('#map .pm-msg'))", "the conflict said")
        self.assertEqual(self.msg(), "Not undone: RLHF → DPO was changed after that edit (the link is back already).")
        self.wait(f"!!{M}.cur()._s.lid['8']", "the view refreshed")
        # moved: Alice renamed a graph after the panel showed Bob's removal
        with self.hub.lock:
            self.hub.s["graphs"][GEN]["name"] = "Generative models"
            self.hub.add_log(ALICE, "graph.rename", GEN, {"id": GEN, "name": "Diffusion and generative models"}, {"id": GEN, "name": "Generative models"})
        self.assertIn("Bob removed", self.js(f"{any_text}.textContent"))
        self.press("[data-panel=undo] .pm-undo[data-scope=any]", "Undo")
        self.wait("(m => !m.hidden && m.textContent.includes('someone edited since'))(document.querySelector('#map .pm-msg'))", "moved said")
        self.assertEqual(self.msg(), "Not undone: someone edited since. The Undo now shows the newest edit.")
        self.wait(f"{any_text}.textContent.includes('Alice')", "the newest edit shown")
        self.assertEqual(self.js(f"{any_text}.textContent"),
                         "Undo: Alice renamed ‘Diffusion and generative models’ to ‘Generative models’ · just now")
        self.wait(f"document.querySelector('#map .pm-tab[data-id=\"{GEN}\"]').textContent === 'Generative models3'", "the tabs refreshed")
        self.assertEqual([b for _, b in self.edits("POST", "revert")], [{"scope": "any", "expect": 3}, {"scope": "any", "expect": 3}])
        self.assertEqual(self.hub.s["graphs"][GEN]["name"], "Generative models", "a moved undo changed something")

    # ------------------------------------------------------------------ 8. history
    def test_08_history_links_to_what_changed(self):
        self.open()
        self.open_panel("History")
        self.wait("document.querySelectorAll('#map [data-panel=hist] .pm-hitem').length === 3", "history")
        self.assertEqual(self.texts("[data-panel=hist] .pm-h-what"),
                         ["Bob removed RLHF → DPO", "Bob made InstructGPT → DPO strong (was essential)", "You added TRPO → PPO (essential)"])
        self.assertEqual(self.texts("[data-panel=hist] .pm-h-when"), ["3 min ago", "1 h ago", "2 h ago"])
        # a change to a link: its card
        self.js("document.querySelectorAll('#map [data-panel=hist] .pm-hitem')[1].click()")
        self.wait(f"{M}.state().link === 4", "the link from the history")
        self.assertEqual(self.text(".pm-card h3"), "InstructGPT → DPO")
        # a removed link: its paper
        self.js("document.querySelectorAll('#map [data-panel=hist] .pm-hitem')[0].click()")
        self.wait(f"{M}.state().sel === {J(RLHF)}", "a removed link shows its paper")
        # someone's change arrives (a log event): it tops the list; it links to another graph
        with self.hub.lock:
            self.hub.s["graphs"][LOCKED]["members"].append(FLOW)
            self.hub.add_log(ALICE, "graph.add_paper", f"{LOCKED}/{FLOW}", None, {"graph_id": LOCKED, "paper_id": FLOW})
        self.js("H.emit('log', {})")
        self.wait("document.querySelectorAll('#map [data-panel=hist] .pm-hitem').length === 4", "the new change")
        self.assertEqual(self.texts("[data-panel=hist] .pm-h-what")[0], "Alice added Flow Matching to ‘Locked picks’")
        self.js("document.querySelectorAll('#map [data-panel=hist] .pm-hitem')[0].click()")
        self.wait(f"{M}.cur().id === {J(LOCKED)} && {M}.state().sel === {J(FLOW)}", "the other graph, the paper selected")
        self.assertEqual(self.text(".pm-card h3"), "Flow Matching for Generative Modeling")
        # my own edit shows up at the top once the hub has it; an undone change is marked
        self.js(f"{M}.show({J(RL)})")
        self.wait(f"{M}.cur().id === {J(RL)} && {M}.cur()._s.nodes.length === 5", "back to RL")
        self.js(f"{M}.selectLink(5)")
        self.js("document.querySelector('#map .pm-card .pm-seg [data-grade=e]').click()")
        self.wait("document.querySelectorAll('#map [data-panel=hist] .pm-hitem').length === 5", "my edit in the history")
        self.assertEqual(self.texts("[data-panel=hist] .pm-h-what")[0], "You made PPO → DPO essential (was weak)")
        with self.hub.lock:
            self.assertEqual(self.hub.revert(self.hub.user(), "mine", 5)[0], 200)
        self.js("H.emit('log', {})")
        self.wait("document.querySelectorAll('#map [data-panel=hist] .pm-hitem').length === 6", "the undo in the history")
        self.assertEqual(self.texts("[data-panel=hist] .pm-hitem.undone .pm-h-what"), ["You made PPO → DPO essential (was weak)"])
        self.assertEqual(self.texts("[data-panel=hist] .pm-h-what")[0], "You undid: You made PPO → DPO essential (was weak)")

    # ------------------------------------------------------------------ 9. new graph, papers in and out
    def test_09_new_graph_and_papers_in_and_out(self):
        self.open()
        self.js("document.querySelector('#map .pm-tab-new').click()")
        self.wait("!document.querySelector('#map [data-panel=newg]').hidden && document.activeElement.classList.contains('pm-newname')", "the new-graph form")
        self.js("document.querySelector('#map .pm-newname').value = 'Robotics'; document.querySelector('#map .pm-newtags').value = 'robotics,  agents ,'")
        self.hub.delay[("POST", r"^/api/graphs$")] = 0.6
        self.press("[data-panel=newg]", "Make graph")
        self.assertEqual(self.js("[...document.querySelectorAll('#map .pm-tab.pm-tmp')].map(t => t.textContent)"), ["Robotics0"], "no tab at once")
        self.wait(f"{M}.cur() && {M}.cur().id === 'g_new0000001' && !!{M}.cur()._s", "the new graph shown")
        self.assertEqual(self.edits("POST", r"^/api/graphs$"), [("/api/graphs", {"name": "Robotics", "tags": ["robotics", "agents"]})])
        self.assertEqual(self.js("document.querySelector('#map .pm-tab[aria-selected=true]').textContent"), "Robotics0")
        self.assertEqual(self.text(".pm-empty p"), "No papers in this graph yet.")
        # add a paper from the library
        self.press(".pm-empty", "Add a paper")
        self.wait("!document.querySelector('#map [data-panel=set]').hidden && document.activeElement.classList.contains('pm-addq')", "the add search")
        self.js("(q => { q.value = 'robots'; q.dispatchEvent(new Event('input')); })(document.querySelector('#map .pm-addq'))")
        self.assertEqual(self.texts("[data-panel=set] .pm-addres .pm-lbl"), ["A Lost Paper About Robots"])
        self.hub.delay[("POST", r"/papers$")] = 0.6
        self.js("document.querySelector('#map [data-panel=set] .pm-addres button').click()")
        self.assertEqual(self.js(f"{M}.cur()._s.nodes.map(n => n.id)"), [LOST], "not on the map at once")
        self.assertTrue(self.js(f"(n => isFinite(n.x) && isFinite(n.y))({M}.cur()._s.nodes[0])"))
        self.assertTrue(self.js("document.querySelector('#map .pm-empty').hidden"))
        self.assertEqual(self.state()["sel"], LOST)
        self.wait(f"{M}.state().pending === 0", "added", 5)
        self.assertEqual(self.edits("POST", r"/papers$"), [("/api/graphs/g_new0000001/papers", {"paper_id": LOST})])
        self.wait("document.querySelector('#map .pm-tab[aria-selected=true]').textContent === 'Robotics1'", "the count")
        # take it out again
        self.js(f"{M}.select({J(LOST)})")
        self.press(".pm-card", "Take out of this graph")
        self.assertEqual(self.js(f"{M}.cur()._s.nodes.length"), 0, "not gone at once")
        self.wait(f"{M}.state().pending === 0", "taken out", 5)
        self.assertEqual(self.edits("DELETE", r"/papers/"), [(f"/api/graphs/g_new0000001/papers/{LOST}", None)])
        self.assertEqual(self.text(".pm-empty p"), "No papers in this graph yet.")
        self.assertEqual(self.msg(), "Took Robots out of ‘Robotics’.")
        # into a graph with links: placed by its neighbours (the hub has no place for it yet)
        self.js(f"{M}.show({J(RL)})")
        self.wait(f"{M}.cur().id === {J(RL)} && {M}.cur()._s.nodes.length === 5", "RL")
        with self.hub.lock:
            self.hub.s["links"][10] = {"id": 10, "src": DDPM, "dst": DPO, "grade": "w", "origin": "human", "state": "active", "created_by": BOB, "created_at": "2026-09-01T00:00:00Z"}
        self.open_panel("Graph settings")
        self.js("(q => { q.value = 'Denoising'; q.dispatchEvent(new Event('input')); })(document.querySelector('#map .pm-addq'))")
        self.js("document.querySelector('#map [data-panel=set] .pm-addres button').click()")
        self.wait(f"{M}.state().pending === 0 && !!{M}.cur()._s.lid['10'] && !{M}.state().moving", "DDPM and its link")
        d = self.js(f"(n => Math.hypot(n.x - 180, n.y - 10))({M}.cur()._s.nodes[{M}.cur()._s.idx[{J(DDPM)}]])")
        self.assertLess(d, 70, "not placed by its neighbour DPO")
        self.assertEqual(self.js(f"document.querySelector('#map .pm-tab[data-id=\"{RL}\"]').textContent"), "Reinforcement learning6")
        self.assert_csrf()

    # ------------------------------------------------------------------ 10. rename, tags, label
    def test_10_rename_tags_and_label(self):
        self.http_errors_ok = True
        self.open()
        self.open_panel("Graph settings")
        self.wait("!!document.querySelector('#map .pm-name')", "settings")
        self.assertEqual(self.js("document.querySelector('#map .pm-name').value"), "Reinforcement learning")
        self.js("document.querySelector('#map .pm-name').value = 'RL'")
        self.press("[data-panel=set]", "Rename")
        self.assertEqual(self.js(f"document.querySelector('#map .pm-tab[data-id=\"{RL}\"]').textContent"), "RL5", "not renamed at once")
        self.js("document.querySelector('#map .pm-tags').value = 'rl, reinforcement learning'")
        self.press("[data-panel=set]", "Save tags")
        self.until(lambda: len(self.edits("PUT", r"^/api/graphs/")) == 2, "the tags saved")
        self.assertEqual(self.edits("PUT", r"^/api/graphs/"), [(f"/api/graphs/{RL}", {"name": "RL"}),
                                                              (f"/api/graphs/{RL}", {"tags": ["rl", "reinforcement learning"]})])
        self.assertEqual(self.hub.s["graphs"][RL]["name"], "RL")
        # a label: on the map at once
        self.js(f"{M}.select({J(PPO)})")
        self.press(".pm-card", "Edit label")
        self.wait("!!(document.activeElement && document.activeElement.closest('.pm-label'))", "the label field")
        self.js("document.activeElement.value = 'PPO-clip'")
        self.b.call("Input.dispatchKeyEvent", type="keyDown", key="Enter", code="Enter", windowsVirtualKeyCode=13)
        self.assertEqual(self.js(f"{M}.cur()._s.nodes[{M}.cur()._s.idx[{J(PPO)}]].label"), "PPO-clip")
        self.wait(f"{M}.state().pending === 0", "saved")
        self.assertEqual(self.edits("PUT", "/label$"), [(f"/api/papers/{PPO}/label", {"label": "PPO-clip"})])
        self.assertEqual(self.text(".pm-card .pm-lab"), "PPO-clip")
        # a label the hub refuses goes back
        self.hub.delay[("PUT", "/label$")] = 0.6
        self.hub.fail[("PUT", "/label$")] = (400, {"error": "bad_label", "message": "a label is 1 to 40 characters"})
        self.press(".pm-card", "Edit label")
        self.wait("!!(document.activeElement && document.activeElement.closest('.pm-label'))", "the label field")
        self.js("document.activeElement.value = 'P'")
        self.press(".pm-card .pm-label", "Save")
        self.assertEqual(self.js(f"{M}.cur()._s.nodes[{M}.cur()._s.idx[{J(PPO)}]].label"), "P")
        self.wait(f"{M}.state().pending === 0", "refused")
        self.assertEqual(self.js(f"{M}.cur()._s.nodes[{M}.cur()._s.idx[{J(PPO)}]].label"), "PPO-clip")
        self.assertEqual(self.msg(), "Could not change the label: a label is 1 to 40 characters.")
        # Escape in the label field cancels the edit, and leaves the map open
        self.press(".pm-card", "Edit label")
        self.wait("!!(document.activeElement && document.activeElement.closest('.pm-label'))", "the label field")
        self.b.call("Input.dispatchKeyEvent", type="keyDown", key="Escape", code="Escape", windowsVirtualKeyCode=27)
        self.wait("!document.querySelector('#map .pm-card input')", "label edit cancelled")
        self.assertEqual(self.js("H.closed"), 0)
        self.assert_csrf()

    # ------------------------------------------------------------------ 11. locked
    def test_11_a_locked_graph_is_read_only_but_for_admins(self):
        self.open()
        self.js(f"document.querySelector('#map .pm-tab[data-id=\"{LOCKED}\"]').click()")
        self.wait(f"{M}.cur().id === {J(LOCKED)} && {M}.cur()._s.nodes.length === 3", "the locked graph")
        x, y = self.pos(PPO)
        self.click(x, y)
        self.wait("!document.querySelector('#map .pm-card').hidden", "card")
        self.assertIsNone(self.js("document.querySelector('#map .pm-card .pm-linkto')"))
        self.assertIsNone(self.js("document.querySelector('#map .pm-card .pm-rm')"))
        self.assertFalse(self.js(f"!!{self.button('.pm-card', 'Edit label')}"))
        self.assertIn("This graph is locked: only admins change it.", self.text(".pm-card"))
        # shift-click only selects
        x, y = self.pos(DPO)
        self.click(x, y, shift=True)
        self.wait(f"{M}.state().sel === {J(DPO)}", "selected")
        self.assertIsNone(self.state()["draft"])
        # a link: its grade shows, but cannot be changed or removed
        x, y = self.near(5, 3)
        self.click(x, y)
        self.wait(f"{M}.state().link === 5", "link")
        self.assertEqual(self.js("[...document.querySelectorAll('#map .pm-card .pm-segb')].map(b => b.disabled)"), [True, True, True])
        self.assertFalse(self.js(f"!!{self.button('.pm-card', 'Remove link')}"))
        self.open_panel("Graph settings")
        self.assertEqual(self.js("document.querySelectorAll('#map .pm-setdyn input').length"), 0)
        self.assertIn("Locked: only admins change this graph.", self.text(".pm-setdyn"))
        self.assertEqual(self.hub.edits(), [])
        # an admin: everything, and the lock itself
        self.open(me=ALICE)
        self.js(f"document.querySelector('#map .pm-tab[data-id=\"{LOCKED}\"]').click()")
        self.wait(f"{M}.cur().id === {J(LOCKED)} && {M}.cur()._s.nodes.length === 3", "the locked graph")
        self.js(f"{M}.select({J(PPO)})")
        self.assertIsNotNone(self.js("document.querySelector('#map .pm-card .pm-linkto')"))
        self.assertIn("as an admin you can still change it", self.text(".pm-card"))
        self.open_panel("Graph settings")
        self.assertTrue(self.js("document.querySelector('#map .pm-lockrow input').checked"))
        self.js("document.querySelector('#map .pm-lockrow input').click()")
        self.wait(f"!document.querySelector('#map .pm-tab[data-id=\"{LOCKED}\"] .pm-lock')", "unlocked")
        time.sleep(0.3)
        self.assertEqual(self.edits("PUT", r"^/api/graphs/"), [(f"/api/graphs/{LOCKED}", {"locked": False})])
        self.assertFalse(self.hub.s["graphs"][LOCKED]["locked"])
        self.assert_csrf()

    # ------------------------------------------------------------------ 12. live
    def test_12_live_events_and_a_refetch_on_show(self):
        self.open()
        self.assertEqual(self.js("H.subscribed()"), ["graph", "log", "paper"])
        # Bob links two papers somewhere else: the graph and log events bring it in
        with self.hub.lock:
            self.hub.s["links"][9] = {"id": 9, "src": TRPO, "dst": RLHF, "grade": "w", "origin": "human", "state": "active", "created_by": BOB, "created_at": "2026-09-28T00:00:00Z"}
            self.hub.add_log(BOB, "link.add", 9, None, {"id": 9, "src": TRPO, "dst": RLHF, "grade": "w", "state": "active"})
        self.js("H.emit('graph', {graph_id: null, op: 'link.add'}); H.emit('log', {id: 4})")
        self.wait(f"!!{M}.cur()._s.lid['9']", "the new link")
        self.wait("document.querySelector('#map .pm-ib[aria-label=Undo]').title === 'Undo: Bob added TRPO → RLHF (weak) · just now'", "the Undo knows")
        # the hub's layout moved a paper: it eases there, then rests
        with self.hub.lock:
            self.hub.s["layout"][RL][PPO] = (-40, -30)
        self.js(f"H.emit('graph', {{graph_id: {J(RL)}}})")
        self.wait(f"(n => n.x === -40 && n.y === -30)({M}.cur()._s.nodes[{M}.cur()._s.idx[{J(PPO)}]]) && !{M}.state().moving", "eased to the hub's place")
        time.sleep(0.3)
        self.assertFalse(self.state()["running"])
        # a page without events: show() asks the hub again
        self.open("sub=0")
        self.assertEqual(self.js("H.subscribed()"), [])
        with self.hub.lock:
            self.hub.s["links"][8]["state"] = "active"
        self.js("H.map.hide(); H.map.show()")
        self.wait(f"!!{M}.cur()._s.lid['8']", "refetched on show")
        # the page can show a paper on the map, in a graph it names
        self.js(f"H.map.select({J(FLOW)}, {J(GEN)})")
        self.wait(f"{M}.cur().id === {J(GEN)} && {M}.state().sel === {J(FLOW)}", "select(paper, graph)")
        self.assertEqual(self.text(".pm-card h3"), "Flow Matching for Generative Modeling")

    # ------------------------------------------------------------------ 13. colours
    def colours(self):
        px = lambda pid: self.js(f"{M}.pixel(...{M}.pos({J(pid)}))")
        rgb = lambda css: self.js("(() => { const e = document.createElement('i'); e.style.color = " + J(css) + "; document.body.append(e);"
                                  " const m = getComputedStyle(e).color.match(/\\d+/g).map(Number); e.remove(); return m.slice(0, 3); })()")
        var = lambda name: rgb(self.js(f"getComputedStyle(document.getElementById('map')).getPropertyValue('{name}').trim()"))
        close = lambda got, want: all(abs(g - w) <= 3 for g, w in zip(got, want))
        return px, var, close

    def test_13_colours_in_2d_and_webgl_light_and_dark(self):
        for query, webgl in (("webgl=0", False), ("", True)):
            with self.subTest(webgl=webgl):
                self.b.call("Emulation.setEmulatedMedia", features=[{"name": "prefers-color-scheme", "value": "light"}])
                self.open(query)
                if webgl and not self.js(f"{M}.webgl()"):
                    self.skipTest("no WebGL in this Chrome")
                self.assertEqual(self.js(f"{M}.webgl()"), webgl)
                px, var, close = self.colours()
                self.assertTrue(close(px(PPO), var("--pm-node")), f"not listened: grey, got {px(PPO)}")
                self.assertTrue(close(px(TRPO), var("--pm-start")), f"a place to start: the accent, got {px(TRPO)}")
                self.assertTrue(close(px(DPO), var("--pm-done")), f"listened: green, got {px(DPO)}")
                self.assertTrue(close(self.js(f"{M}.pixel(700, 800)"), var("--pm-bg")), "the background")
                # the viewer ticks PPO as listened (the page updates the papers, then changed())
                self.js(f"H.papers.get({J(PPO)}).listened = true; H.map.changed()")
                self.wait(f"(g => g[1] > g[0] + 40)({M}.pixel(...{M}.pos({J(PPO)})))", "PPO green")
                self.assertTrue(close(px(PPO), var("--pm-done")))
                # dark
                self.b.call("Emulation.setEmulatedMedia", features=[{"name": "prefers-color-scheme", "value": "dark"}])
                self.wait("getComputedStyle(document.getElementById('map')).getPropertyValue('--pm-node').trim().toUpperCase() === '#9C9C9C'", "dark tokens")
                self.wait(f"(g => g[0] < 40)({M}.pixel(700, 800))", "dark background drawn")
                self.assertTrue(close(px(INSTRUCT), var("--pm-node")), f"dark grey, got {px(INSTRUCT)}")
                self.assertTrue(close(px(TRPO), var("--pm-start")), f"dark accent, got {px(TRPO)}")
                self.assertTrue(close(self.js(f"{M}.pixel(700, 800)"), var("--pm-bg")))
                # a link picked is drawn in the text colour, over the others
                self.js(f"{M}.selectLink(1)")
                time.sleep(0.5)
                x, y = self.js(f"{M}.mid(1)")
                got, bg = self.js(f"{M}.pixel({x}, {y})"), var("--pm-bg")
                self.assertGreater(sum(got) - sum(bg), 150, f"the picked link is not lit: {got}")

    # ------------------------------------------------------------------ 14. the phone
    def test_14_phone_layout_and_tap_targets(self):
        self.phone()
        self.open()
        self.assertLessEqual(self.js("document.documentElement.scrollWidth"), 390)
        rects = self.js("['.pm-tabs', '.pm-q', '.pm-icons', '.pm-closebox'].map(s => (r => [r.left, r.top, r.right, r.bottom])(document.querySelector('#map ' + s).getBoundingClientRect()))")
        tabs, q, icons, close = rects
        self.assertLessEqual(tabs[3], q[1] + 0.5, "the tabs are not above the search")
        self.assertAlmostEqual(q[1], icons[1], delta=1, msg="the search and the icons share a row")
        self.assertLessEqual(icons[2], 390)
        self.assertLessEqual(close[3], q[1] + 0.5, "Close is on the tabs' row")
        self.assertGreater(tabs[2] - tabs[0], 290, "the tabs have the whole row but Close")
        self.assertGreaterEqual(close[0], 390 - 8 - 44 - 0.5, "Close is not at the right edge")
        self.assertEqual(self.js("[...document.querySelectorAll('#map .pm-ib')].filter(b => !b.closest('[hidden]')).map(b => (r => [r.width, r.height])(b.getBoundingClientRect()))"),
                         [[44, 44]] * 6)
        self.assertTrue(self.js("document.querySelector('#map [data-panel=start]').hidden"), "a panel open on the phone at first")
        self.assertTargets("the map")
        for title in ("Start here", "Undo", "History", "Graph settings"):
            self.open_panel(title)
            self.wait("[...document.querySelectorAll('#map .pm-panel')].some(p => !p.hidden) && !document.querySelector('#map .pm-panel:not([hidden])').textContent.includes('Loading')", title)
            self.assertTargets(title)
            self.open_panel(title)
        self.js("document.querySelector('#map .pm-tab-new').click()")
        self.wait("!document.querySelector('#map [data-panel=newg]').hidden", "new graph")
        self.assertTargets("New graph")
        self.press("[data-panel=newg]", "Cancel")
        # the paper, the link, the new link, the label field; a panel closes the card
        self.js(f"{M}.select({J(PPO)})")
        self.assertTargets("a paper")
        self.press(".pm-card", "Edit label")
        self.assertTargets("the label field")
        self.js(f"{M}.selectLink(2)")
        self.assertTargets("a link")
        self.js(f"{M}.select({J(DPO)})")
        self.press(".pm-card", "Link to…")
        x, y = self.pos(TRPO)
        self.tap(x, y)
        self.wait("document.querySelector('#map .pm-card').dataset.kind === 'draft' && !document.querySelector('#map .pm-card').hidden", "draft")
        self.assertTargets("a new link")
        self.open_panel("History")
        self.assertTrue(self.js("document.querySelector('#map .pm-card').hidden"), "the card stays under a panel")
        self.open_panel("History")
        # a message with its Undo
        self.js(f"{M}.selectLink(5)")
        self.press(".pm-card", "Remove link")
        self.wait("!document.querySelector('#map .pm-msg').hidden", "message")
        self.assertTargets("the message")
        self.assertLessEqual(self.js("document.documentElement.scrollWidth"), 390)

    # ------------------------------------------------------------------ 16. delete a graph
    def test_16_an_admin_deletes_a_graph_and_undoes_it(self):
        self.open()
        self.open_panel("Graph settings")
        self.assertFalse(self.js(f"!!{self.button('[data-panel=set]', 'Delete this graph')}"), "a viewer may delete a seeded graph")
        self.open(me=ALICE)
        self.js(f"document.querySelector('#map .pm-tab[data-id=\"{GEN}\"]').click()")
        self.wait(f"{M}.cur().id === {J(GEN)}", "the graph")
        self.open_panel("Graph settings")
        self.press("[data-panel=set]", "Delete this graph")
        self.assertIn("for everyone", self.text(".pm-delrow"))
        self.assertEqual(self.hub.edits(), [], "deleted without asking")
        self.press("[data-panel=set] .pm-delrow", "Delete")
        self.assertIsNone(self.js(f"document.querySelector('#map .pm-tab[data-id=\"{GEN}\"]')"), "the tab is still there")
        self.assertEqual(self.js(f"{M}.cur().id"), RL)
        self.wait("(m => !m.hidden && m.textContent.startsWith('Deleted'))(document.querySelector('#map .pm-msg'))", "said")
        self.assertEqual(self.edits("DELETE", r"^/api/graphs/"), [(f"/api/graphs/{GEN}", None)])
        time.sleep(0.5)
        self.assertIsNone(self.js(f"document.querySelector('#map .pm-tab[data-id=\"{GEN}\"]')"), "a list answer brought it back")
        self.js("document.querySelector('#map .pm-msg button').click()")
        self.wait(f"!!document.querySelector('#map .pm-tab[data-id=\"{GEN}\"]')", "the graph back")
        self.assertFalse(self.hub.s["graphs"][GEN]["deleted"])
        self.assert_csrf()

    # ------------------------------------------------------------------ 17. the last graph goes
    def test_17_the_last_graph_deleted_while_linking(self):
        with self.hub.lock:
            for gid in (GEN, LOCKED):
                self.hub.s["graphs"][gid]["deleted"] = True
        self.open(me=ALICE, tabs=2)
        self.js(f"{M}.select({J(PPO)})")
        self.press(".pm-card", "Link to…")
        self.wait("!document.querySelector('#map .pm-banner').hidden", "linking")
        self.js("(q => { q.value = 'DPO'; q.dispatchEvent(new Event('input')); })(document.querySelector('#map .pm-q'))")
        self.open_panel("Graph settings")
        self.press("[data-panel=set]", "Delete this graph")
        self.press("[data-panel=set] .pm-delrow", "Delete")
        self.wait("document.querySelector('#map .pm-empty p').textContent === 'No graphs yet: make one with + New graph.'", "no graphs")
        self.assertTrue(self.js("document.querySelector('#map .pm-banner').hidden"), "the banner outlived its graph")
        self.assertTrue(self.js("document.querySelector('#map .pm-card').hidden"))
        self.b.call("Input.dispatchKeyEvent", type="keyDown", key="Escape", code="Escape", windowsVirtualKeyCode=27)
        self.js("document.querySelector('#map .pm-q').focus()")
        self.b.call("Input.dispatchKeyEvent", type="keyDown", key="Enter", code="Enter", windowsVirtualKeyCode=13)
        self.wait("H.closed === 1", "Escape with nothing open closes the map")

    # ------------------------------------------------------------------ 15. an old answer
    def test_15_an_answer_older_than_an_edit_does_not_undo_it(self):
        """A refetch the hub answered before an edit landed, arriving after: dropped, asked again."""
        self.open()
        self.hub.lag[("GET", rf"^/api/graphs/{RL}$")] = 1.0
        self.js("H.map.refresh()")
        self.until(lambda: any(r[0] == "GET" and r[1] == f"/api/graphs/{RL}" for r in self.hub.requests[-6:]), "the refetch sent")
        time.sleep(0.1)
        self.js(f"{M}.selectLink(5)")
        self.press(".pm-card", "Remove link")
        self.wait(f"{M}.state().pending === 0", "removed")
        end = time.time() + 1.6
        while time.time() < end:
            self.assertFalse(self.has_link(5), "the old answer brought the link back")
            time.sleep(0.05)
        self.assertFalse(self.hub.active(5))


if __name__ == "__main__":
    unittest.main()
