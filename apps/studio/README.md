# me studio

A desktop command center for coding agents, in the spirit of the Codex app, built on the same `me` agent.

- **Start a task** from one composer: pick the project, *Local* or *Worktree*, the model and effort, describe the job, Enter.
  Projects are added with a built-in folder browser (or the system picker in the app window).
- **Projects** in the sidebar, **tasks** under each with a live status (working, waiting for you, ready to review, done, error),
  search (`Ctrl K`) and a new-task shortcut (`Ctrl N`). Every task is its own agent thread: start several and they run in parallel;
  a toast tells you when another task finishes or asks for approval.
- **Worktree mode** (git projects): a task works in its own `git worktree` on its own branch (`me/<task>`), starting from the
  last commit. Your checkout stays untouched until you apply the result. **Local mode** works in the folder itself.
- **Live view** of each task: answers, thinking, compact tool cards (open one for its output or diff), approvals and questions
  right in the conversation, the plan pinned on top.
  Steer a running task by writing to it: the message reaches the model after the current step.
- **Review** panel: every changed file as a diff with +/− counts. Worktree tasks: *Apply to project* (3-way patch onto your
  checkout, staged), *Commit* with an inline message, *Push branch*, *Discard* (removes the worktree and the branch). Local tasks:
  *Undo last turn*, *Commit*.
- **Terminal** in the task's folder (with history); a model and reasoning effort per task; **Settings**: language (English /
  Russian), permissions (ask / full / read-only), theme (every `me` theme), default model.

Tasks are ordinary `me` sessions, so they sync with your account like any chat. Studio state lives in `~/.me/studio.json`,
worktrees in `~/.me/worktrees/`.

## Run

```sh
me studio                 # from the me CLI: downloads this app once, opens a window (or your browser)
python apps/studio/me_studio.py [--browser] [--port N]   # from a checkout
```

Windows: `me-studio.exe` on the [releases page](https://github.com/Gl1ch5/Me-agent/releases/latest) (or `/download/studio` on the site).
A native window needs `pip install pywebview`; without it the same UI opens in your browser on `127.0.0.1` with a private key in the link.

## Develop

Edit `studio.html` (layout and styles), `studio.js` (the UI logic) and `me_studio.py` (the backend), then `python3 build.py` embeds
the UI into `me_studio.py` so the app stays a single file. Tests: `python3 tests/test_studio.py`; for the UI run it with
`--browser --no-open --port N` and drive it with Playwright. `me studio` refreshes its copy from the server on every start.
