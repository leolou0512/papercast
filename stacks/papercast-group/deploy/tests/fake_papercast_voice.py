#!/usr/bin/env python3
"""A stand-in for papercast-voice (stacks/papercast/voice) with the same command line and files:

    fake_papercast_voice.py info
    fake_papercast_voice.py run <job dir>

`run` reads job.json and script.md, writes status.json through the real phases (preparing,
waiting-for-gpu, speaking, encoding, done | failed) with its pid, one "chunk" per paragraph
(chunks/<n>.done, skipped when there: the real voice's resume), honours `cancel`, holds
.voice.lock (exit 5 when another process has it), and writes out/episode.mp3: silent MPEG-1
Layer III frames, a real MP3 of FAKE_VOICE_SECONDS. It refuses job.json ids and scripts the real
one refuses (its ID_RE; digits), so a test catches what would fail on perov.

The worker starts the voice with a cleaned environment, so tests bake the knobs into a wrapper
script (write_wrapper): FAKE_VOICE_CHUNK_S, FAKE_VOICE_WAIT_S, FAKE_VOICE_SECONDS,
FAKE_VOICE_FAIL (an error code to fail with).
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import re
import sys
import time

ID_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}-[a-z2-7]{8}$")     # papercast_voice/job.py


def silent_mp3(seconds: float) -> bytes:
    """CBR 128 kbit/s, 44.1 kHz, mono MPEG-1 Layer III frames whose side info is all zero: every
    decoder reads them as silence. 417 bytes and 1,152 samples a frame."""
    frames = max(1, math.ceil(seconds * 44100 / 1152))
    frame = b"\xff\xfb\x90\xc4" + b"\x00" * 413
    return frame * frames


def mp3_seconds(data: bytes) -> float:
    return (len(data) // 417) * 1152 / 44100


def write_json(path: str, obj) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh)
    os.replace(tmp, path)


def read_json(path: str):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def run(vdir: str) -> int:
    chunk_s = float(os.environ.get("FAKE_VOICE_CHUNK_S", "0.05"))
    wait_s = float(os.environ.get("FAKE_VOICE_WAIT_S", "0"))
    seconds = float(os.environ.get("FAKE_VOICE_SECONDS", "3"))
    fail = os.environ.get("FAKE_VOICE_FAIL", "")
    lock = open(os.path.join(vdir, ".voice.lock"), "a+")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("another voice process is working here", flush=True)
        return 5
    sp = os.path.join(vdir, "status.json")
    st = {"interface": "1.4", "paper_id": None, "phase": "preparing", "engine": None,
          "chunks_done": 0, "chunks_total": 0, "pid": os.getpid(), "since": now(),
          "updated_at": now(), "error": None, "output": None, "wait_text": None, "note": None,
          "resumed_chunks": 0}

    def put(**kw):
        st.update(kw, updated_at=now())
        write_json(sp, st)

    def failed(code, msg, rc=3):
        put(phase="failed", error={"code": code, "message": msg})
        print(f"failed: {code}: {msg}", flush=True)
        return rc

    prev = read_json(sp) or {}
    out_mp3 = os.path.join(vdir, "out", "episode.mp3")
    if prev.get("phase") == "done" and os.path.isfile(out_mp3):
        with open(out_mp3, "rb") as fh:
            if hashlib.sha256(fh.read()).hexdigest() == (prev.get("output") or {}).get("sha256"):
                put(phase="done", output=prev["output"], paper_id=prev.get("paper_id"))
                print("already done", flush=True)
                return 0
    put()
    job = read_json(os.path.join(vdir, "job.json"))
    if not isinstance(job, dict):
        return failed("engine_failed", "job.json missing")
    if not ID_RE.match(str(job.get("paper_id", ""))):
        return failed("engine_failed", "job.json paper_id is not a papercast id")
    put(paper_id=job["paper_id"])
    try:
        with open(os.path.join(vdir, job.get("script", "script.md")), encoding="utf-8") as fh:
            script = fh.read()
    except OSError:
        return failed("script_invalid", "script.md missing", 2)
    if re.search(r"[0-9]", script):
        return failed("script_invalid", "digits in the script", 2)
    paras = [p for p in re.split(r"\n\s*\n", script) if p.strip()]
    put(phase="waiting-for-gpu", chunks_total=len(paras), wait_text="waiting for GPU: fake")
    end = time.time() + wait_s
    while time.time() < end:
        if os.path.exists(os.path.join(vdir, "cancel")):
            return failed("cancelled", "stopped because the paper was dismissed", 4)
        time.sleep(0.05)
    os.makedirs(os.path.join(vdir, "chunks"), exist_ok=True)
    put(phase="speaking", engine="gpu:fake", wait_text=None, note="Speaking on stibnite GPU 0.")
    done = resumed = 0
    for i, _p in enumerate(paras):
        if os.path.exists(os.path.join(vdir, "cancel")):
            return failed("cancelled", "stopped because the paper was dismissed", 4)
        c = os.path.join(vdir, "chunks", f"{i:04d}.done")
        if os.path.exists(c):
            resumed += 1
        else:
            time.sleep(chunk_s)
            if fail and i == len(paras) // 2:
                return failed(fail, f"fake failure {fail}")
            open(c, "w").close()
        done += 1
        put(chunks_done=done, resumed_chunks=resumed)
    put(phase="encoding")
    os.makedirs(os.path.join(vdir, "out"), exist_ok=True)
    data = silent_mp3(seconds)
    with open(out_mp3 + ".tmp", "wb") as fh:
        fh.write(data)
    os.replace(out_mp3 + ".tmp", out_mp3)
    for f in os.listdir(os.path.join(vdir, "chunks")):
        os.unlink(os.path.join(vdir, "chunks", f))
    put(phase="done", eta_s=0, output={
        "path": "out/episode.mp3", "format": "mp3", "bitrate_kbps": 128,
        "duration_s": round(mp3_seconds(data), 2), "loudness_lufs": -16.0,
        "sha256": hashlib.sha256(data).hexdigest(), "size": len(data), "engine": "gpu:fake",
        "voice": "fake", "tags": job.get("tags")})
    print("done", flush=True)
    return 0


def write_wrapper(path: str, **knobs) -> str:
    """A papercast-voice command for tests: this file with the knobs baked in (the worker cleans
    the environment, as Leo's runner does). The name keeps "papercast_voice" in the command line,
    which the worker looks for when it adopts a process."""
    lines = ["#!/bin/sh"]
    for k, v in knobs.items():
        lines.append(f"FAKE_VOICE_{k.upper()}='{v}'; export FAKE_VOICE_{k.upper()}")
    lines.append(f"exec '{sys.executable}' '{os.path.abspath(__file__)}' \"$@\"")
    with open(path, "w") as fh:
        fh.write("\n".join(lines) + "\n")
    os.chmod(path, 0o755)
    return path


def main(argv) -> int:
    if argv[:1] == ["info"]:
        print(json.dumps({"interface": "1.4", "installed": True,
                          "gpu": {"model": "fake", "need_mib": 1, "installed": True},
                          "cpu": {"model": "fake"}, "words_per_min": 150}))
        return 0
    if argv[:1] == ["run"] and len(argv) == 2:
        return run(argv[1])
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
