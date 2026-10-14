// ─── i18n: English is the source text; RU is a lookup table. t("English") / tt("English") (retranslatable span) ───
const RU = {"files": "файлы", "empty folder": "пустая папка", "back": "назад", "ask about this file": "спросить про этот файл", "open a project folder": "открыть папку проекта", "(no folder)": "(без папки)", "cached": "из кэша", "context": "контекст", "every step re-sends the context; cached input costs a fraction": "каждый шаг заново отправляет контекст; кэшированный ввод стоит в разы дешевле", 
  "New chat": "Новый чат", "Chat": "Чат", "close menu": "закрыть меню", "account": "аккаунт", "local": "локально", "cloud": "облако",
  "notify me when a task is done": "уведомлять о завершении задачи", "theme": "тема", "Language": "Язык", "checklist": "чек-лист",
  "preview": "просмотр", "open the site the agent built": "открыть сайт, созданный агентом", "workspace": "рабочая область",
  "Ask anything…": "Спросите что угодно…", "model": "модель", "no model": "нет модели", "code": "код", "attach images or files": "прикрепить изображения или файлы",
  "hold to talk": "удерживайте, чтобы говорить", "permissions": "разрешения", "menu": "меню", "send": "отправить", "stop": "стоп",
  "what are we": "что будем", "building?": "строить?",
  "Runs in your cloud sandbox · files sync to all your machines": "Работает в облачной песочнице · файлы синхронизируются между вашими устройствами",
  "Running locally on this machine": "Работает локально на этом компьютере",
  "Today": "Сегодня", "This week": "На этой неделе", "Older": "Раньше", "no chats yet": "чатов пока нет", "(untitled)": "(без названия)",
  "Checklist": "Чек-лист", "Goal": "Цель", "Goal done": "Цель достигнута", "Goal blocked": "Цель заблокирована",
  "thinking…": "думаю…", "thought for {d}": "думал {d}", "working…": "работаю…", "copy": "копировать",
  "Allow {name}?": "Разрешить {name}?", "allow": "разрешить", "always allow": "всегда разрешать", "allow all (full)": "разрешить всё (полный доступ)", "deny": "отклонить",
  "answer…": "ответ…", "interrupted": "прервано",
  "compacting history…": "сжимаю историю…", "compacted {a} → {b} messages · ~{c} tokens": "история сжата: {a} → {b} сообщений · ~{c} токенов",
  "self-check: running checks before finishing": "самопроверка: запускаю проверки перед завершением",
  "{a} · ↑ {i} ↓ {o} · {n} tokens{c}": "{a} · ↑ {i} ↓ {o} · {n} токенов{c}",
  "sandbox restarted — your session and files are safe": "песочница перезапущена — сессия и файлы в безопасности",
  "Changes in this turn": "Изменения за этот ход", "{n} {files}": "{n} {files}", "new": "новый", "keep": "оставить", "revert": "откатить", "kept": "оставлено", "reverting…": "откатываю…", "reverted": "откатено",
  "ctx": "контекст", "this task": "эта задача", "session": "сессия", "today": "сегодня",
  "loaded into every turn": "загружается в каждый ход", "ask the agent to create it, or /init in the terminal": "попросите агента создать его или выполните /init в терминале", "no AGENTS.md": "нет AGENTS.md",
  "read-only": "только чтение", "ask": "спрашивать", "full access": "полный доступ", "sandbox": "песочница",
  "no file changes; only allow-listed commands": "без изменений файлов; только разрешённые команды",
  "edits inside the project are free; commands ask": "правки в проекте свободны; команды запрашивают подтверждение",
  "everything runs without asking": "всё выполняется без запроса",
  "Cloud sessions run in an isolated sandbox, so tools run without asking. Permissions apply to the local agent": "Облачные сессии работают в изолированной песочнице, поэтому инструменты выполняются без запроса. Разрешения относятся к локальному агенту",
  "terminal": "терминал",
  "No models — add one in your account or with /models.": "Нет моделей — добавьте их в аккаунте или командой /models.",
  "more agents in the marketplace →": "больше агентов в каталоге →", "more themes in the marketplace →": "больше тем в каталоге →",
  "Install more with /theme in the terminal or the marketplace on the site": "Установите другие командой /theme в терминале или в каталоге на сайте",
  "light": "светлая", "dark": "тёмная",
  "microphone: {m}": "микрофон: {m}", "transcribing…": "распознаю…",
  "Project folder for the local agent": "Папка проекта для локального агента",
  "remove": "удалить", "{name}: over 20 MB": "{name}: больше 20 МБ",
  "Notifications are blocked for this site — allow them in the browser settings.": "Уведомления для этого сайта заблокированы — разрешите их в настройках браузера.",
  "notifications: {m}": "уведомления: {m}",
  "notifications on — you will hear when a task is done or needs you": "уведомления включены — вы узнаете, когда задача выполнена или нужна ваша помощь",
};
const RUPL = {"step": ["шаг", "шага", "шагов"], file: ["файл", "файла", "файлов"], image: ["изображение", "изображения", "изображений"]};
const RUINV = Object.fromEntries(Object.entries(RU).map(([k, v]) => [v, k]));
let LANG = "en";
try { LANG = localStorage.getItem("me-lang") || ""; } catch (e) {}
if (LANG !== "ru" && LANG !== "en") LANG = (navigator.languages || [navigator.language || "en"]).some(l => /^(ru|be|kk)\b/i.test(l)) ? "ru" : "en";
document.documentElement.lang = LANG;
function t(s, v) {
  let r = LANG === "ru" && RU[s] !== undefined ? RU[s] : s;
  if (v) r = r.replace(/\{(\w+)\}/g, (m, k) => k in v ? v[k] : m);
  return r;
}
const untr = s => RUINV[s] || s;  // back to the English key (for text we stored translated)
function pl(n, one, many) {  // "1 file" / "3 files"; Russian has three forms
  if (LANG === "ru" && RUPL[one]) { const f = RUPL[one], a = n % 100, b = n % 10; return f[a > 10 && a < 20 ? 2 : b === 1 ? 0 : b > 1 && b < 5 ? 1 : 2]; }
  return n === 1 ? one : many;
}
const _e = s => String(s ?? "").replace(/[&<>"]/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"})[c]);
function tt(s, v) {  // HTML span that re-translates itself when the language flips
  return `<span data-t="${_e(s)}"${v ? ` data-v="${_e(JSON.stringify(v))}"` : ""}>${_e(t(s, v))}</span>`;
}
function i18nApply(root) {
  root = root || document;
  root.querySelectorAll("[data-i18n]").forEach(n => n.textContent = t(n.dataset.i18n));
  root.querySelectorAll("[data-i18n-title]").forEach(n => n.title = t(n.dataset.i18nTitle));
  root.querySelectorAll("[data-i18n-aria]").forEach(n => n.setAttribute("aria-label", t(n.dataset.i18nAria)));
  root.querySelectorAll("[data-i18n-ph]").forEach(n => n.placeholder = t(n.dataset.i18nPh));
  root.querySelectorAll("[data-t]").forEach(n => { let v; try { v = n.dataset.v ? JSON.parse(n.dataset.v) : undefined; } catch (e) {} n.textContent = t(n.dataset.t, v); });
}
function setLang(l) {
  LANG = l; try { localStorage.setItem("me-lang", l); } catch (e) {}
  document.documentElement.lang = l;
  document.querySelectorAll(".lang").forEach(g => { g.classList.toggle("ru", l === "ru"); g.querySelectorAll("button").forEach(b => b.setAttribute("aria-pressed", b.dataset.l === l)); });
  i18nApply();
  const ti = $("title"); ti.textContent = t(untr(ti.textContent));
  if (typeof renderChrome === "function") { renderChrome(); renderStatus(); renderSessions(); renderTodo(state.session?.todo || []); if (log.querySelector(".empty")) emptyState(); }
  $("input").placeholder = t("Ask anything…");
}
document.addEventListener("click", e => { const b = e.target.closest && e.target.closest(".lang button"); if (b) setLang(b.dataset.l); });
