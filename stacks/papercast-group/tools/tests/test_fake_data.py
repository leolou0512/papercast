"""tools/fake_data.py: a consistent, obviously fake, deterministic library."""
from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

GROUP = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(GROUP))
sys.path.append(str(GROUP.parent.parent / "packages" / "papercast-cli"))

from hub import db  # noqa: E402
from tools import fake_data  # noqa: E402

LEO_FFMPEG = "/home/leo/papercast/voice/bin/ffmpeg"
FFMPEG = shutil.which("ffmpeg") or (LEO_FFMPEG if Path(LEO_FFMPEG).is_file() else None)
FILES = ("script.md", "explainer.json", "explainer.html", "claims.md", "meta.json", "bundle-manifest.json")


def dump(data: Path) -> dict:
    """Every table's rows and every file's sha256: what two runs must agree on."""
    c = sqlite3.connect(str(data / "hub.db"))
    out = {}
    for (t,) in c.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"):
        rows = [tuple(r) for r in c.execute(f"SELECT * FROM {t}")]
        out[t] = sorted(rows, key=repr)
    c.close()
    for p in sorted((data / "episodes").rglob("*")):
        if p.is_file():
            out[str(p.relative_to(data))] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        db.close()
        self.tmp.cleanup()

    def conn(self, data):
        c = sqlite3.connect(str(data / "hub.db"))
        c.row_factory = sqlite3.Row
        return c


class TestFakeData(Base):
    @classmethod
    def setUpClass(cls):
        cls.ctmp = tempfile.TemporaryDirectory()
        cls.data = Path(cls.ctmp.name) / "d"
        cls.counts = fake_data.generate(cls.data, papers=40, users=6, seed=3, audio="none")
        db.close()

    @classmethod
    def tearDownClass(cls):
        cls.ctmp.cleanup()

    def test_counts_and_integrity(self):
        n = self.counts
        self.assertEqual((n["papers"], n["users"]), (40, 6))
        self.assertGreater(n["episodes"], 40)
        self.assertGreater(n["links"], 20)
        c = self.conn(self.data)
        self.assertEqual(c.execute("PRAGMA foreign_key_check").fetchall(), [])
        self.assertEqual(c.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        roles = {r["role"] for r in c.execute("SELECT role FROM users")}
        self.assertEqual(roles, {"admin", "contributor", "viewer"})
        self.assertEqual(c.execute("SELECT COUNT(*) FROM users WHERE disabled = 1").fetchone()[0], 1)
        self.assertGreaterEqual(c.execute("SELECT COUNT(*) FROM graphs").fetchone()[0], 7)
        self.assertEqual(c.execute("SELECT COUNT(*) FROM base_prompts").fetchone()[0], 1)
        self.assertEqual(c.execute("SELECT COUNT(*) FROM prefs").fetchone()[0], 6)

    def test_obviously_fake(self):
        c = self.conn(self.data)
        for r in c.execute("SELECT email FROM users"):
            self.assertTrue(r["email"].endswith("@example.org"))
        for r in c.execute("SELECT arxiv_id, doi, authors FROM papers"):
            if r["arxiv_id"]:
                self.assertRegex(r["arxiv_id"], r"^\d\d99\.\d{5}$")          # month 99: no such arXiv id
            else:
                self.assertTrue(r["doi"].startswith("10.5555/fake."))
            for a in json.loads(r["authors"]):
                self.assertIn(a.split()[-1], fake_data.LAST)
        self.assertEqual(list(self.data.rglob("*.pdf")), [])

    def test_no_audio_means_waiting(self):
        c = self.conn(self.data)
        self.assertEqual(c.execute("SELECT COUNT(*) FROM episodes WHERE state = 'ready'").fetchone()[0], 0)
        self.assertEqual(list(self.data.rglob("audio.mp3")), [])
        self.assertEqual(c.execute("SELECT COUNT(*) FROM positions").fetchone()[0], 0)

    def test_files_valid(self):
        from papercast_cli.common import checks
        c = self.conn(self.data)
        for e in c.execute("SELECT id, words, state FROM episodes"):
            d = self.data / "episodes" / e["id"]
            for f in FILES:
                self.assertTrue((d / f).is_file(), f"{e['id']}/{f}")
            page = (d / "explainer.html").read_text()
            self.assertTrue(page.startswith("<!doctype html"))
            self.assertNotIn("<script", page)
            self.assertNotIn("src=\"http", page)
            self.assertEqual(checks.explainer_problems(json.loads((d / "explainer.json").read_text())), [])
            if e["state"] != "rejected":
                res = checks.check((d / "script.md").read_text(), 150, 15, 25)
                self.assertTrue(res["ok"], (e["id"], res["problems"]))
            m = json.loads((d / "bundle-manifest.json").read_text())
            self.assertEqual(m["manifest_version"], 1)

    def test_links_are_a_dag_older_to_newer(self):
        c = self.conn(self.data)
        year = {r["id"]: r["year"] for r in c.execute("SELECT id, year FROM papers")}
        edges = [(r["src"], r["dst"]) for r in c.execute("SELECT src, dst FROM links")]
        for s, d in edges:
            self.assertLess(year[s], year[d])
        # Kahn: every node leaves, so there is no cycle
        indeg = {p: 0 for p in year}
        out = {p: [] for p in year}
        for s, d in edges:
            indeg[d] += 1
            out[s].append(d)
        q = [p for p, k in indeg.items() if k == 0]
        seen = 0
        while q:
            p = q.pop()
            seen += 1
            for d in out[p]:
                indeg[d] -= 1
                if indeg[d] == 0:
                    q.append(d)
        self.assertEqual(seen, len(year))
        grades = {r[0] for r in c.execute("SELECT DISTINCT grade FROM links")}
        self.assertEqual(grades, {"e", "s", "w"})
        self.assertGreater(c.execute("SELECT COUNT(*) FROM links WHERE state = 'removed'").fetchone()[0], 0)
        self.assertGreater(c.execute("SELECT COUNT(*) FROM links WHERE origin = 'human'").fetchone()[0], 0)

    def test_log_matches_state(self):
        """The newest log row about each link says what the link is now."""
        c = self.conn(self.data)
        links = {str(r["id"]): r for r in c.execute("SELECT * FROM links")}
        last = {}
        for r in c.execute("SELECT * FROM graph_log WHERE op LIKE 'link.%' ORDER BY id"):
            last[r["target"]] = json.loads(r["after"])
        self.assertTrue(last)
        for t, after in last.items():
            self.assertEqual((after["grade"], after["state"]), (links[t]["grade"], links[t]["state"]))
        for r in c.execute("SELECT * FROM graph_log WHERE actor = 'agent'"):
            self.assertIsNotNone(r["user_id"])                                 # the uploader
        ops = {r[0] for r in c.execute("SELECT DISTINCT op FROM graph_log")}
        self.assertTrue({"link.add", "link.remove", "link.grade", "graph.create", "graph.add_paper"} <= ops)

    def test_voice_jobs_match_states(self):
        c = self.conn(self.data)
        pairs = {(r[0], r[1]) for r in c.execute(
            "SELECT e.state, v.state FROM episodes e LEFT JOIN voice_jobs v ON v.episode_id = e.id")}
        allowed = {("waiting-for-gpu", "queued"), ("speaking", "claimed"), ("ready", "done"), ("failed", "failed"),
                   ("checking", None), ("rejected", None)}
        self.assertLessEqual(pairs, allowed)
        self.assertLessEqual(c.execute("SELECT COUNT(*) FROM episodes WHERE state = 'speaking'").fetchone()[0], 1)

    def test_refuses_a_filled_dir_unless_wipe(self):
        with self.assertRaises(SystemExit):
            fake_data.generate(self.data, papers=5, seed=3, audio="none")
        other = self.root / "w"
        fake_data.generate(other, papers=5, seed=1, audio="none")
        n = fake_data.generate(other, papers=7, seed=1, audio="none", do_wipe=True)
        self.assertEqual(n["papers"], 7)
        self.assertEqual(self.conn(other).execute("SELECT COUNT(*) FROM papers").fetchone()[0], 7)


class TestDeterminism(Base):
    def test_same_seed_same_library(self):
        a, b, x = self.root / "a", self.root / "b", self.root / "x"
        fake_data.generate(a, papers=25, seed=11, audio="none")
        fake_data.generate(b, papers=25, seed=11, audio="none")
        fake_data.generate(x, papers=25, seed=12, audio="none")
        da, db_, dx = dump(a), dump(b), dump(x)
        self.assertEqual(da, db_)
        self.assertNotEqual(da["papers"], dx["papers"])


@unittest.skipUnless(FFMPEG, "no ffmpeg on this machine")
class TestAudio(Base):
    def test_tones_for_ready_episodes(self):
        d = self.root / "t"
        n = fake_data.generate(d, papers=12, seed=5, audio="tone", ffmpeg=FFMPEG)
        self.assertTrue(n["tones"])
        c = self.conn(d)
        ready = c.execute("SELECT id, duration_s FROM episodes WHERE state = 'ready'").fetchall()
        self.assertTrue(ready)
        self.assertEqual(n["audio"], len(ready))
        for e in ready:
            mp3 = (d / "episodes" / e["id"] / "audio.mp3").read_bytes()
            self.assertTrue(mp3[:3] == b"ID3" or (mp3[0] == 0xFF and mp3[1] & 0xE0 == 0xE0))
            self.assertIn(e["duration_s"], (24.0, 36.0, 48.0))
        for p in c.execute("SELECT p.seconds, e.duration_s FROM positions p JOIN episodes e ON e.id = p.episode_id"):
            self.assertLess(p[0], p[1])
        self.assertEqual(list(d.glob("pcg-fake-tones-*")), [])


if __name__ == "__main__":
    unittest.main()
