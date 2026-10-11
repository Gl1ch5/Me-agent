"""me cloud — accounts, optional TOTP, last-write-wins doc sync, installer + site.
Run: uvicorn app:app --host 127.0.0.1 --port 8790
Env: ME_DB (sqlite path), ME_CLIENT (path to me.py), ME_PUBLIC_URL (e.g. https://me-agent.ru)
"""
import base64, hashlib, hmac, os, re, secrets, sqlite3, struct, threading, time
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, Response
from pydantic import BaseModel

HERE = Path(__file__).resolve().parent
DB = os.environ.get("ME_DB", str(HERE / "me.db"))
CLIENT = Path(os.environ.get("ME_CLIENT", HERE.parent / "me.py"))
PUBLIC = os.environ.get("ME_PUBLIC_URL", "").rstrip("/")
MAX_DOC = 20 * 1024 * 1024

app = FastAPI(title="me cloud", docs_url=None, redoc_url=None)
lock = threading.Lock()


def db():
    c = sqlite3.connect(DB, check_same_thread=False, isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    return c


conn = db()
conn.executescript("""
CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, username TEXT UNIQUE COLLATE NOCASE, pw TEXT, totp TEXT, totp_on INTEGER DEFAULT 0,
  admin INTEGER DEFAULT 0, created REAL, seen REAL);
CREATE TABLE IF NOT EXISTS tokens(hash TEXT PRIMARY KEY, user_id INTEGER, device TEXT, created REAL, used REAL);
CREATE TABLE IF NOT EXISTS docs(user_id INTEGER, key TEXT, content TEXT, ts REAL, rev INTEGER, deleted INTEGER DEFAULT 0, size INTEGER,
  PRIMARY KEY(user_id, key));
CREATE INDEX IF NOT EXISTS docs_rev ON docs(user_id, rev);
""")

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
        conn.execute("BEGIN")
        for d in r.push:
            if not re.fullmatch(r"(config|skill/[\w.-]+|session/[\w.-]+)", d.key) or len(d.content) > MAX_DOC:
                continue
            cur = conn.execute("SELECT ts FROM docs WHERE user_id=? AND key=?", (uid, d.key)).fetchone()
            if cur and cur["ts"] > d.ts:
                continue  # server is newer; client gets it below
            rev += 1
            conn.execute("INSERT OR REPLACE INTO docs VALUES(?,?,?,?,?,?,?)",
                         (uid, d.key, "" if d.deleted else d.content, d.ts, rev, int(d.deleted), len(d.content)))
            pushed.add((d.key, rev))
        conn.execute("COMMIT")
        out = [dict(key=x["key"], content=x["content"], ts=x["ts"], deleted=bool(x["deleted"]))
               for x in conn.execute("SELECT * FROM docs WHERE user_id=? AND rev>? ORDER BY rev", (uid, r.cursor))
               if (x["key"], x["rev"]) not in pushed]
    return {"cursor": rev, "docs": out}


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


@app.get("/skills/{name}")
def example_skill(name: str):
    p = HERE / "skills" / name
    if not re.fullmatch(r"[\w.-]+", name) or not p.exists():
        raise HTTPException(404)
    return PlainTextResponse(p.read_text("utf-8"))


@app.get("/healthz")
def health():
    return Response("ok")
