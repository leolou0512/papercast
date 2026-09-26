#!/usr/bin/env python3
"""Kokoro-82M sample on CPU (the fallback voice), voice af_heart.

    gen_kokoro.py --text-file paragraph.txt --out kokoro.wav --metrics kokoro.json \
                  [--threads 8]

CPU only (CUDA hidden). Generates twice; run 2 is timed and kept. The thread count
is part of the measurement: RTF on 8 threads of the Xeon Gold 6226R is not RTF on 64.
"""
from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

os.environ["CUDA_VISIBLE_DEVICES"] = ""

import numpy as np
import soundfile as sf

VOICE = "af_heart"   # the highest-graded voice in Kokoro's VOICES.md
LANG = "a"           # American English


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--text-file", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--metrics", type=Path, required=True)
    ap.add_argument("--threads", type=int, default=8)
    a = ap.parse_args()

    import torch
    torch.set_num_threads(a.threads)
    from kokoro import KPipeline

    text = a.text_file.read_text().strip()
    t0 = time.time()
    pipe = KPipeline(lang_code=LANG, repo_id="hexgrad/Kokoro-82M", device="cpu")
    load_s = time.time() - t0

    runs, audio = [], None
    for i in range(2):
        t1 = time.time()
        # split_pattern=None: one call per paragraph; Kokoro chunks internally by tokens.
        parts = [r.audio.numpy() for r in pipe(text, voice=VOICE, speed=1.0,
                                               split_pattern=None)]
        gen_s = time.time() - t1
        audio = np.concatenate(parts).astype(np.float32)
        dur = len(audio) / 24000
        runs.append({"gen_s": round(gen_s, 2), "audio_s": round(dur, 2),
                     "rtf": round(gen_s / dur, 3)})
        print(f"run {i + 1}: {gen_s:.1f} s for {dur:.1f} s audio", flush=True)

    sf.write(a.out, audio, 24000, subtype="PCM_16")
    m = {"model": "hexgrad/Kokoro-82M", "voice": VOICE, "device": "cpu",
         "torch_threads": a.threads, "cpu": "Xeon Gold 6226R (val-stibnite), niced",
         "sample_rate": 24000, "load_s": round(load_s, 1), "runs": runs,
         "torch": torch.__version__}
    a.metrics.write_text(json.dumps(m, indent=2))
    print(json.dumps(m))


if __name__ == "__main__":
    main()
