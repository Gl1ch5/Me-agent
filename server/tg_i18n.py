"""Telegram bot: Russian for the bot's own words. Source strings stay English; send() translates whole messages and buttons,
so agent answers (never an exact match) pass through untouched. The language: the account's setting, else Telegram's."""
import re

RU = {
    "start a new session": "новая сессия", "resume a session": "вернуться к сессии", "choose the workspace for new tasks": "рабочая область для новых задач",
    "switch model": "сменить модель", "work until done and verified: /goal <what>": "работать до результата: /goal <что сделать>",
    "interrupt": "прервать", "revert the last turn's files": "откатить файлы последнего ответа", "run the last request again": "повторить последний запрос",
    "show the checklist": "чеклист", "summarize history": "сжать историю", "spending: this session and total": "расходы: сессия и всего",
    "global notes: /memory, /memory forget <id>": "заметки: /memory, /memory forget <id>", "background tasks: /jobs, /jobs run <id>": "фоновые задачи: /jobs, /jobs run <id>",
    "link to the site the agent built": "ссылка на сайт из песочницы", "published sites": "опубликованные сайты",
    "agent mode: code, design, plan…": "режим агента: code, design, plan…", "switches: self-testing, todo, web, brief…": "переключатели: самопроверка, todo, веб, краткость…",
    "reasoning effort": "глубина рассуждений", "rename this session": "переименовать сессию", "this session as a Markdown file": "сессия файлом Markdown",
    "web search keys: Tavily (free), Exa, Brave": "ключи веб-поиска: Tavily (бесплатно), Exa, Brave",
    "Web search works much better with a free Tavily key (1000 searches a month): sign up at https://app.tavily.com, copy the key and send /searchkey tvly-… — add several keys and they are used in turn. Exa and Brave work too: /searchkey exa KEY":
        "Веб-поиск работает намного лучше с бесплатным ключом Tavily (1000 поисков в месяц): зарегистрируйтесь на https://app.tavily.com, скопируйте ключ и отправьте /searchkey tvly-… Можно добавить несколько ключей — они расходуются по очереди. Подходят и Exa, Brave: /searchkey exa КЛЮЧ",
    "create a GitHub repo and start on it: /newrepo name": "создать репозиторий GitHub и начать в нём: /newrepo имя", "usage: /newrepo name": "так: /newrepo имя",
    "repository (or create one: /newrepo name):": "репозиторий (или создайте новый: /newrepo имя):", "no repositories yet. Create one: /newrepo name": "репозиториев пока нет. Создайте: /newrepo имя",
    "open this session": "открыть эту сессию",
    "new session in:": "новая сессия в проекте:", "+ new project": "+ новый проект",
    "⎇ GitHub repository…": "⎇ репозиторий GitHub…", "Sign in with GitHub, then send /new again.": "Войдите через GitHub и снова отправьте /new.",
    "sign in with GitHub": "войти через GitHub", "repository:": "репозиторий:", "no repositories found": "репозитории не найдены", "send /new again": "отправьте /new ещё раз",
    "find text in all sessions": "поиск по всем сессиям", "bot language: en / ru": "язык бота: en / ru",
    "where tools run (cloud = isolated sandbox)": "где работают инструменты", "GitHub connection status": "статус GitHub",
    "stop mirroring other sessions into this chat": "не дублировать сюда другие сессии", "all commands": "все команды",
    "new session": "новая сессия", "new session — write here": "новая сессия — пишите сюда", "interrupted": "прервано",
    "resume a session:": "вернуться к сессии:", "no sessions yet": "сессий пока нет", "workspace for new tasks:": "рабочая область для новых задач:",
    "no workspaces yet": "рабочих областей пока нет", "model:": "модель:", "no models — add one in the web account": "моделей нет — добавьте в аккаунте на сайте",
    "usage: /goal <what to achieve> — I work until it is done and verified, or tell you why it is blocked":
        "использование: /goal <цель> — работаю, пока не сделаю и не проверю, или скажу, что мешает",
    "still working — /stop first": "ещё работаю — сначала /stop", "◎ goal started — I'll write when it is done or blocked": "◎ цель поставлена — напишу, когда будет готово или что-то помешает",
    "nothing to undo yet": "пока нечего откатывать", "nothing to compact yet": "пока нечего сжимать", "compacting history…": "сжимаю историю…",
    "no checklist in this session yet": "в этой сессии пока нет чеклиста", "usage: /memory forget <id>": "использование: /memory forget <id>",
    "no notes yet — ask the agent to remember something": "заметок пока нет — попросите агента что-нибудь запомнить",
    "usage: /jobs run <id>": "использование: /jobs run <id>", "no background tasks yet — create one in the web account": "фоновых задач пока нет — создайте в аккаунте на сайте",
    "no project yet — start a task first": "проекта пока нет — сначала дайте задачу", "preview is available once the project runs on the server": "превью появится, когда проект запустится на сервере",
    "Cloud tasks always run in an isolated sandbox: files only in the workspace, no access to the server.\n"
    "Read-only / ask / full access apply to the local agent (terminal and `me web`); set it there with /perm.":
        "Облачные задачи всегда работают в изолированной песочнице: файлы только в рабочей области, без доступа к серверу.\n"
        "Режимы «только чтение / спрашивать / полный доступ» — для локального агента (терминал и `me web`), там же /perm.",
    "GitHub is connected: the agent can clone, commit, push and open PRs.": "GitHub подключён: агент может клонировать, коммитить, пушить и открывать PR.",
    "GitHub is not connected. Add a token in the web account (cloud → GitHub).": "GitHub не подключён. Добавьте токен в аккаунте на сайте (облако → GitHub).",
    "other sessions are no longer mirrored here (/quiet again to turn it back on)": "другие сессии сюда больше не дублируются (/quiet — включить обратно)",
    "mirroring is on: tasks from the web and terminal show up here": "дублирование включено: задачи из веба и терминала приходят сюда",
    "that question is no longer open": "этот вопрос уже закрыт", "that session no longer exists": "этой сессии больше нет",
    "resumed — continue here": "сессия открыта — продолжайте здесь", "resumed — continue writing": "сессия открыта — продолжайте",
    "The agent has a question.": "У агента вопрос.", "No answer in time — the agent continues without it.": "Ответа не было — агент продолжает без него.",
    "still working on the previous task — /stop to interrupt": "ещё работаю над прошлой задачей — /stop, чтобы прервать",
    "the previous session keeps working in the cloud — its answer will come here":
        "прошлая сессия продолжает работать в облаке — её ответ придёт сюда",
    "📥 queued — I'll take it into account as soon as I can": "📥 в очереди — учту, как только смогу",
    "working…": "работаю…", "sandbox stopped responding — try again": "песочница не отвечает — попробуйте ещё раз", "sandbox stopped": "песочница остановлена",
    "✓ answer sent": "✓ ответ отправлен", "(no text)": "(без текста)",
    "Linked. Write a task — it runs in your cloud sandbox and syncs to all your machines.\nThings started elsewhere show up here too. /help for commands.":
        "Подключено. Напишите задачу — она выполнится в облачной песочнице и синхронизируется на все устройства.\n"
        "Задачи, начатые в других местах, тоже приходят сюда. Команды — /help.",
    "This link has expired or belongs to another bot. Open your account on the me site → Telegram → connect.":
        "Ссылка устарела или от другого бота. Откройте аккаунт на сайте me → Telegram → подключить.",
    "Not linked yet. Open your account on the me site → Telegram → connect.": "Чат ещё не подключён. Откройте аккаунт на сайте me → Telegram → подключить.",
    "You're connected. Write a task — it runs in your cloud sandbox. /help for commands.":
        "Чат подключён. Напишите задачу — она выполнится в облачной песочнице. /help — команды.",
    "You are linked. /help for commands.": "Чат подключён. Команды — /help.",
    "↶ the last turn's files are restored": "↶ файлы последнего ответа восстановлены", "nothing to retry yet": "пока нечего повторять", "behavior:": "поведение агента:", "agent mode:": "режим агента:", "reasoning effort:": "глубина рассуждений:",
    "usage: /rename <title>": "использование: /rename <название>", "usage: /search <text>": "использование: /search <текст>",
    "nothing found": "ничего не найдено", "no sites yet — turn on publishing in /behavior and ask the agent to publish one":
        "сайтов пока нет — включите публикацию в /behavior и попросите агента опубликовать сайт",
    "bot language:": "язык бота:", "language: Russian": "язык: русский",
    "no session yet": "сессии пока нет", "open": "открыть", "↻ retry": "↻ повторить", "↶ undo": "↶ откатить", "＋ new": "＋ новая",
    "Write a task and it runs in your cloud sandbox. Voice, photos and files work too.":
        "Напишите задачу — она выполнится в облачной песочнице. Голосовые, фото и файлы тоже работают.",
}
RU_RX = [
    (re.compile(r"^cloning (.+)…$"), lambda m: f"клонирую {m[1]}…"),
    (re.compile(r"^(«[^\n]*») — earlier:$", re.M), lambda m: f"{m[1]} — что было раньше:"),
    (re.compile(r"\n\nresumed — continue (here|writing)$"), lambda m: "\n\nсессия открыта — продолжайте" + (" здесь" if m[1] == "here" else "")),
    (re.compile(r"^creating (.+)…$"), lambda m: f"создаю {m[1]}…"),
    (re.compile(r"^search keys: "), lambda m: "ключи поиска: "),
    (re.compile(r"<summary>(\d+) steps?</summary>"), lambda m: f"<summary>шагов: {m[1]}</summary>"),
    (re.compile(r"_working… (\d+)s_"), lambda m: f"_работаю… {m[1]} с_"),
    (re.compile(r"_done · ([\d.]+)s · ([\d,]+) tokens_"), lambda m: f"_готово · {m[1]} с · {m[2]} токенов_"),
    (re.compile(r"^unknown command (\S+) — /help$"), lambda m: f"неизвестная команда {m[1]} — /help"),
    (re.compile(r"^Reply with your answer \(you have (\d+) min\)\.$", re.M), lambda m: f"Напишите ответ (есть {m[1]} мин)."),
    (re.compile(r"^model → (.+)$"), lambda m: f"модель → {m[1]}"), (re.compile(r"^workspace set: (.+)$"), lambda m: f"рабочая область: {m[1]}"),
    (re.compile(r"^effort → (.+)$"), lambda m: f"глубина → {m[1]}"), (re.compile(r"^agent → (.+)$"), lambda m: f"режим → {m[1]}"),
    (re.compile(r"^renamed → (.+)$"), lambda m: f"переименовано → {m[1]}"), (re.compile(r"^site (\S+) deleted$"), lambda m: f"сайт {m[1]} удалён"),
    (re.compile(r"^job (\d+) queued — it runs in a minute$"), lambda m: f"задача {m[1]} в очереди — запустится в течение минуты"),
    (re.compile(r"^forgot (\S+) on all devices$"), lambda m: f"заметка {m[1]} удалена на всех устройствах"),
    (re.compile(r"^checklist (\d+)/(\d+)\n"), lambda m: f"чеклист {m[1]}/{m[2]}\n"),
    (re.compile(r"^this session: "), lambda m: "эта сессия: "), (re.compile(r"\nall sessions: "), lambda m: "\nвсе сессии: "),
    (re.compile(r"\n\(the daily limit and per-task limits are set in the web account\)"), lambda m: "\n(лимиты задаются в аккаунте на сайте)"),
    (re.compile(r"\n\n/jobs run <id> to start one now$"), lambda m: "\n\n/jobs run <id> — запустить сейчас"),
    (re.compile(r"^preview: "), lambda m: "превью: "), (re.compile(r"^error: "), lambda m: "ошибка: "),
    (re.compile(r"^(\d+) found:$"), lambda m: f"найдено: {m[1]}"),
]


def tr(text, lang):
    if lang != "ru" or not isinstance(text, str):
        return text
    if text in RU:
        return RU[text]
    for rx, to in RU_RX:
        text = rx.sub(to, text)
    return text


def tr_markup(markup, lang):
    """Button captions too (inline keyboards)."""
    if lang != "ru" or not markup or "inline_keyboard" not in markup:
        return markup
    return {**markup, "inline_keyboard": [[dict(b, text=tr(b.get("text", ""), lang)) for b in row] for row in markup["inline_keyboard"]]}
