#!/usr/bin/env python3
"""The voice parts of the page (hub/static/app.js "voices") in a real headless Chrome: Settings >
Voice (every voice listed with its sample, disabled until tools/make_voice_samples.py made it;
a sample plays; choosing "My voice"), the window's "Voice: … Change" (its maker only), and the
swap when a version's new audio lands while it is loaded (same sentence, new revision in the URL).

    python3 -m unittest discover -s stacks/papercast-group/hub/tests -p 'test_voice_page.py' -v

Skips when no headless Chrome or no `websocket-client` is available (as test_page.py)."""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from test_page import SKIP, PageBase, B, C  # noqa: E402
from web_rig import publish, silent_mp3  # noqa: E402

from hub import db, voices  # noqa: E402

ROWS = "[...document.querySelectorAll('#voice-list .vrow')]"
CHECKED = f"{ROWS}.filter(r => r.querySelector('.ver').getAttribute('aria-checked') === 'true').map(r => r.dataset.voice)"


def timings(n, per, lead=0.46):
    return {"version": 1, "segments": [{"start": round(lead + i * per, 3), "end": round(lead + (i + 1) * per - 0.3, 3),
                                         "text": f"Sentence {i}."} for i in range(n)]}


@unittest.skipIf(SKIP, SKIP or "")
class VoicePage(PageBase):
    @classmethod
    def fill(cls):
        super().fill()
        r = cls.r
        cls.paper = r.paper("A Fake Paper With A Voice", cls.bob, tags=["voices"])
        cls.ep = r.episode(cls.paper, cls.bob, summary="practical", duration=30, audio_s=30)
        voices.ensure_schema()
        db.conn().execute("INSERT INTO episode_voice (episode_id, voice, rev, updated_at) VALUES (?, 'clear-female', 1, ?)",
                          (cls.ep, db.now()))
        d = r.cfg.episodes / cls.ep
        (d / "timings.json").write_text(json.dumps(timings(6, 5.0)))
        (d / "script.md").write_text("# A heading\n\nSome sentences.\n")

    def samples(self, on: bool):
        d = self.r.cfg.data / "voices"
        d.mkdir(exist_ok=True)
        for p in voices.PRESETS:
            f = d / f"{p['id']}.mp3"
            if on:
                f.write_bytes(silent_mp3(3.0))
            else:
                f.unlink(missing_ok=True)

    def settings_voice(self, user):
        self.home(user)
        self.b.js("location.hash = 'settings=voice'")
        self.b.wait_js(f"{ROWS}.length > 0", 5, "the voice list")

    def test_settings_voice_without_samples(self):
        """Every voice listed; with no sample made yet each says so and nothing can be played."""
        b = self.b
        self.samples(False)
        self.settings_voice(B)
        self.assertEqual(b.js(f"{ROWS}.map(r => r.dataset.voice)"), [p["id"] for p in voices.PRESETS])
        self.assertEqual(b.js(f"{ROWS}.map(r => r.querySelector('.v-who').textContent)"), [p["name"] for p in voices.PRESETS])
        self.assertEqual(b.js(CHECKED), ["clear-female"])
        self.assertEqual(b.js("document.querySelectorAll('#voice-list .vplay').length"), 0)
        self.assertEqual(b.js("[...document.querySelectorAll('#voice-list .vnone')].map(x => x.disabled && x.textContent)"),
                         ["sample not made yet"] * len(voices.PRESETS))
        self.assertIn("Voice", b.js("[...document.querySelectorAll('#set-tabs .tab')].map(t => t.textContent)"))
        # a viewer makes no versions: no Voice tab
        self.home(C)
        b.js("document.getElementById('set-btn').click()")
        b.wait_js("!document.getElementById('settings').hidden && !!document.getElementById('pref-save')", 5, "settings")
        self.assertEqual(b.js("[...document.querySelectorAll('#set-tabs .tab')].map(t => t.textContent)"), ["Preferences", "Devices"])

    def test_settings_voice_preview_and_choose(self):
        b, r = self.b, self.r
        self.samples(True)
        try:
            self.settings_voice(B)
            self.assertEqual(b.js("document.querySelectorAll('#voice-list .vnone').length"), 0)
            self.assertEqual(b.js("document.querySelectorAll('#voice-list .vplay:not(:disabled)').length"), len(voices.PRESETS))
            # a sample plays; another one stops it
            n = len(self.log.lines)
            b.js("document.querySelector('.vplay[data-sample=\"warm-male\"]').click()")
            b.wait_js("document.querySelector('.vplay[data-sample=\"warm-male\"]').getAttribute('aria-pressed') === 'true'", 5, "playing")
            r.wait(lambda: any("GET /api/voices/warm-male/sample.mp3" in ln for ln in self.log.lines[n:]), 5, "the sample fetched")
            b.js("document.querySelector('.vplay[data-sample=\"british-female\"]').click()")
            b.wait_js("document.querySelector('.vplay[data-sample=\"british-female\"]').getAttribute('aria-pressed') === 'true'"
                      " && document.querySelector('.vplay[data-sample=\"warm-male\"]').getAttribute('aria-pressed') === 'false'", 5, "the other")
            b.js("document.querySelector('.vplay[data-sample=\"british-female\"]').click()")
            b.wait_js("document.querySelector('.vplay[data-sample=\"british-female\"]').getAttribute('aria-pressed') === 'false'", 5, "paused")
            # choosing is saved at once, and kept
            b.js(f"{ROWS}.find(r => r.dataset.voice === 'warm-male').querySelector('.ver').click()")
            b.wait_js("document.getElementById('voice-msg').textContent === 'Saved'", 5, "saved")
            self.assertEqual(r.q("SELECT voice FROM user_voice WHERE user_id = ?", self.bob)[0][0], "warm-male")
            self.assertEqual(b.js(CHECKED), ["warm-male"])
            self.load("settings=voice")
            b.wait_js(f"{ROWS}.length > 0", 5, "again")
            self.assertEqual(b.js(CHECKED), ["warm-male"])
            self.shot("desktop-settings-voice")
        finally:
            r.q("DELETE FROM user_voice")
            self.samples(False)

    def test_window_change_voice(self):
        b, r = self.b, self.r
        line = "(document.getElementById('w-voice') || {}).textContent"
        try:
            # Carol listens, and sees the voice; she cannot change it
            self.home(C)
            self.open(self.paper)
            b.wait_js(f"{line} === 'Voice: Clear female, measured'", 5, "voice line")
            self.assertIsNone(b.js("document.getElementById('voice-change')"))
            # Bob made it: Change opens the chooser under the player
            self.home(B)
            self.open(self.paper)
            b.wait_js(f"{line} === 'Voice: Clear female, measured · Change'", 5, "Bob's voice line")
            b.js("document.getElementById('voice-change').click()")
            b.wait_js("!document.getElementById('vchoose').hidden && document.querySelectorAll('#vchoose .vrow').length === 6", 5, "chooser")
            self.assertTrue(b.js("document.getElementById('voice-go').disabled"), "the voice it has is no change")
            b.js("[...document.querySelectorAll('#vchoose .vrow')].find(r => r.dataset.voice === 'calm-male').querySelector('.ver').click()")
            b.wait_js("document.getElementById('voice-go').textContent === 'Record it in Calm male, British'", 5, "picked")
            self.shot("desktop-voice-chooser")
            b.js("document.getElementById('voice-go').click()")
            b.wait_js(f"{line} === 'Voice: Clear female, measured · changing to Calm male, British, 1st in line · Cancel'", 5, "queued")
            self.assertTrue(b.js("document.getElementById('vchoose').hidden"))
            self.assertEqual(r.q("SELECT state, user_id FROM voice_jobs WHERE episode_id = ?", self.ep)[0][:], ("queued", self.bob))
            self.assertEqual(b.js("document.getElementById('audio').getAttribute('src')"), f"/audio/{self.ep}.mp3", "the old audio plays on")
            # taken back
            b.js("document.getElementById('voice-cancel').click()")
            b.wait_js(f"{line} === 'Voice: Clear female, measured · Change'", 5, "taken back")
            self.assertEqual(r.q("SELECT state FROM voice_jobs WHERE episode_id = ?", self.ep)[0][0], "done")
            # on a phone every one of these is a 44 px tap
            self.phone()
            self.home(B)
            self.open(self.paper)
            b.wait_js("!!document.getElementById('voice-change')", 5, "phone voice line")
            self.assertTargets("the window's voice line")
            b.js("document.getElementById('voice-change').click()")
            b.wait_js("!document.getElementById('vchoose').hidden && document.querySelectorAll('#vchoose .vrow').length === 6", 5, "phone chooser")
            b.js("document.getElementById('vchoose').scrollIntoView(); document.getElementById('toast').hidden = true")
            self.assertTargets("the voice chooser")
            self.no_side_scroll("the voice chooser")
            self.shot("phone-voice-chooser")
        finally:
            self.b.call("Emulation.clearDeviceMetricsOverride")
            self.b.viewport(1440, 900)
            r.q("DELETE FROM voice_jobs")
            r.q("UPDATE episode_voice SET want = NULL, want_by = NULL, error = NULL")

    def test_swap_while_loaded(self):
        """The new audio lands while the old one is loaded here at 12.3 s (the third sentence, 10.46
        to 15.16 s): the page loads revision 2 at the same place in the third sentence."""
        b, r = self.b, self.r
        audio = "document.getElementById('audio')"
        d = r.cfg.episodes / self.ep
        old_audio = (d / "audio.mp3").read_bytes()
        try:
            self.home(B)
            self.open(self.paper)
            b.wait_js(f"{audio}.getAttribute('src') === '/audio/{self.ep}.mp3' && {audio}.readyState >= 1", 10, "loaded")
            b.js(f"{audio}.currentTime = 12.3")
            b.wait_js(f"Math.abs({audio}.currentTime - 12.3) < 0.05", 5, "at 12.3 s")
            # what the hub does when the worker's MP3 lands (voiceq.audio -> voices.after_audio)
            (d / "timings.next.json").write_text(json.dumps(timings(6, 7.0)))
            (d / "audio.mp3").write_bytes(silent_mp3(42.0))
            with db.transaction() as c:
                ev = voices.after_audio(c, r.cfg, self.ep, 42.0, "preset-warm-male-s42", True)
                c.execute("UPDATE episodes SET duration_s = 42 WHERE id = ?", (self.ep,))
            publish("episode", ev)
            want = 14.46 + (12.3 - 10.46) / 4.7 * 6.7
            b.wait_js(f"{audio}.getAttribute('src') === '/audio/{self.ep}.mp3?v=2' && {audio}.readyState >= 1"
                      f" && Math.abs({audio}.currentTime - {want}) < 0.3", 10, "the new audio at the same sentence")
            b.wait_js("(document.getElementById('w-voice') || {}).textContent === 'Voice: Warm male · Change'", 5, "new voice named")
            self.assertTrue(b.js(f"{audio}.paused"), "it was paused, so it stays paused")
        finally:
            (d / "audio.mp3").write_bytes(old_audio)
            (d / "timings.json").write_text(json.dumps(timings(6, 5.0)))
            (d / "timings.prev.json").unlink(missing_ok=True)
            r.q("UPDATE episode_voice SET voice = 'clear-female', rev = 1 WHERE episode_id = ?", self.ep)
            r.q("UPDATE episodes SET duration_s = 30 WHERE id = ?", self.ep)
            b.js("localStorage.clear()")
            # the old URL's bytes in the browser's cache are now of two files (the ranges it
            # read before and after the file was replaced, which a real hub never serves again
            # under that revision): the next test must not play them
            b.call("Network.clearBrowserCache")


if __name__ == "__main__":
    unittest.main()
