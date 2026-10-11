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


def ends(ev):
    return [e for e in ev if e["type"] == "tool_end"]


def test_todo_and_goal():
    s = make()
    ag, ev = run(s, "tool:todo " + json.dumps({"items": ["x read code", "> write fix", "run tests"]}))
    assert [i["s"] for i in ag.session["todo"]] == ["done", "doing", "todo"], ag.session["todo"]
    assert any(e["type"] == "todo" for e in ev) and "1/3 done" in ends(ev)[0]["output"]
    ev = []
    ag = me.Agent(s, ev.append)
    g = ag.goal("make it work")
    assert g["status"] == "done" and "tests green" in g["reason"], g
    assert [e["status"] for e in ev if e["type"] == "goal"] == ["working", "done"]


def test_edit_variants(tmp=Path(tempfile.mkdtemp())):
    f = tmp / "e.py"
    f.write_text("a = 1\nb = 2\nc = 3\n")
    s = make()
    run(s, "tool:edit " + json.dumps({"path": str(f), "edits": [{"old": "a = 1", "new": "a = 10"}, {"old": "c = 3", "new": "c = 30"}]}))
    assert f.read_text() == "a = 10\nb = 2\nc = 30\n"
    run(s, "tool:edit " + json.dumps({"path": str(f), "lines": "2-2", "new": "b = 20\nbb = 21"}))
    assert f.read_text() == "a = 10\nb = 20\nbb = 21\nc = 30\n", f.read_text()
    ag, ev = run(s, "tool:edit " + json.dumps({"path": str(f), "edits": [{"old": "a = 10", "new": "x"}, {"old": "nope = 1", "new": "y"}]}))
    assert "edit #2 of 2" in ends(ev)[0]["output"] and "a = 10" in f.read_text()  # all-or-nothing
    ag, ev = run(s, "tool:edit " + json.dumps({"path": str(f), "old": "bb = 22", "new": "z"}))
    assert "Closest is line 3" in ends(ev)[0]["output"], ends(ev)[0]["output"]
    f.write_text("x = 1   \ny = 2\n")
    run(s, "tool:edit " + json.dumps({"path": str(f), "old": "x = 1\ny = 2", "new": "x = 5\ny = 2"}))
    assert f.read_text().startswith("x = 5"), f.read_text()


def test_repo_map(tmp=Path(tempfile.mkdtemp())):
    (tmp / "pkg").mkdir()
    (tmp / "pkg" / "a.py").write_text("import os\nclass Foo:\n    def inner(self): pass\ndef bar():\n    pass\n")
    (tmp / "web.js").write_text("export function render() {}\nconst X = 1\n")
    out = me.repo_map(str(tmp), home=tempfile.mkdtemp())
    assert "pkg/a.py: Foo, bar" in out and "inner" not in out and "web.js: render, X" in out, out


def test_parallel_readonly(tmp=Path(tempfile.mkdtemp())):
    (tmp / "p1.txt").write_text("one")
    (tmp / "p2.txt").write_text("two")
    ag, ev = run(make(), "tools:" + json.dumps([["read", {"path": str(tmp / "p1.txt")}], ["read", {"path": str(tmp / "p2.txt")}]]))
    kinds = [e["type"] for e in ev if e["type"] in ("tool_start", "tool_end")]
    assert kinds == ["tool_start", "tool_start", "tool_end", "tool_end"], kinds  # both started before either finished
    assert "one" in ends(ev)[0]["output"] and "two" in ends(ev)[1]["output"]


def test_undo_and_diff(tmp=Path(tempfile.mkdtemp())):
    f = tmp / "u.txt"
    f.write_text("before\n")
    s = make()
    ev = []
    ag = me.Agent(s, ev.append)
    ag.run("tool:edit " + json.dumps({"path": str(f), "old": "before", "new": "after"}))
    ag.run("tool:write " + json.dumps({"path": str(tmp / "new.txt"), "content": "fresh"}))
    td = [e for e in ev if e["type"] == "turn_diff"]
    assert len(td) == 2 and "+after" in td[0]["files"][0]["diff"], td
    n = len(ag.session["messages"])
    assert ag.undo()["ok"] and not (tmp / "new.txt").exists() and len(ag.session["messages"]) < n
    assert ag.undo()["ok"] and f.read_text() == "before\n"
    assert not ag.undo()["ok"]


def test_self_verify(tmp=Path(tempfile.mkdtemp())):
    ag, ev = run(make(), "tool:write " + json.dumps({"path": str(tmp / "v.py"), "content": "x=1"}))
    assert any(e["type"] == "verify" for e in ev)
    assert any(m["role"] == "user" and str(m["content"]).startswith("[self-check]") for m in ag.session["messages"])


def test_subagent():
    s = make()
    before = len(s.sessions())
    ag, ev = run(s, "tool:task " + json.dumps({"agent": "review", "prompt": "hi"}))
    out = ends(ev)[0]
    assert out["ok"] and "Hello" in out["output"], out
    assert any(e["type"] == "tool_event" and "review agent" in e.get("text", "") for e in ev)
    assert len(s.sessions()) == before + 1  # the subagent's own session is not saved
    sub = me.Agent(s, print)
    sub.depth = 1
    assert "task" not in [t["name"] for t in sub.tool_schemas()]  # no recursive subagents


def test_permissions(tmp=Path(tempfile.mkdtemp())):
    s = make()
    s.config["settings"]["yolo"] = False
    tb = me.ToolBox(s, me.Skills(s))
    s.config["settings"]["perm"] = "ask"
    assert tb.needs_confirm("write", {"path": str(tmp / "x")}, cwd=str(tmp)) is False  # inside the project
    assert tb.needs_confirm("write", {"path": "/etc/x"}, cwd=str(tmp)) is True
    assert tb.needs_confirm("bash", {"cmd": "git status"}) is False
    assert tb.needs_confirm("bash", {"cmd": "git status; rm -rf /"}) is True
    s.config["settings"]["perm"] = "read"
    assert isinstance(tb.needs_confirm("edit", {"path": str(tmp / "x")}, cwd=str(tmp)), str)
    s.config["settings"]["perm"] = "full"
    assert tb.needs_confirm("bash", {"cmd": "rm -rf build"}) is False
    s.config["settings"]["perm"] = "ask"
    ag = me.Agent(s, lambda e: None, confirm=lambda n, a: "always")
    ag.run("tool:bash " + json.dumps({"cmd": "echo always-ok"}))
    assert "echo always-ok*" in s.config["settings"]["allow"]


def test_cost_and_cache():
    s = make()
    s.config["models"][0]["price"] = [1.0, 2.0, 0.1]
    ag, ev = run(s, "hi")
    end = [e for e in ev if e["type"] == "turn_end"][0]
    assert end["cost"] > 0 and ag.session["usage"]["cost"] > 0 and me.day_cost(s.home) > 0
    assert ag.session["usage"]["cached"] == 800
    s.config["models"][0]["cache"] = "anthropic"
    run(s, "hi")
    assert isinstance(mock_llm.LAST["messages"][0]["content"], list) and mock_llm.LAST["messages"][0]["content"][0]["cache_control"]
    s.config["settings"]["max_turn_cost"] = 1e-9
    ag, ev = run(s, "tool:ls " + json.dumps({"pattern": "*"}))
    assert any(e["type"] == "error" and "limit" in e["text"] for e in ev)


MCP_SERVER = r"""
import json, sys
for line in sys.stdin:
    m = json.loads(line)
    if "id" not in m:
        continue
    if m["method"] == "initialize":
        r = {"protocolVersion": "2025-03-26", "capabilities": {"tools": {}}, "serverInfo": {"name": "t", "version": "1"}}
    elif m["method"] == "tools/list":
        r = {"tools": [{"name": "add", "description": "Add numbers", "inputSchema": {"type": "object", "properties": {"a": {"type": "number"}, "b": {"type": "number"}}, "required": ["a", "b"]}}]}
    else:
        r = {"content": [{"type": "text", "text": str(m["params"]["arguments"]["a"] + m["params"]["arguments"]["b"])}]}
    print(json.dumps({"jsonrpc": "2.0", "id": m["id"], "result": r}), flush=True)
"""


def test_mcp_lazy(tmp=Path(tempfile.mkdtemp())):
    srv = tmp / "mcp_srv.py"
    srv.write_text(MCP_SERVER)
    s = make()
    assert "mcp" not in [t["name"] for t in me.Agent(s, print).tool_schemas()]
    s.config["mcp"] = {"calc": {"command": sys.executable, "args": [str(srv)], "description": "math"}}
    a = me.Agent(s, print)
    assert "mcp" in [t["name"] for t in a.tool_schemas()] and "calc: math" in a.system_prompt()
    ag, ev = run(s, "tool:mcp " + json.dumps({"server": "calc"}))
    assert "add(a: number, b: number)" in ends(ev)[0]["output"], ends(ev)[0]["output"]
    ag, ev = run(s, "tool:mcp " + json.dumps({"server": "calc", "tool": "add", "args": {"a": 2, "b": 3}}))
    assert ends(ev)[0]["output"] == "5"


def test_self_extend_agent(tmp=Path(tempfile.mkdtemp())):
    s = make()
    s.config["agent"] = "me"
    ag = me.Agent(s, print)
    sp = ag.system_prompt()
    assert str(s.home) + "/skills/" in sp and "{home}" not in sp
    theme = {"name": "sunset", "label": "Sunset", "dark": True, "colors": dict(me.BUILTIN_THEMES["me-dark"]["colors"], accent="#ff7a59")}
    ev = []
    ag = me.Agent(s, ev.append)
    ag.run("tool:write " + json.dumps({"path": str(s.home / "themes" / "sunset.json"), "content": json.dumps(theme)}))
    assert not [e for e in ev if e["type"] == "tool_end" and not e["ok"]], ev
    assert "sunset" in me.themes(s) and me.theme(s, "sunset")["colors"]["accent"] == "#ff7a59"
    ag.run("tool:write " + json.dumps({"path": str(s.home / "skills" / "hi.md"), "content": "---\nname: hi\ndescription: greets\n---\nSay hi."}))
    assert "hi" in me.Skills(s).enabled()
    assert "me" in me.presets(s)


def test_memory(tmp=Path(tempfile.mkdtemp())):
    s = make()
    ag, ev = run(s, "tool:memory " + json.dumps({"text": "deploy on fridays"}))
    assert ends(ev)[0]["ok"] and "remembered" in ends(ev)[0]["output"], ends(ev)
    mid = me.mem_list(s.home)[0]["id"]
    assert "deploy on fridays" in me.Agent(s, print).system_prompt()
    assert mid in me.mem_prompt(s.home)
    f = s.home / "memory" / (mid + ".json")
    assert f.exists() and '"text": "deploy on fridays"' in f.read_text()
    run(s, "tool:memory " + json.dumps({"forget": mid}))
    assert me.mem_list(s.home) == [] and "deploy on fridays" not in me.Agent(s, print).system_prompt()
    ag, ev = run(s, "tool:memory " + json.dumps({"text": "  "}))
    assert not ends(ev)[0]["ok"]


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
