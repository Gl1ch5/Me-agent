"""Per-account debug log: everything one traced account does, as JSON lines, for reading back when hunting bugs.
Off unless ME_TRACE lists usernames or user ids (comma separated). Files: $ME_DATA/logs/<uid>.jsonl, rotated at 5 MB to .1.
Only short excerpts are kept (prompts, tool args and outputs are cut), never tokens or keys."""
import json, os, re, threading, time, traceback
from pathlib import Path

DIR = Path(os.environ.get("ME_DATA", "/var/lib/me-agent")) / "logs"
WANT = {x.strip().lower() for x in os.environ.get("ME_TRACE", "").split(",") if x.strip()}
MAX = 5 * 1024 * 1024
_lock = threading.Lock()
_on = {}  # uid -> True once seen with a traced username
SECRET = re.compile(r"(tvly-|sk-|BSA|Bearer\s+)[\w-]{6,}|\b[0-9]{8,10}:[\w-]{30,}")


def on(uid, username=None):
    if not WANT or uid is None:
        return False
    if username and username.lower() in WANT:
        _on[uid] = True
    return _on.get(uid) or str(uid) in WANT


def cut(v, n=300):
    s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, default=str)
    s = SECRET.sub(lambda m: m.group(0)[:6] + "…", s)
    return s if len(s) <= n else s[:n] + f"…(+{len(s) - n})"


def log(uid, kind, **data):
    if not on(uid):
        return
    rec = {"t": time.strftime("%Y-%m-%d %H:%M:%S"), "k": kind, **data}
    line = json.dumps(rec, ensure_ascii=False, default=str) + "\n"
    with _lock:
        try:
            DIR.mkdir(parents=True, exist_ok=True)
            p = DIR / f"{uid}.jsonl"
            if p.exists() and p.stat().st_size > MAX:
                p.replace(p.with_suffix(".jsonl.1"))
            with open(p, "a", encoding="utf-8") as f:
                f.write(line)
        except OSError:
            pass


def error(uid, kind, exc=None, **data):
    tb = traceback.format_exc() if exc is not None else ""
    log(uid, kind, err=cut(str(exc or ""), 500), tb=tb[-2000:] if tb.strip() != "NoneType: None" else "", **data)


# agent events worth keeping; streamed text/reasoning deltas are skipped (the turn's final text is in turn_end/session docs)
def event(uid, wid, worker, e):
    t = e.get("type")
    w = {"ws": wid, "w": worker}
    if t == "tool_start":
        log(uid, "tool", name=e.get("name"), args=cut(e.get("args"), 400), **w)
    elif t == "tool_end":
        out = str(e.get("output") or "")
        if not e.get("ok") or re.search(r"error|traceback|not found|failed|denied|timed out", out[:400], re.I):
            log(uid, "tool_fail", ok=e.get("ok"), time=round(e.get("time") or 0, 1), out=cut(out, 600), **w)
        elif (e.get("time") or 0) > 30:
            log(uid, "tool_slow", time=round(e["time"], 1), **w)
    elif t in ("error", "retry", "notice", "verify", "compacted", "goal"):
        log(uid, t, **w, **{k: cut(v) for k, v in e.items() if k not in ("type", "seq")})
    elif t == "log":  # stderr of the sandbox: tracebacks, warnings
        log(uid, "stderr", text=cut(e.get("text"), 500), **w)
    elif t in ("turn_start", "ready", "loaded", "exit"):
        log(uid, t, session=e.get("session"), **w)
    elif t == "turn_end":
        log(uid, t, **w, **{k: cut(v, 120) for k, v in e.items() if k not in ("type", "seq", "text", "messages")})
    elif t == "user":
        log(uid, "prompt", text=cut(e.get("text"), 300), **w)
