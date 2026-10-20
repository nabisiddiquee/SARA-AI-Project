import my_python_project.ai as ai_module
from my_python_project.ai import AIResponse, ask_ai


def _force_no_key(monkeypatch):
    # Remove any real key and stop .env from re-supplying one, so these tests
    # never make a live API call regardless of the local environment.
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("AI_PROVIDER", raising=False)
    monkeypatch.setattr(ai_module, "_load_dotenv", lambda: None)
    monkeypatch.setattr(ai_module, "_ollama_available", lambda: False)


def test_ask_ai_without_key_is_not_ok(monkeypatch):
    # With no API key configured, ask_ai must fail gracefully (never raise)
    # and return a helpful message rather than attempting a request.
    _force_no_key(monkeypatch)
    res = ask_ai("what's the weather like?")
    assert isinstance(res, AIResponse)
    assert res.ok is False
    assert res.text  # non-empty guidance message


def test_ask_ai_hindi_message(monkeypatch):
    _force_no_key(monkeypatch)
    res = ask_ai("aaj mausam kaisa hai", lang="hi")
    assert res.ok is False
    assert res.text


def test_claude_is_used_when_key_set(monkeypatch):
    _force_no_key(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    seen = {}

    def fake(text, lang, system, max_tokens):
        seen.update(text=text, system=system)
        return AIResponse(True, "Paris.")

    monkeypatch.setattr(ai_module, "_ask_anthropic", fake)
    assert ask_ai("capital of france", system="Be brief.").text == "Paris."
    assert seen == {"text": "capital of france", "system": "Be brief."}


def test_claude_failure_falls_back_to_ollama(monkeypatch):
    _force_no_key(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setattr(ai_module, "_ask_anthropic",
                        lambda *a: AIResponse(False, "invalid key"))
    monkeypatch.setattr(ai_module, "_ollama_available", lambda: True)
    monkeypatch.setattr(ai_module, "_ask_ollama", lambda *a: AIResponse(True, "Paris."))
    assert ask_ai("capital of france") == AIResponse(True, "Paris.")


def test_no_key_uses_ollama_when_running(monkeypatch):
    _force_no_key(monkeypatch)
    monkeypatch.setattr(ai_module, "_ollama_available", lambda: True)
    monkeypatch.setattr(ai_module, "_ask_ollama", lambda *a: AIResponse(True, "hi"))
    assert ask_ai("hello").text == "hi"


def test_errors_become_friendly_messages():
    class Err(Exception):
        def __init__(self, status):
            super().__init__(f"Error code: {status}")
            self.status_code = status

    assert "busy" in ai_module._error_reply(Err(529), "en").text
    assert "invalid" in ai_module._error_reply(Err(401), "en").text
    assert "limit" in ai_module._error_reply(Err(429), "en").text
