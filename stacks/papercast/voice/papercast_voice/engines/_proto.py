"""Worker side of the engine protocol. Standard library only: workers run in the engine's own
venv (Kokoro's, or the chosen GPU model's), never in the orchestrator's.

    worker.py --spec '<json>' --threads N
    -> {"event": "ready", "sample_rate": 24000, "load_s": 3.1, ...}
    <- {"op": "synth", "id": 7, "text": "...", "out": "/abs/chunks/.../0007-ab12.wav"}
    -> {"event": "done", "id": 7, "audio_s": 12.3, "gen_s": 2.9, "maxrss_mib": 812}
    -> {"event": "error", "id": 7, "message": "...", "oom": false, "fatal": false}
    <- {"op": "quit"}

stdout carries the protocol and nothing else: fd 1 is moved aside and pointed at stderr before
any library is imported, so a library that prints cannot corrupt a reply.
"""
from __future__ import annotations

import argparse
import json
import os
import resource
import signal
import sys
import threading
import time


class Proto:
    def __init__(self) -> None:
        fd = os.dup(1)
        os.dup2(2, 1)
        sys.stdout = sys.stderr
        self._out = os.fdopen(fd, "w", buffering=1, encoding="utf-8")
        # Die with the orchestrator, even if it was SIGKILLed. Not PR_SET_PDEATHSIG: that fires
        # when the *thread* that spawned us exits (measured: the CPU pool starts workers from
        # threads, and the kernel killed a working engine when its starter thread returned).
        # The parent *process* changing is what matters, so watch that.
        threading.Thread(target=self._watch_parent, args=(os.getppid(),), daemon=True).start()

    @staticmethod
    def _watch_parent(ppid: int) -> None:
        while True:
            time.sleep(1.0)
            if os.getppid() != ppid:
                os.kill(os.getpid(), signal.SIGTERM)
                time.sleep(5.0)
                os._exit(1)

    def send(self, **msg) -> None:
        msg["maxrss_mib"] = round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024)
        self._out.write(json.dumps(msg) + "\n")
        self._out.flush()

    def requests(self):
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                req = json.loads(line)
            except ValueError:
                self.send(event="error", id=None, message="bad request line", oom=False, fatal=True)
                return
            if req.get("op") == "quit":
                return
            yield req


def args() -> argparse.Namespace:
    ap = argparse.ArgumentParser()
    ap.add_argument("--spec", required=True)
    ap.add_argument("--threads", type=int, default=1)
    a = ap.parse_args()
    a.spec = json.loads(a.spec)
    return a


def tmp_for(path: str) -> str:
    d, b = os.path.split(path)
    return os.path.join(d, f".{b}.{os.getpid()}.tmp")
