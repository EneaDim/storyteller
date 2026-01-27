#!/usr/bin/env bash
set -e
export OMP_NUM_THREADS=2
export MKL_NUM_THREADS=2
export APP_ENV=prod
export LOG_LEVEL="${LOG_LEVEL:-INFO}"
export PORT="${PORT:-8000}"
# 1 worker consigliato: modello pesante + memoria
uvicorn app.api:app --host 0.0.0.0 --port "$PORT" --workers 1 --limit-concurrency 1 --timeout-keep-alive 75 --access-log
