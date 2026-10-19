# ───────────────────────────── terminal ─────────────────────────────

ESC = "\x1b"
CSI = ESC + "["
RESET = CSI + "0m"


def rgb(h, bg=False):
    h = h.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return f"{CSI}{48 if bg else 38};2;{r};{g};{b}m"


def mix(a, b, t):
    a, b = a.lstrip("#"), b.lstrip("#")
    return "#" + "".join(f"{int(int(a[i:i + 2], 16) * (1 - t) + int(b[i:i + 2], 16) * t):02x}" for i in (0, 2, 4))


TH = {  # theme
    "bg": "#0e0f13", "fg": "#d8dae3", "dim": "#6b6f80", "faint": "#3a3d4a", "accent": "#8b7cff", "accent2": "#4fd1c5",
    "user": "#e7c66b", "ok": "#5ad18a", "err": "#ff6b7a", "warn": "#ffb454", "code_bg": "#16181f", "sel": "#262a38",
    "add": "#5ad18a", "del": "#ff6b7a",
}
SKILL_COLORS = {"red": "#ff6b7a", "green": "#5ad18a", "blue": "#6ba6ff", "magenta": "#d27cff", "cyan": "#4fd1c5", "yellow": "#e7c66b",
                "orange": "#ffb454", "purple": "#8b7cff", "pink": "#ff8fc7"}
SPIN = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
BAR = " ▏▎▍▌▋▊▉█"


def cw(ch):
    if ch < " ":
        return 0
    if unicodedata.combining(ch):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in "WF" else 1


def swidth(s):
    return sum(cw(c) for c in s)


def cells(s, lo, hi):
    """The part of s that sits in display columns [lo, hi)."""
    out, w = [], 0
    for c in s:
        if lo <= w < hi:
            out.append(c)
        w += cw(c)
    return "".join(out)


class Line:
    """A display line: list of (text, style) segments.
    For copying: `pre` leading cells are decoration (gutter, bullet indent, box border), `end` is where the content stops
    (padding and right borders follow), `soft` is set on a wrapped continuation: the text that joins it to the line above."""
    __slots__ = ("segs", "pre", "end", "soft")

    def __init__(self, segs=None):
        self.segs = segs or []
        self.pre, self.end, self.soft = 0, None, None

    def add(self, text, style=""):
        if text:
            self.segs.append((text, style))
        return self

    def width(self):
        return sum(swidth(t) for t, _ in self.segs)

    def pad(self, width, style=""):
        """Fill to `width` with styled spaces (background); the filler is not part of the copied text."""
        if self.end is None:
            self.end = self.width()
        w = self.width()
        if width > w:
            self.add(" " * (width - w), style)
        return self

    def cells(self, c0, c1):
        """Text of the cells [c0, c1)."""
        out, w = [], 0
        for t, _ in self.segs:
            for c in t:
                if w >= c1:
                    return "".join(out)
                if w >= c0:
                    out.append(c)
                w += cw(c)
        return "".join(out)

    def copy_text(self, c0=0, c1=1 << 30):
        end = self.width() if self.end is None else self.end
        return self.cells(max(c0, self.pre), min(c1, end))

    def render(self, width, base="", hl=None):
        """`hl` = (c0, c1): cells drawn reversed (mouse selection)."""
        out, w = [], 0
        for t, st in self.segs:
            if w >= width:
                break
            buf, inv = [], False
            for c in t:
                cwid = cw(c)
                if w + cwid > width:
                    break
                if hl:
                    on = hl[0] <= w < hl[1]
                    if on != inv:
                        buf.append(CSI + ("7m" if on else "27m"))
                        inv = on
                buf.append(c)
                w += cwid
            if inv:
                buf.append(CSI + "27m")
            out.append(base + st + "".join(buf) + RESET)
        if w < width and hl and hl[1] > w:  # selection runs past the text: show it to the end of the selected range
            n = min(width, hl[1]) - max(w, hl[0])
            if n > 0:
                out.append(base + " " * max(0, hl[0] - w) + CSI + "7m" + " " * n + RESET)
                w = max(w, hl[0]) + n
        # erase the rest instead of writing spaces: the background still fills the row, but native terminal selection
        # copies no trailing blanks. Never at the last column (EL there would wipe the character just written).
        return "".join(out) + (base + CSI + "K" if w < width else "") + RESET


def wrap_segs(segs, width, indent=0, first_prefix=None, prefix=None, keep=0):
    """Word-wrap styled segments into Lines. Prefix cells count as decoration for copying, except the first `keep` cells
    after the first line's indent (e.g. a list bullet)."""
    lines, cur, cw_ = [], Line(), 0
    if first_prefix:
        for t, s in first_prefix:
            cur.add(t, s)
        cw_ = cur.width()
    else:
        cur.add(" " * indent)
        cw_ = indent
    cur.pre = max(0, cw_ - keep)

    def newline(joiner=""):
        nonlocal cur, cw_
        lines.append(cur)
        cur = Line()
        if prefix:
            for t, s in prefix:
                cur.add(t, s)
            cw_ = cur.width()
        else:
            cur.add(" " * indent)
            cw_ = indent
        cur.pre, cur.soft = cw_, joiner

    for text, st in segs:
        for tok in re.findall(r"\S+|\s+", text):
            tw = swidth(tok)
            if tok.isspace():
                if cw_ + tw <= width:
                    cur.add(tok, st)
                    cw_ += tw
                else:
                    newline(tok)  # the wrap swallowed this whitespace: copying puts it back
                continue
            if cw_ + tw > width and cw_ > (indent or 0) + (swidth("".join(t for t, _ in (prefix or []))) if prefix else 0):
                newline()
            while swidth(tok) > width - cw_:
                room = max(1, width - cw_)
                part, pw = "", 0
                for c in tok:
                    if pw + cw(c) > room:
                        break
                    part += c
                    pw += cw(c)
                cur.add(part, st)
                tok = tok[len(part):]
                newline()
            cur.add(tok, st)
            cw_ += swidth(tok)
    lines.append(cur)
    return lines


def selection_text(lines, a, b):
    """Plain text between two (line index, cell) points, inclusive: decoration stripped, soft wraps joined."""
    (i0, c0), (i1, c1) = sorted([a, b])
    parts = []
    for i in range(max(0, i0), min(len(lines) - 1, i1) + 1):
        L = lines[i]
        t = L.copy_text(c0 if i == i0 else 0, c1 + 1 if i == i1 else 1 << 30)
        if i > i0:
            if L.soft is not None:
                parts.append(L.soft)
            else:
                parts[-1] = parts[-1].rstrip(" ")
                parts.append("\n")
        parts.append(t)
    return "".join(parts).rstrip()


_WORD_CH = re.compile(r"[\w./\\:~@#%+=-]")


def word_at(line, col):
    """Cells (first, last) of the word under `col` (paths and dashed names count as one word)."""
    cells, w = [], 0
    for t, _ in line.segs:
        for c in t:
            cells.append((w, c))
            w += cw(c)
    i = next((k for k, (p, c) in enumerate(cells) if p <= col < p + max(1, cw(c))), None)
    if i is None or not _WORD_CH.match(cells[i][1]):
        return col, col
    i0, i1 = i, i
    while i0 > 0 and _WORD_CH.match(cells[i0 - 1][1]):
        i0 -= 1
    while i1 + 1 < len(cells) and _WORD_CH.match(cells[i1 + 1][1]):
        i1 += 1
    return cells[i0][0], cells[i1][0] + max(1, cw(cells[i1][1])) - 1


# ─── Windows console input ───
# Raw INPUT_RECORDs (no VT input mode), translated here the way crossterm does it: key events by virtual-key code,
# characters from UnicodeChar, mouse from MOUSE_EVENT records. Nothing is split into escape sequences, so no key can be
# misread as Esc + garbage, and Windows Terminal (via ConPTY) delivers the same records as the classic console.

WIN_VK = {0x26: "up", 0x28: "down", 0x25: "left", 0x27: "right", 0x24: "home", 0x23: "end", 0x21: "pgup", 0x22: "pgdn", 0x2E: "delete"}


def win_keys(recs, st):
    """recs: ("k", down, repeat, vk, char_code, ctrl_state) | ("m", col, row, buttons, ctrl_state, flags), coords 1-based.
    st: dict kept across calls (surrogate half, mouse buttons). Returns keys in the parse_keys format."""
    out, text = [], []

    def flush():
        if text:
            keys, _ = parse_keys("".join(text), final=True)
            out.extend(keys)
            text.clear()

    for r in recs:
        if r[0] == "m":
            _, x, y, buttons, state, flags = r
            if flags & 0x4:  # MOUSE_WHEELED: high word = signed delta
                flush()
                out.append("wheel-up" if (buttons >> 16) & 0xFFFF < 0x8000 else "wheel-down")
            elif flags & 0x1:  # MOUSE_MOVED
                if buttons & 1:
                    flush()
                    out.append(("mouse", "drag", 0, x, y))
            elif not flags & 0x8:  # press / release / double click (HWHEELED ignored)
                prev = 0 if flags & 0x2 else st.get("btn", 0)
                for bit, b in ((1, 0), (4, 1), (2, 2)):  # left, middle, right
                    if (buttons ^ prev) & bit:
                        flush()
                        out.append(("mouse", "press" if buttons & bit else "release", b, x, y))
            st["btn"] = buttons
            continue
        _, down, rep, vk, ch, state = r
        ctrl, alt, shift = state & 0x0C, state & 0x03, state & 0x10
        if not down:
            if vk == 0x12 and ch:  # Alt+numpad: the composed character comes with the Alt release
                text.append(chr(ch))
            continue
        n = max(1, rep)
        if 0xD800 <= ch <= 0xDBFF:  # first half of an emoji etc.; the second half is the next record
            st["hi"] = ch
            continue
        if 0xDC00 <= ch <= 0xDFFF:
            hi = st.pop("hi", None)
            if hi:
                text.append(chr(0x10000 + ((hi - 0xD800) << 10) + (ch - 0xDC00)) * n)
            continue
        name = None
        if vk in WIN_VK:
            name = WIN_VK[vk]
            if name in ("left", "right") and (ctrl or alt):
                name = "ctrl-" + name
            elif name in ("up", "down") and ctrl:
                name = "ctrl-" + name
        elif vk == 0x0D:
            name = "newline" if shift or ctrl or alt else "enter"
        elif vk == 0x1B:
            name = "esc"
        elif vk == 0x09:
            name = "shift-tab" if shift else "tab"
        elif vk == 0x08:
            name = "ctrl-w" if ctrl or alt else "backspace"
        elif vk == 0x2D and shift:  # Shift+Insert: paste
            name = "ctrl-v"
        elif not ch:
            continue  # modifiers alone, F-keys, …
        if name:
            flush()
            out.extend([name] * n)
            continue
        c = chr(ch)
        if alt and not ctrl and c.lower() in ("b", "f", "v", "c"):  # (AltGr = right alt + left ctrl still types its character)
            flush()
            out.extend([{"b": "ctrl-left", "f": "ctrl-right", "v": "alt-v", "c": "alt-c"}[c.lower()]] * n)
        elif ch < 32 or ch == 127:
            flush()
            out.extend([{10: "newline", 13: "enter", 8: "backspace", 9: "tab", 27: "esc", 127: "ctrl-w"}.get(ch) or "ctrl-" + chr(ch + 96)] * n)
        else:
            text.append(c * n)
    flush()
    return out


def win_paste_burst(keys):
    """Pasting into a console arrives as typed keys, one Enter per line. An Enter followed by more input in the same burst
    (no human types that fast) is a paste: fold the burst into one paste so it is not sent line by line."""
    first = next((i for i, k in enumerate(keys) if k == "enter"), -1)
    if first < 0 or first == len(keys) - 1 or not any(isinstance(k, tuple) and k[0] == "char" for k in keys):
        return keys
    simple = {"enter": "\n", "newline": "\n", "tab": "\t"}
    if not all((isinstance(k, tuple) and k[0] in ("char", "paste")) or k in simple for k in keys):
        # mixed with other keys: at least never send on a pasted line break
        return ["newline" if k == "enter" and any(isinstance(x, tuple) and x[0] == "char" for x in keys[i + 1:]) else k
                for i, k in enumerate(keys)]
    return [("paste", "".join(k[1] if isinstance(k, tuple) else simple[k] for k in keys))]


class Term:
    def __init__(self):
        self.q = queue.Queue()
        self.old = None
        self.mouse = True
        self._wst = {}

    def size(self):
        s = shutil.get_terminal_size((100, 30))
        # Windows console: never touch the last column, writing there can wrap/scroll the whole screen
        return max(20, s.columns - (1 if WIN else 0)), max(8, s.lines)

    def start(self, mouse=True):
        self.mouse = mouse
        if WIN:
            import ctypes
            k = ctypes.windll.kernel32
            h_out, h_in = k.GetStdHandle(-11), k.GetStdHandle(-10)
            mode = ctypes.c_uint32()
            k.GetConsoleMode(h_out, ctypes.byref(mode))
            self.old_out, self.old_cp = mode.value, k.GetConsoleOutputCP()
            k.GetConsoleMode(h_in, ctypes.byref(mode))
            self.old = mode.value
            k.SetConsoleOutputCP(65001)
            self.reassert()
        else:
            import termios, tty
            self.fd = sys.stdin.fileno()
            self.old = termios.tcgetattr(self.fd)
            tty.setraw(self.fd)
        # ?1007l: no "alternate scroll" - otherwise the wheel is sent as up/down arrows and pages through input history
        self.write(f"{CSI}?1049h{CSI}?1007l{CSI}?25l{CSI}?2004h" + (self._mouse_seq(True) if mouse else ""))
        threading.Thread(target=self._reader, daemon=True).start()

    @staticmethod
    def _mouse_seq(on):
        # press/release + drag + SGR coordinates. Windows gets mouse records from ENABLE_MOUSE_INPUT instead.
        if WIN:
            return ""
        return f"{CSI}?1000h{CSI}?1002h{CSI}?1006h" if on else f"{CSI}?1006l{CSI}?1002l{CSI}?1000l"

    def reassert(self):
        """(Re)apply our console modes. A program run by a tool may have changed them (no VT colors → raw escape codes on
        screen, line input → keys echoed); called at start and after every tool."""
        if not WIN or self.old is None:
            return
        import ctypes
        k = ctypes.windll.kernel32
        k.SetConsoleMode(k.GetStdHandle(-11), self.old_out | 0x0001 | 0x0004)  # PROCESSED_OUTPUT | VIRTUAL_TERMINAL_PROCESSING
        # raw keys: no line input / echo / Ctrl+C processing / VT input; EXTENDED_FLAGS so the QuickEdit bit is honored.
        flags = (self.old & ~(0x0001 | 0x0002 | 0x0004 | 0x0008 | 0x0200)) | 0x0080
        if self.mouse:
            flags = (flags | 0x0010) & ~0x0040  # mouse records; QuickEdit off (it would swallow the mouse)
        else:
            flags &= ~0x0010
        k.SetConsoleMode(k.GetStdHandle(-10), flags)

    def set_mouse(self, on):
        self.mouse = on
        self.reassert()
        self.write(self._mouse_seq(on))

    def stop(self):
        self.write(self._mouse_seq(False) + f"{CSI}?2004l{CSI}?25h{CSI}?1007h{CSI}?1049l")
        if WIN:
            import ctypes
            k = ctypes.windll.kernel32
            if self.old is not None:
                k.SetConsoleMode(k.GetStdHandle(-10), self.old)
            if getattr(self, "old_out", None) is not None:
                k.SetConsoleMode(k.GetStdHandle(-11), self.old_out)
                k.SetConsoleOutputCP(self.old_cp)
        elif self.old:
            import termios
            termios.tcsetattr(self.fd, termios.TCSADRAIN, self.old)

    def write(self, s):
        sys.stdout.write(s)
        sys.stdout.flush()

    def _win_api(self):
        if hasattr(self, "_wapi"):
            return self._wapi
        import ctypes
        c = ctypes

        class KEY(c.Structure):  # fixed-width fields: identical layout on every platform (testable off Windows)
            _fields_ = [("down", c.c_int32), ("repeat", c.c_uint16), ("vk", c.c_uint16), ("scan", c.c_uint16), ("ch", c.c_uint16),
                        ("state", c.c_uint32)]

        class MOUSE(c.Structure):
            _fields_ = [("x", c.c_int16), ("y", c.c_int16), ("buttons", c.c_uint32), ("state", c.c_uint32), ("flags", c.c_uint32)]

        class U(c.Union):
            _fields_ = [("key", KEY), ("mouse", MOUSE), ("pad", c.c_byte * 16)]

        class REC(c.Structure):
            _fields_ = [("type", c.c_uint16), ("u", U)]

        class CSBI(c.Structure):
            _fields_ = [("size", c.c_int16 * 2), ("cur", c.c_int16 * 2), ("attr", c.c_uint16), ("win", c.c_int16 * 4), ("max", c.c_int16 * 2)]

        k = c.windll.kernel32
        self._wapi = (c, k, k.GetStdHandle(-10), REC, CSBI)
        return self._wapi

    def _read_win(self):
        """Block for console input; return translated keys. Everything already queued is read in the same go, so a paste
        arrives as one burst."""
        c, k, h, REC, CSBI = self._win_api()
        recs, n, pend = (REC * 256)(), c.c_uint32(), c.c_uint32()
        out, mouse = [], False
        while True:
            if not k.ReadConsoleInputW(h, recs, 256, c.byref(n)):
                raise OSError("ReadConsoleInputW failed")
            for i in range(n.value):  # copy out now: the next read reuses the buffer
                r = recs[i]
                if r.type == 1:
                    e = r.u.key
                    out.append(("k", e.down, e.repeat, e.vk, e.ch, e.state))
                elif r.type == 2 and self.mouse:
                    e = r.u.mouse
                    out.append(["m", e.x, e.y, e.buttons, e.state, e.flags])
                    mouse = True
            if out and out[-1][0] == "k" and out[-1][3] == 0x0D and len(out) < 20000:
                k.WaitForSingleObject(h, 15)  # an Enter at the end may be a pasted line break: is more coming right away?
            if not k.GetNumberOfConsoleInputEvents(h, c.byref(pend)) or not pend.value or len(out) >= 20000:
                break
        if mouse:
            info, left, top = CSBI(), 0, 0
            if k.GetConsoleScreenBufferInfo(k.GetStdHandle(-11), c.byref(info)):
                left, top = info.win[0], info.win[1]  # mouse coordinates are in buffer cells, not window cells
            for r in out:
                if r[0] == "m":
                    r[1], r[2] = r[1] - left + 1, r[2] - top + 1
        return win_paste_burst(win_keys(out, self._wst))

    def _read_win_classic(self):
        """Fallback (ME_WIN_INPUT=classic): msvcrt, keys only."""
        import msvcrt
        c = msvcrt.getwch()
        if c in ("\x00", "\xe0"):
            c2 = msvcrt.getwch()
            k = {"H": "up", "P": "down", "K": "left", "M": "right", "G": "home", "O": "end", "S": "delete", "I": "pgup", "Q": "pgdn",
                 "s": "ctrl-left", "t": "ctrl-right", "\x8d": "ctrl-up", "\x91": "ctrl-down"}.get(c2)
            return [k] if k else []
        return parse_keys(c, final=True)[0]

    def _reader(self):
        if WIN:
            classic = os.environ.get("ME_WIN_INPUT") == "classic"
            while True:
                try:
                    keys = self._read_win_classic() if classic else self._read_win()
                except Exception:
                    return
                for k in keys:
                    self.q.put(k)
        import codecs, select
        dec = codecs.getincrementaldecoder("utf-8")("replace")  # a multi-byte letter split between two reads stays whole
        buf = ""
        while True:
            try:
                buf += dec.decode(os.read(self.fd, 4096))
                while select.select([self.fd], [], [], 0.01)[0]:
                    buf += dec.decode(os.read(self.fd, 4096))
                keys, buf = parse_keys(buf)
                # an escape sequence cut in the middle: give the rest a moment, then take what we have (a lone Esc)
                if buf and not buf.startswith("\x1b[200~") and not select.select([self.fd], [], [], 0.05)[0]:
                    more, buf = parse_keys(buf, final=True)
                    keys += more
            except Exception:
                return
            for k in win_paste_burst(keys):
                self.q.put(k)


KEYMAP = {"[A": "up", "[B": "down", "[C": "right", "[D": "left", "[H": "home", "[F": "end", "[1~": "home", "[4~": "end",
          "[3~": "delete", "[5~": "pgup", "[6~": "pgdn", "OA": "up", "OB": "down", "OC": "right", "OD": "left", "OH": "home",
          "OF": "end", "[1;5A": "ctrl-up", "[1;5B": "ctrl-down", "[1;5C": "ctrl-right", "[1;5D": "ctrl-left", "[1;3C": "ctrl-right",
          "[1;3D": "ctrl-left", "[Z": "shift-tab", "[13;2u": "newline", "[13;5u": "newline", "[13;3u": "newline", "[27;2;13~": "newline",
          "[27;5;13~": "newline", "[3;5~": "ctrl-delete"}
_ESC_PREFIX = re.compile(r"\x1b(\[[<?\d;]*|O)?$")  # what an unfinished CSI / SS3 sequence looks like


def parse_keys(buf, final=False):
    """Bytes from the terminal → keys. Returns (keys, rest): `rest` is an unfinished escape sequence or paste, kept for the
    next read unless `final`."""
    keys, i = [], 0
    while i < len(buf):
        c = buf[i]
        if buf.startswith("\x1b[200~", i):
            end = buf.find("\x1b[201~", i)
            if end < 0:
                if not final:
                    return keys, buf[i:]
                keys.append(("paste", buf[i + 6:]))
                return keys, ""
            keys.append(("paste", buf[i + 6:end]))
            i = end + 6
            continue
        if c == "\x1b":
            if not final and _ESC_PREFIX.match(buf, i):
                return keys, buf[i:]
            if i + 1 >= len(buf):
                keys.append("esc")
                i += 1
                continue
            m = re.match(r"\x1b\[<(\d+);(\d+);(\d+)([mM])", buf[i:])
            if m:
                b, x, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
                if b & 64:  # wheel, with or without shift/ctrl/alt
                    if not b & 2:
                        keys.append("wheel-down" if b & 1 else "wheel-up")
                elif b & 3 != 3:
                    keys.append(("mouse", "drag" if b & 32 else "release" if m.group(4) == "m" else "press", b & 3, x, y))
                i += m.end()
                continue
            m = re.match(r"\x1b(\[[\d;?]*[~A-Za-z]|O[A-Z])", buf[i:])
            if m:
                keys.append(KEYMAP.get(m.group(1), "unknown"))
                i += m.end()
                continue
            nxt = buf[i + 1]
            if nxt in "\r\n":
                keys.append("newline")
            elif nxt in "\x7f\x08":
                keys.append("ctrl-w")
            elif nxt == "b":
                keys.append("ctrl-left")
            elif nxt == "f":
                keys.append("ctrl-right")
            elif nxt in "vV":
                keys.append("alt-v")
            elif nxt in "cC":
                keys.append("alt-c")
            else:
                keys.append("esc")
                i += 1
                continue
            i += 2
            continue
        if c in "\r":
            keys.append("enter")
        elif c == "\n":
            keys.append("newline")
        elif c in "\x7f\x08":
            keys.append("backspace")
        elif c == "\t":
            keys.append("tab")
        elif ord(c) < 32:
            keys.append("ctrl-" + chr(ord(c) + 96))
        else:
            keys.append(("char", c))
        i += 1
    return keys, ""
