# ───────────────────────────── i18n (terminal UI) ─────────────────────────────
# English is the source text. T("…") returns the Russian line when the UI language is ru. Keys with {} are templates:
# T("model → {}") also translates "model → gpt-5". Commands (/model), product words (model, provider, token, todo, API,
# MCP, skill, plugin) and file names stay as they are. The agent itself answers in the user's language either way.

UI_LANG = {"v": "en"}

RU = {
    "web search keys: Tavily (free), Exa, Brave": "ключи веб-поиска: Tavily (бесплатно), Exa, Brave",
    "Tavily key (tvly-…), empty to skip": "ключ Tavily (tvly-…), пусто — пропустить",
    "Web search works much better with a free Tavily key (1000 searches a month): sign up at https://app.tavily.com, copy the key and send /searchkey tvly-… — add several keys and they are used in turn. Exa and Brave work too: /searchkey exa KEY":
        "Веб-поиск работает намного лучше с бесплатным ключом Tavily (1000 поисков в месяц): зарегистрируйтесь на https://app.tavily.com, скопируйте ключ и отправьте /searchkey tvly-… Можно добавить несколько ключей — они расходуются по очереди. Подходят и Exa, Brave: /searchkey exa КЛЮЧ",
    # commands (/help, suggestions)
    "show commands & keys": "команды и клавиши", "switch mode: code, design, plan, review…": "режим агента: code, design, plan, review…",
    "create AGENTS.md for this project": "создать AGENTS.md для проекта", "switch model": "сменить модель (в этом чате)",
    "add / edit / sync models": "добавить / изменить / синхронизировать модели", "add / edit API providers": "добавить / изменить API-провайдеров",
    "reasoning effort: none…xhigh": "глубина рассуждений: none…xhigh", "edit your system prompt": "свой системный промпт",
    "switches: self-testing, todo, web, plan first, brief…": "переключатели: самопроверка, todo, веб, сначала план, кратко…",
    "list / toggle / create skills": "навыки (skills): список, вкл/выкл, создать", "extensions that modify me": "плагины, которые расширяют me",
    "set env secrets for skills": "секреты (env) для навыков", "new session": "новая сессия", "resume a past session": "вернуться к прошлой сессии",
    "sync this project folder to the cloud": "синхронизировать папку проекта с облаком", "clear screen + new session": "очистить экран + новая сессия",
    "copy last answer": "скопировать последний ответ", "summarize history to free context": "сжать историю, чтобы освободить контекст",
    "what fills the context window": "чем занят контекст", "auto-compact at N% of context (off)": "автосжатие при N% контекста (off)",
    "work autonomously until done+verified or blocked": "работать сам, пока не сделает и не проверит (или не упрётся)",
    "revert the files of the last turn": "откатить файлы последнего хода", "take back the last request and run it again": "отменить последний запрос и выполнить заново",
    "save this chat as Markdown": "сохранить чат в Markdown", "permissions: read-only / ask / full + allow-list": "разрешения: read-only / ask / full + список разрешённых",
    "spending: turn, session, today, limits": "расходы: задача, сессия, сегодня, лимиты", "Me AI plan: 5-hour and weekly limits": "тариф Me AI: лимиты на 5 часов и неделю", "invite friends: your link and rewards": "пригласить друзей: ссылка и бонусы", "MCP servers (lazy)": "MCP-серверы (подгружаются по требованию)",
    "show the agent's checklist": "показать todo-список агента", "toggle full access": "полный доступ вкл/выкл", "sync with cloud now": "синхронизировать сейчас",
    "log in / register": "войти / зарегистрироваться", "authenticator (TOTP) on/off": "двухфакторная защита (TOTP) вкл/выкл", "log out of cloud": "выйти из облака",
    "settings": "настройки", "update me": "обновить me", "color theme (installable)": "цветовая тема (можно ставить новые)", "quit": "выход",
    "go back to an earlier request (history + files)": "вернуться к одному из прошлых запросов (история + файлы)",
    "continue in a copy of this chat": "продолжить в копии этого чата", "quick side question, kept out of the history": "быстрый вопрос в сторону, не попадает в историю",
    "find text in all chats": "поиск по всем чатам", "nothing to rewind": "нечего откатывать",
    "Rewind — the chat goes back to before this request": "Откат — чат вернётся к моменту перед этим запросом",
    "↶ rewound · restored {} file(s): {}": "↶ откат · восстановлено файлов: {}: {}", "↶ rewound": "↶ откат выполнен",
    "forked: {} — the original chat stays in /sessions": "копия чата: {} — оригинал остался в /sessions",
    "Side question (does not go into the chat)": "Вопрос в сторону (не попадёт в чат)", "Search all chats": "Поиск по всем чатам",
    "nothing found: {}": "ничего не найдено: {}", "Found in {} chat(s)": "Найдено в чатах: {}", "✗ the command failed": "✗ команда завершилась с ошибкой",
    "no such request": "такого запроса нет",
    "what changed in this chat": "что изменилось в файлах за этот чат", "rename this chat": "переименовать чат", "interface language: en / ru": "язык интерфейса: en / ru",
    # welcome / status / input
    "{} loaded into this session": "{} загружен в эту сессию", "◇ no AGENTS.md — run /init so I remember this project": "◇ нет AGENTS.md — запустите /init, чтобы я запомнил проект",
    "commands": "команды", "files": "файлы", "interrupt": "прервать", "newline": "новая строка", "paste image": "вставить картинку", "copy": "копировать",
    "… +{} more lines": "… ещё строк: {}", "thought for {}": "думал {}", "thinking…": "думаю…", "working…": "работаю…", "writing…": "пишу…",
    "no model — /provider": "нет модели — /provider", "no model": "нет модели", "◇ no AGENTS.md (/init)": "◇ нет AGENTS.md (/init)", "≈ offline": "≈ офлайн",
    "full": "полный", "read-only": "только чтение", "ask anything, / for commands": "спросите что угодно, / — команды", "queue a message…": "сообщение в очередь…",
    "esc to interrupt": "esc — прервать", " ↑ {} more lines below · pgdn ": " ↑ ниже ещё строк: {} · pgdn ", "type to filter": "печатайте для поиска",
    "no matches": "ничего не найдено", "↑↓ select · enter confirm · esc cancel": "↑↓ выбор · enter ок · esc отмена",
    "y/n · ↑↓ enter · esc deny": "y/n · ↑↓ enter · esc запретить", "⚠ allow": "⚠ разрешить", "enter newline · ctrl+s save · esc cancel": "enter новая строка · ctrl+s сохранить · esc отмена",
    "enter confirm · esc cancel": "enter ок · esc отмена", "    … {} more": "    … ещё {}", "{} more lines": "ещё строк: {}", "running {}": "выполняется {}", "starting…": "запуск…",
    "  ◎ goal: {}": "  ◎ цель: {}",
    # input / clipboard / images / workspace
    "◌ queued ({}): {}": "◌ в очереди ({}): {}", "nothing to copy": "нечего копировать", "copy failed: no clipboard here": "не удалось скопировать: нет буфера обмена",
    "copied {} · {} chars": "скопировано: {} · символов: {}", "copied {} · {} chars (via terminal)": "скопировано: {} · символов: {} (через терминал)",
    "last answer": "последний ответ", "selection": "выделение", "image attached: {}": "картинка прикреплена: {}",
    "image attached: {} — this model has vision off (/models)": "картинка прикреплена: {} — у этой модели выключено зрение (/models)",
    "clipboard is empty": "буфер обмена пуст", "clipboard is empty (Linux needs wl-clipboard or xclip)": "буфер обмена пуст (на Linux нужен wl-clipboard или xclip)",
    "no model yet — let's set one up": "модели ещё нет — давайте подключим", "☁ session updated from another device": "☁ сессия обновлена с другого устройства",
    "☁ {} updates from cloud": "☁ обновлений из облака: {}", "☁ project linked as workspace {}": "☁ проект связан с рабочей областью {}",
    "☁ files ↑{} ↓{}": "☁ файлы ↑{} ↓{}", "✗ workspace sync: {}": "✗ синхронизация рабочей области: {}", "log in first — /login": "сначала войдите — /login",
    "↻ sync now": "↻ синхронизировать сейчас", "unlink this folder": "отвязать эту папку", "files stay in the cloud": "файлы останутся в облаке",
    "link this folder": "привязать эту папку", "get a cloud workspace into a folder": "скачать облачную рабочую область в папку",
    "Workspace — code files sync to the cloud (≤1 MB each, heavy/ignored skipped)": "Рабочая область — файлы кода синхронизируются с облаком (≤1 МБ каждый, тяжёлые и игнорируемые пропускаются)",
    "Workspace name": "Имя рабочей области", "Cloud workspaces": "Облачные рабочие области", "{} files · {}B": "файлов: {} · {}Б",
    "Local folder for this workspace": "Локальная папка для рабочей области", "☁ {}: ↓{} files · cwd {}": "☁ {}: ↓{} файлов · папка {}",
    # commands: agent, init, compact, theme, market, copy, perm, goal, behavior, retry, export, todo, cost, mcp
    "Agent mode": "Режим агента", "+ more agents": "+ ещё агенты", "mode → {} · tools: {}": "режим → {} · инструменты: {}",
    "busy — wait for the turn to finish": "занят — дождитесь конца хода", "nothing to compact yet": "пока нечего сжимать",
    "Auto-compact (now: {}{} of context, max 1M tokens)": "Автосжатие (сейчас: {}{} контекста, максимум 1M токенов)", "auto-compact {}": "автосжатие {}",
    "at {} tokens": "при {} токенов", "off": "выкл", "on": "вкл", "default": "по умолчанию", "Theme (↑↓ previews)": "Тема (↑↓ — предпросмотр)", "theme → {}": "тема → {}",
    "+ get more themes": "+ ещё темы", "Marketplace · {}s": "Маркетплейс · {}", "installed {} {}": "установлено: {} {}",
    "busy — esc to stop the current turn first": "занят — сначала остановите ход (esc)", "code block {}": "блок кода {}", "answer #{} from the end": "ответ №{} с конца",
    "whole conversation": "весь разговор", "{} chars": "символов: {}", "{} messages": "сообщений: {}", "no code block in that answer": "в этом ответе нет блока кода",
    "Copy": "Копировать", "permissions: {}": "разрешения: {}", "read-only — no file changes; only allow-listed commands": "только чтение — без изменения файлов; только разрешённые команды",
    "ask — edits inside the project are free; commands ask": "ask — правки внутри проекта без вопросов; команды спрашивают",
    "full access — everything runs without asking": "полный доступ — всё выполняется без вопросов", "  allow-list…": "  список разрешённых…",
    "commands that never ask": "команды, которые не спрашивают", "no file changes; only allow-listed commands": "без изменения файлов; только разрешённые команды",
    "edits inside the project are free; commands ask": "правки внутри проекта без вопросов; команды спрашивают", "everything runs without asking": "всё выполняется без вопросов",
    "Allowed commands, one pattern per line (* = anything; chained/redirected commands always ask)":
        "Разрешённые команды, по шаблону на строку (* = что угодно; цепочки и перенаправления всегда спрашивают)",
    "{} allowed patterns": "разрешённых шаблонов: {}", "Goal — the agent works until it is done and verified, or blocked": "Цель — агент работает, пока не сделает и не проверит, или пока не упрётся",
    "Behavior — enter toggles, esc closes": "Поведение — enter переключает, esc закрывает", "{} on": "{} вкл", "{} off": "{} выкл",
    "verify changes itself (tests, build, run)": "сам проверяет изменения (тесты, сборка, запуск)", "keep a todo checklist on multi-step tasks": "ведёт todo-список в многошаговых задачах",
    "search the web for current facts and docs": "ищет в интернете актуальные факты и доки", "stay in the project folder unless asked": "не выходит за папку проекта без просьбы",
    "show a plan and wait for a go before big changes": "перед большими изменениями показывает план и ждёт «да»", "very short replies": "очень короткие ответы",
    "explain the why (learning mode)": "объясняет «почему» (режим обучения)", "git commit each finished task": "git-коммит после каждой задачи",
    "change only what the task needs": "меняет только то, что нужно для задачи", "ask before adding dependencies": "спрашивает перед добавлением зависимостей", "ask when the request is unclear": "переспрашивает, если запрос неясен", "use subagents for big separate jobs": "использует сабагентов для больших отдельных задач", "global memory across chats": "глобальная память между чатами", "load matching skills before a task": "подбирает и загружает подходящие скиллы перед задачей", "show a project map (files and symbols) with the first task": "даёт карту проекта (файлы и функции) с первой задачей", "suggest the next message (tab to accept)": "подсказывает следующее сообщение (tab — принять)", "comment the code it writes": "комментирует код, который пишет", "watch for security issues": "следит за проблемами безопасности", "end with a short recap and next steps": "в конце — краткий итог и следующие шаги", "publish static sites at a public link (me cloud)": "публикует статические сайты по публичной ссылке (облако me)", "no refactors or cleanups of code you did not ask about": "без рефакторинга и «улучшений» кода, о котором не просили", "offer to create AGENTS.md in new projects": "предлагает создать AGENTS.md в новых проектах",
    "↻ retrying the last request": "↻ повторяю последний запрос", "busy": "занят", "nothing to retry": "нечего повторять", "nothing to undo": "нечего откатывать",
    "✓ chat saved to {}": "✓ чат сохранён в {}", "no todo list in this session": "в этой сессии нет todo-списка", "no changes in this chat yet": "в этом чате файлы ещё не менялись",
    "chat renamed: {}": "чат переименован: {}", "Chat name": "Название чата",
    "MCP servers — loaded lazily: one prompt line idle, tools fetched on use": "MCP-серверы — подгружаются лениво: строка в промпте, инструменты по запросу",
    "+ add server": "+ добавить сервер", "stdio command or http url": "команда stdio или http-адрес", "Name (e.g. github)": "Имя (например, github)",
    "Command (npx -y @modelcontextprotocol/server-github) or URL (https://…/mcp)": "Команда (npx -y @modelcontextprotocol/server-github) или URL (https://…/mcp)",
    "One-line description for the agent": "Описание для агента в одну строку", "MCP {} added": "MCP {} добавлен", "test: list tools": "проверка: список инструментов",
    "remove": "удалить",
    # models / providers
    "no models yet": "моделей пока нет", "Model": "Модель", "model → {} (this chat; new chats start with it too)": "модель → {} (в этом чате; новые чаты тоже начнутся с неё)",
    "Reasoning effort": "Глубина рассуждений", "effort → {}": "effort → {}", "Providers": "Провайдеры", "+ add provider": "+ добавить провайдера",
    "OpenAI-compatible URL + key": "OpenAI-совместимый URL + ключ", "edit URL / key": "изменить URL / ключ", "sync models from API": "загрузить модели из API",
    "delete": "удалить", "deleted {}": "удалено: {}", "Base URL": "Base URL", "any OpenAI-compatible endpoint, e.g. https://openrouter.ai/api/v1": "любой OpenAI-совместимый адрес, например https://openrouter.ai/api/v1",
    "API key": "API-ключ", "stored in your config, synced to your cloud account": "хранится в вашем конфиге и синхронизируется с облачным аккаунтом",
    "Provider name": "Имя провайдера", "provider {} saved": "провайдер {} сохранён", "Fetch models from this provider now?": "Загрузить модели этого провайдера сейчас?",
    "yes, sync models": "да, загрузить модели", "no": "нет", "fetching models from {}…": "загружаю модели из {}…", "✗ could not list models: {}": "✗ не удалось получить модели: {}",
    "provider returned no models": "провайдер не вернул ни одной модели", "✓ done": "✓ готово", "{} added": "добавлено: {}",
    "Add models from {} ({})": "Добавить модели из {} ({})", "enter toggles · type to filter": "enter — добавить/убрать · печатайте для поиска",
    "{} models added": "добавлено моделей: {}", "Models": "Модели", "↻ sync from provider": "↻ загрузить от провайдера", "+ add model manually": "+ добавить модель вручную",
    "Provider": "Провайдер", "add a provider first": "сначала добавьте провайдера", "Model id (as the API expects it)": "id модели (как его ждёт API)",
    "display name": "отображаемое имя", "API model id": "id модели в API", "context window, tokens": "окно контекста, токенов",
    "max output tokens (blank = default)": "максимум токенов ответа (пусто = по умолчанию)", "reasoning effort: none/minimal/low/medium/high/xhigh": "глубина рассуждений: none/minimal/low/medium/high/xhigh",
    "blank = default": "пусто = по умолчанию", "send images: true/false": "отправлять картинки: true/false",
    "$ per 1M tokens: in out [cached] (blank = built-in table)": "$ за 1M токенов: вход выход [кэш] (пусто = встроенная таблица)",
    "prompt caching: auto / anthropic / openai / off": "кэш промпта: auto / anthropic / openai / off", "Model · {}": "Модель · {}", "✓ use this model": "✓ использовать эту модель",
    "✗ delete": "✗ удалить", "model → {}": "модель → {}", "API style": "Тип API", "most compatible": "самый совместимый", "Prompt caching": "Кэш промпта",
    "detect from model/provider": "определить по модели/провайдеру", "in out [cached], $ per 1M tokens": "вход выход [кэш], $ за 1M токенов",
    "numbers, e.g. 3 15 0.3": "числа, например 3 15 0.3", "must be a number": "нужно число", "another model is already called {}": "модель с именем {} уже есть",
    "Vision": "Зрение (картинки)",
    # prompt, secrets, skills, plugins
    "Your system prompt (appended to me's base prompt)": "Ваш системный промпт (добавляется к базовому промпту me)",
    "ctrl+s to save · synced to all your devices": "ctrl+s — сохранить · синхронизируется на все устройства", "prompt saved": "промпт сохранён",
    "Secrets (env vars for skills)": "Секреты (переменные окружения для навыков)", "+ add secret": "+ добавить секрет", "env var for skills": "переменная окружения для навыков",
    "Name (e.g. GITHUB_TOKEN)": "Имя (например, GITHUB_TOKEN)", "Value": "Значение", "{} saved": "{} сохранён", "Skills": "Навыки (skills)",
    "+ new skill from template": "+ новый навык из шаблона", "+ ask me to write a skill": "+ попросить me написать навык", "+ install from URL": "+ установить по URL",
    "● on ○ off": "● вкл ○ выкл", "Skill name (a-z, 0-9, -)": "Имя навыка (a-z, 0-9, -)", "created {} — edit it, it's live immediately": "создан {} — правьте, работает сразу",
    "What should the skill do?": "Что должен делать навык?", "URL of a skill .py/.md": "URL навыка .py/.md", "installed {}": "установлено: {}", "Delete {}?": "Удалить {}?",
    "yes, delete": "да, удалить", "Plugins (changes apply after restart)": "Плагины (изменения — после перезапуска)", "+ browse plugin marketplace": "+ маркетплейс плагинов",
    "+ new plugin from template": "+ новый плагин из шаблона", "Plugin name": "Имя плагина", "created {} — restart me to load it": "создан {} — перезапустите me, чтобы загрузить",
    "Plugin marketplace": "Маркетплейс плагинов", "installed plugin {} — restart me (/update force or relaunch) to load it": "плагин {} установлен — перезапустите me (/update force или заново), чтобы загрузить",
    "view source": "посмотреть код",
    # sessions, cloud, login, 2fa, config, update
    "no sessions yet": "сессий пока нет", "Resume session": "Вернуться к сессии", "resumed · {} messages": "сессия открыта · сообщений: {}", "✗ workspace: {}": "✗ рабочая область: {}",
    "not logged in — /login": "вы не вошли — /login", "✗ sync failed: {}": "✗ синхронизация не удалась: {}", "synced · {} updated from cloud": "синхронизировано · из облака обновлено: {}",
    "Cloud account": "Облачный аккаунт", "log in": "войти", "create account": "создать аккаунт", "change server": "сменить сервер", "Server URL": "Адрес сервера",
    "Username": "Имя пользователя", "Password": "Пароль", "Repeat password": "Повторите пароль", "passwords don't match": "пароли не совпадают",
    "Authenticator code": "Код из приложения-аутентификатора", "Authenticator code (wrong, try again)": "Код из аутентификатора (неверный, попробуйте ещё)",
    "☁ logged in as {} — syncing…": "☁ вы вошли как {} — синхронизация…", "synced · {} items from cloud": "синхронизировано · из облака: {}",
    "logged out — local data kept": "вы вышли — локальные данные сохранены", "Authenticator is ON": "Двухфакторная защита включена", "keep it": "оставить", "turn off": "выключить",
    "Current authenticator code": "Текущий код аутентификатора", "2FA disabled": "2FA выключена", "Enter the 6-digit code to confirm": "Введите 6-значный код для подтверждения",
    "✓ 2FA enabled": "✓ 2FA включена", "Settings": "Настройки", "auto-approve tools   {}": "инструменты без вопросов   {}", "mouse                {}": "мышь                 {}",
    "on: wheel scroll, drag to select+copy · off: the terminal's own selection": "вкл: прокрутка колесом, выделение с копированием · выкл: выделение самого терминала",
    "show thinking        {}": "показывать рассуждения {}", "behavior switches…": "переключатели поведения…", "self-testing, todo, web, plan first, brief, explain, commit": "самопроверка, todo, веб, сначала план, кратко, объяснения, коммиты",
    "language             {}": "язык                 {}", "data dir": "папка данных", "notify when done     {}": "уведомлять о готовности {}",
    "a sound + a desktop notification when a task took over 30 s": "звук и системное уведомление, если задача шла дольше 30 с",
    "checking for updates…": "проверяю обновления…", "✗ bad update payload": "✗ повреждённое обновление", "already on latest v{}": "уже последняя версия v{}",
    "✓ updated to v{} — restarting…": "✓ обновлено до v{} — перезапуск…", "unknown command /{} — /help": "неизвестная команда /{} — /help",
    "Language": "Язык", "auto (system)": "авто (как в системе)", "language → {}": "язык → {}",
    # tool confirm, welcome, events
    "Yes": "Да", "Yes, always allow `{}`": "Да, и всегда разрешать `{}`", "Yes, and switch to full access (/perm)": "Да, и включить полный доступ (/perm)",
    "No, tell me what to do": "Нет, скажу, что делать", "Welcome to me — sync everything across your machines": "Добро пожаловать в me — всё синхронизируется между вашими устройствами",
    "log in / create account": "войти / создать аккаунт", "config, keys, skills & chats follow you everywhere": "настройки, ключи, навыки и чаты с вами на любом устройстве",
    "use offline": "работать офлайн", "you can /login any time": "войти можно в любой момент: /login", "☁ {} items synced from cloud": "☁ из облака синхронизировано: {}",
    "☁ sync failed: {}": "☁ синхронизация не удалась: {}", "☁ syncing…": "☁ синхронизация…",
    "Let's connect a model. Any OpenAI-compatible API works (OpenAI, OpenRouter, DeepSeek, Groq, Ollama, LM Studio…). Or /login for Me AI, the built-in model (free plan).":
        "Подключим модель. Подойдёт любой OpenAI-совместимый API (OpenAI, OpenRouter, DeepSeek, Groq, Ollama, LM Studio…). Или /login — и будет Me AI, встроенная модель (бесплатный тариф).",
    "↻ {} — retrying": "↻ {} — повторяю", "↻ {} — retrying in {}s": "↻ {} — повтор через {} с", "◌ compacting history ({})…": "◌ сжимаю историю ({})…",
    "✓ compacted {} → {} messages · context now ~{} tokens": "✓ сжато: {} → {} сообщений · контекст теперь ~{} токенов",
    "◌ self-check: changes were made but not run yet — verifying before finishing": "◌ самопроверка: изменения не запускались — проверяю перед завершением",
    "◎ goal: {}": "◎ цель: {}", "✓ goal done": "✓ цель достигнута", "✗ goal blocked": "✗ цель заблокирована", "interrupted": "прервано",
    "me · session {} · ↑{} ↓{}  (resume: /sessions)": "me · сессия {} · ↑{} ↓{}  (вернуться: /sessions)",
    "{} step": "{} шаг", "{} steps": "шагов: {}", "context {}": "контекст {}", "sent ↑{}": "отправлено ↑{}", "sent ↑{} ({}% cached)": "отправлено ↑{} ({}% из кэша)",
    "full access — tools run without asking": "полный доступ — инструменты без вопросов", "ask — commands and writes outside the project ask": "ask — команды и запись вне проекта спрашивают",
    "workspace-write (ask for commands)": "правки в проекте (команды спрашивают)", "read-only": "только чтение", "ask": "спрашивать", "full access": "полный доступ", "permissions: {}": "разрешения: {}",
    "dark · built-in": "тёмная · встроенная", "light · built-in": "светлая · встроенная", "dark": "тёмная", "light": "светлая",
    "marketplace": "маркетплейс", "{} · custom": "{} · своя", "unlinked": "отвязано", "Permissions": "Разрешения", "{} limit {}": "лимит ({}) {}", "✗ plugin {}: {}": "✗ плагин {}: {}",
    "✓ task done": "✓ задача готова", "me: task finished": "me: задача готова",
}

_RX = []  # (regex, ru template) for keys with {}


def _compile():
    for k, v in RU.items():
        if "{}" in k:
            _RX.append((re.compile("^" + re.escape(k).replace(r"\{\}", "(.+?)") + "$", re.S), v))


def set_ui_lang(settings):
    """settings.lang: "en", "ru" or "" (auto: the system language)."""
    v = os.environ.get("ME_LANG") or (settings or {}).get("lang") or ""  # ME_LANG: tests and scripts pin it
    if v not in ("en", "ru"):
        v = "ru" if system_lang().startswith("ru") else "en"
    UI_LANG["v"] = v
    if not _RX:
        _compile()
    return v


def system_lang():
    for k in ("LC_ALL", "LC_MESSAGES", "LANG", "LANGUAGE"):
        if os.environ.get(k):
            return os.environ[k].lower()
    if WIN:
        try:
            import ctypes
            return "ru" if ctypes.windll.kernel32.GetUserDefaultUILanguage() & 0x3FF == 0x19 else "en"
        except Exception:
            pass
    return "en"


def T(s):
    """The UI line in the user's language (English text = the key)."""
    if UI_LANG["v"] != "ru" or not isinstance(s, str) or not s:
        return s
    r = RU.get(s)
    if r is not None:
        return r
    st = s.strip()
    if st != s and st in RU:  # padding around a key (" no matches")
        return s[:len(s) - len(s.lstrip())] + RU[st] + s[len(s.rstrip()):]
    for rx, tpl in _RX:
        m = rx.match(s)
        if m:
            parts = tpl.split("{}")
            return "".join(p + (T(m.group(i + 1)) if i < len(parts) - 1 else "") for i, p in enumerate(parts))
    m = _MARK.match(s)  # "● ask", "[x] verify …", "✓ done": a marker in front of a known line
    if m and m.group(2) != s:
        rest = T(m.group(2))
        if rest != m.group(2):
            return m.group(1) + rest
    return s


_MARK = re.compile(r"^((?:[●○✓✗◆◇▸•☁↻◌]|\[[x ]\])?\s+|[●○✓✗◆◇▸•☁↻◌]\s*|\[[x ]\]\s*)(.+)$", re.S)
