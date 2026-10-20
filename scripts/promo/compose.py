"""ffmpeg helpers: every clip is normalised to H.264 (yuv420p, 30 fps, no audio) at the target size and an exact length,
so the final concat can use stream copy. Length rule: longer sources are cut, shorter ones hold their last frame."""
import subprocess
from pathlib import Path

LANDSCAPE, VERTICAL = (1920, 1080), (1080, 1920)
BG = "0x0e0f13"
VENC = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", "-r", "30", "-movflags", "+faststart", "-an"]


def ff(*args):
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args], check=True)


def probe(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height,r_frame_rate,nb_frames:format=duration",
                          "-of", "json", str(path)], check=True, capture_output=True, text=True).stdout
    import json
    d = json.loads(out)
    s = d["streams"][0]
    return {"width": s["width"], "height": s["height"], "fps": s["r_frame_rate"], "duration": float(d["format"]["duration"])}


def _fit_vf(size, seconds):
    w, h = size
    return (f"fps=30,scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color={BG},"
            f"tpad=stop_mode=clone:stop_duration={seconds + 10},format=yuv420p")


def still(png, seconds, out, size):
    ff("-loop", "1", "-framerate", "30", "-i", str(png), "-t", str(seconds), "-vf", _fit_vf(size, seconds), *VENC, str(out))


def fit_webm(src, seconds, out, size, skip=0.0):
    """skip: seconds at the start of the recording to drop (the page is still loading then)."""
    ff("-ss", str(skip), "-i", str(src), "-t", str(seconds), "-vf", _fit_vf(size, seconds), *VENC, str(out))


def fit_concat(listfile, seconds, out, size):
    ff("-f", "concat", "-safe", "0", "-i", str(listfile), "-t", str(seconds), "-vf", _fit_vf(size, seconds), *VENC, str(out))


def phone_split(pc_webm, phone_webm, seconds, out, bg=BG):
    """PC recording on the left (1280x720), the phone recording on the right (390x844 scaled to 780 px high)."""
    fg = ("[1:v]fps=30,tpad=stop_mode=clone:stop_duration=60,scale=1280:720[pc];"
          "[2:v]fps=30,tpad=stop_mode=clone:stop_duration=60,scale=-2:780[ph];"
          "[0:v][pc]overlay=40:180[a];[a][ph]overlay=1480:150,format=yuv420p[v]")
    ff("-f", "lavfi", "-i", f"color=c={bg}:s=1920x1080:r=30", "-i", str(pc_webm), "-i", str(phone_webm),
       "-filter_complex", fg, "-map", "[v]", "-t", str(seconds), *VENC, str(out))


def concat_xfade(parts, out, xfade=0.45):
    """Join scene clips with a crossfade (`parts` must all be the same size/fps). Audio is added later by sfx.mix,
    so the transitions stay picture-only and quick: keyframes are re-encoded once, here."""
    args, filt = [], []
    for p in parts:
        args += ["-i", str(p)]
    last = "[0:v]"
    for i in range(1, len(parts)):
        filt.append(f"{last}[{i}:v]xfade=transition=fade:duration={xfade}:offset=OFF{i}[v{i}]")
        last = f"[v{i}]"
    # xfade's offset is the moment the fade starts: accumulate the lengths minus the fades already spent
    off, offsets = 0.0, []
    for d in [probe(p)["duration"] for p in parts][:-1]:
        off += d - xfade
        offsets.append(max(0.0, off))
    filt = [f.replace(f"OFF{i}", f"{o:.3f}") for i, f in enumerate(filt, 1)]
    ff(*args, "-filter_complex", ";".join(filt), "-map", last, "-vf", "format=yuv420p", *VENC, str(out))


def concat_copy(parts, out):
    listfile = Path(out).with_suffix(".list.txt")
    listfile.write_text("".join(f"file '{Path(p).resolve()}'\n" for p in parts))
    ff("-f", "concat", "-safe", "0", "-i", str(listfile), "-c", "copy", "-movflags", "+faststart", str(out))
    listfile.unlink()


def webp_preview(mp4, out):
    """480 px high animated WebP, 12 fps — small enough to paste into a README or a release note."""
    ff("-i", str(mp4), "-vf", "fps=12,scale=-2:480:flags=lanczos", "-loop", "0", "-q:v", "55", "-an", str(out))


def phone_stack(pc_webm, phone_webm, seconds, out, bg=BG):
    """Portrait (9:16): PC recording on top (1080 px wide), the phone recording below, centred."""
    fg = ("[1:v]fps=30,tpad=stop_mode=clone:stop_duration=60,scale=1080:-2[pc];"
          "[2:v]fps=30,tpad=stop_mode=clone:stop_duration=60,scale=-2:1000[ph];"
          "[0:v][pc]overlay=0:170[a];[a][ph]overlay=(W-w)/2:800,format=yuv420p[v]")
    ff("-f", "lavfi", "-i", f"color=c={bg}:s=1080x1920:r=30", "-i", str(pc_webm), "-i", str(phone_webm),
       "-filter_complex", fg, "-map", "[v]", "-t", str(seconds), *VENC, str(out))


FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"


def segment(src_args, seconds, out, size, caption_file=None, portrait=False, fade=0.5):
    """Normalise one segment to exact length, add a `fade` s fade in/out (0 = cut hard into the next scene, which
    concat_xfade crossfades anyway) and (optionally) an animated lower-third caption.
    The caption rises 40 px and fades in over 0.6 s, and fades out over the last 0.5 s."""
    w, h = size
    vf = _fit_vf(size, seconds).replace(",format=yuv420p", "")
    if fade:
        vf += f",fade=t=in:st=0:d={fade},fade=t=out:st={seconds - fade:.2f}:d={fade}"
    if caption_file:
        fs, pad = (50, 260) if portrait else (46, 120)
        vf += (f",drawtext=fontfile={FONT}:textfile={caption_file}:fontsize={fs}:fontcolor=0xd8dae3:"
               f"box=1:boxcolor=0x111319@0.85:boxborderw=28:x=(w-text_w)/2:"
               f"y=h-th-{pad}+(1-min(t/0.6\\,1))*40:"
               f"alpha=min(t/0.4\\,1)*min(({seconds}-t)/0.5\\,1)")
    vf += ",format=yuv420p"
    ff(*src_args, "-t", str(seconds), "-vf", vf, *VENC, str(out))
