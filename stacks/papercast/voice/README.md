# papercast-voice — the narrator (W2-papercast Part C)

Turns an episode's `script.md` into one audio file: `out/episode.mp3`, loudness-normalised,
tagged for Navidrome (title = paper title, album "Papers", artist = first author, date = the
day it was made). It runs on stibnite as `leo`, started by papercast-runner once per episode
(INTERFACE.md §10). It waits for free GPU memory by default, voices on the CPU with Kokoro when
Leo presses **Use CPU voice (Kokoro)**, and never slows or crashes anyone else's GPU job.

**State today (2026-09-27):** built, tested, installed on stibnite. The GPU voice is **Breeze
TTS 2** (Leo's pick, clip A on the choice page), measured on a full-length episode: peak
**9,690 MiB** of GPU memory, so a paper waits for **10,714 MiB** free (peak + 1 GiB) and an idle
card; it speaks at 0.83 × real time (a twenty-minute episode takes about seventeen minutes).
Kokoro stays the CPU voice behind **Use CPU voice (Kokoro)** (section "GPU voice").
Since 1.1 the same voice (same narrator description and seed) also runs on the idle GPUs of
Leo's server **bs1** (Quadro RTX 6000, fp32: 16,138 MiB, 1.15 × real time), one paper per GPU,
papers taking GPUs in the order they were handed over (section "GPU slots").

## How it fits

```
runner: script.md passes its check
  -> state/<id>/voice/{script.md, job.json}          (a snapshot; the agent cannot touch it)
  -> setsid nice -n 10 papercast-voice run state/<id>/voice
voice:  preparing -> waiting-for-gpu -> speaking -> encoding -> done | failed
        writes status.json (every change, and every 10 s), reads use-cpu / cancel
  -> out/episode.mp3 + status.json output {sha256, duration_s, loudness_lufs, ...}
     + out/timings.json (when each sentence is spoken; output.timings names it)
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

**GPU slots: stibnite and bs1** (`hosts.py`, `sched.py`, 1.1). A *slot* is one GPU that one
paper may hold: stibnite's card (its lock is still `state/voice-gpu.lock`, and the rule above
still decides when it is free) and each bs1 GPU in `hosts.bs1.gpus` (a lock under `run/slots/`).
- **The line.** A waiting paper holds a ticket in `run/queue/`, named by when it was handed over
  (job.json's time; a run that continues an interrupted one keeps the time it had, recorded as
  `queued_at` in status.json; a Retry joins at the back). Only the oldest waiting paper looks
  at the GPUs (one nvidia-smi, one ssh per poll): it takes the first free slot, stibnite's first
  when both are, and the next paper moves up within two seconds. The others only count the
  papers ahead of them ("waiting for a GPU: 11 papers ahead of it in line"), so 260 waiting
  papers cost what one does. A paper that comes back from a GPU it lost keeps its place.
- **A bs1 GPU is taken** only when no compute process at all is on it and it has the measured
  peak + 1 GiB free, checked right before it is taken and again right before the first chunk.
- **The engine runs on bs1**, as leo (the ssh alias logs in as root: `runuser -u leo`), niced 10,
  pinned with `CUDA_VISIBLE_DEVICES`, speaking the engine protocol over the ssh session. The
  chunks come back in the replies (base64 WAV) and are joined, normalised and tagged on
  stibnite, so **no job file is ever written on bs1**: nothing to copy there or delete after,
  and a lost connection costs at most the chunk being spoken. (A deviation from "copy the script
  and job.json there, copy the MP3 back": running the whole job there needs the orchestrator's
  venv and ffmpeg on bs1, and its disk is full.) The worker script goes over with the job,
  content-addressed (`app/v-<hash>/`, 17 KB, newest three kept), so bs1 always runs the code
  of the orchestrator talking to it; the engine ends itself when its session closes or after
  180 s without a request.
- **It gives bs1 back between chunks** when a process that was not there when it started, and
  is not one of our engines (told apart by executable: bs1's voice venv python), appears on
  *any* of the configured bs1 GPUs, not only its own: Leo's chat model spans cards 0-6, and a
  card held by the voice makes it fail. Then every voice job on bs1 yields and none starts for
  30 minutes (`run/holdoff-bs1.json`), so its owner's retry finds the cards free. Also when free
  memory on its GPU falls under 256 MiB.
- **bs1 unreachable**: skipped for 60 s at a time; papers keep using stibnite. A bs1 engine that
  fails (lost connection, crash, out of memory) sends the paper back to its place in line with
  its chunks; after three failures on bs1 in one run it waits for stibnite only.
- **The page** says where it speaks: `note` "Speaking on bs1 GPU 3, about 12 min to go." (the
  page shows it under "Speaking N%"); waiting, `wait_text` names each host's state.

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
and pins the two together): the narrator described in words ("A warm, clear
woman in her thirties with a neutral American accent, narrating a science podcast …"),
classifier-free guidance 4, seed 42, bf16, eager attention, and two of upstream's
five fast stages as CUDA graphs (`depth_decoder`, `backbone_decode`: 0.83 × real time at 9.5 GiB,
against 3.56 × all eager at 8.1 GiB; all five are quoted at 14.4 GiB, which leaves a shared card
no room). The model runs inside the worker process: no server, no port. Each chunk's length is
checked against its word count (0.15–1.2 s a word, plus 2 s): a chunk that stops early or runs on
is generated again with seed 43, then 44; if none passes, the nearest is kept and logged
(`metrics.json` `retried_chunks`). A CUDA out-of-memory, at load or mid-chunk, ends the worker
with `oom`, so the job frees the card and waits again (§10.4). Triton needs a C compiler and
stibnite has none: `engines/breeze/bin/cc` is zig's clang from the venv's `ziglang` wheel.

**One narrator per voice** (1.2, `refs.py`). Up to 1.1 every chunk was voice-designed on its own,
and voice design draws a speaker for the text it is given: every chunk (two to four sentences)
was a different narrator of the same description. Leo heard it ("why it seem to switch voices
every other sentence?"), and a speaker-embedding model measures it (section "Measured": the
default voice's neighbouring chunks 0.38 alike, the halves of one chunk 0.83). So a
voice is now designed once: the engine spec's `reference_text` (the paragraph papercast-group's
samples and custom-voice previews say, so this is the very clip people heard and picked),
voice-designed from the voice's instruction and seed like a chunk was, with the same length check
and retries. The job keeps it under `engines.breeze.references_dir` (`voices/breeze/<voice>-<recipe
id>.wav` and `.json`; the recipe id hashes model, voice, instruction, seed, speaker, CFG and the
paragraph, so a changed description never finds an old clip) and every chunk of every episode in
that voice is then voiced from it: upstream's voice clone (`ref_clone_tata`: the clip and its
transcript before the text, no instruction, no CFG, as upstream's README says a clone runs). The
first job of a voice designs it (about one chunk's time, on whichever GPU it got; the first clip
kept wins if two jobs design at once), every later job, on any host, sends the kept clip to its
engine. A chunk whose reply does not name that clip is not kept; `chunks/<key>/reference.json`
names the clip a job's chunks came from, and chunks from another clip are voiced again. The chunk
key carries the recipe id (`-r<10 hex>`), so no chunk voice-designed by 1.1 is ever reused.
`output.reference` is the clip's sha256. The CPU voice (Kokoro) speaks from a fixed voice file and
needs none of this; an episode is never a mix of the two (Use CPU voice voices every chunk again).

**No systemd unit** (a deliberate deviation from the brief; RISKS.md says why). The voice is a
command, not a service: the runner's user unit supervises it, re-adopts it after its own restart,
and restarts an interrupted voice step once after a reboot (INTERFACE §3).

## Voices and timings (the group's copy)

**job.json `voice`** (optional): which narrator, for one engine, e.g. `{"engine": "breeze",
"voice": "preset-warm-male-s42", "instruction": "Adult male, mid-30s, ...", "seed": 42}` or
`{"engine": "kokoro", "voice": "af_heart"}` (with `"engine": "cpu"` in job.json). It may set only
`voice`, `instruction`, `seed` and `speed` of that engine's spec; `voice` is the chunk cache key
(`chunks/<engine>-<voice>-...`) and comes back as `output.voice`. A Breeze voice's reference clip
is designed from its instruction and seed the first time it speaks, and kept ("One narrator per
voice"). A GPU voice that falls back to the CPU keeps the CPU engine's own voice, for the whole
episode (`output.engine` "cpu:kokoro", `output.voice` "af_heart"). Without it, everything is as
before.

**out/timings.json** (`timings.py`): `{"version": 1, "duration_s", "segments": [{"start", "end",
"text"}]}`, one segment per sentence (a heading is one) in script order, seconds of the MP3. The
times are where `audio.join()` put each chunk (the lead-in, each chunk trimmed, the pause after
it); loudnorm, the resampling and the MP3 encoder keep that timeline (under 10 ms on test bursts).
Inside a chunk the sentences share its voiced part by characters, and each boundary between two
moves into the pause the voice made there, if there is one near (`audio.pauses`). Measured on
synthetic chunks through the real ffmpeg steps (`tests/test_timings.py`): every sentence edge
within 65 ms of its sound in the decoded MP3, where characters alone were off by 0.7 s on uneven
speech. A problem making them never fails the episode.

`papercast-voice timings <job dir> [--out FILE]` makes them afterwards for a job whose chunk WAVs
are still there (a finished job deletes them), checked against the MP3's duration.

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
| bs1, fp16 (2026-09-27) | fails on the first chunk: `probability tensor contains either inf, nan or element < 0` (device-side assert in sampling) | the sample paragraph, bs1 GPU 0 (Quadro RTX 6000, Turing: no bf16), the pipeline's remote path (`hosts.Host` + `Worker`) |
| bs1, fp32: quality gate against stibnite's bf16 (2026-09-27) | same text, narrator description and seed; the two precisions sample different audio, so these compare distributions. **Whisper small.en (CPU) word errors**: sample paragraph 5 of 74 on bs1, 7 of 74 on stibnite (both mostly Whisper writing "200" and "1" for the spoken numbers, which a word-only score counts, and "plane" for "plain"); 1,000-word excerpt 2 errors (0.2 %) on bs1, 4 (0.4 %) on stibnite. **Seconds per word** (raw chunk audio): 0.488 vs 0.482 (paragraph), 0.358 vs 0.354 (excerpt). **Median pitch** (pYIN, voiced frames): 194.9 vs 185.1 Hz (paragraph), 190.5 vs 186.1 Hz (excerpt); bs1's excerpt chunks 166-215 Hz, stibnite's full episode 157-240 Hz. **Raw loudness** before normalisation: -21.2 vs -22.6 LUFS (paragraph); bs1 excerpt -21.6 (chunks -23.5 to -19.4); the pipeline normalises both to -16. No NaN or silent chunk, no chunk retried. **Peak GPU memory 16,138 MiB** (engine process; device 16,141), flat after load; load 48 s; **real-time factor 1.15** (excerpt: 412.8 s of synthesis for 357.8 s of audio; paragraph 1.18) | sample paragraph + the first 15 paragraphs (1,000 words, 24 chunks) of `tests/fixtures/long_script.md`, bs1 GPU 0, nice 10, nvidia-smi every 1 s over ssh. stibnite's reference: `~/papercast-voice-cache/narrators-2026-09-27/now_s42.wav` (the installed engine, seed 42) and the same chunks of the 2026-09-26 full episode (`measure/breeze-episode-2026-09-26.*`; its raw chunks were deleted, so its excerpt loudness is not comparable) |
| short episode on bs1, real | 137 words → 58.9 s of audio in 115 s (engine load 44 s), -16.09 LUFS, -2.4 dBTP, Whisper word error rate 0.7 %, tags read back; afterwards no new file under bs1's install and its engine gone | `test_real_remote.py`: the installed voice through the runner's `launch.sh`, stibnite's card switched off for the job |
| the narrator changing from chunk to chunk (1.1, 2026-09-29) | speaker-embedding cosine (1 = same voice) between the two halves of one chunk, and between the end of one chunk and the start of the next: **default Breeze voice** (e_emcv7ywo56j6, 59 chunks) **0.83 within, 0.38 between neighbours**, 91 % of neighbouring pairs under 0.6, median pitch per chunk 142–215 Hz; anime preset (e_cml7wvtqpfdw, 47 chunks) 0.87 within, 0.65 between, 37 % under 0.6; **Kokoro** (two episodes, 63 and 65 chunks) **0.89 within, 0.87–0.88 between**, none under 0.6, pitch 190–206 Hz. Every chunk of each came from one engine and one voice key (no mixing) | papercast-group's episodes on perov (the MP3s; chunks of at least 6 s; chunk bounds from timings.json, or, where there was none, from the joiner's silences matched to plan.json's gaps, which on the three episodes that have timings put the bounds 0.04–0.06 s from them, median, with at most 2 chunks more than 0.5 s off), ECAPA-TDNN (speechbrain spkrec-ecapa-voxceleb) on the CPU, pitch by pYIN |
| one designed clip, then clone (1.2, 2026-09-29) — CPU proxy | the default voice's description and seed, six chunks of `tests/fixtures/long_script.md` (20–48 words): **designed one by one** (1.1): halves of a chunk 0.71 (median), neighbours **0.39**, all chunk pairs 0.43 (min 0.26), chunks against the voice's clip 0.34 (0.10–0.51), pitch 188–208 Hz; **cloned from one designed clip** (1.2): halves 0.76, neighbours **0.75**, all pairs 0.84 (min 0.76), against the clip 0.89 (0.83–0.89), pitch 164–182 Hz. Both encoded as an episode: -16.2 and -16.1 LUFS, -1.9 and -2.4 dBTP, one attempt | **not the production path**: Breeze on stibnite's CPU (fp32, upstream's HF `generate`, no CUDA graphs; the same weights, templates and seed), because both GPUs were busy with real episodes; the same ECAPA scoring. The six chunks took 488 s to clone against 1,844 s to design, 12 threads each (a clone has no CFG: one branch, not two). Listen: `~/lab/plots/2026-09-29_voice-fix-{before,after}-cpu.mp3`. **Not yet run on the GPU** (bf16, fast stages): its pace, peak memory and the same scores |

## Install (on stibnite, as leo, no sudo)

```bash
cd /home/leo/NAS_setup/stacks/papercast/voice
bash install.sh --gpu breeze   # [run 2026-09-26] ~45 s with the uv cache; prints PAPERCAST_VOICE_CMD
```
It refuses to run while a voice job is speaking or encoding, or holds the CPU slot or a bs1 slot
(`papercast_voice/busy.py`): it replaces venvs and weights a job's engines load. Waiting jobs do
not stop it: a waiting job checks `VERSION` every 10 s and, when the install replaced it,
re-executes itself on the new code in place (same pid, so the runner still sees its process;
same place in line). `app` is a symlink to `app-<time>/`, swapped in one rename, so a job never
finds no code.

Code-only changes install while jobs speak:
```bash
bash install.sh --code-only    # [run 2026-09-27] app/ and the wrapper only; refused if requirements/
                               # or engines/ differ from the installed commit (then: full install, idle)
```
Speaking jobs keep the code they loaded; their engines keep theirs.

**From 1.0 (once).** 1.0 cannot re-execute itself, so papers already waiting under it stay on 1.0
(stibnite's card only, one at a time) until restarted. `tools/restart-waiting-1.0.py --apply`
does that through the runner's own path: it stops each such voice process group (launch.sh then
writes no rc), and the runner, which adopted these processes after its own restart, treats it as
interrupted and restarts the voice step once, on 1.1, at the paper's old place in line. It uses
up that paper's one "interrupted" restart (a second interruption fails it with Retry), and skips
any paper already interrupted once or whose voice is the runner's own child (a kill there would
fail it). [run 2026-09-27 on one paper; the other 263 were not restarted: that needs Leo's go.]
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
# No PAPERCAST_WPM needed: the runner reads "words_per_min" from `papercast-voice info`
# (Breeze 157.6, pauses included) and keeps 172 only as a fallback.
```

## First run and verify

```bash
V=/home/leo/papercast/voice
$V/bin/papercast-voice info                       # [run] "installed": true, "gpu": {"model": "breeze-tts-2", "need_mib": 10714, ...}
$V/bin/papercast-voice check some/script.md       # [run] chunk counts, or the reason it is refused
cd /home/leo/NAS_setup/stacks/papercast/voice
$V/venv/bin/python -m unittest discover -s tests  # [run] 87 tests (3 real ones skipped), about 4 min
PAPERCAST_VOICE_REAL=1 $V/venv/bin/python -m unittest discover -s tests -p test_real.py
                                                  # [run] real Kokoro through the runner's launch.sh
PAPERCAST_VOICE_REAL_GPU=1 $V/venv/bin/python -m unittest discover -s tests -p test_real_gpu.py
                                                  # [run] real Breeze; skips if the GPU is not free
PAPERCAST_VOICE_REAL_REMOTE=1 $V/venv/bin/python -m unittest discover -s tests -p test_real_remote.py
                                                  # [run] real Breeze on bs1; skips if no bs1 GPU is free
$V/bin/papercast-voice slots                      # [run] the line, and who holds each GPU slot
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
To free bs1 for other work now (every voice job there gives its GPU back after the chunk it is
on, keeping its chunks, and none starts there until the file is removed):
```bash
touch /home/leo/papercast/voice/run/pause-bs1       # [run in tests]  rm it to let them back
```
To stop using bs1 for good: `"hosts": {"bs1": {"enabled": false}}` in `voice.json` (new papers;
a waiting one picks it up when it next re-executes, or at its next run).
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

## Remote GPUs (bs1)

bs1 (`ssh bs1`, boomerserver1, 8 × Quadro RTX 6000 24 GB, driver 610) holds a lean install at
`/home/leo/papercast-voice/`, made by hand on 2026-09-27: `venv/` (`requirements/breeze.txt`,
built with uv; its files share their blocks with uv's cache in `~/.cache/uv`, so removing them
frees nothing), `models/breeze-tts-2/` (7.3 GB, the same revision), `src/` (breeze-tts 008f769),
`cache/` (Triton and inductor, 46 MB), `app/v-<hash>/` (the worker, put there by jobs). Its C
compiler is the system gcc; `ziglang`, `ruff` and `pytest` were removed from its venv (unused
there; 10 MB freed). `/home` there is full (2.5 GB free): nothing else is written there.

Leo's own GPU services there are left alone: the speech model (stt, card 6, always loaded) and,
on demand, the image generator and a second model (card 7) and the chat model (cards 0-6, about
142 GiB across them). Card 7 is not in `hosts.bs1.gpus`: its services check for 15-16 GB free
on it before they start, and would refuse while a voice held it. Cards 0-6 are; card 6 is never
taken while stt is on it. RISKS.md "bs1" says what that means for the chat model.

## Files

| path | what |
|---|---|
| `papercast_voice/cli.py` | `info`, `run`, `check`, `slots` (the line and who holds each slot), `measure-*` |
| `papercast_voice/job.py` | one episode: states, controls, resume, GPU and CPU passes, encode |
| `papercast_voice/textprep.py` | chunking and the speakable-text guard |
| `papercast_voice/refs.py` | each GPU voice's reference clip: designed once, kept, every chunk voiced from it |
| `papercast_voice/gpu.py` | nvidia-smi readings, admission rule, give-back rule (stibnite) |
| `papercast_voice/hosts.py` | GPU slots on stibnite and bs1: ssh, remote readings, remote admission and give-back, the engine command there |
| `papercast_voice/sched.py` | the line for GPU slots (tickets in `run/queue/`) |
| `tools/restart-waiting-1.0.py` | the one-off move of papers waiting under 1.0 onto the installed code (Install) |
| `papercast_voice/workers.py`, `engines/` | engine processes and their protocol; the Kokoro and Breeze workers |
| `engines/breeze/` | the Breeze install step and its C compiler wrapper |
| `papercast_voice/busy.py` | install.sh's check that no voice job is running |
| `papercast_voice/audio.py`, `tags.py` | join, loudness, MP3, verification; ID3 |
| `papercast_voice/timings.py` | out/timings.json: each sentence's time in the MP3 |
| `papercast_voice/measure.py` | the measurements above |
| `tests/` | unit and job tests with fake engines (the Breeze worker with its model faked), a fake `nvidia-smi`, and a fake remote host (`fake_ssh.py`, `fake_remote_smi.py`; `test_slots.py`); `test_real.py`, `test_real_gpu.py`, `test_real_remote.py` |
| `samples/` | the earlier three-voice choice for Leo (not the pipeline) |
