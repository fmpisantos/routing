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
        "projectName": "Flowlet",
        "launchScript": "$HOME/Flowlet/scripts/launch.sh",
        "appPath": "$HOME/Flowlet",
        "paths": [
            {
                "path": "/flowlet/api/*",
                "port": 8000
            },
            {
                "path": "/flowlet/*",
                "port": 3000
            }
        ]
    }
]
```

Each project groups the paths that belong to it. A request whose path
matches `path` is forwarded to `127.0.0.1:<port>`. Longest path wins,
regardless of order in the file (`/flowlet/api/*` is checked before
`/flowlet/*`). Unmatched requests get a 404, unless you define a `"/*"`
catch-all route.

`launchScript` is optional — it is the shell command the control panel
(below) runs to start the project (via `bash -lc`, so `$HOME` etc.
expand). Projects without one still show status and can be stopped, but
not started.

`appPath` is optional — the project's git working directory (`$HOME`/`~`
expand). When set alongside `launchScript`, the panel's **Update &
Restart** button stops the project, runs `git pull --rebase` there, and
starts it again.

Optional per-path key:

- `"strip": true` — strip the matched prefix before forwarding
  (Caddy `handle_path`). A request to `/notes/today` reaches the backend
  as `/today`. **Default is false**: the backend receives the full
  original path (Caddy `handle`), which is what apps mounted under a
  prefix expect — this is the whole reason Caddy is used instead of
  `tailscale serve --set-path`, which always strips.

## Control panel: `/default`

`scripts/panel.py` serves a dashboard at `/default` that shows whether
each project is running (per-port listening status) and has Start/Stop
buttons. The Caddy route for it is injected automatically — `/default`
is a reserved prefix and cannot be used in `routes.json`.

- **Start** runs the project's `launchScript` detached in its own
  process group; output is captured to
  `~/.local/state/routing-panel/logs/<project>.log` (viewable from the
  page under "launch log").
- **Stop** SIGTERMs the process groups listening on the project's ports
  (it works even for projects started outside the panel, as long as
  they run as the same user), escalating to SIGKILL after 5 s.
- **Update & Restart** (shown when a project has both `launchScript` and
  `appPath`) stops the project, runs `git pull --rebase` in `appPath`,
  then starts it again. It runs in the background and streams each step
  to the launch log; if the pull fails (e.g. local changes, conflicts)
  it stops there and does not restart.
- **Environment** (per project, expandable) is an editable table of env
  variables the panel injects into the project's process when it starts.
  Add/edit/remove rows and hit **Save**; they take effect on the next
  **Start** or **Update & Restart** (not on an already-running process).
  Overrides are stored per project in
  `~/.local/state/routing-panel/env.json` (chmod `600` — it may hold
  secrets) and so survive `routes.json` regeneration. They are layered on
  top of the panel's own environment, so a key set here wins over an
  inherited one of the same name. The header shows a count, e.g.
  "environment (3)".
- **Apply routes** (header button) runs `scripts/apply.sh` — regenerate
  the Caddyfile from `routes.json`, validate it, deploy it to
  `/etc/caddy` and reload Caddy — so route edits take effect without
  shelling in. Output appears under "apply output". The deploy + reload
  steps use `sudo`, so see the note below for it to work from the panel.

> **Passwordless sudo for Apply routes:** the panel runs as your user
> (see `install-panel.sh`), and `apply.sh` uses `sudo` to write
> `/etc/caddy/Caddyfile` and reload Caddy. From the panel there is no
> terminal to type a password, so grant just those two commands via a
> sudoers drop-in (replace `<user>`):
>
> ```sh
> sudo tee /etc/sudoers.d/routing-panel >/dev/null <<'EOF'
> <user> ALL=(root) NOPASSWD: /usr/bin/cp * /etc/caddy/Caddyfile, /usr/bin/systemctl reload-or-restart caddy
> EOF
> sudo chmod 440 /etc/sudoers.d/routing-panel
> ```
>
> Without this, the button still works but the apply output shows a
> `sudo: a password is required` error instead of reloading Caddy.

Install it as a systemd service:

```sh
scripts/install-panel.sh                    # http://127.0.0.1:8090/default
PANEL_TOKEN=<secret> scripts/install-panel.sh   # with auth (see warning)
```

> **Warning:** if the proxy is exposed via Funnel, `/default` is public —
> anyone on the internet could start/stop your projects. Set
> `PANEL_TOKEN`; the page prompts for it on first use and sends it as an
> `X-Panel-Token` header.

Or run it in the foreground for a quick look:
`python3 scripts/panel.py` (env: `PANEL_PORT`, `PANEL_BIND`, `PANEL_PATH`,
`PANEL_TOKEN`, `ROUTES_CONFIG`).

## Multiple instances on one host

You can run two (or more) fully independent instances on the same machine —
e.g. one per Linux user, each with its own projects, panel path, and Caddy.
One knob, `INSTANCE_NAME`, names everything; ports stay explicit. **Every
instance needs a unique `INSTANCE_NAME`, `PROXY_PORT`, `PANEL_PORT`, and
`ADMIN_PORT`.**

| Env | Default | Purpose |
|-----|---------|---------|
| `INSTANCE_NAME` | `default` | names the panel + caddy systemd units; default panel path |
| `PANEL_PATH` | `/$INSTANCE_NAME` | URL prefix the panel is served at |
| `PROXY_PORT` | `8080` | Caddy listen port |
| `PANEL_PORT` | `8090` | panel listen port |
| `ADMIN_PORT` | `2019` | Caddy admin API port (must differ per instance) |

The **default instance is unchanged**: it uses the distro `caddy` service
reading `/etc/caddy/Caddyfile`, the unit `routing-panel`, and the path
`/default`. A **named** instance instead runs its own `caddy-<name>.service`
straight off its repo's `Caddyfile` (no `/etc/caddy`, no `sudo cp`), with a
distinct admin endpoint so the two Caddys don't clash on `localhost:2019`,
and a `routing-panel-<name>` panel unit.

Set up a second instance as another user:

```sh
# As the second user, in their own clone of this repo with their routes.json:
INSTANCE_NAME=devb PANEL_PATH=/devb \
PROXY_PORT=8081 PANEL_PORT=8091 ADMIN_PORT=2020 \
./init.sh
```

That user's panel is then at `:8091/devb` directly and `:8081/devb` through
their Caddy. Because a named instance's Caddy reloads via its admin API,
the panel's **Apply routes** button needs no sudo (the passwordless-sudo
note below applies only to the default instance).

> **Tailscale Funnel:** a tailnet node has a single serve/funnel config, so
> only one instance can be publicly funneled — `funnel-on.sh` maps one
> `PROXY_PORT` and the last writer wins. Expose one instance publicly and
> reach the others on the tailnet/localhost, or front them behind the
> funneled one.

## Usage

```sh
# 1. Edit routes.json, then generate + validate + deploy to /etc/caddy + reload:
scripts/apply.sh

# 2. Expose Caddy publicly via Tailscale Funnel (idempotent):
scripts/funnel-on.sh

# 3. (once) Install the /default control panel as a systemd service:
scripts/install-panel.sh

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

Run `./init.sh` to install all of the above (Debian/Ubuntu) and bootstrap
`routes.json` from the example.

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
