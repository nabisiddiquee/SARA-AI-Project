import io
import os
import time
import zipfile

from my_python_project import captures_mail, mailer


def _setup(tmp_path, monkeypatch, result):
    day = tmp_path / "2026-10-01"
    day.mkdir()
    old = time.time() - 3600
    for name in ("a.jpg", "b.jpg"):
        (day / name).write_bytes(b"x" * 10)
        os.utime(day / name, (old, old))
    monkeypatch.setattr(captures_mail, "CAPTURES", tmp_path)
    monkeypatch.setenv("SMTP_USER", "me@gmail.com")
    sent = []

    def fake_send(draft, attachments=()):
        sent.append((draft, attachments))
        return result

    monkeypatch.setattr(mailer, "send", fake_send)
    return day, sent


def test_zips_emails_then_deletes(tmp_path, monkeypatch):
    day, sent = _setup(tmp_path, monkeypatch, (True, ""))
    ok, _ = captures_mail.send_now()
    assert ok and len(sent) == 1
    draft, ((name, data),) = sent[0]
    assert draft.to == "me@gmail.com" and name.endswith(".zip")
    assert sorted(zipfile.ZipFile(io.BytesIO(data)).namelist()) == ["2026-10-01/a.jpg", "2026-10-01/b.jpg"]
    assert not day.exists() and tmp_path.exists()


def test_failed_email_deletes_nothing(tmp_path, monkeypatch):
    day, _ = _setup(tmp_path, monkeypatch, (False, "login failed"))
    ok, _ = captures_mail.send_now()
    assert not ok and len(list(day.iterdir())) == 2
