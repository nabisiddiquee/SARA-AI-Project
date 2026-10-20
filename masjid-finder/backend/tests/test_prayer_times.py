"""Prayer calculation checks (offline).

Reference values come from the Aladhan API (api.aladhan.com) for the same
method/school, fetched 2026-10-02. We allow +/- 2 minutes for rounding.
"""

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from app.services import prayer_service as ps


def _minutes(hhmm: str) -> int:
    h, m = map(int, hhmm.split(":"))
    return h * 60 + m


DELHI_ALADHAN = {  # method=1 (Karachi), school=1 (Hanafi)
    "fajr": "04:56", "sunrise": "06:15", "dhuhr": "12:11",
    "asr": "16:27", "maghrib": "18:06", "isha": "19:25",
}


def test_delhi_matches_aladhan():
    out = ps.prayer_times(28.6139, 77.2090, date(2026, 10, 2), "Asia/Kolkata", "KARACHI", "hanafi")
    for name, ref in DELHI_ALADHAN.items():
        assert abs(_minutes(out["times"][name]) - _minutes(ref)) <= 2, (name, out["times"][name], ref)


def test_order_and_format():
    out = ps.prayer_times(21.4225, 39.8262, date(2026, 10, 2), "Asia/Riyadh", "MAKKAH", "standard")
    t = [_minutes(out["times"][p]) for p in ps.PRAYERS]
    assert t == sorted(t)
    # Umm al-Qura: Isha is exactly 90 minutes after Maghrib
    assert _minutes(out["times"]["isha"]) - _minutes(out["times"]["maghrib"]) == 90


def test_auto_method_by_country():
    assert ps.resolve_method("auto", "auto", "IN") == ("KARACHI", "hanafi")
    assert ps.resolve_method("auto", "auto", "SA") == ("MAKKAH", "standard")
    assert ps.resolve_method("auto", "auto", None) == ("MWL", "standard")
    assert ps.resolve_method("ISNA", "hanafi", "IN") == ("ISNA", "hanafi")


def test_next_prayer_and_after_isha():
    tz = ZoneInfo("Asia/Kolkata")
    day = date(2026, 10, 2)
    noon = datetime(2026, 10, 2, 13, 0, tzinfo=tz)
    out = ps.prayer_times(28.6139, 77.2090, day, "Asia/Kolkata", "KARACHI", "hanafi", now=noon)
    assert out["next_prayer"]["name"] == "Asr" and not out["next_prayer"]["is_tomorrow"]
    late = datetime(2026, 10, 2, 23, 0, tzinfo=tz)
    out = ps.prayer_times(28.6139, 77.2090, day, "Asia/Kolkata", "KARACHI", "hanafi", now=late)
    assert out["next_prayer"]["name"] == "Fajr" and out["next_prayer"]["is_tomorrow"]


@pytest.mark.parametrize("lat,lng,tz", [(59.33, 18.07, "Europe/Stockholm"), (64.14, -21.94, "Atlantic/Reykjavik")])
def test_high_latitude_summer_has_valid_times(lat, lng, tz):
    out = ps.prayer_times(lat, lng, date(2026, 6, 21), tz, "MWL", "standard")
    for v in out["times"].values():
        assert len(v) == 5 and v[2] == ":"


def test_makkah_matches_aladhan():
    ref = {"fajr": "04:56", "sunrise": "06:12", "dhuhr": "12:10", "asr": "15:34", "maghrib": "18:08", "isha": "19:38"}
    out = ps.prayer_times(21.4225, 39.8262, date(2026, 10, 2), "Asia/Riyadh", "MAKKAH", "standard")
    for name, v in ref.items():
        assert abs(_minutes(out["times"][name]) - _minutes(v)) <= 2, (name, out["times"][name], v)
