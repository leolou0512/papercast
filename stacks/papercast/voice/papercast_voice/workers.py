"""Orchestrator side of the engine protocol (engines/_proto.py): one Worker = one engine process.

A GPU engine is one adapter: a worker script plus an `engines.<name>` block in voice.json
(python, worker, model files, max_words, peak_mib). Nothing else in the voice knows which model
it is.
"""
from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time

from . import procs
from .textprep import assert_speakable


class EngineError(Exception):
    pass


class EngineOOM(EngineError):
    pass


class Cancelled(Exception):
    pass


def _log(msg: str) -> None:
    print(time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), msg, file=sys.stderr, flush=True)


class Worker:
    def __init__(self, spec: dict, *, threads: int, gpu_index: int | None, cfg: dict, tag: str):
        self.spec = spec
        self.threads = max(1, int(threads))
        self.gpu_index = gpu_index
        self.cfg = cfg
        self.tag = tag
        self.proc: subprocess.Popen | None = None
        self.q: queue.Queue = queue.Queue()
        self.ready: dict | None = None
        self.maxrss_mib = 0
        self.gen_s = 0.0

    @property
    def pid(self) -> int | None:
        return self.proc.pid if self.proc else None

    def _env(self) -> dict:
        home = self.cfg["home"]
        t = str(self.threads)
        env = {
            "HOME": os.environ.get("HOME", "/home/leo"), "USER": os.environ.get("USER", "leo"),
            "LANG": os.environ.get("LANG", "C.UTF-8"),
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
            "OMP_NUM_THREADS": t, "MKL_NUM_THREADS": t, "OPENBLAS_NUM_THREADS": t,
            "NUMEXPR_NUM_THREADS": t, "TOKENIZERS_PARALLELISM": "false",
            "HF_HOME": os.path.join(home, "hf"), "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1", "PYTHONUNBUFFERED": "1",
            "CUDA_DEVICE_ORDER": "PCI_BUS_ID",
            "CUDA_VISIBLE_DEVICES": "" if self.gpu_index is None else str(self.gpu_index),
        }
        for k, v in (self.spec.get("env") or {}).items():
            env[str(k)] = str(v)
        return env

    def _reader(self) -> None:
        assert self.proc and self.proc.stdout
        for line in self.proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                msg = {"event": "garbage", "line": line[:200]}
            self.q.put(msg)
        self.q.put({"event": "eof"})

    def start(self, cancel: threading.Event) -> dict:
        argv = [self.spec["python"], self.spec["worker"], "--spec", json.dumps(self.spec),
                "--threads", str(self.threads)]
        if not os.path.exists(self.spec["python"]):
            raise EngineError(f"engine {self.spec['name']} is not installed "
                              f"({self.spec['python']} missing)")
        self.proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=None, env=self._env(), text=True, bufsize=1,
                                     cwd=self.cfg["home"] if os.path.isdir(self.cfg["home"]) else "/")
        # (No PR_SET_PDEATHSIG here: this may run in a short-lived thread, and the kernel sends
        # that signal when the spawning thread exits. The worker watches its parent instead.)
        threading.Thread(target=self._reader, daemon=True).start()
        msg = self._wait(None, float(self.spec.get("load_timeout_s", 300)), cancel)
        if msg.get("event") != "ready":
            self.stop()
            raise EngineError(f"engine {self.spec['name']} did not start: "
                              f"{msg.get('message') or msg.get('event')}")
        self.ready = msg
        self.maxrss_mib = max(self.maxrss_mib, int(msg.get("maxrss_mib") or 0))
        _log(f"{self.tag}: {self.spec['name']} ready in {msg.get('load_s')} s (pid {self.pid}, "
             f"{self.threads} thread(s))")
        return msg

    def _wait(self, rid, timeout: float, cancel: threading.Event) -> dict:
        deadline = time.time() + timeout
        while True:
            if cancel.is_set():
                self.stop(grace_s=2)
                raise Cancelled()
            try:
                msg = self.q.get(timeout=0.5)
            except queue.Empty:
                if time.time() > deadline:
                    self.stop(grace_s=2)
                    raise EngineError(f"engine {self.spec['name']} gave no answer in {timeout:.0f} s")
                continue
            ev = msg.get("event")
            if ev == "eof":
                rc = self.proc.wait(timeout=30) if self.proc else None
                raise EngineError(f"engine {self.spec['name']} exited (code {rc}); see voice.log")
            if ev == "garbage":
                _log(f"{self.tag}: ignored non-protocol line: {msg.get('line')!r}")
                continue
            if ev == "error" and msg.get("oom"):
                raise EngineOOM(msg.get("message") or "CUDA out of memory")
            if ev == "error" and (msg.get("fatal") or rid is None or msg.get("id") == rid):
                raise EngineError(msg.get("message") or "engine error")
            if rid is None or msg.get("id") == rid:
                return msg

    def synth(self, rid: int, text: str, out: str, cancel: threading.Event) -> dict:
        assert_speakable(text)          # the last check before any text reaches an engine
        if not self.proc or self.proc.poll() is not None:
            raise EngineError(f"engine {self.spec['name']} is not running")
        try:
            self.proc.stdin.write(json.dumps({"op": "synth", "id": rid, "text": text,
                                              "out": out}) + "\n")
            self.proc.stdin.flush()
        except (BrokenPipeError, OSError) as e:
            raise EngineError(f"engine {self.spec['name']} went away: {e}") from e
        msg = self._wait(rid, float(self.spec.get("chunk_timeout_s", 600)), cancel)
        self.maxrss_mib = max(self.maxrss_mib, int(msg.get("maxrss_mib") or 0))
        self.gen_s += float(msg.get("gen_s") or 0)
        if not os.path.isfile(out) or os.path.getsize(out) <= 44:
            raise EngineError(f"engine {self.spec['name']} reported chunk {rid} done but wrote no audio")
        return msg

    def stop(self, grace_s: float = 10.0) -> None:
        p = self.proc
        if not p:
            return
        children = procs.snapshot(p.pid) if p.poll() is None else {}
        if p.poll() is None:
            try:
                p.stdin.write(json.dumps({"op": "quit"}) + "\n")
                p.stdin.flush()
                p.stdin.close()
            except (OSError, ValueError):
                pass
            try:
                p.wait(timeout=min(grace_s, 5.0))
            except subprocess.TimeoutExpired:
                pass
        # The engine may have children (a model server's stage processes): stop the whole tree,
        # and anything from before it exited that is still the same process.
        if p.poll() is None:
            procs.kill_tree(p.pid, grace_s=grace_s)
        procs.kill_set(children, grace_s=grace_s)
        try:
            p.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
