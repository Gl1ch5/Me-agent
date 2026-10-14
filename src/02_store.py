# ───────────────────────────── store ─────────────────────────────

DEFAULT_CONFIG = {
    "providers": {},  # name -> {base_url, api_key}
    "models": [],  # [{name, id, provider, api, context, max_out, effort, temperature, vision}]
    "model": None,  # current model name
    "theme": "me-dark",
    "agent": "",  # active agent preset ("" = default)
    "prompt": "",  # user's extra system prompt
    "secrets": {},  # env vars for skills
    "settings": {"yolo": False, "mouse": True, "disabled_skills": [], "disabled_plugins": []},
    "search": {"deepseek_key": "", "model": "deepseek-v4-flash"},
    "voice": {"groq_key": "", "model": "whisper-large-v3", "language": ""},
}


class Store:
    def __init__(self, home=HOME):
        self.home = Path(home)
        for d in ("skills", "sessions", "plugins", "themes", "agents"):
            (self.home / d).mkdir(parents=True, exist_ok=True)
        self.cfg_path = self.home / "config.json"
        self.config = jload(self.cfg_path) or json.loads(json.dumps(DEFAULT_CONFIG))
        for k, v in DEFAULT_CONFIG.items():
            self.config.setdefault(k, json.loads(json.dumps(v)))
        for k, v in DEFAULT_CONFIG["settings"].items():
            self.config["settings"].setdefault(k, v)
        self.auth = jload(self.home / "auth.json", {}) or {}
        self.on_change = None

    def save(self):
        jsave(self.cfg_path, self.config)
        if self.on_change:
            self.on_change()

    def save_auth(self):
        jsave(self.home / "auth.json", self.auth)

    # models
    def model(self, name=None):
        name = name or self.config.get("model")
        for m in self.config["models"]:
            if m["name"] == name:
                return m
        return self.config["models"][0] if self.config["models"] else None

    def provider(self, m):
        return self.config["providers"].get(m.get("provider", ""), {})

    # sessions
    def session_path(self, sid):
        return self.home / "sessions" / f"{sid}.json"

    def sessions(self):
        """Light index, newest first: {id, title, updated, cwd, ws, n (messages), usage}. A file is parsed again only when it changed."""
        cache = self.__dict__.setdefault("_idx", {})
        out, seen = [], set()
        for p in (self.home / "sessions").glob("*.json"):
            try:
                st = p.stat()
            except OSError:
                continue
            seen.add(p.name)
            hit = cache.get(p.name)
            if not hit or hit[0] != (st.st_mtime_ns, st.st_size):
                d = jload(p)
                if not isinstance(d, dict) or not d.get("id"):
                    continue
                hit = cache[p.name] = ((st.st_mtime_ns, st.st_size), {
                    "id": d["id"], "title": d.get("title", ""), "updated": d.get("updated", 0), "created": d.get("created", 0),
                    "cwd": d.get("cwd", ""), "ws": d.get("ws"), "n": len(d.get("messages", [])), "usage": d.get("usage", {}),
                    "agent": d.get("agent", ""), "pinned": d.get("pinned", False), "archived": d.get("archived", False)})
            out.append(hit[1])
        for k in set(cache) - seen:
            cache.pop(k, None)
        return sorted(out, key=lambda s: s.get("updated", 0), reverse=True)

    def save_session(self, s):
        s["updated"] = time.time()
        msgs = []
        for m in s["messages"]:  # drop image payloads from disk/sync
            if isinstance(m.get("content"), list):
                m = dict(m, content=[c if c.get("type") != "image_url" else {"type": "text", "text": "[image]"} for c in m["content"]])
            msgs.append(m)
        jsave(self.session_path(s["id"]), dict(s, messages=msgs))
        if self.on_change:
            self.on_change()


def new_session(cwd=None):
    return {"id": time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4], "title": "", "cwd": str(cwd or os.getcwd()),
            "created": time.time(), "updated": time.time(), "messages": [], "usage": {"in": 0, "out": 0}}
