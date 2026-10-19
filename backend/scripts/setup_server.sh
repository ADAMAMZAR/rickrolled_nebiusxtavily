#!/usr/bin/env bash
# One-time setup of a Debian/Ubuntu server (GCP Compute Engine) that CI then deploys to. Safe to re-run.
# Run from a clone at ~/troth, after installing Hermes and filling in backend/.env:
#   bash backend/scripts/setup_server.sh
# Serves Troth over HTTPS at $TROTH_DOMAIN, or at <external-ip>.sslip.io when that's unset.
set -euo pipefail
cd "$(dirname "$0")/.."
BACKEND=$PWD

[ -f .env ] || { echo "Create backend/.env first: cp .env.example .env, then fill it in."; exit 1; }
grep -q '^APP_PASSWORD=.' .env || { echo "Set APP_PASSWORD in .env: without it anyone can use Troth and your API credits."; exit 1; }
HERMES=$(command -v hermes) || { echo "Install Hermes first: https://hermes-agent.nousresearch.com"; exit 1; }
HERMES=$(readlink -f "$HERMES")

if [ -z "${TROTH_DOMAIN:-}" ]; then
  IP=$(curl -fsS -H 'Metadata-Flavor: Google' \
    http://metadata.google.internal/computeMetadata/v1/instance/network-interfaces/0/access-configs/0/external-ip)
  TROTH_DOMAIN="${IP//./-}.sslip.io"
fi

sudo apt-get update -q
sudo apt-get install -yq git python3-venv caddy
sudo ln -sf "$HERMES" /usr/local/bin/hermes  # non-login shells (CI's ssh, systemd) don't have Hermes' PATH

python3 -m venv .venv
.venv/bin/pip install -q -r requirements.txt -e .
if grep -q '^PUBLIC_URL=' .env; then
  sed -i "s|^PUBLIC_URL=.*|PUBLIC_URL=https://$TROTH_DOMAIN|" .env
else
  echo "PUBLIC_URL=https://$TROTH_DOMAIN" >> .env
fi
.venv/bin/python scripts/setup_hermes.py

sudo tee /etc/systemd/system/troth.service >/dev/null <<EOF
[Unit]
Description=Troth
After=network-online.target
Wants=network-online.target

[Service]
User=$USER
WorkingDirectory=$BACKEND
ExecStart=$BACKEND/.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000
Restart=on-failure

[Install]
WantedBy=multi-user.target
EOF

# Hermes connects to Troth's MCP tools once, at start: if Troth isn't answering yet, it runs without them. After=
# only waits for Troth's process to start, so ExecStartPre waits until Troth answers (any HTTP status counts).
sudo tee /etc/systemd/system/hermes.service >/dev/null <<EOF
[Unit]
Description=Hermes gateway for Troth
After=troth.service
Wants=troth.service

[Service]
User=$USER
WorkingDirectory=$BACKEND
ExecStartPre=/usr/bin/timeout 90 /bin/sh -c 'until curl -so /dev/null http://127.0.0.1:8000/health; do sleep 1; done'
ExecStart=/usr/local/bin/hermes -p continuum gateway run
Restart=on-failure

[Install]
WantedBy=multi-user.target
EOF

# Caddy gets the HTTPS certificate. /mcp has no password, so only Hermes on this machine may reach it.
sudo tee /etc/caddy/Caddyfile >/dev/null <<EOF
$TROTH_DOMAIN {
	respond /mcp* 403
	reverse_proxy 127.0.0.1:8000
}
EOF

if [ -f /etc/systemd/system/troth-hermes.service ]; then  # this unit's old name: one gateway per bot token
  sudo systemctl disable -q --now troth-hermes
  sudo rm /etc/systemd/system/troth-hermes.service
fi
sudo systemctl daemon-reload
sudo systemctl enable -q troth hermes caddy
sudo systemctl restart troth hermes caddy
echo "Troth: https://$TROTH_DOMAIN (the certificate can take a minute)"
