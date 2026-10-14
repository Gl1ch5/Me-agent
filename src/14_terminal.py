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
    """A display line: list of (text, style) segments."""
    __slots__ = ("segs",)

    def __init__(self, segs=None):
        self.segs = segs or []

    def add(self, text, style=""):
        if text:
            self.segs.append((text, style))
        return self

    def width(self):
        return sum(swidth(t) for t, _ in self.segs)

    def plain(self, width):
        """The text this line shows in `width` cells (what a selection copies)."""
        out, w = [], 0
        for t, _ in self.segs:
            for c in t:
                if w + cw(c) > width:
                    return "".join(out)
                out.append(c)
                w += cw(c)
        return "".join(out)

    def render(self, width, base=""):
        out, w = [], 0
        for t, st in self.segs:
            if w >= width:
                break
            buf = []
            for c in t:
                cwid = cw(c)
                if w + cwid > width:
                    break
                buf.append(c)
                w += cwid
            out.append(base + st + "".join(buf) + RESET)
        return "".join(out) + base + " " * max(0, width - w) + RESET


def wrap_segs(segs, width, indent=0, first_prefix=None, prefix=None):
    """Word-wrap styled segments into Lines."""
    lines, cur, cw_ = [], Line(), 0
    if first_prefix:
        for t, s in first_prefix:
            cur.add(t, s)
        cw_ = cur.width()
    else:
        cur.add(" " * indent)
        cw_ = indent

    def newline():
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

    for text, st in segs:
        for tok in re.findall(r"\S+|\s+", text):
            tw = swidth(tok)
            if tok.isspace():
                if cw_ + tw <= width:
                    cur.add(tok, st)
                    cw_ += tw
                else:
                    newline()
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


class Term:
    def __init__(self):
        self.q = queue.Queue()
        self.old = None
        self.mouse = True

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
            k.SetConsoleMode(h_out, mode.value | 0x0004)
            k.GetConsoleMode(h_in, ctypes.byref(mode))
            self.old = mode.value
            self._win_in_mode(mouse)
            k.SetConsoleOutputCP(65001)
        else:
            import termios, tty
            self.fd = sys.stdin.fileno()
            self.old = termios.tcgetattr(self.fd)
            tty.setraw(self.fd)
        # ?1007l: no "alternate scroll" - otherwise the wheel is sent as up/down arrows and pages through input history
        self.write(f"{CSI}?1049h{CSI}?1007l{CSI}?25l{CSI}?2004h" + (MOUSE_ON if mouse else ""))
        threading.Thread(target=self._reader, daemon=True).start()

    def _win_in_mode(self, mouse):
        """VT input for keys; with mouse on also ENABLE_MOUSE_INPUT + QuickEdit off, so conhost reports the wheel.
        Input is read with ReadConsoleInputW (see _readch_win), so mouse records never reach the key stream."""
        import ctypes
        flags = (self.old | 0x0200) & ~(0x0001 | 0x0002 | 0x0004)
        if mouse:
            flags = (flags | 0x0010 | 0x0080) & ~0x0040
        ctypes.windll.kernel32.SetConsoleMode(ctypes.windll.kernel32.GetStdHandle(-10), flags)

    def set_mouse(self, on):
        self.mouse = on
        if WIN and self.old is not None:
            self._win_in_mode(on)
        self.write(MOUSE_ON if on else MOUSE_OFF)

    def stop(self):
        self.write(f"{MOUSE_OFF}{CSI}?2004l{CSI}?25h{CSI}?1007h{CSI}?1049l")
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

    def _readch_win(self):
        """Block for console input records; return key text (VT sequences included) plus synthesized wheel sequences."""
        import ctypes
        from ctypes import wintypes as w

        class KEY(ctypes.Structure):
            _fields_ = [("down", w.BOOL), ("repeat", w.WORD), ("vk", w.WORD), ("scan", w.WORD), ("ch", w.WCHAR), ("state", w.DWORD)]

        class MOUSE(ctypes.Structure):
            _fields_ = [("x", w.SHORT), ("y", w.SHORT), ("buttons", w.DWORD), ("state", w.DWORD), ("flags", w.DWORD)]

        class U(ctypes.Union):
            _fields_ = [("key", KEY), ("mouse", MOUSE), ("pad", ctypes.c_byte * 16)]

        class REC(ctypes.Structure):
            _fields_ = [("type", w.WORD), ("u", U)]

        k = ctypes.windll.kernel32
        h = k.GetStdHandle(-10)
        recs, n = (REC * 64)(), w.DWORD()
        if not k.ReadConsoleInputW(h, recs, 64, ctypes.byref(n)):
            raise OSError("ReadConsoleInputW failed")
        out, top = [], None
        for r in recs[: n.value]:
            if r.type == 1 and r.u.key.down and r.u.key.ch != "\x00":
                ch = r.u.key.ch
                if r.u.key.state & 0x0003 and not r.u.key.state & 0x000C and ch.isprintable() and not (out and out[-1].endswith("\x1b")):
                    # left/right Alt (not AltGr = Ctrl+Alt); conhost in VT mode may already have sent the ESC itself
                    ch = "\x1b" + ch
                out.append(ch * max(1, r.u.key.repeat))
            elif r.type == 2 and self.mouse:
                m = r.u.mouse
                if m.flags & 0x0004:  # MOUSE_WHEELED; high word = signed delta
                    delta = ctypes.c_short(m.buttons >> 16).value
                    out.append("\x1b[<64;1;1M" if delta > 0 else "\x1b[<65;1;1M")
                    continue
                if top is None:  # record coordinates are buffer-relative: make them window-relative
                    top = self._win_top()
                x, y = m.x + 1, m.y - top + 1
                prev = getattr(self, "_mbtn", 0)
                btn = m.buttons & 0x3
                if m.flags & 0x0001:  # MOUSE_MOVED
                    if btn & 1:
                        out.append(f"\x1b[<32;{x};{y}M")
                elif btn & 1 and not prev & 1:
                    out.append(f"\x1b[<0;{x};{y}M")
                elif prev & 1 and not btn & 1:
                    out.append(f"\x1b[<0;{x};{y}m")
                elif btn & 2 and not prev & 2:
                    out.append(f"\x1b[<2;{x};{y}M")
                self._mbtn = btn
        # join surrogate halves (emoji etc.) that arrive as separate records, also across reads
        s = getattr(self, "_hi_sur", "") + "".join(out)
        self._hi_sur = s[-1] if s and "\ud800" <= s[-1] <= "\udbff" else ""
        if self._hi_sur:
            s = s[:-1]
        return s.encode("utf-16-le", "surrogatepass").decode("utf-16-le", "replace")

    @staticmethod
    def _win_top():
        import ctypes
        from ctypes import wintypes as w

        class CSBI(ctypes.Structure):
            _fields_ = [("size", w._COORD), ("cur", w._COORD), ("attr", w.WORD), ("win", w.SMALL_RECT), ("max", w._COORD)]
        info = CSBI()
        k = ctypes.windll.kernel32
        return info.win.Top if k.GetConsoleScreenBufferInfo(k.GetStdHandle(-11), ctypes.byref(info)) else 0

    def _readch(self):
        if WIN:
            if self.mouse:
                return self._readch_win()
            # mouse off: the classic reader (no wheel), a safe fallback if raw records ever misbehave on some console
            import msvcrt
            c = msvcrt.getwch()
            if c in ("\x00", "\xe0"):
                c2 = msvcrt.getwch()
                return {"H": "\x1b[A", "P": "\x1b[B", "K": "\x1b[D", "M": "\x1b[C", "G": "\x1b[H", "O": "\x1b[F", "S": "\x1b[3~",
                        "I": "\x1b[5~", "Q": "\x1b[6~"}.get(c2, "")
            return c
        b = os.read(self.fd, 4096)
        return b.decode("utf-8", "replace")

    def _reader(self):
        buf = ""
        while True:
            try:
                buf += self._readch()
            except Exception:
                return
            if not WIN:
                import select
                while select.select([self.fd], [], [], 0.01)[0]:
                    buf += os.read(self.fd, 4096).decode("utf-8", "replace")
            keys, buf = parse_keys(buf)
            for k in paste_guard(keys):
                self.q.put(k)


KEYMAP = {"[A": "up", "[B": "down", "[C": "right", "[D": "left", "[H": "home", "[F": "end", "[1~": "home", "[4~": "end",
          "[3~": "delete", "[5~": "pgup", "[6~": "pgdn", "OA": "up", "OB": "down", "OC": "right", "OD": "left", "OH": "home",
          "OF": "end", "[1;5A": "ctrl-up", "[1;5B": "ctrl-down", "[1;5C": "ctrl-right", "[1;5D": "ctrl-left", "[1;3C": "ctrl-right",
          "[1;3D": "ctrl-left", "[Z": "shift-tab", "[13;2u": "newline", "[27;2;13~": "newline"}


MOUSE_ON = f"{CSI}?1000h{CSI}?1002h{CSI}?1006h"  # clicks + drags (for selection) + wheel, SGR coordinates
MOUSE_OFF = f"{CSI}?1002l{CSI}?1000l{CSI}?1006l"


def paste_guard(keys):
    """Terminals without bracketed paste (classic cmd.exe) deliver a pasted block as typed keys: an Enter followed by more text in
    the same read is a pasted line break, not "send" — otherwise each pasted line went out as its own message."""
    out = list(keys)
    for i, k in enumerate(out):
        if k == "enter" and any(isinstance(x, tuple) and x[0] == "char" for x in out[i + 1:]):
            out[i] = "newline"
    return out


def parse_keys(buf):
    keys, i = [], 0
    while i < len(buf):
        c = buf[i]
        if buf.startswith("\x1b[200~", i):
            end = buf.find("\x1b[201~", i)
            if end < 0:
                return keys, buf[i:]
            keys.append(("paste", buf[i + 6:end]))
            i = end + 6
            continue
        if c == "\x1b":
            if i + 1 >= len(buf):
                keys.append("esc")
                i += 1
                continue
            m = re.match(r"\x1b\[<(\d+);(\d+);(\d+)([mM])", buf[i:])
            if m:
                b, x, y, down = int(m.group(1)), int(m.group(2)), int(m.group(3)), m.group(4) == "M"
                if b & 64 and not b & 32:  # wheel, with or without shift/ctrl/alt
                    keys.append("wheel-up" if b & 3 == 0 else "wheel-down" if b & 3 == 1 else "unknown")
                elif b & 32:  # motion with a button held: drag
                    keys.append(("mouse", "drag", x, y))
                elif b & 3 == 0:
                    keys.append(("mouse", "down" if down else "up", x, y))
                elif b & 3 == 2 and down:
                    keys.append(("mouse", "right", x, y))
                i += m.end()
                continue
            if i + 1 < len(buf) and buf[i + 1] in "vV" and not buf.startswith("\x1b[", i):
                keys.append("alt-v")
                i += 2
                continue
            if i + 1 < len(buf) and buf[i + 1] in "cC" and not buf.startswith("\x1b[", i):
                keys.append("alt-c")
                i += 2
                continue
            m = re.match(r"\x1b(\[[\d;]*[~A-Za-z]|O[A-Z])", buf[i:])
            if m:
                keys.append(KEYMAP.get(m.group(1), "unknown"))
                i += m.end()
                continue
            nxt = buf[i + 1]
            if nxt in "\r\n":
                keys.append("newline")
            elif nxt == "\x7f":
                keys.append("ctrl-w")
            elif nxt == "b":
                keys.append("ctrl-left")
            elif nxt == "f":
                keys.append("ctrl-right")
            elif nxt == "\x1b":
                keys.append("esc")
                i += 1
                continue
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
