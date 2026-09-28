#!/usr/bin/env python3
"""papercast-group voice worker (SPEC.md section 9): takes finished scripts from the hub, one at a
time, and voices them with Leo's papercast-voice (Breeze on this machine's GPU).

    python3 voice_worker.py [--once] [--exit-when-idle]

The loop: claim an episode (POST /api/voice/claim, the worker token), fetch its script, write the
job directory <PCG_VOICE_JOBS>/<episode_id>/voice/ {script.md, job.json} exactly as Leo's runner
hands a paper to the voice (stacks/papercast/INTERFACE.md section 10), run
`nice -n 10 papercast-voice run <dir>` detached with a cleaned environment, read its status.json,
report it (PUT /api/voice/<id>/status, also every heartbeat so the hub keeps the claim while the
voice waits for the GPU), upload out/episode.mp3 (PUT /api/voice/<id>/audio, X-Duration-S), or
report the failure (POST /api/voice/<id>/failed).

Restarts: the episode being voiced is in <PCG_WORKER_STATE>/current.json and its job directory is
kept, so a restarted worker carries on with the same episode without claiming again;
papercast-voice never voices a finished chunk twice. A voice process that outlived its worker
(the nohup fallback, no systemd) is adopted from status.json's pid instead of started twice.

Standard library only, Python 3.10. Everything it signals is a process group it started (or the
papercast-voice process it adopted, checked by uid and command line); the machine is shared.

Environment (deploy/install.sh writes worker.env):
  PCG_HUB_URL             http://127.0.0.1:8480
  PCG_WORKER_TOKEN_FILE   file holding the worker token (or PCG_WORKER_TOKEN)
  PAPERCAST_VOICE_CMD     ~/papercast-group/voice/bin/papercast-voice
  PCG_VOICE_JOBS          where job directories go (default $PCG_DATA/episodes, as SPEC section 3)
  PCG_WORKER_STATE        current.json and the worker's lock (default ~/papercast-group/worker)
  PCG_VOICE_ENGINE        auto (the GPU voice, waiting as long as it takes) | cpu (Kokoro now)
  PCG_POLL_S, PCG_HEARTBEAT_S, PCG_MONITOR_S   claim poll when idle (15), status at least this
                          often (60; the hub requeues a claim silent for 30 min), status.json reads (2)
"""
from __future__ import annotations

import argparse
import base64
import fcntl
import hashlib
import http.client
import json
import os
import re
import shlex
import signal
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlsplit

VERSION = "0.1"
EXIT_BUSY = 5                   # papercast-voice: another voice process holds this directory
MAX_AUDIO = 200 * 1024 * 1024   # the same ceiling Leo's runner puts on the voice's output
MAX_SPAWNS = 3                  # a voice that dies without a verdict is restarted this often
LOST = (404, 409, 410)          # the hub no longer gives this episode to us: deleted, requeued


def log(msg: str) -> None:
    t = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"{t} voice-worker: {msg}", flush=True)


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def read_json(p: Path):
    try:
        with open(p, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def write_json(p: Path, obj) -> None:
    tmp = p.with_name(p.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=1)
        fh.write("\n")
    os.replace(tmp, p)


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as fh:
        for b in iter(lambda: fh.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


class HubDown(Exception):
    """No answer from the hub (refused, timed out, reset): try again later."""


class Lost(Exception):
    """The hub says this episode is no longer ours (deleted, or its claim went back to the queue)."""


class Stopping(Exception):
    pass


class Failed(Exception):
    """This episode cannot be voiced: report it to the hub and move on."""

    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code, self.message = code, message


def voice_id(eid: str, day: str) -> str:
    """papercast-voice takes only Leo's paper ids (YYYY-MM-DD-<8 base32>, its job.py ID_RE); an
    episode id becomes one: the claim's day plus eight base32 characters of its sha256."""
    b32 = base64.b32encode(hashlib.sha256(eid.encode()).digest()).decode().lower()
    return f"{day}-{b32[:8]}"


class Config:
    def __init__(self, env=None):
        env = os.environ if env is None else env
        home = Path(env.get("HOME") or Path.home())
        base = home / "papercast-group"
        self.hub = (env.get("PCG_HUB_URL") or "http://127.0.0.1:8480").rstrip("/")
        tok = env.get("PCG_WORKER_TOKEN", "")
        tfile = env.get("PCG_WORKER_TOKEN_FILE") or str(base / "worker.token")
        if not tok and os.path.exists(tfile):
            tok = Path(tfile).read_text(encoding="utf-8").strip()
        self.token = tok
        self.voice_cmd = env.get("PAPERCAST_VOICE_CMD") or str(base / "voice" / "bin" / "papercast-voice")
        data = env.get("PCG_DATA") or str(base / "data")
        self.jobs = Path(env.get("PCG_VOICE_JOBS") or os.path.join(data, "episodes"))
        self.state = Path(env.get("PCG_WORKER_STATE") or str(base / "worker"))
        self.engine = env.get("PCG_VOICE_ENGINE", "auto")
        if self.engine not in ("auto", "cpu"):
            raise SystemExit(f"PCG_VOICE_ENGINE must be auto or cpu, not {self.engine!r}")
        self.poll_s = float(env.get("PCG_POLL_S", "15"))
        self.heartbeat_s = float(env.get("PCG_HEARTBEAT_S", "60"))
        self.monitor_s = float(env.get("PCG_MONITOR_S", "2"))
        # papercast-voice calls its own card "stibnite" in the sentences it writes (its config's
        # local host); the page should name the machine that is really speaking.
        self.host_label = env.get("PCG_VOICE_HOST_LABEL") or socket.gethostname().split(".")[0]
        self.worker_name = env.get("PCG_WORKER_NAME") or socket.gethostname()
        self.backoff_max_s = float(env.get("PCG_BACKOFF_MAX_S", "300"))
        # The voice runs with a cleaned environment, as Leo's runner starts it (INTERFACE §10.1):
        # the installed wrapper names everything it needs itself.
        self.voice_env = {"HOME": str(home), "USER": env.get("USER", "leo"), "LANG": "C.UTF-8",
                          "PATH": env.get("PCG_VOICE_PATH", "/usr/local/bin:/usr/bin:/bin")}


class Hub:
    """The hub's voice API over HTTP, with the worker token."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        u = urlsplit(cfg.hub)
        self.scheme, self.host, self.port = u.scheme, u.hostname, u.port
        self.base_path = u.path.rstrip("/")

    def _conn(self, timeout: float):
        if self.scheme == "https":
            return http.client.HTTPSConnection(self.host, self.port or 443, timeout=timeout)
        return http.client.HTTPConnection(self.host, self.port or 80, timeout=timeout)

    def request(self, method: str, path: str, body=None, headers=None, timeout: float = 30.0):
        """(status, parsed JSON or raw bytes). A file object as body is streamed."""
        if path.startswith("http://") or path.startswith("https://"):
            u = urlsplit(path)
            if (u.scheme, u.hostname, u.port) != (self.scheme, self.host, self.port):
                raise ValueError(f"refusing to send the worker token to another host: {path[:80]}")
            path = u.path + (f"?{u.query}" if u.query else "")
        else:
            path = self.base_path + path
        # X-Worker names this worker on every call, as the claim does: the hub hands a restarted
        # worker the claim it holds, and refuses another worker's updates to it.
        h = {"Authorization": f"Bearer {self.cfg.token}", "User-Agent": f"pcg-voice-worker/{VERSION}",
             "X-Worker": self.cfg.worker_name}
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
            h["Content-Type"] = "application/json"
        if isinstance(body, bytes):
            h["Content-Length"] = str(len(body))
        h.update(headers or {})
        c = self._conn(timeout)
        try:
            c.request(method, path, body=body, headers=h)
            r = c.getresponse()
            data = r.read()
        except (OSError, http.client.HTTPException) as e:
            raise HubDown(f"{method} {path}: {e.__class__.__name__}: {e}") from None
        finally:
            c.close()
        if r.status in (502, 503, 504):
            raise HubDown(f"{method} {path}: HTTP {r.status}")
        ctype = r.getheader("Content-Type", "")
        if "json" in ctype:
            try:
                return r.status, json.loads(data or b"null")
            except ValueError:
                pass
        return r.status, data

    def claim(self):
        return self.request("POST", "/api/voice/claim", {"worker": self.cfg.worker_name})

    def status(self, eid: str, body: dict):
        return self.request("PUT", f"/api/voice/{eid}/status", body)

    def failed(self, eid: str, body: dict):
        return self.request("POST", f"/api/voice/{eid}/failed", body)

    def audio(self, eid: str, path: Path, duration_s: float, sha: str):
        size = path.stat().st_size
        with open(path, "rb") as fh:
            return self.request("PUT", f"/api/voice/{eid}/audio", fh, timeout=300.0, headers={
                "Content-Type": "audio/mpeg", "Content-Length": str(size),
                "X-Duration-S": f"{duration_s:.3f}", "X-Sha256": sha})


def _err(status, body) -> str:
    if isinstance(body, dict):
        return f"HTTP {status} {body.get('error', '')}: {body.get('message', '')}".strip()
    return f"HTTP {status}"


class Worker:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.hub = Hub(cfg)
        self.stopping = False
        self.child: subprocess.Popen | None = None
        self.cur_path = cfg.state / "current.json"
        self._said: dict[str, float] = {}
        self._voice_ok_at = 0.0

    # ------------------------------------------------------------ small helpers
    def say_every(self, key: str, msg: str, every_s: float = 600.0) -> None:
        """Log a repeating condition once per `every_s`, not on every poll."""
        t = time.time()
        if t - self._said.get(key, 0) >= every_s:
            self._said[key] = t
            log(msg)

    def sleep(self, s: float) -> None:
        end = time.time() + s
        while not self.stopping and time.time() < end:
            time.sleep(min(0.2, max(0.0, end - time.time())))
        if self.stopping:
            raise Stopping()

    def voice_ready(self) -> bool:
        """`papercast-voice info` says installed (checked at most every 10 min once it did)."""
        if time.time() - self._voice_ok_at < 600:
            return True
        try:
            out = subprocess.run([*shlex.split(self.cfg.voice_cmd), "info"], capture_output=True,
                                 timeout=30, env=self.cfg.voice_env, stdin=subprocess.DEVNULL)
            info = json.loads(out.stdout.decode().strip().splitlines()[-1])
        except (OSError, ValueError, IndexError, subprocess.TimeoutExpired) as e:
            self.say_every("voice", f"papercast-voice is not usable ({self.cfg.voice_cmd}: {e}); "
                                    "not taking episodes")
            return False
        if not info.get("installed"):
            self.say_every("voice", f"papercast-voice says it is not installed: {info}")
            return False
        if self._voice_ok_at == 0:
            log(f"papercast-voice: {json.dumps(info)[:300]}")
        self._voice_ok_at = time.time()
        return True

    # ------------------------------------------------------------ the loop
    def run(self, once: bool = False, exit_when_idle: bool = False) -> int:
        self.cfg.state.mkdir(parents=True, exist_ok=True)
        lock = open(self.cfg.state / "worker.lock", "a+")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            log(f"another worker holds {self.cfg.state / 'worker.lock'}; exiting")
            return 3
        if not self.cfg.token:
            log("no worker token (PCG_WORKER_TOKEN_FILE); exiting")
            return 2
        log(f"started (hub {self.cfg.hub}, jobs {self.cfg.jobs}, engine {self.cfg.engine}, pid {os.getpid()})")
        backoff = self.cfg.poll_s
        try:
            while not self.stopping:
                job = read_json(self.cur_path)
                if job:
                    log(f"carrying on with {job['episode_id']} (claimed {job.get('claimed_at')})")
                else:
                    if not self.voice_ready():
                        if exit_when_idle:
                            return 1
                        self.sleep(min(60, self.cfg.backoff_max_s))
                        continue
                    try:
                        st, body = self.hub.claim()
                    except HubDown as e:
                        self.say_every("down", f"hub unreachable ({e}); retrying", 300)
                        self.sleep(backoff)
                        backoff = min(backoff * 2, self.cfg.backoff_max_s)
                        continue
                    backoff = self.cfg.poll_s
                    if st == 204 or (st == 200 and not (isinstance(body, dict) and body.get("episode_id"))):
                        if exit_when_idle:
                            return 0
                        self.sleep(self.cfg.poll_s)
                        continue
                    if st in (401, 403):
                        self.say_every("auth", f"the hub refused the worker token ({_err(st, body)}); "
                                               "check PCG_WORKER_TOKEN_SHA256 in hub.env", 600)
                        self.sleep(min(60, self.cfg.backoff_max_s))
                        continue
                    if st != 200:
                        self.say_every(f"claim{st}", f"claim: {_err(st, body)} (the hub's voice API "
                                                     "may not be there yet); retrying", 600)
                        self.sleep(min(60, self.cfg.backoff_max_s))
                        continue
                    job = {"episode_id": body["episode_id"], "claim": body, "claimed_at": now_iso(),
                           "spawns": 0}
                    if not re.match(r"^[A-Za-z0-9_-]{1,64}$", job["episode_id"]):
                        log(f"claim gave an episode id that is not a plain name: {job['episode_id']!r}")
                        self.sleep(min(60, self.cfg.backoff_max_s))
                        continue
                    write_json(self.cur_path, job)
                    log(f"claimed {job['episode_id']}: {body.get('title', '')!r}")
                self.do(job)
                if once:
                    return 0
        except Stopping:
            pass
        finally:
            self.stop_child()
        log("stopped")
        return 0

    def drop(self) -> None:
        try:
            os.unlink(self.cur_path)
        except FileNotFoundError:
            pass

    # ------------------------------------------------------------ one episode
    def do(self, job: dict) -> None:
        eid = job["episode_id"]
        vdir = self.cfg.jobs / eid / "voice"
        try:
            up = read_json(vdir / "uploaded.json") or {}
            if up.get("claimed_at") and up.get("claimed_at") == job.get("claimed_at"):
                log(f"{eid}: its audio was already taken by the hub ({up.get('at')})")
                self.drop()
                return
            self.prepare(job, vdir)
            while True:
                pid = self.adoptable(vdir)
                if pid:
                    log(f"{eid}: adopting the voice process already working on it (pid {pid})")
                    rc, st = self.monitor(eid, vdir, pid=pid)
                else:
                    if job.get("spawns", 0) >= MAX_SPAWNS:
                        raise Failed("voice_died", f"the voice stopped without finishing "
                                                   f"{MAX_SPAWNS} times; see voice.log")
                    job["spawns"] = job.get("spawns", 0) + 1
                    write_json(self.cur_path, job)
                    rc, st = self.monitor(eid, vdir, pid=None)
                if rc == EXIT_BUSY:
                    self.sleep(1)       # another voice process has the directory: adopt it
                    continue
                if st.get("phase") == "done":
                    self.upload(eid, vdir, st, job)
                    return
                if st.get("phase") == "failed":
                    err = st.get("error") or {}
                    if err.get("code") == "cancelled" and (vdir / "cancel").exists():
                        log(f"{eid}: cancelled")
                        self.drop()
                        return
                    raise Failed(err.get("code") or "voice_failed",
                                 err.get("message") or f"the voice exited {rc}")
                log(f"{eid}: the voice exited {rc} in phase {st.get('phase')!r} without a verdict; "
                    "running it again (finished chunks are kept)")
                self.sleep(min(5, self.cfg.backoff_max_s))
        except Lost as e:
            log(f"{eid}: no longer ours ({e}); leaving it, job directory kept")
            self.cancel_voice(vdir)
            self.drop()
        except Failed as e:
            self.cancel_voice(vdir)
            self.report_failed(eid, e.code, e.message)

    def fetch_script(self, eid: str, url: str, have_one: bool) -> str | None:
        """The script's text, or None when the hub is down and a copy is already here."""
        errors = 0
        while True:
            try:
                st, body = self.hub.request("GET", url)
            except HubDown as e:
                if have_one:
                    log(f"{eid}: hub unreachable ({e}); voicing the script already here")
                    return None
                self.say_every("down", f"hub unreachable ({e}); retrying", 300)
                self.sleep(min(30, self.cfg.backoff_max_s))
                continue
            if st == 200:
                if isinstance(body, dict):      # a JSON answer {"script": "..."} is taken too
                    body = (body.get("script") or "").encode()
                try:
                    return body.decode("utf-8")
                except UnicodeDecodeError:
                    raise Failed("script_invalid", "the script is not UTF-8") from None
            if st in LOST:
                raise Lost(f"script: {_err(st, body)}")
            if st in (401, 403):
                self.say_every("auth", f"script: the hub refused the worker token ({_err(st, body)})")
                self.sleep(min(60, self.cfg.backoff_max_s))
                continue
            errors += 1
            if errors >= 5:
                raise Failed("script_unavailable", f"the hub did not give the script: {_err(st, body)}")
            self.sleep(min(30 * errors, self.cfg.backoff_max_s))

    def prepare(self, job: dict, vdir: Path) -> None:
        """The job directory as Leo's runner writes it (INTERFACE §10.2): script.md and job.json."""
        eid = job["episode_id"]
        claim = job.get("claim") or {}
        vdir.mkdir(parents=True, exist_ok=True)
        os.chmod(vdir, 0o700)
        sp = vdir / "script.md"
        text = self.fetch_script(eid, claim.get("script_url") or f"/api/voice/{eid}/script", sp.exists())
        if text is not None:
            if not text.strip():
                raise Failed("script_invalid", "the script is empty")
            old = sp.read_text(encoding="utf-8") if sp.exists() else None
            if old != text:
                if old is not None:
                    # A different script for the same episode: what was voiced is not it.
                    log(f"{eid}: the script changed since the last run; its audio starts again")
                    for f in ("status.json", "out/episode.mp3"):
                        try:
                            os.unlink(vdir / f)
                        except FileNotFoundError:
                            pass
                tmp = vdir / "script.md.tmp"
                tmp.write_text(text, encoding="utf-8")
                os.replace(tmp, sp)
        if not (vdir / "job.json").exists():
            # Written once: papercast-voice keeps the paper's place in its GPU line by it.
            day = (job.get("claimed_at") or now_iso())[:10]
            write_json(vdir / "job.json", {
                "interface": "1.0", "paper_id": voice_id(eid, day), "script": "script.md",
                "output_dir": "out", "engine": self.cfg.engine,
                "tags": {"title": claim.get("title") or eid, "album": "Papers",
                         "artist": claim.get("first_author") or "Unknown author",
                         "albumartist": "Papers", "date": now_iso()[:10], "genre": "Podcast",
                         "comment": f"papercast-group {eid}"}})
        try:
            os.unlink(vdir / "cancel")          # a cancel from an earlier, lost claim
        except FileNotFoundError:
            pass

    def adoptable(self, vdir: Path) -> int | None:
        """The pid of a live papercast-voice process of ours working in vdir, if any."""
        st = read_json(vdir / "status.json") or {}
        pid = st.get("pid")
        if not isinstance(pid, int) or pid <= 1 or st.get("phase") in ("done", "failed"):
            return None
        try:
            if os.stat(f"/proc/{pid}").st_uid != os.getuid():
                return None
            with open(f"/proc/{pid}/cmdline", "rb") as fh:
                cmd = fh.read()
            cwd = os.path.realpath(f"/proc/{pid}/cwd")
        except OSError:
            return None
        if b"papercast_voice" not in cmd and b"papercast-voice" not in cmd:
            return None
        if str(vdir).encode() not in cmd and cwd != str(vdir.resolve()):
            return None
        return pid

    def spawn(self, vdir: Path) -> subprocess.Popen:
        logf = open(vdir / "voice.log", "ab")
        try:
            # nice and the installed wrapper both exec, so the pid is papercast-voice's own.
            p = subprocess.Popen(
                ["nice", "-n", "10", *shlex.split(self.cfg.voice_cmd), "run", str(vdir)],
                cwd=str(vdir), env=self.cfg.voice_env, stdin=subprocess.DEVNULL, stdout=logf,
                stderr=subprocess.STDOUT, start_new_session=True)
        finally:
            logf.close()
        return p

    def monitor(self, eid: str, vdir: Path, pid: int | None):
        """Run (or watch) the voice until it exits, reporting its status. (exit code, or None for
        an adopted process; the status.json this run wrote, or {})."""
        if pid is None:
            self.child = self.spawn(vdir)
            pid = self.child.pid
            log(f"{eid}: voicing (pid {pid})")
        last_sent = 0.0
        last_key = None
        try:
            while True:
                st = read_json(vdir / "status.json") or {}
                if st.get("pid") != pid:
                    st = {}                     # an earlier run's, not this one's yet
                phase = st.get("phase") or "preparing"
                total = st.get("chunks_total") or 0
                progress = round((st.get("chunks_done") or 0) / total, 4) if total else 0.0
                detail = st.get("wait_text") or st.get("note")
                if detail:
                    detail = detail.replace("stibnite", self.cfg.host_label)
                key = (phase, progress, detail)
                t = time.time()
                if phase not in ("done", "failed") and (key != last_key or t - last_sent >= self.cfg.heartbeat_s):
                    # `note` is the sentence the page shows (hub/voiceq.py reads that key);
                    # `detail` repeats it for readers of the first version of this body.
                    body = {"phase": phase, "progress": progress, "note": detail, "detail": detail,
                            "eta_s": st.get("eta_s"), "engine": st.get("engine")}
                    if self.send_status(eid, body):
                        last_sent, last_key = t, key
                if self.child is not None:
                    rc = self.child.poll()
                    if rc is not None:
                        self.child = None
                        st = read_json(vdir / "status.json") or {}
                        return rc, (st if st.get("pid") == pid else {})
                elif not os.path.exists(f"/proc/{pid}"):
                    st = read_json(vdir / "status.json") or {}
                    return None, (st if st.get("pid") == pid else {})
                self.sleep(self.cfg.monitor_s)
        except Stopping:
            self.stop_child()
            raise

    def send_status(self, eid: str, body: dict) -> bool:
        try:
            st, resp = self.hub.status(eid, body)
        except HubDown as e:
            self.say_every("down", f"hub unreachable ({e}); the voice goes on", 300)
            return False
        if st in LOST or (isinstance(resp, dict) and resp.get("cancel")):
            raise Lost(f"status: {_err(st, resp)}")
        if st >= 400:
            self.say_every(f"status{st}", f"{eid}: status refused: {_err(st, resp)}", 600)
            return False
        return True

    def cancel_voice(self, vdir: Path) -> None:
        """Ask the voice this worker started to stop (it honours `cancel` within a second), then
        make sure. An adopted process is only asked."""
        try:
            if self.child is not None or self.adoptable(vdir):
                (vdir / "cancel").touch()
        except OSError:
            pass
        if self.child is None:
            return
        try:
            self.child.wait(timeout=30)
        except subprocess.TimeoutExpired:
            pass
        self.stop_child()

    def stop_child(self) -> None:
        """TERM, then KILL, the process group of the voice this worker started (only that)."""
        p = self.child
        if p is None or p.poll() is not None:
            self.child = None
            return
        for sig, wait_s in ((signal.SIGTERM, 20), (signal.SIGKILL, 5)):
            try:
                os.killpg(p.pid, sig)
            except ProcessLookupError:
                break
            try:
                p.wait(timeout=wait_s)
                break
            except subprocess.TimeoutExpired:
                continue
        self.child = None

    # ------------------------------------------------------------ verdicts
    def retrying(self, what: str, fn):
        """Call fn until the hub answers; HubDown waits with a capped backoff."""
        wait = 10.0
        while True:
            try:
                return fn()
            except HubDown as e:
                self.say_every("down", f"{what}: hub unreachable ({e}); retrying", 300)
                self.sleep(min(wait, self.cfg.backoff_max_s))
                wait = min(wait * 2, self.cfg.backoff_max_s)

    def upload(self, eid: str, vdir: Path, st: dict, job: dict) -> None:
        out = st.get("output") or {}
        rel = out.get("path") or ""
        if not re.match(r"^out/episode\.mp3$", rel):
            raise Failed("voice_failed", f"voice output path not accepted: {rel[:80]}")
        p = vdir / rel
        if not p.is_file() or p.is_symlink() or not 0 < p.stat().st_size <= MAX_AUDIO:
            raise Failed("voice_failed", "voice output missing, empty or over 200 MB")
        sha = sha256_file(p)
        if out.get("sha256") and out["sha256"] != sha:
            raise Failed("voice_failed", "voice output checksum does not match status.json")
        dur = float(out.get("duration_s") or 0)
        log(f"{eid}: uploading {p.stat().st_size} bytes, {dur:.1f} s, "
            f"{out.get('loudness_lufs')} LUFS, {out.get('engine')}")
        for attempt in range(1, 5):
            code, resp = self.retrying("audio upload", lambda: self.hub.audio(eid, p, dur, sha))
            if code < 500:
                break
            log(f"{eid}: upload: {_err(code, resp)}; trying again")
            self.sleep(min(30 * attempt, self.cfg.backoff_max_s))
        if code in LOST:
            raise Lost(f"audio: {_err(code, resp)}")
        if code >= 400:
            # The hub refused the file itself: the episode fails with the hub's reason.
            raise Failed("upload_refused", _err(code, resp))
        write_json(vdir / "uploaded.json", {"at": now_iso(), "claimed_at": job.get("claimed_at"),
                                            "sha256": sha, "duration_s": dur, "status": code,
                                            "hub": resp if isinstance(resp, dict) else None})
        # The hub keeps the audio now (the chunks went when the voice finished: its own cleanup).
        try:
            os.unlink(p)
        except FileNotFoundError:
            pass
        log(f"{eid}: ready (HTTP {code})")
        self.drop()

    def report_failed(self, eid: str, code: str, message: str) -> None:
        log(f"{eid}: failed: {code}: {message}")
        body = {"error": f"{code}: {message}"[:1000], "code": code}
        st, resp = self.retrying("failure report", lambda: self.hub.failed(eid, body))
        if st >= 400 and st not in LOST:
            log(f"{eid}: the hub did not take the failure report: {_err(st, resp)}")
        self.drop()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--once", action="store_true", help="one episode (or none), then exit")
    ap.add_argument("--exit-when-idle", action="store_true", help="exit when the queue is empty")
    a = ap.parse_args(argv)
    w = Worker(Config())

    def stop(signum, _frame):
        w.stopping = True
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    return w.run(once=a.once, exit_when_idle=a.exit_when_idle)


if __name__ == "__main__":
    sys.exit(main())
