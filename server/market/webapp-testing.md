---
name: webapp-testing
description: test and inspect a local web app with Playwright (Python, headless Chromium): start the server, wait for it, screenshots at several widths, console errors, clicks and forms, assertions. Use to verify any UI change or reproduce a frontend bug
icon: ▣
color: green
---
Method adapted from Anthropic's webapp-testing skill (Apache-2.0, github.com/anthropics/skills).

# Web app testing with Playwright

## Decide the path
- Static HTML file → read it for selectors, open it with `file://` (or `python3 -m http.server`).
- Dynamic app → is the dev server already running (`curl -sI localhost:<port>`)? If not, start it with bash background=true and poll until it answers (never `sleep` blindly).
- Install once if missing: `pip install playwright` then `python3 -m playwright install chromium` (ask first if the user's machine has no browser; me cloud sandboxes usually have one).

## Recon, then act
Write a script into a temp/test folder, run it, read the output and the screenshots:
```python
from playwright.sync_api import sync_playwright
URL = "http://localhost:5173"
with sync_playwright() as p:
    b = p.chromium.launch()
    pg = b.new_page()
    errors = []
    pg.on("console", lambda m: m.type in ("error", "warning") and errors.append(f"{m.type}: {m.text}"))
    pg.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
    for w, h in ((360, 780), (1280, 800)):
        pg.set_viewport_size({"width": w, "height": h})
        pg.goto(URL, wait_until="networkidle")
        pg.screenshot(path=f"shot-{w}.png", full_page=True)
        overflow = pg.evaluate("document.documentElement.scrollWidth > innerWidth")
        print(w, "horizontal overflow" if overflow else "ok")
    print("\n".join(errors) or "no console errors")
    b.close()
```
- Always `wait_until="networkidle"` (or wait for a specific selector) before reading the DOM.
- Selectors: `get_by_role("button", name="Save")`, `get_by_label`, `get_by_text`, ids — not brittle CSS chains.
- Interact: `fill`, `click`, `press("Enter")`, `select_option`; then assert with `expect(locator).to_have_text(...)` (`from playwright.sync_api import expect`).
- Dark mode: `b.new_page(color_scheme="dark")`. Mobile: `p.devices["iPhone 13"]`.
- Read the PNGs with the read tool and actually look: layout, overflow, contrast, states.

## Rules
- Console errors, page errors, failed requests or horizontal overflow = not done.
- Stop the servers you started (kill the background pid) unless the user wants them running.
- For a project test suite (Playwright Test, Cypress, Vitest browser), use and extend what exists instead of ad-hoc scripts.
