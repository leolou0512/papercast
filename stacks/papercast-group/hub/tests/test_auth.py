"""hub/auth.py against a real hub: app.serve on a free port with a temporary PCG_DATA.

    cd stacks/papercast-group && python3 -m unittest hub.tests.test_auth -v

The Cloudflare Access tests make RSA keys and a self-signed TLS certificate with `openssl` and
serve a fake JWKS over https on 127.0.0.1 (the hub's certs URL and CA file are injected through
cfg.cf_certs_url / cfg.cf_ca_file); they are skipped when openssl is missing.
Every level gets a tiny echo route (/_t/<level>, GET and POST) added to auth.ROUTES for these
tests only, so each level is checked through the real server."""
from __future__ import annotations

import base64
import hashlib
import hmac
import http.client
import json
import os
import re
import shutil
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]          # stacks/papercast-group
sys.path.insert(0, str(ROOT))
from hub import app, auth, db                       # noqa: E402
from hub import config as C                         # noqa: E402

PUBLIC = "https://hub.example"
SECRET = "test-secret-0123456789abcdef"
WORKER = "the-voice-workers-token"
BOSS = "boss@example.com"                           # in PCG_ADMIN_EMAILS
OPENSSL = shutil.which("openssl")
CODE_RX = r"^[A-HJ-NP-Z2-9]{4}-[A-HJ-NP-Z2-9]{4}$"


def sha(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def b64e(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _echo(req):
    req.send_json(200, {"user": req.user, "token_id": getattr(req, "token_id", None)})


TEST_ROUTES = [(m, rf"^/_t/{lvl}$", _echo, lvl) for lvl in auth.LEVELS for m in ("GET", "POST")]


def setUpModule():
    auth.ROUTES.extend(TEST_ROUTES)


def tearDownModule():
    for r in TEST_ROUTES:
        auth.ROUTES.remove(r)


class Hub:
    def __init__(self, mode: str, **env):
        self.dir = tempfile.mkdtemp(prefix="pcg-auth-")
        self.env = {"PCG_DATA": self.dir, "PCG_PORT": "0", "PCG_AUTH": mode, "PCG_SECRET": SECRET,
                    "PCG_PUBLIC_URL": PUBLIC, "PCG_ADMIN_EMAILS": BOSS, "PCG_WORKER_TOKEN_SHA256": sha(WORKER), **env}
        self.cfg = C.load(self.env)
        self.srv = None

    def start(self):
        self.srv = app.serve(self.cfg)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        return self

    def close(self):
        if self.srv:
            self.srv.shutdown()
            self.srv.server_close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def req(self, method, path, body=None, *, user=None, token=None, cookie=None, headers=None, csrf=True, source=None):
        h = {}
        if user:
            h["X-Test-User"] = user
        if token:
            h["Authorization"] = "Bearer " + token
        if cookie:
            h["Cookie"] = cookie
        if csrf and method not in ("GET", "HEAD"):
            h["X-PCG"] = "1"
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            h["Content-Type"] = "application/json"
        h.update(headers or {})
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30, source_address=(source, 0) if source else None)
        try:
            c.request(method, path, body=data, headers=h)
            r = c.getresponse()
            raw = r.read()
        finally:
            c.close()
        try:
            j = json.loads(raw)
        except ValueError:
            j = raw
        return r.status, j, r

    def q(self, sql, args=()):
        return db.conn().execute(sql, args).fetchall()

    def db_bytes(self) -> bytes:
        return b"".join(p.read_bytes() for p in Path(self.dir).glob("hub.db*"))

    def device_login(self, approver: dict, device="laptop") -> str:
        s, st, _ = self.req("POST", "/api/cli/login/start", {"device": device})
        assert s == 200, st
        s, j, _ = self.req("POST", "/api/cli/login/approve", {"code": st["code"], "approve": True}, **approver)
        assert s == 200, j
        s, p, _ = self.req("POST", "/api/cli/login/poll", {"poll": st["poll"]})
        assert s == 200, p
        return p["token"]


def compare_digest_spy():
    return mock.patch.object(auth.hmac, "compare_digest", wraps=hmac.compare_digest)


# ============================================================ header mode (tests only) and the levels

class HeaderModeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.hub = Hub("header").start()

    @classmethod
    def tearDownClass(cls):
        cls.hub.close()

    def setUp(self):
        self.r = self.hub.req

    def uid(self, email):
        return self.r("GET", "/api/me", user=email)[1]["id"]

    def test_public_needs_nobody_viewer_needs_someone(self):
        s, j, _ = self.r("GET", "/_t/public")
        self.assertEqual((s, j["user"]), (200, None))
        s, j, _ = self.r("GET", "/_t/viewer")
        self.assertEqual((s, j["error"]), (401, "login_required"))

    def test_first_sight_makes_a_viewer_named_after_the_email(self):
        s, j, _ = self.r("GET", "/api/me", user="Carol.X@Example.com")
        self.assertEqual(s, 200)
        self.assertEqual((j["email"], j["name"], j["role"], j["disabled"], j["auth"]),
                         ("carol.x@example.com", "carol.x", "viewer", False, "header"))
        self.assertEqual(self.r("GET", "/api/me", user=BOSS)[1]["role"], "admin")

    def test_header_trusted_only_from_127_0_0_1(self):
        self.assertEqual(self.r("GET", "/_t/viewer", user="dora@example.com")[0], 200)
        self.assertEqual(self.r("GET", "/_t/viewer", user="dora@example.com", source="127.0.0.2")[0], 401)
        self.assertEqual(self.r("GET", "/_t/cli", user="dora@example.com", source="127.0.0.2")[0], 401)

    def test_header_refused_through_a_proxy(self):
        for h in auth.PROXY_HEADERS:
            s, j, _ = self.r("GET", "/_t/viewer", user="dora@example.com", headers={h: "203.0.113.9"})
            self.assertEqual(s, 401, h)

    def test_roles_rank(self):
        e = "ed@example.com"
        self.assertEqual(self.r("GET", "/_t/viewer", user=e)[0], 200)
        self.assertEqual(self.r("GET", "/_t/contributor", user=e)[1]["error"], "forbidden")
        self.assertEqual(self.r("GET", "/_t/admin", user=e)[0], 403)
        self.assertEqual(self.r("PUT", f"/api/admin/users/{self.uid(e)}", {"role": "contributor"}, user=BOSS)[0], 200)
        self.assertEqual(self.r("GET", "/_t/contributor", user=e)[0], 200)
        self.assertEqual(self.r("GET", "/_t/admin", user=e)[0], 403)
        self.assertEqual(self.r("GET", "/_t/admin", user=BOSS)[0], 200)

    def test_csrf(self):
        e = "fay@example.com"
        s, j, _ = self.r("POST", "/_t/viewer", {}, user=e, csrf=False)
        self.assertEqual((s, j["error"]), (403, "csrf"))
        self.assertEqual(self.r("POST", "/_t/viewer", {}, user=e)[0], 200)
        for origin, want in (("https://evil.example", 403), ("null", 403), ("http://hub.example", 403),
                             ("https://hub.example.evil", 403), ("https://hub.example:8443", 403),
                             (PUBLIC, 200), ("https://HUB.example:443", 200)):
            self.assertEqual(self.r("POST", "/_t/viewer", {}, user=e, headers={"Origin": origin})[0], want, origin)
        self.assertEqual(self.r("POST", "/_t/viewer", {}, user=e, headers={"Sec-Fetch-Site": "cross-site"})[0], 403)
        self.assertEqual(self.r("POST", "/_t/viewer", {}, user=e, headers={"Sec-Fetch-Site": "same-origin"})[0], 200)
        self.assertEqual(self.r("POST", "/_t/viewer", {}, user=e, headers={"X-PCG": "0"})[0], 403)
        # reads: a foreign or opaque Origin is refused too; no Origin is fine
        self.assertEqual(self.r("GET", "/_t/viewer", user=e, headers={"Origin": "null"})[0], 403)
        self.assertEqual(self.r("GET", "/_t/viewer", user=e, headers={"Origin": "https://evil.example"})[0], 403)
        self.assertEqual(self.r("GET", "/_t/viewer", user=e)[0], 200)
        # a refused request changes nothing, not even a first-sight user row
        self.assertEqual(self.r("PUT", "/api/me", {"name": "Hacked"}, user=e, csrf=False)[0], 403)
        self.assertEqual(self.r("GET", "/api/me", user=e)[1]["name"], "fay")
        self.assertEqual(self.r("POST", "/_t/viewer", {}, user="new.person@example.com", headers={"Origin": "https://evil.example"})[0], 403)
        self.assertIsNone(auth.user_by_email("new.person@example.com"))

    def test_device_login(self):
        erin = {"user": "erin@example.com"}
        s, st, _ = self.r("POST", "/api/cli/login/start", {"device": "Erin's laptop\n\x07 "})
        self.assertEqual(s, 200)
        self.assertRegex(st["code"], CODE_RX)
        self.assertEqual(st["url"], f"{PUBLIC}/cli?code={st['code']}")
        self.assertEqual((st["interval"], st["expires_in"]), (2, 600))
        row = self.hub.q("SELECT * FROM cli_logins WHERE code = ?", (st["code"],))[0]
        self.assertEqual((row["device"], row["state"], row["poll_hash"]), ("Erin's laptop", "pending", sha(st["poll"])))
        self.assertEqual(auth._unix(row["expires_at"]) - auth._unix(row["created_at"]), 600)

        s, j, _ = self.r("POST", "/api/cli/login/poll", {"poll": st["poll"]})
        self.assertEqual((s, j["error"]), (428, "pending"))
        s, info, _ = self.r("GET", "/api/cli/login/info?code=" + st["code"].lower().replace("-", ""), **erin)
        self.assertEqual((s, info["device"], info["state"], info["code"]), (200, "Erin's laptop", "pending", st["code"]))
        self.assertTrue(590 <= info["expires_in"] <= 600)
        self.assertEqual(self.r("POST", "/api/cli/login/approve", {"code": st["code"], "approve": True}, **erin, csrf=False)[0], 403)
        s, j, _ = self.r("POST", "/api/cli/login/approve", {"code": st["code"], "approve": True}, **erin)
        self.assertEqual((s, j["state"]), (200, "approved"))

        s, p, _ = self.r("POST", "/api/cli/login/poll", {"poll": st["poll"]})
        self.assertEqual(s, 200)
        tok = p["token"]
        self.assertRegex(tok, r"^pcg_[a-z2-7]{32}$")
        self.assertEqual(p["user"]["email"], "erin@example.com")
        s, j, _ = self.r("POST", "/api/cli/login/poll", {"poll": st["poll"]})
        self.assertEqual((s, j["error"]), (410, "expired"))

        # stored as sha256 only: the token and the poll secret are nowhere in the database files
        t = self.hub.q("SELECT * FROM tokens WHERE hash = ?", (sha(tok),))
        self.assertEqual([(r["name"], r["user_id"]) for r in t], [("Erin's laptop", p["user"]["id"])])
        row = self.hub.q("SELECT * FROM cli_logins WHERE code = ?", (st["code"],))[0]
        self.assertEqual((row["state"], row["token_plain"]), ("taken", None))
        blob = self.hub.db_bytes()
        self.assertNotIn(tok.encode(), blob)
        self.assertNotIn(st["poll"].encode(), blob)

        s, j, _ = self.r("GET", "/_t/cli", token=tok)
        self.assertEqual((s, j["user"]["email"], j["token_id"]), (200, "erin@example.com", t[0]["id"]))
        self.assertEqual(self.r("GET", "/_t/cli-contributor", token=tok)[1]["error"], "forbidden")
        # a bearer token never opens a browser level
        for lvl in ("viewer", "contributor", "admin"):
            self.assertEqual(self.r("GET", f"/_t/{lvl}", token=tok)[0], 401, lvl)
        s, j, _ = self.r("GET", "/api/tokens", **erin)
        self.assertEqual([(d["id"], d["name"]) for d in j["tokens"]], [(t[0]["id"], "Erin's laptop")])
        self.assertEqual(set(j["tokens"][0]), {"id", "name", "created_at", "last_used_at"})

    def test_an_approved_code_gives_its_token_exactly_once(self):
        s, st, _ = self.r("POST", "/api/cli/login/start", {"device": "race"})
        self.r("POST", "/api/cli/login/approve", {"code": st["code"], "approve": True}, user="gus@example.com")
        out, go = [], threading.Barrier(8)

        def poll():
            go.wait()
            out.append(self.r("POST", "/api/cli/login/poll", {"poll": st["poll"]})[0])
        ts = [threading.Thread(target=poll) for _ in range(8)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertEqual(sorted(out), [200] + [410] * 7)
        self.assertEqual(len(self.hub.q("SELECT id FROM tokens WHERE name = 'race'")), 1)

    def test_codes_expire(self):
        past = auth._iso(time.time() - 1)
        s, st, _ = self.r("POST", "/api/cli/login/start", {"device": "late"})
        self.hub.q("UPDATE cli_logins SET expires_at = ? WHERE code = ?", (past, st["code"]))
        self.assertEqual(self.r("POST", "/api/cli/login/poll", {"poll": st["poll"]})[1]["error"], "expired")
        self.assertEqual(self.r("GET", f"/api/cli/login/info?code={st['code']}", user="hal@example.com")[1]["state"], "expired")
        s, j, _ = self.r("POST", "/api/cli/login/approve", {"code": st["code"], "approve": True}, user="hal@example.com")
        self.assertEqual((s, j["error"]), (410, "expired"))
        # approved, then not collected in time: no token
        s, st, _ = self.r("POST", "/api/cli/login/start", {"device": "late2"})
        self.r("POST", "/api/cli/login/approve", {"code": st["code"], "approve": True}, user="hal@example.com")
        self.hub.q("UPDATE cli_logins SET expires_at = ? WHERE code = ?", (past, st["code"]))
        self.assertEqual(self.r("POST", "/api/cli/login/poll", {"poll": st["poll"]})[0], 410)
        self.assertEqual(self.hub.q("SELECT id FROM tokens WHERE name = 'late2'"), [])
        # approved a few seconds before the end: still a minute to collect
        s, st, _ = self.r("POST", "/api/cli/login/start", {"device": "late3"})
        self.hub.q("UPDATE cli_logins SET expires_at = ? WHERE code = ?", (auth._iso(time.time() + 3), st["code"]))
        self.r("POST", "/api/cli/login/approve", {"code": st["code"], "approve": True}, user="hal@example.com")
        exp = self.hub.q("SELECT expires_at FROM cli_logins WHERE code = ?", (st["code"],))[0][0]
        self.assertGreaterEqual(auth._unix(exp), time.time() + 55)

    def test_deny(self):
        s, st, _ = self.r("POST", "/api/cli/login/start", {"device": "nope"})
        s, j, _ = self.r("POST", "/api/cli/login/approve", {"code": st["code"], "approve": False}, user="ida@example.com")
        self.assertEqual((s, j["state"]), (200, "denied"))
        self.assertEqual(self.r("POST", "/api/cli/login/poll", {"poll": st["poll"]})[:1], (403,))
        self.assertEqual(self.r("POST", "/api/cli/login/poll", {"poll": st["poll"]})[1]["error"], "denied")
        s, j, _ = self.r("POST", "/api/cli/login/approve", {"code": st["code"], "approve": True}, user="ida@example.com")
        self.assertEqual((s, j["error"]), (409, "already_decided"))

    def test_bad_login_input(self):
        u = {"user": "ida@example.com"}
        self.assertEqual(self.r("POST", "/api/cli/login/approve", {"code": "AAAA-AAAA", "approve": True}, **u)[0], 404)
        self.assertEqual(self.r("POST", "/api/cli/login/approve", {"code": "AAAA-AAAA", "approve": "yes"}, **u)[0], 400)
        self.assertEqual(self.r("POST", "/api/cli/login/approve", {"code": "short", "approve": True}, **u)[0], 400)
        self.assertEqual(self.r("GET", "/api/cli/login/info?code=AAAA-AAAA", **u)[0], 404)
        self.assertEqual(self.r("GET", "/api/cli/login/info?code=AAAA-AAAA")[0], 401)
        self.assertEqual(self.r("POST", "/api/cli/login/poll", {})[0], 400)
        self.assertEqual(self.r("POST", "/api/cli/login/poll", {"poll": "not-a-real-one"})[0], 410)

    def test_start_is_capped(self):
        with mock.patch.object(auth, "MAX_PENDING_LOGINS", 0):
            s, j, _ = self.r("POST", "/api/cli/login/start", {"device": "flood"})
        self.assertEqual((s, j["error"]), (429, "busy"))

    def test_devices_listed_and_revoked_own_only(self):
        a = self.hub.device_login({"user": "jon@example.com"}, "jon-1")
        b = self.hub.device_login({"user": "kay@example.com"}, "kay-1")
        bid = self.r("GET", "/_t/cli", token=b)[1]["token_id"]
        aid = self.r("GET", "/_t/cli", token=a)[1]["token_id"]
        self.assertEqual(self.r("DELETE", f"/api/tokens/{bid}", user="jon@example.com")[0], 404)
        self.assertEqual(self.r("GET", "/_t/cli", token=b)[0], 200)
        self.assertEqual(self.r("DELETE", f"/api/tokens/{aid}", user="jon@example.com", csrf=False)[0], 403)
        self.assertEqual(self.r("DELETE", f"/api/tokens/{aid}", user="jon@example.com")[0], 200)
        s, j, _ = self.r("GET", "/_t/cli", token=a)
        self.assertEqual((s, j["error"]), (401, "bad_token"))
        self.assertEqual(self.r("GET", "/api/tokens", user="jon@example.com")[1]["tokens"], [])
        self.assertEqual(self.r("DELETE", f"/api/tokens/{aid}", user="jon@example.com")[0], 404)

    def test_logout_revokes_the_callers_token(self):
        t = self.hub.device_login({"user": "lou@example.com"}, "lou-1")
        self.assertEqual(self.r("POST", "/api/cli/logout", {}, token=t, csrf=False)[0], 200)
        self.assertEqual(self.r("GET", "/_t/cli", token=t)[0], 401)

    def test_a_disabled_users_tokens_stop_working(self):
        e = "max@example.com"
        t = self.hub.device_login({"user": e}, "max-1")
        uid = self.uid(e)
        s, st, _ = self.r("POST", "/api/cli/login/start", {"device": "max-2"})
        self.r("POST", "/api/cli/login/approve", {"code": st["code"], "approve": True}, user=e)
        self.assertEqual(self.r("PUT", f"/api/admin/users/{uid}", {"disabled": True}, user=BOSS)[0], 200)
        for lvl in ("cli", "cli-contributor"):
            s, j, _ = self.r("GET", f"/_t/{lvl}", token=t)
            self.assertEqual((s, j["error"]), (403, "disabled"), lvl)
        self.assertEqual(self.r("GET", "/_t/viewer", user=e)[1]["error"], "disabled")
        self.assertEqual(self.r("POST", "/api/cli/login/poll", {"poll": st["poll"]})[1]["error"], "denied")
        self.assertEqual(self.r("PUT", f"/api/admin/users/{uid}", {"disabled": False}, user=BOSS)[0], 200)
        self.assertEqual(self.r("GET", "/_t/cli", token=t)[0], 200)

    def test_last_used_at_written_at_most_once_a_minute(self):
        t = self.hub.device_login({"user": "ned@example.com"}, "ned-1")

        def last():
            return self.hub.q("SELECT last_used_at FROM tokens WHERE hash = ?", (sha(t),))[0][0]
        self.assertIsNone(last())
        self.r("GET", "/_t/cli", token=t)
        self.assertGreaterEqual(auth._unix(last()), time.time() - 5)
        recent = auth._iso(time.time() - 30)
        self.hub.q("UPDATE tokens SET last_used_at = ? WHERE hash = ?", (recent, sha(t)))
        self.r("GET", "/_t/cli", token=t)
        self.assertEqual(last(), recent)
        self.hub.q("UPDATE tokens SET last_used_at = ? WHERE hash = ?", (auth._iso(time.time() - 120), sha(t)))
        self.r("GET", "/_t/cli", token=t)
        self.assertGreaterEqual(auth._unix(last()), time.time() - 5)

    def test_worker_token(self):
        s, j, _ = self.r("POST", "/_t/worker", {}, token=WORKER, csrf=False)
        self.assertEqual((s, j["user"]["role"]), (200, "worker"))
        self.assertEqual(self.r("GET", "/_t/worker", token=WORKER + "x")[0], 401)
        self.assertEqual(self.r("GET", "/_t/worker")[0], 401)
        self.assertEqual(self.r("GET", "/_t/worker", user=BOSS)[0], 401)
        user_tok = self.hub.device_login({"user": "ola@example.com"}, "ola-1")
        self.assertEqual(self.r("GET", "/_t/worker", token=user_tok)[0], 401)
        self.assertEqual(self.r("GET", "/_t/cli", token=WORKER)[0], 401)
        self.assertEqual(self.r("GET", "/_t/viewer", token=WORKER)[0], 401)
        with mock.patch.object(self.hub.cfg, "worker_token_sha256", ""):
            self.assertEqual(self.r("GET", "/_t/worker", token=WORKER)[0], 401)
            self.assertEqual(self.r("GET", "/_t/worker", token="")[0], 401)

    def test_test_header_works_on_cli_levels_but_a_bad_bearer_never_falls_back(self):
        self.assertEqual(self.r("GET", "/_t/cli", user="pam@example.com")[0], 200)
        self.assertEqual(self.r("GET", "/_t/cli", user="pam@example.com", token="pcg_" + "a" * 32)[0], 401)
        self.assertEqual(self.r("GET", "/_t/cli", user="pam@example.com", token="garbage")[0], 401)

    def test_me_put_name(self):
        e = "quin@example.com"
        s, j, _ = self.r("PUT", "/api/me", {"name": "  Quin \t  Q​R  "}, user=e)
        self.assertEqual((s, j["name"]), (200, "Quin Q R"))
        for bad in ("", "   ", "x" * 61, 5, None):
            self.assertEqual(self.r("PUT", "/api/me", {"name": bad}, user=e)[0], 400, repr(bad))
        self.assertEqual(self.r("GET", "/api/me", user=e)[1]["name"], "Quin Q R")

    def test_admin_users(self):
        e = "rae@example.com"
        uid = self.uid(e)
        self.assertEqual(self.r("GET", "/api/admin/users", user=e)[0], 403)
        s, j, _ = self.r("GET", "/api/admin/users", user=BOSS)
        self.assertEqual(s, 200)
        me = [u for u in j["users"] if u["email"] == e][0]
        self.assertEqual((me["role"], me["devices"], me["disabled"]), ("viewer", 0, False))
        for body in ({"role": "owner"}, {"disabled": "yes"}, {}, {"name": ""}):
            self.assertEqual(self.r("PUT", f"/api/admin/users/{uid}", body, user=BOSS)[0], 400, body)
        self.assertEqual(self.r("PUT", "/api/admin/users/99999", {"role": "viewer"}, user=BOSS)[0], 404)
        self.assertEqual(self.r("PUT", f"/api/admin/users/{uid}", {"name": "Rae R"}, user=BOSS)[1]["name"], "Rae R")
        self.assertEqual(self.r("PUT", f"/api/admin/users/{uid}", {"role": "admin"}, user=e)[0], 403)

    def test_the_last_admin_stays(self):
        boss, sid = self.uid(BOSS), "sid@example.com"
        others = self.hub.q("SELECT id FROM users WHERE role = 'admin' AND id != ?", (boss,))
        self.assertEqual(others, [])
        for body in ({"role": "viewer"}, {"disabled": True}):
            s, j, _ = self.r("PUT", f"/api/admin/users/{boss}", body, user=BOSS)
            self.assertEqual((s, j["error"]), (409, "last_admin"), body)
        self.r("PUT", f"/api/admin/users/{self.uid(sid)}", {"role": "admin"}, user=BOSS)
        self.assertEqual(self.r("PUT", f"/api/admin/users/{boss}", {"role": "contributor"}, user=BOSS)[0], 200)
        self.assertEqual(self.r("PUT", f"/api/admin/users/{self.uid(sid)}", {"role": "viewer"}, user=sid)[1]["error"], "last_admin")
        self.assertEqual(self.r("PUT", f"/api/admin/users/{boss}", {"role": "admin"}, user=sid)[0], 200)
        self.assertEqual(self.r("PUT", f"/api/admin/users/{self.uid(sid)}", {"role": "viewer"}, user=BOSS)[0], 200)

    def test_links_only_under_local_auth(self):
        self.assertEqual(self.r("POST", "/api/admin/invites", {"role": "viewer"}, user=BOSS)[1]["error"], "not_local")
        self.assertEqual(self.r("POST", "/api/join", {"token": "x" * 43, "name": "Z"})[1]["error"], "not_local")

    def test_worker_and_token_checks_are_constant_time(self):
        t = self.hub.device_login({"user": "uma@example.com"}, "uma-1")
        with compare_digest_spy() as spy:
            self.assertEqual(self.r("GET", "/_t/worker", token=WORKER)[0], 200)
            self.assertTrue(spy.called)
        with compare_digest_spy() as spy:
            self.assertEqual(self.r("GET", "/_t/cli", token=t)[0], 200)
            self.assertTrue(spy.called)

    def test_pages(self):
        s, body, r = self.r("GET", "/cli?code=ABCD-EFGH")
        self.assertEqual((s, r.getheader("Content-Security-Policy"), r.getheader("X-Frame-Options")), (200, app.PAGE_CSP, "DENY"))
        self.assertIn(b'src="/cli.js"', body)
        s, body, r = self.r("GET", "/join/" + "a" * 43)
        self.assertEqual((s, r.getheader("Content-Security-Policy")), (200, app.PAGE_CSP))
        self.assertIn(b'src="/join.js"', body)
        for name, ctype in (("cli.js", "text/javascript"), ("join.js", "text/javascript"), ("auth.css", "text/css")):
            s, _, r = self.r("GET", "/" + name)
            self.assertEqual((s, r.getheader("Content-Type").split(";")[0]), (200, ctype), name)

    def test_pages_have_nothing_inline(self):
        # the page CSP forbids inline script and style: nothing may rely on them
        for name in ("cli.html", "join.html"):
            html = (self.hub.cfg.static / name).read_text()
            self.assertNotRegex(html, r"<script(?![^>]*\bsrc=)[^>]*>", name)
            self.assertNotRegex(html, r"(?i)<style|\sstyle=|\son[a-z]+=|javascript:", name)
        for name in ("cli.js", "join.js"):
            js = (self.hub.cfg.static / name).read_text()
            self.assertNotRegex(js, r"innerHTML|outerHTML|insertAdjacentHTML|document\.write|\.style\.|setAttribute\(\s*['\"]style", name)


# ============================================================ local auth: invites, sign-in links, the pcg_s cookie

class LocalModeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.hub = Hub("local").start()
        # the documented command, run as Leo would: prints a one-time sign-in link
        p = subprocess.run([sys.executable, "-m", "hub.auth", "bootstrap", "Leo@Example.com"], cwd=str(ROOT),
                           env={**os.environ, **cls.hub.env}, capture_output=True, text=True, timeout=60)
        cls.boot_out = p.stdout + p.stderr
        m = re.search(r"https://hub\.example/join/([A-Za-z0-9_-]+)", p.stdout)
        assert p.returncode == 0 and m, cls.boot_out
        cls.boot_token = m[1]
        s, j, r = cls.hub.req("POST", "/api/join", {"token": cls.boot_token})
        assert s == 200, j
        cls.boot_set_cookie = r.getheader("Set-Cookie")
        cls.admin = {"cookie": cls.boot_set_cookie.split(";")[0]}

    @classmethod
    def tearDownClass(cls):
        cls.hub.close()

    def setUp(self):
        self.r = self.hub.req

    def invite(self, role="viewer", **extra):
        s, j, _ = self.r("POST", "/api/admin/invites", {"role": role, **extra}, **self.admin)
        self.assertEqual(s, 201, j)
        return j, j["url"].rsplit("/", 1)[1]

    def join(self, tok, **body):
        s, j, r = self.r("POST", "/api/join", {"token": tok, **body})
        return s, j, ({"cookie": r.getheader("Set-Cookie").split(";")[0]} if s == 200 else None)

    def test_bootstrap_link(self):
        self.assertIn("leo@example.com is an admin.", self.boot_out)
        s, me, _ = self.r("GET", "/api/me", **self.admin)
        self.assertEqual((s, me["email"], me["role"], me["name"], me["auth"]), (200, "leo@example.com", "admin", "leo", "local"))
        self.assertEqual(self.r("POST", "/api/join", {"token": self.boot_token})[1]["error"], "used_or_expired")
        row = self.hub.q("SELECT * FROM invites WHERE token_hash = ?", (sha(self.boot_token),))[0]
        self.assertEqual((row["for_user"], row["used_by"]), (me["id"], me["id"]))
        self.assertEqual(auth._unix(row["expires_at"]) - auth._unix(row["created_at"]), 86400)

    def test_bootstrap_promotes_and_reenables_someone_who_exists(self):
        _, tok = self.invite("viewer")
        s, j, _ = self.join(tok, name="Vic", email="vic@example.com")
        self.r("PUT", f"/api/admin/users/{j['user']['id']}", {"disabled": True}, **self.admin)
        url = auth.bootstrap(self.hub.cfg, "VIC@example.com")
        u = auth.user_by_email("vic@example.com")
        self.assertEqual((u["role"], u["disabled"], u["id"]), ("admin", False, j["user"]["id"]))
        s, j2, c = self.join(url.rsplit("/", 1)[1])
        self.assertEqual((s, j2["user"]["id"]), (200, u["id"]))
        self.assertEqual(self.r("GET", "/_t/admin", **c)[0], 200)
        self.r("PUT", f"/api/admin/users/{u['id']}", {"role": "viewer"}, **self.admin)

    def test_session_cookie_flags(self):
        parts = [p.strip() for p in self.boot_set_cookie.split(";")]
        self.assertTrue(parts[0].startswith("pcg_s=v1."))
        self.assertEqual(set(parts[1:]), {"Path=/", "Max-Age=2592000", "HttpOnly", "SameSite=Lax", "Secure"})
        plain = C.load({**self.hub.env, "PCG_PUBLIC_URL": "http://100.97.205.90:8480"})
        self.assertNotIn("Secure", auth.session_cookie(plain, 1))

    def test_invite_flow(self):
        link, tok = self.invite("contributor")
        self.assertEqual((link["kind"], link["role"]), ("invite", "contributor"))
        self.assertTrue(link["url"].startswith(f"{PUBLIC}/join/"))
        row = self.hub.q("SELECT * FROM invites WHERE token_hash = ?", (sha(tok),))[0]
        self.assertEqual(auth._unix(row["expires_at"]) - auth._unix(row["created_at"]), 7 * 86400)
        self.assertNotIn(tok.encode(), self.hub.db_bytes())
        s, body, r = self.r("GET", f"/join/{tok}")
        self.assertEqual((s, r.getheader("Content-Security-Policy")), (200, app.PAGE_CSP))
        s, peek, _ = self.r("POST", "/api/join", {"token": tok, "peek": True})
        self.assertEqual((s, peek["kind"], peek["role"]), (200, "invite", "contributor"))
        s, j, c = self.join(tok, name="  Mia  ")
        self.assertEqual((s, j["user"]["name"], j["user"]["role"]), (200, "Mia", "contributor"))
        self.assertRegex(j["user"]["email"], r"^mia\.[0-9a-f]{6}@local\.invalid$")
        self.assertEqual(self.r("GET", "/_t/contributor", **c)[0], 200)
        self.assertEqual(self.r("GET", "/api/me", **c)[1]["id"], j["user"]["id"])
        s, again, _ = self.join(tok, name="Mia again")
        self.assertEqual((s, again["error"]), (410, "used_or_expired"))
        self.assertEqual(self.r("POST", "/api/join", {"token": tok, "peek": True})[0], 410)

    def test_invite_email_is_unique_and_a_refusal_keeps_the_link(self):
        _, tok = self.invite()
        s, j, _ = self.join(tok, name="Ned", email=" Ned@Example.com ")
        self.assertEqual((s, j["user"]["email"]), (200, "ned@example.com"))
        _, tok2 = self.invite()
        self.assertEqual(self.join(tok2, name="Not Ned", email="ned@example.com")[1]["error"], "email_taken")
        self.assertEqual(self.join(tok2, name="Not Ned", email="not an email")[0], 400)
        self.assertEqual(self.join(tok2, name="")[0], 400)
        self.assertEqual(self.join(tok2, name="Nora", email="nora@example.com")[0], 200)

    def test_invite_expires(self):
        _, tok = self.invite()
        self.hub.q("UPDATE invites SET expires_at = ? WHERE token_hash = ?", (auth._iso(time.time() - 1), sha(tok)))
        self.assertEqual(self.r("POST", "/api/join", {"token": tok, "peek": True})[0], 410)
        self.assertEqual(self.join(tok, name="Late")[0], 410)
        self.assertEqual(self.join("x" * 43, name="Nobody")[0], 410)

    def test_a_local_email_never_makes_an_admin(self):
        _, tok = self.invite("viewer")
        s, j, c = self.join(tok, name="Boss?", email=BOSS)
        self.assertEqual((s, j["user"]["role"]), (200, "viewer"))
        self.assertEqual(self.r("GET", "/_t/admin", **c)[0], 403)

    def test_invites_need_an_admin(self):
        _, tok = self.invite("viewer")
        _, _, c = self.join(tok, name="Pat")
        self.assertEqual(self.r("POST", "/api/admin/invites", {"role": "admin"}, **c)[0], 403)
        self.assertEqual(self.r("POST", "/api/admin/invites", {"role": "king"}, **self.admin)[0], 400)
        self.assertEqual(self.r("POST", "/api/admin/invites", {"user_id": 99999}, **self.admin)[0], 404)
        self.assertEqual(self.r("POST", "/api/admin/invites", {"role": "viewer"}, **self.admin, csrf=False)[0], 403)

    def test_a_sign_in_link_for_someone_who_exists(self):
        _, tok = self.invite("contributor")
        _, j, _ = self.join(tok, name="Rex")
        link, tok2 = self.invite(user_id=j["user"]["id"])
        self.assertEqual(link["kind"], "signin")
        s, peek, _ = self.r("POST", "/api/join", {"token": tok2, "peek": True})
        self.assertEqual((peek["kind"], peek["name"]), ("signin", "Rex"))
        s, j2, c = self.join(tok2)
        self.assertEqual((s, j2["user"]["id"], j2["user"]["role"]), (200, j["user"]["id"], "contributor"))
        self.assertEqual(self.r("GET", "/api/me", **c)[1]["name"], "Rex")
        self.assertEqual(self.join(tok2)[0], 410)

    def test_join_is_csrf_protected(self):
        _, tok = self.invite()
        self.assertEqual(self.r("POST", "/api/join", {"token": tok, "name": "Sly"}, csrf=False)[1]["error"], "csrf")
        self.assertEqual(self.r("POST", "/api/join", {"token": tok, "name": "Sly"}, headers={"Origin": "https://evil.example"})[0], 403)
        self.assertEqual(self.join(tok, name="Sly")[0], 200)          # still unused after the refusals

    def test_session_cookie_checks(self):
        uid = self.r("GET", "/api/me", **self.admin)[1]["id"]
        good = auth.make_session(self.hub.cfg, uid)
        msg, sig = good.rsplit(".", 1)

        def status(v):
            return self.r("GET", "/_t/viewer", cookie=f"{auth.SESSION_COOKIE}={v}")[0]
        self.assertEqual(status(good), 200)
        flipped = sig[:-2] + ("A" if sig[-2] != "A" else "B") + sig[-1]
        self.assertEqual(status(f"{msg}.{flipped}"), 401)
        self.assertEqual(len(good.split(".")), 4)
        self.assertEqual(status(good.replace(f"v1.{uid}.", f"v1.{uid + 1}.", 1)), 401)
        other = C.load({**self.hub.env, "PCG_SECRET": "another-secret-0123456789"})
        self.assertEqual(status(auth.make_session(other, uid)), 401)
        self.assertEqual(status(auth.make_session(self.hub.cfg, uid, at=time.time() - 31 * 86400)), 401)
        self.assertEqual(status(auth.make_session(self.hub.cfg, uid, at=time.time() - 29 * 86400)), 200)
        self.assertEqual(status(auth.make_session(self.hub.cfg, uid, at=time.time() + 3600)), 401)
        self.assertEqual(status(auth.make_session(self.hub.cfg, 99999)), 401)
        for junk in ("", "x", "v1.1.2.3", good + "x"):
            self.assertEqual(status(junk), 401, junk)
        self.assertEqual(self.r("GET", "/_t/viewer")[0], 401)
        with compare_digest_spy() as spy:
            self.assertEqual(status(good), 200)
            self.assertTrue(spy.called)

    def test_device_login_approved_by_cookie_and_bearer_never_opens_browser_routes(self):
        tok = self.hub.device_login(self.admin, "leo-laptop")
        self.assertEqual(self.r("GET", "/_t/cli-contributor", token=tok)[0], 200)
        self.assertEqual(self.r("GET", "/api/me", token=tok)[0], 401)
        self.assertEqual(self.r("GET", "/api/tokens", token=tok)[0], 401)
        names = [d["name"] for d in self.r("GET", "/api/tokens", **self.admin)[1]["tokens"]]
        self.assertIn("leo-laptop", names)
        # header mode's X-Test-User means nothing here
        self.assertEqual(self.r("GET", "/_t/viewer", user="leo@example.com")[0], 401)
        self.assertEqual(self.r("GET", "/_t/cli", user="leo@example.com")[0], 401)

    def test_a_disabled_user_session_gets_403(self):
        _, tok = self.invite()
        _, j, c = self.join(tok, name="Tod")
        self.r("PUT", f"/api/admin/users/{j['user']['id']}", {"disabled": True}, **self.admin)
        s, e, _ = self.r("GET", "/api/me", **c)
        self.assertEqual((s, e["error"]), (403, "disabled"))
        link, tok2 = self.invite(user_id=j["user"]["id"])
        self.assertEqual(self.join(tok2)[0], 403)


# ============================================================ Cloudflare Access JWTs

class Key:
    """An RSA key made by openssl; signs with `openssl dgst -sha256 -sign` (PKCS#1 v1.5)."""

    def __init__(self, d: Path, kid: str):
        self.pem, self.kid = d / f"{kid}.pem", kid
        subprocess.run([OPENSSL, "genrsa", "-out", str(self.pem), "2048"], check=True, capture_output=True)
        mod = subprocess.run([OPENSSL, "rsa", "-in", str(self.pem), "-noout", "-modulus"], check=True, capture_output=True, text=True).stdout
        text = subprocess.run([OPENSSL, "rsa", "-in", str(self.pem), "-noout", "-text"], check=True, capture_output=True, text=True).stdout
        self.n = int(mod.strip().split("=", 1)[1], 16)
        self.e = int(re.search(r"publicExponent: (\d+)", text)[1])
        self.pub_pem = subprocess.run([OPENSSL, "rsa", "-in", str(self.pem), "-pubout"], check=True, capture_output=True).stdout

    def jwk(self):
        return {"kid": self.kid, "kty": "RSA", "alg": "RS256", "use": "sig",
                "n": b64e(self.n.to_bytes(256, "big")), "e": b64e(self.e.to_bytes(3, "big"))}

    def sign(self, data: bytes) -> bytes:
        return subprocess.run([OPENSSL, "dgst", "-sha256", "-sign", str(self.pem)], input=data, check=True, capture_output=True).stdout


class FakeCerts:
    """Cloudflare's certs endpoint, over https with a self-signed certificate for 127.0.0.1."""

    def __init__(self, d: Path):
        crt, key = d / "tls.crt", d / "tls.key"
        subprocess.run([OPENSSL, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key), "-out", str(crt),
                        "-days", "1", "-subj", "/CN=127.0.0.1", "-addext", "subjectAltName=IP:127.0.0.1"],
                       check=True, capture_output=True)
        self.ca, self.doc, self.hits, self.redirect_to = str(crt), {"keys": []}, 0, None
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                outer.hits += 1
                if self.path == "/short":                  # says 1000 bytes, sends 2
                    self.send_response(200)
                    self.send_header("Content-Length", "1000")
                    self.end_headers()
                    self.wfile.write(b"{}")
                    self.close_connection = True
                    return
                if self.path == "/redirect":
                    self.send_response(302)
                    self.send_header("Location", outer.redirect_to)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                body = json.dumps(outer.doc).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(str(crt), str(key))
        self.srv.socket = ctx.wrap_socket(self.srv.socket, server_side=True)
        self.base = f"https://127.0.0.1:{self.srv.server_address[1]}"
        self.url = self.base + "/cdn-cgi/access/certs"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


class PlainHTTP:
    """An http:// server that counts hits: the hub must never ask it for keys."""

    def __init__(self, certs: FakeCerts):
        self.hits = 0
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                outer.hits += 1
                body = json.dumps(certs.doc).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}/cdn-cgi/access/certs"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


TEAM, AUD = "pcgtest", "aud-0123456789abcdef"


@unittest.skipUnless(OPENSSL, "openssl is not installed")
class CfAccessTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="pcg-jwt-"))
        cls.a, cls.b = Key(cls.tmp, "key-a"), Key(cls.tmp, "key-b")
        cls.certs = FakeCerts(cls.tmp)
        cls.plain = PlainHTTP(cls.certs)
        cls.hub = Hub("cf-access", PCG_CF_TEAM=TEAM, PCG_CF_AUD=AUD)
        cls.hub.cfg.cf_certs_url = cls.certs.url
        cls.hub.cfg.cf_ca_file = cls.certs.ca
        cls.hub.start()

    @classmethod
    def tearDownClass(cls):
        cls.hub.close()
        cls.certs.close()
        cls.plain.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)
        auth._jwks.clear()

    def setUp(self):
        auth._jwks.clear()
        self.certs.doc = {"keys": [self.a.jwk()]}
        self.certs.hits = 0
        self.plain.hits = 0
        self.hub.cfg.cf_certs_url = self.certs.url
        self.hub.cfg.cf_ca_file = self.certs.ca

    def claims(self, **over):
        now = int(time.time())
        c = {"iss": f"https://{TEAM}.cloudflareaccess.com", "aud": [AUD], "email": "Alice@Example.com",
             "exp": now + 600, "nbf": now - 5, "iat": now - 5, "sub": "0000-1111", "type": "app"}
        c.update(over)
        return {k: v for k, v in c.items() if v is not None}

    def jwt(self, claims=None, key=None, kid=None, alg="RS256", header=None):
        key = key or self.a
        h = {"alg": alg, "kid": kid or key.kid, "typ": "JWT", **(header or {})}
        si = b64e(json.dumps({k: v for k, v in h.items() if v is not None}).encode()) + "." + b64e(json.dumps(claims or self.claims()).encode())
        return si + "." + b64e(key.sign(si.encode()))

    def me(self, jwt, **kw):
        return self.hub.req("GET", "/api/me", headers={"Cf-Access-Jwt-Assertion": jwt}, **kw)

    def test_a_valid_token_makes_a_viewer(self):
        s, j, _ = self.me(self.jwt())
        self.assertEqual((s, j["email"], j["name"], j["role"], j["auth"]), (200, "alice@example.com", "alice", "viewer", "cf-access"))
        self.assertEqual(self.me(self.jwt(self.claims(email=BOSS)))[1]["role"], "admin")
        self.assertEqual(self.me(self.jwt(self.claims(aud=AUD)))[0], 200)                       # aud as one string
        self.assertEqual(self.me(self.jwt(self.claims(aud=["other", AUD])))[0], 200)
        self.assertEqual(self.me(self.jwt(self.claims(exp=int(time.time()) - 5, nbf=None)))[0], 200)   # inside the leeway

    def test_the_cookie_works_where_access_is_bypassed(self):
        # /api/cli/ is excluded from Access in production: there only the CF_Authorization cookie comes along
        jar = {"cookie": "CF_Authorization=" + self.jwt(self.claims(email="bea@example.com"))}
        s, j, _ = self.hub.req("GET", "/api/me", **jar)
        self.assertEqual((s, j["email"]), (200, "bea@example.com"))
        tok = self.hub.device_login(jar, "bea-laptop")
        s, j, _ = self.hub.req("GET", "/_t/cli", token=tok)
        self.assertEqual((s, j["user"]["email"]), (200, "bea@example.com"))
        self.assertEqual(self.hub.req("GET", "/api/me", token=tok)[0], 401)          # never a bearer for the browser

    def test_claims_are_checked(self):
        now = int(time.time())
        for why, c in (("aud", self.claims(aud=["someone-else"])), ("aud", self.claims(aud=None)),
                       ("aud", self.claims(aud={"x": AUD})),
                       ("iss", self.claims(iss="https://other.cloudflareaccess.com")),
                       ("iss", self.claims(iss=f"http://{TEAM}.cloudflareaccess.com")), ("iss", self.claims(iss=None)),
                       ("expired", self.claims(exp=now - 3600)), ("expired", self.claims(exp=None)),
                       ("expired", self.claims(exp=str(now + 600))), ("expired", self.claims(exp=True)),
                       ("not valid yet", self.claims(nbf=now + 3600)), ("not valid yet", self.claims(nbf="0"))):
            s, j, _ = self.me(self.jwt(c))
            self.assertEqual((s, j["error"]), (401, "bad_access_token"), c)
            self.assertTrue(j["message"].endswith(": " + why), (j["message"], c))
        s, j, _ = self.me(self.jwt(self.claims(email=None)))
        self.assertEqual((s, j["error"]), (403, "no_email"))

    def test_alg_and_signature_are_checked(self):
        si = b64e(json.dumps({"alg": "none", "kid": self.a.kid}).encode()) + "." + b64e(json.dumps(self.claims()).encode())
        cases = {"alg none": (si + ".", "alg must be RS256"), "alg none, sig": (si + "." + b64e(b"x" * 256), "alg must be RS256")}
        # the classic confusion: HS256 keyed with the RSA public key
        hs = b64e(json.dumps({"alg": "HS256", "kid": self.a.kid}).encode()) + "." + b64e(json.dumps(self.claims()).encode())
        cases["HS256 with the public key"] = (hs + "." + b64e(hmac.new(self.a.pub_pem, hs.encode(), hashlib.sha256).digest()), "alg must be RS256")
        cases["RS512"] = (self.jwt(alg="RS512"), "alg must be RS256")
        cases["no kid"] = (self.jwt(header={"kid": None}), "no kid")
        cases["crit"] = (self.jwt(header={"crit": ["exp"]}), "unknown critical header")
        cases["signed by another key"] = (self.jwt(key=self.b, kid=self.a.kid), "signature")
        good = self.jwt()
        h, p, s = good.split(".")
        cases["payload swapped"] = (h + "." + b64e(json.dumps(self.claims(email=BOSS)).encode()) + "." + s, "signature")
        cases["signature cut"] = (f"{h}.{p}.{s[:-4]}", "signature")
        cases["garbage"] = ("not.a.jwt", "malformed")
        cases["two parts"] = (f"{h}.{p}", "malformed")
        for name, (t, why) in cases.items():
            s_, j, _ = self.me(t)
            self.assertEqual((s_, j["error"]), (401, "bad_access_token"), name)
            self.assertTrue(j["message"].endswith(": " + why), (j["message"], name))
        self.assertEqual(self.me(good)[0], 200)
        self.assertEqual(self.hub.req("GET", "/api/me")[0], 401)

    def test_keys_cached_and_refetched_once_on_an_unknown_kid(self):
        self.assertEqual(self.me(self.jwt())[0], 200)
        self.assertEqual(self.certs.hits, 1)
        self.assertEqual(self.me(self.jwt())[0], 200)
        self.assertEqual(self.certs.hits, 1)                                   # cached
        self.certs.doc = {"keys": [self.a.jwk(), self.b.jwk()]}                # Cloudflare rotates
        with mock.patch.object(auth, "JWKS_MIN_REFRESH", 0):
            self.assertEqual(self.me(self.jwt(key=self.b))[0], 200)
            self.assertEqual(self.certs.hits, 2)
            self.assertEqual(self.me(self.jwt(kid="key-unknown"))[0], 401)
            self.assertEqual(self.certs.hits, 3)                               # one refetch, not a loop
        self.assertEqual(self.me(self.jwt(kid="key-unknown-2"))[0], 401)
        self.assertEqual(self.certs.hits, 3)                                   # and at most once a minute
        ent = auth._jwks[self.certs.url]
        ent["at"] -= auth.JWKS_TTL + 1
        ent["tried"] -= auth.JWKS_TTL + 1
        self.assertEqual(self.me(self.jwt())[0], 200)
        self.assertEqual(self.certs.hits, 4)                                   # an hour on: fetched again

    def test_stale_keys_serve_while_cloudflare_is_unreachable(self):
        self.assertEqual(self.me(self.jwt())[0], 200)
        ent = auth._jwks[self.certs.url]
        ent["at"] -= auth.JWKS_TTL + 1
        ent["tried"] -= auth.JWKS_TTL + 1
        self.hub.cfg.cf_ca_file = None                                         # now the fetch fails (untrusted TLS)
        self.assertEqual(self.me(self.jwt())[0], 200)

    def test_a_broken_answer_is_a_503_not_a_crash(self):
        self.hub.cfg.cf_certs_url = self.certs.base + "/short"
        s, j, _ = self.me(self.jwt())
        self.assertEqual((s, j["error"]), (503, "access_certs"))
        self.certs.doc = {"keys": [{"kid": self.a.kid, "kty": "RSA", "n": b64e(b"\x01" * 64), "e": "AQAB"}]}   # 512-bit key: ignored
        self.hub.cfg.cf_certs_url = self.certs.url
        auth._jwks.clear()
        self.assertEqual(self.me(self.jwt())[0], 503)

    def test_keys_only_over_https(self):
        self.hub.cfg.cf_certs_url = self.plain.url
        s, j, _ = self.me(self.jwt())
        self.assertEqual((s, j["error"]), (503, "access_certs"))
        self.assertEqual(self.plain.hits, 0)

    def test_redirects_are_not_followed(self):
        self.certs.redirect_to = self.plain.url
        self.hub.cfg.cf_certs_url = self.certs.base + "/redirect"
        self.assertEqual(self.me(self.jwt())[0], 503)
        self.assertEqual((self.certs.hits, self.plain.hits), (1, 0))

    def test_tls_is_verified(self):
        self.hub.cfg.cf_ca_file = None
        self.assertEqual(self.me(self.jwt())[0], 503)

    def test_not_configured_fails_closed(self):
        with mock.patch.object(self.hub.cfg, "cf_aud", ""):
            self.assertEqual(self.me(self.jwt())[1]["error"], "not_configured")
        self.assertEqual(auth.certs_url(C.load({**self.hub.env, "PCG_CF_TEAM": "lab.cloudflareaccess.com"})),
                         "https://lab.cloudflareaccess.com/cdn-cgi/access/certs")
        self.assertEqual(auth.cf_team(C.load({**self.hub.env, "PCG_CF_TEAM": "evil.com/x?"})), "")

    def test_rsa_verify_known_answers(self):
        sig = self.a.sign(b"hello")
        v = auth.rsa_pkcs1_sha256_verify
        self.assertTrue(v(self.a.n, self.a.e, b"hello", sig))
        self.assertFalse(v(self.a.n, self.a.e, b"hello!", sig))
        self.assertFalse(v(self.b.n, self.b.e, b"hello", sig))
        self.assertFalse(v(self.a.n, self.a.e, b"hello", sig[1:]))
        self.assertFalse(v(self.a.n, self.a.e, b"hello", self.a.n.to_bytes(256, "big")))
        with compare_digest_spy() as spy:
            self.assertEqual(self.me(self.jwt())[0], 200)
            self.assertTrue(spy.called)


if __name__ == "__main__":
    unittest.main()
