#!/usr/bin/env python3
"""The full promo. python3 scripts/promo/full.py <ru|en> <16x9|9x16> [--tail]  — one variant;
                      python3 scripts/promo/full.py all                        — all four at once, in parallel jobs.

Each variant gets its own stand (its own mock, server, port and work dir), so four of them run side by side in separate
processes: that is the fastest wall-clock way to get every video, and one failure never stops the others.

Records the scenes for that language and aspect against the local stand, animates the title cards, and builds the film
scene by scene:

  1. picture — the recording (terminal frames, browser video or the page that writes itself), captions, crossfades;
  2. sound   — key clicks on the typed prompts, a sound for every action, a soft chime when a file lands (the cues come
               from the scenes themselves: the mock's word cues, the browser marks, the terminal's own timing);
  3. score   — a public-domain Chopin nocturne underneath (Musopen, CC0 — see docs/promo/SFX-PACK.md), ducked while
               the cues play, so nothing is drowned. `--tail` adds a closing card with the credits.

Writes dist/promo/full/me-<lang>-<aspect>.mp4, a .webp preview, the cue log (.cues.txt) and a report (.report.json).
PROMO_NO_MUSIC=1 builds it without the score, PROMO_MUSIC=0.5 makes the bed louder, PROMO_ONE/PROMO_MARKET shorten the two
terminal scenes."""
import json, os, re, shutil, subprocess, sys, textwrap, time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))
import compose as C  # noqa: E402
import mock_promo  # noqa: E402
import run as R  # noqa: E402  (secret scan patterns)
import scene_page  # noqa: E402
import scenes_full as F  # noqa: E402
import scenes_web as W  # noqa: E402
import sfx  # noqa: E402
from lang import STRINGS  # noqa: E402
from stand import Stand  # noqa: E402

OUT = Path(os.environ.get("PROMO_OUT", ROOT / "dist" / "promo")) / "full"
WORK_ROOT = Path(os.environ.get("PROMO_WORK", ROOT / "dist" / "promo-work"))
CARDS_HTML = ROOT / "docs" / "promo" / "full" / "cards.html"
XFADE = 0.45  # between scenes: a visible dissolve; the cards fade into the action instead of cutting
BG = C.BG  # the me-dark background the browser scenes sit on


def plan(lang, tail=False, write=16.0):
    """(kind, id, seconds, caption key). The "write" scene lasts as long as the agent took: `write` is its length."""
    p = [
        ("card", "intro", 4.5, None),
        ("term", "one", float(os.environ.get("PROMO_ONE") or 20), "one"),
        ("write", "write", write, "write"),
        ("card", "phonecard", 2.6, None),
        ("phone", "phone", 19.0, "phone"),
        ("term", "market", float(os.environ.get("PROMO_MARKET") or 16), "theme"),
        ("site", "site", 5.0, "site"),
        ("webmarket", "web-market", 5.0, "market"),
        ("term", "voice", 11.0, "voice"),
        ("card", "outro", 5.0, None),
    ]
    if tail:
        p.append(("card", "credits", 4.0, None))
    return p


def render_card(browser, html, card, lang, portrait, seconds, out_dir):
    """Film one animated title card: a browser page played for `seconds` (`v=1` is the 9:16 layout)."""
    size = {"width": 1080, "height": 1920} if portrait else {"width": 1920, "height": 1080}
    d = out_dir / f"card-{card}"
    d.mkdir(parents=True, exist_ok=True)
    ctx = browser.new_context(viewport=size, record_video_dir=str(d), record_video_size=size)
    page = ctx.new_page()
    page.goto(Path(html).resolve().as_uri() + f"?card={card}&lang={lang}&v={1 if portrait else 0}")
    page.wait_for_timeout(int(seconds * 1000))
    video = page.video.path()
    ctx.close()
    dst = out_dir / f"card-{card}.webm"
    shutil.move(video, dst)
    return dst


# ── timelines ─────────────────────────────────────────────────────────────────────────────────────────

def term_timeline(t):
    """A recorded terminal scene → cues: a key click for every character typed, a click for every Enter, and a
    "pick" for each arrow/space gesture (the theme picker, the voice hold). The pty knows exactly what went in
    and when (the cast log), so the sound matches the picture instead of guessing from the frames."""
    tl = sfx.Timeline()
    typed, sends, moves = _typed_from_cast(t)
    for a, b, n in typed:
        tl.typing(a, b, n)
    for at in sends:
        tl.at(at, "click")
    for at in moves:
        tl.at(at, "pick", 0.28)
    return tl


def _typed_from_cast(t):
    """Read the cast log back: what was typed, when, and the keys that are not characters.

    A typed line arrives one character at a time and is sent with Enter; the TUI echoes nothing back meanwhile, so every
    run of printable characters between two Enter presses is one input. Arrow keys and space are what drives the menus:
    each one gets a sound of its own. Returns ([(t0, t1, chars)], [enter_t], [menu_key_t])."""
    typed, sends, moves = [], [], []
    run, buf = [], ""
    csi = None  # the bytes of an escape sequence being read

    def end_run():
        nonlocal run, buf
        if buf:
            typed.append((run[0], run[-1], len(buf)))
        run, buf = [], ""

    for at, text in t.cast:
        for ch in text:
            if csi is not None:
                csi += ch
                if ch.isalpha() or ch == "~":  # a complete CSI sequence: ESC [ A / ESC [ 1 ~ …
                    if csi in ("\x1b[A", "\x1b[B", "\x1b[C", "\x1b[D"):
                        end_run()
                        moves.append(at)
                    csi = None
                continue
            if ch == "\x1b":
                csi = ch
                continue
            if ch in "\r\n":
                end_run()
                sends.append(at)
                continue
            if ch == " ":
                end_run()  # the voice hold: space is a menu gesture here, not a character of a sentence
                moves.append(at)
                continue
            if ch.isprintable():
                if not buf:
                    run = []
                run.append(at)
                buf += ch
            elif ch == "\x7f" and buf:  # a keystroke taken back: drop it from the run too
                buf, run = buf[:-1], run[:-1]
    end_run()
    return typed, sends, moves


def _thin(tl, per_sec=12, cue="key"):
    """Keep at most `per_sec` clicks a second: typed prompts are already thinned by Timeline.typing, the page scene
    clicks every letter, which sounds like a woodpecker."""
    kept, last = [], {}
    for t, c, g in sorted(tl.events):
        if c == cue:
            if t - last.get(cue, -9) < 1.0 / per_sec:
                continue
            last[cue] = t
        kept.append((t, c, g))
    tl.events = kept
    return tl


def web_timeline(marks):
    """Cues for a browser scene, as the scene marked them while it recorded: clicks, typed text, the message landing."""
    tl = sfx.Timeline()
    for t, cue in marks or ():
        tl.at(t, cue)
    return tl


# ── the build ────────────────────────────────────────────────────────────────────────────────────────

def build(lang, aspect, tail=False):
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
    mock_promo.LANG[0] = lang
    mock_promo.ON[0] = False  # the mock writes cue lines only for the page scene; every other scene is timed from its record
    size_web = phone_size if portrait else pc_size  # the browsers record at the phone's size on 9:16
    marks, cast_files, track = {}, [], {}
    st = Stand(work / "stand")
    try:
        token = st.token_for_browser()
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(**({"executable_path": os.environ["PROMO_CHROMIUM"]} if os.environ.get("PROMO_CHROMIUM") else {}),
                                        args=["--no-sandbox"])
            cards, terms = {}, {}
            for kind, name, secs, _ in plan(lang, tail):  # the cards are pure type: film them first, all at once
                if kind == "card":
                    cards[name] = render_card(browser, CARDS_HTML, name, lang, portrait, secs + 0.6, work / "cards")
            # terminal scenes: a real pty, a frame per change
            fn = {"one": F.one_file, "market": F.marketplace, "voice": F.voice}
            for kind, name, secs, _ in plan(lang, tail):
                if kind != "term":
                    continue
                t = fn[name](work / "term", st, work, S, cfg, secs)
                t.save_cast(work / f"{name}.cast")
                (work / f"{name}.txt").write_text(t.concat_list(t.now()))
                cast_files.append(work / f"{name}.cast")
                terms[name] = work / f"{name}.txt"
                track[name] = term_timeline(t)
                if name == "one" and t.last_seen("curl -fsSL") is not None:  # the installer starts downloading
                    track[name].at(t.last_seen("curl -fsSL"), "now")
                t.close()
            # the page that writes itself: a real agent run, then a frame per 1/30 s
            run = scene_page.run_agent(work / "page", st, lang)
            frames, wdur, wwords = scene_page.draw(run, work / "page", lang, portrait)
            page = dict(dur=wdur, keys=run["keys"], words=wwords, send_at=run["send_at"], done_at=run["done_at"],
                        prompt=run["prompt"], frames=frames)
            steps = plan(lang, tail, write=wdur)
            durs = {n: s for _, n, s, _ in steps}
            for kind, name, secs, _ in steps:
                if kind == "card" and name not in cards:
                    cards[name] = render_card(browser, CARDS_HTML, name, lang, portrait, secs + 0.6, work / "cards")
            track["write"] = sfx.Timeline()
            for k in page["keys"]:  # one click per character of the prompt, at the moment it was typed
                track["write"].at(k, "key")
            _thin(track["write"], per_sec=12)  # a long run of clicks is too much for the ear: thin them out
            track["write"].at(page["send_at"], "click")
            for wt, _ in page["words"]:  # the words as they appear: a soft tick, the tool thinking out loud
                track["write"].at(wt, "tick", 0.16)
            track["write"].at(page["done_at"], "pop")
            # browser scenes (their browsers talk to the same mock: cue lines stay off)
            ph = W.phone_split(browser, st, work, token, total=durs["phone"], S=S)
            track["phone"] = web_timeline(ph["marks"])
            site_dur = durs["site"]
            try:
                found = sorted(rglob_quiet(work / "page", "index.html"), key=lambda p: p.stat().st_mtime)
                site_file = found[-1]  # the page the writing scene's agent wrote (in its own HOME/promo)
            except IndexError:  # the agent wrote nothing: the scene falls back to a card below
                site_file = work / "no-page.html"
            if site_file.exists():
                smarks, _ = W.site_clip(browser, work, site_file, size_web, "site", total=site_dur)
                track["site"] = web_timeline([(t, c) for t, c in smarks if t < site_dur])
            else:  # the agent's page is missing (the writing scene failed): the closing card stands in for it
                cards["site"] = render_card(browser, CARDS_HTML, "outro", lang, portrait, site_dur, work / "cards")

            herr, hmarks = W.market_clip(browser, st, work, token, size_web, "web-market", phone=portrait,
                                         total=durs["web-market"])
            track["web-market"] = web_timeline(hmarks)
            marks["web_js_errors"] = {"phone": ph["errors"], "market": herr}
            browser.close()
    finally:
        mock_promo.ON[0] = False
        mock_promo.TYPING = None
        st.close()
        shutil.rmtree(work / "stand", ignore_errors=True)  # a few hundred MB of server data per variant, not needed after
    raw = work / "raw"

    # captions as text files (drawtext reads them; no escaping needed)
    caps = {}
    for kind, name, secs, cap in steps:
        if cap:
            f = work / f"cap-{cap}.txt"
            text = S["caps"].get(cap, "")
            if portrait:  # 9:16 is narrow: two short lines instead of one long one
                text = textwrap.fill(text, 26)
            f.write_text(text, encoding="utf-8")
            caps[name] = f

    # segments, in order, each with its own timeline
    parts, timelines = [], []
    for kind, name, secs, cap in steps:
        out = clips / f"{len(parts):02d}-{name}.mp4"
        if kind == "card":
            C.segment(["-i", str(cards[name])], secs, out, size, portrait=portrait, fade=0.35)
            parts.append(out)
            timelines.append(sfx.Timeline())
            continue
        if kind == "term":
            C.segment(["-f", "concat", "-safe", "0", "-i", str(terms[name])], secs, out, size, caps.get(name), portrait, fade=0)
            tl = track[name]
        elif kind == "write":
            lst = work / "page.txt"
            lst.write_text("".join(f"file '{Path(f).resolve()}'\nduration {1 / 30:.4f}\n" for f in page["frames"])
                           + f"file '{Path(page['frames'][-1]).resolve()}'\n", encoding="utf-8")
            C.segment(["-f", "concat", "-safe", "0", "-i", str(lst)], secs, out, size, caps.get(name), portrait, fade=0)
            tl = track["write"]
        elif kind == "phone":
            comp = clips / "phone-raw.mp4"
            if portrait:  # 9:16: the PC on top, the phone below (a viewer is holding one of them)
                C.phone_stack(raw / "phone-pc.webm", raw / "phone-phone.webm", secs, comp, BG)
            else:
                C.phone_split(raw / "phone-pc.webm", raw / "phone-phone.webm", secs, comp, BG)
            C.segment(["-i", str(comp)], secs, out, size, caps.get(name), portrait, fade=0)
            tl = track["phone"]
        elif kind == "site" and "site" in cards:  # the page is missing: the stand-in card, held
            C.segment(["-i", str(cards["site"])], secs, out, size, caps.get(name), portrait, fade=0)
            tl = sfx.Timeline().at(0.9, "pop")
        else:  # site / web-market: a browser recording, cropped to the middle `secs` seconds
            src = raw / ("site.webm" if kind == "site" else "web-market.webm")
            lead = 0.0 if kind == "site" else 0.9
            C.segment(["-ss", str(lead), "-i", str(src)], secs, out, size, caps.get(name), portrait, fade=0)
            tl = track.get(name) or sfx.Timeline()
        parts.append(out)
        timelines.append(tl)

    video = work / "video.mp4"
    C.concat_xfade(parts, video, xfade=XFADE)
    durations = [C.probe(p)["duration"] for p in parts]
    total = round(sum(durations) - XFADE * (len(parts) - 1), 3)

    # every scene's cues go onto the film's one clock: shifted by the lengths (and overlaps) before it
    timeline = sfx.Timeline()
    for i, (d, tl) in enumerate(zip(durations, timelines)):
        shift = sum(durations[:i]) - XFADE * i
        for t, c, g in tl.events:
            timeline.at(shift + min(t, d), c, g)
    timeline.at(0.35, "pop", 0.4)  # the logo drops in on the intro card
    timeline.at(total - 1.6, "pop", 0.4)  # …and the outro's, just before the fade
    timeline.at(total - 0.9, "gone", 0.3)
    cues = sorted(timeline.events)
    OUT.mkdir(parents=True, exist_ok=True)
    timeline.save(OUT / f"me-{lang}-{aspect}.cues.txt")

    total = round(C.probe(video)["duration"], 3)  # the crossfades are set by ffmpeg: trust the picture, not the sum
    mixed = work / "mix.wav"
    keys = ["short"] if total <= 60 else ["main", "b", "c", "b"]  # one nocturne for a short film, a medley for a long one
    score = None
    if not os.environ.get("PROMO_NO_MUSIC") and sfx.ensure(score_len=total, quiet=False, keys=keys):
        score = sfx.fetch_score(need=total, keys=keys)
    sfx.mix(cues, total, mixed, music_wav=score, music_vol=float(os.environ.get("PROMO_MUSIC", 0.34)), duck_db=-8.0, level_db=-16.0)

    final = OUT / f"me-{lang}-{aspect}.mp4"
    C.ff("-i", str(video), "-i", str(mixed), "-map", "0:v", "-map", "1:a", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
         "-shortest", "-movflags", "+faststart", str(final))
    C.webp_preview(final, OUT / f"me-{lang}-{aspect}.webp")

    info = C.probe(final)
    ok = abs(info["duration"] - total) < 0.3
    scan_leaks(cast_files)
    report = {"file": final.name, "duration": round(info["duration"], 2), "expected": total, "ok": ok,
              "size": [info["width"], info["height"]], "music": score.name if score else None, "credits": sfx.credits(),
              "cues": len(cues), "scenes": [Path(p).name for p in parts],
              "write_scene": {"prompt": page["prompt"], "dur": round(page["dur"], 1), "keys": len(page["keys"]),
                              "words": len(page["words"])},
              "web_js_errors": marks.get("web_js_errors")}
    (OUT / f"me-{lang}-{aspect}.report.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), "utf-8")
    print(json.dumps(report, ensure_ascii=False))
    if not ok:
        raise SystemExit(f"{final.name}: duration {info['duration']:.2f}s, expected {total}s")
    return final


def rglob_quiet(root, name):
    """Files under `root` that exist now, skipping anything that vanished mid-walk (the agent's HOME is live)."""
    out = []
    for p in root.rglob(name):
        if p.is_file():
            out.append(p)
    return out


def scan_leaks(files):
    """No token, key or e-mail in what gets published: the cast logs are the one place raw output could leak them."""
    hits = []
    for f in files:
        text = Path(f).read_text("utf-8", "replace")
        for pat in R.SECRET_PATTERNS:
            if re.search(pat, text):
                hits.append(f"{f.name}: matches {pat}")
    if hits:
        raise SystemExit("secret scan failed:\n" + "\n".join(hits))


def build_all(tail=False, jobs=4):
    """Every variant as a separate process, `jobs` at a time; exits non-zero if any of them failed."""
    todo = [(lang, aspect) for lang in STRINGS for aspect in ("16x9", "9x16")]
    running, failed = {}, []
    while todo or running:
        while todo and len(running) < jobs:
            lang, aspect = todo.pop(0)
            cmd = [sys.executable, str(Path(__file__).resolve()), lang, aspect] + (["--tail"] if tail else [])
            p = subprocess.Popen(cmd)
            running[p] = f"{lang} {aspect}"
            print(f"▸ {lang} {aspect}: started (job {len(running)}/{jobs})", flush=True)
        for p in list(running):
            if p.poll() is None:
                continue
            name, code = running.pop(p), p.returncode
            print(("✓ " if not code else "✗ ") + f"{name} finished ({code})", flush=True)
            if code:
                failed.append(name)
    if failed:
        raise SystemExit("promo variants failed: " + ", ".join(failed))


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    args = [a for a in args if a]  # an empty argument from the shell means "no flag"
    if args == ["all"]:
        build_all(tail="--tail" in sys.argv, jobs=int(os.environ.get("PROMO_JOBS") or 4))
    elif len(args) == 2 and args[0] in STRINGS and args[1] in ("16x9", "9x16"):
        build(args[0], args[1], tail="--tail" in sys.argv)
    else:
        raise SystemExit("usage: full.py <ru|en> <16x9|9x16> [--tail]   |   full.py all [--tail]")
