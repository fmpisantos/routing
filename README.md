# routing

Path-based reverse proxy for this host. One config file (`routes.json`) maps
URL paths to local ports; everything else is generated.

```
Public internet
     https://<host>.ts.net/<path>/...
Tailscale Funnel ──► Caddy on 127.0.0.1:8080
   (forwards everything,        │
    no path logic)              └─ path-based routing happens HERE,
                                   driven by routes.json
```

## Config: `routes.json`

```json
[
    {
        "path": "/flowlet/api/*",
        "port": 8000
    },
    {
        "path": "/flowlet/*",
        "port": 3000
    }
]
```

A request whose path matches `path` is forwarded to `127.0.0.1:<port>`.
Longest path wins, regardless of order in the file (`/flowlet/api/*` is
checked before `/flowlet/*`). Unmatched requests get a 404, unless you
define a `"/*"` catch-all route.

Optional per-route key:

- `"strip": true` — strip the matched prefix before forwarding
  (Caddy `handle_path`). A request to `/notes/today` reaches the backend
  as `/today`. **Default is false**: the backend receives the full
  original path (Caddy `handle`), which is what apps mounted under a
  prefix expect — this is the whole reason Caddy is used instead of
  `tailscale serve --set-path`, which always strips.

## Usage

```sh
# 1. Edit routes.json, then generate + validate + deploy to /etc/caddy + reload:
scripts/apply.sh

# 2. Expose Caddy publicly via Tailscale Funnel (idempotent):
scripts/funnel-on.sh

# Stop exposing the host:
scripts/funnel-off.sh
```

`scripts/generate-caddyfile.py` can also be run standalone — it prints the
Caddyfile to stdout, so you can preview before deploying:

```sh
python3 scripts/generate-caddyfile.py            # uses ./routes.json
python3 scripts/generate-caddyfile.py --listen 9090 other-routes.json
```

The generated `Caddyfile` in the repo root is a build artifact — never edit
it by hand; edit `routes.json` and re-run `scripts/apply.sh`.

## Requirements

- `caddy` running as a systemd service (`sudo apt install caddy`)
- `tailscale` with Funnel enabled for this host
- `python3` (stdlib only)

## Notes

- `PROXY_PORT` env var changes the Caddy listen port everywhere
  (default 8080); `FUNNEL=0 scripts/funnel-on.sh` exposes to the tailnet
  only instead of the public internet.
- WebSocket upgrades (e.g. Next.js HMR) are auto-detected and forwarded
  by Caddy's `reverse_proxy` — nothing to configure.
- Backends behind a prefixed path must expect that prefix (or set
  `"strip": true`). For example, moving Flowlet from `/*` to `/flowlet/*`
  means Next.js needs `basePath: "/flowlet"` and FastAPI a matching
  mount — the proxy preserves whatever path the client sent.
