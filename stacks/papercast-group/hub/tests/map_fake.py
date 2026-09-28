"""A stand-in for the hub's graph API (SPEC.md section 8), for test_map.py. The real one (graph.py,
A5) is built at the same time; this one follows the contract as written: fixtures in memory, every
edit recorded (method, path, body, the X-PCG header), the edit log and its revert (409 `moved`
when `expect` is not the newest op in scope, 409 `conflict` when the thing changed since), locked
graphs for admins only. Knobs: `fail` and `delay` for the next request matching a method and a
path pattern. It also serves the map (hub/static/map.js, map.css) and a page that mounts it
(map_harness/), with the hub's page CSP, so a CSP violation shows as a console error."""
from __future__ import annotations

import json
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

HERE = Path(__file__).resolve().parent
STATIC = HERE.parent / "static"
HARNESS = HERE / "map_harness"
# the hub's page CSP (hub/app.py PAGE_CSP, which is Leo's)
PAGE_CSP = ("default-src 'self'; img-src 'self' data:; media-src 'self'; style-src 'self'; "
            "script-src 'self'; connect-src 'self'; frame-src 'self'; frame-ancestors 'none'; "
            "base-uri 'none'; form-action 'none'; object-src 'none'")
TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8"}

LEO, BOB, ALICE = 1, 2, 3
USERS = {LEO: {"id": LEO, "name": "Leo", "email": "leo@example.org", "role": "viewer"},
         BOB: {"id": BOB, "name": "Bob", "email": "bob@example.org", "role": "contributor"},
         ALICE: {"id": ALICE, "name": "Alice", "email": "alice@example.org", "role": "admin"}}

TRPO, PPO, RLHF, INSTRUCT, DPO = "p_trpo00000000", "p_ppo000000000", "p_rlhf00000000", "p_instruct0000", "p_dpo000000000"
DDPM, SDE, FLOW, LOST = "p_ddpm00000000", "p_sde000000000", "p_flow00000000", "p_lost00000000"
RL, GEN, LOCKED = "g_rl00000000", "g_gen0000000", "g_lock000000"


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def ago(**kw) -> str:
    return iso(datetime.now(timezone.utc) - timedelta(**kw))


def fixtures() -> dict:
    papers = {
        TRPO: {"title": "Trust Region Policy Optimization", "year": 2015, "authors": ["John Schulman", "Sergey Levine", "Philipp Moritz"], "label": "TRPO"},
        PPO: {"title": "Proximal Policy Optimization Algorithms", "year": 2017, "authors": ["John Schulman", "Filip Wolski", "Prafulla Dhariwal"], "label": "PPO"},
        RLHF: {"title": "Deep Reinforcement Learning from Human Preferences", "year": 2017, "authors": ["Paul Christiano", "Jan Leike"], "label": "RLHF"},
        INSTRUCT: {"title": "Training language models to follow instructions with human feedback", "year": 2022, "authors": ["Long Ouyang", "Jeff Wu", "Xu Jiang"], "label": "InstructGPT"},
        DPO: {"title": "Direct Preference Optimization", "year": 2023, "authors": ["Rafael Rafailov", "Archit Sharma"], "label": "DPO"},
        DDPM: {"title": "Denoising Diffusion Probabilistic Models", "year": 2020, "authors": ["Jonathan Ho", "Ajay Jain", "Pieter Abbeel"], "label": "DDPM"},
        SDE: {"title": "Score-Based Generative Modeling through Stochastic Differential Equations", "year": 2021, "authors": ["Yang Song", "Jascha Sohl-Dickstein"], "label": "Score SDE"},
        FLOW: {"title": "Flow Matching for Generative Modeling", "year": 2023, "authors": ["Yaron Lipman", "Ricky T. Q. Chen"], "label": "Flow Matching"},
        LOST: {"title": "A Lost Paper About Robots", "year": 2024, "authors": ["Ada Lovelace"], "label": "Robots"},
    }
    for pid, p in papers.items():
        p["id"] = pid
        p["made_by"] = [{"id": BOB, "name": "Bob"}] if pid != DPO else [{"id": LEO, "name": "Leo"}, {"id": ALICE, "name": "Alice"}]
        p["listened"] = pid == DPO
        p["tags"] = []
    graphs = {
        RL: {"id": RL, "name": "Reinforcement learning", "tags": ["reinforcement learning"], "locked": False, "created_by": None,
             "members": [TRPO, PPO, RLHF, INSTRUCT, DPO], "deleted": False},
        GEN: {"id": GEN, "name": "Diffusion and generative models", "tags": ["diffusion"], "locked": False, "created_by": None,
              "members": [DDPM, SDE, FLOW], "deleted": False},
        LOCKED: {"id": LOCKED, "name": "Locked picks", "tags": [], "locked": True, "created_by": ALICE,
                 "members": [PPO, DDPM, DPO], "deleted": False},
    }
    # the settled positions (layout.py's job in the hub)
    layout = {
        RL: {TRPO: (-160, -70), PPO: (-50, -10), RLHF: (-70, 120), INSTRUCT: (70, 70), DPO: (180, 10)},
        GEN: {DDPM: (-120, 0), SDE: (0, 40), FLOW: (120, 0)},
        LOCKED: {PPO: (-100, 0), DDPM: (0, 90), DPO: (100, 0)},
    }
    links = {
        1: {"id": 1, "src": TRPO, "dst": PPO, "grade": "e", "origin": "human", "state": "active", "created_by": LEO, "created_at": ago(hours=2)},
        2: {"id": 2, "src": PPO, "dst": INSTRUCT, "grade": "s", "origin": "agent", "state": "active", "created_by": ALICE, "created_at": ago(days=2)},
        3: {"id": 3, "src": RLHF, "dst": INSTRUCT, "grade": "e", "origin": "human", "state": "active", "created_by": BOB, "created_at": ago(days=3)},
        4: {"id": 4, "src": INSTRUCT, "dst": DPO, "grade": "s", "origin": "human", "state": "active", "created_by": BOB, "created_at": ago(days=3)},
        5: {"id": 5, "src": PPO, "dst": DPO, "grade": "w", "origin": "human", "state": "active", "created_by": BOB, "created_at": ago(days=1)},
        6: {"id": 6, "src": DDPM, "dst": SDE, "grade": "s", "origin": "agent", "state": "active", "created_by": BOB, "created_at": ago(days=5)},
        7: {"id": 7, "src": SDE, "dst": FLOW, "grade": "e", "origin": "human", "state": "active", "created_by": BOB, "created_at": ago(days=5)},
        8: {"id": 8, "src": RLHF, "dst": DPO, "grade": "w", "origin": "human", "state": "removed", "created_by": BOB, "created_at": ago(days=4)},
    }
    log = [
        {"id": 1, "at": ago(hours=2), "user_id": LEO, "actor": "human", "op": "link.add", "target": "1",
         "before": None, "after": {"id": 1, "src": TRPO, "dst": PPO, "grade": "e", "state": "active"}, "revert_of": None, "reverted_by": None},
        {"id": 2, "at": ago(hours=1), "user_id": BOB, "actor": "human", "op": "link.grade", "target": "4",
         "before": {"id": 4, "src": INSTRUCT, "dst": DPO, "grade": "e"}, "after": {"id": 4, "src": INSTRUCT, "dst": DPO, "grade": "s"}, "revert_of": None, "reverted_by": None},
        {"id": 3, "at": ago(minutes=3), "user_id": BOB, "actor": "human", "op": "link.remove", "target": "8",
         "before": {"id": 8, "src": RLHF, "dst": DPO, "grade": "w", "state": "active"}, "after": {"id": 8, "src": RLHF, "dst": DPO, "grade": "w", "state": "removed"},
         "revert_of": None, "reverted_by": None},
    ]
    return {"papers": papers, "graphs": graphs, "layout": layout, "links": links, "log": log, "next_link": 9, "next_graph": 1}


class FakeHub:
    def __init__(self):
        self.lock = threading.RLock()
        self.s = fixtures()
        self.me = LEO
        self.requests: list = []          # (method, path, body, x-pcg)
        self.fail: dict = {}              # (method, path regex) -> (code, body), used once
        self.delay: dict = {}             # (method, path regex) -> seconds before answering, used once
        self.lag: dict = {}               # (method, path regex) -> seconds: the answer made now, sent late
        self.srv = None

    # ---------------------------------------------------------------- lifecycle
    def start(self):
        hub = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _go(self, method):
                u = urlsplit(self.path)
                q = {k: v[0] for k, v in parse_qs(u.query).items()}
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n) if n else b""
                try:
                    body = json.loads(raw) if raw else None
                except ValueError:
                    body = None
                code, out, ctype, extra = hub.handle(method, u.path, q, body, self.headers)
                data = out if isinstance(out, bytes) else json.dumps(out).encode()
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                for k, v in extra.items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                self._go("GET")

            def do_POST(self):
                self._go("POST")

            def do_PUT(self):
                self._go("PUT")

            def do_DELETE(self):
                self._go("DELETE")

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.srv.daemon_threads = True
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}/"
        return self

    def stop(self):
        if self.srv:
            self.srv.shutdown()
            self.srv.server_close()

    # ---------------------------------------------------------------- what the tests look at
    def edits(self, method=None, pattern=None):
        with self.lock:
            return [r for r in self.requests if r[0] != "GET" and (method is None or r[0] == method)
                    and (pattern is None or re.search(pattern, r[1]))]

    def active(self, lid):
        with self.lock:
            return self.s["links"][lid]["state"] == "active"

    def add_log(self, user_id, op, target, before, after, actor="human"):
        with self.lock:
            e = {"id": max([x["id"] for x in self.s["log"]] + [0]) + 1, "at": iso(datetime.now(timezone.utc)), "user_id": user_id,
                 "actor": actor, "op": op, "target": str(target), "before": before, "after": after, "revert_of": None, "reverted_by": None}
            self.s["log"].append(e)
            return e

    # ---------------------------------------------------------------- the API
    def handle(self, method, path, q, body, headers):
        if method == "GET" and not path.startswith("/api/"):
            return self.static(path)
        with self.lock:
            self.requests.append((method, path, body, headers.get("X-PCG")))
            wait = self._knob(self.delay, method, path)
            late = self._knob(self.lag, method, path)
            failing = self._knob(self.fail, method, path)
        if wait:
            time.sleep(wait)
        if failing:
            code, out = failing
            return code, out, "application/json", {}
        if method != "GET" and headers.get("X-PCG") != "1":
            return 403, {"error": "csrf", "message": "missing X-PCG"}, "application/json", {}
        with self.lock:
            try:
                code, out = self.route(method, path, q, body or {})
            except KeyError:
                code, out = 404, {"error": "not_found", "message": "not_found"}
            out = json.loads(json.dumps(out))       # as it is now, whatever happens while it waits
        if late:
            time.sleep(late)
        if code == 204:
            return 204, b"", "application/json", {}
        return code, out, "application/json", {}

    @staticmethod
    def _knob(d, method, path):
        for (m, rx), v in list(d.items()):
            if m == method and re.search(rx, path):
                del d[(m, rx)]
                return v
        return None

    def static(self, path):
        name = "index.html" if path in ("/", "/index.html") else path.lstrip("/")
        for base in (HARNESS, STATIC):
            p = base / name
            if "/" not in name and p.is_file():
                extra = {"Content-Security-Policy": PAGE_CSP} if p.suffix == ".html" else {}
                return 200, p.read_bytes(), TYPES.get(p.suffix, "application/octet-stream"), extra
        return 404, {"error": "not_found"}, "application/json", {}

    def user(self):
        return USERS[self.me]

    def meta(self, g):
        return {"id": g["id"], "name": g["name"], "tags": list(g["tags"]), "locked": g["locked"], "n": len(g["members"]),
                "created_by": g["created_by"]}

    def graph(self, gid):
        g = self.s["graphs"][gid]
        if g["deleted"]:
            raise KeyError(gid)
        return g

    def guard(self, g):
        if g["locked"] and self.user()["role"] != "admin":
            return 403, {"error": "locked", "message": "only admins change a locked graph"}
        return None

    def link_out(self, l):
        by = USERS.get(l["created_by"])
        return {"id": l["id"], "src": l["src"], "dst": l["dst"], "grade": l["grade"], "origin": l["origin"],
                "by": {"id": by["id"], "name": by["name"]} if by else None, "created_at": l["created_at"]}

    def detail(self, gid):
        g = self.graph(gid)
        mem = list(g["members"])
        pos = self.s["layout"].get(gid, {})
        nodes = []
        for pid in mem:
            p = self.s["papers"][pid]
            x, y = pos.get(pid, (None, None))
            nodes.append({"id": pid, "label": p["label"], "title": p["title"], "year": p["year"], "made_by": p["made_by"], "x": x, "y": y})
        links = [self.link_out(l) for l in self.s["links"].values() if l["state"] == "active" and l["src"] in mem and l["dst"] in mem]
        kids = {pid: [] for pid in mem}
        has_parent = set()
        for l in links:
            kids[l["src"]].append(l["dst"])
            has_parent.add(l["dst"])

        def reach(pid, seen):
            for k in kids[pid]:
                if k not in seen:
                    seen.add(k)
                    reach(k, seen)
            return seen
        desc = {pid: len(reach(pid, set())) for pid in mem}
        roots = [pid for pid in mem if pid not in has_parent and kids[pid]]
        path = sorted(mem, key=lambda pid: (self.s["papers"][pid]["year"] or 0, pid))
        for n in nodes:
            n["deg"] = sum(1 for l in links if n["id"] in (l["src"], l["dst"]))
        return {"graph": self.meta(g), "nodes": nodes, "links": links, "roots": roots, "start": roots, "path": path,
                "descendants": {k: v for k, v in desc.items() if v}}

    def log_out(self, e):
        u = USERS.get(e["user_id"])
        out = dict(e)
        out["user"] = {"id": u["id"], "name": u["name"]} if u else None
        return out

    def route(self, method, path, q, body):
        me = self.user()
        if method == "GET" and path == "/api/me":
            return 200, me
        if method == "GET" and path == "/api/library":
            return 200, {"papers": [dict(p) for p in self.s["papers"].values()]}
        if path == "/api/graphs":
            if method == "GET":
                return 200, {"graphs": [self.meta(g) for g in self.s["graphs"].values() if not g["deleted"]]}
            if method == "POST":
                name = str(body.get("name") or "").strip()
                if not name:
                    return 400, {"error": "bad_name", "message": "a graph needs a name"}
                gid = "g_new%07d" % self.s["next_graph"]
                self.s["next_graph"] += 1
                tags = [t for t in body.get("tags") or [] if isinstance(t, str)]
                mem = [pid for pid, p in self.s["papers"].items() if set(p["tags"]) & set(tags)]
                g = {"id": gid, "name": name, "tags": tags, "locked": False, "created_by": me["id"], "members": mem, "deleted": False}
                self.s["graphs"][gid] = g
                self.add_log(me["id"], "graph.create", gid, None, {"id": gid, "name": name, "tags": tags})
                return 201, {"graph": self.meta(g)}
        m = re.match(r"^/api/graphs/([^/]+)$", path)
        if m:
            gid = m.group(1)
            if method == "GET":
                return 200, self.detail(gid)
            g = self.graph(gid)
            if method == "PUT":
                if "locked" in body and me["role"] != "admin":
                    return 403, {"error": "forbidden", "message": "only admins lock a graph"}
                bad = self.guard(g) if "locked" not in body else None
                if bad:
                    return bad
                for k, op in (("name", "graph.rename"), ("tags", "graph.set_tags"), ("locked", "graph.lock")):
                    if k in body:
                        before, after = {k: g[k]}, {k: body[k]}
                        g[k] = body[k]
                        before["id"] = after["id"] = gid
                        self.add_log(me["id"], op, gid, before, after)
                return 200, {"graph": self.meta(g)}
            if method == "DELETE":
                if me["role"] != "admin" and g["created_by"] != me["id"]:
                    return 403, {"error": "forbidden", "message": "only its maker or an admin deletes a graph"}
                g["deleted"] = True
                self.add_log(me["id"], "graph.delete", gid, {"id": gid, "name": g["name"]}, None)
                return 204, None
        m = re.match(r"^/api/graphs/([^/]+)/papers(?:/([^/]+))?$", path)
        if m:
            g = self.graph(m.group(1))
            bad = self.guard(g)
            if bad:
                return bad
            if method == "POST":
                pid = body.get("paper_id")
                if pid not in self.s["papers"]:
                    return 404, {"error": "no_paper", "message": "no such paper"}
                if pid not in g["members"]:
                    g["members"].append(pid)
                    self.add_log(me["id"], "graph.add_paper", f"{g['id']}/{pid}", None, {"graph_id": g["id"], "paper_id": pid})
                p = self.s["papers"][pid]
                return 201, {"node": {"id": pid, "label": p["label"], "title": p["title"], "year": p["year"], "made_by": p["made_by"], "x": None, "y": None}}
            if method == "DELETE":
                pid = m.group(2)
                if pid in g["members"]:
                    g["members"].remove(pid)
                    self.add_log(me["id"], "graph.remove_paper", f"{g['id']}/{pid}", {"graph_id": g["id"], "paper_id": pid}, None)
                return 204, None
        if method == "POST" and path == "/api/links":
            src, dst, grade = body.get("src"), body.get("dst"), body.get("grade")
            if src not in self.s["papers"] or dst not in self.s["papers"] or grade not in ("e", "s", "w") or src == dst:
                return 400, {"error": "bad_link", "message": "a link needs two papers and a grade"}
            old = next((l for l in self.s["links"].values() if l["src"] == src and l["dst"] == dst), None)
            if old and old["state"] == "active":
                return 409, {"error": "exists", "message": "these two are linked already"}
            if old:
                before = {"id": old["id"], "src": src, "dst": dst, "grade": old["grade"], "state": "removed"}
                old.update(grade=grade, state="active", origin="human", created_by=me["id"], created_at=iso(datetime.now(timezone.utc)))
                l = old
            else:
                before = None
                lid = self.s["next_link"]
                self.s["next_link"] += 1
                l = {"id": lid, "src": src, "dst": dst, "grade": grade, "origin": "human", "state": "active", "created_by": me["id"],
                     "created_at": iso(datetime.now(timezone.utc))}
                self.s["links"][lid] = l
            self.add_log(me["id"], "link.add", l["id"], before, {"id": l["id"], "src": src, "dst": dst, "grade": grade, "state": "active"})
            return 201, {"link": self.link_out(l)}
        m = re.match(r"^/api/links/(\d+)$", path)
        if m:
            l = self.s["links"][int(m.group(1))]
            if l["state"] != "active":
                return 404, {"error": "not_found", "message": "that link was removed"}
            if method == "PUT":
                grade = body.get("grade")
                if grade not in ("e", "s", "w"):
                    return 400, {"error": "bad_grade", "message": "grade is e, s or w"}
                before = {"id": l["id"], "src": l["src"], "dst": l["dst"], "grade": l["grade"]}
                l["grade"] = grade
                self.add_log(me["id"], "link.grade", l["id"], before, dict(before, grade=grade))
                return 200, {"link": self.link_out(l)}
            if method == "DELETE":
                l["state"] = "removed"
                self.add_log(me["id"], "link.remove", l["id"], {"id": l["id"], "src": l["src"], "dst": l["dst"], "grade": l["grade"], "state": "active"},
                             {"id": l["id"], "src": l["src"], "dst": l["dst"], "grade": l["grade"], "state": "removed"})
                return 204, None
        m = re.match(r"^/api/papers/([^/]+)/label$", path)
        if m and method == "PUT":
            p = self.s["papers"][m.group(1)]
            text = str(body.get("label") or "").strip()
            if not text or len(text) > 40:
                return 400, {"error": "bad_label", "message": "a label is 1 to 40 characters"}
            p["label"] = text
            return 200, {"label": text}
        if method == "GET" and path == "/api/graph-log":
            n = int(q.get("limit") or 100)
            newest = sorted(self.s["log"], key=lambda e: -e["id"])[:n]
            return 200, {"entries": [self.log_out(e) for e in newest]}
        if method == "POST" and path == "/api/graph-log/revert":
            return self.revert(me, body.get("scope"), body.get("expect"))
        return 404, {"error": "not_found", "message": "not_found"}

    # ---------------------------------------------------------------- revert (SPEC.md section 8)
    def revert(self, me, scope, expect):
        if scope not in ("mine", "any"):
            return 400, {"error": "bad_scope", "message": "scope is mine or any"}
        newest = sorted(self.s["log"], key=lambda e: -e["id"])[:100]
        cand = next((e for e in newest if e["reverted_by"] is None and (scope == "any" or e["user_id"] == me["id"])), None)
        if cand is None:
            return 404, {"error": "nothing", "message": "nothing to undo"}
        if cand["id"] != expect:
            return 409, {"error": "moved", "message": "moved", "current": self.log_out(cand)}
        done = self.inverse(cand, me)
        if isinstance(done, str):
            return 409, {"error": "conflict", "message": done}
        cand["reverted_by"] = done["id"]
        done["revert_of"] = cand["id"]
        return 200, {"entry": self.log_out(done)}

    def inverse(self, e, me):
        """Apply the inverse of e against the current state; a string says why it cannot."""
        op, b, a = e["op"], e["before"] or {}, e["after"] or {}
        if op.startswith("link."):
            l = self.s["links"][int(e["target"])]
            cur = {"state": l["state"], "grade": l["grade"]}
            if op == "link.add":
                if cur != {"state": "active", "grade": a["grade"]}:
                    return "the link changed since"
                l["state"] = "removed"
                return self.add_log(me["id"], "link.remove", l["id"], dict(a), dict(a, state="removed"))
            if op == "link.remove":
                if cur["state"] != "removed":
                    return "the link is back already"
                l.update(state="active", grade=b["grade"])
                return self.add_log(me["id"], "link.add", l["id"], dict(a), dict(b, state="active"))
            if op == "link.grade":
                if cur != {"state": "active", "grade": a["grade"]}:
                    return "the grade changed since"
                l["grade"] = b["grade"]
                return self.add_log(me["id"], "link.grade", l["id"], dict(a), dict(b))
        if op in ("graph.add_paper", "graph.remove_paper"):
            gid, pid = e["target"].split("/")
            g = self.s["graphs"][gid]
            if op == "graph.add_paper":
                if pid not in g["members"]:
                    return "the paper left the graph since"
                g["members"].remove(pid)
                return self.add_log(me["id"], "graph.remove_paper", e["target"], a, None)
            if pid in g["members"]:
                return "the paper is back already"
            g["members"].append(pid)
            return self.add_log(me["id"], "graph.add_paper", e["target"], None, b)
        g = self.s["graphs"][e["target"]]
        if op == "graph.create":
            if g["deleted"]:
                return "the graph is gone already"
            g["deleted"] = True
            return self.add_log(me["id"], "graph.delete", g["id"], {"id": g["id"], "name": g["name"]}, None)
        if op == "graph.delete":
            if not g["deleted"]:
                return "the graph is back already"
            g["deleted"] = False
            return self.add_log(me["id"], "graph.create", g["id"], None, {"id": g["id"], "name": g["name"], "tags": g["tags"]})
        for k, o in (("name", "graph.rename"), ("tags", "graph.set_tags"), ("locked", "graph.lock")):
            if op == o:
                if g[k] != a[k]:
                    return "the graph changed since"
                g[k] = b[k]
                return self.add_log(me["id"], op, g["id"], dict(a), dict(b))
        return "cannot undo " + op


if __name__ == "__main__":        # a look by hand: python3 map_fake.py, then open the URL
    h = FakeHub().start()
    print(h.url, flush=True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        h.stop()
