"""api.py against the fake hub: the bearer token, messages a person can act on, retries."""
import unittest

from cli_testlib import CliTestCase, free_port

from papercast_cli.api import Api, normalise_server
from papercast_cli.errors import (ApiError, AuthError, Conflict, Forbidden, NetworkError,
                                  NotFound, NotLoggedIn, ServerError)


class ApiTest(CliTestCase):
    def api(self, token="pcg_valid", **kw):
        kw.setdefault("sleep", lambda s: self.waits.append(s))
        return Api(self.hub.url, token, **kw)

    def setUp(self):
        super().setUp()
        self.waits = []

    def test_bearer_token_and_user_agent(self):
        self.assertEqual(self.api().me()["email"], "ada@example.org")
        r = self.hub.requests[-1]
        self.assertEqual(r["token"], "pcg_valid")
        self.assertTrue(r["ua"].startswith("papercast/0.1.0"))

    def test_401_says_run_login(self):
        with self.assertRaises(AuthError) as cm:
            self.api("pcg_revoked").me()
        self.assertIn("papercast login", str(cm.exception))
        self.assertEqual(cm.exception.status, 401)
        self.assertEqual(self.waits, [])                     # not retried

    def test_no_token_is_not_logged_in(self):
        with self.assertRaises(NotLoggedIn):
            Api(self.hub.url).me()
        self.assertEqual(self.hub.requests, [])

    def test_403_disabled(self):
        with self.assertRaises(Forbidden) as cm:
            self.api().get("/api/cli/forbidden")
        self.assertIn("disabled", str(cm.exception))
        self.assertIn("admin", str(cm.exception))

    def test_403_upload_names_the_role(self):
        self.hub.tokens["pcg_viewer"] = {"id": 9, "name": "Vi", "email": "v@x", "role": "viewer"}
        bundle = self.tmp / "b.tar.gz"
        bundle.write_bytes(b"x")
        with self.assertRaises(Forbidden) as cm:
            self.api("pcg_viewer").upload(str(bundle))
        self.assertIn("contributor", str(cm.exception))

    def test_409_in_progress_names_who(self):
        with self.assertRaises(Conflict) as cm:
            self.api().get("/api/cli/conflict")
        self.assertIn("Bob is already making this paper since", str(cm.exception))
        self.assertEqual(cm.exception.error, "in_progress")
        self.assertEqual(cm.exception.body["by"]["name"], "Bob")

    def test_404(self):
        with self.assertRaises(NotFound):
            self.api().get("/api/cli/nothing")

    def test_5xx_is_retried_with_backoff(self):
        self.hub.fail_next = [502, 503]
        self.assertEqual(self.api(backoff=1.0).me()["name"], "Ada")
        self.assertEqual(self.waits, [1.0, 2.0])
        self.assertEqual(self.hub.paths().count("/api/cli/me"), 3)

    def test_5xx_gives_up_after_the_retries(self):
        self.hub.fail_next = [500] * 10
        with self.assertRaises(ServerError) as cm:
            self.api(backoff=1.0).me()
        self.assertIn("5 tries", str(cm.exception))
        self.assertEqual(self.waits, [1.0, 2.0, 4.0, 8.0])

    def test_4xx_is_not_retried(self):
        with self.assertRaises(NotFound):
            self.api().get("/api/cli/nothing")
        self.assertEqual(self.waits, [])

    def test_network_error_is_retried_then_explained(self):
        api = Api(f"http://127.0.0.1:{free_port()}", "pcg_valid", retries=2,
                  sleep=lambda s: self.waits.append(s), backoff=0.5)
        with self.assertRaises(NetworkError) as cm:
            api.me()
        self.assertIn("Cannot reach", str(cm.exception))
        self.assertIn("3 tries", str(cm.exception))
        self.assertEqual(self.waits, [0.5, 1.0])

    def test_a_web_page_instead_of_the_api(self):
        self.hub.html_paths.add("/api/cli/me")
        with self.assertRaises(ApiError) as cm:
            self.api().me()
        self.assertIn("web page", str(cm.exception))

    def test_a_redirect_is_reported_not_followed(self):
        with self.assertRaises(ApiError) as cm:
            self.api().get("/redirect", auth=False)
        self.assertIn("redirected", str(cm.exception))
        self.assertIn("login.example.org", str(cm.exception))

    def test_upload_sends_gzip(self):
        b = self.tmp / "bundle.tar.gz"
        b.write_bytes(b"\x1f\x8bdata")
        r = self.api().upload(str(b))
        self.assertEqual(r["state"], "checking")
        self.assertEqual(self.hub.uploads, [b"\x1f\x8bdata"])

    def test_normalise_server(self):
        self.assertEqual(normalise_server("hub.example.org/"), "https://hub.example.org")
        self.assertEqual(normalise_server("localhost:8480"), "http://localhost:8480")
        self.assertEqual(normalise_server("https://x.trycloudflare.com"), "https://x.trycloudflare.com")
        with self.assertRaises(Exception):
            normalise_server("ftp://x")


if __name__ == "__main__":
    unittest.main()
