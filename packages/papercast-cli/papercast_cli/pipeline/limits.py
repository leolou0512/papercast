"""Claude's usage limit, detected as Leo's runner does (copied from
stacks/papercast/runner/papercast_runner/limits.py: the detection and the reset time. The
runner-wide pause is the CLI worker's here: it waits until `resume_at` and runs the job again).

Detection is broad on purpose. Claude Code 2.1.283's own schema has rate_limit_info `status` in
allowed | allowed_warning | rejected, `resetsAt` in epoch seconds, and `rateLimitType` in
five_hour | seven_day | ... . A turn counts as limited when it FAILED (an error result, no result,
or a non-zero exit) and one of these holds:
- a rate_limit_event's status is anything but allowed / allowed_warning;
- an assistant `error` field names a rate or usage limit;
- an error text of the turn (the error result's text, its `errors`, a synthetic assistant
  message, the last stderr line: never the model's own prose) looks like a limit.
Limits that are not about usage (context, budget, subagents ...) do not count.

Reset time: the rejected event's `resetsAt` (the later one of it and any full window of
`unifiedWindows`), else a time written in the error text. None found, or one that is stale or
more than 8 days out: resume in 60 minutes and try again.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta

MARGIN_S = 60                  # after the reset, before the job goes on
NO_RESET_S = 3600              # no reset time: wait this long, then try again
STALE_S = 300                  # a reset time this far in the past is not believed
MAX_AHEAD_S = 8 * 86400        # the weekly limit resets within 7 days; later is not believed
ALLOWED = ("allowed", "allowed_warning")

LIMIT_TEXT_RE = re.compile(r"usage[ _-]?limit|limit[ _-]?reached|rate[ _-]?limit|\bresets\b|"
                           r"hit your (?:\w+ )?limit|out of extra usage|"
                           r"\b(?:5|five)[ -]hour limit|\bweekly limit|\b429\b|too many requests",
                           re.I)
# Limits that are not Claude's usage limit; removed from a text before LIMIT_TEXT_RE looks.
NOT_USAGE_RE = re.compile(r"\b(?:context|budget|nesting|export|subagents?|concurrent|concurrency|"
                          r"token|output|turns?|file|size|recursion|watch|fast)[ _-]limit"
                          r"(?:[ _-]reached)?\b", re.I)
ERROR_FIELD_RE = re.compile(r"rate[ _-]?limit|usage[ _-]?limit|limit[ _-]?reached", re.I)
# Transient: an overloaded or failing API, the network. Not the limit, and not the paper's fault:
# the step is retried after a wait (core, PAPERCAST_TRANSIENT_BACKOFF_S) before it may fail.
TRANSIENT_RE = re.compile(r"overloaded|\b(?:500|502|503|504|529)\b|internal server error|"
                          r"service unavailable|bad gateway|gateway time-?out|"
                          r"\bE(?:CONNRESET|CONNREFUSED|CONNABORTED|TIMEDOUT|NOTFOUND|AI_AGAIN|PIPE|"
                          r"NETUNREACH|HOSTUNREACH)\b|socket hang up|network error|fetch failed|"
                          r"connection (?:error|reset|refused|closed)|timed? ?out\b|timeout", re.I)
TRANSIENT_STATUS = (500, 502, 503, 504, 529)

_PIPE_RE = re.compile(r"\|\s*(\d{10,13})\b")
_IN_RE = re.compile(r"\b(?:resets?|try again|available again|back)\s+in\s+"
                    r"(?:(?P<d>\d+)\s*(?:d|days?)\s*)?(?:(?P<h>\d+)\s*(?:h|hrs?|hours?)\s*)?"
                    r"(?:(?P<m>\d+)\s*(?:m|mins?|minutes?))?", re.I)
_ISO_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2})?(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})")
_MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
_CLOCK_RE = re.compile(
    r"\bresets?\s+(?:at\s+|on\s+)?"
    r"(?:(?P<mon>jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+"
    r"(?P<day>\d{1,2})(?:st|nd|rd|th)?,?\s+(?:at\s+)?)?"
    r"(?P<h>\d{1,2})(?::(?P<m>\d{2}))?\s*(?P<ap>am|pm)?"
    r"(?:\s*\((?P<tz>[A-Za-z_]+(?:/[A-Za-z0-9_+-]+)*)\))?", re.I)


# --------------------------------------------------------------------------- detection

@dataclass
class Hit:
    """One sighting of the limit. `resets_at`: epoch seconds as Claude reported it, or None."""
    resets_at: float | None
    how: str                    # "rate_limit_event" | "assistant_error" | "error_text"
    detail: str = ""


def _num(v) -> float | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str) and re.fullmatch(r"\s*\d+(?:\.\d+)?\s*", v):
        return float(v)
    return None


def epoch_of(v) -> float | None:
    """A reset time as Claude gives it (epoch seconds, measured on 2.1.283) in epoch seconds;
    epoch milliseconds and ISO 8601 strings are read too. None for anything else."""
    n = _num(v)
    if n is not None:
        if n > 1e11:                # milliseconds
            n /= 1000.0
        return n if n > 1e9 else None
    if isinstance(v, str) and _ISO_RE.fullmatch(v.strip()):
        s = v.strip().replace(" ", "T").replace("Z", "+00:00")
        if re.search(r"[+-]\d{4}$", s):
            s = s[:-2] + ":" + s[-2:]
        try:
            return datetime.fromisoformat(s).timestamp()
        except ValueError:
            return None
    return None


def info_limited(info) -> bool:
    """A rate_limit_info that says requests are refused: any status but allowed /
    allowed_warning. `overageStatus` is not the status (Leo's account reports
    overageStatus "rejected" on every allowed event, measured); paid extra usage in use
    covers the overflow and is not a limit."""
    if not isinstance(info, dict):
        return False
    st = info.get("status")
    if not isinstance(st, str) or not st or st in ALLOWED:
        return False
    if info.get("isUsingOverage") is True and info.get("overageStatus") in ALLOWED:
        return False
    return True


def info_resets(info) -> float | None:
    """The reset time of a refusing rate_limit_info: the latest of `resetsAt`, the window named
    by `rateLimitType`, and every window of `unifiedWindows` that is full."""
    if not isinstance(info, dict):
        return None
    cands = [epoch_of(info.get("resetsAt"))]
    uw = info.get("unifiedWindows")
    if isinstance(uw, dict):
        named = uw.get(info.get("rateLimitType")) if isinstance(info.get("rateLimitType"), str) else None
        if isinstance(named, dict):
            cands.append(epoch_of(named.get("resetsAt")))
        for w in uw.values():
            if isinstance(w, dict) and (_num(w.get("utilization")) or 0.0) >= 1.0:
                cands.append(epoch_of(w.get("resetsAt")))
    cands = [c for c in cands if c]
    return max(cands) if cands else None


def looks_like_limit(text) -> bool:
    if not isinstance(text, str) or not text:
        return False
    return bool(LIMIT_TEXT_RE.search(NOT_USAGE_RE.sub(" ", text)))


def _zone(name: str | None):
    if not name:
        return None
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except Exception:                               # noqa: BLE001  (unknown zone: local time)
        return None


def reset_from_text(text, now: float | None = None) -> float | None:
    """A reset time written in an error text, in epoch seconds: `…|1790455200` (the CLI's older
    form), an ISO time, or "resets 3pm" / "resets 3:30pm (Europe/London)" / "resets Oct 3 at
    2pm" (the next such time; the runner's local zone when none is named). None otherwise.
    A bare number with neither am/pm nor minutes is not a time."""
    if not isinstance(text, str) or not text:
        return None
    now = time.time() if now is None else now
    m = _PIPE_RE.search(text)
    if m:
        return epoch_of(m.group(1))
    m = _IN_RE.search(text)
    if m and any(m.group(k) for k in ("d", "h", "m")):
        return now + 86400 * int(m.group("d") or 0) + 3600 * int(m.group("h") or 0) + \
            60 * int(m.group("m") or 0)
    m = _ISO_RE.search(text)
    if m:
        v = epoch_of(m.group(0))
        if v:
            return v
    for m in _CLOCK_RE.finditer(text):
        h, mi, ap = int(m.group("h")), m.group("m"), (m.group("ap") or "").lower()
        if not ap and mi is None:
            continue
        mi = int(mi or 0)
        if ap:
            if not 1 <= h <= 12:
                continue
            h = h % 12 + (12 if ap == "pm" else 0)
        if h > 23 or mi > 59:
            continue
        tz = _zone(m.group("tz"))
        base = datetime.fromtimestamp(now, tz) if tz else datetime.fromtimestamp(now).astimezone()
        if m.group("mon"):
            month = _MONTHS.index(m.group("mon").lower()[:3]) + 1
            try:
                cand = base.replace(month=month, day=int(m.group("day")), hour=h, minute=mi,
                                    second=0, microsecond=0)
            except ValueError:
                continue
            if cand.timestamp() < now - STALE_S:
                try:
                    cand = cand.replace(year=cand.year + 1)
                except ValueError:
                    continue
        else:
            cand = base.replace(hour=h, minute=mi, second=0, microsecond=0)
            if cand.timestamp() < now - STALE_S:
                cand = cand + timedelta(days=1)
                if tz is None:           # the same wall-clock time across a DST change
                    cand = cand.replace(tzinfo=None).astimezone()
        return cand.timestamp()
    return None


def _failed(turn, rc) -> bool:
    res = getattr(turn, "result", None)
    return res is None or bool(res.get("is_error")) or rc not in (0, None)


def detect(turn, rc: int | None = None, stderr_last: str = "", now: float | None = None) -> Hit | None:
    """The limit, if `turn` (a claude.Turn) ended with it; else None. `rc`: the process's exit
    status (None when unknown, as for a live turn that ended with its `result`)."""
    if not _failed(turn, rc):
        return None
    errs = [str(e) for e in (getattr(turn, "errors", None) or [])]
    if any("model_not_found" in e or "authentication_failed" in e for e in errs):
        return None                  # §5.5's other signatures: a limit never looks like them
    res = getattr(turn, "result", None) or {}
    rerrs = [str(x) for x in (res.get("errors") or [])]
    if any("No conversation found" in e for e in rerrs):
        return None
    infos = list(getattr(turn, "rate_limits", None) or [])
    last = getattr(turn, "rate_limit", None)
    if last and not any(i is last for i in infos):
        infos.append(last)
    refusing = [i for i in infos if info_limited(i)]
    texts = []
    if res.get("is_error") and isinstance(res.get("result"), str):
        texts.append(res["result"])
    texts += rerrs
    texts += [str(t) for t in (getattr(turn, "synthetic_texts", None) or [])]
    if stderr_last:
        texts.append(str(stderr_last))
    text_hit = next((t for t in texts if looks_like_limit(t)), None)
    err_hit = next((e for e in errs if ERROR_FIELD_RE.search(e)), None)
    if res.get("api_error_status") == 429 and not text_hit:
        text_hit = "HTTP 429 " + (texts[0] if texts else "")
    if not (refusing or text_hit or err_hit):
        return None
    resets = None
    for i in reversed(refusing):
        resets = info_resets(i)
        if resets:
            break
    if resets is None:
        for t in texts:
            resets = reset_from_text(t, now)
            if resets:
                break
    if refusing:
        how, detail = "rate_limit_event", f"status {refusing[-1].get('status')!r}"
    elif err_hit:
        how, detail = "assistant_error", err_hit
    else:
        how, detail = "error_text", text_hit
    return Hit(resets, how, detail[:300])


def _error_texts(turn, stderr_last: str = "") -> list[str]:
    res = getattr(turn, "result", None) or {}
    texts = []
    if res.get("is_error") and isinstance(res.get("result"), str):
        texts.append(res["result"])
    texts += [str(x) for x in (res.get("errors") or [])]
    texts += [str(t) for t in (getattr(turn, "synthetic_texts", None) or [])]
    if stderr_last:
        texts.append(str(stderr_last))
    return texts


def transient(turn, rc: int | None = None, stderr_last: str = "") -> str | None:
    """Why a failed turn looks transient (overloaded or failing API, network, timeout), else
    None. Never the usage limit (detect() is asked first) and never the model's own words."""
    if not _failed(turn, rc) or detect(turn, rc, stderr_last) is not None:
        return None
    res = getattr(turn, "result", None) or {}
    if res.get("api_error_status") in TRANSIENT_STATUS:
        return f"HTTP {res['api_error_status']}"
    for t in _error_texts(turn, stderr_last):
        m = TRANSIENT_RE.search(t)
        if m:
            return t.strip()[:200]
    return None


def hit_of_info(info) -> Hit | None:
    """A refusing rate_limit_event seen while a process still runs: a Hit, else None."""
    if not info_limited(info):
        return None
    return Hit(info_resets(info), "rate_limit_event", f"status {info.get('status')!r}")


# --------------------------------------------------------------------------- when to go on

def resume_at(hit: Hit, now: float | None = None) -> tuple[float, bool]:
    """(epoch seconds the job may run again, guessed): the reset plus a minute; an hour from now
    when Claude gave no believable reset time (the runner's Pause._until_for)."""
    now = time.time() if now is None else now
    r = hit.resets_at
    if r is None or r < now - STALE_S or r > now + MAX_AHEAD_S:
        return now + NO_RESET_S, True
    return max(r, now) + MARGIN_S, False


def local_hhmm(t: float, now: float | None = None) -> str:
    """"14:05" in local time, with the weekday in front ("Tue 14:05") when it is a day or more
    away (the weekly limit), where HH:MM alone would read as today."""
    now = time.time() if now is None else now
    lt = time.localtime(t)
    return time.strftime("%a %H:%M" if t - now >= 86400 else "%H:%M", lt)


def message(hit: Hit, now: float | None = None) -> str:
    """"Claude usage limit reached; resets 14:05"."""
    when = local_hhmm(hit.resets_at, now) if hit.resets_at else "later"
    return f"Claude usage limit reached; resets {when}"
