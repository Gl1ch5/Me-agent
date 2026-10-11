# ───────────────────────────── util ─────────────────────────────


def jload(p, default=None):
    try:
        return json.loads(Path(p).read_text("utf-8"))
    except Exception:
        return default


def jsave(p, data):
    p = Path(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), "utf-8")
    os.replace(tmp, p)


def human(n):
    n = int(n or 0)
    if n < 1000:
        return str(n)
    if n < 1_000_000:
        return f"{n / 1000:.1f}k".replace(".0k", "k")
    return f"{n / 1_000_000:.1f}M".replace(".0M", "M")


def dur(s):
    return f"{s:.1f}s" if s < 60 else f"{int(s // 60)}m{int(s % 60):02d}s"


def clip(s, n):
    s = str(s)
    return s if len(s) <= n else s[: n // 2] + f"\n…[{len(s) - n} chars cut]…\n" + s[-n // 2:]
