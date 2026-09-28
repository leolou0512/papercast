"""A5: settled positions (SPEC.md section 8, Layout): the port of ground_state.py, stability, the
background thread, the admin relayout and the nightly command.
Run: python3 -m unittest discover -s stacks/papercast-group/hub/tests"""
import math
import os
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from hub.tests.graph_testlib import Hub, db, graph, layout  # noqa: E402
from hub import events  # noqa: E402

try:
    import numpy as np
except ImportError:          # the hub runs without numpy (provisional positions); these tests need it
    np = None


def lineage_like(h, n, tags, seed=1, year0=2000):
    """n papers in time order, each building on up to 3 of the 30 before it (a citation graph's shape)."""
    import random
    rnd = random.Random(seed)
    ids = [h.paper(f"Synthetic paper number {k}", year0 + k // 8, tags) for k in range(n)]
    pairs = set()
    for k in range(1, n):
        for j in rnd.sample(range(max(0, k - 30), k), min(k, rnd.randint(0, 3))):
            pairs.add((ids[j], ids[k]))
    at = db.now()
    db.conn().executemany("INSERT INTO links(src, dst, grade, origin, state, created_by, created_at, updated_at) "
                          "VALUES (?, ?, 's', 'agent', 'active', NULL, ?, ?)", [(a, b, at, at) for a, b in pairs])
    return ids, pairs


def stored(gid):
    return {r[0]: (r[1], r[2]) for r in db.conn().execute("SELECT paper_id, x, y FROM layout WHERE graph_id = ?", (gid,))}


@unittest.skipIf(np is None, "numpy is not installed")
class TestPhysics(unittest.TestCase):
    def test_same_defaults_as_map_js(self):
        self.assertEqual(layout.PARAMS, dict(center=0.4, repel=10, link=0.7, dist=70, node=1.0))
        self.assertEqual((layout.FULL_STARTS, layout.WARM_TICKS), (8, 400))

    def test_full_layout_of_tiny_graphs(self):
        """One or two papers, with or without a link: no eigen-start with fewer vectors than
        coordinates (the nightly --all crashed on perov's two-paper graph, 2026-09-28)."""
        rng = np.random.default_rng(1)
        for n, pairs in ((1, []), (2, []), (2, [(0, 1)]), (3, [(0, 1), (1, 2)])):
            s = np.array([a for a, _ in pairs], dtype=int)
            t = np.array([b for _, b in pairs], dtype=int)
            deg = np.maximum(np.bincount(np.r_[s, t], minlength=n).astype(float), 1)
            pos, kept, runs = layout._full(list(range(n)), s, t, deg, {}, rng, time.monotonic() + 60, 0.0)
            self.assertEqual(pos.shape, (n, 2))
            self.assertTrue(np.isfinite(pos).all())

    def test_a_chain_comes_to_rest(self):
        n = 12
        s, t = np.arange(n - 1), np.arange(1, n)
        deg = np.maximum(np.bincount(np.r_[s, t], minlength=n).astype(float), 1)
        p0 = np.random.default_rng(0).normal(size=(n, 2)) * 100
        pos, done = layout.simulate(p0, s, t, deg, 2500, 1.0)
        self.assertTrue(done and np.isfinite(pos).all())
        lengths = np.sqrt(((pos[s] - pos[t]) ** 2).sum(1))
        self.assertTrue(((lengths > 50) & (lengths < 200)).all(), lengths)      # springs near their rest length
        again, _ = layout.simulate(pos, s, t, deg, 400, layout.WARM_ALPHA)
        again = layout._align(again, list(range(n)), {i: tuple(pos[i]) for i in range(n)})
        self.assertLess(np.abs(again - pos).max(), 2.0)                         # at rest: it stays

    def test_the_deadline_stops_a_run(self):
        n = 50
        p0 = np.random.default_rng(0).normal(size=(n, 2))
        _, done = layout.simulate(p0, np.array([0]), np.array([1]), np.ones(n), 10 ** 6, 1.0,
                                  deadline=time.monotonic() + 0.2)
        self.assertFalse(done)

    def test_full_keeps_the_shortest(self):
        rng = np.random.default_rng(2)
        n = 30
        s = np.array([k // 2 for k in range(1, n)])
        t = np.arange(1, n)
        deg = np.maximum(np.bincount(np.r_[s, t], minlength=n).astype(float), 1)
        ids = [f"p{k}" for k in range(n)]
        pos, kept, tried = layout._full(ids, s, t, deg, {}, rng, time.monotonic() + 120, 0.0)
        self.assertEqual(len(tried), 8)
        self.assertEqual([x[0] for x in tried][:2], ["graph distances", "spectral"])
        best = min(tried, key=lambda x: x[1])
        self.assertEqual(kept, best[0])
        self.assertAlmostEqual(layout.total_length(pos, s, t), best[1], delta=1.0)


@unittest.skipIf(np is None, "numpy is not installed")
class TestStored(unittest.TestCase):
    def setUp(self):
        self.h = Hub()

    def tearDown(self):
        self.h.close()

    def test_unchanged_graph_does_not_move(self):
        ids, _ = lineage_like(self.h, 40, ["lay"])
        gid = self.h.ok("POST", "/api/graphs", {"name": "L", "tags": ["lay"]}, code=201)["graph"]["id"]
        st = layout.run(gid, "full")
        self.assertEqual((st["mode"], st["n"], st["rev"]), ("full", 40, 1))
        p1 = stored(gid)
        self.assertEqual(set(p1), set(ids))
        st2 = layout.run(gid, "warm")
        self.assertEqual((st2["mode"], st2["rev"]), ("unchanged", 1))
        self.assertEqual(stored(gid), p1)
        v = self.h.ok("GET", f"/api/graphs/{gid}")
        self.assertTrue(v["layout"]["current"])
        self.assertEqual({n["id"]: (n["x"], n["y"]) for n in v["nodes"]}, p1)
        self.assertTrue(all(n["placed"] for n in v["nodes"]))
        # a change that does not touch the shape (a regrade, a label) does not move it either
        lk = db.conn().execute("SELECT id FROM links LIMIT 1").fetchone()[0]
        self.h.ok("PUT", f"/api/links/{lk}", {"grade": "e"})
        self.h.ok("PUT", f"/api/papers/{ids[0]}/label", {"label": "First"})
        self.assertEqual(layout.run(gid, "warm")["mode"], "unchanged")
        self.assertEqual(stored(gid), p1)

    def test_one_new_paper_moves_the_rest_little(self):
        ids, _ = lineage_like(self.h, 60, ["lay"])
        gid = self.h.ok("POST", "/api/graphs", {"name": "L", "tags": ["lay"]}, code=201)["graph"]["id"]
        layout.run(gid, "full")
        p1 = stored(gid)
        new = self.h.paper("A new paper arrives", 2010, ["lay"])
        graph.apply_agent_links("e_new", new, self.h.alice, [
            {"other": {"paper_id": ids[20]}, "direction": "builds_on", "grade": "s"},
            {"other": {"paper_id": ids[25]}, "direction": "builds_on", "grade": "s"}])
        v = self.h.ok("GET", f"/api/graphs/{gid}")
        self.assertFalse(v["layout"]["current"])
        prov = {n["id"]: n for n in v["nodes"]}[new]
        self.assertFalse(prov["placed"])                     # provisional: near its neighbours until the layout runs
        mid = ((p1[ids[20]][0] + p1[ids[25]][0]) / 2, (p1[ids[20]][1] + p1[ids[25]][1]) / 2)
        self.assertLess(math.dist((prov["x"], prov["y"]), mid), 20)
        st = layout.run(gid, "warm")
        self.assertEqual((st["mode"], st["n"]), ("warm", 61))
        p2 = stored(gid)
        moves = sorted(math.dist(p1[i], p2[i]) for i in ids)
        median, p90 = moves[len(moves) // 2], moves[int(len(moves) * 0.9)]
        self.assertLess(median, 7.0, moves)                  # a tenth of a rest length
        self.assertLess(p90, 20.0, moves)
        near = min(math.dist(p2[new], p2[ids[20]]), math.dist(p2[new], p2[ids[25]]))
        self.assertLess(near, 3 * layout.PARAMS["dist"])
        self.assertTrue(self.h.ok("GET", f"/api/graphs/{gid}")["layout"]["current"])

    def test_nightly_keeps_todays_layout_unless_clearly_shorter(self):
        lineage_like(self.h, 30, ["lay"])
        gid = self.h.ok("POST", "/api/graphs", {"name": "L", "tags": ["lay"]}, code=201)["graph"]["id"]
        layout.run(gid, "full")
        p1 = stored(gid)
        st = layout.run(gid, "full", keep_margin=1.0)        # nothing can be 100% shorter
        self.assertEqual(st["kept"], "current")
        self.assertEqual(st["tried"][0][0], "current")
        p2 = stored(gid)
        self.assertLess(max(math.dist(p1[i], p2[i]) for i in p1), 10.0)

    def test_empty_and_single(self):
        gid = self.h.ok("POST", "/api/graphs", {"name": "E", "tags": ["none-here"]}, code=201)["graph"]["id"]
        self.assertEqual(layout.run(gid, "warm")["n"], 0)
        p = self.h.paper("Only paper here", 2020, ["none-here"])
        self.assertEqual(layout.run(gid, "warm")["n"], 1)
        self.assertEqual(stored(gid), {p: (0.0, 0.0)})
        self.assertIsNone(layout.run("g_nosuchgraph", "warm"))


@unittest.skipIf(np is None, "numpy is not installed")
class TestBackground(unittest.TestCase):
    def setUp(self):
        self._deb = layout.DEBOUNCE_S
        layout.DEBOUNCE_S = 0.6
        self.h = Hub(auto_layout=True)

    def tearDown(self):
        self.h.close()
        layout.DEBOUNCE_S = self._deb

    def rev(self, gid):
        r = db.conn().execute("SELECT rev FROM layout_state WHERE graph_id = ?", (gid,)).fetchone()
        return r[0] if r else 0

    def test_changes_are_debounced_into_one_run(self):
        h = self.h
        ids, _ = lineage_like(h, 25, ["bg"])
        gid = h.ok("POST", "/api/graphs", {"name": "B", "tags": ["bg"]}, code=201)["graph"]["id"]
        self.assertTrue(layout.wait_idle(60))
        r0 = self.rev(gid)
        self.assertGreaterEqual(r0, 1)                       # a new graph is laid out soon after it is made
        self.assertTrue(h.ok("GET", f"/api/graphs/{gid}")["layout"]["current"])
        sub = events.subscribe(None)
        try:
            extra = [h.paper(f"Extra paper {k}", 2010, []) for k in range(3)]
            for p in extra:                                  # three changes in quick succession
                h.ok("POST", f"/api/graphs/{gid}/papers", {"paper_id": p})
                time.sleep(0.1)
            self.assertTrue(layout.wait_idle(60))
            self.assertEqual(self.rev(gid), r0 + 1)
            got = []
            while not sub.q.empty():
                got.append(sub.q.get_nowait())
        finally:
            events.unsubscribe(sub)
        lay = [d for _, k, d in got if k == "graph" and d.get("change") == "layout" and d["id"] == gid]
        self.assertEqual(len(lay), 1)
        self.assertEqual(set(stored(gid)), set(ids) | set(extra))

    def test_admin_relayout(self):
        h = self.h
        lineage_like(h, 20, ["rl"])
        gid = h.ok("POST", "/api/graphs", {"name": "R", "tags": ["rl"]}, code=201)["graph"]["id"]
        self.assertTrue(layout.wait_idle(60))
        r0 = self.rev(gid)
        self.assertEqual(h.req("POST", f"/api/graphs/{gid}/relayout", {}, who="alice")[0], 403)
        st, js = h.req("POST", f"/api/graphs/{gid}/relayout", {}, who="root")
        self.assertEqual((st, js["queued"]), (202, True))
        self.assertEqual(h.req("POST", "/api/graphs/g_nosuchgraph/relayout", {}, who="root")[0], 404)
        self.assertTrue(layout.wait_idle(60))
        self.assertEqual(self.rev(gid), r0 + 1)
        row = db.conn().execute("SELECT mode, kept FROM layout_state WHERE graph_id = ?", (gid,)).fetchone()
        self.assertEqual(row["mode"], "full")


@unittest.skipIf(np is None, "numpy is not installed")
class TestNightly(unittest.TestCase):
    def setUp(self):
        self.h = Hub()

    def tearDown(self):
        self.h.close()

    def test_all(self):
        lineage_like(self.h, 20, ["diffusion"])
        old = os.environ.get("PCG_DATA")
        os.environ["PCG_DATA"] = str(self.h.tmp)
        try:
            self.assertEqual(layout.main(["--all", "--budget", "60"]), 0)
        finally:
            layout.AUTO = False
            if old is None:
                os.environ.pop("PCG_DATA", None)
            else:
                os.environ["PCG_DATA"] = old
        rows = db.conn().execute("SELECT graph_id, mode, n FROM layout_state").fetchall()
        self.assertEqual(len(rows), len(graph.SEED_GRAPHS))
        self.assertEqual({r["n"] for r in rows if r["n"]}, {20})
        self.assertTrue(all(r["mode"] == "full" for r in rows))


if __name__ == "__main__":
    unittest.main()
