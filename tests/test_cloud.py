"""Integration test against a running server + mock LLM.
  ME_DB=/tmp/t.db ME_DATA=/tmp/medata ME_SANDBOX=0 ME_INTERNAL_URL=http://127.0.0.1:8795 uvicorn app:app --port 8795   (in server/)
  python3 tests/mock_llm.py 8921
  python3 tests/test_cloud.py http://127.0.0.1:8795 http://127.0.0.1:8921/v1
"""
import json, os, sys, tempfile, threading, time, uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
SERVER, LLM = sys.argv[1], sys.argv[2]
os.environ["ME_SERVER"] = SERVER
import me  # noqa: E402


def device():
    s = me.Store(tempfile.mkdtemp())
    s.auth = {"server": SERVER}
    return s, me.Cloud(s)


user = "u" + uuid.uuid4().hex[:6]
a, ca = device()
ca.login(user, "secret1", register=True)
a.config["providers"]["mock"] = {"base_url": LLM, "api_key": "x"}
a.config["models"] = [{"name": "mock", "id": "mock-1", "provider": "mock", "api": "chat", "context": 32000}]
a.config["model"] = "mock"
a.save()
ca.sync()
docs = ca.req("GET", "/api/docs")
assert any(d["key"] == "plugin/voice.py" for d in docs), "default voice plugin not seeded"
print("ok   register + default plugin seeded")

# instant sync: device b long-polls, device a writes
b, cb = device()
cb.login(user, "secret1")
cb.sync()
got = []
cb.watch(lambda n: got.append(n))
time.sleep(1)
a.config["prompt"] = "be terse"
a.save()
ca.sync()
for _ in range(50):
    if got:
        break
    time.sleep(0.1)
assert got and me.jload(b.cfg_path)["prompt"] == "be terse", "watch did not deliver"
print("ok   instant sync via long-poll")

# workspace: a pushes a project, b pulls it
proj = Path(tempfile.mkdtemp()) / "proj"
proj.mkdir()
(proj / "main.py").write_text("print('hi')\n")
(proj / "node_modules").mkdir()
(proj / "node_modules" / "x.js").write_text("ignored")
(proj / "big.bin").write_bytes(b"\0" * 2_000_000)
wa = me.Workspaces(a, ca)
wid = wa.link(str(proj))
assert wa.sync(wid) == (1, 0)
wb = me.Workspaces(b, cb)
dst = Path(tempfile.mkdtemp()) / "copy"
wb.attach(wid, str(dst))
assert wb.sync(wid) == (0, 1) and (dst / "main.py").read_text() == "print('hi')\n"
(dst / "main.py").write_text("print('changed on b')\n")
time.sleep(0.05)
wb.sync(wid)
wa.sync(wid)
assert (proj / "main.py").read_text() == "print('changed on b')\n"
print("ok   workspace 3-way sync, heavy/ignored files skipped")

# cloud runner: start worker in that workspace, run a tool, see the file appear locally
w = ca.req("POST", "/api/cloud", {"ws": wid})
after = 0


def events(until, timeout=40):
    global after
    end = time.time() + timeout
    seen = []
    while time.time() < end:
        r = ca.req("GET", f"/api/cloud/{w['id']}/events?after={after}", timeout=40)
        for e in r["events"]:
            after = e["seq"]
            seen.append(e)
            if e["type"] == until:
                return seen
    raise AssertionError(f"no {until}: {[e['type'] for e in seen]}")


events("ready")
ca.req("POST", f"/api/cloud/{w['id']}/prompt", {"text": 'tool:write {"path":"made_in_cloud.txt","content":"hello from the sandbox"}'})
ev = events("turn_end")
assert any(e["type"] == "tool_end" and e["ok"] for e in ev), ev
wa.sync(wid)
assert (proj / "made_in_cloud.txt").read_text() == "hello from the sandbox"
time.sleep(1.5)
ca.sync()
ses = [s for s in ca.req("GET", "/api/sessions") if s["id"]]
assert ses and ses[0]["messages"] >= 3, ses
print("ok   cloud sandbox ran the agent; file + session synced back")
ca.req("DELETE", f"/api/cloud/{w['id']}")
print("all cloud tests passed")
