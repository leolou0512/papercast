"""A running hub for A5's tests (test_graph.py, test_layout.py): app.serve on a free port with a
temporary PCG_DATA, and hub.auth.authenticate replaced by a fake that trusts X-Test-User and
knows the viewer, contributor and admin roles (A2's real one refuses everything in this worktree)."""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import threading
import urllib.error
import urllib.request
import warnings
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]          # stacks/papercast-group
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from hub import app, auth, db, graph, layout  # noqa: E402
from hub import config as C  # noqa: E402
from hub.app import HTTPError  # noqa: E402

RANK = {"viewer": 0, "contributor": 1, "admin": 2}


def fake_authenticate(req, level):
    if level == "public":
        return None
    email = (req.headers.get("X-Test-User") or "").strip().lower()
    row = db.conn().execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone() if email else None
    if row is None:
        raise HTTPError(401, "login_required", "log in first")
    if row["disabled"]:
        raise HTTPError(403, "disabled", "this account is disabled")
    need = {"viewer": "viewer", "contributor": "contributor", "admin": "admin", "cli": "viewer",
            "cli-contributor": "contributor"}.get(level)
    if need is None or RANK[row["role"]] < RANK[need]:
        raise HTTPError(403, "forbidden", "not allowed")
    return dict(row)


class Hub:
    """One hub per test: fresh database, the fake auth, three users (alice and bob viewers,
    root the admin)."""

    def __init__(self, auto_layout=False):
        # db.conn() keeps one connection per request thread and never closes it (A1's design);
        # Python 3.13 warns about each one
        warnings.simplefilter("ignore", ResourceWarning)
        self.tmp = Path(tempfile.mkdtemp(prefix="pcg-a5-"))
        self._auth = auth.authenticate
        self._auto = layout.AUTO
        auth.authenticate = fake_authenticate
        layout.AUTO = auto_layout
        self.srv = app.serve(C.Config(data=self.tmp, port=0, auth="header"))
        self.thread = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"
        graph.ensure_schema()
        self.alice = self.user("alice@example.org", "Alice")
        self.bob = self.user("bob@example.org", "Bob")
        self.root = self.user("root@example.org", "Root", "admin")

    def close(self):
        layout.wait_idle(30)
        self.srv.shutdown()
        self.srv.server_close()
        auth.authenticate = self._auth
        layout.AUTO = self._auto
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- data straight into the database (other parts' helpers are stubs in this worktree)
    def user(self, email, name, role="viewer") -> int:
        c = db.conn()
        return c.execute("INSERT INTO users(email, name, role, created_at) VALUES (?, ?, ?, ?)",
                         (email, name, role, db.now())).lastrowid

    def paper(self, title, year=2020, tags=(), arxiv_id=None, doi=None, by=None) -> str:
        pid = db.new_id("p_")
        db.conn().execute(
            "INSERT INTO papers(id, title, title_norm, authors, year, arxiv_id, doi, tags, created_by, created_at) "
            "VALUES (?, ?, ?, '[]', ?, ?, ?, ?, ?, ?)",
            (pid, title, db.norm_title(title), year, arxiv_id, doi, json.dumps(list(tags)), by, db.now()))
        return pid

    def episode(self, paper_id, user_id, state="ready", deleted=False) -> str:
        eid = db.new_id("e_")
        at = db.now()
        db.conn().execute(
            "INSERT INTO episodes(id, paper_id, made_by, state, created_at, updated_at, deleted_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (eid, paper_id, user_id, state, at, at, at if deleted else None))
        return eid

    # -- HTTP
    def req(self, method, path, body=None, who="alice"):
        email = {"alice": "alice@example.org", "bob": "bob@example.org", "root": "root@example.org"}.get(who, who)
        data = None if body is None else json.dumps(body).encode()
        r = urllib.request.Request(self.base + path, data=data, method=method,
                                   headers={"X-Test-User": email, "X-PCG": "1", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(r, timeout=30) as resp:
                raw = resp.read()
                return resp.status, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as e:
            raw = e.read()
            return e.code, (json.loads(raw) if raw else None)

    def ok(self, method, path, body=None, who="alice", code=None):
        st, js = self.req(method, path, body, who)
        if (code is not None and st != code) or (code is None and st >= 300):
            raise AssertionError(f"{method} {path} -> {st} {js}")
        return js

    def undo(self, scope="mine", who="alice", redo=False):
        """What the page does: read the log's next undo, send it as `expect`."""
        log = self.ok("GET", "/api/graph-log", who=who)
        e = (log["redo"] if redo else log["undo"])[scope]
        if e is None:
            return None, None
        body = {"scope": scope, "expect": e["id"]}
        if redo:
            body["redo"] = True
        return self.req("POST", "/api/graph-log/revert", body, who)

    def link_row(self, src, dst):
        return db.conn().execute("SELECT * FROM links WHERE src = ? AND dst = ?", (src, dst)).fetchone()

    def log_count(self):
        return db.conn().execute("SELECT count(*) FROM graph_log").fetchone()[0]
