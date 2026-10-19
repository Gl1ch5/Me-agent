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
            self.sel = None  # typing ends a mouse selection
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
        sel = getattr(self, "sel", None)
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
                self.sel = None
                return
            p, now = point(y, x), time.time()
            last = getattr(self, "_click", None)
            n = last[2] + 1 if last and now - last[0] < 0.45 and last[1][0] == p[0] and abs(last[1][1] - p[1]) <= 1 else 1
            self._click = (now, p, n)
            if n == 2:
                a, b = word_at(lines[p[0]], p[1])
                self.sel = {"a": (p[0], a), "b": (p[0], b), "word": True}
                self.copy_selection()
            elif n >= 3:  # the whole logical line, across soft wraps
                i0, i1 = p[0], p[0]
                while i0 > 0 and lines[i0].soft is not None:
                    i0 -= 1
                while i1 + 1 < len(lines) and lines[i1 + 1].soft is not None:
                    i1 += 1
                self.sel = {"a": (i0, 0), "b": (i1, 1 << 20), "word": True}
                self.copy_selection()
            else:
                self.sel = {"a": p, "b": p, "drag": True}
        elif kind == "drag" and getattr(self, "sel", None) and self.sel.get("drag"):
            if y <= 1 and self.scroll < maxscroll:  # dragging past the edge scrolls
                self.scroll += 1
            elif y >= avail and self.scroll > 0:
                self.scroll -= 1
            self.sel["b"] = point(y, x)
        elif kind == "release" and getattr(self, "sel", None) and self.sel.get("drag"):
            self.sel["drag"] = False
            if self.sel["a"] != self.sel["b"]:
                self.copy_selection()
                return
            self.sel = None
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
        m = self.s.model() or {}
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
