#!/usr/bin/env bash
set -e
export APP_ENV=prod
export LOG_LEVEL="${LOG_LEVEL:-INFO}"
export PORT="${PORT:-8000}"

# Lascia accettare le richieste: la coda la fa FastAPI con tts_lock
uvicorn app.api:app --host 0.0.0.0 --port "$PORT" --workers 1 --timeout-keep-alive 75 --access-log
