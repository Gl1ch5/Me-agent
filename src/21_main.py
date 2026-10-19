# ───────────────────────────── main ─────────────────────────────


def main():
    args = sys.argv[1:]
    store = Store()
    if args and args[0] in ("-p", "--print", "--rpc", "web", "--web"):
        # one-shot and server modes need the account's config before the first turn; the TUI syncs in the background instead
        # (a blocking sync here kept `me` from starting for up to 30 s whenever the server was slow or unreachable)
        if store.auth.get("token"):
            Cloud(store).sync()
            store.config = jload(store.cfg_path) or store.config
    if not (args and args[0] in ("-v", "--version", "-h", "--help", "--sync")):
        PLUGINS.load(store)
    if args and args[0] in ("-v", "--version"):
        return print(f"me {VERSION}")
    if args and args[0] == "studio":
        return run_studio(store, args[1:])
    if args and args[0] in ("-h", "--help"):
        return print(f"me {VERSION} — minimal cloud-synced agent\n  me              TUI\n  me -p \"task\"     run once (add -y to auto-approve)\n"
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
        yolo = "-y" in args
        rest = [a for a in args[1:] if a != "-y"]
        prompt = " ".join(rest) or sys.stdin.read()
        c = Cloud(store)
        run_headless(store, prompt, yolo)
        if store.auth.get("token"):
            c.sync()
        return
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        if WIN and (os.environ.get("TERM_PROGRAM") == "mintty" or os.environ.get("MSYSTEM")):
            return print("me: Git Bash's window (mintty) is not a Windows console. Run `winpty me`, or open me in Windows Terminal / cmd.exe.")
        return print("me: not a terminal. use: me -p \"task\"")
    tui = TUI(store)
    PLUGINS.tui = tui
    tui.run()


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
