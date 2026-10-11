# ───────────────────────────── agent ─────────────────────────────

SYSTEM = """You are me, an expert software engineer working in the user's terminal.
cwd: {cwd} | os: {os} | date: {date}

Work:
- Understand first: read the relevant code before changing it. Ask only when truly ambiguous.
- Split work into small steps. For 3+ steps state a short plan, then execute it step by step.
- Build what is needed now, but keep it open for the future: clear modules, small functions, no hardcoded hacks.
- Match the project's style, naming and libraries. No new dependencies without reason.
- Verify: run tests, build, or the code itself. Fix what you broke. Never claim untested success.
- Prefer edit over rewrite; use paths relative to cwd. Read several files at once when independent.
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
        project, budget = "", 24000
        for name in self.context_files():
            text = (Path(self.session["cwd"]) / name).read_text("utf-8", "replace")[:min(10000, budget)]
            budget -= len(text)
            project += f"\n\n# {name}\n{text}"
        if "AGENTS.md" in self.context_files():
            project += "\n\nAGENTS.md is this project's memory: update it when you learn something durable (commands, structure, conventions, decisions)."
        else:
            project += ("\n\nNo AGENTS.md here yet. Once you understand the project, create a concise AGENTS.md "
                        "(purpose, stack, run/test commands, structure, conventions) — it is loaded into every future session.")
        pr = self.preset()
        if pr.get("prompt"):
            project += "\n\n" + pr["prompt"]
        user = ("\n\n" + self.s.config["prompt"].strip()) if self.s.config.get("prompt", "").strip() else ""
        user += "".join("\n\n" + x for x in PLUGINS.collect("system_prompt", self))
        return SYSTEM.format(cwd=self.session["cwd"], os=f"{platform.system()} {platform.release()}", date=time.strftime("%Y-%m-%d"),
                             skills=skills, project=project, user=user)

    def preset(self):
        return preset(self.s, self.session.get("agent") or self.s.config.get("agent"))

    def context_files(self):
        cwd = Path(self.session.get("cwd") or ".")
        return [n for n in CONTEXT_FILES if (cwd / n).is_file()]

    def tool_schemas(self):
        allowed = preset_tools(self.preset())
        return [t for t in TOOLS if t["name"] in allowed] + PLUGINS.tool_schemas()

    def run(self, text, images=()):
        """Blocking. Runs one user turn to completion."""
        pr = self.preset()
        m = self.s.model(pr.get("model") or None)
        if m and pr.get("effort"):
            m = dict(m, effort=pr["effort"])
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
        self.emit({"type": "turn_start", "agent": pr["name"], "context": self.context_files()})
        prov = Provider(m, self.s.provider(m))
        try:
            while True:
                limit = self.compact_threshold(m)
                if limit and self.ctx_used > limit:
                    self.compact(reason="auto")
                msg, usage = prov.stream(self.system_prompt(), self.session["messages"], self.tool_schemas(), self.emit, self.cancel)
                tin += usage["in"]
                tout += usage["out"]
                self.ctx_used = usage["in"] + usage["out"] or self.ctx_used
                self.session["ctx"] = self.ctx_used
                self.session["usage"]["in"] = self.session["usage"].get("in", 0) + usage["in"]
                self.session["usage"]["out"] = self.session["usage"].get("out", 0) + usage["out"]
                self.emit({"type": "usage", "ctx": self.ctx_used, "in": tin, "out": tout})
                self.session["messages"].append(msg)
                self.checkpoint()
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
                    elif name not in {t["name"] for t in self.tool_schemas()}:
                        ok, out, img = False, f"error: tool {name} is not available in the {pr['name']} agent", None
                    elif name in ("write", "edit") and not may_write(pr, os.path.relpath(os.path.abspath(args.get("path", "")), self.session["cwd"])):
                        ok, out, img = False, f"error: the {pr['name']} agent may only write {pr.get('writes') or 'nothing'}", None
                    elif isinstance(PLUGINS.emit("before_tool", name, args), str):
                        ok, out, img = False, PLUGINS.emit("before_tool", name, args), None
                    elif self.tools.needs_confirm(name, args) and not self.confirm(name, args):
                        ok, out, img = False, "user denied this action. ask what to do instead.", None
                    else:
                        try:
                            ok, out, img = self.tools.run(name, self.remap(args), lambda e, _id=tid: self.emit({"type": "tool_event", "id": _id, **e}),
                                                          self.cancel, self.ask)
                        except Cancelled:
                            raise
                        except Exception as e:
                            ok, out, img = False, f"error: {type(e).__name__}: {e}", None
                    if img:
                        imgs.append(img)
                    self.session["messages"].append({"role": "tool", "tool_call_id": tid, "content": out})
                    self.emit({"type": "tool_end", "id": tid, "ok": ok, "output": out, "time": time.time() - t1})
                    self.checkpoint()
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

    COMPACT_PROMPT = ("Summarize this coding session so work can continue without it. Keep: the user's goals and preferences, "
                      "decisions made, files created/changed (paths), commands that matter, current state, open problems, next steps. "
                      "Dense bullet points, no filler.")

    def compact_threshold(self, m):
        """Tokens at which history is compacted: settings.autocompact % of the model context (default 80%), never above 1M. 0 = off."""
        pct = self.s.config["settings"].get("autocompact", 80)
        if not pct:
            return 0
        ctx = int(m.get("context") or 128000)
        return min(int(ctx * float(pct) / 100), 1_000_000)

    def compact(self, keep_turns=2, reason="manual"):
        """Replace older history with a model-written summary; keep the last `keep_turns` user turns verbatim."""
        msgs = self.session["messages"]
        starts = [i for i, m in enumerate(msgs) if m["role"] == "user" and isinstance(m.get("content"), str)
                  and not m["content"].startswith(("[interrupted", "[Summary"))]
        if len(starts) <= keep_turns:
            return False
        cut = starts[-keep_turns]
        lines = []
        for m in msgs[:cut]:
            c = m.get("content")
            if isinstance(c, list):
                c = " ".join(x.get("text", "") for x in c if x.get("type") == "text")
            if m["role"] == "tool":
                lines.append(f"[tool result] {str(c)[:600]}")
            else:
                if c:
                    lines.append(f"[{m['role']}] {str(c)[:4000]}")
                for tc in m.get("tool_calls") or []:
                    lines.append(f"[tool call] {tc['function']['name']} {tc['function']['arguments'][:300]}")
        m = self.s.model()
        self.emit({"type": "compacting", "reason": reason})
        msg, usage = Provider(m, self.s.provider(m)).stream(self.COMPACT_PROMPT, [{"role": "user", "content": "\n".join(lines)[-400000:]}],
                                                            None, lambda e: None, self.cancel)
        summary = (msg.get("content") or "").strip()
        if not summary:
            return False
        before = len(msgs)
        self.session["messages"] = [{"role": "user", "content": "[Summary of the earlier conversation]\n" + summary},
                                    {"role": "assistant", "content": "Understood — continuing from this summary."}] + msgs[cut:]
        self.session["compactions"] = self.session.get("compactions", 0) + 1
        self.ctx_used = self.session["ctx"] = len(json.dumps(self.session["messages"], ensure_ascii=False)) // 4
        self.emit({"type": "compacted", "before": before, "after": len(self.session["messages"]), "ctx": self.ctx_used,
                   "reason": reason, "summary": summary[:2000]})
        self.s.save_session(self.session)
        return True

    def checkpoint(self, every=1.0):
        """Save mid-turn (throttled) so other devices see each step live."""
        if time.time() - getattr(self, "_saved", 0) >= every:
            self._saved = time.time()
            self.s.save_session(self.session)

    def refresh_from_disk(self, cwd=None):
        """Another device advanced this session: adopt the newer copy (only while idle). Returns True if reloaded."""
        if self.busy:
            return False
        d = jload(self.s.session_path(self.session["id"]))
        if not d or d.get("updated", 0) <= self.session.get("updated", 0) + 0.01:
            return False
        d.setdefault("usage", {"in": 0, "out": 0})
        self.session = d
        self.ctx_used = d.get("ctx", self.ctx_used)
        if cwd and os.path.abspath(cwd) != d.get("cwd"):
            self.relocate(cwd)
        return True

    def relocate(self, new_cwd):
        """Session moved to another machine/sandbox: map every earlier project root to the new one,
        so absolute paths remembered from history keep pointing into the project."""
        new_cwd = os.path.abspath(new_cwd)
        old = self.session.get("cwd")
        roots = self.session.setdefault("roots", [])
        if old and old != new_cwd and old not in roots:
            roots.append(old)
        self.session["roots"] = [r for r in roots if r != new_cwd]
        self.session["cwd"] = new_cwd

    def remap(self, args):
        roots = self.session.get("roots") or []
        if not roots:
            return args
        cwd = self.session["cwd"]
        out = {}
        for k, v in args.items():
            if isinstance(v, str):
                for r in sorted(roots, key=len, reverse=True):
                    v = v.replace(r, cwd)
            out[k] = v
        return out

    def load(self, sid):
        d = jload(self.s.session_path(sid))
        if d:
            self.session = d
            self.session.setdefault("usage", {"in": 0, "out": 0})
            self.ctx_used = self.session.get("ctx", 0)
        return d
