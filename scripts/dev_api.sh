#!/usr/bin/env bash
set -e
export APP_ENV=dev
export LOG_LEVEL=DEBUG
export PORT="${PORT:-8000}"
uvicorn app.api:app --host 0.0.0.0 --port "$PORT" --reload
