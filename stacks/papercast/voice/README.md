# papercast-voice — the narrator (W2-papercast Part C)

Turns an episode's `script.md` into one audio file: `out/episode.mp3`, loudness-normalised,
tagged for Navidrome (title = paper title, album "Papers", artist = first author, date = the
day it was made). It runs on stibnite as `leo`, started by papercast-runner once per episode
(INTERFACE.md §10). It waits for free GPU memory by default, voices on the CPU with Kokoro when
Leo presses **Use CPU voice (Kokoro)**, and never slows or crashes anyone else's GPU job.

**State today (2026-09-26):** built, tested, installed on stibnite. The GPU voice is **Breeze
TTS 2** (Leo's pick, clip A on the choice page), measured on a full-length episode: peak
**9,690 MiB** of GPU memory, so a paper waits for **10,714 MiB** free (peak + 1 GiB) and an idle
card; it speaks at 0.83 × real time (a twenty-minute episode takes about seventeen minutes).
Kokoro stays the CPU voice behind **Use CPU voice (Kokoro)** (section "GPU voice").

## How it fits

```
runner: script.md passes its check
  -> state/<id>/voice/{script.md, job.json}          (a snapshot; the agent cannot touch it)
  -> setsid nice -n 10 papercast-voice run state/<id>/voice
voice:  preparing -> waiting-for-gpu -> speaking -> encoding -> done | failed
        writes status.json (every change, and every 10 s), reads use-cpu / cancel
  -> out/episode.mp3 + status.json output {sha256, duration_s, loudness_lufs, ...}
runner: verifies, exposes it as audio/episode.mp3; the NAS publishes it to tank/music/podcast/papers
```

The runner's commands are `papercast-voice info` (one JSON line, under 2 s, never touches the GPU)
and `papercast-voice run <dir>`. Everything that must survive a restart is on disk in the job
directory: each finished chunk is a WAV under `chunks/`, and a re-run on the same directory
(Retry, or the runner's automatic restart after a reboot) never voices a finished chunk again.

## What it does, and why

**Chunks for the ear** (`textprep.py`). The script is cut into chunks of at most about sixty
words for Kokoro (it infers 510 phoneme tokens at a time) and 75 for Breeze (clip A's paragraph
was 74 words; a chunk's prompt plus its audio frames must fit Breeze's 2,048 positions). A chunk ends at a sentence end; only a sentence over the limit is cut, at a comma,
semicolon, colon or dash, and only a clause over the limit is cut between words. A `#` heading
is its own chunk, said as a section title. After each chunk comes a pause chosen by what follows:
0.3 s before the next sentence, 0.75 s before a new paragraph, 1.2 s before a heading, 0.6 s after
one (each chunk's own leading and trailing silence is trimmed first). Chunks are also the unit of
progress, of resuming, of the CPU voice's parallelism, and of handing the GPU back.

**Nothing unspeakable reaches an engine.** The runner validates `script.md` first
(INTERFACE §6). The voice checks again, right before each chunk goes to an engine, with the
runner's own character set (a test pins the two together): no digits of any kind, no LaTeX, no
math symbols, no Greek letters, no Markdown but headings. A failure is `script_invalid` with
the reason; the voice never repairs text.

**Waiting for the GPU** (`gpu.py`, spec §2.9). While `waiting-for-gpu` the voice asks
`nvidia-smi` every 10 s (read only) and starts only when, on 3 polls in a row:
1. `nvidia-smi` answers (not being able to check is not the same as free);
2. free memory ≥ the GPU voice's **measured peak + 1 GiB headroom**;
3. the card is idle apart from graphics: utilisation ≤ 20 %. Free memory says nothing about
   someone's job computing on the card, and starting beside it would slow it.

It also needs the one GPU slot, `state/voice-gpu.lock` (INTERFACE §10.4), held from admission
to the end of GPU synthesis, so two waiting papers never start on the same free memory.
While speaking it checks between chunks, at most every 10 s, and **gives the GPU back** (stops its
engine, keeps its chunks, waits again) when another compute process runs at ≥ 20 % SM on 3
checks in a row (`nvidia-smi pmon`, its own processes excluded), or free memory drops under
256 MiB. A CUDA out-of-memory also sends it back to waiting with its chunks; the third in one
run is `failed/gpu_oom`. It never signals a process it did not start. `status.json` carries,
besides INTERFACE §10.3's fields, `wait_reason` and `wait_text`, the sentence for the page
("waiting for GPU: 3.0 GB free, needs 10.5 GB", or "memory is free but the card is busy …",
or "another paper is being voiced on it"), and `note`, the last event worth showing.

**Use CPU voice (Kokoro).** The runner creates `use-cpu`; the voice checks it every half second
while waiting and switches at once. It is honoured whenever the job is in `waiting-for-gpu`,
including after it went back there (out of memory, or GPU handed back), and the whole episode is
then voiced by Kokoro: never a mix of two voices. `cancel` (dismiss) stops the job within a
second, engines included (`failed/cancelled`, exit 4).

**CPU voice parallelism.** Kokoro-82M runs as **4 worker processes × 2 torch threads = 8
threads**, one CPU job at a time across all papers (`run/voice-cpu.lock`). Measured (below): of
the splits tried, 4×2 is the fastest for an 8-thread budget (7.6× faster than real time) and the
most economical but one per CPU-second (0.95 CPU-seconds per second of audio). Going to 16 threads
(4×4, 11.6×) would save about 50 s on a twenty-minute episode for twice the share of a shared
machine. Eight of 64 hardware threads leaves most of stibnite for others; everything runs at
`nice 10` (the voice renices itself if started without it). Memory is the cost: on a full
episode each worker peaked at 2.8–3.7 GB and the whole job at 11.8 GB (5 % of stibnite's RAM).

**Output format: MP3, CBR 96 kbit/s, mono, 44.1 kHz, ID3v2.4.** Not Opus, although Opus is half
the size at the same quality for speech: MP3 plays natively in every browser (the page's player,
including Safari on Leo's iPads) and every phone app, and Navidrome serves it untouched when
Symfonium on his Android phone asks for the original (on mobile data Symfonium asks for Opus
128k and Navidrome transcodes, as it does for all his music: `stacks/media/README.md`). Constant
bitrate gives exact seeking and duration, which matters for a player that remembers the
position. 96 and not 64 kbit/s because of peaks: on a full-length episode the encoder overshot
the true peak by 1.8–2.0 dB at 64 kbit/s (to 0 dBTP, the edge of clipping) and by at most 0.2 dB at
96. Size is about 0.7 MB a minute, 12 MB for a seventeen-minute episode. ID3v2.4 keeps the full date
(`TDRC 2026-09-26`) and UTF-8; `albumartist` "Papers" keeps every episode in one album
(INTERFACE §10.2). A ReplayGain track gain (reference -18 LUFS) is added, so a player in
ReplayGain mode levels episodes with music.

**Loudness: -16 LUFS integrated, true peak ≤ -1 dBTP**, both on the encoded file (ITU-R BS.1770,
the usual podcast level for phone listening). ffmpeg's two-pass `loudnorm` does it. Speech peaks
about 21 dB above its loudness, so the limiter has to work and takes loudness with it (one pass
landed 0.5 LU short for Kokoro and Breeze, 1.2 LU for Voxtral), and the MP3 encoder then lowers
loudness by 0.4 LU and raises peaks by up to 0.2 dB (measured at 96 kbit/s). So loudnorm starts
aimed 0.4 LU high and 1 dB under the peak limit, the *encoded* file is measured after each attempt,
and the targets are corrected until it is within 0.5 LU and under the limit (at most four
attempts; measured: one attempt for a full Kokoro episode and for Kokoro's and Breeze's samples,
two for Voxtral's). Then it verifies with instruments other than the ones that did the work:
loudness by pyloudnorm (an independent BS.1770 implementation) on the decoded file, true peak by
ffmpeg's ebur128, duration by decoding, the stream's codec/rate/channels/bitrate, and every tag
read back by two readers that share no code (mutagen and ffmpeg). More than 1 LU off, a peak
over -1 dBTP, or a tag that does not read back is `encode_failed`, not a shipped episode; the
chunks stay, so Retry only re-encodes (tested).

**One GPU engine is one adapter.** An engine is a worker script speaking a small JSON-lines
protocol (`engines/_proto.py`: ready / synth / done / error with an `oom` flag) plus a block in
`voice.json`: its venv's python, model files, `max_words`, `peak_mib`, words per minute. The
worker runs in the engine's own venv, never the orchestrator's, and dies with the job even if the
job is SIGKILLed (it watches its parent; see RISKS for why not `PR_SET_PDEATHSIG`).

**The GPU voice: Breeze TTS 2** (`engines/breeze_worker.py`, block `engines.breeze` in
`config.py`). Set up exactly as clip A was made (`samples/gen_breeze.py`; a test reads that file
and pins the two together): no reference audio, the narrator described in words ("A warm, clear
woman in her thirties with a neutral American accent, narrating a science podcast …"),
classifier-free guidance 4, seed 42 for every chunk, bf16, eager attention, and two of upstream's
five fast stages as CUDA graphs (`depth_decoder`, `backbone_decode`: 0.83 × real time at 9.5 GiB,
against 3.56 × all eager at 8.1 GiB; all five are quoted at 14.4 GiB, which leaves a shared card
no room). The model runs inside the worker process: no server, no port. Each chunk's length is
checked against its word count (0.15–1.2 s a word, plus 2 s): a chunk that stops early or runs on
is generated again with seed 43, then 44; if none passes, the nearest is kept and logged
(`metrics.json` `retried_chunks`). A CUDA out-of-memory, at load or mid-chunk, ends the worker
with `oom`, so the job frees the card and waits again (§10.4). Triton needs a C compiler and
stibnite has none: `engines/breeze/bin/cc` is zig's clang from the venv's `ziglang` wheel.

**No systemd unit** (a deliberate deviation from the brief; RISKS.md says why). The voice is a
command, not a service: the runner's user unit supervises it, re-adopts it after its own restart,
and restarts an interrupted voice step once after a reboot (INTERFACE §3).

## Measured (stibnite, 2026-09-26)

| what | result | how, and conditions |
|---|---|---|
| Kokoro CPU split | 4×2: 7.6× real time, 7.3 cores busy, 0.95 CPU-s per audio-s, 7.0 GB; 1×8 6.3×; 2×4 6.9×; 8×1 7.5×; 4×4 11.6× on 14.6 cores; 8×2 10.8× | `measure-cpu` on 24 chunks (976 words, 330 s of audio) of `tests/fixtures/long_script.md`, nice 10, load average 4–10 (mostly the sweep itself). `/home/leo/papercast/voice/measure/cpu-2026-09-26.json` |
| full-length Kokoro episode | 2,934 words → 1,024 s (17.1 min) of audio in 200 s: synthesis 137 s (7.5× real time, with 7 s of model loading), encoding 62 s; -16.24 LUFS, -2.0 dBTP, 12.3 MB; **171.9 words per minute** with pauses; Whisper small.en heard 2,940 of 2,940 words, 0.5 % word error rate, no digits | `tests/fixtures/long_script.md` through the runner's `launch.sh`, nice 10, load average 4–6; whole-tree memory sampled every 1 s: **peak 11.8 GB** (workers 2.8–3.7 GB each). `/home/leo/papercast/voice/measure/kokoro-episode-2026-09-26.*` |
| loudness | the three sample voices: -16.2 to -16.4 LUFS, true peak -2.2 to -2.4 dBTP, one or two attempts | `normalise_encode` on the raw clips in `~/papercast-voice-cache/out/` |
| MP3 overshoot | true peak +1.8 to +2.0 dB at 64 kbit/s, +0.0 to +0.2 dB at 96; loudness -0.4 to -0.5 LU at both | the full episode's audio, loudnorm to -16 LUFS / -2 and -2.5 dBTP, then encoded |
| short episode, real | 135 words → 50.2 s in 24.9 s, -16.21 LUFS, -2.2 dBTP, Whisper word error rate 0.7 % | `test_real.py` |
| orchestrator | 1.4 GB peak, during the final check (decoding the whole episode) | `metrics.json` of the full run |
| full-length Breeze episode (GPU) | 2,934 words → 1,117 s (18.6 min) of audio in 1,071 s: admission 20 s, load and CUDA-graph capture 46 s, synthesis 928 s (**real-time factor 0.831**), encoding 66 s; **peak 9,690 MiB** of GPU memory (reached 97 s in, flat after), device peak 10,358 MiB; **157.6 words per minute** with pauses; no chunk needed a second seed; Whisper small.en (CPU) heard 2,934 of 2,934 words, 12 errors, **0.4 % word error rate**, no digits; median pitch per chunk 157–240 Hz across the 60 paragraph chunks (median 188, 10th–90th percentile 169–213): one female narrator throughout, but not a fixed timbre (RISKS) | `measure-gpu --script tests/fixtures/long_script.md --need-mib 10720 --apply` (the sample's peak + 1 GiB as the provisional threshold), nice 10, load average 2, the card holding only Leo's 158 MiB `facecv` process and the desktop; NVML every 50 ms, summed over every process the job started; worker RSS 5.7 GB. `/home/leo/papercast/voice/measure/breeze-episode-2026-09-26.*` |
| short Breeze episode, real | 135 words → 56.5 s of audio in 126 s (20 s admission, about 46 s load), -16.44 LUFS, -2.4 dBTP, Whisper word error rate 2.2 % ("plane" for "plain", "It turns" for "Turn") | `test_real_gpu.py`, through the runner's `launch.sh` and GPU slot |

## Install (on stibnite, as leo, no sudo)

```bash
cd /home/leo/NAS_setup/stacks/papercast/voice
bash install.sh --gpu breeze   # [run 2026-09-26] ~45 s with the uv cache; prints PAPERCAST_VOICE_CMD
```
It refuses to run while a voice job is working or holds a slot (`papercast_voice/busy.py`): it
replaces files a job's engines load.
It installs into `/home/leo/papercast/voice/` (0700): `app/` (a copy of `papercast_voice/`, with
the git commit in `VERSION`), `venv/` (orchestrator: numpy, soundfile, pyloudnorm, mutagen,
static ffmpeg 7.0.2 from `imageio-ffmpeg`), `engines/kokoro/venv/` (the exact pins that made the
samples, `requirements/kokoro.txt`), `models/kokoro-82m/` (checksums verified), `bin/`,
`voice.json` (measurements; kept across re-installs). With `--gpu breeze` (`engines/breeze/install.sh`):
`engines/breeze/venv/` (`requirements/breeze.txt`: the sample's exact pins plus `ziglang`),
`engines/breeze/src/` (breeze-tts at upstream commit 008f769), `engines/breeze/bin/cc`,
compiler caches, and `models/breeze-tts-2/` (revision 3e28c51, every file checksummed; hard links
to the sample step's copy, so no extra space); it sets `gpu_engine: breeze` in `voice.json` and,
until `measure-gpu` has run, the sample's peak as a provisional threshold. Re-run it after every code change: the
installed copy does not follow the repo by itself.

Then the runner needs, in `/home/leo/papercast/runner.env` (Part B's file):
```
PAPERCAST_VOICE_CMD=/home/leo/papercast/voice/bin/papercast-voice
PAPERCAST_WPM=158        # Breeze's measured rate, pauses included (info "words_per_min")
```

## First run and verify

```bash
V=/home/leo/papercast/voice
$V/bin/papercast-voice info                       # [run] "installed": true, "gpu": {"model": "breeze-tts-2", "need_mib": 10714, ...}
$V/bin/papercast-voice check some/script.md       # [run] chunk counts, or the reason it is refused
cd /home/leo/NAS_setup/stacks/papercast/voice
$V/venv/bin/python -m unittest discover -s tests  # [run] 74 tests (2 real ones skipped), about 2 min
PAPERCAST_VOICE_REAL=1 $V/venv/bin/python -m unittest discover -s tests -p test_real.py
                                                  # [run] real Kokoro through the runner's launch.sh
PAPERCAST_VOICE_REAL_GPU=1 $V/venv/bin/python -m unittest discover -s tests -p test_real_gpu.py
                                                  # [run] real Breeze; skips if the GPU is not free
```
A single episode by hand (what the runner does):
```bash
D=<a directory with script.md and job.json, INTERFACE §10.2>
setsid nice -n 10 $V/bin/papercast-voice run "$D" >> "$D/voice.log" 2>&1 &   # [run]
cat "$D/status.json"                                                          # phase, chunks, eta
```
Look at `$D/voice.log` (every event, with UTC times) and `$D/metrics.json` (timings, per-worker
memory, loudness attempts) when something is off.

## Stop it

There is no daemon to stop. To stop one episode (clean: status `failed/cancelled`, engines
stopped within a second):
```bash
touch /home/leo/papercast/state/<id>/voice/cancel                  # [run in tests]
```
To kill it hard (the runner's own record of the group leader is `voice/pid`):
```bash
kill -TERM -- -"$(cat /home/leo/papercast/state/<id>/voice/pid)"    # [run: 6 processes gone in 3 s, chunks kept]
```
Everything the voice runs is `leo`'s and a descendant of the job; nothing else is ever signalled.

## Back out

```bash
# 1. runner.env: remove PAPERCAST_VOICE_CMD (the runner then fails speaking with voice_missing)
rm -rf /home/leo/papercast/voice            # [unrun] the install; job directories are untouched
```
Nothing else was changed: no system files, no user units, no crontab. Episodes already made are
on the NAS.

## GPU voice

Leo picked Breeze TTS 2 (`~/lab/review/2026-09-26_papercast-voice.choices.json`). Install and
measure (each `[run 2026-09-26]`):
```bash
bash install.sh --gpu breeze
nvidia-smi                        # measure only with room: the job waits for it anyway
$V/bin/papercast-voice measure-gpu --script tests/fixtures/long_script.md --need-mib 10720 --apply
```
`measure-gpu` runs a full episode as a real job, in the runner's GPU slot (`state/voice-gpu.lock`,
so it never runs beside a real episode), samples NVML every 50 ms over every process the job
starts, and stores the peak, words per minute and seconds per word; admission then uses peak +
1 GiB. Re-measure after any change to the model, its fast stages or `max_words`. Voxtral, the
other candidate, was not built.

## Files

| path | what |
|---|---|
| `papercast_voice/cli.py` | `info`, `run`, `check`, `measure-*` |
| `papercast_voice/job.py` | one episode: states, controls, resume, GPU and CPU passes, encode |
| `papercast_voice/textprep.py` | chunking and the speakable-text guard |
| `papercast_voice/gpu.py` | nvidia-smi readings, admission rule, give-back rule |
| `papercast_voice/workers.py`, `engines/` | engine processes and their protocol; the Kokoro and Breeze workers |
| `engines/breeze/` | the Breeze install step and its C compiler wrapper |
| `papercast_voice/busy.py` | install.sh's check that no voice job is running |
| `papercast_voice/audio.py`, `tags.py` | join, loudness, MP3, verification; ID3 |
| `papercast_voice/measure.py` | the measurements above |
| `tests/` | unit and job tests with fake engines (the Breeze worker with its model faked) and a fake `nvidia-smi`; `test_real.py`, `test_real_gpu.py` |
| `samples/` | the earlier three-voice choice for Leo (not the pipeline) |
