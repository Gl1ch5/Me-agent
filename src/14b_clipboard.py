# ───────────────────────────── clipboard ─────────────────────────────
# Native clipboard on every OS: text copy/paste and image paste. Windows uses the Win32 API (text) and PowerShell (images);
# macOS pbcopy/pbpaste/osascript; Linux wl-clipboard or xclip/xsel; Termux termux-api. OSC 52 is the fallback for copy
# (works over SSH and in Windows Terminal, iTerm, kitty, WezTerm…).


def _run_clip(argv, data=None, timeout=5, capture=True):
    """capture=False for copy tools: xclip/wl-copy fork a clipboard server that keeps any output pipe open."""
    try:
        out = subprocess.PIPE if capture else subprocess.DEVNULL
        r = subprocess.run(argv, input=data, stdout=out, stderr=out, timeout=timeout, creationflags=0x08000000 if WIN else 0)
        return r.returncode == 0, r.stdout or b""
    except (OSError, subprocess.SubprocessError):
        return False, b""


def _win_clip(text=None):
    """Win32 clipboard: set text (str) or get text (None)."""
    import ctypes
    from ctypes import wintypes as w
    u, k = ctypes.windll.user32, ctypes.windll.kernel32
    u.OpenClipboard.argtypes, u.GetClipboardData.restype, u.SetClipboardData.argtypes = [w.HWND], w.HANDLE, [w.UINT, w.HANDLE]
    k.GlobalAlloc.restype, k.GlobalLock.restype, k.GlobalLock.argtypes, k.GlobalUnlock.argtypes = w.HGLOBAL, w.LPVOID, [w.HGLOBAL], [w.HGLOBAL]
    for _ in range(10):  # another app may hold the clipboard for a moment
        if u.OpenClipboard(None):
            break
        time.sleep(0.03)
    else:
        return None
    try:
        if text is None:
            h = u.GetClipboardData(13)  # CF_UNICODETEXT
            if not h:
                return ""
            p = k.GlobalLock(h)
            try:
                return ctypes.wstring_at(p)
            finally:
                k.GlobalUnlock(h)
        data = text.replace("\r\n", "\n").replace("\n", "\r\n")
        buf = ctypes.create_unicode_buffer(data)
        size = ctypes.sizeof(buf)
        h = k.GlobalAlloc(0x0002, size)  # GMEM_MOVEABLE
        p = k.GlobalLock(h)
        ctypes.memmove(p, buf, size)
        k.GlobalUnlock(h)
        u.EmptyClipboard()
        return bool(u.SetClipboardData(13, h))
    finally:
        u.CloseClipboard()


def _linux_tool(*names):
    return next((n for n in names if shutil.which(n)), None)


def clip_copy(text, term=None):
    """Copy text to the system clipboard. Returns how it was done ("" if nothing worked)."""
    how = ""
    try:
        if WIN:
            how = "clipboard" if _win_clip(text) else ""
        elif sys.platform == "darwin":
            how = "pbcopy" if _run_clip(["pbcopy"], text.encode(), capture=False)[0] else ""
        elif os.environ.get("TERMUX_VERSION") and shutil.which("termux-clipboard-set"):
            how = "termux" if _run_clip(["termux-clipboard-set"], text.encode(), capture=False)[0] else ""
        elif os.environ.get("WAYLAND_DISPLAY") and shutil.which("wl-copy"):
            how = "wl-copy" if _run_clip(["wl-copy"], text.encode(), capture=False)[0] else ""
        elif os.environ.get("DISPLAY") and _linux_tool("xclip", "xsel"):
            t = _linux_tool("xclip", "xsel")
            how = t if _run_clip([t, "-selection", "clipboard"] if t == "xclip" else [t, "-bi"], text.encode(), capture=False)[0] else ""
    except Exception:
        how = ""
    if term is not None:  # OSC 52 too: reaches the local clipboard over SSH, and in terminals the native tools can't see
        term.write(f"{ESC}]52;c;{base64.b64encode(text.encode()).decode()}\a")
        how = how or "osc52"
    return how


def clip_paste():
    """Clipboard text, or None if it can't be read here."""
    try:
        if WIN:
            return _win_clip(None)
        if sys.platform == "darwin":
            ok, out = _run_clip(["pbpaste"])
        elif os.environ.get("TERMUX_VERSION") and shutil.which("termux-clipboard-get"):
            ok, out = _run_clip(["termux-clipboard-get"])
        elif os.environ.get("WAYLAND_DISPLAY") and shutil.which("wl-paste"):
            ok, out = _run_clip(["wl-paste", "-n"])
        elif os.environ.get("DISPLAY") and _linux_tool("xclip", "xsel"):
            t = _linux_tool("xclip", "xsel")
            ok, out = _run_clip([t, "-selection", "clipboard", "-o"] if t == "xclip" else [t, "-bo"])
        else:
            return None
        return out.decode("utf-8", "replace") if ok else None
    except Exception:
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
