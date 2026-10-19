# ───────────────────────────── util ─────────────────────────────


def jload(p, default=None):
    try:
        return json.loads(Path(p).read_text("utf-8"))
    except Exception:
        return default


def jsave(p, data):
    p = Path(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=p.name + ".", suffix=".tmp")  # unique: two threads may save at once
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(json.dumps(data, ensure_ascii=False, indent=1))
    for i in range(10):
        try:
            return os.replace(tmp, p)
        except PermissionError:  # Windows: a reader, antivirus or OneDrive holds the file for a moment
            if i == 9:
                os.unlink(tmp)
                raise
            time.sleep(0.05 * (i + 1))


def read_raw(p):
    """File text exactly as stored (no newline translation: on Windows read_text/write_text turned LF files into CRLF)."""
    with open(p, encoding="utf-8", newline="") as f:
        return f.read()


def write_raw(p, text):
    with open(p, "w", encoding="utf-8", newline="") as f:
        f.write(text)


def relp(path, start=None):
    """os.path.relpath that never raises: on Windows a path on another drive (C: vs D:) has no relative form, keep it absolute."""
    try:
        return os.path.relpath(path, start or os.getcwd())
    except ValueError:
        return os.path.abspath(path)


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


_CACHE = {}


def dir_sig(d, pattern="*"):
    """Cheap fingerprint of a folder's files (name, mtime, size): one listdir + stat per file, no reads."""
    try:
        return tuple((p.name, st.st_mtime_ns, st.st_size) for p in sorted(Path(d).glob(pattern)) for st in (p.stat(),))
    except OSError:
        return ()


def cached(key, sig, fn):
    """fn() once per distinct sig: parsing every skill/agent/theme file on each call made each tool step and TUI frame hit the disk."""
    hit = _CACHE.get(key)
    if hit and hit[0] == sig:
        return hit[1]
    val = fn()
    _CACHE[key] = (sig, val)
    return val
