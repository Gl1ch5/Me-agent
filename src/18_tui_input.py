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
