# ───────────────────────────── provider ─────────────────────────────


class Cancelled(Exception):
    pass


def no_images(messages, note="[image: this model cannot see images]"):
    """Messages with every image part replaced by a text note (copies; the originals stay)."""
    return [dict(m, content=[x if x.get("type") != "image_url" else {"type": "text", "text": note} for x in m["content"]])
            if isinstance(m.get("content"), list) else m for m in messages]


class ProviderError(RuntimeError):
    """An API failure. `retryable`: worth another try (rate limit, overload, 5xx, dropped stream). `overflow`: the prompt
    no longer fits the model's context."""

    def __init__(self, msg, status=None, retry_after=None, retryable=False):
        super().__init__(msg)
        self.status, self.retry_after, self.retryable = status, retry_after, retryable
        self.overflow = bool(OVERFLOW_RX.search(msg)) and status in (None, 400, 413, 422)


RETRY_STATUS = {408, 409, 425, 429, 500, 502, 503, 504, 520, 521, 522, 523, 524, 529}
RETRY_RX = re.compile(r"overload|rate.?limit|try again|temporar|capacity|unavailable|timeout|timed out", re.I)
OVERFLOW_RX = re.compile(r"context.{0,24}(length|window|limit|size)|maximum context|too many (input )?tokens|prompt is too long|"
                         r"reduce the length|input is too long|exceeds? the (model|context|maximum)|context_length_exceeded", re.I)
IMAGE_RX = re.compile(r"image|vision|multi.?modal|image_url|content.{0,20}(type|part)", re.I)
RETRIES, RETRY_BASE = 4, 2.0  # waits 2, 4, 8, 16 s (or what the server's Retry-After asks, up to a minute)
NET_ERRORS = (urllib.error.URLError, ConnectionError, TimeoutError, OSError, http.client.HTTPException)


def _retry_after(headers):
    v = (headers or {}).get("Retry-After") or (headers or {}).get("retry-after")
    try:
        return min(60.0, max(0.0, float(v)))
    except (TypeError, ValueError):
        return None


class Provider:
    """OpenAI-compatible streaming client. Messages use chat/completions shape internally.
    Assistant messages keep what the provider needs back on the next step of the same turn (reasoning_content,
    reasoning_details, Gemini's thought signatures in tool_calls[].extra_content, Responses reasoning items), tagged with
    `_model` so it only ever goes back to the model that produced it."""

    def __init__(self, model, prov):
        self.m, self.p = model, prov
        self.base = (prov.get("base_url") or "https://api.openai.com/v1").rstrip("/")

    def _post(self, path, body):
        r = urllib.request.Request(self.base + path, data=json.dumps(body).encode(), method="POST")
        r.add_header("Content-Type", "application/json")
        r.add_header("Accept", "text/event-stream")
        r.add_header("User-Agent", f"me/{VERSION}")
        for k, v in (self.p.get("headers") or {}).items():  # e.g. OpenRouter's HTTP-Referer, a proxy's own auth header
            r.add_header(k, str(v))
        if self.p.get("api_key"):
            r.add_header("Authorization", "Bearer " + self.p["api_key"])
        try:
            return urllib.request.urlopen(r, timeout=600)
        except urllib.error.HTTPError as e:
            text = e.read().decode("utf-8", "replace")[:1500]
            try:  # the readable part of {"error": {"message": …}}
                j = json.loads(text)
                err = j.get("error") if isinstance(j, dict) else None
                text = (err.get("message") if isinstance(err, dict) else err) or j.get("message") or text
            except (ValueError, AttributeError):
                pass
            text = str(text)[:800]
            if e.code in (400, 404) and "model" not in text.lower() and body.get("model"):  # a proxy that drops the reason: the id is the usual suspect
                text += f" (if this repeats, check the model id {body['model']!r} in /models or pick another model)"
            raise ProviderError(f"HTTP {e.code}: {text}", e.code, _retry_after(e.headers),
                                e.code in RETRY_STATUS or bool(RETRY_RX.search(str(text)) and e.code >= 500))

    @staticmethod
    def _sse(resp, cancel):
        """Parsed SSE events. Lines are read by a helper thread, so Esc stops at once on every OS even while a model thinks
        silently for minutes (a blocked readline() cannot be interrupted reliably on Windows)."""
        ctype = resp.headers.get("Content-Type", "") if getattr(resp, "headers", None) else ""
        if "json" in ctype and "event-stream" not in ctype:  # a server that ignored stream=true: one plain JSON reply
            try:
                yield json.loads(resp.read())
            finally:
                resp.close()
            return
        q = queue.Queue(maxsize=1000)

        def reader():
            try:
                for chunk in iter(resp.readline, b""):
                    q.put(chunk)
                q.put(None)
            except Exception as e:
                q.put(e)
        threading.Thread(target=reader, daemon=True, name="me-sse").start()
        try:
            while True:
                try:
                    chunk = q.get(timeout=0.1)
                except queue.Empty:
                    if cancel.is_set():
                        raise Cancelled()
                    continue
                if cancel.is_set():
                    raise Cancelled()
                if chunk is None:
                    break
                if isinstance(chunk, Exception):
                    raise chunk
                line = chunk.strip()
                if line.startswith(b"data:"):
                    data = line[5:].strip()
                    if data == b"[DONE]":
                        break
                    try:
                        yield json.loads(data)
                    except ValueError:
                        pass
                elif line.startswith(b"{") and b'"error"' in line:  # a plain JSON error body instead of a stream
                    try:
                        err = json.loads(line).get("error")
                    except ValueError:
                        err = line.decode("utf-8", "replace")
                    msg = str(err.get("message") if isinstance(err, dict) else err)[:800]
                    raise ProviderError(msg, None, None, bool(RETRY_RX.search(msg)))
        finally:
            threading.Thread(target=resp.close, daemon=True).start()  # closing may block on some platforms: never in the UI path

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
        """Returns (assistant_message, usage). Emits text/reasoning deltas. The system prompt + tools form a stable prefix.
        Rate limits, overloads, 5xx and dropped connections are retried with backoff; a retry after partial output emits
        {"type": "retry", "discard": True} so the frontend drops what it showed of the failed attempt."""
        attempt = 0
        while True:
            shown = [False]

            def em(e):
                shown[0] = True
                emit(e)
            try:
                if self.m.get("api") == "responses":
                    return self._responses(system, messages, tools, em, cancel, cache_key)
                return self._chat(system, messages, tools, em, cancel, cache_key)
            except Cancelled:
                raise
            except ProviderError as e:
                if not e.retryable or attempt >= RETRIES:
                    raise
                err, delay = e, e.retry_after
            except NET_ERRORS as e:
                if cancel.is_set():
                    raise Cancelled()
                if attempt >= RETRIES:
                    raise ProviderError(f"network error: {e}")
                err, delay = e, None
            attempt += 1
            delay = delay if delay is not None else min(30, RETRY_BASE * 2 ** (attempt - 1))
            emit({"type": "retry", "attempt": attempt, "of": RETRIES, "delay": delay, "error": str(err)[:300], "discard": shown[0]})
            if cancel.wait(delay):
                raise Cancelled()

    def blind(self):
        """vision off in /models: a model without vision rejects the whole request over one image."""
        return str(self.m.get("vision", True)).lower() == "false"

    def wire(self, messages):
        """Session messages → request messages: internal keys dropped; reasoning replayed only to the model that wrote it,
        and only for the current turn (after the last real user message) — what DeepSeek, Kimi and Qwen expect."""
        mid = self.m.get("id")
        if self.blind():
            messages = no_images(messages)
        last = max((i for i, m in enumerate(messages) if m.get("role") == "user" and not m.get("_aux")), default=-1)
        out = []
        for i, m in enumerate(messages):
            mine = m.get("role") == "assistant" and m.get("_model") == mid
            w = {k: v for k, v in m.items() if not k.startswith("_") and k not in ("reasoning_content", "reasoning_details", "reasoning_items")}
            if mine and i > last:
                for k in ("reasoning_content", "reasoning_details"):
                    if m.get(k):
                        w[k] = m[k]
            if w.get("tool_calls"):
                w["tool_calls"] = [tc if mine or "extra_content" not in tc else {k: v for k, v in tc.items() if k != "extra_content"}
                                   for tc in w["tool_calls"]]
            out.append(w)
        return out

    def _chat(self, system, messages, tools, emit, cancel, cache_key=None):
        cm = self.cache_mode()
        sysmsg = {"role": "system", "content": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}] if cm == "anthropic" else system}
        body = {"model": self.m["id"], "stream": True, "stream_options": {"include_usage": True},
                "messages": [sysmsg] + self.wire(messages)}
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
        text, reason, details, calls, usage, finish = "", "", {}, {}, {}, ""
        resp = self._post("/chat/completions", body)
        for ev in self._sse(resp, cancel):
            if ev.get("error"):  # OpenRouter & co report upstream failures inside the stream
                e = ev["error"]
                msg = (e.get("message") if isinstance(e, dict) else str(e)) or "stream error"
                code = e.get("code") if isinstance(e, dict) else None
                code = code if isinstance(code, int) else None
                raise ProviderError(f"{msg}" + (f" ({code})" if code else ""), code, None, code in RETRY_STATUS or bool(RETRY_RX.search(msg)))
            if ev.get("usage"):
                usage = ev["usage"]
            for ch in ev.get("choices") or []:
                d = ch.get("delta") or ch.get("message") or {}
                finish = ch.get("finish_reason") or finish
                r = d.get("reasoning_content") or d.get("reasoning")
                if isinstance(r, str) and r:
                    reason += r
                    emit({"type": "reasoning", "text": r})
                for x in d.get("reasoning_details") or []:  # OpenRouter: merge streamed pieces by index
                    if isinstance(x, dict):
                        cur = details.setdefault(x.get("index", len(details)), {})
                        for k, v in x.items():
                            cur[k] = cur.get(k, "") + v if isinstance(v, str) and k in ("text", "summary", "data", "signature") else v
                        if not r and x.get("type") == "reasoning.text" and x.get("text"):
                            emit({"type": "reasoning", "text": x["text"]})
                if d.get("content"):
                    text += d["content"]
                    emit({"type": "text", "text": d["content"]})
                for j, tc in enumerate(d.get("tool_calls") or []):
                    c = calls.setdefault(tc.get("index", j), {"id": "", "name": "", "args": ""})
                    c["id"] = tc.get("id") or c["id"]
                    f = tc.get("function") or {}
                    c["name"] += f.get("name") or ""
                    c["args"] += f.get("arguments") or ""
                    if tc.get("extra_content"):  # Gemini thought signature: must come back with this call
                        c["extra"] = tc["extra_content"]
        if not (text or calls or reason or usage or finish):
            raise ProviderError("the stream ended before any reply (connection dropped)", retryable=True)
        msg = {"role": "assistant", "content": text or None, "_model": self.m.get("id")}
        if reason:
            msg["reasoning_content"] = reason
        if details:
            msg["reasoning_details"] = [details[k] for k in sorted(details, key=lambda k: (not isinstance(k, int), k if isinstance(k, int) else 0))]
        if finish:
            msg["_finish"] = finish
        if calls:
            msg["tool_calls"] = []
            for _, c in sorted(calls.items()):
                tc = {"id": c["id"] or "call_" + uuid.uuid4().hex[:8], "type": "function", "function": {"name": c["name"], "arguments": c["args"] or "{}"}}
                if c.get("extra"):
                    tc["extra_content"] = c["extra"]
                msg["tool_calls"].append(tc)
        return msg, {"in": usage.get("prompt_tokens", 0), "out": usage.get("completion_tokens", 0), "cached": self._cached(usage)}

    def _responses(self, system, messages, tools, emit, cancel, cache_key=None):
        mid = self.m.get("id")
        items = []
        for m in no_images(messages) if self.blind() else messages:
            if m["role"] == "tool":
                items.append({"type": "function_call_output", "call_id": m["tool_call_id"], "output": m["content"]})
            elif m["role"] == "assistant":
                if m.get("_model") == mid:  # encrypted reasoning: the model keeps its chain of thought between tool calls
                    items.extend(m.get("reasoning_items") or [])
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
            body["include"] = ["reasoning.encrypted_content"]
        if self.m.get("max_out"):
            body["max_output_tokens"] = int(self.m["max_out"])
        if self.m.get("temperature") not in (None, ""):
            body["temperature"] = float(self.m["temperature"])
        text, calls, usage, reasoning, finish = "", {}, {}, [], ""
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
            elif t == "response.output_item.done" and ev.get("item", {}).get("type") == "reasoning":
                it = ev["item"]
                if it.get("encrypted_content"):
                    reasoning.append({k: it[k] for k in ("type", "id", "summary", "encrypted_content") if k in it})
            elif t in ("response.completed", "response.incomplete"):
                r = ev.get("response", {})
                usage = r.get("usage") or {}
                if t == "response.incomplete":
                    finish = "length" if (r.get("incomplete_details") or {}).get("reason") == "max_output_tokens" else "incomplete"
            elif t in ("response.failed", "error"):
                e = ev.get("response", {}).get("error") or ev.get("error") or ev
                msg = (e.get("message") if isinstance(e, dict) else None) or json.dumps(e)[:800]
                code = e.get("code") if isinstance(e, dict) else None
                raise ProviderError(msg, None, None, bool(RETRY_RX.search(msg)) or code in ("server_error", "rate_limit_exceeded"))
        msg = {"role": "assistant", "content": text or None, "_model": mid}
        if reasoning:
            msg["reasoning_items"] = reasoning
        if finish:
            msg["_finish"] = finish
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
