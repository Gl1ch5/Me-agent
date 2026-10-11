"""
name: voice
kind: plugin
description: hold SPACE to dictate — Groq Whisper large v3 speech-to-text
icon: ◉
color: red
default: true
---
Set the Groq API key in the web account (Voice) or with /voice key.
Hold space (input empty or not) to record; release to transcribe into the input.
/voice also starts a recording; any key stops it.
Recorder: arecord / parecord / sox / ffmpeg on Linux & macOS, built-in MCI on Windows,
termux-microphone-record on Android (Termux:API).
"""
import json, os, shutil, signal, subprocess, tempfile, threading, time, uuid, urllib.request

S = {"rec": None, "t0": 0, "last": 0, "times": [], "busy": False, "path": None, "win": False}


def _cfg():
    return plugin.store.config.setdefault("voice", {"groq_key": "", "model": "whisper-large-v3", "language": ""})


def _key():
    k = _cfg().get("groq_key")
    if k:
        return k
    for p in plugin.store.config.get("providers", {}).values():
        if "groq.com" in p.get("base_url", "") and p.get("api_key"):
            return p["api_key"]
    return ""


def _recorder(path):
    if os.name == "nt":
        return None
    if os.environ.get("TERMUX_VERSION") and shutil.which("termux-microphone-record"):
        return ["termux"]
    cands = [
        ("arecord", ["arecord", "-q", "-f", "S16_LE", "-r", "16000", "-c", "1", path]),
        ("parecord", ["parecord", "--channels=1", "--rate=16000", "--file-format=wav", path]),
        ("rec", ["rec", "-q", "-c", "1", "-r", "16000", path]),
        ("ffmpeg", ["ffmpeg", "-loglevel", "quiet", "-y", "-f", "avfoundation" if os.uname().sysname == "Darwin" else "pulse",
                    "-i", ":0" if os.uname().sysname == "Darwin" else "default", "-ac", "1", "-ar", "16000", path]),
    ]
    for exe, cmd in cands:
        if shutil.which(exe):
            return cmd
    return None


def _mci(cmd):
    import ctypes
    buf = ctypes.create_unicode_buffer(256)
    ctypes.windll.winmm.mciSendStringW(cmd, buf, 255, 0)
    return buf.value


def start(tui):
    if S["rec"] or S["busy"]:
        return
    if not _key():
        tui.info("voice: set a Groq API key — /voice key (or in the web account)", "warn")
        return
    S["path"] = os.path.join(tempfile.gettempdir(), f"me-voice-{uuid.uuid4().hex[:6]}.wav")
    if os.name == "nt":
        _mci("open new type waveaudio alias mevoice")
        _mci("set mevoice bitspersample 16 samplespersec 16000 channels 1")
        _mci("record mevoice")
        S["rec"], S["win"] = "mci", True
    elif _recorder(S["path"]) == ["termux"]:  # Android: needs the Termux:API app + `pkg install termux-api`
        S["path"] = S["path"][:-4] + ".m4a"
        subprocess.run(["termux-microphone-record", "-q"], capture_output=True)
        subprocess.run(["termux-microphone-record", "-f", S["path"], "-l", "0", "-e", "aac"], capture_output=True)
        S["rec"] = "termux"
    else:
        cmd = _recorder(S["path"])
        if not cmd:
            tui.info("voice: no recorder found — install alsa-utils (arecord), sox or ffmpeg", "err")
            return
        S["rec"] = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    S["t0"] = S["last"] = time.time()
    tui.redraw()


def stop(tui):
    rec = S["rec"]
    if not rec:
        return
    S["rec"] = None
    if rec == "termux":
        subprocess.run(["termux-microphone-record", "-q"], capture_output=True)
        time.sleep(0.3)
    elif rec == "mci":
        _mci("stop mevoice")
        _mci(f'save mevoice "{S["path"]}"')
        _mci("close mevoice")
    else:
        rec.send_signal(signal.SIGINT)
        try:
            rec.wait(3)
        except subprocess.TimeoutExpired:
            rec.kill()
    if time.time() - S["t0"] < 0.4:
        return
    S["busy"] = True
    threading.Thread(target=_transcribe, args=(tui, S["path"]), daemon=True).start()


def _transcribe(tui, path):
    try:
        data = open(path, "rb").read()
        c = _cfg()
        b = uuid.uuid4().hex
        parts = []
        for k, v in (("model", c.get("model") or "whisper-large-v3"), ("response_format", "json"), ("language", c.get("language"))):
            if v:
                parts.append(f'--{b}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
        parts.append(f'--{b}\r\nContent-Disposition: form-data; name="file"; filename="voice{os.path.splitext(path)[1]}"\r\nContent-Type: application/octet-stream\r\n\r\n'.encode() + data + b"\r\n")
        parts.append(f"--{b}--\r\n".encode())
        req = urllib.request.Request("https://api.groq.com/openai/v1/audio/transcriptions", data=b"".join(parts), method="POST",
                                     headers={"Authorization": "Bearer " + _key(), "Content-Type": f"multipart/form-data; boundary={b}",
                                              "User-Agent": "me-voice"})
        text = json.loads(urllib.request.urlopen(req, timeout=60).read()).get("text", "").strip()
        if text:
            with tui.lock:
                pre = "" if not tui.input or tui.input[: tui.cur].endswith((" ", "\n")) else " "
                tui.insert(pre + text)
        else:
            tui.toast("voice: nothing heard", "warn")
    except Exception as e:
        tui.info(f"voice: {e}", "err")
    finally:
        S["busy"] = False
        try:
            os.unlink(path)
        except OSError:
            pass
        tui.redraw()


@plugin.on("key")
def on_key(tui, k):
    now = time.time()
    if S["rec"]:
        if k == ("char", " "):
            S["last"] = now
            return True
        stop(tui)
        return True
    if k != ("char", " "):
        S["times"] = []
        return None
    S["times"] = [t for t in S["times"] if now - t < 0.9] + [now]
    t = S["times"]
    if len(t) >= 3 and t[-1] - t[-2] < 0.15 and t[-2] - t[-3] < 0.15:
        n = len(t) - 1  # spaces already typed by this hold
        while n and tui.cur and tui.input[tui.cur - 1] == " ":
            tui.input = tui.input[: tui.cur - 1] + tui.input[tui.cur:]
            tui.cur -= 1
            n -= 1
        S["times"] = []
        start(tui)
        return True
    return None


@plugin.on("tick")
def on_tick(tui):
    if S["rec"] and S["rec"] != "manual" and time.time() - S["last"] > 0.45 and not S.get("manual"):
        stop(tui)
        return True
    return True if S["rec"] or S["busy"] else None


@plugin.on("status")
def on_status(tui):
    if S["rec"]:
        on = int(time.time() * 2) % 2
        return [("● " if on else "○ ", "err"), (f"REC {time.time() - S['t0']:.1f}s", "err")]
    if S["busy"]:
        return [("◌ transcribing…", "accent")]
    return None


@plugin.command("voice", "dictate (or: /voice key) — hold space works too")
def voice_cmd(tui, arg):
    if arg.strip() == "key" or not _key():
        k = tui.prompt("Groq API key (console.groq.com/keys)", _cfg().get("groq_key", ""), secret=True)
        if k is not None:
            _cfg()["groq_key"] = k.strip()
            plugin.store.save()
            tui.toast("voice key saved", "ok")
        return
    S["manual"] = True
    start(tui)
    tui.toast("recording — press any key to stop", "err")
    while S["rec"]:
        time.sleep(0.1)
    S["manual"] = False
