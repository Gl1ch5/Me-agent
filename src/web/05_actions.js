// ─── actions ───
async function poll() {
  const my = ++polling;
  while (my === polling) {
    try {
      const r = await api(`${base}/events?after=${after}`);
      if (my !== polling) return;
      for (const e of r.events) { after = Math.max(after, e.seq); onEvent(e); }
      if (r.state) { busy = r.state.busy; setBusy(); }
    } catch (e) {
      if (MODE === "cloud" && /no such cloud session/.test(e.message)) { const w = await api("/api/cloud", {session: state.session?.id, ws: state.ws}); state.worker = w; base = "/api/cloud/" + w.id; after = w.seq || 0; continue; }
      await new Promise(r => setTimeout(r, 2000));
    }
  }
}
async function restartSandbox() {
  if (MODE !== "cloud") return;
  el(`<div class="info warn">${tt("sandbox restarted — your session and files are safe")}</div>`);
  try { const w = await api("/api/cloud", {session: state.session?.id, ws: state.ws}); polling++; apply(await A.attach(w), false); } catch (e) {}
}
function renderChrome() {
  $("mode").textContent = t(MODE === "cloud" ? "cloud" : "local");
  $("who").textContent = state.user || t(MODE === "local" ? "local" : "account");
}
async function apply(s, history = true) {
  state = s; if (s.lang && s.lang !== LANG) setLang(s.lang);
  if (history) renderHistory(s.session); renderSessions(); renderStatus();
  renderChrome();
  after = s.after ?? after;
  if (s.busy) { busy = true; turnT0 = Date.now(); setBusy(); }
  (s.pending || []).forEach(onEvent);
  poll();
}
async function send() {
  let t = $("input").value.trim(); if ((!t && !atts.length) || busy) return;
  const fail = e => el(`<div class="info err">${esc(e.message)}</div>`);
  if (/^\/[a-z0-9]/i.test(t) && !/^\/goal\s/.test(t) && !atts.length && await runSlash(t)) { $("input").value = ""; grow(); return slashUpdate(); }
  const goal = /^\/goal\s+/.test(t);
  if (goal) t = t.replace(/^\/goal\s+/, "").replace(/^"|"$/g, "");
  const images = atts.filter(a => a.image).map(a => a.url);
  const files = [];
  for (const a of atts.filter(a => !a.image)) { try { files.push(await A.upload(a.name, a.b64)); } catch (e) { fail(e); } }
  if (files.length) t += (t ? "\n\n" : "") + "Attached: " + files.join(", ");
  if (!t && images.length) t = "(see the attached image" + (images.length > 1 ? "s" : "") + ")";
  atts = []; renderAtts();
  $("input").value = ""; grow(); userMsg((goal ? "/goal " : "") + t + (images.length ? `  [${images.length} ${pl(images.length, "image", "images")}]` : ""));
  if (untr($("title").textContent) === "New chat") $("title").textContent = t.split("\n")[0].slice(0, 60); cur.echo = true; busy = true; turnT0 = Date.now(); setBusy(); stick = true;
  try { await (goal ? A.goal(t) : A.prompt(t, images)); } catch (e) { fail(e); busy = false; setBusy(); }
}
function sendOrStop() { busy ? api(base + "/cancel", {}) : send(); }
function openSide() { side.classList.add("open"); $("scrim").classList.add("on"); }
function closeSide() { side.classList.remove("open"); $("scrim").classList.remove("on"); }
document.addEventListener("keydown", e => { if (e.key === "Escape") { closeSide(); $("popup").classList.add("hidden"); } });
let touchX = null;  // swipe left on the drawer to close it
side.addEventListener("touchstart", e => touchX = e.touches[0].clientX, {passive: true});
side.addEventListener("touchend", e => { if (touchX !== null && e.changedTouches[0].clientX - touchX < -60) closeSide(); touchX = null; });
async function newChatIn(cwd) {  // a new chat inside a given project folder (sidebar "+")
  closeSide(); polling++;
  try { await api(base + "/new", {cwd}); await apply(await api(base + "/state")); $("title").textContent = t("New chat"); } catch (e) { alert(e.message); }
}
async function newChat() { closeSide(); polling++; busy = false; await apply(await A.newChat()); history.replaceState(null, "", location.pathname); $("title").textContent = t("New chat"); }
async function loadSession(id) { closeSide(); polling++; await apply(await A.load(id)); if (MODE === "cloud") history.replaceState(null, "", "?s=" + id); }
function pickModel() {
  const ms = state.config?.models || [], cur = state.config?.model;
  if (!ms.length) { $("pbody").innerHTML = `<p style="padding:12px;color:var(--dim)">${esc(t("No models — add one in your account or with /models."))}</p>`; return $("popup").classList.remove("hidden"); }
  $("pbody").innerHTML = `<input class="psearch" id="msearch" placeholder="${esc(t("search models…"))}" autocomplete="off" spellcheck="false"><div id="mlist"></div>`
    + `<p class="phint">${esc(t("This chat keeps the model you pick; new chats start with it."))}</p>`;
  const prov = new Set(ms.map(m => m.provider || "")).size > 1;
  let hit = [], sel = 0;
  const draw = () => {
    const q = $("msearch").value.trim().toLowerCase().split(/\s+/).filter(Boolean);
    hit = ms.filter(m => q.every(w => `${m.name} ${m.id} ${m.provider || ""}`.toLowerCase().includes(w)));
    sel = Math.max(0, Math.min(sel, hit.length - 1));
    let last = null;
    $("mlist").innerHTML = hit.map((m, i) => {
      const g = prov && m.provider !== last ? `<div class="pgroup">${esc(m.provider || "")}</div>` : ""; last = m.provider;
      const tags = [m.effort, m.context ? human(m.context) : "", m.vision ? "img" : ""].filter(Boolean).map(x => `<i>${esc(x)}</i>`).join("");
      return g + `<button data-i="${i}" class="${m.name === cur ? "on" : ""}${i === sel ? " sel" : ""}"><span>${m.name === cur ? "●" : "○"}</span>`
        + `<span class="mn"><b>${esc(m.name)}</b>${m.id !== m.name ? `<small>${esc(m.id)}</small>` : ""}</span><span class="tags">${tags}</span></button>`;
    }).join("") || `<p style="padding:12px;color:var(--dim)">${esc(t("nothing found"))}</p>`;
  };
  $("mlist").onclick = e => { const b = e.target.closest("button[data-i]"); if (b) setModel(hit[+b.dataset.i].name); };
  $("msearch").oninput = () => { sel = 0; draw(); };
  $("msearch").onkeydown = e => {
    if (e.key === "ArrowDown" || e.key === "ArrowUp") { e.preventDefault(); sel = (sel + (e.key === "ArrowDown" ? 1 : -1) + hit.length) % Math.max(1, hit.length); draw(); $("mlist").querySelector(".sel")?.scrollIntoView({block: "nearest"}); }
    if (e.key === "Enter" && hit[sel]) setModel(hit[sel].name);
  };
  sel = Math.max(0, ms.findIndex(m => m.name === cur));
  draw();
  $("popup").classList.remove("hidden");
  $("mlist").querySelector(".on")?.scrollIntoView({block: "center"});
  if (!TOUCH) $("msearch").focus();
}
function pickAgent() {
  $("pbody").innerHTML = (state.agents || []).map(a => `<button onclick="setAgent('${esc(a.name)}')"><span>${a.name === state.agent ? "●" : "○"}</span><b>${esc(a.label)}</b><span class="d">${esc(a.description)}</span></button>`).join("")
    + (MODE === "cloud" ? `<a class="more" href="/account#market">${esc(t("more agents in the marketplace →"))}</a>` : "");
  $("popup").classList.remove("hidden");
}
async function setAgent(n) { $("popup").classList.add("hidden"); state.agent = n; renderStatus(); await A.setAgent(n); }
async function setModel(n) {  // switch at once; roll back if the server says no
  $("popup").classList.add("hidden"); const old = state.config.model; if (n === old) return;
  state.config.model = n; renderStatus();
  try { await A.setModel(n); } catch (e) { state.config.model = old; renderStatus(); el(`<div class="info err">${esc(t("could not switch the model: {m}", {m: e.message}))}</div>`); }
}
const inp = $("input");
function grow() { inp.style.height = "auto"; inp.style.height = Math.min(220, inp.scrollHeight) + "px"; setBusy(); }
inp.addEventListener("input", grow);
inp.addEventListener("keydown", e => { if (e.key === "Enter" && !e.shiftKey && !e.isComposing && !TOUCH) { e.preventDefault(); send(); } if (e.key === "Escape" && busy) api(base + "/cancel", {}); });
// voice: hold the mic button (or hold space in an empty input)
let rec = null, chunks = [];
async function recStart() {
  if (rec) return;
  try {
    const stream = await navigator.mediaDevices.getUserMedia({audio: true});
    rec = new MediaRecorder(stream); chunks = [];
    rec.ondataavailable = e => chunks.push(e.data);
    rec.onstop = async () => {
      stream.getTracks().forEach(t => t.stop()); $("mic").classList.remove("rec");
      const blob = new Blob(chunks, {type: rec.mimeType || "audio/webm"}); rec = null;
      if (blob.size < 2000) return;
      inp.placeholder = t("transcribing…");
      try { const r = await A.voice(blob); if (r.text) { inp.value += (inp.value && !inp.value.endsWith(" ") ? " " : "") + r.text; grow(); inp.focus(); } else if (r.detail) alert(r.detail); }
      finally { inp.placeholder = t("Ask anything…"); }
    };
    rec.start(); $("mic").classList.add("rec");
  } catch (e) { alert(t("microphone: {m}", {m: e.message})); }
}
function recStop() { if (rec && rec.state === "recording") rec.stop(); }
const mic = $("mic");
mic.addEventListener("pointerdown", e => { e.preventDefault(); recStart(); });
["pointerup", "pointerleave", "pointercancel"].forEach(ev => mic.addEventListener(ev, recStop));
let spaceT = 0;
inp.addEventListener("keydown", e => { if (e.code === "Space" && !inp.value && e.repeat) { e.preventDefault(); if (!rec) recStart(); } });
inp.addEventListener("keyup", e => { if (e.code === "Space") recStop(); });
$("wsPill").onclick = async () => {
  if (MODE === "cloud") return location.href = "/account#workspaces";
  const native = window.pywebview && window.pywebview.api && window.pywebview.api.pick_folder;  // desktop app: real folder dialog
  const p = native ? await window.pywebview.api.pick_folder(state.cwd || "") : prompt(t("Project folder for the local agent"), state.cwd || "");
  if (!p) return;
  try { await apply(await api(base + "/cwd", {path: p})); $("title").textContent = t("New chat"); } catch (e) { alert(e.message); }
};
if (EMBED) window.addEventListener("message", e => {  // the IDE puts a selection / file reference into the input, or sends a prompt
  if (e.source !== window.parent || !e.data) return;
  if (e.data.type === "new") return newChat();
  if (typeof e.data.text !== "string") return;
  if (e.data.type === "insert") { inp.value += (inp.value && !/\s$/.test(inp.value) ? "\n" : "") + e.data.text; grow(); inp.focus(); inp.setSelectionRange(inp.value.length, inp.value.length); }
  if (e.data.type === "send" && !busy) { inp.value = e.data.text; send(); }
});
A.boot().then(s => { if (s) { apply(s); loadThemes().catch(() => {}); } }).catch(e => el(`<div class="info err">${esc(e.message)}</div>`));
