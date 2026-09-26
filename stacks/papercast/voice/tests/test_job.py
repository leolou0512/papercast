"""Whole jobs, run as subprocesses the way the runner runs them, with the fake engine and the
fake nvidia-smi: the waiting rule, Use CPU voice, cancel, OOM, yielding, resume, the output."""
import json
import os
import signal
import subprocess
import time
import unittest

from helpers import PAPER, TAGS, Rig, gone, read, sha256, status, voice_log, wait_for
from papercast_voice import audio, tags, textprep
from papercast_voice.locks import FileLock

INTERFACE_FIELDS = {"interface", "paper_id", "phase", "engine", "need_mib", "free_mib",
                    "chunks_done", "chunks_total", "audio_s", "eta_s", "pid", "since",
                    "updated_at", "error", "output"}


class Base(unittest.TestCase):
    gpu = True

    def setUp(self):
        self.rig = Rig(gpu=self.gpu)
        self.addCleanup(self.rig.cleanup)

    def assertDone(self, vdir, engine):
        st = status(vdir)
        self.assertEqual(st.get("phase"), "done", voice_log(vdir)[-3000:])
        self.assertTrue(INTERFACE_FIELDS <= set(st), INTERFACE_FIELDS - set(st))
        out = st["output"]
        self.assertEqual(out["path"], "out/episode.mp3")
        self.assertEqual(out["engine"], engine)
        path = os.path.join(vdir, out["path"])
        self.assertEqual(out["sha256"], sha256(path))
        return st

    def texts(self, engine=None):
        return [c["text"] for c in self.rig.fake_calls() if engine is None or c["engine"] == engine]


class CpuPath(Base):
    gpu = False

    def test_episode_is_complete_tagged_and_loudness_normalised(self):
        vdir = self.rig.job()
        rc = self.rig.run(vdir)
        self.assertEqual(rc, 0, voice_log(vdir)[-3000:])
        st = self.assertDone(vdir, "cpu:kokoro")
        self.assertEqual(st["chunks_done"], st["chunks_total"])
        path = os.path.join(vdir, "out", "episode.mp3")
        ff = self.rig.cfg["ffmpeg"]
        # tags, read back by two independent readers
        want = tags.normalise(TAGS)
        for reader in (tags.read_mutagen(path), tags.read_ffmpeg(ff, path)):
            self.assertEqual(tags.mismatches(want, reader), [])
        self.assertEqual(tags.read_mutagen(path)["id3_version"], "2.4")
        # loudness and peak, measured on the decoded file
        m = audio.measure(ff, path, 44100)
        self.assertLess(abs(m["lufs"] - (-16.0)), 1.0, m)
        self.assertLessEqual(m["true_peak_db"], -1.0, m)
        self.assertLess(abs(m["lufs"] - m["lufs_ffmpeg"]), 0.5, m)
        self.assertEqual((m["codec"], m["channels"], m["sample_rate"], m["bitrate_kbps"]),
                         ("mp3", "mono", 44100, 96))
        self.assertAlmostEqual(m["duration_s"], st["output"]["duration_s"], delta=0.2)
        # every text the engine saw passes the guard; chunk audio removed after done
        seen = self.texts()
        self.assertEqual(len(seen), st["chunks_total"])
        for t in seen:
            self.assertEqual(textprep.problems(t), [], t)
        wavs = [f for _d, _s, fs in os.walk(os.path.join(vdir, "chunks")) for f in fs if f.endswith(".wav")]
        self.assertEqual(wavs, [])
        self.assertFalse(os.path.exists(os.path.join(vdir, "work", "joined.wav")))
        metrics = json.loads(read(os.path.join(vdir, "metrics.json")))
        self.assertEqual(metrics["runs"][-1]["phase"], "done")

    def test_bad_script_never_reaches_the_engine(self):
        for script in ("It scored 9 points out of ten.", r"The loss is \frac{a}{b}.",
                       "Set α to one.", "- a list item\n"):
            with self.subTest(script):
                vdir = self.rig.job(script=script)
                rc = self.rig.run(vdir)
                self.assertEqual(rc, 2)
                st = status(vdir)
                self.assertEqual((st["phase"], st["error"]["code"]), ("failed", "script_invalid"))
                self.assertEqual(self.rig.fake_calls(), [])

    def test_rerun_after_done_does_nothing(self):
        vdir = self.rig.job()
        self.assertEqual(self.rig.run(vdir), 0)
        n = len(self.rig.fake_calls())
        sha = status(vdir)["output"]["sha256"]
        self.assertEqual(self.rig.run(vdir), 0)
        self.assertEqual(len(self.rig.fake_calls()), n)
        self.assertEqual(status(vdir)["output"]["sha256"], sha)

    def test_hostile_tags_are_cleaned_not_trusted(self):
        vdir = self.rig.job(tags={**TAGS, "title": "Evil\x00Title\n\twith ../../ and ; = #" + "x" * 900,
                                  "artist": "", "date": "yesterday"})
        self.assertEqual(self.rig.run(vdir), 0, voice_log(vdir)[-2000:])
        t = status(vdir)["output"]["tags"]
        self.assertNotIn("\x00", t["title"])
        self.assertLessEqual(len(t["title"]), 400)
        self.assertEqual(t["artist"], "Unknown author")
        self.assertRegex(t["date"], r"^\d{4}-\d{2}-\d{2}$")
        got = tags.read_ffmpeg(self.rig.cfg["ffmpeg"], os.path.join(vdir, "out", "episode.mp3"))
        self.assertEqual(got["title"], t["title"])

    def test_one_process_per_directory(self):
        vdir = self.rig.job()
        lock = FileLock(os.path.join(vdir, ".voice.lock"))
        self.assertTrue(lock.try_acquire())
        try:
            self.assertEqual(self.rig.run(vdir), 5)
            self.assertEqual(status(vdir), {})          # status.json never touched
        finally:
            lock.release()

    def test_bad_job_json(self):
        vdir = self.rig.job()
        with open(os.path.join(vdir, "job.json"), "w") as fh:
            json.dump({"interface": "1.0", "paper_id": "../../etc", "script": "script.md",
                       "output_dir": "out", "engine": "auto", "tags": {}}, fh)
        self.assertEqual(self.rig.run(vdir), 3)
        self.assertEqual(status(vdir)["error"]["code"], "engine_failed")

    def test_resume_after_kill_skips_finished_chunks(self):
        self.rig.engine_env("kokoro", FAKE_DELAY_S=0.4)
        self.rig.cfg["cpu"]["workers"] = 1
        self.rig.write_cfg()
        vdir = self.rig.job()
        p = self.rig.start(vdir)
        wait_for(lambda: status(vdir).get("chunks_done", 0) >= 3, timeout=60)
        worker = self.rig.pid("cpu")
        os.kill(p.pid, signal.SIGKILL)          # the job alone: its engine must not outlive it
        p.wait()
        done_before = status(vdir)["chunks_done"]
        wait_for(lambda: gone(worker), timeout=10)
        first = [c["idx"] for c in self.rig.fake_calls()]
        self.rig.engine_env("kokoro", FAKE_DELAY_S=0.02)
        self.assertEqual(self.rig.run(vdir), 0, voice_log(vdir)[-2000:])
        self.assertDone(vdir, "cpu:kokoro")
        second = [c["idx"] for c in self.rig.fake_calls()][len(first):]
        self.assertGreaterEqual(done_before, 3)
        finished_first = sorted(first)[:done_before]
        self.assertFalse(set(finished_first) & set(second), (first, second))
        self.assertIn("resuming", voice_log(vdir))

    def test_failed_final_check_keeps_chunks_and_retry_only_encodes(self):
        # a peak limit no encoder can meet: the final check must refuse the file
        self.rig.cfg["audio"] = {"true_peak_db": -30.0}
        self.rig.write_cfg()
        vdir = self.rig.job()
        self.assertEqual(self.rig.run(vdir), 3, voice_log(vdir)[-2000:])
        st = status(vdir)
        self.assertEqual((st["phase"], st["error"]["code"]), ("failed", "encode_failed"))
        self.assertIn("true peak", st["error"]["message"])
        self.assertFalse(os.path.exists(os.path.join(vdir, "out", "episode.mp3")))
        n = len(self.rig.fake_calls())
        del self.rig.cfg["audio"]
        self.rig.write_cfg()
        self.assertEqual(self.rig.run(vdir), 0, voice_log(vdir)[-2000:])
        self.assertDone(vdir, "cpu:kokoro")
        self.assertEqual(len(self.rig.fake_calls()), n)      # nothing voiced again

    def test_engine_dies_with_a_killed_job(self):
        # mid-chunk for 60 s, so only the parent-death signal can stop it in time
        self.rig.engine_env("kokoro", FAKE_DELAY_S=60)
        vdir = self.rig.job()
        p = self.rig.start(vdir)
        wait_for(lambda: len(self.rig.fake_calls()) >= 2, timeout=30)
        workers = {c["pid"] for c in self.rig.fake_calls()}
        os.kill(p.pid, signal.SIGKILL)
        p.wait()
        for w in workers:
            wait_for(lambda: gone(w), timeout=10)

    def test_engine_boundary_guard_is_last(self):
        from papercast_voice.workers import Worker
        import threading
        w = Worker({"name": "x", "python": "/bin/false", "worker": "/bin/false"}, threads=1,
                   gpu_index=None, cfg={"home": self.rig.home}, tag="t")
        with self.assertRaises(textprep.ScriptInvalid):
            w.synth(0, "It costs 5 dollars.", "/tmp/never.wav", threading.Event())


class GpuPath(Base):
    def test_waits_while_memory_short_then_speaks(self):
        self.rig.set_gpu(free=3100, util=0)
        vdir = self.rig.job()
        p = self.rig.start(vdir)
        st = wait_for(lambda: (s := status(vdir)).get("phase") == "waiting-for-gpu" and
                      s.get("free_mib") == 3100 and s, timeout=20)
        self.assertEqual(st["need_mib"], 9024)          # measured peak 8000 + headroom 1024
        self.assertEqual(st["wait_reason"], "memory")
        self.assertEqual(st["wait_text"], "waiting for GPU: 3.0 GB free, needs 8.8 GB")
        time.sleep(1.5)
        self.assertEqual(self.rig.fake_calls(), [])
        self.assertFalse(os.path.exists(os.path.join(self.rig.tmp, "pid-gpu")))  # never started
        self.rig.set_gpu(free=12000, util=0)
        self.assertEqual(p.wait(timeout=60), 0, voice_log(vdir)[-3000:])
        self.assertDone(vdir, "gpu:fake")
        self.assertTrue(all(c["engine"] == "fakegpu" for c in self.rig.fake_calls()))

    def test_busy_card_waits(self):
        self.rig.set_gpu(free=15000, util=70)
        vdir = self.rig.job()
        p = self.rig.start(vdir)
        wait_for(lambda: status(vdir).get("wait_reason") == "busy", timeout=20)
        time.sleep(1.0)
        self.assertEqual(self.rig.fake_calls(), [])
        self.rig.set_gpu(free=15000, util=5)
        self.assertEqual(p.wait(timeout=60), 0)

    def test_unreadable_gpu_waits_without_crashing(self):
        self.rig.set_gpu(fail=True)
        vdir = self.rig.job()
        p = self.rig.start(vdir)
        wait_for(lambda: status(vdir).get("wait_reason") == "nvidia_smi", timeout=20)
        time.sleep(1.0)
        self.assertIsNone(p.poll())
        self.rig.set_gpu(free=12000, util=0)
        self.assertEqual(p.wait(timeout=60), 0)

    def test_one_gpu_job_at_a_time(self):
        lock = FileLock(os.path.join(self.rig.state, "voice-gpu.lock"))   # INTERFACE §10.4 path
        self.assertTrue(lock.try_acquire())
        vdir = self.rig.job()
        p = self.rig.start(vdir)
        try:
            wait_for(lambda: status(vdir).get("wait_reason") == "slot", timeout=20)
            time.sleep(1.0)
            self.assertEqual(self.rig.fake_calls(), [])
        finally:
            lock.release()
        self.assertEqual(p.wait(timeout=60), 0)

    def test_use_cpu_while_waiting(self):
        self.rig.set_gpu(free=1000, util=0)
        vdir = self.rig.job()
        p = self.rig.start(vdir)
        wait_for(lambda: status(vdir).get("phase") == "waiting-for-gpu", timeout=20)
        t0 = time.time()
        open(os.path.join(vdir, "use-cpu"), "w").close()
        wait_for(lambda: status(vdir).get("engine") == "cpu:kokoro", timeout=20)
        self.assertLess(time.time() - t0, 10.5)          # INTERFACE §10.4: within 10 s
        self.assertEqual(p.wait(timeout=60), 0)
        self.assertDone(vdir, "cpu:kokoro")
        self.assertTrue(all(c["engine"] == "kokoro" for c in self.rig.fake_calls()))

    def test_engine_cpu_in_job_skips_the_gpu(self):
        vdir = self.rig.job(engine="cpu")
        self.assertEqual(self.rig.run(vdir), 0)
        self.assertDone(vdir, "cpu:kokoro")
        self.assertFalse(any("query-gpu" in c for c in self.rig.nvsmi_calls()))

    def test_cancel_while_waiting(self):
        self.rig.set_gpu(free=1000, util=0)
        vdir = self.rig.job()
        p = self.rig.start(vdir)
        wait_for(lambda: status(vdir).get("phase") == "waiting-for-gpu", timeout=20)
        open(os.path.join(vdir, "cancel"), "w").close()
        self.assertEqual(p.wait(timeout=20), 4)
        st = status(vdir)
        self.assertEqual((st["phase"], st["error"]["code"]), ("failed", "cancelled"))

    def test_cancel_while_speaking_stops_the_engine(self):
        self.rig.engine_env("fakegpu", FAKE_DELAY_S=0.5)
        vdir = self.rig.job()
        p = self.rig.start(vdir)
        wait_for(lambda: status(vdir).get("chunks_done", 0) >= 1, timeout=30)
        worker = self.rig.pid("gpu")
        open(os.path.join(vdir, "cancel"), "w").close()
        self.assertEqual(p.wait(timeout=20), 4)
        wait_for(lambda: gone(worker), timeout=10)

    def test_oom_returns_to_waiting_and_keeps_chunks(self):
        self.rig.engine_env("fakegpu", FAKE_OOM_AT="2,4")
        vdir = self.rig.job()
        self.assertEqual(self.rig.run(vdir), 0, voice_log(vdir)[-3000:])
        self.assertDone(vdir, "gpu:fake")
        idx = [c["idx"] for c in self.rig.fake_calls()]
        # 0,1,2(oom) | 2,3,4(oom) | 4,5,...: nothing before an OOM is voiced twice
        self.assertEqual(idx.count(0), 1)
        self.assertEqual(idx.count(1), 1)
        self.assertEqual(idx.count(2), 2)
        self.assertEqual(idx.count(4), 2)
        self.assertIn("out of memory (2 of 3)", voice_log(vdir))

    def test_third_oom_fails(self):
        self.rig.engine_env("fakegpu", FAKE_OOM_AT="1,2,3")
        vdir = self.rig.job()
        self.assertEqual(self.rig.run(vdir), 3)
        st = status(vdir)
        self.assertEqual((st["phase"], st["error"]["code"]), ("failed", "gpu_oom"))

    def test_gives_the_gpu_back_to_someone_elses_job(self):
        other = subprocess.Popen(["sleep", "120"])      # stands in for another user's job
        self.addCleanup(other.kill)
        self.rig.engine_env("fakegpu", FAKE_DELAY_S=0.3)
        vdir = self.rig.job()
        p = self.rig.start(vdir)
        wait_for(lambda: status(vdir).get("chunks_done", 0) >= 1, timeout=30)
        # another compute process starts working hard on the card
        self.rig.set_gpu(free=5000, util=100, apps=[[other.pid, 6000]],
                         pmon=[[other.pid, "C", 95]])
        wait_for(lambda: status(vdir).get("phase") == "waiting-for-gpu", timeout=30)
        self.assertIn("gave the GPU back", status(vdir).get("note") or "")
        worker = self.rig.pid("gpu")
        wait_for(lambda: gone(worker), timeout=10)   # our engine stopped
        n = len(self.rig.fake_calls())
        time.sleep(1.0)
        self.assertEqual(len(self.rig.fake_calls()), n)      # nothing voiced while they work
        self.assertIsNone(other.poll())                       # their process untouched
        self.rig.set_gpu(free=12000, util=0)
        self.assertEqual(p.wait(timeout=60), 0, voice_log(vdir)[-3000:])
        self.assertDone(vdir, "gpu:fake")
        idx = [c["idx"] for c in self.rig.fake_calls()]
        self.assertEqual(len(idx), len(set(idx)))            # no chunk voiced twice
        self.assertIsNone(other.poll())

    def test_own_engine_on_the_card_is_not_a_reason_to_yield(self):
        self.rig.engine_env("fakegpu", FAKE_DELAY_S=0.3)
        vdir = self.rig.job()
        p = self.rig.start(vdir)
        pidfile = os.path.join(self.rig.tmp, "pid-gpu")
        wait_for(lambda: os.path.exists(pidfile) and read(pidfile).strip(), timeout=30)
        me = self.rig.pid("gpu")
        self.rig.set_gpu(free=3000, util=100, apps=[[me, 9000]], pmon=[[me, "C", 99]])
        self.assertEqual(p.wait(timeout=60), 0)
        self.assertNotIn("yielded", voice_log(vdir))

    def test_no_gpu_engine_means_cpu_at_once(self):
        self.rig.cfg["gpu_engine"] = None
        self.rig.write_cfg()
        vdir = self.rig.job()
        self.assertEqual(self.rig.run(vdir), 0)
        self.assertDone(vdir, "cpu:kokoro")
        self.assertIn("no GPU voice is installed", status(vdir)["note"])

    def test_gpu_engine_without_measured_peak_fails_loudly(self):
        self.rig.cfg["engines"]["fakegpu"]["peak_mib"] = None
        self.rig.write_cfg()
        vdir = self.rig.job()
        self.assertEqual(self.rig.run(vdir), 3)
        self.assertIn("no measured peak", status(vdir)["error"]["message"])


class Info(Base):
    def test_info_is_fast_json_and_never_touches_the_gpu(self):
        t0 = time.time()
        r = self.rig.cli("info")
        dt = time.time() - t0
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertLess(dt, 2.0)
        i = json.loads(r.stdout)
        self.assertEqual(i["interface"].split(".")[0], "1")
        self.assertTrue(i["installed"])
        self.assertEqual(i["gpu"], {"model": "fake", "need_mib": 9024, "installed": True})
        self.assertEqual(i["cpu"], {"model": "kokoro", "voice": "fake-cpu-voice"})
        self.assertIn("words_per_min", i)
        self.assertEqual(self.rig.nvsmi_calls(), [])

    def test_info_says_not_installed_rather_than_crashing(self):
        with open(self.rig.cfg_path, "w") as fh:
            fh.write("{broken")
        r = self.rig.cli("info")
        self.assertEqual(r.returncode, 0)
        self.assertFalse(json.loads(r.stdout)["installed"])

    def test_status_heartbeat(self):
        self.rig.set_gpu(free=100, util=0)
        vdir = self.rig.job()
        p = self.rig.start(vdir)
        self.addCleanup(lambda: (p.poll() is None) and os.killpg(p.pid, signal.SIGKILL))
        wait_for(lambda: status(vdir).get("phase") == "waiting-for-gpu", timeout=20)
        a = status(vdir)["updated_at"]
        wait_for(lambda: status(vdir)["updated_at"] != a, timeout=5)
        self.assertEqual(status(vdir)["pid"], p.pid)
        self.assertEqual(status(vdir)["paper_id"], PAPER)
        open(os.path.join(vdir, "cancel"), "w").close()
        p.wait(timeout=20)


if __name__ == "__main__":
    unittest.main()
