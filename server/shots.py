"""Screenshots of pages the agent put online (a published site, a preview), for chats that can't open them at a glance.
Runs headless Chromium through playwright in a child process (killed on timeout), one shot at a time; None when either is missing."""
import subprocess, sys, threading

SHOT = r"""
import glob, os, sys
from playwright.sync_api import sync_playwright
url, w, h = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
with sync_playwright() as p:
    try:
        b = p.chromium.launch(args=["--disable-dev-shm-usage"])
    except Exception:  # the browser build playwright wants is missing: any installed Chromium will do
        roots = [os.environ.get("PLAYWRIGHT_BROWSERS_PATH", ""), os.path.expanduser("~/.cache/ms-playwright"), "/opt/pw-browsers"]
        found = [f for r in roots if r for f in sorted(glob.glob(r + "/chromium*/chrome-*/chrome*"), reverse=True)
                 if os.path.basename(f) in ("chrome", "headless_shell", "chrome-headless-shell")]
        b = p.chromium.launch(executable_path=os.environ.get("ME_CHROMIUM") or found[0], args=["--disable-dev-shm-usage"])
    pg = b.new_page(viewport={"width": w, "height": h}, device_scale_factor=2, is_mobile=w < 600, has_touch=w < 600)
    pg.goto(url, wait_until="load", timeout=20000)
    pg.wait_for_timeout(1200)  # entrance animations and web fonts settle
    sys.stdout.buffer.write(pg.screenshot(type="jpeg", quality=80))
    b.close()
"""
_one = threading.Lock()


def screenshot(url, width=390, height=844, timeout=45):
    """JPEG bytes of the page's first screen at a phone's size, or None."""
    if not url.startswith(("http://", "https://")):
        return None
    with _one:
        try:
            r = subprocess.run([sys.executable, "-c", SHOT, url, str(width), str(height)], capture_output=True, timeout=timeout)
        except (OSError, subprocess.TimeoutExpired):
            return None
    return r.stdout if r.returncode == 0 and r.stdout[:2] == b"\xff\xd8" else None
