#!/usr/bin/env python3
"""Voxtral TTS (Mistral, 4B) sample through vLLM-Omni, preset voice, bf16.

    gen_voxtral.py --venv <voxtral venv> --stage-config voxtral_tts_16gb.yaml \
                   --text-file paragraph.txt --out voxtral.wav --metrics voxtral.json

Starts `vllm serve ... --omni` on 127.0.0.1 only, waits for it (bounded), asks for
the paragraph twice (run 1 cold, run 2 timed and kept), then stops the server and
its stage processes. RTF = request wall time / audio seconds; server start-up is
reported separately as load_s.
"""
from __future__ import annotations

import argparse
import io
import json
import os
import signal
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

import soundfile as sf

MODEL = "mistralai/Voxtral-4B-TTS-2603"
VOICE = "neutral_female"
PORT = 18091
STARTUP_TIMEOUT_S = 900


def post(text: str) -> bytes:
    body = json.dumps({"input": text, "model": MODEL, "voice": VOICE,
                       "response_format": "wav"}).encode()
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}/v1/audio/speech", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=600) as r:
        return r.read()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--venv", type=Path, required=True)
    ap.add_argument("--stage-config", type=Path, required=True)
    ap.add_argument("--text-file", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--metrics", type=Path, required=True)
    ap.add_argument("--log", type=Path, default=Path("voxtral.server.log"))
    a = ap.parse_args()
    text = a.text_file.read_text().strip()

    cache = Path(os.environ.get("PAPERCAST_VOICE_CACHE", Path.home() / "papercast-voice-cache"))
    env = dict(os.environ,
               VLLM_CACHE_ROOT=str(cache / "vllm-cache"),
               TRITON_CACHE_DIR=str(cache / "triton-cache"),
               SPEECH_VOICE_SAMPLES=str(cache / "voxtral-voice-samples"),
               FLASHINFER_WORKSPACE_BASE=str(cache),
               VLLM_NO_USAGE_STATS="1",
               VLLM_USE_FLASHINFER_SAMPLER="0")
    # Triton builds small C launcher stubs at first use; stibnite has no gcc, so a
    # zig-based `cc` wrapper in the cache stands in (see README).
    if (cache / "bin" / "cc").exists() and "CC" not in env:
        env["CC"] = str(cache / "bin" / "cc")
    cmd = [str(a.venv / "bin" / "vllm-omni"), "serve", MODEL, "--omni",
           "--stage-configs-path", str(a.stage_config),
           "--host", "127.0.0.1", "--port", str(PORT)]
    log = open(a.log, "w")
    t0 = time.time()
    srv = subprocess.Popen(cmd, env=env, stdout=log, stderr=subprocess.STDOUT)
    try:
        server_errors = 0
        while True:
            if srv.poll() is not None:
                raise SystemExit(f"server exited rc={srv.returncode}; see {a.log}")
            if time.time() - t0 > STARTUP_TIMEOUT_S:
                raise SystemExit(f"server not ready after {STARTUP_TIMEOUT_S}s; see {a.log}")
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/health", timeout=5) as r:
                    if r.status == 200:
                        break
            except urllib.error.HTTPError as e:
                # Up but broken (e.g. a 500 from middleware): do not sit on the GPU.
                server_errors += 1
                if server_errors >= 5:
                    raise SystemExit(f"server answers /health with {e.code}; see {a.log}")
            except Exception:
                pass
            time.sleep(2)
        load_s = time.time() - t0
        print(f"server ready in {load_s:.0f} s", flush=True)

        runs, wav = [], b""
        for i in range(2):
            t1 = time.time()
            wav = post(text)
            gen_s = time.time() - t1
            audio, sr = sf.read(io.BytesIO(wav), dtype="float32")
            dur = len(audio) / sr
            runs.append({"gen_s": round(gen_s, 2), "audio_s": round(dur, 2),
                         "rtf": round(gen_s / dur, 3)})
            print(f"run {i + 1}: {gen_s:.1f} s for {dur:.1f} s audio", flush=True)
        sf.write(a.out, audio, sr, subtype="PCM_16")
        m = {"model": MODEL, "voice": VOICE, "runtime": "vllm-omni",
             "stage_config": a.stage_config.name, "dtype": "bfloat16",
             "sample_rate": sr, "load_s": round(load_s, 1), "runs": runs}
        a.metrics.write_text(json.dumps(m, indent=2))
        print(json.dumps(m))
    finally:
        srv.send_signal(signal.SIGINT)
        try:
            srv.wait(timeout=60)
        except subprocess.TimeoutExpired:
            srv.kill()
            srv.wait()
        log.close()


if __name__ == "__main__":
    main()
