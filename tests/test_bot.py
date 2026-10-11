"""Telegram bot logic without Telegram: agent questions are asked in chat, answered by text or buttons, and time out.
Run: python3 tests/test_bot.py   (needs server deps: fastapi, cryptography, pydantic)"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))
import tgbot  # noqa: E402


class FakeWorker:
    def __init__(self, wid):
        self.id, self.uid, self.sent = wid, 1, []

    def send(self, cmd):
        self.sent.append(cmd)


class FakeDb:
    def execute(self, *a):
        return self

    def fetchone(self):
        return (1,)


def bot():
    b = tgbot.Bot(FakeDb(), None, None, None, None, owner=1)
    b.token, b.username = "t", "mebot"
    b.out = []
    b.send = lambda chat, text, **kw: b.out.append((chat, text, kw))
    b.call = lambda *a, **k: {}
    b.uid_for = lambda chat: 1
    return b


def test_text_answer():
    b = bot()
    w = FakeWorker("w1")
    b.state[5] = {"worker": "w1"}
    assert b.on_ask(w, {"id": "a1", "text": "which db?"})
    assert "which db?" in b.out[-1][1] and "Reply with your answer" in b.out[-1][1]
    b.handle({"message": {"chat": {"id": 5}, "text": "postgres"}})
    assert w.sent == [{"type": "answer", "id": "a1", "text": "postgres"}], w.sent
    assert 5 not in b.asks
    print("ok   question asked in chat, plain-text reply answers it")


def test_button_answer():
    b = bot()
    w = FakeWorker("w1")
    b.state[5] = {"worker": "w1"}
    b.on_ask(w, {"id": "a2", "text": "pick", "options": ["red", "blue"]})
    kb = b.out[-1][2]["reply_markup"]["inline_keyboard"]
    assert [row[0]["callback_data"] for row in kb] == ["a:0", "a:1"]
    b.handle({"callback_query": {"id": "q", "data": "a:1", "message": {"chat": {"id": 5}}}})
    assert w.sent[-1] == {"type": "answer", "id": "a2", "text": "blue"}, w.sent
    print("ok   question with options answered by button")


def test_other_chats_not_asked():
    b = bot()
    b.state[5] = {"worker": "other"}
    assert not b.on_ask(FakeWorker("w1"), {"id": "a3", "text": "x"})
    assert b.out == []
    print("ok   only chats driving this sandbox get the question")


def test_timeout_releases_agent():
    b = bot()
    w = FakeWorker("w1")
    b.state[5] = {"worker": "w1"}
    b.on_ask(w, {"id": "a4", "text": "q"})
    b._ask_timeout(5, "a4")
    assert w.sent[-1] == {"type": "answer", "id": "a4", "text": ""}
    assert "continues without it" in b.out[-1][1]
    b._ask_timeout(5, "a4")  # a second, late timer does nothing
    assert len(w.sent) == 1
    print("ok   no answer in time → agent is released with an empty answer")


def test_stop_clears_question():
    b = bot()
    b.state[5] = {"worker": "w1"}
    b.on_ask(FakeWorker("w1"), {"id": "a5", "text": "q"})
    b.handle({"message": {"chat": {"id": 5}, "text": "/stop"}})
    assert 5 not in b.asks
    print("ok   /stop clears a pending question")


if __name__ == "__main__":
    for k, v in list(globals().items()):
        if k.startswith("test_"):
            v()
    print("all bot tests passed")
