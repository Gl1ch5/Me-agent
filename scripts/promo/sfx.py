"""Sound design for the promos: the UI sound pack, the classical score, and the timeline of cues that mixes both.

Sources (licences and credits live in docs/promo/SFX-PACK.md; `check_pack` refuses a sample whose licence text is gone):
- ui pack — "UI Sound Pack 1" by ViRiX (David McKee), free for any use with credit. Eleven WAVs from
  opengameart.org/sites/default/files/UI%20pack%201.zip; the transients are trimmed here into short 44.1 kHz mono wavs.
- score — real piano recordings, CC0: the Musopen Chopin collection on archive.org (identifier musopen-chopin).
  The first ~24 s of each piece is cut out (a calm, sparse spot) and crossfaded with the next piece, so the
  music keeps moving without a single hard edit. Short promos use one piece and start at its beginning.

Everything is optional: `ensure(need_score=True)` returns None if neither network nor pack is there, and the video
is then built without sound instead of failing.
"""
import hashlib, json, shutil, subprocess, sys, urllib.parse, urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
SFX_DIR = HERE / "sfx"                       # downloaded samples (git-ignored)
SCORE_DIR = HERE / "score"                   # downloaded pieces (git-ignored)
PACK = SFX_DIR / "pack.json"                 # what is in sfx/, with the licence of every file
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"

IA = "https://archive.org/download/musopen-chopin/"
CREDIT = ("Chopin: Musopen (musopen.org, CC0) · UI sounds: ViRiX / David McKee (soundcloud.com/virix)")
UI_PACK = "https://opengameart.org/sites/default/files/UI%20pack%201.zip"
UI_LICENCE = "free for commercial and non-commercial use; credit: \"Some of the sounds in this project were created by David McKee (ViRiX) soundcloud.com/virix\""

# cue → (file in the UI pack, start s, length s). Trimmed around the transient so cues land on the frame.
UI = {
    "key":     ("MENU_Pick.wav", 0.00, 0.045),
    "click":   ("MENU A_Select.wav", 0.00, 0.16),
    "pick":    ("MENU B_Select.wav", 0.00, 0.22),
    "back":    ("MENU B_Back.wav", 0.00, 0.20),
    "msg":     ("MESSAGE-B_Accept.wav", 0.00, 0.42),
    "pop":     ("ALERT_Appear.wav", 0.00, 0.30),
    "gone":    ("ALERT_Dissappear.wav", 0.00, 0.26),
    "error":   ("ALERT_Error.wav", 0.00, 0.45),
    "now":     ("ETRA.wav", 0.00, 0.40),
    "tick":    ("MENU_Pick.wav", 0.00, 0.035),
}
# piece → (file on archive.org, start s, length s, title for the credits)
SCORE = {
    "main":  ("NocturneOp9No2.mp3", 0.0, 30.0, "Chopin — Nocturne Op. 9 no. 2"),
    "b":     ("Nocturne Op. 27 no. 1 in C sharp minor.mp3", 24.0, 28.0, "Chopin — Nocturne Op. 27 no. 1"),
    "c":     ("Nocturne Op. 48 no. 2 in F sharp minor.mp3", 24.0, 28.0, "Chopin — Nocturne Op. 48 no. 2"),
    "short": ("NocturneOp9No2.mp3", 0.0, 26.0, "Chopin — Nocturne Op. 9 no. 2"),
}


def ff(*args):
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args], check=True)


def online(url, timeout=10):
    try:
        urllib.request.urlopen(urllib.request.Request(url, method="HEAD"), timeout=timeout).close()
        return True
    except Exception:
        return False


def _get(url, dst):
    tmp = Path(str(dst) + ".part")
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "me-promo"}), timeout=300) as r, open(tmp, "wb") as f:
        shutil.copyfileobj(r, f)
    tmp.replace(dst)


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()[:16]


# ── the pack ──────────────────────────────────────────────────────────────────────────────────────────

def fetch_sfx(force=False):
    """Download the UI pack and trim every cue into sfx/<cue>.wav. Writes pack.json with the licences."""
    SFX_DIR.mkdir(parents=True, exist_ok=True)
    if force or not all((SFX_DIR / f"{c}.wav").exists() for c in UI):
        zip_path = SFX_DIR / "UI pack 1.zip"
        if force or not zip_path.exists():
            _get(UI_PACK, zip_path)
        raw = SFX_DIR / "raw"
        raw.mkdir(exist_ok=True)
        import zipfile
        with zipfile.ZipFile(zip_path) as z:
            z.extractall(raw)
        (raw / "LICENCE.txt").write_text(f"UI Sound Pack 1 — ViRiX (David McKee).\n{UI_LICENCE}\nSource: {UI_PACK}\n", "utf-8")
        for cue, (name, start, length) in UI.items():
            ff("-ss", f"{start:.3f}", "-t", f"{length:.3f}", "-i", str(raw / name), "-ac", "1", "-ar", "44100",
               "-af", "afade=t=in:d=0.004,afade=t=out:st=%.3f:d=0.03" % max(0.0, length - 0.03), str(SFX_DIR / f"{cue}.wav"))
    pack = {"sources": {c: {"file": n, "trim": [s, l]} for c, (n, s, l) in UI.items()},
            "sha256_16": {c: _sha(SFX_DIR / f"{c}.wav") for c in UI if (SFX_DIR / f"{c}.wav").exists()},
            "licence": {"ui_pack": {"title": "UI Sound Pack 1", "author": "ViRiX (David McKee)", "url": UI_PACK, "terms": UI_LICENCE}}}
    PACK.write_text(json.dumps(pack, ensure_ascii=False, indent=1), "utf-8")
    return pack


def check_pack():
    """The samples on disk must be the ones the licence file describes (a trimmed cue is a few KB: no reason to guess)."""
    if not PACK.exists():
        return None
    pack = json.loads(PACK.read_text("utf-8"))
    bad = [c for c in UI if not (SFX_DIR / f"{c}.wav").exists()]
    if bad:
        raise SystemExit("sfx pack is incomplete: " + ", ".join(bad) + " — run: python3 scripts/promo/sfx.py")
    for c, h in pack.get("sha256_16", {}).items():
        if _sha(SFX_DIR / f"{c}.wav") != h:
            raise SystemExit(f"sfx/{c}.wav changed since it was trimmed from {UI[c][0]} — re-run: python3 scripts/promo/sfx.py --force")
    return pack


def cue(name):
    return SFX_DIR / f"{name}.wav"


# ── the score ────────────────────────────────────────────────────────────────────────────────────────

def fetch_score(need=96.0, force=False, keys=None):
    """Cut the score out of the pieces and write score/<name>.wav, at least `need` seconds long.
    `keys`: which pieces, in order — ["short"] is one piece from its first bar, ["main","b","c","b"] a 100 s medley."""
    SCORE_DIR.mkdir(parents=True, exist_ok=True)
    keys = keys or (["main", "b", "c"] if need <= 82 else ["main", "b", "c", "b"])
    name = "score" if keys == ["main", "b", "c", "b"] else "score-" + "-".join(keys)
    out = SCORE_DIR / (name + ".wav")
    if not force and out.exists():
        return out
    for key in dict.fromkeys(keys):
        src = SCORE_DIR / (key + ".mp3")
        f = SCORE[key]
        if force or not src.exists():
            _get(IA + urllib.parse.quote(f[0]), src)
        part = SCORE_DIR / f"{key}.wav"
        if force or not part.exists():
            ff("-ss", str(f[1]), "-i", str(src), "-t", str(f[2]), "-ac", "2", "-ar", "44100",
               "-af", "highpass=f=70,afade=t=in:d=0.8,afade=t=out:st=%.2f:d=1.6" % (f[2] - 1.6), str(part))
    parts = [SCORE_DIR / f"{k}.wav" for k in keys]
    args, filt, last = [], [], "[0:a]"
    for p in parts:
        args += ["-i", str(p)]
    for i in range(1, len(parts)):
        filt.append(f"{last}[{i}:a]acrossfade=d=2.5:c1=tri:c2=tri[x{i}]")
        last = f"[x{i}]"
    if filt:  # no jump at the seams: one continuous performance
        ff(*args, "-filter_complex", ";".join(filt), "-map", last, "-t", str(need), str(out))
    else:
        shutil.copy(parts[0], out)
    (SCORE_DIR / (name + ".json")).write_text(json.dumps([SCORE[k][3] for k in keys], ensure_ascii=False), "utf-8")
    return out


def credits(name="score"):
    p = SCORE_DIR / (name + ".json")
    pieces = json.loads(p.read_text("utf-8")) if p.exists() else []
    return " · ".join(pieces) + " (Musopen, CC0) · UI: ViRiX (David McKee)" if pieces else CREDIT


# ── the timeline ─────────────────────────────────────────────────────────────────────────────────────

CUE_GAIN = {"key": 0.10, "tick": 0.05, "click": 0.42, "pick": 0.34, "back": 0.26, "msg": 0.55, "pop": 0.50,
            "gone": 0.24, "error": 0.5, "now": 0.5}


class Timeline:
    """What happened on screen, in scene time: typing ticks, clicks, the finished file. Mixed over the music."""

    def __init__(self):
        self.events = []  # (t, cue, gain)

    def at(self, t, cue, gain=None):
        self.events.append((max(0.0, float(t)), cue, CUE_GAIN.get(cue, 0.4) if gain is None else gain))
        return self

    def typing(self, t0, t1, count, cue="key", per_sec=13):
        """Ticks for `count` keystrokes spread over [t0, t1]; sparser than the real typing, like an audio typewriter."""
        if count <= 0 or t1 <= t0:
            return self
        n = max(1, min(count, int((t1 - t0) * per_sec)))
        for i in range(n):
            self.at(t0 + (t1 - t0) * (i + 0.5) / n, cue)
        return self

    def save(self, path):
        path = Path(path)
        path.write_text("\n".join(f"{t:.3f}\t{c}\t{g}" for t, c, g in sorted(self.events)) + "\n", "utf-8")
        return path


def mix(cues, seconds, out, music_wav=None, music_vol=0.30, duck_db=-7.0, level_db=None):
    """Sum the cues onto the score, which fades in over 1.5 s and out over the last 2.5 s and dips `duck_db` while
    cues play (sidechain compression), so a click or a chime always lands on top of the music."""
    out = Path(out)
    n = len(cues)
    if not n:
        if music_wav:
            ff("-i", str(music_wav), "-t", str(seconds), "-af", f"volume={music_vol},afade=t=in:d=1.5,afade=t=out:st={seconds - 2.5:.2f}:d=2.5", str(out))
        else:
            ff("-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo", "-t", str(seconds), str(out))
        return out
    inputs, labels, filt = [], [], []
    for i, (t, c, g) in enumerate(cues):
        inputs += ["-i", str(cue(c))]
        delay = int(max(0.0, t) * 1000)
        filt.append(f"[{i}:a]atrim=0:1,asetpts=N/SR/TB,adelay={delay}|{delay},volume={g}[c{i}]")
        labels.append(f"[c{i}]")
    filt.append(f"{''.join(labels)}amix=inputs={n}:normalize=0:dropout_transition=0[fx]")
    if music_wav:
        # sidechain: the music is ducked by the cue bus, so words and clicks cut through without a static ratio
        filt.append("[fx]asplit=2[fx1][fx2]")
        filt.append("[fx2]aformat=channel_layouts=stereo,volume=2.2[sc]")
        filt.append("[%d:a]volume=%s,afade=t=in:d=1.5,afade=t=out:st=%.2f:d=2.5,"
                    "lowpass=f=14000[mus]" % (n, music_vol, seconds - 2.5))
        filt.append(f"[mus][sc]sidechaincompress=threshold=0.02:ratio={2 ** (-duck_db / 6):.2f}:attack=8:release=320:makeup=1[mus2]")
        filt.append("[mus2][fx1]amix=inputs=2:normalize=0:dropout_transition=0[mix]")
        filt.append(f"[mix]atrim=0:{seconds},alimiter=limit=0.95:level=false[out]")
        inputs += ["-i", str(music_wav)]
    else:
        filt.append(f"[fx]atrim=0:{seconds},alimiter=limit=0.95:level=false[out]")
    ff(*inputs, "-filter_complex", ";".join(filt), "-map", "[out]", "-ac", "2", "-ar", "44100", str(out))
    if level_db is not None:
        ff("-i", str(out), "-af", f"loudnorm=I={level_db}:TP=-1.5:LRA=11", str(out.with_suffix(".norm.wav")))
        out.with_suffix(".norm.wav").replace(out)
    return out


def ensure(score_len=96.0, quiet=True, keys=None):
    """Make sure the pack and the score are on disk. Returns True if both are ready (False when offline and not cached)."""
    ok = True
    try:
        check_pack()
    except SystemExit as e:  # no pack yet, or a sample edited by hand: fetch it again
        if not quiet:
            print("sfx:", str(e).splitlines()[0], file=sys.stderr)
        try:
            fetch_sfx()
        except Exception as e:
            print("sfx: could not download the UI pack:", e, file=sys.stderr)
            return False
    try:
        fetch_score(score_len, keys=keys)
    except Exception as e:
        print("sfx: could not prepare the score:", e, file=sys.stderr)
        ok = False
    return ok


if __name__ == "__main__":
    force = "--force" in sys.argv
    if force:
        shutil.rmtree(SFX_DIR, ignore_errors=True)
        shutil.rmtree(SCORE_DIR, ignore_errors=True)
    fetch_sfx(force)
    print(json.dumps({"sfx": sorted((p.name, p.stat().st_size) for p in SFX_DIR.glob("*.wav")),
                      "score": str(fetch_score(force=force)), "credits": credits()}, ensure_ascii=False, indent=1))
