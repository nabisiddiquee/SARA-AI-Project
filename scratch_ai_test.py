r"""Quick live test of Sara's AI brain. Reads the key from .env automatically.

Usage (PowerShell), from the project folder:
    .\.venv\Scripts\python.exe scratch_ai_test.py
"""
import os
import sys

# Windows consoles default to cp1252 and can't print Devanagari — force UTF-8.
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

from my_python_project.ai import ask_ai  # noqa: E402

print("Q: What is the capital of France? (in one short sentence)")
r1 = ask_ai("What is the capital of France? Answer in one short sentence.")
print(f"ok={r1.ok}\nA: {r1.text}\n")

print("Q (Hindi): aaj ka mausam ke bare mein ek line bolo")
r2 = ask_ai("aaj ka mausam ke bare mein ek line bolo", lang="hi")
print(f"ok={r2.ok}\nA: {r2.text}")
