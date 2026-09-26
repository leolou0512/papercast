"""The installed voice, for real (Kokoro on the CPU), launched the way the runner launches it:
its own launch.sh, setsid, nice 10, the cleaned environment of INTERFACE §5.5 / §10.1.

    PAPERCAST_VOICE_REAL=1 venv/bin/python -m unittest discover -s tests -p test_real.py

Skipped unless PAPERCAST_VOICE_REAL=1 (it uses a few CPU cores for about a minute). When the
Whisper model from the sample step is present, the audio is also transcribed and compared with
the script, so "it produced a file" is not mistaken for "it said the words".
"""
import json
import os
import re
import subprocess
import tempfile
import time
import unittest

from helpers import SCRIPT, TAGS, read, sha256, status, voice_log
from papercast_voice import audio, tags

CMD = os.environ.get("PAPERCAST_VOICE_CMD", "/home/leo/papercast/voice/bin/papercast-voice")
HOME = os.environ.get("PAPERCAST_VOICE_HOME", "/home/leo/papercast/voice")
LAUNCHER = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                        "runner", "papercast_runner", "launch.sh")
WHISPER_PY = "/home/leo/papercast-voice-cache/venvs/voxtral/bin/python"
WHISPER_DIR = "/home/leo/papercast-voice-cache/whisper"
PAPER = "2026-09-26-reaaaaal"


def words(s):
    return re.findall(r"[a-z]+", s.lower())


def wer(ref, hyp):
    d = list(range(len(hyp) + 1))
    for i in range(1, len(ref) + 1):
        prev, d[0] = d[0], i
        for j in range(1, len(hyp) + 1):
            cur = min(d[j] + 1, d[j - 1] + 1, prev + (ref[i - 1] != hyp[j - 1]))
            prev, d[j] = d[j], cur
    return d[-1] / len(ref)


@unittest.skipUnless(os.environ.get("PAPERCAST_VOICE_REAL") == "1", "set PAPERCAST_VOICE_REAL=1")
class RealKokoro(unittest.TestCase):
    def test_short_episode_through_the_runners_launcher(self):
        self.assertTrue(os.access(CMD, os.X_OK), f"{CMD} not installed (run install.sh)")
        if not os.path.exists(LAUNCHER):
            self.skipTest("runner's launch.sh not in this checkout")
        td = tempfile.TemporaryDirectory(prefix="pcv-real-")
        self.addCleanup(td.cleanup)
        vdir = os.path.join(td.name, "state", PAPER, "voice")
        os.makedirs(vdir, mode=0o700)
        with open(os.path.join(vdir, "script.md"), "w") as fh:
            fh.write(SCRIPT)
        with open(os.path.join(vdir, "job.json"), "w") as fh:
            json.dump({"interface": "1.0", "paper_id": PAPER, "script": "script.md",
                       "output_dir": "out", "engine": "cpu", "tags": TAGS}, fh)
        env = {"HOME": os.environ["HOME"], "USER": "leo", "LANG": "C.UTF-8",
               "PATH": "/home/leo/.local/bin:/usr/local/bin:/usr/bin:/bin"}
        rc_file = os.path.join(vdir, "rc")
        t0 = time.time()
        with open(os.path.join(vdir, "voice.log"), "ab") as log:
            p = subprocess.Popen(["nice", "-n", "10", "/bin/sh", LAUNCHER, rc_file, "", "--", CMD,
                                  "run", vdir], cwd=vdir, env=env, stdin=subprocess.DEVNULL,
                                 stdout=log, stderr=log, start_new_session=True)
        p.wait(timeout=600)
        took = time.time() - t0
        self.assertEqual(read(rc_file).strip(), "0", voice_log(vdir)[-3000:])
        st = status(vdir)
        self.assertEqual(st["phase"], "done")
        out = st["output"]
        self.assertEqual((out["engine"], out["voice"], out["format"]), ("cpu:kokoro", "af_heart", "mp3"))
        path = os.path.join(vdir, out["path"])
        self.assertEqual(sha256(path), out["sha256"])
        nice = re.search(r"\(pid \d+, nice (\d+)\)", voice_log(vdir))
        self.assertTrue(nice and int(nice.group(1)) >= 10, voice_log(vdir)[:300])
        ff = os.path.join(HOME, "bin", "ffmpeg")
        m = audio.measure(ff, path, 44100)
        self.assertLess(abs(m["lufs"] + 16.0), 1.0, m)
        self.assertLessEqual(m["true_peak_db"], -1.0, m)
        want = tags.normalise(TAGS)
        self.assertEqual(tags.mismatches(want, tags.read_mutagen(path)), [])
        self.assertEqual(tags.mismatches(want, tags.read_ffmpeg(ff, path)), [])
        n_words = len(SCRIPT.replace("#", " ").split())
        self.assertGreater(m["duration_s"], n_words / 220 * 60)      # not truncated
        self.assertLess(m["duration_s"], n_words / 110 * 60)         # not padded or looping
        print(f"\nreal Kokoro: {n_words} words -> {m['duration_s']:.1f} s audio in {took:.1f} s; "
              f"{m['lufs']:.2f} LUFS, true peak {m['true_peak_db']} dBTP", flush=True)
        if os.path.exists(WHISPER_PY) and os.path.isdir(WHISPER_DIR):
            code = ("import sys,whisper,soxr,numpy as np,subprocess;"
                    "raw=subprocess.run([sys.argv[1],'-nostdin','-i',sys.argv[2],'-f','f32le','-ac','1','-ar','16000','-'],capture_output=True).stdout;"
                    "x=np.frombuffer(raw,dtype='<f4');"
                    "m=whisper.load_model('small.en',device='cpu',download_root=sys.argv[3]);"
                    "print(m.transcribe(x,language='en',fp16=False)['text'])")
            r = subprocess.run(["nice", "-n", "10", WHISPER_PY, "-c", code, ff, path, WHISPER_DIR],
                               capture_output=True, text=True, timeout=600,
                               env={**env, "CUDA_VISIBLE_DEVICES": "", "OMP_NUM_THREADS": "4"})
            self.assertEqual(r.returncode, 0, r.stderr[-1000:])
            e = wer(words(SCRIPT), words(r.stdout))
            print(f"Whisper small.en word error rate against the script: {e:.3f}", flush=True)
            self.assertLess(e, 0.15, r.stdout)


if __name__ == "__main__":
    unittest.main()
