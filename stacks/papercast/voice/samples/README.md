# papercast-voice: the three-voice sample for Leo (W2-papercast §2.9, step 1)

Before building the voice pipeline, Leo hears one paragraph in three voices and picks one:
choice page `~/lab/review/2026-09-26_papercast-voice.html`, answers land in
`~/lab/review/2026-09-26_papercast-voice.choices.json`. Nothing here is the pipeline.

## Candidates and why

Ranking source: Artificial Analysis Speech Arena, open-weights board, read 2026-09-26.

| clip | model | why | voice |
|---|---|---|---|
| A | Breeze TTS 2 (BreezeBlue, 3.5B, Aug 2026) | top open-weight model (Elo 1206, next open one 1119) and fits a 16 GB card | none preset, so a narrator described in words (`INSTRUCTION` in `gen_breeze.py`), seed 42, cfg 4 |
| B | Voxtral TTS (Mistral, 4B, Mar 2026) | next-best open model that fits (Elo 1079); preset voices keep one narrator across a long episode | `neutral_female` |
| C | Kokoro-82M v1.0 | the CPU fallback (Elo 1065) | `af_heart`, its top-graded voice |

Left out: Fish Audio S2 Pro (Elo 1119) needs about 24 GB in bf16. Step Audio EditX
(Elo 1095) states 12 GB, which leaves too little for anyone else. Everything else that
fits scores at or below Kokoro (Magpie 1063, Maya1 1043, Higgs V3 1035, Chatterbox 1023,
VibeVoice 1.5B 953), so it would not be better than the fallback.

All three narrators are female so the comparison is about the model. Kokoro's best
voice is female, and none of its male voices grade above C+.

## Measured (val-stibnite, RTX A4000 16 GB, driver 535 / CUDA 12.2, 2026-09-26)

Same 74-word paragraph (`paragraph.txt`, MatterGen classifier-free guidance, all
numbers/symbols/equation in words). RTF = generation seconds / audio seconds, second of
two runs, model already loaded. GPU peak = NVML per-process memory summed over the
generator's process tree, polled every 50 ms (`gpumem.py`): what other users lose,
including the CUDA context and the allocator's reserve. All jobs `nice -n 10`. The GPU
held only Leo's own 158 MiB `facecv` process throughout.

| run | audio | RTF | 20-min episode | GPU peak | notes |
|---|---|---|---|---|---|
| Breeze, two CUDA-graph stages (clip A) | 35.7 s | 0.83 | ~17 min | 9,696 MiB (9.5 GiB) | `--fast depth_decoder,backbone_decode`; load + graph warm-up 89 s |
| Breeze, all eager | 36.6 s | 3.56 | ~71 min | 8,330 MiB (8.1 GiB) | torch max allocated 7,987 MiB; upstream says 7.7 GiB |
| Voxtral via vLLM-Omni (clip B) | 33.1 s | 0.44 | ~9 min | 10,388 MiB (10.1 GiB) | memory is reserved at start-up (vLLM preallocates), not only while speaking; server start 68 s |
| Kokoro, CPU (clip C) | 26.9 s | 0.22 | ~4 min | 0 | 8 torch threads on the Xeon Gold 6226R, one process, no chunk parallelism |

Upstream `--fast-all` for Breeze (all five stages) is quoted at 14.4 GiB. Not run: it
would leave no headroom on a shared card.

Checks on the clips:
- **Loudness.** One linear gain per clip to -23 LUFS, with no compression or limiting
  (`finish.py`). The decoded MP3s measure -23.3 LUFS each (ffmpeg `ebur128`), and no clip
  clips. At -18 LUFS they clipped by up to 3 dB. Raw Voxtral output is very quiet
  (-37.7 LUFS) and gets +14.7 dB.
- **Content.** Whisper `small.en` on CPU (`asr_check.py`, output in `asr.json`) finds
  every clip reads the whole paragraph, with nothing skipped or added. The remaining
  word errors are how Whisper writes things ("200", "GPa", "plane" for "plain"), plus one
  real difference: both Breeze runs are heard as "*If* gamma at one" where the text says
  "With".

## Where things live (outside the repo)

`~/papercast-voice-cache/` (about 27 GB, safe to delete):
- `venvs/breeze` holds torch 2.9.1+cu128 and the pins from `src/breeze-tts/requirements.txt`
  (upstream commit `008f769`).
- `venvs/voxtral` holds vllm 0.18.0, vllm-omni 0.18.0, torch 2.10.0+cu128,
  prometheus-fastapi-instrumentator 8.1.0 and ziglang.
- `venvs/kokoro` holds torch CPU, kokoro 0.9.4 and en_core_web_sm 3.8.0.
- `venvs/tools` holds nvidia-ml-py, psutil, pyloudnorm, soundfile and imageio-ffmpeg
  (a static ffmpeg 7.0.2 with libmp3lame).
- Weights: `models/breeze-tts-2` (6.4 GB), `hf/` for Voxtral 7.2 GB and Kokoro
  (`HF_HOME`), and `whisper/`.
- `out/` holds the clips, the per-run JSON metrics and the server logs.
- `bin/cc` is `cc-zig.sh` (see below).

### Version traps found here
- **Current vLLM will not run here.** vLLM ≥ 0.20 / vllm-omni ≥ 0.20 need torch ≥ 2.11,
  which is built for CUDA 13, and stibnite's driver is 535 (CUDA 12.2). Upgrading the
  driver is system-wide on a shared machine. So the pins are vllm 0.18.0 with vllm-omni
  0.18.0; vllm 0.19 breaks vllm-omni 0.18 (`vllm.inputs.data` is gone).
- **Use the `vllm-omni` entry point.** The `vllm` script belongs to vllm itself and does
  not know `--omni`.
- **Upgrade the Prometheus middleware.** prometheus-fastapi-instrumentator 7.1.0 makes
  every route return 500 with fastapi 0.141 (`_IncludedRouter`). 8.1.0 fixes it.
- **There is no C compiler on stibnite.** Triton and inductor build C stubs, so
  `cc-zig.sh` wraps zig's clang (from pip `ziglang`) and rewrites Triton's
  `-l:libcuda.so.1`, which zig rejects. Both Voxtral and fast-mode Breeze need `CC`
  pointing at it.
- **Voxtral memory settings.** The stage config `voxtral_tts_16gb.yaml` is upstream's
  with `max_num_seqs` 1 and memory fractions 0.57 and 0.08 (upstream 0.8 and 0.1). At
  0.55 there is not enough key-value cache for its 4,096-token context (0.28 GiB free,
  0.41 needed).

## Commands (all run 2026-09-26, in this order)

```bash
C=~/papercast-voice-cache; S=/home/leo/NAS_setup/stacks/papercast/voice/samples
export HF_HOME=$C/hf UV_CACHE_DIR=$C/uv-cache
# setup                                                             [run]
git clone https://github.com/breezeblue-ai/breeze-tts.git $C/src/breeze-tts   # 008f769
uv venv -p 3.11 $C/venvs/breeze  && VIRTUAL_ENV=$C/venvs/breeze  uv pip install -r $C/src/breeze-tts/requirements.txt "huggingface_hub[hf_xet]"
uv venv -p 3.12 $C/venvs/voxtral && VIRTUAL_ENV=$C/venvs/voxtral uv pip install "vllm==0.18.0" "vllm-omni==0.18.0" \
  "mistral_common[audio]>=1.10.0" "huggingface_hub[hf_xet]" "prometheus-fastapi-instrumentator==8.1.0" ziglang
uv venv -p 3.11 $C/venvs/kokoro  && VIRTUAL_ENV=$C/venvs/kokoro  uv pip install --index-url https://download.pytorch.org/whl/cpu \
  --extra-index-url https://pypi.org/simple --index-strategy unsafe-best-match torch "kokoro>=0.9.4" soundfile \
  "en_core_web_sm @ https://github.com/explosion/spacy-models/releases/download/en_core_web_sm-3.8.0/en_core_web_sm-3.8.0-py3-none-any.whl"
uv venv -p 3.11 $C/venvs/tools   && VIRTUAL_ENV=$C/venvs/tools   uv pip install nvidia-ml-py psutil pyloudnorm soundfile numpy scipy imageio-ffmpeg
$C/venvs/breeze/bin/hf download BreezeBlue/Breeze-TTS-2 --local-dir $C/models/breeze-tts-2
$C/venvs/voxtral/bin/hf download mistralai/Voxtral-4B-TTS-2603
mkdir -p $C/bin $C/out && cp $S/cc-zig.sh $C/bin/cc && chmod +x $C/bin/cc
cd $C/out
nvidia-smi                                   # run before each GPU job; gpumem.py also refuses if too little is free
# A (eager)                                                         [run]
nice -n 10 ../venvs/tools/bin/python $S/gpumem.py --out breeze.gpumem.json --need-mib 10000 -- \
  nice -n 10 ../venvs/breeze/bin/python $S/gen_breeze.py --code ../src/breeze-tts \
  --model ../models/breeze-tts-2 --text-file $S/paragraph.txt --out breeze.wav --metrics breeze.json
# A (two fast stages; this is the clip on the page)                 [run]
CC=$C/bin/cc TRITON_CACHE_DIR=$C/triton-cache TORCHINDUCTOR_CACHE_DIR=$C/inductor-cache \
nice -n 10 ../venvs/tools/bin/python $S/gpumem.py --out breeze_fast.gpumem.json --need-mib 14000 -- \
  nice -n 10 ../venvs/breeze/bin/python $S/gen_breeze.py --code ../src/breeze-tts \
  --model ../models/breeze-tts-2 --text-file $S/paragraph.txt --out breeze_fast.wav \
  --metrics breeze_fast.json --fast depth_decoder,backbone_decode
# B                                                                 [run]
nice -n 10 ../venvs/tools/bin/python $S/gpumem.py --out voxtral.gpumem.json --need-mib 12000 -- \
  nice -n 10 ../venvs/voxtral/bin/python $S/gen_voxtral.py --venv ../venvs/voxtral \
  --stage-config $S/voxtral_tts_16gb.yaml --text-file $S/paragraph.txt --out voxtral.wav \
  --metrics voxtral.json --log voxtral.server.log
# C                                                                 [run]
nice -n 10 ../venvs/kokoro/bin/python $S/gen_kokoro.py --text-file $S/paragraph.txt \
  --out kokoro.wav --metrics kokoro.json --threads 8
# checks, loudness, page                                            [run]
nice -n 10 ../venvs/voxtral/bin/python $S/asr_check.py --dir . --text-file $S/paragraph.txt breeze breeze_fast voxtral kokoro
nice -n 10 ../venvs/tools/bin/python $S/finish.py --dir . breeze breeze_fast voxtral kokoro
../venvs/tools/bin/python $S/build_page.py --dir . --text-file $S/paragraph.txt \
  --template ~/lab/review/_template.html --page ~/lab/review/2026-09-26_papercast-voice.html
```

The page is 774 KB: three MP3s at 48 kbit/s mono, base64-inlined, with the template's
save script unchanged.
