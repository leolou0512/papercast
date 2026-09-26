#!/usr/bin/env python3
"""Trim, loudness-match and encode the three clips for the choice page.

    finish.py --dir <out dir> breeze voxtral kokoro

For each <name>.wav: trim leading/trailing silence to 0.25 s, apply ONE linear gain
so integrated loudness is TARGET_LUFS (-23) (ITU-R BS.1770 via pyloudnorm; no compression,
no limiting, so each model's own dynamics are untouched), then encode mono MP3 at
48 kbit/s with the ffmpeg bundled in imageio-ffmpeg. Writes <name>.mp3 and
clips.json (duration, loudness before/after, sample peak after gain).
"""
from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

import imageio_ffmpeg
import numpy as np
import pyloudnorm as pyln
import soundfile as sf

TARGET_LUFS = -23.0  # EBU R128; at -18 the peakiest clip clipped by 3 dB
PAD_S = 0.25
MP3_KBPS = 48


def trim(x: np.ndarray, sr: int) -> np.ndarray:
    hop = int(0.01 * sr)
    n = len(x) // hop
    rms = np.sqrt(np.mean(x[: n * hop].reshape(n, hop) ** 2, axis=1) + 1e-12)
    db = 20 * np.log10(rms / (np.max(np.abs(x)) + 1e-12))
    voiced = np.where(db > -45)[0]
    if len(voiced) == 0:
        return x
    a = max(0, voiced[0] * hop - int(PAD_S * sr))
    b = min(len(x), (voiced[-1] + 1) * hop + int(PAD_S * sr))
    return x[a:b]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", type=Path, required=True)
    ap.add_argument("names", nargs="+")
    a = ap.parse_args()
    ff = imageio_ffmpeg.get_ffmpeg_exe()
    out = {}
    for name in a.names:
        x, sr = sf.read(a.dir / f"{name}.wav", dtype="float64")
        if x.ndim > 1:
            x = x.mean(axis=1)
        raw_s = len(x) / sr
        x = trim(x, sr)
        meter = pyln.Meter(sr)
        before = meter.integrated_loudness(x)
        y = x * 10 ** ((TARGET_LUFS - before) / 20)
        after = meter.integrated_loudness(y)
        peak_db = 20 * np.log10(np.max(np.abs(y)))
        norm = a.dir / f"{name}.norm.wav"
        sf.write(norm, np.clip(y, -1, 1).astype(np.float32), sr, subtype="FLOAT")
        mp3 = a.dir / f"{name}.mp3"
        subprocess.run([ff, "-y", "-loglevel", "error", "-i", str(norm), "-ac", "1",
                        "-c:a", "libmp3lame", "-b:a", f"{MP3_KBPS}k", str(mp3)], check=True)
        out[name] = {"raw_s": round(raw_s, 2), "trimmed_s": round(len(y) / sr, 2),
                     "lufs_before": round(float(before), 1), "lufs_after": round(float(after), 1),
                     "sample_peak_dbfs": round(float(peak_db), 1), "clipped": bool(peak_db > 0),
                     "mp3_bytes": mp3.stat().st_size, "sample_rate": sr}
        print(name, out[name])
    (a.dir / "clips.json").write_text(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
