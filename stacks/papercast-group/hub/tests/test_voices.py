"""Voices (hub/voices.py) against a real running hub: the presets and their samples, the sentence
timings the worker uploads and where the hub keeps them, changing an episode's voice (its maker
or an admin, nobody else; at most two waiting per person), the fair queue carrying the change,
the old audio playing on until the new one lands, and the swap: new audio, new timings, a new
revision, positions carried to the same sentence."""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import contrib_harness as H  # noqa: E402

from hub import db, events, voices  # noqa: E402

MP3 = b"ID3\x04\x00\x00\x00\x00\x00\x00" + b"\xff\xfb\x90\x00" * 600
MP3_2 = b"ID3\x04\x00\x00\x00\x00\x00\x00" + b"\xff\xfb\x90\x00" * 900
DEFAULT_KEY = "described-narrator-a-seed42"


def timings(n: int, per: float = 4.0, lead: float = 0.46, texts=None) -> dict:
    texts = texts or [f"Sentence number {i}." for i in range(n)]
    return {"version": 1, "duration_s": lead + n * per + 1,
            "segments": [{"start": round(lead + i * per, 3), "end": round(lead + i * per + per - 0.3, 3),
                          "text": t} for i, t in enumerate(texts)]}


class Voices(unittest.TestCase):
    def setUp(self):
        self.h = H.Hub()
        self.n = 0

    def tearDown(self):
        self.h.close()

    # -- helpers
    def web(self, method, path, who, body=None, pcg=True):
        h = {"X-Test-User": who["email"]}
        if pcg:
            h["X-PCG"] = "1"
        return self.h.request(method, path, body=body, headers=h)

    def worker(self, method, path, body=None, **kw):
        return self.h.request(method, path, body=body, worker=True, **kw)

    def episode(self, user) -> str:
        self.n += 1
        return self.h.make_paper(user, f"2302.{30000 + self.n}", f"Voices test paper {self.n}")["id"]

    def claim(self, want=None):
        code, job = self.worker("POST", "/api/voice/claim", {"worker": "w1"})
        self.assertEqual(code, 200, job)
        if want:
            self.assertEqual(job["episode_id"], want)
        return job

    def upload(self, eid, data=MP3, dur="100.0", key=DEFAULT_KEY):
        h = {"X-Duration-S": dur}
        if key:
            h["X-Voice"] = key
        return self.worker("PUT", f"/api/voice/{eid}/audio", body=data, ctype="audio/mpeg", headers=h)

    def voiced(self, user, doc=None, key=DEFAULT_KEY) -> str:
        """A new episode by `user`, voiced: claimed, its timings, its MP3."""
        eid = self.episode(user)
        self.claim(eid)
        if doc is not None:
            self.assertEqual(self.worker("PUT", f"/api/voice/{eid}/timings", doc)[0], 200)
        code, out = self.upload(eid, key=key)
        self.assertEqual(code, 200, out)
        return eid

    def ep_view(self, eid, who):
        code, v = self.web("GET", f"/api/episodes/{eid}/voice", who)
        self.assertEqual(code, 200, v)
        return v

    def lib_voice(self, eid, who):
        code, j = self.web("GET", "/api/library?q=", who)
        self.assertEqual(code, 200, j)
        for p in j["papers"]:
            for e in p["episodes"]:
                if e["id"] == eid:
                    return e
        self.fail(f"{eid} not in the library")

    def files(self, eid):
        d = self.h.cfg.episodes / eid
        return {n: json.loads((d / n).read_text()) for n in ("timings.json", "timings.next.json", "timings.prev.json")
                if (d / n).is_file()}

    # -- the presets
    def test_presets_and_samples(self):
        v = self.h.user("Viv", "viewer")
        code, j = self.web("GET", "/api/voices", v)
        self.assertEqual(code, 200, j)
        ids = [x["id"] for x in j["voices"]]
        self.assertTrue(4 <= len(ids) <= 6, ids)
        self.assertEqual(j["default"], "clear-female")
        self.assertEqual([x["id"] for x in j["voices"] if x["default"]], ["clear-female"])
        self.assertEqual(sum(1 for x in j["voices"] if x["cpu"]), 1)
        self.assertEqual(j["max_waiting"], 2)
        self.assertTrue(all(x["sample"] is None for x in j["voices"]))
        keys = [p["key"] for p in voices.PRESETS]
        self.assertEqual(len(set(keys)), len(keys), "a key names one voice: the voice caches chunks by it")
        # papercast-voice's own default is the default preset, word for word
        sys.path.insert(0, str(H.REPO / "stacks" / "papercast" / "voice"))
        from papercast_voice.config import DEFAULTS
        b = DEFAULTS["engines"]["breeze"]
        dflt = voices.PRESET_BY_ID["clear-female"]
        self.assertEqual((dflt["key"], dflt["instruction"], dflt["seed"]), (b["voice"], b["instruction"], b["seed"]))
        self.assertEqual(voices.PRESET_BY_ID["basic-female"]["key"], DEFAULTS["engines"]["kokoro"]["voice"])
        # a sample clip once tools/make_voice_samples.py has made it
        d = self.h.cfg.data / "voices"
        d.mkdir()
        (d / "warm-male.mp3").write_bytes(MP3)
        code, j = self.web("GET", "/api/voices", v)
        url = next(x["sample"] for x in j["voices"] if x["id"] == "warm-male")
        self.assertRegex(url, r"^/api/voices/warm-male/sample\.mp3\?v=\d+$")
        code, body = self.web("GET", url, v)
        self.assertEqual((code, body), (200, MP3))
        self.assertEqual(self.web("GET", "/api/voices/calm-male/sample.mp3", v)[0], 404)
        self.assertEqual(self.web("GET", "/api/voices/nope/sample.mp3", v)[0], 404)
        self.assertEqual(self.h.request("GET", "/api/voices")[0], 401)

    # -- timings
    def test_timings_upload_and_storage(self):
        a = self.h.user("Ada")
        eid = self.episode(a)
        # before the claim: neither being voiced nor with audio
        self.assertEqual(self.worker("PUT", f"/api/voice/{eid}/timings", timings(3))[1]["error"], "no_audio")
        self.claim(eid)
        doc = timings(5)
        code, out = self.worker("PUT", f"/api/voice/{eid}/timings", doc)
        self.assertEqual((code, out["stored"]), (200, "next"))
        self.assertEqual(list(self.files(eid)), ["timings.next.json"], "not the audio's until the audio lands")
        for bad, why in ((dict(doc, version=2), "version"),
                         ({"version": 1, "segments": []}, "empty"),
                         ({"version": 1, "segments": [{"start": 3, "end": 1, "text": "x"}]}, "end before start"),
                         ({"version": 1, "segments": [{"start": 5, "end": 6, "text": "a"},
                                                      {"start": 1, "end": 2, "text": "b"}]}, "out of order"),
                         ({"version": 1, "segments": [{"start": 0, "end": 1, "text": " "}]}, "no text"),
                         ({"version": 1, "segments": [{"start": True, "end": 1, "text": "x"}]}, "not a number")):
            with self.subTest(why=why):
                code, out = self.worker("PUT", f"/api/voice/{eid}/timings", bad)
                self.assertEqual((code, out["error"]), (400, "bad_timings"))
        code, out = self.worker("PUT", f"/api/voice/{eid}/timings", body=b"{nope", ctype="application/json")
        self.assertEqual((code, out["error"]), (400, "bad_json"))
        code, out = self.worker("PUT", f"/api/voice/{eid}/timings", body=None, length=voices.TIMINGS_MAX + 1,
                                ctype="application/json")
        self.assertEqual(code, 413)
        self.assertEqual(self.h.request("PUT", f"/api/voice/{eid}/timings", doc, user=a)[0], 401)
        self.assertEqual(self.worker("PUT", "/api/voice/e_nosuchepisode/timings", doc)[0], 404)
        code, out = self.worker("PUT", f"/api/voice/{eid}/timings", doc, headers={"X-Worker": "w9"})
        self.assertEqual((code, out["error"]), (409, "claimed_by_other"))
        # the MP3 lands: the timings are the audio's now
        self.assertEqual(self.upload(eid)[0], 200)
        f = self.files(eid)
        self.assertEqual(list(f), ["timings.json"])
        self.assertEqual(f["timings.json"]["segments"], doc["segments"])
        self.assertTrue(self.lib_voice(eid, a)["voice"]["timings"])
        code, t = self.web("GET", f"/api/episodes/{eid}/timings", a)
        self.assertEqual((code, t["version"], t["rev"], t["segments"]), (200, 1, 1, doc["segments"]))
        self.assertEqual(self.h.request("GET", f"/api/episodes/{eid}/timings")[0], 401)
        # afterwards (the backfill), for the audio there is; not timings that outrun it
        doc2 = timings(6, per=3.0)
        code, out = self.worker("PUT", f"/api/voice/{eid}/timings", doc2)
        self.assertEqual((code, out["stored"]), (200, "current"))
        self.assertEqual(self.files(eid)["timings.json"]["segments"], doc2["segments"])
        code, out = self.worker("PUT", f"/api/voice/{eid}/timings", timings(40))
        self.assertEqual((code, out["error"]), (409, "wrong_audio"))
        # a deleted episode's timings are refused
        with db.transaction() as c:
            c.execute("UPDATE episodes SET deleted_at = ? WHERE id = ?", (db.now(), eid))
        self.assertEqual(self.worker("PUT", f"/api/voice/{eid}/timings", doc2)[0], 410)

    def test_timings_that_do_not_fit_the_audio_are_dropped(self):
        a = self.h.user("Abe")
        eid = self.episode(a)
        self.claim(eid)
        self.worker("PUT", f"/api/voice/{eid}/timings", timings(50))       # 200 s of sentences
        self.assertEqual(self.upload(eid, dur="60.0")[0], 200)
        self.assertEqual(self.files(eid), {})
        self.assertFalse(self.lib_voice(eid, a)["voice"]["timings"])
        self.assertTrue(self.web("GET", f"/api/episodes/{eid}/timings", a)[1]["estimated"])     # the page estimates instead

    # -- changing the voice
    def test_change_by_the_maker_allowed_by_others_refused(self):
        maker, other = self.h.user("Mia"), self.h.user("Oli")
        viewer, admin = self.h.user("Vic", "viewer"), self.h.user("Ari", "admin")
        eid, eid2 = self.voiced(maker, timings(4)), self.voiced(maker)
        v = self.ep_view(eid, maker)
        self.assertEqual((v["id"], v["name"], v["rev"], v["pending"], v["can_change"]),
                         ("clear-female", "Clear female, measured", 1, None, True))
        self.assertFalse(self.ep_view(eid, other)["can_change"])
        e = self.lib_voice(eid, other)
        self.assertEqual((e["voice"]["id"], e["voice"]["can_change"]), ("clear-female", False))
        for who, code_want in ((other, 403), (viewer, 403)):
            code, out = self.web("PUT", f"/api/episodes/{eid}/voice", who, {"voice": "warm-male"})
            self.assertEqual((code, out["error"]), (code_want, "not_yours"))
        self.assertEqual(self.web("PUT", f"/api/episodes/{eid}/voice", maker, {"voice": "warm-male"}, pcg=False)[0], 403)
        self.assertEqual(self.web("PUT", f"/api/episodes/{eid}/voice", maker, {"voice": "nobody"})[1]["error"],
                         "no_such_voice")
        code, v = self.web("PUT", f"/api/episodes/{eid}/voice", maker, {"voice": "warm-male"})
        self.assertEqual(code, 200, v)
        self.assertEqual((v["id"], v["pending"]["id"], v["pending"]["name"], v["pending"]["state"], v["pending"]["position"]),
                         ("clear-female", "warm-male", "Warm male", "queued", 1))
        job = db.conn().execute("SELECT state, user_id, attempts FROM voice_jobs WHERE episode_id = ?", (eid,)).fetchone()
        self.assertEqual((job["state"], job["user_id"], job["attempts"]), ("queued", maker["id"], 0))
        e = self.lib_voice(eid, maker)
        self.assertTrue(e["has_audio"], "the old audio plays on")
        self.assertEqual(e["voice"]["pending"]["id"], "warm-male")
        # an admin may change anyone's; an episode without audio yet cannot be changed
        self.assertEqual(self.web("PUT", f"/api/episodes/{eid2}/voice", admin, {"voice": "calm-male"})[0], 200)
        self.assertEqual(self.ep_view(eid2, maker)["pending"]["by"], admin["id"])
        eid3 = self.episode(maker)
        code, out = self.web("PUT", f"/api/episodes/{eid3}/voice", maker, {"voice": "warm-male"})
        self.assertEqual((code, out["error"]), (409, "not_ready"))

    def test_the_change_goes_through_the_queue_and_swaps_in(self):
        maker, listener = self.h.user("Max"), self.h.user("Lou", "viewer")
        texts = [f"The {w} sentence of the script." for w in ("first", "second", "third", "fourth", "fifth")]
        old = timings(5, per=4.0, texts=texts)
        eid = self.voiced(maker, old)
        pid = db.conn().execute("SELECT paper_id FROM episodes WHERE id = ?", (eid,)).fetchone()[0]
        # the listener stopped in the middle of the third sentence (8.46 to 12.16 s)
        code, _ = self.web("PUT", f"/api/episodes/{eid}/position", listener, {"s": 10.31, "at": 1_700_000_000_000})
        self.assertEqual(code, 200)
        self.assertEqual(self.web("PUT", f"/api/episodes/{eid}/voice", maker, {"voice": "warm-male"})[0], 200)
        job = self.claim(eid)
        self.assertEqual(job["voice"]["id"], "warm-male")
        self.assertEqual(job["voice"]["spec"], {"engine": "breeze", "voice": "preset-warm-male-s42", "id": "warm-male",
                                                "instruction": voices.PRESET_BY_ID["warm-male"]["instruction"],
                                                "seed": 42})
        self.assertFalse(job["voice"]["cpu"])
        # while it is made: the episode stays ready with its old audio and timings
        code, out = self.worker("PUT", f"/api/voice/{eid}/status", {"phase": "speaking", "progress": 0.5})
        self.assertEqual((code, out["state"]), (200, "ready"))
        st = db.conn().execute("SELECT state, state_detail FROM episodes WHERE id = ?", (eid,)).fetchone()
        self.assertEqual(st["state"], "ready")
        p = self.ep_view(eid, maker)["pending"]
        self.assertEqual((p["state"], p["phase"], p["progress"]), ("working", "speaking", 0.5))
        code, body = self.web("GET", f"/audio/{eid}.mp3", listener)
        self.assertEqual((code, body), (200, MP3))
        new = timings(5, per=6.0, texts=texts)
        self.assertEqual(self.worker("PUT", f"/api/voice/{eid}/timings", new)[1]["stored"], "next")
        self.assertEqual(self.files(eid)["timings.json"]["segments"], old["segments"])
        # a change is refused while it is being made
        code, out = self.web("PUT", f"/api/episodes/{eid}/voice", maker, {"voice": "calm-male"})
        self.assertEqual((code, out["error"]), (409, "busy"))
        # the new MP3 lands
        sub = events.subscribe(listener["id"])
        try:
            code, out = self.upload(eid, data=MP3_2, dur="31.5", key="preset-warm-male-s42")
            self.assertEqual(code, 200, out)
            got = []
            while not sub.q.empty():
                got.append(sub.q.get_nowait())
        finally:
            events.unsubscribe(sub)
        swaps = [d for _i, kind, d in got if kind == "episode" and d.get("voice_swap")]
        self.assertEqual(swaps, [{"id": eid, "episode_id": eid, "paper_id": pid,
                                  "voice_swap": {"rev": 2, "from_rev": 1, "voice": "warm-male", "name": "Warm male"}}])
        code, body = self.web("GET", f"/audio/{eid}.mp3?v=2", listener)
        self.assertEqual((code, body), (200, MP3_2))
        v = self.ep_view(eid, maker)
        self.assertEqual((v["id"], v["name"], v["rev"], v["pending"], v["error"]), ("warm-male", "Warm male", 2, None, None))
        f = self.files(eid)
        self.assertEqual((f["timings.json"]["segments"], f["timings.prev.json"]["segments"]),
                         (new["segments"], old["segments"]))
        e = self.lib_voice(eid, listener)
        self.assertEqual((e["duration_s"], e["voice"]["rev"], e["voice"]["name"]), (31.5, 2, "Warm male"))
        # the listener's place: the same sentence, as far into it (third: 12.46 to 18.16 s)
        pos = db.conn().execute("SELECT seconds FROM positions WHERE episode_id = ? AND user_id = ?",
                                (eid, listener["id"])).fetchone()[0]
        self.assertAlmostEqual(pos, 12.46 + (10.31 - 8.46) / 3.7 * 5.7, delta=0.1)
        self.assertAlmostEqual(e["position_s"], pos, delta=0.01)
        # a page still on the old audio asks where it is now
        code, v = self.web("GET", f"/api/episodes/{eid}/voice?at=10.31&rev=1", listener)
        self.assertAlmostEqual(v["at"], pos, delta=0.1)
        code, v = self.web("GET", f"/api/episodes/{eid}/voice?at=10.31&rev=2", listener)
        self.assertEqual(v["at"], 10.31)
        # the page reads the new audio's timings, with the revision they are for
        code, t = self.web("GET", f"/api/episodes/{eid}/timings", listener)
        self.assertEqual((code, t["rev"], t["segments"]), (200, 2, new["segments"]))
        self.assertEqual(self.claim_nothing(), 204)

    def claim_nothing(self):
        return self.worker("POST", "/api/voice/claim", {"worker": "w1"})[0]

    def test_positions_without_timings_follow_the_length(self):
        maker = self.h.user("Ned")
        eid = self.voiced(maker)
        self.web("PUT", f"/api/episodes/{eid}/position", maker, {"s": 50.0, "at": 1_700_000_000_000})
        self.web("PUT", f"/api/episodes/{eid}/voice", maker, {"voice": "basic-female"})
        job = self.claim(eid)
        self.assertEqual((job["voice"]["cpu"], job["voice"]["spec"]),
                         (True, {"engine": "kokoro", "voice": "af_heart", "id": "basic-female"}))
        self.assertEqual(self.upload(eid, data=MP3_2, dur="80.0", key="af_heart")[0], 200)
        pos = db.conn().execute("SELECT seconds FROM positions WHERE episode_id = ?", (eid,)).fetchone()[0]
        self.assertEqual(pos, 40.0)
        self.assertEqual(self.ep_view(eid, maker)["id"], "basic-female")

    def test_at_most_two_waiting_per_person(self):
        maker, other = self.h.user("Pat"), self.h.user("Qui")
        e1, e2, e3 = self.voiced(maker), self.voiced(maker), self.voiced(maker)
        mine = self.voiced(other)
        for eid in (e1, e2):
            self.assertEqual(self.web("PUT", f"/api/episodes/{eid}/voice", maker, {"voice": "warm-male"})[0], 200)
        # another voice for one already waiting is not a third
        self.assertEqual(self.web("PUT", f"/api/episodes/{e2}/voice", maker, {"voice": "calm-male"})[0], 200)
        self.assertEqual(self.ep_view(e2, maker)["pending"]["id"], "calm-male")
        code, out = self.web("PUT", f"/api/episodes/{e3}/voice", maker, {"voice": "warm-male"})
        self.assertEqual((code, out["error"]), (429, "too_many"))
        # someone else's count is their own
        self.assertEqual(self.web("PUT", f"/api/episodes/{mine}/voice", other, {"voice": "warm-male"})[0], 200)
        # taking one back, or one finishing, frees a place
        code, v = self.web("DELETE", f"/api/episodes/{e1}/voice", maker)
        self.assertEqual((code, v["pending"]), (200, None))
        self.assertEqual(db.conn().execute("SELECT state FROM voice_jobs WHERE episode_id = ?", (e1,)).fetchone()[0], "done")
        self.assertEqual(self.web("PUT", f"/api/episodes/{e3}/voice", maker, {"voice": "warm-male"})[0], 200)
        self.assertEqual(self.web("PUT", f"/api/episodes/{e1}/voice", maker, {"voice": "warm-male"})[1]["error"], "too_many")
        # the fair order: Qui was served least recently... each person's changes queue under them
        code, q = self.worker("GET", "/api/voice/queue")
        self.assertEqual(sorted(j["episode_id"] for j in q["queued"]), sorted([e2, e3, mine]))
        got = []
        for _ in range(3):
            job = self.claim()
            got.append(job["episode_id"])
            self.upload(job["episode_id"], data=MP3_2, key=job["voice"]["spec"]["voice"])
        self.assertEqual(set(got), {e2, e3, mine})
        self.assertEqual(self.web("PUT", f"/api/episodes/{e1}/voice", maker, {"voice": "warm-male"})[0], 200)
        # choosing the voice it already has takes a waiting change back
        self.assertEqual(self.web("PUT", f"/api/episodes/{e1}/voice", maker, {"voice": "clear-female"})[1]["pending"], None)

    def test_a_failed_change_keeps_the_old_audio(self):
        maker = self.h.user("Ray")
        eid = self.voiced(maker, timings(3))
        self.web("PUT", f"/api/episodes/{eid}/voice", maker, {"voice": "british-female"})
        for attempt in (1, 2, 3):
            job = self.claim(eid)
            self.assertEqual(job["attempt"], attempt)
            code, out = self.worker("POST", f"/api/voice/{eid}/failed", {"error": "gpu_oom: out of memory"})
            self.assertEqual((code, out["retry"]), (200, attempt < 3))
            st = db.conn().execute("SELECT state FROM episodes WHERE id = ?", (eid,)).fetchone()[0]
            self.assertEqual(st, "ready", "a failed change never makes the episode failed")
            if attempt < 3:
                self.assertEqual(self.ep_view(eid, maker)["pending"]["state"], "retrying")
        v = self.ep_view(eid, maker)
        self.assertEqual((v["id"], v["pending"]), ("clear-female", None))
        self.assertIn("British female, composed: gpu_oom", v["error"])
        self.assertEqual(db.conn().execute("SELECT state FROM voice_jobs WHERE episode_id = ?", (eid,)).fetchone()[0], "done")
        self.assertEqual(self.claim_nothing(), 204)
        self.assertEqual(self.web("GET", f"/audio/{eid}.mp3", maker)[1], MP3)
        # dismissing the error
        self.assertIsNone(self.web("DELETE", f"/api/episodes/{eid}/voice", maker)[1]["error"])

    def test_first_voicing_names_its_voice(self):
        a = self.h.user("Sam")
        eid = self.episode(a)
        self.assertIsNone(self.claim(eid)["voice"], "papercast-voice's default: nothing to pass")
        self.upload(eid, key=None)
        self.assertEqual(self.ep_view(eid, a)["id"], "clear-female")
        eid2 = self.voiced(a, key="some-voice-the-hub-does-not-know")
        v = self.ep_view(eid2, a)
        self.assertEqual((v["id"], v["name"], v["rev"]), (None, None, 1))

    def test_my_voice_is_used_for_my_new_episodes(self):
        a, b = self.h.user("Uma"), self.h.user("Val")
        code, j = self.web("GET", "/api/voices", a)
        self.assertEqual(j["mine"], "clear-female", "nobody chose: the default")
        self.assertEqual(self.web("PUT", "/api/voices/mine", a, {"voice": "nobody"})[1]["error"], "no_such_voice")
        self.assertEqual(self.web("PUT", "/api/voices/mine", a, {"voice": "warm-male"}, pcg=False)[0], 403)
        code, out = self.web("PUT", "/api/voices/mine", a, {"voice": "warm-male"})
        self.assertEqual((code, out), (200, {"mine": "warm-male", "name": "Warm male"}))
        self.assertEqual(self.web("GET", "/api/voices", a)[1]["mine"], "warm-male")
        self.assertEqual(self.web("GET", "/api/voices", b)[1]["mine"], "clear-female", "each person's own")
        # Uma's new episode is voiced in hers, Val's in the default
        e1 = self.episode(a)
        job = self.claim(e1)
        self.assertEqual((job["voice"]["id"], job["voice"]["spec"]["voice"]), ("warm-male", "preset-warm-male-s42"))
        self.upload(e1, key="preset-warm-male-s42")
        self.assertEqual(self.ep_view(e1, a)["name"], "Warm male")
        e2 = self.episode(b)
        self.assertIsNone(self.claim(e2)["voice"])
        self.upload(e2)
        self.assertEqual(self.ep_view(e2, b)["id"], "clear-female")
        # a worker that does not say which voice it used: the one the claim asked for
        self.web("PUT", "/api/voices/mine", a, {"voice": "basic-female"})
        e3 = self.episode(a)
        self.assertTrue(self.claim(e3)["voice"]["cpu"])
        self.upload(e3, key=None)
        self.assertEqual(self.ep_view(e3, a)["id"], "basic-female")
        # the default chosen explicitly is still the default: nothing to pass
        self.web("PUT", "/api/voices/mine", a, {"voice": "clear-female"})
        e4 = self.episode(a)
        self.assertIsNone(self.claim(e4)["voice"])

    def test_earlier_episodes_get_their_voice_from_the_job_dir(self):
        a = self.h.user("Tia")
        eid = self.voiced(a)
        with db.transaction() as c:
            c.execute("DELETE FROM episode_voice WHERE episode_id = ?", (eid,))
        vdir = self.h.cfg.episodes / eid / "voice"
        vdir.mkdir()
        (vdir / "status.json").write_text(json.dumps({"phase": "done", "output": {"voice": "preset-calm-male-s42"}}))
        voices.start(self.h.cfg)
        self.assertEqual(self.ep_view(eid, a)["id"], "calm-male")

    def test_remap(self):
        o = timings(3, per=4.0, texts=["a.", "b.", "c."])
        n = timings(3, per=8.0, texts=["a.", "b.", "c."])
        self.assertAlmostEqual(voices.remap(4.46 + 1.0, o, n, 13.46, 25.46), 8.46 + 1.0 / 3.7 * 7.7, places=1)
        self.assertEqual(voices.remap(0.0, o, n, 13.46, 25.46), 0.0)
        self.assertEqual(voices.remap(13.0, o, n, 13.46, 25.46), 25.5, "finished stays finished")
        self.assertEqual(voices.remap(5.0, o, timings(2), 10.0, 20.0), 10.0, "other sentences: by length")
        self.assertEqual(voices.remap(5.0, None, None, 10.0, 20.0), 10.0)


if __name__ == "__main__":
    unittest.main()
