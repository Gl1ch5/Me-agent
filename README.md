<div align="center">

# ◆ me

**Your coding agent, everywhere.** One Python file. Everything synced.

[**me-agent.ru**](https://me-agent.ru) · [Releases](https://github.com/Gl1ch5/Me-agent/releases) · [Web app](https://me-agent.ru/app) · [Account](https://me-agent.ru/account)

</div>

```sh
curl -fsSL https://me-agent.ru/i | sh       # macOS · Linux · Android (Termux)
irm https://me-agent.ru/i.ps1 | iex         # Windows
```


---

## Why

Every agent makes you rebuild your setup on each machine. **me** keeps your models, API keys, prompt, skills, plugins, themes, agents, chats **and project files** in your account. Open a terminal, a browser tab, the phone app or Telegram — you continue exactly where you left off, live.

| | |
|---|---|
| **One file, no deps** | `me.py` is stdlib-only Python 3.8+. ~1k tokens of system prompt + tools at start. |
| **Any model** | Any OpenAI-compatible API — chat/completions or responses, reasoning effort, model sync. |
| **Agent modes** | `code` · `design` (DESIGN.md first) · `plan` · `review` · `debug` · `docs` · `research` + marketplace (devops, tester, security, mobile). |
| **AGENTS.md** | Always loaded into the session and shown in the UI. `/init` writes it; the agent keeps it current. |
| **Cloud sandbox** | Sessions run isolated on the server (bubblewrap). Start on the laptop, continue on the phone — files and history follow. |
| **Live sync** | Every agent step is saved and pushed; other devices update within a second. |
| **Skills** | One `.py` = guide + actions with live progress, questions (`ctx.choose`), tables. Not MCP — ~15 tokens idle. |
| **Plugins** | Change me itself: commands, tools, keys, status bar, themes. Voice input (hold space → Groq Whisper) ships as one. |
| **Themes** | Terminal, web app and site share one palette. Nord, Tokyo Night, Gruvbox, Catppuccin, Rosé Pine… |
| **Search** | `web_search` via DeepSeek's built-in search (DuckDuckGo fallback) + `web_fetch`. |
| **Everywhere** | TUI · `me web` (localhost:8080) · cloud `/app` · Windows app · Android app · Telegram bot. |

## Use

| | |
|---|---|
| `me` | full-screen TUI — `/` for commands |
| `me web [port]` | browser UI with the local agent (`--lan` for other devices) |
| `me -p "task" [-y]` | one-shot, headless |
| `me --rpc` | JSON-lines agent over stdio (what the cloud runner uses) |

Key commands: `/agent` mode · `/model` · `/theme` · `/init` · `/compact` · `/autocompact` · `/skills` · `/plugins` · `/workspace` · `/sessions` · `/login` · `/2fa` · `/quit`

## Develop

```
src/NN_*.py      the agent, split by concern (TUI = render / input / commands mixins)
src/web/         web UI: index.html + style.css + NN_*.js
build.py         src/ → me.py (single file, committed; CI checks it is fresh)
server/          FastAPI: accounts, TOTP, sync, workspaces, sandbox runner, Telegram, site
server/market/   built-in skills, plugins, themes and agents
apps/            desktop (pywebview) and Android (WebView) shells
tests/           test_core.py (offline) · test_cloud.py (needs a server)
```

Edit `src/`, run `python3 build.py`, then `python3 tests/test_core.py`.
