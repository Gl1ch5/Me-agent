"""The "writing" scene: a real headless `me --rpc` writes the coffee-shop page while the frame shows the prompt being
typed and the model's own answer streaming in word by word. Nothing is sped up — the scene lasts as long as the agent
takes, which is what makes it feel alive.

Picture: one PNG per 1/30 s drawn by page.Frame (PIL, me-dark). Sound: the keys and the words come from the prompter's
log, so clicks land exactly where the letters appear. Works for both aspects.
"""
import json, time
from pathlib import Path

import mock_promo
import page
import rpcdrive
import scenes_terminal as base
from lang import STRINGS

PROMPT = {lang: S["p_write"] for lang, S in STRINGS.items()}  # the prompt `lang.py` declares and the mock knows
TAIL = 2.6  # hold the finished page before the next scene
HOLD = 0.9   # the page is empty for a beat before the first key
STEPS = {"ru": ["читаю проект", "пишу страницу", "проверяю результат"],
         "en": ["reading the project", "writing the page", "checking it"]}


def run_agent(work, stand, lang, lead=HOLD):
    """Start the agent, type the prompt into it, wait for the turn to end. Returns the run's facts (no frames yet)."""
    work = Path(work)
    work.mkdir(parents=True, exist_ok=True)
    home = base._home(work, "writing", stand)  # fresh HOME with the promo model; the welcome menu is skipped by `skip`
    (home / ".me" / "auth.json").write_text(json.dumps({"skip": True}), "utf-8")
    mock_promo.ON[0] = True  # this scene wants the mock's word cues; full.py switches them off for the others
    cue_file = work / "cues.txt"
    cue_file.write_text("", "utf-8")
    mock_promo.LANG[0] = lang
    mock_promo.TYPING = str(cue_file)
    mock_promo.FRAME0[0] = time.time()  # every word cue is timed from here: this is the scene's frame 0
    r = rpcdrive.Rpc(home / "promo", dict(base._env(home, stand), ME_YOLO="1"))
    try:
        t0 = r.now()
        keys = [k - t0 + lead for k in r.keys(PROMPT[lang], cps=9)]  # the last moment is Enter
        r.ended.wait(120)
        done = r.now() - t0 + lead
    finally:
        r.close()
        mock_promo.TYPING = None
        mock_promo.ON[0] = False
    return dict(home=home, cue_file=cue_file, keys=keys, done_at=done, send_at=keys[-1], prompt=PROMPT[lang])


def reply(home):
    """The answer the agent wrote: the last assistant message of its session."""
    for p in sorted((Path(home) / ".me" / "sessions").glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            s = json.loads(p.read_text("utf-8"))
        except ValueError:
            continue
        for m in reversed(s.get("messages", [])):
            if m.get("role") == "assistant" and isinstance(m.get("content"), str) and m["content"].strip():
                return m["content"].strip()
    return ""


def words(cue_file, send_at, text):
    """Cue times (already on the scene's clock) paired with the reply's tokens, in order."""
    times = []
    for line in Path(cue_file).read_text("utf-8").splitlines():
        t, kind = line.split("\t")
        if kind == "word" and float(t) >= send_at:
            times.append(float(t))
    toks = [w for w in text.replace("\n", " \n ").split(" ") if w]
    out = []
    for i, t in enumerate(times):
        if i == len(times) - 1 and len(toks) > i:  # a few left over: the rest lands together on the last word
            out.append([t, " ".join(toks[i:])])
            break
        if i < len(toks):
            out.append([t, ("" if not out else " ") + toks[i]])
    return out


def draw(run, work, lang, portrait):
    """Draw the frames for a finished run. Returns (frames, dur, words)."""
    ws = words(run["cue_file"], run["send_at"], reply(run["home"]))
    dur = max(run["done_at"] + TAIL, run["send_at"] + 3.0)
    lc = page.layout(portrait)
    f = page.Frame(lc)
    frames = []
    for i in range(int(dur * 30) + 1):
        t = i / 30.0
        frames.append(f.save(Path(work) / "frames" / f"{i:05d}.png", t=t, prompt=run["prompt"], keys=run["keys"],
                             send_at=run["send_at"], words=ws, done_at=run["done_at"], lang=lang, steps=STEPS[lang],
                             name="me · writing"))
    return frames, dur, ws
