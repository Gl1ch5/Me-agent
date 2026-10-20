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
LATER_TOOL = {"name": "later", "description": "Come back to this chat later, even with this device off: text arrives here as your task "
              "after `minutes`, or on a `cron` schedule (5 fields, the user's local time; tz = UTC offset in hours if they said). "
              "new=true: a fresh chat in this project instead (no old context). No text: list; cancel=id removes one.",
              "parameters": _obj({"text": "string", "minutes": "number", "cron": "string", "tz": "number", "new": "boolean", "cancel": "string"}, [])}
PUBLISH_TOOL = {"name": "publish", "description": "Put a static site online at a public link: path = folder with index.html (the built "
                "output if it has a build step). The same name replaces the site.", "parameters": _obj({"path": "string", "name": "string"}, ["path", "name"])}
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
    rc = proc.returncode
    hint = "; pkill -f also matched this command's own shell - write the pattern as '[v]ite' so it does not" if rc in (-15, -9, 143, 137) and re.search(r"\bp(kill|grep)\b[^|;&]*-f", cmd) else ""
    return rc == 0, _long_out(buf.text()) + (f"\n[exit {rc}{hint}]" if rc else "")


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
