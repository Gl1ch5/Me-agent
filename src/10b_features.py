# ───────────────────────────── repo map ─────────────────────────────
# Compact index of files + top-level symbols, built lazily (map tool) and cached per workspace by file mtime.

MAP_EXT = {".py", ".js", ".mjs", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java", ".kt", ".swift", ".rb", ".php", ".c", ".h", ".cc",
           ".cpp", ".hpp", ".cs", ".scala", ".lua", ".sh", ".vue", ".svelte", ".dart", ".ex", ".css", ".scss", ".html", ".md", ".sql",
           ".gd", ".gdshader", ".zig", ".nim", ".jl", ".hs", ".ml", ".kts"}
SYM_RX = re.compile(r"^(?:export\s+)?(?:default\s+)?(?:pub(?:\(\w+\))?\s+)?(?:async\s+)?(?:abstract\s+)?(?:static\s+)?"
                    r"(?:def|class|class_name|signal|function\*?|func|fn|struct|enum|trait|interface|type|impl|module|object|const|let|var)\s+"
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
    if len(text) <= limit:
        return text
    # too big: the files that matter most first (shallow, with symbols, recently touched), the rest counted per folder
    rank = sorted(lines, key=lambda ln: (ln.split(":")[0].count("/"), ":" not in ln, -fresh[ln.split(":")[0]][0]))
    keep, size = set(), 0
    for ln in rank:
        if size + len(ln) > limit - 600:
            break
        keep.add(ln)
        size += len(ln) + 1
    rest = collections.Counter(os.path.dirname(ln.split(":")[0]) or "." for ln in lines if ln not in keep)
    more = ", ".join(f"{d}/ {n}" for d, n in rest.most_common(12))
    return ("\n".join(ln for ln in lines if ln in keep) + f"\n… {sum(rest.values())} more files ({more}); ls map <folder> for one folder")


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


RM_RX = re.compile(r"(?:^|[;&|(]\s*|\bsudo\s+|\bxargs\s+)rm\s+([^;&|\n]+)")


def rm_targets(cmd, cwd, limit=300, max_bytes=30_000_000):
    """Files an `rm` in this command would delete (globs expanded, -r walks folders), snapshotted first so /undo brings them back."""
    out, size = [], 0
    for m in RM_RX.finditer(cmd):
        try:
            words = shlex.split(m.group(1))
        except ValueError:
            continue
        rec = any(w == "--recursive" or (w.startswith("-") and not w.startswith("--") and ("r" in w or "R" in w)) for w in words)
        for w in words:
            if w.startswith("-"):
                continue
            for p in glob.glob(os.path.join(cwd, os.path.expanduser(w))):
                if os.path.isfile(p):
                    files = [p]
                elif rec and os.path.isdir(p):
                    files = [os.path.join(r, f) for r, ds, fs in os.walk(p) if not ds.__setitem__(slice(None), [d for d in ds if d not in (".git", "node_modules")]) for f in fs]
                else:
                    files = []
                for f in files:
                    try:
                        size += os.path.getsize(f)
                    except OSError:
                        continue
                    if len(out) >= limit or size > max_bytes:
                        return out
                    out.append(f)
    return out


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
DEFAULT_ALLOW = ["git status*", "git diff*", "git log*", "git show*", "ls", "ls *", "pwd", "cat *", "head *", "tail *", "wc *", "which *",
                 "python3 -m pytest*", "pytest*", "npm test*", "npm run test*", "npm run build*", "npm run lint*", "go test*", "cargo test*",
                 "node --check *", "python3 -m py_compile *"]


def perm_mode(settings):
    return "full" if settings.get("yolo") else settings.get("perm") or "ask"


def cmd_allowed(cmd, patterns):
    cmd = cmd.strip()
    if not cmd or re.search(r"[;&|`<>\n]|\$\(", cmd):  # chained / redirected commands always ask
        return False
    if re.search(r"(?:^|\s)(?:--output|--out-file)|(?:^|\s)['\"]?(?:~|/|\\|\.\.|[A-Za-z]:)", cmd):
        return False  # an allow-listed command still asks when it names a path outside the project or writes a file
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
                                         cwd=getattr(self, "cwd", None) if os.path.isdir(getattr(self, "cwd", None) or "") else None, **NO_WINDOW)
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


def mcp_client(store, name, cwd=None):
    """One client per server and project folder: a stdio server started for one chat's folder must not serve another's."""
    spec = (store.config.get("mcp") or {}).get(name)
    if not spec:
        raise KeyError(name)
    key = name if spec.get("url") or not cwd else (name, os.path.normcase(os.path.abspath(cwd)))
    c = MCP_CLIENTS.get(key)
    if not c or c.spec != spec:
        if c:
            c.close()
        c = MCP_CLIENTS[key] = MCPClient(name, spec)
        c.cwd = cwd
    return c


def mcp_describe(tools):
    out = []
    for t in tools:
        props = (t.get("inputSchema") or {}).get("properties", {})
        req = set((t.get("inputSchema") or {}).get("required", []))
        args = ", ".join(f"{k}{'' if k in req else '?'}: {v.get('type', 'any')}" for k, v in props.items())
        out.append(f"- {t['name']}({args}): {(t.get('description') or '').strip().splitlines()[0][:160] if t.get('description') else ''}")
    return "\n".join(out) or "(no tools)"
