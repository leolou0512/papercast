"""Custom voices: previews, "use this voice", and the previews' place in the voice queue (SPEC.md
section 9; the voice itself, its spec and where it is used, are voices.py's).

Each person may describe the narrator they want in Breeze TTS 2's own words ("Young adult
female, mid-20s, Irish accent. Warm, clear voice, ..."), hear it say the paragraph every preset
sample says (voices.SAMPLE_TEXT), try another take (a new seed), and use it: it becomes their
custom voice (voices.py user_custom_voice: the description and seed of that preview) and their
voice for new versions, as picking a preset does. "Use" takes only a finished preview, so a
description that fails (papercast-voice refuses a clip whose loudness is off, say) is never
anyone's voice.

Previews go through the real voice path. The worker's claim (voiceq.claim) may hand one out as
an episode-shaped job under the id vp-<user id>, with the custom voice as its `voice`; its
status, timings (ignored), audio and failure come back on the same /api/voice/<id>/... routes,
which hand a vp- id to this module. So the worker and papercast-voice are unchanged: the job
directory is <jobs>/vp-<user id>/voice/, one per person, started afresh by the worker for each
new take (another voice in job.json).

Queue order: previews take turns with episodes. When both are waiting, the next claim is the
oldest preview unless the last thing handed out was a preview. So an episode waits behind at
most one preview (about 80 s) per turn, however many people preview, and a preview waits for at
most the episode being voiced and one more.

Limits: one preview per person waiting or being made (asking again while it waits replaces its
text or take, in the same place in line; while it is being made, 409), PER_HOUR asked for per
person per rolling hour, the description at most DESC_MAX characters once control characters are
out and whitespace runs are one space. A failed preview is not tried again (the same words and
seed fail the same way); one whose worker went silent for voiceq.STALE_S goes back to the queue,
MAX_ATTEMPTS claims at most. A clip in another voice than asked (papercast-voice fell back to the
CPU voice) is refused and the preview fails.

Files: <data>/voices/custom/<user id>.mp3, the latest finished preview (the next replaces it);
<user id>.voice.mp3, the clip of the custom voice in use (copied from its preview on "use").

Table voice_previews: one row per preview asked for (the hourly count reads them; a person's
rows older than a day go, except their latest).

    POST /api/voices/custom/preview {"description", "another": bool}   preview it (another: a new seed)
    PUT  /api/voices/custom {"preview": id}           use that finished preview's voice: my custom
                                                      voice, and my voice for new versions
    GET  /api/voices/custom/preview.mp3               my latest finished preview
    GET  /api/voices/custom/<user id>/sample.mp3      a person's custom voice (theirs, or an admin)
Live: an event `voice` {preview} or {mine, custom}, to that person only.
"""
from __future__ import annotations

import json
import logging
import os
import re
import secrets
import shutil
import tempfile
import threading
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import db, events, voices
from .app import HTTPError

log = logging.getLogger("pcg.customvoice")

DESC_MAX = 500
PER_HOUR = 5
MAX_ATTEMPTS = 3
FIRST_SEED = 42                 # the presets' seed: a first preview of a description takes it
PREVIEW_MAX = 20 * 1024 * 1024  # a twenty-second clip is well under 1 MB
KEEP_S = 24 * 3600
EXAMPLE = "Young adult female, mid-20s, Irish accent. Warm, clear voice, steady pace."
PID = re.compile(r"^vp-([0-9]{1,12})$")

SCHEMA = """
CREATE TABLE IF NOT EXISTS voice_previews (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL REFERENCES users(id),
  description TEXT NOT NULL,
  seed INTEGER NOT NULL,
  state TEXT NOT NULL CHECK (state IN ('queued', 'claimed', 'done', 'failed')),
  queued_at TEXT NOT NULL,
  claimed_at TEXT,
  heartbeat_at TEXT,
  finished_at TEXT,
  worker TEXT,
  phase TEXT,
  progress REAL,
  attempts INTEGER NOT NULL DEFAULT 0,
  served_seq INTEGER,
  error TEXT,
  duration_s REAL
)
"""
INDEXES = ("CREATE INDEX IF NOT EXISTS voice_previews_user ON voice_previews(user_id, id)",
           "CREATE INDEX IF NOT EXISTS voice_previews_state ON voice_previews(state, queued_at)")

_ready: set = set()
_ready_lock = threading.Lock()


def ensure_schema(c=None) -> None:
    key = str(db._path)
    if key in _ready:
        return
    with _ready_lock:
        if key in _ready:
            return
        c = c or db.conn()
        c.execute(SCHEMA)
        for s in INDEXES:
            c.execute(s)
        if not c.in_transaction:
            _ready.add(key)


def start(cfg) -> None:
    voices.ensure_schema()


# ---------------------------------------------------------------- helpers

def _ago(seconds: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _u(req, k):
    try:
        return req.user[k]
    except (KeyError, IndexError, TypeError):
        return None


def clean(v) -> str:
    """A description as it is kept: control characters out, whitespace runs one space; 400 if it
    is empty or longer than DESC_MAX."""
    if not isinstance(v, str):
        raise HTTPError(400, "bad_description", "describe the voice in words")
    v = unicodedata.normalize("NFC", v)
    v = " ".join("".join(" " if unicodedata.category(ch)[0] == "C" else ch for ch in v).split())
    if not v:
        raise HTTPError(400, "empty", "describe the voice first")
    if len(v) > DESC_MAX:
        raise HTTPError(400, "too_long", f"at most {DESC_MAX} characters")
    return v


def is_preview(eid: str) -> bool:
    return bool(PID.match(eid or ""))


def job_id(uid: int) -> str:
    return f"vp-{int(uid)}"


def _dir(cfg) -> Path:
    return cfg.data / "voices" / "custom"


def preview_file(cfg, uid) -> Path:
    return _dir(cfg) / f"{int(uid)}.mp3"


def voice_file(cfg, uid) -> Path:
    return _dir(cfg) / f"{int(uid)}.voice.mp3"


def _plain(f: Path) -> bool:
    return f.is_file() and not f.is_symlink()


def _latest(c, uid):
    return c.execute("SELECT * FROM voice_previews WHERE user_id = ? ORDER BY id DESC LIMIT 1", (uid,)).fetchone()


def _spec(r) -> dict:
    return voices.custom_spec(r["user_id"], r["description"], r["seed"])


def preview_view(cfg, r) -> dict | None:
    """state: queued (waiting), working (being made), done or failed."""
    if r is None:
        return None
    state = {"claimed": "working"}.get(r["state"], r["state"])
    done = state == "done" and _plain(preview_file(cfg, r["user_id"]))
    return {"id": r["id"], "state": state, "description": r["description"], "seed": r["seed"],
            "key": _spec(r)["key"], "error": r["error"] if state == "failed" else None,
            "duration_s": r["duration_s"] if done else None,
            "sample": f"/api/voices/custom/preview.mp3?v={r['id']}" if done else None}


def custom_view(cfg, sp: dict | None) -> dict | None:
    """My custom voice, for Settings."""
    if sp is None:
        return None
    return {"id": voices.CUSTOM, "name": voices.CUSTOM_NAME, "description": sp["description"], "seed": sp["seed"],
            "key": sp["key"], "sample": voices.custom_sample_url(cfg, sp["user_id"])}


def page_fields(cfg, c, uid) -> dict:
    """What GET /api/voices adds for Settings, Voice."""
    ensure_schema(c)
    return {"custom": custom_view(cfg, voices.saved_custom(c, uid)),
            "preview": preview_view(cfg, _latest(c, uid)) if uid is not None else None,
            "custom_max": DESC_MAX, "custom_example": EXAMPLE, "previews_per_hour": PER_HOUR}


def notify(cfg, uids) -> None:
    """The person's own pages hear how their preview stands."""
    c = db.conn()
    for uid in sorted(set(uids)):
        events.publish("voice", {"preview": preview_view(cfg, _latest(c, uid))}, users=[uid])


# ---------------------------------------------------------------- the queue (voiceq.py calls these)

def max_seq(c) -> int:
    ensure_schema(c)
    return c.execute("SELECT MAX(served_seq) FROM voice_previews").fetchone()[0] or 0


def waiting(c) -> list:
    ensure_schema(c)
    return c.execute("SELECT * FROM voice_previews WHERE state = 'queued' ORDER BY queued_at, id").fetchall()


def _last_was_preview(c) -> bool:
    p = c.execute("SELECT MAX(served_seq) FROM voice_previews").fetchone()[0]
    e = c.execute("SELECT MAX(served_seq) FROM voice_jobs").fetchone()[0]
    return p is not None and (e is None or p > e)


def merge(c, episodes: list) -> list:
    """[(kind, row)]: the episodes in their fair order with the waiting previews taking turns
    with them (a preview first unless the last one handed out was a preview)."""
    pv = waiting(c)
    if not pv:
        return [("episode", j) for j in episodes]
    last_preview = _last_was_preview(c)
    out, i, k = [], 0, 0
    while i < len(episodes) or k < len(pv):
        if k < len(pv) and (i >= len(episodes) or not last_preview):
            out.append(("preview", pv[k]))
            k, last_preview = k + 1, True
        else:
            out.append(("episode", episodes[i]))
            i, last_preview = i + 1, False
    return out


def requeue_stale(c) -> list:
    """Preview claims not updated for voiceq.STALE_S go back to the queue (or fail after
    MAX_ATTEMPTS claims); returns whose they were."""
    from . import voiceq
    ensure_schema(c)
    rows = c.execute("SELECT id, user_id, attempts FROM voice_previews WHERE state = 'claimed' "
                     "AND COALESCE(heartbeat_at, claimed_at) < ?", (_ago(voiceq.STALE_S),)).fetchall()
    for r in rows:
        if r["attempts"] >= MAX_ATTEMPTS:
            c.execute("UPDATE voice_previews SET state = 'failed', worker = NULL, finished_at = ?, "
                      "error = 'the voice stopped answering' WHERE id = ?", (db.now(), r["id"]))
        else:
            c.execute("UPDATE voice_previews SET state = 'queued', worker = NULL, phase = NULL, progress = NULL, "
                      "served_seq = NULL WHERE id = ?", (r["id"],))
        log.info("preview %s (user %s) went stale", r["id"], r["user_id"])
    return [r["user_id"] for r in rows]


def held(c, worker: str):
    ensure_schema(c)
    return c.execute("SELECT * FROM voice_previews WHERE state = 'claimed' AND worker = ? ORDER BY claimed_at LIMIT 1",
                     (worker,)).fetchone()


def take(c, r, worker: str, now: str, seq: int):
    c.execute("UPDATE voice_previews SET state = 'claimed', worker = ?, claimed_at = ?, heartbeat_at = ?, "
              "finished_at = NULL, phase = 'claimed', progress = 0, attempts = attempts + 1, served_seq = ?, "
              "error = NULL WHERE id = ?", (worker, now, now, seq, r["id"]))
    return c.execute("SELECT * FROM voice_previews WHERE id = ?", (r["id"],)).fetchone()


def touch(c, r, now: str) -> None:
    c.execute("UPDATE voice_previews SET heartbeat_at = ? WHERE id = ?", (now, r["id"]))


def claim_body(c, r, resumed: bool) -> dict:
    """The claim answer for a preview: shaped as an episode's, so the worker voices it as one."""
    eid = job_id(r["user_id"])
    u = c.execute("SELECT name FROM users WHERE id = ?", (r["user_id"],)).fetchone()
    return {"episode_id": eid, "script_url": f"/api/voice/{eid}/script", "title": "Voice preview",
            "first_author": None, "authors": [], "year": None, "paper_id": None,
            "made_by": u["name"] if u else None, "attempt": r["attempts"], "resumed": resumed,
            "voice": voices.for_claim(voices.custom_preset(_spec(r))), "preview": True}


def held_rows(c) -> list:
    """The previews being made, for GET /api/voice/queue."""
    ensure_schema(c)
    return [{"episode_id": job_id(r["user_id"]), "worker": r["worker"], "phase": r["phase"],
             "progress": r["progress"], "claimed_at": r["claimed_at"], "heartbeat_at": r["heartbeat_at"],
             "attempts": r["attempts"], "preview": True}
            for r in c.execute("SELECT * FROM voice_previews WHERE state = 'claimed' ORDER BY claimed_at")]


# ---------------------------------------------------------------- the worker's routes, for vp- ids

def _claimed(c, eid: str, req, body=None):
    """The preview this worker holds under that id; 404/409 (the worker drops it) otherwise."""
    ensure_schema(c)
    uid = int(PID.match(eid).group(1))
    r = _latest(c, uid)
    if r is None:
        raise HTTPError(404, "no_such_job", f"no preview for {eid}")
    if r["state"] != "claimed":
        raise HTTPError(409, "not_claimed", f"{eid} is {r['state']}, not claimed")
    named = (body or {}).get("worker") if isinstance(body, dict) else None
    named = named or req.headers.get("X-Worker")
    if named and r["worker"] and str(named).strip()[:100] != r["worker"]:
        raise HTTPError(409, "claimed_by_other", f"{eid} is held by {r['worker']}")
    return r


def w_script(req, eid):
    c = db.conn()
    ensure_schema(c)
    r = _latest(c, int(PID.match(eid).group(1)))
    if r is None or r["state"] not in ("queued", "claimed"):
        raise HTTPError(404, "no_such_job", f"no preview waiting for {eid}")
    req.send(200, (voices.SAMPLE_TEXT + "\n").encode(), "text/markdown; charset=utf-8")


def w_status(req, eid):
    from . import voiceq
    body = req.json()
    phase = body.get("phase")
    if not isinstance(phase, str) or not phase.strip() or len(phase) > 40:
        raise HTTPError(400, "bad_phase", "phase is a short word such as speaking")
    phase = phase.strip().lower()
    progress = voiceq._progress(body.get("progress"))
    with db.transaction() as c:
        r = _claimed(c, eid, req, body)
        c.execute("UPDATE voice_previews SET heartbeat_at = ?, phase = ?, progress = ? WHERE id = ?",
                  (db.now(), phase, progress, r["id"]))
    if phase != r["phase"]:
        notify(req.cfg, [r["user_id"]])
    req.send_json(200, {"episode_id": eid, "state": "working", "phase": phase, "progress": progress})


def w_timings(req, eid):
    """A preview needs no timings: taken and dropped, so the worker goes on to the MP3."""
    req.body(voices.TIMINGS_MAX)
    _claimed(db.conn(), eid, req)
    req.send_json(200, {"episode_id": eid, "stored": "none"})


def _fail(c, r, err: str) -> None:
    c.execute("UPDATE voice_previews SET state = 'failed', error = ?, finished_at = ?, phase = 'failed', "
              "worker = NULL WHERE id = ?", (err[:600], db.now(), r["id"]))


def w_audio(req, eid):
    """The preview's MP3 (voiceq.audio's checks): the person's latest preview from now on."""
    from . import voiceq
    ctype = (req.headers.get("Content-Type") or "").split(";")[0].strip().lower()
    if ctype != "audio/mpeg":
        raise HTTPError(415, "bad_type", "send the audio as audio/mpeg")
    try:
        dur = float(req.headers.get("X-Duration-S") or "")
    except ValueError:
        raise HTTPError(400, "bad_duration", "X-Duration-S (seconds) is required")
    if not 0 < dur <= 600:
        raise HTTPError(400, "bad_duration", "X-Duration-S must be between 0 and 600 seconds for a preview")
    c = db.conn()
    r = _claimed(c, eid, req)
    want = _spec(r)["key"]
    key = (req.headers.get("X-Voice") or "").strip()[:80] or None
    if key and key != want:
        with db.transaction() as t:
            _fail(t, r, f"it came out in another voice ({key}), not the one described")
        notify(req.cfg, [r["user_id"]])
        raise HTTPError(422, "wrong_voice", f"{eid}: the clip is in {key}, not {want}")
    tmpdir = req.cfg.data / "tmp"
    tmpdir.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f"preview-{eid}-", suffix=".mp3", dir=tmpdir)
    os.close(fd)
    tmp = Path(name)
    try:
        n = voiceq.save_body(req, tmp, PREVIEW_MAX, "the preview")
        with open(tmp, "rb") as f:
            head = f.read(4)
        if n < voiceq.AUDIO_MIN or not voiceq.looks_like_mp3(head):
            raise HTTPError(400, "not_mp3", "the body does not start like an MP3 (ID3 tag or MPEG frame)")
        with db.transaction() as t:
            r = _claimed(t, eid, req)                   # still the one being made
            _dir(req.cfg).mkdir(parents=True, exist_ok=True)
            os.replace(tmp, preview_file(req.cfg, r["user_id"]))
            t.execute("UPDATE voice_previews SET state = 'done', finished_at = ?, heartbeat_at = ?, phase = 'done', "
                      "progress = 1, error = NULL, worker = NULL, duration_s = ? WHERE id = ?",
                      (db.now(), db.now(), round(dur, 2), r["id"]))
    finally:
        tmp.unlink(missing_ok=True)
    notify(req.cfg, [r["user_id"]])
    req.send_json(200, {"episode_id": eid, "state": "ready", "duration_s": round(dur, 2), "bytes": n})


def w_failed(req, eid):
    body = req.json()
    err = body.get("error")
    if isinstance(err, dict):
        err = err.get("message") or err.get("code") or db.dumps(err)
    err = str(err).strip()[:2000] if err else "the voice failed without saying why"
    with db.transaction() as c:
        r = _claimed(c, eid, req, body)
        _fail(c, r, err)
    notify(req.cfg, [r["user_id"]])
    req.send_json(200, {"episode_id": eid, "state": "failed", "attempts": r["attempts"], "retry": False})


# ---------------------------------------------------------------- the page's routes

def _mutation(req):
    from . import web
    web._mutation(req)


def post_preview(req):
    """Preview a description (another: in a new take). The same words and take as the preview
    waiting, being made or made already: nothing new to make."""
    _mutation(req)
    body = req.json()
    desc = clean(body.get("description"))
    another = body.get("another") is True
    uid = _u(req, "id")
    voices.ensure_schema()
    with db.transaction() as c:
        cur = _latest(c, uid)
        if another:
            old = cur["seed"] if cur else None
            seed = old
            while seed == old:
                seed = secrets.randbelow(2 ** 31)
        else:
            sp = voices.saved_custom(c, uid)
            seed = cur["seed"] if cur else (sp["seed"] if sp else FIRST_SEED)
        same = cur is not None and cur["description"] == desc and cur["seed"] == seed
        if same and (cur["state"] in ("queued", "claimed") or
                     (cur["state"] == "done" and _plain(preview_file(req.cfg, uid)))):
            pass
        elif cur is not None and cur["state"] == "claimed":
            raise HTTPError(409, "busy", "wait for the preview being made")
        elif cur is not None and cur["state"] == "queued":        # the one waiting says this instead
            c.execute("UPDATE voice_previews SET description = ?, seed = ? WHERE id = ?", (desc, seed, cur["id"]))
        else:
            n = c.execute("SELECT COUNT(*) FROM voice_previews WHERE user_id = ? AND queued_at >= ?",
                          (uid, _ago(3600))).fetchone()[0]
            if n >= PER_HOUR:
                raise HTTPError(429, "too_many", f"{PER_HOUR} previews an hour: try again later")
            c.execute("INSERT INTO voice_previews (user_id, description, seed, state, queued_at) "
                      "VALUES (?, ?, ?, 'queued', ?)", (uid, desc, seed, db.now()))
            c.execute("DELETE FROM voice_previews WHERE user_id = ? AND queued_at < ? AND state IN ('done', 'failed') "
                      "AND id < (SELECT MAX(id) FROM voice_previews WHERE user_id = ?)", (uid, _ago(KEEP_S), uid))
    v = preview_view(req.cfg, _latest(db.conn(), uid))
    events.publish("voice", {"preview": v}, users=[uid])
    req.send_json(200, {"preview": v})


def put_custom(req):
    """Use this finished preview's voice: it becomes my custom voice and my voice for new versions."""
    _mutation(req)
    pid = req.json().get("preview")
    if not isinstance(pid, int) or isinstance(pid, bool):
        raise HTTPError(400, "bad_preview", "which preview: its id")
    uid = _u(req, "id")
    voices.ensure_schema()
    c = db.conn()
    r = _latest(c, uid)
    src = preview_file(req.cfg, uid)
    if r is None or r["id"] != pid or r["state"] != "done" or not _plain(src):
        raise HTTPError(409, "not_ready", "preview it first")
    dest = voice_file(req.cfg, uid)
    tmp = dest.with_name(dest.name + ".tmp")
    shutil.copyfile(src, tmp)
    try:
        with db.transaction() as t:
            if (_latest(t, uid) or {"id": None})["id"] != pid:
                raise HTTPError(409, "not_ready", "preview it first")
            t.execute("INSERT INTO user_custom_voice (user_id, description, seed, updated_at) VALUES (?, ?, ?, ?) "
                      "ON CONFLICT(user_id) DO UPDATE SET description = excluded.description, seed = excluded.seed, "
                      "updated_at = excluded.updated_at", (uid, r["description"], r["seed"], db.now()))
            voices.set_mine(t, uid, voices.CUSTOM)
        os.replace(tmp, dest)
    finally:
        tmp.unlink(missing_ok=True)
    out = {"mine": voices.CUSTOM, "name": voices.CUSTOM_NAME,
           "custom": custom_view(req.cfg, voices.saved_custom(db.conn(), uid))}
    events.publish("voice", out, users=[uid])
    req.send_json(200, out)


def _send_clip(req, f: Path) -> None:
    if not _plain(f):
        raise HTTPError(404, "not_found", "no clip for this voice yet")
    req.send_file(f, "audio/mpeg", {"Cache-Control": "private, max-age=86400",
                                    "Cross-Origin-Resource-Policy": "same-origin"})


def get_preview_mp3(req):
    ensure_schema()
    uid = _u(req, "id")
    r = _latest(db.conn(), uid)
    if r is None or r["state"] != "done":
        raise HTTPError(404, "not_found", "no finished preview")
    _send_clip(req, preview_file(req.cfg, uid))


def get_custom_sample(req, uid):
    uid = int(uid)
    if uid != _u(req, "id") and _u(req, "role") != "admin":
        raise HTTPError(404, "not_found", "no clip for this voice")
    if voices.saved_custom(db.conn(), uid) is None:
        raise HTTPError(404, "not_found", "no custom voice")
    _send_clip(req, voice_file(req.cfg, uid))


ROUTES = [
    ("POST", r"^/api/voices/custom/preview$", post_preview, "contributor"),
    ("PUT", r"^/api/voices/custom$", put_custom, "contributor"),
    ("GET", r"^/api/voices/custom/preview\.mp3$", get_preview_mp3, "viewer"),
    ("GET", r"^/api/voices/custom/([0-9]{1,12})/sample\.mp3$", get_custom_sample, "viewer"),
]
