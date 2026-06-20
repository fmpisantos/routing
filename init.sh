#!/usr/bin/env bash
# One-shot initializer for this repo. It:
#   1. Requires routes.json to exist (copy routes.example.json and edit it).
#   2. Installs everything needed: caddy (as a systemd service),
#      tailscale, and python3.
#   3. Applies the routes (scripts/apply.sh) — generate + deploy the
#      Caddyfile and reload Caddy.
#   4. Sets up the /default control panel (scripts/install-panel.sh).
#
# Idempotent: safe to re-run; already-installed tools are skipped and the
# apply/panel steps simply re-deploy.
#
# Env passed through to the steps it runs:
#   INSTANCE_NAME — instance id (default "default"). The default instance
#                   uses the shared distro Caddy; any other name runs its
#                   own caddy-<name> service so two instances can coexist.
#   PANEL_PATH    — URL prefix for the panel (default /$INSTANCE_NAME)
#   PROXY_PORT    — Caddy listen port (default 8080)
#   PANEL_PORT    — control panel port (default 8090)
#   ADMIN_PORT    — Caddy admin API port (default 2019; must differ per
#                   instance)
#   PANEL_TOKEN   — panel auth token (recommended if exposed via Funnel)

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Instance identity — defaulted + exported so every sub-step agrees.
INSTANCE_NAME="${INSTANCE_NAME:-default}"
PANEL_PATH="${PANEL_PATH:-/$INSTANCE_NAME}"
PROXY_PORT="${PROXY_PORT:-8080}"
PANEL_PORT="${PANEL_PORT:-8090}"
ADMIN_PORT="${ADMIN_PORT:-2019}"
export INSTANCE_NAME PANEL_PATH PROXY_PORT PANEL_PORT ADMIN_PORT

say()  { printf "\033[36m▸\033[0m %s\n" "$*"; }
ok()   { printf "\033[32m✓\033[0m %s\n" "$*"; }
warn() { printf "\033[33m!\033[0m %s\n" "$*" >&2; }

# --- routes.json must exist (first validation) -----------------------------
if [[ ! -f "$ROOT/routes.json" ]]; then
  warn "routes.json is missing — nothing will run."
  warn "Create it first, e.g.:  cp routes.example.json routes.json  (then edit it)"
  exit 1
fi
ok "routes.json found"

# --- guard: don't let a second user hijack the shared default instance -----
# The "default" instance owns host-global resources (/etc/caddy/Caddyfile and
# the shared caddy + routing-panel systemd units). If routing-panel.service is
# already owned by a different user, re-running the default here would stomp
# their setup and take over their /default panel. Named instances are fully
# isolated (caddy-<name>/routing-panel-<name>), so they're always allowed.
if [[ "$INSTANCE_NAME" == "default" ]]; then
  existing_user="$(systemctl show -p User --value routing-panel.service 2>/dev/null || true)"
  if [[ -n "$existing_user" && "$existing_user" != "$USER" ]]; then
    warn "The default instance is already owned by user '$existing_user'."
    warn "Running it as '$USER' would hijack their /default panel and the"
    warn "shared /etc/caddy/Caddyfile. Run a named instance instead, e.g.:"
    warn "  INSTANCE_NAME=$USER PANEL_PATH=/$USER PROXY_PORT=8081 \\"
    warn "  PANEL_PORT=8091 ADMIN_PORT=2020 ./init.sh"
    warn "(To deliberately take over anyway, set ALLOW_DEFAULT_TAKEOVER=1.)"
    [[ "${ALLOW_DEFAULT_TAKEOVER:-}" == "1" ]] || exit 1
  fi
fi

if ! command -v apt-get >/dev/null 2>&1; then
  warn "This script expects a Debian/Ubuntu system (apt-get not found)."
  warn "Install manually: caddy, tailscale, python3"
  exit 1
fi

say "Updating package index"
sudo apt-get update -qq
ok "Package index updated"

# --- python3 ---------------------------------------------------------------
if command -v python3 >/dev/null 2>&1; then
  ok "python3 already installed ($(python3 --version))"
else
  say "Installing python3"
  sudo apt-get install -y python3
  ok "python3 installed"
fi

# --- caddy -----------------------------------------------------------------
if command -v caddy >/dev/null 2>&1; then
  ok "caddy already installed ($(caddy version | cut -d' ' -f1))"
else
  say "Installing caddy"
  sudo apt-get install -y caddy
  ok "caddy installed"
fi

if [[ "$INSTANCE_NAME" == "default" ]]; then
  say "Enabling caddy systemd service"
  sudo systemctl enable --now caddy
  ok "caddy service enabled and running"
else
  # Named instance: run a dedicated Caddy off this repo's Caddyfile with a
  # distinct admin port, so it doesn't fight the default instance over
  # /etc/caddy/Caddyfile or the localhost:2019 admin endpoint.
  CADDY_UNIT="/etc/systemd/system/caddy-$INSTANCE_NAME.service"
  CADDY_BIN="$(command -v caddy)"
  say "Writing ${CADDY_UNIT} (sudo)"
  sudo tee "$CADDY_UNIT" >/dev/null <<EOF
[Unit]
Description=caddy (routing instance ${INSTANCE_NAME})
After=network.target

[Service]
User=${USER}
ExecStart=${CADDY_BIN} run --config ${ROOT}/Caddyfile --adapter caddyfile
ExecReload=${CADDY_BIN} reload --config ${ROOT}/Caddyfile --adapter caddyfile --address localhost:${ADMIN_PORT}
Restart=on-failure
RestartSec=2

[Install]
WantedBy=multi-user.target
EOF
  sudo systemctl daemon-reload
  sudo systemctl enable "caddy-$INSTANCE_NAME" >/dev/null
  ok "caddy-$INSTANCE_NAME service installed (started after routes are applied)"
fi

# --- tailscale -------------------------------------------------------------
if command -v tailscale >/dev/null 2>&1; then
  ok "tailscale already installed ($(tailscale version | head -n1))"
else
  say "Installing tailscale (official install script)"
  curl -fsSL https://tailscale.com/install.sh | sh
  ok "tailscale installed"
fi

if ! tailscale status >/dev/null 2>&1; then
  warn "tailscale is installed but not connected. Run: sudo tailscale up"
fi

chmod +x "$ROOT"/scripts/*.sh

# --- apply routes ----------------------------------------------------------
say "Applying routes (scripts/apply.sh)"
"$ROOT/scripts/apply.sh"
ok "Routes applied"

# --- control panel ---------------------------------------------------------
# Ask for a panel auth token if one wasn't provided via the environment.
if [[ -z "${PANEL_TOKEN:-}" ]]; then
  printf "\033[36m▸\033[0m Enter a PANEL_TOKEN to protect %s (leave empty for no token): " "$PANEL_PATH"
  read -r PANEL_TOKEN
  export PANEL_TOKEN
fi

say "Setting up the ${PANEL_PATH} control panel (scripts/install-panel.sh)"
"$ROOT/scripts/install-panel.sh"
ok "Control panel set up"

echo
ok "Initialization complete (instance: ${INSTANCE_NAME})."
echo "  • Routes are live on :${PROXY_PORT} (edit routes.json + rerun scripts/apply.sh to change)"
echo "  • Control panel: http://127.0.0.1:${PANEL_PORT}${PANEL_PATH}"
echo
echo "Optional next step — expose this host publicly via Tailscale Funnel:"
echo "  scripts/funnel-on.sh   (needs 'sudo tailscale up' first if not connected)"
