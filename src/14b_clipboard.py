# ───────────────────────────── clipboard ─────────────────────────────
# Native clipboard on every OS: text copy/paste and image paste. Windows uses the Win32 API (text) and PowerShell (images);
# macOS pbcopy/pbpaste/osascript; Linux wl-clipboard or xclip/xsel; Termux termux-api; WSL PowerShell. OSC 52 when nothing
# native works and over SSH (a bare OSC 52 cannot be verified, and the classic Windows console ignores it).


def _run_clip(argv, data=None, timeout=5, capture=True):
    """capture=False for copy tools: xclip/wl-copy fork a clipboard server that keeps any output pipe open."""
    try:
        out = subprocess.PIPE if capture else subprocess.DEVNULL
        r = subprocess.run(argv, input=data, stdout=out, stderr=out, timeout=timeout, **NO_WINDOW)
        return r.returncode == 0, r.stdout or b""
    except (OSError, subprocess.SubprocessError):
        return False, b""


def _win_clip_api():
    import ctypes
    u, k = ctypes.windll.user32, ctypes.windll.kernel32
    vp = ctypes.c_void_p
    k.GlobalAlloc.restype, k.GlobalAlloc.argtypes = vp, [ctypes.c_uint, ctypes.c_size_t]  # 64-bit handles must not be truncated
    k.GlobalLock.restype, k.GlobalLock.argtypes = vp, [vp]
    k.GlobalUnlock.argtypes = k.GlobalFree.argtypes = [vp]
    u.SetClipboardData.restype, u.SetClipboardData.argtypes = vp, [ctypes.c_uint, vp]
    u.GetClipboardData.restype, u.GetClipboardData.argtypes = vp, [ctypes.c_uint]
    u.OpenClipboard.argtypes = [vp]
    for _ in range(20):  # another app may hold the clipboard for a moment
        if u.OpenClipboard(None):
            return ctypes, u, k
        time.sleep(0.025)
    return None


def _win_clip(text=None):
    """Win32 clipboard: set text (str) → bool, or get text (None) → str / None."""
    api = _win_clip_api()
    if not api:
        return None
    ctypes, u, k = api
    try:
        if text is None:
            h = u.GetClipboardData(13)  # CF_UNICODETEXT
            if not h:
                return ""
            p = k.GlobalLock(h)
            try:
                return ctypes.wstring_at(p) if p else ""
            finally:
                k.GlobalUnlock(h)
        data = text.replace("\r\n", "\n").replace("\n", "\r\n").encode("utf-16-le", "surrogatepass") + b"\0\0"
        u.EmptyClipboard()
        h = k.GlobalAlloc(0x0002, len(data))  # GMEM_MOVEABLE
        if not h:
            return False
        p = k.GlobalLock(h)
        ctypes.memmove(p, data, len(data))
        k.GlobalUnlock(h)
        if not u.SetClipboardData(13, h):  # on success the system owns the memory
            k.GlobalFree(h)
            return False
        return True
    finally:
        u.CloseClipboard()


def _is_wsl():
    try:
        return "microsoft" in Path("/proc/version").read_text().lower()
    except OSError:
        return False


def clip_copy(text, term=None):
    """Put text on the system clipboard. `term` (a Term or a write function) gets an OSC 52 copy when nothing native worked or
    over SSH. Returns how: "native", "osc52" or "" when nothing worked."""
    env = os.environ
    remote = bool(env.get("SSH_CONNECTION") or env.get("SSH_TTY"))
    ok = False
    try:
        if WIN:
            ok = bool(_win_clip(text))
        elif not remote:
            data = text.encode("utf-8")
            cmds = []
            if sys.platform == "darwin":
                cmds.append(["pbcopy"])
            if env.get("TERMUX_VERSION"):
                cmds.append(["termux-clipboard-set"])
            if env.get("WAYLAND_DISPLAY"):
                cmds.append(["wl-copy"])
            if env.get("DISPLAY"):
                cmds += [["xclip", "-selection", "clipboard"], ["xsel", "--clipboard", "--input"]]
            ok = any(shutil.which(c[0]) and _run_clip(c, data, capture=False)[0] for c in cmds)
            if not ok and _is_wsl() and not env.get("WT_SESSION") and shutil.which("powershell.exe"):
                # clip.exe and PowerShell stdin decode pipes in the console code page: hand the text over in a UTF-8 file
                fd, tmp = tempfile.mkstemp(suffix=".txt")
                try:
                    with os.fdopen(fd, "wb") as f:
                        f.write(data)
                    ok_p, wp = _run_clip(["wslpath", "-w", tmp])
                    wp = wp.decode().strip()
                    if ok_p and wp:
                        ps = f"Set-Clipboard -Value ([IO.File]::ReadAllText('{wp}', [Text.Encoding]::UTF8))"
                        ok = _run_clip(["powershell.exe", "-NoProfile", "-Command", ps], timeout=10)[0]
                finally:
                    os.unlink(tmp)
    except Exception:
        ok = False
    if ok and not remote:
        return "native"
    write = getattr(term, "write", term)
    if write:
        b64 = base64.b64encode(text.encode("utf-8")).decode()
        if len(b64) <= 100_000:
            write(f"{ESC}]52;c;{b64}\a")
            return "osc52"
    return "native" if ok else ""


def clip_paste():
    """Clipboard text, or None if it can't be read here."""
    env = os.environ
    try:
        if WIN:
            t = _win_clip(None)
            return t.replace("\r\n", "\n") if t is not None else None
        cmds = []
        if sys.platform == "darwin":
            cmds.append(["pbpaste"])
        if env.get("TERMUX_VERSION"):
            cmds.append(["termux-clipboard-get"])
        if env.get("WAYLAND_DISPLAY"):
            cmds.append(["wl-paste", "--no-newline"])
        if env.get("DISPLAY"):
            cmds += [["xclip", "-selection", "clipboard", "-o"], ["xsel", "--clipboard", "--output"]]
        if _is_wsl():
            cmds.append(["powershell.exe", "-NoProfile", "-Command", "[Console]::OutputEncoding=[Text.Encoding]::UTF8; Get-Clipboard -Raw"])
        for c in cmds:
            if shutil.which(c[0]):
                ok, out = _run_clip(c)
                if ok:
                    return out.decode("utf-8", "replace").replace("\r\n", "\n")
    except Exception:
        pass
    return None


WIN_CLIP_IMAGE = r"""
Add-Type -AssemblyName System.Windows.Forms, System.Drawing
$out = $args[0]
$img = [System.Windows.Forms.Clipboard]::GetImage()
if (-not $img) {
  $files = [System.Windows.Forms.Clipboard]::GetFileDropList()
  foreach ($f in $files) { if ($f -match '\.(png|jpe?g|gif|bmp|webp)$') { $img = [System.Drawing.Image]::FromFile($f); break } }
}
if (-not $img) { exit 3 }
$max = 2000; $w = $img.Width; $h = $img.Height
if ([Math]::Max($w, $h) -gt $max) {
  $k = $max / [Math]::Max($w, $h); $bmp = New-Object System.Drawing.Bitmap ([int]($w * $k)), ([int]($h * $k))
  $g = [System.Drawing.Graphics]::FromImage($bmp); $g.InterpolationMode = 'HighQualityBicubic'
  $g.DrawImage($img, 0, 0, $bmp.Width, $bmp.Height); $g.Dispose(); $img = $bmp
}
$img.Save($out, [System.Drawing.Imaging.ImageFormat]::Png)
"""


def clip_image(dest_dir):
    """Save an image from the clipboard as PNG in dest_dir. Returns the path, or None (no image / unsupported)."""
    Path(dest_dir).mkdir(parents=True, exist_ok=True)
    out = Path(dest_dir) / time.strftime("paste-%Y%m%d-%H%M%S.png")
    try:
        if WIN:
            script = Path(tempfile.gettempdir()) / "me-clip-image.ps1"
            script.write_text(WIN_CLIP_IMAGE, "utf-8")
            ps = shutil.which("powershell") or shutil.which("pwsh") or "powershell"
            ok, _ = _run_clip([ps, "-NoProfile", "-NonInteractive", "-STA", "-ExecutionPolicy", "Bypass", "-File", str(script), str(out)], timeout=15)
        elif sys.platform == "darwin":
            if shutil.which("pngpaste"):
                ok, _ = _run_clip(["pngpaste", str(out)])
            else:
                osa = ('set f to (open for access POSIX file "%s" with write permission)\n'
                       'try\nwrite (the clipboard as «class PNGf») to f\nend try\nclose access f') % str(out).replace('"', '\\"')
                ok, _ = _run_clip(["osascript", "-e", osa])
        elif os.environ.get("WAYLAND_DISPLAY") and shutil.which("wl-paste"):
            ok, types = _run_clip(["wl-paste", "--list-types"])
            mime = next((t for t in types.decode("utf-8", "replace").split() if t.startswith("image/")), None)
            ok, data = _run_clip(["wl-paste", "--type", mime], timeout=10) if mime else (False, b"")
            if ok and data:
                out = out.with_suffix("." + mime.split("/")[1].replace("jpeg", "jpg"))
                out.write_bytes(data)
        elif os.environ.get("DISPLAY") and shutil.which("xclip"):
            ok, types = _run_clip(["xclip", "-selection", "clipboard", "-t", "TARGETS", "-o"])
            mime = next((t for t in types.decode("utf-8", "replace").split() if t.startswith("image/")), None)
            ok, data = _run_clip(["xclip", "-selection", "clipboard", "-t", mime, "-o"], timeout=10) if mime else (False, b"")
            if ok and data:
                out = out.with_suffix("." + mime.split("/")[1].replace("jpeg", "jpg"))
                out.write_bytes(data)
        else:
            return None
    except Exception:
        return None
    return str(out) if ok and out.exists() and out.stat().st_size > 0 else None


def desktop_notify(title, body=""):
    """Best effort system notification (Linux notify-send, macOS, Windows balloon, Termux). Never raises, never blocks the UI."""
    try:
        if os.environ.get("TERMUX_VERSION") and shutil.which("termux-notification"):
            _run_clip(["termux-notification", "-t", title, "-c", body or title], capture=False)
        elif sys.platform == "darwin":
            q = lambda s: s.replace("\\", "\\\\").replace('"', '\\"')
            _run_clip(["osascript", "-e", f'display notification "{q(body)}" with title "{q(title)}"'], capture=False)
        elif WIN:
            ps = ("Add-Type -AssemblyName System.Windows.Forms; $n = New-Object System.Windows.Forms.NotifyIcon; "
                  "$n.Icon = [System.Drawing.SystemIcons]::Information; $n.Visible = $true; "
                  "$n.ShowBalloonTip(5000, $env:ME_N_TITLE, $env:ME_N_BODY, 'Info'); Start-Sleep 6; $n.Dispose()")
            env = dict(os.environ, ME_N_TITLE=title, ME_N_BODY=body or title)
            subprocess.Popen([shutil.which("powershell") or "powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", ps], env=env,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **NO_WINDOW)
        elif shutil.which("notify-send") and (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
            _run_clip(["notify-send", "-a", "me", title, body], capture=False)
    except Exception:
        pass


def image_paths(text):
    """A paste that is just image file path(s) — what dragging files into a terminal produces — → those paths."""
    text = text.strip()
    if not text or "\n" in text.strip() and len(text) > 4000:
        return []
    parts = re.findall(r'"([^"]+)"|\'([^\']+)\'|((?:\\ |\S)+)', text)
    out = []
    for a, b, c in parts:
        p = (a or b or c).replace("\\ ", " ")
        if p.startswith("file://"):
            import urllib.parse as up
            p = up.unquote(up.urlparse(p).path)
            if WIN and re.match(r"^/[A-Za-z]:", p):
                p = p[1:]
        if Path(p).suffix.lower() not in IMAGE_EXT or not os.path.isfile(os.path.expanduser(p)):
            return []
        out.append(os.path.abspath(os.path.expanduser(p)))
    return out
