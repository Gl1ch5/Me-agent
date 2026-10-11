"""
name: web
description: fetch web pages as text and search the web (DuckDuckGo)
icon: 🌐
color: blue
---
search(query) → titles+urls; fetch(url) → readable text. Cite urls.
"""
import html, re, urllib.parse


def _text(s):
    s = re.sub(r"(?is)<(script|style|noscript|svg).*?</\1>", "", s)
    s = re.sub(r"(?i)<(br|/p|/div|/h\d|/li|/tr)>", "\n", s)
    s = html.unescape(re.sub(r"<[^>]+>", "", s))
    return re.sub(r"\n\s*\n+", "\n\n", s).strip()


@action
def fetch(ctx, url: str, max_chars=20000):
    "Fetch a URL as plain text"
    ctx.status(f"GET {url}")
    r = ctx.http.get(url)
    return (_text(r) if isinstance(r, str) else str(r))[:max_chars]


@action
def search(ctx, query: str):
    "Web search"
    ctx.status(f"searching: {query}")
    page = ctx.http.get("https://html.duckduckgo.com/html/?q=" + urllib.parse.quote(query))
    out = []
    for href, title in re.findall(r'class="result__a" href="([^"]+)"[^>]*>(.*?)</a>', page)[:10]:
        u = urllib.parse.parse_qs(urllib.parse.urlparse(href).query).get("uddg", [href])[0]
        out.append(f"{_text(title)} — {u}")
    return "\n".join(out) or "no results"
