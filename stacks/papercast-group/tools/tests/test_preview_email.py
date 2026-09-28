"""The welcome email (hub/email/welcome.html, welcome.txt) through tools/preview_email.py: every
placeholder filled and escaped, the style blocks' braces untouched, the checks passing (size, no
script, alt text, the banner), the Outlook imitation dropping the non-Outlook branch, and the .eml
a multipart/alternative of the same text, the banner by its public URL."""
from __future__ import annotations

import email
import sys
import tempfile
import unittest
from email import policy
from pathlib import Path

GROUP = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(GROUP / "tools"))

import preview_email as pe  # noqa: E402

VALUES = {"name": "Ann <O'Neil>", "username": "a.ganose", "login_email": "a.ganose@ic.ac.uk", "signin_url": "https://papercast.virtualatoms.org/signin?x=1&y=2",
          "site_url": "https://papercast.virtualatoms.org/", "github_url": "https://github.com/leolou0512/papercast"}


class TestWelcomeEmail(unittest.TestCase):
    def setUp(self):
        self.page, self.text = pe.render(VALUES)

    def test_filled_and_escaped(self):
        self.assertIn("Hi, Ann &lt;O&#x27;Neil&gt;", self.page)
        self.assertIn('href="https://papercast.virtualatoms.org/signin?x=1&amp;y=2"', self.page)
        self.assertIn("a.ganose@ic.ac.uk", self.page)
        self.assertIn("Hi, Ann <O'Neil>", self.text)
        self.assertIn("Email:     a.ganose@ic.ac.uk", self.text)
        self.assertIn("Password:  a.ganose", self.text)
        self.assertIn("@media (prefers-color-scheme: dark) {", self.page)   # braces kept

    def test_checks_pass(self):
        for ok, what in pe.checks(self.page, self.text):
            self.assertTrue(ok, what)

    def test_outlook_takes_its_branch(self):
        o = pe.outlook(self.page)
        self.assertNotIn("<style", o)
        self.assertNotIn('class="pc-btn"', o)       # the non-Outlook button is gone
        self.assertIn("#C8431A", o)                  # the VML button's box stands in
        for mode in ("partial", "full"):
            self.assertNotIn('bgcolor="#FFFFFF"', pe.outlook(self.page, mode))

    def test_eml(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(pe.main(["--out", d, "--name", "Maria", "--username", "mg1234"]), 0)
            m = email.message_from_bytes((Path(d) / "welcome-preview.eml").read_bytes(), policy=policy.default)
            self.assertEqual(m["Subject"], "Welcome to Virtual Atoms Lab Papercast")
            self.assertEqual(m.get_content_type(), "multipart/alternative")
            self.assertEqual([p.get_content_type() for p in m.iter_parts()], ["text/plain", "text/html"])
            h = m.get_body(("html",)).get_content()
            self.assertIn("https://papercast.virtualatoms.org/email/welcome-banner.jpg", h)
            self.assertIn("mg1234@ic.ac.uk", m.get_body(("plain",)).get_content())
            self.assertIn("data:image/jpeg;base64,", (Path(d) / "welcome-preview.html").read_text())


if __name__ == "__main__":
    unittest.main()
