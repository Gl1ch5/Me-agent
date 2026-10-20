"""Telegram bot: Bot API calls. Newer methods (rich messages, drafts) fall back to plain text when unavailable."""
import json, re, urllib.request, uuid

from tg_format import close_md, md_chunks


class TgApi:
    # ─── telegram api ───
    def call(self, method, _timeout=60, **params):
        data = json.dumps(params).encode()
        req = urllib.request.Request(f"https://api.telegram.org/bot{self.token}/{method}", data=data,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=_timeout) as r:
            j = json.loads(r.read())
        if not j.get("ok"):
            raise RuntimeError(j.get("description"))
        return j["result"]

    def download(self, file_id, limit=20_000_000):
        """File bytes from Telegram (the Bot API caps downloads at 20 MB)."""
        f = self.call("getFile", file_id=file_id)
        if f.get("file_size", 0) > limit:
            raise RuntimeError("file over 20 MB")
        return urllib.request.urlopen(f"https://api.telegram.org/file/bot{self.token}/{f['file_path']}", timeout=60).read()

    def _thread_kw(self, thread):
        thread = getattr(self.tl, "thread", None) if thread is None else thread
        return {"message_thread_id": thread} if thread else {}

    def _try(self, cap, method, **params):
        """A call to a newer method: None when it fails; a Bot API without the method turns the feature off for good."""
        if not self.caps.get(cap):
            return None
        try:
            return self.call(method, **params)
        except Exception as e:
            if re.search(r"not found|unknown method|method is not|not implemented", str(e), re.I):
                self.caps[cap] = False
            return None

    def send(self, chat, text, thread=None, md=False, **kw):
        """Plain text, or (md=True) the agent's Markdown as a rich message, falling back to plain text if Telegram rejects it."""
        tk = self._thread_kw(thread)
        if md:
            out = None
            for part in md_chunks(text, 30000):
                out = self._try("rich", "sendRichMessage", chat_id=chat, rich_message={"markdown": part}, **tk, **kw)
                if out is None:
                    break
            else:
                return out
        out = None
        for i in range(0, max(1, len(text)), 4000):
            out = self.call("sendMessage", chat_id=chat, text=text[i:i + 4000] or "…", disable_web_page_preview=True, **tk, **kw)
        return out

    def draft(self, chat, did, md, thread=None):
        """Stream a partial answer (private chats): the draft shows live with a stop button until the final message is sent."""
        tk = self._thread_kw(thread)
        if md and self._try("rich_draft", "sendRichMessageDraft", chat_id=chat, draft_id=did, rich_message={"markdown": close_md(md)[-30000:]},
                            can_stop=True, **tk) is not None:
            return True
        return self._try("draft", "sendMessageDraft", chat_id=chat, draft_id=did, text=md[-4000:], can_stop=True, **tk) is not None

    def upload(self, method, fields, files):
        """A multipart call: fields are plain values (dicts go as JSON), files are {field: (name, bytes)}."""
        import mimetypes
        b = uuid.uuid4().hex
        parts = [f'--{b}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{json.dumps(v) if isinstance(v, dict) else v}\r\n'.encode()
                 for k, v in fields.items()]
        for field, (name, data) in files.items():
            ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
            parts.append(f'--{b}\r\nContent-Disposition: form-data; name="{field}"; filename="{name}"\r\nContent-Type: {ctype}\r\n\r\n'.encode()
                         + data + b"\r\n")
        parts.append(f"--{b}--\r\n".encode())
        req = urllib.request.Request(f"https://api.telegram.org/bot{self.token}/{method}", data=b"".join(parts),
                                     headers={"Content-Type": f"multipart/form-data; boundary={b}"})
        with urllib.request.urlopen(req, timeout=120) as r:
            j = json.loads(r.read())
        if not j.get("ok"):
            raise RuntimeError(j.get("description"))
        return j["result"]

    def send_file(self, chat, name, data, caption=""):
        fields = {"chat_id": chat, **({"caption": caption[:1000]} if caption else {})}
        return self.upload("sendDocument", fields, {"document": (name, data)})

    def edit(self, chat, mid, text, md=False):
        try:
            if md and self.caps["rich"]:
                try:
                    return self.call("editMessageText", chat_id=chat, message_id=mid, rich_message={"markdown": close_md(text)[-30000:]})
                except Exception:
                    pass
            self.call("editMessageText", chat_id=chat, message_id=mid, text=text[-4000:] or "…", disable_web_page_preview=True)
        except Exception:
            pass
