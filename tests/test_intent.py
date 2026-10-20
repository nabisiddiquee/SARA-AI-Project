import pytest

import my_python_project.actions as actions
import my_python_project.apps as apps
from my_python_project import intent


@pytest.fixture(scope="module")
def model():
    # Built-in examples only, so results don't depend on the apps on this PC.
    return intent.IntentModel.train(intent.training_examples(include_system=False))


@pytest.mark.parametrize("text", [
    "exit code 1 kya hai",
    "how do I quit smoking",
    "dukaan band hai kya",
    "python mein loop kaise likhte hai",
    "camera kaise kaam karta hai",
])
def test_questions_with_action_words_are_chat(model, text):
    assert model.predict(text).intent == intent.CHAT


@pytest.mark.parametrize("text, expected", [
    ("close chrome", "close_app"),
    ("notepad band kr do", "close_app"),
    ("zoom kholo", "open_app"),
    ("system band kr do bhai", "power"),
    ("whatsapp kholo", "whatsapp"),
    ("d drive me photos naam ka folder banao", "create_fs"),
])
def test_commands_are_recognised(model, text, expected):
    assert model.predict(text).intent == expected


def test_save_and_load_round_trip(model, tmp_path):
    path = tmp_path / "m.json"
    model.save(path)
    loaded = intent.IntentModel.load(path)
    assert loaded.predict("close chrome").intent == "close_app"


def test_user_examples_round_trip(tmp_path):
    path = tmp_path / "mine.json"
    intent.add_user_example("tv band karo", "close_app", path)
    intent.add_user_example("tv band karo", "close_app", path)  # no duplicates
    assert intent.load_user_examples(path) == [("tv band karo", "close_app")]


def test_question_never_kills_an_app(monkeypatch, model):
    # The bug this model fixes: "exit code 1 kya hai" used to run taskkill.
    monkeypatch.setattr(intent, "_model", model)
    monkeypatch.setattr(actions, "speak", lambda *a, **k: None)
    killed = []
    monkeypatch.setattr(apps.subprocess, "run", lambda cmd, **k: killed.append(cmd))
    monkeypatch.setattr("my_python_project.ai.ask_ai",
                        lambda text, lang="en": type("R", (), {"ok": True, "text": "AI"})())
    res = actions.dispatch_command("exit code 1 kya hai")
    assert killed == []
    assert res.message == "AI"
