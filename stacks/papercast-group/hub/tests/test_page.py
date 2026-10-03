#!/usr/bin/env python3
"""The page (hub/static) in a real headless Chrome, against the real hub and the auth stand-in.

    python3 -m unittest discover -s stacks/papercast-group/hub/tests -p 'test_page.py' -v

Three people: Alice (admin), Bob (contributor), Carol (viewer). The home is the graph list and
the map (test_graphlist_page.py has the list itself); a paper opens in its graph, its window over
the map's side. A paper with two versions has one window listing both; playing picks your own
version, else the first made. Listened and positions are each person's; positions reach the hub
(one request for a burst of seeks) and a newer copy on the device wins. Settings: preferences,
devices, and for admins users and the base prompt. The explainer is sandboxed: a hostile one
reaches nothing (checked against a control run without the sandbox, where it does). On a phone
every control is a 44 x 44 px tap. No test may leave an error in the console. Screenshots go to
$PCG_TEST_SHOTS if set.

Skips when no headless Chrome or no `websocket-client` is available."""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from web_rig import STATIC, Rig, publish  # noqa: E402

from hub import db  # noqa: E402

try:
    import websocket  # noqa: F401
    from web_cdp import Browser, find_chrome
    SKIP = None if find_chrome() else "no headless Chrome"
except ImportError:
    SKIP = "websocket-client not installed"

SHOTS = os.environ.get("PCG_TEST_SHOTS")
A, B, C = "alice@example.org", "bob@example.org", "carol@example.org"
# the graph list's rows (graphs.js), a phone's list of one graph's papers
GROW = "#gl .gl-row[data-id=\"{}\"]"
GROWS = "[...document.querySelectorAll('#gl .gl-row')].map(x => x.dataset.id)"
GPLROWS = "[...document.querySelectorAll('#gpl-rows .gpl-row')].map(x => x.dataset.id)"
COL = "document.getElementById('gcol')"


def js_list(ids):
    """A JS string literal of the JSON that JSON.stringify gives for this list."""
    return json.dumps(json.dumps(ids, separators=(",", ":")))

# Leo's (stacks/papercast/tests/test_browser.py TAP_TARGETS), verbatim: every control the page
# shows now that is under 44 x 44 px, or that a tap on its edge does not reach.
TAP_TARGETS = r"""(() => {
  const q = 'a[href], button, input:not([type=hidden]), select, textarea, summary, label, [role=button], [role=link],' +
            ' [role=slider], [role=menuitem], [tabindex]:not([tabindex="-1"])';
  const bell = document.getElementById('bell-panel');
  const top = !document.getElementById('overlay').hidden ? document.getElementById('overlay') : document.querySelector('.menu') || (bell && !bell.hidden ? bell : null);
  const open = document.body.classList.contains('open'), gp = document.body.classList.contains('gpl-open');
  const phone = matchMedia('(max-width: 720px)').matches, map = !document.getElementById('map').hidden;
  const panes = [document.getElementById('win'), document.getElementById('gcol'), document.getElementById('gpl')];
  const was = panes.map((p) => p.scrollTop);
  const settle = () => panes.forEach((p) => p.dispatchEvent(new Event('scroll')));   // the pinned bar follows
  const name = (e) => `${e.tagName.toLowerCase()}${e.id ? '#' + e.id : ''} "${(e.getAttribute('aria-label') || e.textContent || e.placeholder || '').trim().slice(0, 24)}"`;
  const bad = [], seen = [];
  for (const e of document.querySelectorAll(q)) {
    const cs = getComputedStyle(e);
    if (e.closest('[hidden]') || cs.visibility !== 'visible' || cs.pointerEvents === 'none' || !e.getClientRects().length) continue;
    // under what covers it on a phone: the window over the rest, the drawing over the lists, a graph's papers over the column
    if ((top && !top.contains(e)) || (open && e.closest('#gcol, #gpl, #map')) || (phone && map && e.closest('#gcol, #gpl'))
        || (phone && gp && e.closest('#gcol'))) continue;
    const r = e.getBoundingClientRect();
    const inMd = e.tagName === 'A' && !!e.closest('.md');
    if (!inMd && (r.width < 43.5 || r.height < 43.5)) bad.push(`${name(e)} is ${r.width.toFixed(1)} x ${r.height.toFixed(1)}`);
    if (!e.classList.contains('row-del')) seen.push(e);
  }
  for (const e of seen) {
    panes.forEach((p, i) => { p.scrollTop = was[i]; }); settle();
    let fixed = false;
    for (let a = e; a && !panes.includes(a); a = a.parentElement) {
      if (['fixed', 'sticky'].includes(getComputedStyle(a).position)) { fixed = true; break; }
    }
    if (!fixed || e.closest('#bell-panel')) { e.scrollIntoView({block: 'center', inline: 'nearest'}); settle(); }
    const rects = getComputedStyle(e).display === 'inline' ? [...e.getClientRects()] : [e.getBoundingClientRect()];
    for (const r of rects) {
      for (const [x, y] of [[r.left + 1, r.top + r.height / 2], [r.right - 1, r.top + r.height / 2],
                            [r.left + r.width / 2, r.top + 1], [r.left + r.width / 2, r.bottom - 1]]) {
        if (x < 0 || y < 0 || x >= innerWidth || y >= innerHeight) continue;
        const h = document.elementFromPoint(x, y);
        if (h !== e && !e.contains(h)) {
          const what = h ? `${h.tagName.toLowerCase()}${h.id ? '#' + h.id : ''}${typeof h.className === 'string' && h.className ? '.' + h.className.trim().split(/\s+/)[0] : ''}` : 'nothing';
          bad.push(`${name(e)} is covered at ${Math.round(x)},${Math.round(y)} by ${what}`);
        }
      }
    }
  }
  panes.forEach((p, i) => { p.scrollTop = was[i]; }); settle();
  return JSON.stringify(bad);
})()"""

# An explainer that tries everything (after Leo's tests/hostile_explainer.html). Every URL it
# asks for carries h=1, so the server's log shows whether anything reached it.
HOSTILE = """<!doctype html>
<html><head><meta charset="utf-8"><title>Hostile explainer</title></head>
<body><pre id="result">running</pre>
<form id="f" method="post" action="/api/papers/__PID__/listened?h=1" target="_self"><input name="a" value="b"></form>
<script>
(async () => {
  const R = {};
  const t = async (name, fn) => {
    try { R[name] = { ok: true, value: String(await fn()).slice(0, 300) }; }
    catch (e) { R[name] = { ok: false, error: String(e && (e.name + ": " + e.message)).slice(0, 300) }; }
  };
  const withTimeout = (p, ms) => Promise.race([p, new Promise((_, rej) => setTimeout(() => rej(new Error("timeout")), ms))]);
  await t("cookie_read", () => { const c = document.cookie; if (!c.includes("pcg_test_cookie")) throw new Error("no cookie visible: " + c); return c; });
  await t("local_storage", () => { localStorage.setItem("x", "1"); return localStorage.getItem("x"); });
  await t("fetch_get_api", async () => { const r = await withTimeout(fetch("/api/library?h=1", { credentials: "include" }), 3000); if (!r.ok) throw new Error("HTTP " + r.status); const j = await r.json(); return "papers:" + j.papers.length; });
  await t("fetch_put_listened", async () => {
    const r = await withTimeout(fetch("/api/papers/__PID__/listened?h=1", { method: "PUT", credentials: "include",
      headers: { "Content-Type": "application/json", "X-PCG": "1" }, body: JSON.stringify({ listened: true }) }), 3000);
    if (!r.ok) throw new Error("HTTP " + r.status); return "HTTP " + r.status;
  });
  await t("fetch_post_no_cors", async () => { await withTimeout(fetch("/api/episodes/__EID__/undelete?h=1", { method: "POST", mode: "no-cors", credentials: "include", body: "{}" }), 3000); return "sent"; });
  await t("xhr_get", () => new Promise((res, rej) => { const x = new XMLHttpRequest(); x.open("GET", "/api/config?h=1"); x.onload = () => x.status === 200 ? res("HTTP 200") : rej(new Error("HTTP " + x.status)); x.onerror = () => rej(new Error("xhr error")); x.send(); }));
  await t("beacon", () => { const ok = navigator.sendBeacon("/api/episodes/__EID__/undelete?h=1", new Blob(["{}"], { type: "application/json" })); if (!ok) throw new Error("sendBeacon refused"); return "queued"; });
  await t("eventsource", () => new Promise((res, rej) => { const es = new EventSource("/api/events?h=1"); es.onopen = () => { es.close(); res("open"); }; es.onerror = () => { es.close(); rej(new Error("error")); }; setTimeout(() => rej(new Error("timeout")), 3000); }));
  await t("parent_dom", () => { const t = window.parent.document.title; if (window.parent === window) throw new Error("not framed"); return t; });
  await t("image_get", () => new Promise((res, rej) => { const i = new Image(); i.onload = () => res("loaded"); i.onerror = () => rej(new Error("blocked or not an image")); i.src = "/icon.svg?h=1"; setTimeout(() => rej(new Error("timeout")), 3000); }));
  document.getElementById("result").textContent = JSON.stringify(R);
  try { window.parent.postMessage({ hostile: R }, "*"); } catch (e) { /* not framed */ }
  await new Promise((r) => setTimeout(r, 1000));
  try { window.open("/api/library?h=1&popup=1", "_blank"); } catch (e) { /* checked on the server */ }
  try { if (window.top !== window) window.top.location.href = "/api/library?h=1&navigated=1"; } catch (e) { /* ditto */ }
  try { document.getElementById("f").submit(); } catch (e) { /* ditto */ }
})();
</script></body></html>"""
ATTACKS = ["cookie_read", "local_storage", "fetch_get_api", "fetch_put_listened", "fetch_post_no_cors", "xhr_get",
           "beacon", "eventsource", "parent_dom", "image_get"]
# These two leave the browser in any case (a beacon and a no-cors POST answer "sent" before
# the CSP is asked); what they did is checked on the server. They carry no X-PCG, so even the
# control run cannot change anything with them.
SERVER_SIDE = {"beacon", "fetch_post_no_cors"}


class Log(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())


class PageBase(unittest.TestCase):
    """One hub and one browser per class; each test starts on a fresh page load."""
    static = None

    @classmethod
    def setUpClass(cls):
        cls.r = Rig(static=cls.static)
        cls.log = Log()
        logging.getLogger("pcg").addHandler(cls.log)
        logging.getLogger("pcg").setLevel(logging.INFO)
        cls.fill()
        cls.b = Browser()
        cls.b.call("Log.enable")
        cls.base = cls.r.base
        cls.b.viewport(1440, 900)
        cls.b.call("Network.setCookie", name="pcg_test_cookie", value="secret", url=cls.base, httpOnly=False)
        cls.as_user(A)

    @classmethod
    def tearDownClass(cls):
        cls.b.close()
        logging.getLogger("pcg").removeHandler(cls.log)
        cls.r.close()

    @classmethod
    def fill(cls):
        r = cls.r
        cls.alice = r.user(A, "Alice", "admin")
        cls.bob = r.user(B, "Bob", "contributor")
        cls.carol = r.user(C, "Carol", "viewer")

    @classmethod
    def as_user(cls, email):
        cls.who = email
        cls.b.call("Network.setExtraHTTPHeaders", headers={"X-Test-User": email})

    def setUp(self):
        self.allow = []                     # console errors this test expects (regexes)
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

    # ------------------------------------------------------------------ helpers
    def shot(self, name):
        if SHOTS:
            Path(SHOTS).mkdir(parents=True, exist_ok=True)
            self.b.screenshot(Path(SHOTS) / f"{name}.png")

    def home(self, user=None):
        """The page as it lands, with no paper open: the graph with the most papers (the account's
        graph open last is forgotten first, so every test lands the same)."""
        if user:
            self.as_user(user)
        b = self.b
        self.r.q("DELETE FROM ui_state")
        b.goto("about:blank")
        b.goto(self.base + "/")
        b.wait_js("document.querySelectorAll('#gl .gl-row').length > 0 && /^#g=/.test(location.hash)"
                  " && !!(window.PaperMap && PaperMap.current && PaperMap.current.debug.cur())", 10, "page started")

    def load(self, where=""):
        """A fresh load of the page at a hash (a hash alone would not reload it)."""
        self.b.goto("about:blank")
        self.b.goto(self.base + "/" + (f"#{where}" if where else ""))

    def open(self, pid, what="window open"):
        """A paper by its address alone (#p=): it opens in a graph that has it."""
        self.b.js(f"location.hash = 'p={pid}'")
        self.b.wait_js(f"!document.getElementById('paper').hidden && new URLSearchParams(location.hash.slice(1)).get('p') === '{pid}'"
                       " && /(^#|&)g=/.test(location.hash) && document.getElementById('w-title').textContent !== ''", 10, what)

    def text(self, sel):
        return self.b.js(f"(document.querySelector({json.dumps(sel)}) || {{}}).textContent")

    def phone(self):
        self.b.call("Emulation.setDeviceMetricsOverride", width=390, height=844, deviceScaleFactor=1, mobile=True)

    def assertTargets(self, where):
        # the phone's window and lists at rest, not sliding in or out (a control mid-slide is at no place)
        self.b.wait_js("['win', 'gpl'].every(id => (w => !w || !w.getAnimations().length)(document.getElementById(id)))", 3, "the window at rest")
        bad = json.loads(self.b.js(TAP_TARGETS))
        self.assertEqual(bad, [], f"phone, {where}: {len(bad)} tap target(s) too small or covered:\n" + "\n".join(bad))

    def no_side_scroll(self, where):
        for sel in ("document.documentElement", "document.getElementById('win')", COL, "document.getElementById('gpl')"):
            self.assertLessEqual(self.b.js(f"{sel}.scrollWidth - {sel}.clientWidth"), 0, f"{where}: {sel} scrolls sideways")

    def puts(self, since):
        return [ln for ln in self.log.lines[since:] if '"PUT /api/episodes/' in ln and "/position" in ln]


@unittest.skipIf(SKIP, SKIP or "")
class Page(PageBase):
    @classmethod
    def fill(cls):
        super().fill()
        r = cls.r
        # 120 papers from Alice, oldest first (so the newest come first in the list)
        cls.many = []
        for i in range(120):
            p = r.paper(f"Fake paper n{i:03d}", cls.alice, tags=["long list"] if i % 2 else ["other"], year=2000 + i % 25)
            r.episode(p, cls.alice, summary="derivations", audio_s=1.0)
            cls.many.append(p)
        # one paper, two versions: Alice's first, then Bob's; 30 s of (silent) audio each
        cls.two = r.paper("Two Versions Of One Fake Paper", cls.alice, authors=["Ada Lovelace", "Charles Babbage", "Mary Somerville"],
                          year=2024, tags=["diffusion"], url="https://example.org/two")
        cls.va = r.episode(cls.two, cls.alice, summary="derivations", duration=30, audio_s=30)
        cls.vb = r.episode(cls.two, cls.bob, summary="practical", duration=30, audio_s=30)
        # Bob's: one waiting for the GPU, one being spoken
        cls.wait = r.paper("A Fake Paper Waiting For The GPU", cls.bob, tags=["robotics"])
        cls.ew = r.episode(cls.wait, cls.bob, state="waiting-for-gpu")
        cls.speak = r.paper("A Fake Paper Being Spoken", cls.bob)
        cls.es = r.episode(cls.speak, cls.bob, state="speaking", progress=0.4)
        # never in the library
        gone = r.paper("A Rejected Fake Paper", cls.bob)
        r.episode(gone, cls.bob, state="rejected")
        cls.order = [cls.speak, cls.wait, cls.two] + cls.many[::-1]

    def test_0_the_player_bar(self):
        """The player is a bar along the bottom of the page, the whole width; the list and the
        window end where it starts. It plays, pauses and seeks; it stays while they scroll and
        while another paper is open, which then has its own Play; its title opens its paper."""
        b = self.b
        audio = "document.getElementById('audio')"
        rect = lambda sel: b.js(f"(r => [r.left, r.top, r.right, r.bottom])(document.querySelector({json.dumps(sel)}).getBoundingClientRect())")
        try:
            self.home(C)
            self.open(self.two)
            b.wait_js(f"!document.getElementById('bar').hidden && {audio}.duration > 20", 10, "the bar, loaded")
            vw, vh = b.js("[innerWidth, innerHeight]")
            bar = rect("#bar")
            self.assertEqual(b.js("getComputedStyle(document.getElementById('bar')).position"), "fixed")
            self.assertEqual([round(v) for v in (bar[0], bar[2], bar[3])], [0, vw, vh], "the bar is not along the bottom")
            self.assertAlmostEqual(rect(".app")[3], bar[1], delta=1, msg="the column, the map and the window do not end at the bar")
            self.assertAlmostEqual(rect("#win")[3], bar[1], delta=1, msg="the window does not end at the bar")
            self.assertAlmostEqual(rect("#map")[3], bar[1], delta=1, msg="the map does not end at the bar")
            self.assertEqual(self.text("#bar-title"), "Two Versions Of One Fake Paper")
            self.assertEqual(self.text("#bar-sub"), "by Alice · derivations")
            self.assertTrue(b.js("document.getElementById('w-play').hidden"), "the bar has this paper: no second Play")
            # play, pause
            b.js(f"{audio}.muted = true; document.getElementById('p-play').click()")
            b.wait_js(f"!{audio}.paused && {audio}.currentTime > 0.3 && document.getElementById('p-play').getAttribute('aria-label') === 'Pause'", 10, "playing")
            b.js("document.getElementById('p-play').click()")
            b.wait_js(f"{audio}.paused && document.getElementById('p-play').getAttribute('aria-label') === 'Play'", 5, "paused")
            # seek: pressed at three quarters of the line
            x0, y0, w = b.js("(r => [r.left, r.top + r.height / 2, r.width])(document.querySelector('#scrub .track').getBoundingClientRect())")
            for kind in ("mousePressed", "mouseReleased"):
                b.call("Input.dispatchMouseEvent", type=kind, x=x0 + w * 0.75, y=y0, button="left", clickCount=1)
            b.wait_js(f"Math.abs({audio}.currentTime - 0.75 * {audio}.duration) < 0.8"
                      f" && document.getElementById('p-el').textContent === '0:' + String(Math.floor({audio}.currentTime)).padStart(2, '0')", 5, "sought")
            # the middle and the column scroll under it; it stays
            b.js("document.getElementById('p-mid').scrollTop = 200; document.getElementById('gcol').scrollTop = 400")
            self.assertEqual(rect("#bar"), bar)
            # another paper open: the bar keeps the one playing, and the other paper has its own Play
            b.js(f"document.getElementById('p-play').click()")
            b.wait_js(f"!{audio}.paused", 5, "playing again")
            self.open(self.many[0])
            self.assertEqual(self.text("#bar-title"), "Two Versions Of One Fake Paper")
            self.assertEqual(rect("#bar"), bar)
            b.wait_js(f"!document.getElementById('w-play').hidden && !{audio}.paused", 3, "the open paper's own Play")
            # the bar's title opens the paper that plays
            b.js("document.getElementById('bar-open').click()")
            b.wait_js(f"location.hash.endsWith('p={self.two}') && document.getElementById('w-play').hidden", 5, "back to it from the bar")
            self.open(self.many[0])
            b.js("document.getElementById('w-play').click()")
            b.wait_js("document.getElementById('bar-title').textContent === 'Fake paper n000'"
                      " && document.getElementById('w-play').hidden", 5, "the open paper plays in the bar")
            b.js(f"{audio}.pause()")
            self.shot("desktop-bar")
        finally:
            # the page's last save goes as it is left: gone, then the positions forgotten
            b.js(f"{audio}.pause()")
            b.goto("about:blank")
            time.sleep(0.8)
            b.goto(self.base + "/")
            b.js("localStorage.clear()")
            self.r.q("DELETE FROM positions")

    def test_1_a_paper_opens_in_its_graph_with_its_window(self):
        """No list of papers: the home is the graph list and the map. A paper's address (#p=) opens
        it in a graph that has it, else under Not in any graph; its window says who made the
        version that plays and how far each version is; closing it returns to the map."""
        b = self.b
        self.home(A)
        self.assertIsNone(b.js("document.getElementById('rows')"))
        self.assertIsNone(b.js("document.getElementById('list-pane')"))
        gen = db.conn().execute("SELECT id FROM graphs WHERE name = 'Diffusion and generative models'").fetchone()[0]
        self.open(self.two)                                     # tagged "diffusion": the seed graph has it
        self.assertEqual(b.js("new URLSearchParams(location.hash.slice(1)).get('g')"), gen)
        b.wait_js(f"PaperMap.current.debug.cur().id === '{gen}' && PaperMap.current.debug.state().sel === '{self.two}'", 5, "its card on the map")
        self.assertEqual(self.text("#w-maker"), "by Alice · derivations")      # Alice's own plays for her
        self.assertEqual(b.js("[...document.querySelectorAll('#vlist .v-st')].map(x => x.textContent)"), ["1 min", "1 min"])
        self.open(self.wait)                                    # tagged "robotics": in no graph
        self.assertEqual(b.js("new URLSearchParams(location.hash.slice(1)).get('g')"), "none")
        self.assertEqual(self.text("#w-maker"), "by Bob")
        self.assertEqual(b.js("document.querySelector('#w-state .big').textContent"), "Waiting for GPU")
        self.open(self.speak)
        self.assertEqual(b.js("document.querySelector('#w-state .big').textContent"), "Speaking 40%")
        self.open(self.many[0])
        self.assertEqual(self.text("#w-maker"), "by Alice · derivations")
        # closed: the map again, and the graph it was in
        b.js("document.getElementById('w-close').click()")
        b.wait_js("document.getElementById('paper').hidden && location.hash === '#g=none' && getComputedStyle(document.getElementById('win')).visibility === 'hidden'", 5, "closed")
        self.shot("desktop-home")

    def test_2_listened_is_each_persons_own(self):
        """The Listened tick, in the paper's window: each person's own, and from another device of
        theirs at once."""
        b, r = self.b, self.r
        x, y, z = self.many[119], self.many[118], self.many[117]
        tick = "document.getElementById('w-listened').getAttribute('aria-checked')"
        try:
            self.home(A)
            self.open(x)
            b.js("document.getElementById('w-listened').click()")
            b.wait_js(f"{tick} === 'true'", 5, "ticked")
            r.wait(lambda: r.q("SELECT 1 FROM listened WHERE user_id = ? AND paper_id = ?", self.alice, x), 5, "stored for Alice")
            self.home(B)
            self.open(x)
            self.assertEqual(b.js(tick), "false")                           # Alice's tick is hers
            self.open(y)
            b.js("document.getElementById('w-listened').click()")
            r.wait(lambda: r.q("SELECT 1 FROM listened WHERE user_id = ? AND paper_id = ?", self.bob, y), 5, "stored for Bob")
            self.home(A)
            self.open(x)
            self.assertEqual(b.js(tick), "true")
            self.open(y)
            self.assertEqual(b.js(tick), "false")
            # from another device of hers while the page is open
            self.open(z)
            self.assertEqual(b.js(tick), "false")
            r.req("PUT", f"/api/papers/{z}/listened", {"listened": True}, user=A)
            b.wait_js(f"{tick} === 'true'", 5, "her other device")
            r.req("PUT", f"/api/papers/{x}/listened", {"listened": False}, user=B)     # Bob's, not hers: no change here
            time.sleep(0.8)
            self.open(x)
            self.assertEqual(b.js(tick), "true")
            b.js("document.getElementById('w-listened').click()")
            r.wait(lambda: not r.q("SELECT 1 FROM listened WHERE user_id = ? AND paper_id = ?", self.alice, x), 5, "unticked")
        finally:
            r.q("DELETE FROM listened")

    def test_3_versions_in_one_window(self):
        b, r = self.b, self.r
        src = "document.getElementById('audio').getAttribute('src')"
        checked = "[...document.querySelectorAll('#vlist .ver')].map(v => v.getAttribute('aria-checked') + ' ' + v.textContent)"
        try:
            # Carol made neither: the first made plays
            self.home(C)
            self.open(self.two)
            b.wait_js(f"{src} === '/audio/{self.va}.mp3'", 5, "Alice's version loaded")
            self.assertEqual(self.text("#w-maker"), "by Alice · derivations")
            self.assertEqual(b.js(checked), ["true by Alicederivations1 min", "false by Bobpractical1 min"])
            # she picks Bob's: the player and the window follow, and it is remembered
            b.js(f"document.querySelector('#vlist .ver[data-ep=\"{self.vb}\"]').click()")
            b.wait_js(f"{src} === '/audio/{self.vb}.mp3'", 5, "Bob's version loaded")
            self.assertEqual(self.text("#w-maker"), "by Bob · practical")
            self.assertEqual(self.text("#bar-sub"), "by Bob · practical")
            self.load(f"p={self.two}")
            b.wait_js(f"{src} === '/audio/{self.vb}.mp3'", 5, "the pick kept")
            self.shot("desktop-versions")
            # Bob: his own version plays, marked as his
            self.home(B)
            self.open(self.two)
            b.wait_js(f"{src} === '/audio/{self.vb}.mp3'", 5, "Bob's own")
            self.assertEqual(b.js(checked), ["false by Alicederivations1 min", "true by Bob (you)practical1 min"])
            # and the explainer opens the version that plays
            b.js("document.getElementById('x-open').click()")
            b.wait_js("!document.getElementById('overlay').hidden", 3, "explainer")
            self.assertEqual(b.js("document.getElementById('x-frame').getAttribute('src')"), f"/x/{self.vb}/explainer.html")
            self.assertEqual(b.js("document.getElementById('x-frame').getAttribute('sandbox')"), "allow-scripts allow-popups")
            b.js("document.getElementById('x-close').click()")
        finally:
            b.js("localStorage.clear()")

    def test_4_positions_reach_the_hub(self):
        b, r = self.b, self.r
        audio = "document.getElementById('audio')"
        pos = lambda uid, eid: r.q("SELECT seconds FROM positions WHERE user_id = ? AND episode_id = ?", uid, eid)
        try:
            self.home(C)
            self.open(self.two)
            b.wait_js(f"{audio}.getAttribute('src') === '/audio/{self.va}.mp3' && {audio}.duration > 20", 10, "loaded")
            b.js(f"{audio}.muted = true; document.getElementById('p-play').click()")
            b.wait_js(f"{audio}.currentTime > 1.5", 10, "playing")
            b.js("document.getElementById('p-play').click()")
            at = b.wait_js(f"{audio}.paused && {audio}.currentTime", 5, "paused")
            # the first save came as it started; the pause's is the one that stays
            r.wait(lambda: pos(self.carol, self.va) and abs(pos(self.carol, self.va)[0][0] - at) < 0.3, 5, "the position on the hub")
            self.assertEqual(pos(self.bob, self.va), [])                          # Carol's alone
            # its version says how much is left
            b.wait_js(f"document.querySelector('#vlist .ver[data-ep=\"{self.va}\"] .v-st').textContent === '1 min left'", 5, "left")
            # a fresh load resumes where she paused
            self.load(f"p={self.two}")
            b.wait_js(f"{audio}.readyState >= 1 && Math.abs({audio}.currentTime - {at}) < 0.6", 10, "resumed from the hub")
            # the device's own copy wins when it is newer (the hub did not get the last save)
            b.js(f"localStorage.setItem('pcg.{self.carol}.pos.{self.va}', JSON.stringify({{s: 20, at: Date.now()}}))")
            self.load(f"p={self.two}")
            b.wait_js(f"{audio}.readyState >= 1 && Math.abs({audio}.currentTime - 20) < 0.6", 10, "resumed from the device")
            # ...and the hub's when that is newer (her phone got further, a moment later)
            hub_at = int(time.time() * 1000) + 1000
            r.req("PUT", f"/api/episodes/{self.va}/position", {"s": 7, "at": hub_at}, user=C)
            self.load(f"p={self.two}")
            b.wait_js(f"{audio}.readyState >= 1 && Math.abs({audio}.currentTime - 7) < 0.6", 10, "resumed from the hub again")
            # a burst of seeks is one request, with the last place (made after the phone's)
            while time.time() * 1000 < hub_at + 200:
                time.sleep(0.05)
            n = len(self.log.lines)
            b.js("for (let i = 0; i < 4; i++) document.getElementById('p-back').click()")
            r.wait(lambda: pos(self.carol, self.va)[0][0] == 0, 5, "the seek stored")
            time.sleep(1.0)
            self.assertEqual(len(self.puts(n)), 1, self.puts(n))
            # Bob's place in the same episode is his own
            self.home(B)
            b.js(f"localStorage.setItem('pcg.{self.bob}.pick.{self.two}', '{self.va}')")
            self.open(self.two)
            b.wait_js(f"{audio}.getAttribute('src') === '/audio/{self.va}.mp3' && {audio}.readyState >= 1", 10, "Bob loaded")
            self.assertEqual(b.js(f"{audio}.currentTime"), 0)
        finally:
            b.js("localStorage.clear()")
            r.q("DELETE FROM positions")

    def test_5_settings(self):
        b, r = self.b, self.r
        tabs = "[...document.querySelectorAll('#set-tabs .tab')].map(t => t.textContent)"
        self.allow = [r"400 \(Bad Request\) \S+/api/(prefs|admin/base)$"]     # the refusals this test asks for
        try:
            # a viewer: preferences and devices only
            laptop = r.token(self.carol, "carol-laptop")
            r.token(self.carol, "carol-desktop")
            r.token(self.bob, "bob-laptop")
            self.home(C)
            b.js("document.getElementById('set-btn').click()")
            b.wait_js("!document.getElementById('settings').hidden && !!document.getElementById('pref-save')", 5, "settings")
            self.assertEqual(b.js(tabs), ["Preferences", "Devices"])
            self.assertEqual(b.js("location.hash"), "#settings")
            b.js("[...document.querySelectorAll('.pref[data-k=maths] .seg button')].find(x => x.dataset.v === 'full').click()")
            b.js("[...document.querySelectorAll('.pref[data-k=emphasis] .seg button')].find(x => x.dataset.v === 'practice').click()")
            b.js("{ const n = document.getElementById('pref-note'); n.value = 'More robots.'; n.dispatchEvent(new Event('input')); }")
            self.assertFalse(b.js("document.getElementById('pref-save').disabled"))
            b.js("document.getElementById('pref-save').click()")
            b.wait_js("document.getElementById('pref-msg').textContent === 'Saved'", 5, "saved")
            row = r.q("SELECT settings, note, version FROM prefs WHERE user_id = ?", self.carol)[0]
            self.assertEqual((json.loads(row[0]), row[1], row[2]),
                             ({"maths": "full", "emphasis": "practice", "background": "field"}, "More robots.", 1))
            self.assertEqual(self.text("#pref-summary"), "Your versions show as “by Carol · derivations · practical”.")
            self.assertTrue(b.js("document.getElementById('pref-save').disabled"))
            # a bad note is the hub's to refuse, shown where Save is
            b.js("{ const n = document.getElementById('pref-note'); n.removeAttribute('maxlength'); n.value = 'x'.repeat(501); n.dispatchEvent(new Event('input')); }")
            b.js("document.getElementById('pref-save').click()")
            b.wait_js("document.getElementById('pref-msg').className === 'err' && document.getElementById('pref-msg').textContent.includes('at most 500')", 5, "refused")
            # devices: hers only; Revoke asks once more, then it is gone
            b.js("document.getElementById('tab-devices').click()")
            b.wait_js("document.querySelectorAll('#devices .item').length === 2", 5, "devices")
            self.assertEqual(b.js("[...document.querySelectorAll('#devices .it-t')].map(x => x.textContent)"), ["carol-laptop", "carol-desktop"])
            rev = f"document.querySelector('#devices .item[data-id=\"{laptop}\"] button')"
            b.js(f"{rev}.click()")
            self.assertEqual(b.js(f"{rev}.textContent"), "Revoke now")
            self.assertIsNone(r.q("SELECT revoked_at FROM tokens WHERE id = ?", laptop)[0][0])
            b.js(f"{rev}.click()")
            b.wait_js("document.querySelectorAll('#devices .item').length === 1", 5, "revoked")
            self.assertIsNotNone(r.q("SELECT revoked_at FROM tokens WHERE id = ?", laptop)[0][0])
            # an admin: users and the base prompt as well
            self.home(A)
            b.js("location.hash = 'settings=users'")
            b.wait_js("document.querySelectorAll('#users .item').length === 3", 5, "users")
            self.assertEqual(b.js(tabs), ["Preferences", "Voice", "Devices", "Users", "Base prompt", "Slack"])
            self.assertIsNone(b.js("document.getElementById('invite-link')"))          # not the local sign-in
            sel = f"document.querySelector('#users .item[data-id=\"{self.bob}\"] select')"
            b.js(f"{sel}.value = 'admin'; {sel}.dispatchEvent(new Event('change'))")
            r.wait(lambda: r.q("SELECT role FROM users WHERE id = ?", self.bob)[0][0] == "admin", 5, "Bob an admin")
            b.js(f"{sel}.value = 'contributor'; {sel}.dispatchEvent(new Event('change'))")
            r.wait(lambda: r.q("SELECT role FROM users WHERE id = ?", self.bob)[0][0] == "contributor", 5, "Bob back")
            dis = f"document.querySelector('#users .item[data-id=\"{self.carol}\"] .lbox')"
            b.js(f"{dis}.click()")
            r.wait(lambda: r.q("SELECT disabled FROM users WHERE id = ?", self.carol)[0][0] == 1, 5, "Carol disabled")
            b.js(f"{dis}.click()")
            r.wait(lambda: r.q("SELECT disabled FROM users WHERE id = ?", self.carol)[0][0] == 0, 5, "Carol enabled")
            # the base prompt: none yet; v1; v2 built on v1; both kept
            b.js("document.getElementById('tab-base').click()")
            b.wait_js("!!document.getElementById('base-new')", 5, "base tab")
            self.assertIn("No base prompt yet.", b.js("document.getElementById('set-body').textContent"))
            b.js("document.getElementById('base-new').click()")
            b.js("{ const t = document.getElementById('base-text'); t.value = '# Base v1\\nSay it plainly.'; t.dispatchEvent(new Event('input')); }")
            b.js("document.getElementById('base-save').click()")
            b.wait_js("(document.getElementById('base-view') || {}).textContent === '# Base v1\\nSay it plainly.'", 5, "v1 shown")
            self.assertEqual([tuple(x) for x in r.q("SELECT version, guideline FROM base_prompts")], [(1, "# Base v1\nSay it plainly.")])
            b.js("document.getElementById('base-new').click()")
            self.assertEqual(b.js("document.getElementById('base-text').value"), "# Base v1\nSay it plainly.")
            self.assertIn('"classes"', b.js("document.getElementById('base-wording').value"))
            self.assertEqual(self.text("#base-save"), "Save as v2")
            b.js("{ const t = document.getElementById('base-wording'); t.value = '{\"classes\": []}'; }")
            b.js("document.getElementById('base-save').click()")
            b.wait_js("(document.getElementById('base-err') || {}).textContent.includes('classes')", 5, "bad wording refused")
            b.js("{ const t = document.getElementById('base-wording'); t.value = ''; const g = document.getElementById('base-text'); g.value = '# Base v2'; }")
            b.js("document.getElementById('base-save').click()")
            b.wait_js("(document.getElementById('base-view') || {}).textContent === '# Base v2'", 5, "v2 shown")
            self.assertEqual(b.js("[...document.querySelectorAll('.vers button')].map(x => x.textContent)"), ["v2", "v1"])
            self.assertIn("in use", self.text("#base-meta"))
            b.js("[...document.querySelectorAll('.vers button')].find(x => x.textContent === 'v1').click()")
            self.assertEqual(self.text("#base-view"), "# Base v1\nSay it plainly.")
            self.assertEqual(r.q("SELECT max(version) FROM base_prompts")[0][0], 2)
            self.shot("desktop-settings-base")
            # invites, with the local sign-in
            r.cfg.auth = "local"
            self.load("settings=users")
            b.wait_js("document.querySelectorAll('#users .item').length === 3", 5, "users again")
            b.js("[...document.querySelectorAll('#set-body .btn-accent')].find(x => x.textContent === 'Make an invite link').click()")
            b.wait_js("!document.getElementById('invite-link').hidden", 5, "invite link")
            self.assertRegex(b.js("document.getElementById('invite-link').value"), r"/join/[\w-]+$")
            # back to the graph
            b.js("document.getElementById('set-btn').click()")
            b.wait_js("document.getElementById('settings').hidden && /^#g=/.test(location.hash)", 5, "closed")
        finally:
            r.cfg.auth = "header"
            r.q("DELETE FROM prefs")
            r.q("DELETE FROM base_prompts")
            r.q("DELETE FROM tokens")

    def test_6_explainer_cannot_reach_anything(self):
        """A hostile explainer: first the control (the page's CSP bypassed, a frame without the
        sandbox), where it reads the cookie and ticks Listened through the API; then as the hub
        serves it (in the overlay, and opened directly), where every attempt fails and nothing
        reaches the server."""
        b, r = self.b, self.r
        f = r.cfg.episodes / self.va / "explainer.html"
        keep = f.read_text()
        f.write_text(HOSTILE.replace("__PID__", self.two).replace("__EID__", self.va))
        # the frame's own refusals are expected here, and so is the control's navigation
        self.allow = [r"Content Security Policy", r"sandbox", r"h=1", r"/x/", r"Blocked"]
        listen = "window.__hostile = null; window.addEventListener('message', (e) => { if (e.data && e.data.hostile) window.__hostile = e.data.hostile; });"
        results = lambda: json.loads(b.wait_js("window.__hostile && JSON.stringify(window.__hostile)", 15, "hostile results"))
        hits = lambda since: [ln for ln in self.log.lines[since:] if "h=1" in ln]
        try:
            self.as_user(C)
            # 1. the control
            # (without the form: a form's POST to no route is left unread by app.py, see the report)
            f.write_text(HOSTILE.replace("__PID__", self.two).replace("__EID__", self.va).replace('document.getElementById("f").submit();', ""))
            b.call("Page.setBypassCSP", enabled=True)
            try:
                self.home(C)
                b.js(listen)
                b.js(f"const f = document.createElement('iframe'); f.src = '/x/{self.va}/explainer.html'; document.body.append(f);")
                got = results()
            finally:
                b.call("Page.setBypassCSP", enabled=False)
            for a in ATTACKS:
                if a not in SERVER_SIDE:
                    self.assertTrue(got[a]["ok"], f"control: {a} should succeed without the sandbox: {got[a]}")
            self.assertIn("pcg_test_cookie=secret", got["cookie_read"]["value"])
            r.wait(lambda: r.q("SELECT 1 FROM listened WHERE user_id = ? AND paper_id = ?", self.carol, self.two), 5, "control: the tick went through")
            time.sleep(1.5)
            r.q("DELETE FROM listened")
            self.close_popups()
            # 2. as the hub serves it: in the overlay, then opened directly
            f.write_text(HOSTILE.replace("__PID__", self.two).replace("__EID__", self.va))
            for how in ("overlay", "direct"):
                n = len(self.log.lines)
                if how == "overlay":
                    self.home(C)
                    b.js(listen)
                    self.open(self.two)
                    b.wait_js("!document.getElementById('x-open').disabled", 5, "explainer button")
                    b.js("document.getElementById('x-open').click()")
                    self.assertEqual(b.js("document.getElementById('x-frame').getAttribute('sandbox')"), "allow-scripts allow-popups")
                    got = results()
                else:
                    b.goto(f"{self.base}/x/{self.va}/explainer.html")
                    got = json.loads(b.wait_js("document.getElementById('result').textContent !== 'running' && document.getElementById('result').textContent", 15, "direct results"))
                time.sleep(1.5)                     # the popup, the top navigation and the form land
                self.close_popups()
                for a in ATTACKS:
                    if a in SERVER_SIDE or (how == "direct" and a == "parent_dom"):
                        continue
                    self.assertFalse(got[a]["ok"], f"{how}: {a} must fail: {got[a]}")
                self.assertIn("SecurityError", got["cookie_read"]["error"])
                if how == "overlay":
                    self.assertEqual(b.js("location.pathname"), "/")        # the top was not navigated
                self.assertEqual(r.q("SELECT count(*) FROM listened")[0][0], 0)
                self.assertEqual(r.q("SELECT count(*) FROM episodes WHERE deleted_at IS NOT NULL")[0][0], 0)
                # nothing reached the hub but, at most, a popup's plain GET
                self.assertEqual([ln for ln in hits(n) if "popup=1" not in ln or '"GET ' not in ln], [], how)
        finally:
            f.write_text(keep)
            r.q("DELETE FROM listened")
            self.close_popups()

    def close_popups(self):
        import urllib.request
        for t in json.loads(urllib.request.urlopen(f"{self.b.http}/json/list", timeout=5).read()):
            if t["type"] == "page" and t["id"] != self.b.page_id:
                urllib.request.urlopen(f"{self.b.http}/json/close/{t['id']}", timeout=5).read()

    def test_7_delete_a_version_with_undo(self):
        b, r = self.b, self.r
        deleted = lambda eid: r.q("SELECT deleted_at FROM episodes WHERE id = ?", eid)[0][0]
        items = "[...document.querySelectorAll('.menu [role=menuitem]')].map(x => x.textContent)"
        try:
            # Carol made nothing: no Delete in the window's menu
            self.home(C)
            self.open(self.two)
            b.js("document.getElementById('w-more').click()")
            b.wait_js("!!document.querySelector('.menu')", 3, "menu")
            self.assertEqual(b.js(items), ["Play next", "Add to Up next", "Details", "Open paper link"])
            b.js("document.getElementById('w-more').click()")
            # Bob deletes his only version of a paper: it leaves the library, its window closes; Undo brings it back
            self.home(B)
            self.open(self.wait)
            b.js("document.getElementById('w-more').click()")
            b.wait_js("!!document.querySelector('.menu')", 3, "menu")
            self.assertEqual(b.js(items), ["Details", "Delete my version"])
            b.js("[...document.querySelectorAll('.menu button')].find(x => x.textContent === 'Delete my version').click()")
            b.wait_js("document.getElementById('paper').hidden && !document.getElementById('toast').hidden", 5, "deleted")
            self.assertEqual(self.text("#toast-msg"), "Deleted “A Fake Paper Waiting For…”")
            r.wait(lambda: deleted(self.ew), 5, "deleted on the hub")
            b.js("document.getElementById('toast-act').click()")
            r.wait(lambda: deleted(self.ew) is None, 5, "undone on the hub")
            self.open(self.wait, "back in the library")
            # his version of the two: the paper stays, with Alice's alone
            self.open(self.two)
            b.js("document.getElementById('w-more').click()")
            b.js("[...document.querySelectorAll('.menu button')].find(x => x.textContent === 'Delete my version').click()")
            b.wait_js("document.getElementById('versions').hidden && document.getElementById('w-maker').textContent === 'by Alice · derivations'"
                      " && !document.getElementById('toast').hidden && document.getElementById('toast-msg').textContent === 'Deleted your version'", 5, "one version left")
            b.js("document.getElementById('toast-act').click()")
            b.wait_js("!document.getElementById('versions').hidden && document.querySelectorAll('#vlist .ver').length === 2", 5, "undone")
            # an admin may delete Bob's
            self.home(A)
            self.open(self.two)
            b.js(f"document.querySelector('#vlist .ver[data-ep=\"{self.vb}\"]').click()")
            b.js("document.getElementById('w-more').click()")
            self.assertIn("Delete Bob’s version", b.js("[...document.querySelectorAll('.menu button')].map(x => x.textContent)"))
            b.js("document.getElementById('w-more').click()")
        finally:
            b.js("localStorage.clear()")
            r.q("UPDATE episodes SET deleted_at = NULL")

    def test_8_live_events(self):
        """A version voiced on the hub, a new paper, a resync: the window and the graph list
        follow without a reload."""
        b, r = self.b, self.r
        new = None
        none_n = f"Number(document.querySelector('{GROW.format('none')} .gl-n').textContent)"
        try:
            self.home(C)
            self.open(self.wait)
            b.wait_js("!document.getElementById('w-state').hidden", 5, "waiting")
            (r.cfg.episodes / self.ew / "audio.mp3").write_bytes((r.cfg.episodes / self.va / "audio.mp3").read_bytes())
            r.q("UPDATE episodes SET state = 'ready', duration_s = 600 WHERE id = ?", self.ew)
            publish("episode", {"id": self.ew, "state": "ready"})           # no paper id: the page knows it
            b.wait_js("document.getElementById('w-state').hidden && !document.getElementById('w-listened').hidden", 5, "voiced")
            # a new paper someone just uploaded, in no graph: Not in any graph has one more
            n0 = b.js(none_n)
            new = r.paper("A Brand New Fake Paper", self.bob)
            r.episode(new, self.bob, state="checking")
            publish("episode", {"id": "e_unknown00000", "paper_id": new, "state": "checking"})
            publish("paper", {"id": new, "new": True})
            b.wait_js(f"{none_n} === {n0 + 1}", 5, "one more paper in no graph")
            self.open(new)
            self.assertEqual(b.js("document.querySelector('#w-state .big').textContent"), "Checking…")
            # resync: the library again
            self.open(self.speak)
            r.q("UPDATE papers SET title = 'Renamed Fake Paper' WHERE id = ?", self.speak)
            publish("resync", {})
            b.wait_js("document.getElementById('w-title').textContent === 'Renamed Fake Paper'", 5, "resynced")
        finally:
            r.q("UPDATE episodes SET state = 'waiting-for-gpu', duration_s = NULL WHERE id = ?", self.ew)
            (r.cfg.episodes / self.ew / "audio.mp3").unlink(missing_ok=True)
            r.q("UPDATE papers SET title = 'A Fake Paper Being Spoken' WHERE id = ?", self.speak)
            if new:
                r.q("UPDATE episodes SET deleted_at = '2026-09-01T00:00:00Z' WHERE paper_id = ?", new)

    def test_9_the_map_is_the_home(self):
        """map.js is mounted at the start, beside the column, with the graph the page landed on;
        no Map button."""
        b = self.b
        self.home(A)
        self.assertIsNone(b.js("document.getElementById('map-btn')"))
        b.wait_js("!document.getElementById('map').hidden && !!document.querySelector('#map canvas')", 10, "map mounted")
        col, m = b.js("[document.getElementById('gcol'), document.getElementById('map')].map(e => (r => [r.left, r.right])(e.getBoundingClientRect()))")
        self.assertAlmostEqual(col[1], m[0], delta=1.5, msg="the map is not beside the column")
        self.assertEqual(b.js("PaperMap.current.debug.cur().id"), b.js("new URLSearchParams(location.hash.slice(1)).get('g')"))

    def test_y_the_page_at_each_size(self):
        """100%, 125% (the default) and 150% (theme.js's CSS zoom): the page fills the window and
        no more; a menu opens under its button, right edges aligned, on the screen; the seek bar's
        thumb is where the pointer presses and the position is the fraction pressed."""
        b = self.b
        self.addCleanup(b.repin_size)
        self.addCleanup(lambda: b.js("localStorage.removeItem('pcg-size')"))      # the class's other tests: the default
        for size in ("100", "125", "150"):
            with self.subTest(size=size):
                b.pin_size(size)
                self.load()
                b.wait_js("document.querySelectorAll('#gl .gl-row').length > 0", 10, "page started")
                z = size and int(size) / 100
                self.assertEqual(b.js("document.documentElement.currentCSSZoom"), z)
                self.no_side_scroll(f"{size}%")
                self.open(self.two)
                b.wait_js("!document.getElementById('bar').hidden", 5, "the bar")
                # the column, the map and the window, then the player bar under them: the window, and no more
                a = b.js("[document.querySelector('.app'), document.getElementById('bar')].map(e => (r => [r.left, r.top, r.right, r.bottom])(e.getBoundingClientRect()))")
                self.assertEqual([round(v) for v in a[0][:3] + a[1][2:]], [0, 0, 1440, 1440, 900], "the page does not fill the window")
                self.assertAlmostEqual(a[0][3], a[1][1], delta=1, msg="the window does not end at the bar")
                # the window's ⋯ menu: under the button, its right edge on the button's
                b.js("document.getElementById('w-more').click()")
                b.wait_js("!!document.querySelector('.menu')", 3, "menu")
                m, k = b.js("[document.querySelector('.menu'), document.getElementById('w-more')].map(e => (r => [r.left, r.top, r.right, r.bottom])(e.getBoundingClientRect()))")
                self.assertAlmostEqual(m[2], k[2], delta=1, msg=f"{size}%: the menu {m} is not under its button {k}")
                self.assertAlmostEqual(m[1], k[3] + 4 * z, delta=1, msg=f"{size}%: the menu {m} is not under its button {k}")
                self.assertTrue(m[0] >= 0 and m[3] <= 900, f"{size}%: the menu {m} is off the screen")
                b.js("document.getElementById('w-more').click()")
                # the seek bar: pressed at a quarter, the thumb under the pointer; released, a quarter in
                x0, y0, w = b.js("(r => [r.left, r.top + r.height / 2, r.width])(document.querySelector('#scrub .track').getBoundingClientRect())")
                x = x0 + w / 4
                b.call("Input.dispatchMouseEvent", type="mousePressed", x=x, y=y0, button="left", clickCount=1)
                b.wait_js("(t => Math.abs((t.left + t.right) / 2 - %s) < 1.5)(document.getElementById('thumb').getBoundingClientRect())" % x, 3,
                          f"{size}%: the thumb under the pointer")
                b.call("Input.dispatchMouseEvent", type="mouseReleased", x=x, y=y0, button="left", clickCount=1)
                b.wait_js("Math.abs(Number(document.getElementById('scrub').getAttribute('aria-valuenow')) - 7.5) <= 0.6", 5, f"{size}%: a quarter of 30 s")
                b.js("(a => a && a.pause())(document.querySelector('audio'))")
        # the phone at 125% (390 / 340 = 1.147 at most, so the page keeps 340 CSS px): nothing
        # scrolls sideways, on the list of graphs, a graph's papers, or a paper
        self.addCleanup(lambda: b.viewport(1440, 900))
        self.phone()
        b.pin_size("125")
        self.load()
        b.wait_js("document.querySelectorAll('#gl .gl-row').length > 0 && document.body.classList.contains('gpl-open')", 10, "page started")
        z = b.js("document.documentElement.currentCSSZoom")
        self.assertAlmostEqual(z, 1.147, places=3)
        self.no_side_scroll("a graph's papers, the phone at 125%")
        b.js("document.getElementById('gpl-back').click()")
        b.wait_js("!document.body.classList.contains('gpl-open')", 5, "the list of graphs")
        self.no_side_scroll("the list of graphs, the phone at 125%")
        self.open(self.two)
        self.no_side_scroll("a paper, the phone at 125%")

    def test_z_phone_tap_targets(self):
        """On a phone every control, in every state, takes a 44 x 44 px tap that reaches it."""
        b, r = self.b, self.r
        try:
            self.phone()
            self.home(A)
            # where it lands: a graph's papers, over the list of graphs
            b.wait_js("document.body.classList.contains('gpl-open') && document.querySelectorAll('#gpl-rows .gpl-row').length > 0", 5, "a graph's papers")
            self.assertTargets("a graph's papers")
            self.no_side_scroll("a graph's papers")
            b.js("document.getElementById('gpl-back').click()")
            b.wait_js("!document.body.classList.contains('gpl-open')", 5, "the list of graphs")
            self.assertTargets("the list of graphs")
            self.no_side_scroll("the list of graphs")
            b.js(f"document.querySelector('{GROW.format('none')} .gl-open').click()")
            b.wait_js("document.body.classList.contains('gpl-open') && document.querySelectorAll('#gpl-rows .gpl-row').length > 100", 5, "Not in any graph")
            self.assertTargets("Not in any graph's papers")
            b.js("document.getElementById('gpl-map').click()")
            b.wait_js("!document.getElementById('map').hidden && !!PaperMap.current.debug.cur()._s", 5, "the drawing")
            self.assertTargets("the drawing")
            b.js("history.back()")
            b.wait_js("document.getElementById('map').hidden", 5, "back to the papers")
            self.open(self.two)
            b.wait_js("getComputedStyle(document.getElementById('win')).visibility === 'visible' && document.getElementById('audio').duration > 20", 10, "window")
            self.assertTargets("a paper with two versions")
            self.no_side_scroll("a paper with two versions")
            self.shot("phone-versions")
            b.js("document.getElementById('w-more-phone').click()")
            b.wait_js("!!document.querySelector('.menu')", 3, "menu")
            self.assertTargets("the window's menu")
            b.js("[...document.querySelectorAll('.menu button')].find(x => x.textContent === 'Details').click()")
            self.assertTargets("with its details")
            b.js("document.getElementById('x-open').click()")
            b.wait_js("!document.getElementById('overlay').hidden", 3, "explainer")
            self.assertTargets("the explainer")
            b.js("document.getElementById('x-close').click()")
            self.open(self.wait)
            b.wait_js("!document.getElementById('w-state').hidden", 5, "waiting")
            self.assertTargets("a paper waiting for the GPU")
            # playing, paused, back on the list: the player bar, one line, then opened
            self.open(self.two)
            b.js("document.getElementById('audio').muted = true; document.getElementById('p-play').click()")
            b.wait_js("document.getElementById('audio').currentTime > 0.5", 10, "playing")
            b.js("document.getElementById('p-play').click()")
            b.js("document.getElementById('back').click()")
            b.wait_js("!document.getElementById('bar').hidden"
                      " && getComputedStyle(document.getElementById('win')).visibility === 'hidden'", 5, "the bar on the list")
            self.assertTargets("the player bar")
            self.shot("phone-list")
            b.js("document.getElementById('bar-exp').click()")
            b.wait_js("document.getElementById('bar').classList.contains('open') && document.getElementById('scrub').getClientRects().length > 0"
                      " && document.getElementById('p-speed').getClientRects().length > 0", 3, "the bar opened")
            self.assertTargets("the player bar, opened")
            self.no_side_scroll("the player bar, opened")
            b.js("document.getElementById('bar-exp').click()")
            # settings, every tab
            r.token(self.alice, "alice-laptop")
            for t in ("prefs", "devices", "users", "base"):
                b.js(f"location.hash = 'settings={t}'")
                b.wait_js(f"document.getElementById('tab-{t}') && document.getElementById('tab-{t}').getAttribute('aria-selected') === 'true'"
                          " && !document.getElementById('set-loading')"
                          " && getComputedStyle(document.getElementById('win')).visibility === 'visible'", 5, t)
                self.assertTargets(f"settings, {t}")
                self.no_side_scroll(f"settings, {t}")
                self.shot(f"phone-settings-{t}")
            b.js("document.getElementById('base-new').click()")
            self.assertTargets("settings, a new base prompt")
            b.js("document.getElementById('set-back').click()")
            # the Undo toast after a delete from the menu
            self.open(self.many[119])
            b.js("document.getElementById('w-more-phone').click()")
            b.js("[...document.querySelectorAll('.menu button')].find(x => x.textContent === 'Delete my version').click()")
            b.wait_js("!document.getElementById('toast').hidden && getComputedStyle(document.getElementById('win')).visibility === 'hidden'", 5, "toast")
            self.assertTargets("the Undo toast")
            b.js("document.getElementById('toast-act').click()")
            r.wait(lambda: r.q("SELECT deleted_at FROM episodes WHERE paper_id = ?", self.many[119])[0][0] is None, 5, "undone")
        finally:
            b.viewport(1440, 900)
            b.js("localStorage.clear()")
            r.q("UPDATE episodes SET deleted_at = NULL")
            r.q("DELETE FROM positions")
            r.q("DELETE FROM tokens")


FAKE_MAP = """// a stand-in for map.js (A6's): records how it was mounted, and plays the page's part of it
window.PaperMap = { mount: function (host, opts) {
  var cur = null;
  window.__map = { opts: opts, events: [], changed: 0, graphs: [] };
  host.textContent = "";
  var b = document.createElement("button");
  b.type = "button"; b.id = "fake-open"; b.textContent = "Open"; host.appendChild(b);
  b.addEventListener("click", function () { opts.onOpen(Array.from(opts.papers.keys())[0]); });
  function list() {
    fetch("/api/graphs", { credentials: "same-origin" }).then(function (r) { return r.json(); }).then(function (r) { opts.onList(r); });
  }
  list();
  return { show: function () {}, hide: function () {}, changed: function () { window.__map.changed++; },
           event: function (k, d) { window.__map.events.push(k); },
           graph: function (gid) { cur = gid; window.__map.graphs.push(gid); if (opts.onShow) opts.onShow(gid); },
           current: function () { return cur; }, select: function () {}, refreshList: list, data: function () { return null; },
           inset: function () {} };
} };
"""


@unittest.skipIf(SKIP, SKIP or "")
class Map(PageBase):
    """With map.js there: mounted with the group's options at the start, opens papers, hears the events."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="pcg-static-")
        cls.static = Path(cls.tmp.name) / "static"
        shutil.copytree(STATIC, cls.static)
        (cls.static / "map.js").write_text(FAKE_MAP)
        (cls.static / "map.css").write_text("#map { position: fixed; inset: 0 0 0 340px; background: #fff; }\n")
        super().setUpClass()

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        cls.tmp.cleanup()

    @classmethod
    def fill(cls):
        super().fill()
        cls.papers = []
        for i in range(3):
            p = cls.r.paper(f"Mapped fake paper {i}", cls.alice)
            cls.r.episode(p, cls.alice)
            cls.papers.append(p)

    def test_map_mounts_with_the_group_options(self):
        b = self.b
        self.as_user(A)
        self.r.q("DELETE FROM ui_state")
        b.goto("about:blank")
        b.goto(self.base + "/")
        b.wait_js("!!window.__map && /^#g=/.test(location.hash)", 5, "mounted, and landed")
        o = json.loads(b.js("JSON.stringify({api: __map.opts.api, column: __map.opts.column, live: __map.opts.live, editable: __map.opts.editable, me: __map.opts.me,"
                            " papers: __map.opts.papers instanceof Map ? __map.opts.papers.size : -1, head: __map.opts.head && __map.opts.head.id,"
                            " fns: ['onOpen', 'onClose', 'onList', 'onShow', 'onData'].map(k => typeof __map.opts[k])})"))
        self.assertEqual(o, {"api": "", "column": True, "live": True, "editable": True, "papers": 3, "head": "gh", "fns": ["function"] * 5,
                             "me": {"id": self.alice, "name": "Alice", "email": A, "role": "admin", "avatar": None}})
        # no graph has a paper: it lands where the most papers are (all five have none: the first by name), never prefetching
        self.assertEqual(b.js("__map.graphs.length"), 1)
        publish("graph", {"id": "g_test"})
        publish("log", {"id": 1})
        b.wait_js("__map.events.includes('graph') && __map.events.includes('log')", 5, "graph events to the map")
        self.r.req("PUT", f"/api/papers/{self.papers[0]}/listened", {"listened": True}, user=A)
        b.wait_js("__map.changed > 0 && __map.opts.papers.get(" + json.dumps(self.papers[0]) + ").listened === true", 5, "a tick reaches the map")
        b.js("document.getElementById('fake-open').click()")
        b.wait_js(f"location.hash.endsWith('&p={self.papers[2]}') && !document.getElementById('paper').hidden", 5, "opened from the map")
        self.r.q("DELETE FROM listened")


if __name__ == "__main__":
    unittest.main()
