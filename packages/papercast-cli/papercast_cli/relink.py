"""`papercast relink`: the links between papers already in the group's library, found again the
way a new upload's are (pipeline/links.py) and sent to the hub, which applies them as the agent
acting for you (POST /api/cli/papers/<id>/links, SPEC.md section 4).

Papers uploaded before the two-way filter existed, or before the papers they build on, can miss
links. For every paper on the map (or the ones named):

1. its candidates both ways, as links.candidates finds them for an upload: its Semantic Scholar
   references and citations in the library ("s2"), the library papers its own text names and
   those whose text names it ("text"). Both text directions come from the hub's text index
   (GET /api/cli/mentions, asked once per library paper: A's text names B exactly when B's
   mentions list A), since the papers' texts stay on the hub;
2. each pair (earlier paper, the paper built on it) once, whichever paper found it. A pair a
   person decided (their link, a link they removed or changed, a dismissed suggestion) or that is
   linked the other way is left out before grading;
3. graded by haiku as an upload's are (links.prompt: each child with its claims, up to 50 pairs a
   call), a few calls at a time. Every pair's grade is kept (pairs.json), so a pair is graded
   once and a second run asks nothing and changes nothing;
4. per paper built on, POST /api/cli/papers/<id>/links with the grades (e/s/w, and "none" for a
   link the agent made that no longer holds) and Semantic Scholar's isInfluential: the hub
   regrades or removes the agent's own links, then chooses the paper's parents by Leo's
   per-paper rule (every essential one, strong ones until it has 5, weak ones only until it has
   4; a person's links stay and count): the ones it chooses are linked, the agent's links it
   does not choose removed. Every paper built on that has a pair or an agent link is sent, with
   nothing to grade too, so the rule is applied to each. With --dry-run it only says what it
   would do.

--restructure: steps 1-3 as above, then every graded pair in one request
(POST /api/cli/relink/restructure): the hub applies the rule to the whole map in one
transaction, parents before children, so the dry run says exactly what the real run does.

Semantic Scholar: every answer cached (relink/s2), requests paced S2_INTERVAL_S apart whichever
thread asks, 429 and 5xx retried with backoff (links.retrying). Everything is kept under
state_dir()/relink: s2/, grades/ (each graded call), pairs.json (each pair's grade; delete it to
grade every pair again), runs/ (each run's result, with the hub's log ids), logs/ (the grader's).
"""
from __future__ import annotations

import json
import shutil
import threading
import time
import urllib.parse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import config
from . import util
from .errors import ApiError, NotFound, PapercastError
from .pipeline import claude, limits
from .pipeline import links as L
from .pipeline.errors import UsageLimit, usage_limit

PARALLEL = 3                    # papers (Semantic Scholar) and grading calls at once
PARALLEL_MAX = 8
S2_INTERVAL_S = 1.1             # between two Semantic Scholar requests, over all threads
POST_MAX = 1000                 # links in one request (the hub takes 2000)
TO_HUB = {"essential": "e", "strong": "s", "weak": "w", "none": "none"}
WORD = {"e": "essential", "s": "strong", "w": "weak", "none": "none"}
RANK_OF = {"e": 0, "s": 1, "w": 2}
_DEFAULT = object()


def root_dir() -> Path:
    return config.state_dir() / "relink"


class Paced:
    """fetch(url), at most once every `interval` seconds whichever thread asks: Semantic Scholar's
    rate limit is per address, shared with the worker's own jobs."""

    def __init__(self, fetch, interval: float | None = None, clock=time.monotonic, sleep=time.sleep):
        self.fetch, self.clock, self.sleep = fetch, clock, sleep
        self.interval = S2_INTERVAL_S if interval is None else interval
        self.lock = threading.Lock()
        self.next = None
        self.calls = 0

    def __call__(self, url: str):
        with self.lock:
            now = self.clock()
            if self.next is not None and self.next > now:
                self.sleep(self.next - now)
                now = self.next
            self.next = now + self.interval
            self.calls += 1
        return self.fetch(url)


def claude_grader(root: Path):
    """grade(prompt) -> answer: one tool-free haiku call, as the pipeline's (Job._grade). Claude's
    usage limit raises UsageLimit; an answer to try again raises links.GradeError."""
    gdir = config.private_dir(root / "grader")
    logs = config.private_dir(root / "logs")
    stamp = time.strftime("%Y%m%d-%H%M%S")
    lock, count = threading.Lock(), [0]

    def grade(body: str) -> str:
        path, _ = claude.resolve()
        if not path:
            raise PapercastError("Claude Code (`claude`) is not installed or not on PATH; the grading needs it")
        with lock:
            count[0] += 1
            n = count[0]
        argv = [path, "-p", body, *claude.lockdown_bare(L.GRADER_MODEL)]
        r = claude.run(argv, cwd=str(gdir), out_path=str(logs / f"{stamp}-{n}.jsonl"),
                       err_path=str(logs / f"{stamp}-{n}.err"), init_check=claude.check_init_bare,
                       timeout_s=600)
        if r.violation:
            raise PapercastError(f"the link grader started with tools, so it was stopped ({r.violation})")
        hit = limits.detect(r.turn, r.rc, r.err_last)
        if hit:
            at, guessed = limits.resume_at(hit)
            raise usage_limit(at, hit.resets_at, guessed, limits.message(hit), hit.how)
        if r.stopped:
            raise L.GradeError(r.stopped)
        bad = claude.classify(r.turn, r.rc, r.err_last)
        if bad:
            raise L.GradeError(f"{bad[0]}: {bad[1]}")
        return r.turn.answer
    return grade


def _read_json(p: Path, default):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _write_json(p: Path, obj) -> None:
    config.private_dir(p.parent)
    config.atomic_write(p, json.dumps(obj, indent=1, ensure_ascii=False, sort_keys=True) + "\n")


def evidence(e: dict) -> list:
    """A pair's evidence in plain labels: "s2" (Semantic Scholar lists the reference) and
    "text:<arxiv|doi|title>" (the later paper's own text names the earlier one; found from
    either paper's side it is the same fact: the hub's text index)."""
    out = []
    for v in e["via"]:
        lab = "s2" if v == "s2" else "text:" + v.split(":", 1)[-1] if v.startswith(("text:", "mention:")) else v
        if lab not in out:
            out.append(lab)
    return sorted(out, key=lambda x: (x != "s2", x))


def source(e: dict) -> str:
    ev = evidence(e)
    s2, text = "s2" in ev, any(x.startswith("text:") for x in ev)
    return "both" if s2 and text else "s2" if s2 else "text"


def run(api, *, ids=None, dry_run: bool = False, parallel: int = PARALLEL, fetch=_DEFAULT, grade=None,
        root: Path | None = None, log=None, refresh: bool = False, restructure: bool = False) -> dict:
    """One relink over the library (or the papers `ids`). `api`: get(path, params) and
    post(path, body), as api.Api. `fetch`: Semantic Scholar (None: without it; default: the
    network, paced and retried). `grade`: links' grade(prompt) (default: claude haiku). `log`:
    progress lines. `restructure`: every graded pair to the hub in one request, which applies
    Leo's per-paper rule to the whole map (the whole library only). -> the result (see report()
    and restructure_report())."""
    if restructure and ids:
        raise PapercastError("--restructure applies the rule to the whole map: name no papers")
    root = Path(root) if root is not None else root_dir()
    config.private_dir(root)
    log = log or (lambda msg: None)
    parallel = max(1, min(PARALLEL_MAX, int(parallel)))
    where = getattr(api, "server", "the hub")
    try:
        st = api.get("/api/cli/relink")
    except NotFound:
        raise PapercastError(f"{where} has no relink yet (GET /api/cli/relink): the hub needs its update first")
    if not isinstance(st, dict) or not isinstance(st.get("papers"), list):
        raise PapercastError(f"{where} answered GET /api/cli/relink without its papers")
    everything = [p for p in st["papers"] if isinstance(p, dict) and p.get("id")]
    papers = [p for p in everything if p.get("visible", True)]
    lib = L.Library(papers)
    by_id = lib.by_id
    if ids:
        hidden = {p["id"] for p in everything} - set(by_id)
        bad = [i for i in ids if i not in by_id]
        if bad:
            raise PapercastError("not on the map: " + ", ".join(
                f"{i} (no live episode)" if i in hidden else f"{i} (no such paper)" for i in bad))
        todo = list(dict.fromkeys(ids))
    else:
        todo = [p["id"] for p in papers]
    mode = st.get("agent_links") or "auto"
    res = {"server": where, "at": util.now_iso(), "dry_run": bool(dry_run), "mode": mode, "restructure": bool(restructure),
           "library": len(papers), "papers": len(todo), "log_max": st.get("log_max"),
           "changes": [], "skipped": [], "held": [], "errors": [], "limit": None, "info": {}, "log_ids": []}

    # 1. the hub's text index, both ways: mentioned_by[q] = the papers whose text names q
    mentioned_by, names = {}, {}
    for i, q in enumerate(papers, 1):
        params = {"title": q.get("title"), "arxiv": q.get("arxiv_id"), "doi": q.get("doi"), "exclude": q["id"]}
        if not any(params[k] for k in ("title", "arxiv", "doi")):
            mentioned_by[q["id"]] = []
            continue
        try:
            r = api.get("/api/cli/mentions", params)
        except (ApiError, PapercastError) as e:
            res["errors"].append({"paper_id": q["id"], "what": "mentions", "error": str(e)})
            continue
        ms = r.get("mentions") if isinstance(r, dict) else None
        if not isinstance(ms, list):
            res["errors"].append({"paper_id": q["id"], "what": "mentions", "error": "no mentions in the answer"})
            continue
        mentioned_by[q["id"]] = [m for m in ms if isinstance(m, dict) and m.get("paper_id") in by_id
                                 and m["paper_id"] != q["id"]]
        for m in mentioned_by[q["id"]]:
            names.setdefault(m["paper_id"], {}).setdefault(q["id"], m.get("field") or "title")
        if i % 10 == 0 or i == len(papers):
            log(f"text index: {i} of {len(papers)} papers")

    # 2. each paper's candidates, a few at a time (Semantic Scholar paced across them)
    if refresh:
        shutil.rmtree(root / "s2", ignore_errors=True)
    if fetch is _DEFAULT:
        fetch = L.retrying(Paced(L.http_fetch))
    s2 = L.S2(fetch, str(root / "s2")) if fetch else None
    done = [0]
    lock = threading.Lock()

    def find(pid):
        p = by_id[pid]
        paper = {k: p.get(k) for k in ("title", "year", "arxiv_id", "doi")}
        cands, info = L.candidates(paper, lib, s2, None, {pid}, mentions=mentioned_by.get(pid),
                                   named=names.get(pid, {}))
        with lock:
            done[0] += 1
            if done[0] % 5 == 0 or done[0] == len(todo):
                log(f"candidates: {done[0]} of {len(todo)} papers")
        return pid, cands, info

    with ThreadPoolExecutor(parallel) as ex:
        found = list(ex.map(find, todo))
    pairs: dict[tuple, dict] = {}
    for pid, cands, info in found:
        res["info"][pid] = {k: info.get(k) for k in ("s2_id", "resolved_via", "refs", "cits", "text_names",
                                                     "mentioned_by", "s2_error", "candidates") if k in info}
        res["info"][pid]["text"] = bool(by_id[pid].get("text"))
        for c in cands:
            src, dst = (c["other"], pid) if c["direction"] == "builds_on" else (pid, c["other"])
            e = pairs.setdefault((src, dst), {"src": src, "dst": dst, "via": [], "influential": False,
                                              "found_by": []})
            e["via"] += [v for v in c["via"] if v not in e["via"]]
            e["influential"] = e["influential"] or bool(c.get("influential"))
            if pid not in e["found_by"]:
                e["found_by"].append(pid)
    res["candidates"] = dict(Counter(source(e) for e in pairs.values()), pairs=len(pairs))

    # found both ways round (two papers of one year citing each other's preprints): the earlier
    # by arXiv id is the one built on
    def okey(pid):
        p = by_id[pid]
        return (p.get("year") or 0, L.base_arxiv(p.get("arxiv_id")) or "9999", pid)
    for a, b in list(pairs):
        if (a, b) in pairs and (b, a) in pairs:
            drop = (a, b) if okey(a) > okey(b) else (b, a)
            res["held"].append(dict(_brief(pairs.pop(drop)), reason="found both ways round: kept the other"))

    # 3. left out before grading: a person's decision, or linked the other way
    links_by = {(l["src"], l["dst"]): l for l in st.get("links") or [] if isinstance(l, dict)}
    sugg_by = {(s["src"], s["dst"]): s for s in st.get("suggestions") or [] if isinstance(s, dict)}
    kept_by = {(k["src"], k["dst"]): k for k in st.get("candidates") or [] if isinstance(k, dict)}

    def held(src, dst):
        row, back = links_by.get((src, dst)), links_by.get((dst, src))
        if row is not None:
            if row["state"] == "removed":
                if row.get("person"):
                    return "removed by a person"
                if (kept_by.get((src, dst)) or {}).get("grade") in ("e", "s", "w"):
                    return "left out by the per-paper rule (the hub keeps its grade)"
                return "removed"
            if row.get("person"):
                return "a person's link"
            return None
        if back is not None:
            if back["state"] == "active":
                return "linked the other way"
            if back.get("person"):
                return "removed by a person (the other way)"
        s = sugg_by.get((src, dst))
        if s is not None and s.get("state") == "dismissed":
            return "dismissed by a person"
        if s is not None and mode == "suggest":
            return "suggested already"
        return None

    eligible = []
    for e in pairs.values():
        why = held(e["src"], e["dst"])
        if why:
            res["held"].append(dict(_brief(e), reason=why))
        else:
            eligible.append(e)

    # 4. grading: only pairs never graded before, packed by child, a few calls at a time
    pfile = root / "pairs.json"
    cache = _read_json(pfile, {})
    cache = cache if isinstance(cache, dict) else {}
    need = [e for e in eligible if f"{e['src']}>{e['dst']}" not in cache]
    info = {pid: {"title": p.get("title") or "", "year": p.get("year"),
                  "claims": [c for c in p.get("claims") or [] if isinstance(c, str)]} for pid, p in by_id.items()}
    bs = L.pack([(e["dst"], e["src"], e) for e in need])
    stop = threading.Event()
    got = {"calls": 0, "skipped": 0, "errors": []}
    if bs:
        grade = grade or claude_grader(root)
        log(f"grading {len(need)} pairs in {len(bs)} calls to {L.GRADER_MODEL}"
            + (f" ({len(eligible) - len(need)} graded before)" if len(eligible) > len(need) else ""))

    def grade_one(batch):
        if stop.is_set():
            return
        try:
            graded, stats = L.grade_batches([batch], info, grade, str(root / "grades"))
        except UsageLimit as e:
            stop.set()
            res["limit"] = {"resume_at": getattr(e, "resume_at", None), "message": getattr(e, "message", str(e))}
            return
        except BaseException:
            stop.set()
            raise
        with lock:
            at = util.now_iso()
            for (child, parent, _), g in graded:
                cache[f"{parent}>{child}"] = {"grade": g, "at": at}
            _write_json(pfile, cache)
            got["calls"] += stats.get("batches", 0)
            got["skipped"] += stats.get("skipped", 0)
            got["errors"] += stats.get("errors", [])
            log(f"graded {got['calls'] + got['skipped']} of {len(bs)} calls")

    with ThreadPoolExecutor(parallel) as ex:
        list(ex.map(grade_one, bs))
    res["graded"] = {"pairs": len(eligible), "now": sum(1 for e in need if f"{e['src']}>{e['dst']}" in cache),
                     "before": len(eligible) - len(need), "calls": got["calls"], "calls_failed": got["skipped"],
                     "errors": got["errors"][:10]}

    # 5. to the hub, per paper built on (each pair once)
    by_child: dict[str, list] = {}
    res["graded_none"] = 0
    ungraded = 0
    for e in eligible:
        g = (cache.get(f"{e['src']}>{e['dst']}") or {}).get("grade")
        if g not in TO_HUB:
            ungraded += 1
            continue
        row = links_by.get((e["src"], e["dst"]))
        if g == "none" and not (row and row["state"] == "active"):
            res["graded_none"] += 1
            continue
        by_child.setdefault(e["dst"], []).append(dict(e, grade=g))
    res["ungraded"] = ungraded

    def ev(ch, part):
        """The pair's evidence: from the item sent (its index), else from the pairs found."""
        k = ch.get("index")
        e = part[k] if isinstance(k, int) and 0 <= k < len(part) else pairs.get((ch.get("src"), ch.get("dst")))
        return {"evidence": evidence(e), "found_by": e.get("found_by", [])} if e else {"evidence": [], "found_by": []}

    def item(e):
        return {"grade": TO_HUB[e["grade"]], "influential": bool(e.get("influential")), "source": source(e)}

    if restructure:
        part = [e for _, es in sorted(by_child.items(), key=lambda kv: okey(kv[0])) for e in es]
        body = {"links": [dict(item(e), src=e["src"], dst=e["dst"]) for e in part], "dry_run": bool(dry_run)}
        log(f"{'asking' if dry_run else 'sending'} the hub to restructure the map ({len(part)} graded pairs)")
        try:
            r = api.post("/api/cli/relink/restructure", body, retries=0, timeout=900)
        except NotFound:
            raise PapercastError(f"{where} has no restructure yet (POST /api/cli/relink/restructure): "
                                 "the hub needs its update first")
        except (ApiError, PapercastError) as ex:
            res["errors"].append({"paper_id": None, "what": "restructure", "error": str(ex)})
            r = {}
        r = r if isinstance(r, dict) else {}
        for ch in r.get("changes") or []:
            res["changes"].append(dict(ch, **ev(ch, part)))
        for sk in r.get("skipped") or []:
            e = part[sk["index"]] if isinstance(sk.get("index"), int) and 0 <= sk["index"] < len(part) else None
            res["skipped"].append(dict(_brief(e), reason=sk.get("reason")) if e else sk)
        res["log_ids"] += list(r.get("log_ids") or [])
        res["sent"] = len(part)
        for k in ("papers", "totals", "graphs", "unchanged"):
            res[k] = r.get(k)
    else:
        # every paper built on that has a pair (sent or held back) or an agent link: the hub
        # applies the rule to it, also when there is nothing to send
        for e in pairs.values():
            by_child.setdefault(e["dst"], [])
        for (s, d), l in links_by.items():
            if l.get("state") == "active" and not l.get("person") and d in todo and d in by_id:
                by_child.setdefault(d, [])
        for n, (child, es) in enumerate(sorted(by_child.items(), key=lambda kv: okey(kv[0])), 1):
            for i in range(0, max(1, len(es)), POST_MAX):
                part = es[i:i + POST_MAX]
                body = {"links": [dict(item(e), other={"paper_id": e["src"]}, direction="builds_on") for e in part],
                        "dry_run": bool(dry_run)}
                try:
                    r = api.post(f"/api/cli/papers/{urllib.parse.quote(child)}/links", body)
                except (ApiError, PapercastError) as ex:
                    res["errors"].append({"paper_id": child, "what": "links", "error": str(ex)})
                    continue
                r = r if isinstance(r, dict) else {}
                for ch in r.get("changes") or []:
                    res["changes"].append(dict(ch, **ev(ch, part)))
                for sk in r.get("skipped") or []:
                    e = part[sk["index"]] if isinstance(sk.get("index"), int) and sk["index"] < len(part) else None
                    res["skipped"].append(dict(_brief(e), reason=sk.get("reason")) if e else sk)
                res["log_ids"] += list(r.get("log_ids") or [])
            if n % 10 == 0 or n == len(by_child):
                log(f"{'asked' if dry_run else 'sent'} the hub: {n} of {len(by_child)} papers")
    res["counts"] = dict(Counter(c["op"] for c in res["changes"]))
    # agent links no candidate supports now: left as they are (only a graded "none" removes one)
    cand = set(pairs)
    res["unsupported_agent_links"] = sum(
        1 for (s, d), l in links_by.items() if l["state"] == "active" and not l.get("person")
        and (s, d) not in cand and (s in todo or d in todo) and s in by_id and d in by_id)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    run_file = root / "runs" / f"{stamp}-{'restructure-' if restructure else ''}{'dry-run' if dry_run else 'applied'}.json"
    _write_json(run_file, res)
    res["run_file"] = str(run_file)
    res["titles"] = {pid: {"title": p.get("title"), "year": p.get("year")} for pid, p in by_id.items()}
    return res


def _what(ch: dict) -> str:
    grade = WORD.get(ch.get("grade"), ch.get("grade"))
    was = WORD.get(ch.get("was"), ch.get("was"))
    if ch["op"] == "regrade":
        return f"{was} → {grade}"
    if ch["op"] == "remove":
        return f"{was} · not chosen by the per-paper rule" if ch.get("why") == "rule" else f"was {was}, now graded none"
    return grade + (" · restored (the agent had removed it)" if ch.get("restored") else "")


def _brief(e: dict | None) -> dict:
    if not e:
        return {}
    return {"src": e["src"], "dst": e["dst"], "evidence": evidence(e)}


def _title(res: dict, pid: str, width: int = 90) -> str:
    t = (res["titles"].get(pid) or {}).get("title") or pid
    t = " ".join(t.split())
    return t if len(t) <= width else t[:width - 1] + "…"


def _year(res: dict, pid: str) -> str:
    return str((res["titles"].get(pid) or {}).get("year") or "?")


def report(res: dict) -> str:
    """The result as Markdown: a summary, then per paper built on, its links to add, regrade or
    remove with their evidence; then what was left alone and why."""
    dry = res["dry_run"]
    c = res.get("counts", {})
    rule = sum(1 for ch in res["changes"] if ch.get("op") == "remove" and ch.get("why") == "rule")
    verb = {"add": "would add" if dry else "added", "suggest": "would suggest" if dry else "suggested",
            "regrade": "would regrade" if dry else "regraded", "remove": "would remove" if dry else "removed"}
    out = [f"# papercast relink: {'dry run (nothing was changed)' if dry else 'applied'}", "",
           f"{res['server']} · {res['at']} · links from uploads: "
           f"{'suggestions only' if res['mode'] == 'suggest' else 'automatic'}", ""]
    cand = res.get("candidates", {})
    g = res.get("graded", {})
    out += [f"- Papers on the map: {res['library']}; relinked: {res['papers']}.",
            f"- Candidate pairs (an earlier paper and a later one that may build on it): {cand.get('pairs', 0)}"
            f" (Semantic Scholar only {cand.get('s2', 0)}, text only {cand.get('text', 0)}, both {cand.get('both', 0)}).",
            f"- Left out before grading (a person's decision, or linked the other way): {len(res['held'])}.",
            f"- Graded by {L.GRADER_MODEL}: {g.get('now', 0)} pairs now in {g.get('calls', 0)} calls"
            + (f", {g.get('calls_failed')} calls failed" if g.get("calls_failed") else "")
            + f"; {g.get('before', 0)} graded in an earlier run.",
            f"- Changes: {', '.join(f'{verb[k]} {c[k]}' for k in ('add', 'suggest', 'regrade', 'remove') if c.get(k)) or 'none'}"
            + (f" ({rule} of the removals: not chosen by the per-paper rule)" if rule else "")
            + f"; graded none and not linked: {res.get('graded_none', 0)}; left alone by the hub: {len(res['skipped'])}.",
            f"- Agent links no candidate supports now (left as they are): {res.get('unsupported_agent_links', 0)}."]
    if res.get("ungraded"):
        out.append(f"- Not graded yet: {res['ungraded']} pairs (run papercast relink again).")
    if res.get("limit"):
        at = res["limit"].get("resume_at")
        out.append(f"- Claude's usage limit stopped the grading"
                   + (f"; it resets about {util.local_hhmm(at)}" if at else "") + ".")
    if res["errors"]:
        out.append(f"- Errors: {len(res['errors'])} (listed at the end).")
    out += ["", "Evidence: s2 = Semantic Scholar lists the reference; text:title / text:arxiv / text:doi = the "
            "later paper's own text names the earlier one by its title, arXiv id or DOI (the hub's full-text "
            "index). Grades: essential, strong, weak; none = not built on.", ""]
    by_child: dict[str, list] = {}
    for ch in res["changes"]:
        by_child.setdefault(ch["dst"], []).append(ch)
    mark = {"add": "+ add", "suggest": "+ suggest", "regrade": "~ regrade", "remove": "- remove"}
    for child in sorted(by_child, key=lambda p: (_year(res, p), _title(res, p))):
        out.append(f"## {_title(res, child)} ({_year(res, child)}, {child})")
        out.append("")
        for ch in sorted(by_child[child], key=lambda x: (x["op"], _title(res, x["src"]))):
            out.append(f"- {mark.get(ch['op'], ch['op'])}: builds on “{_title(res, ch['src'])}” ({_year(res, ch['src'])})"
                       f" · {_what(ch)} · evidence {', '.join(ch.get('evidence') or ['?'])}")
        out.append("")
    left = res["held"] + res["skipped"]
    if left:
        out += ["## Left alone", ""]
        for why, n in Counter(x.get("reason") for x in left).most_common():
            out.append(f"- {why}: {n}")
        out.append("")
        for x in sorted(left, key=lambda x: (str(x.get("reason")), _title(res, x.get("dst", "")))):
            if x.get("src"):
                out.append(f"  - {x.get('reason')}: “{_title(res, x['dst'], 60)}” on “{_title(res, x['src'], 60)}”"
                           f" · evidence {', '.join(x.get('evidence') or ['?'])}")
        out.append("")
    out += _coverage(res)
    out.append(f"(Run record: {res.get('run_file')})")
    return "\n".join(out) + "\n"


def _coverage(res: dict) -> list:
    out = []
    no_s2 = [p for p, i in res["info"].items() if not i.get("s2_id")]
    no_text = [p for p, i in res["info"].items() if not i.get("text")]
    s2_err = [(p, i["s2_error"]) for p, i in res["info"].items() if i.get("s2_error")]
    if no_s2 or no_text or s2_err or res["errors"]:
        out += ["## Coverage", ""]
        if no_s2:
            out.append(f"- Not found on Semantic Scholar ({len(no_s2)}): " + "; ".join(_title(res, p, 50) for p in no_s2))
        if s2_err:
            out.append(f"- Semantic Scholar failed for {len(s2_err)}: " + "; ".join(
                f"{_title(res, p, 40)} ({e[:80]})" for p, e in s2_err))
        if no_text:
            out.append(f"- No text of its own on the hub ({len(no_text)}; text evidence only from the other side): "
                       + "; ".join(_title(res, p, 50) for p in no_text))
        for e in res["errors"]:
            out.append(f"- Error ({e.get('what')}, {e.get('paper_id')}): {e.get('error')}")
        out.append("")
    return out


RULE_TEXT = ("Leo's per-paper rule, for each paper over all the papers it builds on in the library (whatever "
             "graph shows them, not graph by graph): every essential parent, no limit; strong ones until it has 5 "
             "parents; weak ones only until it has 4, and a weak one only where no other drawn path already joins "
             "the two papers (its slot then goes to the next weak one). Ties: Semantic Scholar's isInfluential "
             "first, then the most recent parent. A person's links always stay and count toward the 5 and the 4. "
             "The agent's links the rule does not choose are removed, the graded pairs it chooses that are "
             "missing are added.")


def restructure_report(res: dict) -> str:
    """papercast relink --restructure as Markdown: the totals (links before and after, per graph),
    then per paper built on, its parents: kept, added, removed and regraded with their grades, and
    the graded pairs the rule left out."""
    dry = res["dry_run"]
    ch_by: dict[str, list] = {}
    for ch in res["changes"]:
        ch_by.setdefault(ch["dst"], []).append(ch)
    c = Counter(ch["op"] for ch in res["changes"])
    rule = sum(1 for ch in res["changes"] if ch["op"] == "remove" and ch.get("why") == "rule")
    restored = sum(1 for ch in res["changes"] if ch["op"] == "add" and ch.get("restored"))
    t = res.get("totals") or {}
    papers = [p for p in res.get("papers") or [] if isinstance(p, dict)]
    kept = Counter(e["by"] for p in papers for e in p.get("parents", []) if e.get("status") == "kept")
    would = "would be " if dry else ""
    out = [f"# papercast relink --restructure: {'dry run (nothing was changed)' if dry else 'applied'}", "",
           f"{res['server']} · {res['at']} · links from uploads: "
           f"{'suggestions only' if res['mode'] == 'suggest' else 'automatic'}", "", RULE_TEXT, "",
           f"- Links in the library: {t.get('before', '?')} before, {t.get('after', '?')} after"
           + (" (if applied)" if dry else "") + ".",
           f"- {'Would add' if dry else 'Added'} {c.get('add', 0)}" + (f" ({restored} restored)" if restored else "")
           + f"; {'would remove' if dry else 'removed'} {c.get('remove', 0)} ({rule} not chosen by the rule, "
           f"{c.get('remove', 0) - rule} graded none); {'would regrade' if dry else 'regraded'} {c.get('regrade', 0)}"
           + (f"; {'would suggest' if dry else 'suggested'} {c['suggest']}" if c.get("suggest") else "") + ".",
           f"- Links that stay: {kept.get('person', 0)} a person's (never touched), {kept.get('agent', 0)} the agent's.",
           f"- Graded pairs sent: {res.get('sent', 0)}; left out before grading (a person's decision, linked the "
           f"other way, removed): {len(res['held'])}; not graded yet: {res.get('ungraded', 0)}.",
           f"- Papers built on: {sum(1 for p in papers if p.get('parents'))}."]
    if res.get("limit"):
        out.append("- Claude's usage limit stopped the grading: the pairs not graded yet were not sent.")
    if res["errors"]:
        out.append(f"- Errors: {len(res['errors'])} (listed at the end).")
    graphs = res.get("graphs") or []
    if graphs:
        out += ["", "| Graph | Papers | Links before | Links after |", "|---|---:|---:|---:|"]
        for g in graphs:
            out.append(f"| {g.get('name')} | {g.get('n')} | {g.get('before')} | {g.get('after')} |")
    out += ["", "Grades: essential, strong, weak. “S2 influential”: Semantic Scholar flags the citation "
            "isInfluential (the rule's first tie-break).", ""]
    order = sorted(papers, key=lambda p: (_year(res, p["id"]), _title(res, p["id"])))
    for p in order:
        pid, ps = p["id"], p.get("parents") or []
        changes = ch_by.get(pid, [])
        by = {k: [e for e in ps if e.get("status") == k] for k in ("kept", "added", "suggested", "removed",
                                                                    "not selected", "blocked")}
        none_rm = [x for x in changes if x["op"] == "remove" and x.get("why") != "rule"]
        regr = {x["src"]: x for x in changes if x["op"] == "regrade"}
        if not (by["kept"] or by["added"] or by["suggested"] or by["removed"] or none_rm):
            continue
        n_after = len(by["kept"]) + len(by["added"])
        out.append(f"## {_title(res, pid)} ({_year(res, pid)}, {pid})")
        out.append("")
        out.append(f"{n_after} parent{'' if n_after == 1 else 's'} after: {len(by['kept'])} kept, {len(by['added'])} added"
                   f"; removed {len(by['removed']) + len(none_rm)}.")
        out.append("")

        def name(src):
            return f"“{_title(res, src, 80)}” ({_year(res, src)})"

        def grade(e):
            return WORD.get(e.get("grade"), e.get("grade")) + (" · S2 influential" if e.get("influential") else "")
        for e in sorted(by["kept"], key=lambda e: (RANK_OF.get(e.get("grade"), 3), _title(res, e["src"]))):
            rg = regr.get(e["src"])
            out.append(f"- kept: {name(e['src'])} · {grade(e)}" + (" · a person's link" if e.get("by") == "person" else "")
                       + (f" · regraded from {WORD.get(rg.get('was'), rg.get('was'))}" if rg else ""))
        for e in sorted(by["added"] + by["suggested"], key=lambda e: (RANK_OF.get(e.get("grade"), 3), _title(res, e["src"]))):
            out.append(f"- + {'add' if e['status'] == 'added' else 'suggest'}: {name(e['src'])} · {grade(e)}"
                       + (" · restored (the agent had removed it)" if e.get("restored") else ""))
        for e in sorted(by["removed"], key=lambda e: (RANK_OF.get(e.get("grade"), 3), _title(res, e["src"]))):
            out.append(f"- − remove: {name(e['src'])} · {grade(e)} · not chosen by the rule")
        for x in none_rm:
            out.append(f"- − remove: {name(x['src'])} · was {WORD.get(x.get('was'), x.get('was'))}, graded none now")
        if by["not selected"]:
            out.append("- not chosen (not linked): " + "; ".join(
                f"{name(e['src'])} {grade(e)}" for e in sorted(by["not selected"], key=lambda e: (RANK_OF.get(e.get("grade"), 3), _title(res, e["src"])))))
        if by["blocked"]:
            out.append("- not allowed: " + "; ".join(f"{name(e['src'])} {grade(e)} ({e.get('reason')})" for e in by["blocked"]))
        out.append("")
    left = res["held"] + res["skipped"]
    if left:
        out += ["## Left alone", ""]
        for why, n in Counter(x.get("reason") for x in left).most_common():
            out.append(f"- {why}: {n}")
        out.append("")
    out += _coverage(res)
    out.append(f"(Run record: {res.get('run_file')})")
    return "\n".join(out) + "\n"
