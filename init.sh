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
#   PROXY_PORT  — Caddy listen port (default 8080)
#   PANEL_PORT  — control panel port (default 8090)
#   PANEL_TOKEN — panel auth token (recommended if exposed via Funnel)

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

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

say "Enabling caddy systemd service"
sudo systemctl enable --now caddy
ok "caddy service enabled and running"

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
  printf "\033[36m▸\033[0m Enter a PANEL_TOKEN to protect /default (leave empty for no token): "
  read -r PANEL_TOKEN
  export PANEL_TOKEN
fi

say "Setting up the /default control panel (scripts/install-panel.sh)"
"$ROOT/scripts/install-panel.sh"
ok "Control panel set up"

echo
ok "Initialization complete."
echo "  • Routes are live on :${PROXY_PORT:-8080} (edit routes.json + rerun scripts/apply.sh to change)"
echo "  • Control panel: http://127.0.0.1:${PANEL_PORT:-8090}/default"
echo
echo "Optional next step — expose this host publicly via Tailscale Funnel:"
echo "  scripts/funnel-on.sh   (needs 'sudo tailscale up' first if not connected)"
