// ─── rendering ───
const log = $("log"), chat = $("chat");
let stick = true;
chat.addEventListener("scroll", () => stick = chat.scrollHeight - chat.scrollTop - chat.clientHeight < 80);
function scroll() { if (stick) chat.scrollTop = chat.scrollHeight; }
let workEl = null;
function el(html) { const d = document.createElement("div"); d.innerHTML = html.trim(); const n = d.firstChild; i18nApply(d); workEl && workEl.parentNode === log ? log.insertBefore(n, workEl) : log.appendChild(n); scroll(); return n; }
function clearLog() { log.innerHTML = ""; live = {}; cur = {}; workEl = null; }
function emptyState() {
  log.innerHTML = `<div class="empty" style="min-height:60vh"><div><h2>${esc(t("what are we"))} <span>${esc(t("building?"))}</span></h2><p>${esc(MODE === "cloud" ? t("Runs in your cloud sandbox · files sync to all your machines") : t("Running locally on this machine"))}</p></div></div>`;
}
function userMsg(t) { if (log.querySelector(".empty")) log.innerHTML = ""; el(`<div class="msg user"><div class="bub">${esc(t)}</div></div>`); }
function shortPath(s) {
  s = String(s ?? "");
  const roots = [state.session?.cwd, ...(state.session?.roots || []), state.cwd, "/work"].filter(Boolean);
  for (const r of roots) s = s.split(r.replace(/\/$/, "") + "/").join("");
  const max = innerWidth < 500 ? 34 : 70;
  return s.length > max ? "…" + s.slice(-(max - 1)) : s;
}
function toolCard(name, label, args, skill, cli) {
  label = shortPath(label);
  const isSkill = name === "skill" && args && args.action;
  const color = cli ? (COLORS[cli.color] || (/^#/.test(cli.color || "") ? cli.color : COLORS.red || "#ff6b7a")) : isSkill ? (COLORS[skill?.color] || "var(--a)") : "var(--a)";
  const title = cli ? `${esc(cli.icon || "▣")} ${esc(cli.label)} · ${esc(cli.agent)}` : isSkill ? `${esc(args.name)} · ${esc(args.action)}` : esc(name);
  if (cli) label = Object.entries(args || {}).map(([k, v]) => `${k}=${JSON.stringify(v)}`).join(" ");
  const c = el(`<div class="tool ${cli ? "cli" : isSkill ? "skill" : ""} ${cli ? "" : "closed"}" style="--sk:${color}"><div class="h" onclick="this.parentNode.classList.toggle('closed')"><span class="ic"><span class="spin"></span></span><b>${title}</b><span class="lab">${esc(isSkill ? JSON.stringify(args.args || {}) : label)}</span><span class="t"></span></div><div class="st hidden"></div><div class="pbar ${cli ? "ind" : "hidden"}"><i></i></div><div class="log hidden"></div><div class="body"></div></div>`);
  if (EMBED && /^(read|write|edit)$/.test(name) && args && typeof args.path === "string") {  // in the IDE: the path opens the file
    const diff = name !== "read", lab = c.querySelector(".lab");  // a changed file opens as a diff against git
    lab.classList.add("link"); lab.title = tt(diff ? "show the changes in the editor" : "open in the editor");
    lab.onclick = ev => { ev.stopPropagation(); parent.postMessage({meOut: {type: "open", path: args.path, line: args.offset || 0, diff}}, "*"); };
  }
  return c;
}
function diffHtml(t) { return t.split("\n").map(l => `<span class="${l[0] === "+" ? "add" : l[0] === "-" ? "del" : ""}">${esc(l)}</span>`).join("\n"); }
function onEvent(e) {
  const t = e.type;
  if (t === "user") { if (!cur.echo) userMsg(e.text); cur.echo = false; return; }
  if (t === "turn_start") { if (e.context) { state.context = e.context; state.agent = e.agent || state.agent; renderStatus(); } busy = true; turnT0 = Date.now(); setBusy(); return; }
  if (t === "reasoning") {
    if (!cur.think) { cur.think = el(`<details class="think"><summary>${I.think}<span class="shimmer">${tt("thinking…")}</span></summary><pre></pre></details>`); cur.thinkT = Date.now(); }
    cur.think.querySelector("pre").textContent += e.text; scroll(); return;
  }
  const endThink = () => { if (cur.think && !cur.thinkDone) { cur.think.querySelector("summary span").outerHTML = tt("thought for {d}", {d: dur((Date.now() - cur.thinkT) / 1000)}); cur.thinkDone = true; } };
  if (t === "text") {
    endThink();
    if (!cur.text) { cur.text = el(`<div class="msg md"></div>`); cur.raw = ""; }
    cur.raw += e.text; const node = cur.text; node._raw = cur.raw;
    if (!node._raf) node._raf = requestAnimationFrame(() => { node._raf = 0; node.innerHTML = md(node._raw); scroll(); });  // once per frame, not per token
    return;
  }
  if (t === "tool_start") {
    endThink(); cur.text = null; cur.think = null; cur.thinkDone = false;
    live[e.id] = toolCard(e.name, e.label, e.args, e.skill, e.cli); return;
  }
  if (t === "tool_event") {
    const c = live[e.id]; if (!c) return;
    if (e.t === "status") { const s = c.querySelector(".st"); s.classList.remove("hidden"); s.textContent = e.text; }
    else if (e.t === "progress") { const p = c.querySelector(".pbar"); p.classList.remove("hidden"); p.classList.toggle("ind", e.v == null); if (e.v != null) p.firstChild.style.width = Math.round(e.v * 100) + "%"; }
    else if (e.t === "table") {
      const rows = e.rows || []; if (!rows.length) return;
      const cols = Array.isArray(rows[0]) ? null : Object.keys(rows[0]);
      const tr = r => "<tr>" + (cols ? cols.map(k => `<td>${esc(r[k])}</td>`) : r.map(x => `<td>${esc(x)}</td>`)).join("") + "</tr>";
      const p = document.createElement("div"); p.className = "panel";
      p.innerHTML = (e.title ? `<b>${esc(e.title)}</b>` : "") + `<table>${cols ? "<tr>" + cols.map(k => `<th>${esc(k)}</th>`).join("") + "</tr>" : ""}${rows.map(tr).join("")}</table>`;
      c.insertBefore(p, c.querySelector(".body"));
    }
    else if (e.t === "md") { const p = document.createElement("div"); p.className = "panel md"; p.innerHTML = md(e.text); c.insertBefore(p, c.querySelector(".body")); }
    else if (e.t === "diff" && e.text.trim()) { c.querySelector(".body").innerHTML = diffHtml(e.text); c.classList.remove("closed"); }
    else { const l = c.querySelector(".log"); l.classList.remove("hidden"); l.textContent = ("› " + e.text + "\n" + l.textContent).split("\n").slice(0, 3).join("\n"); }
    scroll(); return;
  }
  if (t === "tool_end") {
    const c = live[e.id]; if (!c) return; delete live[e.id];
    c.querySelector(".ic").innerHTML = e.ok ? I.check : I.x; c.querySelector(".t").textContent = dur(e.time);
    c.querySelectorAll(c.classList.contains("cli") ? ".st,.log" : ".st,.pbar,.log").forEach(x => x.classList.add("hidden"));
    if (c.classList.contains("cli")) { c.classList.add("done", e.ok ? "ok" : "fail"); c.querySelector(".pbar").classList.remove("ind"); c.classList.add("closed"); }
    if (c.querySelector(".panel")) c.classList.add("haspanel");
    const b = c.querySelector(".body"); if (!b.innerHTML) b.textContent = (e.output || "").slice(0, 20000);
    if (!e.ok) c.classList.remove("closed");
    return;
  }
  if (t === "ask") {
    const opts = e.options || [];
    const c = el(`<div class="confirm ask"><b>${esc(e.text)}</b><div class="btns" style="margin-top:10px;flex-wrap:wrap">${opts.length
      ? opts.map((o, i) => `<button class="btn ${i ? "ghost" : ""}" data-v="${esc(o)}">${esc(o)}</button>`).join("")
      : `<input class="askin" placeholder="answer…" data-i18n-ph="answer…"><button class="btn" data-in="1">${tt("send")}</button>`}</div></div>`);
    const send = v => { api(base + "/answer", {id: e.id, text: v}); c.innerHTML = `<span class="dim">${esc(e.text)} → <b>${esc(v || "—")}</b></span>`; c.className = "info"; };
    c.querySelectorAll("button").forEach(b => b.onclick = () => send(b.dataset.in ? c.querySelector("input").value : b.dataset.v));
    c.querySelector("input")?.addEventListener("keydown", ev => ev.key === "Enter" && send(ev.target.value));
    return;
  }
  if (t === "confirm") {
    const c = el(`<div class="confirm"><b>${tt("Allow {name}?", {name: e.name})}</b><pre>${esc(JSON.stringify(e.args, null, 1)).slice(0, 3000)}</pre><div class="btns"><button class="btn">${tt("allow")}</button><button class="btn ghost">${tt(e.name === "bash" ? "always allow" : "allow all (full)")}</button><button class="btn ghost">${tt("deny")}</button></div></div>`);
    const [y, al, n] = c.querySelectorAll("button");
    y.onclick = () => { api(base + "/confirm", {id: e.id, ok: true}); c.remove(); };
    al.onclick = () => { api(base + "/confirm", {id: e.id, ok: "always"}); if (e.name !== "bash") { state.perm = "full"; A.setPerm("full"); renderStatus(); } c.remove(); };
    n.onclick = () => { api(base + "/confirm", {id: e.id, ok: false}); c.remove(); };
    return;
  }
  if (t === "retry") {  // the provider failed mid-answer: drop what was shown of that attempt
    if (e.discard) { cur.text?.remove(); cur.think?.remove(); cur.text = null; cur.think = null; cur.thinkDone = false; }
    el(`<div class="info warn">↻ ${esc(String(e.error || "").split("\n")[0].slice(0, 140))}</div>`); return;
  }
  if (t === "compacting") { cur.text = null; el(`<div class="info">${tt("compacting history…")}</div>`); return; }
  if (t === "compacted") { state.ctx = e.ctx; renderStatus(); el(`<div class="info">${tt("compacted {a} → {b} messages · ~{c} tokens", {a: e.before, b: e.after, c: human(e.ctx)})}</div>`); return; }
  if (t === "usage") { state.ctx = e.ctx; if (state.session) state.session.usage = {...(state.session.usage || {}), cost: e.session_cost}; if (e.day_cost != null) state.day_cost = e.day_cost; renderStatus(e.in, e.out); return; }
  if (t === "error") { cur.failed = true; el(`<div class="info ${e.text === "interrupted" ? "warn" : "err"}">${e.text === "interrupted" ? tt("interrupted") : esc(e.text)}</div>`); return; }
  if (t === "turn_end") {
    const failed = cur.failed; endThink(); busy = false; cur = {}; setBusy();
    const st = e.steps || 1, cp = e.cached && e.in ? ` (${Math.round(100 * e.cached / e.in)}% ${T("cached")})` : "";
    el(`<div class="stats" title="${esc(T("every step re-sends the context; cached input costs a fraction"))}">${failed ? I.x : I.check}${esc(`${dur(e.time)} · ${st} ${pl(st, "step", "steps")} · ${T("context")} ${human(e.ctx || 0)} · ↑${human(e.in)}${cp} ↓${human(e.out)}${e.cost ? " · " + money(e.cost) : ""}`)}</div>`);
    state.ctx = e.ctx; renderStatus(); refreshSessions(); return;
  }
  if (t === "todo") { if (state.session) state.session.todo = e.items; renderTodo(e.items); return; }
  if (t === "goal") { goalEvent(e); return; }
  if (t === "notice") { cur.text = null; el(`<div class="info${e.level === "info" ? "" : " warn"}">${esc(e.text)}</div>`); return; }
  if (t === "verify") { cur.text = null; el(`<div class="info">${tt("self-check: running checks before finishing")}</div>`); return; }
  if (t === "turn_diff") { diffCard(e.files || []); return; }
  if (t === "undone") { el(`<div class="info warn">↶ ${esc(e.text)}</div>`); A.state(state.worker).then(s => { state.session = s.session; renderHistory(s.session); }); return; }
  if (t === "loaded" && e.session && state.worker) state.worker.session = e.session;  // later reloads must read this session
  if (t === "loaded" && e.title) $("title").textContent = e.title;
  if (t === "reload") { A.state(state.worker).then(s => { state.session = s.session; renderHistory(s.session); renderSessions(); }); return; }
  if (t === "exit") { busy = false; setBusy(); restartSandbox(); }
}
function renderHistory(s) {
  clearLog();
  if (!s || !s.messages || !s.messages.length) renderTodo([]);
  if (!s || !s.messages || !s.messages.length) return emptyState();
  const txt = c => Array.isArray(c) ? c.filter(x => x.type === "text").map(x => x.text).join(" ") : (c || "");
  const outs = {};
  s.messages.forEach(m => { if (m.role === "tool") outs[m.tool_call_id] = m.content; });
  s.messages.forEach(m => {
    if (m.role === "user" && m._aux) {
      const a = txt(m.content);
      if (/^\[message from the user/.test(a)) userMsg(a.split("\n").slice(1).join("\n"));
      else if (/^\[files the user attached with @\]/.test(a)) el(`<div class="info">${esc("attached " + [...a.matchAll(/^\[([^\]\n]+)\]$/gm)].slice(1).map(x => x[1]).join(", "))}</div>`);
    }
    else if (m.role === "user" && txt(m.content) !== "(images from read)") userMsg(txt(m.content));
    else if (m.role === "assistant") {
      if (m.content) el(`<div class="msg md">${md(m.content)}</div>`);
      (m.tool_calls || []).forEach(tc => {
        let a = {}; try { a = JSON.parse(tc.function.arguments); } catch (e) {}
        const c = toolCard(tc.function.name, a.path || a.cmd || a.pattern || a.query || a.url || a.name || "", a);
        c.querySelector(".ic").innerHTML = I.check; c.querySelector(".body").textContent = String(outs[tc.id] || "").slice(0, 8000);
      });
    }
  });
  $("title").textContent = s.title || t("Chat");
  renderTodo(s.todo || []);
  if (s.goal && s.goal.status !== "working") goalEvent(s.goal, true);
  setTimeout(() => chat.scrollTop = chat.scrollHeight, 0);
}
function renderStatus(tin, tout) {
  const m = (state.config?.models || []).find(x => x.name === state.config?.model);
  $("model").textContent = m ? m.name + (m.effort ? " · " + m.effort : "") : t("no model");
  const ctx = m?.context || 0, used = state.ctx || 0, f = ctx ? Math.min(1, used / ctx) : 0;
  const col = f < .6 ? "var(--ok)" : f < .85 ? "var(--warn)" : "var(--err)";
  $("ctx").innerHTML = ctx ? `${t("ctx")} <span class="bar"><i style="width:${(f * 100).toFixed(1)}%;background:${col}"></i></span> ${human(used)}/${human(ctx)} ${Math.round(f * 100)}%` : `${t("ctx")} ${human(used)}`;
  const u = state.session?.usage || {};
  $("tok").textContent = tin !== undefined ? `${t("this task")} ↑${human(tin)} ↓${human(tout)}` : `${t("session")} ↑${human(u.in)} ↓${human(u.out)}`;
  const ag = (state.agents || []).find(a => a.name === state.agent);
  $("agentName").textContent = ag ? ag.label : (state.agent || "code");
  const cf = state.context || [];
  $("ctxfiles").innerHTML = cf.length ? `<span class="cf on" title="${esc(t("loaded into every turn"))}">◆ ${cf.map(esc).join(" · ")}</span>` : `<span class="cf" title="${esc(t("ask the agent to create it, or /init in the terminal"))}">◇ ${esc(t("no AGENTS.md"))}</span>`;
  const cost = state.session?.usage?.cost || 0;
  $("cost").textContent = cost || state.day_cost ? money(cost) + (state.day_cost ? " · " + t("today") + " " + money(state.day_cost) : "") : "";
  const pm = state.perm || "ask";
  $("perm").textContent = {read: t("read-only"), ask: t("ask"), full: t("full access"), sandbox: t("sandbox")}[pm] || pm;
  $("perm").className = "perm " + pm;
  if (state.preview) { $("previewBtn").href = state.preview + "/"; $("previewBtn").classList.remove("hidden"); } else $("previewBtn").classList.add("hidden");
  $("ws").textContent = state.ws || (MODE === "local" ? (state.cwd || t("local")).split(/[\\/]/).pop() : t("cloud"));
}
function renderSessions() {
  const ss = state.sessions || [], sid = state.session?.id;
  const item = s => `<button class="si ${s.id === sid ? "on" : ""}" onclick="loadSession('${esc(s.id)}')" title="${esc(s.cwd || "")}">${esc(s.title || t("(untitled)"))}</button>`;
  const projects = {};  // Codex-style: chats grouped by project folder, the most recently used project first
  if (MODE === "local") ss.forEach(s => (projects[s.cwd || ""] ??= []).push(s));
  if (MODE === "local" && Object.keys(projects).length) {
    const cur = state.cwd || "";
    if (cur && !projects[cur]) projects[cur] = [];
    const order = Object.keys(projects).sort((a, b) => (a === cur ? -1 : b === cur ? 1 : 0) || Math.max(0, ...projects[b].map(x => x.updated || 0)) - Math.max(0, ...projects[a].map(x => x.updated || 0)));
    $("slist").innerHTML = order.map(p => {
      const name = p.split(/[\\/]/).filter(Boolean).pop() || p || t("(no folder)"), open = p === cur || projects[p].some(x => x.id === sid);
      return `<details class="proj" ${open ? "open" : ""}><summary title="${esc(p)}"><svg viewBox="0 0 24 24"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg><span>${esc(name)}</span><b class="n">${projects[p].length || ""}</b><button class="ib pnew" title="${esc(t("New chat"))}" onclick="event.preventDefault();event.stopPropagation();newChatIn('${esc(p.replace(/\\/g, "\\\\").replace(/'/g, "\\'"))}')"><svg viewBox="0 0 24 24"><path d="M12 5v14M5 12h14"/></svg></button></summary>${projects[p].slice(0, 40).map(item).join("") || `<div class="lbl">${esc(t("no chats yet"))}</div>`}</details>`;
    }).join("") + `<button class="si addp" onclick="$('wsPill').click()">+ ${esc(t("open a project folder"))}</button>`;
    return;
  }
  const groups = {};
  ss.forEach(s => { const d = new Date((s.updated || 0) * 1000), age = (Date.now() - d) / 864e5;
    const g = age < 1 ? t("Today") : age < 7 ? t("This week") : t("Older"); (groups[g] ??= []).push(s); });
  $("slist").innerHTML = Object.entries(groups).map(([g, l]) => `<div class="lbl">${g}</div>` + l.map(item).join("")).join("") || `<div class="lbl">${esc(t("no chats yet"))}</div>`;
}
async function refreshSessions() { try { const s = await A.state(state.worker); state.sessions = s.sessions; if (!state.session || state.session.id === s.session?.id) state.session = s.session || state.session; renderSessions(); } catch (e) {} }
function setBusy() {
  $("sendIcon").innerHTML = busy ? I.stop : I.up;
  $("send").disabled = !busy && !$("input").value.trim() && !atts.length;
  if (busy && (!workEl || workEl.parentNode !== log)) {
    workEl = document.createElement("div"); workEl.className = "working";
    workEl.innerHTML = '<span class="spin"></span><span class="shimmer" data-t="working…">'+t('working…')+'</span><span class="el"></span>'; log.appendChild(workEl); scroll();
  }
  if (!busy && workEl) { workEl.remove(); workEl = null; }
}
setInterval(() => { const w = document.querySelector(".working .el"); if (w && busy) w.textContent = dur((Date.now() - turnT0) / 1000); }, 200);
