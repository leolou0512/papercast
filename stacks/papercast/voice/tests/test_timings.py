"""Sentence timings (timings.py): the sentence mapping, their alignment with the real encoded MP3
(real WAV chunks of known length through join and the same ffmpeg steps as an episode), the
backfill from a job's chunks, and a whole job with a chosen voice writing out/timings.json."""
import hashlib
import json
import os
import tempfile
import unittest

import numpy as np
import soundfile as sf

from helpers import SCRIPT, Rig, status, voice_log
from papercast_voice import audio, textprep, timings
from papercast_voice.config import DEFAULTS

A = DEFAULTS["audio"]
SR = 24000
R = 0.055            # seconds of "speech" per character in the synthetic chunks

LONG = ("# A method in three steps\n\n"
        "First the data is noised a little at a time. Then a network learns to undo each step. "
        "Sampling runs the steps backwards.\n\n"
        "The long sentence here goes on and on, with a clause about the schedule, another about "
        "the weights, a third one about the loss, and a fourth one about how the samples are "
        "judged at the end of it all, so that it must be cut. A short one ends it.\n\n"
        "# What comes out\n\n"
        "The samples are sharp. They are also varied, which is the point.\n")


def burst(seconds: float, seed: int) -> np.ndarray:
    """A steady voiced tone (no dips inside it), so a burst is one run of sound."""
    rng = np.random.default_rng(seed)
    t = np.arange(int(seconds * SR)) / SR
    f0 = 150 + 20 * rng.random()
    x = sum(np.sin(2 * np.pi * f0 * k * t) / k for k in range(1, 5))
    wob = 0.8 + 0.2 * np.sin(2 * np.pi * 1.3 * t + rng.random() * 6)
    return (0.08 * wob * x).astype(np.float32)


def chunk_audio(pieces: list, seed: int, sentence_gap: float = R, rate=None) -> np.ndarray:
    """A chunk as an engine might return it: silence of its own at both ends, and each piece
    [(sentence, text)] a burst as long as its characters (at `rate(sentence)` seconds each), one
    character's time apart (the joining space), or sentence_gap apart between two sentences."""
    rng = np.random.default_rng(seed)
    parts = [np.zeros(int(rng.uniform(0.1, 0.4) * SR), np.float32)]
    for i, (sid, p) in enumerate(pieces):
        parts.append(burst(len(p) * (rate(sid) if rate else R), seed * 100 + i))
        if i < len(pieces) - 1:
            gap = sentence_gap if pieces[i + 1][0] != sid else R
            parts.append(np.zeros(int(gap * SR), np.float32))
    parts.append(np.zeros(int(rng.uniform(0.1, 0.5) * SR), np.float32))
    return np.concatenate(parts)


def runs(x: np.ndarray, sr: int, floor_db: float = -30.0, hop_s: float = 0.005) -> list:
    """(start_s, end_s) of each run of sound in decoded audio."""
    hop = int(hop_s * sr)
    n = len(x) // hop
    rms = np.sqrt(np.mean(x[: n * hop].reshape(n, hop) ** 2, axis=1) + 1e-12)
    on = 20 * np.log10(rms / rms.max()) > floor_db
    out, start = [], None
    for i, v in enumerate(on):
        if v and start is None:
            start = i
        elif not v and start is not None:
            out.append((start * hop_s, i * hop_s))
            start = None
    if start is not None:
        out.append((start * hop_s, n * hop_s))
    return out


def make_chunks(d: str, script: str, max_words: int, **how):
    """The plan's chunks as WAVs in d, named as job.py names them; (plan, paths, pieces)."""
    plan = textprep.plan(script, max_words, A)
    chunks, _sentences = timings.sentence_chunks(script, max_words, A)
    paths = []
    for c, pieces in zip(plan, chunks):
        h = hashlib.sha1(c.text.encode()).hexdigest()[:12]
        p = os.path.join(d, f"{c.idx:04d}-{h}.wav")
        sf.write(p, chunk_audio(pieces, c.idx + 1, **how), SR, subtype="PCM_16")
        paths.append(p)
    return plan, paths, chunks


class Mapping(unittest.TestCase):
    def test_same_chunks_as_the_plan(self):
        for script, mw in ((SCRIPT, 20), (SCRIPT, 75), (LONG, 20), (LONG, 12)):
            with self.subTest(max_words=mw):
                chunks, sentences = timings.sentence_chunks(script, mw, A)
                plan = textprep.plan(script, mw, A)
                self.assertEqual([" ".join(p for _s, p in ch) for ch in chunks], [c.text for c in plan])
                self.assertEqual(sorted({s for ch in chunks for s, _p in ch}), list(range(len(sentences))))

    def test_segments_one_per_sentence_in_order(self):
        plan = textprep.plan(LONG, 20, A)
        spans, t = [], 0.4
        for c in plan:
            spans.append((t, t + 0.4 * c.words))
            t += 0.4 * c.words + c.gap_after_s
        segs = timings.segments(LONG, [c.text for c in plan], spans, 20, A)
        texts = [s["text"] for s in segs]
        self.assertEqual(texts[0], "A method in three steps")          # as written, no added full stop
        self.assertEqual(texts[1], "First the data is noised a little at a time.")
        self.assertIn("What comes out", texts)
        self.assertEqual(texts[-1], "They are also varied, which is the point.")
        self.assertEqual(len(texts), 9)
        long = next(s for s in segs if s["text"].startswith("The long sentence"))
        cut = [c for c in plan if c.text in long["text"] or long["text"].startswith(c.text)]
        self.assertGreaterEqual(len(cut), 2, "the long sentence is over two chunks")
        starts = [s["start"] for s in segs]
        self.assertEqual(starts, sorted(starts))
        for s in segs:
            self.assertLess(s["start"], s["end"])
        # the first sentence starts after the lead-in and the trim's padding
        self.assertAlmostEqual(segs[0]["start"], 0.4 + A["trim_pad_s"], places=3)

    def test_changed_script_falls_back_to_chunks(self):
        plan = textprep.plan(SCRIPT, 20, A)
        spans = [(i * 5.0, i * 5.0 + 4.0) for i in range(len(plan))]
        segs = timings.segments("# Another script\n\nNothing like it.\n", [c.text for c in plan], spans, 20, A)
        self.assertEqual([s["text"] for s in segs], [c.text for c in plan])


class Alignment(unittest.TestCase):
    """The timings against the MP3 itself: every sentence's start and end within 0.15 s of where
    its sound really is in the decoded file."""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory(prefix="pcv-timings-")
        self.addCleanup(self.td.cleanup)
        from helpers import ffmpeg
        self.ff = ffmpeg()

    def encoded(self, script, mw, **how):
        """Chunks -> join -> the episode's loudnorm and MP3 steps -> decoded; the timings, and
        where each sentence's sound really is in the decoded MP3."""
        d = tempfile.mkdtemp(dir=self.td.name)
        plan, paths, chunks = make_chunks(d, script, mw, **how)
        joined, mp3 = os.path.join(d, "joined.wav"), os.path.join(d, "episode.mp3")
        gaps = [c.gap_after_s for c in plan]
        j = audio.join(paths, gaps, joined, lead_in_s=A["lead_in_s"],
                       threshold_db=A["trim_threshold_db"], pad_s=A["trim_pad_s"])
        lay = audio.layout(paths, gaps, lead_in_s=A["lead_in_s"],
                           threshold_db=A["trim_threshold_db"], pad_s=A["trim_pad_s"])
        self.assertEqual((lay["spans"], lay["pauses"], lay["samples"]), (j["spans"], j["pauses"], j["samples"]))
        audio.normalise_encode(self.ff, joined, mp3, A, d)
        x = audio.decode(self.ff, mp3, 44100)
        self.assertAlmostEqual(len(x) / 44100, j["duration_s"], delta=0.05)
        found = runs(x, 44100)
        pieces = [(s, p) for ch in chunks for s, p in ch]
        self.assertEqual(len(found), len(pieces), found)
        truth: dict = {}
        for (sid, _p), (a, b) in zip(pieces, found):
            truth.setdefault(sid, [a, b])[1] = b
        texts = [c.text for c in plan]
        return (timings.segments(script, texts, j["spans"], mw, A, j["pauses"]),
                timings.segments(script, texts, j["spans"], mw, A), truth)

    def worst(self, segs, truth):
        self.assertEqual(len(segs), len(truth))
        return max(max(abs(s["start"] - truth[i][0]), abs(s["end"] - truth[i][1])) for i, s in enumerate(segs))

    def test_timings_match_the_encoded_mp3(self):
        segs, _plain, truth = self.encoded(LONG, 20)
        worst = self.worst(segs, truth)
        self.assertLess(worst, 0.15, (segs, truth))
        print(f"\n  even speech: worst edge {worst * 1000:.0f} ms over {len(segs)} sentences", end=" ")

    def test_pauses_put_uneven_speech_right(self):
        # sentences at different speeds, 0.4 s apart inside a chunk: the characters alone are
        # off; the pauses in the audio put every boundary within 0.15 s
        rates = [0.04, 0.075, 0.05, 0.08, 0.045, 0.07, 0.055, 0.04, 0.08]
        segs, plain, truth = self.encoded(LONG, 40, sentence_gap=0.4, rate=lambda sid: rates[sid % len(rates)])
        worst, worst_plain = self.worst(segs, truth), self.worst(plain, truth)
        self.assertLess(worst, 0.15, (segs, truth))
        self.assertGreater(worst_plain, 0.15, "the test would not show the pauses matter")
        print(f"\n  uneven speech: worst edge {worst * 1000:.0f} ms with pauses, "
              f"{worst_plain * 1000:.0f} ms by characters alone", end=" ")


class Backfill(unittest.TestCase):
    def setUp(self):
        self.rig = Rig(gpu=True)
        self.addCleanup(self.rig.cleanup)

    def job_with_chunks(self, key="fakegpu-described-x-1.0-w20-p1", duration=None):
        vdir = self.rig.job(script=LONG)
        cdir = os.path.join(vdir, "chunks", key)
        os.makedirs(cdir)
        plan, paths, _ = make_chunks(cdir, LONG, 20)
        with open(os.path.join(cdir, "plan.json"), "w") as fh:
            json.dump({"engine": "fakegpu", "chunks": [c.as_dict() for c in plan]}, fh)
        lay = audio.layout(paths, [c.gap_after_s for c in plan], lead_in_s=A["lead_in_s"],
                           threshold_db=A["trim_threshold_db"], pad_s=A["trim_pad_s"])
        with open(os.path.join(vdir, "status.json"), "w") as fh:
            json.dump({"phase": "done", "output": {"path": "out/episode.mp3", "voice": "described-x",
                                                   "duration_s": round(duration or lay["duration_s"], 1)}}, fh)
        return vdir, plan, lay

    def test_cli_writes_the_timings_join_would_give(self):
        vdir, plan, lay = self.job_with_chunks()
        r = self.rig.cli("timings", vdir)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        rep = json.loads(r.stdout.strip().splitlines()[-1])
        self.assertTrue(rep["ok"])
        self.assertEqual(rep["chunks"], "fakegpu-described-x-1.0-w20-p1")
        with open(os.path.join(vdir, "out", "timings.json")) as fh:
            doc = json.load(fh)
        self.assertEqual(doc["version"], 1)
        self.assertEqual(doc["segments"], timings.segments(LONG, [c.text for c in plan], lay["spans"], 20, A))
        self.assertEqual(rep["segments"], len(doc["segments"]))
        out = os.path.join(self.rig.tmp, "t.json")
        self.assertEqual(self.rig.cli("timings", vdir, "--out", out).returncode, 0)
        self.assertTrue(os.path.isfile(out))

    def test_refuses_chunks_that_do_not_add_up_to_the_mp3(self):
        vdir, _plan, lay = self.job_with_chunks(duration=None)
        with open(os.path.join(vdir, "status.json"), "w") as fh:
            json.dump({"phase": "done", "output": {"duration_s": lay["duration_s"] + 30}}, fh)
        r = self.rig.cli("timings", vdir)
        self.assertEqual(r.returncode, 2)
        self.assertIn("not the ones it was made from", json.loads(r.stdout)["problem"])

    def test_refuses_without_chunks(self):
        vdir = self.rig.job()
        r = self.rig.cli("timings", vdir)
        self.assertEqual(r.returncode, 2)
        self.assertIn("no complete set of chunk WAVs", json.loads(r.stdout)["problem"])


class JobVoice(unittest.TestCase):
    """job.json `voice` reaches the engine, names the chunks and the output; out/timings.json."""

    def setUp(self):
        self.rig = Rig(gpu=True)
        self.addCleanup(self.rig.cleanup)

    def job(self, voice, engine="auto", script=SCRIPT):
        vdir = self.rig.job(script=script, engine=engine)
        p = os.path.join(vdir, "job.json")
        with open(p) as fh:
            j = json.load(fh)
        j["voice"] = voice
        with open(p, "w") as fh:
            json.dump(j, fh)
        return vdir

    def test_the_voice_reaches_the_engine_and_the_timings_are_written(self):
        v = {"id": "warm-male", "engine": "fakegpu", "voice": "preset-warm-male-s7",
             "instruction": "A warm man in his forties, narrating.", "seed": 7}
        vdir = self.job(v)
        self.assertEqual(self.rig.run(vdir), 0, voice_log(vdir)[-3000:])
        st = status(vdir)
        self.assertEqual(st["phase"], "done", voice_log(vdir)[-2000:])
        calls = self.rig.fake_calls()
        self.assertTrue(calls)
        for c in calls:
            self.assertEqual((c["voice"], c["instruction"], c["seed"]),
                             ("preset-warm-male-s7", "A warm man in his forties, narrating.", 7))
        self.assertEqual(st["output"]["voice"], "preset-warm-male-s7")
        self.assertEqual(st["output"]["timings"], "out/timings.json")
        self.assertTrue(any("preset-warm-male-s7" in d for d in os.listdir(os.path.join(vdir, "chunks"))))
        with open(os.path.join(vdir, "out", "timings.json")) as fh:
            doc = json.load(fh)
        self.assertEqual(doc["version"], 1)
        segs = doc["segments"]
        self.assertEqual(segs[0]["text"], "How guidance steers the model")
        self.assertTrue(segs[1]["text"].startswith("Here is how the model steers"))
        self.assertLessEqual(segs[-1]["end"], st["output"]["duration_s"] + 0.1)
        self.assertAlmostEqual(doc["duration_s"], st["output"]["duration_s"], delta=0.1)
        self.assertEqual([s["start"] for s in segs], sorted(s["start"] for s in segs))

    def test_cpu_fallback_keeps_the_cpu_voice(self):
        vdir = self.job({"engine": "fakegpu", "voice": "preset-x", "seed": 3}, engine="cpu")
        self.assertEqual(self.rig.run(vdir), 0, voice_log(vdir)[-3000:])
        self.assertEqual({c["voice"] for c in self.rig.fake_calls()}, {"fake-cpu-voice"})
        self.assertEqual(status(vdir)["output"]["voice"], "fake-cpu-voice")

    def test_a_cpu_voice(self):
        vdir = self.job({"engine": "kokoro", "voice": "af_other"}, engine="cpu")
        self.assertEqual(self.rig.run(vdir), 0, voice_log(vdir)[-3000:])
        self.assertEqual({c["voice"] for c in self.rig.fake_calls()}, {"af_other"})

    def test_bad_voices_are_refused(self):
        for v, why in (({"engine": "nope", "voice": "x"}, "not configured"),
                       ({"engine": "fakegpu", "voice": "x", "python": "/bin/sh"}, "may not set"),
                       ({"engine": "fakegpu"}, "needs a `voice` name"),
                       ({"engine": "fakegpu", "voice": "a/b"}, "needs a `voice` name"),
                       ({"engine": "fakegpu", "voice": "x", "seed": "7"}, "seed"),
                       ({"engine": "fakegpu", "voice": "x", "instruction": ""}, "instruction"),
                       ("warm", "must be an object")):
            with self.subTest(why=why):
                vdir = self.job(v)
                self.assertEqual(self.rig.run(vdir), 3)
                st = status(vdir)
                self.assertEqual(st["phase"], "failed")
                self.assertIn(why, st["error"]["message"])
                self.assertEqual(self.rig.fake_calls(), [], "nothing reached an engine")


if __name__ == "__main__":
    unittest.main()
