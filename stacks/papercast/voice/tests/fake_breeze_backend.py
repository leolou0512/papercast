"""A stand-in for the Breeze model inside the real breeze_worker.py (spec "backend" = this file):
no torch, no GPU. The worker's own code (protocol, length check, retries, OOM handling, WAV
writing) runs unchanged; only `Breeze` is replaced.

Behaviour from the environment (the orchestrator passes spec["env"] through):
  FAKE_BREEZE_MODE   ok | runaway_first | always_long | hit_limit_first | oom_load | oom_synth |
                     cuda_error | short_first
  FAKE_BREEZE_LOG    append one JSON line per synth call: {"text", "seed", "instruction", "pid"}
  FAKE_SEC_PER_WORD  seconds of audio per word for a good take (default 0.48, like clip A)
"""
import json
import os

import numpy as np

SR = 24000


class OutOfMemoryError(RuntimeError):
    """Named like torch.cuda.OutOfMemoryError, which is what the worker recognises."""


class Backend:
    def __init__(self, spec: dict):
        self.spec = spec
        self.mode = os.environ.get("FAKE_BREEZE_MODE", "ok")
        if self.mode == "oom_load":
            raise OutOfMemoryError("CUDA out of memory. Tried to allocate 2.00 GiB (fake)")
        self.sample_rate = SR
        self.info = {"torch": "fake", "fast_stages": sorted(spec.get("fast_stages") or [])}
        self.spw = float(os.environ.get("FAKE_SEC_PER_WORD", "0.48"))

    def synth(self, text: str, seed: int):
        if os.environ.get("FAKE_BREEZE_LOG"):
            with open(os.environ["FAKE_BREEZE_LOG"], "a") as fh:
                fh.write(json.dumps({"text": text, "seed": seed, "pid": os.getpid(),
                                     "instruction": self.spec.get("instruction")}) + "\n")
        words = len(text.split())
        first = seed == int(self.spec.get("seed", 42))
        if self.mode == "oom_synth":
            raise OutOfMemoryError("CUDA out of memory. Tried to allocate 512.00 MiB (fake)")
        if self.mode == "cuda_error":
            raise RuntimeError("CUDA error: an illegal memory access was encountered (fake)")
        secs = words * self.spw
        hit = False
        if self.mode == "always_long" or (self.mode == "runaway_first" and first):
            secs = 3.0 + words * 2.0 + (seed - 40)      # later seeds a little longer still
        if self.mode == "short_first" and first:
            secs = words * 0.05
        if self.mode == "hit_limit_first" and first:
            secs, hit = 120.0, True
        t = np.arange(int(secs * SR)) / SR
        audio = (0.2 * np.sin(2 * np.pi * 180 * t) * (np.sin(2 * np.pi * 4 * t) > 0)).astype(np.float32)
        return audio, int(secs * 12.5), hit
