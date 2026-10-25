"""Drive `me.py --rpc` for the promo and log keystrokes, prompts and the moments the reply streams in.

The TUI scenes draw a real pty, where only the bytes are known; the web scenes are a browser video. In both cases the
sound design needs the same three things: when a key went down, when the model's words appeared and when the turn ended.
Rpc gets them exactly (it talks to the same agent the TUI runs), so a scene's audio never drifts from its picture:
the real typing of the scene is imitated here at the same rhythm (`cps`), and the reply is timed from the mock's own
word cues. Left running in the background while the picture is recorded, it costs nothing.
"""
import json, subprocess, sys, threading, time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]


class Rpc:
    def __init__(self, cwd, env, timeout=30):
        self.t0 = time.time()
        self.ended = threading.Event()
        self.proc = subprocess.Popen([sys.executable, str(ROOT / "me.py"), "--rpc"], cwd=str(cwd), env=dict(env, ME_TERM="dumb"),
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                     encoding="utf-8", bufsize=1)
        self.ready = threading.Event()
        threading.Thread(target=self._read, daemon=True).start()
        assert self.ready.wait(timeout), "promo rpc: the agent never said ready"

    def _read(self):
        for line in self.proc.stdout:
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if e.get("type") == "ready":
                self.ready.set()
            elif e.get("type") == "turn_end":
                self.ended.set()

    def now(self):
        return time.time() - self.t0

    def keys(self, text, cps=9, enter=True):
        """Mimic typing `text` at `cps`: returns the key moments and, with `enter`, sends the prompt and returns its moment too."""
        out = []
        for _ in range(len(text)):
            out.append(self.now())
            time.sleep(1.0 / cps)
        if enter:
            self.proc.stdin.write(json.dumps({"type": "prompt", "text": text}, ensure_ascii=False) + "\n")
            self.proc.stdin.flush()
            out.append(self.now())
        return out

    def close(self):
        try:
            self.proc.kill()
            self.proc.wait(10)
        except OSError:
            pass
