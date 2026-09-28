"""Validate script.md before it goes to the voice (INTERFACE.md §6, spec §2.4).

Paragraphs separated by blank lines and `#` heading lines only; no other Markdown; no digits;
no LaTeX or math symbols; 15-25 minutes at PAPERCAST_WPM. The speech engine may trust a script
that passed, so this errs on the side of refusing.

Since INTERFACE 1.8.1 also wording that is never wanted (wording.py, lists in wording.json): the
listener's name, narrating what the listener knows, has done or keeps in notes, talk about the
episode or the narrator's plan, the paper's publication status, stock AI phrases. Each problem
quotes every offending sentence and says how to fix it, for the one repair turn.
"""
from __future__ import annotations

import re
import unicodedata

from . import wording

# Characters a spoken script may contain besides letters and whitespace.
ALLOWED_PUNCT = set(".,;:!?'\"-()–—‘’“”…")
MARKDOWN_LINE = re.compile(r"^\s*([-*+>|]|```|~~~|\d+[.)])\s")
# A sentence ends at . ! ? or … followed by space and a capital (after any closing quote).
_SENTENCE_END = re.compile(r"(?<=[.!?…])[\"”’)]*\s+(?=[\"“‘(]?[A-Z])")


def sentences(text: str) -> list[str]:
    """The script's sentences in order, whitespace collapsed; a heading line is one sentence."""
    out: list[str] = []
    for para in re.split(r"\n\s*\n", text):
        lines = []
        for line in para.splitlines():
            s = line.strip()
            if s.startswith("#"):
                if lines:
                    out += _split(" ".join(lines))
                    lines = []
                if s.lstrip("#").strip():
                    out.append(" ".join(s.lstrip("#").split()))
            elif s:
                lines.append(s)
        if lines:
            out += _split(" ".join(lines))
    return out


def _split(para: str) -> list[str]:
    return [" ".join(x.split()) for x in _SENTENCE_END.split(para) if x.strip()]


def wording_problems(text: str) -> tuple[list[str], list[str]]:
    """(problems, the classes broken): one problem per class of never-wanted wording, quoting
    every sentence that has it and saying how to fix it."""
    per: dict[str, list[str]] = {}
    order: list[dict] = []
    for sent in sentences(text):
        for c, found in wording.hits(sent, "script"):
            if c["id"] not in per:
                per[c["id"]] = []
                order.append(c)
            per[c["id"]].append(f'"{sent}" ({wording.quoted(found)})')
    problems = []
    for c in order:
        items = per[c["id"]]
        listed = "; ".join(f"({i}) {x}" for i, x in enumerate(items, 1))
        n = len(items)
        problems.append(f"{c['wrong']}, in {n} sentence{'s' if n > 1 else ''}: {listed}. "
                        + c["fix"].rstrip("."))
    return problems, [c["wrong"] for c in order]


def check(text: str, wpm: float, min_min: float, max_min: float) -> dict:
    problems: list[str] = []
    bad_chars: dict[str, int] = {}
    digits = 0
    md_lines = []
    words = 0
    for n, line in enumerate(text.splitlines(), 1):
        s = line.strip()
        if not s:
            continue
        if s.startswith("#"):
            body = s.lstrip("#").strip()
            if not body:
                md_lines.append(n)
                continue
        else:
            body = s
            if MARKDOWN_LINE.match(line):
                md_lines.append(n)
        words += len(body.split())
        for ch in body:
            cat = unicodedata.category(ch)
            if cat == "Nd" or cat in ("No", "Nl"):
                digits += 1
            elif ch.isspace() or ch in ALLOWED_PUNCT:
                continue
            elif cat.startswith("L"):
                # Greek letters are symbols in a paper (alpha, sigma): they must be spelt out.
                if "Ͱ" <= ch <= "Ͽ":
                    bad_chars[ch] = bad_chars.get(ch, 0) + 1
            elif cat.startswith("M"):
                continue            # combining accents on names
            else:
                bad_chars[ch] = bad_chars.get(ch, 0) + 1
    if digits:
        problems.append(f"{digits} digit(s): numbers must be written as words")
    if "\\" in text or re.search(r"\$[^$]+\$", text):
        problems.append("LaTeX found: equations must be spoken in words")
    if bad_chars:
        shown = " ".join(repr(c) for c in sorted(bad_chars, key=lambda c: -bad_chars[c])[:12])
        problems.append(f"symbols that cannot be spoken: {shown}")
    if md_lines:
        problems.append(f"Markdown other than headings and paragraphs on line(s) "
                        f"{', '.join(map(str, md_lines[:10]))}")
    lo, hi = int(min_min * wpm), int(max_min * wpm)
    minutes = words / wpm if wpm else 0
    if words < lo:
        problems.append(f"{words} words is about {minutes:.0f} minutes; needs {lo}-{hi} words "
                        f"({min_min:.0f}-{max_min:.0f} minutes at {wpm:.0f} words per minute)")
    elif words > hi:
        problems.append(f"{words} words is about {minutes:.0f} minutes; needs {lo}-{hi} words "
                        f"({min_min:.0f}-{max_min:.0f} minutes at {wpm:.0f} words per minute)")
    said, broken = wording_problems(text)
    problems += said
    # A plain one-line failure message naming what is still wrong (core may show it instead of
    # its generic one; the detail is the problems).
    message = None
    if broken:
        message = ("The script still " + (broken[0] if len(broken) == 1 else
                                          ", ".join(broken[:-1]) + " and " + broken[-1]) +
                   " after one repair turn.")
    return {"ok": not problems, "problems": problems, "words": words,
            "minutes": round(minutes, 1), "wording": broken, "message": message}


# ---- the hub's checks on an uploaded episode (SPEC.md section 6); A9 refines these.

def explainer_problems(obj) -> list[str]:
    """What is wrong with an explainer.json object: [] when it is fine."""
    out = []
    if not isinstance(obj, dict):
        return ["explainer.json is not a JSON object"]
    pts = obj.get("points")
    if not isinstance(pts, list) or not pts or not all(isinstance(p, str) and p.strip() for p in pts):
        out.append("points: one or more non-empty strings")
    figs = obj.get("figures", [])
    if not isinstance(figs, list):
        out.append("figures: a list")
    else:
        for i, f in enumerate(figs):
            if not isinstance(f, dict) or not isinstance(f.get("caption"), str):
                out.append(f"figures[{i}]: an object with a caption")
            elif ("crop" in f) == ("svg" in f):
                out.append(f"figures[{i}]: exactly one of crop or svg")
    return out
