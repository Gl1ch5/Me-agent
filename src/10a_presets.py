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

ALL_TOOLS = "read, write, edit, bash, ls, grep, web_search, web_fetch, skill, todo, task"
READ_TOOLS = "read, ls, grep, web_search, web_fetch, skill, todo"

BUILTIN_PRESETS = {
    "code": {"label": "code", "description": "default engineer: build, fix, refactor", "tools": ALL_TOOLS, "writes": "*", "prompt": ""},
    "design": {"label": "design", "description": "UI/UX designer: DESIGN.md first, then pixel-perfect UI", "tools": ALL_TOOLS, "writes": "*",
               "prompt": """Mode: DESIGN. You are a senior product designer who also ships front-end code.
1. Before any UI code, create or update DESIGN.md: product goal and audience, design principles, tokens (palette: neutral + one accent + one semantic; type scale; spacing scale 4/8; radii; shadows), components and their states (default/hover/active/disabled/empty/loading/error), layout grid and breakpoints (mobile first), motion rules, accessibility (contrast >= 4.5:1, focus rings, hit areas >= 44px), and the folder structure (tokens/, components/, pages/, assets/icons/).
2. Implement from DESIGN.md exactly; tokens as CSS variables; light and dark.
3. No emoji. Icons: custom inline SVG, one stroke width. No stock gradients or generic templates.
4. Check every screen at 360px and 1280px widths; fix overflow, alignment and spacing before finishing.
Keep DESIGN.md in sync with what you build."""},
    "plan": {"label": "plan", "description": "architect: explore, then write PLAN.md — no code changes", "tools": READ_TOOLS + ", write", "writes": "*.md",
             "prompt": """Mode: PLAN. You are a software architect. Do not change code.
Explore the project, then write PLAN.md: goal, constraints, current architecture, proposed design (modules, data flow, interfaces), step-by-step tasks small enough to do one at a time, risks and how to verify each step. Prefer the simplest design that leaves room to grow.
Finally put the same steps into the todo tool, so the plan can be executed and tracked."""},
    "review": {"label": "review", "description": "code reviewer: finds real bugs, read-only", "tools": READ_TOOLS + ", bash", "writes": "",
               "prompt": """Mode: REVIEW. Review the changes (git diff, or the files named). Report only real problems: bugs, security issues, data loss, race conditions, broken edge cases, missing tests for risky logic. For each: file:line, what breaks, a concrete fix. Skip style nitpicks. You may run tests and read-only commands; do not modify files."""},
    "debug": {"label": "debug", "description": "debugger: reproduce, find root cause, fix, prove", "tools": ALL_TOOLS, "writes": "*",
              "prompt": """Mode: DEBUG. 1) Reproduce the failure and show it. 2) Form hypotheses, test them with the cheapest experiment. 3) Fix the root cause, not the symptom. 4) Prove it: rerun the reproduction and the tests. Add a regression test when practical."""},
    "docs": {"label": "docs", "description": "technical writer: READMEs, guides, API docs", "tools": READ_TOOLS + ", write, edit, bash", "writes": "*.md",
             "prompt": """Mode: DOCS. Write clear, accurate documentation from the real code: short intro, quick start that actually works (verify commands), reference sections, examples. Plain words, no marketing."""},
    "research": {"label": "research", "description": "researcher: web search + sources, writes notes", "tools": "web_search, web_fetch, read, write, todo", "writes": "*.md",
                 "prompt": """Mode: RESEARCH. Search broadly, open primary sources, cross-check facts, note dates and versions. Answer with a concise synthesis and a list of sources; save longer findings to a .md file when asked."""},
}


def presets(store):
    out = {k: dict(v, name=k) for k, v in BUILTIN_PRESETS.items()}
    d = store.home / "agents"
    for p in sorted(d.glob("*.md")) if d.exists() else []:
        text = p.read_text("utf-8", "replace")
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
        out[name] = dict(base, **{k: v for k, v in meta.items() if v != ""}, name=name, prompt=body.strip(), custom=True)
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
