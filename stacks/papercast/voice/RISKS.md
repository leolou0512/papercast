# papercast-voice — risks, assumptions, and what is not verified

Read with `README.md`. "Measured" = run on stibnite on 2026-09-26 with the command named in the
README; everything else is an assumption or untested, and says so.

## The GPU voice (Breeze TTS 2)

- **One narrator, described in words, not a fixed recording.** Breeze has no preset voices. Every
  chunk is generated from clip A's description, CFG 4 and seed 42, so the same text always gives
  the same audio and every episode starts from the same settings. But the seed does not pin a
  timbre: each chunk's text differs, so each chunk is a fresh draw from the description. The
  voice can therefore shift between chunks (pitch, brightness, pace). Measured on the full
  episode, with median pitch (librosa pYIN) as a rough proxy, not a speaker-identity test: the
  60 paragraph chunks range from 157 to 240 Hz (median 188; 10th–90th percentile 169–213), with
  one chunk 27 % above the median and 17 more than 10 % off it. That stays within one female
  voice; whether it is more than one narrator's natural variation, and audible, has not been
  checked by ear. `measure/breeze-episode-2026-09-26.pitch.json` has the per-chunk figures.
  If Leo hears it wander, the fix is voice cloning from clip A (upstream's `ref_clone_tata`
  template; the fast path refuses only dual-CFG modes, so it should run there) — not built, not
  measured.
- **A chunk can come out wrong.** Such a model can stop early (words missing) or run on (babble,
  or the 1,500-frame cap). The adapter checks every chunk's length against its word count (0.15
  to 1.2 s a word, plus 2 s) and retries with seed 43, then 44. If no attempt passes it keeps
  the one nearest the expected length and logs a warning (`metrics.json` `retried_chunks`)
  rather than failing an episode that Retry would only fail again. The check cannot hear a
  wrong word said at the right length; only Whisper (a test) or Leo's ear can.
- **The admission threshold is measured on one 20-minute script** (`tests/fixtures/long_script.md`,
  README "Measured"). Memory is set at load (weights, a static 2,048-position cache, CUDA
  graphs): on that run it reached 9,690 MiB 97 s in and stayed there to the end, so another
  script's length should not move it; not tested on other scripts. Between install and that measurement the threshold was provisional: the one-paragraph
  sample's 9,696 MiB + 1 GiB.
- **torch 2.9.1 is the CUDA 12.8 build; stibnite's driver is 535 (CUDA 12.2).** It runs by CUDA's
  minor-version compatibility (measured: the sample, the short real test, the full episode). A
  kernel that needs a newer driver's PTX JIT would fail with a CUDA error (`engine_failed`, then
  Use CPU voice); none has. No driver change was made or is needed.
- **Compiling needs zig on stibnite** (bs1 uses its system gcc). Triton and inductor build C stubs and there is no gcc on stibnite;
  `engines/breeze/bin/cc` wraps zig's clang from the `ziglang` wheel in the engine's venv. Its
  caches live in `engines/breeze/cache/`; a cold cache adds compile time to the first load.
- **The engine runs in-process**: no model server, no port. Besides the GPU it uses 2 torch
  threads and 5.7 GB of RAM (peak resident size on the full episode).
- **Pronunciation** of jargon and names is the model's own; it can misread. Nothing checks it
  but the ear.

## Deliberate deviation from the brief

- **No systemd unit for the voice.** The brief asked for one; the interface (INTERFACE.md §10,
  binding) makes the voice a command the runner starts per episode, not a service. Reboot
  survival is the runner's: its user unit comes back after a reboot and restarts an interrupted
  voice step once (INTERFACE §3), and the voice resumes from its finished chunks. A voice unit
  would be a second supervisor for the same job directories. What was verified instead: a job
  killed with SIGKILL (what a reboot does to it) resumes on re-run without voicing a finished
  chunk twice, and its engine processes die with it. **Not verified: an actual reboot** (never
  reboot a shared machine), and the runner's unit itself (Part B's).
- Consequence: two reboots during one episode's wait make it `failed/interrupted` (the runner's
  restart-once rule). A paper can wait for the GPU for days; Retry brings it back with its
  chunks.

## Shared GPU

- **Admission cannot see the future.** The rule waits for measured peak + 1 GiB free and an
  idle card (≤ 20 % utilisation on 3 polls, 10 s apart). A job someone starts *after* the voice
  was admitted can still find less memory than it wants. The voice gives the GPU back when it
  sees another compute process at ≥ 20 % SM on 3 checks in a row or free memory under 256 MiB,
  but a job that allocates everything at once and dies on the first failure can fail before the
  voice notices. The window is one chunk (seconds). Not preventable without a scheduler.
- **Utilisation includes the desktop.** Xorg, GNOME Shell and Firefox on stibnite use this card
  (106 + 46 + 34 MiB measured, 0 % utilisation idle). A desktop session keeping the card above
  20 % (video, say) would make papers wait; **Use CPU voice (Kokoro)** is the way out. Not
  measured: what a playing video does to `utilization.gpu`.
- **Leo's own GPU work counts as "someone else's job".** The rule cannot tell his research from
  another user's, and treats both as work not to slow down.
- **`nvidia-smi pmon` samples once per check.** A neighbour with bursty GPU use may be missed
  for a while (the voice keeps going) or seen late. Measured: pmon reports per-process SM % for
  compute processes on driver 535.288.01 (a test matmul read 99 %).
- **The voice never signals a process it did not start.** Every kill goes to a descendant of an
  engine it spawned, running as `leo`, identified by pid + start time (tested: another process
  listed on the fake GPU at 95 % stays untouched).

## CPU voice (Kokoro)

- **Memory: up to 3.7 GB per worker, 11.8 GB for a whole job** (measured on a full episode,
  whole process tree sampled every second; the orchestrator's own peak is 1.4 GB, while it
  decodes the episode to check it). Worker memory grows with the chunks done (1.9 GB after 6,
  up to 3.7 GB after about 21); `MALLOC_ARENA_MAX=2` was tried and changed nothing, so it is not
  set. Stibnite had 95 GB available when measured. A longer episode would use somewhat more.
- `nice 10` lowers the voice's CPU share under contention; it does not limit memory bandwidth
  or cache, which Kokoro's workers do share with other users' jobs on the same socket.
- One CPU job at a time across all papers (`run/voice-cpu.lock`); a second waits, shown as
  speaking with `wait_text` "waiting for the CPU voice".
- Pronunciation of jargon and names is Kokoro's G2P (misaki, with espeak for unknown words);
  it can mispronounce. Not a pipeline failure; nothing checks it except the ear.

## Audio

- **Loudness is limited, not just scaled.** Speech peaks about 21 dB above its loudness, so
  reaching -16 LUFS under -1 dBTP means ffmpeg's loudnorm limits peaks by several dB (dynamic
  mode), and the MP3 encoder moves loudness and peak again. The voice corrects on the *encoded*
  file until it is within 0.5 LU and under the limit (measured: one or two attempts). An episode
  more than 1 LU off, or over -1 dBTP, fails `encode_failed` rather than shipping; chunks are
  kept, so Retry only re-encodes. Encoding a seventeen-minute episode takes about 60 s a pass.
- **MP3, not Opus** (README "Output format"). Untested here: Navidrome's display of the ID3v2.4
  tags (one "Papers" album via `albumartist`); INTERFACE §10.2 leaves that check to Part A on
  staging. TagLib reads ID3v2.4.
- `ffmpeg` is the static 7.0.2 build shipped inside the `imageio-ffmpeg` 0.6.0 wheel (pinned),
  not a system package: it updates only when that pin moves.
- After `done`, chunk audio is deleted (about 60 MB an episode); `plan.json` and `metrics.json`
  stay. Re-voicing a finished episode means synthesising it again.

## Processes

- Engines die with their job by watching their parent process (checked every second), not by
  `PR_SET_PDEATHSIG`: the kernel sends that signal when the spawning *thread* exits, and the CPU
  pool starts workers from threads. Found by measurement (it killed a working Kokoro engine
  mid-sweep); tested since: engines asleep mid-chunk die within a second of a SIGKILLed job. The
  Breeze engine uses the same mechanism (it is the same protocol module).
- **Reinstalling underneath a job.** A full `install.sh` replaces venvs and weights a running
  engine loads, so it refuses while a job is speaking or encoding, or the CPU slot or a bs1
  slot is held (`papercast_voice/busy.py`, tested). It cannot see a job admitted in the seconds
  it runs. Waiting jobs do not block it: they re-execute onto the new code (tested with a fake
  install: same pid, same place in line). `--code-only` replaces only the code, while jobs
  speak: a speaking job keeps the modules it loaded, and a new engine it starts (after giving a
  GPU back) is the new worker file, which speaks the same protocol. **Not tested: a code change
  that breaks the protocol between the two**; such a change needs a full install when idle.
- **Voice 1.0 jobs cannot re-execute.** Papers already waiting under 1.0 when 1.1 was installed
  stay on 1.0 (stibnite's card only) until restarted; the restart (README "Install") uses up
  the runner's one "interrupted" restart of that paper's speaking step. On 2026-09-27 one paper
  was moved; 263 were still waiting under 1.0 (not moved: that needs Leo's go-ahead). 1.0 jobs
  and 1.1 jobs share stibnite's slot lock, so they never share the card, but 1.0 jobs are not
  in the line: a 1.1 paper can take stibnite's card ahead of an older 1.0 one.

## bs1 (remote GPUs)

- **Leo's chat model and the voice cannot share bs1 cards 0-6.** The chat model (started on
  demand from his dashboard, `nas-gpu-control` on bs1) spreads about 142 GiB over cards 0-6 and
  refuses to start with under 150,000 MiB free across them. Two or more voice jobs on those
  cards (16 GB each) make it refuse ("not enough VRAM"); with exactly one it would start and
  run out of memory on that card. The voice cannot see a refusal, only a process that appears:
  when one does, on any of cards 0-6, every voice job on bs1 gives its card back after its
  chunk and bs1 is left alone for 30 minutes, so a second press of Start works (tested with the
  fake host; not tested with the real chat model). While the backlog is being voiced, pressing
  Start will usually be refused. **The lever**: `touch ~/papercast/voice/run/pause-bs1` frees
  bs1 within one chunk (about 30 s) and keeps the voice off it until the file is removed.
  Card 7 (image generator, second model) is not used at all for the same reason.
- **One narrator in two precisions.** stibnite speaks bf16, bs1 fp32 (fp16 fails there). Same
  description, seed and settings; measured on the same texts, word error rate, pace and median
  pitch match within what one precision varies from chunk to chunk (README "Measured"). A paper
  that moves between hosts mid-episode (it gave a GPU back) mixes chunks of both; not checked
  by ear.
- **Speed**: fp32 on a Quadro RTX 6000 speaks at 1.15 × real time (stibnite 0.83), and an engine
  takes about 45 s to load, per paper. 6 bs1 cards (0-5; 6 while stt is on it is not taken) and
  stibnite voice about 5.9 × as fast as stibnite alone, not 7 ×.
- **Each paper's engine is one ssh session for its whole episode** (about 25 minutes). A dropped
  connection costs the chunk being spoken; the paper goes back to its place in line, and after
  three bs1 failures in one run it waits for stibnite only. bs1 not answering: it is skipped for
  60 s at a time and stibnite goes on alone.
- **Our engines are recognised by executable** (bs1's `papercast-voice/venv/bin/python`, as
  nvidia-smi names a process by its command: measured). Anyone else starting that exact python
  would be mistaken for the voice.
- **bs1's disk is full** (2.5 GB free on /home). The voice writes nothing per job there; the
  compiler caches (46 MB) can grow a little with a new kernel shape. The venv's 7.4 GB share
  blocks with uv's cache outside the install (reflinks), so they cannot be freed from inside it.
- **Clock**: bs1's clock runs about 2 minutes behind stibnite's; nothing depends on it.
- The page's order follows **hand-over order** (when a script was finished), which differs from
  upload order when scripts finish out of order (10 % of pairs in the 2026-09-27 backlog).

## Input handling

- `script.md` is agent-written text. The voice re-checks every chunk right before it reaches an
  engine (digits, symbols, Greek letters, LaTeX, Markdown), with the runner's character set; a
  failure is `script_invalid`, never a silent repair. File names come from chunk index + hash,
  never from text. Tags are cleaned of control characters and capped at 400 characters.
- `job.json` is the runner's; the voice still refuses a paper id outside the papercast pattern
  and any `script`/`output_dir` other than `script.md`/`out`.

## Where things live, and what depends on what

- Install: `/home/leo/papercast/voice/` (0700): code copy, three venvs, Kokoro and Breeze
  weights (checksums verified), the breeze-tts code at upstream commit 008f769, `voice.json`
  with the measurements. The installed code is a copy: editing the repo changes nothing until
  `install.sh` runs again (`info` prints the installed git commit).
- `~/papercast-voice-cache/` (27 GB, the sample step's venvs, weights and uv cache) is no longer
  needed. The install's Breeze weights and venv files are hard links into it (same pool, no extra
  space; link counts checked), so deleting the cache breaks nothing and frees less than 27 GB.
