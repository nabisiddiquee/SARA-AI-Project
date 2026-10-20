import my_python_project.system as system


def teardown_function():
    system._pending = None


def test_sleep_detection():
    assert system.is_sleep("system ko sleep mode karo")
    assert system.is_sleep("computer sleep karo")
    assert system.is_sleep("laptop so jao")
    # No system noun -> an ordinary sentence must not trigger sleep.
    assert not system.is_sleep("how do I sleep better at night")


def test_lock_detection():
    assert system.is_lock("system lock karo")
    assert system.is_lock("lock screen")
    assert not system.is_lock("unlock my phone story")


def test_restart_and_shutdown_detection():
    assert system.is_restart("system restart karo")
    assert system.is_restart("reboot the pc")

    assert system.is_shutdown("system off karo")
    assert system.is_shutdown("shutdown")
    assert system.is_shutdown("computer band karo")
    # "camera off" has no system noun and no shutdown word.
    assert not system.is_shutdown("camera off karo")


def test_shutdown_requires_confirmation(monkeypatch):
    ran = {"seconds": None}
    monkeypatch.setattr(system, "do_shutdown", lambda s=0: ran.__setitem__("seconds", s) or True)

    assert not system.power_pending()
    prompt = system.request_power("shutdown", lang="en")
    assert "sure" in prompt.lower()
    assert system.power_pending()  # armed, but nothing has happened yet
    assert ran["seconds"] is None

    ok, msg = system.confirm_power("yes", lang="en")
    assert ok and "shutting down" in msg.lower()
    assert ran["seconds"] == 0              # only now does it fire (immediately)
    assert not system.power_pending()


def test_shutdown_cancelled_on_no(monkeypatch):
    ran = {"seconds": None}
    monkeypatch.setattr(system, "do_shutdown", lambda s=0: ran.__setitem__("seconds", s) or True)

    system.request_power("shutdown", lang="en")
    ok, msg = system.confirm_power("no", lang="en")
    assert ok and "cancel" in msg.lower()
    assert ran["seconds"] is None
    assert not system.power_pending()


def test_shutdown_cancelled_on_unrelated_reply(monkeypatch):
    ran = {"seconds": None}
    monkeypatch.setattr(system, "do_shutdown", lambda s=0: ran.__setitem__("seconds", s) or True)

    system.request_power("shutdown", lang="en")
    # Anything that isn't a clear "yes" cancels, for safety.
    ok, msg = system.confirm_power("what time is it", lang="en")
    assert ran["seconds"] is None
    assert not system.power_pending()


def test_restart_confirmation_runs_restart(monkeypatch):
    ran = {"seconds": None}
    monkeypatch.setattr(system, "do_restart", lambda s=0: ran.__setitem__("seconds", s) or True)

    system.request_power("restart", lang="en")
    ok, msg = system.confirm_power("haan", lang="en")
    assert ok and "restart" in msg.lower()
    assert ran["seconds"] == 0


def test_parse_delay_seconds():
    # No duration -> 0 (immediate), preserving the original behaviour.
    assert system.parse_delay_seconds("shutdown") == 0
    assert system.parse_delay_seconds("system off karo") == 0

    # Hours, minutes, seconds, and decimals.
    assert system.parse_delay_seconds("shutdown after 2 hours") == 2 * 3600
    assert system.parse_delay_seconds("shutdown in 1.5 hr") == 5400
    assert system.parse_delay_seconds("shutdown 90 min") == 90 * 60
    assert system.parse_delay_seconds("shutdown in 30 seconds") == 30

    # Glued number+unit and fractions.
    assert system.parse_delay_seconds("shutdown 2hr") == 2 * 3600
    assert system.parse_delay_seconds("shutdown in 1/2 hour") == 1800

    # "half an hour" and "1 hour and a half".
    assert system.parse_delay_seconds("shutdown half an hour") == 1800
    assert system.parse_delay_seconds("shutdown 1 hour and a half") == 5400

    # Hindi/Hinglish number words.
    assert system.parse_delay_seconds("aadha ghanta baad shutdown") == 1800
    assert system.parse_delay_seconds("dedh ghante baad system band karo") == 5400
    assert system.parse_delay_seconds("do ghante 30 minute baad shutdown") == 2 * 3600 + 30 * 60

    # Compound and capped.
    assert system.parse_delay_seconds("shutdown in 1 hour 30 minutes") == 5400
    assert system.parse_delay_seconds("shutdown after 100 hours") == system._MAX_DELAY


def test_timed_shutdown_schedules_with_delay(monkeypatch):
    ran = {"seconds": None}
    monkeypatch.setattr(system, "do_shutdown", lambda s=0: ran.__setitem__("seconds", s) or True)

    prompt = system.request_power("shutdown", delay=7200, lang="en")
    assert "2 hours" in prompt.lower()
    assert system.power_pending()

    ok, msg = system.confirm_power("yes", lang="en")
    assert ok
    assert ran["seconds"] == 7200
    assert "2 hours" in msg.lower()
    assert "cancel" in msg.lower()          # tells the user how to call it off
    assert not system.power_pending()


def test_power_cancel_detection():
    assert system.is_power_cancel("cancel shutdown")
    assert system.is_power_cancel("shutdown cancel karo")
    assert system.is_power_cancel("restart roko")
    assert system.is_power_cancel("computer band mat karo")
    # A plain shutdown request is not a cancellation.
    assert not system.is_power_cancel("shutdown")
    assert not system.is_power_cancel("system off karo")
    # "stop the music" has no power word.
    assert not system.is_power_cancel("stop the music")


def test_cancel_scheduled_runs_abort(monkeypatch):
    ran = {"abort": False}
    monkeypatch.setattr(system, "do_abort", lambda: ran.__setitem__("abort", True) or True)

    system.request_power("shutdown", delay=3600, lang="en")
    ok, msg = system.cancel_scheduled(lang="en")
    assert ok and "cancel" in msg.lower()
    assert ran["abort"] is True
    assert not system.power_pending()       # also clears any pending confirm
