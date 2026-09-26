#!/bin/bash
# C compiler for Triton's launcher stubs: stibnite has no gcc and this is a shared box
# (no sudo, no system packages), so zig's clang stands in. zig's driver does not take
# the "-l:file" form Triton uses for libcuda, so pass that library by absolute path.
# Install: cp cc-zig.sh ~/papercast-voice-cache/bin/cc (needs `ziglang` in the voxtral venv).
args=()
for a in "$@"; do
  case "$a" in
    -l:libcuda.so.1) args+=("/usr/lib/x86_64-linux-gnu/libcuda.so.1") ;;
    *) args+=("$a") ;;
  esac
done
export ZIG_GLOBAL_CACHE_DIR=/home/leo/papercast-voice-cache/zig-cache
exec /home/leo/papercast-voice-cache/venvs/voxtral/bin/python -m ziglang cc "${args[@]}"
