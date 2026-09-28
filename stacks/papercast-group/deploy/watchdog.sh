#!/usr/bin/env bash
# The fallback when systemd --user cannot run the units (no user manager, no linger): leo's
# crontab runs this every two minutes and it starts the hub and the voice worker if they are not
# running, detached (setsid nohup), niced, logging to $PCG_HOME/logs/. install.sh adds the lines.
#
#   watchdog.sh            start whatever is not running
#   watchdog.sh restart    stop both, then start them (after an install)
#   watchdog.sh stop       stop both
#   watchdog.sh nightly    the backup and the layout, as the timers would (guarded the same way)
#
# A process is ours when its pid file names a live process of this user whose command line is
# the one we started; nothing else is ever signalled.
set -uo pipefail
H=${PCG_HOME:-$HOME/papercast-group}
APP=$H/app/papercast-group
PYV=$H/venv/bin/python
mkdir -p "$H/run" "$H/logs"

is_ours() {     # is_ours <name> <needle>: the pid in run/<name>.pid, if it is still ours
    local pid
    pid=$(cat "$H/run/$1.pid" 2>/dev/null) || return 1
    [ -n "$pid" ] && [ -d "/proc/$pid" ] || return 1
    [ "$(stat -c %u "/proc/$pid")" = "$(id -u)" ] || return 1
    tr '\0' ' ' < "/proc/$pid/cmdline" | grep -q -- "$2" || return 1
    echo "$pid"
}

start() {       # start <name> <env file> <needle> <command...>
    local name=$1 envf=$2 needle=$3
    shift 3
    if is_ours "$name" "$needle" >/dev/null; then return 0; fi
    (
        set -a; . "$envf"; set +a
        export PYTHONUNBUFFERED=1
        cd "$APP" || exit 1
        umask 077
        setsid nohup nice -n 10 "$@" >> "$H/logs/$name.log" 2>&1 < /dev/null &
        echo $! > "$H/run/$name.pid"
    )
    echo "$(date -u +%FT%TZ) started $name (pid $(cat "$H/run/$name.pid"))"
}

stop() {        # stop <name> <needle>: TERM the process group we started, then KILL
    local pid
    pid=$(is_ours "$1" "$2") || return 0
    kill -TERM -- "-$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null
    for _ in $(seq 1 40); do [ -d "/proc/$pid" ] || break; sleep 1; done
    [ -d "/proc/$pid" ] && kill -KILL -- "-$pid" 2>/dev/null
    rm -f "$H/run/$1.pid"
    echo "$(date -u +%FT%TZ) stopped $1 (pid $pid)"
}

up() {
    start hub "$H/hub.env" "run_hub.py" "$PYV" "$APP/deploy/run_hub.py"
    start voice "$H/worker.env" "voice_worker.py" "$PYV" "$APP/deploy/voice_worker.py"
}

down() {
    stop voice "voice_worker.py"
    stop hub "run_hub.py"
}

case "${1:-}" in
    "") up ;;
    restart) down; up ;;
    stop) down ;;
    nightly)
        (set -a; . "$H/hub.env"; set +a; cd "$APP" || exit 1
         if [ -f tools/backup.py ]; then nice -n 19 "$PYV" tools/backup.py --data "$PCG_DATA" --dest "$H/backups"
         else echo "tools/backup.py is not installed yet: nothing backed up"; fi
         if grep -q "__main__" hub/layout.py; then nice -n 15 "$PYV" -m hub.layout --all
         else echo "hub/layout.py has no command line yet: nothing laid out"; fi) ;;
    *) echo "usage: watchdog.sh [restart|stop|nightly]" >&2; exit 2 ;;
esac
