"""Promo-only mock LLM for the recorder: natural prompts → fixed tool calls, and a fake Groq transcriber.
Reuses tests/mock_llm.py for the wire format. Nothing here ships in me.py."""
import io, json, sys
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "scripts" / "promo"))
import mock_llm  # noqa: E402
from fixtures import DESIGN_MD, LANDING, LANDING_DARK_THEME  # noqa: E402

VOICE_TEXT = "напиши тест для парсера"  # what the fake transcriber "hears"
SCRIPT = {  # exact natural prompt → (tool, args): the model 'decides' to call that tool once
    "сделай лендинг для кофейни": ("write", {"path": "index.html", "content": LANDING}),
    "опиши дизайн кофейни": ("write", {"path": "DESIGN.md", "content": DESIGN_MD}),
    "сделай страницу по DESIGN.md": ("write", {"path": "index.html", "content": LANDING}),
    "сделай дизайн и страницу кофейни": ("tools", [["write", {"path": "DESIGN.md", "content": DESIGN_MD}],   # one turn, two calls:
                                                  ["write", {"path": "index.html", "content": LANDING}]]),  # DESIGN.md first
    "добавь тёмную тему": ("write", {"path": "index.html", "content": LANDING_DARK_THEME}),
}


class H(mock_llm.H):
    def say(self, text):
        out = []
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        mock_llm.sse(self, {"choices": [{"delta": {"content": text}}]})
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
                return self.say("\nпроверено: файл собран, ошибок нет ✓")
        new = json.dumps(body, ensure_ascii=False).encode()
        self.rfile = io.BytesIO(new)
        self.headers.replace_header("Content-Length", str(len(new)))
        super().do_POST()


def serve(port=0):
    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    import threading
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv
