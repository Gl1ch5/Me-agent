"""Run: python3 tests/test_core.py   (no deps; spins up the mock LLM)"""
import contextlib, io, json, os, re, subprocess, sys, tempfile, threading, time
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
os.environ["ME_HOME"] = tempfile.mkdtemp()
import me  # noqa: E402
me.RETRY_BASE = 0.01  # retries back off in milliseconds here
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
    (s.home / "skills" / "demo.py").write_text(me.SKILL_TEMPLATE.replace("NAME", "demo"), "utf-8")
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
    (s.home / "agents").mkdir(exist_ok=True)
    (s.home / "agents" / "review.md").write_text("---\nname: review\ntools: read, ls, grep\nwrites:\n---\nReview.")
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
    (s.home / "agents").mkdir(exist_ok=True)
    (s.home / "agents" / "me.md").write_text((Path(__file__).resolve().parent.parent / "server" / "market" / "agent-me.md").read_text("utf-8"), "utf-8")
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
    assert "code" in me.presets(s)


def test_modder_pack(tmp=Path(tempfile.mkdtemp())):
    # the modder skills from the marketplace, run through the real skill runner on small fixtures (no network, no fal key)
    import shutil, threading
    from PIL import Image
    s = make()
    market = Path(__file__).resolve().parent.parent / "server" / "market"
    names = ["game-recon", "mod-lint", "saves-backup", "fal-assets", "sprite-tools", "knowledge", "mod-engines", "mod-publish"]
    for n in names:
        src = next(market.glob(n + ".*"))
        shutil.copy(src, s.home / "skills" / src.name)
    sk = me.Skills(s).all()
    assert all(n in sk for n in names), sorted(sk)
    assert sk["fal-assets"]["env"] == "FAL_KEY" and sk["sprite-tools"]["requires"] == "pillow" and sk["mod-engines"]["body"]

    def call(name, action, args):
        return me.Skills(s).run(sk[name], action, args, lambda e: None, threading.Event())

    mod = tmp / "mod"
    game = tmp / "game"
    mod.mkdir()
    game.mkdir()
    (mod / "README.md").write_text("hi")
    (mod / "Mod.cs").write_text('// FUN_0040a1f0\nvar key = "FAL_KEY=abcdefghijklmnopqrstuvwx";\n')
    (mod / ".env").write_text("X=1")
    (mod / "data.pak").write_bytes(b"GAMEDATA" * 100)
    (game / "data.pak").write_bytes(b"GAMEDATA" * 100)
    ok, out = call("mod-lint", "check", {"folder": str(mod), "game": str(game)})
    res = json.loads(out)
    assert ok and res["fail"] == 3 and not res["clean"], out
    assert "abcdefghijklmnopqrstuvwx" not in out  # keys are masked in the report

    eng = tmp / "unity-game"
    (eng / "Game_Data" / "Managed").mkdir(parents=True)
    (eng / "UnityPlayer.dll").write_bytes(b"x")
    (eng / "Game_Data" / "Managed" / "Assembly-CSharp.dll").write_bytes(b"x")
    (eng / "EasyAntiCheat").mkdir()
    ok, out = call("game-recon", "scan", {"path": str(eng)})
    res = json.loads(out)
    assert ok and res["engine"] == "unity-mono" and res["anti_cheat"] == ["EasyAntiCheat"] and res["playbook"] == "mod-engines#unity", out
    assert "do not inject" in res["warning"]

    saves = tmp / "saves"
    saves.mkdir()
    (saves / "slot1.sav").write_text("save")
    ok, snap = call("saves-backup", "backup", {"path": str(saves), "name": "t"})
    assert ok and Path(snap).joinpath("slot1.sav").read_text() == "save", snap
    (saves / "slot1.sav").write_text("broken")
    ok, msg = call("saves-backup", "restore", {"snapshot": snap, "dest": str(saves)})
    assert ok and (saves / "slot1.sav").read_text() == "save", msg

    raw = tmp / "raw.png"
    art = Image.new("RGB", (40, 40), (255, 255, 255))
    art.paste((200, 30, 30), (12, 12, 28, 28))
    art.save(raw)
    ok, cut = call("sprite-tools", "cutout", {"src": str(raw), "dst": str(tmp / "cut.png")})
    im = Image.open(cut).convert("RGBA")
    assert ok and im.getpixel((0, 0))[3] == 0 and im.getpixel((20, 20))[3] == 255, cut
    ok, fit = call("sprite-tools", "fit", {"src": cut, "dst": str(tmp / "fit.png"), "width": 16, "height": 16, "anchor": "bottom"})
    assert ok and Image.open(fit).size == (16, 16), fit

    ok, msg = call("fal-assets", "price", {"endpoint": "fal-ai/x"})
    assert not ok and "FAL_KEY is not set" in msg, msg

    # knowledge: parse the index offline (same table shape as universal-modder's knowledge/INDEX.md)
    index = ("# Knowledge base index\n\n| Game | Note | Engine | Route | Status | Agents | Date |\n|---|---|---|---|---|---|---|\n"
             "| Terraria | [Fal Arsenal: homing missiles](games/terraria/fal-arsenal.md) | dotnet | tmodloader | working | agent | 2026-09-01 |\n"
             "| Stardew Valley | [Crop mod](games/stardew/crop.md) | dotnet | smapi | working | agent | 2026-09-02 |\n")

    class FakeHttp:
        def get(self, url, headers=None):
            return index

    class FakeCtx:
        http = FakeHttp()
        env = {}
        table = staticmethod(lambda rows, title="": None)

    g = {"action": lambda f=None, **kw: f}
    exec(compile(open(sk["knowledge"]["path"], encoding="utf-8").read(), "knowledge.py", "exec"), g)
    hits = g["search"](FakeCtx(), "terraria")
    assert [h["path"] for h in hits] == ["games/terraria/fal-arsenal.md"] and hits[0]["route"] == "tmodloader", hits


def test_tui_message_queue(tmp=Path(tempfile.mkdtemp())):
    # a message sent while the agent works is queued, then sent when it is free, in order, one turn at a time
    from types import SimpleNamespace
    t = me.TUI.__new__(me.TUI)
    t.lock = threading.RLock()
    t.s = SimpleNamespace(home=tmp)
    t.agent = SimpleNamespace(busy=True)
    t.input, t.cur, t.sugg, t.hpos, t.scroll, t.hist, t.pending, t.launching, t.dirty = "", 0, [], None, 0, [], [], False, False
    sent, infos = [], []
    t.info = lambda text, color="dim": infos.append(text)
    t.bg = lambda fn, *a: sent.append(a)
    t.dispatch = lambda text: sent.append(text)
    t.toast = lambda *a, **k: None
    for msg in ("first", "second"):
        t.input = msg
        t.submit()
    assert sent == [] and t.pending == ["first", "second"] and len(infos) == 2, (sent, t.pending)
    t.agent.busy = False
    t.drain_pending()
    assert t.pending == ["second"] and sent == ["first"]
    t.launching = True  # a turn just started, the agent may not show busy yet: wait
    t.drain_pending()
    assert t.pending == ["second"] and sent == ["first"]
    t.launching = False
    t.drain_pending()
    assert t.pending == [] and sent == ["first", "second"]


def test_queued_message_mid_turn(tmp=Path(tempfile.mkdtemp())):
    # a message queued while a tool runs reaches the model right after that tool step, not after the whole turn
    (tmp / "a.txt").write_text("x\n")
    ev, box = [], ["also check b"]
    ag = me.Agent(make(), ev.append)
    ag.take_queued = lambda: [box.pop()] if box else []
    ag.run("tool:read " + json.dumps({"path": str(tmp / "a.txt")}))
    roles = [m["role"] for m in ag.session["messages"]]
    i = roles.index("tool")
    assert roles[i + 1] == "user" and "also check b" in ag.session["messages"][i + 1]["content"], roles
    assert [e["texts"] for e in ev if e["type"] == "queued_sent"] == [["also check b"]]
    assert not box


def test_agent_frontmatter_keys(tmp=Path(tempfile.mkdtemp())):
    # an agent file synced from another machine may carry name/prompt in its frontmatter: must not crash the TUI render
    s = make()
    (s.home / "agents" / "scout.md").write_text("---\nname: scout\nprompt: from frontmatter\ndescription: finds things\n---\nBody text.")
    ps = me.presets(s)
    assert ps["scout"]["name"] == "scout" and ps["scout"]["prompt"] == "Body text." and ps["scout"]["custom"] is True
    assert ps["scout"]["description"] == "finds things"


def test_only_code_builtin_and_cli_tools(tmp=Path(tempfile.mkdtemp())):
    s = make()
    assert list(me.BUILTIN_PRESETS) == ["code"]
    (s.home / "agents").mkdir(exist_ok=True)
    (s.home / "agents" / "ops.md").write_text(
        "---\nname: ops\ncolor: red\ntools: read, bash\n"
        "cli.hello: Say hello | echo hello {who} | cat | who=world\ncli.plain: No params | echo plain-out | \n---\nYou are ops.")
    ps = me.presets(s)
    assert set(ps["ops"]["cli"]) == {"ops_hello", "ops_plain"}, ps["ops"]["cli"]
    assert ps["ops"]["cli"]["ops_hello"]["cmd"] == "echo hello {who} | cat" and ps["ops"]["cli"]["ops_hello"]["params"] == {"who": "world"}
    s.config["agent"] = "ops"
    s.config["settings"]["perm"] = "full"
    ag = me.Agent(s, print)
    names = [t["name"] for t in ag.tool_schemas()]
    assert "ops_hello" in names and "ops_plain" in names and "write" not in names
    assert "ops_hello" in ag.system_prompt()
    ag, ev = run(s, "tool:ops_hello " + json.dumps({"who": "a b; echo hacked"}))
    start = [e for e in ev if e["type"] == "tool_start"][0]
    assert start["cli"]["agent"] == "ops" and start["cli"]["color"] == "red", start
    assert ends(ev)[0]["ok"] and ends(ev)[0]["output"].strip().replace('"', "") == "hello a b; echo hacked", ends(ev)  # one argument, nothing injected (cmd echo keeps quotes)
    assert any(e["type"] == "tool_event" and e.get("t") == "status" for e in ev)
    ag, ev = run(s, "tool:ops_plain {}")
    assert ends(ev)[0]["output"].strip() == "plain-out"
    s.config["agent"] = "code"
    assert "ops_hello" not in [t["name"] for t in me.Agent(s, print).tool_schemas()]


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


def test_retry_rate_limit_and_midstream_error():
    ag, ev = run(make(), "flaky: hello")
    assert [e for e in ev if e["type"] == "retry"] and ag.session["messages"][-1]["content"] == "flaky ok", ev
    ag, ev = run(make(), "dropmid: hello")
    r = [e for e in ev if e["type"] == "retry"]
    assert r and r[0]["discard"] and ag.session["messages"][-1]["content"] == "dropmid ok"  # the partial "partial " is not kept


def test_overflow_compacts_and_retries():
    s = make()
    ev = []
    ag = me.Agent(s, ev.append)
    for i in range(4):
        ag.session["messages"] += [{"role": "user", "content": f"old question {i} " + "x" * 400}, {"role": "assistant", "content": "old answer " + "y" * 400}]
    ag.run("overflow: go")
    assert any(e["type"] == "compacted" for e in ev), [e["type"] for e in ev]
    msgs = ag.session["messages"]
    assert msgs[0]["content"].startswith("[Summary") and msgs[-1]["content"] == "overflow ok", msgs


def test_output_limit_cut_tool_call(tmp=Path(tempfile.mkdtemp())):
    ag, ev = run(make(), "cut: write a big file")
    out = ends(ev)[0]["output"]
    assert "output token limit" in out and not ends(ev)[0]["ok"], out


def test_empty_reply_is_retried():
    ag, ev = run(make(), "empty: hi")
    assert ag.session["messages"][-1]["content"] == "empty ok"
    assert all(m.get("content") or m.get("tool_calls") for m in ag.session["messages"] if m["role"] == "assistant")


def test_reasoning_goes_back_to_the_same_model(tmp=Path(tempfile.mkdtemp())):
    (tmp / "r.txt").write_text("x")
    s = make()
    ag, ev = run(s, "tool:read " + json.dumps({"path": str(tmp / "r.txt")}))
    sent = [m for m in mock_llm.LAST["messages"] if m["role"] == "assistant"]
    assert sent and sent[-1].get("reasoning_content") == "thinking about it", sent
    assert not any(k.startswith("_") for m in mock_llm.LAST["messages"] for k in m)
    # another model never receives it
    prov = me.Provider({"id": "other"}, {})
    assert "reasoning_content" not in prov.wire(ag.session["messages"])[2]
    # on disk, finished turns drop it
    ag.session["messages"].append({"role": "user", "content": "next"})
    s.save_session(ag.session)
    saved = json.loads(s.session_path(ag.session["id"]).read_text())
    assert not any("reasoning_content" in m for m in saved["messages"])


def test_edit_keeps_line_endings_and_encoding(tmp=Path(tempfile.mkdtemp())):
    s = make()
    f = tmp / "crlf.txt"
    f.write_bytes(b"one\r\ntwo\r\nthree\r\n")
    run(s, "tool:edit " + json.dumps({"path": str(f), "old": "two\nthree", "new": "2\n3"}))
    assert f.read_bytes() == b"one\r\n2\r\n3\r\n", f.read_bytes()
    g = tmp / "ru.txt"
    g.write_bytes("привет\nмир\n".encode("cp1251"))
    ag, ev = run(s, "tool:edit " + json.dumps({"path": str(g), "old": "мир", "new": "свет"}))
    assert g.read_bytes() == "привет\nсвет\n".encode("cp1251"), (g.read_bytes(), ends(ev))
    h = tmp / "bom.txt"
    h.write_bytes(b"\xef\xbb\xbfa = 1\n")
    run(s, "tool:edit " + json.dumps({"path": str(h), "old_string": "a = 1", "new_string": "a = 2"}))  # other agents' key names
    assert h.read_bytes() == b"\xef\xbb\xbfa = 2\n"
    w = tmp / "w.txt"
    w.write_bytes(b"a\r\nb\r\n")
    run(s, "tool:write " + json.dumps({"path": str(w), "content": "c\nd\n"}))
    assert w.read_bytes() == b"c\r\nd\r\n"


def test_edit_fuzzy_matches():
    t = "def f():\n    s = \"it’s\"  \n    return s\n"
    new, msg = me.apply_edits(t, {"old": 's = "it\'s"\n    return s', "new": 's = "ok"\n    return s'})
    assert new == 'def f():\n    s = "ok"\n    return s\n', (new, msg)
    # whole lines under-indented by the model
    new, msg = me.apply_edits("if x:\n        a()\n        b()\n", {"old": "    a()\n    b()", "new": "    a()\n    c()"})
    assert new == "if x:\n        a()\n        c()\n", (new, msg)
    # wrong indentation in old: the new lines are shifted to fit the file
    new, msg = me.apply_edits("class A:\n    def g(self):\n        return 1\n", {"old": "def g(self):\n    return 1", "new": "def g(self):\n    return 2"})
    assert new == "class A:\n    def g(self):\n        return 2\n" and "indentation" in msg, (new, msg)
    assert me.apply_edits("a\n", {"old": "a", "new": "a"})[0] is None
    assert me.apply_edits("a = 1\n", {"edits": json.dumps([{"oldText": "a = 1", "newText": "a = 3"}])})[0] == "a = 3\n"


def test_read_cap_and_numbering(tmp=Path(tempfile.mkdtemp())):
    f = tmp / "big.txt"
    f.write_text("\n".join("line %d %s" % (i, "z" * 100) for i in range(3000)) + "\n")
    out, _ = me.read_any(f)
    assert len(out) < 60000 and "use offset=" in out, out[-200:]
    n = int(out.rsplit("offset=", 1)[1].split(")")[0])
    more, _ = me.read_any(f, n, 5)
    assert more.startswith(f"{n}│line {n - 1} ") and "more lines" in more, more[:80]
    (tmp / "t.txt").write_text("a\nb\n")
    assert me.read_any(tmp / "t.txt")[0] == "1│a\n2│b"
    assert "did you mean: t.txt" in me.read_any(tmp / "t.tx")[0]


def test_ls_and_grep(tmp=Path(tempfile.mkdtemp())):
    (tmp / "src" / "deep").mkdir(parents=True)
    (tmp / "node_modules" / "x").mkdir(parents=True)
    (tmp / "src" / "a.ts").write_text("const x = 1\n-flag here\n")
    (tmp / "src" / "deep" / "b.tsx").write_text("export const y = 2\n")
    (tmp / "node_modules" / "x" / "c.ts").write_text("no\n")
    tb = me.ToolBox(make(), None)
    ok, out, _ = tb.run("ls", {"pattern": "**/*.{ts,tsx}"}, print, threading.Event(), cwd=str(tmp))
    assert sorted(out.split("\n")) == ["src/a.ts", "src/deep/b.tsx"], out
    ok, out, _ = tb.run("ls", {"pattern": "src"}, print, threading.Event(), cwd=str(tmp))
    assert out.split("\n") == ["deep/", "a.ts"], out
    ok, out, _ = tb.run("grep", {"pattern": "-flag"}, print, threading.Event(), cwd=str(tmp))
    assert ok and "src/a.ts:2:-flag here" in out, out
    ok, out, _ = tb.run("grep", {"pattern": "(unclosed"}, print, threading.Event(), cwd=str(tmp))
    assert not ok and "error" in out, out


def test_bash_timeout_background_and_cwd(tmp=Path(tempfile.mkdtemp())):
    tb = me.ToolBox(make(), None)
    ok, out, _ = tb.run("bash", {"cmd": "pwd"}, print, threading.Event(), cwd=str(tmp))
    assert ok and os.path.basename(str(tmp)) in out, out  # (Git Bash on Windows prints /tmp/… for the temp folder)
    ok, out, _ = tb.run("bash", {"cmd": "sleep 5", "timeout": 1}, print, threading.Event(), cwd=str(tmp))
    assert not ok and "timed out" in out, out
    ok, out, _ = tb.run("bash", {"cmd": "printf '\\033[31mred\\033[0m 10%%\\r100%%\\n'; exit 3"}, print, threading.Event(), cwd=str(tmp))
    assert not ok and "\x1b" not in out and out.startswith("100%") and "10%" not in out and "[exit 3]" in out, repr(out)
    ok, out, _ = tb.run("bash", {"cmd": "echo started; sleep 30", "background": True}, print, threading.Event(), cwd=str(tmp))
    assert ok and "pid" in out and "started" in out, out
    me.stop_background()
    assert all(p.poll() is not None for p in me.BACKGROUND) or not me.BACKGROUND


def test_compact_inside_one_long_turn():
    s = make()
    s.config["models"][0]["context"] = 4000
    ev = []
    ag = me.Agent(s, ev.append)
    ag.session["messages"] = [{"role": "user", "content": "one big task"}]
    for i in range(12):  # a single turn: many tool steps, no new user message
        ag.session["messages"] += [{"role": "assistant", "content": None, "tool_calls": [{"id": f"c{i}", "type": "function",
                                    "function": {"name": "read", "arguments": "{}"}}]}, {"role": "tool", "tool_call_id": f"c{i}", "content": "z" * 600}]
    assert ag.compact(reason="auto")
    msgs = ag.session["messages"]
    assert msgs[0]["content"].startswith("[Summary") and "one big task" in msgs[0]["content"]
    assert msgs[1]["role"] == "assistant" and msgs[1].get("tool_calls")  # starts at a step, tool results follow their call
    ids = {tc["id"] for m in msgs if m["role"] == "assistant" for tc in m.get("tool_calls") or []}
    assert all(m["tool_call_id"] in ids for m in msgs if m["role"] == "tool")


def test_tool_arg_repair():
    assert me.parse_args('```json\n{"a": 1}\n```') == {"a": 1}
    assert me.parse_args('{"a": 1} trailing') == {"a": 1}
    assert me.parse_args('"{\\"a\\": 2}"') == {"a": 2}
    assert me.parse_args('{"a": ') is None


def test_parent_agents_md(tmp=Path(tempfile.mkdtemp())):
    (tmp / ".git").mkdir()
    (tmp / "AGENTS.md").write_text("root rules: use tabs")
    (tmp / "pkg").mkdir()
    (tmp / "pkg" / "AGENTS.md").write_text("pkg rules")
    ag = me.Agent(make(), print)
    ag.session["cwd"] = str(tmp / "pkg")
    assert ag.context_files() == ["../AGENTS.md", "AGENTS.md"], ag.context_files()
    sp = ag.system_prompt()
    assert "root rules: use tabs" in sp and "pkg rules" in sp


def test_long_output_saved(tmp=Path(tempfile.mkdtemp())):
    tb = me.ToolBox(make(), None)
    ok, out, _ = tb.run("bash", {"cmd": "seq 1 20000"}, print, threading.Event(), cwd=str(tmp))
    path = out.rsplit("full output: ", 1)[1].rstrip("]")
    assert ok and len(out) < 32000 and Path(path).read_text().splitlines()[-1] == "20000", out[-200:]
def test_provider_retry_error_cancel():
    s = make()
    ag, ev = run(s, "flaky hello")  # 429 once → retried, answered
    assert any(e["type"] == "text" for e in ev) and not any(e["type"] == "error" for e in ev), ev
    ag, ev = run(s, "streamerr")
    assert any(e["type"] == "error" and "upstream overloaded" in e["text"] for e in ev)
    ev = []
    ag = me.Agent(s, ev.append)
    threading.Timer(0.6, ag.cancel.set).start()
    t0 = time.time()
    ag.run("silent please")
    assert time.time() - t0 < 4, "Esc must stop a silent model at once"
    assert any(e["type"] == "error" and e["text"] == "interrupted" for e in ev)


def test_paths_follow_session_cwd(tmp=Path(tempfile.mkdtemp())):
    # the web app runs many projects in one process: relative paths and commands must use the session's folder, not os.getcwd()
    s = make()
    (tmp / "a.txt").write_text("alpha\n")
    ag = me.Agent(s, lambda e: None)
    ag.session = me.new_session(str(tmp))
    ag.run("tool:read " + json.dumps({"path": "a.txt"}))
    assert "alpha" in ag.session["messages"][-2]["content"]
    ag.run("tool:write " + json.dumps({"path": "sub/b.txt", "content": "beta"}))
    assert (tmp / "sub" / "b.txt").read_text() == "beta"
    ag.run("tool:bash " + json.dumps({"cmd": "dir" if os.name == "nt" else "ls"}))
    assert "a.txt" in ag.session["messages"][-2]["content"]
    ag.run("tool:grep " + json.dumps({"pattern": "alpha"}))
    assert "a.txt" in ag.session["messages"][-2]["content"]
    ag.run("tool:ls " + json.dumps({"pattern": "**/*.txt"}))
    out = ag.session["messages"][-2]["content"]
    assert "a.txt" in out and "sub/b.txt" in out, out  # ls answers with / on every OS
    assert me.Agent(s, print).undo()["ok"] is False  # other session: nothing to undo
    assert ag.undo()["ok"] and not (tmp / "sub" / "b.txt").exists()


def test_history_trim_and_usage(tmp=Path(tempfile.mkdtemp())):
    s = make()
    big = tmp / "big.txt"
    big.write_text("\n".join(f"line {i} " + "x" * 60 for i in range(800)))
    ev = []
    ag = me.Agent(s, ev.append)
    ag.session = me.new_session(str(tmp))
    ag.run("tool:read " + json.dumps({"path": "big.txt"}))
    tool = [m for m in ag.session["messages"] if m["role"] == "tool"][0]
    assert len(tool["content"]) > 40000  # the current turn sees everything
    end = [e for e in ev if e["type"] == "turn_end"][0]
    assert end["steps"] == 2 and end["cached"] > 0
    ag.run("hi")
    assert tool["content"].startswith("[trimmed old output") and len(tool["content"]) < 3000  # later turns don't pay for it again
    ag.run("tool:write " + json.dumps({"path": "notes.md", "content": "x"}))
    assert not any(e["type"] == "verify" for e in ev[-20:])  # a doc edit needs no test-run round



def test_each_chat_keeps_its_model():
    s = make()
    s.config["models"].append({"name": "m2", "id": "mock-2", "provider": "mock", "api": "chat", "context": 32000})
    a, _ = run(s, "hi")
    assert a.session["model"] == "m"  # pinned on the first turn
    b = me.Agent(s, lambda e: None)
    b.set_model("m2")  # switching in another chat (or terminal) …
    a.run("hi again")
    assert mock_llm.LAST["model"] == "mock-1" and a.model_name() == "m"  # … does not reach this one
    c = me.Agent(s, lambda e: None)
    assert c.model_name() == "m2"  # a new chat starts with the model picked last
    a.session["model"] = "deleted-since"
    assert a.model()["name"] == "m2"  # a model removed since falls back to the default
    s.config["models"] = s.config["models"][:1]
    s.config["model"] = "m"


def test_todo_marks_by_number_and_reminds():
    s = make()
    ag, ev = run(s, "tool:todo " + json.dumps({"items": ["read", "fix", "test"]}))
    ag.run("tool:todo " + json.dumps({"done": [1], "doing": 2}))
    assert [i["s"] for i in ag.session["todo"]] == ["done", "doing", "todo"], ag.session["todo"]
    aux = [m["content"] for m in ag.session["messages"] if m.get("_aux") and isinstance(m["content"], str)]
    assert any("checklist still has open steps" in x and "2. [>] fix" in x for x in aux), aux  # finishing with open steps: nudged
    for _ in range(me.TODO_EVERY):
        ag.run("tool:ls " + json.dumps({"pattern": "*"}))
    assert not any("[reminder]" in str(m.get("content")) for m in ag.session["messages"])  # one tool round per turn: no reminder
    ag.session["messages"].append({"role": "user", "content": "x"})
    ok, out, _ = ag._todo({"done": "x"})
    assert not ok and "numbers" in out


def test_syntax_warning_after_edit(tmp=Path(tempfile.mkdtemp())):
    ag, ev = run(make(), "tool:write " + json.dumps({"path": str(tmp / "a.py"), "content": "def f(:\n"}))
    assert "no longer parses — line 1" in ends(ev)[0]["output"]
    ag.run("tool:write " + json.dumps({"path": str(tmp / "b.json"), "content": "{\"a\": 1}"}))
    assert me.syntax_note(tmp / "b.json", '{"a": 1}') == "" and "invalid JSON" in me.syntax_note(tmp / "b.json", "{")


def test_images_without_vision(tmp=Path(tempfile.mkdtemp())):
    s = make()
    s.config["models"].append({"name": "blind", "id": "blind-1", "provider": "mock", "api": "chat", "context": 32000})
    ev = []
    ag = me.Agent(s, ev.append)
    ag.set_model("blind")
    ag.run("what is this", ["data:image/png;base64,iVBORw0KGgo="])
    assert any(e["type"] == "notice" and "rejected the image" in e["text"] for e in ev), ev
    assert "Hello" in "".join(e["text"] for e in ev if e["type"] == "text")  # answered without the picture
    s.config["models"][-1]["vision"] = False  # vision off: images never reach the API
    ev.clear()
    ag.run("and this", ["data:image/png;base64,iVBORw0KGgo="])
    assert not any(e["type"] in ("notice", "error") for e in ev) and "image_url" not in json.dumps(mock_llm.LAST["messages"])
    s.config["models"] = s.config["models"][:1]
    s.config["model"] = "m"


def test_windows_drag_image_is_a_paste(tmp=Path(tempfile.mkdtemp())):
    img = tmp / "shot 1.png"
    img.write_bytes(b"\x89PNG")
    keys = lambda text: me.win_paste_burst(me.win_keys([("k", 1, 1, 0, ord(c), 0) for c in text], {}))
    assert keys(f'"{img}" ') == [("paste", f'"{img}" ')]
    assert keys("hello") == [("char", c) for c in "hello"]  # typed text stays typed



def test_model_picker_keeps_the_search():
    # adding several models from one provider: the typed filter and the row stay after each pick
    s = make()
    t = me.TUI.__new__(me.TUI)
    t.s, t.agent = s, me.Agent(s, lambda e: None)
    seen, answers = [], iter(["opencode/mock-1", "opencode/mock-2", None])

    def modal(m):
        shown = [it for it in m["items"] if me.fuzzy(m["filter"], it[1] + " " + it[2]) is not None] if m["filter"] else m["items"]
        seen.append((m["filter"], shown[m["sel"]][0]))
        m["filter"] = "opencode"  # the user typed a search
        return next(answers)
    t._modal = modal
    t.toast = t.info = lambda *a, **k: None
    orig = me.list_models
    me.list_models = lambda p: [{"id": "opencode/mock-1"}, {"id": "other/z"}, {"id": "opencode/mock-2"}]
    try:
        t.sync_models("mock")
    finally:
        me.list_models = orig
    assert seen == [("", "*"), ("opencode", "opencode/mock-1"), ("opencode", "opencode/mock-2")], seen
    assert {m["id"] for m in s.config["models"]} >= {"opencode/mock-1", "opencode/mock-2"}
    s.config["models"] = s.config["models"][:1]



def test_tool_arg_aliases(tmp=Path(tempfile.mkdtemp())):
    # models trained on other agents send command / file_path / old_string: they work; a missing argument is named, not a KeyError
    s = make()
    ag, ev = run(s, "tool:bash " + json.dumps({"command": "echo alias-ok"}))
    assert "alias-ok" in ends(ev)[0]["output"], ends(ev)
    f = tmp / "a.txt"
    f.write_text("one\n")
    ag.run("tool:edit " + json.dumps({"file_path": str(f), "old_string": "one", "new_string": "two"}))
    assert f.read_text() == "two\n"
    ev.clear()
    ag2, ev = run(s, "tool:bash " + json.dumps({"timeout": 5}))
    out = ends(ev)[0]["output"]
    assert "bash needs cmd" in out and "KeyError" not in out, out
    ag3, ev = run(s, "tool:bash " + json.dumps({"input": {"cmd": "echo wrapped"}}))
    assert "wrapped" in ends(ev)[0]["output"]



def test_behavior_switches(tmp=Path(tempfile.mkdtemp())):
    s = make()
    st = s.config["settings"]
    ag = me.Agent(s, lambda e: None)
    base = ag.system_prompt()
    assert "verify it (tests" in base and "Work inside cwd" in base
    st["behavior"] = {"tests": False, "todo": False, "web": False, "brief": True, "stay_in_project": False}
    p = ag.system_prompt()
    assert "verify it (tests" not in p and "Self-testing is OFF" in p and "Be terse" in p and "3+ steps" not in p and "Work inside cwd" not in p
    names = {t["name"] for t in ag.tool_schemas()}
    assert not names & {"todo", "web_search", "web_fetch"} and "bash" in names
    ev = []
    ag = me.Agent(s, ev.append)
    ag.run("tool:write " + json.dumps({"path": str(tmp / "x.py"), "content": "x = 1\n"}))
    assert not any(e["type"] == "verify" for e in ev)  # tests off: no self-check round
    for cmd in ("python3 -m pytest -q", "npm i puppeteer-core@22", "cd x && npm run build", "chromium --headless --screenshot a.png", "npx vite build",
                "godot --headless --path . --quit-after 300", "cd /tmp/chk && node check.mjs main.js"):
        ag.run("tool:bash " + json.dumps({"cmd": cmd}))
        assert "turned self-testing off" in [e for e in ev if e["type"] == "tool_end"][-1]["output"], cmd
    assert not me.CHECK_RX.search("node server.js") and not me.CHECK_RX.search("node scripts/checkout.js")
    assert not ag._no_checks("which python3 chromium google-chrome; command -v playwright") and ag._no_checks("which node; npx playwright test")
    ag.run("tool:bash " + json.dumps({"cmd": "echo fine"}))
    assert "fine" in [e for e in ev if e["type"] == "tool_end"][-1]["output"]
    ok, out, _ = ag._cli({"cmd": "npx vite build", "params": {}}, {}, lambda e: None)  # an agent's own CLI tools obey it too
    assert not ok and "turned self-testing off" in out
    st.pop("behavior")
    st["no_verify"] = True  # the old setting still means tests off
    assert me.behavior(st)["tests"] is False
    st.pop("no_verify")
    cfg = {"settings": {}}
    me.apply_setting(cfg, "behavior:commit", True)
    assert cfg["settings"]["behavior"] == {"commit": True}


def test_retry_and_export(tmp=Path(tempfile.mkdtemp())):
    s = make()
    f = tmp / "r.txt"
    ag, ev = run(s, "hi")
    ag.run("tool:write " + json.dumps({"path": str(f), "content": "new"}))
    assert f.exists()
    text, err = ag.retry()
    assert not err and text.startswith("tool:write") and not f.exists()  # files of the last turn restored
    assert [m["content"] for m in ag.session["messages"] if m["role"] == "user"] == ["hi"]
    md = ag.export_md()
    assert md.startswith("# hi") and "## You\n\nhi" in md and "Hello" in md
    rep = ag.context_report()
    assert rep.startswith("## Context") and "system prompt" in rep and "tools (" in rep
    ag.session["messages"] = []
    assert ag.retry() == (None, "nothing to retry")



def test_ui_language_and_unique_models(tmp=Path(tempfile.mkdtemp())):
    os.environ.pop("ME_LANG", None)
    assert me.set_ui_lang({"lang": "ru"}) == "ru"
    assert me.T("no models yet") == "моделей пока нет"
    assert me.T("model → gpt-5 (this chat; new chats start with it too)").startswith("модель → gpt-5 (в этом чате")
    assert me.T("some model output") == "some model output"  # anything that is not UI text stays as is
    assert me.set_ui_lang({"lang": "en"}) == "en" and me.T("no models yet") == "no models yet"
    s = make()
    s.config["models"] += [{"name": "omni/glm", "id": "cbcn/glm", "provider": "omni"}, {"name": "omni/glm", "id": "cb-cn/glm", "provider": "omni"}]
    assert s.dedupe_models() and len({m["name"] for m in s.config["models"]}) == len(s.config["models"])
    assert s.unique_model_name("x/m", "p") == "x/m"  # "m" is taken by the first model: more of the id
    s.config["models"] = s.config["models"][:1]


def test_chat_diff(tmp=Path(tempfile.mkdtemp())):
    s = make()
    f = tmp / "d.txt"
    f.write_text("one\n")
    ag, ev = run(s, "tool:write " + json.dumps({"path": str(f), "content": "two\n"}))
    ag.run("tool:write " + json.dumps({"path": str(f), "content": "three\n"}))
    (path, d), = ag.chat_diff()
    assert "-one" in d and "+three" in d and "two" not in d  # the whole chat: first snapshot against now


def test_mentions(tmp=Path(tempfile.mkdtemp())):
    s = make()
    (tmp / "src").mkdir()
    (tmp / "src" / "a.py").write_text("".join(f"line{i}\n" for i in range(1, 301)))
    ag = me.Agent(s, lambda e: None)
    ag.session["cwd"] = str(tmp)
    names, body = ag.mentions("look at @src/a.py:10-12 and @nothing.py, mail me@x.org, then @src/a.py")
    assert names == ["src/a.py:10-12", "src/a.py"] and "10\tline10" in body and "12\tline12" in body and "13\tline13" in body
    (tmp / "pic.png").write_bytes(b"\x89PNG\r\n\x1a\n\0\0")
    assert ag.mentions("no refs here, @types/node, @pic.png") is None
    ev = []
    ag = me.Agent(s, ev.append)
    ag.session["cwd"] = str(tmp)
    ag.run("what is in @src/a.py:5")
    assert any(e["type"] == "notice" and "src/a.py:5-104" in e["text"] for e in ev)
    assert "5\tline5" in json.dumps(mock_llm.LAST, ensure_ascii=False).encode().decode("unicode_escape") and [r for _, r in ag.requests()] == ["what is in @src/a.py:5"]


def test_act_nudge():
    s = make()
    ag, ev = run(s, "talk: create lru.py with an LRU cache class")
    assert sum(m.get("content") == me.ACT_NUDGE for m in ag.session["messages"]) == 1
    ag, ev = run(s, "talk: explain how an LRU cache works")
    assert not any(m.get("content") == me.ACT_NUDGE for m in ag.session["messages"])
    assert me.should_have_acted("Создай файл game.js с игрой", "```js\na\nb\nc\n```") and not me.should_have_acted("create game.js", "no code")


def test_continue_latest(tmp=Path(tempfile.mkdtemp())):
    s = make()
    cwd = os.getcwd()
    try:
        os.chdir(tmp)
        ag, ev = run(s, "remember kiwi")
        assert s.latest(tmp) == ag.session["id"] and s.latest(tmp / "elsewhere") is None
        s.config["models"].append({"name": "deep", "id": "deepseek-x", "provider": "mock", "api": "chat"})
        assert me.pick_model(s, "m") == "m" and me.pick_model(s, "SEEK") == "deep" and me.pick_model(s, "x") is None  # x: ambiguous
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            me.run_headless(s, "and now?", True, s.latest(tmp))  # me -p -c: the follow-up lands in the same chat
        assert out.getvalue().strip() == "Hello world!\n\n```py\nprint(1)\n```" or "· " not in out.getvalue(), out.getvalue()
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            me.run_headless(s, "tool:bash " + json.dumps({"cmd": "echo hi"}), True)
        assert "· bash" in err.getvalue() and "· bash" not in out.getvalue() and out.getvalue().strip(), (out.getvalue(), err.getvalue())
        d = json.loads(s.session_path(ag.session["id"]).read_text())
        assert [m["content"] for m in d["messages"] if m["role"] == "user"][:2] == ["remember kiwi", "and now?"]
    finally:
        os.chdir(cwd)


def test_rewind_fork_shell_search_btw(tmp=Path(tempfile.mkdtemp())):
    s = make()
    f = tmp / "w.txt"
    ag, ev = run(s, "first zebra")
    ag.run("tool:write " + json.dumps({"path": str(f), "content": "a"}))
    ag.run("tool:write " + json.dumps({"path": str(f), "content": "b"}))
    reqs = ag.requests()
    assert [t for _, t in reqs][0] == "first zebra" and len(reqs) == 3
    sid = ag.session["id"]
    ag.fork()
    assert ag.session["id"] != sid and len(ag.requests()) == 3 and ag.session["forked_from"] == sid
    text, files = ag.rewind(reqs[1][0])  # back to before the first write: both writes undone
    assert text.startswith("tool:write") and not f.exists() and files and [t for _, t in ag.requests()] == ["first zebra"]
    assert len(json.loads(s.session_path(sid).read_text())["messages"]) > len(ag.session["messages"])  # the original is untouched
    ok, out = ag.shell("echo hello-shell")
    assert ok and "hello-shell" in out and ag.session["messages"][-1].get("_shell") and len(ag.requests()) == 1
    assert ag.retry()[0] == "first zebra"  # a shell note is not a request
    hits = s.search("ZEBRA first")
    assert hits and "zebra" in hits[0][1] and not s.search("zebra nothing-like-this")
    n = len(ag.session["messages"])
    assert ag.side_question("what is this?") and len(ag.session["messages"]) == n  # /btw stays out of the history



def test_market_names_unique():
    """The marketplace keys items by name alone: an agent and a skill with one name overwrite each other."""
    seen = {}
    for p in sorted((Path(__file__).parent.parent / "server" / "market").glob("*.*")):
        m = re.search(r"^name:\s*(\S+)", p.read_text(encoding="utf-8"), re.M)
        n = m.group(1) if m else p.stem
        assert n not in seen, f"{p.name} and {seen[n]} are both named {n}"
        seen[n] = p.name


if __name__ == "__main__":
    fails = 0
    for k, v in list(globals().items()):
        if k.startswith("test_"):
            try:
                v()
                print("ok  ", k)
            except ModuleNotFoundError as e:  # optional test deps (Pillow) missing: skip, don't fail
                print("skip", k, e)
            except Exception as e:
                fails += 1
                import traceback
                traceback.print_exc()
                print("FAIL", k, e)
    sys.exit(1 if fails else 0)
