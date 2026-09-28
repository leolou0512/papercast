"""tools/loadcheck.py: runs end to end on a small fake library, every query answers."""
from __future__ import annotations

import io
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path

GROUP = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(GROUP))

from hub import db  # noqa: E402
from tools import loadcheck  # noqa: E402


class TestLoadcheck(unittest.TestCase):
    def tearDown(self):
        db.close()

    def test_small_run(self):
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(loadcheck.main(["--papers", "40", "--repeat", "2"]), 0)
        text = out.getvalue()
        for name, _, _ in loadcheck.QUERIES:
            self.assertIn(name[:58], text)
        self.assertIn("fake library: 40 papers", text)
        self.assertEqual(set(loadcheck.M3_INDEXES), {n for n, _ in db.M3_INDEXES})


if __name__ == "__main__":
    unittest.main()
