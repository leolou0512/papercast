"""A hub for the web tests (test_web.py, test_page.py): a temporary $PCG_DATA, the real server on
a free port, and a stand-in for the auth module (A2's), which in this worktree refuses everything.

The stand-in trusts `X-Test-User: <email>` from 127.0.0.1 (as SPEC section 2's `header` mode),
enforces viewer < contributor < admin and disabled users, and wants `X-PCG: 1` on every change.
It also answers the auth module's own browser routes the page uses (Devices: /api/tokens; Users:
/api/admin/users; invites), with the shapes the page reads. Data goes in with direct SQL: fake
titles and people, no real papers."""
from __future__ import annotations

import hashlib
import json
import secrets
import sys
import tempfile
import threading
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError as URLHTTPError

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))           # stacks/papercast-group: `import hub`

from hub import app, auth, db, events  # noqa: E402
from hub import config as C  # noqa: E402

STATIC = HERE.parent / "static"
RANK = {"viewer": 0, "contributor": 1, "admin": 2}


def silent_mp3(seconds: float) -> bytes:
    """MPEG-1 layer III, 96 kbit/s CBR, 44.1 kHz, mono, all-zero frames: plays as silence
    (Leo's tests/fake_runner.py)."""
    frame = bytes([0xFF, 0xFB, 0x70, 0xC4]) + bytes(313 - 4)
    return frame * max(1, int(seconds * 44100 / 1152))


# ---------------------------------------------------------------- the auth stand-in

def fake_authenticate(req, level):
    from hub.app import HTTPError
    if level == "public":
        return None
    if req.method not in ("GET", "HEAD"):
        req.body()          # read before any refusal: left unread it would start the next request
    if req.client_ip != "127.0.0.1":
        raise HTTPError(401, "login_required", "log in first")
    email = (req.headers.get("X-Test-User") or "").strip().lower()
    u = db.conn().execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone() if email else None
    if not u:
        raise HTTPError(401, "login_required", "log in first")
    if u["disabled"]:
        raise HTTPError(403, "disabled", "this account is disabled")
    if level not in RANK:
        raise HTTPError(403, "forbidden", "not for the browser")
    if RANK[u["role"]] < RANK[level]:
        raise HTTPError(403, "forbidden", f"needs {level}")
    if req.method not in ("GET", "HEAD") and req.headers.get("X-PCG") != "1":
        raise HTTPError(403, "csrf", "missing X-PCG")
    return dict(u)


def _tokens(req):
    rows = db.conn().execute("SELECT id, name, created_at, last_used_at, revoked_at FROM tokens WHERE user_id = ? ORDER BY id",
                             (req.user["id"],)).fetchall()
    req.send_json(200, {"tokens": [dict(r) for r in rows]})


def _revoke(req, tid):
    from hub.app import HTTPError
    n = db.conn().execute("UPDATE tokens SET revoked_at = ? WHERE id = ? AND user_id = ? AND revoked_at IS NULL",
                          (db.now(), int(tid), req.user["id"])).rowcount
    if not n:
        raise HTTPError(404, "not_found", "no such device")
    req.send_json(200, {"ok": True})


def _users(req):
    rows = db.conn().execute("SELECT id, email, name, role, disabled, created_at FROM users ORDER BY id").fetchall()
    req.send_json(200, {"users": [dict(r, disabled=bool(r["disabled"])) for r in rows]})


def _put_user(req, uid):
    from hub.app import HTTPError
    b = req.json()
    if "role" in b:
        if b["role"] not in RANK:
            raise HTTPError(400, "bad_role", "no such role")
        db.conn().execute("UPDATE users SET role = ? WHERE id = ?", (b["role"], int(uid)))
    if "disabled" in b:
        db.conn().execute("UPDATE users SET disabled = ? WHERE id = ?", (1 if b["disabled"] else 0, int(uid)))
    r = db.conn().execute("SELECT id, email, name, role, disabled, created_at FROM users WHERE id = ?", (int(uid),)).fetchone()
    req.send_json(200, dict(r, disabled=bool(r["disabled"])))


def _invite(req):
    req.json()
    tok = secrets.token_urlsafe(16)
    req.send_json(201, {"url": f"{req.cfg.public_url}/join/{tok}", "expires_at": db.now()})


FAKE_AUTH_ROUTES = [
    ("GET", r"^/api/tokens$", _tokens, "viewer"),
    ("DELETE", r"^/api/tokens/(\d+)$", _revoke, "viewer"),
    ("GET", r"^/api/admin/users$", _users, "admin"),
    ("PUT", r"^/api/admin/users/(\d+)$", _put_user, "admin"),
    ("POST", r"^/api/admin/invites$", _invite, "admin"),
]


# ---------------------------------------------------------------- the rig

class Rig:
    def __init__(self, static: Path | None = None, auth_mode: str = "header"):
        self.tmp = tempfile.TemporaryDirectory(prefix="pcg-web-")
        self.data = Path(self.tmp.name)
        self._auth = (auth.authenticate, auth.ROUTES)
        auth.authenticate = fake_authenticate
        auth.ROUTES = FAKE_AUTH_ROUTES
        self.cfg = C.Config(data=self.data, auth=auth_mode, port=0, static=Path(static or STATIC))
        self.srv = app.serve(self.cfg)
        self.port = self.srv.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"
        self.thread = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.thread.start()
        self.t0 = datetime(2026, 9, 1, 8, 0, 0, tzinfo=timezone.utc)
        self.n = 0

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()
        auth.authenticate, auth.ROUTES = self._auth
        self.tmp.cleanup()

    # -- data, straight into the schema
    def stamp(self) -> str:
        """Increasing timestamps: the order things were made in is the order they were added."""
        self.n += 1
        return (self.t0 + timedelta(minutes=self.n)).strftime("%Y-%m-%dT%H:%M:%SZ")

    def user(self, email, name, role="viewer", disabled=False) -> int:
        c = db.conn()
        c.execute("INSERT INTO users(email, name, role, disabled, created_at) VALUES (?, ?, ?, ?, ?)",
                  (email, name, role, 1 if disabled else 0, self.stamp()))
        return c.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()[0]

    def paper(self, title, by, authors=("Ada Lovelace", "Alan Turing"), year=2023, tags=(), arxiv_id=None, url=None) -> str:
        pid = db.new_id("p_")
        db.conn().execute("""INSERT INTO papers(id, title, title_norm, authors, year, arxiv_id, url, tags, created_by, created_at)
                             VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                          (pid, title, db.norm_title(title), db.dumps(list(authors)), year, arxiv_id, url,
                           db.dumps(list(tags)), by, self.stamp()))
        return pid

    def episode(self, pid, by, state="ready", summary="", duration=1800.0, audio_s: float | None = 3.0,
                explainer: str | None = "<!doctype html><title>x</title><p>An explainer.</p>", deleted_at=None,
                progress=None) -> str:
        eid = db.new_id("e_")
        at = self.stamp()
        db.conn().execute("""INSERT INTO episodes(id, paper_id, made_by, state, prefs_summary, duration_s, base_version, model,
                                                  created_at, updated_at, deleted_at)
                             VALUES (?, ?, ?, ?, ?, ?, 1, 'claude-opus-5-5', ?, ?, ?)""",
                          (eid, pid, by, state, summary, duration if state == "ready" else None, at, at, deleted_at))
        d = self.cfg.episodes / eid
        d.mkdir(parents=True, exist_ok=True)
        if audio_s is not None and state == "ready":
            (d / "audio.mp3").write_bytes(silent_mp3(audio_s))
        if explainer is not None:
            (d / "explainer.html").write_text(explainer)
        if progress is not None:
            db.conn().execute("INSERT INTO voice_jobs(episode_id, user_id, state, queued_at, phase, progress) VALUES (?, ?, 'claimed', ?, 'speaking', ?)",
                              (eid, by, at, progress))
        return eid

    def token(self, uid, name) -> int:
        c = db.conn()
        c.execute("INSERT INTO tokens(user_id, name, hash, created_at, last_used_at) VALUES (?, ?, ?, ?, ?)",
                  (uid, name, hashlib.sha256(secrets.token_bytes(16)).hexdigest(), self.stamp(), self.stamp()))
        return c.execute("SELECT max(id) FROM tokens").fetchone()[0]

    def q(self, sql, *args):
        return db.conn().execute(sql, args).fetchall()

    # -- HTTP
    def req(self, method, path, body=None, user="alice@example.org", headers=None, raw=False):
        """(status, parsed JSON or bytes, headers). Mutations carry X-PCG unless headers say otherwise."""
        h = {"X-Test-User": user} if user else {}
        if method not in ("GET", "HEAD"):
            h["X-PCG"] = "1"
        data = None
        if body is not None:
            data = json.dumps(body).encode() if not isinstance(body, bytes) else body
            h["Content-Type"] = "application/json"
        h.update(headers or {})
        h = {k: v for k, v in h.items() if v is not None}
        r = urllib.request.Request(self.base + path, data=data, method=method, headers=h)
        try:
            with urllib.request.urlopen(r, timeout=10) as resp:
                got, code, hdr = resp.read(), resp.status, resp.headers
        except URLHTTPError as e:
            got, code, hdr = e.read(), e.code, e.headers
        if raw:
            return code, got, hdr
        try:
            return code, json.loads(got or b"null"), hdr
        except ValueError:
            return code, got, hdr

    def wait(self, fn, timeout=5.0, what="condition"):
        end = time.time() + timeout
        while time.time() < end:
            v = fn()
            if v:
                return v
            time.sleep(0.05)
        raise AssertionError(f"timed out: {what}")


def publish(kind, data, users=None):
    return events.publish(kind, data, users)
