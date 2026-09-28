"""The hub's CLI API over HTTP (SPEC.md sections 2 and 4): the bearer token, errors a person can
act on, and retries with backoff when the hub or the network hiccups.

    api = Api.from_config()
    api.me()                      -> {"id", "name", "email", "role"}
    api.get("/api/cli/prompt")    -> the parsed JSON answer
    api.upload("bundle.tar.gz")   -> {"episode_id", "paper_id", "state"}

Errors raise the classes in errors.py; each message says what to do.
"""
from __future__ import annotations

import http.client
import json
import os
import platform
import socket
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from . import __version__
from . import config
from . import util
from .errors import (ApiError, AuthError, Conflict, Forbidden, NetworkError, NotFound,
                     NotLoggedIn, PapercastError, ServerError)

# Answers worth another try: the hub restarting, Cloudflare in front of an origin that is down or
# slow (520-524), rate limiting. Everything else is an answer and is not repeated.
RETRY_STATUS = (429, 500, 502, 503, 504, 520, 521, 522, 523, 524, 529)
RETRIES = 4                         # so 5 tries: waits of 1, 2, 4, 8 s (x PAPERCAST_RETRY_BASE_S)
BACKOFF_CAP_S = 30.0
UPLOAD_MAX = 50 * 1024 * 1024       # SPEC section 4
USER_AGENT = (f"papercast/{__version__} (python {platform.python_version()}; {sys.platform})")
_UNSET = object()


def normalise_server(s: str) -> str:
    """"hub.example.org/" -> "https://hub.example.org"; http only for this machine."""
    s = (s or "").strip().rstrip("/")
    if not s:
        raise PapercastError("No server given: papercast login --server https://<your group's hub>")
    if "://" not in s:
        local = s.split("/")[0].split(":")[0] in ("localhost", "127.0.0.1", "[::1]")
        s = ("http://" if local else "https://") + s
    u = urllib.parse.urlsplit(s)
    if u.scheme not in ("http", "https") or not u.netloc:
        raise PapercastError(f"{s!r} is not a web address like https://papercast.example.org")
    return f"{u.scheme}://{u.netloc}{u.path.rstrip('/')}"


class Response:
    def __init__(self, status: int, body, headers):
        self.status, self.body, self.headers = status, body, headers

    def __repr__(self):
        return f"Response({self.status}, {self.body!r})"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect is reported, not followed: a POST turned into a GET, or a login page of
    Cloudflare Access answering for the API, would otherwise look like success."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Api:
    def __init__(self, server: str, token: str | None = None, *, device: str | None = None,
                 timeout: float = 60.0, retries: int = RETRIES, backoff: float | None = None,
                 sleep=time.sleep):
        self.server = normalise_server(server)
        self.token = token
        self.device = device or config.default_device()
        self.timeout = timeout
        self.retries = retries
        if backoff is None:
            try:
                backoff = float(os.environ.get("PAPERCAST_RETRY_BASE_S", "1"))
            except ValueError:
                backoff = 1.0
        self.backoff = backoff
        self.sleep = sleep
        self._opener = urllib.request.build_opener(_NoRedirect())

    @classmethod
    def from_config(cls, cfg: dict | None = None, **kw) -> "Api":
        cfg = config.require_login(cfg)
        return cls(cfg["server"], cfg["token"], device=cfg.get("device"), **kw)

    # ------------------------------------------------------------------ the one request
    def request(self, method: str, path: str, *, params: dict | None = None, json_body=_UNSET,
                data: bytes | None = None, content_type: str | None = None, auth: bool = True,
                accept: tuple = (), retries: int | None = None, timeout: float | None = None,
                headers: dict | None = None) -> Response:
        """One call. Returns a Response for 2xx and for the statuses in `accept`; raises an
        ApiError subclass for anything else. 5xx, 429 and network errors are tried again."""
        url = self.server + path
        if params:
            q = {k: v for k, v in params.items() if v not in (None, "")}
            if q:
                url += "?" + urllib.parse.urlencode(q)
        hdrs = {"User-Agent": USER_AGENT, "Accept": "application/json", **(headers or {})}
        body = data
        if json_body is not _UNSET:
            body = json.dumps(json_body).encode("utf-8")
            content_type = "application/json"
        if content_type:
            hdrs["Content-Type"] = content_type
        if auth:
            if not self.token:
                raise NotLoggedIn(f"Not logged in to {self.server}. Run: papercast login")
            hdrs["Authorization"] = f"Bearer {self.token}"
        retries = self.retries if retries is None else retries
        timeout = self.timeout if timeout is None else timeout
        attempt = 0
        while True:
            req = urllib.request.Request(url, data=body, method=method, headers=hdrs)
            try:
                with self._opener.open(req, timeout=timeout) as r:
                    status, raw, rh = r.status, r.read(), r.headers
            except urllib.error.HTTPError as e:
                status, rh = e.code, e.headers
                try:
                    raw = e.read()
                except (OSError, http.client.HTTPException):
                    raw = b""
            except (urllib.error.URLError, OSError, http.client.HTTPException) as e:
                reason = getattr(e, "reason", e)
                if attempt < retries and not _certificate_problem(reason):
                    self.sleep(self._wait(attempt))
                    attempt += 1
                    continue
                raise NetworkError(self._network_message(reason, attempt + 1))
            if status in RETRY_STATUS and attempt < retries:
                self.sleep(self._wait(attempt, rh))
                attempt += 1
                continue
            break
        parsed = _parse(raw, rh)
        if 200 <= status < 300 or status in accept:
            if 200 <= status < 300 and raw and not isinstance(parsed, (dict, list)) \
                    and "html" in (rh.get("Content-Type") or "").lower():
                raise ApiError(f"{self.server} answered {path} with a web page, not the papercast "
                               "API. Is the address right? (Behind Cloudflare Access, /api/cli/ "
                               "must be left out of Access.)", status=status)
            return Response(status, parsed, rh)
        raise self._error(method, path, status, parsed, rh, attempt + 1)

    def _wait(self, attempt: int, headers=None) -> float:
        if headers is not None:
            ra = headers.get("Retry-After")
            if ra and ra.strip().isdigit():
                return min(float(ra), BACKOFF_CAP_S)
        return min(self.backoff * (2 ** attempt), BACKOFF_CAP_S)

    def _network_message(self, reason, tries: int) -> str:
        why = str(reason) or type(reason).__name__
        if isinstance(reason, (socket.timeout, TimeoutError)) or "timed out" in why:
            why = "timed out"
        msg = f"Cannot reach {self.server} ({why})"
        if tries > 1:
            msg += f", {tries} tries"
        msg += ". Check the connection, or the address: papercast login --server URL"
        if _certificate_problem(reason):
            msg += (". The TLS certificate could not be checked: with Python from python.org on "
                    "macOS, run 'Install Certificates.command' in the Python folder")
        return msg

    def _error(self, method, path, status, body, headers, tries) -> ApiError:
        b = body if isinstance(body, dict) else {}
        code = str(b.get("error") or "")
        msg = str(b.get("message") or b.get("detail") or "")
        kw = {"status": status, "error": code, "body": b}
        where = self.server
        if status == 401:
            return AuthError(f"{where} does not accept this device's login (not logged in, or "
                             "the login was revoked). Run: papercast login", **kw)
        if status == 403:
            if code == "disabled" or "disabled" in msg.lower():
                return Forbidden(f"Your account on {where} is disabled. Ask an admin of the group.",
                                 **kw)
            text = f"{where} refused {method} {path}: {msg or code or 'forbidden'}"
            if "contributor" in (msg + code).lower() or path.startswith(("/api/cli/claims",
                                                                          "/api/cli/episodes")):
                text += ". Uploading needs the contributor role: ask an admin to give it to you"
            return Forbidden(text, **kw)
        if status == 404:
            return NotFound(f"{where} has no {method} {path}" + (f" ({msg})" if msg else "") +
                            ". Is the address right, and the hub up to date?", **kw)
        if status == 409:
            if code == "in_progress":
                by = b.get("by")
                name = (by.get("name") if isinstance(by, dict) else by) or "Someone"
                since = util.parse_iso(b.get("since"))
                when = f" since {util.local_hhmm(since)}" if since else ""
                return Conflict(f"{name} is already making this paper{when}. Try again when "
                                "theirs is up; then you can add your own version.", **kw)
            return Conflict(f"{where}: {msg or code or 'conflict'}", **kw)
        if status == 413:
            return ApiError(f"{where} refused it as too large" + (f": {msg}" if msg else ""), **kw)
        if 300 <= status < 400:
            loc = headers.get("Location") or "?"
            return ApiError(f"{where} redirected {path} to {loc}. If the hub moved, run papercast "
                            "login --server <new address>. (Behind Cloudflare Access, /api/cli/ "
                            "must be left out of Access.)", **kw)
        if status >= 500:
            return ServerError(f"{where} failed on {method} {path} (HTTP {status}" +
                               (f": {msg}" if msg else "") + f"), {tries} tries. Try again later.",
                               **kw)
        return ApiError(f"{where}: HTTP {status} on {method} {path}" +
                        (f": {msg or code}" if (msg or code) else ""), **kw)

    # ------------------------------------------------------------------ verbs
    def get(self, path: str, params: dict | None = None, **kw):
        return self.request("GET", path, params=params, **kw).body

    def post(self, path: str, body=None, **kw):
        return self.request("POST", path, json_body={} if body is None else body, **kw).body

    def put(self, path: str, body=None, **kw):
        return self.request("PUT", path, json_body={} if body is None else body, **kw).body

    def delete(self, path: str, **kw):
        return self.request("DELETE", path, **kw).body

    # ------------------------------------------------------------------ SPEC section 4
    def me(self) -> dict:
        return self.get("/api/cli/me")

    def prompt(self) -> dict:
        """{"version", "guideline", "wording"}: the latest base prompt."""
        return self.get("/api/cli/prompt")

    def prefs(self) -> dict:
        """{"settings", "note", "version"}."""
        return self.get("/api/cli/prefs")

    def set_prefs(self, settings: dict, note: str) -> dict:
        return self.put("/api/cli/prefs", {"settings": settings, "note": note})

    def library(self) -> dict:
        """{"papers": [{"id","title","year","arxiv_id","doi","s2_id"}]}."""
        return self.get("/api/cli/library")

    def lookup(self, arxiv_id=None, doi=None, sha256=None, title=None) -> dict:
        """{"paper": None | {...,"episodes": [...]}, "claim": None | {"by","since"}}."""
        return self.get("/api/cli/lookup", {"arxiv_id": arxiv_id, "doi": doi, "sha256": sha256,
                                            "title": title})

    def claim(self, keys: dict, device: str | None = None) -> dict:
        """{"claim_id", "expires_at"}; raises Conflict (error "in_progress") when someone else
        holds this paper."""
        return self.post("/api/cli/claims", {"keys": keys, "device": device or self.device})

    def renew_claim(self, claim_id: str) -> dict:
        return self.put(f"/api/cli/claims/{urllib.parse.quote(claim_id)}")

    def release_claim(self, claim_id: str):
        """Give a claim back (a cancelled job). Not in SPEC 0.1 section 4: a hub without it
        answers 404/405 and the claim simply expires after 6 h."""
        return self.delete(f"/api/cli/claims/{urllib.parse.quote(claim_id)}", retries=1)

    def upload(self, bundle_path: str) -> dict:
        """POST the bundle (tar.gz): {"episode_id", "paper_id", "state": "checking"}."""
        size = os.path.getsize(bundle_path)
        if size > UPLOAD_MAX:
            raise PapercastError(f"The bundle is {size / 1e6:.1f} MB; the hub takes at most "
                                 f"{UPLOAD_MAX // (1024 * 1024)} MB")
        with open(bundle_path, "rb") as fh:
            data = fh.read()
        return self.request("POST", "/api/cli/episodes", data=data,
                            content_type="application/gzip", timeout=600, retries=2).body

    def episodes(self, mine: bool = True, **kw) -> list:
        """The caller's episodes with their state (a list; the hub may wrap it in "episodes")."""
        b = self.get("/api/cli/episodes", {"mine": "1" if mine else None}, **kw)
        if isinstance(b, dict):
            b = b.get("episodes", [])
        return b if isinstance(b, list) else []

    def episode(self, episode_id: str) -> dict:
        return self.get(f"/api/cli/episodes/{urllib.parse.quote(episode_id)}")

    def logout(self):
        """Revoke this token on the hub. Not in SPEC 0.1: `POST /api/cli/logout` (A2/A3)."""
        return self.post("/api/cli/logout", retries=1, timeout=15)


def _certificate_problem(reason) -> bool:
    return isinstance(reason, ssl.SSLCertVerificationError) or \
        "CERTIFICATE_VERIFY_FAILED" in str(reason)


def _parse(raw: bytes, headers):
    if not raw:
        return {}
    ctype = (headers.get("Content-Type") or "").lower() if headers is not None else ""
    if "json" in ctype or raw[:1] in (b"{", b"["):
        try:
            return json.loads(raw.decode("utf-8"))
        except ValueError:
            pass
    return raw
