"""A fake hub with only the voice API of SPEC.md section 9, for the worker's tests (the real one is
A3's hub/voiceq.py). It keeps everything in memory and records every call.

    hub = FakeHub(token="t"); hub.add("e_aaaaaaaaaaaa", "Title", script); hub.start()
    ... hub.url ... hub.calls ... hub.audio[eid] ... hub.stop()
"""
from __future__ import annotations

import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FakeHub:
    def __init__(self, token: str = "pcgw_test"):
        self.token = token
        self.lock = threading.Lock()
        self.queue: list[str] = []
        self.episodes: dict[str, dict] = {}
        self.calls: list[tuple[str, str]] = []
        self.statuses: dict[str, list[dict]] = {}
        self.audio: dict[str, dict] = {}
        self.failures: dict[str, dict] = {}
        self.timings: dict[str, list] = {}       # every timings body sent, per episode
        self.uploads: list[tuple[str, str | None]] = []     # (episode, X-Voice) per audio upload
        self.claims = 0
        # knobs: an HTTP status to answer instead, per (method, route name), for the next n calls
        self.inject: dict[tuple[str, str], list[int]] = {}
        self.srv = None

    def add(self, eid: str, title: str, script: str, first_author: str = "Ada Lovelace", voice=None):
        """Queue an episode; again with `voice` (hub/voices.py's claim voice) for a voice change."""
        with self.lock:
            self.episodes[eid] = {"episode_id": eid, "title": title, "first_author": first_author,
                                  "year": 2022, "script": script, "state": "waiting-for-gpu", "voice": voice}
            self.queue.append(eid)

    def lose(self, eid: str):
        """The episode is deleted on the hub: every call about it answers 404 from now on."""
        with self.lock:
            self.episodes[eid]["state"] = "deleted"

    def fail_next(self, method: str, route: str, *codes: int):
        with self.lock:
            self.inject.setdefault((method, route), []).extend(codes)

    def phases(self, eid: str) -> list[str]:
        out = []
        for s in self.statuses.get(eid, []):
            if not out or out[-1] != s.get("phase"):
                out.append(s.get("phase"))
        return out

    def start(self):
        hub = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def answer(self, code, obj=None, body=None, ctype="application/json"):
                data = body if body is not None else (json.dumps(obj).encode() if obj is not None else b"")
                self.send_response(code)
                if data or code != 204:
                    self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                if data:
                    self.wfile.write(data)

            def go(self, method):
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n) if n else b""
                path = self.path.split("?")[0]
                parts = path.strip("/").split("/")
                route = parts[-1] if parts[:2] == ["api", "voice"] else "?"
                eid = parts[2] if len(parts) == 4 else None
                with hub.lock:
                    hub.calls.append((method, path))
                    inj = hub.inject.get((method, route))
                    code = inj.pop(0) if inj else None
                if self.headers.get("Authorization") != f"Bearer {hub.token}":
                    return self.answer(401, {"error": "login_required"})
                if code:
                    return self.answer(code, {"error": "injected", "message": f"fake {code}"})
                if method == "POST" and path == "/api/voice/claim":
                    with hub.lock:
                        hub.claims += 1
                        while hub.queue:
                            e = hub.episodes[hub.queue.pop(0)]
                            if e["state"] == "deleted":
                                continue
                            e["state"] = "claimed"
                            return self.answer(200, {
                                "episode_id": e["episode_id"], "title": e["title"],
                                "first_author": e["first_author"], "year": e["year"],
                                "script_url": f"/api/voice/{e['episode_id']}/script",
                                "voice": e.get("voice")})
                    return self.answer(204)
                e = hub.episodes.get(eid)
                if e is None or e["state"] == "deleted":
                    return self.answer(404, {"error": "not_found"})
                if method == "GET" and route == "script":
                    return self.answer(200, body=e["script"].encode(), ctype="text/markdown; charset=utf-8")
                if method == "PUT" and route == "status":
                    with hub.lock:
                        hub.statuses.setdefault(eid, []).append(json.loads(body))
                    return self.answer(200, {"ok": True})
                if method == "PUT" and route == "timings":
                    with hub.lock:
                        hub.timings.setdefault(eid, []).append(json.loads(body))
                    return self.answer(200, {"episode_id": eid, "stored": "next"})
                if method == "PUT" and route == "audio":
                    with hub.lock:
                        hub.uploads.append((eid, self.headers.get("X-Voice")))
                        hub.audio[eid] = {"bytes": body, "duration_s": float(self.headers.get("X-Duration-S")),
                                          "sha256": self.headers.get("X-Sha256"),
                                          "ctype": self.headers.get("Content-Type"),
                                          "ok": hashlib.sha256(body).hexdigest() == self.headers.get("X-Sha256")}
                        e["state"] = "ready"
                    return self.answer(200, {"episode_id": eid, "state": "ready"})
                if method == "POST" and route == "failed":
                    with hub.lock:
                        hub.failures[eid] = json.loads(body)
                        e["state"] = "failed"
                    return self.answer(200, {"ok": True})
                return self.answer(404, {"error": "not_found"})

            def do_GET(self):
                self.go("GET")

            def do_PUT(self):
                self.go("PUT")

            def do_POST(self):
                self.go("POST")

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.srv.daemon_threads = True
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        return self

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.srv.server_address[1]}"

    def stop(self):
        if self.srv:
            self.srv.shutdown()
            self.srv.server_close()
