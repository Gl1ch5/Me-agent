"""Telegram bot: slash commands and inline buttons."""
import json, os, re, time
from pathlib import Path

from tg_i18n import tr

COMMANDS = [
    ("new", "start a new session"), ("sessions", "resume a session"), ("stop", "interrupt"), ("retry", "run the last request again"),
    ("undo", "revert the last turn's files"), ("goal", "work until done and verified: /goal <what>"), ("model", "switch model"),
    ("agent", "agent mode: code, design, plan…"), ("effort", "reasoning effort"), ("behavior", "switches: self-testing, todo, web, brief…"),
    ("ws", "choose the workspace for new tasks"), ("todo", "show the checklist"), ("cost", "spending: this session and total"), ("usage", "Me AI plan: 5-hour and weekly limits"), ("invite", "invite friends: your link and rewards"),
    ("compact", "summarize history"), ("rename", "rename this session"), ("export", "this session as a Markdown file"),
    ("search", "find text in all sessions"), ("sites", "published sites"), ("preview", "link to the site the agent built"),
    ("memory", "global notes: /memory, /memory forget <id>"), ("jobs", "background tasks: /jobs, /jobs run <id>"),
    ("searchkey", "web search keys: Tavily (free), Exa, Brave"), ("lang", "bot language: en / ru"), ("github", "GitHub connection status"), ("newrepo", "create a GitHub repo and start on it: /newrepo name"), ("perm", "where tools run (cloud = isolated sandbox)"),
    ("quiet", "stop mirroring other sessions into this chat"), ("help", "all commands"),
]
EFFORTS = ("none", "low", "medium", "high", "xhigh")
_BH = {}


def behavior_switches():
    """[(key, default, label, label_ru)] read from the client the server hands out, so the bot never lags behind /behavior."""
    CLIENT = Path(os.environ.get("ME_CLIENT", Path(__file__).resolve().parent.parent / "me.py"))  # as app.CLIENT
    mt = CLIENT.stat().st_mtime
    if _BH.get("mt") != mt:
        src = CLIENT.read_text("utf-8")
        i = src.index("BEHAVIOR = {")
        rows = re.findall(r'^    "(\w+)": \((True|False), "([^"]+)"', src[i:src.index("\n}\n", i)], re.M)
        ru = lambda label: (re.search(r'"' + re.escape(label) + r'": "([^"]+)"', src[src.index("RU = {") if "RU = {" in src else 0:]) or [None, label])[1]
        _BH.update(mt=mt, rows=[(k, d == "True", label, ru(label)) for k, d, label in rows])
    return _BH["rows"]


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
        lang = self.ui_lang(chat)
        self.send(chat, tr("Write a task and it runs in your cloud sandbox. Voice, photos and files work too.", lang) + "\n\n"
                  + "\n".join(f"/{c} — {tr(d, lang)}" for c, d in COMMANDS))

    def cmd_new(self, chat, uid, st, w, arg):
        cur = st.get("ws") or (w.wid if w else None) or self.st(chat, 0).get("ws_last")
        if arg:  # /new <title>: straight away, in this project
            return self.new_in(chat, uid, st, w, cur, arg)
        kb = [[{"text": ("● " if wid == cur else "📁 ") + name, "callback_data": "n:" + wid}] for wid, name in self.recent_ws(uid, cur)]
        kb += [[{"text": "+ new project", "callback_data": "n:+"}], [{"text": "⎇ GitHub repository…", "callback_data": "n:gh"}]]
        self.send(chat, "new session in:", reply_markup={"inline_keyboard": kb})

    def gh_pick(self, chat, uid, st):
        """/new → GitHub: sign in first (a button to GitHub's consent page), then the latest repositories as buttons."""
        import features
        if not (self.config(uid).get("secrets") or {}).get("GITHUB_TOKEN"):
            url = features.gh_login_url(uid)
            if not url:
                return self.send(chat, "GitHub is not connected. Add a token in the web account (cloud → GitHub).")
            return self.send(chat, "Sign in with GitHub, then send /new again.", reply_markup={"inline_keyboard": [[{"text": "sign in with GitHub", "url": url}]]})
        repos = features.github_repos("", {"id": uid})[:10]
        st["repos"] = [r["repo"] for r in repos]
        kb = [[{"text": ("🔒 " if r["private"] else "") + r["repo"], "callback_data": f"r:{i}"}] for i, r in enumerate(repos)]
        self.send(chat, "repository (or create one: /newrepo name):" if kb else "no repositories yet. Create one: /newrepo name",
                  reply_markup={"inline_keyboard": kb} if kb else None)

    def cmd_newrepo(self, chat, uid, st, w, arg):
        """/newrepo name: a new private GitHub repo, cloned into its own project, and a new session there."""
        import features
        if not arg.strip():
            return self.send(chat, "usage: /newrepo name")
        if not (self.config(uid).get("secrets") or {}).get("GITHUB_TOKEN"):
            return self.gh_pick(chat, uid, st)
        self.send(chat, f"creating {arg.strip()}…")
        try:
            r = features.github_new_repo(features.GhNew(name=arg.strip()), {"id": uid})
        except Exception as e:
            return self.send(chat, f"error: {getattr(e, 'detail', e)}")
        self.new_in(chat, uid, st, w, r["ws"], r["repo"])

    def recent_ws(self, uid, cur=None, n=5):
        """[(id, name)]: this project first, then the projects of the latest sessions, then the newest projects."""
        from cloud import me as client, ws_meta_path
        idx = client.jload(ws_meta_path(uid), {}) or {}
        used = [cur]
        for (content,) in self.db.execute("SELECT content FROM docs WHERE user_id=? AND deleted=0 AND key LIKE 'session/%' ORDER BY ts DESC LIMIT 300", (uid,)):
            m = re.search(r'"ws":\s*"([^"]+)"', content)
            if m:
                used.append(m.group(1))
        used += sorted(idx, key=lambda k: -idx[k].get("created", 0))
        return [(k, idx[k]["name"]) for k in dict.fromkeys(used) if k in idx][:n]

    def new_in(self, chat, uid, st, w, ws, title="new chat"):
        """A new session in the workspace `ws`: its own topic when topics are on, else here (a sandbox on that workspace)."""
        t = self.new_topic(chat, title, ws=ws)
        if t:  # topics: the new session gets its own topic, the old one stays as it was
            return self.send(chat, "new session — write here", thread=t)
        st.pop("session", None)
        if w and w.wid == ws and not w.busy:
            w.send({"type": "new"})
        else:
            st.pop("worker", None)
        st["ws"] = ws
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
              for m in cfg.get("models", [])[:20] + [{"name": "me-ai"}] * all(m.get("name") != "me-ai" for m in cfg.get("models", []))]  # built in
        self.send(chat, "model:" if kb else "no models — add one in the web account", reply_markup={"inline_keyboard": kb})

    def cmd_usage(self, chat, uid, st, w, arg):
        import plans
        self.send(chat, plans.usage_text(uid, self.ui_lang(chat) == "ru"))

    def cmd_invite(self, chat, uid, st, w, arg):
        import payments
        self.send(chat, payments.ref_text(payments.conn.execute("SELECT id, username FROM users WHERE id=?", (uid,)).fetchone(), self.ui_lang(chat) == "ru"))

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
        after = w.seq
        w.send({"type": "undo"})
        err = next((e for e in w.wait_events(after, timeout=3) if e.get("type") == "error"), None)
        self.send(chat, "error: " + err.get("text", "") if err else "↶ the last turn's files are restored")

    def cmd_compact(self, chat, uid, st, w, arg):
        if not w:
            return self.send(chat, "nothing to compact yet")
        w.send({"type": "compact"})
        self.send(chat, "compacting history…")

    @staticmethod
    def recap(doc, n=6):
        """The last few requests and answers of a session, so a chat that opens it sees where it stands."""
        lines = []
        for m in doc.get("messages") or []:
            c = m.get("content")
            if isinstance(c, list):
                c = " ".join(x.get("text", "") for x in c if isinstance(x, dict))
            if m.get("role") not in ("user", "assistant") or not isinstance(c, str) or not c.strip() or m.get("_aux") or m.get("_hint"):
                continue
            c = " ".join(c.split())
            lines.append(("› " if m["role"] == "user" else "◂ ") + (c[:250] + "…" if len(c) > 250 else c))
        if not lines:
            return ""
        return f"«{(doc.get('title') or 'session')[:60]}» — earlier:\n" + "\n".join(lines[-n:]) + "\n\n"

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
        if not (url and url.startswith("http")):
            return self.send(chat, "preview is available once the project runs on the server")
        self.send(chat, f"preview: {url}/")
        self.send_shot(chat, f"{url}/", self.tl.thread or 0)

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

    def cmd_retry(self, chat, uid, st, w, arg):
        if not w:
            return self.send(chat, "nothing to retry yet")
        if w.busy:
            return self.send(chat, "still working — /stop first")
        after = w.seq
        w.send({"type": "retry"})  # the sandbox takes the request back (history + files) and returns its text
        end = time.time() + 20
        while time.time() < end:
            for e in w.wait_events(after, timeout=5):
                after = e["seq"]
                if e.get("type") == "retried":
                    return self.run(chat, uid, e["text"])
                if e.get("type") == "error":
                    return self.send(chat, "nothing to retry yet" if "nothing" in e.get("text", "") else "error: " + e.get("text", ""))
        self.send(chat, "nothing to retry yet")

    def _put_config(self, uid, cfg, st):
        """Save the account config (syncs to every device) and make this chat's sandbox pick it up now."""
        from app import put_doc  # late import to avoid a cycle
        put_doc(uid, "config", json.dumps(cfg, ensure_ascii=False, indent=1))
        try:
            self.runner.get(uid, st.get("worker", "")).send({"type": "sync"})
        except (KeyError, AttributeError):
            pass

    def _behavior_kb(self, uid, chat):
        on = (self.config(uid).get("settings") or {}).get("behavior") or {}
        ru = self.ui_lang(chat) == "ru"
        return {"inline_keyboard": [[{"text": ("✅ " if on.get(k, d) else "▫️ ") + (lr if ru else label), "callback_data": "b:" + k}]
                                    for k, d, label, lr in behavior_switches()]}

    def cmd_behavior(self, chat, uid, st, w, arg):
        self.send(chat, "behavior:", reply_markup=self._behavior_kb(uid, chat))

    def cmd_agent(self, chat, uid, st, w, arg):
        names = ["code"]
        for (content,) in self.db.execute("SELECT content FROM docs WHERE user_id=? AND deleted=0 AND key LIKE 'agent/%'", (uid,)):
            m = re.search(r"^name:\s*(\S.*)$", content.split("\n---", 1)[0], re.M)
            if m and m.group(1).strip() not in names:
                names.append(m.group(1).strip())
        cur = self.config(uid).get("agent") or "code"
        self.send(chat, "agent mode:", reply_markup={"inline_keyboard": [[{"text": ("● " if n == cur else "") + n, "callback_data": "g:" + n[:60]}] for n in names]})

    def cmd_effort(self, chat, uid, st, w, arg):
        cfg = self.config(uid)
        m = next((x for x in cfg.get("models", []) if x.get("name") == cfg.get("model")), {})
        cur = m.get("effort") or "none"
        self.send(chat, "reasoning effort:", reply_markup={"inline_keyboard": [[{"text": ("● " if e == cur else "") + e, "callback_data": "e:" + e} for e in EFFORTS]]})

    def _sid(self, st, w):
        return (w.session if w else None) or st.get("session")

    def cmd_rename(self, chat, uid, st, w, arg):
        if not arg:
            return self.send(chat, "usage: /rename <title>")
        sid = self._sid(st, w)
        doc = self._session_doc(uid, sid)
        if not doc:
            return self.send(chat, "no session yet")
        from app import put_doc
        doc["title"] = arg[:80]
        put_doc(uid, "session/" + sid, json.dumps(doc, ensure_ascii=False))
        if self.tl.thread:
            self.name_topic(chat, self.tl.thread, arg)
        self.send(chat, f"renamed → {arg[:80]}")

    def cmd_export(self, chat, uid, st, w, arg):
        doc = self._session_doc(uid, self._sid(st, w))
        if not doc.get("messages"):
            return self.send(chat, "no session yet")
        out = [f"# {doc.get('title') or 'chat'}\n"]
        for m in doc["messages"]:
            c = m.get("content")
            c = c if isinstance(c, str) else " ".join(x.get("text", "") for x in c or [] if x.get("type") == "text")
            if m.get("role") == "user" and not m.get("_aux") and not m.get("_hint"):
                out.append("## You\n\n" + c + "\n")
            elif m.get("role") == "assistant":
                if c:
                    out.append("## me\n\n" + c + "\n")
                out += [f"- `{tc['function']['name']}`" for tc in m.get("tool_calls") or []]
        name = re.sub(r"[^\w-]+", "-", doc.get("title") or "chat").strip("-")[:40] or "chat"
        self.send_file(chat, name + ".md", ("\n".join(out) + "\n").encode())

    def cmd_search(self, chat, uid, st, w, arg):
        if not arg:
            return self.send(chat, "usage: /search <text>")
        q, kb = arg.lower(), []
        for key, content in self.db.execute("SELECT key, content FROM docs WHERE user_id=? AND deleted=0 AND key LIKE 'session/%' ORDER BY ts DESC", (uid,)):
            if q in content.lower():
                try:
                    title = json.loads(content).get("title") or "(untitled)"
                except ValueError:
                    continue
                kb.append([{"text": title[:40], "callback_data": "s:" + key[8:]}])
                if len(kb) >= 8:
                    break
        self.send(chat, f"{len(kb)} found:" if kb else "nothing found", reply_markup={"inline_keyboard": kb})

    def cmd_sites(self, chat, uid, st, w, arg):
        from cloud import DATA
        root = DATA / "sites" / str(uid)
        names = sorted(d.name for d in root.iterdir() if d.is_dir() and not d.name.startswith(".")) if root.is_dir() else []
        if not names:
            return self.send(chat, "no sites yet — turn on publishing in /behavior and ask the agent to publish one")
        from app import PUBLIC
        user = self.users(uid)["username"]
        self.send(chat, "\n".join(f"{PUBLIC or 'https://me-agent.ru'}/s/{user}/{n}/" for n in names),
                  reply_markup={"inline_keyboard": [[{"text": "🗑 " + n, "callback_data": "x:" + n}] for n in names]})

    def cmd_searchkey(self, chat, uid, st, w, arg):
        from cloud import me as client
        cfg = self.config(uid)
        try:
            msg = client.search_key(cfg, arg)
        except ValueError as e:
            return self.send(chat, str(e))
        if arg:
            from app import put_doc
            put_doc(uid, "config", json.dumps(cfg, ensure_ascii=False, indent=1))
        self.send(chat, msg.split(" — ")[0] if any(client.search_keys(cfg, a) for a in client.SEARCH_APIS) else client.SEARCH_ASK)

    def ask_search_key(self, chat, uid):
        """Once per account in Telegram: no web search key yet → ask for a free Tavily one."""
        from cloud import me as client
        cfg = self.config(uid)
        sett = cfg.get("settings") or {}
        if not cfg or "tg" in sett.get("search_asked", []) or any(client.search_keys(cfg, a) for a in client.SEARCH_APIS):
            return
        sett["search_asked"] = sorted(set(sett.get("search_asked", [])) | {"tg"})
        cfg["settings"] = sett
        from app import put_doc
        put_doc(uid, "config", json.dumps(cfg, ensure_ascii=False, indent=1))
        self.send(chat, client.SEARCH_ASK)

    def cmd_lang(self, chat, uid, st, w, arg):
        self.send(chat, "bot language:", reply_markup={"inline_keyboard": [[{"text": "Русский", "callback_data": "l:ru"},
                                                                             {"text": "English", "callback_data": "l:en"}]]})

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
                    return self.send(chat, self.recap(doc) + "resumed — continue here", thread=t)
            st["session"] = val
            st.pop("worker", None)
            if doc.get("ws"):
                st["ws"] = doc["ws"]
            self.send(chat, self.recap(doc) + "resumed — continue writing")
        elif kind == "n":  # /new: the project was picked
            self.tl.thread = q["message"].get("message_thread_id") if q["message"].get("is_topic_message") else 0
            st = self.st(chat)
            if val == "gh":
                return self.gh_pick(chat, uid, st)
            if val == "+":
                from cloud import ws_create
                val = ws_create(uid, "project " + time.strftime("%d.%m %H:%M"))["id"]
            try:
                w = self.runner.get(uid, st.get("worker", "")) if self.runner else None
            except KeyError:
                w = None
            self.new_in(chat, uid, st, w, val)
        elif kind == "r":  # /new → GitHub: the repository was picked; the agent works on it through branches and PRs
            self.tl.thread = q["message"].get("message_thread_id") if q["message"].get("is_topic_message") else 0
            st = self.st(chat)
            repos = st.get("repos") or []
            if not val.isdigit() or int(val) >= len(repos):
                return self.send(chat, "send /new again")
            import features
            self.send(chat, f"cloning {repos[int(val)]}…")
            try:
                r = features.repo_ws(uid, repos[int(val)])
            except Exception as e:
                return self.send(chat, f"error: {getattr(e, 'detail', e)}")
            try:
                w = self.runner.get(uid, st.get("worker", "")) if self.runner else None
            except KeyError:
                w = None
            self.new_in(chat, uid, st, w, r["ws"], r["repo"])
        elif kind == "w":
            st["ws"] = val
            st.pop("worker", None)
            st.pop("session", None)
            self.send(chat, f"workspace set: {val}")
        elif kind == "m":
            cfg = self.config(uid)
            cfg["model"] = val
            self._put_config(uid, cfg, st)
            try:
                self.runner.get(uid, st.get("worker", "")).send({"type": "model", "name": val})  # the running chat switches too
            except KeyError:
                pass
            self.send(chat, f"model → {val}")
        elif kind == "c":  # the buttons under an answer
            return self.command(chat, uid, "/" + val)
        elif kind == "b":
            cfg = self.config(uid)
            sw = {k: d for k, d, _, _ in behavior_switches()}
            if val in sw:
                bh = cfg.setdefault("settings", {}).setdefault("behavior", {})
                bh[val] = not bh.get(val, sw[val])
                self._put_config(uid, cfg, st)
                self.call("editMessageReplyMarkup", chat_id=chat, message_id=q["message"]["message_id"], reply_markup=self._behavior_kb(uid, chat))
        elif kind == "g":
            cfg = self.config(uid)
            cfg["agent"] = val
            self._put_config(uid, cfg, st)
            self.send(chat, f"agent → {val}")
        elif kind == "e" and val in EFFORTS:
            cfg = self.config(uid)
            m = next((x for x in cfg.get("models", []) if x.get("name") == cfg.get("model")), None)
            if m is not None:
                m["effort"] = "" if val == "none" else val
                self._put_config(uid, cfg, st)
            self.send(chat, f"effort → {val}")
        elif kind == "l" and val in ("ru", "en"):
            cfg = self.config(uid)
            cfg.setdefault("settings", {})["lang"] = val
            self._put_config(uid, cfg, st)
            self.tl.lang = val
            self.send(chat, "language: Russian" if val == "ru" else "language: English")
        elif kind == "x":  # delete a site: ask first, the link stops working
            self.send(chat, f"delete site {val}?" if self.ui_lang(chat) != "ru" else f"удалить сайт {val}?",
                      reply_markup={"inline_keyboard": [[{"text": "🗑 " + ("delete" if self.ui_lang(chat) != "ru" else "удалить"), "callback_data": "X:" + val}]]})
        elif kind == "X":
            import shutil
            from cloud import DATA
            d = DATA / "sites" / str(uid) / val
            if re.fullmatch(r"[a-z0-9][a-z0-9-]{0,39}", val) and d.is_dir():
                shutil.rmtree(d)
            self.send(chat, f"site {val} deleted")
