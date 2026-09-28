"""PCG_AUTH=password: the ledger of allowed Imperial emails, passwords, reset links, rate limits,
the sign-in log and the one email the hub sends (Leo's decision, 2026-09-28: a list of allowed
Imperial addresses, email only for "forgot password", no Google, no Cloudflare Access).

Who has an account: exactly the emails on the ledger (allowed_emails), @ic.ac.uk or
@imperial.ac.uk. Adding one makes the account at once: username = the email's local part
(yl6719@ic.ac.uk is yl6719, fixed), role contributor, and the first password is the username
(users.pw_hash NULL means that). A session signed in with it can do one thing, choose a new
password: every other API answers 403 must_change_password (auth.authenticate), and the pages
send it to /set-password. Removing an email disables the account: its sessions end (users.session_v,
which every pcg_s cookie carries) and its device tokens are revoked; what the person made stays,
credited to them.

Routes (the public ones check CSRF themselves, as /api/join does, since they set cookies):
  GET  /signin, /set-password                  the two pages (static signin.html, setpw.html)
  GET  /api/auth/state                         mode, who, must change?, email set up? (never a 401)
  POST /api/auth/login {"login","password"}    username or email
  POST /api/auth/forgot {"email"}              always the same answer; a link (1 h) if on the list
  POST /api/auth/link {"token","peek"|"password"}  see a reset link / use it (signs this browser in)
  POST /api/auth/password {"current"?,"password"}  change it (no current while the first one stands)
  POST /api/auth/signout, POST /api/auth/signout-all
  admin: GET/POST/DELETE /api/admin/allowed, POST /api/admin/users/<id>/reset-link,
         POST /api/admin/users/<id>/reset-default, GET /api/admin/auth-log

Secrets: passwords as scrypt (stored with n, r, p, so they can rise: an older hash is redone at
the next sign-in); reset links as sha256 only. Failed sign-ins are limited per account and per
client address (CF-Connecting-IP, trusted only from 127.0.0.1, which is where cloudflared
connects from); an unknown account costs the same scrypt and gets the same answer.

From the command line (python3 -m hub.auth ...): bootstrap EMAIL, allow list|add|remove|import,
email-test ADDRESS. See auth.main."""
from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import logging
import os
import queue
import re
import secrets
import smtplib
import ssl
import stat
import threading
import time
import unicodedata
from collections import deque
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid

from . import auth, db
from .app import HTTPError

log = logging.getLogger("pcg.accounts")

IMPERIAL = ("ic.ac.uk", "imperial.ac.uk")
PANEL_DOMAIN = "ic.ac.uk"                   # the panel's box is "<short code>@ic.ac.uk"
# A short code (the email's local part, and so the username): letters, digits, dots, hyphens.
CODE_RX = re.compile(r"^[a-z0-9]+(?:[.-][a-z0-9]+)*$")
CODE_MAX = 64
NEW_ROLE = "contributor"                    # people added to the list can upload from the CLI at once

# scrypt: n = 2**14, r = 8 is 16 MiB and 44 ms per hash on stibnite's Xeon Gold 6226R under
# python3.10 (2**15 was 102 ms, over the budget of about 100 ms). Raise SCRYPT_N here when
# perov's CPU allows: each stored hash names its own parameters, and a weaker one is redone at the
# person's next sign-in.
SCRYPT_N, SCRYPT_R, SCRYPT_P, SCRYPT_LEN = 2 ** 14, 8, 1, 32
SCRYPT_SLOTS = threading.BoundedSemaphore(4)    # 16 MiB each: a flood of sign-ins queues here
PW_MIN, PW_MAX = 10, 256

LINK_EMAIL_S = 3600                          # a link sent by email
LINK_ADMIN_S = 86400                         # one an admin copies into a chat, or bootstrap prints
LOG_KEEP = 2000

# Sign-in failures: 10 per account, 20 per address (a college NAT puts many people behind one
# address) in 15 min; past that each try waits, doubling from 30 s up to 15 min.
FAIL_WINDOW, ACCOUNT_FAILS, ADDRESS_FAILS, BACKOFF_S, BACKOFF_MAX = 900, 10, 20, 30, 900
# Reset links asked for: 3 per email and 10 per address in an hour.
ASK_WINDOW, ASK_PER_EMAIL, ASK_PER_ADDRESS = 3600, 3, 10

SAME_ANSWER = "If that address is on the list, a link is on its way. It works once, for an hour."
SAME_ANSWER_NO_EMAIL = ("This hub cannot send email yet. If that address is on the list, the admins "
                        "can see that you asked: ask one of them to reset your password.")
BAD_LOGIN = "That username or password is not right."

# Common passwords of 10 characters or more (shorter ones fail the length rule anyway), from the
# usual leaked-password lists, plus a few this group would think of first. Compared lowercased.
COMMON = set("""
0123456789 1234567890 0987654321 9876543210 1234554321 12345678910 12345678901 123456789012
1234567891 1234512345 123123123123 111222333444 1472583690 1111111111 0000000000 2222222222
3333333333 4444444444 5555555555 6666666666 7777777777 8888888888 9999999999 11111111111
1q2w3e4r5t 1q2w3e4r5t6y 1q2w3e4r5t6y7u 1qaz2wsx3edc 1qazxsw23edc zaq12wsxcde3 q1w2e3r4t5
q1w2e3r4t5y6 qwertyuiop qwerty1234 qwerty12345 qwerty123456 qwerty123! qwertyqwerty qazwsxedc123
qazwsxedcrfv 123qweasdzxc asdfghjkl1 asdfghjkl; asdfasdfasdf zxcvbnm123 zxcvbnmasdf 123456789a
a123456789 123456789q abc1234567 abcdefghij abcdefg123 abc123abc123 abcabcabcabc 1234567890a
password12 password123 password1234 password12345 password123456 password!1 password1! password2024
password2025 password2026 passw0rd123 p@ssw0rd123 p@ssword123 1password1 mypassword mypassword1
mypassword123 newpassword newpassword1 newpassword123 secretpassword secret1234 masterkey1
iloveyou12 iloveyou123 iloveyou!! welcome123 welcome1234 letmein123 letmein1234 letmeinplease
pleaseletmein changeme123 changeme1234 trustno1234 trustno1trustno1 football123 baseball123
basketball basketball1 michael123 superman123 starwars123 starwars1234 monkey1234 dragon1234
sunshine123 princess123 chocolate1 butterfly1 whatever123 computer123 computer1234 internet123
testtest123 test123456 testing123 administrator administrator1 admin12345 admin123456 google1234
facebook123 liverpool1 manchester1 arsenal123 chelsea123 cambridge1 london1234 london12345
imperial123 imperial1234 imperialcollege imperial2026 papercast1 papercast123 papercast2026
podcast123 podcast1234 summer2025 summer2026 autumn2026 winter2025 winter2026 september2026
correcthorsebatterystaple
""".split()) | {"correct horse battery staple"}


# ---------------------------------------------------------------- small helpers

def _now() -> float:
    return auth._now()


def _iso(t: float | None = None) -> str:
    return auth._iso(_now() if t is None else t)


def is_password_mode(cfg) -> bool:
    return cfg.auth == "password"


def client_addr(req) -> str:
    """Who is asking: behind cloudflared (which connects from 127.0.0.1) the CF-Connecting-IP
    header, from anywhere else the connection's own address (a header from there is anyone's)."""
    ip = req.client_ip
    if ip == "127.0.0.1":
        v = (req.headers.get("CF-Connecting-IP") or "").strip()
        try:
            return str(ipaddress.ip_address(v))
        except ValueError:
            pass
    return ip


def ledger_email(v, short_ok: bool = True) -> str:
    """An address the ledger takes: lowercased, @ic.ac.uk or @imperial.ac.uk, its local part a
    plausible short code. `short_ok`: a bare short code means <code>@ic.ac.uk (the panel's box)."""
    s = v.strip().lower() if isinstance(v, str) else ""
    if not s:
        raise HTTPError(400, "bad_email", "Type a short code, like yl6719." if short_ok else "Type an Imperial email address.")
    if "@" not in s:
        if not short_ok:
            raise HTTPError(400, "bad_email", f"“{s[:40]}” is not an email address.")
        s += "@" + PANEL_DOMAIN
    local, _, domain = s.rpartition("@")
    if domain not in IMPERIAL:
        raise HTTPError(400, "not_imperial", "Only Imperial addresses (@ic.ac.uk or @imperial.ac.uk) can be on the list.")
    if not local or len(local) > CODE_MAX or not CODE_RX.match(local):
        shown = local if local and len(local) <= 40 else (local[:40] + "…" if local else "")
        raise HTTPError(400, "bad_code", f"“{shown}” does not look like an Imperial short code: letters, digits, dots and hyphens only."
                        if shown else "Type a short code, like yl6719.")
    return s


def username_of(email: str) -> str:
    return email.rpartition("@")[0]


# ---------------------------------------------------------------- passwords

def _b64e(b: bytes) -> str:
    return base64.b64encode(b).decode().rstrip("=")


def _b64d(s: str) -> bytes:
    return base64.b64decode(s + "=" * (-len(s) % 4), validate=True)


def _norm(pw: str) -> bytes:
    """NFKC, so the same password typed on a phone and a laptop is the same bytes."""
    return unicodedata.normalize("NFKC", pw).encode("utf-8")


def _scrypt(pw: bytes, salt: bytes, n: int, r: int, p: int, dklen: int) -> bytes:
    with SCRYPT_SLOTS:          # OpenSSL wants 128 * r * n bytes and a little more: give it twice that
        return hashlib.scrypt(pw, salt=salt, n=n, r=r, p=p, dklen=dklen, maxmem=256 * r * (n + p) + (1 << 20))


def hash_password(pw: str) -> str:
    salt = secrets.token_bytes(16)
    key = _scrypt(_norm(pw), salt, SCRYPT_N, SCRYPT_R, SCRYPT_P, SCRYPT_LEN)
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${_b64e(salt)}${_b64e(key)}"


def _parse(stored: str):
    m = re.fullmatch(r"scrypt\$(\d{1,8})\$(\d{1,3})\$(\d{1,3})\$([A-Za-z0-9+/]{16,64})\$([A-Za-z0-9+/]{16,128})", stored or "")
    if not m:
        return None
    n, r, p = int(m[1]), int(m[2]), int(m[3])
    if n < 2 or n & (n - 1) or n > 2 ** 20 or not 1 <= r <= 32 or not 1 <= p <= 16:
        return None
    return n, r, p, _b64d(m[4]), _b64d(m[5])


_dummy = None


def _dummy_hash() -> str:
    """A hash nobody's password matches, at today's parameters: an unknown account costs the same."""
    global _dummy
    if _dummy is None:
        _dummy = hash_password(secrets.token_urlsafe(24))
    return _dummy


def check_password(pw: str, stored: str | None) -> bool:
    """Timing-safe; a missing or unreadable hash still costs one scrypt."""
    got = _parse(stored) if stored else None
    if got is None:
        _parse_and_burn(pw)
        return False
    n, r, p, salt, key = got
    return hmac.compare_digest(_scrypt(_norm(pw), salt, n, r, p, len(key)), key)


def _parse_and_burn(pw) -> None:
    n, r, p, salt, key = _parse(_dummy_hash())
    hmac.compare_digest(_scrypt(_norm(pw if isinstance(pw, str) else ""), salt, n, r, p, len(key)), key)


def outdated(stored: str | None) -> bool:
    got = _parse(stored) if stored else None
    return got is not None and (got[0], got[1], got[2], len(got[4])) < (SCRYPT_N, SCRYPT_R, SCRYPT_P, SCRYPT_LEN)


def password_problem(pw, user: dict) -> str | None:
    """Why this new password is refused, or None. No composition rules: long enough, not a
    common one, not the username or email."""
    if not isinstance(pw, str):
        return "Type a new password."
    s = unicodedata.normalize("NFKC", pw)
    if len(s) < PW_MIN:
        return f"Use at least {PW_MIN} characters (a few words make a good one)."
    if len(s) > PW_MAX:
        return f"Use at most {PW_MAX} characters."
    low = s.lower()
    name = (user.get("username") or "").lower()
    email = (user.get("email") or "").lower()
    # (a username of a few letters would forbid too much: then only the username itself)
    if name and (low == name or len(name) >= 4 and name in low) or email and email in low:
        return "Choose one that does not contain your username or email."
    if low in COMMON or low.strip() in COMMON or len(set(low)) <= 2:
        return "That one is on the lists of the most common passwords: choose another."
    return None


# ---------------------------------------------------------------- rate limits (in memory)

class Limiter:
    """Timestamps of recent failures (or requests) per key, forgotten after `window` seconds.
    Under `limit` nothing waits; at `limit` and past it, each try waits `base` seconds doubling
    per extra failure, up to `cap`, counted from the last one."""

    def __init__(self, limit: int, window: float, base: float = 0.0, cap: float = 0.0):
        self.limit, self.window, self.base, self.cap = limit, window, base, cap
        self.lock = threading.Lock()
        self.hits: dict = {}
        self.noted: dict = {}                    # key -> the lockout already logged (one row each)

    def _fresh(self, key, now):
        q = self.hits.get(key)
        if q is None:
            return None
        while q and q[0] <= now - self.window:
            q.popleft()
        if not q:
            del self.hits[key]
            self.noted.pop(key, None)
            return None
        return q

    def wait(self, key) -> float:
        """Seconds until `key` may try again (0: now)."""
        now = _now()
        with self.lock:
            q = self._fresh(key, now)
            if q is None or len(q) < self.limit:
                return 0.0
            if not self.base:                    # a plain cap: wait until the oldest one ages out
                return max(0.0, q[0] + self.window - now)
            hold = min(self.cap, self.base * 2 ** (len(q) - self.limit))
            return max(0.0, q[-1] + hold - now)

    MAX_KEYS = 20000

    def hit(self, key) -> None:
        now = _now()
        with self.lock:
            if len(self.hits) > self.MAX_KEYS:   # a flood of distinct keys: drop what has aged out,
                for k in list(self.hits):        # then, if still too many, the longest quiet half
                    self._fresh(k, now)
                if len(self.hits) > self.MAX_KEYS // 2:
                    for k in sorted(self.hits, key=lambda k: self.hits[k][-1])[:len(self.hits) - self.MAX_KEYS // 2]:
                        self.hits.pop(k, None)
                        self.noted.pop(k, None)
            self._fresh(key, now)
            self.hits.setdefault(key, deque()).append(now)

    def clear(self, key) -> None:
        with self.lock:
            self.hits.pop(key, None)
            self.noted.pop(key, None)

    def first_refusal(self, key) -> bool:
        """True once per lockout: the log gets one row for it, not one per refused try."""
        with self.lock:
            q = self.hits.get(key)
            mark = q[-1] if q else None
            if self.noted.get(key) == mark:
                return False
            self.noted[key] = mark
            return True

    def reset(self) -> None:
        with self.lock:
            self.hits.clear()
            self.noted.clear()


FAILS_ACCOUNT = Limiter(ACCOUNT_FAILS, FAIL_WINDOW, BACKOFF_S, BACKOFF_MAX)
FAILS_ADDRESS = Limiter(ADDRESS_FAILS, FAIL_WINDOW, BACKOFF_S, BACKOFF_MAX)
ASKS_EMAIL = Limiter(ASK_PER_EMAIL, ASK_WINDOW)
ASKS_ADDRESS = Limiter(ASK_PER_ADDRESS, ASK_WINDOW)


def reset_limits() -> None:
    for lim in (FAILS_ACCOUNT, FAILS_ADDRESS, ASKS_EMAIL, ASKS_ADDRESS):
        lim.reset()


def _slow_down(seconds: float):
    s = max(1, int(seconds + 0.999))
    when = f"{s} seconds" if s < 90 else f"{(s + 59) // 60} minutes"
    raise HTTPError(429, "slow_down", f"Too many tries. Wait {when}, then try again.", retry_after=s)


# ---------------------------------------------------------------- the sign-in log

_log_n = 0


def log_event(kind: str, *, user_id=None, email=None, actor_id=None, ip=None, detail=None, c=None) -> None:
    global _log_n
    c = c or db.conn()
    c.execute("INSERT INTO auth_log(at, kind, user_id, email, actor_id, ip, detail) VALUES (?, ?, ?, ?, ?, ?, ?)",
              (_iso(), kind, user_id, auth.clean_text(email, 120) if email else None, actor_id,
               ip, auth.clean_text(detail, 300) if detail else None))
    _log_n += 1
    if _log_n % 50 == 0:
        c.execute("DELETE FROM auth_log WHERE id <= (SELECT MAX(id) FROM auth_log) - ?", (LOG_KEEP,))


# ---------------------------------------------------------------- accounts and the ledger

ACCOUNT_COLS = ("u.id, u.email, u.name, u.role, u.disabled, u.created_at, u.username, u.pw_hash, u.session_v, "
                "u.last_login_at, u.reset_asked_at, (SELECT 1 FROM allowed_emails a WHERE a.email = u.email) AS listed")


def account(uid=None, *, username=None, email=None, c=None):
    """One users row with its password state and whether its email is on the ledger."""
    c = c or db.conn()
    if uid is not None:
        where, arg = "u.id = ?", uid
    elif username is not None:
        where, arg = "u.username = ?", username
    else:
        where, arg = "u.email = ?", email
    return c.execute(f"SELECT {ACCOUNT_COLS} FROM users u WHERE {where}", (arg,)).fetchone()


def public_user(row) -> dict:
    u = auth._user(row)
    u["must_change"] = row["pw_hash"] is None
    return u


def allow(cfg, email: str, *, note: str = "", by=None, role: str | None = None, ip=None, short_ok: bool = True) -> dict:
    """Put `email` on the ledger and make sure its account exists and is enabled. Returns
    {"email", "username", "user_id", "state": "added" | "back" | "already"}."""
    email = ledger_email(email, short_ok=short_ok)
    name = username_of(email)
    note = auth.clean_text(note, 200)
    now = _iso()
    with db.transaction() as c:
        listed = c.execute("SELECT 1 FROM allowed_emails WHERE email = ?", (email,)).fetchone() is not None
        u = account(email=email, c=c)
        other = account(username=name, c=c)
        if other is not None and (u is None or other["id"] != u["id"]):
            raise HTTPError(409, "username_taken", f"The username {name} already belongs to {other['email']}"
                            + ("" if other["listed"] else " (removed earlier: add that address to bring the account back)") + ".")
        if listed and u is not None and not u["disabled"]:
            return {"email": email, "username": name, "user_id": u["id"], "state": "already"}
        if not listed:
            c.execute("INSERT INTO allowed_emails(email, note, added_by, added_at) VALUES (?, ?, ?, ?)", (email, note, by, now))
        if u is None:
            want = role or ("admin" if email in cfg.admin_emails else NEW_ROLE)
            uid = c.execute("INSERT INTO users(email, name, role, disabled, created_at, username, session_v) VALUES (?, ?, ?, 0, ?, ?, 0)",
                            (email, name, want, now, name)).lastrowid
            state = "added"
        else:
            uid = u["id"]
            # someone removed earlier (or known from another sign-in mode) comes back: enabled,
            # with the password they had; their old sessions and devices stay ended
            c.execute("UPDATE users SET disabled = 0, username = ? WHERE id = ?", (name, uid))
            if role:
                c.execute("UPDATE users SET role = ? WHERE id = ?", (role, uid))
            state = "back" if listed or u["disabled"] else "added"
        log_event("allowed", user_id=uid, email=email, actor_id=by, ip=ip, detail=note or None, c=c)
    return {"email": email, "username": name, "user_id": uid, "state": state}


def other_admins(cfg, c, uid) -> int:
    """Admins who could still sign in without this one (in password mode, only those on the list)."""
    return c.execute("SELECT COUNT(*) FROM users u WHERE u.role = 'admin' AND u.disabled = 0 AND u.id != ? "
                     "AND (? = 0 OR EXISTS (SELECT 1 FROM allowed_emails a WHERE a.email = u.email))",
                     (uid, 1 if is_password_mode(cfg) else 0)).fetchone()[0]


def end_everything(c, uid, *, tokens: bool) -> None:
    """Every session of this account ends (and, with tokens, every device), and no link works."""
    now = _iso()
    c.execute("UPDATE users SET session_v = session_v + 1 WHERE id = ?", (uid,))
    c.execute("UPDATE pw_links SET used_at = ? WHERE user_id = ? AND used_at IS NULL", (now, uid))
    if tokens:
        c.execute("UPDATE tokens SET revoked_at = ? WHERE user_id = ? AND revoked_at IS NULL", (now, uid))


def disallow(cfg, email: str, *, by=None, ip=None) -> dict:
    """Take `email` off the ledger and disable its account (sessions, devices and links end;
    what the person made stays theirs). The last admin stays."""
    e = email.strip().lower() if isinstance(email, str) else ""
    with db.transaction() as c:
        if c.execute("SELECT 1 FROM allowed_emails WHERE email = ?", (e,)).fetchone() is None:
            raise HTTPError(404, "not_listed", f"{e or 'That address'} is not on the list.")
        u = account(email=e, c=c)
        if u is not None and u["role"] == "admin" and not u["disabled"] and not other_admins(cfg, c, u["id"]):
            raise HTTPError(409, "last_admin", "That is the last admin: make someone else an admin first.")
        c.execute("DELETE FROM allowed_emails WHERE email = ?", (e,))
        if u is not None:
            c.execute("UPDATE users SET disabled = 1 WHERE id = ?", (u["id"],))
            end_everything(c, u["id"], tokens=True)
        log_event("removed", user_id=u["id"] if u else None, email=e, actor_id=by, ip=ip, c=c)
    return {"email": e, "user_id": u["id"] if u else None}


def ledger() -> list:
    rows = db.conn().execute(
        "SELECT a.email, a.note, a.added_at, a.added_by, b.name AS added_by_name, u.id AS uid, u.username, u.name, "
        "u.role, u.disabled, u.pw_hash IS NULL AS default_pw, u.last_login_at "
        "FROM allowed_emails a LEFT JOIN users b ON b.id = a.added_by LEFT JOIN users u ON u.email = a.email "
        "ORDER BY a.added_at, a.email").fetchall()
    return [{"email": r["email"], "note": r["note"], "added_at": r["added_at"],
             "added_by": {"id": r["added_by"], "name": r["added_by_name"]} if r["added_by"] is not None else None,
             "user": None if r["uid"] is None else {
                 "id": r["uid"], "username": r["username"], "name": r["name"], "role": r["role"],
                 "disabled": bool(r["disabled"]), "default_password": bool(r["default_pw"]),
                 "last_login_at": r["last_login_at"]}}
            for r in rows]


def make_link(cfg, uid: int, *, kind: str, by=None, seconds: int = LINK_ADMIN_S, c=None) -> dict:
    """A one-use set-password link for this account: {"url", "expires_at"}. Only its sha256 is kept."""
    tok = secrets.token_urlsafe(32)
    now = _now()
    exp = _iso(now + seconds)
    (c or db.conn()).execute("INSERT INTO pw_links(token_hash, user_id, kind, made_by, created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?)",
                             (auth._sha(tok), uid, kind, by, _iso(now), exp))
    return {"url": f"{cfg.public_url}/set-password#t={tok}", "expires_at": exp}


def bootstrap(cfg, email: str) -> dict:
    """The first admins (python3 -m hub.auth bootstrap EMAIL): on the ledger, an admin, enabled;
    a 24-hour set-password link besides the first password."""
    db.init(cfg)
    db.migrate()
    got = allow(cfg, email, note="bootstrap", role="admin", short_ok=False)
    with db.transaction() as c:
        c.execute("UPDATE users SET role = 'admin', disabled = 0 WHERE id = ?", (got["user_id"],))
        link = make_link(cfg, got["user_id"], kind="bootstrap", c=c)
        log_event("link_made", user_id=got["user_id"], email=got["email"], detail="bootstrap", c=c)
    row = account(got["user_id"])
    return {**got, "default_password": row["pw_hash"] is None, "link": link}


def server_admin(cfg, email: str, password: str) -> dict:
    """An admin account made on the server only (python3 -m hub.auth admin-account EMAIL): any email
    domain, unlike the ledger's box on the page, since the lab's own admin need not be a student or
    member of staff. It goes on the ledger (so signing in works as for everyone), is an admin, and has
    this password at once (no forced change: the person running it chose it). Its sessions end."""
    db.init(cfg)
    db.migrate()
    e = (email or "").strip().lower()
    local, _, dom = e.partition("@")
    if not local or "." not in dom or not re.fullmatch(r"[a-z0-9._+-]{1,64}", local):
        raise HTTPError(400, "bad_email", "That is not an email address.")
    name = re.sub(r"[^a-z0-9._-]", "", local)[:32]
    if len(name) < 3:
        raise HTTPError(400, "bad_email", "The part before the @ needs at least 3 letters or digits for a username.")
    now = db.now()
    with db.transaction() as c:
        clash = c.execute("SELECT id FROM users WHERE username = ? AND email != ?", (name, e)).fetchone()
        if clash is not None:
            raise HTTPError(409, "username_taken", f"The username {name} belongs to another account.")
        row = c.execute("SELECT id FROM users WHERE email = ?", (e,)).fetchone()
        if row is None:
            uid = c.execute("INSERT INTO users(email, name, role, disabled, created_at) VALUES (?, ?, 'admin', 0, ?)",
                            (e, name, now)).lastrowid
        else:
            uid = row["id"]
        problem = password_problem(password, {"username": name, "email": e})
        if problem:
            raise HTTPError(400, "weak_password", problem)
        c.execute("UPDATE users SET role = 'admin', disabled = 0, username = ?, pw_hash = ?, "
                  "session_v = COALESCE(session_v, 0) + 1 WHERE id = ?", (name, hash_password(password), uid))
        if c.execute("SELECT 1 FROM allowed_emails WHERE email = ?", (e,)).fetchone() is None:
            c.execute("INSERT INTO allowed_emails(email, note, added_by, added_at) VALUES (?, ?, NULL, ?)",
                      (e, "server admin account", now))
        log_event("server_admin", user_id=uid, email=e, detail="made or reset on the server", c=c)
    return {"user_id": uid, "email": e, "username": name}


# ---------------------------------------------------------------- email (only the reset link)

def email_ready(cfg) -> bool:
    return bool(cfg.smtp_host and cfg.smtp_from)


def _smtp_password(cfg) -> str:
    p = cfg.smtp_password_file
    if not p:
        raise RuntimeError("PCG_SMTP_USER is set but PCG_SMTP_PASSWORD_FILE is not")
    st = os.stat(p)
    if st.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise RuntimeError(f"{p} can be read by others: chmod 600 it")
    with open(p, encoding="utf-8") as f:
        return f.read().strip()


def send_email(cfg, to: str, subject: str, body: str) -> None:
    """Plain text, over TLS (465) or STARTTLS (any other port; never in the clear). Raises on failure."""
    msg = EmailMessage()
    # a bare address gets the name "papercast" (hub.env is also sourced by bash: no spaces or <> there)
    msg["From"] = cfg.smtp_from if "<" in cfg.smtp_from else formataddr(("papercast", cfg.smtp_from))
    msg["To"] = to
    msg["Subject"] = subject
    msg["Date"] = formatdate(usegmt=True)
    msg["Message-ID"] = make_msgid(domain=cfg.smtp_from.rpartition("@")[2].strip(">") or None)
    msg.set_content(body)
    ctx = ssl.create_default_context(cafile=getattr(cfg, "smtp_ca_file", None) or None)
    if cfg.smtp_port == 465:
        s = smtplib.SMTP_SSL(cfg.smtp_host, cfg.smtp_port, timeout=20, context=ctx)
    else:
        s = smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=20)
    try:
        if cfg.smtp_port != 465:
            s.ehlo()
            s.starttls(context=ctx)             # SMTPNotSupportedError when not offered: nothing sent
            s.ehlo()
        if cfg.smtp_user:
            s.login(cfg.smtp_user, _smtp_password(cfg))
        s.send_message(msg)
    finally:
        try:
            s.quit()
        except (smtplib.SMTPException, OSError):
            s.close()


def reset_email(cfg, username: str, url: str) -> tuple:
    return ("Your papercast password link",
            f"Someone, probably you, asked for a new papercast password for {username}.\n\n"
            f"Set it here (the link works once, for the next hour):\n\n{url}\n\n"
            "If you did not ask, ignore this email: your password stays as it is.\n")


_mailq: queue.Queue = queue.Queue(maxsize=200)
_mailer = {"thread": None}
_mail_lock = threading.Lock()


def queue_email(cfg, to: str, subject: str, body: str, *, user_id=None) -> None:
    """Sent by a background thread: the request never waits for SMTP, and a failure goes to the
    hub's log and the sign-in log, never to the person who asked."""
    with _mail_lock:
        t = _mailer["thread"]
        if t is None or not t.is_alive():
            t = threading.Thread(target=_mail_loop, name="pcg-mail", daemon=True)
            t.start()
            _mailer["thread"] = t
    try:
        _mailq.put_nowait((cfg, to, subject, body, user_id))
    except queue.Full:
        log.warning("mail queue full: the link for %s was not sent", to)
        log_event("email_failed", user_id=user_id, email=to, detail="the hub's mail queue is full")


def _mail_loop():
    while True:
        cfg, to, subject, body, uid = _mailq.get()
        try:
            send_email(cfg, to, subject, body)
            log.info("sent the password link to %s", to)
            log_event("link_sent", user_id=uid, email=to)
        except Exception as ex:                 # noqa: BLE001  (SMTP, TLS, DNS, the password file)
            log.warning("could not email %s: %s: %s", to, type(ex).__name__, ex)
            try:
                log_event("email_failed", user_id=uid, email=to, detail=f"{type(ex).__name__}: {ex}")
            except Exception:                   # noqa: BLE001
                log.exception("auth_log")
        finally:
            _mailq.task_done()


def mail_idle(timeout: float = 10.0) -> bool:
    """Tests: wait until every queued email has been tried."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if _mailq.unfinished_tasks == 0:
            return True
        time.sleep(0.02)
    return False


# ---------------------------------------------------------------- routes: the pages and the person's own

def _need_password_mode(req):
    if not is_password_mode(req.cfg):
        raise HTTPError(409, "not_password", "this hub does not use passwords (PCG_AUTH is not password)")


def signin_page(req):
    auth._page(req, "signin.html")


def setpw_page(req):
    auth._page(req, "setpw.html")


def state(req):
    """What the sign-in pages need to know, without a 401 in the console."""
    try:
        u = auth.browser_user(req)
    except HTTPError:
        u = None
    if u is not None and u.get("disabled"):
        u = None
    req.send_json(200, {"mode": req.cfg.auth, "email": email_ready(req.cfg), "signed_in": u is not None,
                        "must_change": bool(u and u.get("must_change")),
                        "user": None if u is None else {k: u.get(k) for k in ("id", "name", "username", "email", "role")}})


def _find_login(ident: str):
    """The account a sign-in names: a username, or an email at either Imperial domain (the local
    part is the username, so yl6719@imperial.ac.uk is the same person as yl6719@ic.ac.uk)."""
    s = ident.strip().lower()
    if "@" in s:
        local, _, dom = s.rpartition("@")
        if dom not in IMPERIAL:
            # a server admin account (admin-account) may have any domain: its exact email
            r = db.conn().execute("SELECT username FROM users WHERE email = ? AND username IS NOT NULL", (s,)).fetchone()
            return account(username=r["username"]) if r is not None else None
        s = local
    if not s or len(s) > CODE_MAX or not CODE_RX.match(s):
        return None
    return account(username=s)


def _cookie(req, row) -> dict:
    return {"Set-Cookie": auth.session_cookie(req.cfg, row["id"], sv=row["session_v"])}


def login(req):
    auth.csrf(req)
    _need_password_mode(req)
    b = req.json()
    ident, pw = b.get("login"), b.get("password")
    if not isinstance(ident, str) or not isinstance(pw, str) or not ident.strip() or not pw:
        raise HTTPError(400, "bad_request", "Type your username (or email) and your password.")
    ident = ident[:200]
    ip = client_addr(req)
    row = _find_login(ident)
    akey = ("id", row["id"]) if row is not None else ("name", ident.strip().lower()[:120])
    wait = max(FAILS_ACCOUNT.wait(akey), FAILS_ADDRESS.wait(ip))
    if wait > 0:
        for lim, key in ((FAILS_ACCOUNT, akey), (FAILS_ADDRESS, ip)):
            if lim.wait(key) > 0 and lim.first_refusal(key):
                log_event("signin_limited", user_id=row["id"] if row else None,
                          email=row["email"] if row else ident, ip=ip,
                          detail="this account" if lim is FAILS_ACCOUNT else "this address")
        _slow_down(wait)
    usable = row is not None and row["listed"] and row["username"]
    if usable and row["pw_hash"] is not None:
        ok = check_password(pw, row["pw_hash"])
    else:
        # the first password (the username), or no account: the same scrypt either way
        _parse_and_burn(pw)
        ok = bool(usable) and hmac.compare_digest(_norm(pw), _norm(row["username"]))
    if not ok:
        FAILS_ACCOUNT.hit(akey)
        FAILS_ADDRESS.hit(ip)
        log_event("signin_failed", user_id=row["id"] if row else None, email=row["email"] if row else ident, ip=ip,
                  detail=None if row else "no such account")
        raise HTTPError(401, "bad_login", BAD_LOGIN)
    if row["disabled"]:
        log_event("signin_failed", user_id=row["id"], email=row["email"], ip=ip, detail="account disabled")
        raise HTTPError(403, "disabled", "This account is disabled: ask an admin.")
    FAILS_ACCOUNT.clear(akey)
    with db.transaction() as c:
        if row["pw_hash"] is not None and outdated(row["pw_hash"]):
            c.execute("UPDATE users SET pw_hash = ? WHERE id = ? AND pw_hash = ?", (hash_password(pw), row["id"], row["pw_hash"]))
        c.execute("UPDATE users SET last_login_at = ? WHERE id = ?", (_iso(), row["id"]))
        log_event("signin", user_id=row["id"], email=row["email"], ip=ip,
                  detail="with the first password" if row["pw_hash"] is None else None, c=c)
    row = account(row["id"])
    req.send_json(200, {"ok": True, "must_change": row["pw_hash"] is None, "user": public_user(row)}, _cookie(req, row))


def forgot(req):
    """Always the same answer, whoever is on the list; the email goes out from another thread."""
    auth.csrf(req)
    _need_password_mode(req)
    ip = client_addr(req)
    wait = ASKS_ADDRESS.wait(ip)
    if wait > 0:
        _slow_down(wait)
    ASKS_ADDRESS.hit(ip)
    raw = req.json().get("email")
    typed = raw.strip().lower()[:200] if isinstance(raw, str) else ""
    if not auth.EMAIL_RX.match(typed):
        raise HTTPError(400, "bad_email", "Type the email address the group has for you.")
    cfg = req.cfg
    ready = email_ready(cfg)
    row = account(email=typed)
    if row is None or not row["listed"]:
        found = _find_login(typed)
        row = found if found is not None and found["listed"] else None
    key = row["email"] if row is not None else typed
    if ASKS_EMAIL.wait(key) > 0:
        if ASKS_EMAIL.first_refusal(key):
            log_event("reset_limited", user_id=row["id"] if row else None, email=key, ip=ip)
    else:
        ASKS_EMAIL.hit(key)
        if row is None:
            log_event("reset_unknown", email=typed, ip=ip, detail="not on the list")
        elif row["disabled"]:
            log_event("reset_asked", user_id=row["id"], email=row["email"], ip=ip, detail="account disabled: nothing sent")
        elif ready:
            with db.transaction() as c:
                link = make_link(cfg, row["id"], kind="email", seconds=LINK_EMAIL_S, c=c)
                log_event("reset_asked", user_id=row["id"], email=row["email"], ip=ip, detail="emailing a link", c=c)
            queue_email(cfg, row["email"], *reset_email(cfg, row["username"], link["url"]), user_id=row["id"])
        else:
            with db.transaction() as c:
                c.execute("UPDATE users SET reset_asked_at = ? WHERE id = ?", (_iso(), row["id"]))
                log_event("reset_asked", user_id=row["id"], email=row["email"], ip=ip,
                          detail="no email set up: an admin can reset it or make a link", c=c)
    req.send_json(200, {"ok": True, "email": ready, "message": SAME_ANSWER if ready else SAME_ANSWER_NO_EMAIL})


def _live_link(tok: str, c=None):
    if not isinstance(tok, str) or not re.fullmatch(r"[A-Za-z0-9_-]{20,100}", tok):
        return None
    c = c or db.conn()
    r = c.execute("SELECT * FROM pw_links WHERE token_hash = ?", (auth._sha(tok),)).fetchone()
    if r is None or r["used_at"] is not None or r["expires_at"] <= _iso() or not auth._same(r["token_hash"], auth._sha(tok)):
        return None
    return r


def link_post(req):
    auth.csrf(req)
    _need_password_mode(req)
    b = req.json()
    gone = HTTPError(410, "used_or_expired", "This link was already used or has expired. Ask for a new one on the sign-in page.")
    ln = _live_link(b.get("token"))
    row = account(ln["user_id"]) if ln is not None else None
    if row is None or not row["listed"] or row["disabled"]:
        raise gone
    if b.get("peek"):
        req.send_json(200, {"username": row["username"], "email": row["email"], "name": row["name"],
                            "expires_at": ln["expires_at"], "first": row["pw_hash"] is None})
        return
    why = password_problem(b.get("password"), dict(row))
    if why:
        raise HTTPError(400, "weak_password", why)
    h = hash_password(b["password"])
    ip = client_addr(req)
    now = _iso()
    with db.transaction() as c:
        if c.execute("UPDATE pw_links SET used_at = ? WHERE token_hash = ? AND used_at IS NULL AND expires_at > ?",
                     (now, ln["token_hash"], now)).rowcount != 1:
            raise gone
        c.execute("UPDATE users SET pw_hash = ?, pw_set_at = ?, reset_asked_at = NULL, last_login_at = ? WHERE id = ?",
                  (h, now, now, row["id"]))
        end_everything(c, row["id"], tokens=False)     # other browsers out; the other links void
        log_event("link_used", user_id=row["id"], email=row["email"], ip=ip, detail=f"a link {'emailed' if ln['kind'] == 'email' else 'made by an admin' if ln['kind'] == 'admin' else 'from the server'}", c=c)
    FAILS_ACCOUNT.clear(("id", row["id"]))
    row = account(row["id"])
    req.send_json(200, {"ok": True, "user": public_user(row)}, _cookie(req, row))


def change_password(req):
    """The signed-in person's new password. While the first password stands the session is
    proof enough; after that, the current one is asked for."""
    auth.csrf(req)
    _need_password_mode(req)
    u = auth.browser_user(req)
    if u is None:
        raise HTTPError(401, "login_required", "sign in first")
    row = account(u["id"])
    if row["disabled"]:
        raise HTTPError(403, "disabled", "this account is disabled")
    b = req.json()
    first = row["pw_hash"] is None
    ip = client_addr(req)
    if not first:
        akey = ("id", row["id"])
        wait = max(FAILS_ACCOUNT.wait(akey), FAILS_ADDRESS.wait(ip))
        if wait > 0:
            _slow_down(wait)
        cur = b.get("current")
        if not isinstance(cur, str) or not check_password(cur, row["pw_hash"]):
            FAILS_ACCOUNT.hit(akey)
            FAILS_ADDRESS.hit(ip)
            log_event("password_change_failed", user_id=row["id"], email=row["email"], ip=ip, detail="wrong current password")
            raise HTTPError(400, "wrong_password", "Your current password is not right.")
    why = password_problem(b.get("password"), dict(row))
    if why:
        raise HTTPError(400, "weak_password", why)
    h = hash_password(b["password"])
    with db.transaction() as c:
        c.execute("UPDATE users SET pw_hash = ?, pw_set_at = ?, reset_asked_at = NULL WHERE id = ?", (h, _iso(), row["id"]))
        end_everything(c, row["id"], tokens=False)
        log_event("password_changed", user_id=row["id"], email=row["email"], ip=ip,
                  detail="the first password replaced" if first else None, c=c)
    row = account(row["id"])
    req.send_json(200, {"ok": True, "user": public_user(row)}, _cookie(req, row))


def signout(req):
    auth.csrf(req)
    req.send_json(200, {"ok": True}, {"Set-Cookie": auth.clear_cookie(req.cfg)})


def signout_all(req):
    _need_password_mode(req)
    with db.transaction() as c:
        c.execute("UPDATE users SET session_v = session_v + 1 WHERE id = ?", (req.user["id"],))
        log_event("signout_all", user_id=req.user["id"], email=req.user["email"], ip=client_addr(req), c=c)
    req.send_json(200, {"ok": True}, {"Set-Cookie": auth.clear_cookie(req.cfg)})


# ---------------------------------------------------------------- routes: admin

def admin_allowed(req):
    req.send_json(200, {"emails": ledger(), "email": email_ready(req.cfg), "domains": list(IMPERIAL)})


def admin_allow(req):
    b = req.json()
    got = allow(req.cfg, b.get("email"), note=b.get("note") or "", by=req.user["id"], ip=client_addr(req))
    row = account(got["user_id"])
    req.send_json(201 if got["state"] != "already" else 200, {**got, "user": public_user(row)})


def admin_disallow(req):
    b = req.json()
    if not isinstance(b.get("confirm"), str) or b["confirm"].strip().lower() != "delete":
        raise HTTPError(400, "confirm", "Type delete to confirm.")
    e = b.get("email").strip().lower() if isinstance(b.get("email"), str) else ""
    if e and e == (req.user.get("email") or "").lower():
        raise HTTPError(409, "not_yourself", "You cannot remove yourself.")
    req.send_json(200, disallow(req.cfg, e, by=req.user["id"], ip=client_addr(req)))


def _target(uid) -> dict:
    row = account(int(uid))
    if row is None:
        raise HTTPError(404, "not_found", "no such user")
    if not row["listed"] or row["disabled"]:
        raise HTTPError(409, "not_listed", f"{row['email']} is not on the list (or is disabled): add it first.")
    return row


def admin_reset_link(req, uid):
    row = _target(uid)
    with db.transaction() as c:
        link = make_link(req.cfg, row["id"], kind="admin", by=req.user["id"], c=c)
        c.execute("UPDATE users SET reset_asked_at = NULL WHERE id = ?", (row["id"],))
        log_event("link_made", user_id=row["id"], email=row["email"], actor_id=req.user["id"], ip=client_addr(req), c=c)
    req.send_json(201, {**link, "username": row["username"]})


def admin_reset_default(req, uid):
    """Back to the first password (the username): every session, device and link of the account ends."""
    row = _target(uid)
    if row["id"] == req.user["id"]:
        raise HTTPError(409, "not_yourself", "Change your own password under Account.")
    with db.transaction() as c:
        c.execute("UPDATE users SET pw_hash = NULL, pw_set_at = ?, reset_asked_at = NULL WHERE id = ?", (_iso(), row["id"]))
        end_everything(c, row["id"], tokens=True)
        log_event("reset_default", user_id=row["id"], email=row["email"], actor_id=req.user["id"], ip=client_addr(req), c=c)
    req.send_json(200, {"ok": True, "user": public_user(account(row["id"]))})


def admin_log(req):
    try:
        n = max(1, min(500, int(req.arg("limit") or 100)))
    except ValueError:
        n = 100
    rows = db.conn().execute(
        "SELECT l.*, u.name AS user_name, u.username AS user_username, a.name AS actor_name FROM auth_log l "
        "LEFT JOIN users u ON u.id = l.user_id LEFT JOIN users a ON a.id = l.actor_id ORDER BY l.id DESC LIMIT ?", (n,)).fetchall()
    req.send_json(200, {"events": [{
        "id": r["id"], "at": r["at"], "kind": r["kind"], "email": r["email"], "ip": r["ip"], "detail": r["detail"],
        "user": {"id": r["user_id"], "name": r["user_name"], "username": r["user_username"]} if r["user_id"] is not None else None,
        "actor": {"id": r["actor_id"], "name": r["actor_name"]} if r["actor_id"] is not None else None} for r in rows]})


ROUTES = [
    ("GET", r"^/signin$", signin_page, "public"),
    ("GET", r"^/set-password$", setpw_page, "public"),
    ("GET", r"^/api/auth/state$", state, "public"),
    ("POST", r"^/api/auth/login$", login, "public"),
    ("POST", r"^/api/auth/forgot$", forgot, "public"),
    ("POST", r"^/api/auth/link$", link_post, "public"),
    ("POST", r"^/api/auth/password$", change_password, "public"),
    ("POST", r"^/api/auth/signout$", signout, "public"),
    ("POST", r"^/api/auth/signout-all$", signout_all, "viewer"),
    ("GET", r"^/api/admin/allowed$", admin_allowed, "admin"),
    ("POST", r"^/api/admin/allowed$", admin_allow, "admin"),
    ("DELETE", r"^/api/admin/allowed$", admin_disallow, "admin"),
    ("POST", r"^/api/admin/users/(\d{1,12})/reset-link$", admin_reset_link, "admin"),
    ("POST", r"^/api/admin/users/(\d{1,12})/reset-default$", admin_reset_default, "admin"),
    ("GET", r"^/api/admin/auth-log$", admin_log, "admin"),
]

