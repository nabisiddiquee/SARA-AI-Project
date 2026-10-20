"""Skills catalog: structure, the "help" phrases, and that examples are real
triggers (checked with the detectors only; nothing is executed)."""

import pytest

from my_python_project import games, media, power_timer, radio, skills, system, tools_voice, web


def all_examples():
    return [e for c in skills.CATEGORIES for s in c["skills"] for e in s["examples"]]


def test_catalog_shape():
    cat = skills.catalog()
    assert cat["count"] >= 30 and cat["examples"] >= 60
    ids = [c["id"] for c in skills.CATEGORIES]
    assert len(ids) == len(set(ids))
    for e in all_examples():
        assert e["text"].strip() and not (e["run"] and e["edit"])


def test_risky_examples_never_run_on_click():
    risky = ("sleep", "shutdown", "restart", "lock", "whatsapp", "email", "folder", "band karo", "kholo", "send")
    for e in all_examples():
        t = e["text"].lower()
        if any(w in t for w in ("sleep", "shutdown", "restart", "lock", "send karna", "message bhejo", "email bhejo", "folder banao")):
            assert not e["run"], e["text"]
    assert risky


@pytest.mark.parametrize("text,expected", [
    ("tum kya kya kar sakti ho", True), ("what can you do", True), ("skills dikhao", True),
    ("sara help", True), ("help", True), ("git commands batao", False), ("help me write an email", False),
])
def test_help_phrases(text, expected):
    assert skills.is_help_request(text) is expected


@pytest.mark.parametrize("text,check", [
    ("surah yaseen sunao", lambda t: media.find_song(t)["surah"] == 36),
    ("quran sunao", lambda t: media.wants_menu(t) == "quran"),
    ("90s songs chalao", lambda t: media.find_song(t) is not None),
    ("radio mirchi lagao", lambda t: radio.parse_command(t)["action"] == "play"),
    ("map of India Gate", lambda t: web.parse(t)["action"] == "web"),
    ("youtube par arijit singh chalao", lambda t: web.parse(t)["action"] == "youtube_play"),
    ("tic tac toe", games.is_ttt_start),
    ("toss karo", games.is_coin),
    ("system lock karo", system.is_lock),
    ("wake cancel karo", lambda t: power_timer.is_timer_cancel(t) == "wake"),
    ("subah 6 baje system on kar dena", power_timer.is_wake_request),
])
def test_examples_are_real_triggers(text, check):
    assert text in [e["text"] for e in all_examples()]
    assert check(text)


def test_tool_examples():
    assert tools_voice.handle("calculator kholo", hi=True)["kind"] == "calc_open"
    assert "17" in tools_voice.handle("12 plus 5 kitna hai", hi=True)["message"]
