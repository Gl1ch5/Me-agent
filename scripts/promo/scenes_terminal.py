"""Terminal scenes (pty + pyte). Each returns the Term after the planned length; compose.py turns frames into video."""
import json, os, shutil, time
from pathlib import Path

import termrec

ROOT = Path(__file__).resolve().parents[2]

PRO = "~/promo"


def _env(home, stand):
    shims = Path(__file__).resolve().parent / "shims"
    path = f"{shims}:{home}/.local/bin:/usr/local/bin:/usr/bin:/bin"
    return dict(os.environ, HOME=str(home), PATH=path, PYTHONPATH=str(shims), PS1="~/promo $ ", PROMPT_COMMAND="",
                ME_SERVER=stand.base, PROMO_SERVER=stand.base, PROMO_GROQ=stand.llm_url, LANG="C.UTF-8", ME_HOME=str(home / ".me"))


def _home(work, name, stand):
    """Fresh HOME with the promo config (mock provider, fake voice key) — the key is a placeholder, not a secret."""
    home = Path(work) / name / "home"
    (home / "promo").mkdir(parents=True, exist_ok=True)
    (home / ".me").mkdir(parents=True, exist_ok=True)
    cfg = {"providers": {"mock": {"base_url": stand.llm_url + "/v1", "api_key": "promo-mock"}},
           "models": [{"name": "promo-mock", "id": "mock-1", "provider": "mock"}], "model": "promo-mock", "theme": "me-dark",
           "voice": {"groq_key": "promo-placeholder", "model": "whisper-large-v3", "language": ""}}
    (home / ".me" / "config.json").write_text(json.dumps(cfg, ensure_ascii=False))
    return home


def _shell(out, name, home, stand, **kw):
    cmd = ["bash", "--noprofile", "--norc", "-i"]
    return termrec.Term(out, name, cmd, _env(home, stand), home / "promo", **kw)


def _wait_path(t, path, timeout=60):
    end = time.time() + timeout
    while time.time() < end and not Path(path).exists():
        t.pump(0.1)
    if not Path(path).exists():
        raise AssertionError(f"promo terminal: {path} never appeared")


def _welcome(t):
    """A fresh ME_HOME shows the sign-in menu first; 'use offline' keeps everything local (no cloud is contacted)."""
    for _ in range(6):
        scr = "\n".join(t.screen.display)
        if "Welcome to me" not in scr:
            break
        t.key("down", 0.3)
        t.key("enter", 0.8)
    assert "Welcome to me" not in "\n".join(t.screen.display), "promo terminal: welcome menu did not close"
    t.wait_for("ask anything")
    t.pump(0.5)


def preinstall(home, stand):
    """Run the real installer once, outside the recording, so scenes that start from an installed `me` can type `me`."""
    t = _shell(Path(home).parent, "preinstall", home, stand, cols=100, rows=30)
    t.type("curl -fsSL https://me-agent.ru/i | sh", cps=60)
    t.key("enter", 0.2)
    _wait_path(t, home / ".local" / "bin" / "me")
    t.pump(1.0)
    t.close()


def one_file(out, stand, work, total=18.0):
    """Scene 1 (22 s video, 18 s of terminal): install with one command → `me` → a task → the result."""
    home = _home(work, "one-file", stand)
    t = _shell(out, "terminal-one-file", home, stand, cols=100, rows=30)
    t.pump(0.8)
    t.type("curl -fsSL https://me-agent.ru/i | sh", cps=18)
    t.key("enter", 0.2)
    _wait_path(t, home / ".local" / "bin" / "me")
    t.pump(1.4)
    t.type("me", cps=12)
    t.key("enter", 0.6)
    _welcome(t)
    t.type("сделай лендинг для кофейни", cps=16)
    t.key("enter", 0.3)
    t.wait_for("done:", timeout=60)
    t.finish(total)
    return t


def themes_and_modes(out, stand, work, total=18.0):
    """Scene 3, terminal part: /theme with live preview → /agent design → DESIGN.md and the page in one turn."""
    home = _home(work, "themes", stand)
    preinstall(home, stand)
    (home / ".me" / "agents").mkdir(parents=True, exist_ok=True)  # /agent design needs the preset installed, or it opens the picker
    shutil.copy(ROOT / "server" / "market" / "agent-design.md", home / ".me" / "agents" / "design.md")
    t = _shell(out, "terminal-themes-modes", home, stand, cols=100, rows=30)
    t.pump(0.4)
    t.type("me", cps=24)
    t.key("enter", 0.4)
    _welcome(t)
    t.type("/theme", cps=24)
    t.key("enter", 0.4)
    t.wait_for("Theme")
    for _ in range(2):  # ↑↓ previews each theme live (me-light flashes by), then the choice: mono
        t.key("down", 0.6)
    t.key("enter", 0.6)
    t.type("/agent design", cps=24)
    t.key("enter", 0.5)
    t.type("сделай дизайн и страницу кофейни", cps=26)
    t.key("enter", 0.2)
    t.wait_for("✓ write index.html", timeout=60)  # DESIGN.md is written first, then the page
    t.finish(total)
    return t


def vertical_voice(out, stand, work, total=11.0):
    """Scene 4 (15 s, 9:16): hold SPACE → recording → the words land in the input line."""
    home = _home(work, "vertical", stand)
    (home / ".me" / "plugins").mkdir(parents=True, exist_ok=True)
    shutil.copy(ROOT / "server" / "market" / "voice.py", home / ".me" / "plugins" / "voice.py")  # the repo's own voice plugin
    preinstall(home, stand)
    t = _shell(out, "terminal-vertical-voice", home, stand, cols=40, rows=36, font_px=36, canvas=(1080, 1920))
    t.pump(0.6)
    t.type("me", cps=12)
    t.key("enter", 0.6)
    _welcome(t)
    t.hold(2.4)  # hold space: the plugin starts recording after three quick spaces, stops ~0.45 s after release
    t.wait_for("напиши", timeout=30)  # transcription lands in the input line
    t.pump(1.5)
    t.finish(total)
    return t
