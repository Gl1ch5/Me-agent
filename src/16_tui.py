# ───────────────────────────── TUI ─────────────────────────────

COMMANDS = [
    ("help", "show commands & keys"), ("model", "switch model"), ("models", "add / edit / sync models"),
    ("provider", "add / edit API providers"), ("effort", "reasoning effort: none…xhigh"), ("prompt", "edit your system prompt"),
    ("skills", "list / toggle / create skills"), ("plugins", "extensions that modify me"), ("secret", "set env secrets for skills"), ("new", "new session"),
    ("sessions", "resume a past session"), ("workspace", "sync this project folder to the cloud"), ("clear", "clear screen + new session"), ("copy", "copy last answer"),
    ("yolo", "toggle auto-approve tools"), ("sync", "sync with cloud now"), ("login", "log in / register"),
    ("2fa", "authenticator (TOTP) on/off"), ("logout", "log out of cloud"), ("config", "settings"), ("update", "update me"),
    ("exit", "quit"),
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


class TUI:
    def __init__(self, store):
        self.s = store
        self.cloud = Cloud(store)
        self.ws = Workspaces(store, self.cloud)
        self.term = Term()
        self.blocks = []
        self.input, self.cur = "", 0
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
        self._sync_timer = threading.Timer(2.0, lambda: self.cloud.sync_bg(lambda n: self.redraw()))
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
            elif t == "usage":
                self.turn_tokens = (e["in"], e["out"])
            elif t == "error":
                self.add("info", text=("⏹ " if e["text"] == "interrupted" else "✗ ") + e["text"], color="warn" if e["text"] == "interrupted" else "err")
            elif t == "turn_end":
                if self.cur_reason and not self.cur_reason.done:
                    self.cur_reason.done, self.cur_reason.elapsed = True, time.time() - self.cur_reason.t0
                self.add("stats", text=f"{dur(e['time'])}  ·  ↑ {human(e['in'])}  ↓ {human(e['out'])}  ·  {human(e['in'] + e['out'])} tokens")
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
        v = self._modal({"kind": "confirm", "title": f"{name}  {label}", "detail": detail, "sel": 0,
                         "items": [("y", "Yes"), ("a", "Yes, don't ask again (yolo)"), ("n", "No, tell me what to do")]})
        if v == "a":
            self.s.config["settings"]["yolo"] = True
            self.s.save()
        return v in ("y", "a")

    def ask_text(self, q):
        return self.prompt(q) or ""

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
        lbl = label if swidth(label) <= room else label[: max(0, room - 1)] + "…"
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
            border = mix(color, "#ffffff", pulse * 0.25)
        bc = rgb(border)
        icon = b.data.get("icon", "◆")
        t = dur(b.elapsed if b.done else time.time() - b.t0)
        if b.collapsed and b.done:
            mark = ("✓", rgb(TH["ok"])) if b.ok else ("✗", rgb(TH["err"]))
            summary = (b.data.get("output") or "").strip().split("\n")[0][: W - 40] if b.ok else (b.data.get("output") or "").split("\n")[0][: W - 40]
            return [Line().add("  ").add(mark[0] + " ", mark[1]).add(f"{icon} ", rgb(color)).add(name, rgb(color) + CSI + "1m")
                    .add(" · ", rgb(TH["dim"])).add(act, rgb(TH["fg"])).add(f"  {t}", rgb(TH["faint"]))
                    .add(("  " + summary) if summary else "", rgb(TH["dim"]))]
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
            segs.append((ch, rgb(mix(TH["dim"], "#ffffff", t))))
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

    def status_line(self, W):
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
        right = Line()
        for segs in PLUGINS.collect("status", self):
            for t, c in segs:
                right.add(t, rgb(TH.get(c, c) if not str(c).startswith("#") else c))
            right.add("  ")
        if self.s.config["settings"].get("yolo"):
            right.add("yolo ", rgb(TH["warn"]))
        cs = self.cloud.status
        if self.cloud.online:
            sym = {"synced": ("☁ ✓", TH["ok"]), "syncing": ("☁ " + SPIN[self.frame % len(SPIN)], TH["accent"]),
                   "error": ("☁ !", TH["err"])}.get(cs, ("☁", TH["dim"]))
            right.add(sym[0] + " ", rgb(sym[1])).add(self.s.auth.get("user", ""), dim)
        else:
            right.add("☁ offline", faint)
        if self.update_avail:
            right.add(f"  ↑ v{self.update_avail} /update", rgb(TH["accent"]))
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
            in_rows, (cr, cc) = self.input_lines(W - 2)
            in_rows = in_rows[-8:] if len(in_rows) > 8 else in_rows
            cr = min(cr, len(in_rows) - 1)
            busy = self.agent.busy
            box = []
            bcol = rgb(TH["accent"] if not busy else TH["faint"])
            box.append(Line().add("╭" + "─" * (W - 2) + "╮", bcol))
            for i, r in enumerate(in_rows):
                l = Line().add("│", bcol).add(" › " if i == 0 else "   ", rgb(TH["accent"]))
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
            bottom = [act] + box + [status]
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
            out = [CSI + "H"]
            for i, l in enumerate(screen[:H]):
                out.append(f"{CSI}{i + 1};1H" + l.render(W, bgc))
            # modal overlay
            if self.modal:
                out.append(self._draw_modal(W, H, bgc))
            # cursor
            if self.modal and self.modal["kind"] == "input":
                out.append(f"{CSI}?25h")
            elif not self.modal:
                row = H - 1 - len(box) + 1 + cr + 1
                col = 1 + 3 + cc + 1
                out.append(f"{CSI}{row};{col}H{CSI}?25h")
            else:
                out.append(f"{CSI}?25l")
            self.term.write("".join(out))
            self.dirty = False

    def _draw_modal(self, W, H, bgc):
        m = self.modal
        mw = min(W - 4, 90)
        x = (W - mw) // 2 + 1
        mbg = rgb("#15171e", True)
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
            elif k == "up":
                m["sel"] = (m["sel"] - 1) % max(1, len(items))
            elif k == "down":
                m["sel"] = (m["sel"] + 1) % max(1, len(items))
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
        if self.agent.busy:
            self.toast("still working — esc to interrupt", "warn")
            self.input = text
            self.cur = len(text)
            return
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
        self.bg(self.agent.run, text, images)

    def _on_remote_change(self, n):
        cfg = jload(self.s.cfg_path)
        if cfg:
            self.s.config = cfg
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
        st["yolo"] = not st.get("yolo")
        self.s.save()
        self.toast(f"yolo {'ON — tools run without asking' if st['yolo'] else 'off — will ask before write/edit/bash'}", "warn" if st["yolo"] else "ok")

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
                  ("vision", "send images: true/false")]
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
            return self.toast(f"already on latest v{VERSION}", "ok")
        target = Path(os.path.abspath(__file__))
        target.write_text(src, "utf-8")
        self.info(f"✓ updated to v{v.group(1)} — restarting…", "ok")
        time.sleep(0.6)
        self.restart = True
        self.running = False

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
            os.execv(sys.executable, [sys.executable, os.path.abspath(__file__)] + sys.argv[1:])


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
"""


@action
def hello(ctx, who: str = "world"):
    "Say hello (example action)"
    for step in ctx.progress(range(3), "warming up"):
        ctx.log(f"step {step}")
    return f"hello, {who}!"


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
