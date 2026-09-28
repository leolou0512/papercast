"""Settled positions for each graph (SPEC.md section 8, Layout). Owner: A5.

A port of /home/leo/papercast-itest/lineage/ground_state.py: map.js's force equations (link
springs to a rest length, many-body repulsion, a pull to the centre, collision) with map.js's
defaults, run to rest with slow cooling.

- After a change to a graph (debounced: 30 s after the last change, at most 2 min after the
  first) a background thread warm-starts from the stored positions (a new paper at its placed
  neighbours' centroid, else near the centre) and cools for 400 steps. A graph whose members and
  links did not change since its layout is left exactly where it is.
- A full relayout runs from 8 starts (the current positions, graph distances, spectral, random)
  and keeps the one with the shortest total edge length, turned to match the old layout: on
  demand (POST /api/graphs/<id>/relayout, admin) and nightly (python3 -m hub.layout --all).
- One layout at a time (a lock file in $PCG_DATA, shared with the nightly process), the thread
  niced, each run bounded in time. numpy is imported only here, and only when a layout runs;
  without it the page gets provisional positions (graph.py)."""
from __future__ import annotations

import argparse
import fcntl
import logging
import math
import os
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

from . import db, events

log = logging.getLogger("pcg.layout")

PARAMS = dict(center=0.4, repel=10, link=0.7, dist=70, node=1.0)      # map.js DEFAULTS
ALPHA_MIN = 0.001
FULL_STARTS, FULL_TICKS = 8, 2500           # ground_state.py: 2500 steps of slow cooling per start
WARM_TICKS, WARM_ALPHA = 400, 0.1           # a warm start is already near rest: cool gently from there
DEBOUNCE_S, MAX_WAIT_S = 30.0, 120.0
WARM_BUDGET_S, FULL_BUDGET_S = 120.0, 900.0
NIGHTLY_KEEP = 0.02                         # nightly: keep today's layout unless a start is 2% shorter
NICE = 10
AUTO = os.environ.get("PCG_LAYOUT", "1") != "0"      # background layouts after changes (tests turn it off)
ALL = "*"

_np = None


def _numpy():
    global _np
    if _np is None:
        for k in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS"):
            os.environ.setdefault(k, "1")        # the eigensolvers must not take every core of a shared machine
        import numpy
        _np = numpy
    return _np


# ---------------------------------------------------------------- the physics

def simulate(pos, s, t, deg, ticks, alpha0=1.0, params=None, deadline=None):
    """map.js tick() (ground_state.py run()) for `ticks` steps, alpha cooling geometrically from
    alpha0 to ALPHA_MIN. pos: (n, 2); s, t: link ends (index arrays); deg: degrees (at least 1).
    -> (positions, finished before the deadline)"""
    np = _numpy()
    P = dict(PARAMS, **(params or {}))
    n = len(pos)
    if n == 0 or ticks <= 0:
        return np.array(pos, dtype=np.float64).reshape(n, 2), True
    ft = np.float64 if n <= 500 else np.float32          # float32 halves the time at 1,000 papers
    x, y = np.array(pos[:, 0], dtype=ft), np.array(pos[:, 1], dtype=ft)
    vx, vy = np.zeros(n, ft), np.zeros(n, ft)
    with np.errstate(all="ignore"):
        st = 1 / np.minimum(deg[s], deg[t])
        bias = deg[s] / (deg[s] + deg[t])
        kt = (st * P["link"] * bias).astype(ft)           # the target end takes `bias` of the spring
        ks = (st * P["link"] * (1 - bias)).astype(ft)
        r = ((3.5 + 1.25 * np.sqrt(deg)) * P["node"]).astype(ft)
        rr = r[:, None] + r[None, :] + 3
        rr2 = rr * rr
        ch, dmax2, cs0, dist = -P["repel"] * 32, (P["dist"] * 8) ** 2, P["center"] * 0.08, P["dist"]
        diag = np.arange(n)
        DX, DY, D2, W = (np.empty((n, n), ft) for _ in range(4))
        alpha, keep = alpha0, (ALPHA_MIN / alpha0) ** (1 / ticks)
        for k in range(ticks):
            if deadline is not None and k % 25 == 0 and time.monotonic() > deadline:
                return np.stack([x, y], 1).astype(np.float64), False
            alpha *= keep
            if len(s):
                dx = x[t] + vx[t] - x[s] - vx[s]
                dy = y[t] + vy[t] - y[s] - vy[s]
                d = np.sqrt(dx * dx + dy * dy) + 1e-9
                f = (d - dist) / d * alpha
                fx, fy = dx * f, dy * f
                vx -= np.bincount(t, fx * kt, n)
                vy -= np.bincount(t, fy * kt, n)
                vx += np.bincount(s, fx * ks, n)
                vy += np.bincount(s, fy * ks, n)
            np.subtract(x[None, :], x[:, None], out=DX)
            np.subtract(y[None, :], y[:, None], out=DY)
            np.multiply(DX, DX, out=D2)
            D2 += DY * DY
            D2[diag, diag] = dmax2 * 2                     # no force of a paper on itself
            np.maximum(D2, 36, out=W)
            np.divide(ch * alpha, W, out=W)                # many-body, within 8 rest lengths
            W[D2 >= dmax2] = 0
            ci, cj = np.nonzero(D2 < rr2)                  # collision: only the few overlapping pairs
            if len(ci):
                dd = np.sqrt(D2[ci, cj])
                W[ci, cj] -= (rr[ci, cj] - dd) / np.maximum(dd, 1e-6) * 0.35
            vx += np.einsum("ij,ij->i", DX, W)
            vy += np.einsum("ij,ij->i", DY, W)
            cs = cs0 * alpha
            vx -= x * cs
            vy -= y * cs
            vx *= 0.6
            vy *= 0.6
            x += vx
            y += vy
    return np.stack([x, y], 1).astype(np.float64), True


def total_length(pos, s, t) -> float:
    np = _numpy()
    if not len(s):
        return 0.0
    return float(np.sqrt(((pos[s] - pos[t]) ** 2).sum(1)).sum())


def _hop_distances(n, s, t):
    np = _numpy()
    adj = [[] for _ in range(n)]
    for a, b in zip(s.tolist(), t.tolist()):
        adj[a].append(b)
        adj[b].append(a)
    D = np.empty((n, n))
    for src in range(n):
        row = [-1] * n
        row[src] = 0
        frontier, d = [src], 0
        while frontier:
            d += 1
            nxt = []
            for u in frontier:
                for v in adj[u]:
                    if row[v] < 0:
                        row[v] = d
                        nxt.append(v)
            frontier = nxt
        D[src] = row
    return D


def _mds_start(n, s, t):
    """Classical scaling of the hop distances: the stand-in for ground_state.py's Kamada-Kawai start
    (no networkx on the hub)."""
    np = _numpy()
    D = _hop_distances(n, s, t)
    D[D < 0] = (D.max() + 1) if D.max() > 0 else 1        # other components: one step beyond the farthest
    D2 = D * D
    B = -0.5 * (D2 - D2.mean(0)[None, :] - D2.mean(1)[:, None] + D2.mean())
    vals, vecs = np.linalg.eigh(B)
    return vecs[:, -2:] * np.sqrt(np.maximum(vals[-2:], 1e-9))


def _spectral_start(n, s, t):
    np = _numpy()
    A = np.zeros((n, n))
    A[s, t] = 1
    A[t, s] = 1
    vals, vecs = np.linalg.eigh(np.diag(A.sum(1)) - A)
    return vecs[:, 1:3]


def _place(ids, s, t, stored, rng):
    """The warm start: stored positions; a new paper at its placed neighbours' centroid (a chain of
    new papers follows), else near the centre. -> (positions, which were stored)"""
    np = _numpy()
    n = len(ids)
    P = np.zeros((n, 2))
    have = np.zeros(n, bool)
    for i, pid in enumerate(ids):
        if pid in stored:
            P[i] = stored[pid]
            have[i] = True
    old = have.copy()
    if not have.all():
        nb = [[] for _ in range(n)]
        for a, b in zip(s.tolist(), t.tolist()):
            nb[a].append(b)
            nb[b].append(a)
        todo = np.flatnonzero(~have).tolist()
        for _ in range(4):
            moved = False
            for i in todo:
                if have[i]:
                    continue
                js = [j for j in nb[i] if have[j]]
                if js:
                    P[i] = P[js].mean(0) + rng.normal(scale=3.0, size=2)
                    have[i] = moved = True
            if not moved:
                break
        centre = P[old].mean(0) if old.any() else np.zeros(2)
        for i in todo:
            if not have[i]:
                P[i] = centre + rng.normal(scale=PARAMS["dist"] * 0.5, size=2)
                have[i] = True
    # two papers on one spot never separate (the forces on them are equal): nudge exact duplicates
    _, first = np.unique(P, axis=0, return_index=True)
    dup = np.ones(n, bool)
    dup[first] = False
    if dup.any():
        P[dup] += rng.normal(scale=0.5, size=(int(dup.sum()), 2))
    return P, old


def _align(pos, ids, stored):
    """Turn (and maybe mirror) a new layout about the centre to sit on the old one. The springs'
    degree split leaves a small net torque, so every run turns the whole graph a little (0.8
    degrees per warm run at 160 papers): turned back, an unchanged paper moves ~0.1 instead of
    ~5. Only about the centre: every force is unchanged by that, so the layout stays at rest.
    (Not recentred or shifted either: the same split leaves a small net force, so at rest the
    mean sits a little off the centre.)"""
    np = _numpy()
    common = [k for k, pid in enumerate(ids) if pid in stored]
    if len(common) < 3:
        return pos
    old = np.array([stored[ids[k]] for k in common], dtype=np.float64)
    U, _, Vt = np.linalg.svd(pos[common].T @ old)
    return pos @ (U @ Vt)


def _full(ids, s, t, deg, stored, rng, deadline, keep_margin):
    """From up to 8 starts, keep the shortest total edge length (the current positions stay
    unless a start beats them by keep_margin). -> (positions, kept start, [(start, length, finished)])"""
    np = _numpy()
    n = len(ids)
    R = PARAMS["dist"] * math.sqrt(n) * 0.9
    starts = []
    if stored:
        starts.append(("current", _place(ids, s, t, stored, rng)[0], False))
    for name, fn in (("graph distances", _mds_start), ("spectral", _spectral_start)):
        if len(starts) < FULL_STARTS and len(s):
            try:
                starts.append((name, fn(n, s, t), True))
            except Exception:                       # an eigensolver that does not converge: skip that start
                log.exception("layout start %s failed", name)
    k = 0
    while len(starts) < FULL_STARTS:
        starts.append((f"random {k}", rng.normal(size=(n, 2)), True))
        k += 1
    results, per = [], None
    for name, p0, scale in starts:
        if results and per is not None and time.monotonic() + per > deadline:
            break                                   # the next start would not finish in time
        t1 = time.monotonic()
        if scale:
            p0 = p0 - p0.mean(0)
            p0 = p0 / (np.abs(p0).max() + 1e-9) * R + rng.normal(scale=1.0, size=p0.shape)
            pos, done = simulate(p0, s, t, deg, FULL_TICKS, 1.0, deadline=deadline)
            if done:
                per = time.monotonic() - t1
        else:
            pos, done = simulate(p0, s, t, deg, WARM_TICKS, WARM_ALPHA, deadline=deadline)
        L = total_length(pos, s, t)
        if np.isfinite(pos).all() and math.isfinite(L):
            results.append((name, L, pos, done))
    tried = [(name, round(L, 1), done) for name, L, _, done in results]
    ok = [r for r in results if r[3]] or results
    if not ok:
        return _place(ids, s, t, stored, rng)[0], "none", tried
    best = min(ok, key=lambda r: r[1])
    cur = next((r for r in ok if r[0] == "current"), None)
    if cur is not None and best is not cur and best[1] > cur[1] * (1 - keep_margin):
        best = cur
    pos = best[2]
    if stored:
        pos = _align(pos, ids, stored)
    return pos, best[0], tried


# ---------------------------------------------------------------- one graph, stored

def run(graph_id, mode="warm", budget=None, keep_margin=0.0, seed=None):
    """Lay out one graph and store the positions. mode "warm" (from the stored positions; nothing
    happens when the graph's members and links did not change) or "full" (8 starts).
    -> stats dict, or None when the graph is gone."""
    np = _numpy()
    from . import graph
    inp = graph.layout_input(graph_id)
    if inp is None:
        return None
    ids, pairs, stored, sig, state = inp["ids"], inp["links"], inp["stored"], inp["sig"], inp["state"]
    n = len(ids)
    stats = {"graph_id": graph_id, "mode": mode, "n": n, "links": len(pairs)}
    if mode == "warm" and state and state.get("sig") == sig and len(stored) == n:
        return dict(stats, mode="unchanged", seconds=0.0, rev=state.get("rev"))
    if mode == "warm" and not stored and n > 1:
        mode = "full"                               # nothing to start from
    t0 = time.monotonic()
    deadline = t0 + (budget if budget is not None else (WARM_BUDGET_S if mode == "warm" else FULL_BUDGET_S))
    rng = np.random.default_rng(int(sig[:8], 16) if seed is None else seed)
    idx = {pid: k for k, pid in enumerate(ids)}
    s = np.array([idx[a] for a, _ in pairs], dtype=np.intp)
    t = np.array([idx[b] for _, b in pairs], dtype=np.intp)
    deg = np.zeros(n)
    np.add.at(deg, s, 1)
    np.add.at(deg, t, 1)
    deg = np.maximum(deg, 1)
    tried = []
    if n <= 1:
        pos = np.array([stored.get(ids[0], (0.0, 0.0))], dtype=np.float64) if n else np.zeros((0, 2))
        kept = "trivial"
    elif mode == "warm":
        p0, _ = _place(ids, s, t, stored, rng)
        pos, done = simulate(p0, s, t, deg, WARM_TICKS, WARM_ALPHA, deadline=deadline)
        pos = _align(pos, ids, stored) if np.isfinite(pos).all() else p0
        kept = "warm" if done else "warm (stopped at the time limit)"
    else:
        pos, kept, tried = _full(ids, s, t, deg, stored, rng, deadline, keep_margin)
    L = total_length(pos, s, t)
    secs = time.monotonic() - t0
    positions = {pid: (round(float(pos[k, 0]), 1), round(float(pos[k, 1]), 1)) for k, pid in enumerate(ids)}
    rev = _store(graph_id, positions, sig, mode, n, L, secs, kept)
    if rev is not None:
        events.publish("graph", {"id": graph_id, "change": "layout", "rev": rev})
    return dict(stats, mode=mode, seconds=round(secs, 2), total_length=round(L, 1), kept=kept, tried=tried, rev=rev)


def _store(graph_id, positions, sig, mode, n, L, secs, kept):
    at = db.now()
    with db.transaction() as c:
        if not c.execute("SELECT 1 FROM graphs WHERE id = ?", (graph_id,)).fetchone():
            return None
        c.execute("DELETE FROM layout WHERE graph_id = ?", (graph_id,))
        c.executemany("INSERT INTO layout(graph_id, paper_id, x, y, updated_at) VALUES (?, ?, ?, ?, ?)",
                      [(graph_id, pid, x, y, at) for pid, (x, y) in positions.items()])
        c.execute("INSERT INTO layout_state(graph_id, rev, sig, mode, n, total_length, seconds, kept, updated_at) "
                  "VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(graph_id) DO UPDATE SET rev = rev + 1, "
                  "sig = excluded.sig, mode = excluded.mode, n = excluded.n, total_length = excluded.total_length, "
                  "seconds = excluded.seconds, kept = excluded.kept, updated_at = excluded.updated_at",
                  (graph_id, sig, mode, n, round(L, 1), round(secs, 2), kept, at))
        return c.execute("SELECT rev FROM layout_state WHERE graph_id = ?", (graph_id,)).fetchone()[0]


_run_lock = threading.Lock()


@contextmanager
def one_at_a_time():
    """No two layouts at once, in this process or across processes (the nightly run)."""
    with _run_lock:
        path = Path(db._path).parent / "layout.lock"
        with open(path, "a+") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)


# ---------------------------------------------------------------- the background thread

_cv = threading.Condition()
_due: dict = {}               # graph id (or ALL) -> [when, first asked, mode]
_busy = False
_thread = None
_warned = False


def schedule(graph_ids=None, delay=None, mode="warm", reset=True, force=False) -> None:
    """Lay these graphs out (None: every graph) once changes stop: DEBOUNCE_S after the last one
    (reset=False: do not push an already planned run back), at most MAX_WAIT_S after the first;
    `delay` asks for sooner."""
    if not (AUTO or force):
        return
    now = time.monotonic()
    d = DEBOUNCE_S if delay is None else delay
    with _cv:
        for gid in ([ALL] if graph_ids is None else graph_ids):
            e = _due.get(gid)
            if e is None:
                _due[gid] = [now + d, now, mode]
                continue
            if mode == "full":
                e[2] = "full"
            if delay is not None:
                e[0] = min(e[0], now + d)
            elif reset:
                e[0] = min(max(e[0], now + d), e[1] + MAX_WAIT_S)
        _start()
        _cv.notify_all()


def request_full(graph_id) -> None:
    """A full relayout (8 starts) now, in the background."""
    schedule([graph_id], delay=0.0, mode="full", force=True)


def wait_idle(timeout: float = 60.0) -> bool:
    """For tests and shutdown: wait until nothing is planned or running."""
    end = time.monotonic() + timeout
    with _cv:
        while _due or _busy:
            left = end - time.monotonic()
            if left <= 0:
                return False
            _cv.wait(min(left, 0.1))
    return True


def _start():
    global _thread
    if _thread is None or not _thread.is_alive():
        _thread = threading.Thread(target=_loop, name="pcg-layout", daemon=True)
        _thread.start()


def _nice_thread():
    """Linux nices threads one by one: lower this one only."""
    try:
        tid = threading.get_native_id()
        if os.getpriority(os.PRIO_PROCESS, tid) < NICE:
            os.setpriority(os.PRIO_PROCESS, tid, NICE)
    except (AttributeError, OSError):
        pass


def _loop():
    global _busy
    _nice_thread()
    while True:
        with _cv:
            while True:
                now = time.monotonic()
                if _due:
                    gid, e = min(_due.items(), key=lambda kv: kv[1][0])
                    if e[0] <= now:
                        del _due[gid]
                        _busy = True
                        break
                    _cv.wait(e[0] - now)
                else:
                    _cv.wait()
        try:
            _job(gid, e[2])
        except Exception:
            log.exception("layout of %s failed", gid)
        finally:
            with _cv:
                _busy = False
                _cv.notify_all()


def _job(gid, mode):
    global _warned
    try:
        _numpy()
    except ImportError:
        if not _warned:
            log.warning("numpy is not installed: graphs keep provisional positions")
            _warned = True
        return
    from . import graph
    for g in (graph.graph_ids() if gid == ALL else [gid]):
        with one_at_a_time():
            st = run(g, mode)
        if st and st["mode"] != "unchanged":
            log.info("layout %s: %s, %d papers, %d links, %.1f s, total edge length %.0f (%s)", g, st["mode"],
                     st["n"], st["links"], st["seconds"], st["total_length"], st["kept"])


# ---------------------------------------------------------------- the nightly job

def main(argv=None) -> int:
    global AUTO
    ap = argparse.ArgumentParser(prog="python3 -m hub.layout",
                                 description="Lay out the hub's graphs. Nightly: --all (a full relayout of every graph).")
    ap.add_argument("--all", action="store_true", help="every graph")
    ap.add_argument("--graph", action="append", default=[], metavar="ID", help="this graph (repeatable)")
    ap.add_argument("--warm", action="store_true", help="warm start only (a graph that did not change is skipped)")
    ap.add_argument("--budget", type=float, help=f"seconds per graph (default {FULL_BUDGET_S:.0f} full, {WARM_BUDGET_S:.0f} warm)")
    ap.add_argument("--keep", type=float, default=NIGHTLY_KEEP,
                    help="keep the current layout unless a start is this much shorter (default %(default)s)")
    a = ap.parse_args(argv)
    if not a.all and not a.graph:
        ap.error("say --all or --graph ID")
    try:
        os.nice(NICE)
    except OSError:
        pass
    logging.basicConfig(level=os.environ.get("PCG_LOG", "INFO"), format="%(asctime)s %(name)s %(levelname)s %(message)s")
    data = Path(os.environ.get("PCG_DATA", str(Path.home() / "papercast-group" / "data")))
    if not (data / "hub.db").exists():
        print(f"no hub database at {data / 'hub.db'} (set PCG_DATA)", file=sys.stderr)
        return 1
    AUTO = False                                    # this process runs the layouts itself
    db.init(SimpleNamespace(data=data))
    from . import graph
    graph.ensure_schema()
    graph.resolve_pending()                         # links waiting for a paper that came in some other way
    ids = graph.graph_ids() if a.all else a.graph
    for gid in ids:
        with one_at_a_time():
            st = run(gid, "warm" if a.warm else "full", budget=a.budget, keep_margin=a.keep)
        if st is None:
            print(f"{gid}: no such graph")
            continue
        print(f"{gid}: {st['mode']}, {st['n']} papers, {st['links']} links, {st.get('seconds', 0)} s, "
              f"total edge length {st.get('total_length', '-')}, kept {st.get('kept', '-')}", flush=True)
    return 0


if __name__ == "__main__":
    from hub.layout import main as _main            # one module instance, whatever graph.py imports
    sys.exit(_main())
