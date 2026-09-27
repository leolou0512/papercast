"""The Breeze GPU adapter (engines/breeze_worker.py) with its model replaced by
fake_breeze_backend.py: the protocol, the per-chunk length check and its retries, out-of-memory
handling, a whole episode through the GPU path, the narrator pinned to clip A's, and the
install guard (busy.py)."""
import ast
import fcntl
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest

import soundfile as sf

from helpers import HERE, SRC, Rig, status, voice_log
from papercast_voice import busy, config
from papercast_voice.engines import breeze_worker as bw
from papercast_voice.workers import EngineError, EngineOOM, Worker

FAKE = os.path.join(HERE, "fake_breeze_backend.py")
TEXT = ("The guided score equals the plain score, plus gamma times the difference between the "
        "two, and that is the whole trick.")


def breeze_spec(tmp: str, mode: str = "ok", **over) -> dict:
    spec = config.engine_spec(config._expand(config.DEFAULTS, tmp), "breeze")
    spec.update(python=sys.executable, backend=FAKE, load_timeout_s=30, chunk_timeout_s=30)
    spec["env"] = {"FAKE_BREEZE_MODE": mode, "FAKE_BREEZE_LOG": os.path.join(tmp, "breeze.log"),
                   "PYTHONPATH": SRC}
    spec.update(over)
    return spec


class LengthCheck(unittest.TestCase):
    def test_bounds(self):
        spec = config.DEFAULTS["engines"]["breeze"]
        self.assertIsNone(bw.length_problem(10 * 0.48, 10, spec, False))
        self.assertIn("too long", bw.length_problem(20.0, 10, spec, False))
        self.assertIn("too short", bw.length_problem(1.0, 10, spec, False))
        self.assertIn("frame limit", bw.length_problem(5.0, 10, spec, True))
        self.assertIn("no audio", bw.length_problem(0.0, 10, spec, False))
        # a heading of two words said quickly is fine; the same heading running on is not
        self.assertIsNone(bw.length_problem(0.2, 2, spec, False))
        self.assertIsNotNone(bw.length_problem(6.0, 2, spec, False))

    def test_oom_recognised(self):
        class OutOfMemoryError(Exception):
            pass
        self.assertTrue(bw.is_oom(OutOfMemoryError("x")))
        self.assertTrue(bw.is_oom(RuntimeError("CUDA error: out of memory")))
        self.assertTrue(bw.is_oom(RuntimeError("CUBLAS_STATUS_ALLOC_FAILED when calling")))
        self.assertFalse(bw.is_oom(RuntimeError("CUDA error: illegal memory access")))


class NarratorIsClipA(unittest.TestCase):
    """The configured narrator is the one Leo heard and picked: same description, seed, CFG and
    fast stages as samples/gen_breeze.py (read from its source, not copied by hand)."""

    def test_same_as_the_sample(self):
        src = open(os.path.join(SRC, "samples", "gen_breeze.py")).read()
        consts = {}
        for node in ast.parse(src).body:
            if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
                try:
                    consts[node.targets[0].id] = ast.literal_eval(node.value)
                except ValueError:
                    pass
        b = config.DEFAULTS["engines"]["breeze"]
        self.assertEqual(b["instruction"], consts["INSTRUCTION"])
        self.assertEqual(b["seed"], consts["SEED"])
        self.assertEqual(b["cfg_scale"], consts["CFG"])
        self.assertEqual(sorted(b["fast_stages"]), ["backbone_decode", "depth_decoder"])
        self.assertIn("--fast depth_decoder,backbone_decode", open(
            os.path.join(SRC, "samples", "README.md")).read())
        self.assertTrue(os.path.isfile(config.engine_spec(config.DEFAULTS, "breeze")["worker"]))


class WorkerProtocol(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory(prefix="pcv-breeze-")
        self.addCleanup(self.td.cleanup)
        self.tmp = self.td.name
        self.cancel = threading.Event()

    def worker(self, mode: str, **over) -> Worker:
        w = Worker(breeze_spec(self.tmp, mode, **over), threads=1, gpu_index=None,
                   cfg={"home": self.tmp}, tag="t")
        self.addCleanup(w.stop, 2)
        return w

    def calls(self):
        with open(os.path.join(self.tmp, "breeze.log")) as fh:
            return [json.loads(x) for x in fh]

    def synth(self, w: Worker, idx: int = 0):
        out = os.path.join(self.tmp, f"{idx:04d}.wav")
        return w.synth(idx, TEXT, out, self.cancel), out

    def test_good_chunk_one_attempt_with_the_fixed_seed(self):
        w = self.worker("ok")
        ready = w.start(self.cancel)
        self.assertEqual((ready["sample_rate"], ready["seed"]), (24000, 42))
        msg, out = self.synth(w)
        n = len(TEXT.split())
        self.assertEqual((msg["attempts"], msg["seed"]), (1, 42))
        self.assertNotIn("warning", msg)
        info = sf.info(out)
        self.assertEqual(info.samplerate, 24000)
        self.assertAlmostEqual(info.frames / 24000, n * 0.48, delta=0.05)
        self.assertAlmostEqual(msg["audio_s"], n * 0.48, delta=0.05)
        c = self.calls()
        self.assertEqual([x["seed"] for x in c], [42])
        self.assertEqual(c[0]["instruction"], config.DEFAULTS["engines"]["breeze"]["instruction"])
        # the same text again: the same seed again (every chunk, every episode)
        msg2, _ = self.synth(w, 1)
        self.assertEqual(msg2["seed"], 42)

    def test_runaway_chunk_is_retried_with_the_next_seed(self):
        for mode in ("runaway_first", "hit_limit_first", "short_first"):
            with self.subTest(mode):
                w = self.worker(mode)
                w.start(self.cancel)
                msg, out = self.synth(w)
                self.assertEqual((msg["attempts"], msg["seed"]), (2, 43))
                self.assertNotIn("warning", msg)
                self.assertIsNotNone(msg["tries"][0]["problem"])
                self.assertAlmostEqual(sf.info(out).frames / 24000, len(TEXT.split()) * 0.48,
                                       delta=0.05)
                w.stop(2)
                os.unlink(os.path.join(self.tmp, "breeze.log"))

    def test_no_good_attempt_keeps_the_closest_and_says_so(self):
        w = self.worker("always_long")
        w.start(self.cancel)
        msg, _out = self.synth(w)
        self.assertEqual(msg["attempts"], 3)
        self.assertEqual(msg["seed"], 42)                   # the least long of the three
        self.assertIn("too long", msg["warning"])
        self.assertEqual([x["seed"] for x in self.calls()], [42, 43, 44])

    def test_out_of_memory_mid_chunk_is_oom_and_ends_the_worker(self):
        w = self.worker("oom_synth")
        w.start(self.cancel)
        with self.assertRaises(EngineOOM):
            self.synth(w)
        w.proc.wait(timeout=10)
        self.assertNotEqual(w.proc.returncode, 0)

    def test_out_of_memory_at_load_is_oom(self):
        w = self.worker("oom_load")
        with self.assertRaises(EngineOOM):
            w.start(self.cancel)

    def test_other_cuda_error_is_fatal_not_oom(self):
        w = self.worker("cuda_error")
        w.start(self.cancel)
        with self.assertRaises(EngineError) as cm:
            self.synth(w)
        self.assertNotIsInstance(cm.exception, EngineOOM)
        w.proc.wait(timeout=10)


class BreezeEpisode(unittest.TestCase):
    """A whole job with gpu_engine = breeze (the configured spec, model faked), fake nvidia-smi."""

    def test_episode_through_the_gpu_path(self):
        rig = Rig(gpu=True)
        self.addCleanup(rig.cleanup)
        spec = breeze_spec(rig.tmp, "runaway_first", peak_mib=9696)
        spec["worker"] = os.path.join(SRC, "papercast_voice", "engines", "breeze_worker.py")
        rig.cfg["engines"]["breeze"] = spec
        rig.cfg["gpu_engine"] = "breeze"
        rig.write_cfg()
        rig.set_gpu(free=12000, util=0)
        info = json.loads(rig.cli("info").stdout)
        self.assertEqual(info["gpu"]["model"], "breeze-tts-2")
        self.assertEqual(info["gpu"]["need_mib"], 9696 + 1024)
        vdir = rig.job()
        rc = rig.run(vdir)
        self.assertEqual(rc, 0, voice_log(vdir)[-3000:])
        st = status(vdir)
        self.assertEqual(st["output"]["engine"], "gpu:breeze-tts-2")
        self.assertEqual(st["output"]["voice"], "described-narrator-a-seed42")
        self.assertEqual(st["need_mib"], 9696 + 1024)
        m = json.load(open(os.path.join(vdir, "metrics.json")))["runs"][-1]
        # every chunk's first take ran on, so every chunk was retried once and recorded
        self.assertEqual(len(m["retried_chunks"]), st["chunks_total"])
        self.assertTrue(all(r["seed"] == 43 and r["attempts"] == 2 for r in m["retried_chunks"]))
        # no chunk longer than the adapter's limit reached the engine
        with open(os.path.join(rig.tmp, "breeze.log")) as fh:
            texts = {json.loads(x)["text"] for x in fh}
        self.assertTrue(all(len(t.split()) <= spec["max_words"] for t in texts))

    def test_not_enough_memory_waits(self):
        rig = Rig(gpu=True)
        self.addCleanup(rig.cleanup)
        spec = breeze_spec(rig.tmp, "ok", peak_mib=9696)
        rig.cfg["engines"]["breeze"] = spec
        rig.cfg["gpu_engine"] = "breeze"
        rig.write_cfg()
        rig.set_gpu(free=10000, util=0)          # under 9696 + 1024
        vdir = rig.job()
        p = rig.start(vdir)
        from helpers import wait_for
        wait_for(lambda: status(vdir).get("wait_reason") == "memory", timeout=20)
        self.assertEqual(status(vdir)["phase"], "waiting-for-gpu")
        self.assertFalse(os.path.exists(os.path.join(rig.tmp, "breeze.log")))
        rig.set_gpu(free=12000, util=0)
        self.assertEqual(p.wait(timeout=60), 0, voice_log(vdir)[-3000:])
        self.assertEqual(status(vdir)["output"]["engine"], "gpu:breeze-tts-2")


class InstallGuard(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory(prefix="pcv-busy-")
        self.addCleanup(self.td.cleanup)
        self.state = os.path.join(self.td.name, "state")
        self.home = os.path.join(self.td.name, "home")
        os.makedirs(os.path.join(self.home, "run"))

    def job(self, name: str, phase: str, pid: int) -> None:
        d = os.path.join(self.state, name, "voice")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "status.json"), "w") as fh:
            json.dump({"phase": phase, "pid": pid}, fh)

    def cli(self) -> subprocess.CompletedProcess:
        return subprocess.run(["python3", os.path.join(SRC, "papercast_voice", "busy.py"),
                               self.state, self.home], capture_output=True, text=True, timeout=10)

    def test_idle_and_busy(self):
        os.makedirs(self.state)
        self.assertEqual(self.cli().returncode, 0)
        # done jobs, and a speaking job whose process is gone or is not a voice, are not busy
        self.job("2026-09-26-aaaaaaaa", "done", os.getpid())
        self.job("2026-09-26-bbbbbbbb", "speaking", 2 ** 22 + 7)
        other = subprocess.Popen(["sleep", "30"])
        self.addCleanup(other.kill)
        self.job("2026-09-26-cccccccc", "speaking", other.pid)
        self.assertEqual(self.cli().returncode, 0, self.cli().stdout)
        # a live voice process speaking is busy
        voice = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)",
                                  "papercast_voice", "run"])
        self.addCleanup(voice.kill)
        # waiting is not busy: a waiting job holds no engine and re-executes on the new code
        self.job("2026-09-26-dddddddd", "waiting-for-gpu", voice.pid)
        self.assertEqual(self.cli().returncode, 0, self.cli().stdout)
        self.job("2026-09-26-dddddddd", "speaking", voice.pid)
        r = self.cli()
        self.assertEqual(r.returncode, 1)
        self.assertIn("dddddddd", r.stdout)

    def test_held_locks_are_busy(self):
        os.makedirs(self.state)
        os.makedirs(os.path.join(self.home, "run", "slots"))
        for path in (os.path.join(self.home, "run", "voice-cpu.lock"),
                     os.path.join(self.home, "run", "slots", "bs1-gpu3.lock")):
            with self.subTest(path):
                fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
                self.assertEqual(busy.reasons(self.state, self.home), [])
                fcntl.flock(fd, fcntl.LOCK_EX)
                self.assertEqual(busy.reasons(self.state, self.home), [f"{path} is held"])
                os.close(fd)
                self.assertEqual(busy.reasons(self.state, self.home), [])


if __name__ == "__main__":
    unittest.main()
