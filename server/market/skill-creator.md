---
name: skill-creator
description: create or improve me skills, agents and plugins that actually trigger and work: intent → draft → test prompts → run with and without → fix → publish. Use when the user wants a new skill/agent, or one misbehaves
icon: ✦
color: purple
---
Method adapted from Anthropic's skill-creator (Apache-2.0, github.com/anthropics/skills), in me's own formats. Files go in the me home folder; they sync to every device.

## Formats
- Guide skill `<home>/skills/<name>.md`: front matter `name`, `description`, `icon` (one glyph), `color`, then `---` and the instructions. The model sees only name + description until it loads the skill, so the description decides when it is used.
- Action skill `<home>/skills/<name>.py`: a docstring with the same front matter + instructions, then functions:
  ```python
  @action
  def scan(ctx, path: str = ".", limit: int = 20):
      "One line: what it returns"
      ctx.status("scanning"); ctx.log("…"); ctx.table(rows); return "summary text"
  ```
  ctx: log, status, bar(done,total), progress(iterable), ask, choose, confirm, table, md, http.get/post, env (secrets), cwd. `@action(confirm=True)` for anything risky. `requires: pkg1, pkg2` in the front matter installs pip packages on first use. Runs in a subprocess; return text the model can act on.
- Agent `<home>/agents/<name>.md`: front matter `name, kind: agent, label, description, icon, color, tools` (read, write, edit, bash, ls, grep, web_search, web_fetch, skill, todo, task, memory), `writes` (glob, empty = read-only), `skills: a, b` (installed with it from the marketplace), optional `cli.<tool>: description | command with {param} | param, other=default`; then `---` and the prompt.
- Plugin `<home>/plugins/<name>.py`: `@plugin.command`, `@plugin.tool`, `@plugin.on("event")`; full access, loads on restart.

## Loop
1. Intent: what job, for whom, what input, what output, what "good" looks like. Collect 3-5 realistic user requests that should trigger it (and 2 that should not).
2. Description: one line saying WHAT it does and WHEN to use it, with the words users actually type ("Godot", "GDScript", "scene"). Too vague → never loaded; too broad → loaded for everything.
3. Body: the method, not a lecture. Imperative steps, concrete commands, checks, red flags, a report format. Explain the why in a few words where a rule looks arbitrary. Put long references in sections the model can skip. Keep it under ~16 KB.
4. Test: run each test request with the skill (and, for comparison, without it). Did it load? Did it follow the steps? Was the result better? For .py skills, call every action once with the skill tool.
5. Fix what failed (usually the description or a missing concrete step), re-test. Repeat until the test set passes.
6. Offer to publish to the marketplace (agent `me`, mod-publish for game mods), with credits for any adapted source and its license.

Never put secrets in skill files; read them from `ctx.env` (set with /secret). Don't overwrite a user's existing skill without asking.
