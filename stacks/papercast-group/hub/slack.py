"""A new episode announced in the group's Slack channel (Leo, 2026-09-28: "when people contribute
a paper via cli, it prompts them 'Would you like to post this to slack channel too? [Y/n]' ...
posting to our slack channel t-machinelearning channel by a slack bot if they put Y").

The job runs in the background and the episode is ready much later, so `papercast add` asks at
once and the answer rides in the bundle's manifest as "announce": {"slack": true}. The hub posts,
so the webhook stays on the server and the link opens the hub's page. When such an episode turns
ready (its audio landed: the `episode` event voiceq publishes), one message goes to the channel
through a Slack Incoming Webhook:

    *<https://<hub>/#p=p_...|Title>* — added by Alice · 21 min (new version: derivations · practical)

The part in brackets only when the paper had an episode before this one.

Settings (hub.env):
  PCG_SLACK_WEBHOOK_FILE   a 600 file holding the webhook address. The address itself is never in
                           the environment, the log or any answer: it is the one secret here.
  PCG_SLACK_CHANNEL        the channel's name as people know it (default #t-machinelearning). The
                           webhook decides where a message goes; this is only what the CLI shows.

At most once per episode: slack_posts has one row per announced episode (pending, sending,
retrying, posted, failed, skipped). A post Slack refuses for now (429, 5xx, no answer) is tried
again with backoff for up to an hour after the episode turned ready. A row a stopped hub left in
`sending` may or may not have reached Slack, so it is never sent again: it becomes failed and says
so. A failure goes to the hub's log and the admins' Slack tab, never to the uploader. Each
person's default answer (slack_prefs; on until they turn it off) is what `add` takes on Enter, or
when it runs without a terminal.

  GET  /api/cli/features      {"slack": {"enabled", "channel", "default"}}: does `add` ask
  PUT  /api/cli/slack         {"default": bool}: papercast prefs --slack on|off
  GET  /api/slack, PUT        the same for the page (Settings -> Preferences)
  GET  /api/admin/slack       configured or not (and why), the channel, the last 20 posts
  POST /api/admin/slack/test  a test message, sent now: {"ok", "detail"}
"""
from __future__ import annotations

import http.client
import json
import logging
import os
import re
import stat
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from urllib.parse import urlsplit

from . import db, events
from .app import HTTPError

log = logging.getLogger("pcg.slack")

CHANNEL = "#t-machinelearning"
RETRY_FOR_S = 3600.0            # after the episode turned ready, a post is tried for this long
RETRY_BASE_S = 10.0             # then 20, 40, ... s
RETRY_MAX_S = 600.0
TIMEOUT_S = 15.0
SWEEP_S = 30.0                  # the sender looks for due posts at least this often
LAST = 20                       # posts the admins' tab lists
LIVE = ("pending", "retrying")

SCHEMA = """
CREATE TABLE IF NOT EXISTS slack_posts (      -- one row per announced episode: posted at most once
  episode_id TEXT PRIMARY KEY,
  user_id INTEGER,                            -- the uploader
  state TEXT NOT NULL,                        -- pending | sending | retrying | posted | failed | skipped
  detail TEXT,                                -- why, in words (never the webhook address)
  attempts INTEGER NOT NULL DEFAULT 0,
  first_t REAL NOT NULL,                      -- when it was queued (epoch s): retries stop an hour on
  next_t REAL,                                -- when the next try is due
  created_at TEXT NOT NULL,
  sent_at TEXT,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS slack_posts_due ON slack_posts(state, next_t);
CREATE TABLE IF NOT EXISTS slack_prefs (      -- each person's default answer to add's question
  user_id INTEGER PRIMARY KEY,
  post INTEGER NOT NULL,
  updated_at TEXT NOT NULL
)
"""


# ---------------------------------------------------------------- schema

_ready: set = set()
_ready_lock = threading.Lock()


def ensure_schema() -> None:
    """This module's two tables, once per database (db.py is not touched)."""
    key = str(db._path)
    if key in _ready:
        return
    with _ready_lock:
        if key in _ready:
            return
        c = db.conn()
        for stmt in SCHEMA.split(";"):      # one by one: executescript would commit a caller's transaction
            if stmt.strip():
                c.execute(stmt)
        if not c.in_transaction:
            _ready.add(key)


# ---------------------------------------------------------------- settings

def _setting(cfg, attr: str, env: str) -> str:
    """A Config attribute when the hub's config has it, else the environment (hub.env)."""
    v = getattr(cfg, attr, None) if cfg is not None else None
    if v is None:
        v = os.environ.get(env, "")
    return str(v or "").strip()


def channel(cfg) -> str:
    ch = _setting(cfg, "slack_channel", "PCG_SLACK_CHANNEL") or CHANNEL
    return ch if ch.startswith("#") else "#" + ch


def _address_ok(url: str) -> bool:
    """https, or plain http to this machine only (a stand-in for Slack in tests)."""
    try:
        u = urlsplit(url)
    except ValueError:
        return False
    if not u.hostname or any(ch.isspace() for ch in url) or not u.path.strip("/"):
        return False
    return u.scheme == "https" or (u.scheme == "http" and u.hostname in ("127.0.0.1", "localhost"))


def webhook(cfg) -> tuple:
    """(address, None), or (None, what is wrong in words). The words never hold the address."""
    p = _setting(cfg, "slack_webhook_file", "PCG_SLACK_WEBHOOK_FILE")
    if not p:
        return None, "PCG_SLACK_WEBHOOK_FILE is not set in hub.env"
    if "://" in p or p.lower().startswith(("http", "hooks.")):
        return None, ("PCG_SLACK_WEBHOOK_FILE must name a file (chmod 600) that holds the webhook "
                      "address, not the address itself")
    try:
        st = os.stat(p)
    except FileNotFoundError:
        return None, f"{p} does not exist"
    except OSError as e:
        return None, f"{p} cannot be read ({e.strerror})"
    if not stat.S_ISREG(st.st_mode):
        return None, f"{p} is not a file"
    if st.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        return None, f"{p} can be read by others: chmod 600 it"
    try:
        with open(p, encoding="utf-8") as f:
            url = f.read(4096).strip()
    except (OSError, UnicodeDecodeError):
        return None, f"{p} cannot be read"
    if not _address_ok(url):
        return None, f"{p} does not hold a webhook address (https://hooks.slack.com/services/...)"
    return url, None


def _file_set(cfg) -> bool:
    return bool(_setting(cfg, "slack_webhook_file", "PCG_SLACK_WEBHOOK_FILE"))


def enabled(cfg) -> bool:
    return webhook(cfg)[0] is not None


def scrub(text, url) -> str:
    """`text` with the webhook address (and its secret path) taken out, for the log and the admins."""
    s = str(text)
    if url:
        try:
            path = urlsplit(url).path
        except ValueError:
            path = ""
        for part in sorted({url, url.rstrip("/"), path, path.rstrip("/")}, key=len, reverse=True):
            if len(part) > 1:
                s = s.replace(part, "<webhook>")
    return s


# ---------------------------------------------------------------- the message

def esc(s) -> str:
    """Slack's mrkdwn: &, < and > are the only characters it wants escaped."""
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def message(public_url: str, paper_id: str, title: str, maker: str, duration_s, new_version: bool,
            summary: str = "") -> str:
    link = f"{(public_url or '').rstrip('/')}/#p={paper_id}"
    text = f"*<{link}|{esc(' '.join(str(title or 'Untitled').split()))}>* — added by {esc(maker or 'someone')}"
    if isinstance(duration_s, (int, float)) and duration_s > 0:
        text += f" · {max(1, round(duration_s / 60))} min"
    if new_version:
        text += f" (new version: {esc(summary)})" if summary else " (new version)"
    return text


def _info(c, eid):
    """What the message says about an episode: its paper, maker, length, and whether the paper had
    a live episode before it."""
    r = c.execute("SELECT e.id, e.rowid AS rid, e.paper_id, e.made_by, e.state, e.deleted_at, e.duration_s, "
                  "e.prefs_summary, e.created_at, p.title, u.name AS maker FROM episodes e "
                  "JOIN papers p ON p.id = e.paper_id JOIN users u ON u.id = e.made_by WHERE e.id = ?",
                  (eid,)).fetchone()
    if r is None:
        return None
    # made earlier: by the clock, and within one second by the order the rows went in
    before = c.execute("SELECT COUNT(*) FROM episodes WHERE paper_id = ? AND id <> ? AND deleted_at IS NULL "
                       "AND state <> 'rejected' AND (created_at < ? OR (created_at = ? AND rowid < ?))",
                       (r["paper_id"], eid, r["created_at"], r["created_at"], r["rid"])).fetchone()[0]
    return dict(r, new_version=before > 0)


# ---------------------------------------------------------------- posting

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_opener = urllib.request.build_opener(_NoRedirect())


def post(url: str, text: str, timeout: float = TIMEOUT_S) -> tuple:
    """One POST to the webhook: ("posted" | "retry" | "failed", detail in words, Retry-After s)."""
    body = json.dumps({"text": text, "unfurl_links": False, "unfurl_media": False}).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": "application/json; charset=utf-8", "User-Agent": "papercast-group"})
    wait = None
    try:
        with _opener.open(req, timeout=timeout) as r:
            code, raw = r.status, r.read(2000)
    except urllib.error.HTTPError as e:
        code = e.code
        try:
            raw = e.read(2000)
        except (OSError, http.client.HTTPException):
            raw = b""
        ra = (e.headers.get("Retry-After") or "").strip() if e.headers is not None else ""
        wait = float(ra) if re.fullmatch(r"\d{1,6}(\.\d+)?", ra) else None
    except (urllib.error.URLError, OSError, http.client.HTTPException) as e:
        why = getattr(e, "reason", None) or e
        return "retry", scrub(f"no answer from Slack ({type(why).__name__}: {why})", url)[:300], None
    except ValueError:                  # an address urllib cannot use (its message would hold it)
        return "failed", "the webhook address in the file is not usable", None
    said = scrub(raw.decode("utf-8", "replace").strip(), url)[:200]
    if 200 <= code < 300:
        return "posted", said or "ok", None
    detail = f"Slack answered {code}" + (f": {said}" if said else "")
    if code == 429 or code >= 500:
        return "retry", detail, wait
    return "failed", detail, None


def _backoff(attempts: int, wait) -> float:
    if wait is not None:
        return min(RETRY_MAX_S, max(0.0, wait))
    return min(RETRY_MAX_S, RETRY_BASE_S * (2 ** max(0, attempts - 1)))


def _set(c, eid, **f) -> None:
    f["updated_at"] = db.now()
    c.execute(f"UPDATE slack_posts SET {', '.join(f'{k} = ?' for k in f)} WHERE episode_id = ?", (*f.values(), eid))


def attempt(cfg, eid: str) -> str | None:
    """Send one due post (the sender thread). Returns the state it ended in."""
    with db.transaction() as t:     # claim it, so nothing else sends it meanwhile
        row = t.execute("SELECT * FROM slack_posts WHERE episode_id = ?", (eid,)).fetchone()
        if row is None or row["state"] not in LIVE:
            return None
        info = _info(t, eid)
        if info is None or info["deleted_at"] or info["state"] != "ready":
            _set(t, eid, state="skipped", next_t=None, detail="the episode was deleted before it was posted")
            return "skipped"
        if not _file_set(cfg):
            _set(t, eid, state="skipped", next_t=None, detail="Slack was not set up on the hub any more")
            return "skipped"
        url, problem = webhook(cfg)
        n = row["attempts"] + 1
        if url is None:             # a file an admin can fix: tried again until the hour is up
            return _again(t, cfg, row, n, problem, None)
        text = message(cfg.public_url, info["paper_id"], info["title"], info["maker"], info["duration_s"],
                       info["new_version"], info["prefs_summary"] or "")
        _set(t, eid, state="sending", attempts=n)
    outcome, detail, wait = post(url, text)
    with db.transaction() as t:
        row = t.execute("SELECT * FROM slack_posts WHERE episode_id = ?", (eid,)).fetchone()
        if outcome == "posted":
            _set(t, eid, state="posted", next_t=None, sent_at=db.now(), detail=None)
            log.info("slack: posted %s to %s", eid, channel(cfg))
            return "posted"
        if outcome == "failed":
            _set(t, eid, state="failed", next_t=None, detail=detail)
            log.warning("slack: posting %s failed: %s", eid, detail)
            return "failed"
        return _again(t, cfg, row, n, detail, wait)


def _again(t, cfg, row, n: int, detail: str, wait) -> str:
    eid = row["episode_id"]
    due = time.time() + _backoff(n, wait)
    if due > row["first_t"] + RETRY_FOR_S:
        _set(t, eid, state="failed", attempts=n, next_t=None,
             detail=f"gave up after {n} tries in {RETRY_FOR_S / 60:.0f} min: {detail}")
        log.warning("slack: gave up posting %s after %d tries: %s", eid, n, detail)
        return "failed"
    _set(t, eid, state="retrying", attempts=n, next_t=due, detail=detail)
    log.info("slack: posting %s: %s; trying again in %.0f s", eid, detail, due - time.time())
    return "retrying"


def send_due(cfg) -> float | None:
    """Every post that is due, oldest first. Returns seconds until the next one (None: nothing
    waits)."""
    ensure_schema()
    c = db.conn()
    tried: set = set()
    while True:
        r = c.execute(f"SELECT episode_id FROM slack_posts WHERE state IN {LIVE} AND next_t <= ? "
                      "ORDER BY next_t, created_at LIMIT 1", (time.time(),)).fetchone()
        if r is None or r["episode_id"] in tried:
            break
        tried.add(r["episode_id"])
        attempt(cfg, r["episode_id"])
    nxt = c.execute(f"SELECT MIN(next_t) FROM slack_posts WHERE state IN {LIVE}").fetchone()[0]
    return None if nxt is None else max(0.0, nxt - time.time())


# ---------------------------------------------------------------- an episode turned ready

def wants_slack(cfg, eid: str) -> bool:
    """Its uploader said yes: the bundle's manifest has "announce": {"slack": true}."""
    try:
        m = json.loads((cfg.episodes / eid / "bundle-manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    a = m.get("announce") if isinstance(m, dict) else None
    return isinstance(a, dict) and a.get("slack") is True


def episode_ready(cfg, eid: str) -> str | None:
    """Queue the episode's post if it asked for one and has none yet. Returns the new row's state
    (pending, or skipped when Slack is not set up), or None."""
    ensure_schema()
    c = db.conn()
    ep = c.execute("SELECT id, made_by, state, deleted_at FROM episodes WHERE id = ?", (eid,)).fetchone()
    if ep is None or ep["state"] != "ready" or ep["deleted_at"] or not wants_slack(cfg, eid):
        return None
    if c.execute("SELECT 1 FROM slack_posts WHERE episode_id = ?", (eid,)).fetchone():
        return None
    now, t = db.now(), time.time()
    if _file_set(cfg):
        state, detail = "pending", None
    else:                           # recorded, so setting Slack up later never posts old episodes
        state, detail = "skipped", "Slack is not set up on the hub (PCG_SLACK_WEBHOOK_FILE)"
    n = c.execute("INSERT OR IGNORE INTO slack_posts (episode_id, user_id, state, detail, attempts, first_t, "
                  "next_t, created_at, updated_at) VALUES (?, ?, ?, ?, 0, ?, ?, ?, ?)",
                  (eid, ep["made_by"], state, detail, t, t if state == "pending" else None, now, now)).rowcount
    if not n:
        return None
    if state == "pending":
        _wake.set()
    else:
        log.info("slack: %s asked to be announced, but Slack is not set up on the hub", eid)
    return state


# ---------------------------------------------------------------- the threads

_cfg: dict = {"cfg": None}
_wake = threading.Event()
_threads: dict = {}
_threads_lock = threading.Lock()


def _listen(sub) -> None:
    """voiceq publishes an `episode` event with state ready when the audio has landed."""
    while True:
        _, kind, data = sub.q.get()
        if kind != "episode" or not isinstance(data, dict) or data.get("state") != "ready" or not data.get("id"):
            continue
        cfg = _cfg["cfg"]
        try:
            if cfg is not None:
                episode_ready(cfg, str(data["id"]))
        except Exception:
            log.exception("slack: %s turned ready", data.get("id"))


def _send_loop() -> None:
    while True:
        _wake.clear()
        wait = SWEEP_S
        cfg = _cfg["cfg"]
        try:
            if cfg is not None:
                nxt = send_due(cfg)
                if nxt is not None:
                    wait = min(SWEEP_S, nxt)
        except Exception:
            log.exception("slack: sending")
        _wake.wait(timeout=max(0.05, wait))


def start(cfg) -> None:
    """At the hub's start (app.serve): a post a stopped hub left half-sent is not sent again;
    episodes that turned ready while nobody was listening (the hour before) get their post."""
    _cfg["cfg"] = cfg
    ensure_schema()
    c = db.conn()
    n = c.execute("UPDATE slack_posts SET state = 'failed', next_t = NULL, updated_at = ?, detail = "
                  "'the hub stopped while posting it; not sent again, so it is never posted twice' "
                  "WHERE state = 'sending'", (db.now(),)).rowcount
    if n:
        log.warning("slack: %d post(s) were being sent when the hub stopped; not sent again", n)
    since = datetime.fromtimestamp(time.time() - RETRY_FOR_S, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    for r in c.execute("SELECT e.id FROM episodes e LEFT JOIN slack_posts s ON s.episode_id = e.id "
                       "WHERE e.state = 'ready' AND e.deleted_at IS NULL AND e.updated_at >= ? "
                       "AND s.episode_id IS NULL", (since,)).fetchall():
        episode_ready(cfg, r["id"])
    with _threads_lock:
        if "listen" not in _threads:
            sub = events.subscribe(None)            # every event for everyone: episodes among them
            _threads["listen"] = threading.Thread(target=_listen, args=(sub,), name="slack-listen", daemon=True)
            _threads["listen"].start()
        if "send" not in _threads:
            _threads["send"] = threading.Thread(target=_send_loop, name="slack-send", daemon=True)
            _threads["send"].start()
    _wake.set()


# ---------------------------------------------------------------- each person's default

def default_for(uid) -> bool:
    ensure_schema()
    r = db.conn().execute("SELECT post FROM slack_prefs WHERE user_id = ?", (uid,)).fetchone()
    return True if r is None else bool(r["post"])


def set_default(uid, on: bool) -> None:
    ensure_schema()
    db.conn().execute("INSERT INTO slack_prefs (user_id, post, updated_at) VALUES (?, ?, ?) "
                      "ON CONFLICT(user_id) DO UPDATE SET post = excluded.post, updated_at = excluded.updated_at",
                      (uid, 1 if on else 0, db.now()))


def _mine(req) -> dict:
    return {"enabled": enabled(req.cfg), "channel": channel(req.cfg), "default": default_for(req.user["id"])}


def _default_from(req) -> bool:
    v = req.json().get("default")
    if not isinstance(v, bool):
        raise HTTPError(400, "bad_default", "default is true (post) or false (do not)")
    return v


# ---------------------------------------------------------------- routes

def cli_features(req):
    """What this hub offers `papercast add` beyond the upload."""
    req.send_json(200, {"slack": _mine(req)})


def cli_put_default(req):
    set_default(req.user["id"], _default_from(req))
    req.send_json(200, _mine(req))


def get_mine(req):
    req.send_json(200, dict(_mine(req), can_upload=req.user.get("role") in ("contributor", "admin")))


def put_mine(req):
    from .web import _mutation
    _mutation(req)
    set_default(req.user["id"], _default_from(req))
    get_mine(req)


def admin_get(req):
    ensure_schema()
    url, problem = webhook(req.cfg)
    rows = db.conn().execute(
        "SELECT s.*, e.paper_id, p.title, u.name AS maker FROM slack_posts s "
        "LEFT JOIN episodes e ON e.id = s.episode_id LEFT JOIN papers p ON p.id = e.paper_id "
        "LEFT JOIN users u ON u.id = s.user_id ORDER BY s.created_at DESC, s.rowid DESC LIMIT ?", (LAST,)).fetchall()
    req.send_json(200, {
        "configured": url is not None, "problem": problem, "channel": channel(req.cfg),
        "setting": "PCG_SLACK_WEBHOOK_FILE",
        "posts": [{"episode_id": r["episode_id"], "paper_id": r["paper_id"], "title": r["title"],
                   "made_by": {"id": r["user_id"], "name": r["maker"]}, "state": r["state"],
                   "detail": scrub(r["detail"], url) if r["detail"] else None, "attempts": r["attempts"],
                   "created_at": r["created_at"], "sent_at": r["sent_at"], "updated_at": r["updated_at"]}
                  for r in rows]})


def admin_test(req):
    from .web import _mutation
    _mutation(req)
    url, problem = webhook(req.cfg)
    if url is None:
        req.send_json(200, {"ok": False, "detail": f"Slack is not set up: {problem}"})
        return
    who = req.user.get("name") or req.user.get("email") or "an admin"
    text = (f"A test from papercast ({esc(req.cfg.public_url)}), sent by {esc(who)}: new episodes are "
            f"announced here when their makers say yes.")
    outcome, detail, _ = post(url, text, timeout=10.0)
    ok = outcome == "posted"
    (log.info if ok else log.warning)("slack: test message from %s: %s", who, detail)
    req.send_json(200, {"ok": ok, "detail": "sent" if ok else detail})


ROUTES = [
    ("GET", r"^/api/cli/features$", cli_features, "cli"),
    ("PUT", r"^/api/cli/slack$", cli_put_default, "cli"),
    ("GET", r"^/api/slack$", get_mine, "viewer"),
    ("PUT", r"^/api/slack$", put_mine, "viewer"),
    ("GET", r"^/api/admin/slack$", admin_get, "admin"),
    ("POST", r"^/api/admin/slack/test$", admin_test, "admin"),
]
