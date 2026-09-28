"""Identity, roles, sessions, the CLI's device login and tokens (SPEC.md section 2). Owner: A2.

Who may ask, per level (LEVELS):
  public                     anyone
  viewer, contributor, admin a browser. PCG_AUTH=password: the pcg_s cookie of a password sign-in
                             (hub/accounts.py: the ledger of allowed emails, passwords, reset
                             links), which carries the person's session version, so a new
                             password, "sign out everywhere" or removal ends every session; while
                             the first password (the username) stands, every level answers 403
                             must_change_password. cf-access: Cloudflare Access's JWT (header
                             Cf-Access-Jwt-Assertion, else the CF_Authorization cookie, which is
                             all a browser carries on paths Access bypasses, like /api/cli/);
                             local: the pcg_s cookie; header (tests only): X-Test-User from
                             127.0.0.1 with no proxy headers. Never a bearer token. A changing
                             request also needs X-PCG: 1, and an Origin, if sent, equal to
                             PCG_PUBLIC_URL's (CSRF).
  cli, cli-contributor       Authorization: Bearer pcg_... (a device's token; `tokens` holds its
                             sha256). In header mode X-Test-User works here too, for other tests.
  worker                     Authorization: Bearer <the voice worker's token>, whose sha256 is
                             PCG_WORKER_TOKEN_SHA256.
Roles rank viewer < contributor < admin; a disabled user gets 403 everywhere.

Routes: GET/PUT /api/me; GET /api/tokens, DELETE /api/tokens/<id> (own devices); the CLI's
device login POST /api/cli/login/start, /poll, the approve page GET /cli?code=, GET
/api/cli/login/info?code=, POST /api/cli/login/approve; POST /api/cli/logout (revokes the
caller's token); local joining GET /join/<token>, POST /api/join; admin GET /api/admin/users,
PUT /api/admin/users/<id>, POST /api/admin/invites. /api/me and /api/admin/users say each
person's "avatar" (hub/avatars.py: the picture's version, or null).

Secrets are kept as sha256 only: tokens, invite links, poll secrets. A device's token is made at
the poll that collects it, so no plaintext is ever stored (cli_logins.token_plain stays NULL).

    python3 -m hub.auth bootstrap <email>     (from stacks/papercast-group, with the hub's env)
makes that person an admin (creating them if new) and, under local auth, prints a one-time
sign-in link; under password auth it puts the email on the ledger and prints a set-password link.
    python3 -m hub.auth allow list | add EMAIL... | remove EMAIL... | import FILE
    python3 -m hub.auth email-test ADDRESS     (sends one email with the hub's SMTP settings)
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import http.client
import json
import logging
import re
import secrets
import sqlite3
import ssl
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.request
from datetime import datetime, timezone
from urllib.parse import urlsplit

from . import db
from .app import PAGE_CSP, HTTPError

log = logging.getLogger("pcg.auth")

LEVELS = ("public", "viewer", "contributor", "admin", "cli", "cli-contributor", "worker")
ROLES = ("viewer", "contributor", "admin")
RANK = {r: i for i, r in enumerate(ROLES)}

SESSION_COOKIE = "pcg_s"
SESSION_S = 30 * 86400
SESSION_SHORT_S = 12 * 3600                 # "Remember me" off: a browser-session cookie, and 12 h at most
INVITE_S = 7 * 86400
SIGNIN_S = 86400                # a sign-in link for someone who already exists (bootstrap)
LOGIN_S = 600                   # a device code
LOGIN_COLLECT_S = 60            # an approval near the end of those 10 min still gets collected
POLL_INTERVAL = 2
MAX_PENDING_LOGINS = 200        # /api/cli/login/start is open to the internet
TOUCH_S = 60                    # tokens.last_used_at is written at most once a minute
JWKS_TTL = 3600
JWKS_MIN_REFRESH = 60           # an unknown kid refetches the certs, at most once a minute
JWKS_MAX = 1 << 20
JWT_MAX = 16384
JWT_LEEWAY = 30                 # clock skew allowed on exp and nbf
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"   # no 0/O, 1/I
# Headers a proxy adds: header auth is refused when any is present (cloudflared connects from
# 127.0.0.1, so the address alone would not tell a test from the internet).
PROXY_HEADERS = ("Cf-Connecting-Ip", "Cf-Ray", "X-Forwarded-For", "X-Real-Ip", "Forwarded")
EMAIL_RX = re.compile(r"^[^@\s<>\"',;]{1,64}@[^@\s<>\"',;]{1,190}$")
USER_COLS = "id, email, name, role, disabled, created_at, username"


def _now() -> float:
    return time.time()


def _iso(t: float) -> str:
    """The same format as db.now(): 2026-09-28T04:12:09Z (sorts as text)."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


def _unix(iso: str) -> float:
    return datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def _same(a: str, b: str) -> bool:
    return hmac.compare_digest((a or "").encode(), (b or "").encode())


def _b64d(s: str) -> bytes:
    if not isinstance(s, str) or not re.fullmatch(r"[A-Za-z0-9_-]*", s):
        raise ValueError("not base64url")
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _b64e(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def clean_text(v, limit: int) -> str:
    """One line of someone's text: control characters out, whitespace runs to one space."""
    if not isinstance(v, str):
        return ""
    v = "".join(ch if unicodedata.category(ch)[0] != "C" else " " for ch in v)
    v = re.sub(r"\s+", " ", v).strip()
    return v[:limit].strip()


def clean_name(v) -> str:
    if not isinstance(v, str):
        raise HTTPError(400, "bad_name", "a name is text")
    n = clean_text(v, 1000)
    if not n:
        raise HTTPError(400, "bad_name", "a name cannot be empty")
    if len(n) > 60:
        raise HTTPError(400, "bad_name", "a name is at most 60 characters")
    return n


def clean_email(v) -> str:
    e = v.strip().lower() if isinstance(v, str) else ""
    if not EMAIL_RX.match(e):
        raise HTTPError(400, "bad_email", "that is not an email address")
    return e


# ---------------------------------------------------------------- users (own SQL, SPEC.md section 3)

def _user(row) -> dict | None:
    if row is None:
        return None
    return {"id": row["id"], "email": row["email"], "name": row["name"], "role": row["role"],
            "disabled": bool(row["disabled"]), "created_at": row["created_at"],
            "username": row["username"] if "username" in row.keys() else None}


def get_user(uid) -> dict | None:
    return _user(db.conn().execute(f"SELECT {USER_COLS} FROM users WHERE id = ?", (uid,)).fetchone())


def user_by_email(email: str) -> dict | None:
    return _user(db.conn().execute(f"SELECT {USER_COLS} FROM users WHERE email = ?", (email,)).fetchone())


def _seen(cfg, email: str) -> dict:
    """A person the front door vouched for (Access, or a test header): made on first sight, as a
    viewer, named after the email's local part; PCG_ADMIN_EMAILS start as admins."""
    u = user_by_email(email)
    if u is None:
        role = "admin" if email in cfg.admin_emails else "viewer"
        db.conn().execute("INSERT OR IGNORE INTO users(email, name, role, disabled, created_at) VALUES (?, ?, ?, 0, ?)",
                          (email, email.split("@")[0][:60] or email, role, _iso(_now())))
        u = user_by_email(email)
    return u


def _check(user: dict, need: str) -> dict:
    if user["disabled"]:
        raise HTTPError(403, "disabled", "this account is disabled")
    if RANK.get(user["role"], -1) < RANK[need]:
        raise HTTPError(403, "forbidden", f"this needs the {need} role")
    return user


# ---------------------------------------------------------------- authenticate

def authenticate(req, level: str):
    """The user dict for this request at `level`, or raise HTTPError(401|403)."""
    if level not in LEVELS:
        raise HTTPError(500, "bad_level", level)
    if level == "public":
        return None
    if level == "worker":
        return _worker(req)
    if level in ("cli", "cli-contributor"):
        user = _from_bearer(req)
        if user is None and req.cfg.auth == "header":
            email = _test_header(req)
            user = _seen(req.cfg, email) if email else None
        if user is None:
            raise HTTPError(401, "login_required", "run `papercast login` first")
        if req.cfg.auth == "password" and not user.pop("listed", True) and not user["disabled"]:
            raise HTTPError(403, "not_listed", "this account's email is not on the group's list")
        if not user["disabled"]:
            _first_password(req, user)
        return _check(user, "contributor" if level == "cli-contributor" else "viewer")
    # browser levels: CSRF first, so a cross-site request changes nothing (not even a new user row)
    csrf(req)
    user = browser_user(req)
    if user is None:
        raise HTTPError(401, "login_required", "sign in first")
    if not user["disabled"]:
        _first_password(req, user)
    return _check(user, level)


def _first_password(req, user: dict) -> dict:
    """Password mode: a session signed in with the first password (the username) may only choose
    a new one (POST /api/auth/password, which is public and checks the session itself)."""
    if req.cfg.auth == "password" and user.get("must_change"):
        raise HTTPError(403, "must_change_password", "choose a new password first")
    return user


def browser_user(req) -> dict | None:
    """Who this browser is (None: nobody). Bearer tokens are not looked at here."""
    cfg = req.cfg
    if cfg.auth == "cf-access":
        jwt = req.headers.get("Cf-Access-Jwt-Assertion") or cookie(req, "CF_Authorization")
        return _seen(cfg, verify_access_jwt(cfg, jwt)) if jwt else None
    if cfg.auth == "password":
        got = session_v2(cfg, cookie(req, SESSION_COOKIE))
        if got is None:
            return None
        from . import accounts
        row = accounts.account(got[0])
        # a session ends with a new password, sign-out-everywhere, a reset or removal (session_v)
        if row is None or row["session_v"] != got[1] or not row["listed"] or row["disabled"]:
            return None
        return accounts.public_user(row)
    if cfg.auth == "local":
        uid = session_uid(cfg, cookie(req, SESSION_COOKIE))
        return get_user(uid) if uid is not None else None
    if cfg.auth == "header":
        email = _test_header(req)
        return _seen(cfg, email) if email else None
    return None


def _origin(url: str) -> str | None:
    try:
        u = urlsplit(url.strip())
        port = u.port
    except (ValueError, AttributeError):
        return None
    if u.scheme not in ("http", "https") or not u.hostname:
        return None
    if port is None or port == {"http": 80, "https": 443}[u.scheme]:
        return f"{u.scheme}://{u.hostname.lower()}"
    return f"{u.scheme}://{u.hostname.lower()}:{port}"


def csrf(req) -> None:
    """Browser requests: an Origin, if sent, must be PCG_PUBLIC_URL's (so `Origin: null`, the
    sandboxed explainer's, is refused); a changing request also needs X-PCG: 1, which no other
    site can send without a CORS preflight this hub never answers."""
    origin = req.headers.get("Origin")
    if origin is not None and _origin(origin) != _origin(req.cfg.public_url):
        raise HTTPError(403, "cross_origin", "cross-origin request refused")
    if req.method in ("GET", "HEAD"):
        return
    if req.headers.get("X-PCG") != "1":
        raise HTTPError(403, "csrf", "a changing request needs the header X-PCG: 1")
    site = req.headers.get("Sec-Fetch-Site")
    if site is not None and site != "same-origin":
        raise HTTPError(403, "cross_origin", "cross-site request refused")


def _test_header(req) -> str | None:
    """X-Test-User, trusted only straight from 127.0.0.1 (never through a proxy or tunnel)."""
    v = req.headers.get("X-Test-User")
    if not v or req.client_ip != "127.0.0.1" or any(req.headers.get(h) is not None for h in PROXY_HEADERS):
        return None
    try:
        return clean_email(v)
    except HTTPError:
        return None


def bearer(req) -> str | None:
    a = req.headers.get("Authorization") or ""
    if a[:7].lower() != "bearer ":
        return None
    return a[7:].strip() or None


def _from_bearer(req) -> dict | None:
    """A device token's user (None: no bearer at all). A bearer that is sent but not valid is a
    401 even in header mode, so a revoked token never falls back to anything."""
    tok = bearer(req)
    if tok is None:
        return None
    h = _sha(tok)
    row = db.conn().execute(
        "SELECT t.id AS tid, t.hash, t.last_used_at, u.id, u.email, u.name, u.role, u.disabled, u.created_at, "
        "u.username, u.pw_hash, EXISTS (SELECT 1 FROM allowed_emails a WHERE a.email = u.email) AS listed "
        "FROM tokens t JOIN users u ON u.id = t.user_id WHERE t.hash = ? AND t.revoked_at IS NULL", (h,)).fetchone()
    if row is None or not tok.startswith("pcg_") or not _same(row["hash"], h):
        raise HTTPError(401, "bad_token", "this device's token is not valid: run `papercast login` again")
    now = _now()
    if row["last_used_at"] is None or row["last_used_at"] < _iso(now - TOUCH_S):
        db.conn().execute("UPDATE tokens SET last_used_at = ? WHERE id = ?", (_iso(now), row["tid"]))
    req.token_id = row["tid"]
    u = _user(row)
    if req.cfg.auth == "password":
        u["must_change"] = row["pw_hash"] is None
        u["listed"] = bool(row["listed"])
    return u


def _worker(req) -> dict:
    tok, want = bearer(req), req.cfg.worker_token_sha256
    if not tok or not want or not _same(_sha(tok), want):
        raise HTTPError(401, "bad_token", "the voice worker's token is not valid")
    return {"id": None, "email": "", "name": "voice worker", "role": "worker", "disabled": False, "created_at": None}


def cookie(req, name: str) -> str | None:
    for part in (req.headers.get("Cookie") or "").split(";"):
        k, sep, v = part.strip().partition("=")
        if sep and k == name:
            return v.strip().strip('"')
    return None


# ---------------------------------------------------------------- local sessions (pcg_s)

def make_session(cfg, uid: int, at: float | None = None) -> str:
    """v1.<user id>.<issued, unix s>.<HMAC-SHA256 with PCG_SECRET>: stateless, 30 days."""
    msg = f"v1.{int(uid)}.{int(_now() if at is None else at)}"
    return msg + "." + _b64e(hmac.new(cfg.secret, b"pcg_s|" + msg.encode(), hashlib.sha256).digest())


def session_uid(cfg, v: str | None) -> int | None:
    m = re.fullmatch(r"v1\.(\d{1,12})\.(\d{1,12})\.[A-Za-z0-9_-]{43}", v or "")
    if not m or len(cfg.secret) < 16:
        return None
    if not _same(make_session(cfg, int(m[1]), int(m[2])), v):
        return None
    age = _now() - int(m[2])
    if age >= SESSION_S or age < -300:
        return None
    return int(m[1])


def make_session_v2(cfg, uid: int, sv: int, at: float | None = None, remember: bool = True) -> str:
    """Password mode: v2.<user id>.<session version>.<issued>.<HMAC>. Bumping users.session_v
    ends every session of that person at once (new password, sign out everywhere, removal).
    Without "Remember me" the prefix is v2s (signed with the rest): it lasts SESSION_SHORT_S."""
    msg = f"{'v2' if remember else 'v2s'}.{int(uid)}.{int(sv)}.{int(_now() if at is None else at)}"
    return msg + "." + _b64e(hmac.new(cfg.secret, b"pcg_s|" + msg.encode(), hashlib.sha256).digest())


def session_v2(cfg, v: str | None):
    """(user id, session version) of a valid v2 cookie, else None."""
    m = re.fullmatch(r"(v2s?)\.(\d{1,12})\.(\d{1,9})\.(\d{1,12})\.[A-Za-z0-9_-]{43}", v or "")
    if not m or len(cfg.secret) < 16:
        return None
    remember = m[1] == "v2"
    if not _same(make_session_v2(cfg, int(m[2]), int(m[3]), int(m[4]), remember=remember), v):
        return None
    age = _now() - int(m[4])
    if age >= (SESSION_S if remember else SESSION_SHORT_S) or age < -300:
        return None
    return int(m[2]), int(m[3])


def remembered(req) -> bool:
    """False while this browser's session was made without "Remember me" (kept when it is renewed)."""
    return not (cookie(req, SESSION_COOKIE) or "").startswith("v2s.")


def _secure(cfg) -> str:
    return "; Secure" if cfg.public_url.lower().startswith("https://") else ""


def session_cookie(cfg, uid: int, sv: int | None = None, remember: bool = True) -> str:
    """The Set-Cookie value: HttpOnly, SameSite=Lax, Secure when the hub is served over https.
    Without "Remember me" (password mode) it has no Max-Age, so it ends with the browser."""
    keep = f"; Max-Age={SESSION_S}"
    if cfg.auth == "password":
        if sv is None:
            sv = db.conn().execute("SELECT session_v FROM users WHERE id = ?", (uid,)).fetchone()[0]
        val = make_session_v2(cfg, uid, sv, remember=remember)
        keep = keep if remember else ""
    else:
        val = make_session(cfg, uid)
    return f"{SESSION_COOKIE}={val}; Path=/{keep}; HttpOnly; SameSite=Lax{_secure(cfg)}"


def clear_cookie(cfg) -> str:
    return f"{SESSION_COOKIE}=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax{_secure(cfg)}"


# ---------------------------------------------------------------- Cloudflare Access JWT

# DER DigestInfo for SHA-256 (RFC 8017 section 9.2, note 1).
_SHA256_INFO = bytes.fromhex("3031300d060960864801650304020105000420")


def rsa_pkcs1_sha256_verify(n: int, e: int, msg: bytes, sig: bytes) -> bool:
    """RSASSA-PKCS1-v1_5 with SHA-256 (RS256): pow(sig, e, n) must equal the whole expected
    encoding, built here and compared at once (no parsing of the padding)."""
    k = (n.bit_length() + 7) // 8
    if len(sig) != k:
        return False
    s = int.from_bytes(sig, "big")
    if s >= n:
        return False
    t = _SHA256_INFO + hashlib.sha256(msg).digest()
    if k < len(t) + 11:
        return False
    want = b"\x00\x01" + b"\xff" * (k - len(t) - 3) + b"\x00" + t
    return hmac.compare_digest(pow(s, e, n).to_bytes(k, "big"), want)


def cf_team(cfg) -> str:
    """PCG_CF_TEAM as the bare team name ("lab" from "lab", "lab.cloudflareaccess.com" or a URL)."""
    t = re.sub(r"^https?://", "", (cfg.cf_team or "").strip().lower()).rstrip("/")
    if t.endswith(".cloudflareaccess.com"):
        t = t[:-len(".cloudflareaccess.com")]
    return t if re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", t) else ""


def certs_url(cfg) -> str:
    # cfg.cf_certs_url / cf_ca_file are test hooks (a local https JWKS); production has neither.
    return getattr(cfg, "cf_certs_url", "") or f"https://{cf_team(cfg)}.cloudflareaccess.com/cdn-cgi/access/certs"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, "the certs URL redirected; not followed", headers, fp)


def fetch_jwks(cfg, url: str) -> dict:
    """{kid: (n, e)} from a JWKS, over https only, with no redirects."""
    u = urlsplit(url)
    if u.scheme != "https" or not u.hostname:
        raise ValueError(f"the Access certs URL must be https: {url}")
    ctx = ssl.create_default_context(cafile=getattr(cfg, "cf_ca_file", None) or None)
    opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx), _NoRedirect())
    rq = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "papercast-group-hub"})
    with opener.open(rq, timeout=5) as r:
        body = r.read(JWKS_MAX + 1)
    if len(body) > JWKS_MAX:
        raise ValueError("the certs answer is too large")
    doc = json.loads(body)
    keys = {}
    for k in (doc.get("keys") if isinstance(doc, dict) else None) or []:
        if not isinstance(k, dict) or k.get("kty") != "RSA" or not isinstance(k.get("kid"), str):
            continue
        if k.get("alg", "RS256") != "RS256" or k.get("use", "sig") != "sig":
            continue
        try:
            n, e = int.from_bytes(_b64d(k["n"]), "big"), int.from_bytes(_b64d(k["e"]), "big")
        except (KeyError, ValueError):
            continue
        if n.bit_length() >= 2048 and e >= 3 and e % 2:
            keys[k["kid"]] = (n, e)
    return keys


_jwks_lock = threading.Lock()
_jwks: dict = {}    # url -> {"keys": {kid: (n, e)}, "at": last good fetch, "tried": last attempt} (monotonic)


def _refresh(cfg, url: str, ent: dict | None) -> dict:
    now = time.monotonic()
    try:
        ent = {"keys": fetch_jwks(cfg, url), "at": now, "tried": now}
    except (OSError, ValueError, http.client.HTTPException) as ex:   # URLError, ssl errors: OSErrors
        log.warning("Access certs from %s: %s", url, ex)
        ent = {"keys": {}, "at": float("-inf")} if ent is None else dict(ent)
        ent["tried"] = now                       # stale keys (if any) serve until a retry works
    _jwks[url] = ent
    return ent


def jwks_key(cfg, kid: str):
    """The key for kid: from the cache (1 h); unknown kid -> one refetch (at most once a minute)."""
    url = certs_url(cfg)
    with _jwks_lock:
        ent = _jwks.get(url)
        now = time.monotonic()
        if ent is None or (now - ent["at"] >= JWKS_TTL and now - ent["tried"] >= JWKS_MIN_REFRESH):
            ent = _refresh(cfg, url, ent)
        if kid not in ent["keys"] and now - ent["tried"] >= JWKS_MIN_REFRESH:
            ent = _refresh(cfg, url, ent)
        if not ent["keys"]:
            raise HTTPError(503, "access_certs", "the hub could not fetch Cloudflare Access's keys")
        return ent["keys"].get(kid)


def verify_access_jwt(cfg, token: str) -> str:
    """The lowercased email of a valid Cloudflare Access JWT, else HTTPError 401 (403: no email)."""
    def bad(why):
        raise HTTPError(401, "bad_access_token", f"Cloudflare Access token refused: {why}")

    team = cf_team(cfg)
    if not team or not cfg.cf_aud:
        raise HTTPError(503, "not_configured", "PCG_CF_TEAM and PCG_CF_AUD must be set for cf-access")
    if not isinstance(token, str) or len(token) > JWT_MAX or token.count(".") != 2:
        bad("malformed")
    h64, p64, s64 = token.split(".")
    try:
        header, claims, sig = json.loads(_b64d(h64)), json.loads(_b64d(p64)), _b64d(s64)
    except ValueError:
        bad("malformed")
    if not isinstance(header, dict) or not isinstance(claims, dict):
        bad("malformed")
    if header.get("alg") != "RS256":                    # never none, never HS256 keyed by the public key
        bad("alg must be RS256")
    if "crit" in header:
        bad("unknown critical header")
    kid = header.get("kid")
    if not isinstance(kid, str) or not kid:
        bad("no kid")
    key = jwks_key(cfg, kid)
    if key is None:
        bad("unknown key")
    if not rsa_pkcs1_sha256_verify(key[0], key[1], f"{h64}.{p64}".encode("ascii"), sig):
        bad("signature")
    if claims.get("iss") != f"https://{team}.cloudflareaccess.com":
        bad("iss")
    aud = claims.get("aud")
    if cfg.cf_aud not in ([aud] if isinstance(aud, str) else aud if isinstance(aud, list) else []):
        bad("aud")
    now = _now()
    exp, nbf = claims.get("exp"), claims.get("nbf")
    if not _num(exp) or now >= exp + JWT_LEEWAY:
        bad("expired")
    if nbf is not None and (not _num(nbf) or now + JWT_LEEWAY < nbf):
        bad("not valid yet")
    email = claims.get("email")
    if not isinstance(email, str) or not EMAIL_RX.match(email.strip().lower()):
        raise HTTPError(403, "no_email", "this Access identity has no email address")
    return email.strip().lower()


def _num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


# ---------------------------------------------------------------- invites and sign-in links (local)

def _invite_columns() -> None:
    """invites.for_user (added by A2): a sign-in link for someone who exists, not a new person."""
    c = db.conn()
    if "for_user" not in {r[1] for r in c.execute("PRAGMA table_info(invites)")}:
        try:
            c.execute("ALTER TABLE invites ADD COLUMN for_user INTEGER REFERENCES users(id)")
        except sqlite3.OperationalError as ex:  # another process added it first
            if "duplicate column" not in str(ex):
                raise


def make_link(cfg, role: str = "viewer", created_by=None, for_user=None) -> dict:
    _invite_columns()
    tok = secrets.token_urlsafe(32)
    now = _now()
    exp = _iso(now + (SIGNIN_S if for_user is not None else INVITE_S))
    db.conn().execute("INSERT INTO invites(token_hash, role, created_by, created_at, expires_at, for_user) VALUES (?, ?, ?, ?, ?, ?)",
                      (_sha(tok), role, created_by, _iso(now), exp, for_user))
    return {"url": f"{cfg.public_url}/join/{tok}", "expires_at": exp, "role": role,
            "kind": "signin" if for_user is not None else "invite"}


def _placeholder_email(name: str) -> str:
    """Local joiners need not give an email; users.email is unique, so they get one that can
    never be real (.invalid is reserved)."""
    slug = re.sub(r"[^a-z0-9]+", ".", unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()).strip(".")
    return f"{(slug or 'user')[:30]}.{secrets.token_hex(3)}@local.invalid"


# ---------------------------------------------------------------- routes

def _page(req, name: str):
    p = req.cfg.static / name
    if not p.is_file():
        raise HTTPError(404, "not_found", "no such page")
    req.send(200, p.read_bytes(), "text/html; charset=utf-8", {"Content-Security-Policy": PAGE_CSP, "X-Frame-Options": "DENY"})


def page_for(req, e) -> str | None:
    """Where a browser opening the page goes instead of this refusal (app.py): the sign-in page
    when it has no session, the set-password page while the first password stands."""
    if e.code == 401:
        return "/signin"
    if e.err == "must_change_password":
        return "/set-password"
    return None


def me_get(req):
    from . import avatars
    req.send_json(200, {**req.user, "auth": req.cfg.auth, "avatar": avatars.version_of(req.user["id"])})


def me_put(req):
    from . import avatars
    name = clean_name(req.json().get("name"))
    db.conn().execute("UPDATE users SET name = ? WHERE id = ?", (name, req.user["id"]))
    req.send_json(200, {**get_user(req.user["id"]), "auth": req.cfg.auth, "avatar": avatars.version_of(req.user["id"])})


def tokens_list(req):
    rows = db.conn().execute("SELECT id, name, created_at, last_used_at FROM tokens WHERE user_id = ? AND revoked_at IS NULL "
                             "ORDER BY id DESC", (req.user["id"],)).fetchall()
    req.send_json(200, {"tokens": [dict(r) for r in rows]})


def token_revoke(req, tid):
    cur = db.conn().execute("UPDATE tokens SET revoked_at = ? WHERE id = ? AND user_id = ? AND revoked_at IS NULL",
                            (_iso(_now()), int(tid), req.user["id"]))
    if cur.rowcount != 1:
        raise HTTPError(404, "not_found", "no such device")
    req.send_json(200, {"ok": True})


def cli_logout(req):
    tid = getattr(req, "token_id", None)
    if tid is not None:
        db.conn().execute("UPDATE tokens SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL", (_iso(_now()), tid))
    req.send_json(200, {"ok": True})


def norm_code(v) -> str:
    s = re.sub(r"[^A-Za-z0-9]", "", v if isinstance(v, str) else "").upper()
    return f"{s[:4]}-{s[4:]}" if len(s) == 8 else ""


def cli_start(req):
    device = clean_text(req.json().get("device"), 80) or "a device"
    poll = secrets.token_urlsafe(32)
    now = _now()
    with db.transaction() as c:
        c.execute("DELETE FROM cli_logins WHERE expires_at < ?", (_iso(now - 86400),))
        pending = c.execute("SELECT COUNT(*) FROM cli_logins WHERE state = 'pending' AND expires_at > ?", (_iso(now),)).fetchone()[0]
        if pending >= MAX_PENDING_LOGINS:
            raise HTTPError(429, "busy", "too many logins waiting; try again in a few minutes")
        for _ in range(8):
            raw = "".join(secrets.choice(CODE_ALPHABET) for _ in range(8))
            code = f"{raw[:4]}-{raw[4:]}"
            if c.execute("SELECT 1 FROM cli_logins WHERE code = ?", (code,)).fetchone() is None:
                break
        else:
            raise HTTPError(503, "busy", "could not make a code; try again")
        c.execute("INSERT INTO cli_logins(code, poll_hash, device, state, created_at, expires_at) VALUES (?, ?, ?, 'pending', ?, ?)",
                  (code, _sha(poll), device, _iso(now), _iso(now + LOGIN_S)))
    req.send_json(200, {"code": code, "url": f"{req.cfg.public_url}/cli?code={code}", "poll": poll,
                        "interval": POLL_INTERVAL, "expires_in": LOGIN_S})


def cli_poll(req):
    poll = req.json().get("poll")
    if not isinstance(poll, str) or not poll or len(poll) > 200:
        raise HTTPError(400, "bad_poll", "send {\"poll\": \"<the secret from start>\"}")
    h = _sha(poll)
    with db.transaction() as c:
        row = c.execute("SELECT * FROM cli_logins WHERE poll_hash = ?", (h,)).fetchone()
        if row is None or not _same(row["poll_hash"], h):
            raise HTTPError(410, "expired", "this login has expired: run `papercast login` again")
        if row["state"] == "denied":
            raise HTTPError(403, "denied", "the login was denied in the browser")
        if row["state"] == "taken":
            raise HTTPError(410, "expired", "this login was already used")
        if row["expires_at"] <= _iso(_now()):
            raise HTTPError(410, "expired", "this login has expired: run `papercast login` again")
        if row["state"] == "pending":
            raise HTTPError(428, "pending", "waiting for the browser", interval=POLL_INTERVAL)
        user = get_user(row["user_id"])
        if user is None or user["disabled"]:
            raise HTTPError(403, "denied", "that account is disabled")
        if req.cfg.auth == "password":
            from . import accounts
            a = accounts.account(user["id"], c=c)
            if not a["listed"] or a["pw_hash"] is None:     # removed, or reset to the first password since
                raise HTTPError(403, "denied", "that account cannot log in a device now")
        # the token is made here, at its one collection: its plaintext exists only in this answer
        if c.execute("UPDATE cli_logins SET state = 'taken', token_plain = NULL WHERE code = ? AND state = 'approved'",
                     (row["code"],)).rowcount != 1:
            raise HTTPError(410, "expired", "this login was already used")
        token = "pcg_" + base64.b32encode(secrets.token_bytes(20)).decode().lower()
        c.execute("INSERT INTO tokens(user_id, name, hash, created_at) VALUES (?, ?, ?, ?)",
                  (user["id"], row["device"], _sha(token), _iso(_now())))
    req.send_json(200, {"token": token, "user": user})


def _login_row(code: str):
    return db.conn().execute("SELECT code, device, state, created_at, expires_at FROM cli_logins WHERE code = ?", (code,)).fetchone()


def _login_state(row) -> str:
    if row["state"] in ("pending", "approved") and row["expires_at"] <= _iso(_now()):
        return "expired"
    return row["state"]


def cli_info(req):
    code = norm_code(req.arg("code"))
    row = _login_row(code) if code else None
    if row is None:
        raise HTTPError(404, "unknown_code", "no login with that code: check it, or run `papercast login` again")
    left = max(0, int(_unix(row["expires_at"]) - _now()))
    req.send_json(200, {"code": row["code"], "device": row["device"], "state": _login_state(row), "expires_in": left})


def cli_approve(req):
    b = req.json()
    code, approve = norm_code(b.get("code")), b.get("approve")
    if not code or not isinstance(approve, bool):
        raise HTTPError(400, "bad_request", "send {\"code\": \"ABCD-EFGH\", \"approve\": true|false}")
    now = _now()
    with db.transaction() as c:
        row = _login_row(code)
        if row is None:
            raise HTTPError(404, "unknown_code", "no login with that code")
        state = _login_state(row)
        if state == "expired":
            raise HTTPError(410, "expired", "this code has expired: run `papercast login` again")
        if state != "pending":
            raise HTTPError(409, "already_decided", f"this login was already {'denied' if state == 'denied' else 'approved'}", state=state)
        if approve:
            c.execute("UPDATE cli_logins SET state = 'approved', user_id = ?, expires_at = max(expires_at, ?) WHERE code = ?",
                      (req.user["id"], _iso(now + LOGIN_COLLECT_S), code))
        else:
            c.execute("UPDATE cli_logins SET state = 'denied', user_id = ? WHERE code = ?", (req.user["id"], code))
    req.send_json(200, {"ok": True, "state": "approved" if approve else "denied", "device": row["device"]})


def cli_page(req):
    _page(req, "cli.html")


def join_page(req, _token):
    _page(req, "join.html")


def _live_invite(tok: str):
    """(invite row, the user a sign-in link is for or None), or 410."""
    row = db.conn().execute("SELECT * FROM invites WHERE token_hash = ?", (_sha(tok),)).fetchone()
    if row is None or row["used_at"] is not None or row["expires_at"] <= _iso(_now()):
        raise HTTPError(410, "used_or_expired", "this link was already used or has expired: ask for a new one")
    who = get_user(row["for_user"]) if row["for_user"] is not None else None
    if row["for_user"] is not None and who is None:
        raise HTTPError(410, "used_or_expired", "this link is for an account that no longer exists")
    return row, who


def join_post(req):
    csrf(req)                                   # public, but it sets a cookie: no login CSRF
    if req.cfg.auth != "local":
        raise HTTPError(409, "not_local", "joining by link is only for local sign-in")
    b = req.json()
    tok = b.get("token")
    if not isinstance(tok, str) or not re.fullmatch(r"[A-Za-z0-9_-]{20,100}", tok):
        raise HTTPError(410, "used_or_expired", "this link is not valid")
    _invite_columns()
    if b.get("peek"):                           # what the page shows before anything is used
        row, who = _live_invite(tok)
        req.send_json(200, {"kind": "signin" if who else "invite", "role": who["role"] if who else row["role"],
                            "name": who["name"] if who else None, "expires_at": row["expires_at"]})
        return
    now = _iso(_now())
    with db.transaction() as c:
        row, who = _live_invite(tok)
        if who is not None:
            if who["disabled"]:
                raise HTTPError(403, "disabled", "this account is disabled")
            uid = who["id"]
        else:
            name = clean_name(b.get("name"))
            email = clean_email(b["email"]) if b.get("email") else _placeholder_email(name)
            if user_by_email(email) is not None:
                raise HTTPError(409, "email_taken", "someone here already uses that email")
            # the invite's role, never PCG_ADMIN_EMAILS: a local email is only what the person typed
            uid = c.execute("INSERT INTO users(email, name, role, disabled, created_at) VALUES (?, ?, ?, 0, ?)",
                            (email, name, row["role"] if row["role"] in ROLES else "viewer", now)).lastrowid
        if c.execute("UPDATE invites SET used_by = ?, used_at = ? WHERE token_hash = ? AND used_at IS NULL",
                     (uid, now, row["token_hash"])).rowcount != 1:
            raise HTTPError(410, "used_or_expired", "this link was already used")
        user = get_user(uid)
    req.send_json(200, {"ok": True, "user": user}, {"Set-Cookie": session_cookie(req.cfg, uid)})


def admin_users(req):
    rows = db.conn().execute(
        f"SELECT {', '.join('u.' + c for c in USER_COLS.split(', '))}, u.pw_hash IS NULL AS default_pw, "
        "u.last_login_at, u.reset_asked_at, a.email IS NOT NULL AS listed, a.note, "
        "(SELECT COUNT(*) FROM tokens t WHERE t.user_id = u.id AND t.revoked_at IS NULL) AS devices, "
        "(SELECT MAX(t.last_used_at) FROM tokens t WHERE t.user_id = u.id) AS last_used_at, "
        "(SELECT MAX(l.at) FROM auth_log l WHERE l.user_id = u.id AND l.kind = 'welcome_sent') AS welcome_at, "
        "(SELECT MAX(l.at) FROM auth_log l WHERE l.user_id = u.id AND l.kind = 'welcome_failed') AS welcome_failed_at "
        "FROM users u LEFT JOIN allowed_emails a ON a.email = u.email ORDER BY u.id").fetchall()
    pw = req.cfg.auth == "password"
    from . import accounts, avatars
    av = avatars.versions()
    req.send_json(200, {"auth": req.cfg.auth, "email": accounts.email_ready(req.cfg), "users": [{
        **_user(r), "avatar": av.get(r["id"]), "devices": r["devices"], "last_used_at": r["last_used_at"], "last_login_at": r["last_login_at"],
        "on_list": bool(r["listed"]), "note": r["note"] or "",
        "default_password": bool(r["default_pw"]) if pw else False,
        "reset_asked_at": r["reset_asked_at"], "welcome_at": r["welcome_at"],
        "welcome_failed_at": r["welcome_failed_at"]} for r in rows]})


def admin_user_put(req, uid):
    b = req.json()
    f = {}
    if "role" in b:
        if b["role"] not in ROLES:
            raise HTTPError(400, "bad_role", f"role is one of {', '.join(ROLES)}")
        f["role"] = b["role"]
    if "disabled" in b:
        if not isinstance(b["disabled"], bool):
            raise HTTPError(400, "bad_request", "disabled is true or false")
        f["disabled"] = int(b["disabled"])
    if "name" in b:
        f["name"] = clean_name(b["name"])
    if not f:
        raise HTTPError(400, "bad_request", "send role, disabled or name")
    from . import accounts
    with db.transaction() as c:
        u = get_user(int(uid))
        if u is None:
            raise HTTPError(404, "not_found", "no such user")
        stays_admin = f.get("role", u["role"]) == "admin" and not f.get("disabled", int(u["disabled"]))
        if u["role"] == "admin" and not u["disabled"] and not stays_admin:
            if not accounts.other_admins(req.cfg, c, u["id"]):
                raise HTTPError(409, "last_admin", "the hub needs at least one admin")
        if req.cfg.auth == "password" and f.get("disabled") == 0 and u["disabled"] and \
                c.execute("SELECT 1 FROM allowed_emails WHERE email = ?", (u["email"],)).fetchone() is None:
            raise HTTPError(409, "not_listed", f"{u['email']} is not on the list: add it again under Users instead")
        c.execute(f"UPDATE users SET {', '.join(k + ' = ?' for k in f)} WHERE id = ?", (*f.values(), u["id"]))
        if f.get("disabled") == 1 and not u["disabled"]:
            c.execute("UPDATE users SET session_v = session_v + 1 WHERE id = ?", (u["id"],))   # every session ends
        if req.cfg.auth == "password":
            for k, what in (("role", "role"), ("disabled", "disabled")):
                if k in f and f[k] != (u[k] if k == "role" else int(u["disabled"])):
                    before = u[k] if k == "role" else ("yes" if u["disabled"] else "no")
                    after = f[k] if k == "role" else ("yes" if f[k] else "no")
                    accounts.log_event(what, user_id=u["id"], email=u["email"], actor_id=req.user["id"],
                                       ip=accounts.client_addr(req), detail=f"{before} -> {after}", c=c)
    req.send_json(200, get_user(int(uid)))


def admin_invite(req):
    """{"role"} -> a join link for someone new; {"user_id"} -> a one-time sign-in link for someone
    who exists (a new browser or phone)."""
    if req.cfg.auth != "local":
        raise HTTPError(409, "not_local", "invite links are for local sign-in; with passwords, add the person's email "
                        "under Users; under Cloudflare Access people sign in with their email")
    b = req.json()
    if b.get("user_id") is not None:
        uid = b["user_id"]
        u = get_user(uid) if isinstance(uid, int) and not isinstance(uid, bool) else None
        if u is None:
            raise HTTPError(404, "not_found", "no such user")
        link = make_link(req.cfg, u["role"], req.user["id"], u["id"])
    else:
        role = b.get("role", "viewer")
        if role not in ROLES:
            raise HTTPError(400, "bad_role", f"role is one of {', '.join(ROLES)}")
        link = make_link(req.cfg, role, req.user["id"])
    req.send_json(201, link)


ROUTES = [
    ("GET", r"^/api/me$", me_get, "viewer"),
    ("PUT", r"^/api/me$", me_put, "viewer"),
    ("GET", r"^/api/tokens$", tokens_list, "viewer"),
    ("DELETE", r"^/api/tokens/(\d{1,12})$", token_revoke, "viewer"),
    ("POST", r"^/api/cli/login/start$", cli_start, "public"),
    ("POST", r"^/api/cli/login/poll$", cli_poll, "public"),
    ("GET", r"^/api/cli/login/info$", cli_info, "viewer"),
    ("POST", r"^/api/cli/login/approve$", cli_approve, "viewer"),
    ("POST", r"^/api/cli/logout$", cli_logout, "cli"),
    ("GET", r"^/cli$", cli_page, "public"),
    ("GET", r"^/join/([A-Za-z0-9_-]{1,100})$", join_page, "public"),
    ("POST", r"^/api/join$", join_post, "public"),
    ("GET", r"^/api/admin/users$", admin_users, "admin"),
    ("PUT", r"^/api/admin/users/(\d{1,12})$", admin_user_put, "admin"),
    ("POST", r"^/api/admin/invites$", admin_invite, "admin"),
]


# ---------------------------------------------------------------- bootstrap and the command line

def bootstrap(cfg, email: str) -> str | None:
    """Make `email` an admin (created if new, re-enabled if disabled). Local auth: returns a
    one-time sign-in link; password auth: the email goes on the ledger, and the answer is a
    24-hour set-password link (accounts.bootstrap)."""
    if cfg.auth == "password":
        from . import accounts
        return accounts.bootstrap(cfg, email)["link"]["url"]
    email = clean_email(email)
    db.init(cfg)
    db.migrate()
    with db.transaction() as c:
        u = user_by_email(email)
        if u is None:
            uid = c.execute("INSERT INTO users(email, name, role, disabled, created_at) VALUES (?, ?, 'admin', 0, ?)",
                            (email, email.split("@")[0][:60] or email, _iso(_now()))).lastrowid
        else:
            uid = u["id"]
            c.execute("UPDATE users SET role = 'admin', disabled = 0 WHERE id = ?", (uid,))
    return make_link(cfg, "admin", None, uid)["url"] if cfg.auth == "local" else None


USAGE = """usage: python3 -m hub.auth bootstrap EMAIL
       python3 -m hub.auth allow list
       python3 -m hub.auth allow add EMAIL... [--note TEXT] [--no-welcome]
       python3 -m hub.auth allow remove EMAIL...
       python3 -m hub.auth allow import FILE [--note TEXT] [--no-welcome]   (one email per line, # comments; - reads stdin)
       (add and import email each new person the welcome, with the first password, when email is set up)
       python3 -m hub.auth email-test ADDRESS
       python3 -m hub.auth admin-account EMAIL   (password mode: an admin with any email domain; the password is read from stdin)
(from stacks/papercast-group, with the hub's settings: set -a; . ~/papercast-group/hub.env; set +a)"""


def _bootstrap_cmd(cfg, email) -> int:
    if cfg.auth == "password":
        from . import accounts
        got = accounts.bootstrap(cfg, email)
        print(f"{got['email']} is an admin, on the list (username {got['username']}).")
        if got["default_password"]:
            print(f"Sign in at {cfg.public_url}/signin as {got['username']} with the first password "
                  f"{got['username']}; the hub then asks for a new one.")
        print(f"Or set the password now with this one-time link (24 hours): {got['link']['url']}")
        return 0
    link = bootstrap(cfg, email)
    print(f"{email.strip().lower()} is an admin.")
    if link:
        print(f"One-time sign-in link (24 hours): {link}")
    elif cfg.auth == "cf-access":
        print("Sign in through Cloudflare Access with that email.")
    return 0


def _read_emails(path: str) -> list:
    text = sys.stdin.read() if path == "-" else open(path, encoding="utf-8").read()
    out = []
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            out.append(line)
    return out


def _allow_cmd(cfg, args) -> int:
    from . import accounts
    db.init(cfg)
    db.migrate()
    if cfg.auth != "password":
        print(f"(PCG_AUTH is {cfg.auth}: the list decides who signs in once the hub uses passwords)", file=sys.stderr)
    if args.what == "list":
        rows = accounts.ledger()
        if not rows:
            print("The list is empty.")
            return 0
        print(f"{'email':34} {'username':16} {'role':12} {'password':9} {'last sign-in':21} added")
        for r in rows:
            u = r["user"] or {}
            pw = "-" if not u else "first" if u["default_password"] else "own"
            role = (u.get("role") or "-") + (" (off)" if u.get("disabled") else "")
            by = r["added_by"]["name"] if r["added_by"] else "server"
            print(f"{r['email']:34} {u.get('username') or '-':16} {role:12} {pw:9} {u.get('last_login_at') or 'never':21} "
                  f"{r['added_at'][:10]} by {by}" + (f" · {r['note']}" if r["note"] else ""))
        return 0
    emails = args.emails if args.what in ("add", "remove") else _read_emails(args.emails[0])
    bad = 0
    for e in emails:
        try:
            if args.what == "remove":
                got = accounts.disallow(cfg, e)
                print(f"removed {got['email']}" + (": account disabled, its sessions and devices ended" if got["user_id"] else ""))
            else:
                got = accounts.allow(cfg, e, note=args.note or "")
                if got["state"] == "already":
                    print(f"already on the list: {got['email']}")
                elif got["state"] == "back":
                    print(f"back on the list: {got['email']} (enabled again, with the password it had)")
                else:
                    print(f"added {got['email']}: username {got['username']}, first password {got['username']} "
                          "(a new one is asked for at the first sign-in)")
                if got["state"] != "already" and not args.no_welcome and not _welcome_now(cfg, got["user_id"]):
                    bad += 1
        except HTTPError as ex:
            bad += 1
            print(f"refused {e.strip()}: {ex.msg}", file=sys.stderr)
    return 1 if bad else 0


def _welcome_now(cfg, uid) -> bool:
    """The welcome email, sent before the command goes on (no mail thread outlives it). False: it
    should have gone and did not."""
    from . import accounts
    row = accounts.account(uid)
    why = accounts.welcome_problem(cfg, row)
    if why == "no_email":
        print("  no welcome email: this hub cannot send email (papercastctl email ...)")
        return True
    if why:
        print("  no welcome email: " + {"has_password": "they have chosen a password already",
                                         "not_password": "the hub does not use passwords (PCG_AUTH)"}.get(why, "not on the list"))
        return True
    subject, text, page = accounts.welcome_email(cfg, row)
    try:
        accounts.send_email(cfg, row["email"], subject, text, page)
    except Exception as ex:                     # noqa: BLE001  (show whatever SMTP said)
        accounts.log_event("welcome_failed", user_id=uid, email=row["email"], detail=f"{type(ex).__name__}: {ex}")
        print(f"  the welcome email to {row['email']} was not sent: {type(ex).__name__}: {ex} "
              "(the account is made; send it again from the Users panel)", file=sys.stderr)
        return False
    accounts.log_event("welcome_sent", user_id=uid, email=row["email"])
    print(f"  welcome email sent to {row['email']}")
    return True


def _email_test_cmd(cfg, to: str) -> int:
    from . import accounts
    if not accounts.email_ready(cfg):
        print("Email is not set up: PCG_SMTP_HOST and PCG_SMTP_FROM (and for a login PCG_SMTP_USER and "
              "PCG_SMTP_PASSWORD_FILE) go in hub.env.", file=sys.stderr)
        return 2
    try:
        accounts.send_email(cfg, to, "papercast: a test email",
                            f"This is a test from the papercast hub at {cfg.public_url}.\n"
                            "The password links it sends will come from this address.\n")
    except Exception as ex:                     # noqa: BLE001  (show whatever SMTP said)
        print(f"not sent: {type(ex).__name__}: {ex}", file=sys.stderr)
        return 1
    print(f"sent to {to} through {cfg.smtp_host}:{cfg.smtp_port} as {cfg.smtp_user or '(no login)'}")
    return 0


def _admin_account_cmd(cfg, email) -> int:
    """The password comes on stdin (never the command line, so never in the process list or a
    shell's history): the first line, without its newline."""
    if cfg.auth != "password":
        print("admin-account is for password sign-in (PCG_AUTH=password)", file=sys.stderr)
        return 2
    import getpass
    pw = getpass.getpass("Password: ") if sys.stdin.isatty() else sys.stdin.readline().rstrip("\r\n")
    from . import accounts
    got = accounts.server_admin(cfg, email, pw)
    print(f"admin {got['email']} (username {got['username']}): password set; sign in at {cfg.public_url}/signin")
    return 0


def main(argv=None) -> int:
    import argparse
    from . import config as C
    argv = sys.argv[1:] if argv is None else argv
    ap = argparse.ArgumentParser(prog="python3 -m hub.auth", usage=USAGE)
    sub = ap.add_subparsers(dest="cmd")
    b = sub.add_parser("bootstrap")
    b.add_argument("email")
    a = sub.add_parser("allow")
    a.add_argument("what", choices=["list", "add", "remove", "import"])
    a.add_argument("emails", nargs="*")
    a.add_argument("--note", default="")
    a.add_argument("--no-welcome", action="store_true", help="add without emailing the welcome")
    t = sub.add_parser("email-test")
    t.add_argument("address")
    s = sub.add_parser("admin-account")
    s.add_argument("email")
    args = ap.parse_args(argv)
    if args.cmd is None or (args.cmd == "allow" and args.what != "list" and not args.emails) \
            or (args.cmd == "allow" and args.what == "import" and len(args.emails) != 1):
        print(USAGE, file=sys.stderr)
        return 2
    cfg = C.load()
    try:
        if args.cmd == "bootstrap":
            return _bootstrap_cmd(cfg, args.email)
        if args.cmd == "allow":
            return _allow_cmd(cfg, args)
        if args.cmd == "admin-account":
            return _admin_account_cmd(cfg, args.email)
        return _email_test_cmd(cfg, args.address)
    except HTTPError as e:
        print(e.msg, file=sys.stderr)
        return 2
    except OSError as e:
        print(e, file=sys.stderr)
        return 2


if __name__ == "__main__":
    # run as the package's module (accounts.py imports it), not as a second copy called __main__
    from hub import auth as _auth
    sys.exit(_auth.main())
