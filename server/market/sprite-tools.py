"""
name: sprite-tools
description: turn generated or hand-made art into what a game loads: background cutout, trim and fit into a frame size, pixelate to a palette, sprite strips, previews
icon: ▦
color: orange
requires: pillow
---
Learn the target first: frame size, orientation, pivot, palette, format. Open two or three of the game's own assets and match them. Pixel art: scale once with nearest neighbour to the final size, never twice. Painted art: use smooth=true. Always look at the result with preview before you ship it. Frames from a 3D model or from fal edits go through the same fit and then into strip.
Derived from universal-modder (MIT, Copyright (c) 2026 Rehan and universal-modder contributors): https://github.com/rehan-remade/universal-modder
"""
from pathlib import Path

from PIL import Image, ImageDraw

RESAMPLE = getattr(Image, "Resampling", Image)


def _load(path):
    p = Path(path).expanduser()
    if not p.is_file():
        raise ValueError(f"no such file: {p}")
    return Image.open(p).convert("RGBA")


def _save(im, dst):
    d = Path(dst).expanduser()
    d.parent.mkdir(parents=True, exist_ok=True)
    im.save(d)
    return str(d)


def _cut(im, tol):
    """Flood-fill from the border colour: background becomes transparent, interior whites stay."""
    w, h = im.size
    px = im.load()
    bg = px[0, 0][:3]
    seen = bytearray(w * h)
    stack = [(x, 0) for x in range(w)] + [(x, h - 1) for x in range(w)] + [(0, y) for y in range(h)] + [(w - 1, y) for y in range(h)]
    while stack:
        x, y = stack.pop()
        if x < 0 or y < 0 or x >= w or y >= h or seen[y * w + x]:
            continue
        seen[y * w + x] = 1
        p = px[x, y]
        if max(abs(p[0] - bg[0]), abs(p[1] - bg[1]), abs(p[2] - bg[2])) > tol:
            continue
        px[x, y] = (p[0], p[1], p[2], 0)
        stack += [(x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)]
    return im


@action
def info(ctx, path):
    "Size, mode, alpha coverage and corner colour of an image"
    im = _load(path)
    a = im.getchannel("A")
    transparent = sum(1 for v in a.getdata() if v < 8) / (im.width * im.height)
    return {"size": f"{im.width}x{im.height}", "transparent_share": round(transparent, 3), "corner": "#%02x%02x%02x" % im.getpixel((0, 0))[:3]}


@action
def cutout(ctx, src, dst, tolerance=24):
    "Make the border-coloured background transparent (flat backgrounds; tolerance per channel)"
    return _save(_cut(_load(src), int(tolerance)), dst)


@action
def fit(ctx, src, dst, width, height, smooth=False, anchor="center"):
    "Trim transparent margins, scale to fit inside width x height, place on a transparent frame (anchor: center or bottom)"
    im = _load(src)
    box = im.getchannel("A").point(lambda v: 255 if v >= 8 else 0).getbbox()
    if box:
        im = im.crop(box)
    k = min(int(width) / im.width, int(height) / im.height)
    nw, nh = max(1, round(im.width * k)), max(1, round(im.height * k))
    im = im.resize((nw, nh), RESAMPLE.LANCZOS if smooth else RESAMPLE.NEAREST)
    frame = Image.new("RGBA", (int(width), int(height)), (0, 0, 0, 0))
    x = (int(width) - nw) // 2
    y = int(height) - nh if anchor == "bottom" else (int(height) - nh) // 2
    frame.paste(im, (x, y), im)
    return _save(frame, dst)


@action
def pixelate(ctx, src, dst, width, height, colors=16):
    "Downscale to width x height and reduce to a palette of N colours, keeping alpha"
    im = _load(src)
    alpha = im.getchannel("A").resize((int(width), int(height)), RESAMPLE.NEAREST)
    small = im.convert("RGB").resize((int(width), int(height)), RESAMPLE.LANCZOS)
    q = small.quantize(colors=int(colors), method=getattr(Image, "Quantize", Image).MEDIANCUT).convert("RGBA")
    q.putalpha(alpha.point(lambda v: 255 if v >= 128 else 0))
    return _save(q, dst)


@action
def strip(ctx, folder, dst, vertical=True, pattern="*.png"):
    "Join same-size frames from a folder into one strip (vertical like Terraria, or horizontal)"
    files = sorted(Path(folder).expanduser().glob(pattern))
    if not files:
        raise ValueError(f"no frames matching {pattern} in {folder}")
    frames = [Image.open(f).convert("RGBA") for f in files]
    w, h = frames[0].size
    if any(f.size != (w, h) for f in frames):
        raise ValueError("frames must all be the same size; fit them first")
    out = Image.new("RGBA", (w, h * len(frames)) if vertical else (w * len(frames), h), (0, 0, 0, 0))
    for i, f in enumerate(frames):
        out.paste(f, (0, i * h) if vertical else (i * w, 0))
    ctx.log(f"{len(frames)} frames, {w}x{h} each")
    return _save(out, dst)


@action
def preview(ctx, src, dst, scale=6):
    "Zoomed copy on a checkerboard, so transparency and edges are visible"
    im = _load(src)
    s = int(scale)
    big = im.resize((im.width * s, im.height * s), RESAMPLE.NEAREST)
    board = Image.new("RGBA", big.size, (200, 200, 200, 255))
    d = ImageDraw.Draw(board)
    for y in range(0, big.height, 8 * s):
        for x in range(0, big.width, 8 * s):
            if (x // (8 * s) + y // (8 * s)) % 2:
                d.rectangle([x, y, x + 8 * s - 1, y + 8 * s - 1], fill=(150, 150, 150, 255))
    board.alpha_composite(big)
    return _save(board, dst)
