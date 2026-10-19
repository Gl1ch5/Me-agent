"""me desktop: the local agent + web UI in a native window (pywebview).
Built into a single .exe by .github/workflows/release.yml (PyInstaller)."""
import os, socket, sys, threading, time
from pathlib import Path

import webview  # pywebview

if sys.stdout is None or sys.stderr is None:  # PyInstaller --windowed: no console, and the first print() would kill the server thread
    _log = open(Path.home() / ".me-desktop.log", "a", encoding="utf-8", buffering=1)
    sys.stdout = sys.stdout or _log
    sys.stderr = sys.stderr or _log

sys.path.insert(0, str(Path(__file__).resolve().parent))
import me  # noqa: E402  (me.py is copied next to this file at build time)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class Api:
    """Native helpers the web UI calls as window.pywebview.api.* (only inside the desktop app)."""

    def pick_folder(self, start=""):
        r = webview.windows[0].create_file_dialog(webview.FOLDER_DIALOG, directory=start or os.getcwd())
        return (r[0] if isinstance(r, (list, tuple)) else r) if r else ""

    def open_folder(self, path=""):
        p = path or os.getcwd()
        if sys.platform == "win32":
            os.startfile(p)  # noqa: S606 — opens Explorer on the project folder
        else:
            import subprocess
            subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", p])
        return True


def single_instance():
    """A second launch just focuses nothing and exits: two desktop apps would fight over the same sessions."""
    try:
        s = socket.socket()
        s.bind(("127.0.0.1", 47911))
        s.listen(1)
        return s
    except OSError:
        return None


def main():
    lock = single_instance()
    if lock is None:
        return
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
    webview.create_window("me", f"http://127.0.0.1:{port}/", width=1200, height=820, min_size=(380, 500), background_color="#0b0c10",
                          js_api=Api(), text_select=True)
    webview.start(private_mode=False)  # keep localStorage (theme, language) between launches
    me.jsave(me.HOME / "desktop.json", {"cwd": os.getcwd()})


if __name__ == "__main__":
    main()
