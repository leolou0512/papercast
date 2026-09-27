#!/usr/bin/env python3
"""Is a voice job running? install.sh asks before it replaces anything a job may be using.

    python3 busy.py <runner state dir> <voice home>     exit 0 = idle, 1 = busy (says which)

Standard library only (it runs before any venv exists). Busy when an engine may be loading
what the install replaces:
  - a job's status.json (state/<id>/voice/status.json) is speaking or encoding and its pid is a
    live papercast_voice process;
  - the CPU slot (<home>/run/voice-cpu.lock) or a remote GPU slot (<home>/run/slots/*.lock) is
    held.
Waiting jobs do not count: they hold no engine, and a waiting job re-executes itself on the new
code once it is installed (job.py `_maybe_reexec`). stibnite's GPU slot lock is not probed
either: the job at the front of the line holds it while it checks the card is free, and a job
speaking on it says so in its status. A lock is probed with a non-blocking flock that is
released at once; a job trying it in that instant simply tries again at its next poll.
"""
from __future__ import annotations

import fcntl
import glob
import json
import os
import sys

ACTIVE = {"speaking", "encoding"}


def _voice_pid(pid) -> bool:
    try:
        with open(f"/proc/{int(pid)}/cmdline", "rb") as fh:
            return b"papercast_voice" in fh.read()
    except (OSError, ValueError, TypeError):
        return False


def _held(path: str) -> bool:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC)
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return True
    finally:
        os.close(fd)
    return False


def reasons(state: str, home: str) -> list[str]:
    out = []
    for p in sorted(glob.glob(os.path.join(state, "*", "voice", "status.json"))):
        try:
            with open(p, encoding="utf-8") as fh:
                st = json.load(fh)
        except (OSError, ValueError):
            continue
        if st.get("phase") in ACTIVE and _voice_pid(st.get("pid")):
            out.append(f"{p}: {st.get('phase')} (pid {st.get('pid')})")
    locks = [os.path.join(home, "run", "voice-cpu.lock")]
    locks += sorted(glob.glob(os.path.join(home, "run", "slots", "*.lock")))
    for lock in locks:
        if _held(lock):
            out.append(f"{lock} is held")
    return out


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: busy.py <runner state dir> <voice home>", file=sys.stderr)
        return 2
    r = reasons(argv[0], argv[1])
    for x in r:
        print(x)
    return 1 if r else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
