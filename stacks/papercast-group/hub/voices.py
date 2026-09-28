"""Voices: the preset narrators, changing an episode's voice (re-voicing it), and the sentence
timings of its audio (the play-along transcript reads them). SPEC.md section 9.

Presets (PRESETS): Breeze TTS 2 narrators, each described in words and generated from a fixed
seed (as stacks/papercast/voice/papercast_voice/config.py sets its own default), and one Kokoro
voice on the CPU. `key` names the pair: papercast-voice caches chunks by it and names it in its
output, which is how the hub knows what an upload is in. `clear-female` is papercast-voice's own
default (config.py `voice`), so an episode voiced without a choice is in it.

Each person's own voice (Settings, Voice): the preset their new versions are voiced in
(user_voice; none chosen is the default). The claim of a new episode carries its maker's.

Changing the voice: the episode's maker, or an admin, picks a preset for an episode that has
audio. Its voice job goes back into the fair queue (voiceq.py) under the person who asked, the
audio that is there keeps playing, and the new audio replaces it when it lands, with its
timings; saved positions move to the same sentence in the new audio. Each person has at most
MAX_WAITING changes waiting or being made. A change not started yet can be taken back.

Tables (this module's, CREATE TABLE IF NOT EXISTS at start):
  user_voice      the preset each person chose for their new versions
  episode_voice   voice: the preset the audio is in (NULL: not known, as for imported ones);
                  rev: the audio's revision (1 the first; the page asks /audio/<id>.mp3?v=<rev>,
                  so no browser keeps playing a cached old one); want/want_by/want_at: the
                  preset being made and who asked; error: why the last change failed.

Files in the episode dir: timings.json (the audio's), timings.next.json (the audio being made:
the worker sends it before the MP3, whose arrival moves it in) and timings.prev.json (the audio
before the last change, to carry positions over).

    GET    /api/voices                             the presets, with their sample clips, and mine
    PUT    /api/voices/mine {"voice": id}          the voice my new versions are voiced in
    GET    /api/voices/<id>/sample.mp3
    GET    /api/episodes/<id>/voice[?at=S&rev=N]   voice, rev, a change under way; with at and
                                                   rev: where second S of revision N is now
    PUT    /api/episodes/<id>/voice {"voice": id}  maker or admin: re-voice it in that preset
    DELETE /api/episodes/<id>/voice                take back a change that has not started
    GET    /api/episodes/<id>/timings              timings.json, with `rev`: the audio they are for
"""
from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

from . import db, events
from .app import HTTPError

log = logging.getLogger("pcg.voices")

MAX_WAITING = 2
TIMINGS_MAX = 8 * 1024 * 1024
TIMINGS_MAX_SEGMENTS = 50_000
TEXT_MAX = 4000
MAX_DURATION_S = 6 * 3600
EID = r"(e_[a-z0-9]{4,32})"
VID = r"([a-z0-9-]{1,40})"

# The narrator descriptions follow Breeze's own voice-design prompts ("Young adult female,
# mid-20s, American accent. <timbre>. <pace>. <manner>.", as samples/gen_narrators.py found).
_PODCAST = "Narrating a science podcast for a curious listener."
PRESETS = [
    {"id": "clear-female", "name": "Clear female, measured", "engine": "breeze", "seed": 42,
     "key": "described-narrator-a-seed42", "default": True,
     "about": "A woman in her thirties, American; the voice every episode starts in.",
     "instruction": ("A warm, clear woman in her thirties with a neutral American accent, "
                     "narrating a science podcast: measured pace, natural and engaged, "
                     "explaining a technical idea to a curious listener.")},
    {"id": "bright-female", "name": "Bright female, conversational", "engine": "breeze", "seed": 42,
     "key": "preset-bright-female-s42",
     "about": "Younger and brighter, American, like talking to a friend.",
     "instruction": ("Young adult female, early 20s, neutral American accent. Bright, warm, clear "
                     "voice, mid-range pitch. Friendly and sincere, natural conversational delivery, "
                     "steady moderate pace, even intonation. " + _PODCAST)},
    {"id": "british-female", "name": "British female, composed", "engine": "breeze", "seed": 42,
     "key": "preset-british-female-s42",
     "about": "A British woman in her twenties, articulate and calm.",
     "instruction": ("Young adult female, mid-20s, standard British RP accent. Clear, warm, smooth "
                     "voice, mid-range pitch. Articulate and composed, natural conversational "
                     "delivery, steady moderate pace. " + _PODCAST)},
    {"id": "warm-male", "name": "Warm male", "engine": "breeze", "seed": 42,
     "key": "preset-warm-male-s42",
     "about": "A man in his thirties, American, relaxed and warm.",
     "instruction": ("Adult male, mid-30s, neutral American accent. Warm, clear, resonant voice, "
                     "medium-low pitch. Relaxed and engaged, natural conversational delivery, steady "
                     "moderate pace, clear articulation. " + _PODCAST)},
    {"id": "calm-male", "name": "Calm male, British", "engine": "breeze", "seed": 42,
     "key": "preset-calm-male-s42",
     "about": "An older British man, unhurried and precise.",
     "instruction": ("Adult male, mid-40s, standard British RP accent. Calm, clear, smooth voice, "
                     "low-mid pitch. Composed and thoughtful, unhurried steady pace, precise "
                     "articulation. " + _PODCAST)},
    {"id": "anime-female", "name": "Anime heroine (for fun)", "engine": "breeze", "seed": 42,
     "key": "preset-anime-female-s42",
     "about": "An over-the-top anime heroine, far too excited about every equation. A joke voice (Leo, 2026-09-29).",
     "instruction": ("Young adult female, late teens, Japanese anime heroine speaking English with a light "
                     "Japanese accent. High-pitched, bright, sweet and very cute voice. Bubbly and wildly "
                     "enthusiastic, dramatic and expressive delivery, excited rising intonation, lively fast "
                     "pace, as if every result is the most amazing thing ever. " + _PODCAST)},
    {"id": "basic-female", "name": "Basic female (CPU)", "engine": "kokoro", "cpu": True,
     "key": "af_heart",
     "about": "Kokoro on the CPU: plainer, but made in minutes without waiting for the GPU."},
]
PRESET_BY_ID = {p["id"]: p for p in PRESETS}
PRESET_BY_KEY = {p["key"]: p for p in PRESETS}
DEFAULT_ID = next(p["id"] for p in PRESETS if p.get("default"))

SCHEMA_USER = """
CREATE TABLE IF NOT EXISTS user_voice (
  user_id INTEGER PRIMARY KEY REFERENCES users(id),
  voice TEXT NOT NULL,
  updated_at TEXT NOT NULL
)
"""
SCHEMA = """
CREATE TABLE IF NOT EXISTS episode_voice (
  episode_id TEXT PRIMARY KEY REFERENCES episodes(id),
  voice TEXT,
  rev INTEGER NOT NULL DEFAULT 1,
  prev_duration_s REAL,
  want TEXT,
  want_by INTEGER REFERENCES users(id),
  want_at TEXT,
  error TEXT,
  updated_at TEXT NOT NULL
)
"""

# ---------------------------------------------------------------- schema

_ready: set = set()
_ready_lock = threading.Lock()


def ensure_schema() -> None:
    key = str(db._path)
    if key in _ready:
        return
    with _ready_lock:
        if key in _ready:
            return
        c = db.conn()
        c.execute(SCHEMA)
        c.execute(SCHEMA_USER)
        if not c.in_transaction:
            _ready.add(key)


def start(cfg) -> None:
    """At the hub's start: the table, and the voice of episodes the worker voiced before this
    module existed (its job dir's status.json names the voice)."""
    ensure_schema()
    try:
        _infer(cfg)
    except Exception:
        log.exception("inferring the voice of earlier episodes")


def _infer(cfg) -> None:
    c = db.conn()
    rows = c.execute("SELECT e.id FROM episodes e LEFT JOIN episode_voice ev ON ev.episode_id = e.id "
                     "WHERE ev.episode_id IS NULL AND e.state = 'ready'").fetchall()
    found = []
    for r in rows:
        try:
            st = json.loads((cfg.episodes / r["id"] / "voice" / "status.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        p = PRESET_BY_KEY.get(((st or {}).get("output") or {}).get("voice") or "")
        if p:
            found.append((r["id"], p["id"]))
    if found:
        with db.transaction() as t:
            for eid, vid in found:
                t.execute("INSERT OR IGNORE INTO episode_voice (episode_id, voice, rev, updated_at) "
                          "VALUES (?, ?, 1, ?)", (eid, vid, db.now()))
        log.info("voice of %d earlier episodes read from their job dirs", len(found))


# ---------------------------------------------------------------- small helpers

def _u(req, k):
    try:
        return req.user[k]
    except (KeyError, IndexError, TypeError):
        return None


def _name(vid) -> str | None:
    p = PRESET_BY_ID.get(vid or "")
    return p["name"] if p else None


def _row(c, eid):
    return c.execute("SELECT * FROM episode_voice WHERE episode_id = ?", (eid,)).fetchone()


def _now_ms() -> str:
    d = datetime.now(timezone.utc)
    return d.strftime("%Y-%m-%dT%H:%M:%S.") + f"{d.microsecond // 1000:03d}Z"


def _write_json(path: Path, obj) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def for_claim(p: dict) -> dict:
    """What the worker gets in a claim: the preset, and papercast-voice's job.json `voice`."""
    spec = {"engine": p["engine"], "voice": p["key"], "id": p["id"]}
    for k in ("instruction", "seed"):
        if k in p:
            spec[k] = p[k]
    return {"id": p["id"], "name": p["name"], "cpu": bool(p.get("cpu")), "spec": spec}


def preset_view(p: dict, cfg) -> dict:
    f = cfg.data / "voices" / f"{p['id']}.mp3"
    try:
        v = int(f.stat().st_mtime) if f.is_file() and not f.is_symlink() else None
    except OSError:
        v = None
    return {"id": p["id"], "name": p["name"], "about": p.get("about", ""), "engine": p["engine"],
            "cpu": bool(p.get("cpu")), "default": bool(p.get("default")),
            "sample": f"/api/voices/{p['id']}/sample.mp3?v={v}" if v else None}


# ---------------------------------------------------------------- timings

def validate_timings(doc) -> dict:
    """The timings as the hub keeps them, or HTTPError 400: version 1, segments in order, each
    {start, end, text} with 0 <= start <= end (seconds), text a sentence."""
    if not isinstance(doc, dict) or doc.get("version") != 1:
        raise HTTPError(400, "bad_timings", "timings must be {\"version\": 1, \"segments\": [...]}")
    segs = doc.get("segments")
    if not isinstance(segs, list) or not segs or len(segs) > TIMINGS_MAX_SEGMENTS:
        raise HTTPError(400, "bad_timings", "segments must be a non-empty list")
    out, last = [], 0.0
    num = lambda v: isinstance(v, (int, float)) and not isinstance(v, bool) and v == v   # noqa: E731
    for i, s in enumerate(segs):
        if not isinstance(s, dict) or not num(s.get("start")) or not num(s.get("end")):
            raise HTTPError(400, "bad_timings", f"segment {i} needs a start and an end in seconds")
        a, b, t = float(s["start"]), float(s["end"]), s.get("text")
        if not isinstance(t, str) or not t.strip() or len(t) > TEXT_MAX:
            raise HTTPError(400, "bad_timings", f"segment {i} needs its text")
        if not (0 <= a <= b <= MAX_DURATION_S) or a < last - 0.001:
            raise HTTPError(400, "bad_timings", f"segment {i}: times out of order ({a} to {b})")
        last = a
        out.append({"start": round(a, 3), "end": round(b, 3), "text": " ".join(t.split())})
    d = doc.get("duration_s")
    return {"version": 1, "duration_s": round(float(d), 3) if num(d) and d > 0 else None, "segments": out}


def fits(doc: dict, duration_s) -> bool:
    """The timings belong to audio this long (their last sentence ends inside it)."""
    if not duration_s:
        return True
    return doc["segments"][-1]["end"] <= float(duration_s) + 1.5


def store_current(cfg, eid: str, doc: dict) -> Path:
    """Timings for the audio the episode has now (the backfill): timings.json."""
    p = cfg.episodes / eid / "timings.json"
    _write_json(p, doc)
    return p


def save_timings(req, c, eid: str, doc, job) -> str:
    """PUT /api/voice/<id>/timings (voiceq.py): for the audio this claim is making, kept until
    that audio arrives ("next"); else, when the episode has audio, for that audio ("current")."""
    doc = validate_timings(doc)
    d = req.cfg.episodes / eid
    if job["state"] == "claimed":
        d.mkdir(parents=True, exist_ok=True)
        _write_json(d / "timings.next.json", doc)
        return "next"
    e = c.execute("SELECT state, duration_s FROM episodes WHERE id = ?", (eid,)).fetchone()
    if e is None or e["state"] != "ready" or not (d / "audio.mp3").is_file():
        raise HTTPError(409, "no_audio", f"{eid} is neither being voiced nor has audio")
    if not fits(doc, e["duration_s"]):
        raise HTTPError(409, "wrong_audio", f"the timings run past the end of {eid}'s audio "
                                              f"({e['duration_s']} s): they are not for it")
    store_current(req.cfg, eid, doc)
    return "current"


def remap(s: float, old: dict | None, new: dict | None, old_dur, new_dur) -> float:
    """Second s of the old audio -> the same place in the new one: the same sentence, as far
    into it, when both have timings of the same sentences; else in proportion to the lengths."""
    s = max(0.0, float(s))
    old_dur, new_dur = float(old_dur or 0), float(new_dur or 0)
    if old_dur and s >= old_dur - 2:                    # finished stays finished
        return round(new_dur or s, 1)
    so = (old or {}).get("segments") or []
    sn = (new or {}).get("segments") or []
    if so and len(so) == len(sn) and all(a["text"] == b["text"] for a, b in zip(so, sn)):
        if s <= so[0]["start"]:
            t = s * (sn[0]["start"] / so[0]["start"]) if so[0]["start"] > 0 else 0.0
        else:
            i = max(k for k in range(len(so)) if so[k]["start"] <= s)
            a, b = so[i]["start"], so[i]["end"]
            f = min(1.0, (s - a) / (b - a)) if b > a else 0.0
            t = sn[i]["start"] + f * (sn[i]["end"] - sn[i]["start"])
    elif old_dur and new_dur:
        t = s * new_dur / old_dur
    else:
        t = s
    return round(max(0.0, min(t, new_dur) if new_dur else t), 1)


# ---------------------------------------------------------------- hooks for voiceq.py

def revoicing(c, eid: str) -> bool:
    """This job remakes audio the episode already has (a voice change): the episode stays
    ready, with its old audio, until the new one lands."""
    ensure_schema()
    return c.execute("SELECT 1 FROM episode_voice ev JOIN episodes e ON e.id = ev.episode_id "
                     "WHERE ev.episode_id = ? AND ev.want IS NOT NULL AND e.state = 'ready'",
                     (eid,)).fetchone() is not None


def mine(c, uid) -> str:
    """The preset this person's new versions are voiced in."""
    r = c.execute("SELECT voice FROM user_voice WHERE user_id = ?", (uid,)).fetchone() if uid is not None else None
    return r["voice"] if r and r["voice"] in PRESET_BY_ID else DEFAULT_ID


def _claim_preset(c, eid: str):
    r = _row(c, eid)
    if r and r["want"] in PRESET_BY_ID:                         # a voice change
        return PRESET_BY_ID[r["want"]]
    e = c.execute("SELECT made_by FROM episodes WHERE id = ?", (eid,)).fetchone()
    return PRESET_BY_ID[mine(c, e["made_by"] if e else None)]  # a new episode: its maker's voice


def claim_voice(c, eid: str) -> dict | None:
    """The voice a claim is for: the change asked for, else the maker's own voice; None when that
    is the default (papercast-voice's own, so nothing needs passing)."""
    ensure_schema()
    p = _claim_preset(c, eid)
    return None if p["id"] == DEFAULT_ID else for_claim(p)


def after_audio(c, cfg, eid: str, duration_s: float, key: str | None, had_audio: bool):
    """The MP3 has landed (inside voiceq.audio's transaction, before the episode row changes):
    which voice it is in, its timings moved in, the revision bumped and positions carried over
    when it replaced audio. Returns the event to publish after the commit, or None."""
    ensure_schema()
    r = _row(c, eid)
    e = c.execute("SELECT paper_id, duration_s FROM episodes WHERE id = ?", (eid,)).fetchone()
    if key:
        p = PRESET_BY_KEY.get(key)
        vid = p["id"] if p else None
    else:                               # a worker that does not say: what the claim asked for
        vid = _claim_preset(c, eid)["id"]
    old_rev = r["rev"] if r else 1
    rev = old_rev + 1 if had_audio else old_rev
    d = cfg.episodes / eid
    cur, nxt, prev = d / "timings.json", d / "timings.next.json", d / "timings.prev.json"
    old_doc = _read_json(cur) if had_audio else None
    if had_audio and cur.is_file():
        os.replace(cur, prev)
    else:
        prev.unlink(missing_ok=True)
        cur.unlink(missing_ok=True)
    new_doc = None
    if nxt.is_file():
        doc = _read_json(nxt)
        try:
            doc = validate_timings(doc)
            if fits(doc, duration_s):
                new_doc = doc
        except HTTPError:
            pass
        if new_doc is not None:
            os.replace(nxt, cur)
        else:
            log.warning("%s: the timings sent do not fit the audio; dropped", eid)
            nxt.unlink(missing_ok=True)
    now = db.now()
    old_dur = e["duration_s"] if e else None
    if had_audio:
        at = _now_ms()
        for p in c.execute("SELECT user_id, seconds FROM positions WHERE episode_id = ?", (eid,)).fetchall():
            c.execute("UPDATE positions SET seconds = ?, updated_at = ? WHERE user_id = ? AND episode_id = ?",
                      (remap(p["seconds"], old_doc, new_doc, old_dur, duration_s), at, p["user_id"], eid))
    c.execute("INSERT INTO episode_voice (episode_id, voice, rev, prev_duration_s, updated_at) VALUES (?, ?, ?, ?, ?) "
              "ON CONFLICT(episode_id) DO UPDATE SET voice = excluded.voice, rev = excluded.rev, "
              "prev_duration_s = excluded.prev_duration_s, want = NULL, want_by = NULL, want_at = NULL, "
              "error = NULL, updated_at = excluded.updated_at",
              (eid, vid, rev, old_dur if had_audio else None, now))
    if not had_audio or e is None:
        return None
    return {"id": eid, "episode_id": eid, "paper_id": e["paper_id"],
            "voice_swap": {"rev": rev, "from_rev": old_rev, "voice": vid, "name": _name(vid)}}


def revoice_failed(c, eid: str, error: str, retry: bool) -> None:
    """A voice change failed (inside voiceq.failed's transaction). While it will be tried again
    the change stays asked for; the last try leaves the episode as it was, with why."""
    if retry:
        return
    c.execute("UPDATE episode_voice SET error = ?, want = NULL, want_by = NULL, want_at = NULL, updated_at = ? "
              "WHERE episode_id = ?", (f"{_name(_want(c, eid)) or 'the new voice'}: {error}"[:600], db.now(), eid))
    c.execute("UPDATE voice_jobs SET state = 'done', phase = 'done', progress = 1 WHERE episode_id = ?", (eid,))


def _want(c, eid):
    r = _row(c, eid)
    return r["want"] if r else None


def publish(ev) -> None:
    if ev:
        events.publish("episode", ev)


# ---------------------------------------------------------------- the page: views

def _pending(r, job, positions: dict) -> dict | None:
    if not r or not r["want"] or job is None or job["state"] not in ("queued", "claimed", "failed"):
        return None
    working = job["state"] == "claimed"
    return {"id": r["want"], "name": _name(r["want"]), "by": r["want_by"], "at": r["want_at"],
            "state": "working" if working else ("retrying" if job["state"] == "failed" else "queued"),
            "position": positions.get(r["episode_id"]), "phase": job["phase"] if working else None,
            "progress": job["progress"] if working else None, "attempts": job["attempts"]}


def _view(r, job, can_change: bool, positions: dict, epdir: Path | None = None) -> dict:
    vid = r["voice"] if r else None
    out = {"id": vid, "name": _name(vid), "rev": r["rev"] if r else 1,
           "pending": _pending(r, job, positions), "error": r["error"] if r else None,
           "can_change": can_change}
    if epdir is not None:
        out["timings"] = (epdir / "timings.json").is_file()
    return out


def decorate(papers: list, cfg, uid, admin: bool) -> None:
    """web.library(): each episode gets `voice` {id, name, rev, pending, error, can_change,
    timings}. One read of this table for the whole library."""
    ensure_schema()
    c = db.conn()
    rows = {r["episode_id"]: r for r in c.execute("SELECT * FROM episode_voice")}
    jobs = {}
    if any(r["want"] for r in rows.values()):
        jobs = {j["episode_id"]: j for j in c.execute(
            "SELECT episode_id, state, phase, progress, attempts FROM voice_jobs WHERE episode_id IN "
            "(SELECT episode_id FROM episode_voice WHERE want IS NOT NULL)")}
    positions = {}
    if jobs:
        from . import voiceq
        positions = voiceq.queue_positions()
    for p in papers:
        for e in p.get("episodes") or []:
            r = rows.get(e["id"])
            e["voice"] = _view(r, jobs.get(e["id"]), bool(e.get("has_audio") and (admin or e.get("mine"))),
                               positions, cfg.episodes / e["id"])


def _episode(c, eid):
    return c.execute("SELECT id, paper_id, made_by, state, deleted_at, duration_s FROM episodes WHERE id = ?",
                     (eid,)).fetchone()


def _episode_view(req, c, eid) -> dict:
    e = _episode(c, eid)
    r = _row(c, eid)
    job = c.execute("SELECT episode_id, state, phase, progress, attempts FROM voice_jobs WHERE episode_id = ?",
                    (eid,)).fetchone()
    has_audio = e["state"] == "ready" and (req.cfg.episodes / eid / "audio.mp3").is_file()
    positions = {}
    if r and r["want"] and job and job["state"] in ("queued", "failed"):
        from . import voiceq
        positions = voiceq.queue_positions()
    mine = e["made_by"] == _u(req, "id")
    v = _view(r, job, bool(has_audio and (mine or _u(req, "role") == "admin")), positions, req.cfg.episodes / eid)
    v.update(episode_id=eid, paper_id=e["paper_id"], duration_s=e["duration_s"])
    return v


# ---------------------------------------------------------------- routes

def get_voices(req):
    ensure_schema()
    req.send_json(200, {"voices": [preset_view(p, req.cfg) for p in PRESETS], "default": DEFAULT_ID,
                        "mine": mine(db.conn(), _u(req, "id")), "max_waiting": MAX_WAITING})


def put_mine(req):
    """The voice my new versions are voiced in (their claims carry it)."""
    _mutation(req)
    vid = req.json().get("voice")
    if not isinstance(vid, str) or vid not in PRESET_BY_ID:
        raise HTTPError(400, "no_such_voice", "pick one of the voices in the list")
    ensure_schema()
    db.conn().execute("INSERT INTO user_voice (user_id, voice, updated_at) VALUES (?, ?, ?) ON CONFLICT(user_id) "
                      "DO UPDATE SET voice = excluded.voice, updated_at = excluded.updated_at",
                      (_u(req, "id"), vid, db.now()))
    req.send_json(200, {"mine": vid, "name": _name(vid)})


def get_sample(req, vid):
    f = req.cfg.data / "voices" / f"{vid}.mp3"
    if vid not in PRESET_BY_ID or f.is_symlink() or not f.is_file():
        raise HTTPError(404, "not_found", "no sample for this voice yet")
    req.send_file(f, "audio/mpeg", {"Cache-Control": "private, max-age=86400",
                                    "Cross-Origin-Resource-Policy": "same-origin"})


def _live(c, eid):
    ensure_schema()
    e = _episode(c, eid)
    if not e or e["deleted_at"] or e["state"] == "rejected":
        raise HTTPError(404, "not_found", "no such episode")
    return e


def get_episode_voice(req, eid):
    """The episode's voice; with ?at=<s>&rev=<n>, also `at`: where second s of audio revision n
    is in the audio there is now (the page that was playing the old one carries on from there)."""
    c = db.conn()
    _live(c, eid)
    v = _episode_view(req, c, eid)
    at, rev = req.arg("at"), req.arg("rev")
    if at is not None:
        try:
            s, n = float(at), int(rev or v["rev"])
        except ValueError:
            raise HTTPError(400, "bad_at", "at is seconds, rev a whole number")
        if n == v["rev"] - 1:
            d = req.cfg.episodes / eid
            r = _row(c, eid)
            s = remap(s, _read_json(d / "timings.prev.json"), _read_json(d / "timings.json"),
                      r["prev_duration_s"] if r else None, v["duration_s"])
        v["at"] = s
    req.send_json(200, v)


def get_timings(req, eid):
    """The sentence timings of the episode's audio (timings.json, SPEC section 9), with `rev`,
    the revision of the audio they belong to (the page's /audio/<id>.mp3?v=<rev>)."""
    c = db.conn()
    _live(c, eid)
    doc = _read_json(req.cfg.episodes / eid / "timings.json")
    if not isinstance(doc, dict) or not doc.get("segments"):
        raise HTTPError(404, "no_timings", "there are no timings for this episode's audio")
    r = _row(c, eid)
    doc["rev"] = r["rev"] if r else 1
    req.send_json(200, doc)


def _mutation(req):
    from . import web
    web._mutation(req)


def _outstanding(c, uid, eid) -> int:
    return c.execute("SELECT COUNT(*) FROM episode_voice ev JOIN voice_jobs v ON v.episode_id = ev.episode_id "
                     "JOIN episodes e ON e.id = ev.episode_id WHERE ev.want IS NOT NULL AND ev.want_by = ? "
                     "AND ev.episode_id != ? AND e.deleted_at IS NULL AND v.state IN ('queued', 'claimed', 'failed')",
                     (uid, eid)).fetchone()[0]


def _back_to_done(c, eid) -> None:
    c.execute("UPDATE voice_jobs SET state = 'done', phase = 'done', progress = 1, error = NULL, worker = NULL "
              "WHERE episode_id = ? AND state IN ('queued', 'failed')", (eid,))
    c.execute("UPDATE episode_voice SET want = NULL, want_by = NULL, want_at = NULL, updated_at = ? "
              "WHERE episode_id = ?", (db.now(), eid))


def put_episode_voice(req, eid):
    _mutation(req)
    vid = req.json().get("voice")
    p = PRESET_BY_ID.get(vid) if isinstance(vid, str) else None
    if p is None:
        raise HTTPError(400, "no_such_voice", "pick one of the voices in the list")
    uid, admin = _u(req, "id"), _u(req, "role") == "admin"
    from . import voiceq
    now = db.now()
    with db.transaction() as c:
        e = _live(c, eid)
        if e["made_by"] != uid and not admin:
            raise HTTPError(403, "not_yours", "only its maker or an admin can change its voice")
        d = req.cfg.episodes / eid
        if e["state"] != "ready" or not (d / "audio.mp3").is_file():
            raise HTTPError(409, "not_ready", "the voice can be changed once the episode has its audio")
        if not (d / "script.md").is_file():
            raise HTTPError(409, "no_script", "the hub has no script for this episode to voice again")
        voiceq.ensure_columns(c)
        r = _row(c, eid)
        job = c.execute("SELECT * FROM voice_jobs WHERE episode_id = ?", (eid,)).fetchone()
        pending = bool(r and r["want"] and job and job["state"] in ("queued", "claimed", "failed"))
        if pending and job["state"] == "claimed":
            raise HTTPError(409, "busy", f"it is being recorded in {_name(r['want'])} now; "
                                         "change it again once that is done")
        if (r["voice"] if r else None) == vid:
            if pending:                                 # back to the voice it has: nothing to make
                _back_to_done(c, eid)
        elif pending and r["want"] == vid:
            pass
        else:
            if (not pending or r["want_by"] != uid) and _outstanding(c, uid, eid) >= MAX_WAITING:
                raise HTTPError(429, "too_many", f"you have {MAX_WAITING} voice changes waiting; "
                                                 "wait for one to finish")
            if r is None:
                c.execute("INSERT INTO episode_voice (episode_id, voice, rev, updated_at) VALUES (?, NULL, 1, ?)",
                          (eid, now))
            c.execute("UPDATE episode_voice SET want = ?, want_by = ?, want_at = ?, error = NULL, updated_at = ? "
                      "WHERE episode_id = ?", (vid, uid, now, now, eid))
            if job is None:
                c.execute("INSERT INTO voice_jobs (episode_id, user_id, state, queued_at, attempts) "
                          "VALUES (?, ?, 'queued', ?, 0)", (eid, uid, now))
            elif pending:                               # another voice, same place in the line
                c.execute("UPDATE voice_jobs SET user_id = ?, state = 'queued', attempts = 0, error = NULL "
                          "WHERE episode_id = ?", (uid, eid))
            else:
                c.execute("UPDATE voice_jobs SET user_id = ?, state = 'queued', queued_at = ?, claimed_at = NULL, "
                          "heartbeat_at = NULL, finished_at = NULL, phase = NULL, progress = NULL, attempts = 0, "
                          "error = NULL, worker = NULL WHERE episode_id = ?", (uid, now, eid))
            (d / "timings.next.json").unlink(missing_ok=True)
    events.publish("episode", {"id": eid, "episode_id": eid, "paper_id": e["paper_id"], "why": "voice"})
    req.send_json(200, _episode_view(req, db.conn(), eid))


def delete_episode_voice(req, eid):
    _mutation(req)
    uid, admin = _u(req, "id"), _u(req, "role") == "admin"
    with db.transaction() as c:
        e = _live(c, eid)
        r = _row(c, eid)
        if e["made_by"] != uid and not admin and not (r and r["want_by"] == uid):
            raise HTTPError(403, "not_yours", "only its maker or an admin can change its voice")
        job = c.execute("SELECT state FROM voice_jobs WHERE episode_id = ?", (eid,)).fetchone()
        if r and r["want"] and job and job["state"] == "claimed":
            raise HTTPError(409, "busy", "it is being recorded now; it can no longer be taken back")
        if r and r["want"] and job and job["state"] in ("queued", "failed"):
            _back_to_done(c, eid)
        elif r and r["error"]:
            c.execute("UPDATE episode_voice SET error = NULL WHERE episode_id = ?", (eid,))
    events.publish("episode", {"id": eid, "episode_id": eid, "paper_id": e["paper_id"], "why": "voice"})
    req.send_json(200, _episode_view(req, db.conn(), eid))


ROUTES = [
    ("GET", r"^/api/voices$", get_voices, "viewer"),
    ("PUT", r"^/api/voices/mine$", put_mine, "viewer"),
    ("GET", rf"^/api/voices/{VID}/sample\.mp3$", get_sample, "viewer"),
    ("GET", rf"^/api/episodes/{EID}/voice$", get_episode_voice, "viewer"),
    ("PUT", rf"^/api/episodes/{EID}/voice$", put_episode_voice, "viewer"),
    ("DELETE", rf"^/api/episodes/{EID}/voice$", delete_episode_voice, "viewer"),
]
