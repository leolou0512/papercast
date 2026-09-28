"""tools/backfill_timings.py on a synthetic job dir: real WAV chunks (a tone per sentence, known
lengths), plan.json as papercast-voice writes it, the hub's episode with audio; the timings made
by papercast-voice's code with the voice's Python, checked and stored where the hub serves them.

Needs a Python with numpy and soundfile for papercast-voice (PAPERCAST_VOICE_PYTHON, else Leo's
install at /home/leo/papercast/voice/venv/bin/python); skips without one."""
from __future__ import annotations

import hashlib
import json
import math
import os
import struct
import subprocess
import sys
import tempfile
import unittest
import wave
from pathlib import Path

GROUP = Path(__file__).resolve().parents[2]
REPO = GROUP.parent.parent
sys.path.insert(0, str(GROUP))

from hub import db  # noqa: E402
from tools import _common  # noqa: E402

TOOL = GROUP / "tools" / "backfill_timings.py"
VSRC = REPO / "stacks" / "papercast" / "voice"
VPY = Path(os.environ.get("PAPERCAST_VOICE_PYTHON") or "/home/leo/papercast/voice/venv/bin/python")
SKIP = None if VPY.exists() else f"no Python with numpy and soundfile for papercast-voice ({VPY})"
SR = 24000
PER_CHAR = 0.05
SCRIPT = ("# How the model is trained\n\n"
          "Noise is added to every image, a little at a time. The network learns to take it away again. "
          "That is all it is ever asked to do.\n\n"
          "At the end, sampling starts from pure noise and runs the steps backwards. Each step is small.\n")


def plan(script: str, max_words: int) -> list:
    """textprep.plan's chunks, as plan.json holds them (asked of papercast-voice itself)."""
    code = ("import json, sys; from papercast_voice import textprep; from papercast_voice.config import DEFAULTS; "
            f"print(json.dumps([c.as_dict() for c in textprep.plan(sys.stdin.read(), {max_words}, DEFAULTS['audio'])]))")
    r = subprocess.run([str(VPY), "-c", code], input=script, capture_output=True, text=True,
                       env={"PYTHONPATH": str(VSRC), "PATH": os.environ["PATH"]}, timeout=60, check=True)
    return json.loads(r.stdout)


def tone_wav(path: Path, seconds: float, lead: float = 0.2, tail: float = 0.3) -> None:
    """A chunk as an engine writes one: silence, a steady tone, silence; 16-bit mono."""
    frames = bytearray()
    for _ in range(int(lead * SR)):
        frames += struct.pack("<h", 0)
    for i in range(int(seconds * SR)):
        t = i / SR
        frames += struct.pack("<h", int(6000 * (math.sin(2 * math.pi * 160 * t) + 0.4 * math.sin(2 * math.pi * 320 * t))))
    for _ in range(int(tail * SR)):
        frames += struct.pack("<h", 0)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(bytes(frames))


@unittest.skipIf(SKIP, SKIP or "")
class TestBackfill(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="pcg-backfill-")
        root = Path(self.tmp.name)
        self.data, self.vhome = root / "data", root / "voice-home"
        self.vhome.mkdir()
        _common.open_hub(self.data, seed=False)
        c = db.conn()
        c.execute("INSERT INTO users(email, name, role, created_at) VALUES ('a@example.org', 'Ann', 'contributor', ?)", (db.now(),))
        c.execute("INSERT INTO papers(id, title, title_norm, created_at) VALUES ('p_aaaaaaaaaaaa', 'A', 'a', ?)", (db.now(),))
        self.n = 0

    def tearDown(self):
        db.close()
        self.tmp.cleanup()

    def episode(self, duration_s=None, chunks=True, key="breeze-described-narrator-a-seed42-1.0-w20-p1"):
        """An episode with audio whose job dir holds its chunks; (id, the chunks' total length)."""
        self.n += 1
        eid = f"e_{'abcdefghijkl'[self.n]}" + "a" * 11
        ep = self.data / "episodes" / eid
        vdir = ep / "voice"
        cdir = vdir / "chunks" / key
        cdir.mkdir(parents=True)
        (ep / "audio.mp3").write_bytes(b"\xff\xfb" + b"\0" * 5000)
        (vdir / "script.md").write_text(SCRIPT)
        cs = plan(SCRIPT, 20)
        total = 0.4
        for ch in cs:
            secs = len(ch["text"]) * PER_CHAR
            total += secs + 0.12 + ch["gap_after_s"]
            if chunks:
                tone_wav(cdir / f"{ch['idx']:04d}-{hashlib.sha1(ch['text'].encode()).hexdigest()[:12]}.wav", secs)
        (cdir / "plan.json").write_text(json.dumps({"engine": "breeze", "chunks": cs}))
        (vdir / "status.json").write_text(json.dumps({"phase": "done", "output": {
            "path": "out/episode.mp3", "voice": "described-narrator-a-seed42", "duration_s": round(total, 1)}}))
        db.conn().execute("INSERT INTO episodes(id, paper_id, made_by, state, duration_s, created_at, updated_at) "
                          "VALUES (?, 'p_aaaaaaaaaaaa', 1, 'ready', ?, ?, ?)",
                          (eid, duration_s if duration_s is not None else round(total, 1), db.now(), db.now()))
        return eid, total

    def run_tool(self, *args):
        r = subprocess.run([sys.executable, str(TOOL), "--data", str(self.data), "--voice-home", str(self.vhome),
                            "--voice-python", str(VPY), *args], capture_output=True, text=True, timeout=300)
        return r.returncode, r.stdout + r.stderr

    def test_timings_from_the_chunks(self):
        eid, total = self.episode()
        rc, out = self.run_tool()
        self.assertEqual(rc, 0, out)
        doc = json.loads((self.data / "episodes" / eid / "timings.json").read_text())
        segs = doc["segments"]
        self.assertEqual(doc["version"], 1)
        self.assertEqual([s["text"] for s in segs], [
            "How the model is trained", "Noise is added to every image, a little at a time.",
            "The network learns to take it away again.", "That is all it is ever asked to do.",
            "At the end, sampling starts from pure noise and runs the steps backwards.", "Each step is small."])
        self.assertEqual([s["start"] for s in segs], sorted(s["start"] for s in segs))
        # the first sentence: after the 0.4 s lead-in, the chunk's own silence trimmed to 0.06 s
        self.assertAlmostEqual(segs[0]["start"], 0.46, delta=0.02)
        self.assertAlmostEqual(segs[0]["end"], 0.46 + len("How the model is trained.") * PER_CHAR, delta=0.03)
        self.assertLessEqual(segs[-1]["end"], total)
        self.assertIn(f"{eid}: 6 sentences", out)
        # run again: nothing to do; --force: made again
        rc, out = self.run_tool()
        self.assertEqual((rc, "0 got timings, 1 skipped" in out), (0, True), out)
        rc, out = self.run_tool("--force", eid)
        self.assertEqual((rc, f"{eid}: 6 sentences" in out), (0, True), out)

    def test_what_it_cannot_do(self):
        gone, _ = self.episode(chunks=False)                    # the chunks were deleted
        short, _ = self.episode(duration_s=2.0)                 # not the audio these chunks made
        rc, out = self.run_tool(gone, short, "e_nosuchepisode")
        self.assertEqual(rc, 1, out)
        self.assertIn(f"{gone}: no complete set of chunk WAVs", out)
        self.assertIn(f"{short}: the timings end at", out)
        self.assertIn("e_nosuchepisode: no such episode", out)
        self.assertFalse((self.data / "episodes" / gone / "timings.json").exists())
        self.assertFalse((self.data / "episodes" / short / "timings.json").exists())
        # --dry-run writes nothing
        ok, _ = self.episode()
        rc, out = self.run_tool("--dry-run", ok)
        self.assertEqual(rc, 0, out)
        self.assertIn("not written (--dry-run)", out)
        self.assertFalse((self.data / "episodes" / ok / "timings.json").exists())


if __name__ == "__main__":
    unittest.main()
