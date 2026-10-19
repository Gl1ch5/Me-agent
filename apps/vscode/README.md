# me for VS Code

The me agent in an editor panel: it runs locally (`me web`) in the open folder, edits the real files, shows each turn's diff with
revert, and syncs chats with your me account.

- the **me** chat lives in the activity bar (side bar); `ctrl+alt+m` focuses it, **me: Open chat in editor tab** puts it beside the code
- select code → right click → **me: Add selection to chat** (`ctrl+alt+k`): the chat gets `@file:lines` and the snippet
- right click a file → **me: Add file to chat** (`@file`); type `@` in the chat to pick any project file
- click a file path in the chat (read / write / edit) to open it in the editor
- **me: Fix problems in this file** — sends the file's errors and warnings to the agent
- **me: Open agent in terminal** — the full terminal UI

Needs Python 3.8+. Uses `~/.me/me.py` from the me installer, or downloads it from the server on first start.
**me IDE** (VSCodium with this extension preinstalled) is built by `.github/workflows/ide.yml`.
