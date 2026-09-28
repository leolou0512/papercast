#!/usr/bin/env bash
# The voice on this machine: Leo's papercast-voice with the Breeze TTS 2 GPU voice, installed by
# its own install.sh (stacks/papercast/voice/install.sh, run unmodified) into
# $PCG_HOME/voice instead of stibnite's /home/leo/papercast/voice. As leo, no sudo. Safe to re-run.
#
#   bash deploy/install-voice.sh             papercast-voice + Kokoro (CPU) + Breeze (GPU), ~15 GB
#
# Then it points the voice at this machine only: no bs1 (Leo's remote GPUs are stibnite's
# business), and the one GPU slot lock under the voice's own run/ (papercast-voice derives it
# from the job directory otherwise: episodes/voice-gpu.lock inside the hub's data).
[ -n "${BASH_VERSION:-}" ] || exec bash "$0" "$@"
set -euo pipefail
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
. "$HERE/lib.sh"
REPO=$(cd "$HERE/../../.." && pwd)
VSRC=$REPO/stacks/papercast/voice
V=$PCG_HOME/voice

[ "$(id -u)" != 0 ] || die "run as your own user, not root"
[ -f "$VSRC/install.sh" ] || die "no $VSRC/install.sh (needs the repo's stacks/papercast/voice)"
export PATH="$BIN:$PATH"
ensure_uv

mkdir -p "$PCG_HOME"
chmod 700 "$PCG_HOME"
say "papercast-voice -> $V (disk: $(df -h --output=avail / | tail -1 | tr -d ' ') free)"
# The voice's install checks for speaking jobs in <state>/*/voice/status.json: the hub's
# episodes are laid out the same way (episodes/<id>/voice/).
PAPERCAST_VOICE_HOME=$V PAPERCAST_VOICE_CACHE=$PCG_HOME/voice-cache \
PAPERCAST_STATE=$PCG_HOME/data/episodes \
    nice -n 10 bash "$VSRC/install.sh" --gpu breeze

say "voice.json: this machine's card only"
PAPERCAST_VOICE_HOME=$V "$V/venv/bin/python" -I - <<'PY'
import json
from papercast_voice.config import save_measured, load
save_measured({"hosts": {"bs1": {"enabled": False}},
               "gpu_lock": "{home}/run/voice-gpu.lock"})
cfg = load()
print(json.dumps({"gpu_engine": cfg["gpu_engine"], "gpu_lock": cfg["gpu_lock"],
                  "bs1": cfg["hosts"]["bs1"]["enabled"],
                  "peak_mib": cfg["engines"]["breeze"]["peak_mib"]}))
PY
"$V/bin/papercast-voice" info
echo "PAPERCAST_VOICE_CMD=$V/bin/papercast-voice"
