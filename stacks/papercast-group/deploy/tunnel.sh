#!/usr/bin/env bash
# A Cloudflare quick tunnel to the hub, for testing: no account, a random
# https://<words>.trycloudflare.com address, taken down after a few minutes. Only with fake or no
# data: anyone who has the address reaches the hub (its local auth still asks for a session).
# The production path (a named tunnel on Leo's domain with Cloudflare Access) is in README.md.
#
#   bash deploy/tunnel.sh up [--minutes N]   start it (default 15 minutes, 0 = until `down`),
#                                            print the address, set PCG_PUBLIC_URL, restart the hub
#   bash deploy/tunnel.sh down               stop it; PCG_PUBLIC_URL goes back to what it was
#   bash deploy/tunnel.sh status
#
# cloudflared: the official static build, pinned with its checksum (lib.sh); --no-autoupdate, so
# it never replaces the binary. Detached (setsid, nice 10); log in $PCG_HOME/logs/tunnel.log.
# On perov it does not work: Imperial's resolvers answer trycloudflare.com with their phishing
# block page (README "The quick tunnel"), and this script does not go around that.
[ -n "${BASH_VERSION:-}" ] || exec bash "$0" "$@"
set -euo pipefail
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
. "$HERE/lib.sh"
H=$PCG_HOME
E=$H/hub.env
RUN=$H/run
LOG=$H/logs/tunnel.log
CFLOG=$RUN/tunnel.cf.log
mkdir -p "$RUN" "$H/logs"
port=$(get_env "$E" PCG_PORT); port=${port:-8480}

restart_hub() {
    if have_user_systemd && systemctl --user cat pcg-hub.service >/dev/null 2>&1; then
        systemctl --user restart pcg-hub.service
    elif [ -x "$H/app/papercast-group/deploy/watchdog.sh" ]; then
        PCG_HOME=$H bash "$H/app/papercast-group/deploy/watchdog.sh" restart >/dev/null
    fi
}

alive() { [ -n "${1:-}" ] && [ -d "/proc/$1" ] && [ "$(stat -c %u "/proc/$1")" = "$(id -u)" ]; }
stamp() { date -u +%FT%TZ; }

# The address in hub.env changes only under this lock, so an ending tunnel and a starting `up`
# never leave the other's value behind.
lock() { exec 9>"$RUN/tunnel.lock"; flock 9; }
unlock() { flock -u 9; }

restore_url() {          # the value from before the tunnel; true when it changed anything
    lock
    local changed=""
    if [ -f "$RUN/tunnel.prev_url" ]; then
        if [ -f "$RUN/tunnel.url" ]; then
            set_env "$E" PCG_PUBLIC_URL "$(cat "$RUN/tunnel.prev_url")"
            changed=1
        fi
        rm -f "$RUN/tunnel.prev_url" "$RUN/tunnel.url"
    fi
    unlock
    if [ -n "$changed" ]; then
        restart_hub
        echo "$(stamp) PCG_PUBLIC_URL back to $(get_env "$E" PCG_PUBLIC_URL)" >> "$LOG"
    fi
}

case "${1:-}" in
up)
    minutes=15
    [ "${2:-}" != --minutes ] || minutes=${3:?}
    if alive "$(cat "$RUN/tunnel.pid" 2>/dev/null || true)"; then
        die "a tunnel is already up: $(cat "$RUN/tunnel.url" 2>/dev/null || echo '(no address yet)')"
    fi
    curl -s -o /dev/null -m 5 "http://127.0.0.1:$port/" || die "the hub does not answer on 127.0.0.1:$port"
    if [ -n "${PCG_CLOUDFLARED:-}" ]; then CLOUDFLARED=$PCG_CLOUDFLARED     # tests: a fake
    else ensure_cloudflared
    fi
    lock
    [ -f "$RUN/tunnel.prev_url" ] || get_env "$E" PCG_PUBLIC_URL > "$RUN/tunnel.prev_url"
    rm -f "$RUN/tunnel.url"
    unlock
    echo "$(stamp) up for ${minutes} min with $CLOUDFLARED ($("$CLOUDFLARED" --version 2>&1 | head -1))" >> "$LOG"
    : > "$CFLOG"
    setsid nohup bash "$HERE/tunnel.sh" _run "$minutes" "$CLOUDFLARED" >> "$LOG" 2>&1 < /dev/null &
    pid=$!
    echo "$pid" > "$RUN/tunnel.pid"
    url=""
    for _ in $(seq 1 60); do
        url=$(grep -o 'https://[a-z0-9-]*\.trycloudflare\.com' "$CFLOG" 2>/dev/null |
              grep -v '^https://api\.' | head -1 || true)
        [ -n "$url" ] && break
        alive "$pid" || break
        sleep 1
    done
    lock
    if [ -z "$url" ] || ! alive "$pid"; then
        unlock
        grep -iE 'error|failed|ERR' "$CFLOG" | tail -5 >&2 || tail -5 "$CFLOG" >&2 || true
        bash "$HERE/tunnel.sh" down >/dev/null 2>&1 || true
        die "no tunnel: see $CFLOG"
    fi
    echo "$url" > "$RUN/tunnel.url"
    set_env "$E" PCG_PUBLIC_URL "$url"
    unlock
    restart_hub
    echo "$(stamp) address $url" >> "$LOG"
    echo "$url"
    if [ "$minutes" != 0 ]; then echo "(down by itself after $minutes min; now: bash $0 down)" >&2; fi
    ;;
_run)
    # Detached: cloudflared for at most $2 minutes, then the address is taken back.
    minutes=$2 cf=$3
    lim=()
    [ "$minutes" = 0 ] || lim=(timeout --signal=TERM --kill-after=20 "$((minutes * 60))")
    nice -n 10 "${lim[@]}" "$cf" tunnel --no-autoupdate --url "http://127.0.0.1:$port" > "$CFLOG" 2>&1 &
    echo $! > "$RUN/tunnel.child"
    wait || true
    rm -f "$RUN/tunnel.child"
    echo "$(stamp) tunnel ended" >> "$LOG"
    restore_url
    rm -f "$RUN/tunnel.pid"
    ;;
down)
    pid=$(cat "$RUN/tunnel.pid" 2>/dev/null || true)
    child=$(cat "$RUN/tunnel.child" 2>/dev/null || true)
    if alive "$child"; then kill -TERM "$child" 2>/dev/null || true; fi
    for _ in $(seq 1 60); do alive "$pid" || break; sleep 0.5; done
    if alive "$pid"; then kill -TERM -- "-$pid" 2>/dev/null || true; fi
    # Anything left of ours running cloudflared for this port (never anyone else's).
    for p in $(pgrep -u "$(id -u)" -f "tunnel --no-autoupdate --url http://127.0.0.1:$port" || true); do
        kill -TERM "$p" 2>/dev/null || true
    done
    restore_url
    rm -f "$RUN/tunnel.pid" "$RUN/tunnel.child" "$RUN/tunnel.url" "$RUN/tunnel.prev_url"
    echo "tunnel down; PCG_PUBLIC_URL=$(get_env "$E" PCG_PUBLIC_URL)"
    ;;
status)
    if alive "$(cat "$RUN/tunnel.pid" 2>/dev/null || true)"; then
        echo "up: $(cat "$RUN/tunnel.url" 2>/dev/null || echo '(no address yet)')"
    else
        echo "down"
    fi
    echo "PCG_PUBLIC_URL=$(get_env "$E" PCG_PUBLIC_URL)"
    ;;
*)
    echo "usage: tunnel.sh up [--minutes N] | down | status" >&2; exit 2 ;;
esac
