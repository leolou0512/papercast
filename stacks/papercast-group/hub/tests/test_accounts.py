"""PCG_AUTH=password (hub/accounts.py, with hub/auth.py) against a real hub: app.serve on a free
port with a temporary PCG_DATA, a fake SMTP server (fake_smtp.py: STARTTLS, AUTH PLAIN) for the
reset emails, and the pages in a headless Chrome (web_cdp.py) when there is one.

    cd stacks/papercast-group/hub && python3 -m unittest tests.test_accounts -v

The ledger of allowed Imperial emails; the first password (the username) that must be replaced
before anything else; sign-in by username or email; the rate limits; reset links by email and by
an admin; sessions that end together; removal; the command line; the pages."""
from __future__ import annotations

import hashlib
import http.client
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]                              # stacks/papercast-group
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))
from hub import accounts, app, auth, db             # noqa: E402
from hub import config as C                         # noqa: E402
from fake_smtp import OPENSSL, FakeSMTP             # noqa: E402

PUBLIC = "https://papercast.example"
SECRET = "test-secret-0123456789abcdef"
LEO = "yl6719@ic.ac.uk"
BOT, BOT_PW = "papercast.bot@example.org", "app-password-0123"
NEW_PW = "a long enough passphrase"


def sha(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


class Hub:
    def __init__(self, **env):
        self.dir = tempfile.mkdtemp(prefix="pcg-pw-")
        self.env = {"PCG_DATA": self.dir, "PCG_PORT": "0", "PCG_AUTH": "password", "PCG_SECRET": SECRET,
                    "PCG_PUBLIC_URL": PUBLIC, "PCG_ADMIN_EMAILS": "", **env}
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

    def req(self, method, path, body=None, *, cookie=None, token=None, headers=None, csrf=True, source=None):
        h = {}
        if cookie:
            h["Cookie"] = cookie
        if token:
            h["Authorization"] = "Bearer " + token
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

    def dump(self) -> str:
        """Everything in the database, as SQL. (Not the files' bytes: opening and closing hub.db in
        this process would drop SQLite's POSIX locks, and a command-line run could then corrupt it.)"""
        return "\n".join(db.conn().iterdump())

    def run(self, *args, env=None, stdin=None):
        """python3 -m hub.auth ..., as Leo would run it on the server."""
        return subprocess.run([sys.executable, "-m", "hub.auth", *args], cwd=str(ROOT), input=stdin,
                              env={**os.environ, **self.env, **(env or {})}, capture_output=True, text=True, timeout=60)


def cookie_of(r) -> str | None:
    v = r.getheader("Set-Cookie")
    return v.split(";")[0] if v else None


@unittest.skipUnless(OPENSSL, "openssl is not installed (the fake SMTP server's certificate)")
class PasswordTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="pcg-pw-smtp-"))
        cls.smtp = FakeSMTP(cls.tmp, user=BOT, password=BOT_PW).start()
        pwf = cls.tmp / "smtp.password"
        pwf.write_text(BOT_PW + "\n")
        os.chmod(pwf, 0o600)
        cls.hub = Hub(PCG_SMTP_HOST="127.0.0.1", PCG_SMTP_PORT=str(cls.smtp.port), PCG_SMTP_USER=BOT,
                      PCG_SMTP_PASSWORD_FILE=str(pwf), PCG_SMTP_FROM=f"papercast <{BOT}>")
        cls.hub.cfg.smtp_ca_file = cls.smtp.ca          # test hook: trust the fake server's certificate
        cls.hub.start()
        # the first admin, as documented: on the server's command line
        p = cls.hub.run("bootstrap", "YL6719@ic.ac.uk")
        cls.boot_out = p.stdout + p.stderr
        assert p.returncode == 0, cls.boot_out
        cls.boot_link = re.search(r"(https://papercast\.example/set-password#t=[A-Za-z0-9_-]+)", p.stdout)[1]
        accounts.reset_limits()
        s, j, r = cls.hub.req("POST", "/api/auth/login", {"login": "yl6719", "password": "yl6719"})
        assert s == 200 and j["must_change"], j
        s, j, r = cls.hub.req("POST", "/api/auth/password", {"password": "Leo's own long password"}, cookie=cookie_of(r))
        assert s == 200, j
        cls.leo = {"cookie": cookie_of(r)}
        cls.leo_id = j["user"]["id"]

    @classmethod
    def tearDownClass(cls):
        cls.hub.close()
        cls.smtp.stop()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        self.assertTrue(accounts.mail_idle())           # no email of an earlier test still on its way
        accounts.reset_limits()
        self.r = self.hub.req
        self.smtp.fail = None
        self.hub.cfg.smtp_host = "127.0.0.1"

    # ---------------------------------------------------------------- helpers
    def add(self, code, as_=None, **extra):
        """Added without the welcome email (its own tests below), so the email counts stay exact."""
        s, j, _ = self.r("POST", "/api/admin/allowed", {"email": code, "welcome": False, **extra}, **(as_ or self.leo))
        self.assertIn(s, (200, 201), j)
        return j

    def login(self, ident, pw, **kw):
        s, j, r = self.r("POST", "/api/auth/login", {"login": ident, "password": pw}, **kw)
        return s, j, ({"cookie": cookie_of(r)} if s == 200 else None)

    def person(self, code, pw=NEW_PW, role=None):
        """Added, signed in with the first password, and on a password of their own."""
        got = self.add(code)
        if role:
            self.r("PUT", f"/api/admin/users/{got['user_id']}", {"role": role}, **self.leo)
        s, j, c = self.login(code, code)
        self.assertEqual((s, j["must_change"]), (200, True), j)
        s, j, r = self.r("POST", "/api/auth/password", {"password": pw}, **c)
        self.assertEqual(s, 200, j)
        return got["user_id"], {"cookie": cookie_of(r)}

    def me(self, who):
        return self.r("GET", "/api/me", **who)

    def events(self, kind=None):
        s, j, _ = self.r("GET", "/api/admin/auth-log?limit=500", **self.leo)
        self.assertEqual(s, 200, j)
        return [e for e in j["events"] if kind is None or e["kind"] == kind]

    def device(self, who, name="laptop"):
        s, st, _ = self.r("POST", "/api/cli/login/start", {"device": name})
        s, j, _ = self.r("POST", "/api/cli/login/approve", {"code": st["code"], "approve": True}, **who)
        self.assertEqual(s, 200, j)
        s, p, _ = self.r("POST", "/api/cli/login/poll", {"poll": st["poll"]})
        self.assertEqual(s, 200, p)
        return p["token"]

    # ---------------------------------------------------------------- the first admin
    def test_bootstrap(self):
        self.assertIn("yl6719@ic.ac.uk is an admin, on the list (username yl6719).", self.boot_out)
        self.assertIn("with the first password yl6719", self.boot_out)
        u = accounts.account(self.leo_id)
        self.assertEqual((u["email"], u["username"], u["role"], u["listed"]), (LEO, "yl6719", "admin", 1))
        # the printed link works once, and only its sha256 is kept
        tok = self.boot_link.split("#t=")[1]
        row = self.hub.q("SELECT * FROM pw_links WHERE token_hash = ?", (sha(tok),))
        self.assertEqual((len(row), row[0]["kind"]), (1, "bootstrap"))
        self.assertEqual(auth._unix(row[0]["expires_at"]) - auth._unix(row[0]["created_at"]), 86400)
        # (it was voided when Leo chose his password: a new password ends every link)
        self.assertEqual(self.r("POST", "/api/auth/link", {"token": tok, "peek": True})[0], 410)
        # a second run keeps his password and prints a fresh link
        p = self.hub.run("bootstrap", LEO)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertNotIn("first password", p.stdout)
        tok2 = re.search(r"set-password#t=([A-Za-z0-9_-]+)", p.stdout)[1]
        s, peek, _ = self.r("POST", "/api/auth/link", {"token": tok2, "peek": True})
        self.assertEqual((s, peek["username"], peek["email"], peek["first"]), (200, "yl6719", LEO, False))
        self.assertEqual(self.me(self.leo)[0], 200)
        # only Imperial addresses
        p = self.hub.run("bootstrap", "leo@gmail.com")
        self.assertEqual(p.returncode, 2)
        self.assertIn("Only Imperial addresses", p.stderr)

    # ---------------------------------------------------------------- the ledger
    def test_the_add_box_short_code_full_address_and_bad_input(self):
        j = self.add("ab101")
        self.assertEqual((j["email"], j["username"], j["state"], j["user"]["role"], j["user"]["must_change"]),
                         ("ab101@ic.ac.uk", "ab101", "added", "contributor", True))
        j = self.add("  CD202@IC.AC.UK ")
        self.assertEqual((j["email"], j["username"]), ("cd202@ic.ac.uk", "cd202"))
        self.assertEqual(self.add("a.b-c3")["email"], "a.b-c3@ic.ac.uk")
        self.assertEqual(self.add("ef303@imperial.ac.uk")["username"], "ef303")
        again = self.r("POST", "/api/admin/allowed", {"email": "ab101"}, **self.leo)
        self.assertEqual((again[0], again[1]["state"]), (200, "already"))
        for bad, err in (("ab101@gmail.com", "not_imperial"), ("x@ic.ac.uk.evil.com", "not_imperial"), ("yl 6719", "bad_code"),
                         ("a..b", "bad_code"), ("-ab", "bad_code"), ("ab_c", "bad_code"), ("é12", "bad_code"),
                         ("@ic.ac.uk", "bad_code"), ("", "bad_email"), ("x" * 65, "bad_code"), (None, "bad_email")):
            s, j, _ = self.r("POST", "/api/admin/allowed", {"email": bad}, **self.leo)
            self.assertEqual((s, j["error"]), (400, err), bad)
        self.assertIn("Only Imperial addresses", self.r("POST", "/api/admin/allowed", {"email": "x@gmail.com"}, **self.leo)[1]["message"])
        # one username, one person: the same short code at the other Imperial domain is refused
        s, j, _ = self.r("POST", "/api/admin/allowed", {"email": "ab101@imperial.ac.uk"}, **self.leo)
        self.assertEqual((s, j["error"]), (409, "username_taken"))
        self.assertIn("ab101@ic.ac.uk", j["message"])
        # who added it, when, and the note
        self.add("gh404", note="visiting from Oxford")
        row = self.hub.q("SELECT * FROM allowed_emails WHERE email = 'gh404@ic.ac.uk'")[0]
        self.assertEqual((row["added_by"], row["note"]), (self.leo_id, "visiting from Oxford"))
        self.assertTrue(row["added_at"].startswith(time.strftime("%Y-%m-%d", time.gmtime())))
        self.assertIn("gh404@ic.ac.uk", [e["email"] for e in self.events("allowed")])
        listed = {e["email"]: e for e in self.r("GET", "/api/admin/allowed", **self.leo)[1]["emails"]}
        self.assertEqual(listed["gh404@ic.ac.uk"]["added_by"]["id"], self.leo_id)
        self.assertTrue(listed["gh404@ic.ac.uk"]["user"]["default_password"])
        # only admins
        _, bob = self.person("bo505")
        self.assertEqual(self.r("POST", "/api/admin/allowed", {"email": "zz999"}, **bob)[0], 403)
        self.assertEqual(self.r("POST", "/api/admin/allowed", {"email": "zz999"}, **self.leo, csrf=False)[0], 403)

    def test_remove_needs_the_typed_word_and_disables_the_account(self):
        uid, who = self.person("rm101")
        token = self.device(who, "rm-laptop")
        self.assertEqual(self.r("GET", "/api/cli/me", token=token)[0], 200)
        # an episode and a link of theirs: they stay, credited to them
        pid, eid = db.new_id("p_"), db.new_id("e_")
        c = db.conn()
        c.execute("INSERT INTO papers(id, title, title_norm, created_by, created_at) VALUES (?, 'A fake paper', 'a fake paper', ?, ?)",
                  (pid, uid, db.now()))
        c.execute("INSERT INTO episodes(id, paper_id, made_by, state, created_at, updated_at) VALUES (?, ?, ?, 'ready', ?, ?)",
                  (eid, pid, uid, db.now(), db.now()))
        for confirm in (None, "", "yes", "delet", "delete it"):
            body = {"email": "rm101@ic.ac.uk"} if confirm is None else {"email": "rm101@ic.ac.uk", "confirm": confirm}
            s, j, _ = self.r("DELETE", "/api/admin/allowed", body, **self.leo)
            self.assertEqual((s, j["error"]), (400, "confirm"), confirm)
        self.assertEqual(self.me(who)[0], 200)                  # nothing happened yet
        s, j, _ = self.r("DELETE", "/api/admin/allowed", {"email": "rm101@ic.ac.uk", "confirm": "  DeLeTe "}, **self.leo)
        self.assertEqual((s, j["user_id"]), (200, uid))
        self.assertEqual(self.me(who)[0], 401)                  # the session ended
        self.assertEqual(self.r("GET", "/api/cli/me", token=token)[0], 401)     # and the device
        self.assertEqual(self.login("rm101", NEW_PW)[1]["error"], "bad_login")   # no sign-in: like an unknown account
        u = accounts.account(uid)
        self.assertEqual((u["disabled"], u["listed"], u["name"]), (1, None, "rm101"))
        self.assertEqual(self.hub.q("SELECT made_by FROM episodes WHERE id = ?", (eid,))[0][0], uid)
        self.assertIn("rm101@ic.ac.uk", [e["email"] for e in self.events("removed")])
        users = {u["id"]: u for u in self.r("GET", "/api/admin/users", **self.leo)[1]["users"]}
        self.assertEqual((users[uid]["on_list"], users[uid]["disabled"]), (False, True))
        # not on the list: enabling it by hand is refused, adding it again brings it back
        self.assertEqual(self.r("PUT", f"/api/admin/users/{uid}", {"disabled": False}, **self.leo)[1]["error"], "not_listed")
        j = self.add("rm101")
        self.assertEqual((j["state"], j["user_id"]), ("back", uid))
        self.assertEqual(self.login("rm101", NEW_PW)[0], 200)   # with the password it had
        self.assertEqual(self.r("GET", "/api/cli/me", token=token)[0], 401)     # the old device stays revoked
        # twice: not on the list any more
        self.r("DELETE", "/api/admin/allowed", {"email": "rm101@ic.ac.uk", "confirm": "delete"}, **self.leo)
        self.assertEqual(self.r("DELETE", "/api/admin/allowed", {"email": "rm101@ic.ac.uk", "confirm": "delete"}, **self.leo)[0], 404)

    def test_self_and_last_admin_guards(self):
        s, j, _ = self.r("DELETE", "/api/admin/allowed", {"email": LEO, "confirm": "delete"}, **self.leo)
        self.assertEqual((s, j["error"]), (409, "not_yourself"))
        s, j, _ = self.r("DELETE", "/api/admin/allowed", {"email": " YL6719@ic.ac.uk ", "confirm": "delete"}, **self.leo)
        self.assertEqual((s, j["error"]), (409, "not_yourself"))
        self.assertEqual(self.r("POST", f"/api/admin/users/{self.leo_id}/reset-default", **self.leo)[1]["error"], "not_yourself")
        # Leo is the only admin: he cannot step down, and the server's command line cannot remove him
        self.assertEqual(self.r("PUT", f"/api/admin/users/{self.leo_id}", {"role": "viewer"}, **self.leo)[1]["error"], "last_admin")
        p = self.hub.run("allow", "remove", LEO)
        self.assertEqual(p.returncode, 1)
        self.assertIn("last admin", p.stderr)
        self.assertEqual(self.me(self.leo)[0], 200)
        # with a second admin, one can remove the other (never themselves)
        aid, ann = self.person("ag101", role="admin")
        s, j, _ = self.r("DELETE", "/api/admin/allowed", {"email": "ag101@ic.ac.uk", "confirm": "delete"}, **ann)
        self.assertEqual(j["error"], "not_yourself")
        s, j, _ = self.r("DELETE", "/api/admin/allowed", {"email": "ag101@ic.ac.uk", "confirm": "delete"}, **self.leo)
        self.assertEqual(s, 200, j)
        self.assertEqual(self.me(ann)[0], 401)

    def test_the_command_line_ledger(self):
        f = Path(self.hub.dir) / "group.txt"
        f.write_text("# the group, one per line\nxy101@ic.ac.uk\n  XY102@IMPERIAL.AC.UK   # visiting\n\nxy103\nnot-imperial@gmail.com\n")
        p = self.hub.run("allow", "import", str(f), "--no-welcome")
        self.assertEqual(p.returncode, 1)                       # one line refused
        self.assertIn("added xy101@ic.ac.uk: username xy101, first password xy101", p.stdout)
        self.assertIn("added xy102@imperial.ac.uk", p.stdout)
        self.assertIn("added xy103@ic.ac.uk", p.stdout)
        self.assertIn("refused not-imperial@gmail.com: Only Imperial addresses", p.stderr)
        p = self.hub.run("allow", "add", "xy101@ic.ac.uk", "xy104", "--note", "summer student", "--no-welcome")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("already on the list: xy101@ic.ac.uk", p.stdout)
        self.assertEqual(self.hub.q("SELECT note, added_by FROM allowed_emails WHERE email = 'xy104@ic.ac.uk'")[0][:], ("summer student", None))
        p = self.hub.run("allow", "import", "-", "--no-welcome", stdin="xy105@ic.ac.uk\n")
        self.assertEqual(p.returncode, 0, p.stderr)
        p = self.hub.run("allow", "list")
        self.assertEqual(p.returncode, 0, p.stderr)
        line = next(ln for ln in p.stdout.splitlines() if ln.startswith("xy104@ic.ac.uk"))
        self.assertRegex(line, r"xy104\s+contributor\s+first\s+never")
        self.assertIn("summer student", line)
        # removal from the server: the account is disabled at once
        s, j, c = self.login("xy103", "xy103")
        self.assertEqual(s, 200)
        p = self.hub.run("allow", "remove", "xy103@ic.ac.uk", "nobody@ic.ac.uk")
        self.assertEqual(p.returncode, 1)
        self.assertIn("removed xy103@ic.ac.uk: account disabled", p.stdout)
        self.assertIn("nobody@ic.ac.uk is not on the list", p.stderr)
        self.assertEqual(self.r("GET", "/api/auth/state", **c)[1]["signed_in"], False)
        self.assertEqual(self.hub.run("allow").returncode, 2)
        self.assertEqual(self.hub.run("allow", "import", "a", "b").returncode, 2)

    # ---------------------------------------------------------------- the first password
    def test_the_first_password_can_only_be_replaced(self):
        uid = self.add("fp101")["user_id"]
        s, j, c = self.login("fp101", "fp101")
        self.assertEqual((s, j["must_change"], j["user"]["username"]), (200, True, "fp101"))
        for m, path in (("GET", "/api/me"), ("GET", "/api/config"), ("GET", "/api/library"), ("GET", "/api/tokens"),
                        ("PUT", "/api/prefs"), ("GET", "/api/graphs"), ("POST", "/api/auth/signout-all")):
            s, e, _ = self.r(m, path, {} if m != "GET" else None, **c)
            self.assertEqual((s, e.get("error")), (403, "must_change_password"), path)
        s, _, r = self.r("GET", "/", **c)
        self.assertEqual((s, r.getheader("Location")), (303, "/set-password"))
        st = self.r("GET", "/api/auth/state", **c)[1]
        self.assertEqual((st["signed_in"], st["must_change"]), (True, True))
        # a device cannot be approved yet
        _, start, _ = self.r("POST", "/api/cli/login/start", {"device": "fp-laptop"})
        s, e, _ = self.r("POST", "/api/cli/login/approve", {"code": start["code"], "approve": True}, **c)
        self.assertEqual((s, e["error"]), (403, "must_change_password"))
        users = {u["id"]: u for u in self.r("GET", "/api/admin/users", **self.leo)[1]["users"]}
        self.assertEqual((users[uid]["default_password"], users[uid]["last_login_at"] is not None), (True, True))
        # the username itself, or anything with it, is not a new password
        for bad in ("fp101", "fp101fp101", "fp101@ic.ac.uk", "short"):
            s, e, _ = self.r("POST", "/api/auth/password", {"password": bad}, **c)
            self.assertEqual((s, e["error"]), (400, "weak_password"), bad)
        s, j, r = self.r("POST", "/api/auth/password", {"password": NEW_PW}, **c)
        self.assertEqual((s, j["user"]["must_change"]), (200, False))
        new = {"cookie": cookie_of(r)}
        self.assertEqual(self.me(c)[0], 401)                    # that session ended with the change
        self.assertEqual(self.me(new)[0], 200)
        self.assertEqual(self.r("GET", "/", **new)[0], 200)
        s, j, _ = self.r("POST", "/api/cli/login/approve", {"code": start["code"], "approve": True}, **new)
        self.assertEqual(s, 200, j)
        s, p, _ = self.r("POST", "/api/cli/login/poll", {"poll": start["poll"]})
        self.assertEqual((s, p["user"]["username"]), (200, "fp101"))
        self.assertEqual(self.login("fp101", "fp101")[0], 401)  # the first password is gone
        self.assertFalse({u["id"]: u for u in self.r("GET", "/api/admin/users", **self.leo)[1]["users"]}[uid]["default_password"])
        self.assertIn("the first password replaced", [e["detail"] for e in self.events("password_changed") if e["user"]["id"] == uid])

    # ---------------------------------------------------------------- signing in
    def test_sign_in_by_username_or_email(self):
        uid, _ = self.person("si101")
        for ident in ("si101", "SI101", " si101@ic.ac.uk ", "SI101@IC.AC.UK", "si101@imperial.ac.uk"):
            s, j, c = self.login(ident, NEW_PW)
            self.assertEqual((s, j["user"]["id"], j["must_change"]), (200, uid, False), ident)
            self.assertEqual(self.me(c)[1]["username"], "si101")
        self.assertEqual(self.login("si101@gmail.com", NEW_PW)[0], 401)
        s, j, _ = self.login("si101", NEW_PW.upper())
        self.assertEqual((s, j["error"], j["message"]), (401, "bad_login", accounts.BAD_LOGIN))
        self.assertEqual(self.login("si101", "")[0], 400)
        self.assertIsNotNone(accounts.account(uid)["last_login_at"])
        self.assertIn(uid, [e["user"]["id"] for e in self.events("signin") if e["user"]])

    def test_an_unknown_account_gets_the_same_answer_for_the_same_work(self):
        self.person("un101")
        answers = []
        for ident in ("un101", "nobody", "nobody@ic.ac.uk", "x@gmail.com", "fp-not-there"):
            with mock.patch.object(accounts, "_scrypt", wraps=accounts._scrypt) as spy:
                s, j, r = self.r("POST", "/api/auth/login", {"login": ident, "password": "not the password"})
            answers.append((s, j, r.getheader("Set-Cookie")))
            self.assertEqual(spy.call_count, 1, ident)          # one scrypt, known or not
        self.assertEqual(len({json.dumps(a[:2]) for a in answers}), 1, answers)
        self.assertEqual({a[2] for a in answers}, {None})
        # an account still on its first password costs the same scrypt
        self.add("un102")
        with mock.patch.object(accounts, "_scrypt", wraps=accounts._scrypt) as spy:
            self.assertEqual(self.login("un102", "wrong one")[0], 401)
        self.assertEqual(spy.call_count, 1)
        # and the comparisons are constant-time
        with mock.patch.object(accounts.hmac, "compare_digest", wraps=accounts.hmac.compare_digest) as spy:
            self.assertEqual(self.login("un101", NEW_PW)[0], 200)
            self.assertTrue(spy.called)

    def test_failed_sign_ins_are_limited_per_account(self):
        self.person("rl101")
        t = [time.time()]
        with mock.patch.object(accounts, "_now", lambda: t[0]):
            for i in range(accounts.ACCOUNT_FAILS):             # from ten different addresses
                self.assertEqual(self.login("rl101", f"wrong {i}", source=f"127.0.0.{i + 10}")[0], 401)
            s, j, r = self.r("POST", "/api/auth/login", {"login": "rl101", "password": NEW_PW}, source="127.0.0.99")
            self.assertEqual((s, j["error"]), (429, "slow_down"))
            self.assertEqual((r.getheader("Retry-After"), j["retry_after"]), (str(accounts.BACKOFF_S), accounts.BACKOFF_S))
            self.assertIn("Wait 30 seconds", j["message"])
            self.assertEqual(self.login("rl101@ic.ac.uk", NEW_PW)[0], 429)     # the email is the same account
            t[0] += accounts.BACKOFF_S + 1
            self.assertEqual(self.login("rl101", "wrong again")[0], 401)       # an 11th failure: 60 s now
            self.assertEqual(self.login("rl101", NEW_PW)[1]["retry_after"], 2 * accounts.BACKOFF_S)
            t[0] += 2 * accounts.BACKOFF_S + 1
            self.assertEqual(self.login("rl101", NEW_PW)[0], 200)
            t[0] += accounts.FAIL_WINDOW
        # one row per pause (two here), however many tries it refused
        self.assertEqual(len([e for e in self.events("signin_limited") if e["user"] and e["user"]["username"] == "rl101"]), 2)

    def test_failed_sign_ins_are_limited_per_address(self):
        uid, _ = self.person("rl201")
        cf = {"CF-Connecting-IP": "203.0.113.7"}
        for i in range(accounts.ADDRESS_FAILS):                 # different names, one address behind cloudflared
            self.assertEqual(self.login(f"guess{i}", "password123", headers=cf)[0], 401)
        s, j, _ = self.login("rl201", NEW_PW, headers=cf)
        self.assertEqual(s, 429)
        self.assertEqual(self.login("rl201", NEW_PW, headers={"CF-Connecting-IP": "203.0.113.8"})[0], 200)
        self.assertEqual(self.login("rl201", NEW_PW)[0], 200)   # 127.0.0.1 itself is another address
        # the header counts only from 127.0.0.1 (cloudflared): from anywhere else it is the sender's own
        for i in range(accounts.ADDRESS_FAILS):
            self.login(f"guess{i}", "password123", source="127.0.0.3", headers={"CF-Connecting-IP": f"198.51.100.{i}"})
        self.assertEqual(self.login("rl201", NEW_PW, source="127.0.0.3", headers={"CF-Connecting-IP": "198.51.100.200"})[0], 429)
        ips = {e["ip"] for e in self.events("signin_failed")}
        self.assertIn("203.0.113.7", ips)
        self.assertIn("127.0.0.3", ips)
        self.assertNotIn("198.51.100.1", ips)

    def test_the_limiter_forgets_and_stays_small(self):
        t = [1000.0]
        with mock.patch.object(accounts, "_now", lambda: t[0]), mock.patch.object(accounts.Limiter, "MAX_KEYS", 10):
            lim = accounts.Limiter(3, 60, 10, 40)
            for _ in range(3):
                lim.hit("a")
            self.assertEqual(lim.wait("a"), 10)
            lim.hit("a")
            self.assertEqual(lim.wait("a"), 20)
            for _ in range(3):
                lim.hit("a")
            self.assertEqual(lim.wait("a"), 40)             # the cap
            t[0] += 61
            self.assertEqual(lim.wait("a"), 0)              # the window forgets
            for i in range(25):
                lim.hit(f"k{i}")
            self.assertLessEqual(len(lim.hits), 11)
            self.assertIn("k24", lim.hits)

    def test_a_password_is_scrypt_with_its_parameters_and_rises_later(self):
        uid, _ = self.person("hs101")
        h = accounts.account(uid)["pw_hash"]
        n, r, p, salt, key = accounts._parse(h)
        self.assertEqual((n, r, p, len(salt), len(key)), (2 ** 14, 8, 1, 16, 32))
        self.assertTrue(h.startswith("scrypt$16384$8$1$"))
        self.assertNotIn(NEW_PW, self.hub.dump())
        with mock.patch.object(accounts, "SCRYPT_N", 2 ** 12):
            old = accounts.hash_password(NEW_PW)
        db.conn().execute("UPDATE users SET pw_hash = ? WHERE id = ?", (old, uid))
        self.assertTrue(accounts.outdated(old))
        self.assertEqual(self.login("hs101", NEW_PW)[0], 200)
        self.assertTrue(accounts.account(uid)["pw_hash"].startswith("scrypt$16384$8$1$"))
        # the rules: no composition rules, 10 or more, not common, not the username
        user = {"username": "hs101", "email": "hs101@ic.ac.uk"}
        for pw, ok in (("short pw", False), ("password123", False), ("Password1234", False), ("qwertyuiop", False),
                       ("aaaaaaaaaaaa", False), ("my hs101 key!", False), ("abcdefghijklmn", True), ("tea at four, always", True),
                       ("ÅÅÅÅÅBBBBBcc", True), ("x" * 257, False)):
            self.assertEqual(accounts.password_problem(pw, user) is None, ok, pw)
        short = {"username": "li", "email": "li@ic.ac.uk"}
        self.assertIsNone(accounts.password_problem("a quiet little lighthouse", short))
        self.assertIsNotNone(accounts.password_problem("my li@ic.ac.uk login", short))

    # ---------------------------------------------------------------- sessions
    def test_changing_the_password_ends_the_other_sessions(self):
        uid, a = self.person("cp101")
        _, _, b = self.login("cp101", NEW_PW)
        self.assertEqual(self.me(b)[0], 200)
        s, e, _ = self.r("POST", "/api/auth/password", {"current": "not it", "password": "another long password"}, **a)
        self.assertEqual((s, e["error"]), (400, "wrong_password"))
        s, e, _ = self.r("POST", "/api/auth/password", {"password": "another long password"}, **a)
        self.assertEqual((s, e["error"]), (400, "wrong_password"))
        s, e, _ = self.r("POST", "/api/auth/password", {"current": NEW_PW, "password": "password123"}, **a)
        self.assertEqual((s, e["error"]), (400, "weak_password"))
        self.assertEqual(self.me(b)[0], 200)                    # refusals end nothing
        s, j, r = self.r("POST", "/api/auth/password", {"current": NEW_PW, "password": "another long password"}, **a)
        self.assertEqual(s, 200, j)
        a2 = {"cookie": cookie_of(r)}
        self.assertEqual((self.me(a)[0], self.me(b)[0], self.me(a2)[0]), (401, 401, 200))
        self.assertEqual(self.login("cp101", NEW_PW)[0], 401)
        self.assertEqual(self.login("cp101", "another long password")[0], 200)
        self.assertEqual(self.r("POST", "/api/auth/password", {"current": "x", "password": "y" * 12})[0], 401)

    def test_sign_out_and_sign_out_everywhere(self):
        uid, a = self.person("so101")
        _, _, b = self.login("so101", NEW_PW)
        token = self.device(a, "so-laptop")
        s, j, r = self.r("POST", "/api/auth/signout", **a)
        self.assertEqual(s, 200)
        self.assertRegex(r.getheader("Set-Cookie"), r"^pcg_s=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax; Secure$")
        self.assertEqual(self.r("POST", "/api/auth/signout", **a, csrf=False)[0], 403)
        s, j, r = self.r("POST", "/api/auth/signout-all", **b)
        self.assertEqual(s, 200, j)
        self.assertIn("Max-Age=0", r.getheader("Set-Cookie"))
        self.assertEqual((self.me(a)[0], self.me(b)[0]), (401, 401))
        self.assertEqual(self.r("GET", "/api/cli/me", token=token)[0], 200)      # devices are under Devices
        self.assertIn(uid, [e["user"]["id"] for e in self.events("signout_all")])
        self.assertEqual(self.r("POST", "/api/auth/signout-all")[0], 401)

    def test_cookies_are_secure_over_https(self):
        _, _, r = self.r("POST", "/api/auth/login", {"login": "yl6719", "password": "Leo's own long password"})
        parts = [p.strip() for p in r.getheader("Set-Cookie").split(";")]
        self.assertRegex(parts[0], r"^pcg_s=v2\.\d+\.\d+\.\d+\.[A-Za-z0-9_-]{43}$")
        self.assertEqual(set(parts[1:]), {"Path=/", "Max-Age=2592000", "HttpOnly", "SameSite=Lax", "Secure"})
        plain = C.load({**self.hub.env, "PCG_PUBLIC_URL": "http://127.0.0.1:8480"})
        self.assertNotIn("Secure", auth.session_cookie(plain, self.leo_id, sv=0))
        self.assertNotIn("Secure", auth.clear_cookie(plain))
        # a cookie from another mode or another secret, or with the wrong session version, is nobody
        uid = self.leo_id
        sv = accounts.account(uid)["session_v"]
        for v in (auth.make_session(self.hub.cfg, uid), auth.make_session_v2(self.hub.cfg, uid, sv + 1),
                  auth.make_session_v2(C.load({**self.hub.env, "PCG_SECRET": "another-secret-0123456789"}), uid, sv),
                  auth.make_session_v2(self.hub.cfg, uid, sv, at=time.time() - 31 * 86400)):
            self.assertEqual(self.r("GET", "/api/me", cookie=f"pcg_s={v}")[0], 401, v)
        self.assertEqual(self.r("GET", "/api/me", cookie=f"pcg_s={auth.make_session_v2(self.hub.cfg, uid, sv)}")[0], 200)

    # ---------------------------------------------------------------- reset links
    def test_forgot_emails_a_link_and_answers_the_same_for_anyone(self):
        uid, old = self.person("fg101")
        n = len(self.smtp.messages)
        s1, j1, _ = self.r("POST", "/api/auth/forgot", {"email": "fg101@ic.ac.uk"})
        s2, j2, _ = self.r("POST", "/api/auth/forgot", {"email": "stranger@ic.ac.uk"})
        s3, j3, _ = self.r("POST", "/api/auth/forgot", {"email": "someone@gmail.com"})
        self.assertEqual((s1, j1), (200, {"ok": True, "email": True, "message": accounts.SAME_ANSWER}))
        self.assertEqual((s2, j2), (s1, j1))
        self.assertEqual((s3, j3), (s1, j1))
        self.assertTrue(accounts.mail_idle())
        self.assertTrue(self.smtp.wait(n + 1))
        time.sleep(0.2)
        self.assertEqual(len(self.smtp.messages), n + 1)       # only the one on the list
        m = self.smtp.messages[-1]
        self.assertEqual((m["from"], m["to"], m["user"], m["tls"]), (BOT, ["fg101@ic.ac.uk"], BOT, True))
        msg = self.smtp.parsed()
        self.assertEqual((msg["To"], msg["From"], msg["Subject"]), ("fg101@ic.ac.uk", f"papercast <{BOT}>", "Your papercast password link"))
        self.assertEqual(msg.get_content_type(), "text/plain")
        body = msg.get_content()
        self.assertIn("for fg101", body)
        self.assertIn("works once, for the next hour", body)
        tok = re.search(r"https://papercast\.example/set-password#t=([A-Za-z0-9_-]{40,})", body)[1]
        row = self.hub.q("SELECT * FROM pw_links WHERE token_hash = ?", (sha(tok),))[0]
        self.assertEqual((row["kind"], row["user_id"]), ("email", uid))
        self.assertEqual(auth._unix(row["expires_at"]) - auth._unix(row["created_at"]), 3600)
        self.assertNotIn(tok, self.hub.dump())
        kinds = [(e["kind"], e["email"]) for e in self.events()]
        self.assertIn(("link_sent", "fg101@ic.ac.uk"), kinds)
        self.assertIn(("reset_unknown", "stranger@ic.ac.uk"), kinds)
        # the link: peek, then set (signs in, ends the other sessions), then never again
        s, peek, _ = self.r("POST", "/api/auth/link", {"token": tok, "peek": True})
        self.assertEqual((s, peek["username"], peek["email"]), (200, "fg101", "fg101@ic.ac.uk"))
        self.assertEqual(self.r("POST", "/api/auth/link", {"token": tok, "password": "fg101 again"})[1]["error"], "weak_password")
        self.assertEqual(self.r("POST", "/api/auth/link", {"token": tok, "password": "a brand new password"}, csrf=False)[0], 403)
        s, j, r = self.r("POST", "/api/auth/link", {"token": tok, "password": "a brand new password"})
        self.assertEqual((s, j["user"]["id"]), (200, uid))
        self.assertEqual(self.me({"cookie": cookie_of(r)})[0], 200)
        self.assertEqual(self.me(old)[0], 401)
        self.assertEqual(self.r("POST", "/api/auth/link", {"token": tok, "password": "yet another password"})[0], 410)
        self.assertEqual(self.r("POST", "/api/auth/link", {"token": tok, "peek": True})[0], 410)
        self.assertEqual(self.login("fg101", "a brand new password")[0], 200)
        self.assertIn(uid, [e["user"]["id"] for e in self.events("link_used")])

    def test_links_expire_and_a_new_password_voids_the_others(self):
        uid, _ = self.person("fg201")
        for _ in range(2):
            self.r("POST", "/api/auth/forgot", {"email": "fg201@ic.ac.uk"})
        self.assertTrue(accounts.mail_idle())
        toks = [re.search(r"#t=([A-Za-z0-9_-]+)", self.smtp.parsed(i).get_content())[1] for i in (-2, -1)]
        self.hub.q("UPDATE pw_links SET expires_at = ? WHERE token_hash = ?", (auth._iso(time.time() - 1), sha(toks[0])))
        self.assertEqual(self.r("POST", "/api/auth/link", {"token": toks[0], "password": "a brand new password"})[0], 410)
        s, j, _ = self.r("POST", "/api/auth/link", {"token": toks[1], "peek": True})
        self.assertEqual(s, 200, j)
        s, j, r = self.r("POST", "/api/admin/users/%d/reset-link" % uid, **self.leo)
        admin_tok = j["url"].split("#t=")[1]
        self.assertEqual(self.r("POST", "/api/auth/link", {"token": toks[1], "password": "a brand new password"})[0], 200)
        self.assertEqual(self.r("POST", "/api/auth/link", {"token": admin_tok, "peek": True})[0], 410)   # voided
        for junk in ("", "x", "!" * 43, None, 12):
            self.assertEqual(self.r("POST", "/api/auth/link", {"token": junk, "peek": True})[0], 410)

    def test_reset_requests_are_limited(self):
        uid, _ = self.person("fg301")
        n = len(self.smtp.messages)
        for _ in range(accounts.ASK_PER_EMAIL + 2):
            s, j, _ = self.r("POST", "/api/auth/forgot", {"email": "fg301@ic.ac.uk"}, source="127.0.0.5")
            self.assertEqual((s, j["message"]), (200, accounts.SAME_ANSWER))     # the same answer past the limit
        self.assertTrue(accounts.mail_idle())
        time.sleep(0.2)
        self.assertEqual(len(self.smtp.messages), n + accounts.ASK_PER_EMAIL)
        self.assertEqual(len([e for e in self.events("reset_limited") if e["email"] == "fg301@ic.ac.uk"]), 1)
        for i in range(accounts.ASK_PER_ADDRESS - accounts.ASK_PER_EMAIL - 2):
            self.assertEqual(self.r("POST", "/api/auth/forgot", {"email": f"n{i}@ic.ac.uk"}, source="127.0.0.5")[0], 200)
        s, j, _ = self.r("POST", "/api/auth/forgot", {"email": "other@ic.ac.uk"}, source="127.0.0.5")
        self.assertEqual((s, j["error"]), (429, "slow_down"))
        self.assertEqual(self.r("POST", "/api/auth/forgot", {"email": "other@ic.ac.uk"}, source="127.0.0.6")[0], 200)
        self.assertEqual(self.r("POST", "/api/auth/forgot", {"email": "not an email"}, source="127.0.0.7")[0], 400)

    def test_without_email_the_request_waits_for_an_admin(self):
        uid, _ = self.person("ne101")
        self.hub.cfg.smtp_host = ""
        n = len(self.smtp.messages)
        s1, j1, _ = self.r("POST", "/api/auth/forgot", {"email": "ne101@ic.ac.uk"})
        s2, j2, _ = self.r("POST", "/api/auth/forgot", {"email": "stranger2@ic.ac.uk"})
        self.assertEqual((s1, j1), (200, {"ok": True, "email": False, "message": accounts.SAME_ANSWER_NO_EMAIL}))
        self.assertEqual((s2, j2), (s1, j1))
        self.assertIn("ask one of them to reset your password", j1["message"])
        self.assertFalse(self.r("GET", "/api/auth/state")[1]["email"])
        time.sleep(0.2)
        self.assertEqual(len(self.smtp.messages), n)
        j = self.r("GET", "/api/admin/users", **self.leo)[1]
        u = {x["id"]: x for x in j["users"]}[uid]
        self.assertIsNotNone(u["reset_asked_at"])
        self.assertFalse(j["email"])
        self.assertIn("no email set up", [e["detail"] or "" for e in self.events("reset_asked") if e["user"]["id"] == uid][0])
        # the admin makes a link (24 hours) or resets to the first password; either clears the request
        s, link, _ = self.r("POST", f"/api/admin/users/{uid}/reset-link", **self.leo)
        self.assertEqual(s, 201, link)
        self.assertTrue(link["url"].startswith(f"{PUBLIC}/set-password#t="))
        self.assertEqual(auth._unix(link["expires_at"]) - time.time() > 86000, True)
        self.assertIsNone(accounts.account(uid)["reset_asked_at"])
        _, bob = self.person("ne102")
        self.assertEqual(self.r("POST", f"/api/admin/users/{uid}/reset-link", **bob)[0], 403)
        self.assertEqual(self.r("POST", "/api/admin/users/99999/reset-link", **self.leo)[0], 404)
        s, j, r = self.r("POST", "/api/auth/link", {"token": link["url"].split("#t=")[1], "password": "set by a link"})
        self.assertEqual(s, 200, j)

    def test_an_email_that_fails_is_logged_not_shown(self):
        self.person("ef101")
        self.smtp.fail = "auth"
        n = len(self.smtp.messages)
        s, j, _ = self.r("POST", "/api/auth/forgot", {"email": "ef101@ic.ac.uk"})
        self.assertEqual((s, j["message"]), (200, accounts.SAME_ANSWER))
        self.assertTrue(accounts.mail_idle())
        self.assertEqual(len(self.smtp.messages), n)
        fails = [e for e in self.events("email_failed") if e["email"] == "ef101@ic.ac.uk"]
        self.assertEqual(len(fails), 1)
        self.assertIn("SMTPAuthenticationError", fails[0]["detail"])

    def test_the_password_file_must_be_private(self):
        f = Path(self.hub.dir) / "open.pw"
        f.write_text(BOT_PW)
        os.chmod(f, 0o644)
        cfg = C.load({**self.hub.env, "PCG_SMTP_HOST": "127.0.0.1", "PCG_SMTP_PORT": str(self.smtp.port),
                      "PCG_SMTP_USER": BOT, "PCG_SMTP_PASSWORD_FILE": str(f), "PCG_SMTP_FROM": BOT})
        cfg.smtp_ca_file = self.smtp.ca
        with self.assertRaisesRegex(RuntimeError, "chmod 600"):
            accounts.send_email(cfg, "a@ic.ac.uk", "s", "b")
        os.chmod(f, 0o600)
        accounts.send_email(cfg, "a@ic.ac.uk", "s", "b")
        # port 465: TLS from the start (the fake one listens elsewhere: binding 465 needs root)
        tls = FakeSMTP(self.tmp, user=BOT, password=BOT_PW, implicit_tls=True).start()
        try:
            cfg465 = C.load({**self.hub.env, "PCG_SMTP_HOST": "127.0.0.1", "PCG_SMTP_PORT": "465",
                             "PCG_SMTP_USER": BOT, "PCG_SMTP_PASSWORD_FILE": str(f), "PCG_SMTP_FROM": BOT})
            cfg465.smtp_ca_file = self.smtp.ca
            real = accounts.smtplib.SMTP_SSL
            with mock.patch.object(accounts.smtplib, "SMTP_SSL", lambda host, port, **kw: real(host, tls.port, **kw)), \
                    mock.patch.object(accounts.smtplib.SMTP, "starttls", side_effect=AssertionError("no STARTTLS on 465")):
                accounts.send_email(cfg465, "a@ic.ac.uk", "on 465", "b")
            self.assertTrue(tls.wait(1))
            self.assertEqual((tls.messages[0]["tls"], tls.messages[0]["user"]), (True, BOT))
        finally:
            tls.stop()
        # a server without STARTTLS gets nothing: never in the clear
        plain = FakeSMTP(self.tmp, user=BOT, password=BOT_PW, starttls=False).start()
        try:
            cfgp = C.load({**self.hub.env, "PCG_SMTP_HOST": "127.0.0.1", "PCG_SMTP_PORT": str(plain.port),
                           "PCG_SMTP_USER": BOT, "PCG_SMTP_PASSWORD_FILE": str(f), "PCG_SMTP_FROM": BOT})
            with self.assertRaises(accounts.smtplib.SMTPNotSupportedError):
                accounts.send_email(cfgp, "a@ic.ac.uk", "s", "b")
            self.assertEqual(plain.messages, [])
        finally:
            plain.stop()

    def test_email_test_command(self):
        out = []
        with mock.patch("builtins.print", lambda *a, **k: out.append(" ".join(map(str, a)))):
            self.assertEqual(auth._email_test_cmd(self.hub.cfg, "leo@ic.ac.uk"), 0)
            self.smtp.fail = "data"
            self.assertEqual(auth._email_test_cmd(self.hub.cfg, "leo@ic.ac.uk"), 1)
            none = C.load({k: v for k, v in self.hub.env.items() if not k.startswith("PCG_SMTP")})
            self.assertEqual(auth._email_test_cmd(none, "leo@ic.ac.uk"), 2)
        self.assertIn("sent to leo@ic.ac.uk through 127.0.0.1", out[0])
        self.assertIn("not sent: SMTPDataError", out[1])
        self.assertIn("Email is not set up", out[2])
        self.assertEqual(self.smtp.parsed()["Subject"], "papercast: a test email")

    # ---------------------------------------------------------------- admin resets
    # ---------------------------------------------------------------- "Remember me"
    def test_remember_me(self):
        uid, _ = self.person("rm701")
        s, j, r = self.r("POST", "/api/auth/login", {"login": "rm701", "password": NEW_PW, "remember": False})
        self.assertEqual(s, 200, j)
        self.assertNotIn("Max-Age", r.getheader("Set-Cookie"))          # ends with the browser
        short = cookie_of(r)
        v = short.split("=", 1)[1]
        self.assertTrue(v.startswith("v2s."), v)
        self.assertEqual(self.r("GET", "/api/me", cookie=short)[0], 200)
        s, j, r2 = self.r("POST", "/api/auth/login", {"login": "rm701", "password": NEW_PW})
        self.assertIn(f"Max-Age={auth.SESSION_S}", r2.getheader("Set-Cookie"))   # remembered unless asked not to
        self.assertTrue(cookie_of(r2).split("=", 1)[1].startswith("v2."))
        sv = accounts.account(uid)["session_v"]
        ago = time.time() - 13 * 3600
        self.assertEqual(self.r("GET", "/api/me", cookie=f"pcg_s={auth.make_session_v2(self.hub.cfg, uid, sv, at=ago, remember=False)}")[0], 401)
        self.assertEqual(self.r("GET", "/api/me", cookie=f"pcg_s={auth.make_session_v2(self.hub.cfg, uid, sv, at=ago)}")[0], 200)
        self.assertEqual(self.r("GET", "/api/me", cookie="pcg_s=v2." + v[len("v2s."):])[0], 401)   # the prefix is signed too
        # a new password renews the cookie, still not remembered
        s, j, r3 = self.r("POST", "/api/auth/password", {"current": NEW_PW, "password": "another long pass phrase"}, cookie=short)
        self.assertEqual(s, 200, j)
        self.assertNotIn("Max-Age", r3.getheader("Set-Cookie"))
        self.assertTrue(cookie_of(r3).split("=", 1)[1].startswith("v2s."))

    # ---------------------------------------------------------------- the welcome email
    def welcome_parts(self, i=-1):
        msg = self.smtp.parsed(i)
        self.assertEqual(msg.get_content_type(), "multipart/alternative")
        text = msg.get_body(("plain",)).get_content().replace("\r\n", "\n")     # CRLF on the wire
        page = msg.get_body(("html",)).get_content()
        return msg, text, page

    def test_welcome_email_when_an_admin_adds_someone(self):
        n = len(self.smtp.messages)
        s, j, _ = self.r("POST", "/api/admin/allowed", {"email": "wl101"}, **self.leo)
        self.assertEqual((s, j["state"], j["welcome"]), (201, "added", "queued"))
        self.assertTrue(accounts.mail_idle() and self.smtp.wait(n + 1))
        m = self.smtp.messages[-1]
        self.assertEqual((m["to"], m["user"], m["tls"]), (["wl101@ic.ac.uk"], BOT, True))
        msg, text, page = self.welcome_parts()
        self.assertEqual((msg["To"], msg["Subject"]), ("wl101@ic.ac.uk", "Welcome to Virtual Atoms Lab Papercast"))
        self.assertTrue(text.startswith("Hi, wl101\n"), text[:40])
        self.assertIn(f"Join: {PUBLIC}/signin?u=wl101\n", text)
        self.assertRegex(text, r"Email:\s+wl101@ic\.ac\.uk\n\s+Password:\s+wl101\n")
        self.assertIn(accounts.GITHUB_URL, text)
        self.assertIn(f'href="{PUBLIC}/signin?u=wl101"', page)
        self.assertIn("https://papercast.virtualatoms.org/email/welcome-banner.jpg", page)
        for part in (text, page):
            self.assertNotRegex(part, r"\{(" + "|".join(accounts.WELCOME_KEYS) + r")\}")
        ev = [e for e in self.events("welcome_sent") if e["email"] == "wl101@ic.ac.uk"]
        self.assertEqual((len(ev), ev[0]["actor"]["id"]), (1, self.leo_id))
        u = next(u for u in self.r("GET", "/api/admin/users", **self.leo)[1]["users"] if u["username"] == "wl101")
        self.assertEqual((bool(u["welcome_at"]), u["welcome_failed_at"]), (True, None))
        # already on the list, or asked not to: no email
        self.assertEqual(self.r("POST", "/api/admin/allowed", {"email": "wl101"}, **self.leo)[1]["welcome"], "already")
        self.assertEqual(self.r("POST", "/api/admin/allowed", {"email": "wl102", "welcome": False}, **self.leo)[1]["welcome"], "off")
        self.assertTrue(accounts.mail_idle())
        time.sleep(0.2)
        self.assertEqual(len(self.smtp.messages), n + 1)

    def test_welcome_values_are_escaped_and_never_read_as_placeholders(self):
        row = {"username": "ab1", "email": "ab1@ic.ac.uk", "name": '<img src=x onerror="go()"> & {username}'}
        subject, text, page = accounts.welcome_email(self.hub.cfg, row)
        self.assertIn('Hi, <img src=x onerror="go()"> & {username}\n', text)
        self.assertIn("Hi, &lt;img src=x onerror=&quot;go()&quot;&gt; &amp; {username}</p>", page)
        self.assertNotIn("<img src=x", page)
        self.assertIn("@media only screen", page)                # the style blocks' braces are left alone

    def test_the_welcome_again_from_the_users_panel(self):
        n = len(self.smtp.messages)
        uid = self.r("POST", "/api/admin/allowed", {"email": "wa101"}, **self.leo)[1]["user_id"]
        for _ in range(accounts.WELCOME_PER_HOUR - 1):
            s, j, _ = self.r("POST", f"/api/admin/users/{uid}/welcome", **self.leo)
            self.assertEqual((s, j["email"]), (202, "wa101@ic.ac.uk"))
        s, j, _ = self.r("POST", f"/api/admin/users/{uid}/welcome", **self.leo)
        self.assertEqual((s, j["error"]), (429, "slow_down"))    # the add and two more, per hour
        self.assertTrue(accounts.mail_idle() and self.smtp.wait(n + accounts.WELCOME_PER_HOUR))
        self.assertEqual({tuple(m["to"]) for m in self.smtp.messages[n:]}, {("wa101@ic.ac.uk",)})
        accounts.reset_limits()
        # only admins; not once they chose a password; not without email
        bob_id, bob = self.person("wa102")
        self.assertEqual(self.r("POST", f"/api/admin/users/{uid}/welcome", **bob)[0], 403)
        s, j, _ = self.r("POST", f"/api/admin/users/{bob_id}/welcome", **self.leo)
        self.assertEqual((s, j["error"]), (409, "has_password"))
        self.hub.cfg.smtp_host = ""
        s, j, _ = self.r("POST", f"/api/admin/users/{uid}/welcome", **self.leo)
        self.assertEqual((s, j["error"]), (409, "no_email"))
        self.assertEqual(self.r("POST", "/api/admin/allowed", {"email": "wa103"}, **self.leo)[1]["welcome"], "no_email")
        self.assertEqual(self.r("POST", "/api/admin/users/999999/welcome", **self.leo)[0], 404)

    def test_a_welcome_that_fails_is_logged(self):
        self.smtp.fail = "auth"
        n = len(self.smtp.messages)
        self.assertEqual(self.r("POST", "/api/admin/allowed", {"email": "wf101"}, **self.leo)[1]["welcome"], "queued")
        self.assertTrue(accounts.mail_idle())
        self.assertEqual(len(self.smtp.messages), n)
        self.assertIn("wf101@ic.ac.uk", [e["email"] for e in self.events("welcome_failed")])
        u = next(u for u in self.r("GET", "/api/admin/users", **self.leo)[1]["users"] if u["username"] == "wf101")
        self.assertEqual((u["welcome_at"], bool(u["welcome_failed_at"])), (None, True))

    def test_the_welcome_from_the_command_line(self):
        trust = {"SSL_CERT_FILE": self.smtp.ca}                 # the subprocess cannot take the test hook
        n = len(self.smtp.messages)
        p = self.hub.run("allow", "add", "wc101", "wc102", env=trust)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("welcome email sent to wc101@ic.ac.uk", p.stdout)
        self.assertEqual([m["to"] for m in self.smtp.messages[n:]], [["wc101@ic.ac.uk"], ["wc102@ic.ac.uk"]])
        self.assertIn("wc102@ic.ac.uk", [e["email"] for e in self.events("welcome_sent")])
        p = self.hub.run("allow", "add", "wc101", "wc103", "--no-welcome", env=trust)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(len(self.smtp.messages), n + 2)
        self.smtp.fail = "auth"
        p = self.hub.run("allow", "add", "wc104", env=trust)
        self.assertEqual(p.returncode, 1)
        self.assertIn("the welcome email to wc104@ic.ac.uk was not sent", p.stderr)
        self.assertIn("added wc104@ic.ac.uk", p.stdout)

    def test_the_email_banner_is_public(self):
        s, raw, r = self.r("GET", "/email/welcome-banner.jpg")
        self.assertEqual((s, r.getheader("Content-Type")), (200, "image/jpeg"))
        self.assertTrue(raw.startswith(b"\xff\xd8"))
        for bad in ("/email/nope.jpg", "/email/../app.js", "/email/welcome.html", "/email/%2e%2e/app.js"):
            self.assertEqual(self.r("GET", bad)[0], 404, bad)

    def test_reset_to_the_first_password(self):
        uid, who = self.person("rd101")
        token = self.device(who, "rd-laptop")
        _, j, _ = self.r("POST", f"/api/admin/users/{uid}/reset-link", **self.leo)
        s, j, _ = self.r("POST", f"/api/admin/users/{uid}/reset-default", **self.leo)
        self.assertEqual((s, j["user"]["must_change"]), (200, True))
        self.assertEqual(self.me(who)[0], 401)
        self.assertEqual(self.r("GET", "/api/cli/me", token=token)[0], 401)
        self.assertEqual(self.login("rd101", NEW_PW)[0], 401)
        s, j, c = self.login("rd101", "rd101")
        self.assertEqual((s, j["must_change"]), (200, True))
        users = {u["id"]: u for u in self.r("GET", "/api/admin/users", **self.leo)[1]["users"]}
        self.assertTrue(users[uid]["default_password"])
        self.assertIn(uid, [e["user"]["id"] for e in self.events("reset_default")])
        _, bob = self.person("rd102")
        self.assertEqual(self.r("POST", f"/api/admin/users/{uid}/reset-default", **bob)[0], 403)

    def test_disabling_ends_the_sessions(self):
        uid, who = self.person("ds101")
        self.assertEqual(self.r("PUT", f"/api/admin/users/{uid}", {"disabled": True}, **self.leo)[0], 200)
        self.assertEqual(self.me(who)[0], 401)
        s, j, _ = self.login("ds101", NEW_PW)
        self.assertEqual((s, j["error"]), (403, "disabled"))
        self.assertEqual(self.r("PUT", f"/api/admin/users/{uid}", {"disabled": False}, **self.leo)[0], 200)
        self.assertEqual(self.me(who)[0], 401)                  # re-enabling does not bring a session back
        self.assertEqual(self.login("ds101", NEW_PW)[0], 200)
        self.assertEqual([e["detail"] for e in self.events("disabled") if e["user"]["id"] == uid], ["yes -> no", "no -> yes"])

    # ---------------------------------------------------------------- the pages, over HTTP
    def test_signed_out_the_site_is_the_sign_in_page(self):
        for path in ("/", "/index.html"):
            s, _, r = self.r("GET", path)
            self.assertEqual((s, r.getheader("Location")), (303, "/signin"), path)
        for path, js in (("/signin", b'src="/signin.js"'), ("/set-password", b'src="/setpw.js"')):
            s, body, r = self.r("GET", path)
            self.assertEqual((s, r.getheader("Content-Security-Policy"), r.getheader("X-Frame-Options")), (200, app.PAGE_CSP, "DENY"))
            self.assertIn(js, body)
            self.assertIn(b'href="/auth.css"', body)
        for name in ("signin.js", "setpw.js"):
            s, _, r = self.r("GET", "/" + name)
            self.assertEqual((s, r.getheader("Content-Type").split(";")[0]), (200, "text/javascript"))
        self.assertEqual(self.r("GET", "/api/auth/state")[1],
                         {"mode": "password", "email": True, "signed_in": False, "must_change": False, "user": None})
        self.assertEqual(self.r("GET", "/api/me")[0], 401)      # the API still answers 401
        self.assertEqual(self.r("GET", "/", **self.leo)[0], 200)

    def test_the_new_pages_have_nothing_inline(self):
        static = self.hub.cfg.static
        for name in ("signin.html", "setpw.html"):
            html = (static / name).read_text()
            self.assertNotRegex(html, r"<script(?![^>]*\bsrc=)[^>]*>", name)
            self.assertNotRegex(html, r"(?i)<style|\sstyle=|\son[a-z]+=|javascript:", name)
        for name in ("signin.js", "setpw.js"):
            js = (static / name).read_text()
            self.assertNotRegex(js, r"innerHTML|outerHTML|insertAdjacentHTML|document\.write|\.style\.|setAttribute\(\s*['\"]style|eval\(", name)

    def test_other_modes_are_untouched(self):
        # an unknown mode is refused at start; header mode never looks at passwords
        with self.assertRaises(SystemExit):
            C.load({**self.hub.env, "PCG_AUTH": "google"})
        self.assertEqual(C.load({**self.hub.env, "PCG_AUTH": "password"}).auth, "password")


# ============================================================ the pages in a browser

try:
    import websocket  # noqa: F401
    from web_cdp import Browser, find_chrome
    NO_BROWSER = None if find_chrome() else "no headless Chrome"
except ImportError:
    NO_BROWSER = "websocket-client not installed"

# Leo's tap-target check (tests/test_page.py TAP_TARGETS), for pages without the library's panes:
# every control shown that is under 44 x 44 px, or that a tap on its edge does not reach.
TAPS = r"""(() => {
  const q = 'a[href], button, input:not([type=hidden]), select, textarea, summary, label, [role=button], [role=link],' +
            ' [role=slider], [role=menuitem], [tabindex]:not([tabindex="-1"])';
  const top = document.querySelector('.menu');
  const open = document.body.classList.contains('open');
  const name = (e) => `${e.tagName.toLowerCase()}${e.id ? '#' + e.id : ''} "${(e.getAttribute('aria-label') || e.textContent || e.placeholder || '').trim().slice(0, 24)}"`;
  const bad = [];
  for (const e of document.querySelectorAll(q)) {
    const cs = getComputedStyle(e);
    if (e.closest('[hidden]') || cs.visibility !== 'visible' || cs.pointerEvents === 'none' || !e.getClientRects().length) continue;
    if ((top && !top.contains(e)) || (open && e.closest('#list-pane'))) continue;
    const r = e.getBoundingClientRect();
    if (r.width < 43.5 || r.height < 43.5) bad.push(`${name(e)} is ${r.width.toFixed(1)} x ${r.height.toFixed(1)}`);
    let fixed = false;
    for (let a = e; a; a = a.parentElement) if (['fixed', 'sticky'].includes(getComputedStyle(a).position)) { fixed = true; break; }
    if (!fixed) e.scrollIntoView({block: 'center', inline: 'nearest'});
    const b = e.getBoundingClientRect();
    for (const [x, y] of [[b.left + 1, b.top + b.height / 2], [b.right - 1, b.top + b.height / 2], [b.left + b.width / 2, b.top + 1], [b.left + b.width / 2, b.bottom - 1]]) {
      if (x < 0 || y < 0 || x >= innerWidth || y >= innerHeight) continue;
      const h = document.elementFromPoint(x, y);
      if (h !== e && !e.contains(h)) bad.push(`${name(e)} is covered at ${Math.round(x)},${Math.round(y)} by ${h ? h.tagName.toLowerCase() + (h.id ? '#' + h.id : '') : 'nothing'}`);
    }
  }
  return JSON.stringify(bad);
})()"""


@unittest.skipIf(NO_BROWSER or not OPENSSL, NO_BROWSER or "openssl is not installed")
class PasswordPagesTest(unittest.TestCase):
    """The sign-in and set-password pages, the approve page through sign-in, and Settings'
    Account and Users, in a headless Chrome. No test may leave an error in the console (a CSP
    violation is one) unless it asked for that refusal."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="pcg-pw-pages-"))
        cls.smtp = FakeSMTP(cls.tmp).start()
        cls.hub = Hub(PCG_SMTP_HOST="127.0.0.1", PCG_SMTP_PORT=str(cls.smtp.port), PCG_SMTP_FROM=BOT)
        cls.hub.cfg.smtp_ca_file = cls.smtp.ca
        cls.hub.start()
        cls.base = f"http://127.0.0.1:{cls.hub.port}"
        cls.hub.cfg.public_url = cls.base                   # the browser's origin (CSRF), plain http here
        from hub import layout
        layout.AUTO = False
        accounts.bootstrap(cls.hub.cfg, LEO)
        db.conn().execute("UPDATE users SET pw_hash = ?, name = 'Leo' WHERE email = ?", (accounts.hash_password("Leo's own long password"), LEO))
        cls.leo_id = accounts.account(email=LEO)["id"]
        cls.b = Browser()
        cls.b.call("Log.enable")
        cls.b.viewport(1280, 860)

    @classmethod
    def tearDownClass(cls):
        cls.b.close()
        cls.hub.close()
        cls.smtp.stop()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        accounts.reset_limits()
        self.allow = []                                     # console errors this test expects (regexes)
        self.b.call("Network.clearBrowserCookies")
        self.b.viewport(1280, 860)
        self.b.pump(0.05)
        self.b.events.clear()

    def tearDown(self):
        self.b.pump(0.3)
        errs = []
        for m in self.b.events:
            meth, p = m.get("method"), m.get("params", {})
            if meth == "Runtime.exceptionThrown":
                d = p.get("exceptionDetails", {})
                errs.append(f"exception: {(d.get('exception') or {}).get('description') or d.get('text')}")
            elif meth == "Runtime.consoleAPICalled" and p.get("type") in ("error", "assert"):
                errs.append("console.error: " + " ".join(str(a.get("value", a.get("description", ""))) for a in p.get("args", [])))
            elif meth == "Log.entryAdded" and p.get("entry", {}).get("level") == "error":
                e = p["entry"]
                errs.append(f"log ({e.get('source')}): {e.get('text')} {e.get('url', '')}")
        errs = [e for e in errs if not any(re.search(a, e) for a in self.allow)]
        self.b.events.clear()
        self.assertEqual(errs, [], "errors in the console")

    # -- helpers
    def js(self, expr):
        return self.b.js(expr)

    def wait(self, expr, what, timeout=10):
        return self.b.wait_js(expr, timeout, what)

    def typ(self, sel, value):
        self.js(f"{{ const e = document.querySelector({json.dumps(sel)}); e.value = {json.dumps(value)}; e.dispatchEvent(new Event('input')); }}")

    def click(self, sel):
        self.js(f"document.querySelector({json.dumps(sel)}).click()")

    def visible(self, sel):
        return self.js(f"(() => {{ const e = document.querySelector({json.dumps(sel)}); return !!e && !e.closest('[hidden]') && e.getClientRects().length > 0; }})()")

    def taps(self, where):
        bad = json.loads(self.js(TAPS))
        self.assertEqual(bad, [], f"phone, {where}: {len(bad)} tap target(s) too small or covered:\n" + "\n".join(bad))

    def sign_in(self, ident, pw):
        self.wait("location.pathname === '/signin' && !document.getElementById('signin').hidden", "the sign-in form")
        self.typ("#login", ident)
        self.typ("#password", pw)
        self.click("#go")

    def new_person(self, code, pw=None):
        uid = accounts.allow(self.hub.cfg, code)["user_id"]
        if pw:
            db.conn().execute("UPDATE users SET pw_hash = ? WHERE id = ?", (accounts.hash_password(pw), uid))
        return uid

    def signed_in_as_leo(self, where="/"):
        self.b.goto(self.base + "/signin?next=" + where)
        self.sign_in("yl6719", "Leo's own long password")
        self.wait(f"location.pathname === {json.dumps(where.split('#')[0].split('?')[0])}", "signed in")

    # -- tests
    def test_1_first_sign_in_asks_for_a_new_password_then_opens_the_library(self):
        self.new_person("pg101")
        self.b.goto(self.base + "/")
        self.wait("location.pathname === '/signin' && !document.getElementById('signin').hidden", "the site sends a signed-out browser to sign in")
        self.assertEqual(self.js("document.title"), "Sign in · papercast")
        self.assertTrue(self.visible("#signin"))
        self.assertEqual(self.js("document.getElementById('password').type"), "password")
        self.click("#show")
        self.assertEqual(self.js("document.getElementById('password').type"), "text")
        self.click("#show")
        self.sign_in("pg101", "pg101")
        self.wait("location.pathname === '/set-password' && !document.getElementById('setpw').hidden", "the set-password form")
        self.assertEqual(self.js("document.getElementById('title').textContent"), "Choose your own password")
        self.assertEqual(self.js("document.getElementById('username').value"), "pg101")
        self.assertTrue(self.visible("#out"))
        self.allow = [r"400 \(Bad Request\) \S+/api/auth/password$"]
        self.typ("#password", "pg101 and more")
        self.click("#save")
        self.wait("document.getElementById('msg').textContent.includes('does not contain your username')", "refused")
        self.typ("#password", "the tea is cold today")
        self.click("#save")
        self.wait("location.pathname === '/' && !!document.getElementById('rows')", "the library")
        self.wait("document.title === 'Papers'", "the page started")
        self.assertIsNotNone(accounts.account(username="pg101")["pw_hash"])

    def test_2_wrong_password_and_the_approve_page_through_sign_in(self):
        self.new_person("pg201", pw="my own long password")
        s, st, _ = self.hub.req("POST", "/api/cli/login/start", {"device": "pg201-laptop"})
        self.b.goto(self.base + f"/cli?code={st['code']}")
        self.wait("location.pathname === '/signin'", "the approve page sends a signed-out browser to sign in")
        self.assertEqual(self.js("new URLSearchParams(location.search).get('next')"), f"/cli?code={st['code']}")
        self.allow = [r"401 \(Unauthorized\) \S+/api/auth/login$"]
        self.sign_in("pg201", "not my password")
        self.wait("document.getElementById('msg').textContent === " + json.dumps(accounts.BAD_LOGIN), "wrong password said")
        self.assertEqual(self.js("document.getElementById('password').value"), "")
        self.assertEqual(self.js("document.getElementById('msg').className"), "msg danger")
        self.typ("#password", "my own long password")
        self.click("#go")
        self.wait("location.pathname === '/cli' && !document.getElementById('ask').hidden", "back at the approve page")
        self.assertEqual(self.js("document.getElementById('code').textContent"), st["code"])
        self.assertIn("pg201-laptop", self.js("document.getElementById('title').textContent"))
        self.click("#yes")
        self.wait("document.getElementById('title').textContent === 'Approved'", "approved")
        s, p, _ = self.hub.req("POST", "/api/cli/login/poll", {"poll": st["poll"]})
        self.assertEqual((s, p["user"]["username"]), (200, "pg201"))

    def test_3_forgot_password_by_email(self):
        self.new_person("pg301", pw="the one I forgot")
        n = len(self.smtp.messages)
        self.b.goto(self.base + "/signin")
        self.wait("!document.getElementById('signin').hidden", "form")
        self.typ("#login", "pg301@ic.ac.uk")
        self.click("#forgot-open")
        self.assertTrue(self.visible("#forgot"))
        self.assertEqual(self.js("document.getElementById('email').value"), "pg301@ic.ac.uk")
        self.assertIn("emails you a link", self.js("document.getElementById('forgot-sub').textContent"))
        self.click("#send")
        self.wait("!document.getElementById('asked').hidden", "the answer")
        self.assertEqual(self.js("document.getElementById('msg').textContent"), accounts.SAME_ANSWER)
        self.assertTrue(accounts.mail_idle() and self.smtp.wait(n + 1))
        url = re.search(r"(http://127\.0\.0\.1:\d+/set-password#t=[A-Za-z0-9_-]+)", self.smtp.parsed().get_content())[1]
        # the same answer for an address that is not on the list
        self.click("#asked-back")
        self.click("#forgot-open")
        self.typ("#email", "nobody-here@ic.ac.uk")
        self.click("#send")
        self.wait("!document.getElementById('asked').hidden", "the answer again")
        self.assertEqual(self.js("document.getElementById('msg').textContent"), accounts.SAME_ANSWER)
        self.b.goto(url)
        self.wait("!document.getElementById('setpw').hidden", "the link's form")
        self.assertEqual(self.js("location.hash"), "")          # the secret left the address bar
        self.assertIn("pg301@ic.ac.uk", self.js("document.getElementById('msg').textContent"))
        self.typ("#password", "remembered this time")
        self.click("#save")
        self.wait("location.pathname === '/' && document.title === 'Papers'", "signed in, the library")
        self.b.goto(url)
        self.wait("!document.getElementById('dead').hidden", "a used link")
        self.allow = [r"410 \(Gone\) \S+/api/auth/link$"]
        self.assertEqual(self.js("document.getElementById('title').textContent"), "This link cannot be used")

    def test_4_account_tab(self):
        self.new_person("pg401", pw="my own long password")
        self.b.goto(self.base + "/signin")
        self.sign_in("pg401", "my own long password")
        self.wait("location.pathname === '/' && document.title === 'Papers'", "the library")
        self.js("location.hash = 'settings=account'")
        self.wait("!!document.getElementById('acct-pw-save')", "the account tab")
        tabs = self.js("[...document.querySelectorAll('#set-tabs .tab')].map(t => t.textContent)")
        self.assertEqual(tabs, ["Preferences", "Voice", "Devices", "Account"])
        self.typ("#acct-name", "Pat Gray")
        self.click("#acct-name-save")
        self.wait("document.getElementById('acct-name-msg').textContent === 'Saved'", "name saved")
        self.assertEqual(accounts.account(username="pg401")["name"], "Pat Gray")
        self.allow = [r"400 \(Bad Request\) \S+/api/auth/password$"]
        self.typ("#acct-cur", "wrong")
        self.typ("#acct-new", "a newer long password")
        self.click("#acct-pw-save")
        self.wait("document.getElementById('acct-pw-msg').textContent === 'Your current password is not right.'", "refused")
        self.typ("#acct-cur", "my own long password")
        self.click("#acct-pw-save")
        self.wait("document.getElementById('acct-pw-msg').textContent.startsWith('Changed')", "changed")
        self.assertEqual(self.hub.req("POST", "/api/auth/login", {"login": "pg401", "password": "a newer long password"})[0], 200)
        self.click("#acct-out-all")
        self.assertEqual(self.js("document.getElementById('acct-out-all').textContent"), "Sign out everywhere now")
        self.click("#acct-out-all")
        self.wait("location.pathname === '/signin'", "signed out everywhere")
        self.b.goto(self.base + "/")
        self.wait("location.pathname === '/signin'", "still signed out")

    def test_5_users_add_box_and_typed_delete(self):
        self.signed_in_as_leo("/#settings=users")
        self.wait("!!document.getElementById('add-code')", "the users tab")
        self.assertEqual(self.js("document.querySelector('#add-form .suffix').textContent"), "@ic.ac.uk")
        self.assertTrue(self.js("document.getElementById('add-go').disabled"))
        for bad, says in (("yl 67", "letters and digits"), ("x@gmail.com", "Only @ic.ac.uk"), ("a..b", "letters and digits")):
            self.typ("#add-code", bad)
            self.assertTrue(self.js("document.getElementById('add-go').disabled"), bad)
            self.assertIn(says, self.js("document.getElementById('add-msg').textContent"))
        self.typ("#add-code", "pg501")
        self.assertFalse(self.js("document.getElementById('add-go').disabled"))
        n = len(self.smtp.messages)
        self.click("#add-go")
        self.wait("document.getElementById('add-msg').textContent.startsWith('Added pg501@ic.ac.uk')", "added")
        self.assertIn("welcome email with their username and first password is on its way", self.js("document.getElementById('add-msg').textContent"))
        self.assertTrue(accounts.mail_idle() and self.smtp.wait(n + 1))
        self.assertEqual(self.smtp.messages[n]["to"], ["pg501@ic.ac.uk"])
        self.typ("#add-code", "PG502@ic.ac.uk")
        self.click("#add-go")
        self.wait("document.getElementById('add-msg').textContent.startsWith('Added pg502@ic.ac.uk')", "a full address")
        uid = accounts.account(username="pg501")["id"]
        row = f"#users .item[data-id=\"{uid}\"]"
        self.assertIn("first password", self.js(f"document.querySelector('{row} .it-s').textContent"))
        self.assertIn("never signed in", self.js(f"document.querySelector('{row} .it-s').textContent"))
        self.assertEqual(self.js(f"document.querySelector('{row} select').value"), "contributor")
        # the menu: a password link to copy
        self.click(f"{row} .icon-btn")
        self.wait("!!document.querySelector('.menu')", "menu")
        items = self.js("[...document.querySelectorAll('.menu button')].map(b => b.textContent)")
        self.assertEqual(items, ["Make a password link", "Send the welcome email again", "Reset to the first password", "Remove…"])
        n = len(self.smtp.messages)
        self.js("[...document.querySelectorAll('.menu button')][1].click()")
        self.assertTrue(self.smtp.wait(n + 1) and accounts.mail_idle())
        self.assertEqual(self.smtp.messages[n]["to"], ["pg501@ic.ac.uk"])
        self.click(f"{row} .icon-btn")
        self.wait("!!document.querySelector('.menu')", "menu")
        self.js("[...document.querySelectorAll('.menu button')][0].click()")
        self.wait(f"!!document.getElementById('link-{uid}')", "the link")
        self.assertRegex(self.js(f"document.getElementById('link-{uid}').value"), r"/set-password#t=[\w-]+$")
        # remove: the button works only once "delete" is typed
        self.click(f"{row} .icon-btn")
        self.js("[...document.querySelectorAll('.menu button')].find(b => b.textContent === 'Remove…').click()")
        go = f"document.getElementById('del-go-{uid}')"
        self.wait(f"!!{go}", "the confirm step")
        self.assertTrue(self.js(f"{go}.disabled"))
        for word in ("del", "remove", "delete it"):
            self.typ(f"#del-{uid}", word)
            self.assertTrue(self.js(f"{go}.disabled"), word)
        self.typ(f"#del-{uid}", "  DELETE ")
        self.assertFalse(self.js(f"{go}.disabled"))
        self.js(f"{go}.click()")
        self.wait(f"!document.querySelector('{row}')", "gone from the list")
        self.assertTrue(accounts.account(uid)["disabled"])
        self.assertIn("pg501", self.js("document.getElementById('off-list').textContent"))
        # his own row offers no reset and no removal
        self.click(f"#users .item[data-id=\"{self.leo_id}\"] .icon-btn")
        self.assertEqual(self.js("[...document.querySelectorAll('.menu button')].map(b => b.textContent)"), ["Make a password link"])
        self.js("document.body.click()")
        self.assertIn("removed from the list", self.js("document.getElementById('auth-log').textContent"))
        # the role select still works here
        uid2 = accounts.account(username="pg502")["id"]
        sel = f"document.querySelector('#users .item[data-id=\"{uid2}\"] select')"
        self.js(f"{sel}.value = 'viewer'; {sel}.dispatchEvent(new Event('change'))")
        deadline = time.time() + 5
        while accounts.account(uid2)["role"] != "viewer" and time.time() < deadline:
            time.sleep(0.05)
        self.assertEqual(accounts.account(uid2)["role"], "viewer")

    def test_7_sign_in_page_logo_remember_me_and_theme(self):
        self.new_person("pg701")
        self.b.goto(self.base + "/signin")
        self.wait("!document.getElementById('signin').hidden", "form")
        self.addCleanup(lambda: self.js("localStorage.removeItem('pcg-theme')"))     # the browser is shared by the tests
        self.wait("document.querySelector('.brand').complete && document.querySelector('.brand').naturalWidth > 0", "the logo loaded")
        self.assertEqual(self.js("document.querySelector('.brand').alt"), "Virtual Atoms")
        self.assertNotIn("New here", self.js("document.body.textContent"))
        self.assertTrue(self.js("document.getElementById('remember').checked"))
        # light or dark: the button switches, the choice stays after a reload and reaches the app
        start = self.js("pcgTheme.effective()")
        other = "light" if start == "dark" else "dark"
        self.assertEqual(self.js("document.getElementById('theme').getAttribute('aria-label')"), "Light mode" if start == "dark" else "Dark mode")
        self.click("#theme")
        self.assertEqual(self.js("document.documentElement.dataset.theme"), other)
        self.assertEqual(self.js("localStorage.getItem('pcg-theme')"), other)
        self.b.goto(self.base + "/signin")
        self.wait("!document.getElementById('signin').hidden", "form again")
        self.assertEqual(self.js("document.documentElement.dataset.theme"), other)
        self.assertEqual(self.js("getComputedStyle(document.body).backgroundColor"), "rgb(14, 14, 14)" if other == "dark" else "rgb(255, 255, 255)")
        self.assertEqual("invert" in self.js("getComputedStyle(document.querySelector('.brand')).filter"), other == "dark")
        # not remembered: a browser-session cookie
        self.click("#remember")
        self.assertFalse(self.js("document.getElementById('remember').checked"))
        self.sign_in("pg701", "pg701")
        self.wait("location.pathname === '/set-password' && !!document.getElementById('setpw') && !document.getElementById('setpw').hidden", "signed in")
        self.assertEqual(self.js("document.documentElement.dataset.theme"), other)
        c = next(c for c in self.b.call("Network.getCookies")["cookies"] if c["name"] == "pcg_s")
        self.assertTrue(c["session"], c)
        self.assertTrue(c["value"].startswith("v2s."))
        self.js("pcgTheme.set('')")
        self.assertIsNone(self.js("localStorage.getItem('pcg-theme')"))

    def test_8_the_theme_in_account_settings(self):
        self.signed_in_as_leo("/#settings=account")
        self.addCleanup(lambda: self.js("localStorage.removeItem('pcg-theme')"))
        self.wait("!!document.getElementById('acct-theme')", "the account tab")
        self.assertEqual(self.js("document.querySelector('#acct-theme [aria-checked=true]').dataset.v"), "system")
        self.js("document.querySelector('#acct-theme [data-v=dark]').click()")
        self.assertEqual(self.js("document.documentElement.dataset.theme"), "dark")
        self.assertEqual(self.js("document.querySelector('#acct-theme [aria-checked=true]').dataset.v"), "dark")
        self.js("document.querySelector('#acct-theme [data-v=light]').click()")
        self.assertEqual(self.js("document.documentElement.dataset.theme"), "light")
        self.js("document.querySelector('#acct-theme [data-v=system]').click()")
        self.assertIsNone(self.js("document.documentElement.getAttribute('data-theme')"))
        self.assertIsNone(self.js("localStorage.getItem('pcg-theme')"))

    def test_6_phone_tap_targets(self):
        self.new_person("pg601")
        self.b.viewport(390, 844, mobile=True)
        self.b.goto(self.base + "/signin")
        self.wait("!document.getElementById('signin').hidden", "form")
        self.taps("sign-in")
        self.click("#forgot-open")
        self.taps("forgot")
        self.b.goto(self.base + "/signin")
        self.sign_in("pg601", "pg601")
        self.wait("location.pathname === '/set-password' && !document.getElementById('setpw').hidden", "first password")
        self.taps("choose your own password")
        tok = accounts.make_link(self.hub.cfg, accounts.account(username="pg601")["id"], kind="admin")["url"].split("#t=")[1]
        self.b.goto(self.base + "/set-password#t=" + tok)
        self.wait("!document.getElementById('setpw').hidden", "link form")
        self.taps("set a password from a link")
        self.js("document.getElementById('dead').hidden = false")
        self.taps("a used link")
        self.b.call("Network.clearBrowserCookies")
        self.signed_in_as_leo("/#settings=account")
        self.wait("!!document.getElementById('acct-pw-save')", "account tab")
        self.taps("account")
        self.js("location.hash = 'settings=users'")
        self.wait("!!document.getElementById('add-code')", "users tab")
        self.taps("users")
        uid = accounts.account(username="pg601")["id"]
        self.click(f"#users .item[data-id=\"{uid}\"] .icon-btn")
        self.wait("!!document.querySelector('.menu')", "menu")
        self.taps("a person's menu")
        self.js("[...document.querySelectorAll('.menu button')].find(b => b.textContent === 'Remove…').click()")
        self.wait(f"!!document.getElementById('del-{uid}')", "confirm")
        self.taps("the typed-delete step")
        for w in ("document.documentElement", "document.getElementById('win')"):
            self.assertLessEqual(self.js(f"{w}.scrollWidth - {w}.clientWidth"), 0, f"{w} scrolls sideways")


if __name__ == "__main__":
    unittest.main()
