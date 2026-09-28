"""deploy/cloudflare.sh with a stand-in install: $PCG_HOME in a temporary directory whose app/
is this checkout and whose venv/bin/python is the Python running the tests, and fakes of
systemctl and curl on PATH (they log what they were asked; curl answers as a hub would: 303 for
GET / without a session, 200 for /signin). The hub itself is not started; its database is real
(python3 -m hub.auth runs against it). Run:
    cd stacks/papercast-group && python3 -m unittest discover -s deploy/tests"""
from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEPLOY = HERE.parent
GROUP = DEPLOY.parent                                   # stacks/papercast-group
SCRIPT = DEPLOY / "cloudflare.sh"

SYSTEMCTL = """#!/bin/sh
echo "systemctl $*" >> "$FAKE_LOG"
case "$*" in *is-active*) echo active ;; esac
exit 0
"""
CURL = """#!/bin/sh
for a; do url=$a; done
echo "curl $url" >> "$FAKE_LOG"
case "$url" in
  */signin) printf 200 ;;
  *cloudflareaccess.com*) printf 200 ;;
  *) printf "${FAKE_ROOT_CODE:-303}" ;;
esac
"""


class CloudflareScript(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="pcg-cf-test-")
        self.h = Path(self.tmp.name) / "papercast-group"
        (self.h / "app").mkdir(parents=True)
        (self.h / "app" / "papercast-group").symlink_to(GROUP)
        (self.h / "venv" / "bin").mkdir(parents=True)
        (self.h / "venv" / "bin" / "python").symlink_to(sys.executable)
        (self.h / "data").mkdir()
        self.bin = Path(self.tmp.name) / "bin"
        self.bin.mkdir()
        for name, body in (("systemctl", SYSTEMCTL), ("curl", CURL)):
            p = self.bin / name
            p.write_text(body)
            p.chmod(0o755)
        self.log = Path(self.tmp.name) / "calls.log"
        self.env_file = self.h / "hub.env"
        self.env_file.write_text(f"PCG_DATA={self.h / 'data'}\nPCG_SECRET=0123456789abcdef0123456789\nPCG_AUTH=local\n"
                                 "PCG_BIND=127.0.0.1\nPCG_PORT=8480\nPCG_PUBLIC_URL=http://127.0.0.1:8480\nPCG_ADMIN_EMAILS=\n")
        self.env_file.chmod(0o600)

    def tearDown(self):
        self.tmp.cleanup()

    def run_sh(self, *args, code="303"):
        env = {**os.environ, "PCG_MODE": "user", "PCG_HOME": str(self.h), "PATH": f"{self.bin}:{os.environ['PATH']}", "FAKE_LOG": str(self.log),
               "FAKE_ROOT_CODE": code, "HOME": self.tmp.name, "PYTHONPATH": str(GROUP) + os.pathsep + str(GROUP.parents[1] / "packages" / "papercast-cli")}
        return subprocess.run(["bash", str(SCRIPT), *args], env=env, capture_output=True, text=True, timeout=120)

    def env(self) -> dict:
        return dict(ln.split("=", 1) for ln in self.env_file.read_text().splitlines() if "=" in ln)

    def db(self, sql):
        c = sqlite3.connect(str(self.h / "data" / "hub.db"))
        try:
            return c.execute(sql).fetchall()
        finally:
            c.close()

    def calls(self) -> str:
        return self.log.read_text() if self.log.exists() else ""

    def test_auth_password_puts_the_two_admins_on_the_list(self):
        p = self.run_sh("auth", "password")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(self.env()["PCG_AUTH"], "password")
        self.assertEqual(sorted(self.db("SELECT u.email, u.username, u.role, u.disabled FROM users u JOIN allowed_emails a ON a.email = u.email")),
                         [("a.ganose@ic.ac.uk", "a.ganose", "admin", 0), ("yl6719@ic.ac.uk", "yl6719", "admin", 0)])
        self.assertIn("yl6719@ic.ac.uk is an admin, on the list (username yl6719).", p.stdout)
        self.assertIn("with the first password a.ganose", p.stdout)
        self.assertEqual(p.stdout.count("/set-password#t="), 2)
        self.assertIn("not https: cookies are not Secure", p.stdout)
        self.assertIn("no email set up yet", p.stdout)
        self.assertIn("systemctl --user restart pcg-hub.service", self.calls())
        self.assertEqual(oct(self.env_file.stat().st_mode & 0o777), "0o600")
        # again: nothing doubles, the passwords stay
        self.db("SELECT 1")
        p = self.run_sh("auth", "password")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(self.db("SELECT COUNT(*) FROM allowed_emails")[0][0], 2)
        self.assertEqual(self.env_file.read_text().count("PCG_AUTH="), 1)

    def test_auth_password_with_other_admins_and_a_hub_that_does_not_redirect(self):
        p = self.run_sh("auth", "password", "--admin", "zz123@ic.ac.uk")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(self.db("SELECT email FROM allowed_emails"), [("zz123@ic.ac.uk",)])
        p = self.run_sh("auth", "password", "--admin", "someone@gmail.com")
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("Only Imperial addresses", p.stderr)
        p = self.run_sh("auth", "password", "--admin", "zz123@ic.ac.uk", code="401")
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("expected 303", p.stderr)

    def test_url(self):
        p = self.run_sh("url", "--url", "https://papercast.virtualatoms.org/")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(self.env()["PCG_PUBLIC_URL"], "https://papercast.virtualatoms.org")
        self.assertIn("restart pcg-hub.service", self.calls())
        self.assertIn("public address https://papercast.virtualatoms.org", p.stdout)
        p = self.run_sh("url", "--url", "http://papercast.virtualatoms.org")
        self.assertNotEqual(p.returncode, 0)
        self.assertEqual(self.env()["PCG_PUBLIC_URL"], "https://papercast.virtualatoms.org")
        self.assertNotEqual(self.run_sh("url").returncode, 0)

    def test_auth_local_and_the_old_login_is_gone(self):
        self.run_sh("auth", "password")
        p = self.run_sh("auth", "local")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(self.env()["PCG_AUTH"], "local")
        p = self.run_sh("login", "--team", "t", "--aud", "a", "--url", "https://x")
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("usage", p.stderr)
        self.assertNotEqual(self.run_sh("auth", "google").returncode, 0)

    def test_email(self):
        p = self.run_sh("email", "--host", "smtp.gmail.com", "--user", "papercast.group@gmail.com", "--from", "papercast.group@gmail.com")
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("no password at", p.stderr)
        pw = self.h / "smtp.password"
        pw.write_text("wxqz vyuk tsrp onml\n")
        pw.chmod(0o644)
        for bad in ("papercast <x@gmail.com>", "x y@gmail.com", "nobody", "a@b@c"):
            p = self.run_sh("email", "--host", "smtp.gmail.com", "--user", "u@gmail.com", "--from", bad)
            self.assertNotEqual(p.returncode, 0, bad)
        self.assertNotEqual(self.run_sh("email", "--host", "h", "--from", "u@gmail.com", "--port", "25").returncode, 0)
        p = self.run_sh("email", "--host", "smtp.gmail.com", "--port", "465", "--user", "papercast.group@gmail.com",
                        "--from", "papercast.group@gmail.com")
        self.assertEqual(p.returncode, 0, p.stderr)
        e = self.env()
        self.assertEqual((e["PCG_SMTP_HOST"], e["PCG_SMTP_PORT"], e["PCG_SMTP_USER"], e["PCG_SMTP_PASSWORD_FILE"], e["PCG_SMTP_FROM"]),
                         ("smtp.gmail.com", "465", "papercast.group@gmail.com", str(pw), "papercast.group@gmail.com"))
        self.assertEqual(oct(pw.stat().st_mode & 0o777), "0o600")
        self.assertNotIn("wxqz", self.env_file.read_text())      # the password stays in its own file
        p = self.run_sh("status")
        self.assertIn("PCG_SMTP_HOST=smtp.gmail.com", p.stdout)
        self.assertNotIn("PASSWORD", p.stdout)
        self.assertIn("pcg-hub     active", p.stdout)


if __name__ == "__main__":
    unittest.main()
