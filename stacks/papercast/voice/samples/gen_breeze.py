#!/usr/bin/env python3
"""Breeze TTS 2 sample: voice design (no reference audio), bf16, eager attention.

    gen_breeze.py --code <breeze-tts checkout> --model <weights dir> \
                  --text-file paragraph.txt --out breeze.wav --metrics breeze.json

Generates the paragraph twice with the same seed: run 1 is cold (lazy CUDA init),
run 2 is the timed one and is the file written. Real-time factor = generation wall
time / audio seconds (model load excluded, reported separately).
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np
import soundfile as sf

# The narrator, described in words (Breeze has no preset voices).
INSTRUCTION = (
    "A warm, clear woman in her thirties with a neutral American accent, narrating a "
    "science podcast: measured pace, natural and engaged, explaining a technical idea "
    "to a curious listener."
)
SEED = 42
CFG = 4.0  # upstream README: cfg 4 for instruction following


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--code", type=Path, required=True)
    ap.add_argument("--model", type=Path, required=True)
    ap.add_argument("--text-file", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--metrics", type=Path, required=True)
    ap.add_argument("--fast", default="",
                    help="comma list of fast stages: text_encoder,backbone_prefill,"
                         "backbone_decode,depth_decoder,codec (default: all eager)")
    a = ap.parse_args()
    fast = {f.strip() for f in a.fast.split(",") if f.strip()}
    sys.path.insert(0, str(a.code))

    import torch
    from breeze_infer.runtime import (load_runtime, resolve_device, set_all_seeds,
                                      update_generation_config_for_breeze)
    from breeze_infer.templates import get_template, prepare_inputs, select_template_name
    from models.fast_streaming import FastBreezeStreamingRuntime, FastStreamingConfig

    text = a.text_file.read_text().strip()
    t0 = time.time()
    tok, model, atok = load_runtime(a.model, device=resolve_device(),
                                    attn_implementation="eager")
    update_generation_config_for_breeze(model)
    rt = FastBreezeStreamingRuntime(
        model, atok,
        FastStreamingConfig(max_new_tokens=1500, max_seq_len=2048, fast_all=None,
                            fast_text_encoder="text_encoder" in fast,
                            fast_backbone_prefill="backbone_prefill" in fast,
                            fast_backbone_decode="backbone_decode" in fast,
                            fast_depth_decoder="depth_decoder" in fast,
                            fast_codec="codec" in fast, repetition_penalty=1.1),
        tokenizer=tok)
    if rt.fast_enabled:
        from dataclasses import replace
        from models.warmup_profile import load_warmup_profile
        prof = load_warmup_profile(a.code / "configs" / "fast.json")
        rt.warmup_from_profile(replace(prof, codec_chunk_frames=rt.codec_chunk_frames))
    torch.cuda.synchronize()
    load_s = time.time() - t0

    req = {"id": "sample", "text": text, "speaker": "S0", "instruction": INSTRUCTION}
    runs = []
    audio = None
    for i in range(2):
        set_all_seeds(SEED)
        t1 = time.time()
        inputs = prepare_inputs(tok, atok, model, [req], get_template(select_template_name(req)),
                                guidance_scale=CFG, guidance_scale_ref=None,
                                guidance_scale_ins=None)
        chunks = [c.audio for c in rt.iter_audio_chunks(inputs, request_id="sample", seed=SEED)]
        torch.cuda.synchronize()
        gen_s = time.time() - t1
        audio = np.concatenate(chunks).astype(np.float32)
        dur = len(audio) / rt.sample_rate
        runs.append({"gen_s": round(gen_s, 2), "audio_s": round(dur, 2),
                     "rtf": round(gen_s / dur, 3)})
        print(f"run {i + 1}: {gen_s:.1f} s for {dur:.1f} s audio", flush=True)

    sf.write(a.out, audio, rt.sample_rate, subtype="PCM_16")
    m = {
        "model": "BreezeBlue/Breeze-TTS-2", "mode": "voice design", "instruction": INSTRUCTION,
        "seed": SEED, "cfg_scale": CFG, "dtype": "bfloat16", "attn": "eager",
        "fast_stages": sorted(fast),
        "sample_rate": rt.sample_rate, "load_s": round(load_s, 1), "runs": runs,
        "torch_max_allocated_mib": round(torch.cuda.max_memory_allocated() / 2**20),
        "torch_max_reserved_mib": round(torch.cuda.max_memory_reserved() / 2**20),
        "gpu": torch.cuda.get_device_name(0), "torch": torch.__version__,
    }
    a.metrics.write_text(json.dumps(m, indent=2))
    print(json.dumps(m))


if __name__ == "__main__":
    main()
