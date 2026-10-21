"""Workspaces, cloud sandbox sessions, voice, Telegram and admin routes."""
import json, os, shutil, time, urllib.request, uuid

from fastapi import Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

import cloud
from app import app, conn, new_token, notify, setting, user
from tgbot import Bot


def _ws(u, wid):
    if not cloud.ws_exists(u["id"], wid):
        raise HTTPException(404, "no such workspace")
    return wid


class WsCreate(BaseModel):
    name: str


class WsFiles(BaseModel):
    put: list[dict] = []
    delete: list[str] = []


class WsGet(BaseModel):
    paths: list[str]


@app.get("/api/ws")
def ws_list(u=Depends(user)):
    return cloud.ws_list(u["id"])


@app.post("/api/ws")
def ws_create(c: WsCreate, u=Depends(user)):
    return cloud.ws_create(u["id"], c.name.strip()[:60] or "project")


@app.delete("/api/ws/{wid}")
def ws_delete(wid: str, u=Depends(user)):
    cloud.ws_delete(u["id"], _ws(u, wid))
    return {"ok": True}


@app.get("/api/ws/{wid}/manifest")
def ws_manifest(wid: str, u=Depends(user)):
    return {"files": cloud.ws_manifest(u["id"], _ws(u, wid))}


@app.post("/api/ws/{wid}/files")
def ws_files(wid: str, f: WsFiles, u=Depends(user)):
    try:
        cloud.ws_put(u["id"], _ws(u, wid), f.put, f.delete)
    except ValueError as e:
        raise HTTPException(400, str(e))
    notify(u["id"])
    return {"ok": True}


@app.post("/api/ws/{wid}/get")
def ws_get(wid: str, g: WsGet, u=Depends(user)):
    return {"files": cloud.ws_get(u["id"], _ws(u, wid), g.paths[:500])}


@app.get("/api/ws/{wid}/file")
def ws_file(wid: str, path: str, u=Depends(user)):
    try:
        data = cloud.safe_path(cloud.ws_root(u["id"], _ws(u, wid)), path).read_bytes()
    except (OSError, ValueError):
        raise HTTPException(404, "not found")
    if b"\0" in data[:4096]:
        return {"path": path, "binary": True, "size": len(data)}
    return {"path": path, "text": data.decode("utf-8", "replace"), "size": len(data)}


# ─── cloud agent: same endpoint shape as the local `me web` server ───


def runner_token(uid):
    """Each sandbox has its own token: a new one must not log out the others still running (notify, sync, later)."""
    conn.execute("DELETE FROM tokens WHERE user_id=? AND device='cloud sandbox' AND used<?", (uid, time.time() - 14 * 86400))
    return new_token(uid, "cloud sandbox")


class Start(BaseModel):
    session: str | None = None
    ws: str | None = None


class Prompt(BaseModel):
    text: str
    images: list[str] = []


def _worker(u, wid):
    try:
        return cloud.runner.get(u["id"], wid)
    except KeyError as e:
        raise HTTPException(404, str(e))


@app.get("/api/cloud")
def cloud_list(u=Depends(user)):
    return [w.info() for w in cloud.runner.of_user(u["id"])]


@app.post("/api/cloud")
def cloud_start(s: Start, u=Depends(user)):
    ws = s.ws
    if s.session and not ws:
        r = conn.execute("SELECT content FROM docs WHERE user_id=? AND key=?", (u["id"], "session/" + s.session)).fetchone()
        if r:
            try:
                ws = json.loads(r["content"]).get("ws")
            except ValueError:
                pass
    try:
        w = cloud.runner.start(u["id"], u["username"], lambda: runner_token(u["id"]), wid=ws, session=s.session)
    except RuntimeError as e:
        raise HTTPException(503, str(e))
    return w.info()


@app.get("/api/cloud/{wid}/events")
def cloud_events(wid: str, after: int = 0, u=Depends(user)):
    w = _worker(u, wid)
    w.watched = time.time()  # someone is looking: no push needed for this worker's events
    return {"events": w.wait_events(after, timeout=25), "state": w.info()}


@app.post("/api/cloud/{wid}/prompt")
def cloud_prompt(wid: str, p: Prompt, u=Depends(user)):
    w = _worker(u, wid)
    w.prompt(p.text, p.images[:8])  # while busy the sandbox queues it (Agent.submit)
    return {"ok": True}


@app.post("/api/cloud/{wid}/goal")
def cloud_goal(wid: str, p: Prompt, u=Depends(user)):
    w = _worker(u, wid)
    if w.busy:
        raise HTTPException(409, "still working")
    w._push({"type": "user", "text": "/goal " + p.text})
    w.send({"type": "goal", "text": p.text})
    return {"ok": True}


class Answer(BaseModel):
    id: str
    text: str = ""
    ok: bool | None = None


@app.post("/api/cloud/{wid}/answer")
def cloud_answer(wid: str, a: Answer, u=Depends(user)):
    _worker(u, wid).send({"type": "answer", "id": a.id, "text": a.text})
    return {"ok": True}


class ModelPick(BaseModel):
    name: str


@app.post("/api/cloud/{wid}/model")
def cloud_model(wid: str, p: ModelPick, u=Depends(user)):
    _worker(u, wid).send({"type": "model", "name": p.name})  # this chat's model; other chats keep theirs
    return {"ok": True}


@app.post("/api/cloud/{wid}/{action}")
def cloud_action(wid: str, action: str, u=Depends(user)):
    if action not in ("cancel", "new", "sync", "compact", "undo", "retry"):
        raise HTTPException(404)
    _worker(u, wid).send({"type": action})
    return {"ok": True}


@app.delete("/api/cloud/{wid}")
def cloud_stop(wid: str, u=Depends(user)):
    _worker(u, wid).stop()
    return {"ok": True}


# ─── voice ───


def user_config(uid):
    r = conn.execute("SELECT content FROM docs WHERE user_id=? AND key='config' AND deleted=0", (uid,)).fetchone()
    try:
        return json.loads(r["content"]) if r else {}
    except ValueError:
        return {}


def transcribe(uid, audio, filename="voice.ogg"):
    cfg = user_config(uid)
    v = cfg.get("voice") or {}
    key = v.get("groq_key") or next((p.get("api_key") for p in cfg.get("providers", {}).values() if "groq.com" in p.get("base_url", "")), "")
    if not key:
        raise RuntimeError("add a Groq API key in your account (Voice) to use voice")
    b = uuid.uuid4().hex
    parts = [f'--{b}\r\nContent-Disposition: form-data; name="model"\r\n\r\n{v.get("model") or "whisper-large-v3"}\r\n'.encode()]
    if v.get("language"):
        parts.append(f'--{b}\r\nContent-Disposition: form-data; name="language"\r\n\r\n{v["language"]}\r\n'.encode())
    parts.append(f'--{b}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\n\r\n'.encode() + audio + b"\r\n")
    parts.append(f"--{b}--\r\n".encode())
    req = urllib.request.Request("https://api.groq.com/openai/v1/audio/transcriptions", data=b"".join(parts), method="POST",
                                 headers={"Authorization": "Bearer " + key, "Content-Type": f"multipart/form-data; boundary={b}", "User-Agent": "me"})
    return json.loads(urllib.request.urlopen(req, timeout=60).read()).get("text", "").strip()


@app.post("/api/voice")
async def voice(req: Request, u=Depends(user)):
    """Web UI dictation: raw audio body → text."""
    import anyio
    audio = await req.body()
    ext = req.headers.get("content-type", "audio/webm").split("/")[-1].split(";")[0]
    try:
        text = await anyio.to_thread.run_sync(lambda: transcribe(u["id"], audio, "voice." + ext))
    except Exception as e:
        raise HTTPException(400, str(e))
    return {"text": text}


# ─── telegram ───

bot = Bot(conn, cloud.runner, lambda uid: dict(conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()), runner_token, transcribe)
try:
    bot.configure(setting("tg_token"))
except Exception:
    pass


@app.get("/api/agents")
def user_agents(u=Depends(user)):
    """Built-in presets + the user's agent/*.md docs (parsed by the client's own code)."""
    import tempfile
    from pathlib import Path as _P
    with tempfile.TemporaryDirectory() as d:
        st = cloud.me.Store(d)
        for r in conn.execute("SELECT key,content FROM docs WHERE user_id=? AND deleted=0 AND key LIKE 'agent/%'", (u["id"],)):
            (_P(d) / "agents" / r["key"][6:]).write_text(r["content"], "utf-8")
        ps = cloud.me.presets(st)
    cfg = user_config(u["id"])
    return {"current": cfg.get("agent") or "code",
            "agents": [{"name": n, "label": p.get("label", n), "description": p.get("description", ""), "tools": p.get("tools", "")} for n, p in ps.items()]}


# ─── themes (built-ins from the client + the user's installed theme docs) ───


@app.get("/api/themes")
def user_themes(u=Depends(user)):
    out = dict(cloud.me.BUILTIN_THEMES)
    for r in conn.execute("SELECT key,content FROM docs WHERE user_id=? AND deleted=0 AND key LIKE 'theme/%'", (u["id"],)):
        try:
            t = json.loads(r["content"])
            out[t.get("name") or r["key"][6:-5]] = t
        except ValueError:
            pass
    cfg = user_config(u["id"])
    full = []
    for n, t in out.items():
        base = dict(cloud.me.BUILTIN_THEMES["me-dark" if t.get("dark", True) else "me-light"]["colors"])
        cols = t.get("colors") if isinstance(t.get("colors"), dict) else {}
        base.update({k: cloud.me.hex6(v) for k, v in cols.items() if cloud.me.hex6(v)})  # only real colors reach the page's CSS
        full.append(dict(t, name=n, colors=base))
    return {"current": cfg.get("theme") or "me-dark", "themes": full}


# ─── admin ───


class AdminSettings(BaseModel):
    tg_token: str | None = None
    gh_client_id: str | None = None
    gh_client_secret: str | None = None


def _admin(u):
    if not u["admin"]:
        raise HTTPException(403, "admin only")


@app.get("/api/admin/settings")
def admin_settings(u=Depends(user)):
    _admin(u)
    return {"tg_token_set": bool(setting("tg_token")), "tg_bot": bot.username, "max_workers": cloud.MAX_WORKERS, "sandbox": bool(cloud.SANDBOX),
            "gh_client_id": setting("gh_client_id"), "gh_secret_set": bool(setting("gh_client_secret")),
            "workers": [w.info() | {"user": w.username} for w in cloud.runner.workers.values() if w.alive]}


@app.post("/api/admin/settings")
def admin_settings_set(a: AdminSettings, u=Depends(user)):
    _admin(u)
    if a.tg_token is not None:
        try:
            bot.token = a.tg_token.strip() or None
            bot.configure(a.tg_token.strip())
        except Exception as e:
            raise HTTPException(400, f"telegram: {e}")
        setting("tg_token", a.tg_token.strip())
    for k in ("gh_client_id", "gh_client_secret"):  # GitHub OAuth app: "sign in with GitHub" for every user
        if getattr(a, k) is not None:
            setting(k, getattr(a, k).strip())
    return {"ok": True, "tg_bot": bot.username}


@app.get("/api/admin/stats")
def admin_stats(u=Depends(user)):
    _admin(u)
    mem = {}
    if os.path.exists("/proc/meminfo"):
        mem = {l.split(":")[0]: int(l.split()[1]) for l in open("/proc/meminfo").read().splitlines()[:3]}
    du = shutil.disk_usage(str(cloud.DATA) if cloud.DATA.exists() else "/")
    return {"load": os.getloadavg(), "cpus": os.cpu_count(), "mem_total_mb": mem.get("MemTotal", 0) // 1024,
            "mem_avail_mb": mem.get("MemAvailable", 0) // 1024, "disk_free_gb": round(du.free / 1e9, 1),
            "workers": len([w for w in cloud.runner.workers.values() if w.alive]), "max_workers": cloud.MAX_WORKERS}


# ─── web app (same UI as `me web`) ───


@app.get("/app")
def web_app():
    return HTMLResponse(getattr(cloud.me, "WEBUI", "web UI missing").replace("__MODE__", "cloud"))
