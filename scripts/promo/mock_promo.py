"""Promo-only mock LLM for the recorder: natural prompts → fixed tool calls, and a fake Groq transcriber.
Reuses tests/mock_llm.py for the wire format. Nothing here ships in me.py."""
import io, json, os, re, sys, time
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "scripts" / "promo"))
import mock_llm  # noqa: E402

# PROMO_SPEED > 1 is the quick-preview pace: the mock floods the words out, and the TUI's render loop paces the picture.
# The turn is identical, the frames it produces are the same, and the scene is over sooner. 1 (default) is the real pace.
SPEED = float(os.environ.get("PROMO_SPEED") or 1.0)

from fixtures import DESIGN_MD, LANDING, LANDING_DARK_THEME, LANDING_EN, LANDING_EN_DARK  # noqa: E402

VOICE_TEXT = "напиши тест для парсера"  # what the fake transcriber "hears" (set per language by the caller)
TYPING = None  # optional path: every text delta is also logged as a cue time (t, "word"|"done") for the promo's audio
SAYING = {  # the short lines the model streams while it works, per language: word by word, and the audio cues
    "ru": dict(verify="Проверил: файл на месте, ошибок нет ✓"),
    "en": dict(verify="Checked: the file is there, no errors ✓"),
}
LANG = [""]  # set by the caller: which SAYING row to use
T0 = [0.0]
FRAME0 = [0.0]  # wall-clock time of the scene's frame 0: the cue times are written relative to it
ON = [True]  # off in scenes that need no cues (the web scenes' browser talks to the same mock)


def cue(kind):
    """Append a sound cue to the typing log: the video mixer turns it into a key click or a chime at that moment."""
    if TYPING and T0[0] and ON[0]:
        with open(TYPING, "a", encoding="utf-8") as f:
            f.write(f"{time.time() - (FRAME0[0] or T0[0]):.3f}\t{kind}\n")


SCRIPT = {  # exact natural prompt → (tool, args): the model 'decides' to call that tool once
    "сделай лендинг для кофейни": ("write", {"path": "index.html", "content": LANDING}),
    "напиши тест для парсера": ("write", {"path": "test_parser.py", "content": "def test_parse():\n    assert parse('1') == 1\n"}),
    "опиши дизайн кофейни": ("write", {"path": "DESIGN.md", "content": DESIGN_MD}),
    "сделай страницу по DESIGN.md": ("write", {"path": "index.html", "content": LANDING}),
    "сделай дизайн и страницу кофейни": ("tools", [["write", {"path": "DESIGN.md", "content": DESIGN_MD}],   # one turn, two calls:
                                                  ["write", {"path": "index.html", "content": LANDING}]]),  # DESIGN.md first
    "добавь тёмную тему": ("write", {"path": "index.html", "content": LANDING_DARK_THEME}),
    "make a landing page for a coffee shop": ("write", {"path": "index.html", "content": LANDING_EN}),
    "design and build a coffee shop page": ("tools", [["write", {"path": "DESIGN.md", "content": DESIGN_MD}],
                                                      ["write", {"path": "index.html", "content": LANDING_EN}]]),
    "add a dark theme": ("write", {"path": "index.html", "content": LANDING_EN_DARK}),
    "write a test for the parser": ("write", {"path": "test_parser.py", "content": "def test_parse():\n    assert parse('1') == 1\n"}),
}


class H(mock_llm.H):
    def say(self, text):
        """A reply typed out word by word."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for j, w in enumerate(re.split(r"(\s+)", text)):  # word by word, fast: the TUI shows a reply being written
            if w.strip():
                mock_llm.sse(self, {"choices": [{"delta": {"content": w}}]})
                cue("word")
                time.sleep(0.035 if SPEED <= 1 else 0)  # a preview floods the words out; the TUI's render loop paces them
            elif w:
                mock_llm.sse(self, {"choices": [{"delta": {"content": w}}]})
        mock_llm.sse(self, {"choices": [], "usage": {"prompt_tokens": 900, "completion_tokens": 8}})
        self.wfile.write(b"data: [DONE]\n\n")

    def do_POST(self):
        raw = self.rfile.read(int(self.headers["Content-Length"]))
        if "audio/transcriptions" in self.path:  # Groq Whisper stand-in
            out = json.dumps({"text": VOICE_TEXT}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)
            return
        body = json.loads(raw)
        msgs = body.get("messages") or []
        if msgs and msgs[-1].get("role") == "user":
            c = msgs[-1]["content"]
            text = (c if isinstance(c, str) else " ".join(p.get("text", "") for p in c if p.get("type") == "text")).strip()
            if text in SCRIPT:
                name, args = SCRIPT[text]
                if name == "tools":  # several calls in one message (the mock's tools: form)
                    msgs[-1]["content"] = "tools:" + json.dumps(args, ensure_ascii=False)
                else:
                    msgs[-1]["content"] = "tool:" + name + " " + json.dumps(args, ensure_ascii=False)
            elif any(m.get("role") == "tool" for m in msgs):  # the agent's own self-check after a change
                return self.say("\n" + SAYING[LANG[0] or "ru"]["verify"])
        new = json.dumps(body, ensure_ascii=False).encode()
        self.rfile = io.BytesIO(new)
        self.headers.replace_header("Content-Length", str(len(new)))
        super().do_POST()


def serve(port=0):
    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    import threading
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv
