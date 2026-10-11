#!/usr/bin/env python3
"""Build the single-file agent: src/NN_*.py are concatenated in order into me.py, src/web/ (index.html + *.css + *.js) is embedded as WEBUI.
Edit files in src/, then run `python3 build.py`. `python3 build.py --check` fails if me.py is stale (used in CI)."""
import ast, json, pathlib, sys

ROOT = pathlib.Path(__file__).resolve().parent
SRC = ROOT / "src"
OUT = ROOT / "me.py"
PLACEHOLDER = 'WEBUI = "<!-- built into me.py by build.py -->"'


def build():
    parts = [p.read_text("utf-8").rstrip("\n") + "\n" for p in sorted(SRC.glob("[0-9][0-9]*_*.py"))]
    code = "\n\n".join(parts)
    web = SRC / "web"
    html = (web / "index.html").read_text("utf-8")
    html = html.replace("/*CSS*/", "\n".join(p.read_text("utf-8") for p in sorted(web.glob("*.css"))))
    html = html.replace("/*JS*/", "\n".join(p.read_text("utf-8") for p in sorted(web.glob("*.js"))))
    assert PLACEHOLDER in code, "WEBUI placeholder missing in src"
    code = code.replace(PLACEHOLDER, "WEBUI = " + json.dumps(html))
    ast.parse(code)
    return code


if __name__ == "__main__":
    code = build()
    if "--check" in sys.argv:
        if OUT.read_text("utf-8") != code:
            sys.exit("me.py is out of date — run python3 build.py")
        print("me.py up to date")
    else:
        OUT.write_text(code, "utf-8")
        OUT.chmod(0o755)
        print(f"built me.py · {code.count(chr(10))} lines · {len(code) // 1024} KB")
