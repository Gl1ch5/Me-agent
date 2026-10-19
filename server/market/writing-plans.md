---
name: writing-plans
description: turn an agreed design into an executable plan of small, testable tasks (PLAN.md + todo), then execute it task by task with checks. Use for any feature or change bigger than a few edits
icon: ☰
color: blue
---
Adapted from obra/superpowers writing-plans and executing-plans (MIT, github.com/obra/superpowers) and OpenAI's define-goal skill.

## 1. Pin the goal (before planning)
- Objective in one sentence, measurable: what passes when it is done (tests, a command, a screenshot, a number).
- In scope / out of scope (explicit). Constraints copied word for word from the user or spec.
- Stop condition: what would make you stop and ask instead of guessing.

## 2. Map before tasks
- Read the code the change touches. List files to create/modify with their role, and the interfaces between them (function signatures, data shapes, endpoints).
- Name the 3-5 inputs most likely to break it; each gets a test in some task.

## 3. Tasks
Write PLAN.md:
```
# <feature> — plan
Goal / Done when / Out of scope / Constraints
## Files
## Tasks
1. <task> — files: …; check: <command and expected result>
```
- A task = the smallest change that carries its own check and could be reviewed alone (usually 2-15 minutes of work).
- Each task has steps with checkable results: write the failing test → run it (fails) → implement → run it (passes) → run the suite.
- Order: foundations first, risky unknowns early (spike them), UI polish last.
- Put the same tasks in the todo tool.

## 4. Execute
- One task at a time: mark it doing, do it, run its check, mark it done immediately (todo done=[n]). Never batch "done" marks.
- A check fails → fix before moving on (systematic-debugging). The plan turns out wrong → update PLAN.md and todo, say why.
- After the last task: the full verification from step 1, then report against "Done when".
