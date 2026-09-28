#!/usr/bin/env python3
"""A stand-in engine for tests: speaks the engine protocol (engines/_proto.py) with any Python,
no model, no GPU. It writes a speech-like tone whose length follows the word count, and records
every text it was given so a test can prove what reached "the engine".

Behaviour from spec["env"] (the orchestrator passes it into the environment):
  FAKE_LOG        append one JSON line per request: {"engine", "idx", "text", "pid"}
  FAKE_PIDFILE    write this worker's pid (so a test can put it in the fake nvidia-smi)
  FAKE_DELAY_S    seconds per chunk (default 0.05)
  FAKE_LOAD_S     seconds to "load" (default 0)
  FAKE_OOM_AT     comma list of chunk indices that fail once with a CUDA out-of-memory
  FAKE_STATE      directory for the once-only markers
  FAKE_SEC_PER_WORD  audio seconds per word (default 0.3)
  FAKE_GPU_PIDDIR a fake remote host's process list: while running, a file "<gpu>-<pid>" there
                  (the GPU from CUDA_VISIBLE_DEVICES), which its fake nvidia-smi reports
An `inline` request (a remote engine, hosts.py) gets its WAV back in the reply, as base64.
"""
import base64
import io
import json
import math
import os
import struct
import sys
import time
import wave

HERE = os.path.dirname(os.path.abspath(__file__))
# _proto.py sits next to this file when it was copied to a (fake) remote host, else in the repo.
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "papercast_voice", "engines"))
sys.path.insert(0, HERE)
import _proto  # noqa: E402

proto = _proto.Proto()
a = _proto.args()
spec = a.spec
env = os.environ
SR = 24000
if env.get("FAKE_PIDFILE"):
    with open(env["FAKE_PIDFILE"], "w") as fh:
        fh.write(str(os.getpid()))
if env.get("FAKE_GPU_PIDDIR") and env.get("CUDA_VISIBLE_DEVICES"):
    os.makedirs(env["FAKE_GPU_PIDDIR"], exist_ok=True)
    open(os.path.join(env["FAKE_GPU_PIDDIR"], f"{env['CUDA_VISIBLE_DEVICES']}-{os.getpid()}"),
         "w").close()
time.sleep(float(env.get("FAKE_LOAD_S", "0")))
proto.send(event="ready", sample_rate=SR, load_s=float(env.get("FAKE_LOAD_S", "0")),
           pid=os.getpid(), dtype=spec.get("dtype"))
oom_at = {int(x) for x in env.get("FAKE_OOM_AT", "").split(",") if x.strip()}


def tone(words: int, seed: int) -> bytes:
    """Voiced 'syllables' at ~4 Hz on a 140 Hz harmonic carrier, silence at both ends."""
    n = int(max(1, words) * float(env.get("FAKE_SEC_PER_WORD", "0.3")) * SR)
    pad = int(0.15 * SR)
    out = bytearray()
    for i in range(pad):
        out += struct.pack("<h", 0)
    f0 = 140 + (seed % 7) * 10
    for i in range(n):
        t = i / SR
        env_ = max(0.0, math.sin(2 * math.pi * 4.0 * t)) ** 2
        s = sum(math.sin(2 * math.pi * f0 * k * t) / k for k in (1, 2, 3, 4))
        out += struct.pack("<h", int(8000 * env_ * s / 2.1))
    for i in range(pad):
        out += struct.pack("<h", 0)
    return bytes(out)


for req in proto.requests():
    idx = req["id"]
    if env.get("FAKE_LOG"):
        with open(env["FAKE_LOG"], "a") as fh:
            fh.write(json.dumps({"engine": spec["name"], "idx": idx, "text": req["text"],
                                 "pid": os.getpid(), "gpu": env.get("CUDA_VISIBLE_DEVICES"),
                                 "t": time.time(), "dtype": spec.get("dtype"),
                                 "voice": spec.get("voice"), "instruction": spec.get("instruction"),
                                 "seed": spec.get("seed")}) + "\n")
    time.sleep(float(env.get("FAKE_DELAY_S", "0.05")))
    marker = os.path.join(env.get("FAKE_STATE", "/tmp"), f"oom-{spec['name']}-{idx}")
    if idx in oom_at and not os.path.exists(marker):
        open(marker, "w").close()
        proto.send(event="error", id=idx, message="CUDA out of memory (fake)", oom=True, fatal=True)
        sys.exit(1)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(tone(len(req["text"].split()), idx))
    msg = {"event": "done", "id": idx, "audio_s": len(req["text"].split()) * 0.3, "gen_s": 0.05}
    if req.get("inline"):
        msg["wav_b64"] = base64.b64encode(buf.getvalue()).decode("ascii")
    else:
        tmp = _proto.tmp_for(req["out"])
        with open(tmp, "wb") as fh:
            fh.write(buf.getvalue())
        os.replace(tmp, req["out"])
    proto.send(**msg)
