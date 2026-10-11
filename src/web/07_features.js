// ─── todo checklist (sidebar card + header chip), goals, turn diff review ───
function renderTodo(items) {
  items = items || [];
  const card = $("todoCard"), chip = $("todoChip");
  const done = items.filter(i => i.s === "done").length;
  if (!items.length) { card.classList.add("hidden"); chip.classList.add("hidden"); return; }
  card.classList.remove("hidden");
  card.innerHTML = `<div class="tc-h"><b>Checklist</b><span>${done}/${items.length}</span></div><div class="tc-bar"><i style="width:${(done / items.length * 100).toFixed(0)}%"></i></div>`
    + items.map(i => `<div class="tc-i ${i.s}"><span class="tc-b">${i.s === "done" ? '<svg viewBox="0 0 24 24"><path d="M5 12l5 5 9-11"/></svg>' : i.s === "doing" ? '<span class="spin"></span>' : ""}</span><span>${esc(i.t)}</span></div>`).join("");
  chip.querySelector("span").textContent = `${done}/${items.length}`;
  chip.classList.toggle("hidden", done === items.length && !busy);
}
function goalEvent(g, history) {
  if (g.status === "working") { if (!history) el(`<div class="goal working"><span class="spin"></span><span><b>Goal</b> ${esc(g.text)}</span></div>`); return; }
  const ok = g.status === "done";
  el(`<div class="goal ${ok ? "done" : "blocked"}">${ok ? I.check : I.x}<span><b>${ok ? "Goal done" : "Goal blocked"}</b>${g.reason ? " — " + esc(g.reason) : ""}</span></div>`);
}
function diffCard(files) {
  if (!files.length) return;
  const stat = d => { let a = 0, r = 0; d.split("\n").forEach(l => { if (l[0] === "+" && !l.startsWith("+++")) a++; else if (l[0] === "-" && !l.startsWith("---")) r++; }); return `<span class="add">+${a}</span> <span class="del">−${r}</span>`; };
  const c = el(`<div class="review"><div class="rv-h"><b>Changes in this turn</b><span class="d">${files.length} file${files.length > 1 ? "s" : ""}</span></div>${files.map(f =>
    `<details><summary><span class="rv-p">${esc(shortPath(f.path))}</span>${f.new ? '<span class="tag">new</span>' : ""}${stat(f.diff)}</summary><pre>${diffHtml(f.diff)}</pre></details>`).join("")}
    <div class="btns"><button class="btn ghost" data-a="keep">keep</button><button class="btn ghost danger" data-a="revert">revert</button></div></div>`);
  c.querySelector('[data-a="keep"]').onclick = () => { c.querySelector(".btns").innerHTML = '<span class="dim">kept</span>'; };
  c.querySelector('[data-a="revert"]').onclick = async () => {
    if (busy) return;
    c.querySelector(".btns").innerHTML = '<span class="dim">reverting…</span>';
    try { await A.undo(); c.querySelector(".btns").innerHTML = '<span class="dim">reverted</span>'; } catch (e) { c.querySelector(".btns").innerHTML = `<span class="err">${esc(e.message)}</span>`; }
  };
}
// ─── permissions ───
function pickPerm() {
  if (state.perm === "sandbox") { $("pbody").innerHTML = '<p style="padding:12px;color:var(--dim)">Cloud sessions run in an isolated sandbox, so tools run without asking. Permissions apply to the local agent (<code>me web</code> / terminal).</p>'; return $("popup").classList.remove("hidden"); }
  const P = {read: ["read-only", "no file changes; only allow-listed commands"], ask: ["ask", "edits inside the project are free; commands ask"], full: ["full access", "everything runs without asking"]};
  $("pbody").innerHTML = Object.entries(P).map(([k, [l, d]]) => `<button onclick="setPerm('${k}')"><span>${state.perm === k ? "●" : "○"}</span><b>${l}</b><span class="d">${d}</span></button>`).join("");
  $("popup").classList.remove("hidden");
}
async function setPerm(m) { $("popup").classList.add("hidden"); state.perm = m; renderStatus(); try { await A.setPerm(m); } catch (e) { alert(e.message); } }
// ─── attachments: images go to the model, other files into the project ───
function renderAtts() {
  const box = $("atts");
  box.classList.toggle("hidden", !atts.length);
  box.innerHTML = atts.map((a, i) => `<span class="att">${a.image ? `<img src="${a.url}" alt="">` : '<svg viewBox="0 0 24 24"><path d="M14 3H6a1 1 0 0 0-1 1v16a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1V8z"/><path d="M14 3v5h5"/></svg>'}<span class="an">${esc(a.name)}</span><button onclick="atts.splice(${i},1);renderAtts();setBusy()" aria-label="remove"><svg viewBox="0 0 24 24"><path d="M6 6l12 12M18 6L6 18"/></svg></button></span>`).join("");
}
function shrink(dataUrl, max = 1600) {  // phone photos are huge; models need ~1.5k px at most
  return new Promise(res => { const img = new Image(); img.onload = () => {
    const k = Math.min(1, max / Math.max(img.width, img.height)); if (k === 1 && dataUrl.length < 1.5e6) return res(dataUrl);
    const c = document.createElement("canvas"); c.width = Math.round(img.width * k); c.height = Math.round(img.height * k);
    c.getContext("2d").drawImage(img, 0, 0, c.width, c.height); res(c.toDataURL("image/jpeg", .85)); }; img.onerror = () => res(dataUrl); img.src = dataUrl; });
}
async function addFiles(list) {
  for (const f of list) {
    if (f.size > 20e6) { alert(f.name + ": over 20 MB"); continue; }
    const url = await new Promise(r => { const fr = new FileReader(); fr.onload = () => r(fr.result); fr.readAsDataURL(f); });
    if (f.type.startsWith("image/")) atts.push({name: f.name || "image.png", image: true, url: await shrink(url)});
    else atts.push({name: f.name, b64: url.split(",")[1]});
  }
  renderAtts(); setBusy();
}
$("file").addEventListener("change", e => { addFiles([...e.target.files]); e.target.value = ""; });
$("input").addEventListener("paste", e => { const fs = [...(e.clipboardData?.files || [])]; if (fs.length) { e.preventDefault(); addFiles(fs); } });
document.addEventListener("dragover", e => e.preventDefault());
document.addEventListener("drop", e => { if (e.dataTransfer?.files?.length) { e.preventDefault(); addFiles([...e.dataTransfer.files]); } });
// ─── PWA + push ("done", "waiting for you") ───
if ("serviceWorker" in navigator && location.protocol !== "file:") navigator.serviceWorker.register("/sw.js").then(() => {
  if (MODE === "cloud" && "PushManager" in window && Notification.permission !== "granted") $("bell").classList.remove("hidden");
}).catch(() => {});
async function enablePush() {
  try {
    if (await Notification.requestPermission() !== "granted") return alert("Notifications are blocked for this site — allow them in the browser settings.");
    const {key} = await api("/api/push/key");
    const reg = await navigator.serviceWorker.ready;
    const b = atob(key.replace(/-/g, "+").replace(/_/g, "/") + "=".repeat((4 - key.length % 4) % 4));
    const sub = await reg.pushManager.subscribe({userVisibleOnly: true, applicationServerKey: Uint8Array.from(b, c => c.charCodeAt(0))});
    await api("/api/push/subscribe", sub.toJSON());
    $("bell").classList.add("hidden");
    el(`<div class="info">notifications on — you will hear when a task is done or needs you</div>`);
  } catch (e) { alert("notifications: " + e.message); }
}
