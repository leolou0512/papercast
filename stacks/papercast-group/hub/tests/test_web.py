#!/usr/bin/env python3
"""The browser API (hub/web.py, SPEC.md section 7) over HTTP, against the real server.

    python3 -m unittest discover -s stacks/papercast-group/hub/tests -p 'test_web.py' -v

The auth module is a stand-in (web_rig.py): X-Test-User, roles, X-PCG on changes."""
from __future__ import annotations

import json
import os
import re
import socket
import sys
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from web_rig import Rig, publish, silent_mp3  # noqa: E402

from hub import web  # noqa: E402

A, B, CAROL, DAN = "alice@example.org", "bob@example.org", "carol@example.org", "dan@example.org"


class Web(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        r = cls.r = Rig()
        cls.alice = r.user(A, "Alice", "admin")
        cls.bob = r.user(B, "Bob", "contributor")
        cls.carol = r.user(CAROL, "Carol", "viewer")
        cls.dan = r.user(DAN, "Dan", "viewer", disabled=True)
        # one paper, two versions: Alice's first, then Bob's
        cls.p2 = r.paper("Flow Matching for Fake Models", cls.alice, authors=["Yaron Lipman", "Ricky Chen"], year=2022,
                         tags=["diffusion", "generative models"], arxiv_id="2210.00001")
        cls.e2a = r.episode(cls.p2, cls.alice, summary="derivations")
        cls.e2b = r.episode(cls.p2, cls.bob, summary="practical", audio_s=6.0)
        # one version, still waiting for the GPU
        cls.p1 = r.paper("A Paper Still Being Voiced", cls.bob, authors=["Grace Hopper"], year=2021, tags=["robotics"])
        cls.e1 = r.episode(cls.p1, cls.bob, state="waiting-for-gpu")
        # never in the library: only a rejected and a deleted version
        cls.p3 = r.paper("Rejected Only", cls.bob)
        r.episode(cls.p3, cls.bob, state="rejected")
        cls.p4 = r.paper("Deleted Only", cls.bob)
        r.episode(cls.p4, cls.bob, deleted_at="2026-09-10T00:00:00Z")

    @classmethod
    def tearDownClass(cls):
        cls.r.close()

    def lib(self, user=A, q=""):
        st, j, _ = self.r.req("GET", f"/api/library?q={q}", user=user)
        self.assertEqual(st, 200, j)
        return j["papers"]

    # ------------------------------------------------------------ the library
    def test_library_shape(self):
        papers = self.lib()
        self.assertEqual([p["id"] for p in papers], [self.p1, self.p2])        # newest first; rejected/deleted-only left out
        p2 = papers[1]
        self.assertEqual(p2["title"], "Flow Matching for Fake Models")
        self.assertEqual(p2["authors"], ["Yaron Lipman", "Ricky Chen"])
        self.assertEqual(p2["tags"], ["diffusion", "generative models"])
        self.assertFalse(p2["listened"])
        eps = p2["episodes"]
        self.assertEqual([e["id"] for e in eps], [self.e2a, self.e2b])         # first made first
        self.assertEqual(eps[0]["made_by"], {"id": self.alice, "name": "Alice"})
        self.assertEqual(eps[1]["made_by"], {"id": self.bob, "name": "Bob"})
        self.assertEqual([e["prefs_summary"] for e in eps], ["derivations", "practical"])
        self.assertEqual([e["mine"] for e in eps], [True, False])
        self.assertEqual([e["can_delete"] for e in eps], [True, True])        # Alice is an admin
        self.assertTrue(all(e["has_audio"] and e["has_explainer"] and e["duration_s"] == 1800 for e in eps))
        self.assertEqual(p2["added_at"], eps[0]["created_at"])
        wait = papers[0]["episodes"][0]
        self.assertEqual((wait["state"], wait["has_audio"], wait["duration_s"]), ("waiting-for-gpu", False, None))
        # Carol (a viewer) may delete nothing; Bob only his own
        self.assertEqual([e["can_delete"] for e in self.lib(CAROL)[1]["episodes"]], [False, False])
        self.assertEqual([e["can_delete"] for e in self.lib(B)[1]["episodes"]], [False, True])
        self.assertEqual([e["mine"] for e in self.lib(B)[1]["episodes"]], [False, True])

    def test_search(self):
        ids = lambda q, user=A: [p["id"] for p in self.lib(user, q)]
        self.assertEqual(ids("flow"), [self.p2])                     # title
        self.assertEqual(ids("HOPPER"), [self.p1])                   # authors, any case
        self.assertEqual(ids("generative"), [self.p2])               # tags
        self.assertEqual(ids("alice"), [self.p2])                    # a maker's name
        self.assertEqual(ids("bob"), [self.p1, self.p2])
        self.assertEqual(ids("bob+robotics"), [self.p1])             # every word must match
        self.assertEqual(ids("2210.00001"), [self.p2])               # arXiv id
        self.assertEqual(ids("nothing-like-this"), [])
        self.assertEqual(ids("rejected"), [])                        # not in the library at all

    def test_library_by_ids(self):
        """After an event the page reads just those papers again; one that left is not there."""
        st, j, _ = self.r.req("GET", f"/api/library?ids={self.p2},{self.p3},p_aaaaaaaaaaaa,junk")
        self.assertEqual((st, [p["id"] for p in j["papers"]]), (200, [self.p2]))
        self.assertEqual(self.r.req("GET", "/api/library?ids=%27%3B--")[1], {"papers": []})

    def test_body_read_on_a_kept_alive_connection(self):
        """A body a handler has no use for is still read: left in the socket, it would be taken
        for the start of the next request on the same connection."""
        import http.client
        c = http.client.HTTPConnection("127.0.0.1", self.r.port, timeout=5)
        try:
            e = self.r.episode(self.p1, self.bob)
            h = {"X-Test-User": B, "X-PCG": "1", "Content-Type": "application/json"}
            c.request("DELETE", f"/api/episodes/{e}", body=b'{"why": "unused"}', headers=h)
            self.assertEqual(c.getresponse().read() and 200, 200)
            c.request("POST", f"/api/episodes/{e}/undelete", body=b"{}", headers=h)
            r = c.getresponse(); r.read()
            self.assertEqual(r.status, 200)
            c.request("GET", "/api/config", headers={"X-Test-User": B})
            r = c.getresponse()
            self.assertEqual((r.status, json.loads(r.read())["me"]["name"]), (200, "Bob"))
        finally:
            c.close()
            self.r.q("UPDATE episodes SET deleted_at = '2026-09-01T00:00:00Z' WHERE made_by = ? AND paper_id = ? AND id != ?",
                     self.bob, self.p1, self.e1)

    def test_paper_and_404(self):
        st, j, _ = self.r.req("GET", f"/api/papers/{self.p2}")
        self.assertEqual((st, j["id"], len(j["episodes"])), (200, self.p2, 2))
        self.assertEqual(self.r.req("GET", f"/api/papers/{self.p3}")[0], 404)      # rejected only
        self.assertEqual(self.r.req("GET", "/api/papers/p_aaaaaaaaaaaa")[0], 404)

    def test_auth_levels(self):
        self.assertEqual(self.r.req("GET", "/api/library", user=None)[0], 401)
        self.assertEqual(self.r.req("GET", "/api/library", user=DAN)[0], 403)          # disabled
        self.assertEqual(self.r.req("GET", "/api/admin/base", user=CAROL)[0], 403)     # viewer
        self.assertEqual(self.r.req("GET", "/api/admin/base", user=B)[0], 403)         # contributor
        self.assertEqual(self.r.req("GET", "/api/admin/base", user=A)[0], 200)

    def test_csrf_header_on_every_change(self):
        """Without X-PCG no change goes through, whatever the auth module does (web.py checks too),
        nor with a browser's cross-site mark."""
        for method, path, body in (("PUT", f"/api/papers/{self.p2}/listened", {"listened": True}),
                                   ("PUT", f"/api/episodes/{self.e2a}/position", {"s": 5}),
                                   ("DELETE", f"/api/episodes/{self.e2b}", None),
                                   ("PUT", "/api/prefs", {"settings": {}, "note": ""}),
                                   ("POST", "/api/admin/base", {"guideline": "x"})):
            st, j, _ = self.r.req(method, path, body, headers={"X-PCG": None})
            self.assertEqual((st, j["error"]), (403, "csrf"), path)
            st, j, _ = self.r.req(method, path, body, headers={"Sec-Fetch-Site": "cross-site"})
            self.assertEqual((st, j["error"]), (403, "cross_origin"), path)
        # the web module's own fence, with an auth module that lets anything through
        from hub import auth
        was = auth.authenticate
        auth.authenticate = lambda req, level: {"id": self.alice, "role": "admin", "name": "Alice", "email": A}
        try:
            st, j, _ = self.r.req("PUT", f"/api/papers/{self.p2}/listened", {"listened": True}, headers={"X-PCG": None})
            self.assertEqual((st, j["error"]), (403, "csrf"))
        finally:
            auth.authenticate = was
        self.assertEqual(self.r.q("SELECT count(*) FROM listened")[0][0], 0)
        self.assertEqual(self.r.q("SELECT count(*) FROM positions")[0][0], 0)
        self.assertIsNone(self.r.q("SELECT deleted_at FROM episodes WHERE id = ?", self.e2b)[0][0])

    def test_config(self):
        st, j, _ = self.r.req("GET", "/api/config", user=B)
        self.assertEqual(st, 200)
        self.assertEqual(j["me"], {"id": self.bob, "name": "Bob", "email": B, "role": "contributor"})
        self.assertEqual(j["auth"], "header")
        self.assertRegex(j["build"], r"^[0-9a-f]{12}$")
        self.assertEqual(j["undo_days"], 30)

    # ------------------------------------------------------------ listened, per person
    def test_listened_is_per_person(self):
        try:
            st, j, _ = self.r.req("PUT", f"/api/papers/{self.p2}/listened", {"listened": True}, user=CAROL)
            self.assertEqual((st, j["listened"]), (200, True))
            self.assertTrue(self.lib(CAROL)[1]["listened"])
            self.assertFalse(self.lib(B)[1]["listened"])            # Bob's tick is his own
            st, j, _ = self.r.req("PUT", f"/api/papers/{self.p2}/listened", {"listened": True}, user=CAROL)
            self.assertEqual(st, 200)                                  # again: still ticked, once
            self.assertEqual(self.r.q("SELECT count(*) FROM listened WHERE user_id = ?", self.carol)[0][0], 1)
            st, j, _ = self.r.req("PUT", f"/api/papers/{self.p2}/listened", {"listened": False}, user=CAROL)
            self.assertEqual((st, j["listened"]), (200, False))
            self.assertEqual(self.r.req("PUT", f"/api/papers/{self.p2}/listened", {"listened": "yes"}, user=CAROL)[0], 400)
            self.assertEqual(self.r.req("PUT", f"/api/papers/{self.p3}/listened", {"listened": True}, user=CAROL)[0], 404)
        finally:
            self.r.q("DELETE FROM listened")

    def test_listened_event_goes_to_that_person_only(self):
        from hub import events
        mine, other = events.subscribe(self.carol), events.subscribe(self.bob)
        try:
            self.r.req("PUT", f"/api/papers/{self.p2}/listened", {"listened": True}, user=CAROL)
            _, kind, data = mine.q.get(timeout=2)
            self.assertEqual((kind, data["paper_id"]), ("paper", self.p2))
            self.assertTrue(other.q.empty())
        finally:
            events.unsubscribe(mine); events.unsubscribe(other)
            self.r.q("DELETE FROM listened")

    # ------------------------------------------------------------ positions, per person, newest wins
    def test_positions(self):
        now = int(time.time() * 1000)
        try:
            st, j, _ = self.r.req("PUT", f"/api/episodes/{self.e2a}/position", {"s": 61.25, "at": now}, user=CAROL)
            self.assertEqual((st, j["kept_newer"]), (200, False))
            e = self.lib(CAROL)[1]["episodes"][0]
            self.assertEqual((e["position_s"], e["position_at"]), (61.2, now))
            self.assertIsNone(self.lib(B)[1]["episodes"][0]["position_s"])      # Bob's own place
            # a late PUT from a device that stopped earlier does not win
            st, j, _ = self.r.req("PUT", f"/api/episodes/{self.e2a}/position", {"s": 5, "at": now - 60000}, user=CAROL)
            self.assertEqual((st, j["kept_newer"]), (200, True))
            self.assertEqual(self.lib(CAROL)[1]["episodes"][0]["position_s"], 61.2)
            # a newer one does
            self.r.req("PUT", f"/api/episodes/{self.e2a}/position", {"s": 90, "at": now + 1}, user=CAROL)
            self.assertEqual(self.lib(CAROL)[1]["episodes"][0]["position_s"], 90)
            # no `at`, or one far in the future: the hub's clock
            self.r.req("PUT", f"/api/episodes/{self.e2b}/position", {"s": 3, "at": now + 10 ** 12}, user=CAROL)
            at = self.lib(CAROL)[1]["episodes"][1]["position_at"]
            self.assertLess(abs(at - time.time() * 1000), 5000)
            for bad in ({"s": -1}, {"s": "12"}, {"s": True}, {}, {"s": float("nan")}):
                body = json.dumps(bad).encode() if bad.get("s") == bad.get("s") else b'{"s": NaN}'
                self.assertEqual(self.r.req("PUT", f"/api/episodes/{self.e2a}/position", body, user=CAROL)[0], 400, bad)
            self.assertEqual(self.r.req("PUT", "/api/episodes/e_aaaaaaaaaaaa/position", {"s": 1}, user=CAROL)[0], 404)
        finally:
            self.r.q("DELETE FROM positions")

    # ------------------------------------------------------------ delete a version, undo
    def test_delete_and_undo(self):
        r = self.r
        p = r.paper("Delete Me Paper", self.bob)
        eb = r.episode(p, self.bob)
        ec = r.episode(p, self.alice)
        # Carol made neither: refused
        st, j, _ = r.req("DELETE", f"/api/episodes/{eb}", user=CAROL)
        self.assertEqual((st, j["error"]), (403, "not_yours"))
        # Bob deletes his own: gone from the library at once, the paper stays with Alice's
        st, j, _ = r.req("DELETE", f"/api/episodes/{eb}", user=B)
        self.assertEqual((st, j["paper_id"], j["undo_s"]), (200, p, 30 * 86400))
        got = next(x for x in self.lib(CAROL) if x["id"] == p)
        self.assertEqual([e["id"] for e in got["episodes"]], [ec])
        self.assertEqual(r.req("GET", f"/audio/{eb}.mp3", user=CAROL)[0], 404)
        self.assertEqual(r.req("GET", f"/x/{eb}/explainer.html", user=CAROL)[0], 404)
        self.assertEqual(r.req("DELETE", f"/api/episodes/{eb}", user=B)[0], 404)          # already
        # only its maker or an admin brings it back
        self.assertEqual(r.req("POST", f"/api/episodes/{eb}/undelete", {}, user=CAROL)[0], 403)
        st, j, _ = r.req("POST", f"/api/episodes/{eb}/undelete", {}, user=B)
        self.assertEqual((st, [e["id"] for e in j["episodes"]]), (200, [eb, ec]))
        # an admin deletes anyone's; the last version gone, the paper leaves the library
        self.assertEqual(r.req("DELETE", f"/api/episodes/{eb}", user=A)[0], 200)
        self.assertEqual(r.req("DELETE", f"/api/episodes/{ec}", user=A)[0], 200)
        self.assertNotIn(p, [x["id"] for x in self.lib()])
        self.assertEqual(r.req("GET", f"/api/papers/{p}")[0], 404)
        # after 30 days it is too late
        r.q("UPDATE episodes SET deleted_at = '2026-01-01T00:00:00Z' WHERE id = ?", ec)
        st, j, _ = r.req("POST", f"/api/episodes/{ec}/undelete", {}, user=A)
        self.assertEqual((st, j["error"]), (410, "undo_expired"))
        # the undo within the window
        st, j, _ = r.req("POST", f"/api/episodes/{eb}/undelete", {}, user=A)
        self.assertEqual((st, [e["id"] for e in j["episodes"]]), (200, [eb]))
        r.req("DELETE", f"/api/episodes/{eb}", user=A)

    def test_delete_publishes_an_episode_event(self):
        from hub import events
        r = self.r
        p = r.paper("Evented Paper", self.bob)
        e = r.episode(p, self.bob)
        s = events.subscribe(self.carol)
        try:
            r.req("DELETE", f"/api/episodes/{e}", user=B)
            _, kind, data = s.q.get(timeout=2)
            self.assertEqual((kind, data["id"], data["paper_id"], data["deleted"]), ("episode", e, p, True))
            r.req("POST", f"/api/episodes/{e}/undelete", {}, user=B)
            _, kind, data = s.q.get(timeout=2)
            self.assertEqual((kind, data["deleted"]), ("episode", False))
        finally:
            events.unsubscribe(s)
            r.req("DELETE", f"/api/episodes/{e}", user=B)

    # ------------------------------------------------------------ preferences
    def test_prefs(self):
        try:
            st, j, _ = self.r.req("GET", "/api/prefs", user=CAROL)
            self.assertEqual(st, 200)
            self.assertEqual(j["settings"], {"maths": "words", "emphasis": "balanced", "background": "field"})
            self.assertEqual((j["note"], j["version"], j["summary"]), ("", 0, ""))
            self.assertEqual(j["choices"]["maths"], ["words", "key-steps", "full"])
            self.assertEqual(j["note_max"], 500)
            st, j, _ = self.r.req("PUT", "/api/prefs", {"settings": {"maths": "full", "emphasis": "practice"}, "note": " Robots, please. "}, user=CAROL)
            self.assertEqual(st, 200, j)
            self.assertEqual(j["settings"], {"maths": "full", "emphasis": "practice", "background": "field"})
            self.assertEqual((j["note"], j["version"], j["summary"]), ("Robots, please.", 1, "derivations · practical"))
            row = self.r.q("SELECT settings, note, version FROM prefs WHERE user_id = ?", self.carol)[0]
            self.assertEqual((json.loads(row[0])["maths"], row[1], row[2]), ("full", "Robots, please.", 1))
            # the same again: no new version; a change: the next
            self.assertEqual(self.r.req("PUT", "/api/prefs", {"settings": j["settings"], "note": "Robots, please."}, user=CAROL)[1]["version"], 1)
            self.assertEqual(self.r.req("PUT", "/api/prefs", {"settings": {"maths": "key-steps"}, "note": ""}, user=CAROL)[1]["version"], 2)
            # checked with common/prefs.py
            for bad in ({"settings": {"maths": "lots"}, "note": ""}, {"settings": {"colour": "red"}, "note": ""},
                        {"settings": [], "note": ""}, {"settings": {}, "note": "x" * 501}, {"settings": {}, "note": 5}):
                st, j, _ = self.r.req("PUT", "/api/prefs", bad, user=CAROL)
                self.assertEqual((st, j["error"]), (400, "bad_prefs"), bad)
                self.assertTrue(j["problems"])
            self.assertEqual(self.r.req("GET", "/api/prefs", user=B)[1]["version"], 0)       # Bob's are his own
        finally:
            self.r.q("DELETE FROM prefs")

    # ------------------------------------------------------------ base prompt
    def test_base_prompt_versions(self):
        r = self.r
        try:
            st, j, _ = r.req("GET", "/api/admin/base")
            self.assertEqual((st, j), (200, {"versions": [], "latest": None}))
            # v1 with no wording given: the package's wording.json
            st, j, _ = r.req("POST", "/api/admin/base", {"guideline": "# Base\r\nBe clear.\r\n"})
            self.assertEqual(st, 201, j)
            self.assertEqual((j["version"], j["guideline"], j["created_by"]["name"]), (1, "# Base\nBe clear.\n", "Alice"))
            self.assertTrue(j["wording"]["classes"])
            # v2 without wording: v1's; v3 with its own, as JSON text
            st, j, _ = r.req("POST", "/api/admin/base", {"guideline": "Second."})
            self.assertEqual((st, j["version"]), (201, 2))
            own = {"classes": [{"id": "test", "wrong": "says a banned phrase", "fix": "Delete it.",
                                "in": ["script"], "phrases": ["never say this"]}]}
            st, j, _ = r.req("POST", "/api/admin/base", {"guideline": "Third.", "wording": json.dumps(own)})
            self.assertEqual((st, j["version"], j["wording"]), (201, 3, own))
            st, j, _ = r.req("GET", "/api/admin/base")
            self.assertEqual([v["version"] for v in j["versions"]], [3, 2, 1])
            self.assertEqual(j["latest"], 3)
            self.assertEqual(j["versions"][1]["wording"], j["versions"][2]["wording"])
            for bad in ({"guideline": ""}, {"guideline": "   "}, {"guideline": 7},
                        {"guideline": "ok", "wording": "{not json"}, {"guideline": "ok", "wording": {"classes": []}},
                        {"guideline": "ok", "wording": {"classes": [{"id": "x", "phrases": []}]}},
                        {"guideline": "ok", "wording": {"classes": [{"id": "x", "phrases": [3]}]}}):
                st, j, _ = r.req("POST", "/api/admin/base", bad)
                self.assertEqual(st, 400, bad)
            self.assertEqual(r.req("POST", "/api/admin/base", {"guideline": "x" * 200_001})[0], 400)
            self.assertEqual(r.req("POST", "/api/admin/base", {"guideline": "Mine."}, user=B)[0], 403)
            self.assertEqual(r.q("SELECT max(version) FROM base_prompts")[0][0], 3)
        finally:
            r.q("DELETE FROM base_prompts")

    # ------------------------------------------------------------ audio
    def test_audio_with_range(self):
        r = self.r
        body = silent_mp3(6.0)
        st, got, h = r.req("GET", f"/audio/{self.e2b}.mp3", raw=True)
        self.assertEqual((st, got, h["Content-Type"], h["Accept-Ranges"]), (200, body, "audio/mpeg", "bytes"))
        st, got, h = r.req("GET", f"/audio/{self.e2b}.mp3", headers={"Range": "bytes=100-199"}, raw=True)
        self.assertEqual((st, got, h["Content-Range"]), (206, body[100:200], f"bytes 100-199/{len(body)}"))
        st, got, h = r.req("GET", f"/audio/{self.e2b}.mp3", headers={"Range": "bytes=-50"}, raw=True)
        self.assertEqual((st, got), (206, body[-50:]))
        st, got, h = r.req("GET", f"/audio/{self.e2b}.mp3", headers={"Range": f"bytes={len(body)}-"}, raw=True)
        self.assertEqual(st, 416)
        st, got, h = r.req("HEAD", f"/audio/{self.e2b}.mp3", raw=True)
        self.assertEqual((st, got, h["Content-Length"]), (200, b"", str(len(body))))
        self.assertEqual(r.req("GET", f"/audio/{self.e1}.mp3")[0], 404)         # not voiced yet
        self.assertEqual(r.req("GET", f"/audio/{self.e2b}.mp3", user=None)[0], 401)
        self.assertEqual(r.req("GET", "/audio/e_aaaaaaaaaaaa.mp3")[0], 404)
        self.assertEqual(r.req("GET", "/audio/../hub.db")[0], 404)

    def test_audio_symlink_refused(self):
        r = self.r
        p = r.paper("Symlinked Audio", self.bob)
        e = r.episode(p, self.bob, audio_s=None)
        (r.cfg.episodes / e / "audio.mp3").symlink_to(r.data / "hub.db")
        self.assertEqual(r.req("GET", f"/audio/{e}.mp3")[0], 404)
        self.assertFalse(next(x for x in self.lib() if x["id"] == p)["episodes"][0]["has_audio"])
        r.req("DELETE", f"/api/episodes/{e}")

    # ------------------------------------------------------------ explainer
    def test_explainer_headers(self):
        r = self.r
        st, got, h = r.req("GET", f"/x/{self.e2a}/explainer.html", raw=True)
        self.assertEqual(st, 200)
        self.assertIn(b"An explainer.", got)
        self.assertEqual(h["Content-Security-Policy"], web.EXPLAINER_CSP)
        leo = (HERE.parents[3] / "stacks" / "papercast" / "web" / "app.py").read_text()
        m = re.search(r'EXPLAINER_CSP = \((.*?)\)\n', leo, re.S)
        self.assertEqual(web.EXPLAINER_CSP, "".join(re.findall(r'"([^"]*)"', m.group(1))), "not Leo's CSP verbatim")
        self.assertEqual((h["X-Frame-Options"], h["X-Content-Type-Options"], h["Cache-Control"]), ("SAMEORIGIN", "nosniff", "no-store"))
        self.assertEqual(r.req("GET", f"/x/{self.e1}/explainer.html")[0], 200)     # before the voice, too
        self.assertEqual(r.req("GET", f"/x/{self.e2a}/explainer.html", user=None)[0], 401)
        p = r.paper("Explainer Symlink", self.bob)
        e = r.episode(p, self.bob, explainer=None)
        (r.cfg.episodes / e / "explainer.html").symlink_to(r.data / "hub.db")
        self.assertEqual(r.req("GET", f"/x/{e}/explainer.html")[0], 404)
        e2 = r.episode(p, self.bob, explainer=None)
        self.assertEqual(r.req("GET", f"/x/{e2}/explainer.html")[0], 404)
        for x in (e, e2):
            r.req("DELETE", f"/api/episodes/{x}")

    # ------------------------------------------------------------ the page itself
    def test_page_served_with_csp_and_nothing_inline(self):
        from hub.app import PAGE_CSP
        st, got, h = self.r.req("GET", "/", raw=True)
        self.assertEqual((st, h["Content-Security-Policy"]), (200, PAGE_CSP))
        self.assertEqual(self.r.req("GET", "/", user=None)[0], 401)
        html = got.decode()
        self.assertNotRegex(html, r"<script(?![^>]*\bsrc=)")           # no inline script
        self.assertNotRegex(html, r"<style|\sstyle=|\son[a-z]+=")       # no inline style or handler
        for name in re.findall(r'(?:src|href)="/([^"]+)"', html):
            self.assertEqual(self.r.req("GET", f"/{name}", user=None, raw=True)[0], 200, name)
        js = (HERE.parent / "static" / "app.js").read_text()
        self.assertNotIn("setAttribute(\"style\"", js)
        self.assertNotIn("eval(", js)

    # ------------------------------------------------------------ events
    def sse(self, user=A, last=None, timeout=3.0):
        s = socket.create_connection(("127.0.0.1", self.r.port), timeout=timeout)
        extra = f"Last-Event-ID: {last}\r\n" if last else ""
        s.sendall(f"GET /api/events HTTP/1.1\r\nHost: x\r\nX-Test-User: {user}\r\n{extra}\r\n".encode())
        return s

    def read_until(self, s, needle: bytes, timeout=3.0) -> bytes:
        buf, end = b"", time.time() + timeout
        s.settimeout(0.2)
        while needle not in buf and time.time() < end:
            try:
                got = s.recv(65536)
                if not got:
                    break
                buf += got
            except socket.timeout:
                pass
        self.assertIn(needle, buf)
        return buf

    def test_events_stream(self):
        was = web.KEEPALIVE_S
        web.KEEPALIVE_S = 0.3
        s = self.sse(CAROL)
        try:
            head = self.read_until(s, b"event: hello")
            self.assertIn(b"Content-Type: text/event-stream", head)
            self.assertIn(b"id: 0\n", head)
            self.assertNotIn(b"resync", head)
            self.read_until(s, b": ping")                                  # the keepalive
            publish("episode", {"id": self.e1, "paper_id": self.p1, "state": "speaking"})
            got = self.read_until(s, b"event: episode")
            self.assertRegex(got, rb"id: \d+\nevent: episode\ndata: \{.*\"paper_id\": \"" + self.p1.encode())
            publish("paper", {"id": self.p2}, users={self.bob})              # someone else's
            publish("graph", {"id": "g_x"})
            got = self.read_until(s, b"event: graph")
            self.assertNotIn(b"event: paper", got)
        finally:
            s.close()
            web.KEEPALIVE_S = was
        # coming back: told to read everything again
        s = self.sse(CAROL, last="41")
        try:
            self.read_until(s, b"event: resync")
        finally:
            s.close()


if __name__ == "__main__":
    unittest.main()
