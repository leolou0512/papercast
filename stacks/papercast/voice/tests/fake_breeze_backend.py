"""A stand-in for the Breeze model inside the real breeze_worker.py (spec "backend" = this file):
no torch, no GPU. The worker's own code (protocol, length check, retries, OOM handling, WAV
writing) runs unchanged; only `Breeze` is replaced.

Behaviour from the environment (the orchestrator passes spec["env"] through):
  FAKE_BREEZE_MODE   ok | runaway_first | always_long | hit_limit_first | oom_load | oom_synth |
                     cuda_error | short_first
  FAKE_BREEZE_LOG    append one JSON line per synth call: {"text", "seed", "instruction", "pid",
                     "reference": the reference clip's sha256 (a clone) or null (voice design)}
  FAKE_SEC_PER_WORD  seconds of audio per word for a good take (default 0.48, like clip A)
  FAKE_BREEZE_DELAY_S  seconds each synth call takes (default 0)
"""
import json
import os
import time

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

    def synth(self, text: str, seed: int, ref: dict | None = None):
        if os.environ.get("FAKE_BREEZE_LOG"):
            with open(os.environ["FAKE_BREEZE_LOG"], "a") as fh:
                fh.write(json.dumps({"text": text, "seed": seed, "pid": os.getpid(),
                                     "instruction": self.spec.get("instruction"),
                                     "reference": ref["sha256"] if ref else None,
                                     "reference_text": ref["text"] if ref else None}) + "\n")
        time.sleep(float(os.environ.get("FAKE_BREEZE_DELAY_S", "0")))
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
        # the pitch stands in for the speaker: a design draws one from the text, instruction and
        # seed (as the real model does); a clone takes the reference clip's
        key = ref["sha256"] if ref else f"{text}|{self.spec.get('instruction')}|{seed}"
        f0 = 150 + int.from_bytes(__import__("hashlib").sha256(key.encode()).digest()[:2], "big") % 100
        audio = (0.2 * np.sin(2 * np.pi * f0 * t) * (np.sin(2 * np.pi * 4 * t) > 0)).astype(np.float32)
        return audio, int(secs * 12.5), hit
