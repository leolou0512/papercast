"""In-process pub/sub for the page's live events (SPEC.md section 7, GET /api/events).

publish() from any thread; each SSE connection holds one subscription queue. An event carries
an increasing id, a kind (paper, episode, graph, log) and JSON data; `users` limits it to those
user ids (None: everyone)."""
from __future__ import annotations

import itertools
import queue
import threading

_lock = threading.Lock()
_subs: set = set()
_ids = itertools.count(1)


class Sub:
    def __init__(self, user_id):
        self.user_id = user_id
        self.q: queue.Queue = queue.Queue(maxsize=1000)


def subscribe(user_id) -> Sub:
    s = Sub(user_id)
    with _lock:
        _subs.add(s)
    return s


def unsubscribe(s: Sub) -> None:
    with _lock:
        _subs.discard(s)


def publish(kind: str, data, users=None) -> int:
    eid = next(_ids)
    with _lock:
        subs = list(_subs)
    for s in subs:
        if users is not None and s.user_id not in users:
            continue
        try:
            s.q.put_nowait((eid, kind, data))
        except queue.Full:          # a stuck connection loses events; the page resyncs on reconnect
            pass
    return eid
