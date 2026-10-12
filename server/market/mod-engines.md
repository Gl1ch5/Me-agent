---
name: mod-engines
description: how to mod a PC game by its engine: Unity (BepInEx, Harmony), XNA/.NET (Terraria tModLoader, Stardew SMAPI, Celeste Everest), Minecraft (Fabric, NeoForge), Unreal (UE4SS, pak), Godot, Age of Empires II DE data and graphics mods, native C/C++ (proxy DLL, hooks). Plus the hard safety rules. Use after game-recon picks an engine
icon: ⚙
color: purple
---
Derived from universal-modder (MIT, Copyright (c) 2026 Rehan and universal-modder contributors): https://github.com/rehan-remade/universal-modder. Versions move: check the loader's current release and install steps before you follow a recipe here.

## Hard rules (read first)
- Mod only games the user owns, with single-player, offline modes, or servers the user runs.
- Online games with anti-cheat (EasyAntiCheat, BattlEye, Vanguard, VAC on secured servers) are off limits for code injection. Offer official tools (Workshop, editors) or an offline mode instead.
- No cheats for multiplayer (aim bots, ESP, speed hacks, packet tampering), whatever the framing.
- Never disable anti-cheat, spoof hardware IDs, or bypass DRM and ownership checks. If a loader needs that, stop.
- Don't download ROMs, ISOs or game files. Ship only your own code and assets. Never ship decompiled source.
- Test with a backup of the saves (saves-backup) and a spare world or save folder.

## Route choice
Cheapest first: data and asset files > loader API > managed-code patching (Harmony, Mixin, MonoMod) > native hooks > reimplementation. Write the route and the reason into MODLOG.md before building.

## unity
Identify: UnityPlayer.dll and a `<Game>_Data/` folder. Mono: `<Game>_Data/Managed/Assembly-CSharp.dll`. IL2CPP: `GameAssembly.dll` and `il2cpp_data/Metadata/global-metadata.dat`.
1. Check for an existing community library (Thunderstore or Nexus, names like R2API, Jotunn). Build on it when it exists.
2. BepInEx 5 (Mono): unzip into the game folder (it adds winhttp.dll and doorstop_config.ini), run once, plugins go in `BepInEx/plugins/`, log in `BepInEx/LogOutput.log`.
3. BepInEx 6 or MelonLoader for IL2CPP. The first run generates interop assemblies (slow once).
4. Plugin skeleton:
```csharp
[BepInPlugin("you.mymod", "My Mod", "0.1.0")]
public class Plugin : BaseUnityPlugin {
    void Awake() { new Harmony("you.mymod").PatchAll(); Logger.LogInfo("loaded"); }
}
[HarmonyPatch(typeof(PlayerController), "Update")]
static class Patch { static void Postfix(PlayerController __instance) { } }
```
Reference Assembly-CSharp.dll and UnityEngine*.dll from `_Data/Managed`, with `<Private>false</Private>`.
5. Harmony: prefix runs first (return false skips the original), postfix can change `__result`, transpiler edits IL. Use AccessTools for private members.
6. Read the game: ILSpy or ilspycmd for Mono; Cpp2IL or Il2CppDumper for IL2CPP. UnityExplorer is the fastest in-game inspector.
7. Content: clone an existing prefab and swap mesh, material and stats. Load your own AssetBundle built with the same Unity major version.
Pitfalls: loader builds are tied to Unity versions; re-apply state on `SceneManager.sceneLoaded`; IL2CPP strips methods the game never calls; Proton needs `WINEDLLOVERRIDES="winhttp=n,b" %command%`.

## dotnet-xna (Terraria, Stardew, Celeste, MonoGame/FNA games)
Identify: a managed exe, FNA.dll or MonoGame.Framework.dll, Content/*.xnb.
- **Terraria, tModLoader** (free Steam app 1281930, must be in the library). Mods are C# in `Documents/My Games/Terraria/tModLoader/ModSources/<Mod>/`. Build with Workshop > Develop Mods > Build + Reload, or from the command line with the bundled runtime: `.\dotnet\dotnet.exe tModLoader.dll -build "<ModSources>\<Mod>"` on Windows, `dotnet tModLoader.dll -build "<ModSources>/<Mod>"` on Linux.
  - Content classes: ModItem (SetDefaults, Shoot, UseItem, AddRecipes), ModProjectile (AI, PreDraw, OnHitNPC), ModNPC (AI, FindFrame, SetStaticDefaults with Main.npcFrameCount), ModSystem, ModPlayer, ModCommand (chat commands for tests), GlobalNPC and GlobalItem to change vanilla content.
  - Art: PNGs beside the class, `Texture => "Mod/Assets/Name"`. NPC sheets are vertical strips; frame height = texture height / npcFrameCount.
  - Lab: `-tmlsavedirectory <dir>` isolates saves, `-skipselect Player:World` loads straight in.
  - Hooks into vanilla: `On_Main.DoUpdate += ...` (detours) and `IL_*` (IL edits) via MonoMod.
  - Logs: `tModLoader-Logs/client.log`. Single-player gameplay mods; multiplayer needs `side = Both`.
- **Stardew Valley, SMAPI**: plain content (sprites, dialogue, maps) as Content Patcher JSON packs. Code as SMAPI C# mods: `ModEntry : Mod`, `helper.Events.GameLoop.UpdateTicked`, `helper.GameContent` for asset edits, Harmony for patches. Build with the NuGet package Pathoschild.Stardew.ModBuildConfig.
- **Celeste, Everest**: install via Olympus. Code mods hook with MonoMod (`On.Celeste.Player.Update += ...`). Maps with Lönn.
- Read the game with `ilspycmd -p -o <outside-repo>/<game>-decomp <Game>.exe`. The decompile is the spec: port logic, don't guess.
- Assets: loaders take plain PNG and WAV/JSON. Match pixel scale and outline. Terraria sprites are 2x pixel art with dark outlines.
Pitfalls: 32 vs 64 bit (old XNA Terraria is x86; tModLoader is x64 .NET 8).

## minecraft (Java Edition)
- **Fabric** (fast to new versions): template from fabricmc.net/develop/template or the fabric-example-mod repo. `./gradlew build` puts the jar in `mods/`. `./gradlew runClient` is the test oracle: read `run/logs/latest.log`.
- **NeoForge** (successor of Forge for modern versions) and **Forge 1.20.1** (many CurseForge instances, JDK 17). `runClient` and `runServer` are the oracles.
- Content: register items, blocks, entities and sounds through registries. Assets under `src/main/resources/assets/<modid>/` (16x16 PNG, model JSON, lang JSON, sounds.json and ogg). Data (recipes, loot, tags) under `data/<modid>/`.
- Use a separate launcher profile with its own gameDir so the user's worlds stay safe.
- Multiplayer servers need the mod too, or use plugins (Paper/Spigot) plus a resource pack.
Pitfalls: match the exact game + loader + API versions. Dev runs use Mojang names but the shipped jar uses SRG names: test with the real jar in a real instance. Give network channels an explicit protocol version. Stereo sounds don't attenuate: use mono.
Bedrock: behavior packs (JSON plus the @minecraft/server script API) and resource packs, developed in `com.mojang/development_*_packs`.

## unreal (UE4 and UE5)
Identify: `<Project>/Binaries/Win64/<Project>-Win64-Shipping.exe`, `Content/Paks/*.pak`. UE5 often uses IoStore `.utoc` and `.ucas`. Saves are under `%LOCALAPPDATA%\<Project>\Saved\`.
1. **UE4SS** (lua mods, no engine recompile): put its proxy DLL and the `ue4ss/` folder next to the shipping exe. Lua mods live in `ue4ss/Mods/<Mod>/Scripts/main.lua`, enabled in `mods.txt`. Hooks: `RegisterHook("/Script/Engine.PlayerController:ClientRestart", fn)`, `NotifyOnNewObject`, `FindFirstOf`, `ExecuteInGameThread`. The Live View dumper finds classes and properties.
2. **Pak mods**: browse with FModel (needs the right UE version and AES key if encrypted). Edit .uasset with UAssetGUI or UAssetAPI. Pack with repak (`repak pack --version V11`) or retoc for IoStore. Name it `*_P.pak` and put it in `Content/Paks/~mods/`. New content must be cooked in the matching UE editor version, with the same `/Game/...` paths.
3. **Showcase tools**: console unlockers and UEVR enable slow motion, freecam and VR; good for clips.
Pitfalls: paks cooked for the wrong UE version crash on mount; blueprint mods break on updates; EAC/BattlEye titles are off limits; games with .sig verification are not bypassed where that is anti-tamper for online play.

## godot (3.x and 4.x)
Identify: a `.pck` next to the exe (magic GDPC) or appended to the exe. GDScript is tokenized (.gdc), sometimes encrypted.
- Read: GDRE Tools (gdsdecomp) recovers the project. Open it in the matching editor version.
- Routes: Godot Mod Loader (mods are zips in `mods/`, hooks via `extend`); PCK overlay with `ProjectSettings.load_resource_pack("res://mod.pck")` (later packs override the same res:// paths; you need code execution to call it); `override.cfg` next to the exe for autoloads.
- Content: make scenes in the matching editor version and export a pack with only your files (`--export-pack`). Pixel art: nearest texture filter.
Pitfalls: editor and runtime versions must match; GDScript 1.0 (Godot 3) and 2.0 (Godot 4) differ. Rebuilt games are for personal use only.

## genie-aoe2 (Age of Empires II DE)
Identify: `AoE2DE_s.exe`, `resources/_common/dat/empires2_x2_p1.dat`. Local mods are folders in `mods/local/<Mod>/` mirroring `resources/`. Single-player and lobbies are fine; ranked uses unmodded data.
- Data mod: a modified .dat, chosen in the lobby's Data Mod dropdown. Edit with genieutils-py or the Advanced Genie Editor.
- Graphics mod: `resources/_common/drs/graphics/*.sld`. Unit sprites have 16 headings clockwise from east, with a hotspot at the ground point. Render from 3D with `um render3d --preset aoe2 --shadows` (repo tool) and mask player colour.
- Strings: `resources/en/strings/key-value/key-value-modded-strings-utf8.txt`. New ids must be unused in the stock tables.
- Scenarios: AoE2ScenarioParser builds .aoe2scenario test scenes.
Facts: the civ picker has hard-coded limits, so to add a civ replace a slot (copy a base civ's tech tree, overwrite names and art). Append rather than remove, so existing indices stay stable. Castle unique units need creatable_type 2. Command-panel icons come from the base game's atlas.

## native (C/C++ engines with no loader)
1. Get code in: a proxy DLL named like one the game loads (version.dll, dinput8.dll, winmm.dll, dxgi.dll, d3d9.dll, xinput1_3.dll). It forwards the real exports and starts a thread from DllMain (do little under the loader lock). Ultimate ASI Loader loads `*.asi` plugins.
2. Find the hook point: strings and imports first, then Ghidra or IDA cross-references. Cheat Engine for values, x64dbg for tracing, ReClass.NET for structs. Find functions by byte-pattern scan at startup, not fixed addresses, and log when a pattern is not found.
3. Hook with MinHook, SafetyHook or Detours. Match the calling convention. Keep hooks tiny; do heavy work elsewhere.
4. Overlay: hook Present (D3D11/12) or vkQueuePresent and draw Dear ImGui.
Pitfalls: ASLR (compute addresses from the module base), engine threads (queue work for the main thread), crashes (log with flushes, write minidumps), game updates move everything: pin a version.

## Not covered yet
Bethesda (Creation Engine), Source, Zengin and others: there is no playbook here. Search the community (wiki, Nexus, GitHub) and the knowledge base (the knowledge skill) before starting.
