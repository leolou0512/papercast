"""Comments on the episodes, and the board of what happened (Leo, 2026-09-28: "a comment section
could also be useful for each podcast. With time stamp jumps etc." and "a small announcement
board, e.g. Federico has uploaded xxxxxxx. 3 hr ago.").

Comments belong to a paper and name the version (episode) they were written about: the times in
them ("12:40", "1:02:03") are that version's, and the page turns them into links that play it from
there. Plain text, at most 2,000 characters; replies are one level deep (a reply to a reply joins
its thread). People edit or delete their own; admins delete anyone's. A deleted comment loses its
text; with replies under it, it stays as "comment deleted", without, it is gone.

The board is built from what the hub already records: episodes (an upload is a paper's first live
version, a new version any later one), comments, graph_log (a person's edits of one graph, each
within an hour of the one before, are one item; the agent's links come with the upload and are
not listed) and users ("joined"). Only what is recorded nowhere else has a table here: the pinned
notice, when each person last opened the board and, with password sign-in, when a person first
chose their own password (users.created_at is when an admin put them on the list, and pw_set_at
moves with every later change). Nothing is filtered on users.disabled: people removed from the
list keep their items, credited to them.

  GET    /api/papers/<id>/comments      the paper's comments, oldest first
  POST   /api/papers/<id>/comments      {"body", "episode_id"?, "parent_id"?}
  PUT    /api/comments/<id>             {"body"}: its writer only
  DELETE /api/comments/<id>             its writer or an admin
  GET    /api/comments/counts           {"counts": {paper id: live comments}} for the list's rows
  GET    /api/board?limit=5             items newest first, "more", the pinned notice, seen_at, now
  PUT    /api/board/seen                {"at"}: this person has seen the board up to that item
  POST   /api/board/notice              {"body"} (admin): pin a notice; it replaces the one pinned
  DELETE /api/board/notice/<id>         (admin): unpin it

Events: `comment` {"paper_id", "id", "action": new | edit | delete, "count", "comment"} to
everyone; `board` {"why": "notice"} to everyone, {"why": "seen", "seen_at"} to that person."""
from __future__ import annotations

import logging
import re
import threading
import time
import unicodedata
from collections import deque
from datetime import datetime, timedelta, timezone

from . import avatars, db, events
from .app import HTTPError

log = logging.getLogger("pcg.social")

BODY_MAX = 2000
NOTICE_MAX = 280
GROUP_S = 3600              # graph edits (and comments) by one person, each within an hour of the last: one item
BOARD_LIMIT_MAX = 200
SCAN_MAX = 5000             # the newest rows of each source the board is made from
POST_LIMIT, POST_WINDOW = 30, 300       # comments per person in 5 min: a runaway script, not a person
LIVE = "e.deleted_at IS NULL AND e.state != 'rejected'"
PID = r"(p_[a-z0-9]{4,32})"

SCHEMA = """
CREATE TABLE IF NOT EXISTS comments (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  paper_id TEXT NOT NULL REFERENCES papers(id),
  episode_id TEXT REFERENCES episodes(id),       -- the version its times are in
  parent_id INTEGER REFERENCES comments(id),     -- a reply: the comment its thread starts with
  user_id INTEGER NOT NULL REFERENCES users(id),
  body TEXT NOT NULL,                            -- '' once deleted
  created_at TEXT NOT NULL,
  edited_at TEXT,
  deleted_at TEXT,
  deleted_by INTEGER REFERENCES users(id)
);
CREATE INDEX IF NOT EXISTS comments_paper ON comments(paper_id, id);
CREATE INDEX IF NOT EXISTS comments_parent ON comments(parent_id) WHERE parent_id IS NOT NULL;
CREATE TABLE IF NOT EXISTS board_notices (       -- an admin's pinned notice
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  body TEXT NOT NULL,
  user_id INTEGER REFERENCES users(id),
  created_at TEXT NOT NULL,
  unpinned_at TEXT,
  unpinned_by INTEGER REFERENCES users(id)
);
CREATE TABLE IF NOT EXISTS board_seen (          -- the newest item each person has seen
  user_id INTEGER PRIMARY KEY REFERENCES users(id),
  at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS board_joins (         -- password sign-in: when someone first chose their own password
  user_id INTEGER PRIMARY KEY REFERENCES users(id),
  at TEXT NOT NULL
)
"""

_ready: set = set()
_ready_lock = threading.Lock()


def ensure_schema() -> None:
    """This module's tables, once per database (db.py is A1's: nothing of this is in it)."""
    key = str(db._path)
    if key in _ready:
        return
    with _ready_lock:
        if key in _ready:
            return
        c = db.conn()
        for stmt in SCHEMA.split(";"):          # one by one: executescript would commit a caller's transaction
            if stmt.strip():
                c.execute(stmt)
        if not c.in_transaction:
            _ready.add(key)


def start(cfg) -> None:
    ensure_schema()


# ---------------------------------------------------------------- small helpers

def _uid(req):
    return req.user["id"]


def _admin(req) -> bool:
    return req.user["role"] == "admin"


def _mutation(req) -> None:
    """The CSRF fence (SPEC section 2), as web.py's: the page's header, never another site."""
    req.body()
    if req.headers.get("X-PCG") != "1":
        raise HTTPError(403, "csrf", "a changing request needs the page's X-PCG header")
    site = req.headers.get("Sec-Fetch-Site")
    if site is not None and site not in ("same-origin", "none"):
        raise HTTPError(403, "cross_origin", "cross-site request refused")


# bidi embeddings, overrides and isolates: they can make a comment read as something it is not
_BIDI = set("‪‫‬‭‮⁦⁧⁨⁩")


def clean_body(v, limit: int = BODY_MAX) -> str:
    """Someone's plain text: line breaks kept (at most one empty line in a row), other control
    characters out, trimmed; 1 to `limit` characters."""
    if not isinstance(v, str):
        raise HTTPError(400, "bad_body", "a comment is text")
    v = v.replace("\r\n", "\n").replace("\r", "\n").replace(" ", "\n").replace(" ", "\n")
    v = "".join(ch for ch in v if ch in "\n\t" or (unicodedata.category(ch) != "Cc" and ch not in _BIDI))
    v = re.sub(r"[ \t]+\n", "\n", v)
    v = re.sub(r"\n{3,}", "\n\n", v).strip()
    if not v:
        raise HTTPError(400, "empty", "the comment is empty")
    if len(v) > limit:
        raise HTTPError(400, "too_long", f"at most {limit:,} characters")
    return v


def clean_notice(v) -> str:
    if not isinstance(v, str):
        raise HTTPError(400, "bad_body", "a notice is text")
    v = "".join(" " if unicodedata.category(ch) == "Cc" or ch in _BIDI or ch in "  " else ch for ch in v)
    v = re.sub(r"\s+", " ", v).strip()
    if not v:
        raise HTTPError(400, "empty", "the notice is empty")
    if len(v) > NOTICE_MAX:
        raise HTTPError(400, "too_long", f"a notice is at most {NOTICE_MAX} characters")
    return v


def _t(iso) -> float:
    """ISO 8601 UTC ("...Z", a fraction allowed) to seconds since the epoch; 0 if unreadable."""
    try:
        d = datetime.fromisoformat(str(iso).rstrip("Z"))
    except (TypeError, ValueError):
        return 0.0
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.timestamp()


def _iso(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class _Limit:
    """At most n events per key in a sliding window."""

    def __init__(self, n: int, window: float):
        self.n, self.window = n, window
        self.hits: dict = {}
        self.lock = threading.Lock()

    def hit(self, key) -> bool:
        now = time.monotonic()
        with self.lock:
            q = self.hits.setdefault(key, deque())
            while q and now - q[0] > self.window:
                q.popleft()
            if len(q) >= self.n:
                return False
            q.append(now)
            return True


_posts = _Limit(POST_LIMIT, POST_WINDOW)


# ---------------------------------------------------------------- comments

COMMENT_SQL = """SELECT c.id, c.paper_id, c.episode_id, c.parent_id, c.user_id, u.name, c.body, c.created_at,
                        c.edited_at, c.deleted_at
                 FROM comments c LEFT JOIN users u ON u.id = c.user_id"""


def view(r, av: dict | None = None) -> dict:
    """A comment as everyone sees it (the page works out what this person may do with it). A
    deleted one keeps only its place: no text, no name. `av`: avatars.versions(), when known."""
    gone = r["deleted_at"] is not None
    pic = None if gone else av.get(r["user_id"]) if av is not None else avatars.version_of(r["user_id"])
    return {"id": r["id"], "paper_id": r["paper_id"], "episode_id": r["episode_id"], "parent_id": r["parent_id"],
            "user": None if gone else {"id": r["user_id"], "name": r["name"] or "someone", "avatar": pic},
            "body": "" if gone else r["body"], "created_at": r["created_at"],
            "edited_at": None if gone else r["edited_at"], "deleted": gone}


def _in_library(c, pid: str) -> bool:
    return c.execute(f"SELECT 1 FROM episodes e WHERE e.paper_id = ? AND {LIVE} LIMIT 1", (pid,)).fetchone() is not None


def _count(c, pid: str) -> int:
    return c.execute("SELECT COUNT(*) FROM comments WHERE paper_id = ? AND deleted_at IS NULL", (pid,)).fetchone()[0]


def comments_of(pid: str, c=None) -> list:
    """The paper's comments, oldest first: live ones, and a deleted one only while live replies
    hang under it."""
    c = c or db.conn()
    rows = c.execute(COMMENT_SQL + " WHERE c.paper_id = ? ORDER BY c.id", (pid,)).fetchall()
    live_under = {r["parent_id"] for r in rows if r["parent_id"] is not None and r["deleted_at"] is None}
    av = avatars.versions(c)
    return [view(r, av) for r in rows
            if r["deleted_at"] is None or (r["parent_id"] is None and r["id"] in live_under)]


def _one(c, cid: int):
    return c.execute(COMMENT_SQL + " WHERE c.id = ?", (cid,)).fetchone()


def _publish(c, pid: str, cid: int, action: str) -> None:
    r = _one(c, cid)
    events.publish("comment", {"paper_id": pid, "id": cid, "action": action, "count": _count(c, pid),
                               "comment": view(r) if r else None})


def get_comments(req, pid):
    ensure_schema()
    c = db.conn()
    if not _in_library(c, pid):
        raise HTTPError(404, "not_found", "no such paper in the library")
    req.send_json(200, {"paper_id": pid, "comments": comments_of(pid, c), "max": BODY_MAX})


def post_comment(req, pid):
    ensure_schema()
    _mutation(req)
    b = req.json()
    body = clean_body(b.get("body"))
    eid, parent = b.get("episode_id"), b.get("parent_id")
    if eid is not None and not isinstance(eid, str):
        raise HTTPError(400, "bad_episode", "episode_id is a version of this paper")
    if parent is not None and (not isinstance(parent, int) or isinstance(parent, bool)):
        raise HTTPError(400, "bad_parent", "parent_id is a comment of this paper")
    if not _posts.hit(_uid(req)):
        raise HTTPError(429, "slow_down", "that is a lot of comments at once: wait a few minutes", retry_after=60)
    with db.transaction() as c:
        if not _in_library(c, pid):
            raise HTTPError(404, "not_found", "no such paper in the library")
        if eid is not None and c.execute(f"SELECT 1 FROM episodes e WHERE e.id = ? AND e.paper_id = ? AND {LIVE}",
                                         (eid, pid)).fetchone() is None:
            raise HTTPError(400, "bad_episode", "that is not a version of this paper")
        if parent is not None:
            p = c.execute("SELECT id, paper_id, parent_id, deleted_at FROM comments WHERE id = ?", (parent,)).fetchone()
            if p is None or p["paper_id"] != pid:
                raise HTTPError(400, "bad_parent", "that is not a comment on this paper")
            if p["parent_id"] is not None:          # one level deep: a reply to a reply joins its thread
                p = c.execute("SELECT id, paper_id, parent_id, deleted_at FROM comments WHERE id = ?", (p["parent_id"],)).fetchone()
            shown = p["deleted_at"] is None or c.execute(
                "SELECT 1 FROM comments WHERE parent_id = ? AND deleted_at IS NULL LIMIT 1", (p["id"],)).fetchone()
            if not shown:
                raise HTTPError(404, "gone", "that comment was deleted")
            parent = p["id"]
        cid = c.execute("INSERT INTO comments(paper_id, episode_id, parent_id, user_id, body, created_at) "
                        "VALUES (?, ?, ?, ?, ?, ?)", (pid, eid, parent, _uid(req), body, db.now())).lastrowid
    c = db.conn()
    _publish(c, pid, cid, "new")
    req.send_json(201, view(_one(c, cid)))


def _mine_or_404(c, cid: int):
    r = c.execute("SELECT id, paper_id, parent_id, user_id, deleted_at FROM comments WHERE id = ?", (cid,)).fetchone()
    if r is None or r["deleted_at"] is not None:
        raise HTTPError(404, "not_found", "no such comment")
    return r


def put_comment(req, cid):
    ensure_schema()
    _mutation(req)
    body = clean_body(req.json().get("body"))
    cid = int(cid)
    with db.transaction() as c:
        r = _mine_or_404(c, cid)
        if r["user_id"] != _uid(req):
            raise HTTPError(403, "not_yours", "only its writer can edit a comment")
        old = c.execute("SELECT body FROM comments WHERE id = ?", (cid,)).fetchone()["body"]
        if old != body:
            c.execute("UPDATE comments SET body = ?, edited_at = ? WHERE id = ?", (body, db.now(), cid))
    c = db.conn()
    if old != body:
        _publish(c, r["paper_id"], cid, "edit")
    req.send_json(200, view(_one(c, cid)))


def delete_comment(req, cid):
    ensure_schema()
    _mutation(req)
    cid = int(cid)
    with db.transaction() as c:
        r = _mine_or_404(c, cid)
        if r["user_id"] != _uid(req) and not _admin(req):
            raise HTTPError(403, "not_yours", "only its writer or an admin can delete a comment")
        c.execute("UPDATE comments SET body = '', deleted_at = ?, deleted_by = ? WHERE id = ?", (db.now(), _uid(req), cid))
    c = db.conn()
    _publish(c, r["paper_id"], cid, "delete")
    req.send_json(200, {"id": cid, "paper_id": r["paper_id"], "deleted": True, "count": _count(c, r["paper_id"])})


def get_counts(req):
    ensure_schema()
    rows = db.conn().execute(f"""SELECT paper_id, COUNT(*) FROM comments WHERE deleted_at IS NULL
                                 AND paper_id IN (SELECT e.paper_id FROM episodes e WHERE {LIVE})
                                 GROUP BY paper_id""").fetchall()
    req.send_json(200, {"counts": {r[0]: r[1] for r in rows}})


# ---------------------------------------------------------------- the board

def _paper_title(t) -> str:
    return t if isinstance(t, str) and t.strip() else "a paper"


def _item(kind, key, at, user, lead, title="", tail="", **more) -> dict:
    return dict({"kind": kind, "key": key, "at": at, "user": user, "lead": lead, "title": title, "tail": tail,
                 "text": lead + title + tail}, **more)


def _group(rows, key_of, pick=None) -> list:
    """Rows (oldest first, each with "t" in seconds and "uid") into runs: a row joins the open run
    of its person and key when it came within GROUP_S of that run's last row. key_of(row) gives
    the keys the row may belong to, in order of preference; pick chooses among them."""
    open_: dict = {}
    runs = []
    for r in rows:
        keys = key_of(r)
        k = None
        live = [x for x in keys if (r["uid"], x) in open_ and r["t"] - open_[(r["uid"], x)]["last"] <= GROUP_S]
        if live:
            k = max(live, key=lambda x: open_[(r["uid"], x)]["last"])     # the run touched most recently
        elif keys:
            k = keys[0]
        run = open_.get((r["uid"], k))
        if run is None or r["t"] - run["last"] > GROUP_S:
            run = {"key": k, "uid": r["uid"], "rows": [], "last": r["t"]}
            open_[(r["uid"], k)] = run
            runs.append(run)
        run["rows"].append(r)
        run["last"] = r["t"]
    return runs


def _users(c) -> dict:
    return {r["id"]: r["name"] or "someone" for r in c.execute("SELECT id, name FROM users")}


def _who(names, uid) -> dict:
    return {"id": uid, "name": names.get(uid, "someone")}


def _episode_items(c, names) -> list:
    """An upload is a paper's first live version; any later live version is a new version."""
    rows = c.execute(f"""SELECT e.id, e.paper_id, e.made_by, e.created_at, p.title FROM episodes e
                         JOIN papers p ON p.id = e.paper_id WHERE {LIVE}
                         ORDER BY e.created_at, e.id""").fetchall()
    seen, out = set(), []
    for r in rows:
        who = _who(names, r["made_by"])
        title = _paper_title(r["title"])
        if r["paper_id"] not in seen:
            seen.add(r["paper_id"])
            out.append(_item("upload", f"e:{r['id']}", r["created_at"], who, f"{who['name']} uploaded ", title,
                             paper_id=r["paper_id"]))
        else:
            out.append(_item("version", f"e:{r['id']}", r["created_at"], who, f"{who['name']} made a new version of ",
                             title, paper_id=r["paper_id"]))
    return out


def _comment_items(c, names) -> list:
    rows = c.execute(f"""SELECT c.id, c.paper_id, c.user_id, c.created_at, p.title FROM comments c
                         JOIN papers p ON p.id = c.paper_id
                         WHERE c.deleted_at IS NULL AND c.paper_id IN (SELECT e.paper_id FROM episodes e WHERE {LIVE})
                         ORDER BY c.id DESC LIMIT {SCAN_MAX}""").fetchall()
    rows = [dict(r, t=_t(r["created_at"]), uid=r["user_id"]) for r in reversed(rows)]
    out = []
    for run in _group(rows, lambda r: [r["paper_id"]]):
        first, last, n = run["rows"][0], run["rows"][-1], len(run["rows"])
        who = _who(names, run["uid"])
        out.append(_item("comment", f"c:{first['id']}", last["created_at"], who, f"{who['name']} commented on ",
                         _paper_title(last["title"]), f" ({n} comments)" if n > 1 else "",
                         paper_id=last["paper_id"], comment_id=last["id"], n=n))
    return out


def _graph_world():
    """Who is on which graph now (graph.py's snapshot), and every graph's name, deleted ones too."""
    from . import graph
    members, order = {}, []
    try:
        w = graph._world()
        members = {gid: set(m) for gid, m in w.members.items()}
        order = list(w.graphs)
    except Exception:           # graph.py's own trouble: the edits are still listed, under no graph
        log.exception("graph members for the board")
    return members, order


def _graph_items(c, names) -> list:
    rows = c.execute(f"""SELECT id, at, user_id, op, target, before, after FROM graph_log
                         WHERE actor = 'human' AND user_id IS NOT NULL
                         ORDER BY id DESC LIMIT {SCAN_MAX}""").fetchall()
    if not rows:
        return []
    members, order = _graph_world()
    rank = {gid: i for i, gid in enumerate(order)}
    gnames = {r["id"]: (r["name"], r["deleted_at"] is not None) for r in c.execute("SELECT id, name, deleted_at FROM graphs")}

    def graphs_of(r) -> list:
        op, t = r["op"], r["target"]
        if op.startswith("graph."):
            return [t.partition("/")[0]]
        if op.startswith("link."):
            s = db.loads(r["after"]) or db.loads(r["before"]) or {}
            got = [g for g, m in members.items() if s.get("src") in m and s.get("dst") in m]
        elif op == "paper.label":
            got = [g for g, m in members.items() if t in m]
        else:
            got = []
        return sorted(got, key=lambda g: rank.get(g, 1 << 30))

    rows = [dict(r, t=_t(r["at"]), uid=r["user_id"]) for r in reversed(rows)]
    runs = _group(rows, graphs_of)
    singles = [run["rows"][0]["id"] for run in runs if len(run["rows"]) == 1]
    summary = {}
    if singles:
        from . import graph
        try:
            summary = {e["id"]: e["summary"] for e in graph.log_entries(singles[-BOARD_LIMIT_MAX:])}
        except Exception:
            log.exception("graph log summaries for the board")
    out = []
    for run in runs:
        first, last, n = run["rows"][0], run["rows"][-1], len(run["rows"])
        who = _who(names, run["uid"])
        gid = run["key"]
        gname, gone = gnames.get(gid, (None, True)) if gid else (None, True)
        more = {"graph_id": None if gone else gid, "graph": gname, "n": n}
        if n == 1 and first["id"] in summary:
            s = summary[first["id"]]
            if gname and (first["op"].startswith("link.") or first["op"] == "paper.label"):
                it = _item("graph", f"g:{first['id']}", last["at"], who, f"{s} in the graph ", gname, **more)
            else:
                it = _item("graph", f"g:{first['id']}", last["at"], who, s, **more)
        elif gname:
            it = _item("graph", f"g:{first['id']}", last["at"], who, f"{who['name']} edited the graph ", gname,
                       f" ({n} changes)" if n > 1 else "", **more)
        else:
            it = _item("graph", f"g:{first['id']}", last["at"], who,
                       f"{who['name']} made {n} changes on the map" if n > 1 else f"{who['name']} changed the map", **more)
        out.append(it)
    return out


def _join_items(c, cfg, names) -> list:
    """Who joined when. With password sign-in a person is on the list before they ever come:
    they join when they first choose their own password (board_joins keeps that moment, as
    pw_set_at moves with every later change). Elsewhere a person's row is made when they first
    arrive, so users.created_at is it."""
    if getattr(cfg, "auth", "") == "password":
        c.execute("INSERT OR IGNORE INTO board_joins(user_id, at) "
                  "SELECT id, COALESCE(pw_set_at, last_login_at, created_at) FROM users WHERE pw_hash IS NOT NULL")
        rows = c.execute("SELECT user_id AS id, at FROM board_joins").fetchall()
    else:
        rows = c.execute("SELECT id, created_at AS at FROM users").fetchall()
    out = []
    for r in rows:
        who = _who(names, r["id"])
        out.append(_item("join", f"j:{r['id']}", r["at"], who, f"{who['name']} joined"))
    return out


def board(cfg, limit: int = 5) -> dict:
    """The newest `limit` items, and whether there are more."""
    ensure_schema()
    c = db.conn()
    names = _users(c)
    items = (_episode_items(c, names) + _comment_items(c, names) + _graph_items(c, names)
             + _join_items(c, cfg, names))
    items.sort(key=lambda it: (_t(it["at"]), it["key"]), reverse=True)
    avatars.decorate([it["user"] for it in items[:limit]], avatars.versions(c))     # each person's picture
    return {"items": items[:limit], "more": len(items) > limit}


def _notice(c):
    r = c.execute("""SELECT n.id, n.body, n.user_id, u.name, n.created_at FROM board_notices n
                     LEFT JOIN users u ON u.id = n.user_id
                     WHERE n.unpinned_at IS NULL ORDER BY n.id DESC LIMIT 1""").fetchone()
    if r is None:
        return None
    return {"id": r["id"], "body": r["body"],
            "user": {"id": r["user_id"], "name": r["name"] or "someone", "avatar": avatars.version_of(r["user_id"], c)},
            "created_at": r["created_at"]}


def _seen(c, uid):
    r = c.execute("SELECT at FROM board_seen WHERE user_id = ?", (uid,)).fetchone()
    return r["at"] if r else None


def get_board(req):
    ensure_schema()
    try:
        limit = max(1, min(BOARD_LIMIT_MAX, int(req.arg("limit") or 5)))
    except ValueError:
        raise HTTPError(400, "bad_arg", "limit is a number")
    out = board(req.cfg, limit)
    c = db.conn()
    out.update(notice=_notice(c), seen_at=_seen(c, _uid(req)), now=db.now())
    req.send_json(200, out)


def put_seen(req):
    """The page opened the board and showed items up to `at` (the newest one's time). Never ahead
    of the hub's clock, never back."""
    ensure_schema()
    _mutation(req)
    at = req.json().get("at")
    t = _t(at) if isinstance(at, str) else 0.0
    if not t:
        raise HTTPError(400, "bad_at", "at is the time of the newest item seen")
    at = _iso(min(t, time.time()))
    uid = _uid(req)
    with db.transaction() as c:
        old = _seen(c, uid)
        if old is None or _t(old) < _t(at):
            c.execute("INSERT INTO board_seen(user_id, at) VALUES (?, ?) ON CONFLICT(user_id) DO UPDATE SET at = excluded.at",
                      (uid, at))
        seen = _seen(c, uid)
    events.publish("board", {"why": "seen", "seen_at": seen}, users={uid})
    req.send_json(200, {"seen_at": seen})


def post_notice(req):
    ensure_schema()
    _mutation(req)
    body = clean_notice(req.json().get("body"))
    now = db.now()
    with db.transaction() as c:
        c.execute("UPDATE board_notices SET unpinned_at = ?, unpinned_by = ? WHERE unpinned_at IS NULL", (now, _uid(req)))
        c.execute("INSERT INTO board_notices(body, user_id, created_at) VALUES (?, ?, ?)", (body, _uid(req), now))
    n = _notice(db.conn())
    events.publish("board", {"why": "notice", "notice": n})
    req.send_json(201, n)


def delete_notice(req, nid):
    ensure_schema()
    _mutation(req)
    n = db.conn().execute("UPDATE board_notices SET unpinned_at = ?, unpinned_by = ? WHERE id = ? AND unpinned_at IS NULL",
                          (db.now(), _uid(req), int(nid))).rowcount
    if not n:
        raise HTTPError(404, "not_found", "that notice is not pinned")
    events.publish("board", {"why": "notice", "notice": _notice(db.conn())})
    req.send_json(200, {"ok": True})


ROUTES = [
    ("GET", rf"^/api/papers/{PID}/comments$", get_comments, "viewer"),
    ("POST", rf"^/api/papers/{PID}/comments$", post_comment, "viewer"),
    ("PUT", r"^/api/comments/(\d{1,15})$", put_comment, "viewer"),
    ("DELETE", r"^/api/comments/(\d{1,15})$", delete_comment, "viewer"),
    ("GET", r"^/api/comments/counts$", get_counts, "viewer"),
    ("GET", r"^/api/board$", get_board, "viewer"),
    ("PUT", r"^/api/board/seen$", put_seen, "viewer"),
    ("POST", r"^/api/board/notice$", post_notice, "admin"),
    ("DELETE", r"^/api/board/notice/(\d{1,15})$", delete_notice, "admin"),
]
