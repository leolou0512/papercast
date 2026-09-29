"""Links from this paper to papers already in the group's library, from Leo's lineage pipeline
(/home/leo/papercast-itest/lineage: fetch.py resolves papers on Semantic Scholar and reads their
references, build.py matches them to the library, judge2.py grades each candidate with a cheap
model). Here for one paper, both ways, first found by cheap deterministic filters:

- this paper builds on (`builds_on`): its Semantic Scholar references that are in the library
  (source "s2"), and the library papers its own text names by arXiv id, DOI or title (source
  "text"; needs pdftotext);
- the library builds on it (`built_on_by`): the library papers that cite it on Semantic Scholar
  ("s2"), and those whose own text names it, which the hub finds in its full-text index (GET
  /api/cli/mentions, "text"): so an older library paper is linked even when Semantic Scholar's
  citations of this one are missing or late.

Found both ways is source "both". Text matching is common/mentions.py's (the same rules as the
hub's): never a title too short or generic to match safely, never the paper itself or another
version of it. At most MAX_PER_DIRECTION candidates each way (those found both ways first), the
rest listed in info["dropped"].

Every candidate is graded essential / strong / weak / none by `claude -p --model
claude-haiku-4-5` with no tools, one call per up to 50 candidates grouped by child paper, as
judge2.py; e/s/w are kept, none is dropped. Papers not in the library are never links.

Network: only api.semanticscholar.org, through `fetch(url) -> (status, json | None[, headers])`,
which a test replaces; `retrying` waits out its rate limit (429, 5xx: backoff with jitter,
Retry-After respected). A Semantic Scholar that still refuses is no failure: the links come from
the texts, and info["s2_error"] says why. Every answer is cached in the job dir, every graded
batch too, so a resumed job asks nothing twice.
"""
from __future__ import annotations

import email.utils
import hashlib
import json
import os
import random
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from ..common import mentions as M

BASE = "https://api.semanticscholar.org/graph/v1"
FIELDS = "paperId,externalIds,title,year"
REF_FIELDS = "isInfluential,paperId,title,year,externalIds"
USER_AGENT = "papercast-group/0.1 (research group podcast library)"
MAX_BODY = 32 << 20
MAX_PAGES = 10                 # 10 x 1000 references or citations: more than any library holds
S2_WINDOW = 10_000             # S2's graph API serves offset + limit below this, no further
GRADER_MODEL = "claude-haiku-4-5"
BATCH = 50
TRIES = 3                      # grading attempts per batch (judge2.py)
GRADES = ("essential", "strong", "weak", "none")
SHORT = {"essential": "e", "strong": "s", "weak": "w"}
MAX_PER_DIRECTION = 200        # candidates graded each way at most (4 grading calls)
RETRY_TRIES = 8                # 429 and 5xx: tries of one request (waits ~2, 4, 8, 16, 32, 60, 60 s)
NET_TRIES = 3                  # no answer at all (the network, a proxy): tries
BACKOFF_BASE_S = 2.0
BACKOFF_CAP_S = 60.0
RETRY_AFTER_CAP_S = 120.0

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

def http_fetch(url: str, timeout: float = 30.0) -> tuple[int, object, dict]:
    """One GET: (status, parsed JSON or None, headers). Network errors are status 0."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                               "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read(MAX_BODY)
            return r.status, json.loads(body.decode("utf-8", "replace")), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, None, dict(e.headers or {})
    except (urllib.error.URLError, OSError, ValueError):
        return 0, None, {}


def retry_after(headers) -> float | None:
    """Seconds a Retry-After header asks for (a number or an HTTP date), or None."""
    if not headers:
        return None
    v = next((headers[k] for k in headers if str(k).lower() == "retry-after"), None)
    if v is None:
        return None
    v = str(v).strip()
    if re.fullmatch(r"\d+(?:\.\d+)?", v):
        return float(v)
    try:
        t = email.utils.parsedate_to_datetime(v)
    except (TypeError, ValueError):
        return None
    return max(0.0, t.timestamp() - time.time()) if t is not None else None


def retrying(fetch, tries: int = RETRY_TRIES, net_tries: int = NET_TRIES, base: float = BACKOFF_BASE_S,
             cap: float = BACKOFF_CAP_S, sleep=time.sleep, rand=random.random):
    """fetch.py's patience, for many jobs on one machine sharing Semantic Scholar's rate limit:
    429 and 5xx are tried again up to `tries` times, no answer at all `net_tries` times, after
    an exponential wait with jitter (half of it random, so parallel jobs spread out), or after
    Retry-After when the answer gives one (at most RETRY_AFTER_CAP_S). Returns (status, data);
    get.waits lists the waits of the last request."""
    def get(url: str) -> tuple[int, object]:
        n_rate = n_net = 0
        get.waits = []
        while True:
            r = fetch(url)
            status, data = r[0], r[1]
            headers = r[2] if len(r) > 2 else None
            if status == 0:
                n_net += 1
                again = n_net < net_tries
            elif status == 429 or status >= 500:
                n_rate += 1
                again = n_rate < tries
            else:
                return status, data
            if not again:
                return status, data
            d = min(cap, base * 2 ** (n_rate + n_net - 1))
            wait = d / 2 + rand() * d / 2
            ra = retry_after(headers)
            if ra is not None:
                wait = max(wait, min(ra, RETRY_AFTER_CAP_S) + rand())
            get.waits.append(wait)
            sleep(wait)
    get.waits = []
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
        status, data = self.fetch(url)[:2]
        if status == 404:
            data = None
        elif status != 200:
            what = {0: "nothing", 429: "429 (rate limited)"}.get(status, status)
            raise OSError(f"Semantic Scholar answered {what} for {path}")
        tmp = f"{cp}.{os.getpid()}.{threading.get_ident()}.tmp"     # several threads may share the cache
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
        [{isInfluential, citingPaper}]) of one paper, every page S2 serves: offset + limit stays
        under S2_WINDOW (S2 refuses past it, e.g. for a paper with 9,000+ citations), and a page
        refused after the first keeps the pages already fetched."""
        out, off = [], 0
        for _ in range(MAX_PAGES):
            limit = min(1000, S2_WINDOW - 1 - off)
            if limit <= 0:
                break
            try:
                j = self.get(f"/paper/{pid}/{kind}", {"fields": REF_FIELDS, "limit": limit, "offset": off})
            except OSError:
                if out:
                    break
                raise
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

    def versions_of(self, paper: dict) -> set:
        """Library papers that are this paper or another version of it (the same arXiv id, DOI
        or title): never a link."""
        return {p["id"] for p in self.papers if M.same_work(paper, p)}

    def mentioned_in(self, body: str, exclude: set) -> dict[str, str]:
        """{library id: "arxiv" | "doi" | "title"} of the library papers this text names: by
        arXiv id (any version), DOI, or title (common/mentions.py: the first words of it, across
        line breaks and hyphenation; never a short or generic title, and not when every
        occurrence is inside a longer library title, "Improved <t>")."""
        if not body:
            return {}
        sq = M.Squashed(body)
        ids = M.arxiv_ids(body)
        full = {p["id"]: M.squash(p.get("title") or "") for p in self.papers}
        found = {}
        for p in self.papers:
            pid = p["id"]
            if pid in exclude:
                continue
            if base_arxiv(p.get("arxiv_id")) in ids:
                found[pid] = "arxiv"
                continue
            if p.get("doi") and M.squash(p["doi"]) in sq.s and M.doi_in(body, p["doi"]):
                found[pid] = "doi"
                continue
            keys = M.title_keys(p.get("title") or "")
            if not keys or not any(k in sq.s for k in keys):
                continue
            longer = [L for q, L in full.items() if q != pid and q not in exclude and L != full[pid]
                      and any(len(L) > len(k) and k in L for k in keys)]
            if M.title_in(sq, p.get("title") or "", longer):
                found[pid] = "title"
        return found


def label(title: str) -> str:
    """A short name for the grader (build.py): the part before a colon, else three words."""
    m = re.match(r"^([^:]{2,24}):", title or "")
    return m.group(1).strip() if m else " ".join((title or "").split()[:3])


# --- candidates ------------------------------------------------------------------------------

RANK = {"both": 0, "s2": 1, "text": 2}
VIA_RANK = {"arxiv": 0, "doi": 1, "title": 2}


def candidates(paper: dict, lib: Library, s2: S2 | None, text: str | None, exclude: set,
               mentions: list | None = None, named: dict | None = None) -> tuple[list[dict], dict]:
    """[{"other": library id, "direction", "source": "s2" | "text" | "both", "influential",
    "via"}], info. Both ways from Semantic Scholar, the paper's own text (what it names) and
    `mentions` (the hub's answer: library papers whose text names this one), merged; a
    Semantic Scholar that fails in any part leaves the rest standing (info["s2_error"]).
    `named` ({library id: "arxiv" | "doi" | "title"}): what the paper's own text names, found
    elsewhere (papercast relink: the hub's text index), taken as `text`'s findings are."""
    info: dict = {"s2_id": None, "resolved_via": None, "refs": 0, "cits": 0}
    exclude = set(exclude)
    versions = lib.versions_of(paper) - exclude
    if versions:
        info["versions"] = sorted(versions)
        exclude |= versions
    out: dict[tuple, dict] = {}
    year = paper.get("year")
    errors: list[str] = []

    def later(kind: str, other: str, oy) -> bool:
        """An earlier paper is built on, a later one builds on it (build.py drops a "parent"
        later than its child)."""
        oy = oy or lib.by_id[other].get("year")
        return bool(year and oy and ((kind == "builds_on" and oy > year) or
                                     (kind == "built_on_by" and oy < year)))

    def add(other: str, kind: str, source: str, influential: bool = False, via: str | None = None):
        c = out.setdefault((other, kind), {"other": other, "direction": kind, "source": source,
                                           "influential": False, "via": []})
        if c["source"] != source:
            c["source"] = "both"
        c["influential"] = c["influential"] or influential
        if via and via not in c["via"]:
            c["via"].append(via)

    if s2 is not None:
        rec = None
        try:
            rec, how = s2.resolve(paper)
        except OSError as e:
            errors.append(str(e))
        if rec:
            info["s2_id"], info["resolved_via"] = rec["paperId"], how
            year = year or rec.get("year")
            for kind, path, key, n in (("builds_on", "references", "citedPaper", "refs"),
                                       ("built_on_by", "citations", "citingPaper", "cits")):
                try:
                    rows = s2.edges(rec["paperId"], path)
                except OSError as e:
                    errors.append(str(e))
                    continue
                info[n] = len(rows)
                for row in rows:
                    cp = row.get(key)
                    other = lib.match(cp)
                    if not other or other in exclude or later(kind, other, (cp or {}).get("year")):
                        continue
                    add(other, kind, "s2", bool(row.get("isInfluential")), "s2")
    if errors:
        info["s2_error"] = "; ".join(errors)
    found = dict(lib.mentioned_in(text, exclude)) if text else {}
    for other, field in (named or {}).items():
        if other in lib.by_id and other not in exclude:
            found.setdefault(other, field)
    if text or named is not None:
        info["text_names"] = len(found)
    for other, field in found.items():
        if not later("builds_on", other, None):
            add(other, "builds_on", "text", via="text:" + field)
    if mentions is not None:
        n = 0
        for m in mentions:
            other = m.get("paper_id") if isinstance(m, dict) else None
            if other not in lib.by_id or other in exclude:
                continue
            n += 1
            if not later("built_on_by", other, None):
                add(other, "built_on_by", "text", via="mention:" + str(m.get("field") or "text"))
        info["mentioned_by"] = n
    # at most MAX_PER_DIRECTION each way: found both ways first, then Semantic Scholar's
    # (influential first), then the texts' (an id before a title)
    kept, dropped = [], {}
    for kind in ("builds_on", "built_on_by"):
        cs = sorted((c for c in out.values() if c["direction"] == kind),
                    key=lambda c: (RANK[c["source"]], not c["influential"],
                                   min((VIA_RANK.get(v.split(":")[-1], 3) for v in c["via"]), default=3), c["other"]))
        kept += cs[:MAX_PER_DIRECTION]
        if len(cs) > MAX_PER_DIRECTION:
            dropped[kind] = [{"other": c["other"], "source": c["source"]} for c in cs[MAX_PER_DIRECTION:]]
    if dropped:
        info["dropped"] = dropped
    info["candidates"] = len(kept)
    info["sources"] = {k: {s: sum(1 for c in kept if c["direction"] == k and c["source"] == s)
                           for s in ("s2", "text", "both")} for k in ("builds_on", "built_on_by")}
    return kept, info


# --- grading (judge2.py) ---------------------------------------------------------------------

def batches(cands: list[dict], this_id: str) -> list[list[tuple]]:
    """[(child, parent, cand)] in batches of at most BATCH, grouped by child (judge2.py): this
    paper is the child of every builds_on candidate and the parent of every built_on_by one."""
    rows = [((this_id, c["other"]) if c["direction"] == "builds_on" else (c["other"], this_id)) + (c,)
            for c in cands]
    return pack(rows, first=this_id)


def pack(rows: list[tuple], first: str | None = None) -> list[list[tuple]]:
    """Rows (child, parent, cand) in batches of at most BATCH, each child's rows together (a
    child with more than BATCH is split), children in id order (`first` first)."""
    by_child: dict[str, list] = {}
    for r in rows:
        by_child.setdefault(r[0], []).append(r)
    out, cur = [], []
    for child in sorted(by_child, key=lambda c: (c != first, c)):
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


def grade_batches(bs: list[list[tuple]], info: dict, grade, cache_dir: str,
                  progress=None) -> tuple[list[tuple], dict]:
    """Grade batches of rows (child, parent, cand); `info`: id -> {"title", "year", "claims"}.
    -> ([(row, "essential" | "strong" | "weak" | "none")] for every row graded, stats). A batch
    whose answers stay unusable after TRIES is skipped (stats["skipped"], ["errors"]); any other
    exception from `grade` goes to the caller. Every graded batch is cached in cache_dir under
    its prompt's hash."""
    os.makedirs(cache_dir, exist_ok=True)
    graded, stats = [], {g: 0 for g in GRADES}
    stats.update(batches=0, skipped=0)
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
            tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"model": GRADER_MODEL, "answers": ans}, fh)
            os.replace(tmp, path)
        stats["batches"] += 1
        for k, row in enumerate(batch, 1):
            stats[ans[k]] += 1
            graded.append((row, ans[k]))
        if progress:
            progress(bi + 1, len(bs))
    return graded, stats


def grade_all(cands: list[dict], this: dict, lib: Library, grade, cache_dir: str,
              progress=None) -> tuple[list[dict], dict]:
    """(links as the bundle carries them, info). `grade(prompt) -> answer text` is one tool-free
    haiku call; GradeError from it is tried again (TRIES), then the batch is skipped. Any other
    exception (the usage limit) goes to the caller; finished batches stay cached."""
    this_id = "this"
    info = {this_id: {"title": this.get("title") or "", "year": this.get("year"),
                      "claims": this.get("claims") or []}}
    for c in cands:
        p = lib.by_id[c["other"]]
        info[c["other"]] = {"title": p.get("title") or "", "year": p.get("year"), "claims": []}
    graded, stats = grade_batches(batches(cands, this_id), info, grade, cache_dir, progress)
    links = [{"other": {"paper_id": c["other"]}, "direction": c["direction"], "grade": SHORT[g],
              "source": c["source"], "influential": bool(c.get("influential"))}   # the hub's rule breaks ties by it
             for (_, _, c), g in graded if g in SHORT]
    return links, stats


def find(paper: dict, claims: list[str], library: list[dict], fetch, grade, cache_dir: str,
         exclude: set | None = None, text: str | None = None, progress=None,
         mentions: list | None = None) -> dict:
    """Everything for one paper: {"links": [...], "s2_id", "info"}. `fetch` None: no Semantic
    Scholar (the texts only). `mentions`: the hub's GET /api/cli/mentions answer (None: not
    asked, or the hub could not say)."""
    lib = Library(library)
    exclude = set(exclude or ())
    s2 = S2(fetch, os.path.join(cache_dir, "s2")) if fetch else None
    cands, info = candidates(paper, lib, s2, text, exclude, mentions)
    links, stats = grade_all(cands, dict(paper, claims=claims), lib, grade,
                             os.path.join(cache_dir, "grades"), progress) if cands else ([], {})
    info["grades"] = stats
    info["candidate_list"] = [{"other": c["other"], "direction": c["direction"], "source": c["source"],
                               "via": c["via"]} for c in cands]
    return {"links": links, "s2_id": info.get("s2_id"), "info": info}
