#!/usr/bin/env python3
"""Control panel for the projects in routes.json, served at /default
(configurable via $PANEL_PATH so several instances can share a host).

Shows, per project, whether each of its ports is listening, and offers
start/stop for projects that define a "launchScript":

  start — runs the script via `bash -lc`, detached in its own process
          group; output is captured to ~/.local/state/routing-panel/logs/
  stop  — SIGTERMs every process group found listening on the project's
          ports (plus the group we launched, if any), SIGKILL after 5s.

Projects marked `"launchOnStart": true` in routes.json are started
automatically when the panel itself starts (i.e. on boot, since the panel
runs as an enabled systemd service, and on a service restart), unless
they are already listening. The panel's per-project switch writes the
flag straight back to routes.json, so it survives a reboot.

Processes are found by matching listening-socket inodes from
/proc/net/tcp[6] against /proc/<pid>/fd, which only works for
same-user processes — fine here, since the panel and the dev servers
run as the same user.

API (the /default prefix is optional when hitting the port directly):
  GET  /default/                          dashboard (HTML)
  GET  /default/api/projects              status JSON
  GET  /default/api/projects/<name>/log   captured launch output (tail;
                                          ?full=1 for the full log)
  GET  /default/api/projects/<name>/env   per-project env: inline overrides
                                          {KEY: value} plus the .env file path
                                          and the keys it defines (never their
                                          values)
  POST /default/api/projects/<name>/env   replace the project's env overrides
                                          and/or its .env file path (body:
                                          {"env": {KEY: value, ...},
                                           "envFile": "path"});
                                          applied on the next start/restart
  POST /default/api/projects/<name>/autostart  set the project's
                                          "launchOnStart" flag in routes.json
                                          (body: {"launchOnStart": true|false})
  POST /default/api/projects/<name>/start
  POST /default/api/projects/<name>/stop
  POST /default/api/projects/<name>/update  stop, git pull --rebase in
                                            "appPath", then start again
  GET  /default/api/apply                 apply status + output (tail)
  POST /default/api/apply                 run scripts/apply.sh (regenerate
                                          the Caddyfile from routes.json
                                          and reload Caddy)
  GET  /default/api/redeploy              redeploy status + output (tail)
  POST /default/api/redeploy             run scripts/redeploy.sh detached
                                          (apply routes, then reinstall +
                                          restart this panel service)
  POST /default/api/projects              create a project (body: {"name":
                                          "x", "gitUrl": "…"}): a new folder
                                          under $PROJECTS_ROOT with `git init`,
                                          a `git clone` of gitUrl, or — no
                                          gitUrl and the folder exists — that
                                          folder as is; then add it to
                                          routes.json with no routes
  GET  /default/api/create                create status + output (tail)
  GET  /default/api/projects/<name>/claude  Claude session state, its screen,
                                          the claude.ai link and the tmux
                                          attach command
  POST /default/api/projects/<name>/claude  start or stop `claude
                                          remote-control` in the project's
                                          "appPath" (body: {"action":
                                          "start"|"stop"}); {"action": "trust"}
                                          answers yes when the session is
                                          asking whether to trust that folder

Creating projects and starting Claude sessions are refused (403) unless
PANEL_TOKEN is set.

Sign-in (only when PANEL_TOKEN is set; see panel_auth.py). Without a session
GET /default/ serves a sign-in page and the API answers 401. A browser signs
in with a passkey or by pasting the token and gets a session cookie; its
POSTs must also carry `X-Panel-CSRF: 1`. A script may instead send the token
as an X-Panel-Token header on every request.
  GET  /default/api/auth/state?origin=…   signed in? passkeys usable here?
  POST /default/api/auth/login            {"token": "…"} -> session cookie
  POST /default/api/auth/passkey/options  {"origin": "…"} -> sign-in challenge
  POST /default/api/auth/passkey/verify   the passkey's answer -> cookie
  GET  /default/api/auth/passkeys         list passkeys          (signed in)
  POST /default/api/auth/passkeys/options   start adding one     (signed in)
  POST /default/api/auth/passkeys/register  finish adding one    (signed in)
  POST /default/api/auth/passkeys/<id>/delete                    (signed in)
  POST /default/api/auth/logout           {"everywhere": true} ends every
                                          session, not just this one

Env:
  PANEL_PORT     listen port (default 8090)
  PANEL_PATH     URL prefix the panel is served under (default /default)
  PANEL_BIND     bind address (default 127.0.0.1 — Caddy proxies to us)
  PANEL_TOKEN    if set, the panel requires sign-in: a passkey, or this
                 token (pasted on the sign-in page, or sent by scripts as an
                 X-Panel-Token header). Set this if the proxy is exposed via
                 Funnel: otherwise anyone on the internet can start/stop your
                 projects.
  PANEL_SESSION_DAYS  how long a browser stays signed in (default 30)
  ROUTES_CONFIG  path to routes.json (default: repo root)
  PANEL_AUTOSTART_DELAY  seconds to wait before launching the
                 "launchOnStart" projects (default 3; 0 disables the wait)
  PROJECTS_ROOT  where "New project" creates/clones folders (default
                 ~/projects)
  CLAUDE_BIN     the claude executable (default: on PATH, else
                 ~/.local/bin/claude)
  CLAUDE_PERMISSION_MODE  --permission-mode for Claude sessions (default:
                 unset — Claude asks for approval in the app)
  CLAUDE_MAX_SESSIONS  how many Claude sessions may run at once (default 4)
"""

import hmac
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

# panel_auth.py lives next to this file; make it importable however we are run
sys.path.insert(0, str(Path(__file__).resolve().parent))
import panel_auth  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
CONFIG = Path(os.environ.get("ROUTES_CONFIG", str(ROOT / "routes.json")))
PORT = int(os.environ.get("PANEL_PORT", "8090"))
BIND = os.environ.get("PANEL_BIND", "127.0.0.1")
# URL prefix the panel is served under. Configurable so multiple instances
# can run on one host. Normalized: single leading '/', no trailing '/'.
PANEL_PATH = "/" + os.environ.get("PANEL_PATH", "/default").strip().strip("/")
TOKEN = os.environ.get("PANEL_TOKEN", "")
STATE_DIR = Path(os.environ.get("XDG_STATE_HOME",
                                str(Path.home() / ".local/state"))) / "routing-panel"
LOG_DIR = STATE_DIR / "logs"
PGID_FILE = STATE_DIR / "pgids.json"
ENV_FILE = STATE_DIR / "env.json"
ENV_FILES_FILE = STATE_DIR / "env-files.json"
ENV_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*$")
# One `KEY=value` (or `export KEY=value`) per line in a .env file.
ENV_LINE_RE = re.compile(r"(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)")
ENV_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "\\": "\\", '"': '"', "'": "'"}
APPLY_SCRIPT = ROOT / "scripts" / "apply.sh"
APPLY_LOG = STATE_DIR / "apply.log"
# Grace period before the "launchOnStart" projects are launched, so a boot
# has a moment to settle (network, mounts, other services) first.
AUTOSTART_DELAY = float(os.environ.get("PANEL_AUTOSTART_DELAY", "3"))

lock = threading.Lock()
pgids = {}  # projectName -> pgid of the process group we launched
updating = set()  # projectNames currently mid update-and-restart
applying = False  # scripts/apply.sh currently running (global, not per-project)
creating = False  # a "New project" create/clone currently running (global)


def expand(path):
    """Expand $VARS and ~ in a config path (launchScript-style)."""
    return os.path.expanduser(os.path.expandvars(path)) if path else path


class PanelError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def load_projects():
    projects = json.loads(CONFIG.read_text())
    if not isinstance(projects, list):
        raise ValueError("routes.json must be a JSON array of projects")
    return projects


def find_project(name):
    for project in load_projects():
        if project.get("projectName") == name:
            return project
    raise PanelError(404, f"unknown project: {name}")


def set_project_launch_on_start(name, enabled):
    """Write a project's "launchOnStart" flag back into routes.json.

    Persisting it in the config (rather than in the panel's state dir) is the
    point: routes.json is what a fresh panel — after a reboot or a service
    restart — reads to decide what to launch. The file is rewritten whole
    (json.dumps, 2-space indent) via a temp file + rename, so a crash mid-write
    can't leave a truncated config behind. Setting the flag off drops the key
    rather than writing `false`, keeping the config tidy.
    """
    if not isinstance(enabled, bool):
        raise PanelError(400, "\"launchOnStart\" must be true or false")

    def mutate(projects):
        for project in projects:
            if isinstance(project, dict) and project.get("projectName") == name:
                break
        else:
            raise PanelError(404, f"unknown project: {name}")
        if enabled:
            if not project.get("launchScript"):
                raise PanelError(400, f"{name} has no launchScript configured")
            project["launchOnStart"] = True
        else:
            project.pop("launchOnStart", None)

    _update_config(mutate)
    return enabled


def _update_config(mutate):
    """Read routes.json, let mutate(projects) edit the list in place, write it.

    The whole read-modify-write runs under the lock. The file is rewritten
    whole via a temp file + rename, so a crash mid-write can't leave a
    truncated config behind. If mutate raises, nothing is written.
    """
    with lock:
        try:
            projects = json.loads(CONFIG.read_text())
        except OSError as e:
            raise PanelError(500, f"cannot read {CONFIG}: {e.strerror or e}")
        except ValueError as e:
            raise PanelError(500, f"invalid JSON in {CONFIG}: {e}")
        if not isinstance(projects, list):
            raise PanelError(500, "routes.json must be a JSON array of projects")
        mutate(projects)
        tmp = CONFIG.with_name(CONFIG.name + ".tmp")
        try:
            tmp.write_text(json.dumps(projects, indent=2) + "\n")
            os.replace(tmp, CONFIG)
        except OSError as e:
            tmp.unlink(missing_ok=True)
            raise PanelError(500, f"cannot write {CONFIG}: {e.strerror or e}")


def project_ports(project):
    ports = []
    for route in project.get("paths", []):
        if route.get("port") not in ports:
            ports.append(route["port"])
    return ports


def slug(name):
    return re.sub(r"[^A-Za-z0-9._-]+", "-", name)


def listen_inodes():
    """Map of port -> socket inodes for every LISTEN tcp/tcp6 socket."""
    by_port = {}
    for proc_file in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            lines = Path(proc_file).read_text().splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            fields = line.split()
            if len(fields) < 10 or fields[3] != "0A":  # 0A = LISTEN
                continue
            port = int(fields[1].rsplit(":", 1)[1], 16)
            by_port.setdefault(port, set()).add(fields[9])
    return by_port


def pids_for_inodes(inodes):
    """Same-user pids holding any of the given socket inodes."""
    targets = {f"socket:[{inode}]" for inode in inodes}
    pids = set()
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        fd_dir = f"/proc/{entry}/fd"
        try:
            fds = os.listdir(fd_dir)
        except OSError:
            continue
        for fd in fds:
            try:
                if os.readlink(f"{fd_dir}/{fd}") in targets:
                    pids.add(int(entry))
                    break
            except OSError:
                continue
    return pids


def group_alive(pgid):
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def load_pgids():
    global pgids
    try:
        pgids = {k: v for k, v in json.loads(PGID_FILE.read_text()).items()
                 if group_alive(v)}
    except (OSError, ValueError):
        pgids = {}


def save_pgids():
    PGID_FILE.write_text(json.dumps(pgids))


def load_env_overrides():
    """Map of projectName -> {KEY: value} of user-set env overrides."""
    try:
        data = json.loads(ENV_FILE.read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return data


def project_env_overrides(name):
    env = load_env_overrides().get(name, {})
    return env if isinstance(env, dict) else {}


def set_project_env_overrides(name, env):
    """Validate and persist the full env override table for one project."""
    if not isinstance(env, dict):
        raise PanelError(400, "\"env\" must be an object of KEY: value pairs")
    clean = {}
    for key, value in env.items():
        if not isinstance(key, str) or not ENV_NAME_RE.fullmatch(key):
            raise PanelError(400, f"invalid env var name: {key!r} (must match "
                                  "[A-Za-z_][A-Za-z0-9_]*)")
        if value is None:
            continue  # treat null as "remove"
        if not isinstance(value, (str, int, float, bool)):
            raise PanelError(400, f"value for {key} must be a string or number")
        clean[key] = str(value)
    with lock:
        data = load_env_overrides()
        if clean:
            data[name] = clean
        else:
            data.pop(name, None)
        ENV_FILE.write_text(json.dumps(data, indent=2))
        try:
            os.chmod(ENV_FILE, 0o600)  # may hold secrets
        except OSError:
            pass
    return clean


# --- .env files -------------------------------------------------------------
# A project can point at a .env file whose KEY=value lines are loaded into the
# process it launches. The path comes from routes.json ("envFile") or, when the
# user sets one in the panel, from env-files.json (which wins). Layering when a
# key appears in several places:  panel's own env < .env file < inline rows.


def load_env_file_paths():
    """Map of projectName -> raw (unexpanded) .env path set from the panel."""
    try:
        data = json.loads(ENV_FILES_FILE.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def set_project_env_file(name, raw):
    """Persist the panel-set .env path for one project ("" clears it)."""
    if raw is None:
        raw = ""
    if not isinstance(raw, str):
        raise PanelError(400, "\"envFile\" must be a string path")
    raw = raw.strip()
    if len(raw) > 4096:
        raise PanelError(400, "\"envFile\" path is too long")
    with lock:
        data = load_env_file_paths()
        if raw:
            data[name] = raw
        else:
            data.pop(name, None)
        ENV_FILES_FILE.write_text(json.dumps(data, indent=2))
    return raw


def project_env_file(project):
    """(raw path, source) for a project — panel setting beats routes.json."""
    override = load_env_file_paths().get(project.get("projectName", ""))
    if isinstance(override, str) and override.strip():
        return override.strip(), "panel"
    configured = project.get("envFile")
    if isinstance(configured, str) and configured.strip():
        return configured.strip(), "routes.json"
    return "", ""


def resolve_env_file(project, raw):
    """Expand a .env path; relative paths hang off appPath (else the repo)."""
    path = Path(expand(raw))
    if not path.is_absolute():
        base = expand(project.get("appPath") or "") or str(ROOT)
        path = Path(base) / path
    return Path(os.path.normpath(path))  # tidy for display; no symlink resolve


def unquote_env_value(value):
    """Strip quoting from a .env value, dotenv-style."""
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        body = value[1:-1]
        if value[0] == "'":
            return body  # single quotes are literal
        return re.sub(r"\\(.)",
                      lambda m: ENV_ESCAPES.get(m.group(1), m.group(0)), body)
    # unquoted: drop a trailing ` # comment`, as dotenv does
    return re.split(r"\s+#", value, maxsplit=1)[0].strip()


def parse_env_file(path):
    """Parse a .env file into ({KEY: value}, [problems]).

    One KEY=value per line; `export ` prefixes, blank lines, `#` comments and
    quoted values are handled. Multi-line values are not supported.
    """
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return {}, [f"cannot read {path}: {e.strerror or e}"]
    variables, problems = {}, []
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = ENV_LINE_RE.fullmatch(line)
        if not m:
            problems.append(f"line {lineno}: ignored (not KEY=value)")
            continue
        variables[m.group(1)] = unquote_env_value(m.group(2).strip())
    return variables, problems


def project_env(project):
    """Everything about a project's environment, ready for start/report."""
    name = project.get("projectName", "")
    raw, source = project_env_file(project)
    path, file_vars, problems = "", {}, []
    if raw:
        path = str(resolve_env_file(project, raw))
        file_vars, problems = parse_env_file(path)
    overrides = project_env_overrides(name)
    return {
        "raw": raw,            # what the user typed / routes.json holds
        "source": source,      # "panel", "routes.json" or ""
        "path": path,          # expanded, absolute
        "fileVars": file_vars,
        "problems": problems,
        "overrides": overrides,
        "merged": {**file_vars, **overrides},  # inline rows win
    }


def env_report(project):
    """The /env API view: paths and key *names* only — never file values.

    Values from the .env file are deliberately withheld so the panel can't be
    used to read arbitrary files off the host (its API may be exposed).
    """
    env = project_env(project)
    return {
        "env": env["overrides"],
        "envFile": load_env_file_paths().get(project.get("projectName", ""), ""),
        "configEnvFile": project.get("envFile") or "",
        "envFileSource": env["source"],
        "envFilePath": env["path"],
        "fileKeys": sorted(env["fileVars"]),
        "fileProblems": env["problems"],
    }


def project_status():
    by_port = listen_inodes()
    sessions = claude_sessions()
    out = []
    for project in load_projects():
        name = project.get("projectName", "?")
        ports = [{"path": r.get("path"), "port": r.get("port"),
                  "listening": r.get("port") in by_port}
                 for r in project.get("paths", [])]
        uniq = {p["port"]: p["listening"] for p in ports}
        up = sum(1 for listening in uniq.values() if listening)
        state = ("running" if uniq and up == len(uniq)
                 else "stopped" if up == 0 else "partial")
        with lock:
            managed = name in pgids and group_alive(pgids[name])
            is_updating = name in updating
        env = project_env(project)
        out.append({
            "projectName": name,
            "canLaunch": bool(project.get("launchScript")),
            "canUpdate": bool(project.get("launchScript") and project.get("appPath")),
            "launchOnStart": bool(project.get("launchOnStart")),
            "managed": managed,
            "updating": is_updating,
            "ports": ports,
            "state": state,
            "envCount": len(env["merged"]),
            "envFile": env["path"],
            "envFileCount": len(env["fileVars"]),
            "envFileProblem": env["problems"][0] if env["problems"] else "",
            "claude": claude_summary(project, sessions),
        })
    return out


def start_project(project):
    name = project["projectName"]
    script = project.get("launchScript")
    if not script:
        raise PanelError(400, f"{name} has no launchScript configured")

    by_port = listen_inodes()
    ports = project_ports(project)
    if ports and all(port in by_port for port in ports):
        raise PanelError(409, f"{name} is already running")
    if not ports:  # no routes to probe: go by the process group we launched
        with lock:
            alive = name in pgids and group_alive(pgids[name])
        if alive:
            raise PanelError(409, f"{name} is already running")

    env = project_env(project)
    proc_env = {**os.environ, **env["merged"]} if env["merged"] else None

    log_file = LOG_DIR / f"{slug(name)}.log"
    with open(log_file, "ab") as log:
        log.write(f"\n--- start {time.strftime('%F %T')}: {script}\n".encode())
        if env["path"]:
            log.write(f"--- env file ({env['source']}): {env['path']} — "
                      f"{len(env['fileVars'])} variable(s)\n".encode())
            for problem in env["problems"]:
                log.write(f"--- env file: {problem}\n".encode())
        if env["overrides"]:
            log.write(
                f"--- env overrides: {', '.join(sorted(env['overrides']))}\n".encode())
        proc = subprocess.Popen(
            ["bash", "-lc", script],
            cwd=str(Path.home()),
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,  # own process group, survives the panel
            env=proc_env,  # None inherits the panel's env unchanged
        )
    with lock:
        pgids[name] = proc.pid
        save_pgids()
    threading.Thread(target=proc.wait, daemon=True).start()  # reap on exit
    return {"ok": True, "pid": proc.pid}


def autostart_projects():
    """Launch every "launchOnStart" project that isn't already listening.

    Called once, in a background thread, when the panel starts — which covers
    both a reboot (the panel is an enabled systemd service) and a plain
    service restart. A project whose ports are already served is left alone,
    so restarting the panel never doubles up on a running project. Failures
    are reported to the journal and to the project's own launch log; they
    never stop the panel from coming up.
    """
    if AUTOSTART_DELAY > 0:
        time.sleep(AUTOSTART_DELAY)
    try:
        projects = load_projects()
    except (OSError, ValueError) as e:
        print(f"autostart: cannot read {CONFIG}: {e}", file=sys.stderr)
        return
    by_port = listen_inodes()
    for project in projects:
        name = project.get("projectName", "?")
        if not project.get("launchOnStart"):
            continue
        if not project.get("launchScript"):
            print(f"autostart: {name}: launchOnStart set but no launchScript",
                  file=sys.stderr)
            continue
        if any(port in by_port for port in project_ports(project)):
            print(f"autostart: {name} already running — skipped", file=sys.stderr)
            continue
        try:
            start_project(project)
            print(f"autostart: started {name}", file=sys.stderr)
        except Exception as e:
            print(f"autostart: {name}: {e}", file=sys.stderr)
            try:
                with open(LOG_DIR / f"{slug(name)}.log", "ab") as log:
                    log.write(f"--- autostart {time.strftime('%F %T')} "
                              f"failed: {e}\n".encode())
            except OSError:
                pass
        by_port = listen_inodes()  # so later projects see the ports just taken


def stop_project(project):
    name = project["projectName"]
    by_port = listen_inodes()
    inodes = set()
    for port in project_ports(project):
        inodes |= by_port.get(port, set())
    pids = pids_for_inodes(inodes)

    groups = set()
    for pid in pids:
        try:
            groups.add(os.getpgid(pid))
        except ProcessLookupError:
            pass
    with lock:
        saved = pgids.pop(name, None)
        save_pgids()
    if saved and group_alive(saved):
        groups.add(saved)
    groups.discard(os.getpgid(0))  # never kill ourselves

    if not groups:
        if inodes:
            raise PanelError(409, f"{name}'s ports are held by a process this "
                                  "panel cannot signal (owned by another user?)")
        return {"ok": True, "detail": "nothing was running"}

    for pgid in groups:
        try:
            os.killpg(pgid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass

    def escalate(targets):
        time.sleep(5)
        for pgid in targets:
            if group_alive(pgid):
                try:
                    os.killpg(pgid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass

    threading.Thread(target=escalate, args=(set(groups),), daemon=True).start()
    return {"ok": True, "signalled": sorted(groups)}


def update_project(project):
    """Stop the project, git pull --rebase in its appPath, then start it.

    Runs in a background thread: stopping escalates to SIGKILL after 5s and
    the pull can block, so we return immediately and stream progress to the
    project's launch log (viewable under "launch log" on the page). Status
    reports the project as `updating` for the duration.
    """
    name = project["projectName"]
    if not project.get("launchScript"):
        raise PanelError(400, f"{name} has no launchScript configured")
    app_path = expand(project.get("appPath", ""))
    if not app_path:
        raise PanelError(400, f"{name} has no appPath configured")
    if not Path(app_path).is_dir():
        raise PanelError(400, f"{name}'s appPath is not a directory: {app_path}")
    with lock:
        if name in updating:
            raise PanelError(409, f"{name} is already updating")
        updating.add(name)
    threading.Thread(target=_run_update, args=(project, app_path),
                     daemon=True).start()
    return {"ok": True, "detail": "update started"}


def _run_update(project, app_path):
    name = project["projectName"]
    log_file = LOG_DIR / f"{slug(name)}.log"
    try:
        with open(log_file, "ab") as log:
            def emit(msg):
                log.write(f"--- update {time.strftime('%F %T')}: {msg}\n".encode())
                log.flush()

            emit("stopping")
            try:
                stop_project(project)
            except PanelError as e:
                emit(f"stop: {e}")

            # wait for the ports to be released (stop may SIGKILL only at 5s)
            ports = project_ports(project)
            for _ in range(120):  # up to ~12s
                if not any(port in listen_inodes() for port in ports):
                    break
                time.sleep(0.1)
            else:
                emit("ports still in use after stop; pulling anyway")

            emit(f"git pull --rebase in {app_path}")
            result = subprocess.run(
                ["git", "pull", "--rebase"],
                cwd=app_path,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
            )
            if result.returncode != 0:
                emit(f"git pull --rebase failed (exit {result.returncode}); "
                     "not restarting")
                return

            emit("starting")
            try:
                start_project(project)
            except PanelError as e:
                emit(f"start: {e}")
    finally:
        with lock:
            updating.discard(name)


# Cap "full log" reads so a runaway log can never flood the browser; we still
# return only the last FULL_LOG_MAX bytes (the tail people actually want).
FULL_LOG_MAX = 2 * 1024 * 1024


def log_tail(name, max_bytes=16384, full=False):
    log_file = LOG_DIR / f"{slug(name)}.log"
    cap = FULL_LOG_MAX if full else max_bytes
    try:
        with open(log_file, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - cap))
            return f.read().decode("utf-8", "replace")
    except OSError:
        return ""


def apply_routes():
    """Run scripts/apply.sh in the background, streaming output to APPLY_LOG.

    apply.sh regenerates the Caddyfile, validates it, deploys it to
    /etc/caddy and reloads Caddy. The deploy + reload use sudo, so this
    only succeeds non-interactively if the panel's user has passwordless
    sudo for those commands (see the README); otherwise the sudo failure
    shows up in the apply output.
    """
    global applying
    with lock:
        if applying:
            raise PanelError(409, "apply is already running")
        applying = True
    threading.Thread(target=_run_apply, daemon=True).start()
    return {"ok": True, "detail": "apply started"}


def _run_apply():
    global applying
    try:
        with open(APPLY_LOG, "wb") as log:  # truncate: keep only the latest run
            log.write(f"--- apply {time.strftime('%F %T')}: "
                      f"{APPLY_SCRIPT} {CONFIG}\n".encode())
            log.flush()
            result = subprocess.run(
                ["bash", str(APPLY_SCRIPT), str(CONFIG)],
                cwd=str(ROOT),
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
            )
            tail = ("done" if result.returncode == 0
                    else f"FAILED (exit {result.returncode})")
            log.write(f"--- apply {tail}\n".encode())
    finally:
        with lock:
            applying = False


def apply_tail(max_bytes=65536):
    try:
        with open(APPLY_LOG, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - max_bytes))
            return f.read().decode("utf-8", "replace")
    except OSError:
        return ""


# --- redeploy ("Restart panel") --------------------------------------------
# A plain subprocess can't redeploy us: redeploy.sh stops and restarts *this*
# panel service, and systemd's cgroup kill would take any child we spawned down
# with it (leaving the redeploy half-done, possibly with the panel stopped). So
# we launch it as a detached transient unit — its own cgroup, via `systemd-run`
# — which survives our restart. It runs as our own user (--uid/--gid) so the
# files it writes keep the right ownership, and its output streams to
# REDEPLOY_LOG, which outlives the restart so the UI can keep tailing it across
# the brief downtime.
REDEPLOY_SCRIPT = ROOT / "scripts" / "redeploy.sh"
REDEPLOY_LOG = STATE_DIR / "redeploy.log"
INSTANCE_NAME = os.environ.get("INSTANCE_NAME", "default")
REDEPLOY_UNIT = "routing-panel-redeploy-" + slug(INSTANCE_NAME)


def redeploy_running():
    """True while the detached redeploy transient unit is still active."""
    r = subprocess.run(["systemctl", "is-active", "--quiet", REDEPLOY_UNIT],
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL)
    return r.returncode == 0


def redeploy_panel():
    """Launch scripts/redeploy.sh detached so it can restart this panel."""
    if not REDEPLOY_SCRIPT.exists():
        raise PanelError(500, f"redeploy script not found: {REDEPLOY_SCRIPT}")
    if redeploy_running():
        raise PanelError(409, "a redeploy is already running")
    # Truncate + stamp the log; the detached unit appends to it (as us).
    REDEPLOY_LOG.write_text(
        f"--- redeploy {time.strftime('%F %T')}: launching {REDEPLOY_SCRIPT}\n")
    redirect = (f"exec {shlex.quote(str(REDEPLOY_SCRIPT))} "
                f">> {shlex.quote(str(REDEPLOY_LOG))} 2>&1")
    cmd = [
        "sudo", "-n", "systemd-run", "--collect", f"--unit={REDEPLOY_UNIT}",
        f"--uid={os.getuid()}", f"--gid={os.getgid()}",
        f"--setenv=HOME={Path.home()}",
        f"--setenv=PATH={os.environ.get('PATH', '')}",
        f"--setenv=INSTANCE_NAME={INSTANCE_NAME}",
        "/bin/bash", "-c", redirect,
    ]
    # systemd-run returns as soon as the unit has started; capture its own
    # messages (e.g. a sudo/permission failure) into the same log.
    with open(REDEPLOY_LOG, "ab") as log:
        proc = subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=log,
                              stderr=subprocess.STDOUT)
    if proc.returncode != 0:
        raise PanelError(500, "failed to launch redeploy — is passwordless sudo "
                              "for systemd-run set up? (see the README); "
                              "details in the redeploy output")
    return {"ok": True, "detail": "redeploy started"}


def redeploy_tail(max_bytes=65536):
    try:
        with open(REDEPLOY_LOG, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - max_bytes))
            return f.read().decode("utf-8", "replace")
    except OSError:
        return ""


def require_token(what):
    """Refuse an action unless the panel has a token configured.

    Creating/cloning projects and starting Claude sessions change what runs on
    this host, so unlike the older endpoints they are never available on an
    unauthenticated panel.
    """
    if not TOKEN:
        raise PanelError(403, f"{what} is disabled until PANEL_TOKEN is set "
                              "(see the README)")


# --- sign-in ----------------------------------------------------------------
# With a PANEL_TOKEN set the whole panel is behind a sign-in page. A browser
# signs in with a passkey (Face ID, Touch ID, Windows Hello, a security key…)
# or, to set up a new device and as the recovery path, by pasting the token;
# either way it gets a session cookie. Scripts can still send the token in an
# X-Panel-Token header on each request. The protocol work is in panel_auth.py.
SESSION_DAYS = float(os.environ.get("PANEL_SESSION_DAYS") or "30")
COOKIE_NAME = "routing_panel_" + re.sub(r"[^A-Za-z0-9]", "_", INSTANCE_NAME)
auth = panel_auth.Auth(STATE_DIR, session_days=SESSION_DAYS,
                       rp_name=f"routing panel {PANEL_PATH}")


def check_token(given):
    """Does `given` equal PANEL_TOKEN? Shared by the sign-in form and the
    X-Panel-Token header, so both feed one throttle: a wrong token costs a
    second, and too many in a row pause token use altogether (AuthError 429)
    while passkeys and existing sessions carry on working.
    """
    auth.token_login_allowed()
    # constant-time: the API may be public, don't leak the token by timing
    if isinstance(given, str) and hmac.compare_digest(
            given.strip().encode(), TOKEN.encode()):
        return True
    time.sleep(1)
    auth.token_login_failed()
    return False


# --- "New project" ----------------------------------------------------------
# Creates a project folder under PROJECTS_ROOT — empty + `git init`, cloned
# from a git URL, or an existing folder taken as is — and registers it in
# routes.json with an "appPath" and no routes. Runs in a background thread
# (a clone can take a while) with its output in CREATE_LOG, like apply.
PROJECTS_ROOT = Path(expand(os.environ.get("PROJECTS_ROOT") or "~/projects"))
CREATE_LOG = STATE_DIR / "create.log"
CLONE_TIMEOUT = 600  # seconds
PROJECT_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
# https://…, ssh://… or scp-style user@host:path — nothing else (no local
# paths, file://, ext:: or other transports), and no characters outside a
# conservative URL set. The URL is passed to git as a single argv entry after
# `--`, never through a shell.
GIT_URL_RE = re.compile(
    r"(?:(?:https|ssh)://[A-Za-z0-9]"
    r"|[A-Za-z0-9][A-Za-z0-9._-]*@[A-Za-z0-9][A-Za-z0-9.-]*:)"
    r"[A-Za-z0-9._~/:@%+-]*")


def _name_from_url(url):
    tail = re.split(r"[/:]", url.rstrip("/"))[-1]
    return tail[:-4] if tail.endswith(".git") else tail


def _config_path(path):
    """Spell a path the way routes.json does: $HOME-relative when possible."""
    try:
        return "$HOME/" + str(path.relative_to(Path.home()))
    except ValueError:
        return str(path)


def create_project(name, git_url):
    global creating
    require_token("creating projects")
    if not isinstance(name, str) or not isinstance(git_url, str):
        raise PanelError(400, "\"name\" and \"gitUrl\" must be strings")
    name, git_url = name.strip(), git_url.strip()
    if git_url and (len(git_url) > 512 or not GIT_URL_RE.fullmatch(git_url)):
        raise PanelError(400, "git URL must look like https://host/path, "
                              "ssh://host/path or git@host:owner/repo.git")
    if not name and git_url:
        name = _name_from_url(git_url)
    if not PROJECT_NAME_RE.fullmatch(name):
        raise PanelError(400, "project name must be 1-64 characters: letters, "
                              "digits, '.', '_' or '-', starting with a letter "
                              "or digit")
    taken = {str(p.get("projectName", "")).lower() for p in load_projects()
             if isinstance(p, dict)}
    if name.lower() in taken:
        raise PanelError(409, f"a project named {name} already exists")
    dest = PROJECTS_ROOT / name
    if dest.exists() and not dest.is_dir():
        raise PanelError(409, f"{dest} exists and is not a folder")
    if git_url and dest.exists():
        raise PanelError(409, f"{dest} already exists — leave the git URL "
                              "empty to register that folder as it is")
    with lock:
        if creating:
            raise PanelError(409, "another project is being created")
        creating = True
    threading.Thread(target=_run_create, args=(name, git_url, dest),
                     daemon=True).start()
    return {"ok": True, "projectName": name, "detail": "create started"}


def _run_create(name, git_url, dest):
    global creating
    try:
        with open(CREATE_LOG, "wb") as log:  # truncate: keep only the latest run
            def emit(msg):
                log.write(f"--- create {time.strftime('%F %T')}: {msg}\n".encode())
                log.flush()

            made = False  # did this run create dest (so may it remove it)?
            try:
                if git_url:
                    PROJECTS_ROOT.mkdir(parents=True, exist_ok=True)
                    made = not dest.exists()
                    emit(f"git clone {git_url} into {dest}")
                    result = subprocess.run(
                        ["git", "-c", "protocol.file.allow=never",
                         "-c", "protocol.ext.allow=never",
                         "clone", "--", git_url, str(dest)],
                        stdin=subprocess.DEVNULL, stdout=log,
                        stderr=subprocess.STDOUT, timeout=CLONE_TIMEOUT,
                        env={**os.environ, "GIT_TERMINAL_PROMPT": "0",
                             "GIT_SSH_COMMAND": "ssh -o BatchMode=yes "
                                                "-o StrictHostKeyChecking=accept-new"})
                    if result.returncode != 0:
                        raise PanelError(
                            500, f"git clone failed (exit {result.returncode})")
                elif dest.is_dir():
                    emit(f"{dest} already exists — registering it as it is")
                else:
                    emit(f"creating {dest}")
                    dest.mkdir(parents=True)
                    made = True
                    result = subprocess.run(
                        ["git", "init"], cwd=str(dest),
                        stdin=subprocess.DEVNULL, stdout=log,
                        stderr=subprocess.STDOUT, timeout=60)
                    if result.returncode != 0:
                        raise PanelError(
                            500, f"git init failed (exit {result.returncode})")

                def add(projects):
                    if any(isinstance(p, dict) and
                           str(p.get("projectName", "")).lower() == name.lower()
                           for p in projects):
                        raise PanelError(409, f"a project named {name} already exists")
                    projects.append({"projectName": name,
                                     "appPath": _config_path(dest),
                                     "paths": []})

                _update_config(add)
                emit(f"done — {name} added to {CONFIG.name} (no routes yet)")
            except (PanelError, OSError, subprocess.TimeoutExpired) as e:
                if made and dest.exists():
                    shutil.rmtree(dest, ignore_errors=True)
                    emit(f"removed {dest}")
                emit(f"FAILED: {e}")
    finally:
        with lock:
            creating = False


def create_tail(max_bytes=65536):
    try:
        with open(CREATE_LOG, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - max_bytes))
            return f.read().decode("utf-8", "replace")
    except OSError:
        return ""


# --- Claude Remote Control sessions ----------------------------------------
# One `claude remote-control` server per project, run in the project's appPath
# so it can be driven from claude.ai/code or the Claude mobile app. Claude runs
# here, on this host; nothing new listens on the network (it dials out).
#
# Each one lives in a tmux session on a dedicated tmux server (socket
# CLAUDE_SOCKET, apart from the user's own tmux): that gives claude the
# terminal it expects, lets the panel read its screen (the claude.ai link, or a
# question it is waiting on), and lets you attach over SSH. Like a redeploy,
# the tmux server must not be our child — systemd would kill it with us on a
# panel restart — so it is started as its own transient unit via `systemd-run`.
CLAUDE_BIN = (os.environ.get("CLAUDE_BIN") or shutil.which("claude")
              or str(Path.home() / ".local" / "bin" / "claude"))
CLAUDE_PERMISSION_MODE = os.environ.get("CLAUDE_PERMISSION_MODE", "").strip()
CLAUDE_MAX_SESSIONS = int(os.environ.get("CLAUDE_MAX_SESSIONS") or "4")
CLAUDE_SOCKET = "routing-claude-" + slug(INSTANCE_NAME)
CLAUDE_UNIT = CLAUDE_SOCKET  # transient unit that holds the tmux server
CLAUDE_URL_RE = re.compile(r"https://claude\.ai/code[^\s\"'<>]*")
CLAUDE_ENV_RE = re.compile(r"[?&]environment=(env_[A-Za-z0-9]+)")
CLAUDE_SESSION_ID_RE = re.compile(r"^(?:session|cse)_([A-Za-z0-9]+)$")
CLAUDE_HOME = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
CLAUDE_PROMPT_RE = re.compile(r"\[y/N\]|\[Y/n\]|\(y/n\)|Choose \[")
# claude's safety check for a folder it has not been run in before
CLAUDE_TRUST_RE = re.compile(r"Trust (/.+)\? \[y/N\]\s*$")
claude_lock = threading.Lock()  # serialises session starts
_claude_cache = (0.0, {})       # (monotonic stamp, claude_sessions() result)


def claude_session_name(name):
    """tmux session name for a project (tmux forbids '.' and ':' in names)."""
    return re.sub(r"[^A-Za-z0-9_-]+", "-", name) or "project"


def _tmux(*args):
    try:
        return subprocess.run(
            ["tmux", "-u", "-L", CLAUDE_SOCKET, *args],
            stdin=subprocess.DEVNULL, capture_output=True, timeout=10,
            encoding="utf-8", errors="replace")
    except (OSError, subprocess.TimeoutExpired) as e:
        return subprocess.CompletedProcess(args, 1, "", str(e))


def _claude_screen(session):
    """What the session's terminal currently shows, as plain text."""
    r = _tmux("capture-pane", "-p", "-J", "-t", f"={session}:")
    if r.returncode != 0:
        return ""
    lines = [line.rstrip() for line in r.stdout.splitlines()]
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines)


def _claude_session_url(env_url):
    """Link to the conversation claude opened for this folder, or "".

    The link on claude's screen (`/code?environment=env_…`) names the folder's
    connection, not a conversation, and the phone apps do not claim it: tapped
    in a phone browser it stays in the browser. `/code/session_…` is claimed
    (claude.ai's apple-app-site-association lists `/code/*`), so it opens the
    app on that conversation. claude records the conversation it created in
    `<claude home>/projects/<folder>/bridge-pointer.json`; rather than rebuild
    its folder naming, take the pointer whose environmentId is the one on
    screen. Anything unexpected -> "", and the caller keeps the screen's link.
    """
    env = CLAUDE_ENV_RE.search(env_url or "")
    if not env:
        return ""
    try:
        pointers = list((CLAUDE_HOME / "projects").glob("*/bridge-pointer.json"))
    except OSError:
        return ""
    for pointer in pointers:
        try:
            data = json.loads(pointer.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict) or data.get("environmentId") != env.group(1):
            continue
        sid = CLAUDE_SESSION_ID_RE.match(str(data.get("sessionId") or ""))
        # claude.ai links use the session_ spelling of the id
        return f"https://claude.ai/code/session_{sid.group(1)}" if sid else ""
    return ""


def claude_sessions(fresh=False):
    """tmux session name -> {"state", "url", "sessionUrl", "screen"} for ours.

    state: "starting"  — claude is up but hasn't connected yet
           "connected" — its screen shows the claude.ai/code link
           "waiting"   — it is asking a question in the terminal (e.g. whether
                         to trust a folder it has not seen before)
           "exited"    — claude quit; its last screen is kept for reading
    Cached for 2s: every dashboard poll asks, and each answer costs a couple
    of tmux calls per session.
    """
    global _claude_cache
    stamp, cached = _claude_cache
    now = time.monotonic()
    if not fresh and now - stamp < 2:
        return cached
    out = {}
    r = _tmux("list-panes", "-a", "-F", "#{session_name}\t#{pane_dead}")
    if r.returncode == 0:  # non-zero: no tmux server, i.e. no sessions
        for line in r.stdout.splitlines():
            session, _, dead = line.partition("\t")
            if not session or session in out:
                continue
            screen = _claude_screen(session)
            shown = [l for l in screen.splitlines() if l.strip()]
            url = CLAUDE_URL_RE.search(screen)
            if dead == "1":
                state = "exited"
            elif shown and CLAUDE_PROMPT_RE.search(shown[-1]):
                state = "waiting"
            elif url:
                state = "connected"
            else:
                state = "starting"
            trust = CLAUDE_TRUST_RE.search(shown[-1]) if state == "waiting" else None
            link = url.group(0) if state == "connected" else ""
            out[session] = {"state": state, "screen": screen, "url": link,
                            # the conversation itself; opens the phone app
                            "sessionUrl": _claude_session_url(link),
                            # the folder claude is asking to trust, if that is
                            # the question on screen
                            "trust": trust.group(1) if trust else ""}
    _claude_cache = (now, out)
    return out


def claude_available(project):
    """(ok, reason): can this project host a Claude session?"""
    if not TOKEN:
        return False, "set PANEL_TOKEN to enable Claude sessions"
    raw = project.get("appPath")
    if not raw:
        return False, "no appPath configured"
    if not Path(expand(raw)).is_dir():
        return False, f"appPath is not a folder: {expand(raw)}"
    if not shutil.which("tmux"):
        return False, "tmux is not installed"
    if not os.access(CLAUDE_BIN, os.X_OK):
        return False, f"claude not found at {CLAUDE_BIN} (set CLAUDE_BIN)"
    return True, ""


def claude_summary(project, sessions):
    """The per-project "claude" block of the status JSON."""
    ok, reason = claude_available(project)
    s = sessions.get(claude_session_name(project.get("projectName", "")), {})
    return {"available": ok, "reason": reason,
            "state": s.get("state", "off"), "url": s.get("url", ""),
            "sessionUrl": s.get("sessionUrl", ""),
            "trustFolder": s.get("trust", "") if _trust_pending(project, s) else ""}


def _trust_pending(project, s):
    """Is the session asking to trust exactly this project's own appPath?"""
    asked, app_path = s.get("trust"), project.get("appPath")
    return bool(asked and app_path and
                os.path.realpath(asked) == os.path.realpath(expand(app_path)))


def claude_trust(project):
    """Answer yes to claude's "Trust <folder>? [y/N]" for this project.

    This is the panel user's own decision, relayed: it is only sent when that
    exact question, about this project's own appPath, is what the session is
    showing right now. No other question is ever answered from here.
    """
    name = project["projectName"]
    require_token("trusting a folder")
    session = claude_session_name(name)
    s = claude_sessions(fresh=True).get(session, {})
    if not _trust_pending(project, s):
        raise PanelError(409, f"{name}'s Claude session is not asking to trust "
                              "its folder")
    r = _tmux("send-keys", "-t", f"={session}:", "y", "Enter")
    claude_sessions(fresh=True)
    if r.returncode != 0:
        raise PanelError(500, "could not answer the session: "
                              + (r.stderr.strip() or f"exit {r.returncode}"))
    return {"ok": True, "trusted": s["trust"]}


def claude_info(project):
    """Status plus the session's screen and how to attach to it over SSH."""
    session = claude_session_name(project["projectName"])
    s = claude_sessions().get(session, {})
    return {**claude_summary(project, {session: s}),
            "screen": s.get("screen", ""),
            "attach": f"tmux -L {CLAUDE_SOCKET} attach -t {session}"}


def claude_start(project):
    name = project["projectName"]
    require_token("starting Claude sessions")
    ok, reason = claude_available(project)
    if not ok:
        raise PanelError(400, f"{name}: {reason}")
    session = claude_session_name(name)
    app_path = expand(project["appPath"])
    with claude_lock:
        sessions = claude_sessions(fresh=True)
        if session in sessions:
            if sessions[session]["state"] != "exited":
                raise PanelError(409, f"{name} already has a Claude session")
            _tmux("kill-session", "-t", "=" + session)  # clear the dead one
            del sessions[session]
        if len(sessions) >= CLAUDE_MAX_SESSIONS:
            raise PanelError(409, f"{len(sessions)} Claude sessions are already "
                                  "running (CLAUDE_MAX_SESSIONS) — end one first")

        claude = [CLAUDE_BIN, "remote-control", "--name", name,
                  # explicit, or the first run in a folder stops to ask
                  "--spawn=same-dir"]
        if CLAUDE_PERMISSION_MODE:
            claude += ["--permission-mode", CLAUDE_PERMISSION_MODE]
        inner = "exec " + " ".join(shlex.quote(a) for a in claude)
        tmux_cmd = [
            "tmux", "-u", "-L", CLAUDE_SOCKET,
            "new-session", "-d", "-s", session, "-x", "200", "-y", "50",
            "-c", app_path, "bash", "-lc", inner,
            # keep the pane after claude exits so a failure stays readable
            ";", "set-option", "-w", "-t", session, "remain-on-exit", "on",
        ]
        if _tmux("list-sessions").returncode != 0:  # no server yet: own unit
            tmux_cmd = [
                "sudo", "-n", "systemd-run", "--collect", "--quiet",
                f"--unit={CLAUDE_UNIT}", "--service-type=forking",
                f"--uid={os.getuid()}", f"--gid={os.getgid()}",
                f"--working-directory={Path.home()}",
                f"--setenv=HOME={Path.home()}",
                f"--setenv=PATH={os.environ.get('PATH') or '/usr/local/bin:/usr/bin:/bin'}",
                "--setenv=LANG=C.UTF-8",
            ] + tmux_cmd
        try:
            r = subprocess.run(tmux_cmd, stdin=subprocess.DEVNULL,
                               capture_output=True, timeout=30,
                               encoding="utf-8", errors="replace")
        except (OSError, subprocess.TimeoutExpired) as e:
            raise PanelError(500, f"could not start a Claude session: {e}")
        finally:
            claude_sessions(fresh=True)  # drop the cached "no session" answer
        if r.returncode != 0:
            detail = (r.stderr or r.stdout).strip().splitlines()
            raise PanelError(500, "could not start a Claude session: "
                             + (detail[-1] if detail else f"exit {r.returncode}"))
    return {"ok": True, "session": session}


def claude_stop(project):
    session = claude_session_name(project["projectName"])
    r = _tmux("kill-session", "-t", "=" + session)
    claude_sessions(fresh=True)
    if r.returncode != 0:
        return {"ok": True, "detail": "no session was running"}
    return {"ok": True}


PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>routing — projects</title>
<style>
:root{--bg:#0f1115;--card:#181b22;--line:#262b35;--text:#e6e9ef;--dim:#8b93a3;
      --green:#3fb950;--red:#f85149;--amber:#d29922;--accent:#4493f8}
*{box-sizing:border-box}
body{margin:0;font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif;
     background:var(--bg);color:var(--text)}
header{display:flex;align-items:baseline;gap:12px;padding:24px 28px 4px}
h1{font-size:20px;margin:0}
#updated{margin-left:auto;color:var(--dim);font-size:12px}
#err{color:var(--red);font-size:13px;padding:0 28px;min-height:20px}
main{display:grid;grid-template-columns:repeat(auto-fill,minmax(340px,1fr));
     gap:16px;padding:8px 28px 40px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;
      padding:16px 18px}
.head{display:flex;align-items:center;gap:10px;margin-bottom:8px}
.head h2{font-size:16px;margin:0}
.badge{margin-left:auto;font-size:12px;padding:2px 10px;border-radius:999px;
       border:1px solid var(--line);color:var(--dim)}
.badge.running{color:var(--green);border-color:var(--green)}
.badge.partial{color:var(--amber);border-color:var(--amber)}
table{width:100%;border-collapse:collapse;font-size:13px;margin:6px 0 12px}
td{padding:3px 0;color:var(--dim)}
td.mono{text-align:right;font-family:ui-monospace,SFMono-Regular,monospace}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;
     background:var(--line);margin-right:8px}
.dot.on{background:var(--green)}
.on-text{color:var(--green)}
.actions{display:flex;gap:8px}
button{font:inherit;font-size:13px;padding:5px 14px;border-radius:6px;
       border:1px solid var(--line);background:#212630;color:var(--text);
       cursor:pointer}
button:hover:enabled{border-color:var(--accent)}
button.stop:hover:enabled{border-color:var(--red);color:var(--red)}
button:disabled{opacity:.45;cursor:default}
.boot{display:flex;align-items:center;gap:8px;margin-top:12px;font-size:12px;
      color:var(--dim);cursor:pointer;width:fit-content}
.boot input{appearance:none;-webkit-appearance:none;margin:0;flex:none;
      width:34px;height:19px;border-radius:999px;background:#212630;
      border:1px solid var(--line);position:relative;cursor:pointer;
      transition:background .15s,border-color .15s}
.boot input::after{content:"";position:absolute;top:2px;left:2px;width:13px;
      height:13px;border-radius:50%;background:var(--dim);
      transition:transform .15s,background .15s}
.boot input:checked{border-color:var(--green);background:rgba(63,185,80,.18)}
.boot input:checked::after{transform:translateX(15px);background:var(--green)}
.boot input:checked+span{color:var(--text)}
.boot:has(input:disabled){cursor:default;opacity:.45}
.boot input:disabled{cursor:default}
details{margin-top:12px;font-size:12px}
#applybox,#redeploybox,#newbox,#secbox{margin:4px 28px 0;padding:0}
#pkhint{color:var(--dim);font-size:12px;margin:8px 0 0}
#pk-name{flex:0 1 240px}
#pklist{max-width:640px;margin:8px 0 0}
#pklist td{padding:3px 12px 3px 0;color:var(--dim);font-size:12px}
#pklist td.pkname{color:var(--text)}
#pklist button{padding:2px 9px;font-size:12px}
#pklist button:hover:enabled,#signout-all:hover:enabled{border-color:var(--red);color:var(--red)}
.newform{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin:8px 0}
.newform input{font:inherit;font-size:12px;flex:1 1 220px;min-width:0;
    font-family:ui-monospace,SFMono-Regular,monospace;padding:5px 8px;
    border-radius:5px;border:1px solid var(--line);background:#0b0d11;
    color:var(--text)}
.newform input:focus{outline:none;border-color:var(--accent)}
.newform .hint{flex:1 1 100%;color:var(--dim);font-size:11px}
.claude{display:flex;flex-wrap:wrap;align-items:center;gap:8px;margin-top:12px}
button.cstop:hover:enabled{border-color:var(--red);color:var(--red)}
button.ctrust{border-color:var(--amber);color:var(--amber)}
.cstate{font-size:12px;color:var(--dim)}
.cstate.connected{color:var(--green)}
.cstate.waiting,.cstate.exited{color:var(--amber)}
.clink{font-size:13px;padding:5px 14px;border-radius:6px;margin-left:auto;
       border:1px solid var(--accent);color:var(--accent);text-decoration:none;
       white-space:nowrap}
.clink:hover{background:var(--accent);color:#0b0d11}
.cattach{margin:8px 0 0;color:var(--dim);font-size:11px;word-break:break-all;
    font-family:ui-monospace,SFMono-Regular,monospace;user-select:all}
.claudebox pre{white-space:pre;max-height:300px}
#redeploy:hover:enabled{border-color:var(--red);color:var(--red)}
summary{color:var(--dim);cursor:pointer;user-select:none}
pre{background:#0b0d11;border:1px solid var(--line);border-radius:6px;
    padding:8px 10px;max-height:240px;overflow:auto;white-space:pre-wrap;
    font-size:11px}
.logbar{display:flex;align-items:center;gap:12px;margin:8px 0 0;
    font-size:11px;color:var(--dim)}
.logbar label{display:flex;align-items:center;gap:5px;cursor:pointer}
.logbar .live{color:var(--green)}
.logbar .lognote{margin-left:auto;font-family:ui-monospace,SFMono-Regular,monospace}
.env{margin-top:8px}
.env table{margin:0 0 8px}
.env td{padding:2px 4px 2px 0;vertical-align:middle}
.env input{font:inherit;font-size:12px;font-family:ui-monospace,SFMono-Regular,monospace;
    width:100%;padding:4px 6px;border-radius:5px;border:1px solid var(--line);
    background:#0b0d11;color:var(--text)}
.env input:focus{outline:none;border-color:var(--accent)}
.env .k{width:42%}
.env .rm{padding:3px 9px;color:var(--dim)}
.env .rm:hover:enabled{border-color:var(--red);color:var(--red)}
.env .foot{display:flex;gap:8px;align-items:center}
.env .savestate{color:var(--dim);font-size:11px}
.env .savestate.saved{color:var(--green)}
.env .note{margin-left:auto;color:var(--dim);font-size:11px}
.env .file{display:flex;gap:8px;align-items:center;margin:0 0 4px}
.env .file .flabel{color:var(--dim);font-size:11px;white-space:nowrap}
.env .efstate{color:var(--dim);font-size:11px;margin:0 0 8px;
    font-family:ui-monospace,SFMono-Regular,monospace;word-break:break-all}
.env .efstate.bad{color:var(--amber)}
</style>
</head>
<body>
<header><h1>Projects</h1>
<button id="apply" title="regenerate the Caddyfile from routes.json and reload Caddy">Apply routes</button>
<button id="redeploy" title="redeploy the stack and restart this panel: apply routes, then reinstall + restart the panel service so it runs the latest code">Restart panel</button>
<button id="newproj" title="create a project folder (empty, or cloned from git) and add it to routes.json">New project</button>
<button id="secbtn" title="passkeys for signing in, and sign out" hidden>Security</button>
<span id="updated"></span></header>
<div id="err"></div>
<details id="secbox" hidden><summary>security</summary>
<div id="pkhint"></div>
<table id="pklist"></table>
<div class="newform">
<input id="pk-name" placeholder="name for this device's passkey" maxlength="60" spellcheck="false">
<button id="pk-add">Add a passkey on this device</button>
<button id="signout">Sign out</button>
<button id="signout-all" title="end every signed-in browser session, including this one">Sign out everywhere</button>
</div></details>
<details id="newbox"><summary>new project</summary>
<div class="newform">
<input id="np-url" placeholder="git URL to clone (optional) — git@github.com:you/repo.git" spellcheck="false" autocapitalize="off">
<input id="np-name" placeholder="project name (its folder under ~/projects)" spellcheck="false" autocapitalize="off">
<button id="np-create">Create</button>
<span class="hint">No URL: makes an empty folder with git init, or registers the folder if it already exists. The project is added to routes.json with no routes.</span>
</div><pre id="createlog">…</pre></details>
<details id="applybox"><summary>apply output</summary><pre id="applylog">…</pre></details>
<details id="redeploybox"><summary>redeploy output</summary><pre id="redeploylog">…</pre></details>
<main id="grid"></main>
<script>
/*AUTH_JS*/
// The token used to live here in localStorage; sign-in is a cookie now.
try { localStorage.removeItem('routing-panel-token'); } catch (e) {}
const busy = new Set();
const logFull = new Set();     // projects showing the full log (vs. the tail)
const logLoading = new Set();  // in-flight log fetches, so polls don't pile up
const cards = {};              // projectName -> its <div class="card">, kept across refreshes
let last = [];

// Every request rides on the session cookie. X-Panel-CSRF marks it as coming
// from this page (another site cannot add it). A 401 means the session ended:
// reload, and the server answers with the sign-in page.
let signedOut = false;

async function api(path, opts = {}) {
  const r = await fetch(path, {...opts,
    headers: {'X-Panel-CSRF': '1', ...(opts.headers || {})}});
  if (r.status === 401 && !signedOut) { signedOut = true; location.reload(); }
  return r;
}

function esc(s) {
  return String(s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
}

function showErr(msg) { document.getElementById('err').textContent = msg; }

async function act(name, what) {
  busy.add(name);
  if (what === 'update') {  // surface progress as it streams
    const det = cards[name] && cards[name].querySelector('.logbox');
    if (det && !det.open) det.open = true;  // toggle handler starts the live tail
  }
  render(last);
  try {
    const r = await api('api/projects/' + encodeURIComponent(name) + '/' + what, {method: 'POST'});
    const d = await r.json().catch(() => ({}));
    showErr(r.ok ? '' : (d.error || (what + ' failed (' + r.status + ')')));
  } catch (e) {
    showErr(String(e));
  }
  // give the process a moment to bind/release its ports before re-polling;
  // for update, the server keeps reporting `updating` until the restart lands
  setTimeout(() => { busy.delete(name); refresh(); },
             what === 'start' || what === 'update' ? 1500 : 800);
}

// --- "start on boot" switch -------------------------------------------------
// Writes the project's launchOnStart flag straight into routes.json, so the
// panel launches it again after a reboot or a service restart. bootSaving
// holds the projects with a save in flight, so the 3s refresh doesn't flip the
// switch back to the stale server value before the write lands.
const bootSaving = new Set();

async function setAutostart(name, card, on) {
  const chk = card.querySelector('.bootchk');
  bootSaving.add(name);
  chk.disabled = true;
  try {
    const r = await api('api/projects/' + encodeURIComponent(name) + '/autostart',
      {method: 'POST', headers: {'Content-Type': 'application/json'},
       body: JSON.stringify({launchOnStart: on})});
    const d = await r.json().catch(() => ({}));
    if (!r.ok) {
      chk.checked = !on;  // the config wasn't written — don't pretend it was
      showErr(d.error || ('could not save start-on-boot (' + r.status + ')'));
    } else {
      chk.checked = !!d.launchOnStart;
      showErr('');
    }
  } catch (e) {
    chk.checked = !on;
    showErr(String(e));
  } finally {
    bootSaving.delete(name);
    chk.disabled = false;
    refresh();
  }
}

// --- Claude sessions ---------------------------------------------------------
// Start/stop `claude remote-control` in the project's folder. The box under
// the buttons shows the session's terminal screen — the claude.ai link once it
// is connected, or the question it is waiting on — and the tmux command to
// attach to it over SSH. It is polled only while open, like the launch log.
const claudeBusy = new Set();
const claudeLoading = new Set();

async function loadClaude(name, card) {
  if (claudeLoading.has(name)) return;
  claudeLoading.add(name);
  try {
    const r = await api('api/projects/' + encodeURIComponent(name) + '/claude');
    if (!r || !r.ok) return;
    const d = await r.json().catch(() => ({}));
    const pre = card.querySelector('.claudebox pre');
    const display = d.state === 'off'
      ? (d.available ? '(no session — press "Claude session" to start one)'
                     : '(unavailable: ' + (d.reason || 'unknown') + ')')
      : (d.screen || '(starting…)');
    if (pre._shown !== display) {  // only rewrite when changed, to keep selection
      pre._shown = display;
      pre.textContent = display;
    }
    card.querySelector('.cattach').textContent =
      d.state === 'off' ? '' : 'over SSH: ' + d.attach;
  } finally {
    claudeLoading.delete(name);
  }
}

async function claudeAct(name, action) {
  const card = cards[name];
  claudeBusy.add(name);
  if (action === 'start') card.querySelector('.claudebox').open = true;
  render(last);
  try {
    const r = await api('api/projects/' + encodeURIComponent(name) + '/claude',
      {method: 'POST', headers: {'Content-Type': 'application/json'},
       body: JSON.stringify({action})});
    const d = await r.json().catch(() => ({}));
    showErr(r.ok ? '' : (d.error || ('claude ' + action + ' failed (' + r.status + ')')));
  } catch (e) {
    showErr(String(e));
  }
  setTimeout(() => {
    claudeBusy.delete(name);
    refresh();
    if (card.querySelector('.claudebox').open) loadClaude(name, card);
  }, 1000);
}

// --- security: passkeys + sign out -------------------------------------------
// Shown only when the panel requires sign-in (it has a PANEL_TOKEN). A passkey
// is tied to the address the panel is opened at and needs https, so the box
// says why when one cannot be added here.
function deviceName() {
  const u = navigator.userAgent;
  return /iPhone/.test(u) ? 'iPhone' : /iPad/.test(u) ? 'iPad'
    : /Mac/.test(u) ? 'Mac' : /Windows/.test(u) ? 'Windows'
    : /Android/.test(u) ? 'Android' : /Linux/.test(u) ? 'Linux' : 'this device';
}

function fmtDay(t) { return t ? new Date(t * 1000).toLocaleDateString() : 'never'; }

async function loadSecurity() {
  const r = await api('api/auth/state?origin=' + encodeURIComponent(location.origin));
  if (!r.ok) return false;
  const s = await r.json();
  document.getElementById('secbtn').hidden = !s.authRequired;
  document.getElementById('secbox').hidden = !s.authRequired;
  if (!s.authRequired) return false;
  const canAdd = s.passkeysSupported && !!window.PublicKeyCredential;
  document.getElementById('pk-add').disabled = !canAdd;
  const nameBox = document.getElementById('pk-name');
  if (!nameBox.value) nameBox.value = deviceName();
  document.getElementById('pkhint').textContent = !canAdd
    ? 'Passkeys cannot be used here: ' +
      (s.passkeysReason || 'this browser does not support them') + '.'
    : s.passkeys
      ? 'Passkeys sign you in at this address. Add one per device family — ' +
        'an iPhone and a Mac share theirs through iCloud Keychain.'
      : 'No passkey for this address yet. Add one to sign in with Face ID, ' +
        'a fingerprint or Windows Hello instead of the token.';
  const lr = await api('api/auth/passkeys');
  if (!lr.ok) return canAdd;
  const table = document.getElementById('pklist');
  table.textContent = '';
  for (const p of (await lr.json()).passkeys) {
    const tr = table.insertRow();
    const name = tr.insertCell(); name.className = 'pkname'; name.textContent = p.name;
    tr.insertCell().textContent = p.origin === location.origin ? '' : p.origin;
    tr.insertCell().textContent = 'added ' + fmtDay(p.created);
    tr.insertCell().textContent = 'last used ' + fmtDay(p.lastUsed);
    const rm = document.createElement('button');
    rm.textContent = 'Remove';
    rm.onclick = () => removePasskey(p.id, p.name);
    tr.insertCell().appendChild(rm);
  }
  return canAdd;
}

async function addPasskey() {
  const btn = document.getElementById('pk-add');
  btn.disabled = true;
  try {
    const o = await api('api/auth/passkeys/options',
      {method: 'POST', headers: {'Content-Type': 'application/json'},
       body: JSON.stringify({origin: location.origin})});
    const pub = await o.json().catch(() => ({}));
    if (!o.ok) { showErr(pub.error || 'could not start adding a passkey'); return; }
    pub.challenge = b64uToBuf(pub.challenge);
    pub.user.id = b64uToBuf(pub.user.id);
    pub.excludeCredentials = (pub.excludeCredentials || [])
      .map(c => ({...c, id: b64uToBuf(c.id)}));
    let cred;
    try { cred = await navigator.credentials.create({publicKey: pub}); }
    catch (e) { showErr('passkey not added: ' + passkeyError(e)); return; }
    const resp = cred.response;
    const key = resp.getPublicKey && resp.getPublicKey();
    if (!key || !resp.getAuthenticatorData) {
      showErr('this browser is too old to add a passkey here');
      return;
    }
    const r = await api('api/auth/passkeys/register',
      {method: 'POST', headers: {'Content-Type': 'application/json'},
       body: JSON.stringify({
         name: document.getElementById('pk-name').value.trim(),
         id: bufToB64u(cred.rawId),
         clientDataJSON: bufToB64u(resp.clientDataJSON),
         authenticatorData: bufToB64u(resp.getAuthenticatorData()),
         publicKey: bufToB64u(key),
         alg: resp.getPublicKeyAlgorithm(),
         transports: resp.getTransports ? resp.getTransports() : []})});
    const d = await r.json().catch(() => ({}));
    showErr(r.ok ? '' : (d.error || ('could not add the passkey (' + r.status + ')')));
  } finally {
    await loadSecurity();
  }
}

async function removePasskey(id, name) {
  if (!confirm('Remove the passkey "' + name + '"?\\n\\nIt will no longer sign ' +
               'in to this panel. The setup token keeps working.')) return;
  const r = await api('api/auth/passkeys/' + encodeURIComponent(id) + '/delete',
                      {method: 'POST'});
  const d = await r.json().catch(() => ({}));
  showErr(r.ok ? '' : (d.error || 'could not remove the passkey'));
  loadSecurity();
}

async function signOut(everywhere) {
  if (everywhere && !confirm('Sign out every browser, including this one?')) return;
  await api('api/auth/logout',
    {method: 'POST', headers: {'Content-Type': 'application/json'},
     body: JSON.stringify({everywhere})});
  location.reload();
}

document.getElementById('secbtn').onclick = () => {
  document.getElementById('secbox').open = true;
};
document.getElementById('secbox').addEventListener('toggle', e => {
  if (e.target.open) loadSecurity();
});
document.getElementById('pk-add').onclick = addPasskey;
document.getElementById('signout').onclick = () => signOut(false);
document.getElementById('signout-all').onclick = () => signOut(true);

// --- "New project" -----------------------------------------------------------
// Creates/clones the folder server-side in the background; like apply, the
// output lives in a log we poll until the run finishes.
let createBusy = false;
let npNameAuto = '';  // the name we last derived from the URL (safe to replace)

function nameFromUrl(u) {
  let s = u.trim();
  while (s.endsWith('/')) s = s.slice(0, -1);
  if (s.endsWith('.git')) s = s.slice(0, -4);
  return s.slice(Math.max(s.lastIndexOf('/'), s.lastIndexOf(':')) + 1);
}

function setCreateBtn() {
  const b = document.getElementById('np-create');
  b.disabled = createBusy;
  b.textContent = createBusy ? 'Creating…' : 'Create';
}

async function showCreate(open) {
  const r = await api('api/create');
  if (!r || !r.ok) return;
  const d = await r.json();
  const pre = document.getElementById('createlog');
  const display = d.log || '(nothing created yet)';
  if (pre._shown !== display) {
    pre._shown = display; pre.textContent = display; pre.scrollTop = pre.scrollHeight;
  }
  const finished = createBusy && !d.creating;
  createBusy = d.creating;
  setCreateBtn();
  if (open) document.getElementById('newbox').open = true;
  if (d.creating) setTimeout(() => showCreate(false), 800);
  else if (finished) refresh();  // show the new project's card right away
}

async function createProject() {
  const url = document.getElementById('np-url');
  const nm = document.getElementById('np-name');
  createBusy = true;
  setCreateBtn();
  try {
    const r = await api('api/projects',
      {method: 'POST', headers: {'Content-Type': 'application/json'},
       body: JSON.stringify({name: nm.value.trim(), gitUrl: url.value.trim()})});
    const d = await r.json().catch(() => ({}));
    showErr(r.ok ? '' : (d.error || ('create failed (' + r.status + ')')));
    if (r.ok) { url.value = ''; nm.value = ''; npNameAuto = ''; }
  } catch (e) {
    showErr(String(e));
  }
  showCreate(true);
}

document.getElementById('newproj').onclick = () => {
  document.getElementById('newbox').open = true;
  document.getElementById('np-url').focus();
};
document.getElementById('np-create').onclick = createProject;
document.getElementById('np-url').oninput = e => {
  const nm = document.getElementById('np-name');
  if (nm.value === npNameAuto) nm.value = npNameAuto = nameFromUrl(e.target.value);
};
document.getElementById('newbox').addEventListener('toggle', e => {
  if (e.target.open) showCreate(false);
});

// --- per-project environment variables -------------------------------------
// envEdit/envFileEdit hold the in-progress rows and .env path so edits survive
// the 3s refresh re-render; envDirty marks projects whose table has unsaved
// changes (don't clobber them by refetching from the server).
const envEdit = {};
const envFileEdit = {};
const envDirty = new Set();

function readEnvRows(card) {
  return [...card.querySelectorAll('.rows tr')].map(tr => ({
    k: tr.querySelector('.ek').value.trim(),
    v: tr.querySelector('.ev').value,
  }));
}

function markEnvDirty(name, card) {
  envDirty.add(name);
  envEdit[name] = readEnvRows(card);
  envFileEdit[name] = card.querySelector('.ef').value;
  const s = card.querySelector('.savestate');
  if (s) { s.textContent = 'unsaved'; s.className = 'savestate'; }
}

// Show where the .env file came from and what it yielded. The panel never
// receives the file's values — only its key names — so this is a summary.
function renderEnvFile(card, d) {
  const input = card.querySelector('.ef');
  const state = card.querySelector('.efstate');
  input.placeholder = d.configEnvFile
    ? d.configEnvFile + '  (from routes.json)'
    : 'path to a .env file (optional)';
  const problems = d.fileProblems || [];
  if (!d.envFilePath) {
    state.className = 'efstate';
    state.textContent = '';
    return;
  }
  const n = (d.fileKeys || []).length;
  const bad = problems.some(p => p.startsWith('cannot read'));
  state.className = 'efstate' + (bad || problems.length ? ' bad' : '');
  state.textContent = (bad ? problems[0]
      : n + ' variable' + (n === 1 ? '' : 's') + ' from ' + d.envFilePath +
        (problems.length ? ' · ' + problems.length + ' line(s) ignored' : ''));
  if ((d.fileKeys || []).length) state.title = d.fileKeys.join(', ');
}

function addEnvRow(card, k, v) {
  const name = card.dataset.name;
  const tr = document.createElement('tr');
  const kc = document.createElement('td'); kc.className = 'k';
  const ki = document.createElement('input');
  ki.className = 'ek'; ki.placeholder = 'KEY'; ki.value = k; kc.appendChild(ki);
  const vc = document.createElement('td');
  const vi = document.createElement('input');
  vi.className = 'ev'; vi.placeholder = 'value'; vi.value = v; vc.appendChild(vi);
  const rc = document.createElement('td');
  const rb = document.createElement('button');
  rb.className = 'rm'; rb.type = 'button'; rb.textContent = '✕'; rb.title = 'remove';
  rc.appendChild(rb);
  tr.append(kc, vc, rc);
  rb.onclick = () => { tr.remove(); markEnvDirty(name, card); };
  ki.oninput = vi.oninput = () => markEnvDirty(name, card);
  card.querySelector('.rows').appendChild(tr);
}

function renderEnvRows(card, rows) {
  card.querySelector('.rows').innerHTML = '';
  for (const r of rows) addEnvRow(card, r.k, r.v);
}

async function loadEnv(name, card) {
  if (envDirty.has(name)) {
    renderEnvRows(card, envEdit[name] || []);
    card.querySelector('.ef').value = envFileEdit[name] || '';
    return;
  }
  const r = await api('api/projects/' + encodeURIComponent(name) + '/env');
  if (!r || !r.ok) return;
  const d = await r.json().catch(() => ({}));
  const rows = Object.entries(d.env || {}).map(([k, v]) => ({k, v}));
  envEdit[name] = rows;
  envFileEdit[name] = d.envFile || '';
  renderEnvRows(card, rows);
  card.querySelector('.ef').value = envFileEdit[name];
  renderEnvFile(card, d);
}

async function saveEnv(name, card) {
  const env = {};
  for (const {k, v} of readEnvRows(card)) { if (k) env[k] = v; }
  const envFile = card.querySelector('.ef').value.trim();
  const state = card.querySelector('.savestate');
  try {
    const r = await api('api/projects/' + encodeURIComponent(name) + '/env',
      {method: 'POST', headers: {'Content-Type': 'application/json'},
       body: JSON.stringify({env, envFile})});
    const d = await r.json().catch(() => ({}));
    if (!r.ok) { showErr(d.error || ('save env failed (' + r.status + ')')); return; }
    envDirty.delete(name);
    envEdit[name] = Object.entries(d.env || {}).map(([k, v]) => ({k, v}));
    envFileEdit[name] = d.envFile || '';
    renderEnvRows(card, envEdit[name]);
    card.querySelector('.ef').value = envFileEdit[name];
    renderEnvFile(card, d);
    if (state) { state.textContent = 'saved'; state.className = 'savestate saved'; }
    showErr('');
    refresh();  // refresh the (n) count in the section header
  } catch (e) {
    showErr(String(e));
  }
}

async function loadLog(name, card) {
  if (logLoading.has(name)) return;  // a fetch is already in flight for this card
  const pre = card.querySelector('.logbox pre');
  if (!pre) return;
  const full = logFull.has(name);
  logLoading.add(name);
  try {
    const r = await api('api/projects/' + encodeURIComponent(name) + '/log' +
                        (full ? '?full=1' : ''));
    if (!r || !r.ok) return;
    const text = await r.text();
    const display = text || '(no output captured yet)';
    // Only touch the DOM when the text actually changed. Rewriting it every
    // poll would drop the user's selection (so they can't copy) and flicker.
    if (pre._shown !== display) {
      // keep the user's place if they scrolled up to read; follow only at bottom
      const atBottom = pre.scrollHeight - pre.scrollTop - pre.clientHeight < 24;
      pre._shown = display;
      pre.textContent = display;
      if (atBottom) pre.scrollTop = pre.scrollHeight;
    }
    const note = card.querySelector('.logbox .lognote');
    if (note) note.textContent =
      (text ? Math.ceil(text.length / 1024) + ' KB' : '0 KB') +
      (full ? ' · full' : ' · tail');
  } finally {
    logLoading.delete(name);
  }
}

function rowHTML(pt) {
  return '<tr><td><span class="dot' + (pt.listening ? ' on' : '') + '"></span>' +
    esc(pt.path) + '</td>' +
    '<td class="mono">:' + pt.port + '</td>' +
    '<td class="mono st">' +
    (pt.listening ? '<span class="on-text">listening</span>' : 'down') +
    '</td></tr>';
}

function setTitle(el, t) { t ? el.setAttribute('title', t) : el.removeAttribute('title'); }

// Build a card's static skeleton once, wiring its event handlers. The dynamic
// bits (status, badge, button states) are filled in later by updateCard().
function makeCard(name) {
  const card = document.createElement('div');
  card.className = 'card';
  card.dataset.name = name;
  card.innerHTML =
    '<div class="head"><h2>' + esc(name) + '</h2>' +
    '<span class="badge"></span></div>' +
    '<table></table>' +
    '<div class="actions">' +
    '<button class="start">Start</button>' +
    '<button class="stop">Stop</button>' +
    '<button class="update">Update &amp; Restart</button>' +
    '</div>' +
    '<label class="boot"><input type="checkbox" class="bootchk">' +
    '<span>Start on boot</span></label>' +
    '<div class="claude">' +
    '<button class="cstart">Claude session</button>' +
    '<button class="cstop">End session</button>' +
    '<button class="ctrust" hidden>Trust this folder</button>' +
    '<span class="cstate"></span>' +
    '<a class="clink" target="_blank" rel="noopener" hidden>Open in Claude ↗</a>' +
    '</div>' +
    '<details class="claudebox"><summary>claude session</summary>' +
    '<div class="cattach"></div><pre>…</pre></details>' +
    '<details class="envbox"><summary></summary><div class="env">' +
    '<div class="file"><span class="flabel">env file</span>' +
    '<input class="ef" placeholder="path to a .env file (optional)" ' +
    'spellcheck="false"></div>' +
    '<div class="efstate"></div>' +
    '<table class="rows"></table>' +
    '<div class="foot">' +
    '<button class="addvar">+ variable</button>' +
    '<button class="savevar">Save</button>' +
    '<span class="savestate"></span>' +
    '<span class="note">applied on next start / restart</span>' +
    '</div></div></details>' +
    '<details class="logbox"><summary>launch log</summary>' +
    '<div class="logbar">' +
    '<label><input type="checkbox" class="logfull"> full log</label>' +
    '<span class="live">● live</span>' +
    '<span class="lognote"></span>' +
    '</div><pre>…</pre></details>';

  card.querySelector('.start').onclick = () => act(name, 'start');
  card.querySelector('.stop').onclick = () => act(name, 'stop');
  card.querySelector('.update').onclick = () => act(name, 'update');
  card.querySelector('.bootchk').onchange = e => setAutostart(name, card, e.target.checked);
  card.querySelector('.cstart').onclick = () => claudeAct(name, 'start');
  card.querySelector('.cstop').onclick = () => claudeAct(name, 'stop');
  card.querySelector('.ctrust').onclick = e => {
    if (confirm('Trust ' + e.target.dataset.folder + '?\\n\\n' +
                'Claude sessions started in this folder can read, edit and ' +
                'execute files, and the folder\\'s own Claude settings (hooks, ' +
                'MCP servers, permission rules) will be used. Only do this for ' +
                'a project you created or trust.')) claudeAct(name, 'trust');
  };
  const cbox = card.querySelector('.claudebox');
  cbox.addEventListener('toggle', () => { if (cbox.open) loadClaude(name, card); });

  const env = card.querySelector('.envbox');
  env.addEventListener('toggle', () => { if (env.open) loadEnv(name, card); });
  card.querySelector('.ef').oninput = () => markEnvDirty(name, card);
  card.querySelector('.addvar').onclick = () => {
    addEnvRow(card, '', ''); markEnvDirty(name, card);
  };
  card.querySelector('.savevar').onclick = () => saveEnv(name, card);

  const det = card.querySelector('.logbox');
  det.addEventListener('toggle', () => { if (det.open) loadLog(name, card); });
  card.querySelector('.logfull').onchange = e => {
    e.target.checked ? logFull.add(name) : logFull.delete(name);
    loadLog(name, card);
  };
  return card;
}

// Update only the parts of a card that change between polls. Rebuilding the
// whole card every few seconds (as we used to) tore down the log <pre> and env
// inputs each tick — that's what made the text flash and impossible to copy.
function updateCard(card, p) {
  const isBusy = busy.has(p.projectName) || p.updating;

  const badge = card.querySelector('.badge');
  badge.className = 'badge ' + p.state;
  badge.textContent = p.updating ? 'updating…' : isBusy ? 'working…'
    : p.ports.length ? p.state : 'no routes';

  const table = card.querySelector('table');
  const sig = p.ports.map(pt => pt.path + '@' + pt.port).join('|');
  if (table.dataset.sig !== sig) {  // ports changed shape: rebuild the rows
    table.dataset.sig = sig;
    table.innerHTML = p.ports.map(rowHTML).join('');
  } else {                          // same ports: update listening state in place
    const trs = table.querySelectorAll('tr');
    p.ports.forEach((pt, i) => {
      trs[i].querySelector('.dot').className = 'dot' + (pt.listening ? ' on' : '');
      trs[i].querySelector('.st').innerHTML =
        pt.listening ? '<span class="on-text">listening</span>' : 'down';
    });
  }

  const startBtn = card.querySelector('.start');
  startBtn.disabled = !p.canLaunch || p.state === 'running' || isBusy;
  setTitle(startBtn, p.canLaunch ? '' : 'no launchScript configured');
  card.querySelector('.stop').disabled = (p.state === 'stopped' && !p.managed) || isBusy;
  const updBtn = card.querySelector('.update');
  updBtn.disabled = !p.canUpdate || isBusy;
  setTitle(updBtn, p.canUpdate ? '' : 'needs launchScript and appPath');

  const boot = card.querySelector('.bootchk');
  if (!bootSaving.has(p.projectName)) {  // don't clobber a save in flight
    boot.checked = !!p.launchOnStart;
    boot.disabled = !p.canLaunch;
  }
  setTitle(boot.parentElement, p.canLaunch
    ? 'launch this project automatically when the panel starts (written to routes.json)'
    : 'no launchScript configured');

  const c = p.claude ||
    {available: false, reason: '', state: 'off', url: '', trustFolder: ''};
  const cBusy = claudeBusy.has(p.projectName);
  const cStart = card.querySelector('.cstart');
  cStart.disabled = !c.available || cBusy || !(c.state === 'off' || c.state === 'exited');
  setTitle(cStart, c.available
    ? 'run claude remote-control in this project, to use from claude.ai/code or the Claude app'
    : c.reason);
  card.querySelector('.cstop').disabled = c.state === 'off' || cBusy;
  const cTrust = card.querySelector('.ctrust');
  cTrust.hidden = !c.trustFolder;
  cTrust.disabled = cBusy;
  cTrust.dataset.folder = c.trustFolder || '';
  const cState = card.querySelector('.cstate');
  cState.className = 'cstate ' + c.state;
  cState.textContent = cBusy ? 'working…' : c.state === 'off' ? ''
    : c.state === 'waiting' ? 'waiting for an answer' : c.state;
  const cLink = card.querySelector('.clink');
  // sessionUrl is the conversation itself: the Claude phone app claims that
  // link, so a tap opens the app there. url (the folder's connection) is the
  // fallback, and only ever opens in the browser.
  const cHref = c.sessionUrl || c.url;
  cLink.hidden = !cHref;
  if (cHref) cLink.href = cHref;
  setTitle(cLink, c.sessionUrl
    ? 'open this conversation: in the Claude app on a phone that has it, otherwise on claude.ai'
    : 'open this folder on claude.ai');

  const envSum = card.querySelector('.envbox > summary');
  envSum.textContent = 'environment' + (p.envCount ? ' (' + p.envCount + ')' : '') +
    (p.envFileProblem ? ' ⚠' : '');
  setTitle(envSum, p.envFileProblem ||
    (p.envFile ? p.envFileCount + ' of them from ' + p.envFile : ''));
}

function render(projects) {
  last = projects;
  const grid = document.getElementById('grid');
  const seen = new Set();
  for (const p of projects) {
    seen.add(p.projectName);
    let card = cards[p.projectName];
    if (!card) { card = cards[p.projectName] = makeCard(p.projectName); grid.appendChild(card); }
    updateCard(card, p);
  }
  for (const name of Object.keys(cards)) {  // drop projects no longer in the config
    if (!seen.has(name)) { cards[name].remove(); delete cards[name]; }
  }
}

async function refresh() {
  // a full re-render steals focus; pause while the user types in an env field
  const active = document.activeElement;
  if (active && active.closest && active.closest('.env')) return;
  try {
    const r = await api('api/projects');
    if (!r.ok) { showErr('status fetch failed (' + r.status + ')'); return; }
    render(await r.json());
    document.getElementById('updated').textContent =
      'updated ' + new Date().toLocaleTimeString();
    showErr('');
  } catch (e) {
    showErr('panel unreachable: ' + e);
  }
}

let applyBusy = false;

function setApplyBtn() {
  const b = document.getElementById('apply');
  b.disabled = applyBusy;
  b.textContent = applyBusy ? 'Applying…' : 'Apply routes';
}

// Pull the latest apply output; keeps polling while a run is in progress.
async function showApply(open) {
  const r = await api('api/apply');
  if (!r || !r.ok) return;
  const d = await r.json();
  const pre = document.getElementById('applylog');
  const display = d.log || '(no apply has run yet)';
  if (pre._shown !== display) {  // only rewrite when changed, to keep selection
    pre._shown = display;
    pre.textContent = display;
    pre.scrollTop = pre.scrollHeight;
  }
  applyBusy = d.applying;
  setApplyBtn();
  if (open) document.getElementById('applybox').open = true;
  if (d.applying) setTimeout(() => showApply(false), 800);
}

async function apply() {
  applyBusy = true;
  setApplyBtn();
  try {
    const r = await api('api/apply', {method: 'POST'});
    const d = await r.json().catch(() => ({}));
    showErr(r.ok ? '' : (d.error || ('apply failed (' + r.status + ')')));
  } catch (e) {
    showErr(String(e));
  }
  showApply(true);  // reveal output and start polling until it finishes
}

document.getElementById('apply').onclick = apply;
document.getElementById('applybox').addEventListener('toggle', e => {
  if (e.target.open) showApply(false);
});

// --- redeploy ("Restart panel") --------------------------------------------
// redeploy.sh stops, reinstalls and restarts this panel, so the dashboard
// briefly disconnects mid-run. The output log lives server-side and survives
// the restart, so we just keep polling through the downtime (fetch errors are
// expected) until the redeploy unit reports it has finished.
let redeployBusy = false;

function setRedeployBtn() {
  const b = document.getElementById('redeploy');
  b.disabled = redeployBusy;
  b.textContent = redeployBusy ? 'Restarting…' : 'Restart panel';
}

async function showRedeploy(open) {
  let d;
  try {
    const r = await api('api/redeploy');
    if (!r || !r.ok) return;
    d = await r.json();
  } catch (e) {
    // panel is unreachable mid-restart — that's expected; keep polling
    if (redeployBusy) setTimeout(() => showRedeploy(false), 1000);
    return;
  }
  const pre = document.getElementById('redeploylog');
  const display = d.log || '(no redeploy has run yet)';
  if (pre._shown !== display) {
    pre._shown = display; pre.textContent = display; pre.scrollTop = pre.scrollHeight;
  }
  redeployBusy = d.redeploying;
  setRedeployBtn();
  if (open) document.getElementById('redeploybox').open = true;
  if (d.redeploying) setTimeout(() => showRedeploy(false), 1000);
}

async function redeploy() {
  if (!confirm('Restart the panel?\\n\\nThis runs redeploy.sh (apply routes, then ' +
               'reinstall + restart the panel service). The dashboard will ' +
               'disconnect for a few seconds while it comes back up.')) return;
  redeployBusy = true;
  setRedeployBtn();
  document.getElementById('redeploybox').open = true;
  try {
    const r = await api('api/redeploy', {method: 'POST'});
    const d = await r.json().catch(() => ({}));
    showErr(r.ok ? '' : (d.error || ('redeploy failed (' + r.status + ')')));
  } catch (e) {
    showErr(String(e));
  }
  showRedeploy(true);  // reveal output and poll until it finishes (across the restart)
}

document.getElementById('redeploy').onclick = redeploy;
document.getElementById('redeploybox').addEventListener('toggle', e => {
  if (e.target.open) showRedeploy(false);
});

// Live tail: refresh any open log between the slower full-grid refreshes,
// updating just its <pre> so the view doesn't flicker or lose scroll.
function pollLogs() {
  for (const name in cards) {
    const det = cards[name].querySelector('.logbox');
    if (det && det.open) loadLog(name, cards[name]);
    const cbox = cards[name].querySelector('.claudebox');
    if (cbox && cbox.open) loadClaude(name, cards[name]);
  }
}

refresh();
showApply(false);     // sync button state in case an apply is already running
showRedeploy(false);  // and in case a redeploy is still finishing after a restart
showCreate(false);    // and in case a project is still being created/cloned
// Right after a token sign-in, point at "add a passkey" if one can be added.
loadSecurity().then(canAdd => {
  let offer = false;
  try {
    offer = sessionStorage.getItem('routing-panel-offer-passkey') === '1';
    sessionStorage.removeItem('routing-panel-offer-passkey');
  } catch (e) {}
  if (offer && canAdd) document.getElementById('secbox').open = true;
});
setInterval(refresh, 3000);
setInterval(pollLogs, 1500);
</script>
</body>
</html>
"""


# Passkey plumbing both pages need: WebAuthn speaks ArrayBuffers, JSON speaks
# base64url. Spliced into each page's <script> where it says /*AUTH_JS*/.
AUTH_JS = """
function b64uToBuf(s) {
  s = s.split('-').join('+').split('_').join('/');
  while (s.length % 4) s += '=';
  const bin = atob(s), a = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) a[i] = bin.charCodeAt(i);
  return a.buffer;
}

function bufToB64u(buf) {
  let s = '';
  for (const b of new Uint8Array(buf)) s += String.fromCharCode(b);
  s = btoa(s).split('+').join('-').split('/').join('_');
  while (s.endsWith('=')) s = s.slice(0, -1);
  return s;
}

function passkeyError(e) {
  if (e && e.name === 'NotAllowedError') return 'it was cancelled or timed out';
  if (e && e.name === 'InvalidStateError')
    return 'this device already has a passkey for this panel';
  return (e && e.message) || String(e);
}
"""

LOGIN_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>routing — sign in</title>
<style>
:root{--bg:#0f1115;--card:#181b22;--line:#262b35;--text:#e6e9ef;--dim:#8b93a3;
      --red:#f85149;--accent:#4493f8}
*{box-sizing:border-box}
body{margin:0;min-height:100vh;display:flex;align-items:center;
     justify-content:center;padding:24px;background:var(--bg);color:var(--text);
     font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}
main{width:100%;max-width:380px;background:var(--card);
     border:1px solid var(--line);border-radius:12px;padding:28px}
h1{font-size:20px;margin:0 0 4px}
p{margin:0 0 18px;color:var(--dim);font-size:13px}
button{font:inherit;font-size:14px;padding:9px 14px;border-radius:7px;
       border:1px solid var(--line);background:#212630;color:var(--text);
       cursor:pointer}
button:hover:enabled{border-color:var(--accent)}
button:disabled{opacity:.5;cursor:default}
#pk{width:100%;border-color:var(--accent);margin-bottom:14px}
details{font-size:13px}
summary{color:var(--dim);cursor:pointer;user-select:none}
form{display:flex;gap:8px;margin:10px 0 8px}
input{font:inherit;font-size:13px;flex:1;min-width:0;padding:8px 10px;
      border-radius:7px;border:1px solid var(--line);background:#0b0d11;
      color:var(--text)}
input:focus{outline:none;border-color:var(--accent)}
.note{font-size:12px;margin:0}
#err{color:var(--red);font-size:13px;min-height:20px;margin-top:12px}
</style>
</head>
<body>
<main>
<h1>routing panel</h1>
<p>Sign in to manage this host's projects.</p>
<button id="pk" hidden>Sign in with a passkey</button>
<details id="tok"><summary>Use the setup token</summary>
<form id="tokform">
<input type="text" autocomplete="username" value="routing-panel" hidden>
<input id="token" type="password" autocomplete="current-password" placeholder="PANEL_TOKEN" spellcheck="false" autocapitalize="off">
<button id="tokbtn">Sign in</button>
</form>
<p class="note" id="toknote"></p>
</details>
<div id="err"></div>
</main>
<script>
/*AUTH_JS*/
const $ = id => document.getElementById(id);
const err = m => { $('err').textContent = m || ''; };

async function post(path, body) {
  const r = await fetch(path, {method: 'POST',
    headers: {'Content-Type': 'application/json', 'X-Panel-CSRF': '1'},
    body: JSON.stringify(body || {})});
  return {ok: r.ok, d: await r.json().catch(() => ({}))};
}

async function init() {
  let s = {};
  try {
    const r = await fetch('api/auth/state?origin=' + encodeURIComponent(location.origin));
    s = await r.json();
  } catch (e) {}
  const usable = !!s.passkeysSupported && !!window.PublicKeyCredential;
  const ready = usable && s.passkeys > 0;
  $('pk').hidden = !ready;
  $('tok').open = !ready;
  $('toknote').textContent = ready
    ? 'The token also works, e.g. on a device that has no passkey yet.'
    : usable
      ? 'No passkey is set up for this address yet. Sign in with the token, ' +
        'then add one under Security.'
      : 'Passkeys cannot be used here' +
        (s.passkeysReason ? ': ' + s.passkeysReason : '') + '.';
}

$('pk').onclick = async () => {
  err('');
  $('pk').disabled = true;
  try {
    const o = await post('api/auth/passkey/options', {origin: location.origin});
    if (!o.ok) { err(o.d.error || 'could not start passkey sign-in'); return; }
    const pub = o.d;
    pub.challenge = b64uToBuf(pub.challenge);
    pub.allowCredentials = pub.allowCredentials.map(c => ({...c, id: b64uToBuf(c.id)}));
    let cred;
    try { cred = await navigator.credentials.get({publicKey: pub}); }
    catch (e) { err('Passkey sign-in did not finish: ' + passkeyError(e)); return; }
    const resp = cred.response;
    const v = await post('api/auth/passkey/verify', {
      id: bufToB64u(cred.rawId),
      clientDataJSON: bufToB64u(resp.clientDataJSON),
      authenticatorData: bufToB64u(resp.authenticatorData),
      signature: bufToB64u(resp.signature)});
    if (!v.ok) { err(v.d.error || 'passkey sign-in failed'); return; }
    location.reload();
  } finally {
    $('pk').disabled = false;
  }
};

$('tokform').onsubmit = async e => {
  e.preventDefault();
  err('');
  $('tokbtn').disabled = true;
  try {
    const v = await post('api/auth/login', {token: $('token').value});
    if (!v.ok) { err(v.d.error || 'sign-in failed'); return; }
    // the dashboard uses this to point at "add a passkey" once
    try { sessionStorage.setItem('routing-panel-offer-passkey', '1'); } catch (e) {}
    location.reload();
  } finally {
    $('tokbtn').disabled = false;
  }
};

init();
</script>
</body>
</html>
"""

PAGE = PAGE.replace("/*AUTH_JS*/", AUTH_JS)
LOGIN_PAGE = LOGIN_PAGE.replace("/*AUTH_JS*/", AUTH_JS)


class Handler(BaseHTTPRequestHandler):
    server_version = "routing-panel"

    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _send(self, code, body, ctype="application/json", headers=()):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "DENY")  # never inside another site
        self.send_header("Referrer-Policy", "same-origin")
        for name, value in headers:
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(data)

    def _path(self):
        path = urlparse(self.path).path
        if path == PANEL_PATH:  # relative URLs in the page need the slash
            self.send_response(308)
            self.send_header("Location", PANEL_PATH + "/")
            self.end_headers()
            return None
        if path.startswith(PANEL_PATH + "/"):
            path = path[len(PANEL_PATH):]
        return path

    def _cookie(self):
        """Our session cookie's value. Parsed by hand: other apps on this
        hostname set cookies too, and a strict parser chokes on some of them."""
        for part in self.headers.get("Cookie", "").split(";"):
            name, _, value = part.strip().partition("=")
            if name == COOKIE_NAME:
                return value
        return ""

    def _auth_via(self):
        """How this request is authorised: "open" (the panel has no token),
        "token" (X-Panel-Token header), "cookie" (signed-in browser) or ""."""
        if not TOKEN:
            return "open"
        given = self.headers.get("X-Panel-Token")
        if given is not None:
            try:
                if check_token(given):
                    return "token"
            except panel_auth.AuthError:
                pass  # token use is paused; a session cookie may still do
        if auth.session_valid(self._cookie()):
            return "cookie"
        return ""

    def _session_cookie(self, value, max_age):
        secure = ("; Secure"
                  if self.headers.get("Origin", "").startswith("https://") else "")
        # Lax, not Strict, so a link or bookmark into the panel arrives signed
        # in; every state-changing request is a POST that must also carry the
        # X-Panel-CSRF header, which another site cannot send.
        return ("Set-Cookie", f"{COOKIE_NAME}={value}; Path={PANEL_PATH}; "
                              f"Max-Age={max_age}; HttpOnly; SameSite=Lax{secure}")

    def _start_session(self, how):
        value, max_age = auth.new_session(how)
        return self._send(200, {"ok": True},
                          headers=[self._session_cookie(value, max_age)])

    def _auth_route(self, path, method, via):
        if method == "GET" and path == "/api/auth/state":
            origin = parse_qs(urlparse(self.path).query).get("origin", [""])[0]
            try:
                origin, _ = panel_auth.normalize_origin(origin)
                reason = ""
            except panel_auth.AuthError as e:  # e.g. opened over plain http
                origin, reason = None, str(e)
            ok, missing = auth.passkeys_available()
            return self._send(200, {
                "authRequired": bool(TOKEN),
                "authenticated": bool(via),
                "passkeysSupported": bool(TOKEN) and ok and origin is not None,
                "passkeysReason": missing or reason,
                "passkeys": auth.passkey_count(origin) if origin else 0,
            })
        if not TOKEN:
            raise PanelError(400, "sign-in is not enabled: the panel has no "
                                  "PANEL_TOKEN")
        body = self._json_body() if method == "POST" else {}
        if not isinstance(body, dict):
            raise PanelError(400, "request body must be a JSON object")
        if method == "POST" and path == "/api/auth/login":
            if not check_token(body.get("token")):
                raise PanelError(401, "that is not this panel's token")
            return self._start_session("token")
        if method == "POST" and path == "/api/auth/passkey/options":
            return self._send(200, auth.login_options(body.get("origin")))
        if method == "POST" and path == "/api/auth/passkey/verify":
            return self._start_session("passkey: " + auth.login_verify(body))

        # everything below manages sign-in, so it needs a signed-in user
        if not via:
            return self._send(401, {"error": "not signed in"})
        if method == "GET" and path == "/api/auth/passkeys":
            return self._send(200, {"passkeys": auth.list_passkeys()})
        if method == "POST" and path == "/api/auth/passkeys/options":
            return self._send(200, auth.register_options(body.get("origin")))
        if method == "POST" and path == "/api/auth/passkeys/register":
            return self._send(200, {"ok": True, "name": auth.register(body)})
        m = re.fullmatch(r"/api/auth/passkeys/([A-Za-z0-9_-]+)/delete", path)
        if method == "POST" and m:
            auth.delete_passkey(m.group(1))
            return self._send(200, {"ok": True})
        if method == "POST" and path == "/api/auth/logout":
            if body.get("everywhere") is True:
                auth.end_all_sessions()
            else:
                auth.end_session(self._cookie())
            return self._send(200, {"ok": True},
                              headers=[self._session_cookie("", 0)])
        self._send(404, {"error": "not found"})

    def _json_body(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        raw = self.rfile.read(length) if length > 0 else b""
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except ValueError:
            raise PanelError(400, "request body must be valid JSON")

    def _dispatch(self, path, method):
        via = self._auth_via()
        try:
            # A browser sends our cookie along with requests that other sites
            # make it issue; only our own page can add this header to them.
            if (method == "POST" and via not in ("open", "token")
                    and self.headers.get("X-Panel-CSRF") != "1"):
                raise PanelError(403, "missing X-Panel-CSRF header")
            if path.startswith("/api/auth/"):
                return self._auth_route(path, method, via)
            if not via:
                return self._send(401, {"error": "not signed in (browsers: "
                                                 "reload to sign in; scripts: "
                                                 "send X-Panel-Token)"})
            if method == "GET" and path == "/api/projects":
                return self._send(200, project_status())
            if method == "POST" and path == "/api/projects":
                body = self._json_body()
                return self._send(200, create_project(body.get("name", ""),
                                                      body.get("gitUrl", "")))
            if method == "GET" and path == "/api/create":
                with lock:
                    running = creating
                return self._send(200, {"creating": running,
                                        "log": create_tail()})
            if path == "/api/apply":
                if method == "POST":
                    return self._send(200, apply_routes())
                if method == "GET":
                    with lock:
                        running = applying
                    return self._send(200, {"applying": running,
                                            "log": apply_tail()})
            if path == "/api/redeploy":
                if method == "POST":
                    return self._send(200, redeploy_panel())
                if method == "GET":
                    return self._send(200, {"redeploying": redeploy_running(),
                                            "log": redeploy_tail()})
            m = re.fullmatch(
                r"/api/projects/([^/]+)/(log|env|autostart|start|stop|update|claude)",
                path)
            if m:
                name, action = unquote(m.group(1)), m.group(2)
                if action == "claude":
                    project = find_project(name)  # 404 for unknown names
                    if method == "GET":
                        return self._send(200, claude_info(project))
                    if method == "POST":
                        what = self._json_body().get("action")
                        if what == "start":
                            return self._send(200, claude_start(project))
                        if what == "stop":
                            return self._send(200, claude_stop(project))
                        if what == "trust":
                            return self._send(200, claude_trust(project))
                        raise PanelError(400, "\"action\" must be \"start\", "
                                              "\"stop\" or \"trust\"")
                if method == "GET" and action == "log":
                    find_project(name)  # 404 for unknown names
                    full = parse_qs(urlparse(self.path).query).get(
                        "full", ["0"])[0] in ("1", "true", "yes")
                    return self._send(200, log_tail(name, full=full).encode(),
                                      "text/plain; charset=utf-8")
                if action == "env":
                    project = find_project(name)  # 404 for unknown names
                    if method == "GET":
                        return self._send(200, env_report(project))
                    if method == "POST":
                        body = self._json_body()
                        set_project_env_overrides(name, body.get("env", {}))
                        if "envFile" in body:
                            set_project_env_file(name, body["envFile"])
                        return self._send(200, {"ok": True, **env_report(project)})
                if method == "POST" and action == "autostart":
                    find_project(name)  # 404 for unknown names
                    body = self._json_body()
                    value = set_project_launch_on_start(
                        name, body.get("launchOnStart"))
                    return self._send(200, {"ok": True, "launchOnStart": value})
                if method == "POST" and action == "start":
                    return self._send(200, start_project(find_project(name)))
                if method == "POST" and action == "stop":
                    return self._send(200, stop_project(find_project(name)))
                if method == "POST" and action == "update":
                    return self._send(200, update_project(find_project(name)))
            self._send(404, {"error": "not found"})
        except (PanelError, panel_auth.AuthError) as e:
            self._send(e.code, {"error": str(e)})
        except Exception as e:  # bad routes.json, etc.
            self._send(500, {"error": f"{type(e).__name__}: {e}"})

    def do_GET(self):
        path = self._path()
        if path is None:
            return
        if path in ("/", "/index.html"):
            # 200 either way: the sign-in page is what an unauthenticated
            # browser gets here (and what redeploy.sh's health check sees)
            page = PAGE if self._auth_via() else LOGIN_PAGE
            return self._send(200, page.encode(), "text/html; charset=utf-8")
        if path == "/favicon.ico":
            return self._send(404, b"", "text/plain")
        self._dispatch(path, "GET")

    def do_POST(self):
        path = self._path()
        if path is None:
            return
        self._dispatch(path, "POST")


def main():
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(exist_ok=True)
    load_pgids()
    threading.Thread(target=autostart_projects, daemon=True).start()
    if not TOKEN and BIND not in ("127.0.0.1", "::1", "localhost"):
        print("warning: PANEL_TOKEN is not set and the panel is not bound to "
              "localhost — anyone who can reach it can start/stop projects",
              file=sys.stderr)
    server = ThreadingHTTPServer((BIND, PORT), Handler)
    print(f"routing panel on http://{BIND}:{PORT}{PANEL_PATH}  (config: {CONFIG})")
    server.serve_forever()


if __name__ == "__main__":
    main()
