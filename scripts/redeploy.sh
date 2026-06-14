#!/usr/bin/env bash
# Redeploy the routing stack so the panel runs the latest code.
#
# Order:
#   1. Stop the running panel (so it releases its port cleanly).
#   2. scripts/apply.sh         — regenerate + deploy the Caddyfile, reload Caddy.
#   3. scripts/install-panel.sh — rewrite the systemd unit, daemon-reload and
#                                 restart the panel. The existing PANEL_TOKEN /
#                                 PANEL_PORT are read back from the current unit
#                                 and preserved (env vars override them).
#   4. Verify the panel is healthy: service active, port listening, and the
#      /default dashboard returns 200 both directly and through Caddy.
#
# Needs sudo: apply.sh and install-panel.sh write under /etc and manage systemd.
# Run it from a terminal so you can answer the sudo prompt.
#
# Env overrides:
#   PANEL_PORT   panel listen port (default: from current unit, else 8090)
#   PANEL_TOKEN  panel API token   (default: preserved from current unit)
#   PROXY_PORT   Caddy listen port for the reachability check (default 8080)

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT=/etc/systemd/system/routing-panel.service
SERVICE=routing-panel
PROXY_PORT="${PROXY_PORT:-8080}"

say()  { printf "\033[36m▸\033[0m %s\n" "$*"; }
ok()   { printf "\033[32m✓\033[0m %s\n" "$*"; }
warn() { printf "\033[33m!\033[0m %s\n" "$*" >&2; }
die()  { printf "\033[31m✗\033[0m %s\n" "$*" >&2; exit 1; }

# --- preflight -------------------------------------------------------------
[[ -f "$ROOT/routes.json" ]]              || die "routes.json not found in $ROOT — run init.sh first"
[[ -x "$ROOT/scripts/apply.sh" ]]         || die "scripts/apply.sh missing or not executable"
[[ -x "$ROOT/scripts/install-panel.sh" ]] || die "scripts/install-panel.sh missing or not executable"
command -v python3 >/dev/null             || die "python3 not found"
command -v caddy   >/dev/null             || warn "caddy CLI not found — apply.sh will skip validation"

# Warm the sudo credential cache up front so the run doesn't stall midway;
# apply.sh and install-panel.sh both invoke sudo internally.
say "Caching sudo credentials (you may be prompted)"
sudo -v || die "sudo is required — apply.sh and install-panel.sh write to /etc and manage systemd"

# --- preserve existing panel env (PANEL_TOKEN / PANEL_PORT) -----------------
# install-panel.sh rewrites the unit from scratch and DROPS the token if
# PANEL_TOKEN isn't in its environment. Recover whatever the running unit has,
# unless the caller overrode it via the environment.
unit_env() {  # unit_env KEY -> value of the unit's `Environment=KEY=value`
  [[ -r "$UNIT" ]] || return 0
  sed -n "s/^Environment=$1=//p" "$UNIT" | head -n1
}

export PANEL_PORT="${PANEL_PORT:-$(unit_env PANEL_PORT)}"
export PANEL_PORT="${PANEL_PORT:-8090}"

existing_token="$(unit_env PANEL_TOKEN)"
if [[ -n "${PANEL_TOKEN:-}" ]]; then
  export PANEL_TOKEN
  say "Using PANEL_TOKEN from environment"
elif [[ -n "$existing_token" ]]; then
  export PANEL_TOKEN="$existing_token"
  say "Preserving existing PANEL_TOKEN from the current unit"
else
  warn "No PANEL_TOKEN set or found — panel will be installed without auth"
fi

# --- 1. stop the running panel ---------------------------------------------
if systemctl is-active --quiet "$SERVICE"; then
  say "Stopping $SERVICE"
  sudo systemctl stop "$SERVICE"
  ok "Stopped"
else
  say "$SERVICE not running — nothing to stop"
fi

# --- 2. regenerate + deploy the Caddyfile ----------------------------------
say "Applying routes (regenerate + deploy Caddyfile, reload Caddy)"
"$ROOT/scripts/apply.sh"
ok "Caddyfile applied"

# --- 3. rebuild + restart the panel ----------------------------------------
say "Installing + restarting panel on port $PANEL_PORT"
"$ROOT/scripts/install-panel.sh"
ok "Panel service (re)installed and restarted"

# --- 4. verify -------------------------------------------------------------
say "Verifying $SERVICE is healthy"

if ! systemctl is-active --quiet "$SERVICE"; then
  warn "$SERVICE is not active — recent logs:"
  journalctl -u "$SERVICE" -n 30 --no-pager >&2 || true
  die "$SERVICE failed to start"
fi
ok "service active"

# Port can take a beat to bind after the restart — retry briefly.
listening=0
for _ in $(seq 1 20); do
  if ss -ltnH "sport = :$PANEL_PORT" | grep -q .; then listening=1; break; fi
  sleep 0.5
done
if [[ "$listening" != 1 ]]; then
  journalctl -u "$SERVICE" -n 30 --no-pager >&2 || true
  die "panel port $PANEL_PORT never started listening"
fi
ok "listening on 127.0.0.1:$PANEL_PORT"

# Dashboard page needs no token, so this works with or without PANEL_TOKEN set.
code="$(curl -fsS -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PANEL_PORT/default/" || true)"
[[ "$code" == "200" ]] || die "panel did not return 200 directly (got '${code:-no response}')"
ok "panel responds 200 at http://127.0.0.1:$PANEL_PORT/default/"

# Through Caddy — confirms apply.sh wired the /default route in.
code="$(curl -fsS -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PROXY_PORT/default/" || true)"
if [[ "$code" == "200" ]]; then
  ok "reachable through Caddy at http://127.0.0.1:$PROXY_PORT/default/"
else
  warn "panel is up but not reachable through Caddy on :$PROXY_PORT/default/ (got '${code:-no response}')"
  warn "check that Caddy is running (systemctl status caddy) and that apply.sh succeeded above"
fi

echo
ok "Redeploy complete — panel is running the latest code."
