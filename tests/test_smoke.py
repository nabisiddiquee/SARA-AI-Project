from my_python_project.cli import main


def test_cli_hello(capsys):
    assert main(["hello", "--name", "Test"]) == 0
    out = capsys.readouterr().out
    assert "Hello, Test!" in out
