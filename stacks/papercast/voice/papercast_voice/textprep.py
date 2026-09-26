"""script.md -> chunks the speech engine is given, one at a time.

Why chunks at all: an episode is fifteen to twenty-five minutes, and none of the engines takes
that in one call (Kokoro infers 510 phoneme tokens at a time, the GPU models have a few
thousand tokens of context). Chunks are also the unit of progress, of resuming after a
restart (a finished chunk is never voiced again), of the CPU voice's parallelism, and of
giving the GPU back to someone else between two calls.

Rules, "for the ear":
- A chunk ends at the end of a sentence. Only a sentence longer than the limit is cut, at a
  comma, semicolon, colon or dash, and only a clause longer than the limit is cut between words.
- A heading line (`# ...`) is its own chunk, spoken as a section title with longer pauses around
  it (the runner counts heading words as spoken words too).
- The pause after each chunk depends on what follows: next sentence, next paragraph, next section.

And the guard: every chunk is checked again right before it reaches an engine
(`assert_speakable`). The runner validates script.md first (INTERFACE §6), so this should never
fire; it exists so that no digit, symbol or LaTeX can reach a speech engine even if the runner's
check or this module's own splitting has a bug. It uses the runner's character policy.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import asdict, dataclass

# Identical to stacks/papercast/runner/papercast_runner/script_check.py ALLOWED_PUNCT, so a
# script the runner accepted is never refused here (tests/test_textprep.py pins the set).
ALLOWED_PUNCT = frozenset(".,;:!?'\"-()–—‘’“”…")
MARKDOWN_LINE = re.compile(r"^\s*([-*+>|]|```|~~~|\d+[.)])\s")

# Words ending in a full stop that do not end a sentence.
ABBREVIATIONS = frozenset({
    "e.g.", "i.e.", "etc.", "al.", "vs.", "cf.", "dr.", "mr.", "mrs.", "ms.", "prof.",
    "st.", "jr.", "sr.", "approx.", "fig.", "eq.",
})
_SENT_END = re.compile(r"([.!?…]+[\"”’)]*)(\s+)")
_CLAUSE_END = re.compile(r"(?<=[,;:])\s+|(?<=\s[—–])\s+")


class ScriptInvalid(ValueError):
    """The script (or a chunk of it) is not plain speakable text."""


def problems(text: str) -> list[str]:
    """Everything that makes `text` unspeakable, in plain words. Empty list = fine."""
    out: list[str] = []
    digits = 0
    bad: dict[str, int] = {}
    for ch in text:
        cat = unicodedata.category(ch)
        if cat in ("Nd", "No", "Nl"):
            digits += 1
        elif ch.isspace() or ch in ALLOWED_PUNCT or cat.startswith("M"):
            continue
        elif cat.startswith("L"):
            # Greek letters are symbols in a paper (alpha, sigma): they must be spelt out.
            if "Ͱ" <= ch <= "Ͽ" or "ἀ" <= ch <= "῿":
                bad[ch] = bad.get(ch, 0) + 1
        else:
            bad[ch] = bad.get(ch, 0) + 1
    if digits:
        out.append(f"{digits} digit(s): numbers must be written as words")
    if "\\" in text or re.search(r"\$[^$]+\$", text):
        out.append("LaTeX found: equations must be spoken in words")
    if bad:
        shown = " ".join(repr(c) for c in sorted(bad, key=lambda c: -bad[c])[:12])
        out.append(f"symbols that cannot be spoken: {shown}")
    return out


def assert_speakable(text: str) -> None:
    """The engine-boundary guard. Raises ScriptInvalid; never repairs the text."""
    p = problems(text)
    if not text.strip():
        p.append("empty text")
    if p:
        raise ScriptInvalid("; ".join(p) + f" (in: {text[:80]!r})")


def parse_blocks(script: str) -> list[tuple[str, str]]:
    """[(kind, text)] with kind 'heading' or 'para'. Raises ScriptInvalid on other Markdown."""
    blocks: list[tuple[str, str]] = []
    para: list[str] = []
    md_lines: list[int] = []

    def flush() -> None:
        if para:
            blocks.append(("para", " ".join(para)))
            para.clear()

    for n, line in enumerate(script.replace("\r\n", "\n").replace("\r", "\n").split("\n"), 1):
        s = " ".join(line.split())          # collapses tabs, non-breaking spaces, runs
        if not s:
            flush()
            continue
        if s.startswith("#"):
            flush()
            body = s.lstrip("#").strip()
            if not body:
                md_lines.append(n)
                continue
            blocks.append(("heading", body))
            continue
        if MARKDOWN_LINE.match(line):
            md_lines.append(n)
        para.append(s)
    flush()
    if md_lines:
        raise ScriptInvalid("Markdown other than headings and paragraphs on line(s) "
                            + ", ".join(map(str, md_lines[:10])))
    return blocks


def split_sentences(text: str) -> list[str]:
    out: list[str] = []
    start = 0
    for m in _SENT_END.finditer(text):
        cand = text[start:m.end(1)].strip()
        nxt = text[m.end():m.end() + 1]
        last = cand.rsplit(None, 1)[-1] if cand else ""
        if (last.lower() in ABBREVIATIONS or re.fullmatch(r"[A-Z]\.", last)
                or (nxt and nxt.islower())):
            continue
        if cand:
            out.append(cand)
        start = m.end()
    tail = text[start:].strip()
    if tail:
        out.append(tail)
    return out


def _nwords(s: str) -> int:
    return len(s.split())


def _split_words(s: str, max_words: int) -> list[str]:
    w = s.split()
    n = -(-len(w) // max_words)             # fewest pieces, sizes differing by one at most
    base, extra = divmod(len(w), n)
    out, i = [], 0
    for k in range(n):
        size = base + (1 if k < extra else 0)
        out.append(" ".join(w[i:i + size]))
        i += size
    return out


def split_long(sentence: str, max_words: int) -> list[str]:
    """A sentence over max_words, cut at clause boundaries, then between words if it must."""
    if _nwords(sentence) <= max_words:
        return [sentence]
    pieces: list[str] = []
    for clause in _CLAUSE_END.split(sentence):
        clause = clause.strip()
        if not clause:
            continue
        pieces.extend(_split_words(clause, max_words) if _nwords(clause) > max_words else [clause])
    return _pack(pieces, max_words)


def _pack(pieces: list[str], max_words: int) -> list[str]:
    out: list[str] = []
    cur: list[str] = []
    n = 0
    for p in pieces:
        k = _nwords(p)
        if cur and n + k > max_words:
            out.append(" ".join(cur))
            cur, n = [], 0
        cur.append(p)
        n += k
    if cur:
        out.append(" ".join(cur))
    return out


@dataclass
class Chunk:
    idx: int
    kind: str           # "heading" | "para"
    block: int          # index of the heading or paragraph it came from
    text: str
    words: int
    gap_after_s: float

    def as_dict(self) -> dict:
        return asdict(self)


def plan(script: str, max_words: int, audio: dict) -> list[Chunk]:
    """The whole script checked, then cut into chunks with the pause after each."""
    if max_words < 5:
        raise ValueError("max_words too small")
    blocks = parse_blocks(script)
    p = problems("\n".join(text for _kind, text in blocks))   # heading markers removed
    if p:
        raise ScriptInvalid("; ".join(p))
    speak_headings = bool(audio.get("speak_headings", True))
    raw: list[tuple[str, int, str]] = []    # (kind, block, text)
    section_start = set()
    for b, (kind, text) in enumerate(blocks):
        if kind == "heading":
            if not speak_headings:
                section_start.add(b + 1)
                continue
            if not re.search(r"[.!?…:]$", text):
                text += "."                 # a heading is said as a title, with a full stop
            for piece in split_long(text, max_words):
                raw.append(("heading", b, piece))
            continue
        pieces: list[str] = []
        for s in split_sentences(text):
            pieces.extend(split_long(s, max_words))
        for piece in _pack(pieces, max_words):
            raw.append(("para", b, piece))
    if not raw:
        raise ScriptInvalid("the script has no spoken text")
    chunks: list[Chunk] = []
    for i, (kind, b, text) in enumerate(raw):
        nxt = raw[i + 1] if i + 1 < len(raw) else None
        if nxt is None:
            gap = audio["tail_s"]
        elif kind == "heading" and nxt[0] != "heading":
            gap = audio["gap_after_heading_s"]
        elif nxt[0] == "heading" or nxt[1] in section_start and nxt[1] != b:
            gap = audio["gap_before_heading_s"]
        elif nxt[1] != b:
            gap = audio["gap_paragraph_s"]
        else:
            gap = audio["gap_sentence_s"]
        assert_speakable(text)
        chunks.append(Chunk(i, kind, b, text, _nwords(text), float(gap)))
    return chunks
