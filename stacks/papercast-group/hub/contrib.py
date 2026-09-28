"""The CLI API: lookup, claims, bundle upload, checks on the hub (SPEC.md sections 4-6). Owner: A3.

An upload is streamed to $PCG_DATA/tmp, unpacked by hand (regular files and directories only:
no links, devices, absolute paths or `..`; at most 200 members and 60 MB unpacked), its manifest
checked with common.bundle.validate, and the named files copied into
$PCG_DATA/episodes/<episode_id>/ under their fixed names. The upload answers 201 at once; the
checks (section 6) run in a thread and move the episode to waiting-for-gpu (with its voice job)
or to rejected, with a report in plain words.

Paper identity (section 3), in order: arXiv id without version, DOI lowercased, sha256 of the PDF,
normalised title. Two sets of keys are the same paper when the first key both have agrees, so two
different arXiv ids are two papers even if their titles match. A new paper needs the uploader's
live claim on those keys; a new version needs an existing paper_id. A later upload fills a
paper's empty fields and never overwrites one."""
from __future__ import annotations

import gzip
import inspect
import json
import logging
import os
import re
import shutil
import tarfile
import tempfile
import threading
import zlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

from papercast_cli.common import bundle, checks, wording
from papercast_cli.common import prefs as P

from . import db, events, graph, voiceq
from .app import HTTPError

log = logging.getLogger("pcg.contrib")

MB = 1024 * 1024
BUNDLE_MAX = getattr(bundle, "MAX_BYTES", 50 * MB)
UNPACKED_MAX = 60 * MB
TAR_SLACK = 2 * MB              # tar headers and padding on top of the files themselves
MAX_MEMBERS = 200
MANIFEST_MAX = 1 * MB
SCRIPT_MAX = 60 * 1024
EXPLAINER_HTML_MAX = 8 * MB
EXPLAINER_JSON_MAX = 8 * MB
WPM = 150
DEFAULT_MINUTES = (15.0, 25.0)
CLAIM_S = 6 * 3600
CHUNK = 1 << 20
# Manifest file keys -> the names they are stored under (SPEC.md section 3).
FILES = {"script": "script.md", "explainer_json": "explainer.json",
         "explainer_html": "explainer.html", "claims": "claims.md"}
KEY_ORDER = ("arxiv_id", "doi", "source_sha256", "title_norm")

_lock = threading.Lock()
_inflight: set = set()          # episodes a check thread of this process is working on
_recovered: set = set()         # data dirs whose leftover `checking` episodes were picked up


def _at(seconds: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _uid(req) -> int:
    return req.user["id"]


def _user_json(u) -> dict:
    d = dict(u)
    return {k: d.get(k) for k in ("id", "email", "name", "role")}


# ---- paper identity

_ARXIV_NEW = re.compile(r"(\d{4}\.\d{4,5})(?:v\d+)?")
_ARXIV_OLD = re.compile(r"([a-z][a-z.\-]*/\d{7})(?:v\d+)?", re.I)


def norm_arxiv(v):
    """"arXiv:2210.02747v3", "https://arxiv.org/abs/2210.02747" -> "2210.02747"; None if not an id."""
    if not isinstance(v, str):
        return None
    s = v.strip()
    s = re.sub(r"^(?:https?://)?(?:www\.|export\.)?arxiv\.org/(?:abs|pdf)/", "", s, flags=re.I)
    s = re.sub(r"^arxiv:\s*", "", s, flags=re.I)
    s = re.sub(r"\.pdf$", "", s, flags=re.I)
    m = _ARXIV_NEW.fullmatch(s) or _ARXIV_OLD.fullmatch(s)
    return m.group(1).lower() if m else None


def norm_doi(v):
    if not isinstance(v, str):
        return None
    s = v.strip()
    s = re.sub(r"^(?:https?://)?(?:dx\.)?doi\.org/", "", s, flags=re.I)
    s = re.sub(r"^doi:\s*", "", s, flags=re.I).lower()
    return s if re.fullmatch(r"10\.\d{3,9}/\S+", s) else None


def norm_sha(v):
    if not isinstance(v, str):
        return None
    s = v.strip().lower()
    return s if re.fullmatch(r"[0-9a-f]{64}", s) else None


def identity(arxiv_id=None, doi=None, sha=None, title=None) -> dict:
    k = {"arxiv_id": norm_arxiv(arxiv_id), "doi": norm_doi(doi), "source_sha256": norm_sha(sha),
         "title_norm": db.norm_title(title) if isinstance(title, str) else None}
    return {a: b for a, b in k.items() if b}


def same_paper(a: dict, b: dict) -> bool:
    for k in KEY_ORDER:
        if a.get(k) and b.get(k):
            return a[k] == b[k]
    return False


def _row_keys(r) -> dict:
    return {k: r[k] for k in KEY_ORDER if r[k]}


def find_paper(c, keys: dict):
    for k in KEY_ORDER:
        if keys.get(k):
            for r in c.execute(f"SELECT * FROM papers WHERE {k} = ? ORDER BY created_at", (keys[k],)):
                if same_paper(_row_keys(r), keys):
                    return r
    return None


def _live_claims(c, keys: dict) -> list:
    conds = [f"{k} = ?" for k in KEY_ORDER if keys.get(k)]
    if not conds:
        return []
    args = [db.now()] + [keys[k] for k in KEY_ORDER if keys.get(k)]
    rows = c.execute("SELECT claims.*, users.name AS by_name FROM claims JOIN users ON users.id = claims.user_id "
                     f"WHERE done_at IS NULL AND expires_at > ? AND ({' OR '.join(conds)}) "
                     "ORDER BY claims.created_at", args).fetchall()
    return [r for r in rows if same_paper(_row_keys(r), keys)]


def paper_fields(p) -> dict:
    """The manifest's paper, cleaned: bad values are dropped rather than refused."""
    p = p if isinstance(p, dict) else {}
    title = " ".join(str(p.get("title") or "").split())[:500]
    authors = p.get("authors") if isinstance(p.get("authors"), list) else []
    authors = [" ".join(a.split())[:200] for a in authors if isinstance(a, str) and a.strip()][:100]
    year = p.get("year")
    year = year if isinstance(year, int) and not isinstance(year, bool) and 1000 <= year <= 2200 else None
    url = p.get("url")
    url = url.strip() if isinstance(url, str) and re.match(r"^https?://\S+$", url.strip()) and len(url) <= 2000 else None
    tags = []
    for t in p.get("tags") if isinstance(p.get("tags"), list) else []:
        t = " ".join(t.split())[:80] if isinstance(t, str) else ""
        if t and t not in tags:
            tags.append(t)
    s2 = p.get("s2_id")
    s2 = s2.strip()[:100] if isinstance(s2, str) and s2.strip() else None
    return {"title": title, "authors": authors, "year": year, "arxiv_id": norm_arxiv(p.get("arxiv_id")),
            "doi": norm_doi(p.get("doi")), "url": url, "source_sha256": norm_sha(p.get("source_sha256")),
            "s2_id": s2, "tags": tags[:30]}


def _fields_keys(f: dict) -> dict:
    return identity(f.get("arxiv_id"), f.get("doi"), f.get("source_sha256"), f.get("title"))


def _create_paper(c, f: dict, uid: int) -> str:
    pid = db.new_id("p_")
    c.execute("INSERT INTO papers (id, title, title_norm, authors, year, arxiv_id, doi, url, source_sha256, "
              "s2_id, tags, created_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
              (pid, f["title"], db.norm_title(f["title"]), db.dumps(f["authors"]), f["year"], f["arxiv_id"],
               f["doi"], f["url"], f["source_sha256"], f["s2_id"], db.dumps(f["tags"]), uid, db.now()))
    return pid


def _fill_paper(c, paper, f: dict) -> list:
    """Fill the paper's empty fields from `f`; never overwrite. An identity key another paper
    already has is left out (it would make the two indistinguishable)."""
    sets = {}
    for k in ("authors", "year", "arxiv_id", "doi", "url", "source_sha256", "s2_id", "tags"):
        v = f.get(k)
        if v in (None, "", []):
            continue
        cur = paper[k]
        if k in ("authors", "tags"):
            if db.loads(cur, []):
                continue
            v = db.dumps(v)
        elif cur not in (None, ""):
            continue
        if k in ("arxiv_id", "doi", "source_sha256") and c.execute(
                f"SELECT 1 FROM papers WHERE {k} = ? AND id <> ?", (v, paper["id"])).fetchone():
            continue
        sets[k] = v
    if sets:
        c.execute(f"UPDATE papers SET {', '.join(f'{k} = ?' for k in sets)} WHERE id = ?",
                  (*sets.values(), paper["id"]))
    return list(sets)


# ---- bundle unpacking

class BadBundle(Exception):
    def __init__(self, problems):
        self.problems = [problems] if isinstance(problems, str) else list(problems)
        super().__init__(self.problems[0])


def _gunzip(src: Path, dst: Path) -> None:
    """Unzip into a plain tar, stopping as soon as it grows past the unpacked limit (a small gzip
    can hold gigabytes of zeros; tarfile would also read a huge pax header into memory)."""
    total = 0
    try:
        with gzip.open(src, "rb") as g, open(dst, "wb") as o:
            while True:
                chunk = g.read(CHUNK)
                if not chunk:
                    break
                total += len(chunk)
                if total > UNPACKED_MAX + TAR_SLACK:
                    raise BadBundle(f"the bundle unpacks to more than {UNPACKED_MAX // MB} MB")
                o.write(chunk)
    except (OSError, EOFError, zlib.error):
        raise BadBundle("the body is not a complete gzip file")


def _clean_name(name: str) -> str:
    if not name or "\x00" in name or "\\" in name or any(ord(ch) < 32 for ch in name):
        raise BadBundle(f"{name!r}: a file name with control characters or backslashes")
    if name.startswith("/"):
        raise BadBundle(f"{name!r}: an absolute path")
    parts = [p for p in name.split("/") if p not in ("", ".")]
    if ".." in parts:
        raise BadBundle(f"{name!r}: a path that climbs out with ..")
    return "/".join(parts)


def _extract(tarp: Path, dest: Path) -> set:
    """Check every member first, then write the regular files under `dest`. Returns their names."""
    problems, members, seen = [], [], set()
    total = count = 0
    try:
        with tarfile.open(tarp, "r:") as tf:
            while True:
                m = tf.next()
                if m is None:
                    break
                count += 1
                if count > MAX_MEMBERS:
                    raise BadBundle(f"more than {MAX_MEMBERS} files in the bundle")
                try:
                    name = _clean_name(m.name)
                except BadBundle as e:
                    problems.append(e.problems[0])
                    continue
                if m.issym() or m.islnk():
                    problems.append(f"{m.name!r}: a link; links are not allowed")
                elif m.ischr() or m.isblk() or m.isfifo():
                    problems.append(f"{m.name!r}: a device or pipe; only plain files are allowed")
                elif m.issparse() or not (m.isreg() or m.isdir()):
                    problems.append(f"{m.name!r}: not a plain file")
                elif not name and not m.isdir():
                    problems.append(f"{m.name!r}: a file with no name")
                elif m.isreg() and name in seen:
                    problems.append(f"{m.name!r}: in the bundle twice")
                else:
                    if name:
                        if m.isreg():
                            seen.add(name)
                        members.append((m, name))
                    total += m.size if m.isreg() else 0
                    if total > UNPACKED_MAX:
                        raise BadBundle(f"the bundle unpacks to more than {UNPACKED_MAX // MB} MB")
            if problems:
                raise BadBundle(problems)
            root = os.path.realpath(dest)
            names = set()
            for m, name in members:
                target = dest / name
                if not os.path.realpath(target).startswith(root + os.sep):
                    raise BadBundle(f"{m.name!r}: a path outside the bundle")
                try:
                    if m.isdir():
                        target.mkdir(parents=True, exist_ok=True)
                        continue
                    target.parent.mkdir(parents=True, exist_ok=True)
                    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
                except OSError:
                    raise BadBundle(f"{m.name!r}: clashes with another file in the bundle")
                src = tf.extractfile(m)
                with os.fdopen(fd, "wb") as out:
                    shutil.copyfileobj(src, out, CHUNK)
                names.add(name)
            return names
    except (tarfile.TarError, ValueError, OverflowError):
        raise BadBundle("the bundle is not a readable tar archive")


def _read_manifest(stage: Path, names: set) -> dict:
    if "manifest.json" not in names:
        raise BadBundle("manifest.json is missing from the bundle's root")
    p = stage / "manifest.json"
    if p.stat().st_size > MANIFEST_MAX:
        raise BadBundle("manifest.json is over 1 MB")
    try:
        m = json.loads(p.read_bytes().decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise BadBundle("manifest.json is not UTF-8 JSON")
    if not isinstance(m, dict):
        raise BadBundle("manifest.json is not a JSON object")
    return m


# ---- the checks (section 6)

def minutes_range(base) -> tuple:
    """The base prompt's episode length: its wording's "minutes" if given, else the first
    "15 to 25 minutes" in the guideline, else 15-25."""
    if base is None:
        return DEFAULT_MINUTES
    w = db.loads(base["wording"], {})
    m = w.get("minutes") if isinstance(w, dict) else None
    if isinstance(m, dict):
        m = [m.get("min"), m.get("max")]
    if isinstance(m, (list, tuple)) and len(m) == 2 and all(isinstance(x, (int, float)) for x in m) and 0 < m[0] < m[1]:
        return float(m[0]), float(m[1])
    for a, b in re.findall(r"\b(\d{1,3})\s*(?:to|-|–|—)\s*(\d{1,3})[\s-]*minutes?\b", base["guideline"] or ""):
        lo, hi = float(a), float(b)
        if 0 < lo < hi <= 180:
            return lo, hi
    return DEFAULT_MINUTES


def listener_name(name: str):
    """The maker's first name as a script would write it ("alice.chen" -> "Alice")."""
    for tok in re.findall(r"[^\W\d_]+", name or ""):
        if len(tok) >= 2:
            return tok[0].upper() + tok[1:]
    return None


def _script_problems(text: str, res: dict, maker_name: str) -> list:
    """check()'s problems plus common.wording's never-wanted list, with the listener-name class
    taken from the maker's name (SPEC.md section 10) instead of the list's own (Leo's)."""
    problems = list(res.get("problems") or [])
    reported = set(res.get("wording") or [])
    name_cls = next((c for c in wording.classes() if c.get("id") == "listener_name"), None)
    listener = listener_name(maker_name)
    list_names = list(name_cls.get("phrases") or []) if name_cls else []
    if name_cls and listener not in list_names:
        if name_cls["wrong"] in reported:           # that is someone else's name here
            problems = [p for p in problems if not p.startswith(name_cls["wrong"])]
            reported.discard(name_cls["wrong"])
    for c, found in wording.hits(text, "script"):
        if c.get("id") == "listener_name" and listener not in list_names:
            continue
        if c["wrong"] in reported:
            continue
        reported.add(c["wrong"])
        problems.append(f"{c['wrong']}: {wording.quoted(found)}. {c['fix']}")
    if listener and listener not in list_names:
        rx = re.compile(r"(?<![\w'’-])" + re.escape(listener) + r"(?![\w-])")
        said = [s for s in checks.sentences(text) if rx.search(s)]
        if said:
            wrong = name_cls["wrong"] if name_cls else "names the listener"
            fix = name_cls["fix"] if name_cls else "Delete the name."
            listed = "; ".join(f'({i}) "{s}"' for i, s in enumerate(said[:5], 1))
            problems.append(f"{wrong} ({listener}), in {len(said)} sentence{'s' if len(said) > 1 else ''}: "
                            f"{listed}. {fix}")
    return problems


def check_files(epdir: Path, lo: float, hi: float, maker_name: str) -> tuple:
    """(problems in plain words, {"words", "minutes"}) for an episode's stored files."""
    problems, stats = [], {"words": None, "minutes": None}
    sp = epdir / "script.md"
    if not sp.is_file():
        problems.append("script.md is missing")
    elif sp.stat().st_size > SCRIPT_MAX:
        problems.append(f"script.md is {sp.stat().st_size / 1024:.0f} kB; at most {SCRIPT_MAX // 1024} kB")
    else:
        try:
            text = sp.read_bytes().decode("utf-8").lstrip("﻿")
        except UnicodeDecodeError:
            problems.append("script.md is not UTF-8 text")
        else:
            res = checks.check(text, WPM, lo, hi)
            stats = {"words": res.get("words"), "minutes": res.get("minutes")}
            problems += _script_problems(text, res, maker_name)
    hp = epdir / "explainer.html"
    if not hp.is_file():
        problems.append("explainer.html is missing")
    elif hp.stat().st_size > EXPLAINER_HTML_MAX:
        problems.append(f"explainer.html is {hp.stat().st_size / MB:.1f} MB; at most {EXPLAINER_HTML_MAX // MB} MB")
    else:
        with open(hp, "rb") as f:
            head = f.read(512)
        if not head.lstrip(b"\xef\xbb\xbf \t\r\n").lower().startswith(b"<!doctype html"):
            problems.append("explainer.html must start with <!doctype html>")
    jp = epdir / "explainer.json"
    if not jp.is_file():
        problems.append("explainer.json is missing")
    elif jp.stat().st_size > EXPLAINER_JSON_MAX:
        problems.append(f"explainer.json is over {EXPLAINER_JSON_MAX // MB} MB")
    else:
        try:
            obj = json.loads(jp.read_bytes().decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            problems.append("explainer.json is not UTF-8 JSON")
        else:
            problems += [f"explainer.json: {p}" for p in checks.explainer_problems(obj)]
    return problems, stats


def _base_for(c, version):
    if isinstance(version, int) and not isinstance(version, bool):
        r = c.execute("SELECT * FROM base_prompts WHERE version = ?", (version,)).fetchone()
        if r:
            return r
    return c.execute("SELECT * FROM base_prompts ORDER BY version DESC LIMIT 1").fetchone()


def _publish_episode(c, eid) -> None:
    r = c.execute("SELECT id, paper_id, state, state_detail, made_by FROM episodes WHERE id = ?", (eid,)).fetchone()
    if r:
        events.publish("episode", dict(r))


def _npositional(fn) -> int:
    try:
        ps = inspect.signature(fn).parameters.values()
    except (TypeError, ValueError):
        return 4
    if any(p.kind == p.VAR_POSITIONAL for p in ps):
        return 4
    return sum(p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD) for p in ps)


def _apply_links(cfg, ep, links) -> None:
    """Hand the bundle's links to the graph (A5). SPEC.md says apply_agent_links(episode, links),
    the coordinator's brief (episode_id, paper_id, user_id, links): both are accepted. Until the
    graph has it, or when it fails, the links wait in links-pending.json in the episode dir."""
    if not links:
        return
    fn = getattr(graph, "apply_agent_links", None)
    if fn is not None:
        try:
            if _npositional(fn) >= 4:
                fn(ep["id"], ep["paper_id"], ep["made_by"], links)
            else:
                first = next(iter(inspect.signature(fn).parameters), "")
                fn(ep["id"] if "id" in first else dict(ep), links)
            return
        except Exception:
            log.exception("links of %s: graph.apply_agent_links failed; they wait in links-pending.json", ep["id"])
    else:
        log.info("links of %s wait in links-pending.json: graph.apply_agent_links is not there yet", ep["id"])
    (cfg.episodes / ep["id"] / "links-pending.json").write_text(
        json.dumps({"episode_id": ep["id"], "paper_id": ep["paper_id"], "user_id": ep["made_by"],
                    "links": links}, ensure_ascii=False, indent=1), encoding="utf-8")


def run_checks(cfg, eid: str) -> None:
    c = db.conn()
    ep = c.execute("SELECT * FROM episodes WHERE id = ?", (eid,)).fetchone()
    if ep is None or ep["state"] != "checking":
        return
    epdir = cfg.episodes / eid
    manifest = json.loads((epdir / "bundle-manifest.json").read_text(encoding="utf-8"))
    maker = c.execute("SELECT name FROM users WHERE id = ?", (ep["made_by"],)).fetchone()
    lo, hi = minutes_range(_base_for(c, ep["base_version"]))
    problems, stats = check_files(epdir, lo, hi, maker["name"] if maker else "")
    now = db.now()
    with db.transaction() as t:
        cur = t.execute("SELECT state FROM episodes WHERE id = ?", (eid,)).fetchone()
        if cur is None or cur["state"] != "checking":
            return
        if problems:
            n = len(problems)
            t.execute("UPDATE episodes SET state = 'rejected', state_detail = ?, check_report = ?, words = ?, "
                      "est_minutes = ?, updated_at = ? WHERE id = ?",
                      (f"the hub's checks found {n} problem{'s' if n > 1 else ''}", db.dumps(problems),
                       stats["words"], stats["minutes"], now, eid))
        else:
            t.execute("UPDATE episodes SET state = 'waiting-for-gpu', state_detail = 'in the queue for the voice', "
                      "check_report = '[]', words = ?, est_minutes = ?, updated_at = ? WHERE id = ?",
                      (stats["words"], stats["minutes"], now, eid))
            voiceq.enqueue(t, eid, ep["made_by"])
    log.info("episode %s %s%s", eid, "rejected: " if problems else "passed its checks",
             "; ".join(problems)[:500] if problems else "")
    _publish_episode(c, eid)
    if not problems:
        _apply_links(cfg, c.execute("SELECT * FROM episodes WHERE id = ?", (eid,)).fetchone(),
                     manifest.get("links") or [])


def _check_thread(cfg, eid: str) -> None:
    try:
        run_checks(cfg, eid)
    except Exception:
        log.exception("checking %s", eid)
        try:
            with db.transaction() as t:
                t.execute("UPDATE episodes SET state = 'rejected', state_detail = 'the hub could not check it', "
                          "check_report = ?, updated_at = ? WHERE id = ? AND state = 'checking'",
                          (db.dumps(["the hub could not check this episode (an error on the hub); upload it again"]),
                           db.now(), eid))
            _publish_episode(db.conn(), eid)
        except Exception:
            log.exception("marking %s rejected", eid)
    finally:
        with _lock:
            _inflight.discard(eid)


def _start_check(cfg, eid: str) -> None:
    threading.Thread(target=_check_thread, args=(cfg, eid), name=f"check-{eid}", daemon=True).start()


def _recover_once(cfg) -> None:
    """Episodes left in `checking` by a hub that stopped mid-check are checked again, once per
    process, on the first CLI request."""
    key = str(cfg.data)
    with _lock:
        if key in _recovered:
            return
        _recovered.add(key)
    for r in db.conn().execute("SELECT id FROM episodes WHERE state = 'checking'").fetchall():
        with _lock:
            if r["id"] in _inflight:
                continue
            _inflight.add(r["id"])
        log.info("checking %s again (left over from before a restart)", r["id"])
        _start_check(cfg, r["id"])


def _begin(req) -> None:
    _recover_once(req.cfg)
    voiceq.start_sweeper()


# ---- routes: who, prompt, prefs, library

def me(req):
    req.send_json(200, _user_json(req.user))


def prompt(req):
    r = db.conn().execute("SELECT * FROM base_prompts ORDER BY version DESC LIMIT 1").fetchone()
    if r is None:
        raise HTTPError(404, "no_base_prompt", "there is no base prompt yet; an admin adds one on the web")
    req.send_json(200, {"version": r["version"], "guideline": r["guideline"], "wording": db.loads(r["wording"], {})})


def _prefs_of(c, uid) -> dict:
    r = c.execute("SELECT * FROM prefs WHERE user_id = ?", (uid,)).fetchone()
    if r is None:
        return {"settings": dict(P.DEFAULTS), "note": "", "version": 0}
    return {"settings": P.full(db.loads(r["settings"], {})), "note": r["note"], "version": r["version"]}


def get_prefs(req):
    req.send_json(200, _prefs_of(db.conn(), _uid(req)))


def put_prefs(req):
    """`papercast prefs --maths full`: the settings given are changed, the others kept."""
    body = req.json()
    uid = _uid(req)
    settings = body.get("settings", {})
    with db.transaction() as c:
        cur = _prefs_of(c, uid)
        if not isinstance(settings, dict):
            raise HTTPError(400, "bad_prefs", "settings must be an object")
        merged = {**cur["settings"], **settings}
        note = body["note"] if "note" in body else cur["note"]
        problems = P.validate(merged, note)
        if problems:
            raise HTTPError(400, "bad_prefs", problems[0], problems=problems)
        c.execute("INSERT INTO prefs (user_id, settings, note, version, updated_at) VALUES (?, ?, ?, 1, ?) "
                  "ON CONFLICT(user_id) DO UPDATE SET settings = excluded.settings, note = excluded.note, "
                  "version = prefs.version + 1, updated_at = excluded.updated_at",
                  (uid, db.dumps(merged), note.strip(), db.now()))
        out = _prefs_of(c, uid)
    req.send_json(200, out)


def library(req):
    rows = db.conn().execute("SELECT id, title, year, arxiv_id, doi, s2_id FROM papers ORDER BY created_at").fetchall()
    req.send_json(200, {"papers": [dict(r) for r in rows]})


# ---- routes: lookup and claims

def _keys_from(d) -> dict:
    d = d if isinstance(d, dict) else {}
    return identity(d.get("arxiv_id"), d.get("doi"), d.get("sha256") or d.get("source_sha256"), d.get("title"))


def _paper_json(c, paper, uid) -> dict:
    eps = c.execute("SELECT e.id, e.state, e.prefs_summary, e.made_by, u.name FROM episodes e "
                    "JOIN users u ON u.id = e.made_by WHERE e.paper_id = ? AND e.deleted_at IS NULL "
                    "AND (e.state <> 'rejected' OR e.made_by = ?) ORDER BY e.created_at", (paper["id"], uid)).fetchall()
    return {"id": paper["id"], "title": paper["title"],
            "episodes": [{"id": e["id"], "made_by": {"id": e["made_by"], "name": e["name"]},
                          "prefs_summary": e["prefs_summary"], "state": e["state"]} for e in eps]}


def lookup(req):
    _begin(req)
    keys = _keys_from(req.query)
    if not keys:
        raise HTTPError(400, "no_keys", "give at least one of arxiv_id, doi, sha256, title")
    uid = _uid(req)
    c = db.conn()
    paper = find_paper(c, keys)
    claims = _live_claims(c, keys)
    other = next((cl for cl in claims if cl["user_id"] != uid), None)
    mine = next((cl for cl in claims if cl["user_id"] == uid), None)
    claim = None
    if other is not None:
        claim = {"by": {"name": other["by_name"]}, "since": other["created_at"], "mine": False}
    elif mine is not None:
        claim = {"by": {"name": mine["by_name"]}, "since": mine["created_at"], "mine": True,
                 "claim_id": mine["id"], "expires_at": mine["expires_at"]}
    req.send_json(200, {"paper": _paper_json(c, paper, uid) if paper is not None else None, "claim": claim})


def _in_progress(cl):
    return HTTPError(409, "in_progress", f"{cl['by_name']} is making this paper (since {cl['created_at']})",
                     by={"name": cl["by_name"]}, since=cl["created_at"])


def _exists(paper):
    return HTTPError(409, "exists", "this paper is in the library already; make your own version of it",
                     paper={"id": paper["id"], "title": paper["title"]})


def post_claim(req):
    _begin(req)
    body = req.json()
    keys = _keys_from(body.get("keys"))
    if not keys:
        raise HTTPError(400, "no_keys", "keys needs at least one of arxiv_id, doi, sha256, title")
    device = str(body.get("device") or "")[:100] or None
    uid = _uid(req)
    now, until = db.now(), _at(CLAIM_S)
    with db.transaction() as c:
        paper = find_paper(c, keys)
        if paper is not None:
            raise _exists(paper)
        claims = _live_claims(c, keys)
        for cl in claims:
            if cl["user_id"] != uid:
                raise _in_progress(cl)
        if claims:                          # the caller's own: renewed, with any keys it lacked
            cid = claims[0]["id"]
            c.execute("UPDATE claims SET expires_at = ?, device = COALESCE(?, device), arxiv_id = COALESCE(arxiv_id, ?), "
                      "doi = COALESCE(doi, ?), source_sha256 = COALESCE(source_sha256, ?), "
                      "title_norm = COALESCE(title_norm, ?) WHERE id = ?",
                      (until, device, keys.get("arxiv_id"), keys.get("doi"), keys.get("source_sha256"),
                       keys.get("title_norm"), cid))
        else:
            cid = db.new_id("c_")
            c.execute("INSERT INTO claims (id, user_id, arxiv_id, doi, source_sha256, title_norm, device, "
                      "created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                      (cid, uid, keys.get("arxiv_id"), keys.get("doi"), keys.get("source_sha256"),
                       keys.get("title_norm"), device, now, until))
    req.send_json(201, {"claim_id": cid, "expires_at": until, "renewed": bool(claims)})


def _own_claim(c, cid, uid):
    cl = c.execute("SELECT * FROM claims WHERE id = ?", (cid,)).fetchone()
    if cl is None or cl["user_id"] != uid:
        raise HTTPError(404, "no_such_claim", "no such claim of yours")
    return cl


def put_claim(req, cid):
    """Renew a claim for another 6 hours. An expired one is renewed only if nobody else has
    claimed or made the paper meanwhile."""
    uid = _uid(req)
    until = _at(CLAIM_S)
    with db.transaction() as c:
        cl = _own_claim(c, cid, uid)
        if cl["done_at"]:
            raise HTTPError(409, "claim_done", "this claim's upload has landed already")
        keys = _row_keys(cl)
        paper = find_paper(c, keys)
        if paper is not None:
            raise _exists(paper)
        for other in _live_claims(c, keys):
            if other["user_id"] != uid:
                raise _in_progress(other)
        c.execute("UPDATE claims SET expires_at = ? WHERE id = ?", (until, cid))
    req.send_json(200, {"claim_id": cid, "expires_at": until})


def delete_claim(req, cid):
    """Give a claim up (the job was cancelled), so someone else may make the paper."""
    with db.transaction() as c:
        cl = _own_claim(c, cid, _uid(req))
        if not cl["done_at"]:
            c.execute("UPDATE claims SET expires_at = ? WHERE id = ?", (db.now(), cid))
    req.send_json(200, {"claim_id": cid, "released": True})


# ---- routes: upload

def _resolve_paper(c, manifest, uid):
    """(paper row, created, filled fields, claim id to mark done) for an upload, or HTTPError."""
    f = paper_fields(manifest.get("paper"))
    if not f["title"]:
        raise HTTPError(400, "bad_manifest", "paper.title is required", problems=["paper.title is required"])
    if manifest.get("paper_id"):                                   # a new version
        paper = c.execute("SELECT * FROM papers WHERE id = ?", (str(manifest["paper_id"]),)).fetchone()
        if paper is None:
            raise HTTPError(404, "no_such_paper", f"no paper {manifest['paper_id']} for a new version")
        if f["arxiv_id"] and paper["arxiv_id"] and f["arxiv_id"] != paper["arxiv_id"]:
            raise HTTPError(409, "paper_mismatch", f"the bundle is arXiv {f['arxiv_id']}, but "
                            f"{paper['id']} is arXiv {paper['arxiv_id']}")
        return paper, False, _fill_paper(c, paper, f), None
    keys = _fields_keys(f)
    cid = manifest.get("claim_id")
    cl = c.execute("SELECT * FROM claims WHERE id = ?", (str(cid),)).fetchone() if cid else None
    mine = cl is not None and cl["user_id"] == uid
    done = cl["id"] if mine and not cl["done_at"] else None
    paper = find_paper(c, keys)
    if paper is not None:           # made meanwhile (or by this claim's earlier upload): a new version
        return paper, False, _fill_paper(c, paper, f), done
    if not mine:
        raise HTTPError(409, "no_claim", "a new paper needs your claim on it (POST /api/cli/claims)")
    if cl["done_at"]:
        raise HTTPError(409, "claim_done", "this claim's upload has landed already; claim the paper again")
    if cl["expires_at"] <= db.now():
        others = [o for o in _live_claims(c, keys) if o["user_id"] != uid]
        if others:
            raise _in_progress(others[0])
        raise HTTPError(409, "claim_expired", "your claim expired; claim the paper again and upload with the new claim_id")
    if not same_paper(_row_keys(cl), keys):
        raise HTTPError(409, "claim_mismatch", "the claim is for a different paper than the bundle's")
    return c.execute("SELECT * FROM papers WHERE id = ?", (_create_paper(c, f, uid),)).fetchone(), True, [], done


def _land(req, manifest: dict, stage: Path, names: set) -> dict:
    cfg, uid = req.cfg, _uid(req)
    eid = db.new_id("e_")
    epdir = cfg.episodes / eid
    with _lock:
        _inflight.add(eid)
    committed = False
    try:
        epdir.mkdir(parents=True)
        files = manifest.get("files") or {}
        for key, stored in FILES.items():
            n = files.get(key)
            if isinstance(n, str) and n in names:
                shutil.copyfile(stage / n, epdir / stored)
        (epdir / "bundle-manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
        pr = manifest.get("prefs") if isinstance(manifest.get("prefs"), dict) else {}
        settings = pr.get("settings") if isinstance(pr.get("settings"), dict) else {}
        base_v = manifest.get("base_version")
        base_v = base_v if isinstance(base_v, int) and not isinstance(base_v, bool) else None
        now = db.now()
        with db.transaction() as c:
            paper, created, filled, done = _resolve_paper(c, manifest, uid)
            c.execute("INSERT INTO episodes (id, paper_id, made_by, state, state_detail, base_version, prefs, "
                      "prefs_summary, client_version, model, check_report, created_at, updated_at) "
                      "VALUES (?, ?, ?, 'checking', 'being checked on the hub', ?, ?, ?, ?, ?, '[]', ?, ?)",
                      (eid, paper["id"], uid, base_v, db.dumps(pr), P.summary(settings),
                       str(manifest.get("client_version") or "")[:40] or None,
                       str(manifest.get("model") or "")[:80] or None, now, now))
            if done:
                c.execute("UPDATE claims SET done_at = ? WHERE id = ?", (now, done))
            paper = c.execute("SELECT * FROM papers WHERE id = ?", (paper["id"],)).fetchone()
            maker = c.execute("SELECT id, name FROM users WHERE id = ?", (uid,)).fetchone()
        committed = True
    except BaseException:
        if not committed:
            shutil.rmtree(epdir, ignore_errors=True)
            with _lock:
                _inflight.discard(eid)
        raise
    meta = {"episode_id": eid, "paper_id": paper["id"],
            "paper": {k: (db.loads(paper[k], []) if k in ("authors", "tags") else paper[k])
                      for k in ("title", "authors", "year", "arxiv_id", "doi", "url", "source_sha256", "tags")},
            "made_by": {"id": maker["id"], "name": maker["name"]}, "uploaded_at": now,
            "base_version": base_v, "client_version": manifest.get("client_version"),
            "model": manifest.get("model"), "prefs": pr, "stats": manifest.get("stats")}
    try:
        (epdir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError:
        log.exception("meta.json of %s", eid)
    if created or filled:
        events.publish("paper", {"id": paper["id"], "title": paper["title"], "new": created})
    _publish_episode(db.conn(), eid)
    _start_check(cfg, eid)
    return {"episode_id": eid, "paper_id": paper["id"], "state": "checking", "new_paper": created}


@voiceq.streamed
def upload(req):
    _begin(req)
    ctype = (req.headers.get("Content-Type") or "").split(";")[0].strip().lower()
    if ctype not in ("application/gzip", "application/x-gzip"):
        raise HTTPError(415, "bad_type", "send the bundle as application/gzip (a tar.gz)")
    tmp = req.cfg.data / "tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="upload-", dir=tmp))
    try:
        gz = work / "bundle.tar.gz"
        voiceq.save_body(req, gz, BUNDLE_MAX, "the bundle")
        tarp, stage = work / "bundle.tar", work / "files"
        stage.mkdir()
        try:
            _gunzip(gz, tarp)
            gz.unlink()
            names = _extract(tarp, stage)
            tarp.unlink()
            manifest = _read_manifest(stage, names)
        except BadBundle as e:
            raise HTTPError(400, "bad_bundle", e.problems[0], problems=e.problems)
        problems = bundle.validate(manifest, names)
        if problems:
            raise HTTPError(400, "bad_manifest", problems[0], problems=problems)
        out = _land(req, manifest, stage, names)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    req.send_json(201, out)


# ---- routes: the caller's episodes

def _episode_json(r, positions: dict) -> dict:
    return {"id": r["id"], "paper_id": r["paper_id"], "title": r["title"], "state": r["state"],
            "state_detail": r["state_detail"], "check_report": db.loads(r["check_report"], []),
            "made_by": {"id": r["made_by"], "name": r["maker"]}, "prefs_summary": r["prefs_summary"],
            "base_version": r["base_version"], "client_version": r["client_version"], "model": r["model"],
            "words": r["words"], "est_minutes": r["est_minutes"], "duration_s": r["duration_s"],
            "created_at": r["created_at"], "updated_at": r["updated_at"],
            "voice": None if r["vstate"] is None else {
                "state": r["vstate"], "phase": r["phase"], "progress": r["progress"], "attempts": r["attempts"],
                "error": r["error"], "queue_position": positions.get(r["id"])}}


_EPISODE_SQL = ("SELECT e.*, p.title, u.name AS maker, v.state AS vstate, v.phase, v.progress, v.attempts, v.error "
                "FROM episodes e JOIN papers p ON p.id = e.paper_id JOIN users u ON u.id = e.made_by "
                "LEFT JOIN voice_jobs v ON v.episode_id = e.id ")


def list_episodes(req):
    """?mine=1 (the default): the caller's episodes, newest first; ?mine=0: everyone's."""
    _begin(req)
    c = db.conn()
    if req.arg("mine", "1") in ("0", "false", "no"):
        rows = c.execute(_EPISODE_SQL + "WHERE e.deleted_at IS NULL AND e.state <> 'rejected' "
                         "ORDER BY e.created_at DESC LIMIT 500").fetchall()
    else:
        rows = c.execute(_EPISODE_SQL + "WHERE e.made_by = ? AND e.deleted_at IS NULL "
                         "ORDER BY e.created_at DESC LIMIT 500", (_uid(req),)).fetchall()
    pos = voiceq.queue_positions()
    req.send_json(200, {"episodes": [_episode_json(r, pos) for r in rows]})


def get_episode(req, eid):
    _begin(req)
    r = db.conn().execute(_EPISODE_SQL + "WHERE e.id = ?", (eid,)).fetchone()
    if r is None or (r["deleted_at"] and r["made_by"] != _uid(req)):
        raise HTTPError(404, "no_such_episode", f"no episode {eid}")
    req.send_json(200, _episode_json(r, voiceq.queue_positions()))


_ID = r"([A-Za-z0-9_-]{1,64})"
ROUTES = [
    ("GET", r"^/api/cli/me$", me, "cli"),
    ("GET", r"^/api/cli/prompt$", prompt, "cli"),
    ("GET", r"^/api/cli/prefs$", get_prefs, "cli"),
    ("PUT", r"^/api/cli/prefs$", put_prefs, "cli"),
    ("GET", r"^/api/cli/library$", library, "cli"),
    ("GET", r"^/api/cli/lookup$", lookup, "cli"),
    ("POST", r"^/api/cli/claims$", post_claim, "cli-contributor"),
    ("PUT", rf"^/api/cli/claims/{_ID}$", put_claim, "cli-contributor"),
    ("DELETE", rf"^/api/cli/claims/{_ID}$", delete_claim, "cli-contributor"),
    ("POST", r"^/api/cli/episodes$", upload, "cli-contributor"),
    ("GET", r"^/api/cli/episodes$", list_episodes, "cli"),
    ("GET", rf"^/api/cli/episodes/{_ID}$", get_episode, "cli"),
]
