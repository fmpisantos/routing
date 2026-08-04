#!/usr/bin/env python3
"""Control panel for the projects in routes.json, served at /default
(configurable via $PANEL_PATH so several instances can share a host).

Shows, per project, whether each of its ports is listening, and offers
start/stop for projects that define a "launchScript":

  start — runs the script via `bash -lc`, detached in its own process
          group; output is captured to ~/.local/state/routing-panel/logs/
  stop  — SIGTERMs every process group found listening on the project's
          ports (plus the group we launched, if any), SIGKILL after 5s.

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

Env:
  PANEL_PORT     listen port (default 8090)
  PANEL_PATH     URL prefix the panel is served under (default /default)
  PANEL_BIND     bind address (default 127.0.0.1 — Caddy proxies to us)
  PANEL_TOKEN    if set, API requests must send an X-Panel-Token header.
                 Set this if the proxy is exposed via Funnel: otherwise
                 anyone on the internet can start/stop your projects.
  ROUTES_CONFIG  path to routes.json (default: repo root)
"""

import json
import os
import re
import shlex
import signal
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

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

lock = threading.Lock()
pgids = {}  # projectName -> pgid of the process group we launched
updating = set()  # projectNames currently mid update-and-restart
applying = False  # scripts/apply.sh currently running (global, not per-project)


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
            "managed": managed,
            "updating": is_updating,
            "ports": ports,
            "state": state,
            "envCount": len(env["merged"]),
            "envFile": env["path"],
            "envFileCount": len(env["fileVars"]),
            "envFileProblem": env["problems"][0] if env["problems"] else "",
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
details{margin-top:12px;font-size:12px}
#applybox,#redeploybox{margin:4px 28px 0;padding:0}
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
<span id="updated"></span></header>
<div id="err"></div>
<details id="applybox"><summary>apply output</summary><pre id="applylog">…</pre></details>
<details id="redeploybox"><summary>redeploy output</summary><pre id="redeploylog">…</pre></details>
<main id="grid"></main>
<script>
const TKEY = 'routing-panel-token';
const busy = new Set();
const logFull = new Set();     // projects showing the full log (vs. the tail)
const logLoading = new Set();  // in-flight log fetches, so polls don't pile up
const cards = {};              // projectName -> its <div class="card">, kept across refreshes
let last = [];

function authHeaders() {
  const t = localStorage.getItem(TKEY);
  return t ? {'X-Panel-Token': t} : {};
}

async function api(path, opts = {}) {
  let r = await fetch(path, {...opts, headers: {...authHeaders(), ...(opts.headers || {})}});
  if (r.status === 401) {
    const t = prompt('This panel requires a token (PANEL_TOKEN):');
    if (t) {
      localStorage.setItem(TKEY, t);
      r = await fetch(path, {...opts, headers: {...authHeaders(), ...(opts.headers || {})}});
    }
  }
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
  badge.textContent = p.updating ? 'updating…' : isBusy ? 'working…' : p.state;

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
  }
}

refresh();
showApply(false);     // sync button state in case an apply is already running
showRedeploy(false);  // and in case a redeploy is still finishing after a restart
setInterval(refresh, 3000);
setInterval(pollLogs, 1500);
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    server_version = "routing-panel"

    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
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

    def _authed(self):
        return not TOKEN or self.headers.get("X-Panel-Token") == TOKEN

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
        if not self._authed():
            return self._send(401, {"error": "missing or bad X-Panel-Token"})
        try:
            if method == "GET" and path == "/api/projects":
                return self._send(200, project_status())
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
            m = re.fullmatch(r"/api/projects/([^/]+)/(log|env|start|stop|update)", path)
            if m:
                name, action = unquote(m.group(1)), m.group(2)
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
                if method == "POST" and action == "start":
                    return self._send(200, start_project(find_project(name)))
                if method == "POST" and action == "stop":
                    return self._send(200, stop_project(find_project(name)))
                if method == "POST" and action == "update":
                    return self._send(200, update_project(find_project(name)))
            self._send(404, {"error": "not found"})
        except PanelError as e:
            self._send(e.code, {"error": str(e)})
        except Exception as e:  # bad routes.json, etc.
            self._send(500, {"error": f"{type(e).__name__}: {e}"})

    def do_GET(self):
        path = self._path()
        if path is None:
            return
        if path in ("/", "/index.html"):
            return self._send(200, PAGE.encode(), "text/html; charset=utf-8")
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
    if not TOKEN and BIND not in ("127.0.0.1", "::1", "localhost"):
        print("warning: PANEL_TOKEN is not set and the panel is not bound to "
              "localhost — anyone who can reach it can start/stop projects",
              file=sys.stderr)
    server = ThreadingHTTPServer((BIND, PORT), Handler)
    print(f"routing panel on http://{BIND}:{PORT}{PANEL_PATH}  (config: {CONFIG})")
    server.serve_forever()


if __name__ == "__main__":
    main()
