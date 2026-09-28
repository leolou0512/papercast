#!/usr/bin/env python3
"""The search (hub/search.py) through GET /api/library, against the real hub.

    python3 -m unittest discover -s stacks/papercast-group/hub/tests -p 'test_search.py' -v

What is searched (the paper's own text, the episode's script, claims and explainer, the graphs a
paper is in, the people), typos corrected (and real words left alone), "phrases", the last word
as a prefix while typing, the ranking (a title beats the paper's text), every filter alone and
together, a graph filter that follows its members, the index following uploads, deletes and
renames, snippets that carry "<script>" as plain text, the index made again when it is missing
or from another version, and the speed on 1,000 synthetic papers with paper text (about 30 s).
Fake titles and people only."""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
import urllib.parse
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from web_rig import Rig  # noqa: E402

from hub import db, search  # noqa: E402

A, B, C = "alice@example.org", "bob@example.org", "carol@example.org"
SCRIPT = ("# How it works\n\nThe method is explained one small step at a time. It starts from a simple idea "
          "and builds on it until the picture is complete.\n")


class Base(unittest.TestCase):
    def get(self, q="", user=A, **f):
        args = {"q": q, **{k: v for k, v in f.items() if v is not None}}
        st, j, _ = self.r.req("GET", "/api/library?" + urllib.parse.urlencode(args), user=user)
        self.assertEqual(st, 200, j)
        return j

    def ids(self, q="", user=A, **f):
        return [p["id"] for p in self.get(q, user, **f)["papers"]]

    def files(self, eid, script=SCRIPT, claims=None, explainer=None, paper=None):
        d = self.r.cfg.episodes / eid
        d.mkdir(parents=True, exist_ok=True)
        (d / "script.md").write_text(script, encoding="utf-8")
        (d / "claims.md").write_text("---\ntitle: x\n---\n" + (claims or "- A claim of the paper.") + "\n", encoding="utf-8")
        (d / "explainer.json").write_text(json.dumps({"points": explainer or ["A point."], "figures": []}), encoding="utf-8")
        if paper is not None:
            (d / "paper.txt").write_text(paper, encoding="utf-8")

    def resync(self):
        search.sync(self.r.cfg, fuzzy=True)


class Search(Base):
    @classmethod
    def setUpClass(cls):
        r = cls.r = Rig()
        cls.alice = r.user(A, "Alice", "admin")
        cls.bob = r.user(B, "Bob", "contributor")
        cls.carol = r.user(C, "Carol", "viewer")
        t = cls()

        def paper(title, by, text=None, script=SCRIPT, tags=(), year=2023, authors=("Ada Fakerson",), **kw):
            pid = r.paper(title, by, authors=authors, year=year, tags=tags)
            eid = r.episode(pid, by)
            t.files(eid, script=script, paper=text, **kw)
            return pid, eid

        # the title says diffusion; another paper's text says it many times
        cls.p_title, _ = paper("Denoising Diffusion Made Up", cls.alice, tags=["generative models"], year=2020,
                               authors=["Jonathan Hofake", "Pieter Notreal"])
        cls.p_text, _ = paper("A Paper About Something Else", cls.bob, year=2024,
                              text="We study noise. " + "The diffusion of heat in the diffusion equation. " * 30
                              + "An odd word: quokkaglyph.")
        cls.p_script, _ = paper("Episodes Talk Too", cls.bob, script=SCRIPT + "\nThe marmotrix is the key idea here.\n", year=2021)
        cls.p_rl, _ = paper("Deep Reinforcement Learning from Pretend Preferences", cls.alice, tags=["reinforcement learning"],
                            year=2017, claims="- Reinforcement learning from comparisons works.")
        cls.p_tf, _ = paper("Attention Is All You Fake", cls.bob, tags=["language models"], year=2017,
                            explainer=["A transformer replaces recurrence with attention."])
        cls.p_phrase, _ = paper("Sampling Pictures", cls.carol, year=2022, text="Here denoising diffusion turns noise into pictures.")
        cls.p_apart, _ = paper("Two Words Apart", cls.carol, year=2022, text="Denoising is hard. Heat diffusion is slow.")
        cls.p_xss, _ = paper("A Hostile Text", cls.carol, year=2019,
                             text='Before it the zebrafinch <script>alert("x")</script> sings <b>loudly</b>.')
        cls.p_old, _ = paper("An Old Paper Without Any Text", cls.alice, year=1998, tags=["history"])
        t.resync()

    @classmethod
    def tearDownClass(cls):
        cls.r.close()

    # ------------------------------------------------------------ what is searched
    def test_words_only_in_the_paper_text_or_the_episode(self):
        j = self.get("quokkaglyph")
        self.assertEqual([p["id"] for p in j["papers"]], [self.p_text])
        m = j["papers"][0]["match"]
        self.assertEqual((m["where"], m["lead"]), ("paper", "in the paper: "))
        self.assertIn("quokkaglyph", m["parts"][1::2])                 # marked
        self.assertEqual(j["search"]["n"], 1)
        j = self.get("marmotrix")
        self.assertEqual([p["id"] for p in j["papers"]], [self.p_script])
        m = j["papers"][0]["match"]
        self.assertEqual((m["where"], m["lead"]), ("script", "in the episode: "))
        text = "".join(m["parts"])                                      # a little before, as context
        self.assertTrue(text.startswith("…") and text.endswith("The marmotrix is the key idea here."), text)
        self.assertLess(len(text), 200)
        # the claims and the explainer's points are the episode too
        self.assertEqual(self.ids("comparisons"), [self.p_rl])
        self.assertEqual(self.get("recurrence")["papers"][0]["match"]["where"], "explainer")
        # a paper with no text at all is still found by everything else
        self.assertEqual(self.ids("history"), [self.p_old])
        self.assertEqual(self.ids("1998"), [self.p_old])

    def test_people_graphs_and_ids(self):
        self.assertEqual(self.ids("hofake"), [self.p_title])            # an author
        self.assertEqual(self.get("hofake")["papers"][0]["match"]["lead"], "in the authors: ")
        self.assertEqual(set(self.ids("carol")), {self.p_phrase, self.p_apart, self.p_xss})   # who made it
        self.assertEqual(self.get("carol")["papers"][0]["match"]["lead"], "made by ")

    # ------------------------------------------------------------ typos, phrases, prefixes
    def test_typos_are_corrected(self):
        for typed, meant, want in (("difusion", "diffusion", self.p_title), ("reinforcment learnig", "reinforcement learning", self.p_rl),
                                   ("trasnformer", "transformer", self.p_tf)):
            j = self.get(typed + " ")
            self.assertEqual(j["search"].get("used"), meant, typed)
            self.assertIn(want, [p["id"] for p in j["papers"]], typed)
        # while typing too: the last word is no prefix of anything, so it is corrected
        self.assertEqual(self.get("reinforcment learnig")["search"].get("used"), "reinforcement learning")

    def test_real_words_are_left_alone(self):
        for q in ("diffusion", "attention", "learning", "denoising", "quokkaglyph", "zebrafinch", "heat"):
            j = self.get(q + " ")
            self.assertNotIn("used", j["search"], q)
            self.assertTrue(j["papers"], q)
        # nothing close to it: no correction, no papers
        j = self.get("xylophonic ")
        self.assertEqual((j["papers"], j["search"].get("used")), ([], None))

    def test_a_phrase_matches_exactly(self):
        both = self.ids("denoising diffusion ")
        self.assertIn(self.p_apart, both)
        phrase = self.ids('"denoising diffusion"')
        self.assertIn(self.p_phrase, phrase)
        self.assertIn(self.p_title, phrase)
        self.assertNotIn(self.p_apart, phrase)
        self.assertNotIn(self.p_text, phrase)
        # a phrase is never corrected
        self.assertEqual(self.ids('"difusion"'), [])

    def test_the_last_word_is_a_prefix_while_typing(self):
        self.assertEqual(self.ids("reinforc"), [self.p_rl])
        self.assertEqual(self.ids("quokkagl"), [self.p_text])          # in the paper's text too
        self.assertIn(self.p_title, self.ids("denoising diff"))
        self.assertEqual(self.ids("quokkagl "), [])                    # a finished word is a whole word
        # a word inside a longer one, in a title or a name (the trigram table), typed or finished
        self.assertEqual(self.ids("ttention "), [self.p_tf])
        self.assertEqual(self.ids("reinforc "), [self.p_rl])

    def test_little_words_are_dropped(self):
        self.assertEqual(self.ids("the marmotrix"), [self.p_script])

    # ------------------------------------------------------------ ranking and snippets
    def test_a_title_hit_beats_a_paper_text_hit(self):
        got = self.ids("diffusion ")
        self.assertLess(got.index(self.p_title), got.index(self.p_text), got)
        m = self.get("diffusion ")["papers"][0]["match"]
        self.assertEqual((m["where"], m["lead"]), ("title", "in the title"))
        self.assertEqual(m["title"], ["Denoising ", "Diffusion", " Made Up"])

    def test_newer_first_on_ties(self):
        # the same match (the maker's name) in two papers of different years
        got = self.ids("carol ")
        self.assertEqual(got[:2], [self.p_phrase, self.p_apart] if got[0] == self.p_phrase else [self.p_apart, self.p_phrase])
        self.assertEqual(got[-1], self.p_xss)                          # 2019 after the two of 2022

    def test_a_snippet_carries_html_as_plain_text(self):
        j = self.get("zebrafinch")
        m = j["papers"][0]["match"]
        text = "".join(m["parts"])
        self.assertIn('<script>alert("x")</script>', text)            # the page makes it text nodes
        self.assertEqual(m["parts"][1], "zebrafinch")
        self.assertTrue(all(isinstance(x, str) for x in m["parts"]))

    def test_snippets_for_rows_past_the_first_page(self):
        pids = ",".join([self.p_text, self.p_title, "p_aaaaaaaaaaaa"])
        st, j, _ = self.r.req("GET", f"/api/library?q=diffusion&ids={pids}")
        got = {p["id"]: p["match"] for p in j["papers"]}
        self.assertEqual(set(got), {self.p_text, self.p_title})
        self.assertEqual(got[self.p_text]["where"], "paper")
        self.assertEqual(got[self.p_title]["where"], "title")
        # without q, as before: the views alone
        st, j, _ = self.r.req("GET", f"/api/library?ids={self.p_text}")
        self.assertNotIn("match", j["papers"][0])
        self.assertNotIn("search", j)

    def test_the_library_without_a_search_is_as_before(self):
        j = self.get("")
        self.assertNotIn("search", j)
        self.assertEqual(len(j["papers"]), 9)
        self.assertTrue(all("match" not in p for p in j["papers"]))

    # ------------------------------------------------------------ filters
    def test_each_filter_alone_and_together(self):
        r = self.r
        self.assertEqual(self.ids(tag="reinforcement learning"), [self.p_rl])
        self.assertEqual(self.ids(tag="Reinforcement Learning"), [self.p_rl])          # any case
        self.assertEqual(set(self.ids(maker=self.carol)), {self.p_phrase, self.p_apart, self.p_xss})
        self.assertEqual(set(self.ids(year_from=2022)), {self.p_text, self.p_phrase, self.p_apart})
        self.assertEqual(set(self.ids(year_to=2017)), {self.p_rl, self.p_tf, self.p_old})
        self.assertEqual(set(self.ids(year_from=2017, year_to=2017)), {self.p_rl, self.p_tf})
        try:
            r.req("PUT", f"/api/papers/{self.p_tf}/listened", {"listened": True})
            self.assertEqual(self.ids(listened="yes"), [self.p_tf])
            self.assertNotIn(self.p_tf, self.ids(listened="no"))
            self.assertEqual(len(self.ids(listened="no")), 8)
            self.assertEqual(len(self.ids(listened="no", user=B)), 9)                  # Alice's tick, not Bob's
            # together, and with a search
            self.assertEqual(self.ids(year_to=2017, listened="no"), [self.p_old, self.p_rl]
                             if self.ids(year_to=2017, listened="no")[0] == self.p_old else [self.p_rl, self.p_old])
            self.assertEqual(self.ids("diffusion", maker=self.carol), [self.p_phrase, self.p_apart]
                             if self.ids("diffusion", maker=self.carol)[0] == self.p_phrase else [self.p_apart, self.p_phrase])
            self.assertEqual(self.ids("diffusion", maker=self.carol, year_to=2021), [])
            self.assertEqual(self.ids("attention", listened="yes"), [self.p_tf])
            self.assertEqual(self.ids("attention", listened="no"), [])
        finally:
            r.q("DELETE FROM listened")
        # filters alone come newest first, with the count
        j = self.get(maker=self.carol)
        self.assertEqual(j["search"]["n"], 3)
        self.assertEqual(j["papers"][0]["id"], max(j["papers"], key=lambda p: p["added_at"])["id"])
        st, j, _ = r.req("GET", "/api/library?year_from=soon")
        self.assertEqual((st, j["error"]), (400, "bad_filter"))

    def test_the_graph_filter_follows_its_members(self):
        r = self.r
        st, g, _ = r.req("POST", "/api/graphs", {"name": "Wombatology"})
        self.assertEqual(st, 201, g)
        gid = g["graph"]["id"]
        try:
            self.assertEqual(self.ids(graph=gid), [])
            r.req("POST", f"/api/graphs/{gid}/papers", {"paper_id": self.p_script})
            self.assertEqual(self.ids(graph=gid), [self.p_script])
            # its name is searched, and the snippet says whose graph it is
            j = self.get("wombatology")
            self.assertEqual([p["id"] for p in j["papers"]], [self.p_script])
            m = j["papers"][0]["match"]
            self.assertEqual((m["where"], m["lead"], m["parts"]), ("graph", "in your graph ", ["", "Wombatology", ""]))
            self.assertEqual(self.get("wombatology", user=B)["papers"][0]["match"]["lead"], "in Alice’s graph ")
            # renamed: found by the new name, not the old
            r.req("PUT", f"/api/graphs/{gid}", {"name": "Platypus Studies"})
            self.assertEqual(self.ids("platypus"), [self.p_script])
            self.assertEqual(self.ids("wombatology "), [])
            # a tag rule brings members in too; a paper taken out leaves
            r.req("PUT", f"/api/graphs/{gid}", {"tags": ["language models"]})
            self.assertEqual(set(self.ids(graph=gid)), {self.p_script, self.p_tf})
            r.req("DELETE", f"/api/graphs/{gid}/papers/{self.p_tf}")
            self.assertEqual(self.ids(graph=gid), [self.p_script])
            self.assertEqual(self.ids("attention", graph=gid), [])
            self.assertEqual(self.ids("marmotrix", graph=gid), [self.p_script])
        finally:
            r.req("DELETE", f"/api/graphs/{gid}")
        self.assertEqual(self.ids("platypus"), [])

    def test_facets(self):
        st, f, _ = self.r.req("GET", "/api/search/facets", user=B)
        self.assertEqual(st, 200)
        self.assertIn({"tag": "reinforcement learning", "n": 1}, f["tags"])
        self.assertEqual([m["name"] for m in f["makers"]], ["Alice", "Bob", "Carol"])
        self.assertEqual([m["me"] for m in f["makers"]], [False, True, False])
        self.assertEqual(f["years"], {"min": 1998, "max": 2024})

    # ------------------------------------------------------------ keeping in step
    def test_the_index_follows_deletes_and_new_versions(self):
        r = self.r
        pid = r.paper("A Paper That Comes And Goes", self.bob, year=2020)
        eid = r.episode(pid, self.bob)
        self.files(eid, paper="Its text holds the word capybarine.")
        self.assertEqual(self.ids("capybarine"), [pid])               # no event: found at once all the same
        st, _, _ = r.req("DELETE", f"/api/episodes/{eid}", user=B)
        self.assertEqual(st, 200)
        self.assertEqual(self.ids("capybarine"), [])
        r.req("POST", f"/api/episodes/{eid}/undelete", {}, user=B)
        self.assertEqual(self.ids("capybarine"), [pid])
        # a second version by someone else: its maker and its script are searched too
        e2 = r.episode(pid, self.carol)
        self.files(e2, script=SCRIPT + "\nThe second version mentions the dugongoid.\n")
        self.assertEqual(self.ids("dugongoid"), [pid])
        self.assertIn(pid, self.ids("carol"))
        r.q("UPDATE episodes SET deleted_at = ? WHERE paper_id = ?", db.now(), pid)
        self.assertEqual(self.ids("dugongoid"), [])
        self.assertEqual(self.ids("capybarine"), [])

    def test_a_label_is_searched_and_follows_its_edit(self):
        r = self.r
        st, j, _ = r.req("PUT", f"/api/papers/{self.p_apart}/label", {"label": "Numbatnet"})
        self.assertEqual(st, 200, j)
        try:
            j = self.get("numbatnet")
            self.assertEqual([p["id"] for p in j["papers"]], [self.p_apart])
            self.assertEqual(j["papers"][0]["match"]["lead"], "in the map label: ")
        finally:
            r.req("PUT", f"/api/papers/{self.p_apart}/label", {"label": None})
        self.assertEqual(self.ids("numbatnet "), [])

    def test_a_paper_text_that_arrives_later_is_found(self):
        """An import run again brings a paper.txt for a version the hub already has: no row
        changes, so the full comparison (every few minutes) finds it."""
        eid = self.r.q("SELECT id FROM episodes WHERE paper_id = ?", self.p_old)[0][0]
        (self.r.cfg.episodes / eid / "paper.txt").write_text("Late text about the pangolinoid.", encoding="utf-8")
        try:
            search.sync(self.r.cfg, full=True)
            self.assertEqual(self.ids("pangolinoid"), [self.p_old])
        finally:
            (self.r.cfg.episodes / eid / "paper.txt").unlink()
            search.sync(self.r.cfg, full=True)
        self.assertEqual(self.ids("pangolinoid"), [])


class Rebuild(Base):
    """search.db missing, or made by another version: made again, and searches meanwhile still
    find papers by their titles and people."""

    def setUp(self):
        r = self.r = Rig()
        self.alice = r.user(A, "Alice", "admin")
        self.pid = r.paper("Rebuilt Fake Paper", self.alice)
        eid = r.episode(self.pid, self.alice)
        self.files(eid, paper="The text says okapiform.")

    def tearDown(self):
        self.r.close()

    def test_missing_or_other_version(self):
        cfg = self.r.cfg
        self.resync()
        self.assertEqual(self.ids("okapiform"), [self.pid])
        st = search._state(cfg)
        search.close()
        with sqlite_file(st.path) as c:
            c.execute("UPDATE meta SET value = 'old' WHERE key = 'schema'")
        st.checked = st.made = False
        st.synced = None
        self.resync()
        self.assertEqual(self.ids("okapiform"), [self.pid])
        search.close()
        for x in ("", "-wal", "-shm"):
            Path(str(st.path) + x).unlink(missing_ok=True)
        st.checked = st.made = False
        st.synced = None
        self.assertTrue(search.idle(cfg, 20))                         # the hub's own worker makes it again
        self.assertEqual(self.ids("okapiform"), [self.pid])

    def test_while_the_first_build_runs(self):
        cfg = self.r.cfg
        st = search._state(cfg)
        with st.lock:                   # the worker's first build, still going
            st.synced = None
            search.close()
            for x in ("", "-wal", "-shm"):
                Path(str(st.path) + x).unlink(missing_ok=True)
            st.checked = st.made = False
            j = self.get("rebuilt")
            self.assertEqual([p["id"] for p in j["papers"]], [self.pid])
            self.assertIn("indexing", j["search"])
            self.assertEqual(self.ids("okapiform"), [])                # its text is not read yet
        self.assertTrue(search.idle(cfg, 20))
        self.assertEqual(self.ids("okapiform"), [self.pid])


class sqlite_file:
    def __init__(self, path):
        import sqlite3
        self.c = sqlite3.connect(str(path), isolation_level=None)

    def __enter__(self):
        return self.c

    def __exit__(self, *a):
        self.c.close()


class Upload(unittest.TestCase):
    """A bundle with the paper's text, through the CLI API: searched at once; its text kept
    to 2 MB; a delete takes it out of the answers."""

    @classmethod
    def setUpClass(cls):
        from contrib_harness import Hub
        cls.h = Hub()
        cls.u = cls.h.user("Alice", "contributor")
        cls.h.base_prompt()

    @classmethod
    def tearDownClass(cls):
        cls.h.close()

    def browse(self, q):
        st, j = self.h.request("GET", "/api/library?q=" + urllib.parse.quote(q), headers={"X-Test-User": self.u["email"]})
        self.assertEqual(st, 200, j)
        return [p["id"] for p in j["papers"]]

    def test_upload_then_delete(self):
        from contrib_harness import bundle, manifest
        h = self.h
        st, cl = h.claim(self.u, arxiv_id="2011.99999", title="An Uploaded Fake Paper")
        self.assertEqual(st, 201, cl)
        m = manifest(claim_id=cl["claim_id"], paper_over={"arxiv_id": "2011.99999", "title": "An Uploaded Fake Paper"})
        m["files"]["paper_text"] = "paper.txt"
        text = ("The uploaded text names the axolotlium once. " + "Filler words about nothing much. " * 90_000).encode()
        self.assertGreater(len(text), 2 * 1024 * 1024)
        st, out = h.upload(self.u, bundle(m, extra=[("paper.txt", text)]))
        self.assertEqual(st, 201, out)
        ep = h.wait_checked(self.u, out["episode_id"])
        self.assertEqual(ep["state"], "waiting-for-gpu", ep)
        stored = h.cfg.episodes / out["episode_id"] / "paper.txt"
        self.assertLessEqual(stored.stat().st_size, 2 * 1024 * 1024)
        self.assertTrue(stored.read_bytes().startswith(b"The uploaded text names the axolotlium"))
        self.assertEqual(self.browse("axolotlium"), [out["paper_id"]])
        self.assertEqual(self.browse("2011.99999"), [out["paper_id"]])
        # never served: there is no route to it
        st, _ = h.request("GET", f"/x/{out['episode_id']}/paper.txt", headers={"X-Test-User": self.u["email"]})
        self.assertEqual(st, 404)
        st, _ = h.request("DELETE", f"/api/episodes/{out['episode_id']}", headers={"X-Test-User": self.u["email"], "X-PCG": "1"})
        self.assertEqual(st, 200)
        self.assertEqual(self.browse("axolotlium"), [])

    def test_a_bundle_without_the_text_still_lands(self):
        from contrib_harness import bundle, manifest
        h = self.h
        st, cl = h.claim(self.u, arxiv_id="2011.88888", title="A Fake Paper Without Its Text")
        m = manifest(claim_id=cl["claim_id"], paper_over={"arxiv_id": "2011.88888", "title": "A Fake Paper Without Its Text"})
        st, out = h.upload(self.u, bundle(m))
        self.assertEqual(st, 201, out)
        h.wait_checked(self.u, out["episode_id"])
        self.assertFalse((h.cfg.episodes / out["episode_id"] / "paper.txt").exists())
        self.assertEqual(self.browse("without its text"), [out["paper_id"]])
        # the script is searched: the harness's sentences
        self.assertIn(out["paper_id"], self.browse("gaussian cloud"))


class Units(unittest.TestCase):
    def test_parse(self):
        cls = search.parse('Reinforcment "denoising diffusion" q-learning the lea')
        self.assertEqual([(c.toks, c.prefix, c.phrase) for c in cls],
                         [(["reinforcment"], False, False), (["denoising", "diffusion"], False, True),
                          (["q", "learning"], False, True), (["lea"], True, False)])
        self.assertEqual([c.toks for c in search.parse("the of ")], [["the"], ["of"]])   # nothing else: kept
        self.assertEqual([c.toks for c in search.parse("the of")], [["of"]])             # "of…" is being typed
        self.assertTrue(search.parse('"unclosed phr')[0].prefix)

    def test_distance(self):
        self.assertEqual(search._osa("trasnformer", "transformer", 2), 1)
        self.assertEqual(search._osa("difusion", "diffusion", 2), 1)
        self.assertEqual(search._osa("learnig", "learning", 1), 1)
        self.assertEqual(search._osa("kitten", "sitting", 2), 3)
        self.assertEqual(search._osa("abc", "abcdef", 1), 2)

    def test_fold_keeps_offsets(self):
        s = "Naïve Schrödinger ﬁts İstanbul"
        f = search.fold(s)
        self.assertEqual(len(f), len(s))
        self.assertTrue(f.startswith("naive schrodinger"))

    def test_marked_parts(self):
        rx = [search._matcher(c, False) for c in search.parse("diffusion model ")]
        self.assertEqual(search._parts("Diffusion models and a diffusion model", rx),
                         ["", "Diffusion", " models and a ", "diffusion", " ", "model", ""])
        self.assertEqual(search._parts("nothing here", rx), [])


class Speed(unittest.TestCase):
    """1,000 synthetic papers, each with a 94 kB paper text (Leo's own average), indexed; each
    query's search in under 50 ms (the median of 5). Prints the numbers."""

    def test_a_thousand_papers(self):
        sys.path.insert(0, str(HERE.parents[1]))
        from tools import searchcheck
        tmp = tempfile.mkdtemp(prefix="pcg-search-speed-")
        was = db._path
        try:
            b = searchcheck.build(Path(tmp) / "data", 1000, 94)
            rows = searchcheck.run_queries(b["cfg"], 5, b["rare"])
            st = search.status(b["cfg"])
            print(f"\n1,000 papers, 94 kB of text each: first build {b['index_s']:.1f} s, vocabulary {b['vocab_s']:.1f} s, "
                  f"search.db {st['bytes'] / 1e6:.0f} MB", file=sys.stderr)
            for r in rows:
                print(f"  {r['name']:32} {r['q'][:26]:26} {r['n']:>5} papers  search {r['search_ms']:5.1f} ms "
                      f"(slowest {r['search_max']:5.1f}), answer {r['answer_ms']:5.1f} ms", file=sys.stderr)
            for r in rows:
                self.assertLess(r["search_ms"], 50, r)
            fixed = {r["q"]: r["used"] for r in rows if r["used"]}
            self.assertEqual(fixed, {"difusion": "diffusion", "reinforcment learnig": "reinforcement learning",
                                     "trasnformer": "transformer"})
        finally:
            search.close()
            db.close()
            if was is not None:
                db._path = was
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
