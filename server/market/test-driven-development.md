---
name: test-driven-development
description: red-green-refactor for features and bug fixes: a failing test first, the simplest code to pass, the whole suite green, then refactor. Use when adding behavior to code that has (or should have) tests
icon: ✓
color: green
---
Adapted from obra/superpowers test-driven-development (MIT, github.com/obra/superpowers).

Iron law: no production code without a test that failed first.

## Cycle (one behavior at a time)
1. RED — one test for one behavior, clear name ("rejects expired token"), real code, minimal mocks. Run it. It must fail, and fail for the expected reason (an assertion, not an import error or typo).
2. GREEN — the simplest code that passes. No extra options, no future features.
3. Run the WHOLE suite, not just the new file. Report any failure by name.
4. REFACTOR only while green; run again after.
5. Next behavior.

## Honest tests
- Before writing a test, name the production change that would make it fail. If none would, it tests nothing.
- Assert on real behavior and outputs, never on what a mock was told to return.
- No test-only branches in production code. No sleeps or network unless the thing under test is time or network.
- Never weaken or delete an assertion to get green; if the test is right, the code is wrong — say so.

## Bugs
Write the test that reproduces the bug first, watch it fail, then fix. That test stays as the regression guard.

## Rationalizations to reject
"I'll add tests after" (they pass at once and prove nothing) · "too simple to test" · "the test is flaky, skip it" · "I tested it manually".

Find the runner first (package.json scripts, pytest.ini/pyproject, go test, cargo test, GUT for Godot) and follow the project's conventions for file names and fixtures.
