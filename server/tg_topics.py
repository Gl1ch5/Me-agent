"""Telegram bot: per-chat state, and topics in private chats (one topic per session)."""


class TgTopics:
    # ─── topics: one per session ───
    def topic_of(self, chat, session):
        r = self.db.execute("SELECT thread FROM tg_topics WHERE chat_id=? AND bot=? AND session=?", (chat, self.key, session)).fetchone()
        return r[0] if r else None

    def bind_topic(self, chat, thread, session=None, ws=None):
        self.db.execute("INSERT OR REPLACE INTO tg_topics VALUES(?,?,?,?,?)", (chat, self.key, thread, session, ws))

    def new_topic(self, chat, name, session=None, ws=None):
        """A topic for a session in a private chat; None when topics are off (then everything stays in the main chat)."""
        if not self.topics or chat < 0:
            return None
        try:
            t = self.call("createForumTopic", chat_id=chat, name=(name or "new chat")[:128])["message_thread_id"]
        except Exception:
            return None
        self.bind_topic(chat, t, session, ws)
        return t

    def name_topic(self, chat, thread, title):
        try:
            self.call("editForumTopic", chat_id=chat, message_thread_id=thread, name=title[:128])
        except Exception:
            pass

    def st(self, chat, thread=None):
        """State of a chat, or of one topic in it: {"worker", "session", "ws", "busy"}; a topic remembers its session across restarts."""
        thread = (getattr(self.tl, "thread", None) if thread is None else thread) or 0
        k = (chat, thread)
        if k not in self.state:
            self.state[k] = {}
            if thread:
                r = self.db.execute("SELECT session, ws FROM tg_topics WHERE chat_id=? AND bot=? AND thread=?", (chat, self.key, thread)).fetchone()
                if r:
                    self.state[k].update({x: v for x, v in (("session", r[0]), ("ws", r[1])) if v})
        return self.state[k]
