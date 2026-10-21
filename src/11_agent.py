# ───────────────────────────── agent ─────────────────────────────

SYSTEM = """You are me, an expert software engineer working in the user's terminal.
cwd: {cwd} | os: {os} | shell: {shell} | date: {date}
Keep going until the task is fully done: act with tools, don't stop at a plan, an announcement or a half-fix; if a call fails, fix the cause and go on.

Scope:
- Do exactly what was asked, nothing more. Match effort to the request: a question gets an answer, a greeting a short reply, a one-line fix a one-line edit.
- Questions and chat need no tools unless the answer depends on files, commands or current facts. Never create, edit or delete files, install packages or run commands the request does not need.
- No refactors, renames, features, docs or tests nobody asked for; mention worthwhile extras in one line.
- "Like X" / "a clone of X": keep X's genre, core mechanics and controls; don't swap in a different kind of thing.
- If the request is unclear in a way that changes the result, ask one short question; otherwise pick the obvious reading and go. Once you start a task, finish all of it.

Tools:
- Explore with ls (glob), grep and read, not shell cat/find/grep. Put independent reads/searches in one message: they run in parallel.
- Read a file before editing it. edit: copy old exactly from read output without the `N│` prefix, short but unique; several changes to one file in one call (edits=[…]). If an edit fails, read that part again first. write only for new files or full rewrites.
- bash is non-interactive (pass -y / --yes, CI=1); servers and watchers need background=true.
- Never undo or delete work you did not make; no destructive commands (git reset --hard, rm -rf on broad paths, force push) unless asked; confirm anything irreversible.
- <system-reminder> text comes from the harness, not the user: follow it, don't reply to it or mention it.

Work:
- Never guess what code, an API or a command does: check it. Find the root cause before fixing; read errors fully.
- A failed approach: say what you learned and change it; never repeat the same failing call.
- 3+ steps: write a todo first, then mark each step done right after finishing it (todo done=[n]) and the next as doing. The user watches it live.
- Match the project's style; no hacks, no needless deps.
- After changing code, verify it (tests, build, lint or run it) and fix failures. Never claim untested success.
- web_search for current facts, web_fetch for docs/URLs.
UI work: no emoji; SVG icons; neutral palette + one accent; light+dark; empty/loading/error states; responsive.
Reply: in the user's language, concise. After changes: what changed, how you verified it, and anything left undone.{extra}{skills}{project}{user}"""



# Behavior switches (/behavior, web /behavior): one checkbox each, applied to the system prompt and the loop.
# key: (default, label, prompt line when on, prompt line when off)
BEHAVIOR = {
    "tests": (True, "verify changes itself (tests, build, run)", "",
              "Self-testing is OFF: the user tests everything. Never run tests, builds, linters, type checks, the app, servers, browsers "
              "or screenshots to check your work, never install tools for checking (puppeteer, playwright…), and put no checking step "
              "in your plan. Since nothing gets checked, prefer setups that run as written (no build step); never serve uncompiled "
              "TypeScript/JSX with a static server: start the project's dev server. When done, list the exact commands or steps for the user to check."),
    "todo": (True, "keep a todo checklist on multi-step tasks", "", ""),
    "web": (True, "search the web for current facts and docs", "", ""),
    "stay_in_project": (True, "stay in the project folder unless asked",
                        "Work inside cwd; don't explore other folders (parents, siblings, home) for anything helpful. Go outside only "
                        "when the user asks or names a path.", ""),
    "plan_first": (False, "show a plan and wait for a go before big changes",
                   "Before any change bigger than a small fix (several files, new features, refactors), give a short plan and wait for "
                   "the user's go.", ""),
    "brief": (False, "very short replies",
              "Be terse: the fewest words that answer; after changes one line on what changed. No recaps, no lists of options.", ""),
    "explain": (False, "explain the why (learning mode)",
                "The user is learning: explain the reasoning behind each change and any concept they may not know, briefly.", ""),
    "commit": (False, "git commit each finished task",
               "In a git repo, when a task is done (and verified, if you verify), commit just its changes with a clear message. "
               "Never push unless asked.", ""),
    "agents_md": (True, "offer to create AGENTS.md in new projects", "", ""),
    "minimal_diff": (False, "change only what the task needs",
                     "Change only what the task needs: no refactors, renames, reformatting or extra improvements of code you were not "
                     "asked to touch. Mention other problems you notice instead of fixing them.", ""),
    "no_new_deps": (False, "ask before adding dependencies",
                    "Don't add dependencies (packages, libraries, CDN scripts) without asking: use what the project already has or the "
                    "standard library.", ""),
    "ask_unclear": (False, "ask when the request is unclear",
                    "If a request is ambiguous or misses a key detail, ask one short question before starting instead of guessing.", ""),
    "questions": (False, "ask all the questions it needs before starting",
                  "Questions mode: before starting a task, ask every question that matters — as many as it takes, numbered, each with "
                  "your suggested default — and wait for the answers. Ask again when a new decision comes up. Never guess what the user "
                  "can decide; small technical details you may choose yourself.", ""),
    "subagents": (True, "use subagents for big separate jobs", "", ""),
    "memory": (True, "global memory across chats", "", ""),
    "skills": (True, "load matching skills before a task", "", ""),
    "repo_map": (True, "show a project map (files and symbols) with the first task", "", ""),
    "suggest": (True, "suggest the next message (tab to accept)", "", ""),
    "comments": (False, "comment the code it writes",
                 "Add short comments to code you write wherever the why is not obvious; keep the project's comment style.", ""),
    "security": (False, "watch for security issues",
                 "Watch for security issues in code you write or touch (injection, secrets in code, unsafe input handling, weak auth) "
                 "and point them out briefly.", ""),
    "publish": (False, "publish static sites at a public link (me cloud)", "", ""),
    "summary": (False, "end with a short recap and next steps",
                "End every finished task with a short recap: what was done, then 1-3 sensible next steps.", ""),
}


NO_CHECKS = ("error: not run — the user turned self-testing off (/behavior). Do not test, build, lint or run the app on your own; "
             "finish the work and tell the user which commands to run to check it (or that they can ask you to run it).")
# the user's own request to run or check something lifts the self-testing switch for that request
ASKED_RUN_RX = re.compile(r"(?:\b(?:run|start|launch|serve|test|build|lint|check|screenshot)\b|запус|тест|провер|собер|скрин|открой)", re.I)
# what "checking your work" looks like on the command line: blocked outright when self-testing is off
CHECK_RX = re.compile(r"(?:^|[\s;&|(])(?:pytest|py\.test|unittest|tox|nox|jest|vitest|mocha|karma|cypress|playwright|puppeteer|"
                      r"chromium(?:-browser)?|google-chrome|headless|screenshot|lighthouse|eslint|tsc|mypy|ruff|flake8|pylint|"
                      r"(?:npm|pnpm|yarn|bun)\s+(?:run\s+)?(?:test|lint|build|check|typecheck)|go\s+(?:test|vet|build)|cargo\s+(?:test|build|check|clippy)|"
                      r"dotnet\s+(?:test|build)|mvn|gradle\w*|make\s+(?:test|check)|node\s+--check|node\s+(?:\S*/)?(?:check|test|verify|smoke)(?:[-_.]\w+)?\.[cm]?js|py_compile|vite\s+(?:build|preview)|--headless)\b", re.I)


def behavior(settings):
    """Effective switches: defaults, then the user's choices (settings.behavior); the old no_verify setting means tests off."""
    b = {k: v[0] for k, v in BEHAVIOR.items()}
    if settings.get("no_verify"):
        b["tests"] = False
    b.update({k: bool(v) for k, v in (settings.get("behavior") or {}).items() if k in BEHAVIOR})
    return b


class LimitHit(Exception):
    pass


def parse_args(raw):
    """Tool-call arguments → dict, or None. Tolerates what models commonly get wrong: a code fence around the JSON,
    text after it, a JSON object sent as a string."""
    raw = (raw or "").strip() or "{}"
    for cand in (raw, re.sub(r"^```(?:json)?\s*|\s*```$", "", raw)):
        try:
            v = json.JSONDecoder().raw_decode(cand)[0]
        except ValueError:
            continue
        if isinstance(v, str):
            try:
                v = json.loads(v)
            except ValueError:
                continue
        if isinstance(v, dict):
            return v
    return None


TRUNCATED = object()  # marker: tool arguments cut off by the output limit
LOOP_NUDGE = ("[you made the same call three times and got the same result — it will not change. Try a different approach, "
              "or stop and tell the user what blocks you]")
THINK_NUDGE = "[your reasoning used up the output token limit before you answered — think less and act or answer now]"
CUTOFF_NUDGE = "[your reply was cut off by the output token limit — continue exactly where it stopped, without repeating]"
REPEAT_NUDGE = "[your reply started repeating itself and was stopped — do not repeat it; continue the task with tools, or give the final answer]"
EMPTY_NUDGE = "[your last reply was empty — continue: call the next tool with valid JSON arguments, or answer]"
INTENT_NUDGE = "[self-check] You said what you would do next but made no tool call, so nothing happened. Do it now, or say why you stopped."
INTENT_RX = re.compile(r"(?:\b(?:let me(?! know)|let's|now i'll|next,? i'll|i'll now|i'm going to)\b|(?:^|\s)(?:давай(?:те)?|приступаю|"
                       r"(?:сейчас|теперь|далее|затем)\s+(?:я\s+)?\w+(?:у|ю|ем|им)\b))[^\n]{0,160}$|[^\n]{8,}:\s*$", re.I)
DOC_EXT = r"\.(md|markdown|txt|rst|adoc|csv)$"


ARG_ALIASES = {  # names other agents use for the same argument: models trained on them send these
    "bash": {"command": "cmd", "script": "cmd", "commands": "cmd", "shell": "cmd", "cwd": "workdir", "dir": "workdir", "directory": "workdir"},
    "todo": {"todos": "items", "plan": "items", "steps": "items", "tasks": "items", "list": "items"},
    "read": {"file_path": "path", "filepath": "path", "file": "path", "filename": "path", "start_line": "offset", "line_start": "offset", "from_line": "offset"},
    "write": {"file_path": "path", "filepath": "path", "file": "path", "filename": "path", "text": "content", "contents": "content"},
    "edit": {"file_path": "path", "filepath": "path", "file": "path", "filename": "path", "old_string": "old", "new_string": "new",
             "old_str": "old", "new_str": "new", "replace_all": "all", "old_text": "old", "new_text": "new"},
    "grep": {"query": "pattern", "regex": "pattern", "search": "pattern", "include": "glob"},
    "ls": {"path": "pattern", "glob": "pattern", "dir": "pattern", "directory": "pattern"},
    "web_search": {"q": "query", "search": "query"},
    "web_fetch": {"link": "url", "href": "url"},
}
TOOL_ALIASES = {  # tool names from Claude Code, Codex, opencode and others: models trained there call them by habit
    "shell": "bash", "exec_command": "bash", "run_command": "bash", "run_shell_command": "bash", "terminal": "bash", "execute": "bash",
    "read_file": "read", "view": "read", "cat": "read", "open_file": "read",
    "write_file": "write", "create_file": "write", "str_replace": "edit", "str_replace_editor": "edit", "multiedit": "edit",
    "replace": "edit", "edit_file": "edit", "glob": "ls", "find": "ls", "list_files": "ls", "list_dir": "ls", "search": "grep",
    "search_files": "grep", "todowrite": "todo", "update_plan": "todo", "update_todos": "todo", "write_todos": "todo",
}


def fix_args(name, args, schema):
    """Rename well-known argument aliases, then name any required argument still missing (instead of a KeyError)."""
    if len(args) == 1 and isinstance(next(iter(args.values())), dict) and next(iter(args)) in ("input", "args", "arguments", "parameters", "params"):
        args = next(iter(args.values()))  # {"input": {...}}: arguments wrapped one level too deep
    al = ARG_ALIASES.get(name) or {}
    args = {al.get(k, k) if al.get(k, k) not in args else k: v for k, v in args.items()}
    if name == "read":  # end_line / line_end: read only up to there instead of 2000 lines
        end = next((args.pop(k) for k in ("end_line", "line_end", "to_line", "end") if k in args), None)
        try:
            if end is not None and "limit" not in args:
                args["limit"] = max(1, int(end) - int(args.get("offset") or 1) + 1)
        except (TypeError, ValueError):
            pass
    if name == "bash" and isinstance(args.get("cmd"), list):  # Codex sends argv: ["bash", "-lc", "npm test"]
        argv = [str(x) for x in args["cmd"]]
        if len(argv) >= 3 and os.path.basename(argv[0]) in ("bash", "sh", "zsh") and argv[1] in ("-c", "-lc"):
            args["cmd"] = argv[-1]
        else:
            args["cmd"] = " ".join(x if re.fullmatch(r"[\w@%+=:,./-]+", x) else shlex.quote(x) for x in argv)
    req = [r for r in ((schema or {}).get("parameters") or {}).get("required", []) if args.get(r) in (None, "")]
    if req:
        props = list(((schema or {}).get("parameters") or {}).get("properties", {}))
        return args, f"error: {name} needs {', '.join(req)} (its arguments: {', '.join(props)}; you sent: {', '.join(args) or 'nothing'})"
    return args, None


def has_images(messages):
    return any(isinstance(m.get("content"), list) and any(x.get("type") == "image_url" for x in m["content"]) for m in messages)


def tc_args(tc):
    return parse_args(tc["function"]["arguments"])


TODO_START = ("[reminder] This is a multi-step task and you have no checklist. Write it now with todo (items = the whole plan, "
              "finished steps marked), then keep it current as each step ends.")
TODO_EVERY = 8  # tool rounds without a todo update (while steps are open) before the checklist is shown again


TODO_STATE = {"x": "done", "✓": "done", "done": "done", "completed": "done", "complete": "done", "finished": "done",
              ">": "doing", "doing": "doing", "in_progress": "doing", "in progress": "doing", "active": "doing", "current": "doing",
              " ": "todo", "todo": "todo", "pending": "todo", "open": "todo", "not_started": "todo"}
TODO_DONE_RX = re.compile(r"\s*[—–-]+\s*(?:done|готово|сделано|выполнено)\W*$", re.I)


def todo_parse(x, st=None):
    """todo items in whatever shape a model sends: "x step" / "[>] step" / "step — DONE", {t|text|title|task: …, s|status: …,
    done: true}, {"x": "step"}, nested lists. → [(text, state or None)]; None = no mark given."""
    if isinstance(x, (list, tuple)):
        return [r for i in x for r in todo_parse(i, st)]
    if isinstance(x, dict):
        mark = x.get("s") or x.get("status") or x.get("state") or ("done" if x.get("done") in (True, "true") else None)
        mark = TODO_STATE.get(str(mark).strip().lower()) if mark else st
        for k in ("t", "text", "title", "task", "step", "content", "name", "item", "items", "description"):
            if x.get(k):
                return todo_parse(x[k], mark)
        return [r for k, v in x.items() if isinstance(v, str) for r in todo_parse(v, TODO_STATE.get(k.split()[0].lower() if k.strip() else " "))]
    t = str(x).strip()
    m = re.match(r"^(?:\[(x|X|>| |✓)\]|(x|X|>|✓|-))\s+", t)
    if m:
        t, st = t[m.end():], TODO_STATE.get((m.group(1) or m.group(2)).lower()) or st
    if TODO_DONE_RX.search(t):
        t, st = TODO_DONE_RX.sub("", t), "done"
    return [(t.strip(), st)] if t.strip() else []


# self-testing off: plan steps that only check the work (the user does that) are dropped unless the request asked for them
CHECK_STEP_RX = re.compile(r"^\W*(?:(?:run |write |add |прогнать |запустить |написать )?(?:tests?|testing|unit[- ]tests?|smoke[- ]test|qa|тест\w*|протестир\w*)\b"
                           r"|(?:verify|verification|validate|check|final check|проверк\w*|провер(?:ить|яем|яю)|убедиться)\b(?:\W+\w+){0,2}\W*$"
                           r"|(?:verify|verification|validate|check|проверк\w*|провер(?:ить|яем|яю)|убедиться)\b.*\b(?:works?|working|result|everything|all|"
                           r"the app|build|browser|lint|работ\w*|результат\w*|всё|все|сборк\w*|приложени\w*|браузер\w*|запуск\w*))", re.I)
ASKS_CHECK_RX = re.compile(r"test|тест|провер|verif|check|lint|линт", re.I)


def todo_lines(items):
    return "\n".join(f"{n}. [{'x' if i['s'] == 'done' else '>' if i['s'] == 'doing' else ' '}] {i['t']}" for n, i in enumerate(items, 1))


ACT_NUDGE = ("[self-check] You answered with code but changed no files, and the user asked for a change in the project. Make it now with "
             "write/edit (do not paste the code again). If they only wanted to see code, say so in one line.")
ACT_RX = re.compile(r"\b(?:create|write|add|implement|fix|make|change|update|refactor|edit|build|"
                    r"созда\w*|напиши|напис\w*|добав\w*|реализ\w*|исправ\w*|сделай|измени|поправь|обнови)\b", re.I)
FILE_RX = re.compile(r"\b[\w./-]+\.(?:py|js|mjs|ts|tsx|jsx|go|rs|java|c|cc|cpp|h|hpp|cs|rb|php|html|css|scss|json|sh|ps1|ya?ml|toml|sql|kt|swift|vue|svelte|lua|gd|dart)\b")


def should_have_acted(request, reply):
    """A first reply that is all talk: the user asked to create/change a named file, the model pasted code instead of calling a tool."""
    return bool(ACT_RX.search(request or "") and FILE_RX.search(request or "") and re.search(r"```[^\n]*\n(?:.*\n){3,}?```", reply or ""))


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
        self.inbox, self._driving, self._inbox_lock = [], False, threading.Lock()  # messages sent while a turn runs (submit)
        self.take_queued = self.take_inbox  # frontend hook: messages the user sent mid-turn, read after every tool step
        self.suggest = False  # interactive frontends turn it on: after a turn, a guess at the user's next message (event "suggest")

    def system_prompt(self):
        sks = self.skills.enabled()
        skills = ("\nSkills (skill tool: name alone loads the guide — load the one that fits before starting a task it covers; name + action + args runs a tool skill — not shell commands):\n" + "\n".join(f"- {k}: {v['description']}" for k, v in sks.items())) if sks else ""
        project, budget = "", 24000
        for name in self.context_files():
            text = (Path(self.session["cwd"]) / name).read_text("utf-8", "replace")[:min(10000, budget)]
            budget -= len(text)
            project += f"\n\n# {name}\n{text}"
        bh = behavior(self.s.config["settings"])
        if "AGENTS.md" in self.context_files() or any(n.endswith("/AGENTS.md") for n in self.context_files()):
            project += "\n\nAGENTS.md is this project's memory: update it when you learn something durable (commands, structure, conventions, decisions)."
        elif bh["agents_md"]:
            project += ("\n\nNo AGENTS.md here yet. Only during real multi-step work on a code project (not for questions or small tasks), "
                        "you may offer to create a concise AGENTS.md (purpose, stack, run/test commands, structure, conventions).")
        repo = os.environ.get("ME_GIT_REPO")
        if repo:
            base = os.environ.get("ME_GIT_BASE") or "main"
            project += (f"\n\nGitHub mode: this folder is a clone of {repo} (base branch {base}); git push works with the user's token. "
                        "Do the whole job yourself: if .me/setup.sh exists, run it once before the first build or test. "
                        f"For a change: git fetch origin, then work on a branch me/<short-topic> from origin/{base} (keep it for follow-ups in this chat), "
                        "commit in clear steps, git push -u origin <branch>, open a PR with the github skill (open_pr) and give its link. "
                        f"Then check CI (pr_checks) and fix failures. Never push to {base} or force-push; merge (merge_pr) only when the user asks.")
        pr = self.preset()
        if pr.get("prompt"):
            project += "\n\n" + pr["prompt"].replace("{home}", str(self.s.home))
        if pr.get("cli"):
            project += "\nYour CLI tools (prefer them over hand-written shell for these jobs): " + ", ".join(f"{n} — {c['desc']}" for n, c in pr["cli"].items())
        user = ("\n\n" + self.s.config["prompt"].strip()) if self.s.config.get("prompt", "").strip() else ""
        user += "".join("\n\n" + x for x in PLUGINS.collect("system_prompt", self))
        extra = ""
        mem = mem_prompt(self.s.home) if bh["memory"] else ""
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
        prefs = [BEHAVIOR[k][2 if on else 3] for k, on in bh.items() if BEHAVIOR[k][2 if on else 3]]
        if prefs:
            extra += "\nUser's settings (they override the rules above):\n" + "\n".join("- " + x for x in prefs)
        base = SYSTEM
        if not bh["tests"]:
            base = base.replace("- After changing code, verify it (tests, build, lint or run it) and fix failures. Never claim untested success.\n", "")
            base = base.replace("how you verified it", "how the user can check it")
            project = re.sub(r", (?:verify|rerun to prove it)(?=\.)", "", project)
        if not bh["todo"]:
            base = base.replace("- 3+ steps: write a todo first, then mark each step done right after finishing it (todo done=[n]) and the next as doing. The user watches it live.\n", "")
        return base.format(cwd=self.session["cwd"], os=f"{platform.system()} {platform.release()}", date=time.strftime("%Y-%m-%d"),
                             shell=shell_note(self.s.config["settings"].get("shell")),
                             extra=extra, skills=skills, project=project, user=user)

    def preset(self):
        return preset(self.s, self.session.get("agent") or self.s.config.get("agent"))

    def model_name(self):
        """Each chat keeps the model it was started with: switching models in one chat (or terminal) never changes another.
        A new chat starts with the model picked last."""
        return self.session.get("model") or self.preset().get("model") or self.s.config.get("model")

    def model(self):
        return self.s.model(self.model_name())

    def set_model(self, name):
        """Switch this chat's model; it also becomes the default for new chats."""
        self.session["model"] = self.s.config["model"] = name
        self.s.save()
        if self.persist and self.session.get("messages"):
            self.s.save_session(self.session)

    @property
    def cwd(self):
        return self.session.get("cwd") or os.getcwd()

    def abspath(self, path):
        """A tool path as the model meant it: relative to the session's project folder, ~ expanded."""
        return tool_path(self.cwd, path)

    def context_files(self):
        """Project instructions: CONTEXT_FILES in cwd, plus AGENTS.md / CLAUDE.md of parent folders up to the repo root
        (a monorepo's root rules apply in its subfolders). Parents come first; names are relative to cwd."""
        cwd = Path(self.session.get("cwd") or ".").resolve()
        out = [n for n in CONTEXT_FILES if (cwd / n).is_file()]
        if (cwd / ".git").exists():
            return out
        up, d, home = [], cwd, Path.home().resolve()
        for _ in range(6):
            if d.parent == d or d == home:
                break
            d = d.parent
            rel = os.path.relpath(d, cwd)
            up = [os.path.join(rel, n).replace("\\", "/") for n in ("AGENTS.md", "CLAUDE.md") if (d / n).is_file()][:1] + up
            if (d / ".git").exists():
                break
        return up + out

    def tool_schemas(self):
        allowed = preset_tools(self.preset())
        if self.depth:
            allowed.discard("task")
        bh = behavior(self.s.config["settings"])
        if not bh["todo"]:
            allowed.discard("todo")
        if not bh["web"]:
            allowed -= {"web_search", "web_fetch"}
        if not bh["subagents"]:
            allowed.discard("task")
        if not bh["memory"]:
            allowed.discard("memory")
        out = [t for t in TOOLS if t["name"] in allowed]
        if self.s.auth.get("token"):
            out += [NOTIFY_TOOL, LATER_TOOL] + ([PUBLISH_TOOL] if bh["publish"] else [])
        if self.s.config.get("mcp"):
            out.append(MCP_TOOL)
        pr = self.preset()
        out += [cli_schema(n, c, pr["name"]) for n, c in (pr.get("cli") or {}).items()]
        return out + PLUGINS.tool_schemas()

    MENTION_RX = re.compile(r"(?<![\w/@])@([\w.~/\\-]*[\w/])(?::(\d+)(?:-(\d+))?)?")

    def mentions(self, text):
        """@path or @path:10-40 in a message attaches that file (or line range), like other agents do: the model starts
        with the code instead of spending a step on read. Returns (names, text) or None."""
        names, parts = [], []
        for mt in self.MENTION_RX.finditer(text or ""):
            p = self.abspath(mt.group(1))
            try:
                if len(names) >= 8 or not os.path.isfile(p) or os.path.getsize(p) > 2_000_000 or Path(p).suffix.lower() in IMAGE_EXT:
                    continue
                with open(p, "rb") as f:
                    data = f.read()
                if b"\0" in data[:4096]:
                    continue
                lines = decode_text(data)[0].splitlines()
            except (OSError, ValueError):
                continue
            a = int(mt.group(2) or 1)
            b = int(mt.group(3) or (mt.group(2) and a + 99) or len(lines))
            a, b = max(1, a), min(len(lines), b, a + 799)
            name = relp(p, self.cwd).replace(os.sep, "/") + (f":{a}-{b}" if mt.group(2) or b < len(lines) else "")
            if name in names:
                continue
            names.append(name)
            parts.append(f"[{name}]\n" + clip("\n".join(f"{i}\t{l}" for i, l in enumerate(lines[a - 1:b], a)), 40000))
        return (names, "[files the user attached with @]\n\n" + "\n\n".join(parts)) if names else None

    def submit(self, text, images=()):
        """Non-blocking send for web/RPC: runs now, or queues while a turn is working (event "queued"). Plain queued
        messages reach the model after the next tool step; the rest run as the next turns, in order."""
        with self._inbox_lock:
            if self._driving:
                self.inbox.append((text, list(images or ())))
                self.emit({"type": "queued", "text": text})
                return False
            self._driving = True
        threading.Thread(target=self._drive, args=(text, images), daemon=True).start()
        return True

    def _drive(self, text, images):
        while True:
            try:
                self.run(text, images)
            except Exception:
                log_error("turn")
            with self._inbox_lock:
                if not self.inbox or self.cancel.is_set():
                    self.inbox, self._driving = [], False
                    return
                text, images = self.inbox.pop(0)
            self.emit({"type": "queued_sent", "texts": [text]})

    def take_inbox(self):
        with self._inbox_lock:
            out = [t for t, im in self.inbox if not im]
            self.inbox = [x for x in self.inbox if x[1]]
            return out

    def run(self, text, images=()):
        """Blocking. Runs one user turn to completion; turns started from other threads wait for it."""
        with self._turn_lock:
            return self._run(text, images)

    def _run(self, text, images=()):
        pr = self.preset()
        m = self.model()
        if m and not self.session.get("model") and not pr.get("model"):
            self.session["model"] = m["name"]  # pinned: a model switched in another chat or terminal does not reach this one
        effort = self.session.get("effort") or pr.get("effort")
        if m and effort:
            m = dict(m, effort="" if effort == "none" else effort)
        if not m:
            self.emit({"type": "error", "text": "no model configured — use /provider then /models"})
            return
        self.busy = True
        if not self.depth:  # a subagent shares its parent's stop: a stop pressed just before it started must hold
            self.cancel.clear()
        self.snap = {}
        start_idx = len(self.session["messages"])
        self._turn_gen = self.session.get("compactions", 0)
        content = text if not images else [{"type": "text", "text": text}] + [{"type": "image_url", "image_url": {"url": u}} for u in images]
        hint = "\n\n".join(h for h in (self.map_hint(text), self.skill_hint(text)) if h)
        if hint:  # before the request, so the request stays the last thing the model reads
            self.session["messages"].append({"role": "user", "_aux": True, "_hint": True, "content": hint})
        self.session["messages"].append({"role": "user", "content": content})
        att = self.mentions(text)
        if att:
            self.session["messages"].append({"role": "user", "_aux": True, "content": att[1]})
            self.emit({"type": "notice", "level": "info", "text": "attached " + ", ".join(att[0])})
        if not self.session["title"]:
            self.session["title"] = text.strip().split("\n")[0][:60]
        self._trim_history(start_idx)
        t0, tin, tout, tcached, steps, cost = time.time(), 0, 0, 0, 0, 0.0
        wrote, ran, nudged, acted = False, False, False, False
        todo_used, todo_idle, todo_nudged, todo_started = False, 0, False, False
        squeezed, cutoffs, empties, seen, loops, compact_failed = False, 0, 0, [], 0, False
        finished = False
        self.emit({"type": "turn_start", "agent": pr["name"], "context": self.context_files()})
        prov = Provider(m, self.s.provider(m))
        st = self.s.config["settings"]
        try:
            while True:
                limit = self.compact_threshold(m)
                if limit and self.ctx_used > limit and not compact_failed:
                    compact_failed = not self.compact(reason="auto", model=m)  # a failed summary is not retried on every step
                schemas = self.tool_schemas()
                self._tool_names = {t["name"] for t in schemas}
                system = self.system_prompt()
                try:
                    msg, usage = prov.stream(system, self.session["messages"], schemas, self.emit, self.cancel, cache_key=self.session["id"])
                except ProviderError as e:
                    # the history outgrew the model: summarize the older part once and try again
                    if e.overflow and not squeezed and self.compact(reason="context full", model=m, keep_frac=0.15):
                        squeezed = True
                        continue
                    if not e.retryable and IMAGE_RX.search(str(e)) and has_images(self.session["messages"]):
                        # the model does not take images: keep the request, drop the pictures, tell the user why
                        self.session["messages"] = no_images(self.session["messages"], "[image removed: this model rejected images]")
                        self.emit({"type": "notice", "text": f"{m['name']} rejected the image ({str(e)[:160]}) — sent without it. "
                                                            "Pick a model with vision for images (/model), or set vision off in /models."})
                        continue
                    raise
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
                cut = msg.get("_finish") == "length"
                if not msg.get("content") and not msg.get("tool_calls"):  # nothing to keep: an empty assistant message breaks the history
                    if cut and cutoffs < 2:  # the whole output limit went into reasoning
                        cutoffs += 1
                        self.session["messages"].append({"role": "user", "_aux": True, "content": THINK_NUDGE})
                        continue
                    if msg.get("_finish") in ("content_filter", "safety", "SAFETY"):
                        raise ProviderError("the provider blocked the reply (safety filter) — rephrase the request or pick another model")
                    if empties < 2 and not cut:  # an empty reply (no text, no tool call; Gemini's malformed call): ask again, saying why
                        empties += 1
                        self.emit({"type": "retry", "attempt": empties, "of": 2, "delay": 0, "error": "empty reply", "discard": True})
                        if empties == 2 or self.session["messages"][-1].get("content") != EMPTY_NUDGE:
                            self.session["messages"].append({"role": "user", "_aux": True, "content": EMPTY_NUDGE})
                        continue
                    raise ProviderError("the model ran out of output tokens before answering — raise max_out or lower effort in /models" if cut
                                        else "the model returned an empty reply — try again, or pick another model (/model)")
                self.session["messages"].append(msg)
                self.checkpoint()
                if not msg.get("tool_calls"):
                    if cut and cutoffs < 2:  # the reply hit the output limit: let it carry on where it stopped
                        cutoffs += 1
                        self.session["messages"].append({"role": "user", "_aux": True, "content": CUTOFF_NUDGE})
                        continue
                    if msg.get("_finish") == "loop" and cutoffs < 2:  # cut for repeating itself: the task is not done
                        cutoffs += 1
                        self.session["messages"].append({"role": "user", "_aux": True, "content": REPEAT_NUDGE})
                        continue
                    if (not acted and self.depth == 0 and self._tool_names and not self.cancel.is_set()
                            and not (msg.get("content") or "").rstrip().endswith("?") and INTENT_RX.search((msg.get("content") or "").strip()[-220:])):
                        acted = True  # "Now let me update the tests:" and the turn ends: weak models stop right after announcing a step
                        self.session["messages"].append({"role": "user", "_aux": True, "content": INTENT_NUDGE})
                        continue
                    if steps == 1 and not acted and self.depth == 0 and "write" in self._tool_names and should_have_acted(text, msg.get("content")):
                        acted = True  # weak models sometimes "answer" a coding task with a code block: send them back to do it
                        self.session["messages"].append({"role": "user", "_aux": True, "content": ACT_NUDGE})
                        continue
                    open_steps = [i for i in self.session.get("todo") or [] if i["s"] != "done"]
                    left = [i.get("t") for i in open_steps]
                    if todo_used and open_steps and not todo_nudged and self.depth == 0 and left != getattr(self, "_todo_left", None):
                        todo_nudged, self._todo_left = True, left  # the plan says work is left: finish it, or leave the checklist true (once per set of open steps)
                        self.session["messages"].append({"role": "user", "_aux": True, "content": (
                            "[self-check] Your checklist still has open steps:\n" + todo_lines(self.session["todo"]) +
                            "\nMark finished ones done (todo done=[…]) and do the rest. If you stopped on purpose (blocked, or waiting "
                            "for the user's go), keep them open and say so.")})
                        continue
                    if wrote and not ran and not nudged and "bash" in self._tool_names and behavior(st)["tests"]:
                        nudged = True  # self-check before "done": changes were made but nothing was run since
                        self.emit({"type": "verify"})
                        self.session["messages"].append({"role": "user", "_aux": True, "content": VERIFY_NUDGE})
                        continue
                    finished = True
                    break
                self._limits(cost, st)
                results = self._run_calls(msg["tool_calls"], pr, cut)
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
                if any(tc["function"]["name"] == "todo" for tc, _ in results):
                    todo_used, todo_idle = True, 0
                elif (not todo_used and not todo_started and wrote and steps >= 4 and self.depth == 0 and "todo" in self._tool_names
                      and behavior(st)["todo"] and not any(i["s"] != "done" for i in self.session.get("todo") or [])):
                    todo_started = True  # models skip the checklist the prompt asks for: once per turn, once the work is clearly multi-step
                    self.session["messages"].append({"role": "user", "_aux": True, "content": TODO_START})
                elif todo_used and any(i["s"] != "done" for i in self.session.get("todo") or []):  # a list from an earlier task never nags
                    todo_idle += 1
                    if todo_idle >= TODO_EVERY and "todo" in self._tool_names:
                        todo_idle = 0  # models forget the checklist mid-task: show it again with what to do
                        self.session["messages"].append({"role": "user", "_aux": True, "content": (
                            "[reminder] Your checklist:\n" + todo_lines(self.session["todo"]) +
                            "\nIf a step is finished, mark it now: todo(done=[n], doing=next). A list left from an earlier task: replace it.")})
                sig = json.dumps([(tc["function"]["name"], tc["function"]["arguments"], r[1][:2000]) for tc, r in results])
                seen = (seen + [sig])[-6:]
                if seen.count(sig) >= 3:  # the same calls with the same results three times (also A-B-A-B-A): a loop
                    seen, loops = [], loops + 1
                    if loops >= 3:
                        raise LimitHit("stopped: the model kept repeating the same calls with the same results — rephrase the task or pick another model")
                    self.session["messages"].append({"role": "user", "_aux": True, "content": LOOP_NUDGE})
                if imgs and m.get("vision", True):
                    self.session["messages"].append({"role": "user", "_aux": True, "content": [{"type": "text", "text": "(images from read)"}] +
                                                     [{"type": "image_url", "image_url": {"url": u}} for u in imgs]})
                queued = self.take_queued() if self.depth == 0 else []
                if queued:  # the user wrote while tools ran: hand it to the model now instead of after the whole turn
                    self.emit({"type": "queued_sent", "texts": queued})
                    self.session["messages"].append({"role": "user", "_aux": True,
                                                     "content": "[message from the user while you were working — take it into account]\n"
                                                     + "\n\n".join(queued)})
        except Cancelled:
            self._fix_dangling()
            self.session["messages"].append({"role": "user", "_aux": True, "content": "[interrupted by user]"})
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
            if finished and self.suggest and self.depth == 0 and behavior(st)["suggest"]:
                threading.Thread(target=self.suggest_next, args=(m,), daemon=True).start()

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

    def _run_calls(self, calls, pr, cut=False):
        """Independent read-only calls run in parallel; anything that changes state runs in order."""
        prepared = []
        for tc in calls:
            name = tc["function"]["name"]
            if name not in (getattr(self, "_tool_names", None) or ()):
                k = re.sub(r"[^a-z_]", "", name.lower())
                name = tc["function"]["name"] = TOOL_ALIASES.get(k, k if k in (getattr(self, "_tool_names", None) or ()) else name)
            args = parse_args(tc["function"]["arguments"])
            if args is None and cut:
                args = TRUNCATED  # cut by the output limit mid-call: say so, or the model retries the same way
            if isinstance(args, dict):
                args = fix_args(name, args, None)[0]
            ui = args if isinstance(args, dict) else {}
            tid = tc["id"]
            ev = {"type": "tool_start", "id": tid, "name": name, "args": ui, "label": self.tools.preview(name, ui, self.cwd)}
            cli = (self.preset().get("cli") or {}).get(name)
            if cli:
                pr = self.preset()
                ev["cli"] = {"agent": pr["name"], "label": cli["label"], "color": pr.get("color", ""), "icon": pr.get("icon", "")}
            if name == "skill" and ui:
                sk = self.skills.all().get(ui.get("name"), {})
                ev["skill"] = {"color": sk.get("color", ""), "icon": sk.get("icon", "")}
            prepared.append((tc, name, args, tid, ev))
        results = {}
        par = len(prepared) > 1 and all(n in READONLY_TOOLS and isinstance(a, dict) for _, n, a, _, _ in prepared)
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
        if args is TRUNCATED:
            return False, ("error: your reply hit the output token limit, so these arguments were cut off and nothing ran. Send less per "
                           "call: write a big file in parts (write the first part, then add the rest with edit), or raise max_out in /models."), None
        if not isinstance(args, dict):
            return False, "error: arguments were not valid JSON — send a JSON object", None
        args, err = fix_args(name, args, next((t for t in self.tool_schemas() if t["name"] == name), None))
        if err:
            return False, err, None
        if name in ("write", "edit", "read") and not isinstance(args.get("path"), str):
            return False, "error: path must be a string", None
        if name not in (getattr(self, "_tool_names", None) or {t["name"] for t in self.tool_schemas()}):
            return False, (f"error: tool {name} is not available in the {pr['name']} agent. "
                           f"Tools: {', '.join(sorted(getattr(self, '_tool_names', None) or ()))}"), None
        if name in ("write", "edit") and not may_write(pr, relp(self.abspath(args.get("path", "")), self.cwd)):
            return False, f"error: the {pr['name']} agent may only write {pr.get('writes') or 'nothing'}", None
        veto = PLUGINS.emit("before_tool", name, args)
        if isinstance(veto, str):
            return False, veto, None
        if name == "skill" and args.get("name") == pr["name"] and args["name"] not in self.skills.enabled():
            en = self.skills.enabled()  # the agent's own name: load its main guide (webgame -> threejs) instead of failing
            guide = next((n for n in str(pr.get("skills") or "").replace(",", " ").split() if str(en.get(n, {}).get("path", "")).endswith(".md")), None)
            if not guide or args.get("action"):
                return False, f"error: {pr['name']} is the agent mode you run in, not a skill; its guide is already in your instructions. Load one of the skills listed there.", None
            args["name"] = guide
        if name == "bash" and self._no_checks(args.get("cmd")):
            return False, NO_CHECKS, None
        if name == "todo":
            return self._todo(args)
        if name == "memory":
            return self._memory(args)
        if name == "later":
            return self._later(args)
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
        elif name == "bash":
            for p in rm_targets(str(args.get("cmd", "")), self.cwd):
                self._snapshot(p)
        cwd = self.cwd
        if name == "bash" and args.get("workdir"):  # a subfolder for this one command (Codex and opencode pass one)
            cwd = self.abspath(args.pop("workdir"))
            if not os.path.isdir(cwd):
                return False, f"error: workdir {cwd} does not exist", None
        try:
            return self.tools.run(name, args, temit, self.cancel, self.ask, cwd=cwd)
        except Cancelled:
            raise
        except Exception as e:
            return False, f"error: {type(e).__name__}: {e}", None

    def _no_checks(self, cmd):
        cmd = re.sub(r"\b(?:which|command\s+-v|type|where)\s+[^;&|\n]*", "", str(cmd or ""))  # looking a tool up is not checking work
        if behavior(self.s.config["settings"])["tests"] or not CHECK_RX.search(cmd):
            return False
        req = next((m for m in reversed(self.session["messages"]) if m["role"] == "user" and not m.get("_aux")), {}).get("content", "")
        req = req if isinstance(req, str) else " ".join(x.get("text", "") for x in req if x.get("type") == "text")
        return not ASKED_RUN_RX.search(req)

    def _cli(self, spec, args, temit):
        """An agent's own CLI tool: fill the template, stream the output as status lines, report exit code."""
        cmd, err = render_cli(spec, args)
        if err:
            return False, err, None
        if self._no_checks(cmd):
            return False, NO_CHECKS, None
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
        ok, out = run_shell(cmd, int(args.get("timeout") or 300), self.cancel, shell_env(self.s.config.get("secrets")), on_line=line,
                            cwd=self.cwd, pref=self.s.config["settings"].get("shell"))
        return ok, out or "(no output)", None

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

    def _later(self, a):
        """A reminder in the me cloud: at the time it continues this very chat in a sandbox (see jobs in server/features.py)."""
        cloud = Cloud(self.s)
        if not cloud.online:
            return False, "error: not logged in to the me cloud (/login), so reminders are not available", None
        sid = self.session["id"]
        try:
            if a.get("cancel"):
                cloud.req("DELETE", f"/api/jobs/{int(str(a['cancel']).lstrip('#'))}")
                return True, f"cancelled #{a['cancel']}", None
            if not str(a.get("text") or "").strip():
                js = [j for j in cloud.req("GET", "/api/jobs?session=" + urllib.parse.quote(sid)) if j.get("enabled")]
                return True, "\n".join(f"#{j['id']} {j['cron'] or 'once'} · next {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(j['next']))}"
                                       f"{' · new chat' if j.get('fresh') else ''} · {j['name']}" for j in js) or "no reminders in this chat", None
            if not a.get("cron") and not float(a.get("minutes") or 0) > 0:
                return False, "error: give minutes (once) or cron (repeat)", None
            self.s.save_session(self.session)
            cloud.sync()  # the sandbox that wakes up loads this chat from the cloud
            off = time.localtime().tm_gmtoff // 60
            tz = int(float(a["tz"]) * 60) if a.get("tz") not in (None, "") else (off or None)
            j = cloud.req("POST", "/api/jobs", {"prompt": str(a["text"]), "session": sid, "cron": str(a.get("cron") or ""),
                                                "minutes": float(a.get("minutes") or 0), "tz": tz,
                                                "fresh": a.get("new") in (True, "true", 1)})
        except Exception as e:
            return False, f"error: {e}", None
        tzs = "UTC%+g" % (j["tz"] / 60)
        when = time.strftime("%Y-%m-%d %H:%M", time.gmtime(j["next"] + j["tz"] * 60))
        return True, f"#{j['id']} set: {'repeats ' + j['cron'] if j['cron'] else 'once'}, next {when} ({tzs}), " \
                     f"{'in a new chat of this project' if j.get('fresh') else 'continuing this chat'}. It runs in the me cloud and notifies the user.", None

    # ─── todo / goal ───
    def _todo(self, a):
        old = {i["t"]: i["s"] for i in self.session.get("todo") or []}
        if a.get("items"):
            items = []
            for t, st in todo_parse(a["items"]):
                t = t[:200]  # a step resent without a mark keeps its state: resending the list must not undo progress
                items.append({"t": t, "s": st or old.get(t, "todo")})
            if not items:
                return False, 'error: no steps found — items is a list of strings, e.g. ["x read the code", "> fix it", "test"]', None
        else:
            items = [dict(i) for i in self.session.get("todo") or []]
            if not items:
                return False, "error: no checklist yet — set it with items=[…]", None
        if a.get("done") or a.get("doing") or a.get("doing") == 0:  # mark steps by number (also on top of a resent list)
            try:
                done = {int(x) for x in (a.get("done") or [])} if isinstance(a.get("done"), list) else {int(a["done"])} if a.get("done") else set()
                doing = int(a.get("doing") or 0)
            except (TypeError, ValueError):
                return False, "error: done is a list of step numbers (1-based), doing one number", None
            if 0 in done or a.get("doing") == 0:  # counted from 0
                done, doing = {x + 1 for x in done}, doing + 1
            for n, i in enumerate(items, 1):
                if n in done:
                    i["s"] = "done"
                elif n == doing:
                    i["s"] = "doing"
                elif i["s"] == "doing" and doing:
                    i["s"] = "done" if n < doing else "todo"  # moving on to a later step finishes the one in progress
        tests = behavior(self.s.config["settings"])["tests"]
        dropped = [i["t"] for i in items if CHECK_STEP_RX.search(i["t"])] if not tests and not ASKS_CHECK_RX.search(self.request()) else []
        if dropped and len(dropped) < len(items):
            items = [i for i in items if i["t"] not in dropped]
        else:
            dropped = []
        self.session["todo"] = items[:50]
        self.emit({"type": "todo", "items": self.session["todo"]})
        n = sum(1 for i in items if i["s"] == "done")
        now = next((i["t"] for i in items if i["s"] == "doing"), None)
        nxt = next((i["t"] for i in items if i["s"] != "done"), None)
        out = (f"todo: {n}/{len(items)} done" + (f"; {'now' if now else 'next'}: {now or nxt} — mark each step done right after finishing it" if nxt
                                                 else "; all done — " + ("verify, then report" if tests else "report, with the steps for the user to check it")))
        if dropped:
            out += ("\nRemoved checking steps (self-testing is off — the user checks): " + "; ".join(dropped) +
                    ". Do not run checks. The list is now:\n" + todo_lines(items))
        return True, out, None

    def request(self):
        """The user's latest real message (not a self-check or reminder)."""
        for m in reversed(self.session["messages"]):
            if m.get("role") == "user" and not m.get("_aux"):
                c = m.get("content")
                return c if isinstance(c, str) else " ".join(x.get("text", "") for x in c if isinstance(x, dict)) if isinstance(c, list) else ""
        return ""

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
                report["error"] = e["text"]
                temit({"t": "log", "text": "✗ " + e["text"]})
        sub = Agent(self.s, sub_emit, self.confirm, self.ask)
        sub.depth, sub.persist = self.depth + 1, False
        sub.cancel = self.cancel
        sub.session = new_session(self.session.get("cwd"))
        sub.session["agent"] = name
        if not presets(self.s)[name].get("model"):  # the chat's pinned model, not whatever another chat picked last
            sub.session["model"] = self.model_name()
        sub.session["roots"] = list(self.session.get("roots") or [])
        temit({"t": "status", "text": f"{name} agent started"})
        sub.run(a.get("prompt", "") + "\n\nYou are a subagent: do the job, verify it, then reply with a short report "
                                      "(what you did, files touched, results). The report is all the caller sees.")
        for k in ("in", "out", "cost"):
            self.session["usage"][k] = self.session["usage"].get(k, 0) + sub.session["usage"].get(k, 0)
        self.snap.update({k: v for k, v in sub.snap.items() if k not in self.snap})
        last = next((m.get("content") for m in reversed(sub.session["messages"]) if m["role"] == "assistant" and m.get("content")), "")
        if report.get("error"):  # it died midway: its last words ("Let me check X…") are not a report
            return False, clip(f"subagent failed: {report['error']}\nlast output: {last or report['text']}", 6000), None
        return bool(last), clip(last or report["text"] or "(subagent returned nothing)", 6000), None

    # ─── checkpoints / undo ───
    def _snapshot(self, path):
        p = self.abspath(path)
        if p not in self.snap:
            try:
                self.snap[p] = snapshot_file(p)
            except OSError:
                pass

    def _finish_checkpoint(self, start_idx):
        if not self.snap or not self.persist:
            return
        files = dict(self.snap)
        gen = self.session.get("compactions", 0)
        # history was compacted during this turn: message indexes moved, so undo restores files but keeps the history
        msgs = start_idx if gen == getattr(self, "_turn_gen", gen) else -1
        Checkpoints(self.s, self.session["id"]).push({"msgs": msgs, "gen": gen, "files": files, "t": time.time()})
        diffs = [{"path": relp(p, self.cwd), "diff": file_diff(p, old), "new": old is None}
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
        names = [relp(p, self.cwd) for p in cp["files"]]
        ev = {"type": "undone", "files": names, "text": f"restored {len(names)} file{'s' if len(names) != 1 else ''}: " + ", ".join(names[:8])}
        self.emit(ev)
        return dict(ev, ok=True)

    def retry(self):
        """Take back the last request (its file changes and its part of the history) and return its text to run again."""
        if self.busy:
            return None, "busy"
        msgs = self.session["messages"]
        i = max((i for i, m in enumerate(msgs) if m["role"] == "user" and not m.get("_aux") and not m.get("_shell")), default=-1)
        if i < 0:
            return None, "nothing to retry"
        c = msgs[i]["content"]
        text = c if isinstance(c, str) else " ".join(x.get("text", "") for x in c if x.get("type") == "text")
        i -= bool(i and msgs[i - 1].get("_hint"))  # the skill hint sent with the request goes with it
        cps = Checkpoints(self.s, self.session["id"]).load()
        if cps and cps[-1].get("msgs") == i and cps[-1].get("gen", 0) == self.session.get("compactions", 0):
            self.undo()  # the turn changed files: restore them too
        self.session["messages"] = self.session["messages"][:i]
        self.s.save_session(self.session)
        return text, ""

    def context_report(self):
        """What the next request sends, in tokens (about 4 chars each): prompt, tools, history by kind, the biggest messages."""
        tok = lambda x: len(x if isinstance(x, str) else json.dumps(x, ensure_ascii=False)) // 4
        sysp, tools, msgs = self.system_prompt(), self.tool_schemas(), self.session["messages"]
        kinds = collections.Counter()
        for m in msgs:
            kinds["tool results" if m["role"] == "tool" else "your messages" if m["role"] == "user" and not m.get("_aux")
                  else "notes from me" if m["role"] == "user" else "answers + tool calls"] += tok(m)
        total = tok(sysp) + tok(tools) + sum(kinds.values())
        m = self.model() or {}
        ctx = int(m.get("context") or 0)
        rows = [("system prompt", tok(sysp)), (f"tools ({len(tools)})", tok(tools))] + kinds.most_common()
        out = [f"## Context · ~{human(total)} tokens" + (f" of {human(ctx)} ({total * 100 // ctx}%)" if ctx else "")]
        out += [f"- {k}: ~{human(v)}" for k, v in rows]
        big = sorted(((tok(x), i, x) for i, x in enumerate(msgs)), key=lambda t: -t[0])[:5]
        if big and big[0][0] > 500:
            out.append("\nBiggest messages:")
            for n, i, x in big:
                c = x.get("content")
                c = c if isinstance(c, str) else json.dumps(x.get("tool_calls") or c, ensure_ascii=False)
                out.append(f"- #{i} {x['role']}: ~{human(n)} — {(c or '').strip().splitlines()[0][:80] if (c or '').strip() else ''}")
        out.append("\n/compact summarizes old history; old tool output is trimmed automatically before each new request.")
        return "\n".join(out)

    def chat_diff(self):
        """[(path, unified diff)] for every file this chat changed: its first snapshot (checkpoints) against the file now."""
        first = {}
        for cp in Checkpoints(self.s, self.session["id"]).load():
            for p, old in (cp.get("files") or {}).items():
                first.setdefault(p, old)
        out = []
        for p, old in first.items():
            d = file_diff(p, old)
            if d:
                out.append((relp(p, self.cwd), d))
        return out

    def export_md(self):
        """The chat as Markdown: requests, answers, and one line per tool call."""
        out = [f"# {self.session.get('title') or 'chat'}\n"]
        for m in self.session["messages"]:
            c = m.get("content")
            c = c if isinstance(c, str) else " ".join(x.get("text", "") for x in c or [] if x.get("type") == "text")
            if m["role"] == "user" and not m.get("_aux"):
                out.append("## You\n\n" + c + "\n")
            elif m["role"] == "assistant":
                if c:
                    out.append("## me\n\n" + c + "\n")
                for tc in m.get("tool_calls") or []:
                    a = parse_args(tc["function"]["arguments"]) or {}
                    out.append(f"- `{tc['function']['name']}` {self.tools.preview(tc['function']['name'], a, self.cwd)}")
        return "\n".join(out) + "\n"

    def requests(self):
        """[(index, text)] of what the user asked in this chat, oldest first: the points /rewind can go back to."""
        out = []
        for i, m in enumerate(self.session["messages"]):
            c = m.get("content")
            c = c if isinstance(c, str) else " ".join(x.get("text", "") for x in c or [] if x.get("type") == "text")
            if m["role"] == "user" and not m.get("_aux") and not m.get("_shell") and not c.startswith("[Summary"):
                out.append((i, c))
        return out

    def rewind(self, i):
        """Go back to just before request #i: it and everything after leave the history, files those turns changed are restored."""
        if self.busy:
            return None, "busy"
        msgs = self.session["messages"]
        if not (0 <= i < len(msgs)) or msgs[i]["role"] != "user":
            return None, "no such request"
        c = msgs[i]["content"]
        i -= bool(i and msgs[i - 1].get("_hint"))
        cps, gen, files = Checkpoints(self.s, self.session["id"]), self.session.get("compactions", 0), []
        while True:
            last = (cps.load() or [None])[-1]
            if not last or last.get("gen", 0) != gen or last.get("msgs", -1) < i:
                break
            restore_files(cps.pop()["files"])
            files += [relp(p, self.cwd) for p in last["files"]]
        self.session["messages"] = msgs[:i]
        self.s.save_session(self.session)
        return (c if isinstance(c, str) else " ".join(x.get("text", "") for x in c if x.get("type") == "text")), sorted(set(files))

    def fork(self):
        """Continue in a copy of this chat; the original stays as it is (try another approach without losing this one)."""
        old = self.session
        new = new_session(old.get("cwd"))
        new.update(json.loads(json.dumps({k: old[k] for k in ("messages", "model", "todo", "roots", "ws", "compactions", "ctx") if k in old})))
        new.update(title=((old.get("title") or "chat")[:70] + " · fork"), forked_from=old["id"])
        cps = Checkpoints(self.s, old["id"]).load()
        if cps:  # /undo and /rewind work in the copy too
            jsave(Checkpoints(self.s, new["id"]).p, cps)
        self.session, self.snap = new, {}
        self.s.save_session(new)
        return new

    def skill_hint(self, text):
        """Models skip skills they were told about: name the ones that fit this request, once, unless already loaded in this chat."""
        if (self.depth or not behavior(self.s.config["settings"])["skills"] or is_question(text)
                or "skill" not in {t["name"] for t in self.tool_schemas()}):
            return ""
        used = {(tc_args(tc) or {}).get("name") for x in self.session["messages"] for tc in x.get("tool_calls") or []
                if tc["function"]["name"] == "skill"}
        names = [n for n in skill_matches(text, self.skills.enabled()) if n not in used]
        return ("[skills that fit my next request: " + ", ".join(names) + ". If it is a task (not just a question), load the guide "
                "with skill(name) first and follow it]") if names else ""

    def map_hint(self, text):
        """The first task in a chat about a real project: hand over a map of its files and symbols, so the model starts working
        instead of exploring folder by folder (greetings and small talk get none)."""
        if (self.depth or not behavior(self.s.config["settings"])["repo_map"] or len(text.split()) < 4
                or any(m["role"] == "user" and not m.get("_aux") for m in self.session["messages"])):
            return ""
        cwd = os.path.abspath(self.cwd)
        if cwd in (os.path.abspath(os.path.expanduser("~")), os.path.abspath(os.sep)):
            return ""
        srcs = 0
        for f in walk_files(cwd, 3000):
            srcs += os.path.splitext(f)[1].lower() in MAP_EXT
            if srcs >= 6:
                break
        if srcs < 6:
            return ""
        return ("[map of this project (file: top-level symbols) — use it instead of exploring; read what the task needs]\n"
                + repo_map(cwd, "", self.s.home, limit=3000))

    SUGGEST_PROMPT = ("Guess the user's next message in this chat with a coding agent: one short, natural request (at most 12 words), "
                      "in the user's language, that they would most likely send next. Reply with that message only, no quotes. "
                      "If nothing obvious follows, reply with -")

    def suggest_next(self, m):
        """After a finished turn: a cheap tool-less call guessing the next request; the frontends show it greyed in the input."""
        msgs = self.session["messages"]
        req = next((x["content"] for x in reversed(msgs) if x["role"] == "user" and not x.get("_aux") and isinstance(x.get("content"), str)), "")
        ans = next((x["content"] for x in reversed(msgs) if x["role"] == "assistant" and isinstance(x.get("content"), str) and x["content"]), "")
        if not req or not ans:
            return
        sid = self.session["id"]
        try:
            msg, usage = Provider(dict(m, effort=""), self.s.provider(m)).stream(
                self.SUGGEST_PROMPT, [{"role": "user", "content": f"User asked:\n{req[:2000]}\n\nAgent replied:\n{ans[-3000:]}"}],
                None, lambda e: None, threading.Event())
        except Exception:
            return
        c = usage_cost(m, usage)
        if c and self.persist:
            self.session["usage"]["cost"] = round(self.session["usage"].get("cost", 0) + c, 6)
            add_day_cost(self.s.home, c)
        out = re.sub(r"^[\"'«»“”\s]+|[\"'«»“”\s]+$", "", (msg.get("content") or "").strip().split("\n")[0])
        if out and out != "-" and len(out) <= 120 and not self.busy and self.session["id"] == sid:
            self.emit({"type": "suggest", "text": out})

    BTW_PROMPT = "Answer the user's side question about this coding session briefly and directly. You cannot run tools; answer from the transcript."

    def side_question(self, q):
        """/btw: a quick answer from the chat so far, without tools; neither the question nor the answer enters the history."""
        m = self.model()
        if not m:
            return "no model — add one with /models"
        lines = []
        for x in self.session["messages"][-60:]:
            c = x.get("content")
            c = c if isinstance(c, str) else " ".join(y.get("text", "") for y in c or [] if y.get("type") == "text")
            if c:
                lines.append(f"[{'tool result' if x['role'] == 'tool' else x['role']}] {c[:1500 if x['role'] == 'tool' else 4000]}")
            lines += [f"[tool call] {tc['function']['name']} {tc['function']['arguments'][:200]}" for tc in x.get("tool_calls") or []]
        body = "Transcript:\n" + "\n".join(lines)[-60000:] + "\n\nSide question: " + q
        msg, usage = Provider(m, self.s.provider(m)).stream(self.BTW_PROMPT, [{"role": "user", "content": body}], None, lambda e: None, self.cancel)
        c = usage_cost(m, usage)
        u = self.session["usage"]
        u["cost"] = round(u.get("cost", 0) + c, 6)
        if c and self.persist:
            add_day_cost(self.s.home, c)
        return (msg.get("content") or "").strip() or "(no answer)"

    def shell(self, cmd):
        """`!cmd` typed by the user: run it in the chat's folder and put command + output in the history, so the agent sees it."""
        ok, out, _ = self.tools.run("bash", {"cmd": cmd}, lambda e: None, threading.Event(), cwd=self.cwd)
        self.session["messages"].append({"role": "user", "_shell": True,
                                         "content": f"[I ran a command myself]\n$ {cmd}\n{clip(out, 8000)}"})
        if self.persist:
            self.s.save_session(self.session)
        return ok, out

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

    def compact(self, reason="manual", model=None, keep_frac=0.25):
        """Replace older history with a model-written summary. The most recent messages (about `keep_frac` of the model's
        context) stay verbatim. The cut may fall inside one long turn (an assistant step and its tool results stay together),
        so a single big task can be compacted too."""
        msgs = self.session["messages"]
        m = model or self.model()
        if not m or len(msgs) < 3:
            return False
        size = lambda x: len(json.dumps(x, ensure_ascii=False)) // 4
        ctx = int(m.get("context") or 128000)
        budget = max(2000, min(int(ctx * keep_frac), sum(size(x) for x in msgs) // 3))
        acc, cut, last_ok = 0, None, None
        for i in range(len(msgs) - 1, 1, -1):
            acc += size(msgs[i])
            ok = msgs[i]["role"] in ("user", "assistant")  # never cut between a tool call and its result
            if ok and last_ok is None:
                last_ok = i
            if acc > budget:
                break
            if ok:
                cut = i
        cut = cut or last_ok
        if not cut or cut < 2:
            return False
        lines = []
        for x in msgs[:cut]:
            c = x.get("content")
            if isinstance(c, list):
                c = " ".join(y.get("text", "") for y in c if y.get("type") == "text")
            if x["role"] == "tool":
                lines.append(f"[tool result] {str(c)[:1200]}")
            else:
                if c:
                    lines.append(f"[{x['role']}] {str(c)[:4000]}")
                for tc in x.get("tool_calls") or []:
                    lines.append(f"[tool call] {tc['function']['name']} {tc['function']['arguments'][:300]}")
        self.emit({"type": "compacting", "reason": reason})
        msg, usage = Provider(m, self.s.provider(m)).stream(self.COMPACT_PROMPT, [{"role": "user", "content": "\n".join(lines)[-min(400000, ctx * 2):]}],
                                                            None, lambda e: None, self.cancel)
        summary = (msg.get("content") or "").strip()
        if not summary:
            return False
        real = [i for i, x in enumerate(msgs) if x["role"] == "user" and not x.get("_aux") and isinstance(x.get("content"), str)
                and not x["content"].startswith("[Summary")]
        if real and real[-1] < cut:  # the cut went through the current task: keep its request word for word
            summary += "\n\nThe request being worked on now (verbatim):\n" + msgs[real[-1]]["content"][:6000]
        head = [{"role": "user", "content": "[Summary of the earlier conversation]\n" + summary}]
        if msgs[cut]["role"] == "user":
            head.append({"role": "assistant", "content": "Understood — continuing from this summary."})
        before = len(msgs)
        self.session["messages"] = head + msgs[cut:]
        self.session["compactions"] = self.session.get("compactions", 0) + 1
        self.ctx_used = self.session["ctx"] = len(json.dumps(self.session["messages"], ensure_ascii=False)) // 4
        self.emit({"type": "compacted", "before": before, "after": len(self.session["messages"]), "ctx": self.ctx_used,
                   "reason": reason, "summary": summary[:2000]})
        if self.persist:
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
        here = cwd or self.session.get("cwd")  # this chat's folder here, not the process's: me web runs chats in many folders
        self.session = d
        self.ctx_used = d.get("ctx", self.ctx_used)
        if here and os.path.isdir(here) and os.path.abspath(here) != d.get("cwd"):
            self.relocate(here)
        self.ensure_cwd()
        return True

    def ensure_cwd(self):
        """A chat synced from another machine (or continued in the cloud) may name a folder that does not exist here: tools
        would then split between a phantom tree and the process folder. Move it to the first real candidate."""
        cwd = self.session.get("cwd")
        if not cwd or os.path.isdir(cwd):
            return
        for c in list(self.session.get("roots") or []) + [os.getcwd()]:
            if c and os.path.isdir(c):
                self.relocate(c)
                return

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
            self.ensure_cwd()
        return d
