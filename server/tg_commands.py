"""Telegram bot: slash commands and inline buttons."""
import json, time

COMMANDS = [
    ("new", "start a new session"), ("sessions", "resume a session"), ("ws", "choose the workspace for new tasks"),
    ("model", "switch model"), ("goal", "work until done and verified: /goal <what>"), ("stop", "interrupt"),
    ("undo", "revert the last turn's files"), ("todo", "show the checklist"), ("compact", "summarize history"),
    ("cost", "spending: this session and total"), ("memory", "global notes: /memory, /memory forget <id>"),
    ("jobs", "background tasks: /jobs, /jobs run <id>"), ("preview", "link to the site the agent built"),
    ("perm", "where tools run (cloud = isolated sandbox)"), ("github", "GitHub connection status"),
    ("quiet", "stop mirroring other sessions into this chat"), ("help", "all commands"),
]


class TgCommands:
    # ─── commands ───
    def command(self, chat, uid, text):
        parts = text.split()
        cmd = parts[0].split("@")[0].lower()
        arg = text[len(parts[0]):].strip()
        st = self.st(chat)
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
        t = self.new_topic(chat, arg or "new chat", ws=st.get("ws"))
        if t:  # topics: the new session gets its own topic, the old one stays as it was
            return self.send(chat, "new session — write here", thread=t)
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
        st = self.st(chat)
        if kind == "a":  # answer to an agent question from its buttons
            k = (chat, self.tl.thread or 0)
            a = self.asks.get(k)
            if a and val.isdigit() and int(val) < len(a["options"]):
                self.answer(k, a["options"][int(val)])
                return self.send(chat, "✓ " + a["options"][int(val)])
            return self.send(chat, "that question is no longer open")
        if kind == "s":
            row = self.db.execute("SELECT content FROM docs WHERE user_id=? AND key=? AND deleted=0", (uid, "session/" + val)).fetchone()
            if not row:
                return self.send(chat, "that session no longer exists")
            doc = self._session_doc(uid, val)
            if self.topics and chat > 0:  # topics: open the session's topic (one per session)
                t = self.topic_of(chat, val) or self.new_topic(chat, doc.get("title") or "session", val, doc.get("ws"))
                if t:
                    return self.send(chat, "resumed — continue here", thread=t)
            st["session"] = val
            st.pop("worker", None)
            if doc.get("ws"):
                st["ws"] = doc["ws"]
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
