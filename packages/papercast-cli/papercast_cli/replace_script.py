"""`papercast replace-script`: a version's script replaced on the hub, and voiced again.

    papercast replace-script <episode id> <script.md> [--explainer-html F] [--explainer-json F] [--dry-run]
    papercast replace-script --batch FILE [--dry-run] [--parallel N]
    papercast replace-script <episode id> --undo [--dry-run]

Each goes to PUT /api/cli/episodes/<id>/script (hub/scriptswap.py), for the version's maker or
an admin. The hub runs its upload checks on the new script and refuses it with the reasons when
they fail. A version not voiced yet takes the new script at once, in its place in the voice
queue; a voiced one is voiced again in the voice it is in, its old audio playing until the new
one is ready. --dry-run: the hub's checks and what would happen, nothing changed.

The batch: FILE has one version per line, `episode_id<TAB>script.md` (a relative path is from
FILE's folder; a third and a fourth column, when there, are its new explainer.html and
explainer.json; blank lines and # comments are skipped). A few at a time (--parallel, 3), a
line each as they finish. It can be stopped and run again: each version the hub took, or found
with that script already, is written to FILE.done with the sha256 of the script sent, and the
next run skips those whose script has not changed since. A version being voiced at that moment
is refused for now ("busy"); run the batch again later for those.

--undo: the script (and explainer) the version's last replacement replaced, sent back the same
way, so it is checked and voiced again like any other.
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
import threading
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

from .errors import ApiError, PapercastError

PARALLEL = 3
PARALLEL_MAX = 8
EID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
DONE_OK = ("replaced", "unchanged")


def _path(eid: str) -> str:
    return f"/api/cli/episodes/{urllib.parse.quote(eid)}/script"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _text(p: Path, what: str) -> str:
    try:
        return p.read_bytes().decode("utf-8")
    except FileNotFoundError:
        raise PapercastError(f"{p}: no such file ({what})")
    except UnicodeDecodeError:
        raise PapercastError(f"{p}: not UTF-8 text ({what})")
    except OSError as e:
        raise PapercastError(f"{p}: {e.strerror or e} ({what})")


def item(eid: str, script: str | Path, explainer_html=None, explainer_json=None) -> dict:
    """One version to replace: {"episode_id", "body"} (the files read), or PapercastError."""
    if not EID_RE.match(eid or ""):
        raise PapercastError(f"{eid!r} is not an episode id (e_..., from papercast status or the page)")
    body = {"script": _text(Path(script), "the script")}
    if explainer_html:
        body["explainer_html"] = _text(Path(explainer_html), "the explainer.html")
    if explainer_json:
        body["explainer_json"] = _text(Path(explainer_json), "the explainer.json")
    return {"episode_id": eid, "body": body}


def read_batch(path: Path) -> list:
    """[(line number, episode id, script path, explainer.html or None, explainer.json or None)]."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as e:
        raise PapercastError(f"{path}: {e.strerror or e}")
    out, seen = [], {}
    for n, line in enumerate(lines, 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        cols = [c.strip() for c in line.split("\t")]
        if len(cols) < 2 or not cols[0] or not cols[1]:
            raise PapercastError(f"{path}:{n}: want episode_id<TAB>script.md, got {line[:80]!r}")
        if not EID_RE.match(cols[0]):
            raise PapercastError(f"{path}:{n}: {cols[0]!r} is not an episode id")
        if cols[0] in seen:
            raise PapercastError(f"{path}:{n}: {cols[0]} is on line {seen[cols[0]]} already")
        seen[cols[0]] = n
        files = [(path.parent / c) if c and not Path(c).is_absolute() else (Path(c) if c else None)
                 for c in (cols[1:4] + [""] * 3)[:3]]
        out.append((n, cols[0], files[0], files[1], files[2]))
    return out


def done_file(path: Path) -> Path:
    return Path(str(path) + ".done")


def read_done(path: Path) -> set:
    """{(episode id, sha256)} the hub took in an earlier run."""
    out = set()
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return out
    for line in lines:
        cols = line.split("\t")
        if len(cols) >= 3 and cols[2] in DONE_OK:
            out.add((cols[0], cols[1]))
    return out


# ---------------------------------------------------------------- one version

def send(api, it: dict, dry_run: bool) -> dict:
    """PUT the new files: {"episode_id", "result": replaced | unchanged | would | refused | busy |
    error, "line", "problems", "answer"}."""
    eid = it["episode_id"]
    body = dict(it["body"], dry_run=dry_run)
    try:
        r = api.request("PUT", _path(eid), data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                        content_type="application/json", accept=(409, 422), timeout=180, retries=2)
    except ApiError as e:
        return {"episode_id": eid, "result": "error", "line": f"error: {e}", "problems": []}
    a = r.body if isinstance(r.body, dict) else {}
    msg = a.get("message") or a.get("error") or f"HTTP {r.status}"
    if r.status == 409:
        res = "busy" if a.get("error") == "busy" else "error"
        return {"episode_id": eid, "result": res, "answer": a, "problems": [],
                "line": f"busy: {msg}" if res == "busy" else f"refused: {msg}"}
    if r.status == 422 or (dry_run and a.get("ok") is False):
        return {"episode_id": eid, "result": "refused", "answer": a, "problems": list(a.get("problems") or []),
                "line": f"REFUSED: {msg}"}
    stats = []
    if isinstance(a.get("words"), int):
        stats.append(f"{a['words']:,} words")
    if isinstance(a.get("minutes"), (int, float)):
        stats.append(f"{a['minutes']:.1f} min")
    res = "unchanged" if a.get("how") == "unchanged" else ("would" if dry_run else "replaced")
    return {"episode_id": eid, "result": res, "answer": a, "problems": [],
            "line": msg + (f" ({', '.join(stats)})" if stats else "")}


def show(res: dict, out=None) -> None:
    out = out or sys.stdout
    print(f"{res['episode_id']}  {res['line']}", file=out, flush=True)
    for p in res.get("problems") or []:
        print(f"    - {p}", file=out, flush=True)


# ---------------------------------------------------------------- the batch

def run_batch(api, path: Path, dry_run: bool = False, parallel: int = PARALLEL, out=None) -> dict:
    """Every version in the TSV, a few at a time; the counts by result."""
    if not 1 <= parallel <= PARALLEL_MAX:
        raise PapercastError(f"--parallel is 1 to {PARALLEL_MAX}")
    out = out or sys.stdout
    rows = read_batch(path)
    df = done_file(path)
    done = read_done(df)
    counts = {k: 0 for k in ("replaced", "unchanged", "would", "skipped", "refused", "busy", "error")}
    todo = []
    for n, eid, script, html, js in rows:
        try:
            it = item(eid, script, html, js)
        except PapercastError as e:
            counts["error"] += 1
            show({"episode_id": eid, "line": f"error: line {n}: {e}"}, out)
            continue
        it["sha256"] = _sha(it["body"]["script"])
        if (eid, it["sha256"]) in done:
            counts["skipped"] += 1
            show({"episode_id": eid, "line": "done in an earlier run (same script); skipped"}, out)
            continue
        todo.append(it)
    lock = threading.Lock()

    def one(it):
        res = send(api, it, dry_run)
        with lock:
            counts[res["result"]] += 1
            show(res, out)
            if res["result"] in DONE_OK and not dry_run:
                with open(df, "a", encoding="utf-8") as fh:
                    at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                    fh.write(f"{it['episode_id']}\t{it['sha256']}\t{res['result']}\t{at}\n")
        return res

    with ThreadPoolExecutor(max_workers=parallel) as pool:
        for f in as_completed([pool.submit(one, it) for it in todo]):
            f.result()
    total = len(rows)
    parts = [f"{v} {k}" for k, v in counts.items() if v]
    print(f"{total} version{'s' if total != 1 else ''}: {', '.join(parts) or 'nothing to do'}"
          + ("" if dry_run else f" (record: {df})"), file=out, flush=True)
    if counts["busy"]:
        print("Busy ones were being voiced just then: run the same command again later for them.", file=out)
    return counts


def undo_item(api, eid: str) -> tuple:
    """(item, the change it undoes): the files the last replacement in place replaced."""
    if not EID_RE.match(eid or ""):
        raise PapercastError(f"{eid!r} is not an episode id")
    h = api.get(_path(eid))
    done = [c for c in (h.get("changes") or []) if c.get("state") == "done"] if isinstance(h, dict) else []
    if not done:
        raise PapercastError(f"{eid} has had no script replaced: nothing to undo")
    ch = done[-1]
    prev = api.get(_path(eid), {"before": str(ch["id"])})
    body = {k: prev[k] for k in ("script", "explainer_html", "explainer_json") if isinstance(prev.get(k), str)}
    if "script" not in body:
        raise PapercastError(f"the hub has not kept the script change {ch['id']} replaced")
    return {"episode_id": eid, "body": body}, ch
