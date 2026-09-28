#!/usr/bin/env bash
# Copy what perov needs to ~/papercast-group/repo/ there, for deploy/install.sh: only committed
# files (git archive of HEAD), and only the parts the group hub uses (stacks/papercast-group,
# packages/papercast-cli, stacks/papercast/voice for install-voice.sh). Never the working tree:
# it holds ignored files (secrets/, .env) that must not leave stibnite.
#
#   bash stacks/papercast-group/tools/sync_to_perov.sh [user@host]
#
# perov's name does not resolve from stibnite: the tailnet address is the default.
set -euo pipefail
DEST=${1:-leo@100.97.205.90}
REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)
rev=$(git -C "$REPO" rev-parse --short HEAD)
branch=$(git -C "$REPO" rev-parse --abbrev-ref HEAD)
if ! git -C "$REPO" diff --quiet HEAD -- stacks/papercast-group packages/papercast-cli stacks/papercast/voice; then
    echo "note: uncommitted changes are not synced (HEAD $rev is)" >&2
fi
tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
git -C "$REPO" archive HEAD stacks/papercast-group packages/papercast-cli stacks/papercast/voice | tar -x -C "$tmp"
ssh -o BatchMode=yes "$DEST" 'umask 077; mkdir -p ~/papercast-group/repo && chmod 700 ~/papercast-group'
rsync -a --delete "$tmp/" "$DEST:papercast-group/repo/"
echo "$rev ($branch, synced $(date -u +%Y-%m-%dT%H:%M:%SZ))" | ssh -o BatchMode=yes "$DEST" 'cat > ~/papercast-group/repo/.revision'
echo "synced $rev ($branch) -> $DEST:papercast-group/repo"
