"""Links from this paper to papers already in the group's library, from Leo's lineage pipeline
(/home/leo/papercast-itest/lineage: fetch.py resolves papers on Semantic Scholar and reads their
references, build.py matches them to the library, judge2.py grades each candidate with a cheap
model). Here for one paper, both ways:

- its references in the library: this paper builds on them (`builds_on`);
- the library papers that cite it: they build on it (`built_on_by`);
- when Semantic Scholar has no references for it: library titles found verbatim in the paper's
  own text (`source: "text"`, build.py's fallback; needs pdftotext).

Every candidate is graded essential / strong / weak / none by `claude -p --model
claude-haiku-4-5` with no tools, one call per up to 50 candidates grouped by child paper, as
judge2.py; e/s/w are kept, none is dropped. Papers not in the library are never links.

Network: only api.semanticscholar.org, through `fetch(url) -> (status, json | None)`, which a
test replaces. Every answer is cached in the job dir, every graded batch too, so a resumed job
asks nothing twice.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

BASE = "https://api.semanticscholar.org/graph/v1"
FIELDS = "paperId,externalIds,title,year"
REF_FIELDS = "isInfluential,paperId,title,year,externalIds"
USER_AGENT = "papercast-group/0.1 (research group podcast library)"
MAX_BODY = 32 << 20
MAX_PAGES = 10                 # 10 x 1000 references or citations: more than any library holds
GRADER_MODEL = "claude-haiku-4-5"
BATCH = 50
TRIES = 3                      # grading attempts per batch (judge2.py)
GRADES = ("essential", "strong", "weak", "none")
SHORT = {"essential": "e", "strong": "s", "weak": "w"}
TEXT_MIN = 22                  # a title shorter than this (letters and digits) is never matched in text

SYSTEM = (
    "You grade edges for a lineage map of research papers. Each CHILD paper cites each of its numbered candidate "
    "PARENT papers. Grade how much the child is built on the basis of that parent:\n"
    "- essential: a direct successor. The child extends, modifies, scales up or fine-tunes the parent's specific "
    "method or model, and would not exist without it.\n"
    "- strong: the parent's method, model, theory or dataset is a main ingredient of the child's approach.\n"
    "- weak: the child uses something from the parent in a minor role (a component, a training technique, a "
    "formulation it adopts), or builds on it only loosely.\n"
    "- none: the parent is related work, a baseline or competitor, a benchmark or tool used only for evaluation, "
    "or general background.\n"
    "Be strict. A paper cites many works it does not build on, so most candidates are usually weak or none: use none "
    "for a parent that is only compared against, surveyed or mentioned, even when it is in the same area, and keep "
    "essential for the one or few papers the child is a direct successor of. "
    "Judge from the titles, years, short names and the child's claims. Reply with JSON only, no prose: "
    "{\"answers\": [{\"i\": <number>, \"g\": \"essential\"|\"strong\"|\"weak\"|\"none\"}, ...]} with exactly one "
    "entry per candidate number.")


class GradeError(Exception):
    """One grading call gave no usable answer (the batch is tried again, then skipped)."""


# --- Semantic Scholar ---------------------------------------------------------------

def http_fetch(url: str, timeout: float = 30.0) -> tuple[int, object]:
    """One GET: (status, parsed JSON or None). Network errors are status 0."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                               "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read(MAX_BODY)
            return r.status, json.loads(body.decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        return e.code, None
    except (urllib.error.URLError, OSError, ValueError):
        return 0, None


def retrying(fetch, tries: int = 6, delay: float = 3.0, sleep=time.sleep):
    """fetch.py's patience: Semantic Scholar's shared rate limit answers 429 often."""
    def get(url: str) -> tuple[int, object]:
        d = delay
        status, data = 0, None
        for i in range(tries):
            status, data = fetch(url)
            if status == 429 or status == 0 or status >= 500:
                if i + 1 < tries:
                    sleep(d)
                    d = min(d * 1.7, 90.0)
                continue
            return status, data
        return status, data
    return get


class S2:
    """Semantic Scholar through `fetch`, every answer cached as a file in `cache_dir`."""

    def __init__(self, fetch, cache_dir: str):
        self.fetch, self.cache_dir = fetch, cache_dir
        os.makedirs(cache_dir, exist_ok=True)

    def get(self, path: str, params: dict) -> object | None:
        """The JSON answer, None for 404; raises OSError when S2 cannot be reached."""
        url = f"{BASE}{path}?{urllib.parse.urlencode(params)}"
        key = hashlib.sha256(url.encode()).hexdigest()[:20]
        cp = os.path.join(self.cache_dir, key + ".json")
        if os.path.exists(cp):
            with open(cp, encoding="utf-8") as fh:
                return json.load(fh)["data"]
        status, data = self.fetch(url)
        if status == 404:
            data = None
        elif status != 200:
            raise OSError(f"Semantic Scholar answered {status or 'nothing'} for {path}")
        tmp = cp + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"url": url, "data": data}, fh)
        os.replace(tmp, cp)
        return data

    def resolve(self, paper: dict) -> tuple[dict | None, str | None]:
        """(S2 record, how): by arXiv id, then DOI, then the title (a match only when the
        normalised titles agree, fetch.py's rule)."""
        for key, how in ((paper.get("arxiv_id"), "arxiv"), (paper.get("doi"), "doi")):
            if key:
                pid = ("ARXIV:" if how == "arxiv" else "DOI:") + key
                v = self.get(f"/paper/{urllib.parse.quote(pid, safe=':/')}", {"fields": FIELDS})
                if isinstance(v, dict) and v.get("paperId"):
                    return v, how
        t = paper.get("title")
        if t:
            d = self.get("/paper/search/match", {"query": t, "fields": FIELDS})
            v = ((d or {}).get("data") or [None])[0] if isinstance(d, dict) else None
            if isinstance(v, dict) and v.get("paperId"):
                a, b = squash(v.get("title")), squash(t)
                if a == b or (min(len(a), len(b)) > 20 and (a.startswith(b) or b.startswith(a))):
                    return v, "title"
        return None, None

    def edges(self, pid: str, kind: str) -> list[dict]:
        """References ("references": [{isInfluential, citedPaper}]) or citations ("citations":
        [{isInfluential, citingPaper}]) of one paper, every page."""
        out, off = [], 0
        for _ in range(MAX_PAGES):
            j = self.get(f"/paper/{pid}/{kind}", {"fields": REF_FIELDS, "limit": 1000, "offset": off})
            if not isinstance(j, dict):
                break
            out += [x for x in (j.get("data") or []) if isinstance(x, dict)]
            if j.get("next") is None:
                break
            off = j["next"]
        return out


# --- matching against the library (build.py) ------------------------------------------------

def squash(t) -> str:
    return re.sub(r"[^a-z0-9]", "", (t or "").lower()) if isinstance(t, str) else ""


def ntext(t: str) -> str:
    return re.sub(r"[^a-z0-9]", "", unicodedata.normalize("NFKC", t or "").lower())


def base_arxiv(a) -> str | None:
    return re.sub(r"v\d+$", "", a) if isinstance(a, str) and a else None


class Library:
    """The group's library (`GET /api/cli/library`), indexed as build.py does."""

    def __init__(self, papers: list[dict]):
        self.papers = [p for p in papers if isinstance(p, dict) and p.get("id")]
        self.by_id = {p["id"]: p for p in self.papers}
        self.by_s2 = {p["s2_id"]: p["id"] for p in self.papers if p.get("s2_id")}
        self.by_ax = {base_arxiv(p["arxiv_id"]): p["id"] for p in self.papers if p.get("arxiv_id")}
        self.by_doi = {p["doi"].lower(): p["id"] for p in self.papers if isinstance(p.get("doi"), str) and p["doi"]}
        self.by_title = {squash(p.get("title")): p["id"] for p in self.papers if squash(p.get("title"))}

    def match(self, cp: dict | None) -> str | None:
        if not isinstance(cp, dict):
            return None
        if cp.get("paperId") in self.by_s2:
            return self.by_s2[cp["paperId"]]
        ext = cp.get("externalIds") or {}
        ax = base_arxiv(ext.get("ArXiv"))
        if ax and ax in self.by_ax:
            return self.by_ax[ax]
        if isinstance(ext.get("DOI"), str) and ext["DOI"].lower() in self.by_doi:
            return self.by_doi[ext["DOI"].lower()]
        t = squash(cp.get("title"))
        if len(t) >= 12 and t in self.by_title:
            return self.by_title[t]
        return None

    def in_text(self, body: str, exclude: set) -> list[str]:
        """Library papers whose title is in the paper's text (build.py's fallback): titles of
        22+ letters and digits only, and not when every occurrence is inside a longer library
        title ("Improved <t>")."""
        body = ntext(body)
        nt = {p["id"]: ntext(p.get("title") or "") for p in self.papers}
        found = []
        for pid, t in nt.items():
            if pid in exclude or len(t) < TEXT_MIN or t not in body:
                continue
            longer = sum(body.count(T) * T.count(t) for q, T in nt.items()
                         if q != pid and len(T) > len(t) and t in T)
            if body.count(t) <= longer:
                continue
            found.append(pid)
        return found


def label(title: str) -> str:
    """A short name for the grader (build.py): the part before a colon, else three words."""
    m = re.match(r"^([^:]{2,24}):", title or "")
    return m.group(1).strip() if m else " ".join((title or "").split()[:3])


# --- candidates ------------------------------------------------------------------------------

def candidates(paper: dict, lib: Library, s2: S2 | None, text: str | None,
               exclude: set) -> tuple[list[dict], dict]:
    """[{"other": library id, "direction", "source", "influential"}], info. S2 first; the text
    fallback when S2 has no references for the paper (not found, or an empty list)."""
    info: dict = {"s2_id": None, "resolved_via": None, "refs": 0, "cits": 0}
    out: dict[tuple, dict] = {}
    year = paper.get("year")
    refs: list = []
    if s2 is not None:
        rec, how = s2.resolve(paper)
        if rec:
            info["s2_id"], info["resolved_via"] = rec["paperId"], how
            year = year or rec.get("year")
            refs = s2.edges(rec["paperId"], "references")
            cits = s2.edges(rec["paperId"], "citations")
            info["refs"], info["cits"] = len(refs), len(cits)
            for kind, rows, key in (("builds_on", refs, "citedPaper"),
                                    ("built_on_by", cits, "citingPaper")):
                for row in rows:
                    cp = row.get(key)
                    other = lib.match(cp)
                    if not other or other in exclude:
                        continue
                    oy = (cp or {}).get("year") or lib.by_id[other].get("year")
                    # an earlier paper is built on, a later one builds on it (build.py drops a
                    # "parent" later than its child)
                    if year and oy and ((kind == "builds_on" and oy > year) or
                                        (kind == "built_on_by" and oy < year)):
                        continue
                    k = (other, kind)
                    c = out.setdefault(k, {"other": other, "direction": kind, "source": "s2",
                                           "influential": False})
                    c["influential"] = c["influential"] or bool(row.get("isInfluential"))
    if not refs and text:
        for other in lib.in_text(text, exclude):
            oy = lib.by_id[other].get("year")
            if year and oy and oy > year:
                continue
            out.setdefault((other, "builds_on"), {"other": other, "direction": "builds_on",
                                                  "source": "text", "influential": False})
    info["candidates"] = len(out)
    return list(out.values()), info


# --- grading (judge2.py) ---------------------------------------------------------------------

def batches(cands: list[dict], this_id: str) -> list[list[tuple]]:
    """[(child, parent, cand)] in batches of at most BATCH, grouped by child (judge2.py): this
    paper is the child of every builds_on candidate and the parent of every built_on_by one."""
    rows = [((this_id, c["other"]) if c["direction"] == "builds_on" else (c["other"], this_id)) + (c,)
            for c in cands]
    by_child: dict[str, list] = {}
    for r in rows:
        by_child.setdefault(r[0], []).append(r)
    out, cur = [], []
    for child in sorted(by_child, key=lambda c: (c != this_id, c)):
        es = by_child[child]
        if cur and len(cur) + len(es) > BATCH:
            out.append(cur)
            cur = []
        while len(es) > BATCH:
            out.append(es[:BATCH])
            es = es[BATCH:]
        cur += es
    if cur:
        out.append(cur)
    return out


def prompt(batch: list[tuple], info: dict) -> str:
    """SYSTEM, then judge2's body: each child with its claims and its numbered candidates.
    `info`: id -> {"title", "year", "claims"}."""
    out, k, last = [], 0, None
    for child, parent, _ in batch:
        if child != last:
            c = info[child]
            cl = c.get("claims") or []
            out.append(f"\nCHILD: \"{c['title']}\" ({c.get('year') or 'unknown'}; short name {label(c['title'])})")
            out.append("Child's claims:" + ("".join(f"\n- {x}" for x in cl) if cl else " (none recorded)"))
            out.append("Candidate parents:")
            last = child
        k += 1
        p = info[parent]
        out.append(f"  [{k}] \"{p['title']}\" ({p.get('year') or 'unknown'}; short name {label(p['title'])})")
    return SYSTEM + "\n\n" + "\n".join(out).strip() + f"\n\nGrade all {k} candidates."


def parse(answer: str, n: int) -> dict[int, str]:
    m = re.search(r"\{.*\}", answer or "", re.S)
    if not m:
        raise GradeError("no JSON in the answer")
    try:
        ans = {int(a["i"]): str(a["g"]).lower() for a in json.loads(m.group(0))["answers"]}
    except (ValueError, KeyError, TypeError) as e:
        raise GradeError(f"unreadable answer ({e})")
    if set(ans) != set(range(1, n + 1)) or any(g not in GRADES for g in ans.values()):
        raise GradeError(f"answers for {len(ans)} of {n}")
    return ans


def grade_all(cands: list[dict], this: dict, lib: Library, grade, cache_dir: str,
              progress=None) -> tuple[list[dict], dict]:
    """(links as the bundle carries them, info). `grade(prompt) -> answer text` is one tool-free
    haiku call; GradeError from it is tried again (TRIES), then the batch is skipped. Any other
    exception (the usage limit) goes to the caller; finished batches stay cached."""
    os.makedirs(cache_dir, exist_ok=True)
    this_id = "this"
    info = {this_id: {"title": this.get("title") or "", "year": this.get("year"),
                      "claims": this.get("claims") or []}}
    for c in cands:
        p = lib.by_id[c["other"]]
        info[c["other"]] = {"title": p.get("title") or "", "year": p.get("year"), "claims": []}
    links, stats = [], {g: 0 for g in GRADES}
    stats.update(batches=0, skipped=0)
    bs = batches(cands, this_id)
    for bi, batch in enumerate(bs):
        body = prompt(batch, info)
        key = hashlib.sha256(body.encode()).hexdigest()[:16]
        path = os.path.join(cache_dir, f"b{bi:03d}_{key}.json")
        ans = None
        if os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                ans = {int(k): v for k, v in json.load(fh)["answers"].items()}
        else:
            err = None
            for _ in range(TRIES):
                try:
                    ans = parse(grade(body), len(batch))
                    break
                except GradeError as e:
                    err = e
            if ans is None:
                stats["skipped"] += 1
                stats.setdefault("errors", []).append(str(err))
                continue
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"model": GRADER_MODEL, "answers": ans}, fh)
            os.replace(tmp, path)
        stats["batches"] += 1
        for k, (_, _, c) in enumerate(batch, 1):
            g = ans[k]
            stats[g] += 1
            if g in SHORT:
                links.append({"other": {"paper_id": c["other"]}, "direction": c["direction"],
                              "grade": SHORT[g], "source": c["source"]})
        if progress:
            progress(bi + 1, len(bs))
    return links, stats


def find(paper: dict, claims: list[str], library: list[dict], fetch, grade, cache_dir: str,
         exclude: set | None = None, text: str | None = None, progress=None) -> dict:
    """Everything for one paper: {"links": [...], "s2_id", "info"}. `fetch` None: no Semantic
    Scholar (the text fallback only)."""
    lib = Library(library)
    exclude = set(exclude or ())
    s2 = S2(fetch, os.path.join(cache_dir, "s2")) if fetch else None
    try:
        cands, info = candidates(paper, lib, s2, text, exclude)
    except OSError as e:
        # Semantic Scholar unreachable: the text fallback alone, and say so
        cands, info = candidates(paper, lib, None, text, exclude)
        info["s2_error"] = str(e)
    links, stats = grade_all(cands, dict(paper, claims=claims), lib, grade,
                             os.path.join(cache_dir, "grades"), progress) if cands else ([], {})
    info["grades"] = stats
    return {"links": links, "s2_id": info.get("s2_id"), "info": info}
