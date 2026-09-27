"""Configuration: code defaults, overlaid by `$PAPERCAST_VOICE_HOME/voice.json`.

The runner starts the voice with a cleaned environment (INTERFACE.md §5.5, §10.1: HOME, USER,
LANG, PATH only), so nothing here may depend on the caller's environment. The installed
wrapper `bin/papercast-voice` sets PAPERCAST_VOICE_HOME and nothing else. Tests point
PAPERCAST_VOICE_CONFIG at their own file.

Measured values (GPU peak, words per minute, CPU worker count) live in voice.json, written by
install.sh and by `papercast-voice measure-*`, each next to the condition it was measured under.
"""
from __future__ import annotations

import copy
import json
import os

HOME_DEFAULT = "/home/leo/papercast/voice"

# Every value is documented where it is used; the reasons for the numbers are in README.md.
DEFAULTS: dict = {
    # Which GPU engine `engine: auto` uses. null = none installed: `auto` then voices on the CPU
    # at once, because there is nothing to wait for. Set by install.sh --gpu <name> after Leo's
    # pick (choice page 2026-09-26_papercast-voice).
    "gpu_engine": None,
    "cpu_engine": "kokoro",
    "engines": {
        "kokoro": {
            "kind": "cpu",
            "label": "kokoro",
            "python": "{home}/engines/kokoro/venv/bin/python",
            "worker": "kokoro_worker.py",
            "model_dir": "{home}/models/kokoro-82m",
            "files": ["config.json", "kokoro-v1_0.pth", "voices/af_heart.pt"],
            "voice": "af_heart",
            "speed": 1.0,
            # Longest chunk sent in one call. Kokoro runs one inference per 510 phoneme
            # tokens; about sixty words stays inside one, so it never splits mid-sentence.
            "max_words": 60,
            "sample_rate": 24000,
            "load_timeout_s": 300,
            "chunk_timeout_s": 300,
            # Measured by `papercast-voice measure-cpu`; null until then.
            "words_per_min": None,
            "sec_per_word": None,
            "measured": None,
        },
        # The GPU voice Leo picked (clip A, choice page 2026-09-26_papercast-voice): Breeze TTS 2,
        # set up exactly as the clip was made (samples/gen_breeze.py; README "GPU voice").
        "breeze": {
            "kind": "gpu",
            "label": "breeze-tts-2",
            "python": "{home}/engines/breeze/venv/bin/python",
            "worker": "breeze_worker.py",
            "code_dir": "{home}/engines/breeze/src",
            "model_dir": "{home}/models/breeze-tts-2",
            "files": ["config.json", "model-00001-of-00002.safetensors",
                      "model-00002-of-00002.safetensors", "tokenizer.json",
                      "audio_tokenizer/model.safetensors"],
            # Breeze has no preset voices: the narrator is described in words and generated from
            # a fixed seed. `voice` names that pair (it is part of the chunk cache key, so a
            # changed description never reuses old chunks).
            "instruction": ("A warm, clear woman in her thirties with a neutral American accent, "
                            "narrating a science podcast: measured pace, natural and engaged, "
                            "explaining a technical idea to a curious listener."),
            "seed": 42,
            "voice": "described-narrator-a-seed42",
            "speaker": "S0",
            "cfg_scale": 4.0,
            "repetition_penalty": 1.1,
            # The two CUDA-graph stages of clip A: RTF 0.83 at 9.5 GiB (all eager: 3.56, 8.1 GiB).
            "fast_stages": ["depth_decoder", "backbone_decode"],
            # 12.5 codec frames a second: 1,500 frames = 120 s, far above any chunk (a 75-word
            # chunk is about 36 s); prompt + frames must fit the backbone's 2,048 positions.
            "max_new_tokens": 1500,
            "max_seq_len": 2048,
            # Clip A's paragraph was 74 words; chunks stay at or under that size.
            "max_words": 75,
            # Length check per chunk (breeze_worker.length_problem), retried with the next seed.
            "expected_s_per_word": 0.48,
            "min_s_per_word": 0.15,
            "max_s_per_word": 1.2,
            "attempts": 3,
            "threads": 2,
            "sample_rate": 24000,
            # Model load plus CUDA-graph capture took 89 s for the sample (warm compiler caches).
            "load_timeout_s": 900,
            "chunk_timeout_s": 600,
            "env": {
                "CC": "{home}/engines/breeze/bin/cc",
                "TRITON_CACHE_DIR": "{home}/engines/breeze/cache/triton",
                "TORCHINDUCTOR_CACHE_DIR": "{home}/engines/breeze/cache/inductor",
            },
            # Written by install.sh (provisional, from the sample) and then by measure-gpu.
            "peak_mib": None,
            "words_per_min": None,
            "sec_per_word": None,
            "measured": None,
        },
    },
    # CPU fallback parallelism: `workers` processes, each with `threads` torch threads.
    # Chosen by measurement (README "CPU voice"); overwritten by measure-cpu --apply.
    "cpu": {"workers": 4, "threads": 2},
    "gpu": {
        "index": 0,
        # nvidia-smi is asked every poll_s while waiting (and between chunks while speaking).
        "poll_s": 10.0,
        # need_mib = the engine's measured peak_mib + headroom_mib (spec §2.9).
        "headroom_mib": 1024,
        # Admission also needs the card idle apart from us: device utilisation at or under
        # util_max_pct on stable_polls consecutive polls, so a running job is not slowed.
        "util_max_pct": 20,
        "stable_polls": 3,
        # While speaking: yield the GPU (stop, keep chunks, wait again) when another compute
        # process runs at >= yield_other_sm_pct SM for yield_polls checks in a row, or when
        # free memory falls under yield_free_floor_mib (someone else is growing into it).
        "yield_other_sm_pct": 20,
        "yield_polls": 3,
        "yield_free_floor_mib": 256,
        # CUDA out-of-memory during synthesis sends the job back to waiting; the max_ooms-th
        # in one run fails it (INTERFACE §10.4).
        "max_ooms": 3,
        "nvidia_smi_timeout_s": 15,
        # A waiting job counts the tickets ahead of it every line_poll_s (sched.py), and checks
        # every upgrade_check_s whether install.sh replaced the code (then it re-executes).
        "line_poll_s": 2.0,
        "upgrade_check_s": 10.0,
    },
    "audio": {
        # Podcast loudness: -16 LUFS integrated (within lufs_tolerance), true peak at most
        # -1 dBTP, both measured on the encoded file (ITU-R BS.1770). loudnorm aims the peak
        # codec_margin_db lower and the loudness codec_loudness_db higher, because the MP3
        # encoder moves both: measured on a full Kokoro episode at 96 kbit/s, +0.0 to +0.2 dB of
        # peak and -0.4 LU; then it steers on the encoded file (audio.normalise_encode).
        "lufs": -16.0,
        "lufs_tolerance": 0.5,
        "true_peak_db": -1.0,
        "codec_margin_db": 1.0,
        "codec_loudness_db": 0.4,
        "lra": 11.0,
        # MP3, CBR, mono (README "Output format" says why not Opus). 96, not 64: at 64 kbit/s
        # the encoder overshot the true peak by 1.8 to 2.0 dB on speech (to 0 dBTP), at 96 by
        # at most 0.2 dB (measured on a full-length Kokoro episode).
        "format": "mp3",
        "bitrate_kbps": 96,
        "sample_rate": 44100,
        # Silence, in seconds, placed between chunks after each chunk's own leading and
        # trailing silence is trimmed.
        "lead_in_s": 0.4,
        "gap_sentence_s": 0.30,
        "gap_paragraph_s": 0.75,
        "gap_before_heading_s": 1.2,
        "gap_after_heading_s": 0.6,
        "tail_s": 1.0,
        "trim_threshold_db": -45.0,
        "trim_pad_s": 0.06,
        "speak_headings": True,
    },
    "ffmpeg": "{home}/bin/ffmpeg",
    "nvidia_smi": "nvidia-smi",
    # Written at most this often by the heartbeat (INTERFACE §10.3: the runner calls a job
    # dead when its pid is gone and updated_at is older than 120 s).
    "heartbeat_s": 10.0,
    # Where the GPU voice may run (hosts.py): stibnite's own card (index gpu.index, its slot is
    # the lock below) and each idle GPU of the remote hosts, preferred in this order when more
    # than one is free at once.
    "host_order": ["stibnite", "bs1"],
    "hosts": {
        "stibnite": {"kind": "local", "dtype": "bf16"},
        # Leo's server boomerserver1, 8 x Quadro RTX 6000 (24 GB, Turing: no native bf16).
        # Measured 2026-09-27 (README "Measured"): fp16 fails on the first chunk (NaN
        # probabilities in sampling, a device-side assert), so fp32, which is clean. The engine
        # runs as leo (the ssh alias logs in as root) in the lean install at `home`: the Breeze
        # venv (requirements/breeze.txt), weights and upstream code only (README "Remote GPUs");
        # the worker script goes over with each job (hosts.Host.ensure_code).
        "bs1": {
            "kind": "ssh", "enabled": True, "ssh": "bs1", "run_as": "leo",
            # Card 7 is left out: Leo's own on-demand image generator and speech model load
            # there (their control services check for 15-16 GB free on it, and would refuse
            # while a voice held it). Cards 0-6 are his chat model's, also on demand: see
            # holdoff_s and README "Remote GPUs".
            "home": "/home/leo/papercast-voice", "gpus": [0, 1, 2, 3, 4, 5, 6],
            "dtype": "fp32", "cc": "/usr/bin/gcc", "nice": 10,
            # fp32 on one card, the 1,000-word excerpt of tests/fixtures/long_script.md plus the
            # sample paragraph: the engine process's GPU memory (nvidia-smi every 1 s) peaked at
            # 16,138 MiB (flat after load); admission adds gpu.headroom_mib. Synthesis 0.413 s
            # a word (real-time factor 1.15; stibnite's bf16: 0.83).
            "peak_mib": 16138, "sec_per_word": 0.413,
            "measured": "2026-09-27T13:02-13:10Z, Quadro RTX 6000, fp32, 25 chunks, 1,074 words",
            "connect_timeout_s": 10, "timeout_s": 20, "down_backoff_s": 60,
            "idle_exit_s": 180, "max_failures": 3,
            # A stranger's process appearing on any of `gpus` holds the whole host back this
            # long (hosts.py "yield"), so its owner's retry finds the cards free.
            "holdoff_s": 1800,
            "control_dir": "{home}/run/ssh",
        },
    },
    # The GPU slot lock is INTERFACE §10.4's `state/voice-gpu.lock`: derived from the job
    # directory (state/<id>/voice -> state/voice-gpu.lock) unless set here.
    "gpu_lock": None,
    # One CPU job at a time across all papers (the voice's own lock, under its home).
    "cpu_lock": "{home}/run/voice-cpu.lock",
    # The line of jobs waiting for a GPU slot (sched.py); remote slots' locks are run/slots/.
    "queue_dir": "{home}/run/queue",
    # A waiting job whose installed code changed re-executes itself (same pid, same place in
    # line), so an install reaches papers that are already waiting.
    "reexec_when_upgraded": True,
    # Minimum niceness of everything the voice runs (the runner already starts it at 10).
    "nice": 10,
}


def voice_home() -> str:
    return os.path.abspath(os.environ.get("PAPERCAST_VOICE_HOME") or HOME_DEFAULT)


def config_path() -> str:
    return os.environ.get("PAPERCAST_VOICE_CONFIG") or os.path.join(voice_home(), "voice.json")


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def _expand(obj, home: str):
    if isinstance(obj, str):
        return obj.replace("{home}", home)
    if isinstance(obj, dict):
        return {k: _expand(v, home) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_expand(v, home) for v in obj]
    return obj


def load(path: str | None = None) -> dict:
    """Defaults overlaid by the config file. A missing file is the defaults; a broken one raises."""
    path = path or config_path()
    over = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            over = json.load(fh)
    cfg = _merge(DEFAULTS, over)
    home = cfg.get("home") or voice_home()
    cfg["home"] = home
    cfg["config_file"] = path
    return _expand(cfg, home)


def save_measured(updates: dict, path: str | None = None) -> None:
    """Merge `updates` into the config file (not the defaults), atomically."""
    path = path or config_path()
    cur = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            cur = json.load(fh)
    new = _merge(cur, updates)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(new, fh, indent=2, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, path)


def engine_spec(cfg: dict, name: str) -> dict:
    spec = (cfg.get("engines") or {}).get(name)
    if not spec:
        raise KeyError(f"engine {name!r} is not configured in {cfg.get('config_file')}")
    spec = dict(spec)
    spec["name"] = name
    worker = spec.get("worker", "")
    if worker and not os.path.isabs(worker):
        spec["worker"] = os.path.join(os.path.dirname(os.path.abspath(__file__)), "engines", worker)
    return spec


def gpu_need_mib(cfg: dict) -> int | None:
    """Measured peak + headroom for the configured GPU engine, or None if there is none."""
    name = cfg.get("gpu_engine")
    if not name:
        return None
    spec = (cfg.get("engines") or {}).get(name) or {}
    peak = spec.get("peak_mib")
    if not peak:
        return None
    return int(peak) + int(cfg["gpu"]["headroom_mib"])
