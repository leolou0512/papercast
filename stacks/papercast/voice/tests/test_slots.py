"""GPU slots on more than one machine (hosts.py, sched.py), with a fake remote host: jobs run on
its idle GPUs at once, take slots in hand-over order, give a GPU back between chunks when anyone
else appears on it, fall back to stibnite when it cannot be reached, and a waiting job moves to
newly installed code in place."""
import json
import os
import shutil
import signal
import subprocess
import time
import unittest

from helpers import SRC, TAGS, Rig, gone, read, status, voice_log, wait_for

BUSY = dict(free=15000, util=90)        # stibnite's card busy with someone else's work


def script(title: str, n: int = 3) -> str:
    body = ("The guided score equals the plain score plus gamma times the difference. "
            "Turn gamma up and it leans harder into the target, trading diversity for control.")
    return f"# {title}\n\n" + "\n\n".join(body for _ in range(n)) + "\n"


class Base(unittest.TestCase):
    remote_gpus = 2

    def setUp(self):
        self.rig = Rig(remote_gpus=self.remote_gpus)
        self.addCleanup(self.rig.cleanup)

    def paper(self, i: int) -> str:
        return f"2026-09-27-slot{'abcdefgh'[i]}aaa"

    def calls(self, word: str):
        return [c for c in self.rig.fake_calls() if word.lower() in c["text"].lower()]

    def metrics(self, vdir):
        with open(os.path.join(vdir, "metrics.json")) as fh:
            return json.load(fh)["runs"][-1]


class RemoteSlots(Base):
    def test_jobs_speak_on_idle_remote_gpus_at_once_and_leave_nothing_there(self):
        self.rig.set_gpu(**BUSY)
        self.rig.remote_env(FAKE_DELAY_S=0.25)
        dirs = [self.rig.job(script(f"Paper {w}"), paper=self.paper(i))
                for i, w in enumerate(("one", "two"))]
        ps = [self.rig.start(d) for d in dirs]
        notes = [wait_for(lambda d=d: (status(d).get("note") or "").startswith("Speaking on bs1")
                          and status(d)["note"], timeout=30) for d in dirs]
        self.assertRegex(notes[0], r"^Speaking on bs1 GPU [01](, about \d+ min to go)?\.$")
        wait_for(lambda: "min to go." in (status(dirs[0]).get("note") or ""), timeout=30)
        for d, p in zip(dirs, ps):
            self.assertEqual(p.wait(timeout=60), 0, voice_log(d)[-3000:])
            st = status(d)
            self.assertEqual((st["phase"], st["output"]["engine"]), ("done", "gpu:fake"))
        by = [self.metrics(d)["chunks_by_slot"] for d in dirs]
        self.assertEqual(sorted(k for b in by for k in b), ["bs1 GPU 0", "bs1 GPU 1"])
        self.assertEqual({c["dtype"] for c in self.rig.fake_calls()}, {"fp32"})
        # at the same time: each paper's chunks overlap the other's in time
        t1, t2 = ([c["t"] for c in self.rig.fake_calls() if c["gpu"] == g] for g in ("0", "1"))
        self.assertLess(max(min(t1), min(t2)), min(max(t1), max(t2)),
                        "\n=====\n".join(voice_log(d)[-2500:] for d in dirs))
        for d in dirs:                     # the other job's engine is ours, not a stranger
            self.assertNotIn("yielded", voice_log(d))
        # nothing on the remote host but the engine code; its engines are gone
        left = sorted(os.listdir(self.rig.rhome))
        self.assertEqual(left, ["app", "venv"])
        self.assertEqual([n[:2] for n in os.listdir(os.path.join(self.rig.rhome, "app"))], ["v-"])
        for f in os.listdir(os.path.join(self.rig.rhost, "procs")):
            wait_for(lambda f=f: gone(int(f.split("-")[1])), timeout=10)
        self.assertEqual(os.listdir(os.path.join(self.rig.home, "run", "queue")), [])

    def test_a_gpu_with_any_compute_process_is_never_taken(self):
        self.rig.set_gpu(**BUSY)
        other = subprocess.Popen(["sleep", "60"])
        self.addCleanup(other.wait)
        self.addCleanup(other.kill)
        self.rig.set_remote([{"apps": [[other.pid, 200]]}, {}])     # 200 MiB, say stt
        vdir = self.rig.job()
        self.assertEqual(self.rig.run(vdir), 0, voice_log(vdir)[-3000:])
        self.assertEqual(set(c["gpu"] for c in self.rig.fake_calls()), {"1"})
        self.assertIsNone(other.poll())

    def test_oldest_waiting_job_gets_the_next_slot(self):
        """Three papers, one free slot: they speak in the order they were handed over, not the
        order their processes started, and the page says how many are ahead."""
        self.rig.set_gpu(**BUSY)
        taken = {"apps": [[1, 20000]]}
        self.rig.set_remote([taken, taken])                         # no slot free yet
        now = time.time()
        words = ("Alpha", "Bravo", "Charlie")
        dirs = [self.rig.job(script(w, 2), paper=self.paper(i), handed_over=now - 30 + i)
                for i, w in enumerate(words)]
        ps = [self.rig.start(d) for d in reversed(dirs)]            # youngest first
        wait_for(lambda: status(dirs[2]).get("wait_text") ==
                 "waiting for a GPU: 2 papers ahead of it in line", timeout=30)
        wait_for(lambda: status(dirs[0])["wait_text"] == "waiting for a GPU, next in line. "
                 "stibnite: memory is free but the card is busy (90% in use by other work), so "
                 "starting now would slow it; bs1: 2 GPUs in use by other work", timeout=10)
        self.rig.set_remote([{}, taken])                            # one slot frees up
        for p in ps:
            self.assertEqual(p.wait(timeout=90), 0)
        first = [min(c["t"] for c in self.calls(w)) for w in words]
        self.assertEqual(first, sorted(first), [voice_log(d)[-1500:] for d in dirs])

    def test_gives_a_remote_gpu_back_between_chunks_when_another_process_appears(self):
        self.rig.set_gpu(**BUSY)
        self.rig.remote_env(FAKE_DELAY_S=0.3)
        self.rig.set_remote([{}])
        vdir = self.rig.job(script("Yield", 4))
        p = self.rig.start(vdir)
        wait_for(lambda: status(vdir).get("chunks_done", 0) >= 2, timeout=30)
        other = subprocess.Popen(["sleep", "60"])                   # someone's job arrives
        self.addCleanup(other.wait)
        self.addCleanup(other.kill)
        self.rig.set_remote([{"apps": [[other.pid, 3000]]}])
        wait_for(lambda: status(vdir).get("phase") == "waiting-for-gpu", timeout=30)
        self.assertIn(f"another process started on bs1 (process {other.pid} on GPU 0)",
                      status(vdir).get("note") or "")
        wait_for(lambda: "in use by other work" in (status(vdir).get("wait_text") or ""),
                 timeout=10)
        n = len(self.rig.fake_calls())
        time.sleep(1.0)
        self.assertEqual(len(self.rig.fake_calls()), n)            # nothing while they work
        self.rig.set_remote([{}])
        self.assertEqual(p.wait(timeout=60), 0, voice_log(vdir)[-3000:])
        idx = [c["idx"] for c in self.rig.fake_calls()]
        self.assertEqual(len(idx), len(set(idx)))                  # no chunk voiced twice
        self.assertIsNone(other.poll())                            # theirs untouched

    def test_a_stranger_on_any_of_its_gpus_frees_the_whole_host_for_a_while(self):
        """Leo's chat model spans cards 0-6: a job on GPU 0 gives it back when a process
        appears on GPU 1, and no job takes the host until the hold-off ends."""
        self.rig.set_gpu(**BUSY)
        self.rig.cfg["hosts"]["bs1"]["holdoff_s"] = 600
        self.rig.write_cfg()
        self.rig.remote_env(FAKE_DELAY_S=0.3)
        vdir = self.rig.job(script("Hold", 4))
        p = self.rig.start(vdir)
        wait_for(lambda: status(vdir).get("chunks_done", 0) >= 2, timeout=30)
        other = subprocess.Popen(["sleep", "60"])
        self.addCleanup(other.wait)
        self.addCleanup(other.kill)
        self.rig.set_remote([{}, {"apps": [[other.pid, 20000, "/opt/llama/llama-server"]]}])
        st = wait_for(lambda: "left free for other work until" in
                      (status(vdir).get("wait_text") or "") and status(vdir), timeout=30)
        self.assertIn(f"process {other.pid} on GPU 1", st["note"])
        hold = os.path.join(self.rig.home, "run", "holdoff-bs1.json")
        with open(hold) as fh:
            self.assertGreater(json.load(fh)["until"], time.time() + 500)
        self.rig.set_remote([{}, {}])                               # they are gone again
        time.sleep(1.5)
        self.assertEqual(status(vdir)["phase"], "waiting-for-gpu")  # still held back
        os.unlink(hold)
        self.assertEqual(p.wait(timeout=60), 0, voice_log(vdir)[-3000:])
        self.assertIsNone(other.poll())

    def test_pause_file_frees_the_host_until_removed(self):
        self.rig.set_gpu(**BUSY)
        self.rig.remote_env(FAKE_DELAY_S=0.3)
        vdir = self.rig.job(script("Pause", 4))
        p = self.rig.start(vdir)
        wait_for(lambda: status(vdir).get("chunks_done", 0) >= 1, timeout=30)
        pause = os.path.join(self.rig.home, "run", "pause-bs1")
        open(pause, "w").close()
        wait_for(lambda: "bs1: paused" in (status(vdir).get("wait_text") or ""), timeout=30)
        n = len(self.rig.fake_calls())
        time.sleep(1.0)
        self.assertEqual(len(self.rig.fake_calls()), n)
        os.unlink(pause)
        self.assertEqual(p.wait(timeout=60), 0, voice_log(vdir)[-3000:])

    def test_unreachable_host_means_stibnite_only(self):
        self.rig.remote_down(True)
        vdir = self.rig.job()
        self.assertEqual(self.rig.run(vdir), 0, voice_log(vdir)[-3000:])
        self.assertEqual(list(self.metrics(vdir)["chunks_by_slot"]), ["stibnite GPU 0"])
        self.assertEqual({c["gpu"] for c in self.rig.fake_calls()}, {"0"})

    def test_unreachable_host_is_said_on_the_page(self):
        self.rig.set_gpu(**BUSY)
        self.rig.remote_down(True)
        vdir = self.rig.job()
        p = self.rig.start(vdir)
        st = wait_for(lambda: "cannot be reached" in (status(vdir).get("wait_text") or "")
                      and status(vdir), timeout=30)
        self.assertEqual(st["wait_text"], "waiting for a GPU, next in line. stibnite: memory is "
                         "free but the card is busy (90% in use by other work), so starting now "
                         "would slow it; bs1: cannot be reached")
        self.rig.remote_down(False)
        self.assertEqual(p.wait(timeout=60), 0)

    def test_lost_remote_engine_waits_again_and_keeps_its_chunks(self):
        self.rig.set_gpu(**BUSY)
        self.rig.remote_env(FAKE_DELAY_S=0.3)
        vdir = self.rig.job(script("Lost", 4))
        p = self.rig.start(vdir)
        wait_for(lambda: status(vdir).get("chunks_done", 0) >= 2, timeout=30)
        os.kill(int(read(os.path.join(self.rig.tmp, "pid-remote"))), signal.SIGKILL)
        self.assertEqual(p.wait(timeout=60), 0, voice_log(vdir)[-3000:])
        self.assertIn("failed (1 of 3)", voice_log(vdir))
        idx = [c["idx"] for c in self.rig.fake_calls()]
        self.assertLessEqual(len(idx), len(set(idx)) + 1)          # at most the lost chunk again

    def test_use_cpu_and_cancel_while_in_line(self):
        self.rig.set_gpu(**BUSY)
        self.rig.set_remote([{"apps": [[1, 20000]]}, {"apps": [[1, 20000]]}])
        a, b = (self.rig.job(paper=self.paper(i), handed_over=time.time() - 10 + i) for i in (0, 1))
        pa, pb = self.rig.start(a), self.rig.start(b)
        wait_for(lambda: status(b).get("wait_text") ==
                 "waiting for a GPU: 1 paper ahead of it in line", timeout=30)
        open(os.path.join(b, "use-cpu"), "w").close()               # behind in line: at once
        self.assertEqual(pb.wait(timeout=60), 0)
        self.assertEqual(status(b)["output"]["engine"], "cpu:kokoro")
        open(os.path.join(a, "cancel"), "w").close()
        self.assertEqual(pa.wait(timeout=20), 4)
        self.assertEqual(os.listdir(os.path.join(self.rig.home, "run", "queue")), [])


class Upgrade(Base):
    remote_gpus = 0

    def test_waiting_job_moves_to_new_code_in_place(self):
        app = os.path.join(self.rig.tmp, "app")
        shutil.copytree(os.path.join(SRC, "papercast_voice"), os.path.join(app, "papercast_voice"),
                        ignore=shutil.ignore_patterns("__pycache__"))
        ver = os.path.join(app, "papercast_voice", "VERSION")
        with open(ver, "w") as fh:
            fh.write("1.1 (test A)\n")
        self.rig.set_gpu(free=1000, util=0)
        vdir = self.rig.job()
        env = dict(self.rig.env(), PYTHONPATH=app)
        with open(os.path.join(vdir, "voice.log"), "ab") as log:
            p = subprocess.Popen([self.rig.cfg["engines"]["kokoro"]["python"], "-m",
                                  "papercast_voice", "run", vdir], cwd=vdir, env=env,
                                 stdout=log, stderr=log, stdin=subprocess.DEVNULL,
                                 start_new_session=True)
        self.rig.procs.append(p)
        st = wait_for(lambda: status(vdir).get("wait_reason") == "memory" and status(vdir),
                      timeout=30)
        q = os.listdir(os.path.join(self.rig.home, "run", "queue"))
        with open(ver + ".new", "w") as fh:
            fh.write("1.1 (test B)\n")
        os.replace(ver + ".new", ver)                              # what install.sh does
        st2 = wait_for(lambda: status(vdir).get("voice_version") == "1.1 (test B)" and
                       status(vdir).get("wait_reason") == "memory" and status(vdir), timeout=30)
        self.assertEqual(st2["pid"], p.pid)                        # same process for the runner
        self.assertEqual(st2["queued_at"], st["queued_at"])        # same place in line
        self.assertEqual(os.listdir(os.path.join(self.rig.home, "run", "queue")), q)
        self.assertIn("restarting this job on it", voice_log(vdir))
        self.assertIsNone(p.poll())
        self.rig.set_gpu(free=12000, util=0)
        self.assertEqual(p.wait(timeout=60), 0, voice_log(vdir)[-3000:])


class PlaceInLine(unittest.TestCase):
    def job(self, prev: dict | None):
        import tempfile
        from papercast_voice.job import Job
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        vdir = os.path.join(td.name, "state", "2026-09-27-aaaaaaaa", "voice")
        os.makedirs(vdir)
        with open(os.path.join(vdir, "job.json"), "w") as fh:
            json.dump({}, fh)
        os.utime(os.path.join(vdir, "job.json"), (1000.0, 1000.0))
        if prev is not None:
            with open(os.path.join(vdir, "status.json"), "w") as fh:
                json.dump(prev, fh)
        from papercast_voice import config
        return Job(vdir, config.load("/nonexistent")).__class__._queued_at(
            Job(vdir, config.load("/nonexistent")))

    def test_hand_over_time_unless_continuing(self):
        self.assertEqual(self.job(None), 1000.0)
        self.assertEqual(self.job({"phase": "failed", "queued_at": 5.0}), 1000.0)   # a Retry
        self.assertEqual(self.job({"phase": "waiting-for-gpu", "queued_at": 5.0}), 5.0)
        self.assertEqual(self.job({"phase": "speaking", "queued_at": 5.0}), 5.0)
        # a status from the voice before the line (1.0): its wait began at `since`
        self.assertEqual(self.job({"phase": "waiting-for-gpu", "since": "1970-01-01T00:01:40Z"}),
                         100.0)


if __name__ == "__main__":
    unittest.main()
