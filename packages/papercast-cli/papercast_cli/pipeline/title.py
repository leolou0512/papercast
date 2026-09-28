"""The paper's title and identifiers, cleaned. Copied from Leo's runner (title.py: title
hygiene; fetch.py: arXiv and DOI forms). The runner looks the title up itself (arXiv, Crossref,
page one); here the agent reads the paper and writes paper.json, and this makes sure what it
wrote reads like a title and an identifier, never a filename, a URL or a sentence.
"""
from __future__ import annotations

import html
import re
import unicodedata
import urllib.parse

MAX_TITLE = 300
ARXIV_NEW = r"\d{4}\.\d{4,5}"
ARXIV_OLD = r"[a-z\-]+(?:\.[A-Z]{2})?/\d{7}"
ARXIV_ID = re.compile(rf"^({ARXIV_NEW}|{ARXIV_OLD})(v\d+)?$", re.I)
DOI_RE = re.compile(r"\b(10\.\d{4,9}/[^\s\"'<>]+)", re.I)

_TAG = re.compile(r"<[^>]{0,200}>")
_FILENAME = re.compile(r"(?i)\.(pdf|docx?|odt|rtf|tex|dvi|ps|eps|indd|qxd|pages|txt|md|html?)\b")


def clean(t) -> str | None:
    """Whitespace collapsed, markup and entities removed, NFC; None when nothing that reads like
    a title is left."""
    if not t or not isinstance(t, str):
        return None
    t = html.unescape(_TAG.sub("", t))
    t = unicodedata.normalize("NFC", " ".join(t.split())).strip(" \t.;,")
    if not t or len(t) > MAX_TITLE:
        return None
    if "://" in t or t.lower().startswith("www.") or _FILENAME.search(t):
        return None                                     # never a URL or a filename
    letters = sum(c.isalpha() for c in t)
    if letters < 4 or letters < 0.5 * len(t.replace(" ", "")):
        return None
    return t


def norm(t: str | None) -> str:
    """The hub's normalised title (db.norm_title): lowercase, every run of non-alphanumerics to
    one space, trimmed."""
    return re.sub(r"[^0-9a-z]+", " ", (t or "").lower()).strip()


def arxiv_id(v) -> str | None:
    """"arXiv:2210.02747v3", "2210.02747", an arxiv.org link -> "2210.02747" (no version)."""
    if not v or not isinstance(v, str):
        return None
    s = v.strip()
    if "arxiv.org" in s.lower():
        got = arxiv_from_url(s)
        return got
    s = re.sub(r"(?i)^arxiv:\s*", "", s)
    m = ARXIV_ID.match(s)
    return m.group(1) if m else None


def arxiv_from_url(url: str) -> str | None:
    """The id (without version) of an arxiv.org abs/pdf/html link, else None."""
    try:
        p = urllib.parse.urlsplit(url.strip())
    except ValueError:
        return None
    host = (p.hostname or "").lower()
    if host not in ("arxiv.org", "www.arxiv.org", "export.arxiv.org"):
        return None
    m = re.match(r"^/(?:abs|pdf|html)/(.+?)(?:\.pdf)?/?$", p.path)
    if not m:
        return None
    mm = ARXIV_ID.match(m.group(1))
    return mm.group(1) if mm else None


def doi(v) -> str | None:
    """"https://doi.org/10.1000/XYZ." -> "10.1000/xyz" (lowercased: the hub's identity)."""
    if not v or not isinstance(v, str):
        return None
    m = DOI_RE.search(urllib.parse.unquote(v.strip()))
    return m.group(1).rstrip(".,;)]}").lower() if m else None


def pick(paper_title, claims_title) -> str | None:
    """paper.json's title (the agent copied it from the paper, identification first), else
    claims.md's."""
    return clean(paper_title) or clean(claims_title)
