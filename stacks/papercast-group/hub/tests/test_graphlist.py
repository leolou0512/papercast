"""The graph list (hub/graphlist.py, graph.py; DESIGN.md, Leo's decisions of 2026-10-03), over HTTP
against a running hub: subscriptions (a toggle only; a graph's maker is subscribed, nobody else),
each graph's counts for one person (papers, unheard, "N new" since they last opened it), its
times (updated, created, the newest paper's), who may change a graph (its maker and admins, when
someone made it; anyone but for a lock, when nobody did; links everyone's), "Not in any graph",
the account's choices (the graph open last, the sort), the search's graphs, migration 7.
Run: python3 -m unittest discover -s stacks/papercast-group/hub/tests -t stacks/papercast-group -p 'test_graphlist.py'"""
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from hub.tests.graph_testlib import Hub, db, graph  # noqa: E402
from hub import config as C  # noqa: E402
from hub import events, graphlist, listening  # noqa: E402


class Clock:
    """db.now() under the test's hand: each tick() a minute on (the hub's times have seconds)."""

    def __init__(self, t="2026-10-01T09:00:00Z"):
        from datetime import datetime, timezone
        self.t = datetime.strptime(t, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)

    def __call__(self):
        return self.t.strftime("%Y-%m-%dT%H:%M:%SZ")

    def tick(self, minutes=1):
        from datetime import timedelta
        self.t += timedelta(minutes=minutes)
        return self()


class Base(unittest.TestCase):
    def setUp(self):
        self.h = Hub()
        self.clock = Clock()
        p = mock.patch.object(db, "now", self.clock)
        p.start()
        self.addCleanup(p.stop)
        # the seed graphs (no maker) are left out of the way: these tests make their own
        db.conn().execute("UPDATE graphs SET deleted_at = '2026-01-01T00:00:00Z'")

    def tearDown(self):
        self.h.close()

    def paper(self, title, tags=(), year=2020, at=None, live=True):
        """A paper with one version made at `at` (its first live version: when it joins by its tags)."""
        pid = self.h.paper(title, year, tags)
        at = at or self.clock.tick()
        db.conn().execute("INSERT INTO episodes(id, paper_id, made_by, state, created_at, updated_at, deleted_at) "
                          "VALUES (?, ?, ?, 'ready', ?, ?, ?)", (db.new_id("e_"), pid, self.h.alice, at, at, None if live else at))
        return pid

    def episode_of(self, pid):
        return db.conn().execute("SELECT id FROM episodes WHERE paper_id = ? ORDER BY created_at LIMIT 1", (pid,)).fetchone()[0]

    def make(self, name, tags=(), who="alice"):
        self.clock.tick()
        return self.h.ok("POST", "/api/graphs", {"name": name, "tags": list(tags)}, who=who, code=201)["graph"]["id"]

    def items(self, who="alice"):
        return self.h.ok("GET", "/api/graphs", who=who)

    def item(self, gid, who="alice"):
        r = self.items(who)
        return r["unfiled"] if gid == "none" else next(g for g in r["graphs"] if g["id"] == gid)


class TestSubscriptions(Base):
    def test_the_maker_alone_starts_subscribed_and_the_toggle_is_each_persons(self):
        h = self.h
        g = self.make("Alice's graph", ["x"])
        self.assertTrue(self.item(g)["subscribed"])
        self.assertFalse(self.item(g, "bob")["subscribed"])
        self.assertFalse(self.item(g, "root")["subscribed"])
        got = h.ok("PUT", f"/api/graphs/{g}/subscription", {"subscribed": True}, who="bob")["graph"]
        self.assertEqual((got["id"], got["subscribed"]), (g, True))
        self.assertTrue(self.item(g, "bob")["subscribed"])
        h.ok("PUT", f"/api/graphs/{g}/subscription", {"subscribed": False}, who="alice")
        self.assertFalse(self.item(g)["subscribed"])
        self.assertTrue(self.item(g, "bob")["subscribed"])
        # the same twice is the same
        h.ok("PUT", f"/api/graphs/{g}/subscription", {"subscribed": True}, who="bob")
        self.assertEqual(db.conn().execute("SELECT count(*) FROM graph_subs WHERE graph_id = ?", (g,)).fetchone()[0], 1)

    def test_viewing_or_listening_never_subscribes(self):
        h = self.h
        g = self.make("G", ["x"], who="bob")
        p = self.paper("A paper", ["x"])
        h.ok("GET", f"/api/graphs/{g}")
        h.ok("POST", f"/api/graphs/{g}/opened", {})
        h.ok("PUT", f"/api/papers/{p}/listened", {"listened": True})
        self.assertFalse(self.item(g)["subscribed"])

    def test_refusals_and_the_event_to_their_own_tabs(self):
        h = self.h
        g = self.make("G")
        self.assertEqual(h.req("PUT", f"/api/graphs/{g}/subscription", {"subscribed": "yes"})[0], 400)
        self.assertEqual(h.req("PUT", "/api/graphs/g_nosuchgraph/subscription", {"subscribed": True})[0], 404)
        self.assertEqual(h.req("PUT", "/api/graphs/none/subscription", {"subscribed": True})[0], 404)
        h.ok("DELETE", f"/api/graphs/{g}")
        self.assertEqual(h.req("PUT", f"/api/graphs/{g}/subscription", {"subscribed": True})[0], 404)
        g2 = self.make("G2")
        mine, theirs = events.subscribe(h.bob), events.subscribe(h.alice)
        try:
            h.ok("PUT", f"/api/graphs/{g2}/subscription", {"subscribed": True}, who="bob")
            kinds = []
            while not mine.q.empty():
                kinds.append(mine.q.get_nowait()[1])
            self.assertIn("mygraphs", kinds)
            self.assertNotIn("mygraphs", [theirs.q.get_nowait()[1] for _ in range(theirs.q.qsize())])
        finally:
            events.unsubscribe(mine)
            events.unsubscribe(theirs)


class TestCounts(Base):
    def test_papers_unheard_and_heard_by_a_tick_or_a_finished_version(self):
        h = self.h
        g = self.make("G", ["x"])
        a, b, c = self.paper("Alpha", ["x"]), self.paper("Beta", ["x"]), self.paper("Gamma", ["x"])
        self.paper("Elsewhere", ["y"])
        it = self.item(g)
        self.assertEqual((it["n"], it["unheard"]), (3, 3))
        h.ok("PUT", f"/api/papers/{a}/listened", {"listened": True})
        listening.ensure_schema()
        # Bob finished a version of b (his own heard); Alice finished a version of c that is deleted since
        db.conn().execute("INSERT INTO listen_finished(user_id, episode_id, day, at) VALUES (?, ?, '2026-10-01', ?)", (h.bob, self.episode_of(b), db.now()))
        e2 = db.new_id("e_")
        db.conn().execute("INSERT INTO episodes(id, paper_id, made_by, state, created_at, updated_at, deleted_at) VALUES (?, ?, ?, 'ready', ?, ?, ?)",
                          (e2, c, h.alice, db.now(), db.now(), db.now()))
        db.conn().execute("INSERT INTO listen_finished(user_id, episode_id, day, at) VALUES (?, ?, '2026-10-01', ?)", (h.alice, e2, db.now()))
        self.assertEqual(self.item(g)["unheard"], 1)                 # b only
        self.assertEqual(self.item(g, "bob")["unheard"], 2)          # a and c
        # the map's nodes and the library say the same
        heard = {n["id"]: n["heard"] for n in h.ok("GET", f"/api/graphs/{g}")["nodes"]}
        self.assertEqual(heard, {a: True, b: False, c: True})
        lib = {p["id"]: (p["heard"], p["listened"]) for p in h.ok("GET", "/api/library")["papers"]}
        self.assertEqual((lib[a], lib[b], lib[c]), ((True, True), (False, False), (True, False)))
        # "Delete my listening history" takes the finished ones away; the tick stays
        with db.transaction() as c:
            listening.forget(c, h.alice)
        self.assertEqual(self.item(g)["unheard"], 2)

    def test_new_counts_papers_that_joined_since_the_last_opening(self):
        h = self.h
        g = self.make("G", ["x"])                     # Alice made it: subscribed now
        self.paper("Old one", ["x"], at="2026-09-01T00:00:00Z")      # in the library before: not new to her
        self.assertEqual(self.item(g)["new"], 0)
        p1 = self.paper("First new", ["x"])            # joins by its tags when its first version comes
        self.paper("Second new", ["x"])
        self.assertEqual(self.item(g)["new"], 2)
        self.clock.tick()
        h.ok("POST", f"/api/graphs/{g}/opened", {})
        self.assertEqual(self.item(g)["new"], 0)
        # added by hand: when it was added, however old the paper
        old = self.paper("An old paper", ["y"], at="2026-08-01T00:00:00Z")
        self.clock.tick()
        h.ok("POST", f"/api/graphs/{g}/papers", {"paper_id": old})
        it = self.item(g)
        self.assertEqual((it["n"], it["new"]), (4, 1))
        self.assertEqual(it["newest_at"], self.clock())
        # Bob never opened it and is not subscribed: nothing is new to him
        self.assertEqual(self.item(g, "bob")["new"], 0)
        # subscribed later: new from then on
        h.ok("PUT", f"/api/graphs/{g}/subscription", {"subscribed": True}, who="bob")
        self.assertEqual(self.item(g, "bob")["new"], 0)
        self.paper("Third new", ["x"])
        self.assertEqual(self.item(g, "bob")["new"], 1)
        self.assertEqual(self.item(g)["new"], 2)
        self.assertNotEqual(self.item(g)["newest_at"], None)
        self.assertTrue(p1)

    def test_deleting_the_oldest_version_does_not_make_a_paper_new_again(self):
        h = self.h
        p = self.paper("Old paper", ["x"], at="2026-01-10T00:00:00Z")          # Alice's January version
        e1 = self.episode_of(p)
        e2 = db.new_id("e_")
        db.conn().execute("INSERT INTO episodes(id, paper_id, made_by, state, created_at, updated_at) VALUES (?, ?, ?, 'ready', ?, ?)",
                          (e2, p, h.bob, "2026-10-01T10:00:00Z", "2026-10-01T10:00:00Z"))    # and Bob's, after he opened the graph
        g = self.make("G", ["x"], who="bob")
        self.clock.tick()
        h.ok("POST", f"/api/graphs/{g}/opened", {}, who="bob")
        before = self.item(g, "bob")
        self.assertEqual((before["new"], before["newest_at"]), (0, "2026-01-10T00:00:00Z"))
        self.clock.tick()
        h.ok("DELETE", f"/api/episodes/{e1}")                                        # the oldest version goes
        after = self.item(g, "bob")
        self.assertEqual((after["n"], after["new"], after["newest_at"], after["updated_at"]),
                         (1, 0, before["newest_at"], before["updated_at"]))
        # all its versions gone and one back: in the library from the first one still
        h.ok("DELETE", f"/api/episodes/{e2}", who="bob")
        h.ok("POST", f"/api/episodes/{e2}/undelete", {}, who="bob")
        self.assertEqual((self.item(g, "bob")["new"], self.item(g, "bob")["newest_at"]), (0, "2026-01-10T00:00:00Z"))

    def test_papers_a_change_of_tags_brings_in_are_new(self):
        h = self.h
        a = self.paper("Alpha", ["x"], at="2026-08-01T00:00:00Z")
        b2 = self.paper("Beta", ["y"], at="2026-08-02T00:00:00Z")
        c2 = self.paper("Gamma", ["y"], at="2026-08-03T00:00:00Z")
        g = self.make("G", ["x"])                        # Alice made it: subscribed
        h.ok("POST", f"/api/graphs/{g}/papers", {"paper_id": c2})       # added by hand, then taken out by hand:
        h.ok("DELETE", f"/api/graphs/{g}/papers/{c2}")                 # no change of tags brings it in
        self.clock.tick()
        h.ok("POST", f"/api/graphs/{g}/opened", {})
        self.assertEqual(self.item(g)["new"], 0)
        at = self.clock.tick()
        h.ok("PUT", f"/api/graphs/{g}", {"tags": ["x", "y"]})
        it = self.item(g)
        self.assertEqual((it["n"], it["new"], it["newest_at"], it["updated_at"]), (2, 1, at, at))
        self.assertEqual(db.conn().execute("SELECT paper_id, at FROM graph_tag_joins WHERE graph_id = ?", (g,)).fetchall()[0][:], (b2, at))
        # undone: it leaves; redone: it comes in again, then
        self.clock.tick()
        st, _ = h.undo("mine")
        self.assertEqual(st, 200)
        self.assertEqual((self.item(g)["n"], self.item(g)["new"]), (1, 0))
        again = self.clock.tick()
        st, _ = h.undo("mine", redo=True)
        self.assertEqual(st, 200)
        it = self.item(g)
        self.assertEqual((it["n"], it["new"], it["newest_at"]), (2, 1, again))
        # opened: seen; a paper already in by a tag stays as it was when another tag comes
        self.clock.tick()
        h.ok("POST", f"/api/graphs/{g}/opened", {})
        self.clock.tick()
        h.ok("PUT", f"/api/graphs/{g}", {"tags": ["x", "y", "z"]})
        self.assertEqual(self.item(g)["new"], 0)
        self.assertTrue(a)

    def test_updated_at_moves_with_every_change_to_the_graph(self):
        h = self.h
        g = self.make("G", ["x"])
        made = self.clock()
        it = self.item(g)
        self.assertEqual((it["created_at"], it["updated_at"], it["newest_at"]), (made, made, None))
        a = self.paper("Alpha", ["x"], year=2018)          # joins by its tags at its upload
        self.assertEqual(self.item(g)["updated_at"], self.clock())
        b = self.paper("Beta", ["x"], year=2020)
        for change in (lambda: h.ok("PUT", f"/api/graphs/{g}", {"name": "G2"}),                       # its settings
                       lambda: h.ok("PUT", f"/api/graphs/{g}", {"tags": ["x", "z"]}),
                       lambda: h.ok("PUT", f"/api/graphs/{g}", {"locked": True}, who="root"),
                       lambda: h.ok("PUT", f"/api/graphs/{g}", {"locked": False}, who="root"),
                       lambda: h.ok("POST", "/api/links", {"src": a, "dst": b, "grade": "s"}, code=201),   # a link in it
                       lambda: h.ok("DELETE", f"/api/graphs/{g}/papers/{a}"),                          # its papers
                       lambda: h.ok("POST", f"/api/graphs/{g}/papers", {"paper_id": a})):
            at = self.clock.tick()
            change()
            self.assertEqual(self.item(g)["updated_at"], at)
        # its paper's last version deleted: the paper leaves it, a change; brought back, another
        e = self.episode_of(b)
        at = self.clock.tick()
        h.ok("DELETE", f"/api/episodes/{e}")
        self.assertEqual((self.item(g)["updated_at"], self.item(g)["n"]), (at, 1))
        at = self.clock.tick()
        h.ok("POST", f"/api/episodes/{e}/undelete", {})
        self.assertEqual((self.item(g)["updated_at"], self.item(g)["n"]), (at, 2))
        # a change elsewhere does not move it
        other = self.make("Other", ["q"])
        self.clock.tick()
        h.ok("PUT", f"/api/graphs/{other}", {"name": "Other2"})
        self.assertEqual(self.item(g)["updated_at"], at)


class TestRights(Base):
    def test_a_graph_someone_made_is_its_makers_and_the_admins(self):
        h = self.h
        g = self.make("Bob's", ["x"], who="bob")
        p = self.paper("A paper", ["x"])
        q = self.paper("Another", ["y"])
        for method, path, body in (("PUT", f"/api/graphs/{g}", {"name": "Taken"}),
                                   ("PUT", f"/api/graphs/{g}", {"tags": ["y"]}),
                                   ("POST", f"/api/graphs/{g}/papers", {"paper_id": q}),
                                   ("DELETE", f"/api/graphs/{g}/papers/{p}", None),
                                   ("DELETE", f"/api/graphs/{g}", None)):
            st, js = h.req(method, path, body, who="alice")
            self.assertEqual((st, js["error"]), (403, "not_yours"), (method, path))
        self.assertEqual(self.item(g)["name"], "Bob's")
        it = self.item(g, "alice")
        self.assertEqual((it["can_edit"], it["can_delete"], it["can_link"]), (False, False, True))
        it = self.item(g, "bob")
        self.assertEqual((it["can_edit"], it["can_delete"], it["can_link"]), (True, True, True))
        it = self.item(g, "root")
        self.assertEqual((it["can_edit"], it["can_delete"], it["can_link"]), (True, True, True))
        h.ok("POST", f"/api/graphs/{g}/papers", {"paper_id": q}, who="bob")
        h.ok("PUT", f"/api/graphs/{g}", {"name": "Root's now"}, who="root")
        # its view says so too
        self.assertEqual(h.ok("GET", f"/api/graphs/{g}", who="alice")["graph"]["can_edit"], False)
        # links stay everyone's: Alice links two of its papers, and takes her link out
        lk = h.ok("POST", "/api/links", {"src": p, "dst": q, "grade": "w"}, who="alice", code=201)["link"]
        h.ok("PUT", f"/api/links/{lk['id']}", {"grade": "s"}, who="alice")
        h.ok("DELETE", f"/api/links/{lk['id']}", who="alice")
        # and labels
        h.ok("PUT", f"/api/papers/{p}/label", {"label": "Mine"}, who="alice")
        # the maker deletes it
        h.ok("DELETE", f"/api/graphs/{g}", who="bob")

    def test_undo_follows_the_same_rights(self):
        h = self.h
        g = self.make("Bob's", ["x"], who="bob")
        q = self.paper("Another", ["y"])
        h.ok("POST", f"/api/graphs/{g}/papers", {"paper_id": q}, who="bob")
        added = h.ok("GET", "/api/graph-log", who="bob")["undo"]["mine"]["id"]
        # Alice may not undo Bob's changes to his graph: Undo does not offer them, and asked anyway it is refused
        self.assertIsNone(h.ok("GET", "/api/graph-log", who="alice")["undo"]["any"])
        st, js = h.req("POST", "/api/graph-log/revert", {"scope": "any", "expect": added}, who="alice")
        self.assertEqual((st, js["error"]), (409, "moved"))
        self.assertIn(q, graph._world().members[g])
        st, _ = h.undo("mine", who="bob")
        self.assertEqual(st, 200)
        self.assertNotIn(q, graph._world().members[g])

    def test_undo_and_redo_offer_only_what_this_person_may_undo(self):
        h = self.h
        seed = db.conn().execute("SELECT id FROM graphs WHERE created_by IS NULL ORDER BY rowid LIMIT 1").fetchone()[0]
        db.conn().execute("UPDATE graphs SET deleted_at = NULL WHERE id = ?", (seed,))
        g = self.make("Bob's", ["x"], who="bob")
        a, b2 = self.paper("Alpha", ["x"], year=2018), self.paper("Beta", ["x"], year=2020)
        self.clock.tick()
        h.ok("PUT", f"/api/graphs/{seed}", {"name": "Renamed by Bob"}, who="bob")        # anyone may undo this one
        renamed = h.ok("GET", "/api/graph-log", who="bob")["undo"]["mine"]["id"]
        self.clock.tick()
        h.ok("DELETE", f"/api/graphs/{g}/papers/{a}", who="bob")                      # Bob's own graph: his and the admins'
        removed = h.ok("GET", "/api/graph-log", who="bob")["undo"]["mine"]["id"]
        log = h.ok("GET", "/api/graph-log", who="alice")
        self.assertEqual(log["undo"]["any"]["id"], renamed)                           # the newest she may undo
        self.assertEqual(h.ok("GET", "/api/graph-log", who="root")["undo"]["any"]["id"], removed)
        st, js = h.undo("any", who="alice")
        self.assertEqual(st, 200, js)
        self.assertEqual(js["reverted"]["id"], renamed)
        self.assertEqual(h.ok("GET", "/api/graph-log", who="alice")["redo"]["any"]["revert_of"], renamed)
        # her redo of it works; Bob's removal stays offered to Bob and to admins only
        st, _ = h.undo("any", who="alice", redo=True)
        self.assertEqual(st, 200)
        self.assertEqual(h.ok("GET", "/api/graph-log", who="bob")["undo"]["mine"]["id"], removed)
        # an admin's change to Bob's graph, the admin made a viewer since: no longer offered to them
        h.ok("POST", f"/api/graphs/{g}/papers", {"paper_id": a}, who="root")
        self.assertEqual(h.ok("GET", "/api/graph-log", who="root")["undo"]["mine"]["op"], "graph.add_paper")
        db.conn().execute("UPDATE users SET role = 'viewer' WHERE id = ?", (h.root,))
        mine = h.ok("GET", "/api/graph-log", who="root")["undo"]["mine"]
        self.assertTrue(mine is None or mine["op"] != "graph.add_paper", mine)
        # a link among a locked graph's papers: the admins' to undo, not offered to others
        db.conn().execute("UPDATE users SET role = 'admin' WHERE id = ?", (h.root,))
        lk = h.ok("POST", "/api/links", {"src": a, "dst": b2, "grade": "s"}, who="alice", code=201)["link"]
        h.ok("PUT", f"/api/graphs/{g}", {"locked": True}, who="root")
        self.assertNotEqual((h.ok("GET", "/api/graph-log", who="alice")["undo"]["mine"] or {}).get("op"), "link.add")
        self.assertEqual(h.ok("GET", "/api/graph-log", who="root")["undo"]["any"]["op"], "graph.lock")
        self.assertEqual(lk["src"], a)

    def test_a_graph_nobody_made_is_open_to_all_but_for_a_lock(self):
        h = self.h
        gid = graph.SEED_GRAPHS and db.conn().execute("SELECT id FROM graphs WHERE created_by IS NULL ORDER BY rowid LIMIT 1").fetchone()[0]
        db.conn().execute("UPDATE graphs SET deleted_at = NULL WHERE id = ?", (gid,))
        p = self.paper("A paper", ["z"])
        h.ok("PUT", f"/api/graphs/{gid}", {"name": "Renamed by Alice"}, who="alice")
        h.ok("POST", f"/api/graphs/{gid}/papers", {"paper_id": p}, who="bob")
        it = self.item(gid, "alice")
        self.assertEqual((it["can_edit"], it["can_delete"], it["can_link"]), (True, False, True))
        self.assertEqual(h.req("DELETE", f"/api/graphs/{gid}", who="alice")[0], 403)   # a seed graph: an admin's to delete
        h.ok("PUT", f"/api/graphs/{gid}", {"locked": True}, who="root")
        st, js = h.req("PUT", f"/api/graphs/{gid}", {"name": "Again"}, who="alice")
        self.assertEqual((st, js["error"]), (403, "locked"))
        it = self.item(gid, "alice")
        self.assertEqual((it["can_edit"], it["can_link"]), (False, False))
        h.ok("PUT", f"/api/graphs/{gid}", {"name": "An admin's"}, who="root")


class TestUnfiled(Base):
    def test_papers_with_a_live_version_in_no_graph(self):
        h = self.h
        g = self.make("G", ["x"])
        inside, out1, out2 = self.paper("Inside", ["x"]), self.paper("Out one", ["y"], year=2019), self.paper("Out two", [], year=2021)
        self.paper("Gone", ["y"], live=False)
        un = self.items()["unfiled"]
        self.assertEqual((un["id"], un["name"], un["n"], un["pseudo"], un["subscribed"]), ("none", "Not in any graph", 2, True, False))
        self.assertEqual((un["can_edit"], un["can_link"]), (False, True))
        v = h.ok("GET", "/api/graphs/none")
        self.assertEqual([n["id"] for n in v["nodes"]], [out1, out2])            # time order, as any graph
        self.assertEqual((v["graph"]["id"], v["rev"]), ("none", None))
        # links among them are everyone's; it takes no papers
        h.ok("POST", "/api/links", {"src": out1, "dst": out2, "grade": "s"}, code=201)
        self.assertEqual(len(h.ok("GET", "/api/graphs/none")["links"]), 1)
        self.assertEqual(h.req("POST", "/api/graphs/none/papers", {"paper_id": out1})[0], 404)
        # added to a graph, it leaves
        h.ok("POST", f"/api/graphs/{g}/papers", {"paper_id": out1})
        self.assertEqual(self.items()["unfiled"]["n"], 1)
        # the library says which graphs a paper is in
        lib = {p["id"]: p["graphs"] for p in h.ok("GET", "/api/library")["papers"]}
        self.assertEqual((lib[inside], lib[out1], lib[out2]), ([g], [g], []))
        # and each node, which graphs it is in
        self.assertEqual({n["id"]: n["in"] for n in h.ok("GET", f"/api/graphs/{g}")["nodes"]}, {inside: [g], out1: [g]})


class TestUiState(Base):
    def test_the_graph_open_last_and_the_sort_are_the_accounts(self):
        h = self.h
        g = self.make("G")
        self.assertEqual(h.ok("GET", "/api/ui-state"), {"last_graph": None, "sort": "updated"})
        self.assertEqual(self.items()["ui"], {"last_graph": None, "sort": "updated"})
        h.ok("POST", f"/api/graphs/{g}/opened", {})
        self.assertEqual(h.ok("GET", "/api/ui-state")["last_graph"], g)
        self.assertEqual(h.ok("GET", "/api/ui-state", who="bob")["last_graph"], None)       # each person's
        h.ok("POST", "/api/graphs/none/opened", {})
        self.assertEqual(h.ok("GET", "/api/ui-state")["last_graph"], "none")
        for s in graphlist.SORTS:
            self.assertEqual(h.ok("PUT", "/api/ui-state", {"sort": s})["sort"], s)
        self.assertEqual(h.ok("PUT", "/api/ui-state", {"last_graph": None})["last_graph"], None)
        self.assertEqual(h.req("PUT", "/api/ui-state", {"sort": "random"})[0], 400)
        self.assertEqual(h.req("PUT", "/api/ui-state", {"last_graph": "../etc"})[0], 400)
        self.assertEqual(h.req("PUT", "/api/ui-state", {})[0], 400)
        self.assertEqual(h.req("POST", "/api/graphs/g_nosuchgraph/opened", {})[0], 404)
        self.assertEqual(self.items()["ui"]["sort"], graphlist.SORTS[-1])


class TestSearch(Base):
    def test_the_search_finds_graphs_by_their_names(self):
        h = self.h
        g1, g2 = self.make("Diffusion models", ["x"]), self.make("Robot hands", ["y"])
        self.paper("Some paper", ["x"])
        r = h.ok("GET", "/api/library?q=diffu")
        self.assertEqual([x["id"] for x in r["graphs"]], [g1])
        self.assertEqual(r["graphs"][0]["parts"], ["", "Diffusion", " models"])
        self.assertEqual([x["id"] for x in h.ok("GET", "/api/library?q=hands+robot")["graphs"]], [g2])
        self.assertEqual(h.ok("GET", "/api/library?q=zebra")["graphs"], [])
        self.assertNotIn("graphs", h.ok("GET", "/api/library"))


class TestMigration(unittest.TestCase):
    def test_migration_7_subscribes_the_makers_and_nobody_else(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = C.Config(data=Path(d))
            db.init(cfg)
            try:
                db.migrate()
                c = db.conn()
                # a database as version 6 left it: the graph list's tables and columns not there yet
                for t in ("graph_subs", "graph_seen", "graph_tag_joins", "ui_state"):
                    c.execute(f"DROP TABLE {t}")
                c.execute("ALTER TABLE graphs DROP COLUMN updated_at")
                c.execute("ALTER TABLE graph_members DROP COLUMN at")
                c.execute("UPDATE meta SET value = '6' WHERE key = 'schema_version'")
                c.execute("INSERT INTO users(email, name, role, created_at) VALUES ('m@example.org', 'Maker', 'viewer', '2026-09-01T00:00:00Z')")
                uid = c.execute("SELECT id FROM users").fetchone()[0]
                for gid, by, gone in (("g_seed000001", None, None), ("g_made000001", uid, None), ("g_gone000001", uid, "2026-09-03T00:00:00Z")):
                    c.execute("INSERT INTO graphs(id, name, rule_tags, locked, created_by, created_at, deleted_at) VALUES (?, ?, '[]', 0, ?, ?, ?)",
                              (gid, gid, by, "2026-09-02T00:00:00Z", gone))
                with mock.patch.object(db, "now", lambda: "2026-10-04T12:00:00Z"):
                    self.assertEqual(db.migrate(), db.SCHEMA_VERSION)
                subs = c.execute("SELECT user_id, graph_id, at FROM graph_subs").fetchall()
                # as of the migration, not the graph's making: what is in it already is not "new"
                self.assertEqual([tuple(r) for r in subs], [(uid, "g_made000001", "2026-10-04T12:00:00Z")])
                cols = {r[1] for r in c.execute("PRAGMA table_info(graphs)")} | {r[1] for r in c.execute("PRAGMA table_info(graph_members)")}
                self.assertTrue({"updated_at", "at"} <= cols)
                self.assertEqual(c.execute("SELECT updated_at FROM graphs WHERE id = 'g_made000001'").fetchone()[0], None)
                # again: nothing changes
                self.assertEqual(db.migrate(), db.SCHEMA_VERSION)
                self.assertEqual(c.execute("SELECT count(*) FROM graph_subs").fetchone()[0], 1)
            finally:
                db.close()


if __name__ == "__main__":
    unittest.main()
