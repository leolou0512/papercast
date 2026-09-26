"""Loudness, encoding and tags on synthetic audio, measured with independent instruments."""
import os
import tempfile
import unittest

import numpy as np
import soundfile as sf

from helpers import ffmpeg
from papercast_voice import audio, tags
from papercast_voice.config import DEFAULTS

A = DEFAULTS["audio"]


def speechlike(seconds: float, sr: int = 24000, gain: float = 0.1, seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    t = np.arange(int(seconds * sr)) / sr
    syll = np.maximum(0, np.sin(2 * np.pi * 4 * t)) ** 2
    carrier = sum(np.sin(2 * np.pi * 150 * k * t) / k for k in range(1, 6))
    wobble = 0.7 + 0.3 * np.sin(2 * np.pi * 0.7 * t + rng.random() * 6)   # phrase-level loudness
    x = gain * syll * carrier * wobble
    x[rng.random(len(t)) < 0.0005] *= 2.5                   # the odd transient, for the limiter
    return x.astype(np.float32)


class Loudness(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.ff = ffmpeg()

    def _encode(self, x, sr=24000):
        wav = os.path.join(self.td.name, "in.wav")
        sf.write(wav, x, sr, subtype="FLOAT")
        out = os.path.join(self.td.name, "out.mp3")
        rep = audio.normalise_encode(self.ff, wav, out, A, self.td.name)
        return out, rep

    def test_quiet_and_loud_inputs_land_on_target(self):
        for name, gain in (("quiet", 0.01), ("loud", 0.9)):
            with self.subTest(name):
                out, rep = self._encode(np.clip(speechlike(40, gain=gain), -1, 1))
                m = audio.measure(self.ff, out, 44100)
                self.assertLessEqual(abs(m["lufs"] - A["lufs"]), A["lufs_tolerance"] + 0.05, (m, rep["aims"]))
                self.assertLess(abs(m["lufs"] - m["lufs_ffmpeg"]), 0.5, m)   # two meters agree
                self.assertLessEqual(m["true_peak_db"], A["true_peak_db"], m)
                self.assertEqual((m["codec"], m["channels"], m["sample_rate"], m["bitrate_kbps"]),
                                 ("mp3", "mono", 44100, 96))
                self.assertAlmostEqual(m["duration_s"], 40.0, delta=0.2)

    def test_silence_is_refused(self):
        with self.assertRaises(audio.EncodeError):
            self._encode(np.zeros(24000 * 10, dtype=np.float32))


class JoinTrim(unittest.TestCase):
    def test_trim_keeps_only_pad(self):
        sr = 24000
        core = audio.trim(speechlike(2.0), sr, -45.0, 0.0)       # the voiced span itself
        x = np.concatenate([np.zeros(sr), speechlike(2.0), np.zeros(sr)])
        y = audio.trim(x, sr, -45.0, 0.06)
        self.assertAlmostEqual(len(y) / sr, len(core) / sr + 0.12, delta=0.02)
        self.assertLess(np.argmax(np.abs(y) > 1e-4) / sr, 0.07)

    def test_join_places_the_pauses(self):
        td = tempfile.TemporaryDirectory()
        sr, paths = 24000, []
        for i in range(3):
            p = os.path.join(td.name, f"{i}.wav")
            sf.write(p, np.concatenate([np.zeros(sr // 2), speechlike(1.0, seed=i), np.zeros(sr // 2)]),
                     sr, subtype="PCM_16")
            paths.append(p)
        out = os.path.join(td.name, "j.wav")
        j = audio.join(paths, [0.3, 0.75, 1.0], out, lead_in_s=0.4, threshold_db=-45, pad_s=0.06)
        voiced = sum(len(audio.trim(sf.read(p, dtype="float32")[0], sr, -45, 0.06)) for p in paths) / sr
        self.assertAlmostEqual(j["duration_s"], voiced + 0.3 + 0.75 + 1.0 + 0.4, delta=0.01)
        self.assertEqual(sf.info(out).frames, j["samples"])

    def test_mismatched_rates_refused(self):
        td = tempfile.TemporaryDirectory()
        a, b = os.path.join(td.name, "a.wav"), os.path.join(td.name, "b.wav")
        sf.write(a, speechlike(1.0), 24000)
        sf.write(b, speechlike(1.0, sr=22050), 22050)
        with self.assertRaises(audio.EncodeError):
            audio.join([a, b], [0.3, 0.3], os.path.join(td.name, "j.wav"), lead_in_s=0,
                       threshold_db=-45, pad_s=0.06)


class Tags(unittest.TestCase):
    def test_round_trip_through_two_readers(self):
        td = tempfile.TemporaryDirectory()
        wav = os.path.join(td.name, "x.wav")
        sf.write(wav, speechlike(5.0), 24000)
        mp3 = os.path.join(td.name, "x.mp3")
        audio.normalise_encode(ffmpeg(), wav, mp3, A, td.name)
        want = tags.write(mp3, {"title": "Flow Matching = Diffusion? A 'Unified' View; Part #2 — ü",
                                "artist": "Yaron Lipman", "album": "Papers", "albumartist": "Papers",
                                "date": "2026-09-26", "genre": "Podcast",
                                "comment": "papercast 2026-09-26-k3v9x2qa"}, replaygain_db=-2.0,
                          peak=0.8)
        self.assertEqual(tags.mismatches(want, tags.read_mutagen(mp3)), [])
        self.assertEqual(tags.mismatches(want, tags.read_ffmpeg(ffmpeg(), mp3)), [])
        from mutagen.id3 import ID3
        self.assertEqual(str(ID3(mp3).getall("TXXX:REPLAYGAIN_TRACK_GAIN")[0].text[0]), "-2.00 dB")
        # still a playable stream after the tag rewrite
        self.assertEqual(audio.probe(ffmpeg(), mp3)["codec"], "mp3")

    def test_defaults_for_missing_fields(self):
        t = tags.normalise({"title": "", "artist": None, "date": "26/09/2026"})
        self.assertEqual((t["title"], t["artist"], t["album"], t["albumartist"]),
                         ("Untitled paper", "Unknown author", "Papers", "Papers"))
        self.assertRegex(t["date"], r"^\d{4}-\d{2}-\d{2}$")


if __name__ == "__main__":
    unittest.main()
