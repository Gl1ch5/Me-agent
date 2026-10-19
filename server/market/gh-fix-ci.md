---
name: gh-fix-ci
description: find out why a GitHub Actions run or PR check is red and fix it: read the failing job's log, reproduce locally, minimal fix, push, re-check. Use when CI fails
icon: ⎇
color: orange
---
Method adapted from OpenAI's gh-fix-ci skill (github.com/openai/skills).

1. Access: `gh auth status` (or a GITHUB_TOKEN). No gh? Use the REST API with the token, or ask the user to paste the log.
2. Find the failure: `gh pr checks <pr>` or `gh run list --branch <b> -L 5`; then `gh run view <run-id> --log-failed`. Only GitHub Actions logs can be read this way; report other checks (external CI) by their URL.
3. Read the log from the FIRST error, not the last line. Note the job, step, OS/runtime version and the exact failing test or command.
4. Is it this change's fault? Compare with the base branch's latest run. Red there too → not this PR: say so and look for an existing fix. Network/runner death before tests ran → one re-run (`gh run rerun <id> --failed`), once.
5. Reproduce locally with the same command and versions (matrix: Python/Node version, OS-specific paths, locale/encoding, line endings).
6. Smallest fix at the root cause (systematic-debugging). Never skip, disable or weaken a test to get green; never push empty commits to retrigger.
7. Run the failing check locally until it passes, plus the fast suite; then commit and push; `gh pr checks <pr> --watch` until green.
8. Report: the failing check, root cause (file:line), the fix, the green run link.
Ask before pushing to a branch the user did not hand you.
