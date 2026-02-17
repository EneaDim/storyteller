#!/bin/sh
set -eu

MODELS_DIR="${PIPER_MODELS_DIR:-/models}"
mkdir -p "$MODELS_DIR"

OFFICIAL="${PIPER_VOICES_OFFICIAL:-}"
HF="${PIPER_VOICES_HF:-}"

echo "[piper_init] MODELS_DIR=$MODELS_DIR"
echo "[piper_init] OFFICIAL=$OFFICIAL"
echo "[piper_init] HF=$HF"

# -----------------------------
# Helpers
# -----------------------------
download() {
  url="$1"
  out="$2"
  echo "[piper_init] wget $url -> $out"
  wget -q -O "$out" "$url"
}

exists_pair() {
  voice_id="$1"
  [ -f "${MODELS_DIR}/${voice_id}.onnx" ] && [ -f "${MODELS_DIR}/${voice_id}.onnx.json" ]
}

# Official voice URL builder:
# https://huggingface.co/rhasspy/piper-voices/resolve/main/it/it_IT/paola/medium/it_IT-paola-medium.onnx
official_url_for() {
  voice_id="$1"   # e.g. it_IT-paola-medium
  lang="$(echo "$voice_id" | cut -d'_' -f1)"     # it
  locale="$(echo "$voice_id" | cut -d'-' -f1)"   # it_IT
  speaker="$(echo "$voice_id" | cut -d'-' -f2)"  # paola
  quality="$(echo "$voice_id" | cut -d'-' -f3)"  # medium | high | x_low | low
  base="https://huggingface.co/rhasspy/piper-voices/resolve/main"
  echo "${base}/${lang}/${locale}/${speaker}/${quality}/${voice_id}"
}

# Normalize OFFICIAL list: accept commas or spaces
# shellcheck disable=SC2001
OFFICIAL_NORM="$(echo "$OFFICIAL" | sed 's/,/ /g')"

# -----------------------------
# Download OFFICIAL voices
# -----------------------------
for v in $OFFICIAL_NORM; do
  v="$(echo "$v" | tr -d ' \t\r\n')"
  [ -n "$v" ] || continue

  if exists_pair "$v"; then
    echo "[piper_init] Official voice already present: $v"
    continue
  fi

  base="$(official_url_for "$v")"
  download "${base}.onnx" "${MODELS_DIR}/${v}.onnx"
  download "${base}.onnx.json" "${MODELS_DIR}/${v}.onnx.json"

  echo "[piper_init] Downloaded official: $v"
done

# -----------------------------
# Download HF voices
# Format supported (your existing format):
#   entry1;entry2;...
# Each entry can be:
#   repo:folder:model_base:voice_id
#
# Examples you used earlier:
#   kirys79/piper_italiano:Aurora/it_IT-aurora-medium:it_IT-aurora-medium
#   kirys79/piper_italiano:Leonardo/leonardo-epoch=2024-step=996300:it_IT-leonardo-high
#
# We will:
# - build URLs based on verified patterns
# - download .onnx and .json (or .onnx.json) then normalize to <voice_id>.onnx/.onnx.json
# -----------------------------
if [ -n "$HF" ]; then
  OLDIFS="$IFS"
  IFS=";"
  for entry in $HF; do
    entry="$(echo "$entry" | tr -d '\r\n')"
    [ -n "$entry" ] || continue

    # Split repo and rest
    repo="$(echo "$entry" | cut -d':' -f1)"          # kirys79/piper_italiano
    rest="$(echo "$entry" | cut -d':' -f2-)"         # Aurora/xxx:voice_id  or Leonardo/xxx:voice_id

    path="$(echo "$rest" | cut -d':' -f1)"           # Aurora/it_IT-aurora-medium  or Leonardo/leonardo-epoch=...
    voice_id="$(echo "$rest" | cut -d':' -f2)"       # it_IT-aurora-medium / it_IT-leonardo-high

    [ -n "$repo" ] && [ -n "$path" ] && [ -n "$voice_id" ] || {
      echo "[piper_init] Skipping malformed HF entry: $entry"
      continue
    }

    if exists_pair "$voice_id"; then
      echo "[piper_init] HF voice already present: $voice_id"
      continue
    fi

    base="https://huggingface.co/${repo}/resolve/main/${path}"

    # Aurora uses: it_IT-aurora-medium.onnx and it_IT-aurora-medium.onnx.json
    # Leonardo/Giorgio use: <model>.onnx and <model>.json (verified by you)
    # We'll handle both by trying these patterns in order.

    tmp_onnx="${MODELS_DIR}/.__tmp_${voice_id}.onnx"
    tmp_json="${MODELS_DIR}/.__tmp_${voice_id}.json"

    # 1) Try .onnx + .onnx.json
    if wget -q --spider "${base}.onnx" && wget -q --spider "${base}.onnx.json"; then
      download "${base}.onnx" "$tmp_onnx"
      download "${base}.onnx.json" "$tmp_json"
    # 2) Try .onnx + .json
    elif wget -q --spider "${base}.onnx" && wget -q --spider "${base}.json"; then
      download "${base}.onnx" "$tmp_onnx"
      download "${base}.json" "$tmp_json"
    else
      echo "[piper_init] ERROR: HF URLs not found for entry: $entry"
      echo "[piper_init] Tried: ${base}.onnx + (${base}.onnx.json or ${base}.json)"
      exit 1
    fi

    # Normalize names
    mv -f "$tmp_onnx" "${MODELS_DIR}/${voice_id}.onnx"
    mv -f "$tmp_json" "${MODELS_DIR}/${voice_id}.onnx.json"

    echo "[piper_init] Downloaded HF: $voice_id"
  done
  IFS="$OLDIFS"
fi

# -----------------------------
# Validate
# -----------------------------
echo "[piper_init] Validating models..."
bad=0
for f in "${MODELS_DIR}"/*.onnx; do
  [ -f "$f" ] || continue
  base="$(basename "$f" .onnx)"
  if [ ! -f "${MODELS_DIR}/${base}.onnx.json" ]; then
    echo "[piper_init] Missing json for ${base}"
    bad=1
  fi
done

if [ "$bad" -ne 0 ]; then
  echo "[piper_init] Validation failed"
  exit 1
fi

echo "[piper_init] Done."

