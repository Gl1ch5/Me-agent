"""End-to-end TUI test: me.py runs in a real pseudo-terminal, a pyte screen emulates the terminal, keys/mouse/paste are sent as bytes.
Run: python3 tests/test_tui.py   (Linux/macOS; needs `pip install pyte`)"""
import json, os, pty, select, signal, struct, sys, tempfile, threading, time, fcntl, termios
from http.server import ThreadingHTTPServer
from pathlib import Path

import pyte

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))
import mock_llm  # noqa: E402

COLS, ROWS = 110, 32


class TUI:
    def __init__(self, home, cwd):
        self.screen = pyte.Screen(COLS, ROWS)
        self.stream = pyte.ByteStream(self.screen)
        self.raw = b""
        pid, fd = pty.fork()
        if pid == 0:
            os.chdir(cwd)
            os.environ.update(ME_HOME=home, TERM="xterm-256color", COLUMNS=str(COLS), LINES=str(ROWS), ME_LANG=os.environ.get("ME_LANG", "en"))
            os.environ.pop("DISPLAY", None)
            os.environ.pop("WAYLAND_DISPLAY", None)
            os.execv(sys.executable, [sys.executable, str(ROOT / "me.py")])
        self.pid, self.fd = pid, fd
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", ROWS, COLS, 0, 0))
        self.lock = threading.Lock()
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self):
        while True:
            try:
                r, _, _ = select.select([self.fd], [], [], 0.1)
                if r:
                    data = os.read(self.fd, 65536)
                    if not data:
                        return
                    with self.lock:
                        self.raw += data
                        self.stream.feed(data)
            except OSError:
                return

    def text(self):
        with self.lock:
            return "\n".join(self.screen.display)

    def send(self, b):
        os.write(self.fd, b if isinstance(b, bytes) else b.encode())

    def wait(self, needle, timeout=10):
        end = time.time() + timeout
        while time.time() < end:
            if needle in self.text():
                return True
            time.sleep(0.05)
        raise AssertionError(f"{needle!r} not on screen:\n{self.text()}")

    def find(self, needle):
        for y, line in enumerate(self.text().split("\n")):
            x = line.find(needle)
            if x >= 0:
                return x, y
        return None

    def close(self):
        try:
            os.kill(self.pid, signal.SIGKILL)
            os.waitpid(self.pid, 0)
        except OSError:
            pass


def main():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), mock_llm.H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    home, cwd = tempfile.mkdtemp(), tempfile.mkdtemp()
    cfg = {"providers": {"mock": {"base_url": f"http://127.0.0.1:{srv.server_port}/v1", "api_key": "x"}},
           "models": [{"name": "m", "id": "mock-1", "provider": "mock", "api": "chat", "context": 32000, "vision": True}],
           "model": "m", "settings": {"yolo": True, "mouse": True}}
    Path(home, "config.json").write_text(json.dumps(cfg))
    Path(home, "auth.json").write_text(json.dumps({"skip": True}))
    img = Path(cwd, "shot.png")
    img.write_bytes(bytes.fromhex("89504e470d0a1a0a0000000d4948445200000001000000010806000000"
                                  "1f15c4890000000d4944415478da63f8ffff3f0005fe02fea7d6a4c40000000049454e44ae426082"))
    t = TUI(home, cwd)
    try:
        t.wait("me v")
        t.wait("alt+v")
        print("ok   starts without waiting for the network, shows the key hints")

        t.send("hi\r")
        t.wait("Hello world")
        t.wait("1 step")
        print("ok   a turn streams the answer")

        x, y = t.find("Hello world")
        t.send(f"\x1b[<0;{x + 1};{y + 1}M\x1b[<32;{x + 6};{y + 1}M\x1b[<32;{x + 11};{y + 1}M\x1b[<0;{x + 11};{y + 1}m")
        t.wait("copied selection")
        if sys.platform != "darwin":  # macOS has a real clipboard (pbcopy) here, so no terminal fallback is needed
            assert b"\x1b]52;c;" + __import__("base64").b64encode(b"Hello world") in t.raw, "selection must reach the clipboard (OSC 52)"
        print("ok   drag-select copies the text")

        t.send("\x1b[200~" + str(img) + "\x1b[201~")
        t.wait("[image #1]")
        t.wait("image attached")
        t.send("what is it\r")
        t.wait("[image #1] what is it")
        t.wait("Hello world", 5)
        time.sleep(0.6)
        assert '"image_url"' in json.dumps(mock_llm.LAST), "the dragged image must be sent to the model"
        print("ok   dragging an image file in attaches it and sends it to the model")

        t.send("line one\rline two")  # a paste in a terminal without bracketed paste (classic cmd.exe)
        t.wait("line two")
        time.sleep(0.4)
        assert "line one" in t.text() and t.text().count("line one") == 1
        t.send("\x15\x1bOA")  # ctrl+u clears the 2nd line; the 1st line is still in the input, not sent
        print("ok   Enter inside a raw paste is a line break, not send")

        t.send("\x03")  # ctrl+c clears the input
        t.send("/copy last\r")
        t.wait("copied last answer")
        print("ok   /copy")

        if sys.platform != "darwin":  # macOS has a real clipboard here: ctrl+v pastes its text, which is right
            t.send("\x16")  # ctrl+v with no clipboard tools → a clear message, no crash
            t.wait("clipboard is empty")
            print("ok   ctrl+v without a clipboard image explains why")

        for _ in range(40):
            t.send("\x1b[<64;5;5M")  # wheel up
        t.send("\x1b[<65;5;5M" * 40)
        t.wait("› ")
        assert "render error" not in t.text()
        print("ok   wheel scrolling, no render errors")
        t.send("\x03\x03")
        time.sleep(0.5)
    finally:
        t.close()
    print("all TUI tests passed")


if __name__ == "__main__":
    main()
