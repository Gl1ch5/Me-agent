"""Mirroring between the sandbox and Telegram, through the real server event handler (features.on_worker_event).
Run: python3 tests/test_mirror.py"""
import os, sys, tempfile
from pathlib import Path

tmp = tempfile.mkdtemp()
os.environ["ME_DB"] = f"{tmp}/t.db"
os.environ["ME_DATA"] = f"{tmp}/data"
os.environ["ME_SANDBOX"] = "0"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import features  # noqa: E402
from test_bot import FakeWorker, bot, db, link  # noqa: E402


def setup():
    conn = db()
    b = bot(conn)
    link(b, 4)
    features.bots[1] = b
    return b


def test_web_turn_is_mirrored():
    b = setup()
    w = FakeWorker("w9", uid=1)
    w.title = "dashboard"
    features.on_worker_event(w, {"type": "user", "text": "build the dashboard"})
    features.on_worker_event(w, {"type": "turn_start"})
    features.on_worker_event(w, {"type": "text", "text": "done, see index.html"})
    features.on_worker_event(w, {"type": "turn_end", "time": 1, "in": 1, "out": 1, "ctx": 1})
    texts = [t for _, t, _ in b.out]
    assert any(t.startswith("› dashboard: build the dashboard") for t in texts), texts
    assert any("done, see index.html" in t for t in texts), texts
    print("ok   a task started on the web appears in the chat, with its answer")


def test_chat_turn_not_echoed():
    b = setup()
    w = FakeWorker("w8", uid=1)
    w.tg_turn = True  # the chat started this turn; it gets its answer from the bot itself
    features.on_worker_event(w, {"type": "user", "text": "from telegram"})
    features.on_worker_event(w, {"type": "text", "text": "answer"})
    features.on_worker_event(w, {"type": "turn_end", "time": 1, "in": 1, "out": 1, "ctx": 1})
    assert b.out == [], b.out
    assert not w.tg_turn
    print("ok   a chat's own turn is not mirrored back at it")


def test_goal_result_mirrored_and_flag_kept_until_done():
    b = setup()
    w = FakeWorker("w7", uid=1)
    w.tg_turn = w.tg_goal = True
    features.on_worker_event(w, {"type": "turn_end", "time": 1, "in": 1, "out": 1, "ctx": 1})  # a goal round: keeps the flag
    assert w.tg_turn and b.out == []
    features.on_worker_event(w, {"type": "goal", "status": "done", "reason": "tests green", "text": "x"})
    assert b.out[-1][1].startswith("✓ goal done") and "tests green" in b.out[-1][1]
    assert not w.tg_turn and not w.tg_goal
    print("ok   a goal's result reaches the chat once it finishes")


def test_undo_mirrored():
    b = setup()
    w = FakeWorker("w6", uid=1)
    features.on_worker_event(w, {"type": "undone", "text": "restored 2 files: a.txt, b.txt"})
    assert "restored 2 files" in b.out[-1][1]
    print("ok   undo results are mirrored")


if __name__ == "__main__":
    for k, v in list(globals().items()):
        if k.startswith("test_"):
            v()
    print("all mirror tests passed")
