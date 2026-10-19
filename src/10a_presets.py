# ───────────────────────────── agent presets ─────────────────────────────
# A preset = its own system prompt + its own tool catalog (+ optional write scope, model, effort). The model stays the same.
# Built-ins below; ~/.me/agents/<name>.md (synced as agent/<name>.md, installable from the marketplace) override or add more:
#   ---
#   name: design
#   label: design
#   description: one line
#   tools: read, write, edit, bash, ls, grep, web_search, web_fetch, skill
#   writes: *            (glob of paths it may write; "*.md" = docs only; empty = read-only)
#   model:               (optional model name)   effort: (optional)
#   ---
#   prompt…
# (see the CLI tools format below)

ALL_TOOLS = "read, write, edit, bash, ls, grep, web_search, web_fetch, skill, todo, task, memory"
READ_TOOLS = "read, ls, grep, web_search, web_fetch, skill, todo"

# Only `code` is built in. Everything else (design, plan, review, debug, docs, research, me, security, …) is a marketplace agent.
# An agent file may also ship CLI tools, drawn as boxes with a colored status bar:
#   cli.<tool>: description | command template with {param} | param, other=default      (the model sees <agent>_<tool>)
#   color: red   icon: ◈      → bar color / glyph of its CLI boxes
CODE_PROMPT = """Mode: CODE — one universal engineer. Fit the approach to the request instead of announcing modes:
- build / fix / refactor: read first, smallest correct change, verify.
- bug: reproduce, test hypotheses with the cheapest experiment, fix the root cause, rerun to prove it.
- review: read-only unless asked to fix; report real bugs only (file:line, what breaks, concrete fix).
- plan / design: explore, write the steps (PLAN.md or todo) and wait for a go before big changes; UI work starts from tokens and states.
- docs / research: verify commands and facts against the code or primary sources; cite.
- ops, scripts, data, files: do it with the shell tools; confirm before anything destructive.
More specialised agents (design, security, devops, tests, plan, review, debug, docs, research, me…) are in the marketplace: /agent → "+ more agents".
When asked to extend yourself, write files into {home} (they sync everywhere): skills/<name>.py|.md, agents/<name>.md, themes/<name>.json, plugins/<name>.py.
An agent file is front matter (name, label, description, icon, color, tools, writes, and optional `cli.<tool>: description | command with {param} | param, other=default` lines that add CLI tools with a status bar), then --- and the prompt."""

BUILTIN_PRESETS = {
    "code": {"label": "code", "description": "universal engineer: build, fix, debug, review, plan, docs", "tools": ALL_TOOLS, "writes": "*", "prompt": CODE_PROMPT},
}


def parse_cli(meta, agent):
    """cli.<tool>: desc | cmd | params  →  {tool_name: {desc, cmd, params: {name: default|None}, label}}"""
    out = {}
    for k, v in meta.items():
        if not k.startswith("cli."):
            continue
        label = re.sub(r"[^a-z0-9_]+", "_", k[4:].strip().lower()).strip("_")
        segs = [x.strip() for x in re.split(r"\s\|\s", v + " ")]  # " | " separates fields; a pipe inside the command is fine in the middle
        if len(segs) < 2:
            continue
        parts = [segs[0], " | ".join(segs[1:-1]) if len(segs) > 2 else segs[1]] + ([segs[-1]] if len(segs) > 2 else [])
        if not label or not parts[1]:
            continue
        params = {}
        for x in (parts[2].split(",") if len(parts) > 2 else []):
            n, eq, d = x.strip().partition("=")
            n = re.sub(r"\W+", "", n)
            if n:
                params[n] = d.strip() if eq else None
        out[f"{agent}_{label}"] = {"desc": parts[0] or label, "cmd": parts[1], "params": params, "label": label}
    return out


def quote_arg(v):
    v = str(v)
    return '"' + v.replace('"', '\\"') + '"' if WIN else shlex.quote(v)


def render_cli(spec, args):
    """Fill {param} placeholders (shell-quoted). Returns (cmd, error)."""
    vals = {}
    for n, d in spec["params"].items():
        v = args.get(n)
        v = d if v in (None, "") else v
        if v is None:
            return "", f"error: missing parameter {n}"
        vals[n] = quote_arg(v)
    return re.sub(r"\{(\w+)\}", lambda m: vals.get(m.group(1), m.group(0)), spec["cmd"]), ""


def cli_schema(name, spec, agent):
    props = {n: {"type": "string", "description": ("default " + d) if d is not None else "required"} for n, d in spec["params"].items()}
    return {"name": name, "description": f"[{agent} CLI] {spec['desc']}",
            "parameters": {"type": "object", "properties": props, "required": [n for n, d in spec["params"].items() if d is None]}}


def presets(store):
    d = store.home / "agents"
    got = cached(("presets", str(d)), dir_sig(d, "*.md"), lambda: _load_presets(d))
    return {k: dict(v) for k, v in got.items()}


def _load_presets(d):
    out = {k: dict(v, name=k) for k, v in BUILTIN_PRESETS.items()}
    for p in sorted(d.glob("*.md")) if d.exists() else []:
        text = p.read_text("utf-8", "replace").replace("\r\n", "\n")
        m = re.match(r"^---\n(.*?)\n---\n?(.*)$", text, re.S)
        meta, body = {}, text
        if m:
            for line in m.group(1).splitlines():
                k, _, v = line.partition(":")
                if k.strip():
                    meta[k.strip()] = v.strip()
            body = m.group(2)
        name = meta.get("name") or p.stem
        base = out.get(name, {"tools": ALL_TOOLS, "writes": "*"})
        # frontmatter may carry its own name/prompt/custom keys; build the dict in one place so none of them clash
        fields = {k: v for k, v in meta.items() if v != ""}
        fields = {k: v for k, v in fields.items() if not k.startswith("cli.")}
        fields.update(name=name, prompt=body.strip(), custom=True)
        cli = parse_cli(meta, name)
        out[name] = dict(base, **fields)
        if cli:
            out[name]["cli"] = cli
    return out


def preset(store, name=None):
    ps = presets(store)
    return ps.get(name or store.config.get("agent") or "code") or ps["code"]


def preset_tools(p):
    return {t.strip() for t in str(p.get("tools") or ALL_TOOLS).split(",") if t.strip()}


def may_write(p, path):
    globs = [g.strip() for g in str(p.get("writes", "*")).split(",") if g.strip()]
    return any(fnmatch.fnmatch(path, g) or fnmatch.fnmatch(os.path.basename(path), g) for g in globs)


CONTEXT_FILES = ("AGENTS.md", "ME.md", "CLAUDE.md", "DESIGN.md", "PLAN.md")
