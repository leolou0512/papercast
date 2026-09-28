"""The cut-only pass (Leo's runner, cut.py, INTERFACE 1.8.1, Leo 2026-09-27), copied unchanged
but for the files it guards (claims.md and paper.json; the group has no concepts.md).

After script.md and explainer.json pass every check, the same session gets one more turn
(prompts.cut): reread both and delete every sentence (script) or item (explainer points and
figures) that fails the test in the seed guideline (a caveat, a comparison or a link to
something already known stays only if leaving it out would make the listener misread the method
or the result), and anything that only restates its neighbour. Deletions only.

The runner enforces "deletions only" here, and the existing checks are the only other guard:
- script.md: its sentences must be a subsequence of the pre-cut sentences (whitespace
  normalised; any changed, added or moved sentence fails), and it must still pass
  script_check.check (15-25 minutes and the rest). Else the pre-cut script is put back.
- explainer.json: its points must be a subsequence of the pre-cut points, its figures of the
  pre-cut figures (a figure is kept or deleted whole), with at least one point left. Else the
  pre-cut file is put back.
- claims.md and paper.json are not the cut's to touch: changed, they are put back.
A rejected cut is logged and the episode goes on with the pre-cut file; it never fails. The
pre-cut script stays as `script.precut.md` in the work directory so the cuts can be inspected.

The reference copies live in a pipeline-owned directory (`keep_dir`, the job's `cut/`), never the agent's working
directory: the agent could rewrite a copy it can reach.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile

from ..common import checks as script_check

log = logging.getLogger("papercast")

SCRIPT = "script.md"
EXPLAINER = "explainer.json"
UNTOUCHED = ("claims.md", "paper.json")
PRECUT = "script.precut.md"


def _read(path: str) -> str | None:
    try:
        if os.path.islink(path) or not os.path.isfile(path):
            return None
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return None


def _write(path: str, text: str) -> None:
    """Atomic replace; replaces a symlink rather than following it."""
    d = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".cut-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def prepare(cwd: str, keep_dir: str) -> None:
    """Keep the pre-cut files in `keep_dir` (runner-owned). Call before the cut turn."""
    os.makedirs(keep_dir, mode=0o700, exist_ok=True)
    for name in (SCRIPT, EXPLAINER) + UNTOUCHED:
        text = _read(os.path.join(cwd, name))
        dst = os.path.join(keep_dir, name)
        if text is None:
            if os.path.lexists(dst):
                os.unlink(dst)
            continue
        _write(dst, text)


def is_subsequence(new: list, old: list) -> bool:
    it = iter(old)
    return all(any(x == y for y in it) for x in new)


def _norm(s) -> str:
    return " ".join(str(s).split())


def _figure_key(fig) -> str:
    if not isinstance(fig, dict):
        return json.dumps(fig, sort_keys=True)
    return json.dumps({k: (_norm(v) if k == "caption" else v) for k, v in fig.items()},
                      sort_keys=True)


def judge_script(old: str, new: str | None, wpm: float, min_min: float,
                 max_min: float) -> tuple[bool, str | None, int]:
    """(accepted, why not, sentences deleted)."""
    if new is None:
        return False, "script.md is missing after the cut", 0
    a, b = script_check.sentences(old), script_check.sentences(new)
    if b == a:
        return True, None, 0
    if not is_subsequence(b, a):
        changed = next((s for s in b if s not in a), None)
        why = "a sentence was changed, added or moved"
        if changed:
            why += f': "{changed[:120]}"'
        return False, why, 0
    chk = script_check.check(new, wpm, min_min, max_min)
    if not chk["ok"]:
        return False, "the cut script fails the script check: " + "; ".join(chk["problems"]), 0
    return True, None, len(a) - len(b)


def judge_explainer(old: str, new: str | None) -> tuple[bool, str | None, int]:
    """(accepted, why not, items deleted)."""
    if new is None:
        return False, "explainer.json is missing after the cut", 0
    try:
        o, n = json.loads(old), json.loads(new)
    except ValueError:
        return False, "explainer.json is not valid JSON after the cut", 0
    if not isinstance(o, dict) or not isinstance(n, dict) or set(n) - set(o):
        return False, "explainer.json gained or changed fields", 0
    op, np_ = [_norm(x) for x in o.get("points") or []], [_norm(x) for x in n.get("points") or []]
    of = [_figure_key(f) for f in o.get("figures") or []]
    nf = [_figure_key(f) for f in n.get("figures") or []]
    if not isinstance(n.get("points"), list) or not np_:
        return False, "no point is left", 0
    if not is_subsequence(np_, op):
        return False, "a point was changed, added or moved", 0
    if not is_subsequence(nf, of):
        return False, "a figure or caption was changed, added or moved", 0
    return True, None, (len(op) - len(np_)) + (len(of) - len(nf))


def finish(cwd: str, keep_dir: str, wpm: float, min_min: float, max_min: float,
           paper_id: str = "") -> dict:
    """After the cut turn: accept each file's cut or put the pre-cut file back; keep the pre-cut
    script as script.precut.md in `cwd`. Never raises for a bad cut. Returns
    {"script": {...}, "explainer": {...}, "restored": [...]} for state.json and the log."""
    out: dict = {"restored": []}
    for name in (SCRIPT, EXPLAINER):
        old = _read(os.path.join(keep_dir, name))
        key = "script" if name == SCRIPT else "explainer"
        if old is None:
            out[key] = {"accepted": False, "reason": "no pre-cut copy", "deleted": 0}
            continue
        new = _read(os.path.join(cwd, name))
        if name == SCRIPT:
            ok, why, n = judge_script(old, new, wpm, min_min, max_min)
            _write(os.path.join(cwd, PRECUT), old)
        else:
            ok, why, n = judge_explainer(old, new)
        out[key] = {"accepted": ok, "reason": why, "deleted": n}
        if ok:
            log.info("%s: cut pass: %s: %d deleted", paper_id, name, n)
        else:
            _write(os.path.join(cwd, name), old)
            out["restored"].append(name)
            log.warning("%s: cut pass rejected for %s, pre-cut file kept: %s", paper_id, name, why)
    for name in UNTOUCHED:
        old = _read(os.path.join(keep_dir, name))
        if old is not None and _read(os.path.join(cwd, name)) != old:
            _write(os.path.join(cwd, name), old)
            out["restored"].append(name)
            log.warning("%s: cut pass changed %s; put back", paper_id, name)
    return out

