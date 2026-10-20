import json
from datetime import datetime, timezone

import pytest

from my_python_project import ac
from my_python_project.actions import _ac_command


def test_sigv4_matches_botocore():
    # Reference signature produced by botocore's SigV4Auth for the same request.
    body = json.dumps({"state": {"desired": {"pow": 0}}}).encode()
    h = ac.sigv4_headers(
        "POST", "abc123-ats.iot.ap-south-1.amazonaws.com", "/things/BSAC01/shadow", body,
        "AKIDEXAMPLE", "secretEXAMPLE", now=datetime(2026, 10, 13, 9, 30, tzinfo=timezone.utc),
    )
    assert h["x-amz-date"] == "20261013T093000Z"
    assert h["Authorization"].endswith(
        "Signature=6f66528d12811b433f821fad667f810673156c856c924d52ee87f4cdbf450fdf")


@pytest.mark.parametrize("text, expected", [
    ("ac on karo", {"action": "power", "on": True}),
    ("AC chalao", {"action": "power", "on": True}),
    ("ac band karo", {"action": "power", "on": False}),
    ("एसी बंद करो", {"action": "power", "on": False}),
    ("ac 24 pe chalao", {"action": "temp", "value": 24}),
    ("ac ka temperature 18 karo", {"action": "temp", "value": 18}),
    ("ac 2 degree kam karo", {"action": "delta", "value": -2}),
    ("ac thoda thanda karo", {"action": "delta", "value": -1}),
    ("ac temperature badhao", {"action": "delta", "value": 1}),
    ("ac fan high karo", {"action": "fan", "value": "high"}),
    ("ac ka fan tez karo", {"action": "fan", "value": "high"}),
    ("ac fan mode", {"action": "mode", "value": "fan"}),
    ("ac dry mode pe karo", {"action": "mode", "value": "dry"}),
    ("ac turbo on karo", {"action": "feature", "name": "turbo", "on": True}),
    ("ac ki light band karo", {"action": "feature", "name": "display", "on": False}),
    ("ac swing band karo", {"action": "swing", "value": "off"}),
    ("ac eco 60 karo", {"action": "eco", "value": "60"}),
    ("ac ka status batao", {"action": "status"}),
    ("ac kholo", {"action": "open"}),
])
def test_parse_command(text, expected):
    assert ac.parse_command(text) == expected


@pytest.mark.parametrize("text", [
    "back karo", "aaj ka mausam kaisa hai", "account kholo", "kamre ka temperature", "play music",
])
def test_parse_command_ignores_non_ac(text):
    assert ac.parse_command(text) is None


def test_room_word_is_not_a_decrease():
    assert ac.parse_command("ac kamre ka temperature kitna hai") == {"action": "status"}


def test_parse_state():
    d = ac.parse_state(
        {"thing_id": "T1", "name": "Bedroom"},
        {"connected": True, "reported": {"pow": 1, "mode": 3, "stemp": "23.0", "ctemp": "29.5",
                                         "fspd": 4, "vswing": 0, "eco": {"value": 2}, "turbo": 3,
                                         "hswing": 6, "err": 18, "rssi": -61}},
    )
    assert d["power"] and d["mode"] == "dry" and d["target"] == 23.0 and d["room"] == 29.5
    assert d["fan"] == "high" and d["vswing"] == "auto" and d["eco"] == "80"
    assert d["turbo"] is True and d["hswing"] is False and d["error"] == "E1"


@pytest.fixture
def fake_ac(monkeypatch):
    """One online AC; records what would be sent to the cloud."""
    sent = []
    dev = ac.parse_state({"thing_id": "T1", "name": "Hall"},
                         {"reported": {"pow": 0, "mode": 2, "stemp": "25.0", "fspd": 7}})

    class Fake:
        def send(self, thing_id, updates):
            sent.append((thing_id, updates))

    monkeypatch.setenv("BLUESTAR_USER", "u")
    monkeypatch.setenv("BLUESTAR_PASSWORD", "p")
    monkeypatch.setattr(ac, "_get_client", lambda: Fake())
    monkeypatch.setattr(ac, "devices", lambda **_: [dev])
    monkeypatch.setattr(ac, "_pending", {})
    return dev, sent


def test_power_on_uses_mode_command(fake_ac):
    dev, sent = fake_ac
    ac.power(True)
    assert sent == [("T1", {"pow": 1, "lcmd": "mode",
                            "mode": {"value": 2, "stemp": "25.0", "fspd": 7}})]
    assert dev["power"] is True


def test_set_temp_clamps_and_switches_on(fake_ac):
    _, sent = fake_ac
    ac.set_temp(40)
    assert sent[-1][1]["mode"]["stemp"] == "30.0" and sent[-1][1]["pow"] == 1
    ac.set_temp(22)
    assert sent[-1][1] == {"stemp": "22.0", "lcmd": "stemp"}


def test_feature_and_off(fake_ac):
    _, sent = fake_ac
    ac.set_feature("hswing", True)
    ac.power(False)
    assert sent == [("T1", {"hswing": 0, "lcmd": "hswing"}), ("T1", {"pow": 0, "lcmd": "pow"})]


def test_offline_ac_is_refused(fake_ac):
    dev, sent = fake_ac
    dev["online"] = False
    with pytest.raises(ac.ACError):
        ac.power(True)
    assert sent == []


def test_voice_delta_from_current_target(fake_ac):
    _, sent = fake_ac
    r = _ac_command("ac 2 degree kam karo", hi=False, web_ui=True)
    assert r.ok and "23" in r.message
    assert sent[-1][1]["mode"]["stemp"] == "23.0"


def test_voice_not_configured(monkeypatch):
    monkeypatch.delenv("BLUESTAR_USER", raising=False)
    monkeypatch.delenv("BLUESTAR_PASSWORD", raising=False)
    r = _ac_command("ac on karo", hi=False, web_ui=True)
    assert not r.ok and r.kind == "ac_open" and "BLUESTAR_USER" in r.message
    assert _ac_command("ac kholo", hi=False, web_ui=True).kind == "ac_open"
    assert _ac_command("weather batao", hi=False, web_ui=True) is None


@pytest.fixture
def ac_files(tmp_path, monkeypatch):
    monkeypatch.setattr(ac, "HISTORY_FILE", tmp_path / "h.jsonl")
    monkeypatch.setattr(ac, "SCHEDULE_FILE", tmp_path / "s.json")
    monkeypatch.setattr(ac, "_fired", set())
    monkeypatch.setattr(ac, "_timer_at", None)
    return tmp_path


def test_history_run_time_and_energy(ac_files, monkeypatch):
    monkeypatch.setenv("BLUESTAR_WATTS", "1000")
    monkeypatch.setenv("BLUESTAR_RATE", "10")
    now = 1_800_000_000
    on = {"power": True, "online": True, "room": 28.0, "target": 24.0, "mode": "cool"}
    for i in range(12):  # one hour of 5-minute samples, AC on the whole time
        ac.record_sample(on, now=now - 3600 + i * 300)
    h = ac.history(7, now=now)
    assert h["on_minutes"] == 60 and h["kwh"] == 1.0 and h["cost"] == 10.0
    assert h["avg_target"] == 24.0 and len(h["points"]) == 12


def test_history_empty(ac_files):
    h = ac.history(1)
    assert h["on_minutes"] == 0 and h["since"] is None and h["points"] == []


def test_schedule_rules(ac_files):
    rules = ac.schedule_add("Night", "22:00", "6:30", 35, "dry")
    assert rules[0]["end"] == "06:30" and rules[0]["temp"] == 30
    rid = rules[0]["id"]
    assert ac.schedule_update(rid, enabled=False)[0]["enabled"] is False
    assert ac.schedule_update(rid, delete=True) == []
    with pytest.raises(ac.ACError):
        ac.schedule_add("x", "25:00", "06:00", 24, "cool")
    with pytest.raises(ac.ACError):
        ac.schedule_add("x", "05:00", "06:00", 24, "heat")


def test_tick_runs_rule_once_and_timer(ac_files, fake_ac):
    _, sent = fake_ac
    ac.schedule_add("Morning", "06:00", "08:00", 23, "cool")
    ac._tick(datetime(2026, 10, 14, 6, 0))
    ac._tick(datetime(2026, 10, 14, 6, 0))  # same minute again: must not fire twice
    assert len(sent) == 1 and sent[0][1]["mode"]["stemp"] == "23.0"
    ac._tick(datetime(2026, 10, 14, 8, 0))
    assert sent[-1][1] == {"pow": 0, "lcmd": "pow"}
    assert ac.set_timer(30) and 1790 <= ac.timer_left() <= 1800
    assert ac.set_timer(0) is None


def test_write_env_replaces_only_active_lines(tmp_path):
    env = tmp_path / ".env"
    env.write_text("# BLUESTAR_USER=old\nSARA_CITY=Delhi\nBLUESTAR_USER=1\nBLUESTAR_PASSWORD=x", encoding="utf-8")
    ac._write_env(env, {"BLUESTAR_USER": "98765", "BLUESTAR_PASSWORD": "p=w d"})
    lines = env.read_text(encoding="utf-8").splitlines()
    assert lines == ["# BLUESTAR_USER=old", "SARA_CITY=Delhi", "BLUESTAR_USER=98765", "BLUESTAR_PASSWORD=p=w d"]


def test_save_credentials(tmp_path, monkeypatch):
    class Good:
        def __init__(self, user, password):
            self.user = user

        def login(self):
            pass

        def devices(self):
            return [{"id": "T1"}]

    monkeypatch.setattr(ac, "_ROOT", tmp_path)
    monkeypatch.setattr(ac, "BlueStarClient", Good)
    monkeypatch.delenv("BLUESTAR_USER", raising=False)
    monkeypatch.delenv("BLUESTAR_PASSWORD", raising=False)
    assert ac.save_credentials(" 98765 ", "pw") == 1
    assert "BLUESTAR_USER=98765" in (tmp_path / ".env").read_text(encoding="utf-8")
    assert ac.configured()
    monkeypatch.delenv("BLUESTAR_USER")
    monkeypatch.delenv("BLUESTAR_PASSWORD")


def test_save_credentials_rejects_bad_input_and_bad_login(tmp_path, monkeypatch):
    monkeypatch.setattr(ac, "_ROOT", tmp_path)
    with pytest.raises(ac.ACError):
        ac.save_credentials("", "pw")
    with pytest.raises(ac.ACError):
        ac.save_credentials("u", "line1\nline2")

    class Bad:
        def __init__(self, *a):
            pass

        def login(self):
            raise ac.ACError("Blue Star login failed")

    monkeypatch.setattr(ac, "BlueStarClient", Bad)
    with pytest.raises(ac.ACError):
        ac.save_credentials("u", "pw")
    assert not (tmp_path / ".env").exists()


def test_daily_report_splits_across_midnight(ac_files, monkeypatch):
    monkeypatch.setenv("BLUESTAR_WATTS", "1000")
    monkeypatch.setenv("BLUESTAR_RATE", "10")
    now = datetime(2026, 10, 13, 12, 0).timestamp()
    t = datetime(2026, 10, 12, 23, 0).timestamp()
    on = {"power": True, "online": True, "room": 28.0, "target": 24.0, "mode": "cool"}
    while t <= datetime(2026, 10, 13, 1, 0).timestamp():  # 23:00 -> 01:00, AC on
        ac.record_sample(on, now=t)
        t += 300
    ac.record_sample({**on, "power": False}, now=t)  # 01:05 switched off
    rep = ac.daily_report(3, now=now)
    assert [d["date"] for d in rep["days"]] == ["2026-10-11", "2026-10-12", "2026-10-13"]
    assert [d["on_minutes"] for d in rep["days"]] == [0, 60, 65]
    assert rep["days"][1]["kwh"] == 1.0 and rep["days"][1]["cost"] == 10.0
    assert rep["total_minutes"] == 125 and rep["days"][2]["avg_target"] == 24.0
    assert rep["days"][2]["max_room"] == 28.0


def test_report_csv_and_text(ac_files):
    assert ac.report_csv(2).splitlines()[0].startswith("date,on_minutes")
    assert "No AC run-time" in ac.report_text(7)
    now = datetime.now().timestamp()
    ac.record_sample({"power": True, "online": True, "room": 27.0, "target": 24.0, "mode": "cool"}, now=now - 600)
    ac.record_sample({"power": True, "online": True, "room": 26.0, "target": 24.0, "mode": "cool"}, now=now - 300)
    assert "Last 7 days:" in ac.report_text(7)


@pytest.mark.parametrize("text, days", [
    ("ac roz kitna chala", 7), ("ac usage this week", 7), ("ac aaj kitni der chala", 1),
    ("ac usage report", 7), ("ac bijli kharcha is month", 30),
])
def test_parse_report(text, days):
    assert ac.parse_command(text) == {"action": "report", "days": days}


def test_power_off_is_not_a_report():
    assert ac.parse_command("ac power off karo") == {"action": "power", "on": False}
