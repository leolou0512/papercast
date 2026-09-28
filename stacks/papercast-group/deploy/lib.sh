# Shared by install.sh, install-voice.sh, cloudflare.sh, uninstall.sh, tunnel.sh and papercastctl
# (sourced, not run).
#
# Two ways to run papercast-group, the "mode":
#   system  perov since 2026-09-28: the service account `papercast` (no login), everything under
#           /srv/papercast, units papercast-* in /etc/systemd/system; installed and run by sudo.
#   user    before that, and anywhere without sudo: leo's systemd --user units pcg-* and
#           ~/papercast-group; binaries in ~/.local/bin.
#
# Each download is pinned to one release and its sha256 (taken from the publisher on 2026-09-28:
# the release's .sha256 file, GitHub's asset digest, PyPI's digest), so a changed file stops the
# install instead of running.

PCG_HOME=${PCG_HOME:-$HOME/papercast-group}
BIN=${PCG_BIN:-$HOME/.local/bin}
MODE=${PCG_MODE:-user}                                 # use_mode sets it (below)
PCG_USER=${PCG_USER:-papercast}                         # the service account (system mode)
PCG_SYS_HOME=${PCG_SYS_HOME:-/srv/papercast}
PCG_SYS_UNITS=${PCG_SYS_UNITS:-/etc/systemd/system}

die() { echo "${0##*/}: $*" >&2; exit 1; }
say() { echo "== $*"; }

# fetch <url> <sha256> <out>: download to <out>.part, verify, rename.
fetch() {
    local url=$1 sum=$2 out=$3
    curl -fsSL --retry 3 -m 600 -o "$out.part" "$url" || die "download failed: $url"
    local got
    got=$(sha256sum "$out.part" | cut -d' ' -f1)
    if [ "$got" != "$sum" ]; then
        rm -f "$out.part"
        die "checksum mismatch for $url: got $got, pinned $sum"
    fi
    mv "$out.part" "$out"
}

# uv (astral-sh/uv 0.11.33, the version on stibnite): builds the venvs. perov's python3.10 has no
# ensurepip (python3.10-venv is not installed, and installing it needs sudo), so the hub's venv is
# made without pip and filled by uv; papercast-voice's own install needs uv anyway.
UV_VERSION=0.11.33
UV_SHA256=aa9fca823c03289fb6e3460b3dc864f3ea895cafaf9b99247701a67b17d1b018
ensure_uv() {
    if [ -x "$BIN/uv" ]; then export PATH="$BIN:$PATH"; return 0; fi
    if [ "$MODE" = user ] && command -v uv >/dev/null 2>&1; then return 0; fi
    say "uv $UV_VERSION -> $BIN/uv"
    mkdir -p "$BIN"
    local t
    t=$(mktemp -d)
    fetch "https://github.com/astral-sh/uv/releases/download/$UV_VERSION/uv-x86_64-unknown-linux-gnu.tar.gz" \
        "$UV_SHA256" "$t/uv.tar.gz"
    tar -xzf "$t/uv.tar.gz" -C "$t"
    install -m 0755 "$t/uv-x86_64-unknown-linux-gnu/uv" "$BIN/uv"
    install -m 0755 "$t/uv-x86_64-unknown-linux-gnu/uvx" "$BIN/uvx"
    rm -rf "$t"
    export PATH="$BIN:$PATH"
}

# ffmpeg: the static ffmpeg 7.0.2 (libmp3lame) that imageio-ffmpeg 0.6.0 ships, the same build
# papercast-voice encodes with. Taken from the PyPI wheel because PyPI files never change, so the
# pin holds; the "release" tarballs of the usual static builds are replaced in place.
FFMPEG_WHEEL=imageio_ffmpeg-0.6.0-py3-none-manylinux2014_x86_64.whl
FFMPEG_WHEEL_URL=https://files.pythonhosted.org/packages/a0/2d/43c8522a2038e9d0e7dbdf3a61195ecc31ca576fb1527a528c877e87d973/$FFMPEG_WHEEL
FFMPEG_WHEEL_SHA256=c7e46fcec401dd990405049d2e2f475e2b397779df2519b544b8aab515195282
ensure_ffmpeg() {
    if [ -x "$BIN/ffmpeg" ] || { [ "$MODE" = user ] && command -v ffmpeg >/dev/null 2>&1; }; then
        say "ffmpeg: $([ -x "$BIN/ffmpeg" ] && echo "$BIN/ffmpeg" || command -v ffmpeg) (already there)"
        return 0
    fi
    say "ffmpeg 7.0.2 (static, from $FFMPEG_WHEEL) -> $BIN/ffmpeg"
    mkdir -p "$BIN"
    local t
    t=$(mktemp -d)
    fetch "$FFMPEG_WHEEL_URL" "$FFMPEG_WHEEL_SHA256" "$t/w.whl"
    python3 - "$t/w.whl" "$t/ffmpeg" <<'PY'
import sys, zipfile
z = zipfile.ZipFile(sys.argv[1])
name = [n for n in z.namelist() if n.startswith("imageio_ffmpeg/binaries/ffmpeg-linux")][0]
open(sys.argv[2], "wb").write(z.read(name))
PY
    install -m 0755 "$t/ffmpeg" "$BIN/ffmpeg"
    rm -rf "$t"
    "$BIN/ffmpeg" -hide_banner -encoders 2>/dev/null | grep -q libmp3lame || die "ffmpeg has no libmp3lame"
}

# cloudflared 2026.8.2, the official static build (GitHub release asset cloudflared-linux-amd64;
# sha256 = the asset digest GitHub publishes). An existing ~/.local/bin/cloudflared is used when it
# is this exact file, and otherwise left alone and a pinned copy goes next to our install.
CLOUDFLARED_VERSION=2026.8.2
CLOUDFLARED_SHA256=fcfb02b575a52ca1af2e3267af4e1517bcdeb30ac48c834c69abaed3c0576ad2
ensure_cloudflared() {
    local want="$BIN/cloudflared"
    if [ -x "$want" ] && [ "$(sha256sum "$want" | cut -d' ' -f1)" = "$CLOUDFLARED_SHA256" ]; then
        CLOUDFLARED=$want
        return 0
    fi
    if [ "$MODE" = system ] && [ -x "$want" ] && "$want" --version >/dev/null 2>&1; then
        # /srv/papercast/bin is ours: a different cloudflared there was put there on purpose (a
        # newer release: Cloudflare stops supporting ones older than a year). Kept.
        say "cloudflared: $want is $("$want" --version 2>&1 | head -1) (not the pinned $CLOUDFLARED_VERSION; kept)"
        CLOUDFLARED=$want
        return 0
    fi
    if [ -e "$want" ]; then
        # Someone's other cloudflared: never replaced; ours goes under the install instead.
        want="$PCG_HOME/bin/cloudflared"
        if [ -x "$want" ] && [ "$(sha256sum "$want" | cut -d' ' -f1)" = "$CLOUDFLARED_SHA256" ]; then
            CLOUDFLARED=$want
            return 0
        fi
    fi
    say "cloudflared $CLOUDFLARED_VERSION -> $want"
    mkdir -p "$(dirname "$want")"
    fetch "https://github.com/cloudflare/cloudflared/releases/download/$CLOUDFLARED_VERSION/cloudflared-linux-amd64" \
        "$CLOUDFLARED_SHA256" "$want.new"
    chmod 0755 "$want.new"
    mv "$want.new" "$want"
    CLOUDFLARED=$want
}

# systemd --user usable? (a user manager answering on this login; with Linger it also runs
# without one and starts at boot)
have_user_systemd() {
    command -v systemctl >/dev/null 2>&1 || return 1
    [ -n "${XDG_RUNTIME_DIR:-}" ] || export XDG_RUNTIME_DIR=/run/user/$(id -u)
    systemctl --user show-environment >/dev/null 2>&1
}

linger() {
    loginctl show-user "$(id -un)" -p Linger --value 2>/dev/null || echo unknown
}

# set_env <file> <KEY> <value>: replace or append one KEY=value line (mode 600 kept).
set_env() {
    local f=$1 k=$2 v=$3
    touch "$f"
    chmod 600 "$f"
    if grep -q "^$k=" "$f"; then
        local t
        t=$(mktemp "$f.XXXX")
        awk -v k="$k" -v v="$v" 'index($0, k "=") == 1 { print k "=" v; next } { print }' "$f" > "$t"
        chmod 600 "$t"
        mv "$t" "$f"
    else
        printf '%s=%s\n' "$k" "$v" >> "$f"
    fi
    # system mode: the settings belong to the service account (root edits them, it reads them)
    [ -z "${PCG_ETC_OWNER:-}" ] || chown "$PCG_ETC_OWNER" "$f"
}

get_env() {
    [ -f "$1" ] || return 0
    sed -n "s/^$2=//p" "$1" | tail -1
}

# ---- the two modes -----------------------------------------------------------------------------

# detect_mode: PCG_MODE when set; else system when its units are installed here; else user.
detect_mode() {
    if [ -n "${PCG_MODE:-}" ]; then echo "$PCG_MODE"
    elif [ -f "$PCG_SYS_UNITS/papercast-hub.service" ]; then echo system
    else echo user
    fi
}

# use_mode system|user: where things are (H, ETC for the settings and tokens, BIN, APP the code,
# PY the hub's python) and what the units are called (U_HUB, U_VOICE, U_TUNNEL, U_BACKUP, U_LAYOUT).
use_mode() {
    MODE=$1
    case "$MODE" in
    system)
        H=$PCG_SYS_HOME ETC=$PCG_SYS_HOME/etc BIN=$PCG_SYS_HOME/bin
        APP=$PCG_SYS_HOME/app/current/papercast-group
        U_HUB=papercast-hub U_VOICE=papercast-voice U_TUNNEL=papercast-tunnel
        U_BACKUP=papercast-backup U_LAYOUT=papercast-layout
        [ -n "${PCG_NO_SUDO:-}" ] || PCG_ETC_OWNER=$PCG_USER:$PCG_USER
        ;;
    user)
        H=$PCG_HOME ETC=$PCG_HOME BIN=${PCG_BIN:-$HOME/.local/bin}
        APP=$PCG_HOME/app/papercast-group
        U_HUB=pcg-hub U_VOICE=pcg-voice U_TUNNEL=pcg-tunnel U_BACKUP=pcg-backup U_LAYOUT=pcg-layout
        ;;
    *) die "mode is system or user, not '$1'" ;;
    esac
    PCG_HOME=$H
    PY=$H/venv/bin/python
}

sctl() { if [ "$MODE" = system ]; then systemctl "$@"; else systemctl --user "$@"; fi; }
jctl() { if [ "$MODE" = system ]; then journalctl "$@"; else journalctl --user "$@"; fi; }

# need_root <script> <args...>: system mode edits /srv/papercast and /etc/systemd/system, so the
# script runs again under sudo (PCG_NO_SUDO: the tests, which run it as themselves).
need_root() {
    local script=$1; shift
    [ "$MODE" = system ] || return 0
    [ "$(id -u)" = 0 ] || [ -n "${PCG_NO_SUDO:-}" ] || exec sudo -- bash "$script" "$@"
}

# as_pc <command...>: run it as the service account with a clean environment (system mode; in
# user mode, and in the tests, as oneself). HOME is cache/: what the account writes as a home
# (tool caches) lands there; /srv/papercast itself is root's, so nothing the service can write
# is ever run by root.
as_pc() {
    if [ "$MODE" = user ] || [ -n "${PCG_NO_SUDO:-}" ]; then "$@"; return; fi
    # (from /: the account may not be able to enter the directory this was started in)
    (cd / && sudo -u "$PCG_USER" -- env -i HOME="$H/cache" USER="$PCG_USER" LOGNAME="$PCG_USER" LANG=C.UTF-8 \
        PATH="$BIN:/usr/local/bin:/usr/bin:/bin" XDG_CACHE_HOME="$H/cache" \
        UV_CACHE_DIR="$H/cache/uv" UV_PYTHON_INSTALL_DIR="$H/python" \
        ${UV_OFFLINE:+UV_OFFLINE=$UV_OFFLINE} "$@")
}

# hub_py <args...>: the hub's python in its code directory with hub.env loaded, as the account,
# e.g. hub_py -m hub.auth allow list
hub_py() {
    # shellcheck disable=SC2016  (expanded by the inner shell)
    as_pc bash -c 'umask 027; set -a; . "$0"; set +a; cd "$1" && shift && exec "$@"' "$ETC/hub.env" "$APP" "$PY" "$@"
}

# render_units <template dir> <H> <out dir>: the system units with @H@ (the install) and @USER@
# (the account) filled in; install.sh --render-units and the tests use it too.
render_units() {
    local src=$1 h=$2 out=$3 f n
    mkdir -p "$out"
    for f in "$src"/papercast-*.service "$src"/papercast-*.timer; do
        n=$(basename "$f")
        sed -e "s|@H@|$h|g" -e "s|@USER@|$PCG_USER|g" "$f" > "$out/$n.new"
        chmod 0644 "$out/$n.new"
        mv "$out/$n.new" "$out/$n"
    done
}

# hub_port: the hub's port from hub.env (8400 on perov, where the tunnel points; 8480 the code's
# default elsewhere)
hub_port() {
    local p; p=$(get_env "$ETC/hub.env" PCG_PORT)
    if [ -n "$p" ]; then echo "$p"; elif [ "$MODE" = system ]; then echo 8400; else echo 8480; fi
}

# http_code <url>: the status code (000 when nothing answered)
http_code() { curl -s -m 15 -o /dev/null -w '%{http_code}' "$1" 2>/dev/null || true; }

# own_pc <path...>: give it to the service account (not in the tests, which have no such account)
own_pc() { [ -n "${PCG_NO_SUDO:-}" ] || chown "$PCG_USER:$PCG_USER" "$@"; }
