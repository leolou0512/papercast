# papercast-group: handover

The group's paper podcasts, https://papercast.virtualatoms.org. It runs on **perov**
(val-perovskite, Imperial; tailnet address 100.97.205.90) as its own system account,
`papercast`, so it does not depend on anyone's login. Everything is in `/srv/papercast`, and
one command, `papercastctl`, does the usual jobs (it asks for your sudo password).

To read the files and logs without sudo, be in the group `papercast`:
`sudo usermod -aG papercast YOUR_LOGIN` (from your next login).

## What runs

| unit | what |
|---|---|
| `papercast-hub` | the web page, the command line's API and the voice queue, on 127.0.0.1:8400 only |
| `papercast-voice` | the voice worker: voices finished scripts with Breeze TTS 2 on perov's GPU, one at a time, and waits while anyone else is computing on the card |
| `papercast-tunnel` | cloudflared: the only way in from the internet. The domain owner's Cloudflare dashboard sends papercast.virtualatoms.org to http://127.0.0.1:8400 |
| `papercast-backup.timer` | 03:30 every night: a backup of the data (the newest 14 kept) |
| `papercast-layout.timer` | 04:15 every night: the graphs' full layout |

All run as `papercast`, niced, restarted if they stop, and sandboxed (they cannot read anyone's
home, write outside `/srv/papercast`, or gain privileges). They start at boot.

| in /srv/papercast | what |
|---|---|
| `data/` | the hub's database `hub.db` and each episode's files |
| `backups/` | the nightly backups, one folder per night |
| `etc/` | settings (`hub.env`, `worker.env`) and secrets (`worker.token`, `tunnel.token`, `smtp.password`): 0600, readable by `papercast` only |
| `app/current` | the running code (`app/releases/<time>/`); `app/previous` is the one before |
| `src/` | the source that release was installed from |
| `voice/` | the voice (papercast-voice, Breeze, Kokoro; 14 GB with its models) |
| `bin/` | `papercastctl`, `cloudflared`, `ffmpeg`, `uv` |
| `venv/`, `python/`, `cache/`, `worker/` | the hub's Python, the voice's Python, caches, the episode being voiced |

## Is it healthy?

    papercastctl status

Healthy means: the three units `active`; the hub and the public address answer `401` or `303` (a
visitor without a session is sent to sign in); the tunnel has at least one connection (usually
4); the voice is idle or holding an episode; the last backup is from last night. Then:

    papercastctl logs hub            # or voice, tunnel, backup, layout, all; -f to follow
    papercastctl restart             # all three; or restart hub | voice | tunnel
    papercastctl voice-test          # a short Breeze sample through the real worker (2-3 min; card idle)

The logs are in the system journal (`journalctl -u papercast-hub` and so on). Each episode's voice
log is in `data/episodes/<id>/voice/`.

## People and admins

Sign-in is the hub's own: a password, for the emails on its list (Imperial addresses only).
Admins manage people on the page itself: **Settings → Users** (add, remove, change a role, reset
a password). From the server:

    papercastctl allow list
    papercastctl allow add zz123@ic.ac.uk [--note "who"]      # username zz123, first password zz123
    papercastctl allow remove zz123@ic.ac.uk                  # their sessions and CLI tokens end
    papercastctl allow import FILE                            # one email per line
    papercastctl signin-link zz123@ic.ac.uk                   # make them an admin; prints their username,
                                                              # first password and a 24-hour link to set one

Send what `signin-link` prints to that person only. A new person changes the first password at
their first sign-in. If sign-in is not on passwords yet (`status` shows the mode),
`papercastctl auth password --admin YOU@ic.ac.uk` switches it on and makes you an admin.

"Forgot password" emails need an SMTP account (optional; without one an admin makes a link in
Users). With a Gmail account and its app password:

    papercastctl email --host smtp.gmail.com --port 587 --user X@gmail.com --from X@gmail.com \
        --password-file - --test YOU@ic.ac.uk                # paste the app password, Enter, Ctrl-D

## Update the code

The code is the git repository https://github.com/leolou0512/papercast (private: ask Leo, or
whoever owns it after him, for access or to transfer it). From a checkout of it:

    bash stacks/papercast-group/tools/sync_to_perov.sh YOU@100.97.205.90 --install

That copies the committed code to `~/papercast-src` on perov and runs
`sudo papercastctl update ~/papercast-src`. The update keeps the data and settings, takes a backup,
installs a new release, restarts, and goes back to the release before by itself if the hub does not
come up. To go back by hand: `papercastctl rollback`. Without the repository, the installed source
is in `/srv/papercast/src`: copy it, change it, `sudo papercastctl update THAT_COPY`.

cloudflared is pinned in `deploy/lib.sh`. Cloudflare supports each release for about a year: to
update it, put the new `cloudflared-linux-amd64` in `/srv/papercast/bin/cloudflared` (mode 755,
root) and `papercastctl restart tunnel`; updates keep a working one there.

## Tunnel token

The token belongs to the tunnel `papercast` in the domain owner's Cloudflare account (Zero Trust →
Networks → Tunnels). If it leaks or is refreshed there:

    papercastctl tunnel-token FILE        # or - and paste it, Enter, Ctrl-D

It replaces `etc/tunnel.token`, restarts the tunnel (up to 30 s: cloudflared lets requests finish)
and checks it connects; if it does not, the old token is kept aside and the message says how to
put it back.

## Backups and restore

    papercastctl backup-now
    papercastctl backups
    papercastctl restore 2026-10-01T023512Z      # the hub stops for a moment; the data before is kept
                                                 # as /srv/papercast/data.before-restore-<time>

Backups are on perov's own disk: copy `/srv/papercast/backups` elsewhere now and then.

## If perov is replaced

1. On the old machine: `sudo systemctl stop papercast-hub papercast-voice papercast-tunnel`.
2. Copy all of `/srv/papercast` to the same path on the new machine, as root
   (`sudo rsync -aH /srv/papercast/ root@NEW:/srv/papercast/`, about 15 GB with the voice).
3. On the new machine (Ubuntu with systemd, python3.10 and the NVIDIA driver):
   `sudo bash /srv/papercast/src/stacks/papercast-group/deploy/install.sh --system`.
   That makes the account, fixes the owners, installs the units and starts them. Add `--voice` if
   the voice does not start (another GPU or system).
4. `papercastctl status`, then `papercastctl voice-test`. Nothing changes in Cloudflare: the tunnel
   follows its token.

To remove it all: `sudo bash /srv/papercast/src/stacks/papercast-group/deploy/uninstall.sh`
(`--everything` also deletes the data and the account). Details: `deploy/README.md`.
