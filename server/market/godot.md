---
name: godot
description: Godot 4 game development with GDScript (and C#): project layout, scenes and nodes, signals, physics, UI, resources, autoloads, running and testing headless, exporting, Godot 3 to 4 migration, performance. Use for any work inside a Godot project (project.godot)
icon: ◆
color: blue
---
# Godot 4 playbook

## 0. Orient first (every session)
- Find `project.godot`. Read `config/features` (e.g. `PackedStringArray("4.3", "Forward Plus")`): that is the engine version. Godot 3 projects have `config_version=4` and no features line; Godot 4 has `config_version=5`. Never mix 3.x and 4.x APIs.
- Find the editor binary: `godot`, `godot4`, `Godot_v4*.exe` on PATH, or ask once for its path and remember it (memory tool). Check `godot --version` matches the project's major.minor.
- Read `[autoload]`, `[input]` and `run/main_scene` in project.godot, then `ls **/*.tscn` and `ls **/*.gd`. Map scenes → scripts before editing.
- Keep a short GAME.md (or AGENTS.md section): engine version, main scene, autoloads, input actions, folder map, how to run and test.

## 1. Run, check, test — headless, so you can verify without the editor UI
- Parse/compile check of the whole project (catches script errors): `godot --headless --path . --import` (first run, builds .godot/ cache), then `godot --headless --path . --check-only --script res://path/to/script.gd` for one script.
- Run the game for N frames and quit: `godot --headless --path . --quit-after 300` — read stdout/stderr for `SCRIPT ERROR`, `ERROR:` and `WARNING:` lines. Any of them = not done.
- Run a script as the main loop: `extends SceneTree` + `func _init(): ...; quit()` then `godot --headless --path . -s res://tools/check.gd`.
- Tests: GUT (`addons/gut`, run `godot --headless -s addons/gut/gut_cmdln.gd -gdir=res://test -gexit`) or GdUnit4 (`addons/gdUnit4/runtest.sh -a res://test`). Use what the project has; propose GUT if nothing.
- Visual check when it matters: run with a window and `--write-movie out.png --fixed-fps 30 --quit-after 60` (Godot 4.2+) to dump frames you can read.
- Never edit `.godot/` (cache) or `*.import` by hand; delete `.godot/` to force a reimport if the cache is broken.

## 2. Editing .tscn / .tres safely (text formats)
- Prefer creating nodes in code or editing the scene text carefully. Format: `[gd_scene load_steps=N format=3 uid="uid://..."]`, then `[ext_resource type="Script" path="res://player.gd" id="1_abc"]`, `[sub_resource ...]`, `[node name="Player" type="CharacterBody2D"]`, children `[node name="Sprite" type="Sprite2D" parent="."]`, deeper `parent="Body/Arm"`, connections `[connection signal="pressed" from="Button" to="." method="_on_button_pressed"]`.
- Every `ExtResource("id")` / `SubResource("id")` must exist; keep ids unique; `load_steps` = resources + 1 (Godot tolerates a wrong count, but keep it right). Don't invent `uid://` values — omit `uid` on new resources and let Godot add it.
- After editing scene text, run the headless import/run to make sure it loads.

## 3. GDScript 2.0 (Godot 4) essentials
- Static typing everywhere: `var speed: float = 200.0`, `func hit(dmg: int) -> void:`, `@onready var sprite: Sprite2D = $Sprite2D`, `@export var max_hp := 100`, `@export_range(0, 1) var t := 0.5`. Typed code is faster and errors early.
- Signals: `signal died(by: Node)`; `died.emit(self)`; connect with callables: `button.pressed.connect(_on_pressed)`, `enemy.died.connect(_on_enemy_died.bind(enemy))`. Disconnect or use `CONNECT_ONE_SHOT` when needed. "Call down, signal up": parents call children; children emit signals.
- Awaiting: `await get_tree().create_timer(0.5).timeout`, `await anim.animation_finished`. A node freed during await errors — check `is_instance_valid(self)` after long awaits.
- Physics bodies: `CharacterBody2D/3D` — set `velocity`, call `move_and_slide()` (no arguments in 4.x), use `is_on_floor()`. Gravity from `ProjectSettings.get_setting("physics/2d/default_gravity")`. Movement in `_physics_process(delta)`, visuals in `_process`.
- Input: define actions in project.godot `[input]`; use `Input.get_vector("left","right","up","down")`, `Input.is_action_just_pressed("jump")`, `_unhandled_input(event)` for gameplay so UI can eat events first.
- Instancing: `const BULLET := preload("res://bullet.tscn")`; `var b := BULLET.instantiate() as Bullet; get_tree().current_scene.add_child(b)`. `class_name Bullet` registers a global type.
- Groups: `add_to_group("enemies")`, `get_tree().call_group("enemies", "stun")`, `get_tree().get_nodes_in_group(...)`.
- Freeing: `queue_free()`, never `free()` during physics/signal callbacks. Deferred changes to the tree inside physics callbacks: `call_deferred("add_child", n)` / `set_deferred("monitoring", false)`.
- Resources for data: `class_name ItemData extends Resource` with `@export` fields, saved as `.tres`; `duplicate()` before mutating a shared resource (shared by default!).
- Autoloads (singletons) for global state/event buses; keep them few. Save games: `FileAccess.open("user://save.json", FileAccess.WRITE)` + `JSON.stringify`, or `ResourceSaver.save()`; always under `user://`, never `res://` (read-only in exports).
- Tweens: `create_tween().tween_property(node, "modulate:a", 0.0, 0.3)`; tweens are bound to the node that created them.
- UI: Control nodes + Containers (VBox/HBox/Margin/Grid) for layout, anchors presets for screen-relative; a Theme resource for styling; `size_flags_*` for stretching. Don't position UI with absolute pixels.
- Tool scripts: `@tool` runs in the editor — guard game-only code with `if Engine.is_editor_hint(): return`.

## 4. Godot 3 → 4 renames you will hit
`KinematicBody2D`→`CharacterBody2D`, `move_and_slide(v)`→`velocity = v; move_and_slide()`, `yield(x, "s")`→`await x.s`, `connect("s", obj, "m")`→`s.connect(obj.m)`, `export var`→`@export var`, `onready`→`@onready`, `instance()`→`instantiate()`, `Spatial`→`Node3D`, `rand_range`→`randf_range`, `PoolStringArray`→`PackedStringArray`, `File`/`Directory`→`FileAccess`/`DirAccess`, `tool`→`@tool`, `setget`→ property `set(v):`/`get:` blocks, `.empty()`→`.is_empty()`, `OS.get_ticks_msec`→`Time.get_ticks_msec`. The editor's Project → Tools → Upgrade does most of it; review by hand after.

## 5. C# (Godot .NET)
- Needs the .NET build of the editor and the .NET SDK; build with `dotnet build` in the project folder, then run headless as above. Classes must be `partial` and match the file name. Signals: `[Signal] public delegate void DiedEventHandler();` → `EmitSignal(SignalName.Died)`.

## 6. Performance and correctness checklist
- Profile before optimizing (Debugger → Profiler/Monitors; headless: print `Performance.get_monitor(Performance.TIME_FPS)`).
- Many identical objects: MultiMeshInstance, object pools instead of instantiate/free per frame. Cache node lookups in `@onready`.
- Physics layers/masks set deliberately (layer = what I am, mask = what I see). Collision shapes must not be scaled non-uniformly.
- Pixel art: Project Settings → Rendering → Textures → Default Texture Filter = Nearest; snap 2D transforms; integer scaling with `display/window/stretch/mode = viewport`.
- Never leave `print` spam; use `push_warning`/`push_error` for real problems.

## 7. Export
- Install export templates matching the exact editor version. Presets live in `export_presets.cfg`. CLI: `godot --headless --path . --export-release "Windows Desktop" build/game.exe` (`--export-debug`, `--export-pack "preset" game.pck` for a pack only).
- Web export needs threads/SharedArrayBuffer headers (COOP/COEP) or the single-threaded build (4.3+).

## 8. Workflow that works
1. Vertical slice: one scene that runs (player moves, one enemy, one interaction), verified headless and by a short run.
2. Then widen in small steps; after each: headless import + `--quit-after` run, zero `SCRIPT ERROR`.
3. Keep gameplay numbers in exported vars / Resources so the user can tune them in the inspector.
4. Report what you ran and what the user should try in the editor (which scene, which keys).
