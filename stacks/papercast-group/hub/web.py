"""The browser API: library, listened, positions, prefs, admin, audio, explainer, SSE (SPEC.md section 7). Owner: A4.

  GET    /api/config                      who is looking, the auth mode, the page's build, everyone's picture
  GET    /api/library?q=&graph=&tag=...   papers with at least one live episode, for this user;
                                          q searches everything (search.py), the rest filter
  GET    /api/papers/<id>                 one paper, as in the library
  PUT    /api/papers/<id>/listened        {"listened": bool}: this user's tick, never automatic
  PUT    /api/episodes/<id>/position      {"s": seconds, "at": ms since the epoch}: newest wins
  DELETE /api/episodes/<id>               maker or admin: hidden at once, undo for 30 days
  POST   /api/episodes/<id>/undelete
  GET    /api/prefs, PUT /api/prefs       {"settings", "note"}, checked by common/prefs.py
  GET    /api/admin/base, POST            base prompt versions (admin)
  GET    /audio/<episode_id>.mp3          Range supported
  GET    /x/<episode_id>/explainer.html   Leo's explainer CSP: an opaque origin, no API
  GET    /api/events                      SSE: paper, episode, graph, log; hello, resync

A live episode is one not deleted and not rejected; a paper is in the library while it has one.
Every mutation needs `X-PCG: 1` (SPEC section 2), checked here as well as in auth: a header a
page at another origin, or the sandboxed explainer, cannot send without a preflight the hub never
answers."""
from __future__ import annotations

import hashlib
import json
import os
import queue
import re
import select
import socket
import stat
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import avatars, db, events, search, voices
from .app import HTTPError

try:
    from papercast_cli.common import prefs as P, wording as W
except ImportError:         # a checkout: the package sits next to the hub, not pip-installed
    sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "packages" / "papercast-cli"))
    from papercast_cli.common import prefs as P, wording as W

UNDO_DAYS = 30
KEEPALIVE_S = 15.0
MAX_SSE = 64
GUIDELINE_MAX = 200_000
# Leo's (stacks/papercast/web/app.py EXPLAINER_CSP), verbatim. `sandbox` without
# allow-same-origin gives the explainer an opaque origin: no cookies, no storage, and every
# request it could make carries `Origin: null`.
EXPLAINER_CSP = ("sandbox allow-scripts allow-popups; default-src 'none'; img-src data: blob:; "
                 "style-src 'unsafe-inline'; script-src 'unsafe-inline'; font-src data:; "
                 "media-src data: blob:; connect-src 'none'; form-action 'none'; base-uri 'none'")
LIVE = "e.deleted_at IS NULL AND e.state != 'rejected'"
PID = r"(p_[a-z0-9]{4,32})"
EID = r"(e_[a-z0-9]{4,32})"

_sse_slots = threading.BoundedSemaphore(MAX_SSE)


# ---------------------------------------------------------------- small helpers

def _u(req, k):
    try:
        return req.user[k]
    except (KeyError, IndexError, TypeError):
        return None


def _uid(req) -> int:
    return _u(req, "id")


def _admin(req) -> bool:
    return _u(req, "role") == "admin"


def _mutation(req) -> None:
    """The CSRF fence for a changing request (SPEC section 2): the page's own header, and never a
    request the browser marks as coming from another site."""
    # First read whatever was sent, used or not, refused or not: left in the socket it would
    # become the start of the next request on this keep-alive connection.
    req.body()
    if req.headers.get("X-PCG") != "1":
        raise HTTPError(403, "csrf", "a changing request needs the page's X-PCG header")
    site = req.headers.get("Sec-Fetch-Site")
    if site is not None and site not in ("same-origin", "none"):
        raise HTTPError(403, "cross_origin", "cross-site request refused")


def _iso_ms(ms: int) -> str:
    d = _EPOCH + timedelta(milliseconds=int(ms))
    return d.strftime("%Y-%m-%dT%H:%M:%S.") + f"{int(ms) % 1000:03d}Z"


def _ms(iso) -> int:
    """ISO 8601 UTC ("...Z", with or without a fraction) to ms since the epoch; 0 if unreadable."""
    if not iso:
        return 0
    try:
        d = datetime.fromisoformat(str(iso).rstrip("Z"))
    except ValueError:
        return 0
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return (d - _EPOCH) // timedelta(milliseconds=1)       # exact: a float product can be 1 ms off


_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and v == v and abs(v) != float("inf")


_builds: dict = {}


def build(cfg) -> str:
    """A hash of the static files: the page reloads itself when the hub serves a newer one."""
    d = cfg.static
    try:
        files = sorted(p for p in d.iterdir() if p.is_file())
    except OSError:
        return ""
    sig = tuple((p.name, p.stat().st_mtime_ns, p.stat().st_size) for p in files)
    hit = _builds.get(str(d))
    if hit and hit[0] == sig:
        return hit[1]
    h = hashlib.sha256()
    for p in files:
        h.update(p.name.encode() + b"\0" + p.read_bytes() + b"\0")
    _builds[str(d)] = (sig, h.hexdigest()[:12])
    return _builds[str(d)][1]


# ---------------------------------------------------------------- the library

def _episode_rows(uid: int, pids: list | None = None):
    only = f" AND e.paper_id IN ({','.join('?' * len(pids))})" if pids else ""
    sql = f"""SELECT e.id, e.paper_id, e.made_by, u.name AS maker, e.state, e.state_detail, e.prefs_summary,
                     e.duration_s, e.est_minutes, e.model, e.base_version, e.created_at,
                     v.phase AS phase, v.progress AS progress,
                     pos.seconds AS pos_s, pos.updated_at AS pos_at
              FROM episodes e JOIN users u ON u.id = e.made_by
              LEFT JOIN voice_jobs v ON v.episode_id = e.id
              LEFT JOIN positions pos ON pos.episode_id = e.id AND pos.user_id = ?
              WHERE {LIVE}{only}
              ORDER BY e.created_at, e.id"""
    return db.conn().execute(sql, [uid] + list(pids or [])).fetchall()


def _paper_rows(uid: int, pids: list | None = None):
    where = (f"p.id IN ({','.join('?' * len(pids))})" if pids
             else f"p.id IN (SELECT e.paper_id FROM episodes e WHERE {LIVE})")
    sql = f"""SELECT p.*, l.at AS listened_at FROM papers p
              LEFT JOIN listened l ON l.paper_id = p.id AND l.user_id = ?
              WHERE {where}"""
    return db.conn().execute(sql, [uid] + list(pids or [])).fetchall()


def _regular(path: str, follow: bool) -> bool:
    try:
        return stat.S_ISREG((os.stat if follow else os.lstat)(path).st_mode)
    except (OSError, ValueError):
        return False


def _ep_view(e, uid: int, admin: bool, epdir: Path) -> dict:
    # plain strings and one stat each: a library of a thousand papers asks this thousands of
    # times per answer, and pathlib's objects cost more than the stat
    d = os.path.join(str(epdir), e["id"])
    return {
        "id": e["id"], "made_by": {"id": e["made_by"], "name": e["maker"]}, "mine": e["made_by"] == uid,
        "prefs_summary": e["prefs_summary"] or "", "state": e["state"], "state_detail": e["state_detail"],
        "phase": e["phase"], "progress": e["progress"], "duration_s": e["duration_s"],
        "est_minutes": e["est_minutes"], "model": e["model"], "base_version": e["base_version"],
        "created_at": e["created_at"],
        "has_audio": e["state"] == "ready" and _regular(os.path.join(d, "audio.mp3"), False),     # a file, not a link
        "has_explainer": _regular(os.path.join(d, "explainer.html"), True),
        "position_s": e["pos_s"], "position_at": _ms(e["pos_at"]) if e["pos_at"] else 0,
        "can_delete": admin or e["made_by"] == uid,
    }


def _paper_view(p, eps: list) -> dict:
    return {
        "id": p["id"], "title": p["title"], "authors": db.loads(p["authors"], []) or [], "year": p["year"],
        "arxiv_id": p["arxiv_id"], "doi": p["doi"], "url": p["url"], "tags": db.loads(p["tags"], []) or [],
        "created_at": p["created_at"], "added_at": min(e["created_at"] for e in eps) if eps else p["created_at"],
        "listened": p["listened_at"] is not None, "listened_at": p["listened_at"], "episodes": eps,
    }


def _matches(v: dict, words: list) -> bool:
    hay = " ".join([v["title"] or "", " ".join(map(str, v["authors"])), " ".join(map(str, v["tags"])),
                    " ".join(e["made_by"]["name"] or "" for e in v["episodes"]),
                    v["arxiv_id"] or "", v["doi"] or "", str(v["year"] or "")]).lower()
    return all(w in hay for w in words)


def library(cfg, uid: int, admin: bool, q: str | None = None, pids: list | None = None) -> list:
    """Papers with at least one live episode, newest first (by their first live episode), each
    with its live episodes (oldest first), who made them, this user's tick and positions.
    `q`: every word must appear in the title, authors, tags, makers' names, arXiv id, DOI or year.
    `pids`: only these papers (those of them still in the library)."""
    by_paper: dict = {}
    av = avatars.versions()
    for e in _episode_rows(uid, pids):
        v = _ep_view(e, uid, admin, cfg.episodes)
        v["made_by"]["avatar"] = av.get(e["made_by"])       # the maker's picture (avatars.py)
        by_paper.setdefault(e["paper_id"], []).append(v)
    out = [_paper_view(p, by_paper[p["id"]]) for p in _paper_rows(uid, pids) if p["id"] in by_paper]
    voices.decorate(out, cfg, uid, admin)       # each episode's `voice` (voices.py)
    words = (q or "").lower().split()
    if words:
        out = [v for v in out if _matches(v, words)]
    out.sort(key=lambda v: (v["added_at"] or "", v["id"]), reverse=True)
    return out


def _one(req, pid: str) -> dict:
    got = library(req.cfg, _uid(req), _admin(req), pids=[pid])
    if not got:
        raise HTTPError(404, "not_found", "no such paper in the library")
    return got[0]


def get_config(req):
    av = avatars.versions()
    req.send_json(200, {"me": {"id": _uid(req), "name": _u(req, "name"), "email": _u(req, "email"),
                               "role": _u(req, "role"), "avatar": av.get(_uid(req))},
                        "auth": req.cfg.auth, "build": build(req.cfg), "undo_days": UNDO_DAYS,
                        "public_url": req.cfg.public_url,     # the address `papercast login --server` takes
                        "avatars": {str(k): v for k, v in av.items()}})     # everyone's picture (the map's cards)


def get_library(req):
    """?q= searches (hub/search.py): the papers best first, each with `match` (where it matched,
    a snippet, its title's marks), and `search` (the count, the query as corrected, if it was).
    graph, tag, maker, year_from, year_to, listened filter, with q or without (then newest
    first). ?ids=p_a,p_b (at most 50) reads just those again (after an event): a paper that has
    left the library is simply not in the answer; with q, each says where it matches (the rows
    past the first page ask so)."""
    q = (req.arg("q") or "")[:200]
    ids = req.arg("ids")
    cfg, uid, admin = req.cfg, _uid(req), _admin(req)
    f = search.filters_from(req.query)
    if ids is not None:
        pids = [x for x in ids.split(",") if re.fullmatch(PID[1:-1], x)][:50]
        papers = library(cfg, uid, admin, pids=pids) if pids else []
        if papers and q.strip():
            res = search.run(cfg, uid, q, pids=[p["id"] for p in papers])
            for p in papers:
                p["match"] = res["match"].get(p["id"])
        req.send_json(200, {"papers": papers})
        return
    if not q.strip() and not f:
        req.send_json(200, {"papers": library(cfg, uid, admin)})
        return
    res = search.run(cfg, uid, q, f)
    views = {v["id"]: v for v in library(cfg, uid, admin, pids=res["ids"])} if res["ids"] else {}
    papers = []
    for pid in res["ids"]:
        v = views.get(pid)
        if v is not None:
            if pid in res["match"]:
                v["match"] = res["match"][pid]
            papers.append(v)
    res["info"]["n"] = len(papers)
    req.send_json(200, {"papers": papers, "search": res["info"]})


def get_paper(req, pid):
    req.send_json(200, _one(req, pid))


def put_listened(req, pid):
    _mutation(req)
    v = req.json().get("listened")
    if not isinstance(v, bool):
        raise HTTPError(400, "bad_listened", "listened must be true or false")
    c = db.conn()
    if not c.execute(f"SELECT 1 FROM episodes e WHERE e.paper_id = ? AND {LIVE}", (pid,)).fetchone():
        raise HTTPError(404, "not_found", "no such paper in the library")
    if v:
        c.execute("INSERT OR IGNORE INTO listened(user_id, paper_id, at) VALUES (?, ?, ?)", (_uid(req), pid, db.now()))
    else:
        c.execute("DELETE FROM listened WHERE user_id = ? AND paper_id = ?", (_uid(req), pid))
    # This user's other tabs and devices; nobody else's tick changed.
    events.publish("paper", {"id": pid, "paper_id": pid, "why": "listened"}, users={_uid(req)})
    req.send_json(200, _one(req, pid))


def put_position(req, eid):
    """The player position, with when it was taken (`at`, ms, the device's clock; the hub's when
    absent or absurd). The newer one wins, so a late PUT from a device that stopped earlier cannot
    overwrite where the phone got to."""
    _mutation(req)
    b = req.json()
    s, at = b.get("s"), b.get("at")
    if not _num(s) or s < 0:
        raise HTTPError(400, "bad_position", "s must be a number of seconds")
    now = int(time.time() * 1000)
    if not _num(at) or at <= 0 or at > now + 86_400_000:
        at = now
    at = int(at)
    with db.transaction() as c:
        e = c.execute("SELECT deleted_at FROM episodes WHERE id = ?", (eid,)).fetchone()
        if not e or e["deleted_at"]:
            raise HTTPError(404, "not_found", "no such episode")
        old = c.execute("SELECT updated_at FROM positions WHERE user_id = ? AND episode_id = ?",
                        (_uid(req), eid)).fetchone()
        if old and _ms(old["updated_at"]) > at:
            kept = True
        else:
            kept = False
            c.execute("INSERT OR REPLACE INTO positions(user_id, episode_id, seconds, updated_at) VALUES (?, ?, ?, ?)",
                      (_uid(req), eid, round(min(float(s), 1e6), 1), _iso_ms(at)))
    req.send_json(200, {"ok": True, "kept_newer": kept})


def _episode(eid: str):
    return db.conn().execute("SELECT id, paper_id, made_by, deleted_at FROM episodes WHERE id = ?", (eid,)).fetchone()


def delete_episode(req, eid):
    _mutation(req)
    e = _episode(eid)
    if not e or e["deleted_at"]:
        raise HTTPError(404, "not_found", "no such episode")
    if e["made_by"] != _uid(req) and not _admin(req):
        raise HTTPError(403, "not_yours", "only its maker or an admin can delete a version")
    now = db.now()
    db.conn().execute("UPDATE episodes SET deleted_at = ?, updated_at = ? WHERE id = ? AND deleted_at IS NULL",
                      (now, now, eid))
    events.publish("episode", {"id": eid, "episode_id": eid, "paper_id": e["paper_id"], "deleted": True})
    until = datetime.now(timezone.utc) + timedelta(days=UNDO_DAYS)
    req.send_json(200, {"id": eid, "paper_id": e["paper_id"], "deleted_at": now,
                        "undo_until": until.strftime("%Y-%m-%dT%H:%M:%SZ"), "undo_s": UNDO_DAYS * 86400})


def undelete_episode(req, eid):
    _mutation(req)
    e = _episode(eid)
    if not e:
        raise HTTPError(404, "not_found", "no such episode")
    if e["made_by"] != _uid(req) and not _admin(req):
        raise HTTPError(403, "not_yours", "only its maker or an admin can bring a version back")
    if e["deleted_at"]:
        if time.time() * 1000 - _ms(e["deleted_at"]) > UNDO_DAYS * 86_400_000:
            raise HTTPError(410, "undo_expired", "Too late: it is deleted.")
        db.conn().execute("UPDATE episodes SET deleted_at = NULL, updated_at = ? WHERE id = ?", (db.now(), eid))
        events.publish("episode", {"id": eid, "episode_id": eid, "paper_id": e["paper_id"], "deleted": False})
    req.send_json(200, _one(req, e["paper_id"]))


# ---------------------------------------------------------------- preferences

def _prefs_view(uid: int) -> dict:
    r = db.conn().execute("SELECT settings, note, version, updated_at FROM prefs WHERE user_id = ?", (uid,)).fetchone()
    settings = P.full(db.loads(r["settings"], {}) if r else {})
    return {"settings": settings, "note": r["note"] if r else "", "version": r["version"] if r else 0,
            "updated_at": r["updated_at"] if r else None, "summary": P.summary(settings),
            "choices": {k: list(v) for k, v in P.SCHEMA.items()}, "defaults": dict(P.DEFAULTS), "note_max": P.NOTE_MAX}


def get_prefs(req):
    req.send_json(200, _prefs_view(_uid(req)))


def put_prefs(req):
    _mutation(req)
    b = req.json()
    settings, note = b.get("settings", {}), b.get("note", "")
    problems = P.validate(settings, note)
    if problems:
        raise HTTPError(400, "bad_prefs", "; ".join(problems), problems=problems)
    settings, note = P.full(settings), note.strip()
    uid = _uid(req)
    with db.transaction() as c:
        r = c.execute("SELECT settings, note, version FROM prefs WHERE user_id = ?", (uid,)).fetchone()
        if not r:
            c.execute("INSERT INTO prefs(user_id, settings, note, version, updated_at) VALUES (?, ?, ?, 1, ?)",
                      (uid, db.dumps(settings), note, db.now()))
        elif P.full(db.loads(r["settings"], {})) != settings or r["note"] != note:   # unchanged: same version
            c.execute("UPDATE prefs SET settings = ?, note = ?, version = ?, updated_at = ? WHERE user_id = ?",
                      (db.dumps(settings), note, r["version"] + 1, db.now(), uid))
    req.send_json(200, _prefs_view(uid))


# ---------------------------------------------------------------- base prompt (admin)

def _wording_problems(w) -> list:
    """What common/wording.py's build() refuses (it is the reader, so it is the judge: the
    listener-name class may be empty there, as the uploader's name fills it at check time)."""
    if not isinstance(w, dict) or not isinstance(w.get("classes"), list) or not w["classes"]:
        return ["wording must be an object with a non-empty list of classes"]
    try:
        W.build(w)
    except Exception as e:
        return [str(e)]
    return []


def _base_rows():
    return db.conn().execute("""SELECT b.version, b.guideline, b.wording, b.created_at, b.created_by, u.name
                                FROM base_prompts b LEFT JOIN users u ON u.id = b.created_by
                                ORDER BY b.version DESC""").fetchall()


def _base_view(r) -> dict:
    return {"version": r["version"], "guideline": r["guideline"], "wording": db.loads(r["wording"], {}),
            "created_at": r["created_at"],
            "created_by": {"id": r["created_by"], "name": r["name"]} if r["created_by"] is not None else None}


def get_base(req):
    rows = _base_rows()
    req.send_json(200, {"versions": [_base_view(r) for r in rows], "latest": rows[0]["version"] if rows else None})


def post_base(req):
    """A new base prompt version: the guideline, and the wording list (the newest version's when
    left out). Old versions stay: an episode records the version it was made with."""
    _mutation(req)
    b = req.json()
    g, w = b.get("guideline"), b.get("wording")
    if not isinstance(g, str) or not g.strip():
        raise HTTPError(400, "bad_guideline", "the guideline must be some text")
    g = g.replace("\r\n", "\n")
    if len(g.encode()) > GUIDELINE_MAX:
        raise HTTPError(400, "too_long", f"the guideline is over {GUIDELINE_MAX // 1000} kB")
    if isinstance(w, str):
        try:
            w = json.loads(w)
        except ValueError as e:
            raise HTTPError(400, "bad_wording", f"the wording list is not JSON: {e}")
    with db.transaction() as c:
        last = c.execute("SELECT version, wording FROM base_prompts ORDER BY version DESC LIMIT 1").fetchone()
        if w is None:
            w = db.loads(last["wording"], None) if last else None
            if w is None:
                w = json.loads(Path(W.PATH).read_text(encoding="utf-8"))
        problems = _wording_problems(w)
        if problems:
            raise HTTPError(400, "bad_wording", "; ".join(problems), problems=problems)
        n = (last["version"] if last else 0) + 1
        c.execute("INSERT INTO base_prompts(version, guideline, wording, created_by, created_at) VALUES (?, ?, ?, ?, ?)",
                  (n, g, db.dumps(w), _uid(req), db.now()))
    r = next(x for x in _base_rows() if x["version"] == n)
    req.send_json(201, _base_view(r))


# ---------------------------------------------------------------- audio and the explainer

def _live_episode(eid: str):
    e = db.conn().execute("SELECT state, deleted_at FROM episodes WHERE id = ?", (eid,)).fetchone()
    return e if e and not e["deleted_at"] and e["state"] != "rejected" else None


def get_audio(req, eid):
    f = req.cfg.episodes / eid / "audio.mp3"
    if not _live_episode(eid) or f.is_symlink() or not f.is_file():
        raise HTTPError(404, "not_found", "no audio for this episode")
    req.send_file(f, "audio/mpeg", {"Cache-Control": "private, max-age=3600",
                                    "Cross-Origin-Resource-Policy": "same-origin"})


def get_explainer(req, eid):
    if not _live_episode(eid):
        raise HTTPError(404, "not_found", "no explainer for this episode")
    try:
        fd = os.open(req.cfg.episodes / eid / "explainer.html", os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        raise HTTPError(404, "not_found", "no explainer for this episode") from None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise HTTPError(404, "not_found", "no explainer for this episode")
        h = req.h
        h.send_response(200)
        for k, v in (("Content-Type", "text/html; charset=utf-8"), ("Content-Length", str(st.st_size)),
                     ("Content-Security-Policy", EXPLAINER_CSP), ("Cache-Control", "no-store"),
                     ("X-Frame-Options", "SAMEORIGIN"), ("X-Content-Type-Options", "nosniff"),
                     ("Referrer-Policy", "no-referrer"), ("Cross-Origin-Resource-Policy", "same-origin"),
                     ("Cross-Origin-Opener-Policy", "same-origin")):
            h.send_header(k, v)
        h.end_headers()
        req.sent = True
        if req.method == "HEAD":
            return
        while True:         # streamed: an explainer may be 8 MB of inlined figures
            buf = os.read(fd, 256 * 1024)
            if not buf:
                break
            h.wfile.write(buf)
    finally:
        os.close(fd)


# ---------------------------------------------------------------- live events

def _gone(sock) -> bool:
    """The page went away: its socket reads as closed (an event stream's client never sends)."""
    try:
        if not select.select([sock], [], [], 0)[0]:
            return False
        return sock.recv(1, socket.MSG_PEEK) == b""
    except (OSError, ValueError):
        return True


def get_events(req):
    """Server-sent events for this user: every event events.publish() sends to them, a comment
    every KEEPALIVE_S so proxies keep the line open, and `hello` (the build) first. The hub keeps
    no history, so a page coming back (Last-Event-ID, or ?last=) is told to `resync`: it reads
    the library again. A page that left is noticed within a second, so its slot is free again."""
    if not _sse_slots.acquire(blocking=False):
        raise HTTPError(503, "busy", "too many open event streams")
    sub = events.subscribe(_uid(req))
    try:
        w = req.start_stream("text/event-stream; charset=utf-8")

        def send(eid, kind, data):
            if eid is not None:
                w.write(f"id: {eid}\n".encode())
            w.write(f"event: {kind}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode())

        w.write(b"retry: 3000\n\n")
        send(0, "hello", {"build": build(req.cfg)})     # an id, so a reconnect always says Last-Event-ID
        if req.headers.get("Last-Event-ID") or req.arg("last"):
            send(None, "resync", {})
        w.flush()
        wrote = time.monotonic()
        while True:
            try:
                item = sub.q.get(timeout=min(1.0, KEEPALIVE_S))
            except queue.Empty:
                if _gone(req.h.connection):
                    return
                if time.monotonic() - wrote >= KEEPALIVE_S:
                    w.write(b": ping\n\n")
                    w.flush()
                    wrote = time.monotonic()
                continue
            while item is not None:         # whatever else is waiting goes in the same flush
                send(*item)
                try:
                    item = sub.q.get_nowait()
                except queue.Empty:
                    item = None
            w.flush()
            wrote = time.monotonic()
    except (BrokenPipeError, ConnectionResetError, TimeoutError, OSError):
        pass
    finally:
        events.unsubscribe(sub)
        _sse_slots.release()


ROUTES = [
    ("GET", r"^/api/config$", get_config, "viewer"),
    ("GET", r"^/api/library$", get_library, "viewer"),
    ("GET", rf"^/api/papers/{PID}$", get_paper, "viewer"),
    ("PUT", rf"^/api/papers/{PID}/listened$", put_listened, "viewer"),
    ("PUT", rf"^/api/episodes/{EID}/position$", put_position, "viewer"),
    ("DELETE", rf"^/api/episodes/{EID}$", delete_episode, "viewer"),
    ("POST", rf"^/api/episodes/{EID}/undelete$", undelete_episode, "viewer"),
    ("GET", r"^/api/prefs$", get_prefs, "viewer"),
    ("PUT", r"^/api/prefs$", put_prefs, "viewer"),
    ("GET", r"^/api/admin/base$", get_base, "admin"),
    ("POST", r"^/api/admin/base$", post_base, "admin"),
    ("GET", rf"^/audio/{EID}\.mp3$", get_audio, "viewer"),
    ("GET", rf"^/x/{EID}/explainer\.html$", get_explainer, "viewer"),
    ("GET", r"^/api/events$", get_events, "viewer"),
]
