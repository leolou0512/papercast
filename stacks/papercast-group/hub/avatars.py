"""Profile pictures (Leo, 2026-09-29: "Add profile picture option.").

The page makes the picture, not the hub: it reads the file the person picked, crops the middle
square, draws it at 256 x 256 on a canvas and sends that as a JPEG (quality 0.85), so what
arrives is small and plain. The hub never decodes it. It checks only the bytes' headers: at most
AVATAR_MAX bytes, a JPEG's start of image, a frame header (SOF) that parses, with a width and a
height from 32 to 1024 and the two nearly equal, and the end of image last. It stores the bytes as
they came, as $PCG_DATA/avatars/<user id>-<version>.jpg, where the version is the start of the
file's sha256: the file's name, its ETag, and the ?v= in the address the page shows it at (so a
browser keeps a picture until it changes). tools/backup.py backs the folder up with episodes/.

  PUT    /api/me/avatar                  the JPEG as the body (Content-Type image/jpeg) -> {"avatar"}
  DELETE /api/me/avatar                  -> {"avatar": null}
  DELETE /api/admin/users/<id>/avatar    an admin removes anyone's
  GET    /api/avatars/<user id>.jpg?v=   the picture: image/jpeg, nosniff, a CSP of its own
                                         (default-src 'none'; sandbox); kept a year when v is its
                                         version, otherwise revalidated by its ETag

Every user object the page gets (/api/me, /api/config, the library's makers, comments' writers,
the board, /api/admin/users) says "avatar": the version, or null for none; /api/config also has
`avatars`, {user id: version} for everyone with one (the map's cards). When a picture changes the
event `avatar` {"user_id", "avatar"} goes to everyone, and open pages redraw it. Uploads are
limited to UPLOAD_LIMIT a minute per person."""
from __future__ import annotations

import hashlib
import logging
import os
import secrets
import stat
import threading
import time
from collections import deque

from . import db, events
from .app import HTTPError

log = logging.getLogger("pcg.avatars")

AVATAR_MAX = 200 * 1024          # the page sends about 10 to 40 KB (256 x 256 at quality 0.85)
SIDE_MIN, SIDE_MAX = 32, 1024
UPLOAD_LIMIT, UPLOAD_WINDOW = 5, 60.0
VERSION_LEN = 16                 # hex characters of the sha256
CACHE_S = 365 * 86400
CSP = "default-src 'none'; sandbox"
DIR = "avatars"

SCHEMA = """
CREATE TABLE IF NOT EXISTS avatars (             -- one picture per person, its file avatars/<user_id>-<version>.jpg
  user_id INTEGER PRIMARY KEY REFERENCES users(id),
  version TEXT NOT NULL,                         -- the file's sha256, first 16 hex characters
  bytes INTEGER NOT NULL,
  width INTEGER NOT NULL,
  height INTEGER NOT NULL,
  updated_at TEXT NOT NULL
)
"""

_ready: set = set()
_ready_lock = threading.Lock()


def ensure_schema() -> None:
    """This module's table, once per database (db.py is A1's: nothing of this is in it)."""
    key = str(db._path)
    if key in _ready:
        return
    with _ready_lock:
        if key in _ready:
            return
        c = db.conn()
        for stmt in SCHEMA.split(";"):          # one by one: executescript would commit a caller's transaction
            if stmt.strip():
                c.execute(stmt)
        if not c.in_transaction:
            _ready.add(key)


def start(cfg) -> None:
    ensure_schema()
    (cfg.data / DIR).mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------- what other modules use

def versions(c=None) -> dict:
    """{user id: version} for everyone with a picture."""
    ensure_schema()
    c = c or db.conn()
    return {r[0]: r[1] for r in c.execute("SELECT user_id, version FROM avatars")}


def version_of(uid, c=None):
    """This person's picture's version, or None."""
    if uid is None:
        return None
    ensure_schema()
    r = (c or db.conn()).execute("SELECT version FROM avatars WHERE user_id = ?", (uid,)).fetchone()
    return r[0] if r else None


def decorate(users, av: dict | None = None) -> None:
    """Give each user dict (id, ...) its "avatar", in place. None entries are left alone."""
    av = versions() if av is None else av
    for u in users:
        if isinstance(u, dict) and "id" in u:
            u["avatar"] = av.get(u["id"])


# ---------------------------------------------------------------- the check

# The markers that start a frame: SOF0-3, 5-7, 9-11, 13-15 (C4 is DHT, C8 JPG, CC DAC).
SOF = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
STANDALONE = {0x01} | set(range(0xD0, 0xD8))      # TEM, RST0-7: no length


def jpeg_size(b: bytes):
    """(width, height) from a JPEG's frame header, or None when these bytes are not a JPEG this
    hub takes: SOI first, EOI last, and before the first scan a well-formed segment chain with an
    SOF (8-bit, 1 or 3 components). Nothing is decoded."""
    if len(b) < 128 or b[:3] != b"\xff\xd8\xff" or b[-2:] != b"\xff\xd9":
        return None
    i, n = 2, len(b)
    while i + 4 <= n:
        if b[i] != 0xFF:
            return None
        while i < n and b[i] == 0xFF:           # fill bytes
            i += 1
        if i >= n:
            return None
        m = b[i]
        i += 1
        if m in STANDALONE:
            continue
        if m in (0xD8, 0xD9, 0x00):             # a second SOI, EOI before any frame, a stuffed byte
            return None
        if i + 2 > n:
            return None
        ln = (b[i] << 8) | b[i + 1]
        if ln < 2 or i + ln > n:
            return None
        seg = b[i + 2:i + ln]
        if m in SOF:
            if len(seg) < 6:
                return None
            prec, h, w, nf = seg[0], (seg[1] << 8) | seg[2], (seg[3] << 8) | seg[4], seg[5]
            if prec != 8 or nf not in (1, 3) or ln != 8 + 3 * nf:
                return None
            return w, h
        if m == 0xDA:                           # a scan before any frame header
            return None
        i += ln
    return None


def check(b: bytes) -> tuple:
    """(width, height), or HTTPError 400 saying what is wrong."""
    if len(b) > AVATAR_MAX:
        raise HTTPError(413, "too_large", f"a picture is at most {AVATAR_MAX // 1024} KB")
    got = jpeg_size(b)
    if got is None:
        raise HTTPError(400, "not_jpeg", "that is not a JPEG picture")
    w, h = got
    if not (SIDE_MIN <= w <= SIDE_MAX and SIDE_MIN <= h <= SIDE_MAX):
        raise HTTPError(400, "bad_size", f"a picture is {SIDE_MIN} to {SIDE_MAX} pixels on each side")
    if abs(w - h) > max(2, min(w, h) // 20):
        raise HTTPError(400, "not_square", "a picture is square")
    return w, h


# ---------------------------------------------------------------- limits

class _Limit:
    """At most n events per key in a sliding window."""

    def __init__(self, n: int, window: float):
        self.n, self.window = n, window
        self.hits: dict = {}
        self.lock = threading.Lock()

    def hit(self, key) -> bool:
        now = time.monotonic()
        with self.lock:
            q = self.hits.setdefault(key, deque())
            while q and now - q[0] > self.window:
                q.popleft()
            if len(q) >= self.n:
                return False
            q.append(now)
            return True


_uploads = _Limit(UPLOAD_LIMIT, UPLOAD_WINDOW)


def reset_limits() -> None:
    """Tests: a fresh minute for everyone."""
    with _uploads.lock:
        _uploads.hits.clear()


# ---------------------------------------------------------------- files

def folder(cfg):
    return cfg.data / DIR


def path_of(cfg, uid: int, version: str):
    return folder(cfg) / f"{int(uid)}-{version}.jpg"


def _unlink(p) -> None:
    try:
        os.unlink(p)
    except FileNotFoundError:
        pass
    except OSError:
        log.exception("removing %s", p)


_writing = threading.Lock()      # one change of a picture at a time: a file and its row move together


def store(cfg, uid: int, b: bytes) -> str:
    """Check the bytes and make them this person's picture. Returns its version."""
    ensure_schema()
    w, h = check(b)
    with _writing:
        version = _store(cfg, uid, b, w, h)
    events.publish("avatar", {"user_id": uid, "avatar": version})
    return version


def _store(cfg, uid: int, b: bytes, w: int, h: int) -> str:
    version = hashlib.sha256(b).hexdigest()[:VERSION_LEN]
    d = folder(cfg)
    d.mkdir(parents=True, exist_ok=True)
    final = path_of(cfg, uid, version)
    tmp = d / f".tmp-{int(uid)}-{secrets.token_hex(6)}"
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)     # the umask decides, as for episodes/
        with os.fdopen(fd, "wb") as f:
            f.write(b)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, final)
    except BaseException:
        _unlink(tmp)
        raise
    try:
        with db.transaction() as c:
            if c.execute("SELECT 1 FROM users WHERE id = ?", (uid,)).fetchone() is None:
                raise HTTPError(404, "not_found", "no such user")
            old = c.execute("SELECT version FROM avatars WHERE user_id = ?", (uid,)).fetchone()
            c.execute("INSERT INTO avatars(user_id, version, bytes, width, height, updated_at) VALUES (?, ?, ?, ?, ?, ?) "
                      "ON CONFLICT(user_id) DO UPDATE SET version = excluded.version, bytes = excluded.bytes, "
                      "width = excluded.width, height = excluded.height, updated_at = excluded.updated_at",
                      (uid, version, len(b), w, h, db.now()))
    except BaseException:
        if version_of(uid) != version:
            _unlink(final)
        raise
    if old and old[0] != version:
        _unlink(path_of(cfg, uid, old[0]))
    return version


def remove(cfg, uid: int) -> bool:
    """This person has no picture any more. False when they had none."""
    ensure_schema()
    with _writing:
        with db.transaction() as c:
            old = c.execute("SELECT version FROM avatars WHERE user_id = ?", (uid,)).fetchone()
            if old is None:
                return False
            c.execute("DELETE FROM avatars WHERE user_id = ?", (uid,))
        _unlink(path_of(cfg, uid, old[0]))
    events.publish("avatar", {"user_id": uid, "avatar": None})
    return True


# ---------------------------------------------------------------- routes

def _mutation(req, limit: int = 0) -> None:
    """The CSRF fence (SPEC section 2), as web.py's: read the body first (up to `limit`), then the
    page's header, and never a request the browser marks as another site's."""
    req.body(limit or 1024)
    if req.headers.get("X-PCG") != "1":
        raise HTTPError(403, "csrf", "a changing request needs the page's X-PCG header")
    site = req.headers.get("Sec-Fetch-Site")
    if site is not None and site not in ("same-origin", "none"):
        raise HTTPError(403, "cross_origin", "cross-site request refused")


def put_mine(req):
    _mutation(req, AVATAR_MAX)
    ctype = (req.headers.get("Content-Type") or "").split(";")[0].strip().lower()
    if ctype != "image/jpeg":
        raise HTTPError(415, "bad_type", "send the picture as image/jpeg")
    uid = req.user["id"]
    b = req.body(AVATAR_MAX)
    check(b)                                    # before the limit: a refused file costs no turn
    if not _uploads.hit(uid):
        raise HTTPError(429, "slow_down", "that is a lot of pictures at once: wait a minute", retry_after=int(UPLOAD_WINDOW))
    req.send_json(200, {"avatar": store(req.cfg, uid, b)})


def delete_mine(req):
    _mutation(req)
    remove(req.cfg, req.user["id"])
    req.send_json(200, {"avatar": None})


def admin_delete(req, uid):
    _mutation(req)
    uid = int(uid)
    if db.conn().execute("SELECT 1 FROM users WHERE id = ?", (uid,)).fetchone() is None:
        raise HTTPError(404, "not_found", "no such user")
    remove(req.cfg, uid)
    req.send_json(200, {"user_id": uid, "avatar": None})


def get_picture(req, uid):
    ensure_schema()
    uid = int(uid)
    v = version_of(uid)
    if v is None:
        raise HTTPError(404, "not_found", "no picture")
    etag = f'"{v}"'
    cache = f"private, max-age={CACHE_S}, immutable" if req.arg("v") == v else "private, no-cache"
    headers = [("Content-Type", "image/jpeg"), ("X-Content-Type-Options", "nosniff"), ("Content-Security-Policy", CSP),
               ("Cache-Control", cache), ("ETag", etag), ("Referrer-Policy", "no-referrer"),
               ("Cross-Origin-Resource-Policy", "same-origin"), ("Content-Disposition", "inline")]
    inm = req.headers.get("If-None-Match") or ""
    if etag in [x.strip() for x in inm.split(",")] or inm.strip() == "*":
        h = req.h
        h.send_response(304)
        for k, val in headers:
            if k not in ("Content-Type", "Content-Disposition"):
                h.send_header(k, val)
        h.send_header("Content-Length", "0")
        h.end_headers()
        req.sent = True
        return
    try:
        fd = os.open(path_of(req.cfg, uid, v), os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        raise HTTPError(404, "not_found", "no picture") from None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size > AVATAR_MAX:
            raise HTTPError(404, "not_found", "no picture")
        body = b""
        while len(body) < st.st_size:
            chunk = os.read(fd, st.st_size - len(body))
            if not chunk:
                break
            body += chunk
    finally:
        os.close(fd)
    h = req.h
    h.send_response(200)
    for k, val in headers:
        h.send_header(k, val)
    h.send_header("Content-Length", str(len(body)))
    h.end_headers()
    req.sent = True
    if req.method != "HEAD":
        h.wfile.write(body)


ROUTES = [
    ("PUT", r"^/api/me/avatar$", put_mine, "viewer"),
    ("DELETE", r"^/api/me/avatar$", delete_mine, "viewer"),
    ("DELETE", r"^/api/admin/users/(\d{1,12})/avatar$", admin_delete, "admin"),
    ("GET", r"^/api/avatars/(\d{1,12})\.jpg$", get_picture, "viewer"),
]
