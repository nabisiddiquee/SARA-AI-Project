"""Real AI brain for Sara.

* Anthropic / Claude -> set ``ANTHROPIC_API_KEY`` (the main brain)
* OpenAI / ChatGPT   -> set ``OPENAI_API_KEY`` (optional alternative)
* Ollama (offline)   -> install Ollama and pull a model; no key, no internet

Keys are read from a project ``.env`` file (or the real environment). Claude is
used when its key is set; set ``AI_PROVIDER`` to ``anthropic``, ``openai`` or
``ollama`` to choose explicitly. Each SDK is imported lazily so the assistant
keeps working even when none is installed.

Ollama is the offline brain: it answers when no key is set, when
``AI_PROVIDER=ollama``, and as a fallback whenever the online provider fails
(no internet, invalid key, server busy).
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

# Default models for each provider.
ANTHROPIC_MODEL = "claude-sonnet-5"
OPENAI_MODEL = "gpt-4o-mini"
OLLAMA_MODEL = "gemma3:4b"
OLLAMA_HOST = "http://127.0.0.1:11434"

# Project root = .../src/my_python_project/ai.py -> up 3 levels.
_PROJECT_ROOT = Path(__file__).resolve().parents[2]

_SYSTEM_PROMPT = (
    "You are Sara, a friendly and highly capable voice AI assistant running on "
    "the user's computer. Your replies are read aloud by a text-to-speech engine "
    "and shown in a small terminal panel, so keep them short, natural, and "
    "conversational — usually one to three sentences. Use plain text only: no "
    "markdown, bullet points, code fences, or emoji. If the user writes in Hindi "
    "(or Hinglish), reply in Hindi; otherwise reply in English. If you are unsure "
    "or cannot do something, say so briefly and suggest what you can do."
)


@dataclass(frozen=True)
class AIResponse:
    ok: bool
    text: str


def _load_dotenv() -> None:
    """Load KEY=VALUE pairs from a project ``.env`` file into the environment.

    Looks for ``.env`` in the current working directory first, then the project
    root. Real environment variables always win, so an exported key overrides
    the file. This is a tiny parser so we don't need an extra dependency.
    """
    for candidate in (Path.cwd() / ".env", _PROJECT_ROOT / ".env"):
        if not candidate.is_file():
            continue
        try:
            for raw in candidate.read_text(encoding="utf-8").splitlines():
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = value
        except OSError:
            continue


def _empty_reply(lang: str) -> str:
    return (
        "माफ़ कीजिए, मैं इसका जवाब नहीं बना पाई।" if lang == "hi"
        else "Sorry, I couldn't come up with an answer for that."
    )


def _busy_reply(lang: str) -> str:
    """Friendly message when the provider is rate-limited / out of credit.

    We never show the raw 429 JSON to the user — it looks like a crash.
    """
    return (
        "अभी मैं थोड़ी व्यस्त हूँ — एआई की लिमिट पूरी हो गई है। "
        "कुछ देर बाद फिर पूछिए।"
        if lang == "hi" else
        "I'm a bit overloaded right now — the AI usage limit has been reached. "
        "Please try again in a little while."
    )


def _overloaded_reply(lang: str) -> str:
    """Friendly message when the AI service is temporarily down (5xx errors)."""
    return (
        "एआई सर्वर अभी बहुत व्यस्त है। कृपया थोड़ी देर बाद फिर पूछिए।"
        if lang == "hi" else
        "The AI service is very busy right now. Please try again in a moment."
    )


def _bad_key_reply(lang: str) -> str:
    return (
        "Claude की API key सही नहीं है। console.anthropic.com से नई key बनाकर "
        ".env में ANTHROPIC_API_KEY में डालें।"
        if lang == "hi" else
        "The Claude API key is invalid. Create a new key at console.anthropic.com "
        "and put it in .env as ANTHROPIC_API_KEY."
    )


def _missing_key_reply(lang: str, provider: str = "anthropic") -> str:
    """Friendly message when the selected provider has no API key at all."""
    if provider == "openai":
        var, site, name = "OPENAI_API_KEY", "platform.openai.com", "ChatGPT"
    else:
        var, site, name = "ANTHROPIC_API_KEY", "console.anthropic.com", "Claude"
    return (
        f"{name} से जुड़ने के लिए API key नहीं मिली। कृपया {site} से key बनाकर "
        f"प्रोजेक्ट की .env फ़ाइल में {var}=... जोड़ें, फिर सर्वर दोबारा चालू करें।"
        if lang == "hi" else
        f"I can't connect to {name} yet because no API key is configured. "
        f"Please create a key at {site}, add it to the project's .env file as "
        f"{var}=..., and restart the server."
    )


def _network_reply(lang: str) -> str:
    return (
        "एआई सेवा से कनेक्ट नहीं हो पाया। कृपया अपना इंटरनेट कनेक्शन जाँचें और फिर कोशिश करें।"
        if lang == "hi" else
        "I couldn't reach the AI service. Please check your internet connection "
        "and try again."
    )


def _generic_reply(lang: str) -> str:
    return (
        "माफ़ कीजिए, अभी आपका अनुरोध पूरा नहीं हो सका। कृपया थोड़ी देर बाद फिर कोशिश करें।"
        if lang == "hi" else
        "Sorry, I couldn't complete that request right now. Please try again shortly."
    )


def _offline_failed_reply(lang: str) -> str:
    return (
        "ऑफ़लाइन एआई (Ollama) जवाब नहीं दे पाया। कृपया जाँचें कि Ollama चालू है "
        f"और मॉडल {os.environ.get('OLLAMA_MODEL', OLLAMA_MODEL)} डाउनलोड है।"
        if lang == "hi" else
        "The offline AI (Ollama) didn't respond. Please make sure Ollama is running "
        f"and the model {os.environ.get('OLLAMA_MODEL', OLLAMA_MODEL)} is pulled."
    )


def _offline_memory_reply(lang: str) -> str:
    return (
        "ऑफ़लाइन एआई मॉडल लोड करने के लिए कंप्यूटर में पर्याप्त मेमोरी नहीं है। कृपया कुछ "
        "ऐप्स बंद करें, या .env में OLLAMA_MODEL=gemma3:1b जैसा छोटा मॉडल चुनें।"
        if lang == "hi" else
        "There isn't enough free memory to load the offline AI model. Please close "
        "some apps, or choose a smaller model in .env, e.g. OLLAMA_MODEL=gemma3:1b."
    )


def _no_credit_reply(lang: str) -> str:
    return (
        "एआई अकाउंट में क्रेडिट बैलेंस खत्म हो गया है। कृपया console.anthropic.com पर "
        "Plans & Billing में जाकर क्रेडिट जोड़ें।"
        if lang == "hi" else
        "The AI account has run out of credits. Please add credits under Plans & "
        "Billing at console.anthropic.com, then try again."
    )


def _log_error(source: str, err: object) -> None:
    """Keep the technical details in the server console, not in the reply."""
    print(f"[ai] {source} error: {err}", flush=True)


def _is_missing_key(err: Exception) -> bool:
    blob = str(err).lower()
    return any(s in blob for s in (
        "could not resolve authentication", "api_key client option must be set",
        "no api key",
    ))


def _is_network_error(err: Exception) -> bool:
    name = type(err).__name__.lower()
    blob = str(err).lower()
    return "connection" in name or "timeout" in name or any(s in blob for s in (
        "connection error", "timed out", "getaddrinfo", "name resolution",
    ))


def _is_overloaded(err: Exception) -> bool:
    """True for temporary server-side errors (overloaded 529, 500/502/503/504)."""
    status = getattr(err, "status_code", None) or getattr(err, "code", None)
    if status in (500, 502, 503, 504, 529):
        return True
    blob = str(err).lower()
    return any(s in blob for s in (
        "503", "unavailable", "overloaded", "high demand", "internal server error",
    ))


def _is_rate_limit(err: Exception) -> bool:
    """True if the exception looks like an HTTP 429 / quota-exhausted error."""
    status = getattr(err, "status_code", None) or getattr(err, "code", None)
    if status == 429:
        return True
    blob = str(err).lower()
    return "429" in blob or "rate_limit" in blob or "quota" in blob


def _is_auth_error(err: Exception) -> bool:
    status = getattr(err, "status_code", None)
    return status in (401, 403) or "authentication_error" in str(err).lower()


def _error_reply(err: Exception, lang: str, provider: str = "anthropic") -> AIResponse:
    _log_error(provider, err)
    if _is_missing_key(err):
        return AIResponse(ok=False, text=_missing_key_reply(lang, provider))
    if "credit balance" in str(err).lower():
        return AIResponse(ok=False, text=_no_credit_reply(lang))
    if _is_rate_limit(err):
        return AIResponse(ok=False, text=_busy_reply(lang))
    if _is_overloaded(err):
        return AIResponse(ok=False, text=_overloaded_reply(lang))
    if _is_auth_error(err):
        return AIResponse(ok=False, text=_bad_key_reply(lang))
    if _is_network_error(err):
        return AIResponse(ok=False, text=_network_reply(lang))
    return AIResponse(ok=False, text=_generic_reply(lang))


def _ask_anthropic(text: str, lang: str, system: str, max_tokens: int) -> AIResponse:
    try:
        import anthropic  # type: ignore
    except Exception as e:  # noqa: BLE001
        msg = (
            "Claude सपोर्ट इंस्टॉल नहीं है।" if lang == "hi" else
            "Claude support is not installed. Enable it with:\n"
            "  python -m pip install -e \".[ai]\"\n"
            f"Import error: {e}"
        )
        return AIResponse(ok=False, text=msg)

    try:
        # The SDK retries 429/529 "overloaded" errors itself with backoff.
        client = anthropic.Anthropic(max_retries=2, timeout=60)
        response = client.messages.create(
            model=os.environ.get("ANTHROPIC_MODEL", ANTHROPIC_MODEL),
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": text}],
        )
        reply = "".join(
            block.text for block in response.content if block.type == "text"
        ).strip()
        return AIResponse(ok=True, text=reply or _empty_reply(lang))
    except Exception as e:  # noqa: BLE001
        return _error_reply(e, lang)


def _ask_openai(text: str, lang: str, system: str, max_tokens: int) -> AIResponse:
    try:
        from openai import OpenAI  # type: ignore
    except Exception as e:  # noqa: BLE001
        msg = (
            "ChatGPT सपोर्ट इंस्टॉल नहीं है।" if lang == "hi" else
            "ChatGPT support is not installed. Enable it with:\n"
            "  python -m pip install -e \".[chatgpt]\"\n"
            f"Import error: {e}"
        )
        return AIResponse(ok=False, text=msg)

    try:
        client = OpenAI()
        response = client.chat.completions.create(
            model=os.environ.get("OPENAI_MODEL", OPENAI_MODEL),
            max_tokens=max_tokens,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": text},
            ],
        )
        reply = (response.choices[0].message.content or "").strip()
        return AIResponse(ok=True, text=reply or _empty_reply(lang))
    except Exception as e:  # noqa: BLE001
        return _error_reply(e, lang, "openai")


def _ollama_host() -> str:
    return os.environ.get("OLLAMA_HOST", OLLAMA_HOST).rstrip("/")


def _ollama_available() -> bool:
    """True if a local Ollama server is running (checked quickly)."""
    try:
        with urllib.request.urlopen(f"{_ollama_host()}/api/tags", timeout=2) as r:
            return r.status == 200
    except (OSError, ValueError):
        return False


def _ask_ollama(text: str, lang: str, system: str, max_tokens: int) -> AIResponse:
    body = json.dumps({
        "model": os.environ.get("OLLAMA_MODEL", OLLAMA_MODEL),
        "stream": False,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": text},
        ],
        "options": {"num_predict": max_tokens},
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{_ollama_host()}/api/chat", data=body,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            data = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "ignore")
        _log_error("ollama", detail)
        if "allocate" in detail.lower() or "out of memory" in detail.lower():
            return AIResponse(ok=False, text=_offline_memory_reply(lang))
        return AIResponse(ok=False, text=_offline_failed_reply(lang))
    except (OSError, ValueError) as e:
        _log_error("ollama", e)
        return AIResponse(ok=False, text=_offline_failed_reply(lang))
    reply = (data.get("message", {}).get("content") or "").strip()
    return AIResponse(ok=True, text=reply or _empty_reply(lang))


def ask_ai(text: str, *, lang: str = "en", system: str | None = None,
           max_tokens: int = 512) -> AIResponse:
    """Ask the configured AI provider and return its reply.

    Claude (or ChatGPT) when a key is set, with Ollama as the offline brain and
    fallback. ``system`` overrides Sara's default voice-assistant instructions
    (used e.g. to draft emails). Never raises — returns
    ``AIResponse(ok=False, ...)`` with a helpful message when nothing is
    configured or the request fails.
    """
    _load_dotenv()
    system = system or _SYSTEM_PROMPT

    provider = (os.environ.get("AI_PROVIDER") or "").strip().lower()
    if provider == "claude":
        provider = "anthropic"
    if provider not in ("anthropic", "openai", "ollama"):
        if os.environ.get("ANTHROPIC_API_KEY"):
            provider = "anthropic"
        elif os.environ.get("OPENAI_API_KEY"):
            provider = "openai"
        else:
            provider = ""

    if provider == "ollama" or (not provider and _ollama_available()):
        return _ask_ollama(text, lang, system, max_tokens)

    online = {"anthropic": _ask_anthropic, "openai": _ask_openai}
    key_var = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY"}
    if provider in online:
        # Provider chosen but its key is missing: use offline AI or explain clearly.
        if not os.environ.get(key_var[provider], "").strip():
            if _ollama_available():
                offline = _ask_ollama(text, lang, system, max_tokens)
                if offline.ok:
                    return offline
            return AIResponse(ok=False, text=_missing_key_reply(lang, provider))
        res = online[provider](text, lang, system, max_tokens)
        # Online brain down (no internet, bad key, busy)? Answer offline instead.
        if not res.ok and _ollama_available():
            offline = _ask_ollama(text, lang, system, max_tokens)
            if offline.ok:
                return offline
        return res

    msg = (
        "एआई कॉन्फ़िगर नहीं है। .env फ़ाइल में ANTHROPIC_API_KEY डालें, "
        "या ऑफ़लाइन एआई के लिए Ollama चालू करें।"
        if lang == "hi" else
        "AI is not configured. Add your Claude key to the .env file in the project "
        "folder:\n"
        "  ANTHROPIC_API_KEY=sk-ant-...\n"
        "or start Ollama for the offline brain."
    )
    return AIResponse(ok=False, text=msg)
