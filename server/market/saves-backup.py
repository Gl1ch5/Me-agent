"""
name: saves-backup
description: snapshot a game's save or config folder before a modded launch, list snapshots, and restore one (the current state is saved first)
icon: ⧉
color: yellow
---
Run backup before the first modded launch and before every risky step. Snapshots go to ~/.me/backups/<name>-<time>. Restore asks for confirmation and first snapshots what it is about to overwrite, so a restore can itself be undone.
Derived from universal-modder (MIT, Copyright (c) 2026 Rehan and universal-modder contributors): https://github.com/rehan-remade/universal-modder
"""
import shutil, time
from pathlib import Path


def _root(ctx):
    d = Path(ctx.env.get("ME_HOME") or Path.home() / ".me") / "backups"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _size(p):
    return sum(f.stat().st_size for f in Path(p).rglob("*") if f.is_file())


@action
def backup(ctx, path, name="saves"):
    "Copy a save or config folder into a timestamped snapshot"
    src = Path(path).expanduser()
    if not src.is_dir():
        raise ValueError(f"not a folder: {src}")
    dst = _root(ctx) / f"{name}-{time.strftime('%Y%m%d-%H%M%S')}"
    ctx.status(f"copying {_size(src) >> 20} MB")
    shutil.copytree(src, dst)
    ctx.log(f"snapshot: {dst}")
    return str(dst)


@action
def list_backups(ctx, name=""):
    "List snapshots, newest first"
    rows = []
    for d in sorted(_root(ctx).iterdir(), reverse=True):
        if d.is_dir() and (not name or d.name.startswith(name + "-")):
            rows.append({"snapshot": d.name, "mb": round(_size(d) / 1e6, 1), "path": str(d)})
    ctx.table(rows, title="save snapshots")
    return rows


@action(confirm=True)
def restore(ctx, snapshot, dest):
    "Restore a snapshot over a folder; the current contents are snapshotted first"
    src = Path(snapshot).expanduser()
    if not src.is_dir():
        src = _root(ctx) / snapshot
    if not src.is_dir():
        raise ValueError(f"no such snapshot: {snapshot}")
    target = Path(dest).expanduser()
    if target.exists():
        safety = _root(ctx) / f"before-restore-{time.strftime('%Y%m%d-%H%M%S')}"
        shutil.copytree(target, safety)
        ctx.log(f"current state saved to {safety}")
        shutil.rmtree(target)
    shutil.copytree(src, target)
    return f"restored {src.name} to {target}"
