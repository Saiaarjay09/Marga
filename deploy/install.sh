#!/usr/bin/env bash
# Installs Marga as a macOS LaunchAgent: starts at login, restarts if it crashes.
# Usage:  deploy/install.sh            (default port 8090)
#         MARGA_PORT=9000 deploy/install.sh
set -euo pipefail

cd "$(dirname "$0")/.."
ROOT="$PWD"
PORT="${MARGA_PORT:-8090}"
PLIST="$HOME/Library/LaunchAgents/com.marga.server.plist"

[ -d .venv ] || python3 -m venv .venv
.venv/bin/python -m pip install -q -r requirements.txt
[ -f .env ] || { cp .env.example .env; echo "Created .env; add your API keys to it (see README)."; }

mkdir -p "$HOME/Library/Logs" "$HOME/Library/LaunchAgents"
sed -e "s|__ROOT__|$ROOT|g" -e "s|__PORT__|$PORT|g" -e "s|__HOME__|$HOME|g" deploy/com.marga.server.plist > "$PLIST"

launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"

echo "Marga is running at http://127.0.0.1:$PORT  (logs: ~/Library/Logs/marga.log)"
echo "To put it on the internet with Tailscale, run deploy/publish.sh"
