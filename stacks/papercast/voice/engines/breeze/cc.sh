#!/bin/bash
# C compiler for Triton's launcher stubs and inductor (Breeze's two CUDA-graph stages compile
# with them). stibnite has no gcc and is shared (no sudo, no system packages), so zig's clang,
# from the `ziglang` wheel in the Breeze venv, stands in. zig's driver rejects the "-l:file" form
# Triton uses for libcuda, so that library is passed by absolute path. The same wrapper as
# samples/cc-zig.sh, pointed at the installed venv. install.sh fills in @ENGINE@.
args=()
for a in "$@"; do
  case "$a" in
    -l:libcuda.so.1) args+=("/usr/lib/x86_64-linux-gnu/libcuda.so.1") ;;
    *) args+=("$a") ;;
  esac
done
export ZIG_GLOBAL_CACHE_DIR=@ENGINE@/cache/zig
exec @ENGINE@/venv/bin/python -m ziglang cc "${args[@]}"
