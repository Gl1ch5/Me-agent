"""Telegram frontend: chat with your cloud agent from any device.
Two kinds of bots share this code: the optional global bot (token set by the admin) and personal bots — each user can paste
their own @BotFather token in the account; one poller runs per token and a personal bot only ever serves its owner.
"""
import json, secrets, threading, time, urllib.parse, urllib.request, uuid


class Bot:
    def __init__(self, db, runner, users, token_for, transcribe, owner=None):
        self.db, self.runner, self.owner = db, runner, owner
        self.stopped = False
        self.users, self.token_for, self.transcribe = users, token_for, transcribe
        self.token = None
        self.username = ""
        self.offset = 0
        self.codes = {}  # code -> (uid, expires)
        self.state = {}  # chat_id -> {"worker": id, "session": sid, "ws": wid}
        self.thread = None

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

    def send(self, chat, text, **kw):
        out = None
        for i in range(0, max(1, len(text)), 4000):
            out = self.call("sendMessage", chat_id=chat, text=text[i:i + 4000] or "…", disable_web_page_preview=True, **kw)
        return out

    def send_file(self, chat, name, data, caption=""):
        import mimetypes
        b = uuid.uuid4().hex
        parts = [f'--{b}\r\nContent-Disposition: form-data; name="chat_id"\r\n\r\n{chat}\r\n'.encode()]
        if caption:
            parts.append(f'--{b}\r\nContent-Disposition: form-data; name="caption"\r\n\r\n{caption[:1000]}\r\n'.encode())
        ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
        parts.append(f'--{b}\r\nContent-Disposition: form-data; name="document"; filename="{name}"\r\nContent-Type: {ctype}\r\n\r\n'.encode()
                     + data + b"\r\n")
        parts.append(f"--{b}--\r\n".encode())
        req = urllib.request.Request(f"https://api.telegram.org/bot{self.token}/sendDocument", data=b"".join(parts),
                                     headers={"Content-Type": f"multipart/form-data; boundary={b}"})
        with urllib.request.urlopen(req, timeout=120) as r:
            j = json.loads(r.read())
        if not j.get("ok"):
            raise RuntimeError(j.get("description"))
        return j["result"]

    def chats_of(self, uid):
        return [r[0] for r in self.db.execute("SELECT chat_id FROM tg_links WHERE user_id=? AND COALESCE(bot,'')=?", (uid, self.key))]

    @property
    def key(self):
        """tg_links.bot: '' for the global bot, the bot's username for a personal one (same chat id can link to both)."""
        return "" if self.owner is None else (self.username or "?")

    def stop(self):
        self.stopped = True
        self.token = None

    def edit(self, chat, mid, text):
        try:
            self.call("editMessageText", chat_id=chat, message_id=mid, text=text[:4000] or "…", disable_web_page_preview=True)
        except Exception:
            pass

    # ─── lifecycle ───
    def configure(self, token):
        self.token = token or None
        if not self.token:
            self.username = ""
            return
        self.username = self.call("getMe")["username"]
        self.call("setMyCommands", commands=[
            {"command": "new", "description": "new session"}, {"command": "sessions", "description": "resume a session"},
            {"command": "ws", "description": "choose workspace"}, {"command": "model", "description": "switch model"},
            {"command": "stop", "description": "interrupt"}, {"command": "help", "description": "help"}])
        if not self.thread:
            self.thread = threading.Thread(target=self.loop, daemon=True)
            self.thread.start()

    def link_code(self, uid):
        if self.owner is not None and uid != self.owner:
            return None
        code = secrets.token_urlsafe(8)
        self.codes[code] = (uid, time.time() + 600)
        return f"https://t.me/{self.username}?start={code}" if self.username else None

    def uid_for(self, chat):
        r = self.db.execute("SELECT user_id FROM tg_links WHERE chat_id=? AND COALESCE(bot,'')=?", (chat, self.key)).fetchone()
        return r[0] if r else None

    def loop(self):
        while not self.stopped:
            if not self.token:
                time.sleep(5)
                continue
            try:
                ups = self.call("getUpdates", _timeout=70, offset=self.offset, timeout=50)
            except Exception:
                time.sleep(5)
                continue
            for u in ups:
                self.offset = u["update_id"] + 1
                threading.Thread(target=self.handle, args=(u,), daemon=True).start()

    # ─── handlers ───
    def handle(self, u):
        try:
            if "callback_query" in u:
                return self.on_callback(u["callback_query"])
            m = u.get("message")
            if not m:
                return
            chat = m["chat"]["id"]
            text = m.get("text", "")
            if text.startswith("/start"):
                code = text.split(" ", 1)[1].strip() if " " in text else ""
                uid, exp = self.codes.pop(code, (None, 0))
                if uid and exp > time.time():
                    self.db.execute("DELETE FROM tg_links WHERE chat_id=? AND COALESCE(bot,'')=?", (chat, self.key))
                    self.db.execute("INSERT INTO tg_links(chat_id,user_id,created,bot) VALUES(?,?,?,?)", (chat, uid, time.time(), self.key))
                    return self.send(chat, "Linked. Write a task — it runs in your cloud sandbox and syncs to all your machines.\n/help for commands.")
                return self.send(chat, "Open your account on the me site → Telegram → connect.")
            uid = self.uid_for(chat)
            if not uid:
                return self.send(chat, "Not linked yet. Open your account on the me site → Telegram → connect.")
            if m.get("voice") or m.get("audio"):
                f = self.call("getFile", file_id=(m.get("voice") or m.get("audio"))["file_id"])
                audio = urllib.request.urlopen(f"https://api.telegram.org/file/bot{self.token}/{f['file_path']}", timeout=60).read()
                try:
                    text = self.transcribe(uid, audio)
                except Exception as e:
                    return self.send(chat, f"voice: {e}")
                self.send(chat, f"» {text}")
            if not text:
                return
            if text.startswith("/"):
                return self.command(chat, uid, text)
            self.run(chat, uid, text)
        except Exception as e:
            try:
                self.send(u.get("message", u.get("callback_query", {}).get("message", {}))["chat"]["id"], f"error: {e}")
            except Exception:
                pass

    def worker(self, chat, uid):
        st = self.state.setdefault(chat, {})
        try:
            w = self.runner.get(uid, st.get("worker", ""))
            if w.alive:
                return w
        except KeyError:
            pass
        user = self.users(uid)
        w = self.runner.start(uid, user["username"], lambda: self.token_for(uid), wid=st.get("ws"), session=st.get("session"))
        st["worker"] = w.id
        return w

    def run(self, chat, uid, text):
        w = self.worker(chat, uid)
        if w.busy:
            return self.send(chat, "still working on the previous task — /stop to interrupt")
        after = w.seq
        msg = self.send(chat, "working…")
        mid = msg["message_id"]
        w.prompt(text)
        steps, answer, t0, last_edit = [], "", time.time(), 0
        while True:
            evs = w.wait_events(after, timeout=20)
            for e in evs:
                after = e["seq"]
                t = e.get("type")
                if t == "tool_start":
                    steps.append(f"· {e['name']} {e.get('label', '')[:70]}")
                    answer = ""
                elif t == "tool_end" and not e.get("ok") and steps:
                    steps[-1] += "  ✗"
                elif t == "text":
                    answer += e["text"]
                elif t == "error":
                    answer += f"\n[{e['text']}]"
                elif t == "turn_end":
                    st = self.state.setdefault(chat, {})
                    st["session"] = w.session
                    head = "\n".join(steps[-15:])
                    self.edit(chat, mid, (head + "\n\n" if head else "") + f"done · {e['time']:.1f}s · {e['in'] + e['out']:,} tokens")
                    self.send(chat, answer.strip() or "(no text)")
                    return
                elif t == "exit":
                    return self.edit(chat, mid, "sandbox stopped")
            if time.time() - last_edit > 2.5:
                last_edit = time.time()
                tail = answer[-600:] if answer else ""
                self.edit(chat, mid, "\n".join(steps[-12:] + ([f"\n{tail}"] if tail else [])) + f"\n\nworking… {time.time() - t0:.0f}s")

    def command(self, chat, uid, text):
        cmd = text.split()[0].split("@")[0]
        st = self.state.setdefault(chat, {})
        if cmd == "/new":
            st.pop("session", None)
            try:
                w = self.runner.get(uid, st.get("worker", ""))
                w.send({"type": "new"})
            except KeyError:
                pass
            return self.send(chat, "new session")
        if cmd == "/stop":
            try:
                self.runner.get(uid, st.get("worker", "")).send({"type": "cancel"})
            except KeyError:
                pass
            return self.send(chat, "interrupted")
        if cmd == "/sessions":
            rows = self.db.execute("SELECT key,content FROM docs WHERE user_id=? AND deleted=0 AND key LIKE 'session/%' ORDER BY ts DESC LIMIT 8",
                                   (uid,)).fetchall()
            kb = []
            for r in rows:
                try:
                    d = json.loads(r[1])
                except ValueError:
                    continue
                kb.append([{"text": (d.get("title") or "(untitled)")[:40], "callback_data": "s:" + r[0][8:]}])
            return self.send(chat, "resume a session:" if kb else "no sessions yet", reply_markup={"inline_keyboard": kb})
        if cmd == "/ws":
            from cloud import ws_list
            kb = [[{"text": f"{w['name']} · {w['files']} files", "callback_data": "w:" + w["id"]}] for w in ws_list(uid)[:10]]
            return self.send(chat, "workspace for new tasks:" if kb else "no workspaces yet", reply_markup={"inline_keyboard": kb})
        if cmd == "/model":
            cfg = self.config(uid)
            kb = [[{"text": ("● " if m["name"] == cfg.get("model") else "") + m["name"], "callback_data": "m:" + m["name"][:60]}]
                  for m in cfg.get("models", [])[:20]]
            return self.send(chat, "model:" if kb else "no models — add one in the web account", reply_markup={"inline_keyboard": kb})
        return self.send(chat, "Write a task and it runs in your cloud sandbox.\nVoice messages work too (Groq key in your account).\n\n"
                               "/new new session\n/sessions resume\n/ws workspace\n/model switch model\n/stop interrupt")

    def config(self, uid):
        r = self.db.execute("SELECT content FROM docs WHERE user_id=? AND key='config' AND deleted=0", (uid,)).fetchone()
        return json.loads(r[0]) if r else {}

    def on_callback(self, q):
        chat = q["message"]["chat"]["id"]
        uid = self.uid_for(chat)
        self.call("answerCallbackQuery", callback_query_id=q["id"])
        if not uid:
            return
        kind, _, val = q["data"].partition(":")
        st = self.state.setdefault(chat, {})
        if kind == "s":
            st["session"] = val
            st.pop("worker", None)
            ws = json.loads(self.db.execute("SELECT content FROM docs WHERE user_id=? AND key=?", (uid, "session/" + val)).fetchone()[0]).get("ws")
            if ws:
                st["ws"] = ws
            self.send(chat, "resumed — continue writing")
        elif kind == "w":
            st["ws"] = val
            st.pop("worker", None)
            st.pop("session", None)
            self.send(chat, f"workspace set: {val}")
        elif kind == "m":
            from app import put_doc  # late import to avoid a cycle
            cfg = self.config(uid)
            cfg["model"] = val
            put_doc(uid, "config", json.dumps(cfg, ensure_ascii=False, indent=1))
            try:
                self.runner.get(uid, st.get("worker", "")).send({"type": "sync"})
            except KeyError:
                pass
            self.send(chat, f"model → {val}")
