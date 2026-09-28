#!/usr/bin/env bash
# Copy this checkout to perov (val-perovskite) for deploy/install.sh: the repo without .git, into
# ~/papercast-group/repo/ there, with the commit it came from in repo/.revision.
#
#   bash stacks/papercast-group/tools/sync_to_perov.sh [user@host]
#
# perov's name does not resolve from stibnite: the tailnet address is the default.
set -euo pipefail
DEST=${1:-leo@100.97.205.90}
REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)
rev=$(git -C "$REPO" describe --always --dirty 2>/dev/null || echo unknown)
branch=$(git -C "$REPO" rev-parse --abbrev-ref HEAD 2>/dev/null || echo unknown)
ssh -o BatchMode=yes "$DEST" 'umask 077; mkdir -p ~/papercast-group/repo && chmod 700 ~/papercast-group'
rsync -a --delete --exclude .git --exclude .claude --exclude __pycache__ --exclude '*.pyc' \
    "$REPO/" "$DEST:papercast-group/repo/"
echo "$rev ($branch, synced $(date -u +%Y-%m-%dT%H:%M:%SZ))" | ssh -o BatchMode=yes "$DEST" 'cat > ~/papercast-group/repo/.revision'
echo "synced $REPO ($rev) -> $DEST:papercast-group/repo"
