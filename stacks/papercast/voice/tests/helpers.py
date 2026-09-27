"""Test rig: a temporary state tree, a config pointing at the fake engine and the fake
nvidia-smi, and jobs run as real subprocesses (the way the runner runs them)."""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.dirname(HERE)
PY = sys.executable
FAKE_WORKER = os.path.join(HERE, "fake_worker.py")
FAKE_NVSMI = os.path.join(HERE, "fake_nvidia_smi.py")
FAKE_SSH = os.path.join(HERE, "fake_ssh.py")
FAKE_RSMI = os.path.join(HERE, "fake_remote_smi.py")
PAPER = "2026-09-26-abcdefgh"


def ffmpeg() -> str:
    if os.environ.get("PAPERCAST_TEST_FFMPEG"):
        return os.environ["PAPERCAST_TEST_FFMPEG"]
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


SCRIPT = """# How guidance steers the model

Here is how the model steers toward the material you want. At every denoising step it runs the
same network twice. Once it is told the target, and once it is told nothing at all.

The guided score equals the plain score, plus gamma times the difference between the two. With
gamma at one, that is just the conditional model. Turn gamma up, and it leans harder into the
target, trading diversity for control, which is exactly the trade the authors measure.

# What the results show

On the benchmark the guided model hits the requested property far more often than the baseline,
and the structures it makes stay stable after relaxation. The authors are careful to say where it
fails: rare elements, and targets far outside anything seen in training.
"""

TAGS = {"title": "Guidance for Materials: A Test Paper", "album": "Papers", "artist": "Ada Lovelace",
        "albumartist": "Papers", "date": "2026-09-26", "genre": "Podcast",
        "comment": f"papercast {PAPER}"}


class Rig:
    """remote_gpus=N adds a fake remote host "bs1" (fake_ssh.py) with N GPUs, all free."""

    def __init__(self, gpu: bool = True, remote_gpus: int = 0, **over):
        self._td = tempfile.TemporaryDirectory(prefix="pcv-test-")
        self.tmp = self._td.name
        self.home = os.path.join(self.tmp, "home")
        self.state = os.path.join(self.tmp, "state")
        self.fake_log = os.path.join(self.tmp, "fake.log")
        self.nvsmi = os.path.join(self.tmp, "nvsmi.json")
        self.fstate = os.path.join(self.tmp, "fstate")
        for d in (self.home, self.state, self.fstate):
            os.makedirs(d)
        self.fake_env = {"FAKE_LOG": self.fake_log, "FAKE_STATE": self.fstate,
                         "FAKE_DELAY_S": "0.02", "FAKE_SEC_PER_WORD": "0.15"}
        self.cfg = {
            "home": self.home,
            "gpu_engine": "fakegpu" if gpu else None,
            "cpu_engine": "kokoro",
            "engines": {
                "kokoro": self._spec("cpu", "kokoro", "fake-cpu-voice"),
                "fakegpu": {**self._spec("gpu", "fake", "fake-gpu-voice"), "peak_mib": 8000,
                            "threads": 1},
            },
            "cpu": {"workers": 2, "threads": 1},
            "gpu": {"poll_s": 0.2, "stable_polls": 2, "yield_polls": 2, "headroom_mib": 1024,
                    "util_max_pct": 20, "yield_other_sm_pct": 20, "yield_free_floor_mib": 256,
                    "max_ooms": 3, "line_poll_s": 0.2, "upgrade_check_s": 0.5},
            # Never the real remote host from a test.
            "hosts": {"stibnite": {"kind": "local"}, "bs1": {"enabled": False}},
            "ffmpeg": ffmpeg(),
            "nvidia_smi": FAKE_NVSMI,
            "heartbeat_s": 0.5,
            "cpu_lock": os.path.join(self.home, "run", "voice-cpu.lock"),
        }
        self.rhost = os.path.join(self.tmp, "bs1")          # the fake remote host
        self.rhome = os.path.join(self.tmp, "bs1-home")     # its lean install
        if remote_gpus:
            os.makedirs(os.path.join(self.rhome, "venv", "bin"))
            os.makedirs(self.rhost)
            os.symlink(PY, os.path.join(self.rhome, "venv", "bin", "python"))
            self.cfg["hosts"]["bs1"] = {
                "kind": "ssh", "enabled": True, "ssh": self.rhost, "ssh_cmd": FAKE_SSH,
                "run_as": None, "home": self.rhome, "gpus": list(range(remote_gpus)),
                "dtype": "fp32", "cc": None, "nice": 10, "peak_mib": 16000,
                "nvidia_smi": FAKE_RSMI, "control_dir": None, "timeout_s": 10,
                "down_backoff_s": 1, "idle_exit_s": 60, "max_failures": 3, "holdoff_s": 2,
                "path": os.environ.get("PATH", "/usr/bin:/bin"),
                "engine": {"env": {**self.fake_env,
                                   "FAKE_PIDFILE": os.path.join(self.tmp, "pid-remote"),
                                   "FAKE_GPU_PIDDIR": os.path.join(self.rhost, "procs")}}}
            self.set_remote([{} for _ in range(remote_gpus)])
        for k, v in over.items():
            self.cfg[k] = v
        self.write_cfg()
        self.set_gpu(free=12000, util=0)
        self.procs: list[subprocess.Popen] = []

    def set_remote(self, gpus: list[dict]) -> None:
        """The fake remote host's GPUs: [{"free", "total", "util", "apps": [[pid, mib]]}]."""
        path = os.path.join(self.rhost, "gpus.json")
        with open(path + ".tmp", "w") as fh:
            json.dump({"gpus": gpus}, fh)
        os.replace(path + ".tmp", path)

    def remote_env(self, **env) -> None:
        self.cfg["hosts"]["bs1"]["engine"]["env"].update({k: str(v) for k, v in env.items()})
        self.write_cfg()

    def remote_down(self, down: bool) -> None:
        path = os.path.join(self.rhost, "down")
        if down:
            open(path, "w").close()
        elif os.path.exists(path):
            os.unlink(path)

    def _spec(self, kind: str, label: str, voice: str) -> dict:
        return {"kind": kind, "label": label, "python": PY, "worker": FAKE_WORKER,
                "model_dir": None, "files": [], "voice": voice, "speed": 1.0, "max_words": 20,
                "sample_rate": 24000, "load_timeout_s": 30, "chunk_timeout_s": 30,
                "env": {**getattr(self, "fake_env", {}),
                        "FAKE_PIDFILE": os.path.join(self.tmp, f"pid-{kind}")}}

    def engine_env(self, name: str, **env) -> None:
        self.cfg["engines"][name]["env"].update({k: str(v) for k, v in env.items()})
        self.write_cfg()

    def write_cfg(self) -> None:
        self.cfg_path = os.path.join(self.tmp, "voice.json")
        with open(self.cfg_path, "w") as fh:
            json.dump(self.cfg, fh)

    def cleanup(self) -> None:
        for p in self.procs:
            if p.poll() is None:
                os.killpg(p.pid, signal.SIGKILL)
                p.wait()
        self._td.cleanup()

    def pid(self, kind: str) -> int:
        return int(read(os.path.join(self.tmp, f"pid-{kind}")))

    # GPU scenario for the fake nvidia-smi
    def set_gpu(self, **sc) -> None:
        tmp = self.nvsmi + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(sc, fh)
        os.replace(tmp, self.nvsmi)

    def nvsmi_calls(self) -> list[str]:
        try:
            with open(self.nvsmi + ".log") as fh:
                return fh.read().splitlines()
        except OSError:
            return []

    def env(self) -> dict:
        e = dict(os.environ)
        e.update(PAPERCAST_VOICE_CONFIG=self.cfg_path, PAPERCAST_VOICE_HOME=self.home,
                 FAKE_NVSMI=self.nvsmi, PYTHONPATH=SRC)
        return e

    def job(self, script: str = SCRIPT, engine: str = "auto", paper: str = PAPER,
            tags: dict | None = None, handed_over: float | None = None) -> str:
        vdir = os.path.join(self.state, paper, "voice")
        os.makedirs(vdir, exist_ok=True)
        with open(os.path.join(vdir, "script.md"), "w") as fh:
            fh.write(script)
        with open(os.path.join(vdir, "job.json"), "w") as fh:
            json.dump({"interface": "1.0", "paper_id": paper, "script": "script.md",
                       "output_dir": "out", "engine": engine, "tags": tags or TAGS}, fh)
        if handed_over is not None:
            os.utime(os.path.join(vdir, "job.json"), (handed_over, handed_over))
        return vdir

    def start(self, vdir: str) -> subprocess.Popen:
        with open(os.path.join(vdir, "voice.log"), "ab") as log:
            p = subprocess.Popen([PY, "-m", "papercast_voice", "run", vdir], cwd=vdir,
                                 env=self.env(), stdout=log, stderr=log, stdin=subprocess.DEVNULL,
                                 start_new_session=True)
        self.procs.append(p)
        return p

    def run(self, vdir: str, timeout: float = 90) -> int:
        p = self.start(vdir)
        try:
            return p.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(p.pid, signal.SIGKILL)
            raise

    def cli(self, *args: str, timeout: float = 30) -> subprocess.CompletedProcess:
        return subprocess.run([PY, "-m", "papercast_voice", *args], env=self.env(), cwd=self.tmp,
                              capture_output=True, text=True, timeout=timeout)

    def fake_calls(self) -> list[dict]:
        try:
            with open(self.fake_log) as fh:
                return [json.loads(x) for x in fh if x.strip()]
        except OSError:
            return []


def read(path: str) -> str:
    with open(path) as fh:
        return fh.read()


def sha256(path: str) -> str:
    import hashlib
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def status(vdir: str) -> dict:
    try:
        with open(os.path.join(vdir, "status.json")) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def wait_for(pred, timeout: float = 30, step: float = 0.1):
    end = time.time() + timeout
    while time.time() < end:
        v = pred()
        if v:
            return v
        time.sleep(step)
    raise AssertionError("condition not met in time")


def gone(pid: int) -> bool:
    """The process no longer runs (absent, or a zombie waiting to be reaped)."""
    try:
        with open(f"/proc/{pid}/stat") as fh:
            return fh.read().rsplit(")", 1)[1].split()[0] in ("Z", "X")
    except (OSError, IndexError):
        return True


def voice_log(vdir: str) -> str:
    try:
        with open(os.path.join(vdir, "voice.log"), errors="replace") as fh:
            return fh.read()
    except OSError:
        return ""
