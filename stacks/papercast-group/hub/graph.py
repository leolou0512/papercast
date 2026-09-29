"""Links, graphs, the edit log and revert (SPEC.md section 8). Owner: A5.

Links are global: src is the earlier paper, dst the paper built on it. A graph is a named set of
papers (its tag rule, plus papers added by hand, minus papers removed by hand) and shows the links
among its members. Every change is one row in graph_log with the state before and after, so a
change can be undone against the current state, and the undo undone.

Agents (the links an upload brings) only ever add: a link a person removed stays removed, and a
link that already exists (a person's or an agent's) is left as it is. A link whose other paper is
not in the library yet waits in pending_links until that paper arrives.

Locked graphs: only admins change them, their links included (a link between two members of a
locked graph); agents still add links and the tag rule still brings new papers in.

Links from uploads are automatic (as above) or, when an admin sets them to suggest only, kept as
suggestions: the same rules decide which (a pair a person removed, a later paper built on by an
earlier one, a loop: none), and a person accepts one (it becomes their link.add, so it can be
undone) or dismisses it (for good: that pair is never suggested again).

`papercast relink` sends the links found again for papers already in the library (relink): the
same rules as an upload's, and the agent may also regrade its own links or remove one that now
grades none; a link a person made or changed is never touched. Each change is a log row."""
from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import sqlite3
import threading
from collections import defaultdict
from contextlib import contextmanager
from pathlib import Path

from . import db, events
from .app import HTTPError

log = logging.getLogger("pcg.graph")

GRADES = ("e", "s", "w")
GRADE_WORDS = {"essential": "e", "strong": "s", "weak": "w"}
GRADE_NAMES = {"e": "essential", "s": "strong", "w": "weak"}
UNDO_WINDOW = 100           # revert looks among the newest 100 ops only
LOG_LIMIT_MAX = 500
NAME_MAX, TAG_MAX, TAGS_MAX, LABEL_MAX = 80, 60, 40, 40
TITLE_MATCH_MIN = 12        # a title names a paper only when normalised it has 12+ characters (build.py)
GRAPH_FIELD_OPS = ("graph.create", "graph.rename", "graph.delete", "graph.set_tags", "graph.lock")
MEMBER_OPS = ("graph.add_paper", "graph.remove_paper")
LAYOUT_OPS = {"link.add", "link.remove", "graph.create", "graph.delete", "graph.add_paper",
              "graph.remove_paper", "graph.set_tags"}          # the others never move a paper

# Leo's five topics and their tags (papercast-itest/lineage/build.py GROUPS)
SEED_GRAPHS = [
    ("Reinforcement learning", ["reinforcement learning", "policy gradient", "value-based learning",
        "offline learning", "game playing", "sparse rewards", "multi-agent learning", "online learning",
        "policy iteration", "imitation learning"]),
    ("Diffusion and generative models", ["diffusion", "generative models", "text-to-image",
        "fast sampling", "video generation", "image generation", "flow matching", "image editing",
        "discrete diffusion", "guidance", "audio synthesis", "3d generation"]),
    ("Materials and molecules", ["materials discovery", "interatomic potentials", "crystal generation",
        "crystal structures", "materials synthesis", "chemical synthesis", "graph neural networks", "symmetry",
        "protein design"]),
    ("Language models", ["language models"]),
    ("Robotics and agents", ["robotic manipulation", "sim-to-real", "vision language agents"]),
]

AGENT_MODES = ("auto", "suggest")

EXTRA_SCHEMA = """
CREATE TABLE IF NOT EXISTS graph_settings (   -- site-wide settings of the graphs: agent_links = auto | suggest
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  updated_by INTEGER REFERENCES users(id),
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS link_suggestions ( -- a link an upload found while links from uploads were suggestions only
  id INTEGER PRIMARY KEY,
  src TEXT NOT NULL REFERENCES papers(id),
  dst TEXT NOT NULL REFERENCES papers(id),
  grade TEXT NOT NULL CHECK (grade IN ('e', 's', 'w')),
  user_id INTEGER REFERENCES users(id),           -- the uploader
  created_at TEXT NOT NULL,
  state TEXT NOT NULL DEFAULT 'open' CHECK (state IN ('open', 'accepted', 'dismissed')),
  decided_by INTEGER REFERENCES users(id),
  decided_at TEXT,
  link_id INTEGER,
  UNIQUE (src, dst)                               -- one per pair, ever: a dismissed pair stays dismissed
);

CREATE TABLE IF NOT EXISTS pending_links (    -- an upload's link whose other paper is not here yet
  id INTEGER PRIMARY KEY,
  paper_id TEXT NOT NULL REFERENCES papers(id),   -- the uploaded paper
  episode_id TEXT,
  user_id INTEGER REFERENCES users(id),           -- the uploader
  direction TEXT NOT NULL CHECK (direction IN ('builds_on', 'built_on_by')),
  grade TEXT NOT NULL CHECK (grade IN ('e', 's', 'w')),
  source TEXT,
  arxiv_id TEXT,
  doi TEXT,
  title_norm TEXT,
  other TEXT NOT NULL,                            -- the link's `other` as uploaded (JSON)
  key TEXT NOT NULL,
  created_at TEXT NOT NULL,
  resolved_at TEXT,
  outcome TEXT,                                   -- added | exists | removed | order | cycle | self
  link_id INTEGER,
  UNIQUE (paper_id, direction, key)
);
CREATE INDEX IF NOT EXISTS pending_links_arxiv ON pending_links(arxiv_id) WHERE resolved_at IS NULL;
CREATE INDEX IF NOT EXISTS pending_links_doi ON pending_links(doi) WHERE resolved_at IS NULL;
CREATE INDEX IF NOT EXISTS pending_links_title ON pending_links(title_norm) WHERE resolved_at IS NULL;

CREATE TABLE IF NOT EXISTS layout_state (     -- one row per graph: which layout its `layout` rows are
  graph_id TEXT PRIMARY KEY REFERENCES graphs(id),
  rev INTEGER NOT NULL DEFAULT 0,
  sig TEXT,                                       -- members and links it was computed for
  mode TEXT,
  n INTEGER,
  total_length REAL,
  seconds REAL,
  kept TEXT,
  updated_at TEXT
);
CREATE INDEX IF NOT EXISTS links_dst ON links(dst);

CREATE TABLE IF NOT EXISTS graph_rev (        -- one row per graph: its revision, one up with every change to it
  graph_id TEXT PRIMARY KEY REFERENCES graphs(id),
  rev INTEGER NOT NULL DEFAULT 0,
  sig TEXT,                                       -- its members and links at this revision
  user_id INTEGER,                                -- who made the change (null: the agent's upload, or the library)
  actor TEXT,                                     -- human | agent | library (members that came or went by
  log_id INTEGER,                                 --   their tags or episodes, seen by a changed sig)
  at TEXT
);
"""


# ---------------------------------------------------------------- schema, transactions

_ready: set = set()
_ready_lock = threading.Lock()


def ensure_schema() -> None:
    """This part's tables and the papers.label column, once per database. Seeds Leo's five
    topic graphs the first time."""
    key = str(db._path)
    if key in _ready:
        return
    with _ready_lock:
        if key in _ready:
            return
        c = db.conn()
        for stmt in EXTRA_SCHEMA.split(";"):      # one by one: executescript would commit a caller's transaction
            if stmt.strip():
                c.execute(stmt)
        if "label" not in {r[1] for r in c.execute("PRAGMA table_info(papers)")}:
            try:
                c.execute("ALTER TABLE papers ADD COLUMN label TEXT")   # a short name; null: automatic
            except sqlite3.OperationalError:                           # added by another process meanwhile
                pass
        _seed()
        if c.in_transaction:     # a caller's transaction may still roll it back: check again next time
            return
        _ready.add(key)
    from . import layout
    layout.schedule(reset=False)       # after a restart: bring every graph's positions up to date


def _seed() -> None:
    with _tx() as c:
        if c.execute("SELECT 1 FROM meta WHERE key = 'graphs_seeded'").fetchone():
            return
        if not c.execute("SELECT 1 FROM graphs LIMIT 1").fetchone():
            at = db.now()
            for name, tags in SEED_GRAPHS:
                c.execute("INSERT INTO graphs(id, name, rule_tags, locked, created_by, created_at) "
                          "VALUES (?, ?, ?, 0, NULL, ?)", (db.new_id("g_", 10), name, db.dumps(tags), at))
        c.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('graphs_seeded', ?)", (db.now(),))


@contextmanager
def _tx():
    """A write transaction, or the caller's if one is already open (apply_agent_links may be
    called inside the upload's own transaction)."""
    c = db.conn()
    if c.in_transaction:
        yield c
    else:
        with db.transaction() as c2:
            yield c2


@contextmanager
def _reading():
    """One consistent snapshot for several reads (WAL), unless already inside a transaction."""
    c = db.conn()
    if c.in_transaction:
        yield c
    else:
        c.execute("BEGIN")
        try:
            yield c
        finally:
            c.execute("COMMIT")


# ---------------------------------------------------------------- labels and time order

_LEO_LABELS = None
_STOP = {"a", "an", "the", "of", "for", "and", "in", "on", "to", "with", "via", "by", "from", "is", "are",
         "at", "as", "or"}


def _leo_labels() -> dict:
    global _LEO_LABELS
    if _LEO_LABELS is None:
        try:
            _LEO_LABELS = json.loads(Path(__file__).with_name("graph_labels.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            _LEO_LABELS = {}
    return _LEO_LABELS


def auto_label(title: str, arxiv_id=None, doi=None, title_norm=None) -> str:
    """A short name for the map: Leo's curated one where the paper is his (graph_labels.json, from
    papercast-itest/lineage/labels.py), else build.py's rule: the title's prefix before a colon,
    else a parenthesised acronym, else its first three words."""
    L = _leo_labels()
    for kind, v in (("arxiv", _norm_arxiv(arxiv_id)), ("doi", _norm_doi(doi)),
                    ("title", title_norm or db.norm_title(title))):
        if v and v in L.get(kind, {}):
            return L[kind][v]
    t = " ".join((title or "").split())
    m = re.match(r"^([^:]{2,24}):", t)
    if m:
        return m.group(1).strip()
    m = re.search(r"\(([A-Z][A-Za-z0-9\-]{1,11})\)", t)
    if m:
        return m.group(1)
    w = t.split()[:3]
    while len(w) > 1 and w[-1].lower() in _STOP:
        w.pop()
    s = " ".join(w)
    return s if len(s) <= 28 else s[:27].rstrip() + "…"


_AX_NEW = re.compile(r"^(\d{2})(\d{2})\.\d{4,5}$")


def _okey(p: dict) -> tuple:
    """Time order as build.py: year, month (from the arXiv id; unknown: mid-year), arXiv id, title."""
    y = p.get("year") or 0
    ax = p.get("arxiv_id") or ""
    month = None
    m = _AX_NEW.match(ax)
    if m:
        ay, am = 2000 + int(m.group(1)), int(m.group(2))
        if ay == y:
            month = am
        elif ay == y + 1 and am == 1:
            month = 12        # submitted at the very end of the year, announced in January
    return (y, month if month else 6.5, ax or "9999", p.get("title") or "")


def _norm_arxiv(v):
    if not v or not isinstance(v, str):
        return None
    v = v.strip().lower()
    v = re.sub(r"^(https?://)?(www\.)?arxiv\.org/(abs|pdf)/", "", v)
    v = re.sub(r"^arxiv:", "", v)
    v = re.sub(r"\.pdf$", "", v)
    v = re.sub(r"v\d+$", "", v)
    return v or None


def _norm_doi(v):
    if not v or not isinstance(v, str):
        return None
    v = v.strip().lower()
    v = re.sub(r"^(https?://)?(dx\.)?doi\.org/", "", v)
    v = re.sub(r"^doi:", "", v)
    return v or None


# ---------------------------------------------------------------- the world: one snapshot, cached

class _World:
    """Everything a graph answer is made from, read in one snapshot; rebuilt only when the
    database's signature changes."""
    __slots__ = ("sig", "users", "papers", "graphs", "how", "members", "links", "pos", "lstate", "revs", "sugg")


_world_cache = None
_views: dict = {}

# Cheap aggregates that change whenever anything a graph shows changes, whoever wrote it (this
# module, another module, the nightly layout process).
_SIG_SQL = """SELECT
 (SELECT coalesce(max(id), 0) FROM graph_log),
 (SELECT count(*) || ':' || coalesce(max(rowid), 0) || ':' || total(length(tags)) || ':' || total(length(title))
         || ':' || total(year) || ':' || total(length(label)) FROM papers),
 (SELECT count(*) || ':' || coalesce(max(rowid), 0) || ':' || count(deleted_at) || ':' || total(state = 'ready')
         || ':' || total(state = 'rejected') || ':' || total(made_by) FROM episodes),
 (SELECT count(*) || ':' || coalesce(group_concat(id || '=' || name, ','), '') FROM users),
 (SELECT count(*) || ':' || total(rev) FROM layout_state),
 (SELECT count(*) || ':' || total(rev) FROM graph_rev),
 (SELECT count(*) || ':' || total(state = 'open') || ':' || coalesce(max(id), 0) FROM link_suggestions),
 (SELECT count(*) || ':' || total(length(name)) || ':' || total(locked) || ':' || count(deleted_at)
         || ':' || total(length(rule_tags)) FROM graphs),
 (SELECT count(*) || ':' || total(length(how)) FROM graph_members),
 (SELECT count(*) || ':' || total(state = 'active') || ':' || total(length(grade)) FROM links)"""


def _signature(c) -> str:
    return str(db._path) + "|" + "|".join(str(v) for v in c.execute(_SIG_SQL).fetchone())


def _world() -> _World:
    global _world_cache
    ensure_schema()
    in_tx = db.conn().in_transaction
    with _reading() as c:
        sig = _signature(c)
        w = _world_cache
        if w is not None and w.sig == sig:
            return w
        w = _World()
        w.sig = sig
        w.users = {r["id"]: r["name"] for r in c.execute("SELECT id, name FROM users")}
        papers = {}
        for r in c.execute("SELECT id, title, title_norm, year, arxiv_id, doi, tags, label FROM papers"):
            tags = db.loads(r["tags"], []) or []
            papers[r["id"]] = {
                "id": r["id"], "title": r["title"], "title_norm": r["title_norm"], "year": r["year"],
                "arxiv_id": r["arxiv_id"], "doi": r["doi"], "label_set": r["label"],
                "tags": {t.strip().lower() for t in tags if isinstance(t, str) and t.strip()},
                "made_by": [], "episodes": 0, "live": 0, "ready": False}
        for r in c.execute("SELECT paper_id, made_by, state, deleted_at FROM episodes ORDER BY created_at, rowid"):
            p = papers.get(r["paper_id"])
            if p is None:
                continue
            p["episodes"] += 1
            if r["deleted_at"] is None and r["state"] != "rejected":
                p["live"] += 1
                name = w.users.get(r["made_by"])
                if name and name not in p["made_by"]:
                    p["made_by"].append(name)
                if r["state"] == "ready":
                    p["ready"] = True
        for p in papers.values():
            # a paper whose every episode was rejected or deleted is not on any map
            p["visible"] = p["episodes"] == 0 or p["live"] > 0
            p["label"] = p["label_set"] or auto_label(p["title"], p["arxiv_id"], p["doi"], p["title_norm"])
            p["okey"] = _okey(p)
        w.papers = papers
        w.graphs = {}
        for r in c.execute("SELECT id, name, rule_tags, locked, created_by, created_at FROM graphs "
                           "WHERE deleted_at IS NULL ORDER BY created_at, rowid"):
            w.graphs[r["id"]] = {"id": r["id"], "name": r["name"], "tags": _clean_tags(db.loads(r["rule_tags"], [])),
                                 "locked": bool(r["locked"]), "created_by": r["created_by"],
                                 "created_at": r["created_at"]}
        w.how = defaultdict(dict)
        for r in c.execute("SELECT graph_id, paper_id, how FROM graph_members"):
            w.how[r[0]][r[1]] = r[2]
        w.members = {gid: _members(g, w.how.get(gid, {}), papers) for gid, g in w.graphs.items()}
        w.links = [dict(r) for r in c.execute(
            "SELECT l.id, l.src, l.dst, l.grade, l.origin, l.created_at, l.created_by, u.name AS by_name "
            "FROM links l LEFT JOIN users u ON u.id = l.created_by WHERE l.state = 'active' ORDER BY l.id")]
        w.pos = defaultdict(dict)
        for r in c.execute("SELECT graph_id, paper_id, x, y FROM layout"):
            w.pos[r[0]][r[1]] = (r[2], r[3])
        w.lstate = {r["graph_id"]: dict(r) for r in c.execute("SELECT * FROM layout_state")}
        w.revs = {r["graph_id"]: dict(r) for r in c.execute("SELECT * FROM graph_rev")}
        # the open suggestions still worth showing: no link of the pair either way (a pair a person
        # removed, or one linked since, is not suggested)
        seen_pairs = {(r[0], r[1]) for r in c.execute("SELECT src, dst FROM links")}
        back = {(l["dst"], l["src"]) for l in w.links}
        w.sugg = [dict(r) for r in c.execute(
            "SELECT s.id, s.src, s.dst, s.grade, s.user_id, s.created_at, u.name AS by_name FROM link_suggestions s "
            "LEFT JOIN users u ON u.id = s.user_id WHERE s.state = 'open' ORDER BY s.id")
            if (r["src"], r["dst"]) not in seen_pairs and (r["src"], r["dst"]) not in back]
    if not in_tx:          # never cache what an open (maybe rolled-back) write transaction sees
        _world_cache = w
    return w


def _members(g: dict, how: dict, papers: dict) -> set:
    rule = set(g["tags"])
    out = set()
    for pid, p in papers.items():
        h = how.get(pid)
        if h == "removed" or not p["visible"]:
            continue
        if h == "added" or (rule and not rule.isdisjoint(p["tags"])):
            out.add(pid)
    return out


def _glinks(w: _World, gid: str) -> list:
    m = w.members.get(gid, set())
    return [l for l in w.links if l["src"] in m and l["dst"] in m]


def layout_sig(ids, pairs) -> str:
    """What a layout depends on: the members and the links among them."""
    h = hashlib.sha1()
    for pid in sorted(ids):
        h.update(pid.encode() + b",")
    h.update(b"|")
    for s, d in sorted(pairs):
        h.update(f"{s}>{d},".encode())
    return h.hexdigest()


def layout_input(gid: str):
    """For layout.py: the graph's members in time order, the links among them as (src, dst), the
    stored positions, the signature of this state and the stored layout's state row."""
    w = _world()
    if gid not in w.graphs:
        return None
    mem = w.members[gid]
    ids = sorted(mem, key=lambda i: w.papers[i]["okey"])
    pairs = [(l["src"], l["dst"]) for l in _glinks(w, gid)]
    stored = w.pos.get(gid, {})
    return {"ids": ids, "links": pairs, "stored": {i: stored[i] for i in ids if i in stored},
            "sig": layout_sig(ids, pairs), "state": w.lstate.get(gid)}


def graph_ids() -> list:
    return list(_world().graphs)


# ---------------------------------------------------------------- revisions
# Every graph has a revision that goes up by one with every change touching it: its members, the
# links among them, its name, tags or lock, a label of one of its papers, its deletion, and the
# undo or redo of any of these. GET /api/graphs/<id> says which revision it shows; an edit that
# names the revision it was made on (base_rev) is refused with 409 `stale` when the graph moved
# on since, so two people never edit one graph on top of each other. Members can also come or go
# without an edit here (a paper that joins by its tags, a paper whose episodes were all deleted):
# the stored signature of members and links then no longer matches, and the next read or edit
# counts that as one more revision.

def _gsig(w: _World, gid: str) -> str:
    mem = w.members.get(gid, set())
    return layout_sig(mem, [(l["src"], l["dst"]) for l in w.links if l["src"] in mem and l["dst"] in mem])


def _rev_now(c, w: _World, gid: str) -> dict:
    """Graph gid's revision row now, inside a write transaction (one up when its members or links
    changed some other way since the last change here)."""
    sig = _gsig(w, gid)
    r = c.execute("SELECT * FROM graph_rev WHERE graph_id = ?", (gid,)).fetchone()
    if r is None:
        row = {"graph_id": gid, "rev": 1, "sig": sig, "user_id": None, "actor": None, "log_id": None, "at": db.now()}
        c.execute("INSERT INTO graph_rev(graph_id, rev, sig, at) VALUES (?, 1, ?, ?)", (gid, sig, row["at"]))
        return row
    row = dict(r)
    if row["sig"] != sig:
        row.update(rev=row["rev"] + 1, sig=sig, user_id=None, actor="library", log_id=None, at=db.now())
        c.execute("UPDATE graph_rev SET rev = ?, sig = ?, user_id = NULL, actor = 'library', log_id = NULL, at = ? "
                  "WHERE graph_id = ?", (row["rev"], sig, row["at"], gid))
    return row


def _bump(c, gids, user_id, actor, log_id=None) -> dict:
    """After a change, inside its transaction: each graph it touched goes up one revision.
    -> {graph id: new revision}"""
    gids = [g for g in dict.fromkeys(gids) if g]
    if not gids:
        return {}
    w = _world()                    # inside the transaction: the state after the change
    at, out = db.now(), {}
    for gid in gids:
        r = c.execute("SELECT rev FROM graph_rev WHERE graph_id = ?", (gid,)).fetchone()
        rev = (r[0] if r else 0) + 1
        c.execute("INSERT OR REPLACE INTO graph_rev(graph_id, rev, sig, user_id, actor, log_id, at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                  (gid, rev, _gsig(w, gid) if gid in w.graphs else None, user_id, actor, log_id, at))
        out[gid] = rev
    return out


def _base(req, b=None):
    """The graph and the revision of it the page saw, when it sent them (body or query):
    -> (graph id or None, revision or None)."""
    b = b if isinstance(b, dict) else {}
    rev = b["base_rev"] if "base_rev" in b else req.arg("base_rev")
    gid = b["graph_id"] if "graph_id" in b else req.arg("graph_id")
    if gid is not None and not isinstance(gid, str):
        raise HTTPError(400, "bad_base", "graph_id is a graph's id")
    if rev is None or rev == "":
        return gid, None
    try:
        if isinstance(rev, bool):
            raise ValueError
        rev = int(rev)
    except (TypeError, ValueError):
        raise HTTPError(400, "bad_base", "base_rev is the revision of the graph the page shows")
    return gid, rev


def _changed_by(w: _World, row) -> dict | None:
    """Who made a graph's latest revision, and when."""
    if not row or row.get("actor") is None:
        return None
    uid = row.get("user_id")
    return {"by": {"id": uid, "name": w.users.get(uid)} if uid is not None else None,
            "actor": row.get("actor"), "at": row.get("at"), "log_id": row.get("log_id")}


def _fresh(c, w: _World, gid, base) -> None:
    """Refuse (409 stale, nothing changed) an edit made on another revision of graph gid than the
    one it is at now. Without a base revision nothing is checked (the CLI, the tests, older pages)."""
    if base is None:
        return
    if not gid:
        raise HTTPError(400, "bad_base", "base_rev needs the graph_id it is a revision of")
    if gid not in w.graphs:
        raise HTTPError(409, "stale", "this graph was deleted since the page loaded it", graph_id=gid, rev=None,
                        base_rev=base, deleted=True)
    cur = _rev_now(c, w, gid)
    if cur["rev"] == base:
        return
    ch = _changed_by(w, cur) or {}
    actor = ch.get("actor")
    who = ((ch.get("by") or {}).get("name") or "someone") if actor == "human" else \
        "an upload" if actor == "agent" else "a change in the library"
    raise HTTPError(409, "stale", f"{who} changed this graph since the page loaded it", graph_id=gid, rev=cur["rev"],
                    base_rev=base, by=ch.get("by"), actor=actor, at=ch.get("at"))


# ---------------------------------------------------------------- the graph answer

def lineage(ids: list, links: list, papers: dict) -> dict:
    """build.py graph_info: roots (no parent in the graph, at least one child), start (the roots
    with the most descendants, at most 8, in time order), the listening path (parents always
    first, year by year; within a year, stay on the thread just heard) and descendant counts.
    `ids` must be in time order."""
    order = {pid: k for k, pid in enumerate(ids)}
    parents = {i: [] for i in ids}
    children = {i: [] for i in ids}
    for l in links:
        parents[l["dst"]].append(l["src"])
        children[l["src"]].append(l["dst"])
    year = {i: papers[i]["year"] or 0 for i in ids}
    indeg = {i: len(parents[i]) for i in ids}
    avail = {i for i in ids if indeg[i] == 0}
    left = set(ids)
    path = []
    while left:
        if not avail:               # a cycle (never made here, but old data might hold one): take the earliest
            avail.add(min(left, key=order.get))
        y0 = min(year[i] for i in avail)
        pool = {i for i in avail if year[i] == y0}
        pick = None
        for last in reversed(path[-12:]):
            opts = [c for c in children[last] if c in pool]
            if opts:
                pick = min(opts, key=order.get)
                break
        if pick is None:
            pick = min(pool, key=order.get)
        avail.discard(pick)
        left.discard(pick)
        path.append(pick)
        for c in children[pick]:
            if c in left:
                indeg[c] -= 1
                if indeg[c] <= 0:
                    avail.add(c)
    # descendants as bit sets, children before parents (the reverse of the path)
    bit = {i: 1 << k for k, i in enumerate(path)}
    desc = {}
    for i in reversed(path):
        d = 0
        for c in children[i]:
            d |= bit[c] | desc.get(c, 0)
        desc[i] = d & ~bit[i]
    ndesc = {i: bin(desc[i]).count("1") for i in ids}
    roots = [i for i in ids if not parents[i] and children[i]]
    start = sorted(sorted(roots, key=lambda r: (-ndesc[r], order[r]))[:8], key=order.get)
    return {"roots": roots, "start": start, "path": path,
            "descendants": {i: ndesc[i] for i in ids if ndesc[i]}}


def _provisional(ids: list, links: list, stored: dict):
    """Positions for papers the layout has not placed yet: at their placed neighbours' centroid,
    else on a spiral around the centre (d3's starting arrangement)."""
    pos = {i: tuple(stored[i]) for i in ids if i in stored}
    placed = set(pos)
    missing = [i for i in ids if i not in pos]
    if missing:
        nb = defaultdict(list)
        for l in links:
            nb[l["src"]].append(l["dst"])
            nb[l["dst"]].append(l["src"])
        for _ in range(4):
            for i in missing:
                if i in pos:
                    continue
                ps = [pos[j] for j in nb[i] if j in pos]
                if ps:
                    a = int(hashlib.sha1(i.encode()).hexdigest()[:6], 16) / 0xFFFFFF * 2 * math.pi
                    pos[i] = (sum(p[0] for p in ps) / len(ps) + 12 * math.cos(a),
                              sum(p[1] for p in ps) / len(ps) + 12 * math.sin(a))
        k = 0
        for i in missing:
            if i not in pos:
                r, a = 35 * math.sqrt(0.5 + k), k * 2.399963
                pos[i] = (r * math.cos(a), r * math.sin(a))
                k += 1
    return pos, placed


def _graph_item(w: _World, g: dict, user=None) -> dict:
    mem = w.members.get(g["id"], set())
    cb = g["created_by"]
    out = {"id": g["id"], "name": g["name"], "tags": g["tags"], "locked": g["locked"], "n": len(mem),
           "links": sum(1 for l in w.links if l["src"] in mem and l["dst"] in mem),
           "suggestions": sum(1 for x in w.sugg if x["src"] in mem and x["dst"] in mem),
           "created_by": {"id": cb, "name": w.users.get(cb)} if cb is not None else None,
           "created_at": g["created_at"],
           "rev": (w.revs.get(g["id"]) or {}).get("rev", 0), "changed": _changed_by(w, w.revs.get(g["id"]))}
    if user is not None:
        out.update(_perms(g, user))
    return out


def _perms(g: dict, user) -> dict:
    edit = _is_admin(user) or not g["locked"]
    return {"can_edit": edit, "can_delete": edit and (_is_admin(user) or g["created_by"] == user.get("id"))}


def _lazy_rev(gid: str, row, sig: str) -> None:
    """A read found graph gid's members or links changed without an edit here (or no revision
    yet): one more revision, unless another one was made meanwhile."""
    if db.conn().in_transaction:           # never inside a caller's transaction
        return
    with db.transaction() as c:
        if row is None:
            c.execute("INSERT OR IGNORE INTO graph_rev(graph_id, rev, sig, at) VALUES (?, 1, ?, ?)", (gid, sig, db.now()))
        else:
            c.execute("UPDATE graph_rev SET rev = rev + 1, sig = ?, user_id = NULL, actor = 'library', log_id = NULL, "
                      "at = ? WHERE graph_id = ? AND rev = ?", (sig, db.now(), gid, row["rev"]))


def _view(gid: str, _again: int = 0):
    """GET /api/graphs/<id> without the per-person parts, cached until the database changes. Its
    revision is the one of what it shows (one snapshot)."""
    from . import layout
    w = _world()
    hit = _views.get(gid)
    if hit is not None and hit[0] == w.sig:
        return hit[1]
    g = w.graphs.get(gid)
    if g is None:
        return None
    mem = w.members[gid]
    ids = sorted(mem, key=lambda i: w.papers[i]["okey"])
    links = _glinks(w, gid)
    sig = layout_sig(ids, [(l["src"], l["dst"]) for l in links])
    rrow = w.revs.get(gid)
    if (rrow is None or rrow.get("sig") != sig) and _again < 3 and not db.conn().in_transaction:
        _lazy_rev(gid, rrow, sig)
        return _view(gid, _again + 1)
    deg = defaultdict(int)
    for l in links:
        deg[l["src"]] += 1
        deg[l["dst"]] += 1
    pos, placed = _provisional(ids, links, w.pos.get(gid, {}))
    info = lineage(ids, links, w.papers)
    nodes = []
    for i in ids:
        p = w.papers[i]
        x, y = pos[i]
        nodes.append({"id": i, "label": p["label"], "title": p["title"], "year": p["year"],
                      "made_by": list(p["made_by"]), "x": round(x, 1), "y": round(y, 1), "deg": deg[i],
                      "placed": i in placed, "ready": p["ready"]})
    st = w.lstate.get(gid) or {}
    current = st.get("sig") == sig and len(placed) == len(ids)
    item = _graph_item(w, g)
    view = {"graph": item, "rev": item["rev"],
            "nodes": nodes,
            "links": [{"id": l["id"], "src": l["src"], "dst": l["dst"], "grade": l["grade"], "origin": l["origin"],
                       "created_at": l.get("created_at"),     # the map's link card: "added by the agent for Alice"
                       "by": {"id": l["created_by"], "name": l.get("by_name")} if l.get("created_by") is not None else None}
                      for l in links],
            "suggestions": [{"id": x["id"], "src": x["src"], "dst": x["dst"], "grade": x["grade"], "created_at": x["created_at"],
                             "by": {"id": x["user_id"], "name": x["by_name"]} if x["user_id"] is not None else None}
                            for x in w.sugg if x["src"] in mem and x["dst"] in mem],
            **info,
            "layout": {"rev": st.get("rev", 0), "updated_at": st.get("updated_at"), "current": current}}
    if w is _world_cache:
        if len(_views) > 256:
            _views.clear()
        _views[gid] = (w.sig, view)
    if not current:
        layout.schedule([gid], delay=1.0 if not placed else None, reset=False)
    return view


def graph_for(gid: str, user) -> dict | None:
    """The graph answer for one person: their Listened ticks and what they may do."""
    v = _view(gid)
    if v is None:
        return None
    listened = set()
    if user is not None:
        listened = {r[0] for r in db.conn().execute("SELECT paper_id FROM listened WHERE user_id = ?", (user["id"],))}
    out = dict(v)
    out["nodes"] = [dict(n, listened=n["id"] in listened) for n in v["nodes"]]
    g = _world().graphs.get(gid)
    if g is not None and user is not None:
        out["graph"] = dict(v["graph"], **_perms(g, user))      # the rest as of the view's snapshot
    return out


# ---------------------------------------------------------------- permissions and validation

def _is_admin(user) -> bool:
    return bool(user) and user.get("role") == "admin"


def _locked_error(name: str):
    return HTTPError(403, "locked", f"“{name}” is locked: only an admin can change it")


def _check_links_edit(w: _World, user, src: str, dst: str) -> None:
    if _is_admin(user):
        return
    for gid, g in w.graphs.items():
        if g["locked"] and src in w.members[gid] and dst in w.members[gid]:
            raise _locked_error(g["name"])


def _clean_tags(v) -> list:
    out = []
    for t in v if isinstance(v, list) else []:
        if isinstance(t, str):
            t = " ".join(t.split()).lower()
            if t and t not in out:
                out.append(t)
    return out


def _name(v) -> str:
    if not isinstance(v, str) or not " ".join(v.split()):
        raise HTTPError(400, "bad_name", "a graph needs a name")
    v = " ".join(v.split())
    if len(v) > NAME_MAX:
        raise HTTPError(400, "bad_name", f"a graph's name is at most {NAME_MAX} characters")
    return v


def _tags(v) -> list:
    if v is None:
        return []
    if not isinstance(v, list) or not all(isinstance(t, str) for t in v):
        raise HTTPError(400, "bad_tags", "tags is a list of strings")
    tags = _clean_tags(v)
    if len(tags) > TAGS_MAX or any(len(t) > TAG_MAX for t in tags):
        raise HTTPError(400, "bad_tags", f"at most {TAGS_MAX} tags of at most {TAG_MAX} characters")
    return tags


def _grade(v):
    if isinstance(v, str):
        v = v.strip().lower()
        v = GRADE_WORDS.get(v, v)
        if v in GRADES:
            return v
    return None


# ---------------------------------------------------------------- rows, snapshots, the log

def _paper_row(c, pid):
    return c.execute("SELECT * FROM papers WHERE id = ?", (pid,)).fetchone() if pid else None


def _graph_row(c, gid):
    return c.execute("SELECT * FROM graphs WHERE id = ?", (gid,)).fetchone()


def _link_row(c, lid):
    return c.execute("SELECT * FROM links WHERE id = ?", (lid,)).fetchone()


def _link_snap(r) -> dict:
    return {"id": r["id"], "src": r["src"], "dst": r["dst"], "grade": r["grade"], "origin": r["origin"],
            "state": r["state"]}


def _graph_snap(r) -> dict:
    return {"name": r["name"], "tags": _clean_tags(db.loads(r["rule_tags"], [])), "locked": bool(r["locked"]),
            "deleted": r["deleted_at"] is not None}


def _how(c, gid, pid):
    r = c.execute("SELECT how FROM graph_members WHERE graph_id = ? AND paper_id = ?", (gid, pid)).fetchone()
    return r[0] if r else None


def _set_how(c, gid, pid, how) -> None:
    if how is None:
        c.execute("DELETE FROM graph_members WHERE graph_id = ? AND paper_id = ?", (gid, pid))
    else:
        c.execute("INSERT OR REPLACE INTO graph_members(graph_id, paper_id, how) VALUES (?, ?, ?)", (gid, pid, how))


def _rule_matches(graph_row, paper_row) -> bool:
    rule = set(_clean_tags(db.loads(graph_row["rule_tags"], [])))
    tags = set(_clean_tags(db.loads(paper_row["tags"], [])))
    return bool(rule & tags)


def _log(c, user_id, actor, op, target, before, after, revert_of=None) -> int:
    cur = c.execute(
        "INSERT INTO graph_log(at, user_id, actor, op, target, before, after, revert_of) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (db.now(), user_id, actor, op, str(target), None if before is None else db.dumps(before),
         None if after is None else db.dumps(after), revert_of))
    return cur.lastrowid


def _adj(c) -> dict:
    adj = defaultdict(list)
    for s, d in c.execute("SELECT src, dst FROM links WHERE state = 'active'"):
        adj[s].append(d)
    return adj


def _reaches(adj, a, b, skip=None) -> bool:
    """Is b reachable from a along active links (optionally ignoring the link `skip` = (src, dst))?"""
    seen, stack = {a}, [a]
    while stack:
        n = stack.pop()
        if n == b:
            return True
        for m in adj.get(n, ()):
            if skip is not None and (n, m) == skip:
                continue
            if m not in seen:
                seen.add(m)
                stack.append(m)
    return False


def _depths(c, rows, roots=None) -> dict:
    """Revert-chain depth of each row: 0 a change, odd an undo, even (> 0) a redo. With `roots`
    (a dict), each row's change at the start of its chain goes there too."""
    known = {r["id"]: r for r in rows}
    depth: dict = {}
    root: dict = {} if roots is None else roots
    for r in rows:
        chain, rid = [], r["id"]
        while rid is not None and rid not in depth:
            x = known.get(rid)
            if x is None:
                x = c.execute("SELECT id, revert_of FROM graph_log WHERE id = ?", (rid,)).fetchone()
                if x is None:
                    break
                known[rid] = x
            chain.append(rid)
            rid = x["revert_of"]
        base = depth.get(rid, -1) if rid is not None else -1
        top = root.get(rid) if rid is not None else None
        for x in reversed(chain):
            base += 1
            depth[x] = base
            top = x if top is None else top
            root[x] = top
    return depth


def _gnames(c) -> dict:
    return {r[0]: r[1] for r in c.execute("SELECT id, name FROM graphs")}


def _describe(w: _World, gnames: dict, op: str, target: str, before, after) -> str:
    def lab(pid):
        p = w.papers.get(pid)
        return p["label"] if p else pid

    if op.startswith("link."):
        s = after or before or {}
        pair = f"{lab(s.get('src'))} → {lab(s.get('dst'))}"
        on_b = bool(before) and before.get("state") == "active"
        on_a = bool(after) and after.get("state") == "active"
        if on_a and not on_b:
            return ("restored " if before else "linked ") + pair + f" ({GRADE_NAMES.get(after.get('grade'), '?')})"
        if on_b and not on_a:
            return "removed " + pair
        if before and after and before.get("grade") != after.get("grade"):
            return (f"regraded {pair} from {GRADE_NAMES.get(before.get('grade'), '?')} "
                    f"to {GRADE_NAMES.get(after.get('grade'), '?')}")
        return "changed " + pair
    if op in MEMBER_OPS:
        gid, _, pid = target.partition("/")
        g = gnames.get(gid, gid)
        hb, ha = (before or {}).get("how"), (after or {}).get("how")
        if ha == "added" or (ha is None and hb == "removed"):
            return f"added {lab(pid)} to {g}"
        return f"removed {lab(pid)} from {g}"
    if op == "paper.label":
        la = (after or {}).get("label")
        p = w.papers.get(target)
        title = p["title"] if p else target
        return f"set the label of “{title}” to {la}" if la else f"reset the label of “{title}”"
    if op in GRAPH_FIELD_OPS:
        g = gnames.get(target, target)
        b, a = before or {}, after or {}
        if before is None:
            return f"created the graph {a.get('name', g)}"
        if "deleted" in a and a.get("deleted") != b.get("deleted"):
            return f"deleted the graph {g}" if a.get("deleted") else f"restored the graph {g}"
        if "name" in a and a.get("name") != b.get("name"):
            return f"renamed the graph {b.get('name')} to {a.get('name')}"
        if "tags" in a and a.get("tags") != b.get("tags"):
            return f"set the tags of {g} to " + (", ".join(a["tags"]) if a["tags"] else "none")
        if "locked" in a:
            return f"locked {g}" if a["locked"] else f"unlocked {g}"
        return f"changed {g}"
    return op


def _entries(c, rows, w: _World | None = None) -> list:
    rows = [r for r in rows if r is not None]
    if not rows:
        return []
    w = w or _world()
    gnames = _gnames(c)
    depth = _depths(c, rows)
    out = []
    for r in rows:
        before, after = db.loads(r["before"]), db.loads(r["after"])
        d = depth.get(r["id"], 0)
        kind = "change" if d == 0 else ("undo" if d % 2 else "redo")
        uid = r["user_id"]
        user = {"id": uid, "name": w.users.get(uid)} if uid is not None else None
        name = (user or {}).get("name") or "someone"
        text = _describe(w, gnames, r["op"], r["target"], before, after)
        who = f"the agent for {name}" if r["actor"] == "agent" else name
        summary = f"{who} {text}" + {"change": "", "undo": " (undo)", "redo": " (redo)"}[kind]
        out.append({"id": r["id"], "at": r["at"], "user": user, "actor": r["actor"], "op": r["op"],
                    "target": r["target"], "before": before, "after": after, "revert_of": r["revert_of"],
                    "reverted_by": r["reverted_by"], "kind": kind, "text": text, "summary": summary})
    return out


def log_entries(ids) -> list:
    ids = list(ids)
    if not ids:
        return []
    with _reading() as c:
        rows = c.execute(f"SELECT * FROM graph_log WHERE id IN ({','.join('?' * len(ids))}) ORDER BY id", ids).fetchall()
        return _entries(c, rows)


def _affected(w: _World, e: dict) -> set:
    op, t = e["op"], e["target"]
    if op.startswith("graph."):
        return {t.partition("/")[0]}
    if op.startswith("link."):
        s = e["after"] or e["before"] or {}
        return {gid for gid, m in w.members.items() if s.get("src") in m and s.get("dst") in m}
    if op == "paper.label":
        return {gid for gid, m in w.members.items() if t in m}
    return set()


def _after(ids) -> list:
    """After a commit: the log and graph events, and a layout for the graphs whose shape changed."""
    from . import layout
    ids = [i for i in ids if i is not None]
    if not ids:
        return []
    entries = log_entries(ids)
    w = _world()
    touched, relayout = {}, set()
    for e in entries:
        events.publish("log", e)
        for gid in _affected(w, e):
            touched.setdefault(gid, e["id"])
            if e["op"] in LAYOUT_OPS:
                relayout.add(gid)
    by_id = {e["id"]: e for e in entries}
    for gid, lid in touched.items():
        e = by_id[lid]
        events.publish("graph", {"id": gid, "change": "edit", "log_id": lid, "log_op": e["op"], "actor": e["actor"],
                                 "by": e["user"], "graph_rev": (w.revs.get(gid) or {}).get("rev"),
                                 "deleted": gid not in w.graphs})
    if relayout:
        layout.schedule(sorted(relayout))
    return entries


def _after_suggestions(sids) -> None:
    """After a commit: the graphs that show these suggestions (made, accepted, dismissed) hear of it."""
    sids = [i for i in (sids or []) if i is not None]
    if not sids:
        return
    c, w = db.conn(), _world()
    rows = c.execute(f"SELECT src, dst FROM link_suggestions WHERE id IN ({','.join('?' * len(sids))})", sids).fetchall()
    gids = sorted({g for r in rows for g in _link_graphs(w, r[0], r[1])})
    for gid in gids:
        events.publish("graph", {"id": gid, "change": "suggestions"})
    if not gids:
        events.publish("graph", {"change": "suggestions", "graphs": []})     # the count in the settings


# ---------------------------------------------------------------- the agent's links

def _find_paper(c, other: dict):
    """The paper an upload's link names, in the identity order of SPEC.md section 3."""
    if not isinstance(other, dict):
        return None
    pid = other.get("paper_id")
    if isinstance(pid, str) and pid:
        r = c.execute("SELECT id FROM papers WHERE id = ?", (pid,)).fetchone()
        if r:
            return r[0]
    ax = _norm_arxiv(other.get("arxiv_id"))
    if ax:
        r = c.execute("SELECT id FROM papers WHERE arxiv_id = ? ORDER BY created_at LIMIT 1", (ax,)).fetchone()
        if r:
            return r[0]
    doi = _norm_doi(other.get("doi"))
    if doi:
        r = c.execute("SELECT id FROM papers WHERE doi = ? OR lower(doi) = ? ORDER BY created_at LIMIT 1",
                      (doi, doi)).fetchone()
        if r:
            return r[0]
    tn = db.norm_title(other.get("title")) if isinstance(other.get("title"), str) else ""
    if len(tn) >= TITLE_MATCH_MIN:
        r = c.execute("SELECT id FROM papers WHERE title_norm = ? ORDER BY created_at LIMIT 1", (tn,)).fetchone()
        if r:
            return r[0]
    return None


def _pending_keys(other: dict):
    ax = _norm_arxiv(other.get("arxiv_id"))
    doi = _norm_doi(other.get("doi"))
    tn = db.norm_title(other.get("title")) if isinstance(other.get("title"), str) else ""
    tn = tn if len(tn) >= TITLE_MATCH_MIN else None
    return ax, doi, tn


def _link_graphs(w: _World, src, dst) -> list:
    """The graphs that show a link between these two papers."""
    return [gid for gid, m in w.members.items() if src in m and dst in m]


def _bump_links(c, lids, user_id, actor) -> dict:
    """The graphs showing these log rows' links go up one revision each."""
    if not lids:
        return {}
    w, gids = _world(), []
    for lid in lids:
        r = c.execute("SELECT l.src, l.dst FROM graph_log g JOIN links l ON l.id = CAST(g.target AS INTEGER) "
                      "WHERE g.id = ?", (lid,)).fetchone()
        if r:
            gids += _link_graphs(w, r[0], r[1])
    return _bump(c, gids, user_id, actor, lids[-1])


def _agent_mode(c) -> str:
    """Links from uploads: "auto" (added as links) or "suggest" (kept as suggestions)."""
    r = c.execute("SELECT value FROM graph_settings WHERE key = 'agent_links'").fetchone()
    return r[0] if r and r[0] in AGENT_MODES else "auto"


def _agent_add(c, adj, src, dst, grade, user_id, sugg=None):
    """One agent link, unless a person's decision or the time order says no; with `sugg` (a list:
    links from uploads are suggestions only) a suggestion instead, its id appended there.
    -> (outcome, log id)"""
    r = c.execute("SELECT * FROM links WHERE src = ? AND dst = ?", (src, dst)).fetchone()
    if r is not None:
        return ("removed" if r["state"] == "removed" else "exists"), None
    ys = c.execute("SELECT year FROM papers WHERE id = ?", (src,)).fetchone()
    yd = c.execute("SELECT year FROM papers WHERE id = ?", (dst,)).fetchone()
    if ys and yd and ys[0] and yd[0] and ys[0] > yd[0]:
        return "order", None             # the parent is the later paper (build.py drops these too)
    if _reaches(adj, dst, src):
        return "cycle", None
    if sugg is not None:
        old = c.execute("SELECT state FROM link_suggestions WHERE src = ? AND dst = ?", (src, dst)).fetchone()
        if old is not None:
            return ("dismissed" if old[0] == "dismissed" else "suggested already"), None
        sugg.append(c.execute("INSERT INTO link_suggestions(src, dst, grade, user_id, created_at) VALUES (?, ?, ?, ?, ?)",
                              (src, dst, grade, user_id, db.now())).lastrowid)
        return "suggested", None
    at = db.now()
    cur = c.execute("INSERT INTO links(src, dst, grade, origin, state, created_by, created_at, updated_at) "
                    "VALUES (?, ?, ?, 'agent', 'active', ?, ?, ?)", (src, dst, grade, user_id, at, at))
    adj[src].append(dst)
    snap = _link_snap(_link_row(c, cur.lastrowid))
    return "added", _log(c, user_id, "agent", "link.add", cur.lastrowid, None, snap)


def _resolve_for(c, adj, paper, sugg=None) -> list:
    """Pending links that name this paper: add them now (or suggest them, with `sugg`). -> log ids."""
    ax, doi, tn = _norm_arxiv(paper["arxiv_id"]), _norm_doi(paper["doi"]), paper["title_norm"]
    tn = tn if tn and len(tn) >= TITLE_MATCH_MIN else None
    if not (ax or doi or tn):
        return []
    rows = c.execute(
        "SELECT * FROM pending_links WHERE resolved_at IS NULL AND ((arxiv_id IS NOT NULL AND arxiv_id = ?) "
        "OR (doi IS NOT NULL AND doi = ?) OR (title_norm IS NOT NULL AND title_norm = ?)) ORDER BY id",
        (ax, doi, tn)).fetchall()
    ids = []
    for r in rows:
        if r["paper_id"] == paper["id"]:
            outcome, lid = "self", None
        elif r["direction"] == "builds_on":            # the uploaded paper builds on this one
            outcome, lid = _agent_add(c, adj, paper["id"], r["paper_id"], r["grade"], r["user_id"], sugg)
        else:
            outcome, lid = _agent_add(c, adj, r["paper_id"], paper["id"], r["grade"], r["user_id"], sugg)
        link = None
        if lid is not None:
            link = int(c.execute("SELECT target FROM graph_log WHERE id = ?", (lid,)).fetchone()[0])
            ids.append(lid)
        c.execute("UPDATE pending_links SET resolved_at = ?, outcome = ?, link_id = ? WHERE id = ?",
                  (db.now(), outcome, link, r["id"]))
    return ids


def apply_agent_links(episode, paper_id=None, user_id=None, links=None) -> dict:
    """The links an upload brings (the bundle's `links`, SPEC.md section 5), added as the agent:
    one log row per link, actor 'agent', user_id the uploader. Called by contrib after the
    checks pass, as apply_agent_links(episode_id, paper_id, user_id, links) or
    apply_agent_links(episode_row, links). Also adds the earlier uploads' pending links that
    name this paper. Never re-adds a link a person removed and never changes an existing link.

    While links from uploads are suggestions only, the links it would add are suggested instead.

    -> {"added", "suggested", "pending", "resolved", "skipped": [{"index", "reason"}], "log_ids"}"""
    ensure_schema()
    if links is None and isinstance(paper_id, (list, tuple)):
        links, paper_id = paper_id, None
    episode_id = episode
    if isinstance(episode, (dict, sqlite3.Row)):
        ep = dict(episode)
        episode_id = ep.get("id")
        paper_id = paper_id or ep.get("paper_id")
        user_id = user_id if user_id is not None else ep.get("made_by")
    if (paper_id is None or user_id is None) and episode_id:
        r = db.conn().execute("SELECT paper_id, made_by FROM episodes WHERE id = ?", (episode_id,)).fetchone()
        if r:
            paper_id = paper_id or r["paper_id"]
            user_id = user_id if user_id is not None else r["made_by"]
    if not paper_id:
        raise ValueError("apply_agent_links: no paper for this episode")
    out = {"added": 0, "suggested": 0, "pending": 0, "resolved": 0, "skipped": [], "log_ids": []}
    ids = []
    with _tx() as c:
        me = _paper_row(c, paper_id)
        if me is None:
            raise ValueError(f"apply_agent_links: no paper {paper_id}")
        adj = _adj(c)
        sugg = [] if _agent_mode(c) == "suggest" else None
        got = _resolve_for(c, adj, me, sugg)
        out["resolved"] = len(got) + len(sugg or [])
        ids += got
        for k, item in enumerate(links or []):
            if not isinstance(item, dict) or not isinstance(item.get("other"), dict):
                out["skipped"].append({"index": k, "reason": "not a link"})
                continue
            direction, grade = item.get("direction"), _grade(item.get("grade"))
            if direction not in ("builds_on", "built_on_by") or grade is None:
                out["skipped"].append({"index": k, "reason": "bad direction or grade"})
                continue
            other = item["other"]
            oid = _find_paper(c, other)
            if oid is None:
                ax, doi, tn = _pending_keys(other)
                if not (ax or doi or tn):
                    out["skipped"].append({"index": k, "reason": "unknown paper"})
                    continue
                cur = c.execute(
                    "INSERT OR IGNORE INTO pending_links(paper_id, episode_id, user_id, direction, grade, source, "
                    "arxiv_id, doi, title_norm, other, key, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (paper_id, episode_id, user_id, direction, grade, item.get("source"), ax, doi, tn,
                     db.dumps(other), f"a:{ax or ''}|d:{doi or ''}|t:{tn or ''}", db.now()))
                if cur.rowcount:
                    out["pending"] += 1
                else:
                    out["skipped"].append({"index": k, "reason": "already pending"})
                continue
            if oid == paper_id:
                out["skipped"].append({"index": k, "reason": "self"})
                continue
            src, dst = (oid, paper_id) if direction == "builds_on" else (paper_id, oid)
            n0 = len(sugg or [])
            outcome, lid = _agent_add(c, adj, src, dst, grade, user_id, sugg)
            if lid is not None:
                out["added"] += 1
                ids.append(lid)
            elif sugg is not None and len(sugg) > n0:
                out["suggested"] += 1
            else:
                out["skipped"].append({"index": k, "reason": outcome})
        _bump_links(c, ids, user_id, "agent")
    out["log_ids"] = ids
    _after(ids)
    _after_suggestions(sugg)
    from . import layout
    layout.schedule(reset=False)       # a new paper may have joined graphs by its tags
    return out


def resolve_pending(paper_id=None) -> int:
    """Add the pending links naming this paper (or, without one, every paper that has any).
    The upload path calls it through apply_agent_links; call it after creating a paper some
    other way (an import). -> how many links were added."""
    ensure_schema()
    ids = []
    with _tx() as c:
        if paper_id:
            rows = [r for r in [_paper_row(c, paper_id)] if r is not None]
        else:
            rows = c.execute(
                "SELECT DISTINCT p.* FROM papers p JOIN pending_links q ON q.resolved_at IS NULL AND "
                "((q.arxiv_id IS NOT NULL AND q.arxiv_id = p.arxiv_id) OR (q.doi IS NOT NULL AND q.doi = lower(p.doi)) "
                "OR (q.title_norm IS NOT NULL AND q.title_norm = p.title_norm))").fetchall()
        adj = _adj(c)
        sugg = [] if _agent_mode(c) == "suggest" else None
        for r in rows:
            ids += _resolve_for(c, adj, r, sugg)
        if ids:
            u = c.execute("SELECT user_id FROM graph_log WHERE id = ?", (ids[-1],)).fetchone()
            _bump_links(c, ids, u[0] if u else None, "agent")
    _after(ids)
    _after_suggestions(sugg)
    return len(ids) + len(sugg or [])


on_paper_created = resolve_pending


# ---------------------------------------------------------------- relink: the library's links found again

RELINK_MAX = 2000           # links in one relink request


class _DryRun(Exception):
    """Raised inside a relink's transaction so that a dry run keeps nothing."""


def _person_links(c) -> set:
    """Ids of the links a person has had a hand in: made (origin human, an accepted suggestion
    included) or changed in any way, an undo or redo of an agent's change included."""
    out = {r[0] for r in c.execute("SELECT id FROM links WHERE origin = 'human'")}
    for (t,) in c.execute("SELECT DISTINCT target FROM graph_log WHERE actor = 'human' AND op LIKE 'link.%'"):
        if str(t).isdigit():
            out.add(int(t))
    return out


def _relink_grade(v):
    if isinstance(v, str) and v.strip().lower() == "none":
        return "none"
    return _grade(v)


def relink_state(since=None) -> dict:
    """What `papercast relink` needs from the graph side (GET /api/cli/relink): every link (active
    or removed) with `person` (a person made or changed it: the agent leaves it alone), every
    suggestion, the setting for links from uploads, each paper's visibility (on a map at all), the
    graphs with their counts and whether their layout is up to date, and the newest log row
    (with `since`: the log rows after it, counted by op and actor)."""
    ensure_schema()
    w = _world()
    with _reading() as c:
        person = _person_links(c)
        links = [{"id": r["id"], "src": r["src"], "dst": r["dst"], "grade": r["grade"], "origin": r["origin"],
                  "state": r["state"], "person": r["id"] in person, "created_by": r["created_by"]}
                 for r in c.execute("SELECT * FROM links ORDER BY id")]
        sugg = [dict(r) for r in c.execute("SELECT src, dst, grade, state FROM link_suggestions ORDER BY id")]
        mode = _agent_mode(c)
        log_max = c.execute("SELECT coalesce(max(id), 0) FROM graph_log").fetchone()[0]
        counts = None
        if since is not None:
            counts = {f"{r[0]} {r[1]}": r[2] for r in c.execute(
                "SELECT op, actor, count(*) FROM graph_log WHERE id > ? GROUP BY op, actor ORDER BY op, actor", (since,))}
    graphs = []
    for gid, g in w.graphs.items():
        mem = w.members.get(gid, set())
        st = w.lstate.get(gid) or {}
        graphs.append({"id": gid, "name": g["name"], "locked": g["locked"], "n": len(mem),
                       "links": sum(1 for l in w.links if l["src"] in mem and l["dst"] in mem),
                       "suggestions": sum(1 for x in w.sugg if x["src"] in mem and x["dst"] in mem),
                       "layout": {"rev": st.get("rev", 0), "updated_at": st.get("updated_at"),
                                  "current": bool(st) and st.get("sig") == _gsig(w, gid)}})
    out = {"agent_links": mode, "links": links, "suggestions": sugg, "graphs": graphs,
           "visible": {pid: p["visible"] for pid, p in w.papers.items()}, "log_max": log_max}
    if counts is not None:
        out["log_since"] = {"since": since, "rows": counts}
    return out


def relink(paper_id, user, links, dry_run=False) -> dict:
    """`papercast relink` for one paper already in the library (POST /api/cli/papers/<id>/links):
    the links its two-way candidate filter found and haiku graded again, applied as the agent
    acting for `user`. Items as an upload's links, with grade e/s/w or "none" and `other` a
    library paper_id. The rules:
      - no link yet: added as apply_agent_links adds one (or, while links from uploads are
        suggestions only, suggested): never against the time order or into a loop, never a pair
        a person dismissed as a suggestion (in either setting), and never a pair a person
        removed, either way round; "none" adds nothing;
      - a link a person made or changed in any way (_person_links) is never touched;
      - a link the agent made: regraded when the grade differs, removed when it grades "none";
        neither while links from uploads are suggestions only (they wait for a person), and in
        a locked graph only for an admin;
      - a removed link stays removed (a person brings it back by hand).
    Every change is one graph_log row, actor 'agent', user_id the caller: the map's History
    lists each and its Undo reverts it (a person's undo then makes it theirs, so a later relink
    leaves it). dry_run: the same decisions in a transaction that is rolled back.
    -> {"paper_id", "mode", "dry_run", "changes": [{"index", "op": "add" | "suggest" | "regrade" |
    "remove", "src", "dst", "grade", "was"?, "link_id"?}], "skipped": [{"index", "src"?, "dst"?,
    "reason"}], "unchanged", "log_ids"}"""
    ensure_schema()
    if not isinstance(links, list) or len(links) > RELINK_MAX:
        raise HTTPError(400, "bad_links", f"links is a list of at most {RELINK_MAX}")
    uid = user["id"]
    admin = _is_admin(user)
    out = {"paper_id": paper_id, "mode": None, "dry_run": bool(dry_run), "changes": [], "skipped": [],
           "unchanged": 0, "log_ids": []}
    ids, sugg = [], None
    try:
        with db.transaction() as c:
            if _paper_row(c, paper_id) is None:
                raise HTTPError(404, "no_such_paper", f"no paper {paper_id}")
            mode = out["mode"] = _agent_mode(c)
            sugg = [] if mode == "suggest" else None
            adj = _adj(c)
            person = _person_links(c)
            w = _world()
            todo, seen = [], set()

            def skip(k, reason, src=None, dst=None):
                out["skipped"].append({"index": k, **({"src": src, "dst": dst} if src else {}), "reason": reason})

            for k, item in enumerate(links):
                if not isinstance(item, dict) or not isinstance(item.get("other"), dict):
                    skip(k, "not a link")
                    continue
                direction, grade = item.get("direction"), _relink_grade(item.get("grade"))
                if direction not in ("builds_on", "built_on_by") or grade is None:
                    skip(k, "bad direction or grade")
                    continue
                oid = item["other"].get("paper_id")
                if not isinstance(oid, str) or _paper_row(c, oid) is None:
                    skip(k, "unknown paper")
                    continue
                if oid == paper_id:
                    skip(k, "self")
                    continue
                src, dst = (oid, paper_id) if direction == "builds_on" else (paper_id, oid)
                if (src, dst) in seen:
                    skip(k, "twice", src, dst)
                    continue
                seen.add((src, dst))
                todo.append((k, src, dst, grade, c.execute("SELECT * FROM links WHERE src = ? AND dst = ?",
                                                           (src, dst)).fetchone()))
            # the links there are first (a removal may clear the way for a new link), then new ones
            for k, src, dst, grade, row in [t for t in todo if t[4] is not None]:
                if row["state"] == "removed":
                    skip(k, "removed by a person" if row["id"] in person else "removed", src, dst)
                    continue
                if row["id"] in person:
                    skip(k, "a person's link", src, dst)
                    continue
                if grade == row["grade"]:
                    out["unchanged"] += 1
                    continue
                if sugg is not None:
                    skip(k, "suggest only", src, dst)
                    continue
                if not admin:
                    try:
                        _check_links_edit(w, user, src, dst)
                    except HTTPError:
                        skip(k, "locked", src, dst)
                        continue
                before = _link_snap(row)
                if grade == "none":
                    c.execute("UPDATE links SET state = 'removed', updated_at = ? WHERE id = ?", (db.now(), row["id"]))
                    op, what = "link.remove", "remove"
                    if dst in adj.get(src, []):
                        adj[src].remove(dst)
                else:
                    c.execute("UPDATE links SET grade = ?, updated_at = ? WHERE id = ?", (grade, db.now(), row["id"]))
                    op, what = "link.grade", "regrade"
                after = _link_snap(_link_row(c, row["id"]))
                ids.append(_log(c, uid, "agent", op, row["id"], before, after))
                out["changes"].append({"index": k, "op": what, "src": src, "dst": dst, "grade": grade,
                                       "was": row["grade"], "link_id": row["id"]})
            for k, src, dst, grade, _ in [t for t in todo if t[4] is None]:
                if grade == "none":
                    out["unchanged"] += 1
                    continue
                back = c.execute("SELECT * FROM links WHERE src = ? AND dst = ?", (dst, src)).fetchone()
                if back is not None and back["state"] == "removed" and back["id"] in person:
                    skip(k, "removed by a person (the other way)", src, dst)
                    continue
                old = c.execute("SELECT state FROM link_suggestions WHERE src = ? AND dst = ?", (src, dst)).fetchone()
                if old is not None and old[0] == "dismissed":        # a person's no, whatever the setting now
                    skip(k, "dismissed by a person", src, dst)
                    continue
                n0 = len(sugg or [])
                outcome, lid = _agent_add(c, adj, src, dst, grade, uid, sugg)
                if lid is not None:
                    ids.append(lid)
                    link_id = int(c.execute("SELECT target FROM graph_log WHERE id = ?", (lid,)).fetchone()[0])
                    out["changes"].append({"index": k, "op": "add", "src": src, "dst": dst, "grade": grade,
                                           **({} if dry_run else {"link_id": link_id})})
                elif sugg is not None and len(sugg) > n0:
                    out["changes"].append({"index": k, "op": "suggest", "src": src, "dst": dst, "grade": grade})
                else:
                    skip(k, outcome, src, dst)
            if dry_run:
                raise _DryRun
            _bump_links(c, ids, uid, "agent")
    except _DryRun:
        return out
    out["log_ids"] = ids
    _after(ids)
    _after_suggestions(sugg)
    return out


# ---------------------------------------------------------------- revert

def _candidates(c, user) -> dict:
    """Among the newest 100 ops: per scope, the change to undo (the newest un-reverted change or
    redo), the undo to redo, and the newest un-reverted row of any kind. `mine` is the person's
    own edits (not the agent's links from their uploads). As in an editor, a change made after an
    undo leaves nothing to redo there, also when that change was undone and redone since; redoing
    older undos does not (undo, undo, then redo, redo walks forward again): an undo can be redone
    while no change in effect (un-reverted, or redone) in scope is newer than it."""
    rows = c.execute("SELECT * FROM graph_log ORDER BY id DESC LIMIT ?", (UNDO_WINDOW,)).fetchall()
    roots: dict = {}
    depth = _depths(c, rows, roots)
    res = {s: {"undo": None, "redo": None, "latest": None, "newest": 0} for s in ("mine", "any")}
    for r in rows:
        if r["reverted_by"] is not None:
            continue
        d = depth.get(r["id"], 0)
        kind = "redo" if d % 2 else "undo"
        for scope in ("mine", "any"):
            if scope == "mine" and not (user is not None and r["user_id"] == user["id"] and r["actor"] == "human"):
                continue
            slot = res[scope]
            if slot["latest"] is None:
                slot["latest"] = r
            if kind == "redo":
                if slot["redo"] is None and slot["newest"] < r["id"]:
                    slot["redo"] = r
            else:
                if slot["undo"] is None:
                    slot["undo"] = r
                slot["newest"] = max(slot["newest"], roots.get(r["id"], r["id"]))   # the change it has in effect
    return res


def _same(cur, expected) -> bool:
    if expected is None:
        return cur is None
    return cur is not None and all(cur.get(k) == v for k, v in expected.items())


def _conflict(r, expected, current, why="it changed since"):
    return HTTPError(409, "conflict", f"cannot undo #{r['id']}: {why}",
                     detail={"op": r["id"], "expected": expected, "current": current})


def _revert_row(c, user, r) -> int:
    """Apply the inverse of log row r against the current state, log it. -> the new row's id.
    Raises (and so changes nothing) when the thing changed since r."""
    op, target = r["op"], r["target"]
    before, after = db.loads(r["before"]), db.loads(r["after"])
    admin, uid = _is_admin(user), user["id"]
    if op.startswith("link."):
        row = _link_row(c, int(target))
        if row is None:
            raise _conflict(r, after, None, "the link is gone")
        cur = _link_snap(row)
        _check_links_edit(_world(), user, cur["src"], cur["dst"])
        if not _same(cur, after):
            raise _conflict(r, after, cur)
        new = dict(before) if before else dict(cur, state="removed")
        if new["state"] == "active" and cur["state"] != "active" and _reaches(_adj(c), cur["dst"], cur["src"]):
            raise _conflict(r, after, cur, "it would make a cycle")
        c.execute("UPDATE links SET grade = ?, origin = ?, state = ?, updated_at = ? WHERE id = ?",
                  (new["grade"], new["origin"], new["state"], db.now(), row["id"]))
        cur_cmp, new_snap = cur, _link_snap(_link_row(c, row["id"]))
    elif op in GRAPH_FIELD_OPS:
        row = _graph_row(c, target)
        if row is None:
            raise _conflict(r, after, None, "the graph is gone")
        g = _graph_snap(row)
        if op == "graph.lock" and not admin:
            raise HTTPError(403, "forbidden", "only an admin can lock or unlock a graph")
        if g["locked"] and not admin:
            raise _locked_error(g["name"])
        keys = list((after or before).keys())
        cur = {k: g[k] for k in keys}
        if g["deleted"] and "deleted" not in keys:
            raise _conflict(r, after, dict(cur, deleted=True), "the graph was deleted")
        if not _same(cur, after):
            raise _conflict(r, after, cur)
        new = dict(before) if before else dict(cur, deleted=True)
        if new.get("deleted") and not g["deleted"] and not (admin or row["created_by"] == uid):
            raise HTTPError(403, "forbidden", "only the graph's maker or an admin can delete it")
        sets, vals = [], []
        for k, v in new.items():
            if k == "name":
                sets.append("name = ?"); vals.append(v)
            elif k == "tags":
                sets.append("rule_tags = ?"); vals.append(db.dumps(v))
            elif k == "locked":
                sets.append("locked = ?"); vals.append(1 if v else 0)
            elif k == "deleted":
                sets.append("deleted_at = ?"); vals.append(db.now() if v else None)
        if sets:
            c.execute(f"UPDATE graphs SET {', '.join(sets)} WHERE id = ?", (*vals, target))
        g2 = _graph_snap(_graph_row(c, target))
        cur_cmp, new_snap = {k: g[k] for k in new}, {k: g2[k] for k in new}
    elif op in MEMBER_OPS:
        gid, _, pid = target.partition("/")
        row = _graph_row(c, gid)
        if row is None or row["deleted_at"] is not None:
            raise _conflict(r, after, None, "the graph was deleted")
        if row["locked"] and not admin:
            raise _locked_error(row["name"])
        cur = {"how": _how(c, gid, pid)}
        if not _same(cur, after):
            raise _conflict(r, after, cur)
        new = dict(before or {"how": None})
        _set_how(c, gid, pid, new.get("how"))
        cur_cmp, new_snap = cur, {"how": _how(c, gid, pid)}
    elif op == "paper.label":
        row = _paper_row(c, target)
        if row is None:
            raise _conflict(r, after, None, "the paper is gone")
        cur = {"label": row["label"]}
        if not _same(cur, after):
            raise _conflict(r, after, cur)
        new = dict(before or {"label": None})
        c.execute("UPDATE papers SET label = ? WHERE id = ?", (new.get("label"), target))
        cur_cmp, new_snap = cur, {"label": new.get("label")}
    else:
        raise _conflict(r, after, None, f"{op} cannot be undone")
    rid = _log(c, uid, "human", op, target, cur_cmp, new_snap, revert_of=r["id"])
    c.execute("UPDATE graph_log SET reverted_by = ? WHERE id = ?", (rid, r["id"]))
    return rid


# ---------------------------------------------------------------- routes

def _body_paper(req, b):
    pid = b.get("paper_id")
    if not isinstance(pid, str) or not pid:
        raise HTTPError(400, "bad_paper", "paper_id is required")
    return pid


def h_list(req):
    w = _world()
    req.send_json(200, {"graphs": [_graph_item(w, g, req.user) for g in w.graphs.values()]})


def h_get(req, gid):
    v = graph_for(gid, req.user)
    if v is None:
        raise HTTPError(404, "not_found", "no such graph")
    req.send_json(200, v)


def h_create(req):
    ensure_schema()
    b = req.json()
    name, tags = _name(b.get("name")), _tags(b.get("tags"))
    gid = db.new_id("g_", 10)
    with _tx() as c:
        c.execute("INSERT INTO graphs(id, name, rule_tags, locked, created_by, created_at) VALUES (?, ?, ?, 0, ?, ?)",
                  (gid, name, db.dumps(tags), req.user["id"], db.now()))
        lid = _log(c, req.user["id"], "human", "graph.create", gid, None,
                   {"name": name, "tags": tags, "locked": False, "deleted": False})
        revs = _bump(c, [gid], req.user["id"], "human", lid)
    entries = _after([lid])
    from . import layout
    layout.schedule([gid], delay=1.0)
    w = _world()
    req.send_json(201, {"graph": _graph_item(w, w.graphs[gid], req.user), "log": entries[0], "revs": revs})


def h_update(req, gid):
    ensure_schema()
    b = req.json()
    fields = {}
    if "name" in b:
        fields["name"] = _name(b["name"])
    if "tags" in b:
        fields["tags"] = _tags(b["tags"])
    if "locked" in b:
        if not isinstance(b["locked"], bool):
            raise HTTPError(400, "bad_locked", "locked is true or false")
        if not _is_admin(req.user):
            raise HTTPError(403, "forbidden", "only an admin can lock or unlock a graph")
        fields["locked"] = b["locked"]
    if not fields:
        raise HTTPError(400, "nothing", "send name, tags or locked")
    _, base = _base(req, b)
    ids, revs = [], {}
    with _tx() as c:
        row = _graph_row(c, gid)
        if row is None or row["deleted_at"] is not None:
            raise HTTPError(404, "not_found", "no such graph")
        g = _graph_snap(row)
        if g["locked"] and not _is_admin(req.user):
            raise _locked_error(g["name"])
        todo = [(key, op, col) for key, op, col in (("name", "graph.rename", "name"), ("tags", "graph.set_tags", "rule_tags"),
                                                    ("locked", "graph.lock", "locked")) if key in fields and fields[key] != g[key]]
        if todo:
            _fresh(c, _world(), gid, base)
        for key, op, col in todo:
            v = fields[key]
            c.execute(f"UPDATE graphs SET {col} = ? WHERE id = ?",
                      (db.dumps(v) if key == "tags" else (int(v) if key == "locked" else v), gid))
            ids.append(_log(c, req.user["id"], "human", op, gid, {key: g[key]}, {key: v}))
        if ids:
            revs = _bump(c, [gid], req.user["id"], "human", ids[-1])
    entries = _after(ids)
    w = _world()
    req.send_json(200, {"graph": _graph_item(w, w.graphs[gid], req.user), "log": entries, "revs": revs,
                        "already": not ids})


def h_delete(req, gid):
    ensure_schema()
    _, base = _base(req, req.json())
    with _tx() as c:
        row = _graph_row(c, gid)
        if row is None or row["deleted_at"] is not None:
            raise HTTPError(404, "not_found", "no such graph")
        if row["locked"] and not _is_admin(req.user):
            raise _locked_error(row["name"])
        if not (_is_admin(req.user) or row["created_by"] == req.user["id"]):
            raise HTTPError(403, "forbidden", "only the graph's maker or an admin can delete it")
        _fresh(c, _world(), gid, base)
        c.execute("UPDATE graphs SET deleted_at = ? WHERE id = ?", (db.now(), gid))
        lid = _log(c, req.user["id"], "human", "graph.delete", gid, {"deleted": False}, {"deleted": True})
        revs = _bump(c, [gid], req.user["id"], "human", lid)
    entries = _after([lid])
    req.send_json(200, {"log": entries[0], "revs": revs})


def _membership(req, gid, pid, want: str, b=None):
    ensure_schema()
    lid, revs = None, {}
    _, base = _base(req, b)
    with _tx() as c:
        row = _graph_row(c, gid)
        if row is None or row["deleted_at"] is not None:
            raise HTTPError(404, "not_found", "no such graph")
        if row["locked"] and not _is_admin(req.user):
            raise _locked_error(row["name"])
        p = _paper_row(c, pid)
        if p is None:
            raise HTTPError(404, "not_found", "no such paper")
        how, match = _how(c, gid, pid), _rule_matches(row, p)
        member = how == "added" or (how is None and match)
        if (want == "added") != member:
            _fresh(c, _world(), gid, base)
            _set_how(c, gid, pid, want)
            lid = _log(c, req.user["id"], "human", "graph.add_paper" if want == "added" else "graph.remove_paper",
                       f"{gid}/{pid}", {"how": how}, {"how": want})
            revs = _bump(c, [gid], req.user["id"], "human", lid)
    entries = _after([lid])
    w = _world()
    req.send_json(200, {"member": pid in w.members.get(gid, set()), "log": entries[0] if entries else None,
                        "revs": revs, "already": lid is None})


def h_add_paper(req, gid):
    b = req.json()
    _membership(req, gid, _body_paper(req, b), "added", b)


def h_remove_paper(req, gid, pid):
    _membership(req, gid, pid, "removed", req.json())


def h_relayout(req, gid):
    from . import layout
    if gid not in _world().graphs:
        raise HTTPError(404, "not_found", "no such graph")
    layout.request_full(gid)
    req.send_json(202, {"queued": True, "graph_id": gid})


def h_add_link(req):
    ensure_schema()
    b = req.json()
    src, dst = b.get("src"), b.get("dst")
    grade = _grade(b.get("grade", "s"))
    if not isinstance(src, str) or not isinstance(dst, str) or not src or not dst:
        raise HTTPError(400, "bad_link", "src and dst are paper ids")
    if grade is None:
        raise HTTPError(400, "bad_grade", "grade is e, s or w")
    if src == dst:
        raise HTTPError(400, "bad_link", "a paper cannot build on itself")
    bg, base = _base(req, b)
    done = lid = None
    with _tx() as c:
        ps, pd = _paper_row(c, src), _paper_row(c, dst)
        if ps is None or pd is None:
            raise HTTPError(404, "not_found", "no such paper")
        if ps["year"] and pd["year"] and ps["year"] > pd["year"]:
            raise HTTPError(400, "order", f"src is the earlier paper: {ps['year']} is after {pd['year']}")
        w = _world()
        _check_links_edit(w, req.user, src, dst)
        row = c.execute("SELECT * FROM links WHERE src = ? AND dst = ?", (src, dst)).fetchone()
        if base is not None and row is not None and row["state"] == "active" and row["grade"] == grade:
            done = _link_snap(row)          # made already (by someone else meanwhile): done, whatever base_rev says
        else:
            _fresh(c, w, bg, base)
            if row is not None and row["state"] == "active":
                raise HTTPError(409, "exists", "these papers are already linked", link=_link_snap(row))
            if _reaches(_adj(c), dst, src):
                raise HTTPError(409, "cycle", "that link would make a loop: the earlier paper already builds on the later one")
            after, lid = _person_link(c, req.user["id"], src, dst, grade, row)
            revs = _bump(c, _link_graphs(w, src, dst), req.user["id"], "human", lid)
    if done is not None:
        req.send_json(200, {"link": done, "log": None, "revs": {}, "already": True})
        return
    entries = _after([lid])
    req.send_json(201, {"link": after, "log": entries[0], "revs": revs})


def _person_link(c, user_id, src, dst, grade, row):
    """A person's new link (or a removed one they bring back), logged as their link.add.
    -> (the link's snapshot, the log row's id)"""
    at = db.now()
    if row is not None:
        before = _link_snap(row)
        c.execute("UPDATE links SET grade = ?, origin = 'human', state = 'active', updated_at = ? WHERE id = ?",
                  (grade, at, row["id"]))
        link_id = row["id"]
    else:
        before = None
        link_id = c.execute("INSERT INTO links(src, dst, grade, origin, state, created_by, created_at, updated_at) "
                            "VALUES (?, ?, ?, 'human', 'active', ?, ?, ?)", (src, dst, grade, user_id, at, at)).lastrowid
    after = _link_snap(_link_row(c, link_id))
    return after, _log(c, user_id, "human", "link.add", link_id, before, after)


def _link_change(req, link_id, grade=None, b=None):
    """Regrade (grade given) or remove a link; a no-op answers with the link and no log row."""
    ensure_schema()
    lid, revs = None, {}
    bg, base = _base(req, b)
    with _tx() as c:
        row = _link_row(c, int(link_id))
        if row is None:
            raise HTTPError(404, "not_found", "no such link")
        after = _link_snap(row)
        active = row["state"] == "active"
        if not active and grade is not None and base is None:
            raise HTTPError(409, "removed", "that link was removed")
        if active and (grade is None or grade != row["grade"]) or (not active and grade is not None):
            w = _world()
            _check_links_edit(w, req.user, row["src"], row["dst"])
            _fresh(c, w, bg, base)
            if not active:
                raise HTTPError(409, "removed", "that link was removed")
            if grade is not None:
                c.execute("UPDATE links SET grade = ?, updated_at = ? WHERE id = ?", (grade, db.now(), row["id"]))
                op = "link.grade"
            else:
                c.execute("UPDATE links SET state = 'removed', updated_at = ? WHERE id = ?", (db.now(), row["id"]))
                op = "link.remove"
            after = _link_snap(_link_row(c, row["id"]))
            lid = _log(c, req.user["id"], "human", op, row["id"], _link_snap(row), after)
            revs = _bump(c, _link_graphs(w, row["src"], row["dst"]), req.user["id"], "human", lid)
    entries = _after([lid])
    req.send_json(200, {"link": after, "log": entries[0] if entries else None, "revs": revs, "already": lid is None})


def h_grade_link(req, link_id):
    b = req.json()
    grade = _grade(b.get("grade"))
    if grade is None:
        raise HTTPError(400, "bad_grade", "grade is e, s or w")
    _link_change(req, link_id, grade, b)


def h_remove_link(req, link_id):
    _link_change(req, link_id, None, req.json())


def h_label(req, pid):
    ensure_schema()
    b = req.json()
    v = b.get("label")
    if v is not None and not isinstance(v, str):
        raise HTTPError(400, "bad_label", "label is a string (empty: automatic)")
    lab = " ".join((v or "").split()) or None
    if lab and len(lab) > LABEL_MAX:
        raise HTTPError(400, "bad_label", f"a label is at most {LABEL_MAX} characters")
    bg, base = _base(req, b)
    lid, revs = None, {}
    with _tx() as c:
        row = _paper_row(c, pid)
        if row is None:
            raise HTTPError(404, "not_found", "no such paper")
        if row["label"] != lab:
            w = _world()
            _fresh(c, w, bg, base)
            c.execute("UPDATE papers SET label = ? WHERE id = ?", (lab, pid))
            lid = _log(c, req.user["id"], "human", "paper.label", pid, {"label": row["label"]}, {"label": lab})
            revs = _bump(c, [gid for gid, m in w.members.items() if pid in m], req.user["id"], "human", lid)
    entries = _after([lid])
    p = _world().papers.get(pid) or {}
    req.send_json(200, {"paper": {"id": pid, "label": p.get("label"), "custom": lab is not None},
                        "log": entries[0] if entries else None, "revs": revs, "already": lid is None})


# ---------------------------------------------------------------- links from uploads: automatic or suggested

def _settings(c) -> dict:
    r = c.execute("SELECT s.*, u.name FROM graph_settings s LEFT JOIN users u ON u.id = s.updated_by "
                  "WHERE s.key = 'agent_links'").fetchone()
    return {"agent_links": _agent_mode(c), "suggestions": len(_world().sugg),
            "changed": {"by": {"id": r["updated_by"], "name": r["name"]} if r["updated_by"] is not None else None,
                        "at": r["updated_at"]} if r else None}


def h_settings(req):
    ensure_schema()
    req.send_json(200, _settings(db.conn()))


def h_set_settings(req):
    """PUT /api/graph-settings {"agent_links": "auto" | "suggest"} (admins)."""
    ensure_schema()
    v = req.json().get("agent_links")
    if v not in AGENT_MODES:
        raise HTTPError(400, "bad_mode", "agent_links is auto or suggest")
    with _tx() as c:
        c.execute("INSERT OR REPLACE INTO graph_settings(key, value, updated_by, updated_at) VALUES ('agent_links', ?, ?, ?)",
                  (v, req.user["id"], db.now()))
    events.publish("graph", {"change": "settings", "agent_links": v, "graphs": []})
    req.send_json(200, _settings(db.conn()))


def _sugg_row(c, sid):
    try:
        return c.execute("SELECT * FROM link_suggestions WHERE id = ?", (int(sid),)).fetchone()
    except ValueError:
        return None


def _sugg_out(r) -> dict:
    return {"id": r["id"], "src": r["src"], "dst": r["dst"], "grade": r["grade"], "state": r["state"],
            "link_id": r["link_id"]}


def _accept(c, w, adj, user_id, s):
    """Accept suggestion row s as user_id's link. -> (outcome, link snapshot or None, log id or None)
    Outcomes: accepted, exists (linked already: the suggestion is done), removed (a person removed
    that link: it stays removed), order, cycle."""
    src, dst = s["src"], s["dst"]
    row = c.execute("SELECT * FROM links WHERE src = ? AND dst = ?", (src, dst)).fetchone()
    if row is not None and row["state"] == "active":
        if s["state"] == "open":
            c.execute("UPDATE link_suggestions SET state = 'accepted', decided_by = ?, decided_at = ?, link_id = ? WHERE id = ?",
                      (user_id, db.now(), row["id"], s["id"]))
        return "exists", _link_snap(row), None
    if row is not None:
        return "removed", None, None
    ps, pd = _paper_row(c, src), _paper_row(c, dst)
    if ps["year"] and pd["year"] and ps["year"] > pd["year"]:
        return "order", None, None
    if _reaches(adj, dst, src):
        return "cycle", None, None
    after, lid = _person_link(c, user_id, src, dst, s["grade"], row)
    adj[src].append(dst)
    c.execute("UPDATE link_suggestions SET state = 'accepted', decided_by = ?, decided_at = ?, link_id = ? WHERE id = ?",
              (user_id, db.now(), after["id"], s["id"]))
    return "accepted", after, lid


def h_accept(req, sid):
    """POST /api/link-suggestions/<id>/accept: the suggestion becomes the person's link (their
    link.add: undo and redo work on it)."""
    ensure_schema()
    b = req.json()
    bg, base = _base(req, b)
    lid, revs = None, {}
    with _tx() as c:
        s = _sugg_row(c, sid)
        if s is None:
            raise HTTPError(404, "not_found", "no such suggestion")
        if s["state"] == "dismissed":
            raise HTTPError(409, "dismissed", "that suggestion was dismissed", suggestion=_sugg_out(s))
        w = _world()
        _check_links_edit(w, req.user, s["src"], s["dst"])
        cur = c.execute("SELECT state FROM links WHERE src = ? AND dst = ?", (s["src"], s["dst"])).fetchone()
        if s["state"] == "accepted" and not (cur and cur[0] == "active"):
            raise HTTPError(409, "decided", "that suggestion was accepted, and its link removed since", suggestion=_sugg_out(s))
        if not (cur and cur[0] == "active"):
            _fresh(c, w, bg, base)
        outcome, link, lid = _accept(c, w, _adj(c), req.user["id"], s)
        if outcome == "removed":
            raise HTTPError(409, "removed", "a person removed that link: it stays removed (add it by hand to bring it back)")
        if outcome == "order":
            raise HTTPError(400, "order", "the earlier paper must be the one built on")
        if outcome == "cycle":
            raise HTTPError(409, "cycle", "that link would make a loop: the earlier paper already builds on the later one")
        if lid is not None:
            revs = _bump(c, _link_graphs(w, s["src"], s["dst"]), req.user["id"], "human", lid)
        s = _sugg_row(c, sid)
    entries = _after([lid])
    _after_suggestions([s["id"]])
    req.send_json(200, {"suggestion": _sugg_out(s), "link": link, "log": entries[0] if entries else None, "revs": revs,
                        "already": lid is None})


def h_dismiss(req, sid):
    """POST /api/link-suggestions/<id>/dismiss: dropped for good; that pair is never suggested again."""
    ensure_schema()
    with _tx() as c:
        s = _sugg_row(c, sid)
        if s is None:
            raise HTTPError(404, "not_found", "no such suggestion")
        done = s["state"] != "open"
        if not done:
            _check_links_edit(_world(), req.user, s["src"], s["dst"])
            c.execute("UPDATE link_suggestions SET state = 'dismissed', decided_by = ?, decided_at = ? WHERE id = ?",
                      (req.user["id"], db.now(), s["id"]))
        s = _sugg_row(c, sid)
    if not done:
        _after_suggestions([s["id"]])
    req.send_json(200, {"suggestion": _sugg_out(s), "already": done})


def h_accept_all(req):
    """POST /api/link-suggestions/accept-all {"graph_id"?} (admins): every open suggestion (in that
    graph) the rules still allow becomes the admin's link, one log row each."""
    ensure_schema()
    gid = req.json().get("graph_id")
    if gid is not None and not isinstance(gid, str):
        raise HTTPError(400, "bad_graph", "graph_id is a graph's id")
    ids, sids, skipped, gids = [], [], [], []
    with _tx() as c:
        w, adj = _world(), _adj(c)
        mem = w.members.get(gid) if gid else None
        if gid and mem is None:
            raise HTTPError(404, "not_found", "no such graph")
        for s in c.execute("SELECT * FROM link_suggestions WHERE state = 'open' ORDER BY id").fetchall():
            if mem is not None and not (s["src"] in mem and s["dst"] in mem):
                continue
            outcome, link, lid = _accept(c, w, adj, req.user["id"], s)
            if outcome in ("accepted", "exists"):
                sids.append(s["id"])
            else:
                skipped.append({"id": s["id"], "reason": outcome})
            if lid is not None:
                ids.append(lid)
                gids += _link_graphs(w, s["src"], s["dst"])
        revs = _bump(c, gids, req.user["id"], "human", ids[-1] if ids else None)
    _after(ids)
    _after_suggestions(sids)
    req.send_json(200, {"accepted": len(ids), "skipped": skipped, "log_ids": ids, "revs": revs})


def _int_arg(req, name, default, lo, hi):
    v = req.arg(name)
    if v in (None, ""):
        return default
    try:
        return max(lo, min(hi, int(v)))
    except ValueError:
        raise HTTPError(400, "bad_arg", f"{name} is a number")


def h_log(req):
    ensure_schema()
    limit = _int_arg(req, "limit", 100, 1, LOG_LIMIT_MAX)
    before = _int_arg(req, "before", 0, 0, 1 << 62)
    gid = req.arg("graph") or None
    w = _world()
    mem = w.members.get(gid, set()) if gid else None
    with _reading() as c:
        rows, cursor, scanned = [], before or (1 << 62), 0
        while len(rows) < limit and scanned < 20000:
            chunk = c.execute("SELECT * FROM graph_log WHERE id < ? ORDER BY id DESC LIMIT ?",
                              (cursor, 500 if gid else limit)).fetchall()
            if not chunk:
                break
            cursor = chunk[-1]["id"]
            scanned += len(chunk)
            for r in chunk:
                if gid is None or _concerns(r, gid, mem):
                    rows.append(r)
            if gid is None or cursor <= 1:
                break
        rows = rows[:limit]
        entries = _entries(c, rows, w)
        cands = _candidates(c, req.user)
        pick = {s: {k: cands[s][k] for k in ("undo", "redo")} for s in ("mine", "any")}
        flat = [r for s in pick.values() for r in s.values() if r is not None]
        by_id = {e["id"]: e for e in _entries(c, flat, w)}
    req.send_json(200, {"log": entries,
                        "undo": {s: by_id.get(pick[s]["undo"]["id"]) if pick[s]["undo"] else None for s in pick},
                        "redo": {s: by_id.get(pick[s]["redo"]["id"]) if pick[s]["redo"] else None for s in pick}})


def _concerns(r, gid, mem) -> bool:
    op, t = r["op"], r["target"]
    if op.startswith("graph."):
        return t.partition("/")[0] == gid
    if op.startswith("link."):
        s = db.loads(r["after"]) or db.loads(r["before"]) or {}
        return s.get("src") in mem and s.get("dst") in mem
    if op == "paper.label":
        return t in mem
    return False


def h_revert(req):
    """Undo (or, with "redo": true, redo) the newest op in scope; `expect` must name it."""
    ensure_schema()
    b = req.json()
    scope = b.get("scope", "mine")
    if scope not in ("mine", "any"):
        raise HTTPError(400, "bad_scope", "scope is mine or any")
    try:
        expect = int(b.get("expect"))
    except (TypeError, ValueError):
        raise HTTPError(400, "bad_expect", "expect is the id of the op the page showed")
    redo = b.get("redo") is True
    bg, base = _base(req, b)
    with _tx() as c:
        slot = _candidates(c, req.user)[scope]
        want = slot["redo"] if redo else slot["undo"]
        target = None
        if want is not None and want["id"] == expect:
            target = want
        elif not redo and slot["latest"] is not None and slot["latest"]["id"] == expect:
            target = slot["latest"]      # SPEC's literal reading: the newest un-reverted row (an undo: redo it)
        if target is None:
            nxt = _entries(c, [want])[0] if want is not None else None
            raise HTTPError(409, "moved", "the history moved on: look again", next=nxt)
        target_id = target["id"]
        w = _world()
        touched = _affected(w, {"op": target["op"], "target": target["target"],
                                "before": db.loads(target["before"]), "after": db.loads(target["after"])})
        if bg in touched:                # the page shows a graph this undo changes: as it is now?
            _fresh(c, w, bg, base)
        rid = _revert_row(c, req.user, target)
        revs = _bump(c, touched, req.user["id"], "human", rid)
    entries = _after([rid])
    req.send_json(200, {"reverted": log_entries([target_id])[0], "log": entries[0], "revs": revs})


ROUTES = [
    ("GET", r"^/api/graphs$", h_list, "viewer"),
    ("POST", r"^/api/graphs$", h_create, "viewer"),
    ("GET", r"^/api/graphs/([^/]+)$", h_get, "viewer"),
    ("PUT", r"^/api/graphs/([^/]+)$", h_update, "viewer"),
    ("DELETE", r"^/api/graphs/([^/]+)$", h_delete, "viewer"),
    ("POST", r"^/api/graphs/([^/]+)/papers$", h_add_paper, "viewer"),
    ("DELETE", r"^/api/graphs/([^/]+)/papers/([^/]+)$", h_remove_paper, "viewer"),
    ("POST", r"^/api/graphs/([^/]+)/relayout$", h_relayout, "admin"),
    ("POST", r"^/api/links$", h_add_link, "viewer"),
    ("PUT", r"^/api/links/(\d+)$", h_grade_link, "viewer"),
    ("DELETE", r"^/api/links/(\d+)$", h_remove_link, "viewer"),
    ("GET", r"^/api/graph-log$", h_log, "viewer"),
    ("POST", r"^/api/graph-log/revert$", h_revert, "viewer"),
    ("PUT", r"^/api/papers/([^/]+)/label$", h_label, "viewer"),
    ("GET", r"^/api/graph-settings$", h_settings, "viewer"),
    ("PUT", r"^/api/graph-settings$", h_set_settings, "admin"),
    ("POST", r"^/api/link-suggestions/accept-all$", h_accept_all, "admin"),
    ("POST", r"^/api/link-suggestions/(\d+)/accept$", h_accept, "viewer"),
    ("POST", r"^/api/link-suggestions/(\d+)/dismiss$", h_dismiss, "viewer"),
]
