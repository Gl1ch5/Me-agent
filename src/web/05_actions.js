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
  state = s; if (history) renderHistory(s.session); renderSessions(); renderStatus();
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
async function newChat() { closeSide(); await apply(await A.newChat()); history.replaceState(null, "", location.pathname); $("title").textContent = t("New chat"); }
async function loadSession(id) { closeSide(); polling++; await apply(await A.load(id)); if (MODE === "cloud") history.replaceState(null, "", "?s=" + id); }
function pickModel() {
  const ms = state.config?.models || [];
  $("pbody").innerHTML = ms.map(m => `<button onclick="setModel('${esc(m.name)}')"><span>${m.name === state.config.model ? "●" : "○"}</span>${esc(m.name)}<span class="d">${esc(m.id)}</span></button>`).join("") || `<p style="padding:12px;color:var(--dim)">${esc(t("No models — add one in your account or with /models."))}</p>`;
  $("popup").classList.remove("hidden");
}
function pickAgent() {
  $("pbody").innerHTML = (state.agents || []).map(a => `<button onclick="setAgent('${esc(a.name)}')"><span>${a.name === state.agent ? "●" : "○"}</span><b>${esc(a.label)}</b><span class="d">${esc(a.description)}</span></button>`).join("")
    + (MODE === "cloud" ? `<a class="more" href="/account#market">${esc(t("more agents in the marketplace →"))}</a>` : "");
  $("popup").classList.remove("hidden");
}
async function setAgent(n) { $("popup").classList.add("hidden"); state.agent = n; renderStatus(); await A.setAgent(n); }
async function setModel(n) { $("popup").classList.add("hidden"); await A.setModel(n); state.config.model = n; renderStatus(); }
const inp = $("input");
function grow() { inp.style.height = "auto"; inp.style.height = Math.min(220, inp.scrollHeight) + "px"; setBusy(); }
inp.addEventListener("input", grow);
inp.addEventListener("keydown", e => { if (e.key === "Enter" && !e.shiftKey && !e.isComposing && innerWidth > 760) { e.preventDefault(); send(); } if (e.key === "Escape" && busy) api(base + "/cancel", {}); });
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
  const p = prompt(t("Project folder for the local agent"), state.cwd || "");
  if (!p) return;
  try { await apply(await api(base + "/cwd", {path: p})); $("title").textContent = t("New chat"); } catch (e) { alert(e.message); }
};
A.boot().then(s => { if (s) { apply(s); loadThemes().catch(() => {}); } }).catch(e => el(`<div class="info err">${esc(e.message)}</div>`));
