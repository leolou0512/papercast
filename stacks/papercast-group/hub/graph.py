"""Links, graphs, the edit log and revert (SPEC.md section 8). Owner: A5.

Links are global: src is the earlier paper, dst the paper built on it. A graph is a named set of
papers (its tag rule, plus papers added by hand, minus papers removed by hand) and shows the links
among its members. Every change is one row in graph_log with the state before and after, so a
change can be undone against the current state, and the undo undone.

Agents (the links an upload brings) only ever add: a link a person removed stays removed, and a
link that already exists (a person's or an agent's) is left as it is. A link whose other paper is
not in the library yet waits in pending_links until that paper arrives.

Locked graphs: only admins change them, their links included (a link between two members of a
locked graph); agents still add links and the tag rule still brings new papers in."""
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

EXTRA_SCHEMA = """
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
    __slots__ = ("sig", "users", "papers", "graphs", "how", "members", "links", "pos", "lstate")


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
        w.links = [dict(r) for r in c.execute("SELECT id, src, dst, grade, origin FROM links "
                                              "WHERE state = 'active' ORDER BY id")]
        w.pos = defaultdict(dict)
        for r in c.execute("SELECT graph_id, paper_id, x, y FROM layout"):
            w.pos[r[0]][r[1]] = (r[2], r[3])
        w.lstate = {r["graph_id"]: dict(r) for r in c.execute("SELECT * FROM layout_state")}
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
           "created_by": {"id": cb, "name": w.users.get(cb)} if cb is not None else None,
           "created_at": g["created_at"]}
    if user is not None:
        out["can_edit"] = _is_admin(user) or not g["locked"]
        out["can_delete"] = out["can_edit"] and (_is_admin(user) or cb == user.get("id"))
    return out


def _view(gid: str):
    """GET /api/graphs/<id> without the per-person parts, cached until the database changes."""
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
    current = st.get("sig") == layout_sig(ids, [(l["src"], l["dst"]) for l in links]) and len(placed) == len(ids)
    view = {"graph": _graph_item(w, g),
            "nodes": nodes,
            "links": [{"id": l["id"], "src": l["src"], "dst": l["dst"], "grade": l["grade"], "origin": l["origin"]}
                      for l in links],
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
    w = _world()
    g = w.graphs.get(gid)
    if g is not None and user is not None:
        out["graph"] = _graph_item(w, g, user)
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


def _depths(c, rows) -> dict:
    """Revert-chain depth of each row: 0 a change, odd an undo, even (> 0) a redo."""
    known = {r["id"]: r for r in rows}
    depth: dict = {}
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
        for x in reversed(chain):
            base += 1
            depth[x] = base
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
        who = f"the agent ({name}’s upload)" if r["actor"] == "agent" else name
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
    for gid, lid in touched.items():
        events.publish("graph", {"id": gid, "change": "edit", "log_id": lid})
    if relayout:
        layout.schedule(sorted(relayout))
    return entries


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


def _agent_add(c, adj, src, dst, grade, user_id):
    """One agent link, unless a person's decision or the time order says no. -> (outcome, log id)."""
    r = c.execute("SELECT * FROM links WHERE src = ? AND dst = ?", (src, dst)).fetchone()
    if r is not None:
        return ("removed" if r["state"] == "removed" else "exists"), None
    ys = c.execute("SELECT year FROM papers WHERE id = ?", (src,)).fetchone()
    yd = c.execute("SELECT year FROM papers WHERE id = ?", (dst,)).fetchone()
    if ys and yd and ys[0] and yd[0] and ys[0] > yd[0]:
        return "order", None             # the parent is the later paper (build.py drops these too)
    if _reaches(adj, dst, src):
        return "cycle", None
    at = db.now()
    cur = c.execute("INSERT INTO links(src, dst, grade, origin, state, created_by, created_at, updated_at) "
                    "VALUES (?, ?, ?, 'agent', 'active', ?, ?, ?)", (src, dst, grade, user_id, at, at))
    adj[src].append(dst)
    snap = _link_snap(_link_row(c, cur.lastrowid))
    return "added", _log(c, user_id, "agent", "link.add", cur.lastrowid, None, snap)


def _resolve_for(c, adj, paper) -> list:
    """Pending links that name this paper: add them now. -> log ids."""
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
            outcome, lid = _agent_add(c, adj, paper["id"], r["paper_id"], r["grade"], r["user_id"])
        else:
            outcome, lid = _agent_add(c, adj, r["paper_id"], paper["id"], r["grade"], r["user_id"])
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

    -> {"added", "pending", "resolved", "skipped": [{"index", "reason"}], "log_ids"}"""
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
    out = {"added": 0, "pending": 0, "resolved": 0, "skipped": [], "log_ids": []}
    ids = []
    with _tx() as c:
        me = _paper_row(c, paper_id)
        if me is None:
            raise ValueError(f"apply_agent_links: no paper {paper_id}")
        adj = _adj(c)
        got = _resolve_for(c, adj, me)
        out["resolved"] = len(got)
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
            outcome, lid = _agent_add(c, adj, src, dst, grade, user_id)
            if lid is None:
                out["skipped"].append({"index": k, "reason": outcome})
            else:
                out["added"] += 1
                ids.append(lid)
    out["log_ids"] = ids
    _after(ids)
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
        for r in rows:
            ids += _resolve_for(c, adj, r)
    _after(ids)
    return len(ids)


on_paper_created = resolve_pending


# ---------------------------------------------------------------- revert

def _candidates(c, user) -> dict:
    """Among the newest 100 ops: per scope, the change to undo (the newest un-reverted change or
    redo), the undo to redo, and the newest un-reverted row of any kind. `mine` is the person's
    own edits (not the agent's links from their uploads)."""
    rows = c.execute("SELECT * FROM graph_log ORDER BY id DESC LIMIT ?", (UNDO_WINDOW,)).fetchall()
    depth = _depths(c, rows)
    res = {s: {"undo": None, "redo": None, "latest": None} for s in ("mine", "any")}
    for r in rows:
        if r["reverted_by"] is not None:
            continue
        kind = "redo" if depth.get(r["id"], 0) % 2 else "undo"
        for scope in ("mine", "any"):
            if scope == "mine" and not (user is not None and r["user_id"] == user["id"] and r["actor"] == "human"):
                continue
            slot = res[scope]
            if slot["latest"] is None:
                slot["latest"] = r
            if slot[kind] is None:
                slot[kind] = r
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
    entries = _after([lid])
    from . import layout
    layout.schedule([gid], delay=1.0)
    w = _world()
    req.send_json(201, {"graph": _graph_item(w, w.graphs[gid], req.user), "log": entries[0]})


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
    ids = []
    with _tx() as c:
        row = _graph_row(c, gid)
        if row is None or row["deleted_at"] is not None:
            raise HTTPError(404, "not_found", "no such graph")
        g = _graph_snap(row)
        if g["locked"] and not _is_admin(req.user):
            raise _locked_error(g["name"])
        for key, op, col in (("name", "graph.rename", "name"), ("tags", "graph.set_tags", "rule_tags"),
                             ("locked", "graph.lock", "locked")):
            if key in fields and fields[key] != g[key]:
                v = fields[key]
                c.execute(f"UPDATE graphs SET {col} = ? WHERE id = ?",
                          (db.dumps(v) if key == "tags" else (int(v) if key == "locked" else v), gid))
                ids.append(_log(c, req.user["id"], "human", op, gid, {key: g[key]}, {key: v}))
    entries = _after(ids)
    w = _world()
    req.send_json(200, {"graph": _graph_item(w, w.graphs[gid], req.user), "log": entries})


def h_delete(req, gid):
    ensure_schema()
    with _tx() as c:
        row = _graph_row(c, gid)
        if row is None or row["deleted_at"] is not None:
            raise HTTPError(404, "not_found", "no such graph")
        if row["locked"] and not _is_admin(req.user):
            raise _locked_error(row["name"])
        if not (_is_admin(req.user) or row["created_by"] == req.user["id"]):
            raise HTTPError(403, "forbidden", "only the graph's maker or an admin can delete it")
        c.execute("UPDATE graphs SET deleted_at = ? WHERE id = ?", (db.now(), gid))
        lid = _log(c, req.user["id"], "human", "graph.delete", gid, {"deleted": False}, {"deleted": True})
    entries = _after([lid])
    req.send_json(200, {"log": entries[0]})


def _membership(req, gid, pid, want: str):
    ensure_schema()
    lid = None
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
            _set_how(c, gid, pid, want)
            lid = _log(c, req.user["id"], "human", "graph.add_paper" if want == "added" else "graph.remove_paper",
                       f"{gid}/{pid}", {"how": how}, {"how": want})
    entries = _after([lid])
    w = _world()
    req.send_json(200, {"member": pid in w.members.get(gid, set()), "log": entries[0] if entries else None})


def h_add_paper(req, gid):
    _membership(req, gid, _body_paper(req, req.json()), "added")


def h_remove_paper(req, gid, pid):
    _membership(req, gid, pid, "removed")


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
    with _tx() as c:
        ps, pd = _paper_row(c, src), _paper_row(c, dst)
        if ps is None or pd is None:
            raise HTTPError(404, "not_found", "no such paper")
        if ps["year"] and pd["year"] and ps["year"] > pd["year"]:
            raise HTTPError(400, "order", f"src is the earlier paper: {ps['year']} is after {pd['year']}")
        _check_links_edit(_world(), req.user, src, dst)
        row = c.execute("SELECT * FROM links WHERE src = ? AND dst = ?", (src, dst)).fetchone()
        if row is not None and row["state"] == "active":
            raise HTTPError(409, "exists", "these papers are already linked", link=_link_snap(row))
        if _reaches(_adj(c), dst, src):
            raise HTTPError(409, "cycle", "that link would make a loop: the earlier paper already builds on the later one")
        at = db.now()
        if row is not None:           # a removed link a person brings back
            before = _link_snap(row)
            c.execute("UPDATE links SET grade = ?, origin = 'human', state = 'active', updated_at = ? WHERE id = ?",
                      (grade, at, row["id"]))
            link_id = row["id"]
        else:
            before = None
            link_id = c.execute("INSERT INTO links(src, dst, grade, origin, state, created_by, created_at, updated_at) "
                                "VALUES (?, ?, ?, 'human', 'active', ?, ?, ?)",
                                (src, dst, grade, req.user["id"], at, at)).lastrowid
        after = _link_snap(_link_row(c, link_id))
        lid = _log(c, req.user["id"], "human", "link.add", link_id, before, after)
    entries = _after([lid])
    req.send_json(201, {"link": after, "log": entries[0]})


def _link_change(req, link_id, grade=None):
    """Regrade (grade given) or remove a link; a no-op answers with the link and no log row."""
    ensure_schema()
    lid = None
    with _tx() as c:
        row = _link_row(c, int(link_id))
        if row is None:
            raise HTTPError(404, "not_found", "no such link")
        after = _link_snap(row)
        if row["state"] != "active" and grade is not None:
            raise HTTPError(409, "removed", "that link was removed")
        if row["state"] == "active" and (grade is None or grade != row["grade"]):
            _check_links_edit(_world(), req.user, row["src"], row["dst"])
            if grade is not None:
                c.execute("UPDATE links SET grade = ?, updated_at = ? WHERE id = ?", (grade, db.now(), row["id"]))
                op = "link.grade"
            else:
                c.execute("UPDATE links SET state = 'removed', updated_at = ? WHERE id = ?", (db.now(), row["id"]))
                op = "link.remove"
            after = _link_snap(_link_row(c, row["id"]))
            lid = _log(c, req.user["id"], "human", op, row["id"], _link_snap(row), after)
    entries = _after([lid])
    req.send_json(200, {"link": after, "log": entries[0] if entries else None})


def h_grade_link(req, link_id):
    grade = _grade(req.json().get("grade"))
    if grade is None:
        raise HTTPError(400, "bad_grade", "grade is e, s or w")
    _link_change(req, link_id, grade)


def h_remove_link(req, link_id):
    _link_change(req, link_id)


def h_label(req, pid):
    ensure_schema()
    v = req.json().get("label")
    if v is not None and not isinstance(v, str):
        raise HTTPError(400, "bad_label", "label is a string (empty: automatic)")
    lab = " ".join((v or "").split()) or None
    if lab and len(lab) > LABEL_MAX:
        raise HTTPError(400, "bad_label", f"a label is at most {LABEL_MAX} characters")
    lid = None
    with _tx() as c:
        row = _paper_row(c, pid)
        if row is None:
            raise HTTPError(404, "not_found", "no such paper")
        if row["label"] != lab:
            c.execute("UPDATE papers SET label = ? WHERE id = ?", (lab, pid))
            lid = _log(c, req.user["id"], "human", "paper.label", pid, {"label": row["label"]}, {"label": lab})
    entries = _after([lid])
    p = _world().papers.get(pid) or {}
    req.send_json(200, {"paper": {"id": pid, "label": p.get("label"), "custom": lab is not None},
                        "log": entries[0] if entries else None})


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
        rid = _revert_row(c, req.user, target)
    entries = _after([rid])
    req.send_json(200, {"reverted": log_entries([target_id])[0], "log": entries[0]})


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
]
