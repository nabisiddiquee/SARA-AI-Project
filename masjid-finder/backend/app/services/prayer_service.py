"""Prayer times calculated locally (no external API, no rate limits).

Astronomy follows the widely used PrayTimes.org algorithm (sun declination
and equation of time from the date; Fajr/Isha from the sun's depression angle;
Asr from shadow length; sunrise/sunset at 0.833 deg below the horizon). High
latitudes use the "angle-based" rule. Results match public services such as
Aladhan for the same method (tests/test_prayer_times.py checks this).

These are CALCULATED times. A masjid's own Azan / Jamaat schedule can differ.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date as Date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from app.services.cache import TTLCache


@dataclass(frozen=True)
class Method:
    name: str
    fajr: float                      # sun depression angle (degrees)
    isha: float | None = None        # angle, or None when isha_minutes is used
    isha_minutes: int | None = None  # minutes after Maghrib (Umm al-Qura, Gulf, Qatar)
    maghrib_angle: float | None = None  # Shia methods use an angle instead of sunset


METHODS: dict[str, Method] = {
    "MWL": Method("Muslim World League", 18, 17),
    "ISNA": Method("Islamic Society of North America", 15, 15),
    "EGYPT": Method("Egyptian General Authority of Survey", 19.5, 17.5),
    "MAKKAH": Method("Umm al-Qura University, Makkah", 18.5, isha_minutes=90),
    "KARACHI": Method("University of Islamic Sciences, Karachi", 18, 18),
    "TEHRAN": Method("Institute of Geophysics, University of Tehran", 17.7, 14, maghrib_angle=4.5),
    "JAFARI": Method("Shia Ithna-Ashari, Leva Institute, Qum", 16, 14, maghrib_angle=4),
    "GULF": Method("Gulf Region", 19.5, isha_minutes=90),
    "KUWAIT": Method("Kuwait", 18, 17.5),
    "QATAR": Method("Qatar", 18, isha_minutes=90),
    "SINGAPORE": Method("Majlis Ugama Islam Singapura", 20, 18),
    "TURKEY": Method("Diyanet Isleri Baskanligi, Turkey", 18, 17),
    "FRANCE": Method("Union des Organisations Islamiques de France", 12, 12),
    "RUSSIA": Method("Spiritual Administration of Muslims of Russia", 16, 15),
    "DUBAI": Method("Dubai (General Authority of Islamic Affairs)", 18.2, 18.2),
}

# Usual method by country (ISO 3166-1 alpha-2) when PRAYER_METHOD=auto.
COUNTRY_METHOD = {
    "IN": "KARACHI", "PK": "KARACHI", "BD": "KARACHI", "AF": "KARACHI", "NP": "KARACHI", "LK": "KARACHI",
    "SA": "MAKKAH", "YE": "MAKKAH", "AE": "DUBAI", "KW": "KUWAIT", "QA": "QATAR", "BH": "GULF", "OM": "GULF",
    "EG": "EGYPT", "SD": "EGYPT", "LY": "EGYPT", "SY": "EGYPT", "LB": "EGYPT", "JO": "EGYPT", "IQ": "EGYPT",
    "US": "ISNA", "CA": "ISNA", "TR": "TURKEY", "IR": "TEHRAN", "SG": "SINGAPORE", "MY": "SINGAPORE",
    "ID": "SINGAPORE", "BN": "SINGAPORE", "FR": "FRANCE", "RU": "RUSSIA",
}
# Hanafi Asr (shadow factor 2) is the norm in South Asia; standard (1) elsewhere.
HANAFI_COUNTRIES = {"IN", "PK", "BD", "AF"}

PRAYERS = ("fajr", "sunrise", "dhuhr", "asr", "maghrib", "isha")
_LABELS = {"fajr": "Fajr", "sunrise": "Sunrise", "dhuhr": "Dhuhr", "asr": "Asr", "maghrib": "Maghrib", "isha": "Isha"}
_cache = TTLCache(ttl=6 * 3600, max_items=5000)


# --- degree trigonometry ---------------------------------------------------------
def _dsin(d): return math.sin(math.radians(d))
def _dcos(d): return math.cos(math.radians(d))
def _dtan(d): return math.tan(math.radians(d))
def _darcsin(x): return math.degrees(math.asin(x))
def _darccos(x): return math.degrees(math.acos(x)) if -1 <= x <= 1 else math.nan
def _darctan2(y, x): return math.degrees(math.atan2(y, x))
def _darccot(x): return math.degrees(math.atan(1 / x))
def _fix(a, b): return a - b * math.floor(a / b)


def _julian(y: int, m: int, d: int) -> float:
    if m <= 2:
        y -= 1
        m += 12
    a = math.floor(y / 100)
    b = 2 - a + math.floor(a / 4)
    return math.floor(365.25 * (y + 4716)) + math.floor(30.6001 * (m + 1)) + d + b - 1524.5


class _Calc:
    def __init__(self, lat: float, lng: float, day: Date) -> None:
        self.lat, self.lng = lat, lng
        self.jdate = _julian(day.year, day.month, day.day) - lng / (15 * 24)

    def sun_position(self, jd: float) -> tuple[float, float]:
        d = jd - 2451545.0
        g = _fix(357.529 + 0.98560028 * d, 360)
        q = _fix(280.459 + 0.98564736 * d, 360)
        L = _fix(q + 1.915 * _dsin(g) + 0.020 * _dsin(2 * g), 360)
        e = 23.439 - 0.00000036 * d
        ra = _darctan2(_dcos(e) * _dsin(L), _dcos(L)) / 15
        eqt = q / 15 - _fix(ra, 24)
        decl = _darcsin(_dsin(e) * _dsin(L))
        return decl, eqt

    def mid_day(self, t: float) -> float:
        return _fix(12 - self.sun_position(self.jdate + t)[1], 24)

    def sun_angle_time(self, angle: float, t: float, ccw: bool = False) -> float:
        decl = self.sun_position(self.jdate + t)[0]
        noon = self.mid_day(t)
        x = (-_dsin(angle) - _dsin(decl) * _dsin(self.lat)) / (_dcos(decl) * _dcos(self.lat))
        tt = _darccos(x) / 15
        return noon + (-tt if ccw else tt)

    def asr_time(self, factor: int, t: float) -> float:
        decl = self.sun_position(self.jdate + t)[0]
        angle = -_darccot(factor + _dtan(abs(self.lat - decl)))
        return self.sun_angle_time(angle, t)


def _diff(a: float, b: float) -> float:
    return _fix(b - a, 24)


def compute(lat: float, lng: float, day: Date, tz: str, method: str, asr: str) -> dict[str, float]:
    """Prayer times as decimal hours in the location's local time."""
    m = METHODS[method]
    offset = ZoneInfo(tz).utcoffset(datetime.combine(day, time(12))).total_seconds() / 3600
    c = _Calc(lat, lng, day)
    rise_set = 0.833
    t = {"fajr": 5, "sunrise": 6, "dhuhr": 12, "asr": 13, "sunset": 18, "maghrib": 18, "isha": 18}
    out = {
        "fajr": c.sun_angle_time(m.fajr, t["fajr"] / 24, ccw=True),
        "sunrise": c.sun_angle_time(rise_set, t["sunrise"] / 24, ccw=True),
        "dhuhr": c.mid_day(t["dhuhr"] / 24),
        "asr": c.asr_time(2 if asr == "hanafi" else 1, t["asr"] / 24),
        "sunset": c.sun_angle_time(rise_set, t["sunset"] / 24),
        "maghrib": c.sun_angle_time(m.maghrib_angle if m.maghrib_angle else rise_set, t["maghrib"] / 24),
        "isha": c.sun_angle_time(m.isha, t["isha"] / 24) if m.isha is not None else math.nan,
    }
    for k in out:                                  # to local time
        out[k] += offset - lng / 15
    if m.isha_minutes is not None:
        out["isha"] = out["maghrib"] + m.isha_minutes / 60

    # High latitudes ("angle-based"): Fajr/Isha can't be later/earlier than a
    # share of the night, or may not exist at all in summer.
    night = _diff(out["sunset"], out["sunrise"])
    portion = m.fajr / 60 * night
    if math.isnan(out["fajr"]) or _diff(out["fajr"], out["sunrise"]) > portion:
        out["fajr"] = out["sunrise"] - portion
    if m.isha is not None:
        portion = m.isha / 60 * night
        if math.isnan(out["isha"]) or _diff(out["sunset"], out["isha"]) > portion:
            out["isha"] = out["sunset"] + portion
    del out["sunset"]
    return out


def _hhmm(h: float) -> str:
    h = _fix(h + 0.5 / 60, 24)                     # round to the nearest minute
    return f"{int(h):02d}:{int((h - int(h)) * 60):02d}"


def _12h(hhmm: str) -> str:
    h, m = map(int, hhmm.split(":"))
    return f"{(h % 12) or 12}:{m:02d} {'AM' if h < 12 else 'PM'}"


def resolve_method(method: str, asr: str, country: str | None) -> tuple[str, str]:
    cc = (country or "").upper()
    m = method.upper()
    if m == "AUTO" or m not in METHODS:
        m = COUNTRY_METHOD.get(cc, "MWL")
    a = asr.lower()
    if a not in ("standard", "hanafi"):
        a = "hanafi" if cc in HANAFI_COUNTRIES else "standard"
    return m, a


def prayer_times(lat: float, lng: float, day: Date, tz: str, method: str, asr: str,
                 now: datetime | None = None) -> dict:
    key = (round(lat, 3), round(lng, 3), day.isoformat(), tz, method, asr)
    times = _cache.get(key)
    if times is None:
        raw = compute(lat, lng, day, tz, method, asr)
        times = {p: _hhmm(raw[p]) for p in PRAYERS}
        _cache.set(key, times)

    zone = ZoneInfo(tz)
    now = now.astimezone(zone) if now else datetime.now(zone)
    next_prayer = None
    if now.date() == day:
        for p in ("fajr", "dhuhr", "asr", "maghrib", "isha"):
            h, mnt = map(int, times[p].split(":"))
            at = datetime.combine(day, time(h, mnt), zone)
            if at > now:
                next_prayer = {"name": _LABELS[p], "time": _12h(times[p]), "datetime": at.isoformat(),
                               "seconds_remaining": int((at - now).total_seconds()), "is_tomorrow": False}
                break
        if next_prayer is None:                    # after Isha -> tomorrow's Fajr
            tomorrow = day + timedelta(days=1)
            fajr = _hhmm(compute(lat, lng, tomorrow, tz, method, asr)["fajr"])
            h, mnt = map(int, fajr.split(":"))
            at = datetime.combine(tomorrow, time(h, mnt), zone)
            next_prayer = {"name": "Fajr", "time": _12h(fajr), "datetime": at.isoformat(),
                           "seconds_remaining": int((at - now).total_seconds()), "is_tomorrow": True}

    return {
        "date": day.isoformat(),
        "timezone": tz,
        "location": {"latitude": lat, "longitude": lng},
        "method": {"id": method, "name": METHODS[method].name,
                   "asr": "Hanafi (shadow x2)" if asr == "hanafi" else "Standard (Shafi'i, Maliki, Hanbali)"},
        "times": times,
        "times_12h": {p: _12h(v) for p, v in times.items()},
        "next_prayer": next_prayer,
    }
