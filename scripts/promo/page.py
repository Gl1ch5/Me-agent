"""Rendering for the "writing" scene: the prompt is typed by a real `me --rpc` and the reply is the model's own
word-by-word stream, drawn as a page that writes itself. Same me-dark palette and mono type as the terminal scenes.

A frame is a function of t (seconds into the scene): `frame(t)` draws it with PIL. So the scene runs as long as the reply
takes and the picture is built afterwards from the words that are already known — the prompter (`rpcdrive.py`) logs when
each of them landed. One layout for 16:9, one for 9:16.
"""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

FONT_DIR = "/usr/share/fonts/truetype/dejavu/"
TH = dict(bg="#0e0f13", bg2="#111319", bg3="#171a23", line="#1f2230", fg="#d8dae3", dim="#6b6f80", faint="#3a3d4a",
          accent="#8b7cff", accent2="#4fd1c5", ok="#5ad18a", warn="#ffb454", user="#e7c66b")
STEPS = {"ru": ["читаю проект", "пишу страницу", "проверяю результат"], "en": ["reading the project", "writing the page", "checking it"]}


def layout(portrait):
    if portrait:  # 9:16: one tall page, big type — this is the TikTok cut
        return dict(size=(1080, 1920), pad=54, font=33, small=25, win=(972, 1500), top=210, heading=40)
    return dict(size=(1920, 1080), pad=96, font=32, small=26, win=(1240, 800), top=140, heading=38)


class Frame:
    def __init__(self, cfg):
        self.c = cfg
        self.W, self.H = cfg["size"]
        px = cfg["font"]
        self.mono = ImageFont.truetype(FONT_DIR + "DejaVuSansMono.ttf", px)
        self.bold = ImageFont.truetype(FONT_DIR + "DejaVuSansMono-Bold.ttf", px)
        self.small = ImageFont.truetype(FONT_DIR + "DejaVuSansMono.ttf", cfg["small"])

    def wrap(self, text, width):
        lines, cur = [], ""
        for word in text.split(" "):
            probe = (cur + " " + word).strip()
            if self.mono.getlength(probe) <= width or not cur:
                cur = probe
            else:
                lines.append(cur)
                cur = word
        return lines + ([cur] if cur else [])

    def draw(self, t, prompt, keys, send_at, words, done_at, lang="ru", steps=None, name="writing"):
        """t seconds in; `words` = [[t, token], …]; `keys` = moments the prompt's characters landed;
        `steps`: the three things the agent is doing (defaults to the scene's own)."""
        c = self.c
        img = Image.new("RGB", (self.W, self.H), TH["bg"])
        d = ImageDraw.Draw(img)
        ww, wh = c["win"]
        x0, y0 = (self.W - ww) // 2, c["top"]
        d.rounded_rectangle((x0, y0, x0 + ww, y0 + wh), radius=24, fill=TH["bg2"], outline=TH["line"], width=2)
        for i, col in enumerate(["#ff6b7a", "#ffb454", "#5ad18a"]):
            d.ellipse((x0 + 32 + i * 36, y0 + 28, x0 + 52 + i * 36, y0 + 48), fill=col)
        d.text((x0 + ww - 32, y0 + 26), name, font=self.small, fill=TH["dim"], anchor="ra")
        left, inner = x0 + c["pad"], ww - 2 * c["pad"]
        y = y0 + 96
        # 1 ── the prompt, key by key
        shown = prompt if t >= send_at else prompt[: sum(1 for k in keys if k <= t)]
        d.text((left, y), "› ", font=self.bold, fill=TH["user"])
        for i, line in enumerate(self.wrap(shown, inner - 40)):
            d.text((left + 36, y + i * int(c["font"] * 1.45)), line, font=self.mono, fill=TH["fg"])
        y += int(c["font"] * 1.45) * max(1, len(self.wrap(shown, inner - 40))) + 26
        # 2 ── the agent's steps
        if t >= send_at:
            stage = 1 if t < done_at else 2
            row = left
            narrow = c["win"][0] < 1000  # 9:16: the three steps don't fit on one line
            for i, s in enumerate(steps or STEPS.get(lang, STEPS["en"])):
                on, done = i == stage, i < stage
                mark, col = ("●", TH["accent2"]) if on else ("✓", TH["ok"]) if done else ("○", TH["faint"])
                d.text((row, y), mark + " ", font=self.small, fill=col)
                row += self.small.getlength(mark + " ")
                d.text((row, y), s, font=self.small, fill=TH["fg"] if on else TH["dim"] if done else TH["faint"])
                row += self.small.getlength(s) + (24 if narrow else 44)
                if narrow:
                    y += int(c["font"] * 1.6)  # one step per line
                    row = left
            y += int(c["font"] * (3.0 if narrow else 2.2))
            # 3 ── the reply, as far as it has streamed
            text = "".join(w for wt, w in words if wt <= t)
            lines = self.wrap(text, inner)
            if text and int(t * 2) % 2 == 0:
                lines = lines[:-1] + [lines[-1] + "▌"] if lines else lines
            for line in lines[: int((y0 + wh - 60 - y) / (c["font"] * 1.45))]:
                d.text((left, y), line, font=self.mono, fill=TH["fg"])
                y += int(c["font"] * 1.45)
        elif int(t * 2) % 2 == 0:  # caret while typing
            last = self.wrap(shown, inner - 40)[-1]
            cy = y0 + 96 + (len(self.wrap(shown, inner - 40)) - 1) * int(c["font"] * 1.45)
            cx = left + 36 + self.mono.getlength(last)
            d.rectangle((cx, cy, cx + 3, cy + c["font"]), fill=TH["accent"])
        # 4 ── footer: what this is
        d.text((x0 + c["pad"], y0 + wh - 58), "me-agent.ru · " + ("one file, one command" if lang == "en" else "один файл, одна команда"),
               font=self.small, fill=TH["faint"])
        return img

    def save(self, path, **kw):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.draw(**kw).save(path)
        return path
