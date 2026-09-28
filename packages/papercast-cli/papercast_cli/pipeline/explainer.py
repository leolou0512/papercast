"""The explainer page: built by the pipeline, never written by the model. Copied from Leo's
runner (explainer.py); what differs here: poppler is optional (without it a crop figure is
left out and the page has SVG figures and text only), and the page stays within the hub's
8 MB (SPEC.md section 6).

The agent writes `explainer.json`:
    {"points": ["<key point>", ...],                              1 to 5, one sentence each
     "figures": [{"crop": "page=N;box=l,t,r,b", "caption": "..."},  0 to 3
                 {"svg": "<svg ...>...</svg>", "caption": "..."}]}
`check` validates it (schema, lengths, and no meta text: nothing about the audio, the page, the
figures as a set or the instructions, nothing addressed to the reader), cuts the crops from the
PDF and sanitises the SVG. `render` puts it into the one fixed template below: <h1> the paper's
title, the points as a plain list, each figure with its caption, and nothing else.

Why: the model echoed its instructions into the product ("GEODE: three things the audio can't
show", "Paper crops, each with what to look for.", "Check the RMSD column"). A page it cannot
write cannot carry that.
"""
from __future__ import annotations

import base64
import html as _html
import json
import re
import xml.etree.ElementTree as ET
from html.parser import HTMLParser

from ..common import wording
from . import pdf

MAX_JSON = 512 * 1024
MAX_SVG = 100 * 1024
MAX_OUT = 8 * 1024 * 1024       # the hub refuses a larger explainer.html (SPEC.md section 6)
# Leo's page limits (runner config: PAPERCAST_EXPLAINER_MAX_WORDS / _MAX_FIGURES).
PAGE_MAX_WORDS = 400
PAGE_MAX_FIGURES = 3
MAX_POINTS = 5
MAX_FIGURES = 3
POINT_WORDS = 30
CAPTION_WORDS = 40
CROP_RE = re.compile(r"^\s*page\s*=\s*(\d{1,4})\s*;\s*box\s*=\s*"
                     r"([01](?:\.\d+)?)\s*,\s*([01](?:\.\d+)?)\s*,\s*([01](?:\.\d+)?)\s*,\s*([01](?:\.\d+)?)\s*$")
_WORD = re.compile(r"\w", re.U)


def words(text: str) -> int:
    return sum(1 for w in (text or "").split() if _WORD.search(w))


# --- no meta text -------------------------------------------------------------------------
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
_ABBREV = re.compile(r"(?:\b(?:e\.g|i\.e|et al|etc|vs|cf|Fig|Figs|Eq|Eqs|Tab|Sec|No|approx|resp)|"
                     r"\b[A-Z])\.$")
_MARKUP = re.compile(r"<\s*/?\s*[A-Za-z]|\*\*|__|`|^#|\]\(")


def sentences(text: str) -> int:
    """Sentences in `text`: breaks at . ! ? followed by space and an upper-case letter, digit
    or quote, except after a common abbreviation (e.g., et al., Fig.)."""
    n = 1
    for m in re.finditer(r"[.!?](?=\s+[\"'“(]?[A-Z0-9])", text):
        head = text[:m.end()]
        if m.group(0) == "." and _ABBREV.search(head):
            continue
        n += 1
    return n


def meta_problems(text: str) -> list[str]:
    """Why `text` is not a plain statement about the paper (empty list: it is)."""
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
    # never-wanted wording shared with the script (INTERFACE 1.8.1, lists in wording.json)
    for c, found in wording.hits(text, "explainer"):
        out.append(f"{c['wrong']} ({wording.quoted(found)})")
    return out


def _text_problems(label: str, text, max_words: int, max_sentences: int, what: str) -> list[str]:
    if not isinstance(text, str) or not text.strip():
        return [f"{label}: must be a non-empty string"]
    t = " ".join(text.split())
    shown = t if len(t) <= 90 else t[:87] + "..."
    out = []
    # Per-item length is guidance in the prompt, not a rejection: a caption one word over its
    # guide failed a whole paper overnight (2026-09-27, #3). The page-level limit (Leo's 400 words
    # and 3 figures) is still enforced by explainer_too_long.
    if _MARKUP.search(t):
        out.append(f'{label} ("{shown}"): plain text only, no markup')
    for why in meta_problems(t):
        out.append(f'{label} ("{shown}"): {why}; {what}')
    return out


# --- SVG sanitiser ------------------------------------------------------------------------
SVG_NS = "http://www.w3.org/2000/svg"
XLINK_NS = "http://www.w3.org/1999/xlink"
SVG_ELEMENTS = {"svg", "g", "defs", "symbol", "use", "path", "rect", "circle", "ellipse", "line",
                "polyline", "polygon", "text", "tspan", "title", "desc", "marker", "clipPath"}
SVG_ATTRS = {
    "id", "class", "viewBox", "width", "height", "x", "y", "x1", "y1", "x2", "y2", "cx", "cy",
    "r", "rx", "ry", "d", "points", "transform", "fill", "fill-opacity", "fill-rule", "stroke",
    "stroke-width", "stroke-opacity", "stroke-dasharray", "stroke-dashoffset", "stroke-linecap",
    "stroke-linejoin", "stroke-miterlimit", "opacity", "font-size", "font-family", "font-weight",
    "font-style", "text-anchor", "dominant-baseline", "alignment-baseline", "dx", "dy", "rotate",
    "textLength", "lengthAdjust", "marker-start", "marker-mid", "marker-end", "markerWidth",
    "markerHeight", "refX", "refY", "orient", "markerUnits", "preserveAspectRatio", "clip-path",
    "clipPathUnits", "clip-rule", "vector-effect", "letter-spacing", "role", "aria-label",
    "aria-hidden", "href", "overflow", "paint-order", "baseline-shift",
}
_URL_OK = re.compile(r"^\s*url\(\s*#[\w.:-]+\s*\)\s*$")
_BAD_VALUE = re.compile(r"javascript:|vbscript:|data:|expression\s*\(|@import|\\", re.I)


class SvgError(ValueError):
    pass


def _local(tag: str) -> tuple[str | None, str]:
    if tag.startswith("{"):
        ns, _, name = tag[1:].partition("}")
        return ns, name
    return None, tag


def sanitise_svg(src: str) -> tuple[str, int]:
    """(safe SVG markup, number of things removed). Allowlist only: SVG shape, text and
    structure elements with presentation and geometry attributes. Removed: script,
    foreignObject, image, a, style, animation and every other element (with its children);
    every on* attribute, style attribute and attribute outside the list; any href that is not
    `#id`; any url(...) that is not url(#id). Raises SvgError if it is not one SVG element."""
    if not isinstance(src, str) or not src.strip():
        raise SvgError("empty svg")
    if len(src.encode("utf-8")) > MAX_SVG:
        raise SvgError(f"svg is over {MAX_SVG // 1024} KiB")
    if re.search(r"<!\s*(DOCTYPE|ENTITY)", src, re.I):
        raise SvgError("svg must not contain a DOCTYPE or ENTITY declaration")
    try:
        root = ET.fromstring(src.strip())
    except ET.ParseError as e:
        raise SvgError(f"svg is not well-formed XML ({e})")
    ns, name = _local(root.tag)
    if name != "svg" or ns not in (None, SVG_NS):
        raise SvgError("the value must be one <svg> element")
    removed = [0]

    def attrs(el) -> list[tuple[str, str]]:
        out = []
        ens, ename = _local(el.tag)
        for k, v in el.attrib.items():
            kns, kname = _local(k)
            if kns == XLINK_NS and kname == "href":
                kname, kns = "href", None
            if kns is not None or kname not in SVG_ATTRS or kname.lower().startswith("on"):
                removed[0] += 1
                continue
            v = str(v)
            if kname == "href":
                if ename != "use" or not re.match(r"^#[\w.:-]+$", v.strip()):
                    removed[0] += 1
                    continue
            if "url(" in v.lower() and not _URL_OK.match(v):
                removed[0] += 1
                continue
            if _BAD_VALUE.search(v) or len(v) > 50000:
                removed[0] += 1
                continue
            out.append((kname, v))
        return out

    def emit(el, top=False) -> str:
        ens, ename = _local(el.tag)
        if ens not in (None, SVG_NS) or ename not in SVG_ELEMENTS:
            removed[0] += 1
            return ""
        at = attrs(el)
        if ename == "use" and not any(k == "href" for k, _ in at):
            return ""
        if top:
            d = dict(at)
            if "viewBox" not in d:
                try:
                    w = float(re.sub(r"px$", "", d.get("width", "")))
                    h = float(re.sub(r"px$", "", d.get("height", "")))
                    at.append(("viewBox", f"0 0 {w:g} {h:g}"))
                except ValueError:
                    pass
            # The template sizes the figure to the column; fixed sizes would overflow a phone.
            at = [(k, v) for k, v in at if k not in ("width", "height")]
            at.insert(0, ("xmlns", SVG_NS))
        parts = [f"<{ename}"]
        for k, v in at:
            parts.append(f' {k}="{_html.escape(v, quote=True)}"')
        parts.append(">")
        if el.text and ename in ("text", "tspan", "title", "desc"):
            parts.append(_html.escape(el.text, quote=False))
        for child in el:
            parts.append(emit(child))
            if child.tail and ename in ("text", "tspan"):
                parts.append(_html.escape(child.tail, quote=False))
        parts.append(f"</{ename}>")
        return "".join(parts)

    return emit(root, top=True), removed[0]


def svg_text(markup: str) -> str:
    """The visible text of sanitised SVG markup (text, tspan)."""
    try:
        root = ET.fromstring(markup)
    except ET.ParseError:
        return ""
    out = []
    for el in root.iter():
        _, name = _local(el.tag)
        if name in ("text", "tspan"):
            out.append(el.text or "")
            if name == "tspan":
                out.append(el.tail or "")
    return " ".join(" ".join(out).split())


# --- validate -----------------------------------------------------------------------------
class Spec:
    """A validated explainer: points, and figures ready to inline."""

    def __init__(self):
        self.points: list[str] = []
        self.figures: list[dict] = []     # {"kind": "crop"|"svg", "png": bytes | "svg": str, "caption"}
        self.removed = 0
        self.dropped = 0                  # crop figures left out: no poppler or no PDF

    def measure(self, title: str | None) -> dict:
        """Words and figures of the page it renders to (INTERFACE §6 limits)."""
        n = words(title or "") + sum(words(x) for x in self.points)
        for f in self.figures:
            n += words(f["caption"])
            if f["kind"] == "svg":
                n += words(svg_text(f["svg"]))
        return {"words": n, "figures": len(self.figures)}


def check(text: str, pdf_path: str | None, pages: int, crops: bool = True,
          dpi: int = 150) -> tuple[Spec | None, list[str]]:
    """(spec, problems). problems name each offending item and why, in words the agent can act
    on in its one repair turn; spec is None when there is any problem. `crops` False (no
    poppler, or no PDF: the paper was read from a web page): a crop figure is left out of the
    page rather than refused, counted in spec.dropped."""
    if len(text.encode("utf-8")) > MAX_JSON:
        return None, [f"`explainer.json` is over {MAX_JSON // 1024} KiB"]
    try:
        data = json.loads(text)
    except ValueError as e:
        return None, [f"`explainer.json` is not valid JSON ({e})"]
    if not isinstance(data, dict):
        return None, ['`explainer.json` must be one JSON object {"points": [...], "figures": [...]}']
    probs: list[str] = []
    extra = sorted(set(data) - {"points", "figures"})
    if extra:
        probs.append("`explainer.json`: unknown field(s) " + ", ".join(f'"{k}"' for k in extra) +
                     '; only "points" and "figures" (the page has no other text)')
    points = data.get("points")
    figures = data.get("figures", [])
    if not isinstance(points, list) or not (1 <= len(points) <= MAX_POINTS):
        probs.append(f'"points": a list of 1 to {MAX_POINTS} strings'
                     + (f", found {len(points)}" if isinstance(points, list) else ""))
        points = points if isinstance(points, list) else []
    if not isinstance(figures, list) or len(figures) > MAX_FIGURES:
        probs.append(f'"figures": a list of at most {MAX_FIGURES} items'
                     + (f", found {len(figures)}" if isinstance(figures, list) else ""))
        figures = figures[:MAX_FIGURES] if isinstance(figures, list) else []
    spec = Spec()
    for i, pt in enumerate(points[:MAX_POINTS], 1):
        probs += _text_problems(f"point {i}", pt, POINT_WORDS, 1,
                                "state one key point of the paper as a plain fact")
        if isinstance(pt, str):
            spec.points.append(" ".join(pt.split()))
    sizes = None
    for i, fig in enumerate(figures, 1):
        label = f"figure {i}"
        if not isinstance(fig, dict):
            probs.append(f'{label}: must be an object with "crop" or "svg", and "caption"')
            continue
        extra = sorted(set(fig) - {"crop", "svg", "caption"})
        if extra:
            probs.append(f"{label}: unknown field(s) " + ", ".join(f'"{k}"' for k in extra))
        cap = fig.get("caption")
        probs += _text_problems(f"caption of {label}", cap, CAPTION_WORDS, 2,
                                "state what the figure shows as a fact about the paper")
        has_crop, has_svg = "crop" in fig, "svg" in fig
        if has_crop == has_svg:
            probs.append(f'{label}: exactly one of "crop" or "svg"')
            continue
        item = {"caption": " ".join(cap.split()) if isinstance(cap, str) else ""}
        if has_crop:
            spec_s = fig.get("crop")
            m = CROP_RE.match(spec_s) if isinstance(spec_s, str) else None
            if not m:
                probs.append(f'{label}: "crop" must look like "page=3;box=0.08,0.12,0.92,0.55"')
                continue
            page = int(m.group(1))
            x0, y0, x1, y1 = (float(m.group(k)) for k in range(2, 6))
            if not (0 <= x0 < x1 <= 1 and 0 <= y0 < y1 <= 1):
                probs.append(f"{label}: crop box must satisfy 0 <= left < right <= 1 and "
                             "0 <= top < bottom <= 1")
                continue
            if not crops or not pdf_path:
                spec.dropped += 1
                continue
            if sizes is None:
                try:
                    sizes = pdf.page_sizes(pdf_path, pages) if pages else {}
                except Exception:                    # noqa: BLE001
                    sizes = {}
            if page not in sizes:
                probs.append(f"{label}: page {page} does not exist (the paper has {pages} pages)")
                continue
            try:
                png = pdf.crop_png(pdf_path, page, (x0, y0, x1, y1), sizes[page], dpi=dpi)
            except pdf.PdfError as e:
                probs.append(f"{label}: {e}")
                continue
            item.update(kind="crop", png=png)
        else:
            try:
                markup, removed = sanitise_svg(fig.get("svg"))
            except SvgError as e:
                probs.append(f"{label}: {e}")
                continue
            spec.removed += removed
            item.update(kind="svg", svg=markup)
        spec.figures.append(item)
    return (None, probs) if probs else (spec, [])


# --- the one template -----------------------------------------------------------------------
# The page's "Now Playing" design (design/mock-2.html): system font stack, one accent colour,
# light and dark from prefers-color-scheme, generous whitespace; no cards, gradients, shadows,
# badges or icons. Readable at 390 px.
CSS = """\
:root{--bg:#FFFFFF;--text:#111111;--text-2:#5C5C5C;--accent:#C8431A;\
--sans:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;\
color-scheme:light}
@media (prefers-color-scheme:dark){:root{--bg:#0E0E0E;--text:#F2F2F2;--text-2:#9E9E9E;\
--accent:#FF7A45;color-scheme:dark}}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%;text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--text);font:17px/1.55 var(--sans);\
-webkit-font-smoothing:antialiased;text-rendering:optimizeLegibility}
main{max-width:720px;margin:0 auto;padding:48px 24px 64px}
h1{font-size:26px;line-height:1.25;font-weight:700;letter-spacing:-0.01em;margin:0 0 28px}
ul{margin:0 0 44px;padding:0 0 0 1.15em}
li{margin:0 0 14px;padding-left:4px}
li::marker{color:var(--accent)}
figure{margin:0 0 44px}
figure img{display:block;max-width:100%;height:auto;margin:0 auto}
figure svg{display:block;width:100%;height:auto;max-height:70vh;color:var(--text);\
overflow:visible}
figure svg .hl{color:var(--accent)}
figcaption{margin-top:14px;font-size:15px;line-height:1.5;color:var(--text-2)}
@media (max-width:480px){main{padding:28px 16px 48px}h1{font-size:22px;margin-bottom:22px}\
body{font-size:16px}}
"""


def render(spec: Spec, title: str | None) -> bytes:
    """explainer.html: <h1> title, the points as a list, each figure with its caption. No other
    text, heading, intro or footer."""
    e = lambda s: _html.escape(s or "", quote=True)       # noqa: E731
    body = []
    if title:
        body.append(f"<h1>{e(title)}</h1>")
    body.append("<ul>" + "".join(f"<li>{e(x)}</li>" for x in spec.points) + "</ul>")
    for f in spec.figures:
        if f["kind"] == "crop":
            src = "data:image/png;base64," + base64.b64encode(f["png"]).decode("ascii")
            fig = f'<img src="{src}" alt="">'
        else:
            fig = f["svg"]
        body.append(f"<figure>{fig}<figcaption>{e(f['caption'])}</figcaption></figure>")
    page = ("<!doctype html>\n<html lang=\"en\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
            f"<title>{e(title)}</title><style>{CSS}</style></head>"
            "<body><main>" + "".join(body) + "</main></body></html>\n")
    out = page.encode("utf-8")
    if len(out) > MAX_OUT:
        raise ValueError(f"explainer is {len(out)} bytes (limit {MAX_OUT})")
    return out


def over_limits(spec: Spec, title: str | None) -> list[str]:
    """Leo's page limits (at most 400 words and 3 figures), as the repair turn words them."""
    m = spec.measure(title)
    if m["words"] > PAGE_MAX_WORDS or m["figures"] > PAGE_MAX_FIGURES:
        return [f"Explainer has {m['words']} words and {m['figures']} figures; the limits are "
                f"{PAGE_MAX_WORDS} words and {PAGE_MAX_FIGURES} figures."]
    return []


def build(text: str, title: str | None, pdf_path: str | None, pages: int,
          crops: bool) -> tuple[bytes | None, Spec | None, list[str]]:
    """(explainer.html, spec, problems): check, then render; crops are cut again at a lower
    resolution while the page would be over the hub's 8 MB."""
    problems: list[str] = []
    for dpi in (150, 100, 72):
        spec, problems = check(text, pdf_path, pages, crops=crops, dpi=dpi)
        if spec is None:
            return None, None, problems
        try:
            return render(spec, title), spec, []
        except ValueError as e:
            problems = [str(e)]
            if not any(f["kind"] == "crop" for f in spec.figures):
                break
    return None, None, problems


# --- measure (INTERFACE §6, 1.7), of a rendered page -----------------------------------------
_INVISIBLE = {"script", "style", "title", "template"}


class _Measure(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.hidden = 0
        self.svg = 0
        self.words = 0
        self.figures = 0

    def handle_starttag(self, tag, attrs):
        t = tag.lower()
        if t in _INVISIBLE:
            self.hidden += 1
        elif t in ("img", "canvas"):
            self.figures += 1
        elif t == "svg":
            if self.svg == 0:
                self.figures += 1
            self.svg += 1

    def handle_startendtag(self, tag, attrs):
        t = tag.lower()
        if t in ("img", "canvas") or (t == "svg" and self.svg == 0):
            self.figures += 1

    def handle_endtag(self, tag):
        t = tag.lower()
        if t in _INVISIBLE and self.hidden:
            self.hidden -= 1
        elif t == "svg" and self.svg:
            self.svg -= 1

    def handle_data(self, data):
        if not self.hidden:
            self.words += words(data)


def measure(html_text: str) -> dict:
    """{"words", "figures"} of a page: visible words (script, style, title and template content
    and all tags stripped; a word is a whitespace-separated token holding a letter or digit),
    figures = every <img> + outermost <svg> + <canvas>."""
    m = _Measure()
    m.feed(html_text)
    m.close()
    return {"words": m.words, "figures": m.figures}
