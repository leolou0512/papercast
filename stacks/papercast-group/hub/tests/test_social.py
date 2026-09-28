#!/usr/bin/env python3
"""Comments and the board (hub/social.py, static/social.js) against the real hub with the auth
stand-in (web_rig.py: X-Test-User, roles, X-PCG on changes), and in a real headless Chrome.

    cd stacks/papercast-group/hub && python3 -m unittest tests.test_social -v

Comments: plain text up to 2,000 characters, replies one level deep, edit or delete your own,
admins delete any; a deleted comment with replies stays as "comment deleted". The board: uploads,
new versions, comments, graph edits (a person's edits of one graph within an hour are one item),
joins, a pinned notice, and when each person last saw it. The page: times in a comment play the
player from there, web addresses are links that open elsewhere, hostile text stays text, a new
comment reaches a second browser live, no console errors or CSP refusals, and on a phone every
control is a 44 x 44 px tap. Fake titles and people only.

The browser part skips when no headless Chrome or no `websocket-client` is available."""
from __future__ import annotations

import json
import socket
import sys
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from web_rig import Rig  # noqa: E402

from hub import db, events, social  # noqa: E402
from hub import config as C  # noqa: E402

A, B, CAROL, DAN, FED = "alice@example.org", "bob@example.org", "carol@example.org", "dan@example.org", "federico@example.org"


def sub_events(kind):
    """What the hub publishes of one kind, from now on (a subscription like an SSE page's)."""
    s = events.subscribe(None)
    got = []

    def drain():
        while True:
            try:
                eid, k, d = s.q.get_nowait()
            except Exception:
                return got
            if k == kind:
                got.append(d)
    return s, drain


class Comments(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        r = cls.r = Rig()
        cls.alice = r.user(A, "Alice", "admin")
        cls.bob = r.user(B, "Bob", "contributor")
        cls.carol = r.user(CAROL, "Carol", "viewer")
        cls.dan = r.user(DAN, "Dan", "viewer", disabled=True)
        cls.p = r.paper("A Fake Paper To Talk About", cls.alice, tags=["diffusion"])
        cls.ea = r.episode(cls.p, cls.alice, duration=1500)
        cls.eb = r.episode(cls.p, cls.bob, duration=1400)
        cls.other = r.paper("Another Fake Paper", cls.bob)
        cls.eo = r.episode(cls.other, cls.bob)
        cls.gone = r.paper("Only Rejected Fake Paper", cls.bob)
        r.episode(cls.gone, cls.bob, state="rejected")

    @classmethod
    def tearDownClass(cls):
        cls.r.close()

    def post(self, body, user=A, pid=None, **more):
        return self.r.req("POST", f"/api/papers/{pid or self.p}/comments", dict({"body": body}, **more), user=user)[:2]

    def comments(self, user=CAROL, pid=None):
        st, j, _ = self.r.req("GET", f"/api/papers/{pid or self.p}/comments", user=user)
        self.assertEqual(st, 200, j)
        return j["comments"]

    def test_write_read_edit_delete_own_and_admin(self):
        st, c = self.post("At 12:40 the derivation is clearer.", user=B, episode_id=self.eb)
        self.assertEqual(st, 201, c)
        self.assertEqual((c["user"], c["episode_id"], c["parent_id"], c["deleted"], c["edited_at"]),
                         ({"id": self.bob, "name": "Bob", "avatar": None}, self.eb, None, False, None))
        self.assertIn(c["id"], [x["id"] for x in self.comments(user=CAROL)])     # anyone signed in reads
        # only its writer edits, an admin included
        for who in (A, CAROL):
            st, j, _ = self.r.req("PUT", f"/api/comments/{c['id']}", {"body": "not mine"}, user=who)
            self.assertEqual((st, j["error"]), (403, "not_yours"))
        st, j, _ = self.r.req("PUT", f"/api/comments/{c['id']}", {"body": "At 12:41 the derivation is clearer."}, user=B)
        self.assertEqual(st, 200, j)
        self.assertEqual(j["body"], "At 12:41 the derivation is clearer.")
        self.assertIsNotNone(j["edited_at"])
        # a viewer cannot delete someone else's; its writer can; an admin can delete anyone's
        st, j, _ = self.r.req("DELETE", f"/api/comments/{c['id']}", user=CAROL)
        self.assertEqual((st, j["error"]), (403, "not_yours"))
        st, mine = self.post("Mine, then gone.", user=CAROL)
        self.assertEqual(self.r.req("DELETE", f"/api/comments/{mine['id']}", user=CAROL)[0], 200)
        st, j, _ = self.r.req("DELETE", f"/api/comments/{c['id']}", user=A)
        self.assertEqual(st, 200, j)
        ids = [x["id"] for x in self.comments()]
        self.assertNotIn(c["id"], ids)                  # no replies: gone altogether
        self.assertNotIn(mine["id"], ids)
        self.assertEqual(self.r.q("SELECT body, deleted_by FROM comments WHERE id = ?", c["id"])[0][:], ("", self.alice))
        # a deleted one is not there to edit or delete again
        self.assertEqual(self.r.req("PUT", f"/api/comments/{c['id']}", {"body": "x"}, user=B)[0], 404)
        self.assertEqual(self.r.req("DELETE", f"/api/comments/{c['id']}", user=A)[0], 404)
        # a disabled person cannot write
        self.assertEqual(self.post("hello", user=DAN)[0], 403)

    def test_replies_are_one_level_deep(self):
        _, top = self.post("A question about 3:05.", user=CAROL)
        _, r1 = self.post("An answer.", user=B, parent_id=top["id"])
        _, r2 = self.post("A reply to the answer.", user=A, parent_id=r1["id"])
        self.assertEqual((r1["parent_id"], r2["parent_id"]), (top["id"], top["id"]))    # joins the thread
        # a reply on another paper's comment, or to nothing, is refused
        _, elsewhere = self.post("Elsewhere.", user=B, pid=self.other)
        for bad in (elsewhere["id"], 999999):
            st, j = self.post("x", user=B, parent_id=bad)
            self.assertEqual((st, j["error"]), (400, "bad_parent"))
        # the top one deleted: it stays as "comment deleted" while replies hang under it
        self.assertEqual(self.r.req("DELETE", f"/api/comments/{top['id']}", user=A)[0], 200)
        got = {x["id"]: x for x in self.comments()}
        self.assertEqual({k: got[top["id"]][k] for k in ("deleted", "body", "user")}, {"deleted": True, "body": "", "user": None})
        self.assertIn(r1["id"], got)
        # a reply to it still joins that thread
        st, r3 = self.post("Still talking.", user=CAROL, parent_id=top["id"])
        self.assertEqual((st, r3["parent_id"]), (201, top["id"]))
        # its replies gone too, the placeholder goes, and nobody can reply to it any more
        for x in (r1, r2, r3):
            self.assertEqual(self.r.req("DELETE", f"/api/comments/{x['id']}", user=A)[0], 200)
        self.assertNotIn(top["id"], [x["id"] for x in self.comments()])
        st, j = self.post("too late", user=B, parent_id=top["id"])
        self.assertEqual((st, j["error"]), (404, "gone"))

    def test_what_a_comment_may_be(self):
        cases = [("", "empty"), ("   \n\n  ", "empty"), ("x" * 2001, "too_long"), (42, "bad_body"), (None, "bad_body")]
        for body, err in cases:
            st, j = self.post(body)
            self.assertEqual((st, j["error"]), (400, err), repr(body)[:40])
        st, c = self.post("x" * 2000)
        self.assertEqual(st, 201)
        # control characters and direction overrides out; line breaks kept, at most one empty line
        st, c = self.post("  one\r\n\r\n\r\n\r\ntwo\x00\x07 ‮gnirts‬\tthree  ")
        self.assertEqual(c["body"], "one\n\ntwo gnirts\tthree")
        # the version must be this paper's, and live
        st, j = self.post("x", episode_id=self.eo)
        self.assertEqual((st, j["error"]), (400, "bad_episode"))
        # only papers in the library
        self.assertEqual(self.post("x", pid=self.gone)[0], 404)
        self.assertEqual(self.r.req("GET", f"/api/papers/{self.gone}/comments")[0], 404)
        # the CSRF header, on every change
        st, j, _ = self.r.req("POST", f"/api/papers/{self.p}/comments", {"body": "x"}, headers={"X-PCG": None})
        self.assertEqual(st, 403)
        st, j, _ = self.r.req("POST", f"/api/papers/{self.p}/comments", {"body": "x"}, headers={"Sec-Fetch-Site": "cross-site"})
        self.assertEqual(st, 403)

    def test_counts_and_events(self):
        s, drain = sub_events("comment")
        try:
            before = self.r.req("GET", "/api/comments/counts")[1]["counts"].get(self.other, 0)
            _, c = self.post("Counted.", user=B, pid=self.other)
            _, j, _ = self.r.req("GET", "/api/comments/counts")
            self.assertEqual(j["counts"][self.other], before + 1)
            self.assertNotIn(self.gone, j["counts"])
            self.r.req("PUT", f"/api/comments/{c['id']}", {"body": "Counted, edited."}, user=B)
            self.r.req("DELETE", f"/api/comments/{c['id']}", user=B)
            got = [(d["action"], d["id"], d["count"], d["comment"]["deleted"]) for d in drain() if d["paper_id"] == self.other]
            self.assertEqual(got, [("new", c["id"], before + 1, False), ("edit", c["id"], before + 1, False),
                                   ("delete", c["id"], before, True)])
        finally:
            events.unsubscribe(s)

    def test_comment_event_reaches_an_event_stream(self):
        sock = socket.create_connection(("127.0.0.1", self.r.port), timeout=3)
        try:
            sock.sendall(f"GET /api/events HTTP/1.1\r\nHost: x\r\nX-Test-User: {CAROL}\r\n\r\n".encode())
            buf, end = b"", time.time() + 3
            while b"event: hello" not in buf and time.time() < end:
                buf += sock.recv(65536)
            self.post("Live, to Carol's page.", user=B)
            end = time.time() + 3
            while b"event: comment" not in buf and time.time() < end:
                buf += sock.recv(65536)
            self.assertIn(b"event: comment", buf)
            self.assertIn("Live, to Carol's page.".encode(), buf)
        finally:
            sock.close()


class Board(unittest.TestCase):
    """One hub whose history is made here, so the board's items are known exactly."""

    @classmethod
    def setUpClass(cls):
        r = cls.r = Rig()
        cls.alice = r.user(A, "Alice", "admin")          # 08:01 ... one minute apart (Rig.stamp)
        cls.bob = r.user(B, "Bob", "contributor")
        cls.carol = r.user(CAROL, "Carol", "viewer")
        cls.fed = r.user(FED, "Federico", "contributor")
        cls.dan = r.user(DAN, "Dan", "contributor")
        cls.ddpm = r.paper("Denoising Fake Diffusion Models", cls.fed, tags=["diffusion"], year=2020)
        cls.e1 = r.episode(cls.ddpm, cls.fed)                  # the upload
        cls.e2 = r.episode(cls.ddpm, cls.alice)                # a new version
        cls.flow = r.paper("Fake Flow Matching", cls.dan, tags=["diffusion"], year=2022)
        cls.e3 = r.episode(cls.flow, cls.dan)
        cls.rej = r.paper("Fake Paper That Was Rejected", cls.bob)
        r.episode(cls.rej, cls.bob, state="rejected")
        cls.dele = r.paper("Fake Paper That Was Deleted", cls.bob)
        r.episode(cls.dele, cls.bob, deleted_at="2026-09-02T00:00:00Z")

    @classmethod
    def tearDownClass(cls):
        cls.r.close()

    def board(self, user=CAROL, limit=200):
        st, j, _ = self.r.req("GET", f"/api/board?limit={limit}", user=user)
        self.assertEqual(st, 200, j)
        return j

    def texts(self, **kw):
        return [it["text"] for it in self.board(**kw)["items"]]

    def test_1_uploads_versions_joins(self):
        t = self.texts()
        self.assertEqual(t, ["Dan uploaded Fake Flow Matching", "Alice made a new version of Denoising Fake Diffusion Models",
                             "Federico uploaded Denoising Fake Diffusion Models",
                             "Dan joined", "Federico joined", "Carol joined", "Bob joined", "Alice joined"])
        it = self.board()["items"][0]
        self.assertEqual((it["kind"], it["paper_id"], it["user"], it["title"]), ("upload", self.flow, {"id": self.dan, "name": "Dan", "avatar": None}, "Fake Flow Matching"))
        # the newest 5, and more
        j = self.board(limit=5)
        self.assertEqual((len(j["items"]), j["more"]), (5, True))
        self.assertFalse(self.board(limit=8)["more"])
        # Dan removed from the list (disabled): what he did stays, his
        self.r.q("UPDATE users SET disabled = 1 WHERE id = ?", self.dan)
        try:
            self.assertEqual(self.texts(), t)
        finally:
            self.r.q("UPDATE users SET disabled = 0 WHERE id = ?", self.dan)

    def test_2_comments_grouped_per_person_and_paper(self):
        r = self.r
        ids = []
        for body in ("First thought.", "Second thought."):
            st, c, _ = r.req("POST", f"/api/papers/{self.ddpm}/comments", {"body": body}, user=A)
            ids.append(c["id"])
        st, cb, _ = r.req("POST", f"/api/papers/{self.flow}/comments", {"body": "On flows."}, user=B)
        items = self.board()["items"]
        self.assertEqual([it["text"] for it in items[:2]], ["Bob commented on Fake Flow Matching",
                                                            "Alice commented on Denoising Fake Diffusion Models (2 comments)"])
        self.assertEqual((items[1]["comment_id"], items[1]["n"], items[1]["paper_id"]), (ids[1], 2, self.ddpm))
        # over an hour apart: two items
        r.q("UPDATE comments SET created_at = '2026-09-10T10:00:00Z' WHERE id = ?", ids[0])
        r.q("UPDATE comments SET created_at = '2026-09-10T11:30:00Z' WHERE id = ?", ids[1])
        t = self.texts()
        self.assertEqual(t.count("Alice commented on Denoising Fake Diffusion Models"), 2)
        # a deleted comment is not on the board
        r.req("DELETE", f"/api/comments/{cb['id']}", user=B)
        self.assertNotIn("Bob commented on Fake Flow Matching", self.texts())
        r.q("DELETE FROM comments")

    def test_3_graph_edits_grouped(self):
        r = self.r
        st, g, _ = r.req("POST", "/api/graphs", {"name": "Diffusion", "tags": ["diffusion"]}, user=B)
        self.assertEqual(st, 201, g)
        gid = g["graph"]["id"]
        st, ln, _ = r.req("POST", "/api/links", {"src": self.ddpm, "dst": self.flow, "grade": "s"}, user=B)
        self.assertEqual(st, 201, ln)
        lid = ln["link"]["id"]
        self.assertEqual(r.req("PUT", f"/api/links/{lid}", {"grade": "e"}, user=B)[0], 200)
        # the agent's links come with an upload, and are not listed on their own
        db.conn().execute("INSERT INTO graph_log(at, user_id, actor, op, target, before, after) VALUES (?, ?, 'agent', 'link.add', '999', NULL, ?)",
                          (db.now(), self.fed, db.dumps({"src": self.ddpm, "dst": self.flow, "grade": "w", "state": "active"})))
        items = self.board()["items"]
        it = items[0]
        self.assertEqual((it["text"], it["graph_id"], it["n"], it["kind"]), ("Bob edited the graph Diffusion (3 changes)", gid, 3, "graph"))
        # one change by someone else: said as the log says it, with the graph
        self.assertEqual(r.req("DELETE", f"/api/links/{lid}", user=CAROL)[0], 200)
        it = self.board()["items"][0]
        self.assertRegex(it["text"], r"^Carol removed .+ → .+ in the graph ")
        self.assertEqual(it["n"], 1)
        # over an hour after Bob's last edit: a new item for the same graph
        rows = r.q("SELECT id FROM graph_log WHERE user_id = ? AND actor = 'human' ORDER BY id", self.bob)
        for i, (x,) in enumerate(rows):
            r.q("UPDATE graph_log SET at = ? WHERE id = ?", ["2026-09-20T09:00:00Z", "2026-09-20T09:50:00Z", "2026-09-20T11:00:00Z"][i], x)
        bob = [x["text"] for x in self.board()["items"] if x["user"]["id"] == self.bob and x["kind"] == "graph"]
        self.assertEqual(len(bob), 2, bob)
        self.assertEqual(bob[1], "Bob edited the graph Diffusion (2 changes)")
        self.assertRegex(bob[0], r"^Bob regraded .+ in the graph ")
        # a deleted graph: its item stays, but no longer opens anything
        self.assertEqual(r.req("DELETE", f"/api/graphs/{gid}", user=B)[0], 200)
        it = self.board()["items"][0]
        self.assertEqual((it["text"], it["graph_id"]), ("Bob deleted the graph Diffusion", None))
        r.q("DELETE FROM graph_log")

    def test_4_pinned_notice(self):
        r = self.r
        st, j, _ = r.req("POST", "/api/board/notice", {"body": "Reading group"}, user=B)
        self.assertEqual(st, 403)
        s, drain = sub_events("board")
        try:
            st, n, _ = r.req("POST", "/api/board/notice", {"body": "  Reading group Thursday 3 pm:\n the diffusion graph "}, user=A)
            self.assertEqual(st, 201, n)
            self.assertEqual((n["body"], n["user"]["name"]), ("Reading group Thursday 3 pm: the diffusion graph", "Alice"))
            self.assertEqual(self.board()["notice"]["id"], n["id"])
            st, n2, _ = r.req("POST", "/api/board/notice", {"body": "Moved to Friday."}, user=A)      # replaces it
            self.assertEqual(self.board()["notice"]["body"], "Moved to Friday.")
            self.assertEqual(r.req("DELETE", f"/api/board/notice/{n['id']}", user=A)[0], 404)       # no longer pinned
            self.assertEqual(r.req("DELETE", f"/api/board/notice/{n2['id']}", user=B)[0], 403)
            self.assertEqual(r.req("DELETE", f"/api/board/notice/{n2['id']}", user=A)[0], 200)
            self.assertIsNone(self.board()["notice"])
            self.assertEqual([d["why"] for d in drain()], ["notice", "notice", "notice"])
        finally:
            events.unsubscribe(s)
        for body, err in (("", "empty"), ("x" * 281, "too_long"), (["x"], "bad_body")):
            st, j, _ = r.req("POST", "/api/board/notice", {"body": body}, user=A)
            self.assertEqual((st, j["error"]), (400, err))

    def test_5_seen_per_person(self):
        r = self.r
        self.assertIsNone(self.board(user=CAROL)["seen_at"])
        st, j, _ = r.req("PUT", "/api/board/seen", {"at": "2026-09-05T10:00:00Z"}, user=CAROL)
        self.assertEqual((st, j["seen_at"]), (200, "2026-09-05T10:00:00Z"))
        # never back, never ahead of the hub's clock, never someone else's
        r.req("PUT", "/api/board/seen", {"at": "2026-09-01T00:00:00Z"}, user=CAROL)
        self.assertEqual(self.board(user=CAROL)["seen_at"], "2026-09-05T10:00:00Z")
        st, j, _ = r.req("PUT", "/api/board/seen", {"at": "2099-01-01T00:00:00Z"}, user=CAROL)
        self.assertLessEqual(j["seen_at"], db.now())
        self.assertIsNone(self.board(user=B)["seen_at"])
        self.assertEqual(r.req("PUT", "/api/board/seen", {"at": "yesterday"}, user=CAROL)[0], 400)

    def test_6_password_sign_in_joins_when_they_choose_a_password(self):
        """With password sign-in a person is on the list before they come: joined is the first
        time they chose their own password, and it stays when they change it later."""
        cfg = C.Config(data=self.r.data, auth="password")
        c = db.conn()
        c.execute("UPDATE users SET pw_hash = NULL, pw_set_at = NULL")
        c.execute("UPDATE users SET pw_hash = 'scrypt$x', pw_set_at = '2026-09-15T09:00:00Z' WHERE id = ?", (self.fed,))
        try:
            joins = [it for it in social.board(cfg, 200)["items"] if it["kind"] == "join"]
            self.assertEqual([(it["text"], it["at"]) for it in joins], [("Federico joined", "2026-09-15T09:00:00Z")])
            c.execute("UPDATE users SET pw_set_at = '2026-09-20T09:00:00Z' WHERE id = ?", (self.fed,))
            joins = [it for it in social.board(cfg, 200)["items"] if it["kind"] == "join"]
            self.assertEqual([it["at"] for it in joins], ["2026-09-15T09:00:00Z"])
        finally:
            c.execute("UPDATE users SET pw_hash = NULL, pw_set_at = NULL")
            c.execute("DELETE FROM board_joins")


# ==================================================================== in the browser

try:
    from test_page import ROW, SKIP, PageBase
    from web_cdp import Browser
except ImportError as e:                 # no websocket-client: test_page says so itself
    SKIP, PageBase, ROW, Browser = f"browser tests unavailable: {e}", unittest.TestCase, "", None

HOSTILE = ('<script>window.__pwned = 1</script><img src=x onerror="window.__pwned = 2"> <b>bold</b> '
           'javascript:alert(1) [x](javascript:alert(2)) see https://example.org/a_(b). and '
           '(https://example.org/q?x=1&y=2), data:text/html,hi')


def console_errors(browser) -> list:
    """What PageBase.tearDown counts as an error: exceptions, console.error, and the browser's own
    error log (a CSP refusal, a failed request)."""
    errs = []
    for m in browser.events:
        meth, p = m.get("method"), m.get("params", {})
        if meth == "Runtime.exceptionThrown":
            d = p.get("exceptionDetails", {})
            errs.append(f"exception: {(d.get('exception') or {}).get('description') or d.get('text')}")
        elif meth == "Runtime.consoleAPICalled" and p.get("type") in ("error", "assert"):
            errs.append("console.error: " + " ".join(str(a.get("value", a.get("description", ""))) for a in p.get("args", [])))
        elif meth == "Log.entryAdded" and p.get("entry", {}).get("level") == "error":
            e = p["entry"]
            errs.append(f"log ({e.get('source')}): {e.get('text')} {e.get('url', '')}")
    return errs


@unittest.skipIf(SKIP, SKIP or "")
class Page(PageBase):
    """Alice (admin), Bob (contributor), Carol (viewer), as test_page.py."""

    @classmethod
    def fill(cls):
        super().fill()
        r = cls.r
        cls.t30 = r.paper("A Fake Paper Thirty Seconds Long", cls.alice, tags=["diffusion"], year=2021)
        cls.e30 = r.episode(cls.t30, cls.alice, duration=30, audio_s=30)
        cls.two = r.paper("Two Fake Versions To Talk About", cls.alice, tags=["diffusion"], year=2022)
        cls.va = r.episode(cls.two, cls.alice, duration=30, audio_s=30)
        cls.vb = r.episode(cls.two, cls.bob, duration=30, audio_s=30)
        cls.others = []
        for i in range(6):
            p = r.paper(f"Filler fake paper {i}", cls.bob, year=2019)
            r.episode(p, cls.bob, audio_s=1.0)
            cls.others.append(p)

    def tearDown(self):
        super().tearDown()
        self.r.q("DELETE FROM comments")
        self.r.q("DELETE FROM board_notices")
        self.r.q("DELETE FROM board_seen")

    # ------------------------------------------------------------------ helpers
    def write(self, text, box="#c-text", send="#c-post"):
        self.b.js(f"{{ const t = document.querySelector({json.dumps(box)}); t.value = {json.dumps(text)};"
                  " t.dispatchEvent(new Event('input')); }")
        self.b.js(f"document.querySelector({json.dumps(send)}).click()")

    def comments_open(self, pid, user=None):
        if user:
            self.as_user(user)
        self.load(f"p={pid}")
        self.b.wait_js("!document.getElementById('comments').hidden && document.getElementById('w-title').textContent !== ''"
                       " && (!!document.querySelector('#c-list .c-item') || !!document.querySelector('#c-list .c-none'))", 10, "comments")

    def items(self):
        return json.loads(self.b.js("JSON.stringify([...document.querySelectorAll('#c-list .c-item')].map(x => x.dataset.id))"))

    def api_post(self, body, user=B, pid=None, **more):
        st, c, _ = self.r.req("POST", f"/api/papers/{pid or self.t30}/comments", dict({"body": body}, **more), user=user)
        self.assertEqual(st, 201, c)
        return c

    def acts(self, cid):
        return json.loads(self.b.js(f"JSON.stringify([...document.querySelectorAll('.c-item[data-id=\"{cid}\"] > .c-acts button')].map(b => b.dataset.act))"))

    def board_texts(self, b=None):
        return json.loads((b or self.b).js("JSON.stringify([...document.querySelectorAll('#bd-list .bd-item .bd-text')].map(x => x.textContent))"))

    # ------------------------------------------------------------------ tests
    def test_1_time_links_play_from_there(self):
        b = self.b
        try:
            self.comments_open(self.t30, A)
            self.write("The key step is at 0:12, and 1:02:03 is past the end; 12:40pm is a clock.")
            b.wait_js("document.querySelectorAll('#c-list .c-item').length === 1", 5, "posted")
            links = json.loads(b.js("JSON.stringify([...document.querySelectorAll('#c-list .c-time')].map(a => [a.textContent, a.dataset.t, a.dataset.ep]))"))
            self.assertEqual(links, [["0:12", "12", self.e30]])
            self.assertEqual(b.js("document.getElementById('c-text').value"), "")
            b.js("document.getElementById('audio').muted = true; document.querySelector('#c-list .c-time').click()")
            b.wait_js(f"(() => {{ const a = document.getElementById('audio'); return a.getAttribute('src') === '/audio/{self.e30}.mp3'"
                      " && !a.paused && a.currentTime >= 12 && a.currentTime < 20; })()", 10, "playing from 0:12")
            self.assertEqual(b.js("location.hash"), f"#p={self.t30}")          # the link only plays
            # "Comment at": where the player is goes into the box
            b.wait_js("!document.getElementById('c-at').hidden && /^Comment at 0:1\\d$/.test(document.getElementById('c-at').textContent)", 5, "Comment at")
            b.js("document.getElementById('audio').pause()")
            label = b.js("document.getElementById('c-at').textContent")
            b.js("document.getElementById('c-text').value = 'a thought'; document.getElementById('c-at').click()")
            self.assertEqual(b.js("document.getElementById('c-text').value"), label.replace("Comment at ", "") + " a thought")
            self.assertTrue(b.js("document.activeElement === document.getElementById('c-text')"))
            b.js("document.getElementById('c-at').click()")                   # a time already there is replaced
            self.assertEqual(b.js("document.getElementById('c-text').value"), label.replace("Comment at ", "") + " a thought")
            self.assertEqual(self.text(ROW.format(self.t30) + " .row-sub .nc"), "· 1 comment")
            # the draft outlives a reload of the page (this tab)
            self.load(f"p={self.t30}")
            b.wait_js("document.getElementById('c-text').value.endsWith(' a thought')", 5, "draft kept")
            b.js("{ const t = document.getElementById('c-text'); t.value = ''; t.dispatchEvent(new Event('input')); }")
        finally:
            b.js("document.getElementById('audio').pause(); localStorage.clear(); sessionStorage.clear()")
            self.r.q("DELETE FROM positions")

    def test_2_a_time_on_another_version_plays_that_version(self):
        b = self.b
        try:
            self.api_post("Bob's version says it better at 0:05.", user=B, pid=self.two, episode_id=self.vb)
            self.comments_open(self.two, A)                 # Alice's own version plays for her
            self.assertEqual(self.text("#c-list .c-ver"), " · on Bob’s version")
            b.js("document.getElementById('audio').muted = true; document.querySelector('#c-list .c-time').click()")
            b.wait_js(f"(() => {{ const a = document.getElementById('audio'); return a.getAttribute('src') === '/audio/{self.vb}.mp3'"
                      " && !a.paused && a.currentTime >= 5; })()", 10, "Bob's version from 0:05")
            b.wait_js(f"document.querySelector('.ver[data-ep=\"{self.vb}\"]').getAttribute('aria-checked') === 'true'", 5, "picked")
            b.wait_js("!document.querySelector('#c-list .c-ver')", 5, "no longer another version")
        finally:
            b.js("document.getElementById('audio').pause(); localStorage.clear(); sessionStorage.clear()")
            self.r.q("DELETE FROM positions")

    def test_3_web_links_and_hostile_text(self):
        b = self.b
        self.api_post(HOSTILE, user=B)
        self.comments_open(self.t30, A)
        self.assertEqual(self.text("#c-list .c-body"), HOSTILE)
        got = json.loads(b.js("JSON.stringify({ kids: [...document.querySelectorAll('#c-list .c-body *')].map(e => e.tagName),"
                              " links: [...document.querySelectorAll('#c-list .c-body a')].map(a => [a.getAttribute('href'), a.textContent, a.rel, a.target]),"
                              " pwned: typeof window.__pwned, js: document.querySelectorAll('a[href^=\"javascript\"], a[href^=\"data\"]').length })"))
        self.assertEqual(got["kids"], ["A", "A"])
        self.assertEqual(got["links"], [["https://example.org/a_(b)", "https://example.org/a_(b)", "noopener noreferrer", "_blank"],
                                        ["https://example.org/q?x=1&y=2", "https://example.org/q?x=1&y=2", "noopener noreferrer", "_blank"]])
        self.assertEqual((got["pwned"], got["js"]), ("undefined", 0))
        # and on the board, the title of a paper is text too
        self.r.q("UPDATE papers SET title = ? WHERE id = ?", "<img src=x onerror=\"window.__pwned=3\">Fake", self.others[0])
        try:
            self.home(A)
            b.wait_js("[...document.querySelectorAll('#bd-list .bd-t')].some(x => x.textContent.startsWith('<img'))"
                      " || (document.getElementById('bd-more').click(), false)", 10, "the hostile title on the board")
            self.assertEqual(b.js("typeof window.__pwned + document.querySelectorAll('#board img').length"), "undefined0")
        finally:
            self.r.q("UPDATE papers SET title = 'Filler fake paper 0' WHERE id = ?", self.others[0])

    def test_4_live_in_a_second_browser(self):
        b = self.b
        self.comments_open(self.t30, A)
        b2 = Browser()
        try:
            b2.call("Log.enable")
            b2.viewport(1440, 900)
            b2.call("Network.setExtraHTTPHeaders", headers={"X-Test-User": B})
            b2.goto(self.base + f"/#p={self.t30}")
            b2.wait_js("!document.getElementById('comments').hidden && !!document.querySelector('#c-list .c-none')", 10, "Bob's page")
            # Bob writes; Alice's page shows it, and the row's count, with no reload
            b2.js("{ const t = document.getElementById('c-text'); t.value = 'Bob, live, at 0:03.'; t.dispatchEvent(new Event('input')); }")
            b2.js("document.getElementById('c-post').click()")
            b.wait_js("[...document.querySelectorAll('#c-list .c-body')].some(x => x.textContent === 'Bob, live, at 0:03.')", 5, "live on Alice's page")
            b.wait_js(f"(document.querySelector('{ROW.format(self.t30)} .row-sub .nc') || {{}}).textContent === '· 1 comment'", 5, "the count")
            self.assertEqual(self.text("#c-h"), "Comments (1)")
            cid = self.items()[0]
            self.assertEqual(self.acts(cid), ["reply", "delete"])        # Alice is an admin: not hers to edit
            # Bob replies: nested under it on Alice's page
            b2.js(f"document.querySelector('.c-item[data-id=\"{cid}\"] [data-act=reply]').click()")
            b2.wait_js("!!document.querySelector('.c-replybox textarea')", 3, "reply box")
            b2.js("{ const t = document.querySelector('.c-replybox textarea'); t.value = 'And a reply.'; t.dispatchEvent(new Event('input')); }")
            b2.js("document.querySelector('[data-act=send-reply]').click()")
            b.wait_js(f"(document.querySelector('.c-thread[data-id=\"{cid}\"] .c-reply .c-body') || {{}}).textContent === 'And a reply.'", 5, "the reply, nested")
            # the board hears it too
            b.wait_js("[...document.querySelectorAll('#bd-list .bd-text')].some(x => x.textContent === 'Bob commented on A Fake Paper Thirty Seconds Long (2 comments)')", 5, "on the board")
            # Alice is typing a reply of her own meanwhile: a live change leaves her box alone
            b.js(f"document.querySelector('.c-item[data-id=\"{cid}\"] [data-act=reply]').click()")
            b.js("{ const t = document.querySelector('.c-replybox textarea'); t.value = 'Half a thought'; t.dispatchEvent(new Event('input')); t.focus(); }")
            rid = b2.js("document.querySelector('.c-reply').dataset.id")
            b2.js(f"document.querySelector('.c-item[data-id=\"{rid}\"] [data-act=delete]').click()")
            b2.js(f"document.querySelector('.c-item[data-id=\"{rid}\"] [data-act=delete]').click()")
            b.wait_js(f"!document.querySelector('.c-item[data-id=\"{rid}\"]')", 5, "the reply gone on Alice's page")
            self.assertEqual(b.js("document.activeElement === document.querySelector('.c-replybox textarea') && document.activeElement.value"), "Half a thought")
            b.js("document.querySelector('.c-replybox .text-btn').click()")      # Cancel
            b2.pump(0.3)
            self.assertEqual(console_errors(b2), [], "errors in Bob's console")
        finally:
            b2.close()
            b.js("sessionStorage.clear()")

    def test_5_edit_and_delete_own_admins_delete_any(self):
        b = self.b
        self.comments_open(self.t30, CAROL)
        self.write("Carol's first.")
        b.wait_js("document.querySelectorAll('#c-list .c-item').length === 1", 5, "posted")
        cid = self.items()[0]
        self.assertEqual(self.acts(cid), ["reply", "edit", "delete"])
        b.js(f"document.querySelector('.c-item[data-id=\"{cid}\"] [data-act=edit]').click()")
        b.wait_js("document.activeElement && document.activeElement.value === \"Carol's first.\"", 3, "the edit box")
        b.js("{ const t = document.activeElement; t.value = \"Carol's first, better.\"; t.dispatchEvent(new Event('input')); }")
        b.js("document.querySelector('[data-act=save]').click()")
        b.wait_js(f"(document.querySelector('.c-item[data-id=\"{cid}\"] .c-body') || {{}}).textContent === \"Carol's first, better.\""
                  f" && document.querySelector('.c-item[data-id=\"{cid}\"] .c-meta').textContent.includes('edited')", 5, "edited")
        self.api_post("Bob answers.", user=B, parent_id=int(cid))
        # Bob: only Reply on Carol's; his own reply is his to edit or delete
        self.comments_open(self.t30, B)
        rid = self.items()[1]
        self.assertEqual((self.acts(cid), self.acts(rid)), (["reply"], ["reply", "edit", "delete"]))
        # Alice (admin) deletes Carol's: "comment deleted", with Bob's reply still under it
        self.comments_open(self.t30, A)
        self.assertEqual(self.acts(cid), ["reply", "delete"])
        b.js(f"document.querySelector('.c-item[data-id=\"{cid}\"] [data-act=delete]').click()")
        self.assertEqual(self.text(f".c-item[data-id=\"{cid}\"] [data-act=delete]"), "Delete now")
        b.js(f"document.querySelector('.c-item[data-id=\"{cid}\"] [data-act=delete]').click()")
        b.wait_js(f"(document.querySelector('.c-item[data-id=\"{cid}\"]') || {{}}).textContent === 'comment deleted'", 5, "placeholder")
        self.assertEqual(self.text(f".c-thread[data-id=\"{cid}\"] .c-reply .c-body"), "Bob answers.")
        self.assertEqual(self.text("#c-h"), "Comments (1)")

    def test_6_board_items_and_links(self):
        b, r = self.b, self.r
        st, g, _ = r.req("POST", "/api/graphs", {"name": "Fake Diffusion", "tags": ["diffusion"]}, user=B)
        gid = g["graph"]["id"]
        r.req("POST", "/api/links", {"src": self.t30, "dst": self.two, "grade": "s"}, user=B)
        c = self.api_post("A comment for the board.", user=A, pid=self.two)
        try:
            self.home(CAROL)
            b.wait_js("document.querySelectorAll('#bd-list .bd-item').length === 5", 5, "the board")
            t = self.board_texts()
            self.assertEqual(sorted(t[:2]), ["Alice commented on Two Fake Versions To Talk About", "Bob edited the graph Fake Diffusion (2 changes)"])   # the same second
            self.assertEqual(t[2], "Bob uploaded Filler fake paper 5")
            self.assertRegex(self.text("#bd-list .bd-item .bd-when"), r"^ · (just now|\d+ min ago)$")
            # more, and fewer again
            b.js("document.getElementById('bd-more').click()")
            b.wait_js("document.querySelectorAll('#bd-list .bd-item').length > 5", 5, "show more")
            t = self.board_texts()
            self.assertIn("Bob made a new version of Two Fake Versions To Talk About", t)
            self.assertIn("Carol joined", t)
            b.js("document.getElementById('bd-less').click()")
            b.wait_js("document.querySelectorAll('#bd-list .bd-item').length === 5", 5, "show fewer")
            # a comment's item opens the paper at that comment
            b.js("document.querySelector('#bd-list .bd-item[data-kind=comment] .bd-link').click()")
            b.wait_js(f"location.hash === '#p={self.two}' && !!document.querySelector('.c-item[data-id=\"{c['id']}\"].c-flash')", 5, "opened at the comment")
            # an upload's opens its paper
            b.js("[...document.querySelectorAll('#bd-list .bd-item[data-kind=upload] .bd-link')][0].click()")
            b.wait_js(f"location.hash === '#p={self.others[5]}'", 5, "opened the upload")
            # a graph's opens the map on that graph
            b.js("document.querySelector('#bd-list .bd-item[data-kind=graph] .bd-link').click()")
            b.wait_js("!document.getElementById('map').hidden && !!(window.PaperMap && window.PaperMap.current)", 10, "the map")
            b.wait_js(f"(() => {{ try {{ return PaperMap.current.debug.cur().id === '{gid}'; }} catch (e) {{ return true; }} }})()", 10, "on that graph")
            b.js("history.back()")
            b.wait_js("document.getElementById('map').hidden", 5, "map closed")
        finally:
            r.q("DELETE FROM links"); r.q("DELETE FROM graph_members"); r.q("DELETE FROM layout")
            r.q("UPDATE graphs SET deleted_at = '2026-09-01T00:00:00Z' WHERE id = ?", gid)
            r.q("DELETE FROM graph_log")

    def test_7_pinned_notice(self):
        b, r = self.b, self.r
        self.home(A)
        b.js("document.getElementById('bd-pin').click()")
        b.js("{ const t = document.getElementById('bd-nin'); t.value = 'Reading group Thursday 3 pm: the diffusion graph, https://example.org/rg'; t.dispatchEvent(new Event('input')); }")
        b.js("document.getElementById('bd-ngo').click()")
        b.wait_js("!document.getElementById('bd-notice').hidden && document.getElementById('bd-form').hidden", 5, "pinned")
        self.assertEqual(self.text("#bd-ntext"), "Reading group Thursday 3 pm: the diffusion graph, https://example.org/rg")
        self.assertEqual(b.js("document.querySelector('#bd-ntext a').getAttribute('href')"), "https://example.org/rg")
        self.assertIsNotNone(b.js("document.getElementById('bd-unpin')"))
        # Carol sees it on top, and cannot pin or unpin
        self.home(CAROL)
        b.wait_js("!document.getElementById('bd-notice').hidden", 5, "Carol sees it")
        self.assertTrue(b.js("document.getElementById('bd-pin').hidden && !document.getElementById('bd-unpin')"))
        self.assertTrue(b.js("document.getElementById('bd-notice').compareDocumentPosition(document.getElementById('bd-list')) & Node.DOCUMENT_POSITION_FOLLOWING"))
        # folded, the notice still shows
        b.js("document.getElementById('bd-toggle').click()")
        self.assertTrue(b.js("document.getElementById('bd-feed').hidden && !document.getElementById('bd-notice').hidden"))
        b.js("document.getElementById('bd-toggle').click()")
        # unpinned by Alice: gone from Carol's page, live
        n = r.req("GET", "/api/board", user=A)[1]["notice"]
        self.assertEqual(r.req("DELETE", f"/api/board/notice/{n['id']}", user=A)[0], 200)
        b.wait_js("document.getElementById('bd-notice').hidden", 5, "unpinned, live")
        b.js("localStorage.clear()")

    def test_8_unread_dot(self):
        b, r = self.b, self.r
        dot = "!document.querySelector('#bd-toggle .bd-dot').hidden"
        unread = "document.querySelectorAll('#bd-list .bd-item.unread').length"
        r.q("INSERT INTO board_seen(user_id, at) VALUES (?, '2026-09-01T08:00:00Z')", self.carol)
        self.api_post("Carol's own words.", user=CAROL)
        self.home(CAROL)
        b.wait_js(f"{unread} === 4", 5, "items since Carol last looked")
        self.assertEqual(b.js("document.querySelector('#bd-list .bd-item[data-kind=comment]').className"), "bd-item")   # her own: not new to her
        # open and on the screen: seen (the dot on the board goes, the new items keep theirs until the next visit)
        b.wait_js(f"!({dot})", 5, "seen")
        self.assertGreater(r.q("SELECT at FROM board_seen WHERE user_id = ?", self.carol)[0][0], "2026-09-02")
        self.assertEqual(b.js(unread), 4)
        self.home(CAROL)
        b.wait_js("document.querySelectorAll('#bd-list .bd-item').length === 5", 5, "board")
        b.pump(0.3)
        self.assertEqual((b.js(unread), b.js(dot)), (0, False))
        # folded away, something new: the dot; unfolded, seen
        b.js("document.getElementById('bd-toggle').click()")
        self.api_post("Something new from Bob.", user=B)
        b.wait_js(dot, 5, "the dot while folded")
        self.assertEqual(r.q("SELECT count(*) FROM board_seen WHERE user_id = ? AND at >= (SELECT max(created_at) FROM comments)", self.carol)[0][0], 0)
        b.js("document.getElementById('bd-toggle').click()")
        b.wait_js(f"!({dot}) && {unread} === 1", 5, "seen again")
        b.js("localStorage.clear()")

    def test_z_phone_tap_targets(self):
        b, r = self.b, self.r
        st, n, _ = r.req("POST", "/api/board/notice", {"body": "Reading group Thursday 3 pm, notes at https://example.org/notes"}, user=A)
        top = self.api_post("Look at 0:07 and https://example.org/fig-2 for the figure.", user=B)
        self.api_post("A reply at 0:09.", user=CAROL, parent_id=top["id"])
        self.api_post("Alice's own, to edit.", user=A)
        try:
            self.phone()
            self.home(A)
            b.wait_js("document.querySelectorAll('#bd-list .bd-item').length === 5 && !document.getElementById('bd-notice').hidden", 5, "board")
            self.assertTargets("the list with the board")
            self.no_side_scroll("the list with the board")
            b.js("document.getElementById('bd-pin').click()")
            self.assertTargets("the notice form")
            b.js("document.getElementById('bd-more').click()")
            b.wait_js("document.querySelectorAll('#bd-list .bd-item').length > 5", 5, "more")
            self.assertTargets("the board, more")
            self.shot("phone-board")
            b.js("document.getElementById('list-pane').scrollTop = 0")
            self.open(self.t30)
            b.wait_js("getComputedStyle(document.getElementById('win')).visibility === 'visible' && document.querySelectorAll('#c-list .c-item').length === 3", 10, "comments")
            self.assertTargets("comments")
            self.no_side_scroll("comments")
            b.js(f"document.querySelector('.c-item[data-id=\"{top['id']}\"] [data-act=reply]').click()")
            self.assertTargets("a reply being written")
            mine = self.items()[-1]
            b.js(f"document.querySelector('.c-item[data-id=\"{mine}\"] [data-act=edit]').click()")
            self.assertTargets("a comment being edited")
            b.js(f"document.querySelector('.c-item[data-id=\"{top['id']}\"] [data-act=delete]').click()")
            self.assertTargets("Delete now")
            self.no_side_scroll("comments, boxes open")
            self.shot("phone-comments")
        finally:
            b.viewport(1440, 900)
            b.js("localStorage.clear(); sessionStorage.clear()")


if __name__ == "__main__":
    unittest.main()
