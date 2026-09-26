#!/usr/bin/env python3
"""Check each clip says the paragraph: transcribe with Whisper (CPU) and report the
word error rate against the text. Catches skipped, repeated or invented words.

    asr_check.py --dir <out dir> --text-file paragraph.txt breeze voxtral kokoro

Runs in the voxtral venv, which already has openai-whisper and soxr.
"""
from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path

os.environ["CUDA_VISIBLE_DEVICES"] = ""

import numpy as np
import soundfile as sf
import soxr


def words(s: str) -> list[str]:
    s = s.lower().replace("’", "'")
    return re.findall(r"[a-z]+(?:'[a-z]+)?", s)


def wer(ref: list[str], hyp: list[str]) -> tuple[float, int, int, int]:
    d = np.zeros((len(ref) + 1, len(hyp) + 1), dtype=int)
    d[:, 0] = range(len(ref) + 1)
    d[0, :] = range(len(hyp) + 1)
    for i in range(1, len(ref) + 1):
        for j in range(1, len(hyp) + 1):
            d[i, j] = min(d[i - 1, j] + 1, d[i, j - 1] + 1,
                          d[i - 1, j - 1] + (ref[i - 1] != hyp[j - 1]))
    return d[-1, -1] / len(ref), d[-1, -1], len(ref), len(hyp)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", type=Path, required=True)
    ap.add_argument("--text-file", type=Path, required=True)
    ap.add_argument("--model", default="small.en")
    ap.add_argument("names", nargs="+")
    a = ap.parse_args()
    import whisper
    m = whisper.load_model(a.model, device="cpu",
                           download_root=str(Path.home() / "papercast-voice-cache" / "whisper"))
    ref = words(a.text_file.read_text())
    out = {}
    for name in a.names:
        x, sr = sf.read(a.dir / f"{name}.wav", dtype="float32")
        x = soxr.resample(x, sr, 16000)
        hyp_text = m.transcribe(x, language="en", fp16=False,
                                initial_prompt="MatterGen")["text"].strip()
        r, errs, nref, nhyp = wer(ref, words(hyp_text))
        out[name] = {"wer": round(r, 3), "errors": int(errs), "ref_words": nref,
                     "hyp_words": nhyp, "transcript": hyp_text}
        print(name, json.dumps(out[name]))
    (a.dir / "asr.json").write_text(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
