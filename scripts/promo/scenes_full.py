"""Parameterised terminal scenes for the full promo (language × aspect). Slower pacing than the short clips."""
from pathlib import Path

import scenes_terminal as base

LANDSCAPE = dict(cols=100, rows=30, font_px=24, canvas=(1920, 1080))
PORTRAIT = dict(cols=40, rows=38, font_px=34, canvas=(1080, 1920))
INSTALL = "curl -fsSL https://me-agent.ru/i | sh"


def one_file(out, stand, work, S, cfg, total):
    home = base._home(work, "full-one", stand)
    t = base._shell(out, "promo · install", home, stand, **cfg)
    t.pump(1.2)
    t.type(INSTALL, cps=9)
    t.key("enter", 0.8)
    base._wait_path(t, home / ".local" / "bin" / "me")
    t.pump(1.4)
    t.type("me", cps=8)
    t.key("enter", 0.9)
    base._welcome(t)
    t.pump(0.6)
    t.type(S["p_task"], cps=11)
    t.pump(0.4)
    t.key("enter", 0.3)
    t.wait_for("wrote index.html", timeout=90)
    t.pump(1.5)
    t.finish(total)
    return t


def themes(out, stand, work, S, cfg, total):
    home = base._home(work, "full-themes", stand)
    base.preinstall(home, stand)
    t = base._shell(out, "promo · themes & modes", home, stand, **cfg)
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


def marketplace(out, stand, work, S, cfg, total):
    """/theme → "+ get more themes" → the marketplace list → pick one: the theme changes on the spot, in every colour.

    The picker takes a typed filter, so the scene types "coffee" and presses Enter: whatever the list holds above it
    (a new built-in theme, an extra one in ~/.me/themes) the selection stays right — a real install from the stand."""
    home = base._home(work, "full-market", stand)
    base.preinstall(home, stand)
    t = base._shell(out, "promo · themes & modes", home, stand, **cfg)
    t.pump(0.5)
    t.type("me", cps=8)
    t.key("enter", 0.8)
    base._welcome(t)
    t.type("/theme", cps=9)
    t.key("enter", 0.7)
    t.wait_for("Theme")
    t.pump(0.4)
    t.key("down", 0.4)  # the list previews as it moves: the whole UI repaints
    t.key("up", 0.4)
    t.type("market", cps=9)  # the picker filters by typed text: "+ get more themes" is the only match
    t.key("enter", 1.0)  # → the marketplace
    t.wait_for("Marketplace", timeout=40)
    t.pump(1.6)  # the list itself: names, descriptions
    t.type("coffee", cps=9)  # the promo stand has published the "coffee" theme (stand.py, from scripts/promo/theme-coffee.json)
    t.key("enter", 2.4)  # install: the file lands in ~/.me/themes and the whole UI repaints in the new palette
    t.pump(1.2)
    t.finish(total)
    return t


def voice(out, stand, work, S, cfg, total):
    home = base._home(work, "full-voice", stand)
    (home / ".me" / "plugins").mkdir(parents=True, exist_ok=True)
    import shutil
    shutil.copy(base.ROOT / "server" / "market" / "voice.py", home / ".me" / "plugins" / "voice.py")
    base.preinstall(home, stand)
    t = base._shell(out, "promo · voice", home, stand, **cfg)
    t.pump(0.9)
    t.type("me", cps=8)
    t.key("enter", 0.9)
    base._welcome(t)
    t.pump(0.8)
    done = False
    for attempt in range(2):  # the space gesture is timing-sensitive: two tries with the hold
        t.hold(2.6)  # hold space: recording starts after three quick spaces, stops after release
        try:
            t.wait_for(S["voice_word"], timeout=12)
            done = True
            break
        except AssertionError:
            t.pump(1.0)
    if not done:  # fallback: the same plugin's /voice command (any key stops it), independent of timing
        t.type("/voice", cps=8)
        t.key("enter", 0.8)
        t.pump(2.6)
        t.key("tab", 0.8)  # any key stops the recording
        t.wait_for(S["voice_word"], timeout=40)
    t.pump(1.6)
    t.finish(total)
    return t
