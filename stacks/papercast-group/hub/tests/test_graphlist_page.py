#!/usr/bin/env python3
"""The graph list on the page (static/graphs.js, graphs.css, app.js's routes; DESIGN.md, Leo's
decisions of 2026-10-03) in a real headless Chrome, against the real hub (PCG_AUTH=header, as
test_page.py).

    python3 -m unittest discover -s stacks/papercast-group/hub/tests -t stacks/papercast-group -p 'test_graphlist_page.py' -v

What is checked: the column (the graphs subscribed to first, in the text colour, the rest muted;
each row's name, paper count, lock, "N new" when subscribed, its toggle; + New graph; Not in any
graph last); the toggle on a row and in the graph's header; the seven sorts within each section,
kept per account; "N new" gone once the graph is opened; one search box for papers and graphs,
a paper picked opening in a subscribed graph that has it; the hash routes (#g=, #g=&p=, #p=);
where the page lands (the graph open last, from the account, else the one with the most papers);
the bell's board and its dot; a phone: the list of graphs, a graph's papers in the map's order,
its drawing, a paper, Back each time, 44 px taps; every theme draws the new parts in its own
tokens. Screenshots go to $PCG_TEST_SHOTS if set (graphlist-*.png). Fake titles and people only."""
from __future__ import annotations

import json
import sys
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from test_page import A, B, C, GPLROWS, GROW, SKIP, TAP_TARGETS, PageBase  # noqa: E402

from hub import db, graph  # noqa: E402

SUBS = "[...document.querySelectorAll('#gl .gl-subs .gl-row')].map(x => x.dataset.id)"
REST = "[...document.querySelectorAll('#gl .gl-rest .gl-row')].map(x => x.dataset.id)"
TYPE = "{ const s = document.getElementById('search'); s.value = %s; s.dispatchEvent(new Event('input')); }"
J = json.dumps


def seed(name):
    return db.conn().execute("SELECT id FROM graphs WHERE name = ?", (name,)).fetchone()[0]


@unittest.skipIf(SKIP, SKIP or "")
class GraphListPage(PageBase):
    @classmethod
    def fill(cls):
        super().fill()
        r = cls.r
        graph.ensure_schema()
        cls.gen, cls.rl, cls.lm = seed("Diffusion and generative models"), seed("Reinforcement learning"), seed("Language models")
        cls.mat, cls.robot = seed("Materials and molecules"), seed("Robotics and agents")
        # two graphs people made: Bob's and Alice's (each subscribed to their own)
        t = r.stamp()
        cls.bobs = "g_bobsgraph0"
        cls.alices = "g_alicesgra0"
        for gid, name, tags, by in ((cls.bobs, "Bob's hands", ["hands"], cls.bob), (cls.alices, "Alice's flows", ["flows"], cls.alice)):
            db.conn().execute("INSERT INTO graphs(id, name, rule_tags, locked, created_by, created_at, updated_at) VALUES (?, ?, ?, 0, ?, ?, ?)",
                              (gid, name, db.dumps(tags), by, t, t))
            db.conn().execute("INSERT INTO graph_subs(user_id, graph_id, at) VALUES (?, ?, ?)", (by, gid, t))
        db.conn().execute("UPDATE graphs SET locked = 1 WHERE id = ?", (cls.lm,))

        def paper(title, tags, by, year):
            p = r.paper(title, by, tags=tags, year=year)
            r.episode(p, by, summary="derivations", duration=60, audio_s=2.0)
            return p
        cls.d = [paper(f"Fake Diffusion Study {i}", ["diffusion"], cls.alice, 2016 + i) for i in range(4)]
        cls.rlp = [paper(f"Fake Reward Study {i}", ["reinforcement learning"], cls.bob, 2018 + i) for i in range(6)]
        cls.lmp = paper("Fake Language Study", ["language models"], cls.alice, 2021)
        cls.hands = [paper(f"Fake Hands Study {i}", ["hands"], cls.bob, 2020 + i) for i in range(2)]
        cls.both = paper("Fake Hands And Diffusion", ["hands", "diffusion"], cls.bob, 2023)
        cls.loose = [paper(f"Fake Loose Study {i}", ["nothing"], cls.carol if i else cls.alice, 2019 + i) for i in range(3)]
        cls.flow = paper("Fake Flows Study", ["flows"], cls.alice, 2024)
        # Carol: subscribed to the diffusion graph and to Bob's; she opened the diffusion graph
        # before its last two papers came, and Bob's after its papers
        for gid in (cls.gen, cls.bobs):
            db.conn().execute("INSERT INTO graph_subs(user_id, graph_id, at) VALUES (?, ?, ?)", (cls.carol, gid, "2026-09-01T08:00:00Z"))
        at = db.conn().execute("SELECT created_at FROM episodes WHERE paper_id = ?", (cls.d[2],)).fetchone()[0]
        db.conn().execute("INSERT INTO graph_seen(user_id, graph_id, at) VALUES (?, ?, ?)", (cls.carol, cls.gen, at))
        db.conn().execute("INSERT INTO graph_seen(user_id, graph_id, at) VALUES (?, ?, ?)", (cls.carol, cls.bobs, "2026-09-30T00:00:00Z"))
        db.conn().execute("INSERT INTO listened(user_id, paper_id, at) VALUES (?, ?, ?)", (cls.carol, cls.rlp[0], r.stamp()))

    def setUp(self):
        super().setUp()
        self.keep = {t: [tuple(x) for x in self.r.q(f"SELECT * FROM {t}")] for t in ("graph_subs", "graph_seen")}

    def tearDown(self):
        try:
            super().tearDown()
        finally:
            # the page gone first: nothing it still sends (an opened graph) lands after the tables are put back
            self.b.goto("about:blank")
            time.sleep(0.5)
            for t, rows in self.keep.items():
                self.r.q(f"DELETE FROM {t}")
                for row in rows:
                    self.r.q(f"INSERT INTO {t} VALUES ({','.join('?' * len(row))})", *row)
            self.r.q("DELETE FROM ui_state")

    # ------------------------------------------------------------------ helpers
    def shot(self, name):
        super().shot(f"graphlist-{name}")

    def items(self, who):
        _, js, _ = self.r.req("GET", "/api/graphs", user=who)
        return js

    def js(self, expr):
        return self.b.js(expr)

    def wait(self, expr, what, timeout=5):
        return self.b.wait_js(expr, timeout, what)

    def row_text(self, gid):
        """[name, paper count, locked, the new-papers dot on, the row's title (its words)]"""
        return self.js(f"(r => r && [r.querySelector('.gl-name').textContent, r.querySelector('.gl-n').textContent,"
                       f" !!r.querySelector('.gl-lock'), r.querySelector('.gl-dot').classList.contains('on'),"
                       f" r.querySelector('.gl-open').getAttribute('title')])(document.querySelector('{GROW.format(gid)}'))")

    def tok(self, k):
        return self.js(f"(e => {{ e.style.color = 'var({k})'; document.body.append(e); const c = getComputedStyle(e).color; e.remove(); return c; }})(document.createElement('span'))")

    def pill(self):
        """The graph header's pill: [its words, its fill, its words' colour, aria-pressed]"""
        return self.js("(b => b && [b.textContent, getComputedStyle(b).backgroundColor, getComputedStyle(b).color, b.getAttribute('aria-pressed')])"
                       "(document.querySelector('#gh .gh-sub'))")

    # ------------------------------------------------------------------ the column
    def test_1_the_column_subscribed_first_then_the_rest(self):
        """Carol's column: her two subscriptions on top, in the text colour, a dot at the end of the
        one whose last two papers came after she opened it ("2 new papers" its title); the rest
        muted, without a dot; no toggles on the rows (YouTube's subscriptions); the locked graph's
        lock; + New graph; Not in any graph last."""
        self.home(C)
        self.assertEqual(sorted(self.js(SUBS)), sorted([self.gen, self.bobs]))
        self.assertEqual(sorted(self.js(REST)), sorted([self.rl, self.lm, self.mat, self.robot, self.alices]))
        self.assertEqual(self.row_text(self.gen), ["Diffusion and generative models", "5", False, True, "2 new papers"])
        self.assertEqual(self.row_text(self.bobs), ["Bob's hands", "3", False, False, None])
        self.assertEqual(self.row_text(self.lm), ["Language models", "1", True, False, None])
        self.assertEqual(self.row_text(self.rl), ["Reinforcement learning", "6", False, False, None])
        self.assertEqual(self.row_text("none"), ["Not in any graph", "3", False, False, None])
        self.assertIn("2 new papers", self.js(f"document.querySelector('{GROW.format(self.gen)} .gl-open').getAttribute('aria-label')"))
        self.assertEqual(self.js("document.querySelectorAll('#gl .gl-row button').length"), self.js("document.querySelectorAll('#gl .gl-row').length"))
        # the dot at the row's end, in the accent
        dot, n, row = (self.js(f"(e => (r => [r.left, r.right])(e.getBoundingClientRect()))(document.querySelector('{GROW.format(self.gen)} {x}'))")
                       for x in (".gl-dot", ".gl-n", ".gl-open"))
        self.assertGreater(dot[0], n[1])
        self.assertAlmostEqual(dot[1], row[1] - 16 * self.js("document.documentElement.currentCSSZoom || 1"), delta=1.5)
        self.assertEqual(self.js(f"getComputedStyle(document.querySelector('{GROW.format(self.gen)} .gl-dot')).backgroundColor"), self.tok("--accent"))
        # the order on the page: the sections, + New graph, then Not in any graph
        order = self.js("[...document.getElementById('gl').children].map(e => e.className.split(' ')[0] + (e.classList.contains('gl-list') ? ':' + e.className.split(' ')[1] : ''))")
        self.assertEqual(order, ["gl-h", "gl-list:gl-subs", "gl-h", "gl-list:gl-rest", "gl-newb", "gl-list:gl-none"])
        # solid and greyed: the text colour, the muted one
        col = lambda gid: self.js(f"getComputedStyle(document.querySelector('{GROW.format(gid)} .gl-open')).color")
        self.assertEqual(col(self.gen), self.tok("--text"))
        self.assertEqual(col(self.rl), self.tok("--text-2"))
        self.shot("desktop-light")

    def test_2_the_pill_in_the_graphs_header_and_its_confirm(self):
        """YouTube's pill: "Subscribe" solid in the text colour on the page's ground, at once;
        "Subscribed" grey, asking "Unsubscribe from <graph>?" with Cancel (or Escape, or a click
        outside) and Unsubscribe."""
        b, r = self.b, self.r
        sub = lambda gid: bool(r.q("SELECT 1 FROM graph_subs WHERE user_id = ? AND graph_id = ?", self.carol, gid))
        self.home(C)                                       # lands on Reinforcement learning, not subscribed
        self.wait("document.getElementById('gh-name').textContent === 'Reinforcement learning'", "its header")
        self.assertEqual(self.pill(), ["Subscribe", self.tok("--text"), self.tok("--bg"), "false"])
        self.assertEqual(self.js("(b => Math.round(b.getBoundingClientRect().height / (document.documentElement.currentCSSZoom || 1)))(document.querySelector('#gh .gh-sub'))"), 36)
        b.js("document.querySelector('#gh .gh-sub').click()")
        self.wait(f"document.querySelector('#gh .gh-sub').textContent === 'Subscribed' && {SUBS}.includes({J(self.rl)})", "subscribed, no confirm")
        r.wait(lambda: sub(self.rl), 5, "on the hub")
        self.assertFalse(self.js("document.getElementById('gh-dlg').open"))
        self.assertEqual(self.pill(), ["Subscribed", self.tok("--track"), self.tok("--text"), "true"])
        # Subscribed: the confirm; Cancel keeps it
        b.js("document.querySelector('#gh .gh-sub').click()")
        self.wait("document.getElementById('gh-dlg').open", "the confirm")
        self.assertEqual(self.js("document.getElementById('gh-dlg-h').textContent"), "Unsubscribe from Reinforcement learning?")
        self.assertEqual(self.js("[...document.querySelectorAll('#gh-dlg button')].map(x => x.textContent)"), ["Cancel", "Unsubscribe"])
        self.assertEqual(self.js("document.activeElement.id"), "gh-dlg-no")
        time.sleep(0.3)
        self.shot("unsubscribe-confirm")
        b.js("document.getElementById('gh-dlg-no').click()")
        self.wait("!document.getElementById('gh-dlg').open && document.activeElement === document.querySelector('#gh .gh-sub')", "cancelled")
        self.assertTrue(sub(self.rl))
        # Escape keeps it too
        b.js("document.querySelector('#gh .gh-sub').click()")
        self.wait("document.getElementById('gh-dlg').open", "the confirm again")
        b.call("Input.dispatchKeyEvent", type="keyDown", key="Escape", code="Escape", windowsVirtualKeyCode=27)
        self.wait("!document.getElementById('gh-dlg').open", "Escape")
        self.assertTrue(sub(self.rl))
        self.assertEqual(self.pill()[0], "Subscribed")
        # Unsubscribe
        b.js("document.querySelector('#gh .gh-sub').click()")
        self.wait("document.getElementById('gh-dlg').open", "and again")
        b.js("document.getElementById('gh-dlg-yes').click()")
        self.wait(f"!document.getElementById('gh-dlg').open && document.querySelector('#gh .gh-sub').textContent === 'Subscribe'"
                  f" && {REST}.includes({J(self.rl)})", "unsubscribed")
        r.wait(lambda: not sub(self.rl), 5, "off on the hub")
        # Not in any graph has none
        b.js("location.hash = 'g=none'")
        self.wait("document.getElementById('gh-name').textContent === 'Not in any graph' && !document.querySelector('#gh .gh-sub')", "no pill")
        # subscriptions from another tab of hers reach this one
        for gid in (self.alices, self.mat):
            r.req("PUT", f"/api/graphs/{gid}/subscription", {"subscribed": True}, user=C)
        self.wait(f"{SUBS}.includes({J(self.alices)}) && {SUBS}.includes({J(self.mat)})", "from her other tab")
        b.js(f"location.hash = 'g={self.gen}'")
        self.wait("document.getElementById('gh-name').textContent === 'Diffusion and generative models'", "a subscribed graph")
        time.sleep(0.3)
        self.shot("subscribed-mix")

    def test_3_the_seven_sorts_within_each_section_kept_per_account(self):
        b = self.b
        self.home(C)
        r = self.items(C)
        me = self.carol

        def want(how):
            gs = r["graphs"]
            name = lambda g: g["name"].lower()
            keys = {
                "az": lambda g: (name(g),),
                "updated": lambda g: (-ts(g["updated_at"]), name(g)),
                "papers": lambda g: (-g["n"], name(g)),
                "created": lambda g: (-ts(g["created_at"]), name(g)),
                "mine": lambda g: (0 if (g["created_by"] or {}).get("id") == me else 1, -ts(g["updated_at"]), name(g)),
                "unheard": lambda g: (-g["unheard"], name(g)),
                "newest": lambda g: (-ts(g["newest_at"]), name(g)),
            }
            k = keys[how]
            return ([g["id"] for g in sorted((g for g in gs if g["subscribed"]), key=k)],
                    [g["id"] for g in sorted((g for g in gs if not g["subscribed"]), key=k)])
        labels = {"az": "A–Z", "updated": "Recently updated", "papers": "Most papers", "created": "Date created",
                  "mine": "Mine first", "unheard": "Most unheard by me", "newest": "Newest paper added"}
        self.assertEqual(self.js("document.getElementById('sort-label').textContent"), "Recently updated")    # the default
        self.assertEqual((self.js(SUBS), self.js(REST)), want("updated"))
        b.js("document.getElementById('sort-btn').click()")
        self.wait("document.querySelectorAll('.menu [role=menuitemradio]').length === 7", "the sorts")
        self.assertEqual(self.js("[...document.querySelectorAll('.menu [role=menuitemradio]')].map(x => x.textContent)"), list(labels.values()))
        b.js("document.getElementById('sort-btn').click()")
        for how, label in labels.items():
            with self.subTest(sort=how):
                b.js("document.getElementById('sort-btn').click()")
                self.wait("!!document.querySelector('.menu')", "menu")
                b.js(f"document.querySelector('.menu [data-sort={how}]').click()")
                self.wait(f"document.getElementById('sort-label').textContent === {J(label)}", "the label")
                self.assertEqual((self.js(SUBS), self.js(REST)), want(how))
                self.r.wait(lambda: (self.r.q("SELECT sort FROM ui_state WHERE user_id = ?", self.carol) or [[None]])[0][0] == how, 5, "on the hub")
                if how == "unheard":
                    self.shot("sort-unheard")
        # kept on the hub, not in this browser: another of her devices shows it (here: a fresh load)
        b.js("localStorage.clear()")
        self.load()
        self.wait("document.getElementById('sort-label').textContent === 'Newest paper added'", "the sort kept")
        self.assertEqual((self.js(SUBS), self.js(REST)), want("newest"))

    def test_3b_a_tick_resorts_most_unheard_and_an_old_answer_keeps_the_sort(self):
        """Ticking Listened refreshes the list's counts, so "Most unheard by me" sorts again; a list
        answer asked for before the sort was chosen here does not bring the old sort back."""
        b, r = self.b, self.r
        self.addCleanup(lambda: r.q("DELETE FROM listened WHERE paper_id = ?", self.flow))
        self.home(C)
        b.js("document.getElementById('sort-btn').click()")
        self.wait("!!document.querySelector('.menu')", "menu")
        b.js("document.querySelector('.menu [data-sort=unheard]').click()")
        # not subscribed: RL 5 unheard; Alice's flows and Language models 1 each (by name); the rest 0
        want0 = [self.rl, self.alices, self.lm, self.mat, self.robot]
        self.wait(f"JSON.stringify({REST}) === {J(J(want0, separators=(',', ':')))}", "most unheard first")
        b.js(f"location.hash = 'p={self.flow}'")
        self.wait(f"!document.getElementById('paper').hidden && document.getElementById('w-title').textContent === 'Fake Flows Study'", "its paper")
        b.js("document.getElementById('w-listened').click()")
        want1 = [self.rl, self.lm, self.alices, self.mat, self.robot]           # Alice's flows: 0 unheard now
        self.wait(f"JSON.stringify({REST}) === {J(J(want1, separators=(',', ':')))}", "sorted again after the tick", 8)
        # an answer from before the choice (the account's old sort in it) leaves the sort as chosen
        b.js("document.getElementById('sort-btn').click()")
        self.wait("!!document.querySelector('.menu')", "menu")
        b.js("document.querySelector('.menu [data-sort=az]').click()")
        self.wait("document.getElementById('sort-label').textContent === 'A–Z'", "A–Z")
        b.js("fetch('/api/graphs', {credentials: 'same-origin'}).then(r => r.json())"
             ".then(j => { j.ui = {sort: 'updated', last_graph: null}; PaperGraphs.current.setList(j); window.__fed = 1; })")
        self.wait("window.__fed === 1", "an old answer fed to the list")
        self.assertEqual(self.js("document.getElementById('sort-label').textContent"), "A–Z")
        self.assertEqual(self.js(REST), sorted(self.js(REST), key=lambda gid: self.js(f"document.querySelector('{GROW.format(gid)} .gl-name').textContent").lower()))

    def test_4_opening_a_graph_sees_its_new_papers(self):
        b, r = self.b, self.r
        self.home(C)
        self.assertEqual(self.row_text(self.gen)[3:], [True, "2 new papers"])
        b.js(f"document.querySelector('{GROW.format(self.gen)} .gl-open').click()")
        self.wait(f"location.hash === '#g={self.gen}' && !document.querySelector('{GROW.format(self.gen)} .gl-dot.on')", "seen")
        r.wait(lambda: r.q("SELECT at FROM graph_seen WHERE user_id = ? AND graph_id = ?", self.carol, self.gen)[0][0] > "2026-09-02", 5, "on the hub")
        self.assertEqual(r.q("SELECT last_graph FROM ui_state WHERE user_id = ?", self.carol)[0][0], self.gen)
        self.load()
        self.wait(f"document.querySelectorAll('#gl .gl-row').length > 0 && !document.querySelector('{GROW.format(self.gen)} .gl-dot.on')", "still seen")
        self.assertTrue(self.js(f"document.querySelector('{GROW.format(self.gen)}').classList.contains('cur')"))

    def test_5_new_graph(self):
        b, r = self.b, self.r
        # the graph made goes once the page has (a graph deleted under an open page is read again, and not found)
        self.addCleanup(lambda: r.q("UPDATE graphs SET deleted_at = '2026-09-01T00:00:00Z' WHERE created_by = ? AND name = 'Carol’s birds'", self.carol))
        if True:
            self.home(C)
            b.js("document.getElementById('gl-newb').click()")
            self.wait("document.activeElement && document.activeElement.id === 'gl-name'", "the form, the name first")
            b.js("document.getElementById('gl-name').value = 'Carol’s birds'; document.getElementById('gl-tags').value = 'birds, nothing'")
            b.js("document.getElementById('gl-make').click()")
            self.wait("document.getElementById('gh-name') && document.getElementById('gh-name').textContent === 'Carol’s birds'", "the new graph shown")
            gid = self.js("new URLSearchParams(location.hash.slice(1)).get('g')")
            self.assertEqual(r.q("SELECT name FROM graphs WHERE id = ?", gid)[0][0], "Carol’s birds")
            self.wait(f"{SUBS}.includes({J(gid)}) && document.getElementById('gh-name').textContent === 'Carol’s birds'", "subscribed, its maker")
            self.assertEqual(r.q("SELECT created_by FROM graphs WHERE id = ?", gid)[0][0], self.carol)
            self.wait(f"(r => r && r.querySelector('.gl-n').textContent === '3')(document.querySelector('{GROW.format(gid)}'))", "its papers by its tags")
            self.assertTrue(self.js("!!document.getElementById('gl-newb')"))

    def test_5b_a_paper_covers_the_map_and_closing_gives_it_back_as_it_was(self):
        """A paper opened from its card's Open: its window covers the map, right of the graph list,
        with Leo's player layout (the transcript in the middle, the comments a slim column on the
        right, the bar along the bottom); the list stays and works; closing it, or Back, gives the
        map back as it was: the same graph, view and paper picked."""
        b, r = self.b, self.r
        pid = self.d[1]
        eid = r.q("SELECT id FROM episodes WHERE paper_id = ?", pid)[0][0]
        script = r.cfg.episodes / eid / "script.md"
        script.write_text("# The idea\n\n" + "".join(f"Paragraph {w} of plain made-up words about a fake diffusion study. It is only here to be read.\n\n"
                                                       for w in ("one", "two", "three", "four", "five", "six", "seven", "eight")))
        for i, who in enumerate((A, B, A)):
            st, _, _ = r.req("POST", f"/api/papers/{pid}/comments", {"body": f"Fake comment {i}: the part at 0:0{i + 1} is the clearest."}, user=who)
            self.assertEqual(st, 201)
        self.addCleanup(lambda: (script.unlink(missing_ok=True), r.q("DELETE FROM comments")))
        M = "PaperMap.current.debug"
        box = lambda sel: self.js(f"(r => [r.left, r.top, r.right, r.bottom])(document.querySelector({J(sel)}).getBoundingClientRect())")
        self.home(C)
        b.js(f"location.hash = 'g={self.gen}'")
        self.wait(f"{M}.cur().id === {J(self.gen)} && !!{M}.cur()._s && {M}.cur()._s.idx[{J(pid)}] != null", "the diffusion graph")
        # a view of her own, and a paper picked: its card
        b.js(f"{M}.view(1.3, 120, 40); {M}.select({J(pid)})")
        self.wait("!!document.querySelector('#map .pm-card .pm-open')", "its card")
        view0, sel0 = self.js(f"{M}.view()"), self.js(f"{M}.state().sel")
        # its card's Open: the window over the whole map, right of the list
        b.js("document.querySelector('#map .pm-card .pm-open').click()")
        self.wait(f"location.hash === '#g={self.gen}&p={pid}' && getComputedStyle(document.getElementById('win')).visibility === 'visible'"
                  " && document.getElementById('w-title').textContent === 'Fake Diffusion Study 1'", "the paper")
        win, col, m = box("#win"), box("#gcol"), box("#map")
        self.assertAlmostEqual(win[0], col[2], delta=1.5, msg="the window does not start at the graph list")
        self.assertAlmostEqual(win[0], m[0], delta=1.5, msg="the window does not cover the map's left edge")
        self.assertAlmostEqual(win[2], m[2], delta=1, msg="the window does not cover the map's right edge")
        self.assertAlmostEqual(win[3], box("#bar")[1], delta=1, msg="the window does not end at the bar")
        # the player's layout: the transcript in the middle, the comments a slim column on the right
        self.wait("!document.getElementById('tr').hidden && document.querySelectorAll('#c-list .c-item').length === 3", "the transcript and the comments")
        self.assertEqual(self.js("getComputedStyle(document.getElementById('ptabs')).display"), "none")
        c, mid, z = box("#comments"), box("#p-mid"), self.js("document.documentElement.currentCSSZoom || 1")
        self.assertTrue(280 <= (c[2] - c[0]) / z <= 341, f"the comments' column is {(c[2] - c[0]) / z} CSS px wide")
        self.assertAlmostEqual(mid[2], c[0], delta=1, msg="the comments are not right of the transcript")
        self.assertEqual(self.js("getComputedStyle(document.getElementById('bar')).position"), "fixed")
        # the graph list stays, and takes a tap
        self.assertEqual(self.js(f"(r => document.elementFromPoint(r.left + 20, (r.top + r.bottom) / 2).closest('.gl-row').dataset.id)"
                                 f"(document.querySelector('{GROW.format(self.bobs)} .gl-open').getBoundingClientRect())"), self.bobs)
        self.assertEqual(self.js("document.getElementById('gh-name').textContent"), "Diffusion and generative models")
        time.sleep(0.5)
        self.shot("desktop-paper")
        # closed: the map as it was
        b.js("document.getElementById('w-close').click()")
        self.wait(f"location.hash === '#g={self.gen}' && getComputedStyle(document.getElementById('win')).visibility === 'hidden'", "closed")
        self.assertEqual((self.js(f"{M}.cur().id"), self.js(f"{M}.view()"), self.js(f"{M}.state().sel")), (self.gen, view0, sel0))
        # again, then Back
        b.js("document.querySelector('#map .pm-card .pm-open').click()")
        self.wait(f"location.hash === '#g={self.gen}&p={pid}' && getComputedStyle(document.getElementById('win')).visibility === 'visible'", "again")
        b.js("history.back()")
        self.wait(f"location.hash === '#g={self.gen}' && getComputedStyle(document.getElementById('win')).visibility === 'hidden'", "Back")
        self.assertEqual((self.js(f"{M}.cur().id"), self.js(f"{M}.view()"), self.js(f"{M}.state().sel")), (self.gen, view0, sel0))
        # a graph in the list, with a paper open, opens that graph (and the paper's window goes)
        b.js("document.querySelector('#map .pm-card .pm-open').click()")
        self.wait("getComputedStyle(document.getElementById('win')).visibility === 'visible'", "a third time")
        b.js(f"document.querySelector('{GROW.format(self.bobs)} .gl-open').click()")
        self.wait(f"location.hash === '#g={self.bobs}' && getComputedStyle(document.getElementById('win')).visibility === 'hidden'"
                  f" && {M}.cur().id === {J(self.bobs)}", "the list's graph")

    # ------------------------------------------------------------------ search, routes, landing
    def test_6_one_search_box_for_papers_and_graphs(self):
        b = self.b
        self.home(C)
        b.js(TYPE % J("hands"))
        self.wait("document.querySelectorAll('#gl .gl-hits .gl-row').length === 1 && document.querySelectorAll('#gl .gl-papers .gl-prow').length === 3", "graphs and papers")
        self.assertEqual(self.js("document.querySelector('#gl .gl-hits .gl-row').dataset.id"), self.bobs)
        self.assertEqual(self.js("document.querySelector('#gl .gl-hits .gl-name mark').textContent"), "hands")
        # a paper in two graphs opens in the one she subscribes to and shows (Bob's) — her subscriptions first
        b.js(f"location.hash = 'g={self.rl}'")
        self.wait(f"PaperMap.current.debug.cur().id === {J(self.rl)}", "another graph shown")
        b.js(f"document.querySelector('#gl .gl-prow[data-id={J(self.both)}] .gl-popen').click()")
        self.wait(f"location.hash === '#g={self.gen}&p={self.both}' || location.hash === '#g={self.bobs}&p={self.both}'", "opened in a subscribed graph")
        g = self.js("new URLSearchParams(location.hash.slice(1)).get('g')")
        self.assertIn(g, (self.gen, self.bobs))
        self.wait(f"!document.getElementById('paper').hidden && PaperMap.current.debug.cur().id === {J(g)}"
                  f" && PaperMap.current.debug.state().sel === {J(self.both)}", "its window, and its card on the map")
        # the graph shown has it: it stays there
        b.js("document.getElementById('w-close').click()")
        self.wait(f"location.hash === '#g={g}'", "closed")
        b.js(f"document.querySelector('#gl .gl-prow[data-id={J(self.both)}] .gl-popen').click()")
        self.wait(f"location.hash === '#g={g}&p={self.both}'", "in the graph shown")
        # a graph picked opens it; Escape empties the box
        b.js(f"document.querySelector('#gl .gl-hits .gl-open').click()")
        self.wait(f"location.hash === '#g={self.bobs}'", "the graph")
        b.js("document.getElementById('search').focus()")
        b.call("Input.dispatchKeyEvent", type="keyDown", key="Escape", code="Escape", windowsVirtualKeyCode=27)
        self.wait(f"document.getElementById('search').value === '' && {SUBS}.length === 2", "emptied")
        # "/" from anywhere
        b.js("document.activeElement.blur()")
        b.call("Input.dispatchKeyEvent", type="keyDown", key="/", code="Slash", windowsVirtualKeyCode=191, text="/")
        self.wait("document.activeElement === document.getElementById('search')", "/ to the search")
        # nothing found: the word for it
        b.js(TYPE % J("zzzzqqq"))
        self.wait("(document.querySelector('#gl .gl-none-t') || {}).textContent === 'No matches'", "no matches")
        b.js(TYPE % J(""))

    def test_7_the_routes_and_where_the_page_lands(self):
        b, r = self.b, self.r
        # first time: the graph with the most papers (Reinforcement learning, 6), greyed: not subscribed
        self.home(C)
        self.assertEqual(self.js("location.hash"), f"#g={self.rl}")
        self.assertFalse(self.js(f"document.querySelector('{GROW.format(self.rl)}').classList.contains('on')"))
        # the graph open last, from the account: on the hub, so another browser of hers lands there too
        b.js(f"location.hash = 'g={self.bobs}'")
        r.wait(lambda: (r.q("SELECT last_graph FROM ui_state WHERE user_id = ?", self.carol) or [[None]])[0][0] == self.bobs, 5, "kept")
        b.js("localStorage.clear(); sessionStorage.clear()")
        self.load()
        self.wait(f"location.hash === '#g={self.bobs}'", "landed on the graph open last")
        # #g=&p=: that graph, that paper; #p= alone: a graph that has it, subscribed first
        self.load(f"g={self.rl}&p={self.rlp[1]}")
        self.wait(f"!document.getElementById('paper').hidden && PaperMap.current.debug.cur().id === {J(self.rl)}"
                  " && document.getElementById('w-title').textContent === 'Fake Reward Study 1'", "#g=&p=")
        self.load(f"p={self.d[0]}")
        self.wait(f"location.hash === '#g={self.gen}&p={self.d[0]}'", "#p= in a graph that has it")
        self.load(f"p={self.loose[0]}")
        self.wait(f"location.hash === '#g=none&p={self.loose[0]}' && !document.getElementById('paper').hidden", "#p= in no graph")
        # Back closes the paper, to the graph
        b.js("document.getElementById('w-close').click()")
        self.wait("location.hash === '#g=none' && document.getElementById('paper').hidden", "closed")
        # a graph that is not there: where the page lands
        self.load("g=g_nosuchgraph")
        self.wait(f"location.hash === '#g={self.bobs}'", "an unknown graph")

    def test_8_the_bell_opens_the_board(self):
        b = self.b
        self.home(C)
        self.wait("!document.getElementById('bell-dot').hidden", "the dot: something new")
        self.assertEqual(self.js("document.getElementById('bell-btn').getAttribute('aria-label')"), "Board, something new")
        b.js("document.getElementById('bell-btn').click()")
        self.wait("!document.getElementById('bell-panel').hidden && document.querySelectorAll('#bell-panel .bd-item').length > 0", "the board")
        self.assertEqual(self.js("document.getElementById('bell-btn').getAttribute('aria-expanded')"), "true")
        self.assertEqual(self.js("getComputedStyle(document.querySelector('#bell-panel .bd-toggle')).fontWeight"), "400")
        self.r.wait(lambda: self.r.q("SELECT 1 FROM board_seen WHERE user_id = ?", self.carol), 5, "seen")
        self.wait("document.getElementById('bell-dot').hidden", "the dot gone")
        self.shot("bell")
        b.call("Input.dispatchKeyEvent", type="keyDown", key="Escape", code="Escape", windowsVirtualKeyCode=27)
        self.wait("document.getElementById('bell-panel').hidden && document.activeElement === document.getElementById('bell-btn')", "closed with Escape")
        b.js("document.getElementById('bell-btn').click()")
        self.wait("!document.getElementById('bell-panel').hidden", "open again")
        # a paper on it opens in its graph, and the panel goes
        b.js("document.querySelector('#bell-panel a.bd-link').click()")
        self.wait("document.getElementById('bell-panel').hidden && !document.getElementById('paper').hidden && /(^#|&)p=/.test(location.hash)", "a paper from the board")

    # ------------------------------------------------------------------ a phone
    def test_9_phone_the_list_a_graphs_papers_its_drawing_a_paper_and_back(self):
        b = self.b
        try:
            self.phone()
            self.home(C)
            # lands in the graph with the most papers: its papers, in the map's order (earliest first)
            self.wait("document.body.classList.contains('gpl-open') && document.querySelectorAll('#gpl-rows .gpl-row').length === 6", "its papers")
            self.assertEqual(self.js(GPLROWS), self.rlp)
            self.assertEqual(self.js("document.getElementById('gpl-name').textContent"), "Reinforcement learning")
            self.assertTrue(self.js(f"document.querySelector('#gpl-rows .gpl-row[data-id={J(self.rlp[0])}]').classList.contains('heard')"))
            self.assertTargets("a graph's papers")
            self.shot("phone-papers-light")
            # its header's pill: Subscribe at a tap; Subscribed asks first
            pill = "document.querySelector('#gpl-acts .gh-sub')"
            self.assertEqual(self.js(f"{pill}.textContent"), "Subscribe")
            b.js(f"{pill}.click()")
            self.wait(f"{pill}.textContent === 'Subscribed'", "subscribed on the phone")
            b.js(f"{pill}.click()")
            self.wait("document.getElementById('gh-dlg').open", "the confirm on the phone")
            self.assertTargets("the unsubscribe confirm")
            b.js("document.getElementById('gh-dlg-yes').click()")
            self.wait(f"!document.getElementById('gh-dlg').open && {pill}.textContent === 'Subscribe'", "unsubscribed on the phone")
            # Back: the list of graphs, full width
            b.js("document.getElementById('gpl-back').click()")
            self.wait("!document.body.classList.contains('gpl-open') && location.hash === ''", "the list of graphs")
            w = self.js("[document.getElementById('gcol').getBoundingClientRect().width, innerWidth]")
            self.assertAlmostEqual(w[0], w[1], delta=1)
            self.assertTargets("the list of graphs")
            self.shot("phone-home-light")
            # a graph: its papers; Map: the drawing; Back: the papers
            b.js(f"document.querySelector('{GROW.format(self.gen)} .gl-open').click()")
            self.wait(f"document.body.classList.contains('gpl-open') && JSON.stringify({GPLROWS}) === {J(J(self.d + [self.both], separators=(',', ':')))}", "the diffusion papers, earliest first")
            b.js("document.getElementById('gpl-map').click()")
            self.wait(f"!document.getElementById('map').hidden && location.hash === '#g={self.gen}&map' && PaperMap.current.debug.cur()._s.nodes.length === 5", "the drawing")
            self.assertTargets("the drawing")
            b.js("history.back()")
            self.wait(f"document.getElementById('map').hidden && location.hash === '#g={self.gen}'", "back to the papers")
            # a paper: the window and the player; Back: the papers again
            b.js(f"document.querySelector('#gpl-rows .gpl-row[data-id={J(self.d[1])}] .gpl-btn').click()")
            self.wait(f"location.hash === '#g={self.gen}&p={self.d[1]}' && getComputedStyle(document.getElementById('win')).visibility === 'visible'"
                      " && !document.getElementById('bar').hidden", "the paper and the player")
            self.assertTargets("a paper")
            b.js("document.getElementById('back').click()")
            self.wait(f"location.hash === '#g={self.gen}' && getComputedStyle(document.getElementById('win')).visibility === 'hidden'"
                      " && document.body.classList.contains('gpl-open')", "back to its papers")
            b.js("document.getElementById('gpl-back').click()")
            self.wait("!document.body.classList.contains('gpl-open')", "back to the graphs")
            # the bell's board, on the phone
            b.js("document.getElementById('bell-btn').click()")
            self.wait("!document.getElementById('bell-panel').hidden", "the board")
            self.assertTargets("the board")
            b.js("document.getElementById('bell-btn').click()")
            # dark
            b.call("Emulation.setEmulatedMedia", features=[{"name": "prefers-color-scheme", "value": "dark"}])
            self.load()
            self.wait("document.body.classList.contains('gpl-open') && document.querySelectorAll('#gpl-rows .gpl-row').length > 0", "dark")
            time.sleep(0.4)
            self.shot("phone-papers-dark")
            b.js("document.getElementById('gpl-back').click()")
            self.wait("!document.body.classList.contains('gpl-open')", "the list of graphs, dark")
            time.sleep(0.4)
            self.shot("phone-home-dark")
        finally:
            b.call("Emulation.setEmulatedMedia", features=[])
            b.viewport(1440, 900)

    def test_z_every_theme_draws_the_new_parts_in_its_tokens(self):
        """Light, dark and the six others: the rows' text (subscribed: --text, the rest: --text-2)
        on the column's ground (--side), readable (4.5:1, as tools/theme_contrast.py has it); the
        new-papers dot in the accent, seen on the ground (3:1); the header's pill: Subscribe in
        the text colour with the page's ground for its words (black on a light theme, white on a
        dark one), Subscribed grey (--track, its words --text), each readable and seen on the
        page's ground and on the phone's header (--surface); the dark screenshot."""
        b = self.b
        self.home(C)
        probe = """((k) => { const e = document.createElement('span'); e.style.color = `var(${k})`; document.body.append(e);
            const c = getComputedStyle(e).color; e.remove(); return c; })"""
        lum = r"""((c) => { const v = c.match(/\d+(\.\d+)?/g).slice(0, 3).map(Number).map(x => x / 255)
            .map(x => x <= 0.03928 ? x / 12.92 : Math.pow((x + 0.055) / 1.055, 2.4)); return 0.2126 * v[0] + 0.7152 * v[1] + 0.0722 * v[2]; })"""
        check = f"""(() => {{ const P = {probe}, L = {lum};
            const ratio = (a, b) => {{ const x = L(a), y = L(b); return (Math.max(x, y) + 0.05) / (Math.min(x, y) + 0.05); }};
            const cs = (sel, k) => getComputedStyle(document.querySelector(sel))[k];
            const on = cs('{GROW.format(self.gen)} .gl-open', 'color'), off = cs('{GROW.format(self.rl)} .gl-open', 'color');
            const ground = cs('#gcol', 'backgroundColor'), dot = cs('{GROW.format(self.gen)} .gl-dot', 'backgroundColor');
            const pill = cs('#gh .gh-sub', 'backgroundColor'), words = cs('#gh .gh-sub', 'color'), map = cs('#map', 'backgroundColor'),
                  surface = P('--surface');
            const subscribed = document.querySelector('#gh .gh-sub').getAttribute('aria-pressed') === 'true';
            return {{ on: on === P('--text'), off: off === P('--text-2'), ground: ground === P('--side'), dot: dot === P('--accent'),
                     pill: pill === P(subscribed ? '--track' : '--text'), words: words === P(subscribed ? '--text' : '--bg'),
                     r_on: ratio(on, ground), r_off: ratio(off, ground), r_dot: ratio(dot, ground), r_words: ratio(words, pill),
                     r_pill: Math.min(ratio(pill, map), ratio(pill, surface)), subscribed }};
        }})()"""
        themes = [t["v"] for t in self.js("pcgTheme.themes")]
        try:
            for gid, subscribed in ((self.rl, False), (self.gen, True)):
                b.js(f"location.hash = 'g={gid}'")
                self.wait(f"PaperMap.current.debug.cur().id === {J(gid)} && !!document.querySelector('#gh .gh-sub')"
                          f" && document.querySelector('#gh .gh-sub').getAttribute('aria-pressed') === {J(str(subscribed).lower())}", "its header")
                for theme in themes:
                    with self.subTest(theme=theme, subscribed=subscribed):
                        b.js(f"pcgTheme.set({J(theme)})")
                        got = self.js(check)
                        self.assertTrue(all(got[k] for k in ("on", "off", "ground", "dot", "pill", "words")), got)
                        for k in ("r_on", "r_off", "r_words"):
                            self.assertGreaterEqual(got[k], 4.5, f"{theme}: {k} {got}")
                        self.assertGreaterEqual(got["r_dot"], 3.0, f"{theme}: the dot {got}")
                        # Subscribe stands out of the map's ground; Subscribed is a quieter fill, but a fill (as YouTube's)
                        self.assertGreaterEqual(got["r_pill"], 3.0 if not subscribed else 1.1, f"{theme}: the pill {got}")      # YouTube's own grey is 1.1:1
                        if theme == "dark" and not subscribed:
                            time.sleep(0.4)
                            self.shot("desktop-dark")
        finally:
            b.js("pcgTheme.set('')")


def ts(iso):
    return 0 if not iso else time.mktime(time.strptime(iso, "%Y-%m-%dT%H:%M:%SZ"))


if __name__ == "__main__":
    unittest.main()
