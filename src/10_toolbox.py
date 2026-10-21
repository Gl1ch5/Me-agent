# ───────────────────────────── toolbox ─────────────────────────────

class ToolBox:
    def __init__(self, store, skills):
        self.s, self.skills = store, skills

    def preview(self, name, a, cwd=None):
        """Short human label for UI."""
        if name in ("read", "write", "edit"):
            p = str(a.get("path", ""))
            try:
                rel = relp(p, cwd)
                return rel if not rel.startswith("..") else p
            except ValueError:
                return p
        if name == "bash":
            return ("&  " if a.get("background") in (True, "true", 1) else "") + str(a.get("cmd", ""))
        if name == "web_search":
            return a.get("query", "")
        if name == "web_fetch":
            return a.get("url", "")
        if name == "task":
            return f"{a.get('agent') or 'code'}: {a.get('prompt', '')[:80]}"
        if name == "todo" and not a.get("items"):  # marking steps by number
            return " · ".join(f"{k} {a[k]}".replace("[", "").replace("]", "") for k in ("done", "doing") if a.get(k) not in (None, [], ""))
        if name == "todo":
            done = a.get("done") if isinstance(a.get("done"), list) else []
            return f"{len(done) or sum(1 for i in a['items'] if str(i).lstrip().startswith(('x ', '[x]', 'X ')))}/{len(a['items'])} done"
        if name == "mcp":
            return a.get("server", "") + (f" · {a['tool']}" if a.get("tool") else "")
        if name in ("notify", "later"):
            return (f"[{a.get('cron') or a.get('minutes')}] " if name == "later" and (a.get("cron") or a.get("minutes")) else "") + str(a.get("text") or a.get("cancel") or "")[:80]
        if name == "publish":
            return f"{a.get('name', '')} ← {a.get('path', '')}"
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
            root = os.path.normcase(os.path.realpath(cwd or os.getcwd()))  # realpath: a symlink inside must not lead outside unasked
            p = os.path.normcase(os.path.realpath(tool_path(cwd or os.getcwd(), a.get("path", ""))))
            return not (p == root or p.startswith(root.rstrip(os.sep) + os.sep))
        if name in PLUGINS.tools:
            c = PLUGINS.tools[name].get("confirm", False)
            return c(a) if callable(c) else c  # a plugin can ask only for its risky actions
        if name == "skill" and a.get("action"):
            sk = self.skills.enabled().get(a.get("name"), {})
            return sk.get("actions", {}).get(a["action"], {}).get("confirm", False)
        return False

    def run(self, name, a, emit, cancel, ask=None, cwd=None):
        """Returns (ok, text, image_data_url|None). Relative paths are relative to the session's folder (`cwd`)."""
        cwd = cwd or os.getcwd()
        at = lambda x: Path(tool_path(cwd, x))
        if name == "read":
            out, img = read_any(at(a["path"]), a.get("offset", 1), a.get("limit", 2000))
            return not out.startswith("error"), out, img
        if name == "write":
            p = at(a["path"])
            if p.is_dir():
                return False, f"error: {a['path']} is a folder", None
            p.parent.mkdir(parents=True, exist_ok=True)
            old, fmt = read_text_file(p) if p.exists() else (None, None)
            content = str(a["content"])
            write_text_file(p, content, fmt)  # an existing file keeps its line ends and encoding
            emit({"t": "diff", "text": self._diff(old or "", content.replace("\r\n", "\n"), p.name)})
            return True, f"{'overwrote' if old is not None else 'created'} {a['path']} ({len(content.splitlines())} lines)" + syntax_note(p, content), None
        if name == "edit":
            p = at(a["path"])
            if not p.is_file():
                return False, f"error: {a['path']} not found (use write to create it)", None
            text, fmt = read_text_file(p)
            new, msg = apply_edits(text, a)
            if new is None:
                return False, msg, None
            write_text_file(p, new, fmt)
            emit({"t": "diff", "text": self._diff(text, new, p.name)})
            return True, f"edited {a['path']} ({msg})" + syntax_note(p, new), None
        if name == "bash":
            env, pref = shell_env(self.s.config.get("secrets")), self.s.config["settings"].get("shell")
            if a.get("background") in (True, "true", 1):
                return run_background(a["cmd"], cwd, env, pref) + (None,)
            ok, out = run_shell(a["cmd"], int(a.get("timeout") or 120), cancel, env, cwd=cwd, pref=pref)
            return ok, out or "(no output)", None
        if name == "ls":
            pat = str(a.get("pattern") or ".").strip()
            if pat == "map" or pat.startswith("map "):
                return True, repo_map(cwd, pat[4:].strip(), self.s.home), None
            return True, list_glob(cwd, pat), None
        if name == "grep":
            return self._grep(a["pattern"], str(at(a.get("path") or ".")), a.get("glob"), cwd)
        if name == "skill":
            return self._skill(a, emit, cancel, ask, cwd)
        if name == "notify":
            return self._notify(dict(a, file=str(at(a["file"]))) if a.get("file") else a)
        if name == "publish":
            return self._publish(at(a.get("path") or "."), str(a.get("name") or ""))
        if name == "mcp":
            return self._mcp(a, emit, cwd)
        if name == "web_search":
            emit({"t": "status", "text": "searching"})
            return web_search(self.s.config, a["query"], cancel)
        if name == "web_fetch":
            return web_fetch(a["url"])
        tool = PLUGINS.tools.get(name)
        if tool:
            r = tool["fn"](a, PluginCtx(emit, cancel, ask, cwd))
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

    def _publish(self, root, name):
        """Upload a folder as a static site (dotfiles, .git, node_modules and the like stay private)."""
        cloud = Cloud(self.s)
        if not cloud.online:
            return False, "error: not logged in to the me cloud (/login), so publishing is not available", None
        if not (root / "index.html").is_file():
            return False, f"error: no index.html in {root} — pass the folder that has it (the build output, if there is a build)", None
        files, total = [], 0
        for d, dirs, names in os.walk(root):
            dirs[:] = [x for x in dirs if not x.startswith(".") and x not in ("node_modules", "__pycache__", "venv")]
            for n in names:
                fp = Path(d, n)
                if n.startswith(".") or not fp.is_file():
                    continue
                total += fp.stat().st_size
                if total > 25_000_000 or len(files) >= 2000:
                    return False, "error: site over 25 MB or 2000 files — publish only the built output", None
                files.append({"path": fp.relative_to(root).as_posix(), "b64": base64.b64encode(fp.read_bytes()).decode()})
        try:
            r = cloud.req("POST", "/api/sites", {"name": re.sub(r"[^a-z0-9-]+", "-", name.lower()).strip("-")[:40] or "site", "files": files},
                          timeout=300)
        except Exception as e:
            return False, f"error: {e}", None
        if not r.get("url"):
            return False, "error: " + str(r.get("detail") or r), None
        return True, f"published {r['files']} files: {r['url']}", None

    def _mcp(self, a, emit, cwd=None):
        try:
            c = mcp_client(self.s, a.get("server", ""), cwd)
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
    def _grep(pattern, path, gl, cwd="."):
        def rel(f):
            try:
                r = os.path.relpath(f, cwd)
            except ValueError:  # another drive
                return f
            return f if r.startswith("..") else r.replace("\\", "/")
        if shutil.which("rg"):
            cmd = ["rg", "-n", "--no-heading", "--color", "never", "-M", "300", "-m", "50", "-e", pattern] + (["-g", gl] if gl else [])
            r = subprocess.run(cmd + ["--", rel(path)], capture_output=True, cwd=cwd, **NO_WINDOW)
            out = r.stdout.decode("utf-8", "replace")
            if r.returncode == 2 and not out:  # a bad regex or path: say so instead of "no matches"
                return False, "error: " + r.stderr.decode("utf-8", "replace").strip()[:500], None
            return True, clip(out, 20000) or "no matches", None
        try:
            rx = re.compile(pattern)
        except re.error as e:
            return False, f"error: bad regex: {e}", None
        out = []
        grx = glob_rx(gl) if gl else None
        files = [path] if os.path.isfile(path) else walk_files(path)
        for f in files:
            r = rel(f)
            if grx and not (grx.match(os.path.basename(f)) or grx.match(r)):
                continue
            try:
                if os.path.getsize(f) > 2_000_000:
                    continue
                with open(f, "rb") as fh:
                    data = fh.read()
                if b"\0" in data[:4096]:
                    continue
                for i, line in enumerate(decode_text(data)[0].splitlines(), 1):
                    if rx.search(line):
                        out.append(f"{r}:{i}:{line.rstrip()[:300]}")
                        if len(out) >= 300:
                            return True, "\n".join(out) + "\n… (truncated)", None
            except OSError:
                continue
        return True, "\n".join(out) or "no matches", None

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


def list_glob(cwd, pat):
    """ls tool: a folder lists itself; otherwise a glob over the project (heavy folders skipped unless named), newest first."""
    pat = os.path.expanduser(pat)
    p = Path(cwd, pat)
    if not re.search(r"[*?\[{]", pat):
        if p.is_dir():
            return read_any(p)[0]
        return pat if p.exists() else f"no match: {pat} does not exist"
    norm = pat.replace("\\", "/")
    head = []
    for part in norm.split("/"):
        if re.search(r"[*?\[{]", part):
            break
        head.append(part)
    base = Path(cwd, *head) if head else Path(cwd)
    if norm.startswith("/") or re.match(r"[A-Za-z]:", norm):
        base, root = Path("/".join(head) or "/"), ""
    else:
        root = cwd
    rx = glob_rx(norm)
    named = set(re.findall(r"[\w.-]+", norm)) & SKIP_DIRS  # e.g. 'node_modules/x/**' is searched when asked for
    hits = []
    for d, dirs, files in os.walk(base):
        dirs[:] = [x for x in dirs if x in named or (x not in SKIP_DIRS and not x.startswith("."))]
        for f in files + [x + "/" for x in dirs if norm.endswith("/")]:
            full = os.path.join(d, f.rstrip("/"))
            r = os.path.relpath(full, root).replace("\\", "/") if root else full.replace("\\", "/")
            if rx.match(r):
                hits.append(r)
        if len(hits) > 5000:
            break
    def mt(h):
        try:
            return -os.path.getmtime(os.path.join(root, h) if root else h)
        except OSError:
            return 0
    hits.sort(key=mt)
    return "\n".join(hits[:300]) + (f"\n… {len(hits) - 300} more" if len(hits) > 300 else "") if hits else "no matches"


def _closest(text, old):
    """Hint for a failed match: the most similar line in the file, with its number."""
    first = next((l for l in old.splitlines() if l.strip()), old).strip()
    stripped = [l.strip() for l in text.splitlines()]
    best = difflib.get_close_matches(first, stripped, n=1, cutoff=0.5)
    if not best:
        return " Read the file again and copy the text exactly (or use lines='a-b')."
    i = stripped.index(best[0])
    return f" Closest is line {i + 1}: {text.splitlines()[i][:200]!r}. Read the file again and copy exactly (or use lines='a-b')."


_FUZZ = str.maketrans({"\u2018": "'", "\u2019": "'", "\u201a": "'", "\u201b": "'", "\u201c": '"', "\u201d": '"', "\u201e": '"',
                       "\u201f": '"', "\u2010": "-", "\u2011": "-", "\u2012": "-", "\u2013": "-", "\u2014": "-", "\u2015": "-",
                       "\u2212": "-", "\u00a0": " ", "\u202f": " ", "\u2009": " ", "\u200a": " ", "\u3000": " "})


def _line_hits(lines, olds, key):
    ko = [key(x) for x in olds]
    kt = [key(x) for x in lines]
    n = len(ko)
    return [i for i in range(len(kt) - n + 1) if kt[i:i + n] == ko]


def _shift(lines, fi, oi):
    out = []
    for l in lines:
        if not l.strip() or fi == oi:
            out.append(l)
        elif fi.startswith(oi):
            out.append(fi[len(oi):] + l)
        elif oi.startswith(fi) and l.startswith(oi[len(fi):]):
            out.append(l[len(oi) - len(fi):])
        else:  # tabs vs spaces
            out.append(fi + l[len(oi):] if oi and l.startswith(oi) else l)
    return out


def _reindent(news, found, olds):
    """The model got the indentation wrong (or used tabs vs spaces): shift its new lines by the same amount. Its first line
    is often copied from the first non-blank character, so the shift comes from the next lines when there are any."""
    ind = lambda l: l[:len(l) - len(l.lstrip())]
    pairs = [(ind(f), ind(o)) for f, o in zip(found, olds) if o.strip()]
    if not pairs:
        return news
    fi, oi = pairs[1] if len(pairs) > 1 and olds[0].strip() and not ind(olds[0]) and ind(found[0]) else pairs[0]
    out = _shift(news, fi, oi)
    if news and olds[0].strip() and not ind(olds[0]) and ind(found[0]) and not ind(news[0]):
        out[0] = ind(found[0]) + news[0]  # the cut-off first line takes the file's own indentation
    return out


def _replace_one(text, old, new, all_):
    """Exact match first. Then, for slips models often make: curly quotes / dashes / odd spaces, trailing whitespace,
    and wrong indentation (the replacement is re-indented to fit). Returns (text, summary) or (None, error)."""
    if not old:
        return None, "error: old is empty (to insert, include a nearby line in old, or use lines='a-b')"
    if old == new:
        return None, "error: old and new are the same — nothing to change"
    n = text.count(old)
    if n > 1 and not all_:
        return None, f"error: old text matches {n} times; add surrounding lines to make it unique, or set all=true"
    if n:
        return text.replace(old, new), f"{n} replacement{'s' if n > 1 else ''}, line {text[:text.index(old)].count(chr(10)) + 1}"
    t2, o2 = text.translate(_FUZZ), old.translate(_FUZZ)  # 1:1 character map, so positions stay valid
    if o2 != old or t2 != text:
        k = t2.count(o2)
        if k == 1:
            i = t2.index(o2)
            return text[:i] + new + text[i + len(old):], f"1 replacement, line {text[:i].count(chr(10)) + 1} (matched ignoring quote/dash style)"
    lines = text.split("\n")
    olds, news = old.split("\n"), new.split("\n")
    if len(olds) > 1 and olds[-1] == "":  # old ended at a line break: compare whole lines
        olds = olds[:-1]
        news = news[:-1] if news and news[-1] == "" else news
    for how, key in (("trailing whitespace", lambda l: l.translate(_FUZZ).rstrip()), ("indentation", lambda l: l.translate(_FUZZ).strip())):
        if not any(key(x) for x in olds):
            break
        hits = _line_hits(lines, olds, key)
        if len(hits) > 1 and not all_:
            return None, f"error: old text matches {len(hits)} places (ignoring {how}); add surrounding lines to make it unique"
        if hits:
            for i in reversed(hits):
                repl = _reindent(news, lines[i:i + len(olds)], olds) if how == "indentation" else news
                lines[i:i + len(olds)] = repl
            return "\n".join(lines), f"{len(hits)} replacement{'s' if len(hits) > 1 else ''}, line {hits[0] + 1} (matched ignoring {how})"
    return None, "error: old text not found." + _closest(text, old)


EDIT_ALIASES = (("old", ("old_string", "oldText", "old_str", "oldString", "search")), ("new", ("new_string", "newText", "new_str", "newString", "replace")),
                ("all", ("replace_all", "replaceAll")))


def _edit_keys(e):
    """Models trained on other agents send old_string/oldText/…: accept those names too."""
    if not isinstance(e, dict):
        return e
    e = dict(e)
    for k, alts in EDIT_ALIASES:
        for x in alts:
            if k not in e and x in e:
                e[k] = e.pop(x)
    return e


def syntax_note(path, text):
    """Python/JSON broken by a write or edit: say so in the same result, so the model fixes it now instead of at the test run."""
    ext = Path(path).suffix.lower()
    try:
        if ext == ".py":
            compile(text, str(path), "exec", dont_inherit=True)
        elif ext == ".json":
            json.loads(text)
    except SyntaxError as e:
        return f"\nwarning: the file no longer parses — line {e.lineno}: {e.msg}. Fix it now."
    except ValueError as e:
        return f"\nwarning: invalid JSON now — {e}. Fix it now."
    except Exception:
        pass
    return ""


def apply_edits(text, a):
    """edit tool: one old/new pair, several (edits=[…]), or lines a..b (1-based, inclusive) replaced by new.
    All-or-nothing. Text uses \n line ends. Returns (new_text, summary) or (None, error)."""
    a = _edit_keys(a)
    if a.get("lines") not in (None, ""):
        lines = text.split("\n")
        m = re.fullmatch(r"\s*(\d+)\s*(?:[-:,.]+\s*(\d+))?\s*", str(a["lines"]))
        if not m:
            return None, "error: lines must look like '12-20' (or '12' for one line)"
        lo, hi = int(m.group(1)), int(m.group(2) or m.group(1))
        if lo < 1 or hi < lo - 1 or lo > len(lines) + 1 or hi > len(lines):
            return None, f"error: lines {lo}-{hi} are outside the file (1-{len(lines)}); 'a-(a-1)' inserts before line a"
        repl = str(a.get("new") or "").replace("\r\n", "\n")
        rl = [] if repl == "" else repl[:-1].split("\n") if repl.endswith("\n") else repl.split("\n")
        return "\n".join(lines[:lo - 1] + rl + lines[hi:]), f"lines {lo}-{hi} → {len(rl)} line{'s' if len(rl) != 1 else ''}"
    pairs = a.get("edits") or []
    if isinstance(pairs, str):  # some models send the list as a JSON string
        try:
            pairs = json.loads(pairs)
        except ValueError:
            return None, "error: edits must be a list of {old, new}"
    pairs = [pairs] if isinstance(pairs, dict) else list(pairs)
    pairs = [_edit_keys(e) for e in pairs]
    if a.get("old") is not None:
        pairs.insert(0, {"old": a["old"], "new": a.get("new", ""), "all": a.get("all")})
    if not pairs:
        return None, "error: give old+new, edits=[{old,new}], or lines='a-b' with new"
    done = []
    for i, e in enumerate(pairs, 1):
        if not isinstance(e, dict) or "old" not in e:
            return None, f"error: edit #{i} needs old and new"
        old, new = str(e["old"]).replace("\r\n", "\n"), str(e.get("new") or "").replace("\r\n", "\n")
        text, msg = _replace_one(text, old, new, e.get("all") or a.get("all"))
        if text is None:
            return None, (f"edit #{i} of {len(pairs)}: {msg} (nothing was written)" if len(pairs) > 1 else msg)
        done.append(msg)
    return text, "; ".join(done)
