# ───────────────────────────── provider ─────────────────────────────


class Cancelled(Exception):
    pass


class Provider:
    """OpenAI-compatible streaming client. Messages use chat/completions shape internally."""

    def __init__(self, model, prov):
        self.m, self.p = model, prov
        self.base = (prov.get("base_url") or "https://api.openai.com/v1").rstrip("/")

    def _post(self, path, body):
        r = urllib.request.Request(self.base + path, data=json.dumps(body).encode(), method="POST")
        r.add_header("Content-Type", "application/json")
        r.add_header("Accept", "text/event-stream")
        if self.p.get("api_key"):
            r.add_header("Authorization", "Bearer " + self.p["api_key"])
        try:
            return urllib.request.urlopen(r, timeout=600)
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:800]}")

    @staticmethod
    def _sse(resp, cancel):
        buf = b""
        while True:
            if cancel.is_set():
                resp.close()
                raise Cancelled()
            chunk = resp.readline()
            if not chunk:
                break
            line = chunk.strip()
            if line.startswith(b"data:"):
                data = line[5:].strip()
                if data == b"[DONE]":
                    break
                buf = data
                try:
                    yield json.loads(buf)
                except ValueError:
                    pass

    def cache_mode(self):
        """How to ask for prompt caching: 'anthropic' (cache_control marker), 'openai' (prompt_cache_key), or ''. model.cache overrides."""
        c = self.m.get("cache")
        if c in ("anthropic", "openai", "off"):
            return "" if c == "off" else c
        mid = str(self.m.get("id", "")).lower()
        if "claude" in mid or "anthropic" in self.base:
            return "anthropic"
        if "api.openai.com" in self.base:
            return "openai"
        return ""  # DeepSeek, Gemini, Groq… cache a stable prefix automatically

    @staticmethod
    def _cached(u):
        d = u.get("prompt_tokens_details") or u.get("input_tokens_details") or {}
        return int(d.get("cached_tokens") or u.get("cache_read_input_tokens") or u.get("prompt_cache_hit_tokens") or 0)

    def stream(self, system, messages, tools, emit, cancel, cache_key=None):
        """Returns (assistant_message, usage). Emits text/reasoning deltas. The system prompt + tools form a stable prefix."""
        if self.m.get("api") == "responses":
            return self._responses(system, messages, tools, emit, cancel, cache_key)
        cm = self.cache_mode()
        sysmsg = {"role": "system", "content": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}] if cm == "anthropic" else system}
        body = {"model": self.m["id"], "stream": True, "stream_options": {"include_usage": True},
                "messages": [sysmsg] + messages}
        if cm == "openai" and cache_key:
            body["prompt_cache_key"] = cache_key
        if tools:
            body["tools"] = [{"type": "function", "function": t} for t in tools]
        if self.m.get("effort") and self.m["effort"] != "none":
            body["reasoning_effort"] = self.m["effort"]
        if self.m.get("max_out"):
            body["max_completion_tokens" if self.m.get("effort") else "max_tokens"] = int(self.m["max_out"])
        if self.m.get("temperature") not in (None, ""):
            body["temperature"] = float(self.m["temperature"])
        text, calls, usage = "", {}, {}
        resp = self._post("/chat/completions", body)
        for ev in self._sse(resp, cancel):
            if ev.get("usage"):
                usage = ev["usage"]
            for ch in ev.get("choices") or []:
                d = ch.get("delta") or {}
                r = d.get("reasoning_content") or d.get("reasoning")
                if isinstance(r, str) and r:
                    emit({"type": "reasoning", "text": r})
                if d.get("content"):
                    text += d["content"]
                    emit({"type": "text", "text": d["content"]})
                for tc in d.get("tool_calls") or []:
                    c = calls.setdefault(tc.get("index", 0), {"id": "", "name": "", "args": ""})
                    c["id"] = tc.get("id") or c["id"]
                    f = tc.get("function") or {}
                    c["name"] += f.get("name") or ""
                    c["args"] += f.get("arguments") or ""
        msg = {"role": "assistant", "content": text or None}
        if calls:
            msg["tool_calls"] = [{"id": c["id"] or "call_" + uuid.uuid4().hex[:8], "type": "function",
                                  "function": {"name": c["name"], "arguments": c["args"] or "{}"}} for _, c in sorted(calls.items())]
        return msg, {"in": usage.get("prompt_tokens", 0), "out": usage.get("completion_tokens", 0), "cached": self._cached(usage)}

    def _responses(self, system, messages, tools, emit, cancel, cache_key=None):
        items = []
        for m in messages:
            if m["role"] == "tool":
                items.append({"type": "function_call_output", "call_id": m["tool_call_id"], "output": m["content"]})
            elif m["role"] == "assistant":
                if m.get("content"):
                    items.append({"role": "assistant", "content": m["content"]})
                for tc in m.get("tool_calls") or []:
                    items.append({"type": "function_call", "call_id": tc["id"], "name": tc["function"]["name"],
                                  "arguments": tc["function"]["arguments"]})
            else:
                c = m["content"]
                if isinstance(c, list):
                    c = [{"type": "input_text", "text": x["text"]} if x["type"] == "text" else
                         {"type": "input_image", "image_url": x["image_url"]["url"]} for x in c]
                items.append({"role": "user", "content": c})
        body = {"model": self.m["id"], "stream": True, "instructions": system, "input": items, "store": False}
        if self.cache_mode() == "openai" and cache_key:
            body["prompt_cache_key"] = cache_key
        if tools:
            body["tools"] = [dict(type="function", **t) for t in tools]
        if self.m.get("effort") and self.m["effort"] != "none":
            body["reasoning"] = {"effort": self.m["effort"], "summary": "auto"}
        if self.m.get("max_out"):
            body["max_output_tokens"] = int(self.m["max_out"])
        if self.m.get("temperature") not in (None, ""):
            body["temperature"] = float(self.m["temperature"])
        text, calls, usage = "", {}, {}
        for ev in self._sse(self._post("/responses", body), cancel):
            t = ev.get("type", "")
            if t == "response.output_text.delta":
                text += ev["delta"]
                emit({"type": "text", "text": ev["delta"]})
            elif t in ("response.reasoning_summary_text.delta", "response.reasoning_text.delta"):
                emit({"type": "reasoning", "text": ev["delta"]})
            elif t == "response.output_item.done" and ev.get("item", {}).get("type") == "function_call":
                it = ev["item"]
                calls[it["call_id"]] = {"id": it["call_id"], "name": it["name"], "args": it.get("arguments", "")}
            elif t == "response.completed":
                usage = ev.get("response", {}).get("usage") or {}
            elif t in ("response.failed", "error"):
                raise RuntimeError(json.dumps(ev.get("response", {}).get("error") or ev)[:800])
        msg = {"role": "assistant", "content": text or None}
        if calls:
            msg["tool_calls"] = [{"id": c["id"], "type": "function", "function": {"name": c["name"], "arguments": c["args"] or "{}"}}
                                 for c in calls.values()]
        return msg, {"in": usage.get("input_tokens", 0), "out": usage.get("output_tokens", 0), "cached": self._cached(usage)}


def list_models(prov):
    base = (prov.get("base_url") or "https://api.openai.com/v1").rstrip("/")
    r = urllib.request.Request(base + "/models")
    if prov.get("api_key"):
        r.add_header("Authorization", "Bearer " + prov["api_key"])
    with urllib.request.urlopen(r, timeout=30) as resp:
        data = json.loads(resp.read())
    out = []
    for m in data.get("data", data if isinstance(data, list) else []):
        ctx = m.get("context_length") or m.get("context_window") or (m.get("top_provider") or {}).get("context_length")
        out.append({"id": m["id"], "context": ctx})
    return sorted(out, key=lambda x: x["id"])
