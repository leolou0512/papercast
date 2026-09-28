"""A3's voice queue against a real running hub (SPEC.md section 9): the fair order over uploaders,
one claim per worker, status, the audio upload and its checks, failures and retries, and a stale
claim going back to the queue."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import contrib_harness as H  # noqa: E402

from hub import db, voiceq  # noqa: E402

MP3 = b"ID3\x04\x00\x00\x00\x00\x00\x00" + b"\xff\xfb\x90\x00" * 600
MP3_NO_TAG = b"\xff\xfb\x90\x00" * 600


class VoiceTest(unittest.TestCase):
    def setUp(self):
        self.h = H.Hub()
        self.n = 0

    def tearDown(self):
        self.h.close()

    def episode(self, user) -> str:
        """A new paper by `user`, checked and queued; its episode id."""
        self.n += 1
        return self.h.make_paper(user, f"2301.{20000 + self.n}", f"Voice test paper {self.n}")["id"]

    def claim(self, worker="w1"):
        return self.h.request("POST", "/api/voice/claim", {"worker": worker}, worker=True)

    def audio(self, eid, data=MP3, duration="1234.5", ctype="audio/mpeg", worker=None, **kw):
        headers = {"X-Duration-S": duration} if duration is not None else {}
        if worker:
            headers["X-Worker"] = worker
        return self.h.request("PUT", f"/api/voice/{eid}/audio", body=data, ctype=ctype,
                              headers=headers, worker=True, **kw)

    def state(self, eid):
        return db.conn().execute("SELECT e.state, e.state_detail, e.duration_s, v.state AS vstate, v.attempts, "
                                 "v.phase, v.progress, v.error FROM episodes e JOIN voice_jobs v "
                                 "ON v.episode_id = e.id WHERE e.id = ?", (eid,)).fetchone()

    def set_queued_at(self, order):
        """Queue times one minute apart, in this order (as if uploaded minutes apart)."""
        with db.transaction() as c:
            for i, eid in enumerate(order):
                c.execute("UPDATE voice_jobs SET queued_at = ? WHERE episode_id = ?",
                          (f"2026-09-28T01:{i:02d}:00Z", eid))

    # ----

    def test_fair_order_over_three_uploaders(self):
        a, b, c = self.h.user("Ann"), self.h.user("Bob"), self.h.user("Cyd")
        A = [self.episode(a) for _ in range(3)]
        B = [self.episode(b) for _ in range(2)]
        C = [self.episode(c)]
        self.set_queued_at(A + B + C)
        want = [A[0], B[0], C[0], A[1], B[1], A[2]]
        code, q = self.h.request("GET", "/api/voice/queue", worker=True)
        self.assertEqual([j["episode_id"] for j in q["queued"]], want)
        code, mine = self.h.request("GET", "/api/cli/episodes?mine=1", user=a)
        self.assertEqual({e["id"]: e["voice"]["queue_position"] for e in mine["episodes"]},
                         {A[0]: 1, A[1]: 4, A[2]: 6})
        got = []
        for _ in want:
            code, job = self.claim()
            self.assertEqual(code, 200, job)
            got.append(job["episode_id"])
            code, out = self.audio(job["episode_id"])
            self.assertEqual(code, 200, out)
        self.assertEqual(got, want)
        code, _ = self.claim()
        self.assertEqual(code, 204)

        # Who was served longest ago goes first: Cyd (third), then Bob (fifth), then Ann (last),
        # whatever the order they uploaded in now.
        a2, b2, c2 = self.episode(a), self.episode(b), self.episode(c)
        self.set_queued_at([a2, b2, c2])
        got = []
        for _ in range(3):
            code, job = self.claim()
            got.append(job["episode_id"])
            self.audio(job["episode_id"])
        self.assertEqual(got, [c2, b2, a2])

        # Someone never served goes before all of them, even with a later upload.
        d = self.h.user("Dee")
        a3, d1 = self.episode(a), self.episode(d)
        self.set_queued_at([a3, d1])
        self.assertEqual(self.claim()[1]["episode_id"], d1)

    def test_one_claim_per_worker_and_the_job(self):
        a, b = self.h.user("Eve"), self.h.user("Fin")
        e1, e2 = self.episode(a), self.episode(b)
        self.set_queued_at([e1, e2])
        code, job = self.claim("w1")
        self.assertEqual((code, job["episode_id"], job["attempt"], job["resumed"]), (200, e1, 1, False))
        self.assertEqual((job["title"], job["first_author"], job["year"], job["made_by"]),
                         ("Voice test paper 1", "Yang Song", 2020, "Eve"))
        self.assertEqual(job["script_url"], f"/api/voice/{e1}/script")
        code, again = self.claim("w1")                 # the one it holds, not a second
        self.assertEqual((again["episode_id"], again["attempt"], again["resumed"]), (e1, 1, True))
        code, other = self.claim("w2")
        self.assertEqual(other["episode_id"], e2)
        code, text = self.h.request("GET", job["script_url"], worker=True)
        self.assertEqual(code, 200)
        self.assertTrue(text.decode().startswith("# How the method works"))
        self.assertEqual(self.state(e1)["state"], "waiting-for-gpu")
        # only the worker's token opens these
        code, _ = self.h.request("POST", "/api/voice/claim", {}, user=a)
        self.assertEqual(code, 401)
        code, _ = self.h.request("GET", job["script_url"], user=a)
        self.assertEqual(code, 401)
        code, _ = self.h.request("GET", "/api/voice/e_nosuchepisode/script", worker=True)
        self.assertEqual(code, 404)

    def test_status_and_audio(self):
        a = self.h.user("Gil")
        e1, e2 = self.episode(a), self.episode(a)
        self.set_queued_at([e1, e2])
        code, job = self.claim("w1")
        eid = job["episode_id"]
        code, out = self.h.request("PUT", f"/api/voice/{eid}/status", {"phase": "waiting-for-gpu", "progress": 0}, worker=True)
        self.assertEqual((code, out["state"]), (200, "waiting-for-gpu"))
        code, out = self.h.request("PUT", f"/api/voice/{eid}/status", {"phase": "speaking", "progress": 0.4}, worker=True)
        self.assertEqual((code, out["state"]), (200, "speaking"))
        s = self.state(eid)
        self.assertEqual((s["state"], s["state_detail"], s["phase"], s["progress"]), ("speaking", "speaking, 40%", "speaking", 0.4))
        code, ep = self.h.request("GET", f"/api/cli/episodes/{eid}", user=a)
        self.assertEqual((ep["state"], ep["voice"]["progress"], ep["voice"]["phase"]), ("speaking", 0.4, "speaking"))
        code, out = self.h.request("PUT", f"/api/voice/{eid}/status", {"phase": "speaking", "progress": 42,
                                                                         "note": "Speaking on perov."}, worker=True)
        self.assertEqual((self.state(eid)["progress"], self.state(eid)["state_detail"]), (0.42, "Speaking on perov."))
        code, out = self.h.request("PUT", f"/api/voice/{eid}/status", {"phase": "speaking", "progress": "half"}, worker=True)
        self.assertEqual((code, out["error"]), (400, "bad_progress"))
        code, out = self.h.request("PUT", f"/api/voice/{e2}/status", {"phase": "speaking"}, worker=True)
        self.assertEqual((code, out["error"]), (409, "not_claimed"))
        code, out = self.h.request("PUT", "/api/voice/e_nosuchepisode/status", {"phase": "speaking"}, worker=True)
        self.assertEqual(code, 404)

        # the audio: its type, its length header, its first bytes, its size
        self.assertEqual(self.audio(eid, ctype="audio/wav")[0], 415)
        self.assertEqual(self.audio(eid, duration=None)[1]["error"], "bad_duration")
        self.assertEqual(self.audio(eid, duration="-3")[1]["error"], "bad_duration")
        self.assertEqual(self.audio(eid, data=b"RIFF" + b"\0" * 4000)[1]["error"], "not_mp3")
        self.assertEqual(self.audio(eid, data=b"ID3" + b"\0" * 100)[1]["error"], "not_mp3")    # too small
        self.assertEqual(self.audio(eid, data=b"\xff\xf1" + b"\0" * 4000)[1]["error"], "not_mp3")  # AAC ADTS
        code, out = self.audio(eid, data=None, length=200 * 1024 * 1024 + 1)
        self.assertEqual((code, out["error"]), (413, "too_large"))
        self.assertEqual(self.audio(eid, worker="w9")[1]["error"], "claimed_by_other")
        self.assertEqual(self.state(eid)["state"], "speaking")
        code, out = self.audio(eid, worker="w1")
        self.assertEqual((code, out["state"], out["duration_s"]), (200, "ready", 1234.5))
        s = self.state(eid)
        self.assertEqual((s["state"], s["duration_s"], s["vstate"], s["progress"]), ("ready", 1234.5, "done", 1))
        self.assertEqual((self.h.cfg.episodes / eid / "audio.mp3").read_bytes(), MP3)
        self.assertEqual(self.audio(eid)[1]["error"], "already_done")
        self.assertEqual(list((self.h.cfg.data / "tmp").iterdir()), [])
        # an MP3 with no ID3 tag, only frames
        code, job = self.claim("w1")
        self.assertEqual(job["episode_id"], e2)
        self.assertEqual(self.audio(e2, data=MP3_NO_TAG)[0], 200)
        self.assertEqual(self.state(e2)["state"], "ready")

    def test_failed_and_retries(self):
        a = self.h.user("Hub")
        eid = self.episode(a)
        for attempt in (1, 2, 3):
            code, job = self.claim()
            self.assertEqual((code, job["episode_id"], job["attempt"]), (200, eid, attempt), job)
            code, out = self.h.request("POST", f"/api/voice/{eid}/failed", {"error": "gpu_oom: out of memory"}, worker=True)
            self.assertEqual((code, out["attempts"], out["retry"]), (200, attempt, attempt < 3))
            s = self.state(eid)
            self.assertEqual((s["state"], s["vstate"], s["error"]), ("failed", "failed", "gpu_oom: out of memory"))
            self.assertIn("gpu_oom: out of memory", s["state_detail"])
            self.assertIn("tried again" if attempt < 3 else "stays failed", s["state_detail"])
        code, _ = self.claim()
        self.assertEqual(code, 204)
        code, ep = self.h.request("GET", f"/api/cli/episodes/{eid}", user=a)
        self.assertEqual((ep["state"], ep["voice"]["attempts"]), ("failed", 3))
        code, out = self.h.request("POST", f"/api/voice/{eid}/failed", {"error": "again"}, worker=True)
        self.assertEqual((code, out["error"]), (409, "not_claimed"))

    def test_stale_claim_returns_to_the_queue(self):
        a, b = self.h.user("Ian"), self.h.user("Jo")
        e1, e2 = self.episode(a), self.episode(b)
        self.set_queued_at([e1, e2])
        code, job = self.claim("w1")
        self.assertEqual(job["episode_id"], e1)
        self.h.request("PUT", f"/api/voice/{e1}/status", {"phase": "speaking", "progress": 0.3}, worker=True)
        # 29 minutes without news is not stale yet
        with db.transaction() as c:
            c.execute("UPDATE voice_jobs SET heartbeat_at = ? WHERE episode_id = ?", (voiceq._ago(29 * 60), e1))
            self.assertEqual(voiceq.requeue_stale(c), [])
        # 31 minutes is: back in the queue, the episode says why
        with db.transaction() as c:
            c.execute("UPDATE voice_jobs SET heartbeat_at = ?, claimed_at = ? WHERE episode_id = ?",
                      (voiceq._ago(31 * 60), voiceq._ago(40 * 60), e1))
            self.assertEqual(voiceq.requeue_stale(c), [e1])
        s = self.state(e1)
        self.assertEqual((s["state"], s["vstate"], s["phase"]), ("waiting-for-gpu", "queued", None))
        self.assertIn("stopped answering", s["state_detail"])
        # it is the oldest in line, so the next worker gets it (a second attempt); w1 is told it lost it
        code, job = self.claim("w2")
        self.assertEqual((job["episode_id"], job["attempt"]), (e1, 2))
        code, out = self.h.request("PUT", f"/api/voice/{e1}/status", {"phase": "speaking", "worker": "w1"}, worker=True)
        self.assertEqual((code, out["error"]), (409, "claimed_by_other"))
        # the claim path itself sends a stale claim back (no sweeper needed)
        with db.transaction() as c:
            c.execute("UPDATE voice_jobs SET heartbeat_at = ?, claimed_at = ? WHERE episode_id = ?",
                      (voiceq._ago(31 * 60), voiceq._ago(31 * 60), e1))
        code, job = self.claim("w3")
        self.assertEqual((job["episode_id"], job["attempt"]), (e1, 3))
        self.assertEqual(self.state(e1)["vstate"], "claimed")
        code, job = self.claim("w2")                    # w2 lost e1; it gets the next one
        self.assertEqual(job["episode_id"], e2)

    def test_deleted_episodes_are_not_voiced(self):
        a = self.h.user("Kay")
        e1, e2 = self.episode(a), self.episode(a)
        self.set_queued_at([e1, e2])
        with db.transaction() as c:
            c.execute("UPDATE episodes SET deleted_at = ? WHERE id = ?", (db.now(), e1))
        self.assertEqual(self.claim()[1]["episode_id"], e2)
        self.audio(e2)
        self.assertEqual(self.claim()[0], 204)

    def test_fair_order_function(self):
        J = lambda e, u, t, r: {"episode_id": e, "user_id": u, "queued_at": t, "rid": r}  # noqa: E731
        jobs = [J("a1", 1, "t1", 1), J("a2", 1, "t2", 2), J("b1", 2, "t3", 3), J("c1", 3, "t4", 4)]
        self.assertEqual([j["episode_id"] for j in voiceq.fair_order(jobs, {})], ["a1", "b1", "c1", "a2"])
        self.assertEqual([j["episode_id"] for j in voiceq.fair_order(jobs, {1: 9, 2: 3})], ["c1", "b1", "a1", "a2"])
        self.assertEqual(voiceq.fair_order([], {1: 2}), [])
        self.assertTrue(voiceq.looks_like_mp3(b"ID3\x04"))
        self.assertTrue(voiceq.looks_like_mp3(b"\xff\xfb\x90\x00"))
        self.assertFalse(voiceq.looks_like_mp3(b"\xff\xf1\x50\x80"))
        self.assertFalse(voiceq.looks_like_mp3(b"OggS"))


if __name__ == "__main__":
    unittest.main()
