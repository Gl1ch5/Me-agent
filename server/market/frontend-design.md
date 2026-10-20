---
name: frontend-design
description: distinctive, production-grade web UI (landing pages, sites, apps, dashboards, components) that does not look AI-generated: subject inventory → idea → copy → build with real pictures → measured gate → screenshot critique → motion. Use for any frontend, page, redesign or "make it look designed" request
icon: ◐
color: magenta
changes: merged with tasteskill v2 — subject inventory, the idea sentence, one live thing, house rules, real pictures from open collections, a measured gate and a look-and-fix loop
---
Method merged from tasteskill v2 (MIT, github.com/Leonxlnx/taste-skill — a study of 247 award-gallery sites), Anthropic's frontend-design skill (Apache-2.0) and me's design agent. Rewritten for me: no extra scripts needed, checks run with playwright.

# Frontend design

A page built by a model looks like the average of every page it has seen: a centred headline, a small label above it, three icon cards, a soft gradient, thin rules, a monospace caption. Nothing is wrong and nothing was decided. Replace those defaults with decisions — and remember that removing ornament is only half the job: fill the space with substance (larger type, whole fields of colour, real pictures of this subject's world, full content), never with another ornament.

First decide the kind of work:
- **A page** is read and scrolled (landing, product/marketing site, portfolio, event, restaurant, one-pager) → the run below.
- **An app screen** is worked in (dashboard, admin, settings, inbox, list with filters, editor) → "App screens" at the end; the page run does not apply.
- **A component or one section** → match the surrounding design, apply the house rules, still screenshot it.
- If the project has DESIGN.md, a theme or a design system, follow it exactly; the taste below fills only what it leaves open.

## The run (a page)

### 1. Read the brief, inventory the subject
- Find what is real: subject, audience, the one action the page must get, the stack, the existing brand, and **which pictures truly exist** (the project's photos, a real interface to show, an image tool, or nothing). Vague request → decide and state your reading in one line; ask only when a missing fact blocks the work.
- Write an **inventory**: 8–12 things that belong to this client and to no competitor — its name and what it means, its street and what that place looks like (tiles, signs, light), its numbers (seats, minutes to the sea, price, year), the constraint it lives with (one batch a month, closed in winter), its materials and colours, its rituals and hours, the printed things of its world (ticket, menu, label, timetable). The idea and the picture searches come from this list.

### 2. Write the argument
One line per section: the claim and its proof. The claims decide which sections exist and their order; delete any section with no claim. This is how you avoid the stock sequence hero → logos → three features → testimonial → pricing → CTA.

### 3. Choose the direction
Write three different directions for this subject (composition, type system, palette proportions, hero, one signature move), each in two lines, then pick the one that best carries something from the inventory — not the most familiar — and say why in one line. Examples of directions: editorial/magazine, poster/typographic, catalogue/specimen, archival/documentary, instrument/technical, tactile/print, quiet-luxury, brutalist, playful. Commit to it; spend boldness in one place.
- **Palette**: one ground, one ink, one accent (+ 1–3 field colours), as named hex with roles. If the subject has unmistakable colours (its material, its street, its landscape), take them. Contrast: text ≥ 4.5:1, large text/UI ≥ 3:1.
- **Type**: one family (a third of award sites use one) or two clearly different; pick on purpose, not Inter/Roboto by habit.
- **Layout**: an ASCII wireframe for 360px and 1280px.

### 4. Find the idea
Fill both blanks from the inventory before building:
> On this page, **[something that belongs to the subject]** becomes **[the device the direction calls for]**.

Look in: its output (put the real output on the page), its object or shape (enlarge, crop, make it the frame), its name (letters that hold a photo or stack into a wall), its place, its ritual (a sequence the scroll performs), its artefact (ticket, receipt, manual), its opposite. **Competitor test:** put the nearest competitor in the sentence — if it still works, the idea belongs to the category; find another. "Clean and modern" is not an idea.

**One live thing:** one thing the visitor can do, made from the idea and the real content — pick a wood and see the chair in it, set the loan and read the real monthly figure, type your name in the foundry's letters, see whether the ferry still runs today. Works with a thumb on a phone, has a plain no-script fallback, never blocks reading, is one thing, not a playground. In blind tests every winning page had one.

### 5. Write the words (before markup)
- **Every headline fits in two lines or fewer at every width** (about 32 characters is safe at a confident size; award heroes: median 5 words). Cut words, then widen the measure, size last.
- One supporting sentence right under the headline that says plainly what this is; one primary action named for what it does, a real button on the first screen.
- The first screen answers the visitor's first question in large type (event: what, when, where; product: what it is and costs/does; person: who and what they do).
- **Write the full content**: the whole menu with prices, every room, every speaker, the price with what it includes, a real answer per question. Thin sections are the commonest reason a clean page loses to a busier one. Sample details where the brief has gaps are fine — list them for the user to confirm. **Proof is never invented**: no fake customers, quotes, reviews, logos, press or statistics.

### 6. Build it still (no motion yet)
1. **Tokens** as CSS variables (colours, type scale, spacing, radii, motion); everything uses tokens. Light and dark if the project has both.
2. **The hero exactly as planned**, at 1280 and 360 px. The big thing big, the small things small.
3. **Each section from the plan**, neighbours never sharing a layout, **the idea carried into every section** including pricing, FAQ and footer (the commonest failure: a distinctive first screen followed by stock blocks).
4. **Pictures**, in this order: the project's own → generated (if an image tool/skill exists) → **real pictures of the subject's world from open collections** → drawn in code from the subject (one flat consistent graphic language in the palette) → type and colour alone. Never a placeholder, an icon or an abstract blob instead.
   - Search open collections with web_fetch, commercial-use licences only:
     `https://api.openverse.org/v1/images/?q=QUERY&license_type=commercial&page_size=20` (also `/v1/audio/`), Wikimedia Commons `https://commons.wikimedia.org/w/api.php?action=query&generator=search&gsrnamespace=6&gsrsearch=QUERY&prop=imageinfo&iiprop=url|extmetadata&format=json`, the Art Institute of Chicago `https://api.artic.edu/api/v1/artworks/search?q=QUERY&query[term][is_public_domain]=true&fields=id,title,image_id,artist_display` (image: `https://www.artic.edu/iiif/2/IMAGE_ID/full/1686,/0/default.jpg`), the Met `https://collectionapi.metmuseum.org/public/collection/v1/search?hasImages=true&q=QUERY`.
   - Search the inventory, not the category: its street in old postcards, an old map of the district, its material up close, its trade in engravings, a botanical plate. Download each picture into the project (`assets/`), look at it before using it, record title, creator, licence and source; the credit line sits small in the footer. A found picture shows the client's world and is never presented as the client's own product, room, dish or team.
   - **Floor:** at least four real raster pictures on a page, usually 6–12 (for software, crops of its real interface built in HTML count). SVG drawings do not count. One dominant image beats six small ones: on top sites the hero media covers half the first screen or more. Same crop ratios and treatment; square corners unless the direction says otherwise.
5. **The live thing**, in the section where it proves the claim.
6. **Fullness and frame:** look at each desktop screen as a poster — one dominant thing and its content. Equal outer margins at every width; when text sits in one half, the other half holds its picture, list or figure, never nothing. No empty stretch taller than about half a screen (one deliberate rest per page at most); sections 120–240 px apart on desktop. One `h1` on the first screen; every section opens with a heading that names it. Running text in full ink at 16 px or more.
7. **States and semantics:** header/nav/main/section, real links and buttons, labels, visible focus, alt text, hit areas ≥ 44 px, a phone layout that is its own composition. Frameworks: the project's own; standalone page: one HTML file, CSS in `<style>`, no build step.

### 7. Gate (measure, don't hope)
Serve the page and run a playwright check at **1440, 768 and 390 px** (python3 -m playwright, chromium headless; see webapp-testing). Save full-page screenshots and evaluate in the page, then fix every failure and re-run:
- no horizontal overflow (`document.documentElement.scrollWidth > innerWidth`);
- every `h1`–`h6` at most two lines (`el.getClientRects()` / height ÷ line-height);
- exactly one `h1`; every `section` has a heading;
- fonts actually loaded (`document.fonts.check`), at most two families in computed styles;
- no computed `background-image: linear-gradient`/`radial-gradient` (except a scrim under text on a photo), no `box-shadow` on non-floating elements, no `backdrop-filter`, no `font-family: monospace` outside `pre`/`code`;
- body text ≥ 16 px; contrast of text vs its background ≥ 4.5:1;
- ≥ 4 loaded `img`/`video`/`picture` elements wider than 200 px; no broken images (`naturalWidth === 0`);
- left and right page margins equal (±4 px) at each width.

### 8. Look
Passing the gate only means nothing banned was measured. Open every screenshot (read the PNGs), every width, not only the first screen. Critique like a juror: what do I see first, second? Is the idea visible? Does any section look like a stock block? Empty stretches, huddled sections, overlapping text, cramped phone layout? Score design / usability / creativity / content 1–10, fix the **top three problems**, re-shoot. Three rounds at most. If it is visible in a screenshot it is real, whatever the code says.

### 9. Move
Now the motion layer — required, and more than one fade:
1. **One signature move** from the direction, done well (a picture that opens to the full screen, a word that buckles under scroll, the camera through the work).
2. **Entrances per section part:** heading, then text, then picture, staggered 60–90 ms, rising 16–32 px with opacity, once, on entering the viewport (IntersectionObserver). Content must stay visible if the script fails.
3. **Feedback on every control:** hover, press, focus (150–300 ms, ease-out).
4. **One moving picture:** a muted looping clip with a poster frame, or a slow scale/drift on the hero photo.
Transform and opacity only; every move has a `prefers-reduced-motion` fallback that keeps the content. Nothing loops for attention. Re-run the gate and check the screenshots for content that never appeared.

### 10. Report
Only when the gate passes at all three widths on the final version and you looked at every screenshot after the last change. Tell the user briefly: the direction and why, the idea sentence and the live thing, your last critique scores and what still falls short, every found picture with its licence and source, which drawings stand in for photos, and the sample content to replace before launch. Never claim a check you did not run.

## House rules (every build)
| Never | Instead |
|---|---|
| Headlines over two lines at any width | Cut words, widen the measure, size last |
| A small label/eyebrow/pill badge above a heading, "New ·" chips | Put it in the heading or first sentence, or drop it |
| Monospace outside real code | The text face; tabular figures where numbers align |
| Index numbering 01/02/03, "Step 2" on things that are not a sequence | Order by position, size or words; a real `<ol>` if order matters |
| Hairline dividers, 1px outlined boxes, grid lines, crop marks | Space, a step in background tone, a change of scale |
| Gradients (backgrounds, buttons, text), gradient blobs | Flat colour or a real photograph |
| Shadows or glows on things at rest; glass/backdrop blur; floating pill nav | A tone step; shadows only on what floats (menu, dialog); a plain bar: wordmark, 3–5 links, one action |
| The assistant palette: cream + terracotta, violet/indigo, purple-to-blue, near-black + acid green | The chosen palette from the subject |
| Tiny uppercase letter-spaced labels | Sentence case at a readable size |
| Icon-topped feature cards, rows of identical cards, hero = big number + three stat cards | A composition made for that section's content |
| Emoji as icons, decorative shapes, fake dashboards of grey bars, lorem/"Feature one" | A real picture, a real interface fragment, or nothing; inline SVG icons with one stroke width when icons are truly needed |
| One italic/coloured/underlined accent word in the headline | One face, one colour; the words carry the emphasis |
| More than two typefaces; "fade-up on every section, hover-lift on every card" | The type system; the motion layer above |

Exempt: real code in `pre`/`code`, real tables and form fields (they may have lines), and a real product interface fragment marked `data-ui`.

## Laws of the craft (from the study)
- **Type:** two or three sizes do the visible work — one very large, one small. Headline-to-body ratio ≥ 4 (top sites' largest type ≈ 8vw). Display tracking −0.02…−0.05em, leading 0.85–1.05; text 16–20 px, leading 1.45–1.6, 45–75 characters per line. Five sizes max. Left-aligned by default; centre at most one moment.
- **Space:** one grid, tight equal margins (16–32 px), large things run to the edges; text shares two or three left edges on the whole page; gaps between sections clearly larger than inside them; asymmetry inside the grid, never in the frame.
- **Colour:** one ground, one ink, one accent (on the action and maybe one other thing). Running text in full ink — a page in soft grey reads unfinished. Surfaces separated by a tone step, never by a line or shadow. A section may change the whole field colour.
- **Components:** buttons flat, one fill, square or fully round — decided once (the 9–16 px "soft" radius is the weak-site default). Most strong heroes have one button or none. The footer is a designed close (the name returns at full width), not a sitemap.
- **Copy:** active voice; buttons name the action ("Book a table", not "Submit"); the same thing keeps one name; errors say what happened and how to fix it.

## Redesign, restyle or review
1. Run the project as it is, shoot 1440/768/390 into `before/`, read the code: framework, styling method, components, and what must survive (routes, forms, data, brand assets).
2. **A review stops here:** critique the before shots, list findings with measurements, name the three changes that would help most.
3. Keep the stack, the content and what works — no framework migration, check the dependency file before adding anything.
4. Order of work, most effect for least risk: type → colour → frame (margins, rhythm, title, section headings) → states → replace stock components.
5. Shoot `after/`, compare side by side, click through every link, form and menu that worked before.

## App screens (dashboards, admin, settings, inboxes)
Different rules: these are worked in, not read. Quiet 1px borders, small labels and repeated panels are normal here; gradients, glows, icons in tinted squares, rainbow charts and invented vanity numbers are not.
- Start from the job: what does the user check or do here ten times a day? That goes top-left and largest. One primary action per view.
- Dense but calm: 13–14 px UI text, 8 px spacing grid, tabular figures, aligned columns, one accent for state/action, red/amber/green only for real status.
- Real data that adds up (totals match rows, dates are plausible), one finding stated in words above a chart; charts in one or two hues, labelled directly, no legends you can avoid.
- Every view has empty, loading and error states; tables sort, filter and paginate; keyboard focus works; 360 px gets its own layout (cards or a stacked list), not a squeezed table.
- Gate: overflow, contrast, focus, the states, screenshots at 1440 and 390, light and dark.
