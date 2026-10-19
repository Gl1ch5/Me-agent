# ───────────────────────────── headless / rpc ─────────────────────────────


def run_headless(store, prompt, yolo):
    def emit(e):
        t = e["type"]
        if t == "text":
            sys.stdout.write(e["text"])
            sys.stdout.flush()
        elif t == "tool_start":
            sys.stderr.write(f"\n· {e['name']} {e['label'][:100]}\n")
        elif t == "tool_end" and not e["ok"]:
            sys.stderr.write(f"  ✗ {e['output'][:200]}\n")
        elif t == "error":
            sys.stderr.write(f"\nerror: {e['text']}\n")
        elif t == "retry":
            sys.stderr.write(f"\n↻ {e['error'][:160]} — retrying in {e['delay']:.0f}s\n")
        elif t == "turn_end":
            sys.stderr.write(f"\n· {dur(e['time'])} · ↑{human(e['in'])} ↓{human(e['out'])}\n")

    def confirm(name, a):
        if yolo:
            return True
        if not sys.stdin.isatty():
            return False
        sys.stderr.write(f"allow {name}: {json.dumps(a, ensure_ascii=False)[:200]} [y/N/a=always] ")
        r = input().strip().lower()
        return "always" if r.startswith("a") else r.startswith("y")

    def ask(q, options=None):
        if not sys.stdin.isatty():
            return ""
        return input(q + (f" [{' / '.join(options)}]" if options else "") + " ")
    ag = Agent(store, emit, confirm, ask)
    if prompt.startswith("/goal "):
        g = ag.goal(prompt[6:].strip().strip('"'))
        sys.stderr.write(f"\ngoal {g['status']}: {g['reason']}\n")
        print()
        return 0 if g["status"] == "done" else 2
    ag.run(prompt)
    print()


def run_rpc(store):
    """JSON-lines over stdio. in: {type:prompt,text} {type:cancel} {type:confirm,id,ok} {type:answer,id,text} {type:load,id}
    out: agent events + {type:confirm,id,name,args} {type:ask,id,text}"""
    out_lock = threading.Lock()
    waits = {}

    def emit(e):
        with out_lock:
            sys.stdout.write(json.dumps(e, ensure_ascii=False) + "\n")
            sys.stdout.flush()

    def wait(kind, **kw):
        rid = uuid.uuid4().hex[:8]
        ev = threading.Event()
        waits[rid] = [ev, None]
        emit(dict(type=kind, id=rid, **kw))
        ev.wait()
        return waits.pop(rid)[1]

    yolo = bool(os.environ.get("ME_YOLO"))  # cloud runner: sandboxed, so tools run without asking
    cloud = Cloud(store)

    def emit_sync(e):
        emit(e)
        if e["type"] == "turn_end" and cloud.online:
            threading.Thread(target=cloud.sync, daemon=True).start()

    ag = Agent(store, emit_sync, (lambda n, a: True) if yolo else (lambda n, a: (lambda r: "always" if r == "always" else bool(r))(wait("confirm", name=n, args=a))),
               lambda q, options=None: wait("ask", text=q, options=options) or "")
    if os.environ.get("ME_WS_ID"):
        ag.session["ws"] = os.environ["ME_WS_ID"]
    store.on_change = cloud.autosync  # every checkpoint reaches other devices within ~1s

    def remote(n):
        store.config.update(jload(store.cfg_path) or {})
        if ag.refresh_from_disk(os.getcwd()):
            emit({"type": "reload", "session": ag.session["id"]})
    if cloud.online:
        cloud.watch(remote)
    emit({"type": "ready", "version": VERSION, "session": ag.session["id"]})
    src = sys.stdin
    if os.environ.get("ME_RPC_IN"):  # cloud: a FIFO opened read-write never hits EOF, so a server restart does not end us
        src = io.TextIOWrapper(io.FileIO(os.open(os.environ["ME_RPC_IN"], os.O_RDWR), "r"), encoding="utf-8")
    for line in src:
        try:
            cmd = json.loads(line)
        except ValueError:
            continue
        t = cmd.get("type")
        if t == "prompt" and not ag.busy:
            threading.Thread(target=ag.run, args=(cmd["text"], cmd.get("images", ())), daemon=True).start()
        elif t == "goal" and not ag.busy:
            threading.Thread(target=ag.goal, args=(cmd["text"],), daemon=True).start()
        elif t == "undo":
            r = ag.undo()
            if not r["ok"]:
                emit({"type": "error", "text": r["text"]})
        elif t == "cancel":
            ag.cancel.set()
        elif t in ("confirm", "answer") and cmd.get("id") in waits:
            waits[cmd["id"]][1] = cmd.get("ok") if t == "confirm" else cmd.get("text")
            waits[cmd["id"]][0].set()
        elif t == "load":
            if not ag.load(cmd["id"]) and cloud.online:
                cloud.sync()
                ag.load(cmd["id"])
            ag.relocate(os.getcwd())
            if os.environ.get("ME_WS_ID"):
                ag.session["ws"] = os.environ["ME_WS_ID"]
            emit({"type": "loaded", "ok": True, "session": ag.session["id"], "title": ag.session.get("title", "")})
        elif t == "new":
            if ag.busy:
                emit({"type": "error", "text": "busy — stop the current turn first"})
                continue
            ag.session = new_session()
            ag.ctx_used, ag.snap = 0, {}
            if os.environ.get("ME_WS_ID"):
                ag.session["ws"] = os.environ["ME_WS_ID"]
            emit({"type": "loaded", "ok": True, "session": ag.session["id"]})
        elif t == "compact" and not ag.busy:
            threading.Thread(target=ag.compact, daemon=True).start()
        elif t == "sync":
            cloud.sync()
            store.config = jload(store.cfg_path) or store.config
            emit({"type": "synced"})
