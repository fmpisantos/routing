#!/usr/bin/env bash
# Install + enable the control panel (scripts/panel.py) as a systemd
# service so the /default dashboard is always available.
#
# Idempotent: re-running rewrites the unit and restarts the service.
# Env: PANEL_PORT (default 8090), PANEL_TOKEN (optional — recommended
#      if the proxy is exposed publicly via Tailscale Funnel).

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PANEL_PORT="${PANEL_PORT:-8090}"
UNIT=/etc/systemd/system/routing-panel.service

say()  { printf "\033[36m▸\033[0m %s\n" "$*"; }
ok()   { printf "\033[32m✓\033[0m %s\n" "$*"; }
warn() { printf "\033[33m!\033[0m %s\n" "$*" >&2; }

TOKEN_LINE=""
if [[ -n "${PANEL_TOKEN:-}" ]]; then
  TOKEN_LINE="Environment=PANEL_TOKEN=${PANEL_TOKEN}"
else
  warn "PANEL_TOKEN not set — anyone who can reach the proxy can start/stop"
  warn "projects. If you expose via Funnel, reinstall with:"
  warn "  PANEL_TOKEN=<secret> scripts/install-panel.sh"
fi

say "Writing ${UNIT} (sudo)"
sudo tee "$UNIT" >/dev/null <<EOF
[Unit]
Description=routing control panel (/default)
After=network.target

[Service]
User=${USER}
Environment=HOME=${HOME}
Environment=PANEL_PORT=${PANEL_PORT}
${TOKEN_LINE}
ExecStart=/usr/bin/python3 ${ROOT}/scripts/panel.py
Restart=on-failure
RestartSec=2

[Install]
WantedBy=multi-user.target
EOF

say "Enabling + (re)starting routing-panel"
sudo systemctl daemon-reload
sudo systemctl enable routing-panel >/dev/null
sudo systemctl restart routing-panel
ok "Panel running on http://127.0.0.1:${PANEL_PORT}/default"
echo "  (reachable through Caddy at /default once scripts/apply.sh has run)"
