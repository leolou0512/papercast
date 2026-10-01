"""A version's script replaced in place, and voiced again (`papercast replace-script`).

    PUT /api/cli/episodes/<id>/script   {"script": text, "explainer_html": text, "explainer_json":
                                        text or object, "dry_run": bool}: its maker or an admin
    GET /api/cli/episodes/<id>/script   the script, the change waiting for its voice, every change;
                                        ?before=<change id>: the files that change replaced (undo)

The new script goes through the upload's own checks (contrib.check_episode_files: the base
prompt the version was made with, its maker's name), with the explainer it will have (the one
sent, else the one there is), and is refused with the reasons when they fail (422
checks_failed). A dry run answers what the checks say and what would happen, and changes
nothing. A script the version already has is no change (`changed`: false), so sending one twice
is harmless. Then, by where the version is:

- not voiced yet (waiting for the voice, or its voicing failed): the new files take the old
  ones' place at once, and the voice job keeps its place in the queue (a failed one starts its
  attempts afresh);
- voiced: it is voiced again in the voice it is in, by "Change voice"'s path (voices.py): the
  voice job goes back into the fair queue under the person who asked, and the audio, its
  timings, the script and the explainer there are play on. The new files wait beside them
  (script.next.md, explainer.next.html, explainer.next.json); the worker is given
  script.next.md (voiceq.script); when the new MP3 lands, everything moves at once
  (voices.after_audio -> landing()): the audio, its timings, the script, the explainer. A saved
  position keeps its second, one past the new end goes to the end, and finished stays finished
  (clamp). If the voice fails for good, the new script is dropped (dropped()) and the version
  stays as it was. A second replacement before the first is voiced takes its place in line;
- being voiced right now (its voice job claimed): refused, 409 busy; send it again afterwards;
- being checked, or rejected: refused, 409.

Comments, Listened, positions, listening stats, links and Up next are the version's or the
paper's and are left alone. Each replacement is a row of script_changes (db.py migration 6),
the episode's script history; the files it replaced are kept in history/<change id>/ in the
episode's directory. episodes.script_rev counts the scripts in place: the search indexes the
version again when it moves (search._sig_e). Every change publishes the usual episode event
(why "script"); the swap's carries voice_swap with "script": true."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import tempfile
from pathlib import Path

from . import contrib, db, events, voiceq, voices
from .app import HTTPError

log = logging.getLogger("pcg.scriptswap")

MB = 1024 * 1024
BODY_MAX = 40 * MB                  # a script, and two explainers of at most 8 MB each, as JSON
# The files a change may bring, and where each waits for the new audio.
NEXT = {"script.md": "script.next.md", "explainer.html": "explainer.next.html",
        "explainer.json": "explainer.next.json"}
FIELD = {"script.md": "script", "explainer.html": "explainer_html", "explainer.json": "explainer_json"}
_ID = r"([A-Za-z0-9_-]{1,64})"


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _write(path: Path, data: bytes) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def _keep(src: Path, dst: Path) -> None:
    """A copy of `src` at `dst` (a hard link when it can), so `src` can be replaced in one step."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.unlink(missing_ok=True)
    try:
        os.link(src, dst)
    except OSError:
        shutil.copyfile(src, dst)


def _read(path: Path):
    try:
        return path.read_bytes() if path.is_file() and not path.is_symlink() else None
    except OSError:
        return None


# ---------------------------------------------------------------- the request

def _body(req) -> tuple:
    """({stored name: bytes or None}, dry_run) from the JSON body."""
    try:
        b = json.loads(req.body(BODY_MAX) or b"{}")
    except ValueError:
        raise HTTPError(400, "bad_json", "the body is not JSON")
    if not isinstance(b, dict):
        raise HTTPError(400, "bad_json", "the body must be a JSON object")
    s = b.get("script")
    if not isinstance(s, str) or not s.strip():
        raise HTTPError(400, "no_script", "send the new script.md's text as \"script\"")
    new = {"script.md": s.encode("utf-8")}
    h = b.get("explainer_html")
    if h is not None and not isinstance(h, str):
        raise HTTPError(400, "bad_explainer", "explainer_html is the explainer.html file's text")
    new["explainer.html"] = h.encode("utf-8") if h is not None else None
    j = b.get("explainer_json")
    if j is not None and not isinstance(j, str):
        j = json.dumps(j, ensure_ascii=False)
    new["explainer.json"] = j.encode("utf-8") if j is not None else None
    return new, b.get("dry_run") is True


def _episode(c, eid: str):
    e = c.execute("SELECT * FROM episodes WHERE id = ?", (eid,)).fetchone()
    if e is None or e["deleted_at"]:
        raise HTTPError(404, "no_such_episode", f"no episode {eid}")
    return e


def _job(c, eid: str):
    return c.execute("SELECT * FROM voice_jobs WHERE episode_id = ?", (eid,)).fetchone()


def waiting(c, eid: str):
    """The change waiting for this version's new audio, or None."""
    return c.execute("SELECT * FROM script_changes WHERE episode_id = ? AND state = 'waiting' "
                     "ORDER BY id DESC LIMIT 1", (eid,)).fetchone()


def _how(cfg, e, job) -> str:
    """"queue" (not voiced yet) or "revoice" (voiced), or HTTPError: what a change does now."""
    if e["state"] == "checking":
        raise HTTPError(409, "checking", "the hub is still checking this version; send it again in a minute")
    if e["state"] == "rejected":
        raise HTTPError(409, "rejected", "this version was rejected by the hub's checks; upload a new version instead")
    if job is not None and job["state"] == "claimed":
        raise HTTPError(409, "busy", "it is being voiced right now; send the script again once that is done",
                        retry=True)
    if e["state"] == "ready" and (cfg.episodes / e["id"] / "audio.mp3").is_file():
        return "revoice"
    if e["state"] in ("waiting-for-gpu", "failed") and job is not None:
        return "queue"
    raise HTTPError(409, "not_ready", f"this version is {e['state']} with no voice job; its script cannot be replaced now")


def _effective(cfg, c, eid: str, name: str, how: str) -> Path:
    """The file a version will have once what is waiting lands: the waiting one, else the live one."""
    d = cfg.episodes / eid
    if how == "revoice" and waiting(c, eid) is not None and (d / NEXT[name]).is_file():
        return d / NEXT[name]
    return d / name


def _check(cfg, c, e, new: dict, how: str) -> tuple:
    """The upload's checks on the files the version would have."""
    tmp = cfg.data / "tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix="script-", dir=tmp))
    try:
        for name, data in new.items():
            if data is not None:
                (stage / name).write_bytes(data)
            else:
                src = _effective(cfg, c, e["id"], name, how)
                if src.is_file() and not src.is_symlink():
                    _keep(src, stage / name)
        return contrib.check_episode_files(c, e, stage)
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def _voice_of(c, eid: str) -> tuple:
    """(voice id, custom spec JSON or None): the voice its audio is in (the default when not known)."""
    r = voices._row(c, eid)
    vid = r["voice"] if r is not None and r["voice"] else voices.DEFAULT_ID
    spec = r["spec"] if r is not None and vid == voices.CUSTOM else None
    return vid, spec


def _voice_json(c, vid, eid) -> dict:
    if vid == voices.CUSTOM:
        e = c.execute("SELECT made_by FROM episodes WHERE id = ?", (eid,)).fetchone()
        r = voices._row(c, eid)
        sp = voices.load_spec(r["spec"]) if r is not None else None
        owner = sp["user_id"] if sp else (e["made_by"] if e else None)
        return {"id": vid, "name": voices.custom_label(owner, None, voices._users(c, [owner]).get(owner))}
    return {"id": vid, "name": voices._name(vid)}


def _plan(cfg, c, e, how: str) -> dict:
    """What a change would do now: how, the voice it is recorded in."""
    eid = e["id"]
    if how == "queue":
        return {"how": how, "voice": None}
    r = voices._row(c, eid)
    job = _job(c, eid)
    if r is not None and r["want"] and job is not None and job["state"] in ("queued", "failed"):
        vid = r["want"]                 # a voice change waits already: the new script goes with it
    else:
        vid = _voice_of(c, eid)[0]
    return {"how": how, "voice": _voice_json(c, vid, eid)}


def _nth(n) -> str:
    if not n:
        return ""
    suf = "th" if 11 <= n % 100 <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suf} in line"


def _message(out: dict) -> str:
    line = _nth(out.get("queue_position"))
    if out["how"] == "unchanged":
        return "it has this script already: nothing to do"
    dry = out.get("dry_run")
    if dry and out.get("problems"):
        n = len(out["problems"])
        return f"the hub's checks found {n} problem{'s' if n > 1 else ''}: it would be refused"
    pre = "the checks pass; " if dry else ""
    if out["how"] == "queue":
        return pre + ("the new script would take the old one's place" if dry else "the new script is in place") + \
            "; it keeps its place in the voice queue" + (f" ({line})" if line else "")
    v = (out.get("voice") or {}).get("name") or "its voice"
    return pre + f"it {'would be' if dry else 'will be'} voiced again in {v}" + (f", {line}" if line else "") + \
        "; the old audio plays until then"


def put_script(req, eid):
    new, dry = _body(req)
    uid = req.user["id"]
    admin = req.user["role"] == "admin"
    voices.ensure_schema()
    cfg = req.cfg
    c = db.conn()
    voiceq.ensure_columns(c)
    e = _episode(c, eid)
    if e["made_by"] != uid and not admin:
        raise HTTPError(403, "not_yours", "only its maker or an admin can replace its script")
    how = _how(cfg, e, _job(c, eid))
    problems, stats = _check(cfg, c, e, new, how)
    out = {"episode_id": eid, "paper_id": e["paper_id"], "dry_run": dry, "ok": not problems,
           "problems": problems, "words": stats.get("words"), "minutes": stats.get("minutes")}
    if problems and not dry:
        n = len(problems)
        raise HTTPError(422, "checks_failed", f"the hub's checks found {n} problem{'s' if n > 1 else ''}", **out)
    unchanged = all(data is None or _read(_effective(cfg, c, eid, name, how)) == data for name, data in new.items())
    if dry or unchanged:
        out.update(_plan(cfg, c, e, how), changed=False, state=e["state"],
                   queue_position=voiceq.queue_positions().get(eid))
        if unchanged:
            out["how"] = "unchanged"
        out["message"] = _message(out)
        req.send_json(200, out)
        return
    now = db.now()
    with db.transaction() as t:
        e = _episode(t, eid)
        job = _job(t, eid)
        how = _how(cfg, e, job)             # again, in the transaction: a claim may have come meanwhile
        cid = _apply(cfg, t, e, job, how, new, stats, uid, now)
        e = _episode(t, eid)
    out.update(_plan(cfg, db.conn(), e, how), changed=True, change_id=cid, state=e["state"],
               queue_position=voiceq.queue_positions().get(eid))
    out["message"] = _message(out)
    log.info("episode %s: script replaced by user %s (change %s, %s)", eid, uid, cid, how)
    events.publish("episode", {"id": eid, "episode_id": eid, "paper_id": e["paper_id"], "state": e["state"],
                               "state_detail": e["state_detail"], "made_by": e["made_by"], "why": "script"})
    req.send_json(200, out)


def _apply(cfg, t, e, job, how: str, new: dict, stats: dict, uid, now: str) -> int:
    """The change, inside the caller's transaction. Returns its id."""
    eid = e["id"]
    d = cfg.episodes / eid
    script = new["script.md"]
    prev = waiting(t, eid)
    brought = {k for k in ("explainer.html", "explainer.json") if new[k] is not None}
    if how == "revoice" and prev is not None:       # the explainer the change it replaces brought rides on
        brought |= {k for k in ("explainer.html", "explainer.json") if (d / NEXT[k]).is_file()}
    prev_bytes = _read(_effective(cfg, t, eid, "script.md", how))
    cid = t.execute("INSERT INTO script_changes (episode_id, by_user, at, state, how, explainer, words, est_minutes, "
                    "sha256, prev_sha256) VALUES (?, ?, ?, 'waiting', ?, ?, ?, ?, ?, ?)",
                    (eid, uid, now, how, ",".join(k.split(".")[1] for k in sorted(brought)), stats.get("words"),
                     stats.get("minutes"), _sha(script), _sha(prev_bytes) if prev_bytes is not None else None)).lastrowid
    if how == "queue":
        hist = d / "history" / str(cid)
        for name, data in new.items():
            if data is None:
                continue
            if (d / name).is_file():
                _keep(d / name, hist / name)
            _write(d / name, data)
        t.execute("UPDATE episodes SET script_rev = script_rev + 1, words = ?, est_minutes = ?, check_report = '[]', "
                  "updated_at = ? WHERE id = ?", (stats.get("words"), stats.get("minutes"), now, eid))
        if e["state"] == "failed":                  # tried afresh with the new script, in its place in line
            t.execute("UPDATE episodes SET state = 'waiting-for-gpu', state_detail = 'in the queue for the voice' "
                      "WHERE id = ?", (eid,))
        if job["state"] == "failed":
            t.execute("UPDATE voice_jobs SET state = 'queued', attempts = 0, error = NULL, phase = NULL, progress = NULL, "
                      "finished_at = NULL, worker = NULL WHERE episode_id = ?", (eid,))
        rev = t.execute("SELECT script_rev FROM episodes WHERE id = ?", (eid,)).fetchone()[0]
        t.execute("UPDATE script_changes SET state = 'done', script_rev = ?, done_at = ? WHERE id = ?", (rev, now, cid))
        return cid
    # voiced: the new files wait for the new audio
    if prev is not None:
        t.execute("UPDATE script_changes SET state = 'superseded', done_at = ? WHERE id = ?", (now, prev["id"]))
    else:
        for nxt in NEXT.values():                   # nothing waits: no leftovers ride along
            (d / nxt).unlink(missing_ok=True)
    for name, data in new.items():
        if data is not None:
            _write(d / NEXT[name], data)
    r = voices._row(t, eid)
    pending = bool(r is not None and r["want"] and job is not None and job["state"] in ("queued", "failed"))
    if pending:                                     # a voice change (or the change before) waits: same place
        vid = r["want"]
    else:
        vid, spec = _voice_of(t, eid)
        if r is None:
            t.execute("INSERT INTO episode_voice (episode_id, voice, rev, updated_at) VALUES (?, NULL, 1, ?)", (eid, now))
        t.execute("UPDATE episode_voice SET want = ?, want_by = ?, want_at = ?, want_spec = ?, error = NULL, "
                  "updated_at = ? WHERE episode_id = ?", (vid, uid, now, spec, now, eid))
        if job is None:
            t.execute("INSERT INTO voice_jobs (episode_id, user_id, state, queued_at, attempts) "
                      "VALUES (?, ?, 'queued', ?, 0)", (eid, uid, now))
        else:
            t.execute("UPDATE voice_jobs SET user_id = ?, state = 'queued', queued_at = ?, claimed_at = NULL, "
                      "heartbeat_at = NULL, finished_at = NULL, phase = NULL, progress = NULL, attempts = 0, "
                      "error = NULL, worker = NULL WHERE episode_id = ?", (uid, now, eid))
        (d / "timings.next.json").unlink(missing_ok=True)
    t.execute("UPDATE script_changes SET voice = ? WHERE id = ?", (vid, cid))
    t.execute("UPDATE episodes SET updated_at = ? WHERE id = ?", (now, eid))
    return cid


# ---------------------------------------------------------------- hooks (voiceq.py, voices.py)

def voice_script(cfg, c, eid: str) -> Path:
    """The script the worker voices: the one waiting for its new audio, else the episode's."""
    d = cfg.episodes / eid
    if (d / "script.next.md").is_file() and waiting(c, eid) is not None:
        return d / "script.next.md"
    return d / "script.md"


def landing(c, cfg, eid: str, audio_rev: int):
    """New audio has replaced old (inside voiceq.audio's transaction, voices.after_audio): the
    change waiting for it moves in with it, the files it replaced kept in history/<change id>/.
    Returns the change (a dict), or None when none waits."""
    ch = waiting(c, eid)
    if ch is None:
        return None
    d = cfg.episodes / eid
    now = db.now()
    if not (d / "script.next.md").is_file():
        log.warning("%s: script change %s lost its script.next.md; dropped", eid, ch["id"])
        c.execute("UPDATE script_changes SET state = 'failed', error = 'its new script was missing', done_at = ? "
                  "WHERE id = ?", (now, ch["id"]))
        return None
    hist = d / "history" / str(ch["id"])
    for live, nxt in NEXT.items():
        if (d / nxt).is_file():
            if (d / live).is_file():
                _keep(d / live, hist / live)
            os.replace(d / nxt, d / live)
    c.execute("UPDATE episodes SET script_rev = script_rev + 1, words = ?, est_minutes = ?, check_report = '[]' "
              "WHERE id = ?", (ch["words"], ch["est_minutes"], eid))
    rev = c.execute("SELECT script_rev FROM episodes WHERE id = ?", (eid,)).fetchone()[0]
    c.execute("UPDATE script_changes SET state = 'done', script_rev = ?, audio_rev = ?, done_at = ? WHERE id = ?",
              (rev, audio_rev, now, ch["id"]))
    log.info("episode %s: script change %s in place with audio revision %s", eid, ch["id"], audio_rev)
    return dict(ch, script_rev=rev, audio_rev=audio_rev)


def dropped(c, cfg, eid: str, error: str) -> None:
    """The voice failed for good on a re-voice (voices.revoice_failed): the script waiting for
    it is dropped, and the version stays as it was."""
    ch = waiting(c, eid)
    if ch is None:
        return
    c.execute("UPDATE script_changes SET state = 'failed', error = ?, done_at = ? WHERE id = ?",
              (f"the voice failed: {error}"[:600], db.now(), ch["id"]))
    if cfg is not None:
        for nxt in NEXT.values():
            (cfg.episodes / eid / nxt).unlink(missing_ok=True)
    log.info("episode %s: script change %s dropped (the voice failed)", eid, ch["id"])


def swapped_by_script(c, eid: str, audio_rev: int) -> bool:
    """Audio revision `audio_rev` came with a new script (positions are clamped, not remapped)."""
    return c.execute("SELECT 1 FROM script_changes WHERE episode_id = ? AND state = 'done' AND audio_rev = ?",
                     (eid, audio_rev)).fetchone() is not None


def clamp(s: float, old_dur, new_dur) -> float:
    """A saved position in audio of another script: the same second, at most the new end;
    finished stays finished."""
    s = max(0.0, float(s))
    old_dur, new_dur = float(old_dur or 0), float(new_dur or 0)
    if old_dur and s >= old_dur - 2:
        return round(new_dur or s, 1)
    return round(min(s, new_dur) if new_dur else s, 1)


# ---------------------------------------------------------------- GET: the script and its history

def _change_json(r) -> dict:
    return {"id": r["id"], "at": r["at"], "by": r["by_user"], "by_name": r["by_name"], "state": r["state"],
            "how": r["how"], "voice": r["voice"], "explainer": [x for x in (r["explainer"] or "").split(",") if x],
            "words": r["words"], "minutes": r["est_minutes"], "sha256": r["sha256"], "prev_sha256": r["prev_sha256"],
            "script_rev": r["script_rev"], "audio_rev": r["audio_rev"], "done_at": r["done_at"], "error": r["error"]}


def get_script(req, eid):
    """The version's script now (`script`, `sha256`, `script_rev`), the change waiting for its
    voice (`waiting`, with its script) and every change (`changes`, oldest first). ?before=<id>:
    the files change <id> replaced, as PUT takes them (`script`, and `explainer_html`/
    `explainer_json` when it replaced those)."""
    c = db.conn()
    e = _episode(c, eid)
    if e["state"] == "rejected" and e["made_by"] != req.user["id"] and req.user["role"] != "admin":
        raise HTTPError(404, "no_such_episode", f"no episode {eid}")      # as the library shows it
    d = req.cfg.episodes / eid
    before = req.arg("before")
    if before is not None:
        try:
            cid = int(before)
        except ValueError:
            raise HTTPError(400, "bad_arg", "before is a change id")
        ch = c.execute("SELECT * FROM script_changes WHERE id = ? AND episode_id = ?", (cid, eid)).fetchone()
        if ch is None or ch["state"] != "done":
            raise HTTPError(404, "no_such_change", f"no change {before} in place on {eid}")
        hist = d / "history" / str(cid)
        out = {"episode_id": eid, "change_id": cid}
        for name, field in FIELD.items():
            b = _read(hist / name)
            if b is not None:
                out[field] = b.decode("utf-8", "replace")
        if "script" not in out:
            raise HTTPError(404, "no_history", f"the script change {cid} replaced is not kept")
        req.send_json(200, out)
        return
    cur = _read(d / "script.md")
    rows = c.execute("SELECT s.*, u.name AS by_name FROM script_changes s LEFT JOIN users u ON u.id = s.by_user "
                     "WHERE s.episode_id = ? ORDER BY s.id", (eid,)).fetchall()
    w = next((r for r in rows if r["state"] == "waiting"), None)
    wait = None
    if w is not None:
        nb = _read(d / "script.next.md")
        wait = dict(_change_json(w), script=nb.decode("utf-8", "replace") if nb is not None else None)
    req.send_json(200, {"episode_id": eid, "state": e["state"], "script_rev": e["script_rev"],
                        "script": cur.decode("utf-8", "replace") if cur is not None else None,
                        "sha256": _sha(cur) if cur is not None else None, "words": e["words"],
                        "waiting": wait, "changes": [_change_json(r) for r in rows]})


ROUTES = [
    ("GET", rf"^/api/cli/episodes/{_ID}/script$", get_script, "cli"),
    ("PUT", rf"^/api/cli/episodes/{_ID}/script$", put_script, "cli-contributor"),
]
