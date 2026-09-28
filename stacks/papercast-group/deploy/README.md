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
| `  hub.env` (0600) | `PCG_DATA`, `PCG_AUTH`, `PCG_SECRET` (generated once), `PCG_BIND=127.0.0.1`, `PCG_PORT=8480`, `PCG_PUBLIC_URL`, `PCG_ADMIN_EMAILS`, `PCG_WORKER_TOKEN_SHA256`; for "forgot password" emails `PCG_SMTP_HOST`, `PCG_SMTP_PORT`, `PCG_SMTP_USER`, `PCG_SMTP_PASSWORD_FILE`, `PCG_SMTP_FROM` |
| `  smtp.password` (0600) | the SMTP account's (app) password, when email is set up; never in `hub.env` |
| `  worker.token` (0600), `worker.env` (0600) | the voice worker's token (the hub keeps only its sha256) and settings |
| `  data/` | `hub.db` and `episodes/<episode_id>/` (the voice job is `episodes/<id>/voice/`); `search.db`, the search index (made from those two: deleted, the hub builds it again at its start; the backup leaves it out) |
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
The first admins: with password sign-in, `cloudflare.sh auth password` puts them on the list
(below); one more at any time is `cd ~/papercast-group/app/papercast-group && set -a && .
~/papercast-group/hub.env && set +a && ~/papercast-group/venv/bin/python -m hub.auth bootstrap
<email>` (prints the username, the first password and a one-time 24-hour set-password link).
Under local auth the same command prints a one-time sign-in link.

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
papercast-voice with the real one's files and ids; 14 tests, about 20 s) [run on stibnite and perov]; the same command runs `test_cloudflare.py` (5 tests, stibnite only).

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

## Production: papercast.virtualatoms.org (a Cloudflare tunnel, the hub's own sign-in)

Cloudflare only carries the traffic: no Cloudflare Access, no Google, no invite links. perov needs
no open port, since the tunnel connects out (checked 2026-09-28: `region1/2.v2.argotunnel.com` is
reachable from perov; only the free quick tunnel's `trycloudflare.com` is blocked by the college's
DNS). The domain's owner does one thing in his Cloudflare dashboard:

**The tunnel.** Zero Trust → Networks → Tunnels → Create a tunnel → Cloudflared, named
`papercast`. Public hostname: subdomain `papercast`, domain `virtualatoms.org`, service `HTTP` →
`127.0.0.1:8480` (the hub, next to cloudflared on perov). Cloudflare adds the DNS record itself
(`papercast CNAME <tunnel id>.cfargotunnel.com`, proxied); a record alone would not work, as the
tunnel must be in the same account as the domain. He sends the **tunnel token**. No Access
application: if one was made for this hostname earlier, delete it, or Cloudflare would ask for its
own login in front of the hub's.

Then on perov:

    umask 077; cat > ~/papercast-group/tunnel.token      # paste the token, Enter, Ctrl-D
    D=~/papercast-group/app/papercast-group/deploy
    bash $D/cloudflare.sh tunnel
    bash $D/cloudflare.sh url --url https://papercast.virtualatoms.org
    bash $D/cloudflare.sh auth password                  # yl6719@ic.ac.uk and a.ganose@ic.ac.uk, admins

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
- **From the server** (with the hub's env, as for bootstrap above): `python3 -m hub.auth allow
  list`, `allow add EMAIL... [--note TEXT]`, `allow remove EMAIL...`, `allow import FILE` (one
  email per line, `#` comments; `-` reads stdin).
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

       umask 077; cat > ~/papercast-group/smtp.password     # paste the app password, Enter, Ctrl-D
       bash $D/cloudflare.sh email --host smtp.gmail.com --port 587 --user papercast.group@gmail.com \
            --from papercast.group@gmail.com --test yl6719@ic.ac.uk

   That writes `PCG_SMTP_HOST`, `PCG_SMTP_PORT` (587 STARTTLS, or 465 TLS from the start; never
   in the clear), `PCG_SMTP_USER`, `PCG_SMTP_PASSWORD_FILE` and `PCG_SMTP_FROM` into `hub.env`
   (the password stays in its own 600 file), sends a test email and restarts the hub. The emails
   say they come from "papercast". A later check: `python3 -m hub.auth email-test ADDRESS`.
   The first link may land in Imperial's junk or quarantine folder: worth a look.

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
| `cloudflare.sh` | production: the named tunnel, the public address, the sign-in mode and its first admins, the SMTP account |
| `tests/` | the worker's tests: `fake_hub.py`, `fake_papercast_voice.py`, `test_voice_worker.py`; `test_cloudflare.py` (cloudflare.sh with a fake systemctl and curl) |
| `../tools/sync_to_perov.sh` | rsync of the repo to perov |
| `../tools/voice_smoke.py` | one short episode through the worker and the real voice, a fake hub |
| `../tests/e2e_test.py`, `../tests/fake_claude.py` | the end-to-end test |
