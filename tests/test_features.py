"""Server features: jobs (background + cron), preview URLs, marketplace versions/ratings/auto-update, web push crypto, notify, undo/goal.
Run: python3 tests/test_features.py   (starts its own server + mock LLM; needs fastapi/uvicorn/cryptography)"""
import base64, json, os, sys, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_restart as T  # noqa: E402  (server + mock LLM helpers)

me = T.me


def wait_job(c, jid, timeout=40):
    end = time.time() + timeout
    while time.time() < end:
        j = next(x for x in c.req("GET", "/api/jobs") if x["id"] == jid)
        if j["status"] in ("done", "error"):
            return j
        time.sleep(0.5)
    raise AssertionError("job did not finish")


def main():
    sys.path.insert(0, str(T.ROOT / "server"))
    os.environ.setdefault("ME_DB", T.env["ME_DB"])
    os.environ.setdefault("ME_DATA", T.env["ME_DATA"])
    import features  # noqa: F401  (pure helpers below; the server under test is a separate process)

    # cron
    t0 = 1_760_000_000  # 2025-10-09 08:53:20 UTC
    assert time.strftime("%H:%M", time.gmtime(features.cron_next("30 9 * * *", t0))) == "09:30"
    assert features.cron_next("*/15 * * * *", t0) - t0 < 15 * 60
    assert time.strftime("%H:%M", time.gmtime(features.cron_next("0 9 * * *", t0, tz_min=180))) == "06:00"  # 09:00 in UTC+3
    print("ok   cron schedule parsing")

    # push encryption round-trip (decrypt like a browser would, RFC 8291)
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    ua = ec.generate_private_key(ec.SECP256R1())
    ua_pub = ua.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    auth = os.urandom(16)
    body = features.encrypt_push(b'{"title":"hi"}', features.b64u(ua_pub), features.b64u(auth))
    salt, idlen = body[:16], body[20]
    as_pub, ct = body[21:21 + idlen], body[21 + idlen:]
    shared = ua.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), as_pub))
    hk = lambda s, i, info, n: HKDF(hashes.SHA256(), n, s, info).derive(i)
    prk = hk(auth, shared, b"WebPush: info\0" + ua_pub + as_pub, 32)
    plain = AESGCM(hk(salt, prk, b"Content-Encoding: aes128gcm\0", 16)).decrypt(hk(salt, prk, b"Content-Encoding: nonce\0", 12), ct, None)
    assert plain == b'{"title":"hi"}\x02', plain
    print("ok   web push payload decrypts (aes128gcm)")

    srv = T.start()
    try:
        c = T.login()
        assert c.req("GET", "/api/push/key")["key"]

        # background job: runs in a sandbox with nobody watching, result stored
        ws = c.req("POST", "/api/ws", {"name": "jobs"})["id"]
        j = c.req("POST", "/api/jobs", {"prompt": 'tool:write {"path":"site/index.html","content":"<h1>built by a job</h1>"}', "ws": ws})
        j = wait_job(c, j["id"])
        assert j["status"] == "done" and j["session"], j
        assert not j["enabled"], "one-off job stays disabled after running"
        print("ok   background job ran in the cloud and recorded its result")

        # `later`: a reminder continues the same chat (not a new one); only for chats the cloud has
        try:
            c.req("POST", "/api/jobs", {"prompt": "x", "session": "nope", "minutes": 5})
            raise AssertionError("unsynced chat accepted")
        except me.CloudError:
            pass
        r = c.req("POST", "/api/jobs", {"prompt": 'tool:write {"path":"later.txt","content":"again"}', "session": j["session"], "minutes": 0})
        assert r["chat"] == j["session"] and r["ws"] == ws and r["tz"] == 180, r
        assert [x["id"] for x in c.req("GET", "/api/jobs?session=" + j["session"])] == [r["id"]]
        r = wait_job(c, r["id"])
        assert r["status"] == "done" and r["session"] == j["session"], r
        assert c.req("POST", f"/api/ws/{ws}/get", {"paths": ["later.txt"]})["files"], "the reminder ran in the chat's workspace"
        f = wait_job(c, c.req("POST", "/api/jobs", {"prompt": "hi", "session": j["session"], "fresh": True})["id"])
        assert f["status"] == "done" and f["session"] and f["session"] != j["session"] and f["ws"] == ws and f["chat"] == j["session"], f
        for tok in [features.runner_token(1), features.runner_token(1)]:  # a second sandbox must not log out the first
            r = me.urllib.request.Request(T.SERVER + "/api/jobs", headers={"Authorization": "Bearer " + tok})
            assert me.urllib.request.urlopen(r).status == 200
        print("ok   later: a scheduled message continues the same chat in its workspace, or a fresh chat in the same project")

        # account debug log (ME_TRACE=1: the first account): requests, agent tool calls, web errors; keys are masked
        c.req("POST", "/api/clientlog", {"msg": "boom tvly-abcdef123456", "where": "app.js:1"})
        recs = [json.loads(x) for x in open(T.Path(T.env["ME_DATA"]) / "logs" / "1.jsonl", encoding="utf-8")]
        kinds = {r["k"] for r in recs}
        assert {"http", "worker_start", "tool", "turn_end", "web_error"} <= kinds, kinds
        assert "abcdef123456" not in json.dumps(recs) and not any(r.get("path") == "/api/clientlog" for r in recs)
        print("ok   account debug log records requests, agent steps and web errors")

        # GitHub: no OAuth app → token only; a repo session gets (and later reuses) a workspace marked with the repo
        g = c.req("GET", "/api/github")
        assert g == {"connected": False, "oauth": False}, g
        try:
            c.req("POST", "/api/github/oauth", {})
            raise AssertionError("oauth without an app")
        except me.CloudError:
            pass
        import cloud
        saved = features._gh, features._clone, features._gh_token
        features._gh = lambda tok, path, *a, **k: {"default_branch": "dev", "full_name": "octo/" + a[0]["name"] if a else ""}
        features._clone = lambda uid, wid, url, branch: (cloud.ws_root(uid, wid) / ".git").mkdir(parents=True)
        features._gh_token = lambda uid: "t"
        try:
            r1 = features.repo_ws(1, "octo/app")
            r2 = features.repo_ws(1, "octo/app")
            r3 = features.github_new_repo(features.GhNew(name="my new app"), {"id": 1})
        finally:
            features._gh, features._clone, features._gh_token = saved
        assert r1 == r2 and r1["base"] == "dev" and r1["name"] == "octo/app", (r1, r2)
        assert next(w for w in c.req("GET", "/api/ws") if w["id"] == r1["ws"])["repo"] == "octo/app"
        assert r3["repo"] == "octo/my-new-app" and r3["ws"] != r1["ws"], r3
        print("ok   GitHub repo session: one workspace per repo, base branch recorded; a new repo gets its own")

        # scheduled job: next fire time computed, can be toggled and run now
        s = c.req("POST", "/api/jobs", {"prompt": "hi", "cron": "0 9 * * 1-5", "tz": 180, "name": "daily"})
        assert s["next"] > time.time() and s["enabled"]
        s2 = c.req("POST", f"/api/jobs/{s['id']}/toggle")
        assert not s2["enabled"]
        try:
            c.req("POST", "/api/jobs", {"prompt": "x", "cron": "99 * * * *"})
            raise AssertionError("bad cron accepted")
        except me.CloudError:
            pass
        c.req("DELETE", f"/api/jobs/{s['id']}")
        print("ok   scheduled jobs: cron validated, toggle, delete")

        # preview: the file the job wrote is reachable by a signed link, a forged one is not
        w = c.req("POST", "/api/cloud", {"ws": ws})
        assert "/preview/" in w["preview"], w
        path = "/preview/" + w["preview"].split("/preview/")[1]
        try:
            html = me.urllib.request.urlopen(T.SERVER + path + "/site/").read().decode()
        except me.urllib.error.HTTPError as e:
            raise AssertionError(f"{path}: {e.read()[:300]}")
        assert "built by a job" in html, html
        bad = path[:-4] + "0000"
        try:
            me.urllib.request.urlopen(T.SERVER + bad + "/site/")
            raise AssertionError("forged preview token accepted")
        except me.urllib.error.HTTPError as e:
            assert e.code == 404
        print("ok   preview URL serves the workspace (signed, forged rejected)")

        # undo + goal through the cloud sandbox
        def until(kind, after):
            end = time.time() + 30
            seen = []
            while time.time() < end:
                for e in c.req("GET", f"/api/cloud/{w['id']}/events?after={after}", timeout=40)["events"]:
                    after = e["seq"]
                    seen.append(e)
                    if e["type"] == kind:
                        return after, seen
            raise AssertionError(f"no {kind}: {[e['type'] for e in seen]}")
        cur, _ = until("ready", 0)
        c.req("POST", f"/api/cloud/{w['id']}/prompt", {"text": 'tool:write {"path":"undo_me.txt","content":"x"}'})
        cur, seen = until("turn_end", cur)
        assert any(e["type"] == "turn_diff" for e in seen)
        c.req("POST", f"/api/cloud/{w['id']}/undo")
        cur, seen = until("undone", cur)
        assert list((T.Path(T.env["ME_DATA"]) / "ws").glob(f"*/{ws}/site/index.html")), "workspace not where expected"
        assert not list((T.Path(T.env["ME_DATA"]) / "ws").glob(f"*/{ws}/undo_me.txt")), "undo did not remove the file"
        c.req("POST", f"/api/cloud/{w['id']}/goal", {"text": "ship it"})
        cur, seen = until("goal", cur)
        cur, seen = until("goal", cur)
        assert seen[-1]["status"] == "done", seen[-1]
        print("ok   undo and /goal work in the cloud sandbox")
        c.req("POST", f"/api/cloud/{w['id']}/cmd", {"type": "rename", "text": "renamed in the cloud"})
        cur, seen = until("reload", cur)
        sid = seen[-1]["session"]
        assert json.loads(c.req("GET", "/api/doc?key=session/" + sid)["content"])["title"] == "renamed in the cloud"
        c.req("POST", f"/api/cloud/{w['id']}/cmd", {"type": "btw", "text": "side"})
        cur, seen = until("btw", cur)
        print("ok   web chat commands (/rename, /btw) reach the cloud sandbox and the chat is synced before reload")

        # the agent's `later` tool from inside the sandbox: syncs the chat, then the server holds a reminder for it
        c.req("POST", f"/api/cloud/{w['id']}/prompt", {"text": 'tool:later {"text":"check the logs","cron":"0 9 * * *","tz":3}'})
        cur, seen = until("turn_end", cur)
        te = next(e for e in seen if e["type"] == "tool_end")
        assert te["ok"] and "set: repeats 0 9 * * *" in te["output"] and "UTC+3" in te["output"], te
        js = [x for x in c.req("GET", "/api/jobs") if x["name"] == "check the logs"]
        assert len(js) == 1 and js[0]["chat"] and js[0]["cron"] == "0 9 * * *" and js[0]["tz"] == 180, js
        c.req("DELETE", f"/api/jobs/{js[0]['id']}")
        print("ok   later tool: a reminder for this chat is scheduled from inside the sandbox")

        # marketplace: publish v1 → install → publish v2 auto-updates; rating
        skill = '"""\nname: tmpskill\ndescription: v1\n---\nguide\n"""\n'
        c.req("POST", "/api/market", {"content": skill, "ext": "py"})
        mid = next(m["id"] for m in c.req("GET", "/api/market") if m["name"] == "tmpskill")
        c.req("POST", f"/api/market/{mid}/install")
        r = c.req("POST", "/api/market", {"content": skill.replace("v1", "v2"), "ext": "py", "note": "better"})
        assert r["version"] == 2, r
        d = c.req("GET", "/api/doc?key=skill/tmpskill.py")
        assert "v2" in d["content"], "installed skill was not auto-updated"
        inst = next(i for i in c.req("GET", "/api/installed") if i["mid"] == mid)
        assert inst["version"] == 2 and inst["auto"] and not inst["edited"], inst
        c.req("PUT", "/api/doc", {"key": "skill/tmpskill.py", "content": d["content"] + "# my tweak\n"})
        c.req("POST", "/api/market", {"content": skill.replace("v1", "v3"), "ext": "py"})
        assert "my tweak" in c.req("GET", "/api/doc?key=skill/tmpskill.py")["content"], "a locally edited install must not be overwritten"
        assert c.req("POST", f"/api/market/{mid}/rate", {"stars": 4}) == {"rating": 4.0, "votes": 1}
        m = next(m for m in c.req("GET", "/api/market?sort=rating") if m["id"] == mid)
        assert m["version"] == 3 and m["rating"] == 4.0
        assert [v["version"] for v in c.req("GET", f"/api/market/{mid}/versions")] == [3, 2, 1]
        print("ok   marketplace versions, auto-update (skips local edits), ratings")

        # notify without a linked Telegram chat → clear error for the agent
        try:
            c.req("POST", "/api/notify", {"text": "hello"})
            raise AssertionError("notify should fail without a chat")
        except me.CloudError as e:
            assert "Telegram" in str(e)
        assert c.req("GET", "/api/tg") == {"bot": "", "linked": False, "own": None, "own_linked": False}
        try:
            c.req("POST", "/api/tg/bot", {"token": "nope"})
            raise AssertionError("bad bot token accepted")
        except me.CloudError:
            pass
        print("ok   notify / personal Telegram bot validation")

        # global memory: one document per note, synced like any other doc
        c.req("PUT", "/api/doc", {"key": "memory/20260101-000000-abcd.json", "content": json.dumps({"text": "deploy on fridays", "ts": time.time()})})
        keys = [d["key"] for d in c.req("GET", "/api/docs")]
        assert "memory/20260101-000000-abcd.json" in keys
        c.req("DELETE", "/api/doc?key=memory/20260101-000000-abcd.json")
        print("ok   memory notes sync as memory/<id>.json")

        # static sites: the agent's publish tool uploads a folder, anyone opens it by link, sandboxed
        import tempfile, urllib.error, urllib.request
        bh = me.behavior(c.s.config["settings"])
        assert not bh["publish"], "publishing is off by default"
        site = Path(tempfile.mkdtemp())
        (site / "index.html").write_text("<h1>my site</h1>")
        (site / "css").mkdir()
        (site / "css" / "a.css").write_text("h1{color:red}")
        (site / ".env").write_text("SECRET=1")
        ok, out, _ = me.ToolBox(c.s, None).run("publish", {"path": str(site), "name": "My Site!"}, lambda e: None, None)
        assert ok and "/s/" in out, out
        url = out.split(": ", 1)[1]
        assert url.endswith(f"/s/{c.s.auth['user']}/my-site/"), url
        with urllib.request.urlopen(url) as r:
            assert r.read() == b"<h1>my site</h1>" and "sandbox" in r.headers["Content-Security-Policy"]
        assert urllib.request.urlopen(url + "css/a.css").read() == b"h1{color:red}"
        for bad in (".env", "../../t.db", "nope.html"):
            try:
                urllib.request.urlopen(url + bad)
                raise AssertionError(bad + " served")
            except urllib.error.HTTPError as e:
                assert e.code == 404
        assert [x["name"] for x in c.req("GET", "/api/sites")["sites"]] == ["my-site"]
        # its own host (<site>.<user>.<domain>): own origin, no sandbox, nothing of the app reachable there
        label = c.s.auth["user"].lower().replace("_", "-").replace(".", "-")
        sites = T.Path(T.env["ME_DATA"]) / "sites"
        assert (sites / ".want" / f"my-site.{label}.sites.test").exists(), "certificate not requested"

        def at_host(path, method="GET"):
            rq = urllib.request.Request(T.SERVER + path, method=method, headers={"Host": f"my-site.{label}.sites.test"})
            try:
                with urllib.request.urlopen(rq) as r:
                    return r.status, r.read(), r.headers
            except urllib.error.HTTPError as e:
                return e.code, e.read(), e.headers
        code, body, hd = at_host("/")
        assert code == 200 and body == b"<h1>my site</h1>" and not hd.get("Content-Security-Policy"), (code, body)
        assert at_host("/css/a.css")[1] == b"h1{color:red}" and at_host("/api/sites")[0] == 404 and at_host("/", "POST")[0] == 405
        assert at_host("/../../t.db")[0] == 404 and at_host("/.env")[0] == 404
        (sites / ".certs").mkdir(exist_ok=True)
        (sites / ".certs" / f"my-site.{label}.sites.test").touch()  # the certificate helper is done: the host link is handed out
        assert c.req("GET", "/api/sites")["sites"][0]["url"] == f"https://my-site.{label}.sites.test/"
        (site / "index.html").write_text("<h1>v2</h1>")
        (site / "css" / "a.css").unlink()
        me.ToolBox(c.s, None).run("publish", {"path": str(site), "name": "my-site"}, lambda e: None, None)
        assert urllib.request.urlopen(url).read() == b"<h1>v2</h1>"
        try:
            urllib.request.urlopen(url + "css/a.css")
            raise AssertionError("old file kept after republish")
        except urllib.error.HTTPError:
            pass
        ok, out, _ = me.ToolBox(c.s, None).run("publish", {"path": str(site / "css"), "name": "x"}, lambda e: None, None)
        assert not ok and "index.html" in out
        c.req("DELETE", "/api/sites/my-site")
        assert c.req("GET", "/api/sites")["sites"] == []
        print("ok   publish: a folder goes online at /s/<user>/<site>/ and its own host, sandboxed, replaced in place, deletable")
        c.req("DELETE", f"/api/cloud/{w['id']}")
    finally:
        os.killpg(srv.pid, 9)
    print("all feature tests passed")


if __name__ == "__main__":
    main()
