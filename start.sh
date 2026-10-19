#!/usr/bin/env bash
# Démarre le hub sur l'interface Tailscale uniquement (jamais sur le LAN).
set -euo pipefail
cd "$(dirname "$0")"

HOST="${HUB_HOST:-$(tailscale ip -4 2>/dev/null | head -1)}"
HOST="${HOST:-127.0.0.1}"
PORT="${HUB_PORT:-8765}"

exec ./.venv/bin/uvicorn app:app --host "$HOST" --port "$PORT" --workers 1 --log-level info
