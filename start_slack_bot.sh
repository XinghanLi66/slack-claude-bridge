#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"

if [ ! -d "$ROOT_DIR/.venv" ]; then
  python3 -m venv "$ROOT_DIR/.venv"
fi

. "$ROOT_DIR/.venv/bin/activate"
pip install -r "$ROOT_DIR/requirements.txt"
exec python3 "$ROOT_DIR/slack_claude_bot.py"
