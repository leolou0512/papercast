#!/usr/bin/env python3
"""The search and the filters on the page (hub/static), in a real headless Chrome, against the
real hub (PCG_AUTH=header, as test_page.py).

    python3 -m unittest discover -s stacks/papercast-group/hub/tests -p 'test_search_page.py' -v

Results as you type, with where each paper matched and the match marked (DOM nodes: a paper
text holding <script> shows it as text and runs nothing); a clear button, "/" to the search,
Escape to empty it; the count; "Showing results for" a corrected word; snippets for rows past
the first ones; the filter section (topic, tag, maker, years, listened) with one chip per
filter, a tap on a chip removing it; filters with and without a query and with the sorts; the
search and filters kept over a reload in the tab; on a phone every control a 44 px tap. No
test may leave an error in the console (a CSP violation is one). Screenshots go to
$PCG_TEST_SHOTS if set. Fake titles and people only."""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from test_page import A, ROW, ROWS, SKIP, PageBase, js_list  # noqa: E402

from hub import search  # noqa: E402

TYPE = "{ const s = document.getElementById('search'); s.value = %s; s.dispatchEvent(new Event('input')); }"
HITS = "[...document.querySelectorAll('#rows .row .row-hit')].length"
CHIPS = "[...document.querySelectorAll('#chips .chip .chip-t')].map(x => x.textContent)"
STAT = "document.getElementById('sstat').textContent"


@unittest.skipIf(SKIP, SKIP or "")
class SearchPage(PageBase):
    @classmethod
    def fill(cls):
        super().fill()
        r = cls.r

        def paper(title, by, year=2022, tags=(), script=None, text=None):
            pid = r.paper(title, by, year=year, tags=tags)
            eid = r.episode(pid, by, audio_s=1.0)
            d = r.cfg.episodes / eid
            (d / "script.md").write_text(script or "# A fake episode\n\nIt explains a made-up idea step by step.\n", encoding="utf-8")
            (d / "claims.md").write_text("- A fake claim.\n", encoding="utf-8")
            (d / "explainer.json").write_text(json.dumps({"points": ["A fake point."], "figures": []}), encoding="utf-8")
            if text is not None:
                (d / "paper.txt").write_text(text, encoding="utf-8")
            return pid

        # forty papers whose episodes talk about wombats, tagged for the graph's rule
        cls.wombats = [paper(f"Fake Marsupial Study {i:02d}", cls.alice if i % 3 else cls.bob, year=2000 + i % 20,
                             tags=["wombats"] if i % 2 else ["other animals"],
                             script="# A fake episode\n\nThe wombat digs a burrow, and the burrow keeps it cool.\n")
                       for i in range(40)]
        cls.diff = paper("Denoising Diffusion For Fake Pictures", cls.alice, year=2020, tags=["generative models"])
        cls.xss = paper("A Hostile Fake Text", cls.carol, year=2019,
                        text='Before <b>it</b> the zebrafinch <script>window.__xss = 1</script><img src=x onerror="window.__xss = 2"> sings.')
        search.sync(cls.r.cfg, fuzzy=True)
        st, g, _ = r.req("POST", "/api/graphs", {"name": "Wombat Topics", "tags": ["wombats"]})
        assert st == 201, g
        cls.gid = g["graph"]["id"]
        cls.members = [p for i, p in enumerate(cls.wombats) if i % 2]
        search.sync(cls.r.cfg, fuzzy=True)

    def type(self, q):
        self.b.js(TYPE % json.dumps(q))

    def rows_are(self, ids, what):
        self.b.wait_js(f"JSON.stringify({ROWS}) === {js_list(ids)}", 8, what)

    def key(self, key, code=None, text=None):
        b = self.b
        kw = {"key": key, "code": code or key, "windowsVirtualKeyCode": {"Escape": 27, "/": 191, "Enter": 13}.get(key, 0)}
        b.call("Input.dispatchKeyEvent", type="keyDown", **kw, **({"text": text} if text else {}))
        b.call("Input.dispatchKeyEvent", type="keyUp", **kw)

    # ------------------------------------------------------------ the box
    def test_1_results_as_you_type_with_marks(self):
        b = self.b
        self.home(A)
        self.assertTrue(b.js("document.getElementById('search-clear').hidden"))
        self.assertTrue(b.js("document.getElementById('sstat').hidden"))
        self.type("zebr")                                         # a prefix, in the paper's own text
        self.rows_are([self.xss], "the hostile paper")
        b.wait_js(f"{STAT} === '1 paper'", 5, "the count")
        hit = ROW.format(self.xss) + " .row-hit"
        b.wait_js(f"!!document.querySelector('{hit}')", 5, "the snippet")
        self.assertEqual(self.text(hit + " .lead"), "in the paper: ")
        self.assertEqual(b.js(f"[...document.querySelectorAll('{hit} mark')].map(m => m.textContent)"), ["zebrafinch"])
        # the text's markup is text: shown, never run
        self.assertEqual(self.text(hit), 'in the paper: Before <b>it</b> the zebrafinch <script>window.__xss = 1</script>'
                         '<img src=x onerror="window.__xss = 2"> sings.')
        self.assertEqual(b.js("document.querySelectorAll('#rows script, #rows img, #rows b').length"), 0)
        self.assertIsNone(b.js("window.__xss === undefined ? null : window.__xss"))
        self.assertFalse(b.js("document.getElementById('search-clear').hidden"))
        # a title's match is marked in the title
        self.type("denoising diffusion")
        self.rows_are([self.diff], "by the title")
        self.assertEqual(b.js(f"[...document.querySelectorAll('{ROW.format(self.diff)} .row-title mark')].map(m => m.textContent)"),
                         ["Denoising", "Diffusion"])
        self.assertEqual(self.text(ROW.format(self.diff) + " .row-title"), "Denoising Diffusion For Fake Pictures")
        self.shot("search-desktop")
        # the clear button empties it
        b.js("document.getElementById('search-clear').click()")
        b.wait_js(f"{ROWS}.length === 42 && document.getElementById('search').value === ''"
                  " && document.getElementById('sstat').hidden && !document.querySelector('#rows mark')", 5, "cleared")
        self.assertEqual(b.js("document.activeElement.id"), "search")

    def test_2_slash_focuses_and_escape_empties(self):
        b = self.b
        self.home(A)
        b.js("document.activeElement.blur()")
        self.key("/", "Slash", "/")
        b.wait_js("document.activeElement && document.activeElement.id === 'search'", 3, "/ to the search")
        self.assertEqual(b.js("document.getElementById('search').value"), "")        # the / is not typed
        b.call("Input.insertText", text="wombat")
        b.wait_js(f"{ROWS}.length === 40", 5, "typed")
        self.key("Escape")
        b.wait_js(f"document.getElementById('search').value === '' && {ROWS}.length === 42", 5, "Escape empties it")
        self.assertEqual(b.js("document.activeElement.id"), "search")
        self.key("Escape")
        self.assertNotEqual(b.js("document.activeElement && document.activeElement.id"), "search")
        # "/" while typing somewhere else is a /
        self.open(self.diff)
        b.js("location.hash = 'settings=prefs'")
        b.wait_js("!!document.getElementById('pref-note')", 5, "settings")
        b.js("document.getElementById('pref-note').focus()")
        self.key("/", "Slash", "/")
        self.assertEqual(b.js("document.activeElement.id"), "pref-note")
        self.assertEqual(b.js("document.getElementById('pref-note').value"), "/")

    def test_3_a_corrected_word_says_so(self):
        b = self.b
        self.home(A)
        self.type("difusion ")
        self.rows_are([self.diff], "corrected")
        b.wait_js(f"{STAT}.startsWith('Showing results for diffusion')", 5, "Showing results for")
        self.assertEqual(b.js("document.querySelector('#sstat b').textContent"), "diffusion")
        self.assertIn("1 paper", self.text("#sstat"))

    def test_4_snippets_for_every_row(self):
        """The first rows come with their snippets, the rest are asked for as they are drawn."""
        b = self.b
        self.home(A)
        self.type("wombat")
        b.wait_js(f"{ROWS}.length === 40 && {STAT} === '40 papers'", 8, "forty")
        b.wait_js(f"{HITS} === 40", 8, "every row's snippet")
        texts = b.js("[...document.querySelectorAll('#rows .row .row-hit')].map(x => x.textContent)")
        self.assertTrue(all(t.startswith(("in the episode: ", "in the tags: ")) and "wombat" in t for t in texts), texts[:3])
        self.assertEqual(sum(t.startswith("in the tags: ") for t in texts), 20)       # the tag wins where there is one
        # the best matches first; another sort is there, and Best match comes back
        b.js("document.getElementById('sort-btn').click()")
        items = b.js("[...document.querySelectorAll('.menu button')].map(x => [x.textContent, x.getAttribute('aria-checked')])")
        self.assertEqual(items[0], ["Best match", "true"])
        b.js("[...document.querySelectorAll('.menu button')].find(x => x.textContent === 'Title A–Z').click()")
        self.rows_are(self.wombats, "by title")
        self.assertIn("Title", b.js("document.getElementById('sort-btn').getAttribute('aria-label')"))
        b.js("document.getElementById('sort-btn').click()")
        b.js("[...document.querySelectorAll('.menu button')].find(x => x.textContent === 'Best match').click()")
        self.assertEqual(b.js("document.getElementById('sort-btn').getAttribute('aria-label')"), "Sort: Best match")
        b.js("localStorage.removeItem('pcg.sort')")

    # ------------------------------------------------------------ filters
    def open_panel(self):
        b = self.b
        b.js("document.getElementById('filter-btn').click()")
        b.wait_js("!document.getElementById('fpanel').hidden && !!document.getElementById('f-graph')"
                  " && document.getElementById('f-graph').options.length > 1", 5, "the filter section")

    def choose(self, sel, value):
        self.b.js(f"{{ const s = document.getElementById({json.dumps(sel)}); s.value = {json.dumps(value)}; s.dispatchEvent(new Event('change')); }}")

    def test_5_filters_chips_and_a_reload(self):
        b, r = self.b, self.r
        self.home(A)
        self.open_panel()
        self.assertEqual(b.js("document.getElementById('filter-btn').getAttribute('aria-expanded')"), "true")
        opts = b.js("[...document.getElementById('f-graph').options].map(o => o.textContent)")
        self.assertIn("Wombat Topics", opts)
        self.assertEqual(b.js("document.getElementById('f-maker').options.length"), 4)    # Anyone, Alice, Bob, Carol
        self.choose("f-graph", self.gid)
        members = sorted(self.members, key=lambda p: self.wombats.index(p), reverse=True)     # newest first
        self.rows_are(members, "the graph's members")
        b.wait_js(f"JSON.stringify({CHIPS}) === {js_list(['Topic: Wombat Topics'])}", 5, "a chip")
        b.wait_js(f"{STAT} === '20 papers'", 5, "the count")
        # together with a maker and a year range, and with a search
        self.choose("f-maker", str(self.bob))
        self.choose("f-yfrom", "2010")
        want = [p for p in members if self.wombats.index(p) % 3 == 0 and 2000 + self.wombats.index(p) % 20 >= 2010]
        self.rows_are(want, "graph, maker and years")
        self.assertEqual(b.js(CHIPS), ["Topic: Wombat Topics", "By Bob", "From 2010"])
        self.type("burrow")
        self.rows_are(sorted(want, key=lambda p: -(2000 + self.wombats.index(p) % 20)), "and a search")
        self.shot("filters-desktop")
        # the tab keeps the search and the filters over a reload
        self.load()
        b.wait_js(f"JSON.stringify({CHIPS}) === {js_list(['Topic: Wombat Topics', 'By Bob', 'From 2010'])}", 8, "chips again")
        self.assertEqual(b.js("document.getElementById('search').value"), "burrow")
        b.wait_js(f"{ROWS}.length === {len(want)}", 8, "the same rows")
        self.assertEqual(b.js("document.getElementById('sort-btn').getAttribute('aria-label')"), "Sort: Best match")
        # a tap on a chip takes that filter away; Clear takes them all
        b.js("[...document.querySelectorAll('#chips .chip')].find(c => c.dataset.k === 'maker').click()")
        b.wait_js(f"JSON.stringify({CHIPS}) === {js_list(['Topic: Wombat Topics', 'From 2010'])}", 5, "one chip less")
        b.wait_js(f"{ROWS}.length === {len([p for p in members if 2000 + self.wombats.index(p) % 20 >= 2010])}", 5, "wider")
        b.js("document.getElementById('filter-clear').click()")
        b.wait_js(f"document.getElementById('filter').hidden && {ROWS}.length === 40", 5, "cleared, the search left")
        self.type("")
        b.wait_js(f"{ROWS}.length === 42", 5, "everything")

    def test_6_listened_and_the_tag_filter(self):
        b, r = self.b, self.r
        try:
            r.req("PUT", f"/api/papers/{self.diff}/listened", {"listened": True}, user=A)
            self.home(A)
            self.open_panel()
            b.js("[...document.querySelectorAll('#fpanel .f-seg button')].find(x => x.textContent === 'Listened').click()")
            self.rows_are([self.diff], "listened")
            self.assertEqual(b.js(CHIPS), ["Listened"])
            b.js("[...document.querySelectorAll('#fpanel .f-seg button')].find(x => x.textContent === 'Not listened').click()")
            b.wait_js(f"{ROWS}.length === 41 && JSON.stringify({CHIPS}) === {js_list(['Not listened'])}", 5, "not listened")
            # a row's tag is a filter too, with its chip
            b.js("document.getElementById('filter-clear').click()")
            b.wait_js(f"{ROWS}.length === 42", 5, "all")
            b.js(f"[...document.querySelectorAll('{ROW.format(self.diff)} .tag')].find(t => t.textContent === 'generative models').click()")
            self.rows_are([self.diff], "the tag")
            self.assertEqual(b.js(CHIPS), ["Tag: generative models"])
            self.assertEqual(b.js("document.getElementById('f-tag').value"), "generative models")   # the open section follows
            b.js("document.querySelector('#chips .chip').click()")
            b.wait_js(f"{ROWS}.length === 42 && document.getElementById('filter').hidden", 5, "chip gone")
            # Escape inside the section closes it
            b.js("document.getElementById('f-tag').focus()")
            self.key("Escape")
            b.wait_js("document.getElementById('fpanel').hidden && document.activeElement.id === 'filter-btn'", 3, "closed")
        finally:
            r.q("DELETE FROM listened")

    # ------------------------------------------------------------ phone
    def test_z_phone(self):
        b = self.b
        try:
            self.phone()
            self.home(A)
            self.type("wombat")
            b.wait_js(f"{HITS} === 40", 8, "snippets")
            self.assertTargets("a search with its clear button and snippets")
            self.no_side_scroll("a search")
            self.open_panel()
            self.choose("f-graph", self.gid)
            self.choose("f-yto", "2015")
            b.wait_js(f"{CHIPS}.length === 2", 5, "chips")
            self.assertTargets("the filter section and chips")
            self.no_side_scroll("the filter section")
            self.shot("phone-filters")
            b.js("document.getElementById('filter-btn').click()")
            self.assertTargets("the chips alone")
            self.shot("phone-search")
            # Enter searches and puts the keyboard away
            b.js("document.getElementById('search').focus()")
            self.key("Enter")
            b.wait_js("document.activeElement.id !== 'search'", 3, "blurred")
        finally:
            b.viewport(1440, 900)
            b.js("sessionStorage.clear()")


if __name__ == "__main__":
    unittest.main()
