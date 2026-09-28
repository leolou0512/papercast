# papercast-group on perov: install, voice, tunnel

The hub (web page, the CLI's API, the voice queue), the voice worker (Breeze on perov's GPU) and
the way in from the internet (a Cloudflare tunnel), on perov (`val-perovskite`, tailnet
`100.97.205.90`: the name does not resolve from stibnite). Since 2026-09-28 all of it runs as the
system account **`papercast`** from **`/srv/papercast`**, with units `papercast-*` in
`/etc/systemd/system`, so it does not depend on leo's account; `papercastctl` does the admin
jobs, and [`../HANDOVER.md`](../HANDOVER.md) is the short guide for whoever looks after it. perov
is shared with about ten other people: everything is niced, the GPU is taken only when it is
idle and given back when someone else starts on it, and nothing of anyone else's is ever touched.

```
contributor's laptop: papercast add paper.pdf   (Claude runs there, under their own login)
   │  HTTPS, Bearer pcg_… token, /api/cli/*
   ▼
Cloudflare ──tunnel──▶ papercast-tunnel (cloudflared) ──▶ papercast-hub 127.0.0.1:8400 ──▶ data/hub.db, data/episodes/<id>/
                                                              ▲   POST /api/voice/claim, PUT status/audio
                                                              │   (127.0.0.1, worker token)
                                              papercast-voice: voice_worker.py ──▶ papercast-voice run
                                                                                   data/episodes/<id>/voice/
```

## What is where (on perov)

`/srv/papercast` is `root:papercast 0750`. Root owns what root runs (code, venv, binaries,
`papercastctl`), the account owns what it writes; so a compromised hub cannot change anything an
admin later runs with sudo.

| path | owner | what |
|---|---|---|
| `app/releases/<time>/` | root | a release: `papercast-group/` (hub, deploy, tools), `papercast-cli/`, `VERSION` |
| `app/current`, `app/previous` | root | the running release, the one before (symlinks, each swapped in one rename) |
| `venv/` | root | Python 3.10 (system), numpy 2.2.6; `app/current/papercast-group` and `app/current/papercast-cli` on its path (a `.pth`) |
| `src/` | root | the tree the release came from (the three trees below, committed files only; `src/.revision`) |
| `bin/` | root | `uv` 0.11.33, `ffmpeg` 7.0.2, `cloudflared` 2026.8.2, `papercastctl` (`/usr/local/bin/papercastctl` links here) |
| `etc/` | root dir, files papercast 0600 | `hub.env`, `worker.env`, `worker.token` (the hub keeps only its sha256), `tunnel.token`, `smtp.password` when email is set up |
| `data/` | papercast, group-readable | `hub.db` and `episodes/<episode_id>/` (the voice job is `episodes/<id>/voice/`); `search.db`, the search index (rebuilt from those at the hub's start if deleted; the backup leaves it out) |
| `backups/` | papercast | nightly, `tools/backup.py`, the newest 14; one more before each update |
| `voice/` | papercast | papercast-voice with Breeze TTS 2 and Kokoro (14 GB), `voice/voice.json`, its compiler caches |
| `python/` | papercast | the uv-managed CPython 3.11.15 the voice's venvs run on (perov's own is 3.10) |
| `cache/` | papercast | the account's HOME for tools and the voice (uv, huggingface, CUDA) |
| `worker/current.json` | papercast | the episode the worker holds (a restart carries on with it) |
| `HANDOVER.md` | root | the guide, installed with each release |

`hub.env`: `PCG_DATA=/srv/papercast/data`, `PCG_AUTH`, `PCG_SECRET` (generated once), `PCG_BIND=127.0.0.1`,
`PCG_PORT=8400` (where the Cloudflare dashboard points the tunnel), `PCG_PUBLIC_URL`,
`PCG_ADMIN_EMAILS`, `PCG_WORKER_TOKEN_SHA256`; for "forgot password" emails `PCG_SMTP_HOST`,
`PCG_SMTP_PORT`, `PCG_SMTP_USER`, `PCG_SMTP_PASSWORD_FILE`, `PCG_SMTP_FROM`.

The units (`systemd/system/papercast-*`, `@H@` = `/srv/papercast`, `@USER@` = `papercast`):

| unit | runs | notes |
|---|---|---|
| `papercast-hub.service` | `venv/bin/python app/current/papercast-group/deploy/run_hub.py` | `Restart=always`, nice 10, `MemoryMax=4G`, `PrivateDevices` |
| `papercast-voice.service` | `venv/bin/python app/current/papercast-group/deploy/voice_worker.py` | `Restart=always`, nice 10, `MemoryMax=24G`, `KillMode=control-group`, HOME=`cache/`; `DevicePolicy=closed` with `DeviceAllow=char-nvidia rw`, `char-nvidia-uvm rw`, `char-nvidia-caps r` (perov's `/dev/nvidia*` are 0666, so no `SupplementaryGroups=`) |
| `papercast-tunnel.service` | `bin/cloudflared tunnel --no-autoupdate run --token-file etc/tunnel.token` | `Restart=always`, nice 10 |
| `papercast-backup.timer` / `.service` | 03:30, `tools/backup.py --data data --dest backups` | oneshot, nice 19, idle I/O |
| `papercast-layout.timer` / `.service` | 04:15, `python -m hub.layout --all` | oneshot, nice 15 |

All as `User=papercast`, with `NoNewPrivileges`, `ProtectSystem=strict` + `ReadWritePaths=/srv/papercast`,
`ProtectHome=yes` (the services never see anyone's home), `PrivateTmp`, `ProtectKernel*`,
`ProtectControlGroups`, `RestrictSUIDSGID`, `RestrictNamespaces`, `CapabilityBoundingSet=` (none),
`RestrictAddressFamilies` (Unix and IP; netlink for the voice and tunnel), `UMask=0027` (the
group papercast reads new data, others nothing). [checked 2026-09-28 as papercast in the voice
unit's sandbox: `nvidia-smi -L` sees the A4000; `/home` "Permission denied"; `cache/` writable,
`app/` not, `/etc` read-only; `systemd-analyze verify` has nothing to say about the seven units.]

## Install and update

From stibnite (or any checkout of the repo):
```bash
bash stacks/papercast-group/tools/sync_to_perov.sh --install    # git archive of HEAD -> perov:~/papercast-src,
                                                                # then sudo papercastctl update ~/papercast-src
```
or by hand on perov: `sudo papercastctl update ~/papercast-src` (`--voice` also re-installs the
voice). A machine with nothing yet: `sudo bash stacks/papercast-group/deploy/install.sh --system
--voice [--admin-user LOGIN]`. `install.sh` picks the system mode by itself on perov (and wherever
the units are installed); `--user` is leo's old install (below).

What an update does (`install.sh --system`, under sudo): makes the account and the folders if
missing (and gives the account back its files when `/srv/papercast` was copied from another
machine); fetches `uv`, `ffmpeg`, `cloudflared` into `bin/` when missing (pinned in `lib.sh`; a
working `cloudflared` put there by hand is kept); copies the tree to `src/` (git archive of HEAD
when it is a checkout: ignored secrets never travel) and into a new `app/releases/<time>/`,
byte-compiled; swaps `app/current` to it; keeps the secret, the tokens and every setting
(rewriting only the paths of this layout, and refusing any that still name `/home`); takes a
backup and runs `python -m hub.db migrate` as papercast; writes the units, `papercastctl` and
`HANDOVER.md`; restarts the hub and the worker (the tunnel is started if it is not running) and
checks that the hub answers on 127.0.0.1:8400 and both units are active. **If anything after the
swap fails, `app/current` goes back to the release before and that one is restarted.** The newest
three releases are kept, and `papercastctl rollback` swaps to `app/previous` by hand (the
database keeps the newer release's migrations; the backup taken before has it as it was).

Why these choices:
- **The hub starts through `deploy/run_hub.py`**, not `python -m hub.app`: run as `-m`,
  app.py is the module `__main__` and the other modules import a second copy of it as
  `hub.app`, so app.py's `except HTTPError` misses the errors they raise and every 401, 404 or
  428 goes out as a 500 (found by the end-to-end test against the parts as they stood).
- **A venv without pip, filled by uv.** perov's Python 3.10 has no `ensurepip`
  (`python3.10-venv` is not installed), so `python3.10 -m venv --without-pip` and `uv pip
  install`, root's uv without a cache (nothing of the account's is used by root).
- **The code on the path by a `.pth`** through `app/current`, not `pip install -e`: the hub needs
  only `papercast_cli.common` (stdlib), and the `.pth` follows each swap.
- **ffmpeg** is the static 7.0.2 that `imageio-ffmpeg` 0.6.0 ships (the build papercast-voice
  encodes with), taken from the PyPI wheel because PyPI files never change, so the pinned sha256
  keeps holding. The hub does not need it; the voice has its own copy.
- **`HOME` for the voice is `cache/`.** The worker starts the voice with a cleaned environment
  (HOME, USER, LANG, PATH), so whatever its engines keep in a home lands in `cache/`, which the
  account can write; `/srv/papercast` itself it cannot.

### The voice (`install-voice.sh`, run by `install.sh --voice`)

Leo's `stacks/papercast/voice/install.sh --gpu breeze`, unmodified, run as papercast with
`PAPERCAST_VOICE_HOME=/srv/papercast/voice` (stibnite's stays in `/home/leo/papercast/voice`),
`UV_CACHE_DIR=cache/uv`, `UV_PYTHON_INSTALL_DIR=python/`: the orchestrator venv, Kokoro (CPU) and
Breeze TTS 2 (GPU, bf16 as on stibnite's A4000: perov has the same card), every weight file
checked against its pinned sha256. Then `voice.json` gets this machine's card only
(`hosts.bs1.enabled = false`) and its GPU slot lock under `voice/run/`. [first run 2026-09-28 as
leo: 3 min with perov's network, 13 GB, `need_mib` 10,720 (the sample's provisional 9,696 MiB
peak + 1 GiB; `measure-gpu` would replace it).]

**Voice test on perov** (2026-09-28, as leo, before the move): an 80-word script (heading + two
paragraphs, 3 chunks), the card idle (123 MiB used by the desktop, 0 %). Admitted after 20 s (3
idle polls), engine load and CUDA-graph capture 107 s (cold compiler caches: stibnite's was 46 s
warm), speech 28.8 s for 35.7 s of audio (real-time factor 0.81, stibnite 0.83), 9,032 MiB in use
at one reading while speaking, 164 s in all, the card back to 123 MiB after. The MP3: 35.7 s, 96
kbit/s mono 44.1 kHz; **-16.18 LUFS** by pyloudnorm on the decoded file (target -16 ± 0.5), true
peak -2.3 dBTP (limit -1); no chunk retry recorded in metrics.json. Then the same script
**through the worker** (`tools/voice_smoke.py`: the real worker and the real papercast-voice,
`deploy/tests/fake_hub.py` as the hub): claimed, phases preparing → waiting-for-gpu → speaking →
encoding reported, 90.7 s in all (engine load 34.6 s with the caches warm), the hub received
430,551 bytes of `audio/mpeg` (ID3-tagged), `X-Duration-S` 35.7, sha256 matching the voice's; the
page's sentences said "Speaking on val-perovskite GPU 0" (not "stibnite"). `papercastctl
voice-test` runs the same smoke as papercast inside the voice unit's sandbox (`systemd-run` with
its properties). Not measured: a full-length episode on perov (`measure-gpu`), listening by ear.

## The move off leo's account (`migrate-to-system.sh`, 2026-09-28)

`prepare` (while the old install ran): the account (`useradd --system`, no login shell, home
`/srv/papercast`, its own group; leo in the group), the folders, leo's `uv`, `ffmpeg` and
`cloudflared` (the pinned sha256) copied into `bin/`, settings and tokens copied into `etc/`, a
first copy of `data/` and `backups/`, uv's CPython 3.11.15 copied into `python/`, the voice
copied (`rsync -aH`, niced) and its three venvs pointed at the new paths (`pyvenv.cfg`, the
`python` links, the scripts' `#!` lines) and its Breeze compiler caches moved aside (they name
files under the old home, which the sandbox cannot see: the first voice test as papercast failed
at the engine load with `PermissionError: /home/leo/…/cache/inductor/…best_config`; rebuilt at the
first load instead), then `uv pip sync --dry-run` of each venv against the
voice's requirements, which must say "Would make no changes" (nothing downloaded again), then
`install.sh --system --no-start --voice`. `cutover`: stop leo's units, copy data, backups and
settings again, `install.sh --system`, the live checks; leo's units come back if the hub, the
public address or the tunnel fail. `finish`: leo's units disabled (their files kept in the old
folder's `user-units/`), the watchdog cron line removed if there is one, `~/papercast-group`
renamed to `~/papercast-group.migrated-<date>` with a README; `rollback` puts the old install back
with the data the new one has by then.

[run 2026-09-28 on perov: `prepare` copied the voice (14 GB), CPython and the data; all three
venvs "Would make no changes"; `install-voice.sh` as papercast re-checked every weight's sha256
and `papercast-voice info` said installed; `systemd-analyze verify` quiet. `cutover` at 20:21:53Z:
the new hub answered 38 s after the old one stopped (`hub.db` schema 4 -> 5 by the new release's
migration, a backup first); then the hub `GET /` -> 303 to `/signin` as papercast, the tunnel 4
connections registered, https://papercast.virtualatoms.org -> 303, `/signin` 200, the worker's
claims reaching the hub every 15 s (204: nothing queued), both timers set (03:31 and 04:15 BST),
`papercastctl backup-now` a new backup; `papercastctl tunnel-token` with the same token: the
tunnel back with a connection in 31 s (cloudflared drains for up to 30 s when stopped); the hub
SIGKILLed: back by itself (`NRestarts=1`), 303 again. **The voice as papercast** (`papercastctl
voice-test`, the card idle: 123 MiB, 0 %): the first try failed at the engine load on the copied
compiler caches (above); with them rebuilt, admitted after 20 s, engine load 87 s (cold caches),
speech 28.5 s for 35.7 s of audio, 142 s in all; the stand-in hub received 430,551 bytes of
`audio/mpeg`, sha256 matching, -16.18 LUFS, true peak -2.3 dBTP (the same bytes as leo's test in
the morning); the card back to 123 MiB. A second run with the rebuilt caches: 90.7 s in all, as leo's warm run. `papercast-layout.service` run by hand as
papercast fails in `hub/layout.py` (`simulate`: `IndexError: index 1 is out of bounds for axis 1
with size 1`, after a graph with 0 papers), the code's, not the sandbox's (the same on a copy of
`hub.db` outside it): the nightly layout fails until that is fixed. `finish` at 21:37Z: leo's
seven `pcg-*` unit files moved into `~/papercast-group.migrated-2026-09-28/user-units/`, none left
in `~/.config/systemd/user`; his crontab had no `pcg-` line (only his own `gpus-report`); linger
left on (his tmux, bkrelay relay and VS Code server run in session scopes and probably do not
need it, but that is not certain); nothing in the units or `etc/` names `/home`.]

## Running it

```bash
papercastctl status                   # units, the hub and the public address, tunnel connections,
                                      # the worker's last claim, timers, last backup, disk, GPU
papercastctl logs hub -f              # voice, tunnel, backup, layout, all (journalctl -u papercast-*)
papercastctl restart [hub|voice|tunnel|all]
papercastctl backup-now; papercastctl backups; papercastctl restore <name>
papercastctl signin-link EMAIL        # hub.auth bootstrap as papercast
papercastctl allow list|add|remove|import FILE
papercastctl tunnel-token FILE|-      # replace the token, restart the tunnel, check it registers
papercastctl email --host … --password-file -   # the SMTP account (cloudflare.sh email)
papercastctl voice-test               # a short Breeze sample as papercast in the voice unit's sandbox
cat /srv/papercast/data/episodes/<id>/voice/status.json   # the voice's own status (group papercast)
```
`papercastctl` runs itself under sudo; what touches the hub's data runs as the account (`sudo -u
papercast`). The logs are in the system journal (`journalctl -u papercast-hub`), which needs
sudo or the `adm` group; the voice's own logs are in the job directories, readable by the group.

### The user install (perov until 2026-09-28; `install.sh --user`)

The same code as leo, without sudo: `~/papercast-group/` (`app -> app-<time>/`, `venv/`,
`hub.env`, `worker.token`, `data/`, `voice/`), systemd user units `pcg-hub`, `pcg-voice`,
`pcg-tunnel`, `pcg-backup.timer`, `pcg-layout.timer` (`systemd/pcg-*`), static binaries in
`~/.local/bin`. It needs linger (`loginctl enable-linger`, root) to start at boot and survive
logout; without a usable `systemd --user` at all, `install.sh` puts `deploy/watchdog.sh` in the
crontab instead (every 2 min: start the hub and the worker if they are not running, `setsid nohup
nice`; and a nightly line for backup and layout). [run 2026-09-28 on perov: restarts, a SIGKILLed
hub back in 5 s, both timers, `uninstall.sh` and `install.sh` again, the watchdog by hand.]
`cloudflare.sh`, `uninstall.sh` and the tests work in both modes (`PCG_MODE=user|system`, else
the system mode wherever its units are installed).

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
- **Voices and timings** (hub/voices.py, SPEC section 9). A claim naming a voice goes into
  job.json as `voice` (a CPU voice with `"engine": "cpu"`); a claim for an episode voiced before
  (someone changed its voice) starts its job directory afresh. Before the MP3 the worker sends
  `out/timings.json` (`PUT /api/voice/<id>/timings`; a refusal never stops the audio), and the MP3
  says which voice it is in (`X-Voice`). Both need the group's papercast-voice with job.json
  `voice` (from this commit on): after syncing, `bash stacks/papercast-group/deploy/install.sh
  --voice` when no episode is being voiced.
- **Custom voice previews** (hub/customvoice.py, SPEC section 9) come to this worker as ordinary
  claims (`vp-<user id>`, the custom voice as the claim's `voice`), so nothing here changes for
  them; their job directories are `episodes/vp-<user id>/voice/`, one per person, reused for each
  preview.
- **The voice samples** for the page's lists: `python3 stacks/papercast-group/tools/make_voice_samples.py`
  (one clip per preset through papercast-voice, about two minutes each on the A4000 once it has
  the GPU; into `data/voices/`). **Timings for episodes voiced before**:
  `python3 stacks/papercast-group/tools/backfill_timings.py` (only where the job dir still has
  its chunk WAVs; `--dry-run` first).

Tests: `python3 -m unittest discover -s stacks/papercast-group/deploy/tests` (a fake hub, a fake
papercast-voice with the real one's files and ids; 14 tests, about 20 s) [run on stibnite and perov]; the same command runs `test_cloudflare.py` (5 tests) and `test_system_install.py` (16 tests: the rendered system units' paths and settings, `systemd-analyze verify`, and `papercastctl` and `cloudflare.sh` in system mode against a stand-in `/srv/papercast`, without sudo) [35 tests, 45 s, run on stibnite 2026-09-28].

## The quick tunnel (testing only; the user install)

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
different file there would be left alone and ours put in `~/papercast-group/bin/`. (The system install
has its own copy in `/srv/papercast/bin/`.)

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

## Production: papercast.virtualatoms.org (a Cloudflare tunnel, the hub's own sign-in)

Cloudflare only carries the traffic: no Cloudflare Access, no Google, no invite links. perov needs
no open port, since the tunnel connects out (checked 2026-09-28: `region1/2.v2.argotunnel.com` is
reachable from perov; only the free quick tunnel's `trycloudflare.com` is blocked by the college's
DNS). The domain's owner does one thing in his Cloudflare dashboard:

**The tunnel.** Zero Trust → Networks → Tunnels → Create a tunnel → Cloudflared, named
`papercast`. Public hostname: subdomain `papercast`, domain `virtualatoms.org`, service `HTTP` →
`127.0.0.1:8400` (the hub, next to cloudflared on perov; `PCG_PORT=8400` in `hub.env`). Cloudflare adds the DNS record itself
(`papercast CNAME <tunnel id>.cfargotunnel.com`, proxied); a record alone would not work, as the
tunnel must be in the same account as the domain. He sends the **tunnel token**. No Access
application: if one was made for this hostname earlier, delete it, or Cloudflare would ask for its
own login in front of the hub's.

Then on perov:

    sudo papercastctl tunnel-token -                      # paste the token, Enter, Ctrl-D (-> etc/tunnel.token, 0600)
    sudo papercastctl url --url https://papercast.virtualatoms.org
    sudo papercastctl auth password                       # yl6719@ic.ac.uk and a.ganose@ic.ac.uk, admins

(`papercastctl auth|url|email` run `deploy/cloudflare.sh` as root in the system mode;
`cloudflare.sh tunnel` there enables and restarts `papercast-tunnel` and checks it registered.)
[2026-09-28: the tunnel token was set and the tunnel up under leo's user install before the move;
the move copied the token, and the same tunnel registered 4 connections as papercast.]

`auth password` puts the two admins on the list (`--admin EMAIL`, repeated, names others), prints
each one's username, first password and a one-time 24-hour set-password link, restarts the hub
and checks that a browser without a session gets the sign-in page. `auth local` goes back to the
hub's own invite links; `cloudflare.sh status` shows the settings.

### Who can sign in (PCG_AUTH=password; hub/accounts.py)

- **The list** of allowed emails, Imperial only (`@ic.ac.uk`, `@imperial.ac.uk`). An admin adds
  someone in Settings → Users by typing the short code (the box ends in `@ic.ac.uk`; a full
  `@ic.ac.uk` address pasted works too). The account exists at once: username = the short code
  (yl6719@ic.ac.uk is `yl6719`), role contributor (they can upload from the CLI straight away;
  an admin can change the role), **first password = the short code**. Tell them. At the first
  sign-in the hub asks for a new password before anything else: every other request answers 403
  `must_change_password`, and approving a CLI device waits too. Users shows who still has the
  first password and when each person last signed in.
- **Removing** someone (⋯ → Remove…, then type `delete`) takes the email off the list and
  disables the account at once: its sessions and CLI tokens end; the episodes and graph edits
  they made stay, credited to them. Nobody can remove themselves or the last admin. Adding the
  address again brings the account back, with the password it had.
- **From the server**: `papercastctl allow list`, `allow add EMAIL... [--note TEXT]`, `allow
  remove EMAIL...`, `allow import FILE` (one email per line, `#` comments; `-` reads stdin), and
  `papercastctl signin-link EMAIL` (an admin: username, first password and a one-time 24-hour
  set-password link). They are `python3 -m hub.auth allow …` and `bootstrap EMAIL` run as papercast
  with the hub's settings; `import` reads the file as root and hands it over on stdin, since the
  account cannot read people's homes.
- **Sign-in** with the username or the full email and the password. Passwords are scrypt
  (n = 2^14, r = 8, p = 1, 16-byte salt, stored with their parameters: 44 ms on stibnite's Xeon;
  raise `SCRYPT_N` in accounts.py if perov is faster, and each hash is redone at its owner's next
  sign-in). At least 10 characters, no composition rules; common passwords and ones containing
  the username are refused. Sessions last 30 days (cookie `Secure` over https); a new password, a
  reset, Settings → Account → "Sign out everywhere", disabling or removal ends every session.
- **Limits.** 10 failed sign-ins per account and 20 per address in 15 minutes, then each try
  waits, doubling from 30 s up to 15 min; an unknown account costs the same scrypt and gets the
  same answer. Reset links: 3 per email and 10 per address an hour. The address is
  `CF-Connecting-IP`, trusted only on connections from 127.0.0.1 (cloudflared).
- **Forgot password.** The sign-in page asks for the email; if it is on the list, the hub emails a
  one-use link (1 hour; only its sha256 is stored). The page answers the same whoever is on the
  list. Without email set up, it says to ask an admin, and Users shows "asked for a new
  password"; an admin then makes a link (⋯ → Make a password link, 24 hours, copy it to them)
  or resets them to the first password (⋯ → Reset to the first password: their sessions and
  devices end).
- **The log.** Users → "Sign-ins and changes": sign-ins, failures, links asked for, sent and
  used, list changes (the newest 2,000 are kept).

### Email, for "forgot password" only

Everything works without it. A dedicated Gmail account with an app password works from perov
(`smtp.gmail.com` 587 and 465 are reachable):

1. Make the account (say `papercast.group@gmail.com`), turn on 2-Step Verification, then Google
   Account → Security → App passwords → make one named "papercast hub" (16 letters).
2. On perov:

       sudo papercastctl email --host smtp.gmail.com --port 587 --user papercast.group@gmail.com \
            --from papercast.group@gmail.com --password-file - --test yl6719@ic.ac.uk
                                                               # paste the app password, Enter, Ctrl-D

   That writes `PCG_SMTP_HOST`, `PCG_SMTP_PORT` (587 STARTTLS, or 465 TLS from the start; never
   in the clear), `PCG_SMTP_USER`, `PCG_SMTP_PASSWORD_FILE` and `PCG_SMTP_FROM` into `hub.env`
   (the password stays in its own 600 file), sends a test email and restarts the hub. The emails
   say they come from "papercast". A later check: `python3 -m hub.auth email-test ADDRESS`.
   The first link may land in Imperial's junk or quarantine folder: worth a look.

## Uninstall

```bash
sudo bash /srv/papercast/src/stacks/papercast-group/deploy/uninstall.sh              # units, papercastctl, code, venv; keeps data, etc, backups, voice
sudo bash /srv/papercast/src/stacks/papercast-group/deploy/uninstall.sh --voice      # also voice/, cache/, python/ (14 GB)
sudo bash /srv/papercast/src/stacks/papercast-group/deploy/uninstall.sh --everything # all of /srv/papercast and the account (asks)
```
In the user install the same script removes the `pcg-*` units and `~/papercast-group`'s code
(`--voice`, `--everything` likewise); uv, ffmpeg and cloudflared stay in `~/.local/bin`.

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
| `install.sh`, `install-voice.sh`, `uninstall.sh`, `lib.sh` | the install, both modes (pins for uv, ffmpeg, cloudflared in `lib.sh`) |
| `papercastctl` | the admin commands for the system install (into `/srv/papercast/bin`) |
| `migrate-to-system.sh` | the one-off move from leo's user install to the system install (2026-09-28) |
| `systemd/system/papercast-*` | the system units (`@H@` = `/srv/papercast`, `@USER@` = `papercast`) |
| `systemd/pcg-*.service`, `pcg-*.timer` | the user units (`@H@` = `~/papercast-group`) |
| `voice_worker.py` | the worker (stdlib, Python 3.10) |
| `run_hub.py` | how the hub is started (one copy of `hub.app`) |
| `watchdog.sh` | the no-systemd fallback of the user install (crontab) |
| `tunnel.sh` | the quick tunnel (user install) |
| `cloudflare.sh` | production: the named tunnel, the public address, the sign-in mode and its first admins, the SMTP account |
| `tests/` | `test_voice_worker.py` (with `fake_hub.py`, `fake_papercast_voice.py`); `test_cloudflare.py` (cloudflare.sh with a fake systemctl and curl); `test_system_install.py` (the system units, papercastctl, cloudflare.sh in system mode) |
| `../HANDOVER.md` | the guide for whoever looks after it (installed as `/srv/papercast/HANDOVER.md`) |
| `../tools/sync_to_perov.sh` | git archive of HEAD to `perov:~/papercast-src` (`--install`: and update) |
| `../tools/voice_smoke.py` | one short episode through the worker and the real voice, a fake hub |
| `../tests/e2e_test.py`, `../tests/fake_claude.py` | the end-to-end test |
