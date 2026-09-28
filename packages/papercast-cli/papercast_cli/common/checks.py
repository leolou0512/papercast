"""Validate script.md before it goes to the voice (Leo's runner: INTERFACE.md §6, spec §2.4), and
the checks the hub makes on every uploaded episode (SPEC.md section 6).

Paragraphs separated by blank lines and `#` heading lines only; no other Markdown; no digits;
no LaTeX or math symbols; 15-25 minutes at PAPERCAST_WPM. The speech engine may trust a script
that passed, so this errs on the side of refusing.

Since INTERFACE 1.8.1 also wording that is never wanted (wording.py, lists in wording.json): the
listener's name, narrating what the listener knows, has done or keeps in notes, talk about the
episode or the narrator's plan, the paper's publication status, stock AI phrases. Each problem
quotes every offending sentence and says how to fix it, for the one repair turn.

For the group the listener is the uploader: every wording check takes `listener_name` (their
name, which the text must never say) and `data` (a base prompt version's wording; default the
wording.json next to this file). The hub calls episode_problems; explainer_problems is Leo's
explainer.json check without the PDF.
"""
from __future__ import annotations

import json
import re
import unicodedata
import xml.etree.ElementTree as ET

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


def wording_problems(text: str, listener_name: str | None = None,
                     data: dict | None = None) -> tuple[list[str], list[str]]:
    """(problems, the classes broken): one problem per class of never-wanted wording, quoting
    every sentence that has it and saying how to fix it. `listener_name`: the uploader's name;
    `data`: a base prompt version's wording (default: wording.json)."""
    per: dict[str, list[str]] = {}
    order: list[dict] = []
    for sent in sentences(text):
        for c, found in wording.hits(sent, "script", listener_name, data):
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


def check(text: str, wpm: float, min_min: float, max_min: float,
          listener_name: str | None = None, data: dict | None = None) -> dict:
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
    said, broken = wording_problems(text, listener_name, data)
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


# ---- the hub's checks on an uploaded episode (SPEC.md section 6) ----------------------------

SCRIPT_MAX_BYTES = 60 * 1024
HTML_MAX_BYTES = 8 * 1024 * 1024
_RANGE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:to|-|–|—)\s*(\d+(?:\.\d+)?)\s*minutes", re.I)


def minutes_range(guideline, default=(15, 25)) -> tuple:
    """The episode length a base prompt asks for: its first "N to M minutes" ("15 to 25
    minutes" in base v1), else `default`. The hub passes it to episode_problems."""
    m = _RANGE.search(guideline) if isinstance(guideline, str) else None
    if m:
        lo, hi = float(m.group(1)), float(m.group(2))
        if 0 < lo < hi <= 240:
            return tuple(int(x) if x.is_integer() else x for x in (lo, hi))
    return tuple(default)


def episode_problems(script_text, explainer_obj, base_range=(15, 25), wpm=150,
                     listener_name=None, *, title=None, explainer_html=None,
                     data=None) -> list[str]:
    """Everything the hub must refuse in an uploaded episode, in plain words, each starting with
    the file it is about; [] when the episode may go to the voice. The same checks the CLI makes
    before uploading, whatever the client says.

    script_text: script.md, as text or its raw bytes (which must be UTF-8). explainer_obj:
    explainer.json parsed, or its text or bytes. base_range: the base prompt's minutes
    (minutes_range). wpm: words per minute for the length. listener_name: the uploader's name
    (the episode is made for them, so it never names them). title: the paper's title, the
    page's heading, counted in its words. explainer_html: explainer.html's bytes or text, when
    the caller has them. data: the base prompt version's wording (default: wording.json)."""
    out = []
    text = None
    if isinstance(script_text, (bytes, bytearray)):
        if len(script_text) > SCRIPT_MAX_BYTES:
            out.append(f"script.md: {len(script_text) / 1024:.0f} kB, at most "
                       f"{SCRIPT_MAX_BYTES // 1024} kB")
        else:
            try:
                text = bytes(script_text).decode("utf-8")
            except UnicodeDecodeError:
                out.append("script.md: not UTF-8 text")
    elif isinstance(script_text, str):
        n = len(script_text.encode("utf-8", "surrogatepass"))
        if n > SCRIPT_MAX_BYTES:
            out.append(f"script.md: {n / 1024:.0f} kB, at most {SCRIPT_MAX_BYTES // 1024} kB")
        else:
            text = script_text
    else:
        out.append("script.md is missing")
    if text is not None:
        lo, hi = base_range
        out += [f"script.md: {p}" for p in check(text, wpm, lo, hi, listener_name, data)["problems"]]
    if explainer_obj is None:
        out.append("explainer.json is missing")
    else:
        out += [f"explainer.json: {p}" for p in
                explainer_problems(explainer_obj, title, listener_name, data)]
    if explainer_html is not None:
        out += explainer_html_problems(explainer_html)
    return out


def explainer_html_problems(html) -> list[str]:
    """explainer.html: at most 8 MB, and an HTML document (the CLI builds it from explainer.json;
    the hub serves it only inside the sandbox)."""
    if isinstance(html, str):
        html = html.encode("utf-8", "surrogatepass")
    if not isinstance(html, (bytes, bytearray)):
        return ["explainer.html is missing"]
    out = []
    if len(html) > HTML_MAX_BYTES:
        out.append(f"explainer.html: {len(html) / 2**20:.1f} MB, at most "
                   f"{HTML_MAX_BYTES // 2**20} MB")
    head = bytes(html[:256]).lstrip(b"\xef\xbb\xbf").lstrip()
    if head[:14].lower() != b"<!doctype html":
        out.append("explainer.html: does not start with <!doctype html")
    return out


# ---- explainer.json: Leo's explainer.py check, without the PDF (the hub has none) -------------
# Refused: the schema, 1 to 5 points, at most 3 figures, markup, meta text, a malformed crop or
# SVG, and the page's limits (400 words and 3 figures). Guidance only, never refused: the words
# per point (30) and per caption (40); a caption one word over failed a whole paper overnight
# (Leo's runner, 2026-09-27, #3). Not checked here: that a crop's page exists (that needs the
# PDF; the CLI finds out when it cuts the crop).

EXPLAINER_JSON_MAX = 512 * 1024
SVG_MAX = 100 * 1024
MAX_POINTS = 5
MAX_FIGURES = 3
POINT_WORDS = 30
CAPTION_WORDS = 40
PAGE_MAX_WORDS = 400
PAGE_MAX_FIGURES = 3
CROP_RE = re.compile(r"^\s*page\s*=\s*(\d{1,4})\s*;\s*box\s*=\s*"
                     r"([01](?:\.\d+)?)\s*,\s*([01](?:\.\d+)?)\s*,\s*([01](?:\.\d+)?)\s*,\s*([01](?:\.\d+)?)\s*$")
_WORD = re.compile(r"\w", re.U)

# Each (pattern, why). Matched case-insensitively on one point or caption. Kept to phrases that
# are about this product or its instructions, not about a paper's subject: "periodic images",
# "the panels", "episode return" in an RL paper all pass.
_META = [
    (r"\bpodcasts?\b|\bthe audio\b|\bthis audio\b|\baudio (?:version|track|episode)\b|"
     r"\blisteners?\b|\blistening\b|\bthis episode\b|\bin the episode\b|"
     r"\bthe episode (?:explains|covers|describes|discusses|says|mentions)\b|\bthe script\b|"
     r"\bnarrat(?:or|ion)\b",
     "mentions the audio or the episode"),
    (r"\bthis page\b|\bon the page\b|\bthe page (?:shows|below|above)\b|\bexplainers?\b|"
     r"\bthis screen\b",
     "mentions the page itself"),
    (r"\b(?:these|those|the following|following|below|above|all|the|two|three|four|five)"
     r"\s+(?:figures|crops)\b|\bpaper crops?\b|\bcrops? (?:from|of) the paper\b|"
     r"\bfigures? (?:to look at|below|above)\b",
     "talks about the figures as a set"),
    (r"\bcan(?:'|’)?t (?:be )?(?:show|shown|said|heard|told)\b|\bcannot (?:be )?(?:show|shown|said|heard|told)\b|"
     r"\bcan not (?:be )?(?:show|shown|said)\b|\bwords alone\b|\bjust words\b|\bthrough words\b|"
     r"\bput into words\b|\bwhat to look for\b|\bkey points?\b",
     "repeats the instructions"),
    (r"\byou(?:r|rs|rself|'re|’re|'ll|’ll|'d|’d|'ve|’ve)?\b|\bthe reader\b|\bthe viewer\b",
     "addresses the reader"),
]
_META = [(re.compile(p, re.I), why) for p, why in _META]
IMPERATIVES = ("look", "check", "compare", "note", "notice", "see", "consider", "focus",
               "observe", "watch", "remember", "imagine", "keep", "pay", "recall", "mind",
               "think", "let's", "lets", "read", "scan", "start", "try", "study", "examine",
               "follow")
_CLAUSE = re.compile(r"(?:^|[.!?:;—–]\s*|\s-\s)\(?[\"'“‘]?([A-Za-z']+)")
_MID_IMPERATIVE = re.compile(r"\b(?:so|then|just|please)\s+(?:look|check|compare|note|notice|"
                             r"see|consider|focus|observe|watch)\b", re.I)
_MARKUP = re.compile(r"<\s*/?\s*[A-Za-z]|\*\*|__|`|^#|\]\(")

SVG_NS = "http://www.w3.org/2000/svg"
# What Leo's sanitiser keeps; text inside anything else is removed, so it is not counted.
SVG_ELEMENTS = {"svg", "g", "defs", "symbol", "use", "path", "rect", "circle", "ellipse", "line",
                "polyline", "polygon", "text", "tspan", "title", "desc", "marker", "clipPath"}


def words(text: str) -> int:
    """Words as Leo's page measure counts them: whitespace-separated tokens with a letter or digit."""
    return sum(1 for w in (text or "").split() if _WORD.search(w))


def meta_problems(text: str, listener_name: str | None = None, data: dict | None = None) -> list[str]:
    """Why `text` (one point or caption) is not a plain statement about the paper ([]: it is)."""
    out = []
    for rx, why in _META:
        m = rx.search(text)
        if m:
            out.append(f'{why} ("{m.group(0)}")')
    for m in _CLAUSE.finditer(text):
        if m.group(1).lower() in IMPERATIVES:
            out.append(f'tells the reader what to do ("{m.group(1)}")')
            break
    m = _MID_IMPERATIVE.search(text)
    if m:
        out.append(f'tells the reader what to do ("{m.group(0)}")')
    # never-wanted wording shared with the script (wording.json)
    for c, found in wording.hits(text, "explainer", listener_name, data):
        out.append(f"{c['wrong']} ({wording.quoted(found)})")
    return out


def _text_problems(label: str, text, what: str, listener_name, data) -> list[str]:
    if not isinstance(text, str) or not text.strip():
        return [f"{label}: must be a non-empty string"]
    t = " ".join(text.split())
    shown = t if len(t) <= 90 else t[:87] + "..."
    out = []
    if _MARKUP.search(t):
        out.append(f'{label} ("{shown}"): plain text only, no markup')
    for why in meta_problems(t, listener_name, data):
        out.append(f'{label} ("{shown}"): {why}; {what}')
    return out


def _local(tag: str) -> tuple:
    if tag.startswith("{"):
        ns, _, name = tag[1:].partition("}")
        return ns, name
    return None, tag


def _svg(src) -> tuple:
    """(root element, None) or (None, why): what Leo's sanitiser refuses; the rest it cleans."""
    if not isinstance(src, str) or not src.strip():
        return None, "empty svg"
    if len(src.encode("utf-8", "surrogatepass")) > SVG_MAX:
        return None, f"svg is over {SVG_MAX // 1024} KiB"
    if re.search(r"<!\s*(DOCTYPE|ENTITY)", src, re.I):
        return None, "svg must not contain a DOCTYPE or ENTITY declaration"
    try:
        root = ET.fromstring(src.strip())
    except ET.ParseError as e:
        return None, f"svg is not well-formed XML ({e})"
    ns, name = _local(root.tag)
    if name != "svg" or ns not in (None, SVG_NS):
        return None, "the value must be one <svg> element"
    return root, None


def _svg_words(el) -> int:
    """Visible words of an SVG as Leo's page measure counts them: text and tspan, inside kept
    elements."""
    ns, name = _local(el.tag) if isinstance(el.tag, str) else (None, "")
    if ns not in (None, SVG_NS) or name not in SVG_ELEMENTS:
        return 0
    n = 0
    if name in ("text", "tspan"):
        n += words(el.text or "")
    if name == "tspan":
        n += words(el.tail or "")
    return n + sum(_svg_words(child) for child in el)


def explainer_problems(obj, title=None, listener_name=None, data=None,
                       max_words=PAGE_MAX_WORDS, max_figures=PAGE_MAX_FIGURES) -> list[str]:
    """What is wrong with an explainer.json object (or its text): [] when it is fine. Each
    problem names the item and why, in words the agent can act on in its one repair turn.
    title: the paper's title, the page's heading (counted in the page's words)."""
    if isinstance(obj, (bytes, bytearray)):
        if len(obj) > EXPLAINER_JSON_MAX:
            return [f"over {EXPLAINER_JSON_MAX // 1024} KiB"]
        try:
            obj = bytes(obj).decode("utf-8")
        except UnicodeDecodeError:
            return ["not UTF-8 text"]
    if isinstance(obj, str):
        if len(obj.encode("utf-8", "surrogatepass")) > EXPLAINER_JSON_MAX:
            return [f"over {EXPLAINER_JSON_MAX // 1024} KiB"]
        try:
            obj = json.loads(obj)
        except ValueError as e:
            return [f"not valid JSON ({e})"]
    if not isinstance(obj, dict):
        return ['must be one JSON object {"points": [...], "figures": [...]}']
    probs: list[str] = []
    extra = sorted(set(obj) - {"points", "figures"})
    if extra:
        probs.append("unknown field(s) " + ", ".join(f'"{k}"' for k in extra) +
                     '; only "points" and "figures" (the page has no other text)')
    points = obj.get("points")
    figures = obj.get("figures", [])
    if not isinstance(points, list) or not (1 <= len(points) <= MAX_POINTS):
        probs.append(f'"points": a list of 1 to {MAX_POINTS} strings'
                     + (f", found {len(points)}" if isinstance(points, list) else ""))
        points = points if isinstance(points, list) else []
    if not isinstance(figures, list) or len(figures) > MAX_FIGURES:
        probs.append(f'"figures": a list of at most {MAX_FIGURES} items'
                     + (f", found {len(figures)}" if isinstance(figures, list) else ""))
        figures = figures[:MAX_FIGURES] if isinstance(figures, list) else []
    page_words = words(title) if isinstance(title, str) else 0
    for i, pt in enumerate(points[:MAX_POINTS], 1):
        probs += _text_problems(f"point {i}", pt, "state one key point of the paper as a plain fact",
                                listener_name, data)
        if isinstance(pt, str):
            page_words += words(pt)
    for i, fig in enumerate(figures, 1):
        label = f"figure {i}"
        if not isinstance(fig, dict):
            probs.append(f'{label}: must be an object with "crop" or "svg", and "caption"')
            continue
        extra = sorted(set(fig) - {"crop", "svg", "caption"})
        if extra:
            probs.append(f"{label}: unknown field(s) " + ", ".join(f'"{k}"' for k in extra))
        cap = fig.get("caption")
        probs += _text_problems(f"caption of {label}", cap,
                                "state what the figure shows as a fact about the paper",
                                listener_name, data)
        if isinstance(cap, str):
            page_words += words(cap)
        if ("crop" in fig) == ("svg" in fig):
            probs.append(f'{label}: exactly one of "crop" or "svg"')
            continue
        if "crop" in fig:
            m = CROP_RE.match(fig["crop"]) if isinstance(fig["crop"], str) else None
            if not m:
                probs.append(f'{label}: "crop" must look like "page=3;box=0.08,0.12,0.92,0.55"')
                continue
            x0, y0, x1, y1 = (float(m.group(k)) for k in range(2, 6))
            if int(m.group(1)) < 1:
                probs.append(f"{label}: page {m.group(1)} does not exist (pages are numbered from 1)")
            elif not (0 <= x0 < x1 <= 1 and 0 <= y0 < y1 <= 1):
                probs.append(f"{label}: crop box must satisfy 0 <= left < right <= 1 and "
                             "0 <= top < bottom <= 1")
        else:
            root, why = _svg(fig["svg"])
            if why:
                probs.append(f"{label}: {why}")
            else:
                page_words += _svg_words(root)
    if page_words > max_words or len(figures) > max_figures:
        probs.append(f"the page has {page_words} words and {len(figures)} figures; the limits "
                     f"are {max_words} words and {max_figures} figures")
    return probs

