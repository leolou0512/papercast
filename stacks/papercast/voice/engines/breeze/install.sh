#!/usr/bin/env bash
# The Breeze TTS 2 GPU voice, called by ../../install.sh --gpu breeze (which sets H, SRC, CACHE).
# As leo, no sudo, nothing system-wide, no driver change. Safe to re-run.
#
#   $H/engines/breeze/venv     the pinned venv (requirements/breeze.txt), from the uv cache
#   $H/engines/breeze/src      breeze-tts at upstream commit 008f769 (the code that made clip A)
#   $H/engines/breeze/bin/cc   zig's clang for Triton (cc.sh)
#   $H/engines/breeze/cache/   Triton, inductor and zig caches (seeded from the sample step's)
#   $H/models/breeze-tts-2     BreezeBlue/Breeze-TTS-2 at revision 3e28c51, checksums verified
#   voice.json                 gpu_engine = breeze; a provisional peak until measure-gpu runs
set -euo pipefail
: "${H:?}" "${SRC:?}" "${CACHE:?}"
E="$H/engines/breeze"
M="$H/models/breeze-tts-2"
REV=3e28c5151381a722f1d8661b4118c298caa77aa4
CODE_REV=008f769
die() { echo "engines/breeze/install.sh: $*" >&2; exit 1; }
mkdir -p "$E/bin" "$E/cache/triton" "$E/cache/inductor" "$E/cache/zig" "$M/audio_tokenizer"

echo "-- venv"
[ -x "$E/venv/bin/python" ] || uv venv -q -p 3.11 "$E/venv"
VIRTUAL_ENV="$E/venv" uv pip sync -q "$SRC/requirements/breeze.txt"

echo "-- breeze-tts code ($CODE_REV)"
if [ ! -f "$E/src/.rev" ] || [ "$(cat "$E/src/.rev")" != "$CODE_REV" ]; then
    rm -rf "$E/src.new"
    if [ -d "$CACHE/src/breeze-tts/.git" ] &&
       [ "$(git -C "$CACHE/src/breeze-tts" rev-parse --short=7 HEAD)" = "$CODE_REV" ]; then
        git clone -q "$CACHE/src/breeze-tts" "$E/src.new"
    else
        git clone -q https://github.com/breezeblue-ai/breeze-tts.git "$E/src.new"
    fi
    git -C "$E/src.new" checkout -q "$CODE_REV"
    rm -rf "$E/src.new/.git"
    echo "$CODE_REV" > "$E/src.new/.rev"
    rm -rf "$E/src"
    mv "$E/src.new" "$E/src"
fi

echo "-- C compiler wrapper"
sed "s|@ENGINE@|$E|g" "$SRC/engines/breeze/cc.sh" > "$E/bin/cc.new"
chmod 0700 "$E/bin/cc.new"
mv "$E/bin/cc.new" "$E/bin/cc"
echo 'int main(void){return 0;}' > "$E/cache/cc-test.c"
"$E/bin/cc" -o "$E/cache/cc-test" "$E/cache/cc-test.c" || die "zig cc does not compile"
rm -f "$E/cache/cc-test" "$E/cache/cc-test.c"

echo "-- compiler caches (seeded once from the sample step, if there)"
for d in triton inductor; do
    if [ -z "$(ls -A "$E/cache/$d")" ] && [ -d "$CACHE/$d-cache" ]; then
        cp -a "$CACHE/$d-cache/." "$E/cache/$d/"
    fi
done

echo "-- weights (BreezeBlue/Breeze-TTS-2 @ ${REV:0:7})"
# Hard links when the sample step's copy is on the same filesystem (no extra space), else a copy,
# else a download at the pinned revision. Then every file the model reads is checked.
files="config.json generation_config.json model.safetensors.index.json model-00001-of-00002.safetensors
model-00002-of-00002.safetensors special_tokens_map.json tokenizer.json tokenizer_config.json
audio_tokenizer/config.json audio_tokenizer/configuration.json audio_tokenizer/model.safetensors
audio_tokenizer/preprocessor_config.json"
for f in $files; do
    [ -s "$M/$f" ] && continue
    if [ -s "$CACHE/models/breeze-tts-2/$f" ]; then
        ln "$CACHE/models/breeze-tts-2/$f" "$M/$f" 2>/dev/null || cp "$CACHE/models/breeze-tts-2/$f" "$M/$f"
    else
        HF_HUB_DISABLE_TELEMETRY=1 "$E/venv/bin/hf" download BreezeBlue/Breeze-TTS-2 "$f" \
            --revision "$REV" --local-dir "$M" >/dev/null
    fi
done
# The large files' sha256 are their LFS object ids at that revision. The small files' sha256 were
# taken from the 2026-09-26 download, whose git blob ids match the revision's (`git hash-object`).
check() { [ "$(sha256sum "$M/$1" | cut -d' ' -f1)" = "$2" ] || die "checksum mismatch: $M/$1"; }
check model-00001-of-00002.safetensors abf813781256e10cbe81f2dbb415f897556225d4dfa0282d67aa8ea164e114a9 &
check model-00002-of-00002.safetensors 36aa73b1a11361e1774db90d9c63c63303b294c022de112aa51904d940edcef1 &
check audio_tokenizer/model.safetensors 836b7b357f5ea43e889936a3709af68dfe3751881acefe4ecf0dbd30ba571258 &
check tokenizer.json d3ec9ac3eb2392389b9f5112e85d8b43316494addb587ba7b7a9d61eac23af96 &
for j in 1 2 3 4; do wait -n || die "a weights file failed its checksum"; done
(cd "$M" && sha256sum -c --quiet) <<'SUMS' || die "checksum mismatch in $M"
57849eb756ca1602efe89afcc5c70379dd127f8deeb9f655204d4340190ea929  config.json
2ef3f2c0ab8d9ad241059138a795433c5410fd9954efb9625674ecd2a9529434  generation_config.json
19977e3d96bb502ed3165b48adff094f6bd71ec3d349940d1a3ba6a0060bd5e3  model.safetensors.index.json
194f265bb588d142a16f27d9576104eb3dab3d7ba9961541d2d6e3d5e77e6470  special_tokens_map.json
2c084fd6725c2284e8aa6da095a8d036d48fe79665595bfcd50371821b620e44  tokenizer_config.json
ee65bb901c876664ab8707c487157aa1a6ee57c65969b28fb5ec9dc211e68167  audio_tokenizer/config.json
6bc26d64eb5024b4d1dab5a52371958b429256d6c9d59787f1f5294a54e0cebd  audio_tokenizer/configuration.json
fcb3805e597e786d4067706e602f6688524640f8d3396790e2e09b5942fcbdfb  audio_tokenizer/preprocessor_config.json
SUMS

echo "-- voice.json: gpu_engine = breeze"
[ -f "$H/voice.json" ] || echo '{}' > "$H/voice.json"
PAPERCAST_VOICE_HOME="$H" "$H/venv/bin/python" -I - <<'PY'
import json
from papercast_voice.config import config_path, save_measured
cur = json.load(open(config_path()))
b = (cur.get("engines") or {}).get("breeze") or {}
upd = {"gpu_engine": "breeze"}
if not b.get("peak_mib"):
    # Until measure-gpu has run on a full episode: the sample's peak (one 74-word paragraph,
    # the same model settings, NVML every 50 ms over the process tree). Admission adds 1 GiB.
    upd["engines"] = {"breeze": {"peak_mib": 9696, "measured": (
        "provisional: the 2026-09-26 sample (74 words, 9,696 MiB); replaced by measure-gpu")}}
save_measured(upd)
print(json.dumps({"gpu_engine": "breeze", "peak_mib": (b.get("peak_mib") or 9696)}))
PY
