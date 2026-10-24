// me studio — UI. Talks to me_studio.py over /api/* with the per-run key; tasks stream events by long-poll.
const KEY = "__KEY__";
const $ = id => document.getElementById(id);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"})[c]);
const human = n => !n ? "0" : n < 1000 ? String(n) : n < 1e6 ? (n / 1000).toFixed(1).replace(".0", "") + "k" : (n / 1e6).toFixed(1).replace(".0", "") + "M";
const dur = s => s < 60 ? s.toFixed(1) + (L() ? " с" : "s") : Math.floor(s / 60) + (L() ? " мин " : "m ") + String(Math.floor(s % 60)).padStart(2, "0") + (L() ? " с" : "s");
const base = p => String(p || "").split(/[\\/]/).filter(Boolean).pop() || p;

// ─── i18n: English source, Russian table ───
let LANG = "en";
const L = () => LANG === "ru";
const RU = {
  "New task": "Новая задача", "Search tasks": "Поиск задач", "search…": "поиск…", "attach a file": "прикрепить файл", "Add project": "Добавить проект", "Settings": "Настройки", "Changes": "Изменения", "Terminal": "Терминал",
  "What should we work on?": "Над чем работаем?", "Describe a task — fix a bug, add a feature, explain the code…": "Опишите задачу — исправить баг, добавить функцию, объяснить код…",
  "Each task is its own agent. Start several — they run side by side.": "Каждая задача — отдельный агент. Запускайте несколько — они работают параллельно.",
  "Local": "Локально", "Worktree": "Worktree", "Start": "Начать", "Recent": "Недавние", "Choose a project": "Выберите проект", "no project": "нет проекта",
  "Works in the project folder itself": "Работает прямо в папке проекта", "Its own git worktree and branch: your checkout stays untouched until you apply": "Свой git worktree и ветка: ваша папка не меняется, пока вы не примените результат",
  "needs a git repository with a commit": "нужен git-репозиторий с коммитом", "default model": "модель по умолчанию", "effort": "effort",
  "Start by adding a project": "Начните с добавления проекта", "Pick a folder with your code. Git repositories can run tasks in isolated worktrees.": "Выберите папку с кодом. В git-репозиториях задачи могут работать в отдельных worktree.",
  "enter to start": "enter — начать", "shift+enter new line": "shift+enter — новая строка", "Ask for follow-up changes": "Попросите доработать или задайте вопрос",
  "Steer the running task — it reads this after the current step": "Подскажите задаче — она прочтёт это после текущего шага",
  "working": "работает", "waiting for you": "ждёт вас", "ready to review": "можно смотреть", "done": "готово", "error": "ошибка", "idle": "ожидание", "discarded": "отброшена",
  "Stop": "Стоп", "Review": "Ревью", "Open folder": "Открыть папку", "Archive": "В архив", "Unarchive": "Из архива", "Rename": "Переименовать", "Discard…": "Отбросить…",
  "thinking…": "думаю…", "thought for {}": "думал {}", "Allow": "Разрешить", "Always allow this command": "Всегда разрешать эту команду", "Allow everything (full access)": "Разрешать всё (полный доступ)",
  "Deny": "Запретить", "allowed": "разрешено", "always allowed": "разрешено всегда", "denied": "запрещено", "Send": "Отправить", "answer…": "ответ…",
  "needs your approval": "нужно ваше разрешение", "queued": "в очереди", "while working": "во время работы", "retrying": "повторяю", "compacting history…": "сжимаю историю…",
  "history compacted · ~{} tokens": "история сжата · ~{} токенов", "self-check: running checks before finishing": "самопроверка: запускаю проверки перед завершением",
  "{} files changed": "изменено файлов: {}", "1 file changed": "изменён 1 файл", "review": "смотреть", "Worked {}": "Работал {}", "{} steps": "шагов: {}", "1 step": "1 шаг",
  "Plan": "План", "1 file": "1 файл", "{} files": "файлов: {}", "Apply to project": "Применить к проекту", "Commit": "Коммит", "Push branch": "Запушить ветку", "Undo last turn": "Откатить последний ход",
  "Commit message": "Сообщение коммита", "No changes yet": "Пока без изменений", "The agent's edits show up here as it works.": "Правки агента появятся здесь по ходу работы.",
  "branch": "ветка", "commits": "коммитов", "new": "новый", "deleted": "удалён", "binary file": "бинарный файл", "Run a command in the task folder": "Команда в папке задачи",
  "Cancel": "Отмена", "Add": "Добавить", "Save": "Сохранить", "Close": "Закрыть", "Project folder": "Папка проекта", "Recent folders": "Недавние папки", "Browse…": "Обзор…",
  "git repository": "git-репозиторий", "Up": "Вверх", "Use this folder": "Выбрать эту папку", "Theme": "Тема", "Language": "Язык", "Permissions": "Разрешения", "Default model": "Модель по умолчанию",
  "Show archived tasks": "Показывать архивные задачи", "ask": "спрашивать", "full access": "полный доступ", "read-only": "только чтение",
  "edits in the project are free; commands ask": "правки в проекте без вопросов; команды спрашивают", "everything runs without asking": "всё без вопросов", "no file changes": "без изменения файлов",
  "Discard this task?": "Отбросить задачу?", "Its worktree and branch {} are deleted. This cannot be undone.": "Её worktree и ветка {} будут удалены. Это нельзя отменить.",
  "The task is removed from the list; files it changed stay as they are.": "Задача уйдёт из списка; изменённые файлы останутся как есть.", "Discard": "Отбросить",
  "Remove project": "Убрать проект", "New task here": "Новая задача здесь", "no tasks yet": "задач пока нет", "Task name": "Название задачи", "copy": "копировать", "copied": "скопировано",
  "now": "сейчас", "{}m": "{} мин", "{}h": "{} ч", "{}d": "{} дн", "no models — add one with `me` → /models": "нет моделей — добавьте в `me` → /models", "offline": "офлайн",
  "Fix the failing tests": "Почини падающие тесты", "Explain how this project is structured": "Объясни, как устроен проект", "Review my uncommitted changes": "Сделай ревью моих незакоммиченных изменений",
  "Add a README with setup steps": "Добавь README с инструкцией по запуску", "Shortcuts": "Горячие клавиши", "search": "поиск", "stop the task": "остановить задачу",
  "Pick a project folder": "Выберите папку проекта", "loading…": "загрузка…", "Native folder picker": "Системный выбор папки", "Model": "Модель", "default": "по умолчанию",
  "no such folder": "нет такой папки", "Nothing to show yet": "Пока пусто", "Ask in Russian or English — the agent answers in your language.": "Пишите на любом языке — агент ответит на нём же.",
};
const t = (s, ...v) => { let r = L() && RU[s] !== undefined ? RU[s] : s; for (const x of v) r = r.replace("{}", x); return r; };
function i18nApply(root = document) {
  root.querySelectorAll("[data-i]").forEach(n => {
    const spec = n.dataset.i;
    if (!n.dataset.src) n.dataset.src = spec ? "" : n.textContent;
    if (spec.startsWith("ph:")) n.placeholder = t(spec.slice(3)); else if (spec.startsWith("title:")) n.title = t(spec.slice(6)); else n.textContent = t(n.dataset.src);
  });
}
const ago = ts => { const s = Date.now() / 1000 - ts; return s < 60 ? t("now") : s < 3600 ? t("{}m", Math.floor(s / 60)) : s < 86400 ? t("{}h", Math.floor(s / 3600)) : t("{}d", Math.floor(s / 86400)); };

const I = {
  check: '<svg viewBox="0 0 24 24" style="color:var(--ok)"><path d="M5 12l5 5 9-11"/></svg>',
  x: '<svg viewBox="0 0 24 24" style="color:var(--err)"><path d="M6 6l12 12M18 6L6 18"/></svg>',
  stop: '<svg viewBox="0 0 24 24"><rect x="7" y="7" width="10" height="10" rx="2" fill="currentColor" stroke="none"/></svg>',
  up: '<svg viewBox="0 0 24 24"><path d="M12 19V5M5 12l7-7 7 7"/></svg>',
  diff: '<svg viewBox="0 0 24 24"><path d="M12 3v12M6 9h12M6 21h12"/></svg>',
  term: '<svg viewBox="0 0 24 24"><path d="M4 6l6 6-6 6M12 18h8"/></svg>',
  folder: '<svg viewBox="0 0 24 24"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg>',
  branch: '<svg viewBox="0 0 24 24"><circle cx="6" cy="6" r="2"/><circle cx="6" cy="18" r="2"/><circle cx="18" cy="8" r="2"/><path d="M6 8v8M18 10c0 4-6 3-12 6"/></svg>',
  chev: '<svg viewBox="0 0 24 24" class="cv"><path d="M6 9l6 6 6-6"/></svg>',
  right: '<svg viewBox="0 0 24 24" class="ch"><path d="M9 6l6 6-6 6"/></svg>',
  dots: '<svg viewBox="0 0 24 24"><circle cx="5" cy="12" r="1.3" fill="currentColor"/><circle cx="12" cy="12" r="1.3" fill="currentColor"/><circle cx="19" cy="12" r="1.3" fill="currentColor"/></svg>',
  plus: '<svg viewBox="0 0 24 24"><path d="M12 5v14M5 12h14"/></svg>',
  cpu: '<svg viewBox="0 0 24 24"><rect x="6" y="6" width="12" height="12" rx="2"/><path d="M9 2v4M15 2v4M9 18v4M15 18v4M2 9h4M2 15h4M18 9h4M18 15h4"/></svg>',
  warn: '<svg viewBox="0 0 24 24" style="color:var(--warn)"><path d="M12 3l9 16H3zM12 10v4M12 17v.5"/></svg>',
  q: '<svg viewBox="0 0 24 24" style="color:var(--warn)"><circle cx="12" cy="12" r="9"/><path d="M9.5 9.5a2.5 2.5 0 1 1 3.5 2.3c-.6.3-1 .9-1 1.6M12 17v.5"/></svg>',
  file: '<svg viewBox="0 0 24 24"><path d="M14 3H6a1 1 0 0 0-1 1v16a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1V8z"/><path d="M14 3v5h5"/></svg>',
};
const TOOL_VERB = {read: "Read", write: "Write", edit: "Edit", bash: "Run", ls: "List", grep: "Search", web_search: "Web search", web_fetch: "Fetch", skill: "Skill",
  todo: "Plan", task: "Subagent", memory: "Memory", mcp: "MCP", notify: "Notify"};
const TOOL_RU = {Read: "Чтение", Write: "Запись", Edit: "Правка", Run: "Команда", List: "Файлы", Search: "Поиск", "Web search": "Веб-поиск", Fetch: "Загрузка", Skill: "Навык",
  Plan: "План", Subagent: "Субагент", Memory: "Память", MCP: "MCP", Notify: "Уведомление"};
const verb = n => { const v = TOOL_VERB[n] || n; return L() ? (TOOL_RU[v] || v) : v; };

// ─── api ───
async function api(path, body) {
  const r = await fetch(path, {method: body === undefined ? "GET" : "POST", headers: {"Content-Type": "application/json", "X-Key": KEY},
                               body: body === undefined ? undefined : JSON.stringify(body)});
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.detail || r.statusText);
  return j;
}
function toast(text, cls = "") {
  const d = document.createElement("div"); d.className = cls; d.textContent = text; $("toast").appendChild(d);
  setTimeout(() => d.remove(), cls === "err" ? 8000 : 3500);
}
// messages from me_studio.py (English) → the UI language
const SRV_RU = [[/^applied (\d+) file\(s\) to (.+) \(staged\)$/, "применено файлов: $1 → $2 (в индексе git)"], [/^committed to (.+)$/, "закоммичено в $1"],
  [/^pushed (.+)$/, "ветка $1 запушена"], [/^discarded \(worktree and branch (.+) removed\)$/, "отброшено (worktree и ветка $1 удалены)"], [/^discarded$/, "отброшено"],
  [/^nothing to commit$/, "нечего коммитить"], [/^nothing to apply$/, "нечего применять"], [/^restored (\d+) files?: (.*)$/, "восстановлено файлов: $1: $2"],
  [/^stop the task first$/, "сначала остановите задачу"], [/^no such folder: (.+)$/, "нет такой папки: $1"], [/^describe the task first$/, "сначала опишите задачу"],
  [/^nothing to undo$/, "нечего откатывать"], [/^busy$/, "задача ещё работает"]];
const srv = m => { m = String(m || ""); if (!L()) return m; for (const [rx, r] of SRV_RU) if (rx.test(m)) return m.replace(rx, r); return m; };
const fail = e => toast(srv(e.message || String(e)), "err");
async function copyText(s) {
  try { if (navigator.clipboard && window.isSecureContext) { await navigator.clipboard.writeText(s); return true; } } catch (e) {}
  const ta = document.createElement("textarea"); ta.value = s; ta.style.cssText = "position:fixed;top:-1000px;opacity:0"; document.body.appendChild(ta);
  ta.select(); let ok = false; try { ok = document.execCommand("copy"); } catch (e) {} ta.remove(); return ok;
}
function copyCode(b) { copyText(b.closest("pre").querySelector("code").innerText).then(ok => { b.textContent = ok ? t("copied") : "✗"; setTimeout(() => b.textContent = t("copy"), 1200); }); }

// ─── markdown (small, safe: everything is escaped first) ───
function md(src) {
  const blocks = [];
  src = String(src || "").replace(/```(\w*)[^\n]*\n?([\s\S]*?)(```|$)/g, (_, lang, code) => { blocks.push([lang, code.replace(/\n$/, "")]); return `\u0000${blocks.length - 1}\u0000`; });
  let h = esc(src)
    .replace(/^### (.*)$/gm, "<h3>$1</h3>").replace(/^## (.*)$/gm, "<h2>$1</h2>").replace(/^# (.*)$/gm, "<h1>$1</h1>")
    .replace(/^&gt; (.*)$/gm, "<blockquote>$1</blockquote>")
    .replace(/`([^`\n]+)`/g, "<code>$1</code>").replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>").replace(/(^|[\s(])\*([^*\n]+)\*/g, "$1<i>$2</i>")
    .replace(/\[([^\]]+)\]\((https?:[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
  h = h.replace(/(?:^|\n)((?:\|.*\|[ \t]*(?:\n|$))+)/g, (_, tb) => {
    const rows = tb.trim().split("\n").filter(r => !/^\|[\s:|-]+\|$/.test(r.trim()));
    return "\n<table>" + rows.map((r, i) => "<tr>" + r.trim().replace(/^\||\|$/g, "").split("|").map(c => `<${i ? "td" : "th"}>${c.trim()}</${i ? "td" : "th"}>`).join("") + "</tr>").join("") + "</table>\n";
  });
  h = h.replace(/(?:^|\n)((?:[ \t]*[-*] .*(?:\n|$))+)/g, (_, l) => "\n<ul>" + l.trim().split("\n").map(x => "<li>" + x.trim().slice(2) + "</li>").join("") + "</ul>\n");
  h = h.replace(/(?:^|\n)((?:\d+[.)] .*(?:\n|$))+)/g, (_, l) => "\n<ol>" + l.trim().split("\n").map(x => "<li>" + x.replace(/^\d+[.)] /, "") + "</li>").join("") + "</ol>\n");
  h = h.split(/\n{2,}/).map(p => /^\s*<(h\d|ul|ol|blockquote|table)/.test(p) || /^\u0000\d+\u0000$/.test(p.trim()) ? p : "<p>" + p.trim().replace(/\n/g, "<br>") + "</p>").join("");
  return h.replace(/\u0000(\d+)\u0000/g, (_, i) => `<pre><div class="bar"><span>${esc(blocks[i][0] || "code")}</span><button onclick="copyCode(this)">${t("copy")}</button></div><code>${esc(blocks[i][1])}</code></pre>`);
}
const diffHtml = s => String(s).split("\n").map(l => `<span class="dl ${l.startsWith("+") && !l.startsWith("+++") ? "a" : l.startsWith("-") && !l.startsWith("---") ? "d" : l.startsWith("@@") ? "h" : ""}">${esc(l) || " "}</span>`).join("");

// ─── theme: me's own theme colors; soft variants computed here (no color-mix: older WebKitGTK lacks it) ───
const COLOR_VARS = {bg: "--bg", bg2: "--bg2", bg3: "--bg3", line: "--line", fg: "--fg", dim: "--dim", faint: "--faint", accent: "--a", accent2: "--a2",
  ok: "--ok", err: "--err", warn: "--warn", code_bg: "--code", sel: "--sel"};
const rgba = (hex, a) => { const m = /^#?([\da-f]{2})([\da-f]{2})([\da-f]{2})/i.exec(hex || ""); return m ? `rgba(${parseInt(m[1], 16)},${parseInt(m[2], 16)},${parseInt(m[3], 16)},${a})` : hex; };
function applyTheme() {
  const r = document.documentElement.style, c = S.colors || {};
  for (const [k, v] of Object.entries(c)) if (COLOR_VARS[k]) r.setProperty(COLOR_VARS[k], v);
  const a = c.accent || "#8b7cff", w = c.warn || "#ffb454", ok = c.ok || "#5ad18a", er = c.err || "#ff6b7a";
  const lum = (/^#?([\da-f]{6})/i.exec(a) || [0, "8b7cff"])[1].match(/../g).map(x => { x = parseInt(x, 16) / 255; return x <= .03928 ? x / 12.92 : ((x + .055) / 1.055) ** 2.4; });
  r.setProperty("--on-a", .2126 * lum[0] + .7152 * lum[1] + .0722 * lum[2] > .35 ? "#0a0a0a" : "#ffffff");  // text on accent buttons: whichever reads
  for (const [n, v] of Object.entries({"--a10": rgba(a, .1), "--a14": rgba(a, .14), "--a30": rgba(a, .3), "--a45": rgba(a, .45), "--w8": rgba(w, .08), "--w45": rgba(w, .45),
    "--w25": rgba(w, .25), "--ok10": rgba(ok, .1), "--err10": rgba(er, .1), "--ok35": rgba(ok, .35), "--err35": rgba(er, .35)})) r.setProperty(n, v);
  document.documentElement.style.colorScheme = S.dark ? "dark" : "light";
}

// ─── state ───
let S = {tasks: [], projects: [], models: []}, cur = null, after = 0, poll = 0, live = {}, view = {}, panel = null, draft = {};
const PREF = (() => { try { return JSON.parse(localStorage.getItem("studio") || "{}"); } catch (e) { return {}; } })();
const savePref = () => { try { localStorage.setItem("studio", JSON.stringify(PREF)); } catch (e) {} };
const task = id => S.tasks.find(x => x.id === id);
const proj = p => S.projects.find(x => x.path === p) || {path: p, name: base(p), git: false};
const statusOf = x => x.busy ? (x.status === "waiting" ? "waiting" : "running") : (x.status || "idle");
const STATUS = {running: "working", waiting: "waiting for you", review: "ready to review", done: "done", error: "error", idle: "idle", discarded: "discarded"};

async function refresh() {
  let s; try { s = await api("/api/state"); } catch (e) { return; }
  const langChanged = (s.lang || autoLang()) !== LANG;
  const before = Object.fromEntries((S.tasks || []).map(x => [x.id, statusOf(x)]));
  S = s; LANG = S.lang || autoLang(); document.documentElement.lang = LANG;
  for (const x of S.tasks) {  // other tasks finishing or asking for approval while you look at something else
    const was = before[x.id], now = statusOf(x);
    if (!was || was === now || x.id === cur) continue;
    if (now === "waiting") toast(`${x.title.slice(0, 60)} — ${t("needs your approval")}`);
    else if (was === "running" && now !== "running") toast(`${x.title.slice(0, 60)} — ${t(STATUS[now] || now)}`, now === "error" ? "err" : "ok");
  }
  applyTheme();
  if (langChanged) i18nApply();
  $("ver").textContent = "v" + S.version + " · me " + S.me;
  $("who").textContent = S.user ? "☁ " + S.user : t("offline");
  renderSide();
  if (!cur) { const sig = JSON.stringify([S.projects, S.models, LANG, S.tasks.filter(x => !x.archived).slice(0, 6).map(x => [x.id, statusOf(x), x.title])]); if (sig !== view.homeSig) { view.homeSig = sig; renderHome(); } }
  else renderTop();
}
const autoLang = () => (navigator.languages || [navigator.language || "en"]).some(l => /^(ru|be|uk|kk)\b/i.test(l)) ? "ru" : "en";

// ─── sidebar ───
function renderSide() {
  const q = ($("q").value || "").toLowerCase(), arch = !!PREF.archived, closed = PREF.closed || {};
  const by = {};
  for (const p of S.projects) by[p.path] = [];
  for (const x of S.tasks) {
    if ((!arch && x.archived) || (q && !x.title.toLowerCase().includes(q))) continue;
    (by[x.project] = by[x.project] || []).push(x);
  }
  const keys = Object.keys(by);
  $("projects").innerHTML = keys.length ? keys.map(p => { const ts = by[p], pj = proj(p), pe = esc(JSON.stringify(p));
    return `<div class="proj ${closed[p] && !q ? "closed" : ""}">
      <div class="ph" title="${esc(p)}" onclick='toggleProj(${pe})'><svg viewBox="0 0 24 24" class="chev"><path d="M6 9l6 6 6-6"/></svg><span class="nm">${esc(pj.name)}</span>
        <span class="acts"><button class="ib" title="${esc(t("New task here"))}" onclick='event.stopPropagation();go("", ${pe})'>${I.plus}</button>
        <button class="ib" title="${esc(t("Remove project"))}" onclick='event.stopPropagation();projMenu(this, ${pe})'>${I.dots}</button></span></div>
      <div class="tl">${ts.map(x => `<button class="titem ${x.id === cur ? "on" : ""}" onclick="go('${x.id}')" title="${esc(x.title)}">
          <span class="st ${statusOf(x)}"><i></i></span><span class="tt">${esc(x.title)}</span>${x.mode === "worktree" ? `<span class="wt" title="${esc(x.branch || "")}">${I.branch}</span>` : ""}<span class="ago">${ago(x.updated)}</span></button>`).join("")
        || `<div class="empty-side">${esc(t("no tasks yet"))}</div>`}</div></div>`; }).join("")
    : `<div class="empty-side">${esc(t("Pick a folder with your code. Git repositories can run tasks in isolated worktrees."))}</div>`;
}
function toggleProj(p) { PREF.closed = PREF.closed || {}; PREF.closed[p] = !PREF.closed[p]; savePref(); renderSide(); }
function projMenu(el, p) {
  menu(el, [[t("New task here"), () => go("", p)], [t("Open folder"), () => api("/api/open", {path: p}).catch(fail)], "-",
            [t("Remove project"), async () => { await api("/api/project/remove", {path: p}).catch(fail); refresh(); }, "danger"]]);
}
function openSide() { $("side").classList.add("open"); $("scrim").classList.add("on"); }
function closeSide() { $("side").classList.remove("open"); $("scrim").classList.remove("on"); }

// ─── menus & modals ───
function menu(anchor, items, opts = {}) {
  closeMenu();
  const m = document.createElement("div"); m.className = "menu"; m.id = "menu";
  m.innerHTML = (opts.search ? `<input class="msearch" placeholder="${esc(t("search…"))}" spellcheck="false">` : "") + items.map((it, i) => it === "-" ? "<hr>" : typeof it === "string" ? `<div class="lbl">${esc(it)}</div>` :
    `<button class="mi ${it[2] === "danger" ? "danger" : ""}" data-i="${i}"><span class="ck">${it[3] ? "✓" : ""}</span><span>${esc(it[0])}</span>${it[4] ? `<span class="d">${esc(it[4])}</span>` : ""}</button>`).join("");
  document.body.appendChild(m);
  const r = anchor.getBoundingClientRect(), mw = m.offsetWidth, up = r.bottom + 6 + m.offsetHeight > innerHeight - 8;
  const place = () => { const mh = m.offsetHeight; m.style.top = (up ? Math.max(8, r.top - mh - 6) : r.bottom + 6) + "px"; };  // an upward menu keeps its bottom edge by the anchor
  m.style.left = Math.max(8, Math.min(opts.right ? r.right - mw : r.left, innerWidth - mw - 8)) + "px"; m.style.minWidth = mw + "px"; place();
  m.querySelectorAll(".mi").forEach(b => b.onclick = () => { closeMenu(); items[+b.dataset.i][1](); });
  const q = m.querySelector(".msearch");
  if (q) {
    q.oninput = () => {  // filter by name or id; section labels and rules only make sense unfiltered
      const v = q.value.trim().toLowerCase();
      m.querySelectorAll(".mi").forEach(b => b.classList.toggle("hidden", !!v && !b.textContent.toLowerCase().includes(v)));
      m.querySelectorAll(".lbl, hr").forEach(b => b.classList.toggle("hidden", !!v));
      menuKey({key: "Home"}); place();
    };
    if (!matchMedia("(pointer: coarse)").matches) q.focus();
  }
  setTimeout(() => { document.addEventListener("mousedown", outside); document.addEventListener("keydown", menuKey, true); }, 0);
}
function menuKey(e) {  // arrows move, Enter picks — so a long model list works without the mouse
  const m = $("menu"); if (!m) return;
  const rows = [...m.querySelectorAll(".mi:not(.hidden)")], i = rows.findIndex(b => b.classList.contains("kb"));
  let j = null;
  if (e.key === "ArrowDown") j = i < 0 ? 0 : (i + 1) % rows.length;
  else if (e.key === "ArrowUp") j = i <= 0 ? rows.length - 1 : i - 1;
  else if (e.key === "Home") j = 0;
  else if (e.key === "Enter" && rows.length) { e.preventDefault(); e.stopPropagation(); (rows[i < 0 ? 0 : i]).click(); return; }
  if (j == null || !rows.length) return;
  if (e.preventDefault) e.preventDefault();
  rows.forEach(b => b.classList.remove("kb")); rows[j].classList.add("kb"); rows[j].scrollIntoView({block: "nearest"});
}
function outside(e) { const m = $("menu"); if (m && !m.contains(e.target)) closeMenu(); }
function closeMenu() { const m = $("menu"); if (m) m.remove(); document.removeEventListener("mousedown", outside); document.removeEventListener("keydown", menuKey, true); }
function modal(title, body, foot) {
  $("modal").innerHTML = `<div class="dlg" role="dialog" aria-label="${esc(title)}"><div class="dh"><b>${esc(title)}</b><button class="ib" onclick="closeModal()" aria-label="close">${I.x.replace('style="color:var(--err)"', "")}</button></div>
    <div class="db">${body}</div>${foot ? `<div class="df">${foot}</div>` : ""}</div>`;
  $("modal").classList.remove("hidden");
  const f = $("modal").querySelector("input[type=text], textarea"); if (f) setTimeout(() => f.focus(), 30);
}
function closeModal() { $("modal").classList.add("hidden"); $("modal").innerHTML = ""; }
function confirmBox(title, text, okLabel, danger) {
  return new Promise(res => {
    modal(title, `<div style="color:var(--dim)">${esc(text)}</div>`, `<button class="btn" id="cbNo">${esc(t("Cancel"))}</button><button class="btn ${danger ? "danger solid" : "pri"}" id="cbOk">${esc(okLabel)}</button>`);
    $("cbNo").onclick = () => { closeModal(); res(false); }; $("cbOk").onclick = () => { closeModal(); res(true); };
  });
}

// ─── add project: native picker in the app window, a folder browser everywhere ───
async function addProject(then) {
  if (window.pywebview && window.pywebview.api && window.pywebview.api.pick_folder) {
    const p = await window.pywebview.api.pick_folder("").catch(() => "");
    if (p) return addPath(p, then);
    return;
  }
  const recent = S.projects.map(p => p.path);
  modal(t("Pick a project folder"), `
    <label class="f">${esc(t("Project folder"))}<input type="text" id="fsPath" placeholder="${esc(S.home || "")}" spellcheck="false"></label>
    ${recent.length ? `<div class="chips2">${recent.slice(0, 8).map(p => `<button onclick='fsGo(${esc(JSON.stringify(p))})' title="${esc(p)}">${esc(base(p))}</button>`).join("")}</div>` : ""}
    <div class="fs"><div class="fsh"><button class="ib" title="${esc(t("Up"))}" onclick="fsUp()"><svg viewBox="0 0 24 24"><path d="M12 19V5M5 12l7-7 7 7"/></svg></button>
      <input id="fsCur" spellcheck="false" onkeydown="if(event.key==='Enter')fsGo(this.value)"><span id="fsRoots"></span></div><div class="list" id="fsList"></div></div>`,
    `<button class="btn" onclick="closeModal()">${esc(t("Cancel"))}</button><button class="btn pri" id="fsOk">${esc(t("Add"))}</button>`);
  view.fsThen = then;
  $("fsOk").onclick = () => addPath($("fsPath").value.trim() || view.fs?.path, view.fsThen);
  $("fsPath").onkeydown = e => { if (e.key === "Enter") $("fsOk").click(); };
  fsGo(recent[0] ? recent[0].replace(/[\\/][^\\/]+[\\/]?$/, "") || S.home : S.home);
}
async function fsGo(p) {
  $("fsList").innerHTML = `<div class="empty-side">${esc(t("loading…"))}</div>`;
  let d; try { d = await api("/api/fs?path=" + encodeURIComponent(p || "")); } catch (e) { $("fsList").innerHTML = `<div class="empty-side" style="color:var(--err)">${esc(e.message)}</div>`; return; }
  view.fs = d; $("fsCur").value = d.path; $("fsPath").value = d.path;
  $("fsRoots").innerHTML = (d.roots || []).map(r => `<button class="btn sm ghost" onclick='fsGo(${esc(JSON.stringify(r))})'>${esc(r)}</button>`).join("");
  const sep = d.path.includes("\\") ? "\\" : "/";
  $("fsList").innerHTML = (d.dirs.length ? d.dirs.map(x => { const full = d.path.replace(/[\\/]$/, "") + sep + x.name;
    return `<button class="row" ondblclick='fsGo(${esc(JSON.stringify(full))})' onclick='fsPick(this, ${esc(JSON.stringify(full))})'>${I.folder}<span>${esc(x.name)}</span>${x.git ? `<span class="g">git</span>` : ""}</button>`; }).join("")
    : `<div class="empty-side">—</div>`);
}
function fsPick(el, p) { document.querySelectorAll("#fsList .row").forEach(r => r.classList.remove("sel")); el.classList.add("sel"); $("fsPath").value = p; }
function fsUp() { if (view.fs && view.fs.parent) fsGo(view.fs.parent); }
async function addPath(p, then) {
  if (!p) return;
  try { const r = await api("/api/project/add", {path: p}); closeModal(); await refresh(); view.project = r.path; if (then) then(r.path); else go("", r.path); }
  catch (e) { fail(e); }
}

// ─── home: start a task ───
function renderHome() {
  const has = S.projects.length;
  if (!view.project || !S.projects.some(p => p.path === view.project)) view.project = PREF.project && S.projects.some(p => p.path === PREF.project) ? PREF.project : (S.projects[0] || {}).path;
  const p = view.project ? proj(view.project) : null;
  const mode = p && p.git ? (view.mode || PREF.mode || "local") : "local";
  const recent = S.tasks.filter(x => !x.archived).slice(0, 6);
  const m = S.models.find(x => x.name === (view.model || S.model));
  $("home").innerHTML = `<div class="hero">
    <h1>${esc(t("What should we work on?"))}</h1><p class="sub">${esc(t("Each task is its own agent. Start several — they run side by side."))}</p>
    ${has ? "" : `<div class="onboard">${I.folder}<div class="tx"><b>${esc(t("Start by adding a project"))}</b>${esc(t("Pick a folder with your code. Git repositories can run tasks in isolated worktrees."))}</div><button class="btn pri" onclick="addProject()">${esc(t("Add project"))}</button></div>`}
    <div class="composer"><textarea id="prompt" rows="3" placeholder="${esc(t("Describe a task — fix a bug, add a feature, explain the code…"))}">${esc(draft.home || "")}</textarea>
      <div class="crow">
        <button class="chip ${p ? "" : "warn"}" id="pChip" onclick="pickProject(this)" title="${esc(p ? p.path : "")}">${I.folder}<span>${esc(p ? p.name : t("Choose a project"))}</span>${I.chev}</button>
        <span class="seg"><button class="${mode === "local" ? "on" : ""}" onclick="setMode('local')" title="${esc(t("Works in the project folder itself"))}">${esc(t("Local"))}</button><button class="${mode === "worktree" ? "on" : ""}" ${p && p.git ? "" : "disabled"} onclick="setMode('worktree')" title="${esc(p && p.git ? t("Its own git worktree and branch: your checkout stays untouched until you apply") : t("needs a git repository with a commit"))}">${I.branch}${esc(t("Worktree"))}</button></span>
        <button class="chip" onclick="pickModel(this)" title="${esc(t("Model"))}">${I.cpu}<span>${esc(m ? m.name : t("no models — add one with `me` → /models"))}${view.effort ? " · " + esc(view.effort) : ""}</span>${I.chev}</button>
        <span class="sp"></span><button class="send" id="startBtn" onclick="startTask()" title="${esc(t("Start"))}" ${p ? "" : "disabled"}>${I.up}</button></div></div>
    <div class="hint"><span><kbd>Enter</kbd> ${esc(t("enter to start").replace(/^enter — |^enter to /, ""))}</span><span><kbd>Shift Enter</kbd> ${esc(t("shift+enter new line").replace(/^shift\+enter — |^shift\+enter /, ""))}</span><span><kbd>@</kbd> ${esc(t("attach a file"))}</span><span>${esc(t("Ask in Russian or English — the agent answers in your language."))}</span></div>
    ${p ? `<div class="starters">${["Fix the failing tests", "Explain how this project is structured", "Review my uncommitted changes", "Add a README with setup steps"].map(s => `<button onclick='useStarter(${esc(JSON.stringify(t(s)))})'>${esc(t(s))}</button>`).join("")}</div>` : ""}
    ${recent.length ? `<div class="recent"><h3>${esc(t("Recent"))}</h3>${recent.map(x => `<button class="rc" onclick="go('${x.id}')"><span class="st ${statusOf(x)}"><i></i></span><span class="tt">${esc(x.title)}</span><span class="m">${esc(base(x.project))} · ${ago(x.updated)}</span></button>`).join("")}</div>` : ""}
  </div>`;
  const ta = $("prompt"), src = () => view.project ? "path=" + encodeURIComponent(view.project) : "";
  ta.oninput = () => { draft.home = ta.value; grow(ta, 320); atUpdate(ta, src()); };
  ta.onkeydown = e => { if (atKey(e, ta)) return; if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); startTask(); } };
  ta.onblur = atClose;
  grow(ta, 320); if (has) setTimeout(() => ta.focus(), 0);
}
// ─── @file completion in the composers (the agent attaches @path mentions itself) ───
const atCache = {};
async function atFiles(src) {
  const c = atCache[src]; if (c && Date.now() - c.t < 15000) return c.paths;
  try { const d = await api("/api/files?" + src); atCache[src] = {t: Date.now(), paths: d.paths}; return d.paths; } catch (e) { return []; }
}
function atQuery(ta) { const m = /(?:^|\s)@([^\s@]*)$/.exec(ta.value.slice(0, ta.selectionStart)); return m ? m[1] : null; }
function atClose() { const m = $("atmenu"); if (m) m.remove(); }
async function atUpdate(ta, src) {
  const q = atQuery(ta); if (q == null || !src) return atClose();
  const paths = await atFiles(src), ql = q.toLowerCase(), base = p => p.slice(p.lastIndexOf("/") + 1).toLowerCase();
  if (atQuery(ta) !== q || document.activeElement !== ta) return;  // typed on (or left) while the list loaded
  const hits = paths.filter(p => p.toLowerCase().includes(ql)).sort((a, b) => (base(b).startsWith(ql) - base(a).startsWith(ql)) || a.length - b.length).slice(0, 30);
  if (!hits.length) return atClose();
  let m = $("atmenu"); if (!m) { m = document.createElement("div"); m.id = "atmenu"; m.className = "menu"; document.body.appendChild(m); }
  m.innerHTML = hits.map((p, i) => `<button class="mi${i ? "" : " kb"}" data-p="${esc(p)}"><span>${esc(p)}</span></button>`).join("");
  m.querySelectorAll(".mi").forEach(b => b.onmousedown = e => { e.preventDefault(); atPick(ta, b.dataset.p); });
  const r = ta.closest(".composer").getBoundingClientRect(), h = m.offsetHeight;
  m.style.left = r.left + "px"; m.style.width = Math.min(r.width, 520) + "px"; m.style.maxWidth = "none";
  m.style.top = (r.top - h - 6 >= 8 ? r.top - h - 6 : r.bottom + 6) + "px";
}
function atPick(ta, p) {
  const pos = ta.selectionStart, before = ta.value.slice(0, pos).replace(/@[^\s@]*$/, "@" + p + " ");
  ta.value = before + ta.value.slice(pos); ta.selectionStart = ta.selectionEnd = before.length;
  atClose(); ta.focus(); ta.dispatchEvent(new Event("input"));
}
function atKey(e, ta) {  // true when the open list took the key
  const m = $("atmenu"); if (!m || e.isComposing) return false;
  const rows = [...m.querySelectorAll(".mi")], i = Math.max(0, rows.findIndex(b => b.classList.contains("kb")));
  if (e.key === "ArrowDown" || e.key === "ArrowUp") {
    const j = (i + (e.key === "ArrowDown" ? 1 : rows.length - 1)) % rows.length;
    rows.forEach(b => b.classList.remove("kb")); rows[j].classList.add("kb"); rows[j].scrollIntoView({block: "nearest"});
  } else if ((e.key === "Enter" && !e.shiftKey) || e.key === "Tab") atPick(ta, rows[i].dataset.p);
  else if (e.key === "Escape") atClose();
  else return false;
  e.preventDefault(); e.stopPropagation(); return true;
}
function useStarter(s) { draft.home = s; renderHome(); }
function setMode(m) { view.mode = m; PREF.mode = m; savePref(); renderHome(); }
function pickProject(el) {
  menu(el, [t("Project folder"), ...S.projects.map(p => [p.name, () => { view.project = p.path; PREF.project = p.path; savePref(); renderHome(); }, "", p.path === view.project, p.git ? "git" : ""]),
            "-", ["+ " + t("Add project"), () => addProject(path => { view.project = path; PREF.project = path; savePref(); go("", path); })]]);
}
const EFFORTS = ["", "low", "medium", "high", "xhigh"];
function pickModel(el, onPick) {
  const curM = onPick ? (task(cur) || {}).model || S.model : view.model || S.model, curE = onPick ? (task(cur) || {}).effort || "" : view.effort || "";
  const set = onPick || ((k, v) => { view[k] = v; renderHome(); });
  menu(el, [t("Model"), ...S.models.map(x => [x.name, () => set("model", x.name), "", x.name === curM, x.id]),
            "-", t("effort"), ...EFFORTS.map(e => [e || t("default"), () => set("effort", e), "", e === curE])], {search: S.models.length > 6});
}
async function startTask() {
  const ta = $("prompt"), prompt = ta.value.trim(), p = view.project;
  if (!p) return addProject();
  if (!prompt) return ta.focus();
  const pj = proj(p), mode = pj.git ? (view.mode || PREF.mode || "local") : "local";
  $("startBtn").disabled = true;
  try {
    const r = await api("/api/task/new", {project: p, prompt, mode, model: view.model || "", effort: view.effort || ""});
    draft.home = ""; PREF.project = p; savePref();
    await refresh(); go(r.id);
  } catch (e) { fail(e); $("startBtn").disabled = false; }
}
function grow(ta, max = 240) { ta.style.height = "auto"; ta.style.height = Math.min(max, ta.scrollHeight) + "px"; }

// ─── navigation ───
function go(id, project) {
  closeSide(); closeMenu();
  if (cur && $("msg")) draft[cur] = $("msg").value;
  cur = id || null; poll++; live = {}; view.files = null;
  if (project) view.project = project;
  $("home").classList.toggle("hidden", !!cur); $("task").classList.toggle("hidden", !cur);
  if (!cur) { closePanel(); $("ttl").innerHTML = `<b>${esc(t("New task"))}</b>`; $("acts").innerHTML = ""; view.homeSig = ""; renderHome(); }
  else openTask(cur);
  renderSide();
  try { history.replaceState(null, "", location.pathname + location.search.replace(/[&?]t=[^&]*/, "") + (cur ? (location.search ? "&" : "?") + "t=" + cur : "")); } catch (e) {}
}

// ─── task view ───
function renderTop() {
  const x = task(cur); if (!x) return;
  const st = statusOf(x), editing = $("ttlEdit");
  if (!editing) $("ttl").innerHTML = `<b onclick="renameTask()" title="${esc(t("Rename"))}">${esc(x.title)}</b>
    <span class="meta wide">${esc(base(x.project))}<span class="sep">·</span>${x.mode === "worktree" ? `${I.branch}${esc(x.branch || "")}` : esc(t("Local"))}</span>`;
  const files = view.files, adds = view.adds || 0, dels = view.dels || 0;
  $("acts").innerHTML = `<span class="pill ${st}">${st === "running" ? '<span class="spinner"></span>' : ""}${esc(t(STATUS[st] || st))}</span>
    ${x.busy ? `<button class="btn sm" onclick="stopTask()" title="Esc">${I.stop}${esc(t("Stop"))}</button>` : ""}
    <button class="btn sm ${panel === "changes" ? "" : "ghost"}" onclick="togglePanel('changes')" title="${esc(t("Changes"))}">${I.diff}<span class="wide">${esc(t("Review"))}</span>${files ? ` <span class="chg"><span class="add">+${adds}</span> <span class="del">−${dels}</span></span>` : ""}</button>
    <button class="ib ${panel === "term" ? "on" : ""}" onclick="togglePanel('term')" title="${esc(t("Terminal"))}">${I.term}</button>
    <button class="ib" onclick="taskMenu(this)" aria-label="more">${I.dots}</button>`;
}
function taskMenu(el) {
  const x = task(cur); if (!x) return;
  menu(el, [[t("Rename"), renameTask], [t("Open folder"), () => api("/api/open", {id: cur}).catch(fail)],
            [x.archived ? t("Unarchive") : t("Archive"), archiveTask], "-", [t("Discard…"), discardTask, "danger"]], {right: true});
}
async function openTask(id) {
  const x = task(id) || {};
  $("task").innerHTML = `<div id="plan" class="plan hidden"></div><div id="log"><div class="lw" id="lw"></div></div>
    <div class="foot"><div class="composer"><textarea id="msg" rows="1"></textarea>
      <div class="crow"><span class="mini"><button class="chip" style="height:26px" onclick="pickModel(this, setTaskModel)">${I.cpu}<span id="tModel"></span>${I.chev}</button></span>
        <span class="sp"></span><button class="send" id="sendBtn" onclick="sendOrStop()">${I.up}</button></div></div></div>`;
  const ta = $("msg"); ta.value = draft[id] || "";
  ta.oninput = () => { grow(ta); setSendBtn(); atUpdate(ta, "id=" + encodeURIComponent(id)); };
  ta.onkeydown = e => { if (atKey(e, ta)) return; if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); sendMsg(); } };
  ta.onblur = atClose;
  renderTop(); setSendBtn(); renderTaskModel();
  let d;
  try { d = await api("/api/task?id=" + encodeURIComponent(id)); } catch (e) { return fail(e); }
  if (cur !== id) return;
  renderHistory(d.messages); renderPlan(d.todo);
  (d.pending || []).forEach(onEvent);
  after = d.seq; refreshDiff();
  setTimeout(() => ta.focus(), 0);
  const my = ++poll;
  while (my === poll && cur === id) {
    try {
      const r = await api(`/api/events?id=${encodeURIComponent(id)}&after=${after}`);
      if (my !== poll) return;
      for (const e of r.events) { after = Math.max(after, e.seq); onEvent(e); }
      const xx = task(id); if (xx && xx.busy !== r.busy) refresh();
    } catch (e) { await new Promise(r => setTimeout(r, 1500)); }
  }
}
function renderTaskModel() { const x = task(cur) || {}; const el = $("tModel"); if (el) el.textContent = (x.model || S.model || "—") + (x.effort ? " · " + x.effort : ""); }
async function setTaskModel(k, v) {
  try { await api("/api/task/model", {id: cur, [k]: v}); const x = task(cur); if (x) x[k] = v; renderTaskModel(); } catch (e) { fail(e); }
}
function setSendBtn() {
  const x = task(cur) || {}, b = $("sendBtn"), ta = $("msg"); if (!b || !ta) return;
  const stop = x.busy && !ta.value.trim();
  b.className = "send" + (stop ? " stop" : ""); b.innerHTML = stop ? I.stop : I.up; b.title = stop ? t("Stop") : t("Send");
  ta.placeholder = x.busy ? t("Steer the running task — it reads this after the current step") : t("Ask for follow-up changes");
}
const lw = () => $("lw");
function atBottom() { const l = $("log"); return !l || l.scrollHeight - l.scrollTop - l.clientHeight < 140; }
function add(html) {
  const stick = atBottom(), d = document.createElement("div"); d.innerHTML = html.trim(); const n = d.firstChild;
  if (lw()) lw().appendChild(n); if (stick) scrollDown(true); return n;
}
function scrollDown(force) { const l = $("log"); if (l && (force || atBottom())) l.scrollTop = l.scrollHeight; }
const txt = c => Array.isArray(c) ? c.filter(x => x.type === "text").map(x => x.text).join(" ") : (c || "");
function userMsg(text, tag) { add(`<div class="msg user">${tag ? `<span class="tag">${esc(t(tag))}</span>` : ""}<div class="bub">${esc(text)}</div></div>`); }
function toolLabel(name, a) {
  a = a || {};
  if (name === "bash") return a.cmd || "";
  if (name === "todo") return "";
  return a.path || a.pattern || a.query || a.url || a.name || a.prompt || a.text || "";
}
function toolCard(name, a, label) {
  return add(`<div class="tool closed"><div class="h" onclick="this.parentNode.classList.toggle('closed')"><span class="ic"><span class="spinner"></span></span>
    <span class="nm">${esc(verb(name))}</span><span class="lab">${esc(label != null ? label : toolLabel(name, a))}</span><span class="t"></span>${I.right}</div><div class="stl hidden"></div><pre class="body"></pre></div>`);
}
function finishCard(c, ok, out, time) {
  c.querySelector(".ic").innerHTML = ok ? I.check : I.x; if (time != null) c.querySelector(".t").textContent = dur(time);
  c.querySelector(".stl").classList.add("hidden");
  const b = c.querySelector(".body"); if (!b.dataset.diff) b.textContent = String(out || "").slice(0, 30000) || "—";
  if (!ok) c.classList.add("err");
}
const atNames = s => "attached " + [...s.matchAll(/^\[([^\]\n]+)\]$/gm)].slice(1).map(x => x[1]).join(", ");  // headers of the @ attachment message
function renderHistory(msgs) {
  lw().innerHTML = "";
  const outs = {};
  msgs.forEach(m => { if (m.role === "tool") outs[m.tool_call_id] = m.content; });
  msgs.forEach(m => {
    if (m.role === "user") {
      const s = txt(m.content);
      if (m._aux) {
        if (/^\[message from the user/.test(s)) userMsg(s.split("\n").slice(1).join("\n"), "while working");
        else if (/^\[files the user attached with @\]/.test(s)) add(`<div class="note">@ ${esc(atNames(s))}</div>`);
        return;
      }
      if (s && s !== "(images from read)") userMsg(s);
    } else if (m.role === "assistant") {
      if (m.content) add(`<div class="md">${md(m.content)}</div>`);
      (m.tool_calls || []).forEach(tc => {
        if (tc.function.name === "todo") return;
        let a = {}; try { a = JSON.parse(tc.function.arguments); } catch (e) {}
        const c = toolCard(tc.function.name, a); const o = outs[tc.id]; finishCard(c, !/^error/.test(o || ""), o);
      });
    }
  });
  if (!msgs.length) add(`<div class="note">${esc(t("loading…"))}</div>`);
  scrollDown(true);
}
function renderPlan(items) {
  const el = $("plan"); if (!el) return;
  items = items || [];
  if (!items.length) { el.classList.add("hidden"); return; }
  const done = items.filter(i => i.s === "done").length;
  el.classList.remove("hidden"); el.classList.toggle("closed", !!PREF.planClosed);
  el.innerHTML = `<div class="in"><div class="ph2" onclick="PREF.planClosed=!PREF.planClosed;savePref();this.closest('.plan').classList.toggle('closed')"><b>${esc(t("Plan"))}</b><span>${done}/${items.length}</span>
    <span class="bar2"><i style="width:${Math.round(done / items.length * 100)}%"></i></span>${I.chev}</div>
    <div class="items">${items.map(i => `<div class="it ${i.s === "done" ? "d" : i.s === "doing" ? "g" : ""}"><span>${i.s === "done" ? "✓" : i.s === "doing" ? "▸" : "○"}</span><span>${esc(i.t)}</span></div>`).join("")}</div></div>`;
}
let raf = 0;
function flushText() { if (live.text) live.text.innerHTML = md(live.raw); }  // the last streamed piece must not wait for a frame that may never come
function breakText() { endThink(); flushText(); live.text = null; }  // a note between two replies must not glue them together
function endThink() { if (live.think && !live.thinkDone) { live.think.querySelector("summary").innerHTML = esc(t("thought for {}", dur((Date.now() - live.thinkT) / 1000))); live.thinkDone = true; } }
function onEvent(e) {
  const k = e.type;
  if (k === "user") { const last = lw() && lw().lastElementChild; if (!(last && last.classList.contains("msg") && last.querySelector(".bub").textContent === e.text && view.echo)) userMsg(e.text); view.echo = false; return; }
  if (k === "queued") { userMsg(e.text, "queued"); return; }
  if (k === "queued_sent") return;
  if (k === "turn_start") { refresh(); return; }
  if (k === "reasoning") {
    if (!live.think) { live.think = add(`<details class="think"><summary><span class="shimmer">${esc(t("thinking…"))}</span></summary><pre></pre></details>`); live.thinkT = Date.now(); live.thinkDone = false; }
    live.think.querySelector("pre").textContent += e.text; scrollDown(); return;
  }
  if (k === "text") {
    endThink();
    if (!live.text) { live.text = add(`<div class="md"></div>`); live.raw = ""; }
    live.raw += e.text;
    if (!raf) raf = requestAnimationFrame(() => { raf = 0; if (live.text) { live.text.innerHTML = md(live.raw); scrollDown(); } });
    return;
  }
  if (k === "tool_start") {
    endThink(); flushText(); live.text = null; live.think = null;
    if (e.name === "todo") return;
    live[e.id] = toolCard(e.cli ? e.cli.label : e.name, e.args, e.cli ? null : (e.name === "bash" ? (e.args || {}).cmd : toolLabel(e.name, e.args) || e.label)); return;
  }
  if (k === "tool_event") {
    const c = live[e.id]; if (!c) return;
    const s = c.querySelector(".stl");
    if (e.t === "status" || e.t === "log") { s.classList.remove("hidden"); s.textContent = (e.t === "log" ? "› " : "") + e.text; }
    else if (e.t === "diff" && (e.text || "").trim()) { const b = c.querySelector(".body"); b.dataset.diff = 1; b.innerHTML = diffHtml(e.text); c.classList.remove("closed"); }
    scrollDown(); return;
  }
  if (k === "tool_end") { const c = live[e.id]; if (c) { delete live[e.id]; finishCard(c, e.ok, e.output, e.time); } return; }
  if (k === "confirm") {
    endThink();
    const always = e.name === "bash" ? t("Always allow this command") : t("Allow everything (full access)");
    const a = e.args || {}, what = e.name === "bash" ? a.cmd : e.name === "edit" || e.name === "write" ? (a.path || "") + "\n\n" + (a.new || a.content || "").slice(0, 1500) : JSON.stringify(a, null, 1);
    const c = add(`<div class="card"><div class="q">${I.warn}<span>${esc(verb(e.name))} — ${esc(t("needs your approval"))}</span></div><pre>${esc(String(what || "").slice(0, 3000))}</pre>
      <div class="btns"><button class="btn sm pri">${esc(t("Allow"))}</button><button class="btn sm">${esc(always)}</button><button class="btn sm danger">${esc(t("Deny"))}</button></div></div>`);
    const [y, al, n] = c.querySelectorAll("button");
    const ans = v => { api("/api/task/confirm", {id: cur, rid: e.id, ok: v}).catch(fail); c.className = "card done"; c.innerHTML = `${esc(verb(e.name))} · ${esc(t(v ? (v === "always" ? "always allowed" : "allowed") : "denied"))}`; refresh(); };
    y.onclick = () => ans(true); al.onclick = () => ans("always"); n.onclick = () => ans(false);
    y.focus(); scrollDown(true); return;
  }
  if (k === "ask") {
    const opts = e.options || [];
    const c = add(`<div class="card"><div class="q">${I.q}${esc(e.text)}</div><div class="btns">${opts.length ? opts.map(o => `<button class="btn sm" data-v="${esc(o)}">${esc(o)}</button>`).join("") : `<input placeholder="${esc(t("answer…"))}"><button class="btn sm pri" data-in="1">${esc(t("Send"))}</button>`}</div></div>`);
    const send = v => { api("/api/task/answer", {id: cur, rid: e.id, text: v}).catch(fail); c.className = "card done"; c.innerHTML = `${esc(e.text)} → <b>${esc(v || "—")}</b>`; };
    c.querySelectorAll("button").forEach(b => b.onclick = () => send(b.dataset.in ? c.querySelector("input").value : b.dataset.v));
    const inp = c.querySelector("input"); if (inp) { inp.onkeydown = ev => ev.key === "Enter" && send(inp.value); inp.focus(); }
    scrollDown(true); return;
  }
  if (k === "retry") {
    if (e.discard) { live.text && live.text.remove(); live.think && live.think.remove(); live.text = live.think = null; }
    breakText(); add(`<div class="note warn">↻ ${esc(String(e.error || "").split("\n")[0].slice(0, 160))} — ${esc(t("retrying"))}</div>`); return;
  }
  if (k === "compacting") { breakText(); add(`<div class="note">${esc(t("compacting history…"))}</div>`); return; }
  if (k === "compacted") { add(`<div class="note">${esc(t("history compacted · ~{} tokens", human(e.ctx)))}</div>`); return; }
  if (k === "verify") { breakText(); add(`<div class="note">${esc(t("self-check: running checks before finishing"))}</div>`); return; }
  if (k === "notice") { breakText(); add(`<div class="note${e.level === "info" ? "" : " warn"}">${e.level === "info" ? "@ " : ""}${esc(e.text)}</div>`); return; }
  if (k === "error") { flushText(); add(`<div class="note ${e.text === "interrupted" ? "warn" : "err"}">${I.x}${esc(e.text)}</div>`); return; }
  if (k === "todo") { renderPlan(e.items); return; }
  if (k === "turn_diff") {
    const fs = e.files || []; if (!fs.length) return;
    breakText();
    const n = add(`<div class="note">${I.diff}<span>${esc(fs.length === 1 ? t("1 file changed") : t("{} files changed", fs.length))}</span> · <a>${esc(t("review"))}</a></div>`);
    n.querySelector("a").onclick = () => openPanel("changes");
    refreshDiff(); return;
  }
  if (k === "turn_end") {
    endThink(); flushText(); for (const id of Object.keys(live)) if (live[id] && live[id].classList && live[id].classList.contains("tool")) finishCard(live[id], false, "interrupted");
    live = {};
    const steps = e.steps || 1;
    add(`<div class="turnend">${esc(t("Worked {}", dur(e.time || 0)))} · ${esc(steps === 1 ? t("1 step") : t("{} steps", steps))} · ↑${human(e.in)} ↓${human(e.out)}${e.cost ? " · $" + e.cost.toFixed(4) : ""}</div>`);
    refreshDiff(); refresh().then(setSendBtn); return;
  }
  if (k === "status") { refresh().then(setSendBtn); if (panel === "changes") renderChanges(); return; }
}
function sendOrStop() { const x = task(cur) || {}; if (x.busy && !$("msg").value.trim()) stopTask(); else sendMsg(); }
async function sendMsg() {
  const ta = $("msg"), text = ta.value.trim(); if (!text) return;
  ta.value = ""; draft[cur] = ""; grow(ta);
  const x = task(cur) || {};
  if (!x.busy) { userMsg(text); view.echo = true; }
  try { await api("/api/task/send", {id: cur, text}); await refresh(); setSendBtn(); } catch (e) { fail(e); }
}
async function stopTask() { try { await api("/api/task/stop", {id: cur}); } catch (e) { fail(e); } }
async function archiveTask() { try { await api("/api/task/archive", {id: cur}); await refresh(); } catch (e) { fail(e); } }
function renameTask() {
  const x = task(cur); if (!x) return;
  $("ttl").innerHTML = `<input id="ttlEdit" value="${esc(x.title)}">`;
  const i = $("ttlEdit"); i.focus(); i.select();
  const done = async save => { if (!$("ttlEdit")) return; const v = i.value.trim(); i.remove(); if (save && v && v !== x.title) { await api("/api/task/rename", {id: cur, title: v}).catch(fail); x.title = v; } renderTop(); renderSide(); };
  i.onkeydown = e => { if (e.key === "Enter") done(true); if (e.key === "Escape") done(false); };
  i.onblur = () => done(true);
}
async function discardTask() {
  const x = task(cur); if (!x) return;
  const ok = await confirmBox(t("Discard this task?"), x.mode === "worktree" ? t("Its worktree and branch {} are deleted. This cannot be undone.", x.branch || "") : t("The task is removed from the list; files it changed stay as they are."), t("Discard"), true);
  if (!ok) return;
  try { const r = await api("/api/task/discard", {id: cur}); toast(srv(r.text), "ok"); await refresh(); go(""); } catch (e) { fail(e); }
}
async function refreshDiff() {
  if (!cur) return;
  try { const d = await api("/api/diff?id=" + encodeURIComponent(cur)); view.diff = d; view.files = d.files.length; view.adds = d.files.reduce((a, f) => a + f.add, 0); view.dels = d.files.reduce((a, f) => a + f.del, 0); renderTop(); if (panel === "changes") renderChanges(true); } catch (e) {}
}

// ─── review / terminal panel ───
function closePanel() { panel = null; $("panel").classList.add("hidden"); if (cur) renderTop(); }
function togglePanel(tab) { panel === tab ? closePanel() : openPanel(tab); }
function openPanel(tab) { if (!cur) return; panel = tab; $("panel").classList.remove("hidden"); panelTab(tab); renderTop(); }
function panelTab(tab) {
  panel = tab; document.querySelectorAll(".ptab").forEach(b => b.classList.toggle("on", b.dataset.tab === tab));
  if (tab === "term") renderTerm(); else { $("pbody").innerHTML = `<div class="empty">${esc(t("loading…"))}</div>`; refreshDiff().then(() => renderChanges()); }
  renderTop();
}
function renderChanges(keep) {
  const x = task(cur) || {}, d = view.diff; if (!d || panel !== "changes") return;
  const open = new Set(keep ? [...document.querySelectorAll(".fitem.open")].map(f => f.dataset.p) : []);
  const wt = x.mode === "worktree", n = d.files.length;
  const sum = n ? `<b>${esc(n === 1 ? t("1 file") : t("{} files", n))}</b> · <span class="add">+${view.adds}</span> <span class="del">−${view.dels}</span>` : esc(t("No changes yet"));
  $("pbody").innerHTML = `<div class="rv-head"><div class="rv-sum">${sum}${wt ? ` · ${I.branch}<span>${esc(d.branch || x.branch || "")}</span>${d.commits ? ` · ${d.commits} ${esc(t("commits"))}` : ""}` : ""}</div>
      <div class="rv-acts">${wt ? `<button class="btn sm pri" onclick="act('apply')" ${n ? "" : "disabled"}>${esc(t("Apply to project"))}</button><button class="btn sm" onclick="act('push')">${esc(t("Push branch"))}</button>`
        : `<button class="btn sm" onclick="act('undo')" ${x.busy ? "disabled" : ""}>${esc(t("Undo last turn"))}</button>`}</div>
      <div class="commit"><input id="cmsg" placeholder="${esc(t("Commit message"))}" value="${esc(view.cmsg || x.title || "")}" oninput="view.cmsg=this.value" onkeydown="if(event.key==='Enter')act('commit')"><button class="btn sm" onclick="act('commit')" ${n || !wt ? "" : "disabled"}>${esc(t("Commit"))}</button></div>
      ${d.note ? `<div class="note">${esc(d.note)}</div>` : ""}</div>
    ${n ? d.files.map((f, i) => `<div class="fitem ${open.has(f.path) || (!keep && i < 4 && f.add + f.del < 300) ? "open" : ""}" data-p="${esc(f.path)}"><div class="fh" onclick="this.parentNode.classList.toggle('open')">${I.right}<span class="p" title="${esc(f.path)}">${esc(f.path)}</span>${f.new ? `<span class="tag new">${esc(t("new"))}</span>` : ""}${f.deleted ? `<span class="tag gone">${esc(t("deleted"))}</span>` : ""}<span class="chg"><span class="add">+${f.add}</span> <span class="del">−${f.del}</span></span></div>
      <pre>${f.binary ? `<span class="dl">${esc(t("binary file"))}</span>` : diffHtml(f.diff.split("\n").filter(l => !/^(diff --git|index |--- |\+\+\+ |new file mode|deleted file mode)/.test(l)).join("\n"))}</pre></div>`).join("")
      : `<div class="empty">${I.file}<b>${esc(t("No changes yet"))}</b><span>${esc(t("The agent's edits show up here as it works."))}</span></div>`}`;
}
async function act(kind) {
  try {
    let r;
    if (kind === "commit") r = await api("/api/task/commit", {id: cur, message: ($("cmsg") || {}).value || ""});
    else if (kind === "undo") { r = await api("/api/task/undo", {id: cur}); openTask(cur); }
    else r = await api("/api/task/" + kind, {id: cur});
    toast(srv(r.text || "ok"), "ok"); refreshDiff(); refresh();
  } catch (e) { fail(e); }
}
function renderTerm() {
  if (!view.term) view.term = {};
  const out = view.term[cur] || "";
  $("pbody").innerHTML = `<div class="term"><pre id="termOut">${out}</pre><div class="termin"><span>$</span><input id="termIn" placeholder="${esc(t("Run a command in the task folder"))}" spellcheck="false" autocomplete="off"></div></div>`;
  const i = $("termIn"), o = $("termOut"); o.scrollTop = o.scrollHeight; i.focus();
  const hist = view.thist = view.thist || []; let hp = hist.length;
  i.onkeydown = async e => {
    if (e.key === "ArrowUp" && hp > 0) { hp--; i.value = hist[hp]; e.preventDefault(); return; }
    if (e.key === "ArrowDown") { hp = Math.min(hist.length, hp + 1); i.value = hist[hp] || ""; e.preventDefault(); return; }
    if (e.key !== "Enter" || !i.value.trim()) return;
    const cmd = i.value; i.value = ""; hist.push(cmd); hp = hist.length; i.disabled = true;
    const id = cur;
    view.term[id] = (view.term[id] || "") + `<span class="cmd">$ ${esc(cmd)}</span>\n`; if (panel === "term") o.innerHTML = view.term[id];
    try { const r = await api("/api/term", {id, cmd}); view.term[id] += (r.ok ? esc(r.output) : `<span class="bad">${esc(r.output)}</span>`) + "\n"; }
    catch (er) { view.term[id] += `<span class="bad">${esc(er.message)}</span>\n`; }
    if (panel === "term" && cur === id) { o.innerHTML = view.term[id]; o.scrollTop = o.scrollHeight; i.disabled = false; i.focus(); }
  };
}

// ─── settings ───
function openSettings() {
  const perms = [["ask", "ask", "edits in the project are free; commands ask"], ["full", "full access", "everything runs without asking"], ["read", "read-only", "no file changes"]];
  modal(t("Settings"), `
    <label class="f">${esc(t("Language"))}<div class="opt">${[["", "Auto"], ["en", "English"], ["ru", "Русский"]].map(([v, l]) => `<button class="${(S.lang || "") === v ? "on" : ""}" onclick="setSetting({lang: '${v}'})">${l}</button>`).join("")}</div></label>
    <label class="f">${esc(t("Permissions"))}<div class="opt">${perms.map(([v, l, d]) => `<button class="${S.perm === v ? "on" : ""}" onclick="setSetting({perm: '${v}'})">${esc(t(l))}<small>${esc(t(d))}</small></button>`).join("")}</div></label>
    <label class="f">${esc(t("Theme"))}<select onchange="setSetting({theme: this.value})">${(S.themes || []).map(n => `<option ${n === S.theme ? "selected" : ""}>${esc(n)}</option>`).join("")}</select></label>
    <label class="f">${esc(t("Default model"))}<select onchange="setSetting({model: this.value})">${S.models.map(m => `<option ${m.name === S.model ? "selected" : ""}>${esc(m.name)}</option>`).join("")}</select></label>
    <label style="display:flex;gap:8px;align-items:center"><input type="checkbox" ${PREF.archived ? "checked" : ""} onchange="PREF.archived=this.checked;savePref();renderSide()">${esc(t("Show archived tasks"))}</label>
    <div class="note">${esc(t("Shortcuts"))}: <kbd>Ctrl N</kbd> ${esc(t("New task").toLowerCase())} · <kbd>Ctrl K</kbd> ${esc(t("search"))} · <kbd>Esc</kbd> ${esc(t("stop the task"))}</div>`,
    `<button class="btn" onclick="closeModal()">${esc(t("Close"))}</button>`);
}
async function setSetting(b) { try { S = await api("/api/settings", b); LANG = S.lang || autoLang(); applyTheme(); i18nApply(); openSettings(); renderSide(); cur ? renderTop() : (view.homeSig = "", renderHome()); } catch (e) { fail(e); } }

// ─── keys & boot ───
document.addEventListener("keydown", e => {
  const mod = e.ctrlKey || e.metaKey;
  if (mod && e.key.toLowerCase() === "n") { e.preventDefault(); go(""); }
  else if (mod && e.key.toLowerCase() === "k") { e.preventDefault(); openSide(); $("q").focus(); }
  else if (e.key === "Escape") {
    if ($("menu")) closeMenu(); else if (!$("modal").classList.contains("hidden")) closeModal();
    else if (cur && (task(cur) || {}).busy) stopTask(); else if (panel) closePanel();
  }
});
(async () => {
  LANG = autoLang(); i18nApply();
  await refresh(); i18nApply();
  const tid = new URLSearchParams(location.search).get("t");
  go(tid && task(tid) ? tid : "");
  setInterval(refresh, 4000);
})();
