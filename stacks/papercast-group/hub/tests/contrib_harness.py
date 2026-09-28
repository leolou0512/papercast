"""A real hub for A3's tests (test_contrib.py, test_voiceq.py): app.serve on a free port with a temp
PCG_DATA and PCG_AUTH=header, and hub.auth.authenticate replaced by a small fake (A2's module is
a stub in this worktree) that maps a bearer token or X-Test-User to a user row the test inserted
and enforces the levels. Plus a bundle builder and a script that passes the checks."""
from __future__ import annotations

import hashlib
import http.client
import io
import json
import shutil
import sys
import tarfile
import tempfile
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]                       # stacks/papercast-group
REPO = HERE.parents[3]
for p in (str(ROOT), str(REPO / "packages" / "papercast-cli")):
    if p not in sys.path:
        sys.path.insert(0, p)

from hub import app, auth, config, db  # noqa: E402

WORKER_TOKEN = "worker-secret-for-tests"
ROLES_FOR = {"viewer": ("viewer", "contributor", "admin"), "contributor": ("contributor", "admin"),
             "admin": ("admin",), "cli": ("viewer", "contributor", "admin"),
             "cli-contributor": ("contributor", "admin")}

# Sentences that pass checks.check and the wording list; repeated to the length wanted.
SENTENCES = [
    "The method learns a score function that points toward regions of higher density.",
    "Noise is added in small steps until the data looks like a plain Gaussian cloud.",
    "A network is trained to undo each step, one small correction at a time.",
    "Sampling runs the learned corrections backwards, starting from pure noise.",
    "The key quantity is the gradient of the log density, estimated at every noise level.",
    "Training needs only pairs of clean and noisy examples, so it is simple and stable.",
    "The authors compare against adversarial training and find fewer failures of coverage.",
    "A careful choice of the noise schedule decides how many steps sampling needs.",
    "When the schedule is too coarse, fine detail is lost in the final images.",
    "The loss weights each noise level so that no single level dominates the gradient.",
]


def script(words: int = 2600, extra: str = "") -> str:
    out, n, i = ["# How the method works", ""], 0, 0
    para = []
    while n < words:
        s = SENTENCES[i % len(SENTENCES)]
        para.append(s)
        n += len(s.split())
        i += 1
        if len(para) == 6:
            out += [" ".join(para), ""]
            para = []
    if para:
        out += [" ".join(para), ""]
    if extra:
        out += [extra, ""]
    return "\n".join(out)


EXPLAINER_JSON = {"points": ["The score is the gradient of the log density."],
                  "figures": [{"caption": "The noise schedule.", "svg": "<svg xmlns='http://www.w3.org/2000/svg'/>"}]}
EXPLAINER_HTML = "<!doctype html><html><head><meta charset='utf-8'><title>x</title></head><body>ok</body></html>"


def manifest(**over) -> dict:
    m = {"manifest_version": 1, "client_version": "0.1.0", "base_version": 1,
         "prefs": {"settings": {"maths": "full", "emphasis": "practice"}, "note": "", "version": 2},
         "model": "claude-opus-5-5", "claim_id": None, "paper_id": None,
         "paper": {"title": "Score-Based Generative Modeling", "authors": ["Yang Song", "Stefano Ermon"],
                   "year": 2020, "arxiv_id": "2011.13456", "doi": None, "url": "https://arxiv.org/abs/2011.13456",
                   "source_sha256": None, "tags": ["diffusion", "generative models"]},
         "files": {"script": "script.md", "explainer_json": "explainer.json",
                   "explainer_html": "explainer.html", "claims": "claims.md"},
         "links": [{"other": {"arxiv_id": "1907.05600"}, "direction": "builds_on", "grade": "e", "source": "s2"}],
         "stats": {"words": 2600, "est_minutes": 17.3, "wall_s": 1400}}
    for k, v in over.items():
        if k == "paper_over":
            m["paper"].update(v)
        else:
            m[k] = v
    return m


def tar_gz(members: list) -> bytes:
    """members: (TarInfo-ish dict or name, bytes|None). A dict gives the TarInfo fields."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz", format=tarfile.PAX_FORMAT) as tf:
        for spec, data in members:
            spec = {"name": spec} if isinstance(spec, str) else dict(spec)
            ti = tarfile.TarInfo(spec.pop("name"))
            for k, v in spec.items():
                setattr(ti, k, v)
            if data is not None and ti.type in (tarfile.REGTYPE, tarfile.AREGTYPE):
                ti.size = len(data)
                tf.addfile(ti, io.BytesIO(data))
            else:
                tf.addfile(ti)
    return buf.getvalue()


def bundle(man: dict | None = None, script_text: str | None = None, extra: list | None = None,
           explainer_html: str = EXPLAINER_HTML, explainer_json=None) -> bytes:
    man = man or manifest()
    files = [("manifest.json", json.dumps(man).encode()),
             ("script.md", (script_text if script_text is not None else script()).encode()),
             ("explainer.json", json.dumps(explainer_json if explainer_json is not None else EXPLAINER_JSON).encode()),
             ("explainer.html", explainer_html.encode()),
             ("claims.md", b"# Claims\n\nEvery claim is from the paper.\n")]
    return tar_gz(files + (extra or []))


class Hub:
    """One running hub. Users get a bearer token (for cli levels) and an email (browser levels)."""

    def __init__(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="pcg-a3-"))
        self.cfg = config.load({"PCG_DATA": str(self.tmp / "data"), "PCG_AUTH": "header",
                                "PCG_PORT": "0", "PCG_BIND": "127.0.0.1"})
        self.tokens: dict = {}
        self._orig_auth = auth.authenticate
        auth.authenticate = self._authenticate
        from hub import layout              # no background layouts here: they outlive this hub
        layout.AUTO = False                 # and would write into the next test's database
        self.srv = app.serve(self.cfg)
        self.port = self.srv.server_address[1]
        self.thread = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()
        auth.authenticate = self._orig_auth
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- the fake auth.authenticate
    def _authenticate(self, req, level):
        HTTPError = app.HTTPError
        if level == "public":
            return None
        bearer = req.headers.get("Authorization", "")
        bearer = bearer[7:] if bearer.startswith("Bearer ") else ""
        if level == "worker":
            if bearer != WORKER_TOKEN:
                raise HTTPError(401, "worker_only", "the voice worker's token is needed")
            return {"id": None, "name": "voice worker", "role": "worker"}
        if level.startswith("cli"):
            uid = self.tokens.get(bearer)
        else:
            if req.client_ip != "127.0.0.1":
                raise HTTPError(401, "login_required", "log in first")
            r = db.conn().execute("SELECT id FROM users WHERE email = ?",
                                  ((req.headers.get("X-Test-User") or "").lower(),)).fetchone()
            uid = r["id"] if r else None
        if uid is None:
            raise HTTPError(401, "login_required", "log in first")
        u = dict(db.conn().execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone())
        if u["disabled"]:
            raise HTTPError(403, "disabled", "this account is disabled")
        if u["role"] not in ROLES_FOR[level]:
            raise HTTPError(403, "forbidden", f"needs {level}")
        return u

    # -- test data
    def user(self, name: str, role: str = "contributor") -> dict:
        self._n = getattr(self, "_n", 0) + 1
        email = f"{name.lower()}{self._n}@example.org"
        with db.transaction() as c:
            c.execute("INSERT INTO users (email, name, role, created_at) VALUES (?, ?, ?, ?)",
                      (email, name, role, db.now()))
            uid = c.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()["id"]
        tok = "pcg_" + hashlib.sha256(email.encode()).hexdigest()[:32]
        self.tokens[tok] = uid
        return {"id": uid, "email": email, "name": name, "token": tok}

    def base_prompt(self, guideline: str = "One episode per paper, 15 to 25 minutes.", wording=None, version=None):
        with db.transaction() as c:
            v = version or (c.execute("SELECT COALESCE(MAX(version), 0) FROM base_prompts").fetchone()[0] + 1)
            c.execute("INSERT INTO base_prompts (version, guideline, wording, created_at) VALUES (?, ?, ?, ?)",
                      (v, guideline, json.dumps(wording or {"classes": []}), db.now()))
        return v

    # -- HTTP
    def request(self, method: str, path: str, body=None, user=None, headers=None, worker=False,
                ctype=None, length=None):
        """(status, parsed JSON or bytes). `length` overrides Content-Length (the body is then
        not sent: for the size limits)."""
        h = dict(headers or {})
        if user is not None:
            h["Authorization"] = f"Bearer {user['token']}"
        if worker:
            h["Authorization"] = f"Bearer {WORKER_TOKEN}"
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
            h.setdefault("Content-Type", "application/json")
        if ctype:
            h["Content-Type"] = ctype
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=60)
        try:
            if length is not None:
                conn.putrequest(method, path)
                for k, v in h.items():
                    conn.putheader(k, v)
                conn.putheader("Content-Length", str(length))
                conn.endheaders()
            else:
                conn.request(method, path, body=body, headers=h)
            r = conn.getresponse()
            data = r.read()
        finally:
            conn.close()
        if r.getheader("Content-Type", "").startswith("application/json") and data:
            return r.status, json.loads(data)
        return r.status, data

    def upload(self, user, data: bytes, ctype="application/gzip"):
        return self.request("POST", "/api/cli/episodes", body=data, user=user, ctype=ctype)

    def wait_checked(self, user, eid: str, timeout: float = 20) -> dict:
        end = time.time() + timeout
        while time.time() < end:
            code, ep = self.request("GET", f"/api/cli/episodes/{eid}", user=user)
            assert code == 200, (code, ep)
            if ep["state"] != "checking":
                return ep
            time.sleep(0.05)
        raise AssertionError(f"{eid} still checking after {timeout} s")

    def claim(self, user, **keys):
        return self.request("POST", "/api/cli/claims", {"keys": keys, "device": "laptop"}, user=user)

    def make_paper(self, user, arxiv_id: str, title: str, **paper_over) -> dict:
        """Claim, upload, wait for the checks: the passing episode."""
        code, cl = self.claim(user, arxiv_id=arxiv_id, title=title)
        assert code == 201, (code, cl)
        m = manifest(claim_id=cl["claim_id"], paper_over={"arxiv_id": arxiv_id, "title": title, **paper_over})
        code, out = self.upload(user, bundle(m))
        assert code == 201, (code, out)
        ep = self.wait_checked(user, out["episode_id"])
        assert ep["state"] == "waiting-for-gpu", ep
        return ep
