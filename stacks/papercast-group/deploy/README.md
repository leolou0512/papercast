# papercast-group on perov: install, voice, tunnel

The hub (web page, the CLI's API, the voice queue), the voice worker (Breeze on perov's GPU) and
the way in from the internet (a Cloudflare tunnel). All of it runs as `leo` on perov
(`val-perovskite`, tailnet `100.97.205.90`: the name does not resolve from stibnite), with
`systemd --user`, no sudo, nothing system-wide. perov is shared with about ten other people:
everything is niced, the GPU is taken only when it is idle and given back when someone else
starts on it, and nothing of anyone else's is ever touched.

```
contributor's laptop: papercast add paper.pdf   (Claude runs there, under their own login)
   │  HTTPS, Bearer pcg_… token, /api/cli/*
   ▼
Cloudflare ──tunnel──▶ cloudflared (perov) ──▶ hub 127.0.0.1:8480 ──▶ data/hub.db, data/episodes/<id>/
                                                   ▲   POST /api/voice/claim, PUT status/audio
                                                   │   (127.0.0.1, worker token)
                                        pcg-voice: voice_worker.py ──▶ papercast-voice run
                                                                       data/episodes/<id>/voice/
```

## What is where (on perov)

| path | what |
|---|---|
| `~/papercast-group/` (0700) | the install (`PCG_HOME`) |
| `  repo/` | the source it was installed from (`tools/sync_to_perov.sh`; `repo/.revision` = commit) |
| `  app -> app-<time>/` | the running code: `papercast-group/` (hub, deploy, tools), `papercast-cli/`, `VERSION` |
| `  venv/` | Python 3.10 (system), numpy 2.2.6; `app/papercast-group` and `app/papercast-cli` on its path (a `.pth`) |
| `  hub.env` (0600) | `PCG_DATA`, `PCG_AUTH`, `PCG_SECRET` (generated once), `PCG_BIND=127.0.0.1`, `PCG_PORT=8480`, `PCG_PUBLIC_URL`, `PCG_ADMIN_EMAILS`, `PCG_WORKER_TOKEN_SHA256` |
| `  worker.token` (0600), `worker.env` (0600) | the voice worker's token (the hub keeps only its sha256) and settings |
| `  data/` | `hub.db` and `episodes/<episode_id>/` (the voice job is `episodes/<id>/voice/`) |
| `  voice/` | papercast-voice with Breeze TTS 2 and Kokoro (~13 GB), `voice/voice.json` |
| `  worker/current.json` | the episode the worker holds (a restart carries on with it) |
| `  backups/`, `logs/`, `run/` | nightly backups, the tunnel's and the fallback's logs, pid files |
| `~/.config/systemd/user/pcg-*` | `pcg-hub.service`, `pcg-voice.service`, `pcg-backup.timer` (03:30), `pcg-layout.timer` (04:15) |
| `~/.local/bin/uv`, `ffmpeg`, `cloudflared` | static binaries, each pinned to a release and its sha256 (`lib.sh`) |

## Install

From stibnite (or any checkout): copy the repo over, then install there.
```bash
bash stacks/papercast-group/tools/sync_to_perov.sh             # [run 2026-09-28] rsync, 8 MB
ssh leo@100.97.205.90
cd ~/papercast-group/repo
bash stacks/papercast-group/deploy/install.sh --voice          # the first time (= install-voice.sh, [run
                                                               # 2026-09-28 on its own], then the rest)
bash stacks/papercast-group/deploy/install.sh                  # [run 2026-09-28, three times] code only, keeps the voice
```
`install.sh` is idempotent: it swaps the code in (`app` is a symlink replaced in one rename,
the last three copies kept), keeps `data/`, the secret and the worker token, adds settings that
are missing without touching the ones there, runs `python -m hub.db migrate` (A1's: the
migrations, then once the seeds a new hub starts with: base prompt v1 and the five topic
graphs; the hub itself only migrates), writes the units and restarts the hub and the worker.
Options: `--public-url URL`, `--admin a@x,b@y` (sets `PCG_ADMIN_EMAILS`), `--no-start`.

Why these choices:
- **The hub starts through `deploy/run_hub.py`**, not `python -m hub.app`: run as `-m`,
  app.py is the module `__main__` and the other modules import a second copy of it as
  `hub.app`, so app.py's `except HTTPError` misses the errors they raise and every 401, 404 or
  428 goes out as a 500 (found by the end-to-end test against the parts as they stood).
- **A venv without pip, filled by uv.** perov's Python 3.10 has no `ensurepip`
  (`python3.10-venv` is not installed, and installing it needs sudo), so `python3.10 -m venv
  --without-pip` and `uv pip install`. uv 0.11.33 (the version on stibnite) is fetched once
  from its GitHub release, checksum pinned.
- **The code on the path by a `.pth`**, not `pip install -e`: the hub needs only
  `papercast_cli.common` (stdlib), and a `.pth` through the `app` symlink follows each install.
- **ffmpeg** is the static 7.0.2 that `imageio-ffmpeg` 0.6.0 ships (the build papercast-voice
  encodes with), taken from the PyPI wheel because PyPI files never change, so the pinned sha256
  keeps holding; the usual static "release" tarballs are replaced in place. The hub does not need
  it; the voice has its own copy.

### The voice (`install-voice.sh`, run by `install.sh --voice`)

Leo's `stacks/papercast/voice/install.sh --gpu breeze`, unmodified, with
`PAPERCAST_VOICE_HOME=~/papercast-group/voice` (stibnite's stays in `/home/leo/papercast/voice`):
the orchestrator venv, Kokoro (CPU) and Breeze TTS 2 (GPU, bf16 as on stibnite's A4000: perov
has the same card), every weight file checked against its pinned sha256. Then `voice.json` gets
this machine's card only (`hosts.bs1.enabled = false`) and its GPU slot lock under
`voice/run/`. [run 2026-09-28: 3 min with perov's network, 13 GB, `info` says installed,
`need_mib` 10,720 (the sample's provisional 9,696 MiB peak + 1 GiB; `measure-gpu` would replace it).]

**Voice test on perov** (2026-09-28, `~/papercast-group/voice-test/2026-09-28-short/voice/`):
an 80-word script (heading + two paragraphs, 3 chunks), the card idle (123 MiB used by the desktop,
0 %). Admitted after 20 s (3 idle polls), engine load and CUDA-graph capture 107 s (cold
compiler caches: stibnite's was 46 s warm), speech 28.8 s for 35.7 s of audio (real-time factor
0.81, stibnite 0.83), 9,032 MiB in use at one reading while speaking, 164 s in all, the card back to 123 MiB
after. The MP3: 35.7 s, 96 kbit/s mono 44.1 kHz; **-16.18 LUFS** by pyloudnorm on the decoded file
(target -16 ± 0.5), true peak -2.3 dBTP (limit -1); no chunk retry recorded in metrics.json.
Then the same script **through the worker** (`tools/voice_smoke.py`: the real worker and the
real papercast-voice, `deploy/tests/fake_hub.py` as the hub): claimed, phases preparing →
waiting-for-gpu → speaking → encoding reported, 90.7 s in all (engine load 34.6 s with the caches warm), the
hub received 430,551 bytes of `audio/mpeg` (ID3-tagged), `X-Duration-S` 35.7, sha256 matching
the voice's; the page's sentences said "Speaking on val-perovskite GPU 0" (not "stibnite").
The MP3: `~/papercast-group/pcg-voice-smoke-twasx7vj/received.mp3` on perov.
Not measured: a full-length episode on perov (`measure-gpu`), listening by ear.

## Running it

```bash
systemctl --user status pcg-hub pcg-voice            # [run]
journalctl --user -u pcg-hub -u pcg-voice -f         # logs
systemctl --user restart pcg-hub                     # [run] after a settings change
systemctl --user list-timers 'pcg-*'                 # [run] backup 03:30, layout 04:15
systemctl --user start pcg-backup                    # [run, guarded] tools/backup.py --data $PCG_DATA --dest ~/papercast-group/backups
cat ~/papercast-group/worker/current.json            # the episode being voiced, if any
cat ~/papercast-group/data/episodes/<id>/voice/status.json   # the voice's own status
```
The first admin (local auth): `cd ~/papercast-group/app/papercast-group && set -a && .
~/papercast-group/hub.env && set +a && ~/papercast-group/venv/bin/python -m hub.auth bootstrap
<email>` prints a one-time link (A2's command; `PCG_ADMIN_EMAILS` must name that email).

**Linger** is on for leo on perov (`loginctl show-user leo` → `Linger=yes`, checked
2026-09-28), so the units start at boot and survive logout. `install.sh` warns if it is off.
Without a usable `systemd --user` at all, `install.sh` puts `deploy/watchdog.sh` in leo's
crontab instead (every 2 min: start the hub and the worker if they are not running, `setsid
nohup nice`; and a nightly line for backup and layout); `watchdog.sh stop|restart` by hand.
[run 2026-09-28 by hand with the units stopped: both started at nice 10 in their own process
groups, a second run started nothing, `stop` ended both, `nightly` ran its guarded steps.]

Checked on perov 2026-09-28: `systemctl --user restart` of both units (new pids, active), the
hub SIGKILLed (back by itself in 5 s, `NRestarts=1`), both timers' services run once (guarded
messages, `success`), `uninstall.sh` (units, processes and code gone; data, token, voice kept)
and `install.sh` again (all four active, the same worker token).

### The voice worker (`voice_worker.py`, SPEC section 9)

One episode at a time: `POST /api/voice/claim` (worker token) → fetch the script → write
`data/episodes/<id>/voice/{script.md, job.json}` as Leo's runner does (INTERFACE §10.2; the
episode id becomes a papercast-voice id `YYYY-MM-DD-<8 base32>`, because the voice accepts only
those) → `nice -n 10 papercast-voice run <dir>` detached, with a cleaned environment → reads
`status.json` every 2 s and sends `PUT /api/voice/<id>/status {"phase","progress","detail",
"eta_s","engine"}` on every change and at least once a minute (so the hub keeps the claim while
the voice waits for the GPU for hours) → checks the MP3 (path, size, sha256 against
status.json) → `PUT /api/voice/<id>/audio` (`audio/mpeg`, `X-Duration-S`, `X-Sha256`) → deletes its
copy of the MP3 (the hub has it). A failure is `POST /api/voice/<id>/failed {"error", "code"}`.

- **Restarts.** `worker/current.json` names the episode held; a restarted worker carries on
  with it without claiming again, and papercast-voice skips the chunks it already made.
  Stopping the unit stops the voice it started (the unit's cgroup); a voice that outlived its
  worker (the watchdog fallback, a SIGKILL) is adopted from `status.json`'s pid instead of run
  twice (checked: our uid, papercast-voice's command line, this job directory).
- **The hub says no.** 404, 409 or 410 to a status or the upload (the episode was deleted, or
  the claim went back to the queue) or `{"cancel": true}`: the worker touches `cancel` (the voice
  stops within a second), keeps the job directory (its chunks serve a later claim) and moves on.
- **The hub is down.** The voice goes on; statuses are dropped; the upload and the failure
  report wait with a capped backoff (10 s doubling to 5 min) and are never lost.
- **Waiting for the GPU** is papercast-voice's own rule (measured peak + 1 GiB free and the card
  idle on three polls; it gives the card back when someone else starts computing on it). Its
  sentences call the local card "stibnite" (its config's name for the local host); the worker
  says perov's name instead before the page sees them.
- `PCG_VOICE_ENGINE=cpu` in `worker.env` voices with Kokoro on the CPU instead (a different
  voice: Leo's pick is Breeze; never automatic).

Tests: `python3 -m unittest discover -s stacks/papercast-group/deploy/tests` (a fake hub, a fake
papercast-voice with the real one's files and ids; 14 tests, about 20 s) [run on stibnite and perov].

## The quick tunnel (testing only)

```bash
bash ~/papercast-group/app/papercast-group/deploy/tunnel.sh up --minutes 5   # prints https://….trycloudflare.com
bash ~/papercast-group/app/papercast-group/deploy/tunnel.sh status
bash ~/papercast-group/app/papercast-group/deploy/tunnel.sh down
```
A free Cloudflare quick tunnel (`cloudflared tunnel --url http://127.0.0.1:8480`, no account),
detached and niced, ended by itself after `--minutes` (default 15). `up` sets `PCG_PUBLIC_URL`
to the trycloudflare address and restarts the hub (cookies become `Secure`, invite links name
that address); when the tunnel ends, the previous value comes back. `--no-autoupdate` always:
cloudflared never replaces its binary. **Only with fake or no data**: the address is public,
and only the hub's own local auth stands in front of it.

cloudflared is the official static `cloudflared-linux-amd64` 2026.8.2 from Cloudflare's GitHub
releases, sha256 `fcfb02b5…0576ad2` (GitHub's asset digest). perov already had that exact file in
`~/.local/bin/cloudflared` (dated 2026-08-28, before this install); it is used as it is. A
different file there would be left alone and ours put in `~/papercast-group/bin/`.

**On perov the quick tunnel cannot come up** [run 2026-09-28]: Imperial's resolvers
(155.198.142.7/8) answer `trycloudflare.com` and `api.trycloudflare.com` with `146.179.34.253`,
`phishing.net.ic.ac.uk`, the college's block page, which refuses port 443; cloudflared stops
with `failed to request quick Tunnel: Post "https://api.trycloudflare.com/tunnel": dial tcp
146.179.34.253:443: connect: connection refused`. That is the college's security policy
(anonymous trycloudflare addresses are a phishing favourite), so this script does not go
around it (no other resolver, no hosts entry). A named tunnel does not use trycloudflare: its
edge, `region1.v2.argotunnel.com`, resolves to Cloudflare (198.41.192.x) and TCP 7844 answers
from perov. Whether a tunnel out of a college machine is allowed at all is Imperial ICT's
call; ask before production. What was checked instead, with `tests/fake_cloudflared.sh`
standing in for cloudflared (`PCG_CLOUDFLARED=…`): the address parsed (not the `api.` line
cloudflared also prints), `PCG_PUBLIC_URL` set and the hub restarted with it, `down` and the
time limit (`--minutes 1`: ended after 60 s) both putting the old value back, nothing left
running; and with the real cloudflared, the refusal reported and `PCG_PUBLIC_URL` untouched.

## Production: papercast.virtualatoms.org (Cloudflare tunnel + Access)

The domain's owner does two things in his Cloudflare dashboard; perov needs no open port, since
the tunnel connects out (checked 2026-09-28: `region1/2.v2.argotunnel.com` and
`<team>.cloudflareaccess.com` are reachable from perov; only the free quick tunnel's
`trycloudflare.com` is blocked by the college's DNS).

1. **The tunnel.** Zero Trust → Networks → Tunnels → Create a tunnel → Cloudflared, named
   `papercast`. Public hostname: subdomain `papercast`, domain `virtualatoms.org`, service
   `HTTP` → `127.0.0.1:8480` (the hub, next to cloudflared on perov). Cloudflare adds the DNS
   record itself (`papercast CNAME <tunnel id>.cfargotunnel.com`, proxied); a record alone would
   not work, as the tunnel must be in the same account as the domain. He sends the **tunnel token**.
2. **The login.** Zero Trust → Access → Applications → Add → Self-hosted: name `Papercast`,
   domain `papercast.virtualatoms.org`, policy **Allow** with the group's emails (or an email
   domain), login method One-time PIN (and Google if wanted). A second self-hosted application
   for the path `papercast.virtualatoms.org/api/cli/*`, policy **Bypass**, Everyone: the CLI
   talks to the hub with its own device token there (the voice worker talks to the hub on
   127.0.0.1, not through the tunnel, so it needs no bypass). He sends the **team name**
   (`<team>.cloudflareaccess.com`) and the first application's **AUD tag** (its Overview page).

Then on perov:

    umask 077; cat > ~/papercast-group/tunnel.token      # paste the token, Enter, Ctrl-D
    bash ~/papercast-group/app/papercast-group/deploy/cloudflare.sh tunnel
    bash ~/papercast-group/app/papercast-group/deploy/cloudflare.sh login --team <team> --aud <AUD> \
         --url https://papercast.virtualatoms.org --admin <the email Leo logs in with>

From then on everyone signs in with Cloudflare's email code; a person seen for the first time is
a viewer, and an admin makes them a contributor in Settings → Users. Contributors log the CLI in
with `papercast login --server https://papercast.virtualatoms.org` (approve in the browser).
`cloudflare.sh local` goes back to the hub's own invite links; `cloudflare.sh status` shows the mode.

## Uninstall

```bash
bash ~/papercast-group/repo/stacks/papercast-group/deploy/uninstall.sh              # units, code, venv; keeps data, voice
bash ~/papercast-group/repo/stacks/papercast-group/deploy/uninstall.sh --voice      # also the voice (13 GB)
bash ~/papercast-group/repo/stacks/papercast-group/deploy/uninstall.sh --everything # all of ~/papercast-group (asks)
```
uv, ffmpeg and cloudflared stay in `~/.local/bin`.

## The end-to-end test

`tests/e2e_test.py`: a hub on a free port with `PCG_AUTH=header` and a temporary data directory,
users (admin from `PCG_ADMIN_EMAILS`, a contributor, a viewer), then (1) the CLI API by hand:
device login, lookup, claim, a bundle, the hub's checks; (2) the real CLI (`papercast login`,
`papercast add` a tiny PDF) with a fake `claude` on PATH (A8's own if it ships one); each then
the real voice worker with a fake papercast-voice writing a short MP3, and the web API (library,
paper, `/audio/<id>.mp3` with Range, the explainer with its CSP) showing the episode ready with
those exact bytes. A test whose parts are not merged is skipped and names the part. It seeds
the database first as the install does (`python -m hub.db migrate`), so the base prompt exists.
```bash
python3 -m unittest discover -s stacks/papercast-group/tests -p 'e2e_test.py' -v
PCG_E2E_KEEP=1 ...                  # keep the temporary directory (hub.log, worker.log, data)
```
[run 2026-09-28 against a scratch copy of this branch with every other part's files as they
stood in their worktrees at about 05:00 UTC, not a merge: (1) passed, the whole path in 1.6 s;
(2) the real CLI logged in, ran the pipeline with A8's fake claude and stopped at the upload on
an A7/A8 mismatch (`Api.upload() takes 2 positional arguments but 4 were given`).]

## Files

| file | what |
|---|---|
| `install.sh`, `install-voice.sh`, `uninstall.sh`, `lib.sh` | the install (pins for uv, ffmpeg, cloudflared in `lib.sh`) |
| `systemd/pcg-*.service`, `pcg-*.timer` | the units (`@H@` = the install, filled in by install.sh) |
| `voice_worker.py` | the worker (stdlib, Python 3.10) |
| `run_hub.py` | how the hub is started (one copy of `hub.app`) |
| `watchdog.sh` | the no-systemd fallback (crontab) |
| `tunnel.sh` | the quick tunnel |
| `tests/` | the worker's tests: `fake_hub.py`, `fake_papercast_voice.py`, `test_voice_worker.py` |
| `../tools/sync_to_perov.sh` | rsync of the repo to perov |
| `../tools/voice_smoke.py` | one short episode through the worker and the real voice, a fake hub |
| `../tests/e2e_test.py`, `../tests/fake_claude.py` | the end-to-end test |
