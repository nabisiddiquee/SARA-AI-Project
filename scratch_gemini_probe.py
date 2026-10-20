"""Probe which Gemini models work with the key in .env."""
import os
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))
from my_python_project.ai import _load_dotenv  # noqa: E402

_load_dotenv()
key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
print("Key present:", bool(key), "\n")

from google import genai  # noqa: E402
from google.genai import types  # noqa: E402

client = genai.Client(api_key=key)

# 1) List models the key can actually access.
print("=== Models available to this key (generateContent) ===")
try:
    for m in client.models.list():
        actions = getattr(m, "supported_actions", None) or []
        if "generateContent" in actions:
            print(" -", m.name)
except Exception as e:
    print("list failed:", e)

# 2) Try a quick call against common free-tier models.
print("\n=== Live call test per model ===")
for model in [
    "gemini-2.0-flash",
    "gemini-2.5-flash",
    "gemini-2.0-flash-lite",
    "gemini-1.5-flash",
    "gemini-flash-latest",
]:
    try:
        r = client.models.generate_content(
            model=model,
            contents="Say OK",
            config=types.GenerateContentConfig(max_output_tokens=5),
        )
        print(f"  {model}: OK -> {(r.text or '').strip()!r}")
    except Exception as e:
        msg = str(e).replace("\n", " ")
        print(f"  {model}: FAIL -> {msg[:120]}")
