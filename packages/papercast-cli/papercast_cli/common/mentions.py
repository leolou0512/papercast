"""Does one paper's text mention another paper? Shared by the hub (GET /api/cli/mentions: which
library papers mention a new one) and the CLI (pipeline/links.py: which library papers the new
paper mentions), so both directions match the same way. Deterministic, stdlib only.

A paper is mentioned by its arXiv id (any version, "arXiv:2210.02747v2"), its DOI (any case, a
line break after a dot or slash allowed) or its title. A title is matched on its first
KEY_WORDS significant words (so a trailing subtitle, or a reference cut short, still matches),
with case, accents, ligatures, punctuation, line breaks and hyphenation taken out on both sides:
both are squashed to their letters and digits ("Score-\\nbased gen-\\nerative" and "Score-Based
Generative" are the same). A title with fewer than MIN_SIG significant words, fewer than
MIN_CHARS letters and digits, or made only of common words ("Deep Neural Network Models") is
never matched: in a reference list it would match too much. A title that is only found inside a
longer known title ("Improved <title>") is not a mention of it."""
from __future__ import annotations

import bisect
import re
import unicodedata

KEY_WORDS = 8          # a title is matched on its first 8 significant words
MIN_SIG = 4            # fewer significant words: too generic to match safely
MIN_CHARS = 18         # fewer letters and digits in the matched part: likewise
STOP = frozenset("a an and are as at be by can do does for from has have how in into is it its of on or "
                 "our over that the their this to under up via was we were what when which why with without "
                 "you your".split())
# words that on their own make no title specific: a title made only of these is refused
COMMON = frozenset("""
analysis approach approaches based data deep efficient framework frameworks general generative image images
improved improving introduction large learning machine method methods model modeling modelling models network
networks neural new novel overview problem problems review robust scalable simple study studies survey system
systems task tasks toward towards training tutorial understanding using language languages representation
representations recognition detection classification prediction optimization theory applications application
""".split())
ALNUM = re.compile(r"[a-z0-9]+")
_NON_ALNUM = re.compile(r"[^a-z0-9]+")

_ARXIV_NEW = re.compile(r"(\d{4}\.\d{4,5})(?:v\d+)?")
_ARXIV_OLD = re.compile(r"([a-z][a-z.\-]*/\d{7})(?:v\d+)?", re.I)


def fold(s: str) -> str:
    """Lowercase ASCII: accents and ligatures taken off (NFKD), any other letter dropped, the
    same on both sides of a match. All in C, so a 2 MB paper text folds in milliseconds."""
    s = s or ""
    if not s.isascii():
        s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")
    return s.lower()


def squash(s: str) -> str:
    """Letters and digits only: "Score-\\nbased Gen-\\nerative" -> "scorebasedgenerative"."""
    return _NON_ALNUM.sub("", fold(s))


def norm_arxiv(v) -> str | None:
    """"arXiv:2210.02747v3", "https://arxiv.org/abs/2210.02747" -> "2210.02747"."""
    if not isinstance(v, str):
        return None
    s = v.strip()
    s = re.sub(r"^(?:https?://)?(?:www\.|export\.)?arxiv\.org/(?:abs|pdf)/", "", s, flags=re.I)
    s = re.sub(r"^arxiv:\s*", "", s, flags=re.I)
    s = re.sub(r"\.pdf$", "", s, flags=re.I)
    m = _ARXIV_NEW.fullmatch(s) or _ARXIV_OLD.fullmatch(s)
    return m.group(1).lower() if m else None


def norm_doi(v) -> str | None:
    if not isinstance(v, str):
        return None
    s = v.strip()
    s = re.sub(r"^(?:https?://)?(?:dx\.)?doi\.org/", "", s, flags=re.I)
    s = re.sub(r"^doi:\s*", "", s, flags=re.I).lower()
    return s if re.fullmatch(r"10\.\d{3,9}/\S+", s) else None


# ---------------------------------------------------------------- titles

def title_words(title: str) -> list[str]:
    return ALNUM.findall(fold(title or ""))


def _significant(w: str) -> bool:
    return w not in STOP and (len(w) >= 2 or w.isdigit())


def _key_of(words: list[str]) -> tuple[str, list[str]] | None:
    """(squashed key, the words it is made of) of the first KEY_WORDS significant words, or None
    when too short or generic."""
    sig, cut = 0, len(words)
    for i, w in enumerate(words):
        if _significant(w):
            sig += 1
            if sig == KEY_WORDS:
                cut = i + 1
                break
    used = words[:cut]
    s = [w for w in used if _significant(w)]
    key = "".join(used)
    if len(s) < MIN_SIG or len(key) < MIN_CHARS or all(w in COMMON for w in s):
        return None
    return key, used


def refused(title: str) -> str | None:
    """Why this title is not matched in text (None: it is)."""
    words = title_words(title)
    if not words:
        return "no title"
    if _key_of(words) is None:
        s = [w for w in words if _significant(w)]
        if len(s) < MIN_SIG:
            return f"fewer than {MIN_SIG} significant words"
        if all(w in COMMON for w in s):
            return "only common words"
        return f"fewer than {MIN_CHARS} letters and digits"
    return None


def title_keys(title: str) -> list[str]:
    """The squashed keys a title is matched by: its first KEY_WORDS significant words, and the
    part before a colon or dash when that part is specific enough on its own (a citation may
    leave the subtitle out). [] for a title too short or generic to match."""
    words = title_words(title)
    k = _key_of(words)
    if k is None:
        return []
    keys = [k[0]]
    head = re.split(r"\s*(?::|\s[-–—]\s)\s*", title or "", maxsplit=1)[0]
    if head != title:
        h = _key_of(title_words(head))
        if h is not None and h[0] not in keys:
            keys.append(h[0])
    return keys


def near_words(title: str) -> list[str]:
    """Words for a search index's NEAR prefilter: the matched part's significant words that a
    PDF's text holds as the same word (not "score-based", which a line break can join into one)."""
    k = _key_of(title_words(title))
    if k is None:
        return []
    out = []
    for raw in (title or "").split():
        raw = raw.strip(".,;:!?()[]{}'\"‘’“”")
        ws = title_words(raw)
        if raw.isascii() and len(ws) == 1 and ws[0] in k[1] and _significant(ws[0]) and ws[0] not in out:
            out.append(ws[0])
    return out


class Squashed:
    """A text as its letters and digits only (squash), with the way back to the text's offsets
    made only when a snippet asks for it."""

    def __init__(self, text: str):
        self.text = text or ""
        self.s = squash(self.text)
        self._at = None

    def _map(self):
        at, src, n = [], [], 0
        for m in re.finditer(r"[^\W_]+", self.text):
            w = squash(m.group(0))
            if w:
                at.append(n)
                src.append((m.start(), m.end()))
                n += len(w)
        self._at, self._src = at, src

    def span(self, i: int, j: int) -> tuple[int, int]:
        """The text's (start, end) of squashed [i, j) (whole words)."""
        if self._at is None:
            self._map()
        if not self._at:
            return 0, 0
        a = max(0, bisect.bisect_right(self._at, i) - 1)
        b = max(0, bisect.bisect_right(self._at, max(i, j - 1)) - 1)
        return self._src[a][0], self._src[b][1]

    def count(self, key: str) -> int:
        return self.s.count(key) if key else 0

    def find(self, key: str) -> int:
        return self.s.find(key) if key else -1


def title_in(sq: Squashed, title: str, longer: list[str] = ()) -> tuple[int, int] | None:
    """(start, end) in the text of the title's mention, or None. `longer`: squashed titles of
    other known papers; occurrences inside one of those that contains the key do not count."""
    for key in title_keys(title):
        n = sq.count(key)
        if not n:
            continue
        inside = sum(sq.count(L) * L.count(key) for L in longer if len(L) > len(key) and key in L)
        if n <= inside:
            continue
        i = sq.find(key)
        return sq.span(i, i + len(key))
    return None


# ---------------------------------------------------------------- ids

def arxiv_rx(aid: str):
    aid = norm_arxiv(aid)
    if not aid:
        return None
    body = re.escape(aid).replace(r"\.", r"\.\s?").replace("/", r"/\s?")
    return re.compile(r"(?<![\w.])" + body + r"(?:v\d+)?(?!\d)", re.I)


def doi_rx(doi: str):
    doi = norm_doi(doi)
    if not doi:
        return None
    body = "".join(re.escape(ch) + (r"\s?" if ch in "./-_:;" else "") for ch in doi)
    return re.compile(r"(?<![\w.])" + body + r"(?![a-z0-9])", re.I)


def arxiv_in(text: str, aid: str) -> tuple[int, int] | None:
    rx = arxiv_rx(aid)
    m = rx.search(text or "") if rx else None
    return m.span() if m else None


def doi_in(text: str, doi: str) -> tuple[int, int] | None:
    rx = doi_rx(doi)
    m = rx.search(text or "") if rx else None
    return m.span() if m else None


def arxiv_ids(text: str) -> set[str]:
    """Every arXiv id (without its version) the text names."""
    out = {m.group(1) for m in re.finditer(r"(?<![\w.])(\d{4}\.\d{4,5})(?:v\d+)?(?!\d)", text or "")}
    out |= {m.group(1).lower() for m in re.finditer(r"(?<![\w./-])([a-z][a-z\-]*(?:\.[a-z]{2})?/\d{7})(?:v\d+)?(?!\d)",
                                                     text or "", re.I)}
    return out


def snippet(text: str, span: tuple[int, int], width: int = 220) -> str:
    """A short plain stretch of the text around span, white space made single, "…" at a cut."""
    a, b = span
    pad = max(0, (width - (b - a)) // 2)
    s, e = max(0, a - pad), min(len(text), b + pad)
    out = " ".join(text[s:e].split())
    return ("…" if s > 0 else "") + out + ("…" if e < len(text) else "")


def same_work(a: dict, b: dict) -> bool:
    """Two records are (versions of) one work: the same arXiv id, the same DOI, or the same
    title (or the same first KEY_WORDS significant words of it)."""
    ax, bx = norm_arxiv(a.get("arxiv_id")), norm_arxiv(b.get("arxiv_id"))
    if ax and ax == bx:
        return True
    ad, bd = norm_doi(a.get("doi")), norm_doi(b.get("doi"))
    if ad and ad == bd:
        return True
    at, bt = squash(a.get("title") or ""), squash(b.get("title") or "")
    if at and at == bt and len(at) >= 12:
        return True
    ka, kb = title_keys(a.get("title") or ""), title_keys(b.get("title") or "")
    return bool(ka and kb and ka[0] == kb[0])
