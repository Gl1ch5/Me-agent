"""
name: game-recon
description: find installed PC games (Steam libraries) and work out how they can be modded: engine, managed or native code, anti-cheat, loaders already present, route. Writes MODDING_PLAN.md
icon: ⌕
color: cyan
---
Use at the start of any mod task, before writing mod code. Order: libraries → games → scan the chosen folder → plan. scan only reads files. Then search the community now (wiki, Nexus, Thunderstore, GitHub, the loader's current install steps) because versions move. Online games with anti-cheat are not modded (see mod-engines, Hard rules).
Derived from universal-modder (MIT, Copyright (c) 2026 Rehan and universal-modder contributors): https://github.com/rehan-remade/universal-modder
"""
import json, os, re
from pathlib import Path

ANTI_CHEAT = ["EasyAntiCheat", "EasyAntiCheat_EOS", "BattlEye", "BEService", "vgk.sys", "EAAntiCheat", "Javelin", "ACE-Core"]
LOADERS = {
    "BepInEx": ["BepInEx"], "MelonLoader": ["MelonLoader"], "SMAPI": ["StardewModdingAPI.exe"],
    "tModLoader": ["tModLoader.dll", "tModLoader.targets"], "UE4SS": ["ue4ss"], "Everest": ["Everest.dll"],
}
ROUTE = {  # engine -> (playbook section in mod-engines, or None; suggested route)
    "unity-mono": ("unity", "BepInEx 5 + Harmony"), "unity-il2cpp": ("unity", "BepInEx 6 / MelonLoader"),
    "unreal": ("unreal", "UE4SS Lua or pak mods"), "godot": ("godot", "Godot Mod Loader or PCK overlay"),
    "xna-net": ("dotnet-xna", "the game's loader (SMAPI, Everest) or MonoMod"), "terraria": ("dotnet-xna", "tModLoader"),
    "genie": ("genie-aoe2", "data mod + graphics mod"), "bethesda": (None, "ESP/ESL + Nexus packaging (no playbook yet)"),
    "source": (None, "Source SDK tools and VPK (no playbook yet)"), "native": ("native", "proxy DLL + hooks"),
}


def _steam_roots():
    roots = []
    if os.name == "nt":
        for var in ("ProgramFiles(x86)", "ProgramFiles"):
            if os.environ.get(var):
                roots.append(Path(os.environ[var]) / "Steam")
        roots.append(Path("C:/Program Files (x86)/Steam"))
    else:
        home = Path.home()
        roots += [home / ".steam/steam", home / ".local/share/Steam", home / "Library/Application Support/Steam"]
    return [r for r in dict.fromkeys(roots) if r.is_dir()]


def _libraries():
    libs = []
    for root in _steam_roots():
        libs.append(root / "steamapps")
        vdf = root / "steamapps" / "libraryfolders.vdf"
        if vdf.exists():
            for m in re.finditer(r'"path"\s+"([^"]+)"', vdf.read_text("utf-8", "replace")):
                libs.append(Path(m.group(1).replace("\\\\", "\\")) / "steamapps")
    return [l for l in dict.fromkeys(libs) if l.is_dir()]


def _files_upto(root, limit=6000, depth=4):
    """Files and folder names under root (bounded): (list of Path, set of names)."""
    out, names = [], set()
    base = len(root.parts)
    for dp, dn, fn in os.walk(root):
        names.update(dn)
        if len(Path(dp).parts) - base >= depth:
            dn[:] = []
        for f in fn:
            out.append(Path(dp) / f)
            names.add(f)
            if len(out) >= limit:
                return out, names
    return out, names


@action
def libraries(ctx):
    "Steam library folders found on this machine"
    libs = [str(l) for l in _libraries()]
    if not libs:
        ctx.log("no Steam libraries found; use scan with the game's folder path")
    return libs


@action
def games(ctx, filter=""):
    "Installed Steam games: name, appid, folder"
    out = []
    for lib in _libraries():
        for acf in sorted(lib.glob("appmanifest_*.acf")):
            text = acf.read_text("utf-8", "replace")
            name = re.search(r'"name"\s+"([^"]+)"', text)
            appid = re.search(r'"appid"\s+"(\d+)"', text)
            folder = re.search(r'"installdir"\s+"([^"]+)"', text)
            if not (name and folder):
                continue
            if filter and filter.lower() not in name.group(1).lower():
                continue
            out.append({"name": name.group(1), "appid": appid.group(1) if appid else "", "folder": str(lib / "common" / folder.group(1))})
    ctx.table(out, title=f"installed games{' matching ' + filter if filter else ''}")
    return out


@action
def scan(ctx, path):
    "Fingerprint a game folder: engine, code type, anti-cheat, loaders, route, playbook"
    root = Path(path).expanduser()
    if not root.is_dir():
        raise ValueError(f"not a folder: {root}")
    ctx.status("reading the install")
    files, names = _files_upto(root)
    rel = {str(f.relative_to(root)).replace("\\", "/") for f in files}
    signals, engine = [], None

    def hit(key, why):
        signals.append(why)
        return key

    has_data_dir = any(d.is_dir() and d.name.endswith("_Data") for d in root.iterdir())
    if has_data_dir or "UnityPlayer.dll" in names:
        if "GameAssembly.dll" in names:
            engine = hit("unity-il2cpp", "UnityPlayer.dll + GameAssembly.dll (IL2CPP)")
        elif any(r.endswith("Managed/Assembly-CSharp.dll") for r in rel):
            engine = hit("unity-mono", "Unity Mono: Managed/Assembly-CSharp.dll")
        else:
            engine = hit("unity-mono", "Unity layout (backend unclear: check for GameAssembly.dll)")
    if engine is None and any(f.suffix == ".pck" for f in files):
        engine = hit("godot", "a .pck next to the exe (Godot?)")
    if engine is None and (any(re.search(r"Win64.*Shipping\.exe$", r) for r in rel) or any(r.endswith(".pak") and "Content/Paks" in r for r in rel)):
        engine = hit("unreal", "Unreal layout: Binaries/Win64 Shipping exe or Content/Paks")
    if engine is None and any(n.lower() == "terraria.exe" for n in names):
        engine = hit("terraria", "Terraria.exe (.NET, tModLoader route)")
    if engine is None and (({"FNA.dll", "MonoGame.Framework.dll"} & names) or any(n.startswith("Microsoft.Xna.Framework") for n in names)):
        engine = hit("xna-net", "XNA/FNA/MonoGame .NET assemblies")
    if engine is None and ("AoE2DE_s.exe" in names or any(r.endswith("empires2_x2_p1.dat") for r in rel)):
        engine = hit("genie", "AoE2 DE data file")
    if engine is None and any(n.lower().endswith((".bsa", ".ba2")) for n in names):
        engine = hit("bethesda", "Bethesda archives (.bsa/.ba2)")
    if engine is None and "gameinfo.txt" in names:
        engine = hit("source", "gameinfo.txt (Source engine)")
    if engine is None:
        engine = hit("native", "no known engine signature: native C/C++ (check strings and imports)")

    anti_cheat = sorted({m for m in ANTI_CHEAT if m in names or any(r.startswith(m) or f"/{m}" in r for r in rel)})
    loaders = []
    for name, markers in LOADERS.items():
        if any(m in names or any(r.endswith("/" + m) or r == m for r in rel) for m in markers if not m.startswith("*")):
            loaders.append(name)
    if any(f.suffix == ".asi" for f in files):
        loaders.append("ASI loader plugins")
    mono_dotnet = any(n.endswith(".dll") and n.startswith("System.") for n in names)
    section, route = ROUTE[engine]
    managed = mono_dotnet or engine.startswith(("unity-mono", "xna", "terraria"))
    result = {"path": str(root), "engine": engine, "signals": signals, "code": "managed .NET/Mono" if managed else "native or IL2CPP",
              "anti_cheat": anti_cheat, "loaders_present": sorted(set(loaders)), "suggested_route": route,
              "playbook": f"mod-engines#{section}" if section else "none yet: research the community and the knowledge base",
              "warning": ("anti-cheat present: do not inject; use official tools or offline modes" if anti_cheat else "")}
    ctx.table([{"field": k, "value": json.dumps(v, ensure_ascii=False) if isinstance(v, (list, dict)) else str(v)} for k, v in result.items()],
              title=f"recon · {root.name}")
    return result


@action(confirm=True)
def plan(ctx, path, idea, out="MODDING_PLAN.md"):
    "Write MODDING_PLAN.md for a game folder and an idea (creates or overwrites the file)"
    r = scan(ctx, path)
    lines = [f"# {Path(path).name} modding plan", "",
             f"- Install: {r['path']}",
             f"- Engine: {r['engine']} ({'; '.join(r['signals'])})",
             f"- Code: {r['code']}",
             f"- Anti-cheat / online: {', '.join(r['anti_cheat']) if r['anti_cheat'] else 'none detected (check the store page and EULA)'}",
             f"- Loaders already present: {', '.join(r['loaders_present']) or 'none'}",
             f"- Community route: {r['suggested_route']} (verify the current loader release and install steps)",
             f"- Idea: {idea}",
             f"- Chosen route: {r['suggested_route']}, because it is the cheapest route with a loader hook for this idea (confirm)",
             "- Lab plan: back up saves (saves-backup skill), use a separate save folder or a spare world, windowed mode if automating",
             f"- Playbook: {r['playbook']}", "",
             "## Unknowns to resolve first", "- save and config folders", "- loader version compatible with this game build", ""]
    if r["warning"]:
        lines.insert(3, f"- WARNING: {r['warning']}")
    Path(out).write_text("\n".join(lines), "utf-8")
    return f"wrote {out}"
