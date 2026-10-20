# AGENTS.md — me (Me-agent)

**me** is a minimal coding agent: one stdlib-only Python file (`me.py`, Python 3.8+) with a TUI, a headless/RPC mode and a local web UI. A FastAPI server (me-agent.ru) syncs config/skills/chats between devices, runs cloud sandboxes, and hosts the web app, the marketplace and a Telegram bot. Users update with `/update`, which downloads `/me.py` from the server.

## Golden rules
- **Edit `src/`, never `me.py` by hand.** `me.py` is generated: `python3 build.py` joins `src/[0-9][0-9]*_*.py` in name order and inlines `src/web/` (index.html, style.css, `*.js`) into `WEBUI`. Commit both `src/` and the rebuilt `me.py` (CI runs `build.py --check`).
- The core is stdlib only. It must run on Windows, macOS and Linux with Python 3.8–3.13, so no 3.9+ syntax such as `match`, `X | Y` types or `dict |`.
- Keep the startup prompt plus tool schemas around 2000 tokens. Check with `python3 tests/measure_tokens.py` (needs `tiktoken`).
- Every UI must work at 360px and 1280px and in every theme. Never break sync compatibility of existing docs (config/session JSON).
- UI text is English source. Russian goes through `T()` in `src/15b_i18n.py` (TUI) and the `RU` tables in `src/web/*.js` (web chat) and `RU_D` in `server/site/account.html` (site). Command names and product words (model, provider, token, todo, API) are not translated.

## Layout
| Path | What |
|---|---|
| `src/00_head.py` | imports, `VERSION`, `SERVER`, `HOME` (~/.me) |
| `src/01_util.py`, `02_store.py` | helpers; `Store` (config.json, sessions, `model()`, `unique_model_name`, `dedupe_models`), `new_session` |
| `src/03_cloud.py` | `Cloud` client: login, sync, long-poll watch, workspaces |
| `src/04_provider.py` | OpenAI-compatible provider (chat/completions + responses), streaming, retries, `no_images` |
| `src/05_files.py`, `07_tools.py`, `10_toolbox.py` | file reading, `TOOLS` schemas, `ToolBox.run` (read/write/edit/bash/ls/grep…), edit fuzzy matching, `syntax_note` |
| `src/06_skills.py`, `08_plugins.py`, `09_webtools.py` | skills (.md guides / .py actions in a subprocess), plugins, web_search/web_fetch |
| `src/10a_presets.py` | agent presets (`code` built in; others are marketplace `.md` files with front matter, `cli.<tool>` lines, `skills:`) |
| `src/10b_features.py` | checkpoints/undo, costs, memory, MCP client, permissions |
| `src/11_agent.py` | **the agent**: `SYSTEM` prompt, `BEHAVIOR` switches, `Agent._run` loop (nudges: verify, todo reminder, loop detection, image fallback), `fix_args` (tool-arg aliases), todo, goal, subagents, compact, retry, rewind/fork/side_question (`/btw`)/shell (`!cmd`), export, chat_diff, per-chat model (`model_name`/`set_model`) |
| `src/12_headless.py` | `-p` mode and `--rpc` JSON-lines loop (used by cloud sandboxes, studio, IDE) |
| `src/13_web.py` | local web server (`me web`), `WebSession.handle` endpoints, `apply_setting` |
| `src/14_terminal.py`, `14a_themes.py`, `14b_clipboard.py` | raw terminal + Windows console input, themes, clipboard/images/desktop_notify |
| `src/15_markdown.py`, `15b_i18n.py` | markdown → terminal, UI translations (`T`, `set_ui_lang`) |
| `src/16`–`20_tui*.py` | TUI: `COMMANDS`, rendering, input/keys, `cmd_*` slash commands, `TUI` class and event handling |
| `src/21_main.py` | entry point / argv |
| `src/web/` | web chat UI (same code for local `me web` and cloud `/app`; `02_transport.js` switches local vs cloud) |
| `server/app.py` | FastAPI: auth, docs sync, marketplace (seeded from `server/market/` on start), `/me.py`, site pages |
| `server/cloud.py`, `routes_cloud.py` | cloud sandboxes (bubblewrap, `me.py --rpc` via FIFO), `/api/cloud/*` |
| `server/features.py` | push, jobs/schedules (also `later` reminders), previews, notify, GitHub (OAuth, repo sessions, `POST /api/github/repos` creates a repo, cloning empty repos) |
| `server/tgbot.py`, `tg_commands.py`, `tg_i18n.py` | Telegram bot: link codes, mirroring with an "open this session" button, a recap of earlier messages when a session is opened, slash commands (`/newrepo`, `/jobs`…), RU strings |
| `server/market/` | marketplace items: `agent-*.md`, skills `*.md`/`*.py`, `theme-*.json`, plugins. Agents: `skills:` names skills installed with them; `cli.<tool>` commands run in the foreground and obey the self-testing switch (`CHECK_RX`). `server.py` is the **server** plugin: SSH via paramiko, with nothing installed on the remote host. It has one `server` tool (it asks before risky actions), `/server`, and `me server` (an interactive shell, or a single action from the CLI). Settings are `config.server` = `{host, user, port, password}` |
| `server/site/` | landing, account (settings, models, marketplace UI), changelog |
| `apps/studio/me_studio.py` + `studio.html` | **me studio** desktop app (pywebview, falls back to the browser): parallel tasks in git worktrees |
| `apps/desktop`, `apps/android`, `apps/vscode` | desktop wrapper, Android WebView app, VS Code extension |
| `tests/` | see below |

## Run and test
- Build: `python3 build.py`. Then `python3 me.py` (TUI), `python3 me.py web`, `python3 me.py -p "task"`.
- Core tests (fast, no deps): `python3 tests/test_core.py`. They use the mock LLM in `tests/mock_llm.py`, where a user message `tool:<name> <json>` makes the model call that tool.
- TUI end-to-end: `python3 tests/test_tui.py` (needs `pyte`; pins `ME_LANG=en`).
- Server suites (need `fastapi uvicorn httpx python-multipart`):
  - `python3 tests/test_features.py` starts its own server.
  - `tests/test_cloud.py` runs against a server you start yourself, using the commands in its docstring.
- Also run `tests/test_studio.py` and `tests/test_terminal.py`.
- Web UI by hand: `node --check src/web/*.js`. For Playwright, use the preinstalled chromium (`executable_path="/opt/pw-browsers/chromium"`) and `wait_until="load"`, not `networkidle`, because the UI long-polls `/events`.
- When you kill test servers, use `pkill -f "[u]vicorn app"`. A plain pattern also matches your own shell's command line and kills it.

## Branches, release, deploy
- Default branch: `claude/minimal-agent-single-file-1pise8`. Work on a feature branch, open a PR into it and merge it.
- Release steps:
  1. Bump `VERSION` in `src/00_head.py`.
  2. Add a Russian entry at the top of `CHANGELOG.md`.
  3. Rebuild.
  4. Merge.
  5. Deploy.
- The binaries (exe, apk, vsix, IDE) come from the `release.yml` workflow (workflow_dispatch). Pushing tags from cloud sessions is blocked.
- Server: Ubuntu, reachable through the `Server` MCP tools. Code is in `/opt/me-agent`, the systemd unit is `me-agent`, nginx proxies to 127.0.0.1:8790, and data lives in `/var/lib/me-agent` (`me.db`).
  - Deploy: `cd /opt/me-agent && git pull --ff-only && systemctl restart me-agent`, then `curl 127.0.0.1:8790/api/version`.
  - Restarting kills cloud sandboxes; the web UI restarts them.
- CI (`.github/workflows/ci.yml`): `build.py --check`, then `test_core` on Windows/macOS/Linux × Python 3.8/3.11/3.13.
- Promo (`.github/workflows/promo.yml`): renders the short and the full-length videos (with the UI sound pack and the Chopin score) whenever `scripts/promo/**`, `docs/promo/**`, `server/market/**` or `src/web/**` change, or by hand from the Actions tab; the result is the `promo` artifact. It never blocks a release.

## Conventions and gotchas
- Match the surrounding style: small functions, comments only where the why is not obvious, no new dependencies in the core.
- A chat pins its model by **name** (`session["model"]`). Model names must stay unique.
- Settings live in `config["settings"]`, which syncs everywhere:
  - `behavior` holds the `/behavior` switches;
  - `lang` is en/ru/"" for auto;
  - `notify`, `perm`, `autocompact` and the other settings sit beside them.
- Agent events (dicts) flow from `Agent.emit` to whichever frontend is running: TUI `on_event`, web `onEvent` in `04_render.js`, or the RPC stdout. When you add an event type, handle it in all three.
- Cloud web: each chat runs in a sandbox worker. A new chat while the worker is busy starts a new worker (`A.newChat` in `02_transport.js`).
- Live eval: `ME_HOME=~/.me python3 tests/eval_live.py --model NAME [--only a,b]` runs real models on 14 small repo tasks (needs API keys; copies the config, never syncs). Each task dir keeps `.eval.log`.
- Agent nudges (`_aux` user messages): VERIFY, ACT (code pasted instead of written), todo reminder/self-check, LOOP; `looping()` in the provider stops a reply that repeats itself. `todo_parse` accepts the many item shapes models send — add a case there, don't tighten the schema.
- Plugins register `@plugin.tool` (`confirm` may be a function of the args), `@plugin.command`, `@plugin.cli(name)` (runs as `me name …`) and `@plugin.on(event)`.
- Each cloud sandbox gets its own API token (`runner_token`). Never revoke the other sandboxes' tokens: a running sandbox would get `invalid token` on notify, sync and later. Old tokens expire after 14 days without use.
- Skill hints are skipped for questions (`is_question` in `06_skills.py`).
- Message queue: web/RPC prompts go through `Agent.submit`. While a turn runs, it emits `queued`; plain texts join the running turn after the next tool step (`take_inbox`), and image messages run as the next turn (`queued_sent`). `/api/cloud/prompt` never answers 409 busy. In TG a message to a busy worker is queued, and an album (`media_group_id`) is collected into one task (`Bot.album`).
- Cloud capacity: there is no per-user sandbox cap. `Runner.start` stops the oldest idle worker when `ME_MAX_WORKERS` (16) is reached or free memory is below `ME_MIN_FREE_MB` (350), and fails only if all workers are busy. `run_job` then retries every minute for 30 minutes and tells the user in TG if it still could not start.
- `later` tool (only when logged in): a reminder or cron job that continues this chat. It is a `jobs` row with `chat` = session id, and `run_job` resumes that session in the cloud. With `fresh=1` it starts a new chat in the same workspace instead.
- `@path[:a-b]` in a message attaches the file (`Agent.mentions`). The web UI in an iframe (`?embed=vscode`) talks to the VS Code extension via `postMessage` (`meOut` up, `insert`/`send`/`new` down).
- CLI: `me "task"`, `me -c` (continue the newest chat in this folder), `me -m NAME`, piped stdin joins a `-p` task; `-p` piped prints only the final answer to stdout.
- `NEXT_STEPS.md` holds the older roadmap/handoff notes. `CHANGELOG.md` is user-facing and written in Russian.
