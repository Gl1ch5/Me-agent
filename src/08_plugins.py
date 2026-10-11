# ───────────────────────────── plugins ─────────────────────────────


class PluginCtx:
    """What a plugin tool receives: live UI events + cancellation."""

    def __init__(self, emit, cancel, ask=None):
        self.emit, self.cancel, self._ask = emit, cancel, ask

    def log(self, text):
        self.emit({"t": "log", "text": str(text)})

    def status(self, text):
        self.emit({"t": "status", "text": str(text)})

    def progress(self, v, label="", n=None, total=None):
        self.emit({"t": "progress", "v": v, "label": label, "n": n, "total": total})

    def ask(self, q):
        return self._ask(q) if self._ask else ""


class Plugins:
    """Plugins are trusted .py files in ~/.me/plugins, executed in-process at startup.
    They get `plugin` (this registry) and `me` (this module — anything can be patched).
      @plugin.tool("name", "description", {"arg": "string"}, confirm=False)  def fn(args, ctx) -> str
      @plugin.command("name", "description")                                   def fn(tui, arg)
      @plugin.on("key"|"event"|"startup"|"tick"|"status"|"system_prompt"|"before_tool")
    """

    def __init__(self):
        self.tools, self.commands, self.hooks, self.loaded, self.errors = {}, {}, {}, {}, {}
        self.store = self.tui = None

    def tool(self, name, description, params=None, confirm=False, required=None):
        def deco(fn):
            props = {k: (v if isinstance(v, dict) else {"type": v}) for k, v in (params or {}).items()}
            schema = {"name": name, "description": description,
                      "parameters": {"type": "object", "properties": props, "required": required if required is not None else list(props)}}
            self.tools[name] = {"fn": fn, "schema": schema, "confirm": confirm}
            return fn
        return deco

    def command(self, name, description=""):
        def deco(fn):
            self.commands[name] = (fn, description)
            return fn
        return deco

    def on(self, event):
        def deco(fn):
            self.hooks.setdefault(event, []).append(fn)
            return fn
        return deco

    def emit(self, event, *a):
        """Call hooks in order; first non-None result wins."""
        for fn in self.hooks.get(event, []):
            try:
                r = fn(*a)
            except Exception as e:
                self.errors[getattr(fn, "__module__", "?")] = f"{event}: {e}"
                continue
            if r is not None:
                return r
        return None

    def collect(self, event, *a):
        out = []
        for fn in self.hooks.get(event, []):
            try:
                r = fn(*a)
                if r:
                    out.append(r)
            except Exception as e:
                self.errors[getattr(fn, "__module__", "?")] = f"{event}: {e}"
        return out

    def load(self, store):
        self.store = store
        d = store.home / "plugins"
        disabled = set(store.config["settings"].get("disabled_plugins", []))
        for p in sorted(d.glob("*.py")) if d.exists() else []:
            if p.stem in disabled:
                continue
            g = {"__name__": "me_plugin_" + p.stem, "__file__": str(p), "plugin": self, "me": sys.modules[__name__]}
            try:
                exec(compile(p.read_text("utf-8"), str(p), "exec"), g)
                self.loaded[p.stem] = g
            except Exception as e:
                self.errors[p.stem] = f"{type(e).__name__}: {e}"

    def tool_schemas(self):
        return [t["schema"] for t in self.tools.values()]


PLUGINS = Plugins()
