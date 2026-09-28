"""Search: every word a paper carries, typo-tolerant, ranked (GET /api/library?q=, web.py).

What is searched, per paper in the library: its title, map label, authors, tags, the graphs
(topics) it is in, who made its versions, its year, arXiv id and DOI, and the content: every
live version's script.md, claims.md and explainer points, and the paper's own text (paper.txt,
the text the CLI pulled out of the PDF; never served, only searched). A paper without it is
still found by everything else.

The index is $PCG_DATA/search.db, SQLite FTS5, made from hub.db and the episode files and
nothing else: deleted, it is made again (a background build at the hub's start); the backup
leaves it out. Four tables, one row per paper each, so each part is weighed against its own
length (FTS5's bm25 normalises by the whole row, which would let a long paper text drown its
title):
    m  title, label, authors, tags, graphs, makers, ids    words (unicode61, accents folded)
    e  claims, explainer, script                           words
    p  paper                                               words
    t  title, label, tags, graphs                          trigrams: "bert" finds DistilBERT
                                                           (4 letters or more; not in names,
                                                           where "bert" is Hubert)
The paper text is matched by whole words (and a prefix of four letters or more): inside a
word, "bert" is in every Hilbert space.

Ranking: 3 m + 0.5 t + 1 e + 0.4 p of their bm25 scores (column weights inside each), newer
papers a little ahead (up to 3 % by year) and first on ties.

A query: words (each must match somewhere in the paper; the last one as a prefix while it is
being typed) and "quoted phrases" (exactly: those words, one after another). A word the index
does not know is corrected to the closest one it does (edit distance 1 for 4 to 7 letters, 2
for longer; swapping two neighbours is one edit), from a vocabulary made from the index
(fz_word; fz_del holds every word with one or two letters deleted, SymSpell's trick, so a
lookup is a few dozen index probes, not a scan), and the answer says so.

Kept in step: every paper has a fingerprint of what each part was made from; a sync compares
them with hub.db and indexes again only what changed. It runs in a background thread on the
hub's events (uploads, deletes, graph edits) and every few minutes, and before a query whenever
hub.db's cheap signature moved (a write from another process: the import, say). Membership of
a graph is graph.py's own rule (its _members, fed the same rows)."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import queue
import re
import sqlite3
import threading
import time
import unicodedata
import zlib
from contextlib import contextmanager
from pathlib import Path

from . import db, events, graph
from .app import HTTPError

log = logging.getLogger("pcg.search")

# The index's own version: moved whenever its tables, fold() or what is stored changes, since a
# contentless FTS5 table can take a row out only when told exactly what it indexed. An index of
# another version is deleted and built again.
SCHEMA_V = "2"
META_FIELDS = ("title", "label", "authors", "tags", "graphs", "makers", "ids")
TRI_FIELDS = ("title", "label", "tags", "graphs")
EP_FIELDS = ("claims", "explainer", "script")
# bm25 column weights inside each table, and each table's weight in the sum
M_W = (4.0, 4.0, 2.0, 2.0, 2.0, 1.5, 1.5)
T_W = (4.0, 4.0, 2.0, 2.0)
E_W = (2.0, 2.0, 1.0)
TABLE_W = {"m": 3.0, "t": 0.5, "e": 1.0, "p": 0.4}
RECENT = 0.03               # the newest papers' score gets up to 3 % more
PAPER_TEXT_MAX = 2 * 1024 * 1024
SNIPPETS = 24               # the rows first on screen come with snippets; the page asks for the rest
HEAD = 24_000
MAX_CLAUSES = 8
MAX_IDS = 100
SYNC_EVERY_S = 300.0        # a full comparison, for what neither an event nor the signature shows
BATCH_BYTES = 4 * 1024 * 1024
STOP = frozenset("a an and are as at be by for from has have in into is it its of on or that the "
                 "their this to was were which with".split())
WORD = re.compile(r"[^\W_]+")

_states: dict = {}
_states_lock = threading.Lock()


class _State:
    """One data dir's index: its file, the lock that makes one sync at a time, and progress."""

    def __init__(self, data: Path):
        self.path = data / "search.db"
        self.hub = data / "hub.db"
        self.episodes = data / "episodes"
        self.lock = threading.Lock()
        self.vlock = threading.Lock()
        self.checked = False        # the file's schema version was looked at
        self.made = False           # its tables were made (once per process)
        self.synced = None          # hub.db's signature at the last sync
        self.full_at = 0.0          # when the last full comparison ran
        self.pending = 0            # papers not in the index yet (the first build)
        self.total = 0
        self.fz_dirty = True        # the vocabulary changed since fz_word was made
        self.thread = None
        self.pool = []              # idle connections: a warm page cache for the next query
        self.pool_lock = threading.Lock()


def _state(cfg) -> _State:
    key = str(Path(cfg.data).resolve())
    with _states_lock:
        st = _states.get(key)
        if st is None:
            st = _states[key] = _State(Path(key))
        return st


# ---------------------------------------------------------------- text: folding and tokens

_fold_cache: dict = {}


def _fold_char(ch: str) -> str:
    base = "".join(c for c in unicodedata.normalize("NFKD", ch) if not unicodedata.combining(c)).lower()
    if len(base) == 1:
        return base
    low = ch.lower()
    return low if len(low) == 1 else ch


_NON_ASCII = re.compile(r"[^\x00-\x7f]")


def fold(s: str) -> str:
    """Lowercase with accents taken off, one character for one, so an offset in the folded
    text is the same offset in the original (FTS5's unicode61 folds its tokens the same way).
    lower() and a replace() per accented letter: a paper's text has a few dozen of them, and
    both run in C (translate() with a table is ten times slower on 100 kB)."""
    if s.isascii():
        return s.lower()
    odd = set(_NON_ASCII.findall(s))
    for ch in odd:                     # the few whose lowercase is not one character: first
        if len(ch.lower()) != 1:
            s = s.replace(ch, _folded_char(ch))
    s = s.lower()
    for ch in odd:
        lo = ch.lower()
        if len(lo) == 1:
            f = _folded_char(ch)
            if f != lo:
                s = s.replace(lo, f)
    return s


def _folded_char(ch: str) -> str:
    f = _fold_cache.get(ch)
    if f is None:
        f = _fold_cache[ch] = _fold_char(ch)
    return f


def tokens(s: str) -> list:
    return WORD.findall(fold(s))


def _clean_paper(t: str) -> str:
    """pdftotext's output as one line of words: a word broken at a line end is joined again
    ("diffu-\\nsion"), control characters and runs of white space become one space."""
    t = re.sub(r"(?<=[a-z])-\n(?=[a-z])", "", t)
    t = re.sub(r"[\x00-\x08\x0e-\x1f\x7f]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _z(s: str) -> bytes:
    return zlib.compress(s.encode("utf-8"), 3)


def _unz(b) -> str:
    return zlib.decompress(b).decode("utf-8") if b else ""


# ---------------------------------------------------------------- the index file

SCHEMA = [
    "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    """CREATE TABLE IF NOT EXISTS doc (
         id INTEGER PRIMARY KEY,                 -- the rowid in m, e, p and t
         pid TEXT NOT NULL UNIQUE,
         sig_m TEXT NOT NULL, sig_e TEXT NOT NULL, sig_p TEXT NOT NULL,
         title TEXT, label TEXT, authors TEXT, tags TEXT, graphs TEXT, makers TEXT, ids TEXT,
         claims BLOB, explainer BLOB, script BLOB, paper BLOB,     -- zlib, UTF-8
         head BLOB)                              -- the paper text's first HEAD characters: most snippets are there""",
    "CREATE VIRTUAL TABLE IF NOT EXISTS m USING fts5(title, label, authors, tags, graphs, makers, ids, "
    "content='', tokenize='unicode61 remove_diacritics 2', prefix='2 3')",
    "CREATE VIRTUAL TABLE IF NOT EXISTS e USING fts5(claims, explainer, script, "
    "content='', tokenize='unicode61 remove_diacritics 2', prefix='2 3')",
    "CREATE VIRTUAL TABLE IF NOT EXISTS p USING fts5(paper, content='', tokenize='unicode61 remove_diacritics 2')",
    "CREATE VIRTUAL TABLE IF NOT EXISTS t USING fts5(title, label, tags, graphs, content='', tokenize='trigram')",
    "CREATE VIRTUAL TABLE IF NOT EXISTS mv USING fts5vocab(m, 'row')",
    "CREATE VIRTUAL TABLE IF NOT EXISTS ev USING fts5vocab(e, 'row')",
    "CREATE VIRTUAL TABLE IF NOT EXISTS pv USING fts5vocab(p, 'row')",
    # the correction vocabulary: its words, and every string a word gives with a letter (or
    # two) deleted, so the words close to a query word are found by probing its own deletions
    "CREATE TABLE IF NOT EXISTS fz_word (id INTEGER PRIMARY KEY, word TEXT NOT NULL UNIQUE, df INTEGER NOT NULL)",
    "CREATE TABLE IF NOT EXISTS fz_del (key TEXT NOT NULL, wid INTEGER NOT NULL, PRIMARY KEY (key, wid)) WITHOUT ROWID",
]


POOL = 6


@contextmanager
def _conn(st: _State):
    """A connection to the index, one thread's at a time (WAL: queries read while a sync
    writes). Connections are kept between requests, which each come on a new thread, so the
    index's pages stay in SQLite's cache."""
    with st.pool_lock:
        c = st.pool.pop() if st.pool else None
    if c is None:
        _check_version(st)
        c = sqlite3.connect(str(st.path), timeout=30, isolation_level=None, check_same_thread=False)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA busy_timeout=30000")
        c.execute("PRAGMA synchronous=NORMAL")       # derived data: a crash costs a rebuild, not a loss
        c.execute("PRAGMA cache_size=-16384")        # 16 MB each
        if not st.made:
            for q in SCHEMA:
                c.execute(q)
            c.execute("INSERT OR IGNORE INTO meta(key, value) VALUES ('schema', ?)", (SCHEMA_V,))
            st.made = True
    try:
        yield c
    finally:
        if c.in_transaction:
            c.execute("ROLLBACK")
        with st.pool_lock:
            keep = len(st.pool) < POOL and st.path.parent.is_dir()
            if keep:
                st.pool.append(c)
        if not keep:
            c.close()


def _check_version(st: _State) -> None:
    """An index another version of this code made is deleted, once per process: it is derived
    data, and the next sync makes it again."""
    with st.vlock:
        if st.checked:
            return
        st.checked = True
        if not st.path.exists():
            return
        try:
            c = sqlite3.connect(str(st.path), timeout=30)
            try:
                r = c.execute("SELECT value FROM meta WHERE key = 'schema'").fetchone()
                v = r[0] if r else None
            finally:
                c.close()
        except sqlite3.Error:
            v = "unreadable"
        if v != SCHEMA_V:
            log.info("search index %s is version %s, not %s: making it again", st.path, v, SCHEMA_V)
            for suffix in ("", "-wal", "-shm"):
                try:
                    os.unlink(str(st.path) + suffix)
                except FileNotFoundError:
                    pass


def close() -> None:
    """Close every idle connection to every index (tests, tools)."""
    with _states_lock:
        states = list(_states.values())
    for st in states:
        with st.pool_lock:
            pool, st.pool = st.pool, []
        for c in pool:
            c.close()


# ---------------------------------------------------------------- what the library holds

# Changes whenever anything the index is made from changes, whoever wrote it (the hub, the
# import, another process); cheap: aggregates and a few short lists.
_SIG_SQL = """SELECT
 (SELECT count(*) || ':' || coalesce(max(rowid), 0) || ':' || total(length(title)) || ':' || total(length(tags))
         || ':' || total(length(authors)) || ':' || total(year) || ':' || total(length(arxiv_id)) || ':' || total(length(doi))
         || ':' || total(length(label)) FROM papers),
 (SELECT count(*) || ':' || coalesce(max(rowid), 0) || ':' || count(deleted_at) || ':' || total(state = 'rejected')
         || ':' || total(made_by) || ':' || total(length(deleted_at)) FROM episodes),
 (SELECT count(*) || ':' || coalesce(group_concat(id || '=' || name, ','), '') FROM users),
 (SELECT count(*) || ':' || coalesce(group_concat(id || '=' || name || '=' || rule_tags || '=' || coalesce(deleted_at, '')
         || '=' || coalesce(created_by, ''), ','), '') FROM graphs),
 (SELECT count(*) || ':' || coalesce(group_concat(graph_id || paper_id || how, ','), '') FROM graph_members),
 (SELECT coalesce(max(id), 0) FROM graph_log)"""
_cat_cache: dict = {}


def _signature(c) -> str:
    try:
        row = c.execute(_SIG_SQL).fetchone()
    except sqlite3.OperationalError:            # no papers.label yet (a database before migration 2)
        row = c.execute(_SIG_SQL.replace(" || ':' || total(length(label))", "")).fetchone()
    return str(db._path) + "|" + "|".join(str(v) for v in row)


class Catalog:
    """The library as the index sees it, read in one snapshot: the papers with at least one
    live version, what each is made from, and the graphs with their members."""
    __slots__ = ("sig", "papers", "graphs", "years")


def catalog(c=None, sig=None) -> Catalog:
    c = c or db.conn()
    sig = sig or _signature(c)
    key = str(db._path)
    hit = _cat_cache.get(key)
    if hit is not None and hit.sig == sig:
        return hit
    in_tx = c.in_transaction
    if not in_tx:
        c.execute("BEGIN")
    try:
        cols = {r[1] for r in c.execute("PRAGMA table_info(papers)")}
        label = "label" if "label" in cols else "NULL AS label"
        users = {r[0]: r[1] for r in c.execute("SELECT id, name FROM users")}
        rows = c.execute(f"SELECT id, title, title_norm, authors, tags, year, arxiv_id, doi, {label}, created_at FROM papers").fetchall()
        eps = c.execute("SELECT id, paper_id, made_by, state, deleted_at, created_at FROM episodes ORDER BY created_at, rowid").fetchall()
        grows = c.execute("SELECT id, name, rule_tags, created_by FROM graphs WHERE deleted_at IS NULL ORDER BY created_at, rowid").fetchall()
        how: dict = {}
        for r in c.execute("SELECT graph_id, paper_id, how FROM graph_members"):
            how.setdefault(r[0], {})[r[1]] = r[2]
    finally:
        if not in_tx:
            c.execute("COMMIT")
    papers, gpapers = {}, {}
    for r in rows:
        tags = [t for t in (db.loads(r["tags"], []) or []) if isinstance(t, str) and t.strip()]
        papers[r["id"]] = {
            "id": r["id"], "title": r["title"] or "", "tags": tags,
            "authors": [a for a in (db.loads(r["authors"], []) or []) if isinstance(a, str)],
            "year": r["year"] if isinstance(r["year"], int) else None, "arxiv_id": r["arxiv_id"], "doi": r["doi"],
            "label": r["label"] or graph.auto_label(r["title"], r["arxiv_id"], r["doi"], r["title_norm"]),
            "eids": [], "makers": [], "maker_ids": set(), "added": None, "graphs": [], "all": 0}
        gpapers[r["id"]] = {"tags": {t.strip().lower() for t in tags}, "visible": True}
    for e in eps:
        p = papers.get(e["paper_id"])
        if p is None:
            continue
        p["all"] += 1
        if e["deleted_at"] is None and e["state"] != "rejected":
            p["eids"].append(e["id"])
            p["added"] = p["added"] or e["created_at"]
            p["maker_ids"].add(e["made_by"])
            name = users.get(e["made_by"])
            if name and name not in p["makers"]:
                p["makers"].append(name)
    for pid, p in papers.items():
        gpapers[pid]["visible"] = p["all"] == 0 or bool(p["eids"])
    graphs = {}
    for g in grows:
        item = {"id": g["id"], "name": g["name"] or "", "tags": graph._clean_tags(db.loads(g["rule_tags"], [])),
                "creator_id": g["created_by"], "creator": users.get(g["created_by"]) if g["created_by"] is not None else None}
        # graph.py's rule: the tag rule, plus the papers added, minus those removed
        item["members"] = {pid for pid in graph._members(item, how.get(g["id"], {}), gpapers) if papers[pid]["eids"]}
        graphs[g["id"]] = item
        for pid in item["members"]:
            papers[pid]["graphs"].append(g["id"])
    cat = Catalog()
    cat.sig = sig
    cat.papers = {pid: p for pid, p in papers.items() if p["eids"]}
    cat.graphs = graphs
    ys = [p["year"] for p in cat.papers.values() if p["year"] is not None]
    cat.years = (min(ys), max(ys)) if ys else (None, None)
    if not in_tx:                 # never keep what an open (maybe rolled back) transaction sees
        _cat_cache[key] = cat
    return cat


def _meta_fields(cat: Catalog, p: dict) -> dict:
    return {"title": p["title"], "label": p["label"] if p["label"] != p["title"] else "",
            "authors": "; ".join(p["authors"]), "tags": "; ".join(p["tags"]),
            "graphs": "; ".join(cat.graphs[g]["name"] for g in p["graphs"]),
            "makers": "; ".join(p["makers"]),
            "ids": " ".join(str(x) for x in (p["year"], p["arxiv_id"], p["doi"]) if x)}


def _h(obj) -> str:
    return hashlib.sha1(json.dumps(obj, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def _paper_eid(st: _State, p: dict):
    """The version whose paper.txt the paper is searched by: the first made that has one."""
    for eid in p["eids"]:
        f = st.episodes / eid / "paper.txt"
        if f.is_file() and not f.is_symlink():
            return eid
    return None


def _read(f: Path, limit: int) -> str:
    try:
        if f.is_symlink() or not f.is_file():
            return ""
        with open(f, "rb") as fh:
            return fh.read(limit).decode("utf-8", "replace").lstrip("﻿")
    except OSError:
        return ""


def _explainer_text(f: Path) -> str:
    try:
        obj = json.loads(_read(f, 8 * 1024 * 1024) or "{}")
    except ValueError:
        return ""
    if not isinstance(obj, dict):
        return ""
    out = [x for x in obj.get("points") or [] if isinstance(x, str)]
    out += [g["caption"] for g in obj.get("figures") or [] if isinstance(g, dict) and isinstance(g.get("caption"), str)]
    return "\n".join(out)


def _claims_text(f: Path) -> str:
    """claims.md without its front matter (the title and authors, searched as themselves)."""
    t = _read(f, 256 * 1024)
    if t.startswith("---"):
        end = t.find("\n---", 3)
        if end >= 0:
            t = t[end + 4:]
    return t.strip()


def _ep_fields(st: _State, eids: list) -> list:
    """[claims, explainer, script] of these versions, each joined over the versions."""
    parts = ([], [], [])
    for eid in eids:
        d = st.episodes / eid
        parts[0].append(re.sub(r"(?m)^[ \t]*[-*][ \t]+", "", _claims_text(d / "claims.md")))    # the bullets' dashes
        parts[1].append(_explainer_text(d / "explainer.json"))
        parts[2].append(re.sub(r"(?m)^[ \t]*#+[ \t]*", "", _read(d / "script.md", 256 * 1024)))  # the headings' marks
    return ["\n\n".join(x for x in v if x) for v in parts]


# ---------------------------------------------------------------- keeping the index in step

def _fts(c, table: str, cols: tuple, rowid: int, new: list | None, old: list | None) -> None:
    names, marks = ", ".join(cols), ", ".join("?" * len(cols))
    if old is not None:           # a contentless table forgets a row only when told what it held
        c.execute(f"INSERT INTO {table}({table}, rowid, {names}) VALUES ('delete', ?, {marks})", [rowid] + old)
    if new is not None:
        c.execute(f"INSERT INTO {table}(rowid, {names}) VALUES (?, {marks})", [rowid] + new)


def _folded(values: list) -> list:
    """What the trigram table holds, from the meta values: the fields it has, folded (its
    tokenizer, in SQLite 3.37, lowers case but keeps accents)."""
    return [fold(values[META_FIELDS.index(k)] or "") for k in TRI_FIELDS]


def sync(cfg, full: bool = False, fuzzy: bool = False) -> dict:
    """Bring the index in step with hub.db: papers new or changed are indexed again (only the
    parts whose inputs changed), papers gone from the library are taken out. `full`: also look
    at the files (a paper.txt that arrived for an existing version). `fuzzy`: then bring the
    correction vocabulary up to date too. Returns counts."""
    st = _state(cfg)
    with st.lock:
        return _sync(st, full, fuzzy)


def _sync(st: _State, full: bool, fuzzy: bool) -> dict:
    with _conn(st) as c:
        return _sync_with(st, c, full, fuzzy)


def _sync_with(st: _State, c, full: bool, fuzzy: bool) -> dict:
    hc = db.conn()
    sig = _signature(hc)
    cat = catalog(hc, sig)
    by_pid = {r["pid"]: r for r in c.execute("SELECT id, pid, sig_m, sig_e, sig_p FROM doc")}
    todo = []
    for pid, p in cat.papers.items():
        meta = _meta_fields(cat, p)
        sm, se = _h(meta), _h(p["eids"])
        old = by_pid.get(pid)
        if old is not None and not full and old["sig_m"] == sm and old["sig_e"] == se:
            continue              # the paper text goes with the versions; `full` looks at the files too
        pe = _paper_eid(st, p)
        sp = _h([pe])
        if old is not None and (old["sig_m"], old["sig_e"], old["sig_p"]) == (sm, se, sp):
            continue
        todo.append((pid, p, meta, sm, se, pe, sp, old is None))
    gone = [r["id"] for pid, r in by_pid.items() if pid not in cat.papers]
    st.total = len(cat.papers)
    st.pending = sum(1 for x in todo if x[7])
    n = {"indexed": 0, "removed": 0}
    size = 0
    c.execute("BEGIN IMMEDIATE")
    try:
        for rid in gone:
            _drop(c, rid)
            n["removed"] += 1
        for pid, p, meta, sm, se, pe, sp, new in todo:
            size += _index_one(st, c, pid, p, meta, sm, se, pe, sp)
            n["indexed"] += 1
            if new:
                st.pending -= 1
            if size > BATCH_BYTES:            # readers see the progress; a crash keeps what is done
                c.execute("COMMIT")
                c.execute("BEGIN IMMEDIATE")
                size = 0
        c.execute("COMMIT")
    except BaseException:
        c.execute("ROLLBACK")
        raise
    finally:
        st.pending = 0
    st.synced = sig
    if full:
        st.full_at = time.monotonic()
    if n["indexed"] or n["removed"]:
        st.fz_dirty = True
        log.info("search index: %d papers indexed, %d removed", n["indexed"], n["removed"])
    if fuzzy and st.fz_dirty:
        _fuzzy_refresh(st, c)
    return n


def _drop(c, rid: int) -> None:
    r = c.execute("SELECT * FROM doc WHERE id = ?", (rid,)).fetchone()
    if r is None:
        return
    short = [r[k] or "" for k in META_FIELDS]
    _fts(c, "m", META_FIELDS, rid, None, short)
    _fts(c, "t", TRI_FIELDS, rid, None, _folded(short))
    _fts(c, "e", EP_FIELDS, rid, None, [_unz(r[k]) for k in EP_FIELDS])
    _fts(c, "p", ("paper",), rid, None, [_unz(r["paper"])])
    c.execute("DELETE FROM doc WHERE id = ?", (rid,))


def _index_one(st, c, pid, p, meta, sm, se, pe, sp) -> int:
    """Index one paper's parts whose fingerprints changed. Returns the bytes of text written."""
    size = 0
    cur = c.execute("SELECT * FROM doc WHERE pid = ?", (pid,)).fetchone()
    rid = cur["id"] if cur is not None else c.execute(
        "INSERT INTO doc(pid, sig_m, sig_e, sig_p) VALUES (?, '', '', '')", (pid,)).lastrowid
    if cur is None or cur["sig_m"] != sm:
        short = [meta[k] for k in META_FIELDS]
        was = [cur[k] or "" for k in META_FIELDS] if cur is not None else None
        _fts(c, "m", META_FIELDS, rid, short, was)
        _fts(c, "t", TRI_FIELDS, rid, _folded(short), _folded(was) if was is not None else None)
        c.execute(f"UPDATE doc SET sig_m = ?, {', '.join(f'{k} = ?' for k in META_FIELDS)} WHERE id = ?", [sm] + short + [rid])
    if cur is None or cur["sig_e"] != se:
        vals = _ep_fields(st, p["eids"])
        was = [_unz(cur[k]) for k in EP_FIELDS] if cur is not None else None
        _fts(c, "e", EP_FIELDS, rid, vals, was)
        c.execute("UPDATE doc SET sig_e = ?, claims = ?, explainer = ?, script = ? WHERE id = ?", [se] + [_z(v) for v in vals] + [rid])
        size += sum(len(v) for v in vals)
    if cur is None or cur["sig_p"] != sp:
        text = _clean_paper(_read(st.episodes / pe / "paper.txt", PAPER_TEXT_MAX)) if pe else ""
        _fts(c, "p", ("paper",), rid, [text], [_unz(cur["paper"])] if cur is not None else None)
        c.execute("UPDATE doc SET sig_p = ?, paper = ?, head = ? WHERE id = ?", (sp, _z(text), _z(text[:HEAD]), rid))
        size += len(text)
    return size


def _fuzzy_refresh(st: _State, c) -> None:
    """fz_word and fz_del from the index's own vocabulary: words of letters only, 3 to 24 of
    them, that a title, tag, name, graph or episode uses, or that two papers' texts use (one
    paper's oddity, a PDF's broken word, is no correction to offer anyone)."""
    df: dict = {}
    for table, least in (("mv", 1), ("ev", 1), ("pv", 2)):
        for term, doc in c.execute(f"SELECT term, doc FROM {table}"):
            if doc >= least and 3 <= len(term) <= 24 and term.isalpha() and doc > df.get(term, 0):
                df[term] = doc
    old = {r["word"]: (r["id"], r["df"]) for r in c.execute("SELECT id, word, df FROM fz_word")}
    c.execute("BEGIN IMMEDIATE")
    try:
        for w, (wid, _) in old.items():
            if w not in df:
                c.executemany("DELETE FROM fz_del WHERE key = ? AND wid = ?", [(k, wid) for k in _deletes(w, _dmax(w))])
                c.execute("DELETE FROM fz_word WHERE id = ?", (wid,))
        for w, k in df.items():
            o = old.get(w)
            if o is None:
                wid = c.execute("INSERT INTO fz_word(word, df) VALUES (?, ?)", (w, k)).lastrowid
                c.executemany("INSERT OR IGNORE INTO fz_del(key, wid) VALUES (?, ?)", [(x, wid) for x in _deletes(w, _dmax(w))])
            elif o[1] != k:
                c.execute("UPDATE fz_word SET df = ? WHERE id = ?", (k, o[0]))
        c.execute("COMMIT")
    except BaseException:
        c.execute("ROLLBACK")
        raise
    st.fz_dirty = False


def _dmax(w: str) -> int:
    """Deletions kept for a word: 2 from 8 letters (a query word of 8 or more may be two edits
    from it at the same length), else 1 (a query word 2 edits away is then longer than it, so
    at least one of its edits is an insertion, which a deletion on the query's side undoes)."""
    return 2 if len(w) >= 8 else 1


def _deletes(w: str, d: int) -> set:
    out, cur = {w}, {w}
    for _ in range(d):
        cur = {s[:i] + s[i + 1:] for s in cur for i in range(len(s))}
        out |= cur
    return out


def _osa(a: str, b: str, cap: int) -> int:
    """Edit distance counting a swap of two neighbours as one edit (optimal string alignment);
    anything over `cap` comes back as cap + 1."""
    if abs(len(a) - len(b)) > cap:
        return cap + 1
    prev2, prev = None, list(range(len(b) + 1))
    for i in range(1, len(a) + 1):
        cur = [i] + [0] * len(b)
        for j in range(1, len(b) + 1):
            v = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (a[i - 1] != b[j - 1]))
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                v = min(v, prev2[j - 2] + 1)
            cur[j] = v
        if min(cur) > cap:
            return cap + 1
        prev2, prev = prev, cur
    return prev[-1]


# ---------------------------------------------------------------- the background worker

def start(cfg) -> None:
    """At the hub's start (app.serve): the index is brought up to date in a background thread
    (all of it when search.db is missing), then kept in step on the hub's events."""
    st = _state(cfg)
    if st.thread is not None and st.thread.is_alive():
        return
    st.thread = threading.Thread(target=_worker, args=(cfg, st), name="search-index", daemon=True)
    st.thread.start()


def _serving(st: _State) -> bool:
    """This process still serves the data dir the worker was started for (tests make many)."""
    try:
        return db._path is not None and Path(db._path).resolve() == st.hub.resolve() and st.hub.parent.is_dir()
    except OSError:
        return False


def _worker(cfg, st: _State) -> None:
    try:
        os.setpriority(os.PRIO_PROCESS, threading.get_native_id(), 10)    # the machine is shared
    except (AttributeError, OSError):
        pass
    sub = events.subscribe("search")
    try:
        first = True
        while _serving(st):
            try:
                due = first or time.monotonic() - st.full_at > SYNC_EVERY_S
                if due or st.fz_dirty or st.synced != _signature(db.conn()):
                    sync(cfg, full=due, fuzzy=True)
            except sqlite3.Error as e:
                if not _serving(st):
                    return
                log.warning("search index: %s", e)
            except Exception:
                log.exception("search index")
            first = False
            try:                  # an event (an upload, a delete, a graph edit) wakes it; a burst is one sync
                sub.q.get(timeout=5.0)
                time.sleep(0.3)
                while True:
                    sub.q.get_nowait()
            except queue.Empty:
                pass
    finally:
        events.unsubscribe(sub)
        with st.pool_lock:
            pool, st.pool = st.pool, []
        for c in pool:
            c.close()


def idle(cfg, timeout: float = 30.0) -> bool:
    """Wait until the index and its vocabulary are up to date (tests, tools)."""
    st = _state(cfg)
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if st.synced == _signature(db.conn()) and not st.fz_dirty and not st.lock.locked():
            return True
        time.sleep(0.05)
    return False


def _fresh(cfg):
    """Before a query: hub.db changed since the last sync, so sync now (an upload of a moment
    ago is found at once), unless the background is busy (the first build): then the index is
    used as it stands and what it lacks is matched the plain way."""
    st = _state(cfg)
    sig = _signature(db.conn())
    worker = st.thread is not None and st.thread.is_alive()
    if st.synced != sig and (st.synced is not None or not worker) and st.lock.acquire(timeout=0.5):
        try:
            _sync(st, False, False)
        finally:
            st.lock.release()
    return st, sig


# ---------------------------------------------------------------- queries

class Clause:
    """One thing a paper must match: a word (maybe a prefix, maybe corrected) or a phrase."""
    __slots__ = ("raw", "toks", "prefix", "phrase", "fixed_from", "sub")

    def __init__(self, raw: str, toks: list, prefix: bool, phrase: bool):
        self.raw, self.toks, self.prefix, self.phrase = raw, toks, prefix, phrase
        self.fixed_from = None
        s = " ".join(fold(raw).split()).strip(".,;:!?()[]{}'\"")
        # a substring for the trigram table: 4 letters or more ("ppo" is in every Filippo)
        self.sub = s if len(s) >= 4 else None

    def pre(self) -> bool:
        return self.prefix and len(self.toks[-1]) >= 2

    def fts(self, table: str):
        """The clause as an FTS5 string for a words table; None when it cannot match there."""
        if table == "p" and self.pre() and len(self.toks[-1]) < 4:
            return None           # a two- or three-letter prefix would merge thousands of words
        return '"' + " ".join(self.toks) + '"' + ("*" if self.pre() else "")

    def tri(self):
        return '"' + self.sub.replace('"', '""') + '"' if self.sub and not self.fixed_from else None


def parse(q: str) -> list:
    """Words and "phrases"; the last word is a prefix unless the query ends with a space (an
    unclosed phrase at the end is being typed too). Little words ("the", "of") are dropped when
    there is anything else."""
    q = (q or "")[:200]
    typing = bool(q) and not q[-1].isspace()
    out = []
    for m in re.finditer(r'"([^"]*)("|$)|([^\s"]+)', q):
        if m.group(3) is not None:
            raw, phrase, closed = m.group(3), False, False
        else:
            raw, phrase, closed = m.group(1), True, m.group(2) == '"'
        toks = tokens(raw)
        if toks:
            last = m.end() == len(q)
            out.append(Clause(raw.strip(), toks, typing and last and not closed, phrase or len(toks) > 1))
    small = [c for c in out if not c.phrase and not c.prefix and c.toks[0] in STOP]
    if small and len(small) < len(out):
        out = [c for c in out if c not in small]
    return out[:MAX_CLAUSES]


def _rows(c, table: str, match: str) -> set:
    try:
        return {r[0] for r in c.execute(f"SELECT rowid FROM {table} WHERE {table} MATCH ?", (match,))}
    except sqlite3.OperationalError:            # what FTS5 cannot parse matches nothing
        return set()


def _scores(c, table: str, parts: list, weights) -> dict:
    if not parts:
        return {}
    w = "".join(f", {x}" for x in weights) if weights else ""
    try:
        return {r[0]: -r[1] for r in c.execute(f"SELECT rowid, bm25({table}{w}) FROM {table} WHERE {table} MATCH ?",
                                               (" OR ".join(parts),))}
    except sqlite3.OperationalError:
        return {}


def _df(c, word: str) -> int:
    n = 0
    for t in ("mv", "ev", "pv"):
        r = c.execute(f"SELECT doc FROM {t} WHERE term = ?", (word,)).fetchone()
        n = max(n, r[0] if r else 0)
    return n


def _known_prefix(c, word: str) -> bool:
    """Some word of the correction vocabulary starts like this (or, before it is made, some
    word of the index)."""
    hi = word[:-1] + chr(ord(word[-1]) + 1)
    if c.execute("SELECT 1 FROM fz_word LIMIT 1").fetchone():
        return c.execute("SELECT 1 FROM fz_word WHERE word >= ? AND word < ? LIMIT 1", (word, hi)).fetchone() is not None
    return any(c.execute(f"SELECT 1 FROM {t} WHERE term >= ? AND term < ? LIMIT 1", (word, hi)).fetchone()
               for t in ("mv", "ev", "pv"))


def _closest(c, word: str):
    """(word, papers) of the closest word in the correction vocabulary, or None: the fewest
    edits, then the most papers."""
    d = 1 if len(word) <= 7 else 2
    keys = list(_deletes(word, d))
    got = {}
    for i in range(0, len(keys), 400):
        chunk = keys[i:i + 400]
        for r in c.execute(f"SELECT w.word, w.df FROM fz_del d JOIN fz_word w ON w.id = d.wid "
                           f"WHERE d.key IN ({','.join('?' * len(chunk))})", chunk):
            got[r[0]] = r[1]
    best = None
    for w, n in got.items():
        if w != word:
            k = (_osa(word, w, d), -n, w)
            if k[0] <= d and (best is None or k < best):
                best = k
    return (best[2], -best[1]) if best else None


def correct(c, clauses: list) -> bool:
    """Words the index does not know become the closest word it does: single words of 4 or
    more letters, outside quotes. A word it knows stays as typed, unless only one paper's text
    has it and a close word is in ten times as many papers (a typo in a PDF)."""
    fixed = False
    for cl in clauses:
        if cl.phrase or len(cl.toks) != 1:
            continue
        w = cl.toks[0]
        if not 4 <= len(w) <= 26 or not w.isalpha():
            continue              # the vocabulary's words have at most 24 letters
        if c.execute("SELECT 1 FROM fz_word WHERE word = ?", (w,)).fetchone():
            continue              # a word the library uses
        if cl.prefix and _known_prefix(c, w):
            continue              # still being typed, and the library's words start like this
        if cl.tri() and _rows(c, "t", cl.tri()):
            continue              # inside a longer word of a title or a name: "bert" in DistilBERT
        n = _df(c, w)             # known, but maybe only as one PDF's typo
        got = _closest(c, w)
        if got is None or (n and got[1] < max(3, 10 * n)):
            continue
        cl.fixed_from, cl.toks, cl.prefix = w, [got[0]], False
        fixed = True
    return fixed


def _used(q: str, clauses: list) -> str:
    """The query as searched: each corrected word in place of the one typed."""
    out = q.strip()
    for cl in clauses:
        if cl.fixed_from:
            new, n = re.subn(r"(?<![^\W_])" + re.escape(cl.fixed_from) + r"(?![^\W_])", cl.toks[0], out, count=1, flags=re.I)
            out = new if n else out
    return out


class _Rx:
    """Where a clause matches in folded text, as the index would: its words, whole (the last
    one begun, for a prefix), one after another; in the short fields also inside a word (the
    trigram table). The pattern starts with the word itself, so re finds it with its fast
    literal search; that the word starts a word is checked here (a lookbehind would make re
    try every position of a long paper text)."""
    __slots__ = ("rx", "sub")

    def __init__(self, cl: Clause, short: bool):
        body = r"[\W_]+".join(re.escape(t) for t in cl.toks)
        self.rx = re.compile(body + (r"[^\W_]*" if cl.pre() else r"(?![^\W_])"))     # a prefix marks its whole word
        self.sub = re.compile(re.escape(cl.sub)) if short and cl.tri() else None

    def finditer(self, f: str):
        for m in self.rx.finditer(f):
            if m.start() == 0 or not f[m.start() - 1].isalnum():
                yield m
        if self.sub is not None:
            yield from self.sub.finditer(f)

    def search(self, f: str):
        return next(self.finditer(f), None)


def _matcher(cl: Clause, short: bool) -> _Rx:
    return _Rx(cl, short)


def _parts(text: str, rxs: list) -> list:
    """[plain, marked, plain, ...]: the text with every match marked, or [] when none is (the
    page makes text nodes and <mark> elements of them, never HTML)."""
    if not text:
        return []
    f = fold(text)
    spans = sorted((m.start(), m.end()) for rx in rxs for m in rx.finditer(f) if m.end() > m.start())
    merged = []
    for s, e in spans:
        if merged and s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    if not merged:
        return []
    out, at = [], 0
    for s, e in merged:
        out += [text[at:s], text[s:e]]
        at = e
    out.append(text[at:])
    return out


def _hits(f: str, rxs: list) -> list:
    """(offset, clause) of each clause's first match, or of its first few when there are
    several clauses (the snippet looks for where they meet)."""
    hits = []
    for i, rx in enumerate(rxs):
        for k, m in enumerate(rx.finditer(f)):
            hits.append((m.start(), i))
            if len(rxs) == 1 or k >= 12:
                break
    return hits


def _window(text: str, rxs: list, low: str | None = None, width: int = 200, before: int = 24):
    """A short stretch of the text where most of the clauses match, as parts, "…" at a cut.
    Looked for in the text lowercased (one pass in C, offsets kept); accents folded too only
    when that finds nothing (a paper's text is full of symbols, and folding it all is slow)."""
    many = len(rxs) > 1
    head = text[:200_000] if many else text
    if low is None:
        low = head.lower()
        low = low if len(low) == len(head) else None
    else:
        low = low[:len(head)]
    hits = _hits(low, rxs) if low is not None else []
    if not hits:
        hits = _hits(fold(head), rxs)
    if not hits and len(text) > len(head):
        head = text
        hits = [(m.start(), i) for i, rx in enumerate(rxs) for m in [rx.search(fold(text))] if m]
    if not hits:
        return None
    hits.sort()
    reach = width - before - 30
    s = max(hits, key=lambda h: (len({i for x, i in hits if h[0] <= x <= h[0] + reach}), -h[0]))[0] if many else hits[0][0]
    a = max(0, s - before)
    if a:
        sp = head.find(" ", a, s)
        a = sp + 1 if sp >= 0 else a
    b = min(len(head), a + width)
    if b < len(head):
        sp = head.rfind(" ", s + 1, b)
        b = sp if sp > s else b
    parts = _parts(" ".join(head[a:b].split()), rxs)
    if not parts:
        return None
    if a > 0:
        parts[0] = "…" + parts[0]
    if b < len(text):
        parts[-1] += "…"
    return parts


LEADS = {"title": "in the title", "label": "in the map label: ", "authors": "in the authors: ",
         "tags": "in the tags: ", "makers": "made by ", "episode": "in the episode: ", "paper": "in the paper: "}


class _Q:
    """One query, ready to run: its clauses, their regexes, and which rows each one matches."""

    def __init__(self, sc, clauses: list):
        self.clauses = clauses
        self.short_rx = [_matcher(cl, True) for cl in clauses]
        self.word_rx = [_matcher(cl, False) for cl in clauses]
        self.sets = []
        for cl in clauses:
            s = {t: (_rows(sc, t, cl.fts(t)) if cl.fts(t) else set()) for t in ("m", "e", "p")}
            s["t"] = _rows(sc, "t", cl.tri()) if cl.tri() else set()
            s["all"] = s["m"] | s["e"] | s["p"] | s["t"]
            self.sets.append(s)

    def title(self, title: str) -> list:
        return _parts(title, self.short_rx)


def _covers(text, rxs) -> set:
    f = fold(text or "")
    return {i for i, rx in enumerate(rxs) if rx.search(f)}


_texts: "OrderedDict" = None
_texts_lock = threading.Lock()
TEXT_CACHE = 48 * 1024 * 1024


def _text(st: _State, sc, rid: int, col: str):
    """(text, text.lower() or None) of one stored part, from a small cache: typing a word asks
    for the same few papers' texts at every key."""
    global _texts
    r = sc.execute("SELECT pid, sig_e, sig_p FROM doc WHERE id = ?", (rid,)).fetchone()
    if r is None:
        return "", None
    key = (str(st.path), r["pid"], col, r["sig_p"] if col in ("head", "paper") else r["sig_e"])
    with _texts_lock:
        if _texts is None:
            from collections import OrderedDict
            _texts = OrderedDict()
        hit = _texts.get(key)
        if hit is not None:
            _texts.move_to_end(key)
            return hit
    b = sc.execute(f"SELECT {col} FROM doc WHERE id = ?", (rid,)).fetchone()
    text = _unz(b[0]) if b is not None and b[0] else ""
    low = text.lower()
    hit = (text, low if len(low) == len(text) else None)
    with _texts_lock:
        _texts[key] = hit
        size = sum(len(v[0]) for v in _texts.values())
        while size > TEXT_CACHE and len(_texts) > 1:
            _, old = _texts.popitem(last=False)
            size -= len(old[0])
    return hit


def _match_info(cat: Catalog, st, sc, rid, pid: str, qq: _Q, uid) -> dict:
    """Where one paper matched and a snippet of it, for its row: {"where", "lead", "parts",
    "title"} (title: the title as parts, for marking it in the row). The part that holds the
    most of the query wins; among equals the first of title, label, authors, tags, graphs,
    makers, ids, the episode, the paper's text."""
    p = cat.papers[pid]
    out = {"where": None, "lead": "", "parts": [], "title": qq.title(p["title"])}
    label = p["label"] if p["label"] != p["title"] else ""
    gl = [cat.graphs[g] for g in p["graphs"] if g in cat.graphs]
    ids = [(n, v) for n, v in (("the year", str(p["year"] or "")), ("the arXiv id", p["arxiv_id"] or ""),
                               ("the DOI", p["doi"] or "")) if v]
    # which clauses the short fields hold, by the tables' answer (no regex when none does);
    # titles, labels, tags and graph names also inside a word, names and ids by whole words
    in_short = {i for i, s in enumerate(qq.sets) if rid in s["m"] or rid in s["t"]}

    def cov(vals, rxs):
        got = set()
        for v in vals:
            if v:
                f = fold(v)
                got |= {i for i in in_short if rxs[i].search(f)}
        return got

    sub, word = qq.short_rx, qq.word_rx
    none = set()
    cands = [("title", cov([p["title"]], sub) if in_short else none), ("label", cov([label], sub) if in_short else none),
             ("authors", cov(p["authors"], word) if in_short else none), ("tags", cov(p["tags"], sub) if in_short else none),
             ("graphs", cov([g["name"] for g in gl], sub) if in_short else none),
             ("makers", cov(p["makers"], word) if in_short else none), ("ids", cov([v for _, v in ids], word) if in_short else none),
             ("episode", {i for i, s in enumerate(qq.sets) if rid in s["e"]}),
             ("paper", {i for i, s in enumerate(qq.sets) if rid in s["p"]})]
    kind, got = max(enumerate(cands), key=lambda x: (len(x[1][1]), -x[0]))[1]
    if not got:
        return out
    rx = [(word if kind in ("authors", "makers", "ids", "episode", "paper") else sub)[i] for i in sorted(got)]
    if kind == "title":
        out.update(where="title", lead=LEADS["title"])
    elif kind == "label":
        out.update(where="label", lead=LEADS["label"], parts=_parts(label, rx))
    elif kind in ("authors", "tags", "makers"):
        hit = [v for v in p[kind] if _covers(v, rx)][:3]
        out.update(where=kind, lead=LEADS[kind], parts=_parts((" · " if kind == "tags" else ", ").join(hit), rx))
    elif kind == "graphs":
        g = next(g for g in gl if _covers(g["name"], rx))
        who = "your" if uid is not None and g["creator_id"] == uid else f"{g['creator']}’s" if g["creator"] else "the"
        out.update(where="graph", lead=f"in {who} graph ", parts=_parts(g["name"], rx), graph=g["id"])
    elif kind == "ids":
        name, v = next((n, v) for n, v in ids if _covers(v, rx))
        out.update(where="ids", lead=f"in {name}: ", parts=_parts(v, rx))
    else:
        # the paper text's head first: a snippet there costs a twentieth of the whole text
        cols = EP_FIELDS if kind == "episode" else ("head", "paper")
        out.update(where=kind, lead=LEADS[kind].rstrip(": "))
        for col in cols:
            text, low = _text(st, sc, rid, col)
            parts = _window(text, rx, low) if text else None
            if parts and col == "head" and not parts[-1].endswith("…") and len(text) >= HEAD:
                parts[-1] += "…"          # the head's end is not the text's
            if parts:
                out.update(where="paper" if kind == "paper" else col, lead=LEADS[kind], parts=parts)
                break
    return out


def _listened(uid) -> set:
    if uid is None:
        return set()
    return {r[0] for r in db.conn().execute("SELECT paper_id FROM listened WHERE user_id = ?", (uid,))}


def filters_from(query: dict) -> dict:
    """The filters of GET /api/library, cleaned: graph (a graph id), tag, maker (a user id),
    year_from, year_to, listened (yes | no; anything else: all)."""
    f = {}
    g = (query.get("graph") or "").strip()
    if g:
        f["graph"] = g[:40]
    t = " ".join((query.get("tag") or "").split())
    if t:
        f["tag"] = t[:80]
    for k in ("maker", "year_from", "year_to"):
        v = (query.get(k) or "").strip()
        if v:
            try:
                f[k] = int(v)
            except ValueError:
                raise HTTPError(400, "bad_filter", f"{k} must be a whole number") from None
    li = (query.get("listened") or "").strip().lower()
    if li in ("yes", "1", "true", "listened"):
        f["listened"] = True
    elif li in ("no", "0", "false", "not"):
        f["listened"] = False
    return f


def _filtered(cat: Catalog, f: dict, uid) -> set:
    ids = set(cat.papers)
    P = cat.papers
    if "graph" in f:
        g = cat.graphs.get(f["graph"])
        ids &= g["members"] if g else set()
    if "tag" in f:
        t = f["tag"].lower()
        ids = {i for i in ids if any(x.strip().lower() == t for x in P[i]["tags"])}
    if "maker" in f:
        ids = {i for i in ids if f["maker"] in P[i]["maker_ids"]}
    if "year_from" in f:
        ids = {i for i in ids if P[i]["year"] is not None and P[i]["year"] >= f["year_from"]}
    if "year_to" in f:
        ids = {i for i in ids if P[i]["year"] is not None and P[i]["year"] <= f["year_to"]}
    if "listened" in f:
        mine = _listened(uid)
        ids = {i for i in ids if (i in mine) == f["listened"]}
    return ids


def _newest(cat: Catalog, ids) -> list:
    return sorted(ids, key=lambda i: (cat.papers[i]["added"] or "", i), reverse=True)


def _plain_match(p: dict, clauses: list) -> bool:
    """For a paper the first build has not reached yet: its title, people, tags and ids, as
    the search before the index."""
    hay = fold(" ".join([p["title"], " ".join(p["authors"]), " ".join(p["tags"]), " ".join(p["makers"]),
                         p["arxiv_id"] or "", p["doi"] or "", str(p["year"] or "")]))
    return all(" ".join(cl.toks) in hay for cl in clauses)


def run(cfg, uid, q: str = "", f: dict | None = None, pids: list | None = None, snippets: int = SNIPPETS) -> dict:
    """The papers matching q and the filters: {"ids": [best first; newest first without q],
    "match": {pid: where it matched, a snippet}, "info": {"q", "n", "ms", "used"?, "fixes"?,
    "indexing"?}}. `pids`: where these papers match (their snippets), matching or not."""
    t0 = time.perf_counter()
    f = f or {}
    st, sig = _fresh(cfg)
    cat = catalog(db.conn(), sig)
    clauses = parse(q)
    info = {"q": q, "n": 0}
    if st.thread is not None and st.synced is None:
        info["indexing"] = {"done": st.total - st.pending, "total": st.total or len(cat.papers)}
    if pids is not None:
        pids = [i for i in pids if i in cat.papers][:MAX_IDS]
    if not clauses:
        ids = pids if pids is not None else _newest(cat, _filtered(cat, f, uid))
        info.update(n=len(ids), ms=round((time.perf_counter() - t0) * 1000, 1))
        return {"ids": ids, "match": {}, "info": info}
    with _conn(st) as sc:
        return _run(cat, sc, st, uid, q, f, pids, snippets, clauses, info, t0)


def _run(cat, sc, st, uid, q, f, pids, snippets, clauses, info, t0) -> dict:
    if correct(sc, clauses):
        info["used"] = _used(q, clauses)
        info["fixes"] = [[cl.fixed_from, cl.toks[0]] for cl in clauses if cl.fixed_from]
    qq = _Q(sc, clauses)
    rid_of = {r[1]: r[0] for r in sc.execute("SELECT id, pid FROM doc")}
    if pids is not None:
        match = {i: _match_info(cat, st, sc, rid_of[i], i, qq, uid) for i in pids if i in rid_of}
        info.update(n=len(pids), ms=round((time.perf_counter() - t0) * 1000, 1))
        return {"ids": pids, "match": match, "info": info}
    base = _filtered(cat, f, uid)
    hit = set.intersection(*[s["all"] for s in qq.sets])
    ranked = [pid for pid, rid in rid_of.items() if rid in hit and pid in base]
    score: dict = {}
    for table, weights in (("m", M_W), ("e", E_W), ("p", None), ("t", T_W)):
        parts = [x for x in ((cl.tri() if table == "t" else cl.fts(table)) for cl in clauses) if x]
        if any(hit & s[table] for s in qq.sets):
            for rid, s in _scores(sc, table, parts, weights).items():
                score[rid] = score.get(rid, 0.0) + TABLE_W[table] * s
    y0, y1 = cat.years
    span = max(1, (y1 or 0) - (y0 or 0))

    def rank(pid):
        y = cat.papers[pid]["year"]
        s = score.get(rid_of[pid], 0.0) * (1 + (RECENT * (y - y0) / span if y is not None and y0 is not None else 0))
        return (-round(s, 9), -(y or 0))

    ids = sorted(_newest(cat, ranked), key=rank)            # stable: newest first among equals
    # papers the first build has not reached yet: matched as before the index, after the rest
    ids += [i for i in _newest(cat, base - set(rid_of)) if _plain_match(cat.papers[i], clauses)]
    match = {}
    for n, pid in enumerate(ids):
        if n < snippets and pid in rid_of:
            match[pid] = _match_info(cat, st, sc, rid_of[pid], pid, qq, uid)
        else:                     # the title marked now; the page asks for the snippet when it shows the row
            match[pid] = {"where": None, "lead": "", "parts": [], "title": qq.title(cat.papers[pid]["title"]),
                          "more": pid in rid_of}
    info.update(n=len(ids), ms=round((time.perf_counter() - t0) * 1000, 1))
    return {"ids": ids, "match": match, "info": info}


def facets(uid=None) -> dict:
    """What the filters choose from: tags and people with how many papers each, the years."""
    cat = catalog(db.conn())
    tags, makers = {}, {}
    for p in cat.papers.values():
        for t in {x.strip() for x in p["tags"]}:
            tags[t] = tags.get(t, 0) + 1
        for m in p["maker_ids"]:
            makers[m] = makers.get(m, 0) + 1
    users = {r[0]: r[1] for r in db.conn().execute("SELECT id, name FROM users")}
    return {"tags": [{"tag": t, "n": n} for t, n in sorted(tags.items(), key=lambda x: (-x[1], x[0].lower()))],
            "makers": [{"id": m, "name": users.get(m) or "someone", "n": n, "me": m == uid}
                       for m, n in sorted(makers.items(), key=lambda x: ((users.get(x[0]) or "").lower(), x[0]))],
            "years": {"min": cat.years[0], "max": cat.years[1]}}


def status(cfg) -> dict:
    st = _state(cfg)
    with _conn(st) as c:
        n = c.execute("SELECT count(*) FROM doc").fetchone()[0]
        words = c.execute("SELECT count(*) FROM fz_word").fetchone()[0]
    return {"papers": n, "pending": st.pending, "words": words,
            "bytes": sum(Path(str(st.path) + x).stat().st_size for x in ("", "-wal") if Path(str(st.path) + x).exists()),
            "current": st.synced == _signature(db.conn()) and not st.fz_dirty}


def get_facets(req):
    req.send_json(200, facets(req.user["id"] if req.user else None))


def get_status(req):
    req.send_json(200, status(req.cfg))


ROUTES = [
    ("GET", r"^/api/search/facets$", get_facets, "viewer"),
    ("GET", r"^/api/search/status$", get_status, "viewer"),
]
