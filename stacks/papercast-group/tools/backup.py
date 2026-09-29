"""Back up a hub data dir while the hub runs: hub.db through SQLite's online backup API (one
consistent snapshot, whatever the hub writes meanwhile) and episodes/ and avatars/ (the profile
pictures, hub/avatars.py) as an rsync --link-dest style copy (a file unchanged since the previous backup is a hard link to that backup's copy,
anything else is copied), into <dest>/<YYYY-MM-DDTHHMMSSZ>/, keeping the newest --keep (14).

    python3 tools/backup.py --data $PCG_DATA --dest ~/papercast-group/backups [--keep 14]

Every backup is complete on its own (delete any of them, the others stay whole) and costs only
what changed. hub.db goes first, then the files, so every episode in the database snapshot has
its files (a picture replaced in between is missing from that backup: the page shows initials). Left out: each episode's voice/ job dir (the voice worker's scratch, remade from
script.md; --with-voice keeps it) and hub.db-wal / -shm (the snapshot already holds their pages).
A backup is written into .partial-<name>/ and renamed only when whole, so a folder with a date
name is always a complete backup; backup.json in it says what it holds. One backup at a time
(a lock in <dest>). Hard links need <dest> on one filesystem; across filesystems it copies.

Restore (the hub stopped, so nothing writes the data dir meanwhile):

    systemctl --user stop <the hub's unit>                    # see deploy/README
    mv "$PCG_DATA" "$PCG_DATA.before-restore"                 # keep it until the restore is checked
    mkdir -p "$PCG_DATA"
    cp -a <dest>/<name>/hub.db "$PCG_DATA/hub.db"
    cp -a <dest>/<name>/episodes "$PCG_DATA/episodes"
    [ -d <dest>/<name>/avatars ] && cp -a <dest>/<name>/avatars "$PCG_DATA/avatars"
    python3 -m hub.db migrate --data "$PCG_DATA"              # a backup from older code catches up
    systemctl --user start <the hub's unit>

Copy, never hard-link or move, out of a backup: the hub writes into its files, and a link would
change the backup (and every later backup sharing that file). Check the page, then delete
"$PCG_DATA.before-restore". To restore one episode: stop the hub, cp -a its folder back.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import shutil
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

NAME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{6}Z(?:-\d+)?$")


def backups(dest: Path) -> list:
    """The complete backups in dest, oldest first."""
    if not dest.is_dir():
        return []
    return sorted((p for p in dest.iterdir() if p.is_dir() and NAME_RE.match(p.name)), key=lambda p: p.name)


def _snapshot_db(src: Path, dst: Path) -> int:
    """hub.db -> dst as one consistent snapshot, a standalone file (journal DELETE) that passes
    integrity_check. Returns its schema version (0 when it has none)."""
    s = sqlite3.connect(str(src), timeout=60)
    d = sqlite3.connect(str(dst))
    try:
        s.execute("PRAGMA busy_timeout=60000")
        s.backup(d)                    # all pages in one step: one read transaction, one snapshot
        d.execute("PRAGMA journal_mode=DELETE")
        ok = d.execute("PRAGMA integrity_check").fetchone()[0]
        if ok != "ok":
            raise RuntimeError(f"the snapshot fails integrity_check: {ok}")
        try:
            r = d.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
        except sqlite3.OperationalError:
            r = None
        return int(r[0]) if r else 0
    finally:
        d.close()
        s.close()


def _same(a: os.stat_result, b: os.stat_result) -> bool:
    """rsync's quick check: size and modification time."""
    return a.st_size == b.st_size and a.st_mtime_ns == b.st_mtime_ns


def _copy_tree(src: Path, dst: Path, prev: Path | None, with_voice: bool) -> dict:
    n = {"files": 0, "linked": 0, "copied": 0, "bytes_copied": 0, "bytes_total": 0, "skipped": 0}
    if not src.is_dir():
        return n
    for root, dirs, files in os.walk(src):
        rel = Path(root).relative_to(src)
        if not with_voice and len(rel.parts) == 1:
            dirs[:] = [x for x in dirs if x != "voice"]          # episodes/<id>/voice/
        dirs.sort()
        (dst / rel).mkdir(parents=True, exist_ok=True)
        for f in sorted(files):
            live = Path(root) / f
            try:
                st = live.lstat()
            except FileNotFoundError:                             # deleted while we walked
                n["skipped"] += 1
                continue
            if not os.path.isfile(live) or live.is_symlink():
                n["skipped"] += 1
                continue
            out = dst / rel / f
            old = prev / rel / f if prev else None
            done = False
            if old is not None:
                try:
                    if _same(old.lstat(), st):
                        os.link(old, out)
                        n["linked"] += 1
                        done = True
                except OSError:            # not in the previous backup, or another filesystem
                    pass
            if not done:
                try:
                    shutil.copy2(live, out)                        # keeps mtime: next time it links
                except FileNotFoundError:
                    n["skipped"] += 1
                    continue
                n["copied"] += 1
                n["bytes_copied"] += st.st_size
            n["files"] += 1
            n["bytes_total"] += st.st_size
    return n


def backup(data: Path, dest: Path, keep: int = 14, with_voice: bool = False, when: datetime | None = None) -> dict:
    """One backup of data into dest, then the oldest beyond `keep` removed. Returns its report."""
    data, dest = Path(data).resolve(), Path(dest).resolve()
    if not (data / "hub.db").is_file():
        raise SystemExit(f"{data}: no hub.db here")
    if dest == data or dest.is_relative_to(data / "episodes") or dest.is_relative_to(data / "avatars") \
            or data.is_relative_to(dest):
        raise SystemExit(f"--dest {dest} must not be the data dir, inside its episodes/ or avatars/, or above it")
    if keep < 1:
        raise SystemExit("--keep must be at least 1")
    dest.mkdir(parents=True, exist_ok=True)
    lock = open(dest / ".lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit(f"{dest}: another backup is running")
    try:
        for p in dest.iterdir():                                  # what a crashed run left
            if p.name.startswith(".partial-") and p.is_dir():
                shutil.rmtree(p)
        when = when or datetime.now(timezone.utc)
        name = when.strftime("%Y-%m-%dT%H%M%SZ")
        k = 2
        while (dest / name).exists():
            name = when.strftime("%Y-%m-%dT%H%M%SZ") + f"-{k}"
            k += 1
        prev = backups(dest)[-1] if backups(dest) else None
        part = dest / f".partial-{name}"
        part.mkdir()
        t0 = time.monotonic()
        version = _snapshot_db(data / "hub.db", part / "hub.db")
        files = _copy_tree(data / "episodes", part / "episodes", prev / "episodes" if prev else None, with_voice)
        pics = _copy_tree(data / "avatars", part / "avatars", prev / "avatars" if prev else None, False)
        report = {"name": name, "at": when.strftime("%Y-%m-%dT%H:%M:%SZ"), "data": str(data),
                  "schema_version": version, "db_bytes": (part / "hub.db").stat().st_size,
                  "previous": prev.name if prev else None, "with_voice": with_voice,
                  "seconds": round(time.monotonic() - t0, 2), **files, "avatars": pics}
        (part / "backup.json").write_text(json.dumps(report, indent=1) + "\n")
        os.rename(part, dest / name)
        removed = []
        for old in backups(dest)[:-keep]:
            shutil.rmtree(old)
            removed.append(old.name)
        report["removed"] = removed
        report["path"] = str(dest / name)
        return report
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", default=os.environ.get("PCG_DATA"), help="the hub's data dir (default $PCG_DATA)")
    ap.add_argument("--dest", required=True, help="where the dated backup folders go")
    ap.add_argument("--keep", type=int, default=14, help="how many backups to keep (default 14)")
    ap.add_argument("--with-voice", action="store_true", help="also keep each episode's voice/ job dir")
    a = ap.parse_args(argv)
    if not a.data:
        ap.error("--data (or $PCG_DATA) is required")
    try:
        os.nice(10)                    # a background job on a shared machine
    except OSError:
        pass
    r = backup(Path(a.data), Path(a.dest), a.keep, a.with_voice)
    mb = lambda b: f"{b / 1e6:.1f} MB"  # noqa: E731
    print(f"{r['path']}: hub.db {mb(r['db_bytes'])} (schema {r['schema_version']}), {r['files']} files "
          f"({r['linked']} linked, {r['copied']} copied, {mb(r['bytes_copied'])} new of {mb(r['bytes_total'])}), "
          f"{r['avatars']['files']} profile pictures in {r['seconds']} s" + (f"; removed {', '.join(r['removed'])}" if r["removed"] else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
