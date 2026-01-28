#!/usr/bin/env bash
set -e
export APP_ENV=dev
python -m uvicorn app.api:app --host 0.0.0.0 --port 8000 --reload
