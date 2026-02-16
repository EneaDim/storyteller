#!/bin/sh
# piper_init.sh
# Scarica 2 voci Piper (narratrice femminile + esperto maschile) nel volume /models
# usando il downloader ufficiale del pacchetto piper-tts:
#   python -m piper.download_voices <VOICE>
# (Scarica .onnx + .onnx.json) :contentReference[oaicite:3]{index=3}

set -e

MODELS_DIR="${PIPER_MODELS_DIR:-/models}"
VOICE_F="${PIPER_VOICE_F:-it_IT-paola-medium}"       # femminile :contentReference[oaicite:4]{index=4}
VOICE_M="${PIPER_VOICE_M:-it_IT-riccardo-x_low}"     # maschile :contentReference[oaicite:5]{index=5}

echo "[piper_init] MODELS_DIR=${MODELS_DIR}"
echo "[piper_init] VOICE_F=${VOICE_F}"
echo "[piper_init] VOICE_M=${VOICE_M}"

mkdir -p "${MODELS_DIR}"
cd "${MODELS_DIR}"

echo "[piper_init] Downloading female voice..."
python -m piper.download_voices "${VOICE_F}"

echo "[piper_init] Downloading male voice..."
python -m piper.download_voices "${VOICE_M}"

echo "[piper_init] Files in ${MODELS_DIR}:"
ls -lah "${MODELS_DIR}"

# Sanity check: devono esistere .onnx e .onnx.json
test -f "${MODELS_DIR}/${VOICE_F}.onnx"
test -f "${MODELS_DIR}/${VOICE_F}.onnx.json"
test -f "${MODELS_DIR}/${VOICE_M}.onnx"
test -f "${MODELS_DIR}/${VOICE_M}.onnx.json"

echo "[piper_init] Done."

