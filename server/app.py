"""me cloud — accounts, optional TOTP, last-write-wins doc sync, installer + site.
Run: uvicorn app:app --host 127.0.0.1 --port 8790
Env: ME_DB (sqlite path), ME_CLIENT (path to me.py), ME_PUBLIC_URL (e.g. https://me-agent.ru)
"""
import base64, hashlib, hmac, json, os, re, secrets, sqlite3, struct, threading, time
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, Response
from pydantic import BaseModel

HERE = Path(__file__).resolve().parent
DB_PATH = os.environ.get("ME_DB", str(HERE / "me.db"))
CLIENT = Path(os.environ.get("ME_CLIENT", HERE.parent / "me.py"))
PUBLIC = os.environ.get("ME_PUBLIC_URL", "").rstrip("/")
MAX_DOC = 20 * 1024 * 1024

app = FastAPI(title="me cloud", docs_url=None, redoc_url=None)
lock = threading.Lock()
changed = threading.Condition()  # notified on every doc write → instant sync via /api/changes


ws_rev = {}  # uid -> counter of workspace file changes (not stored in docs)


def notify(uid=None):
    if uid is not None:
        ws_rev[uid] = ws_rev.get(uid, 0) + 1
    with changed:
        changed.notify_all()


class DB:
    """One sqlite connection per thread (sqlite objects must not be shared across threads)."""

    def __init__(self, path):
        self.path, self.local = path, threading.local()

    def _c(self):
        c = getattr(self.local, "c", None)
        if c is None:
            c = sqlite3.connect(self.path, isolation_level=None, timeout=30)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA busy_timeout=30000")
            self.local.c = c
        return c

    def execute(self, *a):
        return self._c().execute(*a)

    def executescript(self, s):
        return self._c().executescript(s)


conn = DB(DB_PATH)
conn.executescript("""
CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, username TEXT UNIQUE COLLATE NOCASE, pw TEXT, totp TEXT, totp_on INTEGER DEFAULT 0,
  admin INTEGER DEFAULT 0, created REAL, seen REAL);
CREATE TABLE IF NOT EXISTS tokens(hash TEXT PRIMARY KEY, user_id INTEGER, device TEXT, created REAL, used REAL);
CREATE TABLE IF NOT EXISTS docs(user_id INTEGER, key TEXT, content TEXT, ts REAL, rev INTEGER, deleted INTEGER DEFAULT 0, size INTEGER,
  PRIMARY KEY(user_id, key));
CREATE TABLE IF NOT EXISTS market(id INTEGER PRIMARY KEY, name TEXT UNIQUE, author TEXT, user_id INTEGER, description TEXT, icon TEXT,
  color TEXT, ext TEXT, content TEXT, installs INTEGER DEFAULT 0, created REAL, updated REAL);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS tg_links(chat_id INTEGER PRIMARY KEY, user_id INTEGER, created REAL);
CREATE INDEX IF NOT EXISTS docs_rev ON docs(user_id, rev);
""")

for col, ddl in (("kind", "TEXT DEFAULT 'skill'"), ("is_default", "INTEGER DEFAULT 0")):
    try:
        conn.execute(f"ALTER TABLE market ADD COLUMN {col} {ddl}")
    except sqlite3.OperationalError:
        pass
try:
    conn.execute("ALTER TABLE users ADD COLUMN seeded INTEGER DEFAULT 0")
except sqlite3.OperationalError:
    pass


def setting(key, value=None):
    if value is None:
        r = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return r[0] if r else ""
    conn.execute("INSERT OR REPLACE INTO settings VALUES(?,?)", (key, value))


# ─── crypto helpers ───


def pw_hash(pw, salt=None):
    salt = salt or secrets.token_bytes(16)
    h = hashlib.scrypt(pw.encode(), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)
    return base64.b64encode(salt).decode() + "$" + base64.b64encode(h).decode()


def pw_ok(pw, stored):
    salt, _ = stored.split("$")
    return hmac.compare_digest(pw_hash(pw, base64.b64decode(salt)), stored)


def totp_code(secret, t=None):
    key = base64.b32decode(secret.upper() + "=" * (-len(secret) % 8))
    msg = struct.pack(">Q", int((t or time.time()) // 30))
    h = hmac.new(key, msg, hashlib.sha1).digest()
    o = h[-1] & 15
    return f"{(struct.unpack('>I', h[o:o + 4])[0] & 0x7FFFFFFF) % 1000000:06d}"


def totp_ok(secret, code):
    code = re.sub(r"\D", "", str(code or ""))
    return any(hmac.compare_digest(totp_code(secret, time.time() + d * 30), code) for d in (-1, 0, 1))


fails = {}


def throttle(key):
    n, t = fails.get(key, (0, 0))
    if n >= 8 and time.time() - t < 300:
        raise HTTPException(429, "too many attempts, wait 5 minutes")


def fail(key):
    n, t = fails.get(key, (0, 0))
    fails[key] = (n + 1 if time.time() - t < 300 else 1, time.time())


# ─── auth ───


def new_token(uid, device):
    tok = secrets.token_urlsafe(32)
    conn.execute("INSERT INTO tokens VALUES(?,?,?,?,?)", (hashlib.sha256(tok.encode()).hexdigest(), uid, device[:80], time.time(), time.time()))
    return tok


def user(authorization: str = Header("")):
    tok = authorization.removeprefix("Bearer ").strip()
    if not tok:
        raise HTTPException(401, "not logged in")
    h = hashlib.sha256(tok.encode()).hexdigest()
    r = conn.execute("SELECT u.* FROM tokens t JOIN users u ON u.id=t.user_id WHERE t.hash=?", (h,)).fetchone()
    if not r:
        raise HTTPException(401, "invalid token")
    now = time.time()
    conn.execute("UPDATE tokens SET used=? WHERE hash=?", (now, h))
    conn.execute("UPDATE users SET seen=? WHERE id=?", (now, r["id"]))
    return dict(r, token_hash=h)


class Creds(BaseModel):
    username: str
    password: str
    totp: str | None = None
    device: str = ""


@app.post("/api/register")
def register(c: Creds, req: Request):
    throttle("reg:" + req.client.host)
    if not re.fullmatch(r"[A-Za-z0-9_.-]{3,32}", c.username):
        raise HTTPException(400, "username: 3-32 chars, letters/digits/_.-")
    if len(c.password) < 6:
        raise HTTPException(400, "password: at least 6 chars")
    with lock:
        if conn.execute("SELECT 1 FROM users WHERE username=?", (c.username,)).fetchone():
            fail("reg:" + req.client.host)
            raise HTTPException(409, "username taken")
        first = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0
        cur = conn.execute("INSERT INTO users(username,pw,admin,created,seen) VALUES(?,?,?,?,?)",
                           (c.username, pw_hash(c.password), int(first), time.time(), time.time()))
    seed_user(cur.lastrowid)
    with lock:
        return {"token": new_token(cur.lastrowid, c.device or "web"), "username": c.username}


@app.post("/api/login")
def login(c: Creds, req: Request):
    key = f"login:{req.client.host}:{c.username.lower()}"
    throttle(key)
    u = conn.execute("SELECT * FROM users WHERE username=?", (c.username,)).fetchone()
    if not u or not pw_ok(c.password, u["pw"]):
        fail(key)
        raise HTTPException(401, "wrong username or password")
    if u["totp_on"]:
        if not c.totp:
            raise HTTPException(401, "totp_required")
        if not totp_ok(u["totp"], c.totp):
            fail(key)
            raise HTTPException(401, "totp_invalid")
    fails.pop(key, None)
    return {"token": new_token(u["id"], c.device or "web"), "username": u["username"]}


@app.post("/api/logout")
def logout(u=Depends(user)):
    conn.execute("DELETE FROM tokens WHERE hash=?", (u["token_hash"],))
    return {"ok": True}


@app.get("/api/me")
def me(u=Depends(user)):
    st = conn.execute("SELECT COUNT(*) n, COALESCE(SUM(size),0) s FROM docs WHERE user_id=? AND deleted=0", (u["id"],)).fetchone()
    kinds = {}
    for r in conn.execute("SELECT key FROM docs WHERE user_id=? AND deleted=0", (u["id"],)):
        k = r["key"].split("/")[0]
        kinds[k] = kinds.get(k, 0) + 1
    devices = [dict(id=r["hash"][:12], device=r["device"], created=r["created"], used=r["used"], current=r["hash"] == u["token_hash"])
               for r in conn.execute("SELECT * FROM tokens WHERE user_id=? ORDER BY used DESC", (u["id"],))]
    skills = [r["key"][6:] for r in conn.execute("SELECT key FROM docs WHERE user_id=? AND deleted=0 AND key LIKE 'skill/%' ORDER BY key",
                                                  (u["id"],))]
    return {"username": u["username"], "admin": bool(u["admin"]), "totp_enabled": bool(u["totp_on"]), "created": u["created"],
            "docs": st["n"], "bytes": st["s"], "kinds": kinds, "devices": devices, "skills": skills}


class Code(BaseModel):
    code: str = ""


@app.post("/api/totp/setup")
def totp_setup(u=Depends(user)):
    if u["totp_on"]:
        raise HTTPException(400, "already enabled")
    secret = base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")
    conn.execute("UPDATE users SET totp=? WHERE id=?", (secret, u["id"]))
    host = (PUBLIC or "me").split("//")[-1]
    return {"secret": secret, "uri": f"otpauth://totp/me:{u['username']}?secret={secret}&issuer=me%20({host})"}


@app.post("/api/totp/enable")
def totp_enable(c: Code, u=Depends(user)):
    if not u["totp"] or not totp_ok(u["totp"], c.code):
        raise HTTPException(400, "wrong code")
    conn.execute("UPDATE users SET totp_on=1 WHERE id=?", (u["id"],))
    return {"ok": True}


@app.post("/api/totp/disable")
def totp_disable(c: Code, u=Depends(user)):
    if u["totp_on"] and not totp_ok(u["totp"], c.code):
        raise HTTPException(400, "wrong code")
    conn.execute("UPDATE users SET totp_on=0, totp=NULL WHERE id=?", (u["id"],))
    return {"ok": True}


class PwChange(BaseModel):
    old: str
    new: str


@app.post("/api/password")
def password(c: PwChange, u=Depends(user)):
    if not pw_ok(c.old, u["pw"]):
        raise HTTPException(400, "wrong password")
    if len(c.new) < 6:
        raise HTTPException(400, "password: at least 6 chars")
    conn.execute("UPDATE users SET pw=? WHERE id=?", (pw_hash(c.new), u["id"]))
    conn.execute("DELETE FROM tokens WHERE user_id=? AND hash<>?", (u["id"], u["token_hash"]))
    return {"ok": True}


@app.delete("/api/devices/{tid}")
def revoke(tid: str, u=Depends(user)):
    conn.execute("DELETE FROM tokens WHERE user_id=? AND substr(hash,1,12)=?", (u["id"], tid))
    return {"ok": True}


# ─── sync ───


class Doc(BaseModel):
    key: str
    content: str = ""
    ts: float
    deleted: bool = False


class SyncReq(BaseModel):
    cursor: int = 0
    push: list[Doc] = []


@app.post("/api/sync")
def sync(r: SyncReq, u=Depends(user)):
    uid = u["id"]
    with lock:
        rev = conn.execute("SELECT COALESCE(MAX(rev),0) FROM docs WHERE user_id=?", (uid,)).fetchone()[0]
        pushed = set()
        conn.execute("BEGIN IMMEDIATE")
        for d in r.push:
            if not re.fullmatch(KEY_RX, d.key) or len(d.content) > MAX_DOC:
                continue
            cur = conn.execute("SELECT ts FROM docs WHERE user_id=? AND key=?", (uid, d.key)).fetchone()
            if cur and cur["ts"] > d.ts:
                continue  # server is newer; client gets it below
            rev += 1
            conn.execute("INSERT OR REPLACE INTO docs VALUES(?,?,?,?,?,?,?)",
                         (uid, d.key, "" if d.deleted else d.content, d.ts, rev, int(d.deleted), len(d.content)))
            pushed.add((d.key, rev))
        conn.execute("COMMIT")
        if pushed:
            notify()
        out = [dict(key=x["key"], content=x["content"], ts=x["ts"], deleted=bool(x["deleted"]))
               for x in conn.execute("SELECT * FROM docs WHERE user_id=? AND rev>? ORDER BY rev", (uid, r.cursor))
               if (x["key"], x["rev"]) not in pushed]
    return {"cursor": rev, "docs": out}


KEY_RX = r"(config|skill/[\w.-]+|plugin/[\w.-]+\.py|theme/[\w.-]+\.json|agent/[\w.-]+\.md|session/[\w.-]+)"


def put_doc(uid, key, content, deleted=False):
    with lock:
        rev = conn.execute("SELECT COALESCE(MAX(rev),0)+1 FROM docs WHERE user_id=?", (uid,)).fetchone()[0]
        conn.execute("INSERT OR REPLACE INTO docs VALUES(?,?,?,?,?,?,?)", (uid, key, "" if deleted else content, time.time(), rev, int(deleted), len(content)))
    notify()


class DocPut(BaseModel):
    key: str
    content: str


@app.get("/api/doc")
def doc_get(key: str, u=Depends(user)):
    r = conn.execute("SELECT content,ts FROM docs WHERE user_id=? AND key=? AND deleted=0", (u["id"], key)).fetchone()
    if not r:
        raise HTTPException(404, "not found")
    return {"key": key, "content": r["content"], "ts": r["ts"]}


@app.put("/api/doc")
def doc_put(d: DocPut, u=Depends(user)):
    if not re.fullmatch(KEY_RX, d.key) or len(d.content) > MAX_DOC:
        raise HTTPException(400, "bad key")
    if d.key == "config":
        try:
            json.loads(d.content)
        except ValueError:
            raise HTTPException(400, "config must be JSON")
    put_doc(u["id"], d.key, d.content)
    return {"ok": True}


@app.delete("/api/doc")
def doc_del(key: str, u=Depends(user)):
    put_doc(u["id"], key, "", deleted=True)
    return {"ok": True}


@app.get("/api/sessions")
def sessions(u=Depends(user)):
    out = []
    for r in conn.execute("SELECT key,content,ts FROM docs WHERE user_id=? AND deleted=0 AND key LIKE 'session/%' ORDER BY ts DESC", (u["id"],)):
        try:
            d = json.loads(r["content"])
        except ValueError:
            continue
        out.append({"id": d.get("id"), "title": d.get("title"), "cwd": d.get("cwd"), "updated": d.get("updated", r["ts"]),
                    "messages": len(d.get("messages", [])), "usage": d.get("usage", {})})
    return out


# ─── marketplace ───


def skill_meta(content):
    if content.lstrip().startswith("{"):  # theme JSON
        try:
            t = json.loads(content)
            return {"name": t.get("name", ""), "description": t.get("description") or t.get("label", ""), "kind": "theme",
                    "icon": "◐", "color": (t.get("colors") or {}).get("accent", "purple"), "default": "false"}
        except ValueError:
            return {}
    m = re.search(r'(?:"""|^---)\s*\n(.*?)\n---', content, re.S | re.M)
    meta = {}
    for line in (m.group(1).splitlines() if m else []):
        k, _, v = line.partition(":")
        if k.strip() and v.strip():
            meta[k.strip()] = v.strip()
    return meta


class Publish(BaseModel):
    content: str
    ext: str = "py"


@app.get("/api/market")
def market(q: str = "", kind: str = ""):
    rows = conn.execute("SELECT id,name,author,description,icon,color,ext,installs,updated,kind FROM market ORDER BY installs DESC, updated DESC").fetchall()
    q = q.lower()
    return [dict(r) for r in rows if (not q or q in (r["name"] + " " + (r["description"] or "")).lower()) and (not kind or r["kind"] == kind)]


def doc_key_for(r):
    return {"plugin": f"plugin/{r['name']}.py", "theme": f"theme/{r['name']}.json", "agent": f"agent/{r['name']}.md"}.get(
        r["kind"], f"skill/{r['name']}.{r['ext']}")


def seed_user(uid):
    """Install default plugins/skills for a user once."""
    for r in conn.execute("SELECT * FROM market WHERE is_default=1"):
        if not conn.execute("SELECT 1 FROM docs WHERE user_id=? AND key=?", (uid, doc_key_for(r))).fetchone():
            put_doc(uid, doc_key_for(r), r["content"])
    conn.execute("UPDATE users SET seeded=1 WHERE id=?", (uid,))


@app.get("/api/market/{mid}")
def market_get(mid: int):
    r = conn.execute("SELECT * FROM market WHERE id=?", (mid,)).fetchone()
    if not r:
        raise HTTPException(404)
    d = dict(r)
    d.pop("user_id")
    return d


@app.post("/api/market")
def market_publish(p: Publish, u=Depends(user)):
    meta = skill_meta(p.content)
    name = meta.get("name", "")
    if not re.fullmatch(r"[a-z0-9_-]{2,40}", name):
        raise HTTPException(400, "skill needs 'name: lowercase-name' in its header")
    if len(p.content) > 200_000:
        raise HTTPException(400, "too big")
    ext = "md" if p.ext == "md" else "py"
    kind = meta.get("kind") if meta.get("kind") in ("plugin", "theme", "agent") else "skill"
    ext = {"theme": "json", "agent": "md", "plugin": "py"}.get(kind, ext)
    cur = conn.execute("SELECT user_id FROM market WHERE name=?", (name,)).fetchone()
    if cur and cur["user_id"] != u["id"] and not u["admin"]:
        raise HTTPException(409, "name taken by another author")
    now = time.time()
    if cur:
        conn.execute("UPDATE market SET description=?,icon=?,color=?,ext=?,content=?,updated=?,kind=? WHERE name=?",
                     (meta.get("description", ""), meta.get("icon", "◆"), meta.get("color", "purple"), ext, p.content, now, kind, name))
    else:
        conn.execute("INSERT INTO market(name,author,user_id,description,icon,color,ext,content,created,updated,kind) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                     (name, u["username"], u["id"], meta.get("description", ""), meta.get("icon", "◆"), meta.get("color", "purple"), ext, p.content, now, now, kind))
    return {"ok": True, "name": name}


@app.post("/api/market/{mid}/install")
def market_install(mid: int, u=Depends(user)):
    r = conn.execute("SELECT * FROM market WHERE id=?", (mid,)).fetchone()
    if not r:
        raise HTTPException(404)
    put_doc(u["id"], doc_key_for(r), r["content"])
    conn.execute("UPDATE market SET installs=installs+1 WHERE id=?", (mid,))
    return {"ok": True}


@app.delete("/api/market/{mid}")
def market_delete(mid: int, u=Depends(user)):
    conn.execute("DELETE FROM market WHERE id=? AND (user_id=? OR ?)", (mid, u["id"], u["admin"]))
    return {"ok": True}


def seed_market():
    """Built-in skills/plugins from server/market are (re)published by the system author on start."""
    for p in sorted((HERE / "market").glob("*.*")):
        c = p.read_text("utf-8")
        meta = skill_meta(c)
        if not meta.get("name"):
            continue
        now = time.time()
        vals = (meta.get("description", ""), meta.get("icon", "◆"), meta.get("color", "purple"), p.suffix[1:], c,
                meta.get("kind") if meta.get("kind") in ("plugin", "theme", "agent") else "skill", int(meta.get("default") == "true"))
        if conn.execute("SELECT 1 FROM market WHERE name=?", (meta["name"],)).fetchone():
            conn.execute("UPDATE market SET description=?,icon=?,color=?,ext=?,content=?,kind=?,is_default=?,updated=? WHERE name=? AND user_id=0",
                         (*vals, now, meta["name"]))
        else:
            conn.execute("INSERT INTO market(name,author,user_id,description,icon,color,ext,content,kind,is_default,created,updated) "
                         "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (meta["name"], "me", 0, *vals, now, now))
    for r in conn.execute("SELECT id FROM users WHERE seeded=0").fetchall():
        seed_user(r["id"])


seed_market()


@app.get("/api/changes")
def changes(cursor: int = 0, ws: int = -1, wait: int = 25, u=Depends(user)):
    """Long-poll: returns as soon as the user's data moves past `cursor` (or after `wait` seconds)."""
    end = time.time() + min(wait, 50)
    while True:
        rev = conn.execute("SELECT COALESCE(MAX(rev),0) FROM docs WHERE user_id=?", (u["id"],)).fetchone()[0]
        wsr = ws_rev.get(u["id"], 0)
        ws_changed = ws >= 0 and wsr != ws
        if rev > cursor or ws_changed or time.time() >= end:
            return {"cursor": rev, "changed": rev > cursor, "ws": wsr, "ws_changed": ws_changed, "version": client_version()}
        with changed:
            changed.wait(max(0.1, end - time.time()))


@app.get("/api/docs")
def docs(u=Depends(user)):
    return [dict(key=r["key"], ts=r["ts"], size=r["size"]) for r in
            conn.execute("SELECT key,ts,size FROM docs WHERE user_id=? AND deleted=0 ORDER BY ts DESC", (u["id"],))]


@app.get("/api/admin/users")
def admin_users(u=Depends(user)):
    if not u["admin"]:
        raise HTTPException(403, "admin only")
    rows = conn.execute("""SELECT u.id,u.username,u.created,u.seen,u.totp_on,u.admin,
        (SELECT COUNT(*) FROM docs d WHERE d.user_id=u.id AND d.deleted=0) docs,
        (SELECT COALESCE(SUM(size),0) FROM docs d WHERE d.user_id=u.id AND d.deleted=0) bytes,
        (SELECT COUNT(*) FROM tokens t WHERE t.user_id=u.id) devices FROM users u ORDER BY u.seen DESC""").fetchall()
    return [dict(r) for r in rows]


# ─── client distribution + site ───


def base_url(req: Request):
    if PUBLIC:
        return PUBLIC
    proto = req.headers.get("x-forwarded-proto", req.url.scheme)
    return f"{proto}://{req.headers.get('host', req.url.netloc)}"


def client_version():
    m = re.search(r'^VERSION = "([^"]+)"', CLIENT.read_text("utf-8"), re.M)
    return m.group(1) if m else "0"


@app.get("/api/version")
def version():
    return {"version": client_version()}


@app.get("/me.py")
def client(req: Request):
    src = CLIENT.read_text("utf-8")
    src = re.sub(r'^SERVER = .*$', f'SERVER = os.environ.get("ME_SERVER", "{base_url(req)}")', src, count=1, flags=re.M)
    return PlainTextResponse(src, media_type="text/x-python")


@app.get("/i")
@app.get("/install.sh")
def install_sh(req: Request):
    return PlainTextResponse((HERE / "install.sh").read_text().replace("__SERVER__", base_url(req)), media_type="text/x-shellscript")


@app.get("/i.ps1")
@app.get("/install.ps1")
def install_ps(req: Request):
    return PlainTextResponse((HERE / "install.ps1").read_text().replace("__SERVER__", base_url(req)))


def page(name, req):
    html = (HERE / "site" / name).read_text("utf-8")
    return HTMLResponse(html.replace("__SERVER__", base_url(req)).replace("__VERSION__", client_version()))


@app.get("/")
def index(req: Request):
    return page("index.html", req)


@app.get("/account")
def account(req: Request):
    return page("account.html", req)


@app.get("/favicon.svg")
def favicon():
    return FileResponse(HERE / "site" / "favicon.svg", media_type="image/svg+xml")


@app.get("/market")
def market_page(req: Request):
    return page("account.html", req)


@app.get("/skills/{name}")
def raw_skill(name: str):
    r = conn.execute("SELECT content FROM market WHERE name=?", (name.rsplit(".", 1)[0],)).fetchone()
    if not r:
        raise HTTPException(404)
    return PlainTextResponse(r["content"])


@app.get("/healthz")
def health():
    return Response("ok")


@app.on_event("startup")
async def _threads():
    import anyio.to_thread
    anyio.to_thread.current_default_thread_limiter().total_tokens = 300  # long-polls hold threads


import routes_cloud  # noqa: E402,F401  (registers workspace/cloud/telegram/admin routes)
