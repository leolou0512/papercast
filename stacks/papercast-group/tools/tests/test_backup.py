"""tools/backup.py: a consistent online snapshot, link-dest copies, keeping the newest N."""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

GROUP = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(GROUP))

from hub import db  # noqa: E402
from tools import _common, backup  # noqa: E402

T0 = datetime(2026, 9, 28, 3, 0, 0, tzinfo=timezone.utc)


class TestBackup(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.data, self.dest = root / "data", root / "backups"
        _common.open_hub(self.data)
        c = db.conn()
        c.execute("INSERT INTO users(email, name, role, created_at) VALUES ('a@example.org', 'Ann', 'admin', ?)", (db.now(),))
        c.execute("INSERT INTO papers(id, title, title_norm, created_at) VALUES ('p_aaaaaaaaaaaa', 'A', 'a', ?)", (db.now(),))
        ep = self.data / "episodes" / "e_aaaaaaaaaaaa"
        (ep / "voice" / "chunks").mkdir(parents=True)
        (ep / "script.md").write_text("A script.\n")
        (ep / "audio.mp3").write_bytes(b"\xff\xfb" + b"\0" * 5000)
        (ep / "voice" / "chunks" / "c1.wav").write_bytes(b"RIFF" + b"\0" * 100)
        self.ep = ep

    def tearDown(self):
        db.close()
        self.tmp.cleanup()

    def test_snapshot_and_files(self):
        r = backup.backup(self.data, self.dest, when=T0)
        b = Path(r["path"])
        self.assertEqual(b.name, "2026-09-28T030000Z")
        c = sqlite3.connect(str(b / "hub.db"))
        self.assertEqual(c.execute("PRAGMA journal_mode").fetchone()[0], "delete")
        self.assertEqual(c.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        self.assertEqual(c.execute("SELECT name FROM users").fetchone()[0], "Ann")
        self.assertEqual(c.execute("SELECT COUNT(*) FROM graphs").fetchone()[0], 5)
        c.close()
        self.assertFalse((b / "hub.db-wal").exists())
        e = b / "episodes" / "e_aaaaaaaaaaaa"
        self.assertEqual((e / "script.md").read_text(), "A script.\n")
        self.assertEqual((e / "audio.mp3").read_bytes(), (self.ep / "audio.mp3").read_bytes())
        self.assertFalse((e / "voice").exists())                         # scratch left out
        rep = json.loads((b / "backup.json").read_text())
        self.assertEqual((rep["files"], rep["copied"], rep["linked"]), (2, 2, 0))
        self.assertEqual(rep["schema_version"], db.SCHEMA_VERSION)
        self.assertEqual(r["removed"], [])

    def test_with_voice(self):
        r = backup.backup(self.data, self.dest, with_voice=True, when=T0)
        self.assertTrue((Path(r["path"]) / "episodes/e_aaaaaaaaaaaa/voice/chunks/c1.wav").is_file())

    def test_unchanged_files_link_to_previous(self):
        a = Path(backup.backup(self.data, self.dest, when=T0)["path"])
        (self.ep / "script.md").write_text("A longer script now.\n")
        (self.data / "episodes" / "e_bbbbbbbbbbbb").mkdir()
        (self.data / "episodes" / "e_bbbbbbbbbbbb" / "script.md").write_text("New.\n")
        r = backup.backup(self.data, self.dest, when=T0 + timedelta(days=1))
        b = Path(r["path"])
        ea, eb = a / "episodes/e_aaaaaaaaaaaa", b / "episodes/e_aaaaaaaaaaaa"
        self.assertEqual(os.stat(ea / "audio.mp3").st_ino, os.stat(eb / "audio.mp3").st_ino)
        self.assertNotEqual(os.stat(ea / "script.md").st_ino, os.stat(eb / "script.md").st_ino)
        self.assertEqual((ea / "script.md").read_text(), "A script.\n")     # the old backup is untouched
        self.assertEqual((eb / "script.md").read_text(), "A longer script now.\n")
        self.assertEqual((b / "episodes/e_bbbbbbbbbbbb/script.md").read_text(), "New.\n")
        self.assertEqual((r["linked"], r["copied"]), (1, 2))
        self.assertEqual(r["previous"], a.name)

    def test_keeps_newest(self):
        (self.dest / "notes").mkdir(parents=True)                        # not a backup: left alone
        names = []
        for i in range(4):
            names.append(Path(backup.backup(self.data, self.dest, keep=2, when=T0 + timedelta(days=i))["path"]).name)
        left = [p.name for p in backup.backups(self.dest)]
        self.assertEqual(left, names[-2:])
        self.assertTrue((self.dest / "notes").is_dir())
        # the survivors are whole even though the ones they linked to are gone
        self.assertEqual((self.dest / names[-1] / "episodes/e_aaaaaaaaaaaa/audio.mp3").stat().st_size, 5002)

    def test_same_second_twice(self):
        a = backup.backup(self.data, self.dest, when=T0)["name"]
        b = backup.backup(self.data, self.dest, when=T0)["name"]
        self.assertEqual((a, b), ("2026-09-28T030000Z", "2026-09-28T030000Z-2"))

    def test_consistent_while_a_write_is_open(self):
        w = sqlite3.connect(str(self.data / "hub.db"), isolation_level=None)
        w.execute("BEGIN IMMEDIATE")
        w.execute("INSERT INTO users(email, name, role, created_at) VALUES ('b@example.org', 'Bob', 'viewer', ?)", (db.now(),))
        try:
            b = Path(backup.backup(self.data, self.dest, when=T0)["path"])
        finally:
            w.execute("COMMIT")
            w.close()
        c = sqlite3.connect(str(b / "hub.db"))
        self.assertEqual([r[0] for r in c.execute("SELECT name FROM users")], ["Ann"])   # the committed state only
        c.close()
        b2 = Path(backup.backup(self.data, self.dest, when=T0 + timedelta(hours=1))["path"])
        c = sqlite3.connect(str(b2 / "hub.db"))
        self.assertEqual(c.execute("SELECT COUNT(*) FROM users").fetchone()[0], 2)
        c.close()

    def test_partial_left_by_a_crash_is_removed(self):
        (self.dest / ".partial-2026-09-27T030000Z").mkdir(parents=True)
        backup.backup(self.data, self.dest, when=T0)
        self.assertEqual([p.name for p in self.dest.iterdir() if p.name.startswith(".partial")], [])
        self.assertEqual(len(backup.backups(self.dest)), 1)

    def test_refuses_dest_inside_episodes(self):
        with self.assertRaises(SystemExit):
            backup.backup(self.data, self.data / "episodes" / "bk")
        with self.assertRaises(SystemExit):
            backup.backup(self.data, self.data)

    def test_command_line(self):
        r = subprocess.run([sys.executable, str(GROUP / "tools" / "backup.py"), "--data", str(self.data),
                            "--dest", str(self.dest), "--keep", "3"], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("2 files", r.stdout)
        self.assertEqual(len(backup.backups(self.dest)), 1)


if __name__ == "__main__":
    unittest.main()
