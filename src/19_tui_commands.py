# ───── TUI: slash commands (mixin) ─────


class TUICommands:
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
        txt += ("\n\n## Keys\n- `enter` send · `shift+enter` / `alt+enter` / `ctrl+j` / `\\`+enter newline\n- `esc` interrupt · `ctrl+c` clear/quit"
                "\n- `↑↓` history · `pgup/pgdn` / wheel scroll · `ctrl+o` expand last tool (or click it)\n- `@path` attach file (images go to the model)"
                "\n- `tab` accept suggestion · `ctrl+v` / `alt+v` paste text or an image · drag an image file in to attach it"
                "\n- `alt+c` copy the last answer · `/copy` answer, code block or the whole chat"
                "\n\n## Mouse\n- drag to select text: it is copied when you let go (double click = word, triple = line)"
                "\n- right click: copy the selection, or paste · click a tool card to open it"
                "\n- `shift`+drag: the terminal's own selection · `/config` → mouse off gives it back everywhere")
        self.add("assistant", text=txt).done = True

    def cmd_exit(self, arg):
        self.running = False

    cmd_quit = cmd_exit

    def cmd_agent(self, arg):
        ps = presets(self.s)
        cur = self.agent.preset()["name"]
        if arg in ps:
            name = arg
        else:
            items = [(n, p.get("label", n), p.get("description", "") + (" · custom" if p.get("custom") else "")) for n, p in ps.items()]
            items.append(("+market", "+ more agents", "marketplace"))
            name = self.pick("Agent mode", items, current=cur)
            if name == "+market":
                name = self.install_from_market("agent")
            if not name:
                return
        self.s.config["agent"] = name
        self.agent.session["agent"] = name
        self.s.save()
        p = ps.get(name) or presets(self.s).get(name, {})
        self.toast(f"mode → {p.get('label', name)} · tools: {p.get('tools', ALL_TOOLS)}", "ok")

    def cmd_init(self, arg):
        if self.agent.busy:
            return self.toast("busy", "warn")
        exists = Path("AGENTS.md").exists()
        self.add("user", text="/init")
        self.agent.run(("Update" if exists else "Create") + " AGENTS.md for this project. Explore it first (structure, manifests, scripts, README, "
                       "tests). Write a concise file: purpose; stack; how to install/run/test/build (exact commands, verified); folder map; "
                       "conventions; gotchas. It is loaded into every session, so keep it short and accurate.")

    def cmd_compact(self, arg):
        if self.agent.busy:
            return self.toast("busy — wait for the turn to finish", "warn")
        if not self.agent.compact(reason="manual"):
            self.toast("nothing to compact yet")

    def cmd_autocompact(self, arg):
        st = self.s.config["settings"]
        if not arg:
            cur = st.get("autocompact", 80)
            arg = self.pick(f"Auto-compact (now: {cur or 'off'}{'%' if cur else ''} of context, max 1M tokens)",
                            [("50", "50%", ""), ("70", "70%", ""), ("80", "80%", "default"), ("90", "90%", ""), ("off", "off", "")])
            if not arg:
                return
        st["autocompact"] = 0 if arg == "off" else max(10, min(95, int(str(arg).rstrip("%"))))
        self.s.save()
        m = self.s.model()
        lim = self.agent.compact_threshold(m) if m else 0
        self.toast(f"auto-compact {'off' if not lim else f'at {human(lim)} tokens'}", "ok")

    def cmd_theme(self, arg):
        ts = themes(self.s)
        cur = self.s.config.get("theme") or "me-dark"
        if arg in ts:
            name = arg
        else:
            items = [(n, t.get("label", n), ("dark" if t.get("dark", True) else "light") + (" · built-in" if n in BUILTIN_THEMES else ""))
                     for n, t in ts.items()] + [("+market", "+ get more themes", "marketplace")]
            m = {"kind": "pick", "title": "Theme (↑↓ previews)", "items": items, "sel": next((i for i, x in enumerate(items) if x[0] == cur), 0),
                 "filter": "", "hint": "", "preview": lambda v: v in ts and self.retheme(v)}
            name = self._modal(m)
            if name == "+market":
                return self.install_from_market("theme")
            if not name:
                return self.retheme(cur)
        self.s.config["theme"] = name
        self.s.save()
        self.retheme(name)
        self.toast(f"theme → {name}", "ok")

    def install_from_market(self, kind):
        """Pick an item of `kind` from the marketplace and install it (synced to the account when online)."""
        try:
            items = self.cloud.req("GET", f"/api/market?kind={kind}", token=False)
        except CloudError as e:
            return self.info(f"✗ {e}", "err")
        mid = self.pick(f"Marketplace · {kind}s", [(x["id"], x["name"], x["description"]) for x in items])
        if not mid:
            return None
        x = self.cloud.req("GET", f"/api/market/{mid}", token=False)
        folder = {"theme": "themes", "agent": "agents", "plugin": "plugins", "skill": "skills"}[kind]
        ext = {"theme": "json", "agent": "md", "plugin": "py"}.get(kind, x.get("ext", "py"))
        (self.s.home / folder / f"{x['name']}.{ext}").write_text(x["content"], "utf-8")
        if self.cloud.online:
            try:
                self.cloud.req("POST", f"/api/market/{mid}/install", {})
            except CloudError:
                pass
        self.s.save()
        self.toast(f"installed {kind} {x['name']}", "ok")
        return x["name"]

    def cmd_new(self, arg):
        """A fresh session on a clean screen: nothing of the old conversation stays visible or in the model's context."""
        if self.agent.busy or self.launching:
            return self.toast("busy — esc to stop the current turn first", "warn")
        with self.lock:
            self.agent.session = new_session()
            self.agent.ctx_used, self.agent.snap = 0, {}
            self.blocks, self.tools_live, self.cur_text, self.cur_reason = [], {}, None, None
            self.scroll, self.sel, self.turn_tokens = 0, None, (0, 0)
            self._prev_rows = None
            self.term.write(CSI + "2J")
            self.welcome()
        self.toast("new session", "ok")

    cmd_clear = cmd_new

    def cmd_copy(self, arg):
        """/copy (picker when the answer has code) · /copy 2 (the answer before last) · /copy code · /copy all (whole chat)."""
        answers = [m["content"] for m in self.agent.session["messages"] if m["role"] == "assistant" and isinstance(m.get("content"), str)
                   and m["content"].strip()] or [b.text for b in self.blocks if b.kind == "assistant" and b.text.strip()]
        if not answers:
            return self.toast("nothing to copy")
        words = arg.split()
        n = next((int(w) for w in words if w.isdigit()), 1)
        text = answers[-min(max(1, n), len(answers))]
        codes = re.findall(r"```[^\n]*\n(.*?)```", text, re.S)
        v = "code" if "code" in words else "all" if "all" in words else "last" if words else None
        if v is None:
            items = [("last", "last answer", f"{len(text)} chars")]
            items += [(f"code{i}", f"code block {i + 1}", c.strip().split("\n")[0][:50]) for i, c in enumerate(codes)]
            items += [("all", "whole conversation", f"{len(self.agent.session['messages'])} messages")]
            v = "last" if not codes else self.pick("Copy", items)
            if not v:
                return
        if v == "all":
            parts = []
            for m in self.agent.session["messages"]:
                c = m.get("content")
                if m["role"] == "user" and isinstance(c, str) and not m.get("_aux"):
                    parts.append("> " + c.replace("\n", "\n> "))
                elif m["role"] == "assistant" and isinstance(c, str) and c.strip():
                    parts.append(c.strip())
            return self.copy_text("\n\n".join(parts), "conversation")
        if v == "code" or v.startswith("code"):
            if not codes:
                return self.toast("no code block in that answer", "warn")
            i = int(v[4:]) if v[4:].isdigit() and int(v[4:]) < len(codes) else len(codes) - 1
            return self.copy_text(codes[i].rstrip("\n"), f"code block {i + 1}")
        self.copy_text(text.strip("\n"), "last answer" if n == 1 else f"answer #{n} from the end")

    def cmd_yolo(self, arg):
        st = self.s.config["settings"]
        st["yolo"] = False
        st["perm"] = "ask" if perm_mode(st) == "full" else "full"
        self.s.save()
        self.toast(f"{'full access — tools run without asking' if st['perm'] == 'full' else 'ask — commands and writes outside the project ask'}",
                   "warn" if st["perm"] == "full" else "ok")

    def cmd_perm(self, arg):
        st = self.s.config["settings"]
        cur = perm_mode(st)
        v = arg or self.pick("Permissions", [(k, ("● " if k == cur else "  ") + PERMS[k], {
            "read": "no file changes; only allow-listed commands", "ask": "edits inside the project are free; commands ask",
            "full": "everything runs without asking"}[k]) for k in PERMS] + [("allow", "  allow-list…", "commands that never ask")])
        if v in PERMS:
            st["perm"], st["yolo"] = v, False
            self.s.save()
            return self.toast("permissions: " + PERMS[v], "ok")
        if v == "allow":
            cur_list = st.get("allow") or list(DEFAULT_ALLOW)
            text = self.prompt("Allowed commands, one pattern per line (* = anything; chained/redirected commands always ask)",
                               "\n".join(cur_list), multiline=True)
            if text is not None:
                st["allow"] = [l.strip() for l in text.splitlines() if l.strip()]
                self.s.save()
                self.toast(f"{len(st['allow'])} allowed patterns", "ok")

    def cmd_goal(self, arg):
        if self.agent.busy:
            return self.toast("busy", "warn")
        arg = (arg or "").strip().strip('"')
        if not arg:
            arg = self.prompt("Goal — the agent works until it is done and verified, or blocked", multiline=True)
            if not arg:
                return
        self.add("user", text="/goal " + arg)
        self.agent.goal(arg)

    def cmd_undo(self, arg):
        r = self.agent.undo()
        if not r["ok"]:
            self.toast(r["text"], "warn")
        else:
            self.render_history()
            self.info("↶ " + r["text"], "warn")

    def cmd_todo(self, arg):
        items = self.agent.session.get("todo") or []
        if not items:
            return self.toast("no todo list in this session")
        self.add("assistant", text="\n".join(("- [x] " if i["s"] == "done" else "- [ ] " if i["s"] == "todo" else "- [>] ") + i["t"] for i in items)).done = True

    def cmd_cost(self, arg):
        st = self.s.config["settings"]
        u = self.agent.session.get("usage", {})
        m = self.s.model() or {}
        pin, pout, pc = model_price(m)
        if arg:
            k, _, v = arg.partition(" ")
            if k in ("turn", "day"):
                st["max_turn_cost" if k == "turn" else "max_day_cost"] = float(v or 0)
                self.s.save()
                return self.toast(f"{k} limit {money(float(v or 0)) if float(v or 0) else 'off'}", "ok")
        days = jload(self.s.home / "usage.json", {}) or {}
        txt = (f"## Spending\n- session: **{money(u.get('cost', 0))}** · ↑{human(u.get('in', 0))} ↓{human(u.get('out', 0))}"
               f" · cached {human(u.get('cached', 0))}\n- today: **{money(day_cost(self.s.home))}** · last 7 days: "
               f"{money(sum(v for k, v in sorted(days.items())[-7:]))}\n- {m.get('name', 'model')}: ${pin}/M in · ${pout}/M out · ${pc}/M cached"
               f"{'' if any((pin, pout)) else ' (unknown — set price in /models)'}\n- limits: per task {money(st.get('max_turn_cost') or 0) if st.get('max_turn_cost') else 'off'}"
               f" · per day {money(st.get('max_day_cost') or 0) if st.get('max_day_cost') else 'off'}  (`/cost turn 0.5`, `/cost day 5`)")
        self.add("assistant", text=txt).done = True

    def cmd_mcp(self, arg):
        mc = self.s.config.setdefault("mcp", {})
        items = [(k, k, v.get("url") or " ".join([v.get("command", "")] + v.get("args", []))[:60]) for k, v in mc.items()]
        k = self.pick("MCP servers — loaded lazily: one prompt line idle, tools fetched on use", items + [("+", "+ add server", "stdio command or http url")])
        if k is None:
            return
        if k == "+":
            name = self.prompt("Name (e.g. github)")
            if not name:
                return
            target = self.prompt("Command (npx -y @modelcontextprotocol/server-github) or URL (https://…/mcp)")
            if not target:
                return
            desc = self.prompt("One-line description for the agent", "")
            if target.startswith(("http://", "https://")):
                spec = {"url": target}
            else:
                import shlex
                parts = shlex.split(target)
                spec = {"command": parts[0], "args": parts[1:]}
            if desc:
                spec["description"] = desc
            mc[re.sub(r"[^\w-]", "", name)] = spec
            self.s.save()
            return self.toast(f"MCP {name} added", "ok")
        act = self.pick(k, [("test", "test: list tools", ""), ("del", "remove", "")])
        if act == "del":
            mc.pop(k, None)
            self.s.save()
            MCP_CLIENTS.pop(k, None)
        elif act == "test":
            try:
                self.add("assistant", text=f"## {k}\n" + mcp_describe(mcp_client(self.s, k).list_tools())).done = True
            except Exception as e:
                self.info(f"✗ {e}", "err")

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
                  ("vision", "send images: true/false"), ("price", "$ per 1M tokens: in out [cached] (blank = built-in table)"),
                  ("cache", "prompt caching: auto / anthropic / openai / off")]
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
            elif f == "cache":
                v = self.pick("Prompt caching", [("", "auto", "detect from model/provider"), ("anthropic", "anthropic", "cache_control marker"),
                                                 ("openai", "openai", "prompt_cache_key"), ("off", "off", "")], current=m.get("cache", ""))
            elif f == "price":
                v = self.prompt("in out [cached], $ per 1M tokens", " ".join(map(str, m.get("price") or [])))
                if v is not None:
                    try:
                        v = [float(x) for x in v.replace(",", " ").split()][:3] or ""
                    except ValueError:
                        self.toast("numbers, e.g. 3 15 0.3", "err")
                        continue
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

    def render_history(self):
        """Rebuild the transcript from the current session's messages."""
        with self.lock:
            self.blocks = []
            self.welcome()
            for m in self.agent.session["messages"]:
                c = m.get("content")
                if isinstance(c, list):
                    c = " ".join(x.get("text", "") for x in c if x.get("type") == "text")
                if m["role"] == "user" and m.get("_aux"):  # the agent's own nudges are not something the user typed
                    if isinstance(c, str) and c.startswith("[message from the user"):
                        self.add("user", text=c.split("\n", 1)[-1])
                    continue
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
                        a = a if isinstance(a, dict) else {}
                        b = self.add("tool", name=tc["function"]["name"], args=a, label=self.agent.tools.preview(tc["function"]["name"], a))
                        b.done, b.ok, b.collapsed = True, True, True

    def cmd_sessions(self, arg):
        ss = self.s.sessions()
        if not ss:
            return self.toast("no sessions yet")
        sid = self.pick("Resume session", [(s["id"], s.get("title") or "(untitled)",
                                            time.strftime("%d %b %H:%M", time.localtime(s.get("updated", 0))) + f" · {s.get('n', 0)} msgs · "
                                            + s.get("cwd", "")) for s in ss])
        if not sid or self.agent.busy:
            return
        self.agent.load(sid)
        self.render_history()
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
                     ("mouse", f"mouse                {'on' if st.get('mouse', True) else 'off'}",
                      "on: wheel scroll, drag to select+copy · off: the terminal's own selection"),
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
            self.update_avail = None  # the banner was stale: nothing newer on the server
            return self.toast(f"already on latest v{VERSION}", "ok")
        target = Path(os.path.abspath(__file__))
        target.write_bytes(src.encode("utf-8"))  # byte-exact (write_text would turn every line end into CRLF on Windows)
        self.info(f"✓ updated to v{v.group(1)} — restarting…", "ok")
        time.sleep(0.6)
        self.restart = True
        self.running = False
