"""Cloud sandboxes survive a server restart (deploys must not kill running agents).
Run: python3 tests/test_restart.py   (starts its own server + mock LLM; needs fastapi/uvicorn)"""
import os, subprocess, sys, tempfile, threading, time, uuid
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
import mock_llm  # noqa: E402

llm = ThreadingHTTPServer(("127.0.0.1", 0), mock_llm.H)
threading.Thread(target=llm.serve_forever, daemon=True).start()
PORT = 8797
SERVER = f"http://127.0.0.1:{PORT}"
os.environ["ME_SERVER"] = SERVER
import me  # noqa: E402

tmp = tempfile.mkdtemp()
env = dict(os.environ, ME_DB=f"{tmp}/t.db", ME_DATA=f"{tmp}/data", ME_SANDBOX="0", ME_INTERNAL_URL=SERVER, ME_SITES_DOMAIN="sites.test")


def start():
    p = subprocess.Popen([sys.executable, "-m", "uvicorn", "app:app", "--port", str(PORT)], cwd=ROOT / "server", env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    for _ in range(100):
        try:
            me.urllib.request.urlopen(SERVER + "/healthz", timeout=1)
            return p
        except Exception:
            time.sleep(0.1)
    raise SystemExit("server did not start")


def login():
    s = me.Store(tempfile.mkdtemp())
    s.auth = {"server": SERVER}
    c = me.Cloud(s)
    c.login("r" + uuid.uuid4().hex[:6], "secret1", register=True)
    s.config["providers"]["mock"] = {"base_url": f"http://127.0.0.1:{llm.server_port}/v1"}
    s.config["models"] = [{"name": "m", "id": "mock-1", "provider": "mock"}]
    s.config["model"] = "m"
    s.save()
    c.sync()
    return c


def until(c, wid, kind, after=0):
    end = time.time() + 30
    while time.time() < end:
        r = c.req("GET", f"/api/cloud/{wid}/events?after={after}", timeout=40)
        for e in r["events"]:
            after = e["seq"]
            if e["type"] == kind:
                return after
    raise AssertionError("no " + kind)


if __name__ == "__main__":
    srv = start()
    try:
        c = login()
        w = c.req("POST", "/api/cloud", {})
        cur = until(c, w["id"], "ready")
        os.killpg(srv.pid, 9)  # like systemctl restart with KillMode=process: only the server dies
        srv.wait()
        srv = start()
        assert any(x["id"] == w["id"] and x["alive"] for x in c.req("GET", "/api/cloud")), "sandbox lost on restart"
        c.req("POST", f"/api/cloud/{w['id']}/prompt", {"text": "hi"})
        until(c, w["id"], "turn_end", cur)
        print("ok   sandbox survived a server restart and keeps working")
        c.req("DELETE", f"/api/cloud/{w['id']}")
        assert not c.req("GET", "/api/cloud"), "stop did not kill the sandbox"
        print("ok   stop kills the sandbox")
    finally:
        os.killpg(srv.pid, 9)
