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
        r = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/html,application/json,text/plain,*/*",
                                                        "Accept-Encoding": "gzip, deflate"})
        with urllib.request.urlopen(r, timeout=30) as resp:
            ct = resp.headers.get("Content-Type", "")
            data = resp.read(5_000_000)
            enc = (resp.headers.get("Content-Encoding") or "").lower()
    except Exception as e:
        return False, f"error: {e}", None
    if enc == "gzip" or data[:2] == b"\x1f\x8b":
        data = zlib.decompress(data, 16 + zlib.MAX_WBITS)
    elif enc == "deflate":
        data = zlib.decompress(data)
    if "pdf" in ct:
        fd, tmp = tempfile.mkstemp(suffix=".pdf")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            return True, clip(_pdf_text(tmp), 40000), None
        finally:
            os.unlink(tmp)
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


SEARCH_APIS = ("tavily", "exa", "brave")  # free tiers: Tavily 1000 searches a month, Exa $10 of credits, Brave with a card
SEARCH_ASK = ("Web search works much better with a free Tavily key (1000 searches a month): sign up at https://app.tavily.com, "
              "copy the key and send /searchkey tvly-… — add several keys and they are used in turn. Exa and Brave work too: /searchkey exa KEY")
_SPENT = {}  # key → when it ran out or was refused; skipped for an hour


def search_keys(cfg, api):
    v = (cfg.get("search") or {}).get(api) or []
    return [k for k in ([v] if isinstance(v, str) else v) if k]


def search_key(cfg, arg):
    """/searchkey [api] KEY adds a key (several per API rotate), /searchkey clear [api] drops them. Returns a status line."""
    s = cfg.setdefault("search", {})
    w = arg.split()
    if w and w[0] == "clear":
        for a in SEARCH_APIS:
            if len(w) < 2 or w[1] == a:
                s.pop(a, None)
    elif w:
        a = w.pop(0).lower() if w[0].lower() in SEARCH_APIS else "tavily" if w[0].startswith("tvly-") else "brave" if w[0].startswith("BSA") else \
            "exa" if re.fullmatch(r"[0-9a-f-]{36}", w[0]) else None
        if not a or not w:
            raise ValueError("usage: /searchkey tvly-… · /searchkey exa|brave KEY · /searchkey clear")
        s[a] = list(dict.fromkeys(search_keys(cfg, a) + w))
    have = [f"{a} ×{len(search_keys(cfg, a))}" for a in SEARCH_APIS if search_keys(cfg, a)]
    return "search keys: " + (", ".join(have) if have else "none — " + SEARCH_ASK)


def _search_api(api, key, query):
    """One request to a search API → a list of (title, url, snippet) and an optional short answer."""
    import urllib.parse as up
    if api == "tavily":
        r = urllib.request.Request("https://api.tavily.com/search", method="POST", headers={"Content-Type": "application/json", "Authorization": "Bearer " + key},
                                   data=json.dumps({"query": query, "max_results": 8, "include_answer": True}).encode())
        j = json.loads(urllib.request.urlopen(r, timeout=40).read())
        return [(x.get("title", ""), x.get("url", ""), x.get("content", "")) for x in j.get("results", [])], j.get("answer") or ""
    if api == "exa":
        r = urllib.request.Request("https://api.exa.ai/search", method="POST", headers={"Content-Type": "application/json", "x-api-key": key},
                                   data=json.dumps({"query": query, "numResults": 8, "contents": {"text": {"maxCharacters": 600}}}).encode())
        j = json.loads(urllib.request.urlopen(r, timeout=40).read())
        return [(x.get("title") or "", x.get("url", ""), x.get("text") or "") for x in j.get("results", [])], ""
    r = urllib.request.Request("https://api.search.brave.com/res/v1/web/search?count=8&q=" + up.quote(query),
                               headers={"Accept": "application/json", "X-Subscription-Token": key})
    j = json.loads(urllib.request.urlopen(r, timeout=40).read())
    return [(x.get("title", ""), x.get("url", ""), html_text(x.get("description", ""))) for x in (j.get("web") or {}).get("results", [])], ""


def keyed_search(cfg, query):
    """Tavily, then Exa, then Brave; within one API the keys take turns as each runs out. None when no key worked."""
    for api in SEARCH_APIS:
        for key in search_keys(cfg, api):
            if time.time() - _SPENT.get(key, 0) < 3600:
                continue
            try:
                res, ans = _search_api(api, key, query)
            except urllib.error.HTTPError as e:
                if e.code in (401, 402, 403, 429, 432, 433):  # bad key, out of credits, rate limit: next key
                    _SPENT[key] = time.time()
                continue
            except Exception:
                break  # the API itself is down: next API
            out = ["- %s — %s\n  %s" % (t.strip(), u, " ".join(c.split())[:400]) for t, u, c in res[:8]]
            return (ans.strip() + "\n\n" if ans.strip() else "") + ("\n".join(out) or "no results")
    return None


def web_search(cfg, query, cancel=None):
    found = keyed_search(cfg, query)
    if found is not None:
        return True, found, None
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
