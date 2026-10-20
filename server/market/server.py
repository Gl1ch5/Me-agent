"""
name: server
kind: plugin
description: control your own Linux server over SSH — commands, root, files, services, logs, jobs, git, docker; nothing to install there
icon: ▣
color: blue
---
Gives the agent a `server` tool, you a `/server` command and `me server …` in a shell. Works over plain SSH (paramiko,
installed by itself on first use): nothing is installed on the server.

Setup: the "your server" card in the web account settings (address, user, port, password), or
  /server set root@1.2.3.4 PASSWORD      (blank password = your SSH key / agent; another port: root@host:2222)

CLI: me server                     interactive shell on the server (`:read /etc/hosts` and the other actions work there too)
     me server status · me server run "df -h" · me server root "systemctl restart nginx" · me server read /etc/hosts
     me server logs nginx 50 · me server bg "make build" · me server jobs · me server put ./dist /var/www/app
Without me: python3 server.py <same>, with ME_SERVER=root@host and ME_SERVER_PASSWORD.
"""
import json, os, posixpath, re, shlex, stat, subprocess, sys, threading, time, uuid

# action → (positional args in CLI order, risky: asks before the agent runs it)
ACTIONS = {
    "run": (["cmd", "cwd", "timeout"], False), "root": (["cmd", "cwd", "timeout"], True),
    "read": (["path"], False), "write": (["path", "content"], True), "edit": (["path", "old", "new"], True),
    "ls": (["path"], False), "find": (["pattern", "path", "content"], False), "status": ([], False),
    "service": (["name", "action"], True), "logs": (["name", "lines"], False),
    "bg": (["cmd", "cwd"], False), "jobs": ([], False), "joblog": (["job_id", "lines"], False), "kill": (["job_id"], True),
    "git": (["repo", "args"], False), "docker": (["args"], True), "http": (["url", "method"], False), "backup": (["path"], False),
    "put": (["local", "remote"], True), "get": (["remote", "local"], False),
}
CUT = 20000
CONNS = {}
JOBS = "~/.me-jobs"


def home_dir():
    return os.environ.get("ME_HOME") or os.path.join(os.path.expanduser("~"), ".me")


def target(c):
    """config.server → (user, host, port). `host` may be a whole 'ssh -p 2222 root@1.2.3.4' or 'root@1.2.3.4:2222';
    separate user/port fields win; the user defaults to root, the port to 22."""
    raw = (c.get("host") or c.get("url") or "").strip()
    m = re.search(r"-p\s*(\d+)", raw)
    port = m.group(1) if m else ""
    words = [w for w in re.sub(r"-p\s*\d+", "", raw).split() if w != "ssh" and not w.startswith("-")]
    raw = re.sub(r"^\w+://", "", words[-1] if words else "").split("/")[0]
    user, _, host = raw.rpartition("@")
    host, _, p = host.partition(":")
    port = str(c.get("port") or "").strip() or port or p
    return (c.get("user") or "").strip() or user or "root", host, int(port) if port.isdigit() else 22


def settings(store=None):
    c = dict((store.config.get("server") or {}) if store else {})
    if not store:
        try:
            with open(os.path.join(home_dir(), "config.json"), encoding="utf-8") as f:
                c = json.load(f).get("server") or {}
        except (OSError, ValueError):
            pass
    if os.environ.get("ME_SERVER"):
        c = {"host": os.environ["ME_SERVER"], "password": os.environ.get("ME_SERVER_PASSWORD", "")}
    user, host, port = target(c)
    return {"user": user, "host": host, "port": port, "password": c.get("password") or ""}


def paramiko():
    try:
        import paramiko as p
        return p
    except ImportError:
        pass
    deps = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".deps")
    if deps not in sys.path:
        sys.path.insert(0, deps)
    try:
        import paramiko as p
        return p
    except ImportError:
        r = subprocess.run([sys.executable, "-m", "pip", "install", "-q", "--target", deps, "paramiko"], capture_output=True, text=True)
        if r.returncode:
            raise RuntimeError("could not install paramiko (pip install paramiko): " + (r.stderr or r.stdout).strip()[-300:])
        import paramiko as p
        return p


class Conn:
    """One SSH connection: shell commands (as root through sudo when the login is not root) and SFTP for files."""

    def __init__(self, conf):
        self.user, self.host, self.port, self.password = conf["user"], conf["host"], conf["port"], conf["password"] or None
        pm = paramiko()
        self.c = pm.SSHClient()
        known = os.path.join(home_dir(), "server_known_hosts")
        if os.path.exists(known):
            self.c.load_host_keys(known)
        self.c.set_missing_host_key_policy(pm.AutoAddPolicy())  # trust on first use; a changed key is refused after that
        try:
            self.c.connect(self.host, self.port, self.user, self.password, timeout=15, allow_agent=not self.password,
                           look_for_keys=not self.password)
        except pm.BadHostKeyException:
            raise RuntimeError(f"the host key of {self.host} changed. If you reinstalled the server, delete its line in {known}")
        except pm.AuthenticationException:
            raise RuntimeError(f"SSH login {self.user}@{self.host} failed: wrong password (or no key)")
        os.makedirs(os.path.dirname(known), exist_ok=True)
        self.c.save_host_keys(known)
        self.c.get_transport().set_keepalive(30)
        self._sftp = None

    def alive(self):
        t = self.c.get_transport()
        return bool(t and t.is_active())

    def sftp(self):
        if self._sftp is None:
            self._sftp = self.c.open_sftp()
        return self._sftp

    def sh(self, cmd, cwd=None, timeout=60, root=False, stdin=None, cancel=None):
        if cwd:
            cmd = f"cd {shlex.quote(cwd)} && {cmd}"
        if root and self.user != "root":
            cmd = "sudo -S -p '' bash -c " + shlex.quote(cmd)
            stdin = (self.password or "") + "\n" + (stdin or "")
        ch = self.c.get_transport().open_session()
        ch.exec_command(cmd)
        if stdin:
            ch.sendall(stdin.encode())
        ch.shutdown_write()
        out, err, t0 = [], [], time.time()
        while True:
            while ch.recv_ready():
                out.append(ch.recv(65536))
            while ch.recv_stderr_ready():
                err.append(ch.recv_stderr(65536))
            if ch.exit_status_ready() and not ch.recv_ready() and not ch.recv_stderr_ready():
                break
            if time.time() - t0 > int(timeout or 60) or (cancel and cancel.is_set()):
                ch.close()
                return {"exit_code": -1, "stdout": b"".join(out).decode("utf-8", "replace")[-CUT:], "stderr": f"stopped after {int(time.time() - t0)}s"}
            time.sleep(0.03)
        return {"exit_code": ch.recv_exit_status(), "stdout": b"".join(out).decode("utf-8", "replace")[-CUT:],
                "stderr": b"".join(err).decode("utf-8", "replace")[-4000:]}

    def read(self, path):
        try:
            with self.sftp().open(path) as f:
                return f.read(500000).decode("utf-8", "replace")
        except PermissionError:
            r = self.sh("cat " + shlex.quote(path), root=True)
            if r["exit_code"]:
                raise RuntimeError(r["stderr"].strip() or "permission denied")
            return r["stdout"]

    def write(self, path, text):
        self.sh("mkdir -p " + shlex.quote(posixpath.dirname(path) or "."))
        try:
            with self.sftp().open(path, "w") as f:
                f.write(text.encode())
        except PermissionError:  # a root-owned file: through sudo tee
            r = self.sh("tee " + shlex.quote(path) + " >/dev/null", root=True, stdin=text)
            if r["exit_code"]:
                raise RuntimeError(r["stderr"].strip() or "permission denied")

    def listdir(self, path):
        return [(a.filename + ("/" if stat.S_ISDIR(a.st_mode or 0) else ""), a.st_size) for a in self.sftp().listdir_attr(path)]

    def put(self, local, remote):
        if os.path.isdir(local):
            for root, _, files in os.walk(local):
                d = posixpath.normpath(posixpath.join(remote, os.path.relpath(root, local).replace(os.sep, "/")))
                self.sh("mkdir -p " + shlex.quote(d))
                for n in files:
                    self.sftp().put(os.path.join(root, n), posixpath.join(d, n))
        else:
            self.sh("mkdir -p " + shlex.quote(posixpath.dirname(remote) or "."))
            self.sftp().put(local, remote)

    def get(self, remote, local):
        if os.path.isdir(local):
            local = os.path.join(local, posixpath.basename(remote))
        self.sftp().get(remote, local)
        return local


def connect(conf):
    key = (conf["user"], conf["host"], conf["port"], conf["password"])
    c = CONNS.get(key)
    if not c or not c.alive():
        c = CONNS[key] = Conn(conf)
    return c


def act(c, action, a, cancel=None):
    """Run one action on connection `c`: text, or a dict from sh(); raises on errors."""
    q = lambda k, d="": shlex.quote(str(a.get(k) or d))
    if action in ("run", "root"):
        return c.sh(a["cmd"], a.get("cwd"), a.get("timeout") or 120, root=action == "root", cancel=cancel)
    if action == "read":
        return c.read(a["path"])
    if action == "write":
        c.write(a["path"], a.get("content", ""))
        return f"wrote {a['path']} ({len(a.get('content', '').encode())} bytes)"
    if action == "edit":
        text = c.read(a["path"])
        n = text.count(a["old"])
        if n != 1:
            raise RuntimeError(f"old text found {n} times in {a['path']} (must be exactly once)")
        c.write(a["path"], text.replace(a["old"], a.get("new", ""), 1))
        return f"edited {a['path']}"
    if action == "ls":
        return "\n".join(n + ("" if n.endswith("/") else f"  {s}") for n, s in sorted(c.listdir(a.get("path") or "."))) or "(empty)"
    if action == "find":
        if a.get("content"):
            return c.sh(f"grep -rIn --exclude-dir=.git --exclude-dir=node_modules -E {q('pattern')} {q('path', '.')} | head -100", timeout=120)
        return c.sh(f"find {q('path', '.')} -name {q('pattern')} -not -path '*/.git/*' -not -path '*/node_modules/*' 2>/dev/null | head -100", timeout=120)
    if action == "status":
        return c.sh("echo \"$(hostname) · $(uptime -p 2>/dev/null) · load $(cut -d' ' -f1-3 /proc/loadavg)\"; free -m | head -2; df -h / | tail -1;"
                    " echo 'failed units:'; systemctl --failed --no-legend --plain 2>/dev/null | head -20")
    if action == "service":
        if a.get("action") not in ("start", "stop", "restart", "reload", "status", "enable", "disable"):
            raise RuntimeError("action: start|stop|restart|reload|status|enable|disable")
        return c.sh(f"systemctl {a['action']} {q('name')} --no-pager", root=a["action"] != "status")
    if action == "logs":
        return c.sh(f"journalctl -u {q('name')} -n {int(a.get('lines') or 100)} --no-pager", root=True)
    jid = re.sub(r"\W", "", str(a.get("job_id", "")))
    if action == "bg":
        jid = uuid.uuid4().hex[:6]
        cd = f"cd {q('cwd')} && " if a.get("cwd") else ""
        r = c.sh(f"mkdir -p {JOBS} && printf %s {q('cmd')} > {JOBS}/{jid}.cmd && {{ {cd}nohup setsid bash -c {q('cmd')} > {JOBS}/{jid}.log 2>&1 "
                 f"< /dev/null & echo $! > {JOBS}/{jid}.pid; }}")
        return r if r["exit_code"] else f"started job {jid} — `joblog {jid}` shows its output"
    if action == "jobs":
        r = c.sh(f"for f in {JOBS}/*.pid; do [ -e \"$f\" ] || continue; id=$(basename \"$f\" .pid); "
                 f"case \"$(ps -o stat= -p $(cat \"$f\") 2>/dev/null)\" in ''|Z*) s=done;; *) s=running;; esac; echo \"$id  $s  $(cat {JOBS}/$id.cmd)\"; done")
        return r if r["exit_code"] or r["stdout"].strip() else "no jobs"
    if action == "joblog":
        return c.sh(f"tail -n {int(a.get('lines') or 100)} {JOBS}/{jid}.log")
    if action == "kill":
        return c.sh(f"p=$(cat {JOBS}/{jid}.pid) && (kill -- -$p 2>/dev/null || kill $p) && echo killed {jid}")
    if action == "git":
        return c.sh(f"git -C {q('repo')} {a.get('args', '')}", timeout=300)
    if action == "docker":
        return c.sh("docker " + a.get("args", ""), timeout=600)
    if action == "http":
        return c.sh(f"curl -sS -o /dev/null -w '%{{http_code}} in %{{time_total}}s\\n' -X {q('method', 'GET')} --max-time 20 {q('url')}", timeout=30)
    if action == "backup":
        p = a["path"].rstrip("/") or "/"
        name = re.sub(r"[^\w.-]", "_", posixpath.basename(p) or "root")
        dst = f"~/.me-backups/{name}-{time.strftime('%Y%m%d-%H%M%S')}.tar.gz"
        return c.sh(f"mkdir -p ~/.me-backups && tar czf {dst} -C {shlex.quote(posixpath.dirname(p) or '/')} {shlex.quote(posixpath.basename(p) or '.')}"
                    f" && ls -lh {dst}", timeout=600)
    if action == "put":
        c.put(os.path.expanduser(a["local"]), a["remote"])
        return f"uploaded {a['local']} → {a['remote']}"
    if action == "get":
        return "downloaded → " + c.get(a["remote"], os.path.expanduser(a.get("local") or "."))
    raise RuntimeError(f"unknown action {action!r}; one of: " + ", ".join(ACTIONS))


def show(v):
    if not isinstance(v, dict):
        return str(v)
    out = (v.get("stdout") or "").rstrip()
    if (v.get("stderr") or "").strip():
        out += ("\n" if out else "") + "[stderr] " + v["stderr"].strip()
    if v.get("exit_code"):
        out += f"\n[exit {v['exit_code']}]"
    return out.strip() or "(no output)"


def call(conf, action, args, cancel=None):
    if not conf["host"]:
        return False, "server is not set: /server set root@HOST PASSWORD (or the 'your server' card in the web account)"
    if action not in ACTIONS:
        return False, f"unknown action {action!r}; one of: " + ", ".join(ACTIONS)
    need = [k for k in ACTIONS[action][0][:1] if not args.get(k) and action not in ("ls",)]
    if need:
        return False, f"{action} needs: {need[0]}"
    try:
        v = act(connect(conf), action, args, cancel)
    except Exception as e:
        CONNS.pop((conf["user"], conf["host"], conf["port"], conf["password"]), None)
        return False, f"{conf['host']}: {e if isinstance(e, RuntimeError) else type(e).__name__ + ': ' + str(e)}"
    return not (isinstance(v, dict) and v.get("exit_code")), show(v)[:CUT]


def parse_cli(argv):
    """['logs', 'nginx', '50'] → ('logs', {'name': 'nginx', 'lines': 50}); key=value works for any arg."""
    action, args, rest = argv[0], {}, []
    names = ACTIONS.get(action, ([], 0))[0]
    for a in argv[1:]:
        k, eq, v = a.partition("=")
        if eq and k in names:
            args[k] = v
        else:
            rest.append(a)
    names = [n for n in names if n not in args]
    for i, n in enumerate(names[:len(rest)]):
        if n in ("cmd", "args"):  # a command takes all the words left: `run ls -la /var`
            args[n] = " ".join(rest[i:])
            break
        args[n] = rest[i]
    for k in ("timeout", "lines"):
        if str(args.get(k, "")).isdigit():
            args[k] = int(args[k])
    return action, args


def usage():
    return ("me server                 interactive shell on the server\nme server ACTION [args…]  (key=value works too)\n"
            + "\n".join(f"  {a:8} {' '.join(v[0])}" for a, v in ACTIONS.items()) + "\n  set      root@host[:port] PASSWORD")


def cli(conf, argv, out=print):
    if argv and argv[0] in ("-h", "--help", "help"):
        out(usage())
        return 0
    if not argv:
        return shell(conf, out)
    action, args = parse_cli(argv)
    ok, text = call(conf, action, args)
    out(text)
    return 0 if ok else 1


def shell(conf, out=print, read=input):
    """A remote prompt: lines run on the server (cd is remembered), `:action args` runs an action, exit leaves."""
    ok, cwd = call(conf, "run", {"cmd": "pwd"})
    if not ok:
        out(cwd)
        return 1
    out(f"connected to {conf['user']}@{conf['host']} · commands run there · :read /etc/hosts and other actions (:help) · exit to leave")
    while True:
        try:
            line = read(f"{conf['user']}@{conf['host']}:{cwd}$ ").strip()
        except (EOFError, KeyboardInterrupt):
            out("")
            return 0
        if line in ("exit", "quit", "logout"):
            return 0
        if not line:
            continue
        if line.startswith(":"):
            argv = shlex.split(line[1:]) or ["help"]
            if argv[0] not in ACTIONS:
                out(usage())
                continue
            action, args = parse_cli(argv)
            for k in ("path", "remote", "repo", "cwd"):  # relative to where the prompt is
                if args.get(k) and not args[k].startswith(("/", "~")):
                    args[k] = posixpath.join(cwd, args[k])
            if action in ("run", "root", "bg"):
                args.setdefault("cwd", cwd)
            if action == "ls":
                args.setdefault("path", cwd)
            out(call(conf, action, args)[1])
        elif line == "cd" or line.startswith("cd "):
            ok, text = call(conf, "run", {"cmd": f"{line} && pwd", "cwd": cwd})
            if ok:
                cwd = text.strip().splitlines()[-1]
            else:
                out(text)
        else:
            out(call(conf, "run", {"cmd": line, "cwd": cwd, "timeout": 3600})[1])


def save(store, argv):
    """`set root@host[:port] [password]`: the address as typed, user/port split out of it."""
    user, host, port = target({"host": argv[0]})
    store.config["server"] = {"host": host, "user": user, "port": port, "password": argv[1] if len(argv) > 1 else ""}
    store.save()


# ───────────── me plugin ─────────────

if "plugin" in globals():
    def _risky(a):
        if a.get("action") == "service":
            return (a.get("args") or {}).get("action") != "status"
        return ACTIONS.get(a.get("action"), ([], True))[1]

    @plugin.tool("server", "The user's own remote Linux server over SSH (not this machine). action + args: run|root {cmd, cwd?, timeout?}; "
                           "read {path}; write {path, content}; edit {path, old, new}; ls {path}; find {pattern, path?, content?: grep}; status; "
                           "service {name, action: start|stop|restart|reload|status|enable|disable}; logs {name, lines?}; bg {cmd, cwd?}; jobs; "
                           "joblog {job_id}; kill {job_id}; git {repo, args}; docker {args}; http {url}; backup {path}; put {local, remote}; get {remote, local?}",
                 {"action": {"type": "string", "enum": list(ACTIONS)}, "args": {"type": "object"}}, required=["action"], confirm=_risky)
    def server_tool(a, ctx):
        args = a.get("args") or {}
        if isinstance(args, str):  # some models send the args as a JSON string, or just the command
            try:
                args = json.loads(args)
            except ValueError:
                first = ACTIONS.get(a.get("action"), ([], 0))[0]
                args = {first[0]: args} if first else {}
        conf = settings(plugin.store)
        ctx.status(f"{conf['user']}@{conf['host']}: {a.get('action')}")
        return call(conf, a.get("action", ""), args, ctx.cancel)

    @plugin.command("server", "your server over SSH: /server set root@HOST PASSWORD · /server status · /server run CMD · /server help")
    def server_cmd(tui, arg):
        argv = shlex.split(arg or "", posix=os.name != "nt") or ["help"]
        if argv[0] == "set":
            if len(argv) < 2:
                c = settings(plugin.store)
                return tui.toast(f"server: {c['user']}@{c['host']}:{c['port']}" if c["host"] else "server: not set", "info")
            save(plugin.store, argv[1:])
            return tui.toast("server saved · syncing to your devices", "ok")
        tui.toast(f"server {argv[0]}…")
        threading.Thread(target=lambda: cli(settings(plugin.store), argv, tui.info), daemon=True).start()

    @plugin.cli("server")
    def server_cli(store, argv):
        if argv and argv[0] == "set" and len(argv) > 1:
            save(store, argv[1:])
            print("saved")
            return 0
        return cli(settings(store), argv)


if __name__ == "__main__":
    sys.exit(cli(settings(), sys.argv[1:]))
