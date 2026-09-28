# Prompts

**The base.** `base-guideline.md` is base prompt v1: Leo's seed guideline
(`stacks/papercast/runner/seed-guideline.md`) made general. "The listener" instead of Leo, no vault
(what the listener knows comes from their background setting), no chat; every other rule kept, in
Leo's words. It is the craft: how an episode sounds and what never goes in. The mechanics (which
files to write, their formats, the repair and cut turns) are the CLI pipeline's
(`packages/papercast-cli/papercast_cli/pipeline/prompts.py`). The same text ships in the package as
`papercast_cli/common/base_guideline.md` (`common.base_guideline()`), with the never-wanted wording
list `common/wording.json` (`common.wording.read()`), so the hub can store both as version 1.

**Preferences.** Each listener's settings (`maths`, `emphasis`, `background`) and note are
rendered by `common.prefs.render(settings, note)` into a short section placed after the base,
headed "This listener's preferences": one sentence per setting that differs from the defaults,
then the note, quoted as the listener's own words and marked as unable to override the rules.
Defaults with no note render to nothing, so the default episode is the base alone. The base's rules
are binding over the preferences; preferences only decide what gets more time.

**Versions.** The hub keeps every base prompt as a numbered version (`base_prompts`: the guideline
and its wording JSON). An admin saving a new base makes a new version; old ones are never changed.
The CLI fetches the latest (`GET /api/cli/prompt`) and each episode records the version it was
made with (`base_version` in the bundle and the `episodes` row), so any episode can be traced to
the exact text it was written from. An upload is checked against the version it names:
`common.checks.episode_problems(..., base_range=minutes_range(<its guideline>), data=<its
wording>)`.
