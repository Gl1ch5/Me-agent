# ───────────────────────────── toolbox ─────────────────────────────

class ToolBox:
    def __init__(self, store, skills):
        self.s, self.skills = store, skills

    def preview(self, name, a):
        """Short human label for UI."""
        if name in ("read", "write", "edit"):
            return a.get("path", "")
        if name == "bash":
            return a.get("cmd", "")
        if name == "web_search":
            return a.get("query", "")
        if name == "web_fetch":
            return a.get("url", "")
        if name in ("ls", "grep"):
            return a.get("pattern", "") + (f"  in {a['path']}" if a.get("path") else "")
        if name == "skill":
            return a.get("name", "") + (f" · {a['action']}" if a.get("action") else "")
        return json.dumps(a)[:80]

    def needs_confirm(self, name, a):
        if self.s.config["settings"].get("yolo"):
            return False
        if name in ("write", "edit", "bash"):
            return True
        if name in PLUGINS.tools:
            return PLUGINS.tools[name].get("confirm", False)
        if name == "skill" and a.get("action"):
            sk = self.skills.enabled().get(a.get("name"), {})
            return sk.get("actions", {}).get(a["action"], {}).get("confirm", False)
        return False

    def run(self, name, a, emit, cancel, ask=None):
        """Returns (ok, text, image_data_url|None)."""
        if name == "read":
            out, img = read_any(a["path"], a.get("offset", 1), a.get("limit", 2000))
            return not out.startswith("error"), out, img
        if name == "write":
            p = Path(a["path"]).expanduser()
            p.parent.mkdir(parents=True, exist_ok=True)
            old = p.read_text("utf-8", "replace") if p.exists() else None
            p.write_text(a["content"], "utf-8")
            emit({"t": "diff", "text": self._diff(old or "", a["content"], p.name)})
            return True, f"wrote {p} ({len(a['content'].splitlines())} lines)", None
        if name == "edit":
            p = Path(a["path"]).expanduser()
            if not p.exists():
                return False, f"error: {p} not found", None
            text = p.read_text("utf-8")
            n = text.count(a["old"])
            if n == 0:
                return False, "error: old text not found (read the file first, match exactly)", None
            if n > 1 and not a.get("all"):
                return False, f"error: old text matches {n} times; add context or set all=true", None
            new = text.replace(a["old"], a["new"])
            p.write_text(new, "utf-8")
            emit({"t": "diff", "text": self._diff(text, new, p.name)})
            return True, f"edited {p} ({n} replacement{'s' if n > 1 else ''})", None
        if name == "bash":
            out = run_shell(a["cmd"], int(a.get("timeout") or 120), cancel)
            return "[exit " not in out[-12:], out or "(no output)", None
        if name == "ls":
            pat = a["pattern"]
            hits = [h for h in glob.glob(pat, recursive=True) if not any(f"{os.sep}{d}{os.sep}" in os.sep + h for d in SKIP_DIRS)]
            hits.sort(key=lambda h: -os.path.getmtime(h) if os.path.exists(h) else 0)
            return True, "\n".join(hits[:300]) + (f"\n… {len(hits) - 300} more" if len(hits) > 300 else "") or "no matches", None
        if name == "grep":
            return True, self._grep(a["pattern"], a.get("path") or ".", a.get("glob")), None
        if name == "skill":
            return self._skill(a, emit, cancel, ask)
        if name == "web_search":
            emit({"t": "status", "text": "searching"})
            return web_search(self.s.config, a["query"], cancel)
        if name == "web_fetch":
            return web_fetch(a["url"])
        tool = PLUGINS.tools.get(name)
        if tool:
            r = tool["fn"](a, PluginCtx(emit, cancel, ask))
            return (True, r, None) if isinstance(r, str) else (bool(r[0]), r[1], None)
        return False, f"unknown tool {name}", None

    @staticmethod
    def _diff(a, b, name):
        d = list(difflib.unified_diff(a.splitlines(), b.splitlines(), name, name, n=1, lineterm=""))[2:]
        return "\n".join(d[:80]) + ("\n…" if len(d) > 80 else "")

    @staticmethod
    def _grep(pattern, path, gl):
        if shutil.which("rg"):
            cmd = ["rg", "-n", "--no-heading", "-M", "300", "-m", "50", pattern, path] + (["-g", gl] if gl else [])
            r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
            return clip(r.stdout, 20000) or "no matches"
        try:
            rx = re.compile(pattern)
        except re.error as e:
            return f"error: {e}"
        out = []
        files = [path] if os.path.isfile(path) else walk_files(path)
        for f in files:
            if gl and not fnmatch.fnmatch(os.path.basename(f), gl):
                continue
            try:
                with open(f, encoding="utf-8") as fh:
                    for i, line in enumerate(fh, 1):
                        if rx.search(line):
                            out.append(f"{f}:{i}:{line.rstrip()[:300]}")
                            if len(out) >= 300:
                                return "\n".join(out) + "\n… (truncated)"
            except (UnicodeDecodeError, OSError):
                continue
        return "\n".join(out) or "no matches"

    def _skill(self, a, emit, cancel, ask):
        sks = self.skills.enabled()
        sk = sks.get(a.get("name"))
        if not sk:
            return False, f"no skill '{a.get('name')}'. available: {', '.join(sks) or 'none'}", None
        act = a.get("action")
        if not act:
            acts = "\n".join(f"- {v['sig']}: {v['doc']}" for v in sk["actions"].values())
            return True, (sk["body"] or sk["description"]) + (f"\n\nActions (skill name={sk['name']}, action=…, args={{…}}):\n{acts}" if acts else ""), None
        if act not in sk["actions"]:
            return False, f"no action '{act}' in {sk['name']}. actions: {', '.join(sk['actions'])}", None
        ok, out = self.skills.run(sk, act, a.get("args") or {}, emit, cancel, ask)
        return ok, clip(out, 30000), None
