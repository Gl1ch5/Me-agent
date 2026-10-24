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


_GONE = object()


def _merge_cfg(base, ours, disk, depth=0):
    """Three-way merge of config dicts: a key we changed since `base` keeps our value, any other key takes the file's."""
    out = dict(disk)
    for k in set(base) | set(ours):
        o, b = ours.get(k, _GONE), base.get(k, _GONE)
        if o == b:
            continue  # untouched here: whatever the file has now wins
        if depth == 0 and k == "settings" and isinstance(o, dict) and isinstance(b, dict) and isinstance(disk.get(k), dict):
            out[k] = _merge_cfg(b, o, disk[k], 1)
        elif o is _GONE:
            out.pop(k, None)
        else:
            out[k] = o
    return out


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
        if self.dedupe_models():
            jsave(self.cfg_path, self.config)
        st = self.config["settings"]
        if st.get("mouse_rev", 0) < 2:  # 0.7: the mouse now selects + copies text on every platform (Windows had it off)
            st["mouse"], st["mouse_rev"] = True, 2
        self.auth = jload(self.home / "auth.json", {}) or {}
        self.on_change = None
        self._base = json.loads(json.dumps(self.config))

    def save(self):
        """Write config.json, keeping what other `me` processes (other folders, the web UI, studio) changed since we read it:
        only the keys this process changed are ours; every other key is taken from the file as it is now."""
        disk, base = jload(self.cfg_path), getattr(self, "_base", None)
        if isinstance(disk, dict) and isinstance(base, dict) and disk != base:
            self.config = _merge_cfg(base, self.config, disk)
        jsave(self.cfg_path, self.config)
        self._base = json.loads(json.dumps(self.config))
        if self.on_change:
            self.on_change()

    def save_auth(self):
        jsave(self.home / "auth.json", self.auth)

    # models
    def model(self, name=None):
        """By name; an unknown name (a model deleted since) falls back to the default model, then to the first one."""
        for n in (name, self.config.get("model")):
            for m in self.config["models"]:
                if n and m["name"] == n:
                    return m
        return self.config["models"][0] if self.config["models"] else None

    def unique_model_name(self, mid, provider="", skip=None):
        """A display name no other model has: the id's last part, then more of the id, then the provider, then a number."""
        taken = {m["name"] for m in self.config["models"] if m is not skip}
        parts = [x for x in str(mid).split("/") if x]
        cands = ["/".join(parts[-k:]) for k in range(1, len(parts) + 1)] + [f"{provider}/{mid}" if provider else mid]
        for c in cands:
            if c and c not in taken:
                return c
        n = 2
        while f"{cands[0]} {n}" in taken:
            n += 1
        return f"{cands[0]} {n}"

    def dedupe_models(self):
        """Older versions could give two models the same name; a chat remembers its model by name, so rename the later ones.
        Returns True if anything changed."""
        seen, changed = set(), False
        for m in self.config["models"]:
            if m.get("name") in seen or not m.get("name"):
                m["name"] = self.unique_model_name(m.get("id", "model"), m.get("provider", ""), skip=m)
                changed = True
            seen.add(m["name"])
        return changed

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
                    "cwd": d.get("cwd", ""), "roots": d.get("roots") or [], "ws": d.get("ws"), "n": len(d.get("messages", [])), "usage": d.get("usage", {}),
                    "agent": d.get("agent", ""), "pinned": d.get("pinned", False), "archived": d.get("archived", False)})
            out.append(hit[1])
        for k in set(cache) - seen:
            cache.pop(k, None)
        return sorted(out, key=lambda s: s.get("updated", 0), reverse=True)

    def latest(self, cwd):
        """id of the newest chat started in `cwd` (me -c), or None."""
        key = lambda p: os.path.normcase(os.path.realpath(str(p)))  # realpath: macOS /var is /private/var
        here = key(cwd)
        return next((x["id"] for x in self.sessions() if not x.get("archived") and x.get("n") and here in [key(r) for r in [x.get("cwd") or "/"] + x.get("roots", [])]), None)  # roots: it was continued in the cloud since

    def search(self, q, limit=40):
        """Chats whose title or messages contain every word of `q` (any case), newest first: [(index entry, snippet)]."""
        words = [w for w in q.lower().split() if w]
        out = []
        for s in self.sessions():
            d = jload(self.session_path(s["id"])) or {}
            texts = [s.get("title") or ""]
            for m in d.get("messages") or []:
                c = m.get("content")
                if m.get("role") in ("user", "assistant"):
                    texts.append(c if isinstance(c, str) else " ".join(x.get("text", "") for x in c or [] if isinstance(x, dict)))
            low = "\n".join(texts).lower()
            if words and all(w in low for w in words):
                t = next((t for t in texts if words[0] in t.lower()), "")
                i = t.lower().find(words[0])
                out.append((s, " ".join(t[max(0, i - 40):i + 80].split())))
                if len(out) >= limit:
                    break
        return out

    def save_session(self, s):
        s["updated"] = time.time()
        msgs = []
        last = max((i for i, m in enumerate(s["messages"]) if m.get("role") == "user" and not m.get("_aux")), default=-1)
        for i, m in enumerate(s["messages"]):  # drop image payloads from disk/sync
            if isinstance(m.get("content"), list):
                m = dict(m, content=[c if c.get("type") != "image_url" else {"type": "text", "text": "[image]"} for c in m["content"]])
            if i < last and m.get("role") == "assistant" and any(k in m for k in ("reasoning_content", "reasoning_details", "reasoning_items")):
                m = {k: v for k, v in m.items() if k not in ("reasoning_content", "reasoning_details", "reasoning_items")}  # past turns: never resent
            msgs.append(m)
        jsave(self.session_path(s["id"]), dict(s, messages=msgs))
        if self.on_change:
            self.on_change()


def new_session(cwd=None):
    return {"id": time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4], "title": "", "cwd": str(cwd or os.getcwd()),
            "created": time.time(), "updated": time.time(), "messages": [], "usage": {"in": 0, "out": 0}}
