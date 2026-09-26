#!/usr/bin/env python3
"""Kokoro-82M on the CPU: the "Use CPU voice (Kokoro)" engine. Runs in the Kokoro venv.

Loads the model from local files only (config.json, kokoro-v1_0.pth, voices/<voice>.pt under
spec.model_dir; HF_HUB_OFFLINE=1 set by the orchestrator), so a run never depends on the network.
Thread count comes from the orchestrator (README "CPU voice": measured, not guessed).
"""
from __future__ import annotations

import os
import sys
import time
import warnings

# torch's own deprecation notices about Kokoro's layers: one per worker, never actionable here.
warnings.filterwarnings("ignore", category=UserWarning, module=r"torch\.")
warnings.filterwarnings("ignore", category=FutureWarning, module=r"torch\.")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _proto  # noqa: E402

proto = _proto.Proto()
a = _proto.args()
spec = a.spec
t0 = time.time()

import numpy as np  # noqa: E402
import soundfile as sf  # noqa: E402
import torch  # noqa: E402

torch.set_num_threads(max(1, a.threads))
torch.set_num_interop_threads(1)
from kokoro import KModel, KPipeline  # noqa: E402

REPO = "hexgrad/Kokoro-82M"
md = spec["model_dir"]
SR = 24000
try:
    model = KModel(repo_id=REPO, config=os.path.join(md, "config.json"),
                   model=os.path.join(md, "kokoro-v1_0.pth")).eval()
    pipe = KPipeline(lang_code="a", repo_id=REPO, model=model, device="cpu")
    voice = os.path.join(md, "voices", f"{spec['voice']}.pt")
    pipe.load_voice(voice)
    speed = float(spec.get("speed", 1.0))
    with torch.inference_mode():            # first call pays one-off costs; not a chunk
        list(pipe("Ready.", voice=voice, speed=speed, split_pattern=None))
except Exception as e:  # noqa: BLE001
    proto.send(event="error", id=None, message=f"load failed: {e.__class__.__name__}: {e}"[:500],
               oom=False, fatal=True)
    sys.exit(1)
proto.send(event="ready", sample_rate=SR, load_s=round(time.time() - t0, 2),
           torch=torch.__version__, threads=torch.get_num_threads())

for req in proto.requests():
    rid = req.get("id")
    try:
        t1 = time.time()
        with torch.inference_mode():
            parts = [r.audio.numpy() for r in pipe(req["text"], voice=voice, speed=speed,
                                                   split_pattern=None) if r.audio is not None]
        if not parts:
            raise RuntimeError("Kokoro produced no audio for this text")
        audio = np.concatenate(parts).astype(np.float32)
        tmp = _proto.tmp_for(req["out"])
        sf.write(tmp, audio, SR, subtype="PCM_16", format="WAV")
        os.replace(tmp, req["out"])
        proto.send(event="done", id=rid, audio_s=round(len(audio) / SR, 3),
                   gen_s=round(time.time() - t1, 3))
    except MemoryError as e:
        proto.send(event="error", id=rid, message=f"out of memory: {e}"[:500], oom=False, fatal=True)
        sys.exit(1)
    except Exception as e:  # noqa: BLE001
        proto.send(event="error", id=rid, message=f"{e.__class__.__name__}: {e}"[:500],
                   oom=False, fatal=False)
