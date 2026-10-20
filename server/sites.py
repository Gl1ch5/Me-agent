"""Static sites: the agent publishes a folder, anyone opens it at /s/<username>/<site>/.
Files live in DATA/sites/<uid>/<site>/ and are served as plain files (no code runs on the server). Every page is sent with a
CSP sandbox, so a published page gets its own opaque origin and its scripts can't read the me-agent.ru session of a visitor.
"""
import base64, re, shutil, time

from fastapi import Depends, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import BaseModel

from app import app, base_url, conn, user
from cloud import DATA, safe_path

MAX_SITES, MAX_BYTES, MAX_FILES = 20, 25_000_000, 2000
NAME_RX = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")
SANDBOX = "sandbox allow-scripts allow-forms allow-popups allow-modals allow-downloads allow-pointer-lock"


def site_dir(uid, name):
    return DATA / "sites" / str(uid) / name


def site_url(req, username, name):
    return f"{base_url(req)}/s/{username}/{name}/"


def _size(d):
    return sum(f.stat().st_size for f in d.rglob("*") if f.is_file())


class SiteFile(BaseModel):
    path: str
    b64: str


class Site(BaseModel):
    name: str
    files: list[SiteFile]


@app.post("/api/sites")
def site_publish(s: Site, req: Request, u=Depends(user)):
    """Publish or replace a site: the given files become the whole site."""
    name = s.name.lower()
    if not NAME_RX.fullmatch(name):
        raise HTTPException(400, "site name: 1-40 chars, a-z 0-9 and -")
    root = DATA / "sites" / str(u["id"])
    if not (root / name).is_dir() and root.is_dir() and sum(1 for d in root.iterdir() if d.is_dir() and not d.name.startswith(".")) >= MAX_SITES:
        raise HTTPException(400, f"at most {MAX_SITES} sites — delete one first")
    if not s.files or len(s.files) > MAX_FILES:
        raise HTTPException(400, f"a site is 1-{MAX_FILES} files")
    data = []
    for f in s.files:
        try:
            data.append((f.path.replace("\\", "/").lstrip("/"), base64.b64decode(f.b64)))
        except ValueError:
            raise HTTPException(400, f"{f.path}: bad base64")
    if sum(len(b) for _, b in data) > MAX_BYTES:
        raise HTTPException(400, f"site over {MAX_BYTES // 1_000_000} MB")
    tmp = root / f".{name}.{int(time.time() * 1000)}"
    tmp.mkdir(parents=True)
    try:
        for rel, b in data:
            p = safe_path(tmp, rel)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b)
    except ValueError:
        shutil.rmtree(tmp, ignore_errors=True)
        raise HTTPException(400, "bad file path")
    old = root / name
    if old.exists():  # swap in place: visitors see the old or the new site, never half of each
        trash = root / f".{name}.old.{int(time.time() * 1000)}"
        old.rename(trash)
        tmp.rename(old)
        shutil.rmtree(trash, ignore_errors=True)
    else:
        tmp.rename(old)
    return {"url": site_url(req, u["username"], name), "files": len(data)}


@app.get("/api/sites")
def site_list(req: Request, u=Depends(user)):
    root = DATA / "sites" / str(u["id"])
    out = []
    for d in sorted(root.iterdir()) if root.is_dir() else []:
        if d.is_dir() and not d.name.startswith("."):
            out.append({"name": d.name, "url": site_url(req, u["username"], d.name), "size": _size(d), "updated": d.stat().st_mtime})
    return {"sites": out, "max": MAX_SITES}


@app.delete("/api/sites/{name}")
def site_delete(name: str, u=Depends(user)):
    if not NAME_RX.fullmatch(name) or not site_dir(u["id"], name).is_dir():
        raise HTTPException(404, "no such site")
    shutil.rmtree(site_dir(u["id"], name))
    return {"ok": True}


def _owner(username):
    r = conn.execute("SELECT id, username FROM users WHERE username=?", (username,)).fetchone()
    if not r:
        raise HTTPException(404, "no such site")
    return r


@app.get("/s/{username}/{name}")
def site_root(username: str, name: str):
    r = _owner(username)
    return RedirectResponse(f"/s/{r[1]}/{name}/")


@app.api_route("/s/{username}/{name}/{path:path}", methods=["GET", "HEAD"])
def site_file(username: str, name: str, path: str):
    r = _owner(username)
    if not NAME_RX.fullmatch(name) or not site_dir(r[0], name).is_dir():
        raise HTTPException(404, "no such site")
    root = site_dir(r[0], name)
    try:
        p = safe_path(root, path) if path else root
    except ValueError:
        raise HTTPException(404)
    if p.is_dir():
        if path and not path.endswith("/"):
            return RedirectResponse(f"/s/{r[1]}/{name}/{path}/")
        p = p / "index.html"
    if not p.is_file():
        p = root / "404.html"
        if not p.is_file():
            raise HTTPException(404, "not found")
        return FileResponse(p, status_code=404, headers={"Content-Security-Policy": SANDBOX})
    return FileResponse(p, headers={"Content-Security-Policy": SANDBOX, "Cache-Control": "public, max-age=60",
                                    "X-Content-Type-Options": "nosniff"})
