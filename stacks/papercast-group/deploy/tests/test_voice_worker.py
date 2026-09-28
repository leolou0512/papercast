"""The voice worker against a fake hub (fake_hub.py) and a fake papercast-voice
(fake_papercast_voice.py). Run: python3 -m unittest discover -s stacks/papercast-group/deploy/tests

The worker runs as a subprocess, as under systemd, so a test can stop and restart it."""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

import fake_papercast_voice as fv  # noqa: E402
from fake_hub import FakeHub  # noqa: E402
import voice_worker  # noqa: E402

WORKER = HERE.parent / "voice_worker.py"
SCRIPT = ("# A heading\n\nFirst paragraph, spoken plainly.\n\nSecond paragraph, spoken plainly.\n\n"
          "Third paragraph.\n\nFourth paragraph.\n\nFifth paragraph.\n")


def wait_for(cond, timeout=20.0, what="condition"):
    end = time.time() + timeout
    while time.time() < end:
        v = cond()
        if v:
            return v
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {what}")


class WorkerCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="pcg-worker-test-")
        self.d = Path(self.tmp.name)
        self.hub = FakeHub(token="pcgw_test").start()
        (self.d / "worker.token").write_text("pcgw_test\n")
        self.jobs = self.d / "episodes"
        self.state = self.d / "worker"
        self.procs = []

    def tearDown(self):
        for p in self.procs:
            if p.poll() is None:
                p.send_signal(signal.SIGTERM)
                try:
                    p.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    p.kill()
        self.hub.stop()
        self.tmp.cleanup()

    def voice(self, **knobs) -> str:
        return fv.write_wrapper(str(self.d / "papercast-voice"), **knobs)

    def env(self, voice_cmd: str, **extra) -> dict:
        e = {"HOME": str(self.d), "USER": os.environ.get("USER", "leo"), "PATH": os.environ["PATH"],
             "PCG_HUB_URL": self.hub.url, "PCG_WORKER_TOKEN_FILE": str(self.d / "worker.token"),
             "PAPERCAST_VOICE_CMD": voice_cmd, "PCG_VOICE_JOBS": str(self.jobs),
             "PCG_WORKER_STATE": str(self.state), "PCG_POLL_S": "0.1", "PCG_HEARTBEAT_S": "0.5",
             "PCG_MONITOR_S": "0.05", "PCG_BACKOFF_MAX_S": "0.2", "PCG_VOICE_HOST_LABEL": "perov"}
        e.update(extra)
        return e

    def worker(self, env: dict, *args) -> subprocess.Popen:
        with open(self.d / "worker.log", "ab") as out:
            p = subprocess.Popen([sys.executable, str(WORKER), *args], env=env, stdout=out,
                                 stderr=subprocess.STDOUT)
        self.procs.append(p)
        return p

    def log(self) -> str:
        return (self.d / "worker.log").read_text()

    def status(self, eid) -> dict:
        return json.loads((self.jobs / eid / "voice" / "status.json").read_text())


class TestWorker(WorkerCase):
    def test_voice_id_is_one_papercast_voice_takes(self):
        vid = voice_worker.voice_id("e_abcdefghijkl", "2026-09-28")
        self.assertRegex(vid, fv.ID_RE.pattern)
        self.assertEqual(vid, voice_worker.voice_id("e_abcdefghijkl", "2026-09-28"))
        self.assertNotEqual(vid, voice_worker.voice_id("e_abcdefghijkm", "2026-09-28"))

    def test_one_episode_end_to_end(self):
        self.hub.add("e_aaaaaaaaaaaa", "Flow Matching", SCRIPT)
        p = self.worker(self.env(self.voice(wait_s=0.3)), "--exit-when-idle")
        self.assertEqual(p.wait(timeout=60), 0, self.log())
        a = self.hub.audio["e_aaaaaaaaaaaa"]
        self.assertTrue(a["ok"], "the uploaded bytes match X-Sha256")
        self.assertEqual(a["ctype"], "audio/mpeg")
        self.assertAlmostEqual(a["duration_s"], fv.mp3_seconds(a["bytes"]), places=1)
        self.assertEqual(a["bytes"][:2], b"\xff\xfb")
        ph = self.hub.phases("e_aaaaaaaaaaaa")
        self.assertEqual(ph[0], "preparing")
        self.assertIn("speaking", ph)
        self.assertLess(ph.index("waiting-for-gpu"), ph.index("speaking"))
        # progress rises to 1, and the voice's "stibnite" becomes the real machine's name
        prog = [s["progress"] for s in self.hub.statuses["e_aaaaaaaaaaaa"]]
        self.assertEqual(prog, sorted(prog))
        self.assertGreater(max(prog), 0.0)
        details = [s.get("detail") or "" for s in self.hub.statuses["e_aaaaaaaaaaaa"]]
        self.assertTrue(any("perov" in x for x in details))
        self.assertFalse(any("stibnite" in x for x in details))
        # the job directory as Leo's runner writes it; the MP3 is gone once the hub has it
        v = self.jobs / "e_aaaaaaaaaaaa" / "voice"
        job = json.loads((v / "job.json").read_text())
        self.assertEqual(job["tags"]["title"], "Flow Matching")
        self.assertEqual(job["tags"]["artist"], "Ada Lovelace")
        self.assertEqual(job["engine"], "auto")
        self.assertEqual((v / "script.md").read_text(), SCRIPT)
        self.assertFalse((v / "out" / "episode.mp3").exists())
        self.assertTrue((v / "uploaded.json").exists())
        self.assertFalse((self.state / "current.json").exists())

    def test_two_episodes_one_at_a_time(self):
        self.hub.add("e_aaaaaaaaaaaa", "One", SCRIPT)
        self.hub.add("e_bbbbbbbbbbbb", "Two", SCRIPT)
        p = self.worker(self.env(self.voice(chunk_s=0.05)), "--exit-when-idle")
        self.assertEqual(p.wait(timeout=60), 0, self.log())
        self.assertEqual(set(self.hub.audio), {"e_aaaaaaaaaaaa", "e_bbbbbbbbbbbb"})
        # the second claim came only after the first upload
        calls = self.hub.calls
        first_audio = calls.index(("PUT", "/api/voice/e_aaaaaaaaaaaa/audio"))
        claims = [i for i, c in enumerate(calls) if c == ("POST", "/api/voice/claim")]
        self.assertLess(claims[0], first_audio)
        self.assertGreater(claims[1], first_audio)

    def test_failure_is_reported(self):
        self.hub.add("e_aaaaaaaaaaaa", "Broken", SCRIPT)
        p = self.worker(self.env(self.voice(fail="gpu_oom")), "--exit-when-idle")
        self.assertEqual(p.wait(timeout=60), 0, self.log())
        f = self.hub.failures["e_aaaaaaaaaaaa"]
        self.assertEqual(f["code"], "gpu_oom")
        self.assertIn("fake failure", f["error"])
        self.assertNotIn("e_aaaaaaaaaaaa", self.hub.audio)

    def test_script_the_voice_refuses(self):
        self.hub.add("e_aaaaaaaaaaaa", "Digits", "It has 3 digits.\n")
        p = self.worker(self.env(self.voice()), "--exit-when-idle")
        self.assertEqual(p.wait(timeout=60), 0, self.log())
        self.assertEqual(self.hub.failures["e_aaaaaaaaaaaa"]["code"], "script_invalid")

    def test_restart_carries_on_with_the_same_episode(self):
        self.hub.add("e_aaaaaaaaaaaa", "Slow", SCRIPT)
        voice = self.voice(chunk_s=0.4)
        p = self.worker(self.env(voice))
        wait_for(lambda: (self.jobs / "e_aaaaaaaaaaaa/voice/chunks/0001.done").exists(),
                 what="two chunks voiced")
        vpid = self.status("e_aaaaaaaaaaaa")["pid"]
        p.send_signal(signal.SIGTERM)
        self.assertEqual(p.wait(timeout=30), 0)
        # the voice this worker started is gone with it (its process group), the claim is kept
        wait_for(lambda: not os.path.exists(f"/proc/{vpid}"), 10, "the voice stopped")
        self.assertTrue((self.state / "current.json").exists())
        done_before = len(list((self.jobs / "e_aaaaaaaaaaaa/voice/chunks").iterdir()))
        self.assertGreaterEqual(done_before, 2)
        p2 = self.worker(self.env(voice), "--exit-when-idle")
        self.assertEqual(p2.wait(timeout=60), 0, self.log())
        self.assertIn("e_aaaaaaaaaaaa", self.hub.audio)
        self.assertEqual(self.hub.claims, 2, "claimed once, then once more to find the queue empty")
        self.assertGreaterEqual(self.status("e_aaaaaaaaaaaa")["resumed_chunks"], done_before)
        self.assertIn("carrying on with e_aaaaaaaaaaaa", self.log())

    def test_adopts_a_voice_that_outlived_its_worker(self):
        # the nohup fallback: the worker died, its voice did not
        self.hub.add("e_aaaaaaaaaaaa", "Orphan", SCRIPT)
        env = self.env(self.voice(chunk_s=0.3))
        p = self.worker(env)
        wait_for(lambda: (self.jobs / "e_aaaaaaaaaaaa/voice/chunks/0000.done").exists(), what="a chunk")
        vpid = self.status("e_aaaaaaaaaaaa")["pid"]
        p.kill()                                    # SIGKILL: the worker cannot stop its child
        p.wait()
        self.assertTrue(os.path.exists(f"/proc/{vpid}"))
        p2 = self.worker(env, "--exit-when-idle")
        self.assertEqual(p2.wait(timeout=60), 0, self.log())
        self.assertIn(f"adopting the voice process already working on it (pid {vpid})", self.log())
        self.assertIn("e_aaaaaaaaaaaa", self.hub.audio)
        self.assertEqual(self.status("e_aaaaaaaaaaaa")["pid"], vpid, "voiced by the adopted process")

    def test_episode_deleted_while_voicing(self):
        self.hub.add("e_aaaaaaaaaaaa", "Deleted", SCRIPT)
        self.hub.add("e_bbbbbbbbbbbb", "Next", SCRIPT)
        p = self.worker(self.env(self.voice(chunk_s=0.3)), "--exit-when-idle")
        wait_for(lambda: (self.jobs / "e_aaaaaaaaaaaa/voice/chunks/0000.done").exists(), what="a chunk")
        self.hub.lose("e_aaaaaaaaaaaa")
        self.assertEqual(p.wait(timeout=60), 0, self.log())
        self.assertNotIn("e_aaaaaaaaaaaa", self.hub.audio)
        self.assertNotIn("e_aaaaaaaaaaaa", self.hub.failures)
        self.assertEqual(self.status("e_aaaaaaaaaaaa")["error"]["code"], "cancelled")
        self.assertIn("e_bbbbbbbbbbbb", self.hub.audio, "went on to the next one")

    def test_upload_waits_out_a_hub_that_is_down(self):
        self.hub.add("e_aaaaaaaaaaaa", "Retry", SCRIPT)
        self.hub.fail_next("PUT", "audio", 503, 500, 503)
        p = self.worker(self.env(self.voice()), "--exit-when-idle")
        self.assertEqual(p.wait(timeout=60), 0, self.log())
        self.assertIn("e_aaaaaaaaaaaa", self.hub.audio)
        n = sum(1 for c in self.hub.calls if c == ("PUT", "/api/voice/e_aaaaaaaaaaaa/audio"))
        self.assertEqual(n, 4)

    def test_hub_refuses_the_audio(self):
        self.hub.add("e_aaaaaaaaaaaa", "Refused", SCRIPT)
        self.hub.fail_next("PUT", "audio", 400)
        p = self.worker(self.env(self.voice()), "--exit-when-idle")
        self.assertEqual(p.wait(timeout=60), 0, self.log())
        self.assertEqual(self.hub.failures["e_aaaaaaaaaaaa"]["code"], "upload_refused")

    def test_hub_without_the_voice_api_yet(self):
        # the stub hub answers 404 to claim: the worker waits instead of failing anything
        self.hub.fail_next("POST", "claim", 404, 404)
        self.hub.add("e_aaaaaaaaaaaa", "Later", SCRIPT)
        p = self.worker(self.env(self.voice()), "--exit-when-idle")
        self.assertEqual(p.wait(timeout=60), 0, self.log())
        self.assertIn("may not be there yet", self.log())
        self.assertIn("e_aaaaaaaaaaaa", self.hub.audio)

    def test_wrong_token_claims_nothing(self):
        self.hub.add("e_aaaaaaaaaaaa", "Nope", SCRIPT)
        (self.d / "worker.token").write_text("wrong\n")
        p = self.worker(self.env(self.voice()))
        wait_for(lambda: "refused the worker token" in self.log(), what="the refusal logged")
        p.send_signal(signal.SIGTERM)
        p.wait(timeout=30)
        self.assertEqual(self.hub.claims, 0)
        self.assertEqual(self.hub.audio, {})

    def test_voice_not_installed(self):
        self.hub.add("e_aaaaaaaaaaaa", "No voice", SCRIPT)
        p = self.worker(self.env(str(self.d / "missing-voice")), "--exit-when-idle")
        self.assertEqual(p.wait(timeout=30), 1)
        self.assertEqual(self.hub.claims, 0, "no episode is taken without a voice")

    # ---- voices (hub/voices.py) and timings

    WARM = {"id": "warm-male", "name": "Warm male", "cpu": False,
            "spec": {"engine": "breeze", "voice": "preset-warm-male-s42", "id": "warm-male",
                     "instruction": "Adult male, mid-30s, neutral American accent.", "seed": 42}}
    CPU = {"id": "basic-female", "name": "Basic female (CPU)", "cpu": True,
           "spec": {"engine": "kokoro", "voice": "af_heart", "id": "basic-female"}}

    def test_the_claims_voice_reaches_papercast_voice(self):
        self.hub.add("e_aaaaaaaaaaaa", "Warm", SCRIPT, voice=self.WARM)
        p = self.worker(self.env(self.voice()), "--exit-when-idle")
        self.assertEqual(p.wait(timeout=60), 0, self.log())
        v = self.jobs / "e_aaaaaaaaaaaa" / "voice"
        job = json.loads((v / "job.json").read_text())
        self.assertEqual((job["voice"], job["engine"]), (self.WARM["spec"], "auto"))
        self.assertEqual(self.status("e_aaaaaaaaaaaa")["output"]["voice_spec"], self.WARM["spec"])
        self.assertEqual(self.hub.uploads, [("e_aaaaaaaaaaaa", "preset-warm-male-s42")])
        # the timings went first, so the hub has them when the MP3 lands
        (doc,) = self.hub.timings["e_aaaaaaaaaaaa"]
        self.assertEqual(doc["version"], 1)
        self.assertEqual(doc["segments"][0]["text"], "A heading")
        calls = self.hub.calls
        self.assertLess(calls.index(("PUT", "/api/voice/e_aaaaaaaaaaaa/timings")),
                        calls.index(("PUT", "/api/voice/e_aaaaaaaaaaaa/audio")))

    def test_a_cpu_voice_runs_on_the_cpu(self):
        self.hub.add("e_aaaaaaaaaaaa", "Quick", SCRIPT, voice=self.CPU)
        p = self.worker(self.env(self.voice()), "--exit-when-idle")
        self.assertEqual(p.wait(timeout=60), 0, self.log())
        job = json.loads((self.jobs / "e_aaaaaaaaaaaa" / "voice" / "job.json").read_text())
        self.assertEqual((job["engine"], job["voice"]["voice"]), ("cpu", "af_heart"))
        self.assertEqual(self.hub.uploads, [("e_aaaaaaaaaaaa", "af_heart")])

    def test_no_voice_is_papercast_voices_default(self):
        self.hub.add("e_aaaaaaaaaaaa", "Default", SCRIPT)
        p = self.worker(self.env(self.voice()), "--exit-when-idle")
        self.assertEqual(p.wait(timeout=60), 0, self.log())
        job = json.loads((self.jobs / "e_aaaaaaaaaaaa" / "voice" / "job.json").read_text())
        self.assertNotIn("voice", job)
        self.assertEqual(self.hub.uploads, [("e_aaaaaaaaaaaa", "described-narrator-a-seed42")])

    def test_a_voice_change_starts_the_job_directory_afresh(self):
        self.hub.add("e_aaaaaaaaaaaa", "Twice", SCRIPT)
        env = self.env(self.voice())
        self.assertEqual(self.worker(env, "--exit-when-idle").wait(timeout=60), 0, self.log())
        self.hub.add("e_aaaaaaaaaaaa", "Twice", SCRIPT, voice=self.WARM)       # someone changed its voice
        self.assertEqual(self.worker(env, "--exit-when-idle").wait(timeout=60), 0, self.log())
        self.assertEqual(self.hub.uploads, [("e_aaaaaaaaaaaa", "described-narrator-a-seed42"),
                                            ("e_aaaaaaaaaaaa", "preset-warm-male-s42")])
        v = self.jobs / "e_aaaaaaaaaaaa" / "voice"
        self.assertEqual(json.loads((v / "job.json").read_text())["voice"], self.WARM["spec"])
        self.assertEqual(self.status("e_aaaaaaaaaaaa")["resumed_chunks"], 0, "nothing of the old voice reused")
        self.assertIn("another voice; its job directory starts afresh", self.log())
        self.assertEqual(len(self.hub.timings["e_aaaaaaaaaaaa"]), 2)
        # and the same voice once more (a change back and forth): voiced again, not "already done"
        self.hub.add("e_aaaaaaaaaaaa", "Twice", SCRIPT, voice=self.WARM)
        self.assertEqual(self.worker(env, "--exit-when-idle").wait(timeout=60), 0, self.log())
        self.assertEqual(len(self.hub.uploads), 3)
        self.assertIn("voiced again; its job directory starts afresh", self.log())

    def test_timings_are_a_bonus(self):
        # a voice without timings (papercast-voice before 1.5), and a hub that refuses them
        self.hub.add("e_aaaaaaaaaaaa", "No timings", SCRIPT)
        p = self.worker(self.env(self.voice(no_timings=1)), "--exit-when-idle")
        self.assertEqual(p.wait(timeout=60), 0, self.log())
        self.assertIn("e_aaaaaaaaaaaa", self.hub.audio)
        self.assertNotIn("e_aaaaaaaaaaaa", self.hub.timings)
        self.hub.add("e_bbbbbbbbbbbb", "Refused timings", SCRIPT)
        self.hub.fail_next("PUT", "timings", 400)
        p = self.worker(self.env(self.voice()), "--exit-when-idle")
        self.assertEqual(p.wait(timeout=60), 0, self.log())
        self.assertIn("e_bbbbbbbbbbbb", self.hub.audio)
        self.assertIn("the hub did not take the timings", self.log())

    def test_one_worker_at_a_time(self):
        env = self.env(self.voice())
        p = self.worker(env)
        wait_for(lambda: "started" in self.log(), what="the first worker")
        p2 = self.worker(env)
        self.assertEqual(p2.wait(timeout=20), 3)
        self.assertIn("another worker holds", self.log())


if __name__ == "__main__":
    unittest.main()
