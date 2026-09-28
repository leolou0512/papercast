#!/usr/bin/env python3
"""The first-sign-in tour, Tour again and the "?" help (hub/tour.py, static/tour.js), against the
real hub: the auth stand-in (web_rig.py, X-Test-User) for the page, PCG_AUTH=password (as
test_accounts.py drives it) for the order of a first sign-in, and headless Chrome (web_cdp.py).

    cd stacks/papercast-group/hub && python3 -m unittest tests.test_tour -v

What is checked: the hub keeps each person's tour (new, started, finished, skipped; someone who used
the site before the tour existed is "old"), and Tour again for three days after the first time it
saw them, by its own clock; a first sign-in changes the password first, then the tour starts on
the library; each step's hole sits on its real control (the control's box against the hole's),
follows it on a scroll and a resize, and an arrow points at it; a step whose control is not
there is left out (no transcript, locked graphs, an empty library); Back, Next, the arrow keys,
Escape, and the keyboard kept in the tour; Skip ends it for good, on a second browser too; the
page is put back as it was; reduced motion; the help panel and its copy buttons, a viewer's one
line; 44 px taps on a phone; no console error or CSP refusal. Fake papers and people only.

The browser parts skip when no headless Chrome or no `websocket-client` is available."""
from __future__ import annotations

import json
import re
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from web_rig import Rig  # noqa: E402

from hub import db, graph, tour  # noqa: E402

try:
    import websocket  # noqa: F401
    from web_cdp import Browser, find_chrome
    SKIP = None if find_chrome() else "no headless Chrome"
except ImportError:
    SKIP = "websocket-client not installed"

A, NIA, VIC = "alice@example.org", "nia@example.org", "vic@example.org"
PUBLIC = "https://papercast.example"
SCRIPT = ("# The idea\n\nA fake model learns to undo noise one small step at a time. Each step is a small denoiser.\n\n"
          "A second paragraph says a little more. It has two sentences.\n")
STEPS = ["search", "play", "listened", "upnext", "transcript", "comments", "map", "link", "settings", "help"]
# the control each step must spotlight, found on the page independently of the tour
TARGETS = {
    "search": "document.querySelector('#list-pane .search')",
    "play": "document.getElementById('p-play')",
    "listened": "document.getElementById('w-listened')",
    "upnext": "[...document.querySelectorAll('.menu [role=menuitem]')].find(b => b.textContent === 'Add to Up next')",
    "transcript": "document.querySelector('#tr-body .tr-p .tr-s')",
    "comments": "document.getElementById('c-text')",
    "map": "document.getElementById('map-btn')",
    "link": "document.querySelector('#map .pm-card .pm-linkto')",
    "settings": "document.getElementById('set-btn')",
    "help": "document.getElementById('help-btn')",
}
# The control's box, the hole's, the words', the arrow's tip, and where the keyboard is.
GEOM = r"""((t) => {
  const s = PaperTour.current.state();
  if (!t) return JSON.stringify({err: 'the control is not on the page'});
  const b = (e) => { const r = e.getBoundingClientRect(); return [r.left, r.top, r.right, r.bottom]; };
  const d = document.querySelector('#tour .tour-arrow path').getAttribute('d') || '';
  const m = /L(-?[\d.]+) (-?[\d.]+)$/.exec(d);
  return JSON.stringify({r: b(t), h: b(document.querySelector('#tour .tour-hole')), c: b(document.getElementById('tour-card')),
    tip: m ? [+m[1], +m[2]] : null, focus: !!(document.activeElement && document.activeElement.closest('#tour')),
    text: s.text, i: s.i, n: s.n, vw: innerWidth, vh: innerHeight,
    shown: !t.closest('[hidden]') && getComputedStyle(t).visibility === 'visible'});
})(%s)"""
# test_page.py's check (Leo's TAP_TARGETS) limited to one part of the page: every control in it at
# least 44 x 44 px, and a tap on its middle reaching it.
TAPS_IN = r"""((root) => {
  const q = 'a[href], button, input:not([type=hidden]), select, textarea, [role=button], [tabindex]:not([tabindex="-1"])';
  const name = (e) => `${e.tagName.toLowerCase()}${e.id ? '#' + e.id : ''} "${(e.getAttribute('aria-label') || e.textContent || '').trim().slice(0, 30)}"`;
  const bad = [];
  for (const e of root.querySelectorAll(q)) {
    const cs = getComputedStyle(e);
    if (e.closest('[hidden]') || cs.visibility !== 'visible' || !e.getClientRects().length) continue;
    const r = e.getBoundingClientRect();
    if (r.width < 43.5 || r.height < 43.5) { bad.push(`${name(e)} is ${r.width.toFixed(1)} x ${r.height.toFixed(1)}`); continue; }
    const at = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
    if (!at || !(at === e || e.contains(at))) bad.push(`${name(e)} is covered by ${at ? at.tagName.toLowerCase() + (at.id ? '#' + at.id : '') : 'nothing'}`);
  }
  return JSON.stringify(bad);
})(%s)"""
KEYS = {"ArrowRight": 39, "ArrowLeft": 37, "Escape": 27, "Tab": 9, "Enter": 13, "/": 191}


def new_user(email, name, role):
    """An account made just now (web_rig's people are a month old: they were here before the tour)."""
    c = db.conn()
    c.execute("INSERT INTO users(email, name, role, disabled, created_at) VALUES (?, ?, ?, 0, ?)", (email, name, role, db.now()))
    return c.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()[0]


def fill(r, by, n=3, script=True):
    """n fake papers in the diffusion graph, each with a voiced version (and its script)."""
    pids = []
    for i in range(n):
        p = r.paper(f"A Fake Paper About Denoising n{i}", by, tags=["diffusion"])
        e = r.episode(p, by, duration=60, audio_s=5)
        if script:
            (r.cfg.episodes / e / "script.md").write_text(SCRIPT)
        pids.append(p)
    return pids


# ============================================================================ the hub's side

class TourState(unittest.TestCase):
    """GET/PUT /api/tour: per person, on the hub, by the hub's clock."""

    @classmethod
    def setUpClass(cls):
        cls.r = Rig()
        cls.alice = cls.r.user(A, "Alice", "admin")          # made a month ago
        cls.nia = new_user(NIA, "Nia", "contributor")

    @classmethod
    def tearDownClass(cls):
        cls.r.close()

    def get(self, who):
        s, j, _ = self.r.req("GET", "/api/tour", user=who)
        self.assertEqual(s, 200, j)
        return j

    def put(self, who, state, code=200):
        s, j, _ = self.r.req("PUT", "/api/tour", {"state": state}, user=who)
        self.assertEqual(s, code, j)
        return j

    def test_someone_new_gets_it_until_they_finish_or_skip(self):
        db.conn().execute("DELETE FROM tour")
        t0 = time.time()
        j = self.get(NIA)
        self.assertEqual((j["state"], j["auto"], j["again"]), ("new", True, True))
        self.assertAlmostEqual(j["again_left_s"], tour.AGAIN_S, delta=5)
        self.assertAlmostEqual(tour._unix(j["again_until"]) - t0, 3 * 86400, delta=5)
        first = j["first_seen"]
        self.assertEqual(self.put(NIA, "started")["state"], "started")
        j = self.get(NIA)
        self.assertEqual((j["state"], j["auto"], j["first_seen"]), ("started", True, first))    # an interrupted tour comes back
        j = self.put(NIA, "skipped")
        self.assertEqual((j["state"], j["auto"], j["again"]), ("skipped", False, True))
        # a replay that starts does not undo the skip; its end is kept
        self.assertEqual(self.put(NIA, "started")["state"], "skipped")
        self.assertEqual(self.put(NIA, "finished")["state"], "finished")
        self.assertEqual(self.get(NIA)["first_seen"], first)
        row = self.r.q("SELECT * FROM tour WHERE user_id = ?", self.nia)[0]
        self.assertIsNotNone(row["started_at"])
        self.assertIsNotNone(row["ended_at"])

    def test_someone_who_was_here_before_gets_neither(self):
        j = self.get(A)
        self.assertEqual((j["state"], j["auto"], j["again"], j["again_until"]), ("old", False, False, None))
        self.assertEqual(self.put(A, "finished")["state"], "old")

    def test_a_first_password_chosen_now_is_a_first_sign_in(self):
        """Password sign-in: an admin may add someone days before they come; their first sign-in
        (their own password) is when their account begins."""
        uid = self.r.user("pat@example.org", "Pat", "contributor")         # a month old
        db.conn().execute("UPDATE users SET pw_set_at = ? WHERE id = ?", (db.now(), uid))
        self.assertEqual(self.get("pat@example.org")["state"], "new")

    def test_tour_again_for_three_days_by_the_hubs_clock(self):
        new_user("tim@example.org", "Tim", "viewer")
        t0 = time.time()
        with mock.patch.object(tour, "_now", lambda: t0):
            self.assertTrue(self.get("tim@example.org")["again"])
        with mock.patch.object(tour, "_now", lambda: t0 + 3 * 86400 - 60):
            j = self.get("tim@example.org")
            self.assertEqual((j["again"], j["auto"]), (True, True))
            self.assertLessEqual(j["again_left_s"], 60)
        with mock.patch.object(tour, "_now", lambda: t0 + 3 * 86400 + 1):
            j = self.get("tim@example.org")
            self.assertEqual((j["again"], j["auto"], j["again_left_s"]), (False, False, 0))

    def test_refusals(self):
        self.put(NIA, "done", 400)
        self.put(NIA, None, 400)
        s, j, _ = self.r.req("PUT", "/api/tour", {"state": "skipped"}, user=NIA, headers={"X-PCG": None})
        self.assertEqual(s, 403)
        self.assertEqual(self.r.req("GET", "/api/tour", user=None)[0], 401)


# ============================================================================ the page

class PageBase(unittest.TestCase):
    """One hub and one browser per class; no test may leave an error in the console."""

    @classmethod
    def setUpClass(cls):
        cls.r = Rig()
        cls.r.cfg.public_url = PUBLIC
        cls.alice = cls.r.user(A, "Alice", "admin")
        cls.fill()
        cls.b = Browser()
        cls.b.call("Log.enable")
        cls.base = cls.r.base
        # the page's origin is the rig's, not PUBLIC: the auth stand-in does not check it
        cls.b.viewport(1280, 820)

    @classmethod
    def fill(cls):
        pass

    @classmethod
    def tearDownClass(cls):
        cls.b.close()
        cls.r.close()

    def setUp(self):
        self.allow = []
        self.b.viewport(1280, 820)
        self.b.call("Emulation.setEmulatedMedia", features=[{"name": "prefers-reduced-motion", "value": ""}])
        self.b.pump(0.05)
        self.b.events.clear()

    def tearDown(self):
        self.b.pump(0.3)
        errs = []
        for m in self.b.events:
            meth, p = m.get("method"), m.get("params", {})
            if meth == "Runtime.exceptionThrown":
                d = p.get("exceptionDetails", {})
                errs.append(f"exception: {(d.get('exception') or {}).get('description') or d.get('text')}")
            elif meth == "Runtime.consoleAPICalled" and p.get("type") in ("error", "assert"):
                errs.append("console.error: " + " ".join(str(a.get("value", a.get("description", ""))) for a in p.get("args", [])))
            elif meth == "Log.entryAdded" and p.get("entry", {}).get("level") == "error":
                e = p["entry"]
                errs.append(f"log ({e.get('source')}): {e.get('text')} {e.get('url', '')}")
        errs = [e for e in errs if not any(re.search(a, e) for a in self.allow)]
        self.b.events.clear()
        self.assertEqual(errs, [], "errors in the console")

    # -- helpers
    def js(self, expr):
        return self.b.js(expr)

    def wait(self, expr, what, timeout=10):
        return self.b.wait_js(expr, timeout, what)

    def as_user(self, email):
        self.b.call("Network.setExtraHTTPHeaders", headers={"X-Test-User": email})

    def fresh(self, uid):
        """As if the hub had never seen this person on the page."""
        db.conn().execute("DELETE FROM tour WHERE user_id = ?", (uid,))

    def home(self, email, clear=True):
        self.as_user(email)
        self.b.goto("about:blank")
        self.b.goto(self.base + "/")
        if clear:
            self.js("localStorage.clear(); sessionStorage.clear()")
            self.b.goto(self.base + "/")
        self.wait("!!window.PaperTour && !!PaperTour.current && document.title === 'Papers'", "the page started")

    def key(self, k, shift=False):
        kw = {"key": k, "code": {"/": "Slash"}.get(k, k), "windowsVirtualKeyCode": KEYS[k], "modifiers": 8 if shift else 0}
        self.b.call("Input.dispatchKeyEvent", type="keyDown", **kw, **({"text": k} if k == "/" else {}))
        self.b.call("Input.dispatchKeyEvent", type="keyUp", **kw)

    def state(self):
        return self.js("PaperTour.current.state()")

    def settle(self, sid=None, timeout=12):
        """The tour at a step (that one, if named), not busy, its spotlight at rest."""
        want = f" && s.id === {json.dumps(sid)}" if sid else ""
        return json.loads(self.wait(f"(() => {{ const s = PaperTour.current.state(); return s.on && !s.busy && !s.gliding{want} && JSON.stringify(s); }})()",
                                    f"the tour at {sid or 'a step'}", timeout))

    def geom(self, target):
        g = json.loads(self.js(GEOM % TARGETS[target]))
        self.assertNotIn("err", g, target)
        return g

    def assertSpotlight(self, step, target=None, where=""):
        """The hole round the real control, 6 px all round; the words clear of it; the arrow's tip at
        the hole; the control on screen; the keyboard in the tour."""
        target = target or step
        time.sleep(0.05)
        g = self.geom(target)
        r, h, c = g["r"], g["h"], g["c"]
        for k, (got, want) in enumerate(zip(h, [r[0] - 6, r[1] - 6, r[2] + 6, r[3] + 6])):
            self.assertAlmostEqual(got, want, delta=1.01, msg=f"{where} {step}: hole edge {k} {h} against the control {r}")
        self.assertTrue(g["shown"], f"{where} {step}: the control is hidden")
        self.assertGreaterEqual(r[1], 0, f"{where} {step}: above the screen")
        self.assertLessEqual(r[3], g["vh"], f"{where} {step}: below the screen")
        overlap = c[0] < h[2] and h[0] < c[2] and c[1] < h[3] and h[1] < c[3]
        self.assertFalse(overlap, f"{where} {step}: the words {c} cover the hole {h}")
        self.assertIsNotNone(g["tip"], f"{where} {step}: no arrow")
        dx = max(h[0] - g["tip"][0], 0, g["tip"][0] - h[2])
        dy = max(h[1] - g["tip"][1], 0, g["tip"][1] - h[3])
        self.assertLessEqual((dx * dx + dy * dy) ** 0.5, 9, f"{where} {step}: the arrow's tip {g['tip']} is not at the hole {h}")
        self.assertTrue(g["focus"], f"{where} {step}: the keyboard left the tour")
        return g


@unittest.skipIf(SKIP, SKIP or "")
class TourPage(PageBase):
    @classmethod
    def fill(cls):
        cls.pids = fill(cls.r, cls.alice)
        cls.nia = new_user(NIA, "Nia", "contributor")
        cls.vic = new_user(VIC, "Vic", "viewer")

    def walk(self, where, plan=STEPS, checks=None):
        """Every step with Next, checking each spotlight; ends on the last step."""
        st = self.settle(plan[0])
        self.assertEqual(st["plan"], plan, where)
        for i, sid in enumerate(plan):
            st = self.settle(sid)
            self.assertEqual((st["i"], st["n"]), (i, len(plan)))
            self.assertSpotlight(sid, (checks or {}).get(sid), where)
            if i < len(plan) - 1:
                self.js("document.getElementById('tour-next').click()")
        return st

    def test_1_desktop_every_step_on_its_control_then_the_page_as_it_was(self):
        self.fresh(self.nia)
        self.home(NIA)
        st = self.settle("search")
        self.assertEqual(st["text"], "Search inside papers, or filter")
        self.assertEqual(self.r.q("SELECT state FROM tour WHERE user_id = ?", self.nia)[0][0], "started")
        texts = {}
        for i, sid in enumerate(STEPS):
            st = self.settle(sid)
            texts[sid] = st["text"]
            g = self.assertSpotlight(sid, where="desktop")
            self.assertEqual(self.js("document.getElementById('tour-n').textContent"), f"{i + 1} / 10")
            if sid == "play":
                self.assertEqual(self.js("location.hash"), f"#p={self.pids[-1]}")      # the newest paper, opened for it
                # it follows the control when the page moves under it
                self.b.viewport(1100, 760)
                time.sleep(0.2)
                self.assertSpotlight(sid, where="after a resize")
                self.b.viewport(1280, 820)
                time.sleep(0.2)
            if sid == "transcript":
                self.js("document.getElementById('win').scrollTop += 40")
                time.sleep(0.2)
                g2 = self.assertSpotlight(sid, where="after a scroll")
                self.assertNotEqual(g2["r"][1], g["r"][1], "the window did not scroll")
            if sid == "link":
                self.assertEqual(st["text"], "Link two papers")
                self.assertFalse(self.js("document.getElementById('map').hidden"))
            if i < len(STEPS) - 1:
                self.js("document.getElementById('tour-next').click()")
        self.assertEqual(texts, {
            "search": "Search inside papers, or filter", "play": "Play the episode", "listened": "Tick it when you’ve listened",
            "upnext": "Add it to Up next", "transcript": "Tap a sentence to play from there", "comments": "Comment on the paper",
            "map": "The map of how papers connect", "link": "Link two papers", "settings": "Your voice and preferences",
            "help": "How to add papers"})
        self.assertEqual(self.js("document.getElementById('tour-next').textContent"), "Done")
        # the page untouched by it: nothing ticked, queued or played
        self.assertEqual(self.r.q("SELECT count(*) FROM listened")[0][0], 0)
        self.assertEqual(self.r.q("SELECT count(*) FROM up_next")[0][0], 0)
        self.assertTrue(self.js("document.getElementById('audio').paused"))
        self.js("document.getElementById('tour-next').click()")
        self.wait("!PaperTour.current.state().on && !document.getElementById('tour')", "the tour ended")
        self.wait("PaperTour.current.state().hub.state === 'finished'", "finished, on the hub")
        self.assertEqual(self.r.q("SELECT state FROM tour WHERE user_id = ?", self.nia)[0][0], "finished")
        # put back: the list with nothing open, no menu, no map, and nothing new remembered
        self.wait("location.hash === '' && document.getElementById('paper').hidden && document.getElementById('map').hidden", "the page as it was")
        self.assertFalse(self.js("!!document.querySelector('.menu')"))
        self.assertIsNone(self.js("localStorage.getItem('pcg.last')"))
        self.assertIsNone(self.js("localStorage.getItem('pcg.map.tab')"))
        self.assertFalse(self.js("document.body.classList.contains('touring')"))
        self.assertTrue(self.js("!document.getElementById('tour-again').hidden"))
        # finished: not again on a reload; Tour again runs it once more and leaves it finished
        self.home(NIA, clear=False)
        time.sleep(1.0)
        self.assertFalse(self.state()["on"])
        self.js("document.getElementById('tour-again').click()")
        self.settle("search")
        self.assertTrue(self.js("document.getElementById('tour-again').hidden"))
        self.key("Escape")
        self.wait("!PaperTour.current.state().on", "skipped")
        self.wait("PaperTour.current.state().hub && PaperTour.current.state().hub.state === 'skipped'", "the replay's end kept")

    def test_2_keyboard_back_next_escape_and_focus_kept(self):
        self.fresh(self.nia)
        self.home(NIA)
        self.settle("search")
        self.key("ArrowRight")
        self.settle("play")
        self.key("ArrowRight")
        self.settle("listened")
        self.key("ArrowLeft")
        self.settle("play")
        self.assertSpotlight("play", where="back")
        self.key("ArrowLeft")
        self.settle("search")
        self.assertTrue(self.js("document.getElementById('tour-back').disabled"))
        self.key("ArrowLeft")                              # nothing before the first
        time.sleep(0.3)
        self.assertEqual(self.state()["id"], "search")
        # Tab goes round the tour's own buttons only
        seen = set()
        for _ in range(5):
            self.key("Tab")
            seen.add(self.js("document.activeElement.id"))
        self.assertEqual(seen, {"tour-next", "tour-skip"})
        for _ in range(3):
            self.key("Tab", shift=True)
            self.assertTrue(self.js("!!document.activeElement.closest('#tour')"))
        # the page's own keys wait: "/" does not reach the search, a click nothing under the scrim
        self.key("/")
        self.assertNotEqual(self.js("document.activeElement.id"), "search")
        self.js("document.querySelector('#tour .tour-block').click()")
        self.assertEqual(self.state()["id"], "search")
        # Enter on the words is Next
        self.js("document.getElementById('tour-card').focus()")
        self.key("Enter")
        self.settle("play")
        self.key("Escape")
        self.wait("!PaperTour.current.state().on", "Escape skips")
        self.wait("PaperTour.current.state().hub.state === 'skipped'", "skipped, on the hub")
        self.wait("location.hash === '' && document.getElementById('paper').hidden", "the paper it opened, closed again")
        # skipped for good
        self.home(NIA, clear=False)
        time.sleep(1.0)
        self.assertFalse(self.state()["on"])
        self.assertTrue(self.js("!document.getElementById('tour-again').hidden"))

    def test_3_phone(self):
        self.fresh(self.nia)
        self.b.viewport(390, 844, mobile=True)
        self.home(NIA)
        st = self.settle("search")
        for i, sid in enumerate(STEPS):
            st = self.settle(sid)
            self.assertSpotlight(sid, where="phone")
            bad = json.loads(self.js(TAPS_IN % "document.getElementById('tour')"))
            self.assertEqual(bad, [], f"phone, the tour at {sid}:\n" + "\n".join(bad))
            if sid in ("play", "comments"):
                self.assertTrue(self.js("document.body.classList.contains('open')"), "the paper's window, open over the list")
            if sid in ("search", "map", "settings", "help"):
                self.assertFalse(self.js("document.body.classList.contains('open')"), f"{sid}: the list, on top")
            if i < len(STEPS) - 1:
                self.js("document.getElementById('tour-next').click()")
        self.js("document.getElementById('tour-skip').click()")
        self.wait("!PaperTour.current.state().on && location.hash === '' && !document.body.classList.contains('open')", "put back")
        self.wait("!document.getElementById('tour-again').hidden", "Tour again")
        # the page's own tap check, with Tour again on it
        import test_page
        bad = json.loads(self.js(test_page.TAP_TARGETS))
        self.assertEqual(bad, [], "phone, the list with Tour again:\n" + "\n".join(bad))
        for w in ("document.documentElement", "document.getElementById('list-pane')"):
            self.assertLessEqual(self.js(f"{w}.scrollWidth - {w}.clientWidth"), 0, f"{w} scrolls sideways")

    def test_4_reduced_motion(self):
        self.fresh(self.nia)
        self.b.call("Emulation.setEmulatedMedia", features=[{"name": "prefers-reduced-motion", "value": "reduce"}])
        self.home(NIA)
        self.settle("search")
        self.js("document.getElementById('tour-next').click()")
        self.wait("PaperTour.current.state().id === 'play'", "the next step")
        self.assertFalse(self.state()["gliding"])
        self.assertEqual(self.js("getComputedStyle(document.querySelector('#tour .tour-hole')).transitionDuration"), "0s")
        self.assertEqual(self.js("getComputedStyle(document.getElementById('tour-card')).animationName"), "none")
        self.assertSpotlight("play", where="reduced motion")
        self.js("PaperTour.current.skip()")
        self.wait("!PaperTour.current.state().on", "skipped")

    def test_5_help_panel_and_its_copy_buttons(self):
        self.fresh(self.nia)
        self.r.q("INSERT OR REPLACE INTO tour(user_id, state, first_seen, updated_at) VALUES (?, 'finished', ?, ?)", self.nia, db.now(), db.now())
        self.home(NIA)
        self.assertFalse(self.state()["on"])
        self.assertEqual(self.js("document.getElementById('help-btn').getAttribute('aria-label')"), "Help")
        # in the list's header, beside the map and Settings
        self.assertEqual(self.js("document.getElementById('help-btn').parentElement.className"), "head-btns")
        self.js("document.getElementById('help-btn').click()")
        self.wait("!document.getElementById('help').hidden", "the panel")
        self.assertEqual(self.js("document.getElementById('help-btn').getAttribute('aria-expanded')"), "true")
        cmds = self.js("[...document.querySelectorAll('#help .help-code')].map(c => c.textContent)")
        self.assertEqual(cmds, [
            "pipx install 'git+https://github.com/leolou0512/papercast#subdirectory=packages/papercast-cli'",
            f"papercast login --server {PUBLIC}",
            "papercast add paper.pdf",
            "papercast status"])
        words = self.js("document.getElementById('help').innerText")
        self.assertIn("or an arXiv / DOI link", words)
        self.assertIn("Needs Claude Code, installed and logged in.", words)
        self.assertLess(len(words.split()), 45, words)                       # very few words
        # Copy puts the exact command on the clipboard
        try:
            self.b.call("Browser.grantPermissions", origin=self.base, permissions=["clipboardReadWrite", "clipboardSanitizedWrite"])
            real = True
        except RuntimeError:
            real = False
        if not real:
            self.js("navigator.clipboard.writeText = async (t) => { window.__copied = t; }")
        for i, want in enumerate(cmds):
            self.js(f"document.querySelectorAll('#help .help-copy')[{i}].click()")
            self.wait(f"document.querySelectorAll('#help .help-copy')[{i}].textContent === 'Copied'", "copied")
            got = self.js("navigator.clipboard.readText()") if real else self.js("window.__copied")
            self.assertEqual(got, want)
        self.assertEqual(self.js("[...document.querySelectorAll('#help .help-copy')].map(b => b.getAttribute('aria-label'))"),
                         ["Copy the install command", "Copy the log in command", "Copy the add command", "Copy the status command"])
        # Escape closes it and gives the keyboard back to "?"; so does a click elsewhere
        self.key("Escape")
        self.wait("document.getElementById('help').hidden", "closed")
        self.assertEqual(self.js("document.activeElement.id"), "help-btn")
        self.js("document.getElementById('help-btn').click()")
        self.wait("!document.getElementById('help').hidden", "open again")
        self.b.call("Input.dispatchMouseEvent", type="mousePressed", x=800, y=500, button="left", clickCount=1)
        self.b.call("Input.dispatchMouseEvent", type="mouseReleased", x=800, y=500, button="left", clickCount=1)
        self.wait("document.getElementById('help').hidden", "closed by a click elsewhere")
        # on a phone: reachable, and its taps
        self.b.viewport(390, 844, mobile=True)
        self.home(NIA, clear=False)
        self.js("document.getElementById('help-btn').click()")
        self.wait("!document.getElementById('help').hidden", "the phone's panel")
        bad = json.loads(self.js(TAPS_IN % "document.getElementById('help')"))
        self.assertEqual(bad, [], "phone, the help panel:\n" + "\n".join(bad))
        bad = json.loads(self.js(TAPS_IN % "document.querySelector('.head-btns')"))
        self.assertEqual(bad, [], "phone, the header's buttons")
        self.assertLessEqual(self.js("document.getElementById('help').getBoundingClientRect().right"), 390)

    def test_6_a_viewer_is_told_to_ask_an_admin(self):
        self.fresh(self.vic)
        self.home(VIC)
        self.settle("search")
        texts = {}
        for sid in STEPS:
            st = self.settle(sid)
            texts[sid] = st["text"]
            if sid != "help":
                self.js("document.getElementById('tour-next').click()")
        self.assertEqual((texts["settings"], texts["help"]), ("Your settings", "Help"))
        self.js("document.getElementById('tour-next').click()")
        self.wait("!PaperTour.current.state().on", "done")
        self.js("document.getElementById('help-btn').click()")
        self.wait("!document.getElementById('help').hidden", "the panel")
        self.assertEqual(self.js("document.getElementById('help').innerText.trim().split('\\n').filter(Boolean)"),
                         ["Add papers", "Uploading needs contributor access: ask an admin."])
        self.assertFalse(self.js("!!document.querySelector('#help .help-code')"))

    def test_7_someone_who_was_here_before_sees_none_of_it(self):
        self.home(A)
        time.sleep(1.0)
        st = self.state()
        self.assertEqual((st["on"], st["again"], st["hub"]["state"]), (False, False, "old"))
        self.assertTrue(self.js("document.getElementById('tour-again').hidden"))
        self.assertTrue(self.js("!document.getElementById('help-btn').hidden"))       # "?" is for everyone

    def test_8_it_waits_for_the_library(self):
        """Opened on a paper, it starts once the person is back on the list."""
        self.fresh(self.nia)
        self.as_user(NIA)
        self.b.goto("about:blank")
        self.b.goto(self.base + f"/#p={self.pids[0]}")
        self.wait("!!window.PaperTour && !!PaperTour.current && !document.getElementById('paper').hidden", "the paper")
        time.sleep(1.0)
        self.assertFalse(self.state()["on"])
        self.js("document.getElementById('back').click()")
        self.settle("search")
        self.js("PaperTour.current.skip()")
        self.wait("!PaperTour.current.state().on", "skipped")


@unittest.skipIf(SKIP, SKIP or "")
class TourSparse(PageBase):
    """Steps whose control is not there are left out: no transcript, graphs locked, then no papers."""

    @classmethod
    def fill(cls):
        cls.pids = fill(cls.r, cls.alice, n=2, script=False)
        graph.ensure_schema()
        db.conn().execute("UPDATE graphs SET locked = 1")
        cls.vic = new_user(VIC, "Vic", "viewer")
        cls.nia = new_user(NIA, "Nia", "contributor")

    def test_1_no_transcript_and_locked_graphs(self):
        """A version with no script has no transcript; a viewer cannot link papers in a locked graph."""
        self.fresh(self.vic)
        self.home(VIC)
        self.settle("search")
        plan = [s for s in STEPS if s not in ("transcript", "link")]
        seen = []
        while True:
            st = self.settle()
            if seen and seen[-1] == st["id"]:
                time.sleep(0.1)
                continue
            seen.append(st["id"])
            self.assertSpotlight(st["id"], where="sparse")
            if st["i"] == st["n"] - 1:
                break
            self.js("document.getElementById('tour-next').click()")
        self.assertEqual(seen, plan)
        self.assertEqual(self.state()["plan"], plan)
        self.assertEqual(self.js("document.getElementById('tour-n').textContent"), "8 / 8")
        self.js("document.getElementById('tour-next').click()")
        self.wait("!PaperTour.current.state().on && document.getElementById('map').hidden", "done")

    def test_2_an_empty_library(self):
        db.conn().execute("UPDATE episodes SET deleted_at = ?", (db.now(),))
        self.fresh(self.nia)
        self.home(NIA)
        self.wait("!!document.querySelector('#rows .empty-list')", "no papers")
        st = self.settle("search")
        self.assertEqual(st["plan"], ["search", "map", "settings", "help"])
        for sid in st["plan"]:
            st = self.settle(sid)
            self.assertSpotlight(sid, where="empty")
            if sid != "help":
                self.key("ArrowRight")
        self.key("ArrowRight")
        self.wait("!PaperTour.current.state().on", "done")
        self.wait("PaperTour.current.state().hub.state === 'finished'", "finished")


# ============================================================================ the first sign-in

@unittest.skipIf(SKIP, SKIP or "")
class FirstSignIn(unittest.TestCase):
    """PCG_AUTH=password: the first password is changed first, then the tour starts; skipped, it is
    over on a second browser too; Tour again for three days by the hub's clock."""

    @classmethod
    def setUpClass(cls):
        import test_accounts as TA
        from hub import accounts, layout
        cls.TA, cls.accounts = TA, accounts
        cls.hub = TA.Hub().start()
        cls.base = f"http://127.0.0.1:{cls.hub.port}"
        cls.hub.cfg.public_url = cls.base                   # the browser's origin (CSRF), plain http here
        layout.AUTO = False
        cls.uid = accounts.allow(cls.hub.cfg, "nt101")["user_id"]
        cls.b = Browser()
        cls.b.call("Log.enable")
        cls.b.viewport(1280, 820)
        cls.b2 = None

    @classmethod
    def tearDownClass(cls):
        cls.b.close()
        if cls.b2:
            cls.b2.close()
        cls.hub.close()

    def sign_in(self, b, pw):
        b.goto(self.base + "/")
        b.wait_js("location.pathname === '/signin' && !document.getElementById('signin').hidden", 10, "the sign-in form")
        for sel, v in (("#login", "nt101"), ("#password", pw)):
            b.js(f"{{ const e = document.querySelector({json.dumps(sel)}); e.value = {json.dumps(v)}; e.dispatchEvent(new Event('input')); }}")
        b.js("document.getElementById('go').click()")

    def errors(self, b):
        out = []
        for m in b.events:
            meth, p = m.get("method"), m.get("params", {})
            if meth == "Runtime.exceptionThrown" or (meth == "Runtime.consoleAPICalled" and p.get("type") in ("error", "assert")) \
                    or (meth == "Log.entryAdded" and p.get("entry", {}).get("level") == "error"):
                out.append(json.dumps(p)[:300])
        b.events.clear()
        return out

    def test_first_sign_in_the_password_then_the_tour(self):
        b = self.b
        self.accounts.reset_limits()
        self.sign_in(b, "nt101")
        b.wait_js("location.pathname === '/set-password' && !document.getElementById('setpw').hidden", 10, "the new password first")
        self.assertFalse(b.js("!!window.PaperTour"))
        # until it is changed, the tour's API refuses as every other does
        ck = {c["name"]: c["value"] for c in b.call("Network.getCookies", urls=[self.base])["cookies"]}
        s, j, _ = self.hub.req("GET", "/api/tour", cookie=f"pcg_s={ck['pcg_s']}")
        self.assertEqual((s, j["error"]), (403, "must_change_password"))
        self.assertEqual(self.hub.q("SELECT count(*) FROM tour")[0][0], 0)
        b.js("{ const e = document.getElementById('password'); e.value = 'the kettle is on now'; e.dispatchEvent(new Event('input')); }")
        b.js("document.getElementById('save').click()")
        b.wait_js("location.pathname === '/' && !!window.PaperTour && !!PaperTour.current", 10, "the library")
        b.wait_js("PaperTour.current.state().on && PaperTour.current.state().id === 'search'", 10, "then the tour, on the library")
        b.wait_js("(() => { const s = PaperTour.current.state(); return s.hub && s.hub.state === 'started'; })()", 5, "started, on the hub")
        self.assertEqual(self.hub.q("SELECT state FROM tour WHERE user_id = ?", (self.uid,))[0][0], "started")
        first = tour._unix(self.hub.q("SELECT first_seen FROM tour WHERE user_id = ?", (self.uid,))[0][0])
        # Skip, fixed at the side, ends it for good
        self.assertTrue(b.js("!!document.getElementById('tour-skip').className.match(/at-(rm|lm|br|tr|bl|tl)/)"))
        b.js("document.getElementById('tour-skip').click()")
        b.wait_js("!PaperTour.current.state().on && PaperTour.current.state().hub.state === 'skipped'", 5, "skipped")
        b.goto(self.base + "/")
        b.wait_js("!!window.PaperTour && !!PaperTour.current && !!PaperTour.current.state().hub", 10, "reloaded")
        time.sleep(1.0)
        self.assertFalse(b.js("PaperTour.current.state().on"))
        self.assertTrue(b.js("!document.getElementById('tour-again').hidden"))
        self.assertEqual(self.errors(b), [])
        # a second browser: the same person, the same state
        b2 = type(self).b2 = Browser()
        b2.call("Log.enable")
        b2.viewport(390, 844, mobile=True)
        self.accounts.reset_limits()
        self.sign_in(b2, "the kettle is on now")
        b2.wait_js("location.pathname === '/' && !!window.PaperTour && !!PaperTour.current && !!PaperTour.current.state().hub", 10, "signed in again")
        time.sleep(1.0)
        self.assertFalse(b2.js("PaperTour.current.state().on"))
        self.assertEqual(b2.js("PaperTour.current.state().hub.state"), "skipped")
        self.assertTrue(b2.js("!document.getElementById('tour-again').hidden"))
        # Tour again: within the three days, not after (the hub's clock moved on)
        for shift, shown in ((2 * 86400, True), (3 * 86400 - 30, True), (3 * 86400 + 5, False), (30 * 86400, False)):
            with mock.patch.object(tour, "_now", lambda: first + shift):
                b2.goto(self.base + "/")
                b2.wait_js("!!window.PaperTour && !!PaperTour.current && !!PaperTour.current.state().hub", 10, "reloaded")
                time.sleep(0.3)
                self.assertEqual(b2.js("!document.getElementById('tour-again').hidden"), shown, f"{shift / 86400:.2f} days on")
                self.assertFalse(b2.js("PaperTour.current.state().on"))
        # a page left open past the three days loses it too
        with mock.patch.object(tour, "_now", lambda: first + 3 * 86400 - 2):
            b2.goto(self.base + "/")
            b2.wait_js("!document.getElementById('tour-again').hidden", 10, "Tour again, for two more seconds")
            b2.wait_js("document.getElementById('tour-again').hidden", 6, "gone on its own")
        self.assertEqual(self.errors(b2), [])


if __name__ == "__main__":
    unittest.main()
