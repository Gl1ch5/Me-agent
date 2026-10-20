"""Telegram frontend: chat with your cloud agent from any device.
Two kinds of bots share this code: the optional global bot (token set by the admin) and personal bots — each user can paste
their own @BotFather token in the account; one poller runs per token and a personal bot only ever serves its owner.

What a linked chat gets: its own tasks run in the sandbox; tasks started anywhere else (web, terminal, another chat) are
mirrored into the chat (prompt + final answer + questions + goal results), so the chat is a live window onto the account.

Newer Bot API features, each with a fallback for chats or bots that lack it:
- answers are rich messages (sendRichMessage: the agent's Markdown with headings, tables, code blocks), else plain text;
- in private chats the answer streams as a draft (sendRichMessageDraft / sendMessageDraft, with a stop button), else one
  status message is edited;
- a bot with topics enabled in private chats (@BotFather) keeps one session per topic: /new opens a topic, a topic the
  user creates starts a fresh session, and sessions from the web or terminal get their own topic when mirrored.
"""
import base64, secrets, threading, time
from pathlib import Path

import acctlog
from tg_api import TgApi
from tg_commands import COMMANDS, TgCommands
from tg_i18n import tr
from tg_tasks import TgTasks
from tg_topics import TgTopics

# Every bot (the global one and each personal one) gets this avatar and description; bump PROFILE_VER to push a change to all.
PROFILE_VER = 1
AVATAR = Path(__file__).resolve().parent / "site" / "bot-avatar.jpg"
PROFILE = {
    "": ("Your me coding agent in Telegram. Write a task — it runs in your cloud sandbox and syncs to all your devices: "
         "answers stream live, tasks started on the web or in the terminal show up here, each session can have its own topic. "
         "Link the chat in your account on me-agent.ru → Telegram.",
         "me — your coding agent: tasks run in your cloud sandbox and sync everywhere."),
    "ru": ("Ваш агент me в Telegram. Напишите задачу — она выполнится в вашей облачной песочнице и синхронизируется на все устройства: "
           "ответ стримится вживую, задачи из веба и терминала тоже приходят сюда, у каждой сессии может быть своя тема. "
           "Подключите чат в аккаунте на me-agent.ru → Telegram.",
           "me — ваш агент для кода: задачи выполняются в облачной песочнице и синхронизируются везде."),
}


class Bot(TgApi, TgTopics, TgTasks, TgCommands):
    def __init__(self, db, runner, users, token_for, transcribe, owner=None):
        self.db, self.runner, self.owner = db, runner, owner
        self.stopped = False
        self.users, self.token_for, self.transcribe = users, token_for, transcribe
        self.token = None
        self.username = ""
        self.offset = 0
        self.state = {}  # (chat_id, topic or 0) -> {"worker": id, "session": sid, "ws": wid, "busy"}
        self.asks = {}  # (chat_id, topic or 0) -> pending agent question: {"id", "worker", "options", "t"}
        self.thread = None
        self.topics = False  # the bot has topic mode in private chats: one topic per session
        self.caps = {"rich": True, "draft": True, "rich_draft": True}  # switched off for good when this Bot API lacks a method
        self.tl = threading.local()  # the topic the update being handled came from: replies go back there
        self.seen_lang = {}  # chat -> Telegram's language of its user, for messages sent outside an update (mirroring)
        self.db.execute("CREATE TABLE IF NOT EXISTS tg_codes(code TEXT PRIMARY KEY, uid INTEGER, bot TEXT, exp REAL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS tg_topics(chat_id INTEGER, bot TEXT, thread INTEGER, session TEXT, ws TEXT, "
                        "PRIMARY KEY(chat_id, bot, thread))")
        self.db.execute("CREATE TABLE IF NOT EXISTS tg_profile(bot TEXT PRIMARY KEY, ver INTEGER)")
        try:  # chats can be muted from mirroring; the setting survives restarts
            self.db.execute("ALTER TABLE tg_links ADD COLUMN quiet INTEGER DEFAULT 0")
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

    def mirror(self, uid, text, file=None, session=None, title=""):
        """Send an event from another session into every chat of this user that has not muted mirroring: into the session's
        topic when the bot has topics (opened on first use), else into the main chat."""
        n = 0
        for chat, quiet in self.links(uid):
            if quiet:
                continue
            thread = (self.topic_of(chat, session) or self.new_topic(chat, title or "session", session)) if session and self.topics else 0
            if self.state.get((chat, thread or 0), {}).get("busy"):
                continue
            try:
                self.send(chat, text, thread=thread or 0, md=True)
                n += 1
            except Exception:
                pass
        return n

    def ui_lang(self, chat):
        """'ru' or 'en' for the bot's own words: the update's language, else the account setting, else Telegram's."""
        lang = getattr(self.tl, "lang", None)
        if lang is None:
            uid = self.uid_for(chat)
            lang = ((self.config(uid).get("settings") or {}).get("lang") if uid else "") or self.seen_lang.get(chat, "")
        return "ru" if lang == "ru" else "en"

    def stop(self):
        self.stopped = True
        self.token = None

    # ─── lifecycle ───
    def configure(self, token):
        self.token = token or None
        if not self.token:
            self.username = ""
            return
        me = self.call("getMe")
        self.username, self.topics = me["username"], bool(me.get("has_topics_enabled"))
        self.call("setMyCommands", commands=[{"command": c, "description": d[:250]} for c, d in COMMANDS])
        self.call("setMyCommands", commands=[{"command": c, "description": tr(d, "ru")[:250]} for c, d in COMMANDS], language_code="ru")
        threading.Thread(target=self.profile, daemon=True).start()
        if not self.thread:
            self.thread = threading.Thread(target=self.loop, daemon=True)
            self.thread.start()

    def profile(self):
        """Avatar and description, set once per bot and PROFILE_VER (a profile photo upload is never reused, so not on every start).
        The name stays whatever the owner picked in @BotFather."""
        name = self.username
        r = self.db.execute("SELECT ver FROM tg_profile WHERE bot=?", (name,)).fetchone()
        if not name or (r and r[0] >= PROFILE_VER):
            return
        try:
            for lang, (desc, short) in PROFILE.items():
                self.call("setMyDescription", description=desc[:512], language_code=lang)
                self.call("setMyShortDescription", short_description=short[:120], language_code=lang)
            self.upload("setMyProfilePhoto", {"photo": {"type": "static", "photo": "attach://avatar"}},
                        {"avatar": ("avatar.jpg", AVATAR.read_bytes())})
        except Exception:
            return  # retried on the next start
        self.db.execute("INSERT OR REPLACE INTO tg_profile VALUES(?,?)", (name, PROFILE_VER))

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

    # ─── incoming messages ───
    def _lang(self, chat, frm):
        tg = "ru" if str((frm or {}).get("language_code", "")).startswith(("ru", "uk", "be", "kk")) else "en"
        self.seen_lang[chat] = tg
        uid = self.uid_for(chat)
        self.tl.lang = ((self.config(uid).get("settings") or {}).get("lang") if uid else "") or tg

    def handle(self, u):
        chat = None
        self.tl.thread, self.tl.lang = 0, None
        try:
            if "stopped_message_generation" in u:  # the stop button under a streamed draft
                g = u["stopped_message_generation"]
                return self.stop_turn(g["chat"]["id"], g.get("message_thread_id") or 0)
            if "callback_query" in u:
                qm = u["callback_query"]["message"]
                chat = qm["chat"]["id"]
                self._lang(chat, u["callback_query"].get("from"))
                self.tl.thread = qm.get("message_thread_id") if qm.get("is_topic_message") else 0
                acctlog.log(self.uid_for(chat), "tg_button", data=u["callback_query"].get("data"), thread=self.tl.thread, bot=self.key)
                return self.on_callback(u["callback_query"])
            m = u.get("message")
            if not m:
                return
            chat = m["chat"]["id"]
            self.tl.thread = m.get("message_thread_id") if m.get("is_topic_message") else 0
            self._lang(chat, m.get("from"))
            if m.get("forum_topic_created") or m.get("forum_topic_edited"):
                return
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
            acctlog.log(uid, "tg_msg", text=acctlog.cut(text, 300), thread=self.tl.thread, bot=self.key,
                        media=[k for k in ("voice", "audio", "photo", "document") if m.get(k)])
            try:
                self.ask_search_key(chat, uid)
            except Exception:
                pass
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
                    self.asks.pop((chat, self.tl.thread or 0), None)
                return self.command(chat, uid, text)
            if self.answer((chat, self.tl.thread or 0), text or "(attachment)"):
                return self.send(chat, "✓ answer sent")
            self.run(chat, uid, text, images=images, files=files)
        except Exception as e:
            try:
                acctlog.error(chat is not None and self.uid_for(chat), "tg_crash", e, update=acctlog.cut(u, 400))
            except Exception:
                pass
            try:
                if chat is not None:
                    self.send(chat, f"error: {e}")
            except Exception:
                pass
