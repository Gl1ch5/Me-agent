# ───────────────────────────── tools ─────────────────────────────

TOOLS = [
    {"name": "read", "description": "Read file (text/code/pdf/docx/xlsx/pptx/ipynb/image) or list dir. Lines numbered.",
     "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "offset": {"type": "integer"}, "limit": {"type": "integer"}},
                    "required": ["path"]}},
    {"name": "write", "description": "Create/overwrite file.",
     "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}},
    {"name": "edit", "description": "Replace exact unique text in file (all=true: every match).",
     "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "old": {"type": "string"}, "new": {"type": "string"},
                                                     "all": {"type": "boolean"}}, "required": ["path", "old", "new"]}},
    {"name": "bash", "description": "Run shell command in cwd.",
     "parameters": {"type": "object", "properties": {"cmd": {"type": "string"}, "timeout": {"type": "integer", "description": "sec, default 120"}},
                    "required": ["cmd"]}},
    {"name": "ls", "description": "Glob files, e.g. src/**/*.py.",
     "parameters": {"type": "object", "properties": {"pattern": {"type": "string"}}, "required": ["pattern"]}},
    {"name": "grep", "description": "Regex search in files.",
     "parameters": {"type": "object", "properties": {"pattern": {"type": "string"}, "path": {"type": "string"}, "glob": {"type": "string"}},
                    "required": ["pattern"]}},
    {"name": "web_search", "description": "Search the web; returns answer with sources.",
     "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}},
    {"name": "web_fetch", "description": "Fetch URL as text.",
     "parameters": {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]}},
    {"name": "skill", "description": "Use a skill: no action → its guide+actions; else run action with args.",
     "parameters": {"type": "object", "properties": {"name": {"type": "string"}, "action": {"type": "string"}, "args": {"type": "object"}},
                    "required": ["name"]}},
]
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


def run_shell(cmd, timeout, cancel):
    kw = {} if WIN else {"start_new_session": True}
    proc = subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, **kw)
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
