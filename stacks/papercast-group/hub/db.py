"""The hub's database: SQLite at $PCG_DATA/hub.db (SPEC.md section 3).

The schema below is the contract. A1 owns this file: migrations, indexes, and the helpers named
in SPEC.md section 3. Columns may be added, never renamed. JSON columns hold text.

SCHEMA is version 1 and stays as it is; every later change is a numbered migration after it
(MIGRATIONS), so a database made by any earlier version reaches the same shape as a new one.
meta.schema_version says which migrations have run.

    python3 -m hub.db migrate [--data DIR]     migrations, then the seeds (seed_defaults)
    python3 -m hub.db version [--data DIR]     the database's schema version

app.serve() calls migrate() only; the seeds (base prompt v1, wording, the five topic graphs) come
from `python3 -m hub.db migrate`, which the install runs, so a test's empty database stays empty."""
from __future__ import annotations

import argparse
import base64
import hashlib
import importlib.util
import json
import re
import secrets
import sqlite3
import sys
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY,
  email TEXT NOT NULL UNIQUE,
  name TEXT NOT NULL,
  role TEXT NOT NULL CHECK (role IN ('viewer', 'contributor', 'admin')),
  disabled INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS invites (          -- local auth: one-use join links
  token_hash TEXT PRIMARY KEY,
  role TEXT NOT NULL DEFAULT 'viewer',
  created_by INTEGER REFERENCES users(id),
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  used_by INTEGER REFERENCES users(id),
  used_at TEXT
);

CREATE TABLE IF NOT EXISTS cli_logins (       -- the CLI's device login (SPEC.md section 2)
  code TEXT PRIMARY KEY,
  poll_hash TEXT NOT NULL UNIQUE,
  device TEXT NOT NULL,
  state TEXT NOT NULL CHECK (state IN ('pending', 'approved', 'denied', 'taken')),
  user_id INTEGER REFERENCES users(id),
  token_plain TEXT,                           -- held only between approve and the next poll
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tokens (
  id INTEGER PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id),
  name TEXT NOT NULL,
  hash TEXT NOT NULL UNIQUE,                  -- sha256 of the plaintext
  created_at TEXT NOT NULL,
  last_used_at TEXT,
  revoked_at TEXT
);

CREATE TABLE IF NOT EXISTS papers (
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  title_norm TEXT NOT NULL,
  authors TEXT NOT NULL DEFAULT '[]',
  year INTEGER,
  arxiv_id TEXT,
  doi TEXT,
  url TEXT,
  source_sha256 TEXT,
  s2_id TEXT,
  tags TEXT NOT NULL DEFAULT '[]',
  created_by INTEGER REFERENCES users(id),
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS papers_arxiv ON papers(arxiv_id);
CREATE INDEX IF NOT EXISTS papers_doi ON papers(doi);
CREATE INDEX IF NOT EXISTS papers_sha ON papers(source_sha256);
CREATE INDEX IF NOT EXISTS papers_title ON papers(title_norm);

CREATE TABLE IF NOT EXISTS claims (           -- a paper being made, so two people never make it at once
  id TEXT PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id),
  arxiv_id TEXT, doi TEXT, source_sha256 TEXT, title_norm TEXT,
  device TEXT,
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  done_at TEXT
);

CREATE TABLE IF NOT EXISTS episodes (
  id TEXT PRIMARY KEY,
  paper_id TEXT NOT NULL REFERENCES papers(id),
  made_by INTEGER NOT NULL REFERENCES users(id),
  state TEXT NOT NULL CHECK (state IN ('checking', 'rejected', 'waiting-for-gpu', 'speaking', 'ready', 'failed')),
  state_detail TEXT,
  base_version INTEGER,
  prefs TEXT NOT NULL DEFAULT '{}',
  prefs_summary TEXT NOT NULL DEFAULT '',
  client_version TEXT,
  model TEXT,
  words INTEGER,
  est_minutes REAL,
  duration_s REAL,
  check_report TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  deleted_at TEXT
);
CREATE INDEX IF NOT EXISTS episodes_paper ON episodes(paper_id);

CREATE TABLE IF NOT EXISTS listened (
  user_id INTEGER NOT NULL REFERENCES users(id),
  paper_id TEXT NOT NULL REFERENCES papers(id),
  at TEXT NOT NULL,
  PRIMARY KEY (user_id, paper_id)
);

CREATE TABLE IF NOT EXISTS positions (
  user_id INTEGER NOT NULL REFERENCES users(id),
  episode_id TEXT NOT NULL REFERENCES episodes(id),
  seconds REAL NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (user_id, episode_id)
);

CREATE TABLE IF NOT EXISTS prefs (
  user_id INTEGER PRIMARY KEY REFERENCES users(id),
  settings TEXT NOT NULL DEFAULT '{}',
  note TEXT NOT NULL DEFAULT '',
  version INTEGER NOT NULL DEFAULT 1,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS base_prompts (
  version INTEGER PRIMARY KEY,
  guideline TEXT NOT NULL,
  wording TEXT NOT NULL,
  created_by INTEGER REFERENCES users(id),
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS links (
  id INTEGER PRIMARY KEY,
  src TEXT NOT NULL REFERENCES papers(id),    -- the earlier paper
  dst TEXT NOT NULL REFERENCES papers(id),    -- the paper built on it
  grade TEXT NOT NULL CHECK (grade IN ('e', 's', 'w')),
  origin TEXT NOT NULL CHECK (origin IN ('agent', 'human')),
  state TEXT NOT NULL CHECK (state IN ('active', 'removed')),
  created_by INTEGER REFERENCES users(id),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE (src, dst)
);

CREATE TABLE IF NOT EXISTS graphs (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  rule_tags TEXT NOT NULL DEFAULT '[]',
  locked INTEGER NOT NULL DEFAULT 0,
  created_by INTEGER REFERENCES users(id),
  created_at TEXT NOT NULL,
  deleted_at TEXT
);

CREATE TABLE IF NOT EXISTS graph_members (    -- added to, or removed from, a graph's tag rule
  graph_id TEXT NOT NULL REFERENCES graphs(id),
  paper_id TEXT NOT NULL REFERENCES papers(id),
  how TEXT NOT NULL CHECK (how IN ('added', 'removed')),
  PRIMARY KEY (graph_id, paper_id)
);

CREATE TABLE IF NOT EXISTS graph_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  at TEXT NOT NULL,
  user_id INTEGER REFERENCES users(id),
  actor TEXT NOT NULL CHECK (actor IN ('human', 'agent')),
  op TEXT NOT NULL,
  target TEXT NOT NULL,                       -- link id, graph id, or graph id + paper id
  before TEXT,                                -- JSON, null for a create
  after TEXT,                                 -- JSON, null for a delete
  revert_of INTEGER REFERENCES graph_log(id),
  reverted_by INTEGER REFERENCES graph_log(id)
);

CREATE TABLE IF NOT EXISTS layout (
  graph_id TEXT NOT NULL REFERENCES graphs(id),
  paper_id TEXT NOT NULL REFERENCES papers(id),
  x REAL NOT NULL,
  y REAL NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (graph_id, paper_id)
);

CREATE TABLE IF NOT EXISTS voice_jobs (
  episode_id TEXT PRIMARY KEY REFERENCES episodes(id),
  user_id INTEGER NOT NULL REFERENCES users(id),
  state TEXT NOT NULL CHECK (state IN ('queued', 'claimed', 'done', 'failed')),
  queued_at TEXT NOT NULL,
  claimed_at TEXT,
  heartbeat_at TEXT,
  finished_at TEXT,
  phase TEXT,
  progress REAL,
  attempts INTEGER NOT NULL DEFAULT 0,
  error TEXT
);
"""

# ---- migrations: version n turns a version n-1 database into version n. Each runs once, in one
# transaction with the version bump, and is written so that running it again changes nothing.

def _columns(c, table: str) -> set:
    return {r[1] for r in c.execute(f"PRAGMA table_info({table})")}


def _add_column(c, table: str, column: str, decl: str) -> None:
    if column not in _columns(c, table):
        c.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def _m2_paper_label(c) -> None:
    """papers.label: the short name a node wears on the map ("Reverse-time SDE"). Leo's
    hand-made labels come in with the import; NULL means the map derives one from the title."""
    _add_column(c, "papers", "label", "TEXT")


def _m3_indexes(c) -> None:
    """Indexes for the lookups the primary keys do not cover (measured by tools/loadcheck.py):
    a paper's parents on the map (links by dst), someone's own episodes (cli `?mine=1`, delete
    rights), episodes by state (the voice queue, the checks), the voice queue's order, a user's
    devices, and an identity's open claim."""
    # one statement at a time: executescript() would commit the migration's transaction
    for sql in (
        "CREATE INDEX IF NOT EXISTS links_dst ON links(dst)",
        "CREATE INDEX IF NOT EXISTS episodes_made_by ON episodes(made_by)",
        "CREATE INDEX IF NOT EXISTS episodes_state ON episodes(state)",
        "CREATE INDEX IF NOT EXISTS voice_jobs_state ON voice_jobs(state, queued_at)",
        "CREATE INDEX IF NOT EXISTS tokens_user ON tokens(user_id)",
        "CREATE INDEX IF NOT EXISTS claims_arxiv ON claims(arxiv_id)",
        "CREATE INDEX IF NOT EXISTS claims_doi ON claims(doi)",
        "CREATE INDEX IF NOT EXISTS claims_sha ON claims(source_sha256)",
        "CREATE INDEX IF NOT EXISTS claims_title ON claims(title_norm)",
        "CREATE INDEX IF NOT EXISTS graph_log_user ON graph_log(user_id, id)",
    ):
        c.execute(sql)


MIGRATIONS = [
    (2, _m2_paper_label),
    (3, _m3_indexes),
]
SCHEMA_VERSION = MIGRATIONS[-1][0] if MIGRATIONS else 1      # what migrate() brings a database to

# ---- small helpers every module uses (A1 may extend, never change their meaning)

def now() -> str:
    """UTC, ISO 8601 with seconds and Z: 2026-09-28T04:12:09Z."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_id(prefix: str, n: int = 12) -> str:
    """"p_" + n lowercase base32 characters (papers 12, episodes 12, graphs 10, claims 12)."""
    return prefix + "".join(secrets.choice("abcdefghijklmnopqrstuvwxyz234567") for _ in range(n))


def norm_title(t: str) -> str:
    """Lowercase, every run of non-alphanumerics to one space, trimmed (paper identity)."""
    return re.sub(r"[^0-9a-z]+", " ", (t or "").lower()).strip()


def dumps(v) -> str:
    return json.dumps(v, ensure_ascii=False, separators=(",", ":"))


def loads(s, default=None):
    if s is None or s == "":
        return default
    try:
        return json.loads(s)
    except ValueError:
        return default


@contextmanager
def transaction():
    """BEGIN IMMEDIATE ... COMMIT on this thread's connection (ROLLBACK on an exception)."""
    c = conn()
    c.execute("BEGIN IMMEDIATE")
    try:
        yield c
    except BaseException:
        c.execute("ROLLBACK")
        raise
    c.execute("COMMIT")


_local = threading.local()
_path: Path | None = None


def init(cfg) -> None:
    global _path
    _path = cfg.data / "hub.db"


def conn() -> sqlite3.Connection:
    """This thread's connection (WAL, foreign keys on, rows as sqlite3.Row)."""
    c = getattr(_local, "c", None)
    if c is None or getattr(_local, "path", None) != _path:
        if _path is None:
            raise RuntimeError("db.init(cfg) first")
        c = sqlite3.connect(str(_path), timeout=30, isolation_level=None)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA foreign_keys=ON")
        c.execute("PRAGMA busy_timeout=30000")
        _local.c, _local.path = c, _path
    return c


def close() -> None:
    """Close this thread's connection (tests, tools that switch data dirs)."""
    c = getattr(_local, "c", None)
    if c is not None:
        c.close()
    _local.c = _local.path = None


def schema_version(c=None) -> int:
    c = c or conn()
    try:
        r = c.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    except sqlite3.OperationalError:          # no meta table: an empty file
        return 0
    return int(r[0]) if r else 0


_migrate_lock = threading.Lock()


def migrate() -> int:
    """Bring hub.db to SCHEMA_VERSION: the version 1 tables where missing, then each migration
    not yet run, in order. Safe to call at every start, from several processes at once (each
    step re-reads the version inside its own write transaction). Returns the version."""
    with _migrate_lock:
        c = conn()
        c.executescript(SCHEMA)                  # CREATE ... IF NOT EXISTS: a no-op once there
        c.execute("INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', '1')")
        v = schema_version(c)
        if v > SCHEMA_VERSION:
            raise RuntimeError(f"hub.db is at schema version {v}, newer than this code "
                               f"({SCHEMA_VERSION}): update the hub before starting it")
        for n, fn in MIGRATIONS:
            if n <= v:
                continue
            with transaction() as t:
                if schema_version(t) >= n:       # another process got there first
                    continue
                fn(t)
                t.execute("UPDATE meta SET value = ? WHERE key = 'schema_version'", (str(n),))
        return schema_version(c)


def meta_get(key: str, default=None):
    r = conn().execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return r[0] if r else default


def meta_set(key: str, value: str) -> None:
    conn().execute("INSERT INTO meta(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                   (key, value))


# ---- paper identity (SPEC.md section 3)

def find_paper(keys: dict):
    """The paper these keys name, or None. Tried in the SPEC's order: arXiv id without its
    version, DOI lowercased, the PDF's sha256, the normalised title; the first key that matches
    decides. Returns a sqlite3.Row."""
    c = conn()
    ax = re.sub(r"v\d+$", "", re.sub(r"^arxiv:", "", (keys.get("arxiv_id") or "").strip(), flags=re.I))
    doi = (keys.get("doi") or "").strip().lower()
    sha = (keys.get("source_sha256") or "").strip().lower()
    title = norm_title(keys.get("title") or "")
    for col, val in (("arxiv_id", ax), ("doi", doi), ("source_sha256", sha), ("title_norm", title)):
        if val:
            r = c.execute(f"SELECT * FROM papers WHERE {col} = ? ORDER BY created_at, id LIMIT 1", (val,)).fetchone()
            if r is not None:
                return r
    return None


# ---- seeds: what a new hub starts with (python3 -m hub.db migrate)

# Leo's five topics, with the tags each one gathers: the GROUPS of his lineage build
# (papercast-itest/lineage/build.py), the same lists as graphs[].tags in its lineage.json.
SEED_GRAPHS = [
    ("rl", "Reinforcement learning",
     ["reinforcement learning", "policy gradient", "value-based learning", "offline learning",
      "game playing", "sparse rewards", "multi-agent learning", "online learning",
      "policy iteration", "imitation learning"]),
    ("gen", "Diffusion and generative models",
     ["diffusion", "generative models", "text-to-image", "fast sampling", "video generation",
      "image generation", "flow matching", "image editing", "discrete diffusion", "guidance",
      "audio synthesis", "3d generation"]),
    ("mat", "Materials and molecules",
     ["materials discovery", "interatomic potentials", "crystal generation", "crystal structures",
      "materials synthesis", "chemical synthesis", "graph neural networks", "symmetry",
      "protein design"]),
    ("lm", "Language models", ["language models"]),
    ("robot", "Robotics and agents", ["robotic manipulation", "sim-to-real", "vision language agents"]),
]

GROUP_DIR = Path(__file__).resolve().parent.parent          # stacks/papercast-group
REPO_DIR = GROUP_DIR.parent.parent


def seed_graph_id(key: str) -> str:
    """A seed graph's id, the same on every hub ("rl" -> "g_" + 10 base32), so the import and
    the fake data can find it however it was renamed since."""
    h = hashlib.sha256(b"papercast-group seed graph " + key.encode()).digest()
    return "g_" + base64.b32encode(h).decode().lower()[:10]


def _common_dirs() -> list:
    """Where papercast_cli/common lives: the repo's copy, then an installed package."""
    out = [REPO_DIR / "packages" / "papercast-cli" / "papercast_cli" / "common"]
    try:
        spec = importlib.util.find_spec("papercast_cli")
    except (ImportError, ValueError):
        spec = None
    if spec is not None and spec.submodule_search_locations:
        out += [Path(p) / "common" for p in spec.submodule_search_locations]
    return out


def seed_sources() -> dict:
    """The files the seeds come from (a key is absent when no file was found): `guideline`
    = prompts/base-guideline.md, else common/base_guideline.md; `wording` = common/wording.json."""
    out = {}
    for p in [GROUP_DIR / "prompts" / "base-guideline.md"] + [d / "base_guideline.md" for d in _common_dirs()]:
        if p.is_file():
            out["guideline"] = p
            break
    for p in [d / "wording.json" for d in _common_dirs()]:
        if p.is_file():
            out["wording"] = p
            break
    return out


def seed_defaults(cfg=None) -> dict:
    """What a new hub starts with, added once and never again (idempotent):
    - base prompt version 1 (the guideline file and wording.json, see seed_sources()), when
      there is no base prompt yet and the guideline file exists; skipped otherwise, so a later
      run seeds it once the file lands;
    - the five topic graphs (SEED_GRAPHS), once: meta `seeded.graphs` remembers, so a seed
      graph an admin deleted stays deleted.
    Returns what it added: {"base_prompt": version | None, "graphs": [ids]}."""
    if cfg is not None and _path != Path(cfg.data) / "hub.db":
        init(cfg)
    migrate()
    src = seed_sources()
    out = {"base_prompt": None, "graphs": []}
    with transaction() as c:
        have = c.execute("SELECT COUNT(*) FROM base_prompts").fetchone()[0]
        if not have and "guideline" in src:
            text = src["guideline"].read_text(encoding="utf-8")
            wording = json.loads(src["wording"].read_text(encoding="utf-8")) if "wording" in src else {}
            c.execute("INSERT INTO base_prompts(version, guideline, wording, created_by, created_at) VALUES (1, ?, ?, NULL, ?)",
                      (text, dumps(wording), now()))
            out["base_prompt"] = 1
        if c.execute("SELECT 1 FROM meta WHERE key = 'seeded.graphs'").fetchone() is None:
            t = now()
            for key, name, tags in SEED_GRAPHS:
                gid = seed_graph_id(key)
                cur = c.execute("INSERT OR IGNORE INTO graphs(id, name, rule_tags, locked, created_by, created_at) "
                                "VALUES (?, ?, ?, 0, NULL, ?)", (gid, name, dumps(tags), t))
                if cur.rowcount:
                    out["graphs"].append(gid)
            c.execute("INSERT INTO meta(key, value) VALUES ('seeded.graphs', ?)", (t,))
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python3 -m hub.db", description="The hub's database.")
    ap.add_argument("command", choices=["migrate", "version"])
    ap.add_argument("--data", help="the data dir (default: $PCG_DATA, as the hub reads it)")
    a = ap.parse_args(argv)
    from . import config as C
    if a.data:
        cfg = C.Config(data=Path(a.data))
    else:
        import os
        cfg = C.Config(data=Path(os.environ.get("PCG_DATA", str(Path.home() / "papercast-group" / "data"))))
    if a.command == "version":
        if not (cfg.data / "hub.db").exists():
            print(f"{cfg.data / 'hub.db'}: no database")
            return 1
        init(cfg)
        print(schema_version())
        return 0
    cfg.data.mkdir(parents=True, exist_ok=True)
    (cfg.data / "episodes").mkdir(exist_ok=True)
    init(cfg)
    before = schema_version()
    v = migrate()
    seeded = seed_defaults(cfg)
    src = seed_sources()
    print(f"{cfg.data / 'hub.db'}: schema version {before} -> {v}")
    if seeded["base_prompt"]:
        print(f"seeded base prompt v1 from {src['guideline']}" + (f" and {src['wording']}" if "wording" in src else " (no wording.json found: wording {})"))
    elif not conn().execute("SELECT 1 FROM base_prompts LIMIT 1").fetchone():
        print("no base prompt yet: neither prompts/base-guideline.md nor common/base_guideline.md exists")
    if seeded["graphs"]:
        print(f"seeded {len(seeded['graphs'])} topic graphs")
    return 0


if __name__ == "__main__":
    # run as the package's module, not as a second copy called __main__ with its own connection
    from hub import db as _db
    sys.exit(_db.main())
