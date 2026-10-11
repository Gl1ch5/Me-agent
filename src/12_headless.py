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
        elif t == "turn_end":
            sys.stderr.write(f"\n· {dur(e['time'])} · ↑{human(e['in'])} ↓{human(e['out'])}\n")

    def confirm(name, a):
        if yolo:
            return True
        if not sys.stdin.isatty():
            return False
        sys.stderr.write(f"allow {name}: {json.dumps(a, ensure_ascii=False)[:200]} [y/N] ")
        return input().strip().lower().startswith("y")

    ag = Agent(store, emit, confirm, lambda q: input(q + " "))
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

    ag = Agent(store, emit_sync, (lambda n, a: True) if yolo else (lambda n, a: bool(wait("confirm", name=n, args=a))),
               lambda q: wait("ask", text=q) or "")
    if os.environ.get("ME_WS_ID"):
        ag.session["ws"] = os.environ["ME_WS_ID"]
    emit({"type": "ready", "version": VERSION, "session": ag.session["id"]})
    for line in sys.stdin:
        try:
            cmd = json.loads(line)
        except ValueError:
            continue
        t = cmd.get("type")
        if t == "prompt" and not ag.busy:
            threading.Thread(target=ag.run, args=(cmd["text"], cmd.get("images", ())), daemon=True).start()
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
            ag.session = new_session()
            if os.environ.get("ME_WS_ID"):
                ag.session["ws"] = os.environ["ME_WS_ID"]
            emit({"type": "loaded", "ok": True, "session": ag.session["id"]})
        elif t == "sync":
            cloud.sync()
            store.config = jload(store.cfg_path) or store.config
            emit({"type": "synced"})
