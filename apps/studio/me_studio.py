"""me studio — a command center for coding agents (in the spirit of the Codex app), built on the me agent.

Projects in the sidebar; every task is its own agent thread, and with "worktree" it works in its own git worktree on its own
branch, so several tasks run in parallel without touching each other or your checkout. Watch each one live, answer its
approvals, then review the diff and apply it to the project, commit, push or discard it. A terminal runs in the task's folder.

  python me_studio.py            native window (pywebview) or, without pywebview, your browser
  python me_studio.py --browser  always the browser        --port N   fixed port
  me studio                      the same, started from the me CLI (downloads this file once)

Data: tasks are ordinary me sessions (they sync with your account); studio state is in ~/.me/studio.json, worktrees
in ~/.me/worktrees/.
"""
import collections, hashlib, json, os, re, secrets, socket, subprocess, sys, threading, time, uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
if "me" not in sys.modules:  # me.py next to this file (packaged app) or at the repo root (a checkout)
    for _d in (HERE, HERE.parent.parent, Path.home() / ".me" / "apps"):
        if (_d / "me.py").is_file():
            sys.path.insert(0, str(_d))
            break
import me  # noqa: E402

VERSION = "2.0"
STATE = me.HOME / "studio.json"
WORKTREES = me.HOME / "worktrees"


# ───────────────────────────── git ─────────────────────────────

def git(cwd, *args, check=True, data=None, env=None):
    r = subprocess.run(["git", "-C", str(cwd), *args], input=data, capture_output=True, text=True, encoding="utf-8", errors="replace",
                       env=env, **me.NO_WINDOW)
    if check and r.returncode:
        raise RuntimeError((r.stderr or r.stdout).strip()[:1200] or f"git {args[0]} failed")
    return r.stdout


def git_root(path):
    try:
        return git(path, "rev-parse", "--show-toplevel").strip() or None
    except (RuntimeError, OSError):
        return None


def split_patch(patch):
    """A unified git patch → [{path, add, del, diff, new, deleted, binary}]."""
    files = []
    for chunk in re.split(r"(?m)^(?=diff --git )", patch):
        if not chunk.startswith("diff --git "):
            continue
        m = re.match(r"diff --git a/(.*?) b/(.*)$", chunk.split("\n", 1)[0])
        path = m.group(2) if m else chunk.split("\n", 1)[0][11:]
        body = chunk.split("\n")
        files.append({"path": path, "diff": chunk.rstrip("\n"), "new": "\nnew file mode" in chunk, "deleted": "\ndeleted file mode" in chunk,
                      "binary": "\nGIT binary patch" in chunk or "\nBinary files " in chunk,
                      "add": sum(1 for l in body if l.startswith("+") and not l.startswith("+++")),
                      "del": sum(1 for l in body if l.startswith("-") and not l.startswith("---"))})
    return files


# build output and caches never belong in a task's change set (projects without a .gitignore would show them)
JUNK = [":(exclude,glob)**/__pycache__/**", ":(exclude,glob)**/*.pyc", ":(exclude,glob)**/node_modules/**", ":(exclude,glob)**/.DS_Store",
        ":(exclude,glob)**/.pytest_cache/**", ":(exclude,glob)**/.mypy_cache/**"]


def stage(wt):
    """Stage everything the task changed in its own worktree (new files included), minus caches."""
    git(wt, "add", "-A", "--", ".", *JUNK)


def slug(text, n=28):
    s = re.sub(r"[^a-z0-9]+", "-", text.lower().encode("ascii", "ignore").decode()).strip("-")
    return s[:n].strip("-") or "task"


# ───────────────────────────── tasks ─────────────────────────────

class Task:
    """One agent thread. Its events go into a numbered log the UI long-polls; approvals block the agent until answered."""

    def __init__(self, studio, meta):
        self.st, self.meta = studio, meta
        self.events, self.seq, self.cond = collections.deque(maxlen=4000), 0, threading.Condition()
        self.waits, self.queue = {}, []
        self.agent = me.Agent(studio.store, self.push, self.confirm, self.ask)
        sess = me.jload(studio.store.session_path(meta["session"])) if meta.get("session") else None
        if sess:
            sess.setdefault("usage", {"in": 0, "out": 0})
            self.agent.session = sess
            self.agent.ctx_used = sess.get("ctx", 0)
        else:
            self.agent.session = me.new_session(meta["cwd"])
            meta["session"] = self.agent.session["id"]
        s = self.agent.session
        s["cwd"], s["title"] = meta["cwd"], s.get("title") or meta["title"]
        for k in ("model", "effort"):
            if meta.get(k):
                s[k] = meta[k]
            else:
                s.pop(k, None)
        self.agent.take_queued = self.take_queued
        self.thread = None

    # events
    def push(self, e):
        t = e.get("type")
        m = self.meta
        if t == "turn_start":
            m["status"], m["error"] = "running", ""
        elif t == "error" and e.get("text") != "interrupted":
            m["error"] = e.get("text", "")[:400]
        elif t == "turn_diff" and e.get("files"):
            m["changes"] = True
        with self.cond:
            self.seq += 1
            self.events.append(dict(e, seq=self.seq))
            self.cond.notify_all()
        m["updated"] = time.time()
        if t in ("turn_end", "turn_diff", "error"):
            self.st.save()

    def wait_events(self, after, timeout=25):
        with self.cond:
            if self.seq <= after:
                self.cond.wait(timeout)
            return [e for e in self.events if e["seq"] > after]

    def _wait(self, kind, **kw):
        rid = uuid.uuid4().hex[:8]
        ev = threading.Event()
        self.waits[rid] = [ev, None, dict(type=kind, id=rid, **kw)]
        prev, self.meta["status"] = self.meta.get("status"), "waiting"
        self.push(self.waits[rid][2])
        ev.wait()
        self.meta["status"] = prev if prev != "waiting" else "running"
        return self.waits.pop(rid)[1]

    def confirm(self, name, args):
        r = self._wait("confirm", name=name, args=args)
        return "always" if r == "always" else bool(r)

    def ask(self, q, options=None):
        return self._wait("ask", text=q, options=options) or ""

    def answer(self, rid, value):
        w = self.waits.get(rid)
        if w:
            w[1] = value
            w[0].set()

    def take_queued(self):
        out, self.queue = self.queue, []
        return out

    # running
    @property
    def busy(self):
        return bool(self.thread and self.thread.is_alive())

    def send(self, text):
        """Start a turn, or hand the message to the running one (it reaches the model after the current step)."""
        if self.busy:
            self.queue.append(text)
            self.push({"type": "queued", "text": text})
            return
        self.push({"type": "user", "text": text})

        def run():
            try:
                self.agent.run(text)
                while self.queue and not self.agent.cancel.is_set():
                    nxt = self.queue.pop(0)
                    self.push({"type": "user", "text": nxt})
                    self.agent.run(nxt)
            except Exception as e:  # never leave a task stuck in "running"
                self.push({"type": "error", "text": f"{type(e).__name__}: {e}"})
            finally:
                self.meta["status"] = "error" if self.meta.get("error") else "review" if self.meta.get("changes") else "done"
                self.meta["updated"] = time.time()
                self.st.save()
                self.push({"type": "status", "status": self.meta["status"]})
        self.meta["status"] = "running"
        self.thread = threading.Thread(target=run, daemon=True, name="task-" + self.meta["id"])
        self.thread.start()

    def stop(self):
        self.queue.clear()
        self.agent.cancel.set()
        for w in list(self.waits.values()):
            w[0].set()

    # review
    def diff(self):
        m = self.meta
        if m["mode"] == "worktree":
            if not os.path.isdir(m["wt"]):
                return {"files": [], "note": "the worktree is gone"}
            stage(m["wt"])  # new files count too (this index belongs to the task's worktree only)
            files = split_patch(git(m["wt"], "diff", "--cached", m["base"]))
            ahead = git(m["wt"], "rev-list", "--count", f"{m['base']}..HEAD", check=False).strip()
            return {"files": files, "branch": m["branch"], "commits": int(ahead or 0)}
        # local: what this task's edits changed, file by file since it first touched them
        first = {}
        for cp in me.Checkpoints(self.st.store, self.agent.session["id"]).load():
            for p, old in cp.get("files", {}).items():
                first.setdefault(p, old)
        files = []
        for p, old in first.items():
            d = me.file_diff(p, old)
            if not d.strip():
                continue
            rel = me.relp(p, m["cwd"]).replace("\\", "/")
            body = d.split("\n")
            files.append({"path": rel, "diff": d, "new": old is None, "deleted": not os.path.exists(p), "binary": False,
                          "add": sum(1 for l in body if l.startswith("+") and not l.startswith("+++")),
                          "del": sum(1 for l in body if l.startswith("-") and not l.startswith("---"))})
        return {"files": files, "note": "edits made by this task (commands it ran are not tracked in local mode)"}


class Studio:
    def __init__(self):
        self.store = me.Store()
        self.store.on_change = None
        self.cloud = me.Cloud(self.store)
        if self.cloud.online:
            self.store.on_change = self.cloud.autosync
        d = me.jload(STATE, {}) or {}
        self.projects = [p for p in d.get("projects", []) if os.path.isdir(p)]
        self.metas = d.get("tasks", {})
        for m in self.metas.values():
            if m.get("status") in ("running", "waiting"):  # the app closed mid-turn
                m["status"] = "done"
        self.tasks, self.lock, self._git = {}, threading.RLock(), {}

    def is_git(self, p):
        """Cached: the page polls the state every second or two, one git call per project each time is too much."""
        hit = self._git.get(p)
        if not hit or time.time() - hit[1] > 30:
            hit = self._git[p] = (bool(git_root(p)), time.time())
        return hit[0]

    def save(self):
        with self.lock:
            me.jsave(STATE, {"projects": self.projects, "tasks": self.metas})

    def task(self, tid):
        with self.lock:
            if tid not in self.tasks:
                if tid not in self.metas:
                    raise KeyError("no such task")
                self.tasks[tid] = Task(self, self.metas[tid])
            return self.tasks[tid]

    # projects
    def add_project(self, path):
        p = os.path.abspath(os.path.expanduser(str(path or "")))
        if not os.path.isdir(p):
            raise ValueError(f"no such folder: {p}")
        if p not in self.projects:
            self.projects.insert(0, p)
            self.save()
        return p

    def remove_project(self, path):
        self.projects = [p for p in self.projects if p != path]
        self.save()

    # tasks
    def new_task(self, project, prompt, mode="local", model="", effort=""):
        project = self.add_project(project)
        tid = time.strftime("%m%d%H%M%S") + uuid.uuid4().hex[:4]
        title = " ".join(prompt.split())[:70] or "task"
        meta = {"id": tid, "project": project, "title": title, "mode": mode, "cwd": project, "status": "idle", "created": time.time(),
                "updated": time.time(), "model": model, "effort": effort, "error": "", "changes": False}
        if mode == "worktree":
            root = git_root(project)
            if not root:
                raise ValueError("worktree needs a git repository — pick “local”, or run `git init` and commit once")
            base = git(root, "rev-parse", "HEAD", check=False).strip()
            if not base:
                raise ValueError("the repository has no commits yet: commit once, then a worktree can start from it")
            branch = f"me/{slug(title)}-{tid[-4:]}"
            wt = WORKTREES / f"{Path(root).name}-{hashlib.sha1(root.encode()).hexdigest()[:6]}" / tid
            wt.parent.mkdir(parents=True, exist_ok=True)
            git(root, "worktree", "add", "-b", branch, str(wt), base)
            # the project's place inside the repo, from git itself: on Windows `root` comes back long-named with "/" while the
            # project may be an 8.3 short path (C:\Users\RUNNER~1\…), and relpath between the two walks out of the worktree
            sub = git(project, "rev-parse", "--show-prefix", check=False).strip().strip("/")
            meta.update(root=root, base=base, branch=branch, wt=str(wt), cwd=str(wt / sub if sub else wt))
        with self.lock:
            self.metas[tid] = meta
        t = self.task(tid)
        self.save()
        t.send(prompt)
        return tid

    def apply(self, tid):
        """Worktree task → the project: its changes as a patch, applied with a 3-way merge onto your checkout."""
        m = self.metas[tid]
        if m["mode"] != "worktree":
            raise ValueError("this task already works in the project folder")
        stage(m["wt"])
        patch = git(m["wt"], "diff", "--cached", "--binary", m["base"])
        if not patch.strip():
            return "nothing to apply"
        r = subprocess.run(["git", "-C", m["root"], "apply", "--3way", "--whitespace=nowarn"], input=patch, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", **me.NO_WINDOW)
        if r.returncode:
            raise RuntimeError("could not apply cleanly: " + (r.stderr or r.stdout).strip()[:1200]
                               + "\nCommit or stash your own changes to those files, or commit the task and merge its branch.")
        m["applied"] = time.time()
        self.save()
        return f"applied {len(split_patch(patch))} file(s) to {m['root']} (staged)"

    def commit(self, tid, message):
        m = self.metas[tid]
        cwd = m["wt"] if m["mode"] == "worktree" else (git_root(m["cwd"]) or "")
        if not cwd:
            raise ValueError("not a git repository")
        stage(cwd)
        if not git(cwd, "diff", "--cached", "--name-only").strip():
            return "nothing to commit"
        who = [] if git(cwd, "config", "user.email", check=False).strip() else ["-c", "user.name=me", "-c", "user.email=me@localhost"]
        git(cwd, *who, "commit", "-m", message or m["title"])
        return "committed to " + (m.get("branch") or git(cwd, "branch", "--show-current").strip())

    def push(self, tid):
        m = self.metas[tid]
        cwd = m["wt"] if m["mode"] == "worktree" else (git_root(m["cwd"]) or m["cwd"])
        branch = m.get("branch") or git(cwd, "branch", "--show-current").strip()
        env = me.shell_env(self.store.config.get("secrets"))  # a GITHUB_TOKEN secret is enough for github.com
        git(cwd, "push", "-u", "origin", branch, env=env)
        return f"pushed {branch}"

    def discard(self, tid):
        m = self.metas[tid]
        t = self.tasks.get(tid)
        if t and t.busy:
            raise ValueError("stop the task first")
        if m["mode"] == "worktree":
            git(m["root"], "worktree", "remove", "--force", m["wt"], check=False)
            git(m["root"], "branch", "-D", m["branch"], check=False)
        m["archived"], m["status"] = True, "discarded"
        self.save()
        return "discarded" + (f" (worktree and branch {m['branch']} removed)" if m["mode"] == "worktree" else "")

    def state(self):
        cfg = self.store.config
        out = []
        for m in sorted(self.metas.values(), key=lambda m: m.get("updated", 0), reverse=True):
            t = self.tasks.get(m["id"])
            out.append(dict({k: m.get(k) for k in ("id", "project", "title", "mode", "status", "branch", "updated", "error", "changes",
                                                     "archived", "model", "effort")}, busy=bool(t and t.busy)))
        th = me.theme(self.store)
        return {"version": VERSION, "me": me.VERSION, "projects": [{"path": p, "name": Path(p).name or p, "git": self.is_git(p)}
                                                                    for p in self.projects],
                "tasks": out, "models": [{"name": x["name"], "id": x.get("id", ""), "effort": x.get("effort", "")} for x in cfg.get("models", [])],
                "model": cfg.get("model"), "perm": me.perm_mode(cfg["settings"]), "theme": th["name"], "dark": th.get("dark", True),
                "colors": th["colors"], "themes": list(me.themes(self.store)), "user": self.store.auth.get("user", ""),
                "lang": cfg["settings"].get("lang", ""), "home": str(Path.home()), "native": bool(getattr(self, "native", False))}

    def folders(self, path=""):
        """Subfolders of `path` (default: home) for the add-project browser; git repositories are marked."""
        p = Path(os.path.expanduser(path or "~")).resolve()
        if not p.is_dir():
            raise ValueError(f"no such folder: {p}")
        dirs = []
        try:
            for e in sorted(os.scandir(p), key=lambda e: e.name.lower()):
                if e.name.startswith(".") or e.name in me.SKIP_DIRS:
                    continue
                try:
                    if e.is_dir():
                        dirs.append({"name": e.name, "git": os.path.exists(os.path.join(e.path, ".git"))})
                except OSError:
                    continue
        except PermissionError:
            pass
        roots = []
        if me.WIN:
            import string
            roots = [f"{d}:\\" for d in string.ascii_uppercase if os.path.isdir(f"{d}:\\")]
        return {"path": str(p), "parent": str(p.parent) if p.parent != p else "", "git": (p / ".git").exists(), "dirs": dirs[:500], "roots": roots}


# ───────────────────────────── http ─────────────────────────────

def serve(studio, port=0):
    key = secrets.token_urlsafe(18)

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, data, ctype="application/json"):
            raw = data if isinstance(data, bytes) else json.dumps(data, ensure_ascii=False, default=str).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(raw)

        def _ok(self):
            host = self.headers.get("Host", "").rsplit(":", 1)[0].strip("[]")
            if host not in ("127.0.0.1", "localhost"):
                return False
            return self.headers.get("X-Key") == key or f"k={key}" in self.path

        def _q(self, name, default=""):
            import urllib.parse as up
            return up.parse_qs(up.urlparse(self.path).query).get(name, [default])[0]

        def do_GET(self):
            path = self.path.split("?")[0]
            if path in ("/favicon.ico", "/favicon.svg"):
                return self._send(200, me.ICON_SVG.encode(), "image/svg+xml")
            if path == "/":
                if not self._ok():
                    return self._send(401, b"open the link me studio printed", "text/plain")
                return self._send(200, PAGE.replace("__KEY__", key).encode(), "text/html; charset=utf-8")
            if not self._ok():
                return self._send(401, {"detail": "unauthorized"})
            try:
                if path == "/api/state":
                    return self._send(200, studio.state())
                if path == "/api/task":
                    t = studio.task(self._q("id"))
                    return self._send(200, {"meta": t.meta, "messages": t.agent.session["messages"], "seq": t.seq, "busy": t.busy,
                                            "pending": [w[2] for w in t.waits.values()], "todo": t.agent.session.get("todo") or []})
                if path == "/api/events":
                    t = studio.task(self._q("id"))
                    return self._send(200, {"events": t.wait_events(int(self._q("after", "0") or 0)), "busy": t.busy, "status": t.meta["status"]})
                if path == "/api/diff":
                    return self._send(200, studio.task(self._q("id")).diff())
                if path == "/api/fs":
                    return self._send(200, studio.folders(self._q("path")))
                return self._send(404, {"detail": "not found"})
            except Exception as e:
                return self._send(400, {"detail": str(e)})

        def do_POST(self):
            if not self._ok():
                return self._send(401, {"detail": "unauthorized"})
            path = self.path.split("?")[0]
            try:
                b = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                return self._send(200, route(studio, path, b))
            except Exception as e:
                return self._send(400, {"detail": str(e)})

    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_port}/?k={key}"


def route(studio, path, b):
    st = studio.store
    if path == "/api/project/add":
        return {"path": studio.add_project(b["path"])}
    if path == "/api/project/remove":
        studio.remove_project(b["path"])
        return {"ok": True}
    if path == "/api/task/new":
        if not (b.get("prompt") or "").strip():
            raise ValueError("describe the task first")
        return {"id": studio.new_task(b["project"], b["prompt"], b.get("mode") or "local", b.get("model") or "", b.get("effort") or "")}
    if path == "/api/settings":
        if b.get("model"):
            st.config["model"] = b["model"]
        if b.get("perm") in me.PERMS:
            st.config["settings"]["perm"], st.config["settings"]["yolo"] = b["perm"], False
        if b.get("theme"):
            st.config["theme"] = b["theme"]
        if "lang" in b:
            st.config["settings"]["lang"] = b["lang"] if b["lang"] in ("en", "ru") else ""
        st.save()
        return studio.state()
    if path == "/api/open" and b.get("path"):  # reveal a project folder
        return open_folder(b["path"])
    tid = b.get("id")
    t = studio.task(tid)
    if path == "/api/task/send":
        t.send(b["text"])
        return {"ok": True}
    if path == "/api/task/stop":
        t.stop()
        return {"ok": True}
    if path in ("/api/task/confirm", "/api/task/answer"):
        t.answer(b["rid"], b.get("ok") if path.endswith("confirm") else b.get("text"))
        return {"ok": True}
    if path == "/api/task/model":
        for k in ("model", "effort"):
            if k in b:
                t.meta[k] = b[k] or ""
                if b[k]:
                    t.agent.session[k] = b[k]
                else:
                    t.agent.session.pop(k, None)
        studio.save()
        return {"ok": True}
    if path == "/api/task/undo":
        r = t.agent.undo()
        if not r["ok"]:
            raise ValueError(r["text"])
        return r
    if path == "/api/task/apply":
        return {"text": studio.apply(tid)}
    if path == "/api/task/commit":
        return {"text": studio.commit(tid, b.get("message", ""))}
    if path == "/api/task/push":
        return {"text": studio.push(tid)}
    if path == "/api/task/discard":
        return {"text": studio.discard(tid)}
    if path == "/api/task/archive":
        t.meta["archived"] = not t.meta.get("archived")
        studio.save()
        return {"ok": True}
    if path == "/api/task/rename":
        t.meta["title"] = (b.get("title") or t.meta["title"])[:100]
        studio.save()
        return {"ok": True}
    if path == "/api/term":
        ok, out = me.run_shell(b["cmd"], int(b.get("timeout") or 120), threading.Event(), me.shell_env(st.config.get("secrets")),
                               cwd=t.meta["cwd"], pref=st.config["settings"].get("shell"))
        return {"ok": ok, "output": out}
    if path == "/api/open":  # reveal the task's folder in the file manager
        return open_folder(t.meta["cwd"])
    raise KeyError(path)


def open_folder(p):
    if not os.path.isdir(p):
        raise ValueError(f"no such folder: {p}")
    if me.WIN:
        os.startfile(p)  # noqa
    else:
        subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", p], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return {"ok": True}


# ───────────────────────────── window ─────────────────────────────

class Api:
    """Called from the page through pywebview (native-only extras)."""

    def __init__(self):
        self.window = None

    def pick_folder(self, start=""):
        import webview
        r = self.window.create_file_dialog(webview.FOLDER_DIALOG, directory=start or str(Path.home()))
        return (r[0] if isinstance(r, (list, tuple)) else r) if r else ""


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    studio = Studio()
    me.PLUGINS.load(studio.store)
    if studio.cloud.online:
        threading.Thread(target=studio.cloud.sync, daemon=True).start()
    port = int(argv[argv.index("--port") + 1]) if "--port" in argv else 0
    srv, url = serve(studio, port)
    webview = None
    if "--browser" not in argv:
        try:
            import webview  # pywebview
        except ImportError:
            webview = None
    if webview:
        studio.native = True
        api = Api()
        bg = studio.state()["colors"]["bg"]
        api.window = webview.create_window("me studio", url, js_api=api, width=1380, height=880, min_size=(360, 520), background_color=bg)
        webview.start(private_mode=False)
    else:
        print(f"me studio · {url}\n  (no pywebview: `pip install pywebview` for a native window) · ctrl+c to stop")
        try:
            import webbrowser
            if "--no-open" not in argv:
                webbrowser.open(url)
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass
    for t in studio.tasks.values():
        t.stop()
    studio.save()
    srv.shutdown()


PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>me studio</title>
<link rel="icon" href="/favicon.svg">
<style>
:root { --bg:#0e0f13; --bg2:#121319; --bg3:#181a22; --line:#22252f; --fg:#e2e4ec; --dim:#8a8ea0; --faint:#4a4e5c; --a:#8b7cff; --a2:#4fd1c5;
  --ok:#5ad18a; --err:#ff6b7a; --warn:#ffb454; --code:#15171e; --sel:#242836; --r:12px;
  --a10:rgba(139,124,255,.1); --a14:rgba(139,124,255,.14); --a30:rgba(139,124,255,.3); --a45:rgba(139,124,255,.45); --w8:rgba(255,180,84,.08); --w25:rgba(255,180,84,.25); --w45:rgba(255,180,84,.45); --ok10:rgba(90,209,138,.1); --err10:rgba(255,107,122,.1); --ok35:rgba(90,209,138,.35); --err35:rgba(255,107,122,.35);
  --mono: ui-monospace, "SF Mono", "Cascadia Code", "JetBrains Mono", Consolas, monospace;
  --sans: "Inter", system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }
* { box-sizing: border-box; }
html, body { margin: 0; height: 100%; background: var(--bg); color: var(--fg); font: 14px/1.55 var(--sans); -webkit-font-smoothing: antialiased; }
button, input, select, textarea { font: inherit; color: inherit; }
button { cursor: pointer; }
:focus-visible { outline: 2px solid var(--a); outline-offset: 1px; }
svg { width: 16px; height: 16px; fill: none; stroke: currentColor; stroke-width: 1.8; stroke-linecap: round; stroke-linejoin: round; flex: none; }
.hidden { display: none !important; }
::-webkit-scrollbar { width: 10px; height: 10px; } ::-webkit-scrollbar-thumb { background: var(--line); border-radius: 10px; border: 2px solid transparent; background-clip: padding-box; }
#app { display: grid; grid-template-columns: 272px minmax(0, 1fr) auto; height: 100vh; height: 100dvh; }

/* buttons */
.btn { display: inline-flex; align-items: center; justify-content: center; gap: 7px; height: 32px; padding: 0 12px; border: 1px solid var(--line);
  background: var(--bg3); border-radius: 9px; white-space: nowrap; transition: border-color .12s, background .12s; }
.btn:hover:not(:disabled) { border-color: var(--faint); }
.btn:disabled { opacity: .45; cursor: default; }
.btn.pri { background: var(--a); border-color: var(--a); color: #fff; font-weight: 600; }
.btn.pri:hover:not(:disabled) { filter: brightness(1.08); }
.btn.ghost { background: none; border-color: transparent; color: var(--dim); }
.btn.ghost:hover:not(:disabled) { background: var(--bg3); color: var(--fg); }
.btn.danger { color: var(--err); }
.btn.danger.solid { background: var(--err); border-color: var(--err); color: #fff; }
.btn.sm { height: 28px; padding: 0 10px; font-size: 13px; border-radius: 8px; }
.ib { display: inline-grid; place-items: center; width: 30px; height: 30px; border: 0; background: none; border-radius: 8px; color: var(--dim); }
.ib:hover { background: var(--bg3); color: var(--fg); }
.ib.on { color: var(--a); background: var(--a14); }
kbd { font: 11px var(--mono); color: var(--dim); border: 1px solid var(--line); border-bottom-width: 2px; border-radius: 5px; padding: 0 5px; }

/* sidebar */
#side { background: var(--bg2); border-right: 1px solid var(--line); display: flex; flex-direction: column; min-width: 0; }
.brand { display: flex; align-items: center; gap: 10px; padding: 14px 12px 10px 16px; }
.logo { width: 20px; height: 20px; border-radius: 6px; background: linear-gradient(135deg, var(--a), var(--a2)); flex: none; }
.brand b { font-size: 15px; letter-spacing: .1px; }
.brand .sp { flex: 1; }
.newtask { margin: 2px 12px 10px; height: 36px; justify-content: flex-start; gap: 10px; padding: 0 12px; }
.newtask kbd { margin-left: auto; }
.search { margin: 0 12px 6px; position: relative; }
.search input { width: 100%; height: 32px; border: 1px solid transparent; background: var(--bg3); border-radius: 8px; padding: 0 10px 0 30px; outline: 0; }
.search input:focus { border-color: var(--faint); }
.search svg { position: absolute; left: 9px; top: 8px; color: var(--dim); }
#projects { flex: 1; overflow: auto; padding: 2px 8px 12px; }
.proj { margin-top: 8px; }
.ph { display: flex; align-items: center; gap: 6px; height: 30px; padding: 0 6px 0 8px; color: var(--dim); font-size: 12.5px; border-radius: 8px; cursor: pointer; user-select: none; }
.ph:hover { background: var(--bg3); }
.ph .nm { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-weight: 600; color: var(--fg); opacity: .85; }
.ph .chev { transition: transform .15s; width: 12px; height: 12px; }
.proj.closed .ph .chev { transform: rotate(-90deg); }
.proj.closed .tl { display: none; }
.ph .acts { margin-left: auto; display: flex; opacity: 0; }
.ph:hover .acts, .ph:focus-within .acts { opacity: 1; }
.ph .acts .ib { width: 24px; height: 24px; }
.titem { display: flex; align-items: center; gap: 9px; width: 100%; text-align: left; border: 0; background: none; height: 32px; padding: 0 8px 0 12px; border-radius: 8px; min-width: 0; color: var(--fg); }
.titem:hover { background: var(--bg3); }
.titem.on { background: var(--sel); }
.titem .tt { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.titem .ago { color: var(--faint); font-size: 11.5px; flex: none; }
.titem .wt { color: var(--dim); flex: none; }
.empty-side { color: var(--dim); font-size: 12.5px; padding: 6px 12px 4px; line-height: 1.5; }
.sfoot { border-top: 1px solid var(--line); padding: 8px; display: flex; flex-direction: column; gap: 4px; }
.sfoot .btn { justify-content: flex-start; width: 100%; }
.who { display: flex; align-items: center; gap: 8px; padding: 4px 8px; color: var(--dim); font-size: 12px; }
.who .sp { flex: 1; }

/* status dots */
.st { width: 14px; height: 14px; display: inline-grid; place-items: center; flex: none; }
.st i { width: 7px; height: 7px; border-radius: 50%; background: var(--faint); }
.st.done i { background: var(--ok); }
.st.review i { background: var(--a); }
.st.error i { background: var(--err); }
.st.waiting i { background: var(--warn); box-shadow: 0 0 0 3px var(--w25); }
.st.discarded i { background: transparent; border: 1.5px solid var(--faint); }
.st.running i { width: 11px; height: 11px; background: none; border: 2px solid var(--a30); border-top-color: var(--a); animation: spin .8s linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }

/* main */
#main { display: flex; flex-direction: column; min-width: 0; min-height: 0; position: relative; }
.top { height: 52px; display: flex; align-items: center; gap: 10px; padding: 0 14px 0 18px; border-bottom: 1px solid var(--line); flex: none; min-width: 0; }
.top .ttl { display: flex; align-items: baseline; gap: 10px; min-width: 0; flex: 1; }
.top .ttl b { font-size: 14.5px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; cursor: text; border-radius: 6px; padding: 2px 4px; margin-left: -4px; }
.top .ttl b:hover { background: var(--bg3); }
.top .ttl input { font-weight: 600; font-size: 14.5px; background: var(--bg3); border: 1px solid var(--faint); border-radius: 6px; padding: 2px 6px; outline: 0; min-width: 240px; }
.meta { display: flex; align-items: center; gap: 6px; color: var(--dim); font-size: 12.5px; white-space: nowrap; overflow: hidden; }
.meta .sep { color: var(--faint); }
.pill { display: inline-flex; align-items: center; gap: 6px; height: 24px; padding: 0 9px; border-radius: 99px; font-size: 12px; background: var(--bg3); color: var(--dim); border: 1px solid var(--line); white-space: nowrap; }
.pill.running { color: var(--a); border-color: var(--a45); }
.pill.waiting { color: var(--warn); border-color: var(--w45); }
.pill.review { color: var(--a); }
.pill.done { color: var(--ok); }
.pill.error { color: var(--err); }
.acts { display: flex; align-items: center; gap: 4px; flex: none; }
.chg { font-variant-numeric: tabular-nums; }
.add { color: var(--ok); } .del { color: var(--err); }

/* home */
#home { flex: 1; overflow: auto; }
.hero { max-width: 760px; margin: 0 auto; padding: 12vh 24px 40px; }
.hero h1 { font-size: 28px; line-height: 1.2; margin: 0 0 6px; letter-spacing: -.3px; font-weight: 650; }
.hero p.sub { color: var(--dim); margin: 0 0 22px; }
.composer { border: 1px solid var(--line); background: var(--bg2); border-radius: 16px; padding: 12px 12px 10px; transition: border-color .15s, box-shadow .15s; }
.composer:focus-within { border-color: var(--a45); box-shadow: 0 0 0 4px var(--a10); }
.composer textarea { width: 100%; resize: none; border: 0; background: none; outline: 0; min-height: 52px; max-height: 40vh; padding: 2px 4px; line-height: 1.55; display: block; }
.crow { display: flex; align-items: center; gap: 6px; margin-top: 8px; flex-wrap: wrap; }
.crow .sp { flex: 1; }
.chip { display: inline-flex; align-items: center; gap: 6px; height: 30px; padding: 0 10px; border-radius: 8px; border: 1px solid var(--line); background: var(--bg3);
  color: var(--fg); font-size: 13px; max-width: 260px; }
.chip span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.chip:hover { border-color: var(--faint); }
.chip .cv { width: 12px; height: 12px; color: var(--dim); }
.chip.warn { border-style: dashed; color: var(--warn); }
.seg { display: inline-flex; border: 1px solid var(--line); background: var(--bg3); border-radius: 8px; padding: 2px; height: 30px; }
.seg button { border: 0; background: none; border-radius: 6px; padding: 0 9px; display: inline-flex; align-items: center; gap: 5px; color: var(--dim); font-size: 13px; }
.seg button.on { background: var(--sel); color: var(--fg); }
.seg button:disabled { opacity: .4; cursor: default; }
.send { width: 34px; height: 34px; border-radius: 10px; border: 0; background: var(--a); color: #fff; display: grid; place-items: center; }
.send:disabled { opacity: .4; cursor: default; }
.send.stop { background: var(--fg); color: var(--bg); }
.hint { color: var(--faint); font-size: 12px; margin: 8px 4px 0; display: flex; gap: 12px; flex-wrap: wrap; }
.recent { margin-top: 34px; }
.recent h3 { font-size: 12px; color: var(--dim); text-transform: uppercase; letter-spacing: .6px; margin: 0 0 8px 4px; font-weight: 600; }
.rc { display: flex; align-items: center; gap: 10px; width: 100%; border: 0; background: none; text-align: left; padding: 9px 10px; border-radius: 10px; }
.rc:hover { background: var(--bg2); }
.rc .tt { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.rc .m { color: var(--dim); font-size: 12px; white-space: nowrap; }
.starters { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 14px; }
.starters button { border: 1px solid var(--line); background: none; border-radius: 99px; padding: 5px 12px; color: var(--dim); font-size: 12.5px; }
.starters button:hover { color: var(--fg); border-color: var(--faint); }
.onboard { border: 1px dashed var(--line); border-radius: 14px; padding: 18px; display: flex; gap: 14px; align-items: center; margin-bottom: 16px; }
.onboard .tx { flex: 1; color: var(--dim); }
.onboard .tx b { color: var(--fg); display: block; margin-bottom: 2px; }

/* task view */
#task { flex: 1; display: flex; flex-direction: column; min-height: 0; }
#log { flex: 1; overflow: auto; scroll-behavior: auto; }
.lw { max-width: 820px; margin: 0 auto; padding: 22px 24px 30px; display: flex; flex-direction: column; gap: 12px; }
.msg.user { align-self: flex-end; max-width: 85%; }
.msg.user .bub { background: var(--bg3); border: 1px solid var(--line); border-radius: 14px 14px 4px 14px; padding: 9px 13px; white-space: pre-wrap; word-break: break-word; }
.msg.user .tag { display: block; text-align: right; color: var(--warn); font-size: 11.5px; margin-bottom: 3px; }
.md { word-break: break-word; }
.md > :first-child { margin-top: 0; } .md > :last-child { margin-bottom: 0; }
.md p { margin: 0 0 10px; } .md h1, .md h2, .md h3 { margin: 16px 0 8px; line-height: 1.3; } .md h1 { font-size: 19px; } .md h2 { font-size: 17px; } .md h3 { font-size: 15px; }
.md ul, .md ol { margin: 0 0 10px; padding-left: 22px; } .md li { margin: 2px 0; }
.md code { font: 12.5px var(--mono); background: var(--code); border: 1px solid var(--line); border-radius: 5px; padding: 1px 5px; }
.md pre { background: var(--code); border: 1px solid var(--line); border-radius: 10px; margin: 0 0 10px; overflow: hidden; }
.md pre .bar { display: flex; justify-content: space-between; align-items: center; padding: 4px 6px 4px 12px; color: var(--dim); font-size: 12px; border-bottom: 1px solid var(--line); }
.md pre .bar button { border: 0; background: none; color: var(--dim); font-size: 12px; border-radius: 6px; padding: 2px 8px; }
.md pre .bar button:hover { background: var(--bg3); color: var(--fg); }
.md pre code { display: block; border: 0; background: none; padding: 10px 12px; overflow-x: auto; white-space: pre; }
.md blockquote { margin: 0 0 10px; padding-left: 12px; border-left: 3px solid var(--line); color: var(--dim); }
.md a { color: var(--a); }
.md table { border-collapse: collapse; margin: 0 0 10px; font-size: 13px; } .md td, .md th { border: 1px solid var(--line); padding: 4px 8px; }
.think { color: var(--dim); font-size: 13px; }
.think summary { cursor: pointer; list-style: none; display: inline-flex; gap: 6px; align-items: center; user-select: none; }
.think summary::-webkit-details-marker { display: none; }
.think pre { white-space: pre-wrap; font: 12.5px/1.5 var(--sans); margin: 6px 0 0 4px; padding-left: 12px; border-left: 2px solid var(--line); max-height: 260px; overflow: auto; }
.shimmer { background: linear-gradient(90deg, var(--dim) 0%, var(--fg) 50%, var(--dim) 100%); background-size: 200% 100%; -webkit-background-clip: text; background-clip: text; color: transparent; animation: sh 1.6s linear infinite; }
@keyframes sh { from { background-position: 200% 0; } to { background-position: -200% 0; } }
.tool { border: 1px solid var(--line); border-radius: 10px; background: var(--bg2); overflow: hidden; }
.tool .h { display: flex; align-items: center; gap: 8px; padding: 6px 10px; cursor: pointer; user-select: none; font-size: 13px; min-height: 34px; }
.tool .h:hover { background: var(--bg3); }
.tool .h .nm { font-weight: 600; flex: none; }
.tool .h .lab { color: var(--dim); font-family: var(--mono); font-size: 12.5px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; flex: 1; min-width: 0; }
.tool .h .t { color: var(--faint); font-size: 12px; flex: none; }
.tool .h .ch { width: 12px; height: 12px; color: var(--faint); transition: transform .15s; }
.tool:not(.closed) .h .ch { transform: rotate(90deg); }
.tool .stl { padding: 0 10px 6px 34px; color: var(--dim); font-size: 12px; font-family: var(--mono); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.tool .body { border-top: 1px solid var(--line); margin: 0; padding: 8px 12px; max-height: 360px; overflow: auto; font: 12px/1.5 var(--mono); white-space: pre-wrap; word-break: break-word; color: var(--dim); background: var(--code); }
.tool.closed .body { display: none; }
.tool.err { border-color: var(--err35); }
.ic { width: 16px; height: 16px; display: grid; place-items: center; flex: none; }
.spinner { width: 12px; height: 12px; border-radius: 50%; border: 2px solid var(--a30); border-top-color: var(--a); animation: spin .8s linear infinite; }
.dl { display: block; } .dl.a { color: var(--ok); background: var(--ok10); } .dl.d { color: var(--err); background: var(--err10); } .dl.h { color: var(--a2); }
.card { border: 1px solid var(--w45); background: var(--w8); border-radius: 12px; padding: 12px 14px; }
.card .q { font-weight: 600; margin-bottom: 8px; display: flex; gap: 8px; align-items: center; }
.card pre { margin: 0 0 10px; background: var(--code); border: 1px solid var(--line); border-radius: 8px; padding: 8px 10px; font: 12px/1.5 var(--mono); white-space: pre-wrap; max-height: 220px; overflow: auto; }
.card .btns { display: flex; gap: 6px; flex-wrap: wrap; }
.card input { flex: 1; min-width: 160px; height: 28px; border: 1px solid var(--line); border-radius: 8px; background: var(--bg); padding: 0 9px; outline: 0; }
.card.done { border-color: var(--line); background: none; color: var(--dim); padding: 6px 12px; font-size: 13px; }
.note { color: var(--dim); font-size: 12.5px; display: flex; align-items: center; gap: 8px; }
.note.err { color: var(--err); } .note.warn { color: var(--warn); }
.note a { color: var(--a); cursor: pointer; }
.turnend { display: flex; align-items: center; gap: 10px; color: var(--faint); font-size: 12px; }
.turnend::before, .turnend::after { content: ""; flex: 1; height: 1px; background: var(--line); }
.plan { position: sticky; top: 0; z-index: 2; margin: 0 auto; max-width: 820px; width: 100%; padding: 10px 24px 0; }
.plan .in { background: var(--bg2); border: 1px solid var(--line); border-radius: 12px; padding: 8px 12px; box-shadow: 0 6px 20px rgba(0,0,0,.15); }
.plan .ph2 { display: flex; align-items: center; gap: 8px; font-size: 12.5px; color: var(--dim); cursor: pointer; user-select: none; }
.plan .ph2 b { color: var(--fg); }
.plan .bar2 { flex: 1; height: 4px; border-radius: 4px; background: var(--line); overflow: hidden; } .plan .bar2 i { display: block; height: 100%; background: var(--a); }
.plan .items { margin-top: 6px; display: grid; gap: 2px; font-size: 13px; }
.plan.closed .items { display: none; }
.plan .it { display: flex; gap: 8px; align-items: baseline; } .plan .it.d { color: var(--dim); text-decoration: line-through; text-decoration-color: var(--faint); } .plan .it.g { color: var(--a); }
.foot { padding: 8px 24px 16px; flex: none; }
.foot .composer { max-width: 820px; margin: 0 auto; padding: 10px 10px 8px 12px; }
.foot .composer textarea { min-height: 24px; }
.foot .crow { margin-top: 4px; }
.mini { font-size: 12px; color: var(--dim); display: flex; gap: 6px; align-items: center; }
.mini select { height: 26px; border: 1px solid transparent; background: none; border-radius: 6px; color: var(--dim); font-size: 12.5px; padding: 0 4px; max-width: 170px; }
.mini select:hover { border-color: var(--line); color: var(--fg); }

/* side panel */
#panel { width: min(560px, 46vw); border-left: 1px solid var(--line); background: var(--bg2); display: flex; flex-direction: column; min-height: 0; }
.ptop { height: 52px; display: flex; align-items: center; gap: 4px; padding: 0 8px 0 10px; border-bottom: 1px solid var(--line); flex: none; }
.ptabs { display: flex; gap: 2px; flex: 1; }
.ptab { border: 0; background: none; height: 30px; padding: 0 12px; border-radius: 8px; color: var(--dim); display: inline-flex; gap: 6px; align-items: center; }
.ptab.on { background: var(--bg3); color: var(--fg); }
#pbody { flex: 1; overflow: auto; min-height: 0; }
.rv-head { padding: 12px 14px; border-bottom: 1px solid var(--line); display: grid; gap: 10px; }
.rv-sum { display: flex; align-items: center; gap: 8px; font-size: 13px; color: var(--dim); flex-wrap: wrap; }
.rv-sum b { color: var(--fg); }
.rv-acts { display: flex; gap: 6px; flex-wrap: wrap; }
.commit { display: flex; gap: 6px; }
.commit input { flex: 1; height: 30px; border: 1px solid var(--line); background: var(--bg); border-radius: 8px; padding: 0 10px; outline: 0; min-width: 0; }
.commit input:focus { border-color: var(--faint); }
.fitem { border-bottom: 1px solid var(--line); }
.fitem .fh { display: flex; align-items: center; gap: 8px; padding: 8px 14px; cursor: pointer; font-size: 13px; position: sticky; top: 0; background: var(--bg2); z-index: 1; }
.fitem .fh:hover { background: var(--bg3); }
.fitem .fh .p { flex: 1; font-family: var(--mono); font-size: 12.5px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; direction: rtl; text-align: left; }
.fitem .fh .ch { width: 12px; height: 12px; color: var(--faint); transition: transform .15s; }
.fitem.open .fh .ch { transform: rotate(90deg); }
.fitem pre { margin: 0; padding: 6px 0; font: 12px/1.55 var(--mono); background: var(--code); overflow-x: auto; display: none; }
.fitem.open pre { display: block; }
.fitem pre .dl { padding: 0 14px; white-space: pre; }
.tag { font-size: 11px; border-radius: 5px; padding: 0 5px; border: 1px solid var(--line); color: var(--dim); }
.tag.new { color: var(--ok); border-color: var(--ok35); } .tag.gone { color: var(--err); }
.empty { color: var(--dim); text-align: center; padding: 48px 20px; font-size: 13px; display: grid; gap: 8px; place-items: center; }
.empty svg { width: 28px; height: 28px; color: var(--faint); }
.term { display: flex; flex-direction: column; height: 100%; }
#termOut { flex: 1; overflow: auto; margin: 0; padding: 10px 14px; font: 12.5px/1.5 var(--mono); white-space: pre-wrap; word-break: break-word; background: var(--code); }
#termOut .cmd { color: var(--a2); } #termOut .bad { color: var(--err); }
.termin { display: flex; gap: 6px; padding: 8px; border-top: 1px solid var(--line); align-items: center; }
.termin span { color: var(--a2); font-family: var(--mono); }
.termin input { flex: 1; height: 32px; border: 1px solid var(--line); background: var(--bg); border-radius: 8px; padding: 0 10px; font-family: var(--mono); outline: 0; }

/* popover menu, modal, toast */
.menu { position: fixed; z-index: 50; min-width: 220px; max-width: 360px; max-height: 60vh; overflow: auto; background: var(--bg2); border: 1px solid var(--line); border-radius: 12px; padding: 5px; box-shadow: 0 14px 40px rgba(0,0,0,.35); }
.menu .mi { display: flex; align-items: center; gap: 9px; width: 100%; border: 0; background: none; text-align: left; padding: 7px 9px; border-radius: 8px; font-size: 13px; }
.menu .mi:hover, .menu .mi.kb { background: var(--bg3); }
.menu .mi .d { color: var(--dim); font-size: 12px; margin-left: auto; padding-left: 10px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 160px; }
.menu .mi .ck { width: 14px; color: var(--a); }
.menu .mi.danger { color: var(--err); }
.menu hr { border: 0; border-top: 1px solid var(--line); margin: 4px 2px; }
.menu .lbl { color: var(--faint); font-size: 11.5px; padding: 6px 9px 3px; text-transform: uppercase; letter-spacing: .5px; }
#modal { position: fixed; inset: 0; z-index: 60; background: rgba(0,0,0,.45); display: grid; place-items: center; padding: 16px; }
.dlg { width: min(560px, 100%); max-height: 86vh; display: flex; flex-direction: column; background: var(--bg2); border: 1px solid var(--line); border-radius: 16px; box-shadow: 0 24px 60px rgba(0,0,0,.4); overflow: hidden; }
.dlg .dh { display: flex; align-items: center; padding: 14px 16px 10px; gap: 10px; } .dlg .dh b { font-size: 15px; flex: 1; }
.dlg .db { padding: 0 16px 14px; overflow: auto; display: grid; gap: 12px; }
.dlg .df { display: flex; justify-content: flex-end; gap: 8px; padding: 12px 16px; border-top: 1px solid var(--line); }
.dlg label.f { display: grid; gap: 5px; font-size: 12.5px; color: var(--dim); }
.dlg input[type=text], .dlg select { height: 34px; border: 1px solid var(--line); background: var(--bg); border-radius: 9px; padding: 0 10px; outline: 0; color: var(--fg); }
.dlg input[type=text]:focus, .dlg select:focus { border-color: var(--faint); }
.fs { border: 1px solid var(--line); border-radius: 10px; overflow: hidden; }
.fs .fsh { display: flex; align-items: center; gap: 6px; padding: 6px; border-bottom: 1px solid var(--line); background: var(--bg3); }
.fs .fsh input { flex: 1; height: 28px; border: 1px solid var(--line); background: var(--bg); border-radius: 7px; padding: 0 8px; font-family: var(--mono); font-size: 12.5px; outline: 0; min-width: 0; }
.fs .list { max-height: 300px; overflow: auto; padding: 4px; }
.fs .row { display: flex; align-items: center; gap: 8px; width: 100%; border: 0; background: none; text-align: left; padding: 6px 8px; border-radius: 7px; font-size: 13px; }
.fs .row:hover { background: var(--bg3); }
.fs .row .g { margin-left: auto; color: var(--a); font-size: 11.5px; }
.fs .row.sel { background: var(--a14); }
.chips2 { display: flex; gap: 6px; flex-wrap: wrap; }
.chips2 button { border: 1px solid var(--line); background: none; border-radius: 99px; padding: 3px 10px; font-size: 12px; color: var(--dim); max-width: 220px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.chips2 button:hover { color: var(--fg); border-color: var(--faint); }
.opt { display: flex; gap: 6px; flex-wrap: wrap; }
.opt button { border: 1px solid var(--line); background: var(--bg); border-radius: 9px; padding: 7px 12px; font-size: 13px; text-align: left; display: grid; gap: 1px; min-width: 140px; flex: 1; }
.opt button small { color: var(--dim); font-size: 11.5px; }
.opt button.on { border-color: var(--a); background: var(--a10); }
#toast { position: fixed; bottom: 18px; left: 50%; transform: translateX(-50%); z-index: 70; display: grid; gap: 8px; justify-items: center; pointer-events: none; }
#toast div { background: var(--fg); color: var(--bg); padding: 9px 14px; border-radius: 10px; font-size: 13px; max-width: min(640px, 90vw); box-shadow: 0 8px 24px rgba(0,0,0,.3); white-space: pre-wrap; pointer-events: auto; }
#toast div.err { background: var(--err); color: #fff; }
#toast div.ok { background: var(--ok); color: #06210f; }
#scrim { display: none; }
.mob { display: none !important; }

@media (max-width: 1100px) { #panel { position: fixed; right: 0; top: 0; bottom: 0; width: min(560px, 92vw); z-index: 40; box-shadow: -20px 0 50px rgba(0,0,0,.35); } }
@media (max-width: 760px) {
  #app { grid-template-columns: 1fr; }
  #side { position: fixed; inset: 0 auto 0 0; width: 84vw; max-width: 320px; z-index: 45; transform: translateX(-105%); transition: transform .2s; }
  #side.open { transform: none; }
  #scrim.on { display: block; position: fixed; inset: 0; background: rgba(0,0,0,.4); z-index: 44; }
  .mob { display: inline-grid !important; }
  .wide { display: none !important; }
  .hero { padding-top: 6vh; }
  .lw, .foot { padding-left: 12px; padding-right: 12px; }
  .plan { padding: 8px 12px 0; }
  .top { padding: 0 8px; }
  #panel { width: 100vw; }
}
</style>
</head>
<body>
<div id="app">
  <aside id="side">
    <div class="brand"><span class="logo"></span><b>me studio</b><span class="sp"></span>
      <button class="ib" onclick="openSettings()" data-i="title:Settings" title="Settings"><svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z"/></svg></button>
      <button class="ib mob" onclick="closeSide()" aria-label="close"><svg viewBox="0 0 24 24"><path d="M6 6l12 12M18 6L6 18"/></svg></button></div>
    <button class="btn newtask" onclick="go('')"><svg viewBox="0 0 24 24"><path d="M12 20h9M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z"/></svg><span data-i>New task</span><kbd>Ctrl N</kbd></button>
    <div class="search"><svg viewBox="0 0 24 24"><circle cx="11" cy="11" r="7"/><path d="M20 20l-3.5-3.5"/></svg><input id="q" placeholder="Search tasks" data-i="ph:Search tasks" oninput="renderSide()"></div>
    <div id="projects"></div>
    <div class="sfoot">
      <button class="btn ghost" onclick="addProject()"><svg viewBox="0 0 24 24"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2zM12 11v5M9.5 13.5h5"/></svg><span data-i>Add project</span></button>
      <div class="who"><span id="who"></span><span class="sp"></span><span id="ver"></span></div>
    </div>
  </aside>
  <div id="scrim" onclick="closeSide()"></div>
  <main id="main">
    <div class="top">
      <button class="ib mob" onclick="openSide()" aria-label="menu"><svg viewBox="0 0 24 24"><path d="M4 7h16M4 12h16M4 17h16"/></svg></button>
      <div class="ttl" id="ttl"></div>
      <div class="acts" id="acts"></div>
    </div>
    <div id="home"></div>
    <div id="task" class="hidden"></div>
  </main>
  <aside id="panel" class="hidden">
    <div class="ptop"><div class="ptabs">
      <button class="ptab" data-tab="changes" onclick="panelTab('changes')"><svg viewBox="0 0 24 24"><path d="M12 3v12M6 9h12M6 21h12"/></svg><span data-i>Changes</span></button>
      <button class="ptab" data-tab="term" onclick="panelTab('term')"><svg viewBox="0 0 24 24"><path d="M4 6l6 6-6 6M12 18h8"/></svg><span data-i>Terminal</span></button></div>
      <button class="ib" onclick="closePanel()" aria-label="close"><svg viewBox="0 0 24 24"><path d="M6 6l12 12M18 6L6 18"/></svg></button></div>
    <div id="pbody"></div>
  </aside>
</div>
<div id="modal" class="hidden" onclick="if (event.target === this) closeModal()"></div>
<div id="toast"></div>
<script>
// me studio — UI. Talks to me_studio.py over /api/* with the per-run key; tasks stream events by long-poll.
const KEY = "__KEY__";
const $ = id => document.getElementById(id);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"})[c]);
const human = n => !n ? "0" : n < 1000 ? String(n) : n < 1e6 ? (n / 1000).toFixed(1).replace(".0", "") + "k" : (n / 1e6).toFixed(1).replace(".0", "") + "M";
const dur = s => s < 60 ? s.toFixed(1) + (L() ? " с" : "s") : Math.floor(s / 60) + (L() ? " мин " : "m ") + String(Math.floor(s % 60)).padStart(2, "0") + (L() ? " с" : "s");
const base = p => String(p || "").split(/[\\/]/).filter(Boolean).pop() || p;

// ─── i18n: English source, Russian table ───
let LANG = "en";
const L = () => LANG === "ru";
const RU = {
  "New task": "Новая задача", "Search tasks": "Поиск задач", "Add project": "Добавить проект", "Settings": "Настройки", "Changes": "Изменения", "Terminal": "Терминал",
  "What should we work on?": "Над чем работаем?", "Describe a task — fix a bug, add a feature, explain the code…": "Опишите задачу — исправить баг, добавить функцию, объяснить код…",
  "Each task is its own agent. Start several — they run side by side.": "Каждая задача — отдельный агент. Запускайте несколько — они работают параллельно.",
  "Local": "Локально", "Worktree": "Worktree", "Start": "Начать", "Recent": "Недавние", "Choose a project": "Выберите проект", "no project": "нет проекта",
  "Works in the project folder itself": "Работает прямо в папке проекта", "Its own git worktree and branch: your checkout stays untouched until you apply": "Свой git worktree и ветка: ваша папка не меняется, пока вы не примените результат",
  "needs a git repository with a commit": "нужен git-репозиторий с коммитом", "default model": "модель по умолчанию", "effort": "effort",
  "Start by adding a project": "Начните с добавления проекта", "Pick a folder with your code. Git repositories can run tasks in isolated worktrees.": "Выберите папку с кодом. В git-репозиториях задачи могут работать в отдельных worktree.",
  "enter to start": "enter — начать", "shift+enter new line": "shift+enter — новая строка", "Ask for follow-up changes": "Попросите доработать или задайте вопрос",
  "Steer the running task — it reads this after the current step": "Подскажите задаче — она прочтёт это после текущего шага",
  "working": "работает", "waiting for you": "ждёт вас", "ready to review": "можно смотреть", "done": "готово", "error": "ошибка", "idle": "ожидание", "discarded": "отброшена",
  "Stop": "Стоп", "Review": "Ревью", "Open folder": "Открыть папку", "Archive": "В архив", "Unarchive": "Из архива", "Rename": "Переименовать", "Discard…": "Отбросить…",
  "thinking…": "думаю…", "thought for {}": "думал {}", "Allow": "Разрешить", "Always allow this command": "Всегда разрешать эту команду", "Allow everything (full access)": "Разрешать всё (полный доступ)",
  "Deny": "Запретить", "allowed": "разрешено", "always allowed": "разрешено всегда", "denied": "запрещено", "Send": "Отправить", "answer…": "ответ…",
  "needs your approval": "нужно ваше разрешение", "queued": "в очереди", "while working": "во время работы", "retrying": "повторяю", "compacting history…": "сжимаю историю…",
  "history compacted · ~{} tokens": "история сжата · ~{} токенов", "self-check: running checks before finishing": "самопроверка: запускаю проверки перед завершением",
  "{} files changed": "изменено файлов: {}", "1 file changed": "изменён 1 файл", "review": "смотреть", "Worked {}": "Работал {}", "{} steps": "шагов: {}", "1 step": "1 шаг",
  "Plan": "План", "1 file": "1 файл", "{} files": "файлов: {}", "Apply to project": "Применить к проекту", "Commit": "Коммит", "Push branch": "Запушить ветку", "Undo last turn": "Откатить последний ход",
  "Commit message": "Сообщение коммита", "No changes yet": "Пока без изменений", "The agent's edits show up here as it works.": "Правки агента появятся здесь по ходу работы.",
  "branch": "ветка", "commits": "коммитов", "new": "новый", "deleted": "удалён", "binary file": "бинарный файл", "Run a command in the task folder": "Команда в папке задачи",
  "Cancel": "Отмена", "Add": "Добавить", "Save": "Сохранить", "Close": "Закрыть", "Project folder": "Папка проекта", "Recent folders": "Недавние папки", "Browse…": "Обзор…",
  "git repository": "git-репозиторий", "Up": "Вверх", "Use this folder": "Выбрать эту папку", "Theme": "Тема", "Language": "Язык", "Permissions": "Разрешения", "Default model": "Модель по умолчанию",
  "Show archived tasks": "Показывать архивные задачи", "ask": "спрашивать", "full access": "полный доступ", "read-only": "только чтение",
  "edits in the project are free; commands ask": "правки в проекте без вопросов; команды спрашивают", "everything runs without asking": "всё без вопросов", "no file changes": "без изменения файлов",
  "Discard this task?": "Отбросить задачу?", "Its worktree and branch {} are deleted. This cannot be undone.": "Её worktree и ветка {} будут удалены. Это нельзя отменить.",
  "The task is removed from the list; files it changed stay as they are.": "Задача уйдёт из списка; изменённые файлы останутся как есть.", "Discard": "Отбросить",
  "Remove project": "Убрать проект", "New task here": "Новая задача здесь", "no tasks yet": "задач пока нет", "Task name": "Название задачи", "copy": "копировать", "copied": "скопировано",
  "now": "сейчас", "{}m": "{} мин", "{}h": "{} ч", "{}d": "{} дн", "no models — add one with `me` → /models": "нет моделей — добавьте в `me` → /models", "offline": "офлайн",
  "Fix the failing tests": "Почини падающие тесты", "Explain how this project is structured": "Объясни, как устроен проект", "Review my uncommitted changes": "Сделай ревью моих незакоммиченных изменений",
  "Add a README with setup steps": "Добавь README с инструкцией по запуску", "Shortcuts": "Горячие клавиши", "search": "поиск", "stop the task": "остановить задачу",
  "Pick a project folder": "Выберите папку проекта", "loading…": "загрузка…", "Native folder picker": "Системный выбор папки", "Model": "Модель", "default": "по умолчанию",
  "no such folder": "нет такой папки", "Nothing to show yet": "Пока пусто", "Ask in Russian or English — the agent answers in your language.": "Пишите на любом языке — агент ответит на нём же.",
};
const t = (s, ...v) => { let r = L() && RU[s] !== undefined ? RU[s] : s; for (const x of v) r = r.replace("{}", x); return r; };
function i18nApply(root = document) {
  root.querySelectorAll("[data-i]").forEach(n => {
    const spec = n.dataset.i;
    if (!n.dataset.src) n.dataset.src = spec ? "" : n.textContent;
    if (spec.startsWith("ph:")) n.placeholder = t(spec.slice(3)); else if (spec.startsWith("title:")) n.title = t(spec.slice(6)); else n.textContent = t(n.dataset.src);
  });
}
const ago = ts => { const s = Date.now() / 1000 - ts; return s < 60 ? t("now") : s < 3600 ? t("{}m", Math.floor(s / 60)) : s < 86400 ? t("{}h", Math.floor(s / 3600)) : t("{}d", Math.floor(s / 86400)); };

const I = {
  check: '<svg viewBox="0 0 24 24" style="color:var(--ok)"><path d="M5 12l5 5 9-11"/></svg>',
  x: '<svg viewBox="0 0 24 24" style="color:var(--err)"><path d="M6 6l12 12M18 6L6 18"/></svg>',
  stop: '<svg viewBox="0 0 24 24"><rect x="7" y="7" width="10" height="10" rx="2" fill="currentColor" stroke="none"/></svg>',
  up: '<svg viewBox="0 0 24 24"><path d="M12 19V5M5 12l7-7 7 7"/></svg>',
  diff: '<svg viewBox="0 0 24 24"><path d="M12 3v12M6 9h12M6 21h12"/></svg>',
  term: '<svg viewBox="0 0 24 24"><path d="M4 6l6 6-6 6M12 18h8"/></svg>',
  folder: '<svg viewBox="0 0 24 24"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg>',
  branch: '<svg viewBox="0 0 24 24"><circle cx="6" cy="6" r="2"/><circle cx="6" cy="18" r="2"/><circle cx="18" cy="8" r="2"/><path d="M6 8v8M18 10c0 4-6 3-12 6"/></svg>',
  chev: '<svg viewBox="0 0 24 24" class="cv"><path d="M6 9l6 6 6-6"/></svg>',
  right: '<svg viewBox="0 0 24 24" class="ch"><path d="M9 6l6 6-6 6"/></svg>',
  dots: '<svg viewBox="0 0 24 24"><circle cx="5" cy="12" r="1.3" fill="currentColor"/><circle cx="12" cy="12" r="1.3" fill="currentColor"/><circle cx="19" cy="12" r="1.3" fill="currentColor"/></svg>',
  plus: '<svg viewBox="0 0 24 24"><path d="M12 5v14M5 12h14"/></svg>',
  cpu: '<svg viewBox="0 0 24 24"><rect x="6" y="6" width="12" height="12" rx="2"/><path d="M9 2v4M15 2v4M9 18v4M15 18v4M2 9h4M2 15h4M18 9h4M18 15h4"/></svg>',
  warn: '<svg viewBox="0 0 24 24" style="color:var(--warn)"><path d="M12 3l9 16H3zM12 10v4M12 17v.5"/></svg>',
  q: '<svg viewBox="0 0 24 24" style="color:var(--warn)"><circle cx="12" cy="12" r="9"/><path d="M9.5 9.5a2.5 2.5 0 1 1 3.5 2.3c-.6.3-1 .9-1 1.6M12 17v.5"/></svg>',
  file: '<svg viewBox="0 0 24 24"><path d="M14 3H6a1 1 0 0 0-1 1v16a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1V8z"/><path d="M14 3v5h5"/></svg>',
};
const TOOL_VERB = {read: "Read", write: "Write", edit: "Edit", bash: "Run", ls: "List", grep: "Search", web_search: "Web search", web_fetch: "Fetch", skill: "Skill",
  todo: "Plan", task: "Subagent", memory: "Memory", mcp: "MCP", notify: "Notify"};
const TOOL_RU = {Read: "Чтение", Write: "Запись", Edit: "Правка", Run: "Команда", List: "Файлы", Search: "Поиск", "Web search": "Веб-поиск", Fetch: "Загрузка", Skill: "Навык",
  Plan: "План", Subagent: "Субагент", Memory: "Память", MCP: "MCP", Notify: "Уведомление"};
const verb = n => { const v = TOOL_VERB[n] || n; return L() ? (TOOL_RU[v] || v) : v; };

// ─── api ───
async function api(path, body) {
  const r = await fetch(path, {method: body === undefined ? "GET" : "POST", headers: {"Content-Type": "application/json", "X-Key": KEY},
                               body: body === undefined ? undefined : JSON.stringify(body)});
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.detail || r.statusText);
  return j;
}
function toast(text, cls = "") {
  const d = document.createElement("div"); d.className = cls; d.textContent = text; $("toast").appendChild(d);
  setTimeout(() => d.remove(), cls === "err" ? 8000 : 3500);
}
// messages from me_studio.py (English) → the UI language
const SRV_RU = [[/^applied (\d+) file\(s\) to (.+) \(staged\)$/, "применено файлов: $1 → $2 (в индексе git)"], [/^committed to (.+)$/, "закоммичено в $1"],
  [/^pushed (.+)$/, "ветка $1 запушена"], [/^discarded \(worktree and branch (.+) removed\)$/, "отброшено (worktree и ветка $1 удалены)"], [/^discarded$/, "отброшено"],
  [/^nothing to commit$/, "нечего коммитить"], [/^nothing to apply$/, "нечего применять"], [/^restored (\d+) files?: (.*)$/, "восстановлено файлов: $1: $2"],
  [/^stop the task first$/, "сначала остановите задачу"], [/^no such folder: (.+)$/, "нет такой папки: $1"], [/^describe the task first$/, "сначала опишите задачу"],
  [/^nothing to undo$/, "нечего откатывать"], [/^busy$/, "задача ещё работает"]];
const srv = m => { m = String(m || ""); if (!L()) return m; for (const [rx, r] of SRV_RU) if (rx.test(m)) return m.replace(rx, r); return m; };
const fail = e => toast(srv(e.message || String(e)), "err");
async function copyText(s) {
  try { if (navigator.clipboard && window.isSecureContext) { await navigator.clipboard.writeText(s); return true; } } catch (e) {}
  const ta = document.createElement("textarea"); ta.value = s; ta.style.cssText = "position:fixed;top:-1000px;opacity:0"; document.body.appendChild(ta);
  ta.select(); let ok = false; try { ok = document.execCommand("copy"); } catch (e) {} ta.remove(); return ok;
}
function copyCode(b) { copyText(b.closest("pre").querySelector("code").innerText).then(ok => { b.textContent = ok ? t("copied") : "✗"; setTimeout(() => b.textContent = t("copy"), 1200); }); }

// ─── markdown (small, safe: everything is escaped first) ───
function md(src) {
  const blocks = [];
  src = String(src || "").replace(/```(\w*)[^\n]*\n?([\s\S]*?)(```|$)/g, (_, lang, code) => { blocks.push([lang, code.replace(/\n$/, "")]); return `\u0000${blocks.length - 1}\u0000`; });
  let h = esc(src)
    .replace(/^### (.*)$/gm, "<h3>$1</h3>").replace(/^## (.*)$/gm, "<h2>$1</h2>").replace(/^# (.*)$/gm, "<h1>$1</h1>")
    .replace(/^&gt; (.*)$/gm, "<blockquote>$1</blockquote>")
    .replace(/`([^`\n]+)`/g, "<code>$1</code>").replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>").replace(/(^|[\s(])\*([^*\n]+)\*/g, "$1<i>$2</i>")
    .replace(/\[([^\]]+)\]\((https?:[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
  h = h.replace(/(?:^|\n)((?:\|.*\|[ \t]*(?:\n|$))+)/g, (_, tb) => {
    const rows = tb.trim().split("\n").filter(r => !/^\|[\s:|-]+\|$/.test(r.trim()));
    return "\n<table>" + rows.map((r, i) => "<tr>" + r.trim().replace(/^\||\|$/g, "").split("|").map(c => `<${i ? "td" : "th"}>${c.trim()}</${i ? "td" : "th"}>`).join("") + "</tr>").join("") + "</table>\n";
  });
  h = h.replace(/(?:^|\n)((?:[ \t]*[-*] .*(?:\n|$))+)/g, (_, l) => "\n<ul>" + l.trim().split("\n").map(x => "<li>" + x.trim().slice(2) + "</li>").join("") + "</ul>\n");
  h = h.replace(/(?:^|\n)((?:\d+[.)] .*(?:\n|$))+)/g, (_, l) => "\n<ol>" + l.trim().split("\n").map(x => "<li>" + x.replace(/^\d+[.)] /, "") + "</li>").join("") + "</ol>\n");
  h = h.split(/\n{2,}/).map(p => /^\s*<(h\d|ul|ol|blockquote|table)/.test(p) || /^\u0000\d+\u0000$/.test(p.trim()) ? p : "<p>" + p.trim().replace(/\n/g, "<br>") + "</p>").join("");
  return h.replace(/\u0000(\d+)\u0000/g, (_, i) => `<pre><div class="bar"><span>${esc(blocks[i][0] || "code")}</span><button onclick="copyCode(this)">${t("copy")}</button></div><code>${esc(blocks[i][1])}</code></pre>`);
}
const diffHtml = s => String(s).split("\n").map(l => `<span class="dl ${l.startsWith("+") && !l.startsWith("+++") ? "a" : l.startsWith("-") && !l.startsWith("---") ? "d" : l.startsWith("@@") ? "h" : ""}">${esc(l) || " "}</span>`).join("");

// ─── theme: me's own theme colors; soft variants computed here (no color-mix: older WebKitGTK lacks it) ───
const COLOR_VARS = {bg: "--bg", bg2: "--bg2", bg3: "--bg3", line: "--line", fg: "--fg", dim: "--dim", faint: "--faint", accent: "--a", accent2: "--a2",
  ok: "--ok", err: "--err", warn: "--warn", code_bg: "--code", sel: "--sel"};
const rgba = (hex, a) => { const m = /^#?([\da-f]{2})([\da-f]{2})([\da-f]{2})/i.exec(hex || ""); return m ? `rgba(${parseInt(m[1], 16)},${parseInt(m[2], 16)},${parseInt(m[3], 16)},${a})` : hex; };
function applyTheme() {
  const r = document.documentElement.style, c = S.colors || {};
  for (const [k, v] of Object.entries(c)) if (COLOR_VARS[k]) r.setProperty(COLOR_VARS[k], v);
  const a = c.accent || "#8b7cff", w = c.warn || "#ffb454", ok = c.ok || "#5ad18a", er = c.err || "#ff6b7a";
  for (const [n, v] of Object.entries({"--a10": rgba(a, .1), "--a14": rgba(a, .14), "--a30": rgba(a, .3), "--a45": rgba(a, .45), "--w8": rgba(w, .08), "--w45": rgba(w, .45),
    "--w25": rgba(w, .25), "--ok10": rgba(ok, .1), "--err10": rgba(er, .1), "--ok35": rgba(ok, .35), "--err35": rgba(er, .35)})) r.setProperty(n, v);
  document.documentElement.style.colorScheme = S.dark ? "dark" : "light";
}

// ─── state ───
let S = {tasks: [], projects: [], models: []}, cur = null, after = 0, poll = 0, live = {}, view = {}, panel = null, draft = {};
const PREF = (() => { try { return JSON.parse(localStorage.getItem("studio") || "{}"); } catch (e) { return {}; } })();
const savePref = () => { try { localStorage.setItem("studio", JSON.stringify(PREF)); } catch (e) {} };
const task = id => S.tasks.find(x => x.id === id);
const proj = p => S.projects.find(x => x.path === p) || {path: p, name: base(p), git: false};
const statusOf = x => x.busy ? (x.status === "waiting" ? "waiting" : "running") : (x.status || "idle");
const STATUS = {running: "working", waiting: "waiting for you", review: "ready to review", done: "done", error: "error", idle: "idle", discarded: "discarded"};

async function refresh() {
  let s; try { s = await api("/api/state"); } catch (e) { return; }
  const langChanged = (s.lang || autoLang()) !== LANG;
  const before = Object.fromEntries((S.tasks || []).map(x => [x.id, statusOf(x)]));
  S = s; LANG = S.lang || autoLang(); document.documentElement.lang = LANG;
  for (const x of S.tasks) {  // other tasks finishing or asking for approval while you look at something else
    const was = before[x.id], now = statusOf(x);
    if (!was || was === now || x.id === cur) continue;
    if (now === "waiting") toast(`${x.title.slice(0, 60)} — ${t("needs your approval")}`);
    else if (was === "running" && now !== "running") toast(`${x.title.slice(0, 60)} — ${t(STATUS[now] || now)}`, now === "error" ? "err" : "ok");
  }
  applyTheme();
  if (langChanged) i18nApply();
  $("ver").textContent = "v" + S.version + " · me " + S.me;
  $("who").textContent = S.user ? "☁ " + S.user : t("offline");
  renderSide();
  if (!cur) { const sig = JSON.stringify([S.projects, S.models, LANG, S.tasks.filter(x => !x.archived).slice(0, 6).map(x => [x.id, statusOf(x), x.title])]); if (sig !== view.homeSig) { view.homeSig = sig; renderHome(); } }
  else renderTop();
}
const autoLang = () => (navigator.languages || [navigator.language || "en"]).some(l => /^(ru|be|uk|kk)\b/i.test(l)) ? "ru" : "en";

// ─── sidebar ───
function renderSide() {
  const q = ($("q").value || "").toLowerCase(), arch = !!PREF.archived, closed = PREF.closed || {};
  const by = {};
  for (const p of S.projects) by[p.path] = [];
  for (const x of S.tasks) {
    if ((!arch && x.archived) || (q && !x.title.toLowerCase().includes(q))) continue;
    (by[x.project] = by[x.project] || []).push(x);
  }
  const keys = Object.keys(by);
  $("projects").innerHTML = keys.length ? keys.map(p => { const ts = by[p], pj = proj(p), pe = esc(JSON.stringify(p));
    return `<div class="proj ${closed[p] && !q ? "closed" : ""}">
      <div class="ph" title="${esc(p)}" onclick='toggleProj(${pe})'><svg viewBox="0 0 24 24" class="chev"><path d="M6 9l6 6 6-6"/></svg><span class="nm">${esc(pj.name)}</span>
        <span class="acts"><button class="ib" title="${esc(t("New task here"))}" onclick='event.stopPropagation();go("", ${pe})'>${I.plus}</button>
        <button class="ib" title="${esc(t("Remove project"))}" onclick='event.stopPropagation();projMenu(this, ${pe})'>${I.dots}</button></span></div>
      <div class="tl">${ts.map(x => `<button class="titem ${x.id === cur ? "on" : ""}" onclick="go('${x.id}')" title="${esc(x.title)}">
          <span class="st ${statusOf(x)}"><i></i></span><span class="tt">${esc(x.title)}</span>${x.mode === "worktree" ? `<span class="wt" title="${esc(x.branch || "")}">${I.branch}</span>` : ""}<span class="ago">${ago(x.updated)}</span></button>`).join("")
        || `<div class="empty-side">${esc(t("no tasks yet"))}</div>`}</div></div>`; }).join("")
    : `<div class="empty-side">${esc(t("Pick a folder with your code. Git repositories can run tasks in isolated worktrees."))}</div>`;
}
function toggleProj(p) { PREF.closed = PREF.closed || {}; PREF.closed[p] = !PREF.closed[p]; savePref(); renderSide(); }
function projMenu(el, p) {
  menu(el, [[t("New task here"), () => go("", p)], [t("Open folder"), () => api("/api/open", {path: p}).catch(fail)], "-",
            [t("Remove project"), async () => { await api("/api/project/remove", {path: p}).catch(fail); refresh(); }, "danger"]]);
}
function openSide() { $("side").classList.add("open"); $("scrim").classList.add("on"); }
function closeSide() { $("side").classList.remove("open"); $("scrim").classList.remove("on"); }

// ─── menus & modals ───
function menu(anchor, items, opts = {}) {
  closeMenu();
  const m = document.createElement("div"); m.className = "menu"; m.id = "menu";
  m.innerHTML = items.map((it, i) => it === "-" ? "<hr>" : typeof it === "string" ? `<div class="lbl">${esc(it)}</div>` :
    `<button class="mi ${it[2] === "danger" ? "danger" : ""}" data-i="${i}"><span class="ck">${it[3] ? "✓" : ""}</span><span>${esc(it[0])}</span>${it[4] ? `<span class="d">${esc(it[4])}</span>` : ""}</button>`).join("");
  document.body.appendChild(m);
  const r = anchor.getBoundingClientRect(), mw = m.offsetWidth, mh = m.offsetHeight;
  let x = opts.right ? r.right - mw : r.left, y = r.bottom + 6;
  if (y + mh > innerHeight - 8) y = Math.max(8, r.top - mh - 6);
  m.style.left = Math.max(8, Math.min(x, innerWidth - mw - 8)) + "px"; m.style.top = y + "px";
  m.querySelectorAll(".mi").forEach(b => b.onclick = () => { closeMenu(); items[+b.dataset.i][1](); });
  setTimeout(() => document.addEventListener("mousedown", outside), 0);
}
function outside(e) { const m = $("menu"); if (m && !m.contains(e.target)) closeMenu(); }
function closeMenu() { const m = $("menu"); if (m) m.remove(); document.removeEventListener("mousedown", outside); }
function modal(title, body, foot) {
  $("modal").innerHTML = `<div class="dlg" role="dialog" aria-label="${esc(title)}"><div class="dh"><b>${esc(title)}</b><button class="ib" onclick="closeModal()" aria-label="close">${I.x.replace('style="color:var(--err)"', "")}</button></div>
    <div class="db">${body}</div>${foot ? `<div class="df">${foot}</div>` : ""}</div>`;
  $("modal").classList.remove("hidden");
  const f = $("modal").querySelector("input[type=text], textarea"); if (f) setTimeout(() => f.focus(), 30);
}
function closeModal() { $("modal").classList.add("hidden"); $("modal").innerHTML = ""; }
function confirmBox(title, text, okLabel, danger) {
  return new Promise(res => {
    modal(title, `<div style="color:var(--dim)">${esc(text)}</div>`, `<button class="btn" id="cbNo">${esc(t("Cancel"))}</button><button class="btn ${danger ? "danger solid" : "pri"}" id="cbOk">${esc(okLabel)}</button>`);
    $("cbNo").onclick = () => { closeModal(); res(false); }; $("cbOk").onclick = () => { closeModal(); res(true); };
  });
}

// ─── add project: native picker in the app window, a folder browser everywhere ───
async function addProject(then) {
  if (window.pywebview && window.pywebview.api && window.pywebview.api.pick_folder) {
    const p = await window.pywebview.api.pick_folder("").catch(() => "");
    if (p) return addPath(p, then);
    return;
  }
  const recent = S.projects.map(p => p.path);
  modal(t("Pick a project folder"), `
    <label class="f">${esc(t("Project folder"))}<input type="text" id="fsPath" placeholder="${esc(S.home || "")}" spellcheck="false"></label>
    ${recent.length ? `<div class="chips2">${recent.slice(0, 8).map(p => `<button onclick='fsGo(${esc(JSON.stringify(p))})' title="${esc(p)}">${esc(base(p))}</button>`).join("")}</div>` : ""}
    <div class="fs"><div class="fsh"><button class="ib" title="${esc(t("Up"))}" onclick="fsUp()"><svg viewBox="0 0 24 24"><path d="M12 19V5M5 12l7-7 7 7"/></svg></button>
      <input id="fsCur" spellcheck="false" onkeydown="if(event.key==='Enter')fsGo(this.value)"><span id="fsRoots"></span></div><div class="list" id="fsList"></div></div>`,
    `<button class="btn" onclick="closeModal()">${esc(t("Cancel"))}</button><button class="btn pri" id="fsOk">${esc(t("Add"))}</button>`);
  view.fsThen = then;
  $("fsOk").onclick = () => addPath($("fsPath").value.trim() || view.fs?.path, view.fsThen);
  $("fsPath").onkeydown = e => { if (e.key === "Enter") $("fsOk").click(); };
  fsGo(recent[0] ? recent[0].replace(/[\\/][^\\/]+[\\/]?$/, "") || S.home : S.home);
}
async function fsGo(p) {
  $("fsList").innerHTML = `<div class="empty-side">${esc(t("loading…"))}</div>`;
  let d; try { d = await api("/api/fs?path=" + encodeURIComponent(p || "")); } catch (e) { $("fsList").innerHTML = `<div class="empty-side" style="color:var(--err)">${esc(e.message)}</div>`; return; }
  view.fs = d; $("fsCur").value = d.path; $("fsPath").value = d.path;
  $("fsRoots").innerHTML = (d.roots || []).map(r => `<button class="btn sm ghost" onclick='fsGo(${esc(JSON.stringify(r))})'>${esc(r)}</button>`).join("");
  const sep = d.path.includes("\\") ? "\\" : "/";
  $("fsList").innerHTML = (d.dirs.length ? d.dirs.map(x => { const full = d.path.replace(/[\\/]$/, "") + sep + x.name;
    return `<button class="row" ondblclick='fsGo(${esc(JSON.stringify(full))})' onclick='fsPick(this, ${esc(JSON.stringify(full))})'>${I.folder}<span>${esc(x.name)}</span>${x.git ? `<span class="g">git</span>` : ""}</button>`; }).join("")
    : `<div class="empty-side">—</div>`);
}
function fsPick(el, p) { document.querySelectorAll("#fsList .row").forEach(r => r.classList.remove("sel")); el.classList.add("sel"); $("fsPath").value = p; }
function fsUp() { if (view.fs && view.fs.parent) fsGo(view.fs.parent); }
async function addPath(p, then) {
  if (!p) return;
  try { const r = await api("/api/project/add", {path: p}); closeModal(); await refresh(); view.project = r.path; if (then) then(r.path); else go("", r.path); }
  catch (e) { fail(e); }
}

// ─── home: start a task ───
function renderHome() {
  const has = S.projects.length;
  if (!view.project || !S.projects.some(p => p.path === view.project)) view.project = PREF.project && S.projects.some(p => p.path === PREF.project) ? PREF.project : (S.projects[0] || {}).path;
  const p = view.project ? proj(view.project) : null;
  const mode = p && p.git ? (view.mode || PREF.mode || "local") : "local";
  const recent = S.tasks.filter(x => !x.archived).slice(0, 6);
  const m = S.models.find(x => x.name === (view.model || S.model));
  $("home").innerHTML = `<div class="hero">
    <h1>${esc(t("What should we work on?"))}</h1><p class="sub">${esc(t("Each task is its own agent. Start several — they run side by side."))}</p>
    ${has ? "" : `<div class="onboard">${I.folder}<div class="tx"><b>${esc(t("Start by adding a project"))}</b>${esc(t("Pick a folder with your code. Git repositories can run tasks in isolated worktrees."))}</div><button class="btn pri" onclick="addProject()">${esc(t("Add project"))}</button></div>`}
    <div class="composer"><textarea id="prompt" rows="3" placeholder="${esc(t("Describe a task — fix a bug, add a feature, explain the code…"))}">${esc(draft.home || "")}</textarea>
      <div class="crow">
        <button class="chip ${p ? "" : "warn"}" id="pChip" onclick="pickProject(this)" title="${esc(p ? p.path : "")}">${I.folder}<span>${esc(p ? p.name : t("Choose a project"))}</span>${I.chev}</button>
        <span class="seg"><button class="${mode === "local" ? "on" : ""}" onclick="setMode('local')" title="${esc(t("Works in the project folder itself"))}">${esc(t("Local"))}</button><button class="${mode === "worktree" ? "on" : ""}" ${p && p.git ? "" : "disabled"} onclick="setMode('worktree')" title="${esc(p && p.git ? t("Its own git worktree and branch: your checkout stays untouched until you apply") : t("needs a git repository with a commit"))}">${I.branch}${esc(t("Worktree"))}</button></span>
        <button class="chip" onclick="pickModel(this)" title="${esc(t("Model"))}">${I.cpu}<span>${esc(m ? m.name : t("no models — add one with `me` → /models"))}${view.effort ? " · " + esc(view.effort) : ""}</span>${I.chev}</button>
        <span class="sp"></span><button class="send" id="startBtn" onclick="startTask()" title="${esc(t("Start"))}" ${p ? "" : "disabled"}>${I.up}</button></div></div>
    <div class="hint"><span><kbd>Enter</kbd> ${esc(t("enter to start").replace(/^enter — |^enter to /, ""))}</span><span><kbd>Shift Enter</kbd> ${esc(t("shift+enter new line").replace(/^shift\+enter — |^shift\+enter /, ""))}</span><span>${esc(t("Ask in Russian or English — the agent answers in your language."))}</span></div>
    ${p ? `<div class="starters">${["Fix the failing tests", "Explain how this project is structured", "Review my uncommitted changes", "Add a README with setup steps"].map(s => `<button onclick='useStarter(${esc(JSON.stringify(t(s)))})'>${esc(t(s))}</button>`).join("")}</div>` : ""}
    ${recent.length ? `<div class="recent"><h3>${esc(t("Recent"))}</h3>${recent.map(x => `<button class="rc" onclick="go('${x.id}')"><span class="st ${statusOf(x)}"><i></i></span><span class="tt">${esc(x.title)}</span><span class="m">${esc(base(x.project))} · ${ago(x.updated)}</span></button>`).join("")}</div>` : ""}
  </div>`;
  const ta = $("prompt"); ta.oninput = () => { draft.home = ta.value; grow(ta, 320); };
  ta.onkeydown = e => { if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); startTask(); } };
  grow(ta, 320); if (has) setTimeout(() => ta.focus(), 0);
}
function useStarter(s) { draft.home = s; renderHome(); }
function setMode(m) { view.mode = m; PREF.mode = m; savePref(); renderHome(); }
function pickProject(el) {
  menu(el, [t("Project folder"), ...S.projects.map(p => [p.name, () => { view.project = p.path; PREF.project = p.path; savePref(); renderHome(); }, "", p.path === view.project, p.git ? "git" : ""]),
            "-", ["+ " + t("Add project"), () => addProject(path => { view.project = path; PREF.project = path; savePref(); go("", path); })]]);
}
const EFFORTS = ["", "low", "medium", "high", "xhigh"];
function pickModel(el, onPick) {
  const curM = onPick ? (task(cur) || {}).model || S.model : view.model || S.model, curE = onPick ? (task(cur) || {}).effort || "" : view.effort || "";
  const set = onPick || ((k, v) => { view[k] = v; renderHome(); });
  menu(el, [t("Model"), ...S.models.map(x => [x.name, () => set("model", x.name), "", x.name === curM, x.id]),
            "-", t("effort"), ...EFFORTS.map(e => [e || t("default"), () => set("effort", e), "", e === curE])]);
}
async function startTask() {
  const ta = $("prompt"), prompt = ta.value.trim(), p = view.project;
  if (!p) return addProject();
  if (!prompt) return ta.focus();
  const pj = proj(p), mode = pj.git ? (view.mode || PREF.mode || "local") : "local";
  $("startBtn").disabled = true;
  try {
    const r = await api("/api/task/new", {project: p, prompt, mode, model: view.model || "", effort: view.effort || ""});
    draft.home = ""; PREF.project = p; savePref();
    await refresh(); go(r.id);
  } catch (e) { fail(e); $("startBtn").disabled = false; }
}
function grow(ta, max = 240) { ta.style.height = "auto"; ta.style.height = Math.min(max, ta.scrollHeight) + "px"; }

// ─── navigation ───
function go(id, project) {
  closeSide(); closeMenu();
  if (cur && $("msg")) draft[cur] = $("msg").value;
  cur = id || null; poll++; live = {}; view.files = null;
  if (project) view.project = project;
  $("home").classList.toggle("hidden", !!cur); $("task").classList.toggle("hidden", !cur);
  if (!cur) { closePanel(); $("ttl").innerHTML = `<b>${esc(t("New task"))}</b>`; $("acts").innerHTML = ""; view.homeSig = ""; renderHome(); }
  else openTask(cur);
  renderSide();
  try { history.replaceState(null, "", location.pathname + location.search.replace(/[&?]t=[^&]*/, "") + (cur ? (location.search ? "&" : "?") + "t=" + cur : "")); } catch (e) {}
}

// ─── task view ───
function renderTop() {
  const x = task(cur); if (!x) return;
  const st = statusOf(x), editing = $("ttlEdit");
  if (!editing) $("ttl").innerHTML = `<b onclick="renameTask()" title="${esc(t("Rename"))}">${esc(x.title)}</b>
    <span class="meta wide">${esc(base(x.project))}<span class="sep">·</span>${x.mode === "worktree" ? `${I.branch}${esc(x.branch || "")}` : esc(t("Local"))}</span>`;
  const files = view.files, adds = view.adds || 0, dels = view.dels || 0;
  $("acts").innerHTML = `<span class="pill ${st}">${st === "running" ? '<span class="spinner"></span>' : ""}${esc(t(STATUS[st] || st))}</span>
    ${x.busy ? `<button class="btn sm" onclick="stopTask()" title="Esc">${I.stop}${esc(t("Stop"))}</button>` : ""}
    <button class="btn sm ${panel === "changes" ? "" : "ghost"}" onclick="togglePanel('changes')" title="${esc(t("Changes"))}">${I.diff}<span class="wide">${esc(t("Review"))}</span>${files ? ` <span class="chg"><span class="add">+${adds}</span> <span class="del">−${dels}</span></span>` : ""}</button>
    <button class="ib ${panel === "term" ? "on" : ""}" onclick="togglePanel('term')" title="${esc(t("Terminal"))}">${I.term}</button>
    <button class="ib" onclick="taskMenu(this)" aria-label="more">${I.dots}</button>`;
}
function taskMenu(el) {
  const x = task(cur); if (!x) return;
  menu(el, [[t("Rename"), renameTask], [t("Open folder"), () => api("/api/open", {id: cur}).catch(fail)],
            [x.archived ? t("Unarchive") : t("Archive"), archiveTask], "-", [t("Discard…"), discardTask, "danger"]], {right: true});
}
async function openTask(id) {
  const x = task(id) || {};
  $("task").innerHTML = `<div id="plan" class="plan hidden"></div><div id="log"><div class="lw" id="lw"></div></div>
    <div class="foot"><div class="composer"><textarea id="msg" rows="1"></textarea>
      <div class="crow"><span class="mini"><button class="chip" style="height:26px" onclick="pickModel(this, setTaskModel)">${I.cpu}<span id="tModel"></span>${I.chev}</button></span>
        <span class="sp"></span><button class="send" id="sendBtn" onclick="sendOrStop()">${I.up}</button></div></div></div>`;
  const ta = $("msg"); ta.value = draft[id] || "";
  ta.oninput = () => { grow(ta); setSendBtn(); };
  ta.onkeydown = e => { if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); sendMsg(); } };
  renderTop(); setSendBtn(); renderTaskModel();
  let d;
  try { d = await api("/api/task?id=" + encodeURIComponent(id)); } catch (e) { return fail(e); }
  if (cur !== id) return;
  renderHistory(d.messages); renderPlan(d.todo);
  (d.pending || []).forEach(onEvent);
  after = d.seq; refreshDiff();
  setTimeout(() => ta.focus(), 0);
  const my = ++poll;
  while (my === poll && cur === id) {
    try {
      const r = await api(`/api/events?id=${encodeURIComponent(id)}&after=${after}`);
      if (my !== poll) return;
      for (const e of r.events) { after = Math.max(after, e.seq); onEvent(e); }
      const xx = task(id); if (xx && xx.busy !== r.busy) refresh();
    } catch (e) { await new Promise(r => setTimeout(r, 1500)); }
  }
}
function renderTaskModel() { const x = task(cur) || {}; const el = $("tModel"); if (el) el.textContent = (x.model || S.model || "—") + (x.effort ? " · " + x.effort : ""); }
async function setTaskModel(k, v) {
  try { await api("/api/task/model", {id: cur, [k]: v}); const x = task(cur); if (x) x[k] = v; renderTaskModel(); } catch (e) { fail(e); }
}
function setSendBtn() {
  const x = task(cur) || {}, b = $("sendBtn"), ta = $("msg"); if (!b || !ta) return;
  const stop = x.busy && !ta.value.trim();
  b.className = "send" + (stop ? " stop" : ""); b.innerHTML = stop ? I.stop : I.up; b.title = stop ? t("Stop") : t("Send");
  ta.placeholder = x.busy ? t("Steer the running task — it reads this after the current step") : t("Ask for follow-up changes");
}
const lw = () => $("lw");
function atBottom() { const l = $("log"); return !l || l.scrollHeight - l.scrollTop - l.clientHeight < 140; }
function add(html) {
  const stick = atBottom(), d = document.createElement("div"); d.innerHTML = html.trim(); const n = d.firstChild;
  if (lw()) lw().appendChild(n); if (stick) scrollDown(true); return n;
}
function scrollDown(force) { const l = $("log"); if (l && (force || atBottom())) l.scrollTop = l.scrollHeight; }
const txt = c => Array.isArray(c) ? c.filter(x => x.type === "text").map(x => x.text).join(" ") : (c || "");
function userMsg(text, tag) { add(`<div class="msg user">${tag ? `<span class="tag">${esc(t(tag))}</span>` : ""}<div class="bub">${esc(text)}</div></div>`); }
function toolLabel(name, a) {
  a = a || {};
  if (name === "bash") return a.cmd || "";
  if (name === "todo") return "";
  return a.path || a.pattern || a.query || a.url || a.name || a.prompt || a.text || "";
}
function toolCard(name, a, label) {
  return add(`<div class="tool closed"><div class="h" onclick="this.parentNode.classList.toggle('closed')"><span class="ic"><span class="spinner"></span></span>
    <span class="nm">${esc(verb(name))}</span><span class="lab">${esc(label != null ? label : toolLabel(name, a))}</span><span class="t"></span>${I.right}</div><div class="stl hidden"></div><pre class="body"></pre></div>`);
}
function finishCard(c, ok, out, time) {
  c.querySelector(".ic").innerHTML = ok ? I.check : I.x; if (time != null) c.querySelector(".t").textContent = dur(time);
  c.querySelector(".stl").classList.add("hidden");
  const b = c.querySelector(".body"); if (!b.dataset.diff) b.textContent = String(out || "").slice(0, 30000) || "—";
  if (!ok) c.classList.add("err");
}
function renderHistory(msgs) {
  lw().innerHTML = "";
  const outs = {};
  msgs.forEach(m => { if (m.role === "tool") outs[m.tool_call_id] = m.content; });
  msgs.forEach(m => {
    if (m.role === "user") {
      const s = txt(m.content);
      if (m._aux) { if (/^\[message from the user/.test(s)) userMsg(s.split("\n").slice(1).join("\n"), "while working"); return; }
      if (s && s !== "(images from read)") userMsg(s);
    } else if (m.role === "assistant") {
      if (m.content) add(`<div class="md">${md(m.content)}</div>`);
      (m.tool_calls || []).forEach(tc => {
        if (tc.function.name === "todo") return;
        let a = {}; try { a = JSON.parse(tc.function.arguments); } catch (e) {}
        const c = toolCard(tc.function.name, a); const o = outs[tc.id]; finishCard(c, !/^error/.test(o || ""), o);
      });
    }
  });
  if (!msgs.length) add(`<div class="note">${esc(t("loading…"))}</div>`);
  scrollDown(true);
}
function renderPlan(items) {
  const el = $("plan"); if (!el) return;
  items = items || [];
  if (!items.length) { el.classList.add("hidden"); return; }
  const done = items.filter(i => i.s === "done").length;
  el.classList.remove("hidden"); el.classList.toggle("closed", !!PREF.planClosed);
  el.innerHTML = `<div class="in"><div class="ph2" onclick="PREF.planClosed=!PREF.planClosed;savePref();this.closest('.plan').classList.toggle('closed')"><b>${esc(t("Plan"))}</b><span>${done}/${items.length}</span>
    <span class="bar2"><i style="width:${Math.round(done / items.length * 100)}%"></i></span>${I.chev}</div>
    <div class="items">${items.map(i => `<div class="it ${i.s === "done" ? "d" : i.s === "doing" ? "g" : ""}"><span>${i.s === "done" ? "✓" : i.s === "doing" ? "▸" : "○"}</span><span>${esc(i.t)}</span></div>`).join("")}</div></div>`;
}
let raf = 0;
function flushText() { if (live.text) live.text.innerHTML = md(live.raw); }  // the last streamed piece must not wait for a frame that may never come
function endThink() { if (live.think && !live.thinkDone) { live.think.querySelector("summary").innerHTML = esc(t("thought for {}", dur((Date.now() - live.thinkT) / 1000))); live.thinkDone = true; } }
function onEvent(e) {
  const k = e.type;
  if (k === "user") { const last = lw() && lw().lastElementChild; if (!(last && last.classList.contains("msg") && last.querySelector(".bub").textContent === e.text && view.echo)) userMsg(e.text); view.echo = false; return; }
  if (k === "queued") { userMsg(e.text, "queued"); return; }
  if (k === "queued_sent") return;
  if (k === "turn_start") { refresh(); return; }
  if (k === "reasoning") {
    if (!live.think) { live.think = add(`<details class="think"><summary><span class="shimmer">${esc(t("thinking…"))}</span></summary><pre></pre></details>`); live.thinkT = Date.now(); live.thinkDone = false; }
    live.think.querySelector("pre").textContent += e.text; scrollDown(); return;
  }
  if (k === "text") {
    endThink();
    if (!live.text) { live.text = add(`<div class="md"></div>`); live.raw = ""; }
    live.raw += e.text;
    if (!raf) raf = requestAnimationFrame(() => { raf = 0; if (live.text) { live.text.innerHTML = md(live.raw); scrollDown(); } });
    return;
  }
  if (k === "tool_start") {
    endThink(); flushText(); live.text = null; live.think = null;
    if (e.name === "todo") return;
    live[e.id] = toolCard(e.cli ? e.cli.label : e.name, e.args, e.cli ? null : (e.name === "bash" ? (e.args || {}).cmd : toolLabel(e.name, e.args) || e.label)); return;
  }
  if (k === "tool_event") {
    const c = live[e.id]; if (!c) return;
    const s = c.querySelector(".stl");
    if (e.t === "status" || e.t === "log") { s.classList.remove("hidden"); s.textContent = (e.t === "log" ? "› " : "") + e.text; }
    else if (e.t === "diff" && (e.text || "").trim()) { const b = c.querySelector(".body"); b.dataset.diff = 1; b.innerHTML = diffHtml(e.text); c.classList.remove("closed"); }
    scrollDown(); return;
  }
  if (k === "tool_end") { const c = live[e.id]; if (c) { delete live[e.id]; finishCard(c, e.ok, e.output, e.time); } return; }
  if (k === "confirm") {
    endThink();
    const always = e.name === "bash" ? t("Always allow this command") : t("Allow everything (full access)");
    const a = e.args || {}, what = e.name === "bash" ? a.cmd : e.name === "edit" || e.name === "write" ? (a.path || "") + "\n\n" + (a.new || a.content || "").slice(0, 1500) : JSON.stringify(a, null, 1);
    const c = add(`<div class="card"><div class="q">${I.warn}<span>${esc(verb(e.name))} — ${esc(t("needs your approval"))}</span></div><pre>${esc(String(what || "").slice(0, 3000))}</pre>
      <div class="btns"><button class="btn sm pri">${esc(t("Allow"))}</button><button class="btn sm">${esc(always)}</button><button class="btn sm danger">${esc(t("Deny"))}</button></div></div>`);
    const [y, al, n] = c.querySelectorAll("button");
    const ans = v => { api("/api/task/confirm", {id: cur, rid: e.id, ok: v}).catch(fail); c.className = "card done"; c.innerHTML = `${esc(verb(e.name))} · ${esc(t(v ? (v === "always" ? "always allowed" : "allowed") : "denied"))}`; refresh(); };
    y.onclick = () => ans(true); al.onclick = () => ans("always"); n.onclick = () => ans(false);
    y.focus(); scrollDown(true); return;
  }
  if (k === "ask") {
    const opts = e.options || [];
    const c = add(`<div class="card"><div class="q">${I.q}${esc(e.text)}</div><div class="btns">${opts.length ? opts.map(o => `<button class="btn sm" data-v="${esc(o)}">${esc(o)}</button>`).join("") : `<input placeholder="${esc(t("answer…"))}"><button class="btn sm pri" data-in="1">${esc(t("Send"))}</button>`}</div></div>`);
    const send = v => { api("/api/task/answer", {id: cur, rid: e.id, text: v}).catch(fail); c.className = "card done"; c.innerHTML = `${esc(e.text)} → <b>${esc(v || "—")}</b>`; };
    c.querySelectorAll("button").forEach(b => b.onclick = () => send(b.dataset.in ? c.querySelector("input").value : b.dataset.v));
    const inp = c.querySelector("input"); if (inp) { inp.onkeydown = ev => ev.key === "Enter" && send(inp.value); inp.focus(); }
    scrollDown(true); return;
  }
  if (k === "retry") {
    if (e.discard) { live.text && live.text.remove(); live.think && live.think.remove(); live.text = live.think = null; }
    add(`<div class="note warn">↻ ${esc(String(e.error || "").split("\n")[0].slice(0, 160))} — ${esc(t("retrying"))}</div>`); return;
  }
  if (k === "compacting") { add(`<div class="note">${esc(t("compacting history…"))}</div>`); return; }
  if (k === "compacted") { add(`<div class="note">${esc(t("history compacted · ~{} tokens", human(e.ctx)))}</div>`); return; }
  if (k === "verify") { add(`<div class="note">${esc(t("self-check: running checks before finishing"))}</div>`); return; }
  if (k === "notice") { add(`<div class="note warn">${esc(e.text)}</div>`); return; }
  if (k === "error") { flushText(); add(`<div class="note ${e.text === "interrupted" ? "warn" : "err"}">${I.x}${esc(e.text)}</div>`); return; }
  if (k === "todo") { renderPlan(e.items); return; }
  if (k === "turn_diff") {
    const fs = e.files || []; if (!fs.length) return;
    const n = add(`<div class="note">${I.diff}<span>${esc(fs.length === 1 ? t("1 file changed") : t("{} files changed", fs.length))}</span> · <a>${esc(t("review"))}</a></div>`);
    n.querySelector("a").onclick = () => openPanel("changes");
    refreshDiff(); return;
  }
  if (k === "turn_end") {
    endThink(); flushText(); for (const id of Object.keys(live)) if (live[id] && live[id].classList && live[id].classList.contains("tool")) finishCard(live[id], false, "interrupted");
    live = {};
    const steps = e.steps || 1;
    add(`<div class="turnend">${esc(t("Worked {}", dur(e.time || 0)))} · ${esc(steps === 1 ? t("1 step") : t("{} steps", steps))} · ↑${human(e.in)} ↓${human(e.out)}${e.cost ? " · $" + e.cost.toFixed(4) : ""}</div>`);
    refreshDiff(); refresh().then(setSendBtn); return;
  }
  if (k === "status") { refresh().then(setSendBtn); if (panel === "changes") renderChanges(); return; }
}
function sendOrStop() { const x = task(cur) || {}; if (x.busy && !$("msg").value.trim()) stopTask(); else sendMsg(); }
async function sendMsg() {
  const ta = $("msg"), text = ta.value.trim(); if (!text) return;
  ta.value = ""; draft[cur] = ""; grow(ta);
  const x = task(cur) || {};
  if (!x.busy) { userMsg(text); view.echo = true; }
  try { await api("/api/task/send", {id: cur, text}); await refresh(); setSendBtn(); } catch (e) { fail(e); }
}
async function stopTask() { try { await api("/api/task/stop", {id: cur}); } catch (e) { fail(e); } }
async function archiveTask() { try { await api("/api/task/archive", {id: cur}); await refresh(); } catch (e) { fail(e); } }
function renameTask() {
  const x = task(cur); if (!x) return;
  $("ttl").innerHTML = `<input id="ttlEdit" value="${esc(x.title)}">`;
  const i = $("ttlEdit"); i.focus(); i.select();
  const done = async save => { if (!$("ttlEdit")) return; const v = i.value.trim(); i.remove(); if (save && v && v !== x.title) { await api("/api/task/rename", {id: cur, title: v}).catch(fail); x.title = v; } renderTop(); renderSide(); };
  i.onkeydown = e => { if (e.key === "Enter") done(true); if (e.key === "Escape") done(false); };
  i.onblur = () => done(true);
}
async function discardTask() {
  const x = task(cur); if (!x) return;
  const ok = await confirmBox(t("Discard this task?"), x.mode === "worktree" ? t("Its worktree and branch {} are deleted. This cannot be undone.", x.branch || "") : t("The task is removed from the list; files it changed stay as they are."), t("Discard"), true);
  if (!ok) return;
  try { const r = await api("/api/task/discard", {id: cur}); toast(srv(r.text), "ok"); await refresh(); go(""); } catch (e) { fail(e); }
}
async function refreshDiff() {
  if (!cur) return;
  try { const d = await api("/api/diff?id=" + encodeURIComponent(cur)); view.diff = d; view.files = d.files.length; view.adds = d.files.reduce((a, f) => a + f.add, 0); view.dels = d.files.reduce((a, f) => a + f.del, 0); renderTop(); if (panel === "changes") renderChanges(true); } catch (e) {}
}

// ─── review / terminal panel ───
function closePanel() { panel = null; $("panel").classList.add("hidden"); if (cur) renderTop(); }
function togglePanel(tab) { panel === tab ? closePanel() : openPanel(tab); }
function openPanel(tab) { if (!cur) return; panel = tab; $("panel").classList.remove("hidden"); panelTab(tab); renderTop(); }
function panelTab(tab) {
  panel = tab; document.querySelectorAll(".ptab").forEach(b => b.classList.toggle("on", b.dataset.tab === tab));
  if (tab === "term") renderTerm(); else { $("pbody").innerHTML = `<div class="empty">${esc(t("loading…"))}</div>`; refreshDiff().then(() => renderChanges()); }
  renderTop();
}
function renderChanges(keep) {
  const x = task(cur) || {}, d = view.diff; if (!d || panel !== "changes") return;
  const open = new Set(keep ? [...document.querySelectorAll(".fitem.open")].map(f => f.dataset.p) : []);
  const wt = x.mode === "worktree", n = d.files.length;
  const sum = n ? `<b>${esc(n === 1 ? t("1 file") : t("{} files", n))}</b> · <span class="add">+${view.adds}</span> <span class="del">−${view.dels}</span>` : esc(t("No changes yet"));
  $("pbody").innerHTML = `<div class="rv-head"><div class="rv-sum">${sum}${wt ? ` · ${I.branch}<span>${esc(d.branch || x.branch || "")}</span>${d.commits ? ` · ${d.commits} ${esc(t("commits"))}` : ""}` : ""}</div>
      <div class="rv-acts">${wt ? `<button class="btn sm pri" onclick="act('apply')" ${n ? "" : "disabled"}>${esc(t("Apply to project"))}</button><button class="btn sm" onclick="act('push')">${esc(t("Push branch"))}</button>`
        : `<button class="btn sm" onclick="act('undo')" ${x.busy ? "disabled" : ""}>${esc(t("Undo last turn"))}</button>`}</div>
      <div class="commit"><input id="cmsg" placeholder="${esc(t("Commit message"))}" value="${esc(view.cmsg || x.title || "")}" oninput="view.cmsg=this.value" onkeydown="if(event.key==='Enter')act('commit')"><button class="btn sm" onclick="act('commit')" ${n || !wt ? "" : "disabled"}>${esc(t("Commit"))}</button></div>
      ${d.note ? `<div class="note">${esc(d.note)}</div>` : ""}</div>
    ${n ? d.files.map((f, i) => `<div class="fitem ${open.has(f.path) || (!keep && i < 4 && f.add + f.del < 300) ? "open" : ""}" data-p="${esc(f.path)}"><div class="fh" onclick="this.parentNode.classList.toggle('open')">${I.right}<span class="p" title="${esc(f.path)}">${esc(f.path)}</span>${f.new ? `<span class="tag new">${esc(t("new"))}</span>` : ""}${f.deleted ? `<span class="tag gone">${esc(t("deleted"))}</span>` : ""}<span class="chg"><span class="add">+${f.add}</span> <span class="del">−${f.del}</span></span></div>
      <pre>${f.binary ? `<span class="dl">${esc(t("binary file"))}</span>` : diffHtml(f.diff.split("\n").filter(l => !/^(diff --git|index |--- |\+\+\+ |new file mode|deleted file mode)/.test(l)).join("\n"))}</pre></div>`).join("")
      : `<div class="empty">${I.file}<b>${esc(t("No changes yet"))}</b><span>${esc(t("The agent's edits show up here as it works."))}</span></div>`}`;
}
async function act(kind) {
  try {
    let r;
    if (kind === "commit") r = await api("/api/task/commit", {id: cur, message: ($("cmsg") || {}).value || ""});
    else if (kind === "undo") { r = await api("/api/task/undo", {id: cur}); openTask(cur); }
    else r = await api("/api/task/" + kind, {id: cur});
    toast(srv(r.text || "ok"), "ok"); refreshDiff(); refresh();
  } catch (e) { fail(e); }
}
function renderTerm() {
  if (!view.term) view.term = {};
  const out = view.term[cur] || "";
  $("pbody").innerHTML = `<div class="term"><pre id="termOut">${out}</pre><div class="termin"><span>$</span><input id="termIn" placeholder="${esc(t("Run a command in the task folder"))}" spellcheck="false" autocomplete="off"></div></div>`;
  const i = $("termIn"), o = $("termOut"); o.scrollTop = o.scrollHeight; i.focus();
  const hist = view.thist = view.thist || []; let hp = hist.length;
  i.onkeydown = async e => {
    if (e.key === "ArrowUp" && hp > 0) { hp--; i.value = hist[hp]; e.preventDefault(); return; }
    if (e.key === "ArrowDown") { hp = Math.min(hist.length, hp + 1); i.value = hist[hp] || ""; e.preventDefault(); return; }
    if (e.key !== "Enter" || !i.value.trim()) return;
    const cmd = i.value; i.value = ""; hist.push(cmd); hp = hist.length; i.disabled = true;
    const id = cur;
    view.term[id] = (view.term[id] || "") + `<span class="cmd">$ ${esc(cmd)}</span>\n`; if (panel === "term") o.innerHTML = view.term[id];
    try { const r = await api("/api/term", {id, cmd}); view.term[id] += (r.ok ? esc(r.output) : `<span class="bad">${esc(r.output)}</span>`) + "\n"; }
    catch (er) { view.term[id] += `<span class="bad">${esc(er.message)}</span>\n`; }
    if (panel === "term" && cur === id) { o.innerHTML = view.term[id]; o.scrollTop = o.scrollHeight; i.disabled = false; i.focus(); }
  };
}

// ─── settings ───
function openSettings() {
  const perms = [["ask", "ask", "edits in the project are free; commands ask"], ["full", "full access", "everything runs without asking"], ["read", "read-only", "no file changes"]];
  modal(t("Settings"), `
    <label class="f">${esc(t("Language"))}<div class="opt">${[["", "Auto"], ["en", "English"], ["ru", "Русский"]].map(([v, l]) => `<button class="${(S.lang || "") === v ? "on" : ""}" onclick="setSetting({lang: '${v}'})">${l}</button>`).join("")}</div></label>
    <label class="f">${esc(t("Permissions"))}<div class="opt">${perms.map(([v, l, d]) => `<button class="${S.perm === v ? "on" : ""}" onclick="setSetting({perm: '${v}'})">${esc(t(l))}<small>${esc(t(d))}</small></button>`).join("")}</div></label>
    <label class="f">${esc(t("Theme"))}<select onchange="setSetting({theme: this.value})">${(S.themes || []).map(n => `<option ${n === S.theme ? "selected" : ""}>${esc(n)}</option>`).join("")}</select></label>
    <label class="f">${esc(t("Default model"))}<select onchange="setSetting({model: this.value})">${S.models.map(m => `<option ${m.name === S.model ? "selected" : ""}>${esc(m.name)}</option>`).join("")}</select></label>
    <label style="display:flex;gap:8px;align-items:center"><input type="checkbox" ${PREF.archived ? "checked" : ""} onchange="PREF.archived=this.checked;savePref();renderSide()">${esc(t("Show archived tasks"))}</label>
    <div class="note">${esc(t("Shortcuts"))}: <kbd>Ctrl N</kbd> ${esc(t("New task").toLowerCase())} · <kbd>Ctrl K</kbd> ${esc(t("search"))} · <kbd>Esc</kbd> ${esc(t("stop the task"))}</div>`,
    `<button class="btn" onclick="closeModal()">${esc(t("Close"))}</button>`);
}
async function setSetting(b) { try { S = await api("/api/settings", b); LANG = S.lang || autoLang(); applyTheme(); i18nApply(); openSettings(); renderSide(); cur ? renderTop() : (view.homeSig = "", renderHome()); } catch (e) { fail(e); } }

// ─── keys & boot ───
document.addEventListener("keydown", e => {
  const mod = e.ctrlKey || e.metaKey;
  if (mod && e.key.toLowerCase() === "n") { e.preventDefault(); go(""); }
  else if (mod && e.key.toLowerCase() === "k") { e.preventDefault(); openSide(); $("q").focus(); }
  else if (e.key === "Escape") {
    if ($("menu")) closeMenu(); else if (!$("modal").classList.contains("hidden")) closeModal();
    else if (cur && (task(cur) || {}).busy) stopTask(); else if (panel) closePanel();
  }
});
(async () => {
  LANG = autoLang(); i18nApply();
  await refresh(); i18nApply();
  const tid = new URLSearchParams(location.search).get("t");
  go(tid && task(tid) ? tid : "");
  setInterval(refresh, 4000);
})();

</script>
</body>
</html>
"""

if __name__ == "__main__":
    main()
