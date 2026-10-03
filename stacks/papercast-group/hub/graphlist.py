"""The graph list: the page's home column (DESIGN.md, Leo's decisions of 2026-10-03). Per person:
the graphs they subscribe to (their own toggle; nothing subscribes by itself, but making a graph
subscribes its maker), when they last opened each one (a subscribed graph's "3 new" is the papers
that joined it since), and the page's own choices, kept per account rather than in the browser,
which several people may share: the graph open last (where the page lands) and the sort.

  GET  /api/graphs                     (graph.py) every graph with this person's fields, "unfiled"
                                       (Not in any graph) and "ui"
  PUT  /api/graphs/<id>/subscription   {"subscribed": bool} -> the graph's item
  POST /api/graphs/<id>/opened         this person opened it (also "none"): -> {"opened_at", "ui"}
  GET  /api/ui-state, PUT /api/ui-state  {"last_graph"?: a graph's id | "none" | null, "sort"?: SORTS}

A graph's item (graph.py's, plus): subscribed, n (papers), unheard (papers this person has not
heard: neither ticked as listened nor any version finished), new (papers that joined since they
last opened it, or since they subscribed when they never did), updated_at (the last change to it,
or the newest paper that joined it), newest_at (when the newest paper joined), created_at,
created_by, can_edit, can_delete, can_link. The tables are db.py's (migration 7)."""
from __future__ import annotations

import bisect
import re
import threading

from . import db, events, graph
from .app import HTTPError

SORTS = ("az", "updated", "papers", "created", "mine", "unheard", "newest")
DEFAULT_SORT = "updated"
GID = re.compile(r"^(g_[a-z0-9]{4,32}|none)$")

_ready: set = set()
_ready_lock = threading.Lock()


def ensure_schema() -> None:
    """The tables (db.py's migration 7 makes them; here for a database it has not reached)."""
    key = str(db._path)
    if key in _ready:
        return
    with _ready_lock:
        if key in _ready:
            return
        c = db.conn()                       # (graphs.updated_at, graph_members.at: migration 7, and graph.ensure_schema)
        for stmt in db.GRAPH_LIST_TABLES:
            c.execute(stmt)
        if not c.in_transaction:
            _ready.add(key)


def start(cfg) -> None:
    ensure_schema()


# ---------------------------------------------------------------- one person's state

def subscribe(c, uid: int, gid: str, at: str | None = None) -> None:
    """Inside the caller's transaction (graph.py's h_create: the maker)."""
    ensure_schema()
    c.execute("INSERT OR IGNORE INTO graph_subs(user_id, graph_id, at) VALUES (?, ?, ?)", (uid, gid, at or db.now()))


def subscribed(uid: int, gid: str) -> bool:
    ensure_schema()
    return db.conn().execute("SELECT 1 FROM graph_subs WHERE user_id = ? AND graph_id = ?", (uid, gid)).fetchone() is not None


def ui_state(uid: int) -> dict:
    ensure_schema()
    r = db.conn().execute("SELECT last_graph, sort FROM ui_state WHERE user_id = ?", (uid,)).fetchone()
    sort = r["sort"] if r and r["sort"] in SORTS else DEFAULT_SORT
    return {"last_graph": r["last_graph"] if r else None, "sort": sort}


def _set_ui(c, uid: int, **kw) -> None:
    c.execute("INSERT INTO ui_state(user_id, updated_at) VALUES (?, ?) ON CONFLICT(user_id) DO NOTHING", (uid, db.now()))
    for k, v in kw.items():
        c.execute(f"UPDATE ui_state SET {k} = ?, updated_at = ? WHERE user_id = ?", (v, db.now(), uid))


def items(user) -> dict:
    """GET /api/graphs: every graph with this person's fields, oldest first (the page sorts),
    "Not in any graph" apart, and the page's own choices."""
    ensure_schema()
    w = graph._world()
    uid = user["id"]
    c = db.conn()
    subs = dict(c.execute("SELECT graph_id, at FROM graph_subs WHERE user_id = ?", (uid,)).fetchall())
    seen = dict(c.execute("SELECT graph_id, at FROM graph_seen WHERE user_id = ?", (uid,)).fetchall())
    done = graph.heard(uid)

    def mine(it, mem, gid):
        j = graph.joins(w, gid)
        base = seen.get(gid) or subs.get(gid)
        it.update(subscribed=gid in subs, unheard=len(mem - done), opened_at=seen.get(gid),
                  new=len(j) - bisect.bisect_right(j, base) if base else 0)
        return it
    out = [mine(graph._graph_item(w, g, user), w.members.get(gid, set()), gid) for gid, g in w.graphs.items()]
    un = mine(graph._unfiled_item(w, user), graph.unfiled(w), graph.UNFILED)
    un.update(subscribed=False, new=0)
    return {"graphs": out, "unfiled": un, "ui": ui_state(uid)}


# ---------------------------------------------------------------- routes

def _live_graph(c, gid: str):
    r = c.execute("SELECT id, name FROM graphs WHERE id = ? AND deleted_at IS NULL", (gid,)).fetchone()
    if r is None:
        raise HTTPError(404, "not_found", "no such graph")
    return r


def _item(user, gid: str) -> dict:
    return next((g for g in items(user)["graphs"] if g["id"] == gid), None)


def h_subscription(req, gid):
    """PUT /api/graphs/<id>/subscription {"subscribed": bool}: this person's own toggle."""
    ensure_schema()
    v = req.json().get("subscribed")
    if not isinstance(v, bool):
        raise HTTPError(400, "bad_subscribed", "subscribed is true or false")
    uid = req.user["id"]
    with db.transaction() as c:
        _live_graph(c, gid)
        if v:
            c.execute("INSERT OR IGNORE INTO graph_subs(user_id, graph_id, at) VALUES (?, ?, ?)", (uid, gid, db.now()))
        else:
            c.execute("DELETE FROM graph_subs WHERE user_id = ? AND graph_id = ?", (uid, gid))
    events.publish("mygraphs", {"why": "subscription", "graph_id": gid, "subscribed": v}, users={uid})   # their other tabs
    req.send_json(200, {"graph": _item(req.user, gid)})


def h_opened(req, gid):
    """POST /api/graphs/<id>/opened: this person opened the graph: its new papers are seen, and
    it is where the page lands next time."""
    ensure_schema()
    req.json()
    uid, at = req.user["id"], db.now()
    with db.transaction() as c:
        if gid != graph.UNFILED:
            _live_graph(c, gid)
            c.execute("INSERT INTO graph_seen(user_id, graph_id, at) VALUES (?, ?, ?) "
                      "ON CONFLICT(user_id, graph_id) DO UPDATE SET at = excluded.at", (uid, gid, at))
        _set_ui(c, uid, last_graph=gid)
    req.send_json(200, {"opened_at": at, "ui": ui_state(uid)})


def h_get_ui(req):
    req.send_json(200, ui_state(req.user["id"]))


def h_put_ui(req):
    """PUT /api/ui-state {"last_graph"?, "sort"?}: either or both."""
    ensure_schema()
    b = req.json()
    kw = {}
    if "sort" in b:
        if b["sort"] not in SORTS:
            raise HTTPError(400, "bad_sort", "sort is one of " + ", ".join(SORTS))
        kw["sort"] = b["sort"]
    if "last_graph" in b:
        v = b["last_graph"]
        if v is not None and not (isinstance(v, str) and GID.match(v)):
            raise HTTPError(400, "bad_graph", "last_graph is a graph's id, \"none\" or null")
        kw["last_graph"] = v
    if not kw:
        raise HTTPError(400, "nothing", "send last_graph or sort")
    with db.transaction() as c:
        _set_ui(c, req.user["id"], **kw)
    req.send_json(200, ui_state(req.user["id"]))


ROUTES = [
    ("PUT", r"^/api/graphs/([^/]+)/subscription$", h_subscription, "viewer"),
    ("POST", r"^/api/graphs/([^/]+)/opened$", h_opened, "viewer"),
    ("GET", r"^/api/ui-state$", h_get_ui, "viewer"),
    ("PUT", r"^/api/ui-state$", h_put_ui, "viewer"),
]
