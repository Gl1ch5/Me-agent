"""
name: threejs-kit
description: Three.js game tools: `scaffold` writes a small playable starter game (Vite or a no-build importmap page: loop with fixed step, keyboard + touch input, HUD, states, restart, debug hook), `lint` finds outdated or slow Three.js code (old import paths, Clock, PCFSoftShadowMap, RGBELoader, per-frame allocations, missing resize/pixel ratio/dispose)
icon: △
color: cyan
---
Use with the `threejs` skill. `scaffold path=game template=vite` (or `template=cdn` for one HTML page with no build), then grow the game from it. Run `lint path=.` before you report a Three.js change.
"""
import json, re
from pathlib import Path

THREE = "0.186.1"
VITE = "^8.3.4"

INDEX = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>{title}</title>
<link rel="icon" href="data:,">
<style>
  html, body {{ margin: 0; height: 100%; overflow: hidden; background: #0e1116; font: 600 16px system-ui, sans-serif; color: #f4f1ea; }}
  canvas {{ display: block; touch-action: none; }}
  #hud {{ position: fixed; inset: 0; pointer-events: none; padding: max(12px, env(safe-area-inset-top)) 16px; display: flex; justify-content: space-between; text-shadow: 0 1px 3px #000a; }}
  .panel {{ position: fixed; inset: 0; display: grid; place-content: center; text-align: center; gap: 12px; background: #0e1116cc; }}
  .panel[hidden] {{ display: none; }}
  .panel h1 {{ margin: 0; font-size: 40px; letter-spacing: .02em; }}
  .panel button {{ font: inherit; padding: 14px 28px; border: 0; border-radius: 12px; background: #ffb347; color: #1a1300; cursor: pointer; min-height: 48px; }}
  #stick {{ position: fixed; left: 24px; bottom: max(24px, env(safe-area-inset-bottom)); width: 120px; height: 120px; border-radius: 50%; border: 2px solid #ffffff40; display: none; }}
  #knob {{ position: absolute; left: 40px; top: 40px; width: 40px; height: 40px; border-radius: 50%; background: #ffffff80; }}
  @media (pointer: coarse) {{ #stick {{ display: block; }} }}
</style>{importmap}
</head>
<body>
<div id="hud"><span id="score">0</span><span id="time">30.0</span></div>
<div id="stick"><div id="knob"></div></div>
<div class="panel" id="title"><h1>{title}</h1><p>Collect the orbs before time runs out.<br>WASD / arrows · touch: left stick · P pauses</p><button id="play">Play</button></div>
<div class="panel" id="over" hidden><h1 id="result"></h1><button id="again">Play again</button></div>
<script type="module" src="{main}"></script>
</body>
</html>
"""

IMPORTMAP = """
<script type="importmap">
{{"imports": {{"three": "https://cdn.jsdelivr.net/npm/three@{v}/build/three.module.js", "three/addons/": "https://cdn.jsdelivr.net/npm/three@{v}/examples/jsm/"}}}}
</script>"""

MAIN = r"""import * as THREE from 'three';

// ── config ──
const ROUND = 30, ORBS = 12, ARENA = 14, SPEED = 9, ACCEL = 40, STEP = 1 / 60;

// ── seeded RNG: every random gameplay value comes from here, so runs replay the same way ──
let seed = 1;
const rand = () => { seed |= 0; seed = (seed + 0x6d2b79f5) | 0; let t = Math.imul(seed ^ (seed >>> 15), 1 | seed);
  t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t; return ((t ^ (t >>> 14)) >>> 0) / 4294967296; };

// ── renderer, scene, camera ──
const renderer = new THREE.WebGLRenderer({ antialias: true, powerPreference: 'high-performance' });
renderer.setPixelRatio(Math.min(devicePixelRatio, matchMedia('(pointer: coarse)').matches ? 1.5 : 2));
renderer.setSize(innerWidth, innerHeight);
renderer.toneMapping = THREE.ACESFilmicToneMapping;
renderer.shadowMap.enabled = true;
renderer.shadowMap.type = THREE.PCFShadowMap;
document.body.prepend(renderer.domElement);

const scene = new THREE.Scene();
scene.background = new THREE.Color(0x0e1116);
scene.fog = new THREE.Fog(0x0e1116, 25, 60);
const camera = new THREE.PerspectiveCamera(55, innerWidth / innerHeight, 0.1, 100);
addEventListener('resize', () => { camera.aspect = innerWidth / innerHeight; camera.updateProjectionMatrix(); renderer.setSize(innerWidth, innerHeight); });

scene.add(new THREE.HemisphereLight(0xbfd8ff, 0x2a1d10, 1.2));
const sun = new THREE.DirectionalLight(0xffffff, 2.2);
sun.position.set(8, 16, 6);
sun.castShadow = true;
sun.shadow.mapSize.set(1024, 1024);
Object.assign(sun.shadow.camera, { left: -ARENA, right: ARENA, top: ARENA, bottom: -ARENA, near: 1, far: 40 });
sun.shadow.normalBias = 0.02;
scene.add(sun);

const ground = new THREE.Mesh(new THREE.PlaneGeometry(ARENA * 2, ARENA * 2), new THREE.MeshStandardMaterial({ color: 0x2b3a4a, roughness: 0.9 }));
ground.rotation.x = -Math.PI / 2;
ground.receiveShadow = true;
ground.matrixAutoUpdate = false; ground.updateMatrix();
scene.add(ground);
scene.add(new THREE.GridHelper(ARENA * 2, ARENA, 0x3d5266, 0x334455));

const player = new THREE.Mesh(new THREE.BoxGeometry(1, 1, 1), new THREE.MeshStandardMaterial({ color: 0xffb347, roughness: 0.4 }));
player.castShadow = true;
scene.add(player);

// orbs: one InstancedMesh = one draw call for all of them
const orbs = new THREE.InstancedMesh(new THREE.IcosahedronGeometry(0.45, 1), new THREE.MeshStandardMaterial({ color: 0x5ee6c8, emissive: 0x1b6b5c }), ORBS);
orbs.castShadow = true;
scene.add(orbs);
const orbPos = Array.from({ length: ORBS }, () => new THREE.Vector3());
const alive = new Array(ORBS).fill(true);

// ── input: every device fills one object, gameplay reads only that ──
const input = { move: new THREE.Vector2() }, keys = {}, stick = new THREE.Vector2();
addEventListener('keydown', e => { keys[e.code] = true; if (e.code === 'KeyP') setState(state === 'playing' ? 'paused' : state === 'paused' ? 'playing' : state); });
addEventListener('keyup', e => { keys[e.code] = false; });
addEventListener('blur', () => { for (const k in keys) keys[k] = false; if (state === 'playing') setState('paused'); });
const stickEl = document.getElementById('stick'), knob = document.getElementById('knob');
let stickId = null;
stickEl.addEventListener('pointerdown', e => { stickId = e.pointerId; stickEl.setPointerCapture(e.pointerId); moveStick(e); });
stickEl.addEventListener('pointermove', e => { if (e.pointerId === stickId) moveStick(e); });
stickEl.addEventListener('pointerup', () => { stickId = null; stick.set(0, 0); knob.style.transform = ''; });
function moveStick(e) {
  const r = stickEl.getBoundingClientRect();
  stick.set((e.clientX - r.left - r.width / 2) / (r.width / 2), (e.clientY - r.top - r.height / 2) / (r.height / 2));
  if (stick.length() > 1) stick.normalize();
  knob.style.transform = `translate(${stick.x * 40}px, ${stick.y * 40}px)`;
}
function pollInput() {
  const k = c => (keys[c] ? 1 : 0);
  input.move.set(k('KeyD') + k('ArrowRight') - k('KeyA') - k('ArrowLeft'), k('KeyS') + k('ArrowDown') - k('KeyW') - k('ArrowUp')).add(stick);
  if (input.move.length() > 1) input.move.normalize();
}

// ── game state ──
const ui = { score: document.getElementById('score'), time: document.getElementById('time'), title: document.getElementById('title'),
  over: document.getElementById('over'), result: document.getElementById('result') };
let state = 'title', score = 0, timeLeft = ROUND, shake = 0;
const vel = new THREE.Vector3(), want = new THREE.Vector3(), camTarget = new THREE.Vector3(), tmp = new THREE.Matrix4(), hidden = new THREE.Matrix4().makeScale(0, 0, 0);

function reset() {
  seed = 1; score = 0; timeLeft = ROUND; vel.set(0, 0, 0); player.position.set(0, 0.5, 0);
  for (let i = 0; i < ORBS; i++) { orbPos[i].set((rand() * 2 - 1) * (ARENA - 2), 0.6, (rand() * 2 - 1) * (ARENA - 2)); alive[i] = true; }
  syncOrbs(0);
}
function setState(s) {
  state = s;
  ui.title.hidden = s !== 'title' && s !== 'paused';
  ui.over.hidden = s !== 'over';
  if (s === 'paused') ui.title.querySelector('h1').textContent = 'Paused';
}
function syncOrbs(t) {
  for (let i = 0; i < ORBS; i++) {
    if (!alive[i]) { orbs.setMatrixAt(i, hidden); continue; }
    tmp.makeRotationY(t * 2 + i).setPosition(orbPos[i].x, orbPos[i].y + Math.sin(t * 3 + i) * 0.15, orbPos[i].z);
    orbs.setMatrixAt(i, tmp);
  }
  orbs.instanceMatrix.needsUpdate = true;
  orbs.computeBoundingSphere();
}

function fixedUpdate(dt) {
  if (state !== 'playing') return;
  want.set(input.move.x, 0, input.move.y).multiplyScalar(SPEED);
  vel.lerp(want, Math.min(1, ACCEL * dt / SPEED));         // acceleration, not instant speed
  player.position.addScaledVector(vel, dt);
  player.position.x = THREE.MathUtils.clamp(player.position.x, -ARENA + 0.5, ARENA - 0.5);
  player.position.z = THREE.MathUtils.clamp(player.position.z, -ARENA + 0.5, ARENA - 0.5);
  for (let i = 0; i < ORBS; i++) {
    if (alive[i] && orbPos[i].distanceToSquared(player.position) < 0.9) { alive[i] = false; score++; shake = 0.25; }
  }
  timeLeft -= dt;
  if (score === ORBS || timeLeft <= 0) { ui.result.textContent = score === ORBS ? `All ${ORBS}! ${timeLeft.toFixed(1)}s left` : `${score} / ${ORBS}`; setState('over'); }
}

function update(dt, t) {
  if (vel.lengthSq() > 0.01) player.rotation.y = Math.atan2(vel.x, vel.z);
  syncOrbs(t);
  camTarget.set(player.position.x, 11, player.position.z + 10);
  camera.position.lerp(camTarget, 1 - Math.exp(-6 * dt));  // damped follow, frame-rate independent
  if (shake > 0) { shake -= dt; camera.position.x += (Math.random() - 0.5) * shake; }
  camera.lookAt(player.position);
  sun.position.set(player.position.x + 8, 16, player.position.z + 6);
  sun.target.position.copy(player.position); sun.target.updateMatrixWorld();
  ui.score.textContent = String(score);
  ui.time.textContent = Math.max(0, timeLeft).toFixed(1);
}

// ── loop: fixed step for gameplay, variable step for visuals ──
const timer = new THREE.Timer();
timer.connect(document);
let acc = 0;
renderer.setAnimationLoop(now => {
  timer.update(now);
  const dt = Math.min(timer.getDelta(), 0.1);
  acc += dt;
  while (acc >= STEP) { pollInput(); fixedUpdate(STEP); acc -= STEP; }
  update(dt, timer.getElapsed());
  renderer.render(scene, camera);
});

const start = () => { if (state !== 'paused') reset(); setState('playing'); };
document.getElementById('play').onclick = start;
document.getElementById('again').onclick = start;
reset();

// debug / test hook: read state and drive the game from a test or the console
window.__game = { get state() { return state; }, get score() { return score; }, get timeLeft() { return timeLeft; }, player, input, start,
  step(n = 1) { for (let i = 0; i < n; i++) fixedUpdate(STEP); }, ready: true };
"""

VITE_CONFIG = """import { defineConfig } from 'vite';

export default defineConfig({ base: './' });  // relative paths: the build works on itch.io, GitHub Pages or any folder
"""

GAME_MD = """# {title}

Core loop: move (WASD / arrows / touch stick) to collect {orbs} orbs before {round} s run out; restart instantly.

- Run: {run}
- Code: `{main}` — config at the top, input → fixedUpdate (gameplay, 60 Hz) → update (visuals) → render.
- Debug hook: `window.__game` (state, score, timeLeft, player, start(), step(n)).
"""


@action
def scaffold(ctx, path: str = "game", template: str = "vite", title: str = "Orb Rush"):
    "Write a small playable Three.js game to grow from. template: vite (npm project) or cdn (no build, importmap)."
    root = Path(path).expanduser()
    files = {}
    if template == "cdn":
        files["index.html"] = INDEX.format(title=title, importmap=IMPORTMAP.format(v=THREE), main="main.js")
        files["main.js"] = MAIN
        run = "serve the folder over http (`npx vite` or `python -m http.server`), open the page"
        files["GAME.md"] = GAME_MD.format(title=title, orbs=12, round=30, run=run, main="main.js")
    else:
        files["index.html"] = INDEX.format(title=title, importmap="", main="/src/main.js")
        files["src/main.js"] = MAIN
        files["vite.config.js"] = VITE_CONFIG
        files["package.json"] = json.dumps({"name": re.sub(r"[^a-z0-9-]+", "-", title.lower()).strip("-") or "game", "private": True,
                                            "version": "0.1.0", "type": "module",
                                            "scripts": {"dev": "vite", "build": "vite build", "preview": "vite preview"},
                                            "dependencies": {"three": "^" + THREE}, "devDependencies": {"vite": VITE}}, indent=2) + "\n"
        files[".gitignore"] = "node_modules/\ndist/\n"
        files["GAME.md"] = GAME_MD.format(title=title, orbs=12, round=30, run="`npm install`, then `npm run dev`; build: `npm run build`", main="src/main.js")
    done = []
    for rel, text in files.items():
        p = root / rel
        if p.exists():
            ctx.status(f"kept existing {rel}")
            continue
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        done.append(rel)
    nxt = "cd {0} && npm install && npm run dev".format(root) if template != "cdn" else f"serve {root} over http and open index.html"
    return f"wrote {len(done)} file(s) in {root}: {', '.join(done) or 'nothing (all existed)'}\nnext: {nxt}\nthree {THREE}"


RULES = [
    (r"three/examples/jsm/", "import addons from 'three/addons/...' (examples/jsm breaks under an importmap and in newer setups)"),
    (r"three(\.min)?\.js['\"]|build/three\.min\.js", "classic three.js / three.min.js builds are gone (r160+): use ES modules"),
    (r"new\s+THREE\.Clock\s*\(|new\s+Clock\s*\(", "THREE.Clock is deprecated (r183): use THREE.Timer (timer.connect(document); timer.update() each frame)"),
    (r"PCFSoftShadowMap", "PCFSoftShadowMap is deprecated (r186): use PCFShadowMap"),
    (r"RGBELoader", "RGBELoader was replaced by HDRLoader (three/addons/loaders/HDRLoader.js)"),
    (r"physicallyCorrectLights|useLegacyLights", "removed: lights are physical now (point/spot intensity in candela)"),
    (r"outputEncoding|sRGBEncoding|LinearEncoding|\.encoding\s*=", "encodings were removed: use outputColorSpace / texture.colorSpace = THREE.SRGBColorSpace"),
    (r"renderAsync\s*\(", "renderAsync is deprecated (r181): await renderer.init() once, then render()"),
    (r"detectSupportAsync", "detectSupportAsync is deprecated (r181): await renderer.init(), then detectSupport(renderer)"),
    (r"\bGeometry\(\)|new\s+THREE\.Geometry\b|Face3", "THREE.Geometry/Face3 are gone: use BufferGeometry"),
    (r"intersectObjects\(\s*scene\.children\s*,\s*true", "raycasting the whole scene recursively is slow: keep an explicit array of pickable meshes (or use layers)"),
    (r"layers\.set\(", "layers.set(n) removes layer 0 (the camera stops seeing it): use layers.enable(n) unless that is intended"),
    (r"requestAnimationFrame\(", "prefer renderer.setAnimationLoop(fn): pauses with the tab, required for WebXR"),
]


@action
def lint(ctx, path: str = ".", limit: int = 80):
    "Find outdated or slow Three.js code in .js/.ts/.jsx/.tsx/.html files (skips node_modules and builds)."
    root = Path(path).expanduser()
    files = [root] if root.is_file() else [p for p in root.rglob("*") if p.suffix in (".js", ".ts", ".jsx", ".tsx", ".mjs", ".html", ".vue", ".svelte")
                                             and not any(x in p.parts for x in ("node_modules", "dist", "build", ".git", "vendor", "libs"))]
    out, uses_three = [], False
    for p in files[:2000]:
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if "three" not in text and "THREE" not in text:
            continue
        uses_three = True
        rel = p.relative_to(root) if root.is_dir() else p.name
        lines = text.splitlines()
        for i, line in enumerate(lines, 1):
            for rx, why in RULES:
                if re.search(rx, line):
                    out.append(f"{rel}:{i}: {why}")
        loop = re.search(r"setAnimationLoop\(|requestAnimationFrame\(", text)
        if loop:  # allocations inside the per-frame function
            body = text[loop.start():loop.start() + 4000]
            for m in re.finditer(r"new\s+(THREE\.)?(Vector[234]|Quaternion|Matrix4|Euler|Color|Box3|Raycaster)\(", body):
                out.append(f"{rel}:{text[:loop.start() + m.start()].count(chr(10)) + 1}: new {m.group(2)} in the frame loop: allocate once outside and reuse")
        if "WebGLRenderer" in text:
            if "setPixelRatio" not in text:
                out.append(f"{rel}: renderer without setPixelRatio(Math.min(devicePixelRatio, 2)): blurry on HiDPI or too slow without the cap")
            elif re.search(r"setPixelRatio\(\s*(window\.)?devicePixelRatio\s*\)", text):
                out.append(f"{rel}: setPixelRatio(devicePixelRatio) uncapped: use Math.min(devicePixelRatio, 2)")
            if "resize" not in text and "ResizeObserver" not in text:
                out.append(f"{rel}: no resize handler: update camera.aspect + updateProjectionMatrix() + renderer.setSize() on resize")
        if re.search(r"\.load(Async)?\(", text) and "Loader" in text and "dispose" not in text and "scene.remove" in text:
            out.append(f"{rel}: objects are removed but nothing is disposed: dispose geometries, materials and their textures")
    if not uses_three:
        return f"no Three.js code found under {root}"
    if not out:
        return "no issues found"
    return f"{len(out)} issue(s):\n" + "\n".join(out[:int(limit)]) + ("\n…" if len(out) > int(limit) else "")
