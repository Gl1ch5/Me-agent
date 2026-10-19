"""Run: python3 tests/test_studio.py   (needs git) — me studio: parallel tasks in worktrees, review, apply, commit, discard."""
import json, os, subprocess, sys, tempfile, threading, time
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "apps" / "studio"))
os.environ["ME_HOME"] = tempfile.mkdtemp()
import me  # noqa: E402
import mock_llm  # noqa: E402
import me_studio  # noqa: E402

srv = ThreadingHTTPServer(("127.0.0.1", 0), mock_llm.H)
threading.Thread(target=srv.serve_forever, daemon=True).start()


def studio():
    st = me_studio.Studio()
    st.store.config["providers"]["mock"] = {"base_url": f"http://127.0.0.1:{srv.server_port}/v1", "api_key": "x"}
    st.store.config["models"] = [{"name": "m", "id": "mock-1", "provider": "mock", "api": "chat", "context": 32000}]
    st.store.config["model"] = "m"
    st.store.config["settings"]["perm"] = "full"
    return st


def repo():
    d = tempfile.mkdtemp(prefix="proj-")
    git = lambda *a: subprocess.run(["git", *a], cwd=d, check=True, capture_output=True)
    git("init", "-q")
    Path(d, "app.py").write_text("print('v1')\n")
    git("add", "-A")
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")
    return d


def wait(st, tid, timeout=20):
    t = st.task(tid)
    end = time.time() + timeout
    while time.time() < end:
        if not t.busy and t.meta["status"] not in ("running", "waiting", "idle"):
            return t
        time.sleep(0.05)
    raise AssertionError(f"task {tid} still {t.meta['status']}")


def write(path, text):
    return "tool:write " + json.dumps({"path": path, "content": text})


def test_parallel_worktrees_review_apply_commit_discard():
    st, proj = studio(), repo()
    a = st.new_task(proj, write("a.py", "A = 1\n"), "worktree")
    b = st.new_task(proj, write("b.py", "B = 2\n"), "worktree")
    ta, tb = wait(st, a), wait(st, b)
    assert ta.meta["cwd"] != tb.meta["cwd"] != proj and ta.meta["branch"] != tb.meta["branch"]
    assert Path(ta.meta["cwd"]) == Path(ta.meta["wt"]) and Path(tb.meta["cwd"]) == Path(tb.meta["wt"]), (ta.meta, tb.meta)
    assert Path(ta.meta["cwd"], "a.py").is_file() and not Path(proj, "a.py").exists()  # the checkout is untouched
    da = ta.diff()
    assert [f["path"] for f in da["files"]] == ["a.py"] and da["files"][0]["new"] and da["files"][0]["add"] == 1, da
    assert ta.meta["status"] == "review"
    assert "applied 1 file" in st.apply(a)
    assert Path(proj, "a.py").read_text() == "A = 1\n"
    assert "committed" in st.commit(b, "add b")
    log = subprocess.run(["git", "log", "--oneline", tb.meta["branch"]], cwd=proj, capture_output=True, text=True).stdout
    assert "add b" in log
    assert st.task(b).diff()["commits"] == 1
    st.discard(b)
    branches = subprocess.run(["git", "branch", "--list", "me/*"], cwd=proj, capture_output=True, text=True).stdout
    assert tb.meta["branch"] not in branches and not os.path.isdir(tb.meta["wt"])
    state = st.state()
    assert {t["id"] for t in state["tasks"]} == {a, b} and state["projects"][0]["git"]


def test_worktree_cwd_when_git_spells_the_root_differently():
    # git reports the root resolved (long names on Windows, no symlinks): the task must still land inside its worktree
    st, real = studio(), repo()
    Path(real, "sub").mkdir()
    Path(real, "sub", "keep.txt").write_text("k\n")
    subprocess.run(["git", "add", "-A"], cwd=real, check=True)
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "sub"], cwd=real, check=True)
    link = Path(tempfile.mkdtemp(), "link")
    try:
        link.symlink_to(real, target_is_directory=True)
    except (OSError, NotImplementedError):  # Windows without the symlink privilege
        link = Path(real)
    tid = st.new_task(str(link / "sub"), write("c.py", "C = 3\n"), "worktree")
    t = wait(st, tid)
    assert Path(t.meta["cwd"]) == Path(t.meta["wt"], "sub"), t.meta
    assert Path(t.meta["wt"], "sub", "c.py").is_file()
    assert [f["path"] for f in t.diff()["files"]] == ["sub/c.py"]
    st.discard(tid)


def test_local_task_follow_up_and_undo():
    st = studio()
    proj = tempfile.mkdtemp(prefix="plain-")
    tid = st.new_task(proj, write("notes.txt", "hello\n"), "local")
    t = wait(st, tid)
    assert Path(proj, "notes.txt").read_text() == "hello\n"
    d = t.diff()
    assert [f["path"] for f in d["files"]] == ["notes.txt"], d
    t.send("thanks")  # a follow-up in the same thread
    t = wait(st, tid)
    assert t.agent.session["messages"][-1]["content"].startswith("Hello")
    assert me_studio.route(st, "/api/task/undo", {"id": tid})["ok"]  # the turn that wrote the file (the follow-up changed nothing)
    assert not Path(proj, "notes.txt").exists()


def test_worktree_needs_git_and_a_commit():
    st = studio()
    plain = tempfile.mkdtemp()
    try:
        st.new_task(plain, "x", "worktree")
        raise AssertionError("no error")
    except ValueError as e:
        assert "git" in str(e)


def test_task_model_and_reload():
    st = studio()
    proj = tempfile.mkdtemp()
    tid = st.new_task(proj, "hi", "local", model="m", effort="low")
    t = wait(st, tid)
    assert t.agent.session["model"] == "m" and t.agent.session["effort"] == "low"
    st.save()
    st2 = studio()
    t2 = st2.task(tid)  # survives a restart: the thread comes back from its session
    assert t2.agent.session["messages"] and t2.meta["title"] == "hi"


def test_http_api_requires_key():
    st = studio()
    server, url = me_studio.serve(st)
    import urllib.request, urllib.error
    base = url.split("/?")[0]
    try:
        urllib.request.urlopen(base + "/api/state")
        raise AssertionError("served without the key")
    except urllib.error.HTTPError as e:
        assert e.code == 401
    key = url.split("k=")[1]
    r = urllib.request.Request(base + "/api/state", headers={"X-Key": key})
    assert "tasks" in json.loads(urllib.request.urlopen(r).read())
    page = urllib.request.urlopen(url).read().decode()
    assert "me studio" in page and key in page
    server.shutdown()


if __name__ == "__main__":
    fails = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            try:
                fn()
                print("ok  ", name)
            except Exception:
                import traceback
                traceback.print_exc()
                print("FAIL", name)
                fails += 1
    sys.exit(1 if fails else 0)
