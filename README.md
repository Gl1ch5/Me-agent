# me

Minimal coding agent in one Python file (`me.py`, stdlib only). Config, API keys, prompt, skills and chats sync to your cloud account.

```sh
curl -fsSL https://me-agent.ru/i | sh      # macOS / Linux
irm https://me-agent.ru/i.ps1 | iex        # Windows
```

- `me` — full-screen TUI · `me -p "task" [-y]` — headless · `me --rpc` — JSON-lines agent for other frontends
- Tools: read write edit bash ls grep skill (~600 tokens at start)
- Skills: one `.py` file = guide + `@action` functions, run in a subprocess with live progress (`ctx.progress`, `ctx.log`, `ctx.status`)
- `server/` — FastAPI + SQLite backend (accounts, optional TOTP, sync), site, installers. Deploy files in `server/deploy/`.

Tests: `python3 tests/test_core.py`
