#!/usr/bin/env bash
# Expose the local api on a public HTTPS URL via Cloudflare Tunnel.
# Use this to receive WhatsApp webhooks while developing locally.
#
# Quick (ephemeral) tunnel — random *.trycloudflare.com URL on every run:
#   ./scripts/dev-tunnel.sh
#
# Named tunnel (stable URL — recommended once you're past the experiment phase):
#   1. cloudflared tunnel login
#   2. cloudflared tunnel create whatsapp-assistant
#      → writes credentials to ~/.cloudflared/<UUID>.json
#   3. Create ~/.cloudflared/config.yml:
#        tunnel: <UUID>
#        credentials-file: /home/<you>/.cloudflared/<UUID>.json
#        ingress:
#          - hostname: dev.yourdomain.com
#            service: http://localhost:8000
#          - service: http_status:404
#   4. cloudflared tunnel route dns whatsapp-assistant dev.yourdomain.com
#   5. cloudflared tunnel run whatsapp-assistant
#
# Either way, point the WhatsApp webhook URL at https://<that-host>/webhooks/whatsapp.

set -euo pipefail

PORT="${PORT:-8000}"

if ! command -v cloudflared >/dev/null 2>&1; then
  cat >&2 <<'EOF'
cloudflared not installed. Install it first:
  macOS:    brew install cloudflared
  Debian:   curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64.deb -o /tmp/cf.deb && sudo dpkg -i /tmp/cf.deb
  Windows:  winget install --id Cloudflare.cloudflared
EOF
  exit 1
fi

echo "[dev-tunnel] Exposing http://localhost:${PORT} via Cloudflare Tunnel"
echo "[dev-tunnel] The printed *.trycloudflare.com URL is ephemeral — use a named tunnel for a stable URL (see comments in this script)."
exec cloudflared tunnel --url "http://localhost:${PORT}"
