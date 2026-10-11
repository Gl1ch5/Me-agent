"""Startup prompt + tool schemas in tokens (budget ~1000). Run: python3 tests/measure_tokens.py  (needs tiktoken)"""
import json, os, sys, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ["ME_HOME"] = tempfile.mkdtemp()
os.chdir(tempfile.mkdtemp())
import me, tiktoken  # noqa: E402

enc = tiktoken.get_encoding("o200k_base")
a = me.Agent(me.Store(), print)
sp, tools = len(enc.encode(a.system_prompt())), len(enc.encode(json.dumps(a.tool_schemas())))
print(f"system {sp} + tools {tools} = {sp + tools} tokens")
sys.exit(0 if sp + tools <= 1050 else 1)
