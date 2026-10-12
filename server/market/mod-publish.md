---
name: mod-publish
description: release a finished game mod: lint with mod-lint, package for the platform (Nexus, Thunderstore, Workshop, Modrinth, tModLoader), README with credits and AI disclosure, versioning, and the draft post. Publishing itself is always the user's decision
icon: ⬆
color: green
---
Derived from universal-modder (MIT, Copyright (c) 2026 Rehan and universal-modder contributors): https://github.com/rehan-remade/universal-modder

## 1. Lint
Run the mod-lint skill on the mod folder with the game's install folder as `game`. Fix every FAIL (game files, leaked keys, .env files). Decide each WARN on purpose. AoE2 data mods ship a modified .dat, which is normal for that platform.

## 2. Package for the platform
| Platform | What to ship |
|---|---|
| tModLoader | the built .tmod, published from the in-game Mod Sources menu (Steam Workshop) |
| BepInEx / Thunderstore | zip with manifest.json, icon.png (256x256), README.md, plugins/<Mod>.dll; dependency strings like BepInEx-BepInExPack-5.4.2100 |
| Nexus Mods | zip laid out as it installs (Data/ for Bethesda, BepInEx/plugins/ for BepInEx, ue4ss/Mods/ for UE4SS, ~mods/*.pak for pak mods); options need a FOMOD installer |
| AoE2 DE | the official mods site or the in-game uploader |
| Steam Workshop | the game's own uploader or SDK tool |
| Minecraft | Modrinth or CurseForge jar with fabric.mod.json or neoforge.mods.toml |
| ROM hacks, GameMaker | patches only (BPS, IPS, xdelta), never the modified game file |

## 3. README (in the zip and on the page)
- What it adds: bullets, and the showcase clip or a GIF.
- Requirements: game version, loader and its version, dependencies.
- Install, uninstall, troubleshooting (where the log is).
- Compatibility: multiplayer? Known conflicts?
- Credits: the loader and libraries; references you learned from; "Art and audio generated with fal (fal.ai) using <models>" (the fal_manifest.jsonl lists them); an honest AI disclosure (which agent and model built it).
- License for your code (MIT or Apache are common). Assets follow the models' licenses.

## 4. Version and changelog
Semver in the manifest or build file. Name the supported game version. Keep a changelog. When the game updates, rerun the in-game test before you bump.

## 5. The post
- Lead with the video: 20-45 s, gameplay within 2-3 s.
- Post text: the hook, what it is, the "how" credit (which agent and fal built it), a link.
- Footage from someone else: credit them and ask first.
- Publishing is the user's call. Draft the post, show it, and let them press the button.
