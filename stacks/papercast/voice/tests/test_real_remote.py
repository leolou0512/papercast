"""The installed voice on a remote GPU (bs1), for real: a short script launched the way the runner
launches it (its launch.sh, nice 10, the cleaned environment), with stibnite's own card switched
off for this job so it can only go to bs1. It uses the real slot locks (run/slots/), so it never
shares a card with a real episode, but a line of its own, so it does not wait behind them.

    PAPERCAST_VOICE_REAL_REMOTE=1 venv/bin/python -m unittest discover -s tests -p test_real_remote.py

Skipped unless PAPERCAST_VOICE_REAL_REMOTE=1, and skipped (not waited out) when no bs1 GPU is
free now. Checks the tagged MP3 back on stibnite, the Whisper word error rate, that every chunk
was voiced on bs1, and that nothing of the job is left on bs1: no file, no engine process.
"""
import json
import os
import subprocess
import tempfile
import time
import unittest

from helpers import SCRIPT, TAGS, read, sha256, status, voice_log
from papercast_voice import audio, config, hosts, tags
from test_real import LAUNCHER, WHISPER_DIR, WHISPER_PY, wer, words

CMD = os.environ.get("PAPERCAST_VOICE_CMD", "/home/leo/papercast/voice/bin/papercast-voice")
HOME = os.environ.get("PAPERCAST_VOICE_HOME", "/home/leo/papercast/voice")
PAPER = "2026-09-27-bsoneaaa"


@unittest.skipUnless(os.environ.get("PAPERCAST_VOICE_REAL_REMOTE") == "1",
                     "set PAPERCAST_VOICE_REAL_REMOTE=1")
class RealRemote(unittest.TestCase):
    def test_short_episode_on_bs1(self):
        os.environ.setdefault("PAPERCAST_VOICE_HOME", HOME)
        cfg0 = config.load(os.path.join(HOME, "voice.json"))
        slots = [s for s in hosts.build(cfg0, "/nonexistent", 1) if s.remote]
        self.assertTrue(slots, "no remote slot configured")
        host = slots[0].host
        readings = host.read_remote()
        free = [s for s in slots if readings.get(s.gpu) and readings[s.gpu].ok
                and not readings[s.gpu].apps and readings[s.gpu].free_mib >= s.need_mib]
        if not free:
            self.skipTest(f"no {host.name} GPU free now: not squeezing anyone")
        before = set(host.run(["ls", "-A", host.cfg["home"]], 20).split())
        td = tempfile.TemporaryDirectory(prefix="pcv-realremote-")
        self.addCleanup(td.cleanup)
        vdir = os.path.join(td.name, "state", PAPER, "voice")
        os.makedirs(vdir, mode=0o700)
        with open(os.path.join(vdir, "script.md"), "w") as fh:
            fh.write(SCRIPT)
        with open(os.path.join(vdir, "job.json"), "w") as fh:
            json.dump({"interface": "1.0", "paper_id": PAPER, "script": "script.md",
                       "output_dir": "out", "engine": "auto", "tags": TAGS}, fh)
        cur = json.load(open(os.path.join(HOME, "voice.json")))
        cur["hosts"] = {"stibnite": {"enabled": False}}
        cur["queue_dir"] = os.path.join(td.name, "queue")
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
        self.assertEqual((out["engine"], out["format"]), ("gpu:breeze-tts-2", "mp3"))
        path = os.path.join(vdir, out["path"])
        self.assertEqual(sha256(path), out["sha256"])
        ff = os.path.join(HOME, "bin", "ffmpeg")
        m = audio.measure(ff, path, 44100)
        self.assertLess(abs(m["lufs"] + 16.0), 1.0, m)
        self.assertLessEqual(m["true_peak_db"], -1.0, m)
        self.assertEqual(tags.mismatches(tags.normalise(TAGS), tags.read_mutagen(path)), [])
        mt = json.load(open(os.path.join(vdir, "metrics.json")))["runs"][-1]
        by = mt.get("chunks_by_slot") or {}
        self.assertTrue(by and all(k.startswith(host.name + " GPU") for k in by), by)
        self.assertEqual(sum(by.values()), st["chunks_total"])
        # nothing of the job left on the remote host: no new file, our engine gone
        after = set(host.run(["ls", "-A", host.cfg["home"]], 20).split())
        self.assertEqual(after, before)
        log_text = voice_log(vdir)
        rpid = int(log_text.split("remote pid ")[1].split(")")[0])
        alive = host.run(["sh", "-c", f"test -d /proc/{rpid} && echo alive || echo gone"], 20)
        self.assertEqual(alive.strip(), "gone")
        print(f"\nreal remote: {len(SCRIPT.split())} words -> {m['duration_s']:.1f} s audio in "
              f"{took:.1f} s on {', '.join(by)} (engine load "
              f"{log_text.split('engine ready in ')[1].split(' s')[0]} s); {m['lufs']:.2f} LUFS, "
              f"true peak {m['true_peak_db']} dBTP", flush=True)
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
