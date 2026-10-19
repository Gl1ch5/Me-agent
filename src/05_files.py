# ───────────────────────────── file reading ─────────────────────────────

IMAGE_EXT = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp"}


def _xml_text(data):
    data = re.sub(rb"</(w:p|a:p|text:p|row)>", b"\n", data)
    data = re.sub(rb"</(w:tc|c)>", b"\t", data)
    txt = re.sub(rb"<[^>]+>", b"", data).decode("utf-8", "replace")
    for a, b in (("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"'), ("&apos;", "'"), ("&amp;", "&")):
        txt = txt.replace(a, b)
    return re.sub(r"\n\s*\n+", "\n", txt).strip()


def _office_text(path):
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        parts = []
        if "word/document.xml" in names:
            parts = ["word/document.xml"]
        elif any(n.startswith("ppt/slides/slide") for n in names):
            parts = sorted([n for n in names if re.match(r"ppt/slides/slide\d+\.xml$", n)], key=lambda n: int(re.findall(r"\d+", n)[0]))
        elif "xl/sharedStrings.xml" in names or any(n.startswith("xl/worksheets/") for n in names):
            shared = []
            if "xl/sharedStrings.xml" in names:
                shared = [_xml_text(s) for s in re.findall(rb"<si>(.*?)</si>", z.read("xl/sharedStrings.xml"), re.S)]
            out = []
            for n in sorted(n for n in names if re.match(r"xl/worksheets/sheet\d+\.xml$", n)):
                out.append(f"## {n.split('/')[-1]}")
                for row in re.findall(rb"<row[^>]*>(.*?)</row>", z.read(n), re.S):
                    cells = []
                    for attrs, val in re.findall(rb"<c([^>]*)>(.*?)</c>", row, re.S):
                        v = re.search(rb"<v>(.*?)</v>", val) or re.search(rb"<t[^>]*>(.*?)</t>", val)
                        v = v.group(1).decode() if v else ""
                        if b't="s"' in attrs and v.isdigit() and int(v) < len(shared):
                            v = shared[int(v)]
                        cells.append(v)
                    out.append("\t".join(cells))
            return "\n".join(out)
        elif "content.xml" in names:
            parts = ["content.xml"]
        return "\n\n".join(_xml_text(z.read(p)) for p in parts)


def _pdf_text(path):
    raw = Path(path).read_bytes()
    out = []
    for m in re.finditer(rb"stream\r?\n(.*?)\r?\nendstream", raw, re.S):
        s = m.group(1)
        try:
            s = zlib.decompress(s)
        except Exception:
            pass
        for bt in re.findall(rb"BT(.*?)ET", s, re.S):
            line = []
            for t in re.findall(rb"\((?:\\.|[^\\)])*\)|\[(?:[^\]])*\]|T\*|Td|TD|'", bt):
                if t.startswith(b"["):
                    line.append(b"".join(x[1:-1] for x in re.findall(rb"\((?:\\.|[^\\)])*\)", t)))
                elif t.startswith(b"("):
                    line.append(t[1:-1])
                else:
                    line.append(b"\n")
            txt = b"".join(line)
            txt = re.sub(rb"\\([nrt()\\])", lambda x: {b"n": b"\n", b"r": b"", b"t": b"\t"}.get(x.group(1), x.group(1)), txt)
            out.append(txt.decode("latin-1"))
    text = "".join(out).strip()
    return text or "[pdf: no extractable text (scanned or encoded fonts)]"


def decode_text(data):
    """bytes → (text, encoding, bom). UTF-8 (with or without BOM), UTF-16 with BOM, else the system's legacy code page
    (cp1251 on a Russian Windows), else latin-1. Writing back with the same encoding keeps the file as it was."""
    for bom, enc in ((b"\xef\xbb\xbf", "utf-8"), (b"\xff\xfe", "utf-16-le"), (b"\xfe\xff", "utf-16-be")):
        if data.startswith(bom):
            return data[len(bom):].decode(enc, "replace"), enc, bom
    try:
        return data.decode("utf-8"), "utf-8", b""
    except UnicodeDecodeError:
        pass
    import locale
    for enc in dict.fromkeys([locale.getpreferredencoding(False), "cp1251"]):
        try:
            if enc.lower().replace("-", "") not in ("utf8", "ascii"):
                return data.decode(enc), enc, b""
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("latin-1"), "latin-1", b""


def read_text_file(path):
    """A text file for editing: (text with \n line ends, how to write it back)."""
    text, enc, bom = decode_text(Path(path).read_bytes())
    crlf, lf = text.count("\r\n"), text.count("\n")
    return text.replace("\r\n", "\n"), {"enc": enc, "bom": bom, "nl": "\r\n" if crlf and crlf * 2 >= lf else "\n"}


def write_text_file(path, text, fmt=None):
    """Write \n-text with the file's own line ends, encoding and BOM (no newline translation: on Windows Python would
    otherwise turn every LF file into CRLF)."""
    fmt = fmt or {"enc": "utf-8", "bom": b"", "nl": "\n"}
    if fmt["nl"] == "\r\n":
        text = text.replace("\r\n", "\n").replace("\n", "\r\n")
    try:
        data = text.encode(fmt["enc"])
    except UnicodeEncodeError:  # a character the legacy code page cannot hold: switch the file to UTF-8 rather than lose it
        data = text.encode("utf-8")
        fmt = dict(fmt, enc="utf-8", bom=b"")
    Path(path).write_bytes(fmt["bom"] + data)


READ_CAP = 50_000  # characters of one read result


def read_any(path, offset=1, limit=2000):
    p = Path(path).expanduser()
    if not p.exists():
        hint = ""
        try:
            near = difflib.get_close_matches(p.name, [x.name for x in p.parent.iterdir()], n=3, cutoff=0.6) if p.parent.is_dir() else []
            hint = f" — did you mean: {', '.join(near)}?" if near else ""
        except OSError:
            pass
        return f"error: {path} not found{hint}", None
    if p.is_dir():
        items = sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))
        return "\n".join(x.name + ("/" if x.is_dir() else "") for x in items[:500]) + (f"\n… {len(items) - 500} more" if len(items) > 500 else "") \
            or "(empty dir)", None
    ext = p.suffix.lower()
    if ext in IMAGE_EXT:
        size = p.stat().st_size
        if size > 8_000_000:
            return f"error: image {p.name} is {size // 1_000_000} MB, too big to send to the model (resize it first)", None
        b64 = base64.b64encode(p.read_bytes()).decode()
        return f"[image {p.name}, {size} bytes attached]", f"data:{IMAGE_EXT[ext]};base64,{b64}"
    try:
        if ext in (".docx", ".pptx", ".xlsx", ".odt", ".ods", ".odp"):
            text = _office_text(p)
        elif ext == ".pdf":
            text = _pdf_text(p)
        elif ext == ".ipynb":
            nb = json.loads(p.read_text("utf-8"))
            text = "\n\n".join(f"# [{c['cell_type']}]\n" + "".join(c.get("source", [])) for c in nb.get("cells", []))
        else:
            data = p.read_bytes()
            if b"\0" in data[:8192] and not data.startswith((b"\xff\xfe", b"\xfe\xff")):
                head = data[:512]
                hexd = "\n".join(f"{i:08x}  {head[i:i + 16].hex(' ')}" for i in range(0, len(head), 16))
                return f"[binary {p.name}, {len(data)} bytes]\n{hexd}", None
            text = decode_text(data)[0]
    except Exception as e:
        return f"error reading {path}: {e}", None
    lines = text.replace("\r\n", "\n").split("\n")  # the same numbering the edit tool's lines= uses
    if lines and lines[-1] == "":
        lines.pop()
    offset = max(1, int(offset or 1))
    limit = max(1, int(limit or 2000))
    if offset > len(lines) and lines:
        return f"error: offset {offset} is past the end ({len(lines)} lines)", None
    w = len(str(min(len(lines), offset + limit)))
    out, used, i = [], 0, offset - 1
    for i in range(offset - 1, min(len(lines), offset - 1 + limit)):
        l = lines[i]
        row = f"{i + 1:>{w}}│{l[:2000]}" + (" …[line cut]" if len(l) > 2000 else "")
        if used + len(row) > READ_CAP and out:
            return "\n".join(out) + f"\n… output capped at {READ_CAP // 1000}k chars; {len(lines) - i} more lines (use offset={i + 1})", None
        out.append(row)
        used += len(row) + 1
    rest = len(lines) - (offset - 1 + len(out))
    return ("\n".join(out) + (f"\n… {rest} more lines (use offset={offset + len(out)})" if rest > 0 else "")) or "(empty file)", None
