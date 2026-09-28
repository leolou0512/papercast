"""papercast-group hub: the HTTP server (SPEC.md). Routing and request helpers only; every route
lives in the module that owns it. A module lists its routes as
    ROUTES = [("GET", r"^/api/me$", handler, "viewer"), ...]
where handler(req, *groups) sends the answer through req.send_json / req.send / req.send_file,
and the level is one of auth.LEVELS."""
from __future__ import annotations

import importlib
import json
import logging
import mimetypes
import os
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

if __name__ == "__main__":             # `python -m hub.app`: one module object, one HTTPError
    sys.modules.setdefault("hub.app", sys.modules[__name__])

from . import config as C
from . import db

log = logging.getLogger("pcg")

# Owners add their module here (SPEC.md section 1); nothing else in this file is theirs.
ROUTE_MODULES = ["auth", "accounts", "web", "contrib", "voiceq", "graph", "player", "social", "voices"]
JSON_MAX = 256 * 1024
# Leo's page CSP (stacks/papercast/web/app.py PAGE_CSP): no inline script or style.
PAGE_CSP = ("default-src 'self'; img-src 'self' data:; media-src 'self'; style-src 'self'; "
            "script-src 'self'; connect-src 'self'; frame-src 'self'; frame-ancestors 'none'; "
            "base-uri 'none'; form-action 'none'; object-src 'none'")
STATIC_TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
                ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml", ".json": "application/json",
                ".png": "image/png", ".ico": "image/x-icon"}


class HTTPError(Exception):
    def __init__(self, code: int, err: str, msg: str = "", **extra):
        super().__init__(msg or err)
        self.code, self.err, self.msg, self.extra = code, err, msg or err, extra


class Request:
    """One request: what came in, who sent it (set by auth.authenticate), and how to answer."""

    def __init__(self, h: "Handler", cfg: C.Config, method: str, path: str, query: dict):
        self.h, self.cfg, self.method, self.path, self.query = h, cfg, method, path, query
        self.headers = h.headers
        self.client_ip = h.client_address[0]
        self.user = None
        self.sent = False
        self._body = None

    # -- input
    def body(self, limit: int = JSON_MAX) -> bytes:
        if self._body is None:
            n = int(self.headers.get("Content-Length") or 0)
            if n > limit:
                raise HTTPError(413, "too_large", f"body over {limit} bytes")
            self._body = self.h.rfile.read(n) if n else b""
        return self._body

    def json(self) -> dict:
        try:
            v = json.loads(self.body() or b"{}")
        except ValueError:
            raise HTTPError(400, "bad_json", "the body is not JSON")
        if not isinstance(v, dict):
            raise HTTPError(400, "bad_json", "the body must be a JSON object")
        return v

    def arg(self, name: str, default=None):
        return self.query.get(name, default)

    # -- output
    def send(self, code: int, body: bytes, ctype: str, headers: dict | None = None):
        h = self.h
        h.send_response(code)
        h.send_header("Content-Type", ctype)
        h.send_header("Content-Length", str(len(body)))
        h.send_header("Cache-Control", "no-store")
        h.send_header("X-Content-Type-Options", "nosniff")
        h.send_header("Referrer-Policy", "no-referrer")
        for k, v in (headers or {}).items():
            h.send_header(k, v)
        h.end_headers()
        if self.method != "HEAD":
            h.wfile.write(body)
        self.sent = True

    def send_json(self, code: int, obj, headers: dict | None = None):
        self.send(code, json.dumps(obj, ensure_ascii=False).encode(), "application/json", headers)

    def send_file(self, path: Path, ctype: str, headers: dict | None = None):
        """A file, with Range (audio seeking)."""
        size = path.stat().st_size
        start, end, code = 0, size - 1, 200
        rng = self.headers.get("Range", "")
        m = re.match(r"^bytes=(\d*)-(\d*)$", rng.strip())
        if m and size:
            a, b = m.groups()
            if a:
                start = int(a)
                end = min(int(b), size - 1) if b else size - 1
            elif b:
                start = max(0, size - int(b))
            else:                           # "bytes=-" names no range
                start = size
            if start > end or start >= size:
                self.send(416, b"", ctype, {"Content-Range": f"bytes */{size}"})
                return
            code = 206
        h = self.h
        h.send_response(code)
        h.send_header("Content-Type", ctype)
        h.send_header("Content-Length", str(end - start + 1))
        h.send_header("Accept-Ranges", "bytes")
        h.send_header("X-Content-Type-Options", "nosniff")
        if code == 206:
            h.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        for k, v in (headers or {}).items():
            h.send_header(k, v)
        h.end_headers()
        if self.method != "HEAD":
            with open(path, "rb") as f:
                f.seek(start)
                left = end - start + 1
                while left > 0:
                    chunk = f.read(min(1 << 16, left))
                    if not chunk:
                        break
                    h.wfile.write(chunk)
                    left -= len(chunk)
        self.sent = True

    def start_stream(self, ctype: str = "text/event-stream"):
        """Headers for a long answer (SSE); the caller writes to req.h.wfile and flushes."""
        h = self.h
        h.send_response(200)
        h.send_header("Content-Type", ctype)
        h.send_header("Cache-Control", "no-store")
        h.send_header("X-Accel-Buffering", "no")
        h.end_headers()
        h.close_connection = True
        self.sent = True
        return h.wfile


def _load_routes():
    routes = []
    for name in ROUTE_MODULES:
        mod = importlib.import_module(f"{__package__}.{name}")
        for method, pattern, fn, level in getattr(mod, "ROUTES", []):
            routes.append((method, re.compile(pattern), fn, level))
    return routes


def make_handler(cfg: C.Config):
    from . import auth
    routes = _load_routes()
    static = {p.name: p for p in cfg.static.iterdir() if p.is_file()} if cfg.static.is_dir() else {}

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "papercast-group"
        sys_version = ""
        timeout = 120

        def log_message(self, fmt, *args):
            log.info("%s %s", self.address_string(), fmt % args)

        def _go(self, method: str):
            u = urlsplit(self.path)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            req = Request(self, cfg, method, u.path, q)
            try:
                for m, rx, fn, level in routes:
                    if m != method and not (method == "HEAD" and m == "GET"):
                        continue
                    mt = rx.match(u.path)
                    if not mt:
                        continue
                    req.user = auth.authenticate(req, level)
                    fn(req, *mt.groups())
                    if not req.sent:            # a 204 has no body, or keep-alive breaks
                        if method in ("GET", "HEAD"):
                            req.send_json(200, {})
                        else:
                            req.send(204, b"", "application/json")
                    return
                if method in ("GET", "HEAD"):
                    name = "index.html" if u.path in ("/", "/index.html") else u.path.lstrip("/")
                    if "/" not in name and name in static:
                        if name == "index.html":
                            try:
                                req.user = auth.authenticate(req, "viewer")
                            except HTTPError as e:
                                to = auth.page_for(req, e)      # signed out: the sign-in page, not a JSON 401
                                if not to:
                                    raise
                                req.send(303, b"", "text/plain; charset=utf-8", {"Location": to})
                                return
                        p = static[name]
                        ctype = STATIC_TYPES.get(p.suffix, mimetypes.guess_type(p.name)[0] or "application/octet-stream")
                        extra = {"Content-Security-Policy": PAGE_CSP} if p.suffix == ".html" else {}
                        req.send(200, p.read_bytes(), ctype, extra)
                        return
                raise HTTPError(404, "not_found", "no such page")
            except HTTPError as e:
                if req._body is None and int(self.headers.get("Content-Length") or 0):
                    self.close_connection = True
                if not req.sent:
                    try:
                        wait = {"Retry-After": str(e.extra["retry_after"])} if "retry_after" in e.extra else None
                        req.send_json(e.code, {"error": e.err, "message": e.msg, **e.extra}, wait)
                    except (BrokenPipeError, ConnectionResetError):
                        pass
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception:
                log.exception("%s %s", method, u.path)
                if not req.sent:
                    req.send_json(500, {"error": "internal", "message": "something went wrong on the hub"})

        def do_GET(self):
            self._go("GET")

        def do_HEAD(self):
            self._go("HEAD")

        def do_POST(self):
            self._go("POST")

        def do_PUT(self):
            self._go("PUT")

        def do_DELETE(self):
            self._go("DELETE")

    return Handler


def serve(cfg: C.Config) -> ThreadingHTTPServer:
    cfg.data.mkdir(parents=True, exist_ok=True)
    cfg.episodes.mkdir(parents=True, exist_ok=True)
    db.init(cfg)
    db.migrate()
    srv = ThreadingHTTPServer((cfg.bind, cfg.port), make_handler(cfg))
    srv.daemon_threads = True
    srv.cfg = cfg
    # A module may define start(cfg): background work that must run from the hub's start
    # (recovering episodes stuck in checking, the stale-claim sweeper, layouts), not from the
    # first request that happens to reach it.
    for name in ROUTE_MODULES + ["layout"]:
        mod = importlib.import_module(f"{__package__}.{name}")
        if callable(getattr(mod, "start", None)):
            mod.start(cfg)
    return srv


def main():
    logging.basicConfig(level=os.environ.get("PCG_LOG", "INFO"), format="%(asctime)s %(name)s %(levelname)s %(message)s")
    cfg = C.load()
    srv = serve(cfg)
    log.info("papercast-group hub on %s:%s (auth %s)", cfg.bind, srv.server_address[1], cfg.auth)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    sys.exit(main())
