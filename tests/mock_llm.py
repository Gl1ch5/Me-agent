"""Tiny scripted OpenAI-compatible server for tests (chat/completions + responses, streaming).
Script: user text "tool:<name> <json>" → model calls that tool once, then answers "done: <tool output head>".
"""
import json, sys, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def sse(h, obj):
    h.wfile.write(b"data: " + json.dumps(obj).encode() + b"\n\n")
    h.wfile.flush()


LAST = {}  # last request body, for tests
HISTORY = []  # every request body
COUNT = {}
HITS = {}  # scenario -> requests seen


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path.endswith("/models"):
            body = json.dumps({"data": [{"id": "mock-1", "context_length": 32000}, {"id": "mock-2"}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

    def _fail(self, code, msg, headers=()):
        body = json.dumps({"error": {"message": msg}}).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        for k, v in headers:
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _special(self, req):
        """Failure scripts keyed by the first user words: flaky: (429 once), dropmid: (error chunk mid-stream once),
        overflow: (context error until history is summarized), cut: (output limit hit inside a tool call), empty: (one empty reply)."""
        msgs = req.get("messages") or []
        if msgs and str(msgs[0].get("content", ""))[:30].startswith("Summarize this coding session"):
            self._start()
            sse(self, {"choices": [{"delta": {"content": "- user wanted things; done some"}, "finish_reason": "stop"}]})
            self.wfile.write(b"data: [DONE]\n\n")
            return True
        users = [m for m in msgs if m["role"] == "user" and isinstance(m.get("content"), str)]
        first = next((m["content"] for m in users if m["content"].split(":")[0] in ("flaky", "dropmid", "overflow", "cut", "empty")), "")
        kind = first.split(":")[0]
        if not kind:
            return False
        n = COUNT[first] = COUNT.get(first, 0) + 1
        if kind == "flaky" and n == 1:
            self._fail(429, "Rate limit reached, please try again", [("Retry-After", "0")])
            return True
        if kind == "overflow" and not any("[Summary" in str(m.get("content")) for m in msgs):
            self._fail(400, "This model's maximum context length is 1000 tokens. Please reduce the length of the messages.")
            return True
        if msgs[-1]["role"] != "user":
            return False
        self._start()
        if kind == "dropmid" and n == 1:
            sse(self, {"choices": [{"delta": {"content": "partial "}}]})
            sse(self, {"error": {"message": "upstream overloaded", "code": 502}})
        elif kind == "cut" and n == 1:
            sse(self, {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "t1", "function": {"name": "write", "arguments": '{"path": "big.txt", "content": "abc'}}]},
                                    "finish_reason": "length"}]})
        elif kind == "empty" and n == 1:
            sse(self, {"choices": [{"delta": {}, "finish_reason": "stop"}]})
        else:
            sse(self, {"choices": [{"delta": {"content": f"{kind} ok"}, "finish_reason": "stop"}]})
        self.wfile.write(b"data: [DONE]\n\n")
        return True

    def _start(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()

    def do_POST(self):
        req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if not str((req.get("messages") or [{}])[0].get("content", "")).startswith("Guess the user's next message"):
            LAST.clear()  # the background next-message guess after a turn is not the request tests look at
            LAST.update(req)
            HISTORY.append(req)
        if self._special(req):
            return
        if req.get("model") == "blind-1" and "image_url" in json.dumps(req.get("messages")):  # a text-only model
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b'{"error":{"message":"No endpoints found that support image input"}}')
            return
        first = (req.get("messages") or [{}])[-1].get("content") if "messages" in req else ""
        first = first if isinstance(first, str) else ""
        if first.startswith("flaky"):  # rate-limited once, then fine
            HITS["flaky"] = HITS.get("flaky", 0) + 1
            if HITS["flaky"] == 1:
                self.send_response(429)
                self.send_header("Retry-After", "0")
                self.end_headers()
                self.wfile.write(b'{"error":{"message":"slow down"}}')
                return
        if first.startswith("streamerr"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            sse(self, {"error": {"message": "upstream overloaded", "code": 502}})
            return
        if first.startswith("silent"):  # a model that thinks for a long time without sending a byte
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.flush()
            time.sleep(8)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        if self.path.endswith("/responses"):
            return self.do_responses(req)
        msgs = req["messages"]
        last = msgs[-1]
        if last["role"] == "tool":
            for part in ["done: ", last["content"][:60].replace("\n", " ")]:
                sse(self, {"choices": [{"delta": {"content": part}}]})
        else:
            text = last["content"] if isinstance(last["content"], str) else last["content"][0]["text"]
            if text.startswith("tools:"):  # several calls in one message: tools:[["read", {...}], ["ls", {...}]]
                for i, (name, args) in enumerate(json.loads(text[6:])):
                    sse(self, {"choices": [{"delta": {"tool_calls": [{"index": i, "id": f"c{i}", "function": {"name": name, "arguments": json.dumps(args)}}]}}]})
            elif text.startswith("talk:"):  # answers a coding task with a pasted code block instead of a tool call
                sse(self, {"choices": [{"delta": {"content": "Here it is:\n```python\nclass LRU:\n    def __init__(self):\n        self.d = {}\n```"}}]})
            elif text.startswith("loop:"):  # a degenerate model repeating one sentence until the output limit
                try:
                    for i in range(400):
                        sse(self, {"choices": [{"delta": {"content": "Intro. " if i == 0 else "The b method is called when b is created. "}}]})
                except OSError:
                    return  # the client hung up on the loop
            elif "who are you" in text.lower():  # an upstream that names its vendor (Me AI's guard must hide it)
                for w in ("I am Gemini, a large", " language model built by Google."):
                    sse(self, {"choices": [{"delta": {"content": w}}]})
            elif text.startswith("GOAL:"):
                sse(self, {"choices": [{"delta": {"content": "all criteria pass\nGOAL: DONE tests green"}}]})
            elif text.startswith("tool:"):
                name, _, args = text[5:].partition(" ")
                try:  # ignore any text after the JSON (e.g. a background-job suffix)
                    args = json.dumps(json.JSONDecoder().raw_decode(args)[0])
                except ValueError:
                    pass
                sse(self, {"choices": [{"delta": {"reasoning_content": "thinking about it"}}]})
                sse(self, {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "c1", "function": {"name": name, "arguments": ""}}]}}]})
                for i in range(0, len(args), 7):
                    sse(self, {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": args[i:i + 7]}}]}}]})
            else:
                for w in ("Hello", " **world**", "!\n```py\nprint(1)\n```"):
                    time.sleep(0.02)
                    sse(self, {"choices": [{"delta": {"content": w}}]})
        sse(self, {"choices": [], "usage": {"prompt_tokens": 900 + len(json.dumps(msgs)) // 4, "completion_tokens": 20, "prompt_tokens_details": {"cached_tokens": 800}}})
        self.wfile.write(b"data: [DONE]\n\n")

    def do_responses(self, req):
        last = req["input"][-1]
        if last.get("type") == "function_call_output":
            sse(self, {"type": "response.output_text.delta", "delta": "done: " + last["output"][:40]})
        else:
            text = last["content"] if isinstance(last["content"], str) else last["content"][0]["text"]
            if text.startswith("tool:"):
                name, _, args = text[5:].partition(" ")
                sse(self, {"type": "response.output_item.done", "item": {"type": "function_call", "call_id": "r1", "name": name, "arguments": args}})
            else:
                sse(self, {"type": "response.reasoning_summary_text.delta", "delta": "hmm"})
                sse(self, {"type": "response.output_text.delta", "delta": "Hi from responses"})
        sse(self, {"type": "response.completed", "response": {"usage": {"input_tokens": 50, "output_tokens": 5}}})


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1]) if len(sys.argv) > 1 else 8901), H).serve_forever()
