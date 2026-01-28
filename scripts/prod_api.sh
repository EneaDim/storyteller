#!/usr/bin/env bash
set -euo pipefail

# Force CPU-only (disabilita GPU anche se presente)
export CUDA_VISIBLE_DEVICES="${CUDA_VISIBLE_DEVICES:-}"

# Default model (3B) se non già impostato da Railway
export OLLAMA_MODEL="${OLLAMA_MODEL:-qwen2.5:3b-instruct}"

# Dove Ollama salva i modelli (monta un Volume qui su Railway)
export OLLAMA_MODELS="${OLLAMA_MODELS:-/srv/.ollama}"

echo "[prod_api] APP_ENV=${APP_ENV:-prod} LOG_LEVEL=${LOG_LEVEL:-INFO}"
echo "[prod_api] PORT=${PORT:-8000}"
echo "[prod_api] CUDA_VISIBLE_DEVICES='${CUDA_VISIBLE_DEVICES}'"
echo "[prod_api] OLLAMA_MODEL='${OLLAMA_MODEL}'"
echo "[prod_api] OLLAMA_MODELS='${OLLAMA_MODELS}'"

mkdir -p logs tmp "${OLLAMA_MODELS}"

echo "[prod_api] starting ollama serve..."
ollama serve > logs/ollama.log 2>&1 &
sleep 1

echo "[prod_api] pulling model: ${OLLAMA_MODEL}"
ollama pull "${OLLAMA_MODEL}" || true

echo "[prod_api] starting uvicorn..."
exec uvicorn app.api:app --host 0.0.0.0 --port "${PORT:-8000}"
