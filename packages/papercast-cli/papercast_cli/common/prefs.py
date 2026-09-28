"""A listener's preferences, layered on the base prompt (SPEC.md section 10). A9 owns the
wording of render(); the schema is the contract."""
from __future__ import annotations

SCHEMA = {
    "maths": ("words", "key-steps", "full"),
    "emphasis": ("balanced", "theory", "method", "practice"),
    "background": ("newcomer", "field", "specialist"),
}
DEFAULTS = {"maths": "words", "emphasis": "balanced", "background": "field"}
NOTE_MAX = 500


def validate(settings, note) -> list[str]:
    out = []
    if not isinstance(settings, dict):
        return ["settings must be an object"]
    for k, v in settings.items():
        if k not in SCHEMA:
            out.append(f"unknown setting {k!r}")
        elif v not in SCHEMA[k]:
            out.append(f"{k} must be one of {', '.join(SCHEMA[k])}")
    if not isinstance(note, str):
        out.append("note must be text")
    elif len(note) > NOTE_MAX:
        out.append(f"note is {len(note)} characters, at most {NOTE_MAX}")
    return out


def full(settings) -> dict:
    return {**DEFAULTS, **{k: v for k, v in (settings or {}).items() if k in SCHEMA}}


def summary(settings) -> str:
    """A few words for the library: "derivations · practical"; "" for the defaults."""
    s = full(settings)
    bits = []
    if s["maths"] == "key-steps":
        bits.append("key steps")
    elif s["maths"] == "full":
        bits.append("derivations")
    if s["emphasis"] != "balanced":
        bits.append({"theory": "theory", "method": "method", "practice": "practical"}[s["emphasis"]])
    if s["background"] != "field":
        bits.append(s["background"])
    return " · ".join(bits)


def render(settings, note) -> str:
    """The section appended after the base guideline. A9 writes the real wording."""
    s = full(settings)
    lines = ["## This listener's preferences",
             "The rules above decide how the episode sounds and what never goes in; these preferences decide what gets more time.",
             f"- Maths: {s['maths']}.", f"- Emphasis: {s['emphasis']}.", f"- Background: {s['background']}."]
    if note and note.strip():
        lines.append(f"- Their note: {note.strip()}")
    return "\n".join(lines) + "\n"
