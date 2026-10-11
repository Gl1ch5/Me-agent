#!/usr/bin/env python3
"""One variant of the full promo: python3 scripts/promo/full.py <ru|en> <16x9|9x16>
Records the scenes for that language and aspect against the local stand, animates the title cards, adds lower-third
captions and fades, lays a synthesised Chopin-style score underneath, and writes dist/promo/full/me-<lang>-<aspect>.mp4."""
import json, os, shutil, sys, tempfile, time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))
import compose as C  # noqa: E402
import mock_promo  # noqa: E402
import music  # noqa: E402
import run as R  # noqa: E402  (scan patterns and helpers)
import scenes_full as F  # noqa: E402
import scenes_web as W  # noqa: E402
from lang import STRINGS  # noqa: E402
from stand import Stand  # noqa: E402

OUT = Path(os.environ.get("PROMO_OUT", ROOT / "dist" / "promo")) / "full"
WORK_ROOT = Path(os.environ.get("PROMO_WORK", ROOT / "dist" / "promo-work"))
CARDS_HTML = ROOT / "docs" / "promo" / "full" / "cards.html"

# (kind, id, seconds, caption key or None) — one long promo, ~98 s
PLAN = [
    ("card", "intro", 5, None),
    ("term", "one", 20, "one"),
    ("card", "phonecard", 3, None),
    ("phone", "phone", 22, "phone"),
    ("term", "themes", 20, "theme"),
    ("site", "site", 5, "site"),
    ("market", "market", 6, "market"),
    ("term", "voice", 12, "voice"),
    ("card", "outro", 5, None),
]


def render_card(browser, card, lang, portrait, seconds, out_dir):
    size = {"width": 1080, "height": 1920} if portrait else {"width": 1920, "height": 1080}
    d = out_dir / f"card-{card}"
    d.mkdir(parents=True, exist_ok=True)
    ctx = browser.new_context(viewport=size, record_video_dir=str(d), record_video_size=size)
    page = ctx.new_page()
    page.goto(CARDS_HTML.resolve().as_uri() + f"?card={card}&lang={lang}&v={1 if portrait else 0}")
    page.wait_for_timeout(int(seconds * 1000))
    video = page.video.path()
    ctx.close()
    dst = out_dir / f"card-{card}.webm"
    shutil.move(video, dst)
    return dst


def build(lang, aspect):
    S = STRINGS[lang]
    portrait = aspect == "9x16"
    size = (1080, 1920) if portrait else (1920, 1080)
    cfg = F.PORTRAIT if portrait else F.LANDSCAPE
    phone_size = {"width": 390, "height": 844}
    pc_size = {"width": 1280, "height": 720}
    work = WORK_ROOT / f"full-{lang}-{aspect}"
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    clips = work / "clips"
    clips.mkdir()
    mock_promo.VOICE_TEXT = S["voice_text"]  # the fake transcriber 'hears' the language's sentence
    total = sum(p[2] for p in PLAN)
    st = Stand(work / "stand")
    cast_files = []
    try:
        token = st.token_for_browser()
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(**({"executable_path": os.environ["PROMO_CHROMIUM"]} if os.environ.get("PROMO_CHROMIUM") else {}),
                                        args=["--no-sandbox"])
            cards = {}
            for kind, name, secs, _ in PLAN:
                if kind == "card":
                    cards[name] = render_card(browser, name, lang, portrait, secs + 0.6, work / "cards")
            # terminal scenes
            terms = {}
            for kind, name, secs, _ in PLAN:
                if kind != "term":
                    continue
                fn = {"one": F.one_file, "themes": F.themes, "voice": F.voice}[name]
                t = fn(work / "term", st, work, S, cfg, secs)
                t.save_cast(work / f"{name}.cast")
                (work / f"{name}.txt").write_text(t.concat_list(t.now()))
                cast_files.append(work / f"{name}.cast")
                terms[name] = work / f"{name}.txt"
                t.close()
            # web scenes
            W.phone_split(browser, st, work, token, total=22, S=S)
            site_file = work / "full-themes" / "home" / "promo" / "index.html"
            W.site_clip(browser, work, site_file, phone_size if portrait else pc_size, "site")
            W.market_clip(browser, st, work, token, phone_size if portrait else pc_size, "market", phone=portrait)
            browser.close()
    finally:
        st.close()
    raw = work / "raw"

    # captions as text files (drawtext reads them; no escaping needed)
    caps = {}
    for kind, name, secs, cap in PLAN:
        if cap:
            f = work / f"cap-{cap}.txt"
            text = S["caps"][cap]
            if portrait:  # 9:16 is narrow: two short lines instead of one long one
                import textwrap
                text = textwrap.fill(text, 26)
            f.write_text(text, encoding="utf-8")
            caps[name] = f

    # segments
    parts = []
    for kind, name, secs, cap in PLAN:
        out = clips / f"{len(parts):02d}-{name}.mp4"
        if kind == "card":
            C.segment(["-i", str(cards[name])], secs, out, size, portrait=portrait)
        elif kind == "term":
            C.segment(["-f", "concat", "-safe", "0", "-i", str(terms[name])], secs, out, size, caps.get(name), portrait)
        elif kind == "phone":
            comp = clips / "phone-raw.mp4"
            if portrait:
                C.phone_stack(raw / "phone-pc.webm", raw / "phone-phone.webm", secs, comp)
            else:
                C.phone_split(raw / "phone-pc.webm", raw / "phone-phone.webm", secs, comp)
            C.segment(["-i", str(comp)], secs, out, size, caps.get(name), portrait)
        elif kind == "site":
            C.segment(["-i", str(raw / "site.webm")], secs, out, size, caps.get(name), portrait)
        elif kind == "market":
            C.segment(["-ss", "0.9", "-i", str(raw / "market.webm")], secs, out, size, caps.get(name), portrait)
        parts.append(out)

    video = work / "video.mp4"
    C.concat_copy(parts, video)

    score = work / "score.wav"
    music.render(total, score, volume=0.42)
    OUT.mkdir(parents=True, exist_ok=True)
    final = OUT / f"me-{lang}-{aspect}.mp4"
    C.ff("-i", str(video), "-i", str(score), "-map", "0:v", "-map", "1:a", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
         "-shortest", "-movflags", "+faststart", str(final))
    C.webp_preview(final, OUT / f"me-{lang}-{aspect}.webp")

    info = C.probe(final)
    ok = abs(info["duration"] - total) < 0.3
    scan_leaks(cast_files)
    print(json.dumps({"file": final.name, "duration": round(info["duration"], 2), "expected": total, "ok": ok,
                      "size": [info["width"], info["height"]]}))
    if not ok:
        raise SystemExit(f"{final.name}: duration {info['duration']:.2f}s, expected {total}s")
    return final


def scan_leaks(files):
    hits = []
    for f in files:
        text = f.read_text("utf-8", "replace")
        for pat in R.SECRET_PATTERNS:
            if __import__("re").search(pat, text):
                hits.append(f"{f.name}: matches {pat}")
    if hits:
        raise SystemExit("secret scan failed:\n" + "\n".join(hits))


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] not in STRINGS or sys.argv[2] not in ("16x9", "9x16"):
        raise SystemExit("usage: full.py <ru|en> <16x9|9x16>")
    build(sys.argv[1], sys.argv[2])
