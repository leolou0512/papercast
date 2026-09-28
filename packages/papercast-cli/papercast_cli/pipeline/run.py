"""One job: a paper (a local PDF or a link) becomes an uploaded episode (SPEC.md section 11).

    run_job(job_dir, api, progress) -> result dict

The CLI's worker (A7) calls it. `api` has get(path) -> dict, post(path, json) -> dict and
upload(path, bytes, ctype) -> dict; an HTTP error is an exception with the status as `.status`
(or `.code`) and the answer as `.body` (a dict or its JSON text); any other exception is "the hub
cannot be reached". `progress(phase, fraction, detail)`: fraction of the whole job, 0 to 1.

What the job dir holds. The CLI writes `job.json`:
    {"input": "<path or URL>", "version_of": "p_..." | null, "yes": false,
     "model": "claude-opus-5-5" | null, "device": "<device name>" | null}
(`yes`: the person already said "make my own version" of a paper the hub has.) The pipeline
writes everything else; the agent's working directory is `work/`, the only place it can read or
write (the paper is copied or downloaded there):
    state.json             steps done, the Claude session, repair and cut state
    work/                  paper.pdf, pages/, guideline.md, paper.json, script.md,
                           explainer.json, claims.md (the agent's cwd)
    logs/run-N.jsonl .err  every Claude Code process's stream and stderr
    meta.json lookup.json claim.json prompt.json prefs.json links.json
    out/                   script.md explainer.json claims.md as checked and cut (frozen)
    cut/                   the pre-cut copies (cut.py), out of the agent's reach
    explainer.html manifest.json bundle.tar.gz upload.json result.json

Steps, each skipped when state.json says it is done, so a job killed anywhere (sleep, reboot,
a closed terminal, Claude's usage limit) runs again from where it stopped:
  source    copy the PDF (or download an arXiv link's PDF), sha256, page images
  identify  the agent writes paper.json (title, authors, year, arxiv_id, doi, url)
  claim     lookup; a new paper is claimed; an existing one needs "yes" or version_of
  prompt    the base guideline and this person's preferences, fixed for the job
  episode   script.md, explainer.json, claims.md; the checks with one repair turn; the cut pass
  explainer explainer.html from explainer.json (crops when poppler is installed)
  links     Semantic Scholar references and citations in the library, graded by haiku
  bundle    manifest.json + the files, validated (common.bundle), tar.gz
  upload    POST /api/cli/episodes; then the hub's checking state is polled

Results: {"status": "uploaded" | "rejected", "episode_id", "paper_id", "state", ...};
{"status": "needs_confirmation", "question": "made by Alice. Make your own version? [y/N]",
"paper": {...}} (the CLI asks, sets `yes` in job.json and runs the job again);
{"status": "in_progress", "by": {"name"}, "since"} (someone else is making this paper now).
Raises UsageLimit (resume_at: epoch seconds) at Claude's usage limit, PipelineError when the
job cannot finish (`retryable` when the cause is outside it).
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import tarfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone

try:
    import fcntl
except ImportError:                      # pragma: no cover - not a Unix machine
    fcntl = None

from .. import __version__
from ..common import bundle as cbundle
from ..common import checks
from ..common import prefs as cprefs
from ..common import wording
from . import claude, cut, explainer, limits, pdf, prompts, title as ptitle
from . import links as plinks
from . import tags as ptags
from .errors import PipelineError, UsageLimit, usage_limit

DEFAULT_MODEL = "claude-opus-5-5"            # Leo's runner's default
WPM = 150.0                                  # the hub's estimate: words / 150 (SPEC.md section 6)
MINUTES = (15.0, 25.0)                       # unless the base prompt says otherwise
SCRIPT_MAX = 60 * 1024                       # the hub's limit for script.md
MAX_PDF = 50 << 20
REQUIRED = ("script.md", "explainer.json", "claims.md")
WATCHDOG_S = 2700.0                          # no output this long: stopped, run again once
TRANSIENT_BACKOFF_S = [60.0, 300.0, 900.0]   # an overloaded API, the network: wait, same session
MAX_INTERRUPTS = 3                           # a turn killed this often fails the job
HUB_POLL_S = 2.0
HUB_POLL_MAX_S = 120.0
STEPS = ("source", "identify", "claim", "prompt", "episode", "explainer", "links", "bundle",
         "upload", "hub")
# Where each step starts on the job's progress bar.
AT = {"source": 0.0, "identify": 0.02, "claim": 0.06, "prompt": 0.07, "episode": 0.08,
      "checks": 0.72, "cut": 0.76, "explainer": 0.82, "links": 0.85, "bundle": 0.94,
      "upload": 0.95, "hub": 0.97}

_WORDING_LOCK = threading.RLock()


def run_job(job_dir: str, api, progress, *, fetch=None, download=None) -> dict:
    """Run (or resume) the job in `job_dir`. `fetch(url) -> (status, json)` reaches Semantic
    Scholar and `download(url, limit) -> bytes` an arXiv PDF; tests replace both."""
    return Job(job_dir, api, progress, fetch=fetch, download=download).run()


# --- small helpers -------------------------------------------------------------------------

def now_iso(t: float | None = None) -> str:
    return datetime.fromtimestamp(time.time() if t is None else t, timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s) -> float | None:
    if not isinstance(s, str) or not s:
        return None
    try:
        return datetime.fromisoformat(s.strip().replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def read_json(path: str, default=None):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def write_bytes(path: str, data: bytes, mode: int = 0o600) -> None:
    """Atomic replace."""
    d = os.path.dirname(path) or "."
    os.makedirs(d, exist_ok=True)
    tmp = os.path.join(d, f".tmp-{os.getpid()}-{uuid.uuid4().hex[:8]}")
    try:
        with open(tmp, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def write_json(path: str, obj) -> None:
    write_bytes(path, (json.dumps(obj, indent=1, ensure_ascii=False) + "\n").encode("utf-8"))


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def is_url(s: str) -> bool:
    return bool(re.match(r"(?i)^(https?://|arxiv:)", (s or "").strip()))


def named_https(url: str) -> bool:
    """An https link to one of the research sites the agent's WebFetch may reach."""
    try:
        p = urllib.parse.urlsplit(url)
    except ValueError:
        return False
    host = (p.hostname or "").lower()
    return p.scheme == "https" and any(host in (d, "www." + d) for d in claude.FETCH_DOMAINS)


def pdf_link(url: str) -> bool:
    """A named site's direct PDF link (…/paper.pdf, openreview.net/pdf?id=…)."""
    if not named_https(url):
        return False
    path = urllib.parse.urlsplit(url).path.lower()
    return path.endswith(".pdf") or path.startswith("/pdf")


class _NamedRedirects(urllib.request.HTTPRedirectHandler):
    """Redirects are followed only to https on the named sites."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not named_https(newurl):
            raise urllib.error.HTTPError(newurl, code, "redirect to a site that is not named",
                                         headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def http_download(url: str, limit: int = MAX_PDF, timeout: float = 60.0) -> bytes:
    """GET a PDF from a named site over https (see Job._source); at most `limit` bytes."""
    if not named_https(url):
        raise OSError("not an https link to a named research site")
    req = urllib.request.Request(url, headers={"User-Agent": plinks.USER_AGENT,
                                               "Accept": "application/pdf"})
    opener = urllib.request.build_opener(_NamedRedirects)
    with opener.open(req, timeout=timeout) as r:
        body = r.read(limit + 1)
    if len(body) > limit:
        raise OSError(f"larger than {limit >> 20} MB")
    return body


def _http_status(e) -> int | None:
    for a in ("status", "code", "status_code"):
        v = getattr(e, a, None)
        if isinstance(v, int) and not isinstance(v, bool) and 100 <= v < 600:
            return v
    return None


def _http_body(e) -> dict:
    for a in ("body", "data", "json", "payload", "answer"):
        v = getattr(e, a, None)
        if isinstance(v, dict):
            return v
        if isinstance(v, (str, bytes)) and v:
            with contextlib.suppress(ValueError, TypeError):
                d = json.loads(v)
                if isinstance(d, dict):
                    return d
    rd = getattr(e, "read", None)
    if callable(rd):
        with contextlib.suppress(Exception):
            d = json.loads(rd())
            if isinstance(d, dict):
                return d
    for v in getattr(e, "args", ()):
        if isinstance(v, dict):
            return v
    return {}


def parse_claims(path: str) -> dict:
    """claims.md's front matter and claims (Leo's runner, core.parse_claims)."""
    out = {"title": None, "authors": [], "claims": [], "tags": [], "year": None}
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            text = fh.read(65536)
    except OSError:
        return out
    body = text
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", text, re.S)
    if m:
        body = m.group(2)
        for line in m.group(1).splitlines():
            k, _, v = line.partition(":")
            k, v = k.strip().lower(), v.strip()
            if k == "title" and v:
                out["title"] = v.strip("\"'")[:300]
            elif k == "authors" and v.startswith("["):
                out["authors"] = [x.strip().strip("\"'") for x in v.strip("[]").split(",") if x.strip()]
            elif k == "year":
                out["year"] = ptags.clean_year(v)
            elif k == "tags" and v:
                out["tags"] = ptags.clean_tags(v)
        blk = re.search(r"^authors:\s*\n((?:\s*-\s*.+\n?)+)", m.group(1) + "\n", re.M)
        if blk and not out["authors"]:
            out["authors"] = [l.strip()[2:].strip().strip("\"'") for l in blk.group(1).splitlines() if l.strip()]
        tblk = re.search(r"^tags:\s*\n((?:[ \t]*-[ \t]*.+\n?)+)", m.group(1) + "\n", re.M)
        if tblk and not out["tags"]:
            out["tags"] = ptags.clean_tags([l.strip()[1:].strip().strip("\"'")
                                            for l in tblk.group(1).splitlines() if l.strip()])
    out["claims"] = [l.strip()[2:].strip() for l in body.splitlines() if l.strip().startswith("- ")][:3]
    out["authors"] = [a[:200] for a in out["authors"][:100]]
    return out


def _proc_command(pid: int) -> str:
    """A process's full command line: /proc on Linux, `ps -ww` elsewhere (macOS)."""
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as fh:
            return fh.read().replace(b"\0", b" ").decode("utf-8", "replace")
    except OSError:
        pass
    try:
        r = subprocess.run(["ps", "-ww", "-o", "command=", "-p", str(pid)], capture_output=True,
                           text=True, timeout=10)
        return r.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


class _Interrupted(Exception):
    """The turn's process was stopped by the watchdog."""


class _NoSession(Exception):
    """Claude Code has no transcript to resume."""


class _Transient(Exception):
    def __init__(self, why: str):
        super().__init__(why)
        self.why = why


# --- the job ---------------------------------------------------------------------------------

class Job:
    def __init__(self, job_dir: str, api, progress, fetch=None, download=None):
        self.dir = os.path.abspath(job_dir)
        self.api = api
        self._progress = progress or (lambda *a: None)
        self.fetch = fetch if fetch is not None else plinks.retrying(plinks.http_fetch)
        self.download = download if download is not None else http_download
        self.work = self.p("work")
        self.job = read_json(self.p("job.json"), {}) or {}
        self.state = read_json(self.p("state.json"), {}) or {}
        self.state.setdefault("steps", {})
        self.model = (self.job.get("model") or DEFAULT_MODEL).strip()
        self._classes = None
        self._last_progress = 0.0
        self._phase = None
        self._max = 0.0

    # ---------------------------------------------------------------- bookkeeping
    def p(self, *parts: str) -> str:
        return os.path.join(self.dir, *parts)

    def w(self, *parts: str) -> str:
        return os.path.join(self.work, *parts)

    def save(self) -> None:
        write_json(self.p("state.json"), self.state)

    def done(self, step: str) -> bool:
        return step in self.state["steps"]

    def mark(self, step: str, **info) -> None:
        self.state["steps"][step] = {"at": now_iso(), **info}
        self.save()

    def progress(self, phase: str, fraction: float, detail: str = "", force: bool = False) -> None:
        """At most one call a second per phase (unless forced); the fraction never goes back
        (a repair turn after the checks)."""
        now = time.monotonic()
        if not force and phase == self._phase and now - self._last_progress < 1.0:
            return
        self._phase, self._last_progress = phase, now
        self._max = max(self._max, min(1.0, max(0.0, fraction)))
        self._progress(phase, round(self._max, 3), detail)

    def input(self) -> str:
        for k in ("input", "source", "path", "url"):
            v = self.job.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
        raise PipelineError("source", "no_input", "job.json names no paper (input)")

    def meta(self) -> dict:
        return read_json(self.p("meta.json"), {}) or {}

    def finish(self, result: dict) -> dict:
        write_json(self.p("result.json"), dict(result, at=now_iso()))
        return result

    # ---------------------------------------------------------------- the run
    def run(self) -> dict:
        os.makedirs(self.work, mode=0o700, exist_ok=True)
        os.makedirs(self.p("logs"), mode=0o700, exist_ok=True)
        lock = self._lock()
        try:
            if self.done("hub"):
                return read_json(self.p("result.json"), {}) or self.state["steps"]["hub"]["result"]
            self._stop_stale()
            self.state.pop("paused_until", None)
            self._source()
            self._identify()
            r = self._claim()
            if r is not None:
                return self.finish(r)
            self._prompt()
            self._episode()
            self._explainer()
            self._links()
            self._bundle()
            self._upload()
            return self.finish(self._hub())
        except PipelineError as e:
            self.finish({"status": "failed", **e.as_dict()})
            raise
        except UsageLimit as e:
            self.finish({"status": "paused", "resume_at": getattr(e, "resume_at", None),
                         "message": getattr(e, "message", None) or str(e)})
            raise
        finally:
            if lock is not None:
                with contextlib.suppress(OSError):
                    fcntl.flock(lock, fcntl.LOCK_UN)
                lock.close()

    def _lock(self):
        """One worker per job at a time."""
        if fcntl is None:
            return None
        fh = open(self.p(".lock"), "a")
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            raise PipelineError("start", "busy", "another worker is running this job",
                                retryable=True)
        return fh

    def _stop_stale(self) -> None:
        """A Claude Code process left by a worker that was killed: stopped before anything else
        touches the work directory (only if its command line names this job's session)."""
        rec = self.state.get("claude_pid")
        if not isinstance(rec, dict) or not rec.get("pid"):
            return
        pid, marker = int(rec["pid"]), str(rec.get("marker") or "")
        try:
            os.kill(pid, 0)
            alive = True
        except OSError:
            alive = False
        if alive and marker and marker in _proc_command(pid):
            with contextlib.suppress(OSError):
                os.kill(pid, signal.SIGTERM)
            for _ in range(50):
                time.sleep(0.2)
                try:
                    os.kill(pid, 0)
                except OSError:
                    break
            else:
                with contextlib.suppress(OSError):
                    os.kill(pid, signal.SIGKILL)
        self.state["claude_pid"] = None
        self.save()

    # ---------------------------------------------------------------- 1. source
    def _source(self) -> None:
        if self.done("source"):
            return
        self.progress("reading", AT["source"], "getting the paper", force=True)
        inp = self.input()
        dst = self.w("paper.pdf")
        info: dict = {}
        if is_url(inp):
            url = inp
            if url.lower().startswith("arxiv:"):
                url = "https://arxiv.org/abs/" + url[6:].strip()
            aid = ptitle.arxiv_from_url(url)
            info = {"kind": "url", "url": url, "arxiv_id": aid}
            get = f"https://arxiv.org/pdf/{aid}" if aid else (url if pdf_link(url) else None)
            if get and not os.path.exists(dst):
                # The agent reads a link with WebFetch, which returns a model's digest of a page,
                # not the paper: an arXiv PDF (or a named site's direct PDF link) is fetched here
                # instead, so the agent reads every page and the explainer can crop figures.
                try:
                    body = self.download(get, MAX_PDF)
                    if not body.lstrip().startswith(b"%PDF-"):
                        raise OSError("the answer is not a PDF")
                    write_bytes(dst, body)
                    info["downloaded"] = get
                except Exception as e:           # noqa: BLE001 - the agent's WebFetch remains
                    info["download_error"] = str(e)[:200]
        else:
            src = os.path.abspath(os.path.expanduser(inp))
            if not os.path.isfile(src):
                raise PipelineError("source", "not_found", f"no such file: {src}")
            if not pdf.is_pdf(src):
                raise PipelineError("source", "not_pdf", f"not a PDF file: {src}")
            if os.path.getsize(src) > MAX_PDF:
                raise PipelineError("source", "too_large", f"the PDF is over {MAX_PDF >> 20} MB")
            info = {"kind": "pdf", "path": src, "name": os.path.basename(src)}
            if not os.path.exists(dst):
                shutil.copyfile(src, dst)
                os.chmod(dst, 0o600)
        info["pdf"] = os.path.isfile(dst)
        info["pages"] = 0
        if info["pdf"]:
            info["sha256"] = sha256_file(dst)
            try:
                pi = pdf.info(dst)
            except pdf.PdfError as e:
                raise PipelineError("source", "pdf_invalid", f"not a usable PDF: {e}")
            info["pages"] = pi.get("_pages") or 0
            have = pdf.available()
            if have["text"]:
                try:
                    page1 = pdf.text(dst, self.p("page1.txt"), 1, 1)
                    info["ids"] = pdf.identifiers_from_first_page(page1)
                except pdf.PdfError:
                    pass
            if have["crops"] and info["pages"]:
                try:
                    pdf.render_pages(dst, self.w("pages"), info["pages"])
                except (pdf.PdfError, subprocess.SubprocessError, OSError):
                    pass
        info["pages_dir"] = os.path.isdir(self.w("pages"))
        info["crops"] = bool(info["pdf"] and info["pages"] and pdf.available()["crops"])
        self.mark("source", **info)

    def source(self) -> dict:
        return self.state["steps"].get("source") or {}

    # ---------------------------------------------------------------- 2. identify
    def _identify(self) -> None:
        if self.done("identify"):
            return
        self.progress("reading", AT["identify"], "identifying the paper", force=True)
        src = self.source()
        url = src.get("url")
        st = self.state.setdefault("identify", {})
        pj = self.w("paper.json")
        if not (st.get("turn_done") and os.path.isfile(pj)):
            if self._delivered("identify"):
                prompt = prompts.retry(self.state.get("retry_of")) if self.state.get("retry_of") \
                    else prompts.interrupted()
            else:
                prompt = prompts.identify(src.get("pdf"), url)
            st["started"] = True
            self.save()
            try:
                self._turn("identify", prompt, resume=bool(self.state.get("session_started")))
            except _NoSession:
                self._turn("identify", prompts.identify(src.get("pdf"), url), resume=False)
            st["turn_done"] = True
            self.state["retry_of"] = None
            self.save()
        meta, problems = self._paper_meta()
        if problems:
            if st.get("fixed"):
                raise PipelineError("identify", "paper_unknown",
                                    "the paper could not be identified: " + "; ".join(problems))
            st["fixed"] = True
            self.save()
            try:
                self._turn("identify", prompts.paper_fix(problems), resume=True)
            except _NoSession:
                self._turn("identify", prompts.identify(src.get("pdf"), url), resume=False)
            meta, problems = self._paper_meta()
            if problems:
                raise PipelineError("identify", "paper_unknown",
                                    "the paper could not be identified: " + "; ".join(problems))
        write_json(self.p("meta.json"), meta)
        self.mark("identify", title=meta["title"])

    def _paper_meta(self) -> tuple[dict, list[str]]:
        """paper.json cleaned, with what the pipeline knows better: the arXiv id of the link
        given, the stamp on page one, the PDF's sha256."""
        d = read_json(self.w("paper.json"))
        if not isinstance(d, dict):
            return {}, ["`paper.json` is missing or not one JSON object"]
        src = self.source()
        t = ptitle.clean(d.get("title"))
        if not t:
            return {}, ["`title` must be the paper's title exactly as printed"]
        authors = d.get("authors")
        if isinstance(authors, str):
            authors = [a.strip() for a in re.split(r";|,| and ", authors) if a.strip()]
        authors = [" ".join(a.split())[:200] for a in (authors or []) if isinstance(a, str) and a.strip()][:100]
        ids = src.get("ids") or {}
        aid = src.get("arxiv_id") or ptitle.arxiv_id(d.get("arxiv_id")) or ids.get("arxiv_id")
        doi = ptitle.doi(d.get("doi")) or ids.get("doi")
        year = ptags.clean_year(d.get("year")) or ptags.year_from_arxiv_id(aid)
        url = d.get("url") if isinstance(d.get("url"), str) and d["url"].startswith(("http://", "https://")) else None
        url = src.get("url") or url or (f"https://arxiv.org/abs/{aid}" if aid else None)
        return {"title": t, "authors": authors, "year": year, "arxiv_id": aid, "doi": doi,
                "url": url, "source_sha256": src.get("sha256")}, []

    # ---------------------------------------------------------------- 3. lookup and claim
    def _api(self, step: str, fn, *args) -> tuple[int, dict]:
        """(status, answer). An HTTP error is its status and body; no answer at all raises a
        retryable PipelineError."""
        try:
            r = fn(*args)
        except PipelineError:
            raise
        except Exception as e:                   # noqa: BLE001 - whatever the api raises
            st = _http_status(e)
            if st is None:
                raise PipelineError(step, "hub_unreachable", f"the hub could not be reached: {e}",
                                    retryable=True)
            body = _http_body(e)
            if st == 401:
                raise PipelineError(step, "hub_auth", "the hub refused this device's token; run "
                                    "`papercast login`", str(body)[:300], retryable=True)
            return st, body
        return 200, (r if isinstance(r, dict) else {})

    def _refused(self, step: str, status: int, body: dict, what: str) -> PipelineError:
        msg = body.get("message") or body.get("msg") or body.get("error") or f"HTTP {status}"
        if status == 403:
            return PipelineError(step, "hub_forbidden", f"the hub refused {what}: {msg} (uploading "
                                 "needs the contributor role; an admin can give it)")
        return PipelineError(step, "hub_refused", f"the hub refused {what}: {msg}",
                             json.dumps(body)[:500], retryable=status >= 500)

    def keys(self) -> dict:
        m = self.meta()
        k = {"arxiv_id": m.get("arxiv_id"), "doi": m.get("doi"),
             "source_sha256": m.get("source_sha256"), "title": m.get("title")}
        return {a: b for a, b in k.items() if b}

    def _lookup(self, step: str) -> dict:
        k = self.keys()
        q = {"arxiv_id": k.get("arxiv_id"), "doi": k.get("doi"), "sha256": k.get("source_sha256"),
             "title": k.get("title")}
        path = "/api/cli/lookup?" + urllib.parse.urlencode({a: b for a, b in q.items() if b})
        st, r = self._api(step, self.api.get, path)
        if st != 200:
            raise self._refused(step, st, r, "the lookup")
        write_json(self.p("lookup.json"), r)
        return r

    def _claim(self) -> dict | None:
        """None when the job may go on (claim.json written); else the result to return."""
        if self.done("claim"):
            return None
        self.progress("claiming", AT["claim"], "asking the hub", force=True)
        r = self._lookup("claim")
        paper = r.get("paper") if isinstance(r.get("paper"), dict) else None
        vof = self.job.get("version_of")
        if vof:
            claim = {"mode": "version", "paper_id": vof, "claim_id": None}
        elif paper:
            if not self.job.get("yes"):
                names = []
                for ep in paper.get("episodes") or []:
                    n = ((ep or {}).get("made_by") or {}).get("name")
                    if n and n not in names:
                        names.append(n)
                who = " and ".join([", ".join(names[:-1]), names[-1]] if len(names) > 1 else names) \
                    or "someone"
                return {"status": "needs_confirmation", "paper": paper, "made_by": names,
                        "question": f"made by {who}. Make your own version? [y/N]"}
            claim = {"mode": "version", "paper_id": paper.get("id"), "claim_id": None}
        else:
            claim = self._new_claim("claim")
            if claim.get("status") == "in_progress":
                return claim
        write_json(self.p("claim.json"), claim)
        self.mark("claim", **claim)
        return None

    def _new_claim(self, step: str) -> dict:
        device = self.job.get("device") or socket.gethostname()
        st, r = self._api(step, self.api.post, "/api/cli/claims", {"keys": self.keys(),
                                                                  "device": device})
        if st == 409 or r.get("error") == "in_progress":
            by = r.get("by") if isinstance(r.get("by"), dict) else {}
            return {"status": "in_progress", "by": by, "since": r.get("since"),
                    "message": f"{by.get('name') or 'someone'} is making this paper now"}
        if st not in (200, 201) or not r.get("claim_id"):
            raise self._refused(step, st, r, "the claim")
        return {"mode": "new", "paper_id": None, "claim_id": r["claim_id"],
                "expires_at": r.get("expires_at"), "claimed_at": now_iso()}

    def claim(self) -> dict:
        return read_json(self.p("claim.json"), {}) or {}

    def _ensure_claim(self) -> bool:
        """Before the upload: a new paper's claim still holds (renewed, or taken again after it
        lapsed), or someone else's episode made the paper meanwhile and this becomes a version
        of it. True when claim.json changed."""
        c = self.claim()
        if c.get("mode") != "new":
            return False
        exp = parse_iso(c.get("expires_at"))
        if exp is None or exp - time.time() > 600:
            return False
        r = self._lookup("upload")
        paper = r.get("paper") if isinstance(r.get("paper"), dict) else None
        if paper and paper.get("id"):
            c = {"mode": "version", "paper_id": paper["id"], "claim_id": None,
                 "note": "the paper appeared on the hub while the claim lapsed"}
        else:
            renewed = None
            put = getattr(self.api, "put", None)
            if callable(put) and c.get("claim_id"):
                st, rr = self._api("upload", put, f"/api/cli/claims/{c['claim_id']}", {})
                if st in (200, 201) and rr.get("expires_at"):
                    renewed = dict(c, expires_at=rr["expires_at"])
            if renewed is None:
                n = self._new_claim("upload")
                if n.get("status") == "in_progress":
                    raise PipelineError("upload", "claim_lost", n["message"] + "; the claim of "
                                        "this job lapsed", retryable=True)
                renewed = n
            c = renewed
        write_json(self.p("claim.json"), c)
        return True

    # ---------------------------------------------------------------- 4. prompt and prefs
    def _prompt(self) -> None:
        if self.done("prompt"):
            return
        self.progress("writing", AT["prompt"], "fetching the guideline", force=True)
        st, pr = self._api("prompt", self.api.get, "/api/cli/prompt")
        if st != 200 or not isinstance(pr.get("guideline"), str) or not pr["guideline"].strip():
            raise self._refused("prompt", st, pr, "the base prompt")
        st, pf = self._api("prompt", self.api.get, "/api/cli/prefs")
        if st != 200:
            raise self._refused("prompt", st, pf, "the preferences")
        settings = pf.get("settings") if isinstance(pf.get("settings"), dict) else {}
        note = pf.get("note") if isinstance(pf.get("note"), str) else ""
        write_json(self.p("prompt.json"), pr)
        write_json(self.p("prefs.json"), {"settings": settings, "note": note,
                                          "version": pf.get("version")})
        text = pr["guideline"].rstrip() + "\n\n" + cprefs.render(settings, note)
        write_bytes(self.w("guideline.md"), text.encode("utf-8"))
        self.mark("prompt", base_version=pr.get("version"), prefs_version=pf.get("version"))

    def minutes(self) -> tuple[float, float]:
        """The length range: the base prompt's own fields, else the range its guideline states
        ("15 to 25 minutes"), else 15-25."""
        pr = read_json(self.p("prompt.json"), {}) or {}
        mm = pr.get("minutes")
        if isinstance(mm, (list, tuple)) and len(mm) == 2:
            try:
                return float(mm[0]), float(mm[1])
            except (TypeError, ValueError):
                pass
        try:
            if pr.get("min_minutes") and pr.get("max_minutes"):
                return float(pr["min_minutes"]), float(pr["max_minutes"])
        except (TypeError, ValueError):
            pass
        m = re.search(r"\b(\d{1,3})\s*(?:to|-|–|—)\s*(\d{1,3})\s*minutes?\b", pr.get("guideline") or "")
        if m and 0 < int(m.group(1)) < int(m.group(2)) <= 180:
            return float(m.group(1)), float(m.group(2))
        return MINUTES

    def wpm(self) -> float:
        pr = read_json(self.p("prompt.json"), {}) or {}
        v = pr.get("wpm")
        return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and 60 <= v <= 300 else WPM

    def word_bounds(self) -> tuple[int, int]:
        lo, hi = self.minutes()
        w = self.wpm()
        return int(lo * w), int(hi * w)

    @contextlib.contextmanager
    def wording_of_base(self):
        """The checks use the never-wanted wording the hub serves with the base prompt (the
        listener-name class carries this person's name), the same list the hub checks with;
        the package's own list when the hub sent none that loads."""
        if self._classes is None:
            self._classes = False
            w = (read_json(self.p("prompt.json"), {}) or {}).get("wording")
            if isinstance(w, dict) and w.get("classes"):
                path = self.p("wording.json")
                try:
                    write_json(path, w)
                    self._classes = wording.load(path)
                except (ValueError, KeyError, TypeError, OSError, re.error):
                    self._classes = False
        if not self._classes or not hasattr(wording, "_CLASSES"):
            yield
            return
        with _WORDING_LOCK:
            old = wording._CLASSES
            wording._CLASSES = self._classes
            try:
                yield
            finally:
                wording._CLASSES = old

    # ---------------------------------------------------------------- 5. the episode
    def _delivered(self, kind: str) -> bool:
        """Claude Code started a turn of this kind in the current session (its init event came),
        so the instruction is in the transcript and a retry may refer to it."""
        return bool(self.state.get("session_started") and
                    (self.state.get("delivered") or {}).get(kind))

    def _episode(self) -> None:
        if self.done("episode"):
            return
        for _ in range(2):
            try:
                return self._episode_steps()
            except _NoSession:
                # the session is gone: the episode again from its instruction, in a new one
                self.state["episode"] = {}
                self.state["cut"] = None
                self.save()
        raise PipelineError("episode", "no_session", "Claude Code lost the session twice")

    def _episode_steps(self) -> None:
        st = self.state.setdefault("episode", {})
        lo, hi = self.word_bounds()
        lo_m, hi_m = self.minutes()
        if not st.get("main_done"):
            self.progress("writing", AT["episode"], "reading the paper", force=True)
            resume = bool(self.state.get("session_started"))
            if st.get("started") and self._delivered("episode"):
                prompt = prompts.retry(self.state.get("retry_of")) if self.state.get("retry_of") \
                    else prompts.interrupted()
            else:
                prompt = self._episode_prompt()
            st["started"] = True
            self.save()
            self._turn("episode", prompt, resume=resume)
            st["main_done"] = True
            self.state["retry_of"] = None
            self.save()
        # outputs, script, explainer.json and the page limits: one repair turn naming exactly
        # what is wrong or over, and why (Leo's runner, core._reading)
        while True:
            self.progress("checking", AT["checks"], "checking the script", force=True)
            missing, check, ex_problems, over = self._check_outputs()
            if not missing and check["ok"] and not ex_problems and not over:
                break
            if st.get("repair") == "done":
                if missing:
                    raise PipelineError("checks", "output_missing",
                                        f"the session ended without {', '.join(missing)}")
                if not check["ok"]:
                    raise PipelineError("checks", "script_invalid",
                                        check.get("message") or
                                        "script.md is not ready to be spoken after one repair turn",
                                        "; ".join(check["problems"])[:2000])
                if ex_problems:
                    raise PipelineError("checks", "explainer_invalid",
                                        "The explainer still breaks the rules after one repair "
                                        f"turn: {ex_problems[0]}.", "; ".join(ex_problems)[:2000])
                raise PipelineError("checks", "explainer_too_long", over[0],
                                    "still over after one repair turn")
            st["repair"] = "running"
            st["repair_problems"] = (check["problems"] + ex_problems + over + missing)[:40]
            self.save()
            self.progress("checking", AT["checks"], "one repair turn", force=True)
            self._turn("repair", prompts.repair(check["problems"], missing, over, ex_problems),
                       resume=True)
            st["repair"] = "done"
            self.save()
        self._cut_pass(lo, hi, lo_m, hi_m)
        # frozen: what was checked is what is bundled
        os.makedirs(self.p("out"), mode=0o700, exist_ok=True)
        for name in REQUIRED:
            shutil.copyfile(self.w(name), self.p("out", name))
        with self.wording_of_base():
            chk = checks.check(self._read(self.p("out", "script.md")), self.wpm(), lo_m, hi_m)
        self.mark("episode", words=chk["words"], minutes=round(chk["words"] / self.wpm(), 1),
                  repaired=st.get("repair") == "done", cut=self.state.get("cut"))

    def _episode_prompt(self) -> str:
        src = self.source()
        lo, hi = self.word_bounds()
        lo_m, hi_m = self.minutes()
        return prompts.episode(bool(src.get("pdf")), src.get("url"), bool(src.get("pages_dir")),
                               bool(src.get("crops")), lo, hi, lo_m, hi_m, self._tags_in_use())

    def _tags_in_use(self) -> list[str]:
        """Tags of the library's papers, when the hub lists them (so graphs built from tag
        rules find this paper)."""
        lib = self._library(cached_only=True)
        return ptags.vocabulary(p.get("tags") for p in lib if isinstance(p.get("tags"), list))

    @staticmethod
    def _read(path: str) -> str:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read()

    def _check_outputs(self) -> tuple[list[str], dict, list[str], list[str]]:
        missing = [f for f in REQUIRED if not os.path.isfile(self.w(f))]
        lo_m, hi_m = self.minutes()
        check = {"ok": True, "problems": []}
        ex_problems: list[str] = []
        over: list[str] = []
        with self.wording_of_base():
            sp = self.w("script.md")
            if os.path.isfile(sp):
                if os.path.getsize(sp) > SCRIPT_MAX:
                    check = {"ok": False, "problems": [f"larger than {SCRIPT_MAX // 1024} kB"]}
                else:
                    text = self._read(sp)
                    check = checks.check(text, self.wpm(), lo_m, hi_m)
                    lo, hi = self.word_bounds()
                    n = len(text.split())         # the hub may count this way (headings' #)
                    if check["ok"] and not lo <= n <= hi:
                        check = dict(check, ok=False, problems=[
                            f"{n} words counting every token; needs {lo}-{hi} words"])
            if "explainer.json" not in missing:
                spec, ex_problems = self._explainer_spec()
                if spec is not None:
                    over = explainer.over_limits(spec, self.meta().get("title"))
        return missing, check, ex_problems, over

    def _explainer_spec(self):
        path = self.w("explainer.json")
        if os.path.islink(path) or os.path.getsize(path) > explainer.MAX_JSON:
            return None, [f"`explainer.json` must be a plain file of at most "
                          f"{explainer.MAX_JSON // 1024} KiB"]
        text = self._read(path)
        src = self.source()
        spec, problems = explainer.check(text, self.w("paper.pdf") if src.get("pdf") else None,
                                         src.get("pages") or 0, crops=bool(src.get("crops")))
        if spec is not None:
            # the hub's own test of the same file (SPEC.md section 6)
            problems = checks.explainer_problems(json.loads(text))
            if problems:
                spec = None
        return spec, problems

    def _cut_pass(self, lo: int, hi: int, lo_m: float, hi_m: float) -> None:
        """The cut-only turn (cut.py): once, after every check passed. Deletions only, judged by
        cut.finish, which puts back what breaks the rules. It never fails the episode; the
        usage limit pauses as anywhere else."""
        c = self.state.get("cut") or {}
        if c.get("state") == "done":
            return
        keep = self.p("cut")
        self.progress("cutting", AT["cut"], "the cut-only pass", force=True)
        if c.get("state") != "running":
            cut.prepare(self.work, keep)
            self.state["cut"] = {"state": "running"}
            self.save()
            try:
                self._turn("cut", prompts.cut(lo, hi), resume=True)
            except PipelineError as e:
                self.state["cut"]["turn_error"] = e.code
        with self.wording_of_base():
            res = cut.finish(self.work, keep, self.wpm(), lo_m, hi_m, self.dir)
        self.state["cut"] = dict(res, state="done")
        self.save()

    # ---------------------------------------------------------------- Claude Code turns
    def _next_run(self) -> int:
        n = int(self.state.get("runs") or 0) + 1
        self.state["runs"] = n
        return n

    def _turn(self, kind: str, prompt: str, resume: bool) -> claude.Run:
        """One turn of the episode session, with Leo's runner's recoveries: an overloaded API or
        the network is waited out and the same session asked again; a turn stopped by the
        watchdog runs again. A session Claude Code cannot resume raises _NoSession after a new
        session id is set: the caller starts its step again from the instruction."""
        k = 0
        while True:
            try:
                return self._turn_once(kind, prompt, resume)
            except _Transient as e:
                if k >= len(TRANSIENT_BACKOFF_S):
                    raise PipelineError(self._step_of(kind), "claude_exit",
                                        f"Claude Code failed {k + 1} times: {e.why}",
                                        retryable=True)
                wait = TRANSIENT_BACKOFF_S[k]
                k += 1
                self.progress(self._phase or "writing", self._frac(kind),
                              f"Claude is not answering; trying again in {wait:g} s", force=True)
                time.sleep(wait)
                resume = bool(self.state.get("session_started"))
                if kind in ("episode", "identify") and self._delivered(kind):
                    prompt = prompts.retry({"code": "claude_exit", "message": e.why})
            except _Interrupted as e:
                n = self.state.setdefault("interrupted", {}).get(kind, 0) + 1
                self.state["interrupted"][kind] = n
                self.save()
                if n >= MAX_INTERRUPTS:
                    raise PipelineError(self._step_of(kind), "interrupted",
                                        f"{e}, {n} times; stopped each time")
                resume = bool(self.state.get("session_started"))
                if kind in ("episode", "identify") and self._delivered(kind):
                    prompt = prompts.interrupted()
            except _NoSession:
                # No transcript to resume (it died before writing one).
                self.state["session_id"] = str(uuid.uuid4())
                self.state["session_started"] = False
                self.state["delivered"] = {}
                self.save()
                raise

    @staticmethod
    def _step_of(kind: str) -> str:
        return {"identify": "identify", "episode": "episode", "repair": "checks",
                "cut": "cut"}.get(kind, kind)

    @staticmethod
    def _frac(kind: str) -> float:
        return {"identify": AT["identify"], "episode": AT["episode"], "repair": AT["checks"],
                "cut": AT["cut"], "links": AT["links"]}.get(kind, AT["episode"])

    def _turn_once(self, kind: str, prompt: str, resume: bool) -> claude.Run:
        path, target = claude.resolve()
        if not path:
            raise PipelineError(self._step_of(kind), "claude_missing",
                                "Claude Code (`claude`) is not installed or not on the PATH; "
                                "install it and log in once", retryable=True)
        if not self.state.get("session_id"):
            self.state["session_id"] = str(uuid.uuid4())
        sid = self.state["session_id"]
        argv = [path, "-p", prompt]
        argv += ["--resume", sid] if resume else ["--session-id", sid]
        argv += claude.lockdown(self.model, target)
        n = self._next_run()
        out, err = self.p("logs", f"run-{n}.jsonl"), self.p("logs", f"run-{n}.err")
        rec = {"n": n, "kind": kind, "model": self.model, "claude": target,
               "resume": resume, "started_at": now_iso()}
        self.state.setdefault("turns", []).append(rec)
        self.save()
        base = self._frac(kind)
        span = 0.62 if kind == "episode" else 0.03

        def on_start(pid):
            self.state["claude_pid"] = {"pid": pid, "marker": sid}
            self.save()

        def on_event(ev, turn):
            if ev.get("type") == "system" and ev.get("subtype") == "init" and ev.get("session_id"):
                self.state["session_started"] = True
                self.state.setdefault("delivered", {})[kind] = True
                if ev["session_id"] != self.state.get("session_id"):
                    self.state["session_id"] = ev["session_id"]
                self.state["claude_code_version"] = ev.get("claude_code_version")
                self.save()
            elif ev.get("type") == "assistant":
                for b in (ev.get("message") or {}).get("content") or []:
                    if isinstance(b, dict) and b.get("type") == "tool_use":
                        inp = b.get("input") or {}
                        what = inp.get("file_path") or inp.get("url") or inp.get("pattern") or ""
                        what = os.path.basename(what) if inp.get("file_path") else what
                        frac = base + span * (1 - 0.97 ** turn.tool_uses)
                        self.progress(self._phase_of(kind), frac, f"{b.get('name')} {what}".strip())

        r = claude.run(argv, cwd=self.work, out_path=out, err_path=err,
                       watchdog_s=WATCHDOG_S, on_event=on_event, on_start=on_start)
        self.state["claude_pid"] = None
        rec.update(ended_at=now_iso(), rc=r.rc, wall_s=round(r.wall_s, 1))
        self.save()
        if r.violation:
            raise PipelineError(self._step_of(kind), "tool_violation",
                                "Claude Code started with tools other than the allowlist; stopped",
                                r.violation)
        if r.stopped:
            raise _Interrupted(r.stopped)
        hit = limits.detect(r.turn, r.rc, r.err_last)
        if hit:
            self._limited(kind, hit)
        bad = claude.classify(r.turn, r.rc, r.err_last)
        if bad:
            code, msg = bad
            if code == "no_session" and resume:
                raise _NoSession()
            if code == "claude_exit":
                why = limits.transient(r.turn, r.rc, r.err_last)
                if why:
                    raise _Transient(why)
            raise PipelineError(self._step_of(kind), code, msg, retryable=code == "claude_auth")
        return r

    @staticmethod
    def _phase_of(kind: str) -> str:
        return {"identify": "reading", "episode": "writing", "repair": "checking",
                "cut": "cutting"}.get(kind, "writing")

    def _limited(self, kind: str, hit: limits.Hit) -> None:
        at, guessed = limits.resume_at(hit)
        msg = limits.message(hit)
        self.state["retry_of"] = {"step": kind, "code": "claude_usage_limit", "message": msg,
                                  "detail": f"{hit.how}: {hit.detail}", "at": now_iso()}
        self.state["paused_until"] = at
        self.save()
        self.progress("paused", self._frac(kind),
                      f"{msg}; resumes {limits.local_hhmm(at)}", force=True)
        raise usage_limit(at, hit.resets_at, guessed, msg, hit.how)

    # ---------------------------------------------------------------- 6. explainer.html
    def _explainer(self) -> None:
        if self.done("explainer") and os.path.isfile(self.p("explainer.html")):
            return
        self.progress("explainer", AT["explainer"], "building the explainer page", force=True)
        src = self.source()
        with self.wording_of_base():
            data, spec, problems = explainer.build(
                self._read(self.p("out", "explainer.json")), self.meta().get("title"),
                self.w("paper.pdf") if src.get("pdf") else None, src.get("pages") or 0,
                bool(src.get("crops")))
        if problems:
            raise PipelineError("explainer", "explainer_invalid",
                                f"The explainer breaks the rules: {problems[0]}.",
                                "; ".join(problems)[:2000])
        write_bytes(self.p("explainer.html"), data)
        self.mark("explainer", figures=len(spec.figures), dropped=spec.dropped,
                  svg_removed=spec.removed, size=len(data))

    # ---------------------------------------------------------------- 7. links
    def _library(self, cached_only: bool = False) -> list[dict]:
        lib = read_json(self.p("library.json"))
        if isinstance(lib, dict) and isinstance(lib.get("papers"), list):
            return lib["papers"]
        if cached_only:
            try:
                st, r = self._api("links", self.api.get, "/api/cli/library")
            except PipelineError:
                return []
        else:
            st, r = self._api("links", self.api.get, "/api/cli/library")
        if st != 200 or not isinstance(r.get("papers"), list):
            if cached_only:
                return []
            raise self._refused("links", st, r, "the library")
        write_json(self.p("library.json"), {"papers": r["papers"], "at": now_iso()})
        return r["papers"]

    def _links(self) -> None:
        if self.done("links") and os.path.isfile(self.p("links.json")):
            return
        self.progress("links", AT["links"], "finding links to the library", force=True)
        # the library as it is now, not as it was when the episode started
        with contextlib.suppress(OSError):
            os.unlink(self.p("library.json"))
        library = self._library()
        meta = self.meta()
        claims_md = parse_claims(self.p("out", "claims.md"))
        c = self.claim()
        exclude = {c["paper_id"]} if c.get("paper_id") else set()
        text = None
        src = self.source()
        if src.get("pdf") and pdf.available()["text"]:
            try:
                text = pdf.text(self.w("paper.pdf"), self.p("text.txt"))
            except (pdf.PdfError, OSError):
                text = None

        def prog(i, n):
            self.progress("links", AT["links"] + 0.08 * i / max(1, n), f"graded {i} of {n}")

        res = plinks.find(meta, claims_md.get("claims") or [], library, self.fetch, self._grade,
                          self.p("links"), exclude=exclude, text=text, progress=prog)
        write_json(self.p("links.json"), res)
        self.mark("links", n=len(res["links"]), s2_id=res.get("s2_id"))

    def _grade(self, body: str) -> str:
        """One tool-free haiku call (judge2.py), with Leo's bare lockdown."""
        path, _ = claude.resolve()
        if not path:
            raise PipelineError("links", "claude_missing", "Claude Code (`claude`) is not installed",
                                retryable=True)
        gdir = self.p("grader")
        os.makedirs(gdir, mode=0o700, exist_ok=True)
        argv = [path, "-p", body, *claude.lockdown_bare(plinks.GRADER_MODEL)]
        n = self._next_run()
        self.save()
        r = claude.run(argv, cwd=gdir, out_path=self.p("logs", f"run-{n}.jsonl"),
                       err_path=self.p("logs", f"run-{n}.err"),
                       init_check=claude.check_init_bare, timeout_s=600)
        if r.violation:
            raise PipelineError("links", "tool_violation",
                                "the link grader started with tools; stopped", r.violation)
        hit = limits.detect(r.turn, r.rc, r.err_last)
        if hit:
            self._limited("links", hit)
        if r.stopped:
            raise plinks.GradeError(r.stopped)
        bad = claude.classify(r.turn, r.rc, r.err_last)
        if bad:
            raise plinks.GradeError(f"{bad[0]}: {bad[1]}")
        return r.turn.answer

    # ---------------------------------------------------------------- 8. bundle
    def manifest(self) -> dict:
        meta, c = self.meta(), self.claim()
        pr = read_json(self.p("prompt.json"), {}) or {}
        pf = read_json(self.p("prefs.json"), {}) or {}
        lk = read_json(self.p("links.json"), {}) or {}
        fm = parse_claims(self.p("out", "claims.md"))
        vocab = self._tags_in_use()
        tags = ptags.canonical(fm.get("tags") or [], vocab)
        ep = self.state["steps"].get("episode") or {}
        wall = sum(float(t.get("wall_s") or 0) for t in self.state.get("turns") or [])
        paper = {"title": meta.get("title"), "authors": meta.get("authors") or fm.get("authors") or [],
                 "year": meta.get("year") or fm.get("year"), "arxiv_id": meta.get("arxiv_id"),
                 "doi": meta.get("doi"), "url": meta.get("url"),
                 "source_sha256": meta.get("source_sha256"), "tags": tags}
        if lk.get("s2_id"):
            paper["s2_id"] = lk["s2_id"]
        return {"manifest_version": cbundle.MANIFEST_VERSION, "client_version": __version__,
                "base_version": pr.get("version"),
                "prefs": {"settings": pf.get("settings") or {}, "note": pf.get("note") or "",
                          "version": pf.get("version")},
                "model": self.model,
                "claim_id": c.get("claim_id") if c.get("mode") == "new" else None,
                "paper_id": c.get("paper_id") if c.get("mode") == "version" else None,
                "paper": paper,
                "files": {"script": "script.md", "explainer_json": "explainer.json",
                          "explainer_html": "explainer.html", "claims": "claims.md"},
                "links": lk.get("links") or [],
                "stats": {"words": ep.get("words"), "est_minutes": ep.get("minutes"),
                          "wall_s": round(wall)}}

    def _bundle(self, force: bool = False) -> None:
        if not force and self.done("bundle") and os.path.isfile(self.p("bundle.tar.gz")):
            return
        self.progress("uploading", AT["bundle"], "packing the episode", force=True)
        m = self.manifest()
        files = {"script.md": self.p("out", "script.md"),
                 "explainer.json": self.p("out", "explainer.json"),
                 "explainer.html": self.p("explainer.html"),
                 "claims.md": self.p("out", "claims.md")}
        problems = cbundle.validate(m, set(files) | {"manifest.json"})
        problems += self._hub_checks(m, files)
        if problems:
            raise PipelineError("bundle", "bundle_invalid", "the bundle is not valid: " +
                                problems[0], "; ".join(problems)[:2000])
        raw = json.dumps(m, indent=1, ensure_ascii=False).encode("utf-8")
        buf = io.BytesIO()
        mtime = int(time.time())
        entries = [("manifest.json", raw)]
        for n, p in files.items():
            with open(p, "rb") as fh:
                entries.append((n, fh.read()))
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            for name, data in entries:
                ti = tarfile.TarInfo(name)
                ti.size, ti.mode, ti.mtime = len(data), 0o644, mtime
                tf.addfile(ti, io.BytesIO(data))
        data = buf.getvalue()
        if len(data) > cbundle.MAX_BYTES:
            raise PipelineError("bundle", "bundle_too_large", f"the bundle is {len(data) >> 20} MB; "
                                f"the hub takes at most {cbundle.MAX_BYTES >> 20} MB")
        write_json(self.p("manifest.json"), m)
        write_bytes(self.p("bundle.tar.gz"), data)
        self.mark("bundle", size=len(data), sha256=hashlib.sha256(data).hexdigest())

    def _hub_checks(self, m: dict, files: dict) -> list[str]:
        """The hub's checks (SPEC.md section 6) run here first, so a refusal is known before
        the upload."""
        out = []
        with open(files["script.md"], "rb") as fh:
            raw = fh.read()
        if len(raw) > SCRIPT_MAX:
            out.append(f"script.md is over {SCRIPT_MAX // 1024} kB")
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            out.append("script.md is not UTF-8")
            text = ""
        lo, hi = self.minutes()
        # both ways of counting words (the checks' and a plain split), so the hub agrees
        # whichever it uses
        wpm = self.wpm()
        for n in (checks.check(text, wpm, lo, hi)["words"], len(text.split())):
            est = n / wpm
            if not lo <= est <= hi:
                out.append(f"script.md is about {est:.1f} minutes at {wpm:g} words a minute; "
                           f"the range is {lo:g}-{hi:g}")
                break
        with open(files["explainer.html"], "rb") as fh:
            html = fh.read()
        if len(html) > explainer.MAX_OUT:
            out.append(f"explainer.html is over {explainer.MAX_OUT >> 20} MB")
        if not html[:64].lower().startswith(b"<!doctype html"):
            out.append("explainer.html does not start with <!doctype html")
        try:
            out += checks.explainer_problems(json.loads(self._read(files["explainer.json"])))
        except ValueError:
            out.append("explainer.json is not valid JSON")
        return out

    # ---------------------------------------------------------------- 9. upload
    def _upload(self) -> None:
        if self.done("upload") and read_json(self.p("upload.json")):
            return
        self.progress("uploading", AT["upload"], "uploading", force=True)
        if self._ensure_claim():
            self._bundle(force=True)
        up = self.state.get("upload") or {}
        if up.get("started_at") and not up.get("ended_at"):
            adopted = self._adopt_upload(up["started_at"])
            if adopted:
                write_json(self.p("upload.json"), adopted)
                self.mark("upload", **adopted)
                return
        with open(self.p("bundle.tar.gz"), "rb") as fh:
            data = fh.read()
        self.state["upload"] = {"started_at": now_iso(), "size": len(data)}
        self.save()
        st, r = self._api("upload", self.api.upload, "/api/cli/episodes", data, "application/gzip")
        if st not in (200, 201) or not r.get("episode_id"):
            self.state["upload"]["ended_at"] = now_iso()
            self.state["upload"]["error"] = st
            self.save()
            raise self._refused("upload", st, r, "the episode")
        rec = {"episode_id": r["episode_id"], "paper_id": r.get("paper_id"),
               "state": r.get("state") or "checking"}
        write_json(self.p("upload.json"), rec)
        self.state["upload"]["ended_at"] = now_iso()
        self.mark("upload", **rec)

    def _adopt_upload(self, since: str) -> dict | None:
        """The worker died between sending the bundle and hearing the answer: the hub may have
        it. An episode of this paper by this person created since then is that upload."""
        try:
            st, r = self._api("upload", self.api.get, "/api/cli/episodes?mine=1")
        except PipelineError:
            return None
        eps = r.get("episodes") if isinstance(r.get("episodes"), list) else []
        t0 = (parse_iso(since) or 0) - 5
        c, meta = self.claim(), self.meta()
        for ep in eps:
            if not isinstance(ep, dict) or not ep.get("id"):
                continue
            same = (c.get("paper_id") and ep.get("paper_id") == c["paper_id"]) or \
                ptitle.norm(ep.get("title") or (ep.get("paper") or {}).get("title")) == ptitle.norm(meta.get("title"))
            if same and (parse_iso(ep.get("created_at")) or 0) >= t0:
                return {"episode_id": ep["id"], "paper_id": ep.get("paper_id"),
                        "state": ep.get("state"), "adopted": True}
        return None

    def _hub(self) -> dict:
        """Poll the hub until it has checked the episode (or HUB_POLL_MAX_S passes)."""
        up = read_json(self.p("upload.json"), {}) or {}
        eid = up.get("episode_id")
        state, ep = up.get("state") or "checking", {}
        deadline = time.monotonic() + HUB_POLL_MAX_S
        while True:
            self.progress("checking", AT["hub"], f"the hub: {state}", force=True)
            if state != "checking" or time.monotonic() > deadline:
                break
            time.sleep(HUB_POLL_S)
            try:
                st, r = self._api("hub", self.api.get, f"/api/cli/episodes/{eid}")
            except PipelineError:
                break
            if st != 200:
                break
            ep = r.get("episode") if isinstance(r.get("episode"), dict) else r
            state = ep.get("state") or state
        report = ep.get("check_report")
        if isinstance(report, str):
            with contextlib.suppress(ValueError):
                report = json.loads(report)
        result = {"status": "rejected" if state == "rejected" else "uploaded",
                  "episode_id": eid, "paper_id": ep.get("paper_id") or up.get("paper_id"),
                  "state": state, "title": self.meta().get("title")}
        if report:
            result["check_report"] = report
        self.mark("hub", result=result)
        self.progress("done", 1.0, state, force=True)
        return result
