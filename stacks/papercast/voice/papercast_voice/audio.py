"""Chunks -> one episode file: join with pauses, normalise loudness, encode, and verify.

Verification uses instruments other than the ones that did the work: ffmpeg's loudnorm sets the
loudness and pyloudnorm (an independent BS.1770 implementation) measures the decoded result;
ffmpeg's ebur128 gives the true peak; the duration comes from decoding the file, not from the
join's own arithmetic.
"""
from __future__ import annotations

import json
import os
import re
import subprocess

import numpy as np
import soundfile as sf


class EncodeError(Exception):
    pass


def _ff(ffmpeg: str, args: list[str], timeout: float = 1800, binary: bool = False):
    r = subprocess.run([ffmpeg, "-hide_banner", "-nostdin", *args], capture_output=True,
                       timeout=timeout, text=not binary)
    if r.returncode != 0:
        err = r.stderr if not binary else r.stderr.decode("utf-8", "replace")
        raise EncodeError(f"ffmpeg failed ({r.returncode}): {err.strip()[-400:]}")
    return r


def load_mono(path: str) -> tuple[np.ndarray, int]:
    x, sr = sf.read(path, dtype="float32", always_2d=False)
    if x.ndim > 1:
        x = x.mean(axis=1)
    return x, sr


def trim(x: np.ndarray, sr: int, threshold_db: float, pad_s: float) -> np.ndarray:
    """Cut leading and trailing silence to `pad_s`. Silence = 10 ms frames more than
    |threshold_db| below the chunk's loudest frame."""
    hop = max(1, int(0.01 * sr))
    n = len(x) // hop
    if n == 0:
        return x
    rms = np.sqrt(np.mean(x[: n * hop].reshape(n, hop) ** 2, axis=1) + 1e-12)
    db = 20 * np.log10(rms / (rms.max() + 1e-12))
    voiced = np.where(db > threshold_db)[0]
    if len(voiced) == 0:
        return x[:0]
    pad = int(pad_s * sr)
    a = max(0, voiced[0] * hop - pad)
    b = min(len(x), (voiced[-1] + 1) * hop + pad)
    return x[a:b]


def pauses(y: np.ndarray, sr: int, floor_db: float = -35.0, min_s: float = 0.15) -> list:
    """The silences inside a trimmed chunk (where its sentences part), as (start_s, end_s) from
    its start: runs of 10 ms frames more than |floor_db| below its loudest frame, at least min_s
    long, not touching either end."""
    hop = max(1, int(0.01 * sr))
    n = len(y) // hop
    if n < 3:
        return []
    rms = np.sqrt(np.mean(y[: n * hop].reshape(n, hop) ** 2, axis=1) + 1e-12)
    quiet = 20 * np.log10(rms / (rms.max() + 1e-12)) <= floor_db
    edges = np.flatnonzero(np.diff(np.concatenate(([0], quiet.astype(np.int8), [0]))))
    out = []
    for a, b in zip(edges[::2], edges[1::2]):
        if a > 0 and b < n and (b - a) * hop / sr >= min_s:
            out.append((a * hop / sr, b * hop / sr))
    return out


def _trimmed(paths: list[str], gaps_s: list[float], threshold_db: float, pad_s: float):
    """(sample rate, [(trimmed chunk, samples of the pause after it)] one chunk at a time)."""
    if len(paths) != len(gaps_s) or not paths:
        raise EncodeError("nothing to join")
    sr0 = sf.info(paths[0]).samplerate

    def gen():
        for p, g in zip(paths, gaps_s):
            x, sr = load_mono(p)
            if sr != sr0:
                raise EncodeError(f"chunk {p} is {sr} Hz, the others {sr0} Hz")
            y = trim(x, sr, threshold_db, pad_s)
            if len(y) == 0:
                raise EncodeError(f"chunk {p} is silent")
            yield y, int(g * sr)
    return sr0, gen()


def join(paths: list[str], gaps_s: list[float], out_wav: str, *, lead_in_s: float,
         threshold_db: float, pad_s: float) -> dict:
    """Write the episode as one float WAV, streaming (one chunk in memory at a time).
    `spans`: where each trimmed chunk lies in it, (start_s, end_s), and `pauses` the silences
    inside each (pauses()), in the same seconds: for timings.py."""
    sr0, pieces = _trimmed(paths, gaps_s, threshold_db, pad_s)
    total = 0
    spans, quiet = [], []
    with sf.SoundFile(out_wav, "w", samplerate=sr0, channels=1, subtype="FLOAT", format="WAV") as out:
        lead = np.zeros(int(lead_in_s * sr0), dtype=np.float32)
        out.write(lead)
        total += len(lead)
        for y, gap_n in pieces:
            t0 = total / sr0
            spans.append((t0, (total + len(y)) / sr0))
            quiet.append([(t0 + a, t0 + b) for a, b in pauses(y, sr0)])
            out.write(y)
            out.write(np.zeros(gap_n, dtype=np.float32))
            total += len(y) + gap_n
    return {"sample_rate": sr0, "samples": total, "duration_s": total / sr0, "spans": spans,
            "pauses": quiet}


def layout(paths: list[str], gaps_s: list[float], *, lead_in_s: float, threshold_db: float,
           pad_s: float) -> dict:
    """What join() gives for these chunks (the same trim, the same rounding), without writing
    the WAV: the timings of an episode whose chunks are still on disk."""
    sr0, pieces = _trimmed(paths, gaps_s, threshold_db, pad_s)
    total = int(lead_in_s * sr0)
    spans, quiet = [], []
    for y, gap_n in pieces:
        t0 = total / sr0
        spans.append((t0, (total + len(y)) / sr0))
        quiet.append([(t0 + a, t0 + b) for a, b in pauses(y, sr0)])
        total += len(y) + gap_n
    return {"sample_rate": sr0, "samples": total, "duration_s": total / sr0, "spans": spans,
            "pauses": quiet}


def _last_json(text: str) -> dict:
    m = re.findall(r"\{[^{}]*\}", text, flags=re.S)
    if not m:
        raise EncodeError("ffmpeg loudnorm printed no measurement")
    return json.loads(m[-1])


def _loudnorm(ffmpeg: str, in_wav: str, out_wav: str, i_target: float, tp: float, lra: float,
              sr: int) -> dict:
    """EBU R128 two-pass loudnorm: measure, then apply with the measurement, to a float WAV."""
    target = f"I={i_target:.2f}:TP={tp}:LRA={lra}"
    m = _last_json(_ff(ffmpeg, ["-i", in_wav, "-af", f"loudnorm={target}:print_format=json",
                                "-f", "null", "-"]).stderr)
    if m.get("input_i") in (None, "-inf") or float(m["input_i"]) < -70:
        raise EncodeError(f"the joined audio is silent (measured {m.get('input_i')} LUFS)")
    af = (f"loudnorm={target}:measured_I={m['input_i']}:measured_TP={m['input_tp']}"
          f":measured_LRA={m['input_lra']}:measured_thresh={m['input_thresh']}"
          f":offset={m['target_offset']}:linear=true:print_format=json")
    r = _ff(ffmpeg, ["-y", "-i", in_wav, "-af", af, "-ar", str(sr), "-ac", "1",
                     "-c:a", "pcm_f32le", "-f", "wav", out_wav])
    return {"pass1": m, "pass2": _last_json(r.stderr)}


def normalise_encode(ffmpeg: str, in_wav: str, out_path: str, a: dict, work_dir: str) -> dict:
    """Loudness-normalise and encode, steering on measurements of the encoded file.

    Speech peaks about 21 dB above its loudness (the three sample voices), so reaching -16 LUFS
    under the peak limit means ffmpeg's loudnorm limits peaks, and limiting takes loudness with
    it (one pass: 0.5 LU short for Kokoro and Breeze, 1.2 LU for Voxtral). The MP3 encoder then
    moves both again (at 96 kbit/s: -0.4 LU, peak +0.0 to +0.2 dB, measured). So loudnorm starts
    aimed codec_loudness_db high and codec_margin_db under the peak limit, the encoded file is
    measured (ffmpeg ebur128) after every attempt, a peak over the limit lowers the peak target
    by the excess, and a loudness miss over lufs_tolerance moves the loudness target by the miss
    (scaled by how much the last change moved the result). At most four attempts; the caller
    checks the final file.
    """
    if a["format"] != "mp3":
        raise EncodeError(f"format {a['format']!r} is not built (mp3 only, README)")
    limit = float(a["true_peak_db"])
    tp_aim = min(0.0, max(-9.0, limit - float(a.get("codec_margin_db", 1.5))))   # loudnorm's range
    goal, sr = float(a["lufs"]), int(a["sample_rate"])
    tol = float(a.get("lufs_tolerance", 0.5))
    norm = os.path.join(work_dir, "normalised.wav")
    rep: dict = {"aims": []}
    aim, prev = goal + float(a.get("codec_loudness_db", 0.0)), None
    for _ in range(4):
        rep["loudnorm"] = _loudnorm(ffmpeg, in_wav, norm, aim, round(tp_aim, 2), float(a["lra"]), sr)
        _ff(ffmpeg, ["-y", "-i", norm, "-ac", "1", "-c:a", "libmp3lame",
                     "-b:a", f"{a['bitrate_kbps']}k", "-map_metadata", "-1", "-write_xing", "1",
                     "-id3v2_version", "4", "-f", "mp3", out_path])
        e = ebur128(ffmpeg, out_path)
        rep["aims"].append({"aim_lufs": round(aim, 2), "aim_tp": round(tp_aim, 2),
                            "got_lufs": e["lufs"], "got_tp": e["true_peak_db"]})
        miss = goal - e["lufs"]
        peak_ok = e["true_peak_db"] <= limit
        if abs(miss) <= tol and peak_ok:
            break
        tp_changed = not peak_ok
        if not peak_ok:
            tp_aim = max(-9.0, tp_aim - (e["true_peak_db"] - limit) - 0.3)
        if abs(miss) > tol:
            slope = 1.0
            if prev is not None and not prev[2] and abs(aim - prev[0]) > 0.05:
                slope = min(1.0, max(0.2, (e["lufs"] - prev[1]) / (aim - prev[0])))
            prev = (aim, e["lufs"], tp_changed)
            aim = min(goal + 4.0, aim + miss / slope)
    rep["normalization_type"] = rep["loudnorm"]["pass2"].get("normalization_type")
    rep["output_i"] = rep["aims"][-1]["got_lufs"]
    rep["output_tp"] = rep["aims"][-1]["got_tp"]
    try:
        os.unlink(norm)
    except OSError:
        pass
    return rep


def decode(ffmpeg: str, path: str, sr: int) -> np.ndarray:
    r = _ff(ffmpeg, ["-i", path, "-f", "f32le", "-ac", "1", "-ar", str(sr), "-"], binary=True)
    return np.frombuffer(r.stdout, dtype="<f4")


def probe(ffmpeg: str, path: str) -> dict:
    """Codec, rate, channels and bitrate as ffmpeg's demuxer sees the file."""
    r = subprocess.run([ffmpeg, "-hide_banner", "-nostdin", "-i", path], capture_output=True,
                       text=True, timeout=60)
    m = re.search(r"Audio: (\w+)[^,]*, (\d+) Hz, (\w+),[^,]*, (\d+) kb/s", r.stderr)
    if not m:
        raise EncodeError(f"cannot read the encoded file's stream: {r.stderr.strip()[-300:]}")
    return {"codec": m.group(1), "sample_rate": int(m.group(2)), "channels": m.group(3),
            "bitrate_kbps": int(m.group(4))}


def ebur128(ffmpeg: str, path: str) -> dict:
    r = _ff(ffmpeg, ["-i", path, "-af", "ebur128=peak=true", "-f", "null", "-"])
    summary = r.stderr[r.stderr.rfind("Summary:"):]
    i = re.search(r"I:\s+(-?[\d.]+|-inf) LUFS", summary)
    tp = re.search(r"True peak:\s+Peak:\s+(-?[\d.]+|-inf) dBFS", summary)
    if not i or not tp:
        raise EncodeError("ebur128 printed no summary")
    return {"lufs": float(i.group(1)), "true_peak_db": float(tp.group(1))}


def measure(ffmpeg: str, path: str, sr: int) -> dict:
    import pyloudnorm as pyln
    x = decode(ffmpeg, path, sr)
    if len(x) == 0:
        raise EncodeError("the encoded file decodes to nothing")
    lufs = float(pyln.Meter(sr).integrated_loudness(x.astype(np.float64)))
    peak = float(20 * np.log10(np.max(np.abs(x)) + 1e-12))
    e = ebur128(ffmpeg, path)
    return {"duration_s": len(x) / sr, "lufs": lufs, "sample_peak_db": peak,
            "lufs_ffmpeg": e["lufs"], "true_peak_db": e["true_peak_db"], **probe(ffmpeg, path)}
