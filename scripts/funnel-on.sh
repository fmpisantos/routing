#!/usr/bin/env bash
# Expose Caddy publicly via Tailscale Funnel.
#
#   https://<host>.ts.net/...  →  Caddy on :8080  →  routes.json mappings
#
# Tailscale forwards everything to a single port (Caddy); Caddy does the
# actual path-based routing and — unlike `tailscale serve --set-path` —
# preserves path prefixes by default so backends mounted under a prefix
# see the path they expect.
#
# Idempotent: replaces whatever serve/funnel config is currently set.
# Env: PROXY_PORT (default 8080), FUNNEL=1 (0 = tailnet-only Serve).

set -euo pipefail

PROXY_PORT="${PROXY_PORT:-8080}"
FUNNEL="${FUNNEL:-1}"
CMD=$([[ "$FUNNEL" == "1" ]] && echo "funnel" || echo "serve")
# The default instance uses the distro `caddy` service; named instances run
# their own caddy-<name>. NOTE: a tailnet node has ONE funnel/serve config,
# so only one instance can be publicly exposed — the last run of this wins.
INSTANCE_NAME="${INSTANCE_NAME:-default}"
if [[ "$INSTANCE_NAME" == "default" ]]; then
  CADDY_SERVICE=caddy
else
  CADDY_SERVICE="caddy-$INSTANCE_NAME"
fi

say()  { printf "\033[36m▸\033[0m %s\n" "$*"; }
ok()   { printf "\033[32m✓\033[0m %s\n" "$*"; }
warn() { printf "\033[33m!\033[0m %s\n" "$*" >&2; }

if ! command -v tailscale >/dev/null 2>&1; then
  warn "tailscale CLI not found on PATH"
  exit 1
fi

if ! systemctl is-active --quiet "$CADDY_SERVICE"; then
  warn "Caddy ($CADDY_SERVICE) is not running. Deploy the config first:"
  warn "  scripts/apply.sh"
  exit 1
fi

say "Resetting current serve + funnel config"
tailscale funnel reset 2>/dev/null || true
tailscale serve reset 2>/dev/null || true
ok "Cleared"

say "Mounting / → http://127.0.0.1:${PROXY_PORT}  (via tailscale ${CMD})"
tailscale "$CMD" --bg "http://127.0.0.1:${PROXY_PORT}"

echo
say "Active config:"
tailscale serve status
