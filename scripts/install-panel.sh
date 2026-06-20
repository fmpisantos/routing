#!/usr/bin/env bash
# Install + enable the control panel (scripts/panel.py) as a systemd
# service so the dashboard is always available.
#
# Idempotent: re-running rewrites the unit and restarts the service.
# Env: PANEL_PORT (default 8090), PANEL_TOKEN (optional — recommended
#      if the proxy is exposed publicly via Tailscale Funnel),
#      INSTANCE_NAME (default "default" — names the unit + default path),
#      PANEL_PATH (default /$INSTANCE_NAME), PROXY_PORT, ADMIN_PORT.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PANEL_PORT="${PANEL_PORT:-8090}"
INSTANCE_NAME="${INSTANCE_NAME:-default}"
# Named instances nest their panel under /<name>/default; default stays /default.
if [[ "$INSTANCE_NAME" == "default" ]]; then
  PANEL_PATH="${PANEL_PATH:-/default}"
else
  PANEL_PATH="${PANEL_PATH:-/$INSTANCE_NAME/default}"
fi
PROXY_PORT="${PROXY_PORT:-8080}"
ADMIN_PORT="${ADMIN_PORT:-2019}"
# Keep the legacy unit name for the default instance; suffix named ones.
if [[ "$INSTANCE_NAME" == "default" ]]; then
  SERVICE=routing-panel
else
  SERVICE="routing-panel-$INSTANCE_NAME"
fi
UNIT="/etc/systemd/system/$SERVICE.service"

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
# All generator-affecting vars are put in the unit env because the panel's
# "Apply routes" button runs apply.sh inheriting this environment — without
# them a panel-triggered regenerate would use the wrong path/ports/admin.
sudo tee "$UNIT" >/dev/null <<EOF
[Unit]
Description=routing control panel (${PANEL_PATH})
After=network.target

[Service]
User=${USER}
Environment=HOME=${HOME}
Environment=INSTANCE_NAME=${INSTANCE_NAME}
Environment=PANEL_PORT=${PANEL_PORT}
Environment=PANEL_PATH=${PANEL_PATH}
Environment=PROXY_PORT=${PROXY_PORT}
Environment=ADMIN_PORT=${ADMIN_PORT}
${TOKEN_LINE}
ExecStart=/usr/bin/python3 ${ROOT}/scripts/panel.py
Restart=on-failure
RestartSec=2

[Install]
WantedBy=multi-user.target
EOF

say "Enabling + (re)starting ${SERVICE}"
sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE" >/dev/null
sudo systemctl restart "$SERVICE"
ok "Panel running on http://127.0.0.1:${PANEL_PORT}${PANEL_PATH}"
echo "  (reachable through Caddy at ${PANEL_PATH} once scripts/apply.sh has run)"
