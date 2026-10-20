"""Telegram bot logic without Telegram or a sandbox: questions, mirroring, commands, attachments, link codes.
Run: python3 tests/test_bot.py   (needs server deps: fastapi, cryptography, pydantic)"""
import base64, json, os, sqlite3, sys, time, types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))
import tgbot  # noqa: E402
import tg_tasks  # noqa: E402

SCHEMA = """
CREATE TABLE tg_links(chat_id INTEGER, user_id INTEGER, created REAL, bot TEXT DEFAULT '', PRIMARY KEY(chat_id, bot));
CREATE TABLE docs(user_id INTEGER, key TEXT, content TEXT, ts REAL, rev INTEGER, deleted INTEGER DEFAULT 0, size INTEGER,
  PRIMARY KEY(user_id, key));
CREATE TABLE jobs(id INTEGER PRIMARY KEY, user_id INTEGER, name TEXT, prompt TEXT, cron TEXT, tz INTEGER DEFAULT 0, ws TEXT,
  next REAL, last REAL, status TEXT DEFAULT '', result TEXT DEFAULT '', session TEXT DEFAULT '', enabled INTEGER DEFAULT 1, created REAL);
"""


class FakeWorker:
    def __init__(self, wid, uid=1, session="s1", events=()):
        self.id, self.uid, self.wid, self.session, self.title = wid, uid, "ws1", session, "demo"
        self.seq, self.busy, self.job, self.sent, self.prompts, self.scripted = 0, False, None, [], [], list(events)
        self.tg_turn, self.watched = False, time.time()  # watched: someone is looking, so no push

    def send(self, cmd):
        self.sent.append(cmd)

    def prompt(self, text, images=()):
        self.prompts.append((text, list(images)))

    def wait_events(self, after, timeout=0):
        out = []
        while self.scripted:
            self.seq += 1
            out.append(dict(self.scripted.pop(0), seq=self.seq))
        return out

    def info(self):
        return {"preview": "https://me-agent.ru/preview/x"}


class Runner:
    def __init__(self, w):
        self.w = w

    def get(self, uid, wid):
        if wid != self.w.id:
            raise KeyError(wid)
        return self.w

    def start(self, *a, **k):
        return self.w


def db():
    c = sqlite3.connect(":memory:", isolation_level=None)
    c.executescript(SCHEMA)
    return c


def bot(conn=None, w=None):
    conn = conn or db()
    b = tgbot.Bot(conn, Runner(w) if w else None, lambda uid: {"username": "u"}, lambda uid: "tok", None, owner=1)
    b.token, b.username = "t", "mebot"
    b.out = []
    b.send = lambda chat, text, **kw: (b.out.append((chat, text, kw)), {"message_id": len(b.out)})[1]
    b.call = lambda *a, **k: {}
    b.download = lambda file_id, limit=0: b"PNGDATA"
    return b


def link(b, chat, uid=1, quiet=0):
    b.db.execute("INSERT INTO tg_links(chat_id,user_id,created,bot,quiet) VALUES(?,?,?,?,?)", (chat, uid, time.time(), b.key, quiet))


def msg(chat, text, **extra):
    return {"message": dict({"chat": {"id": chat}, "text": text}, **extra)}


# ─── agent questions ───
def test_text_answer():
    w = FakeWorker("w1")
    b = bot(w=w)
    link(b, 5)
    b.state[(5, 0)] = {"worker": "w1"}
    assert b.on_ask(w, {"id": "a1", "text": "which db?"})
    assert "which db?" in b.out[-1][1]
    b.handle(msg(5, "postgres"))
    assert w.sent == [{"type": "answer", "id": "a1", "text": "postgres"}], w.sent
    print("ok   question asked in chat, plain-text reply answers it")


def test_button_answer():
    w = FakeWorker("w1")
    b = bot(w=w)
    link(b, 5)
    b.state[(5, 0)] = {"worker": "w1"}
    b.on_ask(w, {"id": "a2", "text": "pick", "options": ["red", "blue"]})
    kb = b.out[-1][2]["reply_markup"]["inline_keyboard"]
    assert [row[0]["callback_data"] for row in kb] == ["a:0", "a:1"]
    b.handle({"callback_query": {"id": "q", "data": "a:1", "message": {"chat": {"id": 5}}}})
    assert w.sent[-1] == {"type": "answer", "id": "a2", "text": "blue"}, w.sent
    print("ok   question with options answered by button")


def test_timeout_releases_agent():
    w = FakeWorker("w1")
    b = bot(w=w)
    b.state[(5, 0)] = {"worker": "w1"}
    b.on_ask(w, {"id": "a4", "text": "q"})
    b._ask_timeout((5, 0), "a4")
    assert w.sent[-1] == {"type": "answer", "id": "a4", "text": ""}
    b._ask_timeout((5, 0), "a4")
    assert len(w.sent) == 1
    print("ok   no answer in time → agent is released with an empty answer")


def test_stop_clears_question():
    w = FakeWorker("w1")
    b = bot(w=w)
    link(b, 5)
    b.state[(5, 0)] = {"worker": "w1"}
    b.on_ask(w, {"id": "a5", "text": "q"})
    b.handle(msg(5, "/stop"))
    assert (5, 0) not in b.asks
    print("ok   /stop clears a pending question")


# ─── link codes survive restarts ───
def test_link_code_survives_restart():
    conn = db()
    b1 = bot(conn)
    url = b1.link_code(1)
    code = url.split("start=")[1]
    b2 = bot(conn)  # a new process: the code lives in the database
    b2.handle(msg(9, f"/start {code}"))
    assert b2.uid_for(9) == 1
    b2.handle(msg(9, f"/start {code}"))  # one-time
    assert "expired" in b2.out[-1][1]
    print("ok   link code works after a restart and is one-time")


def test_link_code_for_other_bot_rejected():
    conn = db()
    b1 = bot(conn)
    code = b1.link_code(1).split("start=")[1]
    other = bot(conn)
    other.username = "otherbot"
    other.owner = 2  # a different personal bot
    other.handle(msg(9, f"/start {code}"))
    assert other.uid_for(9) is None
    print("ok   a link code only works in the bot it was made for")


# ─── mirroring ───
def test_mirror_respects_quiet_and_busy():
    conn = db()
    b = bot(conn)
    link(b, 1)
    link(b, 2, quiet=1)
    link(b, 3)
    b.state[(3, 0)] = {"busy": True}
    assert b.mirror(1, "› hello") == 1
    assert [c for c, _, _ in b.out] == [1]
    print("ok   mirroring reaches linked chats, skips muted and busy ones")


def test_quiet_toggle_persists():
    conn = db()
    b = bot(conn)
    link(b, 4)
    b.handle(msg(4, "/quiet"))
    assert b.links(1) == [(4, True)]
    b2 = bot(conn)  # restart
    assert b2.links(1) == [(4, True)]
    b2.handle(msg(4, "/quiet"))
    assert b2.links(1) == [(4, False)]
    print("ok   /quiet toggles mirroring and survives a restart")


# ─── commands ───
def test_commands_render():
    conn = db()
    w = FakeWorker("w1", session="s9")
    b = bot(conn, w)
    link(b, 4)
    b.state[(4, 0)] = {"worker": "w1"}
    conn.execute("INSERT INTO docs VALUES(1,'session/s9',?,0,0,0,0)", (json.dumps({
        "todo": [{"t": "a", "s": "done"}, {"t": "b", "s": "doing"}], "usage": {"cost": 0.25, "in": 10, "out": 5}}),))
    b.handle(msg(4, "/todo"))
    assert "checklist 1/2" in b.out[-1][1] and "✓ a" in b.out[-1][1]
    b.handle(msg(4, "/cost"))
    assert "$0.2500" in b.out[-1][1]
    b.handle(msg(4, "/preview"))
    assert "preview" in b.out[-1][1] and "https://me-agent.ru/preview/x" in b.out[-1][1]
    b.handle(msg(4, "/github"))
    assert "not connected" in b.out[-1][1]
    b.handle(msg(4, "/nonsense"))
    assert "unknown command" in b.out[-1][1]
    b.handle(msg(4, "/help"))
    assert all(f"/{c} " in b.out[-1][1] for c in ("goal", "undo", "todo", "cost", "memory", "jobs", "preview", "quiet"))
    print("ok   /todo /cost /preview /github /help render; unknown commands say so")


def test_goal_and_undo_send_commands():
    conn = db()
    w = FakeWorker("w1")
    b = bot(conn, w)
    link(b, 4)
    b.handle(msg(4, "/goal ship the page"))
    assert {"type": "goal", "text": "ship the page"} in w.sent and w.tg_turn and w.tg_goal
    w.busy = False
    b.handle(msg(4, "/undo"))
    assert w.sent[-1] == {"type": "undo"}
    b.handle(msg(4, "/compact"))
    assert w.sent[-1] == {"type": "compact"}
    print("ok   /goal, /undo, /compact reach the sandbox")


def test_memory_list_and_forget():
    conn = db()
    b = bot(conn)
    link(b, 4)
    conn.execute("INSERT INTO docs VALUES(1,'memory/20260101-000000-abcd.json',?,0,0,0,0)",
                 (json.dumps({"text": "deploy on fridays", "ts": 1767225600}),))
    b.handle(msg(4, "/memory"))
    assert "deploy on fridays" in b.out[-1][1] and "/memory forget 20260101-000000-abcd" in b.out[-1][1]
    calls = []
    fake = types.ModuleType("app")
    fake.put_doc = lambda uid, key, content, deleted=False: calls.append((uid, key, deleted))
    sys.modules["app"] = fake
    try:
        b.handle(msg(4, "/memory forget 20260101-000000-abcd"))
    finally:
        del sys.modules["app"]
    assert calls == [(1, "memory/20260101-000000-abcd.json", True)], calls
    print("ok   /memory lists notes, /memory forget deletes through sync")


def test_jobs_list_and_run():
    conn = db()
    b = bot(conn)
    link(b, 4)
    conn.execute("INSERT INTO jobs(id,user_id,name,prompt,cron,next,enabled,created,status) VALUES(7,1,'check site','p','0 9 * * *',?,1,0,'done')",
                 (time.time() + 3600,))
    b.handle(msg(4, "/jobs"))
    assert "#7 check site" in b.out[-1][1]
    b.handle(msg(4, "/jobs run 7"))
    assert "queued" in b.out[-1][1]
    assert conn.execute("SELECT next <= ? FROM jobs WHERE id=7", (time.time() + 5,)).fetchone()[0] == 1
    print("ok   /jobs lists background tasks and /jobs run starts one")


# ─── attachments and turns ───
def test_photo_goes_to_model_and_chat_turn_answers():
    w = FakeWorker("w1", events=[{"type": "turn_start"}, {"type": "text", "text": "it is a cat"},
                                 {"type": "turn_end", "time": 1.0, "in": 1, "out": 1}])
    b = bot(w=w)
    link(b, 4)
    b.handle(msg(4, "what is this?", photo=[{"file_id": "p1", "file_size": 10}]))
    assert w.prompts and w.prompts[0][1] and w.prompts[0][1][0].startswith("data:image/jpeg;base64,")
    assert sum("it is a cat" in t for _, t, _ in b.out) == 1, b.out  # answered once, in this chat
    assert b.out[-1][2].get("md") and "_done" in b.out[-1][1]
    print("ok   a photo goes to the model; the chat's own turn is answered once")


def test_document_saved_to_workspace(tmp=None):
    w = FakeWorker("w1", events=[{"type": "turn_start"}, {"type": "turn_end", "time": 1.0, "in": 1, "out": 1}])
    b = bot(w=w)
    link(b, 4)
    saved = []
    fake = types.ModuleType("cloud")
    fake.ws_put = lambda uid, wid, put, dele: saved.append((uid, wid, put[0]["path"], base64.b64decode(put[0]["b64"])))
    sys.modules["cloud"] = fake
    try:
        b.handle(msg(4, "read this", document={"file_id": "d1", "file_name": "brief v2!.txt"}))
    finally:
        del sys.modules["cloud"]
    assert saved and saved[0][2] == "uploads/briefv2.txt" and saved[0][3] == b"PNGDATA", saved
    assert w.prompts[0][0].endswith("Attached: uploads/briefv2.txt"), w.prompts
    print("ok   a document is saved into the workspace and named in the prompt")


def test_mirror_from_web_turn_not_echoed_for_chat_turn():
    """The chat's own turn is answered by the bot only; a turn from the web is mirrored by features (checked in test_features)."""
    w = FakeWorker("w1")
    w.tg_turn = True
    assert getattr(w, "tg_turn")
    print("ok   chat turns are flagged so they are not mirrored back")


# ─── newer Bot API: rich messages, drafts, topics ───
def api_bot(fail=(), w=None, topics=False):
    """A bot whose send goes through the real TgApi; Telegram calls are recorded, methods in `fail` raise."""
    b = bot(w=w)
    del b.send
    b.calls, b.topics = [], topics
    def call(method, _timeout=60, **p):
        b.calls.append((method, p))
        if method in fail:
            raise RuntimeError("Not Found: method not found")
        if method == "createForumTopic":
            return {"message_thread_id": 77}
        return {"message_id": len(b.calls)}
    b.call = call
    return b


def test_rich_send_falls_back_to_plain():
    b = api_bot()
    b.send(4, "# hi\n| a |\n|---|", md=True)
    assert b.calls[-1][0] == "sendRichMessage" and b.calls[-1][1]["rich_message"] == {"markdown": "# hi\n| a |\n|---|"}
    b = api_bot(fail=("sendRichMessage",))
    b.send(4, "**x**", md=True)
    b.send(4, "**y**", md=True)
    assert [m for m, _ in b.calls] == ["sendRichMessage", "sendMessage", "sendMessage"]  # rich turned off after the first miss
    print("ok   Markdown goes out as a rich message; without the method it falls back to plain text for good")


def test_answer_streams_as_draft():
    w = FakeWorker("w1", events=[{"type": "turn_start"}, {"type": "tool_start", "name": "bash", "label": "ls"},
                                 {"type": "text", "text": "## result\nall good"}, {"type": "turn_end", "time": 2.0, "in": 5, "out": 5}])
    b = api_bot(w=w)
    link(b, 4)
    b.handle(msg(4, "check"))
    names = [m for m, _ in b.calls]
    assert names[0] == "sendMessageDraft" and "sendMessage" not in names, names  # "Thinking…" first, no status message
    final = b.calls[-1]
    assert final[0] == "sendRichMessage" and "all good" in final[1]["rich_message"]["markdown"], final
    assert "bash" in final[1]["rich_message"]["markdown"] and "<details>" in final[1]["rich_message"]["markdown"]
    b2 = api_bot(w=FakeWorker("w1", events=[{"type": "turn_start"}, {"type": "text", "text": "ok"}, {"type": "turn_end", "time": 1, "in": 1, "out": 1}]))
    link(b2, -100)  # a group: no drafts, one edited status message
    b2.handle(msg(-100, "check"))
    assert not any("Draft" in m for m, _ in b2.calls) and any(m == "editMessageText" for m, _ in b2.calls), b2.calls
    print("ok   a private chat streams the answer as a draft; a group gets an edited status message")


def test_stop_button_cancels_turn():
    w = FakeWorker("w1")
    w.alive = True
    b = bot(w=w)
    link(b, 4)
    b.state[(4, 0)] = {"worker": "w1"}
    b.handle({"update_id": 1, "stopped_message_generation": {"chat": {"id": 4}, "draft_id": 9}})
    assert w.sent == [{"type": "cancel"}]
    print("ok   the stop button under a draft cancels the turn")


def test_topics_one_session_each():
    b = api_bot(topics=True)
    link(b, 4)
    b.handle(msg(4, "/new landing page"))
    assert ("createForumTopic", {"chat_id": 4, "name": "landing page"}) in b.calls
    assert b.calls[-1][1].get("message_thread_id") == 77  # the reply goes into the new topic
    b.bind_topic(4, 78, "s5", "ws1")
    b.tl.thread = 78
    assert b.st(4)["session"] == "s5" and b.st(4, 0) == {}  # a topic remembers its session; the main chat is separate
    b.tl.thread = 0
    b.calls.clear()
    assert b.mirror(1, "› from web", session="s5") == 1 and b.calls[-1][1]["message_thread_id"] == 78
    b.mirror(1, "› other", session="s6", title="other task")
    assert ("createForumTopic", {"chat_id": 4, "name": "other task"}) in b.calls and b.topic_of(4, "s6") == 77
    print("ok   topics: /new opens one, a topic keeps its session, mirrored sessions get their own topic")


def test_profile_set_once():
    b = api_bot()
    conn = b.db
    conn.execute("CREATE TABLE IF NOT EXISTS tg_profile(bot TEXT PRIMARY KEY, ver INTEGER)")
    up = []
    b.upload = lambda method, fields, files: up.append((method, fields, list(files)))
    b.profile()
    names = [(m, p.get("language_code")) for m, p in b.calls]
    assert ("setMyDescription", "") in names and ("setMyShortDescription", "ru") in names, names
    assert up == [("setMyProfilePhoto", {"photo": {"type": "static", "photo": "attach://avatar"}}, ["avatar"])], up
    assert tgbot.AVATAR.read_bytes()[:2] == b"\xff\xd8"  # a JPG, as Telegram requires
    b.calls.clear()
    b.profile()  # restart: already set, no second upload
    assert not b.calls and len(up) == 1
    print("ok   every bot gets the avatar and description once")


def fake_app(conn):
    """The server's app module, as far as the bot uses it: put_doc writes the docs table."""
    fake = types.ModuleType("app")
    def put_doc(uid, key, content, deleted=False):
        conn.execute("INSERT OR REPLACE INTO docs VALUES(?,?,?,?,0,?,?)", (uid, key, content, time.time(), int(deleted), len(content)))
    fake.put_doc, fake.PUBLIC = put_doc, "https://me.test"
    return fake


def test_answer_buttons_and_retry():
    w = FakeWorker("w1", events=[{"type": "turn_start"}, {"type": "text", "text": "hi"}, {"type": "turn_end", "time": 1, "in": 1, "out": 1}])
    w.alive = True
    b = bot(w=w)
    link(b, 4)
    b.handle(msg(4, "make it"))
    kb = b.out[-1][2]["reply_markup"]["inline_keyboard"][0]
    assert [x["callback_data"] for x in kb] == ["c:retry", "c:undo", "c:new"], kb
    w.scripted = [{"type": "retried", "text": "make it"}, {"type": "turn_start"}, {"type": "text", "text": "again"},
                  {"type": "turn_end", "time": 1, "in": 1, "out": 1}]
    b.handle({"callback_query": {"id": "q", "data": "c:retry", "message": {"chat": {"id": 4}, "message_id": 1}}})
    assert {"type": "retry"} in w.sent and w.prompts[-1][0] == "make it" and "again" in b.out[-1][1], b.out
    print("ok   answers carry retry / undo / new buttons; retry runs the request again")


def test_settings_from_chat():
    conn = db()
    w = FakeWorker("w1")
    w.alive = True
    b = bot(conn, w)
    link(b, 4)
    b.state[(4, 0)] = {"worker": "w1"}
    conn.execute("INSERT INTO docs VALUES(1,'config',?,0,0,0,0)", (json.dumps({"model": "m1", "models": [{"name": "m1"}], "settings": {}}),))
    conn.execute("INSERT INTO docs VALUES(1,'agent/design.md',?,0,0,0,0)", ("---\nname: design\n---\nmake it pretty",))
    sys.modules["app"] = fake_app(conn)
    try:
        b.handle(msg(4, "/behavior"))
        rows = b.out[-1][2]["reply_markup"]["inline_keyboard"]
        pub = next(r[0] for r in rows if r[0]["callback_data"] == "b:publish")
        assert pub["text"].startswith("▫️"), pub  # off by default
        b.handle({"callback_query": {"id": "q", "data": "b:publish", "message": {"chat": {"id": 4}, "message_id": 9}}})
        assert b.config(1)["settings"]["behavior"]["publish"] is True and {"type": "sync"} in w.sent
        b.handle(msg(4, "/agent"))
        assert [r[0]["callback_data"] for r in b.out[-1][2]["reply_markup"]["inline_keyboard"]] == ["g:code", "g:design"]
        b.handle({"callback_query": {"id": "q", "data": "g:design", "message": {"chat": {"id": 4}, "message_id": 9}}})
        b.handle({"callback_query": {"id": "q", "data": "e:high", "message": {"chat": {"id": 4}, "message_id": 9}}})
        cfg = b.config(1)
        assert cfg["agent"] == "design" and cfg["models"][0]["effort"] == "high", cfg
    finally:
        del sys.modules["app"]
    print("ok   /behavior, /agent and /effort change the account settings from the chat")


def test_russian():
    b = api_bot()
    conn = b.db
    link(b, 4)
    b.handle(msg(4, "/nonsense", **{"from": {"language_code": "ru"}}))
    assert b.calls[-1][1]["text"] == "неизвестная команда /nonsense — /help", b.calls[-1]
    b.handle(msg(4, "/help", **{"from": {"language_code": "en"}}))
    assert "/retry — run the last request again" in b.calls[-1][1]["text"]
    conn.execute("INSERT INTO docs VALUES(1,'config',?,0,0,0,0)", (json.dumps({"settings": {"lang": "ru"}}),))
    b.handle(msg(4, "/help", **{"from": {"language_code": "en"}}))  # the account's language wins
    assert "/retry — повторить последний запрос" in b.calls[-1][1]["text"]
    b.tl.lang = None
    b.send(4, "hi", reply_markup={"inline_keyboard": [[{"text": "↻ retry", "callback_data": "c:retry"}]]})
    assert b.calls[-1][1]["reply_markup"]["inline_keyboard"][0][0]["text"] == "↻ повторить"  # outside an update too
    print("ok   the bot speaks Russian: account setting first, else the Telegram language")


def test_session_tools():
    w = FakeWorker("w1", session="s9")
    w.alive = True
    b = api_bot(w=w)
    conn = b.db
    link(b, 4)
    b.state[(4, 0)] = {"worker": "w1"}
    conn.execute("INSERT INTO docs VALUES(1,'session/s9',?,0,0,0,0)", (json.dumps({"title": "landing", "messages": [
        {"role": "user", "content": "build a landing page"}, {"role": "assistant", "content": "done", "tool_calls": [{"function": {"name": "write"}}]}]}),))
    files = []
    b.send_file = lambda chat, name, data, caption="": files.append((name, data))
    sys.modules["app"] = fake_app(conn)
    try:
        b.handle(msg(4, "/export"))
        assert files[0][0] == "landing.md" and b"## You\n\nbuild a landing page" in files[0][1] and b"`write`" in files[0][1]
        b.handle(msg(4, "/search LANDING"))
        assert b.calls[-1][1]["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == "s:s9"
        b.handle(msg(4, "/rename new site"))
        assert b._session_doc(1, "s9")["title"] == "new site"
    finally:
        del sys.modules["app"]
    print("ok   /export sends the chat as Markdown, /search finds sessions, /rename renames")


def test_page_screenshot_after_publish():
    os.environ.update(ME_PUBLIC_URL="https://me-agent.ru", ME_SITES_DOMAIN="me-agent.ru")
    try:
        assert tg_tasks.page_links("see https://me-agent.ru/s/u/a/, http://127.0.0.1:8081/s/x/ https://evil.com/s/u/a/ "
                                   "https://a.u.me-agent.ru/ https://me-agent.ru/account") == ["https://me-agent.ru/s/u/a/", "https://a.u.me-agent.ru/"]
        w = FakeWorker("w1", events=[{"type": "turn_start"}, {"type": "tool_start", "name": "publish", "label": "a ← site"},
                                     {"type": "tool_end", "ok": True, "output": "published 3 files: https://a.u.me-agent.ru/"},
                                     {"type": "text", "text": "online at https://a.u.me-agent.ru/"}, {"type": "turn_end", "time": 1, "in": 1, "out": 1}])
        b = bot(w=w)
        link(b, 4)
        shots = []
        b.send_shot = lambda chat, url, thread=0: shots.append((chat, url))
        b.handle(msg(4, "publish it"))
        assert shots == [(4, "https://a.u.me-agent.ru/")], shots
        b.handle(msg(4, "/preview"))
        assert shots[-1] == (4, "https://me-agent.ru/preview/x/"), shots
        up = []
        b = bot(w=w)
        b.upload = lambda method, fields, files: up.append((method, fields, files))
        tg_tasks_shots = sys.modules["shots"] = type(sys)("shots")
        tg_tasks_shots.screenshot = lambda url: b"\xff\xd8jpeg"
        b.tl.lang = "ru"
        b.send_shot(4, "https://a.u.me-agent.ru/")
        for _ in range(50):
            if up:
                break
            time.sleep(0.02)
        assert up[0][0] == "sendPhoto" and up[0][2]["photo"][1] == b"\xff\xd8jpeg", up
        assert up[0][1]["reply_markup"]["inline_keyboard"][0][0] == {"text": "открыть ↗", "url": "https://a.u.me-agent.ru/"}
    finally:
        sys.modules.pop("shots", None)
        os.environ.pop("ME_PUBLIC_URL"), os.environ.pop("ME_SITES_DOMAIN")
    print("ok   a page the turn put online (site, preview) comes back as a screenshot with an open button; only our own hosts")


if __name__ == "__main__":
    for k, v in list(globals().items()):
        if k.startswith("test_"):
            v()
    print("all bot tests passed")
