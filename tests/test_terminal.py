"""Run: python3 tests/test_terminal.py   (no deps) — key parsing, Windows console records, mouse selection + copy."""
import os, sys, tempfile, threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["ME_HOME"] = tempfile.mkdtemp()
import me  # noqa: E402


def test_parse_keys_holds_unfinished_sequences():
    assert me.parse_keys("\x1b[") == ([], "\x1b[")  # wait for the rest instead of Esc + "[" typed into the input
    assert me.parse_keys("ab\x1b") == ([("char", "a"), ("char", "b")], "\x1b")
    assert me.parse_keys("\x1b[<64;10") == ([], "\x1b[<64;10")
    assert me.parse_keys("\x1b", final=True) == (["esc"], "")
    assert me.parse_keys("\x1b[A") == (["up"], "")
    assert me.parse_keys("\x1b[200~a\nb") == ([], "\x1b[200~a\nb")
    assert me.parse_keys("\x1b[200~a\nb\x1b[201~x") == ([("paste", "a\nb"), ("char", "x")], "")


def test_parse_keys_mouse():
    keys, _ = me.parse_keys("\x1b[<0;5;3M\x1b[<32;7;3M\x1b[<0;7;3m\x1b[<64;1;1M\x1b[<65;1;1M\x1b[<2;4;4M")
    assert keys == [("mouse", "press", 0, 5, 3), ("mouse", "drag", 0, 7, 3), ("mouse", "release", 0, 7, 3), "wheel-up", "wheel-down",
                    ("mouse", "press", 2, 4, 4)], keys


def K(vk, ch=0, down=1, state=0, rep=1):
    return ("k", down, rep, vk, ch, state)


def test_win_keys():
    st = {}
    # letters (incl. Cyrillic), key-up ignored, arrows by virtual key, Shift+Enter = newline, Ctrl+C
    recs = [K(0x41, ord("a")), K(0x41, ord("a"), down=0), K(0x4A, ord("ж")), K(0x26), K(0x25, state=0x08), K(0x0D, 13, state=0x10),
            K(0x0D, 13), K(0x43, 3, state=0x08), K(0x1B, 27), K(0x08, 8), K(0x08, 127, state=0x08), K(0x10)]
    assert me.win_keys(recs, st) == [("char", "a"), ("char", "ж"), "up", "ctrl-left", "newline", "enter", "ctrl-c", "esc", "backspace", "ctrl-w"]
    # emoji arrives as two surrogate records; AltGr (right alt + left ctrl) still types its character; Alt+b = word left
    assert me.win_keys([K(0, 0xD83D), K(0, 0xDE00), K(0x51, ord("@"), state=0x09), K(0x42, ord("b"), state=0x02)], st) == \
        [("char", "😀"), ("char", "@"), "ctrl-left"]
    # Alt+numpad: the character comes with the Alt key release
    assert me.win_keys([K(0x12, 0x00e9, down=0)], st) == [("char", "é")]
    # mouse: press, drag, release, double click, wheel (signed high word), right button
    m = [("m", 3, 2, 1, 0, 0), ("m", 6, 2, 1, 0, 1), ("m", 6, 2, 0, 0, 0), ("m", 6, 2, 1, 0, 2), ("m", 6, 2, 0, 0, 0),
         ("m", 1, 1, 120 << 16, 0, 4), ("m", 1, 1, (0x10000 - 120) << 16, 0, 4), ("m", 2, 2, 2, 0, 0), ("m", 2, 2, 0, 0, 1)]
    assert me.win_keys(m, st) == [("mouse", "press", 0, 3, 2), ("mouse", "drag", 0, 6, 2), ("mouse", "release", 0, 6, 2),
                                  ("mouse", "press", 0, 6, 2), ("mouse", "release", 0, 6, 2), "wheel-up", "wheel-down", ("mouse", "press", 2, 2, 2)]


def test_win_paste_burst():
    recs = [K(0x41, ord(c)) for c in "ab"] + [K(0x0D, 13)] + [K(0x43, ord("c"))]
    assert me.win_paste_burst(me.win_keys(recs, {})) == [("paste", "ab\nc")]
    # a human Enter at the end is a submit
    assert me.win_paste_burst(me.win_keys([K(0x41, ord("a")), K(0x0D, 13)], {})) == [("char", "a"), "enter"]


def test_render_erases_instead_of_padding():
    L = me.Line().add("hi")
    r = L.render(10, "")
    assert "hi" in r and "\x1b[K" in r and "        " not in r
    full = me.Line().add("x" * 10).render(10, "")
    assert "\x1b[K" not in full  # EL at the last column would wipe the character just written
    hl = me.Line().add("abcdef").render(10, "", (1, 3))
    assert "a\x1b[7mbc\x1b[27mdef" in hl, repr(hl)


def test_selection_copies_logical_text():
    text = "Первая строка очень длинного абзаца, который не помещается в узкий терминал и переносится."
    lines = me.wrap_segs([(text, "")], 30, indent=2)
    assert len(lines) > 2 and all(l.pre == 2 for l in lines) and all(l.soft is not None for l in lines[1:])
    assert me.selection_text(lines, (0, 0), (len(lines) - 1, 999)) == text
    # code block: gutter and padding are not copied, the indentation inside the code is
    md = me.render_md("Look:\n```py\ndef f():\n    return 1   \n```\n- one\n  - two", 40)
    got = me.selection_text(md, (0, 0), (len(md) - 1, 999))
    assert got == "Look:\n\ndef f():\n    return 1\n\n• one\n  • two", repr(got)
    # partial selection inside a line
    one = me.wrap_segs([("hello brave world", "")], 40, indent=2)
    assert me.selection_text(one, (0, 8), (0, 12)) == "brave"
    assert me.word_at(one[0], 9) == (8, 12)


def fake_tui():
    store = me.Store(tempfile.mkdtemp())
    t = me.TUI(store)
    out = []
    t.term.write = out.append
    t.term.size = lambda: (60, 20)
    return t, out


def test_tui_drag_copies_and_click_toggles():
    t, out = fake_tui()
    copied = []
    me.clip_copy = lambda text, osc=None: copied.append(text) or "native"
    t.add("assistant", text="The answer is **forty two**, see below.\n```sh\necho hi\n```").done = True
    tool = t.add("tool", name="bash", args={}, label="ls -la")
    tool.done, tool.ok, tool.collapsed = True, True, True
    tool.data["output"] = "a\nb\nc\nd\ne"
    t.draw()
    lines, owners, top, avail, _ = t._tx
    row = lambda i: i - top + 1  # screen row of transcript line i
    first = next(i for i, l in enumerate(lines) if "answer" in l.copy_text())
    code = next(i for i, l in enumerate(lines) if l.copy_text() == "echo hi")
    with t.lock:
        t.handle_key(("mouse", "press", 0, 1, row(first)))
        t.handle_key(("mouse", "drag", 0, 50, row(code)))
        t.handle_key(("mouse", "release", 0, 50, row(code)))
    assert copied == ["The answer is forty two, see below.\n\necho hi"], copied
    t.draw()
    assert any("\x1b[7m" in s for s in out), "selection is highlighted"
    t.handle_key(("char", "x"))
    assert t.sel is None and t.input == "x"
    # a plain click on the tool card opens it
    t.draw()
    lines, owners, top, avail, _ = t._tx
    ti = owners.index(tool)
    t.handle_key(("mouse", "press", 0, 5, ti - top + 1))
    t.handle_key(("mouse", "release", 0, 5, ti - top + 1))
    assert tool.collapsed is False
    # double click copies a word
    copied.clear()
    t._click = None
    t.handle_key(("mouse", "press", 0, 8, row(first)))
    t.handle_key(("mouse", "release", 0, 8, row(first)))
    t.handle_key(("mouse", "press", 0, 8, row(first)))
    assert copied == ["answer"], copied


def test_copy_command():
    t, out = fake_tui()
    copied = []
    me.clip_copy = lambda text, osc=None: copied.append(text) or "native"
    t.agent.session["messages"] = [{"role": "assistant", "content": "first"},
                                   {"role": "assistant", "content": "use:\n```py\nprint(1)\n```\ndone"}]
    t.toast = lambda *a, **k: None
    t.cmd_copy("last")  # (no argument opens a picker when the answer has code blocks)
    t.cmd_copy("code")
    t.cmd_copy("2")
    t.cmd_copy("all")
    assert copied[:3] == ["use:\n```py\nprint(1)\n```\ndone", "print(1)", "first"], copied
    assert copied[3].startswith("first\n\nuse:"), copied[3]


def test_modal_ignores_mouse():
    t, out = fake_tui()
    m = {"kind": "pick", "title": "x", "items": [("a", "a", ""), ("b", "b", "")], "sel": 0, "filter": "", "hint": "", "ev": threading.Event()}
    t.modal = m
    t.handle_key(("mouse", "press", 0, 1, 1))
    t.handle_key("wheel-down")
    assert m["filter"] == "" and m["sel"] == 1


def test_new_session_clears_screen():
    t, out = fake_tui()
    t.add("user", text="old question")
    t.add("assistant", text="old answer").done = True
    t.agent.session["messages"] = [{"role": "user", "content": "old question"}]
    old = t.agent.session["id"]
    t.cmd_new("")
    assert t.agent.session["id"] != old and t.agent.session["messages"] == []
    assert [b.kind for b in t.blocks] == ["welcome"], [b.kind for b in t.blocks]
    t.draw()
    assert not any("old" in s for s in out[-1:]), "nothing of the old chat is painted"


def test_big_paste_folds_and_never_crashes():
    t, out = fake_tui()
    sent = []
    t.dispatch = lambda text, shown=None: sent.append((text, shown))
    big = "\n".join(f"line {i} 😀" for i in range(400))
    t.handle_key(("paste", "before " ))
    t.handle_key(("paste", big))
    assert t.input == "before [paste #1 · 400 lines]", t.input
    t.draw()
    t.handle_key("enter")
    assert sent == [("before " + big, "before [paste #1 · 400 lines]")], sent[0][1]
    # lone surrogate halves (the Windows console splits emoji) and control characters never reach the screen or the history
    t.handle_key(("char", "\ud83d"))
    t.handle_key(("paste", "a\x1b[31mb\x00c\tz"))
    assert t.input == "a[31mbc\tz", repr(t.input)
    t.draw()
    t.handle_key("enter")
    # a bug in a key handler is logged, not fatal
    t.handle_key = lambda k: 1 / 0
    t.safe_key("x")
    assert "ZeroDivisionError" in t.notice[0]


def test_long_single_line_burst_is_a_paste():
    keys = [("char", "x")] * 100
    assert me.win_paste_burst(keys) == [("paste", "x" * 100)]
    assert me.win_paste_burst([("char", "x")] * 5) == [("char", "x")] * 5


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
