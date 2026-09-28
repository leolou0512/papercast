"""Sentence timings for a play-along transcript: out/timings.json, next to out/episode.mp3.

    {"version": 1, "duration_s": 1043.2,
     "segments": [{"start": 0.46, "end": 4.21, "text": "One sentence, as in script.md."}, ...]}

One segment per sentence of script.md (a heading is one), in script order; times are seconds of
the final MP3. They come from where audio.join() put each chunk: the lead-in, then each chunk
trimmed to its voiced part plus trim_pad_s at both ends, then the pause after it. The encoding
keeps that timeline: loudnorm (linear or dynamic), the resampling and the MP3 encoder moved the
edges of test bursts by less than 10 ms (tests/test_timings.py measures it on the real ffmpeg).
Inside a chunk its sentences share the voiced part in proportion to their characters, and each
boundary between two of them then moves into the pause the voice really made there, when the
chunk has one near it (audio.pauses: a silence of 0.15 s or more); a sentence cut over two
chunks (only one over max_words is) spans both, the pause between them included.

`papercast-voice timings <job dir>` makes the file for an episode voiced before this existed,
while its chunk WAVs are still on disk (a finished job deletes them: job._cleanup_after_done).
"""
from __future__ import annotations

import hashlib
import json
import os
import re

from . import audio, textprep

VERSION = 1
# A boundary moves to a pause whose middle is within SNAP_S + SNAP_FRAC of the chunk's length of
# where the characters put it; the nearest wins, a longer pause counting as nearer (PAUSE_BONUS
# seconds of distance per second of pause): sentences end in longer pauses than commas do.
SNAP_S, SNAP_FRAC, PAUSE_BONUS = 0.4, 0.08, 2.0


class TimingsError(Exception):
    pass


def sentence_chunks(script: str, max_words: int, audio_cfg: dict):
    """textprep.plan()'s chunks, each as [(sentence number, piece of that sentence)], and the
    sentences' texts. Built with textprep's own functions, in plan()'s order."""
    blocks = textprep.parse_blocks(script)
    speak_headings = bool(audio_cfg.get("speak_headings", True))
    sentences: list[str] = []
    chunks: list[list[tuple[int, str]]] = []
    for kind, text in blocks:
        if kind == "heading":
            if not speak_headings:
                continue
            sid = len(sentences)
            sentences.append(text)
            spoken = text if re.search(r"[.!?…:]$", text) else text + "."
            for piece in textprep.split_long(spoken, max_words):
                chunks.append([(sid, piece)])
            continue
        pieces: list[tuple[int, str]] = []
        for s in textprep.split_sentences(text):
            sid = len(sentences)
            sentences.append(s)
            pieces.extend((sid, p) for p in textprep.split_long(s, max_words))
        cur: list[tuple[int, str]] = []
        n = 0
        for sid, p in pieces:                   # textprep._pack, keeping where each piece came from
            k = len(p.split())
            if cur and n + k > max_words:
                chunks.append(cur)
                cur, n = [], 0
            cur.append((sid, p))
            n += k
        if cur:
            chunks.append(cur)
    return chunks, sentences


def _snap(t: float, quiet: list, after: float, win: float):
    """The pause (start, end) for a boundary the characters put at t, or None."""
    best, cost = None, None
    for qa, qb in quiet:
        if qa < after:
            continue
        d = abs((qa + qb) / 2 - t)
        if d > win:
            continue
        c = d - PAUSE_BONUS * (qb - qa)
        if cost is None or c < cost:
            best, cost = (qa, qb), c
    return best


def segments(script: str, chunk_texts: list[str], spans: list, max_words: int,
             audio_cfg: dict, pauses: list | None = None) -> list[dict]:
    """The sentences of `script` with their times. `chunk_texts` and `spans` are the chunks the
    audio was made from and where join() put them; `pauses` (join()'s) the silences inside each.
    If the script does not cut into exactly those chunks (it changed since), every chunk is one
    segment with its own text."""
    if len(chunk_texts) != len(spans):
        raise TimingsError(f"{len(chunk_texts)} chunks but {len(spans)} places for them")
    pad = float(audio_cfg.get("trim_pad_s", 0.0))
    chunks, sentences = sentence_chunks(script, max_words, audio_cfg)
    if [" ".join(p for _s, p in ch) for ch in chunks] != list(chunk_texts):
        chunks = [[(i, t)] for i, t in enumerate(chunk_texts)]
        sentences = list(chunk_texts)
    first: dict[int, float] = {}
    last: dict[int, float] = {}
    for k, (ch, (a, b)) in enumerate(zip(chunks, spans)):
        a, b = float(a), float(b)
        if b - a > 2 * pad:                     # the voiced part, without the trim's padding
            a, b = a + pad, b - pad
        text = " ".join(p for _s, p in ch)
        total = max(1, len(text))
        times, off = [], 0
        for i, (sid, p) in enumerate(ch):
            start = a + (b - a) * off / total
            off += len(p) + (1 if i < len(ch) - 1 else 0)
            times.append([start, b if i == len(ch) - 1 else a + (b - a) * off / total])
        quiet = (pauses[k] if pauses and k < len(pauses) else None) or []
        after = a
        for i in range(1, len(ch)):
            if ch[i][0] == ch[i - 1][0]:        # two pieces of one sentence: no boundary shown
                continue
            q = _snap(times[i][0], quiet, after, SNAP_S + SNAP_FRAC * (b - a))
            if q and times[i - 1][0] < q[0] and q[1] < times[i][1]:     # the order stays
                times[i - 1][1], times[i][0] = q
                after = q[1]
        for (sid, _p), (start, end) in zip(ch, times):
            first.setdefault(sid, start)
            last[sid] = end
    return [{"start": round(first[i], 3), "end": round(last[i], 3), "text": sentences[i]}
            for i in sorted(first)]


def document(segs: list[dict], duration_s: float | None) -> dict:
    return {"version": VERSION, "duration_s": round(float(duration_s), 3) if duration_s else None,
            "segments": segs}


def write(path: str, doc: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, ensure_ascii=False, separators=(",", ":"))
        fh.write("\n")
    os.replace(tmp, path)


def _read_json(path: str):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def from_job_dir(vdir: str, cfg: dict) -> dict:
    """The timings of a voiced job from its chunk WAVs (they must all still be there), placed as
    join() placed them. Checked against the duration status.json gives for the MP3."""
    vdir = os.path.realpath(vdir)
    try:
        with open(os.path.join(vdir, "script.md"), encoding="utf-8") as fh:
            script = fh.read()
    except (OSError, UnicodeDecodeError) as e:
        raise TimingsError(f"cannot read script.md: {e}") from None
    st = _read_json(os.path.join(vdir, "status.json")) or {}
    out = st.get("output") or {}
    root = os.path.join(vdir, "chunks")
    found = []
    for name in sorted(os.listdir(root)) if os.path.isdir(root) else []:
        plan = _read_json(os.path.join(root, name, "plan.json"))
        if not isinstance(plan, dict) or not plan.get("chunks"):
            continue
        cs = plan["chunks"]
        paths = [os.path.join(root, name, f"{c['idx']:04d}-"
                              f"{hashlib.sha1(c['text'].encode()).hexdigest()[:12]}.wav") for c in cs]
        if all(os.path.isfile(p) for p in paths):
            found.append((name, cs, paths, os.path.getmtime(os.path.join(root, name, "plan.json")),
                          plan.get("engine")))
    if not found:
        raise TimingsError("no complete set of chunk WAVs under chunks/ (a finished job deletes "
                           "them; only a job that still has them can be timed afterwards)")
    voice = out.get("voice")
    match = [f for f in found if voice and f"-{voice}-" in f[0]]
    name, cs, paths, _t, engine = (match or sorted(found, key=lambda f: f[3]))[-1]
    m = re.search(r"-w(\d+)-p\d+$", name)
    if m:
        max_words = int(m.group(1))
    else:
        from .config import engine_spec
        max_words = int(engine_spec(cfg, engine or cfg["cpu_engine"])["max_words"])
    a = cfg["audio"]
    lay = audio.layout(paths, [float(c["gap_after_s"]) for c in cs], lead_in_s=a["lead_in_s"],
                       threshold_db=a["trim_threshold_db"], pad_s=a["trim_pad_s"])
    dur = out.get("duration_s")
    if dur and abs(float(dur) - lay["duration_s"]) > 0.5 + 0.005 * lay["duration_s"]:
        raise TimingsError(f"the chunks in chunks/{name} add up to {lay['duration_s']:.1f} s but "
                           f"the MP3 is {float(dur):.1f} s: they are not the ones it was made from")
    segs = segments(script, [c["text"] for c in cs], lay["spans"], max_words, a, lay["pauses"])
    doc = document(segs, float(dur) if dur else lay["duration_s"])
    doc["chunks"] = name
    return doc
