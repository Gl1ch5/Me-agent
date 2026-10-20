"""Telegram bot: running a task in the sandbox from a chat, streaming its answer, and the agent's questions."""
import base64, random, threading, time

from tg_format import progress_md, steps_md
from tg_i18n import tr

ANSWER_KB = {"inline_keyboard": [[{"text": "↻ retry", "callback_data": "c:retry"}, {"text": "↶ undo", "callback_data": "c:undo"},
                                  {"text": "＋ new", "callback_data": "c:new"}]]}


class TgTasks:
    # ─── agent questions: the sandbox waits for an answer, so Telegram must answer or time out ───
    ASK_TIMEOUT = 600

    def on_ask(self, w, e):
        """Show a question from worker w in the chats driving it (or, if none drives it, in every chat of its owner)."""
        options = [str(o) for o in (e.get("options") or [])][:8]
        keys = [k for k, st in self.state.items() if st.get("worker") == w.id] or [(c, self.topic_of(c, w.session) or 0) for c in self.chats_of(w.uid)]
        for k in keys:
            self.asks[k] = {"id": e["id"], "worker": w, "options": options, "t": time.time()}
            kw = {"reply_markup": {"inline_keyboard": [[{"text": o[:60], "callback_data": f"a:{i}"}] for i, o in enumerate(options)]}} if options else {}
            hint = "" if options else f"\n\nReply with your answer (you have {self.ASK_TIMEOUT // 60} min)."
            self.send(k[0], "❓ " + (e.get("text") or tr("The agent has a question.", self.ui_lang(k[0]))) + hint, thread=k[1], **kw)
            t = threading.Timer(self.ASK_TIMEOUT, self._ask_timeout, args=(k, e["id"]))
            t.daemon = True
            t.start()
        return bool(keys)

    def answer(self, k, text):
        """Route a reply to the pending question of this chat (or topic). False if nothing is waiting."""
        a = self.asks.pop(k, None)
        if not a:
            return False
        try:
            a["worker"].send({"type": "answer", "id": a["id"], "text": text})
        except Exception:
            pass
        return True

    def _ask_timeout(self, k, aid):
        a = self.asks.get(k)
        if a and a["id"] == aid:
            self.asks.pop(k, None)
            a["worker"].send({"type": "answer", "id": aid, "text": ""})
            self.send(k[0], "No answer in time — the agent continues without it.", thread=k[1])

    def stop_turn(self, chat, thread):
        st = self.state.get((chat, thread)) or {}
        try:
            self.runner.get(self.uid_for(chat), st.get("worker", "")).send({"type": "cancel"})
        except KeyError:
            pass

    def worker(self, chat, uid):
        st = self.st(chat)
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
        after, st = w.seq, self.st(chat)
        thread = self.tl.thread or 0
        did = random.randint(1, 2 ** 31 - 1)
        live = chat > 0 and self.draft(chat, did, "")  # private chat: stream the answer as a draft ("Thinking…" first)
        mid = None if live else self.send(chat, "working…")["message_id"]
        w.tg_turn = True  # this turn's answer goes to this chat; other turns are mirrored
        st["busy"] = True
        w.prompt(text, images)
        steps, answer, t0, last, shown, idle = [], "", time.time(), 0, None, 0
        try:
            while True:
                evs = w.wait_events(after, timeout=2 if live else 20)
                idle = 0 if evs else idle + (0.1 if live else 1)
                if idle > 180:  # an hour without any event: the sandbox is gone or hung
                    return self.send(chat, "sandbox stopped responding — try again")
                for e in evs:
                    after = e["seq"]
                    t = e.get("type")
                    if t == "tool_start":
                        steps.append(f"{e['name']} {e.get('label', '')[:70]}".strip())
                        answer = ""
                    elif t == "tool_end" and not e.get("ok") and steps:
                        steps[-1] += "  ✗"
                    elif t == "text":
                        answer += e["text"]
                    elif t == "error":
                        answer += f"\n\n_{e['text']}_"
                    elif t == "turn_end":
                        st["session"] = w.session
                        if thread:
                            self.bind_topic(chat, thread, w.session, w.wid)
                            if not st.get("named") and w.title:
                                st["named"] = True
                                self.name_topic(chat, thread, w.title)
                        foot = f"_done · {e['time']:.1f}s · {e['in'] + e['out']:,} tokens_"
                        if mid:
                            self.edit(chat, mid, steps_md(steps) + foot, md=True)
                            foot = ""
                        self.send(chat, (steps_md(steps) if live else "") + (answer.strip() or tr("(no text)", self.ui_lang(chat)))
                                  + ("\n\n" + foot if foot else ""), md=True, reply_markup=ANSWER_KB)
                        return
                    elif t == "exit":
                        return self.send(chat, "sandbox stopped")
                now = time.time()
                if live:
                    view = progress_md(steps, answer, now - t0)
                    if (view != shown and now - last > 1) or now - last > 15:  # a draft fades after 30 s: refresh it
                        last, shown = now, view
                        live = self.draft(chat, did, view)
                        if not live:  # drafts stopped working mid-turn: fall back to one edited status message
                            mid = self.send(chat, "working…")["message_id"]
                elif now - last > 2.5:
                    last = now
                    self.edit(chat, mid, progress_md(steps, answer[-600:], now - t0), md=True)
        finally:
            w.tg_turn = False
            st["busy"] = False
