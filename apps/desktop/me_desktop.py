"""me desktop: the local agent + web UI in a native window (pywebview).
Built into a single .exe by .github/workflows/release.yml (PyInstaller)."""
import os, socket, sys, threading, time
from pathlib import Path

import webview  # pywebview

sys.path.insert(0, str(Path(__file__).resolve().parent))
import me  # noqa: E402  (me.py is copied next to this file at build time)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def main():
    work = Path(me.jload(me.HOME / "desktop.json", {}).get("cwd") or (Path.home() / "me-workspaces"))
    work.mkdir(parents=True, exist_ok=True)
    os.chdir(work)
    store = me.Store()
    if store.auth.get("token"):
        try:
            me.Cloud(store).sync()
            store.config = me.jload(store.cfg_path) or store.config
        except Exception:
            pass
    me.PLUGINS.load(store)
    port = free_port()
    threading.Thread(target=me.run_web, args=(store, "127.0.0.1", port, False), daemon=True).start()
    for _ in range(50):
        try:
            socket.create_connection(("127.0.0.1", port), 0.1).close()
            break
        except OSError:
            time.sleep(0.1)
    webview.create_window("me", f"http://127.0.0.1:{port}/", width=1200, height=820, min_size=(380, 500), background_color="#0b0c10")
    webview.start()
    me.jsave(me.HOME / "desktop.json", {"cwd": os.getcwd()})


if __name__ == "__main__":
    main()
