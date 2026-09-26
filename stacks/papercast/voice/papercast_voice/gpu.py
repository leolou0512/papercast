"""The GPU is shared: when may the voice take it, and when must it give it back.

Spec §2.9: waiting is the default; require the model's measured peak plus headroom; never slow or
crash someone else's job. So two rules, both read from `nvidia-smi` (read-only: nothing here
touches another process):

Admission (while waiting-for-gpu), all of these on `stable_polls` consecutive polls:
  1. nvidia-smi answers. Not being able to check is not the same as free.
  2. memory.free >= need_mib (measured peak of the engine + headroom).
  3. utilization.gpu <= util_max_pct: memory alone says nothing about someone's job running on
     the card; starting beside it would slow it. Desktop graphics stay far under the threshold.
  A single failing poll starts the count again.

Yield (while speaking, between chunks):
  another compute process at >= yield_other_sm_pct SM (`nvidia-smi pmon`, our own process tree
  excluded) on `yield_polls` checks in a row, or free memory under yield_free_floor_mib (someone
  is growing into the card). The voice then stops its engine, keeps its chunks, and waits again.
"""
from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass, field


@dataclass
class Reading:
    ok: bool
    error: str | None = None
    free_mib: int | None = None
    total_mib: int | None = None
    util_pct: int | None = None
    apps: list = field(default_factory=list)       # [(pid, used_mib | None)] compute processes
    sm: dict = field(default_factory=dict)         # {pid: sm% | None} compute processes (pmon)
    at: float = 0.0


def _num(s: str) -> int | None:
    s = s.strip()
    try:
        return int(float(s))
    except ValueError:
        return None


def _run(argv: list[str], timeout: float) -> str:
    r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(f"{' '.join(argv[:2])} exited {r.returncode}: "
                           f"{(r.stderr or r.stdout).strip()[:200]}")
    return r.stdout


def read(nvidia_smi: str = "nvidia-smi", index: int = 0, timeout: float = 15.0,
         with_pmon: bool = False) -> Reading:
    now = time.time()
    try:
        out = _run([nvidia_smi, "-i", str(index),
                    "--query-gpu=memory.free,memory.total,utilization.gpu",
                    "--format=csv,noheader,nounits"], timeout)
        line = out.strip().splitlines()[0] if out.strip() else ""
        parts = [p.strip() for p in line.split(",")]
        if len(parts) != 3:
            raise RuntimeError(f"unexpected nvidia-smi output: {line[:120]!r}")
        free, total, util = (_num(p) for p in parts)
        if free is None or total is None:
            raise RuntimeError(f"nvidia-smi gave no memory figure: {line[:120]!r}")
        apps = []
        out = _run([nvidia_smi, "-i", str(index), "--query-compute-apps=pid,used_memory",
                    "--format=csv,noheader,nounits"], timeout)
        for ln in out.strip().splitlines():
            ps = [p.strip() for p in ln.split(",")]
            if ps and ps[0].isdigit():
                apps.append((int(ps[0]), _num(ps[1]) if len(ps) > 1 else None))
        sm: dict = {}
        if with_pmon:
            out = _run([nvidia_smi, "pmon", "-i", str(index), "-c", "1", "-s", "u"], timeout)
            for ln in out.splitlines():
                if ln.lstrip().startswith("#"):
                    continue
                f = ln.split()
                if len(f) >= 4 and f[1].isdigit() and "C" in f[2]:
                    sm[int(f[1])] = _num(f[3]) if f[3] != "-" else 0
    except (OSError, subprocess.SubprocessError, RuntimeError, IndexError) as e:
        return Reading(ok=False, error=str(e)[:300], at=now)
    return Reading(ok=True, free_mib=free, total_mib=total, util_pct=util, apps=apps, sm=sm, at=now)


def _gb(mib: int | None) -> str:
    return "?" if mib is None else f"{mib / 1024:.1f} GB"


class Admission:
    def __init__(self, need_mib: int, util_max_pct: int, stable_polls: int):
        self.need = int(need_mib)
        self.util_max = int(util_max_pct)
        self.stable = max(1, int(stable_polls))
        self.streak = 0

    def step(self, r: Reading) -> tuple[bool, str, str]:
        """(admit, reason code, plain-words sentence for the page)."""
        if not r.ok:
            self.streak = 0
            return False, "nvidia_smi", f"cannot read the GPU's state ({r.error}); waiting"
        if r.free_mib < self.need:
            self.streak = 0
            return (False, "memory",
                    f"waiting for GPU: {_gb(r.free_mib)} free, needs {_gb(self.need)}")
        if r.util_pct is None or r.util_pct > self.util_max:
            self.streak = 0
            used = "unknown" if r.util_pct is None else f"{r.util_pct}%"
            return (False, "busy",
                    f"waiting for GPU: memory is free but the card is busy ({used} in use by "
                    f"other work), so starting now would slow it")
        self.streak += 1
        if self.streak < self.stable:
            return (False, "settling",
                    f"waiting for GPU: {_gb(r.free_mib)} free and idle; checking it stays so "
                    f"({self.streak} of {self.stable})")
        return True, "admitted", f"GPU free: {_gb(r.free_mib)} free, needs {_gb(self.need)}"


class YieldRule:
    def __init__(self, other_sm_pct: int, polls: int, free_floor_mib: int):
        self.sm = int(other_sm_pct)
        self.polls = max(1, int(polls))
        self.floor = int(free_floor_mib)
        self.streak = 0

    def step(self, r: Reading, ours: set[int]) -> tuple[bool, str]:
        if not r.ok:
            return False, ""           # a failed read while we hold the card: keep going
        busy = sorted(p for p, v in r.sm.items() if p not in ours and v is not None and v >= self.sm)
        low = r.free_mib is not None and r.free_mib < self.floor
        if not (busy or low):
            self.streak = 0
            return False, ""
        self.streak += 1
        if self.streak < self.polls:
            return False, ""
        if busy:
            return True, (f"another job started on the GPU (process {', '.join(map(str, busy))}); "
                          "gave the GPU back so it is not slowed")
        return True, (f"GPU memory is nearly full ({r.free_mib} MiB free); gave the GPU back so "
                      "another job does not run out")
