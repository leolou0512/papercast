"""The designed narrator of a GPU voice: one reference clip per voice, made once and kept, so every
chunk of every episode in that voice is spoken from the same clip.

Why (2026-09-29, Leo: "why it seem to switch voices every other sentence?"): Breeze TTS 2 voice
design has no fixed speaker. Given a description and a seed it draws a speaker for the text it is
given, so each chunk (two to four sentences) was a fresh draw: a different woman every chunk.
Measured on the group's default-voice episode e_emcv7ywo56j6 (a speaker-embedding model,
README "Measured"): the two halves of one chunk scored 0.83 alike, two neighbouring chunks 0.38
(0.87 and 0.88 for the CPU voice, whose speaker is a fixed file).

So a voice with `reference_text` in its spec is designed once: that paragraph, voice-designed from
the spec's instruction and seed exactly as a chunk used to be (the same request, so the clip is the
voice the presets' samples and the custom-voice previews let people hear and pick). The clip is
kept here, under the engine's `references_dir`; every chunk is then voiced from it (the engine's
voice clone: the clip and its transcript in the prompt).

    <references_dir>/<voice>-<recipe id>.wav    the clip (24 kHz mono PCM, about 20 s)
    <references_dir>/<voice>-<recipe id>.json   what made it (the recipe), its sha256, when, where

The recipe id is a hash of everything that shapes the clip (model, voice key, instruction, seed,
speaker, CFG, the paragraph), so a changed description never finds an old clip. The first clip
kept wins: a second job that designed the same voice at the same moment uses the one kept.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time


def recipe(spec: dict) -> dict:
    return {"engine": spec.get("name"), "model": spec.get("label"), "voice": spec.get("voice"),
            "instruction": spec.get("instruction"), "seed": int(spec.get("seed", 42)),
            "speaker": spec.get("speaker", "S0"), "cfg_scale": float(spec.get("cfg_scale", 4.0)),
            "text": spec["reference_text"]}


def recipe_id(spec: dict) -> str:
    return hashlib.sha256(json.dumps(recipe(spec), sort_keys=True).encode()).hexdigest()[:16]


def uses_reference(spec: dict) -> bool:
    return bool(spec.get("reference_text")) and bool(spec.get("references_dir"))


def paths(spec: dict) -> tuple[str, str]:
    name = re.sub(r"[^A-Za-z0-9._-]", "_", str(spec.get("voice") or "voice"))[:80]
    base = os.path.join(spec["references_dir"], f"{name}-{recipe_id(spec)}")
    return base + ".wav", base + ".json"


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def load(spec: dict) -> tuple[bytes, dict] | None:
    """The kept clip and its record, or None if there is none (or it does not check out)."""
    wav, meta = paths(spec)
    try:
        with open(meta, encoding="utf-8") as fh:
            m = json.load(fh)
        with open(wav, "rb") as fh:
            data = fh.read()
    except (OSError, ValueError):
        return None
    if not isinstance(m, dict) or m.get("sha256") != sha256(data) or m.get("recipe") != recipe(spec):
        return None
    return data, m


def store(spec: dict, data: bytes, meta: dict, wait_s: float = 10.0) -> tuple[bytes, dict, bool]:
    """Keep this clip as the voice's reference unless one is kept already. Returns (the clip in
    use, its record, whether this call kept it). The WAV is linked into place exclusively, so of
    two jobs designing the same voice at once one wins and the other takes the winner's."""
    wav, mpath = paths(spec)
    os.makedirs(os.path.dirname(wav), mode=0o700, exist_ok=True)
    m = {**meta, "recipe": recipe(spec), "sha256": sha256(data), "bytes": len(data),
         "made_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    tw, tm = f"{wav}.{os.getpid()}.tmp", f"{mpath}.{os.getpid()}.tmp"
    with open(tw, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    with open(tm, "w", encoding="utf-8") as fh:
        json.dump(m, fh, indent=1)
        fh.write("\n")
    try:
        for attempt in (1, 2):
            try:
                os.link(tw, wav)
            except FileExistsError:
                end = time.time() + wait_s
                while time.time() < end:
                    got = load(spec)
                    if got:
                        return got[0], got[1], False
                    time.sleep(0.2)
                if attempt == 1:
                    # A clip with no record that checks out, for longer than any writer takes:
                    # left by a crash (or damaged). Set it aside, keep ours.
                    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
                    for p in (wav, mpath):
                        if os.path.exists(p):
                            os.replace(p, f"{p}.bad-{stamp}")
                    continue
                return data, m, False
            os.replace(tm, mpath)
            return data, m, True
        return data, m, False
    finally:
        for p in (tw, tm):
            try:
                os.unlink(p)
            except FileNotFoundError:
                pass
