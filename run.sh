#!/usr/bin/env bash
# Sets up the project on first run, then starts the server.
# Usage: ./run.sh [port]   (default port 8000)
set -e

cd "$(dirname "$0")"
PORT="${1:-8000}"

if [ ! -f .env ]; then
  cp .env.example .env
  echo "Created .env - add your API key to it, then run ./run.sh again."
  open -e .env 2>/dev/null || true
  exit 1
fi

PROVIDER=$(grep -E '^AI_PROVIDER=' .env | cut -d= -f2 | tr -d ' "\r' )
PROVIDER=${PROVIDER:-openrouter}
KEY_NAME=$(echo "$PROVIDER" | tr '[:lower:]' '[:upper:]')_API_KEY
KEY_VALUE=$(grep -E "^$KEY_NAME=" .env | cut -d= -f2- | tr -d "\r")

if [ -z "$KEY_VALUE" ] || echo "$KEY_VALUE" | grep -q "key-here"; then
  echo "AI_PROVIDER is $PROVIDER - put your real $KEY_NAME in .env first."
  open -e .env 2>/dev/null || true
  exit 1
fi

if [ ! -x .venv/bin/uvicorn ]; then
  echo "Installing dependencies..."
  python3 -m venv .venv
  .venv/bin/pip install -q -r requirement.txt
fi

echo "Using $PROVIDER. Open http://127.0.0.1:$PORT/ui  (Ctrl+C to stop)"
exec .venv/bin/uvicorn src.main:app --reload --port "$PORT"
