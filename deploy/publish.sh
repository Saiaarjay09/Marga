#!/usr/bin/env bash
# Publishes Marga at https://<this-machine>.<tailnet>.ts.net/marga through Tailscale.
# Adds ONE path handler and leaves every other Serve/Funnel route alone.
#
#   deploy/publish.sh --public    open to the whole internet (Tailscale Funnel)
#   deploy/publish.sh             private, only devices on your tailnet
#
# WARNING: `tailscale serve` on a port that is currently Funnel-enabled silently
# turns Funnel OFF for that port, taking every other site on it off the internet.
# The private mode below refuses to run in that situation.
set -euo pipefail
PORT="${MARGA_PORT:-8090}"
TARGET="http://127.0.0.1:$PORT"

if [ "${1:-}" = "--public" ]; then
  tailscale funnel --bg --https=443 --set-path /marga "$TARGET"
else
  if tailscale funnel status 2>/dev/null | grep -qE '^https://[^ ]+ \(Funnel on\)'; then
    echo "Port 443 is already public via Funnel; a private 'serve' would switch that off for your other sites."
    echo "Re-run with --public to add Marga alongside them."
    exit 1
  fi
  tailscale serve --bg --set-path /marga "$TARGET"
fi
tailscale serve status
