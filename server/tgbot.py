"""Telegram frontend: chat with your cloud agent from any device.
Two kinds of bots share this code: the optional global bot (token set by the admin) and personal bots — each user can paste
their own @BotFather token in the account; one poller runs per token and a personal bot only ever serves its owner.

What a linked chat gets: its own tasks run in the sandbox; tasks started anywhere else (web, terminal, another chat) are
mirrored into the chat (prompt + final answer + questions + goal results), so the chat is a live window onto the account.
"""
import base64, json, secrets, threading, time, urllib.parse, urllib.request, uuid

COMMANDS = [
    ("new", "start a new session"), ("sessions", "resume a session"), ("ws", "choose the workspace for new tasks"),
    ("model", "switch model"), ("goal", "work until done and verified: /goal <what>"), ("stop", "interrupt"),
    ("undo", "revert the last turn's files"), ("todo", "show the checklist"), ("compact", "summarize history"),
    ("cost", "spending: this session and total"), ("memory", "global notes: /memory, /memory forget <id>"),
    ("jobs", "background tasks: /jobs, /jobs run <id>"), ("preview", "link to the site the agent built"),
    ("perm", "where tools run (cloud = isolated sandbox)"), ("github", "GitHub connection status"),
    ("quiet", "stop mirroring other sessions into this chat"), ("help", "all commands"),
]


class Bot:
    def __init__(self, db, runner, users, token_for, transcribe, owner=None):
        self.db, self.runner, self.owner = db, runner, owner
        self.stopped = False
        self.users, self.token_for, self.transcribe = users, token_for, transcribe
        self.token = None
        self.username = ""
        self.offset = 0
        self.state = {}  # chat_id -> {"worker": id, "session": sid, "ws": wid}
        self.asks = {}  # chat_id -> pending agent question: {"id", "worker", "options", "t"}
        self.thread = None
        self.db.execute("CREATE TABLE IF NOT EXISTS tg_codes(code TEXT PRIMARY KEY, uid INTEGER, bot TEXT, exp REAL)")
        try:  # chats can be muted from mirroring; the setting survives restarts
            self.db.execute("ALTER TABLE tg_links ADD COLUMN quiet INTEGER DEFAULT 0")
        except Exception:
            pass

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

    def edit(self, chat, mid, text):
        try:
            self.call("editMessageText", chat_id=chat, message_id=mid, text=text[:4000] or "…", disable_web_page_preview=True)
        except Exception:
            pass

    # ─── chats ───
    @property
    def key(self):
        """tg_links.bot: '' for the global bot, the bot's username for a personal one (same chat id can link to both)."""
        return "" if self.owner is None else (self.username or "?")

    def links(self, uid):
        """(chat_id, quiet) for every chat linked to this user through this bot."""
        return [(r[0], bool(r[1])) for r in self.db.execute(
            "SELECT chat_id, COALESCE(quiet,0) FROM tg_links WHERE user_id=? AND COALESCE(bot,'')=?", (uid, self.key)).fetchall()]

    def chats_of(self, uid):
        return [c for c, _ in self.links(uid)]

    def mirror(self, uid, text, file=None):
        """Send an event from another session into every chat of this user that has not muted mirroring."""
        n = 0
        for chat, quiet in self.links(uid):
            if quiet or self.state.get(chat, {}).get("busy"):
                continue
            try:
                self.send(chat, text)
                n += 1
            except Exception:
                pass
        return n

    def stop(self):
        self.stopped = True
        self.token = None

    def mark(self, chat, **kw):
        self.state.setdefault(chat, {}).update(kw)

    # ─── lifecycle ───
    def configure(self, token):
        self.token = token or None
        if not self.token:
            self.username = ""
            return
        self.username = self.call("getMe")["username"]
        self.call("setMyCommands", commands=[{"command": c, "description": d[:250]} for c, d in COMMANDS])
        if not self.thread:
            self.thread = threading.Thread(target=self.loop, daemon=True)
            self.thread.start()

    def link_code(self, uid):
        """One-time link code, kept in the database so a server restart does not break it."""
        if self.owner is not None and uid != self.owner:
            return None
        code = secrets.token_urlsafe(8)
        self.db.execute("INSERT INTO tg_codes VALUES(?,?,?,?)", (code, uid, self.key, time.time() + 600))
        return f"https://t.me/{self.username}?start={code}" if self.username else None

    def take_code(self, code):
        r = self.db.execute("SELECT uid, bot, exp FROM tg_codes WHERE code=?", (code,)).fetchone()
        self.db.execute("DELETE FROM tg_codes WHERE code=? OR exp<?", (code, time.time()))
        if not r or r[1] != self.key or r[2] < time.time():
            return None
        return r[0]

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

    # ─── agent questions: the sandbox waits for an answer, so Telegram must answer or time out ───
    ASK_TIMEOUT = 600

    def on_ask(self, w, e):
        """Show a question from worker w in the chats driving it (or, if none drives it, in every chat of its owner)."""
        options = [str(o) for o in (e.get("options") or [])][:8]
        chats = [c for c, st in self.state.items() if st.get("worker") == w.id] or self.chats_of(w.uid)
        for chat in chats:
            self.asks[chat] = {"id": e["id"], "worker": w, "options": options, "t": time.time()}
            kw = {"reply_markup": {"inline_keyboard": [[{"text": o[:60], "callback_data": f"a:{i}"}] for i, o in enumerate(options)]}} if options else {}
            hint = "" if options else f"\n\nReply with your answer (you have {self.ASK_TIMEOUT // 60} min)."
            self.send(chat, "❓ " + (e.get("text") or "The agent has a question.") + hint, **kw)
            t = threading.Timer(self.ASK_TIMEOUT, self._ask_timeout, args=(chat, e["id"]))
            t.daemon = True
            t.start()
        return bool(chats)

    def answer(self, chat, text):
        """Route a reply to the pending question of this chat. False if nothing is waiting."""
        a = self.asks.pop(chat, None)
        if not a:
            return False
        try:
            a["worker"].send({"type": "answer", "id": a["id"], "text": text})
        except Exception:
            pass
        return True

    def _ask_timeout(self, chat, aid):
        a = self.asks.get(chat)
        if a and a["id"] == aid:
            self.asks.pop(chat, None)
            a["worker"].send({"type": "answer", "id": aid, "text": ""})
            self.send(chat, "No answer in time — the agent continues without it.")

    # ─── incoming messages ───
    def handle(self, u):
        chat = None
        try:
            if "callback_query" in u:
                chat = u["callback_query"]["message"]["chat"]["id"]
                return self.on_callback(u["callback_query"])
            m = u.get("message")
            if not m:
                return
            chat = m["chat"]["id"]
            text = (m.get("text") or m.get("caption") or "").strip()
            if text.startswith("/start"):
                code = text.split(" ", 1)[1].strip() if " " in text else ""
                uid = self.take_code(code)
                if uid:
                    self.db.execute("DELETE FROM tg_links WHERE chat_id=? AND COALESCE(bot,'')=?", (chat, self.key))
                    self.db.execute("INSERT INTO tg_links(chat_id,user_id,created,bot,quiet) VALUES(?,?,?,?,0)", (chat, uid, time.time(), self.key))
                    return self.send(chat, "Linked. Write a task — it runs in your cloud sandbox and syncs to all your machines.\n"
                                           "Things started elsewhere show up here too. /help for commands.")
                return self.send(chat, "This link has expired or belongs to another bot. Open your account on the me site → Telegram → connect.")
            uid = self.uid_for(chat)
            if not uid:
                return self.send(chat, "Not linked yet. Open your account on the me site → Telegram → connect.")
            images, files = [], []
            if m.get("voice") or m.get("audio"):
                f = m.get("voice") or m.get("audio")
                try:
                    text = self.transcribe(uid, self.download(f["file_id"]))
                except Exception as e:
                    return self.send(chat, f"voice: {e}")
                self.send(chat, f"» {text}")
            if m.get("photo"):
                p = max(m["photo"], key=lambda x: x.get("file_size", 0))
                data = self.download(p["file_id"])
                images.append("data:image/jpeg;base64," + base64.b64encode(data).decode())
                files.append(("photo.jpg", data))
            if m.get("document"):
                d = m["document"]
                files.append((d.get("file_name") or "file", self.download(d["file_id"])))
            if not text and not files:
                return
            if text.startswith("/"):
                if text.split()[0].split("@")[0] == "/stop":
                    self.asks.pop(chat, None)
                return self.command(chat, uid, text)
            if self.answer(chat, text or "(attachment)"):
                return self.send(chat, "✓ answer sent")
            self.run(chat, uid, text, images=images, files=files)
        except Exception as e:
            try:
                if chat is not None:
                    self.send(chat, f"error: {e}")
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

    def run(self, chat, uid, text, images=(), files=()):
        w = self.worker(chat, uid)
        if w.busy:
            return self.send(chat, "still working on the previous task — /stop to interrupt")
        if files:  # attachments go into the workspace, so the agent can open them like any project file
            from cloud import ws_put
            names = []
            for name, data in files:
                safe = "".join(c for c in name if c.isalnum() or c in "._-")[:80] or "file"
                ws_put(uid, w.wid, [{"path": f"uploads/{safe}", "b64": base64.b64encode(data).decode(), "mtime": time.time()}], [])
                names.append(f"uploads/{safe}")
            text = (text or "Look at the attached file(s).") + "\n\nAttached: " + ", ".join(names)
        after = w.seq
        msg = self.send(chat, "working…")
        mid = msg["message_id"]
        w.tg_turn = True  # this turn's answer goes to this chat; other turns are mirrored
        self.mark(chat, busy=True)
        w.prompt(text, images)
        steps, answer, t0, last_edit = [], "", time.time(), 0
        idle = 0
        try:
            while True:
                evs = w.wait_events(after, timeout=20)
                idle = 0 if evs else idle + 1
                if idle > 180:  # an hour without any event: the sandbox is gone or hung
                    return self.edit(chat, mid, "sandbox stopped responding — try again")
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
        finally:
            w.tg_turn = False
            self.mark(chat, busy=False)

    # ─── commands ───
    def command(self, chat, uid, text):
        parts = text.split()
        cmd = parts[0].split("@")[0].lower()
        arg = text[len(parts[0]):].strip()
        st = self.state.setdefault(chat, {})
        w = None
        try:
            w = self.runner.get(uid, st.get("worker", "")) if self.runner else None
        except KeyError:
            pass
        handler = getattr(self, "cmd_" + cmd[1:], None)
        if cmd in ("/start",):
            return self.send(chat, "You are linked. /help for commands.")
        if handler is None:
            return self.send(chat, f"unknown command {cmd} — /help")
        return handler(chat, uid, st, w, arg)

    def cmd_help(self, chat, uid, st, w, arg):
        self.send(chat, "Write a task and it runs in your cloud sandbox. Voice, photos and files work too.\n\n"
                  + "\n".join(f"/{c} — {d}" for c, d in COMMANDS))

    def cmd_new(self, chat, uid, st, w, arg):
        st.pop("session", None)
        if w:
            w.send({"type": "new"})
        self.send(chat, "new session")

    def cmd_stop(self, chat, uid, st, w, arg):
        if w:
            w.send({"type": "cancel"})
        self.send(chat, "interrupted")

    def cmd_sessions(self, chat, uid, st, w, arg):
        rows = self.db.execute("SELECT key,content FROM docs WHERE user_id=? AND deleted=0 AND key LIKE 'session/%' ORDER BY ts DESC LIMIT 8",
                               (uid,)).fetchall()
        kb = []
        for r in rows:
            try:
                d = json.loads(r[1])
            except ValueError:
                continue
            kb.append([{"text": (d.get("title") or "(untitled)")[:40], "callback_data": "s:" + r[0][8:]}])
        self.send(chat, "resume a session:" if kb else "no sessions yet", reply_markup={"inline_keyboard": kb})

    def cmd_ws(self, chat, uid, st, w, arg):
        from cloud import ws_list
        kb = [[{"text": f"{x['name']} · {x['files']} files", "callback_data": "w:" + x["id"]}] for x in ws_list(uid)[:10]]
        self.send(chat, "workspace for new tasks:" if kb else "no workspaces yet", reply_markup={"inline_keyboard": kb})

    def cmd_model(self, chat, uid, st, w, arg):
        cfg = self.config(uid)
        kb = [[{"text": ("● " if m["name"] == cfg.get("model") else "") + m["name"], "callback_data": "m:" + m["name"][:60]}]
              for m in cfg.get("models", [])[:20]]
        self.send(chat, "model:" if kb else "no models — add one in the web account", reply_markup={"inline_keyboard": kb})

    def cmd_goal(self, chat, uid, st, w, arg):
        if not arg:
            return self.send(chat, "usage: /goal <what to achieve> — I work until it is done and verified, or tell you why it is blocked")
        w = self.worker(chat, uid)
        if w.busy:
            return self.send(chat, "still working — /stop first")
        w.tg_turn, w.tg_goal = True, True
        w.send({"type": "goal", "text": arg})  # the goal event tells the chat the outcome when it finishes
        self.send(chat, "◎ goal started — I'll write when it is done or blocked")

    def cmd_undo(self, chat, uid, st, w, arg):
        if not w:
            return self.send(chat, "nothing to undo yet")
        w.send({"type": "undo"})

    def cmd_compact(self, chat, uid, st, w, arg):
        if not w:
            return self.send(chat, "nothing to compact yet")
        w.send({"type": "compact"})
        self.send(chat, "compacting history…")

    def _session_doc(self, uid, sid):
        r = self.db.execute("SELECT content FROM docs WHERE user_id=? AND key=? AND deleted=0", (uid, "session/" + sid)).fetchone() if sid else None
        try:
            return json.loads(r[0]) if r else {}
        except ValueError:
            return {}

    def cmd_todo(self, chat, uid, st, w, arg):
        sid = (w.session if w else None) or st.get("session")
        items = self._session_doc(uid, sid).get("todo") or []
        if not items:
            return self.send(chat, "no checklist in this session yet")
        mark = {"done": "✓", "doing": "▸", "todo": "○"}
        done = sum(1 for i in items if i.get("s") == "done")
        self.send(chat, f"checklist {done}/{len(items)}\n" + "\n".join(f"{mark.get(i.get('s'), '○')} {i.get('t', '')}" for i in items))

    def cmd_cost(self, chat, uid, st, w, arg):
        sid = (w.session if w else None) or st.get("session")
        u = self._session_doc(uid, sid).get("usage", {})
        total = 0.0
        for r in self.db.execute("SELECT content FROM docs WHERE user_id=? AND deleted=0 AND key LIKE 'session/%'", (uid,)):
            try:
                total += float(json.loads(r[0]).get("usage", {}).get("cost", 0) or 0)
            except ValueError:
                pass
        self.send(chat, f"this session: ${float(u.get('cost', 0) or 0):.4f} · ↑{u.get('in', 0):,} ↓{u.get('out', 0):,} tokens\n"
                        f"all sessions: ${total:.4f}\n(the daily limit and per-task limits are set in the web account)")

    def cmd_memory(self, chat, uid, st, w, arg):
        if arg.startswith("forget "):
            mid = "".join(c for c in arg[7:].strip() if c.isalnum() or c in "-_")
            if not mid:
                return self.send(chat, "usage: /memory forget <id>")
            from app import put_doc  # a real deletion, so every device drops the note
            put_doc(uid, f"memory/{mid}.json", "", deleted=True)
            return self.send(chat, f"forgot {mid} on all devices")
        rows = self.db.execute("SELECT key, content FROM docs WHERE user_id=? AND deleted=0 AND key LIKE 'memory/%'", (uid,)).fetchall()
        notes = []
        for r in rows:
            try:
                d = json.loads(r[1])
                notes.append((d.get("ts", 0), r[0][7:-5], d.get("text", "")))
            except ValueError:
                pass
        if not notes:
            return self.send(chat, "no notes yet — ask the agent to remember something")
        notes.sort(reverse=True)
        self.send(chat, "\n".join(f"{time.strftime('%Y-%m-%d', time.localtime(t))} · {text[:200]}\n   /memory forget {mid}" for t, mid, text in notes[:15]))

    def cmd_jobs(self, chat, uid, st, w, arg):
        if arg.startswith("run "):
            jid = arg[4:].strip()
            if not jid.isdigit():
                return self.send(chat, "usage: /jobs run <id>")
            self.db.execute("UPDATE jobs SET next=?, enabled=1 WHERE id=? AND user_id=?", (time.time(), int(jid), uid))
            return self.send(chat, f"job {jid} queued — it runs in a minute")
        rows = self.db.execute("SELECT id, name, status, cron, next, enabled FROM jobs WHERE user_id=? ORDER BY created DESC LIMIT 15",
                               (uid,)).fetchall()
        if not rows:
            return self.send(chat, "no background tasks yet — create one in the web account")
        lines = []
        for r in rows:
            when = time.strftime("%d.%m %H:%M", time.localtime(r[4])) if r[5] and r[4] else "—"
            lines.append(f"#{r[0]} {r[1][:50]} · {r[2] or 'waiting'} · {r[3] or 'once'} · next {when}")
        self.send(chat, "\n".join(lines) + "\n\n/jobs run <id> to start one now")

    def cmd_preview(self, chat, uid, st, w, arg):
        if not w:
            return self.send(chat, "no project yet — start a task first")
        url = w.info().get("preview")
        self.send(chat, f"preview: {url}/" if url and url.startswith("http") else "preview is available once the project runs on the server")

    def cmd_perm(self, chat, uid, st, w, arg):
        self.send(chat, "Cloud tasks always run in an isolated sandbox: files only in the workspace, no access to the server.\n"
                        "Read-only / ask / full access apply to the local agent (terminal and `me web`); set it there with /perm.")

    def cmd_github(self, chat, uid, st, w, arg):
        cfg = self.config(uid)
        has = bool((cfg.get("secrets") or {}).get("GITHUB_TOKEN"))
        self.send(chat, "GitHub is connected: the agent can clone, commit, push and open PRs." if has
                  else "GitHub is not connected. Add a token in the web account (cloud → GitHub).")

    def cmd_quiet(self, chat, uid, st, w, arg):
        q = 0 if self.db.execute("SELECT COALESCE(quiet,0) FROM tg_links WHERE chat_id=? AND COALESCE(bot,'')=?", (chat, self.key)).fetchone()[0] else 1
        self.db.execute("UPDATE tg_links SET quiet=? WHERE chat_id=? AND COALESCE(bot,'')=?", (q, chat, self.key))
        self.send(chat, "other sessions are no longer mirrored here (/quiet again to turn it back on)" if q
                  else "mirroring is on: tasks from the web and terminal show up here")

    def config(self, uid):
        r = self.db.execute("SELECT content FROM docs WHERE user_id=? AND key='config' AND deleted=0", (uid,)).fetchone()
        try:
            return json.loads(r[0]) if r else {}
        except ValueError:
            return {}

    # ─── buttons ───
    def on_callback(self, q):
        chat = q["message"]["chat"]["id"]
        uid = self.uid_for(chat)
        self.call("answerCallbackQuery", callback_query_id=q["id"])
        if not uid:
            return
        kind, _, val = q["data"].partition(":")
        st = self.state.setdefault(chat, {})
        if kind == "a":  # answer to an agent question from its buttons
            a = self.asks.get(chat)
            if a and val.isdigit() and int(val) < len(a["options"]):
                self.answer(chat, a["options"][int(val)])
                return self.send(chat, "✓ " + a["options"][int(val)])
            return self.send(chat, "that question is no longer open")
        if kind == "s":
            row = self.db.execute("SELECT content FROM docs WHERE user_id=? AND key=? AND deleted=0", (uid, "session/" + val)).fetchone()
            if not row:
                return self.send(chat, "that session no longer exists")
            st["session"] = val
            st.pop("worker", None)
            ws = self._session_doc(uid, val).get("ws")
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
                w = self.runner.get(uid, st.get("worker", ""))
                w.send({"type": "sync"})
                w.send({"type": "model", "name": val})  # the chat running now switches too; other chats keep theirs
            except KeyError:
                pass
            self.send(chat, f"model → {val}")
