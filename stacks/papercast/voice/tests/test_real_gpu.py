"""The installed GPU voice (Breeze TTS 2), for real, on a short script, launched the way the runner
launches it (its launch.sh, nice 10, the cleaned environment), sharing the runner's GPU slot
(state/voice-gpu.lock) so it never runs beside a real episode.

    PAPERCAST_VOICE_REAL_GPU=1 venv/bin/python -m unittest discover -s tests -p test_real_gpu.py

Skipped unless PAPERCAST_VOICE_REAL_GPU=1, and skipped (not waited out) when nvidia-smi shows less
free memory than the voice's admission threshold or the card busy: another user's job comes
first. The audio is transcribed with Whisper on the CPU and compared with the script.
"""
import json
import os
import re
import subprocess
import tempfile
import time
import unittest

from helpers import SCRIPT, TAGS, read, sha256, status, voice_log
from papercast_voice import audio, gpu, tags
from test_real import LAUNCHER, WHISPER_DIR, WHISPER_PY, wer, words

CMD = os.environ.get("PAPERCAST_VOICE_CMD", "/home/leo/papercast/voice/bin/papercast-voice")
HOME = os.environ.get("PAPERCAST_VOICE_HOME", "/home/leo/papercast/voice")
GPU_LOCK = "/home/leo/papercast/state/voice-gpu.lock"
PAPER = "2026-09-26-gpuaaaaa"


@unittest.skipUnless(os.environ.get("PAPERCAST_VOICE_REAL_GPU") == "1",
                     "set PAPERCAST_VOICE_REAL_GPU=1")
class RealBreeze(unittest.TestCase):
    def test_short_episode_on_the_gpu(self):
        info = json.loads(subprocess.run([CMD, "info"], capture_output=True, text=True,
                                         timeout=10).stdout)
        self.assertTrue(info["installed"] and info["gpu"] and info["gpu"]["installed"], info)
        need = info["gpu"]["need_mib"]
        r = gpu.read()
        if not r.ok or r.free_mib < need or (r.util_pct or 0) > 20:
            self.skipTest(f"GPU not free enough now ({r.free_mib} MiB free, {r.util_pct}% busy; "
                          f"need {need} MiB): not squeezing anyone")
        td = tempfile.TemporaryDirectory(prefix="pcv-realgpu-")
        self.addCleanup(td.cleanup)
        vdir = os.path.join(td.name, "state", PAPER, "voice")
        os.makedirs(vdir, mode=0o700)
        with open(os.path.join(vdir, "script.md"), "w") as fh:
            fh.write(SCRIPT)
        with open(os.path.join(vdir, "job.json"), "w") as fh:
            json.dump({"interface": "1.0", "paper_id": PAPER, "script": "script.md",
                       "output_dir": "out", "engine": "auto", "tags": TAGS}, fh)
        # the installed config, plus the runner's GPU slot instead of this temp tree's own
        cur = json.load(open(os.path.join(HOME, "voice.json")))
        if os.path.isdir(os.path.dirname(GPU_LOCK)):
            cur["gpu_lock"] = GPU_LOCK
        cfg = os.path.join(td.name, "voice.json")
        with open(cfg, "w") as fh:
            json.dump(cur, fh)
        env = {"HOME": os.environ["HOME"], "USER": "leo", "LANG": "C.UTF-8",
               "PATH": "/home/leo/.local/bin:/usr/local/bin:/usr/bin:/bin",
               "PAPERCAST_VOICE_CONFIG": cfg}
        rc_file = os.path.join(vdir, "rc")
        t0 = time.time()
        with open(os.path.join(vdir, "voice.log"), "ab") as log:
            p = subprocess.Popen(["nice", "-n", "10", "/bin/sh", LAUNCHER, rc_file, "", "--", CMD,
                                  "run", vdir], cwd=vdir, env=env, stdin=subprocess.DEVNULL,
                                 stdout=log, stderr=log, start_new_session=True)
        p.wait(timeout=1800)
        took = time.time() - t0
        self.assertEqual(read(rc_file).strip(), "0", voice_log(vdir)[-3000:])
        st = status(vdir)
        out = st["output"]
        self.assertEqual((out["engine"], out["voice"], out["format"]),
                         ("gpu:breeze-tts-2", "described-narrator-a-seed42", "mp3"))
        path = os.path.join(vdir, out["path"])
        self.assertEqual(sha256(path), out["sha256"])
        ff = os.path.join(HOME, "bin", "ffmpeg")
        m = audio.measure(ff, path, 44100)
        self.assertLess(abs(m["lufs"] + 16.0), 1.0, m)
        self.assertLessEqual(m["true_peak_db"], -1.0, m)
        want = tags.normalise(TAGS)
        self.assertEqual(tags.mismatches(want, tags.read_mutagen(path)), [])
        n_words = len(SCRIPT.replace("#", " ").split())
        self.assertGreater(m["duration_s"], n_words / 220 * 60)
        self.assertLess(m["duration_s"], n_words / 60 * 60)
        mt = json.load(open(os.path.join(vdir, "metrics.json")))["runs"][-1]
        print(f"\nreal Breeze: {n_words} words -> {m['duration_s']:.1f} s audio in {took:.1f} s "
              f"(waited {mt['wait_s']} s, encoded in {mt.get('encode_s')} s); {m['lufs']:.2f} LUFS, "
              f"true peak {m['true_peak_db']} dBTP; retried chunks: {mt.get('retried_chunks', [])}",
              flush=True)
        if os.path.exists(WHISPER_PY) and os.path.isdir(WHISPER_DIR):
            code = ("import sys,whisper,numpy as np,subprocess;"
                    "raw=subprocess.run([sys.argv[1],'-nostdin','-i',sys.argv[2],'-f','f32le','-ac','1','-ar','16000','-'],capture_output=True).stdout;"
                    "x=np.frombuffer(raw,dtype='<f4');"
                    "m=whisper.load_model('small.en',device='cpu',download_root=sys.argv[3]);"
                    "print(m.transcribe(x,language='en',fp16=False)['text'])")
            r = subprocess.run(["nice", "-n", "10", WHISPER_PY, "-c", code, ff, path, WHISPER_DIR],
                               capture_output=True, text=True, timeout=900,
                               env={**env, "CUDA_VISIBLE_DEVICES": "", "OMP_NUM_THREADS": "4"})
            self.assertEqual(r.returncode, 0, r.stderr[-1000:])
            e = wer(words(SCRIPT), words(r.stdout))
            print(f"Whisper small.en word error rate against the script: {e:.3f}\n{r.stdout}",
                  flush=True)
            self.assertLess(e, 0.15, r.stdout)


if __name__ == "__main__":
    unittest.main()
