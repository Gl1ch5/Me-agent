# ───────────────────────────── toolbox ─────────────────────────────

class ToolBox:
    def __init__(self, store, skills):
        self.s, self.skills = store, skills

    def preview(self, name, a, cwd=None):
        """Short human label for UI."""
        if name in ("read", "write", "edit"):
            p = str(a.get("path", ""))
            try:
                rel = os.path.relpath(p, cwd or os.getcwd())
                return rel if not rel.startswith("..") else p
            except ValueError:
                return p
        if name == "bash":
            return a.get("cmd", "")
        if name == "web_search":
            return a.get("query", "")
        if name == "web_fetch":
            return a.get("url", "")
        if name == "task":
            return f"{a.get('agent') or 'code'}: {a.get('prompt', '')[:80]}"
        if name == "todo":
            return f"{sum(1 for i in a.get('items') or [] if str(i).lstrip().startswith(('x ', '[x]', 'X ')))}/{len(a.get('items') or [])} done"
        if name == "mcp":
            return a.get("server", "") + (f" · {a['tool']}" if a.get("tool") else "")
        if name == "notify":
            return a.get("text", "")[:80]
        if name in ("ls", "grep"):
            return a.get("pattern", "") + (f"  in {a['path']}" if a.get("path") else "")
        if name == "skill":
            return a.get("name", "") + (f" · {a['action']}" if a.get("action") else "")
        return json.dumps(a)[:80]

    def needs_confirm(self, name, a, cwd=None):
        """False = run, True = ask the user, str = refuse with this reason (read-only mode)."""
        st = self.s.config["settings"]
        mode = perm_mode(st)
        if mode == "full":
            return False
        if name == "bash":
            if cmd_allowed(a.get("cmd", ""), st.get("allow") or DEFAULT_ALLOW):
                return False
            return "read-only mode: commands are off (only allow-listed ones run). Tell the user what to run." if mode == "read" else True
        if name in ("write", "edit"):
            if mode == "read":
                return "read-only mode: file changes are off. Describe the change instead."
            p = os.path.abspath(os.path.expanduser(a.get("path", "")))
            root = os.path.abspath(cwd or os.getcwd())
            return not (p == root or p.startswith(root.rstrip(os.sep) + os.sep))
        if name in PLUGINS.tools:
            return PLUGINS.tools[name].get("confirm", False)
        if name == "skill" and a.get("action"):
            sk = self.skills.enabled().get(a.get("name"), {})
            return sk.get("actions", {}).get(a["action"], {}).get("confirm", False)
        return False

    def run(self, name, a, emit, cancel, ask=None, cwd=None):
        """Returns (ok, text, image_data_url|None). Relative paths and commands resolve against `cwd` (the session's project)."""
        cwd = cwd or os.getcwd()
        if isinstance(a.get("path"), str) and a["path"] and name in ("read", "write", "edit", "grep"):
            a = dict(a, path=os.path.join(cwd, os.path.expanduser(a["path"])))
        if name == "read":
            out, img = read_any(a["path"], a.get("offset", 1), a.get("limit", 2000))
            return not out.startswith("error"), out, img
        if name == "write":
            p = Path(a["path"]).expanduser()
            p.parent.mkdir(parents=True, exist_ok=True)
            old = read_raw(p) if p.is_file() else None
            content = a["content"]
            if old and "\r\n" in old and "\r" not in content:  # keep the file's line endings (a Windows CRLF file stays CRLF)
                content = content.replace("\n", "\r\n")
            write_raw(p, content)
            emit({"t": "diff", "text": self._diff((old or "").replace("\r\n", "\n"), a["content"], p.name)})
            return True, f"wrote {p} ({len(a['content'].splitlines())} lines)", None
        if name == "edit":
            p = Path(a["path"]).expanduser()
            if not p.exists():
                return False, f"error: {p} not found (use write to create it)", None
            raw = read_raw(p)
            crlf = "\r\n" in raw
            text = raw.replace("\r\n", "\n") if crlf else raw  # edit on LF text, write back in the file's own line endings
            new, msg = apply_edits(text, a)
            if new is None:
                return False, msg, None
            write_raw(p, new.replace("\n", "\r\n") if crlf else new)
            emit({"t": "diff", "text": self._diff(text, new, p.name)})
            return True, f"edited {p} ({msg})", None
        if name == "bash":
            out = run_shell(a["cmd"], int(a.get("timeout") or 120), cancel, shell_env(self.s.config.get("secrets")), cwd=cwd,
                            shell=self.s.config["settings"].get("shell", ""))
            return "[exit " not in out[-12:], out or "(no output)", None
        if name == "ls":
            pat = a["pattern"]
            if pat == "map" or pat.startswith("map "):
                return True, repo_map(cwd, pat[4:].strip(), self.s.home), None
            full = os.path.join(cwd, os.path.expanduser(pat))
            hits = [os.path.relpath(h, cwd) if not os.path.isabs(os.path.expanduser(pat)) else h for h in glob.glob(full, recursive=True)]
            hits = [h for h in hits if not any(f"{os.sep}{d}{os.sep}" in os.sep + h for d in SKIP_DIRS)]
            hits.sort(key=lambda h: -os.path.getmtime(os.path.join(cwd, h)) if os.path.exists(os.path.join(cwd, h)) else 0)
            return True, "\n".join(hits[:300]) + (f"\n… {len(hits) - 300} more" if len(hits) > 300 else "") or "no matches", None
        if name == "grep":
            return True, self._grep(a["pattern"], a.get("path") or cwd, a.get("glob"), cwd), None
        if name == "skill":
            return self._skill(a, emit, cancel, ask, cwd)
        if name == "notify":
            return self._notify(a)
        if name == "mcp":
            return self._mcp(a, emit)
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

    def _notify(self, a):
        cloud = Cloud(self.s)
        if not cloud.online:
            return False, "error: not logged in to the me cloud (/login), so Telegram is not available", None
        body = {"text": a.get("text", "")}
        if a.get("file"):
            fp = Path(a["file"]).expanduser()
            if not fp.is_file():
                return False, f"error: {fp} not found", None
            if fp.stat().st_size > 45_000_000:
                return False, "error: file over 45 MB", None
            body["file"] = {"name": fp.name, "b64": base64.b64encode(fp.read_bytes()).decode()}
        try:
            r = cloud.req("POST", "/api/notify", body, timeout=120)
        except Exception as e:
            return False, f"error: {e}", None
        n = r.get("sent", 0)
        return bool(n), f"sent to Telegram ({n} chat{'s' if n != 1 else ''})" if n else "error: " + (r.get("detail") or "no Telegram chat linked"), None

    def _mcp(self, a, emit):
        try:
            c = mcp_client(self.s, a.get("server", ""))
        except KeyError:
            return False, f"no MCP server '{a.get('server')}'. configured: {', '.join(self.s.config.get('mcp') or {}) or 'none'}", None
        try:
            if not a.get("tool"):
                emit({"t": "status", "text": "starting " + c.name})
                return True, f"Tools of {c.name} (call: mcp server={c.name} tool=… args={{…}}):\n" + mcp_describe(c.list_tools()), None
            emit({"t": "status", "text": a["tool"]})
            ok, out = c.call(a["tool"], a.get("args") or {})
            return ok, clip(out, 30000), None
        except Exception as e:
            return False, f"error: {e}", None

    @staticmethod
    def _diff(a, b, name):
        d = list(difflib.unified_diff(a.splitlines(), b.splitlines(), name, name, n=1, lineterm=""))[2:]
        return "\n".join(d[:80]) + ("\n…" if len(d) > 80 else "")

    @staticmethod
    def _grep(pattern, path, gl, cwd=None):
        cwd = cwd or os.getcwd()
        rel = os.path.relpath(path, cwd) if os.path.abspath(path).startswith(os.path.abspath(cwd)) else path
        if shutil.which("rg"):
            cmd = ["rg", "-n", "--no-heading", "-M", "300", "-m", "50"] + (["-g", gl] if gl else []) + ["--", pattern, rel]
            r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=cwd,
                               creationflags=0x08000000 if WIN else 0)
            if r.returncode == 2 and r.stderr.strip():
                return "error: " + r.stderr.strip()[:500]
            return clip(r.stdout, 20000) or "no matches"
        path = rel if os.path.isabs(rel) else os.path.join(cwd, rel)
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
                            out.append(f"{os.path.relpath(f, cwd)}:{i}:{line.rstrip()[:300]}")
                            if len(out) >= 300:
                                return "\n".join(out) + "\n… (truncated)"
            except (UnicodeDecodeError, OSError):
                continue
        return "\n".join(out) or "no matches"

    def _skill(self, a, emit, cancel, ask, cwd=None):
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
        ok, out = self.skills.run(sk, act, a.get("args") or {}, emit, cancel, ask, cwd)
        return ok, clip(out, 30000), None


def _closest(text, old):
    """Hint for a failed match: the most similar line in the file, with its number."""
    first = next((l for l in old.splitlines() if l.strip()), old).strip()
    stripped = [l.strip() for l in text.splitlines()]
    best = difflib.get_close_matches(first, stripped, n=1, cutoff=0.5)
    if not best:
        return " Read the file again and copy the text exactly (or use lines='a-b')."
    i = stripped.index(best[0])
    return f" Closest is line {i + 1}: {text.splitlines()[i][:200]!r}. Read the file again and copy exactly (or use lines='a-b')."


def _replace_one(text, old, new, all_):
    if not old:
        return None, "error: old is empty (to insert, include a nearby line in old, or use lines='a-b')"
    n = text.count(old)
    if n == 0:
        norm = lambda s: "\n".join(l.rstrip() for l in s.replace("\r\n", "\n").split("\n"))
        t2, o2 = norm(text), norm(old)  # tolerate trailing-whitespace / CRLF slips, common with weak models
        if o2 and t2.count(o2) == 1:
            return t2.replace(o2, norm(new)), "1 replacement, whitespace-normalized"
        return None, "error: old text not found." + _closest(text, old)
    if n > 1 and not all_:
        return None, f"error: old text matches {n} times; add surrounding lines to make it unique, or set all=true"
    return text.replace(old, new), f"{n} replacement{'s' if n > 1 else ''}"


def apply_edits(text, a):
    """edit tool: one old/new pair, several (edits=[…]), or lines a..b (1-based, inclusive) replaced by new.
    All-or-nothing. Returns (new_text, summary) or (None, error)."""
    if a.get("lines") not in (None, ""):
        lines = text.split("\n")
        m = re.fullmatch(r"\s*(\d+)\s*(?:[-:,.]+\s*(\d+))?\s*", str(a["lines"]))
        if not m:
            return None, "error: lines must look like '12-20' (or '12' for one line)"
        lo, hi = int(m.group(1)), int(m.group(2) or m.group(1))
        if lo < 1 or hi < lo - 1 or lo > len(lines) + 1 or hi > len(lines):
            return None, f"error: lines {lo}-{hi} are outside the file (1-{len(lines)}); 'a-(a-1)' inserts before line a"
        repl = a.get("new") or ""
        rl = [] if repl == "" else repl[:-1].split("\n") if repl.endswith("\n") else repl.split("\n")
        return "\n".join(lines[:lo - 1] + rl + lines[hi:]), f"lines {lo}-{hi} → {len(rl)} line{'s' if len(rl) != 1 else ''}"
    pairs = list(a.get("edits") or [])
    if a.get("old") is not None:
        pairs.insert(0, {"old": a["old"], "new": a.get("new", ""), "all": a.get("all")})
    if not pairs:
        return None, "error: give old+new, edits=[{old,new}], or lines='a-b' with new"
    done = []
    for i, e in enumerate(pairs, 1):
        if not isinstance(e, dict) or "old" not in e:
            return None, f"error: edit #{i} needs old and new"
        text, msg = _replace_one(text, e["old"], e.get("new", ""), e.get("all") or a.get("all"))
        if text is None:
            return None, (f"edit #{i} of {len(pairs)}: {msg} (nothing was written)" if len(pairs) > 1 else msg)
        done.append(msg)
    return text, "; ".join(done)
