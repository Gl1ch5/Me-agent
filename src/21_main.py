# ───────────────────────────── main ─────────────────────────────


def main():
    args, cont, yolo, want = parse_flags(sys.argv[1:])
    store = Store()
    if args and args[0] in ("-p", "--print", "--rpc", "web", "--web"):
        # one-shot and server modes need the account's config before the first turn; the TUI syncs in the background instead
        # (a blocking sync here kept `me` from starting for up to 30 s whenever the server was slow or unreachable)
        if store.auth.get("token"):
            Cloud(store).sync()
            store.config = jload(store.cfg_path) or store.config
    if not (args and args[0] in ("-v", "--version", "-h", "--help", "--sync")):
        PLUGINS.load(store)
    if args and args[0] in PLUGINS.clis:
        sys.exit(PLUGINS.clis[args[0]](store, args[1:]) or 0)
    if args and args[0] in ("-v", "--version"):
        return print(f"me {VERSION}")
    model = want and pick_model(store, want)
    if want and not model:
        return print(f"me: no model matches {want!r}. models: " + ", ".join(m["name"] for m in store.models()))
    if args and args[0] == "studio":
        return run_studio(store, args[1:])
    if args and args[0] in ("-h", "--help"):
        return print(f"me {VERSION} — minimal cloud-synced agent\n  me              TUI\n  me \"task\"       start with a first message\n  me -c           continue the last chat in this folder (-p too)\n  me -m NAME      use this model (-p too; part of a name is enough)\n"
                     f"  me -p \"task\"     run once (add -y to auto-approve; piped stdin is added to the task)\n"
                     f"  me web [port]   browser UI on localhost:8080 (--lan to open on your network)\n"
                     f"  me studio       desktop command center: parallel tasks in git worktrees, review, apply, commit (--browser, update)\n  me --rpc        JSON-lines agent over stdio (for web/telegram/IDE frontends)\n  me --sync       sync with cloud and exit\n"
                     f"  env: ME_HOME={HOME}  ME_SERVER={SERVER}")
    if args and args[0] in ("web", "--web"):
        port = next((int(a) for a in args[1:] if a.isdigit()), 8080)
        host = "0.0.0.0" if "--lan" in args else "127.0.0.1"
        return run_web(store, host, port, "--no-open" not in args)
    if args and args[0] == "--sync":
        c = Cloud(store)
        n = c.sync()
        return print(f"synced: {n}" if n >= 0 else f"sync failed: {c.last_error}")
    if args and args[0] == "--rpc":
        return run_rpc(store)
    if args and args[0] in ("-p", "--print"):
        prompt = " ".join(args[1:])
        if not prompt or stdin_ready():  # `cat log | me -p "why does it fail"`: the piped text comes with the task
            piped = sys.stdin.read().strip()
            prompt = f"{prompt}\n\n```\n{piped}\n```" if prompt and piped else prompt or piped
        c = Cloud(store)
        rc = run_headless(store, prompt, yolo, store.latest(os.getcwd()) if cont else None, model)
        if store.auth.get("token"):
            c.sync()
        if rc:
            sys.exit(rc)  # 2: /goal not reached, 143: stopped by SIGTERM
        return
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        if WIN and (os.environ.get("TERM_PROGRAM") == "mintty" or os.environ.get("MSYSTEM")):
            return print("me: Git Bash's window (mintty) is not a Windows console. Run `winpty me`, or open me in Windows Terminal / cmd.exe.")
        return print("me: not a terminal. use: me -p \"task\"")
    tui = TUI(store)
    tui.first = (cont, " ".join(a for a in args if not a.startswith("-")).strip(), model)  # me -c: continue here; me "task": start with it
    PLUGINS.tui = tui
    tui.run()


def parse_flags(args):
    """-c, -y and -m NAME anywhere; -p first, so `me -m x -y -p "task"` is `me -p "task" -m x -y`. -> (args, cont, yolo, model)"""
    cont = any(a in ("-c", "--continue") for a in args)
    yolo = any(a in ("-y", "--yes") for a in args)
    args = [a for a in args if a not in ("-c", "--continue", "-y", "--yes")]
    want = None
    for i, a in enumerate(args[:-1]):
        if a in ("-m", "--model"):
            want = args[i + 1]
            args = args[:i] + args[i + 2:]
            break
    for f in ("-p", "--print"):
        if f in args[1:]:
            args.remove(f)
            args.insert(0, f)
    return args, cont, yolo, want


def stdin_ready():
    """Piped or redirected input to read along with a prompt; an inherited stdin nobody writes to (a script's, CI's) must not hang -p."""
    if sys.stdin is None or sys.stdin.isatty():
        return False
    try:
        if stat.S_ISREG(os.fstat(sys.stdin.fileno()).st_mode):
            return True
        if WIN:
            return False
        import select
        return bool(select.select([sys.stdin], [], [], 1.0)[0])
    except (OSError, ValueError):
        return False


def pick_model(store, name):
    """-m: an exact model name, else the only one whose name or id contains it."""
    ms = store.models()
    hit = [m for m in ms if m["name"] == name] or [m for m in ms if name.lower() in (m["name"] + " " + m.get("id", "")).lower()]
    return hit[0]["name"] if len(hit) == 1 else None


def run_studio(store, args):
    """me studio: the desktop app (apps/studio/me_studio.py). Taken from a checkout next to me.py, else from the server into
    ~/.me/apps, refreshed on every start when the server has a newer one (offline: the copy already there)."""
    here = Path(os.path.abspath(__file__)).parent
    local = here / "apps" / "studio" / "me_studio.py"
    path = local if local.is_file() else HOME / "apps" / "me_studio.py"
    if path != local:
        try:
            src = Cloud(store).req("GET", "/studio.py", token=False, timeout=60 if not path.is_file() or "update" in args else 8)
            src = src.decode("utf-8") if isinstance(src, bytes) else src
            if "def main(" not in str(src):
                raise CloudError("bad download")
            if not path.is_file() or path.read_bytes() != str(src).encode("utf-8"):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(str(src).encode("utf-8"))
                print(f"me studio updated → {path}")
        except CloudError as e:
            if not path.is_file():
                return print(f"me studio: could not download it ({e}). Get it from the releases page: me-studio.exe, or apps/studio/me_studio.py")
    sys.modules.setdefault("me", sys.modules[__name__])  # the studio imports me: give it this very module
    import runpy
    sys.argv = [str(path)] + [a for a in args if a != "update"]
    runpy.run_path(str(path), run_name="__main__")


if __name__ == "__main__":
    main()
