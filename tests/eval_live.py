"""Live eval: real tasks against a real model, scored automatically. Measures how well the harness + model reach the goal
and follow instructions (scope, constraints, language), plus time/tokens/steps.
Run: ME_HOME=~/.me python3 tests/eval_live.py [--model NAME] [--only a,b] [--agent code] [-j 3]
Uses a copy of ME_HOME's config (providers/models), never syncs, never touches the real home."""
import argparse, json, os, re, shutil, subprocess, sys, tempfile, time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable


def sh(cmd, cwd):
    p = subprocess.run(cmd, cwd=cwd, shell=True, capture_output=True, text=True, timeout=120)
    return p.returncode, p.stdout + p.stderr


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
        out = p.stdout + p.stderr
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or b"").decode() if isinstance(e.stdout, bytes) else (e.stdout or "")
        out += "\n[timeout]"
    dt = time.time() - t0
    try:
        ok, note = check(d, out)
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
    names = [n for n in T if not a.only or n in a.only.split(",")]
    print(f"model {cfg['model']} · agent {a.agent or 'code'} · {len(names)} tasks")
    with ThreadPoolExecutor(a.j) as ex:
        res = list(ex.map(lambda n: run_one(n, home, a.agent, a.timeout), names))
    for r in res:
        print(f"{'PASS' if r['ok'] else 'FAIL'} {r['task']:16} {r['sec']:4}s {r['steps']:3} steps {r['tok']:16} {r['note'][:90]}  {r['dir']}")
    print(f"{sum(r['ok'] for r in res)}/{len(res)} passed")


if __name__ == "__main__":
    main()
