#!/usr/bin/env bash
set -euo pipefail
mkdir -p logs tmp
exec python -m bot.bot
