"""Chunking for the ear, and the guard that keeps digits, symbols and LaTeX from any engine."""
import importlib.util
import os
import re
import unittest

from helpers import SCRIPT, SRC
from papercast_voice import textprep
from papercast_voice.config import DEFAULTS
from papercast_voice.textprep import ScriptInvalid, plan

AUDIO = DEFAULTS["audio"]
RUNNER_CHECK = os.path.join(os.path.dirname(SRC), "runner", "papercast_runner", "script_check.py")


def words(s: str) -> list[str]:
    return re.findall(r"[A-Za-zÀ-ÿ']+", s)


def runner_check():
    if not os.path.exists(RUNNER_CHECK):
        return None
    spec = importlib.util.spec_from_file_location("runner_script_check", RUNNER_CHECK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Chunking(unittest.TestCase):
    def test_nothing_lost_or_added(self):
        chunks = plan(SCRIPT, 20, AUDIO)
        spoken = [w for c in chunks for w in words(c.text)]
        original = [w for line in SCRIPT.splitlines() for w in words(line.lstrip("#"))]
        self.assertEqual(spoken, original)

    def test_chunks_end_at_sentence_ends_and_respect_the_limit(self):
        for c in plan(SCRIPT, 20, AUDIO):
            self.assertLessEqual(c.words, 20, c.text)
            self.assertRegex(c.text, r"[.!?…:,;]$", c.text)
        # with a roomy limit no sentence is cut at all
        for c in plan(SCRIPT, 60, AUDIO):
            self.assertRegex(c.text, r"[.!?…]$", c.text)

    def test_long_sentence_cut_at_clauses_then_words(self):
        clauses = ", ".join(" ".join(["word"] * 9) for _ in range(8)) + "."
        pieces = textprep.split_long(clauses, 20)
        self.assertTrue(all(len(p.split()) <= 20 for p in pieces))
        self.assertTrue(all(p.endswith((",", ".")) for p in pieces), pieces)
        run_on = " ".join(["word"] * 70) + "."
        pieces = textprep.split_long(run_on, 20)
        self.assertEqual(sum(len(p.split()) for p in pieces), 70)
        self.assertTrue(all(len(p.split()) <= 20 for p in pieces))
        self.assertLessEqual(max(len(p.split()) for p in pieces) - min(len(p.split()) for p in pieces), 1)

    def test_abbreviations_and_initials_do_not_end_sentences(self):
        s = "We follow Lipman et al. and the model of J. Smith closely. Then it stops."
        self.assertEqual(textprep.split_sentences(s),
                         ["We follow Lipman et al. and the model of J. Smith closely.", "Then it stops."])

    def test_headings_and_pauses(self):
        chunks = plan(SCRIPT, 60, AUDIO)
        self.assertEqual(chunks[0].kind, "heading")
        self.assertEqual(chunks[0].text, "How guidance steers the model.")
        self.assertEqual(chunks[0].gap_after_s, AUDIO["gap_after_heading_s"])
        heading2 = next(i for i, c in enumerate(chunks) if i and c.kind == "heading")
        self.assertEqual(chunks[heading2 - 1].gap_after_s, AUDIO["gap_before_heading_s"])
        self.assertEqual(chunks[1].gap_after_s, AUDIO["gap_paragraph_s"])
        self.assertEqual(chunks[-1].gap_after_s, AUDIO["tail_s"])
        small = plan(SCRIPT, 12, AUDIO)
        same_para = [c for i, c in enumerate(small[:-1]) if small[i + 1].block == c.block]
        self.assertTrue(same_para)
        self.assertTrue(all(c.gap_after_s == AUDIO["gap_sentence_s"] for c in same_para))

    def test_headings_can_be_silent(self):
        chunks = plan(SCRIPT, 60, {**AUDIO, "speak_headings": False})
        self.assertTrue(all(c.kind == "para" for c in chunks))
        self.assertEqual(chunks[1].gap_after_s, AUDIO["gap_before_heading_s"])


class Guard(unittest.TestCase):
    BAD = {
        "ascii digit": "It scored 9 points.",
        "superscript": "Energy scales as n² here.",
        "arabic-indic digit": "It scored ٣ points.",
        "latex command": r"The loss is \frac{a}{b} overall.",
        "inline math": "The value $x$ grows.",
        "greek letter": "Set α to one.",
        "equals": "Then a = b holds.",
        "plus": "Add a + b now.",
        "times": "Two × three.",
        "less-equal": "Keep it ≤ one.",
        "percent": "About half, or fifty %.",
        "emphasis": "This is *important* stuff.",
        "underscore": "Call my_function now.",
        "link": "See [the paper](here).",
        "slash": "Use and/or here.",
    }
    BAD_MD = {"list": "- one item\n", "numbered": "One.\n\n1. first\n", "quote": "> quoted\n",
              "table": "| a | b |\n", "fence": "```\ncode\n```\n", "empty heading": "#\n\nText."}
    GOOD = ["Schrödinger and Gödel were naïve about it — or so it seems.",
            "“Quoted,” she said; ‘inner’ quotes… and (an aside), too!",
            "It's the model's job: nothing else. Isn't it?"]

    def test_rejected_everywhere(self):
        for name, text in {**self.BAD, **self.BAD_MD}.items():
            with self.subTest(name):
                with self.assertRaises(ScriptInvalid):
                    plan(text, 20, AUDIO)
        for name, text in self.BAD.items():
            with self.subTest("boundary " + name):
                with self.assertRaises(ScriptInvalid):
                    textprep.assert_speakable(text)

    def test_accepted(self):
        for text in self.GOOD:
            with self.subTest(text):
                chunks = plan(text, 20, AUDIO)
                self.assertTrue(chunks)
                for c in chunks:
                    textprep.assert_speakable(c.text)

    def test_empty_script_refused(self):
        for s in ("", "\n\n", "# \n"):
            with self.assertRaises(ScriptInvalid):
                plan(s, 20, AUDIO)


class AgreesWithRunner(unittest.TestCase):
    """The runner validates first and the voice may trust it (INTERFACE §6). If the two
    character policies drift apart, a script could pass the runner and fail here, late."""

    def setUp(self):
        self.rc = runner_check()
        if self.rc is None:
            self.skipTest("runner's script_check.py not in this checkout")

    def test_same_punctuation_set(self):
        self.assertEqual(set(self.rc.ALLOWED_PUNCT), set(textprep.ALLOWED_PUNCT))

    def test_same_verdicts(self):
        cases = list(Guard.BAD.values()) + Guard.GOOD
        for text in cases:
            with self.subTest(text):
                r = self.rc.check(text, 150, 0, 1000)
                runner_bad = any(("digit" in p or "LaTeX" in p or "symbols" in p) for p in r["problems"])
                self.assertEqual(runner_bad, bool(textprep.problems(text)))


if __name__ == "__main__":
    unittest.main()
