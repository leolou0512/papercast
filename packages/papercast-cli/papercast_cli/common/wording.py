"""Wording that is never wanted, for script.md and the explainer's points and captions
(SPEC.md sections 6 and 10; Leo's runner, INTERFACE 1.8.1). The phrase lists live in
`wording.json` next to this file, so they can grow without a code change; its "about" says the
phrase syntax. The hub stores a copy with each base prompt version and serves it to the CLI
(`data` below), so the CLI and the hub check against the same list.

Only hard refusals: the listener's name, narrating what the listener knows, has done or keeps in
notes, talk about the episode or the narrator's plan, a paper's publication status, and stock AI
phrases that are never needed. Anything that is sometimes right is the cut-only pass's judgement
(cut.py), never a count here.

The listener is the person the episode is made for, the uploader. Leo's list named him; here the
class `listener_name` takes the uploader's name at check time (`listener_name=`). Without a name
it checks only the phrases in the file (none in the group's), so calls without one still work.
"""
from __future__ import annotations

import json
import os
import re

PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "wording.json")
NAME_CLASS = "listener_name"
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


def name_phrases(name) -> list[str]:
    """The listener's name as a narrator would say it: the whole name and its first word,
    capitalised ("alice.smith", "alice smith" or "Alice Smith" -> ["Alice Smith", "Alice"]).
    Only the part before an @ counts (a new user's name is their email's local part); digits,
    dots and underscores separate words; one-letter words are dropped."""
    if not isinstance(name, str):
        return []
    words, cur = [], ""
    for ch in name.split("@", 1)[0] + " ":
        if ch.isalpha() or (cur and ch in "'’-"):
            cur += ch
            continue
        w = cur.strip("'’-")
        if sum(c.isalpha() for c in w) >= 2:
            if w.islower() or w.isupper():
                w = "-".join(p[:1].upper() + p[1:] for p in w.lower().split("-"))
            words.append(w)
        cur = ""
    out = []
    for ph in ([" ".join(words)] if len(words) > 1 else []) + words[:1]:
        if ph not in out:
            out.append(ph)
    return out


def read(path: str = PATH) -> dict:
    """The data file as it is (the hub stores it with base prompt version 1)."""
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def build(data: dict, listener_name: str | None = None) -> list[dict]:
    """The classes of a wording data object, each with its compiled `rx`; the listener's name is
    added to the listener_name class. A class with no phrases is an error, except that one when
    no name is given: it is then left out. Raises ValueError on a broken object, so a bad edit
    fails the tests (or the hub's base prompt save) rather than silently checking nothing."""
    if not isinstance(data, dict) or not isinstance(data.get("classes"), list):
        raise ValueError('wording: must be an object with a "classes" list')
    out = []
    for c in data["classes"]:
        if not isinstance(c, dict):
            raise ValueError("wording: every class must be an object")
        cid = c.get("id")
        for k in ("id", "wrong", "fix"):
            if not isinstance(c.get(k), str) or not c[k].strip():
                raise ValueError(f"wording.json: class {cid!r} needs a non-empty {k!r}")
        where = c.get("in")
        if not isinstance(where, list) or not where or not set(where) <= {"script", "explainer"}:
            raise ValueError(f'wording.json: class {cid!r}: "in" must list script and/or explainer')
        phrases = c.get("phrases")
        if not isinstance(phrases, list) or not all(isinstance(p, str) and p.strip() for p in phrases):
            raise ValueError(f"wording.json: class {cid!r}: phrases must be non-empty strings")
        if cid == NAME_CLASS:
            phrases = phrases + [p for p in name_phrases(listener_name) if p not in phrases]
            if not phrases:
                continue
        elif not phrases:
            raise ValueError(f"wording.json: class {cid!r} has no phrases")
        c = dict(c, phrases=phrases)
        c["rx"] = _compile(phrases, bool(c.get("case_sensitive")))
        out.append(c)
    return out


def load(path: str = PATH, listener_name: str | None = None) -> list[dict]:
    """The file's classes, each with its compiled `rx` (see build)."""
    return build(read(path), listener_name)


_CACHE: dict = {}


def classes(listener_name: str | None = None, data: dict | None = None) -> list[dict]:
    """The classes for this listener, from `data` (a base prompt version's wording) or the file.
    Compiled once per name and data."""
    key = (listener_name or "", None if data is None else json.dumps(data, sort_keys=True))
    got = _CACHE.get(key)
    if got is None:
        if len(_CACHE) > 64:
            _CACHE.clear()
        got = _CACHE[key] = build(read() if data is None else data, listener_name)
    return got


def hits(text: str, where: str, listener_name: str | None = None,
         data: dict | None = None) -> list[tuple[dict, list[str]]]:
    """[(class, the distinct matched words)] for each class that applies to `where` ("script"
    or "explainer") and matches `text`."""
    out = []
    for c in classes(listener_name, data):
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


def find(text: str, where: str = "script", listener_name: str | None = None,
         data: dict | None = None) -> list[str]:
    """Plain words for each class `text` breaks: 'names the listener ("Alice")'; [] when clean."""
    return [f"{c['wrong']} ({quoted(found)})" for c, found in hits(text, where, listener_name, data)]


def quoted(words: list[str]) -> str:
    return ", ".join(f'"{w}"' for w in words)
