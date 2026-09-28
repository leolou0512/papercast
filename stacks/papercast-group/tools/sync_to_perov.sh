#!/usr/bin/env bash
# Copy the code perov needs to ~/papercast-src/ there (in the home of whoever logs in), for
# `sudo papercastctl update ~/papercast-src`: only committed files (git archive of HEAD), and
# only the parts the group hub uses (stacks/papercast-group, packages/papercast-cli,
# stacks/papercast/voice for the voice's install). Never the working tree: it holds ignored files
# (secrets/, .env) that must not leave this machine.
#
#   bash stacks/papercast-group/tools/sync_to_perov.sh [user@host] [--dir DIR] [--install]
#
#   --dir DIR    where on perov, relative to that home (default papercast-src)
#   --install    then run `sudo papercastctl update ~/DIR` there (asks for the sudo password if
#                that login needs one)
#
# perov's name does not resolve from stibnite: the tailnet address is the default.
set -euo pipefail
DEST=leo@100.97.205.90 DIR=papercast-src INSTALL=""
while [ $# -gt 0 ]; do
    case "$1" in
        --dir) DIR=${2:?}; shift 2 ;;
        --install) INSTALL=1; shift ;;
        -*) echo "usage: sync_to_perov.sh [user@host] [--dir DIR] [--install]" >&2; exit 2 ;;
        *) DEST=$1; shift ;;
    esac
done
case "$DIR" in /*|*..*|"") echo "--dir is a folder in the remote home, like papercast-src" >&2; exit 2 ;; esac
REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)
rev=$(git -C "$REPO" rev-parse --short HEAD)
branch=$(git -C "$REPO" rev-parse --abbrev-ref HEAD)
if ! git -C "$REPO" diff --quiet HEAD -- stacks/papercast-group packages/papercast-cli stacks/papercast/voice; then
    echo "note: uncommitted changes are not synced (HEAD $rev is)" >&2
fi
tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
git -C "$REPO" archive HEAD stacks/papercast-group packages/papercast-cli stacks/papercast/voice | tar -x -C "$tmp"
echo "$rev ($branch, synced $(date -u +%Y-%m-%dT%H:%M:%SZ))" > "$tmp/.revision"
ssh -o BatchMode=yes "$DEST" "umask 077; mkdir -p ~/$DIR && chmod 700 ~/$DIR"
rsync -a --delete "$tmp/" "$DEST:$DIR/"
echo "synced $rev ($branch) -> $DEST:$DIR"
if [ -n "$INSTALL" ]; then
    ssh -t "$DEST" "sudo papercastctl update ~/$DIR"
else
    echo "to install it: ssh $DEST sudo papercastctl update '~/$DIR'"
fi
