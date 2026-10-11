"""Parameterised terminal scenes for the full promo (language × aspect). Slower pacing than the short clips."""
from pathlib import Path

import scenes_terminal as base

LANDSCAPE = dict(cols=100, rows=30, font_px=24, canvas=(1920, 1080))
PORTRAIT = dict(cols=40, rows=38, font_px=34, canvas=(1080, 1920))
INSTALL = "curl -fsSL https://me-agent.ru/i | sh"


def one_file(out, stand, work, S, cfg, total):
    home = base._home(work, "full-one", stand)
    t = base._shell(out, "me · install", home, stand, **cfg)
    t.pump(1.2)
    t.type(INSTALL, cps=9)
    t.key("enter", 0.8)
    base._wait_path(t, home / ".local" / "bin" / "me")
    t.pump(1.4)
    t.type("me", cps=8)
    t.key("enter", 0.9)
    base._welcome(t)
    t.pump(0.6)
    t.type(S["p_task"], cps=9)
    t.pump(0.7)
    t.key("enter", 0.3)
    t.wait_for("wrote index.html", timeout=90)
    t.pump(1.5)
    t.finish(total)
    return t


def themes(out, stand, work, S, cfg, total):
    home = base._home(work, "full-themes", stand)
    base.preinstall(home, stand)
    t = base._shell(out, "me · themes & modes", home, stand, **cfg)
    t.pump(0.8)
    t.type("me", cps=8)
    t.key("enter", 0.9)
    base._welcome(t)
    t.type("/theme", cps=8)
    t.key("enter", 0.9)
    t.wait_for("Theme")
    t.pump(0.8)
    for _ in range(2):  # live preview: each step repaints the whole UI
        t.key("down", 1.0)
    t.key("enter", 1.0)
    t.type("/agent design", cps=8)
    t.key("enter", 1.0)
    t.type(S["p_design"], cps=10)
    t.key("enter", 0.3)
    t.wait_for("wrote index.html", timeout=90)
    t.pump(1.2)
    t.finish(total)
    return t


def voice(out, stand, work, S, cfg, total):
    home = base._home(work, "full-voice", stand)
    (home / ".me" / "plugins").mkdir(parents=True, exist_ok=True)
    import shutil
    shutil.copy(base.ROOT / "server" / "market" / "voice.py", home / ".me" / "plugins" / "voice.py")
    base.preinstall(home, stand)
    t = base._shell(out, "me · voice", home, stand, **cfg)
    t.pump(0.9)
    t.type("me", cps=8)
    t.key("enter", 0.9)
    base._welcome(t)
    t.pump(0.8)
    for attempt in range(3):  # the hold is timing-sensitive on a slow runner: retry the gesture if nothing lands
        t.hold(2.6)  # hold space: recording starts after three quick spaces, stops after release
        try:
            t.wait_for(S["voice_word"], timeout=15)
            break
        except AssertionError:
            if attempt == 2:
                raise
            t.pump(1.0)
    t.pump(1.6)
    t.finish(total)
    return t
