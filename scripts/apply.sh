#!/usr/bin/env bash
# Regenerate the Caddyfile from routes.json, validate it, deploy it to
# /etc/caddy/Caddyfile and reload Caddy.
#
# Usage: scripts/apply.sh [path/to/routes.json]
# Env:   PROXY_PORT (default 8080) — port Caddy listens on.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG="${1:-$ROOT/routes.json}"
OUT="$ROOT/Caddyfile"

# Instance identity. The default instance uses the shared distro Caddy
# (/etc/caddy/Caddyfile); any named instance runs its own caddy-<name>
# service off this repo's Caddyfile and gets a distinct admin port.
INSTANCE_NAME="${INSTANCE_NAME:-default}"
PANEL_PATH="${PANEL_PATH:-/$INSTANCE_NAME}"
ADMIN_PORT="${ADMIN_PORT:-2019}"
export PANEL_PATH ADMIN_PORT  # consumed by generate-caddyfile.py

say()  { printf "\033[36m▸\033[0m %s\n" "$*"; }
ok()   { printf "\033[32m✓\033[0m %s\n" "$*"; }
warn() { printf "\033[33m!\033[0m %s\n" "$*" >&2; }

say "Generating Caddyfile from ${CONFIG}"
python3 "$ROOT/scripts/generate-caddyfile.py" "$CONFIG" -o "$OUT" \
  --panel-path "$PANEL_PATH" --admin-port "$ADMIN_PORT"
ok "Generated $OUT"

if command -v caddy >/dev/null 2>&1; then
  say "Validating"
  caddy validate --config "$OUT" --adapter caddyfile >/dev/null
  ok "Config is valid"
else
  warn "caddy CLI not found — skipping validation. Install it: sudo apt install caddy"
fi

if [[ "$INSTANCE_NAME" == "default" ]]; then
  # Default instance: the shared distro Caddy reads /etc/caddy/Caddyfile.
  say "Deploying to /etc/caddy/Caddyfile (sudo)"
  sudo cp "$OUT" /etc/caddy/Caddyfile
  sudo systemctl reload-or-restart caddy
  ok "Caddy reloaded"
else
  # Named instance: this instance's own caddy-<name> service runs straight
  # off $OUT. Reload it via its admin API (no sudo). If the admin endpoint
  # isn't up yet (first apply, before the unit is started), fall back to a
  # service restart.
  say "Reloading caddy-$INSTANCE_NAME via admin localhost:$ADMIN_PORT"
  if caddy reload --config "$OUT" --adapter caddyfile \
       --address "localhost:$ADMIN_PORT" 2>/dev/null; then
    ok "Caddy reloaded"
  elif systemctl cat "caddy-$INSTANCE_NAME.service" >/dev/null 2>&1; then
    warn "admin API not reachable — restarting caddy-$INSTANCE_NAME (sudo)"
    sudo systemctl restart "caddy-$INSTANCE_NAME"
    ok "caddy-$INSTANCE_NAME restarted"
  else
    warn "caddy-$INSTANCE_NAME is not installed yet — run init.sh / install it,"
    warn "then this apply will reload it. Config written to $OUT."
  fi
fi

echo
say "Routes now active on :${PROXY_PORT:-8080}:"
grep -E 'handle|reverse_proxy' "$OUT" | sed 's/^\t*/  /'
