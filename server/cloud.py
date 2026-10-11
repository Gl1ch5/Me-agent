"""Cloud side of me: workspaces (synced project files) and the sandboxed agent runner.

Each running cloud session is `me.py --rpc` inside bubblewrap:
  - read-only system, private /tmp, no access to other users or the server's data
  - writable: the user's workspace (/work) and a per-user home (/home/me)
  - memory/process/file-size limits via prlimit, low CPU priority
The worker is just another "device" of the user: it syncs config/skills/sessions through the normal API.
"""
import base64, collections, hashlib, importlib.util, json, os, re, secrets, shutil, subprocess, threading, time, uuid
from pathlib import Path

DATA = Path(os.environ.get("ME_DATA", "/var/lib/me-agent"))
CLIENT = Path(os.environ.get("ME_CLIENT", Path(__file__).resolve().parent.parent / "me.py"))
API = os.environ.get("ME_INTERNAL_URL", "http://127.0.0.1:8790")
MAX_WORKERS = int(os.environ.get("ME_MAX_WORKERS", "6"))
IDLE_SECS = int(os.environ.get("ME_IDLE_SECS", "900"))
PER_USER = int(os.environ.get("ME_PER_USER", "3"))
WS_QUOTA = 100 * 1024 * 1024
SANDBOX = shutil.which("bwrap") and os.environ.get("ME_SANDBOX", "1") == "1"

_spec = importlib.util.spec_from_file_location("me_client", CLIENT)
me = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(me)  # reuse the client's exact file rules (ws_scan)


def slug(s):
    return re.sub(r"[^a-z0-9-]+", "-", s.lower()).strip("-")[:40] or "project"


# ─── workspaces ───


def ws_root(uid, wid):
    if not re.fullmatch(r"[a-z0-9-]{1,60}", wid):
        raise ValueError("bad workspace id")
    return DATA / "ws" / str(uid) / wid


def ws_meta_path(uid):
    return DATA / "ws" / str(uid) / "_index.json"


def ws_list(uid):
    idx = me.jload(ws_meta_path(uid), {}) or {}
    out = []
    for wid, m in idx.items():
        root = ws_root(uid, wid)
        files = me.ws_scan(root) if root.exists() else {}
        size = sum((root / p).stat().st_size for p in files if (root / p).exists())
        out.append({"id": wid, "name": m["name"], "files": len(files), "bytes": size, "created": m.get("created"),
                    "updated": max([t for _, t in files.values()] or [m.get("created", 0)])})
    return sorted(out, key=lambda w: -w["updated"])


def ws_create(uid, name):
    idx = me.jload(ws_meta_path(uid), {}) or {}
    for wid, m in idx.items():
        if m["name"] == name:
            return {"id": wid, "name": name}
    wid = slug(name) + "-" + secrets.token_hex(2)
    idx[wid] = {"name": name, "created": time.time()}
    ws_root(uid, wid).mkdir(parents=True, exist_ok=True)
    me.jsave(ws_meta_path(uid), idx)
    return {"id": wid, "name": name}


def ws_delete(uid, wid):
    idx = me.jload(ws_meta_path(uid), {}) or {}
    idx.pop(wid, None)
    me.jsave(ws_meta_path(uid), idx)
    shutil.rmtree(ws_root(uid, wid), ignore_errors=True)


def ws_exists(uid, wid):
    return wid in (me.jload(ws_meta_path(uid), {}) or {})


def safe_path(root, rel):
    p = (root / rel).resolve()
    if not str(p).startswith(str(root.resolve()) + os.sep):
        raise ValueError("bad path")
    return p


def ws_manifest(uid, wid):
    root = ws_root(uid, wid)
    return {p: [h, 0, t] for p, (h, t) in me.ws_scan(root).items()} if root.exists() else {}


def ws_put(uid, wid, put, delete):
    root = ws_root(uid, wid)
    root.mkdir(parents=True, exist_ok=True)
    used = sum(f.stat().st_size for f in root.rglob("*") if f.is_file())
    for f in put:
        data = base64.b64decode(f["b64"])
        if len(data) > me.WS_MAX_FILE:
            continue
        used += len(data)
        if used > WS_QUOTA:
            raise ValueError("workspace quota (100 MB) exceeded")
        p = safe_path(root, f["path"])
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        if f.get("mtime"):
            os.utime(p, (f["mtime"], f["mtime"]))
    for rel in delete:
        try:
            safe_path(root, rel).unlink()
        except (OSError, ValueError):
            pass


def ws_get(uid, wid, paths):
    root = ws_root(uid, wid)
    out = []
    for rel in paths:
        try:
            p = safe_path(root, rel)
            out.append({"path": rel, "b64": base64.b64encode(p.read_bytes()).decode(), "mtime": p.stat().st_mtime})
        except (OSError, ValueError):
            pass
    return out


# ─── runner ───


class Worker:
    def __init__(self, uid, username, token, wid, session=None):
        self.id = uuid.uuid4().hex[:12]
        self.uid, self.username, self.wid = uid, username, wid
        self.events = collections.deque(maxlen=800)
        self.seq = 0
        self.cond = threading.Condition()
        self.busy = False
        self.session = session
        self.title = ""
        self.last = time.time()
        self.started = time.time()
        home = DATA / "run" / str(uid) / "home"
        home.mkdir(parents=True, exist_ok=True)
        me.jsave(home / ".me" / "auth.json", {"server": API, "token": token, "user": username})
        work = ws_root(uid, wid)
        work.mkdir(parents=True, exist_ok=True)
        env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8", "PYTHONUNBUFFERED": "1", "ME_YOLO": "1", "ME_WS_LOCAL": "1",
               "ME_WS_ID": wid, "ME_SERVER": API, "TERM": "dumb"}
        inner = ["python3", "-I", "/opt/me.py" if SANDBOX else str(CLIENT), "--rpc"]
        limits = ["prlimit", "--as=1500000000", "--nproc=256", "--fsize=200000000", "--nofile=512", "--", "nice", "-n", "10"]
        if SANDBOX:
            ro = [x for d in ("/usr", "/lib", "/lib64", "/bin", "/sbin", "/etc") if os.path.exists(d) for x in ("--ro-bind", d, d)]
            cmd = ["bwrap", *ro, "--ro-bind", str(CLIENT), "/opt/me.py", "--bind", str(home), "/home/me", "--bind", str(work), "/work",
                   "--tmpfs", "/tmp", "--proc", "/proc", "--dev", "/dev", "--unshare-all", "--share-net", "--die-with-parent",
                   "--new-session", "--chdir", "/work", "--setenv", "HOME", "/home/me", "--setenv", "ME_HOME", "/home/me/.me", *limits, *inner]
        else:
            env.update(HOME=str(home), ME_HOME=str(home / ".me"))
            cmd = [*limits, *inner]
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
                                     cwd=str(work), text=True, encoding="utf-8", bufsize=1)
        threading.Thread(target=self._read, daemon=True).start()
        threading.Thread(target=self._err, daemon=True).start()
        if session:
            self.send({"type": "load", "id": session})

    def _err(self):
        for line in self.proc.stderr:
            if line.strip():
                self._push({"type": "log", "text": line.rstrip()[:500]})

    def _push(self, e):
        with self.cond:
            self.seq += 1
            e["seq"] = self.seq
            self.events.append(e)
            self.cond.notify_all()

    def _read(self):
        for line in self.proc.stdout:
            try:
                e = json.loads(line)
            except ValueError:
                continue
            t = e.get("type")
            if t in ("ready", "loaded") and e.get("session"):
                self.session = e["session"]
                self.title = e.get("title") or self.title
            elif t == "turn_start":
                self.busy = True
            elif t == "turn_end":
                self.busy = False
            elif t == "confirm":  # sandboxed; auto-approve (ME_YOLO also does this)
                self.send({"type": "confirm", "id": e["id"], "ok": True})
            self.last = time.time()
            self._push(e)
        self.busy = False
        self._push({"type": "exit"})

    def send(self, cmd):
        self.last = time.time()
        try:
            self.proc.stdin.write(json.dumps(cmd) + "\n")
            self.proc.stdin.flush()
        except (OSError, ValueError):
            pass

    def prompt(self, text):
        if not self.title:
            self.title = text.strip().split("\n")[0][:60]
        self._push({"type": "user", "text": text})
        self.send({"type": "prompt", "text": text})

    @property
    def alive(self):
        return self.proc.poll() is None

    def stop(self):
        try:
            self.proc.kill()
        except OSError:
            pass

    def wait_events(self, after, timeout=25):
        with self.cond:
            if self.seq <= after:
                self.cond.wait(timeout)
            return [e for e in self.events if e["seq"] > after]

    def info(self):
        return {"id": self.id, "session": self.session, "ws": self.wid, "busy": self.busy, "title": self.title,
                "started": self.started, "last": self.last, "alive": self.alive}


class Runner:
    def __init__(self):
        self.workers = {}
        self.lock = threading.Lock()
        threading.Thread(target=self._reaper, daemon=True).start()

    def _reaper(self):
        while True:
            time.sleep(30)
            with self.lock:
                for wid, w in list(self.workers.items()):
                    if not w.alive or (not w.busy and time.time() - w.last > IDLE_SECS):
                        w.stop()
                        self.workers.pop(wid, None)

    def of_user(self, uid):
        return [w for w in self.workers.values() if w.uid == uid and w.alive]

    def get(self, uid, wid):
        w = self.workers.get(wid)
        if not w or w.uid != uid:
            raise KeyError("no such cloud session (it may have stopped after being idle)")
        return w

    def start(self, uid, username, token_fn, wid=None, session=None):
        with self.lock:
            for w in self.of_user(uid):  # reuse a worker already on this session
                if session and w.session == session:
                    return w
            alive = [w for w in self.workers.values() if w.alive]
            if len(alive) >= MAX_WORKERS:
                idle = sorted((w for w in alive if not w.busy), key=lambda w: w.last)
                if not idle:
                    raise RuntimeError("cloud is busy — all sandboxes are working, try again in a minute")
                idle[0].stop()
                self.workers.pop(idle[0].id, None)
            mine = self.of_user(uid)
            if len(mine) >= PER_USER:  # make room, but never interrupt a sandbox that is working
                idle = sorted((w for w in mine if not w.busy), key=lambda w: w.last)
                if not idle:
                    raise RuntimeError(f"you already have {PER_USER} sandboxes working — wait for one to finish")
                idle[0].stop()
                self.workers.pop(idle[0].id, None)
            if not wid or not ws_exists(uid, wid):
                wid = ws_create(uid, "cloud")["id"] if not wid else ws_create(uid, wid)["id"]
            w = Worker(uid, username, token_fn(), wid, session)
            self.workers[w.id] = w
            return w


runner = Runner()
