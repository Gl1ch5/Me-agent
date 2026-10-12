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
            k.SetConsoleMode(h_out, mode.value | 0x0004)
            k.GetConsoleMode(h_in, ctypes.byref(mode))
            self.old = mode.value
            # VT input for keys; with mouse on, also ENABLE_MOUSE_INPUT so the wheel is reported, and QuickEdit off
            # (QuickEdit selection in conhost swallows the wheel)
            # 0x0008 ENABLE_WINDOW_INPUT: report buffer resizes (minimize/restore) so the screen gets repainted
            flags = (mode.value | 0x0200 | 0x0008) & ~(0x0001 | 0x0002 | 0x0004)
            if mouse:
                flags = (flags | 0x0010 | 0x0080) & ~0x0040
            k.SetConsoleMode(h_in, flags)
            k.SetConsoleOutputCP(65001)
        else:
            import termios, tty
            self.fd = sys.stdin.fileno()
            self.old = termios.tcgetattr(self.fd)
            tty.setraw(self.fd)
        # ?1007l: no "alternate scroll" - otherwise the wheel is sent as up/down arrows and pages through input history
        self.write(f"{CSI}?1049h{CSI}?1007l{CSI}?25l{CSI}?2004h" + (f"{CSI}?1000h{CSI}?1006h" if mouse else ""))
        threading.Thread(target=self._reader, daemon=True).start()

    def set_mouse(self, on):
        self.mouse = on
        self.write(f"{CSI}?1000h{CSI}?1006h" if on else f"{CSI}?1000l{CSI}?1006l")

    def stop(self):
        self.write(f"{CSI}?1000l{CSI}?1006l{CSI}?2004l{CSI}?25h{CSI}?1007h{CSI}?1049l")
        if WIN:
            import ctypes
            k = ctypes.windll.kernel32
            if self.old is not None:
                k.SetConsoleMode(k.GetStdHandle(-10), self.old)
        elif self.old:
            import termios
            termios.tcsetattr(self.fd, termios.TCSADRAIN, self.old)

    def write(self, s):
        sys.stdout.write(s)
        sys.stdout.flush()

    def _win_wheel(self):
        """Windows console: peek raw input records so mouse-wheel events (which msvcrt.getwch() silently drops) reach the app.
        Returns an SGR wheel sequence for a wheel record, or None when the next record is a key press for getwch()."""
        import ctypes
        from ctypes import wintypes
        if not hasattr(self, "_wrec"):
            class KEY(ctypes.Structure):
                _fields_ = [("down", wintypes.BOOL), ("rep", wintypes.WORD), ("vk", wintypes.WORD), ("sc", wintypes.WORD),
                            ("ch", wintypes.WCHAR), ("ctl", wintypes.DWORD)]

            class MOUSE(ctypes.Structure):
                _fields_ = [("x", ctypes.c_short), ("y", ctypes.c_short), ("btn", wintypes.DWORD), ("ctl", wintypes.DWORD),
                            ("flags", wintypes.DWORD)]

            class EV(ctypes.Union):
                _fields_ = [("key", KEY), ("mouse", MOUSE), ("pad", ctypes.c_byte * 16)]

            class REC(ctypes.Structure):
                _fields_ = [("type", wintypes.WORD), ("ev", EV)]

            self._wrec = REC
            self._wk = ctypes.windll.kernel32
            self._wh = self._wk.GetStdHandle(-10)
        k, h, REC = self._wk, self._wh, self._wrec
        rec, n = REC(), wintypes.DWORD()
        while True:
            k.WaitForSingleObject(h, 0xFFFFFFFF)
            if not k.PeekConsoleInputW(h, ctypes.byref(rec), 1, ctypes.byref(n)) or not n.value:
                continue
            if rec.type == 1 and rec.ev.key.down and (rec.ev.key.ch != "\x00" or rec.ev.key.vk not in (0x10, 0x11, 0x12, 0x14, 0x5B, 0x5C, 0x90, 0x91)):
                return None  # a real key press: let getwch() translate it
            k.ReadConsoleInputW(h, ctypes.byref(rec), 1, ctypes.byref(n))
            if rec.type in (4, 16):  # window buffer size / focus: repaint
                return "\x1b[999~"
            if rec.type == 2 and rec.ev.mouse.flags & 0x4:  # MOUSE_WHEELED
                delta = ctypes.c_short(rec.ev.mouse.btn >> 16).value
                return f"\x1b[<{64 if delta > 0 else 65};1;1M"

    def _readch(self):
        if WIN:
            import msvcrt
            try:
                w = self._win_wheel()
            except Exception:
                w = None
            if w:
                return w
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
            for k in keys:
                self.q.put(k)


KEYMAP = {"[A": "up", "[B": "down", "[C": "right", "[D": "left", "[H": "home", "[F": "end", "[1~": "home", "[4~": "end",
          "[3~": "delete", "[5~": "pgup", "[6~": "pgdn", "OA": "up", "OB": "down", "OC": "right", "OD": "left", "OH": "home",
          "OF": "end", "[1;5A": "ctrl-up", "[1;5B": "ctrl-down", "[1;5C": "ctrl-right", "[1;5D": "ctrl-left", "[1;3C": "ctrl-right",
          "[1;3D": "ctrl-left", "[Z": "shift-tab", "[999~": "redraw", "[13;2u": "newline", "[27;2;13~": "newline"}


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
                b = int(m.group(1))
                if b & 64 and not b & 32:  # wheel, with or without shift/ctrl/alt
                    keys.append("wheel-up" if b & 3 == 0 else "wheel-down" if b & 3 == 1 else "unknown")
                i += m.end()
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
