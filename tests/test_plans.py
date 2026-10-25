"""Subscriptions and Me AI: the proxy adds the persona, hides the upstream, counts usage into the 5-hour/weekly windows, stops at the limit.
Run: python3 tests/test_plans.py   (starts its own server + mock LLM as the upstream; needs fastapi/uvicorn)"""
import json, os, sqlite3, sys, tempfile, time

sys.path.insert(0, os.path.dirname(__file__))
import test_restart as T  # noqa: E402

T.env["ME_AI_URL"] = f"http://127.0.0.1:{T.llm.server_port}/v1"
T.env["ME_AI_MODEL"] = "mock-1"
me, mock = T.me, T.mock_llm


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
    finally:
        os.killpg(srv.pid, 9)


if __name__ == "__main__":
    main()
