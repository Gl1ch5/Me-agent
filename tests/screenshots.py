"""Playwright screenshots of every UI at 360px and 1280px in every built-in theme.
Run: python3 tests/screenshots.py [outdir]   (starts its own server + mock LLM; needs playwright with chromium)"""
import json, os, subprocess, sys, tempfile, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_restart as T  # noqa: E402

me = T.me
OUT = Path(sys.argv[1] if len(sys.argv) > 1 else tempfile.mkdtemp(prefix="shots-"))
OUT.mkdir(parents=True, exist_ok=True)
THEMES = ["me-dark", "me-light", "mono", "mono-light", "graphite"]
WIDTHS = [(360, 780), (1280, 820)]


def drive(c, wid, text, kind="turn_end", path="prompt"):
    after = c.req("GET", "/api/cloud")[0]["seq"]
    c.req("POST", f"/api/cloud/{wid}/{path}", {"text": text})
    end = time.time() + 30
    while time.time() < end:
        for e in c.req("GET", f"/api/cloud/{wid}/events?after={after}", timeout=40)["events"]:
            after = e["seq"]
            if e["type"] == kind and (kind != "goal" or e.get("status") != "working"):
                return
    raise AssertionError("no " + kind)


def main():
    from playwright.sync_api import sync_playwright
    srv = T.start()
    local = None
    try:
        c = T.login()
        cfg = c.s.config
        ws = c.req("POST", "/api/ws", {"name": "demo"})["id"]
        w = c.req("POST", "/api/cloud", {"ws": ws})
        time.sleep(1.5)  # sandbox boot
        drive(c, w["id"], "tool:todo " + json.dumps({"items": ["x read the brief", "x sketch layout", "> build index.html", "check 360px", "deploy preview"]}))
        drive(c, w["id"], "tool:write " + json.dumps({"path": "index.html", "content": "<!doctype html>\n<h1>Dashboard</h1>\n<p>ok</p>\n"}))
        drive(c, w["id"], "ship the dashboard", "goal", "goal")
        token = c.s.auth["token"]
        # local `me web` with its own agent (no cloud), same mock model
        home = tempfile.mkdtemp()
        st = me.Store(home)
        st.config.update({k: cfg[k] for k in ("providers", "models", "model")})
        st.save()
        proj = tempfile.mkdtemp()
        local = subprocess.Popen([sys.executable, str(T.ROOT / "me.py"), "web", "8798", "--no-open"], cwd=proj,
                                 env=dict(os.environ, ME_HOME=home), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        time.sleep(2)
        with sync_playwright() as p:
            b = p.chromium.launch(executable_path=os.environ.get("PW_CHROMIUM") or None)
            for theme in THEMES:
                cfg["theme"] = theme
                c.req("PUT", "/api/doc", {"key": "config", "content": json.dumps(cfg)})
                for W, H in WIDTHS:
                    ctx = b.new_context(viewport={"width": W, "height": H}, device_scale_factor=1)
                    ctx.add_init_script(f"localStorage.setItem('me_token', {json.dumps(token)}); localStorage.removeItem('me_theme_cache')")
                    pg = ctx.new_page()
                    errors = []
                    pg.on("pageerror", lambda e: errors.append(str(e)))
                    pg.goto(f"{T.SERVER}/app?ws={ws}")
                    pg.wait_for_timeout(2500)
                    pg.screenshot(path=str(OUT / f"app-{theme}-{W}.png"))
                    if W == 360:
                        pg.click("header .menu")
                        pg.wait_for_timeout(400)
                        pg.screenshot(path=str(OUT / f"app-drawer-{theme}-{W}.png"))
                    for tab in ("cloud", "market"):
                        pg.goto(f"{T.SERVER}/account#{tab}")
                        pg.wait_for_timeout(1800)
                        pg.screenshot(path=str(OUT / f"account-{tab}-{theme}-{W}.png"), full_page=True)
                    lp = ctx.new_page()
                    lp.goto("http://127.0.0.1:8798/")
                    lp.wait_for_timeout(1500)
                    lp.evaluate(f"setTheme({json.dumps(theme)})")
                    lp.wait_for_timeout(500)
                    lp.screenshot(path=str(OUT / f"local-{theme}-{W}.png"))
                    overflow = pg.evaluate("document.documentElement.scrollWidth > innerWidth + 1")
                    if errors or overflow:
                        print(f"!! {theme} {W}: errors={errors[:3]} overflow={overflow}")
                    ctx.close()
            b.close()
        print("screenshots in", OUT)
    finally:
        os.killpg(srv.pid, 9)
        if local:
            os.killpg(local.pid, 9)


if __name__ == "__main__":
    main()
