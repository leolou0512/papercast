#!/usr/bin/env bash
# Install papercast-voice on stibnite, as leo, without sudo. Safe to re-run: it replaces the
# code, re-syncs the venvs to the pinned requirements, and keeps voice.json (it holds the
# measurements).
#
#   bash install.sh                  code, the orchestrator venv, the Kokoro CPU voice
#   bash install.sh --gpu <name>     the same, plus the GPU voice Leo picked (README "GPU voice")
#
# Installs into $PAPERCAST_VOICE_HOME (default /home/leo/papercast/voice, mode 0700) and prints
# the PAPERCAST_VOICE_CMD value for the runner's runner.env. Nothing system-wide, no units.
[ -n "${BASH_VERSION:-}" ] || exec bash "$0" "$@"
set -euo pipefail

SRC=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
H=${PAPERCAST_VOICE_HOME:-/home/leo/papercast/voice}
CACHE=${PAPERCAST_VOICE_CACHE:-/home/leo/papercast-voice-cache}
STATE=${PAPERCAST_STATE:-/home/leo/papercast/state}
GPU=""
while [ $# -gt 0 ]; do
    case "$1" in
        --gpu) GPU=${2:?--gpu needs an engine name}; shift 2 ;;
        *) echo "usage: install.sh [--gpu <name>]" >&2; exit 2 ;;
    esac
done

die() { echo "install.sh: $*" >&2; exit 1; }
[ "$(id -u)" != 0 ] || die "run as leo, not root: this is a per-user install"
command -v uv >/dev/null || die "uv not on PATH (expected /home/leo/.local/bin/uv)"
[ -d "$CACHE/uv-cache" ] && export UV_CACHE_DIR=${UV_CACHE_DIR:-$CACHE/uv-cache}

# Never replace code, venvs or weights underneath a voice job (its engines load them).
if ! busy=$(python3 "$SRC/papercast_voice/busy.py" "$STATE" "$H"); then
    die "a voice job is running; not installing now (try again when it is done):
$busy"
fi

echo "== disk before"; df -h /
umask 077
mkdir -p "$H" "$H/bin" "$H/run" "$H/engines" "$H/models"
chmod 0700 "$H"

echo "== code -> $H/app"
rm -rf "$H/app.new"
mkdir -p "$H/app.new"
cp -a "$SRC/papercast_voice" "$H/app.new/"
find "$H/app.new" -name __pycache__ -prune -exec rm -rf {} +
rev=$(git -C "$SRC" rev-parse --short HEAD 2>/dev/null || echo unknown)
dirty=$(git -C "$SRC" status --porcelain -- . 2>/dev/null | head -1 || true)
echo "1.0 (git $rev${dirty:+ + uncommitted edits}, installed $(date -u +%Y-%m-%dT%H:%M:%SZ))" \
    > "$H/app.new/papercast_voice/VERSION"
rm -rf "$H/app.old"
[ -d "$H/app" ] && mv "$H/app" "$H/app.old"
mv "$H/app.new" "$H/app"
rm -rf "$H/app.old"

echo "== orchestrator venv"
[ -x "$H/venv/bin/python" ] || uv venv -q -p 3.11 "$H/venv"
VIRTUAL_ENV="$H/venv" uv pip sync -q "$SRC/requirements/voice.txt"
site=$("$H/venv/bin/python" -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')
echo "$H/app" > "$site/papercast_voice_app.pth"
ff=$("$H/venv/bin/python" -c 'import imageio_ffmpeg; print(imageio_ffmpeg.get_ffmpeg_exe())')
ln -sfn "$ff" "$H/bin/ffmpeg"
"$H/bin/ffmpeg" -hide_banner -encoders 2>/dev/null | grep -q libmp3lame || die "ffmpeg has no libmp3lame"

echo "== Kokoro venv (CPU voice)"
K="$H/engines/kokoro/venv"
[ -x "$K/bin/python" ] || uv venv -q -p 3.11 "$K"
VIRTUAL_ENV="$K" uv pip sync -q --index-url https://download.pytorch.org/whl/cpu \
    --extra-index-url https://pypi.org/simple --index-strategy unsafe-best-match \
    "$SRC/requirements/kokoro.txt"

echo "== Kokoro weights (hexgrad/Kokoro-82M, revision f3ff357)"
M="$H/models/kokoro-82m"
REV=f3ff3571791e39611d31c381e3a41a3af07b4987
mkdir -p "$M/voices"
snap="$CACHE/hf/hub/models--hexgrad--Kokoro-82M/snapshots/$REV"
for f in config.json kokoro-v1_0.pth voices/af_heart.pt; do
    if [ ! -s "$M/$f" ]; then
        if [ -s "$snap/$f" ]; then cp -L "$snap/$f" "$M/$f"
        else HF_HUB_DISABLE_TELEMETRY=1 "$K/bin/hf" download hexgrad/Kokoro-82M "$f" \
                 --revision "$REV" --local-dir "$M" >/dev/null
        fi
    fi
done
# LFS object ids are sha256 of the content (from the Hugging Face repo at that revision).
check() { [ "$(sha256sum "$1" | cut -d' ' -f1)" = "$2" ] || die "checksum mismatch: $1"; }
check "$M/kokoro-v1_0.pth" 496dba118d1a58f5f3db2efc88dbdc216e0483fc89fe6e47ee1f2c53f18ad1e4
check "$M/voices/af_heart.pt" 0ab5709b8ffab19bfd849cd11d98f75b60af7733253ad0d67b12382a102cb4ff

if [ -n "$GPU" ]; then
    [ -f "$SRC/engines/$GPU/install.sh" ] || die "no GPU voice adapter '$GPU' in $SRC/engines/"
    echo "== GPU voice: $GPU"
    H="$H" SRC="$SRC" CACHE="$CACHE" bash "$SRC/engines/$GPU/install.sh"
fi

echo "== config"
[ -f "$H/voice.json" ] || echo '{}' > "$H/voice.json"

echo "== wrapper $H/bin/papercast-voice"
cat > "$H/bin/papercast-voice.new" <<EOF
#!/bin/sh
# Installed by stacks/papercast/voice/install.sh ($rev). The runner calls this with a cleaned
# environment (INTERFACE.md §10.1), so it names everything itself. -I: the job directory (the
# working directory) is never on the module path.
PAPERCAST_VOICE_HOME=$H
export PAPERCAST_VOICE_HOME
exec "$H/venv/bin/python" -I -m papercast_voice "\$@"
EOF
chmod 0700 "$H/bin/papercast-voice.new"
mv "$H/bin/papercast-voice.new" "$H/bin/papercast-voice"

echo "== self-check"
out=$(env -i HOME="$HOME" USER="${USER:-leo}" LANG=C.UTF-8 PATH=/usr/local/bin:/usr/bin:/bin \
      "$H/bin/papercast-voice" info)
echo "$out"
case "$out" in *'"installed": true'*) ;; *) die "info does not say installed" ;; esac

echo "== disk after"; sync; df -h /
echo
echo "Installed. For the runner (runner.env):"
echo "PAPERCAST_VOICE_CMD=$H/bin/papercast-voice"
