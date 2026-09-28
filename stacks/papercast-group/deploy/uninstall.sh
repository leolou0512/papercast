#!/usr/bin/env bash
# Undo deploy/install.sh: stop and remove the user units (or the crontab watchdog), then remove
# the code and venv. Data is kept unless asked, because it is the group's episodes.
#
#   bash deploy/uninstall.sh                 units, code, venv; keeps data/, hub.env, the token,
#                                            voice/ (15 GB, reusable), backups/
#   bash deploy/uninstall.sh --voice         also voice/ and voice-cache/
#   bash deploy/uninstall.sh --everything    all of $PCG_HOME (data included: asks to type yes)
#
# uv, ffmpeg and cloudflared in ~/.local/bin stay (other things may use them); the tunnel is
# stopped if it is up.
[ -n "${BASH_VERSION:-}" ] || exec bash "$0" "$@"
set -euo pipefail
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
. "$HERE/lib.sh"
H=$PCG_HOME
VOICE="" ALL=""
case "${1:-}" in
    --voice) VOICE=1 ;;
    --everything) VOICE=1; ALL=1 ;;
    "") ;;
    *) echo "usage: uninstall.sh [--voice | --everything]" >&2; exit 2 ;;
esac

[ -x "$HERE/tunnel.sh" ] && PCG_HOME=$H bash "$HERE/tunnel.sh" down >/dev/null 2>&1 || true

if have_user_systemd; then
    say "systemd --user units"
    systemctl --user disable --now pcg-hub.service pcg-voice.service pcg-backup.timer pcg-layout.timer 2>/dev/null || true
    systemctl --user stop pcg-tunnel.service 2>/dev/null || true
    rm -f "$HOME"/.config/systemd/user/pcg-*.service "$HOME"/.config/systemd/user/pcg-*.timer
    systemctl --user daemon-reload
    systemctl --user reset-failed 2>/dev/null || true
fi
if crontab -l 2>/dev/null | grep -q '# pcg-'; then
    say "crontab watchdog lines"
    crontab -l | grep -v '# pcg-watchdog$' | grep -v '# pcg-nightly$' | crontab -
fi
[ -x "$H/app/papercast-group/deploy/watchdog.sh" ] && PCG_HOME=$H bash "$H/app/papercast-group/deploy/watchdog.sh" stop || true

say "code and venv"
rm -rf "$H"/app "$H"/app-2* "$H/venv" "$H/run" "$H/worker/worker.lock"
if [ -n "$VOICE" ]; then
    say "voice"
    rm -rf "$H/voice" "$H/voice-cache" "$H/voice-test"
fi
if [ -n "$ALL" ]; then
    printf 'Remove ALL of %s, the hub database and every episode included? Type yes: ' "$H"
    read -r ans
    [ "$ans" = yes ] || die "kept $H"
    rm -rf "$H"
    echo "removed $H"
else
    echo "kept: $(ls -1 "$H" 2>/dev/null | tr '\n' ' ')"
fi
