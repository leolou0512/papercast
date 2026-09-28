"""Test support for the CLI shell (A7): a fake hub (stdlib http.server), a fake `claude` on PATH,
an isolated environment (HOME, XDG dirs, PATH without the real claude), and helpers to run
`papercast` as a real process and wait for its detached worker."""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

TESTS = Path(__file__).resolve().parent
PKG = TESTS.parent
if str(PKG) not in sys.path:
    sys.path.insert(0, str(PKG))

FAKE_CLAUDE = """#!/bin/sh
# A fake Claude Code: answers `auth status --json` only. Never the real one in tests.
[ -n "$FAKE_CLAUDE_LOG" ] && echo "$*" >> "$FAKE_CLAUDE_LOG"
if [ "$1" = "auth" ] && [ "$2" = "status" ]; then
  if [ -n "$FAKE_CLAUDE_LOGGED_OUT" ]; then echo '{"loggedIn": false}'; else
    echo '{"loggedIn": true, "authMethod": "claude.ai", "subscriptionType": "pro"}'; fi
  exit 0
fi
echo "fake claude: unexpected $*" >&2
exit 1
"""

USER = {"id": 7, "name": "Ada", "email": "ada@example.org", "role": "contributor"}


class FakeHub:
    """The CLI API of SPEC.md sections 2 and 4, just enough for the CLI's tests."""

    def __init__(self):
        self.requests: list[dict] = []
        self.tokens: dict[str, dict] = {"pcg_valid": dict(USER)}
        self.revoked: list[str] = []
        self.poll_script: list[tuple[int, dict]] = [(428, {"error": "pending"})]
        self.expires_in = 600
        self.prefs = {"settings": {"maths": "full"}, "note": "", "version": 1}
        self.prefs_put = True
        self.episodes: list[dict] = []
        self.lookup = {"paper": None, "claim": None}
        self.slack = None           # {"enabled", "channel", "default"}; None: a hub without Slack (404)
        self.fail_next: list[int] = []            # statuses answered before anything else
        self.html_paths: set[str] = set()
        self.uploads: list[bytes] = []
        self.lock = threading.Lock()
        hub = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _send(self, code, obj=None, ctype="application/json", raw=None, headers=None):
                body = raw if raw is not None else (b"" if obj is None else json.dumps(obj).encode())
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(body)

            def _go(self, method):
                u = urlsplit(self.path)
                q = {k: v[0] for k, v in parse_qs(u.query).items()}
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n) if n else b""
                try:
                    body = json.loads(raw) if raw and "json" in (self.headers.get("Content-Type") or "") else raw
                except ValueError:
                    body = raw
                tok = (self.headers.get("Authorization") or "").removeprefix("Bearer ").strip()
                with hub.lock:
                    hub.requests.append({"method": method, "path": u.path, "query": q,
                                         "body": body, "token": tok,
                                         "ua": self.headers.get("User-Agent")})
                    if hub.fail_next:
                        return self._send(hub.fail_next.pop(0), {"error": "boom", "message": "the hub fell over"})
                if u.path in hub.html_paths:
                    return self._send(200, raw=b"<!doctype html><title>Sign in</title>", ctype="text/html")
                p = u.path
                if p == "/api/cli/login/start" and method == "POST":
                    return self._send(200, {"code": "ABCD-EFGH", "url": f"{hub.url}/cli?code=ABCD-EFGH",
                                            "poll": "s3cret", "interval": 0, "expires_in": hub.expires_in})
                if p == "/api/cli/login/poll" and method == "POST":
                    with hub.lock:
                        code, obj = hub.poll_script.pop(0) if len(hub.poll_script) > 1 else hub.poll_script[0]
                        if code == 200:
                            hub.tokens[obj["token"]] = obj.get("user") or dict(USER)
                    return self._send(code, obj)
                if p == "/redirect":
                    return self._send(302, {}, headers={"Location": "https://login.example.org/"})
                user = hub.tokens.get(tok)
                if not user:
                    return self._send(401, {"error": "login_required", "message": "log in first"})
                if p == "/api/cli/me":
                    return self._send(200, user)
                if p == "/api/cli/logout" and method == "POST":
                    with hub.lock:
                        hub.tokens.pop(tok, None)
                        hub.revoked.append(tok)
                    return self._send(204)
                if p == "/api/cli/prefs":
                    if method == "GET":
                        return self._send(200, hub.prefs)
                    if method == "PUT":
                        if not hub.prefs_put:
                            return self._send(404, {"error": "not_found", "message": "no such page"})
                        hub.prefs = {"settings": body["settings"], "note": body["note"],
                                     "version": hub.prefs["version"] + 1}
                        return self._send(200, hub.prefs)
                if p == "/api/cli/lookup":
                    return self._send(200, hub.lookup)
                if p == "/api/cli/features" and hub.slack is not None:
                    return self._send(200, {"slack": hub.slack})
                if p == "/api/cli/slack" and method == "PUT" and hub.slack is not None:
                    hub.slack = dict(hub.slack, default=body["default"])
                    return self._send(200, hub.slack)
                if p == "/api/cli/episodes" and method == "GET":
                    return self._send(200, {"episodes": hub.episodes})
                if p == "/api/cli/episodes" and method == "POST":
                    if user.get("role") == "viewer":
                        return self._send(403, {"error": "forbidden", "message": "contributors only"})
                    with hub.lock:
                        hub.uploads.append(raw)
                        n = len(hub.uploads)
                    return self._send(201, {"episode_id": f"e_up{n}", "paper_id": f"p_up{n}",
                                            "state": "checking"})
                if p.startswith("/api/cli/episodes/"):
                    eid = p.rsplit("/", 1)[1]
                    for e in hub.episodes:
                        if e.get("id") == eid:
                            return self._send(200, e)
                    return self._send(404, {"error": "not_found"})
                if p.startswith("/api/cli/claims/") and method == "DELETE":
                    return self._send(204)
                if p == "/api/cli/conflict":
                    return self._send(409, {"error": "in_progress", "by": {"name": "Bob"},
                                            "since": "2026-09-28T04:12:09Z"})
                if p == "/api/cli/forbidden":
                    return self._send(403, {"error": "disabled", "message": "account disabled"})
                return self._send(404, {"error": "not_found", "message": "no such page"})

            def do_GET(self):
                self._go("GET")

            def do_POST(self):
                self._go("POST")

            def do_PUT(self):
                self._go("PUT")

            def do_DELETE(self):
                self._go("DELETE")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()

    def paths(self, method=None):
        return [r["path"] for r in self.requests if method is None or r["method"] == method]


def free_port() -> int:
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class CliTestCase(unittest.TestCase):
    """Each test gets its own HOME, XDG dirs, a fake claude and a fake hub. `self.env` is the
    environment for `papercast` processes; `os.environ` is patched the same for in-process
    calls."""

    hub_needed = True

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="pcg-cli-"))
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        claude = self.bin / "claude"
        claude.write_text(FAKE_CLAUDE)
        claude.chmod(0o755)
        system = [d for d in ("/usr/bin", "/bin") if not os.path.exists(os.path.join(d, "claude"))]
        path = os.pathsep.join([str(self.bin), *system])
        found = shutil.which("claude", path=path)
        assert found == str(claude), f"the real claude would be found: {found}"
        self.hub = FakeHub() if self.hub_needed else None
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("PAPERCAST_", "XDG_", "CLAUDE", "FAKE_", "SSH_"))
               and k not in ("DISPLAY", "WAYLAND_DISPLAY")}
        env.update({
            "HOME": str(self.home), "PATH": path,
            "XDG_CONFIG_HOME": str(self.tmp / "config"), "XDG_STATE_HOME": str(self.tmp / "state"),
            "PYTHONPATH": os.pathsep.join([str(PKG), str(TESTS)]),
            "PAPERCAST_PIPELINE": "cli_fake_pipeline:run_job",
            "PAPERCAST_POLL_S": "0.1", "PAPERCAST_LIMIT_MARGIN_S": "0",
            "PAPERCAST_NO_BROWSER": "1", "PAPERCAST_RETRY_BASE_S": "0.01",
            "FAKE_CLAUDE_LOG": str(self.tmp / "claude-calls.log"),
            "PYTHONDONTWRITEBYTECODE": "1",
        })
        self.env = env
        self._saved_env = dict(os.environ)
        os.environ.clear()
        os.environ.update(env)
        if str(TESTS) not in sys.path:
            sys.path.insert(0, str(TESTS))

    def tearDown(self):
        self.kill_everything()
        os.environ.clear()
        os.environ.update(self._saved_env)
        if self.hub:
            self.hub.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ------------------------------------------------------------------ helpers
    @property
    def state(self) -> Path:
        return self.tmp / "state" / "papercast"

    def login_config(self, token="pcg_valid", server=None, **extra):
        from papercast_cli import config
        config.save({"server": server or self.hub.url, "token": token, "device": "ada@laptop",
                     **extra})

    def run_cli(self, *args, env=None, input=None, timeout=60) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, "-m", "papercast_cli", *map(str, args)],
                              env=env or self.env, input=input, capture_output=True, text=True,
                              timeout=timeout, cwd=str(self.tmp))

    def pdf(self, name="paper.pdf", **directives) -> Path:
        """A 'PDF' whose lines tell the fake pipeline what to do (sleep=1.5, limit_once=2...)."""
        p = self.tmp / name
        lines = ["%PDF-1.4", f"% {name} {time.time_ns()}"]
        lines += [f"{k}={v}" for k, v in directives.items()]
        p.write_text("\n".join(lines) + "\n")
        return p

    def jobs(self) -> list[dict]:
        root = self.state / "jobs"
        out = []
        if root.is_dir():
            for d in sorted(root.iterdir()):
                try:
                    out.append(json.loads((d / "job.json").read_text()))
                except (OSError, ValueError):
                    pass
        out.sort(key=lambda j: (j.get("created_at") or "", j.get("created_t") or 0))
        return out

    def job(self, jid) -> dict:
        return json.loads((self.state / "jobs" / jid / "job.json").read_text())

    def events(self, jid) -> list[dict]:
        p = self.state / "jobs" / jid / "fake-events"
        if not p.exists():
            return []
        return [json.loads(l) for l in p.read_text().splitlines() if l.strip()]

    def wait_for(self, cond, timeout=30.0, what="condition", step=0.05):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            v = cond()
            if v:
                return v
            time.sleep(step)
        self.fail(f"timed out waiting for {what}; jobs: {json.dumps(self.jobs(), indent=1)[:3000]}"
                  f"\nworker.log: {self.worker_log()[-3000:]}")

    def worker_log(self) -> str:
        try:
            return (self.state / "worker.log").read_text()
        except OSError:
            return ""

    def worker_pid(self) -> int | None:
        from papercast_cli import jobs
        pid = jobs.worker_pid()
        return pid if pid and pid > 0 else None

    def all_done(self, n=None):
        js = self.jobs()
        return (n is None or len(js) == n) and js and all(
            j["state"] in ("done", "failed", "cancelled", "asking") for j in js)

    def wait_worker_gone(self, timeout=15):
        self.wait_for(lambda: self.worker_pid() is None, timeout, "the worker to exit")

    def kill_everything(self):
        """No process of a test outlives it: job process groups, then the worker's."""
        from papercast_cli import jobs as J
        try:
            for j in self.jobs():
                d = self.state / "jobs" / j["id"]
                pid = j.get("pid")
                if pid and J.lock_held(d / "run.lock"):
                    try:
                        os.killpg(int(pid), signal.SIGKILL)
                    except OSError:
                        pass
            wp = self.worker_pid()
            if wp:
                try:
                    os.killpg(wp, signal.SIGKILL)
                except OSError:
                    pass
        except Exception:                                   # noqa: BLE001
            pass
        for p in getattr(self, "_extra_pids", []):
            try:
                os.killpg(p, signal.SIGKILL)
            except OSError:
                pass
        for p in list(J._spawned):
            try:
                p.wait(timeout=5)
            except Exception:                               # noqa: BLE001
                pass
            J._spawned.remove(p)
