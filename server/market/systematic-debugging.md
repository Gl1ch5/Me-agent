---
name: systematic-debugging
description: find and fix the root cause of any bug, test failure or unexpected behavior in four phases (investigate, compare, hypothesize, fix with a failing test). Use before proposing any fix
icon: ✱
color: orange
---
Adapted from obra/superpowers systematic-debugging (MIT, github.com/obra/superpowers).

Iron law: no fix without a root cause. A fix for a symptom is a new bug later.

## Phase 1 — Investigate
1. Read the error completely: message, stack trace, file:line, the first error not the last.
2. Reproduce it reliably with one command. Not reproducible → gather data (logs, inputs, timing), don't guess.
3. What changed? `git diff`, `git log -5 --stat`, dependency/config/env changes.
4. Several components (frontend → API → DB, or build → runtime)? Add a diagnostic print at each boundary, run once, find the layer where good data turns bad.
5. Trace backward: from the bad value to where it was produced, step by step, until the original trigger.

## Phase 2 — Compare with what works
- Find a similar working path in the same codebase (or the library's docs/example). Read it fully, list every difference, however small.

## Phase 3 — One hypothesis at a time
- Write it: "X is the cause because Y". Change ONE thing to test it. Wrong → revert, new hypothesis. Don't stack changes.

## Phase 4 — Fix
1. A failing test (or a tiny script) that shows the bug.
2. One fix at the root cause. 3. The test passes, the whole suite passes. 4. Remove the debug prints.
- Three fixes failed? Stop. The model of the system is wrong: question the architecture, re-read the code, tell the user what you know.

## Red flags — stop and go back to Phase 1
"Quick fix for now", "just try changing X", "should work now", a fix proposed before the data flow was traced, a 4th attempt in the same direction, silencing an error (try/except pass, `|| true`, skipping a test) instead of explaining it.

## Report
Root cause (one sentence, file:line), the fix, the test that proves it, anything related you saw but did not change.
