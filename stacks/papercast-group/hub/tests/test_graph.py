"""A5: links, graphs, the edit log and revert (SPEC.md section 8), over HTTP against a running hub.
Run: python3 -m unittest discover -s stacks/papercast-group/hub/tests"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from hub.tests.graph_testlib import Hub, db, graph  # noqa: E402
from hub import events  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self.h = Hub()

    def tearDown(self):
        self.h.close()

    def graph_with(self, tags, who="alice", name="G"):
        return self.h.ok("POST", "/api/graphs", {"name": name, "tags": tags}, who=who, code=201)["graph"]["id"]

    def view(self, gid, who="alice"):
        return self.h.ok("GET", f"/api/graphs/{gid}", who=who)

    def link(self, src, dst, grade="s", who="alice"):
        return self.h.ok("POST", "/api/links", {"src": src, "dst": dst, "grade": grade}, who=who, code=201)["link"]


class TestSeeds(Base):
    def test_leos_five_topics(self):
        gs = self.h.ok("GET", "/api/graphs")["graphs"]
        self.assertEqual([g["name"] for g in gs], [n for n, _ in graph.SEED_GRAPHS])
        self.assertIn("flow matching", gs[1]["tags"])
        self.assertTrue(all(g["can_edit"] and not g["can_delete"] for g in gs))   # seeds: an admin's to delete


class TestLinks(Base):
    def test_each_link_op_undo_and_redo(self):
        h = self.h
        a, b = h.paper("Alpha paper", 2017), h.paper("Beta paper", 2019)
        lk = self.link(a, b, "s")
        self.assertEqual((lk["src"], lk["dst"], lk["grade"], lk["origin"], lk["state"]), (a, b, "s", "human", "active"))
        e = h.ok("GET", "/api/graph-log")["log"][0]
        self.assertEqual((e["op"], e["before"], e["after"]["state"], e["kind"], e["actor"]), ("link.add", None, "active", "change", "human"))
        self.assertEqual(e["user"]["name"], "Alice")
        h.ok("PUT", f"/api/links/{lk['id']}", {"grade": "w"})
        h.ok("DELETE", f"/api/links/{lk['id']}")
        self.assertEqual(h.link_row(a, b)["state"], "removed")
        ops = [e["op"] for e in h.ok("GET", "/api/graph-log")["log"]]
        self.assertEqual(ops, ["link.remove", "link.grade", "link.add"])

        # undo walks back: the remove, then the regrade, then the add
        st, js = h.undo()
        self.assertEqual(st, 200, js)
        self.assertEqual(js["reverted"]["op"], "link.remove")
        self.assertEqual(js["log"]["kind"], "undo")
        self.assertEqual((h.link_row(a, b)["state"], h.link_row(a, b)["grade"]), ("active", "w"))
        st, js = h.undo()
        self.assertEqual((st, js["reverted"]["op"]), (200, "link.grade"))
        self.assertEqual(h.link_row(a, b)["grade"], "s")
        st, js = h.undo()
        self.assertEqual((st, js["reverted"]["op"]), (200, "link.add"))
        self.assertEqual(h.link_row(a, b)["state"], "removed")          # undoing an add leaves it removed
        self.assertIsNone(h.ok("GET", "/api/graph-log")["undo"]["mine"])   # nothing left to undo

        # redo re-applies the newest undo first: the add, the regrade, the remove
        st, js = h.undo(redo=True)
        self.assertEqual((st, js["log"]["kind"]), (200, "redo"))
        self.assertEqual((h.link_row(a, b)["state"], h.link_row(a, b)["grade"]), ("active", "s"))
        h.undo(redo=True)
        self.assertEqual(h.link_row(a, b)["grade"], "w")
        h.undo(redo=True)
        self.assertEqual(h.link_row(a, b)["state"], "removed")
        # and a redo can be undone
        st, js = h.undo()
        self.assertEqual((st, js["reverted"]["kind"]), (200, "redo"))
        self.assertEqual(h.link_row(a, b)["state"], "active")
        # every row names what it reverted, and the reverted row points back
        rows = db.conn().execute("SELECT id, revert_of, reverted_by FROM graph_log ORDER BY id").fetchall()
        for r in rows:
            if r["revert_of"]:
                self.assertEqual(db.conn().execute("SELECT reverted_by FROM graph_log WHERE id = ?",
                                                   (r["revert_of"],)).fetchone()[0], r["id"])

    def test_spec_literal_expect_is_accepted(self):
        """A page that shows the newest un-reverted row (SPEC's literal reading) may name an undo row:
        that redoes it."""
        h = self.h
        a, b = h.paper("Alpha paper", 2017), h.paper("Beta paper", 2019)
        self.link(a, b)
        h.undo()
        newest = h.ok("GET", "/api/graph-log")["log"][0]
        self.assertEqual(newest["kind"], "undo")
        st, js = h.req("POST", "/api/graph-log/revert", {"scope": "mine", "expect": newest["id"]})
        self.assertEqual(st, 200, js)
        self.assertEqual(h.link_row(a, b)["state"], "active")

    def test_conflict_changes_nothing(self):
        h = self.h
        a, b = h.paper("Alpha paper", 2017), h.paper("Beta paper", 2019)
        lk = self.link(a, b, "s", who="bob")
        h.ok("PUT", f"/api/links/{lk['id']}", {"grade": "w"}, who="alice")
        h.ok("PUT", f"/api/links/{lk['id']}", {"grade": "e"}, who="bob")
        n = h.log_count()
        st, js = h.undo(who="alice")               # Alice's regrade: the link is no longer w
        self.assertEqual(st, 409)
        self.assertEqual(js["error"], "conflict")
        self.assertEqual(js["detail"]["expected"]["grade"], "w")
        self.assertEqual(js["detail"]["current"]["grade"], "e")
        self.assertEqual(h.link_row(a, b)["grade"], "e")
        self.assertEqual(h.log_count(), n)
        self.assertIsNone(db.conn().execute("SELECT reverted_by FROM graph_log WHERE op = 'link.grade' AND user_id = ?",
                                            (h.alice,)).fetchone()[0])

    def test_expect_must_name_the_op(self):
        h = self.h
        a, b, c = h.paper("Alpha paper", 2017), h.paper("Beta paper", 2019), h.paper("Gamma paper", 2020)
        self.link(a, b)
        first = h.ok("GET", "/api/graph-log")["undo"]["mine"]["id"]
        self.link(b, c)                             # the page still shows the first one
        st, js = h.req("POST", "/api/graph-log/revert", {"scope": "mine", "expect": first})
        self.assertEqual((st, js["error"]), (409, "moved"))
        self.assertNotEqual(js["next"]["id"], first)
        self.assertEqual(h.link_row(a, b)["state"], "active")
        st, js = h.req("POST", "/api/graph-log/revert", {"scope": "mine"})
        self.assertEqual(st, 400)

    def test_scope_mine_vs_any(self):
        h = self.h
        a, b, c = h.paper("Alpha paper", 2017), h.paper("Beta paper", 2019), h.paper("Gamma paper", 2020)
        self.link(a, b, who="alice")
        self.link(b, c, who="bob")
        log = h.ok("GET", "/api/graph-log", who="alice")
        self.assertEqual(log["undo"]["mine"]["user"]["name"], "Alice")
        self.assertEqual(log["undo"]["any"]["user"]["name"], "Bob")
        self.assertEqual(log["undo"]["any"]["summary"], "Bob linked Beta paper → Gamma paper (strong)")
        st, js = h.undo("mine", who="alice")
        self.assertEqual(st, 200)
        self.assertEqual((h.link_row(a, b)["state"], h.link_row(b, c)["state"]), ("removed", "active"))
        st, js = h.undo("any", who="alice")         # Bob's link: Alice's own undo is not a change to undo
        self.assertEqual(st, 200)
        self.assertEqual(js["reverted"]["user"]["name"], "Bob")
        self.assertEqual(h.link_row(b, c)["state"], "removed")

    def test_only_the_newest_100(self):
        h = self.h
        p = h.paper("Some paper with a title", 2020)
        gid = graph.graph_ids()[0]
        h.ok("PUT", f"/api/graphs/{gid}", {"name": "Alice's name"}, who="alice")
        mine = h.ok("GET", "/api/graph-log")["undo"]["mine"]["id"]
        for k in range(99):
            h.ok("PUT", f"/api/papers/{p}/label", {"label": f"L{k}"}, who="bob")
        self.assertEqual(h.ok("GET", "/api/graph-log")["undo"]["mine"]["id"], mine)    # the 100th newest
        h.ok("PUT", f"/api/papers/{p}/label", {"label": "L99"}, who="bob")
        log = h.ok("GET", "/api/graph-log?limit=100")
        self.assertEqual(len(log["log"]), 100)
        self.assertIsNone(log["undo"]["mine"])
        st, js = h.req("POST", "/api/graph-log/revert", {"scope": "mine", "expect": mine})
        self.assertEqual((st, js["error"], js["next"]), (409, "moved", None))
        self.assertEqual(db.conn().execute("SELECT name FROM graphs WHERE id = ?", (gid,)).fetchone()[0], "Alice's name")
        self.assertEqual(len(h.ok("GET", "/api/graph-log?limit=500")["log"]), 101)
        older = h.ok("GET", f"/api/graph-log?limit=10&before={log['log'][-1]['id']}")["log"]
        self.assertEqual([e["id"] for e in older], [mine])

    def test_order_and_cycles(self):
        h = self.h
        a, b = h.paper("Alpha paper", 2015), h.paper("Beta paper", 2016)
        x, y = h.paper("Xray paper", 2016), h.paper("Yankee paper", 2016)
        self.assertEqual(h.req("POST", "/api/links", {"src": b, "dst": a, "grade": "s"})[1]["error"], "order")
        self.assertEqual(h.req("POST", "/api/links", {"src": a, "dst": a, "grade": "s"})[0], 400)
        self.assertEqual(h.req("POST", "/api/links", {"src": a, "dst": "p_nope", "grade": "s"})[0], 404)
        self.assertEqual(h.req("POST", "/api/links", {"src": a, "dst": b, "grade": "x"})[0], 400)
        lk = self.link(x, y, who="alice")
        self.assertEqual(h.req("POST", "/api/links", {"src": x, "dst": y, "grade": "w"})[1]["error"], "exists")
        self.assertEqual(h.req("POST", "/api/links", {"src": y, "dst": x, "grade": "w"})[1]["error"], "cycle")
        h.ok("DELETE", f"/api/links/{lk['id']}", who="alice")
        self.link(y, x, who="bob")
        st, js = h.undo(who="alice")                # bringing x -> y back would close a loop
        self.assertEqual((st, js["error"]), (409, "conflict"))
        self.assertIn("cycle", js["message"])
        self.assertEqual(h.link_row(x, y)["state"], "removed")
        self.assertEqual(h.req("PUT", f"/api/links/{lk['id']}", {"grade": "e"})[1]["error"], "removed")


class TestGraphs(Base):
    def test_each_graph_op_and_its_undo(self):
        h = self.h
        p1 = h.paper("First paper here", 2018, ["t"])
        p2 = h.paper("Second paper here", 2019, ["u"])
        gid = self.graph_with(["t"], name="Mine")
        self.assertEqual([n["id"] for n in self.view(gid)["nodes"]], [p1])
        h.ok("PUT", f"/api/graphs/{gid}", {"name": "Renamed"})
        h.ok("PUT", f"/api/graphs/{gid}", {"tags": ["t", "u"]})
        self.assertEqual({n["id"] for n in self.view(gid)["nodes"]}, {p1, p2})
        h.ok("DELETE", f"/api/graphs/{gid}/papers/{p1}")
        self.assertEqual({n["id"] for n in self.view(gid)["nodes"]}, {p2})
        h.ok("POST", f"/api/graphs/{gid}/papers", {"paper_id": p1})
        h.ok("DELETE", f"/api/graphs/{gid}")
        self.assertEqual(h.req("GET", f"/api/graphs/{gid}")[0], 404)
        ops = [e["op"] for e in h.ok("GET", "/api/graph-log")["log"]]
        self.assertEqual(ops, ["graph.delete", "graph.add_paper", "graph.remove_paper", "graph.set_tags",
                               "graph.rename", "graph.create"])

        def state():
            r = db.conn().execute("SELECT * FROM graphs WHERE id = ?", (gid,)).fetchone()
            how = db.conn().execute("SELECT how FROM graph_members WHERE graph_id = ? AND paper_id = ?", (gid, p1)).fetchone()
            return r["name"], db.loads(r["rule_tags"]), r["deleted_at"] is not None, how[0] if how else None

        self.assertEqual(state(), ("Renamed", ["t", "u"], True, "added"))
        for want in [("Renamed", ["t", "u"], False, "added"),     # undelete
                     ("Renamed", ["t", "u"], False, "removed"),   # undo the add
                     ("Renamed", ["t", "u"], False, None),        # undo the remove
                     ("Renamed", ["t"], False, None),             # undo the tags
                     ("Mine", ["t"], False, None),                # undo the rename
                     ("Mine", ["t"], True, None)]:                # undo the create: deleted
            st, js = h.undo()
            self.assertEqual(st, 200, js)
            self.assertEqual(state(), want)
        st, js = h.undo(redo=True)                                 # redo the create
        self.assertEqual(st, 200, js)
        self.assertEqual(self.view(gid)["graph"]["name"], "Mine")

    def test_membership_tag_rule_added_removed(self):
        h = self.h
        p1 = h.paper("Diffusion one here", 2020, ["diffusion"])
        p2 = h.paper("Diffusion two here", 2021, ["Diffusion ", "generative models"])
        p3 = h.paper("An rl paper here", 2019, ["reinforcement learning"])
        p4 = h.paper("Rejected diffusion", 2021, ["diffusion"])
        h.episode(p4, h.alice, "rejected")
        p5 = h.paper("Deleted diffusion", 2021, ["diffusion"])
        h.episode(p5, h.alice, "ready", deleted=True)
        p6 = h.paper("Voiced diffusion here", 2022, ["diffusion"])
        h.episode(p6, h.alice, "rejected")
        h.episode(p6, h.bob, "ready")
        gid = self.graph_with(["diffusion"])
        self.assertEqual({n["id"] for n in self.view(gid)["nodes"]}, {p1, p2, p6})
        self.assertEqual(h.ok("POST", f"/api/graphs/{gid}/papers", {"paper_id": p3})["member"], True)
        self.assertEqual(h.ok("DELETE", f"/api/graphs/{gid}/papers/{p1}")["member"], False)
        self.assertEqual({n["id"] for n in self.view(gid)["nodes"]}, {p2, p3, p6})
        r = h.ok("POST", f"/api/graphs/{gid}/papers", {"paper_id": p2})       # already in by its tags
        self.assertIsNone(r["log"])
        h.ok("PUT", f"/api/graphs/{gid}", {"tags": ["reinforcement learning"]})
        self.assertEqual({n["id"] for n in self.view(gid)["nodes"]}, {p3})
        h.ok("PUT", f"/api/graphs/{gid}", {"tags": ["diffusion", "reinforcement learning"]})
        self.assertEqual({n["id"] for n in self.view(gid)["nodes"]}, {p2, p3, p6})   # p1 stays removed
        # the graph shows only the links among its members
        l1 = self.link(p1, p2)
        l2 = self.link(p3, p2)
        v = self.view(gid)
        self.assertEqual([l["id"] for l in v["links"]], [l2["id"]])
        self.assertEqual(v["graph"]["n"], 3)
        self.assertEqual(v["graph"]["links"], 1)
        self.assertNotIn(l1["id"], [l["id"] for l in v["links"]])
        # a paper another part adds straight to the database shows up (the cache notices)
        p7 = h.paper("Arrived later here", 2023, ["diffusion"])
        self.assertIn(p7, {n["id"] for n in self.view(gid)["nodes"]})

    def test_locked_graph(self):
        h = self.h
        p1, p2 = h.paper("Locked one here", 2018, ["lk"]), h.paper("Locked two here", 2019, ["lk"])
        p3, q = h.paper("Locked three here", 2020, ["lk"]), h.paper("Outside paper here", 2021, ["other"])
        gid = self.graph_with(["lk"], who="alice")
        old = self.link(p1, p2, who="alice")
        self.assertEqual(h.req("PUT", f"/api/graphs/{gid}", {"locked": True}, who="alice")[0], 403)
        h.ok("PUT", f"/api/graphs/{gid}", {"locked": True}, who="root")
        self.assertEqual(h.ok("GET", "/api/graph-log")["log"][0]["op"], "graph.lock")
        v = self.view(gid, who="bob")
        self.assertEqual((v["graph"]["locked"], v["graph"]["can_edit"]), (True, False))
        for method, path, body in [("PUT", f"/api/graphs/{gid}", {"name": "x"}),
                                   ("PUT", f"/api/graphs/{gid}", {"tags": ["x"]}),
                                   ("POST", f"/api/graphs/{gid}/papers", {"paper_id": q}),
                                   ("DELETE", f"/api/graphs/{gid}/papers/{p1}", None),
                                   ("DELETE", f"/api/graphs/{gid}", None),
                                   ("PUT", f"/api/links/{old['id']}", {"grade": "e"}),
                                   ("DELETE", f"/api/links/{old['id']}", None),
                                   ("POST", "/api/links", {"src": p1, "dst": p3, "grade": "s"})]:
            st, js = h.req(method, path, body, who="alice")
            self.assertEqual((st, js["error"]), (403, "locked"), (method, path))
        self.link(p1, q, who="alice")                        # q is not in the locked graph
        h.undo(who="alice")                                  # fine: that link is outside it
        n = h.log_count()
        st, js = h.undo(who="alice")                         # her link inside the locked graph
        self.assertEqual((st, js["error"]), (403, "locked"))
        self.assertEqual(h.log_count(), n)
        # agents still add; an admin still edits
        out = graph.apply_agent_links("e_x", p3, h.bob, [{"other": {"paper_id": p2}, "direction": "builds_on", "grade": "s"}])
        self.assertEqual(out["added"], 1)
        h.ok("PUT", f"/api/graphs/{gid}", {"name": "Curated"}, who="root")
        h.ok("PUT", f"/api/links/{old['id']}", {"grade": "e"}, who="root")
        h.ok("PUT", f"/api/graphs/{gid}", {"locked": False}, who="root")
        h.ok("PUT", f"/api/graphs/{gid}", {"name": "Open again"}, who="alice")

    def test_delete_is_the_makers_or_an_admins(self):
        h = self.h
        gid = self.graph_with([], who="alice")
        self.assertEqual(h.req("DELETE", f"/api/graphs/{gid}", who="bob")[0], 403)
        seed = graph.graph_ids()[0]
        self.assertEqual(h.req("DELETE", f"/api/graphs/{seed}", who="alice")[0], 403)
        h.ok("DELETE", f"/api/graphs/{gid}", who="alice")
        h.ok("DELETE", f"/api/graphs/{seed}", who="root")
        self.assertEqual(h.req("PUT", f"/api/graphs/{gid}", {"name": "x"})[0], 404)
        self.assertEqual(h.req("POST", "/api/graphs", {"name": "  "})[0], 400)
        self.assertEqual(h.req("POST", "/api/graphs", {"name": "ok", "tags": "notalist"})[0], 400)

    def test_graph_answer(self):
        h = self.h
        T = ["lin"]
        g_ = h.paper("Gee: an old root", 2014, T)
        a = h.paper("Denoising Diffusion Probabilistic Models", 2015, T, arxiv_id="2006.11239")
        b = h.paper("Bee: builds on a", 2016, T)
        e = h.paper("Isolated paper on its own", 2016, T)
        c = h.paper("Sea: builds on a and g", 2017, T)
        d = h.paper("Dee: builds on b and c", 2018, T)
        f = h.paper("Eff: builds on d", 2019, T)
        for s, t in [(a, b), (a, c), (g_, c), (b, d), (c, d), (d, f)]:
            self.link(s, t)
        h.episode(a, h.alice)
        h.episode(a, h.bob, "speaking")
        db.conn().execute("INSERT INTO listened(user_id, paper_id, at) VALUES (?, ?, ?)", (h.alice, b, db.now()))
        gid = self.graph_with(T)
        v = self.view(gid)
        self.assertEqual(v["roots"], [g_, a])                      # e has no child: not a root
        self.assertEqual(v["start"], [g_, a])
        self.assertEqual(v["path"], [g_, a, b, e, c, d, f])
        self.assertEqual(v["descendants"], {a: 4, g_: 3, b: 2, c: 2, d: 1})
        nodes = {n["id"]: n for n in v["nodes"]}
        self.assertEqual({k: n["deg"] for k, n in nodes.items()}, {g_: 1, a: 2, b: 2, e: 0, c: 3, d: 3, f: 1})
        self.assertEqual(nodes[a]["label"], "DDPM")                # Leo's curated label, by arXiv id
        self.assertEqual(nodes[b]["label"], "Bee")                 # before the colon
        self.assertEqual(nodes[e]["label"], "Isolated paper")      # first words, no trailing "on"
        self.assertEqual(nodes[a]["made_by"], ["Alice", "Bob"])
        self.assertEqual((nodes[a]["ready"], nodes[b]["ready"]), (True, False))
        self.assertEqual((nodes[b]["listened"], nodes[a]["listened"]), (True, False))
        self.assertFalse(self.view(gid, who="bob")["nodes"][2]["listened"])
        for n in v["nodes"]:
            self.assertIsInstance(n["x"], float)
            self.assertEqual(n["title"], db.conn().execute("SELECT title FROM papers WHERE id = ?", (n["id"],)).fetchone()[0])
        self.assertEqual(len(v["links"]), 6)
        self.assertEqual(set(v["links"][0]), {"id", "src", "dst", "grade", "origin", "created_at", "by"})
        # labels: set, cleared, undone
        r = h.ok("PUT", f"/api/papers/{b}/label", {"label": "  My   B "})
        self.assertEqual(r["paper"], {"id": b, "label": "My B", "custom": True})
        self.assertEqual({n["id"]: n["label"] for n in self.view(gid)["nodes"]}[b], "My B")
        self.assertEqual(h.req("PUT", f"/api/papers/{b}/label", {"label": "x" * 41})[0], 400)
        self.assertEqual(h.req("PUT", "/api/papers/p_nope/label", {"label": "x"})[0], 404)
        h.undo()
        self.assertEqual({n["id"]: n["label"] for n in self.view(gid)["nodes"]}[b], "Bee")
        # the answer is cached until something changes
        self.assertIs(graph._view(gid), graph._view(gid))
        # the history filtered to one graph
        mine = h.ok("GET", f"/api/graph-log?graph={gid}")["log"]
        self.assertTrue(mine and all(e["op"] in ("link.add", "paper.label", "graph.create") for e in mine))
        self.assertEqual(len([e for e in mine if e["op"] == "link.add"]), 6)


class TestAgentLinks(Base):
    def test_resolution_directions_and_one_row_each(self):
        h = self.h
        up = h.paper("The uploaded paper itself", 2022, arxiv_id="2201.00001")
        ep = h.episode(up, h.alice, "waiting-for-gpu")
        k1 = h.paper("Attention is all you need", 2017, arxiv_id="1706.03762")
        k2 = h.paper("Some journal paper", 2019, doi="10.1000/abc")
        k3 = h.paper("A very long known title of a paper", 2020)
        k4 = h.paper("A later paper building on it", 2023)
        links = [
            {"other": {"arxiv_id": "arXiv:1706.03762v5"}, "direction": "builds_on", "grade": "e", "source": "s2"},
            {"other": {"doi": "https://doi.org/10.1000/ABC"}, "direction": "builds_on", "grade": "strong", "source": "s2"},
            {"other": {"title": "A Very Long Known Title, of a Paper!"}, "direction": "builds_on", "grade": "w", "source": "text"},
            {"other": {"paper_id": k4}, "direction": "built_on_by", "grade": "s", "source": "s2"},
            {"other": {"arxiv_id": "2301.99999"}, "direction": "built_on_by", "grade": "s", "source": "s2"},
            {"other": {"title": "short"}, "direction": "builds_on", "grade": "s"},
            {"other": {"paper_id": k1}, "direction": "sideways", "grade": "s"},
            {"other": {"paper_id": up}, "direction": "builds_on", "grade": "s"},
            {"other": {"paper_id": k4}, "direction": "builds_on", "grade": "s"},        # k4 is later: skipped
        ]
        out = graph.apply_agent_links(ep, up, h.alice, links)
        self.assertEqual((out["added"], out["pending"]), (4, 1))
        self.assertEqual(sorted(s["reason"] for s in out["skipped"]),
                         ["bad direction or grade", "order", "self", "unknown paper"])
        for other, src, dst, grade in [(k1, k1, up, "e"), (k2, k2, up, "s"), (k3, k3, up, "w"), (k4, up, k4, "s")]:
            r = h.link_row(src, dst)
            self.assertEqual((r["grade"], r["origin"], r["state"], r["created_by"]), (grade, "agent", "active", h.alice))
        rows = db.conn().execute("SELECT * FROM graph_log ORDER BY id").fetchall()
        rows = [r for r in rows if r["op"] == "link.add"]
        self.assertEqual(len(rows), 4)
        self.assertTrue(all(r["actor"] == "agent" and r["user_id"] == h.alice for r in rows))
        e = h.ok("GET", "/api/graph-log")["log"][0]
        self.assertTrue(e["summary"].startswith("the agent (Alice’s upload) linked"), e["summary"])
        # an agent's links are not the uploader's own edits
        self.assertIsNone(h.ok("GET", "/api/graph-log")["undo"]["mine"])
        # the pending one resolves when that paper's own upload comes in (the SPEC's call form)
        late = h.paper("The paper that came later", 2023, arxiv_id="2301.99999")
        ep2 = h.episode(late, h.bob, "waiting-for-gpu")
        epr = dict(db.conn().execute("SELECT * FROM episodes WHERE id = ?", (ep2,)).fetchone())
        out2 = graph.apply_agent_links(epr, [])
        self.assertEqual(out2["resolved"], 1)
        r = h.link_row(up, late)
        self.assertEqual((r["origin"], r["created_by"]), ("agent", h.alice))     # the first uploader's link
        last = db.conn().execute("SELECT * FROM graph_log ORDER BY id DESC LIMIT 1").fetchone()
        self.assertEqual((last["actor"], last["user_id"], last["op"]), ("agent", h.alice, "link.add"))
        q = db.conn().execute("SELECT * FROM pending_links").fetchone()
        self.assertEqual((q["outcome"], q["link_id"]), ("added", r["id"]))
        self.assertEqual(graph.apply_agent_links(epr, [])["resolved"], 0)       # once only

    def test_pending_resolves_on_a_sweep(self):
        h = self.h
        up = h.paper("The uploaded paper itself", 2022)
        out = graph.apply_agent_links("e_1", up, h.bob, [
            {"other": {"doi": "10.5555/Later"}, "direction": "builds_on", "grade": "s"},
            {"other": {"doi": "10.5555/later"}, "direction": "builds_on", "grade": "s"}])      # the same, once
        self.assertEqual(out["pending"], 1)
        imported = h.paper("Imported some other way", 2018, doi="10.5555/later")
        self.assertEqual(graph.resolve_pending(), 1)
        self.assertEqual(h.link_row(imported, up)["origin"], "agent")
        self.assertEqual(graph.resolve_pending(), 0)

    def test_agents_never_undo_people(self):
        h = self.h
        a, b = h.paper("Alpha paper", 2017), h.paper("Beta paper", 2019)
        c, d = h.paper("Gamma paper", 2018), h.paper("Delta paper", 2020)
        e, f = h.paper("Epsilon paper", 2018), h.paper("Zeta paper", 2021)
        agent = lambda src, dst, grade="s": graph.apply_agent_links(
            "e_x", dst, h.alice, [{"other": {"paper_id": src}, "direction": "builds_on", "grade": grade}])
        self.assertEqual(agent(a, b)["added"], 1)
        lab = h.link_row(a, b)["id"]
        h.ok("DELETE", f"/api/links/{lab}", who="bob")               # a person removes the agent's link
        n = h.log_count()
        out = agent(a, b, "e")                                       # a later upload proposes it again
        self.assertEqual((out["added"], out["skipped"][0]["reason"]), (0, "removed"))
        self.assertEqual(h.link_row(a, b)["state"], "removed")
        self.assertEqual(h.log_count(), n)
        self.link(c, d, "w", who="bob")                              # a person's link
        out = agent(c, d, "e")
        self.assertEqual((out["added"], out["skipped"][0]["reason"]), (0, "exists"))
        r = h.link_row(c, d)
        self.assertEqual((r["grade"], r["origin"], r["state"]), ("w", "human", "active"))
        agent(e, f)                                                  # undoing an agent's link is sticky too
        st, js = h.undo("any", who="bob")
        self.assertEqual((st, js["reverted"]["actor"]), (200, "agent"))
        self.assertEqual(agent(e, f)["added"], 0)
        self.assertEqual(h.link_row(e, f)["state"], "removed")
        self.link(a, b, "e", who="alice")                            # a person may bring it back
        r = h.link_row(a, b)
        self.assertEqual((r["state"], r["origin"], r["grade"], r["id"]), ("active", "human", "e", lab))


class TestEvents(Base):
    def test_graph_and_log_events(self):
        h = self.h
        a, b = h.paper("Alpha paper", 2017, ["ev"]), h.paper("Beta paper", 2019, ["ev"])
        gid = self.graph_with(["ev"])
        sub = events.subscribe(None)
        try:
            self.link(a, b)
            got = []
            while not sub.q.empty():
                got.append(sub.q.get_nowait())
        finally:
            events.unsubscribe(sub)
        kinds = [(k, d.get("op") or d.get("id")) for _, k, d in got]
        self.assertIn(("log", "link.add"), kinds)
        self.assertIn(("graph", gid), kinds)


class TestRevisions(Base):
    """Every graph has a revision, one up with every change touching it; an edit sent with the
    revision it was made on (base_rev) is refused with 409 stale when the graph moved on."""

    def setUp(self):
        super().setUp()
        h = self.h
        self.a, self.b, self.c = h.paper("Alpha paper", 2017, ["rv"]), h.paper("Beta paper", 2019, ["rv"]), h.paper("Gamma paper", 2020, ["rv"])
        self.gid = self.graph_with(["rv"])

    def rev(self, who="alice"):
        v = self.view(self.gid, who)
        self.assertEqual(v["rev"], v["graph"]["rev"])
        return v["rev"]

    def test_every_change_goes_up_one(self):
        h, a, b, c, gid = self.h, self.a, self.b, self.c, self.gid
        other = self.graph_with(["elsewhere"], name="Other")
        r0 = self.rev()
        self.assertEqual(self.rev(), r0, "a read changed the revision")
        steps = [("POST", "/api/links", {"src": a, "dst": b, "grade": "s"}),
                 ("PUT", None, {"grade": "e"}),
                 ("DELETE", None, None),
                 ("DELETE", f"/api/graphs/{gid}/papers/{c}", None),
                 ("POST", f"/api/graphs/{gid}/papers", {"paper_id": c}),
                 ("PUT", f"/api/graphs/{gid}", {"name": "Renamed"}),
                 ("PUT", f"/api/graphs/{gid}", {"tags": ["rv", "more"]}),
                 ("PUT", f"/api/papers/{a}/label", {"label": "Al"})]
        lid = None
        for k, (method, path, body) in enumerate(steps, 1):
            path = path or f"/api/links/{lid}"
            base = self.rev()
            body = dict(body or {}, base_rev=base, graph_id=gid)
            js = h.ok(method, path, body)
            if lid is None and "link" in js:
                lid = js["link"]["id"]
            self.assertEqual(js["revs"].get(gid), base + 1, (method, path, js))
            self.assertEqual(self.rev(), r0 + k, (method, path))
        self.assertNotIn(other, js["revs"])
        base = self.rev()
        h.ok("PUT", f"/api/graphs/{gid}", {"locked": True, "base_rev": base}, who="root")
        self.assertEqual(self.rev(), base + 1)
        st, js = h.undo(who="root")                                 # the lock undone
        self.assertEqual((st, js["revs"]), (200, {gid: base + 2}))
        st, js = h.undo(who="root", redo=True)                      # and redone
        self.assertEqual((st, js["revs"]), (200, {gid: base + 3}))
        h.ok("PUT", f"/api/graphs/{gid}", {"locked": False}, who="root")
        base = self.rev()
        js = h.ok("DELETE", f"/api/graphs/{gid}?base_rev={base}")
        self.assertEqual(js["revs"], {gid: base + 1})
        st, js = h.undo()                                           # back, one more
        self.assertEqual((st, js["revs"]), (200, {gid: base + 2}))
        self.assertEqual(self.rev(), base + 2)
        # no change, no new revision
        self.assertTrue(h.ok("PUT", f"/api/graphs/{gid}", {"name": "Renamed", "base_rev": 1})["already"])
        self.assertEqual(self.rev(), base + 2)
        self.assertEqual([g["rev"] for g in h.ok("GET", "/api/graphs")["graphs"] if g["id"] == gid], [base + 2])

    def test_a_stale_edit_is_refused_and_changes_nothing(self):
        h, a, b, c, gid = self.h, self.a, self.b, self.c, self.gid
        seen = self.rev()
        lk = self.link(a, b, "s", who="bob")                        # Bob, after Alice loaded the graph
        n, snap = h.log_count(), (h.link_row(a, b)["grade"], h.link_row(a, b)["state"])
        for method, path, body in [("POST", "/api/links", {"src": b, "dst": c, "grade": "w"}),
                                   ("PUT", f"/api/links/{lk['id']}", {"grade": "e"}),
                                   ("DELETE", f"/api/links/{lk['id']}", {}),
                                   ("DELETE", f"/api/graphs/{gid}/papers/{a}", {}),
                                   ("PUT", f"/api/graphs/{gid}", {"name": "Mine now"}),
                                   ("PUT", f"/api/papers/{c}/label", {"label": "G"}),
                                   ("DELETE", f"/api/graphs/{gid}", {})]:
            st, js = h.req(method, path, dict(body, base_rev=seen, graph_id=gid))
            self.assertEqual((st, js["error"]), (409, "stale"), (method, path, js))
            self.assertEqual((js["rev"], js["base_rev"], js["graph_id"], js["actor"]), (seen + 1, seen, gid, "human"))
            self.assertEqual(js["by"]["name"], "Bob")
        # the same through the query string (a DELETE without a body)
        st, js = h.req("DELETE", f"/api/links/{lk['id']}?base_rev={seen}&graph_id={gid}")
        self.assertEqual((st, js["error"]), (409, "stale"))
        self.assertEqual(h.log_count(), n)
        self.assertEqual((h.link_row(a, b)["grade"], h.link_row(a, b)["state"]), snap)
        self.assertEqual(self.view(gid)["graph"]["name"], "G")
        # with the revision it has now, the edit goes through
        js = h.ok("PUT", f"/api/links/{lk['id']}", {"grade": "e", "base_rev": self.rev(), "graph_id": gid})
        self.assertEqual(js["link"]["grade"], "e")
        # an edit someone else already made is done, whatever the revision says
        js = h.ok("PUT", f"/api/links/{lk['id']}", {"grade": "e", "base_rev": seen, "graph_id": gid})
        self.assertTrue(js["already"])
        st, js = h.req("POST", "/api/links", {"src": a, "dst": b, "grade": "e", "base_rev": seen, "graph_id": gid})
        self.assertEqual((st, js["already"], js["link"]["id"]), (200, True, lk["id"]))
        st, js = h.req("POST", "/api/links", {"src": a, "dst": b, "grade": "w", "base_rev": seen, "graph_id": gid})
        self.assertEqual((st, js["error"]), (409, "stale"))       # not the same link: the page must look again
        self.assertEqual(h.log_count(), n + 1)
        # a revision needs the graph it belongs to; a bad one is a 400
        self.assertEqual(h.req("POST", "/api/links", {"src": b, "dst": c, "base_rev": seen})[0], 400)
        self.assertEqual(h.req("POST", "/api/links", {"src": b, "dst": c, "base_rev": "x", "graph_id": gid})[0], 400)
        # a graph deleted meanwhile
        other = self.graph_with(["rv"], name="Other", who="bob")
        orev = self.view(other)["rev"]
        h.ok("DELETE", f"/api/graphs/{other}", who="bob")
        st, js = h.req("POST", "/api/links", {"src": b, "dst": c, "grade": "s", "base_rev": orev, "graph_id": other})
        self.assertEqual((st, js["error"], js["deleted"]), (409, "stale", True))

    def test_members_that_come_by_their_tags_count(self):
        h, gid = self.h, self.gid
        seen = self.rev()
        h.paper("Delta paper arrives", 2021, ["rv"])               # an upload: no edit here
        st, js = h.req("PUT", f"/api/graphs/{gid}", {"name": "N", "base_rev": seen})
        self.assertEqual((st, js["error"], js["actor"]), (409, "stale", "library"))
        v = self.view(gid)
        self.assertEqual((v["rev"], len(v["nodes"])), (seen + 1, 4))
        self.assertEqual(self.rev(), seen + 1)
        h.ok("PUT", f"/api/graphs/{gid}", {"name": "N", "base_rev": seen + 1})

    def test_undo_and_redo_check_the_graph_the_page_shows(self):
        h, a, b, c, gid = self.h, self.a, self.b, self.c, self.gid
        other = self.graph_with(["other"], name="Other")
        x, y = h.paper("Other one paper", 2018, ["other"]), h.paper("Other two paper", 2019, ["other"])
        self.link(x, y)                                             # Alice's last edit: in Other
        seen = self.rev()
        self.link(a, b, who="bob")                                  # Bob changes the graph Alice shows
        log = h.ok("GET", "/api/graph-log")
        e = log["undo"]["mine"]
        st, js = h.req("POST", "/api/graph-log/revert", {"scope": "mine", "expect": e["id"], "base_rev": seen, "graph_id": gid})
        self.assertEqual(st, 200, js)                               # her undo does not touch it: no check
        self.assertEqual(h.link_row(x, y)["state"], "removed")
        self.assertIn(other, js["revs"])
        e = h.ok("GET", "/api/graph-log")["undo"]["any"]             # Bob's link, in the graph she shows
        st, js = h.req("POST", "/api/graph-log/revert", {"scope": "any", "expect": e["id"], "base_rev": seen, "graph_id": gid})
        self.assertEqual((st, js["error"]), (409, "stale"))
        self.assertEqual(h.link_row(a, b)["state"], "active")
        st, js = h.req("POST", "/api/graph-log/revert", {"scope": "any", "expect": e["id"], "base_rev": self.rev(), "graph_id": gid})
        self.assertEqual((st, js["revs"]), (200, {gid: seen + 2}))
        r = h.ok("GET", "/api/graph-log")["redo"]["any"]
        st, js = h.req("POST", "/api/graph-log/revert", {"scope": "any", "expect": r["id"], "redo": True, "base_rev": seen, "graph_id": gid})
        self.assertEqual((st, js["error"]), (409, "stale"))
        st, js = h.req("POST", "/api/graph-log/revert", {"scope": "any", "expect": r["id"], "redo": True, "base_rev": seen + 2, "graph_id": gid})
        self.assertEqual((st, js["log"]["kind"]), (200, "redo"))
        self.assertEqual(h.link_row(a, b)["state"], "active")

    def test_a_new_change_leaves_nothing_to_redo(self):
        h, a, b, c = self.h, self.a, self.b, self.c
        self.link(a, b)
        h.undo()
        log = h.ok("GET", "/api/graph-log")
        self.assertEqual((log["redo"]["mine"]["kind"], log["redo"]["any"]["kind"]), ("undo", "undo"))
        self.link(b, c, who="bob")                                  # someone else's change: mine can still redo
        log = h.ok("GET", "/api/graph-log")
        self.assertIsNotNone(log["redo"]["mine"])
        self.assertIsNone(log["redo"]["any"])
        self.link(a, c)                                             # my own new change: nothing of mine to redo
        self.assertIsNone(h.ok("GET", "/api/graph-log")["redo"]["mine"])
        # a new change that was undone and redone since is still a new change
        h.undo()                                                    # a->c undone
        self.assertIsNotNone(h.ok("GET", "/api/graph-log")["redo"]["mine"])
        st, js = h.undo(redo=True)                                  # a->c back
        self.assertEqual(st, 200)
        self.assertIsNone(h.ok("GET", "/api/graph-log")["redo"]["mine"], "a->b's old undo is redoable after a->c came back")
        self.assertEqual(h.link_row(a, b)["state"], "removed")
        # undo, undo, then redo, redo walks forward again
        x, y, z = h.paper("Xi paper", 2021), h.paper("Ypsilon paper", 2022), h.paper("Zeta paper", 2023)
        self.link(x, y)
        self.link(y, z)
        h.undo()                                                    # y->z
        h.undo()                                                    # x->y
        st, js = h.undo(redo=True)                                  # x->y again
        self.assertEqual(h.link_row(x, y)["state"], "active")
        log = h.ok("GET", "/api/graph-log")
        self.assertEqual(log["redo"]["mine"]["target"], str(h.link_row(y, z)["id"]), "y->z cannot be redone")
        h.undo(redo=True)
        self.assertEqual(h.link_row(y, z)["state"], "active")

    def test_graph_events_carry_the_revision(self):
        h, a, b, gid = self.h, self.a, self.b, self.gid
        seen = self.rev()
        sub = events.subscribe(None)
        try:
            self.link(a, b, who="bob")
            got = []
            while not sub.q.empty():
                got.append(sub.q.get_nowait())
        finally:
            events.unsubscribe(sub)
        ev = [d for _, k, d in got if k == "graph" and d.get("id") == gid]
        self.assertEqual(len(ev), 1)
        self.assertEqual((ev[0]["graph_rev"], ev[0]["by"]["name"], ev[0]["log_op"], ev[0]["deleted"]), (seen + 1, "Bob", "link.add", False))
        v = self.view(gid)
        self.assertEqual((v["graph"]["changed"]["by"]["name"], v["graph"]["changed"]["actor"]), ("Bob", "human"))


class TestSuggestions(Base):
    """Links from uploads: automatic (as always) or suggest only; accept, dismiss, accept all."""

    def setUp(self):
        super().setUp()
        h = self.h
        self.a, self.b, self.c = h.paper("Alpha paper", 2017, ["sg"]), h.paper("Beta paper", 2019, ["sg"]), h.paper("Gamma paper", 2020, ["sg"])
        self.gid = self.graph_with(["sg"])

    def upload(self, paper, links, who=None):
        """The agent's links of an upload (contrib calls this after the checks)."""
        return graph.apply_agent_links("e_x", paper, who or self.h.bob,
                                       [{"other": {"paper_id": o}, "direction": d, "grade": g} for o, d, g in links])

    def mode(self, v, who="root"):
        return self.h.req("PUT", "/api/graph-settings", {"agent_links": v}, who=who)

    def test_the_setting_is_an_admins_and_automatic_by_default(self):
        h = self.h
        self.assertEqual(h.ok("GET", "/api/graph-settings", who="bob")["agent_links"], "auto")
        out = self.upload(self.b, [(self.a, "builds_on", "s")])
        self.assertEqual((out["added"], out["suggested"]), (1, 0))
        self.assertEqual(h.link_row(self.a, self.b)["origin"], "agent")
        for who in ("alice", "bob"):
            st, js = self.mode("suggest", who)
            self.assertEqual((st, js["error"]), (403, "forbidden"))
        self.assertEqual(self.mode("sometimes")[0], 400)
        st, js = self.mode("suggest")
        self.assertEqual((st, js["agent_links"], js["changed"]["by"]["name"]), (200, "suggest", "Root"))
        self.assertEqual(h.ok("GET", "/api/graph-settings", who="alice")["agent_links"], "suggest")

    def test_suggest_only_keeps_them_aside_until_someone_accepts(self):
        h, a, b, c, gid = self.h, self.a, self.b, self.c, self.gid
        self.mode("suggest")
        n = h.log_count()
        rev = self.view(gid)["rev"]
        out = self.upload(c, [(a, "builds_on", "e"), (b, "builds_on", "w")])
        self.assertEqual((out["added"], out["suggested"], out["log_ids"]), (0, 2, []))
        self.assertEqual(h.log_count(), n, "a suggestion went into the edit log")
        self.assertIsNone(h.link_row(a, c))
        v = self.view(gid)
        self.assertEqual(v["links"], [])
        self.assertEqual(v["rev"], rev, "a suggestion is not a change to the graph")
        self.assertEqual([(x["src"], x["dst"], x["grade"], x["by"]["name"]) for x in v["suggestions"]], [(a, c, "e", "Bob"), (b, c, "w", "Bob")])
        self.assertEqual(v["graph"]["suggestions"], 2)
        self.assertEqual(h.ok("GET", "/api/graph-settings")["suggestions"], 2)
        # the same links again: nothing new
        self.assertEqual(self.upload(c, [(a, "builds_on", "e")])["skipped"][0]["reason"], "suggested already")
        # accept: Alice's link.add (undo and redo work), the revision up one
        sa, sb = v["suggestions"]
        js = h.ok("POST", f"/api/link-suggestions/{sa['id']}/accept", {"base_rev": rev, "graph_id": gid})
        self.assertEqual((js["suggestion"]["state"], js["link"]["grade"], js["log"]["op"], js["log"]["user"]["name"], js["revs"]),
                         ("accepted", "e", "link.add", "Alice", {gid: rev + 1}))
        r = h.link_row(a, c)
        self.assertEqual((r["state"], r["origin"], r["created_by"]), ("active", "human", h.alice))
        v = self.view(gid)
        self.assertEqual(([l["src"] for l in v["links"]], [x["id"] for x in v["suggestions"]]), ([a], [sb["id"]]))
        st, js = h.undo()
        self.assertEqual((st, h.link_row(a, c)["state"]), (200, "removed"))
        self.assertNotIn(sa["id"], [x["id"] for x in self.view(gid)["suggestions"]], "an undone accept came back as a suggestion")
        st, js = h.undo(redo=True)
        self.assertEqual((st, h.link_row(a, c)["state"]), (200, "active"))
        # a stale accept is refused like any edit
        st, js = h.req("POST", f"/api/link-suggestions/{sb['id']}/accept", {"base_rev": rev, "graph_id": gid}, who="bob")
        self.assertEqual((st, js["error"]), (409, "stale"))
        self.assertIsNone(h.link_row(b, c))
        # dismiss: dropped for good, never suggested again for that pair
        js = h.ok("POST", f"/api/link-suggestions/{sb['id']}/dismiss", who="bob")
        self.assertEqual(js["suggestion"]["state"], "dismissed")
        self.assertEqual(self.view(gid)["suggestions"], [])
        self.assertEqual(self.upload(c, [(b, "builds_on", "s")])["skipped"][0]["reason"], "dismissed")
        self.assertEqual(h.req("POST", f"/api/link-suggestions/{sb['id']}/accept", {})[1]["error"], "dismissed")
        self.assertTrue(h.ok("POST", f"/api/link-suggestions/{sb['id']}/dismiss")["already"])
        self.assertEqual(h.req("POST", "/api/link-suggestions/999/accept", {})[0], 404)
        # accepting again changes nothing, not even who accepted it
        who = db.conn().execute("SELECT decided_by FROM link_suggestions WHERE id = ?", (sa["id"],)).fetchone()[0]
        self.assertTrue(h.ok("POST", f"/api/link-suggestions/{sa['id']}/accept", {}, who="bob")["already"])
        self.assertEqual(db.conn().execute("SELECT decided_by FROM link_suggestions WHERE id = ?", (sa["id"],)).fetchone()[0], who)

    def test_the_rules_as_for_links(self):
        h, a, b, c = self.h, self.a, self.b, self.c
        lk = self.link(a, b)
        h.ok("DELETE", f"/api/links/{lk['id']}")                    # a person removed a -> b
        self.link(b, c)
        self.mode("suggest")
        out = self.upload(a, [(b, "built_on_by", "s"),               # a -> b: removed by a person
                              (c, "builds_on", "s"),                 # c -> a: the later paper built on by an earlier one
                              (c, "built_on_by", "w")])              # a -> c: fine
        self.assertEqual(sorted(x["reason"] for x in out["skipped"]), ["order", "removed"])
        self.assertEqual(out["suggested"], 1)
        # loops: not suggested (same-year papers, so the order check lets them through), and an
        # accept that would make one since is refused
        p, q, r = h.paper("Pi paper", 2022, ["sg"]), h.paper("Qu paper", 2022, ["sg"]), h.paper("Rho paper", 2022, ["sg"])
        self.link(p, q)
        self.assertEqual(self.upload(p, [(q, "builds_on", "s")])["skipped"][0]["reason"], "cycle")     # q -> p
        self.assertEqual(self.upload(q, [(r, "built_on_by", "s")])["suggested"], 1)                   # q -> r
        sug = {(x["src"], x["dst"]): x["id"] for x in self.view(self.gid)["suggestions"]}
        self.link(r, p)                                              # r -> p: now q -> r would close a loop
        st, js = h.req("POST", f"/api/link-suggestions/{sug[(q, r)]}/accept", {})
        self.assertEqual((st, js["error"]), (409, "cycle"))
        self.assertIsNone(h.link_row(q, r))
        # a person removed the pair's link since: accepting the old suggestion does not bring it back
        self.assertEqual(self.upload(h.paper("Sigma paper", 2023, ["sg"]), [(r, "builds_on", "s")])["suggested"], 1)
        t = h.ok("GET", "/api/graphs/" + self.gid)["suggestions"][-1]
        lk2 = self.link(t["src"], t["dst"], "w", who="bob")
        h.ok("DELETE", f"/api/links/{lk2['id']}", who="bob")
        st, js = h.req("POST", f"/api/link-suggestions/{t['id']}/accept", {})
        self.assertEqual((st, js["error"]), (409, "removed"))
        self.assertEqual(h.link_row(t["src"], t["dst"])["state"], "removed")
        # the pair got linked by hand meanwhile: accepting is done already, no second row
        self.link(a, c, "w")
        js = h.ok("POST", f"/api/link-suggestions/{sug[(a, c)]}/accept", {})
        self.assertTrue(js["already"])
        self.assertEqual(db.conn().execute("SELECT count(*) FROM links WHERE src = ? AND dst = ?", (a, c)).fetchone()[0], 1)
        self.assertEqual(h.link_row(a, c)["grade"], "w")

    def test_pending_links_become_suggestions_and_accept_all(self):
        h, a, b, c, gid = self.h, self.a, self.b, self.c, self.gid
        self.mode("suggest")
        # an upload names a paper not here yet; it arrives later: a suggestion, not a link
        out = graph.apply_agent_links("e_y", c, h.bob, [{"other": {"arxiv_id": "2101.00001", "title": "A paper still to come"},
                                                         "direction": "builds_on", "grade": "s"}])
        self.assertEqual(out["pending"], 1)
        late = h.paper("A paper still to come", 2018, ["sg"], arxiv_id="2101.00001")
        self.assertEqual(graph.resolve_pending(late), 1)
        self.assertIsNone(h.link_row(late, c))
        self.upload(c, [(a, "builds_on", "e"), (b, "builds_on", "w")])
        self.assertEqual(len(self.view(gid)["suggestions"]), 3)
        # back to automatic: nothing added by itself; Accept all is an admin's
        self.mode("auto")
        self.assertEqual(len(self.view(gid)["suggestions"]), 3)
        self.assertEqual(h.req("POST", "/api/link-suggestions/accept-all", {}, who="alice")[0], 403)
        self.assertEqual(h.req("POST", "/api/link-suggestions/accept-all", {"graph_id": [gid]}, who="root")[0], 400)
        n = h.log_count()
        js = h.ok("POST", "/api/link-suggestions/accept-all", {"graph_id": gid}, who="root")
        self.assertEqual((js["accepted"], js["skipped"]), (3, []))
        self.assertEqual(h.log_count(), n + 3)
        self.assertEqual({(l["src"], l["dst"]) for l in self.view(gid)["links"]}, {(late, c), (a, c), (b, c)})
        self.assertEqual(self.view(gid)["suggestions"], [])
        self.assertEqual([e["user"]["name"] for e in h.ok("GET", "/api/graph-log")["log"][:3]], ["Root"] * 3)
        # automatic again: an upload adds links as before
        d = h.paper("Delta paper", 2021, ["sg"])
        self.assertEqual(self.upload(d, [(c, "builds_on", "s")])["added"], 1)

    def test_suggestions_and_accepts_reach_every_page(self):
        h, a, c, gid = self.h, self.a, self.c, self.gid
        self.mode("suggest")
        sub = events.subscribe(None)
        try:
            self.upload(c, [(a, "builds_on", "e")])
            sid = self.view(gid)["suggestions"][0]["id"]
            h.ok("POST", f"/api/link-suggestions/{sid}/accept", {})
            self.mode("auto")
            got = []
            while not sub.q.empty():
                got.append(sub.q.get_nowait())
        finally:
            events.unsubscribe(sub)
        kinds = [(k, d.get("change"), d.get("id")) for _, k, d in got if k == "graph"]
        self.assertIn(("graph", "suggestions", gid), kinds)
        self.assertIn(("graph", "edit", gid), kinds)                  # the accept, a link.add
        self.assertIn(("graph", "settings", None), kinds)
        self.assertIn("log", [k for _, k, _ in got])


if __name__ == "__main__":
    unittest.main()
