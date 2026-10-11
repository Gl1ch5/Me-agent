# me

Minimal coding agent that ships as **one Python file** (`me.py`, stdlib only). Config, API keys, prompt, skills, plugins, chats and project files sync to your cloud account — continue in the terminal, the browser, the cloud sandbox or Telegram.

```sh
curl -fsSL https://me-agent.ru/i | sh      # macOS / Linux / Termux
irm https://me-agent.ru/i.ps1 | iex        # Windows
```

| command | |
|---|---|
| `me` | full-screen TUI |
| `me web [port]` | browser UI on `localhost:8080` (`--lan` for other devices) |
| `me -p "task" [-y]` | headless one-shot |
| `me --rpc` | JSON-lines agent over stdio (used by the cloud runner) |

## Layout

```
src/NN_*.py      the agent, split by concern — edit these
src/webui.html   chat UI shared by `me web` and the cloud /app
build.py         concatenates src/ → me.py (single file, committed)
server/          FastAPI backend: accounts, TOTP, sync, workspaces, sandbox runner, Telegram bot, site
server/market/   built-in skills & plugins published to the marketplace
tests/           test_core.py (offline, mock LLM) · test_cloud.py (needs a running server)
```

Edit `src/`, then `python3 build.py`. CI fails if `me.py` is stale.

## Extending

- **Skills** — one `.py` (guide + `@action` functions, run in a subprocess with live progress) or `.md` (guide only). ~15 tokens idle.
- **Plugins** — `.py` files executed inside me: `@plugin.command`, `@plugin.tool`, `@plugin.on("key"|"event"|"tick"|"status"|"system_prompt"|"before_tool"|"startup")`, plus the whole `me` module to patch. The default `voice` plugin (hold space → Groq Whisper) is an example.

## Cloud capacity (2 vCPU / 4 GB server)

A sandbox is `me --rpc` in bubblewrap: ~25–35 MB RAM idle, mostly waiting on the LLM API. Limits: 1.5 GB address space, 256 processes, 200 MB per file, 100 MB per workspace, 6 sandboxes at once (2 per user), idle stop after 15 min. That comfortably serves ~10 active users; add servers by running more runners behind the same API later.
