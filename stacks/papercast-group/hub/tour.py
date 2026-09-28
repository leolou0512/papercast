"""The first-sign-in tour (static/tour.js), per person: whether it was started, finished or
skipped, and when the hub first saw the person on the page. It is kept here, not in the browser,
so a tour finished or skipped on the laptop never comes back on the phone.

  GET /api/tour   {"state", "auto", "again", "again_until", "again_left_s", "first_seen", "now"}
  PUT /api/tour   {"state": "started" | "finished" | "skipped"}: the same answer

Someone new gets the tour by itself (`auto`) from the first time the hub sees them on the page
until they finish or skip it, and "Tour again" (`again`) for AGAIN_S after that first time. New:
their account began at most NEW_S before that first time; it begins when it is made, or, with
passwords, when they choose their own password at the first sign-in (an admin may add someone
days before they come). Anyone else was using the site before the tour existed: "old", neither.
A replay (Tour again) that starts is not written, so it never makes the tour start by itself
again; only its end is."""
from __future__ import annotations

import threading
import time
from datetime import datetime, timezone

from . import db
from .app import HTTPError

AGAIN_S = 3 * 86400
NEW_S = 2 * 86400
ENDS = ("finished", "skipped")

SCHEMA = """
CREATE TABLE IF NOT EXISTS tour (
  user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
  state TEXT NOT NULL,                  -- new | started | finished | skipped | old
  first_seen TEXT NOT NULL,             -- the first time the hub saw them on the page
  started_at TEXT,
  ended_at TEXT,
  updated_at TEXT NOT NULL
)
"""

_ready: set = set()
_ready_lock = threading.Lock()


def ensure_schema() -> None:
    """This part's table, once per database."""
    key = str(db._path)
    if key in _ready:
        return
    with _ready_lock:
        if key in _ready:
            return
        c = db.conn()
        for stmt in SCHEMA.split(";"):
            if stmt.strip():
                c.execute(stmt)
        if not c.in_transaction:
            _ready.add(key)


def start(cfg) -> None:
    ensure_schema()


def _now() -> float:
    """The hub's clock (tests move it on)."""
    return time.time()


def _iso(t: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


def _unix(iso) -> float | None:
    try:
        return datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
    except (TypeError, ValueError):
        return None


def _began(u) -> float:
    """When this account began: made, or its own password chosen (password sign-in), the later."""
    ts = [t for t in (_unix(u["created_at"]), _unix(u["pw_set_at"])) if t is not None]
    return max(ts) if ts else 0.0


def _row(c, uid: int):
    r = c.execute("SELECT * FROM tour WHERE user_id = ?", (uid,)).fetchone()
    if r is not None:
        return r
    u = c.execute("SELECT created_at, pw_set_at FROM users WHERE id = ?", (uid,)).fetchone()
    now = _now()
    state = "new" if u is not None and now - _began(u) <= NEW_S else "old"
    c.execute("INSERT OR IGNORE INTO tour(user_id, state, first_seen, updated_at) VALUES (?, ?, ?, ?)",
              (uid, state, _iso(now), _iso(now)))
    return c.execute("SELECT * FROM tour WHERE user_id = ?", (uid,)).fetchone()


def _answer(r) -> dict:
    now = _now()
    offered = r["state"] != "old"
    until = (_unix(r["first_seen"]) or 0.0) + AGAIN_S
    within = offered and now < until
    return {"state": r["state"], "auto": within and r["state"] in ("new", "started"), "again": within,
            "again_until": _iso(until) if offered else None,
            "again_left_s": max(0, int(until - now)) if within else 0,
            "first_seen": r["first_seen"], "now": _iso(now)}


def get_tour(req):
    ensure_schema()
    with db.transaction() as c:
        r = _row(c, req.user["id"])
    req.send_json(200, _answer(r))


def put_tour(req):
    ensure_schema()
    st = req.json().get("state")
    if st not in ("started",) + ENDS:
        raise HTTPError(400, "bad_state", "state is started, finished or skipped")
    at = _iso(_now())
    with db.transaction() as c:
        r = _row(c, req.user["id"])
        if r["state"] == "old" or (st == "started" and r["state"] in ENDS):
            pass                    # an ended tour stays ended: a replay never makes it start by itself again
        elif st == "started":
            c.execute("UPDATE tour SET state = 'started', started_at = COALESCE(started_at, ?), updated_at = ? WHERE user_id = ?",
                      (at, at, r["user_id"]))
        else:
            c.execute("UPDATE tour SET state = ?, started_at = COALESCE(started_at, ?), ended_at = ?, updated_at = ? WHERE user_id = ?",
                      (st, at, at, at, r["user_id"]))
        r = c.execute("SELECT * FROM tour WHERE user_id = ?", (r["user_id"],)).fetchone()
    req.send_json(200, _answer(r))


ROUTES = [
    ("GET", r"^/api/tour$", get_tour, "viewer"),
    ("PUT", r"^/api/tour$", put_tour, "viewer"),
]
