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

VERSION = "1.0"
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
                "colors": th["colors"], "themes": list(me.themes(self.store)), "user": self.store.auth.get("user", "")}


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
        st.save()
        return studio.state()
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
        p = t.meta["cwd"]
        if me.WIN:
            os.startfile(p)  # noqa
        else:
            subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", p], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return {"ok": True}
    raise KeyError(path)


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
<style>
:root { --bg:#0e0f13; --bg2:#111319; --bg3:#171a23; --line:#1f2230; --fg:#d8dae3; --dim:#6b6f80; --faint:#3a3d4a; --a:#8b7cff; --a2:#4fd1c5;
  --ok:#5ad18a; --err:#ff6b7a; --warn:#ffb454; --code:#16181f; --sel:#262a38; --r:10px;
  --mono: ui-monospace, "SF Mono", "Cascadia Code", "JetBrains Mono", Consolas, monospace; --sans: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }
* { box-sizing: border-box; }
html, body { margin: 0; height: 100%; background: var(--bg); color: var(--fg); font: 14px/1.5 var(--sans); }
button, input, select, textarea { font: inherit; color: inherit; }
button { cursor: pointer; }
svg { width: 16px; height: 16px; fill: none; stroke: currentColor; stroke-width: 1.8; stroke-linecap: round; stroke-linejoin: round; flex: none; }
.hidden { display: none !important; }
#app { display: grid; grid-template-columns: 272px 1fr auto; height: 100vh; height: 100dvh; }
/* sidebar */
#side { background: var(--bg2); border-right: 1px solid var(--line); display: flex; flex-direction: column; min-width: 0; }
.brand { display: flex; align-items: center; gap: 10px; padding: 14px 14px 10px; }
.brand .logo { width: 18px; height: 18px; border-radius: 6px; background: var(--a); }
.brand b { font-size: 15px; letter-spacing: .2px; }
.brand .v { color: var(--dim); font: 11px var(--mono); margin-left: auto; }
.btn { display: inline-flex; align-items: center; gap: 8px; border: 1px solid var(--line); background: var(--bg3); border-radius: 8px; padding: 7px 12px; }
.btn:hover { border-color: var(--faint); }
.btn.pri { background: var(--a); border-color: var(--a); color: var(--bg); font-weight: 600; }
.btn.pri:disabled { opacity: .45; cursor: default; }
.btn.danger { color: var(--err); }
.btn.sm { padding: 4px 9px; font-size: 13px; }
.ib { display: inline-grid; place-items: center; width: 30px; height: 30px; border: 0; background: none; border-radius: 7px; color: var(--dim); }
.ib:hover { background: var(--bg3); color: var(--fg); }
.newtask { margin: 4px 12px 8px; justify-content: center; }
#projects { flex: 1; overflow: auto; padding: 4px 8px 12px; }
.proj { margin-top: 10px; }
.proj > .ph { display: flex; align-items: center; gap: 6px; padding: 4px 6px; color: var(--dim); font-size: 12px; text-transform: none; }
.proj > .ph span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-weight: 600; color: var(--fg); opacity: .8; }
.proj > .ph .ib { width: 24px; height: 24px; margin-left: auto; opacity: 0; }
.proj:hover > .ph .ib { opacity: 1; }
.titem { display: flex; align-items: center; gap: 8px; width: 100%; text-align: left; border: 0; background: none; padding: 7px 8px; border-radius: 8px; min-width: 0; }
.titem:hover { background: var(--bg3); }
.titem.on { background: var(--sel); }
.titem .tt { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.titem .ago { color: var(--faint); font-size: 11px; flex: none; }
.dot { width: 8px; height: 8px; border-radius: 50%; flex: none; background: var(--faint); }
.dot.running { background: none; border: 2px solid var(--a); border-top-color: transparent; width: 10px; height: 10px; animation: spin .8s linear infinite; }
.dot.waiting { background: var(--warn); }
.dot.review { background: var(--a); }
.dot.done { background: var(--ok); }
.dot.error { background: var(--err); }
.dot.discarded, .dot.idle { background: var(--faint); }
@keyframes spin { to { transform: rotate(360deg); } }
.empty-side { color: var(--dim); padding: 16px 10px; font-size: 13px; }
.sidefoot { border-top: 1px solid var(--line); padding: 10px 12px; display: grid; gap: 8px; }
.sidefoot .row { display: flex; gap: 8px; align-items: center; }
select { background: var(--bg3); border: 1px solid var(--line); border-radius: 8px; padding: 6px 8px; min-width: 0; }
.sidefoot select { flex: 1; }
/* main */
#main { display: flex; flex-direction: column; min-width: 0; min-height: 0; }
#top { display: flex; align-items: center; gap: 10px; padding: 10px 16px; border-bottom: 1px solid var(--line); min-height: 52px; }
#title { flex: 1; min-width: 0; display: flex; align-items: center; gap: 10px; overflow: hidden; }
#title b { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 15px; flex: 0 1 auto; min-width: 60px; }
.badge { font: 11px var(--mono); color: var(--dim); border: 1px solid var(--line); border-radius: 999px; padding: 1px 8px; white-space: nowrap;
  overflow: hidden; text-overflow: ellipsis; flex: 0 1 auto; min-width: 0; max-width: 280px; }
.badge.a { color: var(--a); border-color: color-mix(in srgb, var(--a) 40%, transparent); }
#actions { display: flex; gap: 6px; flex: none; }
#view { flex: 1; overflow: hidden; display: flex; flex-direction: column; min-height: 0; }
/* new task */
.start { flex: 1; overflow: auto; padding: 8vh 24px 24px; }
.start .card { max-width: 760px; margin: 0 auto; }
.start h1 { font-size: 26px; margin: 0 0 6px; letter-spacing: -.3px; }
.start h1 span { color: var(--a); }
.start p.sub { color: var(--dim); margin: 0 0 18px; }
.composer { background: var(--bg2); border: 1px solid var(--line); border-radius: 14px; padding: 12px; }
.composer:focus-within { border-color: color-mix(in srgb, var(--a) 60%, var(--line)); }
.composer textarea { width: 100%; border: 0; outline: 0; background: none; resize: none; min-height: 72px; max-height: 40vh; }
.opts { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; margin-top: 8px; }
.seg { display: inline-flex; border: 1px solid var(--line); border-radius: 8px; overflow: hidden; }
.seg button { border: 0; background: none; padding: 5px 10px; color: var(--dim); display: inline-flex; gap: 6px; align-items: center; }
.seg button.on { background: var(--sel); color: var(--fg); }
.seg button:disabled { opacity: .4; cursor: default; }
.opts .push { margin-left: auto; }
.hint { color: var(--dim); font-size: 12px; margin-top: 8px; }
.recent { margin-top: 28px; }
.recent h3 { font-size: 12px; color: var(--dim); font-weight: 600; margin: 0 0 8px; text-transform: uppercase; letter-spacing: .6px; }
.rc { display: flex; gap: 10px; align-items: center; padding: 10px 12px; border: 1px solid var(--line); border-radius: 10px; margin-bottom: 8px; background: var(--bg2); width: 100%; text-align: left; }
.rc:hover { border-color: var(--faint); }
.rc .tt { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.rc .meta { color: var(--dim); font-size: 12px; }
/* task */
#log { flex: 1; overflow: auto; padding: 18px 20px 12px; }
.lw { max-width: 860px; margin: 0 auto; }
.msg.user { display: flex; justify-content: flex-end; margin: 14px 0; }
.msg.user .bub { background: var(--sel); border-radius: 14px 14px 4px 14px; padding: 9px 13px; max-width: 85%; white-space: pre-wrap; word-break: break-word; }
.msg.user .tag { color: var(--warn); font-size: 11px; margin-right: 8px; align-self: center; }
.md { line-height: 1.6; word-break: break-word; margin: 8px 0; }
.md p { margin: 6px 0; }
.md h1, .md h2, .md h3 { margin: 14px 0 6px; line-height: 1.3; } .md h1 { font-size: 19px; } .md h2 { font-size: 17px; } .md h3 { font-size: 15px; }
.md code { font: 12.5px var(--mono); background: var(--code); border: 1px solid var(--line); border-radius: 5px; padding: 0 4px; }
.md pre { background: var(--code); border: 1px solid var(--line); border-radius: 10px; overflow: hidden; margin: 10px 0; }
.md pre .bar { display: flex; justify-content: space-between; align-items: center; padding: 4px 10px; color: var(--dim); font: 11px var(--mono); border-bottom: 1px solid var(--line); }
.md pre .bar button { border: 0; background: none; color: var(--dim); font-size: 11px; }
.md pre code { display: block; border: 0; padding: 10px 12px; overflow: auto; white-space: pre; font-size: 12.5px; }
.md ul, .md ol { padding-left: 22px; margin: 6px 0; }
.md blockquote { border-left: 3px solid var(--faint); margin: 8px 0; padding: 0 12px; color: var(--dim); }
.md a { color: var(--a); }
details.think { color: var(--dim); margin: 6px 0; font-size: 13px; }
details.think summary { cursor: pointer; list-style: none; display: inline-flex; gap: 6px; align-items: center; }
details.think pre { white-space: pre-wrap; font: 12px var(--mono); max-height: 260px; overflow: auto; margin: 6px 0 0; padding-left: 12px; border-left: 2px solid var(--line); }
.shimmer { background: linear-gradient(90deg, var(--dim) 30%, var(--fg) 50%, var(--dim) 70%); background-size: 200% 100%; -webkit-background-clip: text; background-clip: text; color: transparent; animation: sh 1.6s linear infinite; }
@keyframes sh { from { background-position: 150% 0; } to { background-position: -50% 0; } }
.tool { border: 1px solid var(--line); border-radius: 10px; margin: 6px 0; background: var(--bg2); overflow: hidden; }
.tool .h { display: flex; gap: 8px; align-items: center; padding: 6px 10px; cursor: pointer; min-width: 0; font-size: 13px; }
.tool .h b { font-weight: 600; flex: none; }
.tool .h .lab { color: var(--dim); font: 12px var(--mono); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; flex: 1; min-width: 0; }
.tool .h .t { color: var(--faint); font-size: 11px; flex: none; }
.tool .ic { width: 16px; display: grid; place-items: center; flex: none; }
.tool .st { padding: 0 10px 6px 34px; color: var(--dim); font-size: 12px; }
.tool .body { border-top: 1px solid var(--line); padding: 8px 10px; font: 12px/1.45 var(--mono); white-space: pre-wrap; max-height: 340px; overflow: auto; word-break: break-word; }
.tool.closed .body { display: none; }
.spin { width: 12px; height: 12px; border: 2px solid var(--a); border-top-color: transparent; border-radius: 50%; animation: spin .8s linear infinite; }
.add { color: var(--ok); } .del { color: var(--err); } .hunk { color: var(--a2); }
.info { color: var(--dim); font-size: 13px; margin: 6px 0; }
.info.warn { color: var(--warn); } .info.err { color: var(--err); } .info.ok { color: var(--ok); }
.stats { color: var(--faint); font-size: 12px; margin: 8px 0 18px; display: flex; gap: 6px; align-items: center; }
.ask { border: 1px solid color-mix(in srgb, var(--warn) 50%, var(--line)); border-radius: 10px; padding: 10px 12px; margin: 8px 0; background: var(--bg2); }
.ask pre { font: 12px var(--mono); white-space: pre-wrap; max-height: 200px; overflow: auto; color: var(--dim); margin: 6px 0; }
.ask .btns { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 8px; }
.ask input { background: var(--bg3); border: 1px solid var(--line); border-radius: 8px; padding: 6px 8px; flex: 1; min-width: 160px; }
.todo { border: 1px solid var(--line); border-radius: 10px; padding: 8px 12px; margin: 0 auto 8px; max-width: 860px; background: var(--bg2); font-size: 13px; }
.todo div { display: flex; gap: 8px; } .todo .d { color: var(--dim); text-decoration: line-through; }
.foot { padding: 8px 20px 14px; }
.foot .composer { max-width: 860px; margin: 0 auto; }
.foot textarea { min-height: 24px; }
/* panel */
@media (max-width: 1600px) { body.panel-open #title .badge.wide { display: none; } }
#panel { width: min(520px, 42vw); border-left: 1px solid var(--line); background: var(--bg2); display: flex; flex-direction: column; min-width: 0; }
.ptabs { display: flex; gap: 4px; align-items: center; padding: 8px 10px; border-bottom: 1px solid var(--line); }
.ptabs button.tab { border: 0; background: none; padding: 6px 10px; border-radius: 7px; color: var(--dim); }
.ptabs button.tab.on { background: var(--sel); color: var(--fg); }
.ptabs .x { margin-left: auto; }
#pbody { flex: 1; overflow: auto; padding: 10px 12px; }
.pact { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 10px; }
.fl details { border: 1px solid var(--line); border-radius: 8px; margin-bottom: 6px; background: var(--bg); overflow: hidden; }
.fl summary { display: flex; gap: 8px; align-items: center; padding: 6px 10px; cursor: pointer; font: 12.5px var(--mono); }
.fl summary .p { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.fl pre { margin: 0; padding: 6px 10px; font: 12px/1.45 var(--mono); overflow: auto; max-height: 420px; border-top: 1px solid var(--line); }
.fl .tag { font-size: 10px; border: 1px solid var(--line); border-radius: 4px; padding: 0 5px; color: var(--dim); }
.note { color: var(--dim); font-size: 12px; margin: 6px 0 10px; }
.term { display: flex; gap: 6px; }
.term input { flex: 1; background: var(--bg); border: 1px solid var(--line); border-radius: 8px; padding: 7px 9px; font: 13px var(--mono); min-width: 0; }
.tout { font: 12px/1.45 var(--mono); white-space: pre-wrap; word-break: break-word; margin-top: 10px; }
.tout .cmd { color: var(--a); margin-top: 10px; } .tout .bad { color: var(--err); }
#toast { position: fixed; right: 16px; bottom: 16px; display: grid; gap: 8px; z-index: 50; max-width: min(420px, calc(100vw - 32px)); }
#toast div { background: var(--bg3); border: 1px solid var(--line); border-radius: 10px; padding: 9px 12px; font-size: 13px; white-space: pre-wrap; box-shadow: 0 8px 24px rgba(0,0,0,.25); }
#toast .err { border-color: var(--err); } #toast .ok { border-color: var(--ok); }
.mobile { display: none; }
#scrim { display: none; }
@media (max-width: 860px) {
  #app { grid-template-columns: 1fr; }
  #side { position: fixed; inset: 0 auto 0 0; width: 84vw; max-width: 320px; z-index: 30; transform: translateX(-100%); transition: transform .2s; }
  #side.open { transform: none; }
  #scrim.on { display: block; position: fixed; inset: 0; background: rgba(0,0,0,.45); z-index: 20; }
  #panel { position: fixed; inset: 0; width: 100%; z-index: 40; }
  .mobile { display: inline-grid; }
  #log { padding: 14px 12px; }
  .foot { padding: 8px 10px 12px; }
  .start { padding: 24px 16px; }
  #top { padding: 8px 10px; }
  #actions .wide { display: none; }
}
</style>
</head>
<body>
<div id="app">
  <aside id="side">
    <div class="brand"><span class="logo"></span><b>me studio</b><span class="v" id="ver"></span>
      <button class="ib mobile" onclick="closeSide()" title="close" aria-label="close menu"><svg viewBox="0 0 24 24"><path d="M6 6l12 12M18 6L6 18"/></svg></button></div>
    <button class="btn newtask" onclick="go('')"><svg viewBox="0 0 24 24"><path d="M12 5v14M5 12h14"/></svg>New task</button>
    <div id="projects"></div>
    <div class="sidefoot">
      <button class="btn" onclick="addProject()"><svg viewBox="0 0 24 24"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2zM12 11v5M9.5 13.5h5"/></svg>Add project</button>
      <div class="row">
        <select id="perm" title="permissions" onchange="settings({perm: this.value})">
          <option value="ask">ask for commands</option><option value="full">full access</option><option value="read">read-only</option></select>
        <button class="ib" id="themeBtn" title="theme" onclick="nextTheme()"><svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="8"/><path d="M12 4v16a8 8 0 0 0 0-16z" fill="currentColor"/></svg></button>
        <label class="ib" title="show archived"><input type="checkbox" id="showArch" onchange="renderSide()" style="margin:0"></label>
      </div>
    </div>
  </aside>
  <div id="scrim" onclick="closeSide()"></div>
  <main id="main">
    <header id="top">
      <button class="ib mobile" onclick="openSide()" aria-label="menu"><svg viewBox="0 0 24 24"><path d="M4 7h16M4 12h16M4 17h16"/></svg></button>
      <div id="title"><b>New task</b></div>
      <div id="actions"></div>
    </header>
    <section id="view"></section>
  </main>
  <aside id="panel" class="hidden">
    <div class="ptabs">
      <button class="tab on" data-tab="changes" onclick="panelTab('changes')">Changes</button>
      <button class="tab" data-tab="term" onclick="panelTab('term')">Terminal</button>
      <button class="ib x" onclick="closePanel()" aria-label="close panel"><svg viewBox="0 0 24 24"><path d="M6 6l12 12M18 6L6 18"/></svg></button>
    </div>
    <div id="pbody"></div>
  </aside>
</div>
<div id="toast"></div>
<script>
const KEY = "__KEY__";
const $ = id => document.getElementById(id);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"})[c]);
const human = n => !n ? "0" : n < 1000 ? String(n) : n < 1e6 ? (n / 1000).toFixed(1).replace(".0", "") + "k" : (n / 1e6).toFixed(1).replace(".0", "") + "M";
const dur = s => s < 60 ? s.toFixed(1) + "s" : Math.floor(s / 60) + "m" + String(Math.floor(s % 60)).padStart(2, "0") + "s";
const ago = t => { const s = Date.now() / 1000 - t; return s < 60 ? "now" : s < 3600 ? Math.floor(s / 60) + "m" : s < 86400 ? Math.floor(s / 3600) + "h" : Math.floor(s / 86400) + "d"; };
const I = {
  check: '<svg viewBox="0 0 24 24" style="color:var(--ok)"><path d="M5 12l5 5 9-11"/></svg>',
  x: '<svg viewBox="0 0 24 24" style="color:var(--err)"><path d="M6 6l12 12M18 6L6 18"/></svg>',
  stop: '<svg viewBox="0 0 24 24"><rect x="7" y="7" width="10" height="10" rx="1.5" fill="currentColor"/></svg>',
  diff: '<svg viewBox="0 0 24 24"><path d="M12 3v12M6 9h12M6 21h12"/></svg>',
  term: '<svg viewBox="0 0 24 24"><path d="M4 6l6 6-6 6M12 18h8"/></svg>',
  folder: '<svg viewBox="0 0 24 24"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg>',
  box: '<svg viewBox="0 0 24 24"><path d="M4 8l8-4 8 4v8l-8 4-8-4zM4 8l8 4 8-4M12 12v8"/></svg>',
  send: '<svg viewBox="0 0 24 24"><path d="M12 19V5M5 12l7-7 7 7"/></svg>',
  branch: '<svg viewBox="0 0 24 24"><circle cx="6" cy="6" r="2"/><circle cx="6" cy="18" r="2"/><circle cx="18" cy="8" r="2"/><path d="M6 8v8M18 10c0 4-6 3-12 6"/></svg>',
};

async function api(path, body) {
  const r = await fetch(path, {method: body === undefined ? "GET" : "POST", headers: {"Content-Type": "application/json", "X-Key": KEY},
                               body: body === undefined ? undefined : JSON.stringify(body)});
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.detail || r.statusText);
  return j;
}
function toast(text, cls = "") {
  const d = document.createElement("div"); d.className = cls; d.textContent = text; $("toast").appendChild(d);
  setTimeout(() => d.remove(), cls === "err" ? 9000 : 4000);
}
async function copyText(s) {
  try { if (navigator.clipboard && window.isSecureContext) { await navigator.clipboard.writeText(s); return true; } } catch (e) {}
  const ta = document.createElement("textarea"); ta.value = s; ta.style.cssText = "position:fixed;top:-1000px;opacity:0"; document.body.appendChild(ta);
  ta.select(); let ok = false; try { ok = document.execCommand("copy"); } catch (e) {} ta.remove(); return ok;
}
function copyCode(b) { copyText(b.closest("pre").querySelector("code").innerText).then(ok => { b.textContent = ok ? "copied" : "✗"; setTimeout(() => b.textContent = "copy", 1200); }); }
function md(src) {
  const blocks = [];
  src = src.replace(/```(\w*)\n?([\s\S]*?)(```|$)/g, (_, lang, code) => { blocks.push([lang, code]); return `\u0000${blocks.length - 1}\u0000`; });
  let h = esc(src)
    .replace(/^### (.*)$/gm, "<h3>$1</h3>").replace(/^## (.*)$/gm, "<h2>$1</h2>").replace(/^# (.*)$/gm, "<h1>$1</h1>")
    .replace(/^&gt; (.*)$/gm, "<blockquote>$1</blockquote>")
    .replace(/`([^`\n]+)`/g, "<code>$1</code>").replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>").replace(/(^|\W)\*([^*\n]+)\*/g, "$1<i>$2</i>")
    .replace(/\[([^\]]+)\]\((https?:[^)]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
  h = h.replace(/(?:^|\n)((?:[-*] .*(?:\n|$))+)/g, (_, l) => "\n<ul>" + l.trim().split("\n").map(x => "<li>" + x.slice(2) + "</li>").join("") + "</ul>\n");
  h = h.replace(/(?:^|\n)((?:\d+[.)] .*(?:\n|$))+)/g, (_, l) => "\n<ol>" + l.trim().split("\n").map(x => "<li>" + x.replace(/^\d+[.)] /, "") + "</li>").join("") + "</ol>\n");
  h = h.split(/\n{2,}/).map(p => /^\s*<(h\d|ul|ol|blockquote)/.test(p) || p.startsWith("\u0000") ? p : "<p>" + p.replace(/\n/g, "<br>") + "</p>").join("");
  return h.replace(/\u0000(\d+)\u0000/g, (_, i) => `<pre><div class="bar"><span>${esc(blocks[i][0] || "code")}</span><button onclick="copyCode(this)">copy</button></div><code>${esc(blocks[i][1])}</code></pre>`);
}
function diffHtml(t) {
  return String(t).split("\n").map(l => `<span class="${l.startsWith("+") && !l.startsWith("+++") ? "add" : l.startsWith("-") && !l.startsWith("---") ? "del" : l.startsWith("@@") ? "hunk" : ""}">${esc(l)}</span>`).join("\n");
}

// ─── state ───
let S = {tasks: [], projects: [], models: []}, cur = null, after = 0, poll = 0, live = {}, view = {}, panel = null;
const COLOR_VARS = {bg: "--bg", bg2: "--bg2", bg3: "--bg3", line: "--line", fg: "--fg", dim: "--dim", faint: "--faint", accent: "--a", accent2: "--a2",
  ok: "--ok", err: "--err", warn: "--warn", code_bg: "--code", sel: "--sel"};
function applyTheme() {
  for (const [k, v] of Object.entries(S.colors || {})) if (COLOR_VARS[k]) document.documentElement.style.setProperty(COLOR_VARS[k], v);
  document.documentElement.style.colorScheme = S.dark ? "dark" : "light";
}
async function refresh() {
  try { S = await api("/api/state"); } catch (e) { return; }
  applyTheme();
  $("ver").textContent = "v" + S.version;
  $("perm").value = S.perm;
  renderSide();
  const sig = JSON.stringify([S.projects, S.tasks.filter(t => !t.archived).slice(0, 6).map(t => [t.id, t.status, t.busy, t.title])]);
  if (!cur && !view.dirtyStart && sig !== view.sig) { view.sig = sig; renderStart(false); }
  if (cur) renderHeader();
}
const task = id => S.tasks.find(t => t.id === id);
function renderSide() {
  const arch = $("showArch").checked;
  const byP = {};
  for (const p of S.projects) byP[p.path] = [];
  for (const t of S.tasks) if (arch || !t.archived) (byP[t.project] = byP[t.project] || []).push(t);
  const proj = p => S.projects.find(x => x.path === p) || {path: p, name: p.split(/[\\/]/).pop() || p};
  $("projects").innerHTML = Object.keys(byP).length ? Object.entries(byP).map(([p, ts]) => `<div class="proj">
      <div class="ph" title="${esc(p)}">${I.folder}<span>${esc(proj(p).name)}</span>
        <button class="ib" title="new task here" onclick="go('', '${esc(p.replace(/\\/g, "\\\\"))}')"><svg viewBox="0 0 24 24"><path d="M12 5v14M5 12h14"/></svg></button></div>
      ${ts.map(t => `<button class="titem ${t.id === cur ? "on" : ""}" onclick="go('${t.id}')" title="${esc(t.title)}">
          <span class="dot ${t.busy ? (t.status === "waiting" ? "waiting" : "running") : t.status}"></span><span class="tt">${esc(t.title)}</span>
          ${t.mode === "worktree" ? `<span class="ago" title="${esc(t.branch || "")}">⑂</span>` : ""}<span class="ago">${ago(t.updated)}</span></button>`).join("")
        || `<div class="empty-side">no tasks yet</div>`}</div>`).join("")
    : `<div class="empty-side">Add a project folder, then describe a task. Each task is an agent of its own; run several at once.</div>`;
}
function openSide() { $("side").classList.add("open"); $("scrim").classList.add("on"); }
function closeSide() { $("side").classList.remove("open"); $("scrim").classList.remove("on"); }
async function pickFolder(start) {
  const native = window.pywebview && window.pywebview.api && window.pywebview.api.pick_folder;
  return native ? await window.pywebview.api.pick_folder(start || "") : prompt("Project folder (full path)", start || "");
}
async function addProject() {
  const p = await pickFolder(""); if (!p) return;
  try { await api("/api/project/add", {path: p}); await refresh(); toast("project added", "ok"); } catch (e) { toast(e.message, "err"); }
}
async function settings(b) { try { S = await api("/api/settings", b); applyTheme(); renderSide(); } catch (e) { toast(e.message, "err"); } }
function nextTheme() { const ts = S.themes || []; settings({theme: ts[(ts.indexOf(S.theme) + 1) % ts.length]}); }

// ─── routing ───
function go(id, project) {
  closeSide();
  cur = id || null; poll++; after = 0; live = {}; view = {project};
  history.replaceState(null, "", id ? "#" + id : "#");
  if (cur) openTask(cur); else renderStart(true);
  renderSide();
  if (panel && cur) panelTab(panel); else closePanel();
}

// ─── new task ───
function modelOptions(sel) {
  return `<option value="">${esc(S.model ? "model: " + S.model : "default model")}</option>` + S.models.map(m => `<option value="${esc(m.name)}" ${m.name === sel ? "selected" : ""}>${esc(m.name)}</option>`).join("");
}
const EFFORTS = ["", "none", "minimal", "low", "medium", "high", "xhigh"];
const effortOptions = sel => EFFORTS.map(e => `<option value="${e}" ${e === (sel || "") ? "selected" : ""}>${e ? "effort: " + e : "effort: default"}</option>`).join("");
function renderStart(force) {
  if (!force && document.activeElement && document.activeElement.id === "prompt") return;
  $("title").innerHTML = "<b>New task</b>"; $("actions").innerHTML = "";
  const projects = S.projects;
  const p = view.project || (projects[0] && projects[0].path) || "";
  const isGit = (projects.find(x => x.path === p) || {}).git;
  const mode = view.mode || (isGit ? "worktree" : "local");
  const draft = $("prompt") ? $("prompt").value : (view.draft || "");
  const recent = S.tasks.filter(t => !t.archived).slice(0, 6);
  $("view").innerHTML = `<div class="start"><div class="card">
    <h1>What should we <span>build</span>?</h1>
    <p class="sub">Each task runs as its own agent. With <b>worktree</b> it works on a separate branch, so tasks run side by side and your checkout stays untouched until you apply the result.</p>
    <div class="composer"><textarea id="prompt" placeholder="Describe a task — fix a bug, add a feature, review the code…" rows="3">${esc(draft)}</textarea>
      <div class="opts">
        <select id="sproj">${projects.map(x => `<option value="${esc(x.path)}" ${x.path === p ? "selected" : ""}>${esc(x.name)}</option>`).join("")}<option value="+">+ add project…</option></select>
        <span class="seg"><button class="${mode === "local" ? "on" : ""}" onclick="view.mode='local';renderStart(true)" title="work in the project folder itself">${I.folder}local</button><button class="${mode === "worktree" ? "on" : ""}" ${isGit ? "" : "disabled"} onclick="view.mode='worktree';renderStart(true)" title="${isGit ? "a git worktree on its own branch" : "needs a git repository"}">${I.branch}worktree</button></span>
        <select id="smodel">${modelOptions(view.model)}</select><select id="seffort">${effortOptions(view.effort)}</select>
        <button class="btn pri push" id="startBtn" onclick="startTask()" ${p ? "" : "disabled"}>${I.send}Start</button>
      </div></div>
    <div class="hint">${p ? `in <b>${esc(p)}</b> · ` : "add a project first · "}${mode === "worktree" ? "starts from the last commit (uncommitted changes are not included) · " : ""}ctrl+enter to start</div>
    ${recent.length ? `<div class="recent"><h3>Recent</h3>${recent.map(t => `<button class="rc" onclick="go('${t.id}')"><span class="dot ${t.busy ? "running" : t.status}"></span><span class="tt">${esc(t.title)}</span><span class="meta">${esc((t.project || "").split(/[\\/]/).pop())} · ${ago(t.updated)}</span></button>`).join("")}</div>` : ""}
  </div></div>`;
  const ta = $("prompt");
  ta.oninput = () => { view.draft = ta.value; view.dirtyStart = !!ta.value; grow(ta); };
  ta.onkeydown = e => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); startTask(); } };
  $("sproj").onchange = async e => {
    if (e.target.value === "+") { await addProject(); view.project = S.projects[0] && S.projects[0].path; } else view.project = e.target.value;
    view.mode = null; renderStart(true);
  };
  $("smodel").onchange = e => view.model = e.target.value;
  $("seffort").onchange = e => view.effort = e.target.value;
  grow(ta); if (force) ta.focus();
}
function grow(ta) { ta.style.height = "auto"; ta.style.height = Math.min(ta.scrollHeight, innerHeight * 0.4) + "px"; }
async function startTask() {
  const prompt = $("prompt").value.trim(), project = $("sproj") && $("sproj").value;
  if (!prompt || !project || project === "+") return toast(prompt ? "pick a project" : "describe the task first", "err");
  const isGit = (S.projects.find(x => x.path === project) || {}).git;
  const mode = view.mode || (isGit ? "worktree" : "local");
  $("startBtn").disabled = true;
  try {
    const r = await api("/api/task/new", {project, prompt, mode, model: $("smodel").value, effort: $("seffort").value});
    view.draft = ""; view.dirtyStart = false;
    await refresh(); go(r.id);
  } catch (e) { toast(e.message, "err"); $("startBtn").disabled = false; }
}

// ─── task view ───
function renderHeader() {
  const t = task(cur); if (!t) return;
  const st = t.busy ? (t.status === "waiting" ? "waiting for you" : "working") : ({review: "ready to review", done: "done", error: "error", idle: "idle", discarded: "discarded"})[t.status] || t.status;
  $("title").innerHTML = `<b ondblclick="renameTask()" title="${esc(t.title)}">${esc(t.title)}</b>
    <span class="badge wide">${esc((t.project || "").split(/[\\/]/).pop())}</span>
    ${t.mode === "worktree" ? `<span class="badge a wide" title="worktree branch">⑂ ${esc(t.branch || "")}</span>` : `<span class="badge wide">local</span>`}
    <span class="badge">${esc(st)}</span>`;
  $("actions").innerHTML = `${t.busy ? `<button class="btn sm danger" onclick="stopTask()">${I.stop}Stop</button>` : ""}
    <button class="btn sm" onclick="panelTab('changes')" title="review changes">${I.diff}<span class="wide">Review</span>${view.files != null ? ` <b>${view.files}</b>` : ""}</button>
    <button class="ib" onclick="panelTab('term')" title="terminal">${I.term}</button>
    <button class="ib wide" onclick="api('/api/open', {id: cur}).catch(e => toast(e.message, 'err'))" title="open folder">${I.folder}</button>
    <button class="ib wide" onclick="archiveTask()" title="${t.archived ? "unarchive" : "archive"}">${I.box}</button>`;
}
async function openTask(id) {
  $("view").innerHTML = `<div id="log"><div class="lw" id="lw"></div></div><div id="todo"></div><div class="foot"><div class="composer">
      <textarea id="msg" rows="1" placeholder="Reply, steer, or ask for changes — enter to send"></textarea>
      <div class="opts"><select id="tmodel">${modelOptions((task(id) || {}).model)}</select><select id="teffort">${effortOptions((task(id) || {}).effort)}</select>
        <button class="btn pri sm push" onclick="sendMsg()">${I.send}Send</button></div></div></div>`;
  const ta = $("msg");
  ta.oninput = () => grow(ta);
  ta.onkeydown = e => { if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); sendMsg(); } };
  $("tmodel").onchange = e => api("/api/task/model", {id: cur, model: e.target.value}).catch(er => toast(er.message, "err"));
  $("teffort").onchange = e => api("/api/task/model", {id: cur, effort: e.target.value}).catch(er => toast(er.message, "err"));
  renderHeader();
  let d;
  try { d = await api("/api/task?id=" + encodeURIComponent(id)); } catch (e) { return toast(e.message, "err"); }
  if (cur !== id) return;
  renderHistory(d.messages); renderTodo(d.todo);
  (d.pending || []).forEach(onEvent);
  after = d.seq; refreshDiffCount();
  const my = ++poll;
  while (my === poll && cur === id) {
    try {
      const r = await api(`/api/events?id=${encodeURIComponent(id)}&after=${after}`);
      if (my !== poll) return;
      for (const e of r.events) { after = Math.max(after, e.seq); onEvent(e); }
    } catch (e) { await new Promise(r => setTimeout(r, 1500)); }
  }
}
function lw() { return $("lw"); }
function atBottom() { const l = $("log"); return !l || l.scrollHeight - l.scrollTop - l.clientHeight < 120; }
function add(html) {
  const stick = atBottom(); const d = document.createElement("div"); d.innerHTML = html.trim(); const n = d.firstChild;
  if (lw()) lw().appendChild(n); if (stick) $("log").scrollTop = $("log").scrollHeight; return n;
}
function scrollDown(force) { const l = $("log"); if (l && (force || atBottom())) l.scrollTop = l.scrollHeight; }
const txt = c => Array.isArray(c) ? c.filter(x => x.type === "text").map(x => x.text).join(" ") : (c || "");
function userMsg(text, tag) { add(`<div class="msg user">${tag ? `<span class="tag">${esc(tag)}</span>` : ""}<div class="bub">${esc(text)}</div></div>`); }
function toolCard(name, label) {
  return add(`<div class="tool closed"><div class="h" onclick="this.parentNode.classList.toggle('closed')"><span class="ic"><span class="spin"></span></span><b>${esc(name)}</b><span class="lab">${esc(label || "")}</span><span class="t"></span></div><div class="st hidden"></div><div class="body"></div></div>`);
}
function finishCard(c, ok, out, time) {
  c.querySelector(".ic").innerHTML = ok ? I.check : I.x; if (time != null) c.querySelector(".t").textContent = dur(time);
  c.querySelector(".st").classList.add("hidden");
  const b = c.querySelector(".body"); if (!b.innerHTML) b.textContent = String(out || "").slice(0, 20000);
  if (!ok) c.classList.remove("closed");
}
function renderHistory(msgs) {
  lw().innerHTML = "";
  const outs = {};
  msgs.forEach(m => { if (m.role === "tool") outs[m.tool_call_id] = m.content; });
  msgs.forEach(m => {
    if (m.role === "user") {
      const t = txt(m.content);
      if (m._aux) { if (/^\[message from the user/.test(t)) userMsg(t.split("\n").slice(1).join("\n"), "while working"); return; }
      if (t && t !== "(images from read)") userMsg(t);
    } else if (m.role === "assistant") {
      if (m.content) add(`<div class="md">${md(m.content)}</div>`);
      (m.tool_calls || []).forEach(tc => {
        let a = {}; try { a = JSON.parse(tc.function.arguments); } catch (e) {}
        const c = toolCard(tc.function.name, a.path || a.cmd || a.pattern || a.query || a.url || a.name || a.prompt || "");
        const o = outs[tc.id]; finishCard(c, !/^error/.test(o || ""), o);
      });
    }
  });
  scrollDown(true);
}
function renderTodo(items) {
  const t = $("todo"); if (!t) return;
  const open = (items || []).filter(i => i.s !== "done").length;
  t.innerHTML = items && items.length && open ? `<div class="todo">${items.map(i => `<div class="${i.s === "done" ? "d" : ""}">${i.s === "done" ? "✓" : i.s === "doing" ? "▸" : "○"} ${esc(i.t)}</div>`).join("")}</div>` : "";
}
let raf = 0;
function onEvent(e) {
  const t = e.type;
  if (t === "user") { userMsg(e.text); return; }
  if (t === "queued") { userMsg(e.text, "queued"); return; }
  if (t === "turn_start") { renderHeader(); return; }
  if (t === "reasoning") {
    if (!live.think) { live.think = add(`<details class="think"><summary><span class="shimmer">thinking…</span></summary><pre></pre></details>`); live.thinkT = Date.now(); }
    live.think.querySelector("pre").textContent += e.text; scrollDown(); return;
  }
  const endThink = () => { if (live.think && !live.thinkDone) { live.think.querySelector("summary").textContent = "thought for " + dur((Date.now() - live.thinkT) / 1000); live.thinkDone = true; } };
  if (t === "text") {
    endThink();
    if (!live.text) { live.text = add(`<div class="md"></div>`); live.raw = ""; }
    live.raw += e.text;
    if (!raf) raf = requestAnimationFrame(() => { raf = 0; if (live.text) { live.text.innerHTML = md(live.raw); scrollDown(); } });
    return;
  }
  if (t === "tool_start") {
    endThink(); live.text = null; live.think = null; live.thinkDone = false;
    live[e.id] = toolCard(e.cli ? e.cli.label : e.name, e.label); return;
  }
  if (t === "tool_event") {
    const c = live[e.id]; if (!c) return;
    if (e.t === "status") { const s = c.querySelector(".st"); s.classList.remove("hidden"); s.textContent = e.text; }
    else if (e.t === "diff" && (e.text || "").trim()) { c.querySelector(".body").innerHTML = diffHtml(e.text); c.classList.remove("closed"); }
    else if (e.t === "log") { const s = c.querySelector(".st"); s.classList.remove("hidden"); s.textContent = "› " + e.text; }
    scrollDown(); return;
  }
  if (t === "tool_end") { const c = live[e.id]; if (c) { delete live[e.id]; finishCard(c, e.ok, e.output, e.time); } return; }
  if (t === "confirm") {
    const c = add(`<div class="ask"><b>Allow ${esc(e.name)}?</b><pre>${esc(JSON.stringify(e.args, null, 1)).slice(0, 3000)}</pre><div class="btns"><button class="btn pri sm">Allow</button><button class="btn sm">${e.name === "bash" ? "Always allow this command" : "Allow all (full access)"}</button><button class="btn sm danger">Deny</button></div></div>`);
    const [y, al, n] = c.querySelectorAll("button");
    const ans = v => { api("/api/task/confirm", {id: cur, rid: e.id, ok: v}); c.innerHTML = `<span class="info">${esc(e.name)} → ${v ? (v === "always" ? "always allowed" : "allowed") : "denied"}</span>`; };
    y.onclick = () => ans(true); al.onclick = () => ans("always"); n.onclick = () => ans(false);
    return;
  }
  if (t === "ask") {
    const opts = e.options || [];
    const c = add(`<div class="ask"><b>${esc(e.text)}</b><div class="btns">${opts.length ? opts.map(o => `<button class="btn sm" data-v="${esc(o)}">${esc(o)}</button>`).join("") : `<input placeholder="answer…"><button class="btn pri sm" data-in="1">Send</button>`}</div></div>`);
    const send = v => { api("/api/task/answer", {id: cur, rid: e.id, text: v}); c.innerHTML = `<span class="info">${esc(e.text)} → <b>${esc(v || "—")}</b></span>`; };
    c.querySelectorAll("button").forEach(b => b.onclick = () => send(b.dataset.in ? c.querySelector("input").value : b.dataset.v));
    const inp = c.querySelector("input"); if (inp) inp.onkeydown = ev => ev.key === "Enter" && send(inp.value);
    return;
  }
  if (t === "retry") {
    if (e.discard) { live.text && live.text.remove(); live.think && live.think.remove(); live.text = live.think = null; live.thinkDone = false; }
    add(`<div class="info warn">↻ ${esc(String(e.error || "").split("\n")[0].slice(0, 160))} — retrying</div>`); return;
  }
  if (t === "compacting") { add(`<div class="info">compacting history…</div>`); return; }
  if (t === "compacted") { add(`<div class="info">history compacted · ~${human(e.ctx)} tokens</div>`); return; }
  if (t === "verify") { add(`<div class="info">self-check: running checks before finishing</div>`); return; }
  if (t === "error") { add(`<div class="info ${e.text === "interrupted" ? "warn" : "err"}">${esc(e.text)}</div>`); return; }
  if (t === "todo") { renderTodo(e.items); return; }
  if (t === "turn_diff") {
    const fs = e.files || []; if (!fs.length) return;
    add(`<div class="info">Δ ${fs.length} file${fs.length > 1 ? "s" : ""} changed · <a href="#" onclick="panelTab('changes');return false">review</a></div>`);
    refreshDiffCount(); return;
  }
  if (t === "turn_end") {
    endThink(); live = {};
    const steps = e.steps || 1;
    add(`<div class="stats">${dur(e.time)} · ${steps} step${steps > 1 ? "s" : ""} · context ${human(e.ctx || 0)} · ↑${human(e.in)} ↓${human(e.out)}${e.cost ? " · $" + e.cost.toFixed(4) : ""}</div>`);
    refreshDiffCount(); refresh(); return;
  }
  if (t === "status") { refresh(); if (panel === "changes") panelTab("changes"); return; }
}
async function sendMsg() {
  const ta = $("msg"), text = ta.value.trim(); if (!text) return;
  ta.value = ""; grow(ta);
  try { await api("/api/task/send", {id: cur, text}); refresh(); } catch (e) { toast(e.message, "err"); }
}
async function stopTask() { try { await api("/api/task/stop", {id: cur}); } catch (e) { toast(e.message, "err"); } }
async function archiveTask() { try { await api("/api/task/archive", {id: cur}); await refresh(); } catch (e) { toast(e.message, "err"); } }
async function renameTask() {
  const t = task(cur); const v = prompt("Task name", t.title); if (!v) return;
  await api("/api/task/rename", {id: cur, title: v}); refresh();
}
async function refreshDiffCount() {
  if (!cur) return;
  try { const d = await api("/api/diff?id=" + encodeURIComponent(cur)); view.files = d.files.length; renderHeader(); } catch (e) {}
}

// ─── review / terminal panel ───
function closePanel() { panel = null; $("panel").classList.add("hidden"); document.body.classList.remove("panel-open"); }
async function panelTab(tab) {
  if (!cur) return;
  panel = tab; $("panel").classList.remove("hidden"); document.body.classList.add("panel-open");
  document.querySelectorAll(".ptabs .tab").forEach(b => b.classList.toggle("on", b.dataset.tab === tab));
  if (tab === "term") return renderTerm();
  const t = task(cur) || {};
  $("pbody").innerHTML = `<div class="note">loading changes…</div>`;
  let d; try { d = await api("/api/diff?id=" + encodeURIComponent(cur)); } catch (e) { $("pbody").innerHTML = `<div class="note">${esc(e.message)}</div>`; return; }
  view.files = d.files.length; renderHeader();
  const adds = d.files.reduce((a, f) => a + f.add, 0), dels = d.files.reduce((a, f) => a + f.del, 0);
  const wt = t.mode === "worktree";
  $("pbody").innerHTML = `<div class="pact">
      ${wt ? `<button class="btn sm pri" onclick="act('apply')" ${d.files.length ? "" : "disabled"}>Apply to project</button>` : `<button class="btn sm" onclick="act('undo')">Undo last turn</button>`}
      <button class="btn sm" onclick="act('commit')" ${d.files.length || !wt ? "" : "disabled"}>Commit…</button>
      ${wt ? `<button class="btn sm" onclick="act('push')">Push branch</button><button class="btn sm danger" onclick="act('discard')">Discard</button>` : ""}
    </div>
    <div class="note">${d.files.length ? `${d.files.length} file${d.files.length > 1 ? "s" : ""} · <span class="add">+${adds}</span> <span class="del">−${dels}</span>` : "no changes yet"}${wt ? ` · branch <b>${esc(d.branch || t.branch || "")}</b>${d.commits ? ` · ${d.commits} commit${d.commits > 1 ? "s" : ""}` : ""}` : ""}${d.note ? `<br>${esc(d.note)}` : ""}</div>
    <div class="fl">${d.files.map((f, i) => `<details ${i < 3 && f.add + f.del < 400 ? "open" : ""}><summary><span class="p" title="${esc(f.path)}">${esc(f.path)}</span>${f.new ? `<span class="tag">new</span>` : ""}${f.deleted ? `<span class="tag">deleted</span>` : ""}<span class="add">+${f.add}</span><span class="del">−${f.del}</span></summary><pre>${f.binary ? "binary file" : diffHtml(f.diff)}</pre></details>`).join("")}</div>`;
}
async function act(kind) {
  const t = task(cur) || {};
  let body = {id: cur};
  if (kind === "commit") { const m = prompt("Commit message", t.title); if (!m) return; body.message = m; }
  if (kind === "discard" && !confirm("Discard this task's worktree and branch? Its changes are lost.")) return;
  try {
    const r = await api(`/api/task/${kind}`, body);
    toast(r.text || "done", "ok");
    await refresh(); if (kind === "discard") go(""); else panelTab("changes");
  } catch (e) { toast(e.message, "err"); }
}
function renderTerm() {
  const t = task(cur) || {};
  if (!$("tcmd")) {
    $("pbody").innerHTML = `<div class="note">runs in ${t.mode === "worktree" ? "the task's worktree" : "the project folder"}</div>
      <div class="term"><input id="tcmd" placeholder="command, e.g. npm test" autocomplete="off"><button class="btn sm" onclick="runCmd()">Run</button></div><div class="tout" id="tout"></div>`;
    $("tcmd").onkeydown = e => { if (e.key === "Enter") runCmd(); };
  }
  $("tcmd").focus();
}
async function runCmd() {
  const inp = $("tcmd"), cmd = inp.value.trim(); if (!cmd) return; inp.value = "";
  const out = $("tout"), box = document.createElement("div");
  box.innerHTML = `<div class="cmd">$ ${esc(cmd)}</div><div class="o">…</div>`; out.appendChild(box);
  try { const r = await api("/api/term", {id: cur, cmd}); const o = box.querySelector(".o"); o.textContent = r.output || "(no output)"; if (!r.ok) o.classList.add("bad"); }
  catch (e) { box.querySelector(".o").textContent = e.message; }
  $("pbody").scrollTop = $("pbody").scrollHeight;
}

document.addEventListener("keydown", e => { if (e.key === "Escape") { if (panel) closePanel(); else closeSide(); } });
(async () => {
  await refresh();
  const id = location.hash.slice(1);
  go(id && task(id) ? id : "");
  setInterval(refresh, 1500);
})();
</script>
</body>
</html>
"""

if __name__ == "__main__":
    main()
