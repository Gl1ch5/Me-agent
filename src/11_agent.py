# ───────────────────────────── agent ─────────────────────────────

SYSTEM = """You are me, an expert software engineer working in the user's terminal.
cwd: {cwd} | os: {os} | date: {date}

Work:
- Understand first: read the relevant code before changing it. Ask only when truly ambiguous.
- Split work into small steps. For 3+ steps state a short plan, then execute it step by step.
- Build what is needed now, but keep it open for the future: clear modules, small functions, no hardcoded hacks.
- Match the project's style, naming and libraries. No new dependencies without reason.
- Verify: run tests, build, or the code itself. Fix what you broke. Never claim untested success.
- Prefer edit over rewrite. Read several files at once when independent.
- web_search for current facts/APIs, web_fetch for docs and URLs.
- Confirm before destructive or irreversible actions (deleting data, force-push, production).
Design (UI work):
- No emoji unless asked. Icons: custom inline SVG with one consistent stroke style.
- Restrained palette: neutral + one accent + one semantic color; consistent spacing scale; clear type hierarchy; light and dark.
- Handle empty, loading and error states; make it responsive.
Reply: concise, no filler. Say what changed and how you verified it.{skills}{project}{user}"""



class Agent:
    """Model⇄tools loop. Frontend-agnostic: everything goes through `emit(event)`.
    Events: turn_start, text, reasoning, tool_start, tool_event, tool_end, usage, error, turn_end.
    `confirm(name, args) -> bool` and `ask(question) -> str` are provided by the frontend."""

    def __init__(self, store, emit, confirm=None, ask=None):
        self.s = store

        def _emit(e):
            PLUGINS.collect("event", e)
            emit(e)
        self.emit = _emit
        self.confirm = confirm or (lambda n, a: True)
        self.ask = ask
        self.skills = Skills(store)
        self.tools = ToolBox(store, self.skills)
        self.session = new_session()
        self.cancel = threading.Event()
        self.busy = False
        self.ctx_used = 0

    def system_prompt(self):
        sks = self.skills.enabled()
        skills = ("\nSkills (load via skill tool):\n" + "\n".join(f"- {k}: {v['description']}" for k, v in sks.items())) if sks else ""
        project = ""
        for name in ("ME.md", "AGENTS.md", "CLAUDE.md"):
            p = Path(self.session["cwd"]) / name
            if p.exists():
                project = f"\n\n# {name}\n" + p.read_text("utf-8", "replace")[:8000]
                break
        user = ("\n\n" + self.s.config["prompt"].strip()) if self.s.config.get("prompt", "").strip() else ""
        user += "".join("\n\n" + x for x in PLUGINS.collect("system_prompt", self))
        return SYSTEM.format(cwd=self.session["cwd"], os=f"{platform.system()} {platform.release()}", date=time.strftime("%Y-%m-%d"),
                             skills=skills, project=project, user=user)

    def run(self, text, images=()):
        """Blocking. Runs one user turn to completion."""
        m = self.s.model()
        if not m:
            self.emit({"type": "error", "text": "no model configured — use /provider then /models"})
            return
        self.busy = True
        self.cancel.clear()
        content = text if not images else [{"type": "text", "text": text}] + [{"type": "image_url", "image_url": {"url": u}} for u in images]
        self.session["messages"].append({"role": "user", "content": content})
        if not self.session["title"]:
            self.session["title"] = text.strip().split("\n")[0][:60]
        t0, tin, tout = time.time(), 0, 0
        self.emit({"type": "turn_start"})
        prov = Provider(m, self.s.provider(m))
        try:
            while True:
                msg, usage = prov.stream(self.system_prompt(), self.session["messages"], TOOLS + PLUGINS.tool_schemas(), self.emit, self.cancel)
                tin += usage["in"]
                tout += usage["out"]
                self.ctx_used = usage["in"] + usage["out"] or self.ctx_used
                self.session["usage"]["in"] = self.session["usage"].get("in", 0) + usage["in"]
                self.session["usage"]["out"] = self.session["usage"].get("out", 0) + usage["out"]
                self.emit({"type": "usage", "ctx": self.ctx_used, "in": tin, "out": tout})
                self.session["messages"].append(msg)
                if not msg.get("tool_calls"):
                    break
                imgs = []
                for tc in msg["tool_calls"]:
                    name = tc["function"]["name"]
                    try:
                        args = json.loads(tc["function"]["arguments"] or "{}")
                    except ValueError:
                        args = None
                    tid = tc["id"]
                    ev = {"type": "tool_start", "id": tid, "name": name, "args": args or {}, "label": self.tools.preview(name, args or {})}
                    if name == "skill" and args:
                        sk = self.skills.all().get(args.get("name"), {})
                        ev["skill"] = {"color": sk.get("color", ""), "icon": sk.get("icon", "")}
                    self.emit(ev)
                    t1 = time.time()
                    if args is None:
                        ok, out, img = False, "error: arguments were not valid JSON", None
                    elif isinstance(PLUGINS.emit("before_tool", name, args), str):
                        ok, out, img = False, PLUGINS.emit("before_tool", name, args), None
                    elif self.tools.needs_confirm(name, args) and not self.confirm(name, args):
                        ok, out, img = False, "user denied this action. ask what to do instead.", None
                    else:
                        try:
                            ok, out, img = self.tools.run(name, args, lambda e, _id=tid: self.emit({"type": "tool_event", "id": _id, **e}),
                                                          self.cancel, self.ask)
                        except Cancelled:
                            raise
                        except Exception as e:
                            ok, out, img = False, f"error: {type(e).__name__}: {e}", None
                    if img:
                        imgs.append(img)
                    self.session["messages"].append({"role": "tool", "tool_call_id": tid, "content": out})
                    self.emit({"type": "tool_end", "id": tid, "ok": ok, "output": out, "time": time.time() - t1})
                    if self.cancel.is_set():
                        raise Cancelled()
                if imgs and m.get("vision", True):
                    self.session["messages"].append({"role": "user", "content": [{"type": "text", "text": "(images from read)"}] +
                                                     [{"type": "image_url", "image_url": {"url": u}} for u in imgs]})
        except Cancelled:
            self._fix_dangling()
            self.session["messages"].append({"role": "user", "content": "[interrupted by user]"})
            self.emit({"type": "error", "text": "interrupted"})
        except Exception as e:
            self._fix_dangling()
            self.emit({"type": "error", "text": str(e)})
        finally:
            self.busy = False
            self.s.save_session(self.session)
            self.emit({"type": "turn_end", "time": time.time() - t0, "in": tin, "out": tout, "ctx": self.ctx_used})

    def _fix_dangling(self):
        """Every tool_call must have a tool result, or APIs reject the history."""
        msgs = self.session["messages"]
        for i in range(len(msgs) - 1, -1, -1):
            if msgs[i]["role"] == "assistant":
                have = {m.get("tool_call_id") for m in msgs[i + 1:] if m["role"] == "tool"}
                for tc in msgs[i].get("tool_calls") or []:
                    if tc["id"] not in have:
                        msgs.append({"role": "tool", "tool_call_id": tc["id"], "content": "cancelled"})
                break

    def load(self, sid):
        d = jload(self.s.session_path(sid))
        if d:
            self.session = d
            self.session.setdefault("usage", {"in": 0, "out": 0})
            self.ctx_used = 0
        return d
