"""login.py: the device flow of SPEC section 2 against a fake hub, and logout revoking."""
import stat
import unittest

from cli_testlib import USER, CliTestCase, free_port

from papercast_cli import config, login
from papercast_cli.errors import PapercastError

PENDING = (428, {"error": "pending"})
APPROVED = (200, {"token": "pcg_NEWTOKEN234567ABCDEFGHIJKLMNOPQ", "user": dict(USER)})


class LoginTest(CliTestCase):
    def setUp(self):
        super().setUp()
        self.out = []
        self.slept = []

    def run_login(self, **kw):
        kw.setdefault("out", self.out.append)
        kw.setdefault("sleep", self.slept.append)
        return login.login(self.hub.url, **kw)

    def test_pending_then_approved_saves_the_token(self):
        self.hub.poll_script = [PENDING, PENDING, APPROVED]
        user = self.run_login(device="ada@laptop")
        self.assertEqual(user["email"], "ada@example.org")
        cfg = config.load()
        self.assertEqual(cfg["token"], APPROVED[1]["token"])
        self.assertEqual(cfg["server"], self.hub.url)
        self.assertEqual(cfg["device"], "ada@laptop")
        self.assertEqual(stat.S_IMODE(config.config_path().stat().st_mode), 0o600)
        start = self.hub.requests[0]
        self.assertEqual(start["path"], "/api/cli/login/start")
        self.assertEqual(start["body"]["device"], "ada@laptop")
        self.assertEqual(self.hub.paths().count("/api/cli/login/poll"), 3)
        text = "\n".join(self.out)
        self.assertIn("ABCD-EFGH", text)
        self.assertIn(f"{self.hub.url}/cli?code=ABCD-EFGH", text)
        self.assertIn("Logged in as Ada (contributor)", text)

    def test_expired(self):
        self.hub.poll_script = [PENDING, (410, {"error": "expired"})]
        with self.assertRaises(PapercastError) as cm:
            self.run_login()
        self.assertIn("expired", str(cm.exception))
        self.assertIn("papercast login", str(cm.exception))
        self.assertNotIn("token", config.load())

    def test_denied(self):
        self.hub.poll_script = [PENDING, (403, {"error": "denied"})]
        with self.assertRaises(PapercastError) as cm:
            self.run_login()
        self.assertIn("denied", str(cm.exception))
        self.assertNotIn("token", config.load())

    def test_expires_locally_when_never_approved(self):
        self.hub.poll_script = [PENDING]
        self.hub.expires_in = 5
        t = [1000.0]

        def sleep(s):
            t[0] += 2

        with self.assertRaises(PapercastError) as cm:
            self.run_login(sleep=sleep, clock=lambda: t[0])
        self.assertIn("expired", str(cm.exception))
        self.assertLessEqual(self.hub.paths().count("/api/cli/login/poll"), 4)

    def test_a_viewer_is_told_about_the_role(self):
        self.hub.poll_script = [(200, {"token": "pcg_V", "user": {**USER, "role": "viewer"}})]
        self.run_login()
        self.assertIn("contributor", "\n".join(self.out))

    def test_logging_in_again_revokes_the_old_token(self):
        self.login_config(token="pcg_valid")
        self.hub.poll_script = [APPROVED]
        self.run_login()
        self.assertEqual(self.hub.revoked, ["pcg_valid"])
        self.assertEqual(config.load()["token"], APPROVED[1]["token"])

    def test_no_server_anywhere(self):
        with self.assertRaises(PapercastError) as cm:
            login.login(None, out=self.out.append)
        self.assertIn("--server", str(cm.exception))


class LogoutTest(CliTestCase):
    def test_logout_revokes_on_the_hub(self):
        self.login_config(token="pcg_valid")
        out = []
        self.assertTrue(login.logout(out=out.append))
        self.assertEqual(self.hub.revoked, ["pcg_valid"])
        self.assertEqual(self.hub.requests[-1]["path"], "/api/cli/logout")
        cfg = config.load()
        self.assertNotIn("token", cfg)
        self.assertEqual(cfg["server"], self.hub.url)      # kept for the next login
        self.assertIn("revoked", out[0])

    def test_logout_when_the_hub_is_unreachable(self):
        self.login_config(token="pcg_valid", server=f"http://127.0.0.1:{free_port()}")
        out = []
        self.assertFalse(login.logout(out=out.append))
        self.assertNotIn("token", config.load())
        self.assertIn("Devices", out[0])

    def test_logout_of_an_already_revoked_token(self):
        self.login_config(token="pcg_gone")
        out = []
        self.assertTrue(login.logout(out=out.append))
        self.assertNotIn("token", config.load())

    def test_logout_when_not_logged_in(self):
        out = []
        login.logout(out=out.append)
        self.assertEqual(out, ["Not logged in."])

    def test_cli_logout_then_whoami(self):
        self.login_config()
        r = self.run_cli("logout")
        self.assertEqual(r.returncode, 0, r.stderr)
        r = self.run_cli("whoami")
        self.assertEqual(r.returncode, 1)
        self.assertIn("papercast login", r.stderr)


if __name__ == "__main__":
    unittest.main()
