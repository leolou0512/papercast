"""The whole path on one machine, with fakes (SPEC.md: A10): a hub (PCG_AUTH=header, a temporary
data directory, a free port on 127.0.0.1), users, an episode made and uploaded, voiced by the real
voice worker (deploy/voice_worker.py) with a fake papercast-voice that writes a short MP3, and
the web API showing the episode ready with its audio.

    python3 -m unittest discover -s stacks/papercast-group/tests -p 'e2e_test.py' -v

Two tests:
  test_bundle_voice_web   CLI API by hand (device login, claim, a bundle built here) -> hub
                          checks -> voice queue -> worker -> web. Needs A2 (auth), A3 (contrib,
                          voiceq), A4 (web), A9 (common/).
  test_cli_voice_web      the real CLI (A7 + A8's pipeline, `papercast login` and `papercast add`)
                          with a fake `claude` on PATH -> hub -> worker -> web.
A test whose parts are not merged yet is skipped, and says which part and what answered.
Nothing here talks to the network or to a real Claude; the hub listens on 127.0.0.1 only and
lives for the test (fake data: on a shared machine anyone local could reach it meanwhile).
"""
from __future__ import annotations

import glob
import hashlib
import io
import json
import os
import re
import signal
import socket
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
PG = HERE.parent                                  # stacks/papercast-group
ROOT = PG.parent.parent                           # the repo
CLI = ROOT / "packages" / "papercast-cli"
WORKER = PG / "deploy" / "voice_worker.py"
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(PG / "deploy" / "tests"))
sys.path.insert(0, str(CLI))

import fake_claude  # noqa: E402
import fake_papercast_voice as fake_voice  # noqa: E402

WORKER_TOKEN = "pcgw_e2e_" + "x" * 20
ADMIN, ALICE, BOB = "admin@example.test", "alice@example.test", "bob@example.test"
TIMEOUT_CLI_S = float(os.environ.get("PCG_E2E_CLI_TIMEOUT", "300"))


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def wait_for(cond, timeout: float, what: str, every: float = 0.2):
    end = time.time() + timeout
    last = None
    while time.time() < end:
        last = cond()
        if last:
            return last
        time.sleep(every)
    raise AssertionError(f"timed out after {timeout:.0f} s waiting for {what} (last: {last!r})")


def tiny_pdf(text: str) -> bytes:
    """A valid one-page PDF with one line of text (for `papercast add`)."""
    esc = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    stream = f"BT /F1 18 Tf 72 720 Td ({esc}) Tj ET".encode()
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
            b"/Resources << /Font << /F1 5 0 R >> >> >>",
            b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offs = []
    for i, o in enumerate(objs, 1):
        offs.append(out.tell())
        out.write(b"%d 0 obj\n" % i + o + b"\nendobj\n")
    xref = out.tell()
    out.write(b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1))
    for o in offs:
        out.write(b"%010d 00000 n \n" % o)
    out.write(b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref))
    return out.getvalue()


def bundle(manifest: dict, files: dict) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, data in [("manifest.json", json.dumps(manifest).encode())] + list(files.items()):
            data = data if isinstance(data, bytes) else data.encode()
            ti = tarfile.TarInfo(name)
            ti.size = len(data)
            ti.mtime = int(time.time())
            tf.addfile(ti, io.BytesIO(data))
    return buf.getvalue()


def explainer_html(title: str) -> str:
    return ("<!doctype html>\n<html><head><meta charset=\"utf-8\"><title>" + title +
            "</title></head><body><h1>" + title + "</h1><p>A test explainer.</p></body></html>\n")


class Hub:
    """The hub under test, as a subprocess."""

    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.data = tmp / "data"
        self.port = free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self.env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(tmp),
            "PYTHONPATH": f"{PG}{os.pathsep}{CLI}", "PYTHONUNBUFFERED": "1",
            "PCG_DATA": str(self.data), "PCG_AUTH": "header", "PCG_BIND": "127.0.0.1",
            "PCG_PORT": str(self.port), "PCG_PUBLIC_URL": self.url,
            "PCG_SECRET": "e2e-secret-" + "s" * 32, "PCG_ADMIN_EMAILS": ADMIN,
            "PCG_WORKER_TOKEN_SHA256": hashlib.sha256(WORKER_TOKEN.encode()).hexdigest(),
            "PCG_LOG": "INFO"}
        self.log = open(tmp / "hub.log", "ab")
        self.p = subprocess.Popen([sys.executable, "-m", "hub.app"], cwd=str(PG), env=self.env,
                                  stdout=self.log, stderr=subprocess.STDOUT)
        wait_for(self.answers, 30, "the hub to answer")

    def answers(self):
        if self.p.poll() is not None:
            raise AssertionError(f"the hub exited {self.p.returncode}:\n{self.logtext()}")
        try:
            urllib.request.urlopen(self.url + "/", timeout=2)
        except urllib.error.HTTPError:
            return True
        except OSError:
            return False
        return True

    def logtext(self) -> str:
        try:
            return (self.tmp / "hub.log").read_text()[-4000:]
        except OSError:
            return ""

    def stop(self):
        if self.p.poll() is None:
            self.p.send_signal(signal.SIGTERM)
            try:
                self.p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.p.kill()
        self.log.close()

    def call(self, method: str, path: str, user: str | None = None, body=None, token: str | None = None,
             headers: dict | None = None, raw: bytes | None = None, ctype: str | None = None):
        """(status, headers, JSON or bytes)."""
        h = {}
        if user:
            h["X-Test-User"] = user
        if token:
            h["Authorization"] = f"Bearer {token}"
        if user and method not in ("GET", "HEAD"):
            h.update({"X-PCG": "1", "Origin": self.url})      # the page's CSRF rule (SPEC section 2)
        data = raw
        if body is not None:
            data = json.dumps(body).encode()
            h["Content-Type"] = "application/json"
        if ctype:
            h["Content-Type"] = ctype
        h.update(headers or {})
        req = urllib.request.Request(self.url + path, data=data, method=method, headers=h)
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                st, hd, b = r.status, dict(r.headers), r.read()
        except urllib.error.HTTPError as e:
            st, hd, b = e.code, dict(e.headers), e.read()
        if "json" in hd.get("Content-Type", ""):
            try:
                return st, hd, json.loads(b or b"null")
            except ValueError:
                pass
        return st, hd, b


def as_list(v, key: str):
    if isinstance(v, dict):
        v = v.get(key, v.get("items", []))
    return v if isinstance(v, list) else []


class E2E(unittest.TestCase):
    def setUp(self):
        self.tmpd = tempfile.TemporaryDirectory(prefix="pcg-e2e-")
        self.tmp = Path(self.tmpd.name)
        self.hub = Hub(self.tmp)
        self.extra_homes: list[Path] = []

    def tearDown(self):
        self.hub.stop()
        for home in self.extra_homes:
            kill_by_home(home)
        if os.environ.get("PCG_E2E_KEEP"):
            print(f"kept {self.tmp}", file=sys.stderr)
        else:
            self.tmpd.cleanup()

    # ------------------------------------------------------------ parts
    def need(self, cond: bool, part: str, what):
        if not cond:
            self.skipTest(f"{part} is not merged yet ({what})")

    def users(self) -> None:
        """admin (from PCG_ADMIN_EMAILS), alice made a contributor by the admin, bob a viewer."""
        st, _, me = self.hub.call("GET", "/api/me", user=ADMIN)
        self.need(st == 200, "auth (A2) / web (A4)", f"GET /api/me -> {st} {me!r:.120}")
        role = (me.get("user", me) if isinstance(me, dict) else {}).get("role")
        self.assertEqual(role, "admin", f"PCG_ADMIN_EMAILS makes {ADMIN} an admin: {me}")
        for u in (ALICE, BOB):
            st, _, r = self.hub.call("GET", "/api/me", user=u)
            self.assertEqual(st, 200, r)
        st, _, users = self.hub.call("GET", "/api/admin/users", user=ADMIN)
        self.need(st == 200, "admin API (A4)", f"GET /api/admin/users -> {st}")
        alice = [u for u in as_list(users, "users") if u.get("email") == ALICE]
        self.assertTrue(alice, users)
        st, _, r = self.hub.call("PUT", f"/api/admin/users/{alice[0]['id']}", user=ADMIN,
                                 body={"role": "contributor"})
        self.assertIn(st, (200, 204), r)

    def cli_token(self, user: str) -> str:
        """The device login of SPEC section 2, done by hand."""
        st, _, r = self.hub.call("POST", "/api/cli/login/start", body={"device": "e2e test"})
        self.need(st == 200, "CLI login (A2)", f"POST /api/cli/login/start -> {st} {r!r:.120}")
        self.assertRegex(r["code"], r"^[A-Z0-9]{4}-[A-Z0-9]{4}$")
        st, _, p = self.hub.call("POST", "/api/cli/login/poll", body={"poll": r["poll"]})
        self.assertEqual(st, 428, p)
        st, _, a = self.hub.call("POST", "/api/cli/login/approve", user=user,
                                 body={"code": r["code"], "approve": True})
        self.assertIn(st, (200, 204), a)
        st, _, p = self.hub.call("POST", "/api/cli/login/poll", body={"poll": r["poll"]})
        self.assertEqual(st, 200, p)
        self.assertTrue(p["token"].startswith("pcg_"), p)
        return p["token"]

    def voice_api(self) -> None:
        st, _, r = self.hub.call("POST", "/api/voice/claim", token=WORKER_TOKEN, body={})
        self.need(st in (200, 204), "the voice queue (A3)", f"POST /api/voice/claim -> {st} {r!r:.120}")
        self.assertEqual(st, 204, f"nothing is queued yet: {r!r:.200}")
        st, _, r = self.hub.call("POST", "/api/voice/claim", token="pcgw_wrong", body={})
        self.assertIn(st, (401, 403), "a wrong worker token is refused")

    def run_worker(self, seconds: float = 4.0) -> Path:
        """The real worker with a fake voice, until the queue is empty. Returns its job root."""
        (self.tmp / "worker.token").write_text(WORKER_TOKEN + "\n")
        voice = fake_voice.write_wrapper(str(self.tmp / "papercast-voice"), seconds=seconds, chunk_s=0.01)
        jobs = self.hub.data / "episodes"                    # as installed: $PCG_DATA/episodes
        env = {"HOME": str(self.tmp), "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
               "USER": os.environ.get("USER", "leo"), "PCG_HUB_URL": self.hub.url,
               "PCG_WORKER_TOKEN_FILE": str(self.tmp / "worker.token"), "PAPERCAST_VOICE_CMD": voice,
               "PCG_VOICE_JOBS": str(jobs), "PCG_WORKER_STATE": str(self.tmp / "worker"),
               "PCG_POLL_S": "0.2", "PCG_HEARTBEAT_S": "1", "PCG_MONITOR_S": "0.1",
               "PCG_BACKOFF_MAX_S": "1"}
        with open(self.tmp / "worker.log", "ab") as log:
            p = subprocess.run([sys.executable, str(WORKER), "--exit-when-idle"], env=env,
                               stdout=log, stderr=subprocess.STDOUT, timeout=180)
        self.assertEqual(p.returncode, 0, (self.tmp / "worker.log").read_text()[-3000:])
        return jobs

    def check_ready(self, eid: str, pid: str | None, title: str, token: str, jobs: Path, seconds: float):
        """What the page would show: the episode ready, with its audio, for another user too."""
        st, _, r = self.hub.call("GET", f"/api/cli/episodes/{eid}", token=token)
        self.assertEqual(st, 200, r)
        ep = r.get("episode", r)
        self.assertEqual(ep.get("state"), "ready", ep)
        up = json.loads((jobs / eid / "voice" / "uploaded.json").read_text())
        st, _, lib = self.hub.call("GET", "/api/library", user=BOB)
        self.assertEqual(st, 200, lib)
        papers = [p for p in as_list(lib, "papers") if p.get("title") == title]
        self.assertEqual(len(papers), 1, lib)
        paper = papers[0]
        if pid:
            self.assertEqual(paper["id"], pid)
        eps = [e for e in paper.get("episodes", []) if e.get("id") == eid]
        self.assertEqual(len(eps), 1, paper)
        self.assertEqual(eps[0].get("state"), "ready", eps[0])
        self.assertAlmostEqual(float(eps[0].get("duration_s") or 0), up["duration_s"], delta=0.5)
        self.assertAlmostEqual(up["duration_s"], seconds, delta=0.1)
        st, _, one = self.hub.call("GET", f"/api/papers/{paper['id']}", user=BOB)
        self.assertEqual(st, 200, one)
        st, hd, audio = self.hub.call("GET", f"/audio/{eid}.mp3", user=BOB)
        self.assertEqual(st, 200, audio if len(audio) < 300 else len(audio))
        self.assertTrue(hd.get("Content-Type", "").startswith("audio/mpeg"), hd)
        self.assertEqual(hashlib.sha256(audio).hexdigest(), up["sha256"], "the bytes the voice made")
        st, hd, part = self.hub.call("GET", f"/audio/{eid}.mp3", user=BOB, headers={"Range": "bytes=0-99"})
        self.assertEqual((st, len(part)), (206, 100))
        st, hd, page = self.hub.call("GET", f"/x/{eid}/explainer.html", user=BOB)
        self.assertEqual(st, 200)
        self.assertIn("Content-Security-Policy", hd)
        st, _, _ = self.hub.call("GET", f"/audio/{eid}.mp3")
        self.assertIn(st, (401, 403), "no audio without a login")

    # ------------------------------------------------------------ the tests
    def test_bundle_voice_web(self):
        self.users()
        token = self.cli_token(ALICE)
        self.voice_api()
        st, _, prompt = self.hub.call("GET", "/api/cli/prompt", token=token)
        self.need(st == 200, "the CLI API (A3)", f"GET /api/cli/prompt -> {st}")
        title = "Straight Paths, an End to End Test"
        st, _, look = self.hub.call("GET", "/api/cli/lookup?title=" + urllib.parse.quote(title), token=token)
        self.assertEqual(st, 200, look)
        self.assertIsNone(look.get("paper"), look)
        st, _, claim = self.hub.call("POST", "/api/cli/claims", token=token,
                                     body={"keys": {"title": title}, "device": "e2e test"})
        self.assertEqual(st, 201, claim)
        script = fake_claude.script_text()
        words = len(script.split())
        manifest = {
            "manifest_version": 1, "client_version": "0.1.0-e2e", "base_version": prompt.get("version"),
            "prefs": {"settings": {}, "note": "", "version": 1}, "model": "fake",
            "claim_id": claim["claim_id"], "paper_id": None,
            "paper": {"title": title, "authors": ["Ada Lovelace", "Alan Turing"], "year": 2022,
                      "arxiv_id": None, "doi": None, "url": None, "source_sha256": None,
                      "tags": ["diffusion", "generative models"]},
            "files": {"script": "script.md", "explainer_json": "explainer.json",
                      "explainer_html": "explainer.html", "claims": "claims.md"},
            "links": [], "stats": {"words": words, "est_minutes": round(words / 150, 1), "wall_s": 1}}
        body = bundle(manifest, {"script.md": script, "explainer.json": json.dumps(fake_claude.explainer()),
                                 "explainer.html": explainer_html(title), "claims.md": fake_claude.claims()})
        st, _, up = self.hub.call("POST", "/api/cli/episodes", token=token, raw=body, ctype="application/gzip")
        self.need(st != 404, "the bundle upload (A3)", f"POST /api/cli/episodes -> {st}")
        self.assertEqual(st, 201, up)
        eid, pid = up["episode_id"], up["paper_id"]

        def checked():
            s, _, r = self.hub.call("GET", f"/api/cli/episodes/{eid}", token=token)
            e = r.get("episode", r) if isinstance(r, dict) else {}
            if e.get("state") == "rejected":
                raise AssertionError(f"the hub rejected the bundle: {e.get('check_report')}")
            return e.get("state") == "waiting-for-gpu"
        wait_for(checked, 60, "the hub's checks")
        jobs = self.run_worker(seconds=4.0)
        self.check_ready(eid, pid, title, token, jobs, 4.0)

    def test_cli_voice_web(self):
        cmd = cli_command()
        self.need(cmd is not None, "the CLI (A7)", f"no papercast_cli/cli.py in {CLI}")
        self.need((CLI / "papercast_cli" / "pipeline" / "run.py").exists(), "the CLI's pipeline (A8)",
                  "no papercast_cli/pipeline/run.py")
        self.users()
        self.voice_api()
        home = self.tmp / "alice-home"
        (home / "bin").mkdir(parents=True)
        self.extra_homes.append(home)
        claude = install_fake_claude(home / "bin")
        env = {"HOME": str(home), "USER": "alice", "LANG": "C.UTF-8",
               "PATH": f"{home / 'bin'}{os.pathsep}{os.environ.get('PATH', '/usr/bin:/bin')}",
               "PYTHONPATH": str(CLI), "PYTHONUNBUFFERED": "1", "BROWSER": "true",
               "PAPERCAST_NO_BROWSER": "1", "NO_COLOR": "1"}
        # 1. papercast login: read the code it prints, approve it as alice, as the web page would
        p = subprocess.Popen(cmd + ["login", "--server", self.hub.url], env=env, cwd=str(home),
                             stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        out: list[str] = []
        threading.Thread(target=lambda: out.extend(l.decode(errors="replace") for l in p.stdout),
                         daemon=True).start()
        code = wait_for(lambda: next((m.group(0) for l in list(out)
                                      for m in [re.search(r"\b[A-Z0-9]{4}-[A-Z0-9]{4}\b", l)] if m), None),
                        30, "papercast login to print its code")
        st, _, a = self.hub.call("POST", "/api/cli/login/approve", user=ALICE,
                                 body={"code": code, "approve": True})
        self.assertIn(st, (200, 204), a)
        self.assertEqual(p.wait(timeout=60), 0, "".join(out))
        cfgf = home / ".config" / "papercast" / "config.json"
        cfg = json.loads(cfgf.read_text())
        self.assertEqual(cfg.get("server", "").rstrip("/"), self.hub.url)
        self.assertEqual(oct(cfgf.stat().st_mode & 0o777), "0o600")
        token = cfg["token"]
        # 2. papercast add <pdf>: detached, so wait on the hub for the upload
        pdf = home / "straight-paths.pdf"
        pdf.write_bytes(tiny_pdf(fake_claude.TITLE))
        r = subprocess.run(cmd + ["add", str(pdf)], env=env, cwd=str(home), stdin=subprocess.DEVNULL,
                           capture_output=True, timeout=120)
        self.assertEqual(r.returncode, 0, (r.stdout + r.stderr).decode(errors="replace"))

        def uploaded():
            s, _, lst = self.hub.call("GET", "/api/cli/episodes?mine=1", token=token)
            eps = as_list(lst, "episodes")
            bad = [e for e in eps if e.get("state") == "rejected"]
            if bad:
                raise AssertionError(f"the hub rejected the CLI's bundle: {bad[0].get('check_report')}")
            ok = [e for e in eps if e.get("state") == "waiting-for-gpu"]
            return ok[0] if ok else None
        try:
            ep = wait_for(uploaded, TIMEOUT_CLI_S, "the CLI to upload its episode", every=1.0)
        except AssertionError:
            s = subprocess.run(cmd + ["status", "--all"], env=env, cwd=str(home), capture_output=True, timeout=60)
            logs = "".join(f"\n--- {f}\n" + Path(f).read_text(errors="replace")[-1500:]
                           for f in glob.glob(str(home / ".local/state/papercast/jobs/*/*.log")))
            raise AssertionError("no upload from the CLI.\npapercast status --all:\n"
                                 + (s.stdout + s.stderr).decode(errors="replace") + logs)
        self.assertTrue(claude.exists())
        jobs = self.run_worker(seconds=5.0)
        self.check_ready(ep["id"], ep.get("paper_id"), fake_claude.TITLE, token, jobs, 5.0)


def cli_command():
    """How to run the CLI from the source tree: its __main__, or cli.main()."""
    if (CLI / "papercast_cli" / "__main__.py").exists():
        return [sys.executable, "-m", "papercast_cli"]
    if (CLI / "papercast_cli" / "cli.py").exists():
        return [sys.executable, "-c", "import sys; from papercast_cli.cli import main; sys.exit(main())"]
    return None


def install_fake_claude(bindir: Path) -> Path:
    """`claude` on PATH: $PCG_E2E_FAKE_CLAUDE, else A8's own fake if it ships one, else ours."""
    src = os.environ.get("PCG_E2E_FAKE_CLAUDE")
    if not src:
        theirs = sorted(glob.glob(str(CLI / "tests" / "fake_claude*")))
        src = theirs[0] if theirs else str(HERE / "fake_claude.py")
    dst = bindir / "claude"
    if src.endswith(".py"):
        dst.write_text(f"#!/bin/sh\nexec '{sys.executable}' '{src}' \"$@\"\n")
    else:
        dst.write_text(f"#!/bin/sh\nexec '{src}' \"$@\"\n")
    dst.chmod(0o755)
    return dst


def kill_by_home(home: Path) -> None:
    """The CLI's detached workers from this test: our processes whose HOME is the test's."""
    me = os.getuid()
    needle = f"HOME={home}".encode()
    for d in glob.glob("/proc/[0-9]*"):
        try:
            if os.stat(d).st_uid != me:
                continue
            with open(f"{d}/environ", "rb") as fh:
                if needle not in fh.read().split(b"\0"):
                    continue
            os.kill(int(d.rsplit("/", 1)[1]), signal.SIGTERM)
        except (OSError, ValueError):
            continue


if __name__ == "__main__":
    unittest.main()
