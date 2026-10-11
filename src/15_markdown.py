# ───────────────────────────── markdown ─────────────────────────────

KW = r"\b(def|class|return|if|elif|else|for|while|import|from|as|try|except|finally|with|lambda|yield|async|await|const|let|var|function|" \
     r"export|new|this|self|true|false|True|False|None|null|nil|fn|pub|struct|impl|match|in|not|and|or|is|echo|then|fi|do|done|package|func)\b"


def hl_code(line, lang):
    """Very small highlighter → segments."""
    base = rgb(TH["fg"]) + rgb(TH["code_bg"], True)
    if lang in ("diff",) or (line[:1] in "+-" and lang == "diff"):
        col = TH["add"] if line.startswith("+") else TH["del"] if line.startswith("-") else TH["fg"]
        return [(line, rgb(col) + rgb(TH["code_bg"], True))]
    segs, pos = [], 0
    rx = re.compile(r"(#.*$|//.*$|\"(?:\\.|[^\"])*\"|'(?:\\.|[^'])*'|`[^`]*`|\b\d+(?:\.\d+)?\b|" + KW + ")")
    for m in rx.finditer(line):
        if m.start() > pos:
            segs.append((line[pos:m.start()], base))
        t = m.group(0)
        if t.startswith(("#", "//")) and lang not in ("css",):
            col = TH["dim"]
        elif t[0] in "\"'`":
            col = TH["ok"]
        elif t[0].isdigit():
            col = TH["warn"]
        else:
            col = TH["accent"]
        segs.append((t, rgb(col) + rgb(TH["code_bg"], True)))
        pos = m.end()
    if pos < len(line):
        segs.append((line[pos:], base))
    return segs


def inline_md(text, base):
    segs = []
    pat = re.compile(r"(\*\*[^*]+\*\*|`[^`]+`|\*[^*\s][^*]*\*|\[[^\]]+\]\([^)]+\))")
    pos = 0
    for m in pat.finditer(text):
        if m.start() > pos:
            segs.append((text[pos:m.start()], base))
        t = m.group(0)
        if t.startswith("**"):
            segs.append((t[2:-2], base + CSI + "1m"))
        elif t.startswith("`"):
            segs.append((t[1:-1], rgb(TH["accent2"]) + rgb(TH["code_bg"], True)))
        elif t.startswith("["):
            lbl, url = re.match(r"\[([^\]]+)\]\(([^)]+)\)", t).groups()
            segs.append((lbl, rgb(TH["accent"]) + CSI + "4m"))
            segs.append((f" ({url})", rgb(TH["dim"])))
        else:
            segs.append((t[1:-1], base + CSI + "3m"))
        pos = m.end()
    if pos < len(text):
        segs.append((text[pos:], base))
    return segs


def render_md(text, width, indent=2):
    out = []
    base = rgb(TH["fg"])
    in_code, lang = False, ""
    for raw in text.split("\n"):
        if raw.strip().startswith("```"):
            if not in_code:
                in_code, lang = True, raw.strip()[3:].strip().lower()
                out.append(Line().add(" " * indent).add(f" {lang or 'code'} ".ljust(width - indent), rgb(TH["dim"]) + rgb(TH["code_bg"], True)))
            else:
                in_code = False
                out.append(Line().add(" " * indent).add(" " * (width - indent), rgb(TH["code_bg"], True)))
            continue
        if in_code:
            segs = hl_code(raw.replace("\t", "    "), lang)
            ls = wrap_segs(segs, width, first_prefix=[(" " * indent, ""), (" ", rgb(TH["code_bg"], True))],
                           prefix=[(" " * indent, ""), (" ", rgb(TH["code_bg"], True))])
            for l in ls:
                pad = width - l.width()
                if pad > 0:
                    l.add(" " * pad, rgb(TH["code_bg"], True))
            out.extend(ls)
            continue
        s = raw.rstrip()
        m = re.match(r"^(#{1,6})\s+(.*)", s)
        if m:
            lvl = len(m.group(1))
            col = TH["accent"] if lvl <= 2 else TH["accent2"]
            out.extend(wrap_segs([(m.group(2), rgb(col) + CSI + "1m")], width, indent))
            continue
        m = re.match(r"^(\s*)([-*+]|\d+[.)])\s+(.*)", s)
        if m:
            ind = indent + len(m.group(1))
            bullet = "•" if m.group(2) in "-*+" else m.group(2)
            pre = [(" " * ind, ""), (bullet + " ", rgb(TH["accent"]))]
            out.extend(wrap_segs(inline_md(m.group(3), base), width, first_prefix=pre, prefix=[(" " * (ind + swidth(bullet) + 1), "")]))
            continue
        if re.match(r"^\s*(---+|\*\*\*+)\s*$", s):
            out.append(Line().add(" " * indent).add("─" * (width - indent * 2), rgb(TH["faint"])))
            continue
        if s.startswith(">"):
            out.extend(wrap_segs(inline_md(s.lstrip("> "), rgb(TH["dim"]) + CSI + "3m"), width,
                                 first_prefix=[(" " * indent, ""), ("▌ ", rgb(TH["faint"]))], prefix=[(" " * indent, ""), ("▌ ", rgb(TH["faint"]))]))
            continue
        if "|" in s and s.strip().startswith("|"):
            if re.match(r"^\s*\|[\s:|-]+\|\s*$", s):
                continue
            cells = [c.strip() for c in s.strip().strip("|").split("|")]
            out.extend(wrap_segs(inline_md("  │  ".join(cells), base), width, indent))
            continue
        out.extend(wrap_segs(inline_md(s, base), width, indent))
    return out
