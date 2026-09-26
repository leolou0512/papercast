"""Process helpers: our own descendants only, identity by pid + start time + boot id.

stibnite is shared. Every signal this package sends goes to a process that is a descendant of
a worker it started itself and runs under its own uid; nothing here ever looks a process up by
name or signals a pid it read from nvidia-smi.
"""
from __future__ import annotations

import ctypes
import os
import signal
import time

PR_SET_PDEATHSIG = 1


def set_pdeathsig(sig: int = signal.SIGTERM) -> None:
    """In a child, before exec: die with `sig` when the parent dies. Only for a child spawned
    from a thread that lives as long as the parent (a GPU adapter's model server, started from
    the worker's main thread): the kernel sends it when the spawning THREAD exits."""
    try:
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        libc.prctl(PR_SET_PDEATHSIG, sig, 0, 0, 0)
    except OSError:
        pass


def boot_id() -> str:
    try:
        with open("/proc/sys/kernel/random/boot_id", encoding="ascii") as fh:
            return fh.read().strip()
    except OSError:
        return ""


def _stat_fields(pid: int) -> list[str] | None:
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8", errors="replace") as fh:
            data = fh.read()
    except OSError:
        return None
    # comm is parenthesised and may contain spaces: split after the LAST ')'.
    rest = data[data.rindex(")") + 2:].split()
    return rest            # rest[0] = state, rest[1] = ppid, rest[19] = starttime


def start_ticks(pid: int) -> int | None:
    f = _stat_fields(pid)
    return int(f[19]) if f and len(f) > 19 else None


def alive(pid: int | None) -> bool:
    """Running and not a zombie."""
    if not pid:
        return False
    f = _stat_fields(pid)
    return bool(f) and f[0] not in ("Z", "X")


def identity(pid: int) -> dict:
    return {"pid": pid, "start_ticks": start_ticks(pid), "boot_id": boot_id()}


def same_process(rec: dict | None) -> bool:
    if not rec or not rec.get("pid"):
        return False
    pid = int(rec["pid"])
    return (alive(pid) and rec.get("boot_id") == boot_id()
            and rec.get("start_ticks") == start_ticks(pid))


def _uid(pid: int) -> int | None:
    try:
        return os.stat(f"/proc/{pid}").st_uid
    except OSError:
        return None


def descendants(root: int) -> set[int]:
    """root and every process below it (by ppid), read from /proc now."""
    kids: dict[int, list[int]] = {}
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        f = _stat_fields(int(name))
        if f and len(f) > 1:
            kids.setdefault(int(f[1]), []).append(int(name))
    out, todo = set(), [root]
    while todo:
        p = todo.pop()
        if p in out:
            continue
        out.add(p)
        todo.extend(kids.get(p, []))
    return out


def snapshot(root: int) -> dict[int, int | None]:
    """{pid: start_ticks} of root's descendants now (not root). Taken before a worker exits,
    because its children are re-parented away from it the moment it does."""
    return {p: start_ticks(p) for p in descendants(root) if p != root}


def kill_set(procs_: dict[int, int | None], grace_s: float = 10.0) -> None:
    """Stop processes from a snapshot that are still the same processes and run as us."""
    me = os.getuid()
    targets = {p for p, t in procs_.items()
               if t is not None and start_ticks(p) == t and _uid(p) == me and p != os.getpid()}
    _signal_all(targets, grace_s)


def kill_tree(root: int, grace_s: float = 10.0) -> None:
    """SIGTERM root and its descendants that run as us, wait, then SIGKILL what is left."""
    me = os.getuid()
    targets = {p for p in descendants(root) if _uid(p) == me and p != os.getpid()}
    _signal_all(targets, grace_s)


def _signal_all(targets: set[int], grace_s: float) -> None:
    for sig in (signal.SIGTERM, signal.SIGKILL):
        for p in targets:
            try:
                os.kill(p, sig)
            except OSError:
                pass
        deadline = time.time() + (grace_s if sig == signal.SIGTERM else 5.0)
        while time.time() < deadline and any(alive(p) for p in targets):
            time.sleep(0.1)
        targets = {p for p in targets if alive(p)}
        if not targets:
            return
