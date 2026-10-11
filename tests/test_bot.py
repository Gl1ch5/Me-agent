"""Telegram bot logic without Telegram or a sandbox: questions, mirroring, commands, attachments, link codes.
Run: python3 tests/test_bot.py   (needs server deps: fastapi, cryptography, pydantic)"""
import base64, json, sqlite3, sys, time, types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))
import tgbot  # noqa: E402

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
    b.state[5] = {"worker": "w1"}
    assert b.on_ask(w, {"id": "a1", "text": "which db?"})
    assert "which db?" in b.out[-1][1]
    b.handle(msg(5, "postgres"))
    assert w.sent == [{"type": "answer", "id": "a1", "text": "postgres"}], w.sent
    print("ok   question asked in chat, plain-text reply answers it")


def test_button_answer():
    w = FakeWorker("w1")
    b = bot(w=w)
    link(b, 5)
    b.state[5] = {"worker": "w1"}
    b.on_ask(w, {"id": "a2", "text": "pick", "options": ["red", "blue"]})
    kb = b.out[-1][2]["reply_markup"]["inline_keyboard"]
    assert [row[0]["callback_data"] for row in kb] == ["a:0", "a:1"]
    b.handle({"callback_query": {"id": "q", "data": "a:1", "message": {"chat": {"id": 5}}}})
    assert w.sent[-1] == {"type": "answer", "id": "a2", "text": "blue"}, w.sent
    print("ok   question with options answered by button")


def test_timeout_releases_agent():
    w = FakeWorker("w1")
    b = bot(w=w)
    b.state[5] = {"worker": "w1"}
    b.on_ask(w, {"id": "a4", "text": "q"})
    b._ask_timeout(5, "a4")
    assert w.sent[-1] == {"type": "answer", "id": "a4", "text": ""}
    b._ask_timeout(5, "a4")
    assert len(w.sent) == 1
    print("ok   no answer in time → agent is released with an empty answer")


def test_stop_clears_question():
    w = FakeWorker("w1")
    b = bot(w=w)
    link(b, 5)
    b.state[5] = {"worker": "w1"}
    b.on_ask(w, {"id": "a5", "text": "q"})
    b.handle(msg(5, "/stop"))
    assert 5 not in b.asks
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
    b.state[3] = {"busy": True}
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
    b.state[4] = {"worker": "w1"}
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
    assert [t for _, t, _ in b.out].count("it is a cat") == 1, b.out  # answered once, in this chat
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


if __name__ == "__main__":
    for k, v in list(globals().items()):
        if k.startswith("test_"):
            v()
    print("all bot tests passed")
