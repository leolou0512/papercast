"""The upload bundle's manifest (SPEC.md section 5). A9 owns this file.

A bundle is a tar.gz with `manifest.json` at the root plus the files it names. validate() checks
every field the hub uses: its type, its size, and its form (ids, arXiv ids, DOIs, hashes, safe
file names). Fields it does not know are ignored, so a newer client can add one without breaking
an older hub; a known field that is wrong is a problem, because the hub stores it.
"""
from __future__ import annotations

import re

from . import checks, prefs

MANIFEST_VERSION = 1
MAX_BYTES = 50 * 1024 * 1024
REQUIRED_FILES = ("script", "explainer_json", "explainer_html")
OPTIONAL_FILES = ("claims",)
# Per file, when the caller knows the sizes (SPEC.md section 6; the JSON as Leo's runner).
FILE_MAX = {"script": checks.SCRIPT_MAX_BYTES, "explainer_json": checks.EXPLAINER_JSON_MAX,
            "explainer_html": checks.HTML_MAX_BYTES, "claims": 64 * 1024}
MANIFEST_MAX = 1024 * 1024
MAX_LINKS = 2000
MAX_AUTHORS = 5000
MAX_TAGS = 10

PAPER_ID = re.compile(r"^p_[a-z0-9]{4,40}$")      # "p_" + 12 lowercase base32 today
CLAIM_ID = re.compile(r"^c_[a-z0-9]{4,40}$")
ARXIV = re.compile(r"^(?:\d{4}\.\d{4,5}|[a-z]+(?:-[a-z]+)*(?:\.[A-Z]{2})?/\d{7})$")
ARXIV_VERSION = re.compile(r"v\d+$")
DOI = re.compile(r"^10\.\d{4,9}/\S+$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
URL = re.compile(r"^https?://[^\s]+$")
SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
VERSION = re.compile(r"^[0-9A-Za-z][0-9A-Za-z.+-]{0,39}$")
DIRECTIONS = ("builds_on", "built_on_by")
GRADES = ("e", "s", "w")
SOURCES = ("s2", "text")
OTHER_FORMS = ("paper_id", "arxiv_id", "doi", "title")


def safe_name(name) -> bool:
    """A plain file name at the bundle's root: no slash, backslash or "..", no leading dot or
    dash, at most 100 characters of letters, digits, dot, dash and underscore."""
    return isinstance(name, str) and bool(SAFE_NAME.match(name)) and ".." not in name


def _int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and v == v and v not in (
        float("inf"), float("-inf"))


def _text(out, label, v, max_len, required=True) -> bool:
    """A non-empty string of at most max_len characters; appends the problem, returns ok."""
    if v is None and not required:
        return True
    if not isinstance(v, str) or not v.strip():
        out.append(f"{label}: must be non-empty text")
        return False
    if len(v) > max_len:
        out.append(f"{label}: {len(v)} characters, at most {max_len}")
        return False
    return True


def _arxiv(out, label, v) -> None:
    if not _text(out, label, v, 40):
        return
    if ARXIV.match(v):
        return
    if ARXIV_VERSION.search(v) and ARXIV.match(ARXIV_VERSION.sub("", v)):
        out.append(f"{label}: without the version ({ARXIV_VERSION.sub('', v)}, not {v})")
    else:
        out.append(f"{label}: not an arXiv id ({v[:40]!r}; like 2210.02747 or hep-th/9901001)")


def _doi(out, label, v) -> None:
    if _text(out, label, v, 300) and not DOI.match(v):
        out.append(f"{label}: not a DOI ({v[:60]!r}; like 10.1038/nature14539)")


def _paper(out, p) -> None:
    if not isinstance(p, dict):
        out.append("paper: must be an object with at least a title")
        return
    _text(out, "paper.title", p.get("title"), 500)
    authors = p.get("authors", [])
    if not isinstance(authors, list):
        out.append("paper.authors: must be a list of names")
    elif len(authors) > MAX_AUTHORS:
        out.append(f"paper.authors: {len(authors)} names, at most {MAX_AUTHORS}")
    else:
        bad = [i for i, a in enumerate(authors) if not isinstance(a, str) or not a.strip() or len(a) > 300]
        if bad:
            out.append(f"paper.authors: item {bad[0]} is not a name (non-empty text, at most 300 "
                       "characters)")
    year = p.get("year")
    if year is not None and not (_int(year) and 1600 <= year <= 2100):
        out.append("paper.year: a year between 1600 and 2100, or null")
    if p.get("arxiv_id") is not None:
        _arxiv(out, "paper.arxiv_id", p["arxiv_id"])
    if p.get("doi") is not None:
        _doi(out, "paper.doi", p["doi"])
    url = p.get("url")
    if url is not None and _text(out, "paper.url", url, 2000) and not URL.match(url):
        out.append("paper.url: must be an http or https address")
    sha = p.get("source_sha256")
    if sha is not None and not (isinstance(sha, str) and SHA256.match(sha)):
        out.append("paper.source_sha256: 64 lowercase hex characters, or null")
    tags = p.get("tags", [])
    if not isinstance(tags, list) or len(tags) > MAX_TAGS or not all(
            isinstance(t, str) and t.strip() and len(t) <= 60 for t in tags):
        out.append(f"paper.tags: a list of at most {MAX_TAGS} tags, each non-empty text of at most "
                   "60 characters")


def _prefs(out, pr) -> None:
    if not isinstance(pr, dict):
        out.append('prefs: must be an object {"settings": {...}, "note": "...", "version": N}')
        return
    out += [f"prefs: {p}" for p in prefs.validate(pr.get("settings", {}), pr.get("note", ""))]
    v = pr.get("version")
    if v is not None and not (_int(v) and v >= 0):
        out.append("prefs.version: a whole number, or null")


def _files(out, files, names: set) -> None:
    if not isinstance(files, dict):
        out.append('files: must be an object naming "script", "explainer_json" and "explainer_html"')
        return
    seen: dict = {}
    for k in REQUIRED_FILES + OPTIONAL_FILES:
        v = files.get(k)
        if v is None:
            if k in REQUIRED_FILES:
                out.append(f"files.{k} is required")
            continue
        if not safe_name(v):
            out.append(f"files.{k}: {str(v)[:60]!r} is not a plain file name (no slashes or '..')")
        elif v == "manifest.json":
            out.append(f"files.{k}: cannot be manifest.json")
        elif v in seen:
            out.append(f"files.{k}: names {v!r}, as files.{seen[v]} does")
        elif v not in names:
            out.append(f"files.{k} names {v!r}, which is not in the bundle")
        else:
            seen[v] = k


def _link(out, i, link) -> None:
    label = f"links[{i}]"
    if not isinstance(link, dict):
        out.append(f"{label}: must be an object with other, direction, grade and source")
        return
    other = link.get("other")
    forms = [k for k in OTHER_FORMS if isinstance(other, dict) and other.get(k) is not None]
    if not isinstance(other, dict) or len(forms) != 1:
        out.append(f"{label}.other: exactly one of " + ", ".join(OTHER_FORMS))
    else:
        k, v = forms[0], other[forms[0]]
        if k == "paper_id":
            if not (isinstance(v, str) and PAPER_ID.match(v)):
                out.append(f"{label}.other.paper_id: not a paper id (p_ and lowercase letters or digits)")
        elif k == "arxiv_id":
            _arxiv(out, f"{label}.other.arxiv_id", v)
        elif k == "doi":
            _doi(out, f"{label}.other.doi", v)
        else:
            _text(out, f"{label}.other.title", v, 500)
    if link.get("direction") not in DIRECTIONS:
        out.append(f"{label}.direction: builds_on or built_on_by")
    if link.get("grade") not in GRADES:
        out.append(f"{label}.grade: e, s or w")
    if link.get("source") not in SOURCES:
        out.append(f"{label}.source: s2 or text")


def _announce(out, a) -> None:
    """"announce": {"slack": true}: the uploader asked for the episode to be posted to the
    group's Slack channel once it is ready (papercast add's question; the hub posts)."""
    if a is not None and not (isinstance(a, dict) and isinstance(a.get("slack", False), bool)):
        out.append('announce: an object like {"slack": true}, or left out')


def announce(answer) -> dict:
    """The manifest's "announce" for a job's answer to add's question: {} when there is none."""
    if isinstance(answer, dict) and isinstance(answer.get("slack"), bool):
        return {"announce": {"slack": answer["slack"]}}
    return {}


def validate(manifest, names) -> list[str]:
    """Problems with a manifest, given the file names in the bundle (the tar's member names as
    stored): [] when fine. `names` may be a dict {name: size in bytes}; then each file's size and
    the total are checked too. Whether a member is a plain file (not a directory or a link) is
    the caller's to check while reading the tar."""
    out: list[str] = []
    sizes = dict(names) if isinstance(names, dict) else None
    names = set(names or ())
    unsafe = sorted(str(n)[:60] for n in names if not safe_name(n))
    if unsafe:
        out.append("the bundle holds files that are not plain names at its root (no folders, "
                   "slashes or '..'): " + ", ".join(repr(n) for n in unsafe[:5]))
    if not isinstance(manifest, dict):
        return out + ["manifest.json is not a JSON object"]
    m = manifest
    if not (_int(m.get("manifest_version")) and m["manifest_version"] == MANIFEST_VERSION):
        out.append(f"manifest_version must be {MANIFEST_VERSION}")
    cv = m.get("client_version")
    if not (isinstance(cv, str) and VERSION.match(cv)):
        out.append("client_version: the client's version, like 0.1.0")
    bv = m.get("base_version")
    if not (_int(bv) and bv >= 1):
        out.append("base_version: the base prompt version the episode was made with (1 or more)")
    _prefs(out, m.get("prefs"))
    model = m.get("model")
    if _text(out, "model", model, 100) and any(c.isspace() or ord(c) < 32 for c in model):
        out.append("model: a model name, with no spaces")
    pid, cid = m.get("paper_id"), m.get("claim_id")
    if pid is not None and not (isinstance(pid, str) and PAPER_ID.match(pid)):
        out.append("paper_id: not a paper id (p_ and lowercase letters or digits), or null")
    if cid is not None and not (isinstance(cid, str) and CLAIM_ID.match(cid)):
        out.append("claim_id: not a claim id (c_ and lowercase letters or digits), or null")
    if not pid and not cid:
        out.append("paper_id (a new version) or claim_id (a new paper) is required")
    _paper(out, m.get("paper"))
    _files(out, m.get("files"), names)
    links = m.get("links", [])
    if not isinstance(links, list):
        out.append("links: must be a list")
    elif len(links) > MAX_LINKS:
        out.append(f"links: {len(links)} links, at most {MAX_LINKS}")
    else:
        for i, link in enumerate(links):
            _link(out, i, link)
    stats = m.get("stats", {})
    if not isinstance(stats, dict):
        out.append("stats: must be an object")
    else:
        if stats.get("words") is not None and not (_int(stats["words"]) and stats["words"] >= 0):
            out.append("stats.words: a whole number")
        for k in ("est_minutes", "wall_s"):
            if stats.get(k) is not None and not (_num(stats[k]) and stats[k] >= 0):
                out.append(f"stats.{k}: a number, zero or more")
    _announce(out, m.get("announce"))
    if sizes is not None:
        files = m.get("files") if isinstance(m.get("files"), dict) else {}
        for k, limit in FILE_MAX.items():
            n = sizes.get(files.get(k)) if isinstance(files.get(k), str) else None
            if _int(n) and n > limit:
                out.append(f"files.{k}: {files[k]} is {_size(n)}, at most {_size(limit)}")
        n = sizes.get("manifest.json")
        if _int(n) and n > MANIFEST_MAX:
            out.append(f"manifest.json is {_size(n)}, at most {_size(MANIFEST_MAX)}")
        total = sum(n for n in sizes.values() if _int(n))
        if total > MAX_BYTES:
            out.append(f"the bundle's files come to {_size(total)}, at most {_size(MAX_BYTES)}")
    return out


def _size(n: int) -> str:
    return f"{n / 2**20:.1f} MB" if n >= 2**20 else f"{n / 1024:.0f} kB"


def summary(manifest) -> str:
    """One line for the hub's log about a bundle, whatever state its manifest is in:
    '"Flow Matching" (2022, arXiv 2210.02747): new paper, claim c_...; base 1, client 0.1.0,
    claude-opus-5-5; prefs derivations · practical; 3100 words, 21.0 min; 4 links'."""
    if not isinstance(manifest, dict):
        return "manifest: not a JSON object"
    m = manifest
    p = m.get("paper") if isinstance(m.get("paper"), dict) else {}

    def short(v, n=80):
        s = " ".join(str(v).split()) if v is not None else ""
        return s if len(s) <= n else s[:n - 1] + "…"

    ids = []
    if p.get("year") is not None:
        ids.append(short(p["year"], 8))
    for k, name in (("arxiv_id", "arXiv"), ("doi", "doi")):
        if p.get(k):
            ids.append(f"{name} {short(p[k], 60)}")
    if not p.get("arxiv_id") and not p.get("doi") and p.get("source_sha256"):
        ids.append(f"sha256 {short(p['source_sha256'], 12)}")
    head = f'"{short(p.get("title") or "untitled")}"' + (f" ({', '.join(ids)})" if ids else "")
    if m.get("paper_id"):
        what = f"new version of {short(m['paper_id'], 44)}"
    elif m.get("claim_id"):
        what = f"new paper, claim {short(m['claim_id'], 44)}"
    else:
        what = "no paper_id or claim_id"
    made = ", ".join(f"{label}{short(m[k], n)}" for k, label, n in (
        ("base_version", "base ", 8), ("client_version", "client ", 20), ("model", "", 40))
        if m.get(k) is not None)
    pr = m.get("prefs") if isinstance(m.get("prefs"), dict) else {}
    ps = prefs.summary(pr.get("settings") if isinstance(pr.get("settings"), dict) else {})
    note = " + note" if isinstance(pr.get("note"), str) and pr["note"].strip() else ""
    st = m.get("stats") if isinstance(m.get("stats"), dict) else {}
    size = []
    if _int(st.get("words")):
        size.append(f"{st['words']} words")
    if _num(st.get("est_minutes")):
        size.append(f"{st['est_minutes']:.1f} min")
    links = m.get("links")
    n_links = len(links) if isinstance(links, list) else 0
    parts = [f"{head}: {what}", made, f"prefs {ps or 'default'}{note}"]
    if size:
        parts.append(", ".join(size))
    parts.append(f"{n_links} link{'' if n_links == 1 else 's'}")
    return "; ".join(x for x in parts if x)
