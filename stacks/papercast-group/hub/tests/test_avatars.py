"""Profile pictures (hub/avatars.py, static/avatar.js) against a real hub with password sign-in
(the production mode: real sessions, the CSRF fence), and the page in a headless Chrome.

    cd stacks/papercast-group && python3 -m unittest hub.tests.test_avatars -v

The hub takes only a small JPEG (the page makes it: the middle square, 256 x 256, quality 0.85),
checks its headers and never decodes it: a PNG or HTML sent as a JPEG, a picture too big, too
small or not square is refused. It serves the picture with its own CSP and nosniff, cached by
version. The version rides in every user object the page reads, and an event tells open pages.
An admin takes anyone's down. Fake people and papers only.

The browser part skips when no headless Chrome or no `websocket-client` is available."""
from __future__ import annotations

import http.client
import json
import os
import re
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
import sys  # noqa: E402
sys.path.insert(0, str(HERE.parents[1]))            # stacks/papercast-group
sys.path.insert(0, str(HERE))
from hub import accounts, app, avatars, db, events  # noqa: E402
from hub import config as C                         # noqa: E402

PUBLIC = "https://papercast.example"
SECRET = "test-secret-0123456789abcdef"
PW = "a long enough passphrase"


def jpeg(w: int = 256, h: int = 256, n: int = 3000, seed: int | None = None, comps: int = 3, marker: int = 0xC0) -> bytes:
    """The headers of a baseline JPEG (SOI, JFIF, SOF, SOS) around n bytes of made-up scan data,
    then EOI. Nothing here decodes it, and neither does the hub."""
    rnd = (bytes((seed * 7 + i * 13) % 251 for i in range(n)) if seed is not None else os.urandom(n)).replace(b"\xff", b"\x00")
    app0 = b"\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    sof = (bytes([0xFF, marker]) + (8 + 3 * comps).to_bytes(2, "big") + bytes([8]) + h.to_bytes(2, "big")
           + w.to_bytes(2, "big") + bytes([comps]) + b"".join(bytes([i + 1, 0x11, 0]) for i in range(comps)))
    sos = b"\xff\xda" + (6 + 2 * comps).to_bytes(2, "big") + bytes([comps]) + b"".join(bytes([i + 1, 0]) for i in range(comps)) + b"\x00\x3f\x00"
    return b"\xff\xd8" + app0 + sof + sos + rnd + b"\xff\xd9"


PNG = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x01\x00\x00\x00\x01\x00\x08\x02\x00\x00\x00" + b"\0" * 400
       + b"\x00\x00\x00\x00IEND\xaeB`\x82")
HTML = b"<!doctype html><html><body><script>window.__pwned = 1</script>" + b" " * 400 + b"</body></html>"


class Hub:
    def __init__(self):
        self.dir = tempfile.mkdtemp(prefix="pcg-av-")
        self.env = {"PCG_DATA": self.dir, "PCG_PORT": "0", "PCG_AUTH": "password", "PCG_SECRET": SECRET,
                    "PCG_PUBLIC_URL": PUBLIC, "PCG_ADMIN_EMAILS": ""}
        self.cfg = C.load(self.env)
        from hub import layout              # no background layouts: they outlive this hub
        layout.AUTO = False
        self.srv = app.serve(self.cfg)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.n = 0

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def req(self, method, path, body=None, *, cookie=None, ctype=None, headers=None, csrf=True, length=None):
        """(status, JSON or bytes, response). A bytes body goes as it is; `length` declares one
        that is never sent."""
        h = {}
        if cookie:
            h["Cookie"] = cookie
        if csrf and method not in ("GET", "HEAD"):
            h["X-PCG"] = "1"
        data = None
        if isinstance(body, (bytes, bytearray)):
            data = bytes(body)
            h["Content-Type"] = ctype or "image/jpeg"
        elif body is not None:
            data = json.dumps(body).encode()
            h["Content-Type"] = "application/json"
        h.update(headers or {})
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        try:
            if length is not None:
                c.putrequest(method, path)
                for k, v in h.items():
                    c.putheader(k, v)
                c.putheader("Content-Length", str(length))
                c.endheaders()
            else:
                c.request(method, path, body=data, headers=h)
            r = c.getresponse()
            raw = r.read()
        finally:
            c.close()
        try:
            j = json.loads(raw)
        except ValueError:
            j = raw
        return r.status, j, r

    def person(self, code, name, role="contributor"):
        """Someone on the list with their own password, signed in: (user id, cookie)."""
        uid = accounts.allow(self.cfg, code)["user_id"]
        db.conn().execute("UPDATE users SET pw_hash = ?, name = ?, role = ? WHERE id = ?", (accounts.hash_password(PW), name, role, uid))
        accounts.reset_limits()
        s, j, r = self.req("POST", "/api/auth/login", {"login": code, "password": PW})
        assert s == 200, j
        return uid, r.getheader("Set-Cookie").split(";")[0]

    def stamp(self):
        self.n += 1
        return f"2026-09-01T08:{self.n // 60:02d}:{self.n % 60:02d}Z"

    def paper(self, title, by) -> tuple:
        """A paper with one ready episode made by `by`: (paper id, episode id)."""
        pid, eid, at = db.new_id("p_"), db.new_id("e_"), self.stamp()
        c = db.conn()
        c.execute("INSERT INTO papers(id, title, title_norm, authors, year, tags, created_by, created_at) VALUES (?, ?, ?, '[]', 2024, '[]', ?, ?)",
                  (pid, title, db.norm_title(title), by, at))
        c.execute("INSERT INTO episodes(id, paper_id, made_by, state, duration_s, base_version, created_at, updated_at) "
                  "VALUES (?, ?, ?, 'ready', 60, 1, ?, ?)", (eid, pid, by, at, at))
        return pid, eid


def sub_events(kind):
    s = events.subscribe(None)
    got = []

    def drain():
        while True:
            try:
                _, k, d = s.q.get_nowait()
            except Exception:
                return got
            if k == kind:
                got.append(d)
    return s, drain


class AvatarServer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.hub = Hub()
        cls.leo, cls.leo_c = cls.hub.person("yl6719", "Leo", "admin")
        cls.bob, cls.bob_c = cls.hub.person("bb101", "Bob")
        cls.cat, cls.cat_c = cls.hub.person("cc101", "Cat", "viewer")
        cls.pid, cls.eid = cls.hub.paper("A Fake Paper About Pictures", cls.bob)

    @classmethod
    def tearDownClass(cls):
        cls.hub.close()

    def setUp(self):
        avatars.reset_limits()

    def tearDown(self):
        for uid in (self.leo, self.bob, self.cat):
            avatars.remove(self.hub.cfg, uid)

    def put(self, body, cookie=None, **kw):
        return self.hub.req("PUT", "/api/me/avatar", body, cookie=cookie or self.bob_c, **kw)

    def files(self):
        d = self.hub.cfg.data / "avatars"
        return sorted(p.name for p in d.iterdir()) if d.is_dir() else []

    # ------------------------------------------------------------------ upload and checks
    def test_upload_stores_the_bytes_under_a_content_name(self):
        b = jpeg(seed=1)
        s, j, _ = self.put(b)
        self.assertEqual(s, 200, j)
        v = j["avatar"]
        self.assertRegex(v, r"^[0-9a-f]{16}$")
        self.assertEqual(self.files(), [f"{self.bob}-{v}.jpg"])
        self.assertEqual((self.hub.cfg.data / "avatars" / f"{self.bob}-{v}.jpg").read_bytes(), b)
        # the same bytes again: the same version, the same file; new bytes: the old file goes
        self.assertEqual(self.put(b)[1]["avatar"], v)
        self.assertEqual(self.files(), [f"{self.bob}-{v}.jpg"])
        s, j2, _ = self.put(jpeg(seed=2))
        self.assertNotEqual(j2["avatar"], v)
        self.assertEqual(self.files(), [f"{self.bob}-{j2['avatar']}.jpg"])
        self.assertEqual(db.conn().execute("SELECT width, height, bytes FROM avatars WHERE user_id = ?", (self.bob,)).fetchone()[:],
                         (256, 256, len(jpeg(seed=2))))

    def test_what_is_refused(self):
        cases = [
            (PNG, "image/jpeg", 400, "not_jpeg"),                       # a PNG that says it is a JPEG
            (HTML, "image/jpeg", 400, "not_jpeg"),                      # a page that says it is one
            (b"\xff\xd8\xff" + HTML, "image/jpeg", 400, "not_jpeg"),   # a JPEG's first bytes, then HTML
            (jpeg()[:-2], "image/jpeg", 400, "not_jpeg"),               # cut short: no end of image
            (b"\xff\xd8" + jpeg()[2:].replace(b"\xff\xc0", b"\xff\xc4", 1), "image/jpeg", 400, "not_jpeg"),   # no frame header
            (jpeg(comps=4), "image/jpeg", 400, "not_jpeg"),             # CMYK: not what a canvas makes
            (jpeg(16, 16), "image/jpeg", 400, "bad_size"),
            (jpeg(2048, 2048), "image/jpeg", 400, "bad_size"),
            (jpeg(256, 200), "image/jpeg", 400, "not_square"),
            (b"", "image/jpeg", 400, "not_jpeg"),
            (jpeg(), "image/png", 415, "bad_type"),                     # a JPEG under another type
            (jpeg(), "text/html", 415, "bad_type"),
        ]
        for body, ctype, code, err in cases:
            s, j, _ = self.put(body, ctype=ctype)
            self.assertEqual((s, j["error"] if isinstance(j, dict) else j), (code, err), (body[:12], ctype))
        # a scan before the frame header
        b = jpeg()
        sof_at, sos_at = b.index(b"\xff\xc0"), b.index(b"\xff\xda")
        sof, sos = b[sof_at:sos_at], b[sos_at:sos_at + 14]
        swapped = b[:sof_at] + sos + sof + b[sos_at + 14:]
        self.assertEqual(self.put(swapped)[1]["error"], "not_jpeg")
        # too big: over 200 KB declared is refused before it is read; the hub still answers
        s, j, _ = self.put(None, length=avatars.AVATAR_MAX + 1, headers={"Content-Type": "image/jpeg"})
        self.assertEqual((s, j["error"]), (413, "too_large"))
        self.assertEqual(self.hub.req("GET", "/api/me", cookie=self.bob_c)[0], 200)
        # nearly square is square enough; a progressive JPEG (SOF2) is one too
        self.assertEqual(self.put(jpeg(256, 250))[0], 200)
        self.assertEqual(self.put(jpeg(marker=0xC2))[0], 200)
        self.assertEqual(self.put(jpeg(32, 32))[0], 200)
        self.assertEqual(self.put(jpeg(1024, 1024))[0], 200)
        self.assertEqual(len(self.files()), 1)
        self.assertEqual([p for p in self.files() if p.startswith(".tmp")], [])

    def test_the_fence(self):
        # signed out, no page header, another site: nothing is stored
        self.assertEqual(self.hub.req("PUT", "/api/me/avatar", jpeg())[0], 401)
        s, j, _ = self.put(jpeg(), csrf=False)
        self.assertEqual((s, j["error"]), (403, "csrf"))
        s, j, _ = self.put(jpeg(), headers={"Origin": "https://evil.example"})
        self.assertEqual((s, j["error"]), (403, "cross_origin"))
        s, j, _ = self.put(jpeg(), headers={"Sec-Fetch-Site": "cross-site"})
        self.assertEqual((s, j["error"]), (403, "cross_origin"))
        self.assertEqual(self.hub.req("DELETE", "/api/me/avatar", cookie=self.bob_c, csrf=False)[0], 403)
        self.assertEqual(self.files(), [])
        # from the page's own origin it goes through
        self.assertEqual(self.put(jpeg(), headers={"Origin": PUBLIC})[0], 200)

    def test_rate_limit(self):
        for i in range(avatars.UPLOAD_LIMIT):
            self.assertEqual(self.put(jpeg(seed=10 + i))[0], 200, i)
        self.assertEqual(self.put(PNG)[1]["error"], "not_jpeg")          # a refused file is refused, not counted
        s, j, r = self.put(jpeg(seed=99))
        self.assertEqual((s, j["error"], r.getheader("Retry-After")), (429, "slow_down", "60"))
        self.assertEqual(self.put(jpeg(seed=99), cookie=self.cat_c)[0], 200)     # per person
        avatars.reset_limits()
        self.assertEqual(self.put(jpeg(seed=99))[0], 200)

    # ------------------------------------------------------------------ serving
    def test_serving(self):
        b = jpeg(seed=3)
        v = self.put(b)[1]["avatar"]
        path = f"/api/avatars/{self.bob}.jpg"
        s, got, r = self.hub.req("GET", f"{path}?v={v}", cookie=self.cat_c)     # anyone signed in
        self.assertEqual((s, got), (200, b))
        self.assertEqual((r.getheader("Content-Type"), r.getheader("X-Content-Type-Options"), r.getheader("Content-Security-Policy")),
                         ("image/jpeg", "nosniff", "default-src 'none'; sandbox"))
        self.assertEqual((r.getheader("ETag"), r.getheader("Cache-Control")), (f'"{v}"', "private, max-age=31536000, immutable"))
        self.assertEqual(r.getheader("Content-Length"), str(len(b)))
        # without its version (or an old one), the browser asks again each time
        for q in ("", "?v=0123456789abcdef"):
            s, got, r = self.hub.req("GET", path + q, cookie=self.cat_c)
            self.assertEqual((s, got, r.getheader("Cache-Control")), (200, b, "private, no-cache"), q)
        s, got, r = self.hub.req("GET", path, cookie=self.cat_c, headers={"If-None-Match": f'"{v}"'})
        self.assertEqual((s, got, r.getheader("ETag")), (304, b"", f'"{v}"'))
        s, got, r = self.hub.req("HEAD", f"{path}?v={v}", cookie=self.cat_c)
        self.assertEqual((s, got, r.getheader("Content-Type")), (200, b"", "image/jpeg"))
        # signed out; someone with no picture; no such person
        self.assertEqual(self.hub.req("GET", path)[0], 401)
        self.assertEqual(self.hub.req("GET", f"/api/avatars/{self.cat}.jpg", cookie=self.cat_c)[0], 404)
        self.assertEqual(self.hub.req("GET", "/api/avatars/999999.jpg", cookie=self.cat_c)[0], 404)
        # the page may show it: its CSP lets it load pictures from the hub itself
        self.assertIn("img-src 'self'", app.PAGE_CSP)

    # ------------------------------------------------------------------ removing
    def test_delete_own_and_admin_delete(self):
        v = self.put(jpeg(seed=4))[1]["avatar"]
        s, j, _ = self.hub.req("DELETE", "/api/me/avatar", cookie=self.bob_c)
        self.assertEqual((s, j), (200, {"avatar": None}))
        self.assertEqual(self.files(), [])
        self.assertEqual(self.hub.req("GET", f"/api/avatars/{self.bob}.jpg?v={v}", cookie=self.cat_c)[0], 404)
        self.assertEqual(self.hub.req("DELETE", "/api/me/avatar", cookie=self.bob_c)[0], 200)      # none: still fine
        # an admin takes anyone's down; nobody else can
        self.put(jpeg(seed=5))
        for who in (self.cat_c, self.bob_c):
            self.assertEqual(self.hub.req("DELETE", f"/api/admin/users/{self.bob}/avatar", cookie=who)[0], 403)
        self.assertEqual(len(self.files()), 1)
        s, j, _ = self.hub.req("DELETE", f"/api/admin/users/{self.bob}/avatar", cookie=self.leo_c)
        self.assertEqual((s, j), (200, {"user_id": self.bob, "avatar": None}))
        self.assertEqual(self.files(), [])
        self.assertEqual(self.hub.req("DELETE", "/api/admin/users/999999/avatar", cookie=self.leo_c)[0], 404)
        self.assertEqual(self.hub.req("DELETE", f"/api/admin/users/{self.bob}/avatar", cookie=self.leo_c, csrf=False)[0], 403)

    # ------------------------------------------------------------------ everywhere a person is named
    def test_the_version_in_the_user_objects_and_the_event(self):
        sub, drain = sub_events("avatar")
        try:
            v = self.put(jpeg(seed=6))[1]["avatar"]
            lv = self.put(jpeg(seed=7), cookie=self.leo_c)[1]["avatar"]
            me = self.hub.req("GET", "/api/me", cookie=self.bob_c)[1]
            self.assertEqual((me["id"], me["avatar"]), (self.bob, v))
            self.assertIsNone(self.hub.req("GET", "/api/me", cookie=self.cat_c)[1]["avatar"])
            self.assertEqual(self.hub.req("PUT", "/api/me", {"name": "Bob B"}, cookie=self.bob_c)[1]["avatar"], v)
            cfg = self.hub.req("GET", "/api/config", cookie=self.cat_c)[1]
            self.assertEqual((cfg["me"]["avatar"], cfg["avatars"]), (None, {str(self.bob): v, str(self.leo): lv}))
            lib = self.hub.req("GET", "/api/library", cookie=self.cat_c)[1]["papers"]
            self.assertEqual([e["made_by"] for p in lib for e in p["episodes"]], [{"id": self.bob, "name": "Bob B", "avatar": v}])
            one = self.hub.req("GET", f"/api/papers/{self.pid}", cookie=self.cat_c)[1]
            self.assertEqual(one["episodes"][0]["made_by"]["avatar"], v)
            # comments: the answer, the list, and the event
            csub, cdrain = sub_events("comment")
            try:
                s, c, _ = self.hub.req("POST", f"/api/papers/{self.pid}/comments", {"body": "Nice at 0:10."}, cookie=self.bob_c)
                self.assertEqual((s, c["user"]), (201, {"id": self.bob, "name": "Bob B", "avatar": v}))
                self.hub.req("POST", f"/api/papers/{self.pid}/comments", {"body": "Agreed."}, cookie=self.cat_c)
                got = self.hub.req("GET", f"/api/papers/{self.pid}/comments", cookie=self.cat_c)[1]["comments"]
                self.assertEqual([(x["user"]["name"], x["user"]["avatar"]) for x in got], [("Bob B", v), ("Cat", None)])
                self.assertEqual(cdrain()[0]["comment"]["user"]["avatar"], v)
            finally:
                events.unsubscribe(csub)
            # the board: its items and the pinned notice
            self.assertEqual(self.hub.req("POST", "/api/board/notice", {"body": "Reading group at 3."}, cookie=self.leo_c)[0], 201)
            board = self.hub.req("GET", "/api/board?limit=50", cookie=self.cat_c)[1]
            pics = {it["user"]["id"]: it["user"]["avatar"] for it in board["items"]}
            self.assertEqual((pics[self.bob], pics[self.cat]), (v, None))
            self.assertEqual(board["notice"]["user"]["avatar"], lv)
            # the admin's list of people
            users = {u["id"]: u for u in self.hub.req("GET", "/api/admin/users", cookie=self.leo_c)[1]["users"]}
            self.assertEqual((users[self.bob]["avatar"], users[self.leo]["avatar"], users[self.cat]["avatar"]), (v, lv, None))
            # removed: null everywhere again
            self.hub.req("DELETE", f"/api/admin/users/{self.bob}/avatar", cookie=self.leo_c)
            self.assertIsNone(self.hub.req("GET", "/api/me", cookie=self.bob_c)[1]["avatar"])
            lib = self.hub.req("GET", "/api/library", cookie=self.cat_c)[1]["papers"]
            self.assertIsNone(lib[0]["episodes"][0]["made_by"]["avatar"])
            self.assertEqual(self.hub.req("GET", "/api/config", cookie=self.cat_c)[1]["avatars"], {str(self.leo): lv})
            self.assertEqual(drain(), [{"user_id": self.bob, "avatar": v}, {"user_id": self.leo, "avatar": lv},
                                       {"user_id": self.bob, "avatar": None}])
        finally:
            events.unsubscribe(sub)
            db.conn().execute("DELETE FROM comments")
            db.conn().execute("UPDATE board_notices SET unpinned_at = ?", (db.now(),))
            db.conn().execute("UPDATE users SET name = 'Bob' WHERE id = ?", (self.bob,))

    def test_the_header_check_alone(self):
        self.assertEqual(avatars.jpeg_size(jpeg(300, 290)), (300, 290))
        self.assertIsNone(avatars.jpeg_size(PNG))
        self.assertIsNone(avatars.jpeg_size(b"\xff\xd8\xff\xd9"))
        # fill bytes and restart markers between segments are allowed, as a JPEG may have them
        b = jpeg(64, 64)
        i = b.index(b"\xff\xc0")
        self.assertEqual(avatars.jpeg_size(b[:i] + b"\xff\xff" + b[i:]), (64, 64))
        # a segment that says it is longer than the file
        self.assertIsNone(avatars.jpeg_size(b"\xff\xd8\xff\xe0\xff\xff" + b"\0" * 200 + b"\xff\xd9"))


# ============================================================ the page in a browser

try:
    import websocket  # noqa: F401
    from web_cdp import Browser, find_chrome
    NO_BROWSER = None if find_chrome() else "no headless Chrome"
except ImportError:
    NO_BROWSER = "websocket-client not installed"

# Leo's tap check (test_accounts.TAPS) over the Settings body: every control there under 44 x 44
# px, or that a tap on its edge does not reach. (The tab strip above it is test_accounts' test_6.)
TAPS = r"""(() => {
  const q = 'a[href], button, input:not([type=hidden]), select, textarea, summary, label, [role=button], [role=link],' +
            ' [role=slider], [role=menuitem], [tabindex]:not([tabindex="-1"])';
  const top = document.querySelector('.menu');
  const open = document.body.classList.contains('open');
  const name = (e) => `${e.tagName.toLowerCase()}${e.id ? '#' + e.id : ''} "${(e.getAttribute('aria-label') || e.textContent || e.placeholder || '').trim().slice(0, 24)}"`;
  const bad = [];
  for (const e of document.getElementById('set-body').querySelectorAll(q)) {
    const cs = getComputedStyle(e);
    if (e.closest('[hidden]') || cs.visibility !== 'visible' || cs.pointerEvents === 'none' || !e.getClientRects().length) continue;
    if ((top && !top.contains(e)) || (open && e.closest('#gcol, #gpl'))) continue;
    const r = e.getBoundingClientRect();
    if (r.width < 43.5 || r.height < 43.5) bad.push(`${name(e)} is ${r.width.toFixed(1)} x ${r.height.toFixed(1)}`);
    let fixed = false;
    for (let a = e; a; a = a.parentElement) if (['fixed', 'sticky'].includes(getComputedStyle(a).position)) { fixed = true; break; }
    if (!fixed) e.scrollIntoView({block: 'center', inline: 'nearest'});
    const b = e.getBoundingClientRect();
    for (const [x, y] of [[b.left + 1, b.top + b.height / 2], [b.right - 1, b.top + b.height / 2], [b.left + b.width / 2, b.top + 1], [b.left + b.width / 2, b.bottom - 1]]) {
      if (x < 0 || y < 0 || x >= innerWidth || y >= innerHeight) continue;
      const h = document.elementFromPoint(x, y);
      if (h !== e && !e.contains(h)) bad.push(`${name(e)} is covered at ${Math.round(x)},${Math.round(y)} by ${h ? h.tagName.toLowerCase() + (h.id ? '#' + h.id : '') : 'nothing'}`);
    }
  }
  return JSON.stringify(bad);
})()"""

# A 600 x 400 picture made on a canvas and put in the file input as a PNG, as a person's pick
# would be: the middle 400 x 400 is blue, the rest red, so a centre crop is all blue. With `mb`:
# a file of that many MB of zeros, of that type, instead.
PICK = r"""(async (type, mb) => {
  const inp = document.getElementById('acct-av-file');
  let file;
  if (mb === undefined) {
    const cv = document.createElement('canvas'); cv.width = 600; cv.height = 400;
    const g = cv.getContext('2d');
    g.fillStyle = '#ff0000'; g.fillRect(0, 0, 600, 400);
    g.fillStyle = '#0000ff'; g.fillRect(100, 0, 400, 400);
    const blob = await new Promise((r) => cv.toBlob(r, 'image/png'));
    file = new File([blob], 'me.png', { type });
  } else {
    file = new File([new Uint8Array(Math.round(mb * 1024 * 1024))], 'x.bin', { type });
  }
  const dt = new DataTransfer(); dt.items.add(file);
  inp.files = dt.files;
  inp.dispatchEvent(new Event('change'));
  return true;
})"""

# The colour at (x, y) of a drawn <img>: [r, g, b].
PIXEL = r"""((img, x, y) => {
  const cv = document.createElement('canvas'); cv.width = img.naturalWidth; cv.height = img.naturalHeight;
  const g = cv.getContext('2d'); g.drawImage(img, 0, 0);
  return [...g.getImageData(x, y, 1, 1).data.slice(0, 3)];
})"""


@unittest.skipIf(NO_BROWSER, NO_BROWSER or "")
class AvatarPage(unittest.TestCase):
    """Settings > Account: upload a picture made in the page, see it next to your name in a
    comment and on the board, and someone else's arrive live; an admin takes one down from Users;
    remove your own. No errors in the console; on a phone every control is a 44 x 44 px tap."""

    @classmethod
    def setUpClass(cls):
        cls.hub = Hub()
        cls.base = f"http://127.0.0.1:{cls.hub.port}"
        cls.hub.cfg.public_url = cls.base                   # the browser's origin (CSRF), plain http here
        cls.leo, _ = cls.hub.person("yl6719", "Leo Lou", "admin")
        cls.bob, cls.bob_c = cls.hub.person("bb101", "Bob")
        cls.pid, cls.eid = cls.hub.paper("A Fake Paper With Faces", cls.bob)
        cls.b = Browser()
        cls.b.call("Log.enable")
        cls.b.viewport(1280, 860)
        cls.b.goto(cls.base + "/signin?next=/")
        cls.b.wait_js("location.pathname === '/signin' && !document.getElementById('signin').hidden", 10, "the sign-in form")
        cls.b.js("{ const f = (s, v) => { const e = document.querySelector(s); e.value = v; e.dispatchEvent(new Event('input')); };"
                 f" f('#login', 'yl6719'); f('#password', {json.dumps(PW)}); document.getElementById('go').click(); }}")
        cls.b.wait_js("location.pathname === '/' && document.title === 'Papers'", 10, "signed in")

    @classmethod
    def tearDownClass(cls):
        cls.b.close()
        cls.hub.close()

    def setUp(self):
        avatars.reset_limits()
        self.b.pump(0.05)
        self.b.events.clear()

    def tearDown(self):
        self.b.pump(0.3)
        errs = []
        for m in self.b.events:
            meth, p = m.get("method"), m.get("params", {})
            if meth == "Runtime.exceptionThrown":
                d = p.get("exceptionDetails", {})
                errs.append(f"exception: {(d.get('exception') or {}).get('description') or d.get('text')}")
            elif meth == "Runtime.consoleAPICalled" and p.get("type") in ("error", "assert"):
                errs.append("console.error: " + " ".join(str(a.get("value", a.get("description", ""))) for a in p.get("args", [])))
            elif meth == "Log.entryAdded" and p.get("entry", {}).get("level") == "error":
                e = p["entry"]
                errs.append(f"log ({e.get('source')}): {e.get('text')} {e.get('url', '')}")
        self.b.events.clear()
        self.assertEqual(errs, [], "errors in the console")

    def js(self, expr):
        return self.b.js(expr)

    def wait(self, expr, what, timeout=10):
        return self.b.wait_js(expr, timeout, what)

    def taps(self, where):
        bad = json.loads(self.js(TAPS))
        self.assertEqual(bad, [], f"phone, {where}: {len(bad)} tap target(s) too small or covered:\n" + "\n".join(bad))

    def real_jpeg(self, colour) -> bytes:
        """A 256 x 256 JPEG of one colour, made by this browser's canvas (so a browser can show it)."""
        import base64
        return base64.b64decode(self.js(
            "(async () => { const cv = document.createElement('canvas'); cv.width = cv.height = 256;"
            f" const g = cv.getContext('2d'); g.fillStyle = {json.dumps(colour)}; g.fillRect(0, 0, 256, 256);"
            " const b = await new Promise((r) => cv.toBlob(r, 'image/jpeg', 0.85)); let s = '';"
            " for (const x of new Uint8Array(await b.arrayBuffer())) s += String.fromCharCode(x); return btoa(s); })()"))

    def account(self):
        self.js("location.hash = 'settings=account'")
        self.wait("!!document.getElementById('acct-av-up') && !!document.querySelector('#acct-av .av')", "the account tab")

    def test_1_upload_see_it_everywhere_remove(self):
        b = self.b
        self.account()
        # none yet: initials on a colour, no Remove
        self.assertEqual(self.js("document.querySelector('#acct-av .av').dataset.i"), "LL")
        self.assertTrue(self.js("!document.querySelector('#acct-av img') && document.getElementById('acct-av-rm').hidden"))
        # the section: its heading, the picture, Upload and Remove, and no sentences (Leo: none)
        sec = "document.getElementById('acct-av').closest('.av-edit')"
        self.assertEqual(self.js(f"{sec}.previousElementSibling.textContent"), "Profile picture")
        self.assertEqual(self.js(f"{sec}.nextElementSibling.id"), "acct-av-file")
        self.assertEqual(self.js("document.getElementById('acct-av-up').textContent"), "Upload")
        self.assertEqual(self.js(f"{sec}.textContent"), "UploadRemove")    # the two buttons' words, nothing else
        self.assertEqual(self.js("document.getElementById('acct-av-file').accept"), "image/*")
        # not a picture, or too big: a word or two, and nothing is sent
        b.js(f"({PICK})('text/plain', 0.01)")
        self.wait("document.getElementById('acct-av-msg').textContent === 'Not an image'", "not a picture")
        b.js(f"({PICK})('image/jpeg', 10.5)")
        self.wait("document.getElementById('acct-av-msg').textContent === 'Too big'", "too big")
        b.js(f"({PICK})('image/png', 0.01)")                               # says it is a PNG, is not one
        self.wait("document.getElementById('acct-av-msg').textContent === 'Not an image'", "an undecodable file")
        self.assertEqual(self.hub.req("GET", f"/api/avatars/{self.leo}.jpg", cookie=self.bob_c)[0], 404)
        # a 600 x 400 picture: the page sends the middle square, 256 x 256, as a JPEG
        b.js(f"({PICK})('image/png')")
        self.wait("!!document.querySelector('#acct-av .av img') && !document.getElementById('acct-av-rm').hidden", "saved")
        self.assertEqual(self.js("document.getElementById('acct-av-msg').textContent"), "")
        v = avatars.version_of(self.leo)
        self.assertIsNotNone(v)
        stored = (self.hub.cfg.data / "avatars" / f"{self.leo}-{v}.jpg").read_bytes()
        self.assertEqual(avatars.jpeg_size(stored), (256, 256))
        self.assertLess(len(stored), 40 * 1024)
        img = "document.querySelector('#acct-av .av img')"
        self.wait(f"!!{img} && {img}.complete && {img}.naturalWidth === 256", "the picture shown")
        self.assertEqual(self.js(f"{img}.getAttribute('src')"), f"/api/avatars/{self.leo}.jpg?v={v}")
        for x, y in ((3, 3), (252, 3), (128, 128), (3, 252), (252, 252)):          # all blue: the red sides were cut off
            r, g, bl = self.js(f"({PIXEL})({img}, {x}, {y})")
            self.assertTrue(bl > 200 and r < 60 and g < 60, (x, y, r, g, bl))
        self.assertFalse(self.js("document.getElementById('acct-av-rm').hidden"))
        # next to Leo's name in a comment he writes, and on the board
        self.js(f"location.hash = 'p={self.pid}'")
        self.wait("!document.getElementById('comments').hidden && !!document.querySelector('#c-list .c-none')", "the comments")
        self.js("{ const t = document.getElementById('c-text'); t.value = 'A face now, at 0:10.'; t.dispatchEvent(new Event('input')); }")
        self.js("document.getElementById('c-post').click()")
        meta = "document.querySelector('#c-list .c-item .c-meta')"
        self.wait(f"!!{meta} && !!{meta}.querySelector('.av img')", "the picture by the comment")
        self.assertEqual(self.js(f"{meta}.querySelector('.av img').getAttribute('src')"), f"/api/avatars/{self.leo}.jpg?v={v}")
        self.assertTrue(self.js(f"{meta}.textContent.startsWith('Leo Lou · ')"))           # the circle adds no text
        self.js("document.getElementById('bell-btn').click()")                # the board, from the bell
        self.wait("!document.getElementById('bell-panel').hidden", "the board open")
        bd = "[...document.querySelectorAll('#bd-list .bd-item')].find(x => x.dataset.kind === 'comment')"
        self.wait(f"!!({bd}) && !!({bd}).querySelector('.bd-link .av img')", "the picture on the board")
        self.assertEqual(self.js(f"({bd}).querySelector('.bd-text').textContent"), "Leo Lou commented on A Fake Paper With Faces")
        # Bob's has none: initials (by his upload on the board); Bob uploads one elsewhere: this page shows it at once
        row = "[...document.querySelectorAll('#bd-list .bd-item')].find(x => x.dataset.kind === 'upload').querySelector('.bd-link')"
        self.wait(f"!!({row}) && !!({row}).querySelector('.av')", "the upload's maker on the board")
        self.assertEqual(self.js(f"(r => [r.querySelector('.av').dataset.i, !r.querySelector('.av img'), r.querySelector('.bd-text').textContent])({row})"),
                         ["B", True, "Bob uploaded A Fake Paper With Faces"])
        s, j, _ = self.hub.req("PUT", "/api/me/avatar", self.real_jpeg("#00aa00"), cookie=self.bob_c)
        self.assertEqual(s, 200, j)
        bv = j["avatar"]
        self.wait(f"(r => !!r.querySelector('.av img') && r.querySelector('.av img').getAttribute('src') === '/api/avatars/{self.bob}.jpg?v={bv}')({row})",
                  "Bob's picture, live")
        self.js("document.getElementById('bell-btn').click()")
        self.wait("document.getElementById('bell-panel').hidden", "the board closed")
        # an admin takes Bob's down from Users: gone from the row too
        self.js("location.hash = 'settings=users'")
        item = f"#users .item[data-id=\"{self.bob}\"]"
        self.wait(f"!!document.querySelector('{item} > .av')", "the users list")
        self.assertTrue(self.js(f"!!document.querySelector('#users .item[data-id=\"{self.leo}\"] > .av img')"))
        self.js(f"document.querySelector('{item} .icon-btn').click()")
        self.wait("!!document.querySelector('.menu')", "Bob's menu")
        self.assertIn("Remove their picture", self.js("[...document.querySelectorAll('.menu button')].map(b => b.textContent)"))
        self.js("[...document.querySelectorAll('.menu button')].find(b => b.textContent === 'Remove their picture').click()")
        self.wait(f"!document.querySelector('{item} > .av img')", "Bob's picture gone from the list")
        self.assertIsNone(avatars.version_of(self.bob))
        self.assertEqual(self.js(f"!!({row}).querySelector('.av img')"), False)              # gone from the board too
        # Leo removes his own: initials again, by his comment too
        self.account()
        self.js("document.getElementById('acct-av-rm').click()")
        self.wait("!document.querySelector('#acct-av img') && document.getElementById('acct-av-rm').hidden", "removed")
        self.assertEqual(self.js("document.getElementById('acct-av-msg').textContent"), "")
        self.assertTrue(self.js("document.getElementById('acct-av-rm').hidden"))
        self.assertIsNone(avatars.version_of(self.leo))
        self.assertEqual(os.listdir(self.hub.cfg.data / "avatars"), [])
        self.assertEqual(self.js("document.querySelectorAll('.av img').length"), 0)
        self.js(f"location.hash = 'p={self.pid}'")
        self.wait(f"!!{meta} && {meta}.querySelector('.av').dataset.i === 'LL' && !{meta}.querySelector('.av img')", "initials by the comment")

    def test_2_phone_tap_targets(self):
        b = self.b
        try:
            b.viewport(390, 844, mobile=True)
            b.goto(self.base + "/")                         # a new page (only the #hash changing would keep the old one)
            b.goto(self.base + "/#settings=account")
            self.wait("!!document.getElementById('acct-av-up')", "the account tab on a phone")
            self.taps("account, no picture")
            b.js(f"({PICK})('image/png')")
            self.wait("!!document.querySelector('#acct-av .av img') && !document.getElementById('acct-av-rm').hidden", "saved")
            self.taps("account, with a picture")
            for w in ("document.documentElement", "document.getElementById('win')"):
                self.assertLessEqual(self.js(f"{w}.scrollWidth - {w}.clientWidth"), 0, f"{w} scrolls sideways")
        finally:
            avatars.remove(self.hub.cfg, self.leo)
            b.viewport(1280, 860)


if __name__ == "__main__":
    unittest.main()
