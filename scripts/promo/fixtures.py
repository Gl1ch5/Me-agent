"""Fixed files the promo mock 'writes' (deterministic, no real content). Palette = me-dark."""


COPY_RU = dict(logo="◆ kofe &amp; co", nav1="меню", nav2="адрес", h1="Кофе, который просыпается раньше вас",
               lead="Свежая обжарка каждую неделю. Заходите за эспрессо или оставайтесь на весь день.",
               cta="Смотреть меню", items=[("Эспрессо", "двойной, 2 шота, шоколад"), ("Флэт уайт", "молоко, бархатная пенка"), ("Фильтр", "сезонный сорт, V60")])
COPY_EN = dict(logo="◆ kofe &amp; co", nav1="menu", nav2="visit", h1="Coffee that wakes up before you do",
               lead="Fresh roast every week. Stop by for an espresso or stay all day.",
               cta="See the menu", items=[("Espresso", "double shot, chocolate notes"), ("Flat white", "milk, velvet foam"), ("Filter", "seasonal beans, V60")])


def page(bg, fg, dim, accent, accent2, title, copy=COPY_RU):
    c = copy
    items = "".join(f'<div class="item"><b>{a}</b><p>{b}</p></div>' for a, b in c["items"])
    return f"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
<style>
:root{{--bg:{bg};--fg:{fg};--dim:{dim};--accent:{accent};--accent2:{accent2}}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--fg);font:16px/1.5 system-ui,sans-serif}}
header{{display:flex;justify-content:space-between;align-items:center;padding:20px 6vw}}
.logo{{font-family:monospace;font-weight:700}}nav a{{color:var(--dim);margin-left:20px;text-decoration:none}}
.hero{{padding:12vh 6vw 10vh}}h1{{font-size:clamp(36px,6vw,64px);line-height:1.05;margin:0 0 18px;max-width:14ch}}
.hero p{{color:var(--dim);max-width:44ch;font-size:18px}}
.cta{{display:inline-block;margin-top:28px;padding:14px 26px;border-radius:12px;background:var(--accent);color:var(--bg);font-weight:600;text-decoration:none}}
.menu{{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:16px;padding:0 6vw 12vh}}
.item{{border:1px solid color-mix(in srgb,var(--fg) 14%,transparent);border-radius:16px;padding:22px}}
.item b{{color:var(--accent2)}}
</style></head>
<body>
<header><span class="logo">{c["logo"]}</span><nav><a href="#menu">{c["nav1"]}</a><a href="#visit">{c["nav2"]}</a></nav></header>
<section class="hero"><h1>{c["h1"]}</h1>
<p>{c["lead"]}</p>
<a class="cta" href="#menu">{c["cta"]}</a></section>
<section class="menu" id="menu">
{items}
</section>
</body></html>
"""


LANDING = page("#0e0f13", "#d8dae3", "#6b6f80", "#8b7cff", "#4fd1c5", "kofe & co")
LANDING_DARK_THEME = page("#212121", "#ececec", "#9b9b9b", "#ececec", "#a3a3a3", "kofe & co")
LANDING_EN = page("#0e0f13", "#d8dae3", "#6b6f80", "#8b7cff", "#4fd1c5", "kofe & co", COPY_EN)
LANDING_EN_DARK = page("#212121", "#ececec", "#9b9b9b", "#ececec", "#a3a3a3", "kofe & co", COPY_EN)

DESIGN_MD = """# DESIGN — kofe & co

## Goal
Landing page for a coffee shop. Audience: commuters on phones, 25–40.

## Principles
- one accent, neutral surfaces, generous spacing (4/8 scale)
- mobile first, readable at 360px, contrast >= 4.5:1

## Tokens
- palette: bg #0e0f13 · fg #d8dae3 · dim #6b6f80 · accent #8b7cff · accent2 #4fd1c5
- type: system-ui 16/1.5, headline 64/1.05
- radius 12 (button) · 16 (cards) · motion: none beyond hover

## Structure
tokens/ · components/ · pages/ · assets/icons/

## States
button: default · hover · focus ring · disabled
"""
