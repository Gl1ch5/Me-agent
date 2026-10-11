<div align="center">

# ◆ me

**Your coding agent, everywhere.**
One Python file. Everything synced.

**English** · [Русский](README.ru.md)

[me-agent.ru](https://me-agent.ru) · [Web app](https://me-agent.ru/app) · [Account](https://me-agent.ru/account) · [Marketplace](https://me-agent.ru/market) · [Releases](https://github.com/Gl1ch5/Me-agent/releases)

</div>

```sh
curl -fsSL https://me-agent.ru/i | sh       # macOS · Linux · Android (Termux)
irm https://me-agent.ru/i.ps1 | iex         # Windows
```

Then type `me`. Needs Python 3.8+, nothing else.

---

## Contents

[Trust](#trust) · [Why me](#why-me) · [Features](#features) · [Usage](#usage) · [Self-hosting](#self-hosting) · [Development](#development)

---

## Trust

> **me is built on trust, not on secrecy.**

Everything is open: the client (`me.py`, built from `src/`), the web UI, the server (`server/`) and the deploy files. Anyone can read them, and anyone can run the server part on their own machine. Nothing in the code is hidden from you.

**What the hosted server at me-agent.ru holds, stated plainly:**

| Data | How it is stored |
|---|---|
| Login | as is |
| Password and session tokens | hashed |
| Synced config, including provider API keys | **unencrypted** on the server |
| Chats and project files | **unencrypted** on the server |

The operator of a server can read what is stored on it. If that is not acceptable for you, [run your own server](#self-hosting), or keep the keys you care about out of the cloud config. Accounts do not share data with each other.

**Marketplace skills run on your machine** and may install Python packages. Read a skill's code before you install it.

Found a security problem? Open a GitHub issue marked `security`, or reach the maintainer through this repository.

---

## Why me

Every agent makes you rebuild your setup on each machine. **me** keeps your models, API keys, prompt, skills, plugins, themes, agents, chats **and project files** in your account. Open a terminal, a browser tab, the phone app or Telegram, and you continue exactly where you left off, live.

## Features

| | |
|---|---|
| **One file, no dependencies** | `me.py` is stdlib-only Python 3.8+. About 1k tokens of system prompt and tools at start. |
| **Any model** | Any OpenAI-compatible API: chat/completions or responses, reasoning effort, model sync. |
| **Agent modes** | `code` · `design` (DESIGN.md first) · `plan` · `review` · `debug` · `docs` · `research`, plus marketplace modes (devops, tester, security, mobile). |
| **AGENTS.md** | Always loaded into the session and shown in the UI. `/init` writes it and the agent keeps it current. |
| **Cloud sandbox** | Sessions run isolated on the server (bubblewrap). Start on the laptop, continue on the phone. Files and history follow. |
| **Live sync** | Every agent step is saved and pushed. Other devices update within a second. |
| **Skills** | One `.py` file is a guide plus actions with live progress, questions (`ctx.choose`) and tables. Not MCP: about 15 tokens idle. |
| **Plugins** | Change me itself: commands, tools, keys, status bar, themes. Voice input (hold space, Groq Whisper) ships as one. |
| **Themes** | Terminal, web app and site share one palette: Nord, Tokyo Night, Gruvbox, Catppuccin, Rosé Pine and more. |
| **Search** | `web_search` via DeepSeek's built-in search (DuckDuckGo fallback) and `web_fetch`. |
| **Goals and plans** | A `todo` checklist live in every UI. `/goal "…"` works until the goal is done **and verified**, or reports exactly why it is blocked. Self-check before "done". |
| **Subagents** | `task(agent, prompt)` runs any preset in a fresh context and returns a short report. |
| **Safe edits** | Multi-edit and line-range patches. A checkpoint per turn gives you `/undo`, and the web UI shows a diff review with keep or revert. |
| **Permissions** | read-only · ask · full, plus a command allow-list ("always allow `npm test`"). |
| **Cost** | Price table per model, prompt caching, parallel read-only tools. Cost per task, session and day, with optional limits. |
| **Background tasks** | "Do X and notify me": tasks keep running after you close the page. Cron schedules, push and Telegram when done. |
| **Preview** | The site the agent built opens on your phone by link (static files, or the sandbox dev server on `$PORT`). |
| **GitHub** | Token in the account: clone into a workspace, branch, commit, push, open pull requests. |
| **Telegram** | Your own bot per account. The agent's `notify` tool sends you messages and files. |
| **MCP** | Any MCP server, loaded lazily like a skill (one line idle, schemas on demand). |
| **Everywhere** | TUI (compact on Termux) · `me web` (localhost:8080) · cloud `/app` (installable PWA, attachments) · Windows · Android · Telegram. |

## Usage

| Command | What it does |
|---|---|
| `me` | full-screen TUI, press `/` for commands |
| `me web [port]` | browser UI with the local agent (`--lan` for other devices) |
| `me -p "task" [-y]` | one-shot, headless |
| `me --rpc` | JSON-lines agent over stdio (what the cloud runner uses) |

**Key commands:** `/goal` · `/undo` · `/todo` · `/perm` · `/cost` · `/mcp` · `/agent` · `/model` · `/theme` · `/init` · `/compact` · `/autocompact` · `/skills` · `/plugins` · `/workspace` · `/sessions` · `/login` · `/2fa` · `/quit`

`me -p '/goal "all tests pass"'` exits 0 only when the goal is done and verified (2 means blocked). Handy in CI.

## Self-hosting

The server is a FastAPI app in `server/`, with systemd and nginx files in `server/deploy/`. Run it on any Linux machine, then point the client at it:

```sh
export ME_SERVER=https://your-server.example
me
```

## Development

```
src/NN_*.py      the agent, split by concern (TUI = render / input / commands mixins)
src/web/         web UI: index.html + style.css + NN_*.js
build.py         src/ → me.py (single file, committed; CI checks it is fresh)
server/          FastAPI: accounts, TOTP, sync, workspaces, sandbox runner, Telegram, site
server/market/   built-in skills, plugins, themes and agents
apps/            desktop (pywebview) and Android (WebView) shells
tests/           test_core.py (offline) · test_cloud.py (needs a server) · test_restart.py · test_features.py (own server)
                 screenshots.py (playwright, every UI × 360/1280 × every theme) · measure_tokens.py (startup budget)
scripts/promo/   promo clips: pty+pyte terminal recorder, local stand, playwright web scenes, ffmpeg assembly
                 (scripts/build_promo.sh → dist/promo/; storyboards in docs/promo/SCRIPTS.md)
```

Edit `src/`, run `python3 build.py`, then `python3 tests/test_core.py`.

---

<div align="center">

[Русская версия](README.ru.md)

</div>
