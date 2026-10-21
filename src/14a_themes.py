# ───────────────────────────── themes ─────────────────────────────
# A theme is JSON: {"name", "label", "dark", "colors": {token: "#rrggbb"}}. Same tokens drive the TUI and the web UI.
# Installed themes live in ~/.me/themes/<name>.json (synced as theme/<name>.json); config["theme"] picks one.

THEME_TOKENS = ["bg", "bg2", "bg3", "line", "fg", "dim", "faint", "accent", "accent2", "ok", "err", "warn", "user", "code_bg", "sel"]
BUILTIN_THEMES = {
    "me-dark": {"name": "me-dark", "label": "me dark", "dark": True, "colors": {
        "bg": "#0e0f13", "bg2": "#111319", "bg3": "#171a23", "line": "#1f2230", "fg": "#d8dae3", "dim": "#6b6f80", "faint": "#3a3d4a",
        "accent": "#8b7cff", "accent2": "#4fd1c5", "ok": "#5ad18a", "err": "#ff6b7a", "warn": "#ffb454", "user": "#e7c66b",
        "code_bg": "#16181f", "sel": "#262a38"}},
    "me-light": {"name": "me-light", "label": "me light", "dark": False, "colors": {
        "bg": "#fafaf8", "bg2": "#f2f2ef", "bg3": "#e9e9e4", "line": "#e2e2dc", "fg": "#1d1e24", "dim": "#62657a", "faint": "#b9bac2",
        "accent": "#6c5ce7", "accent2": "#0f9d8e", "ok": "#1f9d55", "err": "#d63a4a", "warn": "#c77700", "user": "#a3750a",
        "code_bg": "#efefea", "sel": "#e4e2f7"}},
    "mono": {"name": "mono", "label": "mono", "dark": True, "colors": {
        "bg": "#000000", "bg2": "#0d0d0d", "bg3": "#161616", "line": "#262626", "fg": "#ededed", "dim": "#8a8a8a", "faint": "#3d3d3d",
        "accent": "#ffffff", "accent2": "#bdbdbd", "ok": "#d4d4d4", "err": "#ff5f5f", "warn": "#cfcfcf", "user": "#ffffff",
        "code_bg": "#0d0d0d", "sel": "#1f1f1f"}},
    "mono-light": {"name": "mono-light", "label": "mono light", "dark": False, "colors": {
        "bg": "#ffffff", "bg2": "#f6f6f6", "bg3": "#ededed", "line": "#e3e3e3", "fg": "#0a0a0a", "dim": "#6b6b6b", "faint": "#c2c2c2",
        "accent": "#0a0a0a", "accent2": "#4d4d4d", "ok": "#2b2b2b", "err": "#d32f2f", "warn": "#4d4d4d", "user": "#0a0a0a",
        "code_bg": "#f6f6f6", "sel": "#ebebeb"}},
    "graphite": {"name": "graphite", "label": "graphite", "dark": True, "colors": {
        "bg": "#212121", "bg2": "#2a2a2a", "bg3": "#303030", "line": "#3a3a3a", "fg": "#ececec", "dim": "#9b9b9b", "faint": "#555555",
        "accent": "#ececec", "accent2": "#a3a3a3", "ok": "#7fd18b", "err": "#f28b82", "warn": "#e6c07b", "user": "#ececec",
        "code_bg": "#171717", "sel": "#3a3a3a"}},
}


def themes(store):
    out = dict(BUILTIN_THEMES)
    d = store.home / "themes"
    for p in sorted(d.glob("*.json")) if d.exists() else []:
        t = jload(p)
        if isinstance(t, dict) and isinstance(t.get("colors"), dict):
            t.setdefault("name", p.stem)
            out[t["name"]] = t
    return out


def theme(store, name=None):
    ts = themes(store)
    t = ts.get(name or store.config.get("theme") or "me-dark") or ts["me-dark"]
    colors = dict(BUILTIN_THEMES["me-dark" if t.get("dark", True) else "me-light"]["colors"])
    colors.update({k: h for k, h in ((k, hex6(v)) for k, v in t["colors"].items()) if h})
    return dict(t, colors=colors)


def hex6(v):
    """"#abc" → "#aabbcc"; anything that is not a hex color → None (a bad synced theme must not break every frame)."""
    v = str(v or "").strip()
    if re.fullmatch(r"#[0-9a-fA-F]{3}", v):
        v = "#" + "".join(c * 2 for c in v[1:])
    return v.lower() if re.fullmatch(r"#[0-9a-fA-F]{6}", v) else None


def apply_theme(store, name=None):
    """Point the TUI palette at a theme (diff colors follow ok/err)."""
    c = theme(store, name)["colors"]
    TH.update(c)
    TH["add"], TH["del"] = c["ok"], c["err"]
    TH["faint_t"] = mix(c["faint"], c["dim"], .6)  # faint is tuned for rules and borders: text in it is barely readable
    return c
