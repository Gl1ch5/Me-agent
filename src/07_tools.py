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
