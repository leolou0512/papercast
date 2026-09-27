#!/usr/bin/env python3
"""A fake `ssh` for tests: `ssh [-o opt]... HOST COMMAND` runs COMMAND with sh on this machine,
as if HOST were another one. HOST is the path of a test's fake-host directory:

  HOST/down        when it exists, every call fails the way an unreachable host does (255)
  HOST/ssh.log     one line per call: "engine" (a session with no ControlMaster) or "poll"
  HOST/gpus.json   the fake remote nvidia-smi's scenario (fake_remote_smi.py), via $FAKE_RSMI
  HOST/procs/      engines running there, one file "<gpu>-<pid>" each (fake_worker.py)
"""
import os
import sys

args = sys.argv[1:]
i = 0
opts = []
while i < len(args) and args[i].startswith("-"):
    if args[i] in ("-o", "-p", "-i", "-l", "-F"):
        opts.append(args[i + 1])
        i += 2
    else:
        i += 1
host, cmd = args[i], " ".join(args[i + 1:])
kind = "engine" if "ControlMaster=no" in opts else "poll"
with open(os.path.join(host, "ssh.log"), "a") as fh:
    fh.write(f"{kind} {cmd[:200]}\n")
if os.path.exists(os.path.join(host, "down")):
    print(f"ssh: connect to host {os.path.basename(host)} port 22: Network is unreachable",
          file=sys.stderr)
    sys.exit(255)
os.environ["FAKE_RSMI"] = os.path.join(host, "gpus.json")
os.environ["FAKE_RSMI_PROCS"] = os.path.join(host, "procs")
os.execvp("sh", ["sh", "-c", cmd])
