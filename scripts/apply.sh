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

say()  { printf "\033[36m▸\033[0m %s\n" "$*"; }
ok()   { printf "\033[32m✓\033[0m %s\n" "$*"; }
warn() { printf "\033[33m!\033[0m %s\n" "$*" >&2; }

say "Generating Caddyfile from ${CONFIG}"
python3 "$ROOT/scripts/generate-caddyfile.py" "$CONFIG" -o "$OUT"
ok "Generated $OUT"

if command -v caddy >/dev/null 2>&1; then
  say "Validating"
  caddy validate --config "$OUT" --adapter caddyfile >/dev/null
  ok "Config is valid"
else
  warn "caddy CLI not found — skipping validation. Install it: sudo apt install caddy"
fi

say "Deploying to /etc/caddy/Caddyfile (sudo)"
sudo cp "$OUT" /etc/caddy/Caddyfile
sudo systemctl reload-or-restart caddy
ok "Caddy reloaded"

echo
say "Routes now active on :${PROXY_PORT:-8080}:"
grep -E 'handle|reverse_proxy' "$OUT" | sed 's/^\t*/  /'
