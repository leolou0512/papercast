"""The hub's database: SQLite at $PCG_DATA/hub.db (SPEC.md section 3).

The schema below is the contract. A1 owns this file: migrations, indexes, and the helpers named
in SPEC.md section 3. Columns may be added, never renamed. JSON columns hold text."""
from __future__ import annotations

import json
import re
import secrets
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 1
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


def migrate() -> None:
    c = conn()
    c.executescript(SCHEMA)
    c.execute("INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))
