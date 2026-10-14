# ───────────────────────────── agent ─────────────────────────────

SYSTEM = """You are me, an expert software engineer working in the user's terminal.
cwd: {cwd} | os: {os} | shell: {shell} | date: {date}

Scope (most important):
- Do exactly what was asked, nothing more. Match effort to the request: a question gets an answer, a greeting gets a short reply, a one-line fix gets a one-line edit.
- Questions and chat need no tools unless the answer depends on files, commands or current facts. Never create, edit or delete files, install packages or run commands the request does not need.
- Do not refactor, reformat, rename, add features, docs or tests nobody asked for. Mention worthwhile extras in one line instead of doing them.
- If the request is unclear in a way that changes the result, ask one short question; otherwise pick the obvious reading and go.

Work:
- Read the relevant code before changing it. Prefer edit over rewrite; paths relative to cwd. Batch independent reads.
- For 3+ step tasks keep a todo list current; skip it for small ones.
- Match the project's style; clean small functions; no hacks, no needless deps.
- After changing code, verify it (run tests, build, lint or the code). Never claim untested success.
- web_search for current facts, web_fetch for docs/URLs.
- Confirm before destructive or irreversible actions.
UI work: no emoji; inline SVG icons, one stroke style; neutral + one accent palette, spacing scale, light+dark; empty/loading/error states; responsive.
Reply: in the user's language, concise. After changes say what changed and how you verified it.{extra}{skills}{project}{user}"""



class LimitHit(Exception):
    pass


DOC_EXT = r"\.(md|markdown|txt|rst|adoc|csv)$"


def tc_args(tc):
    try:
        a = json.loads(tc["function"]["arguments"] or "{}")
        return a if isinstance(a, dict) else None
    except ValueError:
        return None


VERIFY_NUDGE = ("[self-check] You changed files but ran nothing since. Before finishing, verify: run the tests, build, lint, "
                "or the code itself, and fix any failure. If nothing can be run, say so and why.")
GOAL_PROMPT = """GOAL: {objective}

Work autonomously until this is done. First write verifiable success criteria and a todo list. Then do one step at a time,
keep the todo current, and verify with real checks (tests, build, running the code). Do not ask the user unless truly blocked.
End your final message with exactly one line:
GOAL: DONE <the evidence that every criterion passes>
or
GOAL: BLOCKED <the precise reason and what is needed>"""


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
        self.depth = 0  # subagent nesting
        self.persist = True  # subagents don't save their own sessions
        self.snap = {}  # path -> content before this turn first touched it (None = did not exist)
        self._turn_lock = threading.Lock()
        self.take_queued = lambda: []  # frontend hook: messages the user sent mid-turn, read after every tool step

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
            project += ("\n\nNo AGENTS.md here yet. Only during real multi-step work on a code project (not for questions or small tasks), "
                        "you may offer to create a concise AGENTS.md (purpose, stack, run/test commands, structure, conventions).")
        pr = self.preset()
        if pr.get("prompt"):
            project += "\n\n" + pr["prompt"].replace("{home}", str(self.s.home))
        if pr.get("cli"):
            project += "\nYour CLI tools (prefer them over hand-written shell for these jobs): " + ", ".join(f"{n} — {c['desc']}" for n, c in pr["cli"].items())
        user = ("\n\n" + self.s.config["prompt"].strip()) if self.s.config.get("prompt", "").strip() else ""
        user += "".join("\n\n" + x for x in PLUGINS.collect("system_prompt", self))
        extra = ""
        mem = mem_prompt(self.s.home)
        if mem:
            extra += "\nGlobal memory (notes you kept earlier, newest first; use memory tool to add or forget):\n" + mem
        if "task" in {t["name"] for t in self.tool_schemas()}:
            extra += "\ntask agents: " + ", ".join(presets(self.s)) + "."
        mcp = self.s.config.get("mcp") or {}
        if mcp:
            extra += "\nMCP servers (mcp tool): " + "; ".join(f"{k}{': ' + v['description'] if v.get('description') else ''}" for k, v in mcp.items())
        if os.environ.get("ME_PREVIEW"):
            extra += (f"\nPreview for the user: files in cwd are served at {os.environ['ME_PREVIEW']}/<path>; "
                      f"a dev server on port $PORT ({os.environ.get('PORT')}) at {os.environ['ME_PREVIEW']}/port/. Give the user that link.")
        return SYSTEM.format(cwd=self.session["cwd"], os=f"{platform.system()} {platform.release()}", date=time.strftime("%Y-%m-%d"),
                             shell="cmd.exe (bash tool runs Windows commands: dir, type, findstr, set; or powershell -NoProfile -Command \"...\"; no POSIX syntax)" if WIN else os.environ.get("SHELL", "/bin/sh"),
                             extra=extra, skills=skills, project=project, user=user)

    def preset(self):
        return preset(self.s, self.session.get("agent") or self.s.config.get("agent"))

    @property
    def cwd(self):
        return self.session.get("cwd") or os.getcwd()

    def abspath(self, path):
        """A tool path as the model meant it: relative to the session's project folder, ~ expanded."""
        return os.path.abspath(os.path.join(self.cwd, os.path.expanduser(str(path or ""))))

    def context_files(self):
        cwd = Path(self.session.get("cwd") or ".")
        return [n for n in CONTEXT_FILES if (cwd / n).is_file()]

    def tool_schemas(self):
        allowed = preset_tools(self.preset())
        if self.depth:
            allowed.discard("task")
        out = [t for t in TOOLS if t["name"] in allowed]
        if self.s.auth.get("token"):
            out.append(NOTIFY_TOOL)
        if self.s.config.get("mcp"):
            out.append(MCP_TOOL)
        pr = self.preset()
        out += [cli_schema(n, c, pr["name"]) for n, c in (pr.get("cli") or {}).items()]
        return out + PLUGINS.tool_schemas()

    def run(self, text, images=()):
        """Blocking. Runs one user turn to completion; turns started from other threads wait for it."""
        with self._turn_lock:
            return self._run(text, images)

    def _run(self, text, images=()):
        pr = self.preset()
        m = self.s.model(pr.get("model") or None)
        if m and pr.get("effort"):
            m = dict(m, effort=pr["effort"])
        if not m:
            self.emit({"type": "error", "text": "no model configured — use /provider then /models"})
            return
        self.busy = True
        self.cancel.clear()
        self.snap = {}
        start_idx = len(self.session["messages"])
        self._turn_gen = self.session.get("compactions", 0)
        content = text if not images else [{"type": "text", "text": text}] + [{"type": "image_url", "image_url": {"url": u}} for u in images]
        self.session["messages"].append({"role": "user", "content": content})
        if not self.session["title"]:
            self.session["title"] = text.strip().split("\n")[0][:60]
        self._trim_history(start_idx)
        t0, tin, tout, tcached, steps, cost = time.time(), 0, 0, 0, 0, 0.0
        wrote, ran, nudged = False, False, False
        self.emit({"type": "turn_start", "agent": pr["name"], "context": self.context_files()})
        prov = Provider(m, self.s.provider(m))
        st = self.s.config["settings"]
        try:
            while True:
                limit = self.compact_threshold(m)
                if limit and self.ctx_used > limit:
                    self.compact(reason="auto")
                schemas = self.tool_schemas()
                self._tool_names = {t["name"] for t in schemas}
                system = self.system_prompt()
                msg, usage = prov.stream(system, self.session["messages"], schemas, self.emit, self.cancel,
                                         cache_key=self.session["id"])
                if not usage["in"]:  # provider sent no usage (some proxies drop stream_options): estimate, ~4 chars per token
                    usage = dict(usage, est=True, **{"in": len(system + json.dumps(self.session["messages"], ensure_ascii=False)
                                                                    + json.dumps(schemas)) // 4,
                                                     "out": usage["out"] or len(json.dumps(msg, ensure_ascii=False)) // 4})
                c = usage_cost(m, usage)
                tin += usage["in"]
                tout += usage["out"]
                tcached += min(usage.get("cached", 0), usage["in"])
                steps += 1
                cost += c
                self.ctx_used = usage["in"] + usage["out"] or self.ctx_used
                self.session["ctx"] = self.ctx_used
                u = self.session["usage"]
                u["in"] = u.get("in", 0) + usage["in"]
                u["out"] = u.get("out", 0) + usage["out"]
                u["cached"] = u.get("cached", 0) + usage.get("cached", 0)
                u["cost"] = round(u.get("cost", 0) + c, 6)
                today = add_day_cost(self.s.home, c) if c and self.persist else day_cost(self.s.home)
                self.emit({"type": "usage", "ctx": self.ctx_used, "in": tin, "out": tout, "cached": tcached, "steps": steps, "est": bool(usage.get("est")),
                           "cost": cost, "session_cost": u["cost"], "day_cost": today})
                self.session["messages"].append(msg)
                self.checkpoint()
                if not msg.get("tool_calls"):
                    if wrote and not ran and not nudged and "bash" in self._tool_names and not st.get("no_verify"):
                        nudged = True  # self-check before "done": changes were made but nothing was run since
                        self.emit({"type": "verify"})
                        self.session["messages"].append({"role": "user", "content": VERIFY_NUDGE})
                        continue
                    break
                self._limits(cost, st)
                results = self._run_calls(msg["tool_calls"], pr)
                imgs = []
                for tc, (ok, out, img) in results:
                    name = tc["function"]["name"]
                    if ok and name in ("write", "edit") and not re.search(DOC_EXT, str((tc_args(tc) or {}).get("path", "")), re.I):
                        wrote, ran = True, False  # docs/config prose needs no test run: nudging there only burned a model call
                    elif name == "bash" or name == "task":
                        ran = ran or wrote
                    if img:
                        imgs.append(img)
                if self.cancel.is_set():
                    raise Cancelled()
                if imgs and m.get("vision", True):
                    self.session["messages"].append({"role": "user", "content": [{"type": "text", "text": "(images from read)"}] +
                                                     [{"type": "image_url", "image_url": {"url": u}} for u in imgs]})
                queued = self.take_queued() if self.depth == 0 else []
                if queued:  # the user wrote while tools ran: hand it to the model now instead of after the whole turn
                    self.emit({"type": "queued_sent", "texts": queued})
                    self.session["messages"].append({"role": "user", "content": "[message from the user while you were working — take it into account]\n"
                                                     + "\n\n".join(queued)})
        except Cancelled:
            self._fix_dangling()
            self.session["messages"].append({"role": "user", "content": "[interrupted by user]"})
            self.emit({"type": "error", "text": "interrupted"})
        except LimitHit as e:
            self._fix_dangling()
            self.emit({"type": "error", "text": str(e)})
        except Exception as e:
            self._fix_dangling()
            self.emit({"type": "error", "text": str(e)})
        finally:
            self.busy = False
            todo = self.session.get("todo") or []
            if todo and all(i["s"] == "done" for i in todo):
                self.session["todo"] = []  # finished list: don't carry it into the next request
            self._finish_checkpoint(start_idx)
            if pr.get("name") == "me" and self.persist:
                self.s.save()  # new skills/themes/agents written to the home folder reach every device
            if self.persist:
                self.s.save_session(self.session)
            self.emit({"type": "turn_end", "time": time.time() - t0, "in": tin, "out": tout, "cached": tcached, "steps": steps,
                       "ctx": self.ctx_used, "cost": cost,
                       "session_cost": self.session["usage"].get("cost", 0)})

    TRIM_AT, TRIM_KEEP = 4000, 1500

    def _trim_history(self, upto):
        """Before a new user turn: shrink bulky tool results and drop image payloads of earlier turns.
        Every step re-sends the whole history, so a 40 KB file read once was paid for again on every later step. The model can
        re-read anything it still needs. Only messages before `upto` change, once, so the cached prompt prefix stays stable after."""
        for m in self.session["messages"][:upto]:
            c = m.get("content")
            if m["role"] == "tool" and isinstance(c, str) and len(c) > self.TRIM_AT and not c.startswith("[trimmed"):
                k = self.TRIM_KEEP
                m["content"] = f"[trimmed old output: {len(c)} chars; read again if needed]\n{c[:k]}\n…\n{c[-k // 3:]}"
            elif isinstance(c, list) and any(x.get("type") == "image_url" for x in c):
                m["content"] = [x if x.get("type") != "image_url" else {"type": "text", "text": "[image shown earlier]"} for x in c]

    def _limits(self, cost, st):
        mt, md = float(st.get("max_turn_cost") or 0), float(st.get("max_day_cost") or 0)
        if mt and cost >= mt:
            raise LimitHit(f"stopped: this task reached the {money(mt)} per-task limit (/config to change)")
        if md and day_cost(self.s.home) >= md:
            raise LimitHit(f"stopped: today's spending reached the {money(md)} daily limit (/config to change)")

    def _run_calls(self, calls, pr):
        """Independent read-only calls run in parallel; anything that changes state runs in order."""
        prepared = []
        for tc in calls:
            name = tc["function"]["name"]
            try:
                args = json.loads(tc["function"]["arguments"] or "{}")
                if not isinstance(args, dict):
                    raise ValueError
            except ValueError:
                args = None
            tid = tc["id"]
            ev = {"type": "tool_start", "id": tid, "name": name, "args": args or {}, "label": self.tools.preview(name, args or {}, self.cwd)}
            cli = (self.preset().get("cli") or {}).get(name)
            if cli:
                pr = self.preset()
                ev["cli"] = {"agent": pr["name"], "label": cli["label"], "color": pr.get("color", ""), "icon": pr.get("icon", "")}
            if name == "skill" and args:
                sk = self.skills.all().get(args.get("name"), {})
                ev["skill"] = {"color": sk.get("color", ""), "icon": sk.get("icon", "")}
            prepared.append((tc, name, args, tid, ev))
        results = {}
        par = len(prepared) > 1 and all(n in READONLY_TOOLS and a is not None for _, n, a, _, _ in prepared)
        if par:
            for p in prepared:
                self.emit(p[4])
            ths = []
            for tc, name, args, tid, _ in prepared:
                def work(tc=tc, name=name, args=args, tid=tid):
                    t1 = time.time()
                    try:
                        results[tid] = self._exec(name, args, tid, pr) + (time.time() - t1,)
                    except Exception as e:  # incl. Cancelled: every call still gets a result
                        results[tid] = (False, "cancelled" if isinstance(e, Cancelled) else f"error: {type(e).__name__}: {e}", None, time.time() - t1)
                th = threading.Thread(target=work, daemon=True)
                th.start()
                ths.append(th)
            for th in ths:
                th.join()
        out = []
        for tc, name, args, tid, ev in prepared:
            if not par:
                if self.cancel.is_set():
                    results[tid] = (False, "cancelled", None, 0)
                else:
                    self.emit(ev)
                    t1 = time.time()
                    results[tid] = self._exec(name, args, tid, pr) + (time.time() - t1,)
            ok, res, img, dt = results[tid]
            self.session["messages"].append({"role": "tool", "tool_call_id": tid, "content": res})
            self.emit({"type": "tool_end", "id": tid, "ok": ok, "output": res, "time": dt})
            self.checkpoint()
            out.append((tc, (ok, res, img)))
        return out

    def _exec(self, name, args, tid, pr):
        """Checks + runs one tool. Returns (ok, output, image)."""
        temit = lambda e, _id=tid: self.emit({"type": "tool_event", "id": _id, **e})
        if not isinstance(args, dict):
            return False, "error: arguments were not valid JSON — send a JSON object", None
        if name in ("write", "edit", "read") and not isinstance(args.get("path"), str):
            return False, "error: path must be a string", None
        if name not in (getattr(self, "_tool_names", None) or {t["name"] for t in self.tool_schemas()}):
            return False, f"error: tool {name} is not available in the {pr['name']} agent", None
        if name in ("write", "edit") and not may_write(pr, os.path.relpath(self.abspath(args.get("path", "")), self.cwd)):
            return False, f"error: the {pr['name']} agent may only write {pr.get('writes') or 'nothing'}", None
        veto = PLUGINS.emit("before_tool", name, args)
        if isinstance(veto, str):
            return False, veto, None
        if name == "todo":
            return self._todo(args)
        if name == "memory":
            return self._memory(args)
        if name == "task":
            return self._task(args, temit)
        cli = (pr.get("cli") or {}).get(name)
        if cli:
            return self._cli(cli, args, temit)
        need = self.tools.needs_confirm(name, dict(args, path=self.abspath(args["path"])) if isinstance(args.get("path"), str) else args, self.cwd)
        if isinstance(need, str):
            return False, need, None
        if need:
            ans = self.confirm(name, args)
            if not ans:
                return False, "user denied this action. ask what to do instead.", None
            if ans == "always" and name == "bash":
                st = self.s.config["settings"]
                st["allow"] = list(st.get("allow") or DEFAULT_ALLOW) + [allow_pattern(args.get("cmd", ""))]
                self.s.save()
        args = self.remap(args)
        if name in ("write", "edit"):
            self._snapshot(self.abspath(args.get("path", "")))
        try:
            return self.tools.run(name, args, temit, self.cancel, self.ask, self.cwd)
        except Cancelled:
            raise
        except Exception as e:
            return False, f"error: {type(e).__name__}: {e}", None

    def _cli(self, spec, args, temit):
        """An agent's own CLI tool: fill the template, stream the output as status lines, report exit code."""
        cmd, err = render_cli(spec, args)
        if err:
            return False, err, None
        need = self.tools.needs_confirm("bash", {"cmd": cmd}, self.cwd)
        if isinstance(need, str):
            return False, need, None
        if need and not self.confirm("bash", {"cmd": cmd}):
            return False, "user denied this action. ask what to do instead.", None
        temit({"t": "status", "text": "$ " + cmd[:120]})
        n = [0]

        last = [0.0]

        def line(l):
            n[0] += 1
            if time.time() - last[0] > 0.1:  # throttle: chatty commands must not flood the event log
                last[0] = time.time()
                temit({"t": "status", "text": l.strip()[:160]})
                temit({"t": "progress", "v": None, "label": f"{n[0]} lines", "n": n[0]})
        out = run_shell(cmd, int(args.get("timeout") or 300), self.cancel, shell_env(self.s.config.get("secrets")), on_line=line, cwd=self.cwd,
                        shell=self.s.config["settings"].get("shell", ""))
        return "[exit " not in out[-12:], out or "(no output)", None

    def _memory(self, a):
        try:
            if a.get("forget"):
                mem_forget(self.s.home, a["forget"])
                self.s.save()
                return True, f"forgot note {a['forget']}", None
            if not str(a.get("text", "")).strip():
                return False, "error: give text (to add) or forget (an id)", None
            mid = mem_add(self.s.home, a["text"], source=self.session.get("title", ""))
            self.s.save()
            return True, f"remembered ({mid}) — it will be in every future chat", None
        except ValueError as e:
            return False, f"error: {e}", None

    # ─── todo / goal ───
    def _todo(self, a):
        items = []
        for i in a.get("items") or []:
            if isinstance(i, dict):  # tolerate {t, s} / {text, done}
                t, st = str(i.get("t") or i.get("text") or ""), i.get("s") or ("done" if i.get("done") else "todo")
            else:
                t = str(i).strip()
                m = re.match(r"^(?:\[(x|X|>| )\]|(x|X|>|✓|-))\s+", t)
                mark = (m.group(1) or m.group(2)) if m else ""
                t, st = (t[m.end():] if m else t), {"x": "done", "X": "done", "✓": "done", ">": "doing"}.get(mark, "todo")
            if t.strip():
                items.append({"t": t.strip()[:200], "s": st if st in ("todo", "doing", "done") else "todo"})
        self.session["todo"] = items[:50]
        self.emit({"type": "todo", "items": self.session["todo"]})
        n = sum(1 for i in items if i["s"] == "done")
        nxt = next((i["t"] for i in items if i["s"] != "done"), None)
        return True, f"todo: {n}/{len(items)} done" + (f"; next: {nxt}" if nxt else "; all done — verify, then report"), None

    def goal(self, objective, max_rounds=None):
        """/goal: work autonomously until the objective is verifiably met or blocked. Binary result."""
        max_rounds = int(max_rounds or self.s.config["settings"].get("goal_rounds") or 12)
        g = self.session["goal"] = {"text": objective, "status": "working", "reason": "", "round": 0}
        self.emit({"type": "goal", **g})
        prompt = GOAL_PROMPT.format(objective=objective)
        for r in range(1, max_rounds + 1):
            g["round"] = r
            self.run(prompt)
            last = next((m for m in reversed(self.session["messages"]) if m["role"] == "assistant"), {})
            verdict = re.search(r"GOAL:\s*(DONE|BLOCKED)\b[:\s-]*(.*)", last.get("content") or "", re.S | re.I)
            if self.cancel.is_set() or (self.session["messages"] and self.session["messages"][-1].get("content") == "[interrupted by user]"):
                g.update(status="blocked", reason="interrupted by user")
                break
            if verdict:
                g.update(status=verdict.group(1).lower(), reason=verdict.group(2).strip()[:600])
                break
            prompt = ("Continue toward the goal. Check your todo list, do the next step, verify. When the success criteria pass, "
                      "reply GOAL: DONE <evidence>; if you cannot proceed, GOAL: BLOCKED <reason>.")
        else:
            g.update(status="blocked", reason=f"no verified result after {max_rounds} rounds")
        self.emit({"type": "goal", **g})
        if self.persist:
            self.s.save_session(self.session)
        return g

    # ─── subagents ───
    def _task(self, a, temit):
        name = a.get("agent") or "code"
        if name not in presets(self.s):
            return False, f"error: no agent '{name}'. agents: {', '.join(presets(self.s))}", None
        report = {"text": ""}

        def sub_emit(e):
            t = e["type"]
            if t == "tool_start":
                temit({"t": "status", "text": f"{name} › {e['name']} {e.get('label', '')[:60]}"})
                temit({"t": "log", "text": f"{e['name']} {e.get('label', '')[:80]}"})
            elif t == "text":
                report["text"] += e["text"]
            elif t == "tool_end":
                report["text"] = ""
            elif t == "todo":
                done = sum(1 for i in e["items"] if i["s"] == "done")
                temit({"t": "progress", "v": done / max(1, len(e["items"])), "label": f"{done}/{len(e['items'])}"})
            elif t == "error":
                temit({"t": "log", "text": "✗ " + e["text"]})
        sub = Agent(self.s, sub_emit, self.confirm, self.ask)
        sub.depth, sub.persist = self.depth + 1, False
        sub.cancel = self.cancel
        sub.session = new_session(self.session.get("cwd"))
        sub.session["agent"] = name
        sub.session["roots"] = list(self.session.get("roots") or [])
        temit({"t": "status", "text": f"{name} agent started"})
        sub.run(a.get("prompt", "") + "\n\nYou are a subagent: do the job, verify it, then reply with a short report "
                                      "(what you did, files touched, results). The report is all the caller sees.")
        for k in ("in", "out", "cost"):
            self.session["usage"][k] = self.session["usage"].get(k, 0) + sub.session["usage"].get(k, 0)
        self.snap.update({k: v for k, v in sub.snap.items() if k not in self.snap})
        last = next((m.get("content") for m in reversed(sub.session["messages"]) if m["role"] == "assistant" and m.get("content")), "")
        return bool(last), clip(last or report["text"] or "(subagent returned nothing)", 6000), None

    # ─── checkpoints / undo ───
    def _snapshot(self, path):
        p = self.abspath(path)
        if p not in self.snap:
            try:
                self.snap[p] = Path(p).read_text("utf-8") if os.path.isfile(p) else None
            except (OSError, UnicodeDecodeError):
                pass

    def _finish_checkpoint(self, start_idx):
        if not self.snap or not self.persist:
            return
        files = dict(self.snap)
        gen = self.session.get("compactions", 0)
        # history was compacted during this turn: message indexes moved, so undo restores files but keeps the history
        msgs = start_idx if gen == getattr(self, "_turn_gen", gen) else -1
        Checkpoints(self.s, self.session["id"]).push({"msgs": msgs, "gen": gen, "files": files, "t": time.time()})
        diffs = [{"path": os.path.relpath(p, self.session.get("cwd") or "."), "diff": file_diff(p, old), "new": old is None}
                 for p, old in files.items()]
        self.emit({"type": "turn_diff", "files": [d for d in diffs if d["diff"]]})

    def undo(self):
        """Restore the files the last turn changed and drop that turn from history."""
        if self.busy:
            return {"ok": False, "text": "busy"}
        cp = Checkpoints(self.s, self.session["id"]).pop()
        if not cp:
            return {"ok": False, "text": "nothing to undo"}
        restore_files(cp["files"])
        if cp.get("gen", 0) == self.session.get("compactions", 0) and 0 <= cp["msgs"] <= len(self.session["messages"]):
            self.session["messages"] = self.session["messages"][:cp["msgs"]]
        self.s.save_session(self.session)
        names = [os.path.relpath(p, self.session.get("cwd") or ".") for p in cp["files"]]
        ev = {"type": "undone", "files": names, "text": f"restored {len(names)} file{'s' if len(names) != 1 else ''}: " + ", ".join(names[:8])}
        self.emit(ev)
        return dict(ev, ok=True)

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
        if self.persist and time.time() - getattr(self, "_saved", 0) >= every:
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
            if k == "path" and isinstance(v, str):  # only the path itself: never rewrite file content or commands
                for r in sorted(roots, key=len, reverse=True):
                    if v == r or v.startswith(r.rstrip("/\\") + os.sep) or v.startswith(r.rstrip("/\\") + "/"):
                        v = cwd + v[len(r.rstrip("/\\")):]
                        break
            out[k] = v
        return out

    def load(self, sid):
        d = jload(self.s.session_path(sid))
        if d:
            self.session = d
            self.session.setdefault("usage", {"in": 0, "out": 0})
            self.ctx_used = self.session.get("ctx", 0)
        return d
