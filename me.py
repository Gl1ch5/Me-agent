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
import ast, base64, difflib, fnmatch, glob, hashlib, io, json, os, platform, queue, re
import shutil, signal, subprocess, sys, tempfile, textwrap, threading, time, unicodedata
import urllib.error, urllib.request, uuid, zipfile, zlib
from pathlib import Path

VERSION = "0.5.12"
SERVER = os.environ.get("ME_SERVER", "https://me-agent.ru")
HOME = Path(os.environ.get("ME_HOME", Path.home() / ".me"))
WIN = os.name == "nt"
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
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), "utf-8")
    os.replace(tmp, p)


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
        name = name or self.config.get("model")
        for m in self.config["models"]:
            if m["name"] == name:
                return m
        return self.config["models"][0] if self.config["models"] else None

    def provider(self, m):
        return self.config["providers"].get(m.get("provider", ""), {})

    # sessions
    def session_path(self, sid):
        return self.home / "sessions" / f"{sid}.json"

    def sessions(self):
        out = []
        for p in (self.home / "sessions").glob("*.json"):
            d = jload(p)
            if d:
                out.append(d)
        return sorted(out, key=lambda s: s.get("updated", 0), reverse=True)

    def save_session(self, s):
        s["updated"] = time.time()
        msgs = []
        for m in s["messages"]:  # drop image payloads from disk/sync
            if isinstance(m.get("content"), list):
                m = dict(m, content=[c if c.get("type") != "image_url" else {"type": "text", "text": "[image]"} for c in m["content"]])
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
        jsave(self.state_path, {"cursor": 0, "hashes": {}})

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
            local = self._local()
            push = []
            for key, p in local.items():
                try:
                    content = p.read_text("utf-8")
                except Exception:
                    continue
                h = hashlib.sha1(content.encode()).hexdigest()
                if hashes.get(key) != h:
                    push.append({"key": key, "content": content, "ts": p.stat().st_mtime, "deleted": False})
            for key in list(hashes):
                if key not in local:
                    push.append({"key": key, "content": "", "ts": time.time(), "deleted": True})
            try:
                r = self.req("POST", "/api/sync", {"cursor": st.get("cursor", 0), "push": push}, timeout=60)
            except CloudError as e:
                self.status, self.last_error = "error", str(e)
                return -1
            for d in push:
                if d["deleted"]:
                    hashes.pop(d["key"], None)
                else:
                    hashes[d["key"]] = hashlib.sha1(d["content"].encode()).hexdigest()
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
                    continue
                h = hashlib.sha1(d["content"].encode()).hexdigest()
                if hashes.get(d["key"]) == h and p.exists():
                    continue
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(d["content"], "utf-8")
                hashes[d["key"]] = h
                changed += 1
            jsave(self.state_path, {"cursor": r.get("cursor", 0), "hashes": hashes})
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
                out[rel] = (hashlib.sha1(p.read_bytes()).hexdigest(), st.st_mtime)
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


class Provider:
    """OpenAI-compatible streaming client. Messages use chat/completions shape internally."""

    def __init__(self, model, prov):
        self.m, self.p = model, prov
        self.base = (prov.get("base_url") or "https://api.openai.com/v1").rstrip("/")

    def _post(self, path, body):
        r = urllib.request.Request(self.base + path, data=json.dumps(body).encode(), method="POST")
        r.add_header("Content-Type", "application/json")
        r.add_header("Accept", "text/event-stream")
        if self.p.get("api_key"):
            r.add_header("Authorization", "Bearer " + self.p["api_key"])
        try:
            return urllib.request.urlopen(r, timeout=600)
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:800]}")

    @staticmethod
    def _sse(resp, cancel):
        buf = b""
        while True:
            if cancel.is_set():
                resp.close()
                raise Cancelled()
            chunk = resp.readline()
            if not chunk:
                break
            line = chunk.strip()
            if line.startswith(b"data:"):
                data = line[5:].strip()
                if data == b"[DONE]":
                    break
                buf = data
                try:
                    yield json.loads(buf)
                except ValueError:
                    pass

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
        """Returns (assistant_message, usage). Emits text/reasoning deltas. The system prompt + tools form a stable prefix."""
        if self.m.get("api") == "responses":
            return self._responses(system, messages, tools, emit, cancel, cache_key)
        cm = self.cache_mode()
        sysmsg = {"role": "system", "content": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}] if cm == "anthropic" else system}
        body = {"model": self.m["id"], "stream": True, "stream_options": {"include_usage": True},
                "messages": [sysmsg] + messages}
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
        text, calls, usage = "", {}, {}
        resp = self._post("/chat/completions", body)
        for ev in self._sse(resp, cancel):
            if ev.get("usage"):
                usage = ev["usage"]
            for ch in ev.get("choices") or []:
                d = ch.get("delta") or {}
                r = d.get("reasoning_content") or d.get("reasoning")
                if isinstance(r, str) and r:
                    emit({"type": "reasoning", "text": r})
                if d.get("content"):
                    text += d["content"]
                    emit({"type": "text", "text": d["content"]})
                for tc in d.get("tool_calls") or []:
                    c = calls.setdefault(tc.get("index", 0), {"id": "", "name": "", "args": ""})
                    c["id"] = tc.get("id") or c["id"]
                    f = tc.get("function") or {}
                    c["name"] += f.get("name") or ""
                    c["args"] += f.get("arguments") or ""
        msg = {"role": "assistant", "content": text or None}
        if calls:
            msg["tool_calls"] = [{"id": c["id"] or "call_" + uuid.uuid4().hex[:8], "type": "function",
                                  "function": {"name": c["name"], "arguments": c["args"] or "{}"}} for _, c in sorted(calls.items())]
        return msg, {"in": usage.get("prompt_tokens", 0), "out": usage.get("completion_tokens", 0), "cached": self._cached(usage)}

    def _responses(self, system, messages, tools, emit, cancel, cache_key=None):
        items = []
        for m in messages:
            if m["role"] == "tool":
                items.append({"type": "function_call_output", "call_id": m["tool_call_id"], "output": m["content"]})
            elif m["role"] == "assistant":
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
        if self.m.get("max_out"):
            body["max_output_tokens"] = int(self.m["max_out"])
        if self.m.get("temperature") not in (None, ""):
            body["temperature"] = float(self.m["temperature"])
        text, calls, usage = "", {}, {}
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
            elif t == "response.completed":
                usage = ev.get("response", {}).get("usage") or {}
            elif t in ("response.failed", "error"):
                raise RuntimeError(json.dumps(ev.get("response", {}).get("error") or ev)[:800])
        msg = {"role": "assistant", "content": text or None}
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


def read_any(path, offset=1, limit=2000):
    p = Path(path).expanduser()
    if not p.exists():
        return f"error: {path} not found", None
    if p.is_dir():
        items = sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))
        return "\n".join(x.name + ("/" if x.is_dir() else "") for x in items[:500]) or "(empty dir)", None
    ext = p.suffix.lower()
    if ext in IMAGE_EXT:
        b64 = base64.b64encode(p.read_bytes()).decode()
        return f"[image {p.name}, {p.stat().st_size} bytes attached]", f"data:{IMAGE_EXT[ext]};base64,{b64}"
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
            if b"\0" in data[:8192]:
                head = data[:512]
                hexd = "\n".join(f"{i:08x}  {head[i:i + 16].hex(' ')}" for i in range(0, len(head), 16))
                return f"[binary {p.name}, {len(data)} bytes]\n{hexd}", None
            text = data.decode("utf-8", "replace")
    except Exception as e:
        return f"error reading {path}: {e}", None
    lines = text.splitlines()
    offset = max(1, int(offset or 1))
    limit = int(limit or 2000)
    sel = lines[offset - 1: offset - 1 + limit]
    w = len(str(offset + len(sel)))
    out = "\n".join(f"{i:>{w}}│{l[:2000]}" for i, l in enumerate(sel, offset))
    if offset - 1 + limit < len(lines):
        out += f"\n… {len(lines) - (offset - 1 + limit)} more lines (use offset)"
    return out or "(empty file)", None


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


def parse_skill(path):
    p = Path(path)
    text = p.read_text("utf-8")
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
                defaults = [ast.unparse(d) for d in a.defaults] if hasattr(ast, "unparse") else []
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
        out = {}
        for p in sorted((self.s.home / "skills").iterdir()):
            if p.is_file() and p.suffix in (".py", ".md"):
                sk = parse_skill(p)
                out[sk["name"]] = sk
        return out

    def enabled(self):
        dis = set(self.s.config["settings"].get("disabled_skills", []))
        return {k: v for k, v in self.all().items() if k not in dis}

    def run(self, sk, action, args, emit, cancel, ask=None):
        if sk.get("requires"):
            deps = Path(sk["path"]).parent / ".deps"
            mark = deps / (".ok-" + hashlib.sha1(sk["requires"].encode()).hexdigest()[:8])
            if not mark.exists():
                emit({"t": "status", "text": f"pip install {sk['requires']}"})
                subprocess.run([sys.executable, "-m", "pip", "install", "-q", "--target", str(deps)] + sk["requires"].replace(",", " ").split(),
                               capture_output=True)
                deps.mkdir(exist_ok=True)
                mark.touch()
        env = dict(os.environ)
        env.update({k: str(v) for k, v in self.s.config.get("secrets", {}).items()})
        env["PYTHONIOENCODING"] = "utf-8"
        proc = subprocess.Popen([sys.executable, "-c", SKILL_RUNNER, sk["path"], action, json.dumps(args)], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, text=True, encoding="utf-8", bufsize=1)
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


TOOLS = [
    {"name": "read", "description": "Read file or dir (code, pdf, office, image).",
     "parameters": _obj({"path": "string", "offset": "integer", "limit": "integer"}, ["path"])},
    {"name": "write", "description": "Create/overwrite file.", "parameters": _obj({"path": "string", "content": "string"}, ["path", "content"])},
    {"name": "edit", "description": "Replace exact old→new; all=true for every match; lines='a-b'; edits=[…].",
     "parameters": _obj({"path": "string", "old": "string", "new": "string", "all": "boolean", "lines": "string",
                         "edits": {"type": "array", "items": {"type": "object"}}}, ["path"])},
    {"name": "bash", "description": "Run shell command in cwd.", "parameters": _obj({"cmd": "string", "timeout": "integer"}, ["cmd"])},
    {"name": "ls", "description": "Glob files; pattern=map gives repo map.",
     "parameters": _obj({"pattern": "string"}, ["pattern"])},
    {"name": "grep", "description": "Regex search in files.", "parameters": _obj({"pattern": "string", "path": "string", "glob": "string"}, ["pattern"])},
    {"name": "web_search", "description": "Search the web.", "parameters": _obj({"query": "string"}, ["query"])},
    {"name": "web_fetch", "description": "Fetch URL as text.", "parameters": _obj({"url": "string"}, ["url"])},
    {"name": "skill", "description": "Use a skill (no action = guide).", "parameters": _obj({"name": "string", "action": "string", "args": "object"}, ["name"])},
    {"name": "todo", "description": "Set checklist (whole list); 'x ' done, '> ' doing.",
     "parameters": _obj({"items": {"type": "array", "items": {"type": "string"}}}, ["items"])},
    {"name": "memory", "description": "Global memory: text adds a note, forget=id removes one.",
     "parameters": _obj({"text": "string", "forget": "string"}, [])},
    {"name": "task", "description": "Run a subagent on a job; returns its report.",
     "parameters": _obj({"agent": "string", "prompt": "string"}, ["prompt"])},
]
# tools added only when usable (keeps the idle prompt small)
NOTIFY_TOOL = {"name": "notify", "description": "Send the user a message/file via their Telegram.",
               "parameters": _obj({"text": "string", "file": "string"}, ["text"])}
MCP_TOOL = {"name": "mcp", "description": "Use an MCP server: no tool → its tools; else call tool with args.",
            "parameters": _obj({"server": "string", "tool": "string", "args": "object"}, ["server"])}
READONLY_TOOLS = {"read", "ls", "grep", "web_search", "web_fetch"}
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".mypy_cache", "dist", "build", ".next", ".idea"}


def walk_files(root, limit=20000):
    n = 0
    for d, dirs, files in os.walk(root):
        dirs[:] = [x for x in dirs if x not in SKIP_DIRS and not x.startswith(".")]
        for f in files:
            yield os.path.join(d, f)
            n += 1
            if n >= limit:
                return


def shell_env(secrets):
    """The user's secrets as env vars; with GITHUB_TOKEN, git can clone/push github.com without any setup."""
    env = dict(os.environ, **{k: str(v) for k, v in (secrets or {}).items() if re.fullmatch(r"[A-Za-z_]\w*", k)})
    tok = env.get("GITHUB_TOKEN") or env.get("GH_TOKEN")
    if tok:
        env.setdefault("GH_TOKEN", tok)
        n = int(env.get("GIT_CONFIG_COUNT") or 0)
        env.update({f"GIT_CONFIG_KEY_{n}": "credential.https://github.com.helper",
                    f"GIT_CONFIG_VALUE_{n}": '!f() { echo username=x-access-token; echo "password=$GH_TOKEN"; }; f',
                    "GIT_CONFIG_COUNT": str(n + 1), "GIT_TERMINAL_PROMPT": "0"})
    return env


def run_shell(cmd, timeout, cancel, env=None):
    kw = {} if WIN else {"start_new_session": True}
    proc = subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, env=env, **kw)
    out = []
    threading.Thread(target=lambda: out.append(proc.stdout.read()), daemon=True).start()
    t0 = time.time()
    while proc.poll() is None:
        if cancel.is_set() or time.time() - t0 > timeout:
            try:
                os.killpg(proc.pid, signal.SIGKILL) if not WIN else proc.kill()
            except Exception:
                proc.kill()
            proc.wait()
            if cancel.is_set():
                raise Cancelled()
            time.sleep(0.1)
            return f"{(out[0] if out else b'').decode('utf-8', 'replace')}\n[timed out after {timeout}s]"
        time.sleep(0.05)
    time.sleep(0.05)
    for _ in range(40):
        if out:
            break
        time.sleep(0.05)
    text = (out[0] if out else b"").decode("utf-8", "replace")
    return clip(text, 30000) + (f"\n[exit {proc.returncode}]" if proc.returncode else "")


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
        tmp = Path(tempfile.mkstemp(suffix=".pdf")[1])
        tmp.write_bytes(data)
        return True, clip(_pdf_text(tmp), 40000), None
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

    def preview(self, name, a):
        """Short human label for UI."""
        if name in ("read", "write", "edit"):
            p = str(a.get("path", ""))
            try:
                rel = os.path.relpath(p)
                return rel if not rel.startswith("..") else p
            except ValueError:
                return p
        if name == "bash":
            return a.get("cmd", "")
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
            p = os.path.abspath(os.path.expanduser(a.get("path", "")))
            root = os.path.abspath(cwd or os.getcwd())
            return not (p == root or p.startswith(root.rstrip(os.sep) + os.sep))
        if name in PLUGINS.tools:
            return PLUGINS.tools[name].get("confirm", False)
        if name == "skill" and a.get("action"):
            sk = self.skills.enabled().get(a.get("name"), {})
            return sk.get("actions", {}).get(a["action"], {}).get("confirm", False)
        return False

    def run(self, name, a, emit, cancel, ask=None):
        """Returns (ok, text, image_data_url|None)."""
        if name == "read":
            out, img = read_any(a["path"], a.get("offset", 1), a.get("limit", 2000))
            return not out.startswith("error"), out, img
        if name == "write":
            p = Path(a["path"]).expanduser()
            p.parent.mkdir(parents=True, exist_ok=True)
            old = p.read_text("utf-8", "replace") if p.exists() else None
            p.write_text(a["content"], "utf-8")
            emit({"t": "diff", "text": self._diff(old or "", a["content"], p.name)})
            return True, f"wrote {p} ({len(a['content'].splitlines())} lines)", None
        if name == "edit":
            p = Path(a["path"]).expanduser()
            if not p.exists():
                return False, f"error: {p} not found (use write to create it)", None
            text = p.read_text("utf-8")
            new, msg = apply_edits(text, a)
            if new is None:
                return False, msg, None
            p.write_text(new, "utf-8")
            emit({"t": "diff", "text": self._diff(text, new, p.name)})
            return True, f"edited {p} ({msg})", None
        if name == "bash":
            out = run_shell(a["cmd"], int(a.get("timeout") or 120), cancel, shell_env(self.s.config.get("secrets")))
            return "[exit " not in out[-12:], out or "(no output)", None
        if name == "ls":
            pat = a["pattern"]
            if pat == "map" or pat.startswith("map "):
                return True, repo_map(os.getcwd(), pat[4:].strip(), self.s.home), None
            hits = [h for h in glob.glob(pat, recursive=True) if not any(f"{os.sep}{d}{os.sep}" in os.sep + h for d in SKIP_DIRS)]
            hits.sort(key=lambda h: -os.path.getmtime(h) if os.path.exists(h) else 0)
            return True, "\n".join(hits[:300]) + (f"\n… {len(hits) - 300} more" if len(hits) > 300 else "") or "no matches", None
        if name == "grep":
            return True, self._grep(a["pattern"], a.get("path") or ".", a.get("glob")), None
        if name == "skill":
            return self._skill(a, emit, cancel, ask)
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
    def _grep(pattern, path, gl):
        if shutil.which("rg"):
            cmd = ["rg", "-n", "--no-heading", "-M", "300", "-m", "50", pattern, path] + (["-g", gl] if gl else [])
            r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
            return clip(r.stdout, 20000) or "no matches"
        try:
            rx = re.compile(pattern)
        except re.error as e:
            return f"error: {e}"
        out = []
        files = [path] if os.path.isfile(path) else walk_files(path)
        for f in files:
            if gl and not fnmatch.fnmatch(os.path.basename(f), gl):
                continue
            try:
                with open(f, encoding="utf-8") as fh:
                    for i, line in enumerate(fh, 1):
                        if rx.search(line):
                            out.append(f"{f}:{i}:{line.rstrip()[:300]}")
                            if len(out) >= 300:
                                return "\n".join(out) + "\n… (truncated)"
            except (UnicodeDecodeError, OSError):
                continue
        return "\n".join(out) or "no matches"

    def _skill(self, a, emit, cancel, ask):
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
        ok, out = self.skills.run(sk, act, a.get("args") or {}, emit, cancel, ask)
        return ok, clip(out, 30000), None


def _closest(text, old):
    """Hint for a failed match: the most similar line in the file, with its number."""
    first = next((l for l in old.splitlines() if l.strip()), old).strip()
    stripped = [l.strip() for l in text.splitlines()]
    best = difflib.get_close_matches(first, stripped, n=1, cutoff=0.5)
    if not best:
        return " Read the file again and copy the text exactly (or use lines='a-b')."
    i = stripped.index(best[0])
    return f" Closest is line {i + 1}: {text.splitlines()[i][:200]!r}. Read the file again and copy exactly (or use lines='a-b')."


def _replace_one(text, old, new, all_):
    if not old:
        return None, "error: old is empty (to insert, include a nearby line in old, or use lines='a-b')"
    n = text.count(old)
    if n == 0:
        norm = lambda s: "\n".join(l.rstrip() for l in s.replace("\r\n", "\n").split("\n"))
        t2, o2 = norm(text), norm(old)  # tolerate trailing-whitespace / CRLF slips, common with weak models
        if o2 and t2.count(o2) == 1:
            return t2.replace(o2, norm(new)), "1 replacement, whitespace-normalized"
        return None, "error: old text not found." + _closest(text, old)
    if n > 1 and not all_:
        return None, f"error: old text matches {n} times; add surrounding lines to make it unique, or set all=true"
    return text.replace(old, new), f"{n} replacement{'s' if n > 1 else ''}"


def apply_edits(text, a):
    """edit tool: one old/new pair, several (edits=[…]), or lines a..b (1-based, inclusive) replaced by new.
    All-or-nothing. Returns (new_text, summary) or (None, error)."""
    if a.get("lines") not in (None, ""):
        lines = text.split("\n")
        m = re.fullmatch(r"\s*(\d+)\s*(?:[-:,.]+\s*(\d+))?\s*", str(a["lines"]))
        if not m:
            return None, "error: lines must look like '12-20' (or '12' for one line)"
        lo, hi = int(m.group(1)), int(m.group(2) or m.group(1))
        if lo < 1 or hi < lo - 1 or lo > len(lines) + 1 or hi > len(lines):
            return None, f"error: lines {lo}-{hi} are outside the file (1-{len(lines)}); 'a-(a-1)' inserts before line a"
        repl = a.get("new") or ""
        rl = [] if repl == "" else repl[:-1].split("\n") if repl.endswith("\n") else repl.split("\n")
        return "\n".join(lines[:lo - 1] + rl + lines[hi:]), f"lines {lo}-{hi} → {len(rl)} line{'s' if len(rl) != 1 else ''}"
    pairs = list(a.get("edits") or [])
    if a.get("old") is not None:
        pairs.insert(0, {"old": a["old"], "new": a.get("new", ""), "all": a.get("all")})
    if not pairs:
        return None, "error: give old+new, edits=[{old,new}], or lines='a-b' with new"
    done = []
    for i, e in enumerate(pairs, 1):
        if not isinstance(e, dict) or "old" not in e:
            return None, f"error: edit #{i} needs old and new"
        text, msg = _replace_one(text, e["old"], e.get("new", ""), e.get("all") or a.get("all"))
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

ALL_TOOLS = "read, write, edit, bash, ls, grep, web_search, web_fetch, skill, todo, task, memory"
READ_TOOLS = "read, ls, grep, web_search, web_fetch, skill, todo"

BUILTIN_PRESETS = {
    "code": {"label": "code", "description": "default engineer: build, fix, refactor", "tools": ALL_TOOLS, "writes": "*", "prompt": ""},
    "me": {"label": "me", "description": "self-extending: builds skills, plugins, themes and agents for me itself", "tools": ALL_TOOLS, "writes": "*",
           "prompt": """Mode: ME (self-extension). You extend this very agent for the user. Files go in the me home folder ({home}); they sync to every device by themselves.
What you can create (always write the file, then say how to use it):
- skill  {home}/skills/<name>.py (or .md = guide only). Header docstring: name, description (one line: when to use), icon, color, then --- and instructions. Actions: `@action def f(ctx, arg: str = ""): "doc"` with ctx.log/status/bar/progress/ask/choose/confirm/table/md/http/env; return text. `@action(confirm=True)` for risky ones. Usable immediately.
- agent  {home}/agents/<name>.md. Front matter name, label, description, tools (comma list of read, write, edit, bash, ls, grep, web_search, web_fetch, skill, todo, task), writes (glob of writable paths, empty = read-only), then --- and the prompt. Selectable in /agent.
- theme  {home}/themes/<name>.json: {"name","label","dark":true,"colors":{bg,bg2,bg3,line,fg,dim,faint,accent,accent2,ok,err,warn,user,code_bg,sel}} as #rrggbb. Keep contrast >= 4.5:1 for fg on bg; apply with /theme.
- plugin {home}/plugins/<name>.py: `@plugin.command("name","desc") def f(tui, arg)`, `@plugin.tool("name","desc",{"arg":"string"}) def f(args, ctx)`, `@plugin.on("event")`. Plugins run with full access and load on restart: keep them small, tell the user to restart me.
- prompt/settings: edit {home}/config.json (valid JSON, keep other keys).
Rules: read an existing item before changing it; pick lowercase-hyphen names; test a skill by calling it (skill tool) and a theme by parsing the JSON; never put secrets in files (use env: / /secret). Ask before deleting or overwriting something the user made. Offer to publish good items to the marketplace."""},
    "design": {"label": "design", "description": "UI/UX designer: DESIGN.md first, then pixel-perfect UI", "tools": ALL_TOOLS, "writes": "*",
               "prompt": """Mode: DESIGN. You are a senior product designer who also ships front-end code.
1. Before any UI code, create or update DESIGN.md: product goal and audience, design principles, tokens (palette: neutral + one accent + one semantic; type scale; spacing scale 4/8; radii; shadows), components and their states (default/hover/active/disabled/empty/loading/error), layout grid and breakpoints (mobile first), motion rules, accessibility (contrast >= 4.5:1, focus rings, hit areas >= 44px), and the folder structure (tokens/, components/, pages/, assets/icons/).
2. Implement from DESIGN.md exactly; tokens as CSS variables; light and dark.
3. No emoji. Icons: custom inline SVG, one stroke width. No stock gradients or generic templates.
4. Check every screen at 360px and 1280px widths; fix overflow, alignment and spacing before finishing.
Keep DESIGN.md in sync with what you build."""},
    "plan": {"label": "plan", "description": "architect: explore, then write PLAN.md — no code changes", "tools": READ_TOOLS + ", write", "writes": "*.md",
             "prompt": """Mode: PLAN. You are a software architect. Do not change code.
Explore the project, then write PLAN.md: goal, constraints, current architecture, proposed design (modules, data flow, interfaces), step-by-step tasks small enough to do one at a time, risks and how to verify each step. Prefer the simplest design that leaves room to grow.
Finally put the same steps into the todo tool, so the plan can be executed and tracked."""},
    "review": {"label": "review", "description": "code reviewer: finds real bugs, read-only", "tools": READ_TOOLS + ", bash", "writes": "",
               "prompt": """Mode: REVIEW. Review the changes (git diff, or the files named). Report only real problems: bugs, security issues, data loss, race conditions, broken edge cases, missing tests for risky logic. For each: file:line, what breaks, a concrete fix. Skip style nitpicks. You may run tests and read-only commands; do not modify files."""},
    "debug": {"label": "debug", "description": "debugger: reproduce, find root cause, fix, prove", "tools": ALL_TOOLS, "writes": "*",
              "prompt": """Mode: DEBUG. 1) Reproduce the failure and show it. 2) Form hypotheses, test them with the cheapest experiment. 3) Fix the root cause, not the symptom. 4) Prove it: rerun the reproduction and the tests. Add a regression test when practical."""},
    "docs": {"label": "docs", "description": "technical writer: READMEs, guides, API docs", "tools": READ_TOOLS + ", write, edit, bash", "writes": "*.md",
             "prompt": """Mode: DOCS. Write clear, accurate documentation from the real code: short intro, quick start that actually works (verify commands), reference sections, examples. Plain words, no marketing."""},
    "research": {"label": "research", "description": "researcher: web search + sources, writes notes", "tools": "web_search, web_fetch, read, write, todo", "writes": "*.md",
                 "prompt": """Mode: RESEARCH. Search broadly, open primary sources, cross-check facts, note dates and versions. Answer with a concise synthesis and a list of sources; save longer findings to a .md file when asked."""},
}


def presets(store):
    out = {k: dict(v, name=k) for k, v in BUILTIN_PRESETS.items()}
    d = store.home / "agents"
    for p in sorted(d.glob("*.md")) if d.exists() else []:
        text = p.read_text("utf-8", "replace")
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
        fields.update(name=name, prompt=body.strip(), custom=True)
        out[name] = dict(base, **fields)
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
        rel = os.path.relpath(f, root)
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
    p = mem_dir(home) / f"{re.sub(r'[^\w-]', '', mid)}.json"
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
            p.write_text(old, "utf-8")


def file_diff(path, old):
    try:
        new = Path(path).read_text("utf-8", "replace")
    except OSError:
        new = ""
    d = list(difflib.unified_diff((old or "").splitlines(), new.splitlines(), "a/" + path, "b/" + path, n=2, lineterm=""))
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
                                         stderr=subprocess.DEVNULL, env=env, text=True, encoding="utf-8", bufsize=1)
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
cwd: {cwd} | os: {os} | date: {date}

Work:
- Read the relevant code before changing it. Ask only when truly ambiguous.
- Small steps; for 3+ steps keep a todo list current.
- Build what is needed now, open for later: clear modules, small functions, no hacks. Match the project's style; no needless deps.
- Before saying done, verify: run tests, build, lint or the code. Never claim untested success.
- Prefer edit over rewrite; paths relative to cwd. Batch independent reads.
- web_search for current facts, web_fetch for docs/URLs.
- Confirm before destructive or irreversible actions.
UI work: no emoji; inline SVG icons, one stroke style; neutral + one accent palette, spacing scale, light+dark; empty/loading/error states; responsive.
Reply: concise. Say what changed and how you verified it.{extra}{skills}{project}{user}"""



class LimitHit(Exception):
    pass


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

    def system_prompt(self):
        sks = self.skills.enabled()
        skills = ("\nSkills (load via skill tool):\n" + "\n".join(f"- {k}: {v['description']}" for k, v in sks.items())) if sks else ""
        project, budget = "", 24000
        for name in self.context_files():
            text = (Path(self.session["cwd"]) / name).read_text("utf-8", "replace")[:min(10000, budget)]
            budget -= len(text)
            project += f"\n\n# {name}\n{text}"
        if "AGENTS.md" in self.context_files():
            project += "\n\nAGENTS.md is this project's memory: update it when you learn something durable (commands, structure, conventions, decisions)."
        else:
            project += ("\n\nNo AGENTS.md here yet. Once you understand the project, create a concise AGENTS.md "
                        "(purpose, stack, run/test commands, structure, conventions) — it is loaded into every future session.")
        pr = self.preset()
        if pr.get("prompt"):
            project += "\n\n" + (pr["prompt"].replace("{home}", str(self.s.home)) if pr.get("name") == "me" else pr["prompt"])
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
        return SYSTEM.format(cwd=self.session["cwd"], os=f"{platform.system()} {platform.release()}", date=time.strftime("%Y-%m-%d"),
                             extra=extra, skills=skills, project=project, user=user)

    def preset(self):
        return preset(self.s, self.session.get("agent") or self.s.config.get("agent"))

    def context_files(self):
        cwd = Path(self.session.get("cwd") or ".")
        return [n for n in CONTEXT_FILES if (cwd / n).is_file()]

    def tool_schemas(self):
        allowed = preset_tools(self.preset())
        if self.depth:
            allowed.discard("task")
        out = [t for t in TOOLS if t["name"] in allowed]
        if self.s.auth.get("token"):
            out.append(NOTIFY_TOOL)
        if self.s.config.get("mcp"):
            out.append(MCP_TOOL)
        return out + PLUGINS.tool_schemas()

    def run(self, text, images=()):
        """Blocking. Runs one user turn to completion."""
        pr = self.preset()
        m = self.s.model(pr.get("model") or None)
        if m and pr.get("effort"):
            m = dict(m, effort=pr["effort"])
        if not m:
            self.emit({"type": "error", "text": "no model configured — use /provider then /models"})
            return
        self.busy = True
        self.cancel.clear()
        self.snap = {}
        start_idx = len(self.session["messages"])
        content = text if not images else [{"type": "text", "text": text}] + [{"type": "image_url", "image_url": {"url": u}} for u in images]
        self.session["messages"].append({"role": "user", "content": content})
        if not self.session["title"]:
            self.session["title"] = text.strip().split("\n")[0][:60]
        t0, tin, tout, cost = time.time(), 0, 0, 0.0
        wrote, ran, nudged = False, False, False
        self.emit({"type": "turn_start", "agent": pr["name"], "context": self.context_files()})
        prov = Provider(m, self.s.provider(m))
        st = self.s.config["settings"]
        try:
            while True:
                limit = self.compact_threshold(m)
                if limit and self.ctx_used > limit:
                    self.compact(reason="auto")
                msg, usage = prov.stream(self.system_prompt(), self.session["messages"], self.tool_schemas(), self.emit, self.cancel,
                                         cache_key=self.session["id"])
                c = usage_cost(m, usage)
                tin += usage["in"]
                tout += usage["out"]
                cost += c
                self.ctx_used = usage["in"] + usage["out"] or self.ctx_used
                self.session["ctx"] = self.ctx_used
                u = self.session["usage"]
                u["in"] = u.get("in", 0) + usage["in"]
                u["out"] = u.get("out", 0) + usage["out"]
                u["cached"] = u.get("cached", 0) + usage.get("cached", 0)
                u["cost"] = round(u.get("cost", 0) + c, 6)
                today = add_day_cost(self.s.home, c) if c and self.persist else day_cost(self.s.home)
                self.emit({"type": "usage", "ctx": self.ctx_used, "in": tin, "out": tout, "cached": usage.get("cached", 0),
                           "cost": cost, "session_cost": u["cost"], "day_cost": today})
                self.session["messages"].append(msg)
                self.checkpoint()
                if not msg.get("tool_calls"):
                    if wrote and not ran and not nudged and "bash" in {t["name"] for t in self.tool_schemas()} and not st.get("no_verify"):
                        nudged = True  # self-check before "done": changes were made but nothing was run since
                        self.emit({"type": "verify"})
                        self.session["messages"].append({"role": "user", "content": VERIFY_NUDGE})
                        continue
                    break
                self._limits(cost, st)
                results = self._run_calls(msg["tool_calls"], pr)
                imgs = []
                for tc, (ok, out, img) in results:
                    name = tc["function"]["name"]
                    if ok and name in ("write", "edit"):
                        wrote, ran = True, False
                    elif name == "bash" or name == "task":
                        ran = ran or wrote
                    if img:
                        imgs.append(img)
                if self.cancel.is_set():
                    raise Cancelled()
                if imgs and m.get("vision", True):
                    self.session["messages"].append({"role": "user", "content": [{"type": "text", "text": "(images from read)"}] +
                                                     [{"type": "image_url", "image_url": {"url": u}} for u in imgs]})
        except Cancelled:
            self._fix_dangling()
            self.session["messages"].append({"role": "user", "content": "[interrupted by user]"})
            self.emit({"type": "error", "text": "interrupted"})
        except LimitHit as e:
            self._fix_dangling()
            self.emit({"type": "error", "text": str(e)})
        except Exception as e:
            self._fix_dangling()
            self.emit({"type": "error", "text": str(e)})
        finally:
            self.busy = False
            self._finish_checkpoint(start_idx)
            if pr.get("name") == "me" and self.persist:
                self.s.save()  # new skills/themes/agents written to the home folder reach every device
            if self.persist:
                self.s.save_session(self.session)
            self.emit({"type": "turn_end", "time": time.time() - t0, "in": tin, "out": tout, "ctx": self.ctx_used, "cost": cost,
                       "session_cost": self.session["usage"].get("cost", 0)})

    def _limits(self, cost, st):
        mt, md = float(st.get("max_turn_cost") or 0), float(st.get("max_day_cost") or 0)
        if mt and cost >= mt:
            raise LimitHit(f"stopped: this task reached the {money(mt)} per-task limit (/config to change)")
        if md and day_cost(self.s.home) >= md:
            raise LimitHit(f"stopped: today's spending reached the {money(md)} daily limit (/config to change)")

    def _run_calls(self, calls, pr):
        """Independent read-only calls run in parallel; anything that changes state runs in order."""
        prepared = []
        for tc in calls:
            name = tc["function"]["name"]
            try:
                args = json.loads(tc["function"]["arguments"] or "{}")
                if not isinstance(args, dict):
                    raise ValueError
            except ValueError:
                args = None
            tid = tc["id"]
            ev = {"type": "tool_start", "id": tid, "name": name, "args": args or {}, "label": self.tools.preview(name, args or {})}
            if name == "skill" and args:
                sk = self.skills.all().get(args.get("name"), {})
                ev["skill"] = {"color": sk.get("color", ""), "icon": sk.get("icon", "")}
            prepared.append((tc, name, args, tid, ev))
        results = {}
        par = len(prepared) > 1 and all(n in READONLY_TOOLS and a is not None for _, n, a, _, _ in prepared)
        if par:
            for p in prepared:
                self.emit(p[4])
            ths = []
            for tc, name, args, tid, _ in prepared:
                def work(tc=tc, name=name, args=args, tid=tid):
                    t1 = time.time()
                    results[tid] = self._exec(name, args, tid, pr) + (time.time() - t1,)
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
        if args is None:
            return False, "error: arguments were not valid JSON — send a JSON object", None
        if name not in {t["name"] for t in self.tool_schemas()}:
            return False, f"error: tool {name} is not available in the {pr['name']} agent", None
        if name in ("write", "edit") and not may_write(pr, os.path.relpath(os.path.abspath(args.get("path", "")), self.session["cwd"])):
            return False, f"error: the {pr['name']} agent may only write {pr.get('writes') or 'nothing'}", None
        veto = PLUGINS.emit("before_tool", name, args)
        if isinstance(veto, str):
            return False, veto, None
        if name == "todo":
            return self._todo(args)
        if name == "memory":
            return self._memory(args)
        if name == "task":
            return self._task(args, temit)
        need = self.tools.needs_confirm(name, args, self.session.get("cwd"))
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
            self._snapshot(args.get("path", ""))
        try:
            return self.tools.run(name, args, temit, self.cancel, self.ask)
        except Cancelled:
            raise
        except Exception as e:
            return False, f"error: {type(e).__name__}: {e}", None

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
        return True, f"todo: {n}/{len(items)} done" + (f"; next: {nxt}" if nxt else "; all done — verify, then report"), None

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
        p = os.path.abspath(os.path.expanduser(path))
        if p not in self.snap:
            try:
                self.snap[p] = Path(p).read_text("utf-8") if os.path.isfile(p) else None
            except (OSError, UnicodeDecodeError):
                pass

    def _finish_checkpoint(self, start_idx):
        if not self.snap or not self.persist:
            return
        files = dict(self.snap)
        Checkpoints(self.s, self.session["id"]).push({"msgs": start_idx, "files": files, "t": time.time()})
        diffs = [{"path": os.path.relpath(p, self.session.get("cwd") or "."), "diff": file_diff(p, old), "new": old is None}
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
        if 0 <= cp["msgs"] <= len(self.session["messages"]):
            self.session["messages"] = self.session["messages"][:cp["msgs"]]
        self.s.save_session(self.session)
        names = [os.path.relpath(p, self.session.get("cwd") or ".") for p in cp["files"]]
        ev = {"type": "undone", "files": names, "text": f"restored {len(names)} file{'s' if len(names) != 1 else ''}: " + ", ".join(names[:8])}
        self.emit(ev)
        return dict(ev, ok=True)

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

    def compact(self, keep_turns=2, reason="manual"):
        """Replace older history with a model-written summary; keep the last `keep_turns` user turns verbatim."""
        msgs = self.session["messages"]
        starts = [i for i, m in enumerate(msgs) if m["role"] == "user" and isinstance(m.get("content"), str)
                  and not m["content"].startswith(("[interrupted", "[Summary"))]
        if len(starts) <= keep_turns:
            return False
        cut = starts[-keep_turns]
        lines = []
        for m in msgs[:cut]:
            c = m.get("content")
            if isinstance(c, list):
                c = " ".join(x.get("text", "") for x in c if x.get("type") == "text")
            if m["role"] == "tool":
                lines.append(f"[tool result] {str(c)[:600]}")
            else:
                if c:
                    lines.append(f"[{m['role']}] {str(c)[:4000]}")
                for tc in m.get("tool_calls") or []:
                    lines.append(f"[tool call] {tc['function']['name']} {tc['function']['arguments'][:300]}")
        m = self.s.model()
        self.emit({"type": "compacting", "reason": reason})
        msg, usage = Provider(m, self.s.provider(m)).stream(self.COMPACT_PROMPT, [{"role": "user", "content": "\n".join(lines)[-400000:]}],
                                                            None, lambda e: None, self.cancel)
        summary = (msg.get("content") or "").strip()
        if not summary:
            return False
        before = len(msgs)
        self.session["messages"] = [{"role": "user", "content": "[Summary of the earlier conversation]\n" + summary},
                                    {"role": "assistant", "content": "Understood — continuing from this summary."}] + msgs[cut:]
        self.session["compactions"] = self.session.get("compactions", 0) + 1
        self.ctx_used = self.session["ctx"] = len(json.dumps(self.session["messages"], ensure_ascii=False)) // 4
        self.emit({"type": "compacted", "before": before, "after": len(self.session["messages"]), "ctx": self.ctx_used,
                   "reason": reason, "summary": summary[:2000]})
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
            if isinstance(v, str):
                for r in sorted(roots, key=len, reverse=True):
                    v = v.replace(r, cwd)
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
        elif t == "error":
            sys.stderr.write(f"\nerror: {e['text']}\n")
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
        elif t == "undo":
            r = ag.undo()
            if not r["ok"]:
                emit({"type": "error", "text": r["text"]})
        elif t == "cancel":
            ag.cancel.set()
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
            ag.session = new_session()
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

WEBUI = "<!doctype html>\n<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\">\n<meta name=\"viewport\" content=\"width=device-width,initial-scale=1,viewport-fit=cover\">\n<meta name=\"theme-color\" content=\"#0b0c10\">\n<title>me</title>\n<link rel=\"manifest\" href=\"/manifest.webmanifest\">\n<meta name=\"apple-mobile-web-app-capable\" content=\"yes\">\n<link rel=\"icon\" href=\"data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Crect width='32' height='32' rx='8' fill='%230b0c10'/%3E%3Crect x='9' y='9' width='14' height='14' rx='3.5' fill='%238b7cff'/%3E%3C/svg%3E\">\n<style>:root{--on-a:#fff;--bg:#0b0c10;--bg2:#111319;--bg3:#171a23;--line:#1f2230;--fg:#d8dae3;--dim:#7b8094;--faint:#3a3d4a;--a:#8b7cff;--b:#4fd1c5;--ok:#5ad18a;--err:#ff6b7a;--warn:#ffb454;--user:#e7c66b;\n  --mono:ui-monospace,\"JetBrains Mono\",SFMono-Regular,Menlo,Consolas,monospace;--sans:Inter,system-ui,-apple-system,\"Segoe UI\",Roboto,sans-serif}\n:root[data-theme=\"light\"]{--bg:#fafaf8;--bg2:#f2f2ef;--bg3:#e9e9e4;--line:#e2e2dc;--fg:#1d1e24;--dim:#62657a;--faint:#c4c5cc}\n@media (prefers-color-scheme: light){:root:not([data-theme=\"dark\"]){--bg:#fafaf8;--bg2:#f2f2ef;--bg3:#e9e9e4;--line:#e2e2dc;--fg:#1d1e24;--dim:#62657a;--faint:#c4c5cc}}\n*{box-sizing:border-box;margin:0;padding:0}\nhtml,body{height:100%;background:var(--bg);color:var(--fg);font:14.5px/1.6 var(--sans);-webkit-font-smoothing:antialiased}\nbutton{font:inherit;color:inherit;background:none;border:none;cursor:pointer}\nsvg{width:16px;height:16px;fill:none;stroke:currentColor;stroke-width:1.8;stroke-linecap:round;stroke-linejoin:round;flex-shrink:0}\n.app{display:grid;grid-template-columns:260px 1fr;height:100%}\naside{border-right:1px solid var(--line);display:flex;flex-direction:column;min-height:0;background:var(--bg)}\n.brand{display:flex;align-items:center;gap:10px;padding:16px;font:700 17px var(--mono)}\n.brand i{width:11px;height:11px;border-radius:3px;background:linear-gradient(135deg,var(--a),var(--b))}\n.brand small{font:500 11px var(--mono);color:var(--dim);margin-left:auto;padding:2px 7px;border:1px solid var(--line);border-radius:6px}\n.newbtn{margin:0 12px 10px;display:flex;align-items:center;gap:8px;padding:9px 12px;border:1px solid var(--line);border-radius:9px;color:var(--fg)}\n.newbtn:hover{background:var(--bg2)}\n.slist{flex:1;overflow:auto;padding:0 8px 12px}\n.slist .lbl{font:11px var(--mono);color:var(--dim);padding:10px 8px 6px;text-transform:uppercase;letter-spacing:.5px}\n.si{display:block;width:100%;text-align:left;padding:8px 10px;border-radius:8px;color:var(--dim);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-size:13.5px}\n.si:hover{background:var(--bg2);color:var(--fg)}.si.on{background:var(--bg3);color:var(--fg)}\n.side-foot{border-top:1px solid var(--line);padding:10px 12px;display:flex;gap:6px;align-items:center;font-size:12.5px;color:var(--dim)}\n.side-foot a{color:inherit;text-decoration:none;display:flex;align-items:center;gap:6px}\nmain{display:flex;flex-direction:column;min-width:0;min-height:0}\nheader{display:flex;align-items:center;gap:10px;padding:10px 16px;border-bottom:1px solid var(--line);min-height:52px}\nheader .title{font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;flex:1}\n.ib.menu{display:none}\n.pill{display:flex;align-items:center;gap:6px;font:12px var(--mono);color:var(--dim);border:1px solid var(--line);border-radius:8px;padding:5px 9px;white-space:nowrap}\n.pill:hover{color:var(--fg)}\n#chat{flex:1;overflow:auto;scroll-behavior:smooth}\n.inner{max-width:820px;margin:0 auto;padding:22px 18px 30px}\n.empty{height:100%;display:grid;place-items:center;text-align:center;color:var(--dim)}\n.empty h2{font:700 34px var(--mono);letter-spacing:-1.5px;color:var(--fg);margin-bottom:6px}\n.empty h2 span{background:linear-gradient(90deg,var(--a),var(--b));-webkit-background-clip:text;background-clip:text;color:transparent}\n.msg{margin:18px 0}\n.msg.user{display:flex;justify-content:flex-end}\n.msg.user .bub{background:var(--bg3);border-radius:14px 14px 4px 14px;padding:10px 14px;max-width:85%;white-space:pre-wrap;word-break:break-word}\n.md{word-break:break-word}\n.md p{margin:8px 0}.md ul,.md ol{margin:8px 0 8px 22px}.md li{margin:3px 0}\n.md h1,.md h2,.md h3{font-size:16px;margin:16px 0 6px;color:var(--a)}\n.md code{font:12.5px var(--mono);background:var(--bg2);border:1px solid var(--line);padding:1px 5px;border-radius:5px;color:var(--b)}\n.md pre{background:var(--bg2);border:1px solid var(--line);border-radius:10px;margin:10px 0;overflow:hidden}\n.md pre .bar{display:flex;justify-content:space-between;font:11px var(--mono);color:var(--dim);padding:6px 12px;border-bottom:1px solid var(--line)}\n.md pre code{display:block;background:none;border:none;padding:12px;overflow-x:auto;color:var(--fg);white-space:pre}\n.md a{color:var(--a)}.md blockquote{border-left:3px solid var(--faint);padding-left:12px;color:var(--dim)}\n.md table{border-collapse:collapse;margin:8px 0;font-size:13.5px}.md td,.md th{border:1px solid var(--line);padding:5px 9px}\n.think{font-size:13px;color:var(--dim);margin:6px 0}\n.think summary{cursor:pointer;list-style:none;display:flex;align-items:center;gap:6px;font-style:italic}\n.think pre{white-space:pre-wrap;font:12.5px/1.55 var(--sans);font-style:italic;margin:6px 0 0 22px;max-height:240px;overflow:auto}\n.tool{border:1px solid var(--line);border-radius:10px;margin:8px 0;overflow:hidden;background:var(--bg)}\n.tool .h{display:flex;align-items:center;gap:8px;padding:8px 12px;font:12.5px var(--mono);cursor:pointer}\n.tool .h b{font-weight:600}.tool .h .lab{color:var(--dim);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1}\n.tool .h .t{color:var(--faint)}\n.tool .body{border-top:1px solid var(--line);font:12px/1.55 var(--mono);padding:8px 12px;max-height:320px;overflow:auto;white-space:pre-wrap;color:var(--dim)}\n.tool.closed .body,.tool .body:empty{display:none}\n.tool .st{font:12px var(--mono);color:var(--dim);padding:0 12px 8px}\n.tool .log{font:12px var(--mono);color:var(--dim);padding:0 12px 8px}\n.add{color:var(--ok)}.del{color:var(--err)}\n.pbar{height:6px;border-radius:4px;background:var(--line);margin:0 12px 10px;overflow:hidden}\n.pbar i{display:block;height:100%;background:var(--sk,var(--a));transition:width .2s}\n.pbar.ind i{width:30%!important;animation:ind 1.1s ease-in-out infinite}\n@keyframes ind{0%{margin-left:-30%}100%{margin-left:100%}}\n.skill{border-color:color-mix(in srgb,var(--sk) 45%,var(--line))}\n.skill .h b{color:var(--sk)}\n.spin{width:14px;height:14px;border:2px solid var(--line);border-top-color:var(--a);border-radius:50%;animation:sp .8s linear infinite;flex-shrink:0}\n@keyframes sp{to{transform:rotate(360deg)}}\n.stats{font:12px var(--mono);color:var(--dim);display:flex;align-items:center;gap:6px;margin:4px 0 22px}\n.stats svg{color:var(--ok)}\n.info{font:12.5px var(--mono);color:var(--dim);margin:8px 0}.info.err{color:var(--err)}.info.warn{color:var(--warn)}\n.confirm{border:1px solid var(--warn);border-radius:10px;padding:12px;margin:10px 0}\n.confirm pre{font:12px var(--mono);white-space:pre-wrap;color:var(--dim);margin:8px 0;max-height:200px;overflow:auto}\n.confirm .btns{display:flex;gap:8px}\n.btn{padding:7px 13px;border-radius:8px;font:500 12.5px var(--mono);background:var(--a);color:var(--on-a)}\n.btn.ghost{background:none;color:var(--dim);border:1px solid var(--line)}\n.working{display:flex;align-items:center;gap:8px;font:12.5px var(--mono);color:var(--dim);margin:10px 0}\n.shimmer{background:linear-gradient(90deg,var(--dim) 30%,var(--fg) 50%,var(--dim) 70%);background-size:200% 100%;-webkit-background-clip:text;background-clip:text;color:transparent;animation:sh 1.6s linear infinite}\n@keyframes sh{from{background-position:150% 0}to{background-position:-50% 0}}\n.composer{padding:0 18px 12px;padding-bottom:max(12px,env(safe-area-inset-bottom))}\n.box{max-width:820px;margin:0 auto;border:1px solid var(--line);border-radius:14px;background:var(--bg2);transition:border-color .15s}\n.box:focus-within{border-color:var(--a)}\ntextarea{width:100%;background:none;border:none;outline:none;resize:none;color:var(--fg);font:15px/1.5 var(--sans);padding:12px 14px 4px;max-height:220px}\n.row{display:flex;align-items:center;gap:6px;padding:6px 8px 8px}\n.ib{width:34px;height:34px;border-radius:9px;display:grid;place-items:center;color:var(--dim)}\n.ib:hover{background:var(--bg3);color:var(--fg)}\n.ib.rec{background:var(--err);color:#fff;animation:pulse 1s infinite}\n@keyframes pulse{50%{opacity:.6}}\n.send{background:var(--a);color:var(--on-a)}.send:hover{background:var(--a);filter:brightness(1.1);color:var(--on-a)}\n.send:disabled{background:var(--bg3);color:var(--dim);opacity:1}\n.send:disabled{opacity:.35;cursor:default}\n.spacer{flex:1}\n.status{max-width:820px;margin:8px auto 0;display:flex;gap:14px;align-items:center;font:11.5px var(--mono);color:var(--dim);flex-wrap:wrap}\n.ctx{display:flex;align-items:center;gap:6px}.ctx .bar{width:70px;height:5px;border-radius:3px;background:var(--line);overflow:hidden}.ctx .bar i{display:block;height:100%;background:var(--ok)}\n.popup{position:fixed;inset:0;background:rgba(0,0,0,.55);display:grid;place-items:center;padding:16px;z-index:20}\n.popup>div{background:var(--bg);border:1px solid var(--line);border-radius:14px;padding:8px;width:min(420px,100%);max-height:70vh;overflow:auto}\n.popup button{display:flex;width:100%;text-align:left;padding:9px 12px;border-radius:8px;gap:10px;align-items:center}\n.popup button:hover{background:var(--bg2)}.popup .d{color:var(--dim);font-size:12px;margin-left:auto}\n.hidden{display:none!important}\n@media (max-width:760px){.app{grid-template-columns:1fr}aside{position:fixed;inset:0 25% 0 0;z-index:15;transform:translateX(-102%);transition:transform .2s;box-shadow:0 0 60px rgba(0,0,0,.5)}\n  aside.open{transform:none}.ib.menu{display:grid}.inner{padding:16px 14px}.composer{padding:0 10px 10px}}\n.sw{width:30px;height:20px;border-radius:6px;border:1px solid;display:flex;gap:3px;align-items:center;justify-content:center;flex-shrink:0}\n.sw i{width:7px;height:7px;border-radius:50%}\n.popup .more{display:block;padding:10px 12px;font-size:12.5px;color:var(--a);text-decoration:none}\n.panel{padding:4px 12px 10px;font-size:13px;overflow-x:auto}\n.panel table{border-collapse:collapse;font:12px var(--mono);margin-top:6px}\n.panel td,.panel th{border:1px solid var(--line);padding:4px 8px;text-align:left;white-space:nowrap}\n.panel th{color:var(--dim);font-weight:500}\n.tool.closed .panel{display:block}\n.askin{flex:1;min-width:160px;background:var(--bg2);border:1px solid var(--line);border-radius:8px;color:var(--fg);padding:7px 10px;font:inherit;outline:none}\n.pill.mode{color:var(--a);border-color:color-mix(in srgb,var(--a) 40%,var(--line))}\n.cf{color:var(--faint)}.cf.on{color:var(--ok)}\n.popup button b{font-weight:600;white-space:nowrap}.popup .d{text-align:right}\n.row .pill{min-width:0}\n#modelBtn{flex:0 1 auto;overflow:hidden}#modelBtn span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}\n#agentBtn,.ib{flex-shrink:0}\n@media (max-width:760px){.status{gap:8px;font-size:10.5px}.ctx .bar{width:44px}header{padding:8px 10px}}\n.scrim{display:none}\n.side-close{display:none}\n@media (max-width:760px){\n  .scrim{display:block;position:fixed;inset:0;background:rgba(0,0,0,.5);z-index:14;opacity:0;pointer-events:none;transition:opacity .2s}\n  aside.open+.scrim,.scrim.on{opacity:1;pointer-events:auto}\n  .side-close{display:grid;margin-left:6px}\n  .brand small{margin-left:auto}\n  .status .tok{display:none}\n}\n.tool .h .lab{direction:ltr}\n.todo-card{margin:0 12px 10px;border:1px solid var(--line);border-radius:10px;padding:10px 12px;font-size:12.5px;max-height:38vh;overflow:auto}\n.tc-h{display:flex;justify-content:space-between;align-items:center;font:12px var(--mono);color:var(--dim)}.tc-h b{color:var(--fg);font-weight:600}\n.tc-bar{height:4px;border-radius:3px;background:var(--line);margin:8px 0;overflow:hidden}.tc-bar i{display:block;height:100%;background:var(--ok)}\n.tc-i{display:flex;gap:8px;align-items:flex-start;padding:3px 0;line-height:1.4}.tc-i.done{color:var(--dim);text-decoration:line-through;text-decoration-color:var(--faint)}\n.tc-i.doing{color:var(--fg);font-weight:500}\n.tc-b{width:15px;height:15px;border:1.5px solid var(--faint);border-radius:4px;flex-shrink:0;display:grid;place-items:center;margin-top:1px}\n.tc-i.done .tc-b{border-color:var(--ok);color:var(--ok)}.tc-i.doing .tc-b{border:none}.tc-b svg{width:11px;height:11px;stroke-width:2.6}.tc-b .spin{width:13px;height:13px}\n.goal{display:flex;gap:10px;align-items:flex-start;border:1px solid var(--line);border-radius:10px;padding:10px 12px;margin:10px 0;font-size:13.5px}\n.goal.working{border-color:color-mix(in srgb,var(--a) 50%,var(--line))}.goal.done{border-color:color-mix(in srgb,var(--ok) 55%,var(--line))}.goal.blocked{border-color:color-mix(in srgb,var(--err) 55%,var(--line))}\n.review{border:1px solid var(--line);border-radius:10px;margin:10px 0;overflow:hidden}\n.rv-h{display:flex;justify-content:space-between;padding:9px 12px;font-size:13px;border-bottom:1px solid var(--line)}.rv-h .d{color:var(--dim);font:12px var(--mono)}\n.review details{border-bottom:1px solid var(--line)}.review summary{display:flex;gap:8px;align-items:center;padding:7px 12px;font:12.5px var(--mono);cursor:pointer;list-style:none}\n.review summary .rv-p{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}\n.review pre{font:12px/1.5 var(--mono);padding:8px 12px;max-height:320px;overflow:auto;white-space:pre;color:var(--dim);background:var(--bg2)}\n.review .btns{display:flex;gap:8px;padding:10px 12px;font:12px var(--mono)}.btn.danger{color:var(--err);border-color:color-mix(in srgb,var(--err) 45%,var(--line))}\n.tag{font:10.5px var(--mono);color:var(--b);border:1px solid var(--line);border-radius:5px;padding:0 5px}\n.dim{color:var(--dim)}.err{color:var(--err)}\n.perm{font:11.5px var(--mono);color:var(--dim);border:1px solid var(--line);border-radius:6px;padding:1px 7px}\n.perm.full{color:var(--warn);border-color:color-mix(in srgb,var(--warn) 45%,var(--line))}.perm.read{color:var(--b)}\n.atts{display:flex;gap:6px;flex-wrap:wrap;padding:10px 10px 0}\n.att{display:flex;align-items:center;gap:6px;border:1px solid var(--line);border-radius:8px;padding:3px 4px 3px 4px;font:12px var(--mono);max-width:100%;background:var(--bg)}\n.att img{width:28px;height:28px;object-fit:cover;border-radius:5px}.att .an{max-width:140px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}\n.att button{width:22px;height:22px;display:grid;place-items:center;color:var(--dim);border-radius:6px}.att button:hover{background:var(--bg3)}\nheader .pill span{max-width:90px;overflow:hidden;text-overflow:ellipsis}\n@media (max-width:760px){#previewBtn span{display:none}.status .cost{order:5}}\n@media (max-width:420px){#wsPill span{max-width:70px;overflow:hidden;text-overflow:ellipsis}}\n.side-set{display:flex;align-items:center;gap:8px;padding:8px 14px;border-top:1px solid var(--line);font-size:12.5px;color:var(--dim)}\n.lang{position:relative;display:grid;grid-template-columns:1fr 1fr;border:1px solid var(--line);border-radius:999px;background:var(--bg2);padding:2px;font:600 11.5px var(--mono)}\n.lang b{position:absolute;top:2px;bottom:2px;left:2px;width:calc(50% - 2px);border-radius:999px;background:var(--a);transition:transform .2s ease}\n.lang.ru b{transform:translateX(100%)}\n.lang button{position:relative;z-index:1;min-width:38px;padding:3px 10px;border-radius:999px;color:var(--dim);transition:color .2s}\n.lang button[aria-pressed=\"true\"]{color:var(--on-a)}\n.side-set+.side-foot{border-top:none;padding-top:4px}\n.btn,.perm{max-width:100%}\n.confirm .btns,.review .btns{flex-wrap:wrap}\n.review .rv-h{gap:8px}\n.si,.goal span,.info{overflow-wrap:anywhere}\n.popup .d{max-width:55%}\n.popup button b{white-space:normal}\n.status{row-gap:4px}\n@media (max-width:420px){header .title{min-width:0}.pill{padding:5px 7px}}\n</style>\n</head>\n<body>\n<div class=\"app\">\n<aside id=\"side\">\n  <div class=\"brand\"><i></i>me<small id=\"mode\"></small><button class=\"ib side-close\" onclick=\"closeSide()\" aria-label=\"close menu\" data-i18n-aria=\"close menu\"><svg viewBox=\"0 0 24 24\"><path d=\"M6 6l12 12M18 6L6 18\"/></svg></button></div>\n  <button class=\"newbtn\" onclick=\"newChat()\"><svg viewBox=\"0 0 24 24\"><path d=\"M12 5v14M5 12h14\"/></svg><span data-i18n=\"New chat\">New chat</span></button>\n  <div class=\"todo-card hidden\" id=\"todoCard\"></div>\n  <div class=\"slist\" id=\"slist\"></div>\n  <div class=\"side-set\"><span data-i18n=\"Language\">Language</span><span class=\"spacer\"></span><div class=\"lang\" role=\"group\" aria-label=\"Language\" data-i18n-aria=\"Language\"><b></b><button type=\"button\" data-l=\"en\" aria-pressed=\"true\">EN</button><button type=\"button\" data-l=\"ru\" aria-pressed=\"false\">RU</button></div></div>\n  <div class=\"side-foot\"><a id=\"acct\" href=\"/account\"><svg viewBox=\"0 0 24 24\"><circle cx=\"12\" cy=\"8\" r=\"4\"/><path d=\"M4 21c0-4 4-6 8-6s8 2 8 6\"/></svg><span id=\"who\">account</span></a>\n    <span class=\"spacer\"></span><button class=\"ib hidden\" id=\"bell\" onclick=\"enablePush()\" title=\"notify me when a task is done\" data-i18n-title=\"notify me when a task is done\"><svg viewBox=\"0 0 24 24\"><path d=\"M6 16V11a6 6 0 0 1 12 0v5l1.5 2h-15z\"/><path d=\"M10 20.5a2 2 0 0 0 4 0\"/></svg></button><button class=\"ib\" onclick=\"pickTheme()\" title=\"theme\" data-i18n-title=\"theme\" aria-label=\"theme\" data-i18n-aria=\"theme\"><svg viewBox=\"0 0 24 24\"><circle cx=\"12\" cy=\"12\" r=\"9\"/><path d=\"M12 3a9 9 0 0 0 0 18z\" fill=\"currentColor\"/></svg></button></div>\n</aside>\n<div class=\"scrim\" id=\"scrim\" onclick=\"closeSide()\"></div>\n<main>\n  <header>\n    <button class=\"ib menu\" onclick=\"openSide()\" aria-label=\"menu\" data-i18n-aria=\"menu\" title=\"menu\" data-i18n-title=\"menu\"><svg viewBox=\"0 0 24 24\"><path d=\"M4 7h16M4 12h16M4 17h16\"/></svg></button>\n    <div class=\"title\" id=\"title\">New chat</div>\n    <button class=\"pill hidden\" id=\"todoChip\" onclick=\"openSide()\" title=\"checklist\" data-i18n-title=\"checklist\"><svg viewBox=\"0 0 24 24\"><path d=\"M4 7l2 2 4-4M4 17l2 2 4-4M13 7h7M13 17h7\"/></svg><span></span></button>\n    <a class=\"pill hidden\" id=\"previewBtn\" target=\"_blank\" rel=\"noopener\" title=\"open the site the agent built\" data-i18n-title=\"open the site the agent built\"><svg viewBox=\"0 0 24 24\"><path d=\"M14 4h6v6M20 4l-9 9M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5\"/></svg><span data-i18n=\"preview\">preview</span></a>\n    <button class=\"pill\" id=\"wsPill\" title=\"workspace\" data-i18n-title=\"workspace\"><svg viewBox=\"0 0 24 24\"><path d=\"M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z\"/></svg><span id=\"ws\">\u2014</span></button>\n  </header>\n  <div id=\"chat\"><div class=\"inner\" id=\"log\"></div></div>\n  <div class=\"composer\">\n    <div class=\"box\">\n      <div class=\"atts hidden\" id=\"atts\"></div>\n      <textarea id=\"input\" rows=\"1\" placeholder=\"Ask anything\u2026\" data-i18n-ph=\"Ask anything\u2026\"></textarea>\n      <div class=\"row\">\n        <button class=\"pill\" id=\"modelBtn\" onclick=\"pickModel()\"><svg viewBox=\"0 0 24 24\"><circle cx=\"12\" cy=\"12\" r=\"3\"/><path d=\"M12 2v3M12 19v3M2 12h3M19 12h3M5 5l2 2M17 17l2 2M5 19l2-2M17 7l2-2\"/></svg><span id=\"model\">model</span></button>\n        <button class=\"pill mode\" id=\"agentBtn\" onclick=\"pickAgent()\"><svg viewBox=\"0 0 24 24\"><path d=\"M4 7h10M4 12h16M4 17h7\"/><circle cx=\"18\" cy=\"7\" r=\"2\"/><circle cx=\"15\" cy=\"17\" r=\"2\"/></svg><span id=\"agentName\">code</span></button>\n        <span class=\"spacer\"></span>\n        <input type=\"file\" id=\"file\" multiple hidden>\n        <button class=\"ib\" id=\"attach\" title=\"attach images or files\" data-i18n-title=\"attach images or files\" aria-label=\"attach images or files\" data-i18n-aria=\"attach images or files\" onclick=\"$('file').click()\"><svg viewBox=\"0 0 24 24\"><path d=\"M20 11.5l-8.4 8.4a5 5 0 0 1-7-7l8.5-8.5a3.3 3.3 0 0 1 4.7 4.7l-8.5 8.5a1.7 1.7 0 0 1-2.4-2.4l7.8-7.8\"/></svg></button>\n        <button class=\"ib\" id=\"mic\" title=\"hold to talk\" data-i18n-title=\"hold to talk\" aria-label=\"hold to talk\" data-i18n-aria=\"hold to talk\"><svg viewBox=\"0 0 24 24\"><rect x=\"9\" y=\"3\" width=\"6\" height=\"11\" rx=\"3\"/><path d=\"M5 11a7 7 0 0 0 14 0M12 18v3\"/></svg></button>\n        <button class=\"ib send\" id=\"send\" onclick=\"sendOrStop()\" disabled aria-label=\"send\" data-i18n-aria=\"send\"><svg viewBox=\"0 0 24 24\" id=\"sendIcon\"><path d=\"M12 19V5M5 12l7-7 7 7\"/></svg></button>\n      </div>\n    </div>\n    <div class=\"status\"><button class=\"perm\" id=\"perm\" onclick=\"pickPerm()\" title=\"permissions\" data-i18n-title=\"permissions\"></button><span id=\"ctxfiles\"></span><span class=\"ctx\" id=\"ctx\"></span><span id=\"cost\" class=\"cost\"></span><span id=\"tok\" class=\"tok\"></span><span id=\"sync\"></span></div>\n  </div>\n</main>\n</div>\n<div class=\"popup hidden\" id=\"popup\" onclick=\"if(event.target===this)this.classList.add('hidden')\"><div id=\"pbody\"></div></div>\n<script>// \u2500\u2500\u2500 i18n: English is the source text; RU is a lookup table. t(\"English\") / tt(\"English\") (retranslatable span) \u2500\u2500\u2500\nconst RU = {\n  \"New chat\": \"\u041d\u043e\u0432\u044b\u0439 \u0447\u0430\u0442\", \"Chat\": \"\u0427\u0430\u0442\", \"close menu\": \"\u0437\u0430\u043a\u0440\u044b\u0442\u044c \u043c\u0435\u043d\u044e\", \"account\": \"\u0430\u043a\u043a\u0430\u0443\u043d\u0442\", \"local\": \"\u043b\u043e\u043a\u0430\u043b\u044c\u043d\u043e\", \"cloud\": \"\u043e\u0431\u043b\u0430\u043a\u043e\",\n  \"notify me when a task is done\": \"\u0443\u0432\u0435\u0434\u043e\u043c\u043b\u044f\u0442\u044c \u043e \u0437\u0430\u0432\u0435\u0440\u0448\u0435\u043d\u0438\u0438 \u0437\u0430\u0434\u0430\u0447\u0438\", \"theme\": \"\u0442\u0435\u043c\u0430\", \"Language\": \"\u042f\u0437\u044b\u043a\", \"checklist\": \"\u0447\u0435\u043a-\u043b\u0438\u0441\u0442\",\n  \"preview\": \"\u043f\u0440\u043e\u0441\u043c\u043e\u0442\u0440\", \"open the site the agent built\": \"\u043e\u0442\u043a\u0440\u044b\u0442\u044c \u0441\u0430\u0439\u0442, \u0441\u043e\u0437\u0434\u0430\u043d\u043d\u044b\u0439 \u0430\u0433\u0435\u043d\u0442\u043e\u043c\", \"workspace\": \"\u0440\u0430\u0431\u043e\u0447\u0430\u044f \u043e\u0431\u043b\u0430\u0441\u0442\u044c\",\n  \"Ask anything\u2026\": \"\u0421\u043f\u0440\u043e\u0441\u0438\u0442\u0435 \u0447\u0442\u043e \u0443\u0433\u043e\u0434\u043d\u043e\u2026\", \"model\": \"\u043c\u043e\u0434\u0435\u043b\u044c\", \"no model\": \"\u043d\u0435\u0442 \u043c\u043e\u0434\u0435\u043b\u0438\", \"code\": \"\u043a\u043e\u0434\", \"attach images or files\": \"\u043f\u0440\u0438\u043a\u0440\u0435\u043f\u0438\u0442\u044c \u0438\u0437\u043e\u0431\u0440\u0430\u0436\u0435\u043d\u0438\u044f \u0438\u043b\u0438 \u0444\u0430\u0439\u043b\u044b\",\n  \"hold to talk\": \"\u0443\u0434\u0435\u0440\u0436\u0438\u0432\u0430\u0439\u0442\u0435, \u0447\u0442\u043e\u0431\u044b \u0433\u043e\u0432\u043e\u0440\u0438\u0442\u044c\", \"permissions\": \"\u0440\u0430\u0437\u0440\u0435\u0448\u0435\u043d\u0438\u044f\", \"menu\": \"\u043c\u0435\u043d\u044e\", \"send\": \"\u043e\u0442\u043f\u0440\u0430\u0432\u0438\u0442\u044c\", \"stop\": \"\u0441\u0442\u043e\u043f\",\n  \"what are we\": \"\u0447\u0442\u043e \u0431\u0443\u0434\u0435\u043c\", \"building?\": \"\u0441\u0442\u0440\u043e\u0438\u0442\u044c?\",\n  \"Runs in your cloud sandbox \u00b7 files sync to all your machines\": \"\u0420\u0430\u0431\u043e\u0442\u0430\u0435\u0442 \u0432 \u043e\u0431\u043b\u0430\u0447\u043d\u043e\u0439 \u043f\u0435\u0441\u043e\u0447\u043d\u0438\u0446\u0435 \u00b7 \u0444\u0430\u0439\u043b\u044b \u0441\u0438\u043d\u0445\u0440\u043e\u043d\u0438\u0437\u0438\u0440\u0443\u044e\u0442\u0441\u044f \u043c\u0435\u0436\u0434\u0443 \u0432\u0430\u0448\u0438\u043c\u0438 \u0443\u0441\u0442\u0440\u043e\u0439\u0441\u0442\u0432\u0430\u043c\u0438\",\n  \"Running locally on this machine\": \"\u0420\u0430\u0431\u043e\u0442\u0430\u0435\u0442 \u043b\u043e\u043a\u0430\u043b\u044c\u043d\u043e \u043d\u0430 \u044d\u0442\u043e\u043c \u043a\u043e\u043c\u043f\u044c\u044e\u0442\u0435\u0440\u0435\",\n  \"Today\": \"\u0421\u0435\u0433\u043e\u0434\u043d\u044f\", \"This week\": \"\u041d\u0430 \u044d\u0442\u043e\u0439 \u043d\u0435\u0434\u0435\u043b\u0435\", \"Older\": \"\u0420\u0430\u043d\u044c\u0448\u0435\", \"no chats yet\": \"\u0447\u0430\u0442\u043e\u0432 \u043f\u043e\u043a\u0430 \u043d\u0435\u0442\", \"(untitled)\": \"(\u0431\u0435\u0437 \u043d\u0430\u0437\u0432\u0430\u043d\u0438\u044f)\",\n  \"Checklist\": \"\u0427\u0435\u043a-\u043b\u0438\u0441\u0442\", \"Goal\": \"\u0426\u0435\u043b\u044c\", \"Goal done\": \"\u0426\u0435\u043b\u044c \u0434\u043e\u0441\u0442\u0438\u0433\u043d\u0443\u0442\u0430\", \"Goal blocked\": \"\u0426\u0435\u043b\u044c \u0437\u0430\u0431\u043b\u043e\u043a\u0438\u0440\u043e\u0432\u0430\u043d\u0430\",\n  \"thinking\u2026\": \"\u0434\u0443\u043c\u0430\u044e\u2026\", \"thought for {d}\": \"\u0434\u0443\u043c\u0430\u043b {d}\", \"working\u2026\": \"\u0440\u0430\u0431\u043e\u0442\u0430\u044e\u2026\", \"copy\": \"\u043a\u043e\u043f\u0438\u0440\u043e\u0432\u0430\u0442\u044c\",\n  \"Allow {name}?\": \"\u0420\u0430\u0437\u0440\u0435\u0448\u0438\u0442\u044c {name}?\", \"allow\": \"\u0440\u0430\u0437\u0440\u0435\u0448\u0438\u0442\u044c\", \"always allow\": \"\u0432\u0441\u0435\u0433\u0434\u0430 \u0440\u0430\u0437\u0440\u0435\u0448\u0430\u0442\u044c\", \"allow all (full)\": \"\u0440\u0430\u0437\u0440\u0435\u0448\u0438\u0442\u044c \u0432\u0441\u0451 (\u043f\u043e\u043b\u043d\u044b\u0439 \u0434\u043e\u0441\u0442\u0443\u043f)\", \"deny\": \"\u043e\u0442\u043a\u043b\u043e\u043d\u0438\u0442\u044c\",\n  \"answer\u2026\": \"\u043e\u0442\u0432\u0435\u0442\u2026\", \"interrupted\": \"\u043f\u0440\u0435\u0440\u0432\u0430\u043d\u043e\",\n  \"compacting history\u2026\": \"\u0441\u0436\u0438\u043c\u0430\u044e \u0438\u0441\u0442\u043e\u0440\u0438\u044e\u2026\", \"compacted {a} \u2192 {b} messages \u00b7 ~{c} tokens\": \"\u0438\u0441\u0442\u043e\u0440\u0438\u044f \u0441\u0436\u0430\u0442\u0430: {a} \u2192 {b} \u0441\u043e\u043e\u0431\u0449\u0435\u043d\u0438\u0439 \u00b7 ~{c} \u0442\u043e\u043a\u0435\u043d\u043e\u0432\",\n  \"self-check: running checks before finishing\": \"\u0441\u0430\u043c\u043e\u043f\u0440\u043e\u0432\u0435\u0440\u043a\u0430: \u0437\u0430\u043f\u0443\u0441\u043a\u0430\u044e \u043f\u0440\u043e\u0432\u0435\u0440\u043a\u0438 \u043f\u0435\u0440\u0435\u0434 \u0437\u0430\u0432\u0435\u0440\u0448\u0435\u043d\u0438\u0435\u043c\",\n  \"{a} \u00b7 \u2191 {i} \u2193 {o} \u00b7 {n} tokens{c}\": \"{a} \u00b7 \u2191 {i} \u2193 {o} \u00b7 {n} \u0442\u043e\u043a\u0435\u043d\u043e\u0432{c}\",\n  \"sandbox restarted \u2014 your session and files are safe\": \"\u043f\u0435\u0441\u043e\u0447\u043d\u0438\u0446\u0430 \u043f\u0435\u0440\u0435\u0437\u0430\u043f\u0443\u0449\u0435\u043d\u0430 \u2014 \u0441\u0435\u0441\u0441\u0438\u044f \u0438 \u0444\u0430\u0439\u043b\u044b \u0432 \u0431\u0435\u0437\u043e\u043f\u0430\u0441\u043d\u043e\u0441\u0442\u0438\",\n  \"Changes in this turn\": \"\u0418\u0437\u043c\u0435\u043d\u0435\u043d\u0438\u044f \u0437\u0430 \u044d\u0442\u043e\u0442 \u0445\u043e\u0434\", \"{n} {files}\": \"{n} {files}\", \"new\": \"\u043d\u043e\u0432\u044b\u0439\", \"keep\": \"\u043e\u0441\u0442\u0430\u0432\u0438\u0442\u044c\", \"revert\": \"\u043e\u0442\u043a\u0430\u0442\u0438\u0442\u044c\", \"kept\": \"\u043e\u0441\u0442\u0430\u0432\u043b\u0435\u043d\u043e\", \"reverting\u2026\": \"\u043e\u0442\u043a\u0430\u0442\u044b\u0432\u0430\u044e\u2026\", \"reverted\": \"\u043e\u0442\u043a\u0430\u0442\u0435\u043d\u043e\",\n  \"ctx\": \"\u043a\u043e\u043d\u0442\u0435\u043a\u0441\u0442\", \"this task\": \"\u044d\u0442\u0430 \u0437\u0430\u0434\u0430\u0447\u0430\", \"session\": \"\u0441\u0435\u0441\u0441\u0438\u044f\", \"today\": \"\u0441\u0435\u0433\u043e\u0434\u043d\u044f\",\n  \"loaded into every turn\": \"\u0437\u0430\u0433\u0440\u0443\u0436\u0430\u0435\u0442\u0441\u044f \u0432 \u043a\u0430\u0436\u0434\u044b\u0439 \u0445\u043e\u0434\", \"ask the agent to create it, or /init in the terminal\": \"\u043f\u043e\u043f\u0440\u043e\u0441\u0438\u0442\u0435 \u0430\u0433\u0435\u043d\u0442\u0430 \u0441\u043e\u0437\u0434\u0430\u0442\u044c \u0435\u0433\u043e \u0438\u043b\u0438 \u0432\u044b\u043f\u043e\u043b\u043d\u0438\u0442\u0435 /init \u0432 \u0442\u0435\u0440\u043c\u0438\u043d\u0430\u043b\u0435\", \"no AGENTS.md\": \"\u043d\u0435\u0442 AGENTS.md\",\n  \"read-only\": \"\u0442\u043e\u043b\u044c\u043a\u043e \u0447\u0442\u0435\u043d\u0438\u0435\", \"ask\": \"\u0441\u043f\u0440\u0430\u0448\u0438\u0432\u0430\u0442\u044c\", \"full access\": \"\u043f\u043e\u043b\u043d\u044b\u0439 \u0434\u043e\u0441\u0442\u0443\u043f\", \"sandbox\": \"\u043f\u0435\u0441\u043e\u0447\u043d\u0438\u0446\u0430\",\n  \"no file changes; only allow-listed commands\": \"\u0431\u0435\u0437 \u0438\u0437\u043c\u0435\u043d\u0435\u043d\u0438\u0439 \u0444\u0430\u0439\u043b\u043e\u0432; \u0442\u043e\u043b\u044c\u043a\u043e \u0440\u0430\u0437\u0440\u0435\u0448\u0451\u043d\u043d\u044b\u0435 \u043a\u043e\u043c\u0430\u043d\u0434\u044b\",\n  \"edits inside the project are free; commands ask\": \"\u043f\u0440\u0430\u0432\u043a\u0438 \u0432 \u043f\u0440\u043e\u0435\u043a\u0442\u0435 \u0441\u0432\u043e\u0431\u043e\u0434\u043d\u044b; \u043a\u043e\u043c\u0430\u043d\u0434\u044b \u0437\u0430\u043f\u0440\u0430\u0448\u0438\u0432\u0430\u044e\u0442 \u043f\u043e\u0434\u0442\u0432\u0435\u0440\u0436\u0434\u0435\u043d\u0438\u0435\",\n  \"everything runs without asking\": \"\u0432\u0441\u0451 \u0432\u044b\u043f\u043e\u043b\u043d\u044f\u0435\u0442\u0441\u044f \u0431\u0435\u0437 \u0437\u0430\u043f\u0440\u043e\u0441\u0430\",\n  \"Cloud sessions run in an isolated sandbox, so tools run without asking. Permissions apply to the local agent\": \"\u041e\u0431\u043b\u0430\u0447\u043d\u044b\u0435 \u0441\u0435\u0441\u0441\u0438\u0438 \u0440\u0430\u0431\u043e\u0442\u0430\u044e\u0442 \u0432 \u0438\u0437\u043e\u043b\u0438\u0440\u043e\u0432\u0430\u043d\u043d\u043e\u0439 \u043f\u0435\u0441\u043e\u0447\u043d\u0438\u0446\u0435, \u043f\u043e\u044d\u0442\u043e\u043c\u0443 \u0438\u043d\u0441\u0442\u0440\u0443\u043c\u0435\u043d\u0442\u044b \u0432\u044b\u043f\u043e\u043b\u043d\u044f\u044e\u0442\u0441\u044f \u0431\u0435\u0437 \u0437\u0430\u043f\u0440\u043e\u0441\u0430. \u0420\u0430\u0437\u0440\u0435\u0448\u0435\u043d\u0438\u044f \u043e\u0442\u043d\u043e\u0441\u044f\u0442\u0441\u044f \u043a \u043b\u043e\u043a\u0430\u043b\u044c\u043d\u043e\u043c\u0443 \u0430\u0433\u0435\u043d\u0442\u0443\",\n  \"terminal\": \"\u0442\u0435\u0440\u043c\u0438\u043d\u0430\u043b\",\n  \"No models \u2014 add one in your account or with /models.\": \"\u041d\u0435\u0442 \u043c\u043e\u0434\u0435\u043b\u0435\u0439 \u2014 \u0434\u043e\u0431\u0430\u0432\u044c\u0442\u0435 \u0438\u0445 \u0432 \u0430\u043a\u043a\u0430\u0443\u043d\u0442\u0435 \u0438\u043b\u0438 \u043a\u043e\u043c\u0430\u043d\u0434\u043e\u0439 /models.\",\n  \"more agents in the marketplace \u2192\": \"\u0431\u043e\u043b\u044c\u0448\u0435 \u0430\u0433\u0435\u043d\u0442\u043e\u0432 \u0432 \u043a\u0430\u0442\u0430\u043b\u043e\u0433\u0435 \u2192\", \"more themes in the marketplace \u2192\": \"\u0431\u043e\u043b\u044c\u0448\u0435 \u0442\u0435\u043c \u0432 \u043a\u0430\u0442\u0430\u043b\u043e\u0433\u0435 \u2192\",\n  \"Install more with /theme in the terminal or the marketplace on the site\": \"\u0423\u0441\u0442\u0430\u043d\u043e\u0432\u0438\u0442\u0435 \u0434\u0440\u0443\u0433\u0438\u0435 \u043a\u043e\u043c\u0430\u043d\u0434\u043e\u0439 /theme \u0432 \u0442\u0435\u0440\u043c\u0438\u043d\u0430\u043b\u0435 \u0438\u043b\u0438 \u0432 \u043a\u0430\u0442\u0430\u043b\u043e\u0433\u0435 \u043d\u0430 \u0441\u0430\u0439\u0442\u0435\",\n  \"light\": \"\u0441\u0432\u0435\u0442\u043b\u0430\u044f\", \"dark\": \"\u0442\u0451\u043c\u043d\u0430\u044f\",\n  \"microphone: {m}\": \"\u043c\u0438\u043a\u0440\u043e\u0444\u043e\u043d: {m}\", \"transcribing\u2026\": \"\u0440\u0430\u0441\u043f\u043e\u0437\u043d\u0430\u044e\u2026\",\n  \"Project folder for the local agent\": \"\u041f\u0430\u043f\u043a\u0430 \u043f\u0440\u043e\u0435\u043a\u0442\u0430 \u0434\u043b\u044f \u043b\u043e\u043a\u0430\u043b\u044c\u043d\u043e\u0433\u043e \u0430\u0433\u0435\u043d\u0442\u0430\",\n  \"remove\": \"\u0443\u0434\u0430\u043b\u0438\u0442\u044c\", \"{name}: over 20 MB\": \"{name}: \u0431\u043e\u043b\u044c\u0448\u0435 20 \u041c\u0411\",\n  \"Notifications are blocked for this site \u2014 allow them in the browser settings.\": \"\u0423\u0432\u0435\u0434\u043e\u043c\u043b\u0435\u043d\u0438\u044f \u0434\u043b\u044f \u044d\u0442\u043e\u0433\u043e \u0441\u0430\u0439\u0442\u0430 \u0437\u0430\u0431\u043b\u043e\u043a\u0438\u0440\u043e\u0432\u0430\u043d\u044b \u2014 \u0440\u0430\u0437\u0440\u0435\u0448\u0438\u0442\u0435 \u0438\u0445 \u0432 \u043d\u0430\u0441\u0442\u0440\u043e\u0439\u043a\u0430\u0445 \u0431\u0440\u0430\u0443\u0437\u0435\u0440\u0430.\",\n  \"notifications: {m}\": \"\u0443\u0432\u0435\u0434\u043e\u043c\u043b\u0435\u043d\u0438\u044f: {m}\",\n  \"notifications on \u2014 you will hear when a task is done or needs you\": \"\u0443\u0432\u0435\u0434\u043e\u043c\u043b\u0435\u043d\u0438\u044f \u0432\u043a\u043b\u044e\u0447\u0435\u043d\u044b \u2014 \u0432\u044b \u0443\u0437\u043d\u0430\u0435\u0442\u0435, \u043a\u043e\u0433\u0434\u0430 \u0437\u0430\u0434\u0430\u0447\u0430 \u0432\u044b\u043f\u043e\u043b\u043d\u0435\u043d\u0430 \u0438\u043b\u0438 \u043d\u0443\u0436\u043d\u0430 \u0432\u0430\u0448\u0430 \u043f\u043e\u043c\u043e\u0449\u044c\",\n};\nconst RUPL = {file: [\"\u0444\u0430\u0439\u043b\", \"\u0444\u0430\u0439\u043b\u0430\", \"\u0444\u0430\u0439\u043b\u043e\u0432\"], image: [\"\u0438\u0437\u043e\u0431\u0440\u0430\u0436\u0435\u043d\u0438\u0435\", \"\u0438\u0437\u043e\u0431\u0440\u0430\u0436\u0435\u043d\u0438\u044f\", \"\u0438\u0437\u043e\u0431\u0440\u0430\u0436\u0435\u043d\u0438\u0439\"]};\nconst RUINV = Object.fromEntries(Object.entries(RU).map(([k, v]) => [v, k]));\nlet LANG = \"en\";\ntry { LANG = localStorage.getItem(\"me-lang\") || \"\"; } catch (e) {}\nif (LANG !== \"ru\" && LANG !== \"en\") LANG = (navigator.languages || [navigator.language || \"en\"]).some(l => /^(ru|be|kk)\\b/i.test(l)) ? \"ru\" : \"en\";\ndocument.documentElement.lang = LANG;\nfunction t(s, v) {\n  let r = LANG === \"ru\" && RU[s] !== undefined ? RU[s] : s;\n  if (v) r = r.replace(/\\{(\\w+)\\}/g, (m, k) => k in v ? v[k] : m);\n  return r;\n}\nconst untr = s => RUINV[s] || s;  // back to the English key (for text we stored translated)\nfunction pl(n, one, many) {  // \"1 file\" / \"3 files\"; Russian has three forms\n  if (LANG === \"ru\" && RUPL[one]) { const f = RUPL[one], a = n % 100, b = n % 10; return f[a > 10 && a < 20 ? 2 : b === 1 ? 0 : b > 1 && b < 5 ? 1 : 2]; }\n  return n === 1 ? one : many;\n}\nconst _e = s => String(s ?? \"\").replace(/[&<>\"]/g, c => ({\"&\": \"&amp;\", \"<\": \"&lt;\", \">\": \"&gt;\", '\"': \"&quot;\"})[c]);\nfunction tt(s, v) {  // HTML span that re-translates itself when the language flips\n  return `<span data-t=\"${_e(s)}\"${v ? ` data-v=\"${_e(JSON.stringify(v))}\"` : \"\"}>${_e(t(s, v))}</span>`;\n}\nfunction i18nApply(root) {\n  root = root || document;\n  root.querySelectorAll(\"[data-i18n]\").forEach(n => n.textContent = t(n.dataset.i18n));\n  root.querySelectorAll(\"[data-i18n-title]\").forEach(n => n.title = t(n.dataset.i18nTitle));\n  root.querySelectorAll(\"[data-i18n-aria]\").forEach(n => n.setAttribute(\"aria-label\", t(n.dataset.i18nAria)));\n  root.querySelectorAll(\"[data-i18n-ph]\").forEach(n => n.placeholder = t(n.dataset.i18nPh));\n  root.querySelectorAll(\"[data-t]\").forEach(n => { let v; try { v = n.dataset.v ? JSON.parse(n.dataset.v) : undefined; } catch (e) {} n.textContent = t(n.dataset.t, v); });\n}\nfunction setLang(l) {\n  LANG = l; try { localStorage.setItem(\"me-lang\", l); } catch (e) {}\n  document.documentElement.lang = l;\n  document.querySelectorAll(\".lang\").forEach(g => { g.classList.toggle(\"ru\", l === \"ru\"); g.querySelectorAll(\"button\").forEach(b => b.setAttribute(\"aria-pressed\", b.dataset.l === l)); });\n  i18nApply();\n  const ti = $(\"title\"); ti.textContent = t(untr(ti.textContent));\n  if (typeof renderChrome === \"function\") { renderChrome(); renderStatus(); renderSessions(); renderTodo(state.session?.todo || []); if (log.querySelector(\".empty\")) emptyState(); }\n  $(\"input\").placeholder = t(\"Ask anything\u2026\");\n}\ndocument.addEventListener(\"click\", e => { const b = e.target.closest && e.target.closest(\".lang button\"); if (b) setLang(b.dataset.l); });\n\nconst MODE = \"__MODE__\";\nconst $ = id => document.getElementById(id);\nconst side = $(\"side\");\nconst esc = s => String(s ?? \"\").replace(/[&<>\"]/g, c => ({\"&\": \"&amp;\", \"<\": \"&lt;\", \">\": \"&gt;\", '\"': \"&quot;\"})[c]);\nconst human = n => !n ? \"0\" : n < 1000 ? String(n) : n < 1e6 ? (n / 1000).toFixed(1).replace(\".0\", \"\") + \"k\" : (n / 1e6).toFixed(1).replace(\".0\", \"\") + \"M\";\nconst money = x => !x ? \"$0\" : x < 0.01 ? \"$\" + x.toFixed(4) : x < 1 ? \"$\" + x.toFixed(3) : \"$\" + x.toFixed(2);\nconst dur = s => s < 60 ? s.toFixed(1) + \"s\" : Math.floor(s / 60) + \"m\" + String(Math.floor(s % 60)).padStart(2, \"0\") + \"s\";\nconst COLORS = {red:\"#ff6b7a\",green:\"#5ad18a\",blue:\"#6ba6ff\",magenta:\"#d27cff\",cyan:\"#4fd1c5\",yellow:\"#e7c66b\",orange:\"#ffb454\",purple:\"#8b7cff\",pink:\"#ff8fc7\"};\nconst I = {\n  check: '<svg viewBox=\"0 0 24 24\" style=\"color:var(--ok)\"><path d=\"M5 12l5 5 9-11\"/></svg>',\n  x: '<svg viewBox=\"0 0 24 24\" style=\"color:var(--err)\"><path d=\"M6 6l12 12M18 6L6 18\"/></svg>',\n  stop: '<path d=\"M7 7h10v10H7z\" fill=\"currentColor\"/>', up: '<path d=\"M12 19V5M5 12l7-7 7 7\"/>',\n  think: '<svg viewBox=\"0 0 24 24\"><path d=\"M9 18h6M10 21h4M12 3a6 6 0 0 0-4 10.5V16h8v-2.5A6 6 0 0 0 12 3z\"/></svg>',\n};\nvar atts = [];  // pending attachments: {name, image, url | b64}\nlet token = localStorage.getItem(\"me_token\") || \"\", base = \"\", after = 0, state = {}, busy = false, live = {}, cur = {}, polling = 0, turnT0 = 0;\n\n// \u2500\u2500\u2500 transport: identical endpoints for local (`me web`) and cloud (/app) \u2500\u2500\u2500\nasync function api(path, body, method) {\n  const h = {\"Content-Type\": \"application/json\"};\n  if (token) h.Authorization = \"Bearer \" + token;\n  const r = await fetch(path, {method: method || (body !== undefined ? \"POST\" : \"GET\"), headers: h, body: body !== undefined ? JSON.stringify(body) : undefined});\n  const j = await r.json().catch(() => ({}));\n  if (r.status === 401 && MODE === \"cloud\") { location.href = \"/account#cloud\"; throw new Error(\"login\"); }\n  if (!r.ok) throw new Error(j.detail || r.statusText);\n  return j;\n}\nconst A = {\n  async boot() {\n    if (MODE === \"local\") {\n      const k = new URLSearchParams(location.search).get(\"k\"); if (k) { token = k; localStorage.setItem(\"me_token\", k); }\n      base = \"/api/agent\"; return api(base + \"/state\");\n    }\n    if (!token) { location.href = \"/account#cloud\"; return; }\n    const q = new URLSearchParams(location.search);\n    let w;\n    if (q.get(\"s\") || q.get(\"ws\")) w = await api(\"/api/cloud\", {session: q.get(\"s\"), ws: q.get(\"ws\")});\n    else w = (await api(\"/api/cloud\"))[0] || await api(\"/api/cloud\", {});\n    return A.attach(w);\n  },\n  async attach(w) { base = \"/api/cloud/\" + w.id; const st = await A.state(w); st.after = w.seq || 0; return st; },\n  async state(w) {\n    if (MODE === \"local\") return api(base + \"/state\");\n    const [cfg, sessions, me] = await Promise.all([api(\"/api/doc?key=config\").then(d => JSON.parse(d.content)).catch(() => ({})), api(\"/api/sessions\"), api(\"/api/me\")]);\n    let session = null;\n    if (w && w.session) session = await api(\"/api/doc?key=\" + encodeURIComponent(\"session/\" + w.session)).then(d => JSON.parse(d.content)).catch(() => null);\n    const [ag, man] = await Promise.all([api(\"/api/agents\").catch(() => ({agents: [], current: \"code\"})),\n      w && w.ws ? api(`/api/ws/${w.ws}/manifest`).catch(() => ({files: {}})) : {files: {}}]);\n    const context = [\"AGENTS.md\", \"ME.md\", \"CLAUDE.md\", \"DESIGN.md\", \"PLAN.md\"].filter(f => f in man.files);\n    return {mode: \"cloud\", user: me.username, ctx: session?.ctx || 0, config: {model: cfg.model, models: cfg.models || []}, sessions, session, ws: w && w.ws, worker: w,\n            agent: session?.agent || ag.current, agents: ag.agents, context, perm: \"sandbox\", preview: w && w.preview, push: me.push};\n  },\n  async load(id) {\n    if (MODE === \"local\") { await api(base + \"/load\", {id}); return api(base + \"/state\"); }\n    const w = await api(\"/api/cloud\", {session: id}); return A.attach(w);\n  },\n  async newChat() { await api(base + \"/new\", {}); return A.state(state.worker); },\n  async setAgent(name) {\n    if (MODE === \"local\") return api(base + \"/agent\", {name});\n    const d = await api(\"/api/doc?key=config\"); const cfg = JSON.parse(d.content); cfg.agent = name;\n    await api(\"/api/doc\", {key: \"config\", content: JSON.stringify(cfg, null, 1)}, \"PUT\"); await api(base + \"/sync\", {});\n  },\n  async setModel(name) {\n    if (MODE === \"local\") return api(base + \"/model\", {name});\n    const d = await api(\"/api/doc?key=config\"); const cfg = JSON.parse(d.content); cfg.model = name;\n    await api(\"/api/doc\", {key: \"config\", content: JSON.stringify(cfg, null, 1)}, \"PUT\"); await api(base + \"/sync\", {});\n  },\n  prompt: (text, images) => api(base + \"/prompt\", {text, images: images || []}),\n  goal: (text) => api(base + \"/goal\", {text}),\n  undo: () => api(base + \"/undo\", {}),\n  async upload(name, b64) {  // non-image attachments go into the project so the agent can read them\n    if (MODE === \"local\") return (await api(base + \"/upload\", {name, b64})).path;\n    const path = \"uploads/\" + name.replace(/[^\\w.-]+/g, \"_\").slice(0, 80);\n    await api(`/api/ws/${state.ws}/files`, {put: [{path, b64, mtime: Date.now() / 1000}]});\n    await api(base + \"/sync\", {}).catch(() => {});\n    return path;\n  },\n  async setPerm(mode) {\n    if (MODE === \"local\") return api(base + \"/perm\", {mode});\n    const d = await api(\"/api/doc?key=config\"); const cfg = JSON.parse(d.content); cfg.settings = {...(cfg.settings || {}), perm: mode, yolo: false};\n    await api(\"/api/doc\", {key: \"config\", content: JSON.stringify(cfg, null, 1)}, \"PUT\"); await api(base + \"/sync\", {});\n  },\n  voice: (blob) => fetch(MODE === \"local\" ? \"/api/agent/voice\" : \"/api/voice\", {method: \"POST\", headers: {\"Content-Type\": blob.type, ...(token ? {Authorization: \"Bearer \" + token} : {})}, body: blob}).then(r => r.json()),\n};\n\n// \u2500\u2500\u2500 markdown \u2500\u2500\u2500\nfunction md(src) {\n  const blocks = [];\n  src = src.replace(/```(\\w*)\\n?([\\s\\S]*?)(```|$)/g, (_, lang, code) => { blocks.push([lang, code]); return `\\u0000${blocks.length - 1}\\u0000`; });\n  let h = esc(src)\n    .replace(/^### (.*)$/gm, \"<h3>$1</h3>\").replace(/^## (.*)$/gm, \"<h2>$1</h2>\").replace(/^# (.*)$/gm, \"<h1>$1</h1>\")\n    .replace(/^&gt; (.*)$/gm, \"<blockquote>$1</blockquote>\")\n    .replace(/`([^`\\n]+)`/g, \"<code>$1</code>\").replace(/\\*\\*([^*]+)\\*\\*/g, \"<b>$1</b>\").replace(/(^|\\W)\\*([^*\\n]+)\\*/g, \"$1<i>$2</i>\")\n    .replace(/\\[([^\\]]+)\\]\\((https?:[^)]+)\\)/g, '<a href=\"$2\" target=\"_blank\" rel=\"noopener\">$1</a>');\n  h = h.replace(/(?:^|\\n)((?:[-*] .*(?:\\n|$))+)/g, (_, l) => \"\\n<ul>\" + l.trim().split(\"\\n\").map(x => \"<li>\" + x.slice(2) + \"</li>\").join(\"\") + \"</ul>\\n\");\n  h = h.replace(/(?:^|\\n)((?:\\d+[.)] .*(?:\\n|$))+)/g, (_, l) => \"\\n<ol>\" + l.trim().split(\"\\n\").map(x => \"<li>\" + x.replace(/^\\d+[.)] /, \"\") + \"</li>\").join(\"\") + \"</ol>\\n\");\n  h = h.split(/\\n{2,}/).map(p => /^\\s*<(h\\d|ul|ol|blockquote|\\u0000)/.test(p) || p.startsWith(\"\\u0000\") ? p : \"<p>\" + p.replace(/\\n/g, \"<br>\") + \"</p>\").join(\"\");\n  return h.replace(/\\u0000(\\d+)\\u0000/g, (_, i) => `<pre><div class=\"bar\"><span>${esc(blocks[i][0] || \"code\")}</span><button onclick=\"navigator.clipboard.writeText(this.closest('pre').querySelector('code').innerText)\">${tt(\"copy\")}</button></div><code>${esc(blocks[i][1])}</code></pre>`);\n}\n\n// \u2500\u2500\u2500 rendering \u2500\u2500\u2500\nconst log = $(\"log\"), chat = $(\"chat\");\nlet stick = true;\nchat.addEventListener(\"scroll\", () => stick = chat.scrollHeight - chat.scrollTop - chat.clientHeight < 80);\nfunction scroll() { if (stick) chat.scrollTop = chat.scrollHeight; }\nlet workEl = null;\nfunction el(html) { const d = document.createElement(\"div\"); d.innerHTML = html.trim(); const n = d.firstChild; i18nApply(d); workEl && workEl.parentNode === log ? log.insertBefore(n, workEl) : log.appendChild(n); scroll(); return n; }\nfunction clearLog() { log.innerHTML = \"\"; live = {}; cur = {}; workEl = null; }\nfunction emptyState() {\n  log.innerHTML = `<div class=\"empty\" style=\"min-height:60vh\"><div><h2>${esc(t(\"what are we\"))} <span>${esc(t(\"building?\"))}</span></h2><p>${esc(MODE === \"cloud\" ? t(\"Runs in your cloud sandbox \u00b7 files sync to all your machines\") : t(\"Running locally on this machine\"))}</p></div></div>`;\n}\nfunction userMsg(t) { if (log.querySelector(\".empty\")) log.innerHTML = \"\"; el(`<div class=\"msg user\"><div class=\"bub\">${esc(t)}</div></div>`); }\nfunction shortPath(s) {\n  s = String(s ?? \"\");\n  const roots = [state.session?.cwd, ...(state.session?.roots || []), state.cwd, \"/work\"].filter(Boolean);\n  for (const r of roots) s = s.split(r.replace(/\\/$/, \"\") + \"/\").join(\"\");\n  const max = innerWidth < 500 ? 34 : 70;\n  return s.length > max ? \"\u2026\" + s.slice(-(max - 1)) : s;\n}\nfunction toolCard(name, label, args, skill) {\n  label = shortPath(label);\n  const isSkill = name === \"skill\" && args && args.action;\n  const color = isSkill ? (COLORS[skill?.color] || \"var(--a)\") : \"var(--a)\";\n  const title = isSkill ? `${esc(args.name)} \u00b7 ${esc(args.action)}` : esc(name);\n  return el(`<div class=\"tool ${isSkill ? \"skill\" : \"\"} closed\" style=\"--sk:${color}\"><div class=\"h\" onclick=\"this.parentNode.classList.toggle('closed')\"><span class=\"ic\"><span class=\"spin\"></span></span><b>${title}</b><span class=\"lab\">${esc(isSkill ? JSON.stringify(args.args || {}) : label)}</span><span class=\"t\"></span></div><div class=\"st hidden\"></div><div class=\"pbar hidden\"><i></i></div><div class=\"log hidden\"></div><div class=\"body\"></div></div>`);\n}\nfunction diffHtml(t) { return t.split(\"\\n\").map(l => `<span class=\"${l[0] === \"+\" ? \"add\" : l[0] === \"-\" ? \"del\" : \"\"}\">${esc(l)}</span>`).join(\"\\n\"); }\nfunction onEvent(e) {\n  const t = e.type;\n  if (t === \"user\") { if (!cur.echo) userMsg(e.text); cur.echo = false; return; }\n  if (t === \"turn_start\") { if (e.context) { state.context = e.context; state.agent = e.agent || state.agent; renderStatus(); } busy = true; turnT0 = Date.now(); setBusy(); return; }\n  if (t === \"reasoning\") {\n    if (!cur.think) { cur.think = el(`<details class=\"think\"><summary>${I.think}<span class=\"shimmer\">${tt(\"thinking\u2026\")}</span></summary><pre></pre></details>`); cur.thinkT = Date.now(); }\n    cur.think.querySelector(\"pre\").textContent += e.text; scroll(); return;\n  }\n  const endThink = () => { if (cur.think && !cur.thinkDone) { cur.think.querySelector(\"summary span\").outerHTML = tt(\"thought for {d}\", {d: dur((Date.now() - cur.thinkT) / 1000)}); cur.thinkDone = true; } };\n  if (t === \"text\") {\n    endThink();\n    if (!cur.text) { cur.text = el(`<div class=\"msg md\"></div>`); cur.raw = \"\"; }\n    cur.raw += e.text; cur.text.innerHTML = md(cur.raw); scroll(); return;\n  }\n  if (t === \"tool_start\") {\n    endThink(); cur.text = null; cur.think = null; cur.thinkDone = false;\n    live[e.id] = toolCard(e.name, e.label, e.args, e.skill); return;\n  }\n  if (t === \"tool_event\") {\n    const c = live[e.id]; if (!c) return;\n    if (e.t === \"status\") { const s = c.querySelector(\".st\"); s.classList.remove(\"hidden\"); s.textContent = e.text; }\n    else if (e.t === \"progress\") { const p = c.querySelector(\".pbar\"); p.classList.remove(\"hidden\"); p.classList.toggle(\"ind\", e.v == null); if (e.v != null) p.firstChild.style.width = Math.round(e.v * 100) + \"%\"; }\n    else if (e.t === \"table\") {\n      const rows = e.rows || []; if (!rows.length) return;\n      const cols = Array.isArray(rows[0]) ? null : Object.keys(rows[0]);\n      const tr = r => \"<tr>\" + (cols ? cols.map(k => `<td>${esc(r[k])}</td>`) : r.map(x => `<td>${esc(x)}</td>`)).join(\"\") + \"</tr>\";\n      const p = document.createElement(\"div\"); p.className = \"panel\";\n      p.innerHTML = (e.title ? `<b>${esc(e.title)}</b>` : \"\") + `<table>${cols ? \"<tr>\" + cols.map(k => `<th>${esc(k)}</th>`).join(\"\") + \"</tr>\" : \"\"}${rows.map(tr).join(\"\")}</table>`;\n      c.insertBefore(p, c.querySelector(\".body\"));\n    }\n    else if (e.t === \"md\") { const p = document.createElement(\"div\"); p.className = \"panel md\"; p.innerHTML = md(e.text); c.insertBefore(p, c.querySelector(\".body\")); }\n    else if (e.t === \"diff\" && e.text.trim()) { c.querySelector(\".body\").innerHTML = diffHtml(e.text); c.classList.remove(\"closed\"); }\n    else { const l = c.querySelector(\".log\"); l.classList.remove(\"hidden\"); l.textContent = (\"\u203a \" + e.text + \"\\n\" + l.textContent).split(\"\\n\").slice(0, 3).join(\"\\n\"); }\n    scroll(); return;\n  }\n  if (t === \"tool_end\") {\n    const c = live[e.id]; if (!c) return; delete live[e.id];\n    c.querySelector(\".ic\").innerHTML = e.ok ? I.check : I.x; c.querySelector(\".t\").textContent = dur(e.time);\n    c.querySelectorAll(\".st,.pbar,.log\").forEach(x => x.classList.add(\"hidden\"));\n    if (c.querySelector(\".panel\")) c.classList.add(\"haspanel\");\n    const b = c.querySelector(\".body\"); if (!b.innerHTML) b.textContent = (e.output || \"\").slice(0, 20000);\n    if (!e.ok) c.classList.remove(\"closed\");\n    return;\n  }\n  if (t === \"ask\") {\n    const opts = e.options || [];\n    const c = el(`<div class=\"confirm ask\"><b>${esc(e.text)}</b><div class=\"btns\" style=\"margin-top:10px;flex-wrap:wrap\">${opts.length\n      ? opts.map((o, i) => `<button class=\"btn ${i ? \"ghost\" : \"\"}\" data-v=\"${esc(o)}\">${esc(o)}</button>`).join(\"\")\n      : `<input class=\"askin\" placeholder=\"answer\u2026\" data-i18n-ph=\"answer\u2026\"><button class=\"btn\" data-in=\"1\">${tt(\"send\")}</button>`}</div></div>`);\n    const send = v => { api(base + \"/answer\", {id: e.id, text: v}); c.innerHTML = `<span class=\"dim\">${esc(e.text)} \u2192 <b>${esc(v || \"\u2014\")}</b></span>`; c.className = \"info\"; };\n    c.querySelectorAll(\"button\").forEach(b => b.onclick = () => send(b.dataset.in ? c.querySelector(\"input\").value : b.dataset.v));\n    c.querySelector(\"input\")?.addEventListener(\"keydown\", ev => ev.key === \"Enter\" && send(ev.target.value));\n    return;\n  }\n  if (t === \"confirm\") {\n    const c = el(`<div class=\"confirm\"><b>${tt(\"Allow {name}?\", {name: e.name})}</b><pre>${esc(JSON.stringify(e.args, null, 1)).slice(0, 3000)}</pre><div class=\"btns\"><button class=\"btn\">${tt(\"allow\")}</button><button class=\"btn ghost\">${tt(e.name === \"bash\" ? \"always allow\" : \"allow all (full)\")}</button><button class=\"btn ghost\">${tt(\"deny\")}</button></div></div>`);\n    const [y, al, n] = c.querySelectorAll(\"button\");\n    y.onclick = () => { api(base + \"/confirm\", {id: e.id, ok: true}); c.remove(); };\n    al.onclick = () => { api(base + \"/confirm\", {id: e.id, ok: \"always\"}); if (e.name !== \"bash\") { state.perm = \"full\"; A.setPerm(\"full\"); renderStatus(); } c.remove(); };\n    n.onclick = () => { api(base + \"/confirm\", {id: e.id, ok: false}); c.remove(); };\n    return;\n  }\n  if (t === \"compacting\") { el(`<div class=\"info\">${tt(\"compacting history\u2026\")}</div>`); return; }\n  if (t === \"compacted\") { state.ctx = e.ctx; renderStatus(); el(`<div class=\"info\">${tt(\"compacted {a} \u2192 {b} messages \u00b7 ~{c} tokens\", {a: e.before, b: e.after, c: human(e.ctx)})}</div>`); return; }\n  if (t === \"usage\") { state.ctx = e.ctx; if (state.session) state.session.usage = {...(state.session.usage || {}), cost: e.session_cost}; if (e.day_cost != null) state.day_cost = e.day_cost; renderStatus(e.in, e.out); return; }\n  if (t === \"error\") { cur.failed = true; el(`<div class=\"info ${e.text === \"interrupted\" ? \"warn\" : \"err\"}\">${e.text === \"interrupted\" ? tt(\"interrupted\") : esc(e.text)}</div>`); return; }\n  if (t === \"turn_end\") {\n    const failed = cur.failed; endThink(); busy = false; cur = {}; setBusy();\n    el(`<div class=\"stats\">${failed ? I.x : I.check}${tt(\"{a} \u00b7 \u2191 {i} \u2193 {o} \u00b7 {n} tokens{c}\", {a: dur(e.time), i: human(e.in), o: human(e.out), n: human(e.in + e.out), c: e.cost ? \" \u00b7 \" + money(e.cost) : \"\"})}</div>`);\n    state.ctx = e.ctx; renderStatus(); refreshSessions(); return;\n  }\n  if (t === \"todo\") { if (state.session) state.session.todo = e.items; renderTodo(e.items); return; }\n  if (t === \"goal\") { goalEvent(e); return; }\n  if (t === \"verify\") { el(`<div class=\"info\">${tt(\"self-check: running checks before finishing\")}</div>`); return; }\n  if (t === \"turn_diff\") { diffCard(e.files || []); return; }\n  if (t === \"undone\") { el(`<div class=\"info warn\">\u21b6 ${esc(e.text)}</div>`); A.state(state.worker).then(s => { state.session = s.session; renderHistory(s.session); }); return; }\n  if (t === \"loaded\" && e.title) $(\"title\").textContent = e.title;\n  if (t === \"reload\") { A.state(state.worker).then(s => { state.session = s.session; renderHistory(s.session); renderSessions(); }); return; }\n  if (t === \"exit\") { busy = false; setBusy(); restartSandbox(); }\n}\nfunction renderHistory(s) {\n  clearLog();\n  if (!s || !s.messages || !s.messages.length) renderTodo([]);\n  if (!s || !s.messages || !s.messages.length) return emptyState();\n  const txt = c => Array.isArray(c) ? c.filter(x => x.type === \"text\").map(x => x.text).join(\" \") : (c || \"\");\n  const outs = {};\n  s.messages.forEach(m => { if (m.role === \"tool\") outs[m.tool_call_id] = m.content; });\n  s.messages.forEach(m => {\n    if (m.role === \"user\" && txt(m.content) !== \"(images from read)\") userMsg(txt(m.content));\n    else if (m.role === \"assistant\") {\n      if (m.content) el(`<div class=\"msg md\">${md(m.content)}</div>`);\n      (m.tool_calls || []).forEach(tc => {\n        let a = {}; try { a = JSON.parse(tc.function.arguments); } catch (e) {}\n        const c = toolCard(tc.function.name, a.path || a.cmd || a.pattern || a.query || a.url || a.name || \"\", a);\n        c.querySelector(\".ic\").innerHTML = I.check; c.querySelector(\".body\").textContent = String(outs[tc.id] || \"\").slice(0, 8000);\n      });\n    }\n  });\n  $(\"title\").textContent = s.title || t(\"Chat\");\n  renderTodo(s.todo || []);\n  if (s.goal && s.goal.status !== \"working\") goalEvent(s.goal, true);\n  setTimeout(() => chat.scrollTop = chat.scrollHeight, 0);\n}\nfunction renderStatus(tin, tout) {\n  const m = (state.config?.models || []).find(x => x.name === state.config?.model);\n  $(\"model\").textContent = m ? m.name + (m.effort ? \" \u00b7 \" + m.effort : \"\") : t(\"no model\");\n  const ctx = m?.context || 0, used = state.ctx || 0, f = ctx ? Math.min(1, used / ctx) : 0;\n  const col = f < .6 ? \"var(--ok)\" : f < .85 ? \"var(--warn)\" : \"var(--err)\";\n  $(\"ctx\").innerHTML = ctx ? `${t(\"ctx\")} <span class=\"bar\"><i style=\"width:${(f * 100).toFixed(1)}%;background:${col}\"></i></span> ${human(used)}/${human(ctx)} ${Math.round(f * 100)}%` : `${t(\"ctx\")} ${human(used)}`;\n  const u = state.session?.usage || {};\n  $(\"tok\").textContent = tin !== undefined ? `${t(\"this task\")} \u2191${human(tin)} \u2193${human(tout)}` : `${t(\"session\")} \u2191${human(u.in)} \u2193${human(u.out)}`;\n  const ag = (state.agents || []).find(a => a.name === state.agent);\n  $(\"agentName\").textContent = ag ? ag.label : (state.agent || \"code\");\n  const cf = state.context || [];\n  $(\"ctxfiles\").innerHTML = cf.length ? `<span class=\"cf on\" title=\"${esc(t(\"loaded into every turn\"))}\">\u25c6 ${cf.map(esc).join(\" \u00b7 \")}</span>` : `<span class=\"cf\" title=\"${esc(t(\"ask the agent to create it, or /init in the terminal\"))}\">\u25c7 ${esc(t(\"no AGENTS.md\"))}</span>`;\n  const cost = state.session?.usage?.cost || 0;\n  $(\"cost\").textContent = cost || state.day_cost ? money(cost) + (state.day_cost ? \" \u00b7 \" + t(\"today\") + \" \" + money(state.day_cost) : \"\") : \"\";\n  const pm = state.perm || \"ask\";\n  $(\"perm\").textContent = {read: t(\"read-only\"), ask: t(\"ask\"), full: t(\"full access\"), sandbox: t(\"sandbox\")}[pm] || pm;\n  $(\"perm\").className = \"perm \" + pm;\n  if (state.preview) { $(\"previewBtn\").href = state.preview + \"/\"; $(\"previewBtn\").classList.remove(\"hidden\"); } else $(\"previewBtn\").classList.add(\"hidden\");\n  $(\"ws\").textContent = state.ws || (MODE === \"local\" ? (state.cwd || t(\"local\")).split(/[\\\\/]/).pop() : t(\"cloud\"));\n}\nfunction renderSessions() {\n  const ss = state.sessions || [], sid = state.session?.id;\n  const groups = {};\n  ss.forEach(s => { const d = new Date((s.updated || 0) * 1000), age = (Date.now() - d) / 864e5;\n    const g = age < 1 ? t(\"Today\") : age < 7 ? t(\"This week\") : t(\"Older\"); (groups[g] ??= []).push(s); });\n  $(\"slist\").innerHTML = Object.entries(groups).map(([g, l]) => `<div class=\"lbl\">${g}</div>` + l.map(s =>\n    `<button class=\"si ${s.id === sid ? \"on\" : \"\"}\" onclick=\"loadSession('${esc(s.id)}')\" title=\"${esc(s.cwd || \"\")}\">${esc(s.title || t(\"(untitled)\"))}</button>`).join(\"\")).join(\"\") || `<div class=\"lbl\">${esc(t(\"no chats yet\"))}</div>`;\n}\nasync function refreshSessions() { try { const s = await A.state(state.worker); state.sessions = s.sessions; if (!state.session || state.session.id === s.session?.id) state.session = s.session || state.session; renderSessions(); } catch (e) {} }\nfunction setBusy() {\n  $(\"sendIcon\").innerHTML = busy ? I.stop : I.up;\n  $(\"send\").disabled = !busy && !$(\"input\").value.trim() && !atts.length;\n  if (busy && (!workEl || workEl.parentNode !== log)) {\n    workEl = document.createElement(\"div\"); workEl.className = \"working\";\n    workEl.innerHTML = '<span class=\"spin\"></span><span class=\"shimmer\" data-t=\"working\u2026\">'+t('working\u2026')+'</span><span class=\"el\"></span>'; log.appendChild(workEl); scroll();\n  }\n  if (!busy && workEl) { workEl.remove(); workEl = null; }\n}\nsetInterval(() => { const w = document.querySelector(\".working .el\"); if (w && busy) w.textContent = dur((Date.now() - turnT0) / 1000); }, 200);\n\n// \u2500\u2500\u2500 actions \u2500\u2500\u2500\nasync function poll() {\n  const my = ++polling;\n  while (my === polling) {\n    try {\n      const r = await api(`${base}/events?after=${after}`);\n      if (my !== polling) return;\n      for (const e of r.events) { after = Math.max(after, e.seq); onEvent(e); }\n      if (r.state) { busy = r.state.busy; setBusy(); }\n    } catch (e) {\n      if (MODE === \"cloud\" && /no such cloud session/.test(e.message)) { const w = await api(\"/api/cloud\", {session: state.session?.id, ws: state.ws}); state.worker = w; base = \"/api/cloud/\" + w.id; after = w.seq || 0; continue; }\n      await new Promise(r => setTimeout(r, 2000));\n    }\n  }\n}\nasync function restartSandbox() {\n  if (MODE !== \"cloud\") return;\n  el(`<div class=\"info warn\">${tt(\"sandbox restarted \u2014 your session and files are safe\")}</div>`);\n  try { const w = await api(\"/api/cloud\", {session: state.session?.id, ws: state.ws}); polling++; apply(await A.attach(w), false); } catch (e) {}\n}\nfunction renderChrome() {\n  $(\"mode\").textContent = t(MODE === \"cloud\" ? \"cloud\" : \"local\");\n  $(\"who\").textContent = state.user || t(MODE === \"local\" ? \"local\" : \"account\");\n}\nasync function apply(s, history = true) {\n  state = s; if (history) renderHistory(s.session); renderSessions(); renderStatus();\n  renderChrome();\n  after = s.after ?? after;\n  if (s.busy) { busy = true; turnT0 = Date.now(); setBusy(); }\n  (s.pending || []).forEach(onEvent);\n  poll();\n}\nasync function send() {\n  let t = $(\"input\").value.trim(); if ((!t && !atts.length) || busy) return;\n  const fail = e => el(`<div class=\"info err\">${esc(e.message)}</div>`);\n  if (t === \"/compact\") { $(\"input\").value = \"\"; grow(); return api(base + \"/compact\", {}).catch(fail); }\n  if (t === \"/undo\") { $(\"input\").value = \"\"; grow(); return A.undo().catch(fail); }\n  const goal = /^\\/goal\\s+/.test(t);\n  if (goal) t = t.replace(/^\\/goal\\s+/, \"\").replace(/^\"|\"$/g, \"\");\n  const images = atts.filter(a => a.image).map(a => a.url);\n  const files = [];\n  for (const a of atts.filter(a => !a.image)) { try { files.push(await A.upload(a.name, a.b64)); } catch (e) { fail(e); } }\n  if (files.length) t += (t ? \"\\n\\n\" : \"\") + \"Attached: \" + files.join(\", \");\n  if (!t && images.length) t = \"(see the attached image\" + (images.length > 1 ? \"s\" : \"\") + \")\";\n  atts = []; renderAtts();\n  $(\"input\").value = \"\"; grow(); userMsg((goal ? \"/goal \" : \"\") + t + (images.length ? `  [${images.length} ${pl(images.length, \"image\", \"images\")}]` : \"\"));\n  if (untr($(\"title\").textContent) === \"New chat\") $(\"title\").textContent = t.split(\"\\n\")[0].slice(0, 60); cur.echo = true; busy = true; turnT0 = Date.now(); setBusy(); stick = true;\n  try { await (goal ? A.goal(t) : A.prompt(t, images)); } catch (e) { fail(e); busy = false; setBusy(); }\n}\nfunction sendOrStop() { busy ? api(base + \"/cancel\", {}) : send(); }\nfunction openSide() { side.classList.add(\"open\"); $(\"scrim\").classList.add(\"on\"); }\nfunction closeSide() { side.classList.remove(\"open\"); $(\"scrim\").classList.remove(\"on\"); }\ndocument.addEventListener(\"keydown\", e => { if (e.key === \"Escape\") { closeSide(); $(\"popup\").classList.add(\"hidden\"); } });\nlet touchX = null;  // swipe left on the drawer to close it\nside.addEventListener(\"touchstart\", e => touchX = e.touches[0].clientX, {passive: true});\nside.addEventListener(\"touchend\", e => { if (touchX !== null && e.changedTouches[0].clientX - touchX < -60) closeSide(); touchX = null; });\nasync function newChat() { closeSide(); await apply(await A.newChat()); history.replaceState(null, \"\", location.pathname); $(\"title\").textContent = t(\"New chat\"); }\nasync function loadSession(id) { closeSide(); polling++; await apply(await A.load(id)); if (MODE === \"cloud\") history.replaceState(null, \"\", \"?s=\" + id); }\nfunction pickModel() {\n  const ms = state.config?.models || [];\n  $(\"pbody\").innerHTML = ms.map(m => `<button onclick=\"setModel('${esc(m.name)}')\"><span>${m.name === state.config.model ? \"\u25cf\" : \"\u25cb\"}</span>${esc(m.name)}<span class=\"d\">${esc(m.id)}</span></button>`).join(\"\") || `<p style=\"padding:12px;color:var(--dim)\">${esc(t(\"No models \u2014 add one in your account or with /models.\"))}</p>`;\n  $(\"popup\").classList.remove(\"hidden\");\n}\nfunction pickAgent() {\n  $(\"pbody\").innerHTML = (state.agents || []).map(a => `<button onclick=\"setAgent('${esc(a.name)}')\"><span>${a.name === state.agent ? \"\u25cf\" : \"\u25cb\"}</span><b>${esc(a.label)}</b><span class=\"d\">${esc(a.description)}</span></button>`).join(\"\")\n    + (MODE === \"cloud\" ? `<a class=\"more\" href=\"/account#market\">${esc(t(\"more agents in the marketplace \u2192\"))}</a>` : \"\");\n  $(\"popup\").classList.remove(\"hidden\");\n}\nasync function setAgent(n) { $(\"popup\").classList.add(\"hidden\"); state.agent = n; renderStatus(); await A.setAgent(n); }\nasync function setModel(n) { $(\"popup\").classList.add(\"hidden\"); await A.setModel(n); state.config.model = n; renderStatus(); }\nconst inp = $(\"input\");\nfunction grow() { inp.style.height = \"auto\"; inp.style.height = Math.min(220, inp.scrollHeight) + \"px\"; setBusy(); }\ninp.addEventListener(\"input\", grow);\ninp.addEventListener(\"keydown\", e => { if (e.key === \"Enter\" && !e.shiftKey && !e.isComposing && innerWidth > 760) { e.preventDefault(); send(); } if (e.key === \"Escape\" && busy) api(base + \"/cancel\", {}); });\n// voice: hold the mic button (or hold space in an empty input)\nlet rec = null, chunks = [];\nasync function recStart() {\n  if (rec) return;\n  try {\n    const stream = await navigator.mediaDevices.getUserMedia({audio: true});\n    rec = new MediaRecorder(stream); chunks = [];\n    rec.ondataavailable = e => chunks.push(e.data);\n    rec.onstop = async () => {\n      stream.getTracks().forEach(t => t.stop()); $(\"mic\").classList.remove(\"rec\");\n      const blob = new Blob(chunks, {type: rec.mimeType || \"audio/webm\"}); rec = null;\n      if (blob.size < 2000) return;\n      inp.placeholder = t(\"transcribing\u2026\");\n      try { const r = await A.voice(blob); if (r.text) { inp.value += (inp.value && !inp.value.endsWith(\" \") ? \" \" : \"\") + r.text; grow(); inp.focus(); } else if (r.detail) alert(r.detail); }\n      finally { inp.placeholder = t(\"Ask anything\u2026\"); }\n    };\n    rec.start(); $(\"mic\").classList.add(\"rec\");\n  } catch (e) { alert(t(\"microphone: {m}\", {m: e.message})); }\n}\nfunction recStop() { if (rec && rec.state === \"recording\") rec.stop(); }\nconst mic = $(\"mic\");\nmic.addEventListener(\"pointerdown\", e => { e.preventDefault(); recStart(); });\n[\"pointerup\", \"pointerleave\", \"pointercancel\"].forEach(ev => mic.addEventListener(ev, recStop));\nlet spaceT = 0;\ninp.addEventListener(\"keydown\", e => { if (e.code === \"Space\" && !inp.value && e.repeat) { e.preventDefault(); if (!rec) recStart(); } });\ninp.addEventListener(\"keyup\", e => { if (e.code === \"Space\") recStop(); });\n$(\"wsPill\").onclick = async () => {\n  if (MODE === \"cloud\") return location.href = \"/account#workspaces\";\n  const p = prompt(t(\"Project folder for the local agent\"), state.cwd || \"\");\n  if (!p) return;\n  try { await apply(await api(base + \"/cwd\", {path: p})); $(\"title\").textContent = t(\"New chat\"); } catch (e) { alert(e.message); }\n};\nA.boot().then(s => { if (s) { apply(s); loadThemes().catch(() => {}); } }).catch(e => el(`<div class=\"info err\">${esc(e.message)}</div>`));\n\n// \u2500\u2500\u2500 themes: same tokens as the terminal (see 14a_themes.py) \u2500\u2500\u2500\nconst THEME_VARS = {bg: \"--bg\", bg2: \"--bg2\", bg3: \"--bg3\", line: \"--line\", fg: \"--fg\", dim: \"--dim\", faint: \"--faint\", accent: \"--a\", accent2: \"--b\", ok: \"--ok\", err: \"--err\", warn: \"--warn\", user: \"--user\"};\nlet themeList = [];\nfunction onAccent(hex) {  // black or white text, whichever reads better on the accent color\n  const n = parseInt(hex.slice(1), 16), [r, g, b] = [n >> 16, (n >> 8) & 255, n & 255].map(v => { v /= 255; return v <= .03928 ? v / 12.92 : ((v + .055) / 1.055) ** 2.4; });\n  return .2126 * r + .7152 * g + .0722 * b > .35 ? \"#0a0a0a\" : \"#ffffff\";\n}\nfunction applyTheme(t) {\n  if (!t) return;\n  const r = document.documentElement;\n  for (const [k, v] of Object.entries(THEME_VARS)) if (t.colors[k]) r.style.setProperty(v, t.colors[k]);\n  r.style.colorScheme = t.dark === false ? \"light\" : \"dark\";\n  r.style.setProperty(\"--on-a\", onAccent(t.colors.accent));\n  document.querySelector('meta[name=\"theme-color\"]')?.setAttribute(\"content\", t.colors.bg);\n  try { localStorage.setItem(\"me_theme_cache\", JSON.stringify(t)); } catch (e) {}\n}\ntry { applyTheme(JSON.parse(localStorage.getItem(\"me_theme_cache\"))); } catch (e) {}\nasync function loadThemes() {\n  const r = await api(MODE === \"local\" ? \"/api/agent/themes\" : \"/api/themes\");\n  themeList = r.themes; applyTheme(themeList.find(t => t.name === r.current));\n}\nfunction pickTheme() {\n  $(\"pbody\").innerHTML = themeList.map(t => `<button onclick=\"setTheme('${esc(t.name)}')\"><span class=\"sw\" style=\"background:${t.colors.bg};border-color:${t.colors.line}\"><i style=\"background:${t.colors.accent}\"></i><i style=\"background:${t.colors.accent2}\"></i></span>${esc(t.label || t.name)}<span class=\"d\">${esc(t.dark === false ? window.t(\"light\") : window.t(\"dark\"))}</span></button>`).join(\"\")\n    + `<a class=\"more\" href=\"${MODE === \"cloud\" ? \"/account#market\" : \"#\"}\" onclick=\"${MODE === \"local\" ? \"alert(t('Install more with /theme in the terminal or the marketplace on the site'));return false\" : \"\"}\">${esc(window.t(\"more themes in the marketplace \u2192\"))}</a>`;\n  $(\"popup\").classList.remove(\"hidden\");\n}\nasync function setTheme(name) {\n  applyTheme(themeList.find(t => t.name === name)); $(\"popup\").classList.add(\"hidden\");\n  if (MODE === \"local\") return api(\"/api/agent/theme\", {name});\n  const d = await api(\"/api/doc?key=config\"); const cfg = JSON.parse(d.content); cfg.theme = name;\n  await api(\"/api/doc\", {key: \"config\", content: JSON.stringify(cfg, null, 1)}, \"PUT\");\n}\n\n// \u2500\u2500\u2500 todo checklist (sidebar card + header chip), goals, turn diff review \u2500\u2500\u2500\nfunction renderTodo(items) {\n  items = items || [];\n  const card = $(\"todoCard\"), chip = $(\"todoChip\");\n  const done = items.filter(i => i.s === \"done\").length;\n  if (!items.length) { card.classList.add(\"hidden\"); chip.classList.add(\"hidden\"); return; }\n  card.classList.remove(\"hidden\");\n  card.innerHTML = `<div class=\"tc-h\"><b>${esc(t(\"Checklist\"))}</b><span>${done}/${items.length}</span></div><div class=\"tc-bar\"><i style=\"width:${(done / items.length * 100).toFixed(0)}%\"></i></div>`\n    + items.map(i => `<div class=\"tc-i ${i.s}\"><span class=\"tc-b\">${i.s === \"done\" ? '<svg viewBox=\"0 0 24 24\"><path d=\"M5 12l5 5 9-11\"/></svg>' : i.s === \"doing\" ? '<span class=\"spin\"></span>' : \"\"}</span><span>${esc(i.t)}</span></div>`).join(\"\");\n  chip.querySelector(\"span\").textContent = `${done}/${items.length}`;\n  chip.classList.toggle(\"hidden\", done === items.length && !busy);\n}\nfunction goalEvent(g, history) {\n  if (g.status === \"working\") { if (!history) el(`<div class=\"goal working\"><span class=\"spin\"></span><span><b>${tt(\"Goal\")}</b> ${esc(g.text)}</span></div>`); return; }\n  const ok = g.status === \"done\";\n  el(`<div class=\"goal ${ok ? \"done\" : \"blocked\"}\">${ok ? I.check : I.x}<span><b>${tt(ok ? \"Goal done\" : \"Goal blocked\")}</b>${g.reason ? \" \u2014 \" + esc(g.reason) : \"\"}</span></div>`);\n}\nfunction diffCard(files) {\n  if (!files.length) return;\n  const stat = d => { let a = 0, r = 0; d.split(\"\\n\").forEach(l => { if (l[0] === \"+\" && !l.startsWith(\"+++\")) a++; else if (l[0] === \"-\" && !l.startsWith(\"---\")) r++; }); return `<span class=\"add\">+${a}</span> <span class=\"del\">\u2212${r}</span>`; };\n  const c = el(`<div class=\"review\"><div class=\"rv-h\"><b>${tt(\"Changes in this turn\")}</b><span class=\"d\">${tt(\"{n} {files}\", {n: files.length, files: pl(files.length, \"file\", \"files\")})}</span></div>${files.map(f =>\n    `<details><summary><span class=\"rv-p\">${esc(shortPath(f.path))}</span>${f.new ? `<span class=\"tag\">${tt(\"new\")}</span>` : \"\"}${stat(f.diff)}</summary><pre>${diffHtml(f.diff)}</pre></details>`).join(\"\")}\n    <div class=\"btns\"><button class=\"btn ghost\" data-a=\"keep\">${tt(\"keep\")}</button><button class=\"btn ghost danger\" data-a=\"revert\">${tt(\"revert\")}</button></div></div>`);\n  c.querySelector('[data-a=\"keep\"]').onclick = () => { c.querySelector(\".btns\").innerHTML = `<span class=\"dim\">${tt(\"kept\")}</span>`; };\n  c.querySelector('[data-a=\"revert\"]').onclick = async () => {\n    if (busy) return;\n    c.querySelector(\".btns\").innerHTML = `<span class=\"dim\">${tt(\"reverting\u2026\")}</span>`;\n    try { await A.undo(); c.querySelector(\".btns\").innerHTML = `<span class=\"dim\">${tt(\"reverted\")}</span>`; } catch (e) { c.querySelector(\".btns\").innerHTML = `<span class=\"err\">${esc(e.message)}</span>`; }\n  };\n}\n// \u2500\u2500\u2500 permissions \u2500\u2500\u2500\nfunction pickPerm() {\n  if (state.perm === \"sandbox\") { $(\"pbody\").innerHTML = `<p style=\"padding:12px;color:var(--dim)\">${esc(t(\"Cloud sessions run in an isolated sandbox, so tools run without asking. Permissions apply to the local agent\"))} (<code>me web</code> / ${esc(t(\"terminal\"))}).</p>`; return $(\"popup\").classList.remove(\"hidden\"); }\n  const P = {read: [t(\"read-only\"), t(\"no file changes; only allow-listed commands\")], ask: [t(\"ask\"), t(\"edits inside the project are free; commands ask\")], full: [t(\"full access\"), t(\"everything runs without asking\")]};\n  $(\"pbody\").innerHTML = Object.entries(P).map(([k, [l, d]]) => `<button onclick=\"setPerm('${k}')\"><span>${state.perm === k ? \"\u25cf\" : \"\u25cb\"}</span><b>${l}</b><span class=\"d\">${d}</span></button>`).join(\"\");\n  $(\"popup\").classList.remove(\"hidden\");\n}\nasync function setPerm(m) { $(\"popup\").classList.add(\"hidden\"); state.perm = m; renderStatus(); try { await A.setPerm(m); } catch (e) { alert(e.message); } }\n// \u2500\u2500\u2500 attachments: images go to the model, other files into the project \u2500\u2500\u2500\nfunction renderAtts() {\n  const box = $(\"atts\");\n  box.classList.toggle(\"hidden\", !atts.length);\n  box.innerHTML = atts.map((a, i) => `<span class=\"att\">${a.image ? `<img src=\"${a.url}\" alt=\"\">` : '<svg viewBox=\"0 0 24 24\"><path d=\"M14 3H6a1 1 0 0 0-1 1v16a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1V8z\"/><path d=\"M14 3v5h5\"/></svg>'}<span class=\"an\">${esc(a.name)}</span><button onclick=\"atts.splice(${i},1);renderAtts();setBusy()\" aria-label=\"${esc(t(\"remove\"))}\"><svg viewBox=\"0 0 24 24\"><path d=\"M6 6l12 12M18 6L6 18\"/></svg></button></span>`).join(\"\");\n}\nfunction shrink(dataUrl, max = 1600) {  // phone photos are huge; models need ~1.5k px at most\n  return new Promise(res => { const img = new Image(); img.onload = () => {\n    const k = Math.min(1, max / Math.max(img.width, img.height)); if (k === 1 && dataUrl.length < 1.5e6) return res(dataUrl);\n    const c = document.createElement(\"canvas\"); c.width = Math.round(img.width * k); c.height = Math.round(img.height * k);\n    c.getContext(\"2d\").drawImage(img, 0, 0, c.width, c.height); res(c.toDataURL(\"image/jpeg\", .85)); }; img.onerror = () => res(dataUrl); img.src = dataUrl; });\n}\nasync function addFiles(list) {\n  for (const f of list) {\n    if (f.size > 20e6) { alert(t(\"{name}: over 20 MB\", {name: f.name})); continue; }\n    const url = await new Promise(r => { const fr = new FileReader(); fr.onload = () => r(fr.result); fr.readAsDataURL(f); });\n    if (f.type.startsWith(\"image/\")) atts.push({name: f.name || \"image.png\", image: true, url: await shrink(url)});\n    else atts.push({name: f.name, b64: url.split(\",\")[1]});\n  }\n  renderAtts(); setBusy();\n}\n$(\"file\").addEventListener(\"change\", e => { addFiles([...e.target.files]); e.target.value = \"\"; });\n$(\"input\").addEventListener(\"paste\", e => { const fs = [...(e.clipboardData?.files || [])]; if (fs.length) { e.preventDefault(); addFiles(fs); } });\ndocument.addEventListener(\"dragover\", e => e.preventDefault());\ndocument.addEventListener(\"drop\", e => { if (e.dataTransfer?.files?.length) { e.preventDefault(); addFiles([...e.dataTransfer.files]); } });\n// \u2500\u2500\u2500 PWA + push (\"done\", \"waiting for you\") \u2500\u2500\u2500\nif (\"serviceWorker\" in navigator && location.protocol !== \"file:\") navigator.serviceWorker.register(\"/sw.js\").then(() => {\n  if (MODE === \"cloud\" && \"PushManager\" in window && Notification.permission !== \"granted\") $(\"bell\").classList.remove(\"hidden\");\n}).catch(() => {});\nasync function enablePush() {\n  try {\n    if (await Notification.requestPermission() !== \"granted\") return alert(t(\"Notifications are blocked for this site \u2014 allow them in the browser settings.\"));\n    const {key} = await api(\"/api/push/key\");\n    const reg = await navigator.serviceWorker.ready;\n    const b = atob(key.replace(/-/g, \"+\").replace(/_/g, \"/\") + \"=\".repeat((4 - key.length % 4) % 4));\n    const sub = await reg.pushManager.subscribe({userVisibleOnly: true, applicationServerKey: Uint8Array.from(b, c => c.charCodeAt(0))});\n    await api(\"/api/push/subscribe\", sub.toJSON());\n    $(\"bell\").classList.add(\"hidden\");\n    el(`<div class=\"info\">${tt(\"notifications on \u2014 you will hear when a task is done or needs you\")}</div>`);\n  } catch (e) { alert(t(\"notifications: {m}\", {m: e.message})); }\n}\n\nsetLang(LANG);\n</script>\n</body>\n</html>\n"
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
        return {"mode": "local", "user": self.s.auth.get("user", ""), "cwd": os.getcwd(), "after": self.seq, "ctx": self.agent.ctx_used or ses.get("ctx", 0),
                "config": {"model": self.s.config.get("model"), "models": self.s.config.get("models", [])},
                "session": ses, "ws": ses.get("ws"), "busy": self.agent.busy,
                "pending": [w[2] for w in list(self.waits.values())], "perm": perm_mode(self.s.config["settings"]),
                "day_cost": day_cost(self.s.home), "preview": "",
                "agent": self.agent.preset()["name"], "context": self.agent.context_files(),
                "agents": [{"name": n, "label": p.get("label", n), "description": p.get("description", "")} for n, p in presets(self.s).items()],
                "sessions": [{"id": x["id"], "title": x.get("title"), "updated": x.get("updated"), "cwd": x.get("cwd")} for x in self.s.sessions()[:100]]}

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
                a.session["ws"] = self.ws.for_dir(os.getcwd())
            if path == "/goal":
                threading.Thread(target=a.goal, args=(body["text"],), daemon=True).start()
            else:
                threading.Thread(target=a.run, args=(body["text"], body.get("images") or ()), daemon=True).start()
            return {"ok": True}
        if path == "/undo":
            r = a.undo()
            if not r["ok"]:
                raise ValueError(r["text"])
            return r
        if path == "/upload":  # attachments from the browser land in the project folder
            d = Path(os.getcwd()) / "uploads"
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
        if path == "/new":
            a.session = new_session()
            a.ctx_used = 0
            return {"ok": True}
        if path == "/load":
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
            os.chdir(d)
            a.session = new_session(d)
            a.ctx_used = 0
            return self.state()
        if path == "/model":
            self.s.config["model"] = body["name"]
            self.s.save()
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
        return 0
    if unicodedata.combining(ch):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in "WF" else 1


def swidth(s):
    return sum(cw(c) for c in s)


class Line:
    """A display line: list of (text, style) segments."""
    __slots__ = ("segs",)

    def __init__(self, segs=None):
        self.segs = segs or []

    def add(self, text, style=""):
        if text:
            self.segs.append((text, style))
        return self

    def width(self):
        return sum(swidth(t) for t, _ in self.segs)

    def render(self, width, base=""):
        out, w = [], 0
        for t, st in self.segs:
            if w >= width:
                break
            buf = []
            for c in t:
                cwid = cw(c)
                if w + cwid > width:
                    break
                buf.append(c)
                w += cwid
            out.append(base + st + "".join(buf) + RESET)
        return "".join(out) + base + " " * max(0, width - w) + RESET


def wrap_segs(segs, width, indent=0, first_prefix=None, prefix=None):
    """Word-wrap styled segments into Lines."""
    lines, cur, cw_ = [], Line(), 0
    if first_prefix:
        for t, s in first_prefix:
            cur.add(t, s)
        cw_ = cur.width()
    else:
        cur.add(" " * indent)
        cw_ = indent

    def newline():
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

    for text, st in segs:
        for tok in re.findall(r"\S+|\s+", text):
            tw = swidth(tok)
            if tok.isspace():
                if cw_ + tw <= width:
                    cur.add(tok, st)
                    cw_ += tw
                else:
                    newline()
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


class Term:
    def __init__(self):
        self.q = queue.Queue()
        self.old = None
        self.mouse = True

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
            k.SetConsoleMode(h_out, mode.value | 0x0004)
            k.GetConsoleMode(h_in, ctypes.byref(mode))
            self.old = mode.value
            # VT input for keys. Do NOT add ENABLE_MOUSE_INPUT / drop QuickEdit here: that garbled typing in cmd.exe.
            # The wheel arrives as VT mouse sequences (?1000h/?1006h below) where the console supports them.
            k.SetConsoleMode(h_in, (mode.value | 0x0200) & ~(0x0001 | 0x0002 | 0x0004))
            k.SetConsoleOutputCP(65001)
        else:
            import termios, tty
            self.fd = sys.stdin.fileno()
            self.old = termios.tcgetattr(self.fd)
            tty.setraw(self.fd)
        # ?1007l: no "alternate scroll" - otherwise the wheel is sent as up/down arrows and pages through input history
        self.write(f"{CSI}?1049h{CSI}?1007l{CSI}?25l{CSI}?2004h" + (f"{CSI}?1000h{CSI}?1006h" if mouse else ""))
        threading.Thread(target=self._reader, daemon=True).start()

    def set_mouse(self, on):
        self.mouse = on
        self.write(f"{CSI}?1000h{CSI}?1006h" if on else f"{CSI}?1000l{CSI}?1006l")

    def stop(self):
        self.write(f"{CSI}?1000l{CSI}?1006l{CSI}?2004l{CSI}?25h{CSI}?1007h{CSI}?1049l")
        if WIN:
            import ctypes
            k = ctypes.windll.kernel32
            if self.old is not None:
                k.SetConsoleMode(k.GetStdHandle(-10), self.old)
        elif self.old:
            import termios
            termios.tcsetattr(self.fd, termios.TCSADRAIN, self.old)

    def write(self, s):
        sys.stdout.write(s)
        sys.stdout.flush()

    def _readch(self):
        if WIN:
            import msvcrt
            c = msvcrt.getwch()
            if c in ("\x00", "\xe0"):
                c2 = msvcrt.getwch()
                return {"H": "\x1b[A", "P": "\x1b[B", "K": "\x1b[D", "M": "\x1b[C", "G": "\x1b[H", "O": "\x1b[F", "S": "\x1b[3~",
                        "I": "\x1b[5~", "Q": "\x1b[6~"}.get(c2, "")
            return c
        b = os.read(self.fd, 4096)
        return b.decode("utf-8", "replace")

    def _reader(self):
        buf = ""
        while True:
            try:
                buf += self._readch()
            except Exception:
                return
            if not WIN:
                import select
                while select.select([self.fd], [], [], 0.01)[0]:
                    buf += os.read(self.fd, 4096).decode("utf-8", "replace")
            keys, buf = parse_keys(buf)
            for k in keys:
                self.q.put(k)


KEYMAP = {"[A": "up", "[B": "down", "[C": "right", "[D": "left", "[H": "home", "[F": "end", "[1~": "home", "[4~": "end",
          "[3~": "delete", "[5~": "pgup", "[6~": "pgdn", "OA": "up", "OB": "down", "OC": "right", "OD": "left", "OH": "home",
          "OF": "end", "[1;5A": "ctrl-up", "[1;5B": "ctrl-down", "[1;5C": "ctrl-right", "[1;5D": "ctrl-left", "[1;3C": "ctrl-right",
          "[1;3D": "ctrl-left", "[Z": "shift-tab", "[13;2u": "newline", "[27;2;13~": "newline"}


def parse_keys(buf):
    keys, i = [], 0
    while i < len(buf):
        c = buf[i]
        if buf.startswith("\x1b[200~", i):
            end = buf.find("\x1b[201~", i)
            if end < 0:
                return keys, buf[i:]
            keys.append(("paste", buf[i + 6:end]))
            i = end + 6
            continue
        if c == "\x1b":
            if i + 1 >= len(buf):
                keys.append("esc")
                i += 1
                continue
            m = re.match(r"\x1b\[<(\d+);(\d+);(\d+)([mM])", buf[i:])
            if m:
                b = int(m.group(1))
                if b & 64 and not b & 32:  # wheel, with or without shift/ctrl/alt
                    keys.append("wheel-up" if b & 3 == 0 else "wheel-down" if b & 3 == 1 else "unknown")
                i += m.end()
                continue
            m = re.match(r"\x1b(\[[\d;]*[~A-Za-z]|O[A-Z])", buf[i:])
            if m:
                keys.append(KEYMAP.get(m.group(1), "unknown"))
                i += m.end()
                continue
            nxt = buf[i + 1]
            if nxt in "\r\n":
                keys.append("newline")
            elif nxt == "\x7f":
                keys.append("ctrl-w")
            elif nxt == "b":
                keys.append("ctrl-left")
            elif nxt == "f":
                keys.append("ctrl-right")
            elif nxt == "\x1b":
                keys.append("esc")
                i += 1
                continue
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
                out.append(Line().add(" " * indent).add(f" {lang or 'code'} ".ljust(width - indent), rgb(TH["dim"]) + rgb(TH["code_bg"], True)))
            else:
                in_code = False
                out.append(Line().add(" " * indent).add(" " * (width - indent), rgb(TH["code_bg"], True)))
            continue
        if in_code:
            segs = hl_code(raw.replace("\t", "    "), lang)
            ls = wrap_segs(segs, width, first_prefix=[(" " * indent, ""), (" ", rgb(TH["code_bg"], True))],
                           prefix=[(" " * indent, ""), (" ", rgb(TH["code_bg"], True))])
            for l in ls:
                pad = width - l.width()
                if pad > 0:
                    l.add(" " * pad, rgb(TH["code_bg"], True))
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
            out.extend(wrap_segs(inline_md(m.group(3), base), width, first_prefix=pre, prefix=[(" " * (ind + swidth(bullet) + 1), "")]))
            continue
        if re.match(r"^\s*(---+|\*\*\*+)\s*$", s):
            out.append(Line().add(" " * indent).add("─" * (width - indent * 2), rgb(TH["faint"])))
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


# ───────────────────────────── TUI ─────────────────────────────

COMMANDS = [
    ("help", "show commands & keys"), ("agent", "switch mode: code, design, plan, review…"), ("init", "create AGENTS.md for this project"), ("model", "switch model"), ("models", "add / edit / sync models"),
    ("provider", "add / edit API providers"), ("effort", "reasoning effort: none…xhigh"), ("prompt", "edit your system prompt"),
    ("skills", "list / toggle / create skills"), ("plugins", "extensions that modify me"), ("secret", "set env secrets for skills"), ("new", "new session"),
    ("sessions", "resume a past session"), ("workspace", "sync this project folder to the cloud"), ("clear", "clear screen + new session"), ("copy", "copy last answer"), ("compact", "summarize history to free context"), ("autocompact", "auto-compact at N% of context (off)"),
    ("goal", "work autonomously until done+verified or blocked"), ("undo", "revert the files of the last turn"),
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
        if b._cache and b._cache[0] == width and b.done and b.kind not in ("tool", "skill"):
            return b._cache[1]
        if b._cache and b._cache[0] == width and b.kind in ("user", "info", "stats", "welcome", "assistant") and b.kind != "assistant":
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
            out.append(Line().add("  ").add("◆ " + ", ".join(ctxf) + " loaded into this session", rgb(TH["ok"])) if ctxf else
                       Line().add("  ").add("◇ no AGENTS.md — run /init so I remember this project", rgb(TH["warn"])))
            out.append(Line().add("  ").add("/ ", rgb(TH["accent"])).add("commands   ", dim).add("@ ", rgb(TH["accent"])).add("files   ", dim)
                       .add("esc ", rgb(TH["accent"])).add("interrupt   ", dim).add("alt+enter ", rgb(TH["accent"])).add("newline", dim))
            out.append(Line())
            return out
        if b.kind == "user":
            bar = rgb(TH["user"])
            txt = b.text
            ls = []
            for i, para in enumerate(txt.split("\n")):
                ls.extend(wrap_segs([(para, fg)], W - 2, first_prefix=[("› " if i == 0 else "  ", bar)], prefix=[("  ", "")]))
            bg = rgb(TH["sel"], True)
            for l in ls:
                l.segs = [(t, bg + s) for t, s in l.segs]
                pad = W - l.width()
                if pad > 0:
                    l.add(" " * pad, bg)
            return [Line()] + ls + [Line()]
        if b.kind == "assistant":
            return render_md(b.text.strip("\n"), W - 2, 2)
        if b.kind == "reason":
            col = rgb(TH["dim"]) + CSI + "3m"
            if b.done:
                head = Line().add("  ∴ ", rgb(TH["faint"])).add(f"thought for {dur(b.elapsed or 0)}", col)
                if b.collapsed is False and self.s.config["settings"].get("show_thinking"):
                    return [head] + wrap_segs([(b.text.strip(), col)], W - 4, 4) + [Line()]
                return [head]
            body = b.text.strip().split("\n")[-4:]
            ls = [Line().add("  ∴ ", rgb(TH["accent"])).add("thinking…", self.shimmer_style(0))]
            for para in body:
                ls.extend(wrap_segs([(para, col)], W - 4, 4))
            return ls[-6:]
        if b.kind == "tool":
            return self._render_tool(b, W)
        if b.kind == "skill":
            return self._render_skill(b, W)
        if b.kind == "stats":
            return [Line().add("  ").add("✓ ", rgb(TH["ok"])).add(b.text, dim), Line()]
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
        if b.done:
            head.add(f"  {dur(b.elapsed)}", rgb(TH["faint"]))
        out = [head]
        if b.data.get("diff"):
            for dl in b.data["diff"].split("\n")[: (8 if b.collapsed else 40)]:
                col = TH["add"] if dl.startswith("+") else TH["del"] if dl.startswith("-") else TH["dim"]
                out.append(Line().add("    │ ", rgb(TH["faint"])).add(dl[: W - 8], rgb(col)))
        elif b.done:
            o = (b.data.get("output") or "").rstrip()
            lines = o.split("\n")
            show = 1 if b.ok and name in ("read", "ls", "grep") else 3 if b.ok else 6
            if b.collapsed:
                for l in lines[:show]:
                    out.append(Line().add("    │ ", rgb(TH["faint"])).add(l[: W - 8], rgb(TH["dim"] if b.ok else TH["err"])))
                if len(lines) > show:
                    out.append(Line().add("    └ ", rgb(TH["faint"])).add(f"{len(lines) - show} more lines", rgb(TH["faint"])))
            else:
                for l in lines[:200]:
                    out.append(Line().add("    │ ", rgb(TH["faint"])).add(l[: W - 8], dim))
        elif name == "bash":
            out.append(Line().add("    └ ", rgb(TH["faint"])).add(f"running {dur(time.time() - b.t0)}", dim))
        elif b.status or b.progress:
            if b.status:
                out.append(Line().add("    └ ", rgb(TH["faint"])).add(b.status[: W - 10], dim))
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
            return [Line().add("  ").add(mark[0] + " ", mark[1]).add(f"{icon} ", rgb(color)).add(name, rgb(color) + CSI + "1m")
                    .add(" · ", rgb(TH["dim"])).add(act, rgb(TH["fg"])).add(f"  {t}", rgb(TH["faint"]))
                    .add(("  " + summary) if summary else "", rgb(TH["dim"]))] + \
                [Line().add("    │ ", rgb(color)).add(pl[: W - 8], rgb(TH["fg"])) for pl in b.data.get("panel", [])[:40]]
        inner = W - 6
        title = f" {icon} {name} · {act} "
        top = Line().add("  ╭─", bc).add(title, rgb(color) + CSI + "1m")
        top.add("─" * max(1, inner - swidth(title) - swidth(t) - 2), bc).add(f" {t} ", rgb(TH["dim"])).add("─╮", bc)
        out = [top]

        def row(segs):
            l = Line().add("  │ ", bc)
            for s in segs:
                l.add(*s)
            pad = W - 2 - l.width()
            if pad > 0:
                l.add(" " * pad)
            l.add("│", bc)
            return l

        args = "  ".join(f"{k}={json.dumps(v, ensure_ascii=False)}" for k, v in (a.get("args") or {}).items())
        if args:
            out.append(row([(args[: inner - 2], rgb(TH["dim"]))]))
        if b.status:
            out.append(row([(SPIN[self.frame % len(SPIN)] + " ", rgb(color)), (b.status[: inner - 4], rgb(TH["fg"]))]))
        p = b.progress
        if p:
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
            out.append(row([(SPIN[self.frame % len(SPIN)] + " starting…", rgb(TH["dim"]))]))
        out.append(Line().add("  ╰" + "─" * (W - 5) + "╯", bc))
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
        if not items or (all(i["s"] == "done" for i in items) and not self.agent.busy):
            return []
        done = sum(1 for i in items if i["s"] == "done")
        head = Line().add(" ").add("todo ", rgb(TH["accent"]) + CSI + "1m").add(f"{done}/{len(items)}", rgb(TH["dim"]))
        g = self.agent.session.get("goal") or {}
        if g.get("status") == "working":
            head.add("  ◎ goal: " + g["text"][: max(10, W - 30)], rgb(TH["accent2"]))
        cap = max(2, min(7, H // 4))
        cur = next((k for k, i in enumerate(items) if i["s"] != "done"), len(items))
        lo = max(0, min(cur - 1, len(items) - cap))
        out = [head]
        for i in items[lo:lo + cap]:
            sym, col = {"done": ("✓", TH["ok"]), "doing": ("▸", TH["accent"])}.get(i["s"], ("○", TH["faint"]))
            out.append(Line().add("  " + sym + " ", rgb(col)).add(i["t"][: W - 6], rgb(TH["dim"] if i["s"] == "done" else TH["fg"])))
        if len(items) > lo + cap:
            out.append(Line().add(f"    … {len(items) - lo - cap} more", rgb(TH["faint"])))
        return out

    def status_line(self, W):
        if W < 90:
            return self.status_compact(W)
        m = self.s.model()
        dim, faint = rgb(TH["dim"]), rgb(TH["faint"])
        L = Line().add(" ")
        if m:
            L.add("● ", rgb(TH["accent2"])).add(m["name"], rgb(TH["fg"]))
            if m.get("effort") and m["effort"] != "none":
                L.add(f" · {m['effort']}", dim)
        else:
            L.add("no model — /provider", rgb(TH["warn"]))
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
            right.add("◇ no AGENTS.md (/init)  ", rgb(TH["faint"]))
        for segs in PLUGINS.collect("status", self):
            for t, c in segs:
                right.add(t, rgb(TH.get(c, c) if not str(c).startswith("#") else c))
            right.add("  ")
        pm = perm_mode(self.s.config["settings"])
        if pm != "ask":
            right.add(("full " if pm == "full" else "read-only "), rgb(TH["warn"] if pm == "full" else TH["accent2"]))
        cs = self.cloud.status
        if self.cloud.online:
            sym = {"synced": ("≈ ✓", TH["ok"]), "syncing": ("≈ " + SPIN[self.frame % len(SPIN)] + " ", TH["accent"]),
                   "error": ("≈ !", TH["err"])}.get(cs, ("≈", TH["dim"]))
            right.add(sym[0] + " ", rgb(sym[1])).add(self.s.auth.get("user", ""), dim)
        else:
            right.add("≈ offline", faint)
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
        m = self.s.model()
        dim, faint = rgb(TH["dim"]), rgb(TH["faint"])
        L = Line().add(" ")
        if m:
            L.add("● ", rgb(TH["accent2"])).add(m["name"][: max(6, W // 3)], rgb(TH["fg"]))
        else:
            L.add("no model", rgb(TH["warn"]))
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
            in_rows = in_rows[-8:] if len(in_rows) > 8 else in_rows
            cr = min(cr, len(in_rows) - 1)
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
                    l.add("ask anything, / for commands" if not busy else "queue a message…", rgb(TH["faint"]))
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
                act.segs.extend(self.shimmer(word + "…"))
                act.add(f"  {dur(el)}", rgb(TH["dim"]))
                if any(self.turn_tokens):
                    act.add(f"  ↑{human(self.turn_tokens[0])} ↓{human(self.turn_tokens[1])}", rgb(TH["dim"]))
                act.add("   esc to interrupt", rgb(TH["faint"]))
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
            avail = H - len(bottom) - len(pop)
            # transcript
            lines = []
            for b in self.blocks:
                lines.extend(self.block_lines(b, W - 1))
            maxscroll = max(0, len(lines) - avail)
            self.scroll = min(self.scroll, maxscroll)
            end = len(lines) - self.scroll
            view = lines[max(0, end - avail):end]
            view = [Line()] * (avail - len(view)) + view
            if self.scroll:
                view[0] = Line().add(f" ↑ {self.scroll} more lines below · pgdn ", rgb(TH["accent"]))
            screen = view + pop + bottom
            out = []
            # repaint only the rows that changed (a full repaint every frame flickers badly in the Windows console)
            rows = [l.render(W, bgc) for l in screen[:H]]
            prev = getattr(self, "_prev_rows", None)
            full = prev is None or len(prev) != len(rows) or getattr(self, "_prev_wh", None) != (W, H) or self.modal or getattr(self, "_prev_modal", False)
            for i, r in enumerate(rows):
                if full or prev[i] != r:
                    out.append(f"{CSI}{i + 1};1H" + r)
            self._prev_rows, self._prev_wh, self._prev_modal = rows, (W, H), bool(self.modal)
            out.insert(0, CSI + "?25l")  # hide the cursor while painting
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
            self.term.write("".join(out))
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
            rows.append(Line().add(" ⌕ ", rgb(TH["accent"])).add(m["filter"] or "type to filter", rgb(TH["fg"] if m["filter"] else TH["faint"])))
            rows.append(Line())
            maxn = max(3, H - 12)
            start = max(0, min(m["sel"] - maxn // 2, len(items) - maxn))
            for i, (v, lab, det) in enumerate(items[start:start + maxn]):
                selected = start + i == m["sel"]
                st = rgb(TH["sel"], True) if selected else ""
                l = Line().add((" ▸ " if selected else "   "), st + rgb(TH["accent"]))
                l.add(lab, st + rgb(TH["fg"]) + (CSI + "1m" if selected else ""))
                if det:
                    l.add("  " + det, st + rgb(TH["dim"]))
                if selected:
                    l.add(" " * max(0, mw - 2 - l.width()), st)
                rows.append(l)
            if not items:
                rows.append(Line().add("   no matches", rgb(TH["dim"])))
            rows.append(Line())
            rows.append(Line().add(" ↑↓ select · enter confirm · esc cancel" + (" · " + m["hint"] if m.get("hint") else ""), rgb(TH["faint"])))
        elif m["kind"] == "confirm":
            rows.append(Line().add(" ⚠ allow ", rgb(TH["warn"]) + CSI + "1m").add(title[: mw - 12], rgb(TH["fg"])))
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
            rows.append(Line().add(" y/n · ↑↓ enter · esc deny", rgb(TH["faint"])))
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
            rows.append(Line().add(" enter " + ("newline · ctrl+s save" if m["multiline"] else "confirm") + " · esc cancel", rgb(TH["faint"])))
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


# ───── TUI: keyboard + suggestions (mixin) ─────


class TUIInput:
    # ─── input handling ───
    def handle_key(self, k):
        if self.modal:
            return self.modal_key(k)
        if PLUGINS.emit("key", self, k):
            return
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

    def submit(self):
        text = self.input.strip()
        self.input, self.cur, self.sugg, self.hpos = "", 0, [], None
        if not text:
            return
        if not self.hist or self.hist[-1] != text:
            self.hist.append(text)
            try:
                (self.s.home / "history").write_text("\0".join(self.hist[-500:]), "utf-8")
            except OSError:
                pass
        self.scroll = 0
        if text.startswith("/"):
            cmd, _, arg = text[1:].partition(" ")
            self.bg(self.command, cmd.strip().lower(), arg.strip())
            return
        if self.agent.busy or self.launching or self.pending:
            self.pending.append(text)
            self.info(f"◌ queued ({len(self.pending)}): {text[:100]}", "dim")
            return
        self.dispatch(text)

    def dispatch(self, text):
        images = []
        for ref in re.findall(r"@(\S+)", text):
            p = Path(ref)
            if p.suffix.lower() in IMAGE_EXT and p.exists():
                images.append(read_any(p)[1])
        self.add("user", text=text)
        self.ensure_ws()
        if not self.s.model():
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
        return COMMANDS + [(n, d) for n, (_, d) in PLUGINS.commands.items()]

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
        txt = "## Commands\n" + "\n".join(f"- `/{c}` {d}" for c, d in COMMANDS)
        txt += ("\n\n## Keys\n- `enter` send · `alt+enter` / `ctrl+j` / `\\`+enter newline\n- `esc` interrupt · `ctrl+c` clear/quit"
                "\n- `↑↓` history · `pgup/pgdn` / wheel scroll · `ctrl+o` expand last tool\n- `@path` attach file (images go to the model)"
                "\n- `tab` accept suggestion")
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
        m = self.s.model()
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
            m = {"kind": "pick", "title": "Theme (↑↓ previews)", "items": items, "sel": next((i for i, x in enumerate(items) if x[0] == cur), 0),
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
        if self.cloud.online:
            try:
                self.cloud.req("POST", f"/api/market/{mid}/install", {})
            except CloudError:
                pass
        self.s.save()
        self.toast(f"installed {kind} {x['name']}", "ok")
        return x["name"]

    def cmd_new(self, arg):
        if self.agent.busy:
            return self.toast("busy", "warn")
        self.agent.session = new_session()
        self.agent.ctx_used = 0
        self.info("─── new session ───")

    def cmd_clear(self, arg):
        if self.agent.busy:
            return self.toast("busy", "warn")
        with self.lock:
            self.blocks = []
        self.agent.session = new_session()
        self.agent.ctx_used = 0
        self.welcome()

    def cmd_copy(self, arg):
        for b in reversed(self.blocks):
            if b.kind == "assistant":
                self.term.write(f"{ESC}]52;c;{base64.b64encode(b.text.encode()).decode()}\a")
                return self.toast("copied last answer to clipboard", "ok")
        self.toast("nothing to copy")

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

    def cmd_todo(self, arg):
        items = self.agent.session.get("todo") or []
        if not items:
            return self.toast("no todo list in this session")
        self.add("assistant", text="\n".join(("- [x] " if i["s"] == "done" else "- [ ] " if i["s"] == "todo" else "- [>] ") + i["t"] for i in items)).done = True

    def cmd_cost(self, arg):
        st = self.s.config["settings"]
        u = self.agent.session.get("usage", {})
        m = self.s.model() or {}
        pin, pout, pc = model_price(m)
        if arg:
            k, _, v = arg.partition(" ")
            if k in ("turn", "day"):
                st["max_turn_cost" if k == "turn" else "max_day_cost"] = float(v or 0)
                self.s.save()
                return self.toast(f"{k} limit {money(float(v or 0)) if float(v or 0) else 'off'}", "ok")
        days = jload(self.s.home / "usage.json", {}) or {}
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
                                        + (f" · {m['effort']}" if m.get("effort") else "")) for m in ms], current=self.s.config.get("model"))
        if name:
            self.s.config["model"] = name
            self.s.save()
            self.toast(f"model → {name}", "ok")

    def cmd_effort(self, arg):
        m = self.s.model()
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
        added = 0
        while True:
            items = [("*", "✓ done", f"{added} added")] + [(r["id"], ("✓ " if (pname, r["id"]) in have else "  ") + r["id"],
                                                             human(r["context"]) + " ctx" if r.get("context") else "") for r in remote]
            v = self.pick(f"Add models from {pname} ({len(remote)})", items, hint="enter toggles · type to filter")
            if v in (None, "*"):
                break
            if (pname, v) in have:
                self.s.config["models"] = [m for m in self.s.config["models"] if not (m.get("provider") == pname and m["id"] == v)]
                have.discard((pname, v))
                added -= 1
            else:
                r = next(x for x in remote if x["id"] == v)
                nm = v.split("/")[-1]
                if any(m["name"] == nm for m in self.s.config["models"]):
                    nm = f"{pname}/{nm}"
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
            v = self.pick("Models", items, current=next((i for i, m in enumerate(ms) if m["name"] == self.s.config.get("model")), None))
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
                    ms.append({"name": mid.split("/")[-1], "id": mid, "provider": p, "api": "chat", "context": 128000, "max_out": "",
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
                self.s.config["model"] = m["name"]
                self.s.save()
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
            if f == "name" and self.s.config.get("model") == m["name"]:
                self.s.config["model"] = v
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
                        b = self.add("tool", name=tc["function"]["name"], args=a, label=self.agent.tools.preview(tc["function"]["name"], a))
                        b.done, b.ok, b.collapsed = True, True, True

    def cmd_sessions(self, arg):
        ss = self.s.sessions()
        if not ss:
            return self.toast("no sessions yet")
        sid = self.pick("Resume session", [(s["id"], s.get("title") or "(untitled)",
                                            time.strftime("%d %b %H:%M", time.localtime(s.get("updated", 0))) + f" · {len(s.get('messages', []))} msgs · "
                                            + s.get("cwd", "")) for s in ss])
        if not sid or self.agent.busy:
            return
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
            items = [("yolo", f"auto-approve tools   {'on' if st.get('yolo') else 'off'}", ""),
                     ("mouse", f"mouse wheel scroll   {'on' if st.get('mouse', True) else 'off'}", "off = native text selection"),
                     ("show_thinking", f"show thinking        {'on' if st.get('show_thinking') else 'off'}", ""),
                     ("home", "data dir", str(self.s.home))]
            v = self.pick("Settings", items)
            if v is None or v == "home":
                return
            st[v] = not st.get(v, v == "mouse")
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
        target.write_text(src, "utf-8")
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
        self.agent = Agent(store, self.on_event, self.confirm_tool, self.ask_text)
        self.tools_live = {}
        self.cur_text = None
        self.cur_reason = None
        self.turn_t0 = 0
        self.turn_tokens = (0, 0)
        self.update_avail = None
        self.files_cache = None
        self.notice = ("", 0)
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
        self.notice = (msg, time.time(), color)
        self.redraw()

    def add(self, kind, **kw):
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
                kind = "skill" if e["name"] == "skill" and e["args"].get("action") else "tool"
                b = self.add(kind, name=e["name"], args=e["args"], label=e["label"])
                if kind == "skill":
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
                b = self.tools_live.pop(e["id"], None)
                if b:
                    b.done, b.ok, b.elapsed = True, e["ok"], e["time"]
                    b.data["output"] = e["output"]
                    b.collapsed = True
                    b.touch()
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
                    self.add("info", text=("✓ goal done" if ok else "✗ goal blocked") + (f" — {e['reason']}" if e.get("reason") else ""),
                             color="ok" if ok else "err")
            elif t == "turn_diff":
                files = e.get("files") or []
                if files:
                    stats = []
                    for f in files:
                        add = sum(1 for l in f["diff"].splitlines() if l.startswith("+") and not l.startswith("+++"))
                        rem = sum(1 for l in f["diff"].splitlines() if l.startswith("-") and not l.startswith("---"))
                        stats.append(f"{f['path']} +{add} −{rem}")
                    self.add("info", text="Δ " + " · ".join(stats[:6]) + (f" · +{len(stats) - 6} more" if len(stats) > 6 else "") + "   /undo reverts",
                             color="dim")
            elif t == "undone":
                self.add("info", text="↶ " + e["text"], color="warn")
            elif t == "error":
                self.add("info", text=("⏹ " if e["text"] == "interrupted" else "✗ ") + e["text"], color="warn" if e["text"] == "interrupted" else "err")
            elif t == "turn_end":
                if self.cur_reason and not self.cur_reason.done:
                    self.cur_reason.done, self.cur_reason.elapsed = True, time.time() - self.cur_reason.t0
                self.add("stats", text=f"{dur(e['time'])}  ·  ↑ {human(e['in'])}  ↓ {human(e['out'])}  ·  {human(e['in'] + e['out'])} tokens"
                                       + (f"  ·  {money(e['cost'])}" if e.get("cost") else ""))
                self.cur_text = self.cur_reason = None
                self.sync_ws()
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

    def pick(self, title, items, hint="", current=None):
        """items: list of (value, label, detail). Returns value or None."""
        items = [i if isinstance(i, tuple) else (i, str(i), "") for i in items]
        sel = 0
        for i, it in enumerate(items):
            if current is not None and it[0] == current:
                sel = i
        return self._modal({"kind": "pick", "title": title, "items": items, "sel": sel, "filter": "", "hint": hint})

    def prompt(self, title, default="", secret=False, multiline=False, hint=""):
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
                         "items": [("y", "Yes"), ("a", always), ("n", "No, tell me what to do")]})
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
                    with self.lock:
                        self.handle_key(k)
                    while not self.term.q.empty():
                        with self.lock:
                            self.handle_key(self.term.q.get_nowait())
                    self.dirty = True
                except queue.Empty:
                    pass
                self.drain_pending()
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
                        self.term.write(f"{CSI}H{CSI}2Jrender error: {e}")
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
    if not (args and args[0] in ("-v", "--version", "-h", "--help", "--sync")):
        if store.auth.get("token"):
            Cloud(store).sync()
            store.config = jload(store.cfg_path) or store.config
        PLUGINS.load(store)
    if args and args[0] in ("-v", "--version"):
        return print(f"me {VERSION}")
    if args and args[0] in ("-h", "--help"):
        return print(f"me {VERSION} — minimal cloud-synced agent\n  me              TUI\n  me -p \"task\"     run once (add -y to auto-approve)\n"
                     f"  me web [port]   browser UI on localhost:8080 (--lan to open on your network)\n  me --rpc        JSON-lines agent over stdio (for web/telegram/IDE frontends)\n  me --sync       sync with cloud and exit\n"
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
        Cloud(store).sync() if store.auth.get("token") else None
        return run_rpc(store)
    if args and args[0] in ("-p", "--print"):
        yolo = "-y" in args
        rest = [a for a in args[1:] if a != "-y"]
        prompt = " ".join(rest) or sys.stdin.read()
        c = Cloud(store)
        if store.auth.get("token"):
            c.sync()
        run_headless(store, prompt, yolo)
        if store.auth.get("token"):
            c.sync()
        return
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        return print("me: not a terminal. use: me -p \"task\"")
    tui = TUI(store)
    PLUGINS.tui = tui
    tui.run()


if __name__ == "__main__":
    main()
