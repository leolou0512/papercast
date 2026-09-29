"""tools/theme_contrast.py on the hub's stylesheets: every theme in themes.css reaches WCAG AA on
the pairs the pages draw and sets every colour token Light sets; a theme with a token left out,
or a secondary text too faint, fails."""
from __future__ import annotations

import contextlib
import io
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

GROUP = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(GROUP / "tools"))

import theme_contrast as tc  # noqa: E402

STATIC = GROUP / "hub" / "static"


def run(static: Path) -> tuple[int, str]:
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = tc.main(["--static", str(static)])
    return code, out.getvalue()


class TestThemeContrast(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="pcg-themes-"))
        for p in STATIC.glob("*.css"):
            shutil.copy(p, self.tmp / p.name)
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def edit(self, old_re: str, new: str):
        p = self.tmp / "themes.css"
        s, n = re.subn(old_re, new, p.read_text(), count=1)
        self.assertEqual(n, 1, old_re)
        p.write_text(s)

    def test_the_themes_pass(self):
        code, out = run(STATIC)
        self.assertEqual(code, 0, out)
        got = tc.themes(STATIC)
        self.assertEqual(sorted(got), ["dark", "dimmed", "forest", "latte", "light", "paper"])
        for t in ("paper", "latte", "dimmed", "forest"):
            self.assertRegex(out, rf"(?m)^{t}: \d+ pairs, 0 under AA$")

    def test_a_token_left_out_fails(self):
        self.edit(r"\n  --quote: #CECDC3;[^\n]*", "")
        code, out = run(self.tmp)
        self.assertEqual(code, 1)
        self.assertIn("MISSING --quote", out)

    def test_faint_secondary_text_fails(self):
        self.edit(r"--text-2: #63625E;", "--text-2: #9F9D96;")
        code, out = run(self.tmp)
        self.assertEqual(code, 1)
        self.assertRegex(out, r"FAIL text-2 +on bg ")

    def test_translucent_colours_are_composited(self):
        # the transcript's find match: the accent at 16% over the page, under the text
        t = tc.themes(STATIC)["paper"]
        rows = {(fg, bg): r for fg, bg, r, _ in tc.check(t)}
        hl = tc.over(tc.resolve(t, "hl"), tc.resolve(t, "bg"))
        self.assertAlmostEqual(rows[("text", "hl@bg")], tc.ratio(tc.resolve(t, "text"), hl), places=6)


if __name__ == "__main__":
    unittest.main()
