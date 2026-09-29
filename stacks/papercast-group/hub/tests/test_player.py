#!/usr/bin/env python3
"""Up next and the play-along transcript (hub/player.py and the page's player).

    python3 -m unittest tests.test_player -v        (from stacks/papercast-group/hub)

The hub: each person's queue, in order (add, play next, move, remove, clear, the whole order
with its rev), a deleted episode dropping out; timings from the voice worker's timings.json,
else an estimate from script.md (sentences as common/checks.sentences cuts them, each a share
of the audio in proportion to its characters); the transcript tying the script's words to them.
The page, in a real headless Chrome against the real hub (the auth stand-in of web_rig.py, as
PCG_AUTH=header): Play next and Add to Up next in the menus, the Up next panel (arrows, drag,
remove, clear), the next episode playing at the end (from the window and from the player bar on
a phone's list), window.papercastQueue, the transcript's sentence moving with currentTime and the
transcript rolling with it while it plays (and not once scrolled by hand, until Back to now or
Play), tap to seek, find; no console errors and no CSP violations; 44 x 44 px taps on a phone.

Fake titles, people and scripts only. The browser part skips without a headless Chrome or
`websocket-client`."""
from __future__ import annotations

import json
import os
import sys
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from web_rig import Rig  # noqa: E402

from hub import events, player  # noqa: E402

from papercast_cli.common import checks  # noqa: E402  (on the path through hub.web)

A, B, C = "alice@example.org", "bob@example.org", "carol@example.org"


def fake_script(paragraphs: int = 3, sentences: int = 3) -> str:
    """Headings and paragraphs of plain, made-up sentences (no digits, as a real script)."""
    words = ["plain", "fake", "words", "about", "nothing", "listening", "along", "slowly", "quietly", "again"]
    names = ["Opening", "The method", "What it shows", "Where it stops", "Closing"]
    out = []
    for p in range(paragraphs):
        if p % 3 == 0:
            out.append(f"# {names[(p // 3) % len(names)]}")
        sents = []
        for s in range(sentences):
            w = [words[(p * 7 + s * 3 + k) % len(words)] for k in range(6 + (p + s) % 5)]
            sents.append(" ".join(w).capitalize() + ".")
        out.append(" ".join(sents))
    return "\n\n".join(out) + "\n"


def voice_timings(script: str, total: float, speak_headings: bool = True, split_long: int = 0) -> dict:
    """What the voice worker writes: one segment a sentence, in order; a heading said with a
    full stop (or not at all); a sentence over `split_long` words cut in two."""
    segs, texts = [], []
    heads = {" ".join(line.lstrip("#").split()) for line in script.splitlines() if line.startswith("#")}
    for s in checks.sentences(script):
        if s in heads:
            if speak_headings:
                texts.append(s + ".")
            continue
        w = s.split()
        if split_long and len(w) > split_long:
            texts += [" ".join(w[:len(w) // 2]), " ".join(w[len(w) // 2:])]
        else:
            texts.append(s)
    step = total / len(texts)
    for i, t in enumerate(texts):
        segs.append({"start": round(i * step, 3), "end": round((i + 1) * step - 0.05, 3), "text": t})
    return {"version": 1, "segments": segs}


class HubBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.r = Rig()
        r = cls.r
        cls.alice = r.user(A, "Alice", "admin")
        cls.bob = r.user(B, "Bob", "contributor")
        cls.carol = r.user(C, "Carol", "viewer")

    @classmethod
    def tearDownClass(cls):
        cls.r.close()


class Queue(HubBase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        r = cls.r
        cls.p1 = r.paper("First Fake Paper", cls.alice)
        cls.e1 = r.episode(cls.p1, cls.alice)
        cls.p2 = r.paper("Second Fake Paper", cls.bob)
        cls.e2 = r.episode(cls.p2, cls.bob)
        cls.p3 = r.paper("Third Fake Paper", cls.alice)
        cls.e3 = r.episode(cls.p3, cls.alice)
        cls.p4 = r.paper("Fourth Fake Paper, Two Versions", cls.alice)
        cls.e4a = r.episode(cls.p4, cls.alice)
        cls.e4b = r.episode(cls.p4, cls.bob)
        cls.pr = r.paper("A Rejected Fake Paper", cls.bob)
        cls.er = r.episode(cls.pr, cls.bob, state="rejected")

    def setUp(self):
        self.r.q("DELETE FROM up_next")
        self.r.q("DELETE FROM up_next_rev")
        self.r.q("UPDATE episodes SET deleted_at = NULL WHERE state != 'rejected'")

    def get(self, user=A):
        code, j, _ = self.r.req("GET", "/api/queue", user=user)
        self.assertEqual(code, 200, j)
        return j

    def put(self, body, user=A, code=200):
        got, j, _ = self.r.req("PUT", "/api/queue", body, user=user)
        self.assertEqual(got, code, j)
        return j

    def test_empty(self):
        self.assertEqual(self.get(), {"ids": [], "rev": 0, "papers": []})

    def test_add_next_move_remove_clear(self):
        j = self.put({"op": "add", "ids": [self.e1, self.e2]})
        self.assertEqual((j["ids"], j["rev"]), ([self.e1, self.e2], 1))
        # the papers come with it, as the library shows them, in the queue's order
        self.assertEqual([p["id"] for p in j["papers"]], [self.p1, self.p2])
        self.assertEqual(j["papers"][1]["episodes"][0]["made_by"]["name"], "Bob")
        # adding one already queued leaves it where it is
        self.assertEqual(self.put({"op": "add", "ids": [self.e1, self.e3]})["ids"], [self.e1, self.e2, self.e3])
        # play next: first, moved there if queued; several keep their order
        self.assertEqual(self.put({"op": "next", "id": self.e3})["ids"], [self.e3, self.e1, self.e2])
        self.assertEqual(self.put({"op": "next", "ids": [self.e4b, self.e2]})["ids"], [self.e4b, self.e2, self.e3, self.e1])
        # move: to a place, clamped to the ends
        self.assertEqual(self.put({"op": "move", "id": self.e1, "to": 0})["ids"], [self.e1, self.e4b, self.e2, self.e3])
        self.assertEqual(self.put({"op": "move", "id": self.e1, "to": 99})["ids"], [self.e4b, self.e2, self.e3, self.e1])
        self.assertEqual(self.put({"op": "move", "id": self.e3, "to": 1})["ids"], [self.e4b, self.e3, self.e2, self.e1])
        # remove
        j = self.put({"op": "remove", "ids": [self.e2, self.e4b]})
        self.assertEqual(j["ids"], [self.e3, self.e1])
        rev = j["rev"]
        # nothing changed: the same rev, no event
        self.assertEqual(self.put({"op": "remove", "ids": [self.e2]})["rev"], rev)
        self.assertEqual(self.put({"op": "move", "id": self.e3, "to": 0})["rev"], rev)
        # what the hub keeps is what it answered
        self.assertEqual(self.get()["ids"], [self.e3, self.e1])
        self.assertEqual(self.put({"op": "clear"})["ids"], [])
        self.assertEqual(self.get()["ids"], [])

    def test_the_whole_order_and_its_rev(self):
        j = self.put({"op": "add", "ids": [self.e1, self.e2, self.e3]})
        j = self.put({"ids": [self.e3, self.e1, self.e2], "rev": j["rev"]})
        self.assertEqual(j["ids"], [self.e3, self.e1, self.e2])
        # another device changed it meanwhile: the old rev is refused, nothing changes
        self.put({"op": "remove", "ids": [self.e2]})
        stale = self.put({"ids": [self.e1, self.e3, self.e2], "rev": j["rev"]}, code=409)
        self.assertEqual((stale["error"], stale["ids"]), ("moved", [self.e3, self.e1]))
        self.assertEqual(self.get()["ids"], [self.e3, self.e1])
        # without a rev the order is simply set (duplicates once, dead ids left out); [] clears
        self.assertEqual(self.put({"ids": [self.e2, self.e2, self.er, self.e1]})["ids"], [self.e2, self.e1])
        self.assertEqual(self.put({"ids": []})["ids"], [])

    def test_each_persons_own(self):
        self.put({"op": "add", "ids": [self.e1, self.e2]}, user=A)
        self.put({"op": "add", "ids": [self.e2]}, user=B)
        self.put({"op": "remove", "ids": [self.e2]}, user=A)
        self.assertEqual(self.get(A)["ids"], [self.e1])
        self.assertEqual(self.get(B)["ids"], [self.e2])
        self.assertEqual(self.get(C), {"ids": [], "rev": 0, "papers": []})
        self.put({"op": "clear"}, user=B)
        self.assertEqual(self.get(A)["ids"], [self.e1])

    def test_a_deleted_episode_drops_out(self):
        self.put({"op": "add", "ids": [self.e1, self.e4a, self.e2]}, user=C)
        # Alice deletes her version: out of Carol's queue at once, Bob's version stays queueable
        code, _, _ = self.r.req("DELETE", f"/api/episodes/{self.e4a}", user=A)
        self.assertEqual(code, 200)
        j = self.get(C)
        self.assertEqual(j["ids"], [self.e1, self.e2])
        self.assertEqual({p["id"] for p in j["papers"]}, {self.p1, self.p2})
        # it cannot be queued again while deleted
        self.put({"op": "add", "ids": [self.e4a]}, user=C, code=404)
        self.put({"op": "next", "ids": [self.e4a, self.e4b]}, user=C)
        # Undo before any change brought it back; after one, it is gone for good
        self.assertEqual(self.r.q("SELECT count(*) FROM up_next WHERE episode_id = ?", self.e4a)[0][0], 0)
        self.r.req("POST", f"/api/episodes/{self.e4a}/undelete", {}, user=A)
        self.assertEqual(self.get(C)["ids"], [self.e4b, self.e1, self.e2])
        # a whole paper gone (its only version deleted) likewise
        self.r.req("DELETE", f"/api/episodes/{self.e2}", user=B)
        self.assertEqual(self.get(C)["ids"], [self.e4b, self.e1])
        self.r.req("POST", f"/api/episodes/{self.e2}/undelete", {}, user=B)
        self.assertEqual(self.get(C)["ids"], [self.e4b, self.e1, self.e2])      # no change in between: back

    def test_refusals(self):
        r = self.r
        code, j, _ = r.req("PUT", "/api/queue", {"op": "add", "ids": [self.e1]}, headers={"X-PCG": None})
        self.assertEqual(code, 403)
        self.assertEqual(self.get()["ids"], [])
        self.assertEqual(self.put({"op": "shuffle"}, code=400)["error"], "bad_op")
        self.assertEqual(self.put({}, code=400)["error"], "bad_op")
        self.assertEqual(self.put({"op": "add", "ids": ["p_notanepisode"]}, code=400)["error"], "bad_ids")
        self.assertEqual(self.put({"op": "add", "ids": "e_x"}, code=400)["error"], "bad_ids")
        self.assertEqual(self.put({"op": "add", "ids": [self.e1] * 201}, code=400)["error"], "bad_ids")
        self.assertEqual(self.put({"op": "add", "ids": ["e_nosuchepisode"]}, code=404)["error"], "not_found")
        self.assertEqual(self.put({"op": "add", "ids": [self.er]}, code=404)["error"], "not_found")
        self.assertEqual(self.put({"op": "move", "id": self.e1, "to": 0}, code=404)["error"], "not_queued")
        self.put({"op": "add", "ids": [self.e1]})
        self.assertEqual(self.put({"op": "move", "id": self.e1, "to": "first"}, code=400)["error"], "bad_to")
        self.assertEqual(self.put({"op": "move", "id": self.e1, "to": True}, code=400)["error"], "bad_to")
        # at most 200
        self.r.q("DELETE FROM up_next")
        many = []
        for i in range(201):
            many.append(r.episode(self.p3, self.alice, audio_s=None, explainer=None))
        self.put({"ids": many[:200]})
        self.assertEqual(self.put({"op": "add", "ids": many[200:]}, code=400)["error"], "too_long")
        self.assertEqual(len(self.get()["ids"]), 200)
        r.q(f"UPDATE episodes SET deleted_at = '2026-09-01T00:00:00Z' WHERE id IN ({','.join('?' * len(many))})", *many)

    def test_other_devices_hear_of_a_change(self):
        a, b = events.subscribe(self.alice), events.subscribe(self.bob)
        try:
            j = self.put({"op": "add", "ids": [self.e1]})
            got = a.q.get(timeout=2)
            self.assertEqual(got[1:], ("queue", {"rev": j["rev"]}))
            self.assertTrue(b.q.empty())
            self.put({"op": "add", "ids": [self.e1]})           # no change, no event
            self.assertTrue(a.q.empty())
        finally:
            events.unsubscribe(a)
            events.unsubscribe(b)


class Timings(HubBase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        r = cls.r
        cls.script = fake_script(4, 3)
        cls.p = r.paper("A Fake Paper With A Script", cls.alice)
        cls.e = r.episode(cls.p, cls.alice, duration=120.0)
        cls.dir = r.cfg.episodes / cls.e
        (cls.dir / "script.md").write_text(cls.script)

    def tearDown(self):
        (self.dir / "timings.json").unlink(missing_ok=True)
        self.r.q("UPDATE episodes SET duration_s = 120, deleted_at = NULL WHERE id = ?", self.e)

    def timings(self, code=200, eid=None):
        got, j, _ = self.r.req("GET", f"/api/episodes/{eid or self.e}/timings")
        self.assertEqual(got, code, j)
        return j

    def transcript(self, eid=None):
        got, j, _ = self.r.req("GET", f"/api/episodes/{eid or self.e}/transcript")
        self.assertEqual(got, 200, j)
        return j

    def test_an_estimate_without_the_file(self):
        t = self.timings()
        sents = checks.sentences(self.script)
        self.assertEqual((t["version"], t["estimated"], t["duration_s"]), (1, True, 120.0))
        self.assertEqual([s["text"] for s in t["segments"]], sents)
        # back to back from 0 to the audio's length, each in proportion to its characters
        segs = t["segments"]
        self.assertEqual(segs[0]["start"], 0)
        self.assertEqual(segs[-1]["end"], 120.0)
        for x, y in zip(segs, segs[1:]):
            self.assertAlmostEqual(x["end"], y["start"], places=2)
        total = sum(map(len, sents))
        for s in segs:
            self.assertAlmostEqual(s["end"] - s["start"], 120.0 * len(s["text"]) / total, places=2)
        # no length known yet (still being voiced): about fifteen characters a second
        self.r.q("UPDATE episodes SET duration_s = NULL WHERE id = ?", self.e)
        self.assertEqual(self.timings()["duration_s"], round(total / 15.0, 1))

    def test_the_file_when_it_is_there(self):
        v = voice_timings(self.script, 118.5)
        (self.dir / "timings.json").write_text(json.dumps(v))
        t = self.timings()
        self.assertEqual(t["estimated"], False)
        self.assertEqual(t["segments"], v["segments"])
        # one the hub cannot read is not served: the estimate is
        for bad in ("not json", json.dumps({"version": 2, "segments": []}),
                    json.dumps({"version": 1, "segments": [{"start": "a", "end": 1, "text": "x"}]})):
            (self.dir / "timings.json").write_text(bad)
            with self.assertLogs("pcg.player", "WARNING"):
                self.assertTrue(self.timings()["estimated"], bad)
        # nor one that is a link to somewhere else
        (self.dir / "timings.json").unlink()
        other = self.r.data / "elsewhere.json"
        other.write_text(json.dumps(v))
        os.symlink(other, self.dir / "timings.json")
        self.assertTrue(self.timings()["estimated"])

    def test_no_script(self):
        e = self.r.episode(self.p, self.bob)
        self.assertEqual(self.timings(404, e)["error"], "no_script")
        self.assertEqual(self.transcript(e)["blocks"], [])         # 200: the page just shows none
        # a deleted or rejected episode has neither
        self.r.q("UPDATE episodes SET deleted_at = '2026-09-01T00:00:00Z' WHERE id = ?", self.e)
        self.timings(404)
        got, _, _ = self.r.req("GET", f"/api/episodes/{self.e}/transcript")
        self.assertEqual(got, 404)

    def test_the_transcript_is_the_script_tied_to_the_timings(self):
        tr = self.transcript()
        kinds = [b["kind"] for b in tr["blocks"]]
        self.assertEqual(kinds, ["heading", "para", "para", "para", "heading", "para"])
        # every word of the script, in order, and nothing else
        words = [w for b in tr["blocks"] for p in b["parts"] for w in p["text"].split()]
        self.assertEqual(words, [w for line in self.script.splitlines() for w in line.lstrip("#").split()])
        # the estimate: one part per sentence, the segments in order
        segs = [p["seg"] for b in tr["blocks"] for p in b["parts"]]
        self.assertEqual(segs, list(range(len(checks.sentences(self.script)))))
        self.assertEqual(len(tr["segments"]), len(segs))
        self.assertTrue(tr["estimated"])

    def test_a_voice_that_cut_it_differently_still_lines_up(self):
        v = voice_timings(self.script, 100.0, speak_headings=False, split_long=8)
        (self.dir / "timings.json").write_text(json.dumps(v))
        tr = self.transcript()
        self.assertFalse(tr["estimated"])
        self.assertEqual(len(tr["segments"]), len(v["segments"]))
        heads = [b for b in tr["blocks"] if b["kind"] == "heading"]
        self.assertTrue(all(p["seg"] is None for b in heads for p in b["parts"]))      # not said
        # each paragraph part says exactly its segment's words
        said = {}
        for b in tr["blocks"]:
            for p in b["parts"]:
                if p["seg"] is not None:
                    said.setdefault(p["seg"], []).append(p["text"])
        self.assertEqual({k: " ".join(x) for k, x in said.items()},
                         {i: s["text"] for i, s in enumerate(v["segments"])})
        # headings said with a full stop: tied to their segment
        v = voice_timings(self.script, 100.0)
        (self.dir / "timings.json").write_text(json.dumps(v))
        tr = self.transcript()
        first = tr["blocks"][0]
        self.assertEqual(first["parts"], [{"text": "Opening", "seg": 0}])
        self.assertEqual(v["segments"][0]["text"], "Opening.")

    def test_estimate_alone(self):
        t = player.estimate("")
        self.assertEqual(t["segments"], [])
        t = player.estimate("# A heading\n\nOne sentence here. Another one there.\n", 10)
        self.assertEqual([s["text"] for s in t["segments"]], ["A heading", "One sentence here.", "Another one there."])
        self.assertEqual(t["segments"][-1]["end"], 10)


# ---------------------------------------------------------------- the page

import test_page as tp  # noqa: E402  (not `from ... import`: its test classes would run twice)

try:
    import websocket  # noqa: F401
    from web_cdp import find_chrome
    SKIP = None if find_chrome() else "no headless Chrome"
except ImportError:
    SKIP = "websocket-client not installed"

# test_page's tap-target check, with the Up next panel as a top layer like the explainer
_TOP = "const top = !document.getElementById('overlay').hidden ? document.getElementById('overlay') : document.querySelector('.menu');"
assert _TOP in tp.TAP_TARGETS
TAP_TARGETS = tp.TAP_TARGETS.replace(_TOP, "const top = !document.getElementById('overlay').hidden ? document.getElementById('overlay')"
                                     " : !document.getElementById('q-overlay').hidden ? document.getElementById('q-overlay') : document.querySelector('.menu');")
AUDIO = "document.getElementById('audio')"
SEG = "document.querySelector('#tr-body .tr-s[data-seg=\"{}\"]')"
NOW = "(document.querySelector('#tr-body .tr-s.now') || {dataset: {}}).dataset.seg"


# What scrolls the transcript: the middle column beside the comments, else (a phone) the window.
SCROLLER = ("((m) => getComputedStyle(m).overflowY === 'auto' ? m : document.getElementById('win'))"
            "(document.getElementById('p-mid'))")


def in_view(node: str) -> str:
    """JS: is this node on screen where the transcript scrolls (under the phone's top bar)?"""
    return (f"((n) => {{ if (!n) return false; const w = {SCROLLER}.getBoundingClientRect(), pt = document.querySelector('#paper > .phone-top');"
            " const top = pt && pt.getClientRects().length ? Math.max(w.top, pt.getBoundingClientRect().bottom) : w.top, r = n.getBoundingClientRect();"
            f" return r.bottom > top + 8 && r.top < w.bottom - 8; }})({node})")


def at_third(node: str) -> str:
    """JS: is this node's top between a fifth and a half of the way down that view (rolling)?"""
    return (f"((n) => {{ if (!n) return false; const w = {SCROLLER}.getBoundingClientRect(), r = n.getBoundingClientRect();"
            f" return r.top > w.top + w.height * 0.18 && r.top < w.top + w.height * 0.5; }})({node})")


# every CSP violation the page meets, from before its first script
CSP_WATCH = ("window.__csp = []; document.addEventListener('securitypolicyviolation',"
             " (e) => window.__csp.push(e.violatedDirective + ' ' + (e.blockedURI || '')));")


@unittest.skipIf(SKIP, SKIP or "")
class PlayerPage(tp.PageBase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.b.call("Page.addScriptToEvaluateOnNewDocument", source=CSP_WATCH)

    @classmethod
    def fill(cls):
        super().fill()
        r = cls.r
        # a long script with the voice worker's timings, and a short one without
        cls.read = r.paper("A Fake Paper To Read Along", cls.alice)
        cls.er = r.episode(cls.read, cls.alice, duration=30, audio_s=30)
        cls.script = fake_script(30, 4)
        cls.timed = voice_timings(cls.script, 29.5)
        (r.cfg.episodes / cls.er / "script.md").write_text(cls.script)
        (r.cfg.episodes / cls.er / "timings.json").write_text(json.dumps(cls.timed))
        cls.guess = r.paper("A Fake Paper Without Timings", cls.bob)
        cls.eg = r.episode(cls.guess, cls.bob, duration=30, audio_s=30)
        cls.short_script = fake_script(3, 3)
        (r.cfg.episodes / cls.eg / "script.md").write_text(cls.short_script)
        # three short ones to go through, and one still waiting for the GPU
        cls.short, cls.es = [], []
        for n in ("one", "two", "three"):
            p = r.paper(f"Short fake paper {n}", cls.alice)
            cls.short.append(p)
            cls.es.append(r.episode(p, cls.alice, duration=4, audio_s=4, summary="derivations"))
        cls.wait = r.paper("A Fake Paper Waiting", cls.bob)
        cls.ew = r.episode(cls.wait, cls.bob, state="waiting-for-gpu")

    def setUp(self):
        super().setUp()
        self.r.q("DELETE FROM up_next")
        self.r.q("DELETE FROM up_next_rev")

    def tearDown(self):
        try:
            self.assertEqual(self.b.js("JSON.stringify(window.__csp || [])"), "[]", "CSP violations")
        finally:
            self.b.js(f"{AUDIO}.pause(); localStorage.clear()")
            self.r.q("DELETE FROM positions")
            super().tearDown()

    # -- helpers
    def queue(self, who=A):
        return self.r.req("GET", "/api/queue", user=who)[1]["ids"]

    def set_queue(self, ids, who=A):
        self.assertEqual(self.r.req("PUT", "/api/queue", {"ids": ids}, user=who)[0], 200)

    def menu(self):
        return self.b.js("[...document.querySelectorAll('.menu [role=menuitem]')].map(x => x.textContent)")

    def pick(self, label):
        self.b.js(f"[...document.querySelectorAll('.menu [role=menuitem]')].find(x => x.textContent === {json.dumps(label)}).click()")

    def rows_in_panel(self):
        return self.b.js("[...document.querySelectorAll('#q-list .q-row')].map(x => x.dataset.ep)")

    def center(self, expr):
        return self.b.js(f"(() => {{ const r = ({expr}).getBoundingClientRect(); return [r.left + r.width / 2, r.top + r.height / 2]; }})()")

    def mouse(self, kind, x, y, **more):
        self.b.call("Input.dispatchMouseEvent", type=kind, x=x, y=y, **more)

    def seg_at(self, t, segs):
        return max(i for i, s in enumerate(segs) if s["start"] <= t)

    # -- tests
    def test_1_menus_and_the_up_next_panel(self):
        b = self.b
        e1, e2, e3 = self.es
        self.home(A)
        self.open(self.read)
        # a row's menu: Play next and Add to Up next, then what was there
        b.js(f"document.querySelector('{tp.ROW.format(self.short[0])} .more').click()")
        b.wait_js("!!document.querySelector('.menu')", 3, "row menu")
        self.assertEqual(self.menu(), ["Play next", "Add to Up next", "Delete my version"])
        self.pick("Add to Up next")
        self.r.wait(lambda: self.queue() == [e1], 5, "added on the hub")
        b.wait_js("document.getElementById('toast-msg').textContent === 'Added to Up next'", 3, "toast")
        b.js(f"document.querySelector('{tp.ROW.format(self.short[1])} .more').click()")
        self.pick("Play next")
        self.r.wait(lambda: self.queue() == [e2, e1], 5, "first on the hub")
        # queued already: the menu offers to take it out instead
        b.js(f"document.querySelector('{tp.ROW.format(self.short[0])} .more').click()")
        self.assertEqual(self.menu(), ["Play next", "Remove from Up next", "Delete my version"])
        b.js(f"document.querySelector('{tp.ROW.format(self.short[0])} .more').click()")
        # nothing to listen to yet: nothing to queue (an admin still gets Delete)
        b.js(f"document.querySelector('{tp.ROW.format(self.wait)} .more').click()")
        self.assertEqual(self.menu(), ["Delete Bob’s version"])
        b.js(f"document.querySelector('{tp.ROW.format(self.wait)} .more').click()")
        # the window's menu has them too
        b.js("document.getElementById('w-more').click()")
        self.assertEqual(self.menu()[:3], ["Play next", "Add to Up next", "Details"])
        b.js("document.getElementById('w-more').click()")
        # the player says what is next
        b.wait_js("!document.getElementById('p-next').hidden", 3, "Up next line")
        self.assertEqual(self.text("#p-next-t"), "Short fake paper two")
        self.assertEqual(self.text("#p-next-n"), "1 more")
        # the panel: rows in order, moved by an arrow (the focus stays on the row)
        b.js("document.getElementById('p-next').click()")
        b.wait_js("!document.getElementById('q-overlay').hidden && document.activeElement.id === 'q-close'", 3, "panel")
        self.assertEqual(self.rows_in_panel(), [e2, e1])
        self.assertEqual(self.text(f"#q-list .q-row[data-ep=\"{e2}\"] .q-t"), "Short fake paper two")
        self.assertEqual(self.text(f"#q-list .q-row[data-ep=\"{e2}\"] .q-s"), "by Alice · derivations · 1 min")
        self.assertEqual(self.text("#q-now"), "Paused: A Fake Paper To Read Along")
        b.js(f"document.querySelector('#q-list .q-row[data-ep=\"{e2}\"] .q-down').click()")
        self.r.wait(lambda: self.queue() == [e1, e2], 5, "moved down on the hub")
        self.assertEqual(self.rows_in_panel(), [e1, e2])
        self.assertTrue(b.js(f"!!document.activeElement.closest('.q-row[data-ep=\"{e2}\"]')"))
        # dragged by its handle, with a mouse, back above the other
        x, y = self.center(f"document.querySelector('#q-list .q-row[data-ep=\"{e2}\"] .q-grip')")
        self.mouse("mousePressed", x, y, button="left", buttons=1, clickCount=1)
        for dy in range(0, 80, 10):
            self.mouse("mouseMoved", x, y - dy, button="left", buttons=1)
        self.mouse("mouseReleased", x, y - 80, button="left", buttons=0, clickCount=1)
        self.r.wait(lambda: self.queue() == [e2, e1], 5, "dragged on the hub")
        b.wait_js(f"JSON.stringify([...document.querySelectorAll('#q-list .q-row')].map(x => x.dataset.ep)) === {tp.js_list([e2, e1])}", 3, "drawn")
        # another device of hers adds one: it shows here; Bob's queue is his own
        self.assertEqual(self.r.req("PUT", "/api/queue", {"op": "add", "ids": [e3]}, user=A)[0], 200)
        b.wait_js(f"JSON.stringify([...document.querySelectorAll('#q-list .q-row')].map(x => x.dataset.ep)) === {tp.js_list([e2, e1, e3])}", 5, "live")
        self.assertEqual(self.queue(B), [])
        # removed, cleared
        b.js(f"document.querySelector('#q-list .q-row[data-ep=\"{e1}\"] .q-del').click()")
        self.r.wait(lambda: self.queue() == [e2, e3], 5, "removed on the hub")
        self.shot("desktop-up-next")
        b.js("document.getElementById('q-clear').click()")
        self.r.wait(lambda: self.queue() == [], 5, "cleared on the hub")
        b.wait_js("!document.getElementById('q-empty').hidden && document.getElementById('p-next').hidden", 3, "empty")
        b.js("document.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', bubbles: true}))")
        b.wait_js("document.getElementById('q-overlay').hidden", 3, "closed")

    def test_2_the_next_one_plays_at_the_end(self):
        b = self.b
        e1, e2, e3 = self.es
        src = f"{AUDIO}.getAttribute('src')"
        self.set_queue([e2, e3])
        self.home(A)
        self.open(self.short[0])
        b.wait_js(f"{src} === '/audio/{e1}.mp3' && {AUDIO}.duration > 3", 10, "loaded")
        b.js(f"{AUDIO}.muted = true; document.getElementById('p-play').click()")
        b.wait_js(f"!{AUDIO}.paused && {AUDIO}.currentTime > 0.2", 10, "playing")
        b.js(f"{AUDIO}.currentTime = {AUDIO}.duration - 0.4")
        # the next plays, leaves the queue, and the window follows it
        b.wait_js(f"{src} === '/audio/{e2}.mp3' && !{AUDIO}.paused && {AUDIO}.currentTime > 0.1", 10, "the next one playing")
        self.r.wait(lambda: self.queue() == [e3], 5, "gone from the queue")
        b.wait_js(f"location.hash === '#p={self.short[1]}' && document.getElementById('w-title').textContent === 'Short fake paper two'", 5, "window followed")
        self.assertEqual(self.text("#p-next-t"), "Short fake paper three")
        b.wait_js(f"document.querySelector('{tp.ROW.format(self.short[0])} .row-sub .st').textContent === 'Played'", 5, "the first played")
        # on a phone, from the list: the player bar goes on to the next
        try:
            self.phone()
            b.js("document.getElementById('back').click()")
            b.wait_js("!document.getElementById('bar').hidden && !document.body.classList.contains('open')", 5, "the bar on the list")
            self.assertFalse(b.js("document.getElementById('p-next').hidden"))
            b.js(f"{AUDIO}.currentTime = {AUDIO}.duration - 0.4")
            b.wait_js(f"{src} === '/audio/{e3}.mp3' && !{AUDIO}.paused && {AUDIO}.currentTime > 0.1", 10, "the last one playing")
            b.wait_js("document.getElementById('bar-title').textContent === 'Short fake paper three' && document.getElementById('p-next').hidden", 5, "the bar")
            self.r.wait(lambda: self.queue() == [], 5, "queue empty")
            self.assertFalse(b.js("document.body.classList.contains('open')"))        # the list stays
            # the end of the last: nothing more
            b.js(f"{AUDIO}.currentTime = {AUDIO}.duration - 0.3")
            b.wait_js(f"{AUDIO}.ended", 5, "ended")
            time.sleep(0.5)
            self.assertEqual(b.js(src), f"/audio/{e3}.mp3")
        finally:
            b.viewport(1440, 900)

    def test_3_papercast_queue_for_the_map(self):
        b = self.b
        e1, e2, e3 = self.es
        self.home(A)
        self.assertEqual(b.js(f"window.papercastQueue.add([{json.dumps(self.short[0])}, {json.dumps(self.short[1])}, {json.dumps(self.wait)}])"), 2)
        self.r.wait(lambda: self.queue() == [e1, e2], 5, "added by paper id")
        self.assertEqual(b.js(f"window.papercastQueue.playNext({json.dumps(e3)})"), 1)
        self.r.wait(lambda: self.queue() == [e3, e1, e2], 5, "play next by episode id")
        # "play this graph in order": the first plays now, the rest go first in Up next
        self.assertEqual(b.js(f"window.papercastQueue.playAll([{json.dumps(self.read)}, {json.dumps(self.wait)}, {json.dumps(self.short[1])}])"), 2)
        b.wait_js(f"{AUDIO}.getAttribute('src') === '/audio/{self.er}.mp3' && !{AUDIO}.paused", 10, "playing the first")
        self.r.wait(lambda: self.queue() == [e2, e3, e1], 5, "the rest next")
        self.assertTrue(b.js("Object.isFrozen(window.papercastQueue)"))
        self.assertEqual(b.js("window.papercastQueue.add(['nonsense', 42])"), 0)

    def test_4_the_transcript_follows_the_audio(self):
        b = self.b
        segs = self.timed["segments"]
        self.home(A)
        self.open(self.read)
        b.wait_js(f"!document.getElementById('tr').hidden && {AUDIO}.duration > 20", 10, "transcript")
        # the script as it is: headings and paragraphs of plain text, nothing else
        heads = [line.lstrip("#").strip() for line in self.script.splitlines() if line.startswith("#")]
        self.assertEqual(b.js("[...document.querySelectorAll('#tr-body h4')].map(h => h.textContent)"), heads)
        self.assertEqual(b.js("document.querySelectorAll('#tr-body p').length"), 30)
        self.assertEqual(b.js("document.querySelectorAll('#tr-body *:not(h4):not(p):not(span)').length"), 0)
        self.assertEqual(b.js("[...document.querySelectorAll('#tr-body h4, #tr-body p')].map(n => n.textContent).join(' ').split(' ')"),
                         [w for line in self.script.splitlines() for w in line.lstrip("#").split()])
        self.assertTrue(b.js("document.getElementById('tr-note').hidden"))          # real timings
        self.assertEqual(b.js(NOW), "0")
        # the mark moves with currentTime
        for k in (5, 41):
            b.js(f"{AUDIO}.currentTime = {segs[k]['start'] + 0.05}")
            b.wait_js(f"{NOW} === '{k}'", 5, f"sentence {k}")
        # not playing, so the window stays; the sentence is out of sight: Back to now
        b.wait_js("!document.getElementById('tr-now').hidden", 3, "Back to now shows")
        self.assertFalse(b.js(in_view(SEG.format(41))))
        top = b.js(f"{SCROLLER}.scrollTop")
        b.js("document.getElementById('tr-now').click()")
        b.wait_js(in_view(SEG.format(41)) + " && document.getElementById('tr-now').hidden", 5, "back to now")
        self.assertGreater(b.js(f"{SCROLLER}.scrollTop"), top)
        # a tap on a sentence plays from there
        b.js(f"{AUDIO}.muted = true")
        b.js(f"{SEG.format(23)}.click()")
        b.wait_js(f"!{AUDIO}.paused && {NOW} === '23' && Math.abs({AUDIO}.currentTime - {segs[23]['start']}) < 0.5", 5, "played from the tap")
        # following now: a sentence far down is brought into view, and kept there as the audio
        # goes on (these fake sentences take a quarter of a second each)
        cur = "document.querySelector('#tr-body .tr-s.now')"
        b.js(f"{AUDIO}.currentTime = {segs[70]['start'] + 0.05}")
        b.wait_js(f"Number({NOW}) >= 70 && {in_view(cur)}", 5, "kept in view")
        time.sleep(1.2)
        b.wait_js(f"!{AUDIO}.paused && Number({NOW}) > 72 && {in_view(cur)}", 5, "still in view")
        # the person scrolls away: the page no longer moves, and offers Back to now
        x, y = self.center("document.getElementById('win')")
        self.mouse("mouseWheel", x, y, deltaX=0, deltaY=-2500)
        b.wait_js(f"!{in_view(cur)} && !document.getElementById('tr-now').hidden", 5, "scrolled away")
        time.sleep(0.3)
        top = b.js(f"{SCROLLER}.scrollTop")
        b.js(f"{AUDIO}.currentTime = {segs[90]['start'] + 0.05}")
        b.wait_js(f"Number({NOW}) >= 90", 5, "sentence 90")
        time.sleep(0.8)
        self.assertEqual(b.js(f"{SCROLLER}.scrollTop"), top)
        self.assertFalse(b.js("document.getElementById('tr-now').hidden"))
        b.js("document.getElementById('tr-now').click()")
        b.wait_js(in_view(cur) + " && document.getElementById('tr-now').hidden", 5, "back to now again")
        b.js(f"{AUDIO}.pause()")
        self.shot("desktop-transcript")

    def test_5_find_in_the_transcript(self):
        b = self.b
        self.home(A)
        self.open(self.read)
        b.wait_js("!document.getElementById('tr').hidden", 10, "transcript")
        before = b.js("document.getElementById('tr-body').textContent")
        tr = self.r.req("GET", f"/api/episodes/{self.er}/transcript", user=A)[1]
        q = "quietly again"
        n = sum(p["text"].lower().count(q) for bl in tr["blocks"] for p in bl["parts"])
        self.assertGreater(n, 3)

        def find(text):
            b.js(f"{{ const f = document.getElementById('tr-q'); f.value = {json.dumps(text)}; f.dispatchEvent(new Event('input')); }}")

        def key(k, shift=False):
            b.js(f"document.getElementById('tr-q').dispatchEvent(new KeyboardEvent('keydown', {{key: '{k}', shiftKey: {str(shift).lower()}, bubbles: true}}))")

        find("Quietly AGAIN")
        b.wait_js(f"document.getElementById('tr-count').textContent === '1 of {n}'", 5, "found")
        self.assertEqual(b.js("document.querySelectorAll('#tr-body mark.tr-m').length"), n)
        self.assertEqual(b.js("document.querySelectorAll('#tr-body mark.tr-m.on').length"), 1)
        on = "[...document.querySelectorAll('#tr-body mark.tr-m')].findIndex(m => m.classList.contains('on'))"
        key("Enter")
        self.assertEqual((self.text("#tr-count"), b.js(on)), (f"2 of {n}", 1))
        b.js("document.getElementById('tr-next').click()")
        self.assertEqual(self.text("#tr-count"), f"3 of {n}")
        b.js("document.getElementById('tr-prev').click()")
        key("Enter", shift=True)
        self.assertEqual(self.text("#tr-count"), f"1 of {n}")
        key("Enter", shift=True)                                   # round to the last
        self.assertEqual(self.text("#tr-count"), f"{n} of {n}")
        b.wait_js(in_view("document.querySelector('#tr-body mark.tr-m.on')"), 5, "the match in view")
        self.assertEqual(b.js("document.getElementById('tr-body').textContent"), before)    # marks change no text
        find("zzzz")
        b.wait_js("document.getElementById('tr-count').textContent === 'No matches' && document.getElementById('tr-next').disabled", 5, "none")
        # found again from where the window is: the first match on screen, or under it
        find(q)
        b.wait_js(f"/^[0-9]+ of {n}$/.test(document.getElementById('tr-count').textContent)", 5, "again")
        b.wait_js(in_view("document.querySelector('#tr-body mark.tr-m.on')"), 5, "that match in view")
        key("Escape")
        b.wait_js("document.getElementById('tr-q').value === '' && !document.querySelector('#tr-body mark')", 3, "cleared")
        self.assertEqual(b.js("document.getElementById('tr-body').textContent"), before)
        self.assertEqual(b.js("document.querySelectorAll('#tr-body .tr-s').length"), len(self.timed["segments"]))

    def test_6_an_estimate_works_the_same(self):
        b = self.b
        est = self.r.req("GET", f"/api/episodes/{self.eg}/timings", user=A)[1]
        self.assertTrue(est["estimated"])
        segs = est["segments"]
        self.home(A)
        self.open(self.guess)
        b.wait_js(f"!document.getElementById('tr').hidden && {AUDIO}.duration > 20", 10, "transcript")
        self.assertFalse(b.js("document.getElementById('tr-note').hidden"))
        self.assertEqual(self.text("#tr-note").strip(), "· timing estimated")
        self.assertEqual(b.js("document.querySelectorAll('#tr-body .tr-s').length"), len(checks.sentences(self.short_script)))
        # stretched to the audio's own length (a little under the 30 s the hub was told)
        scale = b.js(f"{AUDIO}.duration") / est["duration_s"]
        for k in (3, len(segs) - 2):
            b.js(f"{AUDIO}.currentTime = {segs[k]['start'] * scale + 0.05}")
            b.wait_js(f"{NOW} === '{k}'", 5, f"estimated sentence {k}")
        b.js(f"{AUDIO}.muted = true; {SEG.format(2)}.click()")
        b.wait_js(f"!{AUDIO}.paused && {NOW} === '2' && Math.abs({AUDIO}.currentTime - {segs[2]['start'] * scale}) < 0.5", 5, "tap")
        b.js(f"{AUDIO}.pause()")
        # a version with no script: no transcript at all
        self.open(self.short[0])
        b.wait_js("document.getElementById('tr').hidden && document.getElementById('w-title').textContent === 'Short fake paper one'", 5, "none")

    def test_7_the_transcript_rolls_while_it_plays(self):
        """Playing, the window rolls with the sentence (about a third of the way down), the
        comments' column staying where it is; scrolled by hand, it stops, and Back to now shows;
        Play (after a pause) follows again."""
        b = self.b
        segs = self.timed["segments"]
        cur = "document.querySelector('#tr-body .tr-s.now')"
        col = "(r => [r.left, r.top, r.right, r.bottom])(document.getElementById('comments').getBoundingClientRect())"
        self.home(A)
        self.open(self.read)
        b.wait_js(f"!document.getElementById('tr').hidden && {AUDIO}.duration > 20", 10, "transcript")
        # the middle scrolls on its own, left of the comments
        self.assertEqual(b.js("getComputedStyle(document.getElementById('p-mid')).overflowY"), "auto")
        self.assertLessEqual(b.js("document.getElementById('tr').getBoundingClientRect().right"), b.js(f"{col}[0]") + 1)
        c0 = b.js(col)
        b.js(f"{AUDIO}.muted = true; document.getElementById('p-play').click()")
        b.wait_js(f"!{AUDIO}.paused", 5, "playing")
        b.js(f"{AUDIO}.currentTime = {segs[80]['start'] + 0.05}")
        b.wait_js(f"Number({NOW}) >= 80 && {at_third(cur)} && document.getElementById('tr-now').hidden", 5, "rolled to it")
        top = b.js(f"{SCROLLER}.scrollTop")
        time.sleep(1.5)             # six more sentences: the view went on with them
        b.wait_js(f"!{AUDIO}.paused && Number({NOW}) >= 85 && {at_third(cur)} && {SCROLLER}.scrollTop > {top}", 5, "rolling")
        self.assertEqual(b.js(col), c0, "the comments' column moved")
        self.assertEqual(b.js("document.getElementById('win').scrollTop"), 0, "the window scrolled, not the middle")
        # scrolled by hand: it stays put, and Back to now shows
        x, y = self.center(SCROLLER)
        self.mouse("mouseWheel", x, y, deltaX=0, deltaY=-20000)
        b.wait_js(f"!{in_view(cur)} && !document.getElementById('tr-now').hidden", 5, "scrolled away")
        time.sleep(0.3)
        top = b.js(f"{SCROLLER}.scrollTop")
        time.sleep(1.0)
        self.assertEqual(b.js(f"{SCROLLER}.scrollTop"), top, "it rolled on after the person scrolled")
        # a pause, then Play: it follows again
        b.js("document.getElementById('p-play').click()")
        b.wait_js(f"{AUDIO}.paused", 3, "paused")
        b.js("document.getElementById('p-play').click()")
        b.wait_js(f"!{AUDIO}.paused && {at_third(cur)} && document.getElementById('tr-now').hidden", 5, "Play follows again")
        b.js(f"{AUDIO}.pause()")

    def test_z_phone(self):
        """On a phone: every control, with the transcript and Up next, takes a 44 x 44 px tap."""
        b = self.b
        e1, e2, e3 = self.es

        def targets(where):
            bad = json.loads(b.js(TAP_TARGETS))
            self.assertEqual(bad, [], f"phone, {where}: {len(bad)} tap target(s) too small or covered:\n" + "\n".join(bad))

        try:
            self.set_queue([e1, e2])
            self.phone()
            self.home(A)
            self.open(self.read)
            b.wait_js("getComputedStyle(document.getElementById('win')).visibility === 'visible' && !document.getElementById('tr').hidden"
                      " && !document.getElementById('p-next').hidden", 10, "window")
            targets("the window with Up next and the transcript")
            self.no_side_scroll("the window with the transcript")
            # the transcript and the comments are two tabs here; the bar is one line along the bottom
            vh = b.js("innerHeight")
            bar = b.js("(r => [r.top, r.bottom, r.height])(document.getElementById('bar').getBoundingClientRect())")
            self.assertEqual(round(bar[1]), vh)
            self.assertLessEqual(bar[2], 72, "the phone's bar is more than one line")
            self.assertAlmostEqual(b.js("document.getElementById('win').getBoundingClientRect().bottom"), bar[0], delta=1)
            self.assertTrue(b.js("document.getElementById('b-prog').getClientRects().length > 0 && !document.getElementById('scrub').getClientRects().length"),
                            "one line: the thin line for how far, no seek bar")
            self.assertEqual(b.js("[getComputedStyle(document.getElementById('ptabs')).display, getComputedStyle(document.getElementById('comments')).display]"), ["flex", "none"])
            b.js("document.getElementById('pt-c').click()")
            b.wait_js("getComputedStyle(document.getElementById('comments')).display !== 'none' && getComputedStyle(document.getElementById('tr')).display === 'none'", 3, "the comments' tab")
            targets("the comments' tab")
            self.no_side_scroll("the comments' tab")
            b.js("document.getElementById('pt-tr').click()")
            b.wait_js("getComputedStyle(document.getElementById('tr')).display !== 'none'", 3, "the transcript's tab")
            b.js("{ const f = document.getElementById('tr-q'); f.value = 'plain fake'; f.dispatchEvent(new Event('input')); }")
            b.wait_js("!document.getElementById('tr-next').disabled", 5, "found")
            targets("find in the transcript")
            b.wait_js(f"{AUDIO}.duration > 20", 10, "audio")
            b.js(f"{AUDIO}.currentTime = {self.timed['segments'][60]['start'] + 0.05}")
            b.wait_js(f"{NOW} === '60'", 5, "sentence 60")
            time.sleep(1.2)
            x, y = self.center("document.getElementById('win')")
            self.mouse("mouseWheel", x, y, deltaX=0, deltaY=-20000)
            self.mouse("mouseWheel", x, y, deltaX=0, deltaY=600)
            b.wait_js("!document.getElementById('tr-now').hidden", 5, "Back to now")
            targets("with Back to now")
            self.shot("phone-transcript")
            b.js("document.getElementById('w-more-phone').click()")
            self.assertEqual(self.menu()[:3], ["Play next", "Add to Up next", "Details"])
            targets("the window's menu")
            b.js("document.getElementById('w-more-phone').click()")
            b.js("document.getElementById('bar-exp').click()")
            b.wait_js("document.getElementById('bar').classList.contains('open')", 3, "the bar opened")
            targets("the window with the bar opened")
            b.js("document.getElementById('p-next').click()")
            b.wait_js("!document.getElementById('q-overlay').hidden", 3, "panel")
            targets("the Up next panel")
            self.no_side_scroll("the Up next panel")
            self.shot("phone-up-next")
            b.js("document.getElementById('q-close').click()")
            # the bar's Up next, from the list
            b.js(f"{AUDIO}.muted = true; document.getElementById('p-play').click()")
            b.wait_js(f"!{AUDIO}.paused", 5, "playing")
            b.js("document.getElementById('back').click()")
            b.wait_js("!document.getElementById('bar').hidden && document.getElementById('bar').classList.contains('open')"
                      " && getComputedStyle(document.getElementById('win')).visibility === 'hidden'", 5, "the bar on the list")
            targets("the opened bar with Up next")
            b.js("document.getElementById('p-next').click()")
            b.wait_js("!document.getElementById('q-overlay').hidden && document.body.classList.contains('open') === false", 3, "panel from the bar")
            targets("the Up next panel from the bar")
            x, y = self.center(f"document.querySelector('#q-list .q-row[data-ep=\"{e1}\"] .q-down')")
            self.mouse("mousePressed", x, y, button="left", buttons=1, clickCount=1)
            self.mouse("mouseReleased", x, y, button="left", buttons=0, clickCount=1)
            self.r.wait(lambda: self.queue() == [e2, e1], 5, "moved with a tap")
            b.js("document.getElementById('q-close').click()")
            b.js(f"{AUDIO}.pause()")
        finally:
            b.viewport(1440, 900)


if __name__ == "__main__":
    unittest.main()
