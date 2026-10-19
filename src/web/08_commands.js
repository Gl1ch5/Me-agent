// ─── slash commands: the same set in the local web UI and in the cloud ───
Object.assign(RU, {
  "show commands & keys": "команды и клавиши", "switch agent": "сменить агента", "create or update AGENTS.md": "создать или обновить AGENTS.md", "switch model": "сменить модель",
  "reasoning effort": "уровень рассуждений", "edit your system prompt": "системный промпт", "list / toggle skills": "навыки: список и переключатели",
  "new chat": "новый чат", "past chats": "прошлые чаты", "copy last answer": "скопировать последний ответ", "summarize history to free context": "сжать историю",
  "auto-compact threshold": "порог автосжатия", "work autonomously until done": "работать автономно до результата", "revert the last turn's files": "откатить файлы последнего хода",
  "permissions": "разрешения", "spending": "расходы", "the agent's checklist": "чек-лист агента", "full access on/off": "полный доступ вкл/выкл", "sync now": "синхронизировать",
  "color theme": "цветовая тема", "project folder / workspaces": "папка проекта / рабочие области", "open on the site": "открыть на сайте", "do this in the terminal": "это делается в терминале",
  "unknown command": "неизвестная команда", "Commands": "Команды", "no skills yet": "навыков пока нет", "copied": "скопировано", "copy failed": "не удалось скопировать", "nothing to copy": "нечего копировать",
  "saved": "сохранено", "nothing to retry": "нечего повторять", "take back the last request and run it again": "отменить последний запрос и выполнить заново",
  "save this chat as Markdown": "сохранить чат в Markdown", "switches: self-testing, todo, web, plan first, brief…": "переключатели: самопроверка, todo, веб, сначала план, кратко…",
  "verify changes itself (tests, build, run)": "сам проверяет изменения (тесты, сборка, запуск)", "keep a todo checklist on multi-step tasks": "ведёт todo-список в многошаговых задачах",
  "search the web for current facts and docs": "ищет в интернете актуальные факты и доки", "show a plan and wait for a go before big changes": "перед большими изменениями показывает план и ждёт «да»",
  "very short replies": "очень короткие ответы", "explain the why (learning mode)": "объясняет «почему» (режим обучения)", "git commit each finished task": "git-коммит после каждой задачи",
  "offer to create AGENTS.md in new projects": "предлагает создать AGENTS.md в новых проектах", "System prompt (empty = default)": "Системный промпт (пусто = по умолчанию)", "Spending": "Расходы", "session": "сессия", "today": "сегодня",
});
const EFFORTS = ["none", "minimal", "low", "medium", "high", "xhigh"];
const note = (txt, cls = "") => el(`<div class="info ${cls}">${esc(txt)}</div>`);
const say = txt => el(`<div class="msg md">${md(txt)}</div>`);
const BEHAVIOR_LABELS = {tests: "verify changes itself (tests, build, run)", todo: "keep a todo checklist on multi-step tasks", web: "search the web for current facts and docs",
  plan_first: "show a plan and wait for a go before big changes", brief: "very short replies", explain: "explain the why (learning mode)",
  commit: "git commit each finished task", agents_md: "offer to create AGENTS.md in new projects"};
const BEHAVIOR_DEFAULTS = {tests: true, todo: true, web: true, plan_first: false, brief: false, explain: false, commit: false, agents_md: true};
function applySetting(cfg, key, v, model) {  // mirror of apply_setting() in the agent, for the cloud config doc
  cfg.settings = cfg.settings || {};
  if (key.startsWith("behavior:")) { cfg.settings.behavior = {...(cfg.settings.behavior || {}), [key.slice(9)]: !!v}; if (key === "behavior:tests") delete cfg.settings.no_verify; return; }
  if (key === "prompt") cfg.prompt = String(v || "");
  else if (key === "autocompact") cfg.settings.autocompact = (v === "off" || v === 0 || v == null) ? 0 : Math.max(10, Math.min(95, parseInt(v)));
  else if (key === "effort") { const m = (cfg.models || []).find(x => x.name === (model || cfg.model)); if (!m) throw new Error("no model"); m.effort = (v === "none" || !v) ? "" : String(v); }
  else if (key.startsWith("skill:")) { const d = new Set(cfg.settings.disabled_skills || []); v ? d.delete(key.slice(6)) : d.add(key.slice(6)); cfg.settings.disabled_skills = [...d].sort(); }
  else throw new Error("unknown setting " + key);
}
async function cfgSet(key, value) {
  if (MODE === "local") { const s = await api(base + "/config", {key, value}); state.config = s.config; return; }
  const d = await api("/api/doc?key=config"), cfg = JSON.parse(d.content);
  applySetting(cfg, key, value, state.config?.model);
  await api("/api/doc", {key: "config", content: JSON.stringify(cfg, null, 1)}, "PUT"); await api(base + "/sync", {}).catch(() => {});
  state.config = (await A.state(state.worker)).config;
}
function popupList(rows, onPick, extra = "") {  // rows: [value, label, desc, on]
  $("pbody").innerHTML = rows.map(([v, l, d, on], i) => `<button data-i="${i}"><span>${on ? "●" : "○"}</span><b>${esc(l)}</b><span class="d">${esc(d || "")}</span></button>`).join("") + extra;
  $("pbody").querySelectorAll("button").forEach(b => b.onclick = () => { $("popup").classList.add("hidden"); onPick(rows[+b.dataset.i][0]); });
  $("popup").classList.remove("hidden");
}
const site = (pane, what) => MODE === "cloud" ? (location.href = "/account#" + pane) : note(`/${what} — ${t("do this in the terminal")}`, "warn");
const lastAnswer = () => { const m = [...log.querySelectorAll(".msg.md")].pop(); return m ? m.innerText : ""; };

const CMDS = {
  help: ["show commands & keys", "", () => say("## " + t("Commands") + "\n" + Object.entries(CMDS).map(([n, c]) => `- \`/${n}${c[1] ? " " + c[1] : ""}\` ${t(c[0])}`).join("\n")
    + "\n\n`enter` send · `shift+enter` newline · `esc` stop · `/` commands")],
  agent: ["switch agent", "[name]", a => { const x = (state.agents || []).find(g => g.name === a); x ? setAgent(x.name) : (a ? note(t("unknown command") + ": " + a, "err") : pickAgent()); }],
  init: ["create or update AGENTS.md", "", () => A.prompt("Create or update AGENTS.md for this project. Explore it first (structure, manifests, scripts, README, tests). Write a concise file: purpose; stack; exact verified commands to install/run/test/build; folder map; conventions; gotchas. It is loaded into every session, so keep it short and accurate.").then(() => { userMsg("/init"); busy = true; turnT0 = Date.now(); setBusy(); cur.echo = true; })],
  model: ["switch model", "[name]", a => { const m = (state.config?.models || []).find(x => x.name === a); m ? setModel(m.name) : pickModel(); }],
  effort: ["reasoning effort", "[none…xhigh]", async a => {
    const go = async v => { await cfgSet("effort", v); renderStatus(); note("effort → " + v, "ok"); };
    EFFORTS.includes(a) ? go(a) : popupList(EFFORTS.map(e => [e, e, "", e === ((state.config?.models || []).find(m => m.name === state.config.model)?.effort || "none")]), go);
  }],
  prompt: ["edit your system prompt", "", async () => {
    const v = prompt(t("System prompt (empty = default)"), state.config?.prompt || ""); if (v === null) return;
    await cfgSet("prompt", v); note(t("saved"), "ok");
  }],
  skills: ["list / toggle skills", "", async () => {
    let list;
    if (MODE === "local") list = (await api(base + "/skills")).skills;
    else { const dis = new Set((JSON.parse((await api("/api/doc?key=config")).content).settings || {}).disabled_skills || []);
      list = (await api("/api/docs")).filter(d => d.key.startsWith("skill/")).map(d => d.key.slice(6).replace(/\.(py|md)$/, "")).map(n => ({name: n, description: "", on: !dis.has(n)})); }
    if (!list.length) return note(t("no skills yet"));
    popupList(list.map(s => [s, s.name, s.description, s.on]), async s => { await cfgSet("skill:" + s.name, !s.on); note(`${s.name} ${s.on ? "off" : "on"}`, "ok"); });
  }],
  plugins: ["extensions that modify me", "", () => site("plugins", "plugins")],
  models: ["add / edit models", "", () => site("models", "models")],
  provider: ["API providers", "", () => site("models", "provider")],
  secret: ["env secrets for skills", "", () => site("models", "secret")],
  mcp: ["MCP servers", "", () => site("models", "mcp")],
  new: ["new chat", "", () => newChat()],
  clear: ["new chat", "", () => newChat()],
  sessions: ["past chats", "", () => openSide()],
  workspace: ["project folder / workspaces", "", () => $("wsPill").click()],
  copy: ["copy last answer", "", async () => { const x = lastAnswer(); if (!x) return note(t("nothing to copy"), "warn"); const ok = await copyText(x); note(t(ok ? "copied" : "copy failed"), ok ? "ok" : "err"); }],
  compact: ["summarize history to free context", "", () => api(base + "/compact", {})],
  autocompact: ["auto-compact threshold", "[50…95|off]", async a => {
    const go = async v => { await cfgSet("autocompact", v); note("auto-compact → " + (v === "off" ? "off" : v + "%"), "ok"); };
    a ? go(a) : popupList(["50", "70", "80", "90", "off"].map(v => [v, v === "off" ? v : v + "%", "", String(state.config?.autocompact ?? 80) === v]), go);
  }],
  goal: ["work autonomously until done", "<objective>", a => a ? (inp.value = "/goal " + a, send()) : note("/goal <objective>", "warn")],
  undo: ["revert the last turn's files", "", () => A.undo().then(() => 0)],
  retry: ["take back the last request and run it again", "", async () => {
    const ms = state.session?.messages || [], i = ms.map(m => m.role === "user" && !m._aux).lastIndexOf(true);
    if (i < 0) return note(t("nothing to retry"), "warn");
    const c = ms[i].content, text = Array.isArray(c) ? c.filter(x => x.type === "text").map(x => x.text).join(" ") : c;
    await api(base + "/retry", {});
    state.session = {...state.session, messages: ms.slice(0, i)}; renderHistory(state.session);
    inp.value = text; send();
  }],
  export: ["save this chat as Markdown", "", () => {
    const s = state.session; if (!s?.messages?.length) return note(t("nothing to copy"), "warn");
    const txt = c => Array.isArray(c) ? c.filter(x => x.type === "text").map(x => x.text).join(" ") : (c || "");
    const out = ["# " + (s.title || "chat"), ""];
    for (const m of s.messages) {
      if (m.role === "user" && !m._aux) out.push("## You", "", txt(m.content), "");
      else if (m.role === "assistant") { if (m.content) out.push("## me", "", txt(m.content), ""); (m.tool_calls || []).forEach(tc => out.push("- `" + tc.function.name + "` " + tc.function.arguments.slice(0, 120))); }
    }
    const a = document.createElement("a"); a.href = URL.createObjectURL(new Blob([out.join("\n")], {type: "text/markdown"}));
    a.download = "me-chat-" + (s.id || "chat") + ".md"; a.click(); setTimeout(() => URL.revokeObjectURL(a.href), 1000);
  }],
  behavior: ["switches: self-testing, todo, web, plan first, brief…", "[name on|off]", async a => {
    const B = state.behavior || {}, [k, v] = (a || "").split(/\s+/);
    const go = async (key, on) => { await cfgSet("behavior:" + key, on); state.behavior = {...(state.behavior || {}), [key]: on}; note(`${key} ${on ? "on" : "off"}`, "ok"); };
    if (k && k in B) return go(k, v ? /^(on|1|true|yes)$/.test(v) : !B[k]);
    const show = () => popupList(Object.keys(B).map(n => [n, t(BEHAVIOR_LABELS[n] || n), n, state.behavior[n]]), async n => { await go(n, !state.behavior[n]); show(); });
    show();
  }],
  perm: ["permissions", "[read|ask|full]", a => ["read", "ask", "full"].includes(a) ? setPerm(a) : pickPerm()],
  yolo: ["full access on/off", "", () => setPerm(state.perm === "full" ? "ask" : "full")],
  cost: ["spending", "", () => {
    const u = state.session?.usage || {};
    say(`## ${t("Spending")}\n- ${t("session")}: **${money(u.cost || 0)}** · ↑${human(u.in || 0)} ↓${human(u.out || 0)}\n- ${t("today")}: **${money(state.day_cost || 0)}**`);
  }],
  todo: ["the agent's checklist", "", () => { const it = state.session?.todo || []; it.length ? say(it.map(i => `- [${i.s === "done" ? "x" : " "}] ${i.t}`).join("\n")) : note("—"); }],
  theme: ["color theme", "[name]", a => { const x = themeList.find(th => th.name === a); x ? setTheme(x.name) : pickTheme(); }],
  sync: ["sync now", "", () => MODE === "cloud" ? api(base + "/sync", {}).then(() => note(t("saved"), "ok")) : note("local", "warn")],
  login: ["log in / register", "", () => MODE === "cloud" ? note(state.user || "") : note("/login — " + t("do this in the terminal"), "warn")],
  logout: ["log out", "", () => MODE === "cloud" ? (localStorage.removeItem("me_token"), location.href = "/account") : note("/logout — " + t("do this in the terminal"), "warn")],
  "2fa": ["authenticator (TOTP)", "", () => site("security", "2fa")],
  config: ["settings", "", () => site("overview", "config")],
  update: ["update me", "", () => MODE === "cloud" ? note("cloud: always current", "ok") : note("/update — " + t("do this in the terminal"), "warn")],
  exit: ["close", "", () => MODE === "cloud" ? (location.href = "/account") : note(t("close this tab"), "warn")],
};
CMDS.quit = CMDS.exit;
function matchCmd(name) {
  if (CMDS[name]) return name;
  const c = Object.keys(CMDS).filter(k => k.startsWith(name));
  return c.length === 1 ? c[0] : null;
}
async function runSlash(text) {
  const m = text.match(/^\/([a-z0-9]+)(?:\s+([\s\S]*))?$/i);
  if (!m) return false;                       // "/etc/hosts?" and the like are ordinary messages
  const name = matchCmd(m[1].toLowerCase());
  if (!name) { note(`${t("unknown command")}: /${m[1]} — /help`, "err"); return true; }
  try { await CMDS[name][2]((m[2] || "").trim()); } catch (e) { note(e.message, "err"); }
  return true;
}
// ─── "/" menu above the input ───
const slashEl = document.createElement("div"); slashEl.id = "slash"; slashEl.className = "slash hidden"; $("input").parentNode.insertBefore(slashEl, $("input"));
let slashSel = 0, slashRows = [];
function slashUpdate() {
  const v = inp.value, m = v.match(/^\/([a-z0-9]*)$/i);
  if (!m) { slashEl.classList.add("hidden"); slashRows = []; return; }
  slashRows = Object.keys(CMDS).filter(k => k !== "quit" && k !== "clear" && k.startsWith(m[1].toLowerCase()));
  slashSel = Math.min(slashSel, Math.max(0, slashRows.length - 1));
  slashEl.classList.toggle("hidden", !slashRows.length);
  slashEl.innerHTML = slashRows.map((k, i) => `<button type="button" class="${i === slashSel ? "on" : ""}" data-k="${k}"><b>/${k}</b><span class="a">${esc(CMDS[k][1])}</span><span class="d">${esc(t(CMDS[k][0]))}</span></button>`).join("");
  slashEl.querySelectorAll("button").forEach(b => b.onmousedown = e => { e.preventDefault(); slashPick(b.dataset.k); });
}
function slashPick(k) { inp.value = "/" + k + (CMDS[k][1] ? " " : ""); grow(); slashEl.classList.add("hidden"); inp.focus(); if (!CMDS[k][1]) send(); }
inp.addEventListener("input", () => { slashSel = 0; slashUpdate(); });
inp.addEventListener("keydown", e => {
  if (slashEl.classList.contains("hidden") || !slashRows.length) return;
  if (e.key === "ArrowDown" || e.key === "ArrowUp") { e.preventDefault(); slashSel = (slashSel + (e.key === "ArrowDown" ? 1 : -1) + slashRows.length) % slashRows.length; slashUpdate(); }
  else if (e.key === "Tab" || (e.key === "Enter" && !e.shiftKey && inp.value.trim() !== "/" + slashRows[slashSel] && !CMDS[inp.value.slice(1).toLowerCase()])) { e.preventDefault(); e.stopImmediatePropagation(); slashPick(slashRows[slashSel]); }
  else if (e.key === "Escape") { slashEl.classList.add("hidden"); slashRows = []; e.stopImmediatePropagation(); }
}, true);
