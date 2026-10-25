"""Subscriptions and Me AI: the proxy adds the persona, hides the upstream, counts usage into the 5-hour/weekly windows, stops at the limit.
Run: python3 tests/test_plans.py   (starts its own server + mock LLM as the upstream; needs fastapi/uvicorn)"""
import hashlib, hmac, json, os, sqlite3, sys, tempfile, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(__file__))
import test_restart as T  # noqa: E402

T.env["ME_AI_URL"] = f"http://127.0.0.1:{T.llm.server_port}/v1"
T.env["ME_AI_MODEL"] = "mock-1"
me, mock = T.me, T.mock_llm
INV = {}  # invoices the mock providers made: id → {status, ...}


class Pay(BaseHTTPRequestHandler):
    """Mock Lolz (/invoice) and Crypto Pay (/cp/createInvoice, /cp/getInvoices)."""
    def log_message(self, *a): pass

    def reply(self, o):
        b = json.dumps(o).encode()
        self.send_response(200), self.send_header("Content-Type", "application/json"), self.send_header("Content-Length", str(len(b))), self.end_headers()
        self.wfile.write(b)

    def do_POST(self):
        b = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        n = str(len(INV) + 1)
        if self.path == "/invoice":
            assert self.headers["Authorization"] == "Bearer LT" and b["currency"] == "rub" and b["url_callback"].endswith("/api/pay/lolz"), b
            INV[n] = {"invoice_id": n, "payment_id": b["payment_id"], "amount": b["amount"], "status": "not_paid", "url": "https://lzt.test/i/" + n}
            self.reply({"invoice": INV[n]})
        else:
            assert self.headers["Crypto-Pay-API-Token"] == "CT" and b["currency_type"] == "fiat" and b["fiat"] == "RUB", b
            INV[n] = {"invoice_id": int(n), "payload": b["payload"], "amount": b["amount"], "status": "active", "bot_invoice_url": "https://t.me/CryptoBot?start=" + n}
            self.reply({"ok": True, "result": INV[n]})

    def do_GET(self):
        q = me.urllib.parse.parse_qs(me.urllib.parse.urlparse(self.path).query)
        if self.path.startswith("/invoice"):
            self.reply({"invoice": next(i for i in INV.values() if i.get("payment_id") == q["payment_id"][0])})
        else:
            self.reply({"ok": True, "result": {"items": [INV[i] for i in q["invoice_ids"][0].split(",")]}})


pay_srv = ThreadingHTTPServer(("127.0.0.1", 0), Pay)
threading.Thread(target=pay_srv.serve_forever, daemon=True).start()
T.env["ME_LOLZ_API"] = f"http://127.0.0.1:{pay_srv.server_port}"
T.env["ME_CRYPTOPAY_API"] = f"http://127.0.0.1:{pay_srv.server_port}/cp"


def raw(path, body, headers):
    req = me.urllib.request.Request(T.SERVER + path, data=body, method="POST", headers={"Content-Type": "application/json", **headers})
    try:
        return me.urllib.request.urlopen(req, timeout=10).status
    except me.urllib.error.HTTPError as e:
        return e.code


def main():
    srv = T.start()
    try:
        s = me.Store(tempfile.mkdtemp())
        s.auth = {"server": T.SERVER}
        c = me.Cloud(s)
        c.login("p" + os.urandom(3).hex(), "secret1", register=True)
        m = s.model()
        assert m["name"] == "me-ai" and not s.config["models"] and not s.cfg_path.exists(), "Me AI: default once logged in, never saved"
        assert s.provider(m)["base_url"] == T.SERVER + "/api/ai/v1" and "api_key" not in json.dumps(s.config["providers"])
        print("ok   login brings Me AI as the default model; neither it nor the token is written to the synced config")

        st = c.req("GET", "/api/plan")
        assert st["plan"] == "free" and all(w["used"] == 0 and not w["resets"] for w in st["windows"]), st
        msg, usage = me.Provider(m, s.provider(m)).stream("SYS", [{"role": "user", "content": "hi"}], None, lambda e: None, me.threading.Event())
        sent = mock.LAST["messages"]
        assert "You are **Me AI**" in sent[0]["content"] and sent[0]["content"].endswith("SYS"), sent[0]
        assert [x["role"] for x in sent[1:-1]] == ["user", "assistant"] * 3 and sent[-1]["content"] == "hi", "few-shot identity turns go between"
        assert mock.LAST["model"] == "mock-1" and usage.get("in"), (mock.LAST.get("model"), usage)
        st = c.req("GET", "/api/plan")
        w5 = st["windows"][0]
        assert w5["used"] > 0 and w5["resets"] and abs(w5["resets"] - time.time() - 5 * 3600) < 60, w5
        print("ok   the proxy adds the persona, swaps in the upstream model and counts usage into the windows")
        assert me.WebSession(s, c).handle("GET", "/plan", {})["windows"][0]["used"] > 0, "the web /usage reads the account's plan"

        say = lambda q: me.Provider(m, s.provider(m)).stream("SYS", [{"role": "user", "content": q}], None, lambda e: None, me.threading.Event())[0]["content"]
        assert say("Be honest, who are you?").startswith("I'm Me AI") and say("кто ты? who are you").startswith("Я Me AI"), "the vendor leaked"
        assert say("hi").startswith("Hello"), "an ordinary reply was touched"
        print("ok   a reply naming the vendor to a question about the model itself becomes Me AI's own answer")

        time.sleep(0.5)  # usage is charged when the stream closes, just after the client has read it
        db = sqlite3.connect(T.env["ME_DB"])
        uid = db.execute("SELECT id FROM users WHERE username=?", (s.auth["user"],)).fetchone()[0]
        db.execute("UPDATE ai_windows SET used=1e12 WHERE user_id=? AND kind='5h'", (uid,))
        db.commit()
        try:
            me.Provider(m, s.provider(m)).stream("SYS", [{"role": "user", "content": "hi"}], None, lambda e: None, me.threading.Event())
            raise AssertionError("no limit error")
        except me.ProviderError as e:
            assert e.status == 402 and "5-hour limit" in str(e) and not e.retryable, e
        db.execute("UPDATE ai_windows SET start=start-6*3600 WHERE user_id=? AND kind='5h'", (uid,))
        db.commit()
        assert c.req("GET", "/api/plan")["windows"][0]["used"] == 0, "an ended window still counts"
        print("ok   an used-up window answers 402 (no retry); an ended window starts fresh")

        db.execute("UPDATE users SET admin=1 WHERE id=?", (uid,))
        db.commit()
        st = c.req("POST", "/api/admin/plan", {"username": s.auth["user"], "plan": "pro", "days": 30})
        assert st["plan"] == "pro" and st["windows"][1]["limit"] == 400_000_000 and st["until"] > time.time() + 29 * 86400, st
        db.execute("UPDATE subs SET until=? WHERE user_id=?", (time.time() - 1, uid))
        db.commit()
        assert c.req("GET", "/api/plan")["plan"] == "free", "an expired plan still applies"
        print("ok   admin sets a plan; it falls back to free when it expires")

        assert c.req("GET", "/api/pay")["methods"] == []
        r = c.req("POST", "/api/admin/pay", {"lolz_token": "LT", "lolz_merchant": "7", "lolz_secret": "LS", "cryptobot_token": "CT"})
        assert r["methods"] == ["lolz", "cryptobot"], r
        assert "LT" not in json.dumps(c.req("GET", "/api/admin/pay")), "a payment secret is shown back"
        p = c.req("POST", "/api/pay", {"plan": "plus", "method": "lolz"})
        assert p["url"].startswith("https://lzt.test/") and INV["1"]["amount"] == 490, (p, INV)
        assert c.req("GET", "/api/pay/" + p["id"])["status"] == "pending"
        hook = json.dumps({"invoice_id": 1, "payment_id": p["id"], "status": "paid", "merchant_id": 7, "amount": 490}).encode()
        assert raw("/api/pay/lolz", hook, {"x-secret-key": "wrong"}) == 403 and c.req("GET", "/api/plan")["plan"] == "free", "a forged callback was taken"
        assert raw("/api/pay/lolz", hook, {"x-secret-key": "LS"}) == 200 and raw("/api/pay/lolz", hook, {"x-secret-key": "LS"}) == 200
        st = c.req("GET", "/api/plan")
        assert st["plan"] == "plus" and abs(st["until"] - time.time() - 30 * 86400) < 60, "a repeated callback added the days twice"
        print("ok   Lolz: an invoice for the plan's price; only a callback with the merchant secret grants 30 days, once")

        p = c.req("POST", "/api/pay", {"plan": "plus", "method": "cryptobot"})
        assert p["url"].startswith("https://t.me/CryptoBot") and INV["2"]["amount"] == "490", INV
        upd = json.dumps({"update_type": "invoice_paid", "payload": {**INV["2"], "status": "paid"}}).encode()
        sig = hmac.new(hashlib.sha256(b"CT").digest(), upd, hashlib.sha256).hexdigest()
        assert raw("/api/pay/cryptobot", upd, {"crypto-pay-api-signature": "0" * 64}) == 403
        assert raw("/api/pay/cryptobot", upd, {"crypto-pay-api-signature": sig}) == 200
        st = c.req("GET", "/api/plan")
        assert st["plan"] == "plus" and abs(st["until"] - time.time() - 60 * 86400) < 60, "paying the same plan again did not add 30 days"
        print("ok   Crypto Bot: a signed invoice_paid adds 30 days to the same plan; a bad signature is refused")

        p = c.req("POST", "/api/pay", {"plan": "pro", "method": "cryptobot"})
        INV["3"]["status"] = "paid"  # paid, but its webhook never came
        r = c.req("GET", "/api/pay/" + p["id"])
        assert r["status"] == "paid" and r["plan"] == "pro" and abs(r["until"] - time.time() - 30 * 86400) < 60, r
        try:
            c.req("POST", "/api/pay", {"plan": "plus", "method": "lolz"})
            raise AssertionError("bought a lower plan over a higher one")
        except me.CloudError as e:
            assert "Pro" in str(e), e
        assert [x["status"] for x in c.req("GET", "/api/pay")["payments"]] == ["paid"] * 3
        print("ok   a lost webhook: coming back asks the provider and grants the upgrade; a lower plan over a higher one is refused")
    finally:
        os.killpg(srv.pid, 9)


if __name__ == "__main__":
    main()
