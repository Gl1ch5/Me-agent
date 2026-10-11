"""Run: python3 tests/test_core.py   (no deps; spins up the mock LLM)"""
import json, os, subprocess, sys, tempfile, threading, time
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
os.environ["ME_HOME"] = tempfile.mkdtemp()
import me  # noqa: E402
import mock_llm  # noqa: E402

srv = ThreadingHTTPServer(("127.0.0.1", 0), mock_llm.H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{srv.server_port}/v1"


def make(api="chat"):
    s = me.Store(os.environ["ME_HOME"])
    s.config["providers"]["mock"] = {"base_url": BASE, "api_key": "x"}
    s.config["models"] = [{"name": "m", "id": "mock-1", "provider": "mock", "api": api, "context": 32000}]
    s.config["model"] = "m"
    s.config["settings"]["yolo"] = True
    return s


def run(store, text):
    ev = []
    ag = me.Agent(store, ev.append)
    ag.run(text)
    return ag, ev


def test_text():
    ag, ev = run(make(), "hi")
    assert "".join(e["text"] for e in ev if e["type"] == "text").startswith("Hello **world**")
    end = [e for e in ev if e["type"] == "turn_end"][0]
    assert end["in"] > 0 and end["out"] == 20


def test_tool_read(tmp=Path(tempfile.mkdtemp())):
    f = tmp / "a.txt"
    f.write_text("alpha\nbeta\n")
    ag, ev = run(make(), "tool:read " + json.dumps({"path": str(f)}))
    kinds = [e["type"] for e in ev]
    assert "reasoning" in kinds and "tool_start" in kinds and "tool_end" in kinds
    out = [e for e in ev if e["type"] == "tool_end"][0]["output"]
    assert "1│alpha" in out, out
    assert ag.session["messages"][-1]["content"].startswith("done:")


def test_edit_write_bash(tmp=Path(tempfile.mkdtemp())):
    f = tmp / "b.py"
    s = make()
    run(s, "tool:write " + json.dumps({"path": str(f), "content": "x = 1\n"}))
    run(s, "tool:edit " + json.dumps({"path": str(f), "old": "x = 1", "new": "x = 2"}))
    assert f.read_text() == "x = 2\n"
    ag, ev = run(s, "tool:bash " + json.dumps({"cmd": "echo hi-there"}))
    assert "hi-there" in [e for e in ev if e["type"] == "tool_end"][0]["output"]


def test_confirm_denied(tmp=Path(tempfile.mkdtemp())):
    s = make()
    s.config["settings"]["yolo"] = False
    ev = []
    ag = me.Agent(s, ev.append, confirm=lambda n, a: False)
    ag.run("tool:write " + json.dumps({"path": str(tmp / "no.txt"), "content": "x"}))
    assert not (tmp / "no.txt").exists()


def test_skill():
    s = make()
    (s.home / "skills" / "demo.py").write_text(me.SKILL_TEMPLATE.replace("NAME", "demo"))
    sk = me.parse_skill(s.home / "skills" / "demo.py")
    assert sk["name"] == "demo" and "hello" in sk["actions"] and sk["actions"]["danger"]["confirm"]
    assert "demo:" in me.Agent(s, print).system_prompt()
    ag, ev = run(s, "tool:skill " + json.dumps({"name": "demo"}))
    assert "hello(who='world')" in [e for e in ev if e["type"] == "tool_end"][0]["output"]
    ag, ev = run(s, "tool:skill " + json.dumps({"name": "demo", "action": "hello", "args": {"who": "me"}}))
    tev = [e for e in ev if e["type"] == "tool_event"]
    assert any(e.get("t") == "progress" for e in tev) and any(e.get("t") == "log" for e in tev)
    assert "hello, me!" in [e for e in ev if e["type"] == "tool_end"][0]["output"]


def test_responses_api():
    ag, ev = run(make("responses"), "hi")
    assert "Hi from responses" in "".join(e["text"] for e in ev if e["type"] == "text")
    ag, ev = run(make("responses"), "tool:ls " + json.dumps({"pattern": "*.py"}))
    assert ag.session["messages"][-1]["content"].startswith("done:")


def test_read_formats(tmp=Path(tempfile.mkdtemp())):
    import zipfile
    d = tmp / "x.docx"
    with zipfile.ZipFile(d, "w") as z:
        z.writestr("word/document.xml", "<w:document><w:p><w:r><w:t>Hello docx</w:t></w:r></w:p></w:document>")
    assert "Hello docx" in me.read_any(d)[0]
    png = tmp / "i.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\0" * 20)
    assert me.read_any(png)[1].startswith("data:image/png")
    b = tmp / "bin"
    b.write_bytes(b"\0\1\2abc")
    assert "[binary" in me.read_any(b)[0]


def test_list_models():
    ms = me.list_models({"base_url": BASE})
    assert ms[0] == {"id": "mock-1", "context": 32000}


def test_md_render():
    lines = me.render_md("# Title\n- item **bold**\n```py\ndef f(): return 1\n```\n" + "word " * 50, 40)
    assert all(l.width() <= 40 for l in lines), [l.width() for l in lines]


if __name__ == "__main__":
    fails = 0
    for k, v in list(globals().items()):
        if k.startswith("test_"):
            try:
                v()
                print("ok  ", k)
            except Exception as e:
                fails += 1
                import traceback
                traceback.print_exc()
                print("FAIL", k, e)
    sys.exit(1 if fails else 0)
