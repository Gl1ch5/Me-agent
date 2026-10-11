"""Tiny scripted OpenAI-compatible server for tests (chat/completions + responses, streaming).
Script: user text "tool:<name> <json>" → model calls that tool once, then answers "done: <tool output head>".
"""
import json, sys, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def sse(h, obj):
    h.wfile.write(b"data: " + json.dumps(obj).encode() + b"\n\n")
    h.wfile.flush()


LAST = {}  # last request body, for tests


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

    def do_POST(self):
        req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        LAST.clear()
        LAST.update(req)
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
