#!/usr/bin/env bash
set -e
export APP_ENV=dev
export LOG_LEVEL=DEBUG
python -m bot.bot
