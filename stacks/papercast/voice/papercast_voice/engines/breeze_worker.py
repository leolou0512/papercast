#!/usr/bin/env python3
"""Breeze TTS 2 on the GPU: the voice Leo picked (clip A on 2026-09-26_papercast-voice.html).
Runs in the Breeze venv (engines/breeze/venv), speaks engines/_proto.py.

What made clip A, kept exactly (samples/gen_breeze.py): voice design with no reference audio,
the narrator described in words (spec "instruction"), classifier-free guidance 4, bf16, eager
attention, and two of upstream's five fast stages as CUDA graphs (spec "fast_stages":
depth_decoder + backbone_decode: real-time factor 0.83 at 9.5 GiB, against 3.56 all eager; all
five need about 14.4 GiB). Every chunk is generated from the same seed (spec "seed"), so the same
text always gives the same audio and every episode starts from the same narrator description.

Each chunk's length is checked against its word count before it is accepted: a model like this
can stop early (words missing) or run on (babble, or the 1,500-frame cap). Such a chunk is tried
again with the next seed, up to spec "attempts" times; if none passes, the attempt closest to the
expected length is kept and the reply says so (`warning`), so one odd chunk does not fail an
episode that Retry would only fail again.

A CUDA out-of-memory, at load or mid-chunk, is reported with `oom: true` and ends the worker, so
the job frees the card and waits for it again (INTERFACE §10.4).

For tests, spec "backend" may name a Python file defining `Backend(spec)` with `.sample_rate`,
`.info` and `.synth(text, seed) -> (float32 numpy audio, frames, hit_limit)`; the default is the
real model below.
"""
from __future__ import annotations

import base64
import importlib.util
import io
import os
import socket
import sys
import threading
import time
import warnings

warnings.filterwarnings("ignore", category=UserWarning)
warnings.filterwarnings("ignore", category=FutureWarning)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _proto  # noqa: E402


def expected_s(words: int, sec_per_word: float) -> float:
    return max(1, words) * sec_per_word


def length_problem(audio_s: float, words: int, spec: dict, hit_limit: bool) -> str | None:
    """Why this chunk's audio cannot be the text read once at a normal pace, or None.

    Bounds, in seconds per word: clip A read 74 words in 35.7 s (0.48 s a word). Under
    min_s_per_word (0.15, 400 words a minute) words are missing; over max_s_per_word (1.2,
    50 a minute) plus a 2 s allowance for a heading's breath, it is running on. Chunks of under
    four words (headings) are only checked for running on."""
    if hit_limit:
        return "generation hit the frame limit without ending (runaway or cut off)"
    lo = float(spec.get("min_s_per_word", 0.15)) * words
    hi = 2.0 + float(spec.get("max_s_per_word", 1.2)) * words
    if audio_s <= 0.05:
        return "no audio"
    if audio_s > hi:
        return f"{audio_s:.1f} s for {words} words is too long (over {hi:.1f} s)"
    if words >= 4 and audio_s < lo:
        return f"{audio_s:.1f} s for {words} words is too short (under {lo:.1f} s)"
    return None


def signal_problem(audio) -> str | None:
    """NaN or infinite samples, or near-silence: what a lower-precision run goes wrong with
    (fp16 overflow). The length check cannot see either."""
    import numpy as np
    a = np.asarray(audio, dtype=np.float32)
    if a.size and not np.isfinite(a).all():
        return f"{int((~np.isfinite(a)).sum())} samples are NaN or infinite"
    if a.size and float(np.sqrt(np.mean(a.astype(np.float64) ** 2))) < 1e-4:
        return "the audio is silent"
    return None


def is_oom(e: BaseException) -> bool:
    name = e.__class__.__name__
    s = str(e).lower()
    return name == "OutOfMemoryError" or "out of memory" in s or "cublas_status_alloc_failed" in s


class Breeze:
    """The real model: the loading and generation code of samples/gen_breeze.py."""

    def __init__(self, spec: dict, threads: int):
        import numpy as np
        import torch
        self.np, self.torch = np, torch
        torch.set_num_threads(max(1, threads))
        code = spec["code_dir"]
        sys.path.insert(0, code)
        from breeze_infer.runtime import (load_runtime, resolve_device, set_all_seeds,
                                          update_generation_config_for_breeze)
        from breeze_infer.templates import get_template, prepare_inputs, select_template_name
        from models.fast_streaming import FastBreezeStreamingRuntime, FastStreamingConfig
        self._seed_all, self._prepare = set_all_seeds, prepare_inputs
        self._tpl = lambda req: get_template(select_template_name(req))
        fast = set(spec.get("fast_stages") or [])
        self.max_new_tokens = int(spec.get("max_new_tokens", 1500))
        from pathlib import Path
        dtype = spec.get("dtype") or "bf16"
        if dtype == "bf16":
            # Clip A's path, unchanged: upstream's loader, bf16 throughout.
            self.tok, self.model, self.atok = load_runtime(Path(spec["model_dir"]),
                                                           device=resolve_device(),
                                                           attn_implementation="eager")
        else:
            self.tok, self.model, self.atok = _load_as(Path(spec["model_dir"]), resolve_device(),
                                                       DTYPES[dtype])
        update_generation_config_for_breeze(self.model)
        self.rt = FastBreezeStreamingRuntime(
            self.model, self.atok,
            FastStreamingConfig(max_new_tokens=self.max_new_tokens,
                                max_seq_len=int(spec.get("max_seq_len", 2048)), fast_all=None,
                                fast_text_encoder="text_encoder" in fast,
                                fast_backbone_prefill="backbone_prefill" in fast,
                                fast_backbone_decode="backbone_decode" in fast,
                                fast_depth_decoder="depth_decoder" in fast,
                                fast_codec="codec" in fast,
                                repetition_penalty=float(spec.get("repetition_penalty", 1.1))),
            tokenizer=self.tok)
        if self.rt.fast_enabled:
            from dataclasses import replace
            from models.warmup_profile import load_warmup_profile
            prof = load_warmup_profile(os.path.join(code, "configs", "fast.json"))
            self.rt.warmup_from_profile(replace(prof, codec_chunk_frames=self.rt.codec_chunk_frames))
        self.instruction = spec["instruction"]
        self.speaker = spec.get("speaker", "S0")
        self.cfg_scale = float(spec.get("cfg_scale", 4.0))
        self.sample_rate = self.rt.sample_rate
        torch.cuda.synchronize()
        self.n = 0
        self.info = {"torch": torch.__version__, "gpu": torch.cuda.get_device_name(0),
                     "fast_stages": sorted(fast), "dtype": dtype,
                     "model_dtype": str(next(self.model.parameters()).dtype),
                     "torch_reserved_mib": round(torch.cuda.memory_reserved() / 2**20)}

    def synth(self, text: str, seed: int):
        torch, np = self.torch, self.np
        self.n += 1
        req = {"id": f"c{self.n}", "text": text, "speaker": self.speaker,
               "instruction": self.instruction}
        self._seed_all(seed)
        frames = [0]

        def count(_frame) -> None:
            frames[0] += 1

        with torch.inference_mode():
            inputs = self._prepare(self.tok, self.atok, self.model, [req], self._tpl(req),
                                   guidance_scale=self.cfg_scale, guidance_scale_ref=None,
                                   guidance_scale_ins=None)
            parts = [c.audio for c in self.rt.iter_audio_chunks(
                inputs, request_id=req["id"], seed=seed, token_observer=count)]
        torch.cuda.synchronize()
        audio = np.concatenate(parts).astype(np.float32) if parts else np.zeros(0, np.float32)
        return audio, frames[0], frames[0] >= self.max_new_tokens - 1

    def memory(self) -> dict:
        t = self.torch
        return {"torch_max_reserved_mib": round(t.cuda.max_memory_reserved() / 2**20),
                "torch_max_allocated_mib": round(t.cuda.max_memory_allocated() / 2**20)}


DTYPES = {"fp16": "float16", "fp32": "float32"}


def _load_as(ckpt_dir, device: str, dtype_name: str):
    """Upstream's load_runtime (breeze_infer/runtime.py at 008f769) in another precision, for
    GPUs without native bf16 (Turing: bs1's Quadro RTX 6000). The weights load straight onto
    the card in that precision (device_map), not onto the CPU first: a float32 copy on the CPU
    is 14 GB of RAM per engine (measured 21 GB peak resident). The text encoder is built in bf16
    explicitly, so from_pretrained's dtype alone leaves it: model.to() casts the rest. The fast
    stages take their dtype from the model; the audio tokenizer stays float32 as upstream's."""
    import torch
    from transformers import AutoTokenizer
    from models.breeze import BreezeForConditionalGeneration
    from qwen_tts import Qwen3TTSTokenizer
    dt = getattr(torch, dtype_name)
    if device.startswith("cuda"):
        torch.cuda.set_device(device)
    tok = AutoTokenizer.from_pretrained(ckpt_dir, fix_mistral_regex=False)
    model = BreezeForConditionalGeneration.from_pretrained(ckpt_dir, dtype=dt,
                                                           attn_implementation="eager",
                                                           device_map={"": device})
    model.to(dt)
    model.eval()
    torch.cuda.empty_cache()
    atok = Qwen3TTSTokenizer.from_pretrained(str(ckpt_dir / "audio_tokenizer"), device_map=device)
    return tok, model, atok


def _idle_exit(state: dict, limit_s: float) -> None:
    """A remote engine (spec idle_exit_s) ends itself when no request came for limit_s while it
    was idle: if the orchestrator's machine vanished, the ssh session may never close, and the
    engine must not sit on someone's GPU."""
    while True:
        time.sleep(min(5.0, limit_s / 4))
        if not state["busy"] and time.time() - state["last"] > limit_s:
            print(f"breeze: no request for {limit_s:.0f} s; exiting", file=sys.stderr, flush=True)
            os._exit(3)


def load_backend(spec: dict, threads: int):
    path = spec.get("backend")
    if not path or path == "breeze":
        return Breeze(spec, threads)
    s = importlib.util.spec_from_file_location("breeze_fake_backend", path)
    mod = importlib.util.module_from_spec(s)
    s.loader.exec_module(mod)
    return mod.Backend(spec)


def main() -> None:
    proto = _proto.Proto()
    a = _proto.args()
    spec = a.spec
    t0 = time.time()
    try:
        be = load_backend(spec, a.threads)
    except BaseException as e:  # noqa: BLE001  (SystemExit included: a load failure is fatal)
        proto.send(event="error", id=None, oom=is_oom(e), fatal=True,
                   message=f"load failed: {e.__class__.__name__}: {e}"[:500])
        sys.exit(1)
    import numpy as np
    import soundfile as sf
    sr = int(be.sample_rate)
    base_seed = int(spec.get("seed", 42))
    attempts = max(1, int(spec.get("attempts", 3)))
    spw = float(spec.get("expected_s_per_word", 0.48))
    proto.send(event="ready", sample_rate=sr, load_s=round(time.time() - t0, 2),
               seed=base_seed, pid=os.getpid(), host=socket.gethostname(), **(be.info or {}))
    state = {"busy": False, "last": time.time()}
    if spec.get("idle_exit_s"):
        threading.Thread(target=_idle_exit, args=(state, float(spec["idle_exit_s"])),
                         daemon=True).start()

    for req in proto.requests():
        state["busy"], state["last"] = True, time.time()
        rid = req.get("id")
        text = req["text"]
        words = len(text.split())
        try:
            t1 = time.time()
            tries = []
            best = None
            for k in range(attempts):
                seed = base_seed + k
                audio, frames, hit = be.synth(text, seed)
                dur = len(audio) / sr
                sig = signal_problem(audio)
                if sig:
                    audio = np.nan_to_num(np.asarray(audio, dtype=np.float32))
                why = length_problem(dur, words, spec, hit) or sig
                tries.append({"seed": seed, "audio_s": round(dur, 2), "frames": frames,
                              "problem": why})
                off = abs(dur - expected_s(words, spw)) + (1e6 if hit or dur <= 0.05 or sig
                                                           else 0)
                if best is None or off < best[0]:
                    best = (off, audio, seed, why)
                if why is None:
                    break
                print(f"breeze: chunk {rid} seed {seed}: {why}; trying the next seed",
                      file=sys.stderr, flush=True)
            _off, audio, seed, why = best
            if len(audio) == 0:
                raise RuntimeError(f"no audio in {attempts} attempts")
            msg = {"event": "done", "id": rid, "audio_s": round(len(audio) / sr, 3),
                   "gen_s": round(time.time() - t1, 3), "seed": seed, "attempts": len(tries)}
            if req.get("inline"):
                # A remote engine: the WAV travels in the reply (base64), nothing stays there.
                buf = io.BytesIO()
                sf.write(buf, np.asarray(audio, dtype=np.float32), sr, subtype="PCM_16",
                         format="WAV")
                msg["wav_b64"] = base64.b64encode(buf.getvalue()).decode("ascii")
            else:
                tmp = _proto.tmp_for(req["out"])
                sf.write(tmp, np.asarray(audio, dtype=np.float32), sr, subtype="PCM_16",
                         format="WAV")
                os.replace(tmp, req["out"])
            if len(tries) > 1:
                msg["tries"] = tries
            if why is not None:
                msg["warning"] = f"kept the closest of {len(tries)} attempts: {why}"
                print(f"breeze: chunk {rid}: {msg['warning']}", file=sys.stderr, flush=True)
            if hasattr(be, "memory"):
                msg.update(be.memory())
            proto.send(**msg)
        except BaseException as e:  # noqa: BLE001
            if isinstance(e, KeyboardInterrupt):
                raise
            oom = is_oom(e)
            fatal = oom or "cuda" in str(e).lower() or isinstance(e, MemoryError)
            proto.send(event="error", id=rid, oom=oom, fatal=fatal,
                       message=f"{e.__class__.__name__}: {e}"[:500])
            if fatal:
                sys.exit(1)
        finally:
            state["busy"], state["last"] = False, time.time()


if __name__ == "__main__":
    main()
