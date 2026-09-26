#!/usr/bin/env python3
"""A fake `nvidia-smi` for tests. Reads the scenario file named by $FAKE_NVSMI on every call,
so a test changes the GPU's state while a job runs. Logs every call to $FAKE_NVSMI.log.

Scenario: {"fail": false, "free": 12000, "total": 16376, "util": 0,
           "apps": [[pid, mib], ...], "pmon": [[pid, "C", sm], ...]}
Only the three call shapes papercast_voice.gpu uses are answered.
"""
import json
import os
import sys

path = os.environ.get("FAKE_NVSMI", "")
with open(path + ".log", "a") as fh:
    fh.write(" ".join(sys.argv[1:]) + "\n")
try:
    with open(path) as fh:
        sc = json.load(fh)
except (OSError, ValueError):
    sc = {"fail": True}
if sc.get("fail"):
    print("NVIDIA-SMI has failed because it couldn't communicate with the NVIDIA driver.")
    sys.exit(9)
args = sys.argv[1:]
if any(a.startswith("--query-gpu=") for a in args):
    free = sc.get("free")
    print(f"{'[N/A]' if free is None else free}, {sc.get('total', 16376)}, {sc.get('util', 0)}")
elif any(a.startswith("--query-compute-apps=") for a in args):
    for pid, mib in sc.get("apps", []):
        print(f"{pid}, {mib}")
elif args[:1] == ["pmon"]:
    print("# gpu         pid  type    sm    mem    enc    dec    command")
    print("# Idx           #   C/G     %      %      %      %    name")
    for pid, typ, sm in sc.get("pmon", []):
        print(f"    0  {pid:>9}     {typ}  {sm if sm is not None else '-':>4}     0      -      -    python")
else:
    print("fake nvidia-smi: unsupported call", file=sys.stderr)
    sys.exit(2)
