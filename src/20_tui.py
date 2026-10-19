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
        self.agent.take_queued = self.take_pending
        self.tools_live = {}
        self.cur_text = None
        self.cur_reason = None
        self.turn_t0 = 0
        self.turn_tokens = (0, 0)
        self.update_avail = None
        self.files_cache = None
        self.notice = ("", 0)
        self.sel = None  # mouse selection in the transcript: {"a": (line, cell), "b": (line, cell), "drag": bool}
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
                self.add("info", text=f"↻ {why} — retrying" + (f" in {e['delay']:.0f}s" if e.get("delay") else "")
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
