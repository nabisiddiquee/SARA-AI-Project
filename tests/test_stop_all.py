from my_python_project import games, mailer, system
from my_python_project.actions import dispatch_command
from my_python_project.stop_all import is_stop_all


def test_bare_stop_commands_match():
    for text in ("Sara kam stop kro", "band karo", "rokdo", "rok do", "Sab cancel kr de",
                 "stop", "ruk jao", "चुप हो जाओ", "सब बंद करो", "sara kaam band kar do!"):
        assert is_stop_all(text), text


def test_targeted_stop_commands_do_not_match():
    for text in ("chrome band karo", "stop music", "shutdown cancel karo",
                 "radio band karo", "camera band karo", "kam karo", "sara"):
        assert not is_stop_all(text), text


def test_stop_all_ends_games_and_pending_confirmations():
    games.start_number_game()
    system._pending = ("shutdown", 0)
    mailer._pending = object()
    r = dispatch_command("sab band karo", web_ui=True)
    assert r.kind == "stop_all"
    assert not games.is_game_active()
    assert not system.power_pending()
    assert not mailer.email_pending()
