# ───── TUI: rendering (mixin) ─────


class TUIRender:
    # ─── rendering ───
    def block_lines(self, b, width):
        if b._cache and b._cache[0] == width and b.done and b.kind not in ("tool", "skill", "welcome"):
            return b._cache[1]
        if b._cache and b._cache[0] == width and b.kind in ("user", "info", "stats"):
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
        if b.status and not b.done:
            out.append(row([(SPIN[self.frame % len(SPIN)] + " ", rgb(color)), (b.status[: inner - 4], rgb(TH["fg"]))]))
        p = b.progress
        if p and not b.done:
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
        g = self.agent.session.get("goal") or {}
        # only while the agent works: a finished turn leaves no checklist behind, even if some items were never ticked
        if not items or not (self.agent.busy or g.get("status") == "working"):
            return []
        done = sum(1 for i in items if i["s"] == "done")
        head = Line().add(" ").add("todo ", rgb(TH["accent"]) + CSI + "1m").add(f"{done}/{len(items)}", rgb(TH["dim"]))
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
            first = max(0, min(cr, len(in_rows) - 8))  # keep the cursor row inside the 8 visible rows
            in_rows = in_rows[first:first + 8]
            cr = max(0, min(cr - first, len(in_rows) - 1))
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
            avail = max(1, H - len(bottom) - len(pop))
            # transcript
            lines = []
            for b in self.blocks:
                lines.extend(self.block_lines(b, W - 1))
            maxscroll = max(0, len(lines) - avail)
            self.scroll = min(self.scroll, maxscroll)
            end = len(lines) - self.scroll
            view = lines[max(0, end - avail):end]
            view = [Line()] * (avail - len(view)) + view
            if self.scroll and view:
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
