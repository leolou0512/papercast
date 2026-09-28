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
  const map = document.getElementById('map');
  const root = map.querySelector('dialog[open]') || map;          // a modal dialog: only its controls take a tap
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
        """(path, body) of each edit, without the revision it names (test_19 checks those)."""
        strip = lambda b: {k: v for k, v in b.items() if k not in ("base_rev", "graph_id")} if isinstance(b, dict) else b
        return [(r[1], strip(r[2])) for r in self.hub.edits(method, pattern)]

    def revs_sent(self, method, pattern):
        """(graph_id, base_rev) each edit named, from its body or (a DELETE) its query."""
        out = []
        for r in self.hub.edits(method, pattern):
            b, q = r[2] if isinstance(r[2], dict) else {}, r[4]
            out.append((b.get("graph_id", q.get("graph_id")), None if b.get("base_rev", q.get("base_rev")) is None else int(b.get("base_rev", q.get("base_rev")))))
        return out

    def assert_csrf(self):
        for method, path, _, xpcg, _ in self.hub.edits():
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
        # the link as aimed: DPO first, so TRPO would build on it; TRPO is older, so the card says
        # so, holds the grades back and offers the swap
        self.assertEqual(self.text(".pm-card .pm-dir"), "TRPO builds on DPO")
        self.assertEqual(self.state()["draft"], {"src": DPO, "dst": TRPO})
        self.assertEqual(self.text(".pm-card .pm-warn"), "TRPO is older: the arrow would go the other way.")
        self.assertEqual(self.js("[...document.querySelectorAll('#map .pm-seg-add button')].map(b => b.disabled)"), [True, True, True])
        self.press(".pm-card", "Swap: DPO builds on TRPO")
        self.assertEqual(self.text(".pm-card .pm-dir"), "DPO builds on TRPO")
        self.assertEqual(self.state()["draft"], {"src": TRPO, "dst": DPO})
        self.assertIsNone(self.js("document.querySelector('#map .pm-card .pm-warn')"))
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
        # ...but not another edit that became my last one meanwhile (mine, in another window)
        self.js(f"{M}.selectLink(2)")
        self.press(".pm-card", "Remove link")
        self.wait("(m => !m.hidden && m.textContent.startsWith('Removed'))(document.querySelector('#map .pm-msg'))", "removed again")
        with self.hub.lock:
            self.hub.s["links"][11] = {"id": 11, "src": TRPO, "dst": RLHF, "grade": "w", "origin": "human", "state": "active", "created_by": LEO, "created_at": "2026-09-28T00:00:00Z"}
            self.hub.add_log(LEO, "link.add", 11, None, {"id": 11, "src": TRPO, "dst": RLHF, "grade": "w", "state": "active"})
        self.js("document.querySelector('#map .pm-msg button').click()")
        self.wait("!document.querySelector('#map [data-panel=undo]').hidden", "the Undo panel instead")
        self.assertEqual(self.msg(), "Your last edit is another one now: see Undo.")
        self.wait("(t => t && t.textContent.startsWith('Undo: You added TRPO → RLHF'))(document.querySelector('#map [data-panel=undo] .pm-undo[data-scope=mine] .pm-undo-t'))", "what it would undo")
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
        self.js(f"{M}.select({J(TRPO)})")
        x, y = self.pos(DPO)
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

    # ------------------------------------------------------------------ 6. undo and redo, both scopes
    def test_06_undo_my_last_edit_and_the_last_edit(self):
        self.open()
        self.wait("document.querySelector('#map .pm-ib[aria-label=Undo]').title.startsWith('Undo: Bob')", "the Undo button says what")
        self.assertEqual(self.js("document.querySelector('#map .pm-ib[aria-label=Undo]').title"), "Undo: Bob removed RLHF → DPO · 3 min ago")
        # nothing undone yet: nothing to redo, and the Redo button is greyed out
        self.assertEqual(self.js("(b => [b.disabled, b.title])(document.querySelector('#map .pm-ib[aria-label=Redo]'))"), [True, "Redo"])
        self.open_panel("Undo")
        self.wait("!!document.querySelector('#map [data-panel=undo] .pm-undo[data-scope=mine] .pm-undo-t')", "undo panel")
        undo_text = lambda scope: self.text(f"[data-panel=undo] .pm-undo[data-scope={scope}] .pm-undo-t")
        redo_text = lambda scope: self.text(f"[data-panel=redo] .pm-undo[data-scope={scope}] .pm-undo-t")
        self.assertEqual(undo_text("mine"), "Undo: You added TRPO → PPO (essential) · 2 h ago")
        self.assertEqual(undo_text("any"), "Undo: Bob removed RLHF → DPO · 3 min ago")
        # my last edit
        self.press("[data-panel=undo] .pm-undo[data-scope=mine]", "Undo")
        self.wait(f"!{M}.cur()._s.lid['1']", "TRPO → PPO undone on the map")
        self.assertEqual(self.edits("POST", "revert"), [("/api/graph-log/revert", {"scope": "mine", "expect": 1})])
        self.assertFalse(self.hub.active(1))
        self.assertEqual(self.msg(), "Undone: You added TRPO → PPO (essential).")
        self.wait("document.querySelector('#map [data-panel=undo] .pm-undo[data-scope=mine] .pm-undo-t').textContent === 'Nothing to undo.'", "nothing more of mine")
        self.assertEqual(undo_text("any"), "Undo: Bob removed RLHF → DPO · 3 min ago")
        # the undo can be redone: the Redo says what it brings back, for mine and for anyone's
        self.wait("!document.querySelector('#map .pm-ib[aria-label=Redo]').disabled", "Redo is on")
        self.assertEqual(self.js("document.querySelector('#map .pm-ib[aria-label=Redo]').title"), "Redo: You added TRPO → PPO (essential) · undone just now")
        self.open_panel("Redo")
        self.wait("!!document.querySelector('#map [data-panel=redo] .pm-undo[data-scope=any] .pm-undo-t')", "redo panel")
        self.assertEqual(self.texts("[data-panel=redo] .pm-undo-h"), ["My last undo", "The last undo, by anyone"])
        self.assertEqual(redo_text("mine"), "Redo: You added TRPO → PPO (essential) · undone just now")
        self.assertEqual(redo_text("any"), redo_text("mine"))
        self.press("[data-panel=redo] .pm-undo[data-scope=any]", "Redo")
        self.wait(f"!!{M}.cur()._s.lid['1']", "TRPO → PPO back")
        self.assertEqual(self.edits("POST", "revert")[1], ("/api/graph-log/revert", {"scope": "any", "expect": 4, "redo": True}))
        self.assertTrue(self.hub.active(1))
        self.assertEqual(self.msg(), "Redone: You added TRPO → PPO (essential).")
        self.wait("document.querySelector('#map [data-panel=redo] .pm-undo[data-scope=mine] .pm-undo-t').textContent === 'Nothing to redo.'", "nothing left to redo")
        self.assertEqual(redo_text("any"), "Nothing to redo.")
        # the redo is an edit of mine: the Undo takes it back
        self.open_panel("Undo")
        self.wait("document.querySelector('#map [data-panel=undo] .pm-undo[data-scope=mine] .pm-undo-t').textContent.startsWith('Undo: You redid')", "the redo to undo")
        self.assertEqual(undo_text("mine"), "Undo: You redid: You added TRPO → PPO (essential) · just now")
        self.wait("document.querySelector('#map .pm-ib[aria-label=Redo]').disabled", "Redo greyed out again")
        # a new change of mine after an undo leaves nothing to redo
        self.press("[data-panel=undo] .pm-undo[data-scope=mine]", "Undo")
        self.wait(f"!{M}.cur()._s.lid['1'] && !document.querySelector('#map .pm-ib[aria-label=Redo]').disabled", "undone, redo on")
        self.js(f"{M}.selectLink(5)")
        self.js("document.querySelector('#map .pm-card .pm-seg [data-grade=e]').click()")
        self.wait(f"{M}.state().pending === 0", "regraded")
        self.wait("document.querySelector('#map .pm-ib[aria-label=Redo]').disabled", "nothing to redo after a new change")
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
        self.assertIn("Locked", self.text(".pm-card"))
        # the card's byline shows (the toolbar row's hide-when-empty rule once matched it too)
        self.assertTrue(self.js("[...document.querySelectorAll('#map .pm-card .pm-sub')].every(e => getComputedStyle(e).display !== 'none')"))
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
        self.assertIn("Locked", self.text(".pm-setdyn"))
        self.assertEqual(self.hub.edits(), [])
        # an admin: everything, and the lock itself
        self.open(me=ALICE)
        self.js(f"document.querySelector('#map .pm-tab[data-id=\"{LOCKED}\"]').click()")
        self.wait(f"{M}.cur().id === {J(LOCKED)} && {M}.cur()._s.nodes.length === 3", "the locked graph")
        self.js(f"{M}.select({J(PPO)})")
        self.assertIsNotNone(self.js("document.querySelector('#map .pm-card .pm-linkto')"))
        self.assertIn("Locked", self.text(".pm-card"))
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
        rects = self.js("['.pm-tabs', '.pm-q', '.pm-icons', '.pm-closebox', '.pm-undobox'].map(s => (r => [r.left, r.top, r.right, r.bottom])(document.querySelector('#map ' + s).getBoundingClientRect()))")
        tabs, q, icons, close, undo = rects
        self.assertLessEqual(tabs[3], q[1] + 0.5, "the tabs are not above the search")
        self.assertAlmostEqual(q[1], icons[1], delta=1, msg="the search and the icons share a row")
        self.assertLessEqual(icons[2], 390)
        self.assertLessEqual(close[3], q[1] + 0.5, "Close is on the tabs' row")
        self.assertAlmostEqual(undo[1], close[1], delta=1, msg="Undo and Redo are on the tabs' row")
        self.assertLessEqual(undo[2], close[0], "Undo and Redo overlap Close")
        self.assertLessEqual(tabs[2], undo[0] + 0.5, "the tabs run under Undo")
        self.assertGreater(tabs[2] - tabs[0], 170, "the tabs have the row but Undo, Redo and Close")
        self.assertGreater(q[2] - q[0], 170, "the search is too narrow")
        self.assertGreaterEqual(close[0], 390 - 8 - 44 - 0.5, "Close is not at the right edge")
        self.assertEqual(self.js("[...document.querySelectorAll('#map .pm-ib')].filter(b => !b.closest('[hidden]')).map(b => (r => [r.width, r.height])(b.getBoundingClientRect()))"),
                         [[44, 44]] * 7)
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
    def dialog(self):
        return self.js("(d => d.open ? [d.querySelector('h2').textContent, d.querySelector('p').textContent,"
                       " [...d.querySelectorAll('button')].map(b => b.textContent), document.activeElement && document.activeElement.textContent] : null)"
                       "(document.querySelector('#map .pm-dialog'))")

    def test_16_an_admin_deletes_a_graph_and_undoes_it(self):
        self.open()
        self.open_panel("Graph settings")
        self.assertFalse(self.js(f"!!{self.button('[data-panel=set]', 'Delete this graph')}"), "a viewer may delete a seeded graph")
        self.open(me=ALICE)
        self.js(f"document.querySelector('#map .pm-tab[data-id=\"{GEN}\"]').click()")
        self.wait(f"{M}.cur().id === {J(GEN)}", "the graph")
        self.open_panel("Graph settings")
        asked = ["Delete the graph “Diffusion and generative models”?", "Its papers and links stay; only this graph goes. You can undo it.",
                 ["Cancel", "Delete"], "Cancel"]
        # Escape, a click outside and Cancel each keep it; the focus starts on Cancel and comes back
        for how in ("escape", "outside", "cancel"):
            self.press("[data-panel=set]", "Delete this graph")
            self.wait("document.querySelector('#map .pm-dialog').open", "the dialog")
            self.assertEqual(self.dialog(), asked, how)
            if how == "escape":
                self.b.call("Input.dispatchKeyEvent", type="keyDown", key="Escape", code="Escape", windowsVirtualKeyCode=27)
            elif how == "outside":
                self.click(12, 450)
            else:
                self.press(".pm-dialog", "Cancel")
            self.wait("!document.querySelector('#map .pm-dialog').open", f"closed by {how}")
            self.assertEqual(self.js("document.activeElement && document.activeElement.textContent"), "Delete this graph", how)
            self.assertEqual(self.hub.edits(), [], f"deleted after {how}")
            self.assertEqual(self.js("H.closed"), 0, f"{how} closed the map")
            self.assertEqual(self.js(f"{M}.cur().id"), GEN)
        # a click inside the box is not outside
        self.press("[data-panel=set]", "Delete this graph")
        self.wait("document.querySelector('#map .pm-dialog').open", "the dialog")
        r = self.js("(r => [r.left + 20, r.top + 12])(document.querySelector('#map .pm-dialog').getBoundingClientRect())")
        self.click(*r)
        self.assertTrue(self.js("document.querySelector('#map .pm-dialog').open"), "a click on the box closed it")
        self.press(".pm-dialog", "Delete")
        self.wait("!document.querySelector('#map .pm-dialog').open", "closed")
        self.assertIsNone(self.js(f"document.querySelector('#map .pm-tab[data-id=\"{GEN}\"]')"), "the tab is still there")
        self.assertEqual(self.js(f"{M}.cur().id"), RL)
        self.wait("(m => !m.hidden && m.textContent.startsWith('Deleted'))(document.querySelector('#map .pm-msg'))", "said")
        self.assertEqual(self.edits("DELETE", r"^/api/graphs/"), [(f"/api/graphs/{GEN}", None)])
        self.assertEqual(self.revs_sent("DELETE", r"^/api/graphs/"), [(GEN, 3)])
        time.sleep(0.5)
        self.assertIsNone(self.js(f"document.querySelector('#map .pm-tab[data-id=\"{GEN}\"]')"), "a list answer brought it back")
        self.js("document.querySelector('#map .pm-msg button').click()")
        self.wait(f"!!document.querySelector('#map .pm-tab[data-id=\"{GEN}\"]')", "the graph back")
        self.assertFalse(self.hub.s["graphs"][GEN]["deleted"])
        # its maker (not an admin) may delete it too; Bob, who did not make it, sees no Delete
        with self.hub.lock:
            self.hub.s["graphs"][GEN]["created_by"] = LEO
        self.open()
        self.js(f"document.querySelector('#map .pm-tab[data-id=\"{GEN}\"]').click()")
        self.wait(f"{M}.cur().id === {J(GEN)}", "the graph")
        self.open_panel("Graph settings")
        self.assertTrue(self.js(f"!!{self.button('[data-panel=set]', 'Delete this graph')}"), "its maker cannot delete it")
        self.open(me=BOB)
        self.js(f"document.querySelector('#map .pm-tab[data-id=\"{GEN}\"]').click()")
        self.wait(f"{M}.cur().id === {J(GEN)}", "the graph")
        self.open_panel("Graph settings")
        self.assertFalse(self.js(f"!!{self.button('[data-panel=set]', 'Delete this graph')}"), "Bob may delete Leo's graph")
        self.assert_csrf()

    def test_16b_the_delete_dialog_on_the_phone(self):
        self.phone()
        self.open(me=ALICE)
        self.open_panel("Graph settings")
        self.tap_button("[data-panel=set]", "Delete this graph")
        self.wait("document.querySelector('#map .pm-dialog').open", "the dialog")
        self.assertTargets("the delete dialog")
        r = self.js("(r => [r.left, r.right])(document.querySelector('#map .pm-dialog').getBoundingClientRect())")
        self.assertGreaterEqual(r[0], 15.5)
        self.assertLessEqual(r[1], 390 - 15.5)
        self.tap_button(".pm-dialog", "Cancel")
        self.wait("!document.querySelector('#map .pm-dialog').open", "closed")
        self.assertEqual(self.hub.edits(), [])

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
        self.press(".pm-dialog", "Delete")
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

    # ------------------------------------------------------------------ 18. aiming a new link
    def key(self, key, code, vk, mods=0, up=False):
        self.b.call("Input.dispatchKeyEvent", type="keyUp" if up else "keyDown", key=key, code=code, windowsVirtualKeyCode=vk, modifiers=mods)

    def move(self, x, y, mods=0):
        self.b.call("Input.dispatchMouseEvent", type="mouseMoved", x=x, y=y, modifiers=mods)

    def aim(self):
        return self.state()["aim"]

    def settled_aim(self, what, over=False, tip=None):
        """The aim once its tip has eased, and (the pointer's events come a frame late) once it is over
        `over` (a paper id or None) and at `tip` when those are given."""
        cond = "a && !a.easing"
        if over is not False:
            cond += f" && a.over === {J(over)}"
        if tip is not None:
            cond += f" && Math.abs(a.tip[0] - {tip[0]}) < 0.01 && Math.abs(a.tip[1] - {tip[1]}) < 0.01"
        return self.wait(f"(a => {cond} ? a : null)({M}.state().aim)", what)

    def arrow_colour(self, a, b):
        """The colour of the arrow halfway between two page points (the nearest to it of three pixels across)."""
        return [self.js(f"{M}.pixel({(a[0] + b[0]) / 2}, {(a[1] + b[1]) / 2 + d})") for d in (-1, 0, 1)]

    def test_18_aim_a_link(self):
        for query, webgl in (("webgl=0", False), ("", True)):
            with self.subTest(webgl=webgl):
                self.hub.stop()
                self.hub = FakeHub().start()
                self.open(query)
                if webgl and not self.js(f"{M}.webgl()"):
                    self.skipTest("no WebGL in this Chrome")
                px, var, close = self.colours()
                near = lambda got, want, other: min(sum(abs(g - w) for g, w in zip(c, want)) for c in got) < min(sum(abs(g - o) for g, o in zip(c, other)) for c in got)
                self.js(f"(s => {M}.view(1.5, innerWidth / 2 - 10 * 1.5, innerHeight / 2 - 20 * 1.5))({M}.cur()._s)")
                self.js(f"{M}.select({J(TRPO)})")
                self.press(".pm-card", "Link to…")
                self.wait("!document.querySelector('#map .pm-banner').hidden", "linking")
                self.assertIsNone(self.aim(), "an arrow before the pointer moved")
                # it appears at the pointer and follows it, in the accent, with no line by it yet
                sx, sy = self.pos(TRPO)
                self.move(sx + 120, sy + 150)
                a = self.settled_aim("the arrow", None, (sx + 120, sy + 150))
                self.assertEqual((a["from"], a["over"], a["tip"], a["warn"], a["text"]), (TRPO, None, [sx + 120, sy + 150], False, None))
                self.assertTrue(near(self.arrow_colour((sx, sy), (sx + 120, sy + 150)), var("--pm-start"), var("--pm-bg")), "the arrow is not drawn")
                self.move(sx + 60, sy + 260)
                self.assertEqual(self.settled_aim("followed", None, (sx + 60, sy + 260))["tip"], [sx + 60, sy + 260])
                # over a paper it snaps to its edge, rings it and says what the link would mean
                dx, dy = self.pos(DPO)
                self.move(dx + 2, dy + 1)
                a = self.settled_aim("snapped", DPO)
                self.assertEqual((a["over"], a["text"], a["warn"]), (DPO, "DPO builds on TRPO", False))
                self.assertEqual([round(v, 3) for v in a["tip"]], [round(dx, 3), round(dy, 3)])
                r = self.js(f"(n => (3.5 + 1.25 * Math.sqrt(n.deg)) * {M}.S.node * {M}.cur()._s.view.k)({M}.cur()._s.nodes[{M}.cur()._s.idx[{J(DPO)}]])")
                self.assertAlmostEqual(a["r"], r, places=3)
                tip = self.js("(r => [(r.left + r.right) / 2, r.bottom])(document.querySelector('#map .pm-aim').getBoundingClientRect())")
                self.assertLess(abs(tip[0] - dx), 1.5, "the line is not over the paper")
                self.assertLessEqual(tip[1], dy - r - 6, "the line covers the paper or its label")
                # off it again: back to the pointer
                self.move(sx + 120, sy + 150)
                self.assertIsNone(self.settled_aim("off the paper", None, (sx + 120, sy + 150))["over"])
                self.assertTrue(self.js("document.querySelector('#map .pm-aim').hidden"))
                # a click on the paper: the grade choice, the arrow as aimed
                self.click(dx, dy)
                self.wait("document.querySelector('#map .pm-card').dataset.kind === 'draft'", "the new-link card")
                self.assertEqual(self.state()["draft"], {"src": TRPO, "dst": DPO})
                self.assertIsNone(self.aim())
                self.assertEqual(self.js("[...document.querySelectorAll('#map .pm-seg-add button')].map(b => b.disabled)"), [False, False, False])
                # aimed back in time: the warning colour and words
                self.js(f"{M}.select({J(DPO)})")
                self.press(".pm-card", "Link to…")
                self.move(sx + 1, sy - 1)
                a = self.settled_aim("aimed at an older paper", TRPO)
                self.assertEqual((a["over"], a["warn"], a["text"]), (TRPO, True, "TRPO is older: the arrow would go the other way"))
                self.assertTrue(self.js("document.querySelector('#map .pm-aim').classList.contains('warn')"))
                self.assertTrue(near(self.arrow_colour((dx, dy), (sx, sy)), var("--pm-warn"), var("--pm-start")), "not in the warning colour")
                # already linked: says so
                ix, iy = self.pos(INSTRUCT)
                self.move(ix, iy)
                self.assertEqual(self.settled_aim("a linked paper", INSTRUCT)["text"], "Linked already: DPO builds on InstructGPT")
                # Escape cancels; so does a click on empty space
                self.key("Escape", "Escape", 27)
                self.wait(f"{M}.state().from === null && {M}.state().aim === null", "Escape cancelled")
                self.assertTrue(self.js("document.querySelector('#map .pm-aim').hidden"))
                self.assertEqual(self.js("H.closed"), 0)
                self.js(f"{M}.select({J(DPO)})")
                self.press(".pm-card", "Link to…")
                self.move(sx + 120, sy + 150)
                self.settled_aim("aiming", None, (sx + 120, sy + 150))
                self.click(sx + 120, sy + 150)
                self.wait(f"{M}.state().from === null && {M}.state().aim === null", "a click on nothing cancelled")
                self.assertEqual(self.state()["sel"], DPO)
                # a drag from a paper to another, and let go: the grade choice
                self.js(f"{M}.select({J(RLHF)})")
                self.press(".pm-card", "Link to…")
                rx, ry = self.pos(RLHF)
                self.b.call("Input.dispatchMouseEvent", type="mousePressed", x=rx, y=ry, button="left", clickCount=1)
                for i in range(1, 6):
                    self.b.call("Input.dispatchMouseEvent", type="mouseMoved", x=rx + (dx - rx) * i / 5, y=ry + (dy - ry) * i / 5, button="left", buttons=1)
                self.b.call("Input.dispatchMouseEvent", type="mouseReleased", x=dx, y=dy, button="left", clickCount=1)
                self.wait(f"document.querySelector('#map .pm-card').dataset.kind === 'draft'", "dragged to a paper")
                self.assertEqual(self.state()["draft"], {"src": RLHF, "dst": DPO})
                self.assertEqual(self.js(f"(n => [n.x, n.y])({M}.cur()._s.nodes[{M}.cur()._s.idx[{J(RLHF)}]])"), [-70, 120], "the drag moved the paper")
                # Shift with a paper picked: the arrow while it is held
                self.js(f"{M}.select({J(PPO)})")
                self.key("Shift", "ShiftLeft", 16, 8)
                self.move(sx + 120, sy + 150, 8)
                self.assertEqual(self.wait(f"(a => a && a.from === {J(PPO)} ? a : null)({M}.state().aim)", "shift aims")["from"], PPO)
                self.key("Shift", "ShiftLeft", 16, 0, up=True)
                self.wait(f"{M}.state().aim === null", "let go of Shift")
                self.assertEqual(self.hub.edits(), [])

    def test_18b_aim_a_link_on_the_phone(self):
        self.phone()
        self.open()
        touch = lambda kind, x=None, y=None: self.b.call("Input.dispatchTouchEvent", type=kind, touchPoints=[] if x is None else [{"x": x, "y": y}])
        self.js(f"{M}.select({J(RLHF)})")
        self.tap_button(".pm-card", "Link to…")
        self.wait("!document.querySelector('#map .pm-banner').hidden", "linking")
        rx, ry = self.pos(RLHF)
        dx, dy = self.pos(DPO)
        mx, my = (rx + dx) / 2 + 30, (ry + dy) / 2 + 60
        # the finger drags the arrow; over DPO it snaps, the line above the finger
        touch("touchStart", rx, ry)
        touch("touchMove", mx, my)
        a = self.settled_aim("the arrow follows the finger", None, (mx, my))
        self.assertEqual((a["from"], a["over"], [round(v, 2) for v in a["tip"]]), (RLHF, None, [round(mx, 2), round(my, 2)]))
        touch("touchMove", dx + 3, dy + 2)
        a = self.settled_aim("snapped", DPO)
        self.assertEqual((a["over"], a["text"]), (DPO, "DPO builds on RLHF"))
        r = self.js("(r => [r.bottom, r.left, r.right])(document.querySelector('#map .pm-aim').getBoundingClientRect())")
        self.assertLess(r[0], dy - 40, "the line is under the finger")
        touch("touchEnd")
        self.wait("document.querySelector('#map .pm-card').dataset.kind === 'draft' && !document.querySelector('#map .pm-card').hidden", "the grade choice")
        self.assertEqual(self.state()["draft"], {"src": RLHF, "dst": DPO})
        self.assertEqual(self.js(f"(n => [n.x, n.y])({M}.cur()._s.nodes[{M}.cur()._s.idx[{J(RLHF)}]])"), [-70, 120])
        # let go on nothing: still linking, no arrow; a tap on nothing: no link
        self.tap_button(".pm-card", "Cancel")
        self.js(f"{M}.select({J(RLHF)})")
        self.tap_button(".pm-card", "Link to…")
        self.wait("!document.querySelector('#map .pm-banner').hidden", "linking")
        touch("touchStart", rx, ry)
        touch("touchMove", mx, my)
        touch("touchEnd")
        self.wait(f"{M}.state().aim === null", "the arrow went")
        self.assertEqual(self.state()["from"], RLHF)
        self.tap(mx, my)
        self.wait(f"{M}.state().from === null", "a tap on nothing cancelled")
        self.assertEqual(self.hub.edits(), [])

    # ------------------------------------------------------------------ 19. every edit names its revision
    def test_19_every_edit_names_the_revision_it_was_made_on(self):
        self.open(me=ALICE)
        self.assertEqual(self.state()["rev"], 7)
        # two quick edits: the second leaves after the first is answered, with the revision it made
        self.hub.delay[("PUT", r"^/api/links/5$")] = 0.8
        self.js(f"{M}.selectLink(5)")
        self.js("document.querySelector('#map .pm-card .pm-seg [data-grade=e]').click()")
        self.js(f"{M}.selectLink(4)")
        self.press(".pm-card", "Remove link")
        self.wait(f"{M}.state().pending === 0", "both answered", 5)
        self.assertEqual(self.revs_sent("PUT", r"^/api/links/"), [(RL, 7)])
        self.assertEqual(self.revs_sent("DELETE", r"^/api/links/"), [(RL, 8)])
        self.assertEqual((self.hub.s["links"][5]["grade"], self.hub.active(4)), ("e", False))
        self.wait(f"{M}.state().rev === 9", "the map at the hub's revision")
        # the rest: a paper in and out, a label, a graph's name, the lock, an undo, a redo
        self.open_panel("Graph settings")
        self.js("(q => { q.value = 'Denoising'; q.dispatchEvent(new Event('input')); })(document.querySelector('#map .pm-addq'))")
        self.js("document.querySelector('#map [data-panel=set] .pm-addres button').click()")
        self.wait(f"{M}.state().pending === 0 && {M}.state().rev === 10", "added")
        self.js(f"{M}.select({J(DDPM)})")
        self.press(".pm-card", "Take out of this graph")
        self.wait(f"{M}.state().pending === 0 && {M}.state().rev === 11", "taken out")
        self.js(f"{M}.select({J(PPO)})")
        self.press(".pm-card", "Edit label")
        self.js("document.activeElement.value = 'PPO-2'")
        self.press(".pm-card .pm-label", "Save")
        self.wait(f"{M}.state().pending === 0 && {M}.state().rev === 12", "labelled")
        self.open_panel("Graph settings")
        self.js("document.querySelector('#map .pm-name').value = 'RL'")
        self.press("[data-panel=set]", "Rename")
        self.until(lambda: len(self.edits("PUT", r"^/api/graphs/")) == 1, "renamed")
        self.wait(f"{M}.state().rev === 13", "rev 13")
        self.js("document.querySelector('#map .pm-lockrow input').click()")
        self.until(lambda: len(self.edits("PUT", r"^/api/graphs/")) == 2, "locked")
        self.wait(f"{M}.state().rev === 14", "rev 14")
        self.js(f"H.map.event('log', {{}})")
        self.open_panel("Undo")
        self.wait("!!document.querySelector('#map [data-panel=undo] .pm-undo[data-scope=mine] button')", "undo panel")
        self.press("[data-panel=undo] .pm-undo[data-scope=mine]", "Undo")
        self.wait("!document.querySelector('#map .pm-ib[aria-label=Redo]').disabled", "undone")
        self.wait(f"{M}.state().rev === 15", "rev 15")
        self.open_panel("Redo")
        self.wait("!!document.querySelector('#map [data-panel=redo] .pm-undo[data-scope=mine] button')", "redo panel")
        self.press("[data-panel=redo] .pm-undo[data-scope=mine]", "Redo")
        self.wait(f"{M}.state().rev === 16", "redone")
        self.assertEqual(self.revs_sent("POST", r"/papers$"), [(RL, 9)])
        self.assertEqual(self.revs_sent("DELETE", r"/papers/"), [(RL, 10)])
        self.assertEqual(self.revs_sent("PUT", "/label$"), [(RL, 11)])
        self.assertEqual(self.revs_sent("PUT", r"^/api/graphs/"), [(RL, 12), (RL, 13)])
        self.assertEqual(self.revs_sent("POST", "revert"), [(RL, 14), (RL, 15)])
        self.assert_csrf()

    # ------------------------------------------------------------------ 20. a stale edit
    def test_20_an_edit_on_an_old_revision_is_refused_and_the_map_catches_up(self):
        self.http_errors_ok = True
        self.open()
        # Bob links two papers; this page did not hear of it (no event)
        nid = self.hub.change_as(BOB, "link", TRPO, RLHF, "w")
        self.assertFalse(self.has_link(nid))
        self.js(f"{M}.selectLink(5)")
        self.press(".pm-card", "Remove link")
        self.wait(f"{M}.state().pending === 0", "answered")
        self.wait(f"!!{M}.cur()._s.lid[{J(str(nid))}]", "brought up to date at once")
        self.assertTrue(self.has_link(5), "the refused removal is still shown")
        self.assertTrue(self.hub.active(5), "the hub removed it")
        self.wait("(m => !m.hidden && m.textContent.includes('up to date'))(document.querySelector('#map .pm-msg'))", "said")
        self.assertEqual(self.msg(), "Bob just changed this graph; it’s up to date now. Try again.")
        self.assertEqual(self.state()["rev"], 8)
        self.assertEqual(self.state()["live"], "Bob is editing this graph")
        # again, now on the revision the hub has: done
        self.js(f"{M}.selectLink(5)")
        self.press(".pm-card", "Remove link")
        self.wait(f"{M}.state().pending === 0", "answered")
        self.assertFalse(self.hub.active(5))
        self.assertEqual(self.revs_sent("DELETE", r"^/api/links/5"), [(RL, 7), (RL, 8)])
        # a rename meanwhile, and an edit someone already made: done, not refused
        self.hub.change_as(ALICE, "rename", RL, "RL by Alice")
        self.js(f"{M}.selectLink(1)")
        self.js("document.querySelector('#map .pm-card .pm-seg [data-grade=e]').click()")
        self.wait(f"{M}.state().pending === 0", "answered")
        self.assertEqual(self.revs_sent("PUT", r"^/api/links/1$"), [], "a regrade to its own grade was sent")
        self.js(f"{M}.selectLink(2)")
        self.js("document.querySelector('#map .pm-card .pm-seg [data-grade=w]').click()")
        self.wait(f"{M}.state().pending === 0", "answered")
        self.wait(f"document.querySelector('#map .pm-tab[data-id=\"{RL}\"]').textContent.startsWith('RL by Alice')", "the new name")
        self.assertEqual(self.msg(), "Alice just changed this graph; it’s up to date now. Try again.")
        self.assertEqual(self.hub.s["links"][2]["grade"], "s", "a stale regrade went through")
        # the regrade someone already made: done
        with self.hub.lock:
            self.hub.s["links"][2]["grade"] = "w"
            self.hub.bump([RL], ALICE)
        self.js("document.querySelector('#map .pm-msg').hidden = true")
        self.js(f"{M}.selectLink(2)")
        self.js("document.querySelector('#map .pm-card .pm-seg [data-grade=w]').click()")
        self.wait(f"{M}.state().pending === 0", "answered")
        self.wait(f"{M}.cur()._s.lid['2'].grade === 'w'", "the grade")
        self.assertIsNone(self.msg(), "an edit someone already made was refused")
        self.assertEqual(self.revs_sent("PUT", r"^/api/links/2$"), [(RL, 9), (RL, 10)])

    # ------------------------------------------------------------------ 21. keyboard
    def test_21_keyboard_undo_and_redo(self):
        self.open()
        self.wait("!document.querySelector('#map .pm-ib[aria-label=Undo]').disabled", "the log")
        self.key("z", "KeyZ", 90, 2)                        # Ctrl+Z: my last edit
        self.wait(f"!{M}.cur()._s.lid['1']", "undone")
        self.assertEqual(self.edits("POST", "revert"), [("/api/graph-log/revert", {"scope": "mine", "expect": 1})])
        self.wait("!document.querySelector('#map .pm-ib[aria-label=Redo]').disabled", "redo on")
        self.key("Z", "KeyZ", 90, 2 | 8)                    # Ctrl+Shift+Z: redo it
        self.wait(f"!!{M}.cur()._s.lid['1']", "redone")
        self.assertEqual(self.edits("POST", "revert")[1], ("/api/graph-log/revert", {"scope": "mine", "expect": 4, "redo": True}))
        self.wait("document.querySelector('#map .pm-ib[aria-label=Undo]').title.startsWith('Undo: You redid')", "the log again")
        self.key("z", "KeyZ", 90, 4)                        # Cmd+Z
        self.wait(f"!{M}.cur()._s.lid['1']", "undone by Cmd+Z")
        self.wait("!document.querySelector('#map .pm-ib[aria-label=Redo]').disabled", "redo on")
        self.key("y", "KeyY", 89, 2)                        # Ctrl+Y
        self.wait(f"!!{M}.cur()._s.lid['1']", "redone by Ctrl+Y")
        self.assertEqual(len(self.edits("POST", "revert")), 4)
        # not while typing: the search keeps its own undo
        self.js("document.querySelector('#map .pm-q').focus()")
        self.key("z", "KeyZ", 90, 2)
        self.key("y", "KeyY", 89, 2)
        time.sleep(0.4)
        self.assertEqual(len(self.edits("POST", "revert")), 4, "Ctrl+Z in the search undid an edit")
        self.js("document.activeElement.blur()")
        # a viewer's page that cannot edit ignores them
        self.open("editable=0", tabs=3)
        self.key("z", "KeyZ", 90, 2)
        time.sleep(0.4)
        self.assertEqual(len(self.edits("POST", "revert")), 4)
        self.assert_csrf()

    # ------------------------------------------------------------------ 22. live through event()
    def test_22_live_events_through_event_and_who_is_editing(self):
        self.open("sub=0")
        self.assertEqual(self.js("H.subscribed()"), [])
        nid = self.hub.change_as(BOB, "link", TRPO, RLHF, "w")
        self.js(f"H.map.event('graph', {{id: {J(RL)}, change: 'edit', log_id: 4, graph_rev: 8, by: {{id: {BOB}, name: 'Bob'}}, actor: 'human'}}); H.map.event('log', {{id: 4}})")
        self.wait(f"!!{M}.cur()._s.lid[{J(str(nid))}]", "the new link")
        self.assertEqual(self.state()["rev"], 8)
        self.assertEqual(self.js("(e => e.hidden ? null : e.textContent)(document.querySelector('#map .pm-live'))"), "Bob is editing this graph")
        self.wait("document.querySelector('#map .pm-ib[aria-label=Undo]').title.startsWith('Undo: Bob added TRPO → RLHF')", "the Undo knows")
        # our own edit's event, at the revision drawn: the graph is not fetched again
        self.js(f"{M}.selectLink(5)")
        self.press(".pm-card", "Remove link")
        self.wait(f"{M}.state().pending === 0 && {M}.state().rev === 9", "removed")
        time.sleep(0.5)
        n = len([r for r in self.hub.requests if r[0] == "GET" and r[1] == f"/api/graphs/{RL}"])
        self.js(f"H.map.event('graph', {{id: {J(RL)}, change: 'edit', graph_rev: 9, by: {{id: {LEO}, name: 'Leo'}}, actor: 'human'}})")
        time.sleep(0.6)
        self.assertEqual(len([r for r in self.hub.requests if r[0] == "GET" and r[1] == f"/api/graphs/{RL}"]), n)
        # a layout: the graph again; a resync: everything again
        with self.hub.lock:
            self.hub.s["layout"][RL][PPO] = (-40, -30)
        self.js(f"H.map.event('graph', {{id: {J(RL)}, change: 'layout', rev: 3}})")
        self.wait(f"(n => n.x === -40 && n.y === -30)({M}.cur()._s.nodes[{M}.cur()._s.idx[{J(PPO)}]])", "the new place")
        with self.hub.lock:
            self.hub.s["graphs"][GEN]["name"] = "Generative"
        self.js("H.map.event('resync', {})")
        self.wait(f"document.querySelector('#map .pm-tab[data-id=\"{GEN}\"]').textContent.startsWith('Generative')", "resynced")
        # a new paper (an upload) and a deleted episode: the graphs again
        with self.hub.lock:
            self.hub.s["graphs"][RL]["members"].append(FLOW)
            self.hub.s["graphs"][RL]["rev"] += 1
        self.js(f"H.map.event('paper', {{id: {J(FLOW)}, title: 'Flow Matching for Generative Modeling', new: true}})")
        self.wait(f"{M}.cur()._s.idx[{J(FLOW)}] != null", "the paper that joined")
        with self.hub.lock:
            self.hub.s["graphs"][RL]["members"].remove(FLOW)
            self.hub.s["graphs"][RL]["rev"] += 1
        self.js(f"H.map.event('episode', {{id: 'e_x', paper_id: {J(FLOW)}, deleted: true}})")
        self.wait(f"{M}.cur()._s.idx[{J(FLOW)}] == null", "the paper that left")

    # ------------------------------------------------------------------ 23. suggestions
    def along(self, a, b, n=16):
        """n page points along the middle of the line between two papers."""
        (ax, ay), (bx, by) = self.pos(a), self.pos(b)
        return [(ax + (bx - ax) * t, ay + (by - ay) * t) for t in [0.3 + 0.4 * i / (n - 1) for i in range(n)]]

    def test_23_suggestions_shown_on_request_accepted_and_dismissed(self):
        self.open("webgl=0")
        px, var, close = self.colours()
        muted, bg = var("--pm-muted"), var("--pm-bg")
        ink = lambda x, y: sum(abs(g - w) for g, w in zip(self.js(f"{M}.pixel({x}, {y})"), bg)) > 24
        dashes = lambda: sum(1 for x, y in self.along(TRPO, RLHF) if ink(x, y))
        # asked for, not shown: the button says how many
        self.assertEqual(self.text(".pm-suggb"), "Show suggestions (2)")
        self.assertEqual(self.state()["sugg"], {"shown": False, "n": 2, "sel": None, "mode": "suggest", "open": 3})
        self.assertEqual(dashes(), 0, "a suggestion drawn before it was asked for")
        mx, my = self.along(TRPO, RLHF, 3)[1]
        self.click(mx, my)
        self.assertIsNone(self.card(), "a hidden suggestion was picked")
        self.js("document.querySelector('#map .pm-suggb').click()")
        self.assertEqual((self.text(".pm-suggb"), self.state()["sugg"]["shown"]), ("Hide suggestions (2)", True))
        self.assertGreaterEqual(dashes(), 4, "not drawn as a dashed line")
        self.assertLessEqual(dashes(), 13, "not dashed")
        # hover and pick one: what it would be, whose upload found it, Accept and Dismiss
        self.b.call("Input.dispatchMouseEvent", type="mouseMoved", x=mx + 2, y=my)
        self.wait("document.querySelector('#map .pm-tip-t').textContent === 'RLHF builds on TRPO'", "hover")
        self.assertEqual(self.text(".pm-tip-s"), "Suggested · weak")
        self.click(mx + 2, my)
        self.wait("document.querySelector('#map .pm-card').dataset.kind === 'suggestion' && !document.querySelector('#map .pm-card').hidden", "the card")
        self.assertEqual(self.text(".pm-card h3"), "TRPO → RLHF")
        self.assertEqual(self.text(".pm-card .pm-dir"), "RLHF builds on TRPO")
        self.assertEqual(self.text(".pm-card .pm-by"), "Suggested, weak: found in Bob’s upload · 1 d ago")
        self.assertEqual(self.texts(".pm-card .pm-act button"), ["Accept", "Dismiss"])
        # accept: a link at once, set right by the hub; the person's edit, with the revision
        self.hub.delay[("POST", r"/accept$")] = 0.8
        self.press(".pm-card", "Accept")
        got = self.link_between(TRPO, RLHF)
        self.assertTrue(got and got["pending"], "not drawn at once")
        self.assertEqual(self.state()["sugg"]["n"], 1)
        self.wait(f"{M}.state().pending === 0", "accepted", 5)
        self.assertEqual(self.link_between(TRPO, RLHF), {"id": 9, "grade": "w", "pending": False})
        self.assertEqual(self.edits("POST", "/accept$"), [("/api/link-suggestions/1/accept", {})])
        self.assertEqual(self.revs_sent("POST", "/accept$"), [(RL, 7)])
        self.assertEqual(self.msg(), "Accepted: RLHF builds on TRPO.")
        self.assertEqual(self.text(".pm-suggb"), "Hide suggestions (1)")
        self.wait("document.querySelector('#map .pm-ib[aria-label=Undo]').title.startsWith('Undo: You added TRPO → RLHF (weak)')", "it is my edit")
        # dismiss the other: gone for good
        x2, y2 = self.along(TRPO, INSTRUCT, 3)[1]
        self.click(x2, y2)
        self.wait("document.querySelector('#map .pm-card').dataset.kind === 'suggestion' && document.querySelector('#map .pm-card h3').textContent === 'TRPO → InstructGPT'", "the other")
        self.assertEqual(self.text(".pm-card .pm-by"), "Suggested, strong: found in Alice’s upload · 5 h ago")
        self.press(".pm-card", "Dismiss")
        self.assertEqual(self.state()["sugg"]["n"], 0, "not gone at once")
        self.wait(f"{M}.state().pending === 0", "dismissed")
        self.assertEqual(self.edits("POST", "/dismiss$"), [("/api/link-suggestions/2/dismiss", {})])
        self.assertEqual(self.hub.s["suggestions"][2]["state"], "dismissed")
        self.assertEqual(self.msg(), "Dismissed TRPO → InstructGPT: it will not be suggested again.")
        self.assertTrue(self.js("document.querySelector('#map .pm-suggb').hidden"), "the button stays with nothing to show")
        # the setting: for a viewer, what it is; no Accept all
        self.open_panel("Graph settings")
        self.wait("!!document.querySelector('#map .pm-setsite h2')", "the setting")
        self.assertEqual(self.texts(".pm-setsite h2"), ["Links from uploads"])
        self.assertEqual(self.text(".pm-setsite .pm-note"), "Links from uploads: Suggest only")
        self.assertEqual(self.js("document.querySelectorAll('#map .pm-setsite button').length"), 0)
        # someone's upload found another: the page hears of it
        with self.hub.lock:
            self.hub.s["suggestions"][4] = {"id": 4, "src": PPO, "dst": RLHF, "grade": "e", "user_id": BOB, "created_at": "2026-09-28T10:00:00Z", "state": "open"}
        self.js(f"H.emit('graph', {{id: {J(RL)}, change: 'suggestions'}})")
        self.wait("!document.querySelector('#map .pm-suggb').hidden && document.querySelector('#map .pm-suggb').textContent === 'Show suggestions (1)'", "the new one")
        self.assert_csrf()

    def test_23b_the_setting_and_accept_all_are_an_admins(self):
        self.open(me=ALICE)
        self.open_panel("Graph settings")
        self.wait("!!document.querySelector('#map .pm-modes')", "the switch")
        pressed = lambda: self.js("[...document.querySelectorAll('#map .pm-modes button')].map(b => [b.textContent, b.getAttribute('aria-pressed')])")
        self.assertEqual(pressed(), [["Automatic", "false"], ["Suggest only", "true"]])
        self.assertFalse(self.js("!!document.querySelector('#map .pm-setsite .pm-note')"))     # no sentence under the switch
        self.press("[data-panel=set] .pm-modes", "Automatic")
        self.assertEqual(pressed(), [["Automatic", "true"], ["Suggest only", "false"]])
        self.until(lambda: self.hub.s["agent_links"] == "auto", "saved")
        self.assertEqual(self.edits("PUT", "graph-settings"), [("/api/graph-settings", {"agent_links": "auto"})])
        self.wait("(m => !m.hidden)(document.querySelector('#map .pm-msg'))", "said")
        self.assertEqual(self.msg(), "Links from uploads are added by themselves now.")
        # switching back does not add the old suggestions; Accept all does
        self.assertEqual(self.state()["sugg"]["n"], 2)
        self.press("[data-panel=set]", "Accept all 3 suggestions")
        self.wait(f"{M}.state().sugg.n === 0 && !!{M}.cur()._s.lid['9'] && !!{M}.cur()._s.lid['10']", "accepted, in this graph")
        self.assertEqual(self.msg(), "Accepted 3 suggested links.")
        self.assertEqual(self.edits("POST", "accept-all"), [("/api/link-suggestions/accept-all", {})])
        self.wait("!document.querySelector('#map .pm-acceptall')", "nothing left to accept")
        self.js(f"document.querySelector('#map .pm-tab[data-id=\"{GEN}\"]').click()")
        self.wait(f"{M}.cur().id === {J(GEN)} && {M}.cur()._s.links.some(l => l.e.src === {J(DDPM)} && l.e.dst === {J(FLOW)})", "in the other graph too")
        self.assert_csrf()

    def test_23c_suggestions_on_the_phone(self):
        self.phone()
        self.open()
        self.tap_button(".pm-sub", "Show suggestions (2)")
        self.wait(f"{M}.state().sugg.shown", "shown")
        self.assertTargets("the map with suggestions")
        top = self.js("document.querySelector('#map .pm-sub').getBoundingClientRect().bottom")
        self.open_panel("Start here")
        self.assertGreaterEqual(self.js("document.querySelector('#map [data-panel=start]').getBoundingClientRect().top"), top - 0.5, "the panel covers the row")
        self.open_panel("Start here")
        x, y = self.along(TRPO, RLHF, 3)[1]
        self.tap(x, y)
        self.wait("document.querySelector('#map .pm-card').dataset.kind === 'suggestion' && !document.querySelector('#map .pm-card').hidden", "the card")
        self.assertTargets("a suggestion's card")
        self.tap_button(".pm-card", "Dismiss")
        self.wait(f"{M}.state().pending === 0 && {M}.state().sugg.n === 1", "dismissed")

    # ------------------------------------------------------------------ 24. a read that overtakes an edit's answer
    def test_24_a_read_overtaking_my_edit_does_not_make_my_next_edit_stale(self):
        self.http_errors_ok = True
        self.open()
        # my regrade lands on the hub at once, its answer comes late; a read overtakes it
        self.hub.lag[("PUT", r"^/api/links/5$")] = 1.2
        self.js(f"{M}.selectLink(5)")
        self.js("document.querySelector('#map .pm-card .pm-seg [data-grade=e]').click()")
        self.until(lambda: self.hub.s["links"][5]["grade"] == "e", "the regrade on the hub")
        self.js("H.map.refresh()")
        self.wait(f"{M}.state().rev === 8 && {M}.state().pending === 1", "the read, before the answer")
        # my next edit, made now, goes after the answer: with the revision the regrade made
        self.js(f"{M}.selectLink(4)")
        self.press(".pm-card", "Remove link")
        self.wait(f"{M}.state().pending === 0", "both answered", 5)
        self.assertFalse(self.hub.active(4), "my own quick edit was refused")
        self.assertEqual(self.revs_sent("DELETE", r"^/api/links/4"), [(RL, 8)])
        self.assertIsNone(self.msg() if (self.msg() or "").endswith("Try again.") else None)
        # the same, with Bob's change in between: an edit made before the read goes as it was made
        self.hub.lag[("PUT", r"^/api/links/1$")] = 1.2
        self.js(f"{M}.selectLink(1)")
        self.js("document.querySelector('#map .pm-card .pm-seg [data-grade=w]').click()")
        self.until(lambda: self.hub.s["links"][1]["grade"] == "w", "the regrade on the hub")
        self.js(f"{M}.selectLink(3)")
        self.js("document.querySelector('#map .pm-card .pm-seg [data-grade=w]').click()")        # made on revision 9
        self.hub.change_as(BOB, "rename", RL, "RL by Bob")                                        # revision 11
        self.js("H.map.refresh()")
        self.wait(f"{M}.state().rev === 11", "the read with Bob's change")
        self.wait(f"{M}.state().pending === 0", "answered", 5)
        self.assertEqual(self.revs_sent("PUT", r"^/api/links/3$"), [(RL, 9)])          # as it was made: refused
        self.assertEqual(self.hub.s["links"][3]["grade"], "e", "an edit made before Bob's change went through")
        self.wait("(m => !m.hidden && m.textContent.startsWith('Bob just changed'))(document.querySelector('#map .pm-msg'))", "said")


if __name__ == "__main__":
    unittest.main()
