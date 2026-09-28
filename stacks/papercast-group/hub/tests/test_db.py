"""hub/db.py: migrations (from an empty file and from version 1), seeds, paper identity."""
from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

GROUP = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(GROUP))

from hub import config as C  # noqa: E402
from hub import db  # noqa: E402

ITEST_LINEAGE = Path("/home/leo/papercast-itest/lineage/lineage.json")


def shape(path: Path) -> dict:
    """The schema as SQLite sees it: every table's columns and every index's definition."""
    c = sqlite3.connect(str(path))
    out = {}
    for name, typ, sql in c.execute("SELECT name, type, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY name"):
        if typ == "table":
            out[name] = [tuple(r[1:]) for r in c.execute(f"PRAGMA table_info({name})")]
        else:
            out[name] = sql
    c.close()
    return out


def make_v1(path: Path) -> None:
    """A database as the first hub made it: SCHEMA and schema_version 1, with some rows."""
    c = sqlite3.connect(str(path))
    c.executescript(db.SCHEMA)
    c.execute("INSERT INTO meta(key, value) VALUES ('schema_version', '1')")
    c.execute("INSERT INTO users(email, name, role, created_at) VALUES ('a@example.org', 'Ann', 'admin', '2026-09-28T00:00:00Z')")
    c.execute("INSERT INTO papers(id, title, title_norm, created_by, created_at) VALUES ('p_aaaaaaaaaaaa', 'A Paper', 'a paper', 1, '2026-09-28T00:00:00Z')")
    c.commit()
    c.close()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.cfg = C.Config(data=self.dir)
        db.init(self.cfg)

    def tearDown(self):
        db.close()
        self.tmp.cleanup()


class TestMigrate(Base):
    def test_empty_db_reaches_latest(self):
        self.assertEqual(db.migrate(), db.SCHEMA_VERSION)
        self.assertEqual(db.schema_version(), db.SCHEMA_VERSION)
        self.assertIn("label", db._columns(db.conn(), "papers"))
        idx = {r[0] for r in db.conn().execute("SELECT name FROM sqlite_master WHERE type = 'index'")}
        self.assertTrue({"links_dst", "episodes_made_by", "papers_arxiv"} <= idx)
        self.assertEqual(db.conn().execute("PRAGMA journal_mode").fetchone()[0], "wal")
        self.assertEqual(db.conn().execute("PRAGMA foreign_keys").fetchone()[0], 1)

    def test_idempotent(self):
        db.migrate()
        first = shape(self.dir / "hub.db")
        self.assertEqual(db.migrate(), db.SCHEMA_VERSION)
        self.assertEqual(shape(self.dir / "hub.db"), first)
        n = db.conn().execute("SELECT COUNT(*) FROM meta WHERE key = 'schema_version'").fetchone()[0]
        self.assertEqual(n, 1)

    def test_from_version_1(self):
        db.close()
        make_v1(self.dir / "hub.db")
        db.init(self.cfg)
        self.assertEqual(db.schema_version(), 1)
        self.assertEqual(db.migrate(), db.SCHEMA_VERSION)
        p = db.conn().execute("SELECT * FROM papers").fetchone()
        self.assertEqual((p["id"], p["title"], p["label"]), ("p_aaaaaaaaaaaa", "A Paper", None))
        self.assertEqual(db.conn().execute("SELECT name FROM users").fetchone()[0], "Ann")
        # the same shape as a database made new
        other = tempfile.TemporaryDirectory()
        try:
            db.init(C.Config(data=Path(other.name)))
            db.migrate()
            self.assertEqual(shape(self.dir / "hub.db"), shape(Path(other.name) / "hub.db"))
        finally:
            db.close()
            other.cleanup()

    def test_each_step_once(self):
        calls = []
        steps = [(n, (lambda f, n=n: (lambda c: (calls.append(n), f(c))))(fn)) for n, fn in db.MIGRATIONS]
        with mock.patch.object(db, "MIGRATIONS", steps):
            db.migrate()
            db.migrate()
        self.assertEqual(calls, [n for n, _ in db.MIGRATIONS])

    def test_failed_step_rolls_back(self):
        def boom(c):
            c.execute("ALTER TABLE papers ADD COLUMN half_done TEXT")
            raise ValueError("step failed")
        with mock.patch.object(db, "MIGRATIONS", db.MIGRATIONS + [(db.SCHEMA_VERSION + 1, boom)]), \
                mock.patch.object(db, "SCHEMA_VERSION", db.SCHEMA_VERSION + 1):
            with self.assertRaises(ValueError):
                db.migrate()
        self.assertEqual(db.schema_version(), db.SCHEMA_VERSION)
        self.assertNotIn("half_done", db._columns(db.conn(), "papers"))

    def test_newer_database_refused(self):
        db.migrate()
        db.conn().execute("UPDATE meta SET value = ? WHERE key = 'schema_version'", (str(db.SCHEMA_VERSION + 1),))
        with self.assertRaises(RuntimeError):
            db.migrate()

    def test_threads_at_once(self):
        errors = []

        def go():
            try:
                db.migrate()
            except Exception as e:           # noqa: BLE001
                errors.append(e)
            finally:
                db.close()
        ts = [threading.Thread(target=go) for _ in range(4)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(db.schema_version(), db.SCHEMA_VERSION)

    def test_serve_still_migrates(self):
        from hub import app
        cfg = C.Config(data=self.dir / "served", port=0)
        srv = app.serve(cfg)
        try:
            self.assertEqual(db.schema_version(), db.SCHEMA_VERSION)
            self.assertEqual(db.conn().execute("SELECT COUNT(*) FROM graphs").fetchone()[0], 0)  # no seeds
        finally:
            srv.server_close()


class TestSeeds(Base):
    def files(self, guideline=True, wording=True):
        out = {}
        if guideline:
            p = self.dir / "base-guideline.md"
            p.write_text("# Base guideline\n\nThe listener hears the paper.\n", encoding="utf-8")
            out["guideline"] = p
        if wording:
            p = self.dir / "wording.json"
            p.write_text(json.dumps({"classes": [{"id": "x", "phrases": ["as you know"]}]}), encoding="utf-8")
            out["wording"] = p
        return out

    def test_seeds_once(self):
        with mock.patch.object(db, "seed_sources", return_value=self.files()):
            got = db.seed_defaults(self.cfg)
            again = db.seed_defaults(self.cfg)
        self.assertEqual(got["base_prompt"], 1)
        self.assertEqual(len(got["graphs"]), 5)
        self.assertEqual(again, {"base_prompt": None, "graphs": []})
        c = db.conn()
        bp = c.execute("SELECT * FROM base_prompts").fetchall()
        self.assertEqual(len(bp), 1)
        self.assertEqual(bp[0]["version"], 1)
        self.assertIn("The listener hears the paper.", bp[0]["guideline"])
        self.assertEqual(json.loads(bp[0]["wording"])["classes"][0]["phrases"], ["as you know"])
        gs = {r["id"]: r for r in c.execute("SELECT * FROM graphs")}
        self.assertEqual(set(gs), {db.seed_graph_id(k) for k, _, _ in db.SEED_GRAPHS})
        rl = gs[db.seed_graph_id("rl")]
        self.assertEqual(rl["name"], "Reinforcement learning")
        self.assertIn("policy gradient", json.loads(rl["rule_tags"]))

    def test_no_guideline_skips_then_seeds_later(self):
        with mock.patch.object(db, "seed_sources", return_value=self.files(guideline=False)):
            got = db.seed_defaults(self.cfg)
        self.assertIsNone(got["base_prompt"])
        self.assertEqual(db.conn().execute("SELECT COUNT(*) FROM base_prompts").fetchone()[0], 0)
        with mock.patch.object(db, "seed_sources", return_value=self.files()):
            got = db.seed_defaults(self.cfg)
        self.assertEqual(got, {"base_prompt": 1, "graphs": []})

    def test_guideline_without_wording(self):
        with mock.patch.object(db, "seed_sources", return_value=self.files(wording=False)):
            db.seed_defaults(self.cfg)
        self.assertEqual(db.conn().execute("SELECT wording FROM base_prompts").fetchone()[0], "{}")

    def test_deleted_seed_graph_stays_deleted(self):
        with mock.patch.object(db, "seed_sources", return_value={}):
            db.seed_defaults(self.cfg)
            db.conn().execute("DELETE FROM graphs WHERE id = ?", (db.seed_graph_id("lm"),))
            db.seed_defaults(self.cfg)
        ids = {r[0] for r in db.conn().execute("SELECT id FROM graphs")}
        self.assertNotIn(db.seed_graph_id("lm"), ids)
        self.assertEqual(len(ids), 4)

    def test_existing_base_prompt_left_alone(self):
        db.migrate()
        db.conn().execute("INSERT INTO base_prompts(version, guideline, wording, created_at) VALUES (1, 'mine', '{}', ?)", (db.now(),))
        with mock.patch.object(db, "seed_sources", return_value=self.files()):
            self.assertIsNone(db.seed_defaults(self.cfg)["base_prompt"])
        self.assertEqual(db.conn().execute("SELECT guideline FROM base_prompts").fetchone()[0], "mine")

    def test_seed_graph_ids(self):
        ids = [db.seed_graph_id(k) for k, _, _ in db.SEED_GRAPHS]
        self.assertEqual(len(set(ids)), 5)
        for i in ids:
            self.assertRegex(i, r"^g_[a-z2-7]{10}$")
        self.assertEqual(db.seed_graph_id("rl"), db.seed_graph_id("rl"))

    def test_real_sources_found(self):
        # wording.json is in the repo (A9's package); the guideline may not have landed yet
        src = db.seed_sources()
        self.assertIn("wording", src)
        self.assertTrue(json.loads(src["wording"].read_text())["classes"])

    @unittest.skipUnless(ITEST_LINEAGE.is_file(), "Leo's lineage build output not on this machine")
    def test_tags_match_leos_graphs(self):
        leo = {g["key"]: (g["name"], g["tags"]) for g in json.loads(ITEST_LINEAGE.read_text())["graphs"]}
        self.assertEqual({k: (n, t) for k, n, t in db.SEED_GRAPHS}, leo)

    def test_command_line(self):
        env = dict(os.environ, PYTHONPATH=str(GROUP))
        out_dir = self.dir / "cli"
        for _ in range(2):
            r = subprocess.run([sys.executable, "-m", "hub.db", "migrate", "--data", str(out_dir)],
                               cwd=str(GROUP), env=env, capture_output=True, text=True, timeout=60)
            self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(f"schema version {db.SCHEMA_VERSION} -> {db.SCHEMA_VERSION}", r.stdout)
        c = sqlite3.connect(str(out_dir / "hub.db"))
        self.assertEqual(c.execute("SELECT COUNT(*) FROM graphs").fetchone()[0], 5)
        c.close()
        r = subprocess.run([sys.executable, "-m", "hub.db", "version", "--data", str(out_dir)],
                           cwd=str(GROUP), env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.stdout.strip(), str(db.SCHEMA_VERSION))


class TestFindPaper(Base):
    def add(self, pid, title, **k):
        db.conn().execute(
            "INSERT INTO papers(id, title, title_norm, arxiv_id, doi, source_sha256, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (pid, title, db.norm_title(title), k.get("arxiv_id"), k.get("doi"), k.get("sha"), k.get("at", db.now())))

    def test_order_and_normalising(self):
        db.migrate()
        self.add("p_arxiv000000a", "Flow Matching", arxiv_id="2210.02747")
        self.add("p_doi00000000a", "Other", doi="10.1000/abc")
        self.add("p_sha00000000a", "Third", sha="ab" * 32)
        self.add("p_title0000000", "Score-Based Models: A Study")
        f = db.find_paper
        self.assertEqual(f({"arxiv_id": "2210.02747v3"})["id"], "p_arxiv000000a")
        self.assertEqual(f({"arxiv_id": "arXiv:2210.02747"})["id"], "p_arxiv000000a")
        self.assertEqual(f({"doi": "10.1000/ABC"})["id"], "p_doi00000000a")
        self.assertEqual(f({"source_sha256": "AB" * 32})["id"], "p_sha00000000a")
        self.assertEqual(f({"title": "score based models  a study!"})["id"], "p_title0000000")
        # the first key that matches decides, in the SPEC's order
        self.assertEqual(f({"arxiv_id": "2210.02747", "doi": "10.1000/abc"})["id"], "p_arxiv000000a")
        self.assertEqual(f({"arxiv_id": "9999.99999", "doi": "10.1000/abc"})["id"], "p_doi00000000a")
        self.assertIsNone(f({"arxiv_id": "9999.99999", "title": "nothing like it"}))
        self.assertIsNone(f({}))


if __name__ == "__main__":
    unittest.main()
