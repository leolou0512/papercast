#!/usr/bin/env python3
"""Run a command and record the peak GPU memory of its whole process tree.

    gpumem.py --out peak.json [--interval 0.05] [--need-mib N] -- <command...>

Every `interval` seconds it asks NVML for the compute processes on GPU 0 and sums
`usedGpuMemory` over the PIDs that belong to the child's process tree. That figure
is what nvidia-smi shows per process: CUDA context + everything the allocator
reserved, i.e. what another user actually loses. It also records the device-wide
used memory, so another user's job starting mid-run is visible in the output.

--need-mib refuses to start (exit 3) unless the GPU has at least that much free, so
the run never squeezes someone else's job. Only reads NVML; never touches other
processes.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time

import psutil
import pynvml


def tree_pids(root: int) -> set[int]:
    try:
        p = psutil.Process(root)
        return {root} | {c.pid for c in p.children(recursive=True)}
    except psutil.NoSuchProcess:
        return set()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--interval", type=float, default=0.05)
    ap.add_argument("--need-mib", type=int, default=0)
    ap.add_argument("cmd", nargs=argparse.REMAINDER)
    a = ap.parse_args()
    cmd = a.cmd[1:] if a.cmd and a.cmd[0] == "--" else a.cmd
    if not cmd:
        ap.error("no command")

    pynvml.nvmlInit()
    h = pynvml.nvmlDeviceGetHandleByIndex(0)
    mi = pynvml.nvmlDeviceGetMemoryInfo(h)
    total_mib = mi.total / 2**20
    before_used = mi.used / 2**20
    others_before = [
        {"pid": p.pid, "mib": (p.usedGpuMemory or 0) / 2**20}
        for p in pynvml.nvmlDeviceGetComputeRunningProcesses(h)
    ]
    free_mib = mi.free / 2**20
    if a.need_mib and free_mib < a.need_mib:
        print(f"gpumem: only {free_mib:.0f} MiB free, need {a.need_mib}; not starting",
              file=sys.stderr)
        return 3

    child = subprocess.Popen(cmd)
    peak_tree = 0.0
    peak_dev = before_used
    samples = 0
    stop = threading.Event()

    def poll() -> None:
        nonlocal peak_tree, peak_dev, samples
        while not stop.is_set():
            pids = tree_pids(child.pid)
            try:
                procs = pynvml.nvmlDeviceGetComputeRunningProcesses(h)
                used = sum((p.usedGpuMemory or 0) for p in procs if p.pid in pids) / 2**20
                dev = pynvml.nvmlDeviceGetMemoryInfo(h).used / 2**20
            except pynvml.NVMLError:
                used, dev = 0.0, 0.0
            peak_tree = max(peak_tree, used)
            peak_dev = max(peak_dev, dev)
            samples += 1
            stop.wait(a.interval)

    t = threading.Thread(target=poll, daemon=True)
    t0 = time.time()
    t.start()
    rc = child.wait()
    stop.set()
    t.join()
    rec = {
        "cmd": cmd,
        "returncode": rc,
        "wall_s": round(time.time() - t0, 2),
        "gpu_total_mib": round(total_mib),
        "gpu_used_before_mib": round(before_used),
        "other_compute_procs_before": others_before,
        "peak_process_tree_mib": round(peak_tree),
        "peak_device_used_mib": round(peak_dev),
        "poll_interval_s": a.interval,
        "samples": samples,
        "measured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    with open(a.out, "w") as f:
        json.dump(rec, f, indent=2)
    print(json.dumps(rec))
    return rc


if __name__ == "__main__":
    sys.exit(main())
