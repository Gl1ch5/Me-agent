"""
name: fal-assets
description: generate game assets with fal (fal.ai): sprites with transparent background, concept images, sound effects, plus price checks. Every output is saved with a manifest line
icon: ✦
color: magenta
env: FAL_KEY
---
Needs secret FAL_KEY (/secret, keys at https://fal.ai/dashboard/keys). Generation is billed to that account: price first with price(endpoint), tell the user the rough cost for more than about 20 items, and iterate cheap before the final run. generate asks for confirmation every time. Input fields differ per model: when a call fails with a validation error, read the model's page on fal.ai and pass the right fields in the JSON input. Credit fal and the model in the mod's README. Check each model's license for commercial use.
Derived from universal-modder (MIT, Copyright (c) 2026 Rehan and universal-modder contributors): https://github.com/rehan-remade/universal-modder
"""
import json, mimetypes, re, time, urllib.parse, urllib.request
from pathlib import Path

QUEUE = "https://queue.fal.run"
PLATFORM = "https://api.fal.ai/v1"
STYLE = {"pixel": "16-bit pixel art game sprite, crisp dark outline, limited palette, centered, plain flat white background, no shadow, no text"}
RECIPES = {
    "sprite": ("openai/gpt-image-2", {"background": "transparent"}),
    "image": ("fal-ai/nano-banana-2", {}),
    "sfx": ("fal-ai/elevenlabs/sound-effects/v2", {}),
    "rmbg": ("fal-ai/birefnet/v2", {}),
}
EXT = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp", "audio/mpeg": ".mp3", "audio/wav": ".wav",
       "audio/x-wav": ".wav", "video/mp4": ".mp4", "model/gltf-binary": ".glb"}


def _key(ctx):
    k = ctx.env.get("FAL_KEY", "")
    if not k:
        raise ValueError("FAL_KEY is not set: add it with /secret FAL_KEY (keys at https://fal.ai/dashboard/keys)")
    return k


def _auth(ctx):
    return {"Authorization": "Key " + _key(ctx)}


def _urls(obj, trail=""):
    if isinstance(obj, dict):
        u = obj.get("url")
        if isinstance(u, str) and u.startswith("http"):
            yield trail, u, obj.get("content_type") or ""
        for k, v in obj.items():
            if k != "url":
                yield from _urls(v, f"{trail}.{k}" if trail else k)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from _urls(v, f"{trail}[{i}]")


def _download(url, dest, ctype=""):
    req = urllib.request.Request(url, headers={"User-Agent": "me-skill"})
    with urllib.request.urlopen(req, timeout=600) as r:
        data = r.read()
        ctype = (r.headers.get_content_type() if r.headers else "") or ctype
    ext = Path(urllib.parse.urlparse(url).path).suffix or EXT.get(ctype) or mimetypes.guess_extension(ctype) or ""
    path = dest.parent / (dest.name + ext)
    path.write_bytes(data)
    return path


@action
def price(ctx, endpoint):
    "Price of a fal model endpoint (free call)"
    return ctx.http.get(f"{PLATFORM}/models/pricing?" + urllib.parse.urlencode({"endpoint_id": endpoint}), _auth(ctx))


@action(confirm=True)
def generate(ctx, endpoint, input="{}", name="asset", out="assets/gen"):
    "Run a fal model with a JSON input, wait for it, and save every output file plus a manifest line"
    body = json.loads(input) if isinstance(input, str) else dict(input)
    headers = _auth(ctx)
    job = ctx.http.post(f"{QUEUE}/{endpoint}", body, headers)
    rid = job.get("request_id")
    status_url = job.get("status_url") or f"{QUEUE}/{endpoint}/requests/{rid}/status"
    response_url = job.get("response_url") or f"{QUEUE}/{endpoint}/requests/{rid}"
    t0, last = time.time(), None
    while True:
        st = ctx.http.get(status_url + "?logs=1", headers)
        s = st.get("status")
        if s != last:
            ctx.status(f"{endpoint}: {s}")
            last = s
        if s == "COMPLETED":
            if st.get("error"):
                raise RuntimeError(f"{endpoint} failed: {st['error']}")
            break
        if time.time() - t0 > 1800:
            raise TimeoutError(f"{endpoint} still {s} after 30 min (request {rid}); fetch it later with request id {rid}")
        time.sleep(1.5 if time.time() - t0 < 30 else 4)
    result = ctx.http.get(response_url, headers)
    out = Path(out).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    files, used, first_key = [], set(), None
    for trail, url, ctype in _urls(result):
        key = re.sub(r"\[\d+\]", "", trail).split(".")[-1] or "file"
        idx = re.findall(r"\[(\d+)\]", trail)
        first_key = first_key or key
        # the first output gets the plain name (images[0] -> name, images[1] -> name_2); other outputs get name_<key>
        stem = name if key == first_key else f"{name}_{key}"
        if idx and idx[-1] != "0":
            stem += f"_{int(idx[-1]) + 1}"
        n, final = 1, stem
        while final in used:
            n += 1
            final = f"{stem}_{n}"
        used.add(final)
        files.append(str(_download(url, out / final, ctype)))
    rec = {"t": time.strftime("%Y-%m-%dT%H:%M:%S"), "endpoint": endpoint, "name": name, "request_id": rid, "input": body, "files": files}
    with open(out / "fal_manifest.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    ctx.log(f"saved {len(files)} file(s) to {out}")
    return files


@action(confirm=True)
def sprite(ctx, prompt, name="sprite", out="assets/gen", style="pixel"):
    "Sprite or icon with a transparent background (style: pixel, or none)"
    text = prompt + (". " + STYLE[style] if style in STYLE else "")
    endpoint, extra = RECIPES["sprite"]
    return generate(ctx, endpoint, json.dumps(dict({"prompt": text}, **extra)), name, out)


@action(confirm=True)
def image(ctx, prompt, name="image", out="assets/gen"):
    "Concept art, key art or background"
    endpoint, extra = RECIPES["image"]
    return generate(ctx, endpoint, json.dumps(dict({"prompt": prompt}, **extra)), name, out)


@action(confirm=True)
def sfx(ctx, prompt, name="sfx", out="assets/gen", seconds=1.5):
    "Sound effect (MP3 out; convert with ffmpeg for the engine)"
    endpoint, extra = RECIPES["sfx"]
    return generate(ctx, endpoint, json.dumps(dict({"text": prompt, "duration_seconds": seconds}, **extra)), name, out)


@action(confirm=True)
def run(ctx, endpoint, input="{}", name="asset", out="assets/gen"):
    "Any fal endpoint with a JSON input (same as generate; for models not in the recipes)"
    return generate(ctx, endpoint, input, name, out)
