"""The line for GPU slots: the oldest waiting job gets the next free slot.

Every job waiting for a GPU holds a ticket, a file in `run/queue/` named by when it joined the
line and kept under flock by the job's process (so a ticket whose process died is known to be
stale by anyone who can lock it). A job joins the line with the time it was handed over
(job.py `_queued_at`: job.json's modification time, or, for a run that continues an interrupted
one, the time recorded then), so a job that restarts, re-executes itself after an upgrade, or
comes back from a GPU it lost keeps its place; a Retry, which is a new hand-off, joins at the
back.

Only the jobs that may take a slot now look at the GPUs (one nvidia-smi on stibnite and one ssh
per remote host every poll): the front of the line for any slot, and for a remote slot the
oldest job that may use one. The others only watch their own controls and count the tickets
ahead of them, so two hundred waiting papers cost the machines what one does.

A job that may no longer use remote GPUs (it failed there `max_failures` times) renames its
ticket with the suffix `.L` (local only; the flock stays, it is on the inode): it keeps its
place for stibnite's slot and stops holding back the jobs behind it for a remote one.
"""
from __future__ import annotations

import fcntl
import os
import re

NAME_RE = re.compile(r"^(\d{10}\.\d{6})-([0-9a-z-]{1,64})(\.L)?$")
LOCAL_ONLY = ".L"


def _held(path: str) -> bool:
    """Someone holds this ticket's lock (its process is alive)."""
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


class Ticket:
    def __init__(self, qdir: str, joined_at: float, paper_id: str):
        self.qdir = qdir
        self.joined_at = float(joined_at)
        self.base = f"{self.joined_at:017.6f}-{paper_id}"
        self.local_only = False
        self.fd: int | None = None

    @property
    def name(self) -> str:
        return self.base + (LOCAL_ONLY if self.local_only else "")

    @property
    def path(self) -> str:
        return os.path.join(self.qdir, self.name)

    def enter(self) -> None:
        if self.fd is not None:
            return
        os.makedirs(self.qdir, mode=0o700, exist_ok=True)
        # A ticket of ours left under the other name (a re-execution, or a crash) goes.
        other = os.path.join(self.qdir, self.base + ("" if self.local_only else LOCAL_ONLY))
        if not _held(other):
            try:
                os.unlink(other)
            except OSError:
                pass
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            raise RuntimeError(f"ticket {self.name} is held by another process")
        self.fd = fd
        try:
            os.ftruncate(fd, 0)
            os.write(fd, f"{os.getpid()}\n".encode())
        except OSError:
            pass

    def set_local_only(self) -> None:
        if self.local_only:
            return
        old = self.path
        self.local_only = True
        if self.fd is not None:
            try:
                os.rename(old, self.path)
            except OSError:
                pass

    def _older(self) -> list[str]:
        try:
            names = os.listdir(self.qdir)
        except OSError:
            return []
        me = self.base
        return sorted(n for n in names if NAME_RE.match(n) and n.split(".L")[0] < me)

    def ahead(self) -> tuple[int, int]:
        """(live tickets in front of this one, those of them that may use a remote slot).
        Stale tickets at the front are removed on the way (only there: the rest are counted,
        not probed, to keep a long line cheap)."""
        older = self._older()
        while older:
            p = os.path.join(self.qdir, older[0])
            if _held(p):
                break
            try:
                os.unlink(p)
            except OSError:
                pass
            older.pop(0)
        return len(older), sum(1 for n in older if not n.endswith(LOCAL_ONLY))

    def leave(self) -> None:
        if self.fd is None:
            return
        try:
            os.unlink(self.path)
        except OSError:
            pass
        try:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
        finally:
            os.close(self.fd)
            self.fd = None


def line(qdir: str) -> list[tuple[str, bool]]:
    """(ticket name, live) for everyone in line, front first (for `papercast-voice slots`)."""
    try:
        names = sorted(n for n in os.listdir(qdir) if NAME_RE.match(n))
    except OSError:
        return []
    return [(n, _held(os.path.join(qdir, n))) for n in names]
