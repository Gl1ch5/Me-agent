"""Cloud side of me: workspaces (synced project files) and the sandboxed agent runner.

Each running cloud session is `me.py --rpc` inside bubblewrap:
  - read-only system, private /tmp, no access to other users or the server's data
  - writable: the user's workspace (/work) and a per-user home (/home/me)
  - memory/process/file-size limits via prlimit, low CPU priority
The worker is just another "device" of the user: it syncs config/skills/sessions through the normal API.
"""
import base64, collections, hashlib, importlib.util, json, os, re, secrets, shutil, subprocess, threading, time, uuid
from pathlib import Path

import acctlog

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
        out.append({"id": wid, "name": m["name"], "files": len(files), "bytes": size, "created": m.get("created"), "repo": m.get("repo"),
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
    """One sandbox. I/O goes through files, not pipes, so the sandbox survives a server restart (KillMode=process):
    commands → FIFO `in` (opened O_RDWR inside, never EOF), events/stderr → append-only `out` (tailed here).
    A small `sh` shepherd is bwrap's parent, so --die-with-parent fires when we kill it, not when uvicorn restarts."""

    def __init__(self, uid, username, token, wid, session=None, meta=None):
        self.events = collections.deque(maxlen=800)
        self.cond = threading.Condition()
        self.busy = False
        self.last = time.time()
        self.proc = None
        self.on_event = None
        self.watched = time.time()  # last time a client polled events (push only when nobody watches)
        self.job = None  # background/scheduled job id driving this worker
        self._fifo = None
        if meta:  # reattach after a server restart
            self.__dict__.update({k: meta[k] for k in ("id", "uid", "username", "wid", "session", "title", "started", "pid", "port")})
            self.seq = meta.get("seq", 0) + 1000  # jump ahead: clients holding an old cursor still get everything new
            self.dir = RUN / self.id
            self._scan()
        else:
            self.id = uuid.uuid4().hex[:12]
            self.uid, self.username, self.wid = uid, username, wid
            self.session, self.title, self.started, self.seq = session, "", time.time(), 0
            self.port = free_port()
            self.dir = RUN / self.id
            self.dir.mkdir(parents=True, exist_ok=True)
            os.mkfifo(self.dir / "in")
            self._spawn(token)
            if session:
                self.send({"type": "load", "id": session})
        acctlog.on(self.uid, self.username)
        acctlog.log(self.uid, "worker_reattach" if meta else "worker_start", w=self.id, ws=self.wid, session=self.session)
        threading.Thread(target=self._tail, daemon=True).start()

    def _spawn(self, token):
        uid, wid = self.uid, self.wid
        home = DATA / "run" / str(uid) / "home"
        home.mkdir(parents=True, exist_ok=True)
        me.jsave(home / ".me" / "auth.json", {"server": API, "token": token, "user": self.username})
        work = ws_root(uid, wid)
        work.mkdir(parents=True, exist_ok=True)
        env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8", "PYTHONUNBUFFERED": "1", "ME_YOLO": "1", "ME_WS_LOCAL": "1",
               "ME_WS_ID": wid, "ME_SERVER": API, "TERM": "dumb", "PORT": str(self.port), "ME_RPC_IN": "/run/me.in",
               "ME_PREVIEW": f"{PUBLIC or 'http://127.0.0.1:8790'}/preview/{preview_token(uid, wid)}"}
        meta = (me.jload(ws_meta_path(uid), {}) or {}).get(wid) or {}
        if meta.get("repo"):  # a GitHub session: the agent branches, pushes and opens PRs on its own
            env.update(ME_GIT_REPO=meta["repo"], ME_GIT_BASE=meta.get("base") or "main")
        inner = ["python3", "-I", "/opt/me.py" if SANDBOX else str(CLIENT), "--rpc"]
        limits = ["prlimit", "--as=4000000000", "--nproc=256", "--fsize=200000000", "--nofile=1024", "--", "nice", "-n", "10"]
        if SANDBOX:
            ro = [x for d in ("/usr", "/lib", "/lib64", "/bin", "/sbin", "/etc") if os.path.exists(d) for x in ("--ro-bind", d, d)]
            # /etc/resolv.conf is often a link into /run (systemd-resolved): without it the sandbox has no DNS
            dns = [x for d in ("/run/systemd/resolve", "/run/resolvconf") if os.path.isdir(d) for x in ("--ro-bind", d, d)]
            cmd = ["bwrap", *ro, "--ro-bind", str(CLIENT), "/opt/me.py", "--bind", str(home), "/home/me", "--bind", str(work), "/work",
                   "--tmpfs", "/tmp", "--dir", "/run", "--bind", str(self.dir / "in"), "/run/me.in", *dns, "--proc", "/proc", "--dev", "/dev",
                   "--unshare-all", "--share-net", "--die-with-parent", "--new-session", "--chdir", "/work", "--setenv", "HOME", "/home/me",
                   "--setenv", "ME_HOME", "/home/me/.me", *limits, *inner]
        else:
            env.update(HOME=str(home), ME_HOME=str(home / ".me"), ME_RPC_IN=str(self.dir / "in"))
            cmd = [*limits, *inner]
        out = open(self.dir / "out", "ab")
        shepherd = ["sh", "-c", '"$@"; echo \'{"type":"exit"}\'', "sh", *cmd]
        self.proc = subprocess.Popen(shepherd, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT, env=env, cwd=str(work),
                                     start_new_session=True)
        out.close()
        self.pid = self.proc.pid
        self._save()

    def _save(self):
        me.jsave(self.dir / "meta.json", {k: getattr(self, k) for k in ("id", "uid", "username", "wid", "session", "title", "started", "pid",
                                                                         "port", "seq")})

    def _scan(self):
        """After reattach: recover busy/session/title from what the sandbox already printed."""
        try:
            for line in open(self.dir / "out", encoding="utf-8", errors="replace"):
                self._track(self._parse(line), replay=True)
        except OSError:
            pass

    @staticmethod
    def _parse(line):
        try:
            e = json.loads(line)
            return e if isinstance(e, dict) else None
        except ValueError:
            return {"type": "log", "text": line.rstrip()[:500]} if line.strip() else None

    def _track(self, e, replay=False):
        if not e:
            return
        t = e.get("type")
        if t in ("ready", "loaded") and e.get("session"):
            self.session = e["session"]
            self.title = e.get("title") or self.title
        elif t == "turn_start":
            self.busy = True
        elif t in ("turn_end", "exit"):
            self.busy = False
        elif t == "confirm" and not replay:  # sandboxed; auto-approve (ME_YOLO also does this)
            self.send({"type": "confirm", "id": e["id"], "ok": True})

    def _push(self, e):
        with self.cond:
            self.seq += 1
            e["seq"] = self.seq
            self.events.append(e)
            self.cond.notify_all()
        if e.get("type") in ("turn_end", "loaded", "ready", "exit"):
            self._save()
        try:
            acctlog.event(self.uid, self.wid, self.id, e)
        except Exception:
            pass
        cb = self.on_event or getattr(globals().get("runner"), "on_event", None)
        if cb:
            try:
                cb(self, e)
            except Exception:
                pass

    def _tail(self):
        (self.dir / "out").touch(exist_ok=True)
        with open(self.dir / "out", encoding="utf-8", errors="replace") as f:
            if self.proc is None:
                f.seek(0, 2)
            buf = ""
            while True:
                chunk = f.readline()
                if not chunk:
                    if not self.alive:
                        rest = f.read()
                        for line in rest.splitlines():
                            self._handle(line)
                        break
                    time.sleep(0.05)
                    continue
                buf += chunk
                if not buf.endswith("\n"):
                    continue
                line, buf = buf, ""
                self._handle(line)
        self.busy = False
        if not self.events or self.events[-1].get("type") != "exit":
            self._push({"type": "exit"})

    def _handle(self, line):
        e = self._parse(line)
        if not e:
            return
        self._track(e)
        self.last = time.time()
        self._push(e)

    def send(self, cmd):
        self.last = time.time()
        try:
            if self._fifo is None:  # O_RDWR never blocks and never fails for lack of a reader: lines wait in the pipe until the sandbox reads
                self._fifo = os.fdopen(os.open(self.dir / "in", os.O_RDWR), "w", encoding="utf-8")
            self._fifo.write(json.dumps(cmd) + "\n")
            self._fifo.flush()
        except (OSError, ValueError):
            self._fifo = None

    def prompt(self, text, images=()):
        if not self.title:
            self.title = text.strip().split("\n")[0][:60]
        self._push({"type": "user", "text": text})
        self.send({"type": "prompt", "text": text, "images": list(images)})

    @property
    def alive(self):
        if self.proc is not None:
            return self.proc.poll() is None
        try:
            with open(f"/proc/{self.pid}/stat") as f:
                return f.read().split(") ")[-1][:1] not in ("Z", "X")
        except OSError:
            return False

    def stop(self):
        acctlog.log(self.uid, "worker_stop", w=self.id, ws=self.wid, busy=self.busy, age=int(time.time() - self.started))
        try:
            os.killpg(self.pid, 9)
        except OSError:
            pass
        if self.proc is not None:
            try:
                self.proc.wait(5)
            except Exception:
                pass
        try:
            self._fifo and self._fifo.close()
        except OSError:
            pass
        shutil.rmtree(self.dir, ignore_errors=True)

    def wait_events(self, after, timeout=25):
        with self.cond:
            if self.seq <= after:
                self.cond.wait(timeout)
            return [e for e in self.events if e["seq"] > after]

    def info(self):
        return {"id": self.id, "seq": self.seq, "session": self.session, "ws": self.wid, "busy": self.busy, "title": self.title,
                "started": self.started, "last": self.last, "alive": self.alive, "port": self.port, "job": self.job,
                "preview": f"{PUBLIC}/preview/{preview_token(self.uid, self.wid)}" if PUBLIC else f"/preview/{preview_token(self.uid, self.wid)}"}


RUN = DATA / "run" / "workers"
PUBLIC = os.environ.get("ME_PUBLIC_URL", "").rstrip("/")
PREVIEW_KEY = os.environ.get("ME_SECRET", "")


def preview_token(uid, wid):
    import hmac
    key = PREVIEW_KEY or me.jload(DATA / "secret.json", {}).get("k") or _make_secret()
    return f"{uid}-{wid}-" + hmac.new(key.encode(), f"{uid}:{wid}".encode(), hashlib.sha256).hexdigest()[:16]


def _make_secret():
    k = secrets.token_hex(16)
    me.jsave(DATA / "secret.json", {"k": k})
    return k


def free_port():
    import socket
    used = {w.port for w in runner.workers.values()} if "runner" in globals() else set()
    for _ in range(50):
        p = 20000 + secrets.randbelow(9000)
        if p in used:
            continue
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", p))
                return p
            except OSError:
                continue
    return 0


class Runner:
    def __init__(self):
        self.workers = {}
        self.lock = threading.Lock()
        self.on_event = None
        RUN.mkdir(parents=True, exist_ok=True)
        for d in RUN.iterdir():  # reattach sandboxes that survived a server restart
            meta = me.jload(d / "meta.json")
            if not meta or "port" not in meta or not (d / "out").exists():
                shutil.rmtree(d, ignore_errors=True)
                continue
            w = Worker(None, None, None, None, meta=meta)
            if w.alive:
                self.workers[w.id] = w
            else:
                shutil.rmtree(d, ignore_errors=True)
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
            w.on_event = self.on_event
            self.workers[w.id] = w
            return w


runner = Runner()
