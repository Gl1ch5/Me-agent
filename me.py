#!/usr/bin/env python3
"""me — a minimal, cloud-synced coding agent in one file.

Layers (top → bottom), each usable on its own:
  Frontends : TUI (default) · headless `-p` · JSON-lines `--rpc`
  Agent     : model ⇄ tools loop, emits events (dicts) to a frontend
  Provider  : any OpenAI-compatible API (chat/completions or responses)
  Tools     : read write edit bash ls grep skill
  Skills    : single .py/.md files; .py actions run in a subprocess and
              stream progress/log events back to the frontend
  Store     : ~/.me (config, skills, sessions)  ⇄  Cloud sync
Stdlib only. Python 3.8+.
"""
import ast, atexit, base64, collections, difflib, fnmatch, glob, hashlib, http.client, io, json, os, platform, queue, re
import shlex, shutil, signal, subprocess, sys, tempfile, textwrap, threading, time, traceback, unicodedata
import urllib.error, urllib.request, uuid, zipfile, zlib
from pathlib import Path

VERSION = "0.11.0"
SERVER = os.environ.get("ME_SERVER", "https://me-agent.ru")
HOME = Path(os.environ.get("ME_HOME", Path.home() / ".me"))
WIN = os.name == "nt"
# Windows: children get their own hidden console, so they can neither read our keystrokes nor change our console modes
NO_WINDOW = {"creationflags": 0x08000000} if WIN else {}
EFFORTS = ["none", "minimal", "low", "medium", "high", "xhigh"]


# ───────────────────────────── util ─────────────────────────────


def jload(p, default=None):
    try:
        return json.loads(Path(p).read_text("utf-8"))
    except Exception:
        return default


def jsave(p, data):
    p = Path(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=p.name + ".", suffix=".tmp")  # unique: two threads may save at once
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(json.dumps(data, ensure_ascii=False, indent=1))
    for i in range(10):
        try:
            return os.replace(tmp, p)
        except PermissionError:  # Windows: a reader, antivirus or OneDrive holds the file for a moment
            if i == 9:
                os.unlink(tmp)
                raise
            time.sleep(0.05 * (i + 1))


def read_raw(p):
    """File text exactly as stored (no newline translation: on Windows read_text/write_text turned LF files into CRLF)."""
    with open(p, encoding="utf-8", newline="") as f:
        return f.read()


def write_raw(p, text):
    with open(p, "w", encoding="utf-8", newline="") as f:
        f.write(text)


def relp(path, start=None):
    """os.path.relpath that never raises: on Windows a path on another drive (C: vs D:) has no relative form, keep it absolute."""
    try:
        return os.path.relpath(path, start or os.getcwd())
    except ValueError:
        return os.path.abspath(path)


def human(n):
    n = int(n or 0)
    if n < 1000:
        return str(n)
    if n < 1_000_000:
        return f"{n / 1000:.1f}k".replace(".0k", "k")
    return f"{n / 1_000_000:.1f}M".replace(".0M", "M")


def dur(s):
    return f"{s:.1f}s" if s < 60 else f"{int(s // 60)}m{int(s % 60):02d}s"


def clip(s, n):
    s = str(s)
    return s if len(s) <= n else s[: n // 2] + f"\n…[{len(s) - n} chars cut]…\n" + s[-n // 2:]


_CACHE = {}


def dir_sig(d, pattern="*"):
    """Cheap fingerprint of a folder's files (name, mtime, size): one listdir + stat per file, no reads."""
    try:
        return tuple((p.name, st.st_mtime_ns, st.st_size) for p in sorted(Path(d).glob(pattern)) for st in (p.stat(),))
    except OSError:
        return ()


def cached(key, sig, fn):
    """fn() once per distinct sig: parsing every skill/agent/theme file on each call made each tool step and TUI frame hit the disk."""
    hit = _CACHE.get(key)
    if hit and hit[0] == sig:
        return hit[1]
    val = fn()
    _CACHE[key] = (sig, val)
    return val


_LOGGED = set()


def log_error(where):
    """Append the current exception's traceback to ~/.me/error.log (each distinct one once per run)."""
    tb = traceback.format_exc()
    if tb in _LOGGED:
        return
    _LOGGED.add(tb)
    try:
        with open(HOME / "error.log", "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%F %T')} me {VERSION} [{where}]\n{tb}\n")
    except OSError:
        pass


# ───────────────────────────── store ─────────────────────────────

DEFAULT_CONFIG = {
    "providers": {},  # name -> {base_url, api_key}
    "models": [],  # [{name, id, provider, api, context, max_out, effort, temperature, vision}]
    "model": None,  # current model name
    "theme": "me-dark",
    "agent": "",  # active agent preset ("" = default)
    "prompt": "",  # user's extra system prompt
    "secrets": {},  # env vars for skills
    "settings": {"yolo": False, "mouse": True, "disabled_skills": [], "disabled_plugins": []},
    "search": {"deepseek_key": "", "model": "deepseek-v4-flash"},
    "voice": {"groq_key": "", "model": "whisper-large-v3", "language": ""},
}


class Store:
    def __init__(self, home=HOME):
        self.home = Path(home)
        for d in ("skills", "sessions", "plugins", "themes", "agents"):
            (self.home / d).mkdir(parents=True, exist_ok=True)
        self.cfg_path = self.home / "config.json"
        self.config = jload(self.cfg_path) or json.loads(json.dumps(DEFAULT_CONFIG))
        for k, v in DEFAULT_CONFIG.items():
            self.config.setdefault(k, json.loads(json.dumps(v)))
        for k, v in DEFAULT_CONFIG["settings"].items():
            self.config["settings"].setdefault(k, v)
        if self.dedupe_models():
            jsave(self.cfg_path, self.config)
        st = self.config["settings"]
        if st.get("mouse_rev", 0) < 2:  # 0.7: the mouse now selects + copies text on every platform (Windows had it off)
            st["mouse"], st["mouse_rev"] = True, 2
        self.auth = jload(self.home / "auth.json", {}) or {}
        self.on_change = None

    def save(self):
        jsave(self.cfg_path, self.config)
        if self.on_change:
            self.on_change()

    def save_auth(self):
        jsave(self.home / "auth.json", self.auth)

    # models
    def model(self, name=None):
        """By name; an unknown name (a model deleted since) falls back to the default model, then to the first one."""
        for n in (name, self.config.get("model")):
            for m in self.config["models"]:
                if n and m["name"] == n:
                    return m
        return self.config["models"][0] if self.config["models"] else None

    def unique_model_name(self, mid, provider="", skip=None):
        """A display name no other model has: the id's last part, then more of the id, then the provider, then a number."""
        taken = {m["name"] for m in self.config["models"] if m is not skip}
        parts = [x for x in str(mid).split("/") if x]
        cands = ["/".join(parts[-k:]) for k in range(1, len(parts) + 1)] + [f"{provider}/{mid}" if provider else mid]
        for c in cands:
            if c and c not in taken:
                return c
        n = 2
        while f"{cands[0]} {n}" in taken:
            n += 1
        return f"{cands[0]} {n}"

    def dedupe_models(self):
        """Older versions could give two models the same name; a chat remembers its model by name, so rename the later ones.
        Returns True if anything changed."""
        seen, changed = set(), False
        for m in self.config["models"]:
            if m.get("name") in seen or not m.get("name"):
                m["name"] = self.unique_model_name(m.get("id", "model"), m.get("provider", ""), skip=m)
                changed = True
            seen.add(m["name"])
        return changed

    def provider(self, m):
        return self.config["providers"].get(m.get("provider", ""), {})

    # sessions
    def session_path(self, sid):
        return self.home / "sessions" / f"{sid}.json"

    def sessions(self):
        """Light index, newest first: {id, title, updated, cwd, ws, n (messages), usage}. A file is parsed again only when it changed."""
        cache = self.__dict__.setdefault("_idx", {})
        out, seen = [], set()
        for p in (self.home / "sessions").glob("*.json"):
            try:
                st = p.stat()
            except OSError:
                continue
            seen.add(p.name)
            hit = cache.get(p.name)
            if not hit or hit[0] != (st.st_mtime_ns, st.st_size):
                d = jload(p)
                if not isinstance(d, dict) or not d.get("id"):
                    continue
                hit = cache[p.name] = ((st.st_mtime_ns, st.st_size), {
                    "id": d["id"], "title": d.get("title", ""), "updated": d.get("updated", 0), "created": d.get("created", 0),
                    "cwd": d.get("cwd", ""), "ws": d.get("ws"), "n": len(d.get("messages", [])), "usage": d.get("usage", {}),
                    "agent": d.get("agent", ""), "pinned": d.get("pinned", False), "archived": d.get("archived", False)})
            out.append(hit[1])
        for k in set(cache) - seen:
            cache.pop(k, None)
        return sorted(out, key=lambda s: s.get("updated", 0), reverse=True)

    def search(self, q, limit=40):
        """Chats whose title or messages contain every word of `q` (any case), newest first: [(index entry, snippet)]."""
        words = [w for w in q.lower().split() if w]
        out = []
        for s in self.sessions():
            d = jload(self.session_path(s["id"])) or {}
            texts = [s.get("title") or ""]
            for m in d.get("messages") or []:
                c = m.get("content")
                if m.get("role") in ("user", "assistant"):
                    texts.append(c if isinstance(c, str) else " ".join(x.get("text", "") for x in c or [] if isinstance(x, dict)))
            low = "\n".join(texts).lower()
            if words and all(w in low for w in words):
                t = next((t for t in texts if words[0] in t.lower()), "")
                i = t.lower().find(words[0])
                out.append((s, " ".join(t[max(0, i - 40):i + 80].split())))
                if len(out) >= limit:
                    break
        return out

    def save_session(self, s):
        s["updated"] = time.time()
        msgs = []
        last = max((i for i, m in enumerate(s["messages"]) if m.get("role") == "user" and not m.get("_aux")), default=-1)
        for i, m in enumerate(s["messages"]):  # drop image payloads from disk/sync
            if isinstance(m.get("content"), list):
                m = dict(m, content=[c if c.get("type") != "image_url" else {"type": "text", "text": "[image]"} for c in m["content"]])
            if i < last and m.get("role") == "assistant" and any(k in m for k in ("reasoning_content", "reasoning_details", "reasoning_items")):
                m = {k: v for k, v in m.items() if k not in ("reasoning_content", "reasoning_details", "reasoning_items")}  # past turns: never resent
            msgs.append(m)
        jsave(self.session_path(s["id"]), dict(s, messages=msgs))
        if self.on_change:
            self.on_change()


def new_session(cwd=None):
    return {"id": time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4], "title": "", "cwd": str(cwd or os.getcwd()),
            "created": time.time(), "updated": time.time(), "messages": [], "usage": {"in": 0, "out": 0}}


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
            wscur = -1
            while True:
                if not self.online:
                    time.sleep(5)
                    continue
                cur = (jload(self.state_path, {}) or {}).get("cursor", 0)
                try:
                    r = self.req("GET", f"/api/changes?cursor={cur}&ws={wscur}&wait=25", timeout=40)
                except CloudError:
                    time.sleep(10)
                    continue
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


# ───────────────────────────── provider ─────────────────────────────


class Cancelled(Exception):
    pass


def no_images(messages, note="[image: this model cannot see images]"):
    """Messages with every image part replaced by a text note (copies; the originals stay)."""
    return [dict(m, content=[x if x.get("type") != "image_url" else {"type": "text", "text": note} for x in m["content"]])
            if isinstance(m.get("content"), list) else m for m in messages]


class ProviderError(RuntimeError):
    """An API failure. `retryable`: worth another try (rate limit, overload, 5xx, dropped stream). `overflow`: the prompt
    no longer fits the model's context."""

    def __init__(self, msg, status=None, retry_after=None, retryable=False):
        super().__init__(msg)
        self.status, self.retry_after, self.retryable = status, retry_after, retryable
        self.overflow = bool(OVERFLOW_RX.search(msg)) and status in (None, 400, 413, 422)


RETRY_STATUS = {408, 409, 425, 429, 500, 502, 503, 504, 520, 521, 522, 523, 524, 529}
RETRY_RX = re.compile(r"overload|rate.?limit|try again|temporar|capacity|unavailable|timeout|timed out", re.I)
OVERFLOW_RX = re.compile(r"context.{0,24}(length|window|limit|size)|maximum context|too many (input )?tokens|prompt is too long|"
                         r"reduce the length|input is too long|exceeds? the (model|context|maximum)|context_length_exceeded", re.I)
IMAGE_RX = re.compile(r"image|vision|multi.?modal|image_url|content.{0,20}(type|part)", re.I)
RETRIES, RETRY_BASE = 4, 2.0  # waits 2, 4, 8, 16 s (or what the server's Retry-After asks, up to a minute)
NET_ERRORS = (urllib.error.URLError, ConnectionError, TimeoutError, OSError, http.client.HTTPException)


def _retry_after(headers):
    v = (headers or {}).get("Retry-After") or (headers or {}).get("retry-after")
    try:
        return min(60.0, max(0.0, float(v)))
    except (TypeError, ValueError):
        return None


class Provider:
    """OpenAI-compatible streaming client. Messages use chat/completions shape internally.
    Assistant messages keep what the provider needs back on the next step of the same turn (reasoning_content,
    reasoning_details, Gemini's thought signatures in tool_calls[].extra_content, Responses reasoning items), tagged with
    `_model` so it only ever goes back to the model that produced it."""

    def __init__(self, model, prov):
        self.m, self.p = model, prov
        self.base = (prov.get("base_url") or "https://api.openai.com/v1").rstrip("/")

    def _post(self, path, body):
        r = urllib.request.Request(self.base + path, data=json.dumps(body).encode(), method="POST")
        r.add_header("Content-Type", "application/json")
        r.add_header("Accept", "text/event-stream")
        r.add_header("User-Agent", f"me/{VERSION}")
        for k, v in (self.p.get("headers") or {}).items():  # e.g. OpenRouter's HTTP-Referer, a proxy's own auth header
            r.add_header(k, str(v))
        if self.p.get("api_key"):
            r.add_header("Authorization", "Bearer " + self.p["api_key"])
        try:
            return urllib.request.urlopen(r, timeout=600)
        except urllib.error.HTTPError as e:
            text = e.read().decode("utf-8", "replace")[:1500]
            try:  # the readable part of {"error": {"message": …}}
                j = json.loads(text)
                err = j.get("error") if isinstance(j, dict) else None
                text = (err.get("message") if isinstance(err, dict) else err) or j.get("message") or text
            except (ValueError, AttributeError):
                pass
            raise ProviderError(f"HTTP {e.code}: {str(text)[:800]}", e.code, _retry_after(e.headers),
                                e.code in RETRY_STATUS or bool(RETRY_RX.search(str(text)) and e.code >= 500))

    @staticmethod
    def _sse(resp, cancel):
        """Parsed SSE events. Lines are read by a helper thread, so Esc stops at once on every OS even while a model thinks
        silently for minutes (a blocked readline() cannot be interrupted reliably on Windows)."""
        ctype = resp.headers.get("Content-Type", "") if getattr(resp, "headers", None) else ""
        if "json" in ctype and "event-stream" not in ctype:  # a server that ignored stream=true: one plain JSON reply
            try:
                yield json.loads(resp.read())
            finally:
                resp.close()
            return
        q = queue.Queue(maxsize=1000)

        def reader():
            try:
                for chunk in iter(resp.readline, b""):
                    q.put(chunk)
                q.put(None)
            except Exception as e:
                q.put(e)
        threading.Thread(target=reader, daemon=True, name="me-sse").start()
        try:
            while True:
                try:
                    chunk = q.get(timeout=0.1)
                except queue.Empty:
                    if cancel.is_set():
                        raise Cancelled()
                    continue
                if cancel.is_set():
                    raise Cancelled()
                if chunk is None:
                    break
                if isinstance(chunk, Exception):
                    raise chunk
                line = chunk.strip()
                if line.startswith(b"data:"):
                    data = line[5:].strip()
                    if data == b"[DONE]":
                        break
                    try:
                        yield json.loads(data)
                    except ValueError:
                        pass
                elif line.startswith(b"{") and b'"error"' in line:  # a plain JSON error body instead of a stream
                    try:
                        err = json.loads(line).get("error")
                    except ValueError:
                        err = line.decode("utf-8", "replace")
                    msg = str(err.get("message") if isinstance(err, dict) else err)[:800]
                    raise ProviderError(msg, None, None, bool(RETRY_RX.search(msg)))
        finally:
            threading.Thread(target=resp.close, daemon=True).start()  # closing may block on some platforms: never in the UI path

    def cache_mode(self):
        """How to ask for prompt caching: 'anthropic' (cache_control marker), 'openai' (prompt_cache_key), or ''. model.cache overrides."""
        c = self.m.get("cache")
        if c in ("anthropic", "openai", "off"):
            return "" if c == "off" else c
        mid = str(self.m.get("id", "")).lower()
        if "claude" in mid or "anthropic" in self.base:
            return "anthropic"
        if "api.openai.com" in self.base:
            return "openai"
        return ""  # DeepSeek, Gemini, Groq… cache a stable prefix automatically

    @staticmethod
    def _cached(u):
        d = u.get("prompt_tokens_details") or u.get("input_tokens_details") or {}
        return int(d.get("cached_tokens") or u.get("cache_read_input_tokens") or u.get("prompt_cache_hit_tokens") or 0)

    def stream(self, system, messages, tools, emit, cancel, cache_key=None):
        """Returns (assistant_message, usage). Emits text/reasoning deltas. The system prompt + tools form a stable prefix.
        Rate limits, overloads, 5xx and dropped connections are retried with backoff; a retry after partial output emits
        {"type": "retry", "discard": True} so the frontend drops what it showed of the failed attempt."""
        attempt = 0
        while True:
            shown = [False]

            def em(e):
                shown[0] = True
                emit(e)
            try:
                if self.m.get("api") == "responses":
                    return self._responses(system, messages, tools, em, cancel, cache_key)
                return self._chat(system, messages, tools, em, cancel, cache_key)
            except Cancelled:
                raise
            except ProviderError as e:
                if not e.retryable or attempt >= RETRIES:
                    raise
                err, delay = e, e.retry_after
            except NET_ERRORS as e:
                if cancel.is_set():
                    raise Cancelled()
                if attempt >= RETRIES:
                    raise ProviderError(f"network error: {e}")
                err, delay = e, None
            attempt += 1
            delay = delay if delay is not None else min(30, RETRY_BASE * 2 ** (attempt - 1))
            emit({"type": "retry", "attempt": attempt, "of": RETRIES, "delay": delay, "error": str(err)[:300], "discard": shown[0]})
            if cancel.wait(delay):
                raise Cancelled()

    def blind(self):
        """vision off in /models: a model without vision rejects the whole request over one image."""
        return str(self.m.get("vision", True)).lower() == "false"

    def wire(self, messages):
        """Session messages → request messages: internal keys dropped; reasoning replayed only to the model that wrote it,
        and only for the current turn (after the last real user message) — what DeepSeek, Kimi and Qwen expect."""
        mid = self.m.get("id")
        if self.blind():
            messages = no_images(messages)
        last = max((i for i, m in enumerate(messages) if m.get("role") == "user" and not m.get("_aux")), default=-1)
        out = []
        for i, m in enumerate(messages):
            mine = m.get("role") == "assistant" and m.get("_model") == mid
            w = {k: v for k, v in m.items() if not k.startswith("_") and k not in ("reasoning_content", "reasoning_details", "reasoning_items")}
            if mine and i > last:
                for k in ("reasoning_content", "reasoning_details"):
                    if m.get(k):
                        w[k] = m[k]
            if w.get("tool_calls"):
                w["tool_calls"] = [tc if mine or "extra_content" not in tc else {k: v for k, v in tc.items() if k != "extra_content"}
                                   for tc in w["tool_calls"]]
            out.append(w)
        return out

    def _chat(self, system, messages, tools, emit, cancel, cache_key=None):
        cm = self.cache_mode()
        sysmsg = {"role": "system", "content": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}] if cm == "anthropic" else system}
        body = {"model": self.m["id"], "stream": True, "stream_options": {"include_usage": True},
                "messages": [sysmsg] + self.wire(messages)}
        if cm == "openai" and cache_key:
            body["prompt_cache_key"] = cache_key
        if tools:
            body["tools"] = [{"type": "function", "function": t} for t in tools]
        if self.m.get("effort") and self.m["effort"] != "none":
            body["reasoning_effort"] = self.m["effort"]
        if self.m.get("max_out"):
            body["max_completion_tokens" if self.m.get("effort") else "max_tokens"] = int(self.m["max_out"])
        if self.m.get("temperature") not in (None, ""):
            body["temperature"] = float(self.m["temperature"])
        text, reason, details, calls, usage, finish = "", "", {}, {}, {}, ""
        resp = self._post("/chat/completions", body)
        for ev in self._sse(resp, cancel):
            if ev.get("error"):  # OpenRouter & co report upstream failures inside the stream
                e = ev["error"]
                msg = (e.get("message") if isinstance(e, dict) else str(e)) or "stream error"
                code = e.get("code") if isinstance(e, dict) else None
                code = code if isinstance(code, int) else None
                raise ProviderError(f"{msg}" + (f" ({code})" if code else ""), code, None, code in RETRY_STATUS or bool(RETRY_RX.search(msg)))
            if ev.get("usage"):
                usage = ev["usage"]
            for ch in ev.get("choices") or []:
                d = ch.get("delta") or ch.get("message") or {}
                finish = ch.get("finish_reason") or finish
                r = d.get("reasoning_content") or d.get("reasoning")
                if isinstance(r, str) and r:
                    reason += r
                    emit({"type": "reasoning", "text": r})
                for x in d.get("reasoning_details") or []:  # OpenRouter: merge streamed pieces by index
                    if isinstance(x, dict):
                        cur = details.setdefault(x.get("index", len(details)), {})
                        for k, v in x.items():
                            cur[k] = cur.get(k, "") + v if isinstance(v, str) and k in ("text", "summary", "data", "signature") else v
                        if not r and x.get("type") == "reasoning.text" and x.get("text"):
                            emit({"type": "reasoning", "text": x["text"]})
                if d.get("content"):
                    text += d["content"]
                    emit({"type": "text", "text": d["content"]})
                for j, tc in enumerate(d.get("tool_calls") or []):
                    c = calls.setdefault(tc.get("index", j), {"id": "", "name": "", "args": ""})
                    c["id"] = tc.get("id") or c["id"]
                    f = tc.get("function") or {}
                    c["name"] += f.get("name") or ""
                    c["args"] += f.get("arguments") or ""
                    if tc.get("extra_content"):  # Gemini thought signature: must come back with this call
                        c["extra"] = tc["extra_content"]
        if not (text or calls or reason or usage or finish):
            raise ProviderError("the stream ended before any reply (connection dropped)", retryable=True)
        msg = {"role": "assistant", "content": text or None, "_model": self.m.get("id")}
        if reason:
            msg["reasoning_content"] = reason
        if details:
            msg["reasoning_details"] = [details[k] for k in sorted(details, key=lambda k: (not isinstance(k, int), k if isinstance(k, int) else 0))]
        if finish:
            msg["_finish"] = finish
        if calls:
            msg["tool_calls"] = []
            for _, c in sorted(calls.items()):
                tc = {"id": c["id"] or "call_" + uuid.uuid4().hex[:8], "type": "function", "function": {"name": c["name"], "arguments": c["args"] or "{}"}}
                if c.get("extra"):
                    tc["extra_content"] = c["extra"]
                msg["tool_calls"].append(tc)
        return msg, {"in": usage.get("prompt_tokens", 0), "out": usage.get("completion_tokens", 0), "cached": self._cached(usage)}

    def _responses(self, system, messages, tools, emit, cancel, cache_key=None):
        mid = self.m.get("id")
        items = []
        for m in no_images(messages) if self.blind() else messages:
            if m["role"] == "tool":
                items.append({"type": "function_call_output", "call_id": m["tool_call_id"], "output": m["content"]})
            elif m["role"] == "assistant":
                if m.get("_model") == mid:  # encrypted reasoning: the model keeps its chain of thought between tool calls
                    items.extend(m.get("reasoning_items") or [])
                if m.get("content"):
                    items.append({"role": "assistant", "content": m["content"]})
                for tc in m.get("tool_calls") or []:
                    items.append({"type": "function_call", "call_id": tc["id"], "name": tc["function"]["name"],
                                  "arguments": tc["function"]["arguments"]})
            else:
                c = m["content"]
                if isinstance(c, list):
                    c = [{"type": "input_text", "text": x["text"]} if x["type"] == "text" else
                         {"type": "input_image", "image_url": x["image_url"]["url"]} for x in c]
                items.append({"role": "user", "content": c})
        body = {"model": self.m["id"], "stream": True, "instructions": system, "input": items, "store": False}
        if self.cache_mode() == "openai" and cache_key:
            body["prompt_cache_key"] = cache_key
        if tools:
            body["tools"] = [dict(type="function", **t) for t in tools]
        if self.m.get("effort") and self.m["effort"] != "none":
            body["reasoning"] = {"effort": self.m["effort"], "summary": "auto"}
            body["include"] = ["reasoning.encrypted_content"]
        if self.m.get("max_out"):
            body["max_output_tokens"] = int(self.m["max_out"])
        if self.m.get("temperature") not in (None, ""):
            body["temperature"] = float(self.m["temperature"])
        text, calls, usage, reasoning, finish = "", {}, {}, [], ""
        for ev in self._sse(self._post("/responses", body), cancel):
            t = ev.get("type", "")
            if t == "response.output_text.delta":
                text += ev["delta"]
                emit({"type": "text", "text": ev["delta"]})
            elif t in ("response.reasoning_summary_text.delta", "response.reasoning_text.delta"):
                emit({"type": "reasoning", "text": ev["delta"]})
            elif t == "response.output_item.done" and ev.get("item", {}).get("type") == "function_call":
                it = ev["item"]
                calls[it["call_id"]] = {"id": it["call_id"], "name": it["name"], "args": it.get("arguments", "")}
            elif t == "response.output_item.done" and ev.get("item", {}).get("type") == "reasoning":
                it = ev["item"]
                if it.get("encrypted_content"):
                    reasoning.append({k: it[k] for k in ("type", "id", "summary", "encrypted_content") if k in it})
            elif t in ("response.completed", "response.incomplete"):
                r = ev.get("response", {})
                usage = r.get("usage") or {}
                if t == "response.incomplete":
                    finish = "length" if (r.get("incomplete_details") or {}).get("reason") == "max_output_tokens" else "incomplete"
            elif t in ("response.failed", "error"):
                e = ev.get("response", {}).get("error") or ev.get("error") or ev
                msg = (e.get("message") if isinstance(e, dict) else None) or json.dumps(e)[:800]
                code = e.get("code") if isinstance(e, dict) else None
                raise ProviderError(msg, None, None, bool(RETRY_RX.search(msg)) or code in ("server_error", "rate_limit_exceeded"))
        msg = {"role": "assistant", "content": text or None, "_model": mid}
        if reasoning:
            msg["reasoning_items"] = reasoning
        if finish:
            msg["_finish"] = finish
        if calls:
            msg["tool_calls"] = [{"id": c["id"], "type": "function", "function": {"name": c["name"], "arguments": c["args"] or "{}"}}
                                 for c in calls.values()]
        return msg, {"in": usage.get("input_tokens", 0), "out": usage.get("output_tokens", 0), "cached": self._cached(usage)}


def list_models(prov):
    base = (prov.get("base_url") or "https://api.openai.com/v1").rstrip("/")
    r = urllib.request.Request(base + "/models")
    if prov.get("api_key"):
        r.add_header("Authorization", "Bearer " + prov["api_key"])
    with urllib.request.urlopen(r, timeout=30) as resp:
        data = json.loads(resp.read())
    out = []
    for m in data.get("data", data if isinstance(data, list) else []):
        ctx = m.get("context_length") or m.get("context_window") or (m.get("top_provider") or {}).get("context_length")
        out.append({"id": m["id"], "context": ctx})
    return sorted(out, key=lambda x: x["id"])


# ───────────────────────────── file reading ─────────────────────────────

IMAGE_EXT = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp"}


def _xml_text(data):
    data = re.sub(rb"</(w:p|a:p|text:p|row)>", b"\n", data)
    data = re.sub(rb"</(w:tc|c)>", b"\t", data)
    txt = re.sub(rb"<[^>]+>", b"", data).decode("utf-8", "replace")
    for a, b in (("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"'), ("&apos;", "'"), ("&amp;", "&")):
        txt = txt.replace(a, b)
    return re.sub(r"\n\s*\n+", "\n", txt).strip()


def _office_text(path):
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        parts = []
        if "word/document.xml" in names:
            parts = ["word/document.xml"]
        elif any(n.startswith("ppt/slides/slide") for n in names):
            parts = sorted([n for n in names if re.match(r"ppt/slides/slide\d+\.xml$", n)], key=lambda n: int(re.findall(r"\d+", n)[0]))
        elif "xl/sharedStrings.xml" in names or any(n.startswith("xl/worksheets/") for n in names):
            shared = []
            if "xl/sharedStrings.xml" in names:
                shared = [_xml_text(s) for s in re.findall(rb"<si>(.*?)</si>", z.read("xl/sharedStrings.xml"), re.S)]
            out = []
            for n in sorted(n for n in names if re.match(r"xl/worksheets/sheet\d+\.xml$", n)):
                out.append(f"## {n.split('/')[-1]}")
                for row in re.findall(rb"<row[^>]*>(.*?)</row>", z.read(n), re.S):
                    cells = []
                    for attrs, val in re.findall(rb"<c([^>]*)>(.*?)</c>", row, re.S):
                        v = re.search(rb"<v>(.*?)</v>", val) or re.search(rb"<t[^>]*>(.*?)</t>", val)
                        v = v.group(1).decode() if v else ""
                        if b't="s"' in attrs and v.isdigit() and int(v) < len(shared):
                            v = shared[int(v)]
                        cells.append(v)
                    out.append("\t".join(cells))
            return "\n".join(out)
        elif "content.xml" in names:
            parts = ["content.xml"]
        return "\n\n".join(_xml_text(z.read(p)) for p in parts)


def _pdf_text(path):
    raw = Path(path).read_bytes()
    out = []
    for m in re.finditer(rb"stream\r?\n(.*?)\r?\nendstream", raw, re.S):
        s = m.group(1)
        try:
            s = zlib.decompress(s)
        except Exception:
            pass
        for bt in re.findall(rb"BT(.*?)ET", s, re.S):
            line = []
            for t in re.findall(rb"\((?:\\.|[^\\)])*\)|\[(?:[^\]])*\]|T\*|Td|TD|'", bt):
                if t.startswith(b"["):
                    line.append(b"".join(x[1:-1] for x in re.findall(rb"\((?:\\.|[^\\)])*\)", t)))
                elif t.startswith(b"("):
                    line.append(t[1:-1])
                else:
                    line.append(b"\n")
            txt = b"".join(line)
            txt = re.sub(rb"\\([nrt()\\])", lambda x: {b"n": b"\n", b"r": b"", b"t": b"\t"}.get(x.group(1), x.group(1)), txt)
            out.append(txt.decode("latin-1"))
    text = "".join(out).strip()
    return text or "[pdf: no extractable text (scanned or encoded fonts)]"


def decode_text(data):
    """bytes → (text, encoding, bom). UTF-8 (with or without BOM), UTF-16 with BOM, else the system's legacy code page
    (cp1251 on a Russian Windows), else latin-1. Writing back with the same encoding keeps the file as it was."""
    for bom, enc in ((b"\xef\xbb\xbf", "utf-8"), (b"\xff\xfe", "utf-16-le"), (b"\xfe\xff", "utf-16-be")):
        if data.startswith(bom):
            return data[len(bom):].decode(enc, "replace"), enc, bom
    try:
        return data.decode("utf-8"), "utf-8", b""
    except UnicodeDecodeError:
        pass
    import locale
    for enc in dict.fromkeys([locale.getpreferredencoding(False), "cp1251"]):
        try:
            if enc.lower().replace("-", "") in ("utf8", "ascii"):
                continue
            text = data.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
        # a Cyrillic file read on a Western Windows (cp1252) turns into words of accented letters only ("ïðèâåò"):
        # such words do not exist in Western languages, so take the Cyrillic code page instead
        if enc.lower() != "cp1251" and len(re.findall("[\u00c0-\u00ff]{3,}", text)) >= 2:
            try:
                return data.decode("cp1251"), "cp1251", b""
            except UnicodeDecodeError:
                pass
        return text, enc, b""
    return data.decode("latin-1"), "latin-1", b""


def read_text_file(path):
    """A text file for editing: (text with \n line ends, how to write it back)."""
    text, enc, bom = decode_text(Path(path).read_bytes())
    crlf, lf = text.count("\r\n"), text.count("\n")
    return text.replace("\r\n", "\n"), {"enc": enc, "bom": bom, "nl": "\r\n" if crlf and crlf * 2 >= lf else "\n"}


def write_text_file(path, text, fmt=None):
    """Write \n-text with the file's own line ends, encoding and BOM (no newline translation: on Windows Python would
    otherwise turn every LF file into CRLF)."""
    fmt = fmt or {"enc": "utf-8", "bom": b"", "nl": "\n"}
    if fmt["nl"] == "\r\n":
        text = text.replace("\r\n", "\n").replace("\n", "\r\n")
    try:
        data = text.encode(fmt["enc"])
    except UnicodeEncodeError:  # a character the legacy code page cannot hold: switch the file to UTF-8 rather than lose it
        data = text.encode("utf-8")
        fmt = dict(fmt, enc="utf-8", bom=b"")
    Path(path).write_bytes(fmt["bom"] + data)


READ_CAP = 50_000  # characters of one read result


def read_any(path, offset=1, limit=2000):
    p = Path(path).expanduser()
    if not p.exists():
        hint = ""
        try:
            near = difflib.get_close_matches(p.name, [x.name for x in p.parent.iterdir()], n=3, cutoff=0.6) if p.parent.is_dir() else []
            hint = f" — did you mean: {', '.join(near)}?" if near else ""
        except OSError:
            pass
        return f"error: {path} not found{hint}", None
    if p.is_dir():
        items = sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))
        return "\n".join(x.name + ("/" if x.is_dir() else "") for x in items[:500]) + (f"\n… {len(items) - 500} more" if len(items) > 500 else "") \
            or "(empty dir)", None
    ext = p.suffix.lower()
    if ext in IMAGE_EXT:
        size = p.stat().st_size
        if size > 8_000_000:
            return f"error: image {p.name} is {size // 1_000_000} MB, too big to send to the model (resize it first)", None
        b64 = base64.b64encode(p.read_bytes()).decode()
        return f"[image {p.name}, {size} bytes attached]", f"data:{IMAGE_EXT[ext]};base64,{b64}"
    try:
        if ext in (".docx", ".pptx", ".xlsx", ".odt", ".ods", ".odp"):
            text = _office_text(p)
        elif ext == ".pdf":
            text = _pdf_text(p)
        elif ext == ".ipynb":
            nb = json.loads(p.read_text("utf-8"))
            text = "\n\n".join(f"# [{c['cell_type']}]\n" + "".join(c.get("source", [])) for c in nb.get("cells", []))
        else:
            data = p.read_bytes()
            if b"\0" in data[:8192] and not data.startswith((b"\xff\xfe", b"\xfe\xff")):
                head = data[:512]
                hexd = "\n".join(f"{i:08x}  {head[i:i + 16].hex(' ')}" for i in range(0, len(head), 16))
                return f"[binary {p.name}, {len(data)} bytes]\n{hexd}", None
            text = decode_text(data)[0]
    except Exception as e:
        return f"error reading {path}: {e}", None
    lines = text.replace("\r\n", "\n").split("\n")  # the same numbering the edit tool's lines= uses
    if lines and lines[-1] == "":
        lines.pop()
    offset = max(1, int(offset or 1))
    limit = max(1, int(limit or 2000))
    if offset > len(lines) and lines:
        return f"error: offset {offset} is past the end ({len(lines)} lines)", None
    w = len(str(min(len(lines), offset + limit)))
    out, used, i = [], 0, offset - 1
    for i in range(offset - 1, min(len(lines), offset - 1 + limit)):
        l = lines[i]
        row = f"{i + 1:>{w}}│{l[:2000]}" + (" …[line cut]" if len(l) > 2000 else "")
        if used + len(row) > READ_CAP and out:
            return "\n".join(out) + f"\n… output capped at {READ_CAP // 1000}k chars; {len(lines) - i} more lines (use offset={i + 1})", None
        out.append(row)
        used += len(row) + 1
    rest = len(lines) - (offset - 1 + len(out))
    return ("\n".join(out) + (f"\n… {rest} more lines (use offset={offset + len(out)})" if rest > 0 else "")) or "(empty file)", None


# ───────────────────────────── skills ─────────────────────────────

SKILL_RUNNER = r'''
import sys, json, os, builtins, urllib.request, time
_out = os.fdopen(os.dup(1), "w", buffering=1, encoding="utf-8")
def _emit(**e): _out.write(json.dumps(e, ensure_ascii=False) + "\n"); _out.flush()
class _Log:
    def write(self, s):
        for l in s.splitlines():
            if l.strip(): _emit(t="log", text=l)
    def flush(self): pass
sys.stdout = _Log()
def action(fn=None, **kw):
    if fn is None: return lambda f: f
    return fn
builtins.action = action
class _Http:
    def _r(self, method, url, data=None, headers=None):
        h = {"User-Agent": "me-skill"}; h.update(headers or {})
        body = None
        if data is not None:
            body = json.dumps(data).encode(); h.setdefault("Content-Type", "application/json")
        req = urllib.request.Request(url, data=body, method=method, headers=h)
        with urllib.request.urlopen(req, timeout=120) as r:
            raw = r.read()
            try: return json.loads(raw)
            except ValueError: return raw.decode("utf-8", "replace")
    def get(self, url, headers=None): return self._r("GET", url, None, headers)
    def post(self, url, data=None, headers=None): return self._r("POST", url, data, headers)
    def put(self, url, data=None, headers=None): return self._r("PUT", url, data, headers)
    def delete(self, url, headers=None): return self._r("DELETE", url, None, headers)
class Ctx:
    http = _Http()
    env = os.environ
    cwd = os.getcwd()
    def log(self, *a): _emit(t="log", text=" ".join(map(str, a)))
    def status(self, s): _emit(t="status", text=str(s))
    def bar(self, done, total=None, label=""): _emit(t="progress", v=(done / total if total else done), label=label, n=done, total=total)
    def progress(self, it, label="", total=None):
        try: total = total or len(it)
        except TypeError: total = None
        for i, x in enumerate(it):
            self.bar(i, total, label) if total else _emit(t="progress", v=None, label=label, n=i)
            yield x
        if total: self.bar(total, total, label)
    def ask(self, q, options=None):
        _emit(t="ask", text=str(q), options=[str(o) for o in options] if options else None)
        return sys.stdin.readline().rstrip("\n")
    def choose(self, q, options):
        "Let the user pick one option; returns the chosen string (or '' if dismissed)."
        return self.ask(q, list(options))
    def confirm(self, q):
        return self.ask(q, ["yes", "no"]).lower().startswith("y")
    def table(self, rows, title=""):
        "Show rows (list of dicts or lists) in the skill card."
        _emit(t="table", title=str(title), rows=rows[:200])
    def md(self, text):
        "Show markdown in the skill card."
        _emit(t="md", text=str(text)[:20000])
path, fn, args = sys.argv[1], sys.argv[2], json.loads(sys.argv[3])
sys.path.insert(0, os.path.join(os.path.dirname(path), ".deps"))
g = {"__name__": "skill", "__file__": path, "action": action}
try:
    exec(compile(open(path, encoding="utf-8").read(), path, "exec"), g)
    r = g[fn](Ctx(), **args)
    _emit(t="result", ok=True, value=r if isinstance(r, (str, int, float, bool, list, dict, type(None))) else str(r))
except Exception as e:
    import traceback
    _emit(t="result", ok=False, value=f"{type(e).__name__}: {e}\n" + traceback.format_exc()[-1500:])
'''


def _src(text, node):
    """Source of a default value (ast.unparse is 3.9+)."""
    try:
        return repr(ast.literal_eval(node))
    except ValueError:
        return ast.unparse(node) if hasattr(ast, "unparse") else (ast.get_source_segment(text, node) or "…")


def parse_skill(path):
    p = Path(path)
    text = p.read_text("utf-8").replace("\r\n", "\n")
    meta, body, actions = {"name": p.stem, "description": ""}, "", {}
    if p.suffix == ".py":
        try:
            tree = ast.parse(text)
        except SyntaxError as e:
            return dict(meta, error=str(e), path=str(p), body="", actions={})
        doc = ast.get_docstring(tree) or ""
        for node in tree.body:
            if isinstance(node, ast.FunctionDef):
                dec = None
                for d in node.decorator_list:
                    if (isinstance(d, ast.Name) and d.id == "action") or (isinstance(d, ast.Call) and getattr(d.func, "id", "") == "action"):
                        dec = d
                if dec is None:
                    continue
                kw = {k.arg: ast.literal_eval(k.value) for k in getattr(dec, "keywords", []) if isinstance(k.value, ast.Constant)}
                a = node.args
                names = [x.arg for x in a.args][1:]
                defaults = [_src(text, d) for d in a.defaults]
                sig = []
                for i, n in enumerate(names):
                    j = i - (len(names) - len(defaults))
                    sig.append(f"{n}={defaults[j]}" if j >= 0 else n)
                actions[node.name] = {"sig": f"{node.name}({', '.join(sig)})", "doc": (ast.get_docstring(node) or "").split("\n")[0],
                                      "confirm": bool(kw.get("confirm"))}
    else:
        m = re.match(r"^---\n(.*?)\n---\n?(.*)$", text, re.S)
        doc = (m.group(1) + "\n---\n" + m.group(2)) if m else text
    head, sep, rest = doc.partition("\n---")
    if sep:
        for line in head.splitlines():
            k, _, v = line.partition(":")
            if k.strip() and v.strip():
                meta[k.strip()] = v.strip()
        body = rest.lstrip("-\n")
    else:
        body = doc
    return dict(meta, body=body.strip(), actions=actions, path=str(p))


class Skills:
    def __init__(self, store):
        self.s = store

    def all(self):
        d = self.s.home / "skills"
        return dict(cached(("skills", str(d)), dir_sig(d), lambda: self._load(d)))

    @staticmethod
    def _load(d):
        out = {}
        for p in sorted(d.iterdir()) if d.is_dir() else []:
            if p.is_file() and p.suffix in (".py", ".md"):
                try:
                    sk = parse_skill(p)
                except (OSError, UnicodeDecodeError) as e:
                    sk = {"name": p.stem, "description": "", "error": str(e), "body": "", "actions": {}, "path": str(p)}
                out[sk["name"]] = sk
        return out

    def enabled(self):
        dis = set(self.s.config["settings"].get("disabled_skills", []))
        return {k: v for k, v in self.all().items() if k not in dis}

    def run(self, sk, action, args, emit, cancel, ask=None, cwd=None):
        if sk.get("requires"):
            deps = Path(sk["path"]).parent / ".deps"
            mark = deps / (".ok-" + hashlib.sha1(sk["requires"].encode()).hexdigest()[:8])
            if not mark.exists():
                emit({"t": "status", "text": f"pip install {sk['requires']}"})
                r = subprocess.run([sys.executable, "-m", "pip", "install", "-q", "--target", str(deps)] + sk["requires"].replace(",", " ").split(),
                                   capture_output=True, text=True, encoding="utf-8", errors="replace", **NO_WINDOW)
                if r.returncode:  # remember success only: a failed install is retried next time, and the reason is shown now
                    return False, f"could not install {sk['requires']}: {(r.stderr or r.stdout).strip()[-1200:]}"
                deps.mkdir(exist_ok=True)
                mark.touch()
        env = dict(os.environ)
        env.update({k: str(v) for k, v in self.s.config.get("secrets", {}).items()})
        env["PYTHONIOENCODING"] = "utf-8"
        proc = subprocess.Popen([sys.executable, "-c", SKILL_RUNNER, sk["path"], action, json.dumps(args)], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, text=True, encoding="utf-8", errors="replace", bufsize=1,
                                cwd=cwd if cwd and os.path.isdir(cwd) else None, **NO_WINDOW)
        result = None

        def watch():
            while proc.poll() is None:
                if cancel.is_set():
                    proc.kill()
                    return
                time.sleep(0.1)

        threading.Thread(target=watch, daemon=True).start()
        for line in proc.stdout:
            try:
                ev = json.loads(line)
            except ValueError:
                emit({"t": "log", "text": line.rstrip()})
                continue
            if ev.get("t") == "result":
                result = ev
            elif ev.get("t") == "ask":
                ans = (ask(ev["text"], ev.get("options")) if ev.get("options") else ask(ev["text"])) if ask else ""
                proc.stdin.write((ans or "") + "\n")
                proc.stdin.flush()
            else:
                emit(ev)
        proc.wait()
        if cancel.is_set():
            raise Cancelled()
        if result is None:
            err = proc.stderr.read()[-1500:]
            return False, f"skill crashed (exit {proc.returncode}): {err}"
        v = result.get("value")
        return result.get("ok", False), v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, indent=1)


# ───────────────────────────── tools ─────────────────────────────

def _obj(props, req):
    return {"type": "object", "properties": {k: ({"type": v} if isinstance(v, str) else v) for k, v in props.items()}, "required": req}


def _p(t, d):
    return {"type": t, "description": d}


TOOLS = [
    {"name": "read", "description": "Read a file (text with line numbers as `N│line`; also pdf, office, notebooks, images) or list a directory. "
                                    "Large files: read a range with offset/limit, or grep first.",
     "parameters": _obj({"path": "string", "offset": _p("integer", "first line, 1-based"), "limit": _p("integer", "max lines (default 2000)")},
                        ["path"])},
    {"name": "write", "description": "Create a file or replace all of it (parent folders are created). Prefer edit for changes to an existing file.",
     "parameters": _obj({"path": "string", "content": "string"}, ["path", "content"])},
    {"name": "edit", "description": "Change an existing file. Replace exact text: old → new (old must occur once; all=true replaces every "
                                    "match). Several changes in one call: edits=[{old,new},…]. Or replace lines a-b with new: lines='12-20'. "
                                    "Copy old from the file exactly, without the `N│` prefix; keep it short but unique.",
     "parameters": _obj({"path": "string", "old": "string", "new": "string", "all": "boolean", "lines": "string",
                         "edits": {"type": "array", "items": {"type": "object", "properties": {"old": {"type": "string"}, "new": {"type": "string"}}}}},
                        ["path"])},
    {"name": "bash", "description": "Run a shell command in the project folder (shell named in the system prompt). Output is cut to its "
                                    "start and end when long. timeout in seconds (default 120). background=true for servers and watchers: "
                                    "returns at once with the pid and a log file.",
     "parameters": _obj({"cmd": "string", "timeout": "integer", "background": "boolean"}, ["cmd"])},
    {"name": "ls", "description": "Find files by glob, newest first: '**/*.py', 'src/**/*.{ts,tsx}'. A folder path lists that folder. "
                                  "pattern=map gives a map of the repo's files and symbols.",
     "parameters": _obj({"pattern": "string"}, ["pattern"])},
    {"name": "grep", "description": "Search file contents by regex (ripgrep syntax; .gitignore respected). Returns path:line:text.",
     "parameters": _obj({"pattern": "string", "path": _p("string", "file or folder (default: project)"), "glob": _p("string", "e.g. *.py")},
                        ["pattern"])},
    {"name": "web_search", "description": "Search the web.", "parameters": _obj({"query": "string"}, ["query"])},
    {"name": "web_fetch", "description": "Fetch a URL as text.", "parameters": _obj({"url": "string"}, ["url"])},
    {"name": "skill", "description": "Use a skill (no action = its guide).", "parameters": _obj({"name": "string", "action": "string", "args": "object"}, ["name"])},
    {"name": "todo", "description": "Plan checklist the user sees live. items = the whole list ('x ' done, '> ' doing); "
                                    "done=[n] / doing=n mark steps by number — do it as each step ends.",
     "parameters": _obj({"items": {"type": "array", "items": {"type": "string"}}, "done": {"type": "array", "items": {"type": "integer"}},
                         "doing": "integer"}, [])},
    {"name": "memory", "description": "Global memory across chats: text adds a note, forget=id removes one.",
     "parameters": _obj({"text": "string", "forget": "string"}, [])},
    {"name": "task", "description": "Run a subagent on a self-contained job in a fresh context; returns its report.",
     "parameters": _obj({"agent": "string", "prompt": "string"}, ["prompt"])},
]
# tools added only when usable (keeps the idle prompt small)
NOTIFY_TOOL = {"name": "notify", "description": "Send the user a message/file via their Telegram.",
               "parameters": _obj({"text": "string", "file": "string"}, ["text"])}
MCP_TOOL = {"name": "mcp", "description": "Use an MCP server: no tool → its tools; else call tool with args.",
            "parameters": _obj({"server": "string", "tool": "string", "args": "object"}, ["server"])}
READONLY_TOOLS = {"read", "ls", "grep", "web_search", "web_fetch"}
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".mypy_cache", "dist", "build", ".next", ".idea", ".pytest_cache",
             ".tox", "target", ".gradle", ".cache"}


def walk_files(root, limit=20000):
    n = 0
    for d, dirs, files in os.walk(root):
        dirs[:] = [x for x in dirs if x not in SKIP_DIRS and not x.startswith(".")]
        for f in files:
            yield os.path.join(d, f)
            n += 1
            if n >= limit:
                return


def glob_rx(pattern):
    """Glob → regex over '/'-separated relative paths. ** spans folders, {a,b} alternates (Python's glob has no braces)."""
    out, i, n = [], 0, len(pattern)
    while i < n:
        c = pattern[i]
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
            continue
        if pattern.startswith("**", i):
            out.append(".*")
            i += 2
            continue
        if c == "*":
            out.append("[^/]*")
        elif c == "?":
            out.append("[^/]")
        elif c == "{":
            j = pattern.find("}", i)
            if j > i:
                out.append("(?:" + "|".join(re.escape(x) for x in pattern[i + 1:j].split(",")) + ")")
                i = j + 1
                continue
            out.append(re.escape(c))
        elif c == "[":
            j = pattern.find("]", i + 1)
            if j > i:
                out.append("[" + pattern[i + 1:j].replace("!", "^", 1) + "]")
                i = j + 1
                continue
            out.append(re.escape(c))
        else:
            out.append(re.escape(c))
        i += 1
    return re.compile("".join(out) + r"\Z", re.I if WIN else 0)


def shell_env(secrets):
    """The user's secrets as env vars; with GITHUB_TOKEN, git can clone/push github.com without any setup.
    No pagers, prompts or colors: the model reads the output, nobody can answer a prompt."""
    env = dict(os.environ, **{k: str(v) for k, v in (secrets or {}).items() if re.fullmatch(r"[A-Za-z_]\w*", k)})
    for k, v in (("PAGER", "cat"), ("GIT_PAGER", "cat"), ("GIT_TERMINAL_PROMPT", "0"), ("NO_COLOR", "1"), ("PYTHONUNBUFFERED", "1"),
                 ("PYTHONIOENCODING", "utf-8"), ("npm_config_yes", "true")):
        env.setdefault(k, v)
    tok = env.get("GITHUB_TOKEN") or env.get("GH_TOKEN")
    if tok:
        env.setdefault("GH_TOKEN", tok)
        n = int(env.get("GIT_CONFIG_COUNT") or 0)
        env.update({f"GIT_CONFIG_KEY_{n}": "credential.https://github.com.helper",
                    f"GIT_CONFIG_VALUE_{n}": '!f() { echo username=x-access-token; echo "password=$GH_TOKEN"; }; f',
                    "GIT_CONFIG_COUNT": str(n + 1)})
    return env


_SHELL = {}


def find_shell(pref=None):
    """(kind, executable) of the shell commands run in: bash where there is one (models write bash best; on Windows that is
    Git Bash), else PowerShell, else cmd. `pref` (settings.shell / ME_SHELL) may name a kind or a path."""
    pref = pref or os.environ.get("ME_SHELL") or ""
    if pref in _SHELL:
        return _SHELL[pref]
    kind_of = lambda p: "bash" if "bash" in os.path.basename(p).lower() else "pwsh" if "pwsh" in p.lower() else \
        "powershell" if "powershell" in p.lower() else "cmd" if os.path.basename(p).lower().startswith("cmd") else "sh"
    found = None
    if pref and pref not in ("bash", "pwsh", "powershell", "cmd", "sh") and (os.path.isfile(pref) or shutil.which(pref)):
        p = pref if os.path.isfile(pref) else shutil.which(pref)
        found = (kind_of(p), p)
    elif WIN:
        cands = {"bash": [], "pwsh": [shutil.which("pwsh")], "cmd": [os.environ.get("ComSpec") or "cmd.exe"],
                 "powershell": [shutil.which("powershell"), os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32",
                                                                        "WindowsPowerShell", "v1.0", "powershell.exe")]}
        for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramW6432"), os.environ.get("ProgramFiles(x86)"),
                     os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs"), os.path.expanduser(r"~\scoop\apps\git\current")):
            if base:
                cands["bash"].append(os.path.join(base, "bin", "bash.exe") if base.endswith("current") else os.path.join(base, "Git", "bin", "bash.exe"))
        git = shutil.which("git")
        if git:  # …\Git\cmd\git.exe → …\Git\bin\bash.exe
            cands["bash"].append(os.path.join(os.path.dirname(os.path.dirname(git)), "bin", "bash.exe"))
        b = shutil.which("bash")
        if b and "system32" not in b.lower():  # System32\bash.exe is the WSL launcher: a different machine
            cands["bash"].append(b)
        order = [pref] if pref in cands else []
        for kind in order + ["bash", "pwsh", "powershell", "cmd"]:
            p = next((c for c in cands[kind] if c and (os.path.isfile(c) or kind == "cmd")), None)
            if p:
                found = (kind, p)
                break
    else:
        b = "/bin/bash" if os.path.exists("/bin/bash") else shutil.which("bash")
        found = ("bash", b) if b and pref != "sh" else ("sh", "/bin/sh")
    _SHELL[pref] = found or ("cmd", "cmd.exe")
    return _SHELL[pref]


SHELL_NOTES = {
    "bash": "bash",
    "sh": "sh (POSIX)",
    "cmd": "cmd.exe (Windows commands: dir, type, findstr, set; or powershell -NoProfile -Command \"...\"; no POSIX syntax)",
    "powershell": "Windows PowerShell 5.1 (PowerShell syntax: Get-ChildItem, Select-String, $env:X; no && — use ; or if ($?) {…})",
    "pwsh": "PowerShell 7 (PowerShell syntax: Get-ChildItem, Select-String, $env:X)",
}


def shell_note(pref=None):
    kind, path = find_shell(pref)
    if WIN and kind == "bash":
        return "Git Bash (bash on Windows: POSIX tools, paths like /c/Users/x or C:/Users/x; Windows programs such as python, node, npm, git work)"
    return SHELL_NOTES.get(kind, kind)


def shell_argv(cmd, pref=None):
    kind, path = find_shell(pref)
    if kind in ("powershell", "pwsh"):
        pre = "$ProgressPreference='SilentlyContinue'; [Console]::OutputEncoding=[Text.Encoding]::UTF8; $OutputEncoding=[Text.Encoding]::UTF8; "
        return [path, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", pre + cmd]
    if kind == "cmd":
        return None  # shell=True: cmd.exe /c with Windows quoting rules
    return [path, "-c", cmd]


ANSI_RX = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]")


def _decode_out(b):
    """One line of command output: UTF-8, else (Windows tools writing to a pipe) the OEM code page, e.g. cp866 for Russian cmd.exe.
    Colors and progress-bar redraws (\\r) are dropped: they only cost the model tokens."""
    try:
        s = b.decode("utf-8")
    except UnicodeDecodeError:
        s = b.decode("oem" if WIN else "utf-8", "replace")
    s = ANSI_RX.sub("", s)
    if "\r" in s.rstrip("\r\n"):
        s = s.rstrip("\r\n").rsplit("\r", 1)[-1] + ("\n" if s.endswith("\n") else "")
    return s.replace("\r\n", "\n")


class _OutBuf:
    """Keeps the first and the last megabyte of a command's output (a runaway `cat` must not eat memory)."""

    def __init__(self, cap=1_000_000):
        self.head, self.tail, self.n, self.tn, self.cap, self.dropped = [], collections.deque(), 0, 0, cap, 0

    def add(self, line):
        if self.n < self.cap:
            self.head.append(line)
            self.n += len(line)
            return
        self.tail.append(line)
        self.tn += len(line)
        while self.tn > self.cap and len(self.tail) > 1:
            self.tn -= len(self.tail.popleft())
            self.dropped += 1

    def text(self):
        mid = [f"…[{self.dropped} lines dropped]…\n"] if self.dropped else []
        return "".join(self.head + mid + list(self.tail))


def kill_tree(proc):
    try:
        if WIN:  # proc.kill() only ends the shell and leaves its children holding the pipe
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True, timeout=10, **NO_WINDOW)
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except Exception:
        try:
            proc.kill()
        except OSError:
            pass


def _popen(cmd, cwd, env, pref, **kw):
    argv = shell_argv(cmd, pref)
    if WIN:
        kw.setdefault("creationflags", NO_WINDOW["creationflags"])
    else:
        kw["start_new_session"] = True
    cwd = cwd if cwd and os.path.isdir(cwd) else None  # a session folder that no longer exists: run where we are
    return subprocess.Popen(argv if argv else cmd, shell=argv is None, stdin=subprocess.DEVNULL, env=env, cwd=cwd, **kw)


def run_shell(cmd, timeout, cancel, env=None, on_line=None, cwd=None, pref=None):
    """Returns (ok, output). ok is False on a non-zero exit or a timeout."""
    proc = _popen(cmd, cwd, env, pref, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    buf, eof = _OutBuf(), threading.Event()

    def pump():
        try:
            for ln in iter(proc.stdout.readline, b""):
                s = _decode_out(ln)
                buf.add(s)
                if on_line:
                    try:
                        on_line(s.rstrip())
                    except Exception:
                        pass
        finally:
            eof.set()

    threading.Thread(target=pump, daemon=True).start()
    t0 = time.time()
    while proc.poll() is None:
        if cancel.is_set() or time.time() - t0 > timeout:
            kill_tree(proc)
            proc.wait()
            if cancel.is_set():
                raise Cancelled()
            eof.wait(1)
            return False, _long_out(buf.text()) + f"\n[timed out after {timeout}s and was stopped; for servers or watchers use background=true]"
        time.sleep(0.05)
    eof.wait(2)
    return proc.returncode == 0, _long_out(buf.text()) + (f"\n[exit {proc.returncode}]" if proc.returncode else "")


def _long_out(text, cap=30000):
    """Long output: its start and end for the model, all of it in a file it can grep or read."""
    if len(text) <= cap:
        return text
    fd, path = tempfile.mkstemp(prefix="me-out-", suffix=".log")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    return clip(text, cap) + f"\n[{len(text)} chars in all; full output: {path}]"


BACKGROUND = []  # processes started with background=true; stopped when me exits


def run_background(cmd, cwd, env, pref=None):
    """Start a long-running command (dev server, watcher) detached, its output going to a log file. Returns (ok, text)."""
    d = Path(tempfile.gettempdir()) / "me-bg"
    d.mkdir(exist_ok=True)
    log = d / f"{time.strftime('%H%M%S')}-{uuid.uuid4().hex[:4]}.log"
    f = open(log, "wb")
    kw = {"creationflags": NO_WINDOW["creationflags"] | 0x00000200} if WIN else {}  # + CREATE_NEW_PROCESS_GROUP
    proc = _popen(cmd, cwd, env, pref, stdout=f, stderr=subprocess.STDOUT, **kw)
    f.close()
    BACKGROUND.append(proc)
    time.sleep(1.5)  # long enough to catch an immediate failure (port in use, command not found)
    head = _decode_out(log.read_bytes()[-4000:]).strip()
    stop = f"taskkill /F /T /PID {proc.pid}" if WIN else f"kill {proc.pid}"
    if proc.poll() is not None:
        return proc.returncode == 0, f"exited at once with code {proc.returncode}\n{head}"
    return True, f"running in background · pid {proc.pid} · log {log} (read it to check progress) · stop: {stop}" + (f"\n{head}" if head else "")


def stop_background():
    for p in BACKGROUND:
        if p.poll() is None:
            kill_tree(p)
    BACKGROUND.clear()


atexit.register(stop_background)


# ───────────────────────────── plugins ─────────────────────────────


class PluginCtx:
    """What a plugin tool receives: live UI events + cancellation."""

    def __init__(self, emit, cancel, ask=None):
        self.emit, self.cancel, self._ask = emit, cancel, ask

    def log(self, text):
        self.emit({"t": "log", "text": str(text)})

    def status(self, text):
        self.emit({"t": "status", "text": str(text)})

    def progress(self, v, label="", n=None, total=None):
        self.emit({"t": "progress", "v": v, "label": label, "n": n, "total": total})

    def ask(self, q, options=None):
        return (self._ask(q, options) if options else self._ask(q)) if self._ask else ""


class Plugins:
    """Plugins are trusted .py files in ~/.me/plugins, executed in-process at startup.
    They get `plugin` (this registry) and `me` (this module — anything can be patched).
      @plugin.tool("name", "description", {"arg": "string"}, confirm=False)  def fn(args, ctx) -> str
      @plugin.command("name", "description")                                   def fn(tui, arg)
      @plugin.on("key"|"event"|"startup"|"tick"|"status"|"system_prompt"|"before_tool")
    """

    def __init__(self):
        self.tools, self.commands, self.hooks, self.loaded, self.errors = {}, {}, {}, {}, {}
        self.store = self.tui = None

    def tool(self, name, description, params=None, confirm=False, required=None):
        def deco(fn):
            props = {k: (v if isinstance(v, dict) else {"type": v}) for k, v in (params or {}).items()}
            schema = {"name": name, "description": description,
                      "parameters": {"type": "object", "properties": props, "required": required if required is not None else list(props)}}
            self.tools[name] = {"fn": fn, "schema": schema, "confirm": confirm}
            return fn
        return deco

    def command(self, name, description=""):
        def deco(fn):
            self.commands[name] = (fn, description)
            return fn
        return deco

    def on(self, event):
        def deco(fn):
            self.hooks.setdefault(event, []).append(fn)
            return fn
        return deco

    def theme(self, t):
        """Register a color theme: {"name", "label", "dark", "colors": {...}} (see THEME_TOKENS)."""
        BUILTIN_THEMES[t["name"]] = t
        return t

    def emit(self, event, *a):
        """Call hooks in order; first non-None result wins."""
        for fn in self.hooks.get(event, []):
            try:
                r = fn(*a)
            except Exception as e:
                self.errors[getattr(fn, "__module__", "?")] = f"{event}: {e}"
                continue
            if r is not None:
                return r
        return None

    def collect(self, event, *a):
        out = []
        for fn in self.hooks.get(event, []):
            try:
                r = fn(*a)
                if r:
                    out.append(r)
            except Exception as e:
                self.errors[getattr(fn, "__module__", "?")] = f"{event}: {e}"
        return out

    def load(self, store):
        self.store = store
        d = store.home / "plugins"
        disabled = set(store.config["settings"].get("disabled_plugins", []))
        for p in sorted(d.glob("*.py")) if d.exists() else []:
            if p.stem in disabled:
                continue
            g = {"__name__": "me_plugin_" + p.stem, "__file__": str(p), "plugin": self, "me": sys.modules[__name__]}
            try:
                exec(compile(p.read_text("utf-8"), str(p), "exec"), g)
                self.loaded[p.stem] = g
            except Exception as e:
                self.errors[p.stem] = f"{type(e).__name__}: {e}"

    def tool_schemas(self):
        return [t["schema"] for t in self.tools.values()]


PLUGINS = Plugins()


# ───────────────────────────── web tools ─────────────────────────────

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"


def html_text(s):
    s = re.sub(r"(?is)<(script|style|noscript|svg|head|nav|footer)\b.*?</\1>", "", s)
    s = re.sub(r"(?i)<h([1-6])[^>]*>", lambda m: "\n" + "#" * int(m.group(1)) + " ", s)
    s = re.sub(r"(?i)<li[^>]*>", "\n- ", s)
    s = re.sub(r"(?i)<(br|/p|/div|/h\d|/tr|/pre|/section|/article)[^>]*>", "\n", s)
    s = re.sub(r"<[^>]+>", "", s)
    import html as _h
    s = _h.unescape(s)
    return re.sub(r"\n\s*\n+", "\n\n", re.sub(r"[ \t]+", " ", s)).strip()


def web_fetch(url):
    try:
        r = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/html,application/json,text/plain,*/*",
                                                        "Accept-Encoding": "gzip, deflate"})
        with urllib.request.urlopen(r, timeout=30) as resp:
            ct = resp.headers.get("Content-Type", "")
            data = resp.read(5_000_000)
            enc = (resp.headers.get("Content-Encoding") or "").lower()
    except Exception as e:
        return False, f"error: {e}", None
    if enc == "gzip" or data[:2] == b"\x1f\x8b":
        data = zlib.decompress(data, 16 + zlib.MAX_WBITS)
    elif enc == "deflate":
        data = zlib.decompress(data)
    if "pdf" in ct:
        fd, tmp = tempfile.mkstemp(suffix=".pdf")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            return True, clip(_pdf_text(tmp), 40000), None
        finally:
            os.unlink(tmp)
    text = data.decode("utf-8", "replace")
    if "html" in ct or text.lstrip()[:15].lower().startswith(("<!doctype", "<html")):
        text = html_text(text)
    return True, clip(text, 40000), None


def deepseek_key(cfg):
    k = (cfg.get("search") or {}).get("deepseek_key")
    if k:
        return k
    for p in cfg.get("providers", {}).values():
        if "deepseek.com" in p.get("base_url", "") and p.get("api_key"):
            return p["api_key"]
    return None


def web_search(cfg, query, cancel=None):
    key = deepseek_key(cfg)
    if key:
        body = {"model": (cfg.get("search") or {}).get("model") or "deepseek-v4-flash", "input": query,
                "tools": [{"type": "web_search"}], "tool_choice": {"type": "web_search"}, "max_output_tokens": 4000}
        try:
            r = urllib.request.Request("https://api.deepseek.com/responses", data=json.dumps(body).encode(), method="POST",
                                       headers={"Content-Type": "application/json", "Authorization": "Bearer " + key})
            with urllib.request.urlopen(r, timeout=180) as resp:
                j = json.loads(resp.read())
            msgs = [o for o in j.get("output", []) if o.get("type") == "message"]
            final = [m for m in msgs if m.get("phase") == "final_answer"] or msgs[-1:]
            ans = "".join(c.get("text", "") for m in final for c in m.get("content", []) if c.get("type") == "output_text")
            urls = []
            for m in final:
                for c in m.get("content", []):
                    urls += [a.get("url") for a in c.get("annotations", []) if a.get("url")]
            for o in j.get("output", []):
                if o.get("type") == "web_search_call" and (o.get("action") or {}).get("url"):
                    urls.append(o["action"]["url"])
            src = "\n".join(f"- {u}" for u in dict.fromkeys(urls))
            if ans.strip():
                return True, ans + (f"\n\nSources:\n{src}" if src else ""), None
        except Exception:
            pass  # fall back to DuckDuckGo
    try:
        import urllib.parse as up
        r = urllib.request.Request("https://html.duckduckgo.com/html/?q=" + up.quote(query), headers={"User-Agent": UA})
        page = urllib.request.urlopen(r, timeout=30).read().decode("utf-8", "replace")
    except Exception as e:
        return False, f"search failed: {e}", None
    out = []
    for m in re.finditer(r'class="result__a" href="([^"]+)"[^>]*>(.*?)</a>.*?class="result__snippet"[^>]*>(.*?)</a>', page, re.S):
        href = m.group(1)
        u = up.parse_qs(up.urlparse(href).query).get("uddg", [href])[0]
        out.append(f"- {html_text(m.group(2))} — {u}\n  {html_text(m.group(3))}")
        if len(out) >= 8:
            break
    return bool(out), "\n".join(out) or "no results", None


# ───────────────────────────── toolbox ─────────────────────────────

class ToolBox:
    def __init__(self, store, skills):
        self.s, self.skills = store, skills

    def preview(self, name, a, cwd=None):
        """Short human label for UI."""
        if name in ("read", "write", "edit"):
            p = str(a.get("path", ""))
            try:
                rel = relp(p, cwd)
                return rel if not rel.startswith("..") else p
            except ValueError:
                return p
        if name == "bash":
            return ("&  " if a.get("background") in (True, "true", 1) else "") + str(a.get("cmd", ""))
        if name == "web_search":
            return a.get("query", "")
        if name == "web_fetch":
            return a.get("url", "")
        if name == "task":
            return f"{a.get('agent') or 'code'}: {a.get('prompt', '')[:80]}"
        if name == "todo":
            return f"{sum(1 for i in a.get('items') or [] if str(i).lstrip().startswith(('x ', '[x]', 'X ')))}/{len(a.get('items') or [])} done"
        if name == "mcp":
            return a.get("server", "") + (f" · {a['tool']}" if a.get("tool") else "")
        if name == "notify":
            return a.get("text", "")[:80]
        if name in ("ls", "grep"):
            return a.get("pattern", "") + (f"  in {a['path']}" if a.get("path") else "")
        if name == "skill":
            return a.get("name", "") + (f" · {a['action']}" if a.get("action") else "")
        return json.dumps(a)[:80]

    def needs_confirm(self, name, a, cwd=None):
        """False = run, True = ask the user, str = refuse with this reason (read-only mode)."""
        st = self.s.config["settings"]
        mode = perm_mode(st)
        if mode == "full":
            return False
        if name == "bash":
            if cmd_allowed(a.get("cmd", ""), st.get("allow") or DEFAULT_ALLOW):
                return False
            return "read-only mode: commands are off (only allow-listed ones run). Tell the user what to run." if mode == "read" else True
        if name in ("write", "edit"):
            if mode == "read":
                return "read-only mode: file changes are off. Describe the change instead."
            root = os.path.normcase(os.path.abspath(cwd or os.getcwd()))
            p = os.path.normcase(os.path.abspath(os.path.join(cwd or os.getcwd(), os.path.expanduser(a.get("path", "")))))
            return not (p == root or p.startswith(root.rstrip(os.sep) + os.sep))
        if name in PLUGINS.tools:
            return PLUGINS.tools[name].get("confirm", False)
        if name == "skill" and a.get("action"):
            sk = self.skills.enabled().get(a.get("name"), {})
            return sk.get("actions", {}).get(a["action"], {}).get("confirm", False)
        return False

    def run(self, name, a, emit, cancel, ask=None, cwd=None):
        """Returns (ok, text, image_data_url|None). Relative paths are relative to the session's folder (`cwd`)."""
        cwd = cwd or os.getcwd()
        at = lambda x: Path(cwd, os.path.expanduser(str(x)))
        if name == "read":
            out, img = read_any(at(a["path"]), a.get("offset", 1), a.get("limit", 2000))
            return not out.startswith("error"), out, img
        if name == "write":
            p = at(a["path"])
            if p.is_dir():
                return False, f"error: {a['path']} is a folder", None
            p.parent.mkdir(parents=True, exist_ok=True)
            old, fmt = read_text_file(p) if p.exists() else (None, None)
            content = str(a["content"])
            write_text_file(p, content, fmt)  # an existing file keeps its line ends and encoding
            emit({"t": "diff", "text": self._diff(old or "", content.replace("\r\n", "\n"), p.name)})
            return True, f"{'overwrote' if old is not None else 'created'} {a['path']} ({len(content.splitlines())} lines)" + syntax_note(p, content), None
        if name == "edit":
            p = at(a["path"])
            if not p.is_file():
                return False, f"error: {a['path']} not found (use write to create it)", None
            text, fmt = read_text_file(p)
            new, msg = apply_edits(text, a)
            if new is None:
                return False, msg, None
            write_text_file(p, new, fmt)
            emit({"t": "diff", "text": self._diff(text, new, p.name)})
            return True, f"edited {a['path']} ({msg})" + syntax_note(p, new), None
        if name == "bash":
            env, pref = shell_env(self.s.config.get("secrets")), self.s.config["settings"].get("shell")
            if a.get("background") in (True, "true", 1):
                return run_background(a["cmd"], cwd, env, pref) + (None,)
            ok, out = run_shell(a["cmd"], int(a.get("timeout") or 120), cancel, env, cwd=cwd, pref=pref)
            return ok, out or "(no output)", None
        if name == "ls":
            pat = str(a.get("pattern") or ".").strip()
            if pat == "map" or pat.startswith("map "):
                return True, repo_map(cwd, pat[4:].strip(), self.s.home), None
            return True, list_glob(cwd, pat), None
        if name == "grep":
            return self._grep(a["pattern"], str(at(a.get("path") or ".")), a.get("glob"), cwd)
        if name == "skill":
            return self._skill(a, emit, cancel, ask, cwd)
        if name == "notify":
            return self._notify(a)
        if name == "mcp":
            return self._mcp(a, emit)
        if name == "web_search":
            emit({"t": "status", "text": "searching"})
            return web_search(self.s.config, a["query"], cancel)
        if name == "web_fetch":
            return web_fetch(a["url"])
        tool = PLUGINS.tools.get(name)
        if tool:
            r = tool["fn"](a, PluginCtx(emit, cancel, ask))
            return (True, r, None) if isinstance(r, str) else (bool(r[0]), r[1], None)
        return False, f"unknown tool {name}", None

    def _notify(self, a):
        cloud = Cloud(self.s)
        if not cloud.online:
            return False, "error: not logged in to the me cloud (/login), so Telegram is not available", None
        body = {"text": a.get("text", "")}
        if a.get("file"):
            fp = Path(a["file"]).expanduser()
            if not fp.is_file():
                return False, f"error: {fp} not found", None
            if fp.stat().st_size > 45_000_000:
                return False, "error: file over 45 MB", None
            body["file"] = {"name": fp.name, "b64": base64.b64encode(fp.read_bytes()).decode()}
        try:
            r = cloud.req("POST", "/api/notify", body, timeout=120)
        except Exception as e:
            return False, f"error: {e}", None
        n = r.get("sent", 0)
        return bool(n), f"sent to Telegram ({n} chat{'s' if n != 1 else ''})" if n else "error: " + (r.get("detail") or "no Telegram chat linked"), None

    def _mcp(self, a, emit):
        try:
            c = mcp_client(self.s, a.get("server", ""))
        except KeyError:
            return False, f"no MCP server '{a.get('server')}'. configured: {', '.join(self.s.config.get('mcp') or {}) or 'none'}", None
        try:
            if not a.get("tool"):
                emit({"t": "status", "text": "starting " + c.name})
                return True, f"Tools of {c.name} (call: mcp server={c.name} tool=… args={{…}}):\n" + mcp_describe(c.list_tools()), None
            emit({"t": "status", "text": a["tool"]})
            ok, out = c.call(a["tool"], a.get("args") or {})
            return ok, clip(out, 30000), None
        except Exception as e:
            return False, f"error: {e}", None

    @staticmethod
    def _diff(a, b, name):
        d = list(difflib.unified_diff(a.splitlines(), b.splitlines(), name, name, n=1, lineterm=""))[2:]
        return "\n".join(d[:80]) + ("\n…" if len(d) > 80 else "")

    @staticmethod
    def _grep(pattern, path, gl, cwd="."):
        def rel(f):
            try:
                r = os.path.relpath(f, cwd)
            except ValueError:  # another drive
                return f
            return f if r.startswith("..") else r.replace("\\", "/")
        if shutil.which("rg"):
            cmd = ["rg", "-n", "--no-heading", "--color", "never", "-M", "300", "-m", "50", "-e", pattern] + (["-g", gl] if gl else [])
            r = subprocess.run(cmd + ["--", rel(path)], capture_output=True, cwd=cwd, **NO_WINDOW)
            out = r.stdout.decode("utf-8", "replace")
            if r.returncode == 2 and not out:  # a bad regex or path: say so instead of "no matches"
                return False, "error: " + r.stderr.decode("utf-8", "replace").strip()[:500], None
            return True, clip(out, 20000) or "no matches", None
        try:
            rx = re.compile(pattern)
        except re.error as e:
            return False, f"error: bad regex: {e}", None
        out = []
        grx = glob_rx(gl) if gl else None
        files = [path] if os.path.isfile(path) else walk_files(path)
        for f in files:
            r = rel(f)
            if grx and not (grx.match(os.path.basename(f)) or grx.match(r)):
                continue
            try:
                if os.path.getsize(f) > 2_000_000:
                    continue
                with open(f, "rb") as fh:
                    data = fh.read()
                if b"\0" in data[:4096]:
                    continue
                for i, line in enumerate(decode_text(data)[0].splitlines(), 1):
                    if rx.search(line):
                        out.append(f"{r}:{i}:{line.rstrip()[:300]}")
                        if len(out) >= 300:
                            return True, "\n".join(out) + "\n… (truncated)", None
            except OSError:
                continue
        return True, "\n".join(out) or "no matches", None

    def _skill(self, a, emit, cancel, ask, cwd=None):
        sks = self.skills.enabled()
        sk = sks.get(a.get("name"))
        if not sk:
            return False, f"no skill '{a.get('name')}'. available: {', '.join(sks) or 'none'}", None
        act = a.get("action")
        if not act:
            acts = "\n".join(f"- {v['sig']}: {v['doc']}" for v in sk["actions"].values())
            return True, (sk["body"] or sk["description"]) + (f"\n\nActions (skill name={sk['name']}, action=…, args={{…}}):\n{acts}" if acts else ""), None
        if act not in sk["actions"]:
            return False, f"no action '{act}' in {sk['name']}. actions: {', '.join(sk['actions'])}", None
        ok, out = self.skills.run(sk, act, a.get("args") or {}, emit, cancel, ask, cwd)
        return ok, clip(out, 30000), None


def list_glob(cwd, pat):
    """ls tool: a folder lists itself; otherwise a glob over the project (heavy folders skipped unless named), newest first."""
    p = Path(cwd, os.path.expanduser(pat))
    if not re.search(r"[*?\[{]", pat):
        if p.is_dir():
            return read_any(p)[0]
        return pat if p.exists() else f"no match: {pat} does not exist"
    norm = pat.replace("\\", "/")
    head = []
    for part in norm.split("/"):
        if re.search(r"[*?\[{]", part):
            break
        head.append(part)
    base = Path(cwd, *head) if head else Path(cwd)
    if norm.startswith("/") or re.match(r"[A-Za-z]:", norm):
        base, root = Path("/".join(head) or "/"), ""
    else:
        root = cwd
    rx = glob_rx(norm)
    named = set(re.findall(r"[\w.-]+", norm)) & SKIP_DIRS  # e.g. 'node_modules/x/**' is searched when asked for
    hits = []
    for d, dirs, files in os.walk(base):
        dirs[:] = [x for x in dirs if x in named or (x not in SKIP_DIRS and not x.startswith("."))]
        for f in files + [x + "/" for x in dirs if norm.endswith("/")]:
            full = os.path.join(d, f.rstrip("/"))
            r = os.path.relpath(full, root).replace("\\", "/") if root else full.replace("\\", "/")
            if rx.match(r):
                hits.append(r)
        if len(hits) > 5000:
            break
    def mt(h):
        try:
            return -os.path.getmtime(os.path.join(root, h) if root else h)
        except OSError:
            return 0
    hits.sort(key=mt)
    return "\n".join(hits[:300]) + (f"\n… {len(hits) - 300} more" if len(hits) > 300 else "") if hits else "no matches"


def _closest(text, old):
    """Hint for a failed match: the most similar line in the file, with its number."""
    first = next((l for l in old.splitlines() if l.strip()), old).strip()
    stripped = [l.strip() for l in text.splitlines()]
    best = difflib.get_close_matches(first, stripped, n=1, cutoff=0.5)
    if not best:
        return " Read the file again and copy the text exactly (or use lines='a-b')."
    i = stripped.index(best[0])
    return f" Closest is line {i + 1}: {text.splitlines()[i][:200]!r}. Read the file again and copy exactly (or use lines='a-b')."


_FUZZ = str.maketrans({"\u2018": "'", "\u2019": "'", "\u201a": "'", "\u201b": "'", "\u201c": '"', "\u201d": '"', "\u201e": '"',
                       "\u201f": '"', "\u2010": "-", "\u2011": "-", "\u2012": "-", "\u2013": "-", "\u2014": "-", "\u2015": "-",
                       "\u2212": "-", "\u00a0": " ", "\u202f": " ", "\u2009": " ", "\u200a": " ", "\u3000": " "})


def _line_hits(lines, olds, key):
    ko = [key(x) for x in olds]
    kt = [key(x) for x in lines]
    n = len(ko)
    return [i for i in range(len(kt) - n + 1) if kt[i:i + n] == ko]


def _shift(lines, fi, oi):
    out = []
    for l in lines:
        if not l.strip() or fi == oi:
            out.append(l)
        elif fi.startswith(oi):
            out.append(fi[len(oi):] + l)
        elif oi.startswith(fi) and l.startswith(oi[len(fi):]):
            out.append(l[len(oi) - len(fi):])
        else:  # tabs vs spaces
            out.append(fi + l[len(oi):] if oi and l.startswith(oi) else l)
    return out


def _reindent(news, found, olds):
    """The model got the indentation wrong (or used tabs vs spaces): shift its new lines by the same amount. Its first line
    is often copied from the first non-blank character, so the shift comes from the next lines when there are any."""
    ind = lambda l: l[:len(l) - len(l.lstrip())]
    pairs = [(ind(f), ind(o)) for f, o in zip(found, olds) if o.strip()]
    if not pairs:
        return news
    fi, oi = pairs[1] if len(pairs) > 1 and olds[0].strip() and not ind(olds[0]) and ind(found[0]) else pairs[0]
    out = _shift(news, fi, oi)
    if news and olds[0].strip() and not ind(olds[0]) and ind(found[0]) and not ind(news[0]):
        out[0] = ind(found[0]) + news[0]  # the cut-off first line takes the file's own indentation
    return out


def _replace_one(text, old, new, all_):
    """Exact match first. Then, for slips models often make: curly quotes / dashes / odd spaces, trailing whitespace,
    and wrong indentation (the replacement is re-indented to fit). Returns (text, summary) or (None, error)."""
    if not old:
        return None, "error: old is empty (to insert, include a nearby line in old, or use lines='a-b')"
    if old == new:
        return None, "error: old and new are the same — nothing to change"
    n = text.count(old)
    if n > 1 and not all_:
        return None, f"error: old text matches {n} times; add surrounding lines to make it unique, or set all=true"
    if n:
        return text.replace(old, new), f"{n} replacement{'s' if n > 1 else ''}, line {text[:text.index(old)].count(chr(10)) + 1}"
    t2, o2 = text.translate(_FUZZ), old.translate(_FUZZ)  # 1:1 character map, so positions stay valid
    if o2 != old or t2 != text:
        k = t2.count(o2)
        if k == 1:
            i = t2.index(o2)
            return text[:i] + new + text[i + len(old):], f"1 replacement, line {text[:i].count(chr(10)) + 1} (matched ignoring quote/dash style)"
    lines = text.split("\n")
    olds, news = old.split("\n"), new.split("\n")
    if len(olds) > 1 and olds[-1] == "":  # old ended at a line break: compare whole lines
        olds = olds[:-1]
        news = news[:-1] if news and news[-1] == "" else news
    for how, key in (("trailing whitespace", lambda l: l.translate(_FUZZ).rstrip()), ("indentation", lambda l: l.translate(_FUZZ).strip())):
        if not any(key(x) for x in olds):
            break
        hits = _line_hits(lines, olds, key)
        if len(hits) > 1 and not all_:
            return None, f"error: old text matches {len(hits)} places (ignoring {how}); add surrounding lines to make it unique"
        if hits:
            for i in reversed(hits):
                repl = _reindent(news, lines[i:i + len(olds)], olds) if how == "indentation" else news
                lines[i:i + len(olds)] = repl
            return "\n".join(lines), f"{len(hits)} replacement{'s' if len(hits) > 1 else ''}, line {hits[0] + 1} (matched ignoring {how})"
    return None, "error: old text not found." + _closest(text, old)


EDIT_ALIASES = (("old", ("old_string", "oldText", "old_str", "oldString", "search")), ("new", ("new_string", "newText", "new_str", "newString", "replace")),
                ("all", ("replace_all", "replaceAll")))


def _edit_keys(e):
    """Models trained on other agents send old_string/oldText/…: accept those names too."""
    if not isinstance(e, dict):
        return e
    e = dict(e)
    for k, alts in EDIT_ALIASES:
        for x in alts:
            if k not in e and x in e:
                e[k] = e.pop(x)
    return e


def syntax_note(path, text):
    """Python/JSON broken by a write or edit: say so in the same result, so the model fixes it now instead of at the test run."""
    ext = Path(path).suffix.lower()
    try:
        if ext == ".py":
            compile(text, str(path), "exec", dont_inherit=True)
        elif ext == ".json":
            json.loads(text)
    except SyntaxError as e:
        return f"\nwarning: the file no longer parses — line {e.lineno}: {e.msg}. Fix it now."
    except ValueError as e:
        return f"\nwarning: invalid JSON now — {e}. Fix it now."
    except Exception:
        pass
    return ""


def apply_edits(text, a):
    """edit tool: one old/new pair, several (edits=[…]), or lines a..b (1-based, inclusive) replaced by new.
    All-or-nothing. Text uses \n line ends. Returns (new_text, summary) or (None, error)."""
    a = _edit_keys(a)
    if a.get("lines") not in (None, ""):
        lines = text.split("\n")
        m = re.fullmatch(r"\s*(\d+)\s*(?:[-:,.]+\s*(\d+))?\s*", str(a["lines"]))
        if not m:
            return None, "error: lines must look like '12-20' (or '12' for one line)"
        lo, hi = int(m.group(1)), int(m.group(2) or m.group(1))
        if lo < 1 or hi < lo - 1 or lo > len(lines) + 1 or hi > len(lines):
            return None, f"error: lines {lo}-{hi} are outside the file (1-{len(lines)}); 'a-(a-1)' inserts before line a"
        repl = str(a.get("new") or "").replace("\r\n", "\n")
        rl = [] if repl == "" else repl[:-1].split("\n") if repl.endswith("\n") else repl.split("\n")
        return "\n".join(lines[:lo - 1] + rl + lines[hi:]), f"lines {lo}-{hi} → {len(rl)} line{'s' if len(rl) != 1 else ''}"
    pairs = a.get("edits") or []
    if isinstance(pairs, str):  # some models send the list as a JSON string
        try:
            pairs = json.loads(pairs)
        except ValueError:
            return None, "error: edits must be a list of {old, new}"
    pairs = [pairs] if isinstance(pairs, dict) else list(pairs)
    pairs = [_edit_keys(e) for e in pairs]
    if a.get("old") is not None:
        pairs.insert(0, {"old": a["old"], "new": a.get("new", ""), "all": a.get("all")})
    if not pairs:
        return None, "error: give old+new, edits=[{old,new}], or lines='a-b' with new"
    done = []
    for i, e in enumerate(pairs, 1):
        if not isinstance(e, dict) or "old" not in e:
            return None, f"error: edit #{i} needs old and new"
        old, new = str(e["old"]).replace("\r\n", "\n"), str(e.get("new") or "").replace("\r\n", "\n")
        text, msg = _replace_one(text, old, new, e.get("all") or a.get("all"))
        if text is None:
            return None, (f"edit #{i} of {len(pairs)}: {msg} (nothing was written)" if len(pairs) > 1 else msg)
        done.append(msg)
    return text, "; ".join(done)


# ───────────────────────────── agent presets ─────────────────────────────
# A preset = its own system prompt + its own tool catalog (+ optional write scope, model, effort). The model stays the same.
# Built-ins below; ~/.me/agents/<name>.md (synced as agent/<name>.md, installable from the marketplace) override or add more:
#   ---
#   name: design
#   label: design
#   description: one line
#   tools: read, write, edit, bash, ls, grep, web_search, web_fetch, skill
#   writes: *            (glob of paths it may write; "*.md" = docs only; empty = read-only)
#   model:               (optional model name)   effort: (optional)
#   ---
#   prompt…
# (see the CLI tools format below)

ALL_TOOLS = "read, write, edit, bash, ls, grep, web_search, web_fetch, skill, todo, task, memory"
READ_TOOLS = "read, ls, grep, web_search, web_fetch, skill, todo"

# Only `code` is built in. Everything else (design, plan, review, debug, docs, research, me, security, …) is a marketplace agent.
# An agent file may also ship CLI tools, drawn as boxes with a colored status bar:
#   cli.<tool>: description | command template with {param} | param, other=default      (the model sees <agent>_<tool>)
#   color: red   icon: ◈      → bar color / glyph of its CLI boxes
CODE_PROMPT = """Mode: CODE — one universal engineer. Fit the approach to the request instead of announcing modes:
- build / fix / refactor: read first, smallest correct change, verify.
- bug: reproduce, test hypotheses with the cheapest experiment, fix the root cause, rerun to prove it.
- review: read-only unless asked to fix; report real bugs only (file:line, what breaks, concrete fix).
- plan / design: explore, write the steps (PLAN.md or todo) and wait for a go before big changes; UI work starts from tokens and states.
- docs / research: verify commands and facts against the code or primary sources; cite.
- ops, scripts, data, files: do it with the shell tools; confirm before anything destructive.
More specialised agents (design, security, devops, tests, plan, review, debug, docs, research, me…) are in the marketplace: /agent → "+ more agents".
When asked to extend yourself, write files into {home} (they sync everywhere): skills/<name>.py|.md, agents/<name>.md, themes/<name>.json, plugins/<name>.py.
An agent file is front matter (name, label, description, icon, color, tools, writes, and optional `cli.<tool>: description | command with {param} | param, other=default` lines that add CLI tools with a status bar), then --- and the prompt."""

BUILTIN_PRESETS = {
    "code": {"label": "code", "description": "universal engineer: build, fix, debug, review, plan, docs", "tools": ALL_TOOLS, "writes": "*", "prompt": CODE_PROMPT},
}


def parse_cli(meta, agent):
    """cli.<tool>: desc | cmd | params  →  {tool_name: {desc, cmd, params: {name: default|None}, label}}"""
    out = {}
    for k, v in meta.items():
        if not k.startswith("cli."):
            continue
        label = re.sub(r"[^a-z0-9_]+", "_", k[4:].strip().lower()).strip("_")
        segs = [x.strip() for x in re.split(r"\s\|\s", v + " ")]  # " | " separates fields; a pipe inside the command is fine in the middle
        if len(segs) < 2:
            continue
        parts = [segs[0], " | ".join(segs[1:-1]) if len(segs) > 2 else segs[1]] + ([segs[-1]] if len(segs) > 2 else [])
        if not label or not parts[1]:
            continue
        params = {}
        for x in (parts[2].split(",") if len(parts) > 2 else []):
            n, eq, d = x.strip().partition("=")
            n = re.sub(r"\W+", "", n)
            if n:
                params[n] = d.strip() if eq else None
        out[f"{agent}_{label}"] = {"desc": parts[0] or label, "cmd": parts[1], "params": params, "label": label}
    return out


def quote_arg(v):
    v = str(v)
    return '"' + v.replace('"', '\\"') + '"' if WIN else shlex.quote(v)


def render_cli(spec, args):
    """Fill {param} placeholders (shell-quoted). Returns (cmd, error)."""
    vals = {}
    for n, d in spec["params"].items():
        v = args.get(n)
        v = d if v in (None, "") else v
        if v is None:
            return "", f"error: missing parameter {n}"
        vals[n] = quote_arg(v)
    return re.sub(r"\{(\w+)\}", lambda m: vals.get(m.group(1), m.group(0)), spec["cmd"]), ""


def cli_schema(name, spec, agent):
    props = {n: {"type": "string", "description": ("default " + d) if d is not None else "required"} for n, d in spec["params"].items()}
    return {"name": name, "description": f"[{agent} CLI] {spec['desc']}",
            "parameters": {"type": "object", "properties": props, "required": [n for n, d in spec["params"].items() if d is None]}}


def presets(store):
    d = store.home / "agents"
    got = cached(("presets", str(d)), dir_sig(d, "*.md"), lambda: _load_presets(d))
    return {k: dict(v) for k, v in got.items()}


def _load_presets(d):
    out = {k: dict(v, name=k) for k, v in BUILTIN_PRESETS.items()}
    for p in sorted(d.glob("*.md")) if d.exists() else []:
        text = p.read_text("utf-8", "replace").replace("\r\n", "\n")
        m = re.match(r"^---\n(.*?)\n---\n?(.*)$", text, re.S)
        meta, body = {}, text
        if m:
            for line in m.group(1).splitlines():
                k, _, v = line.partition(":")
                if k.strip():
                    meta[k.strip()] = v.strip()
            body = m.group(2)
        name = meta.get("name") or p.stem
        base = out.get(name, {"tools": ALL_TOOLS, "writes": "*"})
        # frontmatter may carry its own name/prompt/custom keys; build the dict in one place so none of them clash
        fields = {k: v for k, v in meta.items() if v != ""}
        fields = {k: v for k, v in fields.items() if not k.startswith("cli.")}
        fields.update(name=name, prompt=body.strip(), custom=True)
        cli = parse_cli(meta, name)
        out[name] = dict(base, **fields)
        if cli:
            out[name]["cli"] = cli
    return out


def preset(store, name=None):
    ps = presets(store)
    return ps.get(name or store.config.get("agent") or "code") or ps["code"]


def preset_tools(p):
    return {t.strip() for t in str(p.get("tools") or ALL_TOOLS).split(",") if t.strip()}


def may_write(p, path):
    globs = [g.strip() for g in str(p.get("writes", "*")).split(",") if g.strip()]
    return any(fnmatch.fnmatch(path, g) or fnmatch.fnmatch(os.path.basename(path), g) for g in globs)


CONTEXT_FILES = ("AGENTS.md", "ME.md", "CLAUDE.md", "DESIGN.md", "PLAN.md")


# ───────────────────────────── repo map ─────────────────────────────
# Compact index of files + top-level symbols, built lazily (map tool) and cached per workspace by file mtime.

MAP_EXT = {".py", ".js", ".mjs", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java", ".kt", ".swift", ".rb", ".php", ".c", ".h", ".cc",
           ".cpp", ".hpp", ".cs", ".scala", ".lua", ".sh", ".vue", ".svelte", ".dart", ".ex", ".css", ".scss", ".html", ".md", ".sql"}
SYM_RX = re.compile(r"^(?:export\s+)?(?:default\s+)?(?:pub(?:\(\w+\))?\s+)?(?:async\s+)?(?:abstract\s+)?(?:static\s+)?"
                    r"(?:def|class|function\*?|func|fn|struct|enum|trait|interface|type|impl|module|object|const|let|var)\s+"
                    r"(?:\([^)]*\)\s*)?([A-Za-z_$][\w$]*)")
MD_RX = re.compile(r"^#{1,2}\s+(.+)")
CSS_RX = re.compile(r"^([.#]?[A-Za-z][\w-]*)\s*[{,]")


def file_symbols(path):
    ext = os.path.splitext(path)[1].lower()
    out = []
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f):
                if i > 4000 or len(out) >= 40:
                    break
                if ext == ".md":
                    m = MD_RX.match(line)
                elif ext in (".css", ".scss"):
                    m = CSS_RX.match(line)
                else:
                    m = SYM_RX.match(line)  # top level only: no leading indent
                if m:
                    out.append(m.group(1).strip()[:40])
    except OSError:
        pass
    return out


def repo_map(root, sub="", home=HOME, limit=12000):
    root = os.path.abspath(root)
    cache_p = Path(home) / "cache" / ("map-" + hashlib.sha1(root.encode()).hexdigest()[:12] + ".json")
    cache = jload(cache_p, {}) or {}
    fresh, lines = {}, []
    base = os.path.join(root, sub) if sub else root
    for f in sorted(walk_files(base, 5000)):
        rel = os.path.relpath(f, root).replace("\\", "/")
        if os.path.splitext(f)[1].lower() not in MAP_EXT:
            continue
        try:
            mt = os.path.getmtime(f)
        except OSError:
            continue
        c = cache.get(rel)
        syms = c[1] if c and c[0] == mt else file_symbols(f)
        fresh[rel] = [mt, syms]
        lines.append(rel + (": " + ", ".join(syms) if syms else ""))
    if not sub:
        jsave(cache_p, fresh)
    else:
        cache.update(fresh)
        jsave(cache_p, cache)
    text = "\n".join(lines) or "no source files"
    return text if len(text) <= limit else text[:limit] + f"\n… cut; use map with path=<subdir> ({len(lines)} files)"


# ───────────────────────────── global memory ─────────────────────────────
# Notes the agent keeps across every chat and device: one JSON file per note (synced as memory/<id>.json, so two devices never overwrite
# each other). Newest notes go into the system prompt; the account page lists them all with their time.

def mem_dir(home):
    d = Path(home) / "memory"
    d.mkdir(parents=True, exist_ok=True)
    return d


def mem_list(home):
    out = []
    for p in mem_dir(home).glob("*.json"):
        d = jload(p)
        if d and d.get("text"):
            out.append(dict(d, id=p.stem))
    return sorted(out, key=lambda m: m.get("ts", 0), reverse=True)


def mem_add(home, text, source=""):
    text = " ".join(str(text).split())[:800]
    if not text:
        raise ValueError("empty note")
    mid = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4]
    jsave(mem_dir(home) / f"{mid}.json", {"text": text, "ts": time.time(), "source": source[:80]})
    return mid


def mem_forget(home, mid):
    p = mem_dir(home) / (re.sub(r"[^\w-]", "", mid) + ".json")  # (no backslash inside an f-string: Python < 3.12)
    if not p.exists():
        raise ValueError(f"no memory note {mid}")
    p.unlink()


def mem_prompt(home, limit=30, cap=3000):
    """Newest notes, for the system prompt. Bounded so memory never crowds the context."""
    lines, used = [], 0
    for m in mem_list(home)[:limit]:
        line = f"- [{time.strftime('%Y-%m-%d', time.localtime(m['ts']))} {m['id']}] {m['text']}"
        if used + len(line) > cap:
            break
        lines.append(line)
        used += len(line)
    return "\n".join(lines)


# ───────────────────────────── checkpoints ─────────────────────────────
# Before a turn first touches a file, its old content is kept; /undo restores the files and drops the turn from history.

class Checkpoints:
    KEEP = 30

    def __init__(self, store, sid):
        self.p = store.home / "checkpoints" / f"{sid}.json"

    def load(self):
        return jload(self.p, []) or []

    def push(self, cp):
        cps = self.load()[-(self.KEEP - 1):] + [cp]
        jsave(self.p, cps)

    def pop(self):
        cps = self.load()
        if not cps:
            return None
        cp = cps.pop()
        jsave(self.p, cps)
        return cp


def snapshot_file(p):
    """A file's exact content for /undo: text as is (line ends kept), other bytes as {"b64": …}; None = did not exist."""
    if not os.path.isfile(p):
        return None
    data = Path(p).read_bytes()
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return {"b64": base64.b64encode(data).decode()}


def _snap_text(old):
    return decode_text(base64.b64decode(old["b64"]))[0] if isinstance(old, dict) else (old or "")


def restore_files(files):
    for path, old in files.items():
        p = Path(path)
        if old is None:
            try:
                p.unlink()
            except OSError:
                pass
        else:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(base64.b64decode(old["b64"]) if isinstance(old, dict) else old.encode("utf-8"))


def file_diff(path, old):
    try:
        new = decode_text(Path(path).read_bytes())[0]
    except OSError:
        new = ""
    d = list(difflib.unified_diff(_snap_text(old).splitlines(), new.splitlines(), "a/" + path, "b/" + path, n=2, lineterm=""))
    return "\n".join(d[:400]) + ("\n…" if len(d) > 400 else "")


# ───────────────────────────── cost ─────────────────────────────
# $ per 1M tokens: (input, output, cached input). First substring match of the model id wins; a model's own "price" overrides.

PRICES = [
    ("gpt-5-nano", (0.05, 0.4, 0.005)), ("gpt-5-mini", (0.25, 2, 0.025)), ("gpt-5", (1.25, 10, 0.125)), ("gpt-4.1-nano", (0.1, 0.4, 0.025)),
    ("gpt-4.1-mini", (0.4, 1.6, 0.1)), ("gpt-4.1", (2, 8, 0.5)), ("gpt-4o-mini", (0.15, 0.6, 0.075)), ("gpt-4o", (2.5, 10, 1.25)),
    ("o4-mini", (1.1, 4.4, 0.275)), ("o3", (2, 8, 0.5)), ("haiku", (1, 5, 0.1)), ("sonnet", (3, 15, 0.3)), ("opus", (5, 25, 0.5)),
    ("deepseek", (0.28, 0.42, 0.028)), ("gemini-2.5-pro", (1.25, 10, 0.31)), ("gemini-2.5-flash-lite", (0.1, 0.4, 0.025)),
    ("flash-lite", (0.1, 0.4, 0.025)), ("flash", (0.3, 2.5, 0.075)), ("gemini", (1.25, 10, 0.31)), ("grok-4-fast", (0.2, 0.5, 0.05)),
    ("grok", (3, 15, 0.75)), ("kimi", (0.6, 2.5, 0.15)), ("qwen", (0.4, 1.2, 0.08)), ("glm", (0.6, 2.2, 0.11)), ("mistral", (0.4, 2, 0.04)),
]


def model_price(m):
    p = m.get("price")
    if isinstance(p, (list, tuple)) and len(p) >= 2:
        return float(p[0]), float(p[1]), float(p[2] if len(p) > 2 else p[0])
    mid = str(m.get("id", "")).lower()
    for k, v in PRICES:
        if k in mid:
            return v
    return 0.0, 0.0, 0.0


def usage_cost(m, u):
    pin, pout, pc = model_price(m)
    cached = min(u.get("cached", 0), u.get("in", 0))
    return ((u.get("in", 0) - cached) * pin + cached * pc + u.get("out", 0) * pout) / 1e6


def add_day_cost(home, cost):
    p = Path(home) / "usage.json"
    d = jload(p, {}) or {}
    day = time.strftime("%Y-%m-%d")
    d[day] = round(d.get(day, 0) + cost, 6)
    jsave(p, dict(sorted(d.items())[-90:]))
    return d[day]


def day_cost(home):
    return (jload(Path(home) / "usage.json", {}) or {}).get(time.strftime("%Y-%m-%d"), 0)


def money(x):
    return "$0" if not x else f"${x:.4f}" if x < 0.01 else f"${x:.3f}" if x < 1 else f"${x:.2f}"


# ───────────────────────────── permissions ─────────────────────────────
# settings.perm: "read" (no writes/commands unless allow-listed) · "ask" (writes inside the project are free, commands ask) · "full".
# settings.allow: command patterns that never ask, e.g. "git status*", "npm test*". Legacy settings.yolo = full.

PERMS = {"read": "read-only", "ask": "workspace-write (ask for commands)", "full": "full access"}
DEFAULT_ALLOW = ["git status*", "git diff*", "git log*", "git show*", "ls*", "pwd", "cat *", "head *", "tail *", "wc *", "which *",
                 "python3 -m pytest*", "pytest*", "npm test*", "npm run test*", "npm run build*", "npm run lint*", "go test*", "cargo test*",
                 "node --check *", "python3 -m py_compile *"]


def perm_mode(settings):
    return "full" if settings.get("yolo") else settings.get("perm") or "ask"


def cmd_allowed(cmd, patterns):
    cmd = cmd.strip()
    if not cmd or re.search(r"[;&|`<>\n]|\$\(", cmd):  # chained / redirected commands always ask
        return False
    return any(fnmatch.fnmatchcase(cmd, p) for p in patterns)


def allow_pattern(cmd):
    """'npm run build --prod' → 'npm run build*' (first two words)."""
    words = cmd.strip().split()
    return " ".join(words[:2 if len(words) > 1 else 1]) + "*"


# ───────────────────────────── MCP (lazy) ─────────────────────────────
# config.mcp = {name: {"command": "npx", "args": [...], "env": {...}, "description": ""} | {"url": "https://…/mcp", "headers": {…}}}
# Idle cost: one line per server in the prompt. Servers start on first use; tool schemas are fetched only when asked.

class MCPClient:
    def __init__(self, name, spec):
        self.name, self.spec = name, spec
        self.proc, self.lock, self.n, self.sid = None, threading.Lock(), 0, None
        self.tools = None

    def _rpc_stdio(self, msg):
        if not self.proc or self.proc.poll() is not None:
            env = dict(os.environ, **{k: str(v) for k, v in (self.spec.get("env") or {}).items()})
            self.proc = subprocess.Popen([self.spec["command"], *self.spec.get("args", [])], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                         stderr=subprocess.DEVNULL, env=env, text=True, encoding="utf-8", errors="replace", bufsize=1,
                                         **NO_WINDOW)
            self._init()
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()
        if "id" not in msg:
            return None
        while True:
            line = self.proc.stdout.readline()
            if not line:
                raise RuntimeError(f"mcp {self.name}: server exited")
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("id") == msg["id"]:
                return r

    def _rpc_http(self, msg):
        h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream", **(self.spec.get("headers") or {})}
        if self.sid:
            h["Mcp-Session-Id"] = self.sid
        req = urllib.request.Request(self.spec["url"], data=json.dumps(msg).encode(), headers=h, method="POST")
        with urllib.request.urlopen(req, timeout=120) as resp:
            self.sid = resp.headers.get("Mcp-Session-Id") or self.sid
            body = resp.read().decode("utf-8", "replace")
        if "id" not in msg:
            return None
        if resp.headers.get("Content-Type", "").startswith("text/event-stream"):
            for line in body.splitlines():
                if line.startswith("data:"):
                    try:
                        r = json.loads(line[5:])
                    except ValueError:
                        continue
                    if r.get("id") == msg["id"]:
                        return r
            raise RuntimeError("mcp: no response in stream")
        return json.loads(body)

    def _send(self, method, params=None, notify=False):
        msg = {"jsonrpc": "2.0", "method": method, "params": params or {}}
        if not notify:
            self.n += 1
            msg["id"] = self.n
        r = (self._rpc_http if self.spec.get("url") else self._rpc_stdio)(msg)
        if r and r.get("error"):
            raise RuntimeError(f"mcp {self.name}: {r['error'].get('message', r['error'])}")
        return (r or {}).get("result")

    def _init(self):
        self._send("initialize", {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "me", "version": VERSION}})
        self._send("notifications/initialized", notify=True)

    def list_tools(self):
        with self.lock:
            if self.spec.get("url") and not self.sid and self.tools is None:
                self._init()
            if self.tools is None:
                self.tools = (self._send("tools/list") or {}).get("tools", [])
            return self.tools

    def call(self, tool, args):
        with self.lock:
            if self.spec.get("url") and not self.sid:
                self._init()
            r = self._send("tools/call", {"name": tool, "arguments": args or {}}) or {}
        parts = []
        for c in r.get("content", []):
            parts.append(c.get("text") if c.get("type") == "text" else f"[{c.get('type')}]")
        return not r.get("isError"), "\n".join(p for p in parts if p) or json.dumps(r)[:4000]

    def close(self):
        if self.proc:
            try:
                self.proc.kill()
            except OSError:
                pass


MCP_CLIENTS = {}


def mcp_client(store, name):
    spec = (store.config.get("mcp") or {}).get(name)
    if not spec:
        raise KeyError(name)
    c = MCP_CLIENTS.get(name)
    if not c or c.spec != spec:
        if c:
            c.close()
        c = MCP_CLIENTS[name] = MCPClient(name, spec)
    return c


def mcp_describe(tools):
    out = []
    for t in tools:
        props = (t.get("inputSchema") or {}).get("properties", {})
        req = set((t.get("inputSchema") or {}).get("required", []))
        args = ", ".join(f"{k}{'' if k in req else '?'}: {v.get('type', 'any')}" for k, v in props.items())
        out.append(f"- {t['name']}({args}): {(t.get('description') or '').strip().splitlines()[0][:160] if t.get('description') else ''}")
    return "\n".join(out) or "(no tools)"


# ───────────────────────────── agent ─────────────────────────────

SYSTEM = """You are me, an expert software engineer working in the user's terminal.
cwd: {cwd} | os: {os} | shell: {shell} | date: {date}

Scope (most important):
- Do exactly what was asked, nothing more. Match effort to the request: a question gets an answer, a greeting a short reply, a one-line fix a one-line edit.
- Questions and chat need no tools unless the answer depends on files, commands or current facts. Never create, edit or delete files, install packages or run commands the request does not need.
- No refactors, renames, features, docs or tests nobody asked for; mention worthwhile extras in one line.
- If the request is unclear in a way that changes the result, ask one short question; otherwise pick the obvious reading and go. Once you start a task, finish all of it.

Tools:
- Explore with ls (glob), grep and read, not shell cat/find/grep. Put independent reads/searches in one message: they run in parallel.
- Read a file before editing it. edit: copy old exactly from read output without the `N│` prefix, short but unique; several changes to one file in one call (edits=[…]). If an edit fails, read that part again first. write only for new files or full rewrites.
- bash is non-interactive (pass -y / --yes, CI=1); servers and watchers need background=true.
- Never undo or delete work you did not make; no destructive commands (git reset --hard, rm -rf on broad paths, force push) unless asked; confirm anything irreversible.
- [Bracketed] "user" lines mid-task come from me (self-check, reminder, a queued user message).

Work:
- Never guess what code, an API or a command does: check it. Find the root cause before fixing; read errors fully.
- A failed approach: say what you learned and change it; never repeat the same failing call.
- 3+ steps: write a todo first, then mark each step done right after finishing it (todo done=[n]) and the next as doing. The user watches it live.
- Match the project's style; no hacks, no needless deps.
- After changing code, verify it (tests, build, lint or run it) and fix failures. Never claim untested success.
- web_search for current facts, web_fetch for docs/URLs.
UI work: no emoji; SVG icons; neutral palette + one accent; light+dark; empty/loading/error states; responsive.
Reply: in the user's language, concise. After changes: what changed, how you verified it, and anything left undone.{extra}{skills}{project}{user}"""



# Behavior switches (/behavior, web /behavior): one checkbox each, applied to the system prompt and the loop.
# key: (default, label, prompt line when on, prompt line when off)
BEHAVIOR = {
    "tests": (True, "verify changes itself (tests, build, run)", "",
              "Self-testing is OFF: the user tests everything. Never run tests, builds, linters, type checks, the app, servers, browsers "
              "or screenshots to check your work, never install tools for checking (puppeteer, playwright…), and put no checking step "
              "in your plan. When done, list the exact commands or steps for the user to check."),
    "todo": (True, "keep a todo checklist on multi-step tasks", "", ""),
    "web": (True, "search the web for current facts and docs", "", ""),
    "stay_in_project": (True, "stay in the project folder unless asked",
                        "Work inside cwd; don't explore other folders (parents, siblings, home) for anything helpful. Go outside only "
                        "when the user asks or names a path.", ""),
    "plan_first": (False, "show a plan and wait for a go before big changes",
                   "Before any change bigger than a small fix (several files, new features, refactors), give a short plan and wait for "
                   "the user's go.", ""),
    "brief": (False, "very short replies",
              "Be terse: the fewest words that answer; after changes one line on what changed. No recaps, no lists of options.", ""),
    "explain": (False, "explain the why (learning mode)",
                "The user is learning: explain the reasoning behind each change and any concept they may not know, briefly.", ""),
    "commit": (False, "git commit each finished task",
               "In a git repo, when a task is done (and verified, if you verify), commit just its changes with a clear message. "
               "Never push unless asked.", ""),
    "agents_md": (True, "offer to create AGENTS.md in new projects", "", ""),
}


NO_CHECKS = ("error: not run — the user turned self-testing off (/behavior). Do not test, build, lint or run the app; "
             "finish the work and tell the user which commands to run to check it.")
# what "checking your work" looks like on the command line: blocked outright when self-testing is off
CHECK_RX = re.compile(r"(?:^|[\s;&|(])(?:pytest|py\.test|unittest|tox|nox|jest|vitest|mocha|karma|cypress|playwright|puppeteer|"
                      r"chromium(?:-browser)?|google-chrome|headless|screenshot|lighthouse|eslint|tsc|mypy|ruff|flake8|pylint|"
                      r"(?:npm|pnpm|yarn|bun)\s+(?:run\s+)?(?:test|lint|build|check|typecheck)|go\s+(?:test|vet|build)|cargo\s+(?:test|build|check|clippy)|"
                      r"dotnet\s+(?:test|build)|mvn|gradle\w*|make\s+(?:test|check)|node\s+--check|node\s+(?:\S*/)?(?:check|test|verify|smoke)(?:[-_.]\w+)?\.[cm]?js|py_compile|vite\s+(?:build|preview)|--headless)\b", re.I)


def behavior(settings):
    """Effective switches: defaults, then the user's choices (settings.behavior); the old no_verify setting means tests off."""
    b = {k: v[0] for k, v in BEHAVIOR.items()}
    if settings.get("no_verify"):
        b["tests"] = False
    b.update({k: bool(v) for k, v in (settings.get("behavior") or {}).items() if k in BEHAVIOR})
    return b


class LimitHit(Exception):
    pass


def parse_args(raw):
    """Tool-call arguments → dict, or None. Tolerates what models commonly get wrong: a code fence around the JSON,
    text after it, a JSON object sent as a string."""
    raw = (raw or "").strip() or "{}"
    for cand in (raw, re.sub(r"^```(?:json)?\s*|\s*```$", "", raw)):
        try:
            v = json.JSONDecoder().raw_decode(cand)[0]
        except ValueError:
            continue
        if isinstance(v, str):
            try:
                v = json.loads(v)
            except ValueError:
                continue
        if isinstance(v, dict):
            return v
    return None


TRUNCATED = object()  # marker: tool arguments cut off by the output limit
LOOP_NUDGE = ("[you made the same call three times and got the same result — it will not change. Try a different approach, "
              "or stop and tell the user what blocks you]")
THINK_NUDGE = "[your reasoning used up the output token limit before you answered — think less and act or answer now]"
CUTOFF_NUDGE = "[your reply was cut off by the output token limit — continue exactly where it stopped, without repeating]"
DOC_EXT = r"\.(md|markdown|txt|rst|adoc|csv)$"


ARG_ALIASES = {  # names other agents use for the same argument: models trained on them send these
    "bash": {"command": "cmd", "script": "cmd", "commands": "cmd", "shell": "cmd"},
    "read": {"file_path": "path", "filepath": "path", "file": "path", "filename": "path", "start_line": "offset"},
    "write": {"file_path": "path", "filepath": "path", "file": "path", "filename": "path", "text": "content", "contents": "content"},
    "edit": {"file_path": "path", "filepath": "path", "file": "path", "filename": "path", "old_string": "old", "new_string": "new",
             "old_str": "old", "new_str": "new", "replace_all": "all", "old_text": "old", "new_text": "new"},
    "grep": {"query": "pattern", "regex": "pattern", "search": "pattern", "include": "glob"},
    "ls": {"path": "pattern", "glob": "pattern", "dir": "pattern", "directory": "pattern"},
    "web_search": {"q": "query", "search": "query"},
    "web_fetch": {"link": "url", "href": "url"},
}


def fix_args(name, args, schema):
    """Rename well-known argument aliases, then name any required argument still missing (instead of a KeyError)."""
    if len(args) == 1 and isinstance(next(iter(args.values())), dict) and next(iter(args)) in ("input", "args", "arguments", "parameters", "params"):
        args = next(iter(args.values()))  # {"input": {...}}: arguments wrapped one level too deep
    al = ARG_ALIASES.get(name) or {}
    args = {al.get(k, k) if al.get(k, k) not in args else k: v for k, v in args.items()}
    if name == "bash" and isinstance(args.get("cmd"), list):
        args["cmd"] = " && ".join(map(str, args["cmd"]))
    req = [r for r in ((schema or {}).get("parameters") or {}).get("required", []) if args.get(r) in (None, "")]
    if req:
        props = list(((schema or {}).get("parameters") or {}).get("properties", {}))
        return args, f"error: {name} needs {', '.join(req)} (its arguments: {', '.join(props)}; you sent: {', '.join(args) or 'nothing'})"
    return args, None


def has_images(messages):
    return any(isinstance(m.get("content"), list) and any(x.get("type") == "image_url" for x in m["content"]) for m in messages)


def tc_args(tc):
    return parse_args(tc["function"]["arguments"])


TODO_EVERY = 4  # tool rounds without a todo update (while steps are open) before the checklist is shown again


def todo_lines(items):
    return "\n".join(f"{n}. [{'x' if i['s'] == 'done' else '>' if i['s'] == 'doing' else ' '}] {i['t']}" for n, i in enumerate(items, 1))


VERIFY_NUDGE = ("[self-check] You changed files but ran nothing since. Before finishing, verify: run the tests, build, lint, "
                "or the code itself, and fix any failure. If nothing can be run, say so and why.")
GOAL_PROMPT = """GOAL: {objective}

Work autonomously until this is done. First write verifiable success criteria and a todo list. Then do one step at a time,
keep the todo current, and verify with real checks (tests, build, running the code). Do not ask the user unless truly blocked.
End your final message with exactly one line:
GOAL: DONE <the evidence that every criterion passes>
or
GOAL: BLOCKED <the precise reason and what is needed>"""


class Agent:
    """Model⇄tools loop. Frontend-agnostic: everything goes through `emit(event)`.
    Events: turn_start, text, reasoning, tool_start, tool_event, tool_end, usage, error, turn_end.
    `confirm(name, args) -> bool` and `ask(question) -> str` are provided by the frontend."""

    def __init__(self, store, emit, confirm=None, ask=None):
        self.s = store

        def _emit(e):
            PLUGINS.collect("event", e)
            emit(e)
        self.emit = _emit
        self.confirm = confirm or (lambda n, a: True)
        self.ask = ask
        self.skills = Skills(store)
        self.tools = ToolBox(store, self.skills)
        self.session = new_session()
        self.cancel = threading.Event()
        self.busy = False
        self.ctx_used = 0
        self.depth = 0  # subagent nesting
        self.persist = True  # subagents don't save their own sessions
        self.snap = {}  # path -> content before this turn first touched it (None = did not exist)
        self._turn_lock = threading.Lock()
        self.take_queued = lambda: []  # frontend hook: messages the user sent mid-turn, read after every tool step

    def system_prompt(self):
        sks = self.skills.enabled()
        skills = ("\nSkills (load via skill tool):\n" + "\n".join(f"- {k}: {v['description']}" for k, v in sks.items())) if sks else ""
        project, budget = "", 24000
        for name in self.context_files():
            text = (Path(self.session["cwd"]) / name).read_text("utf-8", "replace")[:min(10000, budget)]
            budget -= len(text)
            project += f"\n\n# {name}\n{text}"
        bh = behavior(self.s.config["settings"])
        if "AGENTS.md" in self.context_files() or any(n.endswith("/AGENTS.md") for n in self.context_files()):
            project += "\n\nAGENTS.md is this project's memory: update it when you learn something durable (commands, structure, conventions, decisions)."
        elif bh["agents_md"]:
            project += ("\n\nNo AGENTS.md here yet. Only during real multi-step work on a code project (not for questions or small tasks), "
                        "you may offer to create a concise AGENTS.md (purpose, stack, run/test commands, structure, conventions).")
        pr = self.preset()
        if pr.get("prompt"):
            project += "\n\n" + pr["prompt"].replace("{home}", str(self.s.home))
        if pr.get("cli"):
            project += "\nYour CLI tools (prefer them over hand-written shell for these jobs): " + ", ".join(f"{n} — {c['desc']}" for n, c in pr["cli"].items())
        user = ("\n\n" + self.s.config["prompt"].strip()) if self.s.config.get("prompt", "").strip() else ""
        user += "".join("\n\n" + x for x in PLUGINS.collect("system_prompt", self))
        extra = ""
        mem = mem_prompt(self.s.home)
        if mem:
            extra += "\nGlobal memory (notes you kept earlier, newest first; use memory tool to add or forget):\n" + mem
        if "task" in {t["name"] for t in self.tool_schemas()}:
            extra += "\ntask agents: " + ", ".join(presets(self.s)) + "."
        mcp = self.s.config.get("mcp") or {}
        if mcp:
            extra += "\nMCP servers (mcp tool): " + "; ".join(f"{k}{': ' + v['description'] if v.get('description') else ''}" for k, v in mcp.items())
        if os.environ.get("ME_PREVIEW"):
            extra += (f"\nPreview for the user: files in cwd are served at {os.environ['ME_PREVIEW']}/<path>; "
                      f"a dev server on port $PORT ({os.environ.get('PORT')}) at {os.environ['ME_PREVIEW']}/port/. Give the user that link.")
        prefs = [BEHAVIOR[k][2 if on else 3] for k, on in bh.items() if BEHAVIOR[k][2 if on else 3]]
        if prefs:
            extra += "\nUser's settings (they override the rules above):\n" + "\n".join("- " + x for x in prefs)
        base = SYSTEM
        if not bh["tests"]:
            base = base.replace("- After changing code, verify it (tests, build, lint or run it) and fix failures. Never claim untested success.\n", "")
        if not bh["todo"]:
            base = base.replace("- 3+ steps: write a todo first, then mark each step done right after finishing it (todo done=[n]) and the next as doing. The user watches it live.\n", "")
        return base.format(cwd=self.session["cwd"], os=f"{platform.system()} {platform.release()}", date=time.strftime("%Y-%m-%d"),
                             shell=shell_note(self.s.config["settings"].get("shell")),
                             extra=extra, skills=skills, project=project, user=user)

    def preset(self):
        return preset(self.s, self.session.get("agent") or self.s.config.get("agent"))

    def model_name(self):
        """Each chat keeps the model it was started with: switching models in one chat (or terminal) never changes another.
        A new chat starts with the model picked last."""
        return self.session.get("model") or self.preset().get("model") or self.s.config.get("model")

    def model(self):
        return self.s.model(self.model_name())

    def set_model(self, name):
        """Switch this chat's model; it also becomes the default for new chats."""
        self.session["model"] = self.s.config["model"] = name
        self.s.save()
        if self.persist and self.session.get("messages"):
            self.s.save_session(self.session)

    @property
    def cwd(self):
        return self.session.get("cwd") or os.getcwd()

    def abspath(self, path):
        """A tool path as the model meant it: relative to the session's project folder, ~ expanded."""
        return os.path.abspath(os.path.join(self.cwd, os.path.expanduser(str(path or ""))))

    def context_files(self):
        """Project instructions: CONTEXT_FILES in cwd, plus AGENTS.md / CLAUDE.md of parent folders up to the repo root
        (a monorepo's root rules apply in its subfolders). Parents come first; names are relative to cwd."""
        cwd = Path(self.session.get("cwd") or ".").resolve()
        out = [n for n in CONTEXT_FILES if (cwd / n).is_file()]
        if (cwd / ".git").exists():
            return out
        up, d, home = [], cwd, Path.home().resolve()
        for _ in range(6):
            if d.parent == d or d == home:
                break
            d = d.parent
            rel = os.path.relpath(d, cwd)
            up = [os.path.join(rel, n).replace("\\", "/") for n in ("AGENTS.md", "CLAUDE.md") if (d / n).is_file()][:1] + up
            if (d / ".git").exists():
                break
        return up + out

    def tool_schemas(self):
        allowed = preset_tools(self.preset())
        if self.depth:
            allowed.discard("task")
        bh = behavior(self.s.config["settings"])
        if not bh["todo"]:
            allowed.discard("todo")
        if not bh["web"]:
            allowed -= {"web_search", "web_fetch"}
        out = [t for t in TOOLS if t["name"] in allowed]
        if self.s.auth.get("token"):
            out.append(NOTIFY_TOOL)
        if self.s.config.get("mcp"):
            out.append(MCP_TOOL)
        pr = self.preset()
        out += [cli_schema(n, c, pr["name"]) for n, c in (pr.get("cli") or {}).items()]
        return out + PLUGINS.tool_schemas()

    def run(self, text, images=()):
        """Blocking. Runs one user turn to completion; turns started from other threads wait for it."""
        with self._turn_lock:
            return self._run(text, images)

    def _run(self, text, images=()):
        pr = self.preset()
        m = self.model()
        if m and not self.session.get("model") and not pr.get("model"):
            self.session["model"] = m["name"]  # pinned: a model switched in another chat or terminal does not reach this one
        effort = self.session.get("effort") or pr.get("effort")
        if m and effort:
            m = dict(m, effort="" if effort == "none" else effort)
        if not m:
            self.emit({"type": "error", "text": "no model configured — use /provider then /models"})
            return
        self.busy = True
        self.cancel.clear()
        self.snap = {}
        start_idx = len(self.session["messages"])
        self._turn_gen = self.session.get("compactions", 0)
        content = text if not images else [{"type": "text", "text": text}] + [{"type": "image_url", "image_url": {"url": u}} for u in images]
        self.session["messages"].append({"role": "user", "content": content})
        if not self.session["title"]:
            self.session["title"] = text.strip().split("\n")[0][:60]
        self._trim_history(start_idx)
        t0, tin, tout, tcached, steps, cost = time.time(), 0, 0, 0, 0, 0.0
        wrote, ran, nudged = False, False, False
        todo_used, todo_idle, todo_nudged = False, 0, False
        squeezed, cutoffs, empties, seen = False, 0, 0, []
        self.emit({"type": "turn_start", "agent": pr["name"], "context": self.context_files()})
        prov = Provider(m, self.s.provider(m))
        st = self.s.config["settings"]
        try:
            while True:
                limit = self.compact_threshold(m)
                if limit and self.ctx_used > limit:
                    self.compact(reason="auto", model=m)
                schemas = self.tool_schemas()
                self._tool_names = {t["name"] for t in schemas}
                system = self.system_prompt()
                try:
                    msg, usage = prov.stream(system, self.session["messages"], schemas, self.emit, self.cancel, cache_key=self.session["id"])
                except ProviderError as e:
                    # the history outgrew the model: summarize the older part once and try again
                    if e.overflow and not squeezed and self.compact(reason="context full", model=m, keep_frac=0.15):
                        squeezed = True
                        continue
                    if not e.retryable and IMAGE_RX.search(str(e)) and has_images(self.session["messages"]):
                        # the model does not take images: keep the request, drop the pictures, tell the user why
                        self.session["messages"] = no_images(self.session["messages"], "[image removed: this model rejected images]")
                        self.emit({"type": "notice", "text": f"{m['name']} rejected the image ({str(e)[:160]}) — sent without it. "
                                                            "Pick a model with vision for images (/model), or set vision off in /models."})
                        continue
                    raise
                if not usage["in"]:  # provider sent no usage (some proxies drop stream_options): estimate, ~4 chars per token
                    usage = dict(usage, est=True, **{"in": len(system + json.dumps(self.session["messages"], ensure_ascii=False)
                                                                    + json.dumps(schemas)) // 4,
                                                     "out": usage["out"] or len(json.dumps(msg, ensure_ascii=False)) // 4})
                c = usage_cost(m, usage)
                tin += usage["in"]
                tout += usage["out"]
                tcached += min(usage.get("cached", 0), usage["in"])
                steps += 1
                cost += c
                self.ctx_used = usage["in"] + usage["out"] or self.ctx_used
                self.session["ctx"] = self.ctx_used
                u = self.session["usage"]
                u["in"] = u.get("in", 0) + usage["in"]
                u["out"] = u.get("out", 0) + usage["out"]
                u["cached"] = u.get("cached", 0) + usage.get("cached", 0)
                u["cost"] = round(u.get("cost", 0) + c, 6)
                today = add_day_cost(self.s.home, c) if c and self.persist else day_cost(self.s.home)
                self.emit({"type": "usage", "ctx": self.ctx_used, "in": tin, "out": tout, "cached": tcached, "steps": steps, "est": bool(usage.get("est")),
                           "cost": cost, "session_cost": u["cost"], "day_cost": today})
                cut = msg.get("_finish") == "length"
                if not msg.get("content") and not msg.get("tool_calls"):  # nothing to keep: an empty assistant message breaks the history
                    if cut and cutoffs < 2:  # the whole output limit went into reasoning
                        cutoffs += 1
                        self.session["messages"].append({"role": "user", "_aux": True, "content": THINK_NUDGE})
                        continue
                    if empties < 1 and not cut:  # an empty reply (no text, no tool call): ask once more
                        empties += 1
                        self.emit({"type": "retry", "attempt": 1, "of": 1, "delay": 0, "error": "empty reply", "discard": True})
                        continue
                    raise ProviderError("the model ran out of output tokens before answering — raise max_out or lower effort in /models" if cut
                                        else "the model returned an empty reply — try again, or pick another model (/model)")
                self.session["messages"].append(msg)
                self.checkpoint()
                if not msg.get("tool_calls"):
                    if cut and cutoffs < 2:  # the reply hit the output limit: let it carry on where it stopped
                        cutoffs += 1
                        self.session["messages"].append({"role": "user", "_aux": True, "content": CUTOFF_NUDGE})
                        continue
                    open_steps = [i for i in self.session.get("todo") or [] if i["s"] != "done"]
                    if todo_used and open_steps and not todo_nudged and self.depth == 0:
                        todo_nudged = True  # the plan says work is left: finish it, or at least leave the checklist true
                        self.session["messages"].append({"role": "user", "_aux": True, "content": (
                            "[self-check] Your checklist still has open steps:\n" + todo_lines(self.session["todo"]) +
                            "\nMark finished ones done (todo done=[…]) and do the rest. If you stopped on purpose (blocked, or waiting "
                            "for the user's go), keep them open and say so.")})
                        continue
                    if wrote and not ran and not nudged and "bash" in self._tool_names and behavior(st)["tests"]:
                        nudged = True  # self-check before "done": changes were made but nothing was run since
                        self.emit({"type": "verify"})
                        self.session["messages"].append({"role": "user", "_aux": True, "content": VERIFY_NUDGE})
                        continue
                    break
                self._limits(cost, st)
                results = self._run_calls(msg["tool_calls"], pr, cut)
                imgs = []
                for tc, (ok, out, img) in results:
                    name = tc["function"]["name"]
                    if ok and name in ("write", "edit") and not re.search(DOC_EXT, str((tc_args(tc) or {}).get("path", "")), re.I):
                        wrote, ran = True, False  # docs/config prose needs no test run: nudging there only burned a model call
                    elif name == "bash" or name == "task":
                        ran = ran or wrote
                    if img:
                        imgs.append(img)
                if self.cancel.is_set():
                    raise Cancelled()
                if any(tc["function"]["name"] == "todo" for tc, _ in results):
                    todo_used, todo_idle = True, 0
                elif any(i["s"] != "done" for i in self.session.get("todo") or []):
                    todo_idle += 1
                    if todo_idle >= TODO_EVERY and "todo" in self._tool_names:
                        todo_idle = 0  # models forget the checklist mid-task: show it again with what to do
                        self.session["messages"].append({"role": "user", "_aux": True, "content": (
                            "[reminder] Your checklist:\n" + todo_lines(self.session["todo"]) +
                            "\nIf a step is finished, mark it now: todo(done=[n], doing=next). A list left from an earlier task: replace it.")})
                sig = json.dumps([(tc["function"]["name"], tc["function"]["arguments"], r[1][:2000]) for tc, r in results])
                seen = (seen + [sig])[-6:]
                if seen.count(sig) >= 3:  # the same calls with the same results three times (also A-B-A-B-A): a loop
                    seen = []
                    self.session["messages"].append({"role": "user", "_aux": True, "content": LOOP_NUDGE})
                if imgs and m.get("vision", True):
                    self.session["messages"].append({"role": "user", "_aux": True, "content": [{"type": "text", "text": "(images from read)"}] +
                                                     [{"type": "image_url", "image_url": {"url": u}} for u in imgs]})
                queued = self.take_queued() if self.depth == 0 else []
                if queued:  # the user wrote while tools ran: hand it to the model now instead of after the whole turn
                    self.emit({"type": "queued_sent", "texts": queued})
                    self.session["messages"].append({"role": "user", "_aux": True,
                                                     "content": "[message from the user while you were working — take it into account]\n"
                                                     + "\n\n".join(queued)})
        except Cancelled:
            self._fix_dangling()
            self.session["messages"].append({"role": "user", "_aux": True, "content": "[interrupted by user]"})
            self.emit({"type": "error", "text": "interrupted"})
        except LimitHit as e:
            self._fix_dangling()
            self.emit({"type": "error", "text": str(e)})
        except Exception as e:
            self._fix_dangling()
            self.emit({"type": "error", "text": str(e)})
        finally:
            self.busy = False
            todo = self.session.get("todo") or []
            if todo and all(i["s"] == "done" for i in todo):
                self.session["todo"] = []  # finished list: don't carry it into the next request
            self._finish_checkpoint(start_idx)
            if pr.get("name") == "me" and self.persist:
                self.s.save()  # new skills/themes/agents written to the home folder reach every device
            if self.persist:
                self.s.save_session(self.session)
            self.emit({"type": "turn_end", "time": time.time() - t0, "in": tin, "out": tout, "cached": tcached, "steps": steps,
                       "ctx": self.ctx_used, "cost": cost,
                       "session_cost": self.session["usage"].get("cost", 0)})

    TRIM_AT, TRIM_KEEP = 4000, 1500

    def _trim_history(self, upto):
        """Before a new user turn: shrink bulky tool results and drop image payloads of earlier turns.
        Every step re-sends the whole history, so a 40 KB file read once was paid for again on every later step. The model can
        re-read anything it still needs. Only messages before `upto` change, once, so the cached prompt prefix stays stable after."""
        for m in self.session["messages"][:upto]:
            c = m.get("content")
            if m["role"] == "tool" and isinstance(c, str) and len(c) > self.TRIM_AT and not c.startswith("[trimmed"):
                k = self.TRIM_KEEP
                m["content"] = f"[trimmed old output: {len(c)} chars; read again if needed]\n{c[:k]}\n…\n{c[-k // 3:]}"
            elif isinstance(c, list) and any(x.get("type") == "image_url" for x in c):
                m["content"] = [x if x.get("type") != "image_url" else {"type": "text", "text": "[image shown earlier]"} for x in c]

    def _limits(self, cost, st):
        mt, md = float(st.get("max_turn_cost") or 0), float(st.get("max_day_cost") or 0)
        if mt and cost >= mt:
            raise LimitHit(f"stopped: this task reached the {money(mt)} per-task limit (/config to change)")
        if md and day_cost(self.s.home) >= md:
            raise LimitHit(f"stopped: today's spending reached the {money(md)} daily limit (/config to change)")

    def _run_calls(self, calls, pr, cut=False):
        """Independent read-only calls run in parallel; anything that changes state runs in order."""
        prepared = []
        for tc in calls:
            name = tc["function"]["name"]
            args = parse_args(tc["function"]["arguments"])
            if args is None and cut:
                args = TRUNCATED  # cut by the output limit mid-call: say so, or the model retries the same way
            if isinstance(args, dict):
                args = fix_args(name, args, None)[0]
            ui = args if isinstance(args, dict) else {}
            tid = tc["id"]
            ev = {"type": "tool_start", "id": tid, "name": name, "args": ui, "label": self.tools.preview(name, ui, self.cwd)}
            cli = (self.preset().get("cli") or {}).get(name)
            if cli:
                pr = self.preset()
                ev["cli"] = {"agent": pr["name"], "label": cli["label"], "color": pr.get("color", ""), "icon": pr.get("icon", "")}
            if name == "skill" and ui:
                sk = self.skills.all().get(ui.get("name"), {})
                ev["skill"] = {"color": sk.get("color", ""), "icon": sk.get("icon", "")}
            prepared.append((tc, name, args, tid, ev))
        results = {}
        par = len(prepared) > 1 and all(n in READONLY_TOOLS and isinstance(a, dict) for _, n, a, _, _ in prepared)
        if par:
            for p in prepared:
                self.emit(p[4])
            ths = []
            for tc, name, args, tid, _ in prepared:
                def work(tc=tc, name=name, args=args, tid=tid):
                    t1 = time.time()
                    try:
                        results[tid] = self._exec(name, args, tid, pr) + (time.time() - t1,)
                    except Exception as e:  # incl. Cancelled: every call still gets a result
                        results[tid] = (False, "cancelled" if isinstance(e, Cancelled) else f"error: {type(e).__name__}: {e}", None, time.time() - t1)
                th = threading.Thread(target=work, daemon=True)
                th.start()
                ths.append(th)
            for th in ths:
                th.join()
        out = []
        for tc, name, args, tid, ev in prepared:
            if not par:
                if self.cancel.is_set():
                    results[tid] = (False, "cancelled", None, 0)
                else:
                    self.emit(ev)
                    t1 = time.time()
                    results[tid] = self._exec(name, args, tid, pr) + (time.time() - t1,)
            ok, res, img, dt = results[tid]
            self.session["messages"].append({"role": "tool", "tool_call_id": tid, "content": res})
            self.emit({"type": "tool_end", "id": tid, "ok": ok, "output": res, "time": dt})
            self.checkpoint()
            out.append((tc, (ok, res, img)))
        return out

    def _exec(self, name, args, tid, pr):
        """Checks + runs one tool. Returns (ok, output, image)."""
        temit = lambda e, _id=tid: self.emit({"type": "tool_event", "id": _id, **e})
        if args is TRUNCATED:
            return False, ("error: your reply hit the output token limit, so these arguments were cut off and nothing ran. Send less per "
                           "call: write a big file in parts (write the first part, then add the rest with edit), or raise max_out in /models."), None
        if not isinstance(args, dict):
            return False, "error: arguments were not valid JSON — send a JSON object", None
        args, err = fix_args(name, args, next((t for t in self.tool_schemas() if t["name"] == name), None))
        if err:
            return False, err, None
        if name in ("write", "edit", "read") and not isinstance(args.get("path"), str):
            return False, "error: path must be a string", None
        if name not in (getattr(self, "_tool_names", None) or {t["name"] for t in self.tool_schemas()}):
            return False, f"error: tool {name} is not available in the {pr['name']} agent", None
        if name in ("write", "edit") and not may_write(pr, relp(self.abspath(args.get("path", "")), self.cwd)):
            return False, f"error: the {pr['name']} agent may only write {pr.get('writes') or 'nothing'}", None
        veto = PLUGINS.emit("before_tool", name, args)
        if isinstance(veto, str):
            return False, veto, None
        if name == "skill" and args.get("name") == pr["name"] and args["name"] not in self.skills.enabled():
            return False, f"error: {pr['name']} is the agent mode you run in, not a skill; its guide is already in your instructions. Load one of the skills listed there.", None
        if name == "bash" and self._no_checks(args.get("cmd")):
            return False, NO_CHECKS, None
        if name == "todo":
            return self._todo(args)
        if name == "memory":
            return self._memory(args)
        if name == "task":
            return self._task(args, temit)
        cli = (pr.get("cli") or {}).get(name)
        if cli:
            return self._cli(cli, args, temit)
        need = self.tools.needs_confirm(name, dict(args, path=self.abspath(args["path"])) if isinstance(args.get("path"), str) else args, self.cwd)
        if isinstance(need, str):
            return False, need, None
        if need:
            ans = self.confirm(name, args)
            if not ans:
                return False, "user denied this action. ask what to do instead.", None
            if ans == "always" and name == "bash":
                st = self.s.config["settings"]
                st["allow"] = list(st.get("allow") or DEFAULT_ALLOW) + [allow_pattern(args.get("cmd", ""))]
                self.s.save()
        args = self.remap(args)
        if name in ("write", "edit"):
            self._snapshot(self.abspath(args.get("path", "")))
        try:
            return self.tools.run(name, args, temit, self.cancel, self.ask, cwd=self.cwd)
        except Cancelled:
            raise
        except Exception as e:
            return False, f"error: {type(e).__name__}: {e}", None

    def _no_checks(self, cmd):
        return not behavior(self.s.config["settings"])["tests"] and bool(CHECK_RX.search(str(cmd or "")))

    def _cli(self, spec, args, temit):
        """An agent's own CLI tool: fill the template, stream the output as status lines, report exit code."""
        cmd, err = render_cli(spec, args)
        if err:
            return False, err, None
        if self._no_checks(cmd):
            return False, NO_CHECKS, None
        need = self.tools.needs_confirm("bash", {"cmd": cmd}, self.cwd)
        if isinstance(need, str):
            return False, need, None
        if need and not self.confirm("bash", {"cmd": cmd}):
            return False, "user denied this action. ask what to do instead.", None
        temit({"t": "status", "text": "$ " + cmd[:120]})
        n = [0]

        last = [0.0]

        def line(l):
            n[0] += 1
            if time.time() - last[0] > 0.1:  # throttle: chatty commands must not flood the event log
                last[0] = time.time()
                temit({"t": "status", "text": l.strip()[:160]})
                temit({"t": "progress", "v": None, "label": f"{n[0]} lines", "n": n[0]})
        ok, out = run_shell(cmd, int(args.get("timeout") or 300), self.cancel, shell_env(self.s.config.get("secrets")), on_line=line,
                            cwd=self.cwd, pref=self.s.config["settings"].get("shell"))
        return ok, out or "(no output)", None

    def _memory(self, a):
        try:
            if a.get("forget"):
                mem_forget(self.s.home, a["forget"])
                self.s.save()
                return True, f"forgot note {a['forget']}", None
            if not str(a.get("text", "")).strip():
                return False, "error: give text (to add) or forget (an id)", None
            mid = mem_add(self.s.home, a["text"], source=self.session.get("title", ""))
            self.s.save()
            return True, f"remembered ({mid}) — it will be in every future chat", None
        except ValueError as e:
            return False, f"error: {e}", None

    # ─── todo / goal ───
    def _todo(self, a):
        if not a.get("items") and (a.get("done") or a.get("doing")):  # mark steps by number, no need to resend the list
            items = [dict(i) for i in self.session.get("todo") or []]
            if not items:
                return False, "error: no checklist yet — set it with items=[…]", None
            try:
                done = {int(x) for x in (a.get("done") or [])} if isinstance(a.get("done"), list) else {int(a["done"])}
                doing = int(a.get("doing") or 0)
            except (TypeError, ValueError):
                return False, "error: done is a list of step numbers (1-based), doing one number", None
            for n, i in enumerate(items, 1):
                if n in done:
                    i["s"] = "done"
                elif n == doing:
                    i["s"] = "doing"
                elif i["s"] == "doing" and doing:
                    i["s"] = "todo"
            a = {"items": [{"t": i["t"], "s": i["s"]} for i in items]}
        items = []
        for i in a.get("items") or []:
            if isinstance(i, dict):  # tolerate {t, s} / {text, done}
                t, st = str(i.get("t") or i.get("text") or ""), i.get("s") or ("done" if i.get("done") else "todo")
            else:
                t = str(i).strip()
                m = re.match(r"^(?:\[(x|X|>| )\]|(x|X|>|✓|-))\s+", t)
                mark = (m.group(1) or m.group(2)) if m else ""
                t, st = (t[m.end():] if m else t), {"x": "done", "X": "done", "✓": "done", ">": "doing"}.get(mark, "todo")
            if t.strip():
                items.append({"t": t.strip()[:200], "s": st if st in ("todo", "doing", "done") else "todo"})
        self.session["todo"] = items[:50]
        self.emit({"type": "todo", "items": self.session["todo"]})
        n = sum(1 for i in items if i["s"] == "done")
        nxt = next((i["t"] for i in items if i["s"] != "done"), None)
        return True, (f"todo: {n}/{len(items)} done" + (f"; next: {nxt} — mark each step done right after finishing it" if nxt
                                                          else "; all done — verify, then report")), None

    def goal(self, objective, max_rounds=None):
        """/goal: work autonomously until the objective is verifiably met or blocked. Binary result."""
        max_rounds = int(max_rounds or self.s.config["settings"].get("goal_rounds") or 12)
        g = self.session["goal"] = {"text": objective, "status": "working", "reason": "", "round": 0}
        self.emit({"type": "goal", **g})
        prompt = GOAL_PROMPT.format(objective=objective)
        for r in range(1, max_rounds + 1):
            g["round"] = r
            self.run(prompt)
            last = next((m for m in reversed(self.session["messages"]) if m["role"] == "assistant"), {})
            verdict = re.search(r"GOAL:\s*(DONE|BLOCKED)\b[:\s-]*(.*)", last.get("content") or "", re.S | re.I)
            if self.cancel.is_set() or (self.session["messages"] and self.session["messages"][-1].get("content") == "[interrupted by user]"):
                g.update(status="blocked", reason="interrupted by user")
                break
            if verdict:
                g.update(status=verdict.group(1).lower(), reason=verdict.group(2).strip()[:600])
                break
            prompt = ("Continue toward the goal. Check your todo list, do the next step, verify. When the success criteria pass, "
                      "reply GOAL: DONE <evidence>; if you cannot proceed, GOAL: BLOCKED <reason>.")
        else:
            g.update(status="blocked", reason=f"no verified result after {max_rounds} rounds")
        self.emit({"type": "goal", **g})
        if self.persist:
            self.s.save_session(self.session)
        return g

    # ─── subagents ───
    def _task(self, a, temit):
        name = a.get("agent") or "code"
        if name not in presets(self.s):
            return False, f"error: no agent '{name}'. agents: {', '.join(presets(self.s))}", None
        report = {"text": ""}

        def sub_emit(e):
            t = e["type"]
            if t == "tool_start":
                temit({"t": "status", "text": f"{name} › {e['name']} {e.get('label', '')[:60]}"})
                temit({"t": "log", "text": f"{e['name']} {e.get('label', '')[:80]}"})
            elif t == "text":
                report["text"] += e["text"]
            elif t == "tool_end":
                report["text"] = ""
            elif t == "todo":
                done = sum(1 for i in e["items"] if i["s"] == "done")
                temit({"t": "progress", "v": done / max(1, len(e["items"])), "label": f"{done}/{len(e['items'])}"})
            elif t == "error":
                temit({"t": "log", "text": "✗ " + e["text"]})
        sub = Agent(self.s, sub_emit, self.confirm, self.ask)
        sub.depth, sub.persist = self.depth + 1, False
        sub.cancel = self.cancel
        sub.session = new_session(self.session.get("cwd"))
        sub.session["agent"] = name
        sub.session["roots"] = list(self.session.get("roots") or [])
        temit({"t": "status", "text": f"{name} agent started"})
        sub.run(a.get("prompt", "") + "\n\nYou are a subagent: do the job, verify it, then reply with a short report "
                                      "(what you did, files touched, results). The report is all the caller sees.")
        for k in ("in", "out", "cost"):
            self.session["usage"][k] = self.session["usage"].get(k, 0) + sub.session["usage"].get(k, 0)
        self.snap.update({k: v for k, v in sub.snap.items() if k not in self.snap})
        last = next((m.get("content") for m in reversed(sub.session["messages"]) if m["role"] == "assistant" and m.get("content")), "")
        return bool(last), clip(last or report["text"] or "(subagent returned nothing)", 6000), None

    # ─── checkpoints / undo ───
    def _snapshot(self, path):
        p = self.abspath(path)
        if p not in self.snap:
            try:
                self.snap[p] = snapshot_file(p)
            except OSError:
                pass

    def _finish_checkpoint(self, start_idx):
        if not self.snap or not self.persist:
            return
        files = dict(self.snap)
        gen = self.session.get("compactions", 0)
        # history was compacted during this turn: message indexes moved, so undo restores files but keeps the history
        msgs = start_idx if gen == getattr(self, "_turn_gen", gen) else -1
        Checkpoints(self.s, self.session["id"]).push({"msgs": msgs, "gen": gen, "files": files, "t": time.time()})
        diffs = [{"path": relp(p, self.cwd), "diff": file_diff(p, old), "new": old is None}
                 for p, old in files.items()]
        self.emit({"type": "turn_diff", "files": [d for d in diffs if d["diff"]]})

    def undo(self):
        """Restore the files the last turn changed and drop that turn from history."""
        if self.busy:
            return {"ok": False, "text": "busy"}
        cp = Checkpoints(self.s, self.session["id"]).pop()
        if not cp:
            return {"ok": False, "text": "nothing to undo"}
        restore_files(cp["files"])
        if cp.get("gen", 0) == self.session.get("compactions", 0) and 0 <= cp["msgs"] <= len(self.session["messages"]):
            self.session["messages"] = self.session["messages"][:cp["msgs"]]
        self.s.save_session(self.session)
        names = [relp(p, self.cwd) for p in cp["files"]]
        ev = {"type": "undone", "files": names, "text": f"restored {len(names)} file{'s' if len(names) != 1 else ''}: " + ", ".join(names[:8])}
        self.emit(ev)
        return dict(ev, ok=True)

    def retry(self):
        """Take back the last request (its file changes and its part of the history) and return its text to run again."""
        if self.busy:
            return None, "busy"
        msgs = self.session["messages"]
        i = max((i for i, m in enumerate(msgs) if m["role"] == "user" and not m.get("_aux") and not m.get("_shell")), default=-1)
        if i < 0:
            return None, "nothing to retry"
        c = msgs[i]["content"]
        text = c if isinstance(c, str) else " ".join(x.get("text", "") for x in c if x.get("type") == "text")
        cps = Checkpoints(self.s, self.session["id"]).load()
        if cps and cps[-1].get("msgs") == i and cps[-1].get("gen", 0) == self.session.get("compactions", 0):
            self.undo()  # the turn changed files: restore them too
        self.session["messages"] = self.session["messages"][:i]
        self.s.save_session(self.session)
        return text, ""

    def context_report(self):
        """What the next request sends, in tokens (about 4 chars each): prompt, tools, history by kind, the biggest messages."""
        tok = lambda x: len(x if isinstance(x, str) else json.dumps(x, ensure_ascii=False)) // 4
        sysp, tools, msgs = self.system_prompt(), self.tool_schemas(), self.session["messages"]
        kinds = collections.Counter()
        for m in msgs:
            kinds["tool results" if m["role"] == "tool" else "your messages" if m["role"] == "user" and not m.get("_aux")
                  else "notes from me" if m["role"] == "user" else "answers + tool calls"] += tok(m)
        total = tok(sysp) + tok(tools) + sum(kinds.values())
        m = self.model() or {}
        ctx = int(m.get("context") or 0)
        rows = [("system prompt", tok(sysp)), (f"tools ({len(tools)})", tok(tools))] + kinds.most_common()
        out = [f"## Context · ~{human(total)} tokens" + (f" of {human(ctx)} ({total * 100 // ctx}%)" if ctx else "")]
        out += [f"- {k}: ~{human(v)}" for k, v in rows]
        big = sorted(((tok(x), i, x) for i, x in enumerate(msgs)), key=lambda t: -t[0])[:5]
        if big and big[0][0] > 500:
            out.append("\nBiggest messages:")
            for n, i, x in big:
                c = x.get("content")
                c = c if isinstance(c, str) else json.dumps(x.get("tool_calls") or c, ensure_ascii=False)
                out.append(f"- #{i} {x['role']}: ~{human(n)} — {(c or '').strip().splitlines()[0][:80] if (c or '').strip() else ''}")
        out.append("\n/compact summarizes old history; old tool output is trimmed automatically before each new request.")
        return "\n".join(out)

    def chat_diff(self):
        """[(path, unified diff)] for every file this chat changed: its first snapshot (checkpoints) against the file now."""
        first = {}
        for cp in Checkpoints(self.s, self.session["id"]).load():
            for p, old in (cp.get("files") or {}).items():
                first.setdefault(p, old)
        out = []
        for p, old in first.items():
            d = file_diff(p, old)
            if d:
                out.append((relp(p, self.cwd), d))
        return out

    def export_md(self):
        """The chat as Markdown: requests, answers, and one line per tool call."""
        out = [f"# {self.session.get('title') or 'chat'}\n"]
        for m in self.session["messages"]:
            c = m.get("content")
            c = c if isinstance(c, str) else " ".join(x.get("text", "") for x in c or [] if x.get("type") == "text")
            if m["role"] == "user" and not m.get("_aux"):
                out.append("## You\n\n" + c + "\n")
            elif m["role"] == "assistant":
                if c:
                    out.append("## me\n\n" + c + "\n")
                for tc in m.get("tool_calls") or []:
                    a = parse_args(tc["function"]["arguments"]) or {}
                    out.append(f"- `{tc['function']['name']}` {self.tools.preview(tc['function']['name'], a, self.cwd)}")
        return "\n".join(out) + "\n"

    def requests(self):
        """[(index, text)] of what the user asked in this chat, oldest first: the points /rewind can go back to."""
        out = []
        for i, m in enumerate(self.session["messages"]):
            c = m.get("content")
            c = c if isinstance(c, str) else " ".join(x.get("text", "") for x in c or [] if x.get("type") == "text")
            if m["role"] == "user" and not m.get("_aux") and not m.get("_shell") and not c.startswith("[Summary"):
                out.append((i, c))
        return out

    def rewind(self, i):
        """Go back to just before request #i: it and everything after leave the history, files those turns changed are restored."""
        if self.busy:
            return None, "busy"
        msgs = self.session["messages"]
        if not (0 <= i < len(msgs)) or msgs[i]["role"] != "user":
            return None, "no such request"
        cps, gen, files = Checkpoints(self.s, self.session["id"]), self.session.get("compactions", 0), []
        while True:
            last = (cps.load() or [None])[-1]
            if not last or last.get("gen", 0) != gen or last.get("msgs", -1) < i:
                break
            restore_files(cps.pop()["files"])
            files += [relp(p, self.cwd) for p in last["files"]]
        c = msgs[i]["content"]
        self.session["messages"] = msgs[:i]
        self.s.save_session(self.session)
        return (c if isinstance(c, str) else " ".join(x.get("text", "") for x in c if x.get("type") == "text")), sorted(set(files))

    def fork(self):
        """Continue in a copy of this chat; the original stays as it is (try another approach without losing this one)."""
        old = self.session
        new = new_session(old.get("cwd"))
        new.update(json.loads(json.dumps({k: old[k] for k in ("messages", "model", "todo", "roots", "ws", "compactions", "ctx") if k in old})))
        new.update(title=((old.get("title") or "chat")[:70] + " · fork"), forked_from=old["id"])
        cps = Checkpoints(self.s, old["id"]).load()
        if cps:  # /undo and /rewind work in the copy too
            jsave(Checkpoints(self.s, new["id"]).p, cps)
        self.session, self.snap = new, {}
        self.s.save_session(new)
        return new

    BTW_PROMPT = "Answer the user's side question about this coding session briefly and directly. You cannot run tools; answer from the transcript."

    def side_question(self, q):
        """/btw: a quick answer from the chat so far, without tools; neither the question nor the answer enters the history."""
        m = self.model()
        if not m:
            return "no model — add one with /models"
        lines = []
        for x in self.session["messages"][-60:]:
            c = x.get("content")
            c = c if isinstance(c, str) else " ".join(y.get("text", "") for y in c or [] if y.get("type") == "text")
            if c:
                lines.append(f"[{'tool result' if x['role'] == 'tool' else x['role']}] {c[:1500 if x['role'] == 'tool' else 4000]}")
            lines += [f"[tool call] {tc['function']['name']} {tc['function']['arguments'][:200]}" for tc in x.get("tool_calls") or []]
        body = "Transcript:\n" + "\n".join(lines)[-60000:] + "\n\nSide question: " + q
        msg, usage = Provider(m, self.s.provider(m)).stream(self.BTW_PROMPT, [{"role": "user", "content": body}], None, lambda e: None, self.cancel)
        c = usage_cost(m, usage)
        u = self.session["usage"]
        u["cost"] = round(u.get("cost", 0) + c, 6)
        if c and self.persist:
            add_day_cost(self.s.home, c)
        return (msg.get("content") or "").strip() or "(no answer)"

    def shell(self, cmd):
        """`!cmd` typed by the user: run it in the chat's folder and put command + output in the history, so the agent sees it."""
        ok, out, _ = self.tools.run("bash", {"cmd": cmd}, lambda e: None, threading.Event(), cwd=self.cwd)
        self.session["messages"].append({"role": "user", "_shell": True,
                                         "content": f"[I ran a command myself]\n$ {cmd}\n{clip(out, 8000)}"})
        if self.persist:
            self.s.save_session(self.session)
        return ok, out

    def _fix_dangling(self):
        """Every tool_call must have a tool result, or APIs reject the history."""
        msgs = self.session["messages"]
        for i in range(len(msgs) - 1, -1, -1):
            if msgs[i]["role"] == "assistant":
                have = {m.get("tool_call_id") for m in msgs[i + 1:] if m["role"] == "tool"}
                for tc in msgs[i].get("tool_calls") or []:
                    if tc["id"] not in have:
                        msgs.append({"role": "tool", "tool_call_id": tc["id"], "content": "cancelled"})
                break

    COMPACT_PROMPT = ("Summarize this coding session so work can continue without it. Keep: the user's goals and preferences, "
                      "decisions made, files created/changed (paths), commands that matter, current state, open problems, next steps. "
                      "Dense bullet points, no filler.")

    def compact_threshold(self, m):
        """Tokens at which history is compacted: settings.autocompact % of the model context (default 80%), never above 1M. 0 = off."""
        pct = self.s.config["settings"].get("autocompact", 80)
        if not pct:
            return 0
        ctx = int(m.get("context") or 128000)
        return min(int(ctx * float(pct) / 100), 1_000_000)

    def compact(self, reason="manual", model=None, keep_frac=0.25):
        """Replace older history with a model-written summary. The most recent messages (about `keep_frac` of the model's
        context) stay verbatim. The cut may fall inside one long turn (an assistant step and its tool results stay together),
        so a single big task can be compacted too."""
        msgs = self.session["messages"]
        m = model or self.model()
        if not m or len(msgs) < 3:
            return False
        size = lambda x: len(json.dumps(x, ensure_ascii=False)) // 4
        ctx = int(m.get("context") or 128000)
        budget = max(2000, min(int(ctx * keep_frac), sum(size(x) for x in msgs) // 3))
        acc, cut, last_ok = 0, None, None
        for i in range(len(msgs) - 1, 1, -1):
            acc += size(msgs[i])
            ok = msgs[i]["role"] in ("user", "assistant")  # never cut between a tool call and its result
            if ok and last_ok is None:
                last_ok = i
            if acc > budget:
                break
            if ok:
                cut = i
        cut = cut or last_ok
        if not cut or cut < 2:
            return False
        lines = []
        for x in msgs[:cut]:
            c = x.get("content")
            if isinstance(c, list):
                c = " ".join(y.get("text", "") for y in c if y.get("type") == "text")
            if x["role"] == "tool":
                lines.append(f"[tool result] {str(c)[:1200]}")
            else:
                if c:
                    lines.append(f"[{x['role']}] {str(c)[:4000]}")
                for tc in x.get("tool_calls") or []:
                    lines.append(f"[tool call] {tc['function']['name']} {tc['function']['arguments'][:300]}")
        self.emit({"type": "compacting", "reason": reason})
        msg, usage = Provider(m, self.s.provider(m)).stream(self.COMPACT_PROMPT, [{"role": "user", "content": "\n".join(lines)[-min(400000, ctx * 2):]}],
                                                            None, lambda e: None, self.cancel)
        summary = (msg.get("content") or "").strip()
        if not summary:
            return False
        real = [i for i, x in enumerate(msgs) if x["role"] == "user" and not x.get("_aux") and isinstance(x.get("content"), str)
                and not x["content"].startswith("[Summary")]
        if real and real[-1] < cut:  # the cut went through the current task: keep its request word for word
            summary += "\n\nThe request being worked on now (verbatim):\n" + msgs[real[-1]]["content"][:6000]
        head = [{"role": "user", "content": "[Summary of the earlier conversation]\n" + summary}]
        if msgs[cut]["role"] == "user":
            head.append({"role": "assistant", "content": "Understood — continuing from this summary."})
        before = len(msgs)
        self.session["messages"] = head + msgs[cut:]
        self.session["compactions"] = self.session.get("compactions", 0) + 1
        self.ctx_used = self.session["ctx"] = len(json.dumps(self.session["messages"], ensure_ascii=False)) // 4
        self.emit({"type": "compacted", "before": before, "after": len(self.session["messages"]), "ctx": self.ctx_used,
                   "reason": reason, "summary": summary[:2000]})
        if self.persist:
            self.s.save_session(self.session)
        return True

    def checkpoint(self, every=1.0):
        """Save mid-turn (throttled) so other devices see each step live."""
        if self.persist and time.time() - getattr(self, "_saved", 0) >= every:
            self._saved = time.time()
            self.s.save_session(self.session)

    def refresh_from_disk(self, cwd=None):
        """Another device advanced this session: adopt the newer copy (only while idle). Returns True if reloaded."""
        if self.busy:
            return False
        d = jload(self.s.session_path(self.session["id"]))
        if not d or d.get("updated", 0) <= self.session.get("updated", 0) + 0.01:
            return False
        d.setdefault("usage", {"in": 0, "out": 0})
        self.session = d
        self.ctx_used = d.get("ctx", self.ctx_used)
        if cwd and os.path.abspath(cwd) != d.get("cwd"):
            self.relocate(cwd)
        return True

    def relocate(self, new_cwd):
        """Session moved to another machine/sandbox: map every earlier project root to the new one,
        so absolute paths remembered from history keep pointing into the project."""
        new_cwd = os.path.abspath(new_cwd)
        old = self.session.get("cwd")
        roots = self.session.setdefault("roots", [])
        if old and old != new_cwd and old not in roots:
            roots.append(old)
        self.session["roots"] = [r for r in roots if r != new_cwd]
        self.session["cwd"] = new_cwd

    def remap(self, args):
        roots = self.session.get("roots") or []
        if not roots:
            return args
        cwd = self.session["cwd"]
        out = {}
        for k, v in args.items():
            if k == "path" and isinstance(v, str):  # only the path itself: never rewrite file content or commands
                for r in sorted(roots, key=len, reverse=True):
                    if v == r or v.startswith(r.rstrip("/\\") + os.sep) or v.startswith(r.rstrip("/\\") + "/"):
                        v = cwd + v[len(r.rstrip("/\\")):]
                        break
            out[k] = v
        return out

    def load(self, sid):
        d = jload(self.s.session_path(sid))
        if d:
            self.session = d
            self.session.setdefault("usage", {"in": 0, "out": 0})
            self.ctx_used = self.session.get("ctx", 0)
        return d


# ───────────────────────────── headless / rpc ─────────────────────────────


def run_headless(store, prompt, yolo):
    def emit(e):
        t = e["type"]
        if t == "text":
            sys.stdout.write(e["text"])
            sys.stdout.flush()
        elif t == "tool_start":
            sys.stderr.write(f"\n· {e['name']} {e['label'][:100]}\n")
        elif t == "tool_end" and not e["ok"]:
            sys.stderr.write(f"  ✗ {e['output'][:200]}\n")
        elif t in ("error", "notice"):
            sys.stderr.write(f"\n{t}: {e['text']}\n")
        elif t == "retry":
            sys.stderr.write(f"\n↻ {e['error'][:160]} — retrying in {e['delay']:.0f}s\n")
        elif t == "turn_end":
            sys.stderr.write(f"\n· {dur(e['time'])} · ↑{human(e['in'])} ↓{human(e['out'])}\n")

    def confirm(name, a):
        if yolo:
            return True
        if not sys.stdin.isatty():
            return False
        sys.stderr.write(f"allow {name}: {json.dumps(a, ensure_ascii=False)[:200]} [y/N/a=always] ")
        r = input().strip().lower()
        return "always" if r.startswith("a") else r.startswith("y")

    def ask(q, options=None):
        if not sys.stdin.isatty():
            return ""
        return input(q + (f" [{' / '.join(options)}]" if options else "") + " ")
    ag = Agent(store, emit, confirm, ask)
    if prompt.startswith("/goal "):
        g = ag.goal(prompt[6:].strip().strip('"'))
        sys.stderr.write(f"\ngoal {g['status']}: {g['reason']}\n")
        print()
        return 0 if g["status"] == "done" else 2
    ag.run(prompt)
    print()


def run_rpc(store):
    """JSON-lines over stdio. in: {type:prompt,text} {type:cancel} {type:confirm,id,ok} {type:answer,id,text} {type:load,id}
    out: agent events + {type:confirm,id,name,args} {type:ask,id,text}"""
    out_lock = threading.Lock()
    waits = {}

    def emit(e):
        with out_lock:
            sys.stdout.write(json.dumps(e, ensure_ascii=False) + "\n")
            sys.stdout.flush()

    def wait(kind, **kw):
        rid = uuid.uuid4().hex[:8]
        ev = threading.Event()
        waits[rid] = [ev, None]
        emit(dict(type=kind, id=rid, **kw))
        ev.wait()
        return waits.pop(rid)[1]

    yolo = bool(os.environ.get("ME_YOLO"))  # cloud runner: sandboxed, so tools run without asking
    cloud = Cloud(store)

    def emit_sync(e):
        emit(e)
        if e["type"] == "turn_end" and cloud.online:
            threading.Thread(target=cloud.sync, daemon=True).start()

    ag = Agent(store, emit_sync, (lambda n, a: True) if yolo else (lambda n, a: (lambda r: "always" if r == "always" else bool(r))(wait("confirm", name=n, args=a))),
               lambda q, options=None: wait("ask", text=q, options=options) or "")
    if os.environ.get("ME_WS_ID"):
        ag.session["ws"] = os.environ["ME_WS_ID"]
    store.on_change = cloud.autosync  # every checkpoint reaches other devices within ~1s

    def remote(n):
        store.config.update(jload(store.cfg_path) or {})
        if ag.refresh_from_disk(os.getcwd()):
            emit({"type": "reload", "session": ag.session["id"]})
    if cloud.online:
        cloud.watch(remote)
    emit({"type": "ready", "version": VERSION, "session": ag.session["id"]})
    src = sys.stdin
    if os.environ.get("ME_RPC_IN"):  # cloud: a FIFO opened read-write never hits EOF, so a server restart does not end us
        src = io.TextIOWrapper(io.FileIO(os.open(os.environ["ME_RPC_IN"], os.O_RDWR), "r"), encoding="utf-8")
    for line in src:
        try:
            cmd = json.loads(line)
        except ValueError:
            continue
        t = cmd.get("type")
        if t == "prompt" and not ag.busy:
            threading.Thread(target=ag.run, args=(cmd["text"], cmd.get("images", ())), daemon=True).start()
        elif t == "goal" and not ag.busy:
            threading.Thread(target=ag.goal, args=(cmd["text"],), daemon=True).start()
        elif t == "retry":
            text, err = ag.retry()
            emit({"type": "error", "text": err} if err else {"type": "retried", "text": text})
        elif t == "undo":
            r = ag.undo()
            if not r["ok"]:
                emit({"type": "error", "text": r["text"]})
        elif t == "cancel":
            ag.cancel.set()
        elif t == "model" and cmd.get("name"):
            store.config = jload(store.cfg_path) or store.config
            ag.set_model(cmd["name"])
        elif t in ("confirm", "answer") and cmd.get("id") in waits:
            waits[cmd["id"]][1] = cmd.get("ok") if t == "confirm" else cmd.get("text")
            waits[cmd["id"]][0].set()
        elif t == "load":
            if not ag.load(cmd["id"]) and cloud.online:
                cloud.sync()
                ag.load(cmd["id"])
            ag.relocate(os.getcwd())
            if os.environ.get("ME_WS_ID"):
                ag.session["ws"] = os.environ["ME_WS_ID"]
            emit({"type": "loaded", "ok": True, "session": ag.session["id"], "title": ag.session.get("title", "")})
        elif t == "new":
            if ag.busy:
                emit({"type": "error", "text": "busy — stop the current turn first"})
                continue
            ag.session = new_session()
            ag.ctx_used, ag.snap = 0, {}
            if os.environ.get("ME_WS_ID"):
                ag.session["ws"] = os.environ["ME_WS_ID"]
            emit({"type": "loaded", "ok": True, "session": ag.session["id"]})
        elif t == "compact" and not ag.busy:
            threading.Thread(target=ag.compact, daemon=True).start()
        elif t == "sync":
            cloud.sync()
            store.config = jload(store.cfg_path) or store.config
            emit({"type": "synced"})


# ───────────────────────────── web ui server ─────────────────────────────

WEBUI = "<!doctype html>\n<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\">\n<meta name=\"viewport\" content=\"width=device-width,initial-scale=1,viewport-fit=cover\">\n<meta name=\"theme-color\" content=\"#0b0c10\">\n<title>me</title>\n<link rel=\"manifest\" href=\"/manifest.webmanifest\">\n<meta name=\"apple-mobile-web-app-capable\" content=\"yes\">\n<link rel=\"icon\" href=\"data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Crect width='32' height='32' rx='8' fill='%230b0c10'/%3E%3Crect x='9' y='9' width='14' height='14' rx='3.5' fill='%238b7cff'/%3E%3C/svg%3E\">\n<style>:root{--on-a:#fff;--bg:#0b0c10;--bg2:#111319;--bg3:#171a23;--line:#1f2230;--fg:#d8dae3;--dim:#7b8094;--faint:#3a3d4a;--a:#8b7cff;--b:#4fd1c5;--ok:#5ad18a;--err:#ff6b7a;--warn:#ffb454;--user:#e7c66b;\n  --mono:ui-monospace,\"JetBrains Mono\",SFMono-Regular,Menlo,Consolas,monospace;--sans:Inter,system-ui,-apple-system,\"Segoe UI\",Roboto,sans-serif}\n:root[data-theme=\"light\"]{--bg:#fafaf8;--bg2:#f2f2ef;--bg3:#e9e9e4;--line:#e2e2dc;--fg:#1d1e24;--dim:#62657a;--faint:#c4c5cc}\n@media (prefers-color-scheme: light){:root:not([data-theme=\"dark\"]){--bg:#fafaf8;--bg2:#f2f2ef;--bg3:#e9e9e4;--line:#e2e2dc;--fg:#1d1e24;--dim:#62657a;--faint:#c4c5cc}}\n*{box-sizing:border-box;margin:0;padding:0}\nhtml,body{height:100%;background:var(--bg);color:var(--fg);font:14.5px/1.6 var(--sans);-webkit-font-smoothing:antialiased}\nbutton{font:inherit;color:inherit;background:none;border:none;cursor:pointer}\nsvg{width:16px;height:16px;fill:none;stroke:currentColor;stroke-width:1.8;stroke-linecap:round;stroke-linejoin:round;flex-shrink:0}\n.app{display:grid;grid-template-columns:260px 1fr;height:100%}\naside{border-right:1px solid var(--line);display:flex;flex-direction:column;min-height:0;background:var(--bg)}\n.brand{display:flex;align-items:center;gap:10px;padding:16px;font:700 17px var(--mono)}\n.brand i{width:11px;height:11px;border-radius:3px;background:linear-gradient(135deg,var(--a),var(--b))}\n.brand small{font:500 11px var(--mono);color:var(--dim);margin-left:auto;padding:2px 7px;border:1px solid var(--line);border-radius:6px}\n.newbtn{margin:0 12px 10px;display:flex;align-items:center;gap:8px;padding:9px 12px;border:1px solid var(--line);border-radius:9px;color:var(--fg)}\n.newbtn:hover{background:var(--bg2)}\n.slist{flex:1;overflow:auto;padding:0 8px 12px}\n.slist .lbl{font:11px var(--mono);color:var(--dim);padding:10px 8px 6px;text-transform:uppercase;letter-spacing:.5px}\n.si{display:block;width:100%;text-align:left;padding:8px 10px;border-radius:8px;color:var(--dim);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-size:13.5px}\n.si:hover{background:var(--bg2);color:var(--fg)}.si.on{background:var(--bg3);color:var(--fg)}\n.side-foot{border-top:1px solid var(--line);padding:10px 12px;display:flex;gap:6px;align-items:center;font-size:12.5px;color:var(--dim)}\n.side-foot a{color:inherit;text-decoration:none;display:flex;align-items:center;gap:6px}\nmain{display:flex;flex-direction:column;min-width:0;min-height:0}\nheader{display:flex;align-items:center;gap:10px;padding:10px 16px;border-bottom:1px solid var(--line);min-height:52px}\nheader .title{font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;flex:1}\n.ib.menu{display:none}\n.pill{display:flex;align-items:center;gap:6px;font:12px var(--mono);color:var(--dim);border:1px solid var(--line);border-radius:8px;padding:5px 9px;white-space:nowrap}\n.pill:hover{color:var(--fg)}\n#chat{flex:1;overflow:auto;scroll-behavior:smooth}\n.inner{max-width:820px;margin:0 auto;padding:22px 18px 30px}\n.empty{height:100%;display:grid;place-items:center;text-align:center;color:var(--dim)}\n.empty h2{font:700 34px var(--mono);letter-spacing:-1.5px;color:var(--fg);margin-bottom:6px}\n.empty h2 span{background:linear-gradient(90deg,var(--a),var(--b));-webkit-background-clip:text;background-clip:text;color:transparent}\n.msg{margin:18px 0}\n.msg.user{display:flex;justify-content:flex-end}\n.msg.user .bub{background:var(--bg3);border-radius:14px 14px 4px 14px;padding:10px 14px;max-width:85%;white-space:pre-wrap;word-break:break-word}\n.md{word-break:break-word}\n.md p{margin:8px 0}.md ul,.md ol{margin:8px 0 8px 22px}.md li{margin:3px 0}\n.md h1,.md h2,.md h3{font-size:16px;margin:16px 0 6px;color:var(--a)}\n.md code{font:12.5px var(--mono);background:var(--bg2);border:1px solid var(--line);padding:1px 5px;border-radius:5px;color:var(--b)}\n.md pre{background:var(--bg2);border:1px solid var(--line);border-radius:10px;margin:10px 0;overflow:hidden}\n.md pre .bar{display:flex;justify-content:space-between;font:11px var(--mono);color:var(--dim);padding:6px 12px;border-bottom:1px solid var(--line)}\n.md pre code{display:block;background:none;border:none;padding:12px;overflow-x:auto;color:var(--fg);white-space:pre}\n.md a{color:var(--a)}.md blockquote{border-left:3px solid var(--faint);padding-left:12px;color:var(--dim)}\n.md table{border-collapse:collapse;margin:8px 0;font-size:13.5px}.md td,.md th{border:1px solid var(--line);padding:5px 9px}\n.think{font-size:13px;color:var(--dim);margin:6px 0}\n.think summary{cursor:pointer;list-style:none;display:flex;align-items:center;gap:6px;font-style:italic}\n.think pre{white-space:pre-wrap;font:12.5px/1.55 var(--sans);font-style:italic;margin:6px 0 0 22px;max-height:240px;overflow:auto}\n.tool{border:1px solid var(--line);border-radius:10px;margin:8px 0;overflow:hidden;background:var(--bg)}\n.tool .h{display:flex;align-items:center;gap:8px;padding:8px 12px;font:12.5px var(--mono);cursor:pointer}\n.tool .h b{font-weight:600}.tool .h .lab{color:var(--dim);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1}\n.tool .h .t{color:var(--faint)}\n.tool .body{border-top:1px solid var(--line);font:12px/1.55 var(--mono);padding:8px 12px;max-height:320px;overflow:auto;white-space:pre-wrap;color:var(--dim)}\n.tool.closed .body,.tool .body:empty{display:none}\n.tool .st{font:12px var(--mono);color:var(--dim);padding:0 12px 8px}\n.tool .log{font:12px var(--mono);color:var(--dim);padding:0 12px 8px}\n.add{color:var(--ok)}.del{color:var(--err)}\n.pbar{height:6px;border-radius:4px;background:var(--line);margin:0 12px 10px;overflow:hidden}\n.pbar i{display:block;height:100%;background:var(--sk,var(--a));transition:width .2s}\n.pbar.ind i{width:30%!important;animation:ind 1.1s ease-in-out infinite}\n@keyframes ind{0%{margin-left:-30%}100%{margin-left:100%}}\n.skill{border-color:color-mix(in srgb,var(--sk) 45%,var(--line))}\n.skill .h b{color:var(--sk)}\n.spin{width:14px;height:14px;border:2px solid var(--line);border-top-color:var(--a);border-radius:50%;animation:sp .8s linear infinite;flex-shrink:0}\n@keyframes sp{to{transform:rotate(360deg)}}\n.stats{font:12px var(--mono);color:var(--dim);display:flex;align-items:center;gap:6px;margin:4px 0 22px}\n.stats svg{color:var(--ok)}\n.info{font:12.5px var(--mono);color:var(--dim);margin:8px 0}.info.err{color:var(--err)}.info.warn{color:var(--warn)}\n.confirm{border:1px solid var(--warn);border-radius:10px;padding:12px;margin:10px 0}\n.confirm pre{font:12px var(--mono);white-space:pre-wrap;color:var(--dim);margin:8px 0;max-height:200px;overflow:auto}\n.confirm .btns{display:flex;gap:8px}\n.btn{padding:7px 13px;border-radius:8px;font:500 12.5px var(--mono);background:var(--a);color:var(--on-a)}\n.btn.ghost{background:none;color:var(--dim);border:1px solid var(--line)}\n.working{display:flex;align-items:center;gap:8px;font:12.5px var(--mono);color:var(--dim);margin:10px 0}\n.shimmer{background:linear-gradient(90deg,var(--dim) 30%,var(--fg) 50%,var(--dim) 70%);background-size:200% 100%;-webkit-background-clip:text;background-clip:text;color:transparent;animation:sh 1.6s linear infinite}\n@keyframes sh{from{background-position:150% 0}to{background-position:-50% 0}}\n.composer{padding:0 18px 12px;padding-bottom:max(12px,env(safe-area-inset-bottom))}\n.box{max-width:820px;margin:0 auto;border:1px solid var(--line);border-radius:14px;background:var(--bg2);transition:border-color .15s}\n.box:focus-within{border-color:var(--a)}\ntextarea{width:100%;background:none;border:none;outline:none;resize:none;color:var(--fg);font:15px/1.5 var(--sans);padding:12px 14px 4px;max-height:220px}\n.row{display:flex;align-items:center;gap:6px;padding:6px 8px 8px}\n.ib{width:34px;height:34px;border-radius:9px;display:grid;place-items:center;color:var(--dim)}\n.ib:hover{background:var(--bg3);color:var(--fg)}\n.ib.rec{background:var(--err);color:#fff;animation:pulse 1s infinite}\n@keyframes pulse{50%{opacity:.6}}\n.send{background:var(--a);color:var(--on-a)}.send:hover{background:var(--a);filter:brightness(1.1);color:var(--on-a)}\n.send:disabled{background:var(--bg3);color:var(--dim);opacity:1}\n.send:disabled{opacity:.35;cursor:default}\n.spacer{flex:1}\n.status{max-width:820px;margin:8px auto 0;display:flex;gap:14px;align-items:center;font:11.5px var(--mono);color:var(--dim);flex-wrap:wrap}\n.ctx{display:flex;align-items:center;gap:6px}.ctx .bar{width:70px;height:5px;border-radius:3px;background:var(--line);overflow:hidden}.ctx .bar i{display:block;height:100%;background:var(--ok)}\n.popup{position:fixed;inset:0;background:rgba(0,0,0,.55);display:grid;place-items:center;padding:16px;z-index:20}\n.popup>div{background:var(--bg);border:1px solid var(--line);border-radius:14px;padding:8px;width:min(420px,100%);max-height:70vh;overflow:auto}\n.popup button{display:flex;width:100%;text-align:left;padding:9px 12px;border-radius:8px;gap:10px;align-items:center}\n.popup button:hover{background:var(--bg2)}.popup .d{color:var(--dim);font-size:12px;margin-left:auto}\n.hidden{display:none!important}\n@media (max-width:760px){.app{grid-template-columns:1fr}aside{position:fixed;inset:0 25% 0 0;z-index:15;transform:translateX(-102%);transition:transform .2s;box-shadow:0 0 60px rgba(0,0,0,.5)}\n  aside.open{transform:none}.ib.menu{display:grid}.inner{padding:16px 14px}.composer{padding:0 10px 10px}}\n.sw{width:30px;height:20px;border-radius:6px;border:1px solid;display:flex;gap:3px;align-items:center;justify-content:center;flex-shrink:0}\n.sw i{width:7px;height:7px;border-radius:50%}\n.popup .more{display:block;padding:10px 12px;font-size:12.5px;color:var(--a);text-decoration:none}\n.panel{padding:4px 12px 10px;font-size:13px;overflow-x:auto}\n.panel table{border-collapse:collapse;font:12px var(--mono);margin-top:6px}\n.panel td,.panel th{border:1px solid var(--line);padding:4px 8px;text-align:left;white-space:nowrap}\n.panel th{color:var(--dim);font-weight:500}\n.tool.closed .panel{display:block}\n.askin{flex:1;min-width:160px;background:var(--bg2);border:1px solid var(--line);border-radius:8px;color:var(--fg);padding:7px 10px;font:inherit;outline:none}\n.pill.mode{color:var(--a);border-color:color-mix(in srgb,var(--a) 40%,var(--line))}\n.cf{color:var(--faint)}.cf.on{color:var(--ok)}\n.popup button b{font-weight:600;white-space:nowrap}.popup .d{text-align:right}\n.row .pill{min-width:0}\n#modelBtn{flex:0 1 auto;overflow:hidden}#modelBtn span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}\n#agentBtn,.ib{flex-shrink:0}\n@media (max-width:760px){.status{gap:8px;font-size:10.5px}.ctx .bar{width:44px}header{padding:8px 10px}}\n.scrim{display:none}\n.side-close{display:none}\n@media (max-width:760px){\n  .scrim{display:block;position:fixed;inset:0;background:rgba(0,0,0,.5);z-index:14;opacity:0;pointer-events:none;transition:opacity .2s}\n  aside.open+.scrim,.scrim.on{opacity:1;pointer-events:auto}\n  .side-close{display:grid;margin-left:6px}\n  .brand small{margin-left:auto}\n  .status .tok{display:none}\n}\n.tool .h .lab{direction:ltr}\n.todo-card{margin:0 12px 10px;border:1px solid var(--line);border-radius:10px;padding:10px 12px;font-size:12.5px;max-height:38vh;overflow:auto}\n.tc-h{display:flex;justify-content:space-between;align-items:center;font:12px var(--mono);color:var(--dim)}.tc-h b{color:var(--fg);font-weight:600}\n.tc-bar{height:4px;border-radius:3px;background:var(--line);margin:8px 0;overflow:hidden}.tc-bar i{display:block;height:100%;background:var(--ok)}\n.tc-i{display:flex;gap:8px;align-items:flex-start;padding:3px 0;line-height:1.4}.tc-i.done{color:var(--dim);text-decoration:line-through;text-decoration-color:var(--faint)}\n.tc-i.doing{color:var(--fg);font-weight:500}\n.tc-b{width:15px;height:15px;border:1.5px solid var(--faint);border-radius:4px;flex-shrink:0;display:grid;place-items:center;margin-top:1px}\n.tc-i.done .tc-b{border-color:var(--ok);color:var(--ok)}.tc-i.doing .tc-b{border:none}.tc-b svg{width:11px;height:11px;stroke-width:2.6}.tc-b .spin{width:13px;height:13px}\n.goal{display:flex;gap:10px;align-items:flex-start;border:1px solid var(--line);border-radius:10px;padding:10px 12px;margin:10px 0;font-size:13.5px}\n.goal.working{border-color:color-mix(in srgb,var(--a) 50%,var(--line))}.goal.done{border-color:color-mix(in srgb,var(--ok) 55%,var(--line))}.goal.blocked{border-color:color-mix(in srgb,var(--err) 55%,var(--line))}\n.review{border:1px solid var(--line);border-radius:10px;margin:10px 0;overflow:hidden}\n.rv-h{display:flex;justify-content:space-between;padding:9px 12px;font-size:13px;border-bottom:1px solid var(--line)}.rv-h .d{color:var(--dim);font:12px var(--mono)}\n.review details{border-bottom:1px solid var(--line)}.review summary{display:flex;gap:8px;align-items:center;padding:7px 12px;font:12.5px var(--mono);cursor:pointer;list-style:none}\n.review summary .rv-p{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}\n.review pre{font:12px/1.5 var(--mono);padding:8px 12px;max-height:320px;overflow:auto;white-space:pre;color:var(--dim);background:var(--bg2)}\n.review .btns{display:flex;gap:8px;padding:10px 12px;font:12px var(--mono)}.btn.danger{color:var(--err);border-color:color-mix(in srgb,var(--err) 45%,var(--line))}\n.tag{font:10.5px var(--mono);color:var(--b);border:1px solid var(--line);border-radius:5px;padding:0 5px}\n.dim{color:var(--dim)}.err{color:var(--err)}\n.perm{font:11.5px var(--mono);color:var(--dim);border:1px solid var(--line);border-radius:6px;padding:1px 7px}\n.perm.full{color:var(--warn);border-color:color-mix(in srgb,var(--warn) 45%,var(--line))}.perm.read{color:var(--b)}\n.atts{display:flex;gap:6px;flex-wrap:wrap;padding:10px 10px 0}\n.att{display:flex;align-items:center;gap:6px;border:1px solid var(--line);border-radius:8px;padding:3px 4px 3px 4px;font:12px var(--mono);max-width:100%;background:var(--bg)}\n.att img{width:28px;height:28px;object-fit:cover;border-radius:5px}.att .an{max-width:140px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}\n.att button{width:22px;height:22px;display:grid;place-items:center;color:var(--dim);border-radius:6px}.att button:hover{background:var(--bg3)}\nheader .pill span{max-width:90px;overflow:hidden;text-overflow:ellipsis}\n@media (max-width:760px){#previewBtn span{display:none}.status .cost{order:5}}\n@media (max-width:420px){#wsPill span{max-width:70px;overflow:hidden;text-overflow:ellipsis}}\n.side-set{display:flex;align-items:center;gap:8px;padding:8px 14px;border-top:1px solid var(--line);font-size:12.5px;color:var(--dim)}\n.lang{position:relative;display:grid;grid-template-columns:1fr 1fr;border:1px solid var(--line);border-radius:999px;background:var(--bg2);padding:2px;font:600 11.5px var(--mono)}\n.lang b{position:absolute;top:2px;bottom:2px;left:2px;width:calc(50% - 2px);border-radius:999px;background:var(--a);transition:transform .2s ease}\n.lang.ru b{transform:translateX(100%)}\n.lang button{position:relative;z-index:1;min-width:38px;padding:3px 10px;border-radius:999px;color:var(--dim);transition:color .2s}\n.lang button[aria-pressed=\"true\"]{color:var(--on-a)}\n.side-set+.side-foot{border-top:none;padding-top:4px}\n.btn,.perm{max-width:100%}\n.confirm .btns,.review .btns{flex-wrap:wrap}\n.review .rv-h{gap:8px}\n.si,.goal span,.info{overflow-wrap:anywhere}\n.popup .d{max-width:55%}\n.popup button b{white-space:normal}\n.status{row-gap:4px}\n@media (max-width:420px){header .title{min-width:0}.pill{padding:5px 7px}}\n\n.box{position:relative}\n.slash{position:absolute;left:0;right:0;bottom:calc(100% + 6px);background:var(--bg);border:1px solid var(--line);border-radius:12px;padding:4px;max-height:42vh;overflow:auto;box-shadow:0 8px 28px rgba(0,0,0,.35);z-index:15}\n.slash button{display:flex;width:100%;gap:10px;align-items:baseline;text-align:left;padding:7px 10px;border-radius:8px;font:13px var(--mono)}\n.slash button.on,.slash button:hover{background:var(--bg2)}.slash b{color:var(--a);font-weight:600}.slash .a{color:var(--faint)}.slash .d{margin-left:auto;color:var(--dim);font:12px var(--sans,inherit);text-align:right}\n.tool.cli{border-color:color-mix(in srgb,var(--sk) 55%,var(--line));border-left:3px solid var(--sk)}\n.tool.cli .h b{color:var(--sk)}\n.tool.cli .pbar{margin:0;border-radius:0;height:5px;background:color-mix(in srgb,var(--sk) 16%,var(--bg2))}\n.tool.cli .pbar i{background:var(--sk)}\n.tool.cli.done .pbar i{width:100%!important;animation:none;margin:0}.tool.cli.fail{--sk:var(--err)}\n\n.proj{margin:2px 0}.proj summary{display:flex;align-items:center;gap:8px;padding:7px 10px;border-radius:8px;cursor:pointer;list-style:none;color:var(--fg);font-size:13.5px;font-weight:600}\n.proj summary::-webkit-details-marker{display:none}.proj summary:hover{background:var(--bg2)}\n.proj summary span{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.proj summary .n{font:500 11px var(--mono);color:var(--dim)}\n.proj .pnew{width:24px;height:24px;opacity:.55}.proj summary:hover .pnew{opacity:1}\n.proj .si{padding-left:32px}.si.addp{color:var(--a);margin-top:6px}\n\n.fhead{display:flex;align-items:center;gap:8px;padding:8px 10px;border-bottom:1px solid var(--line);margin-bottom:4px;font:12.5px var(--mono)}\n.fhead b{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}\n.fview{font:12px/1.55 var(--mono);white-space:pre;overflow:auto;max-height:62vh;padding:8px 10px;color:var(--fg)}\n.fview i{display:inline-block;width:3.2em;color:var(--faint);font-style:normal;text-align:right;margin-right:12px;user-select:none}\n.popup>div:has(.fview){width:min(900px,100%)}\n@media (max-width:760px){#filesBtn span{display:none}}\n.popup .fhead .ib{width:30px;height:30px;flex:0 0 30px;padding:0;justify-content:center;border-radius:8px}\n</style>\n</head>\n<body>\n<div class=\"app\">\n<aside id=\"side\">\n  <div class=\"brand\"><i></i>me<small id=\"mode\"></small><button class=\"ib side-close\" onclick=\"closeSide()\" aria-label=\"close menu\" data-i18n-aria=\"close menu\"><svg viewBox=\"0 0 24 24\"><path d=\"M6 6l12 12M18 6L6 18\"/></svg></button></div>\n  <button class=\"newbtn\" onclick=\"newChat()\"><svg viewBox=\"0 0 24 24\"><path d=\"M12 5v14M5 12h14\"/></svg><span data-i18n=\"New chat\">New chat</span></button>\n  <div class=\"todo-card hidden\" id=\"todoCard\"></div>\n  <div class=\"slist\" id=\"slist\"></div>\n  <div class=\"side-set\"><span data-i18n=\"Language\">Language</span><span class=\"spacer\"></span><div class=\"lang\" role=\"group\" aria-label=\"Language\" data-i18n-aria=\"Language\"><b></b><button type=\"button\" data-l=\"en\" aria-pressed=\"true\">EN</button><button type=\"button\" data-l=\"ru\" aria-pressed=\"false\">RU</button></div></div>\n  <div class=\"side-foot\"><a id=\"acct\" href=\"/account\"><svg viewBox=\"0 0 24 24\"><circle cx=\"12\" cy=\"8\" r=\"4\"/><path d=\"M4 21c0-4 4-6 8-6s8 2 8 6\"/></svg><span id=\"who\">account</span></a>\n    <span class=\"spacer\"></span><button class=\"ib hidden\" id=\"bell\" onclick=\"enablePush()\" title=\"notify me when a task is done\" data-i18n-title=\"notify me when a task is done\"><svg viewBox=\"0 0 24 24\"><path d=\"M6 16V11a6 6 0 0 1 12 0v5l1.5 2h-15z\"/><path d=\"M10 20.5a2 2 0 0 0 4 0\"/></svg></button><button class=\"ib\" onclick=\"pickTheme()\" title=\"theme\" data-i18n-title=\"theme\" aria-label=\"theme\" data-i18n-aria=\"theme\"><svg viewBox=\"0 0 24 24\"><circle cx=\"12\" cy=\"12\" r=\"9\"/><path d=\"M12 3a9 9 0 0 0 0 18z\" fill=\"currentColor\"/></svg></button></div>\n</aside>\n<div class=\"scrim\" id=\"scrim\" onclick=\"closeSide()\"></div>\n<main>\n  <header>\n    <button class=\"ib menu\" onclick=\"openSide()\" aria-label=\"menu\" data-i18n-aria=\"menu\" title=\"menu\" data-i18n-title=\"menu\"><svg viewBox=\"0 0 24 24\"><path d=\"M4 7h16M4 12h16M4 17h16\"/></svg></button>\n    <div class=\"title\" id=\"title\">New chat</div>\n    <button class=\"pill hidden\" id=\"todoChip\" onclick=\"openSide()\" title=\"checklist\" data-i18n-title=\"checklist\"><svg viewBox=\"0 0 24 24\"><path d=\"M4 7l2 2 4-4M4 17l2 2 4-4M13 7h7M13 17h7\"/></svg><span></span></button>\n    <a class=\"pill hidden\" id=\"previewBtn\" target=\"_blank\" rel=\"noopener\" title=\"open the site the agent built\" data-i18n-title=\"open the site the agent built\"><svg viewBox=\"0 0 24 24\"><path d=\"M14 4h6v6M20 4l-9 9M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5\"/></svg><span data-i18n=\"preview\">preview</span></a>\n    <button class=\"pill\" id=\"filesBtn\" onclick=\"openFiles('')\" title=\"files\" data-i18n-title=\"files\"><svg viewBox=\"0 0 24 24\"><path d=\"M14 3H6a1 1 0 0 0-1 1v16a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1V8z\"/><path d=\"M14 3v5h5\"/></svg><span data-i18n=\"files\">files</span></button>\n    <button class=\"pill\" id=\"wsPill\" title=\"workspace\" data-i18n-title=\"workspace\"><svg viewBox=\"0 0 24 24\"><path d=\"M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z\"/></svg><span id=\"ws\">\u2014</span></button>\n  </header>\n  <div id=\"chat\"><div class=\"inner\" id=\"log\"></div></div>\n  <div class=\"composer\">\n    <div class=\"box\">\n      <div class=\"atts hidden\" id=\"atts\"></div>\n      <textarea id=\"input\" rows=\"1\" placeholder=\"Ask anything\u2026\" data-i18n-ph=\"Ask anything\u2026\"></textarea>\n      <div class=\"row\">\n        <button class=\"pill\" id=\"modelBtn\" onclick=\"pickModel()\"><svg viewBox=\"0 0 24 24\"><circle cx=\"12\" cy=\"12\" r=\"3\"/><path d=\"M12 2v3M12 19v3M2 12h3M19 12h3M5 5l2 2M17 17l2 2M5 19l2-2M17 7l2-2\"/></svg><span id=\"model\">model</span></button>\n        <button class=\"pill mode\" id=\"agentBtn\" onclick=\"pickAgent()\"><svg viewBox=\"0 0 24 24\"><path d=\"M4 7h10M4 12h16M4 17h7\"/><circle cx=\"18\" cy=\"7\" r=\"2\"/><circle cx=\"15\" cy=\"17\" r=\"2\"/></svg><span id=\"agentName\">code</span></button>\n        <span class=\"spacer\"></span>\n        <input type=\"file\" id=\"file\" multiple hidden>\n        <button class=\"ib\" id=\"attach\" title=\"attach images or files\" data-i18n-title=\"attach images or files\" aria-label=\"attach images or files\" data-i18n-aria=\"attach images or files\" onclick=\"$('file').click()\"><svg viewBox=\"0 0 24 24\"><path d=\"M20 11.5l-8.4 8.4a5 5 0 0 1-7-7l8.5-8.5a3.3 3.3 0 0 1 4.7 4.7l-8.5 8.5a1.7 1.7 0 0 1-2.4-2.4l7.8-7.8\"/></svg></button>\n        <button class=\"ib\" id=\"mic\" title=\"hold to talk\" data-i18n-title=\"hold to talk\" aria-label=\"hold to talk\" data-i18n-aria=\"hold to talk\"><svg viewBox=\"0 0 24 24\"><rect x=\"9\" y=\"3\" width=\"6\" height=\"11\" rx=\"3\"/><path d=\"M5 11a7 7 0 0 0 14 0M12 18v3\"/></svg></button>\n        <button class=\"ib send\" id=\"send\" onclick=\"sendOrStop()\" disabled aria-label=\"send\" data-i18n-aria=\"send\"><svg viewBox=\"0 0 24 24\" id=\"sendIcon\"><path d=\"M12 19V5M5 12l7-7 7 7\"/></svg></button>\n      </div>\n    </div>\n    <div class=\"status\"><button class=\"perm\" id=\"perm\" onclick=\"pickPerm()\" title=\"permissions\" data-i18n-title=\"permissions\"></button><span id=\"ctxfiles\"></span><span class=\"ctx\" id=\"ctx\"></span><span id=\"cost\" class=\"cost\"></span><span id=\"tok\" class=\"tok\"></span><span id=\"sync\"></span></div>\n  </div>\n</main>\n</div>\n<div class=\"popup hidden\" id=\"popup\" onclick=\"if(event.target===this)this.classList.add('hidden')\"><div id=\"pbody\"></div></div>\n<script>// \u2500\u2500\u2500 i18n: English is the source text; RU is a lookup table. t(\"English\") / tt(\"English\") (retranslatable span) \u2500\u2500\u2500\nconst RU = {\"files\": \"\u0444\u0430\u0439\u043b\u044b\", \"empty folder\": \"\u043f\u0443\u0441\u0442\u0430\u044f \u043f\u0430\u043f\u043a\u0430\", \"back\": \"\u043d\u0430\u0437\u0430\u0434\", \"ask about this file\": \"\u0441\u043f\u0440\u043e\u0441\u0438\u0442\u044c \u043f\u0440\u043e \u044d\u0442\u043e\u0442 \u0444\u0430\u0439\u043b\", \"open a project folder\": \"\u043e\u0442\u043a\u0440\u044b\u0442\u044c \u043f\u0430\u043f\u043a\u0443 \u043f\u0440\u043e\u0435\u043a\u0442\u0430\", \"(no folder)\": \"(\u0431\u0435\u0437 \u043f\u0430\u043f\u043a\u0438)\", \"cached\": \"\u0438\u0437 \u043a\u044d\u0448\u0430\", \"context\": \"\u043a\u043e\u043d\u0442\u0435\u043a\u0441\u0442\", \"every step re-sends the context; cached input costs a fraction\": \"\u043a\u0430\u0436\u0434\u044b\u0439 \u0448\u0430\u0433 \u0437\u0430\u043d\u043e\u0432\u043e \u043e\u0442\u043f\u0440\u0430\u0432\u043b\u044f\u0435\u0442 \u043a\u043e\u043d\u0442\u0435\u043a\u0441\u0442; \u043a\u044d\u0448\u0438\u0440\u043e\u0432\u0430\u043d\u043d\u044b\u0439 \u0432\u0432\u043e\u0434 \u0441\u0442\u043e\u0438\u0442 \u0432 \u0440\u0430\u0437\u044b \u0434\u0435\u0448\u0435\u0432\u043b\u0435\", \n  \"New chat\": \"\u041d\u043e\u0432\u044b\u0439 \u0447\u0430\u0442\", \"Chat\": \"\u0427\u0430\u0442\", \"close menu\": \"\u0437\u0430\u043a\u0440\u044b\u0442\u044c \u043c\u0435\u043d\u044e\", \"account\": \"\u0430\u043a\u043a\u0430\u0443\u043d\u0442\", \"local\": \"\u043b\u043e\u043a\u0430\u043b\u044c\u043d\u043e\", \"cloud\": \"\u043e\u0431\u043b\u0430\u043a\u043e\",\n  \"notify me when a task is done\": \"\u0443\u0432\u0435\u0434\u043e\u043c\u043b\u044f\u0442\u044c \u043e \u0437\u0430\u0432\u0435\u0440\u0448\u0435\u043d\u0438\u0438 \u0437\u0430\u0434\u0430\u0447\u0438\", \"theme\": \"\u0442\u0435\u043c\u0430\", \"Language\": \"\u042f\u0437\u044b\u043a\", \"checklist\": \"\u0447\u0435\u043a-\u043b\u0438\u0441\u0442\",\n  \"preview\": \"\u043f\u0440\u043e\u0441\u043c\u043e\u0442\u0440\", \"open the site the agent built\": \"\u043e\u0442\u043a\u0440\u044b\u0442\u044c \u0441\u0430\u0439\u0442, \u0441\u043e\u0437\u0434\u0430\u043d\u043d\u044b\u0439 \u0430\u0433\u0435\u043d\u0442\u043e\u043c\", \"workspace\": \"\u0440\u0430\u0431\u043e\u0447\u0430\u044f \u043e\u0431\u043b\u0430\u0441\u0442\u044c\",\n  \"Ask anything\u2026\": \"\u0421\u043f\u0440\u043e\u0441\u0438\u0442\u0435 \u0447\u0442\u043e \u0443\u0433\u043e\u0434\u043d\u043e\u2026\", \"model\": \"\u043c\u043e\u0434\u0435\u043b\u044c\", \"no model\": \"\u043d\u0435\u0442 \u043c\u043e\u0434\u0435\u043b\u0438\", \"code\": \"\u043a\u043e\u0434\", \"attach images or files\": \"\u043f\u0440\u0438\u043a\u0440\u0435\u043f\u0438\u0442\u044c \u0438\u0437\u043e\u0431\u0440\u0430\u0436\u0435\u043d\u0438\u044f \u0438\u043b\u0438 \u0444\u0430\u0439\u043b\u044b\",\n  \"hold to talk\": \"\u0443\u0434\u0435\u0440\u0436\u0438\u0432\u0430\u0439\u0442\u0435, \u0447\u0442\u043e\u0431\u044b \u0433\u043e\u0432\u043e\u0440\u0438\u0442\u044c\", \"permissions\": \"\u0440\u0430\u0437\u0440\u0435\u0448\u0435\u043d\u0438\u044f\", \"menu\": \"\u043c\u0435\u043d\u044e\", \"send\": \"\u043e\u0442\u043f\u0440\u0430\u0432\u0438\u0442\u044c\", \"stop\": \"\u0441\u0442\u043e\u043f\",\n  \"what are we\": \"\u0447\u0442\u043e \u0431\u0443\u0434\u0435\u043c\", \"building?\": \"\u0441\u0442\u0440\u043e\u0438\u0442\u044c?\",\n  \"Runs in your cloud sandbox \u00b7 files sync to all your machines\": \"\u0420\u0430\u0431\u043e\u0442\u0430\u0435\u0442 \u0432 \u043e\u0431\u043b\u0430\u0447\u043d\u043e\u0439 \u043f\u0435\u0441\u043e\u0447\u043d\u0438\u0446\u0435 \u00b7 \u0444\u0430\u0439\u043b\u044b \u0441\u0438\u043d\u0445\u0440\u043e\u043d\u0438\u0437\u0438\u0440\u0443\u044e\u0442\u0441\u044f \u043c\u0435\u0436\u0434\u0443 \u0432\u0430\u0448\u0438\u043c\u0438 \u0443\u0441\u0442\u0440\u043e\u0439\u0441\u0442\u0432\u0430\u043c\u0438\",\n  \"Running locally on this machine\": \"\u0420\u0430\u0431\u043e\u0442\u0430\u0435\u0442 \u043b\u043e\u043a\u0430\u043b\u044c\u043d\u043e \u043d\u0430 \u044d\u0442\u043e\u043c \u043a\u043e\u043c\u043f\u044c\u044e\u0442\u0435\u0440\u0435\",\n  \"Today\": \"\u0421\u0435\u0433\u043e\u0434\u043d\u044f\", \"This week\": \"\u041d\u0430 \u044d\u0442\u043e\u0439 \u043d\u0435\u0434\u0435\u043b\u0435\", \"Older\": \"\u0420\u0430\u043d\u044c\u0448\u0435\", \"no chats yet\": \"\u0447\u0430\u0442\u043e\u0432 \u043f\u043e\u043a\u0430 \u043d\u0435\u0442\", \"(untitled)\": \"(\u0431\u0435\u0437 \u043d\u0430\u0437\u0432\u0430\u043d\u0438\u044f)\",\n  \"Checklist\": \"\u0427\u0435\u043a-\u043b\u0438\u0441\u0442\", \"Goal\": \"\u0426\u0435\u043b\u044c\", \"Goal done\": \"\u0426\u0435\u043b\u044c \u0434\u043e\u0441\u0442\u0438\u0433\u043d\u0443\u0442\u0430\", \"Goal blocked\": \"\u0426\u0435\u043b\u044c \u0437\u0430\u0431\u043b\u043e\u043a\u0438\u0440\u043e\u0432\u0430\u043d\u0430\",\n  \"thinking\u2026\": \"\u0434\u0443\u043c\u0430\u044e\u2026\", \"thought for {d}\": \"\u0434\u0443\u043c\u0430\u043b {d}\", \"working\u2026\": \"\u0440\u0430\u0431\u043e\u0442\u0430\u044e\u2026\", \"copy\": \"\u043a\u043e\u043f\u0438\u0440\u043e\u0432\u0430\u0442\u044c\",\n  \"Allow {name}?\": \"\u0420\u0430\u0437\u0440\u0435\u0448\u0438\u0442\u044c {name}?\", \"allow\": \"\u0440\u0430\u0437\u0440\u0435\u0448\u0438\u0442\u044c\", \"always allow\": \"\u0432\u0441\u0435\u0433\u0434\u0430 \u0440\u0430\u0437\u0440\u0435\u0448\u0430\u0442\u044c\", \"allow all (full)\": \"\u0440\u0430\u0437\u0440\u0435\u0448\u0438\u0442\u044c \u0432\u0441\u0451 (\u043f\u043e\u043b\u043d\u044b\u0439 \u0434\u043e\u0441\u0442\u0443\u043f)\", \"deny\": \"\u043e\u0442\u043a\u043b\u043e\u043d\u0438\u0442\u044c\",\n  \"answer\u2026\": \"\u043e\u0442\u0432\u0435\u0442\u2026\", \"interrupted\": \"\u043f\u0440\u0435\u0440\u0432\u0430\u043d\u043e\",\n  \"compacting history\u2026\": \"\u0441\u0436\u0438\u043c\u0430\u044e \u0438\u0441\u0442\u043e\u0440\u0438\u044e\u2026\", \"compacted {a} \u2192 {b} messages \u00b7 ~{c} tokens\": \"\u0438\u0441\u0442\u043e\u0440\u0438\u044f \u0441\u0436\u0430\u0442\u0430: {a} \u2192 {b} \u0441\u043e\u043e\u0431\u0449\u0435\u043d\u0438\u0439 \u00b7 ~{c} \u0442\u043e\u043a\u0435\u043d\u043e\u0432\",\n  \"self-check: running checks before finishing\": \"\u0441\u0430\u043c\u043e\u043f\u0440\u043e\u0432\u0435\u0440\u043a\u0430: \u0437\u0430\u043f\u0443\u0441\u043a\u0430\u044e \u043f\u0440\u043e\u0432\u0435\u0440\u043a\u0438 \u043f\u0435\u0440\u0435\u0434 \u0437\u0430\u0432\u0435\u0440\u0448\u0435\u043d\u0438\u0435\u043c\",\n  \"{a} \u00b7 \u2191 {i} \u2193 {o} \u00b7 {n} tokens{c}\": \"{a} \u00b7 \u2191 {i} \u2193 {o} \u00b7 {n} \u0442\u043e\u043a\u0435\u043d\u043e\u0432{c}\",\n  \"sandbox restarted \u2014 your session and files are safe\": \"\u043f\u0435\u0441\u043e\u0447\u043d\u0438\u0446\u0430 \u043f\u0435\u0440\u0435\u0437\u0430\u043f\u0443\u0449\u0435\u043d\u0430 \u2014 \u0441\u0435\u0441\u0441\u0438\u044f \u0438 \u0444\u0430\u0439\u043b\u044b \u0432 \u0431\u0435\u0437\u043e\u043f\u0430\u0441\u043d\u043e\u0441\u0442\u0438\",\n  \"Changes in this turn\": \"\u0418\u0437\u043c\u0435\u043d\u0435\u043d\u0438\u044f \u0437\u0430 \u044d\u0442\u043e\u0442 \u0445\u043e\u0434\", \"{n} {files}\": \"{n} {files}\", \"new\": \"\u043d\u043e\u0432\u044b\u0439\", \"keep\": \"\u043e\u0441\u0442\u0430\u0432\u0438\u0442\u044c\", \"revert\": \"\u043e\u0442\u043a\u0430\u0442\u0438\u0442\u044c\", \"kept\": \"\u043e\u0441\u0442\u0430\u0432\u043b\u0435\u043d\u043e\", \"reverting\u2026\": \"\u043e\u0442\u043a\u0430\u0442\u044b\u0432\u0430\u044e\u2026\", \"reverted\": \"\u043e\u0442\u043a\u0430\u0442\u0435\u043d\u043e\",\n  \"ctx\": \"\u043a\u043e\u043d\u0442\u0435\u043a\u0441\u0442\", \"this task\": \"\u044d\u0442\u0430 \u0437\u0430\u0434\u0430\u0447\u0430\", \"session\": \"\u0441\u0435\u0441\u0441\u0438\u044f\", \"today\": \"\u0441\u0435\u0433\u043e\u0434\u043d\u044f\",\n  \"loaded into every turn\": \"\u0437\u0430\u0433\u0440\u0443\u0436\u0430\u0435\u0442\u0441\u044f \u0432 \u043a\u0430\u0436\u0434\u044b\u0439 \u0445\u043e\u0434\", \"ask the agent to create it, or /init in the terminal\": \"\u043f\u043e\u043f\u0440\u043e\u0441\u0438\u0442\u0435 \u0430\u0433\u0435\u043d\u0442\u0430 \u0441\u043e\u0437\u0434\u0430\u0442\u044c \u0435\u0433\u043e \u0438\u043b\u0438 \u0432\u044b\u043f\u043e\u043b\u043d\u0438\u0442\u0435 /init \u0432 \u0442\u0435\u0440\u043c\u0438\u043d\u0430\u043b\u0435\", \"no AGENTS.md\": \"\u043d\u0435\u0442 AGENTS.md\",\n  \"read-only\": \"\u0442\u043e\u043b\u044c\u043a\u043e \u0447\u0442\u0435\u043d\u0438\u0435\", \"ask\": \"\u0441\u043f\u0440\u0430\u0448\u0438\u0432\u0430\u0442\u044c\", \"full access\": \"\u043f\u043e\u043b\u043d\u044b\u0439 \u0434\u043e\u0441\u0442\u0443\u043f\", \"sandbox\": \"\u043f\u0435\u0441\u043e\u0447\u043d\u0438\u0446\u0430\",\n  \"no file changes; only allow-listed commands\": \"\u0431\u0435\u0437 \u0438\u0437\u043c\u0435\u043d\u0435\u043d\u0438\u0439 \u0444\u0430\u0439\u043b\u043e\u0432; \u0442\u043e\u043b\u044c\u043a\u043e \u0440\u0430\u0437\u0440\u0435\u0448\u0451\u043d\u043d\u044b\u0435 \u043a\u043e\u043c\u0430\u043d\u0434\u044b\",\n  \"edits inside the project are free; commands ask\": \"\u043f\u0440\u0430\u0432\u043a\u0438 \u0432 \u043f\u0440\u043e\u0435\u043a\u0442\u0435 \u0441\u0432\u043e\u0431\u043e\u0434\u043d\u044b; \u043a\u043e\u043c\u0430\u043d\u0434\u044b \u0437\u0430\u043f\u0440\u0430\u0448\u0438\u0432\u0430\u044e\u0442 \u043f\u043e\u0434\u0442\u0432\u0435\u0440\u0436\u0434\u0435\u043d\u0438\u0435\",\n  \"everything runs without asking\": \"\u0432\u0441\u0451 \u0432\u044b\u043f\u043e\u043b\u043d\u044f\u0435\u0442\u0441\u044f \u0431\u0435\u0437 \u0437\u0430\u043f\u0440\u043e\u0441\u0430\",\n  \"Cloud sessions run in an isolated sandbox, so tools run without asking. Permissions apply to the local agent\": \"\u041e\u0431\u043b\u0430\u0447\u043d\u044b\u0435 \u0441\u0435\u0441\u0441\u0438\u0438 \u0440\u0430\u0431\u043e\u0442\u0430\u044e\u0442 \u0432 \u0438\u0437\u043e\u043b\u0438\u0440\u043e\u0432\u0430\u043d\u043d\u043e\u0439 \u043f\u0435\u0441\u043e\u0447\u043d\u0438\u0446\u0435, \u043f\u043e\u044d\u0442\u043e\u043c\u0443 \u0438\u043d\u0441\u0442\u0440\u0443\u043c\u0435\u043d\u0442\u044b \u0432\u044b\u043f\u043e\u043b\u043d\u044f\u044e\u0442\u0441\u044f \u0431\u0435\u0437 \u0437\u0430\u043f\u0440\u043e\u0441\u0430. \u0420\u0430\u0437\u0440\u0435\u0448\u0435\u043d\u0438\u044f \u043e\u0442\u043d\u043e\u0441\u044f\u0442\u0441\u044f \u043a \u043b\u043e\u043a\u0430\u043b\u044c\u043d\u043e\u043c\u0443 \u0430\u0433\u0435\u043d\u0442\u0443\",\n  \"terminal\": \"\u0442\u0435\u0440\u043c\u0438\u043d\u0430\u043b\",\n  \"No models \u2014 add one in your account or with /models.\": \"\u041d\u0435\u0442 \u043c\u043e\u0434\u0435\u043b\u0435\u0439 \u2014 \u0434\u043e\u0431\u0430\u0432\u044c\u0442\u0435 \u0438\u0445 \u0432 \u0430\u043a\u043a\u0430\u0443\u043d\u0442\u0435 \u0438\u043b\u0438 \u043a\u043e\u043c\u0430\u043d\u0434\u043e\u0439 /models.\",\n  \"more agents in the marketplace \u2192\": \"\u0431\u043e\u043b\u044c\u0448\u0435 \u0430\u0433\u0435\u043d\u0442\u043e\u0432 \u0432 \u043a\u0430\u0442\u0430\u043b\u043e\u0433\u0435 \u2192\", \"more themes in the marketplace \u2192\": \"\u0431\u043e\u043b\u044c\u0448\u0435 \u0442\u0435\u043c \u0432 \u043a\u0430\u0442\u0430\u043b\u043e\u0433\u0435 \u2192\",\n  \"Install more with /theme in the terminal or the marketplace on the site\": \"\u0423\u0441\u0442\u0430\u043d\u043e\u0432\u0438\u0442\u0435 \u0434\u0440\u0443\u0433\u0438\u0435 \u043a\u043e\u043c\u0430\u043d\u0434\u043e\u0439 /theme \u0432 \u0442\u0435\u0440\u043c\u0438\u043d\u0430\u043b\u0435 \u0438\u043b\u0438 \u0432 \u043a\u0430\u0442\u0430\u043b\u043e\u0433\u0435 \u043d\u0430 \u0441\u0430\u0439\u0442\u0435\",\n  \"light\": \"\u0441\u0432\u0435\u0442\u043b\u0430\u044f\", \"dark\": \"\u0442\u0451\u043c\u043d\u0430\u044f\",\n  \"microphone: {m}\": \"\u043c\u0438\u043a\u0440\u043e\u0444\u043e\u043d: {m}\", \"transcribing\u2026\": \"\u0440\u0430\u0441\u043f\u043e\u0437\u043d\u0430\u044e\u2026\",\n  \"Project folder for the local agent\": \"\u041f\u0430\u043f\u043a\u0430 \u043f\u0440\u043e\u0435\u043a\u0442\u0430 \u0434\u043b\u044f \u043b\u043e\u043a\u0430\u043b\u044c\u043d\u043e\u0433\u043e \u0430\u0433\u0435\u043d\u0442\u0430\",\n  \"remove\": \"\u0443\u0434\u0430\u043b\u0438\u0442\u044c\", \"{name}: over 20 MB\": \"{name}: \u0431\u043e\u043b\u044c\u0448\u0435 20 \u041c\u0411\",\n  \"Notifications are blocked for this site \u2014 allow them in the browser settings.\": \"\u0423\u0432\u0435\u0434\u043e\u043c\u043b\u0435\u043d\u0438\u044f \u0434\u043b\u044f \u044d\u0442\u043e\u0433\u043e \u0441\u0430\u0439\u0442\u0430 \u0437\u0430\u0431\u043b\u043e\u043a\u0438\u0440\u043e\u0432\u0430\u043d\u044b \u2014 \u0440\u0430\u0437\u0440\u0435\u0448\u0438\u0442\u0435 \u0438\u0445 \u0432 \u043d\u0430\u0441\u0442\u0440\u043e\u0439\u043a\u0430\u0445 \u0431\u0440\u0430\u0443\u0437\u0435\u0440\u0430.\",\n  \"notifications: {m}\": \"\u0443\u0432\u0435\u0434\u043e\u043c\u043b\u0435\u043d\u0438\u044f: {m}\",\n  \"notifications on \u2014 you will hear when a task is done or needs you\": \"\u0443\u0432\u0435\u0434\u043e\u043c\u043b\u0435\u043d\u0438\u044f \u0432\u043a\u043b\u044e\u0447\u0435\u043d\u044b \u2014 \u0432\u044b \u0443\u0437\u043d\u0430\u0435\u0442\u0435, \u043a\u043e\u0433\u0434\u0430 \u0437\u0430\u0434\u0430\u0447\u0430 \u0432\u044b\u043f\u043e\u043b\u043d\u0435\u043d\u0430 \u0438\u043b\u0438 \u043d\u0443\u0436\u043d\u0430 \u0432\u0430\u0448\u0430 \u043f\u043e\u043c\u043e\u0449\u044c\",\n};\nconst RUPL = {\"step\": [\"\u0448\u0430\u0433\", \"\u0448\u0430\u0433\u0430\", \"\u0448\u0430\u0433\u043e\u0432\"], file: [\"\u0444\u0430\u0439\u043b\", \"\u0444\u0430\u0439\u043b\u0430\", \"\u0444\u0430\u0439\u043b\u043e\u0432\"], image: [\"\u0438\u0437\u043e\u0431\u0440\u0430\u0436\u0435\u043d\u0438\u0435\", \"\u0438\u0437\u043e\u0431\u0440\u0430\u0436\u0435\u043d\u0438\u044f\", \"\u0438\u0437\u043e\u0431\u0440\u0430\u0436\u0435\u043d\u0438\u0439\"]};\nconst RUINV = Object.fromEntries(Object.entries(RU).map(([k, v]) => [v, k]));\nlet LANG = \"en\";\ntry { LANG = localStorage.getItem(\"me-lang\") || \"\"; } catch (e) {}\nif (LANG !== \"ru\" && LANG !== \"en\") LANG = (navigator.languages || [navigator.language || \"en\"]).some(l => /^(ru|be|kk)\\b/i.test(l)) ? \"ru\" : \"en\";\ndocument.documentElement.lang = LANG;\nfunction t(s, v) {\n  let r = LANG === \"ru\" && RU[s] !== undefined ? RU[s] : s;\n  if (v) r = r.replace(/\\{(\\w+)\\}/g, (m, k) => k in v ? v[k] : m);\n  return r;\n}\nconst untr = s => RUINV[s] || s;  // back to the English key (for text we stored translated)\nconst T = (s, v) => t(s, v);  // for code where a local `t` (an event type) hides the translator\nfunction pl(n, one, many) {  // \"1 file\" / \"3 files\"; Russian has three forms\n  if (LANG === \"ru\" && RUPL[one]) { const f = RUPL[one], a = n % 100, b = n % 10; return f[a > 10 && a < 20 ? 2 : b === 1 ? 0 : b > 1 && b < 5 ? 1 : 2]; }\n  return n === 1 ? one : many;\n}\nconst _e = s => String(s ?? \"\").replace(/[&<>\"]/g, c => ({\"&\": \"&amp;\", \"<\": \"&lt;\", \">\": \"&gt;\", '\"': \"&quot;\"})[c]);\nfunction tt(s, v) {  // HTML span that re-translates itself when the language flips\n  return `<span data-t=\"${_e(s)}\"${v ? ` data-v=\"${_e(JSON.stringify(v))}\"` : \"\"}>${_e(t(s, v))}</span>`;\n}\nfunction i18nApply(root) {\n  root = root || document;\n  root.querySelectorAll(\"[data-i18n]\").forEach(n => n.textContent = t(n.dataset.i18n));\n  root.querySelectorAll(\"[data-i18n-title]\").forEach(n => n.title = t(n.dataset.i18nTitle));\n  root.querySelectorAll(\"[data-i18n-aria]\").forEach(n => n.setAttribute(\"aria-label\", t(n.dataset.i18nAria)));\n  root.querySelectorAll(\"[data-i18n-ph]\").forEach(n => n.placeholder = t(n.dataset.i18nPh));\n  root.querySelectorAll(\"[data-t]\").forEach(n => { let v; try { v = n.dataset.v ? JSON.parse(n.dataset.v) : undefined; } catch (e) {} n.textContent = t(n.dataset.t, v); });\n}\nfunction setLang(l) {\n  LANG = l; try { localStorage.setItem(\"me-lang\", l); } catch (e) {}\n  document.documentElement.lang = l;\n  document.querySelectorAll(\".lang\").forEach(g => { g.classList.toggle(\"ru\", l === \"ru\"); g.querySelectorAll(\"button\").forEach(b => b.setAttribute(\"aria-pressed\", b.dataset.l === l)); });\n  i18nApply();\n  const ti = $(\"title\"); ti.textContent = t(untr(ti.textContent));\n  if (typeof renderChrome === \"function\") { renderChrome(); renderStatus(); renderSessions(); renderTodo(state.session?.todo || []); if (log.querySelector(\".empty\")) emptyState(); }\n  $(\"input\").placeholder = t(\"Ask anything\u2026\");\n}\ndocument.addEventListener(\"click\", e => { const b = e.target.closest && e.target.closest(\".lang button\");\n  if (b) { setLang(b.dataset.l); if (typeof cfgSet === \"function\" && base) cfgSet(\"lang\", b.dataset.l).catch(() => {}); } });  // the choice follows you to every device\n\nconst MODE = \"__MODE__\";\nconst $ = id => document.getElementById(id);\nconst side = $(\"side\");\nconst esc = s => String(s ?? \"\").replace(/[&<>\"]/g, c => ({\"&\": \"&amp;\", \"<\": \"&lt;\", \">\": \"&gt;\", '\"': \"&quot;\"})[c]);\nconst human = n => !n ? \"0\" : n < 1000 ? String(n) : n < 1e6 ? (n / 1000).toFixed(1).replace(\".0\", \"\") + \"k\" : (n / 1e6).toFixed(1).replace(\".0\", \"\") + \"M\";\nconst money = x => !x ? \"$0\" : x < 0.01 ? \"$\" + x.toFixed(4) : x < 1 ? \"$\" + x.toFixed(3) : \"$\" + x.toFixed(2);\nconst dur = s => s < 60 ? s.toFixed(1) + \"s\" : Math.floor(s / 60) + \"m\" + String(Math.floor(s % 60)).padStart(2, \"0\") + \"s\";\nconst COLORS = {red:\"#ff6b7a\",green:\"#5ad18a\",blue:\"#6ba6ff\",magenta:\"#d27cff\",cyan:\"#4fd1c5\",yellow:\"#e7c66b\",orange:\"#ffb454\",purple:\"#8b7cff\",pink:\"#ff8fc7\"};\nconst I = {\n  check: '<svg viewBox=\"0 0 24 24\" style=\"color:var(--ok)\"><path d=\"M5 12l5 5 9-11\"/></svg>',\n  x: '<svg viewBox=\"0 0 24 24\" style=\"color:var(--err)\"><path d=\"M6 6l12 12M18 6L6 18\"/></svg>',\n  stop: '<path d=\"M7 7h10v10H7z\" fill=\"currentColor\"/>', up: '<path d=\"M12 19V5M5 12l7-7 7 7\"/>',\n  think: '<svg viewBox=\"0 0 24 24\"><path d=\"M9 18h6M10 21h4M12 3a6 6 0 0 0-4 10.5V16h8v-2.5A6 6 0 0 0 12 3z\"/></svg>',\n};\nasync function copyText(s) {  // navigator.clipboard needs https or localhost: fall back for LAN / webviews\n  try { if (navigator.clipboard && window.isSecureContext) { await navigator.clipboard.writeText(s); return true; } } catch (e) {}\n  const ta = document.createElement(\"textarea\"); ta.value = s; ta.setAttribute(\"readonly\", \"\"); ta.style.cssText = \"position:fixed;top:-1000px;opacity:0\";\n  document.body.appendChild(ta); ta.select(); ta.setSelectionRange(0, s.length);\n  let ok = false; try { ok = document.execCommand(\"copy\"); } catch (e) {}\n  ta.remove(); return ok;\n}\nfunction copyCode(b) {\n  copyText(b.closest(\"pre\").querySelector(\"code\").innerText).then(ok => { b.textContent = ok ? \"\u2713\" : \"\u2717\"; setTimeout(() => b.textContent = t(\"copy\"), 1200); });\n}\nvar atts = [];  // pending attachments: {name, image, url | b64}\nlet token = localStorage.getItem(\"me_token\") || \"\", base = \"\", after = 0, state = {}, busy = false, live = {}, cur = {}, polling = 0, turnT0 = 0;\n\n// \u2500\u2500\u2500 transport: identical endpoints for local (`me web`) and cloud (/app) \u2500\u2500\u2500\nasync function api(path, body, method) {\n  const h = {\"Content-Type\": \"application/json\"};\n  if (token) h.Authorization = \"Bearer \" + token;\n  const r = await fetch(path, {method: method || (body !== undefined ? \"POST\" : \"GET\"), headers: h, body: body !== undefined ? JSON.stringify(body) : undefined});\n  const j = await r.json().catch(() => ({}));\n  if (r.status === 401 && MODE === \"cloud\") { location.href = \"/account#cloud\"; throw new Error(\"login\"); }\n  if (!r.ok) throw new Error(j.detail || r.statusText);\n  return j;\n}\nconst A = {\n  async boot() {\n    if (MODE === \"local\") {\n      const k = new URLSearchParams(location.search).get(\"k\"); if (k) { token = k; localStorage.setItem(\"me_token\", k); }\n      base = \"/api/agent\"; return api(base + \"/state\");\n    }\n    if (!token) { location.href = \"/account#cloud\"; return; }\n    const q = new URLSearchParams(location.search);\n    let w;\n    if (q.get(\"s\") || q.get(\"ws\")) w = await api(\"/api/cloud\", {session: q.get(\"s\"), ws: q.get(\"ws\")});\n    else w = (await api(\"/api/cloud\"))[0] || await api(\"/api/cloud\", {});\n    return A.attach(w);\n  },\n  async attach(w) { base = \"/api/cloud/\" + w.id; const st = await A.state(w); st.after = w.seq || 0; return st; },\n  async state(w) {\n    if (MODE === \"local\") return api(base + \"/state\");\n    const [cfg, sessions, me] = await Promise.all([api(\"/api/doc?key=config\").then(d => JSON.parse(d.content)).catch(() => ({})), api(\"/api/sessions\"), api(\"/api/me\")]);\n    let session = null;\n    if (w && w.session) session = await api(\"/api/doc?key=\" + encodeURIComponent(\"session/\" + w.session)).then(d => JSON.parse(d.content)).catch(() => null);\n    const [ag, man] = await Promise.all([api(\"/api/agents\").catch(() => ({agents: [], current: \"code\"})),\n      w && w.ws ? api(`/api/ws/${w.ws}/manifest`).catch(() => ({files: {}})) : {files: {}}]);\n    const context = [\"AGENTS.md\", \"ME.md\", \"CLAUDE.md\", \"DESIGN.md\", \"PLAN.md\"].filter(f => f in man.files);\n    return {mode: \"cloud\", user: me.username, ctx: session?.ctx || 0, config: {model: (session?.model && (cfg.models || []).some(m => m.name === session.model)) ? session.model : cfg.model, models: cfg.models || [], prompt: cfg.prompt || \"\", autocompact: (cfg.settings || {}).autocompact ?? 80}, sessions, session, ws: w && w.ws, worker: w,\n            lang: (cfg.settings || {}).lang || \"\", behavior: {...BEHAVIOR_DEFAULTS, ...((cfg.settings || {}).no_verify ? {tests: false} : {}), ...((cfg.settings || {}).behavior || {})},\n            agent: session?.agent || ag.current, agents: ag.agents, context, perm: \"sandbox\", preview: w && w.preview, push: me.push};\n  },\n  async load(id) {\n    if (MODE === \"local\") { await api(base + \"/load\", {id}); return api(base + \"/state\"); }\n    const w = await api(\"/api/cloud\", {session: id}); return A.attach(w);\n  },\n  async newChat() {\n    if (MODE === \"cloud\") {  // the sandbox is still working on the previous chat: the new one gets its own, so both can run\n      const mine = await api(\"/api/cloud\").catch(() => []), w0 = mine.find(x => x.id === state.worker?.id);\n      if (!w0 || w0.busy) {\n        const w = await api(\"/api/cloud\", {ws: state.ws}), s = await A.attach(w);  // no session = a new sandbox\n        return {...s, session: null, ctx: 0};\n      }\n    }\n    await api(base + \"/new\", {});\n    if (MODE === \"local\") return A.state();\n    // the sandbox starts the new session asynchronously: never reload the old one through the worker's previous session id\n    const w = {...(state.worker || {}), session: null}, s = await A.state(w);\n    return {...s, session: null, ctx: 0, after};  // keep the live event cursor: older events must not replay\n  },\n  async setAgent(name) {\n    if (MODE === \"local\") return api(base + \"/agent\", {name});\n    const d = await api(\"/api/doc?key=config\"); const cfg = JSON.parse(d.content); cfg.agent = name;\n    await api(\"/api/doc\", {key: \"config\", content: JSON.stringify(cfg, null, 1)}, \"PUT\"); await api(base + \"/sync\", {});\n  },\n  async setModel(name) {\n    if (MODE === \"local\") return api(base + \"/model\", {name});\n    // the chat keeps this model (other chats keep theirs); it is also the default for new chats\n    const d = await api(\"/api/doc?key=config\"); const cfg = JSON.parse(d.content); cfg.model = name;\n    await api(\"/api/doc\", {key: \"config\", content: JSON.stringify(cfg, null, 1)}, \"PUT\"); await api(base + \"/sync\", {});\n    await api(base + \"/model\", {name}).catch(() => {});\n  },\n  prompt: (text, images) => api(base + \"/prompt\", {text, images: images || []}),\n  goal: (text) => api(base + \"/goal\", {text}),\n  undo: () => api(base + \"/undo\", {}),\n  async upload(name, b64) {  // non-image attachments go into the project so the agent can read them\n    if (MODE === \"local\") return (await api(base + \"/upload\", {name, b64})).path;\n    const path = \"uploads/\" + name.replace(/[^\\w.-]+/g, \"_\").slice(0, 80);\n    await api(`/api/ws/${state.ws}/files`, {put: [{path, b64, mtime: Date.now() / 1000}]});\n    await api(base + \"/sync\", {}).catch(() => {});\n    return path;\n  },\n  async setPerm(mode) {\n    if (MODE === \"local\") return api(base + \"/perm\", {mode});\n    const d = await api(\"/api/doc?key=config\"); const cfg = JSON.parse(d.content); cfg.settings = {...(cfg.settings || {}), perm: mode, yolo: false};\n    await api(\"/api/doc\", {key: \"config\", content: JSON.stringify(cfg, null, 1)}, \"PUT\"); await api(base + \"/sync\", {});\n  },\n  voice: (blob) => fetch(MODE === \"local\" ? \"/api/agent/voice\" : \"/api/voice\", {method: \"POST\", headers: {\"Content-Type\": blob.type, ...(token ? {Authorization: \"Bearer \" + token} : {})}, body: blob}).then(r => r.json()),\n};\n\n// \u2500\u2500\u2500 markdown \u2500\u2500\u2500\nfunction md(src) {\n  const blocks = [];\n  src = src.replace(/```(\\w*)\\n?([\\s\\S]*?)(```|$)/g, (_, lang, code) => { blocks.push([lang, code]); return `\\u0000${blocks.length - 1}\\u0000`; });\n  let h = esc(src)\n    .replace(/^### (.*)$/gm, \"<h3>$1</h3>\").replace(/^## (.*)$/gm, \"<h2>$1</h2>\").replace(/^# (.*)$/gm, \"<h1>$1</h1>\")\n    .replace(/^&gt; (.*)$/gm, \"<blockquote>$1</blockquote>\")\n    .replace(/`([^`\\n]+)`/g, \"<code>$1</code>\").replace(/\\*\\*([^*]+)\\*\\*/g, \"<b>$1</b>\").replace(/(^|\\W)\\*([^*\\n]+)\\*/g, \"$1<i>$2</i>\")\n    .replace(/\\[([^\\]]+)\\]\\((https?:[^)]+)\\)/g, '<a href=\"$2\" target=\"_blank\" rel=\"noopener\">$1</a>');\n  h = h.replace(/(?:^|\\n)((?:[-*] .*(?:\\n|$))+)/g, (_, l) => \"\\n<ul>\" + l.trim().split(\"\\n\").map(x => \"<li>\" + x.slice(2) + \"</li>\").join(\"\") + \"</ul>\\n\");\n  h = h.replace(/(?:^|\\n)((?:\\d+[.)] .*(?:\\n|$))+)/g, (_, l) => \"\\n<ol>\" + l.trim().split(\"\\n\").map(x => \"<li>\" + x.replace(/^\\d+[.)] /, \"\") + \"</li>\").join(\"\") + \"</ol>\\n\");\n  h = h.split(/\\n{2,}/).map(p => /^\\s*<(h\\d|ul|ol|blockquote|\\u0000)/.test(p) || p.startsWith(\"\\u0000\") ? p : \"<p>\" + p.replace(/\\n/g, \"<br>\") + \"</p>\").join(\"\");\n  return h.replace(/\\u0000(\\d+)\\u0000/g, (_, i) => `<pre><div class=\"bar\"><span>${esc(blocks[i][0] || \"code\")}</span><button onclick=\"copyCode(this)\">${tt(\"copy\")}</button></div><code>${esc(blocks[i][1])}</code></pre>`);\n}\n\n// \u2500\u2500\u2500 rendering \u2500\u2500\u2500\nconst log = $(\"log\"), chat = $(\"chat\");\nlet stick = true;\nchat.addEventListener(\"scroll\", () => stick = chat.scrollHeight - chat.scrollTop - chat.clientHeight < 80);\nfunction scroll() { if (stick) chat.scrollTop = chat.scrollHeight; }\nlet workEl = null;\nfunction el(html) { const d = document.createElement(\"div\"); d.innerHTML = html.trim(); const n = d.firstChild; i18nApply(d); workEl && workEl.parentNode === log ? log.insertBefore(n, workEl) : log.appendChild(n); scroll(); return n; }\nfunction clearLog() { log.innerHTML = \"\"; live = {}; cur = {}; workEl = null; }\nfunction emptyState() {\n  log.innerHTML = `<div class=\"empty\" style=\"min-height:60vh\"><div><h2>${esc(t(\"what are we\"))} <span>${esc(t(\"building?\"))}</span></h2><p>${esc(MODE === \"cloud\" ? t(\"Runs in your cloud sandbox \u00b7 files sync to all your machines\") : t(\"Running locally on this machine\"))}</p></div></div>`;\n}\nfunction userMsg(t) { if (log.querySelector(\".empty\")) log.innerHTML = \"\"; el(`<div class=\"msg user\"><div class=\"bub\">${esc(t)}</div></div>`); }\nfunction shortPath(s) {\n  s = String(s ?? \"\");\n  const roots = [state.session?.cwd, ...(state.session?.roots || []), state.cwd, \"/work\"].filter(Boolean);\n  for (const r of roots) s = s.split(r.replace(/\\/$/, \"\") + \"/\").join(\"\");\n  const max = innerWidth < 500 ? 34 : 70;\n  return s.length > max ? \"\u2026\" + s.slice(-(max - 1)) : s;\n}\nfunction toolCard(name, label, args, skill, cli) {\n  label = shortPath(label);\n  const isSkill = name === \"skill\" && args && args.action;\n  const color = cli ? (COLORS[cli.color] || (/^#/.test(cli.color || \"\") ? cli.color : COLORS.red || \"#ff6b7a\")) : isSkill ? (COLORS[skill?.color] || \"var(--a)\") : \"var(--a)\";\n  const title = cli ? `${esc(cli.icon || \"\u25a3\")} ${esc(cli.label)} \u00b7 ${esc(cli.agent)}` : isSkill ? `${esc(args.name)} \u00b7 ${esc(args.action)}` : esc(name);\n  if (cli) label = Object.entries(args || {}).map(([k, v]) => `${k}=${JSON.stringify(v)}`).join(\" \");\n  return el(`<div class=\"tool ${cli ? \"cli\" : isSkill ? \"skill\" : \"\"} ${cli ? \"\" : \"closed\"}\" style=\"--sk:${color}\"><div class=\"h\" onclick=\"this.parentNode.classList.toggle('closed')\"><span class=\"ic\"><span class=\"spin\"></span></span><b>${title}</b><span class=\"lab\">${esc(isSkill ? JSON.stringify(args.args || {}) : label)}</span><span class=\"t\"></span></div><div class=\"st hidden\"></div><div class=\"pbar ${cli ? \"ind\" : \"hidden\"}\"><i></i></div><div class=\"log hidden\"></div><div class=\"body\"></div></div>`);\n}\nfunction diffHtml(t) { return t.split(\"\\n\").map(l => `<span class=\"${l[0] === \"+\" ? \"add\" : l[0] === \"-\" ? \"del\" : \"\"}\">${esc(l)}</span>`).join(\"\\n\"); }\nfunction onEvent(e) {\n  const t = e.type;\n  if (t === \"user\") { if (!cur.echo) userMsg(e.text); cur.echo = false; return; }\n  if (t === \"turn_start\") { if (e.context) { state.context = e.context; state.agent = e.agent || state.agent; renderStatus(); } busy = true; turnT0 = Date.now(); setBusy(); return; }\n  if (t === \"reasoning\") {\n    if (!cur.think) { cur.think = el(`<details class=\"think\"><summary>${I.think}<span class=\"shimmer\">${tt(\"thinking\u2026\")}</span></summary><pre></pre></details>`); cur.thinkT = Date.now(); }\n    cur.think.querySelector(\"pre\").textContent += e.text; scroll(); return;\n  }\n  const endThink = () => { if (cur.think && !cur.thinkDone) { cur.think.querySelector(\"summary span\").outerHTML = tt(\"thought for {d}\", {d: dur((Date.now() - cur.thinkT) / 1000)}); cur.thinkDone = true; } };\n  if (t === \"text\") {\n    endThink();\n    if (!cur.text) { cur.text = el(`<div class=\"msg md\"></div>`); cur.raw = \"\"; }\n    cur.raw += e.text; const node = cur.text; node._raw = cur.raw;\n    if (!node._raf) node._raf = requestAnimationFrame(() => { node._raf = 0; node.innerHTML = md(node._raw); scroll(); });  // once per frame, not per token\n    return;\n  }\n  if (t === \"tool_start\") {\n    endThink(); cur.text = null; cur.think = null; cur.thinkDone = false;\n    live[e.id] = toolCard(e.name, e.label, e.args, e.skill, e.cli); return;\n  }\n  if (t === \"tool_event\") {\n    const c = live[e.id]; if (!c) return;\n    if (e.t === \"status\") { const s = c.querySelector(\".st\"); s.classList.remove(\"hidden\"); s.textContent = e.text; }\n    else if (e.t === \"progress\") { const p = c.querySelector(\".pbar\"); p.classList.remove(\"hidden\"); p.classList.toggle(\"ind\", e.v == null); if (e.v != null) p.firstChild.style.width = Math.round(e.v * 100) + \"%\"; }\n    else if (e.t === \"table\") {\n      const rows = e.rows || []; if (!rows.length) return;\n      const cols = Array.isArray(rows[0]) ? null : Object.keys(rows[0]);\n      const tr = r => \"<tr>\" + (cols ? cols.map(k => `<td>${esc(r[k])}</td>`) : r.map(x => `<td>${esc(x)}</td>`)).join(\"\") + \"</tr>\";\n      const p = document.createElement(\"div\"); p.className = \"panel\";\n      p.innerHTML = (e.title ? `<b>${esc(e.title)}</b>` : \"\") + `<table>${cols ? \"<tr>\" + cols.map(k => `<th>${esc(k)}</th>`).join(\"\") + \"</tr>\" : \"\"}${rows.map(tr).join(\"\")}</table>`;\n      c.insertBefore(p, c.querySelector(\".body\"));\n    }\n    else if (e.t === \"md\") { const p = document.createElement(\"div\"); p.className = \"panel md\"; p.innerHTML = md(e.text); c.insertBefore(p, c.querySelector(\".body\")); }\n    else if (e.t === \"diff\" && e.text.trim()) { c.querySelector(\".body\").innerHTML = diffHtml(e.text); c.classList.remove(\"closed\"); }\n    else { const l = c.querySelector(\".log\"); l.classList.remove(\"hidden\"); l.textContent = (\"\u203a \" + e.text + \"\\n\" + l.textContent).split(\"\\n\").slice(0, 3).join(\"\\n\"); }\n    scroll(); return;\n  }\n  if (t === \"tool_end\") {\n    const c = live[e.id]; if (!c) return; delete live[e.id];\n    c.querySelector(\".ic\").innerHTML = e.ok ? I.check : I.x; c.querySelector(\".t\").textContent = dur(e.time);\n    c.querySelectorAll(c.classList.contains(\"cli\") ? \".st,.log\" : \".st,.pbar,.log\").forEach(x => x.classList.add(\"hidden\"));\n    if (c.classList.contains(\"cli\")) { c.classList.add(\"done\", e.ok ? \"ok\" : \"fail\"); c.querySelector(\".pbar\").classList.remove(\"ind\"); c.classList.add(\"closed\"); }\n    if (c.querySelector(\".panel\")) c.classList.add(\"haspanel\");\n    const b = c.querySelector(\".body\"); if (!b.innerHTML) b.textContent = (e.output || \"\").slice(0, 20000);\n    if (!e.ok) c.classList.remove(\"closed\");\n    return;\n  }\n  if (t === \"ask\") {\n    const opts = e.options || [];\n    const c = el(`<div class=\"confirm ask\"><b>${esc(e.text)}</b><div class=\"btns\" style=\"margin-top:10px;flex-wrap:wrap\">${opts.length\n      ? opts.map((o, i) => `<button class=\"btn ${i ? \"ghost\" : \"\"}\" data-v=\"${esc(o)}\">${esc(o)}</button>`).join(\"\")\n      : `<input class=\"askin\" placeholder=\"answer\u2026\" data-i18n-ph=\"answer\u2026\"><button class=\"btn\" data-in=\"1\">${tt(\"send\")}</button>`}</div></div>`);\n    const send = v => { api(base + \"/answer\", {id: e.id, text: v}); c.innerHTML = `<span class=\"dim\">${esc(e.text)} \u2192 <b>${esc(v || \"\u2014\")}</b></span>`; c.className = \"info\"; };\n    c.querySelectorAll(\"button\").forEach(b => b.onclick = () => send(b.dataset.in ? c.querySelector(\"input\").value : b.dataset.v));\n    c.querySelector(\"input\")?.addEventListener(\"keydown\", ev => ev.key === \"Enter\" && send(ev.target.value));\n    return;\n  }\n  if (t === \"confirm\") {\n    const c = el(`<div class=\"confirm\"><b>${tt(\"Allow {name}?\", {name: e.name})}</b><pre>${esc(JSON.stringify(e.args, null, 1)).slice(0, 3000)}</pre><div class=\"btns\"><button class=\"btn\">${tt(\"allow\")}</button><button class=\"btn ghost\">${tt(e.name === \"bash\" ? \"always allow\" : \"allow all (full)\")}</button><button class=\"btn ghost\">${tt(\"deny\")}</button></div></div>`);\n    const [y, al, n] = c.querySelectorAll(\"button\");\n    y.onclick = () => { api(base + \"/confirm\", {id: e.id, ok: true}); c.remove(); };\n    al.onclick = () => { api(base + \"/confirm\", {id: e.id, ok: \"always\"}); if (e.name !== \"bash\") { state.perm = \"full\"; A.setPerm(\"full\"); renderStatus(); } c.remove(); };\n    n.onclick = () => { api(base + \"/confirm\", {id: e.id, ok: false}); c.remove(); };\n    return;\n  }\n  if (t === \"retry\") {  // the provider failed mid-answer: drop what was shown of that attempt\n    if (e.discard) { cur.text?.remove(); cur.think?.remove(); cur.text = null; cur.think = null; cur.thinkDone = false; }\n    el(`<div class=\"info warn\">\u21bb ${esc(String(e.error || \"\").split(\"\\n\")[0].slice(0, 140))}</div>`); return;\n  }\n  if (t === \"compacting\") { el(`<div class=\"info\">${tt(\"compacting history\u2026\")}</div>`); return; }\n  if (t === \"compacted\") { state.ctx = e.ctx; renderStatus(); el(`<div class=\"info\">${tt(\"compacted {a} \u2192 {b} messages \u00b7 ~{c} tokens\", {a: e.before, b: e.after, c: human(e.ctx)})}</div>`); return; }\n  if (t === \"usage\") { state.ctx = e.ctx; if (state.session) state.session.usage = {...(state.session.usage || {}), cost: e.session_cost}; if (e.day_cost != null) state.day_cost = e.day_cost; renderStatus(e.in, e.out); return; }\n  if (t === \"error\") { cur.failed = true; el(`<div class=\"info ${e.text === \"interrupted\" ? \"warn\" : \"err\"}\">${e.text === \"interrupted\" ? tt(\"interrupted\") : esc(e.text)}</div>`); return; }\n  if (t === \"turn_end\") {\n    const failed = cur.failed; endThink(); busy = false; cur = {}; setBusy();\n    const st = e.steps || 1, cp = e.cached && e.in ? ` (${Math.round(100 * e.cached / e.in)}% ${T(\"cached\")})` : \"\";\n    el(`<div class=\"stats\" title=\"${esc(T(\"every step re-sends the context; cached input costs a fraction\"))}\">${failed ? I.x : I.check}${esc(`${dur(e.time)} \u00b7 ${st} ${pl(st, \"step\", \"steps\")} \u00b7 ${T(\"context\")} ${human(e.ctx || 0)} \u00b7 \u2191${human(e.in)}${cp} \u2193${human(e.out)}${e.cost ? \" \u00b7 \" + money(e.cost) : \"\"}`)}</div>`);\n    state.ctx = e.ctx; renderStatus(); refreshSessions(); return;\n  }\n  if (t === \"todo\") { if (state.session) state.session.todo = e.items; renderTodo(e.items); return; }\n  if (t === \"goal\") { goalEvent(e); return; }\n  if (t === \"notice\") { el(`<div class=\"info warn\">${esc(e.text)}</div>`); return; }\n  if (t === \"verify\") { el(`<div class=\"info\">${tt(\"self-check: running checks before finishing\")}</div>`); return; }\n  if (t === \"turn_diff\") { diffCard(e.files || []); return; }\n  if (t === \"undone\") { el(`<div class=\"info warn\">\u21b6 ${esc(e.text)}</div>`); A.state(state.worker).then(s => { state.session = s.session; renderHistory(s.session); }); return; }\n  if (t === \"loaded\" && e.session && state.worker) state.worker.session = e.session;  // later reloads must read this session\n  if (t === \"loaded\" && e.title) $(\"title\").textContent = e.title;\n  if (t === \"reload\") { A.state(state.worker).then(s => { state.session = s.session; renderHistory(s.session); renderSessions(); }); return; }\n  if (t === \"exit\") { busy = false; setBusy(); restartSandbox(); }\n}\nfunction renderHistory(s) {\n  clearLog();\n  if (!s || !s.messages || !s.messages.length) renderTodo([]);\n  if (!s || !s.messages || !s.messages.length) return emptyState();\n  const txt = c => Array.isArray(c) ? c.filter(x => x.type === \"text\").map(x => x.text).join(\" \") : (c || \"\");\n  const outs = {};\n  s.messages.forEach(m => { if (m.role === \"tool\") outs[m.tool_call_id] = m.content; });\n  s.messages.forEach(m => {\n    if (m.role === \"user\" && m._aux) { if (/^\\[message from the user/.test(txt(m.content))) userMsg(txt(m.content).split(\"\\n\").slice(1).join(\"\\n\")); }\n    else if (m.role === \"user\" && txt(m.content) !== \"(images from read)\") userMsg(txt(m.content));\n    else if (m.role === \"assistant\") {\n      if (m.content) el(`<div class=\"msg md\">${md(m.content)}</div>`);\n      (m.tool_calls || []).forEach(tc => {\n        let a = {}; try { a = JSON.parse(tc.function.arguments); } catch (e) {}\n        const c = toolCard(tc.function.name, a.path || a.cmd || a.pattern || a.query || a.url || a.name || \"\", a);\n        c.querySelector(\".ic\").innerHTML = I.check; c.querySelector(\".body\").textContent = String(outs[tc.id] || \"\").slice(0, 8000);\n      });\n    }\n  });\n  $(\"title\").textContent = s.title || t(\"Chat\");\n  renderTodo(s.todo || []);\n  if (s.goal && s.goal.status !== \"working\") goalEvent(s.goal, true);\n  setTimeout(() => chat.scrollTop = chat.scrollHeight, 0);\n}\nfunction renderStatus(tin, tout) {\n  const m = (state.config?.models || []).find(x => x.name === state.config?.model);\n  $(\"model\").textContent = m ? m.name + (m.effort ? \" \u00b7 \" + m.effort : \"\") : t(\"no model\");\n  const ctx = m?.context || 0, used = state.ctx || 0, f = ctx ? Math.min(1, used / ctx) : 0;\n  const col = f < .6 ? \"var(--ok)\" : f < .85 ? \"var(--warn)\" : \"var(--err)\";\n  $(\"ctx\").innerHTML = ctx ? `${t(\"ctx\")} <span class=\"bar\"><i style=\"width:${(f * 100).toFixed(1)}%;background:${col}\"></i></span> ${human(used)}/${human(ctx)} ${Math.round(f * 100)}%` : `${t(\"ctx\")} ${human(used)}`;\n  const u = state.session?.usage || {};\n  $(\"tok\").textContent = tin !== undefined ? `${t(\"this task\")} \u2191${human(tin)} \u2193${human(tout)}` : `${t(\"session\")} \u2191${human(u.in)} \u2193${human(u.out)}`;\n  const ag = (state.agents || []).find(a => a.name === state.agent);\n  $(\"agentName\").textContent = ag ? ag.label : (state.agent || \"code\");\n  const cf = state.context || [];\n  $(\"ctxfiles\").innerHTML = cf.length ? `<span class=\"cf on\" title=\"${esc(t(\"loaded into every turn\"))}\">\u25c6 ${cf.map(esc).join(\" \u00b7 \")}</span>` : `<span class=\"cf\" title=\"${esc(t(\"ask the agent to create it, or /init in the terminal\"))}\">\u25c7 ${esc(t(\"no AGENTS.md\"))}</span>`;\n  const cost = state.session?.usage?.cost || 0;\n  $(\"cost\").textContent = cost || state.day_cost ? money(cost) + (state.day_cost ? \" \u00b7 \" + t(\"today\") + \" \" + money(state.day_cost) : \"\") : \"\";\n  const pm = state.perm || \"ask\";\n  $(\"perm\").textContent = {read: t(\"read-only\"), ask: t(\"ask\"), full: t(\"full access\"), sandbox: t(\"sandbox\")}[pm] || pm;\n  $(\"perm\").className = \"perm \" + pm;\n  if (state.preview) { $(\"previewBtn\").href = state.preview + \"/\"; $(\"previewBtn\").classList.remove(\"hidden\"); } else $(\"previewBtn\").classList.add(\"hidden\");\n  $(\"ws\").textContent = state.ws || (MODE === \"local\" ? (state.cwd || t(\"local\")).split(/[\\\\/]/).pop() : t(\"cloud\"));\n}\nfunction renderSessions() {\n  const ss = state.sessions || [], sid = state.session?.id;\n  const item = s => `<button class=\"si ${s.id === sid ? \"on\" : \"\"}\" onclick=\"loadSession('${esc(s.id)}')\" title=\"${esc(s.cwd || \"\")}\">${esc(s.title || t(\"(untitled)\"))}</button>`;\n  const projects = {};  // Codex-style: chats grouped by project folder, the most recently used project first\n  if (MODE === \"local\") ss.forEach(s => (projects[s.cwd || \"\"] ??= []).push(s));\n  if (MODE === \"local\" && Object.keys(projects).length) {\n    const cur = state.cwd || \"\";\n    if (cur && !projects[cur]) projects[cur] = [];\n    const order = Object.keys(projects).sort((a, b) => (a === cur ? -1 : b === cur ? 1 : 0) || Math.max(0, ...projects[b].map(x => x.updated || 0)) - Math.max(0, ...projects[a].map(x => x.updated || 0)));\n    $(\"slist\").innerHTML = order.map(p => {\n      const name = p.split(/[\\\\/]/).filter(Boolean).pop() || p || t(\"(no folder)\"), open = p === cur || projects[p].some(x => x.id === sid);\n      return `<details class=\"proj\" ${open ? \"open\" : \"\"}><summary title=\"${esc(p)}\"><svg viewBox=\"0 0 24 24\"><path d=\"M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z\"/></svg><span>${esc(name)}</span><b class=\"n\">${projects[p].length || \"\"}</b><button class=\"ib pnew\" title=\"${esc(t(\"New chat\"))}\" onclick=\"event.preventDefault();event.stopPropagation();newChatIn('${esc(p.replace(/\\\\/g, \"\\\\\\\\\").replace(/'/g, \"\\\\'\"))}')\"><svg viewBox=\"0 0 24 24\"><path d=\"M12 5v14M5 12h14\"/></svg></button></summary>${projects[p].slice(0, 40).map(item).join(\"\") || `<div class=\"lbl\">${esc(t(\"no chats yet\"))}</div>`}</details>`;\n    }).join(\"\") + `<button class=\"si addp\" onclick=\"$('wsPill').click()\">+ ${esc(t(\"open a project folder\"))}</button>`;\n    return;\n  }\n  const groups = {};\n  ss.forEach(s => { const d = new Date((s.updated || 0) * 1000), age = (Date.now() - d) / 864e5;\n    const g = age < 1 ? t(\"Today\") : age < 7 ? t(\"This week\") : t(\"Older\"); (groups[g] ??= []).push(s); });\n  $(\"slist\").innerHTML = Object.entries(groups).map(([g, l]) => `<div class=\"lbl\">${g}</div>` + l.map(item).join(\"\")).join(\"\") || `<div class=\"lbl\">${esc(t(\"no chats yet\"))}</div>`;\n}\nasync function refreshSessions() { try { const s = await A.state(state.worker); state.sessions = s.sessions; if (!state.session || state.session.id === s.session?.id) state.session = s.session || state.session; renderSessions(); } catch (e) {} }\nfunction setBusy() {\n  $(\"sendIcon\").innerHTML = busy ? I.stop : I.up;\n  $(\"send\").disabled = !busy && !$(\"input\").value.trim() && !atts.length;\n  if (busy && (!workEl || workEl.parentNode !== log)) {\n    workEl = document.createElement(\"div\"); workEl.className = \"working\";\n    workEl.innerHTML = '<span class=\"spin\"></span><span class=\"shimmer\" data-t=\"working\u2026\">'+t('working\u2026')+'</span><span class=\"el\"></span>'; log.appendChild(workEl); scroll();\n  }\n  if (!busy && workEl) { workEl.remove(); workEl = null; }\n}\nsetInterval(() => { const w = document.querySelector(\".working .el\"); if (w && busy) w.textContent = dur((Date.now() - turnT0) / 1000); }, 200);\n\n// \u2500\u2500\u2500 actions \u2500\u2500\u2500\nasync function poll() {\n  const my = ++polling;\n  while (my === polling) {\n    try {\n      const r = await api(`${base}/events?after=${after}`);\n      if (my !== polling) return;\n      for (const e of r.events) { after = Math.max(after, e.seq); onEvent(e); }\n      if (r.state) { busy = r.state.busy; setBusy(); }\n    } catch (e) {\n      if (MODE === \"cloud\" && /no such cloud session/.test(e.message)) { const w = await api(\"/api/cloud\", {session: state.session?.id, ws: state.ws}); state.worker = w; base = \"/api/cloud/\" + w.id; after = w.seq || 0; continue; }\n      await new Promise(r => setTimeout(r, 2000));\n    }\n  }\n}\nasync function restartSandbox() {\n  if (MODE !== \"cloud\") return;\n  el(`<div class=\"info warn\">${tt(\"sandbox restarted \u2014 your session and files are safe\")}</div>`);\n  try { const w = await api(\"/api/cloud\", {session: state.session?.id, ws: state.ws}); polling++; apply(await A.attach(w), false); } catch (e) {}\n}\nfunction renderChrome() {\n  $(\"mode\").textContent = t(MODE === \"cloud\" ? \"cloud\" : \"local\");\n  $(\"who\").textContent = state.user || t(MODE === \"local\" ? \"local\" : \"account\");\n}\nasync function apply(s, history = true) {\n  state = s; if (s.lang && s.lang !== LANG) setLang(s.lang);\n  if (history) renderHistory(s.session); renderSessions(); renderStatus();\n  renderChrome();\n  after = s.after ?? after;\n  if (s.busy) { busy = true; turnT0 = Date.now(); setBusy(); }\n  (s.pending || []).forEach(onEvent);\n  poll();\n}\nasync function send() {\n  let t = $(\"input\").value.trim(); if ((!t && !atts.length) || busy) return;\n  const fail = e => el(`<div class=\"info err\">${esc(e.message)}</div>`);\n  if (/^\\/[a-z0-9]/i.test(t) && !/^\\/goal\\s/.test(t) && !atts.length && await runSlash(t)) { $(\"input\").value = \"\"; grow(); return slashUpdate(); }\n  const goal = /^\\/goal\\s+/.test(t);\n  if (goal) t = t.replace(/^\\/goal\\s+/, \"\").replace(/^\"|\"$/g, \"\");\n  const images = atts.filter(a => a.image).map(a => a.url);\n  const files = [];\n  for (const a of atts.filter(a => !a.image)) { try { files.push(await A.upload(a.name, a.b64)); } catch (e) { fail(e); } }\n  if (files.length) t += (t ? \"\\n\\n\" : \"\") + \"Attached: \" + files.join(\", \");\n  if (!t && images.length) t = \"(see the attached image\" + (images.length > 1 ? \"s\" : \"\") + \")\";\n  atts = []; renderAtts();\n  $(\"input\").value = \"\"; grow(); userMsg((goal ? \"/goal \" : \"\") + t + (images.length ? `  [${images.length} ${pl(images.length, \"image\", \"images\")}]` : \"\"));\n  if (untr($(\"title\").textContent) === \"New chat\") $(\"title\").textContent = t.split(\"\\n\")[0].slice(0, 60); cur.echo = true; busy = true; turnT0 = Date.now(); setBusy(); stick = true;\n  try { await (goal ? A.goal(t) : A.prompt(t, images)); } catch (e) { fail(e); busy = false; setBusy(); }\n}\nfunction sendOrStop() { busy ? api(base + \"/cancel\", {}) : send(); }\nfunction openSide() { side.classList.add(\"open\"); $(\"scrim\").classList.add(\"on\"); }\nfunction closeSide() { side.classList.remove(\"open\"); $(\"scrim\").classList.remove(\"on\"); }\ndocument.addEventListener(\"keydown\", e => { if (e.key === \"Escape\") { closeSide(); $(\"popup\").classList.add(\"hidden\"); } });\nlet touchX = null;  // swipe left on the drawer to close it\nside.addEventListener(\"touchstart\", e => touchX = e.touches[0].clientX, {passive: true});\nside.addEventListener(\"touchend\", e => { if (touchX !== null && e.changedTouches[0].clientX - touchX < -60) closeSide(); touchX = null; });\nasync function newChatIn(cwd) {  // a new chat inside a given project folder (sidebar \"+\")\n  closeSide(); polling++;\n  try { await api(base + \"/new\", {cwd}); await apply(await api(base + \"/state\")); $(\"title\").textContent = t(\"New chat\"); } catch (e) { alert(e.message); }\n}\nasync function newChat() { closeSide(); polling++; busy = false; await apply(await A.newChat()); history.replaceState(null, \"\", location.pathname); $(\"title\").textContent = t(\"New chat\"); }\nasync function loadSession(id) { closeSide(); polling++; await apply(await A.load(id)); if (MODE === \"cloud\") history.replaceState(null, \"\", \"?s=\" + id); }\nfunction pickModel() {\n  const ms = state.config?.models || [];\n  $(\"pbody\").innerHTML = ms.map(m => `<button onclick=\"setModel('${esc(m.name)}')\"><span>${m.name === state.config.model ? \"\u25cf\" : \"\u25cb\"}</span>${esc(m.name)}<span class=\"d\">${esc(m.id)}</span></button>`).join(\"\") || `<p style=\"padding:12px;color:var(--dim)\">${esc(t(\"No models \u2014 add one in your account or with /models.\"))}</p>`;\n  $(\"popup\").classList.remove(\"hidden\");\n}\nfunction pickAgent() {\n  $(\"pbody\").innerHTML = (state.agents || []).map(a => `<button onclick=\"setAgent('${esc(a.name)}')\"><span>${a.name === state.agent ? \"\u25cf\" : \"\u25cb\"}</span><b>${esc(a.label)}</b><span class=\"d\">${esc(a.description)}</span></button>`).join(\"\")\n    + (MODE === \"cloud\" ? `<a class=\"more\" href=\"/account#market\">${esc(t(\"more agents in the marketplace \u2192\"))}</a>` : \"\");\n  $(\"popup\").classList.remove(\"hidden\");\n}\nasync function setAgent(n) { $(\"popup\").classList.add(\"hidden\"); state.agent = n; renderStatus(); await A.setAgent(n); }\nasync function setModel(n) { $(\"popup\").classList.add(\"hidden\"); await A.setModel(n); state.config.model = n; renderStatus(); }\nconst inp = $(\"input\");\nfunction grow() { inp.style.height = \"auto\"; inp.style.height = Math.min(220, inp.scrollHeight) + \"px\"; setBusy(); }\ninp.addEventListener(\"input\", grow);\ninp.addEventListener(\"keydown\", e => { if (e.key === \"Enter\" && !e.shiftKey && !e.isComposing && innerWidth > 760) { e.preventDefault(); send(); } if (e.key === \"Escape\" && busy) api(base + \"/cancel\", {}); });\n// voice: hold the mic button (or hold space in an empty input)\nlet rec = null, chunks = [];\nasync function recStart() {\n  if (rec) return;\n  try {\n    const stream = await navigator.mediaDevices.getUserMedia({audio: true});\n    rec = new MediaRecorder(stream); chunks = [];\n    rec.ondataavailable = e => chunks.push(e.data);\n    rec.onstop = async () => {\n      stream.getTracks().forEach(t => t.stop()); $(\"mic\").classList.remove(\"rec\");\n      const blob = new Blob(chunks, {type: rec.mimeType || \"audio/webm\"}); rec = null;\n      if (blob.size < 2000) return;\n      inp.placeholder = t(\"transcribing\u2026\");\n      try { const r = await A.voice(blob); if (r.text) { inp.value += (inp.value && !inp.value.endsWith(\" \") ? \" \" : \"\") + r.text; grow(); inp.focus(); } else if (r.detail) alert(r.detail); }\n      finally { inp.placeholder = t(\"Ask anything\u2026\"); }\n    };\n    rec.start(); $(\"mic\").classList.add(\"rec\");\n  } catch (e) { alert(t(\"microphone: {m}\", {m: e.message})); }\n}\nfunction recStop() { if (rec && rec.state === \"recording\") rec.stop(); }\nconst mic = $(\"mic\");\nmic.addEventListener(\"pointerdown\", e => { e.preventDefault(); recStart(); });\n[\"pointerup\", \"pointerleave\", \"pointercancel\"].forEach(ev => mic.addEventListener(ev, recStop));\nlet spaceT = 0;\ninp.addEventListener(\"keydown\", e => { if (e.code === \"Space\" && !inp.value && e.repeat) { e.preventDefault(); if (!rec) recStart(); } });\ninp.addEventListener(\"keyup\", e => { if (e.code === \"Space\") recStop(); });\n$(\"wsPill\").onclick = async () => {\n  if (MODE === \"cloud\") return location.href = \"/account#workspaces\";\n  const native = window.pywebview && window.pywebview.api && window.pywebview.api.pick_folder;  // desktop app: real folder dialog\n  const p = native ? await window.pywebview.api.pick_folder(state.cwd || \"\") : prompt(t(\"Project folder for the local agent\"), state.cwd || \"\");\n  if (!p) return;\n  try { await apply(await api(base + \"/cwd\", {path: p})); $(\"title\").textContent = t(\"New chat\"); } catch (e) { alert(e.message); }\n};\nA.boot().then(s => { if (s) { apply(s); loadThemes().catch(() => {}); } }).catch(e => el(`<div class=\"info err\">${esc(e.message)}</div>`));\n\n// \u2500\u2500\u2500 themes: same tokens as the terminal (see 14a_themes.py) \u2500\u2500\u2500\nconst THEME_VARS = {bg: \"--bg\", bg2: \"--bg2\", bg3: \"--bg3\", line: \"--line\", fg: \"--fg\", dim: \"--dim\", faint: \"--faint\", accent: \"--a\", accent2: \"--b\", ok: \"--ok\", err: \"--err\", warn: \"--warn\", user: \"--user\"};\nlet themeList = [];\nfunction onAccent(hex) {  // black or white text, whichever reads better on the accent color\n  const n = parseInt(hex.slice(1), 16), [r, g, b] = [n >> 16, (n >> 8) & 255, n & 255].map(v => { v /= 255; return v <= .03928 ? v / 12.92 : ((v + .055) / 1.055) ** 2.4; });\n  return .2126 * r + .7152 * g + .0722 * b > .35 ? \"#0a0a0a\" : \"#ffffff\";\n}\nfunction applyTheme(t) {\n  if (!t) return;\n  const r = document.documentElement;\n  for (const [k, v] of Object.entries(THEME_VARS)) if (t.colors[k]) r.style.setProperty(v, t.colors[k]);\n  r.style.colorScheme = t.dark === false ? \"light\" : \"dark\";\n  r.style.setProperty(\"--on-a\", onAccent(t.colors.accent));\n  document.querySelector('meta[name=\"theme-color\"]')?.setAttribute(\"content\", t.colors.bg);\n  try { localStorage.setItem(\"me_theme_cache\", JSON.stringify(t)); } catch (e) {}\n}\ntry { applyTheme(JSON.parse(localStorage.getItem(\"me_theme_cache\"))); } catch (e) {}\nasync function loadThemes() {\n  const r = await api(MODE === \"local\" ? \"/api/agent/themes\" : \"/api/themes\");\n  themeList = r.themes; applyTheme(themeList.find(t => t.name === r.current));\n}\nfunction pickTheme() {\n  $(\"pbody\").innerHTML = themeList.map(t => `<button onclick=\"setTheme('${esc(t.name)}')\"><span class=\"sw\" style=\"background:${t.colors.bg};border-color:${t.colors.line}\"><i style=\"background:${t.colors.accent}\"></i><i style=\"background:${t.colors.accent2}\"></i></span>${esc(t.label || t.name)}<span class=\"d\">${esc(t.dark === false ? window.t(\"light\") : window.t(\"dark\"))}</span></button>`).join(\"\")\n    + `<a class=\"more\" href=\"${MODE === \"cloud\" ? \"/account#market\" : \"#\"}\" onclick=\"${MODE === \"local\" ? \"alert(t('Install more with /theme in the terminal or the marketplace on the site'));return false\" : \"\"}\">${esc(window.t(\"more themes in the marketplace \u2192\"))}</a>`;\n  $(\"popup\").classList.remove(\"hidden\");\n}\nasync function setTheme(name) {\n  applyTheme(themeList.find(t => t.name === name)); $(\"popup\").classList.add(\"hidden\");\n  if (MODE === \"local\") return api(\"/api/agent/theme\", {name});\n  const d = await api(\"/api/doc?key=config\"); const cfg = JSON.parse(d.content); cfg.theme = name;\n  await api(\"/api/doc\", {key: \"config\", content: JSON.stringify(cfg, null, 1)}, \"PUT\");\n}\n\n// \u2500\u2500\u2500 todo checklist (sidebar card + header chip), goals, turn diff review \u2500\u2500\u2500\nfunction renderTodo(items) {\n  items = items || [];\n  const card = $(\"todoCard\"), chip = $(\"todoChip\");\n  const done = items.filter(i => i.s === \"done\").length;\n  if (!items.length) { card.classList.add(\"hidden\"); chip.classList.add(\"hidden\"); return; }\n  card.classList.remove(\"hidden\");\n  card.innerHTML = `<div class=\"tc-h\"><b>${esc(t(\"Checklist\"))}</b><span>${done}/${items.length}</span></div><div class=\"tc-bar\"><i style=\"width:${(done / items.length * 100).toFixed(0)}%\"></i></div>`\n    + items.map(i => `<div class=\"tc-i ${i.s}\"><span class=\"tc-b\">${i.s === \"done\" ? '<svg viewBox=\"0 0 24 24\"><path d=\"M5 12l5 5 9-11\"/></svg>' : i.s === \"doing\" ? '<span class=\"spin\"></span>' : \"\"}</span><span>${esc(i.t)}</span></div>`).join(\"\");\n  chip.querySelector(\"span\").textContent = `${done}/${items.length}`;\n  chip.classList.toggle(\"hidden\", done === items.length && !busy);\n}\nfunction goalEvent(g, history) {\n  if (g.status === \"working\") { if (!history) el(`<div class=\"goal working\"><span class=\"spin\"></span><span><b>${tt(\"Goal\")}</b> ${esc(g.text)}</span></div>`); return; }\n  const ok = g.status === \"done\";\n  el(`<div class=\"goal ${ok ? \"done\" : \"blocked\"}\">${ok ? I.check : I.x}<span><b>${tt(ok ? \"Goal done\" : \"Goal blocked\")}</b>${g.reason ? \" \u2014 \" + esc(g.reason) : \"\"}</span></div>`);\n}\nfunction diffCard(files) {\n  if (!files.length) return;\n  const stat = d => { let a = 0, r = 0; d.split(\"\\n\").forEach(l => { if (l[0] === \"+\" && !l.startsWith(\"+++\")) a++; else if (l[0] === \"-\" && !l.startsWith(\"---\")) r++; }); return `<span class=\"add\">+${a}</span> <span class=\"del\">\u2212${r}</span>`; };\n  const c = el(`<div class=\"review\"><div class=\"rv-h\"><b>${tt(\"Changes in this turn\")}</b><span class=\"d\">${tt(\"{n} {files}\", {n: files.length, files: pl(files.length, \"file\", \"files\")})}</span></div>${files.map(f =>\n    `<details><summary><span class=\"rv-p\">${esc(shortPath(f.path))}</span>${f.new ? `<span class=\"tag\">${tt(\"new\")}</span>` : \"\"}${stat(f.diff)}</summary><pre>${diffHtml(f.diff)}</pre></details>`).join(\"\")}\n    <div class=\"btns\"><button class=\"btn ghost\" data-a=\"keep\">${tt(\"keep\")}</button><button class=\"btn ghost danger\" data-a=\"revert\">${tt(\"revert\")}</button></div></div>`);\n  c.querySelector('[data-a=\"keep\"]').onclick = () => { c.querySelector(\".btns\").innerHTML = `<span class=\"dim\">${tt(\"kept\")}</span>`; };\n  c.querySelector('[data-a=\"revert\"]').onclick = async () => {\n    if (busy) return;\n    c.querySelector(\".btns\").innerHTML = `<span class=\"dim\">${tt(\"reverting\u2026\")}</span>`;\n    try { await A.undo(); c.querySelector(\".btns\").innerHTML = `<span class=\"dim\">${tt(\"reverted\")}</span>`; } catch (e) { c.querySelector(\".btns\").innerHTML = `<span class=\"err\">${esc(e.message)}</span>`; }\n  };\n}\n// \u2500\u2500\u2500 permissions \u2500\u2500\u2500\nfunction pickPerm() {\n  if (state.perm === \"sandbox\") { $(\"pbody\").innerHTML = `<p style=\"padding:12px;color:var(--dim)\">${esc(t(\"Cloud sessions run in an isolated sandbox, so tools run without asking. Permissions apply to the local agent\"))} (<code>me web</code> / ${esc(t(\"terminal\"))}).</p>`; return $(\"popup\").classList.remove(\"hidden\"); }\n  const P = {read: [t(\"read-only\"), t(\"no file changes; only allow-listed commands\")], ask: [t(\"ask\"), t(\"edits inside the project are free; commands ask\")], full: [t(\"full access\"), t(\"everything runs without asking\")]};\n  $(\"pbody\").innerHTML = Object.entries(P).map(([k, [l, d]]) => `<button onclick=\"setPerm('${k}')\"><span>${state.perm === k ? \"\u25cf\" : \"\u25cb\"}</span><b>${l}</b><span class=\"d\">${d}</span></button>`).join(\"\");\n  $(\"popup\").classList.remove(\"hidden\");\n}\nasync function setPerm(m) { $(\"popup\").classList.add(\"hidden\"); state.perm = m; renderStatus(); try { await A.setPerm(m); } catch (e) { alert(e.message); } }\n// \u2500\u2500\u2500 attachments: images go to the model, other files into the project \u2500\u2500\u2500\nfunction renderAtts() {\n  const box = $(\"atts\");\n  box.classList.toggle(\"hidden\", !atts.length);\n  box.innerHTML = atts.map((a, i) => `<span class=\"att\">${a.image ? `<img src=\"${a.url}\" alt=\"\">` : '<svg viewBox=\"0 0 24 24\"><path d=\"M14 3H6a1 1 0 0 0-1 1v16a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1V8z\"/><path d=\"M14 3v5h5\"/></svg>'}<span class=\"an\">${esc(a.name)}</span><button onclick=\"atts.splice(${i},1);renderAtts();setBusy()\" aria-label=\"${esc(t(\"remove\"))}\"><svg viewBox=\"0 0 24 24\"><path d=\"M6 6l12 12M18 6L6 18\"/></svg></button></span>`).join(\"\");\n}\nfunction shrink(dataUrl, max = 1600) {  // phone photos are huge; models need ~1.5k px at most\n  return new Promise(res => { const img = new Image(); img.onload = () => {\n    const k = Math.min(1, max / Math.max(img.width, img.height)); if (k === 1 && dataUrl.length < 1.5e6) return res(dataUrl);\n    const c = document.createElement(\"canvas\"); c.width = Math.round(img.width * k); c.height = Math.round(img.height * k);\n    c.getContext(\"2d\").drawImage(img, 0, 0, c.width, c.height); res(c.toDataURL(\"image/jpeg\", .85)); }; img.onerror = () => res(dataUrl); img.src = dataUrl; });\n}\nasync function addFiles(list) {\n  for (const f of list) {\n    if (f.size > 20e6) { alert(t(\"{name}: over 20 MB\", {name: f.name})); continue; }\n    const url = await new Promise(r => { const fr = new FileReader(); fr.onload = () => r(fr.result); fr.readAsDataURL(f); });\n    if (f.type.startsWith(\"image/\")) atts.push({name: f.name || \"image.png\", image: true, url: await shrink(url)});\n    else atts.push({name: f.name, b64: url.split(\",\")[1]});\n  }\n  renderAtts(); setBusy();\n}\n$(\"file\").addEventListener(\"change\", e => { addFiles([...e.target.files]); e.target.value = \"\"; });\n$(\"input\").addEventListener(\"paste\", e => {  // screenshots come as clipboard items in some browsers, not as files\n  const cd = e.clipboardData; let fs = [...(cd?.files || [])];\n  if (!fs.length) fs = [...(cd?.items || [])].filter(i => i.kind === \"file\").map(i => i.getAsFile()).filter(Boolean);\n  if (fs.length) { e.preventDefault(); addFiles(fs); }\n});\ndocument.addEventListener(\"dragover\", e => e.preventDefault());\ndocument.addEventListener(\"drop\", e => { if (e.dataTransfer?.files?.length) { e.preventDefault(); addFiles([...e.dataTransfer.files]); } });\n// \u2500\u2500\u2500 PWA + push (\"done\", \"waiting for you\") \u2500\u2500\u2500\nif (\"serviceWorker\" in navigator && location.protocol !== \"file:\") navigator.serviceWorker.register(\"/sw.js\").then(() => {\n  if (MODE === \"cloud\" && \"PushManager\" in window && Notification.permission !== \"granted\") $(\"bell\").classList.remove(\"hidden\");\n}).catch(() => {});\nasync function enablePush() {\n  try {\n    if (await Notification.requestPermission() !== \"granted\") return alert(t(\"Notifications are blocked for this site \u2014 allow them in the browser settings.\"));\n    const {key} = await api(\"/api/push/key\");\n    const reg = await navigator.serviceWorker.ready;\n    const b = atob(key.replace(/-/g, \"+\").replace(/_/g, \"/\") + \"=\".repeat((4 - key.length % 4) % 4));\n    const sub = await reg.pushManager.subscribe({userVisibleOnly: true, applicationServerKey: Uint8Array.from(b, c => c.charCodeAt(0))});\n    await api(\"/api/push/subscribe\", sub.toJSON());\n    $(\"bell\").classList.add(\"hidden\");\n    el(`<div class=\"info\">${tt(\"notifications on \u2014 you will hear when a task is done or needs you\")}</div>`);\n  } catch (e) { alert(t(\"notifications: {m}\", {m: e.message})); }\n}\n\nsetLang(LANG);\n\n// \u2500\u2500\u2500 project files (local agent): browse folders, read files \u2500\u2500\u2500\nasync function openFiles(dir) {\n  if (MODE !== \"local\") return location.href = \"/account#workspaces\";\n  let r; try { r = await api(base + \"/files\", {path: dir}); } catch (e) { return alert(e.message); }\n  const up = dir ? dir.split(\"/\").slice(0, -1).join(\"/\") : null;\n  const size = n => n < 1024 ? n + \" B\" : n < 1048576 ? (n / 1024).toFixed(1) + \" KB\" : (n / 1048576).toFixed(1) + \" MB\";\n  $(\"pbody\").innerHTML = `<div class=\"fhead\"><b>${esc((state.cwd || \"\").split(/[\\\\/]/).pop())}</b><span class=\"d\">/${esc(dir)}</span></div>`\n    + (up !== null ? `<button data-d=\"${esc(up)}\"><span>\u21b0</span>..</button>` : \"\")\n    + r.items.map(i => i.dir ? `<button data-d=\"${esc((dir ? dir + \"/\" : \"\") + i.name)}\"><svg viewBox=\"0 0 24 24\"><path d=\"M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z\"/></svg>${esc(i.name)}</button>`\n      : `<button data-f=\"${esc((dir ? dir + \"/\" : \"\") + i.name)}\"><svg viewBox=\"0 0 24 24\"><path d=\"M14 3H6a1 1 0 0 0-1 1v16a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1V8z\"/></svg>${esc(i.name)}<span class=\"d\">${size(i.size)}</span></button>`).join(\"\")\n    + (r.items.length ? \"\" : `<p class=\"dim\" style=\"padding:12px\">${esc(t(\"empty folder\"))}</p>`);\n  $(\"pbody\").querySelectorAll(\"[data-d]\").forEach(b => b.onclick = () => openFiles(b.dataset.d));\n  $(\"pbody\").querySelectorAll(\"[data-f]\").forEach(b => b.onclick = () => viewFile(b.dataset.f));\n  $(\"popup\").classList.remove(\"hidden\");\n}\nasync function viewFile(path) {\n  let r; try { r = await api(base + \"/file\", {path}); } catch (e) { return alert(e.message); }\n  const dir = path.split(\"/\").slice(0, -1).join(\"/\");\n  $(\"pbody\").innerHTML = `<div class=\"fhead\"><button class=\"ib\" id=\"fback\" title=\"${esc(t(\"back\"))}\">\u21b0</button><b>${esc(path)}</b><button class=\"ib\" id=\"fask\" title=\"${esc(t(\"ask about this file\"))}\"><svg viewBox=\"0 0 24 24\"><path d=\"M4 5h16v11H8l-4 4z\"/></svg></button></div>`\n    + (r.note ? `<p class=\"dim\" style=\"padding:12px\">${esc(r.note)}</p>` : `<pre class=\"fview\">${r.text.split(\"\\n\").slice(0, 5000).map((l, i) => `<i>${i + 1}</i>${esc(l)}`).join(\"\\n\")}</pre>`);\n  $(\"fback\").onclick = () => openFiles(dir);\n  $(\"fask\").onclick = () => { $(\"popup\").classList.add(\"hidden\"); inp.value = (inp.value ? inp.value + \" \" : \"\") + \"@\" + path + \" \"; grow(); inp.focus(); };\n}\n\n// \u2500\u2500\u2500 slash commands: the same set in the local web UI and in the cloud \u2500\u2500\u2500\nObject.assign(RU, {\n  \"show commands & keys\": \"\u043a\u043e\u043c\u0430\u043d\u0434\u044b \u0438 \u043a\u043b\u0430\u0432\u0438\u0448\u0438\", \"switch agent\": \"\u0441\u043c\u0435\u043d\u0438\u0442\u044c \u0430\u0433\u0435\u043d\u0442\u0430\", \"create or update AGENTS.md\": \"\u0441\u043e\u0437\u0434\u0430\u0442\u044c \u0438\u043b\u0438 \u043e\u0431\u043d\u043e\u0432\u0438\u0442\u044c AGENTS.md\", \"switch model\": \"\u0441\u043c\u0435\u043d\u0438\u0442\u044c \u043c\u043e\u0434\u0435\u043b\u044c\",\n  \"reasoning effort\": \"\u0443\u0440\u043e\u0432\u0435\u043d\u044c \u0440\u0430\u0441\u0441\u0443\u0436\u0434\u0435\u043d\u0438\u0439\", \"edit your system prompt\": \"\u0441\u0438\u0441\u0442\u0435\u043c\u043d\u044b\u0439 \u043f\u0440\u043e\u043c\u043f\u0442\", \"list / toggle skills\": \"\u043d\u0430\u0432\u044b\u043a\u0438: \u0441\u043f\u0438\u0441\u043e\u043a \u0438 \u043f\u0435\u0440\u0435\u043a\u043b\u044e\u0447\u0430\u0442\u0435\u043b\u0438\",\n  \"new chat\": \"\u043d\u043e\u0432\u044b\u0439 \u0447\u0430\u0442\", \"past chats\": \"\u043f\u0440\u043e\u0448\u043b\u044b\u0435 \u0447\u0430\u0442\u044b\", \"copy last answer\": \"\u0441\u043a\u043e\u043f\u0438\u0440\u043e\u0432\u0430\u0442\u044c \u043f\u043e\u0441\u043b\u0435\u0434\u043d\u0438\u0439 \u043e\u0442\u0432\u0435\u0442\", \"summarize history to free context\": \"\u0441\u0436\u0430\u0442\u044c \u0438\u0441\u0442\u043e\u0440\u0438\u044e\",\n  \"auto-compact threshold\": \"\u043f\u043e\u0440\u043e\u0433 \u0430\u0432\u0442\u043e\u0441\u0436\u0430\u0442\u0438\u044f\", \"work autonomously until done\": \"\u0440\u0430\u0431\u043e\u0442\u0430\u0442\u044c \u0430\u0432\u0442\u043e\u043d\u043e\u043c\u043d\u043e \u0434\u043e \u0440\u0435\u0437\u0443\u043b\u044c\u0442\u0430\u0442\u0430\", \"revert the last turn's files\": \"\u043e\u0442\u043a\u0430\u0442\u0438\u0442\u044c \u0444\u0430\u0439\u043b\u044b \u043f\u043e\u0441\u043b\u0435\u0434\u043d\u0435\u0433\u043e \u0445\u043e\u0434\u0430\",\n  \"permissions\": \"\u0440\u0430\u0437\u0440\u0435\u0448\u0435\u043d\u0438\u044f\", \"spending\": \"\u0440\u0430\u0441\u0445\u043e\u0434\u044b\", \"the agent's checklist\": \"\u0447\u0435\u043a-\u043b\u0438\u0441\u0442 \u0430\u0433\u0435\u043d\u0442\u0430\", \"full access on/off\": \"\u043f\u043e\u043b\u043d\u044b\u0439 \u0434\u043e\u0441\u0442\u0443\u043f \u0432\u043a\u043b/\u0432\u044b\u043a\u043b\", \"sync now\": \"\u0441\u0438\u043d\u0445\u0440\u043e\u043d\u0438\u0437\u0438\u0440\u043e\u0432\u0430\u0442\u044c\",\n  \"color theme\": \"\u0446\u0432\u0435\u0442\u043e\u0432\u0430\u044f \u0442\u0435\u043c\u0430\", \"project folder / workspaces\": \"\u043f\u0430\u043f\u043a\u0430 \u043f\u0440\u043e\u0435\u043a\u0442\u0430 / \u0440\u0430\u0431\u043e\u0447\u0438\u0435 \u043e\u0431\u043b\u0430\u0441\u0442\u0438\", \"open on the site\": \"\u043e\u0442\u043a\u0440\u044b\u0442\u044c \u043d\u0430 \u0441\u0430\u0439\u0442\u0435\", \"do this in the terminal\": \"\u044d\u0442\u043e \u0434\u0435\u043b\u0430\u0435\u0442\u0441\u044f \u0432 \u0442\u0435\u0440\u043c\u0438\u043d\u0430\u043b\u0435\",\n  \"unknown command\": \"\u043d\u0435\u0438\u0437\u0432\u0435\u0441\u0442\u043d\u0430\u044f \u043a\u043e\u043c\u0430\u043d\u0434\u0430\", \"Commands\": \"\u041a\u043e\u043c\u0430\u043d\u0434\u044b\", \"no skills yet\": \"\u043d\u0430\u0432\u044b\u043a\u043e\u0432 \u043f\u043e\u043a\u0430 \u043d\u0435\u0442\", \"copied\": \"\u0441\u043a\u043e\u043f\u0438\u0440\u043e\u0432\u0430\u043d\u043e\", \"copy failed\": \"\u043d\u0435 \u0443\u0434\u0430\u043b\u043e\u0441\u044c \u0441\u043a\u043e\u043f\u0438\u0440\u043e\u0432\u0430\u0442\u044c\", \"nothing to copy\": \"\u043d\u0435\u0447\u0435\u0433\u043e \u043a\u043e\u043f\u0438\u0440\u043e\u0432\u0430\u0442\u044c\",\n  \"saved\": \"\u0441\u043e\u0445\u0440\u0430\u043d\u0435\u043d\u043e\", \"nothing to retry\": \"\u043d\u0435\u0447\u0435\u0433\u043e \u043f\u043e\u0432\u0442\u043e\u0440\u044f\u0442\u044c\", \"take back the last request and run it again\": \"\u043e\u0442\u043c\u0435\u043d\u0438\u0442\u044c \u043f\u043e\u0441\u043b\u0435\u0434\u043d\u0438\u0439 \u0437\u0430\u043f\u0440\u043e\u0441 \u0438 \u0432\u044b\u043f\u043e\u043b\u043d\u0438\u0442\u044c \u0437\u0430\u043d\u043e\u0432\u043e\",\n  \"save this chat as Markdown\": \"\u0441\u043e\u0445\u0440\u0430\u043d\u0438\u0442\u044c \u0447\u0430\u0442 \u0432 Markdown\", \"switches: self-testing, todo, web, plan first, brief\u2026\": \"\u043f\u0435\u0440\u0435\u043a\u043b\u044e\u0447\u0430\u0442\u0435\u043b\u0438: \u0441\u0430\u043c\u043e\u043f\u0440\u043e\u0432\u0435\u0440\u043a\u0430, todo, \u0432\u0435\u0431, \u0441\u043d\u0430\u0447\u0430\u043b\u0430 \u043f\u043b\u0430\u043d, \u043a\u0440\u0430\u0442\u043a\u043e\u2026\",\n  \"verify changes itself (tests, build, run)\": \"\u0441\u0430\u043c \u043f\u0440\u043e\u0432\u0435\u0440\u044f\u0435\u0442 \u0438\u0437\u043c\u0435\u043d\u0435\u043d\u0438\u044f (\u0442\u0435\u0441\u0442\u044b, \u0441\u0431\u043e\u0440\u043a\u0430, \u0437\u0430\u043f\u0443\u0441\u043a)\", \"keep a todo checklist on multi-step tasks\": \"\u0432\u0435\u0434\u0451\u0442 todo-\u0441\u043f\u0438\u0441\u043e\u043a \u0432 \u043c\u043d\u043e\u0433\u043e\u0448\u0430\u0433\u043e\u0432\u044b\u0445 \u0437\u0430\u0434\u0430\u0447\u0430\u0445\",\n  \"search the web for current facts and docs\": \"\u0438\u0449\u0435\u0442 \u0432 \u0438\u043d\u0442\u0435\u0440\u043d\u0435\u0442\u0435 \u0430\u043a\u0442\u0443\u0430\u043b\u044c\u043d\u044b\u0435 \u0444\u0430\u043a\u0442\u044b \u0438 \u0434\u043e\u043a\u0438\", \"stay in the project folder unless asked\": \"\u043d\u0435 \u0432\u044b\u0445\u043e\u0434\u0438\u0442 \u0437\u0430 \u043f\u0430\u043f\u043a\u0443 \u043f\u0440\u043e\u0435\u043a\u0442\u0430 \u0431\u0435\u0437 \u043f\u0440\u043e\u0441\u044c\u0431\u044b\", \"show a plan and wait for a go before big changes\": \"\u043f\u0435\u0440\u0435\u0434 \u0431\u043e\u043b\u044c\u0448\u0438\u043c\u0438 \u0438\u0437\u043c\u0435\u043d\u0435\u043d\u0438\u044f\u043c\u0438 \u043f\u043e\u043a\u0430\u0437\u044b\u0432\u0430\u0435\u0442 \u043f\u043b\u0430\u043d \u0438 \u0436\u0434\u0451\u0442 \u00ab\u0434\u0430\u00bb\",\n  \"very short replies\": \"\u043e\u0447\u0435\u043d\u044c \u043a\u043e\u0440\u043e\u0442\u043a\u0438\u0435 \u043e\u0442\u0432\u0435\u0442\u044b\", \"explain the why (learning mode)\": \"\u043e\u0431\u044a\u044f\u0441\u043d\u044f\u0435\u0442 \u00ab\u043f\u043e\u0447\u0435\u043c\u0443\u00bb (\u0440\u0435\u0436\u0438\u043c \u043e\u0431\u0443\u0447\u0435\u043d\u0438\u044f)\", \"git commit each finished task\": \"git-\u043a\u043e\u043c\u043c\u0438\u0442 \u043f\u043e\u0441\u043b\u0435 \u043a\u0430\u0436\u0434\u043e\u0439 \u0437\u0430\u0434\u0430\u0447\u0438\",\n  \"offer to create AGENTS.md in new projects\": \"\u043f\u0440\u0435\u0434\u043b\u0430\u0433\u0430\u0435\u0442 \u0441\u043e\u0437\u0434\u0430\u0442\u044c AGENTS.md \u0432 \u043d\u043e\u0432\u044b\u0445 \u043f\u0440\u043e\u0435\u043a\u0442\u0430\u0445\", \"System prompt (empty = default)\": \"\u0421\u0438\u0441\u0442\u0435\u043c\u043d\u044b\u0439 \u043f\u0440\u043e\u043c\u043f\u0442 (\u043f\u0443\u0441\u0442\u043e = \u043f\u043e \u0443\u043c\u043e\u043b\u0447\u0430\u043d\u0438\u044e)\", \"Spending\": \"\u0420\u0430\u0441\u0445\u043e\u0434\u044b\", \"session\": \"\u0441\u0435\u0441\u0441\u0438\u044f\", \"today\": \"\u0441\u0435\u0433\u043e\u0434\u043d\u044f\",\n});\nconst EFFORTS = [\"none\", \"minimal\", \"low\", \"medium\", \"high\", \"xhigh\"];\nconst note = (txt, cls = \"\") => el(`<div class=\"info ${cls}\">${esc(txt)}</div>`);\nconst say = txt => el(`<div class=\"msg md\">${md(txt)}</div>`);\nconst BEHAVIOR_LABELS = {tests: \"verify changes itself (tests, build, run)\", todo: \"keep a todo checklist on multi-step tasks\", web: \"search the web for current facts and docs\", stay_in_project: \"stay in the project folder unless asked\",\n  plan_first: \"show a plan and wait for a go before big changes\", brief: \"very short replies\", explain: \"explain the why (learning mode)\",\n  commit: \"git commit each finished task\", agents_md: \"offer to create AGENTS.md in new projects\"};\nconst BEHAVIOR_DEFAULTS = {tests: true, todo: true, web: true, stay_in_project: true, plan_first: false, brief: false, explain: false, commit: false, agents_md: true};\nfunction applySetting(cfg, key, v, model) {  // mirror of apply_setting() in the agent, for the cloud config doc\n  cfg.settings = cfg.settings || {};\n  if (key === \"lang\") { cfg.settings.lang = [\"en\", \"ru\"].includes(v) ? v : \"\"; return; }\n  if (key.startsWith(\"behavior:\")) { cfg.settings.behavior = {...(cfg.settings.behavior || {}), [key.slice(9)]: !!v}; if (key === \"behavior:tests\") delete cfg.settings.no_verify; return; }\n  if (key === \"prompt\") cfg.prompt = String(v || \"\");\n  else if (key === \"autocompact\") cfg.settings.autocompact = (v === \"off\" || v === 0 || v == null) ? 0 : Math.max(10, Math.min(95, parseInt(v)));\n  else if (key === \"effort\") { const m = (cfg.models || []).find(x => x.name === (model || cfg.model)); if (!m) throw new Error(\"no model\"); m.effort = (v === \"none\" || !v) ? \"\" : String(v); }\n  else if (key.startsWith(\"skill:\")) { const d = new Set(cfg.settings.disabled_skills || []); v ? d.delete(key.slice(6)) : d.add(key.slice(6)); cfg.settings.disabled_skills = [...d].sort(); }\n  else throw new Error(\"unknown setting \" + key);\n}\nasync function cfgSet(key, value) {\n  if (MODE === \"local\") { const s = await api(base + \"/config\", {key, value}); state.config = s.config; return; }\n  const d = await api(\"/api/doc?key=config\"), cfg = JSON.parse(d.content);\n  applySetting(cfg, key, value, state.config?.model);\n  await api(\"/api/doc\", {key: \"config\", content: JSON.stringify(cfg, null, 1)}, \"PUT\"); await api(base + \"/sync\", {}).catch(() => {});\n  state.config = (await A.state(state.worker)).config;\n}\nfunction popupList(rows, onPick, extra = \"\") {  // rows: [value, label, desc, on]\n  $(\"pbody\").innerHTML = rows.map(([v, l, d, on], i) => `<button data-i=\"${i}\"><span>${on ? \"\u25cf\" : \"\u25cb\"}</span><b>${esc(l)}</b><span class=\"d\">${esc(d || \"\")}</span></button>`).join(\"\") + extra;\n  $(\"pbody\").querySelectorAll(\"button\").forEach(b => b.onclick = () => { $(\"popup\").classList.add(\"hidden\"); onPick(rows[+b.dataset.i][0]); });\n  $(\"popup\").classList.remove(\"hidden\");\n}\nconst site = (pane, what) => MODE === \"cloud\" ? (location.href = \"/account#\" + pane) : note(`/${what} \u2014 ${t(\"do this in the terminal\")}`, \"warn\");\nconst lastAnswer = () => { const m = [...log.querySelectorAll(\".msg.md\")].pop(); return m ? m.innerText : \"\"; };\n\nconst CMDS = {\n  help: [\"show commands & keys\", \"\", () => say(\"## \" + t(\"Commands\") + \"\\n\" + Object.entries(CMDS).map(([n, c]) => `- \\`/${n}${c[1] ? \" \" + c[1] : \"\"}\\` ${t(c[0])}`).join(\"\\n\")\n    + \"\\n\\n`enter` send \u00b7 `shift+enter` newline \u00b7 `esc` stop \u00b7 `/` commands\")],\n  agent: [\"switch agent\", \"[name]\", a => { const x = (state.agents || []).find(g => g.name === a); x ? setAgent(x.name) : (a ? note(t(\"unknown command\") + \": \" + a, \"err\") : pickAgent()); }],\n  init: [\"create or update AGENTS.md\", \"\", () => A.prompt(\"Create or update AGENTS.md for this project. Explore it first (structure, manifests, scripts, README, tests). Write a concise file: purpose; stack; exact verified commands to install/run/test/build; folder map; conventions; gotchas. It is loaded into every session, so keep it short and accurate.\").then(() => { userMsg(\"/init\"); busy = true; turnT0 = Date.now(); setBusy(); cur.echo = true; })],\n  model: [\"switch model\", \"[name]\", a => { const m = (state.config?.models || []).find(x => x.name === a); m ? setModel(m.name) : pickModel(); }],\n  effort: [\"reasoning effort\", \"[none\u2026xhigh]\", async a => {\n    const go = async v => { await cfgSet(\"effort\", v); renderStatus(); note(\"effort \u2192 \" + v, \"ok\"); };\n    EFFORTS.includes(a) ? go(a) : popupList(EFFORTS.map(e => [e, e, \"\", e === ((state.config?.models || []).find(m => m.name === state.config.model)?.effort || \"none\")]), go);\n  }],\n  prompt: [\"edit your system prompt\", \"\", async () => {\n    const v = prompt(t(\"System prompt (empty = default)\"), state.config?.prompt || \"\"); if (v === null) return;\n    await cfgSet(\"prompt\", v); note(t(\"saved\"), \"ok\");\n  }],\n  skills: [\"list / toggle skills\", \"\", async () => {\n    let list;\n    if (MODE === \"local\") list = (await api(base + \"/skills\")).skills;\n    else { const dis = new Set((JSON.parse((await api(\"/api/doc?key=config\")).content).settings || {}).disabled_skills || []);\n      list = (await api(\"/api/docs\")).filter(d => d.key.startsWith(\"skill/\")).map(d => d.key.slice(6).replace(/\\.(py|md)$/, \"\")).map(n => ({name: n, description: \"\", on: !dis.has(n)})); }\n    if (!list.length) return note(t(\"no skills yet\"));\n    popupList(list.map(s => [s, s.name, s.description, s.on]), async s => { await cfgSet(\"skill:\" + s.name, !s.on); note(`${s.name} ${s.on ? \"off\" : \"on\"}`, \"ok\"); });\n  }],\n  plugins: [\"extensions that modify me\", \"\", () => site(\"plugins\", \"plugins\")],\n  models: [\"add / edit models\", \"\", () => site(\"models\", \"models\")],\n  provider: [\"API providers\", \"\", () => site(\"models\", \"provider\")],\n  secret: [\"env secrets for skills\", \"\", () => site(\"models\", \"secret\")],\n  mcp: [\"MCP servers\", \"\", () => site(\"models\", \"mcp\")],\n  new: [\"new chat\", \"\", () => newChat()],\n  clear: [\"new chat\", \"\", () => newChat()],\n  sessions: [\"past chats\", \"\", () => openSide()],\n  workspace: [\"project folder / workspaces\", \"\", () => $(\"wsPill\").click()],\n  copy: [\"copy last answer\", \"\", async () => { const x = lastAnswer(); if (!x) return note(t(\"nothing to copy\"), \"warn\"); const ok = await copyText(x); note(t(ok ? \"copied\" : \"copy failed\"), ok ? \"ok\" : \"err\"); }],\n  compact: [\"summarize history to free context\", \"\", () => api(base + \"/compact\", {})],\n  autocompact: [\"auto-compact threshold\", \"[50\u202695|off]\", async a => {\n    const go = async v => { await cfgSet(\"autocompact\", v); note(\"auto-compact \u2192 \" + (v === \"off\" ? \"off\" : v + \"%\"), \"ok\"); };\n    a ? go(a) : popupList([\"50\", \"70\", \"80\", \"90\", \"off\"].map(v => [v, v === \"off\" ? v : v + \"%\", \"\", String(state.config?.autocompact ?? 80) === v]), go);\n  }],\n  goal: [\"work autonomously until done\", \"<objective>\", a => a ? (inp.value = \"/goal \" + a, send()) : note(\"/goal <objective>\", \"warn\")],\n  undo: [\"revert the last turn's files\", \"\", () => A.undo().then(() => 0)],\n  retry: [\"take back the last request and run it again\", \"\", async () => {\n    const ms = state.session?.messages || [], i = ms.map(m => m.role === \"user\" && !m._aux).lastIndexOf(true);\n    if (i < 0) return note(t(\"nothing to retry\"), \"warn\");\n    const c = ms[i].content, text = Array.isArray(c) ? c.filter(x => x.type === \"text\").map(x => x.text).join(\" \") : c;\n    await api(base + \"/retry\", {});\n    state.session = {...state.session, messages: ms.slice(0, i)}; renderHistory(state.session);\n    inp.value = text; send();\n  }],\n  export: [\"save this chat as Markdown\", \"\", () => {\n    const s = state.session; if (!s?.messages?.length) return note(t(\"nothing to copy\"), \"warn\");\n    const txt = c => Array.isArray(c) ? c.filter(x => x.type === \"text\").map(x => x.text).join(\" \") : (c || \"\");\n    const out = [\"# \" + (s.title || \"chat\"), \"\"];\n    for (const m of s.messages) {\n      if (m.role === \"user\" && !m._aux) out.push(\"## You\", \"\", txt(m.content), \"\");\n      else if (m.role === \"assistant\") { if (m.content) out.push(\"## me\", \"\", txt(m.content), \"\"); (m.tool_calls || []).forEach(tc => out.push(\"- `\" + tc.function.name + \"` \" + tc.function.arguments.slice(0, 120))); }\n    }\n    const a = document.createElement(\"a\"); a.href = URL.createObjectURL(new Blob([out.join(\"\\n\")], {type: \"text/markdown\"}));\n    a.download = \"me-chat-\" + (s.id || \"chat\") + \".md\"; a.click(); setTimeout(() => URL.revokeObjectURL(a.href), 1000);\n  }],\n  behavior: [\"switches: self-testing, todo, web, plan first, brief\u2026\", \"[name on|off]\", async a => {\n    const B = state.behavior || {}, [k, v] = (a || \"\").split(/\\s+/);\n    const go = async (key, on) => { await cfgSet(\"behavior:\" + key, on); state.behavior = {...(state.behavior || {}), [key]: on}; note(`${key} ${on ? \"on\" : \"off\"}`, \"ok\"); };\n    if (k && k in B) return go(k, v ? /^(on|1|true|yes)$/.test(v) : !B[k]);\n    const show = () => popupList(Object.keys(B).map(n => [n, t(BEHAVIOR_LABELS[n] || n), n, state.behavior[n]]), async n => { await go(n, !state.behavior[n]); show(); });\n    show();\n  }],\n  perm: [\"permissions\", \"[read|ask|full]\", a => [\"read\", \"ask\", \"full\"].includes(a) ? setPerm(a) : pickPerm()],\n  yolo: [\"full access on/off\", \"\", () => setPerm(state.perm === \"full\" ? \"ask\" : \"full\")],\n  cost: [\"spending\", \"\", () => {\n    const u = state.session?.usage || {};\n    say(`## ${t(\"Spending\")}\\n- ${t(\"session\")}: **${money(u.cost || 0)}** \u00b7 \u2191${human(u.in || 0)} \u2193${human(u.out || 0)}\\n- ${t(\"today\")}: **${money(state.day_cost || 0)}**`);\n  }],\n  todo: [\"the agent's checklist\", \"\", () => { const it = state.session?.todo || []; it.length ? say(it.map(i => `- [${i.s === \"done\" ? \"x\" : \" \"}] ${i.t}`).join(\"\\n\")) : note(\"\u2014\"); }],\n  theme: [\"color theme\", \"[name]\", a => { const x = themeList.find(th => th.name === a); x ? setTheme(x.name) : pickTheme(); }],\n  sync: [\"sync now\", \"\", () => MODE === \"cloud\" ? api(base + \"/sync\", {}).then(() => note(t(\"saved\"), \"ok\")) : note(\"local\", \"warn\")],\n  login: [\"log in / register\", \"\", () => MODE === \"cloud\" ? note(state.user || \"\") : note(\"/login \u2014 \" + t(\"do this in the terminal\"), \"warn\")],\n  logout: [\"log out\", \"\", () => MODE === \"cloud\" ? (localStorage.removeItem(\"me_token\"), location.href = \"/account\") : note(\"/logout \u2014 \" + t(\"do this in the terminal\"), \"warn\")],\n  \"2fa\": [\"authenticator (TOTP)\", \"\", () => site(\"security\", \"2fa\")],\n  config: [\"settings\", \"\", () => site(\"overview\", \"config\")],\n  update: [\"update me\", \"\", () => MODE === \"cloud\" ? note(\"cloud: always current\", \"ok\") : note(\"/update \u2014 \" + t(\"do this in the terminal\"), \"warn\")],\n  exit: [\"close\", \"\", () => MODE === \"cloud\" ? (location.href = \"/account\") : note(t(\"close this tab\"), \"warn\")],\n};\nCMDS.quit = CMDS.exit;\nfunction matchCmd(name) {\n  if (CMDS[name]) return name;\n  const c = Object.keys(CMDS).filter(k => k.startsWith(name));\n  return c.length === 1 ? c[0] : null;\n}\nasync function runSlash(text) {\n  const m = text.match(/^\\/([a-z0-9]+)(?:\\s+([\\s\\S]*))?$/i);\n  if (!m) return false;                       // \"/etc/hosts?\" and the like are ordinary messages\n  const name = matchCmd(m[1].toLowerCase());\n  if (!name) { note(`${t(\"unknown command\")}: /${m[1]} \u2014 /help`, \"err\"); return true; }\n  try { await CMDS[name][2]((m[2] || \"\").trim()); } catch (e) { note(e.message, \"err\"); }\n  return true;\n}\n// \u2500\u2500\u2500 \"/\" menu above the input \u2500\u2500\u2500\nconst slashEl = document.createElement(\"div\"); slashEl.id = \"slash\"; slashEl.className = \"slash hidden\"; $(\"input\").parentNode.insertBefore(slashEl, $(\"input\"));\nlet slashSel = 0, slashRows = [];\nfunction slashUpdate() {\n  const v = inp.value, m = v.match(/^\\/([a-z0-9]*)$/i);\n  if (!m) { slashEl.classList.add(\"hidden\"); slashRows = []; return; }\n  slashRows = Object.keys(CMDS).filter(k => k !== \"quit\" && k !== \"clear\" && k.startsWith(m[1].toLowerCase()));\n  slashSel = Math.min(slashSel, Math.max(0, slashRows.length - 1));\n  slashEl.classList.toggle(\"hidden\", !slashRows.length);\n  slashEl.innerHTML = slashRows.map((k, i) => `<button type=\"button\" class=\"${i === slashSel ? \"on\" : \"\"}\" data-k=\"${k}\"><b>/${k}</b><span class=\"a\">${esc(CMDS[k][1])}</span><span class=\"d\">${esc(t(CMDS[k][0]))}</span></button>`).join(\"\");\n  slashEl.querySelectorAll(\"button\").forEach(b => b.onmousedown = e => { e.preventDefault(); slashPick(b.dataset.k); });\n}\nfunction slashPick(k) { inp.value = \"/\" + k + (CMDS[k][1] ? \" \" : \"\"); grow(); slashEl.classList.add(\"hidden\"); inp.focus(); if (!CMDS[k][1]) send(); }\ninp.addEventListener(\"input\", () => { slashSel = 0; slashUpdate(); });\ninp.addEventListener(\"keydown\", e => {\n  if (slashEl.classList.contains(\"hidden\") || !slashRows.length) return;\n  if (e.key === \"ArrowDown\" || e.key === \"ArrowUp\") { e.preventDefault(); slashSel = (slashSel + (e.key === \"ArrowDown\" ? 1 : -1) + slashRows.length) % slashRows.length; slashUpdate(); }\n  else if (e.key === \"Tab\" || (e.key === \"Enter\" && !e.shiftKey && inp.value.trim() !== \"/\" + slashRows[slashSel] && !CMDS[inp.value.slice(1).toLowerCase()])) { e.preventDefault(); e.stopImmediatePropagation(); slashPick(slashRows[slashSel]); }\n  else if (e.key === \"Escape\") { slashEl.classList.add(\"hidden\"); slashRows = []; e.stopImmediatePropagation(); }\n}, true);\n</script>\n</body>\n</html>\n"
ICON_SVG = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512"><rect width="512" height="512" rx="112" fill="#0b0c10"/>'
            '<rect x="146" y="146" width="220" height="220" rx="56" fill="#8b7cff"/></svg>')
PWA_MANIFEST = {"name": "me", "short_name": "me", "description": "Your coding agent, everywhere", "start_url": "/app", "scope": "/",
                "display": "standalone", "background_color": "#0b0c10", "theme_color": "#0b0c10",
                "icons": [{"src": "/icon.svg", "sizes": "any", "type": "image/svg+xml", "purpose": "any maskable"}]}
SW_JS = """self.addEventListener('install', e => self.skipWaiting());
self.addEventListener('activate', e => e.waitUntil(self.clients.claim()));
self.addEventListener('fetch', () => {});
self.addEventListener('push', e => {
  let d = {}; try { d = e.data.json(); } catch (x) { d = {title: 'me', body: e.data ? e.data.text() : ''}; }
  e.waitUntil(self.registration.showNotification(d.title || 'me', {body: d.body || '', tag: d.tag || 'me', data: {url: d.url || '/app'},
    icon: '/icon.svg', badge: '/icon.svg', renotify: true}));
});
self.addEventListener('notificationclick', e => {
  e.notification.close();
  const url = e.notification.data && e.notification.data.url || '/app';
  e.waitUntil(clients.matchAll({type: 'window', includeUncontrolled: true}).then(ws => {
    for (const w of ws) if (w.url.includes('/app') || w.url.endsWith('/')) { w.focus(); return w.navigate(url); }
    return clients.openWindow(url);
  }));
});"""


def apply_setting(cfg, key, value, model=None):
    """Settings the web UI may change by name (same keys on the local agent and, client side, on the cloud config doc).
    `model`: the chat's model (effort is set on it), default the config's."""
    st = cfg["settings"]
    if key.startswith("behavior:") and key[9:] in BEHAVIOR:
        st.setdefault("behavior", {})[key[9:]] = bool(value) and value not in ("off", "0", "false")
        if key == "behavior:tests":
            st.pop("no_verify", None)
    elif key == "lang":
        st["lang"] = value if value in ("en", "ru") else ""
    elif key == "prompt":
        cfg["prompt"] = str(value or "")
    elif key == "autocompact":
        st["autocompact"] = 0 if value in (0, "0", "off", None) else max(10, min(95, int(str(value).rstrip("%"))))
    elif key == "effort":
        m = next((x for x in cfg.get("models", []) if x.get("name") == (model or cfg.get("model"))), None)
        if not m:
            raise ValueError("no model")
        m["effort"] = "" if value in ("none", "", None) else str(value)
    elif key.startswith("skill:"):
        dis = set(st.get("disabled_skills", []))
        (dis.discard if value else dis.add)(key[6:])
        st["disabled_skills"] = sorted(dis)
    else:
        raise ValueError("unknown setting " + key)


class WebSession:
    """Agent + event log for the browser UI. Same endpoint shape as the cloud runner (/api/cloud/<id>/...)."""

    def __init__(self, store):
        import collections
        self.s = store
        self.cloud = Cloud(store)
        self.ws = Workspaces(store, self.cloud)
        self.events = collections.deque(maxlen=2000)
        self.seq = 0
        self.cond = threading.Condition()
        self.waits = {}
        self.agent = Agent(store, self.push, self.confirm, self.ask)
        store.on_change = self.cloud.autosync

    def push(self, e):
        with self.cond:
            self.seq += 1
            self.events.append(dict(e, seq=self.seq))
            self.cond.notify_all()
        if e["type"] == "turn_end" and self.cloud.online:
            def after():
                self.cloud.sync()
                if self.agent.session.get("ws"):
                    try:
                        self.ws.sync(self.agent.session["ws"])
                    except CloudError:
                        pass
            threading.Thread(target=after, daemon=True).start()

    def _wait(self, kind, **kw):
        rid = uuid.uuid4().hex[:8]
        ev = threading.Event()
        e = dict(type=kind, id=rid, **kw)
        self.waits[rid] = [ev, None, e]
        self.push(e)
        ev.wait()
        return self.waits.pop(rid)[1]

    def confirm(self, name, args):
        r = self._wait("confirm", name=name, args=args)
        return "always" if r == "always" else bool(r)

    def ask(self, q, options=None):
        return self._wait("ask", text=q, options=options) or ""

    def state(self):
        ses = self.agent.session
        return {"mode": "local", "user": self.s.auth.get("user", ""), "cwd": self.agent.cwd, "after": self.seq, "ctx": self.agent.ctx_used or ses.get("ctx", 0),
                "config": {"model": self.agent.model_name(), "models": self.s.config.get("models", []), "prompt": self.s.config.get("prompt", ""),
                           "autocompact": self.s.config["settings"].get("autocompact", 80)},
                "session": ses, "ws": ses.get("ws"), "busy": self.agent.busy,
                "pending": [w[2] for w in list(self.waits.values())], "perm": perm_mode(self.s.config["settings"]),
                "day_cost": day_cost(self.s.home), "preview": "", "behavior": behavior(self.s.config["settings"]), "lang": self.s.config["settings"].get("lang", ""),
                "agent": self.agent.preset()["name"], "context": self.agent.context_files(),
                "agents": [{"name": n, "label": p.get("label", n), "description": p.get("description", "")} for n, p in presets(self.s).items()],
                "sessions": [{"id": x["id"], "title": x.get("title"), "updated": x.get("updated"), "cwd": x.get("cwd"), "n": x.get("n", 0)}
                             for x in self.s.sessions()[:300]]}

    def handle(self, method, path, body):
        a = self.agent
        if path == "/state":
            return self.state()
        if path.startswith("/events"):
            import urllib.parse as up
            after = int(up.parse_qs(up.urlparse(path).query).get("after", ["0"])[0])
            with self.cond:
                if self.seq <= after:
                    self.cond.wait(25)
                evs = [e for e in self.events if e["seq"] > after]
            return {"events": evs, "state": {"busy": a.busy}}
        if path in ("/prompt", "/goal"):
            if a.busy:
                raise ValueError("still working")
            self.push({"type": "user", "text": ("/goal " if path == "/goal" else "") + body["text"]})
            if not a.session.get("ws") and self.cloud.online:
                a.session["ws"] = self.ws.for_dir(a.cwd)
            if path == "/goal":
                threading.Thread(target=a.goal, args=(body["text"],), daemon=True).start()
            else:
                threading.Thread(target=a.run, args=(body["text"], body.get("images") or ()), daemon=True).start()
            return {"ok": True}
        if path in ("/files", "/file"):  # project browser: list a folder / read a file, never outside the session's project
            root = os.path.realpath(a.cwd)
            p = os.path.realpath(os.path.join(root, body.get("path") or ""))
            if p != root and not p.startswith(root.rstrip(os.sep) + os.sep):
                raise ValueError("outside the project")
            if path == "/files":
                items = []
                for e in sorted(os.scandir(p), key=lambda e: (not e.is_dir(), e.name.lower())):
                    if e.name in SKIP_DIRS or e.name.startswith(".") and e.name not in (".github", ".env.example"):
                        continue
                    try:
                        items.append({"name": e.name, "dir": e.is_dir(), "size": 0 if e.is_dir() else e.stat().st_size})
                    except OSError:
                        continue
                return {"path": relp(p, root).replace("\\", "/").lstrip("."), "items": items[:1000]}
            if os.path.getsize(p) > 2_000_000:
                return {"path": body.get("path"), "text": "", "note": "file over 2 MB"}
            data = Path(p).read_bytes()
            if b"\0" in data[:8192]:
                return {"path": body.get("path"), "text": "", "note": f"binary file, {len(data)} bytes"}
            return {"path": body.get("path"), "text": data.decode("utf-8", "replace")}
        if path == "/retry":  # the browser re-sends the request itself, right after
            text, err = a.retry()
            if err:
                raise ValueError(err)
            return {"text": text}
        if path == "/undo":
            r = a.undo()
            if not r["ok"]:
                raise ValueError(r["text"])
            return r
        if path == "/upload":  # attachments from the browser land in the project folder
            d = Path(a.cwd) / "uploads"
            d.mkdir(exist_ok=True)
            name = re.sub(r"[^\w.-]+", "_", body["name"])[:80] or "file"
            (d / name).write_bytes(base64.b64decode(body["b64"]))
            return {"path": f"uploads/{name}"}
        if path == "/perm":
            st = self.s.config["settings"]
            st["perm"], st["yolo"] = body["mode"], False
            self.s.save()
            return {"ok": True}
        if path == "/cancel":
            a.cancel.set()
            return {"ok": True}
        if path == "/new":  # a new chat, in the given project folder or the current one
            if a.busy:
                raise ValueError("still working")
            d = os.path.abspath(os.path.expanduser(body.get("cwd") or a.cwd))
            if not os.path.isdir(d):
                raise ValueError(f"no such folder: {d}")
            a.session = new_session(d)
            a.ctx_used, a.snap = 0, {}
            return {"ok": True}
        if path == "/load":
            if a.busy:
                raise ValueError("still working — wait or stop it first")
            a.load(body["id"])
            wid = a.session.get("ws")
            local = self.ws.local_path(wid) if wid else None
            if local and os.path.isdir(local):
                try:
                    self.ws.sync(wid)
                except CloudError:
                    pass
                os.chdir(local)
                a.relocate(local)
            return self.state()
        if path == "/compact":
            if a.busy:
                raise ValueError("busy")
            threading.Thread(target=lambda: a.compact(reason="manual") or self.push({"type": "error", "text": "nothing to compact yet"}),
                             daemon=True).start()
            return {"ok": True}
        if path == "/agent":
            self.s.config["agent"] = body["name"]
            a.session["agent"] = body["name"]
            self.s.save()
            return {"ok": True}
        if path == "/skills":
            dis = set(self.s.config["settings"].get("disabled_skills", []))
            return {"skills": [{"name": n, "description": k.get("description", ""), "on": n not in dis} for n, k in Skills(self.s).all().items()]}
        if path == "/config":
            apply_setting(self.s.config, body["key"], body.get("value"), a.model_name())
            self.s.save()
            return self.state()
        if path == "/themes":
            return {"current": self.s.config.get("theme") or "me-dark", "themes": [theme(self.s, n) for n in themes(self.s)]}
        if path == "/theme":
            self.s.config["theme"] = body["name"]
            self.s.save()
            return {"ok": True}
        if path == "/cwd":
            d = os.path.abspath(os.path.expanduser(body["path"]))
            if not os.path.isdir(d):
                raise ValueError(f"no such folder: {d}")
            if a.busy:
                raise ValueError("still working")
            a.session = new_session(d)  # tools follow the session's folder; no process-wide chdir needed
            a.ctx_used = 0
            return self.state()
        if path == "/model":
            a.set_model(body["name"])
            return {"ok": True}
        if path in ("/confirm", "/answer"):
            w = self.waits.get(body.get("id"))
            if w:
                w[1] = body.get("ok") if path == "/confirm" else body.get("text")
                w[0].set()
            return {"ok": True}
        raise KeyError(path)


def groq_transcribe(cfg, audio, ctype="audio/webm"):
    v = cfg.get("voice") or {}
    key = v.get("groq_key") or next((p.get("api_key") for p in cfg.get("providers", {}).values() if "groq.com" in p.get("base_url", "")), "")
    if not key:
        raise ValueError("set a Groq API key (web account → Voice, or /voice key)")
    b = uuid.uuid4().hex
    ext = ctype.split("/")[-1].split(";")[0]
    parts = [f'--{b}\r\nContent-Disposition: form-data; name="model"\r\n\r\n{v.get("model") or "whisper-large-v3"}\r\n'.encode()]
    if v.get("language"):
        parts.append(f'--{b}\r\nContent-Disposition: form-data; name="language"\r\n\r\n{v["language"]}\r\n'.encode())
    parts.append(f'--{b}\r\nContent-Disposition: form-data; name="file"; filename="voice.{ext}"\r\n\r\n'.encode() + audio + b"\r\n")
    parts.append(f"--{b}--\r\n".encode())
    r = urllib.request.Request("https://api.groq.com/openai/v1/audio/transcriptions", data=b"".join(parts), method="POST",
                               headers={"Authorization": "Bearer " + key, "Content-Type": f"multipart/form-data; boundary={b}", "User-Agent": "me"})
    return json.loads(urllib.request.urlopen(r, timeout=60).read()).get("text", "").strip()


def run_web(store, host="127.0.0.1", port=8080, open_browser=True):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import secrets as _sec
    ws = WebSession(store)
    key = "" if host in ("127.0.0.1", "localhost") else _sec.token_urlsafe(16)

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, data, ctype="application/json"):
            raw = data if isinstance(data, bytes) else json.dumps(data, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(raw)

        def _authed(self):
            if not key:
                # local mode has no key: only a page served from this machine may talk to the agent
                # (blocks other sites POSTing from the browser and DNS rebinding via a foreign Host)
                host = self.headers.get("Host", "").rsplit(":", 1)[0].strip("[]")
                origin = self.headers.get("Origin")
                if host not in ("localhost", "127.0.0.1"):
                    return False
                return origin is None or origin in (f"http://localhost:{port}", f"http://127.0.0.1:{port}")
            return self.headers.get("Authorization", "") == "Bearer " + key

        def _route(self, method):
            path = self.path
            if method == "GET" and (path == "/" or path.startswith("/?")):
                return self._send(200, WEBUI.replace("__MODE__", "local").encode(), "text/html; charset=utf-8")
            if path == "/manifest.webmanifest":
                return self._send(200, json.dumps(dict(PWA_MANIFEST, start_url="/")).encode(), "application/manifest+json")
            if path == "/sw.js":
                return self._send(200, SW_JS.encode(), "text/javascript")
            if path == "/icon.svg":
                return self._send(200, ICON_SVG.encode(), "image/svg+xml")
            if not path.startswith("/api/agent"):
                return self._send(404, {"detail": "not found"})
            if not self._authed():
                return self._send(401, {"detail": "open the link printed in the terminal (it carries the access key)"})
            raw = self.rfile.read(int(self.headers.get("Content-Length") or 0)) if method == "POST" else b""
            sub = path[len("/api/agent"):]
            try:
                if sub == "/voice":
                    return self._send(200, {"text": groq_transcribe(store.config, raw, self.headers.get("Content-Type", "audio/webm"))})
                return self._send(200, ws.handle(method, sub, json.loads(raw or b"{}")))
            except KeyError:
                return self._send(404, {"detail": "not found"})
            except Exception as e:
                return self._send(400, {"detail": str(e)})

        def do_GET(self):
            self._route("GET")

        def do_POST(self):
            self._route("POST")

    srv = ThreadingHTTPServer((host, port), H)
    srv.daemon_threads = True
    url = f"http://{'localhost' if host in ('127.0.0.1', '0.0.0.0') else host}:{port}/" + (f"?k={key}" if key else "")
    print(f"me web · {url}\n  agent runs here, in {os.getcwd()} · ctrl+c to stop")
    if host == "0.0.0.0":
        print(f"  from other devices on your network use this machine's IP with ?k={key}")
    if open_browser:
        try:
            import webbrowser
            if os.environ.get("TERMUX_VERSION") and shutil.which("termux-open-url"):
                subprocess.Popen(["termux-open-url", url])
            else:
                webbrowser.open(url)
        except Exception:
            pass
    def remote(n):
        store.config.update(jload(store.cfg_path) or {})
        if ws.agent.refresh_from_disk(os.getcwd()):
            ws.push({"type": "reload"})
    if store.auth.get("token"):
        ws.cloud.watch(remote)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")


# ───────────────────────────── terminal ─────────────────────────────

ESC = "\x1b"
CSI = ESC + "["
RESET = CSI + "0m"


def rgb(h, bg=False):
    h = h.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return f"{CSI}{48 if bg else 38};2;{r};{g};{b}m"


def mix(a, b, t):
    a, b = a.lstrip("#"), b.lstrip("#")
    return "#" + "".join(f"{int(int(a[i:i + 2], 16) * (1 - t) + int(b[i:i + 2], 16) * t):02x}" for i in (0, 2, 4))


TH = {  # theme
    "bg": "#0e0f13", "fg": "#d8dae3", "dim": "#6b6f80", "faint": "#3a3d4a", "accent": "#8b7cff", "accent2": "#4fd1c5",
    "user": "#e7c66b", "ok": "#5ad18a", "err": "#ff6b7a", "warn": "#ffb454", "code_bg": "#16181f", "sel": "#262a38",
    "add": "#5ad18a", "del": "#ff6b7a",
}
SKILL_COLORS = {"red": "#ff6b7a", "green": "#5ad18a", "blue": "#6ba6ff", "magenta": "#d27cff", "cyan": "#4fd1c5", "yellow": "#e7c66b",
                "orange": "#ffb454", "purple": "#8b7cff", "pink": "#ff8fc7"}
SPIN = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
BAR = " ▏▎▍▌▋▊▉█"


def cw(ch):
    if ch < " ":
        return 1 if ch == "\t" else 0  # a tab is drawn as one space
    if unicodedata.combining(ch):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in "WF" else 1


def swidth(s):
    return sum(cw(c) for c in s)


def cells(s, lo, hi):
    """The part of s that sits in display columns [lo, hi)."""
    out, w = [], 0
    for c in s:
        if lo <= w < hi:
            out.append(c)
        w += cw(c)
    return "".join(out)


class Line:
    """A display line: list of (text, style) segments.
    For copying: `pre` leading cells are decoration (gutter, bullet indent, box border), `end` is where the content stops
    (padding and right borders follow), `soft` is set on a wrapped continuation: the text that joins it to the line above."""
    __slots__ = ("segs", "pre", "end", "soft")

    def __init__(self, segs=None):
        self.segs = segs or []
        self.pre, self.end, self.soft = 0, None, None

    def add(self, text, style=""):
        if text:
            self.segs.append((text, style))
        return self

    def width(self):
        return sum(swidth(t) for t, _ in self.segs)

    def pad(self, width, style=""):
        """Fill to `width` with styled spaces (background); the filler is not part of the copied text."""
        if self.end is None:
            self.end = self.width()
        w = self.width()
        if width > w:
            self.add(" " * (width - w), style)
        return self

    def cells(self, c0, c1):
        """Text of the cells [c0, c1)."""
        out, w = [], 0
        for t, _ in self.segs:
            for c in t:
                if w >= c1:
                    return "".join(out)
                if w >= c0:
                    out.append(c)
                w += cw(c)
        return "".join(out)

    def copy_text(self, c0=0, c1=1 << 30):
        end = self.width() if self.end is None else self.end
        return self.cells(max(c0, self.pre), min(c1, end))

    def render(self, width, base="", hl=None):
        """`hl` = (c0, c1): cells drawn reversed (mouse selection)."""
        out, w = [], 0
        for t, st in self.segs:
            if w >= width:
                break
            buf, inv = [], False
            for c in t:
                if c < " ":  # control characters would move the real cursor: a tab is a space, the rest are dropped
                    if c != "\t":
                        continue
                    c = " "
                cwid = cw(c)
                if w + cwid > width:
                    break
                if hl:
                    on = hl[0] <= w < hl[1]
                    if on != inv:
                        buf.append(CSI + ("7m" if on else "27m"))
                        inv = on
                buf.append(c)
                w += cwid
            if inv:
                buf.append(CSI + "27m")
            out.append(base + st + "".join(buf) + RESET)
        if w < width and hl and hl[1] > w:  # selection runs past the text: show it to the end of the selected range
            n = min(width, hl[1]) - max(w, hl[0])
            if n > 0:
                out.append(base + " " * max(0, hl[0] - w) + CSI + "7m" + " " * n + RESET)
                w = max(w, hl[0]) + n
        # erase the rest instead of writing spaces: the background still fills the row, but native terminal selection
        # copies no trailing blanks. Never at the last column (EL there would wipe the character just written).
        return "".join(out) + (base + CSI + "K" if w < width else "") + RESET


def wrap_segs(segs, width, indent=0, first_prefix=None, prefix=None, keep=0):
    """Word-wrap styled segments into Lines. Prefix cells count as decoration for copying, except the first `keep` cells
    after the first line's indent (e.g. a list bullet)."""
    lines, cur, cw_ = [], Line(), 0
    if first_prefix:
        for t, s in first_prefix:
            cur.add(t, s)
        cw_ = cur.width()
    else:
        cur.add(" " * indent)
        cw_ = indent
    cur.pre = max(0, cw_ - keep)

    def newline(joiner=""):
        nonlocal cur, cw_
        lines.append(cur)
        cur = Line()
        if prefix:
            for t, s in prefix:
                cur.add(t, s)
            cw_ = cur.width()
        else:
            cur.add(" " * indent)
            cw_ = indent
        cur.pre, cur.soft = cw_, joiner

    for text, st in segs:
        for tok in re.findall(r"\S+|\s+", text):
            tw = swidth(tok)
            if tok.isspace():
                if cw_ + tw <= width:
                    cur.add(tok, st)
                    cw_ += tw
                else:
                    newline(tok)  # the wrap swallowed this whitespace: copying puts it back
                continue
            if cw_ + tw > width and cw_ > (indent or 0) + (swidth("".join(t for t, _ in (prefix or []))) if prefix else 0):
                newline()
            while swidth(tok) > width - cw_:
                room = max(1, width - cw_)
                part, pw = "", 0
                for c in tok:
                    if pw + cw(c) > room:
                        break
                    part += c
                    pw += cw(c)
                cur.add(part, st)
                tok = tok[len(part):]
                newline()
            cur.add(tok, st)
            cw_ += swidth(tok)
    lines.append(cur)
    return lines


_CTRL = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def clean_text(s):
    """Typed or pasted text made safe to keep, save and print: surrogate halves joined (the Windows console splits emoji),
    lone halves dropped (they made writing the screen or the history file raise), control characters removed (an ESC in a
    paste would drive the terminal). Newlines and tabs stay."""
    if not s:
        return s
    s = s.encode("utf-16-le", "surrogatepass").decode("utf-16-le", "ignore")
    return _CTRL.sub("", s.replace("\r\n", "\n").replace("\r", "\n"))


def selection_text(lines, a, b):
    """Plain text between two (line index, cell) points, inclusive: decoration stripped, soft wraps joined."""
    (i0, c0), (i1, c1) = sorted([a, b])
    parts = []
    for i in range(max(0, i0), min(len(lines) - 1, i1) + 1):
        L = lines[i]
        t = L.copy_text(c0 if i == i0 else 0, c1 + 1 if i == i1 else 1 << 30)
        if i > i0:
            if L.soft is not None:
                parts.append(L.soft)
            else:
                parts[-1] = parts[-1].rstrip(" ")
                parts.append("\n")
        parts.append(t)
    return "".join(parts).rstrip()


_WORD_CH = re.compile(r"[\w./\\:~@#%+=-]")


def word_at(line, col):
    """Cells (first, last) of the word under `col` (paths and dashed names count as one word)."""
    cells, w = [], 0
    for t, _ in line.segs:
        for c in t:
            cells.append((w, c))
            w += cw(c)
    i = next((k for k, (p, c) in enumerate(cells) if p <= col < p + max(1, cw(c))), None)
    if i is None or not _WORD_CH.match(cells[i][1]):
        return col, col
    i0, i1 = i, i
    while i0 > 0 and _WORD_CH.match(cells[i0 - 1][1]):
        i0 -= 1
    while i1 + 1 < len(cells) and _WORD_CH.match(cells[i1 + 1][1]):
        i1 += 1
    return cells[i0][0], cells[i1][0] + max(1, cw(cells[i1][1])) - 1


# ─── Windows console input ───
# Raw INPUT_RECORDs (no VT input mode), translated here the way crossterm does it: key events by virtual-key code,
# characters from UnicodeChar, mouse from MOUSE_EVENT records. Nothing is split into escape sequences, so no key can be
# misread as Esc + garbage, and Windows Terminal (via ConPTY) delivers the same records as the classic console.

WIN_VK = {0x26: "up", 0x28: "down", 0x25: "left", 0x27: "right", 0x24: "home", 0x23: "end", 0x21: "pgup", 0x22: "pgdn", 0x2E: "delete"}


def win_keys(recs, st):
    """recs: ("k", down, repeat, vk, char_code, ctrl_state) | ("m", col, row, buttons, ctrl_state, flags), coords 1-based.
    st: dict kept across calls (surrogate half, mouse buttons). Returns keys in the parse_keys format."""
    out, text = [], []

    def flush():
        if text:
            keys, _ = parse_keys("".join(text), final=True)
            out.extend(keys)
            text.clear()

    for r in recs:
        if r[0] == "m":
            _, x, y, buttons, state, flags = r
            if flags & 0x4:  # MOUSE_WHEELED: high word = signed delta
                flush()
                out.append("wheel-up" if (buttons >> 16) & 0xFFFF < 0x8000 else "wheel-down")
            elif flags & 0x1:  # MOUSE_MOVED
                if buttons & 1:
                    flush()
                    out.append(("mouse", "drag", 0, x, y))
            elif not flags & 0x8:  # press / release / double click (HWHEELED ignored)
                prev = 0 if flags & 0x2 else st.get("btn", 0)
                for bit, b in ((1, 0), (4, 1), (2, 2)):  # left, middle, right
                    if (buttons ^ prev) & bit:
                        flush()
                        out.append(("mouse", "press" if buttons & bit else "release", b, x, y))
            st["btn"] = buttons
            continue
        _, down, rep, vk, ch, state = r
        ctrl, alt, shift = state & 0x0C, state & 0x03, state & 0x10
        if not down:
            if vk == 0x12 and ch:  # Alt+numpad: the composed character comes with the Alt release
                text.append(chr(ch))
            continue
        n = max(1, rep)
        if 0xD800 <= ch <= 0xDBFF:  # first half of an emoji etc.; the second half is the next record
            st["hi"] = ch
            continue
        if 0xDC00 <= ch <= 0xDFFF:
            hi = st.pop("hi", None)
            if hi:
                text.append(chr(0x10000 + ((hi - 0xD800) << 10) + (ch - 0xDC00)) * n)
            continue
        name = None
        if vk in WIN_VK:
            name = WIN_VK[vk]
            if name in ("left", "right") and (ctrl or alt):
                name = "ctrl-" + name
            elif name in ("up", "down") and ctrl:
                name = "ctrl-" + name
        elif vk == 0x0D:
            name = "newline" if shift or ctrl or alt else "enter"
        elif vk == 0x1B:
            name = "esc"
        elif vk == 0x09:
            name = "shift-tab" if shift else "tab"
        elif vk == 0x08:
            name = "ctrl-w" if ctrl or alt else "backspace"
        elif vk == 0x2D and shift:  # Shift+Insert: paste
            name = "ctrl-v"
        elif not ch:
            continue  # modifiers alone, F-keys, …
        if name:
            flush()
            out.extend([name] * n)
            continue
        c = chr(ch)
        if alt and not ctrl and c.lower() in ("b", "f", "v", "c"):  # (AltGr = right alt + left ctrl still types its character)
            flush()
            out.extend([{"b": "ctrl-left", "f": "ctrl-right", "v": "alt-v", "c": "alt-c"}[c.lower()]] * n)
        elif ch < 32 or ch == 127:
            flush()
            out.extend([{10: "newline", 13: "enter", 8: "backspace", 9: "tab", 27: "esc", 127: "ctrl-w"}.get(ch) or "ctrl-" + chr(ch + 96)] * n)
        else:
            text.append(c * n)
    flush()
    return out


def win_paste_burst(keys):
    """Pasting into a console arrives as typed keys, one Enter per line. An Enter followed by more input in the same burst
    (no human types that fast) is a paste: fold the burst into one paste so it is not sent line by line."""
    if len(keys) >= 4 and all(isinstance(k, tuple) and k[0] == "char" for k in keys):
        burst = "".join(k[1] for k in keys)
        if image_paths(burst):  # an image file dragged into Windows Terminal arrives as typed keys: make it a paste, so it attaches
            return [("paste", burst)]
    first = next((i for i, k in enumerate(keys) if k == "enter"), -1)
    many = len(keys) > 64  # nobody types 64 characters between two reads
    if (first < 0 or first == len(keys) - 1) and not many or not any(isinstance(k, tuple) and k[0] == "char" for k in keys):
        return keys
    simple = {"enter": "\n", "newline": "\n", "tab": "\t"}
    if not all((isinstance(k, tuple) and k[0] in ("char", "paste")) or k in simple for k in keys):
        # mixed with other keys: at least never send on a pasted line break
        return ["newline" if k == "enter" and any(isinstance(x, tuple) and x[0] == "char" for x in keys[i + 1:]) else k
                for i, k in enumerate(keys)]
    return [("paste", "".join(k[1] if isinstance(k, tuple) else simple[k] for k in keys))]


class Term:
    def __init__(self):
        self.q = queue.Queue()
        self.old = None
        self.mouse = True
        self._wst = {}

    def size(self):
        s = shutil.get_terminal_size((100, 30))
        # Windows console: never touch the last column, writing there can wrap/scroll the whole screen
        return max(20, s.columns - (1 if WIN else 0)), max(8, s.lines)

    def start(self, mouse=True):
        self.mouse = mouse
        if WIN:
            import ctypes
            k = ctypes.windll.kernel32
            h_out, h_in = k.GetStdHandle(-11), k.GetStdHandle(-10)
            mode = ctypes.c_uint32()
            k.GetConsoleMode(h_out, ctypes.byref(mode))
            self.old_out, self.old_cp = mode.value, k.GetConsoleOutputCP()
            k.GetConsoleMode(h_in, ctypes.byref(mode))
            self.old = mode.value
            k.SetConsoleOutputCP(65001)
            self.reassert()
        else:
            import termios, tty
            self.fd = sys.stdin.fileno()
            self.old = termios.tcgetattr(self.fd)
            tty.setraw(self.fd)
        # ?1007l: no "alternate scroll" - otherwise the wheel is sent as up/down arrows and pages through input history
        self.write(f"{CSI}?1049h{CSI}?1007l{CSI}?25l{CSI}?2004h" + (self._mouse_seq(True) if mouse else ""))
        threading.Thread(target=self._reader, daemon=True).start()

    @staticmethod
    def _mouse_seq(on):
        # press/release + drag + SGR coordinates. Windows gets mouse records from ENABLE_MOUSE_INPUT instead.
        if WIN:
            return ""
        return f"{CSI}?1000h{CSI}?1002h{CSI}?1006h" if on else f"{CSI}?1006l{CSI}?1002l{CSI}?1000l"

    def reassert(self):
        """(Re)apply our console modes. A program run by a tool may have changed them (no VT colors → raw escape codes on
        screen, line input → keys echoed); called at start and after every tool."""
        if not WIN or self.old is None:
            return
        import ctypes
        k = ctypes.windll.kernel32
        k.SetConsoleMode(k.GetStdHandle(-11), self.old_out | 0x0001 | 0x0004)  # PROCESSED_OUTPUT | VIRTUAL_TERMINAL_PROCESSING
        # raw keys: no line input / echo / Ctrl+C processing / VT input; EXTENDED_FLAGS so the QuickEdit bit is honored.
        flags = (self.old & ~(0x0001 | 0x0002 | 0x0004 | 0x0008 | 0x0200)) | 0x0080
        if self.mouse:
            flags = (flags | 0x0010) & ~0x0040  # mouse records; QuickEdit off (it would swallow the mouse)
        else:
            flags &= ~0x0010
        k.SetConsoleMode(k.GetStdHandle(-10), flags)

    def set_mouse(self, on):
        self.mouse = on
        self.reassert()
        self.write(self._mouse_seq(on))

    def stop(self):
        self.write(self._mouse_seq(False) + f"{CSI}?2004l{CSI}?25h{CSI}?1007h{CSI}?1049l")
        # throw away keys still waiting (the rest of a big paste): after we exit they would be typed into the shell — cmd.exe
        # would run every pasted line as a command
        if WIN:
            import ctypes
            k = ctypes.windll.kernel32
            k.FlushConsoleInputBuffer(k.GetStdHandle(-10))
            if self.old is not None:
                k.SetConsoleMode(k.GetStdHandle(-10), self.old)
            if getattr(self, "old_out", None) is not None:
                k.SetConsoleMode(k.GetStdHandle(-11), self.old_out)
                k.SetConsoleOutputCP(self.old_cp)
        elif self.old:
            import termios
            termios.tcflush(self.fd, termios.TCIFLUSH)
            termios.tcsetattr(self.fd, termios.TCSADRAIN, self.old)

    def write(self, s):
        try:
            sys.stdout.write(s)
            sys.stdout.flush()
        except UnicodeEncodeError:  # a stray surrogate must never take the app down
            sys.stdout.write(s.encode("utf-16-le", "surrogatepass").decode("utf-16-le", "replace"))
            sys.stdout.flush()
        except OSError:
            pass

    def _win_api(self):
        if hasattr(self, "_wapi"):
            return self._wapi
        import ctypes
        c = ctypes

        class KEY(c.Structure):  # fixed-width fields: identical layout on every platform (testable off Windows)
            _fields_ = [("down", c.c_int32), ("repeat", c.c_uint16), ("vk", c.c_uint16), ("scan", c.c_uint16), ("ch", c.c_uint16),
                        ("state", c.c_uint32)]

        class MOUSE(c.Structure):
            _fields_ = [("x", c.c_int16), ("y", c.c_int16), ("buttons", c.c_uint32), ("state", c.c_uint32), ("flags", c.c_uint32)]

        class U(c.Union):
            _fields_ = [("key", KEY), ("mouse", MOUSE), ("pad", c.c_byte * 16)]

        class REC(c.Structure):
            _fields_ = [("type", c.c_uint16), ("u", U)]

        class CSBI(c.Structure):
            _fields_ = [("size", c.c_int16 * 2), ("cur", c.c_int16 * 2), ("attr", c.c_uint16), ("win", c.c_int16 * 4), ("max", c.c_int16 * 2)]

        k = c.windll.kernel32
        self._wapi = (c, k, k.GetStdHandle(-10), REC, CSBI)
        return self._wapi

    def _read_win(self):
        """Block for console input; return translated keys. Everything already queued is read in the same go, so a paste
        arrives as one burst."""
        c, k, h, REC, CSBI = self._win_api()
        recs, n, pend = (REC * 256)(), c.c_uint32(), c.c_uint32()
        out, mouse = [], False
        while True:
            if not k.ReadConsoleInputW(h, recs, 256, c.byref(n)):
                raise OSError("ReadConsoleInputW failed")
            for i in range(n.value):  # copy out now: the next read reuses the buffer
                r = recs[i]
                if r.type == 1:
                    e = r.u.key
                    out.append(("k", e.down, e.repeat, e.vk, e.ch, e.state))
                elif r.type == 2 and self.mouse:
                    e = r.u.mouse
                    out.append(["m", e.x, e.y, e.buttons, e.state, e.flags])
                    mouse = True
            if out and out[-1][0] == "k" and out[-1][3] == 0x0D and len(out) < 200000:
                k.WaitForSingleObject(h, 15)  # an Enter at the end may be a pasted line break: is more coming right away?
            if not k.GetNumberOfConsoleInputEvents(h, c.byref(pend)) or not pend.value or len(out) >= 200000:
                break
        if mouse:
            info, left, top = CSBI(), 0, 0
            if k.GetConsoleScreenBufferInfo(k.GetStdHandle(-11), c.byref(info)):
                left, top = info.win[0], info.win[1]  # mouse coordinates are in buffer cells, not window cells
            for r in out:
                if r[0] == "m":
                    r[1], r[2] = r[1] - left + 1, r[2] - top + 1
        return win_paste_burst(win_keys(out, self._wst))

    def _read_win_classic(self):
        """Fallback (ME_WIN_INPUT=classic): msvcrt, keys only."""
        import msvcrt
        c = msvcrt.getwch()
        if c in ("\x00", "\xe0"):
            c2 = msvcrt.getwch()
            k = {"H": "up", "P": "down", "K": "left", "M": "right", "G": "home", "O": "end", "S": "delete", "I": "pgup", "Q": "pgdn",
                 "s": "ctrl-left", "t": "ctrl-right", "\x8d": "ctrl-up", "\x91": "ctrl-down"}.get(c2)
            return [k] if k else []
        return parse_keys(c, final=True)[0]

    def _reader(self):
        """Keys → self.q. One bad read must not leave the app deaf: errors are logged and reading goes on (a dead console
        that fails every time ends it)."""
        fails = 0

        def failed():
            nonlocal fails
            fails += 1
            log_error("input")
            time.sleep(0.05 * min(fails, 20))
            return fails < 50
        if WIN:
            classic = os.environ.get("ME_WIN_INPUT") == "classic"
            while True:
                try:
                    keys = self._read_win_classic() if classic else self._read_win()
                    fails = 0
                except Exception:
                    if failed():
                        continue
                    return
                for k in keys:
                    self.q.put(k)
        import codecs, select
        dec = codecs.getincrementaldecoder("utf-8")("replace")  # a multi-byte letter split between two reads stays whole
        buf = ""
        while True:
            try:
                buf += dec.decode(os.read(self.fd, 4096))
                while select.select([self.fd], [], [], 0.01)[0]:
                    buf += dec.decode(os.read(self.fd, 4096))
                keys, buf = parse_keys(buf)
                # an escape sequence cut in the middle: give the rest a moment, then take what we have (a lone Esc)
                if buf and not buf.startswith("\x1b[200~") and not select.select([self.fd], [], [], 0.05)[0]:
                    more, buf = parse_keys(buf, final=True)
                    keys += more
                fails = 0
            except Exception:
                buf = ""
                if failed():
                    continue
                return
            for k in win_paste_burst(keys):
                self.q.put(k)


KEYMAP = {"[A": "up", "[B": "down", "[C": "right", "[D": "left", "[H": "home", "[F": "end", "[1~": "home", "[4~": "end",
          "[3~": "delete", "[5~": "pgup", "[6~": "pgdn", "OA": "up", "OB": "down", "OC": "right", "OD": "left", "OH": "home",
          "OF": "end", "[1;5A": "ctrl-up", "[1;5B": "ctrl-down", "[1;5C": "ctrl-right", "[1;5D": "ctrl-left", "[1;3C": "ctrl-right",
          "[1;3D": "ctrl-left", "[Z": "shift-tab", "[13;2u": "newline", "[13;5u": "newline", "[13;3u": "newline", "[27;2;13~": "newline",
          "[27;5;13~": "newline", "[3;5~": "ctrl-delete"}
_ESC_PREFIX = re.compile(r"\x1b(\[[<?\d;]*|O)?$")  # what an unfinished CSI / SS3 sequence looks like


def parse_keys(buf, final=False):
    """Bytes from the terminal → keys. Returns (keys, rest): `rest` is an unfinished escape sequence or paste, kept for the
    next read unless `final`."""
    keys, i = [], 0
    while i < len(buf):
        c = buf[i]
        if buf.startswith("\x1b[200~", i):
            end = buf.find("\x1b[201~", i)
            if end < 0:
                if not final:
                    return keys, buf[i:]
                keys.append(("paste", buf[i + 6:]))
                return keys, ""
            keys.append(("paste", buf[i + 6:end]))
            i = end + 6
            continue
        if c == "\x1b":
            if not final and _ESC_PREFIX.match(buf, i):
                return keys, buf[i:]
            if i + 1 >= len(buf):
                keys.append("esc")
                i += 1
                continue
            m = re.match(r"\x1b\[<(\d+);(\d+);(\d+)([mM])", buf[i:])
            if m:
                b, x, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
                if b & 64:  # wheel, with or without shift/ctrl/alt
                    if not b & 2:
                        keys.append("wheel-down" if b & 1 else "wheel-up")
                elif b & 3 != 3:
                    keys.append(("mouse", "drag" if b & 32 else "release" if m.group(4) == "m" else "press", b & 3, x, y))
                i += m.end()
                continue
            m = re.match(r"\x1b(\[[\d;?]*[~A-Za-z]|O[A-Z])", buf[i:])
            if m:
                keys.append(KEYMAP.get(m.group(1), "unknown"))
                i += m.end()
                continue
            nxt = buf[i + 1]
            if nxt in "\r\n":
                keys.append("newline")
            elif nxt in "\x7f\x08":
                keys.append("ctrl-w")
            elif nxt == "b":
                keys.append("ctrl-left")
            elif nxt == "f":
                keys.append("ctrl-right")
            elif nxt in "vV":
                keys.append("alt-v")
            elif nxt in "cC":
                keys.append("alt-c")
            else:
                keys.append("esc")
                i += 1
                continue
            i += 2
            continue
        if c in "\r":
            keys.append("enter")
        elif c == "\n":
            keys.append("newline")
        elif c in "\x7f\x08":
            keys.append("backspace")
        elif c == "\t":
            keys.append("tab")
        elif ord(c) < 32:
            keys.append("ctrl-" + chr(ord(c) + 96))
        else:
            keys.append(("char", c))
        i += 1
    return keys, ""


# ───────────────────────────── themes ─────────────────────────────
# A theme is JSON: {"name", "label", "dark", "colors": {token: "#rrggbb"}}. Same tokens drive the TUI and the web UI.
# Installed themes live in ~/.me/themes/<name>.json (synced as theme/<name>.json); config["theme"] picks one.

THEME_TOKENS = ["bg", "bg2", "bg3", "line", "fg", "dim", "faint", "accent", "accent2", "ok", "err", "warn", "user", "code_bg", "sel"]
BUILTIN_THEMES = {
    "me-dark": {"name": "me-dark", "label": "me dark", "dark": True, "colors": {
        "bg": "#0e0f13", "bg2": "#111319", "bg3": "#171a23", "line": "#1f2230", "fg": "#d8dae3", "dim": "#6b6f80", "faint": "#3a3d4a",
        "accent": "#8b7cff", "accent2": "#4fd1c5", "ok": "#5ad18a", "err": "#ff6b7a", "warn": "#ffb454", "user": "#e7c66b",
        "code_bg": "#16181f", "sel": "#262a38"}},
    "me-light": {"name": "me-light", "label": "me light", "dark": False, "colors": {
        "bg": "#fafaf8", "bg2": "#f2f2ef", "bg3": "#e9e9e4", "line": "#e2e2dc", "fg": "#1d1e24", "dim": "#62657a", "faint": "#b9bac2",
        "accent": "#6c5ce7", "accent2": "#0f9d8e", "ok": "#1f9d55", "err": "#d63a4a", "warn": "#c77700", "user": "#a3750a",
        "code_bg": "#efefea", "sel": "#e4e2f7"}},
    "mono": {"name": "mono", "label": "mono", "dark": True, "colors": {
        "bg": "#000000", "bg2": "#0d0d0d", "bg3": "#161616", "line": "#262626", "fg": "#ededed", "dim": "#8a8a8a", "faint": "#3d3d3d",
        "accent": "#ffffff", "accent2": "#bdbdbd", "ok": "#d4d4d4", "err": "#ff5f5f", "warn": "#cfcfcf", "user": "#ffffff",
        "code_bg": "#0d0d0d", "sel": "#1f1f1f"}},
    "mono-light": {"name": "mono-light", "label": "mono light", "dark": False, "colors": {
        "bg": "#ffffff", "bg2": "#f6f6f6", "bg3": "#ededed", "line": "#e3e3e3", "fg": "#0a0a0a", "dim": "#6b6b6b", "faint": "#c2c2c2",
        "accent": "#0a0a0a", "accent2": "#4d4d4d", "ok": "#2b2b2b", "err": "#d32f2f", "warn": "#4d4d4d", "user": "#0a0a0a",
        "code_bg": "#f6f6f6", "sel": "#ebebeb"}},
    "graphite": {"name": "graphite", "label": "graphite", "dark": True, "colors": {
        "bg": "#212121", "bg2": "#2a2a2a", "bg3": "#303030", "line": "#3a3a3a", "fg": "#ececec", "dim": "#9b9b9b", "faint": "#555555",
        "accent": "#ececec", "accent2": "#a3a3a3", "ok": "#7fd18b", "err": "#f28b82", "warn": "#e6c07b", "user": "#ececec",
        "code_bg": "#171717", "sel": "#3a3a3a"}},
}


def themes(store):
    out = dict(BUILTIN_THEMES)
    d = store.home / "themes"
    for p in sorted(d.glob("*.json")) if d.exists() else []:
        t = jload(p)
        if isinstance(t, dict) and isinstance(t.get("colors"), dict):
            t.setdefault("name", p.stem)
            out[t["name"]] = t
    return out


def theme(store, name=None):
    ts = themes(store)
    t = ts.get(name or store.config.get("theme") or "me-dark") or ts["me-dark"]
    colors = dict(BUILTIN_THEMES["me-dark" if t.get("dark", True) else "me-light"]["colors"])
    colors.update({k: v for k, v in t["colors"].items() if isinstance(v, str) and v.startswith("#")})
    return dict(t, colors=colors)


def apply_theme(store, name=None):
    """Point the TUI palette at a theme (diff colors follow ok/err)."""
    c = theme(store, name)["colors"]
    TH.update(c)
    TH["add"], TH["del"] = c["ok"], c["err"]
    return c


# ───────────────────────────── clipboard ─────────────────────────────
# Native clipboard on every OS: text copy/paste and image paste. Windows uses the Win32 API (text) and PowerShell (images);
# macOS pbcopy/pbpaste/osascript; Linux wl-clipboard or xclip/xsel; Termux termux-api; WSL PowerShell. OSC 52 when nothing
# native works and over SSH (a bare OSC 52 cannot be verified, and the classic Windows console ignores it).


def _run_clip(argv, data=None, timeout=5, capture=True):
    """capture=False for copy tools: xclip/wl-copy fork a clipboard server that keeps any output pipe open."""
    try:
        out = subprocess.PIPE if capture else subprocess.DEVNULL
        r = subprocess.run(argv, input=data, stdout=out, stderr=out, timeout=timeout, **NO_WINDOW)
        return r.returncode == 0, r.stdout or b""
    except (OSError, subprocess.SubprocessError):
        return False, b""


def _win_clip_api():
    import ctypes
    u, k = ctypes.windll.user32, ctypes.windll.kernel32
    vp = ctypes.c_void_p
    k.GlobalAlloc.restype, k.GlobalAlloc.argtypes = vp, [ctypes.c_uint, ctypes.c_size_t]  # 64-bit handles must not be truncated
    k.GlobalLock.restype, k.GlobalLock.argtypes = vp, [vp]
    k.GlobalUnlock.argtypes = k.GlobalFree.argtypes = [vp]
    u.SetClipboardData.restype, u.SetClipboardData.argtypes = vp, [ctypes.c_uint, vp]
    u.GetClipboardData.restype, u.GetClipboardData.argtypes = vp, [ctypes.c_uint]
    u.OpenClipboard.argtypes = [vp]
    for _ in range(20):  # another app may hold the clipboard for a moment
        if u.OpenClipboard(None):
            return ctypes, u, k
        time.sleep(0.025)
    return None


def _win_clip(text=None):
    """Win32 clipboard: set text (str) → bool, or get text (None) → str / None."""
    api = _win_clip_api()
    if not api:
        return None
    ctypes, u, k = api
    try:
        if text is None:
            h = u.GetClipboardData(13)  # CF_UNICODETEXT
            if not h:
                return ""
            p = k.GlobalLock(h)
            try:
                return ctypes.wstring_at(p) if p else ""
            finally:
                k.GlobalUnlock(h)
        data = text.replace("\r\n", "\n").replace("\n", "\r\n").encode("utf-16-le", "surrogatepass") + b"\0\0"
        u.EmptyClipboard()
        h = k.GlobalAlloc(0x0002, len(data))  # GMEM_MOVEABLE
        if not h:
            return False
        p = k.GlobalLock(h)
        ctypes.memmove(p, data, len(data))
        k.GlobalUnlock(h)
        if not u.SetClipboardData(13, h):  # on success the system owns the memory
            k.GlobalFree(h)
            return False
        return True
    finally:
        u.CloseClipboard()


def _is_wsl():
    try:
        return "microsoft" in Path("/proc/version").read_text().lower()
    except OSError:
        return False


def clip_copy(text, term=None):
    """Put text on the system clipboard. `term` (a Term or a write function) gets an OSC 52 copy when nothing native worked or
    over SSH. Returns how: "native", "osc52" or "" when nothing worked."""
    env = os.environ
    remote = bool(env.get("SSH_CONNECTION") or env.get("SSH_TTY"))
    ok = False
    try:
        if WIN:
            ok = bool(_win_clip(text))
        elif not remote:
            data = text.encode("utf-8")
            cmds = []
            if sys.platform == "darwin":
                cmds.append(["pbcopy"])
            if env.get("TERMUX_VERSION"):
                cmds.append(["termux-clipboard-set"])
            if env.get("WAYLAND_DISPLAY"):
                cmds.append(["wl-copy"])
            if env.get("DISPLAY"):
                cmds += [["xclip", "-selection", "clipboard"], ["xsel", "--clipboard", "--input"]]
            ok = any(shutil.which(c[0]) and _run_clip(c, data, capture=False)[0] for c in cmds)
            if not ok and _is_wsl() and not env.get("WT_SESSION") and shutil.which("powershell.exe"):
                # clip.exe and PowerShell stdin decode pipes in the console code page: hand the text over in a UTF-8 file
                fd, tmp = tempfile.mkstemp(suffix=".txt")
                try:
                    with os.fdopen(fd, "wb") as f:
                        f.write(data)
                    ok_p, wp = _run_clip(["wslpath", "-w", tmp])
                    wp = wp.decode().strip()
                    if ok_p and wp:
                        ps = f"Set-Clipboard -Value ([IO.File]::ReadAllText('{wp}', [Text.Encoding]::UTF8))"
                        ok = _run_clip(["powershell.exe", "-NoProfile", "-Command", ps], timeout=10)[0]
                finally:
                    os.unlink(tmp)
    except Exception:
        ok = False
    if ok and not remote:
        return "native"
    write = getattr(term, "write", term)
    if write:
        b64 = base64.b64encode(text.encode("utf-8")).decode()
        if len(b64) <= 100_000:
            write(f"{ESC}]52;c;{b64}\a")
            return "osc52"
    return "native" if ok else ""


def clip_paste():
    """Clipboard text, or None if it can't be read here."""
    env = os.environ
    try:
        if WIN:
            t = _win_clip(None)
            return t.replace("\r\n", "\n") if t is not None else None
        cmds = []
        if sys.platform == "darwin":
            cmds.append(["pbpaste"])
        if env.get("TERMUX_VERSION"):
            cmds.append(["termux-clipboard-get"])
        if env.get("WAYLAND_DISPLAY"):
            cmds.append(["wl-paste", "--no-newline"])
        if env.get("DISPLAY"):
            cmds += [["xclip", "-selection", "clipboard", "-o"], ["xsel", "--clipboard", "--output"]]
        if _is_wsl():
            cmds.append(["powershell.exe", "-NoProfile", "-Command", "[Console]::OutputEncoding=[Text.Encoding]::UTF8; Get-Clipboard -Raw"])
        for c in cmds:
            if shutil.which(c[0]):
                ok, out = _run_clip(c)
                if ok:
                    return out.decode("utf-8", "replace").replace("\r\n", "\n")
    except Exception:
        pass
    return None


WIN_CLIP_IMAGE = r"""
Add-Type -AssemblyName System.Windows.Forms, System.Drawing
$out = $args[0]
$img = [System.Windows.Forms.Clipboard]::GetImage()
if (-not $img) {
  $files = [System.Windows.Forms.Clipboard]::GetFileDropList()
  foreach ($f in $files) { if ($f -match '\.(png|jpe?g|gif|bmp|webp)$') { $img = [System.Drawing.Image]::FromFile($f); break } }
}
if (-not $img) { exit 3 }
$max = 2000; $w = $img.Width; $h = $img.Height
if ([Math]::Max($w, $h) -gt $max) {
  $k = $max / [Math]::Max($w, $h); $bmp = New-Object System.Drawing.Bitmap ([int]($w * $k)), ([int]($h * $k))
  $g = [System.Drawing.Graphics]::FromImage($bmp); $g.InterpolationMode = 'HighQualityBicubic'
  $g.DrawImage($img, 0, 0, $bmp.Width, $bmp.Height); $g.Dispose(); $img = $bmp
}
$img.Save($out, [System.Drawing.Imaging.ImageFormat]::Png)
"""


def clip_image(dest_dir):
    """Save an image from the clipboard as PNG in dest_dir. Returns the path, or None (no image / unsupported)."""
    Path(dest_dir).mkdir(parents=True, exist_ok=True)
    out = Path(dest_dir) / time.strftime("paste-%Y%m%d-%H%M%S.png")
    try:
        if WIN:
            script = Path(tempfile.gettempdir()) / "me-clip-image.ps1"
            script.write_text(WIN_CLIP_IMAGE, "utf-8")
            ps = shutil.which("powershell") or shutil.which("pwsh") or "powershell"
            ok, _ = _run_clip([ps, "-NoProfile", "-NonInteractive", "-STA", "-ExecutionPolicy", "Bypass", "-File", str(script), str(out)], timeout=15)
        elif sys.platform == "darwin":
            if shutil.which("pngpaste"):
                ok, _ = _run_clip(["pngpaste", str(out)])
            else:
                osa = ('set f to (open for access POSIX file "%s" with write permission)\n'
                       'try\nwrite (the clipboard as «class PNGf») to f\nend try\nclose access f') % str(out).replace('"', '\\"')
                ok, _ = _run_clip(["osascript", "-e", osa])
        elif os.environ.get("WAYLAND_DISPLAY") and shutil.which("wl-paste"):
            ok, types = _run_clip(["wl-paste", "--list-types"])
            mime = next((t for t in types.decode("utf-8", "replace").split() if t.startswith("image/")), None)
            ok, data = _run_clip(["wl-paste", "--type", mime], timeout=10) if mime else (False, b"")
            if ok and data:
                out = out.with_suffix("." + mime.split("/")[1].replace("jpeg", "jpg"))
                out.write_bytes(data)
        elif os.environ.get("DISPLAY") and shutil.which("xclip"):
            ok, types = _run_clip(["xclip", "-selection", "clipboard", "-t", "TARGETS", "-o"])
            mime = next((t for t in types.decode("utf-8", "replace").split() if t.startswith("image/")), None)
            ok, data = _run_clip(["xclip", "-selection", "clipboard", "-t", mime, "-o"], timeout=10) if mime else (False, b"")
            if ok and data:
                out = out.with_suffix("." + mime.split("/")[1].replace("jpeg", "jpg"))
                out.write_bytes(data)
        else:
            return None
    except Exception:
        return None
    return str(out) if ok and out.exists() and out.stat().st_size > 0 else None


def desktop_notify(title, body=""):
    """Best effort system notification (Linux notify-send, macOS, Windows balloon, Termux). Never raises, never blocks the UI."""
    try:
        if os.environ.get("TERMUX_VERSION") and shutil.which("termux-notification"):
            _run_clip(["termux-notification", "-t", title, "-c", body or title], capture=False)
        elif sys.platform == "darwin":
            q = lambda s: s.replace("\\", "\\\\").replace('"', '\\"')
            _run_clip(["osascript", "-e", f'display notification "{q(body)}" with title "{q(title)}"'], capture=False)
        elif WIN:
            ps = ("Add-Type -AssemblyName System.Windows.Forms; $n = New-Object System.Windows.Forms.NotifyIcon; "
                  "$n.Icon = [System.Drawing.SystemIcons]::Information; $n.Visible = $true; "
                  "$n.ShowBalloonTip(5000, $env:ME_N_TITLE, $env:ME_N_BODY, 'Info'); Start-Sleep 6; $n.Dispose()")
            env = dict(os.environ, ME_N_TITLE=title, ME_N_BODY=body or title)
            subprocess.Popen([shutil.which("powershell") or "powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", ps], env=env,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **NO_WINDOW)
        elif shutil.which("notify-send") and (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
            _run_clip(["notify-send", "-a", "me", title, body], capture=False)
    except Exception:
        pass


def image_paths(text):
    """A paste that is just image file path(s) — what dragging files into a terminal produces — → those paths."""
    text = text.strip()
    if not text or "\n" in text.strip() and len(text) > 4000:
        return []
    parts = re.findall(r'"([^"]+)"|\'([^\']+)\'|((?:\\ |\S)+)', text)
    out = []
    for a, b, c in parts:
        p = (a or b or c).replace("\\ ", " ")
        if p.startswith("file://"):
            import urllib.parse as up
            p = up.unquote(up.urlparse(p).path)
            if WIN and re.match(r"^/[A-Za-z]:", p):
                p = p[1:]
        if Path(p).suffix.lower() not in IMAGE_EXT or not os.path.isfile(os.path.expanduser(p)):
            return []
        out.append(os.path.abspath(os.path.expanduser(p)))
    return out


# ───────────────────────────── markdown ─────────────────────────────

KW = r"\b(def|class|return|if|elif|else|for|while|import|from|as|try|except|finally|with|lambda|yield|async|await|const|let|var|function|" \
     r"export|new|this|self|true|false|True|False|None|null|nil|fn|pub|struct|impl|match|in|not|and|or|is|echo|then|fi|do|done|package|func)\b"


def hl_code(line, lang):
    """Very small highlighter → segments."""
    base = rgb(TH["fg"]) + rgb(TH["code_bg"], True)
    if lang in ("diff",) or (line[:1] in "+-" and lang == "diff"):
        col = TH["add"] if line.startswith("+") else TH["del"] if line.startswith("-") else TH["fg"]
        return [(line, rgb(col) + rgb(TH["code_bg"], True))]
    segs, pos = [], 0
    rx = re.compile(r"(#.*$|//.*$|\"(?:\\.|[^\"])*\"|'(?:\\.|[^'])*'|`[^`]*`|\b\d+(?:\.\d+)?\b|" + KW + ")")
    for m in rx.finditer(line):
        if m.start() > pos:
            segs.append((line[pos:m.start()], base))
        t = m.group(0)
        if t.startswith(("#", "//")) and lang not in ("css",):
            col = TH["dim"]
        elif t[0] in "\"'`":
            col = TH["ok"]
        elif t[0].isdigit():
            col = TH["warn"]
        else:
            col = TH["accent"]
        segs.append((t, rgb(col) + rgb(TH["code_bg"], True)))
        pos = m.end()
    if pos < len(line):
        segs.append((line[pos:], base))
    return segs


def inline_md(text, base):
    segs = []
    pat = re.compile(r"(\*\*[^*]+\*\*|`[^`]+`|\*[^*\s][^*]*\*|\[[^\]]+\]\([^)]+\))")
    pos = 0
    for m in pat.finditer(text):
        if m.start() > pos:
            segs.append((text[pos:m.start()], base))
        t = m.group(0)
        if t.startswith("**"):
            segs.append((t[2:-2], base + CSI + "1m"))
        elif t.startswith("`"):
            segs.append((t[1:-1], rgb(TH["accent2"]) + rgb(TH["code_bg"], True)))
        elif t.startswith("["):
            lbl, url = re.match(r"\[([^\]]+)\]\(([^)]+)\)", t).groups()
            segs.append((lbl, rgb(TH["accent"]) + CSI + "4m"))
            segs.append((f" ({url})", rgb(TH["dim"])))
        else:
            segs.append((t[1:-1], base + CSI + "3m"))
        pos = m.end()
    if pos < len(text):
        segs.append((text[pos:], base))
    return segs


def render_md(text, width, indent=2):
    out = []
    base = rgb(TH["fg"])
    in_code, lang = False, ""
    for raw in text.split("\n"):
        if raw.strip().startswith("```"):
            if not in_code:
                in_code, lang = True, raw.strip()[3:].strip().lower()
                bar = Line().add(" " * indent).add(f" {lang or 'code'} ".ljust(width - indent), rgb(TH["dim"]) + rgb(TH["code_bg"], True))
            else:
                in_code = False
                bar = Line().add(" " * indent).add(" " * (width - indent), rgb(TH["code_bg"], True))
            bar.pre = width  # the code block's title / bottom bar is not copied
            out.append(bar)
            continue
        if in_code:
            segs = hl_code(raw.replace("\t", "    "), lang)
            ls = wrap_segs(segs, width, first_prefix=[(" " * indent, ""), (" ", rgb(TH["code_bg"], True))],
                           prefix=[(" " * indent, ""), (" ", rgb(TH["code_bg"], True))])
            for l in ls:
                l.pad(width, rgb(TH["code_bg"], True))
            out.extend(ls)
            continue
        s = raw.rstrip()
        m = re.match(r"^(#{1,6})\s+(.*)", s)
        if m:
            lvl = len(m.group(1))
            col = TH["accent"] if lvl <= 2 else TH["accent2"]
            out.extend(wrap_segs([(m.group(2), rgb(col) + CSI + "1m")], width, indent))
            continue
        m = re.match(r"^(\s*)([-*+]|\d+[.)])\s+(.*)", s)
        if m:
            ind = indent + len(m.group(1))
            bullet = "•" if m.group(2) in "-*+" else m.group(2)
            pre = [(" " * ind, ""), (bullet + " ", rgb(TH["accent"]))]
            out.extend(wrap_segs(inline_md(m.group(3), base), width, first_prefix=pre, prefix=[(" " * (ind + swidth(bullet) + 1), "")],
                                 keep=len(m.group(1)) + swidth(bullet) + 1))  # copying keeps the bullet and nesting
            continue
        if re.match(r"^\s*(---+|\*\*\*+)\s*$", s):
            hr = Line().add(" " * indent).add("─" * (width - indent * 2), rgb(TH["faint"]))
            hr.pre = width
            out.append(hr)
            continue
        if s.startswith(">"):
            out.extend(wrap_segs(inline_md(s.lstrip("> "), rgb(TH["dim"]) + CSI + "3m"), width,
                                 first_prefix=[(" " * indent, ""), ("▌ ", rgb(TH["faint"]))], prefix=[(" " * indent, ""), ("▌ ", rgb(TH["faint"]))]))
            continue
        if "|" in s and s.strip().startswith("|"):
            if re.match(r"^\s*\|[\s:|-]+\|\s*$", s):
                continue
            cells = [c.strip() for c in s.strip().strip("|").split("|")]
            out.extend(wrap_segs(inline_md("  │  ".join(cells), base), width, indent))
            continue
        out.extend(wrap_segs(inline_md(s, base), width, indent))
    return out


# ───────────────────────────── i18n (terminal UI) ─────────────────────────────
# English is the source text. T("…") returns the Russian line when the UI language is ru. Keys with {} are templates:
# T("model → {}") also translates "model → gpt-5". Commands (/model), product words (model, provider, token, todo, API,
# MCP, skill, plugin) and file names stay as they are. The agent itself answers in the user's language either way.

UI_LANG = {"v": "en"}

RU = {
    # commands (/help, suggestions)
    "show commands & keys": "команды и клавиши", "switch mode: code, design, plan, review…": "режим агента: code, design, plan, review…",
    "create AGENTS.md for this project": "создать AGENTS.md для проекта", "switch model": "сменить модель (в этом чате)",
    "add / edit / sync models": "добавить / изменить / синхронизировать модели", "add / edit API providers": "добавить / изменить API-провайдеров",
    "reasoning effort: none…xhigh": "глубина рассуждений: none…xhigh", "edit your system prompt": "свой системный промпт",
    "switches: self-testing, todo, web, plan first, brief…": "переключатели: самопроверка, todo, веб, сначала план, кратко…",
    "list / toggle / create skills": "навыки (skills): список, вкл/выкл, создать", "extensions that modify me": "плагины, которые расширяют me",
    "set env secrets for skills": "секреты (env) для навыков", "new session": "новая сессия", "resume a past session": "вернуться к прошлой сессии",
    "sync this project folder to the cloud": "синхронизировать папку проекта с облаком", "clear screen + new session": "очистить экран + новая сессия",
    "copy last answer": "скопировать последний ответ", "summarize history to free context": "сжать историю, чтобы освободить контекст",
    "what fills the context window": "чем занят контекст", "auto-compact at N% of context (off)": "автосжатие при N% контекста (off)",
    "work autonomously until done+verified or blocked": "работать сам, пока не сделает и не проверит (или не упрётся)",
    "revert the files of the last turn": "откатить файлы последнего хода", "take back the last request and run it again": "отменить последний запрос и выполнить заново",
    "save this chat as Markdown": "сохранить чат в Markdown", "permissions: read-only / ask / full + allow-list": "разрешения: read-only / ask / full + список разрешённых",
    "spending: turn, session, today, limits": "расходы: задача, сессия, сегодня, лимиты", "MCP servers (lazy)": "MCP-серверы (подгружаются по требованию)",
    "show the agent's checklist": "показать todo-список агента", "toggle full access": "полный доступ вкл/выкл", "sync with cloud now": "синхронизировать сейчас",
    "log in / register": "войти / зарегистрироваться", "authenticator (TOTP) on/off": "двухфакторная защита (TOTP) вкл/выкл", "log out of cloud": "выйти из облака",
    "settings": "настройки", "update me": "обновить me", "color theme (installable)": "цветовая тема (можно ставить новые)", "quit": "выход",
    "go back to an earlier request (history + files)": "вернуться к одному из прошлых запросов (история + файлы)",
    "continue in a copy of this chat": "продолжить в копии этого чата", "quick side question, kept out of the history": "быстрый вопрос в сторону, не попадает в историю",
    "find text in all chats": "поиск по всем чатам", "nothing to rewind": "нечего откатывать",
    "Rewind — the chat goes back to before this request": "Откат — чат вернётся к моменту перед этим запросом",
    "↶ rewound · restored {} file(s): {}": "↶ откат · восстановлено файлов: {}: {}", "↶ rewound": "↶ откат выполнен",
    "forked: {} — the original chat stays in /sessions": "копия чата: {} — оригинал остался в /sessions",
    "Side question (does not go into the chat)": "Вопрос в сторону (не попадёт в чат)", "Search all chats": "Поиск по всем чатам",
    "nothing found: {}": "ничего не найдено: {}", "Found in {} chat(s)": "Найдено в чатах: {}", "✗ the command failed": "✗ команда завершилась с ошибкой",
    "no such request": "такого запроса нет",
    "what changed in this chat": "что изменилось в файлах за этот чат", "rename this chat": "переименовать чат", "interface language: en / ru": "язык интерфейса: en / ru",
    # welcome / status / input
    "{} loaded into this session": "{} загружен в эту сессию", "◇ no AGENTS.md — run /init so I remember this project": "◇ нет AGENTS.md — запустите /init, чтобы я запомнил проект",
    "commands": "команды", "files": "файлы", "interrupt": "прервать", "newline": "новая строка", "paste image": "вставить картинку", "copy": "копировать",
    "… +{} more lines": "… ещё строк: {}", "thought for {}": "думал {}", "thinking…": "думаю…", "working…": "работаю…", "writing…": "пишу…",
    "no model — /provider": "нет модели — /provider", "no model": "нет модели", "◇ no AGENTS.md (/init)": "◇ нет AGENTS.md (/init)", "≈ offline": "≈ офлайн",
    "full": "полный", "read-only": "только чтение", "ask anything, / for commands": "спросите что угодно, / — команды", "queue a message…": "сообщение в очередь…",
    "esc to interrupt": "esc — прервать", " ↑ {} more lines below · pgdn ": " ↑ ниже ещё строк: {} · pgdn ", "type to filter": "печатайте для поиска",
    "no matches": "ничего не найдено", "↑↓ select · enter confirm · esc cancel": "↑↓ выбор · enter ок · esc отмена",
    "y/n · ↑↓ enter · esc deny": "y/n · ↑↓ enter · esc запретить", "⚠ allow": "⚠ разрешить", "enter newline · ctrl+s save · esc cancel": "enter новая строка · ctrl+s сохранить · esc отмена",
    "enter confirm · esc cancel": "enter ок · esc отмена", "    … {} more": "    … ещё {}", "{} more lines": "ещё строк: {}", "running {}": "выполняется {}", "starting…": "запуск…",
    "  ◎ goal: {}": "  ◎ цель: {}",
    # input / clipboard / images / workspace
    "◌ queued ({}): {}": "◌ в очереди ({}): {}", "nothing to copy": "нечего копировать", "copy failed: no clipboard here": "не удалось скопировать: нет буфера обмена",
    "copied {} · {} chars": "скопировано: {} · символов: {}", "copied {} · {} chars (via terminal)": "скопировано: {} · символов: {} (через терминал)",
    "last answer": "последний ответ", "selection": "выделение", "image attached: {}": "картинка прикреплена: {}",
    "image attached: {} — this model has vision off (/models)": "картинка прикреплена: {} — у этой модели выключено зрение (/models)",
    "clipboard is empty": "буфер обмена пуст", "clipboard is empty (Linux needs wl-clipboard or xclip)": "буфер обмена пуст (на Linux нужен wl-clipboard или xclip)",
    "no model yet — let's set one up": "модели ещё нет — давайте подключим", "☁ session updated from another device": "☁ сессия обновлена с другого устройства",
    "☁ {} updates from cloud": "☁ обновлений из облака: {}", "☁ project linked as workspace {}": "☁ проект связан с рабочей областью {}",
    "☁ files ↑{} ↓{}": "☁ файлы ↑{} ↓{}", "✗ workspace sync: {}": "✗ синхронизация рабочей области: {}", "log in first — /login": "сначала войдите — /login",
    "↻ sync now": "↻ синхронизировать сейчас", "unlink this folder": "отвязать эту папку", "files stay in the cloud": "файлы останутся в облаке",
    "link this folder": "привязать эту папку", "get a cloud workspace into a folder": "скачать облачную рабочую область в папку",
    "Workspace — code files sync to the cloud (≤1 MB each, heavy/ignored skipped)": "Рабочая область — файлы кода синхронизируются с облаком (≤1 МБ каждый, тяжёлые и игнорируемые пропускаются)",
    "Workspace name": "Имя рабочей области", "Cloud workspaces": "Облачные рабочие области", "{} files · {}B": "файлов: {} · {}Б",
    "Local folder for this workspace": "Локальная папка для рабочей области", "☁ {}: ↓{} files · cwd {}": "☁ {}: ↓{} файлов · папка {}",
    # commands: agent, init, compact, theme, market, copy, perm, goal, behavior, retry, export, todo, cost, mcp
    "Agent mode": "Режим агента", "+ more agents": "+ ещё агенты", "mode → {} · tools: {}": "режим → {} · инструменты: {}",
    "busy — wait for the turn to finish": "занят — дождитесь конца хода", "nothing to compact yet": "пока нечего сжимать",
    "Auto-compact (now: {}{} of context, max 1M tokens)": "Автосжатие (сейчас: {}{} контекста, максимум 1M токенов)", "auto-compact {}": "автосжатие {}",
    "at {} tokens": "при {} токенов", "off": "выкл", "on": "вкл", "default": "по умолчанию", "Theme (↑↓ previews)": "Тема (↑↓ — предпросмотр)", "theme → {}": "тема → {}",
    "+ get more themes": "+ ещё темы", "Marketplace · {}s": "Маркетплейс · {}", "installed {} {}": "установлено: {} {}",
    "busy — esc to stop the current turn first": "занят — сначала остановите ход (esc)", "code block {}": "блок кода {}", "answer #{} from the end": "ответ №{} с конца",
    "whole conversation": "весь разговор", "{} chars": "символов: {}", "{} messages": "сообщений: {}", "no code block in that answer": "в этом ответе нет блока кода",
    "Copy": "Копировать", "permissions: {}": "разрешения: {}", "read-only — no file changes; only allow-listed commands": "только чтение — без изменения файлов; только разрешённые команды",
    "ask — edits inside the project are free; commands ask": "ask — правки внутри проекта без вопросов; команды спрашивают",
    "full access — everything runs without asking": "полный доступ — всё выполняется без вопросов", "  allow-list…": "  список разрешённых…",
    "commands that never ask": "команды, которые не спрашивают", "no file changes; only allow-listed commands": "без изменения файлов; только разрешённые команды",
    "edits inside the project are free; commands ask": "правки внутри проекта без вопросов; команды спрашивают", "everything runs without asking": "всё выполняется без вопросов",
    "Allowed commands, one pattern per line (* = anything; chained/redirected commands always ask)":
        "Разрешённые команды, по шаблону на строку (* = что угодно; цепочки и перенаправления всегда спрашивают)",
    "{} allowed patterns": "разрешённых шаблонов: {}", "Goal — the agent works until it is done and verified, or blocked": "Цель — агент работает, пока не сделает и не проверит, или пока не упрётся",
    "Behavior — enter toggles, esc closes": "Поведение — enter переключает, esc закрывает", "{} on": "{} вкл", "{} off": "{} выкл",
    "verify changes itself (tests, build, run)": "сам проверяет изменения (тесты, сборка, запуск)", "keep a todo checklist on multi-step tasks": "ведёт todo-список в многошаговых задачах",
    "search the web for current facts and docs": "ищет в интернете актуальные факты и доки", "stay in the project folder unless asked": "не выходит за папку проекта без просьбы",
    "show a plan and wait for a go before big changes": "перед большими изменениями показывает план и ждёт «да»", "very short replies": "очень короткие ответы",
    "explain the why (learning mode)": "объясняет «почему» (режим обучения)", "git commit each finished task": "git-коммит после каждой задачи",
    "offer to create AGENTS.md in new projects": "предлагает создать AGENTS.md в новых проектах",
    "↻ retrying the last request": "↻ повторяю последний запрос", "busy": "занят", "nothing to retry": "нечего повторять", "nothing to undo": "нечего откатывать",
    "✓ chat saved to {}": "✓ чат сохранён в {}", "no todo list in this session": "в этой сессии нет todo-списка", "no changes in this chat yet": "в этом чате файлы ещё не менялись",
    "chat renamed: {}": "чат переименован: {}", "Chat name": "Название чата",
    "MCP servers — loaded lazily: one prompt line idle, tools fetched on use": "MCP-серверы — подгружаются лениво: строка в промпте, инструменты по запросу",
    "+ add server": "+ добавить сервер", "stdio command or http url": "команда stdio или http-адрес", "Name (e.g. github)": "Имя (например, github)",
    "Command (npx -y @modelcontextprotocol/server-github) or URL (https://…/mcp)": "Команда (npx -y @modelcontextprotocol/server-github) или URL (https://…/mcp)",
    "One-line description for the agent": "Описание для агента в одну строку", "MCP {} added": "MCP {} добавлен", "test: list tools": "проверка: список инструментов",
    "remove": "удалить",
    # models / providers
    "no models yet": "моделей пока нет", "Model": "Модель", "model → {} (this chat; new chats start with it too)": "модель → {} (в этом чате; новые чаты тоже начнутся с неё)",
    "Reasoning effort": "Глубина рассуждений", "effort → {}": "effort → {}", "Providers": "Провайдеры", "+ add provider": "+ добавить провайдера",
    "OpenAI-compatible URL + key": "OpenAI-совместимый URL + ключ", "edit URL / key": "изменить URL / ключ", "sync models from API": "загрузить модели из API",
    "delete": "удалить", "deleted {}": "удалено: {}", "Base URL": "Base URL", "any OpenAI-compatible endpoint, e.g. https://openrouter.ai/api/v1": "любой OpenAI-совместимый адрес, например https://openrouter.ai/api/v1",
    "API key": "API-ключ", "stored in your config, synced to your cloud account": "хранится в вашем конфиге и синхронизируется с облачным аккаунтом",
    "Provider name": "Имя провайдера", "provider {} saved": "провайдер {} сохранён", "Fetch models from this provider now?": "Загрузить модели этого провайдера сейчас?",
    "yes, sync models": "да, загрузить модели", "no": "нет", "fetching models from {}…": "загружаю модели из {}…", "✗ could not list models: {}": "✗ не удалось получить модели: {}",
    "provider returned no models": "провайдер не вернул ни одной модели", "✓ done": "✓ готово", "{} added": "добавлено: {}",
    "Add models from {} ({})": "Добавить модели из {} ({})", "enter toggles · type to filter": "enter — добавить/убрать · печатайте для поиска",
    "{} models added": "добавлено моделей: {}", "Models": "Модели", "↻ sync from provider": "↻ загрузить от провайдера", "+ add model manually": "+ добавить модель вручную",
    "Provider": "Провайдер", "add a provider first": "сначала добавьте провайдера", "Model id (as the API expects it)": "id модели (как его ждёт API)",
    "display name": "отображаемое имя", "API model id": "id модели в API", "context window, tokens": "окно контекста, токенов",
    "max output tokens (blank = default)": "максимум токенов ответа (пусто = по умолчанию)", "reasoning effort: none/minimal/low/medium/high/xhigh": "глубина рассуждений: none/minimal/low/medium/high/xhigh",
    "blank = default": "пусто = по умолчанию", "send images: true/false": "отправлять картинки: true/false",
    "$ per 1M tokens: in out [cached] (blank = built-in table)": "$ за 1M токенов: вход выход [кэш] (пусто = встроенная таблица)",
    "prompt caching: auto / anthropic / openai / off": "кэш промпта: auto / anthropic / openai / off", "Model · {}": "Модель · {}", "✓ use this model": "✓ использовать эту модель",
    "✗ delete": "✗ удалить", "model → {}": "модель → {}", "API style": "Тип API", "most compatible": "самый совместимый", "Prompt caching": "Кэш промпта",
    "detect from model/provider": "определить по модели/провайдеру", "in out [cached], $ per 1M tokens": "вход выход [кэш], $ за 1M токенов",
    "numbers, e.g. 3 15 0.3": "числа, например 3 15 0.3", "must be a number": "нужно число", "another model is already called {}": "модель с именем {} уже есть",
    "Vision": "Зрение (картинки)",
    # prompt, secrets, skills, plugins
    "Your system prompt (appended to me's base prompt)": "Ваш системный промпт (добавляется к базовому промпту me)",
    "ctrl+s to save · synced to all your devices": "ctrl+s — сохранить · синхронизируется на все устройства", "prompt saved": "промпт сохранён",
    "Secrets (env vars for skills)": "Секреты (переменные окружения для навыков)", "+ add secret": "+ добавить секрет", "env var for skills": "переменная окружения для навыков",
    "Name (e.g. GITHUB_TOKEN)": "Имя (например, GITHUB_TOKEN)", "Value": "Значение", "{} saved": "{} сохранён", "Skills": "Навыки (skills)",
    "+ new skill from template": "+ новый навык из шаблона", "+ ask me to write a skill": "+ попросить me написать навык", "+ install from URL": "+ установить по URL",
    "● on ○ off": "● вкл ○ выкл", "Skill name (a-z, 0-9, -)": "Имя навыка (a-z, 0-9, -)", "created {} — edit it, it's live immediately": "создан {} — правьте, работает сразу",
    "What should the skill do?": "Что должен делать навык?", "URL of a skill .py/.md": "URL навыка .py/.md", "installed {}": "установлено: {}", "Delete {}?": "Удалить {}?",
    "yes, delete": "да, удалить", "Plugins (changes apply after restart)": "Плагины (изменения — после перезапуска)", "+ browse plugin marketplace": "+ маркетплейс плагинов",
    "+ new plugin from template": "+ новый плагин из шаблона", "Plugin name": "Имя плагина", "created {} — restart me to load it": "создан {} — перезапустите me, чтобы загрузить",
    "Plugin marketplace": "Маркетплейс плагинов", "installed plugin {} — restart me (/update force or relaunch) to load it": "плагин {} установлен — перезапустите me (/update force или заново), чтобы загрузить",
    "view source": "посмотреть код",
    # sessions, cloud, login, 2fa, config, update
    "no sessions yet": "сессий пока нет", "Resume session": "Вернуться к сессии", "resumed · {} messages": "сессия открыта · сообщений: {}", "✗ workspace: {}": "✗ рабочая область: {}",
    "not logged in — /login": "вы не вошли — /login", "✗ sync failed: {}": "✗ синхронизация не удалась: {}", "synced · {} updated from cloud": "синхронизировано · из облака обновлено: {}",
    "Cloud account": "Облачный аккаунт", "log in": "войти", "create account": "создать аккаунт", "change server": "сменить сервер", "Server URL": "Адрес сервера",
    "Username": "Имя пользователя", "Password": "Пароль", "Repeat password": "Повторите пароль", "passwords don't match": "пароли не совпадают",
    "Authenticator code": "Код из приложения-аутентификатора", "Authenticator code (wrong, try again)": "Код из аутентификатора (неверный, попробуйте ещё)",
    "☁ logged in as {} — syncing…": "☁ вы вошли как {} — синхронизация…", "synced · {} items from cloud": "синхронизировано · из облака: {}",
    "logged out — local data kept": "вы вышли — локальные данные сохранены", "Authenticator is ON": "Двухфакторная защита включена", "keep it": "оставить", "turn off": "выключить",
    "Current authenticator code": "Текущий код аутентификатора", "2FA disabled": "2FA выключена", "Enter the 6-digit code to confirm": "Введите 6-значный код для подтверждения",
    "✓ 2FA enabled": "✓ 2FA включена", "Settings": "Настройки", "auto-approve tools   {}": "инструменты без вопросов   {}", "mouse                {}": "мышь                 {}",
    "on: wheel scroll, drag to select+copy · off: the terminal's own selection": "вкл: прокрутка колесом, выделение с копированием · выкл: выделение самого терминала",
    "show thinking        {}": "показывать рассуждения {}", "behavior switches…": "переключатели поведения…", "self-testing, todo, web, plan first, brief, explain, commit": "самопроверка, todo, веб, сначала план, кратко, объяснения, коммиты",
    "language             {}": "язык                 {}", "data dir": "папка данных", "notify when done     {}": "уведомлять о готовности {}",
    "a sound + a desktop notification when a task took over 30 s": "звук и системное уведомление, если задача шла дольше 30 с",
    "checking for updates…": "проверяю обновления…", "✗ bad update payload": "✗ повреждённое обновление", "already on latest v{}": "уже последняя версия v{}",
    "✓ updated to v{} — restarting…": "✓ обновлено до v{} — перезапуск…", "unknown command /{} — /help": "неизвестная команда /{} — /help",
    "Language": "Язык", "auto (system)": "авто (как в системе)", "language → {}": "язык → {}",
    # tool confirm, welcome, events
    "Yes": "Да", "Yes, always allow `{}`": "Да, и всегда разрешать `{}`", "Yes, and switch to full access (/perm)": "Да, и включить полный доступ (/perm)",
    "No, tell me what to do": "Нет, скажу, что делать", "Welcome to me — sync everything across your machines": "Добро пожаловать в me — всё синхронизируется между вашими устройствами",
    "log in / create account": "войти / создать аккаунт", "config, keys, skills & chats follow you everywhere": "настройки, ключи, навыки и чаты с вами на любом устройстве",
    "use offline": "работать офлайн", "you can /login any time": "войти можно в любой момент: /login", "☁ {} items synced from cloud": "☁ из облака синхронизировано: {}",
    "☁ sync failed: {}": "☁ синхронизация не удалась: {}", "☁ syncing…": "☁ синхронизация…",
    "Let's connect a model. Any OpenAI-compatible API works (OpenAI, OpenRouter, DeepSeek, Groq, Ollama, LM Studio…).":
        "Подключим модель. Подойдёт любой OpenAI-совместимый API (OpenAI, OpenRouter, DeepSeek, Groq, Ollama, LM Studio…).",
    "↻ {} — retrying": "↻ {} — повторяю", "↻ {} — retrying in {}s": "↻ {} — повтор через {} с", "◌ compacting history ({})…": "◌ сжимаю историю ({})…",
    "✓ compacted {} → {} messages · context now ~{} tokens": "✓ сжато: {} → {} сообщений · контекст теперь ~{} токенов",
    "◌ self-check: changes were made but not run yet — verifying before finishing": "◌ самопроверка: изменения не запускались — проверяю перед завершением",
    "◎ goal: {}": "◎ цель: {}", "✓ goal done": "✓ цель достигнута", "✗ goal blocked": "✗ цель заблокирована", "interrupted": "прервано",
    "me · session {} · ↑{} ↓{}  (resume: /sessions)": "me · сессия {} · ↑{} ↓{}  (вернуться: /sessions)",
    "{} step": "{} шаг", "{} steps": "шагов: {}", "context {}": "контекст {}", "sent ↑{}": "отправлено ↑{}", "sent ↑{} ({}% cached)": "отправлено ↑{} ({}% из кэша)",
    "full access — tools run without asking": "полный доступ — инструменты без вопросов", "ask — commands and writes outside the project ask": "ask — команды и запись вне проекта спрашивают",
    "workspace-write (ask for commands)": "правки в проекте (команды спрашивают)", "read-only": "только чтение", "ask": "спрашивать", "full access": "полный доступ", "permissions: {}": "разрешения: {}",
    "dark · built-in": "тёмная · встроенная", "light · built-in": "светлая · встроенная", "dark": "тёмная", "light": "светлая",
    "marketplace": "маркетплейс", "{} · custom": "{} · своя", "unlinked": "отвязано", "Permissions": "Разрешения", "{} limit {}": "лимит ({}) {}", "✗ plugin {}: {}": "✗ плагин {}: {}",
    "✓ task done": "✓ задача готова", "me: task finished": "me: задача готова",
}

_RX = []  # (regex, ru template) for keys with {}


def _compile():
    for k, v in RU.items():
        if "{}" in k:
            _RX.append((re.compile("^" + re.escape(k).replace(r"\{\}", "(.+?)") + "$", re.S), v))


def set_ui_lang(settings):
    """settings.lang: "en", "ru" or "" (auto: the system language)."""
    v = os.environ.get("ME_LANG") or (settings or {}).get("lang") or ""  # ME_LANG: tests and scripts pin it
    if v not in ("en", "ru"):
        v = "ru" if system_lang().startswith("ru") else "en"
    UI_LANG["v"] = v
    if not _RX:
        _compile()
    return v


def system_lang():
    for k in ("LC_ALL", "LC_MESSAGES", "LANG", "LANGUAGE"):
        if os.environ.get(k):
            return os.environ[k].lower()
    if WIN:
        try:
            import ctypes
            return "ru" if ctypes.windll.kernel32.GetUserDefaultUILanguage() & 0x3FF == 0x19 else "en"
        except Exception:
            pass
    return "en"


def T(s):
    """The UI line in the user's language (English text = the key)."""
    if UI_LANG["v"] != "ru" or not isinstance(s, str) or not s:
        return s
    r = RU.get(s)
    if r is not None:
        return r
    st = s.strip()
    if st != s and st in RU:  # padding around a key (" no matches")
        return s[:len(s) - len(s.lstrip())] + RU[st] + s[len(s.rstrip()):]
    for rx, tpl in _RX:
        m = rx.match(s)
        if m:
            parts = tpl.split("{}")
            return "".join(p + (T(m.group(i + 1)) if i < len(parts) - 1 else "") for i, p in enumerate(parts))
    m = _MARK.match(s)  # "● ask", "[x] verify …", "✓ done": a marker in front of a known line
    if m and m.group(2) != s:
        rest = T(m.group(2))
        if rest != m.group(2):
            return m.group(1) + rest
    return s


_MARK = re.compile(r"^((?:[●○✓✗◆◇▸•☁↻◌]|\[[x ]\])?\s+|[●○✓✗◆◇▸•☁↻◌]\s*|\[[x ]\]\s*)(.+)$", re.S)


# ───────────────────────────── TUI ─────────────────────────────

COMMANDS = [
    ("help", "show commands & keys"), ("agent", "switch mode: code, design, plan, review…"), ("init", "create AGENTS.md for this project"), ("model", "switch model"), ("models", "add / edit / sync models"),
    ("provider", "add / edit API providers"), ("effort", "reasoning effort: none…xhigh"), ("prompt", "edit your system prompt"), ("behavior", "switches: self-testing, todo, web, plan first, brief…"),
    ("skills", "list / toggle / create skills"), ("plugins", "extensions that modify me"), ("secret", "set env secrets for skills"), ("new", "new session"),
    ("sessions", "resume a past session"), ("workspace", "sync this project folder to the cloud"), ("clear", "clear screen + new session"), ("copy", "copy last answer"), ("compact", "summarize history to free context"), ("context", "what fills the context window"), ("autocompact", "auto-compact at N% of context (off)"),
    ("goal", "work autonomously until done+verified or blocked"), ("undo", "revert the files of the last turn"), ("retry", "take back the last request and run it again"),
    ("rewind", "go back to an earlier request (history + files)"), ("fork", "continue in a copy of this chat"),
    ("btw", "quick side question, kept out of the history"), ("search", "find text in all chats"),
    ("export", "save this chat as Markdown"), ("diff", "what changed in this chat"), ("rename", "rename this chat"),
    ("lang", "interface language: en / ru"),
    ("perm", "permissions: read-only / ask / full + allow-list"), ("cost", "spending: turn, session, today, limits"), ("mcp", "MCP servers (lazy)"),
    ("todo", "show the agent's checklist"), ("yolo", "toggle full access"), ("sync", "sync with cloud now"), ("login", "log in / register"),
    ("2fa", "authenticator (TOTP) on/off"), ("logout", "log out of cloud"), ("config", "settings"), ("update", "update me"),
    ("theme", "color theme (installable)"), ("exit", "quit"), ("quit", "quit"),
]
LOGO = ["█▀▄▀█ █▀▀", "█ ▀ █ ██▄"]


class Block:
    """Transcript item. Renders to Lines for a width; cached."""

    def __init__(self, kind, **kw):
        self.kind, self.__dict__["data"] = kind, kw
        self.text = kw.get("text", "")
        self.t0 = time.time()
        self.done = False
        self.collapsed = kw.get("collapsed", False)
        self.events = []
        self.progress = None
        self.status = ""
        self.ok = None
        self.elapsed = 0
        self._cache = None

    def touch(self):
        self._cache = None


# ───── TUI: rendering (mixin) ─────


class TUIRender:
    # ─── rendering ───
    def block_lines(self, b, width):
        if b._cache and b._cache[0] == width and b.done and b.kind not in ("tool", "skill", "welcome"):
            return b._cache[1]
        if b._cache and b._cache[0] == width and b.kind in ("user", "info", "stats"):
            return b._cache[1]
        L = self._render_block(b, width)
        b._cache = (width, L)
        return L

    def _render_block(self, b, width):
        W = width
        dim, fg = rgb(TH["dim"]), rgb(TH["fg"])
        if b.kind == "welcome":
            out = [Line()]
            for i, l in enumerate(LOGO):
                segs = Line().add("  ")
                for j, ch in enumerate(l):
                    segs.add(ch, rgb(mix(TH["accent"], TH["accent2"], j / len(l))))
                out.append(segs)
            out.append(Line())
            out.append(Line().add("  ").add(f"me v{VERSION}", rgb(TH["fg"]) + CSI + "1m").add("  ·  ", dim).add(b.text, dim))
            ctxf = self.agent.context_files()
            out.append(Line().add("  ").add("◆ " + T(f"{', '.join(ctxf)} loaded into this session"), rgb(TH["ok"])) if ctxf else
                       Line().add("  ").add(T("◇ no AGENTS.md — run /init so I remember this project"), rgb(TH["warn"])))
            out.append(Line().add("  ").add("/ ", rgb(TH["accent"])).add(T("commands") + "   ", dim).add("@ ", rgb(TH["accent"])).add(T("files") + "   ", dim)
                       .add("esc ", rgb(TH["accent"])).add(T("interrupt") + "   ", dim).add("shift+enter ", rgb(TH["accent"])).add(T("newline") + "   ", dim)
                       .add("alt+v ", rgb(TH["accent"])).add(T("paste image") + "   ", dim).add("drag ", rgb(TH["accent"])).add(T("copy"), dim))
            out.append(Line())
            return out
        if b.kind == "user":
            bar = rgb(TH["user"])
            paras = b.text.split("\n")
            if len(paras) > 30:  # a long message (a big paste) shows its start; the model gets all of it
                paras = paras[:20] + [T(f"… +{len(paras) - 20} more lines")]
            ls = []
            for i, para in enumerate(paras):
                ls.extend(wrap_segs([(para, fg)], W - 2, first_prefix=[("› " if i == 0 else "  ", bar)], prefix=[("  ", "")]))
            bg = rgb(TH["sel"], True)
            for l in ls:
                l.segs = [(t, bg + s) for t, s in l.segs]
                l.pad(W, bg)
            return [Line()] + ls + [Line()]
        if b.kind == "assistant":
            return render_md(b.text.strip("\n"), W - 2, 2)
        if b.kind == "reason":
            col = rgb(TH["dim"]) + CSI + "3m"
            if b.done:
                head = Line().add("  ∴ ", rgb(TH["faint"])).add(T(f"thought for {dur(b.elapsed or 0)}"), col)
                head.pre = head.width()
                if b.collapsed is False and self.s.config["settings"].get("show_thinking"):
                    return [head] + wrap_segs([(b.text.strip(), col)], W - 4, 4) + [Line()]
                return [head]
            body = b.text.strip().split("\n")[-4:]
            ls = [Line().add("  ∴ ", rgb(TH["accent"])).add(T("thinking…"), self.shimmer_style(0))]
            for para in body:
                ls.extend(wrap_segs([(para, col)], W - 4, 4))
            return ls[-6:]
        if b.kind == "tool":
            return self._render_tool(b, W)
        if b.kind == "skill":
            return self._render_skill(b, W)
        if b.kind == "stats":
            l = Line().add("  ").add("✓ ", rgb(TH["ok"])).add(b.text, dim)
            l.pre = 4
            return [l, Line()]
        if b.kind == "info":
            col = rgb(TH.get(b.data.get("color", "dim"), TH["dim"]))
            out = []
            for para in b.text.split("\n"):
                out.extend(wrap_segs([(para, col)], W - 2, 2))
            return out
        return []

    def _render_tool(self, b, W):
        dim = rgb(TH["dim"])
        name, label = b.data["name"], b.data["label"].replace("\n", " ⏎ ")
        if not b.done:
            icon = (SPIN[self.frame % len(SPIN)], rgb(TH["accent"]))
        else:
            icon = ("✓", rgb(TH["ok"])) if b.ok else ("✗", rgb(TH["err"]))
        head = Line().add("  ").add(icon[0] + " ", icon[1]).add(name, rgb(TH["fg"]) + CSI + "1m").add(" ")
        room = W - head.width() - 10
        if swidth(label) <= room:
            lbl = label
        elif name in ("read", "write", "edit"):  # keep the file name visible
            lbl = "…" + label[-max(1, room - 1):]
        else:
            lbl = label[: max(0, room - 1)] + "…"
        head.add(lbl, dim)
        head.pre = 4
        head.end = head.width()
        if b.done:
            head.add(f"  {dur(b.elapsed)}", rgb(TH["faint"]))
        out = [head]
        if b.data.get("diff"):
            for dl in b.data["diff"].split("\n")[: (8 if b.collapsed else 40)]:
                col = TH["add"] if dl.startswith("+") else TH["del"] if dl.startswith("-") else TH["dim"]
                out.append(gutter("    │ ", rgb(TH["faint"])).add(dl[: W - 8], rgb(col)))
        elif b.done:
            o = (b.data.get("output") or "").rstrip()
            lines = o.split("\n")
            show = 1 if b.ok and name in ("read", "ls", "grep") else 3 if b.ok else 6
            if b.collapsed:
                for l in lines[:show]:
                    out.append(gutter("    │ ", rgb(TH["faint"])).add(l[: W - 8], rgb(TH["dim"] if b.ok else TH["err"])))
                if len(lines) > show:
                    out.append(gutter("    └ ", rgb(TH["faint"])).add(T(f"{len(lines) - show} more lines"), rgb(TH["faint"])))
            else:
                for l in lines[:200]:
                    out.append(gutter("    │ ", rgb(TH["faint"])).add(l[: W - 8], dim))
        elif name == "bash":
            out.append(gutter("    └ ", rgb(TH["faint"])).add(T(f"running {dur(time.time() - b.t0)}"), dim))
        elif b.status or b.progress:
            if b.status:
                out.append(gutter("    └ ", rgb(TH["faint"])).add(b.status[: W - 10], dim))
            if b.progress and b.progress.get("v") is not None:
                l = Line().add("      ")
                l.segs.extend(self._bar(b.progress["v"], min(40, W - 12), TH["accent"]))
                out.append(l)
        return out

    def _bar(self, frac, width, color):
        if frac is None:  # indeterminate: moving glow
            pos = (self.frame * 0.6) % (width + 6) - 3
            return [("━", rgb(mix(TH["faint"], color, max(0, 1 - abs(i - pos) / 4)))) for i in range(width)]
        frac = max(0.0, min(1.0, frac))
        full = frac * width
        n = int(full)
        k = int((full - n) * 8)
        part = BAR[k] if n < width and k else ""
        segs = [("█" * n, rgb(color)), (part, rgb(color) + rgb(TH["faint"], True)) if part else ("", "")]
        rest = width - n - (1 if part else 0)
        segs.append(("░" * max(0, rest), rgb(TH["faint"])))
        return segs

    def _render_skill(self, b, W):
        color = b.data.get("color") or TH["accent"]
        a = b.data["args"]
        name, act = a.get("name", ""), a.get("action", "")
        border = color if not b.done else (mix(color, TH["bg"], 0.45) if b.ok else TH["err"])
        if not b.done:
            pulse = (1 + __import__("math").sin(self.frame / 3)) / 2
            border = mix(color, TH["fg"], pulse * 0.25)
        bc = rgb(border)
        icon = b.data.get("icon", "◆")
        t = dur(b.elapsed if b.done else time.time() - b.t0)
        if b.collapsed and b.done:
            mark = ("✓", rgb(TH["ok"])) if b.ok else ("✗", rgb(TH["err"]))
            summary = (b.data.get("output") or "").strip().split("\n")[0][: W - 40] if b.ok else (b.data.get("output") or "").split("\n")[0][: W - 40]
            head = Line().add("  ").add(mark[0] + " ", mark[1]).add(f"{icon} ", rgb(color)).add(name, rgb(color) + CSI + "1m") \
                .add(" · ", rgb(TH["dim"])).add(act, rgb(TH["fg"])).add(f"  {t}", rgb(TH["faint"])) \
                .add(("  " + summary) if summary else "", rgb(TH["dim"]))
            head.pre = 4
            return [head] + [gutter("    │ ", rgb(color)).add(pl[: W - 8], rgb(TH["fg"])) for pl in b.data.get("panel", [])[:40]]
        inner = W - 6
        title = f" {icon} {name} · {act} "
        top = Line().add("  ╭─", bc).add(title, rgb(color) + CSI + "1m")
        top.add("─" * max(1, inner - swidth(title) - swidth(t) - 3), bc).add(f" {t} ", rgb(TH["dim"])).add("─╮", bc)
        top.pre = top.width()
        out = [top]

        def row(segs):
            l = gutter("  │ ", bc)
            for s in segs:
                l.add(*s)
            l.pad(W - 2)
            l.add("│", bc)
            return l

        args = "  ".join(f"{k}={json.dumps(v, ensure_ascii=False)}" for k, v in (a.get("args") or {}).items())
        if args:
            out.append(row([(args[: inner - 2], rgb(TH["dim"]))]))
        if b.status and not b.done:
            out.append(row([(SPIN[self.frame % len(SPIN)] + " ", rgb(color)), (b.status[: inner - 4], rgb(TH["fg"]))]))
        p = b.progress
        if p and not b.done:
            lbl = (p.get("label") or "")[:24]
            cnt = f" {p['n']}/{p['total']}" if p.get("total") else (f" {p['n']}" if p.get("n") is not None else "")
            bw = max(10, inner - swidth(lbl) - swidth(cnt) - 6)
            out.append(row([(lbl + " ", rgb(TH["fg"]))] + self._bar(p.get("v"), bw, color) + [(cnt, rgb(TH["dim"]))]))
        for pl in b.data.get("panel", [])[-12:]:
            out.append(row([(pl[: inner - 2], rgb(TH["fg"]))]))
        for ev in b.events[-3:]:
            out.append(row([("› ", rgb(color)), (ev[: inner - 4], rgb(TH["dim"]))]))
        if b.done and not b.collapsed:
            for l in (b.data.get("output") or "").split("\n")[:30]:
                out.append(row([(l[: inner - 2], rgb(TH["fg"] if b.ok else TH["err"]))]))
        if len(out) == 1:
            out.append(row([(SPIN[self.frame % len(SPIN)] + " " + T("starting…"), rgb(TH["dim"]))]))
        if b.data.get("cli") and b.done:  # status bar: full, red-family while ok, error color when the command failed
            col = color if b.ok else TH["err"]
            msg = f" {'ok' if b.ok else 'failed'} · {t}"
            out.append(row(self._bar(1.0, max(10, inner - swidth(msg) - 2), col) + [(msg, rgb(col))]))
        bottom = Line().add("  ╰" + "─" * (W - 5) + "╯", bc)
        bottom.pre = bottom.width()
        out.append(bottom)
        return out

    def shimmer_style(self, i):
        return rgb(TH["accent"])

    def shimmer(self, text):
        segs = []
        pos = (self.frame * 0.7) % (len(text) + 10) - 5
        for i, ch in enumerate(text):
            t = max(0.0, 1 - abs(i - pos) / 4)
            segs.append((ch, rgb(mix(TH["dim"], TH["fg"], t))))
        return segs

    def input_lines(self, width):
        """Returns Lines for the input box and cursor (row, col) inside it."""
        inner = width - 4
        text = self.input
        rows, cur_rc = [[]], (0, 0)
        col = 0
        for i, ch in enumerate(text + "\0"):
            if i == self.cur:
                cur_rc = (len(rows) - 1, col)
            if ch == "\0":
                break
            if ch == "\n":
                rows.append([])
                col = 0
                continue
            w = cw(ch)
            if col + w > inner:
                rows.append([])
                col = 0
                if i == self.cur:
                    cur_rc = (len(rows) - 1, 0)
            rows[-1].append(ch)
            col += w
        return ["".join(r) for r in rows], cur_rc

    def todo_lines(self, W, H):
        """Live checklist above the input while there is open work (the agent's todo tool)."""
        items = self.agent.session.get("todo") or []
        g = self.agent.session.get("goal") or {}
        # only while the agent works: a finished turn leaves no checklist behind, even if some items were never ticked
        if not items or not (self.agent.busy or g.get("status") == "working"):
            return []
        done = sum(1 for i in items if i["s"] == "done")
        head = Line().add(" ").add("todo ", rgb(TH["accent"]) + CSI + "1m").add(f"{done}/{len(items)}", rgb(TH["dim"]))
        if g.get("status") == "working":
            head.add(T(f"  ◎ goal: {g['text'][: max(10, W - 30)]}"), rgb(TH["accent2"]))
        cap = max(2, min(7, H // 4))
        cur = next((k for k, i in enumerate(items) if i["s"] != "done"), len(items))
        lo = max(0, min(cur - 1, len(items) - cap))
        out = [head]
        for i in items[lo:lo + cap]:
            sym, col = {"done": ("✓", TH["ok"]), "doing": ("▸", TH["accent"])}.get(i["s"], ("○", TH["faint"]))
            out.append(Line().add("  " + sym + " ", rgb(col)).add(i["t"][: W - 6], rgb(TH["dim"] if i["s"] == "done" else TH["fg"])))
        if len(items) > lo + cap:
            out.append(Line().add(T(f"    … {len(items) - lo - cap} more"), rgb(TH["faint"])))
        return out

    def status_line(self, W):
        if W < 90:
            return self.status_compact(W)
        m = self.agent.model()
        dim, faint = rgb(TH["dim"]), rgb(TH["faint"])
        L = Line().add(" ")
        if m:
            L.add("● ", rgb(TH["accent2"])).add(m["name"], rgb(TH["fg"]))
            if m.get("effort") and m["effort"] != "none":
                L.add(f" · {m['effort']}", dim)
        else:
            L.add(T("no model — /provider"), rgb(TH["warn"]))
        ctx = int(m.get("context") or 0) if m else 0
        used = self.agent.ctx_used
        L.add("   ")
        if ctx:
            frac = min(1, used / ctx)
            col = TH["ok"] if frac < 0.6 else TH["warn"] if frac < 0.85 else TH["err"]
            L.add("ctx ", dim)
            L.segs.extend(self._bar(frac, 10, col))
            L.add(f" {human(used)}/{human(ctx)} {int(frac * 100)}%", dim)
        else:
            L.add(f"ctx {human(used)}", dim)
        u = self.agent.session.get("usage", {})
        L.add("   ↑", faint).add(human(u.get("in", 0)), dim).add(" ↓", faint).add(human(u.get("out", 0)), dim)
        if u.get("cost"):
            L.add("  " + money(u["cost"]), dim)
        right = Line()
        ctxf = self.agent.context_files()
        if ctxf:
            right.add("◆ " + " ".join(ctxf) + "  ", rgb(TH["ok"]))
        else:
            right.add(T("◇ no AGENTS.md (/init)") + "  ", rgb(TH["faint"]))
        for segs in PLUGINS.collect("status", self):
            for t, c in segs:
                right.add(t, rgb(TH.get(c, c) if not str(c).startswith("#") else c))
            right.add("  ")
        pm = perm_mode(self.s.config["settings"])
        if pm != "ask":
            right.add(T("full" if pm == "full" else "read-only") + " ", rgb(TH["warn"] if pm == "full" else TH["accent2"]))
        cs = self.cloud.status
        if self.cloud.online:
            sym = {"synced": ("≈ ✓", TH["ok"]), "syncing": ("≈ " + SPIN[self.frame % len(SPIN)] + " ", TH["accent"]),
                   "error": ("≈ !", TH["err"])}.get(cs, ("≈", TH["dim"]))
            right.add(sym[0] + " ", rgb(sym[1])).add(self.s.auth.get("user", ""), dim)
        else:
            right.add(T("≈ offline"), faint)
        if self.update_avail:
            right.add(f"  ↑ v{self.update_avail} /update", rgb(TH["accent"]))
        right.add(" ")
        gap = W - L.width() - right.width()
        if gap > 0:
            L.add(" " * gap)
            L.segs.extend(right.segs)
        return L

    def status_compact(self, W):
        """Narrow terminals (Termux on a phone): model · ctx% · cost · cloud, nothing that wraps."""
        m = self.agent.model()
        dim, faint = rgb(TH["dim"]), rgb(TH["faint"])
        L = Line().add(" ")
        if m:
            L.add("● ", rgb(TH["accent2"])).add(m["name"][: max(6, W // 3)], rgb(TH["fg"]))
        else:
            L.add(T("no model"), rgb(TH["warn"]))
        ctx = int(m.get("context") or 0) if m else 0
        used = self.agent.ctx_used
        if ctx:
            frac = min(1, used / ctx)
            L.add(f" {int(frac * 100)}%", rgb(TH["ok"] if frac < 0.6 else TH["warn"] if frac < 0.85 else TH["err"]))
        cost = self.agent.session.get("usage", {}).get("cost", 0)
        if cost and W >= 44:
            L.add(" " + money(cost), dim)
        right = Line()
        pm = perm_mode(self.s.config["settings"])
        if pm != "ask":
            right.add("full " if pm == "full" else "ro ", rgb(TH["warn"] if pm == "full" else TH["accent2"]))
        if self.cloud.online:
            sym = {"synced": ("≈ ✓", TH["ok"]), "syncing": ("≈ " + SPIN[self.frame % len(SPIN)], TH["accent"]), "error": ("≈ !", TH["err"])}.get(
                self.cloud.status, ("≈", TH["dim"]))
            right.add(sym[0], rgb(sym[1]))
        else:
            right.add("≈–", faint)
        if self.update_avail:
            right.add(" ↑", rgb(TH["accent"]))
        right.add(" ")
        gap = W - L.width() - right.width()
        if gap > 0:
            L.add(" " * gap)
            L.segs.extend(right.segs)
        return L

    def draw(self):
        W, H = self.term.size()
        bgc = rgb(TH["bg"], True)
        with self.lock:
            # input box
            mode = self.agent.preset().get("label") or self.agent.preset()["name"]
            pre = f" {mode} › "
            pw = swidth(pre)
            in_rows, (cr, cc) = self.input_lines(W - 2 - (pw - 3))
            first = max(0, min(cr, len(in_rows) - 8))  # keep the cursor row inside the 8 visible rows
            in_rows = in_rows[first:first + 8]
            cr = max(0, min(cr - first, len(in_rows) - 1))
            busy = self.agent.busy
            box = []
            bcol = rgb(TH["accent"] if not busy else TH["faint"])
            box.append(Line().add("╭" + "─" * (W - 2) + "╮", bcol))
            for i, r in enumerate(in_rows):
                l = Line().add("│", bcol)
                if i == 0:
                    l.add(" " + mode, rgb(TH["accent"]) + CSI + "1m").add(" › ", rgb(TH["faint"]))
                else:
                    l.add(" " * pw)
                if not self.input and i == 0:
                    l.add(T("ask anything, / for commands" if not busy else "queue a message…"), rgb(TH["faint"]))
                else:
                    l.add(r, rgb(TH["fg"]))
                l.add(" " * max(0, W - 1 - l.width())).add("│", bcol)
                box.append(l)
            box.append(Line().add("╰" + "─" * (W - 2) + "╯", bcol))
            # activity line
            act = Line().add(" ")
            if busy:
                el = time.time() - self.turn_t0
                act.add(SPIN[self.frame % len(SPIN)] + " ", rgb(TH["accent"]))
                word = "working" if self.tools_live else "thinking" if not self.cur_text else "writing"
                act.segs.extend(self.shimmer(T(word + "…")))
                act.add(f"  {dur(el)}", rgb(TH["dim"]))
                if any(self.turn_tokens):
                    act.add(f"  ↑{human(self.turn_tokens[0])} ↓{human(self.turn_tokens[1])}", rgb(TH["dim"]))
                act.add("   " + T("esc to interrupt"), rgb(TH["faint"]))
            else:
                msg = self.notice
                if msg[0] and time.time() - msg[1] < 4:
                    act.add(msg[0], rgb(TH.get(msg[2], TH["dim"])))
            status = self.status_line(W)
            bottom = self.todo_lines(W, H) + [act] + box + [status]
            # suggestions popup
            pop = []
            if self.sugg and not self.modal:
                show = self.sugg[:8]
                start = max(0, min(self.sel - 3, len(self.sugg) - 8))
                show = self.sugg[start:start + 8]
                for i, (val, lab, det) in enumerate(show):
                    selected = start + i == self.sel
                    l = Line().add("  ")
                    st = rgb(TH["sel"], True) if selected else ""
                    l.add((" ▸ " if selected else "   ") + lab.ljust(18), st + rgb(TH["accent"] if selected else TH["fg"]))
                    l.add(" " + det[: W - 30], st + rgb(TH["dim"]))
                    pop.append(l)
            avail = max(1, H - len(bottom) - len(pop))
            # transcript
            lines, owners = [], []
            for b in self.blocks:
                bl = self.block_lines(b, W - 1)
                lines.extend(bl)
                owners.extend([b] * len(bl))
            maxscroll = max(0, len(lines) - avail)
            self.scroll = min(self.scroll, maxscroll)
            end = len(lines) - self.scroll
            top = end - avail  # transcript line on screen row 1 (negative while the transcript is shorter than the screen)
            view = lines[max(0, top):end]
            view = [Line()] * (avail - len(view)) + view
            self._tx = (lines, owners, top, avail, maxscroll)
            if self.scroll and view:
                view[0] = Line().add(T(f" ↑ {self.scroll} more lines below · pgdn "), rgb(TH["accent"]))
            screen = view + pop + bottom
            out = []
            # repaint only the rows that changed (a full repaint every frame flickers badly in the Windows console)
            sel = self.sel_range()
            rows = []
            for r, l in enumerate(screen[:H]):
                hl = None
                if sel and r < avail and sel[0][0] <= top + r <= sel[1][0]:
                    i = top + r
                    hl = (sel[0][1] if i == sel[0][0] else 0, sel[1][1] + 1 if i == sel[1][0] else W)
                rows.append(l.render(W, bgc, hl))
            prev = getattr(self, "_prev_rows", None)
            full = prev is None or len(prev) != len(rows) or getattr(self, "_prev_wh", None) != (W, H) or self.modal or getattr(self, "_prev_modal", False)
            for i, r in enumerate(rows):
                if full or prev[i] != r:
                    out.append(f"{CSI}{i + 1};1H" + r)
            self._prev_rows, self._prev_wh, self._prev_modal = rows, (W, H), bool(self.modal)
            changed = bool(out)
            out.insert(0, CSI + "?2026h" + CSI + "?25l")  # synchronized update (Windows Terminal, kitty…); hide the cursor while painting
            # modal overlay
            if self.modal:
                out.append(self._draw_modal(W, H, bgc))
            # cursor
            if self.modal and self.modal["kind"] == "input":
                out.append(f"{CSI}?25h")
            elif not self.modal:
                row = H - 1 - len(box) + 1 + cr + 1
                col = 1 + pw + cc + 1
                out.append(f"{CSI}{row};{col}H{CSI}?25h")
            else:
                out.append(f"{CSI}?25l")
            out.append(CSI + "?2026l")
            frame = "".join(out)
            if changed or self.modal or frame != getattr(self, "_prev_frame_tail", None):
                self.term.write(frame)  # an unchanged frame writes nothing: cursor hide/show every tick flickered in cmd.exe
            self._prev_frame_tail = frame if not changed else None
            self.dirty = False

    def _draw_modal(self, W, H, bgc):
        m = self.modal
        mw = min(W - 4, 90)
        x = (W - mw) // 2 + 1
        mbg = rgb(TH["bg2"], True)
        bc = rgb(TH["accent"])
        rows = []
        title = m.get("title", "")
        if m["kind"] == "pick":
            flt = m["filter"].lower()
            items = [it for it in m["items"] if fuzzy(flt, it[1] + " " + it[2]) is not None] if flt else m["items"]
            m["_visible"] = items
            m["sel"] = max(0, min(m["sel"], len(items) - 1))
            rows.append(Line().add(" " + title, rgb(TH["fg"]) + CSI + "1m"))
            rows.append(Line().add(" ⌕ ", rgb(TH["accent"])).add(m["filter"] or T("type to filter"), rgb(TH["fg"] if m["filter"] else TH["faint"])))
            rows.append(Line())
            maxn = max(3, H - 12)
            start = max(0, min(m["sel"] - maxn // 2, len(items) - maxn))
            for i, (v, lab, det) in enumerate(items[start:start + maxn]):
                selected = start + i == m["sel"]
                st = rgb(TH["sel"], True) if selected else ""
                l = Line().add((" ▸ " if selected else "   "), st + rgb(TH["accent"]))
                l.add(lab, st + rgb(TH["fg"]) + (CSI + "1m" if selected else ""))
                if det:
                    room = mw - 4 - l.width()
                    if room > 3:  # a long detail is cut to the box, never drawn over its border
                        d = "  " + det
                        while swidth(d) > room:
                            d = d[:-2] + "…" if swidth(d) > room + 1 else d[:-1]
                        l.add(d, st + rgb(TH["dim"]))
                if selected:
                    l.add(" " * max(0, mw - 2 - l.width()), st)
                rows.append(l)
            if not items:
                rows.append(Line().add("   " + T("no matches"), rgb(TH["dim"])))
            rows.append(Line())
            rows.append(Line().add(" " + T("↑↓ select · enter confirm · esc cancel") + (" · " + m["hint"] if m.get("hint") else ""), rgb(TH["faint"])))
        elif m["kind"] == "confirm":
            rows.append(Line().add(" " + T("⚠ allow") + " ", rgb(TH["warn"]) + CSI + "1m").add(title[: mw - 12], rgb(TH["fg"])))
            if m.get("detail"):
                rows.append(Line())
                for dl in m["detail"].split("\n")[:14]:
                    col = TH["add"] if dl.startswith("+") else TH["del"] if dl.startswith("-") else TH["dim"]
                    rows.append(Line().add("   " + dl[: mw - 6], rgb(col)))
            rows.append(Line())
            for i, (v, lab) in enumerate(m["items"]):
                selected = i == m["sel"]
                st = rgb(TH["sel"], True) if selected else ""
                rows.append(Line().add((" ▸ " if selected else "   ") + f"{i + 1}. {lab}", st + rgb(TH["accent"] if selected else TH["fg"])))
            rows.append(Line())
            rows.append(Line().add(" " + T("y/n · ↑↓ enter · esc deny"), rgb(TH["faint"])))
        elif m["kind"] == "input":
            rows.append(Line().add(" " + title, rgb(TH["fg"]) + CSI + "1m"))
            if m.get("hint"):
                rows.append(Line().add(" " + m["hint"], rgb(TH["dim"])))
            rows.append(Line())
            txt = m["text"] if not m["secret"] else "•" * len(m["text"])
            lines = txt.split("\n") if m["multiline"] else [txt]
            # cursor position
            before = (m["text"] if not m["secret"] else "•" * len(m["text"]))[: m["cur"]]
            crow = before.count("\n")
            ccol = swidth(before.split("\n")[-1])
            maxw = mw - 6
            first = max(0, crow - (H - 14))
            for i, l in enumerate(lines[first:first + max(1, H - 12)]):
                vis = l
                off = 0
                if i + first == crow and ccol > maxw - 1:
                    off = ccol - maxw + 1
                    vis = l[off:]
                rows.append(Line().add(" › " if i + first == 0 else "   ", rgb(TH["accent"])).add(vis[:maxw], rgb(TH["fg"])))
                if i + first == crow:
                    m["_cur"] = (len(rows) - 1, 3 + ccol - off)
            rows.append(Line())
            rows.append(Line().add(" " + T("enter newline · ctrl+s save · esc cancel" if m["multiline"] else "enter confirm · esc cancel"), rgb(TH["faint"])))
        h = len(rows) + 2
        y = max(1, (H - h) // 2)
        out = [f"{CSI}{y};{x}H" + Line().add("╭" + "─" * (mw - 2) + "╮", bc).render(mw, mbg)]
        for i, r in enumerate(rows):
            line = Line().add("│", bc)
            line.segs.extend(r.segs)
            pad = mw - 1 - line.width()
            if pad < 0:
                line = Line(line.segs)
            line.add(" " * max(0, pad))
            line.add("│", bc)
            out.append(f"{CSI}{y + 1 + i};{x}H" + line.render(mw, mbg))
        out.append(f"{CSI}{y + h - 1};{x}H" + Line().add("╰" + "─" * (mw - 2) + "╯", bc).render(mw, mbg))
        if m["kind"] == "input" and "_cur" in m:
            r, c = m["_cur"]
            out.append(f"{CSI}{y + 1 + r};{x + 1 + c}H")
        return "".join(out)


def gutter(text, style):
    """A line that starts with decoration (tool output bar, box border): left out when the line is copied."""
    l = Line().add(text, style)
    l.pre = swidth(text)
    return l


# ───── TUI: keyboard + suggestions (mixin) ─────

PASTE_FOLD, PASTE_FOLD_LINES = 1500, 15  # pastes bigger than this show as one [paste #n · N lines] marker in the input


class TUIInput:
    # ─── input handling ───
    def handle_key(self, k):
        if isinstance(k, tuple) and k[0] == "mouse":
            if not self.modal:
                self.mouse(*k[1:])
            return
        if k not in ("wheel-up", "wheel-down"):
            self.msel = None  # typing ends a mouse selection
        if self.modal:
            if k in ("wheel-up", "wheel-down") and self.modal["kind"] in ("pick", "confirm"):
                k = "up" if k == "wheel-up" else "down"
            return self.modal_key(k)
        if PLUGINS.emit("key", self, k):
            return
        if k in ("ctrl-v", "alt-v"):
            return self.paste_clipboard()
        if k == "alt-c":
            return self.copy_text(next((b.text for b in reversed(self.blocks) if b.kind == "assistant"), ""), "last answer")
        if isinstance(k, tuple) and k[0] in ("paste", "char"):
            k = (k[0], clean_text(k[1]))
            if not k[1] and k[0] == "char":
                return
        if isinstance(k, tuple) and k[0] == "paste":
            if len(k[1]) > PASTE_FOLD or k[1].count("\n") >= PASTE_FOLD_LINES:  # a big paste: one short marker in the input
                self.pastes.append(k[1])
                self.insert(f"[paste #{len(self.pastes)} · {k[1].count(chr(10)) + 1} lines]")
                return self.update_sugg()
            imgs = image_paths(k[1])
            if imgs:  # a file dragged into the terminal: attach it instead of pasting its path
                for p in imgs:
                    self.attach_image(p)
                return self.update_sugg()
            if not k[1].strip():  # some terminals paste nothing when the clipboard holds only an image
                return self.paste_clipboard(text_too=False)
        if k == "ctrl-c":
            if self.agent.busy:
                self.agent.cancel.set()
            elif self.input:
                self.input, self.cur = "", 0
            else:
                self.running = False
        elif k == "ctrl-d" and not self.input:
            self.running = False
        elif k == "esc":
            if self.sugg:
                self.sugg = []
            elif self.agent.busy:
                self.agent.cancel.set()
        elif k == "enter":
            if self.sugg and self.input.startswith("/") and " " not in self.input.strip():
                self.accept_sugg()
                if self.input.strip() in [f"/{c}" for c, _ in self.all_commands()]:
                    self.submit()
                return
            if self.sugg and self.sel < len(self.sugg) and "@" in self.input:
                self.accept_sugg()
                return
            if self.input.endswith("\\"):
                self.insert("\n", replace_last=True)
                return
            self.submit()
        elif k == "newline":
            self.insert("\n")
        elif k == "tab":
            if self.sugg:
                self.accept_sugg()
        elif k == "backspace":
            if self.cur:
                self.input = self.input[: self.cur - 1] + self.input[self.cur:]
                self.cur -= 1
        elif k == "delete":
            self.input = self.input[: self.cur] + self.input[self.cur + 1:]
        elif k == "left":
            self.cur = max(0, self.cur - 1)
        elif k == "right":
            self.cur = min(len(self.input), self.cur + 1)
        elif k in ("home", "ctrl-a"):
            self.cur = self.input.rfind("\n", 0, self.cur) + 1
        elif k in ("end", "ctrl-e"):
            e = self.input.find("\n", self.cur)
            self.cur = len(self.input) if e < 0 else e
        elif k == "ctrl-u":
            s = self.input.rfind("\n", 0, self.cur) + 1
            self.input, self.cur = self.input[:s] + self.input[self.cur:], s
        elif k == "ctrl-k":
            e = self.input.find("\n", self.cur)
            self.input = self.input[: self.cur] + (self.input[e:] if e >= 0 else "")
        elif k == "ctrl-w":
            s = self.cur
            while s and self.input[s - 1] == " ":
                s -= 1
            while s and self.input[s - 1] not in " \n":
                s -= 1
            self.input, self.cur = self.input[:s] + self.input[self.cur:], s
        elif k == "ctrl-left":
            s = self.cur
            while s and not self.input[s - 1].isalnum():
                s -= 1
            while s and self.input[s - 1].isalnum():
                s -= 1
            self.cur = s
        elif k == "ctrl-right":
            s, n = self.cur, len(self.input)
            while s < n and not self.input[s].isalnum():
                s += 1
            while s < n and self.input[s].isalnum():
                s += 1
            self.cur = s
        elif k == "ctrl-l":
            self.term.write(CSI + "2J")
            self._prev_rows = None
        elif k == "ctrl-o":
            for b in reversed(self.blocks):
                if b.kind in ("tool", "skill", "reason") and b.done:
                    b.collapsed = not b.collapsed
                    if b.kind == "reason":
                        self.s.config["settings"]["show_thinking"] = not b.collapsed
                    b.touch()
                    break
        elif k in ("up", "down"):
            if self.sugg:
                self.sel = (self.sel + (1 if k == "down" else -1)) % len(self.sugg)
                return
            line_start = self.input.rfind("\n", 0, self.cur) + 1
            if k == "up" and line_start > 0:
                prev_start = self.input.rfind("\n", 0, line_start - 1) + 1
                self.cur = min(line_start - 1, prev_start + (self.cur - line_start))
                return
            nl = self.input.find("\n", self.cur)
            if k == "down" and nl >= 0:
                nxt_end = self.input.find("\n", nl + 1)
                self.cur = min(nxt_end if nxt_end >= 0 else len(self.input), nl + 1 + (self.cur - line_start))
                return
            if not self.hist:
                return
            if self.hpos is None:
                if k == "down":
                    return
                self.hpos = len(self.hist)
                self._draft = self.input
            self.hpos += -1 if k == "up" else 1
            self.hpos = max(0, self.hpos)
            if self.hpos >= len(self.hist):
                self.hpos = None
                self.input = getattr(self, "_draft", "")
            else:
                self.input = self.hist[self.hpos]
            self.cur = len(self.input)
            return
        elif k in ("pgup", "wheel-up", "ctrl-up"):
            self.scroll += (self.term.size()[1] - 8) if k == "pgup" else 3
        elif k in ("pgdn", "wheel-down", "ctrl-down"):
            self.scroll = max(0, self.scroll - ((self.term.size()[1] - 8) if k == "pgdn" else 3))
        elif isinstance(k, tuple):
            if k[0] == "char":
                self.insert(k[1])
            elif k[0] == "paste":
                self.insert(k[1].replace("\r\n", "\n").replace("\r", "\n"))
        self.update_sugg()

    # ─── mouse: drag selects transcript text and copies it, click toggles a tool card, right click pastes (Windows) ───
    def sel_range(self):
        sel = getattr(self, "msel", None)
        if not sel or sel["a"] == sel["b"] and not sel.get("word"):
            return None
        return tuple(sorted([sel["a"], sel["b"]]))

    def mouse(self, kind, btn, x, y):
        tx = getattr(self, "_tx", None)
        if not tx:
            return
        lines, owners, top, avail, maxscroll = tx

        def point(row, col):
            return max(0, min(len(lines) - 1, top + min(max(row, 1), avail) - 1)), max(0, col - 1)

        if kind == "press" and btn == 2:
            if self.sel_range():
                self.copy_selection()
            else:  # the terminal's own right-click menu / paste is off while we own the mouse
                self.paste_clipboard()
            return
        if btn != 0 or not lines:
            return
        if kind == "press":
            if y > avail:
                self.msel = None
                return
            p, now = point(y, x), time.time()
            last = getattr(self, "_click", None)
            n = last[2] + 1 if last and now - last[0] < 0.45 and last[1][0] == p[0] and abs(last[1][1] - p[1]) <= 1 else 1
            self._click = (now, p, n)
            if n == 2:
                a, b = word_at(lines[p[0]], p[1])
                self.msel = {"a": (p[0], a), "b": (p[0], b), "word": True}
                self.copy_selection()
            elif n >= 3:  # the whole logical line, across soft wraps
                i0, i1 = p[0], p[0]
                while i0 > 0 and lines[i0].soft is not None:
                    i0 -= 1
                while i1 + 1 < len(lines) and lines[i1 + 1].soft is not None:
                    i1 += 1
                self.msel = {"a": (i0, 0), "b": (i1, 1 << 20), "word": True}
                self.copy_selection()
            else:
                self.msel = {"a": p, "b": p, "drag": True}
        elif kind == "drag" and getattr(self, "msel", None) and self.msel.get("drag"):
            if y <= 1 and self.scroll < maxscroll:  # dragging past the edge scrolls
                self.scroll += 1
            elif y >= avail and self.scroll > 0:
                self.scroll -= 1
            self.msel["b"] = point(y, x)
        elif kind == "release" and getattr(self, "msel", None) and self.msel.get("drag"):
            self.msel["drag"] = False
            if self.msel["a"] != self.msel["b"]:
                self.copy_selection()
                return
            self.msel = None
            i = point(y, x)[0]
            b = owners[i] if 0 <= top + y - 1 < len(owners) else None
            if b and b.kind in ("tool", "skill", "reason") and b.done:  # click a card to open / close it
                b.collapsed = not b.collapsed
                if b.kind == "reason":
                    self.s.config["settings"]["show_thinking"] = not b.collapsed
                b.touch()

    def copy_selection(self):
        r = self.sel_range()
        if r:
            text = selection_text(self._tx[0], r[0], r[1])
            if text.strip():
                self.copy_text(text)

    def insert(self, s, replace_last=False):
        if replace_last:
            self.input = self.input[:-1]
            self.cur = len(self.input)
        self.input = self.input[: self.cur] + s + self.input[self.cur:]
        self.cur += len(s)
        self.hpos = None

    def update_sugg(self):
        old = [x[0] for x in self.sugg]
        self.sugg = []
        t = self.input[: self.cur]
        if t.startswith("/") and "\n" not in t:
            parts = t[1:].split(" ", 1)
            if len(parts) == 1:
                q = parts[0].lower()
                scored = [(fuzzy(q, c), c, d) for c, d in self.all_commands()]
                self.sugg = [("/" + c, "/" + c, d) for s, c, d in sorted((x for x in scored if x[0] is not None), key=lambda x: x[0])]
            else:
                cmd, arg = parts
                opts = []
                if cmd == "model":
                    opts = [(m["name"], m["name"], f"{m['id']} · {human(m.get('context') or 0)} ctx") for m in self.s.config["models"]]
                elif cmd == "effort":
                    opts = [(e, e, "") for e in EFFORTS]
                if opts:
                    self.sugg = [(f"/{cmd} {v}", l, d) for v, l, d in opts if fuzzy(arg.lower(), l.lower()) is not None]
        else:
            m = re.search(r"@([^\s@]*)$", t)
            if m:
                q = m.group(1).lower()
                if self.files_cache is None or time.time() - self.files_cache[0] > 10:
                    self.files_cache = (time.time(), [os.path.relpath(f) for f in walk_files(".", 5000)])
                scored = [(fuzzy(q, f.lower()), f) for f in self.files_cache[1]]
                best = sorted((x for x in scored if x[0] is not None), key=lambda x: (x[0], len(x[1])))[:50]
                self.sugg = [("@" + f, f, "") for _, f in best]
        if [x[0] for x in self.sugg] != old:
            self.sel = 0

    def accept_sugg(self):
        if not self.sugg:
            return
        val = self.sugg[self.sel][0]
        t = self.input[: self.cur]
        if val.startswith("@"):
            start = t.rfind("@")
            self.input = self.input[:start] + val + " " + self.input[self.cur:]
            self.cur = start + len(val) + 1
        else:
            self.input = val + (" " if val.count(" ") == 0 and any(c in val for c in ("/model", "/effort")) else "")
            self.cur = len(self.input)
        self.sugg = []

    def modal_key(self, k):
        m = self.modal

        def close(v):
            m["value"] = v
            self.modal = None
            m["ev"].set()

        if m["kind"] in ("pick", "confirm"):
            items = m.get("_visible", m["items"]) if m["kind"] == "pick" else m["items"]
            if k in ("esc", "ctrl-c"):
                close(None if m["kind"] == "pick" else "n")
            elif k in ("up", "down"):
                m["sel"] = (m["sel"] + (1 if k == "down" else -1)) % max(1, len(items))
                if m.get("preview") and items:
                    m["preview"](items[m["sel"]][0])
            elif k in ("enter", "tab"):
                if items:
                    close(items[m["sel"]][0])
            elif m["kind"] == "confirm" and isinstance(k, tuple) and k[0] == "char":
                c = k[1].lower()
                if c in "yna":
                    close(c)
                elif c.isdigit() and 0 < int(c) <= len(items):
                    close(items[int(c) - 1][0])
            elif m["kind"] == "pick":
                if k == "backspace":
                    m["filter"] = m["filter"][:-1]
                    m["sel"] = 0
                elif isinstance(k, tuple):
                    m["filter"] += k[1].replace("\n", "")
                    m["sel"] = 0
        elif m["kind"] == "input":
            t, c = m["text"], m["cur"]
            if k in ("esc", "ctrl-c"):
                close(None)
            elif k == "enter" and not m["multiline"]:
                close(t)
            elif k == "ctrl-s" or (k == "enter" and m["multiline"] and t.endswith("\n\n\n")):
                close(t.rstrip("\n") if k != "ctrl-s" else t)
            elif k in ("enter", "newline") and m["multiline"]:
                m["text"], m["cur"] = t[:c] + "\n" + t[c:], c + 1
            elif k == "backspace" and c:
                m["text"], m["cur"] = t[: c - 1] + t[c:], c - 1
            elif k == "delete":
                m["text"] = t[:c] + t[c + 1:]
            elif k == "left":
                m["cur"] = max(0, c - 1)
            elif k == "right":
                m["cur"] = min(len(t), c + 1)
            elif k in ("home", "ctrl-a"):
                m["cur"] = 0 if not m["multiline"] else t.rfind("\n", 0, c) + 1
            elif k in ("end", "ctrl-e"):
                m["cur"] = len(t) if not m["multiline"] or t.find("\n", c) < 0 else t.find("\n", c)
            elif k == "ctrl-u":
                m["text"], m["cur"] = t[c:], 0
            elif k in ("up", "down") and m["multiline"]:
                ls = t.rfind("\n", 0, c) + 1
                if k == "up" and ls:
                    ps = t.rfind("\n", 0, ls - 1) + 1
                    m["cur"] = min(ls - 1, ps + c - ls)
                elif k == "down":
                    nl = t.find("\n", c)
                    if nl >= 0:
                        ne = t.find("\n", nl + 1)
                        m["cur"] = min(ne if ne >= 0 else len(t), nl + 1 + c - ls)
            elif isinstance(k, tuple):
                s = k[1] if m["multiline"] else k[1].replace("\n", " ")
                m["text"], m["cur"] = t[:c] + s + t[c:], c + len(s)

    def expand_pastes(self, text):
        """[paste #n · N lines] markers → the pasted text."""
        ps = getattr(self, "pastes", [])
        return re.sub(r"\[paste #(\d+) · \d+ lines\]", lambda m: ps[int(m.group(1)) - 1] if 0 < int(m.group(1)) <= len(ps) else m.group(0), text)

    def submit(self):
        shown = self.input.strip()
        text = self.expand_pastes(shown)
        self.input, self.cur, self.sugg, self.hpos, self.pastes = "", 0, [], None, []
        if not text:
            return
        if not self.hist or self.hist[-1] != text:
            self.hist.append(text)
            try:  # a huge paste is not worth keeping whole in the recall history
                (self.s.home / "history").write_text("\0".join(h[:20000] for h in self.hist[-500:]), "utf-8")
            except (OSError, UnicodeError):
                pass
        self.scroll = 0
        if text.startswith("/"):
            cmd, _, arg = text[1:].partition(" ")
            self.bg(self.command, cmd.strip().lower(), arg.strip())
            return
        if text.startswith("!") and text[1:].strip():
            self.bg(self.run_shell_escape, text[1:].strip())
            return
        if self.agent.busy or self.launching or self.pending:
            self.pending.append(text)
            self.info(f"◌ queued ({len(self.pending)}): {shown[:100]}", "dim")
            return
        self.dispatch(text, shown)

    # ─── clipboard, images, mouse selection ───
    def copy_text(self, text, what="selection"):
        if not text:
            return self.toast("nothing to copy")
        how = clip_copy(text, self.term)
        self.toast(f"copied {what} · {len(text)} chars" + (" (via terminal)" if how == "osc52" else "") if how else
                   "copy failed: no clipboard here", "ok" if how else "err")

    def attach_image(self, path):
        with self.lock:
            self.attached.append(path)
            self.insert(f"[image #{len(self.attached)}] ")
        m = self.agent.model() or {}
        self.toast(f"image attached: {os.path.basename(path)}" + ("" if m.get("vision", True) else " — this model has vision off (/models)"),
                   "ok" if m.get("vision", True) else "warn")

    def paste_clipboard(self, text_too=True):
        """ctrl+v / alt+v / right click: clipboard text is pasted (into a dialog too); with no text, an image is attached."""
        def run():
            txt = clip_paste() if text_too else None
            if txt:
                txt = txt.replace("\r\n", "\n").replace("\r", "\n")
                with self.lock:
                    m = self.modal
                    if m and m["kind"] == "input":
                        t = txt if m["multiline"] else txt.replace("\n", " ")
                        m["text"], m["cur"] = m["text"][:m["cur"]] + t + m["text"][m["cur"]:], m["cur"] + len(t)
                    elif not m:
                        self.handle_key(("paste", txt))  # same path as a terminal paste: big pastes fold into a marker
                self.redraw()
                return
            img = clip_image(self.s.home / "pastes")
            if img:
                return self.attach_image(img)
            self.toast("clipboard is empty" + ("" if WIN or sys.platform == "darwin" else " (Linux needs wl-clipboard or xclip)"), "warn")
            self.redraw()
        threading.Thread(target=run, daemon=True).start()

    def dispatch(self, text, shown=None):
        images = []
        for ref in re.findall(r"@(\S+)", text):
            p = Path(os.path.join(self.agent.cwd, os.path.expanduser(ref)))
            if p.suffix.lower() in IMAGE_EXT and p.exists():
                images.append(read_any(p)[1])
        for i, path in enumerate(self.attached, 1):
            if f"[image #{i}]" in text and os.path.isfile(path):
                images.append(read_any(path)[1])
        self.attached = []
        self.add("user", text=shown or text)
        self.ensure_ws()
        if not self.agent.model():
            self.info("no model yet — let's set one up", "warn")
            self.bg(self.setup_provider)
            return
        self.launching = True
        self.bg(self._run_turn, text, images)

    def _run_turn(self, text, images):
        try:
            self.agent.run(text, images)
        finally:
            self.launching = False

    def retheme(self, name=None):
        apply_theme(self.s, name)
        with self.lock:
            for b in self.blocks:
                b.touch()
        self.redraw()

    def _on_remote_change(self, n):
        cfg = jload(self.s.cfg_path)
        if cfg:
            self.s.config = cfg
            if self.s.dedupe_models():
                self.s.save()
            set_ui_lang(cfg.get("settings") or {})
            self.retheme()
        if self.agent.refresh_from_disk(os.getcwd()):
            self.render_history()
            self.toast("☁ session updated from another device", "ok")
        else:
            self.toast(f"☁ {n} updates from cloud", "ok")
        if not self.agent.busy:
            self.sync_ws()

    def ensure_ws(self):
        """Attach the session to the cloud workspace of this folder (auto-creates one for git repos)."""
        ses = self.agent.session
        if ses.get("ws") or not self.cloud.online or self.ws.disabled:
            return
        wid = self.ws.for_dir(os.getcwd())
        if not wid and self.s.config["settings"].get("auto_workspace", True) and Path(".git").exists() \
                and os.path.abspath(os.getcwd()) != str(Path.home()):
            try:
                wid = self.ws.link(os.getcwd())
                self.toast(f"☁ project linked as workspace {wid}", "ok")
            except CloudError:
                return
        if wid:
            ses["ws"] = wid
            self.sync_ws()

    def sync_ws(self, quiet=True):
        wid = self.agent.session.get("ws")
        if not wid or not self.cloud.online:
            return

        def run():
            try:
                up, down = self.ws.sync(wid)
                if (up or down) and not quiet or down:
                    self.toast(f"☁ files ↑{up} ↓{down}", "ok")
            except CloudError as e:
                if not quiet:
                    self.info(f"✗ workspace sync: {e}", "err")
        threading.Thread(target=run, daemon=True).start()

    def cmd_workspace(self, arg):
        if not self.cloud.online:
            return self.info("log in first — /login", "warn")
        wid = self.agent.session.get("ws") or self.ws.for_dir(os.getcwd())
        items = []
        if wid:
            items += [("sync", "↻ sync now", self.ws.local_path(wid) or ""), ("unlink", "unlink this folder", "files stay in the cloud")]
        else:
            items += [("link", "link this folder", os.getcwd())]
        items += [("clone", "get a cloud workspace into a folder", ""), ("auto", "auto-link git repos: " +
                  ("on" if self.s.config["settings"].get("auto_workspace", True) else "off"), "")]
        v = self.pick("Workspace — code files sync to the cloud (≤1 MB each, heavy/ignored skipped)", items)
        if v == "link":
            wid = self.ws.link(os.getcwd(), self.prompt("Workspace name", Path(os.getcwd()).name) or None)
            self.agent.session["ws"] = wid
            self.sync_ws(quiet=False)
        elif v == "sync":
            self.agent.session["ws"] = wid
            up, down = self.ws.sync(wid, emit=None)
            self.toast(f"☁ files ↑{up} ↓{down}", "ok")
        elif v == "unlink":
            self.ws.unlink(wid)
            self.agent.session.pop("ws", None)
            self.toast("unlinked")
        elif v == "auto":
            st = self.s.config["settings"]
            st["auto_workspace"] = not st.get("auto_workspace", True)
            self.s.save()
        elif v == "clone":
            lst = self.cloud.req("GET", "/api/ws")
            wid = self.pick("Cloud workspaces", [(w["id"], w["name"], f"{w['files']} files · {human(w['bytes'])}B") for w in lst])
            if wid:
                self.open_ws(wid, next(w["name"] for w in lst if w["id"] == wid))

    def open_ws(self, wid, name):
        """Make sure a cloud workspace exists locally, pull it, and cd into it."""
        path = self.ws.local_path(wid)
        if not path:
            path = self.prompt("Local folder for this workspace", str(Path.home() / "me-workspaces" / name))
            if not path:
                return False
            path = os.path.expanduser(path)
            self.ws.attach(wid, path, name)
        Path(path).mkdir(parents=True, exist_ok=True)
        up, down = self.ws.sync(wid)
        os.chdir(path)
        self.agent.relocate(path)
        self.agent.session["ws"] = wid
        self.files_cache = None
        self.toast(f"☁ {name}: ↓{down} files · cwd {path}", "ok")
        return True

    def bg(self, fn, *a):
        def run():
            try:
                fn(*a)
            except Exception as e:
                self.info(f"✗ {type(e).__name__}: {e}", "err")
            self.redraw()
        threading.Thread(target=run, daemon=True).start()


# ───── TUI: slash commands (mixin) ─────


class TUICommands:
    # ─── commands (run in worker thread; may block on modals) ───
    def all_commands(self):
        return [(c, T(d)) for c, d in COMMANDS] + [(n, d) for n, (_, d) in PLUGINS.commands.items()]

    def command(self, cmd, arg):
        names = [c for c, _ in self.all_commands()]
        if cmd not in names:
            cands = [c for c in names if c.startswith(cmd)]
            if len(cands) == 1:
                cmd = cands[0]
            else:
                return self.info(f"unknown command /{cmd} — /help", "err")
        if cmd in PLUGINS.commands:
            return PLUGINS.commands[cmd][0](self, arg)
        getattr(self, "cmd_" + cmd.replace("2fa", "twofa"))(arg)

    def cmd_help(self, arg):
        if UI_LANG["v"] == "ru":
            txt = "## Команды\n" + "\n".join(f"- `/{c}` {T(d)}" for c, d in COMMANDS)
            txt += ("\n\n## Клавиши\n- `enter` отправить · `shift+enter` / `alt+enter` / `ctrl+j` / `\\`+enter новая строка\n- `esc` прервать · `ctrl+c` очистить/выход"
                    "\n- `↑↓` история · `pgup/pgdn` / колесо — прокрутка · `ctrl+o` раскрыть последний инструмент (или клик)\n- `@путь` прикрепить файл (картинки уходят модели) · `!команда` выполнить самому — агент увидит вывод"
                    "\n- `tab` принять подсказку · `ctrl+v` / `alt+v` вставить текст или картинку · перетащите картинку, чтобы прикрепить"
                    "\n- `alt+c` скопировать последний ответ · `/copy` ответ, блок кода или весь чат"
                    "\n\n## Мышь\n- выделите текст мышью — он скопируется, как только отпустите (двойной клик — слово, тройной — строка)"
                    "\n- правый клик: скопировать выделение или вставить · клик по карточке инструмента — открыть её"
                    "\n- `shift`+выделение — выделение самого терминала · `/config` → мышь выкл возвращает его везде"
                    "\n\nЯзык интерфейса: `/lang en` или `/lang ru`.")
            return self.add("assistant", text=txt).__setattr__("done", True)
        txt = "## Commands\n" + "\n".join(f"- `/{c}` {d}" for c, d in COMMANDS)
        txt += ("\n\n## Keys\n- `enter` send · `shift+enter` / `alt+enter` / `ctrl+j` / `\\`+enter newline\n- `esc` interrupt · `ctrl+c` clear/quit"
                "\n- `↑↓` history · `pgup/pgdn` / wheel scroll · `ctrl+o` expand last tool (or click it)\n- `@path` attach file (images go to the model) · `!cmd` run a command yourself — the agent sees the output"
                "\n- `tab` accept suggestion · `ctrl+v` / `alt+v` paste text or an image · drag an image file in to attach it"
                "\n- `alt+c` copy the last answer · `/copy` answer, code block or the whole chat"
                "\n\n## Mouse\n- drag to select text: it is copied when you let go (double click = word, triple = line)"
                "\n- right click: copy the selection, or paste · click a tool card to open it"
                "\n- `shift`+drag: the terminal's own selection · `/config` → mouse off gives it back everywhere")
        self.add("assistant", text=txt).done = True

    def cmd_exit(self, arg):
        self.running = False

    cmd_quit = cmd_exit

    def cmd_agent(self, arg):
        ps = presets(self.s)
        cur = self.agent.preset()["name"]
        if arg in ps:
            name = arg
        else:
            items = [(n, p.get("label", n), p.get("description", "") + (" · custom" if p.get("custom") else "")) for n, p in ps.items()]
            items.append(("+market", "+ more agents", "marketplace"))
            name = self.pick("Agent mode", items, current=cur)
            if name == "+market":
                name = self.install_from_market("agent")
            if not name:
                return
        self.s.config["agent"] = name
        self.agent.session["agent"] = name
        self.s.save()
        p = ps.get(name) or presets(self.s).get(name, {})
        self.toast(f"mode → {p.get('label', name)} · tools: {p.get('tools', ALL_TOOLS)}", "ok")

    def cmd_init(self, arg):
        if self.agent.busy:
            return self.toast("busy", "warn")
        exists = Path("AGENTS.md").exists()
        self.add("user", text="/init")
        self.agent.run(("Update" if exists else "Create") + " AGENTS.md for this project. Explore it first (structure, manifests, scripts, README, "
                       "tests). Write a concise file: purpose; stack; how to install/run/test/build (exact commands, verified); folder map; "
                       "conventions; gotchas. It is loaded into every session, so keep it short and accurate.")

    def cmd_compact(self, arg):
        if self.agent.busy:
            return self.toast("busy — wait for the turn to finish", "warn")
        if not self.agent.compact(reason="manual"):
            self.toast("nothing to compact yet")

    def cmd_autocompact(self, arg):
        st = self.s.config["settings"]
        if not arg:
            cur = st.get("autocompact", 80)
            arg = self.pick(f"Auto-compact (now: {cur or 'off'}{'%' if cur else ''} of context, max 1M tokens)",
                            [("50", "50%", ""), ("70", "70%", ""), ("80", "80%", "default"), ("90", "90%", ""), ("off", "off", "")])
            if not arg:
                return
        st["autocompact"] = 0 if arg == "off" else max(10, min(95, int(str(arg).rstrip("%"))))
        self.s.save()
        m = self.agent.model()
        lim = self.agent.compact_threshold(m) if m else 0
        self.toast(f"auto-compact {'off' if not lim else f'at {human(lim)} tokens'}", "ok")

    def cmd_theme(self, arg):
        ts = themes(self.s)
        cur = self.s.config.get("theme") or "me-dark"
        if arg in ts:
            name = arg
        else:
            items = [(n, t.get("label", n), ("dark" if t.get("dark", True) else "light") + (" · built-in" if n in BUILTIN_THEMES else ""))
                     for n, t in ts.items()] + [("+market", "+ get more themes", "marketplace")]
            items = [(v, T(lab), T(det)) for v, lab, det in items]
            m = {"kind": "pick", "title": T("Theme (↑↓ previews)"), "items": items, "sel": next((i for i, x in enumerate(items) if x[0] == cur), 0),
                 "filter": "", "hint": "", "preview": lambda v: v in ts and self.retheme(v)}
            name = self._modal(m)
            if name == "+market":
                return self.install_from_market("theme")
            if not name:
                return self.retheme(cur)
        self.s.config["theme"] = name
        self.s.save()
        self.retheme(name)
        self.toast(f"theme → {name}", "ok")

    def install_from_market(self, kind):
        """Pick an item of `kind` from the marketplace and install it (synced to the account when online)."""
        try:
            items = self.cloud.req("GET", f"/api/market?kind={kind}", token=False)
        except CloudError as e:
            return self.info(f"✗ {e}", "err")
        mid = self.pick(f"Marketplace · {kind}s", [(x["id"], x["name"], x["description"]) for x in items])
        if not mid:
            return None
        x = self.cloud.req("GET", f"/api/market/{mid}", token=False)
        folder = {"theme": "themes", "agent": "agents", "plugin": "plugins", "skill": "skills"}[kind]
        ext = {"theme": "json", "agent": "md", "plugin": "py"}.get(kind, x.get("ext", "py"))
        (self.s.home / folder / f"{x['name']}.{ext}").write_text(x["content"], "utf-8")
        m = re.search(r"^skills:\s*(.+)$", x["content"].split("\n---", 1)[0], re.M) if kind == "agent" else None
        for name in [n.strip() for n in m.group(1).split(",") if n.strip()] if m else []:  # the skills this agent brings
            try:
                sk = next((y for y in self.cloud.req("GET", "/api/market?kind=skill&q=" + urllib.request.quote(name), token=False)
                           if y["name"] == name), None)
                if sk:
                    sk = self.cloud.req("GET", f"/api/market/{sk['id']}", token=False)
                    (self.s.home / "skills" / f"{name}.{sk.get('ext') or 'md'}").write_text(sk["content"], "utf-8")
            except CloudError:
                pass
        if self.cloud.online:
            try:
                self.cloud.req("POST", f"/api/market/{mid}/install", {})
            except CloudError:
                pass
        self.s.save()
        self.toast(f"installed {kind} {x['name']}", "ok")
        return x["name"]

    def cmd_new(self, arg):
        """A fresh session on a clean screen: nothing of the old conversation stays visible or in the model's context."""
        if self.agent.busy or self.launching:
            return self.toast("busy — esc to stop the current turn first", "warn")
        with self.lock:
            self.agent.session = new_session()
            self.agent.ctx_used, self.agent.snap = 0, {}
            self.blocks, self.tools_live, self.cur_text, self.cur_reason = [], {}, None, None
            self.scroll, self.msel, self.turn_tokens = 0, None, (0, 0)
            self._prev_rows = None
            self.term.write(CSI + "2J")
            self.welcome()
        self.toast("new session", "ok")

    cmd_clear = cmd_new

    def cmd_copy(self, arg):
        """/copy (picker when the answer has code) · /copy 2 (the answer before last) · /copy code · /copy all (whole chat)."""
        answers = [m["content"] for m in self.agent.session["messages"] if m["role"] == "assistant" and isinstance(m.get("content"), str)
                   and m["content"].strip()] or [b.text for b in self.blocks if b.kind == "assistant" and b.text.strip()]
        if not answers:
            return self.toast("nothing to copy")
        words = arg.split()
        n = next((int(w) for w in words if w.isdigit()), 1)
        text = answers[-min(max(1, n), len(answers))]
        codes = re.findall(r"```[^\n]*\n(.*?)```", text, re.S)
        v = "code" if "code" in words else "all" if "all" in words else "last" if words else None
        if v is None:
            items = [("last", "last answer", f"{len(text)} chars")]
            items += [(f"code{i}", f"code block {i + 1}", c.strip().split("\n")[0][:50]) for i, c in enumerate(codes)]
            items += [("all", "whole conversation", f"{len(self.agent.session['messages'])} messages")]
            v = "last" if not codes else self.pick("Copy", items)
            if not v:
                return
        if v == "all":
            parts = []
            for m in self.agent.session["messages"]:
                c = m.get("content")
                if m["role"] == "user" and isinstance(c, str) and not m.get("_aux"):
                    parts.append("> " + c.replace("\n", "\n> "))
                elif m["role"] == "assistant" and isinstance(c, str) and c.strip():
                    parts.append(c.strip())
            return self.copy_text("\n\n".join(parts), "conversation")
        if v == "code" or v.startswith("code"):
            if not codes:
                return self.toast("no code block in that answer", "warn")
            i = int(v[4:]) if v[4:].isdigit() and int(v[4:]) < len(codes) else len(codes) - 1
            return self.copy_text(codes[i].rstrip("\n"), f"code block {i + 1}")
        self.copy_text(text.strip("\n"), "last answer" if n == 1 else f"answer #{n} from the end")

    def cmd_yolo(self, arg):
        st = self.s.config["settings"]
        st["yolo"] = False
        st["perm"] = "ask" if perm_mode(st) == "full" else "full"
        self.s.save()
        self.toast(f"{'full access — tools run without asking' if st['perm'] == 'full' else 'ask — commands and writes outside the project ask'}",
                   "warn" if st["perm"] == "full" else "ok")

    def cmd_perm(self, arg):
        st = self.s.config["settings"]
        cur = perm_mode(st)
        v = arg or self.pick("Permissions", [(k, ("● " if k == cur else "  ") + PERMS[k], {
            "read": "no file changes; only allow-listed commands", "ask": "edits inside the project are free; commands ask",
            "full": "everything runs without asking"}[k]) for k in PERMS] + [("allow", "  allow-list…", "commands that never ask")])
        if v in PERMS:
            st["perm"], st["yolo"] = v, False
            self.s.save()
            return self.toast("permissions: " + PERMS[v], "ok")
        if v == "allow":
            cur_list = st.get("allow") or list(DEFAULT_ALLOW)
            text = self.prompt("Allowed commands, one pattern per line (* = anything; chained/redirected commands always ask)",
                               "\n".join(cur_list), multiline=True)
            if text is not None:
                st["allow"] = [l.strip() for l in text.splitlines() if l.strip()]
                self.s.save()
                self.toast(f"{len(st['allow'])} allowed patterns", "ok")

    def cmd_goal(self, arg):
        if self.agent.busy:
            return self.toast("busy", "warn")
        arg = (arg or "").strip().strip('"')
        if not arg:
            arg = self.prompt("Goal — the agent works until it is done and verified, or blocked", multiline=True)
            if not arg:
                return
        self.add("user", text="/goal " + arg)
        self.agent.goal(arg)

    def cmd_undo(self, arg):
        r = self.agent.undo()
        if not r["ok"]:
            self.toast(r["text"], "warn")
        else:
            self.render_history()
            self.info("↶ " + r["text"], "warn")

    def cmd_behavior(self, arg):
        """One switch per line; enter flips it. `/behavior tests off` sets one directly."""
        st = self.s.config["settings"]
        w = arg.split()
        if w and w[0] in BEHAVIOR:
            on = (w[1] in ("on", "1", "true", "yes")) if len(w) > 1 else not behavior(st)[w[0]]
            st.setdefault("behavior", {})[w[0]] = on
            self.s.save()
            return self.toast(f"{w[0]} {'on' if on else 'off'}", "ok")
        keep = {}
        while True:
            b = behavior(st)
            items = [(k, ('[x] ' if b[k] else '[ ] ') + T(BEHAVIOR[k][1]), k) for k in BEHAVIOR]
            v = self.pick("Behavior — enter toggles, esc closes", items, current=keep.get("v"), keep=keep)
            if v is None:
                return
            keep["v"] = v
            st.setdefault("behavior", {})[v] = not b[v]
            st.pop("no_verify", None)
            self.s.save()

    def cmd_retry(self, arg):
        text, err = self.agent.retry()
        if err:
            return self.toast(err, "warn")
        self.render_history()
        self.info("↻ retrying the last request", "dim")
        self.dispatch(text)

    def cmd_lang(self, arg):
        """/lang en · /lang ru · /lang auto — the interface language (commands stay as they are)."""
        st = self.s.config["settings"]
        v = arg.strip().lower() or self.pick("Language", [("auto", "auto (system)", system_lang()[:5]), ("en", "English", ""), ("ru", "Русский", "")],
                                             current=st.get("lang") or "auto")
        if v not in ("auto", "en", "ru"):
            return
        st["lang"] = "" if v == "auto" else v
        set_ui_lang(st)
        self.s.save()
        with self.lock:
            for b in self.blocks:
                b.touch()
        self.toast(f"language → {UI_LANG['v']}", "ok")

    def cmd_diff(self, arg):
        """Every file this chat changed: the first snapshot against the file now."""
        diffs = self.agent.chat_diff()
        if not diffs:
            return self.toast("no changes in this chat yet")
        txt = "\n\n".join(f"### {p}\n```diff\n{d[:6000]}\n```" for p, d in diffs[:20])
        self.add("assistant", text=txt).done = True

    def cmd_rename(self, arg):
        name = arg.strip() or self.prompt("Chat name", self.agent.session.get("title", ""))
        if not name:
            return
        self.agent.session["title"] = name.strip()[:80]
        self.s.save_session(self.agent.session)
        self.toast(f"chat renamed: {name.strip()[:80]}", "ok")

    def cmd_rewind(self, arg):
        """Pick an earlier request: the chat goes back to just before it, files the later turns changed are restored."""
        if self.agent.busy:
            return self.toast("busy — esc to stop the current turn first", "warn")
        reqs = self.agent.requests()
        if not reqs:
            return self.toast("nothing to rewind")
        items = [(str(i), " ".join(t.split())[:90] or "…", f"#{n + 1}") for n, (i, t) in reversed(list(enumerate(reqs)))]
        v = self.pick("Rewind — the chat goes back to before this request", items)
        if v is None:
            return
        text, files = self.agent.rewind(int(v))
        if text is None:
            return self.toast(files, "warn")
        self.render_history()
        self.info(f"↶ rewound · restored {len(files)} file(s): {', '.join(files[:6])}" if files else "↶ rewound", "warn")
        with self.lock:
            self.input, self.cur = text, len(text)  # the request is back in the input: edit it and press enter, or clear it

    def cmd_fork(self, arg):
        if self.agent.busy:
            return self.toast("busy — esc to stop the current turn first", "warn")
        old = self.agent.session.get("title") or "chat"
        self.agent.fork()
        self.toast(f"forked: {old[:50]} — the original chat stays in /sessions", "ok")

    def cmd_btw(self, arg):
        q = arg.strip() or self.prompt("Side question (does not go into the chat)")
        if not q:
            return
        self.info("◌ /btw " + q[:100], "dim")
        try:
            a = self.agent.side_question(q)
        except (ProviderError, OSError) as e:
            return self.info(f"✗ {e}", "err")
        self.add("assistant", text="> /btw " + q[:300] + "\n\n" + a).done = True

    def cmd_search(self, arg):
        q = arg.strip() or self.prompt("Search all chats")
        if not q:
            return
        hits = self.s.search(q)
        if not hits:
            return self.toast(f"nothing found: {q}")
        sid = self.pick(f"Found in {len(hits)} chat(s)", [(s["id"], s.get("title") or "(untitled)", snip) for s, snip in hits])
        if sid:
            self.resume(sid)

    def run_shell_escape(self, cmd):
        """`!cmd`: run a command yourself; the agent sees it and its output in the next request."""
        if self.agent.busy:
            return self.toast("busy — esc to stop the current turn first", "warn")
        self.add("user", text="!" + cmd)
        ok, out = self.agent.shell(cmd)
        self.add("assistant", text="```\n" + clip(out, 6000) + "\n```").done = True
        if not ok:
            self.info("✗ the command failed", "err")

    def cmd_context(self, arg):
        self.add("assistant", text=self.agent.context_report()).done = True

    def cmd_export(self, arg):
        name = arg.strip() or f"me-chat-{self.agent.session['id']}.md"
        p = Path(self.agent.abspath(name))
        p.write_text(self.agent.export_md(), "utf-8")
        self.info(f"✓ chat saved to {p}", "ok")

    def cmd_todo(self, arg):
        items = self.agent.session.get("todo") or []
        if not items:
            return self.toast("no todo list in this session")
        self.add("assistant", text="\n".join(("- [x] " if i["s"] == "done" else "- [ ] " if i["s"] == "todo" else "- [>] ") + i["t"] for i in items)).done = True

    def cmd_cost(self, arg):
        st = self.s.config["settings"]
        u = self.agent.session.get("usage", {})
        m = self.agent.model() or {}
        pin, pout, pc = model_price(m)
        if arg:
            k, _, v = arg.partition(" ")
            if k in ("turn", "day"):
                st["max_turn_cost" if k == "turn" else "max_day_cost"] = float(v or 0)
                self.s.save()
                return self.toast(f"{k} limit {money(float(v or 0)) if float(v or 0) else 'off'}", "ok")
        days = jload(self.s.home / "usage.json", {}) or {}
        if UI_LANG["v"] == "ru":
            off = "выкл"
            txt = (f"## Расходы\n- сессия: **{money(u.get('cost', 0))}** · ↑{human(u.get('in', 0))} ↓{human(u.get('out', 0))}"
                   f" · из кэша {human(u.get('cached', 0))}\n- сегодня: **{money(day_cost(self.s.home))}** · за 7 дней: "
                   f"{money(sum(v for k, v in sorted(days.items())[-7:]))}\n- {m.get('name', 'model')}: ${pin}/M вход · ${pout}/M выход · ${pc}/M кэш"
                   f"{'' if any((pin, pout)) else ' (цена неизвестна — задайте в /models)'}\n- лимиты: на задачу {money(st.get('max_turn_cost') or 0) if st.get('max_turn_cost') else off}"
                   f" · в день {money(st.get('max_day_cost') or 0) if st.get('max_day_cost') else off}  (`/cost turn 0.5`, `/cost day 5`)")
            return self.add("assistant", text=txt).__setattr__("done", True)
        txt = (f"## Spending\n- session: **{money(u.get('cost', 0))}** · ↑{human(u.get('in', 0))} ↓{human(u.get('out', 0))}"
               f" · cached {human(u.get('cached', 0))}\n- today: **{money(day_cost(self.s.home))}** · last 7 days: "
               f"{money(sum(v for k, v in sorted(days.items())[-7:]))}\n- {m.get('name', 'model')}: ${pin}/M in · ${pout}/M out · ${pc}/M cached"
               f"{'' if any((pin, pout)) else ' (unknown — set price in /models)'}\n- limits: per task {money(st.get('max_turn_cost') or 0) if st.get('max_turn_cost') else 'off'}"
               f" · per day {money(st.get('max_day_cost') or 0) if st.get('max_day_cost') else 'off'}  (`/cost turn 0.5`, `/cost day 5`)")
        self.add("assistant", text=txt).done = True

    def cmd_mcp(self, arg):
        mc = self.s.config.setdefault("mcp", {})
        items = [(k, k, v.get("url") or " ".join([v.get("command", "")] + v.get("args", []))[:60]) for k, v in mc.items()]
        k = self.pick("MCP servers — loaded lazily: one prompt line idle, tools fetched on use", items + [("+", "+ add server", "stdio command or http url")])
        if k is None:
            return
        if k == "+":
            name = self.prompt("Name (e.g. github)")
            if not name:
                return
            target = self.prompt("Command (npx -y @modelcontextprotocol/server-github) or URL (https://…/mcp)")
            if not target:
                return
            desc = self.prompt("One-line description for the agent", "")
            if target.startswith(("http://", "https://")):
                spec = {"url": target}
            else:
                import shlex
                parts = shlex.split(target)
                spec = {"command": parts[0], "args": parts[1:]}
            if desc:
                spec["description"] = desc
            mc[re.sub(r"[^\w-]", "", name)] = spec
            self.s.save()
            return self.toast(f"MCP {name} added", "ok")
        act = self.pick(k, [("test", "test: list tools", ""), ("del", "remove", "")])
        if act == "del":
            mc.pop(k, None)
            self.s.save()
            MCP_CLIENTS.pop(k, None)
        elif act == "test":
            try:
                self.add("assistant", text=f"## {k}\n" + mcp_describe(mcp_client(self.s, k).list_tools())).done = True
            except Exception as e:
                self.info(f"✗ {e}", "err")

    def cmd_model(self, arg):
        ms = self.s.config["models"]
        if not ms:
            self.info("no models yet", "warn")
            return self.cmd_models("")
        if arg and any(m["name"] == arg for m in ms):
            name = arg
        else:
            name = self.pick("Model", [(m["name"], m["name"], f"{m['id']} · {m.get('provider')} · {human(m.get('context') or 0)} ctx"
                                        + (f" · {m['effort']}" if m.get("effort") else "")) for m in ms], current=self.agent.model_name())
        if name:
            self.agent.set_model(name)
            self.toast(f"model → {name} (this chat; new chats start with it too)", "ok")

    def cmd_effort(self, arg):
        m = self.agent.model()
        if not m:
            return self.toast("no model", "warn")
        e = arg if arg in EFFORTS else self.pick("Reasoning effort", [(e, e, "") for e in EFFORTS], current=m.get("effort") or "none")
        if e:
            m["effort"] = "" if e == "none" else e
            self.s.save()
            self.toast(f"effort → {e}", "ok")

    def cmd_provider(self, arg):
        provs = self.s.config["providers"]
        items = [(n, n, p.get("base_url", "")) for n, p in provs.items()] + [("+", "+ add provider", "OpenAI-compatible URL + key")]
        v = self.pick("Providers", items)
        if v is None:
            return
        if v == "+":
            return self.setup_provider()
        act = self.pick(v, [("edit", "edit URL / key", ""), ("sync", "sync models from API", ""), ("del", "delete", "")])
        if act == "edit":
            self.setup_provider(v)
        elif act == "sync":
            self.sync_models(v)
        elif act == "del":
            provs.pop(v, None)
            self.s.save()
            self.toast(f"deleted {v}")

    def setup_provider(self, name=None):
        p = self.s.config["providers"].get(name, {}) if name else {}
        url = self.prompt("Base URL", p.get("base_url", "https://api.openai.com/v1"), hint="any OpenAI-compatible endpoint, e.g. https://openrouter.ai/api/v1")
        if url is None:
            return
        key = self.prompt("API key", p.get("api_key", ""), secret=True, hint="stored in your config, synced to your cloud account")
        if key is None:
            return
        default = name or re.sub(r"^(api|www)\.", "", re.sub(r"https?://", "", url).split("/")[0]).split(".")[0] or "provider"
        nm = self.prompt("Provider name", default)
        if not nm:
            return
        if name and nm != name:
            self.s.config["providers"].pop(name, None)
            for m in self.s.config["models"]:
                if m.get("provider") == name:
                    m["provider"] = nm
        self.s.config["providers"][nm] = {"base_url": url.strip().rstrip("/"), "api_key": key.strip()}
        self.s.save()
        self.toast(f"provider {nm} saved", "ok")
        if self.pick("Fetch models from this provider now?", [(True, "yes, sync models", ""), (False, "no", "")]):
            self.sync_models(nm)

    def sync_models(self, pname):
        self.toast(f"fetching models from {pname}…")
        try:
            remote = list_models(self.s.config["providers"][pname])
        except Exception as e:
            return self.info(f"✗ could not list models: {e}", "err")
        if not remote:
            return self.info("provider returned no models", "warn")
        have = {(m.get("provider"), m["id"]) for m in self.s.config["models"]}
        added, keep = 0, {}  # keep: the filter and the picked row survive each toggle, so several models can be added from one search
        while True:
            items = [("*", "✓ done", f"{added} added")] + [(r["id"], ("✓ " if (pname, r["id"]) in have else "  ") + r["id"],
                                                             human(r["context"]) + " ctx" if r.get("context") else "") for r in remote]
            v = self.pick(f"Add models from {pname} ({len(remote)})", items, hint="enter toggles · type to filter", current=keep.get("v"), keep=keep)
            keep["v"] = v
            if v in (None, "*"):
                break
            if (pname, v) in have:
                self.s.config["models"] = [m for m in self.s.config["models"] if not (m.get("provider") == pname and m["id"] == v)]
                have.discard((pname, v))
                added -= 1
            else:
                r = next(x for x in remote if x["id"] == v)
                nm = self.s.unique_model_name(v, pname)
                self.s.config["models"].append({"name": nm, "id": v, "provider": pname, "api": "chat", "context": r.get("context") or 128000,
                                                "max_out": "", "effort": "", "temperature": "", "vision": True})
                have.add((pname, v))
                added += 1
                if not self.s.config.get("model"):
                    self.s.config["model"] = nm
        self.s.save()
        self.toast(f"{added} models added", "ok")

    def cmd_models(self, arg):
        while True:
            ms = self.s.config["models"]
            items = [(i, m["name"], f"{m['id']} · {m.get('provider')} · {human(m.get('context') or 0)}") for i, m in enumerate(ms)]
            items += [("sync", "↻ sync from provider", ""), ("add", "+ add model manually", "")]
            v = self.pick("Models", items, current=next((i for i, m in enumerate(ms) if m["name"] == self.agent.model_name()), None))
            if v is None:
                return
            if v == "sync":
                provs = list(self.s.config["providers"])
                if not provs:
                    return self.setup_provider()
                p = provs[0] if len(provs) == 1 else self.pick("Provider", provs)
                if p:
                    self.sync_models(p)
                continue
            if v == "add":
                provs = list(self.s.config["providers"])
                if not provs:
                    self.info("add a provider first", "warn")
                    return self.setup_provider()
                p = provs[0] if len(provs) == 1 else self.pick("Provider", provs)
                mid = p and self.prompt("Model id (as the API expects it)")
                if mid:
                    ms.append({"name": self.s.unique_model_name(mid, p), "id": mid, "provider": p, "api": "chat", "context": 128000, "max_out": "",
                               "effort": "", "temperature": "", "vision": True})
                    self.s.save()
                    self.edit_model(ms[-1])
                continue
            self.edit_model(ms[v])

    def edit_model(self, m):
        fields = [("name", "display name"), ("id", "API model id"), ("provider", "provider"), ("api", "chat | responses"),
                  ("context", "context window, tokens"), ("max_out", "max output tokens (blank = default)"),
                  ("effort", "reasoning effort: none/minimal/low/medium/high/xhigh"), ("temperature", "blank = default"),
                  ("vision", "send images: true/false"), ("price", "$ per 1M tokens: in out [cached] (blank = built-in table)"),
                  ("cache", "prompt caching: auto / anthropic / openai / off")]
        while True:
            items = [(f, f"{f:<12} {m.get(f, '')}", d) for f, d in fields] + [("use", "✓ use this model", ""), ("del", "✗ delete", "")]
            f = self.pick(f"Model · {m['name']}", items)
            if f is None:
                break
            if f == "use":
                self.agent.set_model(m["name"])
                self.toast(f"model → {m['name']}", "ok")
                break
            if f == "del":
                self.s.config["models"].remove(m)
                if self.s.config.get("model") == m["name"]:
                    self.s.config["model"] = self.s.config["models"][0]["name"] if self.s.config["models"] else None
                self.s.save()
                break
            if f == "api":
                v = self.pick("API style", [("chat", "chat/completions", "most compatible"), ("responses", "responses", "OpenAI Responses API")],
                              current=m.get("api"))
            elif f == "effort":
                v = self.pick("Reasoning effort", [(e, e, "") for e in EFFORTS], current=m.get("effort") or "none")
                v = "" if v == "none" else v
            elif f == "provider":
                v = self.pick("Provider", list(self.s.config["providers"]), current=m.get("provider"))
            elif f == "cache":
                v = self.pick("Prompt caching", [("", "auto", "detect from model/provider"), ("anthropic", "anthropic", "cache_control marker"),
                                                 ("openai", "openai", "prompt_cache_key"), ("off", "off", "")], current=m.get("cache", ""))
            elif f == "price":
                v = self.prompt("in out [cached], $ per 1M tokens", " ".join(map(str, m.get("price") or [])))
                if v is not None:
                    try:
                        v = [float(x) for x in v.replace(",", " ").split()][:3] or ""
                    except ValueError:
                        self.toast("numbers, e.g. 3 15 0.3", "err")
                        continue
            elif f == "vision":
                v = self.pick("Vision", [(True, "true", ""), (False, "false", "")], current=m.get("vision", True))
            else:
                v = self.prompt(f, m.get(f, ""))
            if v is None:
                continue
            if f in ("context", "max_out") and v != "":
                try:
                    v = int(str(v).replace("k", "000").replace("K", "000"))
                except ValueError:
                    self.toast("must be a number", "err")
                    continue
            if f == "name" and any(x is not m and x["name"] == v for x in self.s.config["models"]):
                self.toast(f"another model is already called {v}", "err")
                continue
            if f == "name" and self.s.config.get("model") == m["name"]:
                self.s.config["model"] = v
            if f == "name" and self.agent.session.get("model") == m["name"]:
                self.agent.session["model"] = v
            m[f] = v
            self.s.save()

    def cmd_prompt(self, arg):
        v = self.prompt("Your system prompt (appended to me's base prompt)", self.s.config.get("prompt", ""), multiline=True,
                        hint="ctrl+s to save · synced to all your devices")
        if v is not None:
            self.s.config["prompt"] = v.strip()
            self.s.save()
            self.toast("prompt saved", "ok")

    def cmd_secret(self, arg):
        sec = self.s.config.setdefault("secrets", {})
        items = [(k, k, "•" * 8) for k in sec] + [("+", "+ add secret", "env var for skills")]
        k = self.pick("Secrets (env vars for skills)", items)
        if k is None:
            return
        if k == "+":
            k = self.prompt("Name (e.g. GITHUB_TOKEN)")
            if not k:
                return
        else:
            act = self.pick(k, [("edit", "edit", ""), ("del", "delete", "")])
            if act == "del":
                sec.pop(k, None)
                self.s.save()
                return
            if act is None:
                return
        v = self.prompt(k, sec.get(k, ""), secret=True)
        if v is not None:
            sec[k] = v
            self.s.save()
            self.toast(f"{k} saved", "ok")

    def cmd_skills(self, arg):
        while True:
            sks = self.agent.skills.all()
            dis = self.s.config["settings"].setdefault("disabled_skills", [])
            items = [(n, ("○ " if n in dis else "● ") + f"{sk.get('icon', '◆')} {n}",
                      (sk.get("error") or sk["description"])[:60] + (f" · {len(sk['actions'])} actions" if sk["actions"] else ""))
                     for n, sk in sks.items()]
            items += [("+new", "+ new skill from template", ""), ("+ai", "+ ask me to write a skill", ""), ("+url", "+ install from URL", "")]
            v = self.pick("Skills", items, hint="● on ○ off")
            if v is None:
                return
            if v == "+new":
                n = self.prompt("Skill name (a-z, 0-9, -)")
                if n:
                    n = re.sub(r"[^a-z0-9_-]", "-", n.lower())
                    p = self.s.home / "skills" / f"{n}.py"
                    p.write_text(SKILL_TEMPLATE.replace("NAME", n), "utf-8")
                    self.s.save()
                    self.info(f"created {p} — edit it, it's live immediately", "ok")
                continue
            if v == "+ai":
                what = self.prompt("What should the skill do?")
                if what:
                    self.add("user", text=f"Create a skill: {what}")
                    self.agent.run(f"Create a me skill: {what}\nWrite it to {self.s.home / 'skills'}/<name>.py following this format exactly:\n\n"
                                   f"{SKILL_TEMPLATE}\nKeep it compact. Use ctx.progress/ctx.log for long steps. Test an action with python if possible.")
                    self.s.save()
                return
            if v == "+url":
                url = self.prompt("URL of a skill .py/.md")
                if url:
                    try:
                        data = urllib.request.urlopen(url, timeout=30).read().decode("utf-8")
                        fn = re.sub(r"[^\w.-]", "-", url.rstrip("/").split("/")[-1].split("?")[0]) or "skill.py"
                        if not fn.endswith((".py", ".md")):
                            fn += ".py"
                        (self.s.home / "skills" / fn).write_text(data, "utf-8")
                        self.s.save()
                        self.toast(f"installed {fn}", "ok")
                    except Exception as e:
                        self.info(f"✗ {e}", "err")
                continue
            sk = sks[v]
            act = self.pick(f"{sk.get('icon', '◆')} {v}", [("toggle", "disable" if v not in dis else "enable", ""), ("view", "view", sk["path"]),
                                                            ("del", "delete", "")])
            if act == "toggle":
                dis.remove(v) if v in dis else dis.append(v)
                self.s.save()
            elif act == "view":
                lang = "python" if sk["path"].endswith(".py") else "markdown"
                self.add("assistant", text=f"**{v}** — {sk['path']}\n```{lang}\n{Path(sk['path']).read_text('utf-8')}\n```").done = True
                return
            elif act == "del":
                if self.pick(f"Delete {v}?", [(False, "no", ""), (True, "yes, delete", "")]):
                    Path(sk["path"]).unlink()
                    self.s.save()

    def cmd_plugins(self, arg):
        while True:
            d = self.s.home / "plugins"
            dis = self.s.config["settings"].setdefault("disabled_plugins", [])
            files = sorted(d.glob("*.py"))
            items = [(p.stem, ("○ " if p.stem in dis else "● ") + p.stem,
                      PLUGINS.errors.get(p.stem) or (meta_line(p.read_text("utf-8")) if p.exists() else "")) for p in files]
            items += [("+market", "+ browse plugin marketplace", ""), ("+new", "+ new plugin from template", "")]
            v = self.pick("Plugins (changes apply after restart)", items, hint="● on ○ off")
            if v is None:
                return
            if v == "+new":
                n = self.prompt("Plugin name")
                if n:
                    n = re.sub(r"[^a-z0-9_-]", "-", n.lower())
                    (d / f"{n}.py").write_text(PLUGIN_TEMPLATE.replace("NAME", n), "utf-8")
                    self.s.save()
                    self.info(f"created {d / (n + '.py')} — restart me to load it", "ok")
                continue
            if v == "+market":
                try:
                    items = [x for x in self.cloud.req("GET", "/api/market?kind=plugin", token=False)]
                except CloudError as e:
                    self.info(f"✗ {e}", "err")
                    continue
                mid = self.pick("Plugin marketplace", [(x["id"], x["name"], x["description"]) for x in items])
                if mid:
                    x = self.cloud.req("GET", f"/api/market/{mid}", token=False)
                    (d / f"{x['name']}.py").write_text(x["content"], "utf-8")
                    if self.cloud.online:
                        try:
                            self.cloud.req("POST", f"/api/market/{mid}/install", {})
                        except CloudError:
                            pass
                    self.s.save()
                    self.info(f"installed plugin {x['name']} — restart me (/update force or relaunch) to load it", "ok")
                continue
            act = self.pick(v, [("toggle", "disable" if v not in dis else "enable", ""), ("view", "view source", ""), ("del", "delete", "")])
            if act == "toggle":
                dis.remove(v) if v in dis else dis.append(v)
                self.s.save()
            elif act == "view":
                self.add("assistant", text=f"```python\n{(d / (v + '.py')).read_text('utf-8')}\n```").done = True
                return
            elif act == "del" and self.pick(f"Delete {v}?", [(False, "no", ""), (True, "yes", "")]):
                (d / f"{v}.py").unlink()
                self.s.save()

    def render_history(self):
        """Rebuild the transcript from the current session's messages."""
        with self.lock:
            self.blocks = []
            self.welcome()
            for m in self.agent.session["messages"]:
                c = m.get("content")
                if isinstance(c, list):
                    c = " ".join(x.get("text", "") for x in c if x.get("type") == "text")
                if m.get("_shell"):
                    cmd, _, out = (c or "").partition("\n$ ")[2].partition("\n")
                    self.add("user", text="!" + cmd)
                    self.add("assistant", text="```\n" + out + "\n```").done = True
                    continue
                if m["role"] == "user" and m.get("_aux"):  # the agent's own nudges are not something the user typed
                    if isinstance(c, str) and c.startswith("[message from the user"):
                        self.add("user", text=c.split("\n", 1)[-1])
                    continue
                if m["role"] == "user" and c and c != "(images from read)":
                    self.add("user", text=c)
                elif m["role"] == "assistant":
                    if c:
                        self.add("assistant", text=c).done = True
                    for tc in m.get("tool_calls") or []:
                        try:
                            a = json.loads(tc["function"]["arguments"])
                        except ValueError:
                            a = {}
                        a = a if isinstance(a, dict) else {}
                        b = self.add("tool", name=tc["function"]["name"], args=a, label=self.agent.tools.preview(tc["function"]["name"], a))
                        b.done, b.ok, b.collapsed = True, True, True

    def cmd_sessions(self, arg):
        ss = self.s.sessions()
        if not ss:
            return self.toast("no sessions yet")
        sid = self.pick("Resume session", [(s["id"], s.get("title") or "(untitled)",
                                            time.strftime("%d %b %H:%M", time.localtime(s.get("updated", 0))) + f" · {s.get('n', 0)} msgs · "
                                            + s.get("cwd", "")) for s in ss])
        if sid:
            self.resume(sid)

    def resume(self, sid):
        if self.agent.busy:
            return self.toast("busy — esc to stop the current turn first", "warn")
        self.agent.load(sid)
        self.render_history()
        wid = self.agent.session.get("ws")
        if wid and self.cloud.online:
            try:
                self.open_ws(wid, Path(self.agent.session.get("cwd", wid)).name)
            except CloudError as e:
                self.info(f"✗ workspace: {e}", "err")
        elif self.agent.session.get("cwd") and os.path.isdir(self.agent.session["cwd"]):
            os.chdir(self.agent.session["cwd"])
        self.toast(f"resumed · {len(self.agent.session['messages'])} messages", "ok")

    def cmd_sync(self, arg):
        if not self.cloud.online:
            return self.info("not logged in — /login", "warn")
        n = self.cloud.sync()
        if n < 0:
            self.info(f"✗ sync failed: {self.cloud.last_error}", "err")
        else:
            self.toast(f"synced · {n} updated from cloud", "ok")

    def cmd_login(self, arg):
        mode = self.pick("Cloud account", [("login", "log in", ""), ("register", "create account", ""),
                                           ("server", "change server", self.cloud.server)])
        if mode is None:
            return False
        if mode == "server":
            v = self.prompt("Server URL", self.cloud.server)
            if v:
                self.s.auth["server"] = v.rstrip("/")
                self.s.save_auth()
            return self.cmd_login(arg)
        user = self.prompt("Username")
        if not user:
            return False
        pw = self.prompt("Password", secret=True)
        if not pw:
            return False
        if mode == "register":
            pw2 = self.prompt("Repeat password", secret=True)
            if pw2 != pw:
                self.info("passwords don't match", "err")
                return False
        totp = None
        while True:
            try:
                self.cloud.login(user.strip(), pw, totp, register=mode == "register")
                break
            except CloudError as e:
                if str(e) == "totp_required" or str(e) == "totp_invalid":
                    totp = self.prompt("Authenticator code" + (" (wrong, try again)" if str(e) == "totp_invalid" else ""))
                    if not totp:
                        return False
                    continue
                self.info(f"✗ {e}", "err")
                return False
        self.info(f"☁ logged in as {self.s.auth['user']} — syncing…", "ok")
        local_cfg = json.loads(json.dumps(self.s.config))
        n = self.cloud.sync()
        if n >= 0:
            self.s.config = jload(self.s.cfg_path) or self.s.config
            if not self.s.config.get("models") and local_cfg.get("models"):
                self.s.config = local_cfg
                self.s.save()
            self.toast(f"synced · {n} items from cloud", "ok")
        if not PLUGINS.loaded:
            PLUGINS.load(self.s)
            PLUGINS.collect("startup", self)
        return True

    def cmd_logout(self, arg):
        self.cloud.logout()
        self.toast("logged out — local data kept", "ok")

    def cmd_twofa(self, arg):
        if not self.cloud.online:
            return self.info("log in first — /login", "warn")
        try:
            me = self.cloud.req("GET", "/api/me")
        except CloudError as e:
            return self.info(f"✗ {e}", "err")
        if me.get("totp_enabled"):
            if self.pick("Authenticator is ON", [(False, "keep it", ""), (True, "turn off", "")]):
                code = self.prompt("Current authenticator code")
                if code:
                    try:
                        self.cloud.req("POST", "/api/totp/disable", {"code": code})
                        self.toast("2FA disabled", "ok")
                    except CloudError as e:
                        self.info(f"✗ {e}", "err")
            return
        try:
            r = self.cloud.req("POST", "/api/totp/setup", {})
        except CloudError as e:
            return self.info(f"✗ {e}", "err")
        self.add("assistant", text=f"### Authenticator setup\nAdd this key to Google Authenticator / Aegis / 1Password:\n\n"
                                   f"`{r['secret']}`\n\nor open the QR on the site: {self.cloud.server}/account").done = True
        code = self.prompt("Enter the 6-digit code to confirm")
        if code:
            try:
                self.cloud.req("POST", "/api/totp/enable", {"code": code})
                self.info("✓ 2FA enabled", "ok")
            except CloudError as e:
                self.info(f"✗ {e}", "err")

    def cmd_config(self, arg):
        st = self.s.config["settings"]
        while True:
            onoff = lambda x: T("on" if x else "off")
            items = [("lang", f"language             {st.get('lang') or 'auto'}", "en / ru"),
                     ("yolo", f"auto-approve tools   {onoff(st.get('yolo'))}", ""),
                     ("mouse", f"mouse                {onoff(st.get('mouse', True))}",
                      "on: wheel scroll, drag to select+copy · off: the terminal's own selection"),
                     ("show_thinking", f"show thinking        {onoff(st.get('show_thinking'))}", ""),
                     ("notify", f"notify when done     {onoff(st.get('notify', True))}", "a sound + a desktop notification when a task took over 30 s"),
                     ("behavior", "behavior switches…", "self-testing, todo, web, plan first, brief, explain, commit"),
                     ("home", "data dir", str(self.s.home))]
            v = self.pick("Settings", items)
            if v is None or v == "home":
                return
            if v == "behavior":
                self.cmd_behavior("")
                continue
            if v == "lang":
                self.cmd_lang("")
                continue
            st[v] = not st.get(v, v in ("mouse", "notify"))
            if v == "mouse":
                self.term.set_mouse(st[v])
            self.s.save()

    def cmd_update(self, arg):
        self.toast("checking for updates…")
        try:
            src = self.cloud.req("GET", "/me.py", token=False)
            src = src.decode("utf-8") if isinstance(src, bytes) else src
        except CloudError as e:
            return self.info(f"✗ {e}", "err")
        v = re.search(r'^VERSION = "([^"]+)"', src, re.M)
        if not v:
            return self.info("✗ bad update payload", "err")
        if v.group(1) == VERSION and arg != "force":
            self.update_avail = None  # the banner was stale: nothing newer on the server
            return self.toast(f"already on latest v{VERSION}", "ok")
        target = Path(os.path.abspath(__file__))
        target.write_bytes(src.encode("utf-8"))  # byte-exact (write_text would turn every line end into CRLF on Windows)
        self.info(f"✓ updated to v{v.group(1)} — restarting…", "ok")
        time.sleep(0.6)
        self.restart = True
        self.running = False


# ───── TUI: state, agent events, modals, lifecycle ─────


class TUI(TUIRender, TUIInput, TUICommands):
    def __init__(self, store):
        self.s = store
        apply_theme(store)
        self.cloud = Cloud(store)
        self.ws = Workspaces(store, self.cloud)
        self.term = Term()
        self.blocks = []
        self.input, self.cur = "", 0
        self.pending = []  # messages sent while the agent was busy: sent in order when it is free
        self.launching = False  # a turn was started but the agent may not show busy yet
        self.hist = [l for l in (self.s.home / "history").read_text("utf-8").split("\0") if l] if (self.s.home / "history").exists() else []
        self.hpos = None
        self.scroll = 0  # lines from bottom
        self.sugg, self.sel = [], 0
        self.modal = None
        self.running = True
        self.dirty = True
        self.frame = 0
        self.lock = threading.RLock()
        set_ui_lang(store.config["settings"])
        self.agent = Agent(store, self.on_event, self.confirm_tool, self.ask_text)
        self.agent.take_queued = self.take_pending
        self.tools_live = {}
        self.cur_text = None
        self.cur_reason = None
        self.turn_t0 = 0
        self.turn_tokens = (0, 0)
        self.update_avail = None
        self.files_cache = None
        self.notice = ("", 0)
        self.msel = None  # mouse selection in the transcript: {"a": (line, cell), "b": (line, cell), "drag": bool}
        self.attached = []  # image files attached to the message being typed ([image #n] in the input)
        self.pastes = []  # big pastes, shown as [paste #n · N lines] in the input and expanded when it is sent
        store.on_change = self._schedule_sync
        self._sync_timer = None

    # ─── plumbing ───
    def _schedule_sync(self):
        if not self.cloud.online:
            return
        if self._sync_timer:
            self._sync_timer.cancel()
        self._sync_timer = threading.Timer(0.7, lambda: self.cloud.sync_bg(lambda n: self.redraw()))
        self._sync_timer.daemon = True
        self._sync_timer.start()

    def redraw(self):
        self.dirty = True

    def toast(self, msg, color="dim"):
        self.notice = (T(msg), time.time(), color)
        self.redraw()

    def add(self, kind, **kw):
        if kind == "info" and isinstance(kw.get("text"), str):
            kw["text"] = T(kw["text"])
        with self.lock:
            b = Block(kind, **kw)
            self.blocks.append(b)
            self.redraw()
            return b

    def info(self, text, color="dim"):
        return self.add("info", text=text, color=color)

    # ─── agent events (worker thread) ───
    def on_event(self, e):
        with self.lock:
            t = e["type"]
            if t == "turn_start":
                self.turn_t0 = time.time()
                self.cur_text = self.cur_reason = None
            elif t == "reasoning":
                if self.cur_reason is None or self.cur_reason.done:
                    self.cur_reason = self.add("reason", text="")
                self.cur_reason.text += e["text"]
                self.cur_reason.touch()
            elif t == "text":
                if self.cur_reason and not self.cur_reason.done:
                    self.cur_reason.done, self.cur_reason.elapsed = True, time.time() - self.cur_reason.t0
                    self.cur_reason.touch()
                if self.cur_text is None:
                    self.cur_text = self.add("assistant", text="")
                self.cur_text.text += e["text"]
                self.cur_text.touch()
            elif t == "tool_start":
                if self.cur_reason and not self.cur_reason.done:
                    self.cur_reason.done, self.cur_reason.elapsed = True, time.time() - self.cur_reason.t0
                    self.cur_reason.touch()
                self.cur_text = None
                self.cur_reason = None
                cli = e.get("cli")
                kind = "skill" if cli or (e["name"] == "skill" and e["args"].get("action")) else "tool"
                b = self.add(kind, name=e["name"], args=e["args"], label=e["label"])
                if cli:  # an agent's own CLI tool: a box with a colored status bar
                    c = cli.get("color") or "red"
                    b.data.update(cli=True, color=SKILL_COLORS.get(c, c if str(c).startswith("#") else SKILL_COLORS["red"]), icon=cli.get("icon") or "▣",
                                  args={"name": cli["label"], "action": cli["agent"], "args": e["args"]})
                elif kind == "skill":
                    sk = self.agent.skills.all().get(e["args"].get("name"), {})
                    b.data["color"] = SKILL_COLORS.get(sk.get("color", ""), sk.get("color") if str(sk.get("color", "")).startswith("#") else TH["accent"])
                    b.data["icon"] = sk.get("icon", "◆")
                self.tools_live[e["id"]] = b
            elif t == "tool_event":
                b = self.tools_live.get(e["id"])
                if b:
                    k = e.get("t")
                    if k == "progress":
                        b.progress = e
                    elif k == "status":
                        b.status = e["text"]
                    elif k == "diff":
                        b.data["diff"] = e["text"]
                    elif k == "table":
                        rows = e.get("rows") or []
                        if rows and isinstance(rows[0], dict):
                            cols = list(rows[0])
                            rows = [cols] + [[r.get(c, "") for c in cols] for r in rows]
                        widths = [min(30, max(len(str(r[i])) for r in rows if i < len(r))) for i in range(max(len(r) for r in rows))] if rows else []
                        panel = b.data.setdefault("panel", [])
                        if e.get("title"):
                            panel.append(e["title"])
                        for r in rows[:30]:
                            panel.append("  ".join(str(x)[:30].ljust(widths[i]) for i, x in enumerate(r)))
                    elif k == "md":
                        b.data.setdefault("panel", []).extend(e["text"].splitlines()[:40])
                    else:
                        b.events.append(e.get("text", ""))
                    b.touch()
            elif t == "tool_end":
                self.term.reassert()  # the command may have changed the console's modes (Windows)
                b = self.tools_live.pop(e["id"], None)
                if b:
                    b.done, b.ok, b.elapsed = True, e["ok"], e["time"]
                    b.data["output"] = e["output"]
                    b.collapsed = True
                    b.touch()
            elif t == "retry":
                if e.get("discard"):  # the failed attempt's partial answer is not part of the reply
                    for b in (self.cur_text, self.cur_reason):
                        if b is not None and b in self.blocks:
                            self.blocks.remove(b)
                    self.cur_text = self.cur_reason = None
                why = (e.get("error") or "").split("\n")[0][:140]
                self.add("info", text=T(f"↻ {why} — retrying" + (f" in {e['delay']:.0f}s" if e.get("delay") else ""))
                         + (f" ({e['attempt']}/{e['of']})" if e.get("of", 1) > 1 else ""), color="warn")
            elif t == "compacting":
                self.add("info", text=f"◌ compacting history ({e['reason']})…", color="accent")
            elif t == "compacted":
                self.agent.ctx_used = e["ctx"]
                self.add("info", text=f"✓ compacted {e['before']} → {e['after']} messages · context now ~{human(e['ctx'])} tokens", color="ok")
            elif t == "usage":
                self.turn_tokens = (e["in"], e["out"])
            elif t == "todo":
                pass  # drawn live from the session by todo_lines()
            elif t == "verify":
                self.add("info", text="◌ self-check: changes were made but not run yet — verifying before finishing", color="accent")
            elif t == "goal":
                if e["status"] == "working":
                    self.add("info", text=f"◎ goal: {e['text']}", color="accent")
                else:
                    ok = e["status"] == "done"
                    self.add("info", text=T("✓ goal done" if ok else "✗ goal blocked") + (f" — {e['reason']}" if e.get("reason") else ""),
                             color="ok" if ok else "err")
            elif t == "turn_diff":
                files = e.get("files") or []
                if files:
                    stats = []
                    for f in files:
                        add = sum(1 for l in f["diff"].splitlines() if l.startswith("+") and not l.startswith("+++"))
                        rem = sum(1 for l in f["diff"].splitlines() if l.startswith("-") and not l.startswith("---"))
                        stats.append(f"{f['path']} +{add} −{rem}")
                    self.add("info", text="Δ " + " · ".join(stats[:6]) + (f" · +{len(stats) - 6}" if len(stats) > 6 else "") + "   /undo",
                             color="dim")
            elif t == "notice":
                self.add("info", text="! " + e["text"], color="warn")
            elif t == "undone":
                self.add("info", text="↶ " + e["text"], color="warn")
            elif t == "error":
                self.add("info", text=("⏹ " if e["text"] == "interrupted" else "✗ ") + e["text"], color="warn" if e["text"] == "interrupted" else "err")
            elif t == "queued_sent":
                for x in e["texts"]:
                    self.add("user", text=x)
                self.cur_text = self.cur_reason = None
            elif t == "turn_end":
                if self.cur_reason and not self.cur_reason.done:
                    self.cur_reason.done, self.cur_reason.elapsed = True, time.time() - self.cur_reason.t0
                    self.cur_reason.touch()
                # tools cut off by an interrupt/error never get tool_end: close them so no spinner is left running
                for b in self.tools_live.values():
                    b.done, b.ok, b.elapsed, b.collapsed = True, False, time.time() - b.t0, True
                    b.data.setdefault("output", "interrupted")
                    b.touch()
                self.tools_live.clear()
                self.add("stats", text=turn_stats(e))
                self.cur_text = self.cur_reason = None
                self.sync_ws()
                if e.get("time", 0) >= 30 and self.s.config["settings"].get("notify", True) and self.agent.depth == 0:
                    self.term.write("\a")  # a long task finished: the terminal bell, and a desktop notification where there is one
                    title = (self.agent.session.get("title") or "")[:80]
                    threading.Thread(target=desktop_notify, args=(T("me: task finished"), title), daemon=True).start()
            self.redraw()

    # ─── modal helpers (called from worker threads; block until answered) ───
    def _modal(self, m):
        m["ev"] = threading.Event()
        m.setdefault("value", None)
        with self.lock:
            self.modal = m
            self.redraw()
        m["ev"].wait()
        with self.lock:
            if self.modal is m:
                self.modal = None
            self.redraw()
        return m["value"]

    def pick(self, title, items, hint="", current=None, keep=None):
        """items: list of (value, label, detail). Returns value or None. `keep` (a dict) carries the typed filter from one
        call to the next, for pickers reopened in a loop."""
        items = [i if isinstance(i, tuple) else (i, str(i), "") for i in items]
        items = [(v, T(lab), T(det)) for v, lab, det in items]
        title, hint = T(title), T(hint)
        flt = (keep or {}).get("filter", "")
        shown = [it for it in items if fuzzy(flt.lower(), it[1] + " " + it[2]) is not None] if flt else items
        sel = next((i for i, it in enumerate(shown) if current is not None and it[0] == current), 0)
        m = {"kind": "pick", "title": title, "items": items, "sel": sel, "filter": flt, "hint": hint}
        v = self._modal(m)
        if keep is not None:
            keep["filter"] = m["filter"]
        return v

    def prompt(self, title, default="", secret=False, multiline=False, hint=""):
        title, hint = T(title), T(hint)
        return self._modal({"kind": "input", "title": title, "text": str(default or ""), "cur": len(str(default or "")), "secret": secret,
                            "multiline": multiline, "hint": hint})

    def confirm_tool(self, name, a):
        label = self.agent.tools.preview(name, a)
        detail = ""
        if name == "edit":
            detail = ToolBox._diff(a.get("old", ""), a.get("new", ""), "")
        elif name == "write":
            detail = "\n".join(a.get("content", "").splitlines()[:12])
        elif name == "skill":
            detail = json.dumps(a.get("args", {}), ensure_ascii=False)
        always = (f"Yes, always allow `{allow_pattern(a.get('cmd', ''))}`" if name == "bash" else "Yes, and switch to full access (/perm)")
        v = self._modal({"kind": "confirm", "title": f"{name}  {label}", "detail": detail, "sel": 0,
                         "items": [("y", T("Yes")), ("a", T(always)), ("n", T("No, tell me what to do"))]})
        if v == "a" and name != "bash":
            self.s.config["settings"]["perm"] = "full"
            self.s.save()
        return "always" if v == "a" else v == "y"

    def ask_text(self, q, options=None):
        if options:
            return self.pick(q, [(o, o, "") for o in options]) or ""
        return self.prompt(q) or ""

    # ─── lifecycle ───
    def welcome(self):
        self.add("welcome", text=os.getcwd().replace(str(Path.home()), "~"))

    def startup(self):
        PLUGINS.collect("startup", self)
        for n, e in PLUGINS.errors.items():
            self.info(f"✗ plugin {n}: {e}", "err")
        if not self.s.auth.get("token") and not self.s.auth.get("skip"):
            v = self.pick("Welcome to me — sync everything across your machines", [
                ("login", "log in / create account", "config, keys, skills & chats follow you everywhere"),
                ("skip", "use offline", "you can /login any time")])
            if v == "login":
                self.cmd_login("")
            elif v == "skip":
                self.s.auth["skip"] = True
                self.s.save_auth()
        elif self.cloud.online:
            n = self.cloud.sync()
            if n > 0:
                self.retheme()
                self.toast(f"☁ {n} items synced from cloud", "ok")
            elif n < 0:
                self.toast(f"☁ sync failed: {self.cloud.last_error[:60]}", "err")
        if not self.s.config.get("models"):
            self.info("Let's connect a model. Any OpenAI-compatible API works (OpenAI, OpenRouter, DeepSeek, Groq, Ollama, LM Studio…).")
            self.setup_provider()
        try:
            r = self.cloud.req("GET", "/api/version", token=False, timeout=5)
            if r.get("version") and r["version"] != VERSION:
                self.update_avail = r["version"]
        except Exception:
            pass

    def safe_key(self, k):
        """A key that trips a bug is logged and shown, never fatal: a crash here used to drop the user into the shell with the
        rest of a big paste still queued — and cmd.exe ran every pasted line as a command."""
        try:
            with self.lock:
                self.handle_key(k)
        except Exception as e:
            log_error("key")
            self.toast(f"✗ {type(e).__name__}: {e} (details: {HOME / 'error.log'})", "err")

    def take_pending(self):
        "Called by the agent after each tool step: queued plain messages go into the running turn (commands and @images wait)."
        with self.lock:
            out = [t for t in self.pending if not re.search(r"@\S+\.(png|jpe?g|gif|webp|bmp)\b", t, re.I)]
            self.pending = [t for t in self.pending if t not in out]
            return out

    def drain_pending(self):
        "Send the next queued message once the agent is free (one at a time: the next waits for this turn)."
        if self.pending and not self.agent.busy and not self.launching:
            with self.lock:
                self.dispatch(self.pending.pop(0))
            self.dirty = True

    def run(self):
        self.restart = False
        self.term.start(self.s.config["settings"].get("mouse", True))
        if not WIN:
            signal.signal(signal.SIGWINCH, lambda *a: self.redraw())
        self.welcome()
        self.bg(self.startup)
        self.cloud.watch(self._on_remote_change, lambda v: setattr(self, "update_avail", v),
                         lambda: not self.agent.busy and self.sync_ws())
        last, last_sync = 0, time.time()
        try:
            while self.running:
                if self.cloud.online and not self.agent.busy and time.time() - last_sync > 60:
                    last_sync = time.time()
                    self.sync_ws()
                try:
                    k = self.term.q.get(timeout=0.05)
                    self.safe_key(k)
                    while not self.term.q.empty():
                        self.safe_key(self.term.q.get_nowait())
                    self.dirty = True
                except queue.Empty:
                    pass
                try:
                    self.drain_pending()
                except Exception:
                    log_error("queue")
                # window size is polled: resize / minimize+restore events are not delivered reliably (SIGWINCH does not exist on
                # Windows, and Windows Terminal does not forward buffer-size records), so a stale size leaves a box wider than the window
                sz = self.term.size()
                if sz != getattr(self, "_last_sz", sz):
                    self._prev_rows = None
                    self.term.write(CSI + "2J")
                    self.dirty = True
                self._last_sz = sz
                if PLUGINS.emit("tick", self):
                    self.dirty = True
                animating = self.agent.busy or self.tools_live or self.cloud.status == "syncing" or (self.notice[0] and time.time() - self.notice[1] < 4.5)
                now = time.time()
                if (self.dirty or (animating and now - last > 0.08)) and now - last > 0.016:
                    self.frame += 1
                    try:
                        self.draw()
                    except Exception as e:
                        try:  # keep the full traceback: the one-line message alone is not enough to fix it
                            with open(self.s.home / "render-error.log", "a", encoding="utf-8") as f:
                                f.write(f"{time.strftime('%F %T')} me {VERSION}\n{traceback.format_exc()}\n")
                        except OSError:
                            pass
                        self.term.write(f"{CSI}H{CSI}2Jrender error: {e} (details: {self.s.home / 'render-error.log'})")
                        self._prev_rows = None
                    last = now
        finally:
            self.agent.cancel.set()
            self.term.stop()
            if self.agent.session["messages"]:
                self.s.save_session(self.agent.session)
            if self.cloud.online:
                print("☁ syncing…", end="", flush=True)
                self.cloud.sync()
                print("\r" + " " * 12 + "\r", end="")
            u = self.agent.session.get("usage", {})
            if self.agent.session["messages"]:
                print(f"me · session {self.agent.session['id']} · ↑{human(u.get('in', 0))} ↓{human(u.get('out', 0))}  (resume: /sessions)")
        if self.restart:
            args = [sys.executable, os.path.abspath(__file__)] + sys.argv[1:]
            if WIN:
                # os.execv does not replace the process on Windows: the old one exits at once, the shell takes the console back
                # and fights the new process for keystrokes. Wait for the child instead.
                sys.exit(subprocess.call(args))
            os.execv(*(args[0], args))



def turn_stats(e):
    """One honest line per turn: every step re-sends the context, so ↑ is the sum over steps; cached input is billed at a fraction."""
    steps = e.get("steps") or 1
    cached = e.get("cached", 0)
    parts = [dur(e["time"]), T(f"{steps} step{'s' if steps != 1 else ''}"), T(f"context {human(e.get('ctx', 0))}"),
             T(f"sent ↑{human(e['in'])}" + (f" ({int(100 * cached / e['in'])}% cached)" if cached and e["in"] else "")), f"↓{human(e['out'])}"]
    if e.get("cost"):
        parts.append(money(e["cost"]))
    return "  ·  ".join(parts)


def fuzzy(q, s):
    """Subsequence match score (lower = better) or None."""
    if not q:
        return 0
    s_l = s.lower()
    if s_l.startswith(q):
        return -100 + len(s)
    i = s_l.find(q)
    if i >= 0:
        return -50 + i
    pos, score = 0, 0
    for ch in q:
        j = s_l.find(ch, pos)
        if j < 0:
            return None
        score += j - pos
        pos = j + 1
    return score


SKILL_TEMPLATE = '''"""
name: NAME
description: one line — when should the agent use this skill
icon: ◆
color: purple
env:
requires:
---
Instructions for the model, loaded only when the skill is used.
Explain when to call which action and any gotchas.
ctx: progress(it,label) bar(n,total) log() status() ask(q) choose(q,opts) confirm(q) table(rows) md(text) http env cwd
"""


@action
def hello(ctx, who: str = "world"):
    "Say hello (example action)"
    for step in ctx.progress(range(3), "warming up"):
        ctx.log(f"step {step}")
    return f"hello, {who}!"


@action
def pick(ctx):
    "Interactive example: ask the user, show a table"
    lang = ctx.choose("Which language?", ["python", "go", "rust"])
    if not ctx.confirm(f"Show stats for {lang}?"):
        return "cancelled"
    ctx.table([{"lang": lang, "typing": "static" if lang != "python" else "dynamic"}], title="stats")
    return f"picked {lang}"


@action(confirm=True)  # asks the user before running
def danger(ctx, path: str):
    "Example of an action that needs confirmation"
    ctx.status(f"would touch {path}")
    return {"ok": True}
'''


PLUGIN_TEMPLATE = '''"""
name: NAME
description: what this plugin changes in me
---
Plugins run inside me with full access: `plugin` registry + `me` module.
"""


@plugin.command("hello", "example command from NAME")
def hello(tui, arg):
    tui.info(f"hello from NAME plugin! arg={arg!r}", "ok")


@plugin.tool("now", "Current local date and time.", {})
def now(args, ctx):
    import time
    return time.strftime("%Y-%m-%d %H:%M:%S")


@plugin.on("event")
def on_event(e):
    if e["type"] == "turn_end" and e["time"] > 60:
        pass  # e.g. notify somewhere about long tasks
'''


def meta_line(text):
    m = re.search(r"^description:\s*(.+)$", text, re.M)
    return m.group(1).strip()[:60] if m else ""


# ───────────────────────────── main ─────────────────────────────


def main():
    args = sys.argv[1:]
    store = Store()
    if args and args[0] in ("-p", "--print", "--rpc", "web", "--web"):
        # one-shot and server modes need the account's config before the first turn; the TUI syncs in the background instead
        # (a blocking sync here kept `me` from starting for up to 30 s whenever the server was slow or unreachable)
        if store.auth.get("token"):
            Cloud(store).sync()
            store.config = jload(store.cfg_path) or store.config
    if not (args and args[0] in ("-v", "--version", "-h", "--help", "--sync")):
        PLUGINS.load(store)
    if args and args[0] in ("-v", "--version"):
        return print(f"me {VERSION}")
    if args and args[0] == "studio":
        return run_studio(store, args[1:])
    if args and args[0] in ("-h", "--help"):
        return print(f"me {VERSION} — minimal cloud-synced agent\n  me              TUI\n  me -p \"task\"     run once (add -y to auto-approve)\n"
                     f"  me web [port]   browser UI on localhost:8080 (--lan to open on your network)\n"
                     f"  me studio       desktop command center: parallel tasks in git worktrees, review, apply, commit (--browser, update)\n  me --rpc        JSON-lines agent over stdio (for web/telegram/IDE frontends)\n  me --sync       sync with cloud and exit\n"
                     f"  env: ME_HOME={HOME}  ME_SERVER={SERVER}")
    if args and args[0] in ("web", "--web"):
        port = next((int(a) for a in args[1:] if a.isdigit()), 8080)
        host = "0.0.0.0" if "--lan" in args else "127.0.0.1"
        return run_web(store, host, port, "--no-open" not in args)
    if args and args[0] == "--sync":
        c = Cloud(store)
        n = c.sync()
        return print(f"synced: {n}" if n >= 0 else f"sync failed: {c.last_error}")
    if args and args[0] == "--rpc":
        return run_rpc(store)
    if args and args[0] in ("-p", "--print"):
        yolo = "-y" in args
        rest = [a for a in args[1:] if a != "-y"]
        prompt = " ".join(rest) or sys.stdin.read()
        c = Cloud(store)
        run_headless(store, prompt, yolo)
        if store.auth.get("token"):
            c.sync()
        return
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        if WIN and (os.environ.get("TERM_PROGRAM") == "mintty" or os.environ.get("MSYSTEM")):
            return print("me: Git Bash's window (mintty) is not a Windows console. Run `winpty me`, or open me in Windows Terminal / cmd.exe.")
        return print("me: not a terminal. use: me -p \"task\"")
    tui = TUI(store)
    PLUGINS.tui = tui
    tui.run()


def run_studio(store, args):
    """me studio: the desktop app (apps/studio/me_studio.py). Taken from a checkout next to me.py, else from the server into
    ~/.me/apps, refreshed on every start when the server has a newer one (offline: the copy already there)."""
    here = Path(os.path.abspath(__file__)).parent
    local = here / "apps" / "studio" / "me_studio.py"
    path = local if local.is_file() else HOME / "apps" / "me_studio.py"
    if path != local:
        try:
            src = Cloud(store).req("GET", "/studio.py", token=False, timeout=60 if not path.is_file() or "update" in args else 8)
            src = src.decode("utf-8") if isinstance(src, bytes) else src
            if "def main(" not in str(src):
                raise CloudError("bad download")
            if not path.is_file() or path.read_bytes() != str(src).encode("utf-8"):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(str(src).encode("utf-8"))
                print(f"me studio updated → {path}")
        except CloudError as e:
            if not path.is_file():
                return print(f"me studio: could not download it ({e}). Get it from the releases page: me-studio.exe, or apps/studio/me_studio.py")
    sys.modules.setdefault("me", sys.modules[__name__])  # the studio imports me: give it this very module
    import runpy
    sys.argv = [str(path)] + [a for a in args if a != "update"]
    runpy.run_path(str(path), run_name="__main__")


if __name__ == "__main__":
    main()
