"""Sentence timings (the play-along transcript's) for episodes voiced before the voice wrote them,
from the chunk WAVs their job dir still has. papercast-voice deletes a job's chunks once its
episode is done, so this works only for jobs that kept them.

    python3 tools/backfill_timings.py [--data DIR] [--jobs DIR] [--voice-home DIR]
                                      [--voice-python PY] [--force] [--dry-run] [EPISODE ...]

Each episode with audio and no timings.json (--force: those too; or only the ones named) whose
job dir <jobs>/<id>/voice/ holds script.md, status.json and chunks/<key>/ (plan.json and every
chunk's WAV) gets <data>/episodes/<id>/timings.json. The times come from papercast-voice's own
code, `python -m papercast_voice timings` from this repository's copy, run niced with the voice's
venv (it has numpy and soundfile) and its voice.json: every chunk trimmed and placed as join()
placed it when the MP3 was made, its sentences split by characters and moved to the pauses
between them. The hub then checks them (hub/voices.validate_timings) and that they end inside
the episode's audio.

The defaults are perov's (the data in ~/papercast-group/data, the voice in ~/papercast-group/voice):
there, `python3 stacks/papercast-group/tools/backfill_timings.py` from ~/papercast-group/repo.
Exit 0 when every episode it looked at has timings now (or needed none), 1 when one could not.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

GROUP = Path(__file__).resolve().parent.parent
REPO = GROUP.parent.parent
if str(GROUP) not in sys.path:
    sys.path.insert(0, str(GROUP))

from tools._common import open_hub  # noqa: E402
from hub import db, voices  # noqa: E402
from hub.app import HTTPError  # noqa: E402


def voice_timings(vdir: Path, a) -> dict:
    """papercast-voice's timings for this job dir: {"ok": True, "doc": {...}} or {"ok": False, "problem"}."""
    env = {"HOME": os.environ.get("HOME", str(Path.home())), "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
           "LANG": "C.UTF-8", "PAPERCAST_VOICE_HOME": str(a.voice_home), "PYTHONPATH": str(a.voice_src)}
    if a.voice_config:
        env["PAPERCAST_VOICE_CONFIG"] = str(a.voice_config)
    fd, out = tempfile.mkstemp(prefix="timings-", suffix=".json")
    os.close(fd)
    try:
        r = subprocess.run(["nice", "-n", "10", str(a.voice_python), "-m", "papercast_voice", "timings",
                            str(vdir), "--out", out], env=env, capture_output=True, text=True, timeout=900,
                           stdin=subprocess.DEVNULL)
        lines = [ln for ln in r.stdout.splitlines() if ln.strip().startswith("{")]
        try:
            rep = json.loads(lines[-1]) if lines else {}
        except ValueError:
            rep = {}
        if not rep.get("ok"):
            why = rep.get("problem") or (r.stderr.strip().splitlines() or [f"exit {r.returncode}"])[-1]
            return {"ok": False, "problem": why[:400]}
        with open(out, encoding="utf-8") as fh:
            return {"ok": True, "doc": json.load(fh), "chunks": rep.get("chunks")}
    except (OSError, ValueError, subprocess.TimeoutExpired) as e:
        return {"ok": False, "problem": f"{e.__class__.__name__}: {e}"}
    finally:
        try:
            os.unlink(out)
        except OSError:
            pass


def backfill(a) -> int:
    cfg = open_hub(a.data, seed=False)
    voices.ensure_schema()
    jobs = Path(a.jobs) if a.jobs else cfg.episodes
    c = db.conn()
    if a.episodes:
        marks = ",".join("?" * len(a.episodes))
        rows = c.execute(f"SELECT id, state, duration_s, deleted_at FROM episodes WHERE id IN ({marks})", a.episodes).fetchall()
        missing = set(a.episodes) - {r["id"] for r in rows}
        for eid in sorted(missing):
            print(f"{eid}: no such episode")
    else:
        missing = set()
        rows = c.execute("SELECT id, state, duration_s, deleted_at FROM episodes WHERE state = 'ready' "
                         "AND deleted_at IS NULL ORDER BY created_at").fetchall()
    done = failed = skipped = 0
    for r in rows:
        eid, epdir = r["id"], cfg.episodes / r["id"]
        vdir = jobs / eid / "voice"
        if r["deleted_at"] or r["state"] != "ready" or not (epdir / "audio.mp3").is_file():
            print(f"{eid}: no audio (state {r['state']}{', deleted' if r['deleted_at'] else ''}); skipped")
            skipped += 1
            continue
        if (epdir / "timings.json").is_file() and not a.force:
            if a.episodes:
                print(f"{eid}: has timings already (--force to make them again)")
            skipped += 1
            continue
        if not (vdir / "chunks").is_dir():
            if a.episodes:
                print(f"{eid}: no job dir with chunks at {vdir}")
                failed += 1
            else:
                skipped += 1
            continue
        got = voice_timings(vdir, a)
        if not got["ok"]:
            print(f"{eid}: {got['problem']}")
            failed += 1
            continue
        try:
            doc = voices.validate_timings(got["doc"])
        except HTTPError as e:
            print(f"{eid}: the timings made are not valid: {e.msg}")
            failed += 1
            continue
        if not voices.fits(doc, r["duration_s"]):
            print(f"{eid}: the timings end at {doc['segments'][-1]['end']:.1f} s, past the "
                  f"{r['duration_s']:.1f} s of its audio: not the chunks it was made from")
            failed += 1
            continue
        n, end = len(doc["segments"]), doc["segments"][-1]["end"]
        if a.dry_run:
            print(f"{eid}: {n} sentences to {end:.1f} s of {r['duration_s'] or 0:.1f} s (chunks/{got.get('chunks')}); "
                  "not written (--dry-run)")
        else:
            p = voices.store_current(cfg, eid, doc)
            print(f"{eid}: {n} sentences to {end:.1f} s of {r['duration_s'] or 0:.1f} s (chunks/{got.get('chunks')}) -> {p}")
        done += 1
    print(f"{done} {'would get' if a.dry_run else 'got'} timings, {skipped} skipped, {failed + len(missing)} could not")
    return 1 if failed or missing else 0


def main(argv=None) -> int:
    home = Path.home()
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("episodes", nargs="*", help="episode ids (default: every episode with audio and no timings)")
    ap.add_argument("--data", default=os.environ.get("PCG_DATA") or str(home / "papercast-group" / "data"))
    ap.add_argument("--jobs", default=os.environ.get("PCG_VOICE_JOBS"), help="the voice job dirs (default <data>/episodes)")
    ap.add_argument("--voice-home", type=Path, default=home / "papercast-group" / "voice",
                    help="papercast-voice's install (its voice.json)")
    ap.add_argument("--voice-python", type=Path, help="a Python with numpy and soundfile (default <voice-home>/venv/bin/python)")
    ap.add_argument("--voice-config", type=Path, help="another voice.json (PAPERCAST_VOICE_CONFIG)")
    ap.add_argument("--voice-src", type=Path, default=REPO / "stacks" / "papercast" / "voice",
                    help="where papercast_voice's code is (default this repository's copy)")
    ap.add_argument("--force", action="store_true", help="make them again where there are some")
    ap.add_argument("--dry-run", action="store_true", help="make and check them, write nothing")
    a = ap.parse_args(argv)
    a.voice_python = a.voice_python or a.voice_home / "venv" / "bin" / "python"
    if not a.voice_python.exists():
        print(f"no {a.voice_python}: point --voice-python at papercast-voice's venv", file=sys.stderr)
        return 2
    if not (a.voice_src / "papercast_voice" / "timings.py").is_file():
        print(f"no papercast_voice/timings.py under {a.voice_src}", file=sys.stderr)
        return 2
    return backfill(a)


if __name__ == "__main__":
    sys.exit(main())
