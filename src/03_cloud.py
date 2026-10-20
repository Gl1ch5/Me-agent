# ───────────────────────────── cloud ─────────────────────────────


class CloudError(Exception):
    pass


class Cloud:
    """Last-write-wins document sync. Docs: config, skill/<file>, session/<id>."""

    def __init__(self, store):
        self.s = store
        self.lock = threading.Lock()
        self.state_path = store.home / "sync.json"
        self.status = "offline"
        self.last_error = ""

    @property
    def server(self):
        return self.s.auth.get("server") or SERVER

    @property
    def online(self):
        return bool(self.s.auth.get("token"))

    def req(self, method, path, body=None, token=True, timeout=30):
        data = json.dumps(body).encode() if body is not None else None
        r = urllib.request.Request(self.server.rstrip("/") + path, data=data, method=method)
        r.add_header("Content-Type", "application/json")
        r.add_header("User-Agent", f"me/{VERSION}")
        if token and self.s.auth.get("token"):
            r.add_header("Authorization", "Bearer " + self.s.auth["token"])
        try:
            with urllib.request.urlopen(r, timeout=timeout) as resp:
                raw = resp.read()
                return json.loads(raw) if raw[:1] in (b"{", b"[") else raw
        except urllib.error.HTTPError as e:
            try:
                detail = json.loads(e.read()).get("detail", str(e))
            except Exception:
                detail = str(e)
            raise CloudError(detail if isinstance(detail, str) else json.dumps(detail))
        except Exception as e:
            raise CloudError(f"server unreachable: {e}")

    def login(self, username, password, totp=None, register=False):
        body = {"username": username, "password": password, "device": platform.node()}
        if totp:
            body["totp"] = totp
        r = self.req("POST", "/api/register" if register else "/api/login", body, token=False)
        self.s.auth = {"server": self.server, "token": r["token"], "user": r["username"]}
        self.s.save_auth()
        jsave(self.state_path, {"cursor": 0, "hashes": {}, "stats": {}})

    def logout(self):
        try:
            self.req("POST", "/api/logout", {})
        except CloudError:
            pass
        self.s.auth = {}
        self.s.save_auth()
        self.status = "offline"

    # local doc mapping
    def _local(self):
        docs = {"config": self.s.cfg_path}
        for p in (self.s.home / "skills").iterdir():
            if p.is_file() and p.suffix in (".py", ".md"):
                docs["skill/" + p.name] = p
        for p in (self.s.home / "sessions").glob("*.json"):
            docs["session/" + p.stem] = p
        for p in (self.s.home / "plugins").glob("*.py"):
            docs["plugin/" + p.name] = p
        for p in (self.s.home / "themes").glob("*.json"):
            docs["theme/" + p.name] = p
        for p in (self.s.home / "agents").glob("*.md"):
            docs["agent/" + p.name] = p
        for p in (self.s.home / "memory").glob("*.json"):
            docs["memory/" + p.name] = p
        return docs

    def _path(self, key):
        if key == "config":
            return self.s.cfg_path
        kind, _, name = key.partition("/")
        if not name or "/" in name or "\\" in name or name.startswith("."):
            return None
        return {"skill": self.s.home / "skills" / name, "plugin": self.s.home / "plugins" / name, "theme": self.s.home / "themes" / name,
                "agent": self.s.home / "agents" / name, "session": self.s.home / "sessions" / (name + ".json"),
                "memory": self.s.home / "memory" / name}.get(kind)

    def sync(self):
        if not self.online:
            return 0
        with self.lock:
            self.status = "syncing"
            st = jload(self.state_path, {"cursor": 0, "hashes": {}})
            hashes = st.get("hashes", {})
            stats = st.get("stats", {})  # key -> [mtime_ns, size] when its hash was taken: unchanged files are not read again
            local = self._local()
            push = []
            for key, p in local.items():
                try:
                    pst = p.stat()
                    sig = [pst.st_mtime_ns, pst.st_size]
                    if key in hashes and stats.get(key) == sig:
                        continue
                    content = p.read_text("utf-8")
                except Exception:
                    continue
                h = hashlib.sha1(content.encode()).hexdigest()
                if hashes.get(key) != h:
                    push.append({"key": key, "content": content, "ts": pst.st_mtime, "deleted": False, "sig": sig})
                else:
                    stats[key] = sig
            for key in list(hashes):
                if key not in local:
                    push.append({"key": key, "content": "", "ts": time.time(), "deleted": True})
            try:
                r = self.req("POST", "/api/sync", {"cursor": st.get("cursor", 0), "push": [{k: v for k, v in d.items() if k != "sig"} for d in push]},
                             timeout=60)
            except CloudError as e:
                self.status, self.last_error = "error", str(e)
                return -1
            for d in push:
                if d["deleted"]:
                    hashes.pop(d["key"], None)
                    stats.pop(d["key"], None)
                else:
                    hashes[d["key"]] = hashlib.sha1(d["content"].encode()).hexdigest()
                    stats[d["key"]] = d["sig"]
            changed = 0
            for d in r.get("docs", []):
                p = self._path(d["key"])
                if not p:
                    continue
                if d.get("deleted"):
                    if p.exists():
                        p.unlink()
                        changed += 1
                    hashes.pop(d["key"], None)
                    stats.pop(d["key"], None)
                    continue
                h = hashlib.sha1(d["content"].encode()).hexdigest()
                if hashes.get(d["key"]) == h and p.exists():
                    continue
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(d["content"], "utf-8")
                hashes[d["key"]] = h
                pst = p.stat()
                stats[d["key"]] = [pst.st_mtime_ns, pst.st_size]
                changed += 1
            jsave(self.state_path, {"cursor": r.get("cursor", 0), "hashes": hashes, "stats": stats})
            if changed and (self.s.cfg_path.exists()):
                cfg = jload(self.s.cfg_path)
                if cfg:
                    self.s.config.update(cfg)
            self.status = "synced"
            return changed

    def watch(self, on_change, on_version=None, on_ws=None):
        """Background thread: long-polls the server and syncs the moment anything changes elsewhere.
        Never blocks the caller; survives network errors."""
        def loop():
            wscur, pause = -1, 10
            while True:
                if not self.online:
                    time.sleep(5)
                    continue
                cur = (jload(self.state_path, {}) or {}).get("cursor", 0)
                try:
                    r = self.req("GET", f"/api/changes?cursor={cur}&ws={wscur}&wait=25", timeout=40)
                except CloudError:
                    time.sleep(pause)  # back off: a revoked token or a server that is down is not hammered every 10 s
                    pause = min(pause * 2, 300)
                    continue
                pause = 10
                if on_version and r.get("version") and r["version"] != VERSION:
                    on_version(r["version"])
                if r.get("ws_changed") and on_ws:
                    on_ws()
                wscur = r.get("ws", wscur)
                if r.get("changed"):
                    n = self.sync()
                    if n:
                        on_change(n)
        threading.Thread(target=loop, daemon=True, name="me-watch").start()

    def autosync(self, delay=0.7, done=None):
        """Debounced background sync — call on every local change."""
        if not self.online:
            return
        t = getattr(self, "_timer", None)
        if t:
            t.cancel()
        self._timer = threading.Timer(delay, lambda: self.sync_bg(done))
        self._timer.daemon = True
        self._timer.start()

    def sync_bg(self, done=None):
        def run():
            n = self.sync()
            if done:
                done(n)
        threading.Thread(target=run, daemon=True).start()


WS_MAX_FILE = 1_000_000
WS_SKIP_EXT = {".pyc", ".o", ".so", ".dll", ".exe", ".class", ".jar", ".zip", ".tar", ".gz", ".7z", ".rar", ".mp4", ".mov", ".avi", ".mkv",
               ".mp3", ".wav", ".iso", ".dmg", ".bin", ".pt", ".onnx", ".safetensors", ".ckpt", ".sqlite", ".db"}


def _ignore_rules(root):
    rules = []
    gi = Path(root) / ".gitignore"
    if gi.exists():
        for l in gi.read_text("utf-8", "replace").splitlines():
            l = l.strip()
            if l and not l.startswith(("#", "!")):
                rules.append(l.rstrip("/").lstrip("/"))
    return rules


_WS_HASH = {}  # abs path -> ((mtime_ns, size), sha1)


def ws_scan(root):
    """Code-sized files of a project → {relpath: (sha1, mtime)}. Heavy/binary/ignored files are skipped."""
    root = Path(root)
    rules = _ignore_rules(root)
    out = {}
    for d, dirs, files in os.walk(root):
        rel_d = os.path.relpath(d, root)
        dirs[:] = [x for x in dirs if x not in SKIP_DIRS and not (x.startswith(".") and x not in (".github", ".vscode"))
                   and not any(fnmatch.fnmatch(x, r) or fnmatch.fnmatch(os.path.normpath(os.path.join(rel_d, x)), r) for r in rules)]
        for f in files:
            rel = os.path.normpath(os.path.join(rel_d, f)).replace("\\", "/")
            if Path(f).suffix.lower() in WS_SKIP_EXT or f.endswith(".conflict") or any(fnmatch.fnmatch(f, r) or fnmatch.fnmatch(rel, r) for r in rules):
                continue
            p = Path(d) / f
            try:
                st = p.stat()
                if st.st_size > WS_MAX_FILE or not p.is_file():
                    continue
                key, sig = str(p), (st.st_mtime_ns, st.st_size)
                hit = _WS_HASH.get(key)
                if not hit or hit[0] != sig:  # hash only files that changed since the last scan
                    hit = _WS_HASH[key] = (sig, hashlib.sha1(p.read_bytes()).hexdigest())
                out[rel] = (hit[1], st.st_mtime)
            except OSError:
                continue
            if len(out) > 5000:
                return out
    return out


class Workspaces:
    """Project folders mirrored to the cloud so a session can continue on another machine or in the cloud runner.
    3-way sync against the last synced state; on conflict the newer file wins."""

    def __init__(self, store, cloud):
        self.s, self.cloud = store, cloud
        self.path = store.home / "workspaces.json"
        self.lock = threading.Lock()
        self.disabled = bool(os.environ.get("ME_WS_LOCAL"))  # cloud runner: files are already on the server

    def _load(self):
        return jload(self.path, {}) or {}

    def _save(self, d):
        jsave(self.path, d)

    def for_dir(self, path):
        path = os.path.abspath(path)
        for wid, w in self._load().items():
            if os.path.abspath(w["path"]) == path:
                return wid
        return None

    def link(self, path, name=None):
        name = name or Path(path).name or "project"
        r = self.cloud.req("POST", "/api/ws", {"name": name})
        d = self._load()
        d[r["id"]] = {"path": os.path.abspath(path), "name": r["name"], "base": {}}
        self._save(d)
        return r["id"]

    def attach(self, wid, path, name=""):
        d = self._load()
        d[wid] = {"path": os.path.abspath(path), "name": name or wid, "base": {}}
        self._save(d)

    def unlink(self, wid):
        d = self._load()
        d.pop(wid, None)
        self._save(d)

    def local_path(self, wid):
        w = self._load().get(wid)
        return w["path"] if w else None

    def sync(self, wid, emit=None):
        """Returns (pushed, pulled) or raises CloudError."""
        if self.disabled or not self.cloud.online:
            return 0, 0
        with self.lock:
            d = self._load()
            w = d.get(wid)
            if not w:
                return 0, 0
            root = Path(w["path"])
            root.mkdir(parents=True, exist_ok=True)
            base = w.get("base", {})
            local = ws_scan(root)
            remote = self.cloud.req("GET", f"/api/ws/{wid}/manifest")["files"]  # path -> [sha1, size, mtime]
            push, dele, pull, rm_local = [], [], [], []
            for path in set(local) | set(remote) | set(base):
                L = local.get(path, (None, 0))
                R = remote.get(path, [None, 0, 0])
                B = base.get(path)
                if L[0] == R[0]:
                    if L[0]:
                        base[path] = L[0]
                    else:
                        base.pop(path, None)
                    continue
                l_changed, r_changed = L[0] != B, R[0] != B
                if l_changed and (not r_changed or L[1] >= R[2]):
                    (push if L[0] else dele).append(path)
                else:
                    (pull if R[0] else rm_local).append(path)
            batch, size = [], 0
            for i, path in enumerate(push):
                data = (root / path).read_bytes()
                batch.append({"path": path, "b64": base64.b64encode(data).decode(), "mtime": local[path][1]})
                size += len(data)
                if size > 3_000_000 or i == len(push) - 1:
                    self.cloud.req("POST", f"/api/ws/{wid}/files", {"put": batch, "delete": []}, timeout=120)
                    batch, size = [], 0
                base[path] = local[path][0]
                if emit:
                    emit(f"↑ {path}")
            if dele:
                self.cloud.req("POST", f"/api/ws/{wid}/files", {"put": [], "delete": dele})
                for path in dele:
                    base.pop(path, None)
            for i in range(0, len(pull), 200):
                got = self.cloud.req("POST", f"/api/ws/{wid}/get", {"paths": pull[i:i + 200]}, timeout=120)
                for f in got["files"]:
                    p = (root / f["path"]).resolve()
                    if not str(p).startswith(str(root.resolve())):
                        continue
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_bytes(base64.b64decode(f["b64"]))
                    if f.get("mtime"):
                        os.utime(p, (f["mtime"], f["mtime"]))
                    base[f["path"]] = remote[f["path"]][0]
                    if emit:
                        emit(f"↓ {f['path']}")
            for path in rm_local:
                try:
                    (root / path).unlink()
                except OSError:
                    pass
                base.pop(path, None)
            w["base"] = base
            d[wid] = w
            self._save(d)
            return len(push) + len(dele), len(pull) + len(rm_local)
