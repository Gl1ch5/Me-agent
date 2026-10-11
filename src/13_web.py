# ───────────────────────────── web ui server ─────────────────────────────

WEBUI = "<!-- built into me.py by build.py -->"
ICON_SVG = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512"><rect width="512" height="512" rx="112" fill="#0b0c10"/>'
            '<rect x="146" y="146" width="220" height="220" rx="56" fill="#8b7cff"/></svg>')
PWA_MANIFEST = {"name": "me", "short_name": "me", "description": "Your coding agent, everywhere", "start_url": "/app", "scope": "/",
                "display": "standalone", "background_color": "#0b0c10", "theme_color": "#0b0c10",
                "icons": [{"src": "/icon.svg", "sizes": "any", "type": "image/svg+xml", "purpose": "any maskable"}]}
SW_JS = """self.addEventListener('install', e => self.skipWaiting());
self.addEventListener('activate', e => e.waitUntil(self.clients.claim()));
self.addEventListener('fetch', () => {});
self.addEventListener('push', e => {
  let d = {}; try { d = e.data.json(); } catch (x) { d = {title: 'me', body: e.data ? e.data.text() : ''}; }
  e.waitUntil(self.registration.showNotification(d.title || 'me', {body: d.body || '', tag: d.tag || 'me', data: {url: d.url || '/app'},
    icon: '/icon.svg', badge: '/icon.svg', renotify: true}));
});
self.addEventListener('notificationclick', e => {
  e.notification.close();
  const url = e.notification.data && e.notification.data.url || '/app';
  e.waitUntil(clients.matchAll({type: 'window', includeUncontrolled: true}).then(ws => {
    for (const w of ws) if (w.url.includes('/app') || w.url.endsWith('/')) { w.focus(); return w.navigate(url); }
    return clients.openWindow(url);
  }));
});"""


class WebSession:
    """Agent + event log for the browser UI. Same endpoint shape as the cloud runner (/api/cloud/<id>/...)."""

    def __init__(self, store):
        import collections
        self.s = store
        self.cloud = Cloud(store)
        self.ws = Workspaces(store, self.cloud)
        self.events = collections.deque(maxlen=2000)
        self.seq = 0
        self.cond = threading.Condition()
        self.waits = {}
        self.agent = Agent(store, self.push, self.confirm, self.ask)
        store.on_change = self.cloud.autosync

    def push(self, e):
        with self.cond:
            self.seq += 1
            self.events.append(dict(e, seq=self.seq))
            self.cond.notify_all()
        if e["type"] == "turn_end" and self.cloud.online:
            def after():
                self.cloud.sync()
                if self.agent.session.get("ws"):
                    try:
                        self.ws.sync(self.agent.session["ws"])
                    except CloudError:
                        pass
            threading.Thread(target=after, daemon=True).start()

    def _wait(self, kind, **kw):
        rid = uuid.uuid4().hex[:8]
        ev = threading.Event()
        e = dict(type=kind, id=rid, **kw)
        self.waits[rid] = [ev, None, e]
        self.push(e)
        ev.wait()
        return self.waits.pop(rid)[1]

    def confirm(self, name, args):
        r = self._wait("confirm", name=name, args=args)
        return "always" if r == "always" else bool(r)

    def ask(self, q, options=None):
        return self._wait("ask", text=q, options=options) or ""

    def state(self):
        ses = self.agent.session
        return {"mode": "local", "user": self.s.auth.get("user", ""), "cwd": os.getcwd(), "after": self.seq, "ctx": self.agent.ctx_used or ses.get("ctx", 0),
                "config": {"model": self.s.config.get("model"), "models": self.s.config.get("models", [])},
                "session": ses, "ws": ses.get("ws"), "busy": self.agent.busy,
                "pending": [w[2] for w in list(self.waits.values())], "perm": perm_mode(self.s.config["settings"]),
                "day_cost": day_cost(self.s.home), "preview": "",
                "agent": self.agent.preset()["name"], "context": self.agent.context_files(),
                "agents": [{"name": n, "label": p.get("label", n), "description": p.get("description", "")} for n, p in presets(self.s).items()],
                "sessions": [{"id": x["id"], "title": x.get("title"), "updated": x.get("updated"), "cwd": x.get("cwd")} for x in self.s.sessions()[:100]]}

    def handle(self, method, path, body):
        a = self.agent
        if path == "/state":
            return self.state()
        if path.startswith("/events"):
            import urllib.parse as up
            after = int(up.parse_qs(up.urlparse(path).query).get("after", ["0"])[0])
            with self.cond:
                if self.seq <= after:
                    self.cond.wait(25)
                evs = [e for e in self.events if e["seq"] > after]
            return {"events": evs, "state": {"busy": a.busy}}
        if path in ("/prompt", "/goal"):
            if a.busy:
                raise ValueError("still working")
            self.push({"type": "user", "text": ("/goal " if path == "/goal" else "") + body["text"]})
            if not a.session.get("ws") and self.cloud.online:
                a.session["ws"] = self.ws.for_dir(os.getcwd())
            if path == "/goal":
                threading.Thread(target=a.goal, args=(body["text"],), daemon=True).start()
            else:
                threading.Thread(target=a.run, args=(body["text"], body.get("images") or ()), daemon=True).start()
            return {"ok": True}
        if path == "/undo":
            r = a.undo()
            if not r["ok"]:
                raise ValueError(r["text"])
            return r
        if path == "/upload":  # attachments from the browser land in the project folder
            d = Path(os.getcwd()) / "uploads"
            d.mkdir(exist_ok=True)
            name = re.sub(r"[^\w.-]+", "_", body["name"])[:80] or "file"
            (d / name).write_bytes(base64.b64decode(body["b64"]))
            return {"path": f"uploads/{name}"}
        if path == "/perm":
            st = self.s.config["settings"]
            st["perm"], st["yolo"] = body["mode"], False
            self.s.save()
            return {"ok": True}
        if path == "/cancel":
            a.cancel.set()
            return {"ok": True}
        if path == "/new":
            a.session = new_session()
            a.ctx_used = 0
            return {"ok": True}
        if path == "/load":
            a.load(body["id"])
            wid = a.session.get("ws")
            local = self.ws.local_path(wid) if wid else None
            if local and os.path.isdir(local):
                try:
                    self.ws.sync(wid)
                except CloudError:
                    pass
                os.chdir(local)
                a.relocate(local)
            return self.state()
        if path == "/compact":
            if a.busy:
                raise ValueError("busy")
            threading.Thread(target=lambda: a.compact(reason="manual") or self.push({"type": "error", "text": "nothing to compact yet"}),
                             daemon=True).start()
            return {"ok": True}
        if path == "/agent":
            self.s.config["agent"] = body["name"]
            a.session["agent"] = body["name"]
            self.s.save()
            return {"ok": True}
        if path == "/themes":
            return {"current": self.s.config.get("theme") or "me-dark", "themes": [theme(self.s, n) for n in themes(self.s)]}
        if path == "/theme":
            self.s.config["theme"] = body["name"]
            self.s.save()
            return {"ok": True}
        if path == "/cwd":
            d = os.path.abspath(os.path.expanduser(body["path"]))
            if not os.path.isdir(d):
                raise ValueError(f"no such folder: {d}")
            os.chdir(d)
            a.session = new_session(d)
            a.ctx_used = 0
            return self.state()
        if path == "/model":
            self.s.config["model"] = body["name"]
            self.s.save()
            return {"ok": True}
        if path in ("/confirm", "/answer"):
            w = self.waits.get(body.get("id"))
            if w:
                w[1] = body.get("ok") if path == "/confirm" else body.get("text")
                w[0].set()
            return {"ok": True}
        raise KeyError(path)


def groq_transcribe(cfg, audio, ctype="audio/webm"):
    v = cfg.get("voice") or {}
    key = v.get("groq_key") or next((p.get("api_key") for p in cfg.get("providers", {}).values() if "groq.com" in p.get("base_url", "")), "")
    if not key:
        raise ValueError("set a Groq API key (web account → Voice, or /voice key)")
    b = uuid.uuid4().hex
    ext = ctype.split("/")[-1].split(";")[0]
    parts = [f'--{b}\r\nContent-Disposition: form-data; name="model"\r\n\r\n{v.get("model") or "whisper-large-v3"}\r\n'.encode()]
    if v.get("language"):
        parts.append(f'--{b}\r\nContent-Disposition: form-data; name="language"\r\n\r\n{v["language"]}\r\n'.encode())
    parts.append(f'--{b}\r\nContent-Disposition: form-data; name="file"; filename="voice.{ext}"\r\n\r\n'.encode() + audio + b"\r\n")
    parts.append(f"--{b}--\r\n".encode())
    r = urllib.request.Request("https://api.groq.com/openai/v1/audio/transcriptions", data=b"".join(parts), method="POST",
                               headers={"Authorization": "Bearer " + key, "Content-Type": f"multipart/form-data; boundary={b}", "User-Agent": "me"})
    return json.loads(urllib.request.urlopen(r, timeout=60).read()).get("text", "").strip()


def run_web(store, host="127.0.0.1", port=8080, open_browser=True):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import secrets as _sec
    ws = WebSession(store)
    key = "" if host in ("127.0.0.1", "localhost") else _sec.token_urlsafe(16)

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, data, ctype="application/json"):
            raw = data if isinstance(data, bytes) else json.dumps(data, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(raw)

        def _authed(self):
            if not key:
                # local mode has no key: only a page served from this machine may talk to the agent
                # (blocks other sites POSTing from the browser and DNS rebinding via a foreign Host)
                host = self.headers.get("Host", "").rsplit(":", 1)[0].strip("[]")
                origin = self.headers.get("Origin")
                if host not in ("localhost", "127.0.0.1"):
                    return False
                return origin is None or origin in (f"http://localhost:{port}", f"http://127.0.0.1:{port}")
            return self.headers.get("Authorization", "") == "Bearer " + key

        def _route(self, method):
            path = self.path
            if method == "GET" and (path == "/" or path.startswith("/?")):
                return self._send(200, WEBUI.replace("__MODE__", "local").encode(), "text/html; charset=utf-8")
            if path == "/manifest.webmanifest":
                return self._send(200, json.dumps(dict(PWA_MANIFEST, start_url="/")).encode(), "application/manifest+json")
            if path == "/sw.js":
                return self._send(200, SW_JS.encode(), "text/javascript")
            if path == "/icon.svg":
                return self._send(200, ICON_SVG.encode(), "image/svg+xml")
            if not path.startswith("/api/agent"):
                return self._send(404, {"detail": "not found"})
            if not self._authed():
                return self._send(401, {"detail": "open the link printed in the terminal (it carries the access key)"})
            raw = self.rfile.read(int(self.headers.get("Content-Length") or 0)) if method == "POST" else b""
            sub = path[len("/api/agent"):]
            try:
                if sub == "/voice":
                    return self._send(200, {"text": groq_transcribe(store.config, raw, self.headers.get("Content-Type", "audio/webm"))})
                return self._send(200, ws.handle(method, sub, json.loads(raw or b"{}")))
            except KeyError:
                return self._send(404, {"detail": "not found"})
            except Exception as e:
                return self._send(400, {"detail": str(e)})

        def do_GET(self):
            self._route("GET")

        def do_POST(self):
            self._route("POST")

    srv = ThreadingHTTPServer((host, port), H)
    srv.daemon_threads = True
    url = f"http://{'localhost' if host in ('127.0.0.1', '0.0.0.0') else host}:{port}/" + (f"?k={key}" if key else "")
    print(f"me web · {url}\n  agent runs here, in {os.getcwd()} · ctrl+c to stop")
    if host == "0.0.0.0":
        print(f"  from other devices on your network use this machine's IP with ?k={key}")
    if open_browser:
        try:
            import webbrowser
            if os.environ.get("TERMUX_VERSION") and shutil.which("termux-open-url"):
                subprocess.Popen(["termux-open-url", url])
            else:
                webbrowser.open(url)
        except Exception:
            pass
    def remote(n):
        store.config.update(jload(store.cfg_path) or {})
        if ws.agent.refresh_from_disk(os.getcwd()):
            ws.push({"type": "reload"})
    if store.auth.get("token"):
        ws.cloud.watch(remote)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")
