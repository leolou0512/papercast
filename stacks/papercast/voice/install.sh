#!/usr/bin/env bash
# Install papercast-voice on stibnite, as leo, without sudo. Safe to re-run: it replaces the
# code, re-syncs the venvs to the pinned requirements, and keeps voice.json (it holds the
# measurements).
#
#   bash install.sh                  code, the orchestrator venv, the Kokoro CPU voice
#   bash install.sh --gpu <name>     the same, plus the GPU voice Leo picked (README "GPU voice")
#   bash install.sh --code-only      only the code and the wrapper, allowed while jobs speak:
#                                    refused if requirements/ or engines/ changed since the
#                                    installed commit (those need a full install, when idle)
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
CODE_ONLY=""
while [ $# -gt 0 ]; do
    case "$1" in
        --gpu) GPU=${2:?--gpu needs an engine name}; shift 2 ;;
        --code-only) CODE_ONLY=1; shift ;;
        *) echo "usage: install.sh [--gpu <name> | --code-only]" >&2; exit 2 ;;
    esac
done
[ -z "$GPU" ] || [ -z "$CODE_ONLY" ] || { echo "install.sh: --gpu and --code-only exclude each other" >&2; exit 2; }

die() { echo "install.sh: $*" >&2; exit 1; }
[ "$(id -u)" != 0 ] || die "run as leo, not root: this is a per-user install"
command -v uv >/dev/null || die "uv not on PATH (expected /home/leo/.local/bin/uv)"
[ -d "$CACHE/uv-cache" ] && export UV_CACHE_DIR=${UV_CACHE_DIR:-$CACHE/uv-cache}

if [ -n "$CODE_ONLY" ]; then
    # Code only: waiting jobs re-execute onto it, speaking ones keep what they loaded, and their
    # engines keep theirs (README "Install"). Venvs, weights and engine installers must be what
    # the installed commit had, or this would pair new code with old dependencies.
    [ -x "$H/venv/bin/python" ] && [ -f "$H/app/papercast_voice/VERSION" ] ||
        die "--code-only needs a full install first"
    old=$(sed -n 's/.*(git \([0-9a-f]\{7,40\}\).*/\1/p' "$H/app/papercast_voice/VERSION")
    [ -n "$old" ] || die "--code-only: the installed VERSION names no commit"
    git -C "$SRC" diff --quiet "$old" -- requirements engines ||
        die "requirements/ or engines/ changed since the installed $old: run a full install"
else
    # Never replace venvs or weights underneath a speaking job (its engines load them).
    if ! busy=$(python3 "$SRC/papercast_voice/busy.py" "$STATE" "$H"); then
        die "a voice job is speaking; not installing now (try again when it is done, or use
--code-only if only code changed):
$busy"
    fi
fi

echo "== disk before"; df -h /
umask 077
mkdir -p "$H" "$H/bin" "$H/run" "$H/engines" "$H/models"
chmod 0700 "$H"

echo "== code -> $H/app"
# app is a symlink to app-<time>; rename(2) replaces it in one step, so a job starting, or a
# waiting job re-executing onto the new code (it watches VERSION), never finds no code. The
# two previous copies stay (nothing loads them; they are small).
stamp=$(date -u +%Y%m%dT%H%M%SZ)
new="$H/app-$stamp"
rm -rf "$new"
mkdir -p "$new"
cp -a "$SRC/papercast_voice" "$new/"
find "$new" -name __pycache__ -prune -exec rm -rf {} +
rev=$(git -C "$SRC" rev-parse --short HEAD 2>/dev/null || echo unknown)
dirty=$(git -C "$SRC" status --porcelain -- . 2>/dev/null | head -1 || true)
echo "1.1 (git $rev${dirty:+ + uncommitted edits}, installed $(date -u +%Y-%m-%dT%H:%M:%SZ))" \
    > "$new/papercast_voice/VERSION"
if [ -d "$H/app" ] && [ ! -L "$H/app" ]; then
    mv "$H/app" "$H/app-1.0"        # once: 1.0 installed a directory here
fi
ln -sfn "app-$stamp" "$H/app.link"
mv -T "$H/app.link" "$H/app"
ls -1dt "$H"/app-2* 2>/dev/null | tail -n +4 | xargs -r rm -rf

if [ -z "$CODE_ONLY" ]; then
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
fi   # the full install

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
