"""Up next and the play-along transcript: the player's part of the hub.

  GET /api/queue                       this person's Up next: {"ids", "rev", "papers"}
  PUT /api/queue                       {"ids": [...]}: the whole order (with "rev": refused, 409
                                       moved, if the queue changed since), or one change:
                                         {"op": "add", "ids"}        at the end, those not queued yet
                                         {"op": "next", "ids"}       first, in this order (moved if queued)
                                         {"op": "remove", "ids"}
                                         {"op": "move", "id", "to"}  to that place, 0 the first
                                         {"op": "clear"}
  GET /api/episodes/<id>/timings       timings.json as the voice worker wrote it, else an estimate
                                       from script.md, marked "estimated": true
  GET /api/episodes/<id>/transcript    script.md as headings and paragraphs, each piece tied to
                                       its timing: what the page's transcript draws

The queue is each person's own and lives on the hub, so it follows them from the laptop to the
phone. It holds episode ids (a version, not a paper). Each change is one transaction on the
queue as it is then, so two devices changing it at once both land; only the whole order (`ids`)
can undo another device's change, which `rev` guards against. `papers` in every answer are the
queued episodes' papers as the library shows them, so a page can draw and play them even when
its list is filtered. A deleted episode drops out: it is left out of every answer, and out of
the table at the next change.

Timings: {"version": 1, "segments": [{"start", "end", "text"}]}, one sentence each, in script
order. Without the file, script.md is cut into sentences as common/checks.sentences does and
each gets a share of the audio's length in proportion to its characters. The transcript ties
the script's words to the segments by matching the words themselves, so a voice that cut the
script a little differently (a heading said with a full stop, a long sentence in two) still
lines up."""
from __future__ import annotations

import difflib
import json
import logging
import os
import re
import stat
import sys
import threading
from pathlib import Path

from . import db, events, web
from .app import HTTPError

try:
    from papercast_cli.common import checks
except ImportError:         # a checkout: the package sits next to the hub (web.py adds the path)
    sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "packages" / "papercast-cli"))
    from papercast_cli.common import checks

log = logging.getLogger("pcg.player")

MAX_QUEUE = 200
SCRIPT_MAX = 256 * 1024
TIMINGS_MAX = 4 * 1024 * 1024
CHARS_PER_S = 15.0          # about 150 words a minute: when neither the audio nor the upload says how long
EID = web.EID

SCHEMA = """
CREATE TABLE IF NOT EXISTS up_next (
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  episode_id TEXT NOT NULL REFERENCES episodes(id) ON DELETE CASCADE,
  pos INTEGER NOT NULL,
  added_at TEXT NOT NULL,
  PRIMARY KEY (user_id, episode_id)
);
CREATE INDEX IF NOT EXISTS up_next_order ON up_next(user_id, pos);
CREATE TABLE IF NOT EXISTS up_next_rev (      -- one row per person: bumped by every change
  user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
  rev INTEGER NOT NULL,
  updated_at TEXT NOT NULL
)
"""

_ready: set = set()
_ready_lock = threading.Lock()


def ensure_schema() -> None:
    """This part's tables, once per database."""
    key = str(db._path)
    if key in _ready:
        return
    with _ready_lock:
        if key in _ready:
            return
        c = db.conn()
        for stmt in SCHEMA.split(";"):      # one by one: executescript would commit a caller's transaction
            if stmt.strip():
                c.execute(stmt)
        if not c.in_transaction:
            _ready.add(key)


def start(cfg) -> None:
    ensure_schema()


# ---------------------------------------------------------------- the queue

def _ids(c, uid: int) -> list:
    """The queue in order, live episodes only."""
    return [r[0] for r in c.execute(f"""SELECT q.episode_id FROM up_next q JOIN episodes e ON e.id = q.episode_id
                                        WHERE q.user_id = ? AND {web.LIVE}
                                        ORDER BY q.pos, q.added_at, q.episode_id""", (uid,))]


def _rev(c, uid: int) -> int:
    r = c.execute("SELECT rev FROM up_next_rev WHERE user_id = ?", (uid,)).fetchone()
    return r[0] if r else 0


def _live(c, ids: list) -> set:
    if not ids:
        return set()
    marks = ",".join("?" * len(ids))
    return {r[0] for r in c.execute(f"SELECT e.id FROM episodes e WHERE e.id IN ({marks}) AND {web.LIVE}", ids)}


def _save(c, uid: int, ids: list) -> int:
    """The queue becomes `ids` (dead rows go); each keeps when it was first added. The new rev."""
    added = dict(c.execute("SELECT episode_id, added_at FROM up_next WHERE user_id = ?", (uid,)).fetchall())
    now = db.now()
    c.execute("DELETE FROM up_next WHERE user_id = ?", (uid,))
    c.executemany("INSERT INTO up_next(user_id, episode_id, pos, added_at) VALUES (?, ?, ?, ?)",
                  [(uid, e, i, added.get(e, now)) for i, e in enumerate(ids)])
    rev = _rev(c, uid) + 1
    c.execute("INSERT OR REPLACE INTO up_next_rev(user_id, rev, updated_at) VALUES (?, ?, ?)", (uid, rev, now))
    return rev


def _unique(ids: list) -> list:
    seen: set = set()
    return [x for x in ids if not (x in seen or seen.add(x))]


def _ids_arg(b: dict) -> list:
    v = b.get("ids", b.get("id"))
    if isinstance(v, str):
        v = [v]
    if not isinstance(v, list) or len(v) > MAX_QUEUE or not all(isinstance(x, str) and re.fullmatch(EID[1:-1], x) for x in v):
        raise HTTPError(400, "bad_ids", f"ids must be a list of at most {MAX_QUEUE} episode ids")
    return _unique(v)


def _view(req, ids: list, rev: int) -> dict:
    pids: list = []
    if ids:
        marks = ",".join("?" * len(ids))
        by = dict(db.conn().execute(f"SELECT id, paper_id FROM episodes WHERE id IN ({marks})", ids).fetchall())
        pids = _unique([by[e] for e in ids if e in by])
    papers = web.library(req.cfg, web._uid(req), web._admin(req), pids=pids) if pids else []
    at = {p: i for i, p in enumerate(pids)}
    papers.sort(key=lambda v: at.get(v["id"], len(at)))         # in the queue's order
    return {"ids": ids, "rev": rev, "papers": papers}


def get_queue(req):
    ensure_schema()
    c = db.conn()
    uid = web._uid(req)
    req.send_json(200, _view(req, _ids(c, uid), _rev(c, uid)))


def put_queue(req):
    web._mutation(req)
    ensure_schema()
    b = req.json()
    op = b.get("op", "set" if "ids" in b else None)
    if op not in ("set", "add", "next", "remove", "move", "clear"):
        raise HTTPError(400, "bad_op", "op must be add, next, remove, move or clear (or send the whole order as ids)")
    uid = web._uid(req)
    with db.transaction() as c:
        cur, rev = _ids(c, uid), _rev(c, uid)
        if op == "set":
            if "rev" in b and b["rev"] != rev:
                raise HTTPError(409, "moved", "Up next changed on another device", ids=cur, rev=rev)
            want = _ids_arg(b)
            live = _live(c, want)
            new = [x for x in want if x in live]
        elif op in ("add", "next"):
            want = _ids_arg(b)
            live = _live(c, want)
            want = [x for x in want if x in live]
            if not want:
                raise HTTPError(404, "not_found", "no such episode")
            if op == "add":
                new = cur + [x for x in want if x not in cur]
            else:
                new = want + [x for x in cur if x not in want]
        elif op == "remove":
            gone = set(_ids_arg(b))
            new = [x for x in cur if x not in gone]
        elif op == "move":
            eid, to = b.get("id"), b.get("to")
            if eid not in cur:
                raise HTTPError(404, "not_queued", "that episode is not in Up next")
            if not isinstance(to, int) or isinstance(to, bool):
                raise HTTPError(400, "bad_to", "to must be a whole number (0 = first)")
            new = [x for x in cur if x != eid]
            new.insert(max(0, min(len(new), to)), eid)
        else:
            new = []
        if len(new) > MAX_QUEUE:
            raise HTTPError(400, "too_long", f"Up next holds at most {MAX_QUEUE} episodes")
        changed = new != cur
        if changed:
            rev = _save(c, uid, new)
    if changed:
        events.publish("queue", {"rev": rev}, users={uid})     # this person's other tabs and devices
    req.send_json(200, _view(req, new, rev))


# ---------------------------------------------------------------- timings and the transcript

def _read(path: Path, limit: int):
    """(text, (mtime_ns, size)) of a regular UTF-8 file at most `limit` bytes, never through a
    link; (None, None) otherwise."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return None, None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size > limit:
            return None, None
        buf = bytearray()
        while True:
            chunk = os.read(fd, 1 << 16)
            if not chunk:
                break
            buf += chunk
        return bytes(buf).decode("utf-8"), (st.st_mtime_ns, st.st_size)
    except (OSError, UnicodeDecodeError):
        return None, None
    finally:
        os.close(fd)


def _segments(raw: str):
    """The voice worker's segments, each start no earlier than the one before; None when the file
    is not version 1 timings."""
    try:
        j = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(j, dict) or j.get("version") != 1 or not isinstance(j.get("segments"), list):
        return None
    out, last = [], 0.0
    for s in j["segments"]:
        if not (isinstance(s, dict) and web._num(s.get("start")) and web._num(s.get("end")) and isinstance(s.get("text"), str)):
            return None
        a = max(float(s["start"]), last)
        out.append({"start": round(a, 3), "end": round(max(float(s["end"]), a), 3), "text": s["text"]})
        last = a
    return out


def estimate(script: str, duration_s=None) -> dict:
    """Timings from the script alone: its sentences (common/checks.sentences), each a share of
    `duration_s` in proportion to its characters."""
    sents = checks.sentences(script)
    chars = sum(len(s) for s in sents)
    total = float(duration_s) if web._num(duration_s) and duration_s > 0 else round(chars / CHARS_PER_S, 1)
    segs, t = [], 0.0
    for i, s in enumerate(sents):
        end = total if i == len(sents) - 1 else t + total * len(s) / chars
        segs.append({"start": round(t, 3), "end": round(end, 3), "text": s})
        t = end
    return {"version": 1, "segments": segs, "estimated": True, "duration_s": total}


def _duration(e):
    if web._num(e["duration_s"]) and e["duration_s"] > 0:
        return e["duration_s"]
    if web._num(e["est_minutes"]) and e["est_minutes"] > 0:
        return e["est_minutes"] * 60
    return None


def _timings(cfg, e):
    """(timings or None, script or None, a signature of what they were made from)."""
    d = cfg.episodes / e["id"]
    script, ssig = _read(d / "script.md", SCRIPT_MAX)
    raw, tsig = _read(d / "timings.json", TIMINGS_MAX)
    dur = _duration(e)
    t = None
    if raw is not None:
        segs = _segments(raw)
        if segs is None:
            log.warning("%s: timings.json is not version 1 timings; estimating instead", e["id"])
        else:
            t = {"version": 1, "segments": segs, "estimated": False, "duration_s": e["duration_s"]}
    if t is None and script is not None:
        t = estimate(script, dur)
    return t, script, (ssig, tsig, dur)


def _blocks(script: str) -> list:
    """[(kind, text)]: the headings and paragraphs, as papercast-voice's textprep.parse_blocks
    reads them (without refusing other Markdown: the page shows what is there)."""
    blocks, para = [], []
    for line in script.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        s = " ".join(line.split())
        if s.startswith("#") or not s:
            if para:
                blocks.append(("para", " ".join(para)))
                para = []
            body = s.lstrip("#").strip()
            if body:
                blocks.append(("heading", body))
            continue
        para.append(s)
    if para:
        blocks.append(("para", " ".join(para)))
    return blocks


def _key(word: str) -> str:
    """A word as the matching sees it: letters and digits, lowercase ("Results." is "results")."""
    return "".join(ch for ch in word.lower() if ch.isalnum())


def transcript(script: str, timings: dict) -> dict:
    """{"blocks": [{"kind": "heading"|"para", "parts": [{"text", "seg"}]}], "segments": [{"start",
    "end"}], "estimated", "duration_s"}. Each part is a run of the script's own words that the
    segment `seg` says (null: none does, as a heading the voice left out)."""
    blocks = _blocks(script)
    toks, spans = [], []
    for b, (_kind, text) in enumerate(blocks):
        s = len(toks)
        toks += text.split()
        spans.append((s, len(toks)))
    a, a_at = [], []
    for i, w in enumerate(toks):
        k = _key(w)
        if k:
            a.append(k)
            a_at.append(i)
    segs = timings.get("segments") or []
    bw, b_seg = [], []
    for n, s in enumerate(segs):
        for w in s["text"].split():
            k = _key(w)
            if k:
                bw.append(k)
                b_seg.append(n)
    owner: list = [None] * len(toks)
    if a == bw:                         # the usual case: the voice said the script's words
        for i, n in zip(a_at, b_seg):
            owner[i] = n
    elif a and bw:
        for i, j, size in difflib.SequenceMatcher(None, a, bw, autojunk=False).get_matching_blocks():
            for t in range(size):
                owner[a_at[i + t]] = b_seg[j + t]
    # A word no segment matched (a dash, a word said differently) goes with its neighbour in the
    # same heading or paragraph, the one before it first.
    for s, e in spans:
        last = None
        for i in range(s, e):
            if owner[i] is None:
                owner[i] = last
            else:
                last = owner[i]
        nxt = None
        for i in range(e - 1, s - 1, -1):
            if owner[i] is None:
                owner[i] = nxt
            else:
                nxt = owner[i]
    out = []
    for (kind, _text), (s, e) in zip(blocks, spans):
        parts: list = []
        for i in range(s, e):
            if parts and parts[-1]["seg"] == owner[i]:
                parts[-1]["text"] += " " + toks[i]
            else:
                parts.append({"text": toks[i], "seg": owner[i]})
        out.append({"kind": kind, "parts": parts})
    return {"blocks": out, "segments": [{"start": s["start"], "end": s["end"]} for s in segs],
            "estimated": bool(timings.get("estimated")), "duration_s": timings.get("duration_s")}


def _episode(eid: str):
    e = db.conn().execute("SELECT id, state, deleted_at, duration_s, est_minutes FROM episodes WHERE id = ?", (eid,)).fetchone()
    if not e or e["deleted_at"] or e["state"] == "rejected":
        raise HTTPError(404, "not_found", "no such episode")
    return e


def get_timings(req, eid):
    t, _script, _sig = _timings(req.cfg, _episode(eid))
    if t is None:
        raise HTTPError(404, "no_script", "this episode has no script")
    req.send_json(200, t)


_made: dict = {}            # episode id -> (signature, transcript): matching words is not free
_made_lock = threading.Lock()


def get_transcript(req, eid):
    """Always 200 for a live episode: without a script, no blocks (the page shows nothing)."""
    e = _episode(eid)
    t, script, sig = _timings(req.cfg, e)
    if t is None or script is None:
        req.send_json(200, {"blocks": [], "segments": [], "estimated": False, "duration_s": e["duration_s"]})
        return
    with _made_lock:
        hit = _made.get(eid)
    if hit and hit[0] == sig:
        req.send_json(200, hit[1])
        return
    v = transcript(script, t)
    with _made_lock:
        _made[eid] = (sig, v)
        while len(_made) > 64:
            _made.pop(next(iter(_made)))
    req.send_json(200, v)


ROUTES = [
    ("GET", r"^/api/queue$", get_queue, "viewer"),
    ("PUT", r"^/api/queue$", put_queue, "viewer"),
    ("GET", rf"^/api/episodes/{EID}/timings$", get_timings, "viewer"),
    ("GET", rf"^/api/episodes/{EID}/transcript$", get_transcript, "viewer"),
]
