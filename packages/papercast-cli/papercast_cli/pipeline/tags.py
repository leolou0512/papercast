"""Topic tags and the publication year, copied from Leo's runner (tags.py, INTERFACE 1.10).

Two or three short lowercase topic tags, chosen by the episode agent in claims.md from the tags
already in use in the group's library (so they stay consistent, and graphs built from tag rules
find the paper), cleaned here.
"""
from __future__ import annotations

import collections
import re
import time
import unicodedata

MAX_TAGS = 3
MAX_WORDS = 4
MAX_CHARS = 40
VOCAB_MAX = 200          # tags shown to an agent as "in use", most used first
EXAMPLES = ("reinforcement learning", "offline rl", "diffusion", "guidance", "materials",
            "crystal generation", "interatomic potentials", "video generation")


def clean_tag(t) -> str | None:
    """Lowercase, trimmed, one to four words, at most 40 characters; None if nothing usable."""
    if not isinstance(t, str):
        return None
    s = unicodedata.normalize("NFKC", t).lower().replace("_", " ")
    s = re.sub(r"[^\w\s+./&-]", " ", s)          # quotes, #, brackets, commas, colons ...
    s = " ".join(s.split()).strip(" -./&+")
    if not s or not re.search(r"[a-z]", s) or len(s) > MAX_CHARS or len(s.split()) > MAX_WORDS:
        return None
    return s


def split_inline(v: str) -> list[str]:
    """`[a, "b"]` or `a, b` -> ["a", "b"]."""
    v = v.strip()
    if v.startswith("[") and v.endswith("]"):
        v = v[1:-1]
    return [x.strip().strip("\"'") for x in v.split(",") if x.strip()]


def clean_tags(raw) -> list[str]:
    """At most three distinct cleaned tags, in the order given."""
    if isinstance(raw, str):
        raw = split_inline(raw)
    if not isinstance(raw, (list, tuple)):
        return []
    out: list[str] = []
    for t in raw:
        c = clean_tag(t)
        if c and c not in out:
            out.append(c)
        if len(out) >= MAX_TAGS:
            break
    return out


def _key(t: str) -> str:
    k = re.sub(r"[^a-z0-9]", "", t.lower())
    return k[:-1] if len(k) > 3 and k.endswith("s") else k


def canonical(tags: list[str], vocab) -> list[str]:
    """A tag that differs from one in use only by spacing, hyphens or a plural s becomes that one."""
    by_key = {}
    for v in vocab:
        by_key.setdefault(_key(v), v)
    out: list[str] = []
    for t in tags:
        c = by_key.get(_key(t), t)
        if c not in out:
            out.append(c)
    return out


def clean_year(v) -> int | None:
    """A plausible publication year (1900 to next year), from an int or a string holding one."""
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        y = v
    elif isinstance(v, str):
        m = re.search(r"\b(1[89]\d{2}|20\d{2})\b", v)
        if not m:
            return None
        y = int(m.group(1))
    else:
        return None
    return y if 1900 <= y <= time.gmtime().tm_year + 1 else None


def year_from_arxiv_id(aid: str | None) -> int | None:
    """An arXiv id carries the year and month of its first version: 2401.01234 -> 2024,
    hep-th/9901001 -> 1999."""
    if not aid:
        return None
    m = re.match(r"^(\d{2})(\d{2})\.\d{4,5}", aid) or \
        re.match(r"^[a-z\-]+(?:\.[A-Z]{2})?/(\d{2})(\d{2})\d{3}", aid, re.I)
    if not m or not 1 <= int(m.group(2)) <= 12:
        return None
    yy = int(m.group(1))
    return clean_year(1900 + yy if yy >= 91 else 2000 + yy)


def vocabulary(tag_lists) -> list[str]:
    """Tags in use, most used first (ties alphabetical), at most VOCAB_MAX."""
    c = collections.Counter(t for tl in tag_lists for t in (tl or []) if isinstance(t, str))
    return [t for t, _ in sorted(c.items(), key=lambda kv: (-kv[1], kv[0]))][:VOCAB_MAX]


def in_use_text(vocab: list[str]) -> str:
    return ", ".join(vocab) if vocab else "none yet"
