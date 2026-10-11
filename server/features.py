"""Push notifications, background + scheduled jobs, preview URLs, personal Telegram bots, notify, GitHub.

- Web Push (VAPID + aes128gcm, RFC 8291/8292) with `cryptography`: "done" when a task finishes and nobody is watching,
  "waiting for you" when the agent asks.
- Jobs: "do X and notify me" runs in a sandbox after the UI closes; optional cron schedule per user.
- Preview: /preview/<signed token>/<path> serves a workspace's files; /preview/<token>/port/… proxies the sandbox's dev server ($PORT).
- Telegram: every user may run their own bot (token in the account); notify() sends through it (or the global bot).
"""
import base64, hashlib, hmac, json, os, re, secrets, subprocess, threading, time, urllib.error, urllib.parse, urllib.request
from datetime import datetime, timedelta, timezone

from fastapi import Depends, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse, Response
from pydantic import BaseModel

import cloud
from app import PUBLIC, app, conn, notify as notify_changes, put_doc, setting, user
from routes_cloud import bot as global_bot, runner_token, transcribe, user_config
from tgbot import Bot

conn.executescript("""
CREATE TABLE IF NOT EXISTS push_subs(endpoint TEXT PRIMARY KEY, user_id INTEGER, p256dh TEXT, auth TEXT, created REAL);
CREATE TABLE IF NOT EXISTS tg_bots(user_id INTEGER PRIMARY KEY, token TEXT, username TEXT, created REAL);
CREATE TABLE IF NOT EXISTS jobs(id INTEGER PRIMARY KEY, user_id INTEGER, name TEXT, prompt TEXT, cron TEXT, tz INTEGER DEFAULT 0, ws TEXT,
  next REAL, last REAL, status TEXT DEFAULT '', result TEXT DEFAULT '', session TEXT DEFAULT '', enabled INTEGER DEFAULT 1, created REAL);
""")


def b64u(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def unb64u(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


# ─── web push ───


def _vapid():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    pem = setting("vapid")
    if not pem:
        key = ec.generate_private_key(ec.SECP256R1())
        pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
        setting("vapid", pem)
    key = serialization.load_pem_private_key(pem.encode(), None)
    pub = key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return key, pub


def _jwt(key, aud):
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
    head = b64u(json.dumps({"typ": "JWT", "alg": "ES256"}).encode())
    body = b64u(json.dumps({"aud": aud, "exp": int(time.time()) + 12 * 3600, "sub": "mailto:admin@me-agent.ru"}).encode())
    r, s = decode_dss_signature(key.sign(f"{head}.{body}".encode(), ec.ECDSA(hashes.SHA256())))
    return f"{head}.{body}." + b64u(r.to_bytes(32, "big") + s.to_bytes(32, "big"))


def encrypt_push(payload, p256dh, auth):
    """RFC 8291 aes128gcm body for one subscription."""
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    ua_pub = unb64u(p256dh)
    eph = ec.generate_private_key(ec.SECP256R1())
    as_pub = eph.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    shared = eph.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_pub))
    hk = lambda salt, ikm, info, n: HKDF(hashes.SHA256(), n, salt, info).derive(ikm)
    prk = hk(unb64u(auth), shared, b"WebPush: info\0" + ua_pub + as_pub, 32)
    salt = secrets.token_bytes(16)
    cek = hk(salt, prk, b"Content-Encoding: aes128gcm\0", 16)
    nonce = hk(salt, prk, b"Content-Encoding: nonce\0", 12)
    ct = AESGCM(cek).encrypt(nonce, payload + b"\x02", None)
    return salt + (4096).to_bytes(4, "big") + bytes([len(as_pub)]) + as_pub + ct


def push(uid, title, body="", url="/app", tag="me"):
    subs = conn.execute("SELECT * FROM push_subs WHERE user_id=?", (uid,)).fetchall()
    if not subs:
        return 0
    key, pub = _vapid()
    data = json.dumps({"title": title, "body": body[:300], "url": url, "tag": tag}).encode()
    sent = 0
    for s in subs:
        ep = s["endpoint"]
        aud = "{0.scheme}://{0.netloc}".format(urllib.parse.urlparse(ep))
        req = urllib.request.Request(ep, data=encrypt_push(data, s["p256dh"], s["auth"]), method="POST", headers={
            "Authorization": f"vapid t={_jwt(key, aud)}, k={b64u(pub)}", "Content-Encoding": "aes128gcm", "TTL": "86400",
            "Content-Type": "application/octet-stream", "Urgency": "high"})
        try:
            urllib.request.urlopen(req, timeout=15).read()
            sent += 1
        except urllib.error.HTTPError as e:
            if e.code in (404, 410):
                conn.execute("DELETE FROM push_subs WHERE endpoint=?", (ep,))
        except Exception:
            pass
    return sent


@app.get("/api/push/key")
def push_key():
    return {"key": b64u(_vapid()[1])}


class PushSub(BaseModel):
    endpoint: str
    keys: dict


@app.post("/api/push/subscribe")
def push_subscribe(s: PushSub, u=Depends(user)):
    if not s.endpoint.startswith("https://") or not s.keys.get("p256dh") or not s.keys.get("auth"):
        raise HTTPException(400, "bad subscription")
    conn.execute("INSERT OR REPLACE INTO push_subs VALUES(?,?,?,?,?)", (s.endpoint, u["id"], s.keys["p256dh"], s.keys["auth"], time.time()))
    return {"ok": True}


@app.delete("/api/push")
def push_off(u=Depends(user)):
    conn.execute("DELETE FROM push_subs WHERE user_id=?", (u["id"],))
    return {"ok": True}


@app.post("/api/push/test")
def push_test(u=Depends(user)):
    return {"sent": push(u["id"], "me", "Notifications work.", tag="test")}


# ─── personal Telegram bots + notify ───

bots = {}  # uid -> Bot (personal)


def _users(uid):
    return dict(conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone())


def start_bot(uid, token):
    b = Bot(conn, cloud.runner, _users, runner_token, transcribe, owner=uid)
    b.configure(token)  # raises on a bad token
    old = bots.pop(uid, None)
    if old:
        old.stop()
    bots[uid] = b
    return b


def _boot_bots():
    for r in conn.execute("SELECT user_id, token FROM tg_bots").fetchall():
        try:
            start_bot(r["user_id"], r["token"])
        except Exception:
            pass


threading.Thread(target=_boot_bots, daemon=True).start()


def tg_send(uid, text, file=None):
    """Send via the user's own bot (preferred) and/or the global bot to every chat they linked. Returns chats reached."""
    sent = 0
    for b in [bots.get(uid), global_bot]:
        if not b or not b.token:
            continue
        for chat in b.chats_of(uid):
            try:
                if file:
                    b.send_file(chat, file[0], file[1], caption=text)
                else:
                    b.send(chat, text)
                sent += 1
            except Exception:
                pass
        if sent:
            break
    return sent


@app.get("/api/tg")
def tg_state(u=Depends(user)):
    own = bots.get(u["id"])
    links = {r["bot"] for r in conn.execute("SELECT COALESCE(bot,'') bot FROM tg_links WHERE user_id=?", (u["id"],))}
    return {"bot": global_bot.username, "linked": "" in links, "own": own.username if own else None,
            "own_linked": bool(own and own.key in links)}


class TgLink(BaseModel):
    own: bool = False


@app.post("/api/tg/link")
def tg_link(l: TgLink | None = None, u=Depends(user)):
    b = bots.get(u["id"]) if l and l.own else global_bot
    url = b.link_code(u["id"]) if b else None
    if not url:
        raise HTTPException(400, "add your bot token first" if l and l.own else "Telegram bot is not configured by the admin yet")
    return {"url": url}


@app.delete("/api/tg/link")
def tg_unlink(u=Depends(user)):
    conn.execute("DELETE FROM tg_links WHERE user_id=?", (u["id"],))
    return {"ok": True}


class TgBot(BaseModel):
    token: str


@app.post("/api/tg/bot")
def tg_bot_set(t: TgBot, u=Depends(user)):
    tok = t.token.strip()
    if not re.fullmatch(r"\d{5,}:[\w-]{20,}", tok):
        raise HTTPException(400, "that does not look like a bot token (123456789:AA…)")
    if any(b.token == tok for uid, b in bots.items() if uid != u["id"]) or tok == global_bot.token:
        raise HTTPException(409, "this bot is already in use")
    try:
        b = start_bot(u["id"], tok)
    except Exception as e:
        raise HTTPException(400, f"telegram: {e}")
    conn.execute("INSERT OR REPLACE INTO tg_bots VALUES(?,?,?,?)", (u["id"], tok, b.username, time.time()))
    return {"ok": True, "username": b.username, "url": b.link_code(u["id"])}


@app.delete("/api/tg/bot")
def tg_bot_del(u=Depends(user)):
    b = bots.pop(u["id"], None)
    if b:
        conn.execute("DELETE FROM tg_links WHERE user_id=? AND bot=?", (u["id"], b.key))
        b.stop()
    conn.execute("DELETE FROM tg_bots WHERE user_id=?", (u["id"],))
    return {"ok": True}


class Notify(BaseModel):
    text: str = ""
    file: dict | None = None


@app.post("/api/notify")
def notify_route(n: Notify, u=Depends(user)):
    """The agent's notify tool: message (and optional file) to the user's Telegram; also a push if subscribed."""
    file = None
    if n.file:
        data = base64.b64decode(n.file.get("b64", ""))
        if len(data) > 45_000_000:
            raise HTTPException(400, "file over 45 MB")
        file = (re.sub(r"[^\w.-]+", "_", n.file.get("name") or "file")[:80], data)
    sent = tg_send(u["id"], n.text[:4000] or (file[0] if file else "…"), file)
    if not sent:
        pushed = push(u["id"], "me", n.text or "file ready")
        if not pushed:
            raise HTTPException(400, "no Telegram chat linked — connect your bot in the account (Telegram card)")
    return {"sent": sent}


# ─── worker events → notifications, jobs ───

_texts = {}  # worker id -> final answer text of the current turn


def on_worker_event(w, e):
    t = e.get("type")
    if t == "turn_start":
        _texts[w.id] = ""
    elif t == "text":
        _texts[w.id] = (_texts.get(w.id, "") + e.get("text", ""))[-4000:]
    elif t == "tool_start":
        _texts[w.id] = ""
    elif t in ("ask", "confirm") and time.time() - w.watched > 20:
        threading.Thread(target=push, args=(w.uid, "me is waiting for you", e.get("text") or e.get("name", ""),
                                            f"/app?s={w.session or ''}", "wait-" + w.id), daemon=True).start()
    elif t == "turn_end" or (t == "exit" and w.job):
        text = _texts.pop(w.id, "").strip()
        if w.job:
            threading.Thread(target=job_finished, args=(w, e, text), daemon=True).start()
        elif time.time() - w.watched > 20:
            title = "Done: " + (w.title or "task")[:60]
            threading.Thread(target=push, args=(w.uid, title, text[:300] or "finished", f"/app?s={w.session or ''}", "done-" + w.id),
                             daemon=True).start()


cloud.runner.on_event = on_worker_event


def cron_next(expr, after, tz_min=0):
    """Next fire time (epoch) of a 5-field cron expression in the user's timezone (offset in minutes)."""
    fields = expr.split()
    if len(fields) != 5:
        raise ValueError("cron needs 5 fields: minute hour day month weekday")
    rng = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 6)]

    def parse(f, lo, hi):
        out = set()
        for part in f.split(","):
            step = 1
            if "/" in part:
                part, st = part.split("/")
                step = int(st)
            if part in ("*", ""):
                a, b = lo, hi
            elif "-" in part:
                a, b = map(int, part.split("-"))
            else:
                a = b = int(part)
            if a < lo or b > hi + (1 if hi == 6 else 0):
                raise ValueError(f"{part} out of range {lo}-{hi}")
            out.update(x % 7 if hi == 6 else x for x in range(a, b + 1, step))
        return out
    mins, hours, days, months, wdays = [parse(f, *r) for f, r in zip(fields, rng)]
    tz = timezone(timedelta(minutes=tz_min))
    t = datetime.fromtimestamp(after, tz).replace(second=0, microsecond=0) + timedelta(minutes=1)
    day_any, wd_any = fields[2] == "*", fields[4] == "*"
    for _ in range(366 * 24 * 60):
        if t.month in months and t.hour in hours and t.minute in mins:
            dm, dw = t.day in days, (t.isoweekday() % 7) in wdays
            if (dm and dw) if (day_any or wd_any) else (dm or dw):
                return t.timestamp()
        if t.month not in months:
            t = (t.replace(day=1, hour=0, minute=0) + timedelta(days=32)).replace(day=1)
        elif t.hour not in hours:
            t = t.replace(minute=0) + timedelta(hours=1)
        else:
            t += timedelta(minutes=1)
    raise ValueError("cron never fires")


class Job(BaseModel):
    prompt: str
    name: str = ""
    cron: str = ""
    tz: int = 0  # minutes east of UTC (browser: -new Date().getTimezoneOffset())
    ws: str = ""


def _job(r):
    d = dict(r)
    d.pop("user_id", None)
    return d


@app.get("/api/jobs")
def jobs_list(u=Depends(user)):
    return [_job(r) for r in conn.execute("SELECT * FROM jobs WHERE user_id=? ORDER BY created DESC", (u["id"],))]


@app.post("/api/jobs")
def jobs_add(j: Job, u=Depends(user)):
    if not j.prompt.strip():
        raise HTTPException(400, "prompt is empty")
    if conn.execute("SELECT COUNT(*) FROM jobs WHERE user_id=?", (u["id"],)).fetchone()[0] >= 50:
        raise HTTPException(400, "50 jobs max — delete some")
    nxt = time.time()
    if j.cron.strip():
        try:
            nxt = cron_next(j.cron.strip(), time.time(), j.tz)
        except ValueError as e:
            raise HTTPException(400, f"cron: {e}")
    if j.ws and not cloud.ws_exists(u["id"], j.ws):
        raise HTTPException(404, "no such workspace")
    cur = conn.execute("INSERT INTO jobs(user_id,name,prompt,cron,tz,ws,next,created) VALUES(?,?,?,?,?,?,?,?)",
                       (u["id"], (j.name or j.prompt.split("\n")[0])[:80], j.prompt[:20000], j.cron.strip(), j.tz, j.ws, nxt, time.time()))
    _wake.set()
    return _job(conn.execute("SELECT * FROM jobs WHERE id=?", (cur.lastrowid,)).fetchone())


@app.delete("/api/jobs/{jid}")
def jobs_del(jid: int, u=Depends(user)):
    conn.execute("DELETE FROM jobs WHERE id=? AND user_id=?", (jid, u["id"]))
    return {"ok": True}


@app.post("/api/jobs/{jid}/{action}")
def jobs_action(jid: int, action: str, u=Depends(user)):
    r = conn.execute("SELECT * FROM jobs WHERE id=? AND user_id=?", (jid, u["id"])).fetchone()
    if not r:
        raise HTTPException(404)
    if action == "run":
        conn.execute("UPDATE jobs SET next=?, enabled=1 WHERE id=?", (time.time(), jid))
        _wake.set()
    elif action == "toggle":
        en = 0 if r["enabled"] else 1
        nxt = cron_next(r["cron"], time.time(), r["tz"]) if en and r["cron"] else r["next"]
        conn.execute("UPDATE jobs SET enabled=?, next=? WHERE id=?", (en, nxt, jid))
    else:
        raise HTTPException(404)
    return _job(conn.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone())


JOB_SUFFIX = ("\n\n(This runs as a background task: nobody is watching. Do not ask questions — decide sensibly, verify your work, "
              "and end with a short report of the result.)")


def run_job(r):
    u = _users(r["user_id"])
    conn.execute("UPDATE jobs SET status='running', last=?, next=? WHERE id=?",
                 (time.time(), cron_next(r["cron"], time.time(), r["tz"]) if r["cron"] else 0, r["id"]))
    if not r["cron"]:
        conn.execute("UPDATE jobs SET enabled=0 WHERE id=?", (r["id"],))
    try:
        w = cloud.runner.start(u["id"], u["username"], lambda: runner_token(u["id"]), wid=r["ws"] or None)
    except RuntimeError as e:
        conn.execute("UPDATE jobs SET status='error', result=? WHERE id=?", (str(e), r["id"]))
        return
    w.job = r["id"]
    w.watched = 0
    w.prompt(r["prompt"] + JOB_SUFFIX)


def job_finished(w, e, text):
    jid, w.job = w.job, None
    r = conn.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
    if not r:
        return
    ok = e.get("type") == "turn_end"
    conn.execute("UPDATE jobs SET status=?, result=?, session=? WHERE id=?", ("done" if ok else "error", text[:4000], w.session or "", jid))
    url = f"{PUBLIC}/app?s={w.session}" if w.session else f"{PUBLIC}/app"
    msg = f"{'✓' if ok else '✗'} {r['name']}\n\n{text[:3500] or ('finished' if ok else 'the sandbox stopped')}\n\n{url}"
    if not tg_send(r["user_id"], msg):
        push(r["user_id"], ("Done: " if ok else "Failed: ") + r["name"], text[:300], f"/app?s={w.session or ''}", f"job-{jid}")


_wake = threading.Event()


def _scheduler():
    while True:
        _wake.wait(15)
        _wake.clear()
        try:
            due = conn.execute("SELECT * FROM jobs WHERE enabled=1 AND next>0 AND next<=? AND status<>'running'", (time.time(),)).fetchall()
            for r in due:
                run_job(r)
        except Exception as ex:
            print("scheduler:", ex)


conn.execute("UPDATE jobs SET status='error', result='server restarted while running' WHERE status='running'")
threading.Thread(target=_scheduler, daemon=True).start()


# ─── preview: a sandbox's files or dev server on the phone, by link ───


def _preview_owner(tok):
    m = re.fullmatch(r"(\d+)-([a-z0-9-]+)-([0-9a-f]{16})", tok)
    if not m or not hmac.compare_digest(cloud.preview_token(int(m.group(1)), m.group(2)), tok):
        raise HTTPException(404, "preview link is invalid")
    return int(m.group(1)), m.group(2)


@app.get("/preview/{tok}")
def preview_root(tok: str):
    _preview_owner(tok)
    return RedirectResponse(f"/preview/{tok}/")


@app.api_route("/preview/{tok}/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"])
async def preview(tok: str, path: str, req: Request):
    import anyio
    uid, wid = _preview_owner(tok)
    if path == "port" or path.startswith("port/"):
        w = next((w for w in cloud.runner.of_user(uid) if w.wid == wid and w.port), None)
        if not w:
            raise HTTPException(404, "no sandbox is running for this project — open it in the web app")
        body = await req.body()
        url = f"http://127.0.0.1:{w.port}/{path[5:]}" + (f"?{req.url.query}" if req.url.query else "")
        hdr = {k: v for k, v in req.headers.items() if k.lower() in ("content-type", "accept", "cookie", "authorization", "user-agent")}

        def fetch():
            r = urllib.request.Request(url, data=body or None, method=req.method, headers=hdr)
            try:
                with urllib.request.urlopen(r, timeout=60) as resp:
                    return resp.status, resp.headers, resp.read()
            except urllib.error.HTTPError as e:
                return e.code, e.headers, e.read()
            except OSError as e:
                return 502, {}, f"dev server on port {w.port} is not answering ({e}). Start it with: PORT={w.port}".encode()
        status, headers, data = await anyio.to_thread.run_sync(fetch)
        keep = {k: v for k, v in dict(headers).items() if k.lower() in ("content-type", "cache-control", "set-cookie", "location")}
        return Response(data, status_code=status, headers=keep)
    root = cloud.ws_root(uid, wid)
    try:
        p = cloud.safe_path(root, path) if path else root
    except ValueError:
        raise HTTPException(404)
    if p.is_dir():
        if path and not path.endswith("/"):
            return RedirectResponse(f"/preview/{tok}/{path}/")
        for idx in ("index.html", "dist/index.html", "build/index.html", "public/index.html"):
            if (p / idx).is_file():
                if idx != "index.html":
                    return RedirectResponse(f"/preview/{tok}/{path}{idx.rsplit('/', 1)[0]}/")
                p = p / idx
                break
        else:
            items = sorted(x.name + ("/" if x.is_dir() else "") for x in p.iterdir() if not x.name.startswith("."))
            html = "".join(f'<li><a href="{urllib.parse.quote(n)}">{n}</a></li>' for n in items[:500])
            return Response(f"<!doctype html><meta name=viewport content='width=device-width'><title>preview</title>"
                            f"<body style='font:15px system-ui;padding:16px'><h3>/{path}</h3><ul>{html}</ul>", media_type="text/html")
    if not p.is_file():
        raise HTTPException(404, "not found")
    return FileResponse(p, headers={"Cache-Control": "no-store"})


# ─── GitHub: token per user, clone into a workspace ───


class GhToken(BaseModel):
    token: str


def _gh_user(tok):
    req = urllib.request.Request("https://api.github.com/user", headers={"Authorization": "Bearer " + tok, "User-Agent": "me",
                                                                        "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())["login"]


def _set_secret(uid, key, value):
    cfg = user_config(uid) or {}
    sec = cfg.setdefault("secrets", {})
    if value:
        sec[key] = value
    else:
        sec.pop(key, None)
    put_doc(uid, "config", json.dumps(cfg, ensure_ascii=False, indent=1))


@app.get("/api/github")
def github_state(u=Depends(user)):
    tok = (user_config(u["id"]).get("secrets") or {}).get("GITHUB_TOKEN")
    if not tok:
        return {"connected": False}
    try:
        return {"connected": True, "login": _gh_user(tok)}
    except Exception as e:
        return {"connected": True, "login": None, "error": str(e)[:200]}


@app.post("/api/github")
def github_connect(t: GhToken, u=Depends(user)):
    tok = t.token.strip()
    try:
        login = _gh_user(tok)
    except Exception as e:
        raise HTTPException(400, f"GitHub rejected the token: {e}")
    _set_secret(u["id"], "GITHUB_TOKEN", tok)
    for r in conn.execute("SELECT id, content FROM market WHERE name='github'"):  # the github skill is the PR/issue half of the integration
        if not conn.execute("SELECT 1 FROM docs WHERE user_id=? AND key LIKE 'skill/github.%' AND deleted=0", (u["id"],)).fetchone():
            put_doc(u["id"], "skill/github.py", r["content"])
    return {"ok": True, "login": login}


@app.delete("/api/github")
def github_disconnect(u=Depends(user)):
    _set_secret(u["id"], "GITHUB_TOKEN", "")
    return {"ok": True}


class Clone(BaseModel):
    url: str
    branch: str = ""


@app.post("/api/ws/{wid}/clone")
def ws_clone(wid: str, c: Clone, u=Depends(user)):
    if not cloud.ws_exists(u["id"], wid):
        raise HTTPException(404, "no such workspace")
    url = c.url.strip()
    if re.fullmatch(r"[\w.-]+/[\w.-]+", url):
        url = f"https://github.com/{url}"
    if not re.fullmatch(r"https://[\w.-]+/[\w./-]+?(\.git)?", url):
        raise HTTPException(400, "give an https git URL or owner/repo")
    root = cloud.ws_root(u["id"], wid)
    name = re.sub(r"\.git$", "", url.rstrip("/").split("/")[-1])
    dest = root if not any(root.iterdir()) else root / name
    if dest.exists() and dest != root:
        raise HTTPException(409, f"{name}/ already exists in this workspace")
    env = cloud.me.shell_env(user_config(u["id"]).get("secrets"))
    cmd = ["git", "clone", "--depth", "50", *(["--branch", c.branch] if c.branch else []), url, str(dest)]
    r = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=300)
    if r.returncode:
        raise HTTPException(400, (r.stderr or r.stdout).strip()[-600:])
    notify_changes(u["id"])
    return {"ok": True, "path": "." if dest == root else name}


# ─── PWA: the web app installs to the home screen (same manifest/worker as `me web`) ───


@app.get("/manifest.webmanifest")
def manifest():
    return Response(json.dumps(cloud.me.PWA_MANIFEST), media_type="application/manifest+json")


@app.get("/sw.js")
def service_worker():
    return Response(cloud.me.SW_JS, media_type="text/javascript", headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"})


@app.get("/icon.svg")
def icon():
    return Response(cloud.me.ICON_SVG, media_type="image/svg+xml")
