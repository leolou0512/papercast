"""Wording that is never wanted, for script.md and the explainer's points and captions
(INTERFACE 1.8.1). The phrase lists live in `wording.json` next to this file, so they can grow
without a code change; its "about" says the phrase syntax.

Only hard refusals: the listener's name, narrating what the listener knows, has done or keeps in
notes, talk about the episode or the narrator's plan, a paper's publication status, and stock AI
phrases that are never needed. Anything that is sometimes right is the cut-only pass's judgement
(cut.py), never a count here.
"""
from __future__ import annotations

import json
import os
import re

PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "wording.json")
_APOS = "['’]"


def _compile(phrases: list[str], case_sensitive: bool) -> re.Pattern:
    flags = 0 if case_sensitive else re.I
    alts = []
    for ph in sorted(phrases, key=len, reverse=True):     # longest first: "as you know"
        toks = ph.split()
        rx = []
        for i, tok in enumerate(toks):
            if tok == "...":
                rx.append(r"(?:\s+[\w'’-]+){0,2}")
                continue
            star = tok.endswith("*")
            t = tok.rstrip("*")
            body = "".join(_APOS if c in "'’" else re.escape(c) for c in t)
            body += r"[\w'’-]*" if star else ""
            rx.append((r"\s+" if i and rx else "") + body)
        alts.append("".join(rx))
    return re.compile(r"(?<![\w'’-])(?:" + "|".join(alts) + r")(?![\w-])", flags)


def load(path: str = PATH) -> list[dict]:
    """The classes, each with its compiled `rx`. Read at every call site's first use; a broken
    data file raises, so a bad edit fails the tests rather than silently checking nothing."""
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    out = []
    for c in data["classes"]:
        if not c.get("phrases"):
            raise ValueError(f"wording.json: class {c.get('id')!r} has no phrases")
        c = dict(c)
        c["rx"] = _compile(c["phrases"], bool(c.get("case_sensitive")))
        out.append(c)
    return out


_CLASSES: list[dict] | None = None


def classes() -> list[dict]:
    global _CLASSES
    if _CLASSES is None:
        _CLASSES = load()
    return _CLASSES


def hits(text: str, where: str) -> list[tuple[dict, list[str]]]:
    """[(class, the distinct matched words)] for each class that applies to `where` ("script"
    or "explainer") and matches `text`."""
    out = []
    for c in classes():
        if where not in c["in"]:
            continue
        found: list[str] = []
        for m in c["rx"].finditer(text):
            w = " ".join(m.group(0).split())
            if w not in found:
                found.append(w)
        if found:
            out.append((c, found))
    return out


def quoted(words: list[str]) -> str:
    return ", ".join(f'"{w}"' for w in words)
