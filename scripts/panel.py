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
  GET  /default/api/projects/<name>/env   per-project env overrides {KEY: value}
  POST /default/api/projects/<name>/env   replace the project's env overrides
                                          (body: {"env": {KEY: value, ...}});
                                          applied on the next start/restart
  POST /default/api/projects/<name>/start
  POST /default/api/projects/<name>/stop
  POST /default/api/projects/<name>/update  stop, git pull --rebase in
                                            "appPath", then start again
  GET  /default/api/apply                 apply status + output (tail)
  POST /default/api/apply                 run scripts/apply.sh (regenerate
                                          the Caddyfile from routes.json
                                          and reload Caddy)

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
ENV_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*$")
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
        out.append({
            "projectName": name,
            "canLaunch": bool(project.get("launchScript")),
            "canUpdate": bool(project.get("launchScript") and project.get("appPath")),
            "managed": managed,
            "updating": is_updating,
            "ports": ports,
            "state": state,
            "envCount": len(project_env_overrides(name)),
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

    overrides = project_env_overrides(name)
    proc_env = {**os.environ, **overrides} if overrides else None

    log_file = LOG_DIR / f"{slug(name)}.log"
    with open(log_file, "ab") as log:
        log.write(f"\n--- start {time.strftime('%F %T')}: {script}\n".encode())
        if overrides:
            log.write(f"--- env overrides: {', '.join(sorted(overrides))}\n".encode())
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
#applybox{margin:4px 28px 0;padding:0}
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
</style>
</head>
<body>
<header><h1>Projects</h1>
<button id="apply" title="regenerate the Caddyfile from routes.json and reload Caddy">Apply routes</button>
<span id="updated"></span></header>
<div id="err"></div>
<details id="applybox"><summary>apply output</summary><pre id="applylog">…</pre></details>
<main id="grid"></main>
<script>
const TKEY = 'routing-panel-token';
const busy = new Set();
const openLogs = new Set();
const logFull = new Set();     // projects showing the full log (vs. the tail)
const logLoading = new Set();  // in-flight log fetches, so polls don't pile up
const openEnv = new Set();
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
  if (what === 'update') openLogs.add(name);  // surface progress as it streams
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
// envEdit holds the in-progress rows so edits survive the 3s refresh re-render;
// envDirty marks projects whose table has unsaved changes (don't clobber them
// by refetching from the server).
const envEdit = {};
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
  const s = card.querySelector('.savestate');
  if (s) { s.textContent = 'unsaved'; s.className = 'savestate'; }
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
  if (envDirty.has(name)) { renderEnvRows(card, envEdit[name] || []); return; }
  const r = await api('api/projects/' + encodeURIComponent(name) + '/env');
  if (!r || !r.ok) return;
  const d = await r.json().catch(() => ({}));
  const rows = Object.entries(d.env || {}).map(([k, v]) => ({k, v}));
  envEdit[name] = rows;
  renderEnvRows(card, rows);
}

async function saveEnv(name, card) {
  const env = {};
  for (const {k, v} of readEnvRows(card)) { if (k) env[k] = v; }
  const state = card.querySelector('.savestate');
  try {
    const r = await api('api/projects/' + encodeURIComponent(name) + '/env',
      {method: 'POST', headers: {'Content-Type': 'application/json'},
       body: JSON.stringify({env})});
    const d = await r.json().catch(() => ({}));
    if (!r.ok) { showErr(d.error || ('save env failed (' + r.status + ')')); return; }
    envDirty.delete(name);
    envEdit[name] = Object.entries(d.env || {}).map(([k, v]) => ({k, v}));
    renderEnvRows(card, envEdit[name]);
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
    // keep the user's place if they scrolled up to read; follow only at bottom
    const atBottom = pre.scrollHeight - pre.scrollTop - pre.clientHeight < 24;
    pre.textContent = text || '(no output captured yet)';
    const note = card.querySelector('.logbox .lognote');
    if (note) note.textContent =
      (text ? Math.ceil(text.length / 1024) + ' KB' : '0 KB') +
      (full ? ' · full' : ' · tail');
    if (atBottom) pre.scrollTop = pre.scrollHeight;
  } finally {
    logLoading.delete(name);
  }
}

function render(projects) {
  last = projects;
  const grid = document.getElementById('grid');
  grid.innerHTML = '';
  for (const p of projects) {
    const card = document.createElement('div');
    card.className = 'card';
    card.dataset.name = p.projectName;
    const rows = p.ports.map(pt =>
      '<tr><td><span class="dot ' + (pt.listening ? 'on' : '') + '"></span>' + esc(pt.path) + '</td>' +
      '<td class="mono">:' + pt.port + '</td>' +
      '<td class="mono">' + (pt.listening ? '<span class="on-text">listening</span>' : 'down') + '</td></tr>'
    ).join('');
    const isBusy = busy.has(p.projectName) || p.updating;
    const startOff = !p.canLaunch || p.state === 'running' || isBusy;
    const stopOff = (p.state === 'stopped' && !p.managed) || isBusy;
    const updateOff = !p.canUpdate || isBusy;
    const badge = p.updating ? 'updating…' : isBusy ? 'working…' : p.state;
    card.innerHTML =
      '<div class="head"><h2>' + esc(p.projectName) + '</h2>' +
      '<span class="badge ' + p.state + '">' + badge + '</span></div>' +
      '<table>' + rows + '</table>' +
      '<div class="actions">' +
      '<button class="start"' + (startOff ? ' disabled' : '') +
      (p.canLaunch ? '' : ' title="no launchScript configured"') + '>Start</button>' +
      '<button class="stop"' + (stopOff ? ' disabled' : '') + '>Stop</button>' +
      '<button class="update"' + (updateOff ? ' disabled' : '') +
      (p.canUpdate ? '' : ' title="needs launchScript and appPath"') +
      '>Update &amp; Restart</button>' +
      '</div>' +
      '<details class="envbox"' + (openEnv.has(p.projectName) ? ' open' : '') +
      '><summary>environment' + (p.envCount ? ' (' + p.envCount + ')' : '') +
      '</summary><div class="env">' +
      '<table class="rows"></table>' +
      '<div class="foot">' +
      '<button class="addvar">+ variable</button>' +
      '<button class="savevar">Save</button>' +
      '<span class="savestate"></span>' +
      '<span class="note">applied on next start / restart</span>' +
      '</div></div></details>' +
      '<details class="logbox"' + (openLogs.has(p.projectName) ? ' open' : '') +
      '><summary>launch log</summary>' +
      '<div class="logbar">' +
      '<label><input type="checkbox" class="logfull"' +
      (logFull.has(p.projectName) ? ' checked' : '') + '> full log</label>' +
      '<span class="live">● live</span>' +
      '<span class="lognote"></span>' +
      '</div><pre>…</pre></details>';
    card.querySelector('.start').onclick = () => act(p.projectName, 'start');
    card.querySelector('.stop').onclick = () => act(p.projectName, 'stop');
    card.querySelector('.update').onclick = () => act(p.projectName, 'update');

    const env = card.querySelector('.envbox');
    env.addEventListener('toggle', () => {
      env.open ? openEnv.add(p.projectName) : openEnv.delete(p.projectName);
      if (env.open) loadEnv(p.projectName, card);
    });
    card.querySelector('.addvar').onclick = () => {
      addEnvRow(card, '', ''); markEnvDirty(p.projectName, card);
    };
    card.querySelector('.savevar').onclick = () => saveEnv(p.projectName, card);
    if (env.open) loadEnv(p.projectName, card);

    const det = card.querySelector('.logbox');
    det.addEventListener('toggle', () => {
      det.open ? openLogs.add(p.projectName) : openLogs.delete(p.projectName);
      if (det.open) loadLog(p.projectName, card);
    });
    card.querySelector('.logfull').onchange = e => {
      e.target.checked ? logFull.add(p.projectName) : logFull.delete(p.projectName);
      loadLog(p.projectName, card);
    };
    if (det.open) loadLog(p.projectName, card);
    grid.appendChild(card);
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
  pre.textContent = d.log || '(no apply has run yet)';
  pre.scrollTop = pre.scrollHeight;
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

// Live tail: refresh any open log between the slower full-grid refreshes,
// updating just its <pre> so the view doesn't flicker or lose scroll.
function pollLogs() {
  if (!openLogs.size) return;
  for (const card of document.querySelectorAll('#grid .card')) {
    if (openLogs.has(card.dataset.name)) loadLog(card.dataset.name, card);
  }
}

refresh();
showApply(false);  // sync button state in case an apply is already running
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
                    find_project(name)  # 404 for unknown names
                    if method == "GET":
                        return self._send(200, {"env": project_env_overrides(name)})
                    if method == "POST":
                        env = set_project_env_overrides(name, self._json_body().get("env", {}))
                        return self._send(200, {"ok": True, "env": env})
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
