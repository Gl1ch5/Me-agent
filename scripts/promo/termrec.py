"""Terminal recorder: runs a real command in a pty, feeds the bytes to pyte, renders every changed screen to PNG,
and writes the raw byte stream as an asciinema v2 .cast. Frames are later concatenated into video by compose.py."""
import codecs, json, os, pty, select, struct, fcntl, termios, time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import pyte
from PIL import Image, ImageDraw, ImageFont

FONT_DIR = "/usr/share/fonts/truetype/dejavu/"
ANSI = {"black": "#16181f", "red": "#ff6b7a", "green": "#5ad18a", "brown": "#ffb454", "yellow": "#ffb454", "blue": "#8b7cff",
        "magenta": "#c792ea", "cyan": "#4fd1c5", "white": "#d8dae3"}
KEYS = {"down": "\x1b[B", "up": "\x1b[A", "enter": "\r", "esc": "\x1b", "backspace": "\x7f", "tab": "\t"}
ME_DARK = {"bg": "#0e0f13", "bg2": "#111319", "line": "#1f2230", "fg": "#d8dae3", "dim": "#6b6f80", "accent": "#8b7cff", "sel": "#262a38"}


RENDER = ThreadPoolExecutor(max_workers=max(2, (os.cpu_count() or 2) - 1))  # frames are drawn while the scene keeps running
FAST = float(os.environ.get("PROMO_SPEED") or 1.0)  # >1 plays every pause and every keystroke faster (a quick preview)


def _color(c, default):
    if c == "default":
        return default
    if c in ANSI:
        return ANSI[c]
    if len(c) == 6 and all(ch in "0123456789abcdefABCDEF" for ch in c):
        return "#" + c
    return default


class Term:
    """One pty session. `canvas` = (W, H) of the output frame; the terminal window is centred on it."""

    def __init__(self, out, name, cmd, env, cwd, cols=100, rows=30, font_px=24, canvas=(1920, 1080), theme=None):
        self.out, self.name = Path(out), name
        self.out.mkdir(parents=True, exist_ok=True)
        self.frames_dir = self.out / f"{name}-frames"
        self.frames_dir.mkdir(exist_ok=True)
        self.cols, self.rows, self.canvas = cols, rows, canvas
        self.font = ImageFont.truetype(FONT_DIR + "DejaVuSansMono.ttf", font_px)
        self.bold = ImageFont.truetype(FONT_DIR + "DejaVuSansMono-Bold.ttf", font_px)
        self.cw = round(self.font.getlength("M"))
        self.ch = round(font_px * 1.25)
        self.theme = theme or ME_DARK
        self.win_w, self.win_h = cols * self.cw + 40, rows * self.ch + 40
        self.wx = (canvas[0] - self.win_w) // 2
        self.wy = (canvas[1] - self.win_h - 64) // 2 + 64
        self.screen = pyte.Screen(cols, rows)
        self.stream = pyte.ByteStream(self.screen)
        self.dec = codecs.getincrementaldecoder("utf-8")("replace")
        self.t0 = time.time()
        self.cast = []  # (t, text) asciinema events
        self.jobs = []  # the frame renders still running: finish() waits for them
        self.marks = {}  # text → when it first showed up on screen (scene timing for the audio)
        self.frames = []  # (t, png path) for every distinct screen
        self._last = None
        self._n = 0
        self.pid, self.fd = pty.fork()
        if self.pid == 0:
            os.chdir(cwd)
            os.execvpe(cmd[0], cmd, dict(env, TERM="xterm-256color", COLUMNS=str(cols), LINES=str(rows)))
        fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        self.pump(0.3)

    # ── driving ────────────────────────────────────────────────────────────
    def now(self):
        return time.time() - self.t0

    def pump(self, seconds):
        end = time.time() + seconds / FAST
        while True:
            left = end - time.time()
            r, _, _ = select.select([self.fd], [], [], max(0.0, min(0.02, left)))
            if r:
                try:
                    data = os.read(self.fd, 65536)
                except OSError:
                    data = b""
                if data:
                    text = self.dec.decode(data)
                    self.cast.append((round(self.now(), 3), text))
                    self.stream.feed(data)
            self._sample()
            if left <= 0:
                break

    def write(self, data):
        os.write(self.fd, data.encode() if isinstance(data, str) else data)

    def key(self, name, pause=0.4):
        self.write(KEYS[name])
        self.pump(pause)

    def type(self, text, cps=14):
        for ch in text:
            self.write(ch)
            self.pump(1 / cps)
        self.pump(0.2)

    def wait_for(self, needle, timeout=30):
        end = time.time() + timeout
        while time.time() < end:
            self.pump(0.1)
            if needle in "\n".join(self.screen.display):
                self.marks.setdefault(needle, self.now())
                return True
        raise AssertionError(f"promo terminal: '{needle}' never appeared")

    def last_seen(self, needle):
        """When `needle` first appeared (for the sound design): a wait_for that matched, or a scan of the screen now."""
        if needle in self.marks:
            return self.marks[needle]
        return self.now() if needle in "\n".join(self.screen.display) else None

    def hold(self, seconds, every=0.07, ch=" "):
        """Hold a key: repeated presses, like key auto-repeat (the voice plugin reads three quick spaces as 'start')."""
        end = time.time() + seconds / FAST
        while time.time() < end:
            self.write(ch)
            self.pump(every)

    def hold_until(self, seconds):
        self.pump(seconds)

    def finish(self, total):
        """Hold the last screen until the scene's planned length, then make sure every frame is on disk."""
        self.pump(max(0.0, total - self.now()))
        for j in self.jobs:
            j.result()
        self.jobs.clear()

    def close(self):
        for j in self.jobs:  # a scene can be closed without finish(): never leave a half-written frame behind
            j.result()
        self.jobs.clear()
        try:
            os.kill(self.pid, 9)
            os.waitpid(self.pid, 0)
        except OSError:
            pass

    # ── rendering ──────────────────────────────────────────────────────────
    def _key(self):
        s = self.screen
        rows = []
        for y in range(self.rows):
            line = s.buffer[y]
            rows.append(tuple((line[x].data, line[x].fg, line[x].bg, line[x].bold, line[x].reverse) for x in range(self.cols)))
        cur = (s.cursor.x, s.cursor.y, s.cursor.hidden)
        return tuple(rows), cur

    def _sample(self):
        key = self._key()
        if key == self._last:
            return
        self._last = key
        path = self.frames_dir / f"{self._n:05d}.png"
        self._n += 1
        img = self._render()  # the screen state is copied here; the drawing runs in the background
        self.frames.append((self.now(), path))
        self.jobs.append(RENDER.submit(img.save, path))

    def _render(self):
        t = self.theme
        W, H = self.canvas
        img = Image.new("RGB", (W, H), t["bg"])
        d = ImageDraw.Draw(img)
        # window chrome
        x0, y0 = self.wx, self.wy - 64
        d.rounded_rectangle((x0, y0, x0 + self.win_w, y0 + self.win_h + 64), radius=18, fill=t["bg2"], outline=t["line"], width=2)
        d.rectangle((x0 + 2, y0 + 62, x0 + self.win_w - 2, y0 + 64), fill=t["line"])
        for i, c in enumerate(["#ff6b7a", "#ffb454", "#5ad18a"]):
            d.ellipse((x0 + 26 + i * 30, y0 + 22, x0 + 42 + i * 30, y0 + 38), fill=c)
        d.text((x0 + 130, y0 + 18), self.name, font=self.font, fill=t["dim"])
        tx, ty = self.wx + 20, self.wy
        s = self.screen
        for y in range(self.rows):
            line = s.buffer[y]
            x = 0
            while x < self.cols:  # draw runs of cells that share colours and weight: far fewer draw calls per frame
                c = line[x]
                key = (c.fg, c.bg, c.bold, c.reverse)
                j = x + 1
                while j < self.cols and (line[j].fg, line[j].bg, line[j].bold, line[j].reverse) == key:
                    j += 1
                fg = _color(c.fg, t["fg"])
                bg = _color(c.bg, t["bg2"])
                if c.reverse:
                    fg, bg = bg, fg
                px, py = tx + x * self.cw, ty + y * self.ch
                if bg != t["bg2"]:
                    d.rectangle((px, py, px + (j - x) * self.cw, py + self.ch), fill=bg)
                font = self.bold if c.bold else self.font
                for k in range(x, j):  # one glyph per cell: no drift against the grid; blanks are skipped
                    ch = line[k].data
                    if ch and ch != " ":
                        d.text((tx + k * self.cw, py + 2), ch, font=font, fill=fg)
                x = j
        if not s.cursor.hidden:
            cx, cy = tx + s.cursor.x * self.cw, ty + s.cursor.y * self.ch
            d.rectangle((cx, cy + self.ch - 4, cx + self.cw, cy + self.ch), fill=t["accent"])
        return img

    def save_cast(self, path):
        header = {"version": 2, "width": self.cols, "height": self.rows, "timestamp": int(self.t0), "env": {"TERM": "xterm-256color"},
                  "title": "me promo: " + self.name}
        with open(path, "w") as f:
            f.write(json.dumps(header) + "\n")
            for t, text in self.cast:
                f.write(json.dumps([t, "o", text], ensure_ascii=False) + "\n")

    def concat_list(self, total):
        """ffmpeg concat-demuxer list: each distinct screen shown for its real duration, last one until `total`."""
        lines = []
        for i, (t, p) in enumerate(self.frames):
            nxt = self.frames[i + 1][0] if i + 1 < len(self.frames) else total
            lines.append(f"file '{p.resolve()}'\nduration {max(0.034, nxt - t):.3f}")
        if self.frames:
            lines.append(f"file '{self.frames[-1][1].resolve()}'")
        return "\n".join(lines) + "\n"
