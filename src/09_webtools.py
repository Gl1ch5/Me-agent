# ───────────────────────────── web tools ─────────────────────────────

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"


def html_text(s):
    s = re.sub(r"(?is)<(script|style|noscript|svg|head|nav|footer)\b.*?</\1>", "", s)
    s = re.sub(r"(?i)<h([1-6])[^>]*>", lambda m: "\n" + "#" * int(m.group(1)) + " ", s)
    s = re.sub(r"(?i)<li[^>]*>", "\n- ", s)
    s = re.sub(r"(?i)<(br|/p|/div|/h\d|/tr|/pre|/section|/article)[^>]*>", "\n", s)
    s = re.sub(r"<[^>]+>", "", s)
    import html as _h
    s = _h.unescape(s)
    return re.sub(r"\n\s*\n+", "\n\n", re.sub(r"[ \t]+", " ", s)).strip()


def web_fetch(url):
    try:
        r = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/html,application/json,text/plain,*/*"})
        with urllib.request.urlopen(r, timeout=30) as resp:
            ct = resp.headers.get("Content-Type", "")
            data = resp.read(5_000_000)
    except Exception as e:
        return False, f"error: {e}", None
    if "pdf" in ct:
        tmp = Path(tempfile.mkstemp(suffix=".pdf")[1])
        tmp.write_bytes(data)
        return True, clip(_pdf_text(tmp), 40000), None
    text = data.decode("utf-8", "replace")
    if "html" in ct or text.lstrip()[:15].lower().startswith(("<!doctype", "<html")):
        text = html_text(text)
    return True, clip(text, 40000), None


def deepseek_key(cfg):
    k = (cfg.get("search") or {}).get("deepseek_key")
    if k:
        return k
    for p in cfg.get("providers", {}).values():
        if "deepseek.com" in p.get("base_url", "") and p.get("api_key"):
            return p["api_key"]
    return None


def web_search(cfg, query, cancel=None):
    key = deepseek_key(cfg)
    if key:
        body = {"model": (cfg.get("search") or {}).get("model") or "deepseek-v4-flash", "input": query,
                "tools": [{"type": "web_search"}], "tool_choice": {"type": "web_search"}, "max_output_tokens": 4000}
        try:
            r = urllib.request.Request("https://api.deepseek.com/responses", data=json.dumps(body).encode(), method="POST",
                                       headers={"Content-Type": "application/json", "Authorization": "Bearer " + key})
            with urllib.request.urlopen(r, timeout=180) as resp:
                j = json.loads(resp.read())
            msgs = [o for o in j.get("output", []) if o.get("type") == "message"]
            final = [m for m in msgs if m.get("phase") == "final_answer"] or msgs[-1:]
            ans = "".join(c.get("text", "") for m in final for c in m.get("content", []) if c.get("type") == "output_text")
            urls = []
            for m in final:
                for c in m.get("content", []):
                    urls += [a.get("url") for a in c.get("annotations", []) if a.get("url")]
            for o in j.get("output", []):
                if o.get("type") == "web_search_call" and (o.get("action") or {}).get("url"):
                    urls.append(o["action"]["url"])
            src = "\n".join(f"- {u}" for u in dict.fromkeys(urls))
            if ans.strip():
                return True, ans + (f"\n\nSources:\n{src}" if src else ""), None
        except Exception:
            pass  # fall back to DuckDuckGo
    try:
        import urllib.parse as up
        r = urllib.request.Request("https://html.duckduckgo.com/html/?q=" + up.quote(query), headers={"User-Agent": UA})
        page = urllib.request.urlopen(r, timeout=30).read().decode("utf-8", "replace")
    except Exception as e:
        return False, f"search failed: {e}", None
    out = []
    for m in re.finditer(r'class="result__a" href="([^"]+)"[^>]*>(.*?)</a>.*?class="result__snippet"[^>]*>(.*?)</a>', page, re.S):
        href = m.group(1)
        u = up.parse_qs(up.urlparse(href).query).get("uddg", [href])[0]
        out.append(f"- {html_text(m.group(2))} — {u}\n  {html_text(m.group(3))}")
        if len(out) >= 8:
            break
    return bool(out), "\n".join(out) or "no results", None
