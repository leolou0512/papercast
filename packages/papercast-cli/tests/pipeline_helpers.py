"""Test helpers for the pipeline (A8): a fake hub API, a Semantic Scholar fixture fetcher, a
small real PDF, and a temp job with the fake `claude` first on PATH. No network, no real
Claude."""
from __future__ import annotations

import io
import json
import os
import shutil
import sys
import tarfile
import tempfile
import time
import unittest
import urllib.parse
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.dirname(HERE)
if PKG not in sys.path:
    sys.path.insert(0, PKG)

from papercast_cli.common import bundle as cbundle          # noqa: E402
from papercast_cli.pipeline import claude as pclaude         # noqa: E402

FAKE_DIR = os.path.join(HERE, "pipeline_fake")
FAKE = os.path.join(FAKE_DIR, "claude")
FIXTURES = os.path.join(HERE, "pipeline_fixtures")

GUIDELINE = """# Base guideline

## The listener's rules
- One narrator, about the paper, never to the listener.
- One episode per paper, 15 to 25 minutes.
"""

LIBRARY = [
    {"id": "p_ddpm00000001", "title": "Denoising Diffusion Probabilistic Models", "year": 2020,
     "arxiv_id": "2006.11239", "doi": None, "s2_id": None},
    {"id": "p_sde000000001", "title": "Score-Based Generative Modeling through Stochastic Differential Equations",
     "year": 2020, "arxiv_id": "2011.13456", "doi": None, "s2_id": None},
    {"id": "p_adam00000001", "title": "Adam: A Method for Stochastic Optimization", "year": 2014,
     "arxiv_id": "1412.6980", "doi": None, "s2_id": None},
    {"id": "p_sd3000000001", "title": "Scaling Rectified Flow Transformers for High-Resolution Image Synthesis",
     "year": 2024, "arxiv_id": "2403.03206", "doi": None, "s2_id": None},
    {"id": "p_ppo000000001", "title": "Proximal Policy Optimization Algorithms", "year": 2017,
     "arxiv_id": None, "doi": None, "s2_id": None},
]
GRADES = {"Denoising Diffusion Probabilistic Models": "weak",
          "Score-Based Generative Modeling through Stochastic Differential Equations": "strong",
          "Adam: A Method for Stochastic Optimization": "none",
          "Scaling Rectified Flow Transformers for High-Resolution Image Synthesis": "essential",
          "Proximal Policy Optimization Algorithms": "essential"}


def iso(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def default_wording(name: str = "Alice") -> dict:
    """The package's wording.json with the listener-name class set to `name`, as the hub
    serves it with the base prompt."""
    from papercast_cli.common import wording
    with open(wording.PATH, encoding="utf-8") as fh:
        w = json.load(fh)
    for c in w["classes"]:
        if c["id"] == "listener_name":
            c["phrases"] = [name]
    return w


# --- the hub ---------------------------------------------------------------------------

class HTTPError(Exception):
    def __init__(self, status: int, body: dict):
        super().__init__(f"HTTP {status}")
        self.status, self.body = status, body


class FakeApi:
    """The CLI API of SPEC.md section 4, in memory."""

    def __init__(self, paper=None, conflict=False, library=None, wording=None, guideline=GUIDELINE,
                 settings=None, note="", final_state="waiting-for-gpu", down=False,
                 claim_expires_in=6 * 3600, mentions=()):
        """`mentions`: what GET /api/cli/mentions answers ([{"paper_id", "field"}]); None:
        a hub from before it (404)."""
        self.paper, self.conflict, self.down = paper, conflict, down
        self.mentions = None if mentions is None else list(mentions)
        self.library = list(LIBRARY if library is None else library)
        self.wording = default_wording() if wording is None else wording
        self.guideline, self.final_state = guideline, final_state
        self.settings = {"maths": "full", "emphasis": "method"} if settings is None else settings
        self.note = note
        self.prompt_version = 2
        self.claim_expires_in = claim_expires_in
        self.calls: list[tuple] = []
        self.uploads: list[dict] = []
        self.polls = 0

    def _net(self):
        if self.down:
            raise ConnectionRefusedError("hub down")

    def get(self, path: str) -> dict:
        self.calls.append(("GET", path))
        self._net()
        u = urllib.parse.urlsplit(path)
        if u.path == "/api/cli/lookup":
            return {"paper": self.paper, "claim": None}
        if u.path == "/api/cli/prompt":
            return {"version": self.prompt_version, "guideline": self.guideline, "wording": self.wording}
        if u.path == "/api/cli/prefs":
            return {"settings": self.settings, "note": self.note, "version": 3}
        if u.path == "/api/cli/library":
            return {"papers": self.library}
        if u.path == "/api/cli/mentions" and self.mentions is not None:
            return {"mentions": self.mentions, "info": {"title": "searched"}}
        if u.path.startswith("/api/cli/episodes/"):
            self.polls += 1
            eid = u.path.rsplit("/", 1)[1]
            state = "checking" if self.polls < 2 else self.final_state
            ep = {"id": eid, "state": state, "paper_id": self.uploads[-1]["paper_id"]}
            if state == "rejected":
                ep["check_report"] = ["script.md: 12 minutes; the range is 15-25"]
            return ep
        if u.path == "/api/cli/episodes":
            return {"episodes": [dict(id=x["episode_id"], paper_id=x["paper_id"], state="checking",
                                      title=x["manifest"]["paper"]["title"], created_at=x["at"])
                                 for x in self.uploads]}
        raise HTTPError(404, {"error": "not_found"})

    def post(self, path: str, body: dict) -> dict:
        self.calls.append(("POST", path, body))
        self._net()
        if path == "/api/cli/claims":
            if self.conflict:
                raise HTTPError(409, {"error": "in_progress", "by": {"name": "Bob"},
                                      "since": "2026-09-28T04:00:00Z"})
            return {"claim_id": "c_test00000001", "expires_at": iso(time.time() + self.claim_expires_in)}
        raise HTTPError(404, {"error": "not_found"})

    def upload(self, path: str, data: bytes, ctype: str) -> dict:
        self.calls.append(("UPLOAD", path, ctype, len(data)))
        self._net()
        assert path == "/api/cli/episodes" and ctype == "application/gzip"
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
            names = set(tf.getnames())
            manifest = json.load(tf.extractfile("manifest.json"))
            files = {n: tf.extractfile(n).read() for n in names}
        problems = cbundle.validate(manifest, names)
        if problems:
            raise HTTPError(400, {"error": "bad_manifest", "problems": problems})
        pid = manifest.get("paper_id") or "p_new000000001"
        rec = {"episode_id": f"e_test{len(self.uploads) + 1:08d}", "paper_id": pid,
               "manifest": manifest, "files": files, "size": len(data), "at": iso(time.time())}
        self.uploads.append(rec)
        return {"episode_id": rec["episode_id"], "paper_id": pid, "state": "checking"}


# --- Semantic Scholar ------------------------------------------------------------------------

class S2Fixture:
    """fetch(url) -> (status, json) from a fixture file; 404 for anything it does not hold."""

    def __init__(self, name: str = "s2_flow_matching.json", status: int | None = None):
        with open(os.path.join(FIXTURES, name), encoding="utf-8") as fh:
            self.answers = json.load(fh)
        self.status = status
        self.urls: list[str] = []

    def __call__(self, url: str):
        self.urls.append(url)
        if self.status is not None:
            return self.status, None
        u = urllib.parse.urlsplit(url)
        assert u.hostname == "api.semanticscholar.org", url
        path = urllib.parse.unquote(u.path).replace("/graph/v1", "", 1)
        q = urllib.parse.parse_qs(u.query)
        off = (q.get("offset") or ["0"])[0]
        key = path + (f"@{off}" if off != "0" else "")
        if key in self.answers:
            return 200, self.answers[key]
        return 404, None


# --- a PDF -----------------------------------------------------------------------------------

def _esc(s: str) -> str:
    return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def make_pdf(path: str, pages: list[list[str]]) -> None:
    """A small valid PDF: one Helvetica text block per page, US letter."""
    objs: list[bytes | None] = [None, None, b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    kids = []
    for lines in pages:
        content = ("BT /F1 12 Tf 72 720 Td 14 TL " +
                   " ".join(f"({_esc(l)}) Tj T*" for l in lines) + " ET").encode("latin-1")
        objs.append(b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream")
        c = len(objs)
        objs.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources "
                    f"<< /Font << /F1 3 0 R >> >> /Contents {c} 0 R >>".encode())
        kids.append(len(objs))
    objs[0] = b"<< /Type /Catalog /Pages 2 0 R >>"
    objs[1] = (f"<< /Type /Pages /Kids [{' '.join(f'{k} 0 R' for k in kids)}] "
               f"/Count {len(kids)} >>").encode()
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, o in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + o + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += (f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n").encode()
    with open(path, "wb") as fh:
        fh.write(bytes(out))


PAGE1 = ["arXiv:2210.02747v2 [cs.LG] 8 Feb 2023", "Flow Matching for Generative Modeling",
         "Yaron Lipman, Ricky T. Q. Chen", "Abstract. We introduce a new paradigm for generative modeling."]
PAGE2 = ["Related work builds on Denoising Diffusion Probabilistic Models and on",
         "Score-Based Generative Modeling through Stochastic Differential Equations."]


# --- a job -----------------------------------------------------------------------------------

class PipelineCase(unittest.TestCase):
    """A temp dir with `job/` and `fake-claude/` (the fake's state and scenario); the fake
    first on PATH and named by PAPERCAST_CLAUDE, so no real `claude` can ever run."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="pc-pipeline-")
        self.new_job("a")
        self._env = {k: os.environ.get(k) for k in ("PATH", "PAPERCAST_CLAUDE", "FAKE_CLAUDE_DIR",
                                                   "ANTHROPIC_API_KEY", "CLAUDECODE")}
        os.environ["PATH"] = FAKE_DIR + os.pathsep + os.environ.get("PATH", "")
        os.environ["PAPERCAST_CLAUDE"] = FAKE
        os.environ.pop("FAKE_CLAUDE_DIR", None)
        # these must never reach Claude Code (claude.env)
        os.environ["ANTHROPIC_API_KEY"] = "sk-test-never-passed"
        os.environ["CLAUDECODE"] = "1"
        assert pclaude.resolve()[0] == FAKE
        from papercast_cli.pipeline import pdf, run
        self._saved = (set(pdf.DISABLED), list(run.TRANSIENT_BACKOFF_S), run.HUB_POLL_S)
        run.TRANSIENT_BACKOFF_S[:] = [0.0, 0.0, 0.0]
        run.HUB_POLL_S = 0.01
        self.pdf_path = os.path.join(self.tmp, "flow-matching.pdf")
        make_pdf(self.pdf_path, [PAGE1, PAGE2])
        self.events: list[tuple] = []

    def tearDown(self):
        from papercast_cli.pipeline import pdf, run
        pdf.DISABLED.clear()
        pdf.DISABLED.update(self._saved[0])
        run.TRANSIENT_BACKOFF_S[:] = self._saved[1]
        run.HUB_POLL_S = self._saved[2]
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---- setup
    def new_job(self, name: str) -> None:
        """A fresh job dir and fake-claude state (the fake finds its state next to the job)."""
        base = os.path.join(self.tmp, name)
        self.job = os.path.join(base, "job")
        self.fake = os.path.join(base, "fake-claude")
        os.makedirs(self.job)
        os.makedirs(self.fake)

    def scenario(self, **s) -> None:
        s.setdefault("grades", GRADES)
        with open(os.path.join(self.fake, "scenario.json"), "w") as fh:
            json.dump(s, fh)

    def write_job(self, **j) -> None:
        j.setdefault("input", self.pdf_path)
        j.setdefault("device", "test-laptop")
        with open(os.path.join(self.job, "job.json"), "w") as fh:
            json.dump(j, fh)

    def progress(self, phase, fraction, detail):
        self.events.append((phase, fraction, detail))

    def run_job(self, api, **kw):
        from papercast_cli.pipeline.run import run_job
        kw.setdefault("fetch", S2Fixture())
        kw.setdefault("download", self._no_download)
        return run_job(self.job, api, kw.pop("progress", self.progress), **kw)

    @staticmethod
    def _no_download(url, limit):
        raise AssertionError(f"unexpected download of {url}")

    # ---- what happened
    def calls(self) -> list[dict]:
        try:
            with open(os.path.join(self.fake, "calls.jsonl")) as fh:
                return [json.loads(l) for l in fh if l.strip()]
        except OSError:
            return []

    def kinds(self) -> list[str]:
        return [c["kind"] for c in self.calls()]

    def jpath(self, *p) -> str:
        return os.path.join(self.job, *p)

    def jread(self, *p):
        with open(self.jpath(*p), encoding="utf-8") as fh:
            return json.load(fh) if p[-1].endswith(".json") else fh.read()
