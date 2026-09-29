"""usage.py: a paper's share of the five-hour limit, from the jobs' own Claude logs."""
import json
import os
import tempfile
import unittest
from pathlib import Path

from papercast_cli import usage


def rl(five, reset=1_790_652_000, week=0.23):
    return json.dumps({"type": "rate_limit_event", "rate_limit_info": {"unifiedWindows": {
        "five_hour": {"utilization": five, "resetsAt": reset}, "seven_day": {"utilization": week, "resetsAt": 1}}}})


def result(cost):
    return json.dumps({"type": "result", "total_cost_usd": cost})


class UsageTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="pcg-usage-"))

    def job(self, name, runs, t):
        d = self.root / name / "logs"
        d.mkdir(parents=True)
        for i, lines in enumerate(runs, 1):
            f = d / f"run-{i}.jsonl"
            f.write_text("\n".join(lines) + "\n")
            os.utime(f, (t + i, t + i))
        os.utime(self.root / name, (t, t))

    def test_share_per_paper_and_the_window_now(self):
        # three papers of $2 each; the window rose 0.10 -> 0.16 over $6: 1 point per $, 2 points each
        self.job("ja", [[rl(0.10), result(0.1)], [rl(0.11), result(1.9)]], 1000)
        self.job("jb", [[rl(0.12), result(2.0)]], 2000)
        self.job("jc", [[rl(0.14), result(1.5)], [rl(0.16), "not json", result(0.5)]], 3000)
        self.job("jd", [[rl(0.16), result(0.05)]], 4000)          # stopped before writing: not a paper
        e = usage.estimate(self.root)
        self.assertAlmostEqual(e["per_paper"], 2.0 * (0.06 / 6.05), places=4)
        self.assertEqual((e["now"], e["resets"], e["week"], e["papers"]), (0.16, 1_790_652_000, 0.23, 3))

    def test_too_little_history_says_nothing(self):
        self.assertIsNone(usage.estimate(self.root))
        self.job("ja", [[rl(0.10), result(2.0)], [rl(0.11), result(0.1)]], 1000)   # a 1-point rise
        self.assertIsNone(usage.estimate(self.root))
        self.assertIsNone(usage.estimate(self.root / "missing"))


if __name__ == "__main__":
    unittest.main()
