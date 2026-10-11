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
    def ask(self, q):
        _emit(t="ask", text=str(q)); return sys.stdin.readline().rstrip("\n")
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
                ans = ask(ev["text"]) if ask else ""
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
