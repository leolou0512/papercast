"""Time the queries the page needs, in plain SQL, on a big fake library (tools/fake_data.py):

    python3 tools/loadcheck.py [--papers 1000] [--users 5] [--repeat 30] [--data DIR]

For each query: rows, the median and slowest of --repeat runs (ms, fetching every row), and the
query plan's full-table scans; once with this code's indexes and once without the ones migration
3 adds, so it shows what each index buys. Without --data it builds the library in a temp dir
(about 30 MB for 1,000 papers, no audio) and removes it after.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import statistics
import sys
import tempfile
import time
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import fake_data  # noqa: E402
from hub import db  # noqa: E402

LIBRARY = """
SELECT p.id, p.title, p.label, p.authors, p.year, p.arxiv_id, p.doi, p.url, p.tags, p.created_at,
       e.id AS episode_id, e.state, e.prefs_summary, e.duration_s, e.est_minutes, e.created_at AS made_at,
       e.made_by, u.name AS maker
FROM papers p
JOIN episodes e ON e.paper_id = p.id AND e.deleted_at IS NULL
JOIN users u ON u.id = e.made_by
ORDER BY p.created_at DESC, e.created_at"""

QUERIES = [
    ("library: every paper with its episodes and makers", LIBRARY, ()),
    ("one user's Listened ticks", "SELECT paper_id, at FROM listened WHERE user_id = ?", ("U",)),
    ("one user's positions", "SELECT episode_id, seconds, updated_at FROM positions WHERE user_id = ?", ("U",)),
    ("library, one query with this user's ticks and positions", LIBRARY.replace(
        "JOIN users u ON u.id = e.made_by",
        "JOIN users u ON u.id = e.made_by\nLEFT JOIN listened l ON l.user_id = ? AND l.paper_id = p.id\n"
        "LEFT JOIN positions s ON s.user_id = ? AND s.episode_id = e.id").replace(
        "u.name AS maker", "u.name AS maker, l.at AS listened_at, s.seconds AS position"), ("U", "U")),
    ("search: title contains a word", LIBRARY.replace("ORDER BY", "WHERE p.title_norm LIKE '%' || ? || '%'\nORDER BY"), ("pudding",)),
    ("one paper's episodes", "SELECT e.*, u.name FROM episodes e JOIN users u ON u.id = e.made_by WHERE e.paper_id = ?", ("P",)),
    ("a paper's parents on the map (links by dst)", "SELECT src, grade FROM links WHERE dst = ? AND state = 'active'", ("P",)),
    ("a graph's members by tag rule", "SELECT p.id FROM papers p WHERE EXISTS (SELECT 1 FROM json_each(p.tags) t "
     "WHERE t.value IN (SELECT value FROM json_each((SELECT rule_tags FROM graphs WHERE id = ?))))", ("G",)),
    ("links among a graph's members", "WITH m AS (SELECT p.id FROM papers p WHERE EXISTS (SELECT 1 FROM json_each(p.tags) t "
     "WHERE t.value IN (SELECT value FROM json_each((SELECT rule_tags FROM graphs WHERE id = ?))))) "
     "SELECT l.id, l.src, l.dst, l.grade, l.origin FROM links l WHERE l.state = 'active' "
     "AND l.src IN (SELECT id FROM m) AND l.dst IN (SELECT id FROM m)", ("G",)),
    ("a graph's positions", "SELECT paper_id, x, y FROM layout WHERE graph_id = ?", ("G",)),
    ("edit log, newest 100", "SELECT * FROM graph_log ORDER BY id DESC LIMIT 100", ()),
    ("edit log, one user's newest 100", "SELECT * FROM graph_log WHERE user_id = ? ORDER BY id DESC LIMIT 100", ("U",)),
    ("voice queue, oldest first", "SELECT * FROM voice_jobs WHERE state = 'queued' ORDER BY queued_at", ()),
    ("one user's own episodes (cli ?mine=1)", "SELECT e.*, p.title FROM episodes e JOIN papers p ON p.id = e.paper_id "
     "WHERE e.made_by = ? ORDER BY e.created_at DESC", ("U",)),
]
M3_INDEXES = [name for name, _ in db.M3_INDEXES]


def run(path: Path, repeat: int) -> list:
    c = sqlite3.connect(str(path))
    uid = c.execute("SELECT made_by FROM episodes GROUP BY made_by ORDER BY COUNT(*) DESC LIMIT 1").fetchone()[0]
    listener = c.execute("SELECT user_id FROM listened GROUP BY user_id ORDER BY COUNT(*) DESC LIMIT 1").fetchone()[0]
    pid = c.execute("SELECT dst FROM links GROUP BY dst ORDER BY COUNT(*) DESC LIMIT 1").fetchone()[0]
    gid = c.execute("SELECT graph_id FROM layout GROUP BY graph_id ORDER BY COUNT(*) DESC LIMIT 1").fetchone()[0]
    out = []
    for name, sql, args in QUERIES:
        vals = tuple({"U": listener if "Listened" in name or "positions" in name or "ticks" in name else uid,
                      "P": pid, "G": gid}.get(a, a) for a in args)
        plan = [r[3] for r in c.execute("EXPLAIN QUERY PLAN " + sql, vals)]
        scans = sorted({p.split()[1] for p in plan if p.startswith("SCAN") and len(p.split()) > 1
                        and not p.split()[1].startswith(("json_each", "m", "CONSTANT"))})
        ts = []
        rows = 0
        for _ in range(repeat):
            t = time.perf_counter()
            rows = len(c.execute(sql, vals).fetchall())
            ts.append((time.perf_counter() - t) * 1000)
        out.append({"name": name, "rows": rows, "median_ms": statistics.median(ts), "max_ms": max(ts), "scans": scans})
    # what the page's JSON would cost on top: every library row as a dict, then dumps
    c.row_factory = sqlite3.Row
    t = time.perf_counter()
    body = json.dumps([dict(r) for r in c.execute(LIBRARY)])
    out.append({"name": "library rows -> JSON (Python)", "rows": len(body), "median_ms": (time.perf_counter() - t) * 1000,
                "max_ms": 0.0, "scans": []})
    c.close()
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--papers", type=int, default=1000)
    ap.add_argument("--users", type=int, default=5)
    ap.add_argument("--repeat", type=int, default=30)
    ap.add_argument("--data", help="an existing hub data dir to measure instead of a fresh fake one")
    a = ap.parse_args(argv)
    tmp = None
    try:
        if a.data:
            data = Path(a.data)
        else:
            tmp = tempfile.mkdtemp(prefix="pcg-loadcheck-")
            data = Path(tmp) / "data"
            t = time.perf_counter()
            n = fake_data.generate(data, papers=a.papers, users=a.users, seed=1, audio="none", ready_without_audio=True)
            db.close()
            print(f"fake library: {n['papers']} papers, {n['episodes']} episodes, {n['links']} links, "
                  f"{n['listened']} ticks, {n['positions']} positions, {n['users']} users "
                  f"({time.perf_counter() - t:.1f} s to make)")
        work = Path(tempfile.mkdtemp(prefix="pcg-loadcheck-db-"))
        try:
            src = sqlite3.connect(str(data / "hub.db"))
            for tag in ("with", "without"):
                dst = sqlite3.connect(str(work / f"{tag}.db"))
                src.backup(dst)
                if tag == "without":
                    for i in M3_INDEXES:
                        dst.execute(f"DROP INDEX IF EXISTS {i}")
                dst.execute("ANALYZE")
                dst.commit()
                dst.close()
            src.close()
            res = {tag: run(work / f"{tag}.db", a.repeat) for tag in ("with", "without")}
        finally:
            shutil.rmtree(work, ignore_errors=True)
        print(f"sqlite {sqlite3.sqlite_version}, python {sys.version.split()[0]}; ms = median of {a.repeat} (slowest)")
        print(f"{'query':58} {'rows':>6} {'with indexes':>18} {'without m3':>18}  full scans (with)")
        for w, wo in zip(res["with"], res["without"]):
            fmt = lambda r: f"{r['median_ms']:7.2f} ({r['max_ms']:6.2f})"  # noqa: E731
            print(f"{w['name'][:58]:58} {w['rows']:>6} {fmt(w):>18} {fmt(wo):>18}  {', '.join(w['scans']) or '-'}")
    finally:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
