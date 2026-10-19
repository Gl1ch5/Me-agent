---
name: frontend-design
description: distinctive, production-grade web UI (pages, apps, components) that does not look AI-generated: brief → named palette and type → wireframe → build → screenshot critique. Use for any frontend, landing page, dashboard or component work
icon: ◐
color: magenta
---
Method adapted from Anthropic's frontend-design and web-artifacts-builder skills (Apache-2.0, github.com/anthropics/skills) and me's design agent. Written for me: verify with real screenshots.

# Frontend design

## 0. Read the brief like a designer
- What is it, who uses it, on what device, what is the one thing they must do or feel? If the project has DESIGN.md, a theme or a design system: follow it exactly — taste below applies only to what it leaves open.
- Ground the look in the subject's own world (its industry, materials, documents, signage, tools). A bakery, a trading terminal and a game studio should not share a template.
- Pick ONE direction and commit: e.g. editorial, technical/instrument, playful-tactile, quiet-luxury, brutalist, retro-print, organic. Spend boldness in one place (type, color, or layout), keep the rest calm.

## 1. Plan before code (write it into DESIGN.md or the reply, short)
- Palette: 4-6 named hex colors with roles (background, surface, text, muted, accent, state). Check contrast: text ≥ 4.5:1, large text/UI ≥ 3:1. Define light and dark.
- Type: one family, or two clearly different ones (display + text). Pick on purpose; set a scale (e.g. 12/14/16/20/28/40/64). Body line length ≤ 75-80 characters; serif body gets a bit more line-height.
- Layout: an ASCII wireframe for mobile (360px) and desktop (1280px). A grid and a spacing scale (4/8-based).
- Then re-read the plan against the brief and replace anything that could be "anyone's default". Say what you changed and why.

## 2. Tells of generic AI UI — avoid unless the brief asks for them
- Inter/Roboto/system font with a purple-to-blue gradient; cream #F4F1EA + terracotta; near-black #0B0B0B with acid green; uniform rounded cards with soft rgba(0,0,0,.1) shadows and gradient washes.
- Hero = big number + small label + three stat cards; eyebrow ALL-CAPS labels over every section; "01 / 02 / 03" markers on things that are not a sequence; one italic or colored word in every headline; middle-dot meta strings; arrows on every link.
- Fade-and-slide-up on every section, hover lift on every card, emoji as icons, everything centered.
- Placeholder copy ("Lorem", "Feature one", "Get started" everywhere).

## 3. Build
- Tokens first as CSS variables (colors, type scale, spacing, radii, shadows, motion); components use only tokens. Light + dark via `prefers-color-scheme` and a data attribute.
- Semantic HTML (header/nav/main/section/button/label), real form labels, visible focus rings, hit areas ≥ 44px, `prefers-reduced-motion` respected.
- Icons: inline SVG, one stroke width, `currentColor`. No icon fonts, no emoji.
- Motion: one orchestrated moment (page load or one key reveal) plus responses to user actions; 150-300ms, ease-out. Nothing loops for attention.
- Every interactive element has hover, active, focus, disabled; every data view has empty, loading and error states.
- Copy: active voice; buttons name the action ("Save changes", not "Submit"); the same thing keeps the same name across the flow; errors say what happened and how to fix it.
- Frameworks: use the project's (React/Vue/Svelte/Tailwind/shadcn…). Standalone page with no stack: one HTML file, CSS in <style>, no build step.

## 4. Verify with your eyes
- Serve it and take screenshots at 360px and 1280px, light and dark (the webapp-testing skill, or `python3 -m playwright` with chromium headless: `page.set_viewport_size`, `page.screenshot(full_page=True)`), then `read` the PNGs.
- Critique like a reviewer: alignment, rhythm, hierarchy (what do you see first, second?), overflow, horizontal scroll, contrast, broken states. Then remove one accessory that does not earn its place. Fix and re-shoot.
- Report the screenshots you checked and anything you could not check.
