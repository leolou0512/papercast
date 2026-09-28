"""Small helpers: times as the person reads them, and the hub's ISO times."""
from __future__ import annotations

import time
from datetime import datetime, timezone


def now_iso(t: float | None = None) -> str:
    """UTC with seconds and Z, the hub's form: 2026-09-28T04:12:09Z."""
    return datetime.fromtimestamp(time.time() if t is None else t, timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s) -> float | None:
    """Epoch seconds of an ISO 8601 time (the hub's `...Z` or with an offset), else None."""
    if not isinstance(s, str) or not s:
        return None
    try:
        d = datetime.fromisoformat(s.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.timestamp()


def local_hhmm(t: float, now: float | None = None) -> str:
    """"07:01" in local time; with the weekday ("Tue 07:01") when it is a day or more away (the
    weekly limit), where HH:MM alone would read as today. As Leo's runner shows it."""
    now = time.time() if now is None else now
    return time.strftime("%a %H:%M" if abs(t - now) >= 86400 else "%H:%M", time.localtime(t))


def ago(t: float | None, now: float | None = None) -> str:
    """"3 min ago", "2 h ago", "yesterday 14:02" for listing when a job was added."""
    if not t:
        return ""
    now = time.time() if now is None else now
    d = max(0.0, now - t)
    if d < 60:
        return "just now"
    if d < 3600:
        return f"{int(d // 60)} min ago"
    if d < 86400:
        return f"{int(d // 3600)} h ago"
    return time.strftime("%a %d %b %H:%M", time.localtime(t))
