"""hub/slack.py: an episode whose maker said yes at `papercast add` is announced in the group's
Slack channel once its audio lands, exactly once; none without the yes or without Slack set up;
the text and its escaping; retries on 429 and 500, giving up after the hour; no second post after
a restart (not even for a post a stopped hub left half-sent); the admins' tab and test message;
each person's default; and the webhook address never in a log line or an answer.

The real hub (contrib harness: app.serve, PCG_AUTH=header, a fake auth) and a fake Slack
Incoming Webhook (http.server on 127.0.0.1). Nothing reaches the real Slack.

    cd stacks/papercast-group/hub && python3 -m unittest tests.test_slack -v"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import contrib_harness as H
from hub import app, db, events, slack

SECRET = "B0PCGTEST/xoxSecretPathNeverLogged42"
MP3 = b"ID3\x04\x00\x00\x00\x00\x00\x00" + b"\xff\xfb\x90\x00" * 600
PUBLIC = "https://papercast.example.org"


class FakeSlack:
    """An Incoming Webhook: answers `script` in turn ((code, body, headers)), then 200 "ok"."""

    def __init__(self):
        self.script: list = []
        self.posts: list = []
        self.lock = threading.Lock()
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):          # its request line holds the secret path
                pass

            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                with fake.lock:
                    fake.posts.append({"path": self.path, "headers": dict(self.headers),
                                       "json": json.loads(raw or b"null")})
                    code, body, headers = fake.script.pop(0) if fake.script else (200, "ok", {})
                data = body.encode()
                self.send_response(code)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(data)))
                for k, v in headers.items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(data)

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.srv.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}/services/T0PCGTEST/{SECRET}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def texts(self) -> list:
        with self.lock:
            return [p["json"]["text"] for p in self.posts]

    def stop(self):
        self.srv.shutdown()
        self.srv.server_close()


class Capture(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.setFormatter(logging.Formatter("%(name)s %(levelname)s %(message)s"))
        self.lines: list = []

    def emit(self, record):
        self.lines.append(self.format(record))


class SlackTest(unittest.TestCase):
    def setUp(self):
        self.saved = {k: getattr(slack, k) for k in ("RETRY_BASE_S", "RETRY_FOR_S", "SWEEP_S")}
        slack.RETRY_BASE_S, slack.SWEEP_S = 0.05, 0.5
        self.logs = Capture()
        self.pcg = logging.getLogger("pcg")
        self.level = self.pcg.level
        self.pcg.setLevel(logging.DEBUG)
        self.pcg.addHandler(self.logs)
        self.h = H.Hub()
        self.slack = FakeSlack()
        self.hook = self.h.tmp / "slack.webhook"
        self.hook.write_text(self.slack.url + "\n")
        os.chmod(self.hook, 0o600)
        self.h.cfg.slack_webhook_file = str(self.hook)
        self.h.cfg.public_url = PUBLIC
        self.h.base_prompt()
        self.alice = self.h.user("Alice")
        self.bob = self.h.user("Bob")
        self.root = self.h.user("Root", role="admin")
        self.n = 0
        self.answers: list = []

    def tearDown(self):
        try:
            for line in self.logs.lines:
                self.assertNotIn(SECRET, line)
                self.assertNotIn(self.slack.url, line)
            for a in self.answers:
                self.assertNotIn(SECRET, a)
        finally:
            self.pcg.removeHandler(self.logs)
            self.pcg.setLevel(self.level)
            self.h.close()
            self.slack.stop()
            for k, v in self.saved.items():
                setattr(slack, k, v)

    # ------------------------------------------------------------------ helpers
    def req(self, method, path, body=None, user=None, browser=None, **kw):
        """Every answer is kept, and none may hold the webhook's secret (checked in tearDown)."""
        headers = dict(kw.pop("headers", None) or {})
        if browser is not None:
            headers.update({"X-Test-User": browser["email"], "X-PCG": "1"})
        code, out = self.h.request(method, path, body, user=user, headers=headers, **kw)
        self.answers.append(out.decode("utf-8", "replace") if isinstance(out, bytes) else json.dumps(out))
        return code, out

    def upload(self, user, announce="yes", paper_id=None, title=None, prefs=None) -> str:
        """A checked, queued episode; announce: "yes", "no" or None (a manifest without it)."""
        self.n += 1
        over = {"announce": {"slack": announce == "yes"}} if announce else {}
        if prefs is not None:
            over["prefs"] = prefs
        if paper_id:
            p = db.conn().execute("SELECT arxiv_id, title FROM papers WHERE id = ?", (paper_id,)).fetchone()
            m = H.manifest(paper_id=paper_id, paper_over={"arxiv_id": p["arxiv_id"], "title": p["title"]}, **over)
        else:
            arxiv, title = f"2302.{30000 + self.n}", title or f"Announce test paper {self.n}"
            code, cl = self.h.claim(user, arxiv_id=arxiv, title=title)
            self.assertEqual(code, 201, cl)
            m = H.manifest(claim_id=cl["claim_id"], paper_over={"arxiv_id": arxiv, "title": title}, **over)
        code, out = self.h.upload(user, H.bundle(m))
        self.assertEqual(code, 201, out)
        ep = self.h.wait_checked(user, out["episode_id"])
        self.assertEqual(ep["state"], "waiting-for-gpu", ep)
        return out["episode_id"]

    def voice(self, eid, seconds=1260.0):
        """The worker takes it and sends its audio: the episode turns ready."""
        code, job = self.h.request("POST", "/api/voice/claim", {"worker": "w1"}, worker=True)
        self.assertEqual((code, job.get("episode_id")), (200, eid))
        code, out = self.h.request("PUT", f"/api/voice/{eid}/audio", body=MP3, ctype="audio/mpeg",
                                   headers={"X-Duration-S": str(seconds)}, worker=True)
        self.assertEqual((code, out.get("state")), (200, "ready"), out)

    def row(self, eid):
        return db.conn().execute("SELECT * FROM slack_posts WHERE episode_id = ?", (eid,)).fetchone()

    def wait_row(self, eid, *states, timeout=10.0):
        end = time.time() + timeout
        while time.time() < end:
            r = self.row(eid)
            if r is not None and r["state"] in states:
                return r
            time.sleep(0.02)
        r = self.row(eid)
        self.fail(f"{eid}: slack_posts is {dict(r) if r else None}, not {states}")

    def wait_posts(self, n, timeout=10.0):
        end = time.time() + timeout
        while time.time() < end and len(self.slack.posts) < n:
            time.sleep(0.02)
        self.assertEqual(len(self.slack.posts), n, self.slack.texts())

    def paper_of(self, eid) -> str:
        return db.conn().execute("SELECT paper_id FROM episodes WHERE id = ?", (eid,)).fetchone()[0]

    def restart(self):
        """The hub stopped and started again on the same data (the threads are the process's)."""
        self.h.srv.shutdown()
        self.h.srv.server_close()
        self.h.srv = app.serve(self.h.cfg)
        self.h.port = self.h.srv.server_address[1]
        threading.Thread(target=self.h.srv.serve_forever, daemon=True).start()

    # ------------------------------------------------------------------ tests
    def test_posted_once_when_the_audio_lands(self):
        code, f = self.req("GET", "/api/cli/features", user=self.alice)
        self.assertEqual((code, f), (200, {"slack": {"enabled": True, "channel": "#t-machinelearning",
                                                     "default": True}}))
        eid = self.upload(self.alice, title="Score-Based Generative Modeling")
        time.sleep(0.3)
        self.assertEqual(self.slack.posts, [])              # checked and queued is not ready
        self.assertIsNone(self.row(eid))
        self.voice(eid, seconds=1260.0)
        self.wait_posts(1)
        pid = self.paper_of(eid)
        p = self.slack.posts[0]
        self.assertEqual(p["path"], f"/services/T0PCGTEST/{SECRET}")
        self.assertEqual(p["json"], {"text": f"*<{PUBLIC}/#p={pid}|Score-Based Generative Modeling>* — "
                                             "added by Alice · 21 min",
                                     "unfurl_links": False, "unfurl_media": False})
        self.assertTrue(p["headers"]["Content-Type"].startswith("application/json"))
        r = self.wait_row(eid, "posted")
        self.assertEqual((r["attempts"], r["user_id"]), (1, self.alice["id"]))
        self.assertIsNotNone(r["sent_at"])
        events.publish("episode", {"id": eid, "state": "ready"})     # the same news again
        time.sleep(0.5)
        self.assertEqual(len(self.slack.posts), 1)

    def test_none_without_a_yes(self):
        e1 = self.upload(self.alice, announce=None)          # an older client: no announce at all
        e2 = self.upload(self.bob, announce="no")
        self.voice(e1)
        self.voice(e2)
        time.sleep(0.6)
        self.assertEqual(self.slack.posts, [])
        self.assertIsNone(self.row(e1))
        self.assertIsNone(self.row(e2))

    def test_none_when_slack_is_not_set_up(self):
        self.h.cfg.slack_webhook_file = ""
        code, f = self.req("GET", "/api/cli/features", user=self.alice)
        self.assertEqual(f["slack"]["enabled"], False)
        eid = self.upload(self.alice)
        self.voice(eid)
        r = self.wait_row(eid, "skipped")
        self.assertIn("not set up", r["detail"])
        # set up afterwards: an episode from before is not announced late
        self.h.cfg.slack_webhook_file = str(self.hook)
        self.restart()
        time.sleep(0.6)
        self.assertEqual(self.slack.posts, [])
        self.assertEqual(self.row(eid)["state"], "skipped")

    def test_the_text_and_its_escaping(self):
        self.assertEqual(slack.esc("Q&A <b> > & <"), "Q&amp;A &lt;b&gt; &gt; &amp; &lt;")
        self.assertEqual(slack.message("https://h.org/", "p_x", "  A   <Title>\n", "B&B", 89.0, False),
                         "*<https://h.org/#p=p_x|A &lt;Title&gt;>* — added by B&amp;B · 1 min")
        self.assertEqual(slack.message("https://h.org", "p_x", "T", "Ann", 1500, True, "derivations · practical"),
                         "*<https://h.org/#p=p_x|T>* — added by Ann · 25 min (new version: derivations · practical)")
        self.assertEqual(slack.message("https://h.org", "p_x", "T", "Ann", None, True), "*<https://h.org/#p=p_x|T>* — added by Ann (new version)")
        # through the hub: a title and a name with <, > and &; then Bob's own version of it
        with db.transaction() as c:
            c.execute("UPDATE users SET name = ? WHERE id = ?", ("Alice <admin> & co", self.alice["id"]))
        e1 = self.upload(self.alice, title="Q&A: <Scores> > Samples")
        self.voice(e1, seconds=600)
        self.wait_posts(1)
        pid = self.paper_of(e1)
        self.assertEqual(self.slack.texts()[0], f"*<{PUBLIC}/#p={pid}|Q&amp;A: &lt;Scores&gt; &gt; Samples>* — "
                                                "added by Alice &lt;admin&gt; &amp; co · 10 min")
        e2 = self.upload(self.bob, paper_id=pid,
                         prefs={"settings": {"maths": "full", "emphasis": "practice"}, "note": "", "version": 1})
        self.voice(e2, seconds=1500)
        self.wait_posts(2)
        self.assertEqual(self.slack.texts()[1], f"*<{PUBLIC}/#p={pid}|Q&amp;A: &lt;Scores&gt; &gt; Samples>* — "
                                                "added by Bob · 25 min (new version: derivations · practical)")

    def test_retries_on_429_and_500(self):
        self.slack.script = [(429, "rate_limited", {"Retry-After": "0"}), (500, "rollup_error", {})]
        eid = self.upload(self.alice)
        self.voice(eid)
        r = self.wait_row(eid, "posted")
        self.assertEqual(r["attempts"], 3)
        self.assertEqual(len(self.slack.posts), 3)
        self.assertEqual(len(set(self.slack.texts())), 1)    # the same message each time
        time.sleep(0.4)
        self.assertEqual(len(self.slack.posts), 3)

    def test_gives_up_after_the_hour_and_tells_only_the_admins(self):
        slack.RETRY_FOR_S = 0.4
        self.slack.script = [(503, "service unavailable", {})] * 100
        eid = self.upload(self.alice)
        self.voice(eid)
        r = self.wait_row(eid, "failed")
        self.assertIn("gave up", r["detail"])
        self.assertIn("Slack answered 503", r["detail"])
        n = len(self.slack.posts)
        self.assertGreaterEqual(n, 2)
        time.sleep(0.3)
        self.assertEqual(len(self.slack.posts), n)
        # the uploader sees a ready episode and nothing about Slack
        code, ep = self.req("GET", f"/api/cli/episodes/{eid}", user=self.alice)
        self.assertEqual(ep["state"], "ready")
        self.assertNotIn("slack", json.dumps(ep).lower())
        code, _ = self.req("GET", "/api/admin/slack", browser=self.alice)
        self.assertEqual(code, 403)
        code, a = self.req("GET", "/api/admin/slack", browser=self.root)
        self.assertEqual(code, 200)
        self.assertEqual((a["posts"][0]["episode_id"], a["posts"][0]["state"]), (eid, "failed"))
        self.assertTrue(any("gave up posting" in line for line in self.logs.lines))

    def test_a_refusal_is_not_repeated(self):
        self.slack.script = [(404, "no_service", {})]
        eid = self.upload(self.alice)
        self.voice(eid)
        r = self.wait_row(eid, "failed")
        self.assertEqual((r["attempts"], r["detail"]), (1, "Slack answered 404: no_service"))
        time.sleep(0.4)
        self.assertEqual(len(self.slack.posts), 1)

    def test_no_second_post_after_a_restart(self):
        eid = self.upload(self.alice)
        self.voice(eid)
        self.wait_row(eid, "posted")
        self.restart()
        events.publish("episode", {"id": eid, "state": "ready"})
        time.sleep(0.6)
        self.assertEqual(len(self.slack.posts), 1)
        # a post a stopped hub left half-sent may have reached Slack: never sent again
        slack.RETRY_BASE_S = 100.0
        self.slack.script = [(500, "rollup_error", {})]
        e2 = self.upload(self.bob)
        self.voice(e2)
        self.wait_row(e2, "retrying")
        with db.transaction() as c:
            c.execute("UPDATE slack_posts SET state = 'sending', next_t = 0 WHERE episode_id = ?", (e2,))
        self.restart()
        r = self.wait_row(e2, "failed")
        self.assertIn("never posted twice", r["detail"])
        time.sleep(0.6)
        self.assertEqual(len(self.slack.posts), 2)
        # an episode that turned ready with nobody listening (the hub stopped in between) is
        # announced at the next start
        slack.RETRY_BASE_S = 0.05
        e3 = self.upload(self.alice)
        with db.transaction() as c:
            c.execute("UPDATE episodes SET state = 'ready', duration_s = 900, updated_at = ? WHERE id = ?",
                      (db.now(), e3))
        time.sleep(0.3)
        self.assertEqual(len(self.slack.posts), 2)
        self.restart()
        self.wait_row(e3, "posted")
        self.assertEqual(len(self.slack.posts), 3)
        self.assertIn("· 15 min", self.slack.texts()[2])

    def test_a_deleted_episode_is_not_announced(self):
        slack.RETRY_BASE_S = 0.3
        self.slack.script = [(500, "rollup_error", {})]
        eid = self.upload(self.alice)
        self.voice(eid)
        self.wait_row(eid, "retrying")
        with db.transaction() as c:
            c.execute("UPDATE episodes SET deleted_at = ? WHERE id = ?", (db.now(), eid))
        r = self.wait_row(eid, "skipped")
        self.assertIn("deleted", r["detail"])
        self.assertEqual(len(self.slack.posts), 1)

    def test_the_admins_tab_and_test_message(self):
        eid = self.upload(self.alice, title="Flow Matching")
        self.voice(eid)
        self.wait_row(eid, "posted")
        code, a = self.req("GET", "/api/admin/slack", browser=self.root)
        self.assertEqual(code, 200)
        self.assertEqual((a["configured"], a["problem"], a["channel"]), (True, None, "#t-machinelearning"))
        self.assertEqual(len(a["posts"]), 1)
        p = a["posts"][0]
        self.assertEqual((p["episode_id"], p["title"], p["made_by"]["name"], p["state"], p["attempts"]),
                         (eid, "Flow Matching", "Alice", "posted", 1))
        # the test message: sent now, and its answer says so
        code, t = self.req("POST", "/api/admin/slack/test", {}, browser=self.root)
        self.assertEqual((code, t), (200, {"ok": True, "detail": "sent"}))
        self.assertIn("A test from papercast", self.slack.texts()[-1])
        self.assertIn("sent by Root", self.slack.texts()[-1])
        self.slack.script = [(403, "action_prohibited", {})]
        code, t = self.req("POST", "/api/admin/slack/test", {}, browser=self.root)
        self.assertEqual((code, t), (200, {"ok": False, "detail": "Slack answered 403: action_prohibited"}))
        # admins only, and the page's CSRF header
        self.assertEqual(self.req("POST", "/api/admin/slack/test", {}, browser=self.alice)[0], 403)
        code, _ = self.h.request("POST", "/api/admin/slack/test", {}, headers={"X-Test-User": self.root["email"]})
        self.assertEqual(code, 403)
        self.assertEqual(len(self.slack.posts), 3)
        # the channel's name comes from PCG_SLACK_CHANNEL
        self.h.cfg.slack_channel = "ml-papers"
        self.assertEqual(self.req("GET", "/api/admin/slack", browser=self.root)[1]["channel"], "#ml-papers")
        self.assertEqual(self.req("GET", "/api/cli/features", user=self.bob)[1]["slack"]["channel"], "#ml-papers")
        # the last 20 only
        with db.transaction() as c:
            for i in range(25):
                c.execute("INSERT INTO slack_posts (episode_id, user_id, state, attempts, first_t, created_at, updated_at) "
                          "VALUES (?, ?, 'skipped', 0, 0, ?, ?)", (f"e_old{i:02d}", self.bob["id"], "2026-01-01T00:00:00Z",
                                                                  "2026-01-01T00:00:00Z"))
        a = self.req("GET", "/api/admin/slack", browser=self.root)[1]
        self.assertEqual(len(a["posts"]), 20)
        self.assertEqual(a["posts"][0]["episode_id"], eid)

    def test_each_persons_default(self):
        f = lambda u: self.req("GET", "/api/cli/features", user=u)[1]["slack"]["default"]  # noqa: E731
        self.assertTrue(f(self.alice))
        code, out = self.req("PUT", "/api/cli/slack", {"default": False}, user=self.alice)
        self.assertEqual((code, out["default"]), (200, False))
        self.assertFalse(f(self.alice))
        self.assertTrue(f(self.bob))                        # hers only
        code, out = self.req("GET", "/api/slack", browser=self.alice)
        self.assertEqual(out, {"enabled": True, "channel": "#t-machinelearning", "default": False, "can_upload": True})
        code, out = self.req("PUT", "/api/slack", {"default": True}, browser=self.alice)
        self.assertEqual((code, out["default"]), (200, True))
        self.assertTrue(f(self.alice))
        self.assertEqual(self.req("PUT", "/api/slack", {"default": "yes"}, browser=self.alice)[0], 400)
        self.assertEqual(self.req("PUT", "/api/cli/slack", {}, user=self.alice)[0], 400)
        code, _ = self.h.request("PUT", "/api/slack", {"default": False}, headers={"X-Test-User": self.alice["email"]})
        self.assertEqual(code, 403)                         # no X-PCG: not from the page
        self.assertTrue(f(self.alice))
        viewer = self.h.user("Vera", role="viewer")
        self.assertEqual(self.req("GET", "/api/slack", browser=viewer)[1]["can_upload"], False)
        self.assertEqual(self.req("GET", "/api/cli/features")[0], 401)

    def test_the_webhook_file_and_what_is_said_about_it(self):
        def admin():
            return self.req("GET", "/api/admin/slack", browser=self.root)[1]

        os.chmod(self.hook, 0o644)
        a = admin()
        self.assertEqual((a["configured"], a["problem"]), (False, f"{self.hook} can be read by others: chmod 600 it"))
        self.assertFalse(self.req("GET", "/api/cli/features", user=self.alice)[1]["slack"]["enabled"])
        self.assertEqual(self.req("POST", "/api/admin/slack/test", {}, browser=self.root)[1]["ok"], False)
        os.chmod(self.hook, 0o600)
        self.h.cfg.slack_webhook_file = self.slack.url                  # the address instead of a file
        a = admin()
        self.assertFalse(a["configured"])
        self.assertIn("not the address itself", a["problem"])
        self.h.cfg.slack_webhook_file = str(self.h.tmp / "missing")
        self.assertIn("does not exist", admin()["problem"])
        bad = self.h.tmp / "bad.webhook"
        bad.write_text("ftp://example.org/" + SECRET)
        os.chmod(bad, 0o600)
        self.h.cfg.slack_webhook_file = str(bad)
        self.assertIn("does not hold a webhook address", admin()["problem"])
        self.h.cfg.slack_webhook_file = ""
        self.assertIn("PCG_SLACK_WEBHOOK_FILE is not set", admin()["problem"])
        # an address nobody answers at: tried again, and the detail never has the address
        dead = self.h.tmp / "dead.webhook"
        dead.write_text(f"http://127.0.0.1:{H_free_port()}/services/T0PCGTEST/{SECRET}")
        os.chmod(dead, 0o600)
        self.h.cfg.slack_webhook_file = str(dead)
        eid = self.upload(self.alice)
        self.voice(eid)
        r = self.wait_row(eid, "retrying")
        self.assertIn("no answer from Slack", r["detail"])
        self.assertNotIn(SECRET, r["detail"])
        t = self.req("POST", "/api/admin/slack/test", {}, browser=self.root)[1]
        self.assertFalse(t["ok"])
        self.assertIn("no answer from Slack", t["detail"])
        # scrub: the address and its path go, the rest stays
        u = "https://hooks.slack.com/services/T1/B2/abc"
        self.assertEqual(slack.scrub(f"x {u} y /services/T1/B2/abc z", u), "x <webhook> y <webhook> z")

    def test_env_settings_when_the_config_has_none(self):
        """The hub's Config has no Slack fields: hub.env's PCG_SLACK_* are read from the environment."""
        cfg = H.config.load({"PCG_DATA": str(self.h.tmp / "d2"), "PCG_AUTH": "header"})
        old = {k: os.environ.get(k) for k in ("PCG_SLACK_WEBHOOK_FILE", "PCG_SLACK_CHANNEL")}
        try:
            os.environ["PCG_SLACK_WEBHOOK_FILE"] = str(self.hook)
            os.environ["PCG_SLACK_CHANNEL"] = "#reading-group"
            self.assertEqual(slack.webhook(cfg), (self.slack.url, None))
            self.assertEqual(slack.channel(cfg), "#reading-group")
            del os.environ["PCG_SLACK_CHANNEL"]
            self.assertEqual(slack.channel(cfg), "#t-machinelearning")
            del os.environ["PCG_SLACK_WEBHOOK_FILE"]
            self.assertFalse(slack.enabled(cfg))
        finally:
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v


# ---------------------------------------------------------------- the page (headless Chrome)

from .test_page import SHOTS, SKIP, PageBase  # noqa: E402,F401  (only these: the loader would run any imported case)


@unittest.skipIf(SKIP, SKIP or "")
class SlackPage(PageBase):
    """Settings -> Preferences: a contributor's default, saved at once; Settings -> Slack for
    admins: set up or not, the test message, the last posts."""

    @classmethod
    def fill(cls):
        super().fill()
        r = cls.r
        cls.fake = FakeSlack()
        hook = r.data / "slack.webhook"
        hook.write_text(cls.fake.url)
        os.chmod(hook, 0o600)
        cls.hook = str(hook)
        r.cfg.slack_webhook_file = cls.hook
        cls.pid = r.paper("Flow Matching for Generative Modeling", cls.bob)
        cls.eid = r.episode(cls.pid, cls.bob, summary="derivations")

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        cls.fake.stop()

    def settings(self, user, tab):
        self.home(user)
        self.b.js(f"location.hash = 'settings={tab}'")
        self.b.wait_js(f"document.getElementById('tab-{tab}') && document.getElementById('tab-{tab}').getAttribute('aria-selected') === 'true'"
                       " && !document.getElementById('set-loading')", 10, f"settings {tab}")

    def test_the_default_in_preferences(self):
        from .test_page import B, C
        b, r = self.b, self.r
        try:
            self.settings(B, "prefs")
            b.wait_js("!!document.getElementById('pref-slack')", 5, "the Slack choice")
            self.assertEqual(self.text("#pref-slack .pref-h"), "Post my new episodes to #t-machinelearning")
            checked = "[...document.querySelectorAll('#pref-slack .seg button')].filter(x => x.getAttribute('aria-checked') === 'true').map(x => x.textContent)"
            self.assertEqual(b.js(checked), ["Yes"])
            b.js("[...document.querySelectorAll('#pref-slack .seg button')].find(x => x.dataset.v === 'false').click()")
            b.wait_js("document.getElementById('slack-msg').textContent === 'Saved'", 5, "saved")
            self.assertEqual(r.q("SELECT post FROM slack_prefs WHERE user_id = ?", self.bob)[0][0], 0)
            self.assertEqual(b.js(checked), ["No"])
            self.assertTrue(b.js("document.getElementById('pref-save').disabled"))     # the other Save is not involved
            self.shot("slack-preferences")
            self.phone()
            self.load("settings=prefs")
            b.wait_js("!!document.getElementById('pref-slack')", 5, "the Slack choice on a phone")
            self.assertEqual(b.js(checked), ["No"])
            self.assertTargets("preferences with Slack")
            self.shot("slack-preferences-phone")
            self.no_side_scroll("preferences with Slack")
            b.call("Emulation.clearDeviceMetricsOverride")
            b.viewport(1440, 900)
            # a viewer adds nothing, so has nothing to choose; nor anyone while Slack is not set up
            self.settings(C, "prefs")
            b.pump(0.5)
            self.assertIsNone(b.js("document.getElementById('pref-slack')"))
            r.cfg.slack_webhook_file = ""
            self.settings(B, "prefs")
            b.pump(0.5)
            self.assertIsNone(b.js("document.getElementById('pref-slack')"))
        finally:
            r.cfg.slack_webhook_file = self.hook
            r.q("DELETE FROM slack_prefs")

    def test_the_admins_tab(self):
        from .test_page import A, B
        b, r = self.b, self.r
        tabs = "[...document.querySelectorAll('#set-tabs .tab')].map(t => t.textContent)"
        try:
            with db.transaction() as c:
                c.execute("INSERT INTO slack_posts (episode_id, user_id, state, attempts, first_t, created_at, sent_at, "
                          "updated_at) VALUES (?, ?, 'posted', 1, 0, ?, ?, ?)", (self.eid, self.bob, db.now(), db.now(), db.now()))
            self.settings(A, "slack")
            self.assertEqual(b.js(tabs)[-1], "Slack")
            self.assertTrue(self.text("#slack-status").startswith("Set up: "))
            self.assertEqual(b.js("[...document.querySelectorAll('#slack-posts .it-t')].map(x => x.textContent)"),
                             ["Flow Matching for Generative Modeling"])
            self.assertIn("by Bob · posted", self.text("#slack-posts .it-s"))
            b.js("document.getElementById('slack-test').click()")
            b.wait_js("document.getElementById('slack-test-msg').textContent === 'Sent to #t-machinelearning'", 10, "test sent")
            self.assertEqual(len(self.fake.posts), 1)
            self.assertIn("A test from papercast", self.fake.texts()[0])
            self.assertNotIn(SECRET, b.js("document.body.innerHTML"))
            self.shot("slack-admin-tab")
            # a refusal is shown where the button is
            self.fake.script = [(404, "no_service", {})]
            b.js("document.getElementById('slack-test').click()")
            b.wait_js("document.getElementById('slack-test-msg').textContent === 'Slack answered 404: no_service'", 10, "refusal")
            self.phone()
            self.load("settings=slack")
            b.wait_js("!!document.getElementById('slack-test')", 5, "the Slack tab on a phone")
            self.assertTargets("the Slack tab")
            self.no_side_scroll("the Slack tab")
            b.call("Emulation.clearDeviceMetricsOverride")
            b.viewport(1440, 900)
            # not set up: says why, and there is nothing to test
            r.cfg.slack_webhook_file = ""
            self.settings(A, "slack")
            self.assertEqual(self.text("#slack-status"), "Not set up: PCG_SLACK_WEBHOOK_FILE is not set in hub.env")
            self.assertTrue(b.js("document.getElementById('slack-test').disabled"))
            # a contributor has no Slack tab
            self.settings(B, "prefs")
            self.assertNotIn("Slack", b.js(tabs))
        finally:
            r.cfg.slack_webhook_file = self.hook
            r.q("DELETE FROM slack_posts")


def H_free_port() -> int:
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


if __name__ == "__main__":
    unittest.main()
