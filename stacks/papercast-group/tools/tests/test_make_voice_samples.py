"""tools/make_voice_samples.py with a fake papercast-voice (deploy/tests/fake_papercast_voice.py):
one clip per preset, each job.json carrying that preset's voice (the CPU one with engine cpu),
the clips where the hub serves them, kept when there, and never kept in the wrong voice."""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

GROUP = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(GROUP))
sys.path.insert(0, str(GROUP / "deploy" / "tests"))

import fake_papercast_voice as fv  # noqa: E402
from hub import voices  # noqa: E402

TOOL = GROUP / "tools" / "make_voice_samples.py"


class TestSamples(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="pcg-samples-")
        self.d = Path(self.tmp.name)
        self.data = self.d / "data"

    def tearDown(self):
        self.tmp.cleanup()

    def run_tool(self, *args, **knobs):
        voice = fv.write_wrapper(str(self.d / "papercast-voice"), seconds=2, chunk_s=0.01, **knobs)
        r = subprocess.run([sys.executable, str(TOOL), "--data", str(self.data), "--voice-cmd", voice,
                            "--poll-s", "0.05", *args], capture_output=True, text=True, timeout=120)
        return r.returncode, r.stdout + r.stderr

    def test_one_clip_per_voice(self):
        rc, out = self.run_tool("--keep")
        self.assertEqual(rc, 0, out)
        vd = self.data / "voices"
        for p in voices.PRESETS:
            with self.subTest(voice=p["id"]):
                mp3 = vd / f"{p['id']}.mp3"
                self.assertEqual(mp3.read_bytes()[:2], b"\xff\xfb")
                meta = json.loads((vd / f"{p['id']}.json").read_text())
                self.assertEqual((meta["voice"], meta["name"]), (p["key"], p["name"]))
                job = json.loads((vd / "work" / p["id"] / "voice" / "job.json").read_text())
                self.assertEqual(job["voice"], voices.for_claim(p)["spec"])
                self.assertEqual(job["engine"], "cpu" if p.get("cpu") else "auto")
                self.assertRegex(job["paper_id"], fv.ID_RE.pattern)
                script = (vd / "work" / p["id"] / "voice" / "script.md").read_text()
                self.assertFalse(any(ch.isdigit() for ch in script), "papercast-voice refuses digits")
        # there already: kept; --force --only: that one again; the work dirs go without --keep
        before = (vd / "warm-male.json").read_text()
        rc, out = self.run_tool()
        self.assertEqual(rc, 0, out)
        self.assertIn("warm-male: there already", out)
        self.assertEqual((vd / "warm-male.json").read_text(), before)
        (vd / "warm-male.json").write_text("{}")
        rc, out = self.run_tool("--force", "--only", "warm-male")
        self.assertEqual(rc, 0, out)
        self.assertEqual(json.loads((vd / "warm-male.json").read_text())["voice"], "preset-warm-male-s42")
        self.assertFalse((vd / "work" / "warm-male").exists())
        self.assertEqual(self.run_tool("--only", "nobody")[0], 2)

    def test_failures_and_the_wrong_voice_are_not_kept(self):
        rc, out = self.run_tool("--only", "warm-male", fail="gpu_oom")
        self.assertEqual(rc, 1, out)
        self.assertIn("warm-male: not made: gpu_oom", out)
        self.assertFalse((self.data / "voices" / "warm-male.mp3").exists())
        rc, out = self.run_tool("--only", "calm-male", ignore_voice=1)
        self.assertEqual(rc, 1, out)
        self.assertIn("not kept: it came out in the voice 'described-narrator-a-seed42'", out)
        self.assertFalse((self.data / "voices" / "calm-male.mp3").exists())


if __name__ == "__main__":
    unittest.main()
