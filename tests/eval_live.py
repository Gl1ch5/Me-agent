"""Live eval: real tasks against a real model, scored automatically. Measures how well the harness + model reach the goal
and follow instructions (scope, constraints, language), plus time/tokens/steps.
Run: ME_HOME=~/.me python3 tests/eval_live.py [--model NAME] [--only a,b] [--agent code] [-j 3]
Uses a copy of ME_HOME's config (providers/models), never syncs, never touches the real home."""
import argparse, json, os, re, shutil, subprocess, sys, tempfile, time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable


def sh(cmd, cwd, err=True):
    p = subprocess.run(cmd, cwd=cwd, shell=True, capture_output=True, text=True, timeout=120)
    return p.returncode, p.stdout + (p.stderr if err else "")


def git_init(d):
    Path(d, ".gitignore").write_text("__pycache__/\n.eval.log\n")
    sh("git init -q && git add -A && git -c user.email=e@x -c user.name=e commit -qm init", d)


def changed(d):
    return [l[3:] for l in sh("git status --porcelain", d)[1].splitlines() if l.strip()]


# ── tasks: files, prompt, check(dir, output) -> (ok, note) ──────────────────────────────────────────
T = {}


def task(name, files, prompt):
    def deco(fn):
        T[name] = (files, prompt, fn)
        return fn
    return deco


@task("bugfix", {
    "calc/__init__.py": "",
    "calc/stats.py": "def mean(xs):\n    return sum(xs) / len(xs)\n\n\ndef median(xs):\n    s = sorted(xs)\n    n = len(s)\n    return s[n // 2]\n",
    "tests/__init__.py": "",
    "tests/test_stats.py": "import unittest\nfrom calc.stats import mean, median\n\n\nclass T(unittest.TestCase):\n    def test_mean(self):\n        self.assertEqual(mean([1, 2, 3]), 2)\n\n    def test_median_odd(self):\n        self.assertEqual(median([3, 1, 2]), 2)\n\n    def test_median_even(self):\n        self.assertEqual(median([4, 1, 3, 2]), 2.5)\n\n    def test_empty(self):\n        with self.assertRaises(ValueError):\n            mean([])\n        with self.assertRaises(ValueError):\n            median([])\n\n\nif __name__ == '__main__':\n    unittest.main()\n",
}, "The tests in tests/test_stats.py fail. Fix the code so they pass. Do not change the tests.")
def _(d, out):
    rc, o = sh(f"{PY} -m unittest -q tests.test_stats", d)
    if "tests/test_stats.py" in changed(d):
        return False, "edited the tests"
    return rc == 0, "tests pass" if rc == 0 else o[-300:]


@task("feature", {
    "todo.py": "import sys\n\nITEMS = [\"buy milk\", \"write report\", \"call mom\"]\n\n\ndef main(argv):\n    for i, t in enumerate(ITEMS, 1):\n        print(f\"{i}. {t}\")\n\n\nif __name__ == \"__main__\":\n    main(sys.argv[1:])\n",
}, "Add a --json flag to todo.py: with it, print the items as a JSON array of objects {\"id\": n, \"text\": ...} where id is the item's position in the full list, from 1. "
   "Without the flag the output must stay exactly as it is now. Also add --grep WORD that keeps only items containing WORD (case-insensitive); it works with and without --json, and keeps the original numbers/ids.")
def _(d, out):
    rc, a = sh(f"{PY} todo.py", d)
    if a != "1. buy milk\n2. write report\n3. call mom\n":
        return False, "plain output changed: " + a[:120]
    rc, b = sh(f"{PY} todo.py --json", d)
    try:
        ok = json.loads(b) == [{"id": 1, "text": "buy milk"}, {"id": 2, "text": "write report"}, {"id": 3, "text": "call mom"}]
    except ValueError:
        return False, "--json is not JSON: " + b[:120]
    rc, c = sh(f"{PY} todo.py --grep MOM --json", d)
    rc2, e = sh(f"{PY} todo.py --grep write", d)
    try:
        ok = ok and json.loads(c) == [{"id": 3, "text": "call mom"}] and e.strip() == "2. write report"
    except ValueError:
        return False, "--grep --json is not JSON: " + c[:120]
    return ok, "ok" if ok else f"grep: {c[:80]!r} / {e[:80]!r}"


@task("rename_scope", {
    "shop/cart.py": "from shop.price import calc_total\n\n\nclass Cart:\n    def __init__(self):\n        self.items = []\n\n    def total(self):\n        return calc_total(self.items)  # TODO: cache this\n",
    "shop/price.py": "TAX = 0.2\n\n\ndef calc_total(items):\n    s=sum(p for _,p in items)\n    return round(s*(1+TAX),2)\n",
    "shop/report.py": "from shop.price import calc_total\n\n\ndef report(cart):\n    return f\"total: {calc_total(cart.items)}\"\n",
    "shop/__init__.py": "",
    "README.md": "# shop\nUse `calc_total(items)` to get the price with tax.\n",
}, "Rename the function calc_total to total_with_tax everywhere (code and README). Change nothing else: no reformatting, no fixing TODOs, no new files.")
def _(d, out):
    src = {p: (Path(d) / p).read_text() for p in ("shop/cart.py", "shop/price.py", "shop/report.py", "README.md")}
    if any("calc_total" in v for v in src.values()):
        return False, "calc_total left"
    files, prompt, _f = T["rename_scope"]
    for p, v in src.items():
        if v != files[p].replace("calc_total", "total_with_tax"):
            return False, f"extra changes in {p}"
    extra = [c for c in changed(d) if c not in src]
    return not extra, "exact" if not extra else f"extra files: {extra}"


@task("readonly_answer", {
    "lib/rules.py": "def fee(amount, vip=False):\n    if amount <= 0:\n        return 0\n    base = 5 if amount < 100 else amount * 0.03\n    if vip:\n        base /= 2\n    return round(base + 0.5, 2)\n",
}, "Не меняя файлы, ответь: какую комиссию вернёт fee(200, vip=True) из lib/rules.py? Ответь одним числом в конце.")
def _(d, out):
    if changed(d):
        return False, f"changed files: {changed(d)}"
    tail = out.strip().splitlines()[-6:]
    ok = any(re.search(r"(?<![\d.])3\.5(?![\d])", l) for l in tail)
    return ok, "3.5" if ok else "wrong answer: " + " | ".join(tail)[-160:]


@task("multifile", {
    "shapes/__init__.py": "from shapes.registry import REGISTRY, make\nfrom shapes import circle, square  # noqa: F401  (registers)\n",
    "shapes/registry.py": "REGISTRY = {}\n\n\ndef register(name):\n    def deco(cls):\n        REGISTRY[name] = cls\n        return cls\n    return deco\n\n\ndef make(name, *a):\n    return REGISTRY[name](*a)\n",
    "shapes/circle.py": "import math\nfrom shapes.registry import register\n\n\n@register(\"circle\")\nclass Circle:\n    def __init__(self, r):\n        self.r = r\n\n    def area(self):\n        return math.pi * self.r ** 2\n\n    def perimeter(self):\n        return 2 * math.pi * self.r\n",
    "shapes/square.py": "from shapes.registry import register\n\n\n@register(\"square\")\nclass Square:\n    def __init__(self, a):\n        self.a = a\n\n    def area(self):\n        return self.a ** 2\n\n    def perimeter(self):\n        return 4 * self.a\n",
    "cli.py": "import sys\nfrom shapes import make\n\nkind, *nums = sys.argv[1:]\ns = make(kind, *map(float, nums))\nprint(f\"area={s.area():.2f} perimeter={s.perimeter():.2f}\")\n",
}, "Add a triangle shape given by three sides (Heron's formula for area), the same way circle and square are done, so that "
   "`python cli.py triangle 3 4 5` works. Invalid sides (triangle inequality violated or non-positive) must raise ValueError.")
def _(d, out):
    rc, o = sh(f"{PY} cli.py triangle 3 4 5", d)
    if "area=6.00 perimeter=12.00" not in o:
        return False, o[-200:]
    rc, o = sh(f"{PY} -c \"from shapes import make\ntry:\n  make('triangle',1,2,10)\nexcept ValueError:\n  print('VE')\"", d)
    return "VE" in o, "ok" if "VE" in o else "no ValueError: " + o[-150:]


@task("crlf_edit", {}, "In config.ini change the port in the [server] section to 9090. Touch nothing else.")
def _(d, out):
    b = (Path(d) / "config.ini").read_bytes()
    want = CRLF.replace(b"port = 8080\r\nhost", b"port = 9090\r\nhost")
    return b == want, "exact, CRLF kept" if b == want else f"diff: {b[:200]!r}"


CRLF = (b"; app config\r\n[server]\r\nport = 8080\r\nhost = 0.0.0.0\r\n\r\n[metrics]\r\nport = 8080\r\nenabled = yes\r\n")


@task("find_and_fix", dict({f"pkg/mod{i}.py": f"def f{i}(x):\n    return x + {i}\n" for i in range(25)}, **{
    "pkg/__init__.py": "",
    "pkg/mod17.py": "RETRY_LIMIT = 3\n\n\ndef f17(x):\n    return x + 17\n",
    "main.py": "from pkg.mod17 import RETRY_LIMIT\n\nprint('retries', RETRY_LIMIT)\n",
}), "Somewhere in this project the retry limit is set to 3. Make it 5. Don't change anything else.")
def _(d, out):
    rc, o = sh(f"{PY} main.py", d)
    ch = changed(d)
    return o.strip() == "retries 5" and ch == ["pkg/mod17.py"], f"{o.strip()} · changed {ch}"


@task("todo_plan", {
    "app.py": "def greet(name):\n    return 'Hello ' + name\n",
}, "Do these three steps: 1) make greet strip whitespace from name and title-case it; 2) add a function farewell(name) with the same "
   "normalization returning 'Bye <Name>'; 3) add tests in test_app.py (unittest) for both. Run the tests.")
def _(d, out):
    rc, o = sh(f"{PY} -m unittest -q test_app", d)
    rc2, o2 = sh(f"{PY} -c \"import app;print(app.greet('  ann lee '), '|', app.farewell(' bob'))\"", d)
    ok = rc == 0 and o2.strip() == "Hello Ann Lee | Bye Bob"
    return ok, "ok" if ok else (o2.strip() or o)[-200:]


@task("agents_md_rule", {
    "AGENTS.md": "# Project rules\n- Standard library only.\n- Every public function has type hints and a one-line docstring.\n- Never use print in library code; use the `log` logger from utils.py.\n",
    "utils.py": "import logging\n\nlog = logging.getLogger(\"app\")\n\n\ndef clamp(x: float, lo: float, hi: float) -> float:\n    \"\"\"Limit x to [lo, hi].\"\"\"\n    return max(lo, min(hi, x))\n",
}, "Add a slugify(text) function to utils.py: lowercase, transliterate nothing, keep [a-z0-9], turn runs of anything else into one '-', strip '-' at the ends. "
   "Log a warning when the result is empty.")
def _(d, out):
    import ast
    src = (Path(d) / "utils.py").read_text()
    fn = next((n for n in ast.walk(ast.parse(src)) if isinstance(n, ast.FunctionDef) and n.name == "slugify"), None)
    if not fn:
        return False, "no slugify"
    bad = []
    if not fn.returns or not all(a.annotation for a in fn.args.args):
        bad.append("no type hints")
    if not ast.get_docstring(fn):
        bad.append("no docstring")
    if "print(" in src:
        bad.append("uses print")
    rc, o = sh(f"{PY} -c \"from utils import slugify as s;print(s('  Hello, World!! 2026 '), s('--a__b--'), repr(s('!!!')))\"", d, err=False)
    if o.strip().splitlines()[-1:] != ["hello-world-2026 a-b ''"]:
        bad.append("wrong output: " + o.strip()[-80:])
    return not bad, ", ".join(bad) or "follows AGENTS.md"


@task("hidden_tests", {}, "Create lru.py with a class LRUCache(capacity, ttl=None): get(key, default=None), put(key, value), __len__, __contains__. "
   "Least-recently-used entries are evicted past capacity; get and put both count as use. With ttl (seconds), entries older than ttl are misses "
   "and do not count in len. Standard library only.")
def _(d, out):
    test = (
        "import time\nfrom lru import LRUCache\n"
        "c = LRUCache(2)\nc.put('a', 1); c.put('b', 2); c.get('a'); c.put('c', 3)\n"
        "assert 'b' not in c and c.get('a') == 1 and c.get('c') == 3 and len(c) == 2, 'eviction'\n"
        "c.put('a', 9); c.put('d', 4)\nassert c.get('a') == 9 and 'c' not in c, 'put refreshes'\n"
        "assert c.get('zz', 'dflt') == 'dflt'\n"
        "t = LRUCache(5, ttl=0.3)\nt.put('x', 1)\nassert t.get('x') == 1\ntime.sleep(0.45)\n"
        "assert t.get('x') is None and 'x' not in t and len(t) == 0, 'ttl'\nprint('HIDDEN-OK')\n")
    Path(d, "_hidden.py").write_text(test)
    rc, o = sh(f"{PY} _hidden.py", d)
    return "HIDDEN-OK" in o, "ok" if "HIDDEN-OK" in o else o.strip()[-200:]


@task("cross_file_bug", {
    "billing/__init__.py": "",
    "billing/money.py": "TAX = 0.25\n\n\ndef with_tax(amount):\n    return round(amount * (1 + TAX), 2)\n",
    "billing/lines.py": "from billing.money import with_tax\n\n\ndef line_total(qty, price):\n    return with_tax(qty * price)\n",
    "billing/invoice.py": "from billing.lines import line_total\nfrom billing.money import with_tax\n\n\ndef invoice_total(lines):\n    return with_tax(sum(line_total(q, p) for q, p in lines))\n",
    "main.py": "from billing.invoice import invoice_total\n\nprint(invoice_total([(2, 10.0), (1, 20.0)]))\n",
}, "Customers say invoice totals are too high: 2×10 + 1×20 with 25% tax should be 50.0 but main.py prints something else. "
   "Find the cause and fix it. line_total is also used elsewhere to show per-line prices with tax, so keep that behaviour.")
def _(d, out):
    rc, o = sh(f"{PY} main.py", d)
    rc2, o2 = sh(f"{PY} -c \"from billing.lines import line_total;print(line_total(2, 10.0))\"", d)
    ok = o.strip() == "50.0" and o2.strip() == "25.0"
    return ok, f"invoice {o.strip()} · line {o2.strip()}"


@task("scope_discipline", {
    "calc.py": "def div(a, b):\n    return a / b\n\n\ndef pct(part, whole):\n    return div(part, whole) * 100\n",
    "test_calc.py": "import unittest\nfrom calc import div, pct\n\n\nclass T(unittest.TestCase):\n    def test_div_zero(self):\n        self.assertIsNone(div(1, 0))\n\n"
                    "    def test_pct(self):\n        self.assertEqual(pct(1, 4), 25.0)\n\n    def test_pct_zero(self):  # planned: issue #12, do not touch\n"
                    "        self.assertEqual(pct(1, 0), 0.0)\n\n\nif __name__ == '__main__':\n    unittest.main()\n",
}, "div(1, 0) should return None instead of raising. Make test_div_zero pass. Change only div in calc.py: pct and the tests stay "
   "exactly as they are, even if another test still fails.")
def _(d, out):
    rc, o = sh(f"{PY} -m unittest -q test_calc.T.test_div_zero test_calc.T.test_pct", d)
    src = (Path(d) / "calc.py").read_text()
    bad = [] if rc == 0 else ["tests fail: " + o.strip()[-120:]]
    if changed(d) != ["calc.py"]:
        bad.append(f"changed {changed(d)}")
    if "def pct(part, whole):\n    return div(part, whole) * 100\n" not in src:
        bad.append("pct changed")
    return not bad, ", ".join(bad) or "only div changed"


@task("json_only", {
    "lib/__init__.py": "",
    "lib/a.py": "def load(p):\n    pass\n\n\ndef _cache():\n    pass\n\n\nclass Store:\n    def save(self):\n        pass\n",
    "lib/b.py": "import os\n\n\ndef dump(x):\n    pass\n\n\nasync def fetch(url):\n    pass\n",
}, "List the public top-level functions defined in lib/ (not methods, not names starting with _). "
   "Reply with only a JSON array of their names sorted alphabetically: no prose, no code fence.")
def _(d, out):
    if changed(d):
        return False, f"changed files: {changed(d)}"
    try:
        v = json.loads(out.strip())
    except ValueError:
        return False, "not bare JSON: " + out.strip()[-120:]
    return v == ["dump", "fetch", "load"], json.dumps(v)


@task("refactor_keep_api", {
    "counter.py": "_count = 0\n\n\ndef inc(n=1):\n    global _count\n    _count += n\n    return _count\n\n\ndef get():\n    return _count\n\n\ndef reset():\n    global _count\n    _count = 0\n",
    "app.py": "import counter\n\ncounter.inc()\ncounter.inc(5)\nprint(counter.get())\ncounter.reset()\nprint(counter.get(), counter.inc())\n",
}, "Refactor counter.py: put the state in a class Counter with inc(n=1), get() and reset(), so several independent counters can exist. "
   "The module-level inc/get/reset must keep working exactly as before (backed by one default Counter), and app.py must not change.")
def _(d, out):
    rc, o = sh(f"{PY} app.py", d)
    bad = [] if o.split() == ["6", "0", "1"] else ["app.py prints " + o.strip()[-80:]]
    if "app.py" in changed(d):
        bad.append("app.py changed")
    rc, o = sh(f"{PY} -c \"from counter import Counter\na, b = Counter(), Counter()\na.inc(); a.inc(2); b.inc()\nprint(a.get(), b.get())\"", d)
    if o.split() != ["3", "1"]:
        bad.append("Counter: " + o.strip()[-80:])
    return not bad, ", ".join(bad) or "ok"


# ── the owner's everyday jobs: a Telegram bot, a website, a game (run with --only tgbot,site,game) ──
NODE = shutil.which("node")


@task("tgbot", {}, "Напиши Telegram-бота в bot.py только на стандартной библиотеке Python (urllib, long polling через getUpdates). "
      "Токен бери из переменной окружения BOT_TOKEN. Команды: /start — приветствие, /echo ТЕКСТ — отвечает этим текстом, "
      "/sum 2 3 4 — сумма чисел, любой другой текст — «Длина: N» (N — число символов). Вынеси логику ответа в функцию "
      "handle(text) -> str, а запуск бота — под if __name__ == '__main__'.")
def _(d, out):
    if not Path(d, "bot.py").is_file():
        return False, "no bot.py"
    code = ("import bot\n"
            "assert bot.handle('/start').strip(), 'start'\n"
            "assert bot.handle('/echo привет мир').strip() == 'привет мир', bot.handle('/echo привет мир')\n"
            "assert '9' in bot.handle('/sum 2 3 4'), bot.handle('/sum 2 3 4')\n"
            "assert '5' in bot.handle('abcde'), bot.handle('abcde')\n"
            "src = open('bot.py', encoding='utf-8').read()\n"
            "assert 'BOT_TOKEN' in src and 'getUpdates' in src, 'token/polling'\n"
            "assert not any(m in src for m in ('import telebot', 'import aiogram', 'from telegram')), 'third-party lib'\n")
    rc, o = sh(f"{PY} -c \"{code}\"", d)
    return rc == 0, "handle() works" if rc == 0 else o[-300:]


@task("site", {}, "Сделай одностраничный сайт кофейни «Bean There»: index.html, styles.css, script.js. Нужны: шапка с меню-навигацией, "
      "блок меню минимум из 4 напитков с ценами в рублях (₽), форма обратной связи (имя, email, сообщение) с проверкой на JS — "
      "при неверном email показать ошибку и не отправлять форму. Сайт должен нормально выглядеть на телефоне (360px).")
def _(d, out):
    p = {n: Path(d, n) for n in ("index.html", "styles.css", "script.js")}
    miss = [n for n, f in p.items() if not f.is_file()]
    if miss:
        return False, "missing " + ", ".join(miss)
    h, c, j = (p[n].read_text("utf-8", "replace") for n in ("index.html", "styles.css", "script.js"))
    probs = []
    if "Bean There" not in h:
        probs.append("no name")
    if "styles.css" not in h or "script.js" not in h:
        probs.append("css/js not linked")
    if len(re.findall(r"\d+\s*(?:₽|руб)", h + j)) < 4:
        probs.append("fewer than 4 prices")
    if not re.search(r"<form", h, re.I) or not re.search(r'(?:type|name|id)=["\']?[\w-]*email', h, re.I):
        probs.append("no form/email field")
    if "@media" not in c and "viewport" not in h:
        probs.append("not responsive")
    if not re.search(r"preventDefault|return false", j):
        probs.append("form not stopped on error")
    if NODE and sh(f"{NODE} --check script.js", d)[0]:
        probs.append("script.js has a syntax error")
    return not probs, "; ".join(probs) or "site ok"


@task("game", {}, "Сделай браузерную игру «Змейка»: index.html и game.js, canvas, управление стрелками, счёт на экране, конец игры при "
      "ударе о стену или себя, рестарт пробелом. Логику держи в чистых функциях: newGame() возвращает состояние "
      "{snake: [{x, y}, …] (голова первая), dir: {x, y}, food: {x, y}, score, over}, step(state) делает один ход и возвращает "
      "новое состояние. Чтобы логику можно было проверить в Node, в конце game.js: "
      "if (typeof module !== 'undefined') module.exports = {newGame, step};")
def _(d, out):
    if not Path(d, "game.js").is_file() or not Path(d, "index.html").is_file():
        return False, "missing files"
    h = Path(d, "index.html").read_text("utf-8", "replace")
    if "<canvas" not in h or "game.js" not in h:
        return False, "no canvas or game.js not loaded"
    if not NODE:
        return True, "files ok (no node to test logic)"
    Path(d, ".t.js").write_text(  # any DOM access at load time hits a stub that answers everything
        "const S = new Proxy(function () {}, {get: (t, k) => k === Symbol.toPrimitive ? () => 0 : k === 'then' ? undefined : S, "
        "apply: () => S, construct: () => S, set: () => true});\n"
        "for (const k of ['document', 'window', 'localStorage', 'Image', 'Audio', 'navigator']) global[k] = S;\n"
        "global.requestAnimationFrame = global.setInterval = global.setTimeout = () => 0; global.addEventListener = () => {};\n"
        "const g = require('./game.js'); let s = g.newGame(); if (!s.dir.x && !s.dir.y) s.dir = {x: 1, y: 0}; const h0 = {...s.snake[0]}, n0 = s.snake.length, sc = s.score;\n"
        "s.food = {x: h0.x + s.dir.x, y: h0.y + s.dir.y}; s = g.step(s) || s;\n"
        "if (s.snake[0].x !== h0.x + s.dir.x || s.snake[0].y !== h0.y + s.dir.y) throw 'head did not move';\n"
        "if (!(s.score > sc) || s.snake.length !== n0 + 1) throw 'eating food: no score/growth';\n"
        "for (let i = 0; i < 300 && !s.over; i++) { s.food = {x: -99, y: -99}; s = g.step(s) || s; }\n"
        "if (!s.over) throw 'no game over at the wall'; console.log('ok');\n")
    rc, o = sh(f"{NODE} .t.js", d)
    return rc == 0, "logic ok" if rc == 0 else o[-300:]


def run_one(name, home, agent, timeout):
    files, prompt, check = T[name]
    d = tempfile.mkdtemp(prefix=f"eval-{name}-")
    for p, v in files.items():
        f = Path(d) / p
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(v)
    if name == "crlf_edit":
        (Path(d) / "config.ini").write_bytes(CRLF)
    git_init(d)
    env = dict(os.environ, ME_HOME=home, ME_LANG="en", PYTHONIOENCODING="utf-8")
    t0 = time.time()
    try:
        p = subprocess.run([PY, str(ROOT / "me.py"), "-p", "-y", prompt], cwd=d, env=env, capture_output=True, text=True, timeout=timeout)
        ans, out = p.stdout, p.stdout + p.stderr
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or b"").decode() if isinstance(e.stdout, bytes) else (e.stdout or "")
        ans = out = out + "\n[timeout]"
    dt = time.time() - t0
    try:
        ok, note = check(d, ans)  # checks see only the reply; tool lines go to stderr
    except Exception as e:
        ok, note = False, f"check crashed: {e}"
    steps = len(re.findall(r"^· ", out, re.M))
    m = re.search(r"↑([\d.]+k?) ↓([\d.]+k?)", out)
    Path(d, ".eval.log").write_text(out)
    return {"task": name, "ok": ok, "note": note, "sec": round(dt), "steps": steps, "tok": m.group(0) if m else "", "dir": d}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model")
    ap.add_argument("--agent", default="")
    ap.add_argument("--only", default="")
    ap.add_argument("-j", type=int, default=3)
    ap.add_argument("--timeout", type=int, default=600)
    ap.add_argument("--reps", type=int, default=1, help="run each task N times (models are noisy)")
    a = ap.parse_args()
    src = Path(os.environ.get("ME_HOME") or Path.home() / ".me")
    home = tempfile.mkdtemp(prefix="eval-home-")
    cfg = json.loads((src / "config.json").read_text())
    if a.model:
        cfg["model"] = a.model
    cfg["agent"] = a.agent
    cfg.setdefault("settings", {}).pop("behavior", None)
    Path(home, "config.json").write_text(json.dumps(cfg))
    Path(home, "auth.json").write_text(json.dumps({"skip": True}))
    for sub in ("skills", "agents", "plugins"):
        if (src / sub).is_dir():
            shutil.copytree(src / sub, Path(home) / sub)
    names = [n for n in T if not a.only or n in a.only.split(",")] * a.reps
    print(f"model {cfg['model']} · agent {a.agent or 'code'} · {len(names)} tasks")
    with ThreadPoolExecutor(a.j) as ex:
        res = list(ex.map(lambda n: run_one(n, home, a.agent, a.timeout), names))
    for r in res:
        print(f"{'PASS' if r['ok'] else 'FAIL'} {r['task']:16} {r['sec']:4}s {r['steps']:3} steps {r['tok']:16} {r['note'][:90]}  {r['dir']}")
    print(f"{sum(r['ok'] for r in res)}/{len(res)} passed")
    if a.reps > 1:  # averages per task: steps and time say how efficiently it got there
        for n in dict.fromkeys(names):
            rs = [r for r in res if r["task"] == n]
            print(f"  {n:16} {sum(r['ok'] for r in rs)}/{len(rs)} ok · {sum(r['steps'] for r in rs) / len(rs):.1f} steps · "
                  f"{sum(r['sec'] for r in rs) / len(rs):.0f}s avg")


if __name__ == "__main__":
    main()
