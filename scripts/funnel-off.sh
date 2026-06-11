#!/usr/bin/env bash
# Stop exposing this host: clear all Tailscale serve + funnel config.

set -euo pipefail

say()  { printf "\033[36m▸\033[0m %s\n" "$*"; }
ok()   { printf "\033[32m✓\033[0m %s\n" "$*"; }

say "Clearing tailscale serve + funnel config"
tailscale funnel reset 2>/dev/null || true
tailscale serve reset 2>/dev/null || true
ok "This host is no longer exposed"
