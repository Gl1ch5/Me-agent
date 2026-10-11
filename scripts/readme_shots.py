"""Screenshots for the README and the site (docs/screenshots/*.webp), English and Russian.
Starts its own server + mock model (tests/test_restart.py helpers), fills a demo project, then shoots the cloud web app,
the account pages, the landing page and the terminal UI. No real keys or accounts are involved.
Run: python3 scripts/readme_shots.py [outdir]   (needs playwright + chromium, Pillow, pyte)"""
import io, json, os, subprocess, sys, tempfile, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "tests"), str(ROOT / "scripts" / "promo")]
import test_restart as T  # noqa: E402
from screenshots import drive  # noqa: E402
from PIL import Image  # noqa: E402

me = T.me
OUT = Path(sys.argv[1] if len(sys.argv) > 1 else ROOT / "docs" / "screenshots")
OUT.mkdir(parents=True, exist_ok=True)
TABS = ["overview", "cloud", "models", "skills", "market", "sessions", "workspaces", "security"]


def save(png, name, size=None):
    im = Image.open(io.BytesIO(png)) if isinstance(png, bytes) else png
    im = im.convert("RGB")
    if size:
        im = im.resize(size, Image.LANCZOS)
    im.save(OUT / f"{name}.webp", "WEBP", quality=84, method=6)


def shoot(pg, name, **kw):
    save(pg.screenshot(type="png", **kw), name)


def web_shots(b, token, ws, lang):
    L = "ru-RU" if lang == "ru" else "en-US"
    for W, H in ((1280, 800), (360, 760)):
        ctx = b.new_context(viewport={"width": W, "height": H}, locale=L)
        ctx.add_init_script(f"localStorage.setItem('me_token', {json.dumps(token)}); localStorage.setItem('me-lang', {json.dumps(lang)}); localStorage.removeItem('me_theme_cache')")
        pg = ctx.new_page()
        pg.goto(f"{T.SERVER}/app?ws={ws}")
        pg.wait_for_timeout(2500)
        shoot(pg, f"app-chat-{lang}-{W}")
        if W == 360:
            pg.click("header .menu")
            pg.wait_for_timeout(500)
            shoot(pg, f"app-drawer-{lang}-{W}")
        for tab in (TABS if W == 1280 else ("overview", "models")):
            pg.goto(f"{T.SERVER}/account#{tab}")
            pg.reload()
            pg.wait_for_timeout(1800)
            shoot(pg, f"account-{tab}-{lang}-{W}", full_page=(tab == "overview" and W == 360))
        ctx.close()
    ctx = b.new_context(viewport={"width": 360, "height": 640}, locale=L)
    ctx.add_init_script(f"localStorage.setItem('me-lang', {json.dumps(lang)})")
    pg = ctx.new_page()
    pg.goto(T.SERVER + "/account")
    pg.wait_for_timeout(900)
    shoot(pg, f"login-{lang}-360")
    ctx.close()
    for W, H in ((1280, 820), (360, 780)):
        ctx = b.new_context(viewport={"width": W, "height": H}, locale=L)
        ctx.add_init_script(f"localStorage.setItem('me-lang', {json.dumps(lang)})")
        pg = ctx.new_page()
        pg.goto(T.SERVER + "/")
        pg.wait_for_timeout(2600)
        shoot(pg, f"site-hero-{lang}-{W}")
        if W == 1280:
            pg.locator("#features").scroll_into_view_if_needed()
            pg.wait_for_timeout(300)
            shoot(pg, f"site-features-{lang}-{W}")
        ctx.close()


def themes_grid(b, c, cfg, ws):
    token = c.s.auth["token"]
    names = ["me-dark", "me-light", "mono", "graphite"]
    tiles = []
    for th in names:
        cfg["theme"] = th
        c.req("PUT", "/api/doc", {"key": "config", "content": json.dumps(cfg)})
        ctx = b.new_context(viewport={"width": 1000, "height": 640})
        ctx.add_init_script(f"localStorage.setItem('me_token', {json.dumps(token)}); localStorage.setItem('me-lang','en'); localStorage.removeItem('me_theme_cache')")
        pg = ctx.new_page()
        pg.goto(f"{T.SERVER}/app?ws={ws}")
        pg.wait_for_timeout(2200)
        tiles.append(Image.open(io.BytesIO(pg.screenshot(type="png"))).convert("RGB").resize((800, 512), Image.LANCZOS))
        ctx.close()
    grid = Image.new("RGB", (1600, 1024))
    for i, im in enumerate(tiles):
        grid.paste(im, ((i % 2) * 800, (i // 2) * 512))
    save(grid, "themes")
    cfg["theme"] = "me-dark"


def tui_shots(home_cfg):
    import termrec
    from scenes_terminal import _welcome
    home = Path(tempfile.mkdtemp())
    (home / ".me").mkdir()
    (home / ".me" / "config.json").write_text(json.dumps(home_cfg))
    work = home / "project"
    work.mkdir()
    env = dict(os.environ, HOME=str(home), ME_HOME=str(home / ".me"), ME_SERVER=T.SERVER, LANG="C.UTF-8", TERM="xterm-256color")
    tmp = Path(tempfile.mkdtemp())
    for name, script in (("tui-commands", "/"), ("tui-turn", 'tool:write {"path":"index.html","content":"<h1>Coffee</h1>"}')):
        t = termrec.Term(tmp, name, [sys.executable, str(ROOT / "me.py")], env, work, cols=100, rows=28, font_px=17, canvas=(1500, 760))
        try:
            _welcome(t)
            t.type(script, cps=60)
            if name == "tui-turn":
                t.key("enter", 0.3)
                t.wait_for("index.html", 30)
                t.pump(2.5)
            else:
                t.pump(0.8)
            last = sorted((tmp / f"{name}-frames").glob("*.png"))[-1]
            save(last.read_bytes(), name)
        finally:
            t.close()


def main():
    from playwright.sync_api import sync_playwright
    srv = T.start()
    try:
        c = T.login()
        cfg = c.s.config
        cfg["models"][0]["name"] = "demo"
        cfg["model"] = "demo"
        c.req("PUT", "/api/doc", {"key": "config", "content": json.dumps(cfg)})
        ws = c.req("POST", "/api/ws", {"name": "coffee-site"})["id"]
        w = c.req("POST", "/api/cloud", {"ws": ws})
        time.sleep(1.5)
        drive(c, w["id"], "tool:todo " + json.dumps({"items": ["x read the brief", "x sketch layout", "> build index.html", "check 360px", "deploy preview"]}))
        drive(c, w["id"], "tool:write " + json.dumps({"path": "index.html", "content": "<!doctype html>\n<h1>Coffee</h1>\n<p>Fresh roasts.</p>\n"}))
        drive(c, w["id"], "ship the landing page", "goal", "goal")
        token = c.s.auth["token"]
        with sync_playwright() as p:
            b = p.chromium.launch(executable_path=os.environ.get("PW_CHROMIUM") or None)
            for lang in ("en", "ru"):
                web_shots(b, token, ws, lang)
            themes_grid(b, c, cfg, ws)
            b.close()
        tui_shots({"providers": cfg["providers"], "models": cfg["models"], "model": cfg["model"], "theme": "me-dark"})
        print("screenshots in", OUT, len(list(OUT.glob("*.webp"))), "files")
    finally:
        os.killpg(srv.pid, 9)


if __name__ == "__main__":
    main()
