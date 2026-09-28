#!/usr/bin/env bash
# Install papercast-group on this machine from a git checkout or a synced copy of the repo
# (tools/sync_to_perov.sh). Safe to re-run: it puts the new code in, keeps the data, the
# settings, the secret and the tokens, and restarts what runs.
#
# Two modes (lib.sh):
#   --system   perov (val-perovskite): the default there. The service account `papercast`, all of
#              it under /srv/papercast, units papercast-* in /etc/systemd/system. Runs itself
#              under sudo. A new release goes into app/releases/<time>/ and app/current points at
#              it; if the self-check after the restart fails, current goes back to the release
#              before and that one is restarted.
#   --user     leo's own install without sudo (~/papercast-group, systemd --user units pcg-*, the
#              crontab watchdog without a user manager): how perov ran until 2026-09-28.
#
#   bash stacks/papercast-group/deploy/install.sh            hub + voice worker + tunnel + timers
#   bash stacks/papercast-group/deploy/install.sh --voice    the same, and (re)install the voice
#                                                            (papercast-voice + Breeze, ~15 GB)
#   options: --no-start (install only)  --public-url URL  --admin EMAIL[,EMAIL] (PCG_ADMIN_EMAILS)
#            --admin-user USER (system: puts a login in the group papercast; repeatable)
#            --account-only (system: the account and the folders, nothing else)
#            --render-units DIR (write the system units for /srv/papercast into DIR and stop;
#                                no root, nothing else touched: what the tests check)
#
# System layout (/srv/papercast, root:papercast 0750; root owns what root runs, the account owns
# what it writes, so nothing the service could change is ever run by root):
#   app/releases/<time>/{papercast-group,papercast-cli,VERSION}, app/current -> the running one,
#     app/previous -> the one before (root)
#   venv/        python3.10 + numpy for the hub; app/current/* on its path by a .pth (root)
#   bin/         uv, ffmpeg, cloudflared, papercastctl (root; /usr/local/bin/papercastctl links here)
#   src/         the tree this release was installed from (root; `papercastctl update` re-runs it)
#   etc/         hub.env, worker.env, worker.token, tunnel.token, smtp.password: 0600 papercast
#   data/        PCG_DATA: hub.db, episodes/<id>/ (papercast; group-readable)
#   backups/     nightly (papercast)       voice/   papercast-voice + Breeze + Kokoro (papercast)
#   cache/       the account's HOME for tools: uv, huggingface, CUDA (papercast)
#   python/      the uv-managed Python 3.11 the voice's venvs run on (papercast)
#   worker/ logs/                          (papercast)
[ -n "${BASH_VERSION:-}" ] || exec bash "$0" "$@"
set -euo pipefail
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
. "$HERE/lib.sh"
PG=$(cd "$HERE/.." && pwd)                       # stacks/papercast-group
REPO=$(cd "$PG/../.." && pwd)
CLI=$REPO/packages/papercast-cli
PYHUB=${PCG_PYTHON:-python3.10}
ARGS=("$@")

VOICE="" START=1 PUBLIC_URL="" ADMINS="" ADMIN_USERS="" WANT="" ACCOUNT_ONLY="" RENDER=""
while [ $# -gt 0 ]; do
    case "$1" in
        --voice) VOICE=1; shift ;;
        --no-start) START=""; shift ;;
        --public-url) PUBLIC_URL=${2:?}; shift 2 ;;
        --admin) ADMINS=${2:?}; shift 2 ;;
        --admin-user) ADMIN_USERS="$ADMIN_USERS ${2:?}"; shift 2 ;;
        --system) WANT=system; shift ;;
        --user) WANT=user; shift ;;
        --account-only) ACCOUNT_ONLY=1; shift ;;
        --render-units) RENDER=${2:?}; shift 2 ;;
        *) echo "usage: install.sh [--system|--user] [--voice] [--no-start] [--public-url URL] [--admin EMAIL]" \
                "[--admin-user USER] [--account-only] [--render-units DIR]" >&2; exit 2 ;;
    esac
done

if [ -n "$RENDER" ]; then
    render_units "$HERE/systemd/system" "$PCG_SYS_HOME" "$RENDER"
    echo "rendered the system units for $PCG_SYS_HOME into $RENDER"
    exit 0
fi

# The mode: asked for, or the one installed here, or system on perov, else user.
if [ -z "$WANT" ]; then
    WANT=${PCG_MODE:-}
    if [ -z "$WANT" ]; then
        if [ -f "$PCG_SYS_UNITS/papercast-hub.service" ] || [ "$(hostname -s)" = val-perovskite ]; then WANT=system
        else WANT=user
        fi
    fi
fi

# ---- system mode -------------------------------------------------------------------------------

sys_account_and_dirs() {
    say "account $PCG_USER, folders under $H"
    getent group "$PCG_USER" >/dev/null || groupadd --system "$PCG_USER"
    if ! id -u "$PCG_USER" >/dev/null 2>&1; then
        useradd --system --gid "$PCG_USER" --home-dir "$H" --no-create-home \
            --shell /usr/sbin/nologin --comment "papercast-group service" "$PCG_USER"
    fi
    local u d f
    for u in $ADMIN_USERS; do
        id -u "$u" >/dev/null 2>&1 || die "no user $u"
        usermod -aG "$PCG_USER" "$u"
        echo "$u is in the group $PCG_USER (reads data and backups; from their next login)"
    done
    mkdir -p "$H"
    chown root:"$PCG_USER" "$H"; chmod 0750 "$H"
    for d in app etc; do
        mkdir -p "$H/$d"; chown root:"$PCG_USER" "$H/$d"; chmod 0750 "$H/$d"
    done
    mkdir -p "$H/app/releases" "$H/bin"
    chown root:root "$H/app/releases" "$H/bin"; chmod 0755 "$H/app/releases" "$H/bin"
    for d in data data/episodes backups voice cache worker logs python; do
        mkdir -p "$H/$d"; chown "$PCG_USER:$PCG_USER" "$H/$d"; chmod 0750 "$H/$d"
    done
    # A copy from another machine (perov replaced) has that machine's numbers for the owner.
    for d in data backups voice cache worker logs python; do
        find "$H/$d" -xdev ! -user "$PCG_USER" -exec chown -h "$PCG_USER:$PCG_USER" {} + 2>/dev/null || true
    done
    for f in "$H"/etc/*; do
        [ -f "$f" ] || continue
        chown "$PCG_USER:$PCG_USER" "$f"; chmod 0600 "$f"
    done
}

# The tree to install from, as root's copy in src/: committed files only when it is a git
# checkout (git archive HEAD, the three trees the hub uses: a working tree can hold ignored
# secrets), else the synced copy as it is.
sys_source() {
    if [ "$REPO" = "$H/src" ]; then
        rev=$(cat "$H/src/.revision" 2>/dev/null || echo unknown)
        return 0
    fi
    say "source -> $H/src"
    local t
    t=$(mktemp -d "$H/src.new.XXXX")
    if git -C "$REPO" rev-parse --git-dir >/dev/null 2>&1; then
        git -C "$REPO" diff --quiet HEAD -- stacks/papercast-group packages/papercast-cli stacks/papercast/voice ||
            echo "note: uncommitted changes in $REPO are not installed (HEAD is)"
        git -C "$REPO" archive HEAD stacks/papercast-group packages/papercast-cli stacks/papercast/voice | tar -x -C "$t"
        echo "$(git -C "$REPO" rev-parse --short HEAD) ($(git -C "$REPO" rev-parse --abbrev-ref HEAD), from $REPO)" > "$t/.revision"
    else
        mkdir -p "$t/stacks/papercast" "$t/packages"
        cp -a "$PG" "$t/stacks/papercast-group"
        cp -a "$CLI" "$t/packages/papercast-cli"
        [ ! -d "$REPO/stacks/papercast/voice" ] || cp -a "$REPO/stacks/papercast/voice" "$t/stacks/papercast/voice"
        cat "$REPO/.revision" > "$t/.revision" 2>/dev/null || echo "unknown (from $REPO)" > "$t/.revision"
    fi
    find "$t" -name __pycache__ -prune -exec rm -rf {} +
    chown -R root:root "$t"; chmod -R u=rwX,go=rX "$t"
    rm -rf "$H/src.old"
    [ ! -d "$H/src" ] || mv "$H/src" "$H/src.old"
    mv "$t" "$H/src"
    rm -rf "$H/src.old"
    rev=$(cat "$H/src/.revision")
}

# After app/current moved to the new release: if anything below fails, it goes back.
SWAPPED="" PREV="" OK=""
sys_swap_back() {
    [ -n "$SWAPPED" ] && [ -z "$OK" ] || return 0
    if [ -n "$PREV" ] && [ -d "$H/app/$PREV" ]; then
        echo "ROLLBACK: app/current -> $PREV again" >&2
        ln -sfn "$PREV" "$H/app/current.new"; mv -T "$H/app/current.new" "$H/app/current"
        if [ -n "$START" ]; then
            systemctl restart "$U_HUB.service" "$U_VOICE.service" || true
            local i c=000
            for i in $(seq 1 20); do c=$(http_code "http://127.0.0.1:$(hub_port)/"); [ "$c" = 000 ] || break; sleep 1; done
            echo "after the rollback: $U_HUB $(systemctl is-active "$U_HUB" || true), GET / -> $c" >&2
        fi
    else
        echo "no release before this one to go back to" >&2
    fi
}

install_system() {
    use_mode system
    [ "$(id -u)" = 0 ] || exec sudo -- bash "$0" --system "${ARGS[@]}"
    command -v systemctl >/dev/null || die "no systemctl here"
    umask 022
    sys_account_and_dirs
    if [ -n "$ACCOUNT_ONLY" ]; then echo "account and folders ready"; return 0; fi
    command -v "$PYHUB" >/dev/null || die "$PYHUB not found (the hub targets Python 3.10)"
    [ -f "$PG/hub/app.py" ] || die "no hub at $PG/hub"
    [ -d "$CLI/papercast_cli" ] || die "no packages/papercast-cli at $CLI"
    say "disk"; df -h "$H" | tail -1

    say "binaries in $BIN"
    ensure_uv
    ( ensure_ffmpeg ) || echo "ffmpeg: not installed (the hub does not need it; the voice has its own)"
    ensure_cloudflared

    sys_source
    local SRC=$H/src stamp rel site
    stamp=$(date -u +%Y%m%dT%H%M%SZ)
    rel=$H/app/releases/$stamp
    say "release $rev -> $rel"
    rm -rf "$rel"; mkdir -p "$rel"
    cp -a "$SRC/stacks/papercast-group" "$rel/papercast-group"
    cp -a "$SRC/packages/papercast-cli" "$rel/papercast-cli"
    echo "$rev (installed $(date -u +%Y-%m-%dT%H:%M:%SZ))" > "$rel/VERSION"

    say "venv ($PYHUB)"
    if [ ! -x "$H/venv/bin/python" ]; then
        if "$PYHUB" -c 'import ensurepip' 2>/dev/null; then "$PYHUB" -m venv "$H/venv"
        else "$PYHUB" -m venv --without-pip "$H/venv"      # no python3.10-venv on perov: uv fills it
        fi
    fi
    # (root's uv runs without a cache: nothing of the account's is ever used by root; numpy comes
    # from PyPI even when the voice's install is asked to stay offline)
    env -u UV_OFFLINE UV_NO_CACHE=1 "$BIN/uv" pip install -q --python "$H/venv/bin/python" 'numpy==2.2.6'
    site=$("$H/venv/bin/python" -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')
    printf '%s\n%s\n' "$H/app/current/papercast-group" "$H/app/current/papercast-cli" > "$site/papercast_group.pth"
    "$H/venv/bin/python" -m compileall -q "$rel" >/dev/null || true    # the account cannot write there
    chown -R root:root "$rel" "$H/venv"; chmod -R u=rwX,go=rX "$rel"

    PREV=$(readlink "$H/app/current" 2>/dev/null || true)
    trap sys_swap_back EXIT
    ln -sfn "releases/$stamp" "$H/app/current.new"; mv -T "$H/app/current.new" "$H/app/current"
    SWAPPED=1
    if [ -n "$PREV" ] && [ "$PREV" != "releases/$stamp" ]; then
        ln -sfn "$PREV" "$H/app/previous.new"; mv -T "$H/app/previous.new" "$H/app/previous"
    fi
    as_pc "$PY" -c 'import hub.app, hub.config, papercast_cli.common.checks, numpy, sys
print("python", sys.version.split()[0], "numpy", numpy.__version__, "hub", hub.app.__file__)'

    say "settings in $ETC"
    local E=$ETC/hub.env W=$ETC/worker.env T=$ETC/worker.token port f tok_sha
    for f in "$E" "$W"; do
        [ -f "$f" ] || : > "$f"
        chown "$PCG_USER:$PCG_USER" "$f"; chmod 600 "$f"
    done
    if [ ! -s "$T" ]; then
        (umask 077; "$PY" -c 'import secrets; print("pcgw_" + secrets.token_urlsafe(32))' > "$T")
    fi
    chown "$PCG_USER:$PCG_USER" "$T"; chmod 600 "$T"
    tok_sha=$("$PY" -c 'import hashlib,sys; print(hashlib.sha256(open(sys.argv[1]).read().strip().encode()).hexdigest())' "$T")
    port=$(hub_port)
    [ -n "$(get_env "$E" PCG_SECRET)" ] || set_env "$E" PCG_SECRET "$("$PY" -c 'import secrets; print(secrets.token_urlsafe(48))')"
    set_env "$E" PCG_DATA "$H/data"
    [ -n "$(get_env "$E" PCG_AUTH)" ] || set_env "$E" PCG_AUTH local
    [ -n "$(get_env "$E" PCG_BIND)" ] || set_env "$E" PCG_BIND 127.0.0.1
    [ -n "$(get_env "$E" PCG_PORT)" ] || set_env "$E" PCG_PORT "$port"
    [ -z "$PUBLIC_URL" ] || set_env "$E" PCG_PUBLIC_URL "$PUBLIC_URL"
    [ -n "$(get_env "$E" PCG_PUBLIC_URL)" ] || set_env "$E" PCG_PUBLIC_URL "http://127.0.0.1:$port"
    [ -z "$ADMINS" ] || set_env "$E" PCG_ADMIN_EMAILS "$ADMINS"
    grep -q '^PCG_ADMIN_EMAILS=' "$E" || set_env "$E" PCG_ADMIN_EMAILS ""
    [ -z "$(get_env "$E" PCG_SMTP_PASSWORD_FILE)" ] || set_env "$E" PCG_SMTP_PASSWORD_FILE "$ETC/smtp.password"
    set_env "$E" PCG_WORKER_TOKEN_SHA256 "$tok_sha"
    set_env "$W" PCG_HUB_URL "http://127.0.0.1:$port"
    set_env "$W" PCG_WORKER_TOKEN_FILE "$T"
    set_env "$W" PAPERCAST_VOICE_CMD "$H/voice/bin/papercast-voice"
    set_env "$W" PCG_VOICE_JOBS "$H/data/episodes"
    set_env "$W" PCG_WORKER_STATE "$H/worker"
    grep -q '^PCG_VOICE_ENGINE=' "$W" || set_env "$W" PCG_VOICE_ENGINE auto
    for f in "$ETC"/*; do
        [ -f "$f" ] || continue
        chown "$PCG_USER:$PCG_USER" "$f"; chmod 600 "$f"
    done
    if grep -n '/home/' "$E" "$W"; then
        die "the settings above still name a path in /home, which the services cannot see (ProtectHome)"
    fi
    sed -e 's/^\(PCG_SECRET=\).*/\1<set>/' -e 's/^\(PCG_WORKER_TOKEN_SHA256=\).*/\1<set>/' "$E"

    say "database"
    if [ -s "$H/data/hub.db" ] && [ -f "$APP/tools/backup.py" ]; then
        # a backup first: the migrations change hub.db, and a release rolled back keeps them
        hub_py "$APP/tools/backup.py" --data "$H/data" --dest "$H/backups" | tail -1 ||
            die "the backup before the update failed"
    fi
    hub_py -m hub.db migrate

    if [ -n "$VOICE" ]; then
        say "voice (as $PCG_USER)"
        as_pc env PCG_MODE=system PCG_HOME="$H" PCG_BIN="$BIN" PCG_VOICE_CACHE="$H/cache/voice" \
            HF_HOME="$H/cache/huggingface" bash "$SRC/stacks/papercast-group/deploy/install-voice.sh"
    elif [ ! -x "$H/voice/bin/papercast-voice" ]; then
        echo "note: no voice at $H/voice yet (install.sh --voice); the worker takes no episode until then"
    fi

    say "units in $PCG_SYS_UNITS, papercastctl, HANDOVER.md"
    render_units "$rel/papercast-group/deploy/systemd/system" "$H" "$PCG_SYS_UNITS"
    install -m 0755 -o root -g root "$rel/papercast-group/deploy/papercastctl" "$BIN/papercastctl"
    ln -sfn "$BIN/papercastctl" /usr/local/bin/papercastctl
    install -m 0644 -o root -g root "$rel/papercast-group/HANDOVER.md" "$H/HANDOVER.md"
    systemctl daemon-reload
    systemctl enable -q "$U_HUB.service" "$U_VOICE.service" "$U_BACKUP.timer" "$U_LAYOUT.timer"
    if [ -s "$ETC/tunnel.token" ]; then systemctl enable -q "$U_TUNNEL.service"
    else echo "note: no $ETC/tunnel.token: the tunnel is not enabled (papercastctl tunnel-token FILE)"
    fi

    if [ -n "$START" ]; then
        systemctl restart "$U_HUB.service" "$U_VOICE.service"
        systemctl start "$U_BACKUP.timer" "$U_LAYOUT.timer"
        [ ! -s "$ETC/tunnel.token" ] || systemctl start "$U_TUNNEL.service"
        say "self-check"
        local code="" i
        for i in $(seq 1 30); do
            code=$(http_code "http://127.0.0.1:$port/")
            case "$code" in 000|"") sleep 1 ;; *) break ;; esac
        done
        case "$code" in
            000|""|5*) die "the hub does not answer on 127.0.0.1:$port (GET / -> ${code:-nothing}; journalctl -u $U_HUB)" ;;
        esac
        sleep 3
        systemctl is-active -q "$U_HUB" || die "$U_HUB is not running (journalctl -u $U_HUB)"
        systemctl is-active -q "$U_VOICE" || die "$U_VOICE is not running (journalctl -u $U_VOICE)"
        echo "hub answers on 127.0.0.1:$port (GET / -> $code); $U_VOICE $(systemctl is-active "$U_VOICE")"
    fi
    OK=1
    # Keep the newest three releases, and whichever current and previous point at.
    local keep r
    keep=" $(readlink "$H/app/current") $(readlink "$H/app/previous" 2>/dev/null || true) "
    for r in $(ls -1dt "$H"/app/releases/2* 2>/dev/null | tail -n +4); do
        case "$keep" in *" releases/$(basename "$r") "*) ;; *) rm -rf "$r" ;; esac
    done
    echo
    echo "Installed $rev into $H (app/current -> releases/$stamp${PREV:+; before: $PREV})."
}

# ---- user mode (leo's own, without sudo; perov until 2026-09-28) ------------------------------

install_user() {
H=$PCG_HOME
PY=$PYHUB
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

# The database: migrations, then what a new hub starts with (base prompt v1, the five topic
# graphs), once; A1's `python -m hub.db migrate`, which the hub itself does not run.
if grep -q 'def main' "$H/app/papercast-group/hub/db.py"; then
    say "database"
    (set -a; . "$E"; set +a; cd "$H/app/papercast-group" && "$H/venv/bin/python" -m hub.db migrate)
else
    echo "note: hub/db.py has no command line yet: the hub makes its tables at start, nothing is seeded"
fi

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
}

case "$WANT" in
    system) install_system ;;
    user) use_mode user; install_user ;;
    *) die "the mode is --system or --user, not $WANT" ;;
esac
