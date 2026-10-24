// ─── todo checklist (sidebar card + header chip), goals, turn diff review ───
function renderTodo(items) {
  items = items || [];
  const card = $("todoCard"), chip = $("todoChip");
  const done = items.filter(i => i.s === "done").length;
  if (!items.length) { card.classList.add("hidden"); chip.classList.add("hidden"); return; }
  card.classList.remove("hidden");
  card.innerHTML = `<div class="tc-h"><b>${esc(t("Checklist"))}</b><span>${done}/${items.length}</span></div><div class="tc-bar"><i style="width:${(done / items.length * 100).toFixed(0)}%"></i></div>`
    + items.map(i => `<div class="tc-i ${i.s}"><span class="tc-b">${i.s === "done" ? '<svg viewBox="0 0 24 24"><path d="M5 12l5 5 9-11"/></svg>' : i.s === "doing" ? '<span class="spin"></span>' : ""}</span><span>${esc(i.t)}</span></div>`).join("");
  chip.querySelector("span").textContent = `${done}/${items.length}`;
  chip.classList.toggle("hidden", done === items.length && !busy);
}
function goalEvent(g, history) {
  if (g.status === "working") { if (!history) el(`<div class="goal working"><span class="spin"></span><span><b>${tt("Goal")}</b> ${esc(g.text)}</span></div>`); return; }
  const ok = g.status === "done";
  el(`<div class="goal ${ok ? "done" : "blocked"}">${ok ? I.check : I.x}<span><b>${tt(ok ? "Goal done" : "Goal blocked")}</b>${g.reason ? " — " + esc(g.reason) : ""}</span></div>`);
}
function diffCard(files) {
  if (!files.length) return;
  // undo reverts the newest turn: an older card's buttons would revert the wrong one
  document.querySelectorAll(".review .btns").forEach(b => { if (b.querySelector("[data-a]")) b.innerHTML = `<span class="dim">${tt("a later turn changed files — /undo reverts the newest first")}</span>`; });
  const stat = d => { let a = 0, r = 0; d.split("\n").forEach(l => { if (l[0] === "+" && !l.startsWith("+++")) a++; else if (l[0] === "-" && !l.startsWith("---")) r++; }); return `<span class="add">+${a}</span> <span class="del">−${r}</span>`; };
  const c = el(`<div class="review"><div class="rv-h"><b>${tt("Changes in this turn")}</b><span class="d">${tt("{n} {files}", {n: files.length, files: pl(files.length, "file", "files")})}</span></div>${files.map(f =>
    `<details><summary><span class="rv-p">${esc(shortPath(f.path))}</span>${f.new ? `<span class="tag">${tt("new")}</span>` : ""}${stat(f.diff)}</summary><pre>${diffHtml(f.diff)}</pre></details>`).join("")}
    <div class="btns"><button class="btn ghost" data-a="keep">${tt("keep")}</button><button class="btn ghost danger" data-a="revert">${tt("revert")}</button></div></div>`);
  c.querySelector('[data-a="keep"]').onclick = () => { c.querySelector(".btns").innerHTML = `<span class="dim">${tt("kept")}</span>`; };
  c.querySelector('[data-a="revert"]').onclick = async () => {
    if (busy) return;
    c.querySelector(".btns").innerHTML = `<span class="dim">${tt("reverting…")}</span>`;
    try { await A.undo(); c.querySelector(".btns").innerHTML = `<span class="dim">${tt("reverted")}</span>`; } catch (e) { c.querySelector(".btns").innerHTML = `<span class="err">${esc(e.message)}</span>`; }
  };
}
// ─── permissions ───
function pickPerm() {
  if (state.perm === "sandbox") { $("pbody").innerHTML = `<p style="padding:12px;color:var(--dim)">${esc(t("Cloud sessions run in an isolated sandbox, so tools run without asking. Permissions apply to the local agent"))} (<code>me web</code> / ${esc(t("terminal"))}).</p>`; return $("popup").classList.remove("hidden"); }
  const P = {read: [t("read-only"), t("no file changes; only allow-listed commands")], ask: [t("ask"), t("edits inside the project are free; commands ask")], full: [t("full access"), t("everything runs without asking")]};
  $("pbody").innerHTML = Object.entries(P).map(([k, [l, d]]) => `<button onclick="setPerm('${k}')"><span>${state.perm === k ? "●" : "○"}</span><b>${l}</b><span class="d">${d}</span></button>`).join("");
  $("popup").classList.remove("hidden");
}
async function setPerm(m) { $("popup").classList.add("hidden"); state.perm = m; renderStatus(); try { await A.setPerm(m); } catch (e) { alert(e.message); } }
// ─── attachments: images go to the model, other files into the project ───
function renderAtts() {
  const box = $("atts");
  box.classList.toggle("hidden", !atts.length);
  box.innerHTML = atts.map((a, i) => `<span class="att">${a.image ? `<img src="${a.url}" alt="">` : '<svg viewBox="0 0 24 24"><path d="M14 3H6a1 1 0 0 0-1 1v16a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1V8z"/><path d="M14 3v5h5"/></svg>'}<span class="an">${esc(a.name)}</span><button onclick="atts.splice(${i},1);renderAtts();setBusy()" aria-label="${esc(t("remove"))}"><svg viewBox="0 0 24 24"><path d="M6 6l12 12M18 6L6 18"/></svg></button></span>`).join("");
}
function shrink(dataUrl, max = 1600) {  // phone photos are huge; models need ~1.5k px at most
  return new Promise(res => { const img = new Image(); img.onload = () => {
    const k = Math.min(1, max / Math.max(img.width, img.height)); if (k === 1 && dataUrl.length < 1.5e6) return res(dataUrl);
    const c = document.createElement("canvas"); c.width = Math.round(img.width * k); c.height = Math.round(img.height * k);
    c.getContext("2d").drawImage(img, 0, 0, c.width, c.height); res(c.toDataURL("image/jpeg", .85)); }; img.onerror = () => res(dataUrl); img.src = dataUrl; });
}
async function addFiles(list) {
  for (const f of list) {
    if (f.size > 20e6) { alert(t("{name}: over 20 MB", {name: f.name})); continue; }
    const url = await new Promise(r => { const fr = new FileReader(); fr.onload = () => r(fr.result); fr.readAsDataURL(f); });
    if (f.type.startsWith("image/")) atts.push({name: f.name || "image.png", image: true, url: await shrink(url)});
    else atts.push({name: f.name, b64: url.split(",")[1]});
  }
  renderAtts(); setBusy();
}
$("file").addEventListener("change", e => { addFiles([...e.target.files]); e.target.value = ""; });
$("input").addEventListener("paste", e => {  // screenshots come as clipboard items in some browsers, not as files
  const cd = e.clipboardData; let fs = [...(cd?.files || [])];
  if (!fs.length) fs = [...(cd?.items || [])].filter(i => i.kind === "file").map(i => i.getAsFile()).filter(Boolean);
  if (fs.length) { e.preventDefault(); addFiles(fs); }
});
document.addEventListener("dragover", e => e.preventDefault());
document.addEventListener("drop", e => { if (e.dataTransfer?.files?.length) { e.preventDefault(); addFiles([...e.dataTransfer.files]); } });
// ─── PWA + push ("done", "waiting for you") ───
if ("serviceWorker" in navigator && location.protocol !== "file:") navigator.serviceWorker.register("/sw.js").then(() => {
  if (MODE === "cloud" && "PushManager" in window && Notification.permission !== "granted") $("bell").classList.remove("hidden");
}).catch(() => {});
async function enablePush() {
  try {
    if (await Notification.requestPermission() !== "granted") return alert(t("Notifications are blocked for this site — allow them in the browser settings."));
    const {key} = await api("/api/push/key");
    const reg = await navigator.serviceWorker.ready;
    const b = atob(key.replace(/-/g, "+").replace(/_/g, "/") + "=".repeat((4 - key.length % 4) % 4));
    const sub = await reg.pushManager.subscribe({userVisibleOnly: true, applicationServerKey: Uint8Array.from(b, c => c.charCodeAt(0))});
    await api("/api/push/subscribe", sub.toJSON());
    $("bell").classList.add("hidden");
    el(`<div class="info">${tt("notifications on — you will hear when a task is done or needs you")}</div>`);
  } catch (e) { alert(t("notifications: {m}", {m: e.message})); }
}

setLang(LANG);

// ─── project files (local agent): browse folders, read files ───
async function openFiles(dir) {
  if (MODE !== "local") return location.href = "/account#workspaces";
  let r; try { r = await api(base + "/files", {path: dir}); } catch (e) { return alert(e.message); }
  const up = dir ? dir.split("/").slice(0, -1).join("/") : null;
  const size = n => n < 1024 ? n + " B" : n < 1048576 ? (n / 1024).toFixed(1) + " KB" : (n / 1048576).toFixed(1) + " MB";
  $("pbody").innerHTML = `<div class="fhead"><b>${esc((state.cwd || "").split(/[\\/]/).pop())}</b><span class="d">/${esc(dir)}</span></div>`
    + (up !== null ? `<button data-d="${esc(up)}"><span>↰</span>..</button>` : "")
    + r.items.map(i => i.dir ? `<button data-d="${esc((dir ? dir + "/" : "") + i.name)}"><svg viewBox="0 0 24 24"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg>${esc(i.name)}</button>`
      : `<button data-f="${esc((dir ? dir + "/" : "") + i.name)}"><svg viewBox="0 0 24 24"><path d="M14 3H6a1 1 0 0 0-1 1v16a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1V8z"/></svg>${esc(i.name)}<span class="d">${size(i.size)}</span></button>`).join("")
    + (r.items.length ? "" : `<p class="dim" style="padding:12px">${esc(t("empty folder"))}</p>`);
  $("pbody").querySelectorAll("[data-d]").forEach(b => b.onclick = () => openFiles(b.dataset.d));
  $("pbody").querySelectorAll("[data-f]").forEach(b => b.onclick = () => viewFile(b.dataset.f));
  $("popup").classList.remove("hidden");
}
async function viewFile(path) {
  let r; try { r = await api(base + "/file", {path}); } catch (e) { return alert(e.message); }
  const dir = path.split("/").slice(0, -1).join("/");
  $("pbody").innerHTML = `<div class="fhead"><button class="ib" id="fback" title="${esc(t("back"))}">↰</button><b>${esc(path)}</b><button class="ib" id="fask" title="${esc(t("ask about this file"))}"><svg viewBox="0 0 24 24"><path d="M4 5h16v11H8l-4 4z"/></svg></button></div>`
    + (r.note ? `<p class="dim" style="padding:12px">${esc(r.note)}</p>` : `<pre class="fview">${r.text.split("\n").slice(0, 5000).map((l, i) => `<i>${i + 1}</i>${esc(l)}`).join("\n")}</pre>`);
  $("fback").onclick = () => openFiles(dir);
  $("fask").onclick = () => { $("popup").classList.add("hidden"); inp.value = (inp.value ? inp.value + " " : "") + "@" + path + " "; grow(); inp.focus(); };
}
