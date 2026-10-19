# Next steps for me (handoff)

Read AGENTS-style context first: README.md, build.py, src/ (agent, split by file), src/web/ (web UI), server/ (FastAPI backend),
tests/. Edit `src/`, run `python3 build.py`, keep `python3 tests/test_core.py` green; integration: tests/test_cloud.py against a
local server (see its docstring). Deploy: server is reachable via the `Server` MCP (Ubuntu, /opt/me-agent, systemd `me-agent`,
nginx :7777 → 127.0.0.1:8790, data in /var/lib/me-agent). After push: `cd /opt/me-agent && git pull && systemctl restart me-agent`.
Restarting kills cloud sandboxes (web UI auto-restarts them). Release: run workflow `release.yml` (workflow_dispatch) after bumping
VERSION in src/00_head.py. Domain me-agent.ru: when `dig +short me-agent.ru @8.8.8.8` returns 64.188.66.249, run certbot --nginx for
me-agent.ru + www, set ME_PUBLIC_URL=https://me-agent.ru in the systemd unit (and server/deploy), SERVER default in src/00_head.py,
Android meServer in apps/android/gradle.properties.

Testing: `python3 tests/test_core.py` (any OS), `python3 tests/test_tui.py` (pty + pyte end-to-end TUI: keys, mouse selection, paste,
images), cloud suites via a local server (see tests/test_cloud.py docstring). CI runs test_core on Windows/macOS/Linux × Python 3.8/3.11/3.13.
Releases: run `release.yml` (workflow_dispatch) — builds me-desktop.exe, me-android.apk, me-agent.vsix, me IDE (Windows/Linux, VSCodium-based)
and publishes them under the version in src/00_head.py; the site links `/download/<windows|android|vsix|ide-windows|ide-linux>` follow the newest.
Pushing tags from cloud sessions is blocked by the proxy: dispatch the workflow instead.

Rules: keep the core agent a single built file with stdlib only; startup prompt+tools under ~2000 tokens (measure with tests/measure_tokens.py; clear tool descriptions beat a tiny prompt);
every UI must work at 360px and 1280px and in every built-in theme (me-dark, me-light, mono, mono-light, graphite); test with
playwright screenshots before deploying; never break sync compatibility of existing docs.

## Build all of this

1. **Plan mode + Todo + /goal.** `todo` tool (add/update/complete items, persisted in the session, synced); UI shows the checklist
   live (TUI panel, web sidebar card). `/goal "<objective>"`: agent works autonomously step by step until verifiable success criteria
   pass or it is blocked (binary result: done+verified or blocked with reason). Plan preset writes PLAN.md and a todo list.
2. **Self-verification before "done".** After changes the agent must run tests/build/lint (or the code); add to prompt + a
   finishing check in the loop.
3. **Subagents.** Tool `task(agent, prompt)` runs a preset in a fresh context and returns a short result; show nested progress in UI.
4. **Better editing.** `edit` with multiple replacements per call and line-range patches; clearer errors for weak models.
5. **Repo map.** Lazy compact index of files + top-level symbols, loaded on demand (tool or skill), cached per workspace.
6. **Checkpoints + /undo + diff review.** Snapshot touched files before each turn; `/undo` restores the last turn; web shows the
   turn's diff with accept/revert.
7. **Permissions.** Modes: read-only / workspace-write (ask) / full; command allowlists instead of the single yolo switch.
8. **Prompt caching + parallel tools + cost.** Stable prompt prefix for caching providers; run independent read/grep/ls in parallel;
   per-model price table → cost per turn/session/day in UI; optional limits.
9. **Cloud background tasks + schedule.** "Do X and notify me": task keeps running after the UI closes; cron-like schedules per user.
10. **Preview URLs.** Serve a sandbox's dev server or static build at `/preview/<id>/` (proxied, auth-gated) so a site the agent built
    opens on the phone instantly; show the link in chat.
11. **Git integration.** Connect GitHub (token per user): clone into workspace, branch, commit, push, open PR.
12. **Per-user Telegram bridge.** Each user sets their own bot token in the account (not one global bot); server runs one poller per
    user token; built-in `notify`/"send to my Telegram" tool sends via the user's own bot to their linked chat (text + files).
    Keep admin global bot optional.
13. **Web chat upgrades.** Attach images/files from the phone; PWA (manifest + service worker) with push notifications ("done",
    "waiting for you").
14. **Marketplace.** Ratings, versions, auto-update of installed skills/plugins/themes/agents.
15. **Optional MCP bridge.** Connect any MCP server but load it lazily like a skill (one line idle, schemas on demand).
16. **Polish.** Status line compact on narrow terminals (Termux); "record" = ask the user what they meant (voice recording already exists).

## Status after 0.6.1 (2026-10-14)
Done: Python 3.8–3.11 startup fix; tools use the session's folder; provider retries + instant Esc + mid-stream errors; history trimming and honest
token stats; clipboard everywhere in the TUI (select-to-copy, ctrl+v/alt+v/right-click image paste, dragged images); Windows line endings and
cross-drive paths; desktop exe crash on print + native folder picker; Android file chooser + Termux phone mode; VS Code extension + me IDE;
Codex-style project sidebar in the web/desktop app; site apps section.
Next: (a) Codex app — file tree + file viewer panel, per-turn diff panel with per-file revert, several agents running in parallel (one Agent
per project in WebSession), git status/commit panel; (b) live smoke test against a real model (Gemini Flash) via a test key in the env, not
the production DB; (c) Windows-native TUI test (ConPTY) in CI; (d) desktop: auto-update of me.py inside the exe; (e) Android: share-to-me intent.

Known open items: voice (Groq) and the Telegram bot were never tested with real keys; Windows/Android apps built but not run.
