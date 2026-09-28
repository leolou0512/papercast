"""Command line. The runner uses `info` and `run <dir>` (INTERFACE.md §10.1); the rest is for people.

    papercast-voice info                   one JSON line, < 2 s, never touches the GPU
    papercast-voice run <job dir>          voice one episode (exit 0 = status.json phase done)
    papercast-voice check <script.md>      how a script would be chunked, or why it is refused
    papercast-voice timings <job dir> [--out FILE]
                                           out/timings.json for a voiced job whose chunk WAVs
                                           are still there (one JSON line: ok, segments, path)
    papercast-voice slots [<state dir>]    the line for GPU slots and who holds each slot (read only)
    papercast-voice measure-cpu [...]      time the CPU voice at several worker/thread splits
    papercast-voice measure-episode <dir>  words per minute and speed from a finished episode
    papercast-voice measure-gpu [...]      peak GPU memory and speed of the configured GPU voice
"""
from __future__ import annotations

import json
import os
import sys

# Before numpy is imported anywhere: its BLAS would otherwise start one spinning thread per CPU
# (measured: 5.8 s of CPU in a 0.4 s run on stibnite's 64). The orchestrator needs one.
for _v in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
    os.environ.setdefault(_v, "1")

from . import VERSION  # noqa: E402
from .config import engine_spec, gpu_need_mib, load

INTERFACE = "1.4"   # this document version (INTERFACE.md); readers compare the major


def _engine_installed(spec: dict) -> bool:
    if not os.path.isfile(spec.get("python", "")) or not os.path.isfile(spec.get("worker", "")):
        return False
    md = spec.get("model_dir")
    return all(os.path.isfile(os.path.join(md, f)) for f in spec.get("files", [])) if md else True


def info() -> dict:
    try:
        cfg = load()
        cpu = engine_spec(cfg, cfg["cpu_engine"])
        ok = _engine_installed(cpu) and os.access(cfg["ffmpeg"], os.X_OK)
        g = None
        wpm = cpu.get("words_per_min")
        if cfg.get("gpu_engine"):
            spec = engine_spec(cfg, cfg["gpu_engine"])
            g = {"model": spec.get("label", spec["name"]), "need_mib": gpu_need_mib(cfg),
                 "installed": _engine_installed(spec)}
            wpm = spec.get("words_per_min") or wpm
        return {"interface": INTERFACE, "installed": bool(ok), "gpu": g,
                "cpu": {"model": cpu.get("label", cpu["name"]), "voice": cpu.get("voice")},
                "words_per_min": wpm, "version": VERSION}
    except Exception as e:  # noqa: BLE001  (info must answer, not crash: installed false)
        return {"interface": INTERFACE, "installed": False, "gpu": None, "cpu": None,
                "words_per_min": None, "version": VERSION, "error": f"{e.__class__.__name__}: {e}"}


def check(path: str) -> int:
    from . import textprep
    cfg = load()
    with open(path, encoding="utf-8") as fh:
        script = fh.read()
    names = [cfg["cpu_engine"]] + ([cfg["gpu_engine"]] if cfg.get("gpu_engine") else [])
    out = {}
    try:
        for n in names:
            spec = engine_spec(cfg, n)
            chunks = textprep.plan(script, int(spec["max_words"]), cfg["audio"])
            out[n] = {"chunks": len(chunks), "words": sum(c.words for c in chunks),
                      "max_chunk_words": max(c.words for c in chunks),
                      "headings": sum(1 for c in chunks if c.kind == "heading")}
    except textprep.ScriptInvalid as e:
        print(json.dumps({"ok": False, "problem": str(e)}))
        return 2
    print(json.dumps({"ok": True, **out}))
    return 0


def timings_cmd(args: list[str]) -> int:
    from . import timings
    vdir, out = args[0], None
    if len(args) == 3 and args[1] == "--out":
        out = args[2]
    elif len(args) != 1:
        print(__doc__, file=sys.stderr)
        return 2
    try:
        doc = timings.from_job_dir(vdir, load())
    except (timings.TimingsError, OSError, ValueError, KeyError) as e:
        print(json.dumps({"ok": False, "problem": f"{e.__class__.__name__}: {e}"
                          if not isinstance(e, timings.TimingsError) else str(e)}))
        return 2
    name = doc.pop("chunks", None)
    if out is None:
        os.makedirs(os.path.join(vdir, "out"), mode=0o700, exist_ok=True)
        out = os.path.join(vdir, "out", "timings.json")
    timings.write(out, doc)
    print(json.dumps({"ok": True, "path": out, "segments": len(doc["segments"]),
                      "duration_s": doc["duration_s"], "chunks": name}))
    return 0


def slots(state_dir: str) -> dict:
    """The line and the slots, read only: ticket names, and each slot's lock holder (the pid a
    FileLock writes into its file) when it is held."""
    from . import hosts, sched
    from .locks import FileLock
    cfg = load()
    line = sched.line(cfg.get("queue_dir") or os.path.join(cfg["home"], "run", "queue"))
    out = {"line": {"live": sum(1 for _n, live in line if live),
                    "stale": sum(1 for _n, live in line if not live),
                    "front": [n for n, live in line if live][:3]},
           "slots": {}}
    for s in hosts.build(cfg, state_dir, gpu_need_mib(cfg) or 0):
        lk = FileLock(s.lock_path)
        if lk.try_acquire():
            lk.release()
            out["slots"][s.label] = None
        else:
            try:
                with open(s.lock_path, encoding="ascii") as fh:
                    out["slots"][s.label] = {"pid": int(fh.read().strip() or 0)}
            except (OSError, ValueError):
                out["slots"][s.label] = {"pid": None}
    return out


def main(argv: list[str]) -> int:
    if argv[:1] == ["info"]:
        print(json.dumps(info()))
        return 0
    if argv[:1] == ["run"] and len(argv) == 2:
        from .job import Job
        return Job(argv[1], load()).run()
    if argv[:1] == ["check"] and len(argv) == 2:
        return check(argv[1])
    if argv[:1] == ["timings"] and len(argv) in (2, 4):
        return timings_cmd(argv[1:])
    if argv[:1] == ["slots"] and len(argv) <= 2:
        print(json.dumps(slots(argv[1] if len(argv) == 2 else "/home/leo/papercast/state")))
        return 0
    if argv[:1] == ["measure-cpu"]:
        from .measure import measure_cpu
        return measure_cpu(argv[1:])
    if argv[:1] == ["measure-episode"]:
        from .measure import measure_episode
        return measure_episode(argv[1:])
    if argv[:1] == ["measure-gpu"]:
        from .measure import measure_gpu
        return measure_gpu(argv[1:])
    print(__doc__, file=sys.stderr)
    return 2
