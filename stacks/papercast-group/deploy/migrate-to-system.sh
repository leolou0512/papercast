#!/usr/bin/env bash
# Once, on perov (2026-09-28): move papercast-group from leo's user install (~/papercast-group,
# systemd --user units pcg-*) to the system install (the account papercast, /srv/papercast, units
# papercast-*), so it keeps running after leo's account goes. Run as leo, from the synced tree
# (tools/sync_to_perov.sh); it uses sudo itself. Copies, never moves: the old install stays whole
# until `finish` renames it, and `rollback` brings it back.
#
#   bash deploy/migrate-to-system.sh prepare    while the old one runs: the account and folders,
#                                               the binaries, the voice (17 GB, copied, nothing
#                                               downloaded), a first copy of the data, settings and
#                                               tokens, the release and the units (not started)
#   bash deploy/migrate-to-system.sh cutover    stop the old units, copy the data and settings
#                                               again, install.sh --system (starts and self-checks),
#                                               then the live checks; the old units come back if
#                                               the hub, the public address or the tunnel fail
#   bash deploy/migrate-to-system.sh check      the live checks again
#   bash deploy/migrate-to-system.sh finish     disable the old units, remove the watchdog cron
#                                               line, rename ~/papercast-group to
#                                               ~/papercast-group.migrated-<date> with a README
#   bash deploy/migrate-to-system.sh rollback   the old user install again (before or after finish),
#                                               with the data the new one has by then
[ -n "${BASH_VERSION:-}" ] || exec bash "$0" "$@"
set -euo pipefail
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
. "$HERE/lib.sh"
OLD=${PCG_OLD_HOME:-$HOME/papercast-group}
S=$PCG_SYS_HOME
PUBLIC=https://papercast.virtualatoms.org
USER_UNITS="pcg-hub.service pcg-voice.service pcg-tunnel.service pcg-backup.timer pcg-layout.timer"
SYS_UNITS="papercast-hub.service papercast-voice.service papercast-tunnel.service papercast-backup.timer papercast-layout.timer"
[ "$(id -u)" != 0 ] || die "run as leo (it uses sudo where it needs to)"
sudo -n true 2>/dev/null || die "needs sudo without a password prompt here"
export XDG_RUNTIME_DIR=${XDG_RUNTIME_DIR:-/run/user/$(id -u)}

pc() {      # as the account, clean environment, from /
    (cd / && sudo -u "$PCG_USER" -- env -i HOME="$S/cache" PATH="$S/bin:/usr/bin:/bin" LANG=C.UTF-8 "$@")
}
rs() {      # rsync as root, niced, owned by the account
    sudo nice -n 19 ionice -c3 rsync -a --chown="$PCG_USER:$PCG_USER" "$@"
}

copy_etc() {
    local f n=0
    for f in hub.env worker.env worker.token tunnel.token smtp.password; do
        [ -f "$OLD/$f" ] || continue
        sudo install -m 0600 -o "$PCG_USER" -g "$PCG_USER" "$OLD/$f" "$S/etc/$f"
        n=$((n + 1))
    done
    echo "settings and tokens: $n file(s) -> $S/etc (0600 $PCG_USER; install.sh rewrites their paths)"
}

copy_data() {   # copy_data [--delete]: data/ (group-readable), backups/, the worker's state
    rs --chmod=Dg+rx,Fg+r,o-rwx ${1:-} "$OLD/data/" "$S/data/"
    rs -H --chmod=Dg+rx,Fg+r,o-rwx "$OLD/backups/" "$S/backups/"
    if [ -f "$OLD/worker/current.json" ]; then rs "$OLD/worker/current.json" "$S/worker/current.json"
    else sudo rm -f "$S/worker/current.json"
    fi
    echo "data: $(sudo du -sh "$S/data" | cut -f1) in $S/data, backups: $(sudo ls "$S/backups" | grep -c '^2' || true)"
}

# The voice's venvs run on a uv-managed Python 3.11 (perov's own is 3.10): copy it, point the
# venvs at the copy, and rewrite the absolute paths in their scripts. install-voice.sh then
# re-checks everything (weights by sha256) and rewrites the wrapper and the C compiler shim.
relocate_voice() {
    local home minor real
    home=$(sed -n 's/^home = //p' "$OLD/voice/venv/pyvenv.cfg")       # .../cpython-3.11-linux-x86_64-gnu/bin
    minor=$(dirname "$home")
    real=$(readlink -f "$minor")
    [ -x "$real/bin/python3.11" ] || die "no Python at $real"
    say "python $(basename "$real") -> $S/python"
    rs "$real/" "$S/python/$(basename "$real")/"
    pc ln -sfn "$S/python/$(basename "$real")" "$S/python/$(basename "$minor")"
    say "voice $(du -sh "$OLD/voice" | cut -f1) -> $S/voice (niced)"
    rs -H --delete "$OLD/voice/" "$S/voice/"
    say "the voice's venvs -> the new paths"
    # shellcheck disable=SC2016
    pc bash -c '
        set -euo pipefail
        old=$1 new=$2 py=$3
        for v in "$new/venv" "$new"/engines/*/venv; do
            [ -f "$v/pyvenv.cfg" ] || continue
            ln -sfn "$py/bin/python3.11" "$v/bin/python"
            sed -i "s|^home = .*|home = $py/bin|" "$v/pyvenv.cfg"
            { grep -rlIZ -- "$old" "$v/bin" || true; } | xargs -0 -r sed -i "s|$old|$new|g"
            find "$v/lib" -maxdepth 3 -name "*.pth" -print0 | { xargs -0 -r grep -lIZ -- "$old" || true; } |
                xargs -0 -r sed -i "s|$old|$new|g"
            "$v/bin/python" -c "import sys; assert sys.prefix == \"$v\", sys.prefix"
            echo "  $v: $("$v/bin/python" -c "import sys; print(sys.version.split()[0], sys.prefix)")"
        done
        left=$({ grep -rlI -- "$old" "$new/venv/bin" "$new"/engines/*/venv/bin "$new"/engines/*/venv/pyvenv.cfg 2>/dev/null || true; } | wc -l)
        [ "$left" = 0 ] || { echo "still naming $old: $left file(s)" >&2; exit 1; }
    ' _ "$OLD/voice" "$S/voice" "$S/python/$(basename "$minor")"
}

prepare() {
    [ -d "$OLD" ] || die "no $OLD"
    say "disk"; df -h "$S" /
    sudo bash "$HERE/install.sh" --system --account-only --admin-user "$(id -un)"
    say "binaries: $S/bin"
    local b
    for b in uv uvx ffmpeg; do
        [ -x "$HOME/.local/bin/$b" ] && sudo install -m 0755 -o root -g root "$HOME/.local/bin/$b" "$S/bin/$b"
    done
    if [ "$(sha256sum "$HOME/.local/bin/cloudflared" | cut -d' ' -f1)" = "$CLOUDFLARED_SHA256" ]; then
        sudo install -m 0755 -o root -g root "$HOME/.local/bin/cloudflared" "$S/bin/cloudflared"
    fi
    sudo ls -l "$S/bin"
    copy_etc
    copy_data --delete
    relocate_voice
    say "install.sh --system --no-start --voice (offline: the voice's packages must all be there)"
    sudo env UV_OFFLINE=1 bash "$HERE/install.sh" --system --no-start --voice
    say "units"
    sudo systemd-analyze verify "$PCG_SYS_UNITS"/papercast-*.service "$PCG_SYS_UNITS"/papercast-*.timer 2>&1 |
        grep -v -e '^$' || echo "systemd-analyze verify: nothing to say"
    echo
    echo "Prepared. Next: papercastctl voice-test (the GPU as $PCG_USER), then $0 cutover."
}

stop_old() {
    systemctl --user stop pcg-tunnel.service pcg-voice.service pcg-hub.service pcg-backup.timer pcg-layout.timer
}
start_old() {
    systemctl --user start pcg-hub.service pcg-voice.service pcg-tunnel.service pcg-backup.timer pcg-layout.timer
}

# The live checks; prints each, returns 1 when the hub, the public address or the tunnel fail.
check() {
    local bad="" c p n i inv
    c=$(http_code "http://127.0.0.1:8400/")
    echo "hub 127.0.0.1:8400: GET / -> $c ($(systemctl is-active papercast-hub), user $(systemctl show -p User --value papercast-hub))"
    case "$c" in 401|303|200) ;; *) bad=1 ;; esac
    n=0
    for i in $(seq 1 30); do
        inv=$(systemctl show -p InvocationID --value papercast-tunnel)
        n=$(sudo journalctl --no-pager -o cat "_SYSTEMD_INVOCATION_ID=$inv" | grep -c 'Registered tunnel connection' || true)
        [ "$n" -ge 1 ] && break
        sleep 1
    done
    echo "tunnel: $n connection(s) registered ($(systemctl is-active papercast-tunnel))"
    [ "$n" -ge 1 ] || bad=1
    p=000
    for i in $(seq 1 10); do p=$(http_code "$PUBLIC/"); case "$p" in 401|303|200) break ;; esac; sleep 3; done
    echo "$PUBLIC: GET / -> $p"
    case "$p" in 401|303|200) ;; *) bad=1 ;; esac
    inv=$(systemctl show -p InvocationID --value papercast-hub)
    for i in $(seq 1 25); do
        sudo journalctl --no-pager -o cat "_SYSTEMD_INVOCATION_ID=$inv" | grep -q 'POST /api/voice/claim' && break
        sleep 1
    done
    echo "voice worker: $(systemctl is-active papercast-voice); claims seen by the hub since it started:" \
         "$(sudo journalctl --no-pager -o cat "_SYSTEMD_INVOCATION_ID=$inv" | grep -c 'POST /api/voice/claim' || true)" \
         "(last: $(sudo journalctl --no-pager -o cat "_SYSTEMD_INVOCATION_ID=$inv" | grep 'POST /api/voice/claim' | tail -1 | sed 's/.*"POST/"POST/'))"
    systemctl list-timers --no-pager papercast-backup.timer papercast-layout.timer | sed -n '1,3p'
    [ -z "$bad" ]
}

cutover() {
    [ -x "$S/venv/bin/python" ] && [ -L "$S/app/current" ] || die "run prepare first"
    [ ! -f "$OLD/worker/current.json" ] || die "the old worker is voicing an episode ($OLD/worker/current.json): wait until it is done"
    local t0 t1
    t0=$(date +%s)
    say "stop the old units ($(date -u +%H:%M:%SZ))"
    stop_old
    copy_data --delete
    copy_etc
    if ! sudo bash "$HERE/install.sh" --system; then
        echo "install.sh --system failed: the old units again" >&2
        sudo systemctl stop $SYS_UNITS || true
        start_old
        die "cut-over abandoned; the old install runs (check: curl -sI $PUBLIC)"
    fi
    t1=$(date +%s)
    say "live checks (the hub answered $((t1 - t0)) s after the old one stopped)"
    if ! check; then
        echo "a check failed: the old units again" >&2
        sudo systemctl stop $SYS_UNITS || true
        start_old
        die "cut-over abandoned; the old install runs"
    fi
    echo
    echo "Cut over. Next: papercastctl backup-now, papercastctl voice-test, then $0 finish."
}

finish() {
    [ -d "$OLD" ] || die "no $OLD (already finished?)"
    systemctl is-active -q papercast-hub || die "papercast-hub is not running: not finishing"
    local U=$HOME/.config/systemd/user dest
    dest=$OLD.migrated-$(date +%F)
    say "the old user units: disabled, their files kept in $dest/user-units/"
    systemctl --user disable --now $USER_UNITS 2>/dev/null || true
    mkdir -p "$OLD/user-units"
    mv "$U"/pcg-*.service "$U"/pcg-*.timer "$OLD/user-units/" 2>/dev/null || true
    systemctl --user daemon-reload
    systemctl --user reset-failed 2>/dev/null || true
    if crontab -l 2>/dev/null | grep -q '# pcg-'; then
        crontab -l | grep -v '# pcg-watchdog$' | grep -v '# pcg-nightly$' | crontab -
        echo "removed the pcg- lines from leo's crontab"
    else
        echo "leo's crontab has no pcg- line (nothing to remove)"
    fi
    mv "$OLD" "$dest"
    cat > "$dest/README.md" <<EOF
# Rollback copy of papercast-group (leo's user install) — delete after $(date -d '+7 days' +%F)

Until $(date +%F) papercast-group ran from here as leo (systemd --user units pcg-*). It now runs as
the system account \`papercast\` from /srv/papercast (units papercast-*, \`papercastctl status\`,
/srv/papercast/HANDOVER.md). This folder is the old install, whole, as it was at the cut-over:
code, venv, data, backups, settings and tokens (0600), the 17 GB voice, and the old unit files in
user-units/.

Nothing uses it. If the new install works for a week, delete it:

    rm -rf $dest

To go back to it instead (with the data the new install has by then):

    bash $S/src/stacks/papercast-group/deploy/migrate-to-system.sh rollback
EOF
    say "linger for $(id -un): $(linger) (left as it is)"
    echo "Finished: $dest (README.md in it)."
}

rollback() {
    local U=$HOME/.config/systemd/user m
    if [ ! -d "$OLD" ]; then
        m=$(ls -1d "$OLD".migrated-* 2>/dev/null | tail -1 || true)
        [ -n "$m" ] || die "no $OLD and no $OLD.migrated-*: nothing to go back to"
        mv "$m" "$OLD"
    fi
    say "stop the system units"
    sudo systemctl disable --now $SYS_UNITS || true
    say "the data the new install has -> $OLD/data (the old copy kept aside)"
    if [ -f "$S/data/hub.db" ]; then
        mv "$OLD/data" "$OLD/data.before-rollback-$(date -u +%Y%m%dT%H%M%SZ)"
        sudo rsync -a --chown="$(id -un):$(id -gn)" --chmod=go-rwx "$S/data/" "$OLD/data/"
    fi
    if [ -d "$OLD/user-units" ]; then
        mkdir -p "$U"; cp -a "$OLD"/user-units/pcg-* "$U/"
    fi
    systemctl --user daemon-reload
    systemctl --user enable --now $USER_UNITS
    sleep 5
    echo "old hub: GET / -> $(http_code http://127.0.0.1:8400/); $PUBLIC -> $(http_code "$PUBLIC/")"
}

case "${1:-}" in
    prepare) prepare ;;
    cutover) cutover ;;
    check) check ;;
    finish) finish ;;
    rollback) rollback ;;
    *) sed -n '2,24p' "$0"; exit 2 ;;
esac
