"""The local job queue (SPEC.md section 11): one directory per job, a detached worker that runs
at most two at once, and resuming after sleep, reboot or Claude's usage limit.

  $XDG_STATE_HOME/papercast/
    jobs/<job_id>/job.json     the job's state (below); written atomically, under .job.lock
                  run.lock     flock held by the job's process while it runs
                  log          that process's stdout and stderr
                  cancel       present once `papercast cancel` asked for it
                  source.pdf   a PDF input, copied (the original may move; the agent reads here)
                  ...          whatever the pipeline writes (paper.json, script.md, ...)
    worker.lock                flock held by the worker; holds its pid
    worker.log
    pause.json                 Claude's usage limit: no job starts before `until`

The worker (`papercast worker`, started detached by `add` and by any later command when jobs
are waiting and no worker holds worker.lock) starts each job as its own process
(`python -m papercast_cli _job <dir>`, own session, niced as the worker) which calls
    papercast_cli.pipeline.run.run_job(job_dir, api, progress)
A job process holds run.lock while it runs, so a worker that died (killed, reboot) is told apart
from a job still running: a new worker adopts a job whose lock is held, and runs again a job
whose lock is free but whose state still says running. The pipeline resumes from the files
already in the job dir. Locks are flock(2): released by the OS when a process dies, on Linux
and macOS alike, so no pid is ever trusted after a reboot.

job.json states: queued -> running -> done | failed | cancelled, and on the way
  limited  Claude's usage limit: waits for pause.json's time, then queued again
  asking   the pipeline needs the person (NeedsAnswer): `retry --yes` or `cancel`
"""
from __future__ import annotations

import errno
import fcntl
import hashlib
import importlib
import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import time
import traceback
from contextlib import contextmanager
from pathlib import Path

from . import __version__
from . import config
from . import util
from .errors import (ApiError, Cancelled, ClaudeNotReady, NeedsAnswer, PapercastError,
                     UsageLimit)

PIPELINE = "papercast_cli.pipeline.run:run_job"
MAX_PARALLEL = 2                    # SPEC section 11: at most 2 jobs at once
MAX_INTERRUPTIONS = 5               # a job whose process keeps dying stops being restarted
NICE = 10
NO_RESET_S = 3600                   # the limit gave no reset time: wait an hour, then try
STALE_S = 300                       # a reset time this far past is not believed
MAX_AHEAD_S = 8 * 86400             # the weekly limit resets within 7 days
KEEP_FINISHED_S = 3 * 86400         # `status` without --all hides jobs finished before this
EXIT_BUSY = 75                      # a job process found its job already running

PENDING = ("queued", "running", "limited")
FINAL = ("done", "failed", "cancelled")
HUB_FINAL = ("ready", "rejected", "failed")

JOB_FILE, RUN_LOCK, STATE_LOCK, LOG, CANCEL = "job.json", "run.lock", ".job.lock", "log", "cancel"
# job.json keys the queue owns; progress() may set any other (title, paper_id, claim_id, ...).
OWNED = frozenset({"id", "state", "attempts", "interruptions", "pid", "created_at", "created_t",
                   "updated_at",
                   "started_at", "finished_at", "error", "question", "resume_at", "input", "kind",
                   "source", "source_name", "source_sha256", "yes", "version_of", "model",
                   "client_version", "claude_bin", "announce"})

NOT_INSTALLED = ("Claude Code is not installed here (no `claude` on PATH). papercast runs Claude "
                 "Code on this computer under your own Claude login. Install it "
                 "(https://code.claude.com/docs/en/setup), run `claude` once to log in, then try "
                 "again.")
NOT_LOGGED_IN = ("Claude Code is not logged in. Run `claude` and log in (/login), or run "
                 "`claude auth login`, then try again.")


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


def poll_s() -> float:
    return _env_float("PAPERCAST_POLL_S", 2.0)


def limit_margin_s() -> float:
    """After Claude's reset time, before jobs go on (Leo's runner: a minute)."""
    return _env_float("PAPERCAST_LIMIT_MARGIN_S", 60.0)


# --------------------------------------------------------------------------- locks

def try_lock(path: Path, wait: float = 0.0) -> int | None:
    """An exclusive flock on `path` (created 600), as an open fd; None if another process holds
    it. `wait`: keep trying this long (a `status` peeking at the lock holds it for a moment)."""
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o600)
    deadline = time.monotonic() + wait
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except OSError as e:
            if e.errno not in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES):
                os.close(fd)
                raise
        if time.monotonic() >= deadline:
            os.close(fd)
            return None
        time.sleep(0.05)


def unlock(fd: int | None) -> None:
    if fd is None:
        return
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def lock_held(path: Path) -> bool:
    """Is the flock on `path` held by some process (never by this peek)?"""
    if not path.exists():
        return False
    fd = try_lock(path)
    if fd is None:
        return True
    unlock(fd)
    return False


@contextmanager
def _locked(path: Path):
    """A short critical section (read-modify-write of a JSON file) across processes."""
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


# --------------------------------------------------------------------------- job files

def job_dir(job_id: str) -> Path:
    return config.jobs_dir() / job_id


def read(d: Path) -> dict | None:
    try:
        v = json.loads((Path(d) / JOB_FILE).read_text(encoding="utf-8"))
        return v if isinstance(v, dict) else None
    except (OSError, ValueError):
        return None


def read_job(job_dir) -> dict:
    """job.json of `job_dir` (for the pipeline): {} when missing."""
    return read(Path(job_dir)) or {}


def _write(d: Path, job: dict) -> None:
    job["updated_at"] = util.now_iso()
    config.atomic_write(d / JOB_FILE, json.dumps(job, indent=1, sort_keys=True) + "\n")


@contextmanager
def edit(d: Path):
    """with edit(d) as job: job["x"] = 1   (saved on exit, under the job's state lock)"""
    d = Path(d)
    with _locked(d / STATE_LOCK):
        job = read(d)
        if job is None:
            raise PapercastError(f"{d / JOB_FILE} is missing or broken")
        yield job
        _write(d, job)


def update(d: Path, **fields) -> dict:
    with edit(d) as job:
        job.update(fields)
    return job


def list_jobs() -> list[dict]:
    root = config.jobs_dir()
    if not root.is_dir():
        return []
    out = []
    for d in root.iterdir():
        if d.is_dir():
            j = read(d)
            if j and j.get("id") == d.name:
                out.append(j)
    # created_t orders jobs added in the same second (created_at has whole seconds).
    out.sort(key=lambda j: (j.get("created_at") or "", j.get("created_t") or 0, j["id"]))
    return out


def find(ref: str) -> dict:
    """A job by its id or an unambiguous start of it."""
    ref = (ref or "").strip()
    jobs = list_jobs()
    exact = [j for j in jobs if j["id"] == ref]
    if exact:
        return exact[0]
    hits = [j for j in jobs if ref and j["id"].startswith(ref)]
    if len(hits) == 1:
        return hits[0]
    if not hits:
        raise PapercastError(f"No job {ref!r}. papercast status --all lists them.")
    raise PapercastError(f"{ref!r} could be {', '.join(j['id'] for j in hits)}: give more of it")


def _new_id() -> str:
    alphabet = "abcdefghijklmnopqrstuvwxyz234567"
    while True:
        jid = "j" + "".join(secrets.choice(alphabet) for _ in range(6))
        if not job_dir(jid).exists():
            return jid


# --------------------------------------------------------------------------- inputs

_ARXIV_NEW = r"(\d{4}\.\d{4,5})(?:v\d+)?"
_ARXIV_OLD = r"([a-z][a-z.-]*/\d{7})(?:v\d+)?"
_ARXIV_URL = re.compile(r"^https?://(?:www\.|export\.)?(?:arxiv\.org|alphaxiv\.org)/"
                        r"(?:abs|pdf|html|overview)/(?:" + _ARXIV_NEW + "|" + _ARXIV_OLD +
                        r")(?:\.pdf)?/?(?:[?#].*)?$", re.I)
_DOI = re.compile(r"^(10\.\d{4,9}/\S+)$")
_DOI_URL = re.compile(r"^https?://(?:dx\.)?doi\.org/(10\.\d{4,9}/\S+)$", re.I)


def parse_input(s: str) -> dict:
    """What `add` was given: {"input", "kind": "pdf"|"url", "path"|"url", "arxiv_id", "doi"}. A PDF
    path, a web address, an arXiv id (2210.02747, arXiv:2210.02747) or a DOI."""
    raw = s
    s = (s or "").strip()
    if not s:
        raise PapercastError("An empty input: give a PDF, a web address, an arXiv id or a DOI")
    p = Path(s).expanduser()
    if p.exists():
        if p.is_dir():
            raise PapercastError(f"{s} is a folder: give a PDF file")
        try:
            with open(p, "rb") as fh:
                head = fh.read(1024)
        except OSError as e:
            raise PapercastError(f"Cannot read {s}: {e.strerror}")
        if b"%PDF-" not in head:
            raise PapercastError(f"{s} is not a PDF")
        return {"input": raw, "kind": "pdf", "path": str(p.resolve()), "name": p.name,
                "arxiv_id": None, "doi": None}
    m = re.fullmatch(r"(?:arxiv:)?(?:" + _ARXIV_NEW + "|" + _ARXIV_OLD + ")", s, re.I)
    if m:
        aid = m.group(1) or m.group(2)
        return {"input": raw, "kind": "url", "url": f"https://arxiv.org/abs/{aid}",
                "arxiv_id": aid, "doi": None}
    m = _DOI.match(re.sub(r"^doi:\s*", "", s, flags=re.I))
    if m and not s.lower().startswith("http"):
        doi = m.group(1).lower()
        return {"input": raw, "kind": "url", "url": f"https://doi.org/{doi}", "arxiv_id": None,
                "doi": doi}
    if re.match(r"^https?://[^\s/]+", s, re.I):
        m = _ARXIV_URL.match(s)
        aid = (m.group(1) or m.group(2)) if m else None
        m = _DOI_URL.match(s)
        doi = m.group(1).lower() if m else None
        return {"input": raw, "kind": "url", "url": s, "arxiv_id": aid, "doi": doi}
    if "/" in s or s.lower().endswith(".pdf"):
        raise PapercastError(f"No such file: {s}")
    raise PapercastError(f"{s!r} is not a file, a web address, an arXiv id or a DOI")


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def keys_of(inp: dict) -> dict:
    """The identity keys known before the agent reads the paper (for lookup and dedupe)."""
    if inp.get("kind") == "pdf" and not inp.get("source_sha256"):
        inp["source_sha256"] = _sha256(inp["path"])
    return {k: v for k, v in (("arxiv_id", inp.get("arxiv_id")), ("doi", inp.get("doi")),
                              ("sha256", inp.get("source_sha256"))) if v}


def duplicate_of(inp: dict) -> dict | None:
    """A job here for the same paper that is still on its way (queued, running, waiting for
    the limit or for an answer). A finished one is the hub's business: `add` asks it."""
    keys = keys_of(inp)
    for j in list_jobs():
        if j.get("state") not in PENDING + ("asking",):
            continue
        if (keys.get("sha256") and j.get("source_sha256") == keys["sha256"]) or \
                (keys.get("arxiv_id") and j.get("arxiv_id") == keys["arxiv_id"]) or \
                (keys.get("doi") and j.get("doi") == keys["doi"]) or \
                (inp.get("url") and j.get("url") == inp.get("url")):
            return j
    return None


def create(inp: dict, *, version_of: str | None = None, model: str | None = None,
           yes: bool = False, announce: dict | None = None) -> dict:
    """A new queued job for one parsed input (a PDF is copied into the job dir). `announce`:
    add's answer to "Post to #channel when it's ready?", {"slack": bool}, for the manifest."""
    config.private_dir(config.jobs_dir())
    jid = _new_id()
    d = job_dir(jid)
    d.mkdir(mode=0o700)
    t = time.time()
    job = {"id": jid, "client_version": __version__, "created_at": util.now_iso(t),
           "created_t": t, "input": inp["input"], "kind": inp["kind"], "url": inp.get("url"),
           "arxiv_id": inp.get("arxiv_id"), "doi": inp.get("doi"),
           "source": None, "source_name": None, "source_sha256": None,
           "version_of": version_of, "yes": bool(yes), "model": model, "announce": announce,
           "device": config.load().get("device") or config.default_device(),
           "claude_bin": shutil.which("claude"),
           "state": "queued", "phase": None, "progress": None, "detail": None,
           "title": None, "paper_id": version_of, "claim_id": None, "episode_id": None,
           "hub_state": None, "attempts": 0, "interruptions": 0, "pid": None,
           "started_at": None, "finished_at": None, "error": None, "question": None,
           "resume_at": None}
    try:
        if inp["kind"] == "pdf":
            shutil.copyfile(inp["path"], d / "source.pdf")
            os.chmod(d / "source.pdf", 0o600)
            job.update(source="source.pdf", source_name=inp.get("name"),
                       source_sha256=inp.get("source_sha256") or _sha256(str(d / "source.pdf")))
        _write(d, job)
    except BaseException:
        shutil.rmtree(d, ignore_errors=True)
        raise
    return job


def label(job: dict) -> str:
    """What the person calls this job: its title once known, else what they typed."""
    return job.get("title") or job.get("source_name") or job.get("input") or job["id"]


# --------------------------------------------------------------------------- claude

def claude_status(timeout: float = 30.0) -> tuple[bool | None, str]:
    """(True, "") when `claude` is on PATH and logged in; (False, what to do) when not; (None,
    why) when `claude auth status --json` gave no answer (as Leo's runner asks it)."""
    exe = shutil.which("claude")
    if not exe:
        return False, NOT_INSTALLED
    # Not inherited from a Claude Code session this may run inside of.
    env = {k: v for k, v in os.environ.items()
           if k != "CLAUDECODE" and not k.startswith("CLAUDE_CODE_")}
    try:
        r = subprocess.run([exe, "auth", "status", "--json"], capture_output=True, text=True,
                           timeout=timeout, env=env, stdin=subprocess.DEVNULL,
                           cwd=os.path.expanduser("~"))
    except (OSError, subprocess.SubprocessError) as e:
        return None, f"`claude auth status` did not run ({e})"
    try:
        d = json.loads(r.stdout or "")
    except ValueError:
        d = None
    if not isinstance(d, dict) or "loggedIn" not in d:
        return None, ("`claude auth status --json` gave no answer (an old Claude Code? "
                      "`claude update`)")
    return (True, "") if d.get("loggedIn") else (False, NOT_LOGGED_IN)


def check_claude(warn=None) -> None:
    """Raise ClaudeNotReady saying what to do; an unclear answer is only warned about."""
    ok, msg = claude_status()
    if ok is False:
        raise ClaudeNotReady(msg)
    if ok is None and warn:
        warn(f"Could not tell whether Claude Code is logged in: {msg}. Going on.")


# --------------------------------------------------------------------------- the usage limit

def _pause_path() -> Path:
    return config.state_dir() / "pause.json"


def read_pause() -> dict | None:
    try:
        v = json.loads(_pause_path().read_text(encoding="utf-8"))
        return v if isinstance(v, dict) and isinstance(v.get("until"), (int, float)) else None
    except (OSError, ValueError):
        return None


def pause_until(now: float | None = None) -> float | None:
    """When the Claude-limit pause ends, while it holds; else None."""
    p = read_pause()
    now = time.time() if now is None else now
    return float(p["until"]) if p and p["until"] > now else None


def set_pause(resume_at: float | None, detail: str = "", now: float | None = None) -> float:
    """Pause every job until Claude's reset plus a minute (an hour when no usable reset time was
    given). A guess never replaces a real time; a later real time extends. Returns `until`."""
    now = time.time() if now is None else now
    guessed = resume_at is None or resume_at < now - STALE_S or resume_at > now + MAX_AHEAD_S
    until = now + NO_RESET_S if guessed else max(resume_at, now) + limit_margin_s()
    config.private_dir(config.state_dir())
    with _locked(config.state_dir() / ".pause.lock"):
        cur = read_pause()
        if cur and cur["until"] > now:
            if guessed and not cur.get("guessed"):
                until, guessed = cur["until"], False
            elif not guessed and not cur.get("guessed"):
                until = max(until, cur["until"])
            elif guessed:
                until = cur["until"]
        config.atomic_write(_pause_path(), json.dumps({
            "until": until, "until_iso": util.now_iso(until), "resets_at": resume_at,
            "guessed": guessed, "since": now, "detail": (detail or "")[:300]}, indent=1) + "\n")
    return until


def clear_pause() -> None:
    try:
        _pause_path().unlink()
    except FileNotFoundError:
        pass


def limit_text(until: float, now: float | None = None) -> str:
    return f"Claude limit · resumes {util.local_hhmm(until, now)}"


# --------------------------------------------------------------------------- the worker

def _worker_lock() -> Path:
    return config.state_dir() / "worker.lock"


def worker_pid() -> int | None:
    """The running worker's pid (-1 when one runs but its pid is unreadable), else None."""
    p = _worker_lock()
    if not lock_held(p):
        return None
    try:
        return int(json.loads(p.read_text(encoding="utf-8"))["pid"])
    except (OSError, ValueError, KeyError, TypeError):
        return -1


def _any_pending() -> bool:
    return any(j.get("state") in PENDING for j in list_jobs())


_spawned: list[subprocess.Popen] = []


def spawn_worker() -> int:
    """Start `papercast worker` detached: its own session (a closing terminal's SIGHUP never
    reaches it), stdin from /dev/null, output to worker.log. It renices itself."""
    sd = config.private_dir(config.state_dir())
    logp = sd / "worker.log"
    try:
        if logp.stat().st_size > 2 * 1024 * 1024:
            os.replace(logp, sd / "worker.log.1")
    except OSError:
        pass
    with open(logp, "ab") as log:
        p = subprocess.Popen([sys.executable, "-m", "papercast_cli", "worker"], cwd=str(sd),
                             stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                             start_new_session=True, close_fds=True)
    for q in list(_spawned):                     # reap earlier ones (a long-lived caller)
        if q.poll() is not None:
            _spawned.remove(q)
    _spawned.append(p)
    return p.pid


def ensure_worker() -> bool:
    """Start a worker when jobs wait and none runs (every papercast command calls this, so a job
    interrupted by sleep, reboot or a killed worker goes on). True when one was started."""
    if not _any_pending() or lock_held(_worker_lock()):
        return False
    spawn_worker()
    return True


def _renice(target: int = NICE) -> None:
    try:
        inc = target - os.nice(0)
        if inc > 0:
            os.nice(inc)
    except OSError:
        pass


def _log(msg: str) -> None:
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}", flush=True)


class Worker:
    """Runs queued jobs, at most MAX_PARALLEL at once, until none is left."""

    def __init__(self, out=print):
        self.out = out
        self.children: dict[str, subprocess.Popen] = {}
        self.rc: dict[str, int] = {}
        self.stopping = False

    def run(self) -> int:
        config.private_dir(config.state_dir())
        fd = try_lock(_worker_lock(), wait=1.0)
        if fd is None:
            self.out(f"A papercast worker is already running (pid {worker_pid()}).")
            return 0
        self._mark(fd)
        _renice()
        signal.signal(signal.SIGTERM, self._on_term)
        try:
            signal.signal(signal.SIGHUP, signal.SIG_IGN)
        except (AttributeError, ValueError):
            pass
        _log(f"worker {os.getpid()} up (papercast {__version__})")
        try:
            while not self.stopping:
                if self.tick():
                    time.sleep(poll_s())
                    continue
                # Nothing left. Let go of the lock first, then look once more: a job added
                # between the last look and now saw the lock held and started no worker.
                unlock(fd)
                fd = None
                if not _any_pending() and not self.children:
                    break
                fd = try_lock(_worker_lock(), wait=1.0)
                if fd is None:
                    break                            # another worker took over
                self._mark(fd)
        finally:
            unlock(fd)
        _log(f"worker {os.getpid()} done")
        return 0

    def _on_term(self, *_):
        # Job processes are their own sessions and go on; the next worker adopts them.
        self.stopping = True

    @staticmethod
    def _mark(fd: int) -> None:
        data = json.dumps({"pid": os.getpid(), "started_at": util.now_iso()}).encode()
        os.ftruncate(fd, 0)
        os.pwrite(fd, data, 0)

    # ------------------------------------------------------------------ one look at the queue
    def tick(self) -> bool:
        """Start what may start. True while any job is still pending."""
        for jid, p in list(self.children.items()):
            if p.poll() is not None:                 # reap: no zombies, and its exit status
                self.children.pop(jid, None)
                self.rc[jid] = p.returncode
        now = time.time()
        until = pause_until(now)
        if until is None and read_pause() is not None:
            clear_pause()
            _log("Claude limit over; jobs go on")
        running, queued, pending = 0, [], False
        for job in list_jobs():
            d = job_dir(job["id"])
            st = job.get("state")
            if st == "running":
                if self._alive(job["id"], d):
                    running += 1
                    pending = True
                    continue
                self._ended(job["id"], d)
                job = read(d) or job
                st = job.get("state")
            if st == "limited":
                if until is not None:
                    pending = True
                    continue
                job = update(d, state="queued", resume_at=None,
                             detail="resuming after the Claude limit")
                st = "queued"
            if st == "queued":
                if (d / CANCEL).exists():
                    update(d, state="cancelled", finished_at=util.now_iso())
                    continue
                pending = True
                queued.append(job)
        if until is None:
            for job in queued:
                if running >= MAX_PARALLEL:
                    break
                self._start(job)
                running += 1
        return pending or bool(self.children)

    def _alive(self, jid: str, d: Path) -> bool:
        return jid in self.children or lock_held(d / RUN_LOCK)

    def _ended(self, jid: str, d: Path) -> None:
        """The job's process is gone while job.json still says running: it died without saying
        (killed, the machine went down) or it crashed."""
        rc = self.rc.pop(jid, None)
        with edit(d) as job:
            if job.get("state") != "running":
                return
            if (d / CANCEL).exists():
                job.update(state="cancelled", finished_at=util.now_iso(), pid=None)
                return
            if rc is not None and rc > 0 and rc != EXIT_BUSY:
                job.update(state="failed", finished_at=util.now_iso(), pid=None,
                           error=f"the job's process stopped with exit code {rc}: "
                                 f"{_last_line(d / LOG)} (log: {d / LOG})")
                return
            n = int(job.get("interruptions") or 0) + 1
            if n > MAX_INTERRUPTIONS:
                job.update(state="failed", finished_at=util.now_iso(), pid=None,
                           error=f"stopped {n} times before finishing (log: {d / LOG})")
                return
            job.update(state="queued", interruptions=n, pid=None,
                       detail="interrupted; resuming")
        _log(f"{jid}: its process ended without finishing; queued again ({n})")

    def _start(self, job: dict) -> None:
        jid = job["id"]
        d = job_dir(jid)
        with edit(d) as j:
            j.update(state="running", attempts=int(j.get("attempts") or 0) + 1,
                     started_at=util.now_iso(), pid=None, error=None, question=None)
        with open(d / LOG, "ab") as log:
            p = subprocess.Popen([sys.executable, "-m", "papercast_cli", "_job", str(d)],
                                 cwd=str(d), stdin=subprocess.DEVNULL, stdout=log,
                                 stderr=subprocess.STDOUT, start_new_session=True,
                                 close_fds=True)
        self.children[jid] = p
        self.rc.pop(jid, None)
        _log(f"{jid}: started (pid {p.pid}): {label(job)}")


def _last_line(path: Path, limit: int = 4000) -> str:
    try:
        with open(path, "rb") as fh:
            fh.seek(0, 2)
            fh.seek(max(0, fh.tell() - limit))
            lines = [l for l in fh.read().decode("utf-8", "replace").splitlines() if l.strip()]
        return lines[-1][:300] if lines else ""
    except OSError:
        return ""


# --------------------------------------------------------------------------- one job's process

class _Stop(BaseException):
    """SIGTERM. A BaseException, so the pipeline's `except Exception` does not swallow it."""


class Progress:
    """The pipeline's window onto its job:

        progress(phase=None, fraction=None, detail=None, **info)

    phase: a short word `status` shows ("reading", "writing", "checking", "uploading", ...);
    fraction: 0..1 of the whole job, or None; detail: one short line (a new phase without one
    clears the old one); info: facts for status and for a resumed run, stored in job.json
    (title, paper_id, claim_id, episode_id, ...; not the queue's own keys). Raises Cancelled
    when `papercast cancel` asked for it, so a long step stops at its next report.
    `progress.job` is job.json as it is now."""

    def __init__(self, job_dir):
        self.dir = Path(job_dir)

    @property
    def job(self) -> dict:
        return read(self.dir) or {}

    def cancelled(self) -> bool:
        return (self.dir / CANCEL).exists()

    def __call__(self, phase: str | None = None, fraction: float | None = None,
                 detail: str | None = None, **info) -> None:
        if self.cancelled():
            raise Cancelled("cancelled")
        bad = OWNED.intersection(info)
        if bad:
            raise ValueError(f"progress() may not set {', '.join(sorted(bad))}")
        with edit(self.dir) as job:
            if phase is not None:
                if phase != job.get("phase") or detail is not None:
                    job["detail"] = None if detail is None else str(detail)[:300]
                job["phase"] = str(phase)
            elif detail is not None:
                job["detail"] = str(detail)[:300]
            if fraction is not None:
                job["progress"] = max(0.0, min(1.0, float(fraction)))
            job.update(info)


def _load_pipeline():
    spec = os.environ.get("PAPERCAST_PIPELINE") or PIPELINE
    mod, _, fn = spec.partition(":")
    try:
        m = importlib.import_module(mod)
    except ModuleNotFoundError as e:
        if e.name and mod.startswith(e.name):
            raise PapercastError(f"This papercast has no pipeline ({mod} is missing): reinstall "
                                 "papercast")
        raise
    return getattr(m, fn or "run_job")


def run_one(job_dir) -> int:
    """The job's process (`python -m papercast_cli _job <dir>`): run the pipeline once and write
    how it ended. Exit status 0 whatever the pipeline did (job.json says it)."""
    from .api import Api
    d = Path(job_dir)
    fd = try_lock(d / RUN_LOCK, wait=2.0)
    if fd is None:
        print(f"{d.name} is already running", flush=True)
        return EXIT_BUSY
    try:
        job = read(d)
        if job is None:
            print(f"{d / JOB_FILE} is missing or broken", flush=True)
            return 1
        if (d / CANCEL).exists():
            update(d, state="cancelled", finished_at=util.now_iso(), pid=None)
            return 0
        signal.signal(signal.SIGTERM, _raise_stop)
        try:
            signal.signal(signal.SIGHUP, signal.SIG_IGN)
        except (AttributeError, ValueError):
            pass
        _renice()
        job = update(d, state="running", pid=os.getpid())
        _log(f"{job['id']} attempt {job.get('attempts')}: {job.get('input')}")
        _path_hint(job)
        try:
            ok, msg = claude_status()
            if ok is False:
                raise ClaudeNotReady(msg)
            api = Api.from_config()
            fn = _load_pipeline()
            result = fn(str(d), api, Progress(d))
        except UsageLimit as e:
            until = set_pause(e.resume_at, e.detail)
            update(d, state="limited", pid=None, resume_at=util.now_iso(until),
                   detail=e.detail[:300] if e.detail else None)
            _log(f"{job['id']}: Claude usage limit; waits until {util.now_iso(until)}")
        except NeedsAnswer as e:
            update(d, state="asking", pid=None, question=e.question,
                   ask_paper_id=e.paper_id)
            _log(f"{job['id']}: asks: {e.question}")
        except (Cancelled, _Stop, KeyboardInterrupt) as e:
            if isinstance(e, Cancelled) or (d / CANCEL).exists():
                update(d, state="cancelled", pid=None, finished_at=util.now_iso())
                _log(f"{job['id']}: cancelled")
            else:
                # Stopped from outside (shutdown, logout): not the person's wish; go on later.
                with edit(d) as j:
                    j.update(state="queued", pid=None, detail="stopped; resumes",
                             interruptions=int(j.get("interruptions") or 0) + 1)
                _log(f"{job['id']}: stopped by a signal; queued again")
        except (ApiError, PapercastError) as e:
            update(d, state="failed", pid=None, error=str(e), finished_at=util.now_iso())
            _log(f"{job['id']}: failed: {e}")
        except Exception as e:                          # noqa: BLE001  (the pipeline's bug)
            traceback.print_exc()
            update(d, state="failed", pid=None, finished_at=util.now_iso(),
                   error=f"{type(e).__name__}: {e} (log: {d / LOG})")
        else:
            r = result if isinstance(result, dict) else {}
            with edit(d) as j:
                j.update(state="done", pid=None, phase="uploaded", progress=1.0, detail=None,
                         error=None, finished_at=util.now_iso(),
                         episode_id=r.get("episode_id") or j.get("episode_id"),
                         paper_id=r.get("paper_id") or j.get("paper_id"),
                         hub_state=r.get("state") or j.get("hub_state"), result=r or None)
            _log(f"{job['id']}: done: episode {r.get('episode_id')}")
        return 0
    finally:
        unlock(fd)


def _raise_stop(*_):
    raise _Stop()


def _path_hint(job: dict) -> None:
    """A worker started from a login item may have a bare PATH: then look where `add` found
    claude."""
    b = job.get("claude_bin")
    if b and not shutil.which("claude") and os.path.exists(b):
        os.environ["PATH"] = os.path.dirname(b) + os.pathsep + os.environ.get("PATH", "")


# --------------------------------------------------------------------------- the person's actions

def cancel(job: dict, api=None, grace_s: float = 10.0) -> str:
    """Stop a job for good (its process group is sent SIGTERM, then SIGKILL)."""
    jid, d = job["id"], job_dir(job["id"])
    st = job.get("state")
    if st == "done":
        raise PapercastError(f"{jid} is already uploaded (episode {job.get('episode_id')}); "
                             "delete the episode on the web page if you do not want it.")
    if st == "cancelled":
        return f"{jid} was already cancelled."
    (d / CANCEL).touch()
    if lock_held(d / RUN_LOCK):
        pid = (read(d) or job).get("pid")
        if pid:
            _signal_group(int(pid), signal.SIGTERM)
            deadline = time.monotonic() + grace_s
            while lock_held(d / RUN_LOCK) and time.monotonic() < deadline:
                time.sleep(0.1)
            if lock_held(d / RUN_LOCK):
                _signal_group(int(pid), signal.SIGKILL)
                time.sleep(0.3)
    with edit(d) as j:
        if j.get("state") != "cancelled":
            j.update(state="cancelled", pid=None, finished_at=util.now_iso())
        claim = j.get("claim_id") if not j.get("episode_id") else None
    if claim and api is not None:
        try:
            api.release_claim(claim)
        except (ApiError, PapercastError):
            pass                                  # it expires on the hub after 6 h
    return f"Cancelled {jid} ({label(job)})."


def _signal_group(pid: int, sig) -> None:
    try:
        os.killpg(pid, sig)               # the job process leads its own session and group
    except OSError:
        try:
            os.kill(pid, sig)
        except OSError:
            pass


def retry(job: dict, yes: bool = False) -> dict:
    """Queue a failed, cancelled, asking or limited job again (it resumes from its files). A job
    the hub rejected is made again from scratch as a new job. Returns the queued job."""
    jid, d = job["id"], job_dir(job["id"])
    st = job.get("state")
    if st == "done":
        if job.get("hub_state") not in ("rejected", "failed"):
            raise PapercastError(f"{jid} is already uploaded (episode {job.get('episode_id')}).")
        inp = {"input": job.get("input"), "kind": job.get("kind"), "url": job.get("url"),
               "arxiv_id": job.get("arxiv_id"), "doi": job.get("doi")}
        if job.get("kind") == "pdf":
            inp.update(path=str(d / (job.get("source") or "source.pdf")),
                       name=job.get("source_name"), source_sha256=job.get("source_sha256"))
        new = create(inp, version_of=job.get("paper_id") or job.get("version_of"),
                     model=job.get("model"), yes=True, announce=job.get("announce"))
        update(d, superseded_by=new["id"])
        return new
    if st in ("running", "queued"):
        raise PapercastError(f"{jid} is already {st}.")
    try:
        (d / CANCEL).unlink()
    except FileNotFoundError:
        pass
    if st == "limited":
        clear_pause()                     # the person says: try now
    with edit(d) as j:
        j.update(state="queued", error=None, question=None, detail=None, finished_at=None,
                 resume_at=None, interruptions=0, yes=bool(j.get("yes") or yes))
        if yes and st == "asking" and j.get("ask_paper_id") and not j.get("version_of"):
            j["version_of"] = j["ask_paper_id"]     # yes = my own version of that paper
    return read(d) or job


# --------------------------------------------------------------------------- what status shows

def hub_text(ep: dict | None, server: str, job: dict) -> str:
    """The hub's side of an uploaded job, in words."""
    if not ep:
        return "uploaded" + (f" (episode {job['episode_id']})" if job.get("episode_id") else "")
    st = ep.get("state")
    if st == "checking":
        return "uploaded · the hub is checking it"
    if st == "rejected":
        rep = ep.get("check_report")
        if isinstance(rep, str):
            try:
                rep = json.loads(rep)
            except ValueError:
                rep = [rep]
        reasons = [str(r) for r in (rep or []) if str(r).strip()]
        text = "rejected by the hub"
        if reasons:
            text += ": " + "; ".join(r if len(r) < 240 else r[:237] + "..." for r in reasons[:3])
            if len(reasons) > 3:
                text += f" (+{len(reasons) - 3} more)"
        return text + f"\n  papercast retry {job['id']} makes it again"
    if st == "waiting-for-gpu":
        pos = ep.get("queue_position")
        return "waiting for the voice" + (f" (#{pos} in the queue)" if pos else "")
    if st == "speaking":
        p = ep.get("progress")
        pct = f" · {int(float(p) * 100)}%" if isinstance(p, (int, float)) else ""
        return f"being voiced{pct}"
    if st == "ready":
        return f"ready · {page_link(ep, server, job)}"
    if st == "failed":
        return "the voice failed" + (f": {ep['state_detail']}" if ep.get("state_detail") else "")
    return str(st or "uploaded")


def page_link(ep: dict, server: str, job: dict) -> str:
    """The episode's page: the hub's own `url` when it sends one, else its page's #p=<paper>
    (Leo's page links a paper that way)."""
    url = ep.get("url") or ep.get("page_url")
    if url:
        return url if "://" in str(url) else server.rstrip("/") + "/" + str(url).lstrip("/")
    pid = ep.get("paper_id") or job.get("paper_id")
    return f"{server.rstrip('/')}/#p={pid}" if pid else server


def state_text(job: dict, *, ep: dict | None = None, server: str = "",
               until: float | None = None, busy: int = 0, now: float | None = None) -> str:
    """One line (sometimes two) for `papercast status`."""
    st = job.get("state")
    now = time.time() if now is None else now
    if st == "queued":
        if until:
            return "queued · " + limit_text(until, now)
        extra = " · " + job["detail"] if job.get("detail") else ""
        return "queued" + (f" · waits for a free slot ({MAX_PARALLEL} at once)"
                           if busy >= MAX_PARALLEL else "") + extra
    if st == "running":
        bits = [job.get("phase") or "starting"]
        if isinstance(job.get("progress"), (int, float)):
            bits.append(f"{int(job['progress'] * 100)}%")
        if job.get("detail"):
            bits.append(job["detail"])
        if int(job.get("attempts") or 0) > 1:
            bits.append(f"attempt {job['attempts']}")
        return " · ".join(bits)
    if st == "limited":
        u = until or util.parse_iso(job.get("resume_at"))
        return limit_text(u, now) if u else "Claude limit · resumes soon"
    if st == "asking":
        return (f"{job.get('question') or 'needs an answer'}\n"
                f"  yes: papercast retry {job['id']} --yes    no: papercast cancel {job['id']}")
    if st == "failed":
        return f"failed: {job.get('error') or '?'}\n  papercast retry {job['id']} tries again"
    if st == "cancelled":
        return "cancelled"
    if st == "done":
        return hub_text(ep, server, job)
    return str(st)


def visible(job: dict, now: float | None = None) -> bool:
    """Shown by `status` without --all: unfinished, or finished in the last three days, or
    uploaded and not yet ready or rejected."""
    st = job.get("state")
    if st not in FINAL:
        return True
    if job.get("superseded_by"):
        return False
    if st == "done" and job.get("hub_state") not in HUB_FINAL:
        return True
    t = util.parse_iso(job.get("finished_at") or job.get("updated_at"))
    now = time.time() if now is None else now
    return t is None or now - t < KEEP_FINISHED_S
