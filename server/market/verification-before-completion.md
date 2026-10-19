---
name: verification-before-completion
description: the evidence gate before saying "done", "fixed" or "passing": run the proving command now, read its output, then claim exactly what it shows. Use before every completion report, commit or PR
icon: ☑
color: green
---
Adapted from obra/superpowers verification-before-completion (MIT, github.com/obra/superpowers).

Iron law: no claim without fresh evidence from this turn.

Gate: 1) name the command that proves the claim; 2) run it fully; 3) read the output and exit code; 4) claim only what it shows.

| Claim | Evidence required |
|---|---|
| tests pass | the suite ran now, 0 failures (count shown) |
| build works | the build command exited 0 (a linter is not a build) |
| bug fixed | the original reproduction now passes |
| regression test added | it failed before the fix and passes after |
| UI works | screenshot(s) read at the target widths, no console errors |
| subagent did X | you checked the diff/files yourself |
| requirements met | each requirement checked off line by line |

Red flags in your own reply: "should", "probably", "looks good", "Done!" with no command output, trusting a tool's or subagent's success message, committing or deploying unverified.
Could not verify (no runtime, no device, needs the user's account)? Say exactly what is unverified and the command or steps for the user to check.
