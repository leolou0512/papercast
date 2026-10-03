#!/usr/bin/env python3
"""The column's one search box (hub/static/graphs.js, GET /api/library?q=), in a real headless
Chrome, against the real hub (PCG_AUTH=header, as test_page.py).

    python3 -m unittest discover -s stacks/papercast-group/hub/tests -t stacks/papercast-group -p 'test_search_page.py' -v

Papers and graphs as you type: the graphs whose name matches, then the papers, best first, each
with what matched marked and, when it matched elsewhere than in its title, where (DOM nodes: a
paper text holding <script> shows it as text and runs nothing); the first rows with their
snippets, the first thirty papers then "Show all"; a clear button; "/" to the search, Escape
to empty it, then to leave it; a corrected word still finds the paper; Enter opens the first
result; a paper picked opens in its graph. On a phone every control a 44 px tap. No test may
leave an error in the console (a CSP violation is one). Screenshots go to $PCG_TEST_SHOTS if
set. Fake titles and people only. (The list page's filters, chips, sort of papers and the
search kept over a reload went with the list.)"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from test_page import A, GROW, SKIP, PageBase, js_list  # noqa: E402

from hub import db, search  # noqa: E402

TYPE = "{ const s = document.getElementById('search'); s.value = %s; s.dispatchEvent(new Event('input')); }"
PAPERS = "[...document.querySelectorAll('#gl .gl-papers .gl-prow')].map(x => x.dataset.id)"
GHITS = "[...document.querySelectorAll('#gl .gl-hits .gl-row')].map(x => x.dataset.id)"
SNIPS = "[...document.querySelectorAll('#gl .gl-papers .gl-phit')].map(x => x.textContent)"
PROW = "#gl .gl-papers .gl-prow[data-id=\"{}\"]"
SECTIONS = "!!document.querySelector('#gl .gl-subs, #gl .gl-rest')"


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

    def papers_are(self, ids, what):
        self.b.wait_js(f"JSON.stringify({PAPERS}) === {js_list(ids)}", 8, what)

    def key(self, key, code=None, text=None):
        b = self.b
        kw = {"key": key, "code": code or key, "windowsVirtualKeyCode": {"Escape": 27, "/": 191, "Enter": 13}.get(key, 0)}
        b.call("Input.dispatchKeyEvent", type="keyDown", **kw, **({"text": text} if text else {}))
        b.call("Input.dispatchKeyEvent", type="keyUp", **kw)

    def hub_order(self, q):
        st, js, _ = self.r.req("GET", f"/api/library?q={q}", user=A)
        return [p["id"] for p in js["papers"]]

    # ------------------------------------------------------------ the box
    def test_1_results_as_you_type_with_marks(self):
        b = self.b
        self.home(A)
        self.assertTrue(b.js("document.getElementById('search-clear').hidden"))
        self.type("zebr")                                         # a prefix, in the paper's own text
        self.papers_are([self.xss], "the hostile paper")
        self.assertEqual(b.js(GHITS), [])                         # no graph is called that
        hit = PROW.format(self.xss) + " .gl-phit"
        b.wait_js(f"!!document.querySelector('{hit}')", 5, "the snippet")
        self.assertEqual(self.text(hit + " .lead"), "in the paper: ")
        self.assertEqual(b.js(f"[...document.querySelectorAll('{hit} mark')].map(m => m.textContent)"), ["zebrafinch"])
        # the text's markup is text: shown, never run
        self.assertEqual(self.text(hit), 'in the paper: Before <b>it</b> the zebrafinch <script>window.__xss = 1</script>'
                         '<img src=x onerror="window.__xss = 2"> sings.')
        self.assertEqual(b.js("document.querySelectorAll('#gl script, #gl img, #gl b').length"), 0)
        self.assertIsNone(b.js("window.__xss === undefined ? null : window.__xss"))
        self.assertFalse(b.js("document.getElementById('search-clear').hidden"))
        # a title's match is marked in the title, and no snippet says so again
        self.type("denoising diffusion")
        self.papers_are([self.diff], "by the title")
        self.assertEqual(b.js(f"[...document.querySelectorAll('{PROW.format(self.diff)} .gl-ptitle mark')].map(m => m.textContent)"),
                         ["Denoising", "Diffusion"])
        self.assertEqual(self.text(PROW.format(self.diff) + " .gl-ptitle"), "Denoising Diffusion For Fake Pictures")
        self.assertIsNone(b.js(f"document.querySelector('{PROW.format(self.diff)} .gl-phit')"))
        self.assertEqual(self.text(PROW.format(self.diff) + " .gl-psub"), "Lovelace and Turing · 2020")
        self.shot("search-desktop")
        # the clear button empties it: the graph list again
        b.js("document.getElementById('search-clear').click()")
        b.wait_js(f"{SECTIONS} && document.getElementById('search').value === '' && !document.querySelector('#gl mark')"
                  " && document.getElementById('search-clear').hidden", 5, "cleared")
        self.assertEqual(b.js("document.activeElement.id"), "search")
        # nothing found: the word for it
        self.type("qqqqzzzz")
        b.wait_js("(document.querySelector('#gl .gl-none-t') || {}).textContent === 'No matches'", 5, "no matches")
        self.type("")
        b.wait_js(SECTIONS, 5, "the list again")

    def test_2_slash_focuses_and_escape_empties(self):
        b = self.b
        self.home(A)
        b.js("document.activeElement.blur()")
        self.key("/", "Slash", "/")
        b.wait_js("document.activeElement && document.activeElement.id === 'search'", 3, "/ to the search")
        self.assertEqual(b.js("document.getElementById('search').value"), "")        # the / is not typed
        b.call("Input.insertText", text="wombat")
        b.wait_js(f"{PAPERS}.length === 30", 5, "typed")
        self.key("Escape")
        b.wait_js(f"document.getElementById('search').value === '' && {SECTIONS}", 5, "Escape empties it")
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

    def test_3_a_corrected_word_still_finds_it(self):
        b = self.b
        self.home(A)
        self.type("difusion ")
        self.papers_are([self.diff], "corrected")
        gen = db.conn().execute("SELECT id FROM graphs WHERE name = 'Diffusion and generative models'").fetchone()[0]
        b.wait_js(f"{GHITS}.includes({json.dumps(gen)})", 5, "the graph's name, corrected too")
        self.assertEqual(b.js(f"[...document.querySelectorAll('{PROW.format(self.diff)} .gl-ptitle mark')].map(m => m.textContent)"),
                         ["Diffusion"])

    def test_4_papers_and_graphs_snippets_and_show_all(self):
        """The graphs whose name matches first, then the papers in the hub's order: the first rows
        with their snippets, thirty of them, then "Show all"."""
        b = self.b
        self.home(A)
        self.type("wombat")
        b.wait_js(f"JSON.stringify({GHITS}) === {js_list([self.gid])}", 8, "the wombat graph")
        self.assertEqual(b.js(f"document.querySelector('{GROW.format(self.gid)} .gl-name mark').textContent"), "Wombat")
        want = self.hub_order("wombat")
        self.assertEqual(len(want), 40)
        self.papers_are(want[:30], "the first thirty, best first")
        self.assertEqual(self.text("#gl-all"), "Show all 40")
        texts = b.js(SNIPS)
        self.assertEqual(len(texts), search.SNIPPETS)                 # the rows past them come without
        self.assertTrue(all(t.startswith(("in the episode: ", "in the tags: ")) and "wombat" in t.lower() for t in texts), texts[:3])
        b.js("document.getElementById('gl-all').click()")
        self.papers_are(want, "all forty")
        self.assertIsNone(b.js("document.getElementById('gl-all')"))
        # Enter opens the first result: here the graph
        b.js("document.getElementById('search').focus()")
        self.key("Enter")
        b.wait_js(f"location.hash === '#g={self.gid}'", 5, "the graph opened")
        # a paper picked opens in its graph, with its window
        self.type("denoising")
        self.papers_are([self.diff], "the diffusion paper")
        gen = db.conn().execute("SELECT id FROM graphs WHERE name = 'Diffusion and generative models'").fetchone()[0]
        b.js(f"document.querySelector('{PROW.format(self.diff)} .gl-popen').click()")
        b.wait_js(f"location.hash === '#g={gen}&p={self.diff}' && !document.getElementById('paper').hidden"
                  " && document.getElementById('w-title').textContent === 'Denoising Diffusion For Fake Pictures'", 5, "opened in its graph")
        # Enter with a paper alone: that paper
        self.type("zebr")
        self.papers_are([self.xss], "the hostile paper")
        b.js("document.getElementById('search').focus()")
        self.key("Enter")
        b.wait_js(f"location.hash === '#g=none&p={self.xss}' && document.getElementById('w-title').textContent === 'A Hostile Fake Text'", 5, "opened")

    # ------------------------------------------------------------ phone
    def test_z_phone(self):
        b = self.b
        try:
            self.phone()
            self.home(A)
            b.js("document.getElementById('gpl-back').click()")              # it lands in a graph: back to the list of graphs
            b.wait_js("!document.body.classList.contains('gpl-open')", 5, "the list of graphs")
            self.type("wombat")
            b.wait_js(f"{PAPERS}.length === 30 && {GHITS}.length === 1", 8, "results")
            self.assertTargets("a search with its clear button, graphs and papers")
            self.no_side_scroll("a search")
            b.js("document.getElementById('gl-all').click()")
            b.wait_js(f"{PAPERS}.length === 40", 5, "all")
            self.assertTargets("all the papers")
            self.shot("phone-search")
            # Enter opens the first result and puts the keyboard away
            b.js("document.getElementById('search').focus()")
            self.key("Enter")
            b.wait_js(f"document.activeElement.id !== 'search' && location.hash === '#g={self.gid}'"
                      " && document.body.classList.contains('gpl-open')", 3, "the graph's papers, blurred")
        finally:
            b.viewport(1440, 900)


if __name__ == "__main__":
    unittest.main()
