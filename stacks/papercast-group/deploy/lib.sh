# Shared by install.sh, install-voice.sh and tunnel.sh (sourced, not run). Per-user only: every
# binary goes to ~/.local/bin, nothing system-wide, no sudo.
#
# Each download is pinned to one release and its sha256 (taken from the publisher on 2026-09-28:
# the release's .sha256 file, GitHub's asset digest, PyPI's digest), so a changed file stops the
# install instead of running.

PCG_HOME=${PCG_HOME:-$HOME/papercast-group}
BIN=${PCG_BIN:-$HOME/.local/bin}

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
    if command -v uv >/dev/null 2>&1; then return 0; fi
    if [ -x "$BIN/uv" ]; then export PATH="$BIN:$PATH"; return 0; fi
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
    if command -v ffmpeg >/dev/null 2>&1; then
        say "ffmpeg: $(command -v ffmpeg) (already there)"
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
}

get_env() {
    [ -f "$1" ] || return 0
    sed -n "s/^$2=//p" "$1" | tail -1
}
