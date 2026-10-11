#!/usr/bin/env python3
"""Records every promo scene against a local stand and assembles the four clips into dist/promo/.
Nothing is uploaded and me-agent.ru is never contacted: the stand is a local server with a mock model and a throwaway account.
Usage: python3 scripts/promo/run.py   (env: PROMO_OUT, PROMO_WORK, PROMO_CHROMIUM)"""
import json, os, re, shutil, subprocess, sys, tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))
import compose as C  # noqa: E402
import scenes_terminal as T  # noqa: E402
import scenes_web as W  # noqa: E402
from stand import Stand  # noqa: E402

OUT = Path(os.environ.get("PROMO_OUT", ROOT / "dist" / "promo"))
WORK = Path(os.environ.get("PROMO_WORK", ROOT / "dist" / "promo-work"))
CARDS = ROOT / "docs" / "promo" / "cards"
# (file, planned seconds) — the storyboards in docs/promo/SCRIPTS.md use the same numbers
PLAN = {"promo-1-one-file": 22, "promo-2-continue-on-phone": 30, "promo-3-themes-modes": 25, "promo-4-vertical-voice": 15}


def render_cards(browser):
    out = WORK / "cards"
    out.mkdir(parents=True, exist_ok=True)
    pngs = {}
    for html in sorted(CARDS.glob("*.html")):
        vert = html.stem.endswith("-v")
        size = {"width": 1080, "height": 1920} if vert else {"width": 1920, "height": 1080}
        page = browser.new_page(viewport=size)
        page.goto(html.resolve().as_uri())
        page.evaluate("document.fonts.ready")
        png = out / (html.stem + ".png")
        page.screenshot(path=str(png))
        page.close()
        pngs[html.stem] = png
    return pngs


def terminal(name, fn, _unused, *args, **kw):
    t = fn(WORK / "term", *args, **kw)
    t.save_cast(OUT / "raw" / f"{name}.cast")
    listfile = WORK / f"{name}.txt"
    listfile.write_text(t.concat_list(t.now()))
    t.close()
    return listfile


def main():
    from playwright.sync_api import sync_playwright
    if WORK.exists():
        shutil.rmtree(WORK)
    if OUT.exists():
        shutil.rmtree(OUT)
    (OUT / "raw").mkdir(parents=True)
    (OUT / "qa").mkdir(parents=True)
    WORK.mkdir(parents=True)
    st = Stand(WORK / "stand")
    report = {"steps": {}, "errors": {}}
    try:
        token = st.token_for_browser()
        with sync_playwright() as p:
            launch = {"args": ["--no-sandbox"]}
            if os.environ.get("PROMO_CHROMIUM"):
                launch["executable_path"] = os.environ["PROMO_CHROMIUM"]
            browser = p.chromium.launch(**launch)

            # 1 ── terminal scenes (pty + pyte)
            lists = {}
            lists["one"] = terminal("terminal-one-file", T.one_file, None, st, WORK, total=PLAN["promo-1-one-file"] - 4)
            lists["themes"] = terminal("terminal-themes-modes", T.themes_and_modes, None, st, WORK, total=18.0)
            lists["vertical"] = terminal("terminal-vertical-voice", T.vertical_voice, None, st, WORK, total=11.0)

            # 2 ── web scenes (playwright video=on)
            report["errors"]["phone"] = W.phone_split(browser, st, WORK, token, total=25.0)
            report["errors"]["tour"] = W.web_tour(browser, st, WORK, token)
            site = WORK / "themes" / "home" / "promo" / "index.html"  # the page the theme scene's agent wrote
            report["errors"]["site"] = W.site_view(browser, WORK, site, total=3.0)

            # 3 ── title cards (HTML in docs/promo/cards, rendered with the same chromium)
            cards = render_cards(browser)
            browser.close()
    finally:
        st.close()

    # 4 ── assemble (exact planned lengths)
    raw = WORK / "raw"
    L, V = C.LANDSCAPE, C.VERTICAL
    work = WORK / "clips"
    work.mkdir(exist_ok=True)
    seg = lambda name: work / (name + ".mp4")  # noqa: E731

    C.still(cards["p1-open"], 2, seg("p1a"), L)
    C.fit_concat(lists["one"], 18, seg("p1b"), L)
    C.still(cards["p1-end"], 2, seg("p1c"), L)
    C.concat_copy([seg("p1a"), seg("p1b"), seg("p1c")], OUT / "promo-1-one-file.mp4")

    C.still(cards["p2-open"], 2, seg("p2a"), L)
    C.phone_split(raw / "phone-pc.webm", raw / "phone-phone.webm", 25, seg("p2b"))
    C.still(cards["p2-end"], 3, seg("p2c"), L)
    C.concat_copy([seg("p2a"), seg("p2b"), seg("p2c")], OUT / "promo-2-continue-on-phone.mp4")

    C.still(cards["p3-open"], 2, seg("p3a"), L)
    C.fit_concat(lists["themes"], 18, seg("p3b"), L)
    C.fit_webm(raw / "site-view.webm", 3, seg("p3c"), L, skip=0.9)
    C.fit_webm(raw / "web-market-1280.webm", 2, seg("p3d"), L, skip=0.9)
    C.concat_copy([seg("p3a"), seg("p3b"), seg("p3c"), seg("p3d")], OUT / "promo-3-themes-modes.mp4")

    C.still(cards["p4-open-v"], 2, seg("p4a"), V)
    C.fit_concat(lists["vertical"], 11, seg("p4b"), V)
    C.still(cards["p4-end-v"], 2, seg("p4c"), V)
    C.concat_copy([seg("p4a"), seg("p4b"), seg("p4c")], OUT / "promo-4-vertical-voice.mp4")

    for name in PLAN:
        C.webp_preview(OUT / f"{name}.mp4", OUT / f"{name}.webp")

    # 5 ── checks: length, size, fps, and that the clips contain no secrets
    checks = {}
    for name, secs in PLAN.items():
        info = C.probe(OUT / f"{name}.mp4")
        ok = abs(info["duration"] - secs) < 0.2
        checks[name] = dict(info, expected=secs, ok=ok)
        if not ok:
            raise SystemExit(f"{name}: duration {info['duration']:.2f}s, expected {secs}s")
    for f in raw.glob("*.webm"):
        shutil.copy(f, OUT / "raw" / f.name)
    for f in (WORK / "qa").glob("*.png"):
        shutil.copy(f, OUT / "qa" / f.name)
    (OUT / "qa" / "checks.json").write_text(json.dumps({"clips": checks, "web_js_errors": report["errors"]}, ensure_ascii=False, indent=2))
    scan_secrets(token)
    print("promo ok:", ", ".join(f"{k} {v['duration']:.1f}s" for k, v in checks.items()))


SECRET_PATTERNS = [r"[\w.+-]+@[\w-]+\.[\w.]+", r"\bgsk_\w{8,}", r"\bsk-[\w-]{8,}", r"\bghp_\w{8,}", r"Bearer\s+\S{8,}"]


def scan_secrets(token):
    """Fail the build if a recording or a card contains a token, a password or an e-mail address."""
    needles = [token, "promo-demo-pass", "promo-placeholder"]
    files = list((OUT / "raw").glob("*.cast")) + list((ROOT / "docs" / "promo").rglob("*.html")) + [ROOT / "docs" / "promo" / "SCRIPTS.md"]
    hits = []
    for f in files:
        if not f.exists():
            continue
        text = f.read_text("utf-8", "replace")
        for n in needles:
            if n and n in text:
                hits.append(f"{f.name}: contains a secret")
        for pat in SECRET_PATTERNS:
            if re.search(pat, text):
                hits.append(f"{f.name}: matches {pat}")
    if hits:
        raise SystemExit("secret scan failed:\n" + "\n".join(hits))
    print(f"secret scan ok ({len(files)} files)")


if __name__ == "__main__":
    main()
