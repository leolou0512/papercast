# papercast-voice — risks, assumptions, and what is not verified

Read with `README.md`. "Measured" = run on stibnite on 2026-09-26 with the command named in the
README; everything else is an assumption or untested, and says so.

## Not built yet

- **The GPU voice.** Leo has not picked one on the choice page
  (`~/lab/review/2026-09-26_papercast-voice.html`). Until an adapter is installed,
  `engine: auto` voices every episode with Kokoro on the CPU at once, because there is nothing
  to wait for; status says so in `note`. The GPU path (waiting, admission, yielding, the one-slot
  lock, out-of-memory recovery) is built and tested against a fake engine and a fake
  `nvidia-smi`, but no real GPU model has run through it.
- **No GPU peak measured through the pipeline.** The sample step measured one 74-word paragraph
  per model (README "Measured"). The admission threshold must come from a full-length run of the
  picked model (`measure-gpu`); until then there is no threshold, and a GPU engine without a
  measured `peak_mib` refuses to run (`engine_failed`, "no measured peak") rather than guess.

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
  mid-sweep); tested since: engines asleep mid-chunk die within a second of a SIGKILLed job. A GPU
  adapter's model server, spawned from the worker's main thread, may use `PR_SET_PDEATHSIG`.
- A GPU adapter that runs a model server (Voxtral runs vLLM-Omni) would listen on a loopback
  port for the length of one episode; any local user could send it text meanwhile. Not built.

## Input handling

- `script.md` is agent-written text. The voice re-checks every chunk right before it reaches an
  engine (digits, symbols, Greek letters, LaTeX, Markdown), with the runner's character set; a
  failure is `script_invalid`, never a silent repair. File names come from chunk index + hash,
  never from text. Tags are cleaned of control characters and capped at 400 characters.
- `job.json` is the runner's; the voice still refuses a paper id outside the papercast pattern
  and any `script`/`output_dir` other than `script.md`/`out`.

## Where things live, and what depends on what

- Install: `/home/leo/papercast/voice/` (0700): code copy, two venvs, Kokoro weights (checksums
  verified), `voice.json` with the measurements. The installed code is a copy: editing the repo
  changes nothing until `install.sh` runs again (`info` prints the installed git commit).
- `~/papercast-voice-cache/` (27 GB, the sample step's venvs and weights) is no longer needed by
  the CPU voice. The GPU adapter's install will copy what it needs from it; after that it can go.
