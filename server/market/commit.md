---
name: commit
description: write great git commits — conventional style, small focused diffs
icon: ⎇
color: orange
---
1. `git status` and `git diff --staged` (stage with `git add -p` if nothing staged).
2. One logical change per commit. Subject ≤ 60 chars, imperative: `fix(auth): handle expired token`.
3. Body: why, not what. Wrap at 72.
4. Never commit secrets, .env, build output. Ask before pushing.
