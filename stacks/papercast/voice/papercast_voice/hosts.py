"""GPU slots on more than one machine: stibnite's own card plus each idle GPU of a remote host.

A slot is one GPU that one job may hold. stibnite's slot is INTERFACE §10.4's
`state/voice-gpu.lock` (so voice processes of every version exclude each other on it) and keeps
stibnite's admission rule (gpu.py: memory, utilisation, handing back at >= 20 % SM). A remote
GPU's slot is a lock under the voice's own `run/slots/`, and its rule is stricter, because a
remote GPU is only lent while nobody uses it:

  admission  no compute process on that GPU at all, free memory >= the host's measured peak +
             headroom, utilisation <= util_max_pct, checked right before the slot is taken and
             once more right before the first chunk;
  yield      between chunks (every poll_s): a compute process that was not there when the job
             took its slot and is not one of our engines, on ANY of the host's configured GPUs
             (not only ours: Leo's chat model on bs1 spans cards 0-6, and one card held by the
             voice makes it fail), ends the job's turn after the chunk it is on, and holds the
             whole host back for holdoff_s: every voice job there yields, none starts, so the
             owner's retry finds the cards free. Also free memory under the floor on our GPU.
  pause      `touch <voice home>/run/pause-<host>`: every job there yields between chunks and
             none starts until the file is removed (Leo's lever to free bs1 at once).

Our engines are told apart from everyone else's by their executable (the remote install's venv
python, as nvidia-smi names it), so one voice job never mistakes another's engine for a
stranger.

The engine runs on the remote host as the host's `run_as` user (ssh logs in as whoever the
alias says; bs1's is root, so `runuser -u leo`), niced, pinned with CUDA_VISIBLE_DEVICES, and
speaks the same JSON-lines protocol over the ssh session's stdin/stdout. Its WAVs come back in
the replies (base64), so every finished chunk is on stibnite at once: a lost connection costs at
most the chunk being spoken, and no job file is ever written on the remote host. The remote
engine ends itself when its session closes (end of input) or when no request arrived for
idle_exit_s.

The remote host keeps only the engine's venv, weights, upstream code and compiler caches (a
one-off install under `home`). The worker script itself goes over with the job: `ensure_code`
puts this install's copy of the engine files in `home/app/v-<hash>/` (17 KB, written once per
code version, the newest three kept), so the remote engine is always the code of the
orchestrator that talks to it.

Every ssh call has a timeout; a host that does not answer is skipped for down_backoff_s, and
jobs meanwhile use the other slots.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import shlex
import subprocess
import tarfile
import time
from dataclasses import dataclass

from . import gpu

LOCAL = "local"
SSH = "ssh"


def _gb(mib: int | None) -> str:
    return "?" if mib is None else f"{mib / 1024:.1f} GB"


@dataclass
class Host:
    name: str
    kind: str
    cfg: dict
    local_home: str = ""
    down_until: float = 0.0
    last_error: str | None = None

    @property
    def remote(self) -> bool:
        return self.kind == SSH

    # ------------------------------------------------------------------ pause and hold-off
    def _run_file(self, name: str) -> str:
        return os.path.join(self.local_home, "run", name)

    def paused(self) -> bool:
        return os.path.exists(self._run_file(f"pause-{self.name}"))

    def holdoff(self) -> tuple[float, str] | None:
        """(until, why) while the host is held back for someone else's work, else None."""
        try:
            with open(self._run_file(f"holdoff-{self.name}.json"), encoding="utf-8") as fh:
                d = json.load(fh)
            until = float(d.get("until") or 0)
        except (OSError, ValueError, TypeError, AttributeError):
            return None
        return (until, str(d.get("why") or "")) if until > time.time() else None

    def hold_off(self, why: str) -> None:
        until = time.time() + float(self.cfg.get("holdoff_s", 1800))
        cur = self.holdoff()
        if cur and cur[0] >= until:
            return
        path = self._run_file(f"holdoff-{self.name}.json")
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"until": until, "why": why[:300],
                       "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}, fh)
        os.replace(tmp, path)

    def ours(self, name: str | None) -> bool:
        """A compute process of one of our engines (any job's), by its executable."""
        return bool(name) and name == f"{self.cfg['home']}/venv/bin/python"

    def strangers(self, readings: dict, known: set[int] = frozenset()) -> list[tuple[int, int]]:
        """(gpu, pid) of compute processes on the host's configured GPUs that are not our
        engines and not in `known`."""
        out = []
        for g in self.gpus():
            r = readings.get(g)
            if r is None or not r.ok:
                continue
            for a in r.apps:
                pid, name = a[0], (a[2] if len(a) > 2 else None)
                if pid not in known and not self.ours(name):
                    out.append((g, pid))
        return out

    # ------------------------------------------------------------------ ssh
    def ssh_base(self, *, control: bool) -> list[str]:
        c = self.cfg
        argv = [c.get("ssh_cmd") or "ssh", "-o", "BatchMode=yes",
                "-o", f"ConnectTimeout={int(c.get('connect_timeout_s', 10))}",
                "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=4"]
        if control and c.get("control_dir"):
            # Polls share one connection (a master kept 60 s). Engine sessions never do: a
            # master that died would take every job's engine on that host with it.
            os.makedirs(c["control_dir"], mode=0o700, exist_ok=True)
            argv += ["-o", "ControlMaster=auto", "-o", "ControlPersist=60",
                     "-o", f"ControlPath={os.path.join(c['control_dir'], '%C')}"]
        else:
            argv += ["-o", "ControlMaster=no", "-o", "ControlPath=none"]
        return argv + [c["ssh"]]

    def as_user(self, argv: list[str]) -> list[str]:
        u = self.cfg.get("run_as")
        return (["runuser", "-u", u, "--"] if u else []) + argv

    def run(self, remote_argv: list[str], timeout: float) -> str:
        cmd = " ".join(shlex.quote(a) for a in remote_argv)
        r = subprocess.run(self.ssh_base(control=True) + [cmd], capture_output=True, text=True,
                           timeout=timeout, stdin=subprocess.DEVNULL)
        if r.returncode != 0:
            raise RuntimeError(f"ssh {self.cfg['ssh']} exited {r.returncode}: "
                               f"{(r.stderr or r.stdout).strip()[-200:]}")
        return r.stdout

    # ------------------------------------------------------------------ GPU state
    def read_remote(self) -> dict[int, gpu.Reading]:
        """{gpu index: Reading} for every GPU of the host, from one ssh call. A failed call is
        one not-ok Reading per configured GPU (and the host is skipped for a while)."""
        now = time.time()
        smi = self.cfg.get("nvidia_smi") or "nvidia-smi"
        timeout = float(self.cfg.get("timeout_s", 20))
        script = (f"{shlex.quote(smi)} --query-gpu=index,uuid,memory.free,memory.total,"
                  "utilization.gpu --format=csv,noheader,nounits && echo --- && "
                  f"{shlex.quote(smi)} --query-compute-apps=gpu_uuid,pid,used_memory,process_name "
                  "--format=csv,noheader,nounits")
        try:
            out = self.run(["sh", "-c", script], timeout)
            head, _, apps_txt = out.partition("---")
            by_uuid: dict[str, int] = {}
            res: dict[int, gpu.Reading] = {}
            for ln in head.strip().splitlines():
                p = [x.strip() for x in ln.split(",")]
                if len(p) != 5 or not p[0].isdigit():
                    raise RuntimeError(f"unexpected nvidia-smi line: {ln[:120]!r}")
                i = int(p[0])
                by_uuid[p[1]] = i
                res[i] = gpu.Reading(ok=True, free_mib=gpu._num(p[2]), total_mib=gpu._num(p[3]),
                                     util_pct=gpu._num(p[4]), apps=[], at=now)
            for ln in apps_txt.strip().splitlines():
                p = [x.strip() for x in ln.split(",", 3)]
                if len(p) >= 2 and p[1].isdigit() and p[0] in by_uuid:
                    res[by_uuid[p[0]]].apps.append((int(p[1]), gpu._num(p[2]) if len(p) > 2 else None,
                                                    p[3] if len(p) > 3 else None))
            if not res:
                raise RuntimeError("nvidia-smi listed no GPU")
        except (OSError, subprocess.SubprocessError, RuntimeError) as e:
            self.last_error = str(e)[:300]
            self.down_until = time.time() + float(self.cfg.get("down_backoff_s", 60))
            return {i: gpu.Reading(ok=False, error=self.last_error, at=now) for i in self.gpus()}
        self.last_error, self.down_until = None, 0.0
        return res

    def gpus(self) -> list[int]:
        return [int(i) for i in self.cfg.get("gpus") or []]

    # ------------------------------------------------------------------ engine
    @staticmethod
    def code_files(local: dict) -> list[str]:
        """The files the engine needs from this install: its worker and the protocol module
        (the worker imports `_proto` from its own directory, where they both land)."""
        return [local["worker"],
                os.path.join(os.path.dirname(os.path.abspath(__file__)), "engines", "_proto.py")]

    @staticmethod
    def code_version(files: list[str]) -> str:
        h = hashlib.sha256()
        for f in files:
            h.update(os.path.basename(f).encode() + b"\0")
            with open(f, "rb") as fh:
                h.update(fh.read())
        return h.hexdigest()[:12]

    def ensure_code(self, local: dict) -> str:
        """Put this install's engine files on the remote host (once per version) and return
        the directory. Raises RuntimeError when the host cannot be reached."""
        files = self.code_files(local)
        d = f"{self.cfg['home']}/app/v-{self.code_version(files)}"
        t = float(self.cfg.get("timeout_s", 20))
        have = self.run(self.as_user(["sh", "-c", 'test -f "$1/.complete" && echo yes || echo no',
                                      "sh", d]), t).strip()
        if have == "yes":
            return d
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tf:
            for f in files:
                tf.add(f, arcname=os.path.basename(f))
            info = tarfile.TarInfo(".complete")
            tf.addfile(info, io.BytesIO(b""))
        # Unpacked beside the target and renamed into place: a reader sees all or nothing.
        # Then only the newest three versions are kept (names are strictly v-<12 hex>).
        script = ('set -e; d=$1; t="$d.tmp.$$"; rm -rf "$t"; mkdir -p "$t"; tar -xf - -C "$t"; '
                  'if [ -e "$d" ]; then rm -rf "$t"; else mv "$t" "$d"; fi; '
                  'cd "$(dirname "$d")"; ls -1dt v-[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]'
                  '[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f] 2>/dev/null | '
                  'tail -n +4 | while read -r old; do rm -rf "./$old"; done')
        cmd = " ".join(shlex.quote(a) for a in self.as_user(["sh", "-c", script, "sh", d]))
        r = subprocess.run(self.ssh_base(control=True) + [cmd], input=buf.getvalue(),
                           capture_output=True, timeout=t * 3)
        if r.returncode != 0:
            raise RuntimeError(f"could not put the engine code on {self.name}: "
                               f"{(r.stderr or r.stdout).decode(errors='replace').strip()[-200:]}")
        return d

    def engine_spec(self, local: dict, code_dir: str | None = None) -> dict:
        """The GPU engine's spec as the remote host runs it: the same model settings, the
        remote install's paths, the host's precision and anything the host overrides."""
        rh = self.cfg["home"]
        spec = dict(local)
        spec["python"] = f"{rh}/venv/bin/python"
        spec["worker"] = f"{code_dir or rh + '/app/engines'}/{os.path.basename(local['worker'])}"
        if local.get("code_dir"):
            spec["code_dir"] = f"{rh}/src"
        if local.get("model_dir"):
            spec["model_dir"] = f"{rh}/models/{os.path.basename(local['model_dir'].rstrip('/'))}"
        env = {"TRITON_CACHE_DIR": f"{rh}/cache/triton",
               "TORCHINDUCTOR_CACHE_DIR": f"{rh}/cache/inductor"}
        if self.cfg.get("cc"):
            env["CC"] = self.cfg["cc"]
        spec["env"] = env
        spec["dtype"] = self.cfg.get("dtype") or local.get("dtype") or "bf16"
        spec["idle_exit_s"] = float(self.cfg.get("idle_exit_s", 180))
        over = self.cfg.get("engine") or {}
        for k, v in over.items():
            if k == "env":
                spec["env"] = {**spec["env"], **v}
            else:
                spec[k] = v
        return spec

    def worker_argv(self, spec: dict, threads: int, gpu_index: int) -> list[str]:
        rh = self.cfg["home"]
        t = str(threads)
        env = {"HOME": self.cfg.get("user_home") or os.path.dirname(rh.rstrip("/")),
               "USER": self.cfg.get("run_as") or "leo", "LANG": "C.UTF-8",
               "PATH": self.cfg.get("path") or "/usr/local/bin:/usr/bin:/bin",
               "OMP_NUM_THREADS": t, "MKL_NUM_THREADS": t, "OPENBLAS_NUM_THREADS": t,
               "NUMEXPR_NUM_THREADS": t, "TOKENIZERS_PARALLELISM": "false",
               "HF_HOME": f"{rh}/hf", "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1",
               "PYTHONUNBUFFERED": "1", "CUDA_DEVICE_ORDER": "PCI_BUS_ID",
               "CUDA_VISIBLE_DEVICES": str(gpu_index)}
        env.update({str(k): str(v) for k, v in (spec.get("env") or {}).items()})
        remote = self.as_user(["env", "-i", *[f"{k}={v}" for k, v in env.items()],
                               "nice", "-n", str(int(self.cfg.get("nice", 10))),
                               spec["python"], spec["worker"], "--spec", json.dumps(spec),
                               "--threads", t])
        return self.ssh_base(control=False) + [" ".join(shlex.quote(a) for a in remote)]

    def kill(self, pid: int, worker_path: str) -> None:
        """Stop our engine on the remote host by pid, only if that pid still runs our worker
        script, and as run_as (who cannot signal anyone else's process)."""
        script = ('p=$1; w=$2; own() { tr "\\0" "\\n" < /proc/$p/cmdline 2>/dev/null | '
                  'grep -qxF "$w"; }; own || exit 0; kill -TERM $p; '
                  'for i in 1 2 3 4 5 6 7 8 9 10; do sleep 0.5; own || exit 0; done; kill -KILL $p')
        try:
            self.run(self.as_user(["sh", "-c", script, "sh", str(int(pid)), worker_path]),
                     float(self.cfg.get("timeout_s", 20)))
        except (OSError, subprocess.SubprocessError, RuntimeError):
            pass


@dataclass
class Slot:
    host: Host
    gpu: int
    lock_path: str
    need_mib: int
    dtype: str

    @property
    def key(self) -> str:
        return f"{self.host.name}-gpu{self.gpu}"

    @property
    def label(self) -> str:
        return f"{self.host.name} GPU {self.gpu}"

    @property
    def remote(self) -> bool:
        return self.host.remote


def build(cfg: dict, state_dir: str, local_need: int | None) -> list[Slot]:
    """Every slot in preference order: the hosts in config order ("order"), GPUs ascending."""
    out: list[Slot] = []
    hosts = cfg.get("hosts") or {}
    names = cfg.get("host_order") or list(hosts)
    head = int(cfg["gpu"]["headroom_mib"])
    for name in names:
        h = hosts.get(name) or {}
        if not h.get("enabled", True):
            continue
        kind = h.get("kind", LOCAL)
        host = Host(name=name, kind=kind, cfg=h, local_home=cfg["home"])
        if kind == LOCAL:
            if local_need is None:
                continue
            lock = cfg.get("gpu_lock") or os.path.join(state_dir, "voice-gpu.lock")
            out.append(Slot(host, int(cfg["gpu"]["index"]), lock, local_need,
                            h.get("dtype") or "bf16"))
        else:
            if not h.get("peak_mib"):
                continue            # not measured there: never admitted on a guess
            need = int(h["peak_mib"]) + head
            for g in host.gpus():
                out.append(Slot(host, g, os.path.join(cfg["home"], "run", "slots",
                                                      f"{name}-gpu{g}.lock"),
                                need, h.get("dtype") or "bf16"))
    return out


class RemoteAdmission:
    """A remote GPU is taken only while nobody computes on it (module docstring)."""

    def __init__(self, need_mib: int, util_max_pct: int, stable_polls: int):
        self.need = int(need_mib)
        self.util_max = int(util_max_pct)
        self.stable = max(1, int(stable_polls))
        self.streak = 0

    def check(self, r: gpu.Reading) -> tuple[bool, str]:
        if not r.ok:
            return False, "unreachable"
        if r.apps:
            return False, "in use"
        if r.free_mib is None or r.free_mib < self.need:
            return False, "no room"
        if r.util_pct is None or r.util_pct > self.util_max:
            return False, "busy"
        return True, "idle"

    def step(self, r: gpu.Reading) -> tuple[bool, str]:
        ok, why = self.check(r)
        if not ok:
            self.streak = 0
            return False, why
        self.streak += 1
        return self.streak >= self.stable, ("idle" if self.streak >= self.stable else "settling")


class RemoteYield:
    """Between chunks on a remote GPU (module docstring). `known` = the strangers already on the
    host's GPUs when the job took its slot (Leo's speech model on card 6, say)."""

    def __init__(self, host: Host, gpu_index: int, free_floor_mib: int, known: set[int]):
        self.host, self.gpu, self.floor, self.known = host, gpu_index, int(free_floor_mib), known

    def step(self, readings: dict, own_pid: int | None) -> tuple[bool, str, bool]:
        """(yield now, why, hold the whole host back)."""
        if self.host.paused():
            return True, f"{self.host.name} is paused (run/pause-{self.host.name}); gave the GPU back", False
        h = self.host.holdoff()
        if h:
            return True, f"{self.host.name} is left free for other work: {h[1]}", False
        r = readings.get(self.gpu)
        if r is None or not r.ok:
            return False, "", False     # cannot see it: keep going; a lost session ends the turn
        new = [(g, p) for g, p in self.host.strangers(readings, self.known | {own_pid or -1})]
        if new:
            where = ", ".join(f"process {p} on GPU {g}" for g, p in new)
            return True, (f"another process started on {self.host.name} ({where}); gave the GPU "
                          "back after the chunk it was on"), True
        if r.free_mib is not None and r.free_mib < self.floor:
            return True, (f"GPU memory is nearly full ({r.free_mib} MiB free); gave it back so "
                          "another job does not run out"), False
        return False, "", False
