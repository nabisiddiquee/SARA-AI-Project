"""Timed sleep / wake: parsing, command routing and timers.
The PC is never actually put to sleep here (sleep_now is replaced)."""

import sys
import time
from datetime import datetime, timedelta

import pytest

from my_python_project import power_timer as pt
from my_python_project.actions import dispatch_command

NOW = datetime(2026, 10, 2, 14, 30)       # 2:30 PM


@pytest.mark.parametrize("text,expect", [
    ("raat 11 baje system sleep kar dena", datetime(2026, 10, 2, 23, 0)),
    ("11:30 pm pe sleep", datetime(2026, 10, 2, 23, 30)),
    ("subah 6 baje system on kar dena", datetime(2026, 10, 3, 6, 0)),
    ("subah 6.15 baje jaga dena", datetime(2026, 10, 3, 6, 15)),
    ("shaam 7 baje sleep", datetime(2026, 10, 2, 19, 0)),
    ("raat 2 baje sleep", datetime(2026, 10, 3, 2, 0)),
    ("6 am wake up the system", datetime(2026, 10, 3, 6, 0)),
    ("3 baje sleep", datetime(2026, 10, 2, 15, 0)),        # no hint: next 3 o'clock (3 PM today)
    ("2 baje sleep", datetime(2026, 10, 3, 2, 0)),         # 2 PM has passed -> 2 AM tomorrow
    ("23:45 sleep", datetime(2026, 10, 2, 23, 45)),
])
def test_parse_clock(text, expect):
    assert pt.parse_clock(text, now=NOW) == expect


@pytest.mark.parametrize("text", ["30 minute baad sleep", "8 amazing apps", "sleep after 2 hours", "system sleep karo"])
def test_no_clock_time(text):
    assert pt.parse_clock(text, now=NOW) is None


@pytest.fixture
def no_real_sleep(monkeypatch):
    calls = []
    monkeypatch.setattr(pt, "sleep_now", lambda: calls.append(time.time()) or True)
    monkeypatch.setattr(pt, "_keep_awake", lambda seconds: None)
    yield calls
    pt.cancel_sleep()
    pt.cancel_wake()


def test_commands_schedule_and_cancel(no_real_sleep):
    r = dispatch_command("30 minute baad system sleep kar do", web_ui=True, reply_lang="hi")
    assert r.kind == "task" and "स्लीप" in r.message
    at = datetime.fromisoformat(pt.status()["sleep_at"])
    assert abs((at - datetime.now()).total_seconds() - 1800) < 5

    r = dispatch_command("raat 11 baje sleep aur subah 6 baje system on kar dena", web_ui=True, reply_lang="hi")
    st = pt.status()
    assert datetime.fromisoformat(st["sleep_at"]).hour == 23
    if sys.platform == "win32":
        assert datetime.fromisoformat(st["wake_at"]).hour == 6

    r = dispatch_command("sleep cancel karo", web_ui=True, reply_lang="hi")
    assert "रद्द" in r.message and pt.status()["sleep_at"] is None
    assert not no_real_sleep                    # nothing slept


def test_sleep_timer_fires(no_real_sleep, monkeypatch):
    monkeypatch.setattr(pt, "WARN_BEFORE_S", 0)
    said = []
    pt.schedule_sleep(datetime.now() + timedelta(seconds=1), lambda h, e: said.append(e))
    time.sleep(5)
    assert len(no_real_sleep) == 1 and any("sleep now" in s for s in said)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows waitable timer")
def test_wake_timer_fires(no_real_sleep):
    said = []
    assert pt.schedule_wake(datetime.now() + timedelta(seconds=2), lambda h, e: said.append(e))
    time.sleep(4)
    assert said and "awake" in said[0] and pt.status()["wake_at"] is None
