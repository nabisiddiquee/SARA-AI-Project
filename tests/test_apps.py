import my_python_project.apps as apps


def test_find_app_matches_known_names():
    assert apps.find_app("postman open karo")["label"] == "Postman"
    assert apps.find_app("open chrome")["label"] == "Google Chrome"
    assert apps.find_app("vlc chalu karo")["label"] == "VLC media player"
    assert apps.find_app("open vs code")["label"] == "Visual Studio Code"


def test_find_app_unknown_returns_none():
    assert apps.find_app("open something imaginary") is None
    assert apps.find_app("what's the weather") is None


def test_launch_uses_resolved_path(monkeypatch):
    calls = {}

    def fake_popen(cmd, *a, **k):
        calls["cmd"] = cmd
        calls["shell"] = k.get("shell", False)
        return object()

    monkeypatch.setattr(apps.subprocess, "Popen", fake_popen)
    # Pretend Postman's exe exists so the path branch is used.
    monkeypatch.setattr(apps.os.path, "exists", lambda p: True)

    app = apps.find_app("open postman")
    assert apps.launch_app(app) is True
    assert isinstance(calls["cmd"], list)  # launched by absolute path, not shell
    assert calls["shell"] is False


def test_launch_falls_back_to_shell(monkeypatch):
    calls = {}
    monkeypatch.setattr(apps.subprocess, "Popen",
                        lambda cmd, *a, **k: calls.update(cmd=cmd, shell=k.get("shell")))
    monkeypatch.setattr(apps, "_resolve_candidate", lambda app: None)

    app = apps.find_app("open calculator")
    assert apps.launch_app(app) is True
    assert calls["shell"] is True
    assert "calc" in calls["cmd"]


def test_launch_returns_false_on_error(monkeypatch):
    def boom(*a, **k):
        raise OSError("nope")

    monkeypatch.setattr(apps.subprocess, "Popen", boom)
    app = apps.find_app("open postman")
    assert apps.launch_app(app) is False


# --- generic Start Menu launcher ---

_FAKE_SHORTCUTS = [
    (r"C:\sm\Postman.lnk", "Postman"),
    (r"C:\sm\MongoDB Compass.lnk", "MongoDB Compass"),
    (r"C:\sm\Requestly API Client.lnk", "Requestly API Client"),
    (r"C:\sm\Cursor.lnk", "Cursor"),
]


def _fake_iter():
    return iter(_FAKE_SHORTCUTS)


def test_extract_app_name_strips_verbs_and_fillers():
    assert apps.extract_app_name("postman open karo please", apps.APP_OPEN_VERBS) == "postman"
    assert apps.extract_app_name("open mongodb compass app", apps.APP_OPEN_VERBS) == "mongodb compass"
    assert apps.extract_app_name("requestly kholo", apps.APP_OPEN_VERBS) == "requestly"


def test_resolve_shortcut_matches(monkeypatch):
    monkeypatch.setattr(apps, "_iter_shortcuts", _fake_iter)

    assert apps.resolve_shortcut("postman")[1] == "Postman"
    assert apps.resolve_shortcut("mongodb compass")[1] == "MongoDB Compass"
    # partial/prefix words still resolve
    assert apps.resolve_shortcut("requestly")[1] == "Requestly API Client"
    assert apps.resolve_shortcut("cursor")[1] == "Cursor"


def test_resolve_shortcut_rejects_weak_match(monkeypatch):
    monkeypatch.setattr(apps, "_iter_shortcuts", _fake_iter)
    # No shortcut is a strong match for a random phrase.
    assert apps.resolve_shortcut("explaining recursion") is None
    assert apps.resolve_shortcut("") is None


def test_open_by_name(monkeypatch):
    monkeypatch.setattr(apps, "_iter_shortcuts", _fake_iter)
    opened = {}
    monkeypatch.setattr(apps.os, "startfile", lambda p: opened.update(path=p), raising=False)

    ok, label = apps.open_by_name("postman")
    assert ok and label == "Postman"
    assert opened["path"].endswith("Postman.lnk")

    ok, label = apps.open_by_name("nonexistent xyz app")
    assert not ok and label == ""


# --- closing apps ---

class _Result:
    def __init__(self, rc):
        self.returncode = rc


def test_close_by_name_kills_registry_exe(monkeypatch):
    calls = []
    monkeypatch.setattr(apps.subprocess, "run",
                        lambda cmd, **k: calls.append(cmd) or _Result(0))

    ok, label = apps.close_by_name("postman")
    assert ok and label == "Postman"
    # taskkill is case-insensitive; the image name is lowercased internally.
    assert calls[0] == ["taskkill", "/IM", "postman.exe", "/F"]


def test_close_by_name_reports_when_not_running(monkeypatch):
    # taskkill returns non-zero when the process isn't found.
    monkeypatch.setattr(apps.subprocess, "run", lambda cmd, **k: _Result(128))
    ok, label = apps.close_by_name("postman")
    assert not ok and label == "Postman"


def test_close_never_kills_explorer(monkeypatch):
    calls = []
    monkeypatch.setattr(apps.subprocess, "run",
                        lambda cmd, **k: calls.append(cmd) or _Result(0))
    # "explorer" -> explorer.exe is blocklisted, so taskkill is never invoked.
    ok, _label = apps.close_by_name("explorer")
    assert not ok
    assert calls == []
