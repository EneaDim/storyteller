#!/usr/bin/env bash
set -e
export APP_ENV=prod
export LOG_LEVEL="${LOG_LEVEL:-INFO}"
python -m bot.bot
