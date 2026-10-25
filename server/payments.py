"""Paying for a plan: Lolz (LZT Market merchant invoices) and Crypto Bot (Crypto Pay API). Neither needs the seller's personal data.

- POST /api/pay {plan, method} makes an invoice for 30 days and returns its url; the user pays on the provider's page.
- The provider calls back: /api/pay/lolz carries header x-secret-key = the merchant's secret key; /api/pay/cryptobot carries
  crypto-pay-api-signature = hex HMAC-SHA256 of the body with key SHA256(app token). The Crypto Bot webhook URL is set once
  in @CryptoBot → Crypto Pay → My apps. A lost callback is covered by GET /api/pay/{id}, which asks the provider itself.
- A paid invoice adds 30 days: to the current end when it is the same plan, else from now.
- Referrals (link /r/<username>): only a real payment rewards, so fake accounts earn nothing. The friend's first payment
  gets ref_bonus extra days; the inviter gets ref_pct % of every payment of the friend back as days of their own plan (Plus while free).
Settings (admin → payments): lolz_token (scope invoice), lolz_merchant, lolz_secret; cryptobot_token, cryptobot_net (main/test);
ref_pct (default 20), ref_bonus (days, default 7); 0 turns either off.
"""
import hashlib, hmac, json, os, time, urllib.error, urllib.parse, urllib.request, uuid

from fastapi import Depends, HTTPException, Request
from pydantic import BaseModel

import plans
from app import PUBLIC, app, base_url, conn, setting, user

conn.executescript("""
CREATE TABLE IF NOT EXISTS payments(id TEXT PRIMARY KEY, user_id INTEGER, plan TEXT, method TEXT, amount REAL, status TEXT,
  ext TEXT DEFAULT '', url TEXT DEFAULT '', created REAL, paid REAL);
CREATE INDEX IF NOT EXISTS payments_user ON payments(user_id, created);
CREATE TABLE IF NOT EXISTS ref_rewards(payment TEXT PRIMARY KEY, referrer INTEGER, plan TEXT, days REAL, created REAL);
""")
DAYS = 30
LOLZ = os.environ.get("ME_LOLZ_API", "https://prod-api.lzt.market")
KEYS = ("lolz_token", "lolz_merchant", "lolz_secret", "cryptobot_token", "cryptobot_net", "ref_pct", "ref_bonus")


def num(key, default):
    try:
        return max(0.0, float(setting(key) or default))
    except ValueError:
        return float(default)


def cryptobot_api():
    return os.environ.get("ME_CRYPTOPAY_API") or ("https://testnet-pay.crypt.bot/api" if setting("cryptobot_net") == "test" else "https://pay.crypt.bot/api")


def methods():
    return [m for m, ok in (("lolz", setting("lolz_token") and setting("lolz_merchant")), ("cryptobot", setting("cryptobot_token"))) if ok]


def http(method, url, body=None, headers=None):
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None, method=method,
                                 headers={"Content-Type": "application/json", "Accept": "application/json", "User-Agent": "me", **(headers or {})})
    try:
        return json.loads(urllib.request.urlopen(req, timeout=20).read() or b"{}")
    except urllib.error.HTTPError as e:
        raise HTTPException(502, f"payment provider: {e.code} {e.read()[:300].decode('utf-8', 'replace')}")
    except (OSError, ValueError) as e:
        raise HTTPException(502, f"payment provider: {e}")


# ─── providers: create an invoice → (external id, url); paid(payment) → bool ───


def lolz_create(p, base):
    r = http("POST", LOLZ + "/invoice", {
        "currency": "rub", "amount": p["amount"], "payment_id": p["id"], "comment": f"me · Me AI {plans.PLANS[p['plan']]['title']} · {DAYS} days",
        "url_success": base + "/account#plans", "url_callback": base + "/api/pay/lolz", "merchant_id": int(setting("lolz_merchant")), "lifetime": 3600},
        {"Authorization": "Bearer " + setting("lolz_token")})
    inv = r.get("invoice") or {}
    if not inv.get("url"):
        raise HTTPException(502, "payment provider: " + json.dumps(r)[:300])
    return str(inv.get("invoice_id", "")), inv["url"]


def lolz_paid(p):
    r = http("GET", LOLZ + "/invoice?" + urllib.parse.urlencode({"payment_id": p["id"]}), None, {"Authorization": "Bearer " + setting("lolz_token")})
    return (r.get("invoice") or {}).get("status") == "paid"


def cryptobot_create(p, base):
    r = http("POST", cryptobot_api() + "/createInvoice", {
        "currency_type": "fiat", "fiat": "RUB", "amount": f"{p['amount']:g}", "payload": p["id"], "expires_in": 3600,
        "description": f"me · Me AI {plans.PLANS[p['plan']]['title']} · {DAYS} days",
        **({"paid_btn_name": "callback", "paid_btn_url": base + "/account#plans"} if base.startswith("https://") else {})},
        {"Crypto-Pay-API-Token": setting("cryptobot_token")})
    inv = r.get("result") or {}
    url = inv.get("bot_invoice_url") or inv.get("pay_url")
    if not r.get("ok") or not url:
        raise HTTPException(502, "payment provider: " + json.dumps(r.get("error") or r)[:300])
    return str(inv.get("invoice_id", "")), url


def cryptobot_paid(p):
    r = http("GET", cryptobot_api() + "/getInvoices?" + urllib.parse.urlencode({"invoice_ids": p["ext"]}), None,
             {"Crypto-Pay-API-Token": setting("cryptobot_token")})
    return any(i.get("status") == "paid" for i in (r.get("result") or {}).get("items", []))


PROVIDERS = {"lolz": (lolz_create, lolz_paid), "cryptobot": (cryptobot_create, cryptobot_paid)}


def add_days(uid, plan, days, note, now):
    """Extend plan by days: from its end when it is the current plan, else from now. A paid plan given forever stays as it is."""
    cur, until = plans.plan_of(uid)
    if days <= 0 or (cur == plan and until is None):
        return
    start = until if cur == plan else now
    conn.execute("INSERT OR REPLACE INTO subs VALUES(?,?,?,?,?)", (uid, plan, start + days * 86400, note, now))


def reward(p, now):
    r = conn.execute("SELECT referrer FROM referrals WHERE user_id=?", (p["user_id"],)).fetchone()
    if not r:
        return
    if not conn.execute("SELECT 1 FROM payments WHERE user_id=? AND status='paid' AND id<>?", (p["user_id"], p["id"])).fetchone():
        add_days(p["user_id"], p["plan"], num("ref_bonus", 7), f"invite bonus {p['id']}", now)
    cur, _ = plans.plan_of(r["referrer"])
    plan = cur if plans.PLANS[cur]["price"] else "plus"
    days = round(p["amount"] * num("ref_pct", 20) / 100 / (plans.PLANS[plan]["price"] / DAYS), 1)
    if days > 0:
        add_days(r["referrer"], plan, days, f"referral {p['id']}", now)
        conn.execute("INSERT OR IGNORE INTO ref_rewards VALUES(?,?,?,?,?)", (p["id"], r["referrer"], plan, days, now))


def grant(pid):
    """Mark the payment paid, extend the plan and reward the inviter; safe to call twice (webhook + check)."""
    with plans.lock:
        p = conn.execute("SELECT * FROM payments WHERE id=?", (pid,)).fetchone()
        if not p or p["status"] == "paid":
            return
        now = time.time()
        add_days(p["user_id"], p["plan"], DAYS, f"paid {p['method']} {pid}", now)
        reward(p, now)
        conn.execute("UPDATE payments SET status='paid', paid=? WHERE id=?", (now, pid))


# ─── endpoints ───


class Pay(BaseModel):
    plan: str
    method: str


@app.get("/api/pay")
def pay_info(u=Depends(user)):
    rows = conn.execute("SELECT id, plan, method, amount, status, created, paid FROM payments WHERE user_id=? ORDER BY created DESC LIMIT 20", (u["id"],))
    return {"methods": methods(), "days": DAYS, "payments": [dict(r) for r in rows]}


@app.post("/api/pay")
def pay_create(b: Pay, req: Request, u=Depends(user)):
    if b.method not in methods():
        raise HTTPException(400, "this payment method is not available")
    if b.plan not in plans.PLANS or not plans.PLANS[b.plan]["price"]:
        raise HTTPException(400, "no such paid plan")
    cur, until = plans.plan_of(u["id"])
    if plans.PLANS[cur]["price"] > plans.PLANS[b.plan]["price"]:
        raise HTTPException(400, f"you have {plans.PLANS[cur]['title']} until {time.strftime('%d.%m.%Y', time.localtime(until)) if until else 'forever'}")
    p = {"id": uuid.uuid4().hex, "user_id": u["id"], "plan": b.plan, "method": b.method, "amount": float(plans.PLANS[b.plan]["price"])}
    ext, url = PROVIDERS[b.method][0](p, base_url(req))
    conn.execute("INSERT INTO payments VALUES(?,?,?,?,?,?,?,?,?,?)", (p["id"], u["id"], b.plan, b.method, p["amount"], "pending", ext, url, time.time(), None))
    return {"id": p["id"], "url": url}


@app.get("/api/pay/{pid}")
def pay_check(pid: str, u=Depends(user)):
    """Back from the provider's page: confirm with the provider itself in case its callback never came."""
    p = conn.execute("SELECT * FROM payments WHERE id=? AND user_id=?", (pid, u["id"])).fetchone()
    if not p:
        raise HTTPException(404, "no such payment")
    if p["status"] == "pending" and p["method"] in methods() and PROVIDERS[p["method"]][1](p):
        grant(pid)
    return {"status": conn.execute("SELECT status FROM payments WHERE id=?", (pid,)).fetchone()[0], **plans.status(u["id"])}


@app.post("/api/pay/lolz")
async def lolz_hook(req: Request):
    secret = setting("lolz_secret")
    if not secret or not hmac.compare_digest(req.headers.get("x-secret-key", ""), secret):
        raise HTTPException(403, "bad secret")
    b = json.loads(await req.body() or b"{}")
    if b.get("status") == "paid" and str(b.get("merchant_id")) == setting("lolz_merchant"):
        grant(str(b.get("payment_id", "")))
    return {"ok": True}


@app.post("/api/pay/cryptobot")
async def cryptobot_hook(req: Request):
    raw, token = await req.body(), setting("cryptobot_token")
    sig = hmac.new(hashlib.sha256(token.encode()).digest(), raw, hashlib.sha256).hexdigest()
    if not token or not hmac.compare_digest(req.headers.get("crypto-pay-api-signature", ""), sig):
        raise HTTPException(403, "bad signature")
    b = json.loads(raw or b"{}")
    inv = b.get("payload") or {}
    if b.get("update_type") == "invoice_paid" and inv.get("status") == "paid":
        p = conn.execute("SELECT id FROM payments WHERE id=? AND method='cryptobot' AND ext=?", (str(inv.get("payload", "")), str(inv.get("invoice_id", "")))).fetchone()
        if p:
            grant(p["id"])
    return {"ok": True}


def ref_stats(u, base):
    """The invite link and what it brought."""
    invited = conn.execute("SELECT COUNT(*) FROM referrals WHERE referrer=?", (u["id"],)).fetchone()[0]
    paid = conn.execute("SELECT COUNT(DISTINCT r.user_id) FROM referrals r JOIN payments p ON p.user_id=r.user_id AND p.status='paid' WHERE r.referrer=?",
                        (u["id"],)).fetchone()[0]
    days = conn.execute("SELECT plan, SUM(days) d FROM ref_rewards WHERE referrer=? GROUP BY plan", (u["id"],)).fetchall()
    import routes_cloud  # the shared bot: t.me/<bot>?start=r<id> opens sign-up with this invite
    bot = routes_cloud.bot.username if routes_cloud.bot.token else ""
    return {"link": base + "/r/" + urllib.parse.quote(u["username"]), "tg": f"https://t.me/{bot}?start=r{u['id']}" if bot else None,
            "invited": invited, "paid": paid,
            "days": {r["plan"]: round(r["d"], 1) for r in days}, "pct": num("ref_pct", 20), "bonus": num("ref_bonus", 7)}


def ref_text(u, ru=False):
    """/invite in Telegram."""
    st = ref_stats(u, PUBLIC or "https://me-agent.ru")
    earned = ", ".join(f"{plans.PLANS[k]['title']} +{v:g} {'дн.' if ru else 'days'}" for k, v in st["days"].items()) or "—"
    if ru:
        return (f"Пригласить друзей\n{st['tg'] or st['link']}\n\nДруг получает +{st['bonus']:g} дн. к первой оплате тарифа.\n"
                f"Вы получаете {st['pct']:g}% от каждой его оплаты днями своего тарифа (Plus, если у вас Free).\n\n"
                f"Приглашено: {st['invited']} · оплатили: {st['paid']} · получено: {earned}")
    return (f"Invite friends\n{st['tg'] or st['link']}\n\nA friend gets +{st['bonus']:g} days on their first plan payment.\n"
            f"You get {st['pct']:g}% of each of their payments back as days of your plan (Plus while you are on Free).\n\n"
            f"Invited: {st['invited']} · paid: {st['paid']} · earned: {earned}")


@app.get("/api/ref")
def ref_info(req: Request, u=Depends(user)):
    return ref_stats(u, base_url(req))


class PaySettings(BaseModel):
    lolz_token: str | None = None
    lolz_merchant: str | None = None
    lolz_secret: str | None = None
    cryptobot_token: str | None = None
    cryptobot_net: str | None = None
    ref_pct: str | None = None
    ref_bonus: str | None = None


def _admin(u):
    if not u["admin"]:
        raise HTTPException(403, "admin only")


@app.get("/api/admin/pay")
def admin_pay(req: Request, u=Depends(user)):
    _admin(u)
    rows = conn.execute("SELECT p.created, u.username, p.plan, p.method, p.amount, p.status FROM payments p JOIN users u ON u.id=p.user_id "
                        "ORDER BY p.created DESC LIMIT 50").fetchall()
    return {"set": {k: bool(setting(k)) for k in KEYS}, "lolz_merchant": setting("lolz_merchant"), "cryptobot_net": setting("cryptobot_net") or "main",
            "ref_pct": num("ref_pct", 20), "ref_bonus": num("ref_bonus", 7),
            "cryptobot_webhook": base_url(req) + "/api/pay/cryptobot", "methods": methods(), "payments": [dict(r) for r in rows]}


@app.post("/api/admin/pay")
def admin_pay_set(s: PaySettings, u=Depends(user)):
    _admin(u)
    for k in KEYS:
        v = getattr(s, k)
        if v is not None:
            setting(k, v.strip())
    return {"ok": True, "methods": methods()}
