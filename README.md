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

`paths` may be empty (or left out) for a project that has nothing to route
yet — that is how the panel's **New project** registers a folder. Such a
project still gets a card (badge "no routes"), a Claude session button and,
once you add a `launchScript`, Start/Stop.

`launchScript` is optional — it is the shell command the control panel
(below) runs to start the project (via `bash -lc`, so `$HOME` etc.
expand). Projects without one still show status and can be stopped, but
not started.

`appPath` is optional — the project's git working directory (`$HOME`/`~`
expand). When set alongside `launchScript`, the panel's **Update &
Restart** button stops the project, runs `git pull --rebase` there, and
starts it again. It is also the folder a [Claude session](#claude-sessions)
for the project runs in.

`envFile` is optional — a path to a `.env` file whose `KEY=value` lines the
panel loads into the project's process when it starts (`$HOME`/`~` expand; a
relative path is resolved against `appPath`, else the repo root). See
[Environment variables](#environment-variables).

`launchOnStart` is optional (default `false`) — when `true`, the panel runs
the project's `launchScript` as soon as the panel itself starts, i.e. on
boot and on every panel restart. Needs a `launchScript`; a project whose
ports are already listening is left alone, so this never starts a second
copy. The panel's **Start on boot** switch writes this key back into
`routes.json`, so the two are always the same setting.

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
- **Start on boot** (per project, a switch) toggles `launchOnStart` in
  `routes.json` — the file is rewritten immediately, so the choice
  survives a reboot. On boot (the panel is an enabled systemd service) and
  on every panel restart, each project with the switch on is started unless
  its ports are already listening. Results go to the service journal
  (`journalctl -u routing-panel`) and, on failure, to the project's launch
  log. The switch is disabled for projects with no `launchScript`.
- **Environment** (per project, expandable) is an **env file** path plus an
  editable table of individual variables, both injected into the project's
  process when it starts. See [Environment variables](#environment-variables)
  below. Changes take effect on the next **Start** or **Update & Restart**
  (not on an already-running process). The header shows how many variables
  the project will get, e.g. "environment (12)".
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

- **Restart panel** (header button) runs `scripts/redeploy.sh` so the
  panel picks up new code without shelling in: it applies routes, then
  reinstalls and restarts the panel service, and verifies it came back.
  Output appears under "redeploy output". Because the script restarts the
  panel itself, it is launched *detached* (as a `systemd-run` transient
  unit) so it survives the restart that would otherwise kill it; the
  dashboard disconnects for a few seconds and the log keeps streaming once
  it reconnects.

> **Passwordless sudo for Restart panel:** unlike Apply routes, a full
> redeploy stops/starts the panel unit, rewrites `/etc/systemd/system`
> and runs `systemd-run`, so it needs broader rights. The panel launches
> it with `sudo -n systemd-run …`, and `redeploy.sh`/`install-panel.sh`
> use `sudo` internally. Grant these (replace `<user>`; this is close to
> full root — only do it on a machine you trust):
>
> ```sh
> sudo tee /etc/sudoers.d/routing-panel-redeploy >/dev/null <<'EOF'
> <user> ALL=(root) NOPASSWD: /usr/bin/systemd-run *
> <user> ALL=(root) NOPASSWD: /usr/bin/systemctl, /usr/bin/tee /etc/systemd/system/*
> EOF
> sudo chmod 440 /etc/sudoers.d/routing-panel-redeploy
> ```
>
> Without this, the button still works but the redeploy output shows a
> `sudo: a password is required` error and the panel is left running its
> current code. You can always redeploy from a terminal with
> `scripts/redeploy.sh` instead.

Install it as a systemd service:

```sh
scripts/install-panel.sh                    # http://127.0.0.1:8090/default
PANEL_TOKEN=<secret> scripts/install-panel.sh   # with auth (see warning)
```

> **Warning:** if the proxy is exposed via Funnel, `/default` is public —
> anyone on the internet could start/stop your projects, set environment
> variables for them and redeploy the panel. Set `PANEL_TOKEN`: the panel
> is then locked behind a sign-in page (see [Signing in](#signing-in)).
> To add a token to a panel that is already installed:
> `PANEL_TOKEN=<secret> scripts/redeploy.sh` (later redeploys keep it).
> **New project** and **Claude session** refuse to work at all (403) on a
> panel without a token.

Or run it in the foreground for a quick look:
`python3 scripts/panel.py` (env: `PANEL_PORT`, `PANEL_BIND`, `PANEL_PATH`,
`PANEL_TOKEN`, `PANEL_SESSION_DAYS`, `ROUTES_CONFIG`, `PROJECTS_ROOT`,
`CLAUDE_BIN`, `CLAUDE_PERMISSION_MODE`, `CLAUDE_MAX_SESSIONS`).

### Signing in

With a `PANEL_TOKEN` set, opening the panel shows a sign-in page and every
API call is refused until you sign in. There are two ways in:

- **A passkey** — Face ID or Touch ID on an iPhone or Mac, Windows Hello, an
  Android screen lock, or a hardware security key. This is the everyday way.
- **The setup token** — paste `PANEL_TOKEN`. Use it the first time on a new
  device, and as the way back in if you lose your passkeys.

To set a device up: sign in with the token, open **Security** (it opens by
itself after a token sign-in) and press **Add a passkey on this device**.
From then on the sign-in page offers **Sign in with a passkey**. An iPhone
and a Mac on the same Apple account share one passkey through iCloud
Keychain; a Windows PC needs its own (or can use the phone's by QR code).
**Security** also lists the passkeys with when each was last used, removes
them, and has **Sign out** and **Sign out everywhere**.

Things to know:

- **Passkeys need https and belong to one address.** They work at the
  Funnel address (`https://<host>.<tailnet>.ts.net/default/`), which is what
  they are tied to. Opened over plain http (a LAN or tailnet IP), the panel
  only accepts the token.
- **A device stays signed in for 30 days** (`PANEL_SESSION_DAYS`, kept across
  redeploys like the other optional settings). Sessions survive panel
  restarts.
- **Scripts** can still send the token on each request instead of signing
  in: `curl -H "X-Panel-Token: $TOKEN" …/default/api/projects`.
- **Wrong tokens are throttled.** Each costs a second, and ten within ten
  minutes pause all token use (the sign-in form and the header) for ten
  minutes. Passkeys and devices that are already signed in are unaffected —
  which also means someone hammering the public address can keep the token
  paused, but cannot keep you out of a device with a passkey.
- **Where it is stored:** passkey public keys in
  `~/.local/state/routing-panel/passkeys.json` and session hashes in
  `sessions.json` next to it (both mode 600). Deleting `passkeys.json`
  removes every passkey; deleting `sessions.json` signs everyone out (restart
  the panel after either).
- **Other apps on the same address:** your projects are served from the same
  hostname as the panel, so a script running in one of them can make
  requests to the panel from a browser that is signed in. Only host projects
  you trust.
- Passkey checks use the `python3-cryptography` package (`init.sh` installs
  it). Without it the panel still runs and the token still signs in.

### Claude sessions

Each project with an `appPath` has a **Claude session** button. It runs
[`claude remote-control`](https://code.claude.com/docs/en/remote-control) in
that folder, so you can work on the project from
[claude.ai/code](https://claude.ai/code) or the Claude mobile app. Claude and
its commands run on this host, with the project's real files; the app only
shows the session. Nothing new is exposed to the network — the host dials
out — and the session uses the Claude account that `claude` is logged in to
on this host.

- The card shows the session's state (`starting`, `connected`, `waiting for
  an answer`, `exited`) and, once connected, an **Open in Claude** button.
  It links to the conversation Claude created for the folder
  (`https://claude.ai/code/session_…`), which the Claude phone app claims: on
  a phone with the app installed, tapping it opens the app on that
  conversation; elsewhere it opens claude.ai. If the conversation cannot be
  identified, the button falls back to the folder's `claude.ai/code?environment=…`
  link, which always opens in the browser.
  The session also appears under its project name in the session list at
  claude.ai/code and in the app.
- **claude session** (expandable) shows the session's terminal screen and the
  command to attach to it over SSH, e.g.
  `tmux -L routing-claude-default attach -t FITAUTO` (detach with
  `Ctrl-b d`).
- **Waiting for an answer** means Claude asked a question in its terminal
  before connecting. The usual one is the safety check for a folder Claude
  has not been run in before (every newly created or cloned project):
  `Trust <folder>? [y/N]`. Read it in the box; a **Trust this folder** button
  appears on the card and, after you confirm, answers yes for you. The panel
  only ever answers that one question, and only when it is about the
  project's own `appPath` — anything else is answered by attaching over SSH.
  It is asked once per folder. Think before trusting a repository you
  cloned: its own Claude settings (hooks, MCP servers) apply to sessions
  there.
- **End session** stops it. Sessions also end when the host reboots; they are
  not restarted automatically.
- Sessions survive a panel restart or redeploy: they run in tmux on a
  dedicated tmux server that the panel starts as its own transient systemd
  unit (`routing-claude-<instance>`) via `sudo -n systemd-run`. This needs
  the same passwordless `systemd-run` rule as **Restart panel** above.
- At most `CLAUDE_MAX_SESSIONS` (default 4) run at once. By default Claude
  asks you to approve actions in the app; set `CLAUDE_PERMISSION_MODE`
  (e.g. `acceptEdits`) to change that for every session. Both, and
  `CLAUDE_BIN` if `claude` is not at `~/.local/bin/claude`, are read from
  the environment by `install-panel.sh`/`redeploy.sh` and kept in the unit,
  e.g. `CLAUDE_MAX_SESSIONS=2 scripts/redeploy.sh`.

Needs `tmux` and a logged-in `claude` CLI for the panel's user.

### New project

**New project** (header button) creates a project without shelling in. Give
it a name and, optionally, a git URL:

- **No URL, new name** — makes `~/projects/<name>` and runs `git init`.
- **No URL, folder already exists** — registers that folder as it is.
- **Git URL** — runs `git clone` into `~/projects/<name>` (the name defaults
  to the repository's). `https://`, `ssh://` and `git@host:owner/repo.git`
  URLs are accepted. Private repositories work when the panel user's SSH key
  can read them; there is nowhere to type a password or passphrase.

The project is then added to `routes.json` with its `appPath` and no routes,
so its card (and Claude session button) appears straight away. Add `paths`
and a `launchScript` to `routes.json` later and press **Apply routes** when
it has something to serve. Output appears under "new project"; if a clone
fails, the half-made folder is removed. Set `PROJECTS_ROOT` to use a folder
other than `~/projects`.

## Environment variables

Projects the panel starts get their environment from three layers, each
overriding the one above it:

1. the panel's own environment (what the systemd unit inherits),
2. a **`.env` file**, if the project points at one,
3. the **inline variables** typed into the panel's environment table.

So a key you type in the panel wins over the same key in the `.env` file,
which wins over one inherited from the panel's own environment. All of it is
applied on the next **Start** / **Update & Restart** — never to a process
that is already running.

### Pointing at a `.env` file

Two places, whichever suits you:

- **`routes.json`** — an `"envFile"` key on the project (versioned with the
  rest of your config):

  ```json
  {
      "projectName": "Flowlet",
      "launchScript": "cd $HOME/projects/Flowlet && ./scripts/launch.sh web",
      "appPath": "$HOME/projects/Flowlet",
      "envFile": "$HOME/projects/Flowlet/.env",
      "paths": [{ "path": "/*", "port": 3000 }]
  }
  ```

- **The panel** — the "env file" box at the top of a project's
  **environment** section. It is stored in
  `~/.local/state/routing-panel/env-files.json` and **overrides** the
  `routes.json` value (which shows as the box's placeholder), so you can
  repoint a project without editing config. Clear the box to fall back to
  `routes.json`.

`$HOME`, other `$VARS` and `~` expand. A relative path is resolved against
the project's `appPath` (so `.env` usually just works), or against this repo
if the project has no `appPath`.

Under the box the panel reports what it found — `12 variables from
/home/you/projects/app/.env` — or the read error if the path is wrong; hover
it to see the key names. The panel deliberately shows **names only, never
values**: its API can be exposed via Funnel, and returning file contents
would turn it into a way to read any file on the host. The same summary is
written to the launch log every time the project starts.

Supported file syntax — one variable per line:

```sh
# comments and blank lines are skipped
FOO=bar
export ALSO_FINE=yes           # an `export` prefix is allowed
QUOTED="escapes \n work"       # double quotes: \n \t \r \\ \" \'
LITERAL='no escapes here'      # single quotes are literal
PLAIN=value                    # a trailing comment like this is stripped
URL=https://example.com/a#b    # ...but only when preceded by whitespace
```

Multi-line values are not supported; a line that isn't `KEY=value` is
ignored, and the panel says how many were (with a ⚠ on the section header).

### Inline variables

Add/edit/remove rows in the panel's environment table and hit **Save**. They
are stored per project in `~/.local/state/routing-panel/env.json` (chmod
`600` — it may hold secrets) and so survive `routes.json` regeneration. Use
these for one-off tweaks on top of a `.env` file; keep the bulk of your
secrets in the file.

## Multiple instances on one host

You can run two (or more) fully independent instances on the same machine —
e.g. one per Linux user, each with its own projects, panel path, and Caddy.
One knob, `INSTANCE_NAME`, names everything; ports stay explicit. **Every
instance needs a unique `INSTANCE_NAME`, `PROXY_PORT`, `PANEL_PORT`, and
`ADMIN_PORT`.**

| Env | Default | Purpose |
|-----|---------|---------|
| `INSTANCE_NAME` | `default` | names the panel + caddy systemd units; default panel path |
| `PANEL_PATH` | `/default` (default instance) or `/$INSTANCE_NAME/default` (named) | URL prefix the panel is served at |
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
INSTANCE_NAME=devb \
PROXY_PORT=8081 PANEL_PORT=8091 ADMIN_PORT=2020 \
./init.sh
```

That user's panel is then at `:8091/devb/default` directly and
`:8081/devb/default` through their Caddy — named instances nest their panel
under `/<name>/default` by default (override with `PANEL_PATH`). The rest of
the instance's routes are whatever its own `routes.json` declares; they need
**not** start with `/<name>` (that user owns their own URL space on their own
port). Because a named instance's Caddy reloads via its admin API, the
panel's **Apply routes** button needs no sudo (the passwordless-sudo note
below applies only to the default instance).

### Exposing several instances behind one funnel

A tailnet node has a single serve/funnel config, so `funnel-on.sh` points the
public hostname at exactly one `PROXY_PORT` — by default the **default**
instance's Caddy on `:8080`. To reach a named instance through that same
public hostname, give the funneled Caddy a passthrough route to it. Add an
entry to the **default** instance's `routes.json` and re-apply:

```jsonc
// in the default instance's routes.json
{ "projectName": "devb", "paths": [ { "path": "/devb/*", "port": 8081 } ] }
```

Now `https://<host>.ts.net/devb/default` → default Caddy (`:8080`) → devb's
Caddy (`:8081`) → devb's panel, and any `/devb/...` route devb declares rides
along. This is why named panels nest under `/<name>/default`: the prefix is
what the front Caddy keys on, and Caddy preserves it end-to-end. A devb route
that is *not* under `/devb` stays private to `:8081` (tailnet/localhost only),
which is the point — only what devb namespaces under `/devb` becomes public.

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
- `python3` (stdlib only, plus `python3-cryptography` for passkey sign-in)
- `git` for **Update & Restart** and **New project**
- `tmux` and the `claude` CLI (logged in) for **Claude session** — optional

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
