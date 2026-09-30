#!/usr/bin/env python3
"""Listening time (hub/listening.py) and the Listening page.

    python3 -m unittest hub.tests.test_listening -v        (from stacks/papercast-group)

The hub: the seconds credited from the player's position updates (normal play, 2x, a seek or a
skip forward counting only the time that passed, a jump back nothing, a pause, another tab, an
old page without the fields, stale and repeated updates, midnight in UTC and in the listener's
own time zone, the episode's length as a cap), an episode counted once when finished and not
when dragged to the end; GET /api/listening/me (totals, streaks, weeks) and /group (order, the
hide switch, admins seeing no more); the Listened ticks' backfill, once.
The page, in a real headless Chrome: your heatmap and the group's with pictures and names, the
tooltip, the weeks, in a light and a dark theme, on a phone (the year scrolls inside its box, the
page never sideways, 44 x 44 px taps), the switch in Settings; no console errors.

Fake people and made-up listening only. The browser part skips without a headless Chrome or
`websocket-client`."""
from __future__ import annotations

import json
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from web_rig import Rig  # noqa: E402

from hub import db, listening  # noqa: E402

A, B, C, D = "alice@example.org", "bob@example.org", "carol@example.org", "dan@example.org"
# a fixed "now" for the views: 2026-09-30 (a Wednesday), noon UTC
NOW = datetime(2026, 9, 30, 12, 0, 0, tzinfo=timezone.utc)
NOW_MS = int(NOW.timestamp() * 1000)


def ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.r = Rig()
        r = cls.r
        cls.alice = r.user(A, "Alice", "admin")
        cls.bob = r.user(B, "Bob", "contributor")
        cls.carol = r.user(C, "Carol", "viewer")
        cls.p = r.paper("A Fake Paper About Listening", cls.alice)
        cls.e = r.episode(cls.p, cls.alice, duration=1800.0)
        cls.p2 = r.paper("A Short Fake Paper", cls.bob)
        cls.short = r.episode(cls.p2, cls.bob, duration=600.0)
        cls._now = listening._now
        listening._now = lambda: NOW.timestamp()

    @classmethod
    def tearDownClass(cls):
        listening._now = cls._now
        cls.r.close()

    def setUp(self):
        for t in ("listen_days", "listen_last", "listen_finished", "listen_hidden", "positions"):
            self.r.q(f"DELETE FROM {t}")

    def put(self, s, at, eid=None, user=A, playing=True, rate=1, tab="tab1", tz=0, **more):
        body = {"s": s, "at": at, "playing": playing, "rate": rate, "tab": tab, "tz": tz, **more}
        body = {k: v for k, v in body.items() if v is not None}
        code, j, _ = self.r.req("PUT", f"/api/episodes/{eid or self.e}/position", body, user=user)
        self.assertEqual(code, 200, j)
        return j

    def play(self, s0, t0, seconds, step=10, rate=1, **kw):
        """Continuous play from position s0 at t0 (ms): an update every `step` s of wall time."""
        t = 0
        while t <= seconds:
            self.put(s0 + t * rate, t0 + t * 1000, rate=rate, **kw)
            t += step

    def heard(self, uid=None, day=None):
        uid = uid or self.alice
        if day:
            r = self.r.q("SELECT seconds, episodes FROM listen_days WHERE user_id = ? AND day = ?", uid, day)
            return (round(r[0][0], 3), r[0][1]) if r else (0, 0)
        r = self.r.q("SELECT coalesce(sum(seconds), 0), coalesce(sum(episodes), 0) FROM listen_days WHERE user_id = ?", uid)
        return round(r[0][0], 3), r[0][1]


class Crediting(Base):
    T0 = NOW_MS - 3 * 3600_000          # 09:00 UTC on the fixed day

    def test_normal_play_and_a_pause(self):
        self.play(0, self.T0, 60)                                   # 60 s heard, 7 updates
        self.assertEqual(self.heard(), (60, 0))
        self.put(65, self.T0 + 65_000, playing=False)               # the pause: its 5 s count
        self.assertEqual(self.heard(), (65, 0))
        # paused an hour, then played again: the hour counts nothing, the play after it does
        self.put(65, self.T0 + 3665_000, playing=True)
        self.assertEqual(self.heard(), (65, 0))
        self.put(75, self.T0 + 3675_000)
        self.assertEqual(self.heard(), (75, 0))
        self.assertEqual(self.heard(day="2026-09-30"), (75, 0))

    def test_twice_the_speed_counts_the_audio_heard(self):
        self.play(0, self.T0, 60, rate=2)                           # 120 s of audio in 60 s
        self.assertEqual(self.heard(), (120, 0))

    def test_the_speed_caps_what_counts(self):
        # a page saying 1x whose position moves 2x (a skip each time): only the wall time counts
        for i in range(7):
            self.put(i * 20, self.T0 + i * 10_000, rate=1)
        self.assertEqual(self.heard(), (60, 0))
        # an absurd speed is held to 4x
        self.setUp()
        self.put(0, self.T0, rate=100)
        self.put(1000, self.T0 + 10_000, rate=100)
        self.assertEqual(self.heard(), (40, 0))

    def test_a_seek_forward_counts_only_the_time_that_passed(self):
        self.put(100, self.T0)
        self.put(900, self.T0 + 3_000)                              # 3 s, then a seek to 900
        self.assertEqual(self.heard(), (3, 0))
        self.put(910, self.T0 + 13_000)
        self.assertEqual(self.heard(), (13, 0))

    def test_a_skip_while_playing(self):
        self.put(100, self.T0)
        self.put(135, self.T0 + 5_000)                              # 5 s heard, then +30
        self.assertEqual(self.heard(), (5, 0))

    def test_a_jump_back_counts_nothing(self):
        self.put(500, self.T0)
        self.put(50, self.T0 + 4_000)
        self.assertEqual(self.heard(), (0, 0))
        self.put(60, self.T0 + 14_000)                              # and playing on from there counts
        self.assertEqual(self.heard(), (10, 0))

    def test_a_seek_while_paused_counts_nothing(self):
        self.put(100, self.T0, playing=False)
        self.put(1500, self.T0 + 3600_000, playing=False)
        self.assertEqual(self.heard(), (0, 0))

    def test_stale_and_repeated_updates(self):
        self.put(0, self.T0)
        j = self.put(10, self.T0 + 10_000)
        self.assertFalse(j["kept_newer"])
        for _ in range(3):                                          # a retry of the same request
            self.put(10, self.T0 + 10_000)
        self.assertEqual(self.heard(), (10, 0))
        # a late one, older than what is recorded: the position and the time both stay
        j = self.put(5, self.T0 + 5_000)
        self.assertTrue(j["kept_newer"])
        self.assertEqual(self.heard(), (10, 0))
        self.put(20, self.T0 + 20_000)
        self.assertEqual(self.heard(), (20, 0))
        self.assertEqual(self.r.q("SELECT seconds FROM positions")[0][0], 20)

    def test_another_tab_or_an_old_page_counts_nothing(self):
        self.put(100, self.T0, tab="laptop")
        self.put(900, self.T0 + 7200_000, tab="phone")              # the phone, two hours on
        self.assertEqual(self.heard(), (0, 0))
        self.put(910, self.T0 + 7210_000, tab="phone")
        self.assertEqual(self.heard(), (10, 0))
        # a page without the new fields (before this module): positions still kept, no time
        self.setUp()
        for i in range(4):
            code, j, _ = self.r.req("PUT", f"/api/episodes/{self.e}/position", {"s": i * 10, "at": self.T0 + i * 10_000})
            self.assertEqual(code, 200, j)
        self.assertEqual(self.heard(), (0, 0))
        self.assertEqual(self.r.q("SELECT seconds FROM positions")[0][0], 30)

    def test_each_person_their_own(self):
        self.play(0, self.T0, 30)
        self.play(0, self.T0, 20, user=B, tab="b")
        self.assertEqual(self.heard(self.alice), (30, 0))
        self.assertEqual(self.heard(self.bob), (20, 0))

    def test_midnight_utc(self):
        t = ms(datetime(2026, 9, 29, 23, 59, 55, tzinfo=timezone.utc))
        self.put(0, t)
        self.put(10, t + 10_000)
        self.assertEqual(self.heard(day="2026-09-29"), (5, 0))
        self.assertEqual(self.heard(day="2026-09-30"), (5, 0))

    def test_midnight_in_the_listeners_time_zone(self):
        # British Summer Time (the page's getTimezoneOffset() is -60): local midnight is 23:00 UTC
        t = ms(datetime(2026, 9, 29, 22, 59, 58, tzinfo=timezone.utc))
        self.put(0, t, tz=-60)
        self.put(10, t + 10_000, tz=-60)
        self.assertEqual(self.heard(day="2026-09-29"), (2, 0))
        self.assertEqual(self.heard(day="2026-09-30"), (8, 0))
        # an absurd offset is UTC
        self.setUp()
        self.put(0, t, tz=5000)
        self.put(10, t + 10_000, tz=5000)
        self.assertEqual(self.heard(day="2026-09-29"), (10, 0))

    def test_a_long_gap_while_playing(self):
        # a phone asleep: no update for 20 minutes while it played on, then one
        self.put(0, self.T0)
        self.put(1200, self.T0 + 1200_000)
        self.assertEqual(self.heard(), (1200, 0))

    def test_never_more_than_the_episode(self):
        self.put(0, self.T0, eid=self.short, rate=4)
        self.put(10_000, self.T0 + 3 * 3600_000, eid=self.short, rate=4)
        self.assertEqual(self.heard(), (600, 1))

    def test_finishing_an_episode_counts_it_once(self):
        self.play(0, self.T0, 600, eid=self.short)                  # the whole 600 s
        self.put(600, self.T0 + 601_000, eid=self.short, playing=False)
        self.assertEqual(self.heard(), (600, 1))
        # played again to the end the next day: still one
        t = self.T0 + 86400_000
        self.put(0, t, eid=self.short)
        self.play(560, t + 1_000, 40, eid=self.short)
        self.assertEqual(self.heard()[1], 1)
        self.assertEqual(len(self.r.q("SELECT * FROM listen_finished")), 1)

    def test_dragged_to_the_end_is_not_finished(self):
        self.put(0, self.T0, eid=self.short)
        self.put(20, self.T0 + 20_000, eid=self.short)
        self.put(599, self.T0 + 21_000, eid=self.short)             # 21 s heard, then to the end
        self.assertEqual(self.heard(), (21, 0))
        self.assertEqual(self.r.q("SELECT count(*) FROM listen_finished")[0][0], 0)

    def test_finished_on_the_listeners_day(self):
        t = ms(datetime(2026, 9, 29, 23, 30, 0, tzinfo=timezone.utc))      # 00:30 on the 30th in BST
        self.play(0, t - 600_000, 600, eid=self.short, tz=-60)
        self.assertEqual(self.heard(day="2026-09-30")[1], 1)


class Views(Base):
    def days(self, uid, rows):
        for day, sec, ep in rows:
            self.r.q("INSERT INTO listen_days(user_id, day, seconds, episodes) VALUES (?, ?, ?, ?)", uid, day, sec, ep)

    def me(self, user=A, tz=0):
        code, j, _ = self.r.req("GET", f"/api/listening/me?tz={tz}", user=user)
        self.assertEqual(code, 200, j)
        return j

    def group(self, user=A):
        code, j, _ = self.r.req("GET", "/api/listening/group?tz=0", user=user)
        self.assertEqual(code, 200, j)
        return j

    def test_empty(self):
        j = self.me()
        self.assertEqual(j["today"], "2026-09-30")
        self.assertEqual(j["since"], "2025-09-25")                  # 371 days back
        self.assertEqual(j["days"], {})
        self.assertEqual(j["totals"]["all"], {"s": 0, "episodes": 0})
        self.assertEqual(j["streak"], {"current": 0, "longest": 0})
        self.assertEqual(len(j["weeks"]), 12)
        self.assertEqual(j["weeks"][-1], {"start": "2026-09-28", "s": 0, "episodes": 0})   # this week's Monday
        self.assertTrue(j["shown"])

    def test_totals_streaks_weeks(self):
        self.days(self.alice, [
            ("2026-09-30", 600, 1), ("2026-09-29", 1200, 0), ("2026-09-28", 60, 0),     # this week, a 3-day run
            ("2026-09-26", 300, 1),                                                     # last week
            ("2026-09-01", 900, 2), ("2026-09-02", 900, 0), ("2026-09-03", 900, 0), ("2026-09-04", 900, 0),   # a 4-day run
            ("2026-08-15", 1800, 1),
            ("2024-01-01", 3600, 1),                                                    # over a year ago: all time only
        ])
        j = self.me()
        t = j["totals"]
        self.assertEqual(t["today"], {"s": 600, "episodes": 1})
        self.assertEqual(t["week"], {"s": 1860, "episodes": 1})
        self.assertEqual(t["month"], {"s": 600 + 1200 + 60 + 300 + 3600, "episodes": 4})
        self.assertEqual(t["all"], {"s": 600 + 1200 + 60 + 300 + 3600 + 1800 + 3600, "episodes": 6})
        self.assertEqual(j["streak"], {"current": 3, "longest": 4})
        self.assertNotIn("2024-01-01", j["days"])
        self.assertEqual(j["days"]["2026-09-29"], [1200, 0])
        self.assertEqual(j["weeks"][-1]["s"], 1860)
        self.assertEqual(j["weeks"][-2], {"start": "2026-09-21", "s": 300, "episodes": 1})
        # yesterday's run is still the current one while today has nothing yet
        self.r.q("DELETE FROM listen_days WHERE day = '2026-09-30'")
        self.assertEqual(self.me()["streak"], {"current": 2, "longest": 4})
        # the viewer's own day: at noon UTC it is already the 1st of October at UTC+13
        self.assertEqual(self.me(tz=-780)["today"], "2026-10-01")
        self.assertEqual(self.me(tz=-780)["totals"]["today"], {"s": 0, "episodes": 0})

    def test_the_group_in_order_and_the_hide_switch(self):
        dan = self.r.user(D, "Dan", "viewer", disabled=True)
        self.days(self.alice, [("2026-09-30", 600, 1)])
        self.days(self.bob, [("2026-09-20", 3000, 2), ("2026-08-01", 9000, 0)])       # the second, 60 days ago: not in the order
        self.days(dan, [("2026-09-30", 9999, 5)])
        g = self.group()
        self.assertEqual([p["user"]["name"] for p in g["people"]], ["Bob", "Alice", "Carol"])      # disabled Dan: not a member
        self.assertEqual([p["last30_s"] for p in g["people"]], [3000, 600, 0])
        self.assertEqual(g["people"][0]["days"], {"2026-09-20": [3000, 2], "2026-08-01": [9000, 0]})
        self.assertIn("avatar", g["people"][0]["user"])
        self.assertEqual(set(g["people"][0]["user"]), {"id", "name", "avatar"})       # no emails
        # Bob hides his: gone from the group for everyone, the admin too; his own stays his
        code, j, _ = self.r.req("PUT", "/api/me/listening-visibility", {"shown": False}, user=B)
        self.assertEqual((code, j), (200, {"shown": False}))
        for who in (A, B, C):
            self.assertEqual([p["user"]["name"] for p in self.group(who)["people"]], ["Alice", "Carol"], who)
        mine = self.me(B)
        self.assertFalse(mine["shown"])
        self.assertEqual(mine["days"]["2026-09-20"], [3000, 2])
        # twice is the same; back on
        self.assertEqual(self.r.req("PUT", "/api/me/listening-visibility", {"shown": False}, user=B)[1], {"shown": False})
        self.assertEqual(self.r.req("PUT", "/api/me/listening-visibility", {"shown": True}, user=B)[1], {"shown": True})
        self.assertEqual([p["user"]["name"] for p in self.group(C)["people"]], ["Bob", "Alice", "Carol"])
        self.r.q("DELETE FROM users WHERE id = ?", dan)

    def test_the_switch_refuses_junk_and_other_sites(self):
        code, j, _ = self.r.req("PUT", "/api/me/listening-visibility", {"shown": "no"})
        self.assertEqual((code, j["error"]), (400, "bad_shown"))
        code, j, _ = self.r.req("PUT", "/api/me/listening-visibility", {"shown": False}, headers={"X-PCG": None})
        self.assertEqual(code, 403)
        code, j, _ = self.r.req("PUT", "/api/me/listening-visibility", {"shown": False}, headers={"Sec-Fetch-Site": "cross-site"})
        self.assertEqual(code, 403)
        self.assertTrue(self.me()["shown"])
        code, _, _ = self.r.req("GET", "/api/listening/me", user=None)
        self.assertEqual(code, 401)

    def test_junk_tz_is_utc(self):
        for tz in ("abc", "99999", ""):
            code, j, _ = self.r.req("GET", f"/api/listening/me?tz={tz}")
            self.assertEqual((code, j["today"]), (200, "2026-09-30"), tz)

    def test_backfill_of_ticks_once(self):
        self.r.q("DELETE FROM meta WHERE key = ?", listening.BACKFILL_KEY)
        self.r.q("INSERT INTO listened(user_id, paper_id, at) VALUES (?, ?, '2026-09-10T08:00:00Z')", self.alice, self.p)
        self.r.q("INSERT INTO listened(user_id, paper_id, at) VALUES (?, ?, '2026-09-10T21:00:00Z')", self.alice, self.p2)
        self.r.q("INSERT INTO listened(user_id, paper_id, at) VALUES (?, ?, '2026-09-12T08:00:00Z')", self.bob, self.p)
        try:
            self.assertEqual(listening.backfill(), 3)
            self.assertEqual(listening.backfill(), 0)
            self.assertEqual(self.heard(self.alice, "2026-09-10"), (0, 2))
            self.assertEqual(self.heard(self.bob, "2026-09-12"), (0, 1))
            self.assertEqual(self.me()["days"]["2026-09-10"], [0, 2])
        finally:
            self.r.q("DELETE FROM listened")


# ---------------------------------------------------------------- the page

import test_page as tp  # noqa: E402


def fake_days(seed: int, months: int = 4, today=None) -> list:
    """Made-up listening: (day, seconds, episodes) over the last `months` months, some days empty."""
    import random
    rnd = random.Random(seed)
    today = today or listening._today(0)
    out = []
    for i in range(months * 30, -1, -1):
        d = today - timedelta(days=i)
        if rnd.random() < 0.45:
            continue
        m = rnd.choice([3, 8, 15, 22, 30, 41, 55, 70, 95])
        out.append((d.isoformat(), m * 60 + rnd.randint(0, 59), rnd.choice([0, 0, 1, 1, 2]) if m > 15 else 0))
    return out


@unittest.skipIf(tp.SKIP, tp.SKIP or "")
class ListeningPage(tp.PageBase):
    @classmethod
    def fill(cls):
        super().fill()
        r = cls.r
        cls.dan = r.user(D, "Dan", "viewer")
        cls.p = r.paper("A Fake Paper To Listen To", cls.alice)
        cls.e = r.episode(cls.p, cls.alice, duration=30, audio_s=30)
        cls.today = listening._today(0)
        for uid, seed, months in ((cls.alice, 1, 4), (cls.bob, 2, 6), (cls.dan, 3, 2)):
            for day, sec, ep in fake_days(seed, months, cls.today):
                r.q("INSERT INTO listen_days(user_id, day, seconds, episodes) VALUES (?, ?, ?, ?)", uid, day, sec, ep)
        # today, for the tooltip: 42 minutes, 2 episodes
        r.q("INSERT OR REPLACE INTO listen_days(user_id, day, seconds, episodes) VALUES (?, ?, 2520, 2)", cls.alice, cls.today.isoformat())
        r.q("INSERT INTO listen_hidden(user_id, at) VALUES (?, ?)", cls.dan, db.now())
        # Bob listened most in the last 30 days, then Alice; Carol not at all
        r.q("UPDATE listen_days SET seconds = seconds + 7200 WHERE user_id = ? AND day >= ?", cls.bob,
            (cls.today - timedelta(days=29)).isoformat())

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.b.call("Emulation.setTimezoneOverride", timezoneId="UTC")

    def tearDown(self):
        self.b.js("localStorage.removeItem('pcg-theme')")
        super().tearDown()

    def page(self, theme=None, user=A):
        self.as_user(user)
        self.b.goto(self.base + "/")
        self.b.js(f"localStorage.setItem('pcg-theme', {json.dumps(theme)})" if theme else "localStorage.removeItem('pcg-theme')")
        self.load("listening")
        self.b.wait_js("!document.getElementById('listening').hidden && document.querySelectorAll('#ls-year rect').length > 0"
                       " && !!document.getElementById('ls-group')", 10, "the Listening page")

    def colours(self):
        return self.b.js("""(() => { const cs = (s) => { const e = document.querySelector(s); return e ? getComputedStyle(e).fill : null; };
          return { lv0: cs('#ls-year rect.lv0'), lv4: cs('#ls-year rect.lv4'), bg: getComputedStyle(document.body).backgroundColor,
                   accent: getComputedStyle(document.documentElement).getPropertyValue('--accent').trim() }; })()""")

    def test_1_your_year_and_the_group(self):
        b = self.b
        self.page()
        dow = self.today.weekday()
        self.assertEqual(b.js("document.querySelectorAll('#ls-year rect').length"), 52 * 7 + dow + 1)
        self.assertEqual(b.js("[...document.querySelectorAll('#ls-year rect')].at(-1).dataset.d"), self.today.isoformat())
        self.assertEqual(b.js("document.querySelector('#ls-year svg').getAttribute('aria-label')"), "Your listening, the last 52 weeks")
        months = b.js("[...document.querySelectorAll('#ls-year text')].map(t => t.textContent)")
        self.assertEqual(months[-3:], ["Mon", "Wed", "Fri"])
        self.assertGreaterEqual(len(months) - 3, 11)
        # every shade is drawn, and each is a different colour
        fills = b.js("[0,1,2,3,4].map(i => { const e = document.querySelector('#ls-year rect.lv' + i); return e && getComputedStyle(e).fill; })")
        self.assertEqual(len(set(fills)), 5, fills)
        # today's cell: 42 minutes, 2 episodes, the darkest but one... (42 min: shade 3)
        cell = f"document.querySelector('#ls-year rect[data-d=\"{self.today.isoformat()}\"]')"
        self.assertEqual(b.js(f"{cell}.getAttribute('class')"), "lv3")
        b.js(f"{cell}.dispatchEvent(new MouseEvent('click', {{bubbles: true}}))")
        tip = b.wait_js("(t => t && !t.hidden && t.textContent)(document.querySelector('.hm-tip'))", 3, "the tooltip")
        self.assertIn("42 min · 2 episodes", tip)
        self.assertIn(str(self.today.year), tip)
        b.js("document.getElementById('ls-body').dispatchEvent(new PointerEvent('pointerdown', {bubbles: true}))")
        self.assertTrue(b.js("document.querySelector('.hm-tip').hidden"))
        # the totals
        self.assertEqual(self.text("#ls-today .ls-v"), "42 min")
        self.assertEqual(self.text("#ls-today .ls-s"), "2 episodes")
        self.assertTrue(self.text("#ls-streak .ls-v").endswith("day") or self.text("#ls-streak .ls-v").endswith("days"))
        self.assertEqual(b.js("document.querySelectorAll('#ls-weeks rect.wk-base').length"), 12)
        # the group: most in the last 30 days first, hidden Dan left out, each with a picture and a year
        self.assertEqual(b.js("[...document.querySelectorAll('#ls-group .gp-name')].map(x => x.textContent)"), ["Bob", "Alice", "Carol"])
        self.assertEqual(b.js("document.querySelectorAll('#ls-group .gp-row .av').length"), 3)
        self.assertEqual(b.js("document.querySelectorAll('#ls-group .gp-row')[2].querySelectorAll('rect:not(.lv0)').length"), 0)
        self.assertEqual(b.js("document.querySelectorAll('#ls-group .gp-row')[0].querySelectorAll('rect').length"), 52 * 7 + dow + 1)
        self.assertEqual(self.text("#ls-group .gp-row:nth-child(3) .gp-n"), "0 min")
        # the header's button says where you are; again goes back to the list
        self.assertEqual(b.js("document.getElementById('listen-btn').getAttribute('aria-current')"), "page")
        self.assertEqual(b.js("document.title"), "Listening · Papers")
        b.js("document.getElementById('listen-btn').click()")
        b.wait_js("document.getElementById('listening').hidden && !location.hash", 3, "back to the list")
        self.shot("listening-light")

    def test_2_a_dark_theme(self):
        for theme in ("dark", "forest", "paper"):
            self.page(theme)
            c = self.colours()
            self.assertNotEqual(c["lv0"], c["bg"], theme)
            self.assertNotEqual(c["lv0"], c["lv4"], theme)
            self.assertTrue(self.b.js("document.documentElement.dataset.theme") == theme)
            self.shot(f"listening-{theme}")
        # the accent in full is the darkest shade in a light theme and the brightest in a dark one
        lum = ("(c => { const k = c.startsWith('color(') ? 255 : 1, m = c.match(/[\\d.]+/g).map(Number);"
               " return k * (0.2126 * m[0] + 0.7152 * m[1] + 0.0722 * m[2]); })")
        self.page("dark")
        dark = self.b.js(f"[{lum}(getComputedStyle(document.querySelector('#ls-year rect.lv0')).fill), {lum}(getComputedStyle(document.querySelector('#ls-year rect.lv4')).fill)]")
        self.assertLess(dark[0], dark[1])
        self.page("light")
        light = self.b.js(f"[{lum}(getComputedStyle(document.querySelector('#ls-year rect.lv0')).fill), {lum}(getComputedStyle(document.querySelector('#ls-year rect.lv4')).fill)]")
        self.assertGreater(light[0], light[1])

    def test_3_on_a_phone(self):
        b = self.b
        self.phone()
        try:
            self.page()
            # the year scrolls inside its box, showing the recent weeks; the page never sideways
            box = b.js("(x => [x.scrollWidth, x.clientWidth, x.scrollLeft])(document.getElementById('ls-year'))")
            self.assertGreater(box[0], box[1])
            self.assertGreater(box[2], 0)
            self.no_side_scroll("Listening")
            self.assertLessEqual(b.js("document.getElementById('listening').scrollWidth - document.getElementById('win').clientWidth"), 0)
            # the group's: the last 26 weeks, the width of the column
            self.assertEqual(b.js("document.querySelectorAll('#ls-group .gp-row')[0].querySelectorAll('rect').length"), 25 * 7 + self.today.weekday() + 1)
            self.assertTargets("Listening")
            self.shot("listening-phone")
            b.js("document.getElementById('ls-back').click()")
            b.wait_js("document.getElementById('listening').hidden", 3, "back")
        finally:
            b.viewport(1440, 900)

    def test_4_the_switch(self):
        b = self.b
        self.as_user(C)
        self.load("settings=prefs")
        b.wait_js("(x => x && !x.disabled)(document.getElementById('ls-shown'))", 10, "the switch")
        self.assertEqual(b.js("document.getElementById('ls-shown').getAttribute('aria-checked')"), "true")
        b.js("document.getElementById('ls-shown').click()")
        b.wait_js("document.getElementById('ls-shown-msg').textContent === 'Saved'", 5, "saved")
        self.assertEqual(b.js("document.getElementById('ls-shown').getAttribute('aria-checked')"), "false")
        self.assertEqual(len(self.r.q("SELECT 1 FROM listen_hidden WHERE user_id = ?", self.carol)), 1)
        self.page(user=C)
        self.assertEqual(b.js("[...document.querySelectorAll('#ls-group .gp-name')].map(x => x.textContent)"), ["Bob", "Alice"])
        self.assertTrue(b.js("document.querySelectorAll('#ls-year rect').length > 0"))     # her own is still hers
        self.phone()
        try:
            self.load("settings=prefs")
            b.wait_js("(x => x && !x.disabled)(document.getElementById('ls-shown'))", 10, "the switch")
            self.assertTargets("Settings with the switch")
            b.js("document.getElementById('ls-shown').click()")
            b.wait_js("document.getElementById('ls-shown-msg').textContent === 'Saved'", 5, "saved")
        finally:
            b.viewport(1440, 900)
        self.assertEqual(len(self.r.q("SELECT 1 FROM listen_hidden WHERE user_id = ?", self.carol)), 0)

    def test_5_playing_counts_the_time_heard(self):
        b = self.b
        self.as_user(B)
        self.r.q("DELETE FROM listen_last")
        before = self.r.q("SELECT coalesce(sum(seconds), 0) FROM listen_days WHERE user_id = ?", self.bob)[0][0]
        self.load(f"p={self.p}")
        audio = "document.getElementById('audio')"
        b.wait_js(f"!document.getElementById('bar').hidden && {audio}.readyState >= 1", 10, "the player")
        b.js("document.getElementById('p-speed').click()")          # 1.25x
        b.js(f"{audio}.muted = true; document.getElementById('p-play').click()")
        b.wait_js(f"{audio}.currentTime > 4", 10, "playing")
        b.js("document.getElementById('p-play').click()")
        at = b.wait_js(f"{audio}.paused && {audio}.currentTime", 5, "paused")
        row = self.r.wait(lambda: (lambda x: x and not x[0]["playing"] and x[0])(self.r.q("SELECT * FROM listen_last WHERE user_id = ?", self.bob)),
                          5, "the pause reached the hub")
        self.assertEqual(row["rate"], 1.25)
        self.assertTrue(row["tab"])
        got = self.r.q("SELECT coalesce(sum(seconds), 0) FROM listen_days WHERE user_id = ?", self.bob)[0][0] - before
        self.assertAlmostEqual(got, at, delta=0.8)
        b.js("localStorage.setItem('pcg.speed', '1')")


import test_accounts as ta  # noqa: E402

_PW = ta.PasswordPagesTest


@unittest.skipIf(ta.NO_BROWSER or not ta.OPENSSL, ta.NO_BROWSER or "openssl is not installed")
class ListeningWithPasswords(unittest.TestCase):
    """PCG_AUTH=password, as on perov (test_accounts.py's hub and browser): the switch is in
    Settings, Account; the group is the people on the group's list."""
    setUpClass = classmethod(_PW.setUpClass.__func__)
    tearDownClass = classmethod(_PW.tearDownClass.__func__)
    setUp, tearDown = _PW.setUp, _PW.tearDown
    js, wait, typ, click, taps, sign_in, new_person, signed_in_as_leo = (
        _PW.js, _PW.wait, _PW.typ, _PW.click, _PW.taps, _PW.sign_in, _PW.new_person, _PW.signed_in_as_leo)

    def test_the_switch_in_account_and_the_listed_group(self):
        pat = self.new_person("pl701", pw="pat's own long password")
        gone = self.new_person("pl702")
        ta.accounts.disallow(self.hub.cfg, ta.accounts.account(gone)["email"])
        self.signed_in_as_leo("/#listening")
        self.wait("!!document.getElementById('ls-group')", "the Listening page")
        names = self.js("[...document.querySelectorAll('#ls-group .gp-row')].map(x => Number(x.dataset.uid))")
        self.assertEqual(sorted(names), sorted([self.leo_id, pat]))            # the removed person is not in it
        self.js("location.hash = 'settings=prefs'")
        self.wait("!!document.getElementById('pref-save')", "Preferences")
        self.assertIsNone(self.js("document.getElementById('ls-shown')"))       # with passwords it is in Account
        self.js("location.hash = 'settings=account'")
        self.wait("(x => x && !x.disabled)(document.getElementById('ls-shown'))", "the switch")
        self.assertEqual(self.js("document.getElementById('ls-shown').getAttribute('aria-checked')"), "true")
        if tp.SHOTS:
            self.js("document.getElementById('ls-shown').scrollIntoView({block: 'center'})")
            Path(tp.SHOTS).mkdir(parents=True, exist_ok=True)
            self.b.screenshot(Path(tp.SHOTS) / "listening-switch-account.png")
        self.click("#ls-shown")
        self.wait("document.getElementById('ls-shown-msg').textContent === 'Saved'", "saved")
        self.assertEqual(self.js("document.getElementById('ls-shown').getAttribute('aria-checked')"), "false")
        self.assertEqual(self.hub.req("GET", "/api/listening/group")[0], 401)
        self.js("location.hash = 'listening'")
        self.wait(f"JSON.stringify([...document.querySelectorAll('#ls-group .gp-row')].map(x => Number(x.dataset.uid))) === '[{pat}]'",
                  "the Listening page again, without Leo in the group")
        self.assertTrue(self.js("document.querySelectorAll('#ls-year rect').length > 300"))      # his own is still there
        self.b.viewport(390, 844, mobile=True)
        self.js("location.hash = 'settings=account'")
        self.wait("(x => x && !x.disabled)(document.getElementById('ls-shown'))", "the switch on a phone")
        self.taps("Account with the switch")
        self.click("#ls-shown")
        self.wait("document.getElementById('ls-shown').getAttribute('aria-checked') === 'true' && document.getElementById('ls-shown-msg').textContent === 'Saved'", "shown again")


del _PW         # else unittest would run test_accounts' own tests from this module too


if __name__ == "__main__":
    unittest.main()
