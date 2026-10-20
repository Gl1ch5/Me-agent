"""
name: server
kind: plugin
description: control your own Linux server — commands, root, files, services, logs, jobs, git, docker
icon: ▣
color: blue
---
Gives the agent a `server` tool and you a `/server` command and `me server …` in a shell.

Setup:
1. On the server, run the daemon (stdlib Python 3, nothing to install):
     python3 server.py serve --password SECRET            (port 8722; add --install to make it a systemd service)
   An existing Claude MCP endpoint (https://host/mcp/<secret>) works too — no daemon needed.
2. Here: /server set https://host:8722 SECRET (or the "server" card in the web account settings).

CLI: me server status · me server run "df -h" · me server root "systemctl restart nginx" · me server read /etc/hosts
     me server logs nginx 50 · me server bg "make build" · me server jobs · me server ping
Without me: python3 server.py <same> with ME_SERVER_URL and ME_SERVER_PASSWORD set.
"""
import fnmatch, hmac, json, os, re, shlex, shutil, signal, socket, subprocess, sys, tarfile, threading, time, urllib.error, urllib.request, uuid

# action → (MCP tool name, positional args in CLI order, risky)
ACTIONS = {
    "run": ("run_command", ["cmd", "cwd", "timeout"], False), "root": ("run_root", ["cmd", "cwd", "timeout"], True),
    "read": ("read_file", ["path"], False), "write": ("write_file", ["path", "content"], True),
    "edit": ("edit_file", ["path", "old", "new"], True), "ls": ("list_dir", ["path"], False),
    "find": ("search_files", ["pattern", "path", "search_content", "max_results"], False), "status": ("system_status", [], False),
    "service": ("service_control", ["name", "action"], True), "logs": ("service_logs", ["name", "lines"], False),
    "bg": ("run_background", ["cmd", "cwd"], False), "jobs": ("job_status", ["job_id"], False),
    "joblog": ("job_logs", ["job_id", "lines"], False), "kill": ("job_kill", ["job_id"], True),
    "git": ("git", ["repo", "args"], False), "docker": ("docker", ["args"], True),
    "http": ("http_check", ["url", "method", "timeout"], False), "backup": ("backup_path", ["path"], False),
}
SAFE_SERVICE = ("status",)
CUT = 20000


# ───────────── client ─────────────

PORT = 8722
HELP_SETUP = f"on the server run: python3 server.py serve --password PASSWORD --install  (port {PORT}, password 8+ characters)"


def norm_url(url):
    """'1.2.3.4' or 'host' → http://host:8722; a scheme or a path (an MCP URL) is kept as given."""
    url = (url or "").strip().rstrip("/")
    if url and "://" not in url:
        host, slash, path = url.partition("/")
        url = "http://" + host + ("" if ":" in host or slash else f":{PORT}") + slash + path
    return url


def settings(store=None):
    c = dict((store.config.get("server") or {}) if store else {})
    if not store:
        try:
            cfg = json.load(open(os.path.join(os.environ.get("ME_HOME") or os.path.expanduser("~/.me"), "config.json"), encoding="utf-8"))
            c = cfg.get("server") or {}
        except (OSError, ValueError):
            pass
    return {"url": norm_url(os.environ.get("ME_SERVER_URL") or c.get("url", "")), "password": os.environ.get("ME_SERVER_PASSWORD") or c.get("password", "")}


def _post(url, body, password, sid=None):
    h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream", "User-Agent": "me-server"}
    if password:
        h["Authorization"] = "Bearer " + password
    if sid:
        h["Mcp-Session-Id"] = sid
    args = body.get("args") or body.get("params", {}).get("arguments") or {}
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=h, method="POST")
    with urllib.request.urlopen(req, timeout=int(args.get("timeout") or 60) + 60) as r:
        return r.headers, r.read().decode("utf-8", "replace")


class Client:
    """Talks to the `serve` daemon (POST /call) or to an MCP endpoint (URL containing /mcp)."""

    def __init__(self, url, password):
        self.url, self.password, self.sid, self.n = url.rstrip("/"), password, None, 0

    def mcp(self, method, params=None, note=False):
        msg = {"jsonrpc": "2.0", "method": method, "params": params or {}}
        if not note:
            self.n += 1
            msg["id"] = self.n
        h, body = _post(self.url, msg, self.password, self.sid)
        self.sid = h.get("Mcp-Session-Id") or self.sid
        if note:
            return None
        for line in body.splitlines() if h.get("Content-Type", "").startswith("text/event-stream") else [body]:
            line = line[5:] if line.startswith("data:") else line
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("id") == msg["id"]:
                if r.get("error"):
                    raise RuntimeError(r["error"].get("message", r["error"]))
                return r.get("result") or {}
        raise RuntimeError("no MCP response")

    def call(self, action, args):
        tool = ACTIONS[action][0]
        args = {k: v for k, v in args.items() if v not in (None, "")}
        if "/mcp" in self.url:
            if not self.sid:
                self.mcp("initialize", {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "me", "version": "1"}})
                self.mcp("notifications/initialized", note=True)
            r = self.mcp("tools/call", {"name": tool, "arguments": args})
            text = "\n".join(c.get("text", "") for c in r.get("content", []) if c.get("type") == "text")
            try:
                return not r.get("isError"), json.loads(text)
            except ValueError:
                return not r.get("isError"), text
        try:
            _, body = _post(self.url + "/call", {"tool": tool, "args": args}, self.password)
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")
            if e.code == 401:
                return False, "wrong password (the one given to serve --password)"
            if '"result"' not in body:  # something else answers on this port
                return False, f"HTTP {e.code}: no me server daemon at {self.url}; {HELP_SETUP}"
            return False, f"HTTP {e.code}: {body[:300]}"
        try:
            r = json.loads(body)
        except ValueError:
            return False, f"no me server daemon at {self.url}; {HELP_SETUP}"
        return r.get("ok", False), r.get("result")


def show(v):
    """A tool result as text: stdout/stderr unwrapped, the rest as compact JSON."""
    if isinstance(v, dict) and ("stdout" in v or "stderr" in v):
        out = (v.get("stdout") or "").rstrip()
        if (v.get("stderr") or "").strip():
            out += ("\n" if out else "") + "[stderr]\n" + v["stderr"].rstrip()
        rest = {k: x for k, x in v.items() if k not in ("stdout", "stderr", "cmd", "cwd") and not (k == "exit_code" and x == 0)}
        return (out + "\n" if out else "") + (json.dumps(rest, ensure_ascii=False) if rest else "")
    if isinstance(v, dict) and isinstance(v.get("content"), str):
        return v["content"]
    if isinstance(v, dict) and isinstance(v.get("entries"), list):
        return "\n".join(e.get("name", "") + ("/" if e.get("type") == "dir" else f"  {e.get('size', '')}") for e in v["entries"]) or "(empty)"
    if isinstance(v, dict) and isinstance(v.get("matches"), list):
        return "\n".join(map(str, v["matches"])) or "(no matches)"
    return v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, indent=1)


def call(conf, action, args):
    if not conf["url"]:
        return False, "server is not set: /server set URL PASSWORD (or the server card in the web account)"
    if action not in ACTIONS:
        return False, f"unknown action {action!r}; one of: " + ", ".join(ACTIONS)
    try:
        ok, v = Client(conf["url"], conf["password"]).call(action, args)
    except (urllib.error.URLError, OSError) as e:
        return False, f"{conf['url']}: cannot connect ({getattr(e, 'reason', e)}); {HELP_SETUP}"
    except Exception as e:
        return False, f"{conf['url']}: {type(e).__name__}: {e}"
    return ok, show(v)[:CUT]


def parse_cli(argv):
    """['logs', 'nginx', '50'] → ('logs', {'name': 'nginx', 'lines': 50}); key=value works for any arg."""
    action, args, rest = argv[0], {}, []
    for a in argv[1:]:
        k, eq, v = a.partition("=")
        if eq and k in ACTIONS.get(action, ("", []))[1]:
            args[k] = v
        else:
            rest.append(a)
    names = [n for n in ACTIONS.get(action, ("", []))[1] if n not in args]
    for i, n in enumerate(names[:len(rest)]):
        if n in ("cmd", "args"):  # a command takes all the words left: `run ls -la /var`
            args[n] = " ".join(rest[i:])
            break
        args[n] = rest[i]
    for k in ("timeout", "lines", "max_results"):
        if str(args.get(k, "")).isdigit():
            args[k] = int(args[k])
    if "search_content" in args:
        args["search_content"] = str(args["search_content"]).lower() in ("1", "true", "yes", "content")
    return action, args


def cli(conf, argv, out=print):
    if not argv or argv[0] in ("-h", "--help", "help"):
        out("me server ACTION [args…]   (key=value works too)\n" + "\n".join(f"  {a:8} {' '.join(v[1])}" for a, v in ACTIONS.items())
            + "\n  ping\n  set URL PASSWORD")
        return 0
    if argv[0] == "ping":
        argv = ["status"]
    action, args = parse_cli(argv)
    ok, text = call(conf, action, args)
    out(text)
    return 0 if ok else 1


# ───────────── me plugin ─────────────

if "plugin" in globals():
    def _conf():
        return plugin.store.config.setdefault("server", {"url": "", "password": ""})

    @plugin.tool("server", "Your own remote Linux server (not this machine). action + args: run|root {cmd, cwd?, timeout?}; read {path}; "
                           "write {path, content}; edit {path, old, new}; ls {path}; find {pattern, path?, search_content?}; status; "
                           "service {name, action: start|stop|restart|reload|status|enable|disable}; logs {name, lines?}; bg {cmd, cwd?}; "
                           "jobs {job_id?}; joblog {job_id, lines?}; kill {job_id}; git {repo, args}; docker {args}; http {url, method?}; backup {path}",
                 {"action": {"type": "string", "enum": list(ACTIONS)}, "args": {"type": "object"}}, required=["action"],
                 confirm=lambda a: ACTIONS.get(a.get("action"), ("", [], True))[2]
                 and not (a.get("action") == "service" and (a.get("args") or {}).get("action") in SAFE_SERVICE))
    def server_tool(a, ctx):
        args = a.get("args") or {}
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except ValueError:
                args = {ACTIONS.get(a.get("action"), ("", ["cmd"]))[1][0]: args} if ACTIONS.get(a.get("action"), ("", []))[1] else {}
        ctx.status(f"server {a.get('action')}")
        return call(settings(plugin.store), a.get("action", ""), args)

    @plugin.command("server", "your server: /server set URL PASSWORD · /server status · /server run CMD · /server help")
    def server_cmd(tui, arg):
        argv = shlex.split(arg or "", posix=os.name != "nt") or ["help"]
        if argv[0] == "set":
            if len(argv) < 2:
                c = _conf()
                return tui.toast(f"server: {c.get('url') or 'not set'}", "info")
            _conf().update(url=argv[1], password=argv[2] if len(argv) > 2 else "")
            plugin.store.save()
            return tui.toast("server saved · syncing to your devices", "ok")
        tui.toast(f"server {argv[0]}…")
        threading.Thread(target=lambda: cli(settings(plugin.store), argv, tui.info), daemon=True).start()

    @plugin.cli("server")
    def server_cli(store, argv):
        if argv and argv[0] == "set" and len(argv) > 1:
            store.config["server"] = {"url": argv[1], "password": argv[2] if len(argv) > 2 else ""}
            store.save()
            print("saved")
            return 0
        if argv and argv[0] == "serve":
            return serve(argv[1:])
        return cli(settings(store), argv)


# ───────────── daemon (runs on the server) ─────────────

STATE = os.path.join(os.path.expanduser("~"), ".me-server")
JOBS = {}


def _sh(cmd, cwd=None, timeout=60, root=False):
    if root and os.geteuid() != 0:
        cmd = "sudo -n bash -c " + shlex.quote(cmd)
    if root:
        with open(os.path.join(STATE, "root.log"), "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%F %T')} {cmd}\n")
    try:
        r = subprocess.run(cmd, shell=True, cwd=cwd or None, capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=int(timeout or 60), executable="/bin/bash")
        return {"exit_code": r.returncode, "stdout": r.stdout[-CUT:], "stderr": r.stderr[-4000:]}
    except subprocess.TimeoutExpired as e:
        return {"exit_code": -1, "stdout": (e.stdout or b"")[-CUT:].decode("utf-8", "replace") if isinstance(e.stdout, bytes) else (e.stdout or "")[-CUT:],
                "stderr": f"timed out after {timeout}s"}


def _argv(args):
    return " ".join(shlex.quote(a) for a in shlex.split(args or ""))


def t_read_file(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return {"path": path, "content": f.read(500000)}
    except PermissionError:
        r = _sh("cat " + shlex.quote(path), root=True)
        if r["exit_code"]:
            raise
        return {"path": path, "content": r["stdout"], "elevated": True}


def t_write_file(path, content):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return {"path": path, "bytes": len(content.encode())}


def t_edit_file(path, old, new):
    text = t_read_file(path)["content"]
    n = text.count(old)
    if n != 1:
        return {"error": f"old text found {n} times (must be exactly once)"}
    return t_write_file(path, text.replace(old, new, 1))


def t_list_dir(path="."):
    out = []
    for e in sorted(os.scandir(path), key=lambda e: e.name):
        try:
            out.append({"name": e.name, "type": "dir" if e.is_dir() else "file", "size": e.stat().st_size})
        except OSError:
            out.append({"name": e.name, "type": "?"})
    return {"path": os.path.abspath(path), "entries": out[:1000]}


def t_search_files(pattern, path=".", search_content=False, max_results=100):
    hits, rx = [], re.compile(pattern if search_content else fnmatch.translate(pattern), re.I)
    for root, dirs, files in os.walk(path):
        dirs[:] = [d for d in dirs if d not in (".git", "node_modules", "__pycache__", "venv", ".venv")]
        for name in files:
            p = os.path.join(root, name)
            if not search_content:
                if rx.match(name):
                    hits.append(p)
            else:
                try:
                    with open(p, encoding="utf-8") as f:
                        for i, line in enumerate(f, 1):
                            if rx.search(line):
                                hits.append(f"{p}:{i}: {line.strip()[:200]}")
                                break
                except (OSError, UnicodeDecodeError):
                    continue
            if len(hits) >= int(max_results):
                return {"matches": hits, "truncated": True}
    return {"matches": hits}


def t_system_status():
    mem = {}
    try:
        for line in open("/proc/meminfo"):
            k, v = line.split(":")
            mem[k] = int(v.split()[0]) // 1024
    except OSError:
        pass
    du = shutil.disk_usage("/")
    up = float(open("/proc/uptime").read().split()[0]) if os.path.exists("/proc/uptime") else 0
    return {"host": socket.gethostname(), "uptime_h": round(up / 3600, 1), "load": os.getloadavg(),
            "mem_mb": {"total": mem.get("MemTotal"), "available": mem.get("MemAvailable")},
            "disk_gb": {"total": du.total >> 30, "free": du.free >> 30}, "failed_units": _sh("systemctl --failed --no-legend --plain | head -20")["stdout"].strip()}


def t_service_control(name, action):
    if action not in ("start", "stop", "restart", "reload", "status", "enable", "disable"):
        return {"error": "action: start|stop|restart|reload|status|enable|disable"}
    return _sh(f"systemctl {action} {shlex.quote(name)} --no-pager", root=action != "status")


def t_run_background(cmd, cwd=None):
    os.makedirs(os.path.join(STATE, "jobs"), exist_ok=True)
    jid = uuid.uuid4().hex[:8]
    log = os.path.join(STATE, "jobs", jid + ".log")
    p = subprocess.Popen(cmd, shell=True, cwd=cwd or None, stdout=open(log, "w"), stderr=subprocess.STDOUT, start_new_session=True,
                         executable="/bin/bash")
    JOBS[jid] = {"cmd": cmd, "proc": p, "log": log, "started": time.strftime("%F %T")}
    return {"job_id": jid, "pid": p.pid}


def _job(jid, j):
    rc = j["proc"].poll()
    return {"job_id": jid, "cmd": j["cmd"], "started": j["started"], "running": rc is None, "exit_code": rc}


def t_job_status(job_id=None):
    if job_id:
        return _job(job_id, JOBS[job_id]) if job_id in JOBS else {"error": "no such job"}
    return {"jobs": [_job(k, j) for k, j in JOBS.items()]}


def t_job_logs(job_id, lines=100):
    if job_id not in JOBS:
        return {"error": "no such job"}
    with open(JOBS[job_id]["log"], encoding="utf-8", errors="replace") as f:
        return {"job_id": job_id, "stdout": "".join(f.readlines()[-int(lines):])[-CUT:]}


def t_job_kill(job_id):
    if job_id not in JOBS:
        return {"error": "no such job"}
    try:
        os.killpg(JOBS[job_id]["proc"].pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    return _job(job_id, JOBS[job_id])


def t_http_check(url, method="GET", timeout=10):
    t0 = time.time()
    try:
        with urllib.request.urlopen(urllib.request.Request(url, method=method, headers={"User-Agent": "me-server"}), timeout=int(timeout)) as r:
            code, body = r.status, r.read(600).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        code, body = e.code, e.read(600).decode("utf-8", "replace")
    except Exception as e:
        return {"url": url, "error": f"{type(e).__name__}: {e}"}
    return {"url": url, "status": code, "ms": int((time.time() - t0) * 1000), "body": body}


def t_backup_path(path):
    os.makedirs(os.path.join(STATE, "backups"), exist_ok=True)
    dst = os.path.join(STATE, "backups", f"{os.path.basename(os.path.abspath(path)) or 'root'}-{time.strftime('%Y%m%d-%H%M%S')}.tar.gz")
    with tarfile.open(dst, "w:gz") as t:
        t.add(path)
    return {"backup": dst, "bytes": os.path.getsize(dst)}


TOOLS = {
    "run_command": lambda cmd, cwd=None, timeout=60: _sh(cmd, cwd, timeout),
    "run_root": lambda cmd, cwd=None, timeout=60: _sh(cmd, cwd, timeout, root=True),
    "read_file": t_read_file, "write_file": t_write_file, "edit_file": t_edit_file, "list_dir": t_list_dir,
    "search_files": t_search_files, "system_status": t_system_status, "service_control": t_service_control,
    "service_logs": lambda name, lines=100: _sh(f"journalctl -u {shlex.quote(name)} -n {int(lines)} --no-pager", root=True),
    "run_background": t_run_background, "job_status": t_job_status, "job_logs": t_job_logs, "job_kill": t_job_kill,
    "git": lambda repo, args: _sh(f"git -C {shlex.quote(repo)} {_argv(args)}", timeout=120),
    "docker": lambda args: _sh("docker " + _argv(args), timeout=300),
    "http_check": t_http_check, "backup_path": t_backup_path,
}


FAILS = {}  # ip -> recent failed logins: a wrong password costs a growing delay, so guessing is slow


def handle(password, auth, body, ip=""):
    if not password or not hmac.compare_digest(auth.encode(), ("Bearer " + password).encode()):
        n = FAILS[ip] = FAILS.get(ip, 0) + 1
        time.sleep(min(30, 0.5 * n) if ip else 0)
        return 401, {"ok": False, "result": "bad password"}
    FAILS.pop(ip, None)
    fn = TOOLS.get(body.get("tool"))
    if not fn:
        return 404, {"ok": False, "result": f"unknown tool {body.get('tool')!r}"}
    try:
        r = fn(**(body.get("args") or {}))
    except Exception as e:
        return 200, {"ok": False, "result": f"{type(e).__name__}: {e}"}
    return 200, {"ok": not (isinstance(r, dict) and (r.get("error") or r.get("exit_code"))), "result": r}


def serve(argv):
    import argparse
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    ap = argparse.ArgumentParser(prog="server.py serve")
    ap.add_argument("--password", default=os.environ.get("ME_SERVER_PASSWORD", ""))
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=PORT)
    ap.add_argument("--install", action="store_true", help="install and start a systemd service with these settings")
    o = ap.parse_args(argv)
    if len(o.password) < 8:
        print("a --password of 8+ characters is required")
        return 2
    if o.install:
        unit = (f"[Unit]\nDescription=me server agent\nAfter=network.target\n\n[Service]\nEnvironment=ME_SERVER_PASSWORD={o.password}\n"
                f"ExecStart={sys.executable} {os.path.abspath(__file__)} serve --host {o.host} --port {o.port}\nRestart=always\n"
                f"User={os.environ.get('SUDO_USER') or os.environ.get('USER') or 'root'}\n\n[Install]\nWantedBy=multi-user.target\n")
        path = "/etc/systemd/system/me-server.service"
        with open(path, "w") as f:
            f.write(unit)
        os.chmod(path, 0o600)
        subprocess.run("systemctl daemon-reload && systemctl enable --now me-server", shell=True)
        print(f"installed {path} · http://{socket.gethostname()}:{o.port}")
        return 0
    os.makedirs(STATE, exist_ok=True)

    class H(BaseHTTPRequestHandler):
        def _send(self, code, data):
            b = json.dumps(data, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def do_GET(self):
            self._send(200, {"me-server": True})

        def do_POST(self):
            if self.path.rstrip("/") != "/call":
                return self._send(404, {"ok": False, "result": "POST /call"})
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            except ValueError:
                return self._send(400, {"ok": False, "result": "bad json"})
            self._send(*handle(o.password, self.headers.get("Authorization", ""), body, self.client_address[0]))

        def log_message(self, fmt, *a):
            sys.stderr.write(f"{time.strftime('%T')} {self.address_string()} {fmt % a}\n")

    print(f"me server agent on http://{o.host}:{o.port} — put a TLS proxy (nginx/caddy) in front for internet use")
    ThreadingHTTPServer((o.host, o.port), H).serve_forever()


if __name__ == "__main__":
    a = sys.argv[1:]
    sys.exit(serve(a[1:]) if a[:1] == ["serve"] else cli(settings(), a))
