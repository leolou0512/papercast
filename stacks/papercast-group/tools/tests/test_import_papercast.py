"""tools/import_papercast.py against a made-up copy of Leo's state layout (no real data)."""
from __future__ import annotations

import hashlib
import io
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

GROUP = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(GROUP))

from hub import db  # noqa: E402
from tools import import_papercast as imp  # noqa: E402

MAKER = "leo@example.org"


def tree_hash(root: Path) -> str:
    """Names, sizes, mtimes and contents of everything under root: equal means untouched."""
    h = hashlib.sha256()
    for p in sorted(root.rglob("*")):
        st = p.lstat()
        h.update(f"{p.relative_to(root)} {st.st_size} {st.st_mtime_ns} {st.st_mode}\n".encode())
        if p.is_file():
            h.update(p.read_bytes())
    return h.hexdigest()


def leo_paper(state: Path, leo_id: str, title: str, *, arxiv=None, doi=None, tags=(), audio=None,
              voice_out=False, explainer_json=True, final=True, state_name="waiting-for-gpu", year=2020):
    d = state / leo_id
    (d / "voice").mkdir(parents=True)
    meta = {"title": title, "authors": ["A. Person", "B. Person"], "year": year, "arxiv_id": arxiv, "doi": doi,
            "tags": list(tags), "first_author": "A. Person"}
    size = len(audio) if audio else None
    pj = {"id": leo_id, "meta": meta, "state": state_name, "created_at": "2026-09-27T04:00:00Z",
          "state_since": "2026-09-27T05:00:00Z", "model": "claude-opus-5-5", "source_sha256_fetched": "ab" * 32,
          "final": {"paper.pdf": {"sha256": hashlib.sha256(leo_id.encode()).hexdigest(), "size": 3}}}
    (d / "paper.json").write_text(json.dumps(pj))
    if final:
        (d / "final").mkdir()
        (d / "final" / "script.md").write_text("# Title\n\n" + "word " * 3000 + "\n")
        (d / "final" / "explainer.html").write_text("<!doctype html><title>x</title>")
        (d / "final" / "claims.md").write_text("- a claim\n")
        (d / "final" / "meta.json").write_text(json.dumps(meta))
        (d / "final" / "paper.pdf").write_bytes(b"%PDF")
        (d / "final" / "concepts.md").write_text("vault notes")
    if explainer_json:
        (d / "cut").mkdir()
        (d / "cut" / "explainer.json").write_text(json.dumps({"points": ["p"], "figures": []}))
    status = {"phase": "waiting-for-gpu", "output": None}
    if audio is not None:
        status = {"phase": "done", "output": {"path": "out/episode.mp3", "duration_s": 1200.5, "size": size}}
        (d / "voice" / "out").mkdir()
        (d / "voice" / "out" / "episode.mp3").write_bytes(audio)
        if not voice_out:
            (d / "final" / "audio").mkdir()
            (d / "final" / "audio" / "episode.mp3").write_bytes(audio)
    (d / "voice" / "status.json").write_text(json.dumps(status))
    return d


class TestImport(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.state, self.data = root / "state", root / "hub"
        self.state.mkdir()
        leo_paper(self.state, "2026-09-27-aaaaaaaa", "Old Paper", arxiv="1111.00001v2", tags=["reinforcement learning"],
                  audio=b"\xff\xfb" + b"\0" * 999, state_name="ready", year=2015)
        leo_paper(self.state, "2026-09-27-bbbbbbbb", "Middle Paper", doi="10.1/ABC", tags=["diffusion", "language models"],
                  explainer_json=False, year=2019)
        leo_paper(self.state, "2026-09-27-cccccccc", "New Paper", arxiv="2222.00002", tags=["policy gradient"],
                  audio=b"\xff\xfb" + b"\1" * 499, voice_out=True, state_name="ready", year=2024)
        leo_paper(self.state, "2026-09-27-dddddddd", "Unfinished", final=False)
        (self.state / "admin").mkdir()
        (self.state / "events.jsonl").write_text("{}\n")
        A, B, C, D = "2026-09-27-aaaaaaaa", "2026-09-27-bbbbbbbb", "2026-09-27-cccccccc", "2026-09-27-dddddddd"
        lineage = {"papers": {A: {"title": "Old Paper, cleaned", "label": "Old", "year": 2014, "s2": "s2old"}},
                   "graphs": [
                       {"key": "rl", "name": "Reinforcement learning",
                        "nodes": [{"id": A, "x": 1.5, "y": -2.0}, {"id": C, "x": 3.0, "y": 4.0}, {"id": B, "x": 0, "y": 0}],
                        "edges": [[A, C, "e"], [A, B, "w"], [B, D, "s"]]},
                       {"key": "gen", "name": "Diffusion and generative models",
                        "nodes": [{"id": B, "x": 9.0, "y": 9.0}], "edges": [[A, C, "e"]]},
                       {"key": "zz", "name": "Not a seed", "nodes": [], "edges": []}]}
        self.lineage = root / "lineage.json"
        self.lineage.write_text(json.dumps(lineage))
        self.ids = (A, B, C, D)

    def tearDown(self):
        db.close()
        self.tmp.cleanup()

    def run_main(self, *args):
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()), mock.patch.dict(os.environ, {"PCG_ADMIN_EMAILS": ""}):
            rc = imp.main(["--state", str(self.state), "--lineage", str(self.lineage), *args])
        db.close()
        return rc, out.getvalue()

    def conn(self):
        c = sqlite3.connect(str(self.data / "hub.db"))
        c.row_factory = sqlite3.Row
        return c

    def test_dry_run_writes_nothing(self):
        before = tree_hash(self.state)
        rc, out = self.run_main("--data", str(self.data))
        self.assertEqual(rc, 0)
        self.assertIn("Dry run: nothing written", out)
        self.assertIn("3 papers with a finished episode", out)
        self.assertIn("with audio 2", out)
        self.assertIn("explainer.json 2", out)
        self.assertIn("2 to add", out)                        # A->C, A->B; B->D has an end not imported
        self.assertFalse(self.data.exists())
        self.assertEqual(tree_hash(self.state), before)

    def test_apply(self):
        before = tree_hash(self.state)
        rc, out = self.run_main("--data", str(self.data), "--maker", MAKER, "--apply")
        self.assertEqual(rc, 0, out)
        self.assertEqual(tree_hash(self.state), before)       # the sources are only read
        A, B, C, _ = self.ids
        c = self.conn()
        ps = {r["id"]: r for r in c.execute("SELECT * FROM papers")}
        self.assertEqual(len(ps), 3)
        pa = ps[imp.paper_id(A)]
        self.assertEqual((pa["title"], pa["label"], pa["year"], pa["arxiv_id"], pa["s2_id"]),
                         ("Old Paper, cleaned", "Old", 2014, "1111.00001", "s2old"))
        self.assertEqual(ps[imp.paper_id(B)]["doi"], "10.1/abc")
        u = c.execute("SELECT * FROM users").fetchall()
        self.assertEqual([(x["email"], x["name"], x["role"]) for x in u], [(MAKER, "Leo", "admin")])
        eps = {r["id"]: r for r in c.execute("SELECT * FROM episodes")}
        ea, eb, ec = (eps[imp.episode_id(x)] for x in (A, B, C))
        self.assertEqual((ea["state"], ea["duration_s"]), ("ready", 1200.5))
        self.assertEqual((eb["state"], eb["duration_s"]), ("waiting-for-gpu", None))
        self.assertEqual(ec["state"], "ready")                                    # from voice/out
        self.assertEqual(ea["words"], 3001)
        self.assertEqual(c.execute("SELECT COUNT(*) FROM voice_jobs").fetchone()[0], 0)
        da = self.data / "episodes" / ea["id"]
        self.assertEqual(sorted(p.name for p in da.iterdir()),
                         ["audio.mp3", "claims.md", "explainer.html", "explainer.json", "meta.json", "script.md"])
        self.assertFalse((self.data / "episodes" / eb["id"] / "explainer.json").exists())
        self.assertEqual(list(self.data.rglob("*.pdf")), [])
        links = {(r["src"], r["dst"]): r for r in c.execute("SELECT * FROM links")}
        self.assertEqual(set(links), {(imp.paper_id(A), imp.paper_id(C)), (imp.paper_id(A), imp.paper_id(B))})
        self.assertTrue(all(r["origin"] == "agent" and r["state"] == "active" for r in links.values()))
        self.assertEqual(links[(imp.paper_id(A), imp.paper_id(C))]["grade"], "e")
        # graphs: rl's rule gathers A and C; Leo's rl graph also has B (added); gen's rule has B only
        rl, gen = db.seed_graph_id("rl"), db.seed_graph_id("gen")
        mem = {(r["graph_id"], r["paper_id"], r["how"]) for r in c.execute("SELECT * FROM graph_members")}
        self.assertEqual(mem, {(rl, imp.paper_id(B), "added")})
        lay = {(r["graph_id"], r["paper_id"]): (r["x"], r["y"]) for r in c.execute("SELECT * FROM layout")}
        self.assertEqual(lay[(rl, imp.paper_id(A))], (1.5, -2.0))
        self.assertEqual(len(lay), 4)
        self.assertEqual(c.execute("SELECT COUNT(*) FROM graph_log").fetchone()[0], 0)

    def test_rerun_adds_only_what_is_new(self):
        self.run_main("--data", str(self.data), "--maker", MAKER, "--apply")
        A, B, C, _ = self.ids
        c = self.conn()
        # a person removes a link and edits a title; then the import runs again
        c.execute("UPDATE links SET state = 'removed' WHERE src = ? AND dst = ?", (imp.paper_id(A), imp.paper_id(C)))
        c.execute("UPDATE papers SET title = 'Edited' WHERE id = ?", (imp.paper_id(A),))
        c.commit()
        # Leo's runner voices B meanwhile
        d = self.state / B
        (d / "final" / "audio").mkdir()
        (d / "final" / "audio" / "episode.mp3").write_bytes(b"\xff\xfb" + b"\2" * 99)
        (d / "voice" / "status.json").write_text(json.dumps({"phase": "done", "output": {"duration_s": 900.0, "size": 101}}))
        rc, out = self.run_main("--data", str(self.data), "--maker", MAKER, "--apply")
        self.assertIn("1 imported before now have their MP3", out)
        c = self.conn()
        self.assertEqual(c.execute("SELECT COUNT(*) FROM papers").fetchone()[0], 3)
        self.assertEqual(c.execute("SELECT COUNT(*) FROM episodes").fetchone()[0], 3)
        self.assertEqual(c.execute("SELECT COUNT(*) FROM links").fetchone()[0], 2)
        self.assertEqual(c.execute("SELECT state FROM links WHERE src = ? AND dst = ?",
                                   (imp.paper_id(A), imp.paper_id(C))).fetchone()[0], "removed")
        self.assertEqual(c.execute("SELECT title FROM papers WHERE id = ?", (imp.paper_id(A),)).fetchone()[0], "Edited")
        eb = c.execute("SELECT * FROM episodes WHERE id = ?", (imp.episode_id(B),)).fetchone()
        self.assertEqual((eb["state"], eb["duration_s"]), ("ready", 900.0))
        self.assertTrue((self.data / "episodes" / eb["id"] / "audio.mp3").is_file())
        self.assertEqual(c.execute("SELECT COUNT(*) FROM users").fetchone()[0], 1)

    def test_paper_already_in_the_hub(self):
        from tools import _common
        _common.open_hub(self.data)
        c = db.conn()
        uid = c.execute("INSERT INTO users(email, name, role, created_at) VALUES ('bob@example.org', 'Bob', 'contributor', ?)",
                        (db.now(),)).lastrowid
        c.execute("INSERT INTO papers(id, title, title_norm, arxiv_id, created_by, created_at) VALUES "
                  "('p_bobsbobsbobs', 'Bob''s upload', 'bob s upload', '2222.00002', ?, ?)", (uid, db.now()))
        db.close()
        rc, out = self.run_main("--data", str(self.data), "--maker", MAKER)
        self.assertIn("2 new, 1 already in the hub", out)
        rc, out = self.run_main("--data", str(self.data), "--maker", MAKER, "--apply")
        c = self.conn()
        self.assertEqual(c.execute("SELECT COUNT(*) FROM papers").fetchone()[0], 3)
        e = c.execute("SELECT paper_id, made_by FROM episodes WHERE id = ?", (imp.episode_id(self.ids[2]),)).fetchone()
        self.assertEqual(e["paper_id"], "p_bobsbobsbobs")                 # a version of Bob's paper
        A = self.ids[0]
        self.assertIsNotNone(c.execute("SELECT 1 FROM links WHERE src = ? AND dst = 'p_bobsbobsbobs'", (imp.paper_id(A),)).fetchone())

    def test_queue_voice(self):
        self.run_main("--data", str(self.data), "--maker", MAKER, "--apply", "--queue-voice")
        c = self.conn()
        rows = c.execute("SELECT episode_id, state FROM voice_jobs").fetchall()
        self.assertEqual([(r[0], r[1]) for r in rows], [(imp.episode_id(self.ids[1]), "queued")])

    def test_apply_needs_maker(self):
        with self.assertRaises(SystemExit):
            self.run_main("--data", str(self.data), "--apply")
        self.assertFalse((self.data / "hub.db").exists())


if __name__ == "__main__":
    unittest.main()
