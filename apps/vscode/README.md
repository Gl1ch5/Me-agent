# me for VS Code

The me agent in an editor panel: it runs locally (`me web`) in the open folder, edits the real files, shows each turn's diff with
revert, and syncs chats with your me account.

- `ctrl+alt+m` — open the agent beside the editor (or the **me** item in the status bar)
- select code → right click → **me: Ask about selection** (`ctrl+alt+k`)
- **me: Fix problems in this file** — sends the file's errors and warnings to the agent
- **me: Open agent in terminal** — the full terminal UI

Needs Python 3.8+. Uses `~/.me/me.py` from the me installer, or downloads it from the server on first start.
**me IDE** (VSCodium with this extension preinstalled) is built by `.github/workflows/ide.yml`.
