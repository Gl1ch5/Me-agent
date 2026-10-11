"""Web scenes (playwright, chromium from the image, video=on). Every context writes a .webm under out/raw/."""
import json, shutil, time
from pathlib import Path

PC = {"width": 1280, "height": 720}
PHONE = {"width": 390, "height": 844}


def _context(browser, out, name, size, token, phone=False):
    d = Path(out) / "raw" / name
    d.mkdir(parents=True, exist_ok=True)
    kw = dict(viewport=size, record_video_dir=str(d), record_video_size=size, device_scale_factor=1)
    if phone:
        kw.update(is_mobile=True, has_touch=True)
    ctx = browser.new_context(**kw)
    # the promo account's token goes straight into the browser (same key the login form stores)
    ctx.add_init_script(f"try{{localStorage.setItem('me_token', {json.dumps(token)})}}catch(e){{}}")
    page = ctx.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    return ctx, page, errors


def _log_has(page, needle, timeout=40):
    page.wait_for_function("(s) => document.querySelector('#log') && document.querySelector('#log').innerText.includes(s)",
                           arg=needle, timeout=timeout * 1000)


def phone_split(browser, stand, out, token, total=25.0, S=None):
    """Scene 2: start on the PC, open the same session on the phone, send from the phone, watch it land on the PC."""
    t0 = time.time()
    pc_ctx, pc, pc_err = _context(browser, out, "phone-pc", PC, token)
    ph_ctx, ph, ph_err = _context(browser, out, "phone-phone", PHONE, token, phone=True)
    ph.goto(stand.base + "/app")  # the phone is already open on the account when the work starts on the PC
    ph.wait_for_selector("#input", timeout=30000)
    pc.goto(stand.base + "/app")
    pc.wait_for_selector("#input", timeout=30000)
    pc.wait_for_timeout(1200)
    pc.click("#input")
    pc.keyboard.type(S["p_task"] if S else "сделай лендинг для кофейни", delay=45)
    pc.wait_for_timeout(400)
    pc.click("#send")
    _log_has(pc, "wrote index.html")
    pc.wait_for_timeout(1500)  # the PC shows the finished page
    # the phone: the session appears in its list through sync; open it from the menu and reply
    key = S["session_key"] if S else "сделай лендинг"
    ph.locator("#slist button.si", has_text=key).first.wait_for(timeout=40000)
    ph.click("button.ib.menu")
    ph.wait_for_timeout(700)
    ph.locator("#slist button.si", has_text=key).first.click()
    ph.wait_for_timeout(1200)
    ph.click("#input")
    ph.keyboard.type(S["p_phone"] if S else "добавь тёмную тему", delay=90)
    ph.wait_for_timeout(600)
    ph.click("#send")
    _log_has(pc, S["p_phone"] if S else "добавь тёмную тему")  # the phone's message reaches the PC through sync
    pc.wait_for_timeout(4000)
    pc_errors, ph_errors = list(pc_err), list(ph_err)
    pc_path = pc.video.path()
    ph_path = ph.video.path()
    ph_ctx.close()
    pc_ctx.close()
    _keep(pc_path, Path(out) / "raw" / "phone-pc.webm")
    _keep(ph_path, Path(out) / "raw" / "phone-phone.webm")
    return {"errors": pc_errors + ph_errors, "seconds": round(time.time() - t0, 1)}


def web_tour(browser, stand, out, token):
    """QA + marketing clips: /account, /market, /app at 1280x720 and 390x844, with screenshots and a JS-error check."""
    shots = Path(out) / "qa"
    shots.mkdir(parents=True, exist_ok=True)
    result = {}
    for size_name, size, phone in (("1280", PC, False), ("390", PHONE, True)):
        for page_name, path in (("account", "/account"), ("market", "/market"), ("app", "/app")):
            ctx, page, errs = _context(browser, out, f"web-{page_name}-{size_name}", size, token, phone=phone)
            page.goto(stand.base + path)
            page.wait_for_timeout(1000)
            if path == "/market":  # open the themes tab first: the part of the marketplace the theme scene is about
                page.click('#mkind button[data-k="theme"]', timeout=5000)
            page.wait_for_timeout(1500)
            page.screenshot(path=str(shots / f"{page_name}-{size_name}.png"))
            page.mouse.wheel(0, 400)
            page.wait_for_timeout(1500)
            page.screenshot(path=str(shots / f"{page_name}-{size_name}-scrolled.png"))
            video = page.video.path()
            ctx.close()
            _keep(video, Path(out) / "raw" / f"web-{page_name}-{size_name}.webm")
            result[f"{page_name}-{size_name}"] = errs
    return result


def site_view(browser, out, site_file, token=None, total=3.0):
    """Scene 3, second half: the page the agent wrote, opened in a browser at 1280x720."""
    d = Path(out) / "raw" / "site-view"
    d.mkdir(parents=True, exist_ok=True)
    ctx = browser.new_context(viewport=PC, record_video_dir=str(d), record_video_size=PC)
    page = ctx.new_page()
    errs = []
    page.on("pageerror", lambda e: errs.append(str(e)))
    page.goto(Path(site_file).resolve().as_uri())
    page.wait_for_timeout(int(total * 1000 * 0.45))
    page.mouse.wheel(0, 500)
    page.wait_for_timeout(int(total * 1000 * 0.4))
    page.screenshot(path=str(Path(out) / "qa" / "site-1280.png"))
    video = page.video.path()
    ctx.close()
    _keep(video, Path(out) / "raw" / "site-view.webm")
    return errs


def _keep(src, dst):
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(src, dst)


def site_clip(browser, out, site_file, size, name, total=3.0):
    """The page the agent wrote, at the given size (PC or phone), recorded to raw/<name>.webm."""
    d = Path(out) / "raw" / name
    d.mkdir(parents=True, exist_ok=True)
    ctx = browser.new_context(viewport=size, record_video_dir=str(d), record_video_size=size)
    page = ctx.new_page()
    page.goto(Path(site_file).resolve().as_uri())
    page.wait_for_timeout(int(total * 1000 * 0.45))
    page.mouse.wheel(0, 400)
    page.wait_for_timeout(int(total * 1000 * 0.4))
    video = page.video.path()
    ctx.close()
    _keep(video, Path(out) / "raw" / f"{name}.webm")


def market_clip(browser, stand, out, token, size, name, phone=False):
    """/market with the themes tab open: the marketplace shot for the full promo."""
    ctx, page, errs = _context(browser, out, name + "-src", size, token, phone=phone)
    page.goto(stand.base + "/market")
    page.wait_for_timeout(1000)
    page.click('#mkind button[data-k="theme"]', timeout=8000)
    page.wait_for_timeout(2600)
    video = page.video.path()
    ctx.close()
    _keep(video, Path(out) / "raw" / f"{name}.webm")
    return errs
