"""How the pipeline runs Claude Code: environment, lockdown, stream parsing, one process.

Copied from Leo's runner (stacks/papercast/runner/papercast_runner/claude.py) and adapted: the
agent works in the job's `work/` directory, which is its cwd, and has no other directory (no
vault, no library), so with `--restricted` it reads and writes only there. The lockdown is
applied on every spawn AND asserted from every init event: a CLI upgrade or a newly added
connector cannot silently widen what the agent can do ("--tools '' does not disable MCP
connectors").

It is the contributor's own `claude`, under their own login, on their own machine (SPEC.md
section 0): nothing here stores or forwards a credential.
"""
from __future__ import annotations

import json
import mmap
import os
import queue
import re
import shutil
import subprocess
import threading
import time

from . import limits

TOOLS = ["Read", "Write", "Edit", "Glob", "Grep", "WebFetch", "WebSearch"]
EXPECTED_PERMISSION_MODE = "dontAsk"
NICE = 10

# The only sites the agent's WebFetch may reach: Leo's PAPERCAST_FETCH_DOMAINS (runner
# config.py, "named sites only", 2026-09-26). Each also allows its `www.` form. The three
# redirect hosts are where doi.org sends Elsevier, APS and RSC DOIs before the publisher's page.
FETCH_DOMAINS = [
    "arxiv.org", "export.arxiv.org", "doi.org", "dx.doi.org",
    "semanticscholar.org", "api.semanticscholar.org", "openreview.net",
    "nature.com", "science.org", "sciencedirect.com", "linkinghub.elsevier.com",
    "link.springer.com", "springer.com", "onlinelibrary.wiley.com", "pubs.acs.org",
    "journals.aps.org", "link.aps.org", "iopscience.iop.org", "pubs.rsc.org", "xlink.rsc.org",
    "pnas.org", "cell.com", "ieeexplore.ieee.org", "dl.acm.org", "proceedings.mlr.press",
    "proceedings.neurips.cc", "papers.nips.cc", "jmlr.org", "aclanthology.org"]

# Where `claude` usually lives when the worker's PATH is thin (a login item, a detached
# worker): the native installer, npm's user prefix, Homebrew.
EXTRA_PATH = ["~/.local/bin", "~/.claude/local", "~/.npm-global/bin", "/opt/homebrew/bin",
              "/usr/local/bin", "/usr/bin", "/bin"]


def search_path() -> str:
    parts = [p for p in os.environ.get("PATH", "").split(os.pathsep) if p]
    for p in EXTRA_PATH:
        p = os.path.expanduser(p)
        if p not in parts:
            parts.append(p)
    return os.pathsep.join(parts)


def resolve() -> tuple[str | None, str | None]:
    """(path of `claude`, its resolved target). Looked up at every spawn, never pinned.
    PAPERCAST_CLAUDE names it explicitly (tests, or an unusual install)."""
    explicit = os.environ.get("PAPERCAST_CLAUDE", "").strip()
    if explicit:                       # authoritative: never falls back to another claude
        p = explicit if os.path.isfile(explicit) and os.access(explicit, os.X_OK) else None
    else:
        p = shutil.which("claude", path=search_path())
    if not p:
        return None, None
    return p, os.path.realpath(p)


# Passed through to Claude Code besides HOME USER LOGNAME LANG PATH: the person's own login
# location (CLAUDE_CONFIG_DIR) and what a laptop needs to reach the internet at all.
PASS_ENV = ("CLAUDE_CONFIG_DIR", "TMPDIR", "HTTPS_PROXY", "https_proxy", "HTTP_PROXY",
            "http_proxy", "NO_PROXY", "no_proxy", "SSL_CERT_FILE", "NODE_EXTRA_CA_CERTS")


def env() -> dict:
    """`env -i` plus only what Claude Code needs. Never inherited: CLAUDECODE, CLAUDE_CODE_*
    (a `papercast add` typed inside a Claude Code session), any ANTHROPIC_* (an API key would
    silently move the run off the person's subscription login)."""
    home = os.path.expanduser("~")
    user = os.environ.get("USER") or os.environ.get("LOGNAME") or ""
    e = {"HOME": home, "USER": user, "LOGNAME": os.environ.get("LOGNAME", user),
         "LANG": "C.UTF-8", "PATH": search_path()}
    for k in PASS_ENV:
        v = os.environ.get(k)
        if v:                          # an empty CLAUDE_CONFIG_DIR is not an unset one
            e[k] = v
    return e


def lockdown(model: str, target: str | None = None, domains: list[str] | None = None) -> list[str]:
    """The episode run's flags: Leo's `claude.lockdown` without the vault and library
    directories (the group has neither), so the cwd is the only directory the agent has."""
    allow, deny = web_rules(domains if domains is not None else FETCH_DOMAINS, target)
    return [
        "--model", model, "--output-format", "stream-json", "--verbose",
        "--safe-mode", "--restricted", "--disable-slash-commands",
        "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
        "--tools", ",".join(TOOLS),
        "--permission-mode", EXPECTED_PERMISSION_MODE,
        # WebFetch only for the named sites (never the bare name, which allows every URL);
        # with dontAsk anything not allowed here is refused without a prompt.
        "--allowedTools", *["Read", "Glob", "Grep", "Write", "Edit", "WebSearch"], *allow,
    ] + (["--disallowedTools", *deny] if deny else [])


def lockdown_bare(model: str) -> list[str]:
    """No tools at all (the link grading reads only its prompt): no MCP, no slash commands,
    no customisations, no directories, no session kept on disk. Leo's `lockdown_bare`."""
    return [
        "--model", model, "--output-format", "stream-json", "--verbose",
        "--safe-mode", "--restricted", "--disable-slash-commands",
        "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
        "--tools", "", "--permission-mode", EXPECTED_PERMISSION_MODE,
        "--no-session-persistence",
    ]


def check_init(ev: dict | None) -> str | None:
    """None if the init event shows exactly the lockdown; else what differs."""
    if not isinstance(ev, dict):
        return "no init event"
    tools = ev.get("tools")
    if not isinstance(tools, list) or sorted(tools) != sorted(TOOLS):
        return f"tools {tools!r} != {sorted(TOOLS)!r}"
    if ev.get("mcp_servers") != []:
        return f"mcp_servers {ev.get('mcp_servers')!r} is not empty"
    if ev.get("permissionMode") != EXPECTED_PERMISSION_MODE:
        return f"permissionMode {ev.get('permissionMode')!r} != {EXPECTED_PERMISSION_MODE!r}"
    return None


def check_init_bare(ev: dict | None) -> str | None:
    """None if the init event shows no tools, no MCP servers and dontAsk."""
    if not isinstance(ev, dict):
        return "no init event"
    if ev.get("tools") != []:
        return f"tools {ev.get('tools')!r} != []"
    if ev.get("mcp_servers") != []:
        return f"mcp_servers {ev.get('mcp_servers')!r} is not empty"
    if ev.get("permissionMode") != EXPECTED_PERMISSION_MODE:
        return f"permissionMode {ev.get('permissionMode')!r} != {EXPECTED_PERMISSION_MODE!r}"
    return None


# --- WebFetch: named sites only ------------------------------------------------------
#
# Claude Code checks a WebFetch in this order (read from 2.1.283's bundled code, and measured,
# Leo's runner README "Web access"): deny rules, ask rules, allow rules, then its own list of
# "preapproved" documentation hosts, which it allows with no rule at all. So an allow list alone
# is not "named sites only": every preapproved host is denied explicitly. The list below is
# 2.1.283's; the list in the installed binary is read as well, so a CLI update that adds hosts
# is covered (the static list is the floor if the read fails).
PREAPPROVED_2_1_283 = (
    "platform.claude.com code.claude.com claude.com modelcontextprotocol.io github.com "
    "agentskills.io docs.python.org en.cppreference.com docs.oracle.com learn.microsoft.com "
    "developer.mozilla.org go.dev www.php.net docs.swift.org kotlinlang.org ruby-doc.org "
    "doc.rust-lang.org www.typescriptlang.org react.dev angular.io vuejs.org nextjs.org "
    "expressjs.com nodejs.org bun.sh jquery.com getbootstrap.com tailwindcss.com d3js.org "
    "threejs.org redux.js.org webpack.js.org jestjs.io reactrouter.com docs.djangoproject.com "
    "flask.palletsprojects.com fastapi.tiangolo.com pandas.pydata.org numpy.org "
    "www.tensorflow.org pytorch.org scikit-learn.org matplotlib.org requests.readthedocs.io "
    "jupyter.org laravel.com symfony.com wordpress.org docs.spring.io hibernate.org "
    "tomcat.apache.org gradle.org maven.apache.org asp.net dotnet.microsoft.com blazor.net "
    "reactnative.dev docs.flutter.dev developer.apple.com developer.android.com keras.io "
    "spark.apache.org huggingface.co www.kaggle.com www.mongodb.com redis.io www.postgresql.org "
    "dev.mysql.com www.sqlite.org graphql.org prisma.io docs.getdbt.com docs.aws.amazon.com "
    "cloud.google.com kubernetes.io www.docker.com www.terraform.io www.ansible.com vercel.com "
    "docs.stripe.com docs.netlify.com devcenter.heroku.com dev.wix.com cypress.io selenium.dev "
    "docs.unity.com docs.unrealengine.com git-scm.com nginx.org httpd.apache.org").split()

_PREAPPROVED_CACHE: dict[tuple, frozenset] = {}
_HOST_RE = re.compile(r"^[a-z0-9.-]+\.[a-z]{2,63}$")


def preapproved_hosts(binary: str | None) -> tuple[frozenset, str]:
    """(hosts, source): 2.1.283's list plus whatever list the installed CLI binary carries.
    Found by the one array that contains "docs.python.org" and "developer.mozilla.org"; read with
    mmap (the binary is ~240 MB; nothing is loaded into memory) once per binary version."""
    base = frozenset(PREAPPROVED_2_1_283)
    if not binary:
        return base, "static"
    try:
        st = os.stat(binary)
    except OSError:
        return base, "static"
    key = (binary, st.st_size, st.st_mtime_ns)
    if key not in _PREAPPROVED_CACHE:
        found: set[str] = set()
        try:
            with open(binary, "rb") as fh, mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ) as m:
                pos = 0
                while True:
                    i = m.find(b'"docs.python.org"', pos)
                    if i < 0:
                        break
                    pos = i + 1
                    a, b = m.rfind(b"[", max(0, i - 8192), i), m.find(b"]", i, i + 16384)
                    if a < 0 or b < 0:
                        continue
                    try:
                        arr = json.loads(m[a:b + 1].decode("utf-8"))
                    except (ValueError, UnicodeDecodeError):
                        continue
                    if (isinstance(arr, list) and "developer.mozilla.org" in arr
                            and all(isinstance(x, str) for x in arr)):
                        for x in arr:
                            h = x.split("/", 1)[0].lower()
                            if _HOST_RE.match(h):
                                found.add(h)
        except (OSError, ValueError):
            found = set()
        _PREAPPROVED_CACHE.clear()
        _PREAPPROVED_CACHE[key] = frozenset(found)
    extra = _PREAPPROVED_CACHE[key]
    return base | extra, ("cli" if extra else "static")


def _covered(host: str, allowed: list[str]) -> bool:
    for d in allowed:
        if d.startswith("*."):
            if host.endswith(d[1:]):
                return True
        elif host in (d, "www." + d):
            return True
    return False


def web_rules(domains: list[str], target: str | None) -> tuple[list[str], list[str]]:
    """(allow, deny) WebFetch rules: allow each named site (and its www. form), deny every
    preapproved host that is not one of them."""
    allow: list[str] = []
    for d in domains:
        allow.append(f"WebFetch(domain:{d})")
        if not d.startswith(("*.", "www.")):
            allow.append(f"WebFetch(domain:www.{d})")
    hosts, _ = preapproved_hosts(target)
    deny = [f"WebFetch(domain:{h})" for h in sorted(hosts) if not _covered(h, domains)]
    return list(dict.fromkeys(allow)), deny


# --- stream parsing -------------------------------------------------------------

class Turn:
    """Accumulates one `claude -p` run: from process start to its `result`."""

    def __init__(self):
        self.texts: list[str] = []
        self.last_assistant: dict | None = None
        self.result: dict | None = None
        self.errors: list[str] = []         # assistant `error` fields (model_not_found, ...)
        self.rate_limit: dict | None = None      # the latest rate_limit_info
        self.rate_limits: list[dict] = []        # every one of the turn (limits.detect)
        self.synthetic_texts: list[str] = []     # Claude Code's own error texts, never the model's
        self.init: dict | None = None
        self.tool_uses: int = 0

    def feed(self, ev: dict) -> None:
        t = ev.get("type")
        if t == "system" and ev.get("subtype") == "init":
            self.init = ev
        elif t == "assistant" and ev.get("parent_tool_use_id") is None:
            msg = ev.get("message") or {}
            for err in (ev.get("error"), msg.get("error")):
                if err:
                    self.errors.append(str(err))
            for block in msg.get("content") or []:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text" and block.get("text"):
                    self.texts.append(block["text"])
                    if msg.get("model") == "<synthetic>":
                        self.synthetic_texts.append(block["text"])
                elif block.get("type") == "tool_use":
                    self.tool_uses += 1
            if msg.get("model") != "<synthetic>":
                self.last_assistant = ev
        elif t == "rate_limit_event":
            self.rate_limit = ev.get("rate_limit_info") or {}
            self.rate_limits.append(self.rate_limit)
        elif t == "result":
            self.result = ev

    @property
    def text(self) -> str:
        return "\n\n".join(t.strip("\n") for t in self.texts if t.strip())

    @property
    def model_text(self) -> str:
        """The model's own words: `text` without Claude Code's synthetic error messages."""
        syn = list(self.synthetic_texts)
        out = []
        for t in self.texts:
            if t in syn:
                syn.remove(t)
            elif t.strip():
                out.append(t.strip("\n"))
        return "\n\n".join(out)

    @property
    def answer(self) -> str:
        """The final answer text: the result's `result`, else the model's text."""
        r = (self.result or {}).get("result")
        return r if isinstance(r, str) and r else self.model_text


def classify(turn: Turn, rc: int | None, stderr_last: str = "") -> tuple[str, str] | None:
    """None when the run succeeded; else (code, message). Judged by `is_error`, never by
    `subtype` (an unknown model is subtype "success")."""
    res = turn.result
    errs = " ".join(turn.errors)
    if "model_not_found" in errs:
        return "claude_model", "model id not accepted by Claude Code"
    if "authentication_failed" in errs:
        return "claude_auth", "Claude Code is not logged in on this machine: run `claude` once and log in"
    if res is not None:
        rerrs = " ".join(str(x) for x in (res.get("errors") or []))
        if "No conversation found" in rerrs:
            return "no_session", rerrs[:300]
        hit = limits.detect(turn, rc, stderr_last)
        if hit:
            return "claude_usage_limit", limits.message(hit)
        if res.get("is_error"):
            msg = res.get("result") or rerrs or stderr_last or "Claude Code reported an error"
            return "claude_exit", str(msg)[:400]
        if rc not in (0, None):
            return "claude_exit", (stderr_last or f"Claude Code exited {rc}")[:400]
        return None
    hit = limits.detect(turn, rc, stderr_last)
    if hit:
        return "claude_usage_limit", limits.message(hit)
    if rc is None:
        return "claude_exit", (stderr_last or "Claude Code ended without a result")[:400]
    return "claude_exit", (stderr_last or f"Claude Code exited {rc} without a result")[:400]


# --- one process ------------------------------------------------------------------

class Run:
    """What one process left: its Turn, exit status, last stderr line, and why it was stopped
    (a lockdown violation, the watchdog, the timeout), if it was."""

    def __init__(self, turn: Turn, rc: int | None, err_last: str, violation: str | None,
                 stopped: str | None, wall_s: float):
        self.turn, self.rc, self.err_last = turn, rc, err_last
        self.violation, self.stopped, self.wall_s = violation, stopped, wall_s


def _last_line(path: str) -> str:
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            n = fh.tell()
            fh.seek(max(0, n - 8192))
            lines = [l for l in fh.read().decode("utf-8", "replace").splitlines() if l.strip()]
        return lines[-1].strip() if lines else ""
    except OSError:
        return ""


def _stop(proc: subprocess.Popen, grace_s: float = 5.0) -> None:
    if proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=grace_s)
    except subprocess.TimeoutExpired:
        proc.kill()
    except OSError:
        pass


def run(argv: list[str], cwd: str, out_path: str, err_path: str, *, init_check=check_init,
        watchdog_s: float = 0.0, timeout_s: float = 0.0, on_event=None, on_start=None,
        tick=None, nice: int = NICE) -> Run:
    """Run one `claude -p` (argv[0] is the binary), niced, in `cwd`, with env(); stream-json
    lines are appended to `out_path` as they come, stderr to `err_path`. Blocks until the
    process ends. The init event is checked against the lockdown: any difference stops it."""
    nice_bin = shutil.which("nice")
    full = ([nice_bin, "-n", str(nice)] if nice and nice_bin else []) + list(argv)
    turn = Turn()
    violation = stopped = None
    t0 = time.monotonic()
    with open(out_path, "ab") as out_fh, open(err_path, "ab") as err_fh:
        proc = subprocess.Popen(full, cwd=cwd, env=env(), stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=err_fh)
        if on_start:
            on_start(proc.pid)
        q: queue.Queue = queue.Queue()

        def reader():
            try:
                for raw in proc.stdout:
                    q.put(raw)
            finally:
                q.put(None)

        th = threading.Thread(target=reader, daemon=True)
        th.start()
        last = t0
        while True:
            try:
                raw = q.get(timeout=1.0)
            except queue.Empty:
                raw = b""
            if raw is None:
                break
            if raw:
                last = time.monotonic()
                out_fh.write(raw)
                out_fh.flush()
                try:
                    ev = json.loads(raw)
                except ValueError:
                    ev = None
                if isinstance(ev, dict):
                    turn.feed(ev)
                    if ev.get("type") == "system" and ev.get("subtype") == "init" and init_check:
                        bad = init_check(ev)
                        if bad and not violation:
                            violation = bad
                            _stop(proc, 2.0)
                    if on_event:
                        on_event(ev, turn)
            now = time.monotonic()
            if tick:
                tick()
            if not stopped and watchdog_s and now - last > watchdog_s:
                stopped = f"no output from Claude Code for {watchdog_s / 60:g} minutes"
                _stop(proc, 10.0)
            if not stopped and timeout_s and now - t0 > timeout_s:
                stopped = f"no answer within {timeout_s:g} s"
                _stop(proc, 5.0)
        rc = proc.wait()
        th.join(timeout=5)
        proc.stdout.close()
    if init_check and turn.init is None and not violation and rc == 0:
        violation = "no init event"
    return Run(turn, rc, _last_line(err_path), violation, stopped, time.monotonic() - t0)
