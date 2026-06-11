#!/usr/bin/env bash
# Install everything this repo needs: caddy (as a systemd service),
# tailscale, and python3. Also bootstraps routes.json from the example
# if it doesn't exist yet.
#
# Idempotent: safe to re-run; already-installed tools are skipped.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

say()  { printf "\033[36m▸\033[0m %s\n" "$*"; }
ok()   { printf "\033[32m✓\033[0m %s\n" "$*"; }
warn() { printf "\033[33m!\033[0m %s\n" "$*" >&2; }

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

# --- repo bootstrap --------------------------------------------------------
if [[ -f "$ROOT/routes.json" ]]; then
  ok "routes.json already exists"
else
  say "Creating routes.json from routes.example.json"
  cp "$ROOT/routes.example.json" "$ROOT/routes.json"
  ok "routes.json created — edit it, then run scripts/apply.sh"
fi

chmod +x "$ROOT"/scripts/*.sh

echo
ok "All requirements installed. Next steps:"
echo "  1. Edit routes.json"
echo "  2. scripts/apply.sh          # generate + deploy + reload Caddy"
echo "  3. scripts/funnel-on.sh      # expose via Tailscale Funnel"
echo "  4. scripts/install-panel.sh  # /default start/stop dashboard"
