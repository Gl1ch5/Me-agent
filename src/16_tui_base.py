# ───────────────────────────── TUI ─────────────────────────────

COMMANDS = [
    ("help", "show commands & keys"), ("agent", "switch mode: code, design, plan, review…"), ("init", "create AGENTS.md for this project"), ("model", "switch model"), ("models", "add / edit / sync models"),
    ("provider", "add / edit API providers"), ("effort", "reasoning effort: none…xhigh"), ("prompt", "edit your system prompt"), ("behavior", "switches: self-testing, todo, web, plan first, brief…"),
    ("skills", "list / toggle / create skills"), ("plugins", "extensions that modify me"), ("secret", "set env secrets for skills"), ("searchkey", "web search keys: Tavily (free), Exa, Brave"), ("new", "new session"),
    ("sessions", "resume a past session"), ("workspace", "sync this project folder to the cloud"), ("clear", "clear screen + new session"), ("copy", "copy last answer"), ("compact", "summarize history to free context"), ("context", "what fills the context window"), ("autocompact", "auto-compact at N% of context (off)"),
    ("goal", "work autonomously until done+verified or blocked"), ("undo", "revert the files of the last turn"), ("retry", "take back the last request and run it again"),
    ("rewind", "go back to an earlier request (history + files)"), ("fork", "continue in a copy of this chat"),
    ("btw", "quick side question, kept out of the history"), ("search", "find text in all chats"),
    ("export", "save this chat as Markdown"), ("diff", "what changed in this chat"), ("rename", "rename this chat"),
    ("lang", "interface language: en / ru"),
    ("perm", "permissions: read-only / ask / full + allow-list"), ("cost", "spending: turn, session, today, limits"), ("usage", "Me AI plan: 5-hour and weekly limits"), ("mcp", "MCP servers (lazy)"),
    ("todo", "show the agent's checklist"), ("yolo", "toggle full access"), ("sync", "sync with cloud now"), ("login", "log in / register"),
    ("2fa", "authenticator (TOTP) on/off"), ("logout", "log out of cloud"), ("config", "settings"), ("update", "update me"),
    ("theme", "color theme (installable)"), ("exit", "quit"), ("quit", "quit"),
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
