"""
name: mod-lint
description: lint a mod folder before you share it: leaked keys, .env files, game files copied from an install, decompiler leftovers, absolute paths, missing README and license
icon: ✔
color: green
---
Run check before every upload, release or post. Pass the mod folder; pass the game's install folder as `game` to catch files that are byte-identical to the game (the hard FAIL). Fix every FAIL. Decide each WARN deliberately. Keys are masked in the output: rotate any key that shows up.
Derived from universal-modder (MIT, Copyright (c) 2026 Rehan and universal-modder contributors): https://github.com/rehan-remade/universal-modder
"""
import hashlib, os, re
from pathlib import Path

SECRETS = [
    ("fal key", re.compile(r"FAL_KEY\s*[=:]\s*['\"]?[A-Za-z0-9:_\-]{20,}")),
    ("sk- style API key", re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}")),
    ("GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}")),
    ("AWS access key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("private key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
]
DECOMP = re.compile(r"\b(?:FUN|DAT|sub|LAB)_[0-9a-fA-F]{4,}\b|Decompiled with|ILSpy|Ghidra")
USER_PATH = re.compile(r"[A-Za-z]:\\\\?Users\\\\?[A-Za-z0-9_.\- ]+|/home/[a-z0-9_.\-]+/|/Users/[A-Za-z0-9_.\- ]+/")
TEXT_EXT = {".cs", ".py", ".lua", ".json", ".txt", ".md", ".cfg", ".ini", ".xml", ".hjson", ".js", ".ts", ".java", ".toml",
            ".yaml", ".yml", ".c", ".cpp", ".h", ".gd", ".sh", ".bat", ".ps1", ".csproj", ".props", ".targets"}
GAME_FORMATS = {".pak", ".ucas", ".utoc", ".pck", ".xnb", ".bundle", ".assets", ".resource", ".resS", ".dat", ".drs", ".bsa",
                ".ba2", ".vpk", ".wad", ".dll", ".exe"}
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".deps", "bin", "obj"}
BIG = 50 << 20


def _walk(root):
    for p in root.rglob("*"):
        if p.is_file() and not (set(p.relative_to(root).parts[:-1]) & SKIP_DIRS):
            yield p


def _sha(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _mask(s):
    return s[:6] + "…" if len(s) > 8 else "…"


@action
def check(ctx, folder=".", game=""):
    "Lint a mod folder; game = the game's install folder (enables the byte-identical check)"
    root = Path(folder).expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"not a folder: {root}")
    files = list(_walk(root))
    rows = []

    def add(level, rule, p, detail=""):
        rows.append({"level": level, "rule": rule, "file": str(p.relative_to(root)), "detail": detail})

    ctx.status(f"linting {len(files)} files")
    for p in files:
        rel = p.relative_to(root)
        if p.name == ".env" or p.name.startswith(".env."):
            add("FAIL", ".env file", p, "secrets live here; remove it from the mod")
        if p.suffix.lower() in TEXT_EXT and p.stat().st_size < 2 << 20:
            text = p.read_text("utf-8", "replace")
            for n, line in enumerate(text.splitlines(), 1):
                for name, rx in SECRETS:
                    m = rx.search(line)
                    if m:
                        add("FAIL", f"leaked {name}", p, f"line {n}: {_mask(m.group(0))}")
                if DECOMP.search(line):
                    add("WARN", "decompiler fingerprint", p, f"line {n}")
                if USER_PATH.search(line):
                    add("WARN", "absolute user path", p, f"line {n}")
        if p.stat().st_size > BIG:
            add("WARN", "large file", p, f"{p.stat().st_size >> 20} MB")

    identical = set()
    if game:
        gdir = Path(game).expanduser()
        if not gdir.is_dir():
            raise ValueError(f"game folder not found: {gdir}")
        sizes = {}
        for p in files:
            sizes.setdefault(p.stat().st_size, []).append(p)
        hashes = {}
        ctx.status("comparing with the game install")
        for g in gdir.rglob("*"):
            try:
                if not g.is_file() or g.stat().st_size not in sizes:
                    continue
            except OSError:
                continue
            gh = _sha(g)
            for p in sizes[g.stat().st_size]:
                if p not in hashes:
                    hashes[p] = _sha(p)
                if hashes[p] == gh:
                    identical.add(p)
                    add("FAIL", "byte-identical to a game file", p, f"same as {g.name}")
    for p in files:
        if p.suffix.lower() in GAME_FORMATS and p not in identical:
            add("WARN", "game-format file", p, "make sure it is your own file, not an extracted asset")

    names = [p.name.lower() for p in files if p.parent == root]
    if not any(n.startswith("readme") for n in names):
        add("WARN", "no README", root, "add install steps, requirements, credits, AI disclosure")
    if not any(n.startswith(("license", "licence")) for n in names):
        add("WARN", "no license", root, "pick a license for your code (MIT is common)")

    fails = sum(r["level"] == "FAIL" for r in rows)
    warns = sum(r["level"] == "WARN" for r in rows)
    ctx.table(rows or [{"level": "OK", "rule": "clean", "file": "", "detail": ""}], title=f"mod-lint · {root.name}: {fails} fail, {warns} warn")
    return {"folder": str(root), "files": len(files), "fail": fails, "warn": warns, "clean": fails == 0 and warns == 0}
