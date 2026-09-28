"""The voice queue and its fair order (SPEC.md section 9). Owner: A3.

A checked episode gets a `voice_jobs` row (state queued). The worker on perov asks for work with
POST /api/voice/claim and gets the next episode in the fair order: round-robin over uploaders by
who was served longest ago (someone never served goes first), oldest first within one uploader.
So one person's ten uploads never hold back another person's one.

A worker holds one claim at a time (asking again gives back the job it holds, so a restarted
worker resumes). Status updates are its heartbeat; a claim not updated for 30 minutes goes back
to the queue, and that lost turn does not count as the uploader being served. A failure is shown on the episode with the error and is tried again until the
third attempt, after which it stays failed.

Two columns this module adds to voice_jobs if they are missing (A1 may put them in SCHEMA):
`served_seq` (a counter set when a job is handed out: who was served longest ago, even within
one second) and `worker` (which worker holds the claim).

Also here, because the bundle upload needs it too: save_body(), which streams a request body to
disk in chunks under a size limit instead of holding it in memory."""
from __future__ import annotations

import logging
import os
import sqlite3
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import db, events
from .app import HTTPError

log = logging.getLogger("pcg.voiceq")

MB = 1024 * 1024
AUDIO_MAX = 200 * MB
AUDIO_MIN = 1024                 # smaller than any real episode: a broken encode, not audio
STALE_S = 30 * 60                # a claim not updated for this long goes back to the queue
MAX_ATTEMPTS = 3
MAX_DURATION_S = 6 * 3600
SWEEP_S = 60
CHUNK = 1 << 20
# The voice's own phases (INTERFACE 10.3) that mean it has not started speaking yet; Leo's runner
# maps them to waiting-for-gpu the same way.
WAITING_PHASES = {"claimed", "preparing", "waiting", "waiting-for-gpu"}
EXTRA_COLUMNS = (("served_seq", "INTEGER"), ("worker", "TEXT"))


# ---- small helpers

def _ago(seconds: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


def save_body(req, dest: Path, limit: int, what: str) -> int:
    """Stream the request body to `dest` (at most `limit` bytes, Content-Length required).
    Marks the request so a refusal before this point closes the connection instead of leaving
    an unread body on it."""
    te = (req.headers.get("Transfer-Encoding") or "").strip().lower()
    if te and te != "identity":
        raise HTTPError(411, "length_required", f"send {what} with a Content-Length")
    try:
        n = int(req.headers.get("Content-Length") or "")
    except ValueError:
        raise HTTPError(411, "length_required", f"send {what} with a Content-Length")
    if n > limit:
        raise HTTPError(413, "too_large", f"{what} is {n:,} bytes, over the {limit // MB} MB limit")
    if n <= 0:
        raise HTTPError(400, "empty", f"{what} is empty")
    left = n
    try:
        with open(dest, "wb") as f:
            while left:
                chunk = req.h.rfile.read(min(CHUNK, left))
                if not chunk:
                    raise HTTPError(400, "incomplete", f"{what} ended after {n - left} of {n} bytes")
                f.write(chunk)
                left -= len(chunk)
    except OSError:
        raise HTTPError(400, "incomplete", f"{what} did not arrive in full")
    req.body_read = True
    return n


def streamed(fn):
    """For handlers that stream a body: a refusal sent before the body was read closes the
    connection (the unread bytes would otherwise be taken for the next request)."""
    def wrap(req, *args):
        try:
            return fn(req, *args)
        except BaseException:
            if not getattr(req, "body_read", False):
                req.h.close_connection = True
            raise
    wrap.__name__ = fn.__name__
    wrap.__doc__ = fn.__doc__
    return wrap


def ensure_columns(c) -> None:
    have = {r[1] for r in c.execute("PRAGMA table_info(voice_jobs)")}
    for name, typ in EXTRA_COLUMNS:
        if name not in have:
            try:
                c.execute(f"ALTER TABLE voice_jobs ADD COLUMN {name} {typ}")
            except sqlite3.OperationalError as e:      # another thread added it first
                if "duplicate column" not in str(e):
                    raise


def _publish(c, episode_ids) -> None:
    for eid in episode_ids:
        r = c.execute("SELECT e.id, e.paper_id, e.state, e.state_detail, e.made_by, e.duration_s, "
                      "v.phase, v.progress FROM episodes e LEFT JOIN voice_jobs v ON v.episode_id = e.id "
                      "WHERE e.id = ?", (eid,)).fetchone()
        if r:
            events.publish("episode", {"id": r["id"], "paper_id": r["paper_id"], "state": r["state"],
                                       "state_detail": r["state_detail"], "made_by": r["made_by"],
                                       "duration_s": r["duration_s"], "phase": r["phase"],
                                       "progress": r["progress"]})


# ---- the queue (other modules call these)

def enqueue(c, episode_id: str, user_id: int) -> None:
    """A checked episode joins the queue (inside the caller's transaction)."""
    c.execute("INSERT OR IGNORE INTO voice_jobs (episode_id, user_id, state, queued_at, attempts) "
              "VALUES (?, ?, 'queued', ?, 0)", (episode_id, user_id, db.now()))


def requeue_stale(c) -> list:
    """Claims not updated for STALE_S go back to the queue; returns their episode ids."""
    rows = c.execute("SELECT episode_id FROM voice_jobs WHERE state = 'claimed' "
                     "AND COALESCE(heartbeat_at, claimed_at) < ?", (_ago(STALE_S),)).fetchall()
    ids = [r["episode_id"] for r in rows]
    now = db.now()
    for eid in ids:
        # served_seq cleared: a turn lost to a dead worker is not a turn served
        c.execute("UPDATE voice_jobs SET state = 'queued', worker = NULL, phase = NULL, progress = NULL, "
                  "served_seq = NULL WHERE episode_id = ?", (eid,))
        c.execute("UPDATE episodes SET state = 'waiting-for-gpu', updated_at = ?, "
                  "state_detail = 'the voice stopped answering for 30 minutes; back in the queue' "
                  "WHERE id = ? AND state IN ('waiting-for-gpu', 'speaking')", (now, eid))
        log.info("voice claim on %s went stale; back in the queue", eid)
    return ids


def _eligible(c) -> list:
    return c.execute(
        "SELECT v.episode_id, v.user_id, v.queued_at, v.rowid AS rid, v.state, v.attempts "
        "FROM voice_jobs v JOIN episodes e ON e.id = v.episode_id "
        "WHERE e.deleted_at IS NULL AND (v.state = 'queued' OR (v.state = 'failed' AND v.attempts < ?))",
        (MAX_ATTEMPTS,)).fetchall()


def _last_served(c) -> dict:
    return {r[0]: r[1] for r in c.execute(
        "SELECT user_id, MAX(served_seq) FROM voice_jobs WHERE served_seq IS NOT NULL GROUP BY user_id")}


def fair_order(jobs, served: dict) -> list:
    """The order the worker will get `jobs` in: each turn goes to the uploader served longest ago
    (never served first; a tie goes to whoever has the older job), their oldest job first."""
    by_user: dict = {}
    for j in sorted(jobs, key=lambda j: (j["queued_at"], j["rid"])):
        by_user.setdefault(j["user_id"], []).append(j)
    last = {u: served.get(u) for u in by_user}
    seq = max([v for v in served.values() if v is not None], default=0)
    out = []
    while by_user:
        u = min(by_user, key=lambda u: (last[u] is not None, last[u] or 0,
                                        by_user[u][0]["queued_at"], by_user[u][0]["rid"]))
        out.append(by_user[u].pop(0))
        seq += 1
        last[u] = seq
        if not by_user[u]:
            del by_user[u]
    return out


def queue_positions() -> dict:
    """{episode_id: place in line (1 = next)} for everything waiting for the voice."""
    c = db.conn()
    ensure_columns(c)
    return {j["episode_id"]: i for i, j in enumerate(fair_order(_eligible(c), _last_served(c)), 1)}


# ---- the sweeper: stale claims go back even when no worker is asking (a dead worker)

_sweep_lock = threading.Lock()
_sweep_started = False


def start_sweeper() -> None:
    global _sweep_started
    with _sweep_lock:
        if _sweep_started:
            return
        _sweep_started = True
    threading.Thread(target=_sweep, name="voice-sweeper", daemon=True).start()


def _sweep() -> None:
    while True:
        time.sleep(SWEEP_S)
        try:
            with db.transaction() as c:
                ensure_columns(c)
                ids = requeue_stale(c)
            _publish(db.conn(), ids)
        except Exception:
            log.exception("voice sweeper")


# ---- routes (worker)

def _job(c, eid):
    return c.execute("SELECT * FROM voice_jobs WHERE episode_id = ?", (eid,)).fetchone()


def _worker_name(req, body=None) -> str:
    w = (body or {}).get("worker") if isinstance(body, dict) else None
    w = w or req.headers.get("X-Worker") or "worker"
    return str(w).strip()[:100] or "worker"


def _check_holder(req, job, body=None) -> None:
    named = (body or {}).get("worker") if isinstance(body, dict) else None
    named = named or req.headers.get("X-Worker")
    if named and job["worker"] and str(named).strip()[:100] != job["worker"]:
        raise HTTPError(409, "claimed_by_other", f"{job['episode_id']} is held by {job['worker']}")


def claim(req):
    start_sweeper()
    body = req.json()
    worker = _worker_name(req, body)
    now = db.now()
    with db.transaction() as c:
        ensure_columns(c)
        stale = requeue_stale(c)
        job = c.execute("SELECT * FROM voice_jobs WHERE state = 'claimed' AND worker = ? "
                        "ORDER BY claimed_at LIMIT 1", (worker,)).fetchone()
        resumed = job is not None
        if resumed:                     # one claim per worker: give back the one it holds
            c.execute("UPDATE voice_jobs SET heartbeat_at = ? WHERE episode_id = ?", (now, job["episode_id"]))
        else:
            order = fair_order(_eligible(c), _last_served(c))
            if order:
                eid = order[0]["episode_id"]
                seq = (c.execute("SELECT MAX(served_seq) FROM voice_jobs").fetchone()[0] or 0) + 1
                c.execute("UPDATE voice_jobs SET state = 'claimed', worker = ?, claimed_at = ?, heartbeat_at = ?, "
                          "finished_at = NULL, phase = 'claimed', progress = 0, attempts = attempts + 1, "
                          "served_seq = ? WHERE episode_id = ?", (worker, now, now, seq, eid))
                c.execute("UPDATE episodes SET state = 'waiting-for-gpu', state_detail = 'with the voice', "
                          "updated_at = ? WHERE id = ?", (now, eid))
                job = _job(c, eid)
        info = None
        if job is not None:
            info = c.execute("SELECT e.id, e.paper_id, p.title, p.authors, p.year, u.name AS maker "
                             "FROM episodes e JOIN papers p ON p.id = e.paper_id "
                             "JOIN users u ON u.id = e.made_by WHERE e.id = ?", (job["episode_id"],)).fetchone()
    c = db.conn()
    _publish(c, stale + ([job["episode_id"]] if job is not None and not resumed else []))
    if job is None:
        req.send(204, b"", "application/json")
        return
    authors = db.loads(info["authors"], []) if info else []
    req.send_json(200, {
        "episode_id": job["episode_id"],
        "script_url": f"/api/voice/{job['episode_id']}/script",
        "title": info["title"] if info else None,
        "first_author": authors[0] if authors else None,
        "authors": authors,
        "year": info["year"] if info else None,
        "paper_id": info["paper_id"] if info else None,
        "made_by": info["maker"] if info else None,
        "attempt": job["attempts"],
        "resumed": resumed,
    })


def script(req, eid):
    c = db.conn()
    if _job(c, eid) is None:
        raise HTTPError(404, "no_such_job", f"no voice job for {eid}")
    p = req.cfg.episodes / eid / "script.md"
    if not p.is_file():
        raise HTTPError(404, "no_script", f"{eid} has no script.md")
    req.send(200, p.read_bytes(), "text/markdown; charset=utf-8")


def _progress(v):
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise HTTPError(400, "bad_progress", "progress is a number from 0 to 1")
    v = float(v)
    if 1 < v <= 100:                    # a percentage
        v /= 100
    if not 0 <= v <= 1:
        raise HTTPError(400, "bad_progress", "progress is a number from 0 to 1")
    return round(v, 4)


def status(req, eid):
    body = req.json()
    phase = body.get("phase")
    if not isinstance(phase, str) or not phase.strip() or len(phase) > 40:
        raise HTTPError(400, "bad_phase", "phase is a short word such as speaking")
    phase = phase.strip().lower()
    progress = _progress(body.get("progress"))
    note = body.get("note") or body.get("text")
    note = note.strip()[:300] if isinstance(note, str) and note.strip() else None
    now = db.now()
    with db.transaction() as c:
        ensure_columns(c)
        job = _job(c, eid)
        if job is None:
            raise HTTPError(404, "no_such_job", f"no voice job for {eid}")
        if job["state"] != "claimed":
            raise HTTPError(409, "not_claimed", f"{eid} is {job['state']}, not claimed")
        _check_holder(req, job, body)
        c.execute("UPDATE voice_jobs SET heartbeat_at = ?, phase = ?, progress = ? WHERE episode_id = ?",
                  (now, phase, progress, eid))
        state = "waiting-for-gpu" if phase in WAITING_PHASES else "speaking"
        detail = note or (f"{phase}, {progress * 100:.0f}%" if progress is not None else phase)
        c.execute("UPDATE episodes SET state = ?, state_detail = ?, updated_at = ? WHERE id = ?",
                  (state, detail, now, eid))
    _publish(db.conn(), [eid])
    req.send_json(200, {"episode_id": eid, "state": state, "phase": phase, "progress": progress})


def looks_like_mp3(head: bytes) -> bool:
    """ID3v2 tags, or an MPEG audio frame: eleven sync bits and a layer that is not reserved."""
    if head[:3] == b"ID3":
        return True
    return len(head) >= 2 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0 and (head[1] & 0x06) != 0


@streamed
def audio(req, eid):
    ctype = (req.headers.get("Content-Type") or "").split(";")[0].strip().lower()
    if ctype != "audio/mpeg":
        raise HTTPError(415, "bad_type", "send the audio as audio/mpeg")
    try:
        dur = float(req.headers.get("X-Duration-S") or "")
    except ValueError:
        raise HTTPError(400, "bad_duration", "X-Duration-S (seconds) is required")
    if not 0 < dur <= MAX_DURATION_S:
        raise HTTPError(400, "bad_duration", f"X-Duration-S must be between 0 and {MAX_DURATION_S} seconds")
    c = db.conn()
    ensure_columns(c)
    job = _job(c, eid)
    if job is None:
        raise HTTPError(404, "no_such_job", f"no voice job for {eid}")
    if job["state"] == "done":
        raise HTTPError(409, "already_done", f"{eid} already has its audio")
    _check_holder(req, job)
    tmpdir = req.cfg.data / "tmp"
    tmpdir.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f"audio-{eid}-", suffix=".mp3", dir=tmpdir)
    os.close(fd)
    tmp = Path(name)
    try:
        n = save_body(req, tmp, AUDIO_MAX, "the audio")
        with open(tmp, "rb") as f:
            head = f.read(4)
        if n < AUDIO_MIN or not looks_like_mp3(head):
            raise HTTPError(400, "not_mp3", "the body does not start like an MP3 (ID3 tag or MPEG frame)")
        epdir = req.cfg.episodes / eid
        epdir.mkdir(parents=True, exist_ok=True)
        os.replace(tmp, epdir / "audio.mp3")
    finally:
        tmp.unlink(missing_ok=True)
    now = db.now()
    with db.transaction() as c:
        c.execute("UPDATE voice_jobs SET state = 'done', finished_at = ?, heartbeat_at = ?, phase = 'done', "
                  "progress = 1, error = NULL, worker = NULL WHERE episode_id = ?", (now, now, eid))
        c.execute("UPDATE episodes SET state = 'ready', duration_s = ?, state_detail = NULL, updated_at = ? "
                  "WHERE id = ?", (round(dur, 2), now, eid))
    _publish(db.conn(), [eid])
    req.send_json(200, {"episode_id": eid, "state": "ready", "duration_s": round(dur, 2), "bytes": n})


def failed(req, eid):
    body = req.json()
    err = body.get("error")
    if isinstance(err, dict):
        err = err.get("message") or err.get("code") or db.dumps(err)
    err = str(err).strip()[:2000] if err else "the voice failed without saying why"
    now = db.now()
    with db.transaction() as c:
        ensure_columns(c)
        job = _job(c, eid)
        if job is None:
            raise HTTPError(404, "no_such_job", f"no voice job for {eid}")
        if job["state"] not in ("claimed", "queued"):
            raise HTTPError(409, "not_claimed", f"{eid} is {job['state']}, not claimed")
        _check_holder(req, job, body)
        attempts = max(job["attempts"], 1)
        retry = attempts < MAX_ATTEMPTS
        c.execute("UPDATE voice_jobs SET state = 'failed', error = ?, finished_at = ?, phase = 'failed', "
                  "worker = NULL WHERE episode_id = ?", (err, now, eid))
        detail = f"the voice failed: {err} " + (
            f"(attempt {attempts} of {MAX_ATTEMPTS}; it will be tried again)" if retry
            else f"(tried {MAX_ATTEMPTS} times; it stays failed)")
        c.execute("UPDATE episodes SET state = 'failed', state_detail = ?, updated_at = ? WHERE id = ?",
                  (detail, now, eid))
    _publish(db.conn(), [eid])
    req.send_json(200, {"episode_id": eid, "state": "failed", "attempts": attempts, "retry": retry})


def queue(req):
    """What the worker holds and what waits, in the order it will be handed out."""
    c = db.conn()
    ensure_columns(c)
    held = c.execute("SELECT episode_id, worker, phase, progress, claimed_at, heartbeat_at, attempts "
                     "FROM voice_jobs WHERE state = 'claimed' ORDER BY claimed_at").fetchall()
    order = fair_order(_eligible(c), _last_served(c))
    req.send_json(200, {"claimed": [dict(r) for r in held],
                        "queued": [{"episode_id": j["episode_id"], "user_id": j["user_id"],
                                    "queued_at": j["queued_at"], "attempts": j["attempts"],
                                    "retry": j["state"] == "failed"} for j in order]})


_ID = r"([A-Za-z0-9_-]{1,64})"
ROUTES = [
    ("POST", r"^/api/voice/claim$", claim, "worker"),
    ("GET", r"^/api/voice/queue$", queue, "worker"),
    ("GET", rf"^/api/voice/{_ID}/script$", script, "worker"),
    ("PUT", rf"^/api/voice/{_ID}/status$", status, "worker"),
    ("PUT", rf"^/api/voice/{_ID}/audio$", audio, "worker"),
    ("POST", rf"^/api/voice/{_ID}/failed$", failed, "worker"),
]
