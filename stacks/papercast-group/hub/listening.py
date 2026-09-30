"""Listening time: how much each person listened, per day (Leo, 2026-09-30: "a small dashboard
for everyone detailing how much they've listened per day per week etc in the style of GitHub
heatmap for contribution").

  GET /api/listening/me?tz=<min>      your days, totals, streaks, the last 12 weeks, `shown`
  GET /api/listening/group?tz=<min>   every member who turned "Show mine to the group" on, most
                                      minutes in the last 30 days first:
                                      {"people": [{"user", "days", "last30_s"}]}
  PUT /api/me/listening-visibility    {"shown": bool}: whether the group sees yours (default: not)
  DELETE /api/me/listening            all of yours: the days, the per-episode state, the finished
                                      episodes, the switch (back to off)

`tz` is the page's Date.getTimezoneOffset() (minutes, UTC minus local); it says which day is
"today". A day is {"YYYY-MM-DD": [seconds, episodes]}, only days with something in them, for the
last DAYS_BACK days. Sharing is opt-in (UK GDPR, 2026-09-30): only the people in listen_shown are
in the group, for everyone, admins too; nobody else's appears; their own answer (me) is always
theirs. The hidden-list this replaced (listen_hidden) is dropped at the first start after the
change, so everyone starts hidden and nobody is carried over as shown.

Retention: days, finished episodes and per-episode state older than RETAIN_DAYS (365) go, at the
hub's start and once a day after (prune()). Taking someone off the list (accounts.disallow) or an
admin disabling the account deletes all of theirs at once (forget()).

Recording (record(), called by web.put_position in its transaction for every position it keeps).
The page sends, with each position, `rate` (the playback speed), `playing` (whether the audio is
playing as the position is taken: false at a pause, the end, leaving the page or switching to
another episode), `tab` (a random id per page load) and `tz`. Between two updates of the same
episode by the same person, the advance of the position is credited as heard when the earlier
update was playing and came from the same tab, at most the wall time between them (the device's
own `at` clock, one tab's) times the faster of the two speeds, and at most the episode's length:
a seek or a skip forward counts only the time that passed, a jump back counts nothing, 2x counts
the audio heard. An update not newer than the last one (a retry, a late request) changes nothing,
so retries are harmless. Seconds go to the listener's local day (the update's `tz`; UTC when
the page sends none), split at midnight when an interval spans it. An episode counts once per
person (while its listen_finished row is kept), on the day its position first reaches the last
FINISH_S seconds (FINISH_FRAC of a short one) after at least FINISH_HEARD_S of it was heard
(half, for a short one): dragging to the end is not finishing.

History: nothing was recorded before this module, so the days start at its deploy. The one thing
kept from before is the Listened tick's date: at the first start, each tick then on the hub
counts as one episode (no minutes) on its UTC day (meta key BACKFILL_KEY; once)."""
from __future__ import annotations

import logging
import re
import threading
import time
from datetime import date, datetime, timedelta, timezone

from . import avatars, db
from .app import HTTPError

log = logging.getLogger("pcg.listening")

DAYS_BACK = 371                 # 53 weeks: the year's heatmap, whatever day of the week today is
RETAIN_DAYS = 365               # older than this is deleted (prune())
PRUNE_S = 86400                 # prune() again after this, while the hub runs
GROUP_DAYS = 30                 # the group's order: minutes in the last 30 days
WEEKS = 12                      # the per-week bars
FINISH_S = 30.0
FINISH_FRAC = 0.05
FINISH_HEARD_S = 60.0
RATE_MIN, RATE_MAX = 0.25, 4.0
TZ_MAX = 14 * 60
BACKFILL_KEY = "listening_ticks_backfilled"
TAB_RX = re.compile(r"^[A-Za-z0-9_-]{1,40}$")

SCHEMA = """
CREATE TABLE IF NOT EXISTS listen_days (       -- seconds heard and episodes finished, per person per local day
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  day TEXT NOT NULL,                           -- YYYY-MM-DD, the listener's own day (UTC if the page sent no offset)
  seconds REAL NOT NULL DEFAULT 0,
  episodes INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (user_id, day)
);
CREATE TABLE IF NOT EXISTS listen_last (       -- the last position update, per person per episode
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  episode_id TEXT NOT NULL,
  s REAL NOT NULL,
  at_ms INTEGER NOT NULL,                      -- the device's clock
  playing INTEGER NOT NULL,
  rate REAL NOT NULL,
  tab TEXT NOT NULL,
  heard REAL NOT NULL DEFAULT 0,               -- seconds of it credited, all told
  PRIMARY KEY (user_id, episode_id)
);
CREATE TABLE IF NOT EXISTS listen_finished (   -- an episode counted: once per person
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  episode_id TEXT NOT NULL,
  day TEXT NOT NULL,
  at TEXT NOT NULL,
  PRIMARY KEY (user_id, episode_id)
);
CREATE TABLE IF NOT EXISTS listen_shown (      -- people who turned "Show mine to the group" on
  user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
  at TEXT NOT NULL
);
DROP TABLE IF EXISTS listen_hidden;            -- the old hidden-list (shown by default): nobody carried over
CREATE INDEX IF NOT EXISTS listen_days_day ON listen_days(day);
CREATE INDEX IF NOT EXISTS listen_finished_day ON listen_finished(day);
CREATE INDEX IF NOT EXISTS listen_last_at ON listen_last(at_ms)
"""

_ready: set = set()
_ready_lock = threading.Lock()


def ensure_schema() -> None:
    """This module's tables, once per database."""
    key = str(db._path)
    if key in _ready:
        return
    with _ready_lock:
        if key in _ready:
            return
        c = db.conn()
        for stmt in SCHEMA.split(";"):          # one by one: executescript would commit a caller's transaction
            if stmt.strip():
                c.execute(stmt)
        if not c.in_transaction:
            _ready.add(key)


def start(cfg) -> None:
    ensure_schema()
    backfill()
    prune()
    _start_pruner()


# ---------------------------------------------------------------- retention and deletion

TABLES = ("listen_days", "listen_last", "listen_finished", "listen_shown")
_pruner_started = False
_pruner_lock = threading.Lock()


def prune() -> int:
    """Delete what is older than RETAIN_DAYS: days before the cutoff, episodes finished on them,
    and per-episode state last updated before it. The rows deleted."""
    ensure_schema()
    cutoff = _today(0) - timedelta(days=RETAIN_DAYS)
    cut_ms = int(datetime(cutoff.year, cutoff.month, cutoff.day, tzinfo=timezone.utc).timestamp() * 1000)
    with db.transaction() as c:
        n = c.execute("DELETE FROM listen_days WHERE day < ?", (cutoff.isoformat(),)).rowcount
        n += c.execute("DELETE FROM listen_finished WHERE day < ?", (cutoff.isoformat(),)).rowcount
        n += c.execute("DELETE FROM listen_last WHERE at_ms < ?", (cut_ms,)).rowcount
    if n:
        log.info("listening: %d rows older than %d days deleted", n, RETAIN_DAYS)
    return n


def _prune_loop() -> None:
    while True:
        time.sleep(PRUNE_S)
        try:
            prune()
        except Exception:
            log.exception("listening prune")


def _start_pruner() -> None:
    global _pruner_started
    with _pruner_lock:
        if _pruner_started:
            return
        _pruner_started = True
    threading.Thread(target=_prune_loop, name="listening-prune", daemon=True).start()


def forget(c, uid: int) -> int:
    """Every listening row of this person (in the caller's transaction): the rows deleted."""
    ensure_schema()
    return sum(c.execute(f"DELETE FROM {t} WHERE user_id = ?", (uid,)).rowcount for t in TABLES)


def backfill() -> int:
    """Each Listened tick on the hub at the first start: one episode on its UTC day. Once."""
    with db.transaction() as c:
        if c.execute("SELECT 1 FROM meta WHERE key = ?", (BACKFILL_KEY,)).fetchone():
            return 0
        rows = c.execute("SELECT user_id, substr(at, 1, 10) AS day, count(*) AS n FROM listened "
                         "WHERE at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]*' GROUP BY user_id, day").fetchall()
        for r in rows:
            _add(c, r["user_id"], r["day"], 0.0, r["n"])
        c.execute("INSERT INTO meta(key, value) VALUES (?, ?)", (BACKFILL_KEY, db.now()))
        return sum(r["n"] for r in rows)


# ---------------------------------------------------------------- recording

def _num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and v == v and abs(v) != float("inf")


def _tz(v) -> int:
    return int(v) if _num(v) and abs(v) <= TZ_MAX else 0


def _day(ms: int, tz: int) -> str:
    """The local day of a moment (ms since the epoch) at offset tz (UTC minus local, minutes)."""
    return (datetime(1970, 1, 1) + timedelta(milliseconds=ms - tz * 60_000)).strftime("%Y-%m-%d")


def _add(c, uid: int, day: str, seconds: float, episodes: int = 0) -> None:
    c.execute("""INSERT INTO listen_days(user_id, day, seconds, episodes) VALUES (?, ?, ?, ?)
                 ON CONFLICT(user_id, day) DO UPDATE SET seconds = seconds + excluded.seconds,
                                                         episodes = episodes + excluded.episodes""",
              (uid, day, seconds, episodes))


def _spread(t0: int, t1: int, credit: float, tz: int) -> dict:
    """credit seconds over the wall-clock interval [t0, t1] (ms), split at local midnights in
    proportion to the time on each side."""
    out: dict = {}
    if t1 <= t0:
        out[_day(t1, tz)] = credit
        return out
    off = tz * 60_000
    a = t0
    while a < t1:
        local = a - off
        nxt = (local // 86_400_000 + 1) * 86_400_000 + off      # the next local midnight
        b = min(t1, nxt)
        d = _day(a, tz)
        out[d] = out.get(d, 0.0) + credit * (b - a) / (t1 - t0)
        a = b
    return out


def finish_line(d: float) -> float:
    """Where an episode of d seconds counts as finished."""
    return d - min(FINISH_S, d * FINISH_FRAC)


def record(c, uid: int, eid: str, s: float, at: int, body: dict, duration_s=None) -> float:
    """One position update (web.put_position's, in its transaction): the seconds credited."""
    ensure_schema()
    s = min(float(s), 1e6)
    rate = float(body.get("rate")) if _num(body.get("rate")) else 1.0
    rate = max(RATE_MIN, min(RATE_MAX, rate))
    playing = body.get("playing") is True
    tab = body.get("tab") if isinstance(body.get("tab"), str) and TAB_RX.match(body.get("tab")) else ""
    tz = _tz(body.get("tz"))
    d = float(duration_s) if _num(duration_s) and duration_s > 0 else None
    if d is None and _num(body.get("d")) and 0 < body.get("d") < 1e6:
        d = float(body["d"])                    # the page's audio length, when the hub has none
    prev = c.execute("SELECT * FROM listen_last WHERE user_id = ? AND episode_id = ?", (uid, eid)).fetchone()
    if prev is not None and at <= prev["at_ms"]:
        return 0.0                              # a retry, or older than what is recorded: nothing
    credit = 0.0
    if prev is not None and prev["playing"] and tab and prev["tab"] == tab:
        adv = s - prev["s"]
        if adv > 0:
            credit = min(adv, (at - prev["at_ms"]) / 1000.0 * max(rate, prev["rate"]))
            if d:
                credit = min(credit, d)
    heard = (prev["heard"] if prev is not None else 0.0) + credit
    c.execute("""INSERT OR REPLACE INTO listen_last(user_id, episode_id, s, at_ms, playing, rate, tab, heard)
                 VALUES (?, ?, ?, ?, ?, ?, ?, ?)""", (uid, eid, s, at, 1 if playing else 0, rate, tab, heard))
    if credit > 0:
        for day, sec in _spread(prev["at_ms"], at, credit, tz).items():
            _add(c, uid, day, sec)
    if d and s >= finish_line(d) and heard >= min(FINISH_HEARD_S, d / 2):
        day = _day(at, tz)
        n = c.execute("INSERT OR IGNORE INTO listen_finished(user_id, episode_id, day, at) VALUES (?, ?, ?, ?)",
                      (uid, eid, day, db.now())).rowcount
        if n:
            _add(c, uid, day, 0.0, 1)
    return credit


# ---------------------------------------------------------------- reading

def _now() -> float:
    """The hub's clock (tests move it)."""
    return time.time()


def _today(tz: int) -> date:
    return date.fromisoformat(_day(int(_now() * 1000), tz))


def _rows(c, uid: int, since: str | None = None) -> dict:
    q = "SELECT day, seconds, episodes FROM listen_days WHERE user_id = ?" + (" AND day >= ?" if since else "")
    return {r["day"]: (r["seconds"], r["episodes"]) for r in c.execute(q, (uid, since) if since else (uid,))
            if r["seconds"] >= 0.5 or r["episodes"]}


def _days_view(rows: dict, since: str) -> dict:
    return {d: [int(round(v[0])), v[1]] for d, v in sorted(rows.items()) if d >= since}


def _sum(rows: dict, since: str, until: str) -> dict:
    s = e = 0
    for d, (sec, ep) in rows.items():
        if since <= d <= until:
            s += sec
            e += ep
    return {"s": int(round(s)), "episodes": e}


def streaks(days: set, today: date) -> dict:
    """Days in a row with any listening: the current run (ending today, or yesterday while today
    has none yet) and the longest."""
    longest = run = 0
    last = None
    for d in sorted(date.fromisoformat(x) for x in days):
        run = run + 1 if last is not None and d - last == timedelta(days=1) else 1
        longest = max(longest, run)
        last = d
    cur = 0
    d = today if today.isoformat() in days else today - timedelta(days=1)
    while d.isoformat() in days:
        cur += 1
        d -= timedelta(days=1)
    return {"current": cur, "longest": longest}


def shown(c, uid: int) -> bool:
    ensure_schema()
    return c.execute("SELECT 1 FROM listen_shown WHERE user_id = ?", (uid,)).fetchone() is not None


def me_view(c, uid: int, tz: int) -> dict:
    ensure_schema()
    today = _today(tz)
    t = today.isoformat()
    rows = _rows(c, uid)
    since = (today - timedelta(days=DAYS_BACK - 1)).isoformat()
    monday = today - timedelta(days=today.weekday())
    weeks = []
    for i in range(WEEKS - 1, -1, -1):
        a = monday - timedelta(weeks=i)
        weeks.append({"start": a.isoformat(), **_sum(rows, a.isoformat(), (a + timedelta(days=6)).isoformat())})
    return {
        "today": t, "since": since, "days": _days_view(rows, since),
        "totals": {"today": _sum(rows, t, t), "week": _sum(rows, monday.isoformat(), t),
                   "month": _sum(rows, today.replace(day=1).isoformat(), t), "all": _sum(rows, "0000", "9999")},
        "streak": streaks(set(rows), today), "weeks": weeks, "shown": shown(c, uid),
    }


def _members(c, cfg):
    """Everyone the group sees: turned sharing on, not disabled, and with passwords, still on
    the group's list."""
    listed = (" AND EXISTS (SELECT 1 FROM allowed_emails a WHERE a.email = lower(u.email))"
              if cfg.auth == "password" else "")
    return c.execute(f"""SELECT u.id, u.name FROM users u WHERE u.disabled = 0{listed}
                         AND EXISTS (SELECT 1 FROM listen_shown h WHERE h.user_id = u.id)
                         ORDER BY u.id""").fetchall()


def group_view(c, cfg, tz: int) -> dict:
    ensure_schema()
    today = _today(tz)
    since = (today - timedelta(days=DAYS_BACK - 1)).isoformat()
    last30 = (today - timedelta(days=GROUP_DAYS - 1)).isoformat()
    av = avatars.versions()
    people = []
    for u in _members(c, cfg):
        rows = _rows(c, u["id"], since)
        people.append({"user": {"id": u["id"], "name": u["name"], "avatar": av.get(u["id"])},
                       "days": _days_view(rows, since), "last30_s": _sum(rows, last30, today.isoformat())["s"]})
    people.sort(key=lambda p: (-p["last30_s"], (p["user"]["name"] or "").lower(), p["user"]["id"]))
    return {"today": today.isoformat(), "since": since, "people": people}


# ---------------------------------------------------------------- routes

def _tz_arg(req) -> int:
    try:
        return _tz(int(req.arg("tz") or 0))
    except ValueError:
        return 0


def get_me(req):
    req.send_json(200, me_view(db.conn(), req.user["id"], _tz_arg(req)))


def get_group(req):
    req.send_json(200, group_view(db.conn(), req.cfg, _tz_arg(req)))


def put_visibility(req):
    from . import web
    web._mutation(req)
    v = req.json().get("shown")
    if not isinstance(v, bool):
        raise HTTPError(400, "bad_shown", "shown must be true or false")
    ensure_schema()
    uid = req.user["id"]
    with db.transaction() as c:
        if v:
            c.execute("INSERT OR IGNORE INTO listen_shown(user_id, at) VALUES (?, ?)", (uid, db.now()))
        else:
            c.execute("DELETE FROM listen_shown WHERE user_id = ?", (uid,))
    req.send_json(200, {"shown": shown(db.conn(), uid)})


def delete_mine(req):
    """Delete my listening history: all of the caller's rows. Twice is the same."""
    from . import web
    web._mutation(req)
    with db.transaction() as c:
        forget(c, req.user["id"])
    req.send_json(200, {"deleted": True})


ROUTES = [
    ("GET", r"^/api/listening/me$", get_me, "viewer"),
    ("GET", r"^/api/listening/group$", get_group, "viewer"),
    ("PUT", r"^/api/me/listening-visibility$", put_visibility, "viewer"),
    ("DELETE", r"^/api/me/listening$", delete_mine, "viewer"),
]
