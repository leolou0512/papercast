#!/usr/bin/env python3
"""A fake remote `nvidia-smi` (the two queries hosts.Host.read_remote makes, all GPUs at once).

Scenario $FAKE_RSMI: {"gpus": [{"free": 24000, "total": 24576, "util": 0,
                                "apps": [[pid, mib], ...]}, ...]}
Engines running on the fake host ($FAKE_RSMI_PROCS/<gpu>-<pid>, pid alive) are listed as compute
processes of their GPU too, as a real driver lists them, named by their argv[0] (the driver
names a process by its command, so a venv's python shows as that venv's bin/python).
"""
import json
import os
import sys

try:
    with open(os.environ["FAKE_RSMI"]) as fh:
        sc = json.load(fh)
except (OSError, ValueError, KeyError):
    print("NVIDIA-SMI has failed because it couldn't communicate with the NVIDIA driver.")
    sys.exit(9)
gpus = sc.get("gpus", [])
args = sys.argv[1:]


def alive(pid):
    try:
        with open(f"/proc/{pid}/stat") as fh:
            return fh.read().rsplit(")", 1)[1].split()[0] not in ("Z", "X")
    except (OSError, IndexError):
        return False


if any(a.startswith("--query-gpu=") for a in args):
    for i, g in enumerate(gpus):
        print(f"{i}, GPU-fake-{i}, {g.get('free', 24000)}, {g.get('total', 24576)}, {g.get('util', 0)}")
elif any(a.startswith("--query-compute-apps=") for a in args):
    for i, g in enumerate(gpus):
        for pid, mib, *name in g.get("apps", []):
            print(f"GPU-fake-{i}, {pid}, {mib}, {name[0] if name else '/usr/bin/python3'}")
    d = os.environ.get("FAKE_RSMI_PROCS", "")
    for name in (os.listdir(d) if d and os.path.isdir(d) else []):
        gi, _, pid = name.partition("-")
        if pid.isdigit() and alive(int(pid)):
            try:
                with open(f"/proc/{pid}/cmdline", "rb") as fh:
                    argv0 = fh.read().split(b"\0")[0].decode()
            except OSError:
                continue
            print(f"GPU-fake-{gi}, {pid}, 9000, {argv0}")
else:
    print("fake remote nvidia-smi: unsupported call", file=sys.stderr)
    sys.exit(2)
