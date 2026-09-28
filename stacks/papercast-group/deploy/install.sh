#!/usr/bin/env bash
# Install papercast-group on this machine (perov: val-perovskite), as leo, without sudo, from a
# git checkout or an rsync of the repo (tools/sync_to_perov.sh). Safe to re-run: it replaces the
# code, keeps the data, the secret and the worker token, and restarts what runs.
#
#   bash stacks/papercast-group/deploy/install.sh            hub + voice worker + nightly timers
#   bash stacks/papercast-group/deploy/install.sh --voice    the same, and (re)install the voice
#                                                            (papercast-voice + Breeze, ~15 GB)
#   options: --no-start (install only)  --public-url URL  --admin EMAIL[,EMAIL]
#
# Everything goes under $PCG_HOME (default /home/leo/papercast-group, mode 700):
#   app -> app-<time>/{papercast-group, papercast-cli, VERSION}   the code, swapped in one rename
#   venv/            python3.10, numpy; app/papercast-group and app/papercast-cli on its path (.pth)
#   data/            PCG_DATA: hub.db, episodes/<id>/ (voice job dirs under episodes/<id>/voice/)
#   hub.env          the hub's settings (600): PCG_SECRET generated once, PCG_WORKER_TOKEN_SHA256
#   worker.token     the voice worker's token (600); worker.env its settings (600)
#   voice/           papercast-voice (deploy/install-voice.sh)
#   worker/ logs/ backups/ run/
# plus systemd user units in ~/.config/systemd/user/ (pcg-hub, pcg-voice, pcg-backup.timer,
# pcg-layout.timer), static uv and ffmpeg in ~/.local/bin when missing (checksums pinned in lib.sh).
# Without a usable systemd --user it falls back to deploy/watchdog.sh from leo's crontab.
[ -n "${BASH_VERSION:-}" ] || exec bash "$0" "$@"
set -euo pipefail
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
. "$HERE/lib.sh"
PG=$(cd "$HERE/.." && pwd)                       # stacks/papercast-group
REPO=$(cd "$PG/../.." && pwd)
CLI=$REPO/packages/papercast-cli
H=$PCG_HOME
PY=${PCG_PYTHON:-python3.10}

VOICE="" START=1 PUBLIC_URL="" ADMINS=""
while [ $# -gt 0 ]; do
    case "$1" in
        --voice) VOICE=1; shift ;;
        --no-start) START=""; shift ;;
        --public-url) PUBLIC_URL=${2:?}; shift 2 ;;
        --admin) ADMINS=${2:?}; shift 2 ;;
        *) echo "usage: install.sh [--voice] [--no-start] [--public-url URL] [--admin EMAIL]" >&2; exit 2 ;;
    esac
done

[ "$(id -u)" != 0 ] || die "run as your own user (leo), not root: this is a per-user install"
command -v "$PY" >/dev/null || die "$PY not found (the hub targets Python 3.10)"
[ -f "$PG/hub/app.py" ] || die "no hub at $PG/hub"
[ -d "$CLI/papercast_cli" ] || die "no packages/papercast-cli at $CLI"
export PATH="$BIN:$PATH"

say "disk"; df -h / | tail -1
umask 077
mkdir -p "$H" "$H/data" "$H/data/episodes" "$H/logs" "$H/worker" "$H/backups" "$H/run"
chmod 700 "$H"

ensure_uv

say "code -> $H/app"
rev=$(git -C "$REPO" describe --always --dirty 2>/dev/null || cat "$REPO/.revision" 2>/dev/null || echo unknown)
stamp=$(date -u +%Y%m%dT%H%M%SZ)
new="$H/app-$stamp"
rm -rf "$new"
mkdir -p "$new"
cp -a "$PG" "$new/papercast-group"
cp -a "$CLI" "$new/papercast-cli"
find "$new" -name __pycache__ -prune -exec rm -rf {} +
echo "$rev (installed $(date -u +%Y-%m-%dT%H:%M:%SZ) from $REPO)" > "$new/VERSION"
ln -sfn "app-$stamp" "$H/app.link"
mv -T "$H/app.link" "$H/app"
ls -1dt "$H"/app-2* 2>/dev/null | tail -n +4 | xargs -r rm -rf      # keep the last three

say "venv ($PY)"
if [ ! -x "$H/venv/bin/python" ]; then
    if "$PY" -c 'import ensurepip' 2>/dev/null; then "$PY" -m venv "$H/venv"
    else "$PY" -m venv --without-pip "$H/venv"       # no python3.10-venv here: uv fills it
    fi
fi
uv pip install -q --python "$H/venv/bin/python" 'numpy==2.2.6'
site=$("$H/venv/bin/python" -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')
printf '%s\n%s\n' "$H/app/papercast-group" "$H/app/papercast-cli" > "$site/papercast_group.pth"
"$H/venv/bin/python" -c 'import hub.app, hub.config, papercast_cli.common.checks, numpy, sys
print("python", sys.version.split()[0], "numpy", numpy.__version__, "hub", hub.app.__file__)'

say "settings"
E=$H/hub.env W=$H/worker.env T=$H/worker.token
touch "$E" "$W"; chmod 600 "$E" "$W"
if [ ! -s "$T" ]; then
    "$H/venv/bin/python" -c 'import secrets; print("pcgw_" + secrets.token_urlsafe(32))' > "$T"
fi
chmod 600 "$T"
tok_sha=$("$H/venv/bin/python" -c 'import hashlib,sys; print(hashlib.sha256(open(sys.argv[1]).read().strip().encode()).hexdigest())' "$T")
port=$(get_env "$E" PCG_PORT); port=${port:-8480}
[ -n "$(get_env "$E" PCG_SECRET)" ] || set_env "$E" PCG_SECRET "$("$H/venv/bin/python" -c 'import secrets; print(secrets.token_urlsafe(48))')"
[ -n "$(get_env "$E" PCG_DATA)" ] || set_env "$E" PCG_DATA "$H/data"
[ -n "$(get_env "$E" PCG_AUTH)" ] || set_env "$E" PCG_AUTH local
[ -n "$(get_env "$E" PCG_BIND)" ] || set_env "$E" PCG_BIND 127.0.0.1
[ -n "$(get_env "$E" PCG_PORT)" ] || set_env "$E" PCG_PORT "$port"
[ -z "$PUBLIC_URL" ] || set_env "$E" PCG_PUBLIC_URL "$PUBLIC_URL"
[ -n "$(get_env "$E" PCG_PUBLIC_URL)" ] || set_env "$E" PCG_PUBLIC_URL "http://127.0.0.1:$port"
[ -z "$ADMINS" ] || set_env "$E" PCG_ADMIN_EMAILS "$ADMINS"
grep -q '^PCG_ADMIN_EMAILS=' "$E" || set_env "$E" PCG_ADMIN_EMAILS ""
set_env "$E" PCG_WORKER_TOKEN_SHA256 "$tok_sha"
set_env "$W" PCG_HUB_URL "http://127.0.0.1:$port"
set_env "$W" PCG_WORKER_TOKEN_FILE "$T"
set_env "$W" PAPERCAST_VOICE_CMD "$H/voice/bin/papercast-voice"
set_env "$W" PCG_VOICE_JOBS "$(get_env "$E" PCG_DATA)/episodes"
set_env "$W" PCG_WORKER_STATE "$H/worker"
grep -q '^PCG_VOICE_ENGINE=' "$W" || set_env "$W" PCG_VOICE_ENGINE auto
sed -e 's/^\(PCG_SECRET=\).*/\1<set>/' "$E"

( ensure_ffmpeg ) || echo "ffmpeg: not installed (the hub does not need it; the voice has its own)"

if [ -n "$VOICE" ]; then
    PCG_HOME=$H bash "$HERE/install-voice.sh"
elif [ ! -x "$H/voice/bin/papercast-voice" ]; then
    echo "note: no voice at $H/voice yet (install.sh --voice); the worker takes no episode until then"
fi

if have_user_systemd; then
    say "systemd --user units (linger: $(linger))"
    U=$HOME/.config/systemd/user
    mkdir -p "$U"
    for f in "$HERE"/systemd/pcg-*; do
        sed "s|@H@|$H|g" "$f" > "$U/$(basename "$f").new"
        mv "$U/$(basename "$f").new" "$U/$(basename "$f")"
    done
    systemctl --user daemon-reload
    systemctl --user enable -q pcg-hub.service pcg-voice.service pcg-backup.timer pcg-layout.timer
    [ "$(linger)" = yes ] || echo "WARNING: Linger is off for $(id -un): the units stop at logout and do not start at boot (loginctl enable-linger needs root)"
    if [ -n "$START" ]; then
        systemctl --user restart pcg-hub.service pcg-voice.service
        systemctl --user start pcg-backup.timer pcg-layout.timer
    fi
else
    say "no systemd --user here: the watchdog from crontab"
    line="*/2 * * * * $H/app/papercast-group/deploy/watchdog.sh >> $H/logs/watchdog.log 2>&1 # pcg-watchdog"
    nightly="30 3 * * * $H/app/papercast-group/deploy/watchdog.sh nightly >> $H/logs/watchdog.log 2>&1 # pcg-nightly"
    (crontab -l 2>/dev/null | grep -v '# pcg-watchdog$' | grep -v '# pcg-nightly$'; echo "$line"; echo "$nightly") | crontab -
    [ -z "$START" ] || PCG_HOME=$H bash "$H/app/papercast-group/deploy/watchdog.sh" restart
fi

if [ -n "$START" ]; then
    say "self-check"
    ok=""
    for _ in $(seq 1 30); do
        code=$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$port/" || true)
        if [ -n "$code" ] && [ "$code" != 000 ]; then ok=1; break; fi
        sleep 1
    done
    [ -n "$ok" ] || die "the hub does not answer on 127.0.0.1:$port (journalctl --user -u pcg-hub)"
    echo "hub answers on 127.0.0.1:$port (GET / -> $code)"
fi
echo
echo "Installed $rev into $H."
