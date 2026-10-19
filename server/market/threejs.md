---
name: threejs
description: browser 3D games and scenes with Three.js (r186+): setup (Vite or CDN importmap), renderer/camera/resize, game loop with Timer and fixed-step physics, input (keyboard, pointer lock, touch), Rapier/cannon-es physics, GLTF/Draco/KTX2 assets, lighting and shadows, performance, audio, HUD, mobile, testing a canvas game, deploy. Use for any Three.js / WebGL game or 3D web page
icon: △
color: cyan
---
Adapted for me from CloudAI-X/threejs-skills and majidmanzarpour/threejs-game-skills (both MIT) plus the three.js docs, checked against three 0.186.1. Pair with the `threejs-kit` skill: `scaffold` writes a working starter and `lint` finds outdated or slow API use.

# Three.js game playbook

## 0. Orient
- Find the three version: `package.json` (`"three"`) or the importmap URL. Use APIs from that version. New projects get the current release (0.186.x).
- Find out how it is built. Vite (`vite.config.*`, `npm run dev`) or no build (`index.html` with `<script type="importmap">`). Keep whatever the project uses.
- Keep a short GAME.md with:
  - the core loop in one sentence;
  - controls;
  - the folder map;
  - how to run and build;
  - the debug keys.
- For a new game, write the **core loop contract** first: "Player does [verb] to reach [goal] while [pressure] creates risk; success gives [reward], failure costs [cost]". Build it as a playable vertical slice before any polish: move, goal, fail, restart.

## 1. Project setup
- **Vite (default for anything bigger than a toy):**
  - `npm create vite@latest game -- --template vanilla-ts`, then `npm i three` and `npm i -D @types/three`.
  - Assets go in `public/`, referenced as `/models/x.glb`.
  - Set `base: './'` in vite.config for itch.io or GitHub Pages.
- **No build (quick prototypes, one HTML file):**
  ```html
  <script type="importmap">{"imports":{"three":"https://cdn.jsdelivr.net/npm/three@0.186.1/build/three.module.js","three/addons/":"https://cdn.jsdelivr.net/npm/three@0.186.1/examples/jsm/"}}</script>
  <script type="module" src="main.js"></script>
  ```
  - Serve over http (`npx vite` or `python -m http.server`). `file://` breaks modules and loaders.
- Import addons as `three/addons/...`, never `three/examples/jsm/...` (that path breaks under an importmap).
- `build/three.min.js` and global `THREE` script tags are gone (r160+). Always use ES modules.
- Pin the version. Mixing two copies of three (an addon from another version) breaks `instanceof` checks and doubles the bundle.

## 2. Renderer, camera, resize
```js
const renderer = new THREE.WebGLRenderer({ antialias: true, powerPreference: 'high-performance' });
renderer.setPixelRatio(Math.min(devicePixelRatio, 2));   // >2 costs 4x the fill for no visible gain
renderer.setSize(innerWidth, innerHeight);
renderer.toneMapping = THREE.ACESFilmicToneMapping;       // off by default; AgX/Neutral also fine
renderer.shadowMap.enabled = true; renderer.shadowMap.type = THREE.PCFShadowMap; // PCFSoftShadowMap is deprecated (r186)
document.body.appendChild(renderer.domElement);
const camera = new THREE.PerspectiveCamera(60, innerWidth / innerHeight, 0.1, 500); // keep near/far tight: z-fighting
addEventListener('resize', () => { camera.aspect = innerWidth / innerHeight; camera.updateProjectionMatrix(); renderer.setSize(innerWidth, innerHeight); });
```
- CSS: `html,body{margin:0;height:100%;overflow:hidden}`, `canvas{display:block;touch-action:none}`.
- Leave `outputColorSpace` at sRGB. Color textures (albedo, emissive) need `tex.colorSpace = THREE.SRGBColorSpace`. Data maps (normal, roughness, AO) stay linear. GLTFLoader sets this for you.

## 3. The game loop
```js
const timer = new THREE.Timer(); timer.connect(document);      // THREE.Clock is deprecated (r183); Timer pauses when the tab hides
const STEP = 1 / 60; let acc = 0;
renderer.setAnimationLoop((t) => {
  timer.update(t); const dt = Math.min(timer.getDelta(), 0.1); // clamp: a lag spike must not teleport things through walls
  acc += dt;
  while (acc >= STEP) { input.poll(); world.fixedUpdate(STEP); acc -= STEP; }  // physics + gameplay: deterministic step
  world.update(dt, acc / STEP);                                  // visuals: interpolate, camera, particles, animation mixers
  renderer.render(scene, camera);
});
```
- Use `renderer.setAnimationLoop`, not a hand-written rAF loop. WebXR needs it, and `setAnimationLoop(null)` stops it.
- Keep explicit game states: `title → playing → paused → over`, with one `setState()` that shows and hides UI and starts and stops audio. Pause stops the fixed step, not the render.
- Make the update order explicit: input, then gameplay systems, then physics step, then sync meshes from bodies, then camera, then render.
- Put all gameplay randomness through one seeded RNG (mulberry32), so a bug replays the same way.
- Hot path: no `new Vector3()` per frame. Pre-allocate temporaries at module level and reuse them.

## 4. Input
- Keep a keyboard map in an object: `keydown`/`keyup` set `keys[e.code] = true/false` (`KeyW`, not `w`, so it works on any layout). Clear everything on `blur`.
- Write one `input` object that every device fills (`move: Vector2`, `look`, `jump`, `fire`). Gameplay reads only that object.
- Mouse look: use `PointerLockControls` (`three/addons/controls/PointerLockControls.js`), or call `canvas.requestPointerLock()` on click and read `movementX/Y`. It must start from a user gesture. Show "click to play" until it is locked.
- Touch: use pointer events with `touch-action:none`. Put a virtual stick on the left half (pointerdown origin to current position, then normalize) and buttons on the right. Make buttons at least 48px and keep the HUD inside `env(safe-area-inset-*)`.
- Gamepad: poll `navigator.getGamepads()` every frame (no events). Apply a dead zone of 0.15.
- Picking: build one `Raycaster` and test an explicit array of pickable meshes, or use layers. Never call `intersectObjects(scene.children, true)` every frame.

## 5. Physics and collision
- Simple arcade games (top-down, runner, shooter): no engine. Use spheres and AABBs (`Box3.intersectsBox`, distance checks), plus a ground height or a raycast down for floors.
- Real rigid bodies, characters on slopes, vehicles: **Rapier** (`@dimforge/rapier3d-compat`, 0.21). Call `await RAPIER.init()`, then create a `World({x:0,y:-9.81,z:0})` and `world.step()` in the fixed step.
  - Its `KinematicCharacterController` handles player movement.
  - Sensors handle pickups and triggers.
- Lighter option: **cannon-es** (0.20). Use `world.fixedStep()` or `world.step(STEP)`.
- Sync each mesh from its body after every step (position plus quaternion). Never move a dynamic body's mesh directly.
- Debug: draw colliders with a toggle (Rapier `world.debugRender()` into LineSegments, or cannon-es-debugger).

## 6. Assets
```js
import { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js';
import { DRACOLoader } from 'three/addons/loaders/DRACOLoader.js';
import { KTX2Loader } from 'three/addons/loaders/KTX2Loader.js';
import { MeshoptDecoder } from 'three/addons/libs/meshopt_decoder.module.js';
const draco = new DRACOLoader().setDecoderPath('https://cdn.jsdelivr.net/npm/three@0.186.1/examples/jsm/libs/draco/');
const ktx2 = new KTX2Loader().setTranscoderPath('https://cdn.jsdelivr.net/npm/three@0.186.1/examples/jsm/libs/basis/').detectSupport(renderer);
const gltf = new GLTFLoader().setDRACOLoader(draco).setKTX2Loader(ktx2).setMeshoptDecoder(MeshoptDecoder);
```
- Use a `LoadingManager` for the progress bar, and `await renderer.compileAsync(scene, camera)` before the start screen to avoid first-frame shader stalls.
- Format: glTF/GLB only. Compress with `npx @gltf-transform/cli optimize in.glb out.glb --texture-compress ktx2`. Size textures to how big they appear on screen.
- Skinned characters:
  - Clone with `SkeletonUtils.clone()`, never `.clone()`.
  - Use one `AnimationMixer` per character and `mixer.update(dt)` every frame.
  - Switch animations with `action.reset().fadeIn(0.2).play()` and `prev.fadeOut(0.2)`.
- Placeholder art first: primitives with flat colors (and the `threejs-kit` palette). Use only assets the user has rights to (CC0: Kenney, Quaternius, Poly Pizza, ambientCG, Poly Haven).
- Free memory when a level unloads: traverse the scene and dispose every geometry, every material and each texture on it (`for (const v of Object.values(mat)) if (v?.isTexture) v.dispose()`). `material.dispose()` does not free its textures.

## 7. Light, look, effects
- Use an environment for nice PBR cheaply: `scene.environment = pmrem.fromScene(new RoomEnvironment(), 0.04).texture` (or an HDR via `HDRLoader`, which replaces `RGBELoader`, with `EquirectangularReflectionMapping`). Add one `DirectionalLight` with shadows.
- Shadows:
  - Fit the directional light's shadow camera tightly around the play area (`left/right/top/bottom/near/far`) and make it follow the player.
  - Set `shadow.mapSize` to 1024–2048, `shadow.normalBias ≈ 0.02` and a tiny `bias` (-0.0001).
  - `castShadow` only on objects that matter.
  - For static scenes, set `shadow.autoUpdate = false` and `needsUpdate = true` on change.
- Point and spot lights use physical units: intensity in candela (tens to hundreds), with decay. Turn lights off with `intensity`. Adding or removing lights at runtime recompiles every material.
- Cheap style that reads well: `MeshToonMaterial` or flat colors, fog (`scene.fog = new THREE.Fog(c, near, far)`, which also hides the far plane), a fake blob shadow (a radial-gradient plane with `depthWrite:false`).
- Post-processing (`EffectComposer`, `RenderPass`, `UnrealBloomPass`, `OutputPass` last) costs a full-screen pass each. Make it optional on mobile.
- Game feel matters more than graphics. Use these:
  - acceleration and friction instead of instant speed;
  - camera follow with damping (`camera.position.lerp(target, 1 - Math.exp(-k * dt))`);
  - screen shake on hits, hit-stop of 50–80 ms;
  - particles and sound on every important event;
  - an instant restart.

## 8. Performance (measure, don't guess)
- Read `renderer.info.render.calls` / `.triangles` and `renderer.info.memory` after a render. Aim for under ~150 draw calls on mobile. Show `stats.module.js` or a small FPS counter behind a debug key.
- Many copies of one mesh: use `InstancedMesh` (`setMatrixAt` + `instanceMatrix.needsUpdate = true`, and `computeBoundingSphere()` after moving instances).
- Many different static meshes: merge them with `BufferGeometryUtils.mergeGeometries` per area, or use `BatchedMesh`.
- Static objects: `matrixAutoUpdate = false` and one `updateMatrix()`.
- Never toggle `transparent`, `side`, `fog` or texture presence at runtime. That forces a new shader program.
- Pool bullets, particles and enemies (deactivate and reuse) instead of creating and disposing them every frame.
- Fill cost: pixel ratio squared times overdraw. Lower `setPixelRatio` first when the GPU is the bottleneck.

## 9. UI, audio, mobile
- HUD in HTML/CSS over the canvas (`position:fixed; pointer-events:none`, buttons `pointer-events:auto`). Do not draw text in WebGL. Update the DOM only when a value changes.
- Audio: Web Audio (or `THREE.AudioListener` + `Audio`/`PositionalAudio`). The context must be resumed from a user gesture (the start button). Keep a mute toggle and pause audio with the game.
- Mobile: test at 360×640 portrait and landscape. Use touch controls. Cap the pixel ratio at 1.5 there and turn shadows and post-processing down. Handle `visibilitychange` (pause).
- Save progress in `localStorage`, wrapped in try/catch.

## 10. Verify (when self-testing is on)
- Run `npm run build` (or `npx vite build`). The build must pass and the console must stay clean.
- Run it in a headless browser (`webapp-testing` skill, Playwright):
  - use `--use-angle=swiftshader` or `--enable-unsafe-swiftshader` for WebGL;
  - wait for a `window.__game.ready` flag, send keys, read state from `window.__game` (score, player position, state);
  - take a screenshot during play, not on the title screen;
  - check that the canvas is not blank (sample pixels), and that there are no console or page errors and no 404s.
- Expose a small debug hook in development (`window.__game = { state, player, step(n) }`) and strip it from production builds (`import.meta.env.DEV`).
- Report to the user:
  - how to run it (`npm run dev`, then the URL);
  - the controls;
  - what to try;
  - what is placeholder.

## Pitfalls checklist
- Black screen. Check, in order:
  1. no light on a lit material;
  2. camera inside or behind the object;
  3. object not added to the scene;
  4. near/far wrong;
  5. the console.
- A model is invisible, or culled while visible: wrong scale (Blender units), or a stale bounding sphere after editing vertices.
- Textures look washed out or too dark: wrong `colorSpace` on color maps.
- Shadow acne or peter-panning: tune `normalBias` and `bias`, then tighten the shadow camera.
- Stutter on first explosion or level: shaders compiling lazily. Use `compileAsync` and pre-warm effects.
- Memory grows across restarts: missing dispose, or listeners added again on every restart.
- Speed differs by monitor (60 vs 144 Hz): movement not multiplied by `dt`, or physics not on the fixed step.
- `layers.set(n)` removes the object from layer 0, so the camera stops seeing it. Use `layers.enable(n)`.
